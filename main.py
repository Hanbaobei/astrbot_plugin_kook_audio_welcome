"""KOOK 语音欢迎插件。

当成员进入 KOOK 语音频道时，机器人自动加入该频道、播放欢迎音频，播放结束后
立即离开。

语音频道事件说明
----------------
AstrBot 自带的 KOOK 适配器只把聊天消息转换成 AstrBot 事件，网关收到的系统
消息（``type == 255``）除角色变更外都会被丢弃。因此本插件在加载后挂接
``KookClient.event_callback``，在不新建第二条网关连接、不重复占用机器人会话
的前提下，额外拿到 ``joined_channel`` / ``exited_channel`` 等系统事件。

挂接是可逆的：插件卸载或重载时会还原原始回调，热重载也不会重复播放。

开发者文档：https://docs.astrbot.app/dev/star/plugin-new.html
KOOK 语音接口：https://developer.kookapp.cn/doc/http/voice
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import re
import shutil
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse
from uuid import uuid4

import aiohttp

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import File, Record
from astrbot.api.star import Context, Star, StarTools, register

PLUGIN_NAME = "astrbot_plugin_kook_audio_welcome"
PLUGIN_VERSION = "v1.4.3"

# 诊断日志单文件上限，超过后轮转为 .log.1
LOG_MAX_BYTES = 1024 * 1024

KOOK_API_BASE = "https://www.kookapp.cn/api/v3"
KOOK_SYSTEM_MESSAGE_TYPE = 255
KOOK_VOICE_JOINED = "joined_channel"
KOOK_VOICE_EXITED = "exited_channel"

# KOOK 官方要求：长时间断流会回收端口资源，建议每 45 秒调用一次 keep-alive
VOICE_KEEP_ALIVE_INTERVAL = 40
# /voice/leave 之后需要等待 2-3 秒才能重新加入
STALE_VOICE_RELEASE_WAIT = 3
# 加入频道后等待多久再开始播放：空频道里第一个进入的人，客户端往往还没建好语音接收
DEFAULT_START_DELAY = 2
# 音频末尾附加静音的时长：避免 RTP 流一断，服务端就把缓冲中的音频丢弃
# （会预先拼进音频文件，整段播放只使用一条 RTP 流）
DEFAULT_LEAVE_DELAY = 2
# FFmpeg 播放超时余量（秒）
PLAYBACK_TIMEOUT_MARGIN = 20
DEFAULT_PLAYBACK_TIMEOUT = 180
# 上传音频大小上限
MAX_AUDIO_BYTES = 25 * 1024 * 1024
# 超过这个时长的音频不再预拼尾部静音，避免生成过大的中间 WAV
MAX_PAD_AUDIO_SECONDS = 300
# 下载音频用的 UA：部分 CDN 会拒绝空 UA 的请求
DOWNLOAD_USER_AGENT = f"AstrBotKOOKAudioWelcome/{PLUGIN_VERSION.lstrip('v')}"

AUDIO_EXTS = {".mp3", ".wav", ".m4a", ".ogg", ".flac", ".aac", ".opus", ".webm"}

# 欢迎音频来源（对应 _conf_schema.json 中 audio_source 的 options）
SOURCE_UPLOAD = "upload"
SOURCE_URL = "url"
SOURCE_PATH = "path"
SOURCE_AUTO = "auto"
SOURCE_LABELS = {
    SOURCE_UPLOAD: "上传的音频文件",
    SOURCE_URL: "网络链接（URL）",
    SOURCE_PATH: "本地文件路径",
    SOURCE_AUTO: "自动查找 welcome.*",
}

# URL 音频的本地缓存目录；聊天上传的音频存放目录；音频+尾部静音的合成缓存目录
URL_CACHE_DIR_NAME = "audio_cache"
CHAT_AUDIO_DIR_NAME = "audio"
STREAM_CACHE_DIR_NAME = "stream_cache"

CONTENT_TYPE_SUFFIX = {
    "audio/mpeg": ".mp3",
    "audio/mp3": ".mp3",
    "audio/wav": ".wav",
    "audio/x-wav": ".wav",
    "audio/wave": ".wav",
    "audio/ogg": ".ogg",
    "audio/mp4": ".m4a",
    "audio/aac": ".aac",
    "audio/flac": ".flac",
    "audio/x-flac": ".flac",
    "audio/webm": ".webm",
}

# 用于识别「本插件挂接的回调」，方便热重载时先解开旧包装，避免重复播放
_WRAPPER_FLAG = "__kook_audio_welcome_wrapper__"
_WRAPPER_ORIGINAL = "__kook_audio_welcome_original__"


# --------------------------------------------------------------------------- #
# 通用小工具
# --------------------------------------------------------------------------- #


def to_mapping(value: Any) -> dict[str, Any]:
    """把 pydantic 模型 / 字典统一转成 dict，失败时返回空字典。"""
    if isinstance(value, dict):
        return value
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        try:
            result = dump(mode="python")
        except TypeError:
            result = dump()
        if isinstance(result, dict):
            return result
    return {}


def to_text(value: Any) -> str:
    """把枚举 / 数字 / None 统一转成去空白的字符串。"""
    if value is None:
        return ""
    raw = getattr(value, "value", value)
    return str(raw).strip()


def to_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def to_bool(value: Any, default: bool = False) -> bool:
    """把 bool / 数字 / 字符串统一转成布尔值。

    KOOK 接口里部分字段是字符串（例如 ``"false"``），直接用 ``bool()`` 会得到
    True，必须显式解析。
    """
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off", ""}:
        return False
    return default


def extract_channel_event(
    data: Any, event_type: str
) -> tuple[str, str, str] | None:
    """从 KOOK 网关事件中解析语音频道的加入 / 退出事件。

    Returns:
        ``(user_id, channel_id, guild_id)``；不是该事件时返回 ``None``。
    """
    payload = to_mapping(data)
    if not payload:
        return None
    if to_int(payload.get("type"), -1) != KOOK_SYSTEM_MESSAGE_TYPE:
        return None

    extra = to_mapping(payload.get("extra"))
    if to_text(extra.get("type")) != event_type:
        return None

    body = to_mapping(extra.get("body"))
    user_id = to_text(body.get("user_id"))
    channel_id = to_text(body.get("channel_id"))
    guild_id = to_text(payload.get("target_id"))
    if not user_id:
        return None
    return user_id, channel_id, guild_id


def extract_joined_channel_event(data: Any) -> tuple[str, str, str] | None:
    """从 KOOK 网关事件中解析「用户加入语音频道」。

    Returns:
        ``(user_id, channel_id, guild_id)``；不是该事件时返回 ``None``。
    """
    parsed = extract_channel_event(data, KOOK_VOICE_JOINED)
    if parsed is None:
        return None
    if not parsed[1]:
        # 加入事件必须带频道 ID，否则无法决定加入哪个频道
        return None
    return parsed


def extract_exited_channel_event(data: Any) -> tuple[str, str, str] | None:
    """从 KOOK 网关事件中解析「用户退出语音频道」。"""
    return extract_channel_event(data, KOOK_VOICE_EXITED)


async def run_capture(
    args: list[str],
    timeout: float,
    on_start: Callable[[asyncio.subprocess.Process], None] | None = None,
) -> tuple[int, str, bool]:
    """执行子进程并收集输出。

    返回 ``(returncode, output, timed_out)``。Windows 上如果 AstrBot 使用了
    Selector 事件循环，``create_subprocess_exec`` 不可用，这里会自动退回线程中
    的同步 ``subprocess.run``，保证插件仍能工作。

    ``on_start`` 会在子进程创建后立即收到进程对象，便于调用方在播放中途
    主动结束推流（例如机器人被移出语音频道）。
    """
    try:
        process = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
    except NotImplementedError:
        return await asyncio.to_thread(_run_capture_sync, args, timeout)

    if on_start is not None:
        on_start(process)

    try:
        stdout, _ = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except asyncio.CancelledError:
        # 插件卸载 / 任务取消时不要留下仍在推流的 FFmpeg 进程
        with contextlib.suppress(ProcessLookupError):
            process.kill()
        with contextlib.suppress(Exception):
            await process.wait()
        raise
    except asyncio.TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            process.kill()
        with contextlib.suppress(Exception):
            await process.wait()
        return -1, "", True

    return (
        process.returncode if process.returncode is not None else -1,
        (stdout or b"").decode("utf-8", "ignore"),
        False,
    )


def _run_capture_sync(args: list[str], timeout: float) -> tuple[int, str, bool]:
    try:
        completed = subprocess.run(  # noqa: S603 - 参数由本插件拼装
            args,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        output = exc.stdout or b""
        return -1, output.decode("utf-8", "ignore"), True
    return completed.returncode, completed.stdout.decode("utf-8", "ignore"), False


def resolve_ffprobe(ffmpeg_path: str) -> str:
    """根据 FFmpeg 路径推断同级目录下的 ffprobe。"""
    if not ffmpeg_path:
        return "ffprobe"
    base = os.path.basename(ffmpeg_path)
    if base.lower().startswith("ffprobe"):
        return ffmpeg_path
    directory = os.path.dirname(ffmpeg_path)
    suffix = ".exe" if base.lower().endswith(".exe") else ""
    probe = f"ffprobe{suffix}"
    return os.path.join(directory, probe) if directory else probe


def ffprobe_available(ffmpeg_path: str) -> bool:
    """判断推导出的 ffprobe 是否存在。

    用来区分「音频损坏」与「这台机器根本没装 ffprobe」：后者不应该阻止播放，
    只是拿不到时长，超时会退回默认值。
    """
    probe = resolve_ffprobe(ffmpeg_path)
    if os.path.isabs(probe) or os.path.dirname(probe):
        return Path(probe).is_file()
    return shutil.which(probe) is not None


async def probe_audio_duration(ffmpeg_path: str, audio_path: Path) -> float | None:
    """读取音频时长（秒），失败时返回 ``None``。"""
    args = [
        resolve_ffprobe(ffmpeg_path),
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "default=nw=1:nk=1",
        str(audio_path),
    ]
    try:
        code, output, timed_out = await run_capture(args, timeout=12)
    except (OSError, FileNotFoundError):
        return None
    if code != 0 or timed_out:
        return None
    for line in output.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            value = float(line)
        except ValueError:
            continue
        if value > 0:
            return value
    return None


# --------------------------------------------------------------------------- #
# KOOK 语音接口
# --------------------------------------------------------------------------- #


class KookApiError(RuntimeError):
    """KOOK 接口返回非 0 code 或网络异常。"""

    def __init__(self, code: Any, message: str, path: str = "") -> None:
        self.code = code
        self.kook_message = message
        self.path = path
        super().__init__(f"KOOK 接口 {path} 调用失败（code={code}）：{message}")


@dataclass(slots=True)
class VoiceJoinInfo:
    """``/voice/join`` 返回的推流信息。"""

    ip: str
    port: str
    rtcp_mux: bool
    rtcp_port: str
    bitrate: int
    audio_ssrc: str
    audio_pt: str

    @classmethod
    def from_data(cls, data: dict[str, Any]) -> VoiceJoinInfo:
        info = cls(
            ip=to_text(data.get("ip")),
            port=to_text(data.get("port")),
            rtcp_mux=to_bool(data.get("rtcp_mux"), True),
            rtcp_port=to_text(data.get("rtcp_port")),
            bitrate=to_int(data.get("bitrate"), 48000),
            audio_ssrc=to_text(data.get("audio_ssrc")) or "1111",
            audio_pt=to_text(data.get("audio_pt")) or "111",
        )
        if not info.ip or not info.port:
            raise KookApiError(-1, f"返回的推流地址不完整：{data}", "/voice/join")
        return info


class KookApi:
    """最小化的 KOOK HTTP 客户端，只覆盖语音相关接口。"""

    def __init__(self) -> None:
        self._session: aiohttp.ClientSession | None = None
        # 音频下载的地址可能是任意第三方域名，必须使用不复用 Authorization 的
        # 独立会话，否则等于把 Bot Token 一并发给对方服务器。
        self._download_session: aiohttp.ClientSession | None = None
        self._token = ""

    async def _ensure_session(self, token: str | None = None) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=30),
            )
        if token and token != self._token:
            self._token = token
            self._session.headers["Authorization"] = f"Bot {token}"
        return self._session

    async def _ensure_download_session(self) -> aiohttp.ClientSession:
        """获取下载音频专用的无鉴权会话。"""
        if self._download_session is None or self._download_session.closed:
            self._download_session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=60),
                headers={"User-Agent": DOWNLOAD_USER_AGENT},
            )
        return self._download_session

    async def request(
        self,
        method: str,
        path: str,
        token: str,
        *,
        params: dict[str, Any] | None = None,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not token:
            raise KookApiError(-1, "未配置 KOOK Bot Token", path)

        session = await self._ensure_session(token)
        url = f"{KOOK_API_BASE}{path}"
        try:
            async with session.request(
                method,
                url,
                params=params,
                json=payload,
            ) as response:
                text = await response.text()
                if response.status != 200:
                    raise KookApiError(
                        response.status,
                        f"HTTP {response.status}: {text[:300]}",
                        path,
                    )
                try:
                    result = json.loads(text)
                except json.JSONDecodeError as exc:
                    raise KookApiError(
                        -1, f"响应不是合法 JSON：{text[:300]}", path
                    ) from exc

                if to_int(result.get("code"), -1) != 0:
                    raise KookApiError(
                        result.get("code"),
                        str(result.get("message") or "未知错误"),
                        path,
                    )

                data = result.get("data")
                return data if isinstance(data, dict) else {}
        except aiohttp.ClientError as exc:
            raise KookApiError(-1, f"网络请求失败：{exc!s}", path) from exc
        except asyncio.TimeoutError as exc:
            raise KookApiError(-1, "请求超时", path) from exc

    async def join_voice(
        self,
        token: str,
        channel_id: str,
        password: str = "",
    ) -> VoiceJoinInfo:
        # 显式要求独立 RTCP 端口：rtcp_mux=true 时 FFmpeg 不会发送任何 RTCP，
        # 部分媒体服务器需要 RTCP SR 才会真正转发音频。
        payload: dict[str, Any] = {"channel_id": channel_id, "rtcp_mux": False}
        if password:
            payload["password"] = password
        data = await self.request("POST", "/voice/join", token, payload=payload)
        return VoiceJoinInfo.from_data(data)

    async def leave_voice(self, token: str, channel_id: str) -> None:
        await self.request(
            "POST",
            "/voice/leave",
            token,
            payload={"channel_id": channel_id},
        )

    async def keep_alive(self, token: str, channel_id: str) -> None:
        await self.request(
            "POST",
            "/voice/keep-alive",
            token,
            payload={"channel_id": channel_id},
        )

    async def voice_list(self, token: str) -> list[dict[str, Any]]:
        data = await self.request("GET", "/voice/list", token)
        items = data.get("items")
        if isinstance(items, list):
            return [item for item in items if isinstance(item, dict)]
        return []

    async def get_channel(self, token: str, channel_id: str) -> dict[str, Any]:
        return await self.request(
            "GET",
            "/channel/view",
            token,
            params={"target_id": channel_id},
        )

    async def get_me(self, token: str) -> dict[str, Any]:
        return await self.request("GET", "/user/me", token)

    async def download(self, url: str, limit: int) -> tuple[bytes, str]:
        """下载音频文件，返回 ``(内容, 后缀)``。

        注意：这里必须使用无鉴权会话。调用方传入的链接由用户提供（管理面板
        的欢迎音频链接、聊天里引用的音频），复用带 ``Authorization`` 的会话
        会把 Bot Token 泄露给该域名。
        """
        session = await self._ensure_download_session()
        try:
            async with session.get(url) as response:
                if response.status != 200:
                    raise KookApiError(
                        response.status,
                        f"下载音频失败：HTTP {response.status}",
                        "download",
                    )
                declared = to_int(response.headers.get("Content-Length"), 0)
                if declared > limit:
                    raise KookApiError(-1, "音频文件超过大小限制", "download")
                content = await response.read()
                if len(content) > limit:
                    raise KookApiError(-1, "音频文件超过大小限制", "download")

                content_type = (
                    (response.headers.get("Content-Type") or "")
                    .split(";")[0]
                    .strip()
                    .lower()
                )
        except aiohttp.ClientError as exc:
            raise KookApiError(-1, f"下载音频失败：{exc!s}", "download") from exc

        suffix = CONTENT_TYPE_SUFFIX.get(content_type, "")
        if not suffix:
            suffix = Path(urlparse(url).path).suffix.lower()
        if suffix not in AUDIO_EXTS:
            if content_type.startswith(("text/", "application/json")):
                raise KookApiError(
                    -1,
                    f"链接返回的不是音频文件（Content-Type: {content_type or '未知'}）",
                    "download",
                )
            suffix = ".mp3"
        return content, suffix

    async def close(self) -> None:
        for session in (self._session, self._download_session):
            if session and not session.closed:
                with contextlib.suppress(Exception):
                    await session.close()
        self._session = None
        self._download_session = None


# --------------------------------------------------------------------------- #
# FFmpeg 命令拼装
# --------------------------------------------------------------------------- #


def build_rtp_tee(info: VoiceJoinInfo) -> str:
    """构造 tee 的 RTP 输出目标（含 ssrc / payload_type / rtcp 端口）。"""
    target = f"rtp://{info.ip}:{info.port}"
    if not info.rtcp_mux and info.rtcp_port:
        target += f"?rtcpport={info.rtcp_port}"
    return (
        f"[select=a:f=rtp:ssrc={info.audio_ssrc}:payload_type={info.audio_pt}]{target}"
    )


def rtp_bitrate(info: VoiceJoinInfo) -> int:
    return info.bitrate if info.bitrate > 0 else 48000


def build_ffmpeg_args(
    *,
    ffmpeg: str,
    audio_path: Path | str,
    info: VoiceJoinInfo,
    volume: int,
) -> list[str]:
    """按 KOOK 官方示例拼装 FFmpeg 推流命令。

    官方示例：
    ``ffmpeg -i test.mp3 -re -map 0:a:0 -acodec libopus -ab 48k -ac 2 -ar 48000
    -filter:a volume=0.8 -f tee
    '[select=a:f=rtp:ssrc=1111:payload_type=111]rtp://ip:port?rtcpport=1001'``
    """
    volume = max(0, min(100, to_int(volume, 100)))

    args = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-re",
        "-i",
        str(audio_path),
        "-map",
        "0:a:0",
        "-vn",
        "-sn",
        "-dn",
        "-acodec",
        "libopus",
        "-b:a",
        str(rtp_bitrate(info)),
        "-ac",
        "2",
        "-ar",
        "48000",
    ]
    if volume != 100:
        args += ["-filter:a", f"volume={volume / 100:.2f}"]
    args += ["-f", "tee", build_rtp_tee(info)]
    return args


# --------------------------------------------------------------------------- #
# 插件主体
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class _Hook:
    client: Any
    original: Any
    wrapper: Any


@dataclass(slots=True)
class _PlayRequest:
    channel_id: str
    user_id: str = ""
    guild_id: str = ""
    reason: str = "event"


@register(
    PLUGIN_NAME,
    "HanZai",
    "成员进入 KOOK 语音频道时自动播放欢迎音频",
    PLUGIN_VERSION,
)
class KookAudioWelcomePlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig) -> None:
        super().__init__(context, config)
        self.config = config

        self._data_dir = self._resolve_data_dir()
        self._plugin_dir = Path(__file__).resolve().parent
        self._log_file = self._data_dir / "logs" / "kook_audio_welcome.log"
        self._api = KookApi()

        self._queue: asyncio.Queue[_PlayRequest] = asyncio.Queue(maxsize=1)
        self._worker: asyncio.Task | None = None
        self._attach_task: asyncio.Task | None = None
        self._ffmpeg_task: asyncio.Task | None = None

        self._hooks: dict[str, _Hook] = {}
        self._bot_id = ""
        self._voice_channel = ""
        self._playing = False
        self._last_finished = 0.0
        self._ffmpeg_cache: tuple[str, bool, str] | None = None
        # 当前正在推流的 FFmpeg 子进程；机器人被移出频道时用它立刻止损
        self._ffmpeg_proc: asyncio.subprocess.Process | None = None
        self._stream_aborted = False

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #

    async def initialize(self) -> None:
        """启动后台任务，并尝试挂接已经加载的 KOOK 适配器。"""
        self._log_startup()
        self._worker = asyncio.create_task(
            self._worker_loop(),
            name=f"{PLUGIN_NAME}-worker",
        )
        self._attach_task = asyncio.create_task(
            self._attach_loop(),
            name=f"{PLUGIN_NAME}-attach",
        )
        self._attach_all()
        self._ffmpeg_task = asyncio.create_task(
            self._log_ffmpeg_state(),
            name=f"{PLUGIN_NAME}-ffmpeg",
        )

    async def terminate(self) -> None:
        """还原回调、停止任务并释放语音连接。"""
        for platform_id in list(self._hooks):
            self._detach(platform_id)

        for task in (self._attach_task, self._worker, self._ffmpeg_task):
            if task and not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

        token = self._resolve_token()
        channel = self._voice_channel
        # 先结束仍在推流的子进程，避免卸载后还有 RTP 包发往语音服务器
        proc = self._ffmpeg_proc
        if proc is not None and proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
        self._ffmpeg_proc = None

        if token and channel:
            # worker 里的 finally 可能已经被取消打断，这里无条件补一次离开
            left = await self._leave_voice(token, channel)
            if not left:
                with contextlib.suppress(Exception):
                    await self._release_stale_voice(token, channel)
        self._voice_channel = ""

        await self._api.close()
        self._trace("插件卸载：已还原回调并释放语音资源")
        logger.info("[KOOK语音欢迎] 插件已卸载，语音资源已释放")

    # ------------------------------------------------------------------ #
    # 配置读取
    # ------------------------------------------------------------------ #

    def _conf_str(self, key: str, default: str = "") -> str:
        value = self.config.get(key, default)
        if value is None:
            return default
        return str(value).strip()

    def _conf_bool(self, key: str, default: bool = True) -> bool:
        value = self.config.get(key, default)
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on", "是", "开"}
        return bool(value)

    def _conf_int(self, key: str, default: int, minimum: int, maximum: int) -> int:
        value = to_int(self.config.get(key, default), default)
        return max(minimum, min(maximum, value))

    def _conf_list(self, key: str) -> list[str]:
        """读取列表型配置（WebUI 的 file 类型会保存为路径列表）。"""
        value = self.config.get(key, [])
        if isinstance(value, str):
            return [value.strip()] if value.strip() else []
        if not isinstance(value, (list, tuple, set)):
            return []
        return [str(item).strip() for item in value if str(item).strip()]

    @property
    def _audio_source(self) -> str:
        mode = self._conf_str("audio_source") or SOURCE_UPLOAD
        if mode not in SOURCE_LABELS:
            # 兼容旧版本：只有 welcome_audio 字符串配置时按本地路径处理
            return SOURCE_PATH if mode == "local" else SOURCE_UPLOAD
        return mode

    @property
    def _chat_audio_dir(self) -> Path:
        return self._data_dir / CHAT_AUDIO_DIR_NAME

    def _candidate_dirs(self) -> list[Path]:
        """解析相对路径时依次尝试的基准目录。"""
        directories = [self._data_dir, self._plugin_dir, Path.cwd()]
        data_root = self._data_dir.parent.parent  # data/plugin_data/<name> -> data
        if data_root not in directories:
            directories.append(data_root)
        try:
            from astrbot.core.utils.astrbot_path import get_astrbot_data_path

            astrbot_data = Path(get_astrbot_data_path())
            if astrbot_data not in directories:
                directories.append(astrbot_data)
        except Exception:  # pragma: no cover - 仅在非 AstrBot 环境触发
            pass
        return directories

    @property
    def _enabled(self) -> bool:
        return self._conf_bool("enabled", True)

    @property
    def _target_channel(self) -> str:
        return self._conf_str("target_channel_id")

    @property
    def _cooldown(self) -> int:
        return self._conf_int("cooldown", 3, 0, 60)

    @property
    def _volume(self) -> int:
        return self._conf_int("volume", 100, 0, 100)

    @property
    def _start_delay(self) -> int:
        return self._conf_int("start_delay", DEFAULT_START_DELAY, 0, 10)

    @property
    def _leave_delay(self) -> int:
        return self._conf_int("leave_delay", DEFAULT_LEAVE_DELAY, 0, 10)

    @property
    def _ffmpeg_path(self) -> str:
        return self._conf_str("ffmpeg_path") or "ffmpeg"

    def _ffmpeg_executable(self) -> str:
        path = self._ffmpeg_path
        if os.path.isabs(path):
            return path
        return shutil.which(path) or path

    def _resolve_data_dir(self) -> Path:
        try:
            return StarTools.get_data_dir(PLUGIN_NAME)
        except Exception as exc:  # pragma: no cover - 仅在异常环境触发
            fallback = Path(os.getcwd()) / "data" / "plugin_data" / PLUGIN_NAME
            fallback.mkdir(parents=True, exist_ok=True)
            logger.warning(
                f"[KOOK语音欢迎] 无法通过 StarTools 获取数据目录（{exc}），"
                f"回退到 {fallback}"
            )
            return fallback

    def _describe_source_sync(self) -> str:
        """在不发起网络请求的前提下描述当前音频来源，用于启动日志。"""
        mode = self._audio_source
        resolved = self._source_path_sync(mode)
        if resolved is not None:
            return str(resolved)
        if mode == SOURCE_URL:
            url = self._conf_str("welcome_audio_url")
            if not url:
                return "来源=链接，但未填写链接"
            cached = self._url_cache_path(url)
            return (
                f"{url}（缓存：{cached}）" if cached.is_file() else f"{url}（尚未下载）"
            )
        if mode == SOURCE_UPLOAD:
            return "来源=上传，但还没有可用的上传文件"
        if mode == SOURCE_PATH:
            return f"来源=本地路径，文件不存在：{self._conf_str('welcome_audio')}"
        return "未找到 welcome.* 音频"

    def _log_startup(self) -> None:
        channel = self._target_channel or "自动跟随事件频道"
        audio_text = self._describe_source_sync()
        summary = (
            f"已加载 {PLUGIN_VERSION}：启用={self._enabled}，监听频道={channel}，"
            f"来源={self._audio_source}，音频={audio_text}，"
            f"音量={self._volume}，冷却={self._cooldown}s，"
            f"启动延迟={self._start_delay}s，尾部静音={self._leave_delay}s"
        )
        logger.info(f"[KOOK语音欢迎] {summary}")
        self._trace(f"插件加载：{summary}")
        self._trace(f"数据目录：{self._data_dir}")
        self._trace(f"诊断日志：{self._log_file}")

    def _trace(self, message: str) -> None:
        """把诊断信息追加到插件日志文件，便于用户回传排查问题。

        日志只记录执行流程、频道 ID 与用户 ID，不会写入 Bot Token。
        """
        if not self._conf_bool("diagnostic_log", True):
            return
        try:
            self._log_file.parent.mkdir(parents=True, exist_ok=True)
            if (
                self._log_file.exists()
                and self._log_file.stat().st_size > LOG_MAX_BYTES
            ):
                backup = self._log_file.with_name(f"{self._log_file.name}.1")
                with contextlib.suppress(OSError):
                    if backup.exists():
                        backup.unlink()
                    self._log_file.rename(backup)
            stamp = time.strftime("%Y-%m-%d %H:%M:%S")
            with self._log_file.open("a", encoding="utf-8") as handle:
                handle.write(f"[{stamp}] {message}\n")
        except OSError:
            # 日志写入失败不应影响插件主流程
            pass

    def _save_config(self, action: str) -> bool:
        """保存配置并统一处理失败，返回是否真的写入磁盘。

        早期版本用 ``suppress(Exception)`` 吞掉保存异常，指令却照样回复
        「已生效」。配置目录只读、磁盘写满时用户完全无从判断。
        """
        try:
            self.config.save_config()
        except Exception as exc:
            logger.warning(f"[KOOK语音欢迎] 保存配置失败（{action}）：{exc}")
            self._trace(f"保存配置失败（{action}）：{exc!r}")
            return False
        return True

    async def _log_ffmpeg_state(self) -> None:
        ok, detail = await self._check_ffmpeg()
        if ok:
            logger.info(f"[KOOK语音欢迎] FFmpeg 检查通过：{detail}")
        else:
            logger.error(f"[KOOK语音欢迎] FFmpeg 不可用：{detail}")

    # ------------------------------------------------------------------ #
    # KOOK 适配器挂接
    # ------------------------------------------------------------------ #

    def _iter_kook_adapters(self) -> list[Any]:
        try:
            instances = self.context.platform_manager.get_insts()
        except Exception:
            return []
        adapters: list[Any] = []
        for instance in instances:
            try:
                if instance.meta().name == "kook":
                    adapters.append(instance)
            except Exception:
                continue
        return adapters

    async def _attach_loop(self) -> None:
        """兜底轮询：适配器可能比插件晚加载，或运行中被重载。"""
        while True:
            try:
                self._attach_all()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("[KOOK语音欢迎] 挂接 KOOK 适配器失败")
            await asyncio.sleep(5)

    def _attach_all(self) -> None:
        for adapter in self._iter_kook_adapters():
            self._attach_adapter(adapter)

    def _attach_adapter(self, adapter: Any) -> None:
        try:
            platform_id = adapter.meta().id or "kook"
        except Exception:
            platform_id = "kook"

        client = getattr(adapter, "client", None)
        if client is None:
            logger.debug("[KOOK语音欢迎] KOOK 适配器尚未创建客户端，稍后重试")
            return

        hook = self._hooks.get(platform_id)
        if hook is not None:
            same_client = hook.client is client
            still_ours = getattr(client, "event_callback", None) is hook.wrapper
            if same_client and still_ours:
                return
            # 适配器换了 client，或回调被第三方改写：先把旧实例还原干净再重挂。
            # 直接覆盖 _hooks[platform_id] 会让旧 client 上永远留着这层包装，
            # 同一次事件被处理两次，进而重复播放。
            if not same_client:
                self._trace(f"适配器 {platform_id} 已更换客户端，先还原旧回调")
            self._detach(platform_id)

        callback = getattr(client, "event_callback", None)
        if callback is None:
            logger.warning(
                "[KOOK语音欢迎] 当前 KOOK 适配器没有 event_callback，"
                "无法接收语音频道事件，请升级 AstrBot"
            )
            self._trace(f"挂接失败：适配器 {platform_id} 没有 event_callback")
            return

        original = callback
        guard = 0
        while getattr(original, _WRAPPER_FLAG, False) and guard < 5:
            original = getattr(original, _WRAPPER_ORIGINAL, original)
            guard += 1

        async def wrapper(
            data: Any, _original: Any = original, _adapter: Any = adapter
        ) -> Any:
            try:
                await self._on_kook_event(_adapter, data)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("[KOOK语音欢迎] 处理语音频道事件时出现异常")
                self._trace("处理语音频道事件时出现异常，详见 AstrBot 日志")
            return await _original(data)

        setattr(wrapper, _WRAPPER_FLAG, True)
        setattr(wrapper, _WRAPPER_ORIGINAL, original)
        client.event_callback = wrapper
        self._hooks[platform_id] = _Hook(
            client=client, original=original, wrapper=wrapper
        )
        logger.info(f"[KOOK语音欢迎] 已挂接 KOOK 适配器 {platform_id} 的系统事件")
        self._trace(f"已挂接 KOOK 适配器 {platform_id} 的系统事件")

    def _detach(self, platform_id: str) -> None:
        hook = self._hooks.pop(platform_id, None)
        if hook is None:
            return
        if getattr(hook.client, "event_callback", None) is hook.wrapper:
            hook.client.event_callback = hook.original
            logger.info(f"[KOOK语音欢迎] 已还原 KOOK 适配器 {platform_id} 的回调")
            self._trace(f"已还原 KOOK 适配器 {platform_id} 的回调")

    @filter.on_platform_loaded()
    async def _on_platform_loaded(self) -> None:
        """平台适配器加载完成后立即尝试挂接，避免等待轮询。"""
        self._attach_all()

    # ------------------------------------------------------------------ #
    # 事件处理与播放队列
    # ------------------------------------------------------------------ #

    async def _on_kook_event(self, adapter: Any, data: Any) -> None:
        # 退出事件先处理：即使配置被中途关闭，也要能停止正在进行的推流
        exited = extract_exited_channel_event(data)
        if exited is not None:
            user_id, channel_id, _guild_id = exited
            bot_id = self._resolve_bot_id()
            if bot_id and user_id == bot_id:
                self._on_bot_left_voice(channel_id)
            return

        if not self._enabled:
            return

        parsed = extract_joined_channel_event(data)
        if parsed is None:
            return
        user_id, channel_id, guild_id = parsed
        self._trace(
            f"收到语音加入事件：user={user_id} channel={channel_id} guild={guild_id}"
        )

        bot_id = self._resolve_bot_id()
        if bot_id and user_id == bot_id:
            # 机器人自己加入频道（本插件触发的 join）同样会产生该事件
            self._trace("忽略：事件来自机器人自己")
            return

        target = self._target_channel
        if target and channel_id != target:
            logger.debug(f"[KOOK语音欢迎] 忽略非目标频道 {channel_id} 的加入事件")
            self._trace(f"忽略：频道 {channel_id} 不是目标频道 {target}")
            return

        if self._playing or not self._queue.empty():
            logger.debug("[KOOK语音欢迎] 正在播放欢迎音频，忽略新的加入事件")
            self._trace("忽略：正在播放中")
            return

        if time.monotonic() - self._last_finished < self._cooldown:
            logger.debug("[KOOK语音欢迎] 仍在冷却时间内，忽略本次加入事件")
            self._trace("忽略：仍在冷却时间内")
            return

        self._enqueue(
            _PlayRequest(channel_id=channel_id, user_id=user_id, guild_id=guild_id)
        )
        logger.info(
            f"[KOOK语音欢迎] 用户 {user_id} 加入语音频道 {channel_id}，准备播放欢迎音频"
        )
        self._trace(f"已加入播放队列：channel={channel_id} user={user_id}")

    def _on_bot_left_voice(self, channel_id: str) -> None:
        """机器人自己被移出语音频道：立刻停止推流。

        KOOK 在机器人被踢出频道、频道被删除或权限被收回时会下发
        ``exited_channel``。此时继续推流没有意义，而且会一直占用「播放中」
        状态，直到整段音频放完、新事件全部被忽略。
        """
        current = self._voice_channel
        if not current:
            self._trace(f"忽略机器人退出事件：当前不在语音频道（channel={channel_id}）")
            return
        if channel_id and channel_id != current:
            self._trace(
                f"忽略机器人退出事件：channel={channel_id} 不是正在使用的 {current}"
            )
            return

        self._stream_aborted = True
        logger.warning(f"[KOOK语音欢迎] 机器人被移出语音频道 {current}，中止本次推流")
        self._trace(f"机器人被移出语音频道 {current}，中止本次推流")
        proc = self._ffmpeg_proc
        if proc is not None and proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()

    def _track_ffmpeg_process(self, process: asyncio.subprocess.Process) -> None:
        """记录当前推流进程；若已收到中止事件，创建后立刻结束它。"""
        self._ffmpeg_proc = process
        if self._stream_aborted and process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                process.kill()

    def _enqueue(self, request: _PlayRequest) -> None:
        if self._queue.full():
            with contextlib.suppress(asyncio.QueueEmpty):
                self._queue.get_nowait()
        with contextlib.suppress(asyncio.QueueFull):
            self._queue.put_nowait(request)

    async def _worker_loop(self) -> None:
        while True:
            request = await self._queue.get()
            self._playing = True
            try:
                await self._play_welcome(request)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.exception("[KOOK语音欢迎] 播放欢迎音频失败")
                self._trace(f"播放异常：{exc!r}")
            finally:
                self._playing = False
                self._last_finished = time.monotonic()
                self._queue.task_done()

    async def _play_welcome(self, request: _PlayRequest) -> None:
        channel_id = request.channel_id
        self._trace(f"开始播放流程：channel={channel_id} reason={request.reason}")

        if (
            time.monotonic() - self._last_finished < self._cooldown
            and request.reason == "event"
        ):
            logger.debug("[KOOK语音欢迎] 冷却未结束，跳过本次播放")
            self._trace("跳过：冷却未结束")
            return

        audio_path = await self.resolve_audio_path()
        if audio_path is None:
            logger.warning(
                "[KOOK语音欢迎] 未找到可用的欢迎音频：请在管理面板选择音频来源"
                "（上传 / 链接 / 本地路径），或使用 /语音欢迎上传 上传"
            )
            self._trace("失败：未找到欢迎音频")
            return
        if not audio_path.is_file():
            logger.error(f"[KOOK语音欢迎] 欢迎音频不存在：{audio_path}")
            self._trace(f"失败：音频文件不存在 {audio_path}")
            return

        token = self._resolve_token()
        if not token:
            logger.error(
                "[KOOK语音欢迎] 未获取到 KOOK Bot Token：请确认 KOOK 适配器已启用，"
                "或在插件配置中填写 bot_token"
            )
            self._trace("失败：未获取到 Bot Token")
            return

        ffmpeg_ok, ffmpeg_detail = await self._check_ffmpeg()
        if not ffmpeg_ok:
            logger.error(f"[KOOK语音欢迎] FFmpeg 不可用，无法推流：{ffmpeg_detail}")
            self._trace(f"失败：FFmpeg 不可用（{ffmpeg_detail}）")
            return

        await self._ensure_bot_id(token)

        # 先把「播放结束后停留」的静音拼进音频，整段播放只保留一条 RTP 流
        stream_path = await self._prepare_stream_audio(audio_path)

        # 先校验音频再加入频道：坏文件不应该先占住语音频道、再失败退出
        duration = await probe_audio_duration(self._ffmpeg_executable(), stream_path)
        if duration is None and ffprobe_available(self._ffmpeg_executable()):
            logger.error(
                f"[KOOK语音欢迎] 无法解析音频（可能已损坏或格式不支持）：{audio_path}"
            )
            self._trace(f"失败：音频无法解析 {audio_path}")
            return

        self._stream_aborted = False
        info = await self._join_voice(token, channel_id)
        self._voice_channel = channel_id
        logger.info(
            f"[KOOK语音欢迎] 已加入语音频道 {channel_id}"
            f"（{info.ip}:{info.port}，码率 {info.bitrate}，"
            f"ssrc={info.audio_ssrc}，pt={info.audio_pt}，mux={info.rtcp_mux}）"
        )
        self._trace(
            f"加入语音频道成功：channel={channel_id} media={info.ip}:{info.port} "
            f"bitrate={info.bitrate} ssrc={info.audio_ssrc} pt={info.audio_pt} "
            f"mux={info.rtcp_mux} rtcp_port={info.rtcp_port or '-'}"
        )

        # 保活从加入频道就开始计时，覆盖「启动延迟 + 推流」整段
        keepalive = asyncio.create_task(
            self._keepalive_loop(token, channel_id),
            name=f"{PLUGIN_NAME}-keepalive",
        )
        try:
            start_delay = self._start_delay
            if start_delay > 0:
                logger.info(
                    f"[KOOK语音欢迎] 等待 {start_delay} 秒后开始播放，"
                    "给听众客户端留出建立语音接收的时间"
                )
                self._trace(f"启动延迟：{start_delay}s")
                await asyncio.sleep(start_delay)

            if self._stream_aborted:
                self._trace("启动前收到机器人退出事件，取消本次播放")
                return

            await self._stream_audio(
                stream_path,
                info,
                display_name=audio_path.name,
                duration=duration,
            )
        finally:
            keepalive.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await keepalive
            # 卸载 / 重载会取消 worker，用 shield 保证离开请求一定发得出去
            with contextlib.suppress(Exception):
                await asyncio.shield(self._leave_voice(token, channel_id))
            self._stream_aborted = False

    async def _stream_audio(
        self,
        audio_path: Path,
        info: VoiceJoinInfo,
        display_name: str = "",
        duration: float | None = None,
    ) -> None:
        ffmpeg = self._ffmpeg_executable()
        args = build_ffmpeg_args(
            ffmpeg=ffmpeg,
            audio_path=audio_path,
            info=info,
            volume=self._volume,
        )

        if duration is None:
            duration = await probe_audio_duration(ffmpeg, audio_path)
        timeout = (
            duration + PLAYBACK_TIMEOUT_MARGIN
            if duration
            else float(DEFAULT_PLAYBACK_TIMEOUT)
        )

        duration_text = f"{duration:.1f} 秒" if duration else "未知时长"
        label = display_name or audio_path.name
        logger.info(f"[KOOK语音欢迎] 开始播放 {label}（推流时长约 {duration_text}）")
        self._trace(f"开始推流：{' '.join(args)}")

        started = time.monotonic()
        self._ffmpeg_proc = None
        try:
            code, output, timed_out = await run_capture(
                args,
                timeout,
                on_start=self._track_ffmpeg_process,
            )
        finally:
            self._ffmpeg_proc = None
        elapsed = time.monotonic() - started

        if self._stream_aborted:
            logger.info("[KOOK语音欢迎] 机器人已离开语音频道，推流被中止")
            self._trace(f"推流中止：机器人不在频道内（用时 {elapsed:.1f}s）")
        elif timed_out:
            logger.warning(
                f"[KOOK语音欢迎] 推流超时（{timeout:.0f}s）已被终止，"
                "请检查音频文件或网络"
            )
            self._trace(f"推流超时：{timeout:.0f}s")
        elif code != 0:
            detail = output.strip()[:500] or "无错误输出"
            logger.error(f"[KOOK语音欢迎] FFmpeg 推流失败（退出码 {code}）：{detail}")
            self._trace(f"推流失败：exit={code} {detail}")
        else:
            logger.info(f"[KOOK语音欢迎] 欢迎音频播放完成（用时 {elapsed:.1f} 秒）")
            self._trace(f"推流完成：用时 {elapsed:.2f}s")

    async def _prepare_stream_audio(self, audio_path: Path) -> Path:
        """把「播放结束后停留」的静音拼进音频，返回真正用于推流的文件。

        早期版本在音频播完后另起一个 FFmpeg 进程推静音，但那是一条新的 RTP
        会话：SSRC 序列号重新开始、UDP 源端口也变了，KOOK 会把它当成新流，
        尾部静音基本收不到。这里改为预先合成「音频 + 静音」的单个文件，整段
        播放始终只有一条 RTP 流，时序仍由 ``-re`` 按真实时间推进。

        合成结果按（路径、大小、修改时间、静音长度）缓存，重复播放不会再次
        转码；合成失败或音频过长时退回原始文件。
        """
        tail = self._leave_delay
        if tail <= 0:
            return audio_path

        try:
            stat = audio_path.stat()
        except OSError as exc:
            logger.debug(f"[KOOK语音欢迎] 读取音频信息失败，跳过尾部静音：{exc}")
            return audio_path

        duration = await probe_audio_duration(self._ffmpeg_executable(), audio_path)
        if duration is not None and duration > MAX_PAD_AUDIO_SECONDS:
            self._trace(
                f"音频时长 {duration:.0f}s 超过 {MAX_PAD_AUDIO_SECONDS}s，"
                "跳过尾部静音"
            )
            return audio_path

        key = f"{audio_path.resolve()}|{stat.st_size}|{stat.st_mtime_ns}|{tail}"
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]
        target = self._data_dir / STREAM_CACHE_DIR_NAME / f"tail_{digest}.wav"
        try:
            if target.is_file() and target.stat().st_size > 44:
                return target
        except OSError:
            pass

        args = [
            self._ffmpeg_executable(),
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostdin",
            "-y",
            "-i",
            str(audio_path),
            "-f",
            "lavfi",
            "-t",
            f"{tail:.2f}",
            "-i",
            "anullsrc=r=48000:cl=stereo",
            "-filter_complex",
            "[0:a]aformat=sample_fmts=fltp:sample_rates=48000:"
            "channel_layouts=stereo[a0];"
            "[1:a]aformat=sample_fmts=fltp:sample_rates=48000:"
            "channel_layouts=stereo[a1];"
            "[a0][a1]concat=n=2:v=0:a=1[out]",
            "-map",
            "[out]",
            "-c:a",
            "pcm_s16le",
            "-ar",
            "48000",
            "-ac",
            "2",
            "-f",
            "wav",
        ]

        try:
            target.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            logger.warning(f"[KOOK语音欢迎] 无法创建缓存目录 {target.parent}：{exc}")
            return audio_path

        temp = target.with_name(target.name + ".part")
        self._trace(f"合成尾部静音：{tail}s -> {target}")
        try:
            code, output, timed_out = await run_capture([*args, str(temp)], timeout=30)
        except Exception as exc:
            logger.warning(f"[KOOK语音欢迎] 合成尾部静音失败：{exc}")
            self._trace(f"合成尾部静音异常：{exc!r}")
            with contextlib.suppress(OSError):
                temp.unlink()
            return audio_path

        if timed_out or code != 0:
            detail = output.strip()[:300] or "无输出"
            logger.warning(
                f"[KOOK语音欢迎] 合成尾部静音失败（exit={code}），"
                f"本次不推尾部静音：{detail}"
            )
            self._trace(f"合成尾部静音失败：exit={code} {detail}")
            with contextlib.suppress(OSError):
                temp.unlink()
            return audio_path

        try:
            os.replace(temp, target)
        except OSError as exc:
            logger.warning(f"[KOOK语音欢迎] 保存尾部静音缓存失败：{exc}")
            with contextlib.suppress(OSError):
                temp.unlink()
            return audio_path

        self._trace(f"尾部静音已合成：{target}")
        return target

    async def _keepalive_loop(self, token: str, channel_id: str) -> None:
        while True:
            await asyncio.sleep(VOICE_KEEP_ALIVE_INTERVAL)
            try:
                await self._api.keep_alive(token, channel_id)
                logger.debug("[KOOK语音欢迎] 已发送语音保活")
            except Exception as exc:
                logger.debug(f"[KOOK语音欢迎] 语音保活失败：{exc}")

    async def _join_voice(self, token: str, channel_id: str) -> VoiceJoinInfo:
        password = self._conf_str("voice_password")
        try:
            return await self._api.join_voice(token, channel_id, password=password)
        except KookApiError as exc:
            logger.warning(
                f"[KOOK语音欢迎] 加入语音频道失败：{exc}；尝试释放残留连接后重试"
            )
            self._trace(f"加入语音频道失败：{exc}；准备释放残留连接后重试")

        await self._release_stale_voice(token, channel_id)
        await asyncio.sleep(STALE_VOICE_RELEASE_WAIT)
        return await self._api.join_voice(token, channel_id, password=password)

    async def _release_stale_voice(self, token: str, target_channel: str = "") -> None:
        """释放残留的语音连接，最多一条，并优先选择目标频道。

        ``/voice/list`` 返回的是本 Bot 账号当前加入的频道。机器人同一时间只能
        待在一个频道里，正常只会有 0～1 条记录；出现多条说明是历史残留。
        早期版本会把列表里的频道**全部** leave，一旦同一个 Bot Token 被多个
        实例共用，就会把别人的连接也踢掉。这里只释放一条，并把频道 ID 写进
        日志，便于排查。
        """
        try:
            items = await self._api.voice_list(token)
        except Exception as exc:
            logger.debug(f"[KOOK语音欢迎] 获取语音频道列表失败：{exc}")
            return

        candidates: list[str] = []
        for item in items:
            channel_id = to_text(item.get("id"))
            if channel_id and channel_id not in candidates:
                candidates.append(channel_id)
        if not candidates:
            self._trace("没有需要释放的残留语音连接")
            return

        channel_id = target_channel if target_channel in candidates else candidates[0]
        if target_channel and channel_id != target_channel:
            logger.warning(
                f"[KOOK语音欢迎] 残留语音连接在 {channel_id}（不是目标频道 "
                f"{target_channel}），只释放这一条后重试"
            )
            self._trace(f"释放非目标频道的残留连接：{channel_id}")
        if len(candidates) > 1:
            self._trace(f"语音连接列表有多条记录，只释放 {channel_id}：{candidates}")

        with contextlib.suppress(Exception):
            await self._api.leave_voice(token, channel_id)
            logger.info(f"[KOOK语音欢迎] 已释放残留语音连接：{channel_id}")
            self._trace(f"已释放残留语音连接：{channel_id}")

    async def _leave_voice(self, token: str, channel_id: str) -> bool:
        """离开语音频道，返回是否成功（失败时不抛异常）。"""
        ok = False
        try:
            await self._api.leave_voice(token, channel_id)
            ok = True
            logger.info(f"[KOOK语音欢迎] 已离开语音频道 {channel_id}")
            self._trace(f"已离开语音频道：{channel_id}")
        except Exception as exc:
            logger.warning(f"[KOOK语音欢迎] 离开语音频道失败：{exc}")
            self._trace(f"离开语音频道失败：{exc!r}")
        finally:
            if self._voice_channel == channel_id:
                self._voice_channel = ""
        return ok

    # ------------------------------------------------------------------ #
    # Token / 音频 / FFmpeg 资源
    # ------------------------------------------------------------------ #

    def _resolve_token(self) -> str:
        token = self._conf_str("bot_token")
        if token:
            return token
        for adapter in self._iter_kook_adapters():
            config = getattr(adapter, "kook_config", None)
            candidate = getattr(config, "token", "") if config is not None else ""
            if candidate:
                return str(candidate).strip()
        return ""

    def _resolve_bot_id(self) -> str:
        if self._bot_id:
            return self._bot_id
        for adapter in self._iter_kook_adapters():
            client = getattr(adapter, "client", None)
            candidate = getattr(client, "bot_id", "") if client is not None else ""
            if candidate:
                self._bot_id = str(candidate).strip()
                return self._bot_id
        return ""

    async def _ensure_bot_id(self, token: str) -> None:
        if self._bot_id:
            return
        try:
            me = await self._api.get_me(token)
        except Exception as exc:
            logger.debug(f"[KOOK语音欢迎] 获取机器人信息失败：{exc}")
            return
        self._bot_id = to_text(me.get("id"))

    # ------------------------------------------------------------------ #
    # 欢迎音频来源
    # ------------------------------------------------------------------ #

    def _resolve_configured_path(self, raw: str) -> Path | None:
        """把配置里的路径字符串解析成真实文件。

        支持三种写法：绝对路径、相对 ``data`` / 插件目录的路径（WebUI 上传后
        保存的就是这类路径）、以及只写文件名（在候选目录中查找）。
        """
        if not raw:
            return None
        candidate_text = raw.strip().strip('"').strip("'")
        if not candidate_text:
            return None
        if candidate_text.startswith(("http://", "https://")):
            return None

        path = Path(os.path.expanduser(candidate_text))
        if path.is_absolute():
            return path if self._is_audio_file(path) else None

        for base in self._candidate_dirs():
            candidate = base / path
            if self._is_audio_file(candidate):
                return candidate

        # 退化为按文件名搜索（WebUI 可能保存在未知层级的子目录中）
        return self._find_audio_by_name(path.name)

    @staticmethod
    def _is_audio_file(path: Path) -> bool:
        """只接受音频扩展名的真实文件，避免把任意文件推流到语音频道。"""
        try:
            if not path.is_file():
                return False
            return path.suffix.lower() in AUDIO_EXTS
        except OSError:
            return False

    def _find_audio_by_name(self, name: str) -> Path | None:
        """在候选目录中按文件名有限深度搜索音频文件。"""
        if not name or f".{name.rsplit('.', 1)[-1]}".lower() not in AUDIO_EXTS:
            return None
        searched: set[str] = set()
        for base in self._candidate_dirs():
            key = str(base)
            if key in searched or not base.is_dir():
                continue
            searched.add(key)
            # 深度限制为 3 层，避免在 data 目录上做无界遍历
            for pattern in (name, f"*/{name}", f"*/*/{name}", f"*/*/*/{name}"):
                try:
                    for candidate in base.glob(pattern):
                        if self._is_audio_file(candidate):
                            return candidate
                except OSError:
                    continue
        return None

    def _auto_audio_path(self) -> Path | None:
        """在数据目录 / 插件目录中查找 welcome.* 与聊天上传的音频。"""
        directories = [
            self._data_dir,
            self._chat_audio_dir,
            self._plugin_dir,
        ]
        for directory in directories:
            if not directory.is_dir():
                continue
            for suffix in sorted(AUDIO_EXTS):
                candidate = directory / f"welcome{suffix}"
                if candidate.is_file():
                    return candidate
            # 没有 welcome.* 时，取目录里最新的音频文件
            try:
                audios = [
                    item
                    for item in directory.iterdir()
                    if item.is_file() and item.suffix.lower() in AUDIO_EXTS
                ]
            except OSError:
                continue
            if audios:
                return max(audios, key=lambda item: item.stat().st_mtime)
        return None

    def _source_path_sync(self, mode: str) -> Path | None:
        """不需要网络请求时的来源解析（上传 / 本地路径 / 自动）。"""
        if mode == SOURCE_UPLOAD:
            for raw in self._conf_list("welcome_audio_files"):
                resolved = self._resolve_configured_path(raw)
                if resolved is not None and resolved.is_file():
                    return resolved
            return None
        if mode == SOURCE_PATH:
            resolved = self._resolve_configured_path(self._conf_str("welcome_audio"))
            return resolved if resolved and resolved.is_file() else None
        if mode == SOURCE_AUTO:
            return self._auto_audio_path()
        return None

    async def resolve_audio_path(self) -> Path | None:
        """按「欢迎音频来源」解析出可用的音频文件。

        顺序为：所选来源 → 其它已配置来源 → 数据目录自动查找，保证任何一种
        配置方式都能工作，来源切换失败时也不会突然没声音。
        """
        mode = self._audio_source
        resolved = self._source_path_sync(mode)
        if resolved is not None:
            return resolved

        if mode == SOURCE_URL:
            cached = await self._prepare_url_audio(self._conf_str("welcome_audio_url"))
            if cached is not None:
                return cached

        # 回退：其它来源中已经配置好的文件
        for fallback in (SOURCE_UPLOAD, SOURCE_PATH, SOURCE_AUTO):
            if fallback == mode:
                continue
            resolved = self._source_path_sync(fallback)
            if resolved is not None:
                self._trace(f"来源 {mode} 不可用，回退到 {fallback}：{resolved}")
                return resolved
        return None

    def _resolve_audio_readonly(self) -> tuple[Path | None, str]:
        """只读解析当前音频，供状态类指令使用。

        与 :meth:`resolve_audio_path` 的区别是不发起任何网络请求：链接来源
        尚未缓存时只报告 URL，不会为了显示状态去下载（那种情况最长要等几十
        秒，指令看起来就像卡死）。
        """
        mode = self._audio_source
        for candidate in (mode, SOURCE_UPLOAD, SOURCE_PATH, SOURCE_AUTO):
            resolved = self._source_path_sync(candidate)
            if resolved is not None and resolved.is_file():
                return resolved, str(resolved)

        if mode == SOURCE_URL:
            url = self._conf_str("welcome_audio_url")
            if not url:
                return None, "未配置（来源：链接，但未填写链接）"
            cached = self._url_cache_path(url)
            try:
                usable = cached.is_file() and cached.stat().st_size > 0
            except OSError:
                usable = False
            if usable:
                return cached, str(cached)
            return None, f"{url}（尚未下载，首次播放时自动缓存）"

        return None, f"未配置（来源：{SOURCE_LABELS.get(mode, mode)}）"

    def _url_cache_path(self, url: str) -> Path:
        digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]
        suffix = Path(urlparse(url).path).suffix.lower()
        if suffix not in AUDIO_EXTS:
            suffix = ".mp3"
        return self._data_dir / URL_CACHE_DIR_NAME / f"url_{digest}{suffix}"

    async def _prepare_url_audio(self, url: str) -> Path | None:
        """下载链接音频并缓存到本地，失败时返回已有缓存。"""
        if not url.startswith(("http://", "https://")):
            return None

        cache = self._url_cache_path(url)
        if cache.is_file() and cache.stat().st_size > 0:
            return cache

        self._trace(f"开始下载欢迎音频：{url}")
        try:
            content, suffix = await self._api.download(url, MAX_AUDIO_BYTES)
        except Exception as exc:
            logger.error(f"[KOOK语音欢迎] 下载欢迎音频失败：{exc}")
            self._trace(f"下载欢迎音频失败：{exc!r}")
            return cache if cache.is_file() and cache.stat().st_size > 0 else None

        destination = cache.with_suffix(suffix) if suffix else cache
        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(content)
        except OSError as exc:
            logger.error(f"[KOOK语音欢迎] 缓存欢迎音频失败：{exc}")
            self._trace(f"缓存欢迎音频失败：{exc!r}")
            return None

        if destination != cache and cache.is_file():
            with contextlib.suppress(OSError):
                cache.unlink()

        logger.info(f"[KOOK语音欢迎] 欢迎音频已缓存到 {destination}")
        self._trace(f"欢迎音频已缓存：{destination}（{len(content)} 字节）")
        return destination

    async def _check_ffmpeg(self, force: bool = False) -> tuple[bool, str]:
        path = self._ffmpeg_path
        if not force and self._ffmpeg_cache and self._ffmpeg_cache[0] == path:
            return self._ffmpeg_cache[1], self._ffmpeg_cache[2]

        executable = self._ffmpeg_executable()
        if not executable or (
            os.path.isabs(executable) and not os.path.exists(executable)
        ):
            detail = f"未找到 FFmpeg 可执行文件：{path}"
            self._ffmpeg_cache = (path, False, detail)
            return False, detail

        ok = False
        detail = ""
        try:
            code, output, timed_out = await run_capture(
                [executable, "-hide_banner", "-version"],
                timeout=8,
            )
            ok = code == 0 and not timed_out
            first_line = output.strip().splitlines()[0] if output.strip() else ""
            detail = first_line or f"退出码 {code}"
        except (OSError, FileNotFoundError) as exc:
            detail = f"无法执行 FFmpeg：{exc}"

        if ok:
            try:
                code, output, _ = await run_capture(
                    [executable, "-hide_banner", "-encoders"],
                    timeout=8,
                )
                if code == 0 and "libopus" not in output:
                    ok = False
                    detail = "FFmpeg 缺少 libopus 编码器，请安装完整版 FFmpeg"
            except (OSError, FileNotFoundError):
                pass

        self._ffmpeg_cache = (path, ok, detail)
        return ok, detail

    # ------------------------------------------------------------------ #
    # 指令
    # ------------------------------------------------------------------ #

    @filter.command("语音欢迎")
    @filter.platform_adapter_type(filter.PlatformAdapterType.KOOK)
    async def cmd_status(self, event: AstrMessageEvent):
        """查看 KOOK 语音欢迎插件的当前状态"""
        mode = self._audio_source
        # 状态查询只读：链接来源尚未缓存时，不应该为了显示去下载（最长会卡 30 秒）
        audio, audio_text = self._resolve_audio_readonly()
        if audio is not None and audio.is_file():
            duration = await probe_audio_duration(self._ffmpeg_executable(), audio)
            if duration:
                audio_text = f"{audio_text}，约 {duration:.1f} 秒"

        ffmpeg_ok, ffmpeg_detail = await self._check_ffmpeg()
        token_ready = "已配置" if self._resolve_token() else "未获取"
        adapters = len(self._iter_kook_adapters())
        uploaded = self._conf_list("welcome_audio_files")
        url_text = self._conf_str("welcome_audio_url") or "未填写"

        if self._playing:
            state = f"正在播放（频道 {self._voice_channel or '未知'}）"
        elif self._voice_channel:
            state = f"占用语音频道 {self._voice_channel}"
        else:
            state = "空闲"

        lines = [
            "🎧 KOOK 语音欢迎",
            f"插件状态：{'已启用' if self._enabled else '已关闭'}",
            f"监听频道：{self._target_channel or '自动跟随事件频道'}",
            f"音频来源：{SOURCE_LABELS.get(mode, mode)}（{mode}）",
            f"欢迎音频：{audio_text}",
            f"上传文件：{len(uploaded)} 个 / 链接：{url_text}",
            f"播放音量：{self._volume} / 冷却：{self._cooldown} 秒",
            f"启动延迟：{self._start_delay} 秒 / 尾部静音：{self._leave_delay} 秒",
            f"FFmpeg：{'可用' if ffmpeg_ok else '不可用'}"
            + ("" if ffmpeg_ok else f"（{ffmpeg_detail}）"),
            f"Bot Token：{token_ready} / KOOK 适配器：{adapters} 个",
            f"运行状态：{state}",
            f"诊断日志：{self._log_file}",
        ]
        yield event.plain_result("\n".join(lines))

    @filter.command("语音欢迎帮助")
    @filter.platform_adapter_type(filter.PlatformAdapterType.KOOK)
    async def cmd_help(self, event: AstrMessageEvent):
        """查看 KOOK 语音欢迎插件指令列表"""
        prefix = "指令前缀以 AstrBot 全局设置为准，下表以默认的 / 为例"
        lines = [
            "🎧 KOOK 语音欢迎 · 指令帮助",
            f"（{prefix}）",
            "",
            "普通指令",
            "· /语音欢迎 —— 查看插件状态、音频来源与运行信息",
            "· /语音欢迎帮助 —— 查看本帮助",
            "",
            "管理员指令",
            "· /语音欢迎上传 —— 引用或附带 MP3/WAV 音频，上传为欢迎音频",
            "· /语音欢迎链接 <url> —— 使用网络音频链接",
            "· /语音欢迎来源 upload|url|path|auto —— 切换音频来源",
            "· /语音欢迎试听 [频道ID] —— 让机器人加入语音频道试听",
            "· /语音欢迎重载 —— 重新读取配置并检测 FFmpeg",
            "",
            "音频也可以在管理面板的插件配置里上传，或填写链接。",
            "工作方式：成员进入监听范围内的语音频道 → 机器人加入并播放欢迎音频 →"
            " 播放完成后离开。",
        ]
        yield event.plain_result("\n".join(lines))

    @filter.command("语音欢迎上传")
    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.platform_adapter_type(filter.PlatformAdapterType.KOOK)
    async def cmd_upload(self, event: AstrMessageEvent):
        """上传并设置欢迎音频（管理员）"""
        component = self._find_audio_component(event)
        if component is None:
            yield event.plain_result(
                "请引用或附带一个 MP3 / WAV 音频文件后再执行该指令。\n"
                "例如：先发送音频，再引用它并发送 /语音欢迎上传。"
            )
            return

        yield event.plain_result("正在保存音频，请稍候……")
        try:
            destination, duration = await self._save_audio_component(component)
        except Exception as exc:
            logger.exception("[KOOK语音欢迎] 保存欢迎音频失败")
            self._trace(f"上传欢迎音频失败：{exc!r}")
            yield event.plain_result(f"❌ 保存失败：{exc}")
            return

        saved = self._register_uploaded_audio(destination)
        duration_text = f"，时长约 {duration:.1f} 秒" if duration else ""
        source_text = "，音频来源已切换为「上传」" if saved.get("switched") else ""
        persist_text = (
            ""
            if saved.get("persisted", True)
            else "\n⚠️ 配置未能写入磁盘，重启后可能失效，请检查日志。"
        )
        yield event.plain_result(
            f"✅ 欢迎音频已更新：{destination}{duration_text}\n"
            f"已登记到「上传的欢迎音频」列表{source_text}，"
            "下次成员进入语音频道时生效。" + persist_text
        )

    @filter.command("语音欢迎链接")
    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.platform_adapter_type(filter.PlatformAdapterType.KOOK)
    async def cmd_link(self, event: AstrMessageEvent):
        """使用网络链接作为欢迎音频（管理员）"""
        url = self._extract_url(event.message_str)
        if not url:
            yield event.plain_result(
                "请提供音频链接：/语音欢迎链接 https://example.com/welcome.mp3"
            )
            return

        yield event.plain_result("正在检查链接，请稍候……")
        cached = await self._prepare_url_audio(url)
        if cached is None:
            yield event.plain_result(
                f"❌ 无法下载该链接的音频，请确认是可直接访问的音频直链：{url}"
            )
            return

        self.config["welcome_audio_url"] = url
        self.config["audio_source"] = SOURCE_URL
        persisted = self._save_config("设置音频链接")

        duration = await probe_audio_duration(self._ffmpeg_executable(), cached)
        duration_text = f"，时长约 {duration:.1f} 秒" if duration else ""
        persist_text = (
            ""
            if persisted
            else "\n⚠️ 配置未能写入磁盘，重启后可能失效，请检查日志。"
        )
        self._trace(f"管理员设置音频链接：{url}（persisted={persisted}）")
        yield event.plain_result(
            f"✅ 欢迎音频已设为链接来源：{url}{duration_text}\n"
            f"本地缓存：{cached}\n音频来源已切换为「链接」。" + persist_text
        )

    @filter.command("语音欢迎来源")
    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.platform_adapter_type(filter.PlatformAdapterType.KOOK)
    async def cmd_source(self, event: AstrMessageEvent):
        """切换欢迎音频来源（管理员）"""
        requested = self._extract_source(event.message_str)
        if requested not in SOURCE_LABELS:
            options = " / ".join(
                f"{key}（{label}）" for key, label in SOURCE_LABELS.items()
            )
            current = SOURCE_LABELS.get(self._audio_source, self._audio_source)
            yield event.plain_result(
                f"当前音频来源：{current}\n用法：/语音欢迎来源 <{options}>",
            )
            return

        # 先落盘再回复：早期版本只回复「已切换」，配置其实没有写回去，
        # 用户切了来源却仍是旧音频，只有重启后才生效。
        self.config["audio_source"] = requested
        persisted = self._save_config("切换音频来源")

        suffix = (
            ""
            if persisted
            else "\n⚠️ 配置未能写入磁盘，重启后可能恢复为原来源，请检查日志。"
        )

        audio = self._source_path_sync(requested)
        if requested == SOURCE_URL:
            url = self._conf_str("welcome_audio_url")
            if not url:
                message = (
                    "⚠️ 已切换到「链接」来源，但尚未配置欢迎音频链接，"
                    "请使用 /语音欢迎链接 <url> 设置。"
                )
            else:
                cached = self._url_cache_path(url)
                cache_text = (
                    f"\n本地缓存：{cached}"
                    if cached.is_file()
                    else "\n首次播放时会自动下载并缓存。"
                )
                message = f"✅ 音频来源已切换为「链接」：{url}{cache_text}"
        elif audio is None:
            message = (
                f"⚠️ 已切换到「{SOURCE_LABELS[requested]}」，"
                "但当前该来源没有可用文件，播放时会自动回退到其它可用来源。"
            )
        else:
            message = f"✅ 音频来源已切换为「{SOURCE_LABELS[requested]}」：{audio}"

        self._trace(f"管理员切换音频来源：{requested}（persisted={persisted}）")
        yield event.plain_result(message + suffix)

    @filter.command("语音欢迎试听")
    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.platform_adapter_type(filter.PlatformAdapterType.KOOK)
    async def cmd_preview(self, event: AstrMessageEvent):
        """在指定语音频道试听欢迎音频（管理员）"""
        channel_id = self._extract_channel_id(event.message_str) or self._target_channel
        if not channel_id:
            yield event.plain_result(
                "请指定语音频道 ID：/语音欢迎试听 <频道ID>\n"
                "或在插件配置中填写「目标语音频道 ID」。"
            )
            return

        token = self._resolve_token()
        if not token:
            yield event.plain_result(
                "未获取到 KOOK Bot Token，请确认 KOOK 适配器已启用，"
                "或在插件配置中填写 bot_token。"
            )
            return

        audio = await self.resolve_audio_path()
        if audio is None or not audio.is_file():
            yield event.plain_result(
                "尚未设置可用的欢迎音频，可在管理面板上传/填写链接，"
                "或使用 /语音欢迎上传、/语音欢迎链接。"
            )
            return

        try:
            channel = await self._api.get_channel(token, channel_id)
        except KookApiError as exc:
            yield event.plain_result(
                f"❌ 无法访问频道 {channel_id}：{exc.kook_message}"
            )
            return

        if to_int(channel.get("type"), 0) != 2:
            yield event.plain_result(f"❌ 频道 {channel_id} 不是语音频道。")
            return

        self._enqueue(
            _PlayRequest(
                channel_id=channel_id,
                user_id=to_text(event.get_sender_id()),
                reason="preview",
            )
        )
        self._trace(f"管理员试听：channel={channel_id}")
        yield event.plain_result(
            f"🎧 即将在语音频道 {channel_id} 播放试听，机器人会在播放结束后自动离开。"
        )

    @filter.command("语音欢迎重载")
    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.platform_adapter_type(filter.PlatformAdapterType.KOOK)
    async def cmd_reload(self, event: AstrMessageEvent):
        """重新读取配置并检测 FFmpeg（管理员）"""
        self._ffmpeg_cache = None
        ok, detail = await self._check_ffmpeg(force=True)
        mode = self._audio_source
        # 重载同样只读：不能为了显示状态去触发一次网络下载
        _audio, audio_text = self._resolve_audio_readonly()
        self._trace(f"管理员重载配置：ffmpeg_ok={ok} source={mode} audio={audio_text}")

        lines = [
            "✅ 已重新读取插件配置",
            f"启用：{'是' if self._enabled else '否'}",
            f"监听频道：{self._target_channel or '自动跟随事件频道'}",
            f"音频来源：{SOURCE_LABELS.get(mode, mode)}",
            f"欢迎音频：{audio_text}",
            f"FFmpeg：{'可用' if ok else '不可用'}" + ("" if ok else f"（{detail}）"),
        ]
        yield event.plain_result("\n".join(lines))

    # ------------------------------------------------------------------ #
    # 指令辅助
    # ------------------------------------------------------------------ #

    @staticmethod
    def _extract_channel_id(message_str: str) -> str:
        """从指令文本中取出频道 ID（最后一个纯数字 token）。"""
        candidates = [
            token for token in re.split(r"\s+", message_str or "") if token.isdigit()
        ]
        return candidates[-1] if candidates else ""

    @staticmethod
    def _extract_url(message_str: str) -> str:
        """从指令文本中取出第一个 http(s) 链接。"""
        match = re.search(r"https?://\S+", message_str or "")
        if not match:
            return ""
        return match.group(0).strip().strip('<>"')

    @staticmethod
    def _extract_source(message_str: str) -> str:
        """从指令文本中取出音频来源。"""
        aliases = {
            "upload": SOURCE_UPLOAD,
            "uploaded": SOURCE_UPLOAD,
            "file": SOURCE_UPLOAD,
            "url": SOURCE_URL,
            "link": SOURCE_URL,
            "path": SOURCE_PATH,
            "local": SOURCE_PATH,
            "auto": SOURCE_AUTO,
            "welcome": SOURCE_AUTO,
        }
        tokens = [
            token for token in re.split(r"\s+", (message_str or "").strip()) if token
        ]
        for token in reversed(tokens):
            key = token.strip("<>\"',").lower()
            if key in aliases:
                return aliases[key]
        return ""

    @staticmethod
    def _find_audio_component(event: AstrMessageEvent) -> Any:
        try:
            components = event.get_messages() or []
        except Exception:
            components = []

        for component in components:
            if isinstance(component, Record):
                return component
        for component in components:
            if isinstance(component, File):
                name = str(getattr(component, "name", "") or "").lower()
                if any(name.endswith(ext) for ext in AUDIO_EXTS):
                    return component
        return None

    @staticmethod
    def _component_source(component: Any) -> str:
        if isinstance(component, Record):
            for attribute in ("path", "file", "url"):
                value = getattr(component, attribute, None)
                if isinstance(value, str) and value.strip():
                    return value.strip()
            return ""

        if isinstance(component, File):
            try:
                local = component.file
            except Exception as exc:
                logger.warning(f"[KOOK语音欢迎] 读取文件消息失败：{exc}")
                local = ""
            if isinstance(local, str) and local and Path(local).exists():
                return local
            url = getattr(component, "url", "")
            return url.strip() if isinstance(url, str) else ""
        return ""

    async def _save_audio_component(self, component: Any) -> tuple[Path, float | None]:
        source = self._component_source(component)
        if not source:
            raise ValueError("消息中没有可用的音频文件")
        preferred_name = str(getattr(component, "name", "") or "")
        destination = await self._materialize_audio(source, preferred_name)
        duration = await probe_audio_duration(self._ffmpeg_executable(), destination)
        return destination, duration

    async def _materialize_audio(self, source: str, preferred_name: str = "") -> Path:
        if source.startswith("file://"):
            parsed = unquote(urlparse(source).path)
            if re.match(r"^/[A-Za-z]:", parsed):
                parsed = parsed[1:]
            source = parsed

        if source.startswith(("http://", "https://")):
            content, suffix = await self._api.download(source, MAX_AUDIO_BYTES)
            name = preferred_name or urlparse(source).path.rsplit("/", 1)[-1]
            return self._write_chat_audio(content, suffix, name)

        path = Path(source)
        if not path.is_file():
            raise FileNotFoundError(f"音频文件不存在：{path}")
        size = path.stat().st_size
        if size <= 0:
            raise ValueError("音频文件为空")
        if size > MAX_AUDIO_BYTES:
            raise ValueError("音频文件超过 25 MB 限制")
        return self._write_chat_audio(
            path.read_bytes(),
            path.suffix.lower() or ".mp3",
            path.name,
        )

    def _write_chat_audio(
        self,
        content: bytes,
        suffix: str,
        preferred_name: str = "",
    ) -> Path:
        """把聊天上传的音频保存到插件数据目录，返回保存路径。"""
        if not suffix.startswith("."):
            suffix = f".{suffix}"
        suffix = suffix.lower()
        if suffix not in AUDIO_EXTS:
            suffix = ".mp3"

        directory = self._chat_audio_dir
        directory.mkdir(parents=True, exist_ok=True)

        name = self._sanitize_audio_name(preferred_name)
        if not name:
            name = f"welcome{suffix}"
        if Path(name).suffix.lower() not in AUDIO_EXTS:
            name = f"{Path(name).stem or 'welcome'}{suffix}"

        destination = directory / name
        if destination.exists():
            # 秒级时间戳在同一秒内的两次上传会撞名，直接覆盖掉前一个文件；
            # 用随机后缀保证每次上传都是独立文件。
            destination = directory / (
                f"{destination.stem}_{uuid4().hex[:8]}{destination.suffix}"
            )

        destination.write_bytes(content)
        return destination

    @staticmethod
    def _sanitize_audio_name(name: str) -> str:
        """去掉路径分隔符等不安全字符，保留可打印的文件名。"""
        if not name:
            return ""
        cleaned = unquote(str(name)).replace("\\", "/").rsplit("/", 1)[-1]
        cleaned = re.sub(r"[\x00-\x1f\x7f<>:\"|?*]", "", cleaned).strip().strip(".")
        if len(cleaned) > 80:
            suffix = Path(cleaned).suffix
            cleaned = cleaned[: 80 - len(suffix)] + suffix
        return cleaned

    def _register_uploaded_audio(self, destination: Path) -> dict[str, Any]:
        """把聊天上传的文件写入「上传的欢迎音频」配置，必要时切换来源。"""
        entries = self._conf_list("welcome_audio_files")
        saved: dict[str, Any] = {"audio_source": self._audio_source, "switched": False}

        relative = self._relative_to_data(destination)
        key = relative or str(destination)

        # 只去掉指向同一个文件的旧条目；无法解析的条目保留，交给用户自行清理，
        # 避免误删 AstrBot 保存在其它目录的上传记录。
        kept: list[str] = []
        for item in entries:
            resolved = self._resolve_configured_path(item)
            if resolved is not None and resolved == destination:
                continue
            kept.append(item)
        self.config["welcome_audio_files"] = [key, *kept][:20]

        current = self._audio_source
        if self._source_path_sync(current) is None and (
            current != SOURCE_URL or not self._conf_str("welcome_audio_url")
        ):
            self.config["audio_source"] = SOURCE_UPLOAD
            saved["audio_source"] = SOURCE_UPLOAD
            saved["switched"] = True

        persisted = self._save_config("登记上传的欢迎音频")
        saved["persisted"] = persisted
        self._trace(
            f"管理员上传欢迎音频：{destination}，"
            f"来源={saved['audio_source']}（persisted={persisted}）"
        )
        return saved

    def _relative_to_data(self, path: Path) -> str:
        """尽量保存为相对路径，便于换机器或换部署目录后仍能解析。"""
        try:
            return path.resolve().relative_to(self._data_dir.resolve()).as_posix()
        except (ValueError, OSError):
            return ""

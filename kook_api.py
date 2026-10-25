"""KOOK 开放平台 HTTP 客户端。

只覆盖语音相关的最小接口集：加入 / 离开语音频道、keep-alive、语音连接列表、
频道信息、机器人信息，以及欢迎音频的下载。

会话管理上有两条硬性规则：

- API 会话带 ``Authorization: Bot <token>``，只能用于 kookapp.cn 域名；
- 音频下载可能指向任意第三方域名，必须使用不带鉴权头的独立会话，
  否则等于把 Bot Token 泄露给对方服务器。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import aiohttp

from .constants import (
    AUDIO_EXTS,
    CONTENT_TYPE_SUFFIX,
    DOWNLOAD_USER_AGENT,
    KOOK_API_BASE,
)
from .utils import to_bool, to_int, to_text


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
        """从接口返回值构造推流信息，字段缺失时给出安全默认值。"""
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
        """获取（并按需创建）带鉴权头的 API 会话，token 变化时自动更新。"""
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
        """调用 KOOK 开放接口，返回 ``data`` 字段；失败时抛出 :class:`KookApiError`。"""
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
        """加入语音频道，返回推流所需的地址与 RTP 参数。"""
        # 显式要求独立 RTCP 端口：rtcp_mux=true 时 FFmpeg 不会发送任何 RTCP，
        # 部分媒体服务器需要 RTCP SR 才会真正转发音频。
        payload: dict[str, Any] = {"channel_id": channel_id, "rtcp_mux": False}
        if password:
            payload["password"] = password
        data = await self.request("POST", "/voice/join", token, payload=payload)
        return VoiceJoinInfo.from_data(data)

    async def leave_voice(self, token: str, channel_id: str) -> None:
        """离开语音频道。"""
        await self.request(
            "POST",
            "/voice/leave",
            token,
            payload={"channel_id": channel_id},
        )

    async def keep_alive(self, token: str, channel_id: str) -> None:
        """语音保活，长播放时必须定期调用，否则连接会被服务端回收。"""
        await self.request(
            "POST",
            "/voice/keep-alive",
            token,
            payload={"channel_id": channel_id},
        )

    async def voice_list(self, token: str) -> list[dict[str, Any]]:
        """查询本 Bot 当前加入的语音频道列表。"""
        data = await self.request("GET", "/voice/list", token)
        items = data.get("items")
        if isinstance(items, list):
            return [item for item in items if isinstance(item, dict)]
        return []

    async def get_channel(self, token: str, channel_id: str) -> dict[str, Any]:
        """查询频道信息（用于校验目标是否为语音频道）。"""
        return await self.request(
            "GET",
            "/channel/view",
            token,
            params={"target_id": channel_id},
        )

    async def get_me(self, token: str) -> dict[str, Any]:
        """查询机器人自身信息（用于拿到 bot_id）。"""
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

        # 后缀优先取 Content-Type，其次取 URL 路径；两者都拿不到时按 mp3 处理，
        # 并拒绝明显是文本 / JSON 的响应，避免把错误页当成音频缓存下来。
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
        """关闭所有 HTTP 会话，插件卸载时调用。"""
        for session in (self._session, self._download_session):
            if session and not session.closed:
                with contextlib.suppress(Exception):
                    await session.close()
        self._session = None
        self._download_session = None

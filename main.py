"""KOOK 语音欢迎插件 —— 插件入口。

当成员进入 KOOK 语音频道时，机器人自动加入该频道、播放欢迎音频，播放结束后
立即离开。

模块结构
--------
AstrBot 按插件主模块（本文件）的模块路径绑定指令与事件 handler，因此所有
带 ``@filter`` 装饰器的方法必须留在本文件；具体实现按职责拆分在子模块中，
通过 Mixin 混入插件类：

- ``constants.py``   全局常量与时序参数
- ``utils.py``       类型转换、子进程执行、ffprobe 封装
- ``kook_api.py``    KOOK 开放平台 HTTP 客户端（语音接口 + 音频下载）
- ``ffmpeg.py``      FFmpeg RTP 推流命令拼装
- ``config.py``      配置读取、目录解析与诊断日志（ConfigMixin）
- ``hooking.py``     KOOK 适配器事件挂接（HookingMixin）
- ``playback.py``    播放队列与推流流程（PlaybackMixin）
- ``resources.py``   Token / 音频来源 / FFmpeg 资源解析（ResourcesMixin）
- ``commands.py``    指令参数解析与上传音频处理（CommandsMixin）

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
import time
from pathlib import Path
from typing import Any

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

from .commands import CommandsMixin
from .config import ConfigMixin
from .constants import (
    PLUGIN_NAME,
    PLUGIN_VERSION,
    SOURCE_LABELS,
    SOURCE_URL,
)
from .hooking import (
    HookingMixin,
    _Hook,
    extract_exited_channel_event,
    extract_joined_channel_event,
)
from .kook_api import KookApi, KookApiError
from .playback import PlaybackMixin, _PlayRequest
from .resources import ResourcesMixin
from .utils import probe_audio_duration, to_int, to_text


@register(
    PLUGIN_NAME,
    "HanZai",
    "成员进入 KOOK 语音频道时自动播放欢迎音频",
    PLUGIN_VERSION,
)
class KookAudioWelcomePlugin(
    CommandsMixin,
    ResourcesMixin,
    PlaybackMixin,
    HookingMixin,
    ConfigMixin,
    Star,
):
    """插件主体：生命周期管理 + 指令 handler，实现分布在各 Mixin 中。"""

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
    # KOOK 事件分发（由 hooking.py 的包装回调调用）
    # ------------------------------------------------------------------ #

    async def _on_kook_event(self, adapter: Any, data: Any) -> None:
        """处理包装回调捕获到的每一条 KOOK 网关事件。"""
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

    @filter.on_platform_loaded()
    async def _on_platform_loaded(self) -> None:
        """平台适配器加载完成后立即尝试挂接，避免等待轮询。"""
        self._attach_all()

    # ------------------------------------------------------------------ #
    # 指令：状态与帮助
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

    # ------------------------------------------------------------------ #
    # 指令：管理员
    # ------------------------------------------------------------------ #

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
            "" if persisted else "\n⚠️ 配置未能写入磁盘，重启后可能失效，请检查日志。"
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

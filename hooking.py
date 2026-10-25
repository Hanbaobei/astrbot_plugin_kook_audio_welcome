"""KOOK 适配器事件挂接（Mixin）。

AstrBot 自带的 KOOK 适配器只把聊天消息转换成 AstrBot 事件，网关收到的系统
消息（``type == 255``）除角色变更外都会被丢弃。本模块负责在不新建第二条
网关连接的前提下，包装 ``KookClient.event_callback``，额外拿到
``joined_channel`` / ``exited_channel`` 等语音频道系统事件。

挂接是可逆的：插件卸载或重载时会还原原始回调，热重载也不会重复播放。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from astrbot.api import logger

from .constants import (
    KOOK_SYSTEM_MESSAGE_TYPE,
    KOOK_VOICE_EXITED,
    KOOK_VOICE_JOINED,
)
from .utils import to_int, to_mapping, to_text

# 用于识别「本插件挂接的回调」，方便热重载时先解开旧包装，避免重复播放
_WRAPPER_FLAG = "__kook_audio_welcome_wrapper__"
_WRAPPER_ORIGINAL = "__kook_audio_welcome_original__"


def extract_channel_event(data: Any, event_type: str) -> tuple[str, str, str] | None:
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


@dataclass(slots=True)
class _Hook:
    """一条适配器回调挂接记录：client、原始回调与本插件的包装回调。"""

    client: Any
    original: Any
    wrapper: Any


class HookingMixin:
    """KOOK 适配器回调的挂接 / 还原 / 事件分发。方法由 main.py 的插件类继承。"""

    # ------------------------------------------------------------------ #
    # 适配器发现
    # ------------------------------------------------------------------ #

    def _iter_kook_adapters(self) -> list[Any]:
        """找出当前已加载的所有 KOOK 平台适配器实例。"""
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

    # ------------------------------------------------------------------ #
    # 挂接与还原
    # ------------------------------------------------------------------ #

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
        """把本插件的事件包装挂到所有 KOOK 适配器上。"""
        for adapter in self._iter_kook_adapters():
            self._attach_adapter(adapter)

    def _attach_adapter(self, adapter: Any) -> None:
        """包装单个适配器的 ``event_callback``。

        同一适配器重复挂接前会先检查：客户端没换、回调仍是本插件的包装，
        则跳过；否则先还原旧包装再重新挂接，保证任何情况下同一次事件只被
        处理一次。
        """
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

        # 如果回调已经层层包装（例如本插件重载后），剥开找到最底层的原始回调，
        # 避免包装嵌套导致事件被处理多次。
        original = callback
        guard = 0
        while getattr(original, _WRAPPER_FLAG, False) and guard < 5:
            original = getattr(original, _WRAPPER_ORIGINAL, original)
            guard += 1

        async def wrapper(
            data: Any, _original: Any = original, _adapter: Any = adapter
        ) -> Any:
            """包装回调：先处理语音事件，再把原始事件交还给适配器。"""
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
        """还原指定适配器的回调；回调已被第三方改写时保持原样。"""
        hook = self._hooks.pop(platform_id, None)
        if hook is None:
            return
        if getattr(hook.client, "event_callback", None) is hook.wrapper:
            hook.client.event_callback = hook.original
            logger.info(f"[KOOK语音欢迎] 已还原 KOOK 适配器 {platform_id} 的回调")
            self._trace(f"已还原 KOOK 适配器 {platform_id} 的回调")

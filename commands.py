"""聊天指令的辅助逻辑（Mixin）。

包含指令参数解析（频道 ID / URL / 来源别名）与聊天音频的落地保存、
登记到配置。真正的指令 handler（带 ``@filter`` 装饰器）在 main.py，
因为 AstrBot 按 handler 所在模块路径绑定插件实例，装饰器方法不能外移。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse
from uuid import uuid4

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent
from astrbot.api.message_components import File, Record

from .constants import (
    AUDIO_EXTS,
    MAX_AUDIO_BYTES,
    SOURCE_AUTO,
    SOURCE_PATH,
    SOURCE_UPLOAD,
    SOURCE_URL,
)
from .utils import probe_audio_duration


class CommandsMixin:
    """指令参数解析与上传音频处理。方法由 main.py 的插件类继承。"""

    # ------------------------------------------------------------------ #
    # 指令参数解析
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
        """从指令文本中取出音频来源（支持中英文别名）。"""
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

    # ------------------------------------------------------------------ #
    # 聊天音频组件处理
    # ------------------------------------------------------------------ #

    @staticmethod
    def _find_audio_component(event: AstrMessageEvent) -> Any:
        """从消息里找出音频组件：优先语音（Record），其次音频文件（File）。"""
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
        """读取音频组件的来源地址：本地路径或 URL。"""
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
        """把消息里的音频组件保存到数据目录，返回 ``(路径, 时长)``。"""
        source = self._component_source(component)
        if not source:
            raise ValueError("消息中没有可用的音频文件")
        preferred_name = str(getattr(component, "name", "") or "")
        destination = await self._materialize_audio(source, preferred_name)
        duration = await probe_audio_duration(self._ffmpeg_executable(), destination)
        return destination, duration

    async def _materialize_audio(self, source: str, preferred_name: str = "") -> Path:
        """把各种来源（file:// / URL / 本地路径）的音频读取并保存到数据目录。"""
        if source.startswith("file://"):
            parsed = unquote(urlparse(source).path)
            # Windows 的 file:///D:/... 去掉 scheme 后会多出开头的 /，需要剥掉
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

    # ------------------------------------------------------------------ #
    # 上传音频登记
    # ------------------------------------------------------------------ #

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

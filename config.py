"""配置读取与诊断日志（Mixin）。

本模块的 :class:`ConfigMixin` 会被混入最终的插件类
（见 main.py 的 :class:`KookAudioWelcomePlugin`），提供：

- 配置项的类型安全读取（``_conf_str`` / ``_conf_bool`` / ``_conf_int`` /
  ``_conf_list``）与常用配置的属性封装；
- 数据目录 / 插件目录 / 候选查找目录的解析；
- 诊断日志 ``_trace``：把执行流程写入独立日志文件，方便用户回传排查；
- 启动信息摘要与配置落盘的统一错误处理。
"""

from __future__ import annotations

import contextlib
import os
import shutil
import time
from pathlib import Path

from astrbot.api import logger
from astrbot.api.star import StarTools

from .constants import (
    CHAT_AUDIO_DIR_NAME,
    DEFAULT_LEAVE_DELAY,
    DEFAULT_START_DELAY,
    LOG_MAX_BYTES,
    PLUGIN_NAME,
    PLUGIN_VERSION,
    SOURCE_LABELS,
    SOURCE_PATH,
    SOURCE_UPLOAD,
    SOURCE_URL,
)
from .utils import to_int


class ConfigMixin:
    """配置读取、目录解析与诊断日志。方法由 main.py 的插件类继承使用。"""

    # ------------------------------------------------------------------ #
    # 配置读取
    # ------------------------------------------------------------------ #

    def _conf_str(self, key: str, default: str = "") -> str:
        """读取字符串配置，None 与两端空白统一处理。"""
        value = self.config.get(key, default)
        if value is None:
            return default
        return str(value).strip()

    def _conf_bool(self, key: str, default: bool = True) -> bool:
        """读取布尔配置，兼容 WebUI 可能保存的字符串形式。"""
        value = self.config.get(key, default)
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on", "是", "开"}
        return bool(value)

    def _conf_int(self, key: str, default: int, minimum: int, maximum: int) -> int:
        """读取整数配置并夹在 [minimum, maximum] 区间内。"""
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

    # ------------------------------------------------------------------ #
    # 常用配置的属性封装
    # ------------------------------------------------------------------ #

    @property
    def _audio_source(self) -> str:
        """欢迎音频来源，兼容旧版本的 ``local`` 取值。"""
        mode = self._conf_str("audio_source") or SOURCE_UPLOAD
        if mode not in SOURCE_LABELS:
            # 兼容旧版本：只有 welcome_audio 字符串配置时按本地路径处理
            return SOURCE_PATH if mode == "local" else SOURCE_UPLOAD
        return mode

    @property
    def _chat_audio_dir(self) -> Path:
        """聊天上传音频的存放目录。"""
        return self._data_dir / CHAT_AUDIO_DIR_NAME

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
        """把配置里的 FFmpeg 路径解析成可执行文件（相对路径走 PATH 查找）。"""
        path = self._ffmpeg_path
        if os.path.isabs(path):
            return path
        return shutil.which(path) or path

    # ------------------------------------------------------------------ #
    # 目录解析
    # ------------------------------------------------------------------ #

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

    def _resolve_data_dir(self) -> Path:
        """插件数据目录，StarTools 失败时回退到 ``data/plugin_data/<插件名>``。"""
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

    # ------------------------------------------------------------------ #
    # 启动摘要与诊断日志
    # ------------------------------------------------------------------ #

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
        """在 AstrBot 日志与诊断日志里输出一份启动配置摘要。"""
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
            # 手写轮转：主日志超过 1 MB 后改名 .log.1，防止无限膨胀
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
        """启动时检测一次 FFmpeg 并把结果写进 AstrBot 日志。"""
        ok, detail = await self._check_ffmpeg()
        if ok:
            logger.info(f"[KOOK语音欢迎] FFmpeg 检查通过：{detail}")
        else:
            logger.error(f"[KOOK语音欢迎] FFmpeg 不可用：{detail}")

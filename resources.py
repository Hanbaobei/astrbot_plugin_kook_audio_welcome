"""Token / 音频来源 / FFmpeg 资源解析（Mixin）。

- Bot Token 与机器人 ID 的解析：优先插件配置，其次从 KOOK 适配器读取；
- 欢迎音频来源解析：上传文件 / 网络链接 / 本地路径 / 自动查找四种方式，
  外加来源失效时的自动回退与只读预览；
- FFmpeg 可用性检测与缓存。
"""

from __future__ import annotations

import contextlib
import hashlib
import os
from pathlib import Path
from urllib.parse import urlparse

from astrbot.api import logger

from .constants import (
    AUDIO_EXTS,
    MAX_AUDIO_BYTES,
    SOURCE_AUTO,
    SOURCE_LABELS,
    SOURCE_PATH,
    SOURCE_UPLOAD,
    SOURCE_URL,
    URL_CACHE_DIR_NAME,
)
from .utils import run_capture, to_text


class ResourcesMixin:
    """资源解析与检测。方法由 main.py 的插件类继承。"""

    # ------------------------------------------------------------------ #
    # Token / 机器人 ID
    # ------------------------------------------------------------------ #

    def _resolve_token(self) -> str:
        """Bot Token：优先插件配置，其次从 KOOK 适配器配置里读取。"""
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
        """机器人用户 ID：优先缓存，其次从适配器客户端读取。"""
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
        """缓存里没有 bot_id 时通过 ``/user/me`` 拉取一次。"""
        if self._bot_id:
            return
        try:
            me = await self._api.get_me(token)
        except Exception as exc:
            logger.debug(f"[KOOK语音欢迎] 获取机器人信息失败：{exc}")
            return
        self._bot_id = to_text(me.get("id"))

    # ------------------------------------------------------------------ #
    # 配置路径解析
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

    # ------------------------------------------------------------------ #
    # 音频来源解析
    # ------------------------------------------------------------------ #

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

    # ------------------------------------------------------------------ #
    # URL 音频下载与缓存
    # ------------------------------------------------------------------ #

    def _url_cache_path(self, url: str) -> Path:
        """URL 音频的本地缓存路径：对 URL 取哈希，避免文件名过长或非法。"""
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

        # Content-Type 推断出的后缀与 URL 后缀不一致时，清掉旧缓存文件
        if destination != cache and cache.is_file():
            with contextlib.suppress(OSError):
                cache.unlink()

        logger.info(f"[KOOK语音欢迎] 欢迎音频已缓存到 {destination}")
        self._trace(f"欢迎音频已缓存：{destination}（{len(content)} 字节）")
        return destination

    # ------------------------------------------------------------------ #
    # FFmpeg 检测
    # ------------------------------------------------------------------ #

    async def _check_ffmpeg(self, force: bool = False) -> tuple[bool, str]:
        """检测 FFmpeg 是否可用（可执行 + 含 libopus 编码器），结果按路径缓存。

        失败时返回 ``(False, 原因)``，原因会展示给管理员。
        """
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
            # KOOK 推流必须 libopus 编码器；精简版 FFmpeg 不带，需要提前发现
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

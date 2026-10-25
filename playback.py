"""欢迎音频播放流程（Mixin）。

完整流程：解析音频 → 校验 FFmpeg → 加入语音频道 → 启动保活 → 延迟启动 →
FFmpeg RTP 推流 → 离开频道。同时负责：

- 播放请求队列与 worker 循环（同一时间只播放一份音频）；
- 机器人被移出频道时立刻中止推流；
- 尾部静音的预合成与缓存（保证整段播放只有一条 RTP 流）；
- 残留语音连接的释放与重试。
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import os
import time
from dataclasses import dataclass
from pathlib import Path

from astrbot.api import logger

from .constants import (
    DEFAULT_PLAYBACK_TIMEOUT,
    MAX_PAD_AUDIO_SECONDS,
    PLUGIN_NAME,
    STALE_VOICE_RELEASE_WAIT,
    STREAM_CACHE_DIR_NAME,
    VOICE_KEEP_ALIVE_INTERVAL,
)
from .ffmpeg import build_ffmpeg_args, playback_timeout
from .kook_api import KookApiError, VoiceJoinInfo
from .utils import probe_audio_duration, ffprobe_available, run_capture, to_text


@dataclass(slots=True)
class _PlayRequest:
    """一次播放请求。``reason`` 为 ``event``（成员加入触发）或 ``preview``。"""

    channel_id: str
    user_id: str = ""
    guild_id: str = ""
    reason: str = "event"


class PlaybackMixin:
    """播放队列与推流流程。方法由 main.py 的插件类继承。"""

    # ------------------------------------------------------------------ #
    # 队列
    # ------------------------------------------------------------------ #

    def _enqueue(self, request: _PlayRequest) -> None:
        """入队一次播放请求；队列已满时用新请求顶掉旧请求。"""
        if self._queue.full():
            with contextlib.suppress(asyncio.QueueEmpty):
                self._queue.get_nowait()
        with contextlib.suppress(asyncio.QueueFull):
            self._queue.put_nowait(request)

    async def _worker_loop(self) -> None:
        """串行消费播放请求，保证同一时间只有一次推流。"""
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

    # ------------------------------------------------------------------ #
    # 推流子进程跟踪
    # ------------------------------------------------------------------ #

    def _track_ffmpeg_process(self, process: asyncio.subprocess.Process) -> None:
        """记录当前推流进程；若已收到中止事件，创建后立刻结束它。"""
        self._ffmpeg_proc = process
        if self._stream_aborted and process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                process.kill()

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

    # ------------------------------------------------------------------ #
    # 主流程
    # ------------------------------------------------------------------ #

    async def _play_welcome(self, request: _PlayRequest) -> None:
        """一次完整的播放流程，由 worker 调用，异常不应逃逸。"""
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
                    "给听众客户端建立语音接收的时间"
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
        """启动 FFmpeg 推流并等待结束，按退出情况记录日志。"""
        ffmpeg = self._ffmpeg_executable()
        args = build_ffmpeg_args(
            ffmpeg=ffmpeg,
            audio_path=audio_path,
            info=info,
            volume=self._volume,
        )

        if duration is None:
            duration = await probe_audio_duration(ffmpeg, audio_path)
        timeout = playback_timeout(duration, DEFAULT_PLAYBACK_TIMEOUT)

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

    # ------------------------------------------------------------------ #
    # 尾部静音合成
    # ------------------------------------------------------------------ #

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
                f"音频时长 {duration:.0f}s 超过 {MAX_PAD_AUDIO_SECONDS}s，跳过尾部静音"
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
            # anullsrc 生成静音源，与音频统一为 48kHz 立体声后 concat
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

    # ------------------------------------------------------------------ #
    # 语音连接管理
    # ------------------------------------------------------------------ #

    async def _keepalive_loop(self, token: str, channel_id: str) -> None:
        """推流期间定期发送语音保活，防止连接被服务端回收。"""
        while True:
            await asyncio.sleep(VOICE_KEEP_ALIVE_INTERVAL)
            try:
                await self._api.keep_alive(token, channel_id)
                logger.debug("[KOOK语音欢迎] 已发送语音保活")
            except Exception as exc:
                logger.debug(f"[KOOK语音欢迎] 语音保活失败：{exc}")

    async def _join_voice(self, token: str, channel_id: str) -> VoiceJoinInfo:
        """加入语音频道；失败时释放残留连接并重试一次。"""
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

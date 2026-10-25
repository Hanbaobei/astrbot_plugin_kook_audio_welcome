"""FFmpeg 推流命令拼装。

按 KOOK 官方示例构造 RTP 推流参数。官方文档：
https://developer.kookapp.cn/doc/http/voice
"""

from __future__ import annotations

from pathlib import Path

from .constants import PLAYBACK_TIMEOUT_MARGIN
from .kook_api import VoiceJoinInfo
from .utils import to_int


def build_rtp_tee(info: VoiceJoinInfo) -> str:
    """构造 tee 的 RTP 输出目标（含 ssrc / payload_type / rtcp 端口）。"""
    target = f"rtp://{info.ip}:{info.port}"
    if not info.rtcp_mux and info.rtcp_port:
        target += f"?rtcpport={info.rtcp_port}"
    return (
        f"[select=a:f=rtp:ssrc={info.audio_ssrc}:payload_type={info.audio_pt}]{target}"
    )


def rtp_bitrate(info: VoiceJoinInfo) -> int:
    """取有效推流码率，接口未返回时退回 48k。"""
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
        # -re 按音频的真实时长推流，保证 KOOK 服务端收到的时序与播放一致
        "-re",
        "-i",
        str(audio_path),
        "-map",
        "0:a:0",
        # 只保留音频流，忽略封面 / 字幕等
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


def playback_timeout(duration: float | None, default: int) -> float:
    """根据音频时长计算推流超时；时长未知时使用默认值。"""
    if duration:
        return duration + PLAYBACK_TIMEOUT_MARGIN
    return float(default)

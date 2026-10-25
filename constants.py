"""全局常量与默认值。

集中存放所有模块共享的常量：插件标识、KOOK 接口地址、语音推流的时序参数、
音频格式白名单、欢迎音频来源选项、缓存目录名、Content-Type 映射等。

部分常量与 KOOK 服务端的时序约束直接相关（keep-alive 间隔、leave 后重进
等待、尾部静音时长等），调整前请先阅读注释并查阅官方文档：
https://developer.kookapp.cn/doc/http/voice
"""

from __future__ import annotations

# --------------------------------------------------------------------------- #
# 插件标识
# --------------------------------------------------------------------------- #

PLUGIN_NAME = "astrbot_plugin_kook_audio_welcome"
PLUGIN_VERSION = "v1.4.3"

# 诊断日志单文件上限，超过后轮转为 .log.1
LOG_MAX_BYTES = 1024 * 1024

# --------------------------------------------------------------------------- #
# KOOK 接口
# --------------------------------------------------------------------------- #

KOOK_API_BASE = "https://www.kookapp.cn/api/v3"
# KOOK 网关系统消息的 type 固定为 255，具体事件名在 extra.type 字段里
KOOK_SYSTEM_MESSAGE_TYPE = 255
KOOK_VOICE_JOINED = "joined_channel"
KOOK_VOICE_EXITED = "exited_channel"

# --------------------------------------------------------------------------- #
# 语音推流时序
# --------------------------------------------------------------------------- #

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

# --------------------------------------------------------------------------- #
# 音频文件
# --------------------------------------------------------------------------- #

# 上传音频大小上限
MAX_AUDIO_BYTES = 25 * 1024 * 1024
# 超过这个时长的音频不再预拼尾部静音，避免生成过大的中间 WAV
MAX_PAD_AUDIO_SECONDS = 300
# 下载音频用的 UA：部分 CDN 会拒绝空 UA 的请求
DOWNLOAD_USER_AGENT = f"AstrBotKOOKAudioWelcome/{PLUGIN_VERSION.lstrip('v')}"

# 允许推流的音频扩展名
AUDIO_EXTS = {".mp3", ".wav", ".m4a", ".ogg", ".flac", ".aac", ".opus", ".webm"}

# --------------------------------------------------------------------------- #
# 欢迎音频来源（对应 _conf_schema.json 中 audio_source 的 options）
# --------------------------------------------------------------------------- #

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

# --------------------------------------------------------------------------- #
# 数据目录与缓存
# --------------------------------------------------------------------------- #

# URL 音频的本地缓存目录；聊天上传的音频存放目录；音频+尾部静音的合成缓存目录
URL_CACHE_DIR_NAME = "audio_cache"
CHAT_AUDIO_DIR_NAME = "audio"
STREAM_CACHE_DIR_NAME = "stream_cache"

# HTTP Content-Type -> 音频扩展名，下载音频时用于推断保存后缀
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

# KOOK 语音欢迎插件

> 当成员进入 KOOK 语音频道时，机器人自动加入、播放欢迎音频，播放完毕后立即离开。

适配 **KOOK（开黑啦）** 语音频道的 AstrBot 插件。机器人不常驻语音频道，按需加入、用完即走，适合社区、游戏开黑、语音房等场景。

![AstrBot](https://img.shields.io/badge/AstrBot-%E2%89%A54.16-4B8BBE)
![KOOK](https://img.shields.io/badge/KOOK-Voice-6A5ACD)
![FFmpeg](https://img.shields.io/badge/FFmpeg-Required-007808)

## 目录

- [功能特性](#功能特性)
- [工作原理](#工作原理)
- [环境要求](#环境要求)
- [安装](#安装)
- [配置](#配置)
- [使用方法](#使用方法)
- [指令列表](#指令列表)
- [技术细节](#技术细节)
- [诊断日志](#诊断日志)
- [注意事项](#注意事项)
- [常见问题](#常见问题)
- [开发与测试](#开发与测试)
- [致谢](#致谢)

## 功能特性

- **事件驱动，按需加入**：监听 KOOK 语音频道的成员加入事件（`joined_channel`），事件触发后才加入频道，不占用常驻语音连接。
- **复用 AstrBot 的网关连接**：直接挂接 KOOK 适配器的网关回调，不额外建立第二条 WebSocket，也不重复占用机器人会话。
- **自动跟随频道**：目标频道留空时，自动跟随触发事件中的语音频道；也可以固定监听某一个频道。
- **多种音频来源**：支持在管理面板**上传文件**、填写**网络链接**（自动下载缓存）、指定**本地路径**，或自动查找数据目录下的 `welcome.*`；来源不可用时自动回退，不会突然没声音。
- **MP3 / WAV 等格式**：支持 MP3、WAV、M4A、OGG、FLAC、AAC、OPUS、WebM，可随时更换。
- **FFmpeg 实时推流**：自动转码为 KOOK 要求的 Opus 格式，通过 RTP 推送到语音服务器。
- **码率自适应**：读取 `/voice/join` 返回的 `bitrate`，按官方限制控制推流码率，避免因超限被断开。
- **完整生命周期**：加入频道 → 等待 → 推流 → 保活 → 尾部静音 → 离开频道，全流程自动完成。
- **首次进入也能听到**：空频道里第一个进入的人，客户端尚未建好语音接收，插件会先等 2 秒再播放，并在音频末尾附上 2 秒静音（同一条 RTP 流）。
- **并发保护**：对短时间内的多次加入事件做防抖与串行处理，并遵守离开后的冷却时间，避免并发加入失败。
- **可视化配置**：常用选项都可以在 AstrBot 管理面板的「插件管理」中完成，无需改代码。
- **指令管理**：管理员可以通过聊天指令上传音频、设置链接、切换来源、试听和重载。
- **诊断日志**：内置独立日志文件，方便测试和远程排查问题。

## 工作原理

```text
成员进入语音频道
        │
        ▼
KOOK 网关系统消息（type=255, extra.type=joined_channel）
        │
        ▼
AstrBot KOOK 适配器 KookClient
        │  适配器只处理聊天消息，本插件在 event_callback 上挂接一层
        ▼
┌──────────────────┐
│  防抖 / 冷却 / 队列 │
└────────┬─────────┘
         ▼
POST /api/v3/voice/join
         │  返回 ip / port / bitrate / audio_ssrc / audio_pt
         ▼
等待 2 秒（给听众客户端建立语音接收）
         │
         ▼
FFmpeg 转码 Opus ──► RTP 推流（周期 keep-alive）
         │  推流期间每 40 秒调用 /voice/keep-alive
         ▼
音频末尾自带 2 秒静音（同一条 RTP 流，让服务端把缓冲中的音频放完）
         │
         ▼
POST /api/v3/voice/leave
         │
         ▼
等待 3 秒冷却，处理下一次事件
```

1. **挂接事件**：AstrBot 的 KOOK 适配器会把网关收到的所有事件交给 `KookClient.event_callback`，但适配器自身只处理聊天消息和角色变更，语音频道事件会被丢弃。插件在加载后包装该回调，额外读取 `joined_channel` 事件，其余事件仍原样交给适配器。
2. **过滤**：忽略机器人自己产生的加入事件、非目标频道、播放中与冷却期内的重复事件。
3. **加入频道**：携带 Bot Token 调用 `/api/v3/voice/join`，拿到语音服务器地址、端口、码率与 RTP 参数。
4. **等待**：空频道里第一个进入的人，客户端往往还没建立好语音接收，因此先等待「启动延迟」再播放。
5. **转码推流**：FFmpeg 将欢迎音频转为 Opus，按返回的 `bitrate` 推流到 `rtp://ip:port`。
6. **保持活跃**：推流期间每 40 秒调用 `/api/v3/voice/keep-alive`，防止连接被服务端回收。
7. **尾部静音**：把「停留」时长的静音预先拼进音频再推流，整段播放始终只有一条 RTP 流：SSRC 与序列号连续，服务端不会把它当成新流丢掉。
8. **离开频道**：静音结束后调用 `/api/v3/voice/leave`，并等待冷却时间再处理下一次加入。

## 环境要求

| 项目 | 要求 |
| --- | --- |
| AstrBot | ≥ 4.16，KOOK 适配器已启用并在线 |
| KOOK 机器人 | 已配置 Bot Token，且具备加入语音频道的权限 |
| FFmpeg | 已安装，需包含 `libopus` 编码器（插件启动时会自动检测） |
| 网络 | 服务器可访问 KOOK 开放平台，并允许向语音服务器发送 UDP 数据 |
| 音频文件 | MP3 / WAV 等，建议 48 kHz、双声道、时长 5–15 秒 |

> KOOK 的 RTP 推流走 UDP。如果服务器防火墙只放行了 TCP，可能会出现「成功加入频道但没有声音」的情况。

## 安装

### 1. 安装插件

将本插件文件夹放入 AstrBot 的插件目录，通常为：

```text
AstrBot/
└── data/
    └── plugins/
        └── astrbot_plugin_kook_audio_welcome/   # 本插件目录
```

然后重启 AstrBot，或在管理面板的「插件管理」中重载。

### 2. 安装 FFmpeg

**Windows（winget）：**

```powershell
winget install --id Gyan.FFmpeg -e
```

**Ubuntu / Debian：**

```bash
sudo apt update
sudo apt install -y ffmpeg
```

**macOS（Homebrew）：**

```bash
brew install ffmpeg
```

安装完成后验证：

```bash
ffmpeg -version
ffmpeg -encoders | grep opus
```

Windows 下如果没有 `grep`，可以把第二条命令换成：

```powershell
ffmpeg -encoders | findstr opus
```

确认输出中包含 `libopus` 即可。若 FFmpeg 没有加入系统 `PATH`，请在插件配置中填写 `ffmpeg` 可执行文件的绝对路径。

### 3. 启用插件

在 AstrBot 管理面板的「插件管理」中启用本插件，并按下方说明完成配置。

### 4. 准备欢迎音频

欢迎音频支持四种来源，任选其一：

- **管理面板上传（推荐）**：在插件配置中把「欢迎音频来源」选为「上传的音频文件」，然后在「上传的欢迎音频」里点击上传（支持多选与拖拽）。文件保存在插件数据目录，插件更新不会被清空。
- **网络链接**：「欢迎音频来源」选「网络链接（URL）」，在「欢迎音频链接」填写音频直链（如 `https://example.com/welcome.mp3`）。首次播放时会自动下载并缓存到本地，之后离线也能播放。
- **聊天上传**：管理员发送音频文件后引用它，发送 `/语音欢迎上传`；也可以用 `/语音欢迎链接 <url>` 直接设置链接。
- **本地路径 / 自动查找**：把音频放进 `data/plugin_data/astrbot_plugin_kook_audio_welcome/` 命名为 `welcome.mp3`（或 `welcome.wav`），来源选「自动查找」即可；已有服务器文件路径的场景选「本地文件路径（高级）」并填写绝对路径。

> 来源切换失败、或所选来源的文件暂时不可用时，插件会自动回退到其它已配置的来源，不会突然没声音。

## 配置

在 AstrBot 管理面板的「插件管理」中找到本插件，进入配置页面：

| 配置项 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| 启用语音欢迎 | 开关 | 开启 | 关闭后不再响应成员进入语音频道事件，管理指令仍然可用。 |
| 目标语音频道 ID | 字符串 | 空 | 留空时自动跟随事件中的频道；填写后只对该频道生效。 |
| 欢迎音频来源 | 下拉 | 上传的音频文件 | 可选：上传的音频文件 / 网络链接（URL）/ 本地文件路径（高级）/ 自动查找 `welcome.*`。 |
| 上传的欢迎音频 | 文件上传 | 空 | 点击按钮上传音频（支持多选与拖拽），列表第一个可用文件生效。 |
| 欢迎音频链接 | 字符串 | 空 | 音频直链，首次播放时自动下载并缓存到插件数据目录。 |
| 欢迎音频文件路径（高级） | 字符串 | 空 | 服务器上音频的绝对路径，适合不方便上传的场景。 |
| 播放音量 | 整数 | 100 | 推流音量，范围 0–100，100 表示原始音量。 |
| 触发冷却 | 整数 | 3 | 播放结束后的等待秒数，建议不低于 3 秒。 |
| 加入频道后等待 | 整数 | 2 | 加入后先等这么久再播放，给听众客户端留出建立语音接收的时间。 |
| 播放结束后停留 | 整数 | 2 | 音频末尾附加的静音秒数（会拼进同一条 RTP 流），避免服务端丢弃缓冲中的音频。 |
| FFmpeg 路径 | 字符串 | `ffmpeg` | FFmpeg 未加入 `PATH` 时填写可执行文件的绝对路径。 |
| 写入诊断日志 | 开关 | 开启 | 把运行流程写入插件自己的日志文件，便于排查问题。 |
| KOOK Bot Token | 字符串（密文） | 空 | 可选。留空时自动读取 AstrBot 中 KOOK 适配器的 Token。 |
| 语音频道密码 | 字符串（密文） | 空 | 可选。目标语音频道设置了密码时填写。 |

### 配置建议

- **频道 ID**：在 KOOK 客户端中右键语音频道，选择「复制频道 ID」即可。
- **音频链接**：必须是能直接下载音频的直链；如果链接返回的是网页（如网盘分享页），插件会拒绝并提示。
- **音频路径**：Windows 路径包含空格或中文时，建议直接填写完整绝对路径。
- **音频时长**：欢迎语建议控制在 5–15 秒，太长会延长机器人占用语音频道的时间。
- **音频质量**：推荐 48 kHz 采样率、双声道；响度尽量统一，避免忽大忽小或削波。
- **固定频道与自动跟随**：只有一个语音房时推荐留空自动跟随；多个语音房同时使用时，可填写固定频道 ID。

## 使用方法

1. **准备欢迎音频**：见「安装 → 准备欢迎音频」，选择任一来源。
2. **设置频道**：填写目标语音频道 ID，或留空以自动跟随事件频道。
3. **进入频道测试**：用另一个账号进入目标语音频道，机器人应自动加入并播放欢迎音频，播放结束后离开。
4. **查看日志**：如果机器人没有加入或没有声音，先看 AstrBot 日志与[诊断日志](#诊断日志)。

## 指令列表

> 指令前缀以 AstrBot 全局设置为准，下表以默认的 `/` 为例。管理员指 AstrBot 配置中的管理员账号。

### 基础指令

| 指令 | 权限 | 功能说明 |
| --- | --- | --- |
| `/语音欢迎` | 所有人 | 查看插件状态、音频来源、FFmpeg 与诊断日志路径。 |
| `/语音欢迎帮助` | 所有人 | 查看指令列表与使用说明。 |

### 管理指令（仅管理员）

| 指令 | 权限 | 功能说明 |
| --- | --- | --- |
| `/语音欢迎上传` | 管理员 | 引用或附带 MP3/WAV 音频，上传为欢迎音频并登记到「上传的欢迎音频」。 |
| `/语音欢迎链接 <url>` | 管理员 | 用网络音频链接作为欢迎音频，自动下载缓存并切换来源。 |
| `/语音欢迎来源 <upload\|url\|path\|auto>` | 管理员 | 切换欢迎音频来源，不带参数时显示当前来源与可选值。 |
| `/语音欢迎试听 [频道ID]` | 管理员 | 让机器人加入指定语音频道试听；不填则使用配置中的目标频道。 |
| `/语音欢迎重载` | 管理员 | 重新读取配置并检测 FFmpeg，无需重启 AstrBot。 |

## 技术细节

### 语音频道事件

成员加入语音频道时，KOOK 网关下发一条系统消息：

```json
{
  "s": 0,
  "d": {
    "channel_type": "GROUP",
    "type": 255,
    "target_id": "<guild_id>",
    "extra": {
      "type": "joined_channel",
      "body": {
        "user_id": "<user_id>",
        "channel_id": "<voice_channel_id>",
        "joined_at": 1612790368279
      }
    }
  }
}
```

插件从 `extra.body` 中取出 `user_id` 与 `channel_id`，从 `target_id` 中取出服务器 ID。

### 加入语音频道

调用 KOOK 接口：

```http
POST https://www.kookapp.cn/api/v3/voice/join
Authorization: Bot <你的 Bot Token>
Content-Type: application/json

{
  "channel_id": "<语音频道 ID>"
}
```

接口返回的 `data` 中通常包含：

- `ip`：语音服务器地址
- `port`：RTP 推流端口
- `rtcp_mux` / `rtcp_port`：是否复用 RTCP 端口及对应端口
- `bitrate`：允许的码率上限
- `audio_ssrc` / `audio_pt`：RTP 的 SSRC 与 payload type

### FFmpeg 推流

插件拼装的命令与 KOOK 官方示例一致：

```bash
ffmpeg -hide_banner -loglevel error -nostdin -re -i "welcome.mp3" \
  -map 0:a:0 -vn -sn -dn \
  -acodec libopus -b:a <bitrate> -ac 2 -ar 48000 \
  -filter:a "volume=0.80" \
  -f tee "[select=a:f=rtp:ssrc=<audio_ssrc>:payload_type=<audio_pt>]rtp://<ip>:<port>"
```

说明：

- `-re`：按音频原始速度读取，模拟实时推流，避免一次性灌入。
- `-ar 48000`：KOOK 使用 48 kHz 的 Opus 音频。
- `-b:a`：取自 `/voice/join` 返回的 `bitrate`，不要超过其 **120%**，否则可能被 KOOK 断开。
- 加入语音频道时会显式传 `rtcp_mux=false`，让 KOOK 返回独立的 `rtcp_port`，推流地址随之带上 `?rtcpport=<rtcp_port>`。这样 FFmpeg 会发送 RTCP SR；`rtcp_mux=true` 时 FFmpeg 一条 RTCP 都不会发，部分媒体服务器因此不会转发音频。
- 音量不等于 100 时才会附加 `volume` 滤镜。
- 「播放结束后停留」的静音并不是另起一个进程推：那样会带来新的 SSRC 序列号与新的 UDP 源端口，KOOK 会当成一条新流丢弃。插件会在播放前用 `concat` 滤镜把静音拼进音频（PCM WAV），再由同一个 FFmpeg 进程一次推完。合成结果按「文件 + 大小 + 修改时间 + 静音长度」缓存到 `data/plugin_data/<插件名>/stream_cache/`，重复播放不会再次转码。

### 保活与离开

- 推流期间每 **40 秒** 调用 `POST /api/v3/voice/keep-alive`（官方建议 45 秒内）。
- 加入频道后先等待「加入频道后等待」（默认 2 秒）再播放：空频道里第一个进入的人，客户端常常还没建好语音接收，立刻播放会导致对方什么都听不到。
- 「播放结束后停留」（默认 2 秒）的静音已拼进音频流，推流结束后才调用 `POST /api/v3/voice/leave`。如果 RTP 流在音频结束的瞬间断开，服务端可能直接丢弃缓冲中的最后一段音频。
- 离开频道后需要等待 **2–3 秒** 才能重新加入，插件通过冷却时间与串行队列处理。
- 如果加入失败，插件会查询 `/voice/list`，释放残留语音连接后重试一次。

## 诊断日志

插件会把关键流程写入独立日志文件，默认开启：

```text
AstrBot/data/plugin_data/astrbot_plugin_kook_audio_welcome/logs/kook_audio_welcome.log
```

- 路径可以在 `/语音欢迎` 的返回结果中看到。
- 内容包含：插件加载、适配器挂接、事件过滤原因、加入/离开、FFmpeg 命令与退出码、异常信息。
- **不会写入 Bot Token**。
- 单文件超过 1 MB 会轮转为 `kook_audio_welcome.log.1`。
- 如需关闭，在插件配置里关闭「写入诊断日志」。

测试结果可以记录在仓库根目录的 [TEST_LOG.md](TEST_LOG.md)。

## 注意事项

- **单一频道限制**：机器人同一时间只能加入一个语音频道，并发加入会失败，插件已做队列与防抖处理。
- **被移出频道会自动停止**：播放过程中如果机器人被踢出频道、频道被删除或权限被收回，插件会收到 `exited_channel` 并立刻结束推流，不会把剩下的音频推完。
- **残留连接处理**：加入失败时会释放一条残留语音连接（优先目标频道），最多一条，不会把同一 Bot Token 下其它实例的连接一起踢掉。
- **配置写盘失败会提示**：管理指令会检查配置是否真的保存成功，只读目录或磁盘满时会在回复里注明「配置未能写入磁盘」。
- **码率红线**：`/voice/join` 返回的 `bitrate` 是硬性要求，FFmpeg 推流时必须用 `-b:a` 控制码率。
- **FFmpeg 验证**：部署前先在 Bot 运行环境中执行 `ffmpeg -version`，确保二进制和 `libopus` 编码器可用。
- **UDP 网络**：RTP 推流依赖 UDP，请确认服务器防火墙与安全组没有拦截。
- **端口不可复用**：KOOK 会把推流地址绑定到首次推流的 IP/端口，因此插件每次都重新调用 `/voice/join`。
- **音频版权**：请使用拥有播放权限的音频，避免在公开社区中引发版权问题。
- **权限检查**：机器人必须拥有加入目标语音频道的权限，否则 `/voice/join` 会返回错误。
- **适配器兼容**：插件通过包装 KOOK 适配器的网关回调获取事件。如果 AstrBot 大版本调整了适配置内部结构，插件会在日志中提示「无法挂接」，此时请升级插件或提交 issue。

## 常见问题

### 机器人没有加入语音频道

按顺序检查：

1. KOOK 适配器是否在线，Bot Token 是否有效；
2. `/语音欢迎` 返回的「KOOK 适配器」数量是否大于 0；
3. 诊断日志中是否有「已挂接 KOOK 适配器」与「收到语音加入事件」；
4. 目标频道 ID 是否正确，机器人是否有加入该频道的权限；
5. 是否处于冷却期，或上一次连接尚未释放；
6. 查看日志中 `/voice/join` 的错误码与信息。

### 机器人加入了频道，但没有声音

1. 发送 `/语音欢迎` 确认音频来源与解析到的文件都存在；
2. 执行 `ffmpeg -version`，确认 FFmpeg 可执行；
3. 执行 `ffmpeg -encoders | grep opus`，确认包含 `libopus`；
4. 确认服务器允许向语音服务器发送 UDP 数据；
5. 检查配置中的播放音量是否被设为 0；
6. 检查诊断日志里 FFmpeg 的退出码与错误输出。

### 空频道的第一个人听不到声音

这是早期版本最常见的问题：成员刚进入空频道时，KOOK 客户端还在建立语音接收，机器人却已经播放完毕并离开。

- 插件现在默认「加入后等待 2 秒再播放」，并把音频末尾「停留 2 秒」的静音拼进同一条流推完再离开。
- 如果对方设备较慢，可以把「加入频道后等待」调到 3–4 秒、「播放结束后停留」调到 3 秒。
- 把这两项调成 0 会恢复旧行为，通常不建议。

### 上传 / 链接配置了但没生效

- 上传后请确认「欢迎音频来源」是否为「上传的音频文件」；用聊天指令 `/语音欢迎上传` 会自动切换。
- WebUI 上传列表中的第一个可用文件生效；如果第一个文件被删除，会自动取下一个。
- 链接来源要求是能直接下载音频的直链，返回网页（如网盘分享页）会被拒绝，诊断日志中会写明原因。
- 任何来源不可用时都会自动回退到其它已配置的来源，不会突然没声音。

### 提示已经在语音频道中 / 加入失败

机器人同一时间只能加入一个语音频道。请确认上一次播放已经结束并调用了 `/voice/leave`，然后等待 2–3 秒再试。插件在加入失败时会自动释放一条残留连接（优先目标频道）并重试一次；如果残留连接在别的频道，诊断日志里会写明释放的是哪个频道。

### 欢迎音频被截断

- 确认播放结束后才调用 `/voice/leave`，不要在 FFmpeg 进程退出前离开；
- 确认推流时使用了 `-re`；
- 如果音频较长，检查保活是否正常；
- 建议把欢迎音频控制在 5–15 秒。

### Windows 下提示找不到 FFmpeg

- 将 FFmpeg 的 `bin` 目录加入系统 `PATH`，或
- 在插件配置的「FFmpeg 路径」中填写 `ffmpeg.exe` 的绝对路径，例如 `C:\ffmpeg\bin\ffmpeg.exe`；
- 修改后使用 `/语音欢迎重载` 或重启 AstrBot。

### 多人同时进入语音频道会怎样

插件对事件做防抖与串行处理，播放期间的新事件会被忽略，避免同时发起多个 `/voice/join`。如果短时间内大量成员进入，可以适当增大「触发冷却」。

### 日志在哪里？

见[诊断日志](#诊断日志)；也可以直接发送 `/语音欢迎` 查看路径。

## 开发与测试

### 目录结构

```text
astrbot_plugin_kook_audio_welcome/
├── main.py              # 插件入口：生命周期、事件分发与全部指令 handler
├── constants.py         # 全局常量与时序参数
├── utils.py             # 类型转换、子进程执行、ffprobe 封装
├── kook_api.py          # KOOK 开放平台 HTTP 客户端（语音接口 + 音频下载）
├── ffmpeg.py            # FFmpeg RTP 推流命令拼装
├── config.py            # 配置读取、目录解析与诊断日志（ConfigMixin）
├── hooking.py           # KOOK 适配器事件挂接（HookingMixin）
├── playback.py          # 播放队列与推流流程（PlaybackMixin）
├── resources.py         # Token / 音频来源 / FFmpeg 资源解析（ResourcesMixin）
├── commands.py          # 指令参数解析与上传音频处理（CommandsMixin）
├── _conf_schema.json    # 插件配置模式（WebUI 可视化配置 + 文件上传）
├── metadata.yaml        # 插件元数据
├── ruff.toml            # 与 AstrBot 主仓库一致的 Ruff 配置
├── requirements.txt     # 运行依赖
├── OPTIMIZATION.md      # 优化清单：待修 bug、建议与增强路线
├── README.md
└── TEST_LOG.md          # 测试反馈记录
```

插件数据统一存放在 `data/plugin_data/astrbot_plugin_kook_audio_welcome/`，与插件代码目录分离，更新插件不会丢失：

```text
data/plugin_data/astrbot_plugin_kook_audio_welcome/
├── audio/               # 聊天上传的欢迎音频
├── audio_cache/         # 链接音频的本地缓存
├── stream_cache/        # 「音频 + 尾部静音」的合成缓存（可安全删除）
└── logs/
    └── kook_audio_welcome.log
```

### 代码风格

```bash
ruff format .
ruff check .
```

### 测试建议

1. 在 AstrBot 中重载插件，查看诊断日志是否出现「已挂接 KOOK 适配器」。
2. 分别测试四种音频来源：管理面板上传、`/语音欢迎链接`、`/语音欢迎上传`、本地路径 / `welcome.*`。
3. 用另一个账号进入语音频道，观察完整流程。
4. 按 [TEST_LOG.md](TEST_LOG.md) 的模板记录结果，并把诊断日志贴回。

## 开发文档

### KOOK 官方文档

- **语音接口（核心）**：<https://developer.kookapp.cn/doc/http/voice>
  - `POST /api/v3/voice/join`：加入语音频道，返回推流 IP、端口、码率与 RTP 参数
  - `POST /api/v3/voice/leave`：离开频道，退出后需等待 2–3 秒才能重新加入
  - `POST /api/v3/voice/keep-alive`：推流期间保持连接活跃
  - `GET /api/v3/voice/list`：获取机器人当前加入的语音频道
- **事件结构**：<https://developer.kookapp.cn/doc/event/event-introduction>
- **用户相关事件（含 joined_channel）**：<https://developer.kookapp.cn/doc/event/user>
- **KOOK 开发者文档首页**：<https://developer.kookapp.cn/doc>

### AstrBot 插件开发

- **插件开发指南**：<https://docs.astrbot.app/dev/star/plugin-new.html>
- **插件配置（含 `file` 文件上传类型）**：<https://docs.astrbot.app/dev/star/guides/plugin-config.html>
- 关键要点：
  - 从 `helloworld` 模板开始；
  - 插件目录放在 AstrBot 的插件目录下；
  - 正确填写 `metadata.yaml`；
  - 持久化数据放在 `data/plugin_data/<插件名>` 下，避免更新时被覆盖。

### FFmpeg

- **官方文档**：<https://ffmpeg.org/documentation.html>
- 重点参数：`-re`、`-ar`、`-ac`、`-c:a libopus`、`-b:a`、`-f tee`、`-f rtp`。

## 致谢

- 感谢 [AstrBot](https://github.com/AstrBotDevs/AstrBot) 提供插件框架与 KOOK 适配能力。
- 感谢 [KOOK 开放平台](https://developer.kookapp.cn/) 提供语音接口与开发文档。
- 感谢 [FFmpeg](https://ffmpeg.org/) 提供音频转码与 RTP 推流能力。

> 本插件仅供学习与交流使用，请遵守 KOOK 开放平台的服务条款与社区规范。

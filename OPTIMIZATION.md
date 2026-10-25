# 优化清单

> 记录时间：2026-10-24 ・ 基线版本：v1.4.1 ・ 当前版本：v1.4.3
>
> 本文档由完整代码审阅整理，P0（正确性/安全）与 B 系列（必修 bug）已修复，
> 其余按"建议 → 维护 → 增强"保留，供后续迭代取用。
>
> 标记说明：🔴 必须维修；🟡 建议修复；🔵 体验/架构增强。

---

## 一、已修复

### v1.4.3 · B 系列（必修 bug）

| 编号 | 问题 | 位置 | 修复方式 | 验证 |
| --- | --- | --- | --- | --- |
| B-1 | 适配器换 `client` 实例时旧 wrapper 未还原，一次事件被处理两次 | `_attach_adapter()` `main.py:943` | 只要 hook 存在且「客户端不同」或「回调被改写」，先 `_detach()` 还原旧实例再重挂 | 重载适配器后只出现一次「已挂接」 |
| B-2 | `_release_stale_voice()` 把 `/voice/list` 里的频道全部 leave | `main.py:1419` | 最多释放一条，优先目标频道；多条时只取第一条并写日志 | 同 Token 多实例时不再互相踢下线 |
| B-3 | `save_config()` 失败被 `suppress(Exception)` 吞掉，仍回复成功 | `_save_config()` `main.py:889`，用于 `cmd_link` / `cmd_source` / `_register_uploaded_audio` | 统一走 `_save_config()`，失败返回 `False`，指令回复追加「⚠️ 配置未能写入磁盘」 | 配置目录只读时指令能明确报错 |
| B-4 | 先 `join` 再校验音频，坏文件留下一次空 join | `_play_welcome()` `main.py:1127` | 加入频道前先 `ffprobe` 探测；用 `ffprobe_available()` 区分「音频损坏」与「没装 ffprobe」，后者不拦截播放 | 把 `.txt` 改名成 `.mp3`，机器人不加入频道 |
| B-5 | 保活从「开始推流」计时，未覆盖启动延迟与静音 | `main.py:1195`、`_keepalive_loop()` `main.py:1396` | 保活任务在 `join` 成功后立即创建，覆盖「启动延迟 + 推流」，`finally` 统一取消 | `start_delay=10` + 60 秒音频，首个保活在 45 秒内 |
| B-6 | 只解析 `joined_channel`，机器人被踢后仍推完整段音频 | `extract_channel_event()`、`_on_bot_left_voice()` `main.py:1072` | 解析 `exited_channel`；`user_id == bot_id` 且频道匹配时置中止标记并 `kill()` 当前 FFmpeg 进程，`run_capture` 新增 `on_start` 回调暴露进程句柄 | 播放中被踢，日志立即出现中止记录 |
| B-7 | 同秒同名上传互相覆盖 | `_write_chat_audio()` `main.py:2138` | 撞名后缀从秒级时间戳改为 `uuid4().hex[:8]` | 1 秒内连续上传两次得到两个文件 |
| B-8 | 卸载清理可能被二次取消，留下残留连接 | `terminate()` `main.py:693`、`_play_welcome()` | 先 `kill` 推流进程；`_leave_voice()` 返回是否成功，失败时用 `_release_stale_voice()` 兜底；`_play_welcome` 的 `finally` 用 `asyncio.shield` 包住 leave | 播放中重载插件，`/voice/list` 为空 |

附带改动：

- 新增 `ffprobe_available()`、`extract_channel_event()`、`_save_config()`、`_on_bot_left_voice()`、`_track_ffmpeg_process()`。
- `run_capture()` 新增 `on_start` 回调；`_stream_audio()` 改为接收 `duration`，避免重复探测。
- 已用桩模块（模拟 AstrBot）做了一次真实导入与逻辑冒烟：事件解析、FFmpeg 命令、同秒上传、预拼静音、链接只读解析共 22 项全部通过。

### v1.4.2 · P0（正确性与安全）

| 编号 | 问题 | 位置 | 修复方式 | 验证 |
| --- | --- | --- | --- | --- |
| P0-1 | `/语音欢迎来源` 只回复「已切换」，从未写回 `audio_source` | `cmd_source()` | 先写配置再 `save_config()`，失败时明确提示 | 切来源后重启仍保持 |
| P0-2 | 下载音频复用了带 `Authorization` 的会话，泄露 Bot Token | `KookApi.download()` | 新增无鉴权、独立 UA 的下载会话，`close()` 一并释放 | 下载请求不含 `Authorization` |
| P0-3 | 尾部静音另起进程，SSRC/序列号/源端口全变，被当成新流 | `_prepare_stream_audio()` | 播放前用 `concat` 把静音拼进音频（PCM WAV），整段只有一条 RTP 流，按「路径+大小+mtime+静音长度」缓存 | `welcome.wav` 实测 1.53s → 3.53s |
| P0-4 | 状态与重载指令会真的去下载链接音频 | `_resolve_audio_readonly()` | 只读解析，不发起网络请求 | 清空缓存后指令秒回 |

---

## 二、建议修复（🟡）

| 编号 | 问题 | 位置 | 影响 | 建议 |
| --- | --- | --- | --- | --- |
| S-1 | `target_channel_id` 留空 = 任意服务器的任意语音频道都能触发 | `main.py:1021` `_on_kook_event()`、`_conf_schema.json` | 别人把你拉进频道就能触发；多服务器会抢占唯一的语音连接 | 增加 `guild_whitelist` / `channel_blacklist`，默认只响应配置内服务器 |
| S-2 | 只有 3 秒全局冷却，没有按用户/频道节流 | `main.py:1053` 附近 | 同一用户反复进出可无限触发，刷屏并长期占用频道 | 增加 per-user 冷却（如 300 秒）与「仅在频道 0→1 人时播放」开关 |
| S-3 | `_trace()` 在事件循环里同步 `stat` + 写入 + 手写轮转 | `main.py:864` | 日志变大后每次事件都有磁盘 IO，拖慢事件处理 | 换成 `logging` + `RotatingFileHandler(maxBytes=1MB, backupCount=1)` 或 `QueueHandler` |
| S-4 | 接口无重试、无退避、无 `User-Agent` | `main.py` `KookApi.request()` | KOOK 偶发 5xx / 429 时直接失败；keep-alive 失败只记 debug | 幂等接口做 2 次指数退避；429 单独处理 |
| S-5 | 启动不清理残留语音连接 | `main.py:661` `initialize()` | 上次崩溃留下的连接让首次 `join` 失败并多等 3 秒 | 启动时按需查一次 `/voice/list`（做成开关） |
| S-6 | 队列只有 1 个位置，新请求会顶掉已排队请求 | `main.py:645`、`_enqueue()` | 试听与事件共用队列，可能互相顶掉且用户无感知 | 改有界 `deque` + 明确策略；试听走独立通道并回执「已排队」 |
| S-7 | 链接音频的坏内容会被写进缓存 | `main.py:1673` `_prepare_url_audio()` | HTML 伪装的 `.mp3` 现在会在加入频道前被 B-4 拦下，但坏文件永久留在 `audio_cache/`，每次播放都报错且不会重新下载 | 下载后立即 `ffprobe` 校验，失败则不写缓存并提示 |

---

## 三、性能与维护（🟡 / 🔵）

| 编号 | 问题 | 位置 | 建议 |
| --- | --- | --- | --- |
| M-1 | `ffprobe` 结果没有缓存，播放/状态/上传各跑一次 | `probe_audio_duration()`、`_play_welcome()`、`cmd_status()` | 按 `(path, mtime, size)` 缓存到字典，配置变更时清空 |
| M-2 | 目录扫描无缓存：`_candidate_dirs` / `_auto_audio_path` / `_find_audio_by_name` | `main.py:740`、`main.py:1493`、`main.py:1473` | 缓存 + TTL（如 30 秒）；`get_astrbot_data_path` 只解析一次 |
| M-3 | `audio_cache/`、`audio/`、`stream_cache/` 只增不减 | `_url_cache_path`、`_write_chat_audio`、`_prepare_stream_audio` | 按「最近 N 个 / 总大小上限」清理；删除时同步移出 `welcome_audio_files` |
| M-4 | 单文件约 2200 行，纯逻辑与网络/IO/命令混杂，`tests/` 为空 | `main.py` | ✅ 已拆分：`constants.py` / `utils.py` / `kook_api.py` / `ffmpeg.py` / `config.py` / `hooking.py` / `playback.py` / `resources.py` / `commands.py`；`tests/` 仍为空，纯函数 pytest 未做 |
| M-5 | 没有 CI | 仓库根目录 | GitHub Actions：`ruff check`、`ruff format --check`、`pytest -q` |
| M-6 | 版本号两处维护（`main.py:45` 与 `metadata.yaml`） | — | 运行时读 `metadata.yaml` 或构建时注入；`metadata.yaml` 补 `repo` / `tags` |

---

## 四、功能增强（🔵）

| 编号 | 方向 | 建议 |
| --- | --- | --- |
| F-1 | 编码参数 | `-application voip`、`-frame_duration 20`、低码率下 `-compression_level 10`；可选 `loudnorm`；`-b:a` 对 `bitrate` 做上限 clamp 并记录 |
| F-2 | 播放策略 | `max_duration` 截断、`loop` 次数、只欢迎新成员、多音频随机 |
| F-3 | 运维指令 | `/语音欢迎日志 [N]` 回传日志尾部、`/语音欢迎统计`（播放/失败/平均耗时）；用 `filter.command_group` 收拢子指令 |
| F-4 | 配置项 | `guild_whitelist`、`max_audio_seconds`、音量改 dB 语义、`source_fallback` 开关 |
| F-5 | 文档 | README 中的 40s/45s、2-3s、1MB 等常量与代码重复，改为集中表格（未做）；✅ 已新增 `CHANGELOG.md` |
| F-6 | Windows | spawn FFmpeg 时加 `creationflags=CREATE_NO_WINDOW`，服务化部署避免弹窗 |

---

## 五、回归测试清单

1. 插件加载：日志出现「插件加载」「已挂接 KOOK 适配器」。
2. `/语音欢迎`：链接来源未缓存时秒回，不出现长时间等待。
3. `/语音欢迎来源 url|path|auto`：重启后来源保持切换后的值。
4. 管理面板上传 / `/语音欢迎上传` / `/语音欢迎链接`：三种来源都能解析到文件。
5. 成员进入频道：加入 → 等待 → 播放（含尾部静音）→ 离开，全程只有一条 RTP 流。
6. `leave_delay=0`：不生成 `stream_cache/` 文件。
7. 第二次播放：命中 `stream_cache/` 缓存，日志无「合成尾部静音」。
8. 拔网线：`/语音欢迎` 仍能秒回状态。
9. 播放中重载插件：`/voice/list` 为空，无残留连接。
10. 抓包：下载音频的请求不带 `Authorization`。
11. B-1：重载 KOOK 适配器，进入频道只播放一次。
12. B-2：`/voice/list` 有两条记录时，日志只释放一条。
13. B-3：配置目录只读，指令回复「配置未能写入磁盘」。
14. B-4：损坏音频不会触发 `/voice/join`。
15. B-5：`start_delay=10` + 60 秒音频，日志每 40 秒出现保活。
16. B-6：播放中把机器人踢出频道，日志立即出现「中止本次推流」。
17. B-7：1 秒内连续上传两次同名文件，目录出现两个文件。
18. B-8：播放中卸载插件，`/voice/list` 为空。

---

## 六、待用户测试确认

以下改动已完成代码，但按项目约定未执行编译 / 测试，需要用户在真实环境确认：

| 改动 | 确认要点 |
| --- | --- |
| `main.py` 拆分为 9 个模块（mixin 组合） | 插件能正常加载、无 ImportError；日志出现「插件加载」「已挂接 KOOK 适配器」；`/语音欢迎` 状态正常；成员进入频道能完整走完加入 → 播放 → 离开；重载插件后不重复播放 |

对应回归项见「五、回归测试清单」第 1、2、5、9、11 条。

---

## 七、变更记录

| 版本 | 时间 | 内容 |
| --- | --- | --- |
| 未发布 | — | 拆分 `main.py` 为 9 个模块；新增 `CHANGELOG.md`；补 `AGENTS.md` 项目约束 |
| v1.4.3 | 2026-10-25 | 修复 B-1 ～ B-8：回调泄漏、误踢残留连接、配置静默失败、坏音频空 join、保活窗口、被踢中止、上传覆盖、卸载残留 |
| v1.4.2 | 2026-10-24 | 修复 P0-1 ～ P0-4：来源指令落盘、无鉴权下载、单条 RTP 流尾部静音、状态只读解析 |

# Codex 语音助理

本机代码每 3 秒轮询 Codex 的明确状态记录；出现新结果或结构化用户问题时，让 DeepSeek Harness 决定中文提醒内容，通过 `speak_notification` 工具提交给本机执行器，再调用火山引擎 TTS 并用 `afplay` 播放。

## 安装（macOS）

需要 Python 3.11 或更新版本、已安装的 DeepSeek Harness 桌面版，以及已开通对应资源和音色的火山 TTS 凭据。默认复用 Harness 账号登录，也支持单独配置 DeepSeek API Key。

下载或克隆本仓库后运行：

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
codex-voice configure --account --api-key
codex-voice doctor
codex-voice test
codex-voice install-service
```

`configure --api-key` 通过隐藏输入提示读取凭据；不要把密钥直接写到命令行或仓库。新终端可以先激活虚拟环境，或使用 `.venv/bin/codex-voice`。后台服务使用安装时的 Python 绝对路径，运行期间请保留项目及虚拟环境所在目录。

## 使用

```bash
codex-voice status
codex-voice events
codex-voice pause
codex-voice resume
codex-voice test
codex-voice doctor
```

暂停只关闭语音，继续记录事件。恢复后过时的完成通知会丢弃；仍待处理的问题会重新核实。测试命令会实际调用模型、TTS，并播放一次；`test --no-play` 只验证合成。

后台由用户级 launchd 管理，登录后运行，关闭 Harness 窗口不影响监听。

```bash
codex-voice install-service
codex-voice uninstall-service
```

移除服务会保留配置和通知记录。

## 已实现的状态范围

| 状态来源 | 可以识别 | 边界 |
| --- | --- | --- |
| 桌面 Codex 的本机 rollout 日志 | 明确的本轮结束记录；结构化 `request_user_input` / `request_user_input_async` 提问及对应回答；明确中断 | 不能把文件停止更新当作完成；嵌在任意代码字符串里的提问调用不做猜测解析 |
| 已运行的 Codex daemon | 该 daemon **实际加载**的任务，以及 `waitingOnApproval` / `waitingOnUserInput` 标记、明确完成或失败 | 不启动、恢复或接管任务；`notLoaded` 不代表桌面任务结束 |

**当前桌面应用的审批弹窗未能通过独立后台接口读取。**桌面工具 socket 有宿主进程校验，后台进程无法直接使用；本项目保持该校验，不注入或修改 Codex。daemon 的审批状态已通过真实 Unix WebSocket 协议测试，但这不等于桌面审批弹窗已覆盖。

普通最终回复中的“需要你选择”会随轮次结束一同交给 DS 总结。没有明确结束或结构化提问证据时，程序保持静默。本轮结束不等于整个任务成功。

## 通知行为

- 首次启动建立历史基线，历史完成任务不播报。
- 事件、文件游标、队列与播报状态保存到本机 SQLite，重复事件不会重复入队。
- 完成事件等待 8 秒，合并窗口内的多项结果；阻塞问题与审批优先。
- 同一待处理事项播报一次，5 分钟后最多补充提醒一次。
- 模型生成后、合成后重新读取事件状态；已回答或已进入下一轮的事项取消播报。
- 实时源失联时保持未知，不把过期标记当作待处理事实。
- 模型失败使用保守固定文案；TTS 失败有限重试，最终只播放本机提示音，不声称语音送达。
- 播放中进程丢失或播放器异常时记录 `uncertain`，不自动重播可能已经响过的语音。
- 音频播放后删除。服务日志轮转，错误日志不含密钥或供应商响应正文。

## 本机配置

普通设置：`~/.config/codex-voice/config.json`。

凭据：`~/.config/codex-voice/secrets.json`，目录权限 0700、文件权限 0600；也支持 `VOLC_TTS_API_KEY` / `DEEPSEEK_API_KEY`。不要把凭据加入仓库或命令参数。

```bash
codex-voice configure --api-key
codex-voice configure --speaker zh_female_vv_uranus_bigtts --resource seed-tts-2.0
```

默认复用 Harness 已有账号登录。专用 `codex-voice` profile 恢复官方账户服务，与桌面 profile 分开；不用提取或复制账户 token。如果要改用 API Key：

```bash
codex-voice configure --deepseek-key
codex-voice configure --account  # 切回已有 Harness 账户
```

可调整 `call_name`、`volume`（0—1）、`poll_seconds`、`merge_seconds`、`quiet_hours`。例如 `"quiet_hours": [["23:00", "08:00"]]`，时间采用本机时区；默认无免打扰时段。后台每轮重新加载配置。

DS 只接收脱敏的标题、事件证据和必要摘要；链接、常见凭据、UUID、本机路径会被替换。通知 Agent 在作用域内只暴露 `speak_notification`，启动时审计工具目录。事件内容作为数据，不允许它执行任务或回答审批。火山只接收最终播报文字。

本机状态和日志：`~/Library/Application Support/CodexVoice/`。`health.json` 明确报告接入范围；`events` 能区分 `played`、`failed`、`cancelled` 和 `uncertain`。

## 开发和验证

```bash
python3 -m venv .venv
.venv/bin/pip install -e .
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python tests/live_smoke.py  # 真实模型和 TTS，会播放一条模拟任务提醒
```

外部模型与 TTS 作为测试边界替换；游标、文件解析、队列、SQLite、异步交接及 Unix WebSocket 使用真实实现。`live_smoke.py` 通过生产服务入口监听隔离的模拟 Codex 日志，并检查播报和重启回读，不改动用户任务文件。

官方接口参考：[Harness headless](https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/bundle/headless/README.md)、[Codex app-server 源码](https://github.com/openai/codex/tree/main/codex-rs/app-server)、[字节 TTS 示例](https://github.com/bytedance/agentkit-samples/blob/main/skills/byted-text-to-speech/scripts/text_to_speech.py)。

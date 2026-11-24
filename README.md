# Codex 语音助理

本机代码每 3 秒轮询 Codex 的明确状态记录；出现新结果或结构化用户问题时，通过已安装的 Codex CLI 和 ChatGPT 订阅登录生成中文提醒，再调用火山引擎 TTS 并用 `afplay` 播放。也可在设置中切换到 DeepSeek Harness。

## 安装（macOS）

需要 Python 3.11 或更新版本、已登录 ChatGPT 账号的 Codex CLI，以及已开通对应资源和音色的火山 TTS 凭据。Codex 模式不需要 OpenAI API Key，但仍需联网并占用账号的 Codex 用量。已在 Codex CLI 0.170.0 验证；CLI 需要支持 `--ephemeral`、`--ignore-user-config` 和 `--output-schema`。

下载或克隆本仓库后运行：

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
codex login                    # 已登录可跳过
codex-voice configure --provider codex --api-key
codex-voice doctor
codex-voice test
codex-voice install-service
```

`configure --api-key` 通过隐藏输入提示读取凭据；不要把密钥直接写到命令行或仓库。新终端可以先激活虚拟环境，或使用 `.venv/bin/codex-voice`。后台服务使用安装时的 Python 绝对路径，运行期间请保留项目及虚拟环境所在目录。

## 图形界面与菜单栏

```bash
codex-voice install-ui  # 使用系统 swiftc 构建原生菜单栏应用
codex-voice ui          # 打开配置窗口
```

应用安装到 `~/Applications/Codex Voice.app`，登录后可从顶部菜单栏打开。紧凑窗口只保留语音设置、提醒规则和播报记录；连接与高级选项默认折叠。可修改称呼、音色、音量、文案生成方式、模型、提醒间隔和免打扰时段。配置保存后由现有服务热加载。凭据只显示配置状态，可以通过独立的密码输入框更新。

菜单栏提供暂停 / 恢复提醒、试听声音和查看记录。关闭窗口保留菜单栏和后台监听；退出菜单栏只关闭界面，不停止监听服务。显式试听共用现有播放队列，即使任务提醒暂停或处于免打扰时段，用户主动点击试听仍可播放。

设置页面只监听本机回环地址，通过每次启动生成的令牌访问；接口校验 Host、Origin 和允许修改的配置项。访问令牌保存在本机私有状态目录，不会进入仓库。页面和装饰资源都在本机，无外部字体、脚本或分析请求。新版采用青绿水墨、米白宣纸与描金细线，资产来源见 `codex_voice/static/assets/ASSETS.md`。

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

后台由用户级 launchd 管理，登录后运行，关闭设置窗口不影响监听。

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

普通最终回复中的“需要你选择”会随轮次结束一同交给所选模型总结。没有明确结束或结构化提问证据时，程序保持静默。本轮结束不等于整个任务成功。

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

默认复用 `codex login` 的 ChatGPT 登录状态，不提取或复制账户 token；强制 ChatGPT 认证，不回退到 API Key。模型留空时仅跟随本机 `~/.codex/config.toml` 的 `model` 字段，也可在连接设置中指定：

```bash
codex-voice configure --provider codex
codex-voice configure --codex-model ""  # 跟随本机模型
```

每次生成使用独立临时目录、`--no-daemon --ephemeral --ignore-user-config --ignore-rules`、只读沙箱和结构化输出。不加载日常 MCP / hooks 配置，关闭 shell、应用和浏览器等功能，拒绝含非文本操作的结果。不保存 Codex 会话日志，因此不会被监听器递归播报。生成失败时沿用保守固定提醒。

如需使用 DeepSeek，需要另行安装 Harness。其专用 `codex-voice` profile 与桌面 profile 分开，只暴露并审计 `speak_notification` 工具：

```bash
codex-voice configure --account       # 使用已有 Harness 账户
codex-voice configure --deepseek-key  # 或使用 DeepSeek API Key
```

可调整 `call_name`、`volume`（0—1）、`poll_seconds`、`merge_seconds`、`quiet_hours`。例如 `"quiet_hours": [["23:00", "08:00"]]`，时间采用本机时区；默认无免打扰时段。后台每轮重新加载配置。

所选文案模型只接收脱敏的标题、事件证据和必要摘要；链接、常见凭据、UUID、本机路径会被替换。事件内容作为数据，不允许模型执行任务或回答审批。火山只接收最终播报文字。

本机状态和日志：`~/Library/Application Support/CodexVoice/`。`health.json` 明确报告接入范围；`events` 能区分 `played`、`failed`、`cancelled` 和 `uncertain`。

## 开发和验证

发布前完整检查与能力边界见 [TESTING.md](TESTING.md)。

```bash
python3 -m venv .venv
.venv/bin/pip install -e .
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python tests/live_smoke.py  # 真实模型和 TTS，会播放一条模拟任务提醒
```

外部模型与 TTS 作为测试边界替换；游标、文件解析、队列、SQLite、异步交接及 Unix WebSocket 使用真实实现。`live_smoke.py` 通过生产服务入口监听隔离的模拟 Codex 日志，并检查播报和重启回读，不改动用户任务文件。

官方接口参考：[Codex CLI](https://developers.openai.com/codex/cli/)、[Codex 配置](https://developers.openai.com/codex/config-reference/)、[Harness headless](https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/bundle/headless/README.md)、[Codex app-server 源码](https://github.com/openai/codex/tree/main/codex-rs/app-server)、[字节 TTS 示例](https://github.com/bytedance/agentkit-samples/blob/main/skills/byted-text-to-speech/scripts/text_to_speech.py)。

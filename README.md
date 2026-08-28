# 微信群重要信息 Agent

监听指定微信群，将新消息、近期历史、群聊专属提示词和长期记忆交给 OpenAI-compatible 上游。模型可以通过 tool call 获取更长历史、读写记忆、查询近期转发、创建/查看/取消日程以及转发重要信息。

## 运行模型

服务层长期运行在 `while True` 中。监听回调先持久化消息，再放入聚合器，不等待 AI；连续消息默认聚合 8 秒，然后进入单独的 Agent 工作线程。单批最多 50 条，避免高流量群或重启恢复时一次请求撑爆模型上下文。

每批消息内部也是一个 Agent 循环：

1. 首次请求携带近期历史、长期记忆、本次新消息和群聊提示词。
2. 模型可以连续调用工具，工具结果会追加回上下文。
3. `forward_important` 只执行转发，不终止循环。
4. 模型完成转发、记忆和日程等操作后，必须单独回复：

   ```text
   FINAL_DECISION: {"important": true, "forwarded": true, "reason": "最终判定理由"}
   ```

5. 只有上述回复被判定为正常终止。`max_steps` 是异常保护，不是正常流程。

## 已实现的工具

- `get_chat_history`：最多补取 200 条历史，可用 offset 分页。
- `get_memory` / `remember`：读取或维护当前群的长期记忆。
- `get_recent_forwarded`：检查近期通知，帮助避免语义重复。
- `schedule_reminder`：创建明确的未来提醒。
- `list_schedules` / `cancel_schedule`：管理当前群创建的日程。
- `forward_important`：将摘要放入串行微信发送队列，但不终止 Agent。
- `ask_forward_target`：只有在判断确实缺少用户偏好或背景时，向 `forward_to` 询问；答复会写入长期记忆并触发原消息重新评估。

SQLite 保存长期记忆、转发事件、日程、Agent 审计记录以及持久化 inbox/outbox。微信发送始终由一个线程串行执行；每个目标联系人独立记录投递状态，某个联系人失败不会让已经成功的联系人重复收到通知。

微信 GUI 发送采用 at-most-once 语义：如果 UI 已执行发送但微信数据库在校验窗口内尚未确认，系统将该投递标记为“已接受但未验证”，不会再次操作 UI。只有在发送动作本身没有完成时才进入重试，以避免相同通知重复发送。

监听消息会先写入 inbox，再进入内存聚合器；Agent 正常给出 `FINAL_DECISION` 后才标记完成。进程意外退出时，未完成消息会在下次启动自动重放。重要事件记录与 outbox 任务在同一 SQLite 事务中创建，避免出现“记录成已转发，但发送任务实际丢失”的状态。

## 安装

要求 Windows 10/11、Python 3.10+、已登录的微信 4.1.12+，以及 `wechatauto-replica>=1.1.9`。

```powershell
python -m pip install -e ".[web]"
Copy-Item config.example.json config.json
```

项目没有强制依赖 OpenAI Python SDK，使用 Python 标准库请求 OpenAI-compatible `/chat/completions` 接口。

## 配置

编辑 `config.json`：

- `ai.base_url`：通常以 `/v1` 结尾；程序会追加 `/chat/completions`。
- `ai.model`：上游实际支持的模型名。
- `groups[].id`：传给 wechatauto 的群名或内部 username。
- `groups[].forward_to`：一个或多个微信联系人。
- `groups[].system_prompt_file`：相对配置文件所在目录解析。
- `importance_threshold`：低于此分数时，程序拒绝执行转发工具。

先检查 JSON 配置：

```powershell
python -m wechat_agent.main --config config.json --check
```

不确定群聊标识时，在微信登录状态下列出最近会话：

```powershell
python -m wechat_agent.main --config config.json --list-sessions
```

## 启动

PowerShell 当前窗口设置密钥并运行：

```powershell
$env:AI_API_KEY = "你的密钥"
python -m wechat_agent.main --config config.json
```

保持微信登录且桌面未锁定。消息读取可在后台进行，但转发依赖微信桌面 UI。

## WebUI

WebUI 已内嵌到 Agent 服务中，只需运行一个进程：

```powershell
python -m wechat_agent.main --config config.json
```

浏览器打开 <http://127.0.0.1:8765>。管理界面包含：

- 从微信数据库中的完整群聊目录选择监听群，不需要手动输入群标识。
- 按群配置 `forward_to`、待确认询问对象、重要性阈值和 Prompt。
- 编辑 Prompt 文件和每个群的长期记忆。
- 查看并人工回答待确认事项；答复写入记忆后，运行中的 Agent 会自动取回并重新评估原消息。
- 查看 Agent 决策、日程和失败队列。

保存监听规则或 Prompt 后会立即热更新：新增或移除群监听、`forward_to`、澄清答复监听、阈值和 Prompt 都无需重启；手工编辑 `config.json` 或当前引用的 Prompt 文件也会在约 1 秒内自动载入。已经进入 Agent 队列的消息继续使用入队时的规则，新消息使用最新规则。

Web 服务默认只监听 `127.0.0.1`，不会直接暴露到局域网。维护场景可用 `--web` 仅启动管理界面（不监听消息），或用 `--agent-only` 仅启动 Agent。

如果需要修改 Vue 页面：

```powershell
Set-Location webui
npm.cmd install
npm.cmd run dev

# 生成由 Python Web 服务托管的生产文件
npm.cmd run build
```

## 状态与故障管理

以下命令只读取本地 SQLite，不会启动监听或调用 AI：

```powershell
# inbox、outbox、逐联系人投递、日程的状态数量
python -m wechat_agent.main --config config.json --status

# 某个群的长期记忆和全部日程
python -m wechat_agent.main --config config.json --memory "群聊标识"
python -m wechat_agent.main --config config.json --schedules "群聊标识"

# 最近 50 次 Agent 运行结果
python -m wechat_agent.main --config config.json --runs 50

# 查看死信
python -m wechat_agent.main --config config.json --failed

# 将失败 inbox 和投递重新置为待处理；随后重启主服务
python -m wechat_agent.main --config config.json --retry-failed
```

单个微信目标连续失败 5 个投递周期后进入 `failed`，不会无限占用发送线程。使用 `--failed` 查看错误，确认微信窗口和联系人配置正常后再执行 `--retry-failed`。

## 安全边界

群消息被视为不可信数据。基础提示词明确禁止群消息修改 Agent 规则或诱导调用工具。API Key 只从环境变量读取，不写入配置、SQLite 或日志。正式使用前建议先将 `forward_to` 设为“文件传输助手”观察判断效果。

## 测试

测试使用假微信和假 AI，不会操作真实微信：

```powershell
python -m unittest discover -s tests -v
```

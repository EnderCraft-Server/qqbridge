# qqbridge

QQ ↔ MCP 桥：把 QQ 群聊接进 Agent，**没有消息时不产生任何模型调用**。

## 为什么做这个

常见的群聊机器人实现用「让模型等待」的方式驱动：

```
Agent → wait(timeout=180)   # 阻塞，没消息也干等，照样烧 token
     → 有消息 → 回复 → 再 wait → …
```

外部 Agent 一停整条链路就断，空闲时也在消耗上下文。

qqbridge 把**等待下沉到服务层**：事件在本地排队（零模型成本），
只在真的有事时才唤醒模型。

```
OneBot WebSocket（常驻，断线自动重连）
   └─► 事件总线（环形缓冲 + 自增 id）
          ├─ pending_high  @我
          ├─ pending_mid   命中关键词
          └─ pending_low   冷却期内的闲聊
                 │
                 ▼
        MCP 工具全部「立刻返回」
        poll_events / list_pending / mark_processed
```

## 架构

**bot 自带模型，独立于任何 Agent 宿主。**

```
QQ 客户端
   └─ OneBot v11（SnowLuma / NapCat / Lagrange）   HTTP :3000   WS :3001
          └─ qqbridge                               MCP :18900   控制台 :18900
                 ├─ 自带模型：直接调 OpenAI 兼容 API，自己接话
                 └─ MCP 工具：外部 Agent（Codex / Claude Code / 任意 MCP 客户端）可接入
```

两条路并存、共用同一个事件队列：

| | 谁驱动 | 需要外部 Agent |
|---|---|---|
| **自动接话** | qqbridge 进程内循环调自己的模型 | 否 |
| **MCP 工具** | 外部 Agent 主动调用 | 是 |

### 为什么不用「让模型等待」

常见实现是让 Agent `wait(timeout=180)` 阻塞着等消息 —— 空转也烧 token，
Agent 一停链路就断。qqbridge 把等待下沉到服务层：事件在本地排队（零模型成本），
只在真有消息时才醒来处理。

## 快速开始

```powershell
pip install -r requirements.txt
copy .env.example .env      # 填写 OneBot 地址与 Token
python run.py
```

控制台：<http://127.0.0.1:18900/?token=你的MCP_TOKEN>

## 让 bot 自己说话（自带模型）

`.env` 里配三项即可，**不依赖任何外部 Agent**：

```dotenv
LLM_API_BASE=https://api.deepseek.com    # 任何 OpenAI 兼容端点
LLM_API_KEY=sk-...
LLM_MODEL=deepseek-chat
AUTO_REPLY=true                          # false = 只做工具，不主动发言
SYSTEM_PROMPT_FILE=data/system_prompt.md # 人格文件，改完热加载
```

**人格**：直接编辑 `data/system_prompt.md`（或控制台里的「人格 / System Prompt」文本框）。
文件内容是完整的 system prompt，可以随便写人设。程序会要求模型以 JSON 回
`{"reply": "...", "reason": "..."}`，`reply` 为空即沉默 —— 所以**沉默是模型可以主动选的**，
不是异常。

调用统计（次数 / tokens）在控制台的「模型」卡片里实时显示。

## 配置

见 `.env.example`。关键项：

| 变量 | 说明 |
|---|---|
| `LLM_API_BASE` / `LLM_API_KEY` / `LLM_MODEL` | bot 自己的模型凭据 |
| `AUTO_REPLY` | 是否让 bot 主动接话 |
| `SYSTEM_PROMPT_FILE` | 人格文件路径 |
| `ONEBOT_HTTP` / `ONEBOT_WS` | OneBot 的 HTTP 与 WebSocket 地址 |
| `ONEBOT_TOKEN` / `ONEBOT_WS_TOKEN` | 两者常常**不是同一个** token |
| `MCP_TOKEN` | 本服务的访问令牌（MCP 与控制台共用） |
| `OWNER_IDS` | 只有这些 QQ 能触发写操作；留空则全部拒绝 |
| `ALLOW_SEND` / `ALLOW_MANAGE` | 发送 / 群管理总开关，默认关 |
| `WATCH_GROUPS` | 只监控这些群；留空=全部。范围外的群只落库不唤醒 |

## MCP 工具

**只读**：`get_status` `list_groups` `list_members` `get_history` `local_history`
`search_messages` `poll_events` `list_pending` `mark_processed` `audit_tail`
`list_keywords` `get_scheduler`

**写操作**：`send_message` `send_image` `manage_group`（禁言/解禁/踢人/改名/名片/全员禁言/退群）
`set_keywords` `set_scheduler` `set_control` `set_profile`

**单聊与 QQ 空间**（SnowLuma 扩展接口，非 OneBot 标准）：
`send_private` `set_profile`（改昵称）`qzone_list` `qzone_feeds` `qzone_publish`（发说说）
`qzone_delete` `qzone_like` `qzone_comment`

> ⚠️ 这些扩展接口是 SnowLuma 独有的，换 OneBot 实现时须重新核对，
> 代码里只做了「缺参数回显」式的只读探测确认。

写操作要求 `actor` 在 `OWNER_IDS` 内，并携带唯一 `idempotency_key`，全部记入审计表。

## ⚠️ 游标陷阱（已知问题，使用前必读）

`poll_events(cursor)` 会**先推进游标再取消息**，而 `mark_processed` **只进不退**：

| 缺陷 | 后果 |
|---|---|
| 传较大的 cursor 即清空队列 | 静默丢弃中间所有消息 |
| `/api/pending` 的 cursor 取所有事件最大 id（含自己的消息） | 自己发一条就会跳过别人插在中间的消息 |
| 环形缓冲 `maxlen=500` 满即丢弃 | 最旧事件永久消失，无告警 |

**安全用法**：只用服务器返回的 cursor；判断"有没有新消息"用 `list_pending` 或
`/api/events`，不要只看 `poll_events` 的空返回；发现游标跳跃立刻停止。

## 控制台

`http://127.0.0.1:18900/?token=<MCP_TOKEN>`

可配置：模型（API Base / Key / 模型名 / 自动接话开关）、**人格 system prompt**、
巡检间隔、权限开关、Owner 白名单、监控群、关键词表、运行模式（auto / stopped / manual）。
实时显示消息流、审计记录、token 用量。设置落盘到 `.env`、`data/`。

## 安全约定

- 写操作只认 `OWNER_IDS`，昵称与自称不算数
- 幂等键防重放；审计表记录「谁发起、做什么、结果」
- 群管理前核对目标 role（owner 不可禁言），失败如实上报，不谎报成功
- 不读取宿主机任何密钥或凭据

## License

MIT
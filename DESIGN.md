# QQ 桥 (qqbridge) — 自研 OneBot + MCP

替代 Tulpa 的最小可用实现：直接吃 SnowLuma 的 OneBot v11 接口，
把 QQ 常用能力包成 MCP 工具，**并且不用 wait 阻塞循环**。

## 核心问题：为什么 Tulpa 会一直 wait

Tulpa 的持续群聊协议是「拉模型」：
```
Agent → wait_chat_messages(timeout=180)  # 阻塞，没消息就干等
      → 返回一批 → 我回复 → send_chat_message
      → 再 wait …… 如此循环
```
每一轮 wait 都要占用一次完整的模型请求（几十秒到 3 分钟），
就算群里没人说话也在烧 token。而且外部 Agent 一旦停下，整条链路就死了。

## 解法：把「等待」从模型层下沉到服务层

```
OneBot WS 事件 ──► 本地事件总线（常驻，零模型成本）
                        │
                        ├─► 落盘 ring buffer（最近 N 条，带 event_id）
                        │
                        └─► 按规则触发：@我 / 关键词 / 冷却时间到
                                  │
                                  ▼
                          MCP resource 推送（DSH 侧订阅）
                          或 Agent 用 tools/list_pending 主动拉
```

关键点：**服务端一直在线**，模型只在「真有事」时才被唤醒。
Agent 侧的工具调用变成**非阻塞**的：

| 工具 | 语义 |
|---|---|
| `poll_events(cursor, max_wait)` | 立刻返回；最多等 max_wait 秒，默认 0（不等） |
| `list_pending()` | 只返回待处理队列，绝不阻塞 |
| `send_message(...)` | 立刻发 |
| `mark_processed(ids)` | 标记已处理，推进游标 |

这样即使 Agent 每隔几分钟才醒一次，也不会漏消息（事件都在服务端排队）。

## 目录结构

```
qqbridge/
  qqbridge/
    __init__.py
    config.py         # .env 读取
    onebot.py         # OneBot v11 HTTP + WS 客户端
    bus.py            # 事件总线：ring buffer + 订阅 + 触发规则
    store.py          # SQLite：消息、成员、别名、待办
    tools.py          # MCP 工具定义与实现
    server.py         # MCP Streamable HTTP 服务
  tests/
  run.py              # 入口
  .env.example
  requirements.txt
```

## MCP 工具清单（第一版）

**只读**
- `get_status` — 桥与 OneBot 连接状态、账号、队列深度
- `list_groups` — 群列表
- `list_members(group_id)` — 群成员（含 role）
- `get_history(group_id, count)` — 最近消息
- `search_messages(query)` — 本地索引检索
- `poll_events(cursor, max_wait)` — 非阻塞取事件
- `list_pending()` — 待处理队列

**写操作（需权限开关）**
- `send_message(group_id, text, reply_to)` — 发文本
- `send_image(group_id, path_or_url)` — 发图
- `set_group_ban(group_id, user_id, seconds)` — 禁言/解禁
- `kick_member(group_id, user_id)` — 踢人
- `set_group_name(group_id, name)` — 改群名
- `set_group_card(group_id, user_id, card)` — 改群名片
- `set_group_whole_ban(group_id, enable)` — 全员禁言
- `set_group_leave(group_id)` — 退群

## 安全设计（吸取 Tulpa 的教训）

1. **owner 白名单**：写操作只接受配置里列的 QQ 号发起的指令；
   日志记录「谁要求、执行什么、结果如何」。
2. **幂等键**：每个写操作带 idempotency_key，重试不重复执行。
3. **审计表**：SQLite `audit(id, at, actor, action, target, params, state, result)`。
4. **默认只读**：send/manage 分别开关，默认关。
5. **不碰密钥**：不读宿主机的 .env / 凭据 / 密钥文件，不做任何"帮我找 token"这类事。

## 长期运行策略（用户要求：不要一直 wait）

- 服务端常驻，事件不丢
- 触发器分级：
  - `mention`：被 @ → 立刻入待处理队列，优先级高
  - `keyword`：命中关键词表 → 入队
  - `cooldown`：距上次发言 > N 秒且有新消息 → 低优先级入队
  - `silent`：都不满足 → 只记录，不唤醒模型
- Agent 侧建议节奏：醒一次 → `list_pending` → 处理 → `send_message` → 再次挂起
  由 DSH 的 schedule/goal 机制定期唤醒，而不是死循环 wait

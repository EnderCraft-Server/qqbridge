# qqbridge

> 把 QQ 群聊接进 AI 的本地桥。**没有消息的时候，一次模型调用都不会发生。**

![license](https://img.shields.io/badge/license-MIT-blue.svg)
![python](https://img.shields.io/badge/python-3.10%2B-3776AB.svg)
![mcp](https://img.shields.io/badge/MCP-compatible-6E56CF.svg)
![onebot](https://img.shields.io/badge/OneBot-v11-12B7F5.svg)

跑在你自己电脑上的 QQ ↔ AI 桥，同时干两件事：

| 能力 | 是什么 | 需要外挂 Agent 吗 |
|---|---|---|
| **自动接话** | 进程内自带模型凭据，自己判断该不该说话、说什么 | 不需要 |
| **MCP 服务** | 一套工具，给 Codex / Claude Code / 任意 MCP 客户端用 | 需要 |

两条路共用同一个事件队列。空闲时消息只在本地排队，**零 token 消耗**。

---

## 目录

- [它解决什么问题](#它解决什么问题)
- [架构](#架构)
- [30 秒跑起来](#30-秒跑起来)
- [消息是怎么被处理的](#消息是怎么被处理的)
- [自带模型与人格](#自带模型与人格)
- [自动发说说](#自动发说说)
- [控制台](#控制台)
- [内置 Agent](#内置-agent)
- [MCP 工具](#mcp-工具)
- [配置速查](#配置速查)
- [目录结构](#目录结构)
- [安全约定](#安全约定)
- [测试](#测试)
- [常见问题](#常见问题)
- [License](#license)

---

## 它解决什么问题

大多数群聊机器人的驱动方式是这样的：

```text
Agent → wait(timeout=180)   # 阻塞着等，没消息也干等，照样烧 token
     → 有消息 → 回复 → 再 wait → ……
```

三个毛病：空闲时白烧 token；Agent 进程一停整条链路就断；等待期间还会一直占着上下文。

qqbridge 把**等待下沉到服务层**：

```text
OneBot WebSocket（常驻，断线自动重连）
   └─► 事件总线（环形缓冲 + 持久化自增 id）
          │
          ▼
   接话循环（每 1.5 秒看一眼，没新消息就接着睡）
          ├─ 管理员在跟它说话（@ / 私聊）→ 直接叫内置 Agent（带工具）
          └─ 其它 → 攒一段，整段交给模型判断要不要插一句
                 │
                 ▼
        直连 OpenAI 兼容 API，自己把话发回群里
```

MCP 工具全部「立刻返回」——`poll_events` 没消息就返回空，永远不会让调用方卡住等。

## 架构

```text
QQ 客户端
  └─ OneBot v11 实现（SnowLuma / NapCat / Lagrange）    HTTP :3000   WS :3001
       └─ qqbridge                                      MCP :18900   控制台 :18900
            ├─ 自带模型：直接调 OpenAI 兼容 API，自己接话
            ├─ 内置 Agent：自己能读写文件、跑命令（有边界，全程留痕）
            └─ MCP 工具：外部 Agent 可接入，共用同一个事件队列
```

内置 Agent 让它**不需要在电脑上再常开一个 Agent 宿主**；MCP 工具则是给已经有的宿主留的口子。

## 30 秒跑起来

```powershell
pip install -r requirements.txt
copy .env.example .env          # Linux/macOS 用 cp
# 编辑 .env：填 OneBot 地址与 Token、模型的 API Key、OWNER_IDS
python run.py
```

然后浏览器打开 <http://127.0.0.1:18900/>：

1. 第一次进会弹《使用须知》和《最终用户许可协议》，勾同意
2. 设置后台账号密码（PBKDF2-SHA256 加盐存储，不存明文）
3. 以后每次都登录进后台

> Windows 上可以直接双击 `start.bat`（里面写死了打包运行时的路径，按自己的 Python 改一下）。

## 消息是怎么被处理的

两条路，**完全分开**：

| 情况 | 走哪条路 | 怎么决定 |
|---|---|---|
| 管理员在跟它说话（@ 了它，或私聊） | **直接叫内置 Agent** | 不判断、不批处理，直接带工具去干 |
| 其它（群里所有人聊天） | **批量闲聊** | 攒一段，整段交给模型判断要不要插一句 |

中间那条边界是故意的：管理员在群里**没 @ 它**的时候也走批量闲聊，
否则「管理员说啥都当命令」，冒个泡都会被回。

### 闲聊：攒一段再看，不是一条一条接

人隔一会儿扫一眼手机，判断「整段值不值得插一句」，
而不是被每条消息牵着回一句。接话循环就是这个节奏：

```text
每 1.5 秒看一眼
  ├─ 没有未读 → 接着睡
  ├─ 有人 @ 我 → 不等，立刻处理
  ├─ 否则等「最后一条之后静默 3 秒」再出手
  │   （群里一直热闹、静不下来时最多等 45 秒，靠 max_batch_age 兜底）
  ├─ 把整段（未读 + 上下文）交给模型
  └─ 回一句 or 沉默 → 处理完整批才推进「已看过」标记
```

**标记只会落在「模型真的看过的那条」上。** 以前的游标是按条推进的，
一旦跳着处理就会把中间没处理的消息一起划掉 —— 现在结构上不可能发生：
一次就是处理一整段，处理完才推，推的位置就是这一批的最后一条。

被 @ 时如果模型没给话，会再问一次，还不行就发固定短句「在，你说。」
—— 被点名还一声不吭，用户只会以为机器人死了。

每分钟最多发 8 条（@ 不受限制）。

### 群里的控制命令

只有管理员能用，命令本身不入队、不触发模型，执行完直接回执：

| 命令 | 模式 | 群里收到的回执 |
|---|---|---|
| `/auto` | `auto`（默认） | 已开启对话自动接话功能，并接受所有agent请求 |
| `/start` | `manual` | 已切换为手动模式：只保留工具，不自动接话 |
| `/stop` | `stopped` | 已停止当前会话的自动接话功能，并已停止所有agent请求 |

非管理员发这些命令，只会收到一句「只有管理员能切换运行状态」。

### 沉默是允许的，但被 @ 不行

整段看下来没意思就别接（`reply` 留空），这是正常行为，不是故障，而且是**最常见的正确答案**。
但**被 @ 时必须回一句**：

- 模型沉默 / 输出解析失败 / 超时 / agent 链路报错 —— 都会带着「必须说一句」再问一次
- 再问还问不出话，就发固定短句「在，你说。」

被点名还一声不吭，用户只会以为机器人死了。

## 自带模型与人格

`.env` 里配三项就能说话，**不依赖任何外部 Agent**：

```dotenv
LLM_API_BASE=https://api.deepseek.com    # 任何 OpenAI 兼容端点
LLM_API_KEY=sk-...
LLM_MODEL=deepseek-chat
AUTO_REPLY=true                          # false = 只做工具，不主动发言
SYSTEM_PROMPT_FILE=data/system_prompt.md # 人格文件，改完热加载
```

**人格**直接编辑 `data/system_prompt.md`（或控制台「模型与人格」页的文本框）。
文件内容就是完整的 system prompt，人设随便写。

> `SYSTEM_PROMPT_FILE` 的回退链：配置的文件 → `prompts/default_persona.md` → 代码内置默认。
> 所以自己改了人格，升级仓库时不会被覆盖。

### 输出解析：五档兜底，绝不把 JSON 发进群

模型被要求以 JSON 回复。解析分五档，**任何一档都不会把原始 JSON 发到群里**：

| how | 场景 | 行为 |
|---|---|---|
| `ok` | 直接是合法 JSON | 正常取 `reply` |
| `fenced` | 被 markdown 代码块包着 | 剥壳后取 `reply` |
| `embedded` | JSON 前后夹了别的话 | 抠出第一个 `{...}` 块 |
| `plain` | 本来就是一句人话 | 当纯文本发 |
| `failed` | **看着像 JSON 但坏了（截断 / 畸形）** | **改判沉默**，并写审计 `decide_parse_failed` |

> 这条兜底是踩坑加的：带思考链的模型会把推理 token 也算进 `max_tokens`，
> JSON 被截断时早期版本会把整段 `{"reply": ...}` 原样发到群里。
> 用 reasoning 模型时建议 `LLM_MAX_TOKENS>=2048`。

调用统计（次数 / 输入输出 token）在控制台实时显示。

## 自动发说说

按节奏用模型现写内容，发到机器人自己的 QQ 空间。配置在 `data/qzone.json`，控制台可视化编辑。

| 项目 | 说明 |
|---|---|
| **频率** | 按间隔（每 N 小时）或按时刻（每天 `12:30` / `21:00` 这类） |
| **兜底** | 最短间隔 + 静默时段（例如 23:30–08:00 不发） |
| **主题** | 预设 8 个，可增删改、可只启用其中几个、可随机或轮转 |
| **时间窗** | 主题可带 `hours`：「深夜」只在 22:30–05:30 被选中，下午不会写出「凌晨了还醒着」 |
| **试口吻** | 控制台有「立刻发一条」，不影响排期 |

预设主题：日常随想 / 写代码 / Minecraft 服务器 / 深夜 / 玩梗吐槽 / 天气季节 / 游戏 / AI 自省。

> 走的是 SnowLuma 的扩展接口 `send_qzone_msg`，**不是** OneBot v11 标准接口。

## 控制台

<http://127.0.0.1:18900/> —— 需要账号密码登录。

| 页面 | 能干什么 |
|---|---|
| 概览 | 队列水位、消息流、待处理数量、缓冲丢弃计数 |
| 模型与人格 | API Base / Key / 模型名 / 自动接话开关、system prompt |
| 巡检与唤醒 | 巡检间隔、运行开关、**监控范围**、关键词表 |
| 自动说说 | 发帖频率、主题管理、发送记录 |
| 权限与安全 | 允许发送 / 群管理、Owner 白名单、改后台密码 |
| 内置 Agent | 文件读写与命令执行记录 |
| 日志与审计 | 谁发起、做什么、结果如何 |

几个值得单独说的设计：

**重启后不会一片空白。** 进程重启时会把库里的历史消息回填进环形缓冲，
所以控制台一开机就有东西看（只展示，不会把旧消息重新回复一遍）。

**监控范围是勾选框，不是让人手打群号。** 列表直接来自 OneBot 的 `get_group_list`，
显示群名、人数、群号；一个都不勾 = 不设限制，所有群都监控（范围外的群只落库，完全不唤醒）。

**表单不会被轮询冲掉。** 状态每 4 秒刷一次，但只回填「你没碰过」的字段，
碰过的字段会一直保留到你保存成功为止。

**群里的图片直接渲染成缩略图**（点开是大图灯箱），不再是一串 `[CQ:image,file=...]`。
图片经后端 `/api/image` 代理：只放行 QQ 图床（`*.qpic.cn` / `*.qq.com`）的 https 地址，
挡掉 IP 字面量与其它主机，避免这个接口变成任意 URL 的跳板。
图床链接带签名会过期，取不到时显示「图片已过期或取不到」而不是一直转圈。

## 内置 Agent

机器人自己能读写文件、执行命令，替代外部 Agent 宿主。

```dotenv
AGENT_ENABLED=true      # 关掉则只有聊天，没有文件 / 命令能力
AGENT_ROOT=             # 操作根目录，越界一律拒绝；留空 = 仓库根目录
AGENT_MAX_STEPS=60      # 单次任务最多几步（真正兜底的是 90 秒超时和死循环检测）
```

可用工具：`list_dir` `read_file` `write_file` `search_files` `run_command` `fetch_url`

> `fetch_url` 是专门加的：让模型查网页时不用拼 curl。实测同一个查询从 15+ 步降到 2 步、6 秒。

### 每一次读写都留痕

所有读取与写入逐行写进 `data/agent.log`（JSON Lines），同时进 SQLite 审计表：

```json
{"at":"2026-10-07 17:52:07","action":"write_file","target":".../data/x.txt","detail":{"bytes":12},"state":"SUCCEEDED"}
{"at":"2026-10-07 17:52:08","action":"run_command","target":"format C: /y","detail":{},"state":"DENIED","result":"命中危险命令黑名单"}
```

### 边界

| 约束 | 行为 |
|---|---|
| 路径越界 | 直接拒绝，只允许 `AGENT_ROOT` 之内 |
| 单次读 | ≤ 256 KB |
| 单次写 | ≤ 1 MB |
| 命令输出 | ≤ 64 KB |
| 命令超时 | 默认 15 秒，硬上限 60 秒 |
| 抓网页 | ≤ 512 KB，只允许 http/https |
| 危险命令 | 黑名单拦截（格式化 / mkfs / 关机重启 / diskpart / rm -rf 等），记 `DENIED` |
| 死循环 | 同一个工具调用用同样参数连续失败 3 次就停手，改问模型要一句人话 |
| 调用方 | MCP 侧 `agent_run` 仅限 `ADMIN_IDS` |

> 命令走 `asyncio.to_thread`，不会阻塞事件循环 —— 实测执行期间心跳最大间隔 0.111 秒。

## MCP 工具

端点：`http://127.0.0.1:18900/mcp/`（注意结尾斜杠），鉴权头 `Authorization: Bearer <MCP_TOKEN>`。

**读取与队列（13 个）**

| 工具 | 用途 |
|---|---|
| `get_status` | 连接状态、队列水位、配置概览 |
| `list_groups` / `list_members` | 群列表 / 群成员（含 role） |
| `get_history` / `local_history` | 从 OneBot 拉历史 / 读本地缓存 |
| `search_messages` | 本地消息全文检索 |
| `poll_events` / `list_pending` | 取事件（都立刻返回，永不阻塞） |
| `mark_processed` | 推进已处理游标 |
| `list_keywords` / `get_scheduler` / `get_control` | 读当前设置 |
| `audit_tail` | 最近的操作审计 |

**写操作（8 个）** —— 要求 `actor` 在 `OWNER_IDS` 内，且带唯一 `idempotency_key`

| 工具 | 用途 |
|---|---|
| `send_message` / `send_image` / `send_private` | 发文本 / 发图 / 单聊 |
| `manage_group` | `mute` `unmute` `kick` `rename` `card` `whole_ban` `leave` |
| `set_keywords` / `set_scheduler` / `set_control` / `set_profile` | 改配置 / 改昵称 |

**QQ 空间与 Agent（8 个）** —— 前 6 个是 SnowLuma 扩展接口，**非 OneBot 标准**

| 工具 | 用途 |
|---|---|
| `qzone_list` / `qzone_feeds` | 自己的说说 / 好友动态 |
| `qzone_publish` / `qzone_delete` | 发 / 删说说 |
| `qzone_like` / `qzone_comment` | 点赞 / 评论（好友的需带 `target_uin`） |
| `agent_run` / `agent_log` | 给内置 Agent 派活 / 查它的读写记录 |

> 这些扩展接口是 SnowLuma 独有的。换 OneBot 实现时必须重新核对，
> 代码里只做过「缺参数回显」式的只读探测确认。

## 配置速查

完整项见 [`.env.example`](.env.example)。最常改的几个：

| 变量 | 说明 |
|---|---|
| `LLM_API_BASE` / `LLM_API_KEY` / `LLM_MODEL` | 机器人自己的模型凭据 |
| `AUTO_REPLY` | 是否主动接话 |
| `SYSTEM_PROMPT_FILE` | 人格文件路径 |
| `ONEBOT_HTTP` / `ONEBOT_WS` | OneBot 的 HTTP 与 WebSocket 地址 |
| `ONEBOT_TOKEN` / `ONEBOT_WS_TOKEN` | 两者常常**不是同一个** token |
| `MCP_TOKEN` | MCP 与控制台共用的访问令牌 |
| `OWNER_IDS` | 只有这些 QQ 能触发写操作；留空则全部拒绝 |
| `ADMIN_IDS` | 能发 `/stop` `/start` `/auto`、能走 agent 路径；留空退回 `OWNER_IDS` |
| `ALLOW_SEND` / `ALLOW_MANAGE` | 发送 / 群管理总开关，默认关 |
| `WATCH_GROUPS` | 只监控这些群；留空 = 全部 |
| `COOLDOWN_SECONDS` | 自动触发接话的冷却秒数（0 = 不自动触发） |
| `RING_SIZE` | 环形缓冲大小（默认 500），满了会丢最旧的并计数 |

## 目录结构

```text
qqbridge/
├─ run.py                   入口（把仓库目录塞进 sys.path，不用装包）
├─ start.bat                Windows 一键启动
├─ .env.example             配置模板
├─ README.md                这份说明
├─ DESIGN.md                更细的设计笔记
├─ prompts/
│   └─ default_persona.md   自带人格（用户没配人格文件时的回退）
├─ tests/                   自包含测试脚本，见下方「测试」
├─ qqbridge/                包本体
│   ├─ server.py            FastAPI：MCP 端点 + 控制台 REST + 图片代理
│   ├─ ui.html              控制台单页（无构建步骤）
│   ├─ bus.py               事件总线：环形缓冲 + 持久化自增 id + 三级队列
│   ├─ autoreply.py         接话循环：全自动分流、限流、@ 兜底
│   ├─ agent.py             人格、历史、决策与输出解析兜底
│   ├─ agentloop.py         内置 Agent 的多步循环
│   ├─ agenttools.py        文件 / 命令 / 抓网页工具（带边界与留痕）
│   ├─ llm.py               OpenAI 兼容客户端（含 DeepSeek thinking 开关）
│   ├─ onebot.py            OneBot HTTP + 常驻 WebSocket（指数退避重连）
│   ├─ qzone.py             QQ 空间扩展接口
│   ├─ qzone_auto.py        自动发说说的调度与生成
│   ├─ auth.py              控制台鉴权 + 两份协议全文
│   ├─ scheduler.py         巡检节拍
│   ├─ control.py           auto / manual / stopped
│   ├─ commands.py          群内命令与回执文案
│   ├─ rules.py             关键词分级
│   ├─ store.py             SQLite 持久化
│   └─ config.py            .env 读取与回写
└─ data/                    运行时数据（已 gitignore）
    ├─ qqbridge.sqlite3     消息 / 成员 / 群 / 审计 / 幂等键
    ├─ system_prompt.md     人格（可改，热加载）
    ├─ qzone.json           自动说说配置与历史
    ├─ auth.json            后台账号（PBKDF2 加盐，无明文）
    ├─ agent.log            内置 Agent 的读写记录（JSONL）
    └─ scheduler.json       巡检设置
```

## 安全约定

- **写操作只认 `OWNER_IDS`**，昵称与自称不算数
- **幂等键防重放**；审计表记录「谁发起、做什么、结果如何」
- **控制台要登录**，会话只存在内存里，重启即失效；密码 PBKDF2-SHA256 加盐
- **群管理前核对目标 role**，群主不可被禁言；失败如实上报，绝不谎报成功
- **内置 Agent 的所有读写都在 `AGENT_ROOT` 内**，越界直接拒绝，且逐条留痕
- **提示词注入不生效**：要求以开发者身份下令、索要凭据、顶号之类的请求一律拒绝并留审计

控制台首访必须同意两份协议，要点：

1. **不保证没有 BUG**，因 BUG 造成的损失作者不赔
2. **若有人拿它做违法的事，开发者概不负责**，责任由使用者独立承担
3. 机器人及其相关创作的**所有权归属 EnderCraft**

## 测试

全部是自包含脚本：不联网、不碰仓库里的真实配置（临时目录里跑）。

```powershell
python tests/test_auth_qzone.py    # 门禁 / 协议 / 自动说说 / 图片代理白名单
python tests/test_console.py       # 控制台每个开关是不是「真的生效」
python tests/test_autoreply.py     # 批量闲聊、派活直连 Agent、被 @ 必回
python tests/test_agenttools.py    # 命令黑名单别误杀、卡住时别发废话
python tests/test_bus.py           # 事件总线：批量标记、启动回填、溢出计数
python tests/test_onebot.py        # WebSocket 事件流：断线退避、connected 不说谎
node   tests/ui_console.mjs        # 控制台前端脚本（假 DOM 里跑 ui.html）
python tests/test_readme.py        # 这份 README 有没有跟代码对不上
```

两个值得一提的守卫：

- `test_console.py` 会在开头给仓库真实的 `.env` 逐字节拍快照，结尾断言一个字节都没变 ——
  因为控制台接口会把设置写回 `.env`，测试一旦认错目录就会把线上配置冲掉
- `test_readme.py` 检查这份 README：提到的命令、环境变量、MCP 工具、目录结构、
  测试脚本是不是都真实存在，工具数量对不对得上。文档一漂就报错

## 常见问题

<details>
<summary><b>机器人一直不吭声，审计里全是 <code>auto_silent</code></b></summary>

先看控制台「模型与人格」页：`last_error` 是不是有值。
最常见的两个原因：`LLM_API_BASE` / `LLM_MODEL` 被改成了不可用的值（例如测试值）；
或者 API Key 余额不足。改回来立刻生效，不用重启。

如果 `last_error` 是空的，那就是模型真的选择了沉默 —— 看审计里的 `reason`，
它会写清楚为什么（例如「没人点名，也没有能自然接上的话题」）。
</details>

<details>
<summary><b>@ 了机器人还是不回</b></summary>

正常情况下不可能：被 @ 时如果模型不给话，会再问一次，还不行就发「在，你说。」。
如果仍然完全没反应，检查三件事：

1. 这个群在不在控制台的「监控范围」里（没勾的群只落库，不唤醒）
2. 运行模式是不是 `stopped`（群里发 `/auto` 恢复）
3. 审计里有没有 `auto_reply` 记录 —— 有记录说明发出去了，那就是 QQ 侧的问题
</details>

<details>
<summary><b>控制台里改了设置没反应</b></summary>

先确认那个页面显示的确实是你的新值（不是被刷回去了）。
如果值对但行为没变，多半是进程还跑着改动前的代码 —— 重启一次。

界面上的值来自后端，只有「保存」成功才会落盘；
顶部会弹提示，保存失败会直接说原因。
</details>

<details>
<summary><b>控制台里图片显示「图片已过期或取不到」</b></summary>

QQ 图床的链接是带签名的临时链，几小时后会失效。这是正常的，不是 BUG。
如果你需要长期留档，得在收到消息时就另存一份。
</details>

<details>
<summary><b>收不到某个群的消息</b></summary>

按顺序排查：OneBot 连接（控制台右上角是不是「QQ 已连接」）→ 监控范围有没有勾这个群
→ 群在不在 SnowLuma 的监听列表里。
</details>

## License

MIT

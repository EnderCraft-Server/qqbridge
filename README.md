# qqbridge

> 把 QQ 群聊接进 AI 的本地桥。**没有消息的时候，一次模型调用都不会发生。**

![license](https://img.shields.io/badge/license-MIT-blue.svg)
![python](https://img.shields.io/badge/python-3.10%2B-3776AB.svg)
![mcp](https://img.shields.io/badge/MCP-compatible-6E56CF.svg)
![onebot](https://img.shields.io/badge/OneBot-v11-12B7F5.svg)

跑在你自己电脑上的 QQ ↔ AI 桥。装上之后它能在群里当一个会看气氛的群友，
也能私聊着让它干活（读写文件、跑命令、查网页），还会定时往 QQ 空间发说说。

---

## 这份文档怎么读

不用从头看到尾。按你现在想干的事挑一章：

| 你现在想干嘛 | 翻到 |
|---|---|
| **第一次装，先把它跑起来** | [第 0~4 章](#第-0-章--先确认这些东西你都有)，照着抄就行 |
| 跑起来了，想让它的语气像人 | [第 5 章](#第-5-章--调教成你想要的样子) |
| 想让它帮我干活 | [第 6 章](#第-6-章--让它真的能干活) |
| 想让它自己发说说 | [第 7 章](#第-7-章--自动发说说) |
| 想把它接到 Codex / Claude Code | [第 8 章](#第-8-章--接到外部-agent) |
| 跑不起来 / 不吭声 / 报错 | [常见问题](#常见问题) |
| 想改代码 | [附录 A](#附录-a--它内部是怎么跑的) + [目录结构](#目录结构) |

前面四章是一条直线：**每一步都有「检查点」**，对上了再往下走，别跳。

---

## 第 0 章 · 先确认这些东西你都有

- 一台能一直开着的电脑（Windows / Linux / macOS 都行）
- **Python 3.10 或以上**
- **一个专门给机器人用的 QQ 号**
- **一个 OneBot v11 实现**（第 1 章装）
- **一个 OpenAI 兼容的模型 API**（DeepSeek 或者任何兼容端点）

> 为什么要专用 QQ 号：登录协议层有风控，拿主号去试容易被限制，很麻烦。

---

## 第 0.5 章 · 安装并配置此程序
> 本章可能是唯一由人类编写的。

首先从任意途径获取此仓库的代码。你可以从 Github 仓库上直接克隆，目前不提供单文件安装。  
克隆仓库后，先运行根目录下的 setup.bat，这会自动创建虚拟环境并且安装依赖。  
如果您遇到下载问题，请打开 setup.bat，在第三行的 `-i` 后面把网址换成其他的 Pypi 镜像站网址。  
运行完 setup.bat 后，请在 .env 文件中配置一些数据（参见第二章）。  
配置完成后，请运行根目录下的 start.bat 来启动此程序。

---

## 第 1 章 · 让 QQ 连上 OneBot

这一章最容易卡住，卡住的人 90% 是卡在这儿。

**qqbridge 不直接连 QQ，它连的是 OneBot。**
OneBot 是一个把 QQ 协议翻译成 HTTP / WebSocket 的中间层，你要先把它跑起来、
让它登着你的机器人号。

### 1.1 挑一个 OneBot 实现

| 实现 | 平台 | 说明 |
|---|---|---|
| SnowLuma | Windows | 本项目的 QQ 空间功能（发说说、点赞）就是按它写的，**优先选它** |
| NapCat | 全平台 | 基于官方客户端，比较稳 |
| Lagrange | 全平台 | 纯协议实现，不用挂 QQ 客户端 |

### 1.2 用机器人号登录

打开它，扫码头或者输密码，登进你的机器人 QQ。

### 1.3 开两个服务端

在它的配置里找到「HTTP 服务端」和「正向 WebSocket 服务端」，两个都开：

| 服务 | 端口填 | 作用 |
|---|---|---|
| HTTP 服务端 | 3000 | qqbridge 调接口**发**消息 |
| 正向 WebSocket 服务端 | 3001 | OneBot 把群消息**推**给 qqbridge |

两个各有一个 **token**，而且 **它们往往不是同一个** —— 都记下来，第 2 章要填。

### ✅ 检查点

打开 PowerShell 或终端，把下面这行的 token 换成你的 HTTP token：

```powershell
curl.exe http://127.0.0.1:3000/get_login_info -H "Authorization: Bearer 你的HTTPtoken"
```

**要看到**类似这样的输出：

```json
{"status":"ok","retcode":0,"data":{"user_id":123456789,"nickname":"你的机器人"}}
```

看到了 → 第 1 章过关。
报 401 → token 不对。连不上 → 服务端没开或者端口不是 3000。

> 这一步没过就**千万别**往下走。后面所有「收不到消息」的问题，八成都是这里没通。

### 1.4 让机器人号进一个群

用机器人号加一个群，实在没有就自己拉个测试群。
**第 4 章要用它验证「它到底会不会说话」。**

---

## 第 2 章 · 配置

### 2.1 装依赖、复制配置模板

```powershell
pip install -r requirements.txt
copy .env.example .env          # Linux / macOS 用 cp
```

### 2.2 填 .env

用记事本打开 `.env`，**最少**把下面这几块填掉：

```dotenv
# ① OneBot 连接（第 1 章那两个端口和两个 token）
ONEBOT_HTTP=http://127.0.0.1:3000
ONEBOT_WS=ws://127.0.0.1:3001
ONEBOT_TOKEN=你的HTTPtoken
ONEBOT_WS_TOKEN=你的WebSockettoken

# ② 模型（任何 OpenAI 兼容端点都行）
LLM_API_BASE=https://api.deepseek.com
LLM_API_KEY=sk-xxxxxxxx
LLM_MODEL=deepseek-chat

# ③ 谁的指令它才听 —— 填你自己的 QQ 号
OWNER_IDS=123456789
ADMIN_IDS=123456789

# ④ 控制台和 MCP 的访问令牌，随便一串长随机字符
MCP_TOKEN=换成一串你自己乱敲的长字符
```

几个容易填错的地方：

| 变量 | 注意 |
|---|---|
| `ONEBOT_TOKEN` / `ONEBOT_WS_TOKEN` | **不是同一个**，别两个填一样的 |
| `OWNER_IDS` | 写你自己的 QQ 号，不是机器人号。写错了它就不听你的 |
| `ADMIN_IDS` | 决定谁能让它干活、谁能发控制命令；留空会退回 `OWNER_IDS` |
| `LLM_API_KEY` | 只填 key 本身，别带引号、别带空格 |

### ✅ 检查点

**在仓库根目录**执行（这个包没装进环境，所以要先把它塞进 sys.path）：

```powershell
python -c "import sys; sys.path.insert(0,'.'); from qqbridge.config import config; print(config.describe())"
```

**要看到**一行字典，重点看三处：

```text
"ws_token_set": true          ← WebSocket token 填上了
"owners": ["你的QQ号"]         ← 白名单是你的号
"llm": { ..., "has_key": true }  ← 模型 key 填上了
```

对不上怎么办：

| 现象 | 原因 |
|---|---|
| `ModuleNotFoundError: No module named 'qqbridge'` | 没在仓库根目录执行，或者依赖没装 |
| `ws_token_set: false` | `ONEBOT_WS_TOKEN` 没填 |
| `owners: []` | `OWNER_IDS` 没填 —— 这样它不会听任何人的指令 |
| `has_key: false` | `LLM_API_KEY` 没填进去 |

---

## 第 3 章 · 启动

```powershell
python run.py
```

**应该看到**类似这样的日志（时间戳会不一样）：

```text
INFO qqbridge.onebot: event stream connected: ws://127.0.0.1:3001
INFO qqbridge.server: bus.self_id = 123456789（@ 判定已启用）
INFO qqbridge.autoreply: autoreply loop started (enabled=True)
INFO:     Uvicorn running on http://127.0.0.1:18900
```

三行缺一不可：

- `event stream connected` → WebSocket 通了
- `bus.self_id = ...` → 拿到了机器人自己的号，**没有它 @ 消息就识别不出来**
- `autoreply loop started` → 接话循环起来了

> Windows 上也可以直接双击 `start.bat`。里面写死了一个 Python 路径，
> 换成你自己的。

然后浏览器打开 <http://127.0.0.1:18900/>：第一次进会弹两份协议，勾同意，
设一个后台账号密码，以后每次用这个登录。

### ✅ 检查点

控制台右上角那个写着「**QQ 已连接**」的小圆点要是**绿色**的。

显示「QQ 断开」→ WebSocket 没通，回第 1.3 步检查端口和 WS token。

---

## 第 4 章 · 验证它真的在听

现在去第 1.4 步那个群里，随便发一句话。

### 4.1 看它有没有收到

控制台「概览」页的「最近消息」，应该几秒内冒出来你刚发的那条。

> 没冒出来？去 [常见问题 · 收不到某个群的消息](#常见问题)。

### 4.2 让它说话

在群里 @ 它：

```text
@你的机器人 在干嘛
```

**正常情况下几秒内会回话。** 被 @ 是硬性要求 —— 就算模型判断「没什么可说的」，
程序也会再问它一次，还不行就回一句「在，你说。」，不会装死。

### 4.3 看它为什么说 / 为什么不说

控制台「日志与审计」页，找 `auto_reply`（说了）或 `auto_silent`（没说）。
`reason` 字段里是模型自己写的理由。

**沉默是正常的、也是常态。** 它是群友不是客服，大多数时候不该接话。
判断标准写在人格文件里，第 5 章可以改。

### 🎉 到这儿就算装好了

后面几章都是「按需调」，不是必须做的。

---

## 第 5 章 · 调教成你想要的样子

### 5.1 让它说话像人（人格）

人格就是一份 markdown，直接决定它的性格、语气、什么情况下不说话。

- 文件在 `data/system_prompt.md`
- 也可以在控制台「模型与人格」页的文本框里改，**改完立刻生效，不用重启**

想改得靠谱，先看文件里已经有的结构：人物设定 → 说话铁律 → 什么值得接 →
话术示例 → AI 味黑名单。改的时候别只改设定，**示例那部分最有用** ——
模型是照着示例学语气的。

> 找不到这个文件？它不在版本库里（属于你的私人配置）。
> 程序会退回用 `prompts/default_persona.md`，把它复制到 `data/system_prompt.md` 再改。

### 5.2 只监控几个群

默认所有群都说话。只想让它在指定几个群活动：

**控制台「巡检与唤醒」→ 监控范围**，里面是机器人所在群的勾选框（群名、人数、群号都有），
勾上保存就行。

- 一个都不勾 = 不设限制，所有群都管
- **没勾的群只落库，完全不唤醒** —— 不调模型、不花钱

### 5.3 说话节奏

现在默认是「攒一段再看」：最后一条消息之后静默 3 秒才出手，
群里一直热闹静不下来时最多等 45 秒，每分钟最多发 8 条（被 @ 不受限制）。

这些是 `AutoReply` 的构造参数（`settle_seconds` / `max_batch_age` /`max_per_minute`），
目前写在代码里，要改得动 `qqbridge/server.py` 里创建它的那一行。

### 5.4 群里能用的控制命令

只有管理员能发，命令本身不会触发模型，执行完直接回执：

| 命令 | 效果 | 群里收到的回执 |
|---|---|---|
| `/auto` | 恢复自动接话（默认就是它） | 已开启对话自动接话功能，并接受所有agent请求 |
| `/start` | 只保留工具，不自动接话 | 已切换为手动模式：只保留工具，不自动接话 |
| `/stop` | 全停 | 已停止当前会话的自动接话功能，并已停止所有agent请求 |

非管理员发这些，只会收到一句「只有管理员能切换运行状态」。

---

## 第 6 章 · 让它真的能干活

**管理员 @ 它，或者私聊它**，就是派活。比如：

```text
@你的机器人 看看 D 盘还有多少空间
@你的机器人 把这个目录下的日志按日期归个类
```

它会自己决定用哪个工具、跑几步，干完回一句结果。

> 关键区别：管理员**在群里闲聊（没 @）** 不会触发干活，走的是普通聊天。
> 这样才不会「管理员说啥都被当命令」。

### 它能用的工具

| 工具 | 干什么 |
|---|---|
| `list_dir` | 列目录 |
| `read_file` | 读文件 |
| `write_file` | 写文件 |
| `search_files` | 按文件名找 |
| `run_command` | 跑本地命令 |
| `fetch_url` | 抓网页 / 调 API |

### 边界（这些是硬限制，不是建议）

| 约束 | 行为 |
|---|---|
| 路径越界 | 直接拒绝，只允许 `AGENT_ROOT` 之内 |
| 单次读 | ≤ 256 KB |
| 单次写 | ≤ 1 MB |
| 命令输出 | ≤ 64 KB |
| 命令超时 | 默认 15 秒，硬上限 60 秒 |
| 抓网页 | ≤ 512 KB，只允许 http/https |
| 危险命令 | 黑名单拦截（格式化 / mkfs / 关机重启 / diskpart / rm -rf 等），记 `DENIED` |
| 死循环 | 同一个调用同样参数连续失败 3 次就停手，改问模型要一句人话 |
| 调用方 | MCP 侧 `agent_run` 仅限 `ADMIN_IDS` |

> 命令走 `asyncio.to_thread`，不阻塞事件循环 —— 实测执行期间心跳最大间隔 0.111 秒。

### 它做过的每件事都留痕

所有读写逐行写进 `data/agent.log`（JSON Lines），同时进 SQLite 审计表：

```json
{"at":"2026-10-07 17:52:07","action":"write_file","target":".../data/x.txt","detail":{"bytes":12},"state":"SUCCEEDED"}
{"at":"2026-10-07 17:52:08","action":"run_command","target":"format C: /y","detail":{},"state":"DENIED"}
```

控制台「内置 Agent」页可以直接看。

---

## 第 7 章 · 自动发说说

让机器人按节奏往自己的 QQ 空间发内容，内容由模型按主题现写。

打开控制台「自动说说」页：

| 你要配的 | 说明 |
|---|---|
| **频率** | 按间隔（每 N 小时）或按时刻（每天 12:30 / 21:00 这类） |
| **兜底** | 最短间隔 + 静默时段（例如 23:30–08:00 不发） |
| **主题** | 预设 8 个，可增删改、可只启用其中几个、随机或轮转 |
| **时间窗** | 主题能带时间窗：「深夜」只在 22:30–05:30 被选中，下午不会写着「凌晨了还醒着」 |

按钮「立刻发一条」可以试口吻，不影响排期。

> 这条链路走的是 SnowLuma 的扩展接口。**换 NapCat / Lagrange 就没有了**，
> 因为发说说不在 OneBot v11 标准里。

---

## 第 8 章 · 接到外部 Agent

qqbridge 同时是个 MCP 服务。已经用着 Codex / Claude Code / 其它 MCP 宿主的话，
可以直接挂上去，让它用工具收发消息、管群、查审计。

```json
{
  "mcpServers": {
    "qqbridge": {
      "url": "http://127.0.0.1:18900/mcp/",
      "headers": { "Authorization": "Bearer 你的MCP_TOKEN" }
    }
  }
}
```

> 地址结尾那个斜杠别丢，丢了会 307 跳转。

工具清单见[附录 C](#附录-c--mcp-工具)。

---

## 附录 A · 它内部是怎么跑的

### 为什么不用「让模型等着」

大多数群聊机器人是这么驱动的：

```text
Agent → wait(timeout=180)   # 阻塞着等，没消息也干等，照样烧 token
     → 有消息 → 回复 → 再 wait → ……
```

三个毛病：空闲时白烧 token；Agent 进程一停整条链路就断；等待期间一直占着上下文。

qqbridge 把**等待下沉到服务层**：消息先在本地的环形缓冲里排着（零成本），
真有事才叫模型。

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

MCP 工具也全部「立刻返回」，`poll_events` 没消息就返回空，永远不会让调用方卡住。

### 两条路，完全分开

| 情况 | 走哪条 | 怎么决定 |
|---|---|---|
| 管理员在跟它说话（@ 了它，或私聊） | 直接叫内置 Agent | 不判断、不批处理，带工具去干 |
| 其它（群里所有人聊天） | 批量闲聊 | 攒一段，整段交给模型判断要不要插一句 |

### 闲聊：攒一段再看，不是一条一条接

人隔一会儿扫一眼手机，判断「这段值不值得插一句」，而不是被每条消息牵着回一句。

```text
每 1.5 秒看一眼
  ├─ 没有未读 → 接着睡
  ├─ 按会话（群 / 私聊）分成几个桶，每个桶单独处理
  │    ├─ 有人 @ 我 → 不等，立刻处理
  │    └─ 否则等「最后一条之后静默 3 秒」再出手
  ├─ 把这个群的整段（未读 + 上下文）交给模型
  └─ 回一句 or 沉默 → 处理完整批才推进这个群自己的「已看过」标记
```

**每个群一份独立会话。** 上下文只取本群的，已读位置也各记各的 ——
不然 A 群的话题会混进 B 群的判断里。

### 一次回几条，也能拆开发

模型可以一次给 2~3 条短消息，程序**分几次调用**发出去，中间隔 1.2 秒加一点随机。
一条一条蹦出来才像人在打字。

发之前统一过一遍文字规整（`qqbridge/textnorm.py`）：

| 规则 | 例子 |
|---|---|
| 句尾不收句号 | `好的。` → `好的` |
| 省略号用中文句号 | `不知道...` → `不知道。。。` |
| 语气用的连续句号保留 | `无语。。。` 不动 |
| 句尾左括号保留 | `行（` 不动 |
| 一条里塞了换行就拆开 | `吃了\n你呢` → 两条 |

### 输出解析：绝不把 JSON 发进群

模型被要求以 JSON 回复。解析分五档，**任何一档都不会把原始 JSON 发出去**：

| 判定 | 场景 | 行为 |
|---|---|---|
| `ok` | 直接是合法 JSON | 正常取内容 |
| `fenced` | 被 markdown 代码块包着 | 剥壳后取内容 |
| `embedded` | JSON 前后夹了别的话 | 抠出第一个 `{...}` 块 |
| `plain` | 本来就是一句人话 | 当纯文本发 |
| `failed` | **看着像 JSON 但坏了（截断 / 畸形）** | **改判沉默**，并写审计 |

> 这条是踩坑加的：带思考链的模型会把推理 token 也算进上限，
> JSON 被截断时早期版本会把整段 `{"reply": ...}` 原样发进群。
> 用 reasoning 模型时建议 `LLM_MAX_TOKENS` 不低于 2048。

### 出口网络：代理挂了不能让它哑掉

所有对外请求都走 `qqbridge/net.py`，规矩三条：

1. **回环地址永远直连** —— 系统代理不转发 127.0.0.1，走代理只会连不上
2. **远端先按系统代理走，连不上自动退回直连**，并记住上次成功的路径
3. 两种都不通时抛出带代理信息的可读错误

> 踩出来的：梯子关掉后进程环境里还留着 HTTPS 代理变量，
> 默认情况下所有请求都被丢给没人监听的端口，模型、说说、Agent 一起静默。

---

## 附录 B · 配置速查

完整项见 [`.env.example`](.env.example)。最常改的：

| 变量 | 说明 |
|---|---|
| `LLM_API_BASE` / `LLM_API_KEY` / `LLM_MODEL` | 机器人自己的模型凭据 |
| `AUTO_REPLY` | 是否主动接话 |
| `SYSTEM_PROMPT_FILE` | 人格文件路径 |
| `ONEBOT_HTTP` / `ONEBOT_WS` | OneBot 的 HTTP 与 WebSocket 地址 |
| `ONEBOT_TOKEN` / `ONEBOT_WS_TOKEN` | 两者常常**不是同一个** token |
| `MCP_TOKEN` | MCP 与控制台共用的访问令牌 |
| `OWNER_IDS` | 只有这些 QQ 能触发写操作；留空则全部拒绝 |
| `ADMIN_IDS` | 能发 `/stop` `/start` `/auto`、能走 agent 路径 |
| `ALLOW_SEND` / `ALLOW_MANAGE` | 发送 / 群管理总开关，默认关 |
| `WATCH_GROUPS` | 只监控这些群；留空 = 全部 |
| `COOLDOWN_SECONDS` | 自动触发接话的冷却秒数（0 = 不自动触发） |
| `RING_SIZE` | 环形缓冲大小（默认 500），满了会丢最旧的并计数 |
| `AGENT_ENABLED` / `AGENT_ROOT` / `AGENT_MAX_STEPS` | 内置 Agent 的开关、根目录、单次步数上限 |

---

## 附录 C · MCP 工具

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

> 这些扩展接口是 SnowLuma 独有的。换 OneBot 实现时必须重新核对。

---

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
│   ├─ bus.py               事件总线：环形缓冲 + 持久化 id + 按会话水位线
│   ├─ autoreply.py         接话循环：按会话分桶、限流、@ 兜底
│   ├─ agent.py             人格、历史、决策与输出解析兜底
│   ├─ agentloop.py         内置 Agent 的多步循环
│   ├─ agenttools.py        文件 / 命令 / 抓网页工具（带边界与留痕）
│   ├─ textnorm.py          发出前的文字规整
│   ├─ net.py               出口网络策略（代理挂了自动退回直连）
│   ├─ llm.py               OpenAI 兼容客户端
│   ├─ vision.py            看图（把群里的图片喂给视觉模型）
│   ├─ webpage.py           抓链接正文
│   ├─ academic.py          学术模式识别
│   ├─ onebot.py            OneBot HTTP + 常驻 WebSocket（指数退避重连）
│   ├─ qzone.py             QQ 空间扩展接口
│   ├─ qzone_auto.py        自动发说说的调度与生成
│   ├─ auth.py              控制台鉴权 + 两份协议全文
│   ├─ scheduler.py         巡检节拍
│   ├─ control.py           auto / manual / stopped
│   ├─ commands.py          群内命令与回执文案
│   ├─ rules.py             关键词分级
│   ├─ store.py             SQLite 持久化
│   ├─ config.py            .env 读取与回写
│   └─ __init__.py
└─ data/                    运行时数据（已 gitignore）
    ├─ qqbridge.sqlite3     消息 / 成员 / 群 / 审计 / 幂等键
    ├─ system_prompt.md     人格（可改，热加载）
    ├─ qzone.json           自动说说配置与历史
    ├─ auth.json            后台账号（加盐，无明文）
    ├─ agent.log            内置 Agent 的读写记录（JSONL）
    └─ scheduler.json       巡检设置
```

---

## 测试

全部是自包含脚本：不联网、不碰仓库里的真实配置（临时目录里跑）。

```powershell
python tests/test_auth_qzone.py    # 门禁 / 协议 / 自动说说 / 图片代理白名单
python tests/test_console.py       # 控制台每个开关是不是「真的生效」
python tests/test_autoreply.py     # 批量闲聊、派活直连 Agent、被 @ 必回
python tests/test_agenttools.py    # 命令黑名单别误杀、卡住时别发废话
python tests/test_bus.py           # 事件总线：按会话水位线、启动回填、溢出计数
python tests/test_textnorm.py      # 发出前的文字规整：句号、省略号、拆多条
python tests/test_onebot.py        # WebSocket 事件流：断线退避、connected 不说谎
python tests/test_net.py           # 出口策略：代理挂了要能自动退回直连
node   tests/ui_console.mjs        # 控制台前端脚本（假 DOM 里跑 ui.html）
python tests/test_readme.py        # 这份 README 有没有跟代码对不上
```

两个值得一提的守卫：

- `test_console.py` 会在开头给仓库真实的 `.env` 逐字节拍快照，结尾断言一个字节都没变 ——
  因为控制台接口会把设置写回 `.env`，测试一旦认错目录就会把线上配置冲掉
- `test_readme.py` 检查这份 README：提到的命令、环境变量、MCP 工具、目录结构、
  测试脚本是不是都真实存在，工具数量对不对得上。文档一漂就报错

---

## 常见问题

### 收不到某个群的消息

按顺序查：

1. 控制台右上角是不是「QQ 已连接」—— 不是的话回第 1 章
2. 「监控范围」有没有勾这个群（没勾的群只落库，不唤醒）
3. 这个群在不在 OneBot 实现的监听列表里

### 机器人一直不吭声，审计里全是 auto_silent

先看控制台「模型与人格」页的 `last_error`：

- **有值** → 模型调用失败了。最常见的两个原因：地址或模型名填错；key 没余额。
  改回来立刻生效，不用重启。
- **是空的** → 模型真的选择了沉默。看审计里的 `reason`，它会写清楚理由。

如果是「本来应该说话但没说」，多半是人格改得太保守，去第 5.1 章调。

### @ 了它还是不回

正常情况下不可能（被 @ 有硬兜底）。如果真的一点反应都没有：

1. 这个群在「监控范围」里吗
2. 运行模式是不是被 `/stop` 停了 —— 群里发 `/auto` 恢复
3. 审计里有没有 `auto_reply` 记录 —— 有记录说明已经发出去了，那是 QQ 侧的问题

### 控制台里改了设置没反应

先确认页面上显示的是你的新值（不是被刷回去了）。
值对但行为没变，多半是进程还在跑改动前的代码 —— **重启一次**。

界面上的值来自后端，只有「保存」成功才落盘；顶部会弹提示，失败会直接说原因。

### 控制台里图片显示「图片已过期或取不到」

QQ 图床的链接是带签名的临时链，几小时后失效。正常现象，不是 BUG。
要长期留档得在收到消息时就另存。

### 模型报「All connection attempts failed」

代理问题。开了 VPN / 梯子又关掉之后，进程环境里的代理变量还在，
请求会发给一个没人监听的端口。程序会自动退回直连（日志里会写「经代理连不上，改试直连」），
如果还不行，清掉环境里的代理变量再启动。

---

## 安全与协议

- **写操作只认 `OWNER_IDS`**，昵称与自称不算数
- **幂等键防重放**；审计表记录「谁发起、做什么、结果如何」
- **控制台要登录**，会话只存在内存里，重启即失效；密码加盐存储
- **群管理前核对目标 role**，群主不可被禁言；失败如实上报，绝不谎报成功
- **内置 Agent 的所有读写都在 `AGENT_ROOT` 内**，越界直接拒绝，且逐条留痕
- **提示词注入不生效**：要求以开发者身份下令、索要凭据、顶号之类的请求一律拒绝并留审计

控制台首访必须同意两份协议，要点：

1. **不保证没有 BUG**，因 BUG 造成的损失作者不赔
2. **若有人拿它做违法的事，开发者概不负责**，责任由使用者独立承担
3. 机器人及其相关创作的**所有权归属 EnderCraft**

---

## License

MIT

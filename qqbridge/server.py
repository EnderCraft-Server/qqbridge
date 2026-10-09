"""MCP Streamable HTTP server exposing the QQ toolbox.

Design note: every tool returns immediately. There is no 180-second wait loop;
events queue in the bus and the agent pulls them when it runs.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import uvicorn
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from mcp.server.lowlevel import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.server.transport_security import TransportSecuritySettings

from .bus import EventBus
from .config import config
from .onebot import OneBot
from .agent import QqAgent, load_system_prompt, DEFAULT_SYSTEM
from .agentloop import AgentLoop
from .agenttools import LocalTools
from .auth import AGREEMENT_VERSION, EULA, TERMS, Auth
from .autoreply import AutoReply
from .control import Control
from .llm import LLM
from .qzone_auto import QzoneAuto
from .rules import Rules
from . import commands
from .scheduler import Scheduler
from .store import Store
from .tools import Denied, Toolbox

log = logging.getLogger("qqbridge.server")

INSTRUCTIONS = """QQ 桥。所有工具都是非阻塞的：poll_events 立刻返回，没有消息就返回空，
不要用循环空转等待。处理完一批事件后调用 mark_processed 推进游标。

后台巡检轮次的输出纪律：处理完直接结束本轮，不要向用户输出任何文字——
不汇报、不总结、不复述唤醒提示、不说明做了什么。发到群里的消息就是全部产出。

写操作只接受白名单内的 owner 发起，并且需要 idempotency_key。"""


def build_tools(box: Toolbox, rules: Rules, sched: Scheduler, control: Control,
                llm: LLM, agent: QqAgent, agent_loop: AgentLoop, local_tools: LocalTools):
    tools = []

    def tool(name: str, description: str, schema: dict):
        def deco(fn):
            tools.append({"name": name, "description": description,
                          "inputSchema": schema, "fn": fn})
            return fn
        return deco

    @tool("get_status", "查看桥、OneBot 连接与事件队列状态。", {"type": "object", "properties": {}, "additionalProperties": False})
    async def _status(**kw):
        return box.get_status()

    @tool("list_groups", "列出机器人所在群。", {"type": "object",
          "properties": {"refresh": {"type": "boolean", "default": False}}, "additionalProperties": False})
    async def _groups(refresh: bool = False, **kw):
        return await box.list_groups(refresh)

    @tool("list_members", "列出群成员（含 role）。", {"type": "object",
          "properties": {"group_id": {"type": "string"}, "refresh": {"type": "boolean", "default": True}},
          "required": ["group_id"], "additionalProperties": False})
    async def _members(group_id: str, refresh: bool = True, **kw):
        return await box.list_members(group_id, refresh)

    @tool("get_history", "从 OneBot 拉取群历史（最多200条）。", {"type": "object",
          "properties": {"group_id": {"type": "string"}, "count": {"type": "integer", "default": 30}},
          "required": ["group_id"], "additionalProperties": False})
    async def _hist(group_id: str, count: int = 30, **kw):
        return await box.get_history(group_id, count)

    @tool("local_history", "读取本地已缓存的消息。", {"type": "object",
          "properties": {"group_id": {"type": "string"}, "limit": {"type": "integer", "default": 30}},
          "required": ["group_id"], "additionalProperties": False})
    async def _local(group_id: str, limit: int = 30, **kw):
        return box.local_history(group_id, limit)

    @tool("search_messages", "在本地消息里做关键词检索。", {"type": "object",
          "properties": {"query": {"type": "string"}, "group_id": {"type": "string", "default": ""},
                         "limit": {"type": "integer", "default": 20}},
          "required": ["query"], "additionalProperties": False})
    async def _search(query: str, group_id: str = "", limit: int = 20, **kw):
        return box.search_messages(query, group_id, limit)

    @tool("poll_events", "非阻塞取事件：立刻返回，最多给出当前排队的消息。cursor 传上次的游标。",
          {"type": "object", "properties": {
              "cursor": {"type": "integer", "default": 0},
              "include_low": {"type": "boolean", "default": True},
              "limit": {"type": "integer", "default": 50}},
           "additionalProperties": False})
    async def _poll(cursor: int = 0, include_low: bool = True, limit: int = 50, **kw):
        return box.poll_events(cursor, include_low, limit)

    @tool("list_pending", "只返回待处理队列，绝不等待。", {"type": "object",
          "properties": {"include_low": {"type": "boolean", "default": True}}, "additionalProperties": False})
    async def _pending(include_low: bool = True, **kw):
        return box.list_pending(include_low)

    @tool("mark_processed", "推进已处理游标。", {"type": "object",
          "properties": {"through_id": {"type": "integer"}}, "required": ["through_id"],
          "additionalProperties": False})
    async def _mark(through_id: int, **kw):
        return {"cursor": box.bus.mark_processed(through_id)}

    @tool("send_message", "向群发送文本。需 ALLOW_SEND 且 actor 在 OWNER_IDS。",
          {"type": "object", "properties": {
              "actor": {"type": "string", "description": "发起指令的 QQ 号"},
              "group_id": {"type": "string"}, "text": {"type": "string"},
              "reply_to": {"type": "string", "default": ""},
              "idempotency_key": {"type": "string"}},
           "required": ["actor", "group_id", "text", "idempotency_key"], "additionalProperties": False})
    async def _send(actor: str, group_id: str, text: str, idempotency_key: str, reply_to: str = "", **kw):
        return await box.send_message(actor, group_id, text, idempotency_key, reply_to)

    @tool("send_image", "向群发送图片（本地路径或 URL）。需 ALLOW_SEND。",
          {"type": "object", "properties": {
              "actor": {"type": "string"}, "group_id": {"type": "string"},
              "image": {"type": "string"}, "idempotency_key": {"type": "string"}},
           "required": ["actor", "group_id", "image", "idempotency_key"], "additionalProperties": False})
    async def _img(actor: str, group_id: str, image: str, idempotency_key: str, **kw):
        return await box.send_image(actor, group_id, image, idempotency_key)

    @tool("manage_group", "群管理：mute/unmute/kick/rename/card/whole_ban/leave。需 ALLOW_MANAGE。",
          {"type": "object", "properties": {
              "actor": {"type": "string"}, "action": {"type": "string",
                  "enum": ["mute", "unmute", "kick", "rename", "card", "whole_ban", "leave"]},
              "group_id": {"type": "string"}, "user_id": {"type": "string", "default": ""},
              "duration_seconds": {"type": "integer", "default": 0}, "value": {"type": "string", "default": ""},
              "idempotency_key": {"type": "string"}},
           "required": ["actor", "action", "group_id", "idempotency_key"], "additionalProperties": False})
    async def _manage(actor: str, action: str, group_id: str, idempotency_key: str,
                      user_id: str = "", duration_seconds: int = 0, value: str = "", **kw):
        return await box.manage(actor, action, group_id, idempotency_key, user_id, duration_seconds, value)

    @tool("list_keywords", "查看当前触发接话的关键词表。", {"type": "object",
          "properties": {}, "additionalProperties": False})
    async def _kw_list(**kw):
        return {"keywords": rules.keywords, "count": len(rules.keywords)}

    @tool("set_keywords", "整体替换关键词表；命中关键词的消息会进入待处理队列。",
          {"type": "object", "properties": {
              "actor": {"type": "string"}, "keywords": {"type": "array", "items": {"type": "string"}}},
           "required": ["actor", "keywords"], "additionalProperties": False})
    async def _kw_set(actor: str, keywords: list, **kw):
        box._owner(actor, "set_keywords")
        rules.save([str(k).strip() for k in keywords if str(k).strip()])
        return {"keywords": rules.keywords, "count": len(rules.keywords)}

    @tool("get_scheduler", "查看自动唤醒设置与运行状态。", {"type": "object",
          "properties": {}, "additionalProperties": False})
    async def _sched_get(**kw):
        return sched.status()

    @tool("set_scheduler", "设置自动唤醒：启用状态、间隔秒数、最小间隔、每小时上限、静默时段。",
          {"type": "object", "properties": {
              "actor": {"type": "string"},
              "enabled": {"type": "boolean"},
              "interval_seconds": {"type": "integer"},
              "min_gap_seconds": {"type": "integer"},
              "max_turns_per_hour": {"type": "integer"},
              "quiet_hours": {"type": "array", "items": {"type": "array", "items": {"type": "string"}}}},
           "required": ["actor"], "additionalProperties": False})
    async def _sched_set(actor: str, **patch):
        box._owner(actor, "set_scheduler")
        patch.pop("actor", None)
        saved = sched.save({k: v for k, v in patch.items() if v is not None})
        box.store.audit(actor, "set_scheduler", "scheduler", saved, "SUCCEEDED")
        return sched.status()

    @tool("get_control", "查看运行模式（auto/stopped/manual）。", {"type": "object",
          "properties": {}, "additionalProperties": False})
    async def _ctl_get(**kw):
        return control.status()

    @tool("set_control", "切换运行模式。mode: auto/stopped/manual；agent_mode: chat/agent。仅管理员。",
          {"type": "object", "properties": {
              "actor": {"type": "string"},
              "mode": {"type": "string", "enum": ["auto", "stopped", "manual"]},
              "agent_mode": {"type": "string", "enum": ["chat", "agent"]},
              "reason": {"type": "string", "default": ""}},
           "required": ["actor"], "additionalProperties": False})
    async def _ctl_set(actor: str, mode: str = "", agent_mode: str = "", reason: str = "", **kw):
        if str(actor) not in config.admins:
            raise Denied(f"{actor} 不是管理员，不能切换运行模式。")
        if agent_mode:
            control.set_agent_mode(agent_mode, actor=actor, reason=reason)
        if mode:
            control.set(mode, actor=actor, reason=reason)
        return control.status()

    @tool("send_private", "单聊发文本。需 ALLOW_SEND 且 actor 在 OWNER_IDS。",
          {"type": "object", "properties": {
              "actor": {"type": "string"}, "user_id": {"type": "string"},
              "text": {"type": "string"}, "idempotency_key": {"type": "string"}},
           "required": ["actor", "user_id", "text", "idempotency_key"], "additionalProperties": False})
    async def _priv(actor: str, user_id: str, text: str, idempotency_key: str, **kw):
        return await box.send_private(actor, user_id, text, idempotency_key)

    @tool("set_profile", "修改机器人自己的 QQ 昵称。仅管理员。",
          {"type": "object", "properties": {
              "actor": {"type": "string"}, "nickname": {"type": "string"},
              "idempotency_key": {"type": "string"}},
           "required": ["actor", "nickname", "idempotency_key"], "additionalProperties": False})
    async def _profile(actor: str, nickname: str, idempotency_key: str, **kw):
        return await box.set_profile(actor, nickname, idempotency_key)

    @tool("qzone_list", "读取说说列表（默认机器人自己的空间）。",
          {"type": "object", "properties": {
              "target_uin": {"type": "string", "default": ""},
              "pos": {"type": "integer", "default": 0},
              "num": {"type": "integer", "default": 20}}, "additionalProperties": False})
    async def _qzlist(target_uin: str = "", pos: int = 0, num: int = 20, **kw):
        return await box.qzone_list(target_uin, pos, num)

    @tool("qzone_feeds", "读取好友动态。", {"type": "object", "properties": {
              "page": {"type": "integer", "default": 1},
              "count": {"type": "integer", "default": 10}}, "additionalProperties": False})
    async def _qzfeeds(page: int = 1, count: int = 10, **kw):
        return await box.qzone_feeds(page, count)

    @tool("qzone_publish", "发表纯文本说说（发到机器人自己的空间）。仅管理员。",
          {"type": "object", "properties": {
              "actor": {"type": "string"}, "content": {"type": "string"},
              "idempotency_key": {"type": "string"}},
           "required": ["actor", "content", "idempotency_key"], "additionalProperties": False})
    async def _qzpub(actor: str, content: str, idempotency_key: str, **kw):
        return await box.qzone_publish(actor, content, idempotency_key)

    @tool("qzone_delete", "删除机器人自己空间的一条说说（按 tid）。仅管理员。",
          {"type": "object", "properties": {
              "actor": {"type": "string"}, "tid": {"type": "string"},
              "idempotency_key": {"type": "string"}},
           "required": ["actor", "tid", "idempotency_key"], "additionalProperties": False})
    async def _qzdel(actor: str, tid: str, idempotency_key: str, **kw):
        return await box.qzone_delete(actor, tid, idempotency_key)

    @tool("qzone_like", "给一条说说点赞/取消赞（点赞好友的需传 target_uin）。仅管理员。",
          {"type": "object", "properties": {
              "actor": {"type": "string"}, "tid": {"type": "string"},
              "target_uin": {"type": "string", "default": ""},
              "like": {"type": "boolean", "default": True},
              "abstime": {"type": "integer", "default": 0},
              "idempotency_key": {"type": "string"}},
           "required": ["actor", "tid", "idempotency_key"], "additionalProperties": False})
    async def _qzlike(actor: str, tid: str, idempotency_key: str, target_uin: str = "",
                      like: bool = True, abstime: int = 0, **kw):
        return await box.qzone_like(actor, tid, idempotency_key, target_uin, like, abstime)

    @tool("qzone_comment", "评论一条说说（好友的需传 target_uin）。仅管理员。",
          {"type": "object", "properties": {
              "actor": {"type": "string"}, "tid": {"type": "string"},
              "content": {"type": "string"}, "target_uin": {"type": "string", "default": ""},
              "idempotency_key": {"type": "string"}},
           "required": ["actor", "tid", "content", "idempotency_key"], "additionalProperties": False})
    async def _qzcmt(actor: str, tid: str, content: str, idempotency_key: str,
                     target_uin: str = "", **kw):
        return await box.qzone_comment(actor, tid, content, idempotency_key, target_uin)

    @tool("agent_run", "让内置 Agent 执行一个任务（可读写文件、跑命令）。所有读写都记日志。",
          {"type": "object", "properties": {
              "actor": {"type": "string"},
              "task": {"type": "string", "description": "要它做什么，说清楚目标和范围"},
              "max_steps": {"type": "integer", "default": 6}},
           "required": ["actor", "task"], "additionalProperties": False})
    async def _agent_run(actor: str, task: str, max_steps: int = 6, **kw):
        if str(actor) not in config.admins:
            raise Denied(f"{actor} 不是管理员，不能用内置 Agent。")
        if not config.agent_enabled:
            raise Denied("AGENT_ENABLED=false，内置 Agent 未启用。")
        if max_steps and int(max_steps) != agent_loop.max_steps:
            agent_loop.max_steps = max(1, min(int(max_steps), 20))
        system = agent.system_prompt + "\n\n你还可以使用工具读写文件、执行命令。改动前先看清楚目标。"
        # 这条是外部 MCP 调用，没有群上下文 —— 必须清掉上一轮可能残留的目标，
        # 否则 agent 会把图发到之前那个群。要发图请直接用 send_image 工具。
        agent_loop.tools.target_group = ""
        return await agent_loop.run(system, task)

    @tool("agent_log", "查看内置 Agent 的文件读写/命令执行日志。", {"type": "object",
          "properties": {"limit": {"type": "integer", "default": 40}}, "additionalProperties": False})
    async def _agent_log(limit: int = 40, **kw):
        p = local_tools.log_path
        if not p.is_file():
            return {"lines": [], "note": "还没有记录"}
        rows = p.read_text(encoding="utf-8", errors="replace").splitlines()
        return {"lines": rows[-max(1, min(int(limit), 200)):], "total": len(rows)}

    @tool("audit_tail", "查看最近的操作审计记录。", {"type": "object",
          "properties": {"limit": {"type": "integer", "default": 30}}, "additionalProperties": False})
    async def _audit(limit: int = 30, **kw):
        return box.audit_tail(limit)

    return tools


def create_app() -> FastAPI:
    store = Store(config.data_dir / "qqbridge.sqlite3")
    rules = Rules(path=config.data_dir / "keywords.json")
    bus = EventBus(size=config.ring_size, cooldown_seconds=config.cooldown, rules=rules,
                   watch_groups=config.watch_groups, store=store)
    control = Control(config.data_dir / "control.json", bus, store)
    bus.control = control
    sched = Scheduler(config.data_dir / "scheduler.json", bus, store)
    llm = LLM(config.llm_api_base, config.llm_api_key, config.llm_model,
              max_tokens=config.llm_max_tokens, temperature=config.llm_temperature,
              thinking=config.llm_thinking, reasoning_effort=config.llm_reasoning_effort,
              timeout=config.llm_timeout)
    prompt_path = (config.data_dir.parent / config.system_prompt_file
                   if not config.system_prompt_file.startswith("/")
                   else __import__("pathlib").Path(config.system_prompt_file))
    if not prompt_path.is_absolute():
        prompt_path = config.data_dir.parent / config.system_prompt_file
    agent = QqAgent(llm, load_system_prompt(prompt_path, DEFAULT_SYSTEM), store=store)
    # bot 必须在 local_tools 之前建 —— send_image 要用它。
    # （之前顺序写反了，直接 UnboundLocalError，start.bat 一闪就退。）
    bot = OneBot(config.http, config.ws, config.token, config.ws_token)
    local_tools = LocalTools(Path(config.agent_root) if config.agent_root else config.ROOT,
                             store, bot=bot)
    agent_loop = AgentLoop(llm, local_tools, max_steps=config.agent_max_steps, store=store)
    auth = Auth(config.data_dir / "auth.json")
    qzone = QzoneAuto(config.data_dir / "qzone.json", llm, bot, store)
    autoreply = AutoReply(bus, store, agent, bot, control, enabled=config.auto_reply,
                          agent_loop=agent_loop)
    box = Toolbox(bot, bus, store)
    specs = build_tools(box, rules, sched, control, llm, agent, agent_loop, local_tools)
    by_name = {s["name"]: s for s in specs}

    protocol = Server("qqbridge", version="0.1.0", instructions=INSTRUCTIONS)

    @protocol.list_tools()
    async def list_tools():
        from mcp import types
        return [types.Tool(name=s["name"], description=s["description"], inputSchema=s["inputSchema"])
                for s in specs]

    @protocol.call_tool(validate_input=False)
    async def call_tool(name: str, arguments: dict):
        from mcp import types
        spec = by_name.get(name)
        if spec is None:
            return [types.TextContent(type="text", text=json.dumps({"error": f"未知工具 {name}"}, ensure_ascii=False))]
        try:
            result = await spec["fn"](**(arguments or {}))
            return [types.TextContent(type="text", text=json.dumps(result, ensure_ascii=False, default=str))]
        except Denied as exc:
            return [types.TextContent(type="text", text=json.dumps({"error": str(exc), "error_code": "denied"}, ensure_ascii=False))]
        except Exception as exc:
            log.exception("tool %s failed", name)
            return [types.TextContent(type="text", text=json.dumps({"error": str(exc), "error_code": "failed"}, ensure_ascii=False))]

    manager = StreamableHTTPSessionManager(
        protocol, json_response=True, stateless=True,
        security_settings=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=[f"{config.host}:{config.port}", f"localhost:{config.port}"],
            allowed_origins=[f"http://{config.host}:{config.port}", f"http://localhost:{config.port}"],
        ),
    )

    app = FastAPI()
    app.state.box = box
    app.state.bot = bot
    app.state.rules = rules
    app.state.llm = llm
    app.state.agent = agent
    app.state.autoreply = autoreply
    app.state.agent_loop = agent_loop
    app.state.local_tools = local_tools
    app.state.auth = auth
    app.state.qzone = qzone
    app.state.sched = sched
    app.state.control = control

    @app.on_event("startup")
    async def _startup():
        global _MAIN_LOOP, _BOT
        _MAIN_LOOP = asyncio.get_running_loop()
        _BOT = bot
        # 先回填再开工：反过来的话，WS 循环可能已经塞进来几条，
        # 回填把游标推过去之后就留下几条没人管的僵尸 pending。
        try:
            bus.backfill(store.recent_events(config.ring_size))
        except Exception:
            log.exception("启动回填历史消息失败（控制台会从空列表开始）")
        await bot.start(on_event=lambda ev: _ingest(ev, bus, store, control))
        # 关键：OneBot 启动后才拿得到自己的 QQ 号。bus 没有 self_id 的话，
        # mentions_me 永远判 False —— @ 消息就进不了高优先级队列。
        if bot.self_id:
            bus.self_id = str(bot.self_id)
            log.info("bus.self_id = %s（@ 判定已启用）", bus.self_id)
        else:
            log.warning("拿不到 self_id，@ 消息将无法识别")
        await sched.start()
        await autoreply.start()
        await qzone.start()
        app.state.session_task = asyncio.create_task(_run_manager())

    @app.on_event("shutdown")
    async def _shutdown():
        await qzone.stop()
        await autoreply.stop()
        await sched.stop()
        await bot.stop()

    async def _run_manager():
        async with manager.run():
            await asyncio.Event().wait()


    # ---------- GUI + REST ----------
    from pathlib import Path as _Path
    from fastapi.responses import HTMLResponse

    UI = _Path(__file__).with_name("ui.html")

    @app.get("/", response_class=HTMLResponse)
    async def ui_page():
        return HTMLResponse(UI.read_text(encoding="utf-8"))

    COOKIE = "qqbridge_session"

    def _ui_auth(request: Request):
        """控制台接口的鉴权：优先登录态 cookie，其次兼容 mcp_token（脚本自动化）。"""
        hdr = request.headers.get("x-bridge-token")
        if hdr and config.mcp_token and hdr == config.mcp_token:
            return
        token = request.cookies.get(COOKIE) or request.query_params.get("session") or ""
        if auth.check(token):
            return
        raise HTTPException(401, "未登录或登录已过期")

    # ---------- 登录 / 协议 ----------
    def _set_session(response: Response, token: str):
        response.set_cookie(COOKIE, token, max_age=7 * 24 * 3600, httponly=True,
                            samesite="lax", path="/")

    @app.get("/api/auth/state")
    async def auth_state(request: Request):
        token = request.cookies.get(COOKIE) or request.query_params.get("session") or ""
        st = auth.state()
        st["logged_in"] = auth.check(token)
        return st

    @app.get("/api/auth/agreements")
    async def auth_agreements():
        return {"version": AGREEMENT_VERSION, "terms": TERMS, "eula": EULA}

    @app.post("/api/auth/accept")
    async def auth_accept(request: Request, response: Response):
        """首次进入：勾选同意协议 + 设置后台账号密码。"""
        body = await request.json()
        if not body.get("agreed"):
            raise HTTPException(400, "需要先同意《使用须知》与《最终用户许可协议》")
        user = str(body.get("username") or "")
        pwd = str(body.get("password") or "")
        try:
            auth.accept(user, pwd)          # 记录协议同意 + 建账号
            token = auth.login(user, pwd)   # 随即建立登录态
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        _set_session(response, token)
        store.audit("gui", "auth_init", auth.data.get("username", ""),
                    {"agreement": AGREEMENT_VERSION}, "SUCCEEDED")
        return {"ok": True, "username": auth.data.get("username", "")}

    @app.post("/api/auth/login")
    async def auth_login(request: Request, response: Response):
        body = await request.json()
        try:
            token = auth.login(str(body.get("username") or ""), str(body.get("password") or ""))
        except ValueError as exc:
            store.audit("gui", "auth_login", str(body.get("username") or ""), {}, "DENIED")
            raise HTTPException(401, str(exc))
        _set_session(response, token)
        store.audit("gui", "auth_login", auth.data.get("username", ""), {}, "SUCCEEDED")
        return {"ok": True, "username": auth.data.get("username", "")}

    @app.post("/api/auth/logout")
    async def auth_logout(request: Request, response: Response):
        auth.logout(request.cookies.get(COOKIE) or "")
        response.delete_cookie(COOKIE, path="/")
        return {"ok": True}

    @app.post("/api/auth/password")
    async def auth_password(request: Request):
        _ui_auth(request)
        body = await request.json()
        try:
            auth.change_password(str(body.get("old") or ""), str(body.get("new") or ""))
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        store.audit("gui", "auth_password", auth.data.get("username", ""), {}, "SUCCEEDED")
        return {"ok": True}

    # ---------- 图片代理 ----------
    # QQ 图床的 rkey 是签名过的临时链接。让浏览器直连有两个风险：
    # Referer 被挡、以及以后换域名。统一走后端取，顺便做主机白名单防 SSRF。
    IMAGE_HOST_OK = ("multimedia.nt.qq.com.cn", "multimedia.qpic.cn", "gchat.qpic.cn")
    IMAGE_SUFFIX_OK = (".qpic.cn", ".qq.com")
    IMAGE_MAX_BYTES = 12 * 1024 * 1024
    _img_cache: dict[str, tuple[bytes, str]] = {}

    @app.get("/api/image")
    async def api_image(request: Request, u: str = ""):
        _ui_auth(request)
        parsed = urlparse(u or "")
        host = (parsed.hostname or "").lower()
        if parsed.scheme != "https" or not host:
            raise HTTPException(400, "只允许 https 图片地址")
        if not (host in IMAGE_HOST_OK or host.endswith(IMAGE_SUFFIX_OK)):
            raise HTTPException(400, f"不允许从这个主机取图：{host}")
        if host.replace(".", "").isdigit():
            raise HTTPException(400, "不接受 IP 形式的图片地址")

        hit = _img_cache.get(u)
        if hit is None:
            import httpx
            try:
                async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
                    resp = await client.get(u, headers={"User-Agent": "qqbridge-console/1.0"})
                resp.raise_for_status()
            except Exception as exc:
                log.warning("图片代理失败 %s: %s", host, exc)
                raise HTTPException(502, f"取图失败：{type(exc).__name__}")
            body = resp.content
            if len(body) > IMAGE_MAX_BYTES:
                raise HTTPException(413, "图片太大")
            ctype = (resp.headers.get("content-type") or "image/jpeg").split(";")[0].strip()
            if not ctype.startswith("image/"):
                raise HTTPException(415, f"返回的不是图片：{ctype}")
            if len(_img_cache) > 60:
                _img_cache.clear()
            _img_cache[u] = (body, ctype)
            hit = _img_cache[u]
            store.audit("gui", "proxy_image", host, {"bytes": len(body)}, "SUCCEEDED")
        body, ctype = hit
        return Response(content=body, media_type=ctype,
                        headers={"Cache-Control": "private, max-age=600"})

    @app.get("/api/qzone")
    async def api_qzone(request: Request):
        _ui_auth(request)
        return qzone.status()

    @app.post("/api/qzone")
    async def api_qzone_set(request: Request):
        _ui_auth(request)
        patch = await request.json()
        qzone.save(patch)
        store.audit("gui", "set_qzone", "qzone", {k: v for k, v in patch.items() if k != "history"},
                    "SUCCEEDED")
        return qzone.status()

    @app.post("/api/qzone/post")
    async def api_qzone_post(request: Request):
        """立刻发一条（用于测试主题与口吻）。"""
        _ui_auth(request)
        body = {}
        try:
            body = await request.json()
        except Exception:
            pass
        out = await qzone.post_once(force_theme=str(body.get("theme") or ""))
        return out

    @app.get("/api/state")
    async def api_state(request: Request):
        _ui_auth(request)
        return {
            "config": config.describe(),
            "bot": {"self_id": bot.self_id, "ws_connected": bot.connected, "last_event_at": bot.last_event_at},
            "scheduler": sched.status(),
            "control": control.status(),
            "keywords": rules.keywords,
            "llm": llm.describe(),
            "agent": agent.status(),
            "autoreply": autoreply.status(),
            "agent_loop": agent_loop.status(),
            "qzone": qzone.brief(),
            "auth": auth.state(),
        }

    @app.get("/api/events")
    async def api_events(request: Request, limit: int = 25):
        _ui_auth(request)
        with bus._lock:
            events = list(bus._events)[-limit:]
            high, mid, low = set(bus._pending_high), set(bus._pending_mid), set(bus._pending_low)
        out = []
        for e in events:
            prio = "high" if e["id"] in high else "mid" if e["id"] in mid else "low" if e["id"] in low else "stored"
            out.append({**e, "priority": prio})
        return {"events": out}

    @app.get("/api/pending")
    async def api_pending(request: Request, cursor: int = 0, groups: str = ""):
        """Cheap poll for the wake plugin. Empty list when nothing new — costs no model tokens."""
        _ui_auth(request)
        if control.paused():
            return {"events": [], "count": 0, "cursor": cursor, "empty": True,
                    "paused": True, "mode": control.mode}
        wanted = {g.strip() for g in groups.split(",") if g.strip()} or set(config.watch_groups)
        with bus._lock:
            events = [e for e in bus._events if e["id"] > cursor]
        out = []
        for e in events:
            if wanted and e["group_id"] not in wanted:
                continue
            if e["is_self"]:
                continue
            out.append({
                "id": e["id"], "at": e["at"], "group_id": e["group_id"],
                "user_id": e["user_id"], "sender": e["sender"], "text": e["text"],
                "mentions_me": e["mentions_me"], "keyword_hit": e.get("keyword_hit"),
            })
        # The advertised cursor must only cover events we actually delivered. Taking the max
        # over the raw id>cursor set instead would skip past filtered-out events (our own
        # messages, other groups' traffic) and silently drop real messages the caller never saw.
        # When nothing is delivered we do not advance, so the caller re-polls cheaply.
        delivered = max((e["id"] for e in out), default=None)
        return {"events": out, "count": len(out),
                "cursor": cursor if delivered is None else delivered,
                "empty": not out}

    @app.get("/api/audit")
    async def api_audit(request: Request, limit: int = 20):
        _ui_auth(request)
        return {"records": store.audit_tail(limit)}

    @app.get("/api/prompt")
    async def api_prompt_get(request: Request):
        _ui_auth(request)
        return {"prompt": agent.system_prompt, "file": str(prompt_path), "chars": len(agent.system_prompt)}

    @app.post("/api/prompt")
    async def api_prompt_set(request: Request):
        _ui_auth(request)
        body = await request.json()
        text = str(body.get("prompt") or "")
        if not text.strip():
            raise __import__("fastapi").HTTPException(400, "system prompt 不能为空")
        prompt_path.parent.mkdir(parents=True, exist_ok=True)
        prompt_path.write_text(text, encoding="utf-8")
        agent.reconfigure(text)
        store.audit("gui", "set_prompt", str(prompt_path), {"chars": len(text)}, "SUCCEEDED")
        return {"prompt": agent.system_prompt, "chars": len(agent.system_prompt)}

    @app.get("/api/agent_log")
    async def api_agent_log(request: Request, limit: int = 30):
        _ui_auth(request)
        p = local_tools.log_path
        if not p.is_file():
            return {"lines": [], "total": 0}
        rows = p.read_text(encoding="utf-8", errors="replace").splitlines()
        return {"lines": rows[-max(1, min(int(limit), 200)):], "total": len(rows)}

    @app.post("/api/llm")
    async def api_llm(request: Request):
        _ui_auth(request)
        body = await request.json()
        llm.reconfigure(
            api_base=body.get("api_base"),
            api_key=body.get("api_key") or None,
            model=body.get("model"),
            max_tokens=body.get("max_tokens"),
            temperature=body.get("temperature"),
        )
        config.llm_api_base = llm.api_base
        config.llm_model = llm.model
        if body.get("api_key"):
            _persist_key(str(body["api_key"]))
        if "auto_reply" in body:
            config.auto_reply = bool(body["auto_reply"])
            autoreply.enabled = config.auto_reply
        config.persist_permissions()
        store.audit("gui", "set_llm", llm.model,
                    {"api_base": llm.api_base, "auto_reply": config.auto_reply}, "SUCCEEDED")
        return {"llm": llm.describe(), "auto_reply": config.auto_reply}

    def _persist_key(key: str):
        env = config.ROOT / ".env"
        try:
            lines = env.read_text(encoding="utf-8").splitlines() if env.is_file() else []
        except OSError:
            return
        out, seen = [], False
        for line in lines:
            if line.split("=", 1)[0].strip() == "LLM_API_KEY":
                out.append("LLM_API_KEY=" + key)
                seen = True
            else:
                out.append(line)
        if not seen:
            out.append("LLM_API_KEY=" + key)
        env.write_text("\n".join(out) + "\n", encoding="utf-8")

    @app.post("/api/control")
    async def api_control(request: Request):
        _ui_auth(request)
        body = await request.json()
        reason = str(body.get("reason") or "")
        am = str(body.get("agent_mode") or "")
        if am:
            control.set_agent_mode(am, actor="gui", reason=reason)
        mode = str(body.get("mode") or "")
        if mode:
            control.set(mode, actor="gui", reason=reason)
        return control.status()

    @app.post("/api/scheduler")
    async def api_scheduler(request: Request):
        _ui_auth(request)
        patch = await request.json()
        sched.save(patch)
        store.audit("gui", "set_scheduler", "scheduler", patch, "SUCCEEDED")
        return sched.status()

    @app.post("/api/permissions")
    async def api_permissions(request: Request):
        _ui_auth(request)
        body = await request.json()
        if "allow_send" in body:
            config.allow_send = bool(body["allow_send"])
        if "allow_manage" in body:
            config.allow_manage = bool(body["allow_manage"])
        if isinstance(body.get("owners"), list):
            config.owners = {str(x).strip() for x in body["owners"] if str(x).strip()}
        config.persist_permissions()
        store.audit("gui", "set_permissions", "config",
                    {"allow_send": config.allow_send, "allow_manage": config.allow_manage,
                     "owners": sorted(config.owners)}, "SUCCEEDED")
        return config.describe()

    @app.get("/api/groups")
    async def api_groups(request: Request, refresh: int = 0):
        """机器人实际加入的群，供控制台用勾选框选监控范围。
        以前这里是让用户手打群号，打错、或者被 4 秒一次的刷新冲掉，就变成「改了没用」。"""
        _ui_auth(request)
        try:
            rows = await box.list_groups(refresh=bool(refresh))
        except Exception as exc:
            log.warning("get_group_list 失败，退回本地缓存：%s", exc)
            rows = store.groups()
        watched = {str(g) for g in config.watch_groups}
        seen = set()
        out = []
        for r in rows:
            gid = str(r.get("group_id") or "")
            if not gid or gid in seen:
                continue
            seen.add(gid)
            out.append({"group_id": gid, "name": r.get("name") or "",
                        "member_count": r.get("member_count") or 0,
                        "watched": gid in watched})
        # 配置里有、但机器人已不在（或接口没返回）的群，也要显示出来，免得看起来像被吞了
        for gid in sorted(watched - seen):
            out.append({"group_id": gid, "name": "（不在机器人的群列表里）",
                        "member_count": 0, "watched": True})
        out.sort(key=lambda x: (not x["watched"], x["group_id"]))
        return {"groups": out, "watched": sorted(watched),
                "watch_all": not watched, "total": len(out)}

    @app.post("/api/watch")
    async def api_watch(request: Request):
        _ui_auth(request)
        body = await request.json()
        config.watch_groups = {str(x).strip() for x in (body.get("watch_groups") or []) if str(x).strip()}
        bus.watch_groups = set(config.watch_groups)
        config.persist_permissions()
        store.audit("gui", "set_watch_groups", "config", {"groups": sorted(config.watch_groups)}, "SUCCEEDED")
        return {"watch_groups": sorted(config.watch_groups)}

    @app.post("/api/keywords")
    async def api_keywords(request: Request):
        _ui_auth(request)
        body = await request.json()
        rules.save([str(k).strip() for k in (body.get("keywords") or []) if str(k).strip()])
        store.audit("gui", "set_keywords", "rules", {"count": len(rules.keywords)}, "SUCCEEDED")
        return {"keywords": rules.keywords}

    @app.get("/health")
    async def health():
        out = box.get_status()
        out["control"] = control.status()
        return JSONResponse(out)

    async def endpoint(scope, receive, send):
        request = Request(scope, receive)
        if request.headers.get("authorization") != f"Bearer {config.mcp_token}":
            return await JSONResponse({"error": "unauthorized"}, status_code=401)(scope, receive, send)
        await manager.handle_request(scope, receive, send)

    from starlette.routing import Mount
    app.router.routes.append(Mount(config.path, app=endpoint))
    app.router.routes.append(Mount(config.path + "/", app=endpoint))

    return app


# 控制类回执需要从 WS 线程发消息：这里保存主循环与 bot 引用。
_MAIN_LOOP = None
_BOT = None


def _reply(control, conversation: str, text: str):
    """控制类回执：直接发出去，不进队列、不触发模型。"""
    try:
        conv = str(conversation or "")
        if not conv.isdigit():
            log.info("control reply (bad target): %s", text)
            return
        is_group = int(conv) > 1000000000      # QQ 群号量级
        if _BOT is None or _MAIN_LOOP is None:
            log.info("control reply (bot not ready): %s", text)
            return
        # 不等待结果：这是在 WebSocket 事件线程里，阻塞会拖住事件流。
        fut = asyncio.run_coroutine_threadsafe(
            _BOT.call("send_group_msg" if is_group else "send_private_msg",
                      **({"group_id": int(conv)} if is_group else {"user_id": int(conv)}),
                      message=text),
            _MAIN_LOOP)
        fut.add_done_callback(
            lambda f: log.warning("control reply failed: %s", f.exception())
            if f.exception() else None)
    except Exception:
        log.exception("control reply failed")


def _ingest(event: dict, bus: EventBus, store: Store, control: "Control" = None):
    """Callback from the WebSocket thread: store + queue. Never touches the model."""
    # 管理员命令在入队前拦下
    if control is not None and event.get("post_type") == "message":
        raw = event.get("raw_message") or ""
        sender = str(event.get("user_id") or "")

        # 运行开关：/stop 与 /auto（带明确回执）
        target = commands.parse(raw)
        if target:
            gid = event.get("group_id") or event.get("user_id")
            if sender not in config.admins:
                _reply(control, gid, "只有管理员能切换运行状态。")
                return
            control.set(target, actor=sender, reason=raw.strip()[:80])
            _reply(control, gid, commands.reply_for(target))
            log.info("control: %s -> %s by %s", raw.strip()[:20], target, sender)
            return                  # 命令本身不入库、不入队

    record = bus.push(event)
    if record:
        try:
            store.add_message(record)
        except Exception:
            log.exception("store.add_message failed")


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    config.data_dir.mkdir(parents=True, exist_ok=True)
    uvicorn.run(create_app(), host=config.host, port=config.port, log_level="info")


if __name__ == "__main__":
    main()
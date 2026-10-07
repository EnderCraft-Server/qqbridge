"""MCP Streamable HTTP server exposing the QQ toolbox.

Design note: every tool returns immediately. There is no 180-second wait loop;
events queue in the bus and the agent pulls them when it runs.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from typing import Any

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from mcp.server.lowlevel import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.server.transport_security import TransportSecuritySettings

from .bus import EventBus
from .config import config
from .onebot import OneBot
from .control import Control
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


def build_tools(box: Toolbox, rules: Rules, sched: Scheduler):
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

    @tool("set_control", "切换运行模式：auto 自动唤醒 / stopped 强制静默 / manual 手动。仅管理员。",
          {"type": "object", "properties": {
              "actor": {"type": "string"},
              "mode": {"type": "string", "enum": ["auto", "stopped", "manual"]},
              "reason": {"type": "string", "default": ""}},
           "required": ["actor", "mode"], "additionalProperties": False})
    async def _ctl_set(actor: str, mode: str, reason: str = "", **kw):
        if str(actor) not in config.admins:
            raise Denied(f"{actor} 不是管理员，不能切换运行模式。")
        return control.set(mode, actor=actor, reason=reason)

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
    bot = OneBot(config.http, config.ws, config.token, config.ws_token)
    box = Toolbox(bot, bus, store)
    specs = build_tools(box, rules, sched)
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

    @app.on_event("startup")
    async def _startup():
        await bot.start(on_event=lambda ev: _ingest(ev, bus, store, control))
        await sched.start()
        app.state.session_task = asyncio.create_task(_run_manager())

    @app.on_event("shutdown")
    async def _shutdown():
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

    def _ui_auth(request: Request):
        token = request.headers.get("x-bridge-token") or request.query_params.get("token")
        if token != config.mcp_token:
            raise __import__("fastapi").HTTPException(401, "unauthorized")

    @app.get("/api/state")
    async def api_state(request: Request):
        _ui_auth(request)
        return {
            "config": config.describe(),
            "bot": {"self_id": bot.self_id, "ws_connected": bot.connected, "last_event_at": bot.last_event_at},
            "scheduler": sched.status(),
            "control": control.status(),
            "keywords": rules.keywords,
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

    @app.post("/api/control")
    async def api_control(request: Request):
        _ui_auth(request)
        body = await request.json()
        mode = str(body.get("mode") or "")
        return control.set(mode, actor="gui", reason=str(body.get("reason") or ""))

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


def _ingest(event: dict, bus: EventBus, store: Store, control: "Control" = None):
    """Callback from the WebSocket thread: store + queue. Never touches the model."""
    # 管理员命令在入队前拦下
    if control is not None and event.get("post_type") == "message":
        raw = event.get("raw_message") or ""
        target = commands.parse(raw)
        if target:
            sender = str(event.get("user_id") or "")
            if sender in config.admins:
                control.set(target, actor=sender, reason=raw.strip()[:80])
                log.info("control: %s -> %s by %s", raw.strip()[:20], target, sender)
                return              # 命令本身不入库、不入队
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
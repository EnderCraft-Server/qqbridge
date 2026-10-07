"""MCP tool surface. Read tools are always on; write tools need permission."""
from __future__ import annotations

import time
from typing import Any

from .bus import EventBus
from .config import config
from .onebot import OneBot, OneBotError
from .qzone import QzoneMixin
from .store import Store


class Denied(RuntimeError):
    pass


class Toolbox(QzoneMixin):
    def __init__(self, bot: OneBot, bus: EventBus, store: Store):
        self.bot = bot
        self.bus = bus
        self.store = store

    # ---------- guards ----------
    @staticmethod
    def _owner(actor: str, action: str) -> None:
        if not config.owners:
            raise Denied("未配置 OWNER_IDS，写操作全部拒绝。")
        if str(actor) not in config.owners:
            raise Denied(f"{actor} 不在 OWNER_IDS 白名单，拒绝执行 {action}。")

    @staticmethod
    def _deny(message: str) -> "Denied":
        return Denied(message)

    @staticmethod
    def _once(key: str, store: Store):
        if not key:
            raise Denied("写操作必须带 idempotency_key。")
        cached = store.seen(key)
        if cached is not None:
            return cached
        return None

    # ---------- read ----------
    def get_status(self) -> dict:
        return {
            "config": config.describe(),
            "bot": {
                "self_id": self.bot.self_id,
                "ws_connected": self.bot.connected,
                "last_event_at": self.bot.last_event_at,
            },
            "bus": self.bus.status(),
            "counts": {
                "groups": len(self.store.groups()),
                "messages": len(self.store.recent("", 1)) and None,
            },
        }

    async def list_groups(self, refresh: bool = False) -> list:
        if refresh or not self.store.groups():
            rows = await self.bot.call("get_group_list") or []
            self.store.cache_groups(rows)
        return self.store.groups()

    async def list_members(self, group_id: str, refresh: bool = True) -> list:
        if refresh or not self.store.members(group_id):
            rows = await self.bot.call("get_group_member_list", group_id=int(group_id)) or []
            self.store.cache_members(group_id, rows)
        return self.store.members(group_id)

    async def get_history(self, group_id: str, count: int = 30) -> list:
        rows = await self.bot.call("get_group_msg_history", group_id=int(group_id), count=min(count, 200)) or {}
        return (rows or {}).get("messages") or []

    def local_history(self, group_id: str, limit: int = 30) -> list:
        return self.store.recent(group_id, limit)

    def search_messages(self, query: str, group_id: str = "", limit: int = 20) -> list:
        return self.store.search(query, group_id, limit)

    def poll_events(self, cursor: int = 0, include_low: bool = True, limit: int = 50) -> dict:
        """Non-blocking. Never sleeps; returns whatever is queued right now."""
        if cursor:
            self.bus.mark_processed(cursor)
        items = self.bus.pending(include_low=include_low, limit=limit)
        return {
            "events": items,
            "count": len(items),
            "cursor": items[-1]["id"] if items else self.bus.status()["cursor"],
            "status": self.bus.status(),
        }

    def list_pending(self, include_low: bool = True) -> dict:
        return self.poll_events(0, include_low)

    # ---------- write ----------
    async def send_message(self, actor: str, group_id: str, text: str,
                           idempotency_key: str, reply_to: str = "") -> dict:
        if not config.allow_send:
            raise Denied("ALLOW_SEND=false，发送被禁用。")
        self._owner(actor, "send_message")
        cached = self._once(idempotency_key, self.store)
        if cached is not None:
            return cached
        payload: dict[str, Any] = {"group_id": int(group_id), "message": text}
        if reply_to:
            payload["message"] = f"[CQ:reply,id={reply_to}]{text}"
        result = await self.bot.call("send_group_msg", **payload)
        self.bus.note_own_send()
        out = {"state": "SUCCEEDED", "result": result}
        self.store.remember(idempotency_key, out)
        self.store.audit(actor, "send_message", group_id, {"text": text}, "SUCCEEDED", str(result))
        return out

    async def send_image(self, actor: str, group_id: str, image: str, idempotency_key: str) -> dict:
        if not config.allow_send:
            raise Denied("ALLOW_SEND=false，发送被禁用。")
        self._owner(actor, "send_image")
        cached = self._once(idempotency_key, self.store)
        if cached is not None:
            return cached
        result = await self.bot.call(
            "send_group_msg", group_id=int(group_id), message=f"[CQ:image,file={image}]"
        )
        self.bus.note_own_send()
        out = {"state": "SUCCEEDED", "result": result}
        self.store.remember(idempotency_key, out)
        self.store.audit(actor, "send_image", group_id, {"image": image}, "SUCCEEDED", str(result))
        return out

    async def manage(self, actor: str, action: str, group_id: str, idempotency_key: str,
                     user_id: str = "", duration_seconds: int = 0, value: str = "") -> dict:
        if not config.allow_manage:
            raise Denied("ALLOW_MANAGE=false，群管理被禁用。")
        self._owner(actor, action)
        cached = self._once(idempotency_key, self.store)
        if cached is not None:
            return cached

        if action == "mute":
            target = self.store.member(group_id, user_id)
            if target and target.get("role") == "owner":
                out = {"state": "REJECTED", "reason": "目标为群主，QQ 不允许禁言 owner"}
                self.store.remember(idempotency_key, out)
                self.store.audit(actor, action, f"{group_id}/{user_id}", {}, "REJECTED", out["reason"])
                return out
            result = await self.bot.call("set_group_ban", group_id=int(group_id),
                                         user_id=int(user_id), duration=int(duration_seconds))
        elif action == "unmute":
            result = await self.bot.call("set_group_ban", group_id=int(group_id),
                                         user_id=int(user_id), duration=0)
        elif action == "kick":
            result = await self.bot.call("set_group_kick", group_id=int(group_id),
                                         user_id=int(user_id), reject_add_request=False)
        elif action == "rename":
            result = await self.bot.call("set_group_name", group_id=int(group_id), group_name=value)
        elif action == "card":
            result = await self.bot.call("set_group_card", group_id=int(group_id),
                                         user_id=int(user_id), card=value)
        elif action == "whole_ban":
            result = await self.bot.call("set_group_whole_ban", group_id=int(group_id),
                                         enable=value.lower() in ("1", "true", "on", "yes"))
        elif action == "leave":
            result = await self.bot.call("set_group_leave", group_id=int(group_id))
        else:
            raise Denied(f"未知 action: {action}")

        out = {"state": "SUCCEEDED", "result": result}
        self.store.remember(idempotency_key, out)
        self.store.audit(actor, action, f"{group_id}/{user_id}", {"value": value,
                         "duration": duration_seconds}, "SUCCEEDED", str(result))
        return out

    def audit_tail(self, limit: int = 30) -> list:
        return self.store.audit_tail(limit)
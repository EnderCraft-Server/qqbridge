"""In-memory event bus with a ring buffer and trigger rules.

The whole point: the model never waits. Events accumulate here (cheap, no model
cost) and the agent pulls whatever is pending whenever it happens to run.
"""
from __future__ import annotations

import itertools
import logging
import threading
import time
from collections import deque

log = logging.getLogger("qqbridge.bus")


class EventBus:
    """Thread-safe ring buffer of normalised message events."""

    def __init__(self, size: int = 500, cooldown_seconds: int = 0, self_id: str = "",
                 rules=None, watch_groups=None, control=None, store=None):
        self._lock = threading.RLock()
        self._events = deque(maxlen=size)
        self._dropped = 0            # 被环形缓冲挤掉的条数（可观测）
        # Durable id sequence + cursor. Both live in the store when one is supplied, so a
        # restart resumes where it left off instead of rewinding to 1. Without this, an
        # An external poller keeps its own monotonic cursor and sees every
        # new id as "already processed", and silently stops waking anyone until it is restarted.
        self._store = store
        # Seed from persisted state; on the first run after an upgrade, fall back to the highest
        # event id already in the message log so the sequence never steps backwards even once.
        self._seq_value = max(self._load_int("bus.seq", 0), self._seed_from_history())
        self._seq = itertools.count(self._seq_value + 1)
        if self._seq_value:
            self._save_int("bus.seq", self._seq_value)
        self._processed = self._load_int("bus.cursor", 0)   # cursor: highest id handed to the model
        self._last_own_send = 0.0
        self.cooldown = cooldown_seconds
        self.self_id = self_id
        self.rules = rules
        # 只监控这些群；空集合表示全部群
        self.watch_groups = set(str(g) for g in (watch_groups or []))
        # 运行时控制（/stop 挂起）。为 None 时视为不暂停。
        self.control = control
        self._pending_high: list = []   # @me
        self._pending_mid: list = []    # keyword hits
        self._pending_low: list = []    # cooldown chatter

    # ---------- durable state ----------
    def _load_int(self, key: str, default: int = 0) -> int:
        if self._store is None:
            return default
        try:
            return max(0, int(self._store.get_state(key, default)))
        except (TypeError, ValueError):
            return default

    def _seed_from_history(self) -> int:
        """Highest event id already in the message log (0 when unavailable)."""
        if self._store is None or not hasattr(self._store, "max_event_id"):
            return 0
        try:
            return max(0, int(self._store.max_event_id()))
        except (TypeError, ValueError):
            return 0

    def _save_int(self, key: str, value: int) -> None:
        if self._store is not None:
            self._store.set_state(key, int(value))

    def _next_id(self) -> int:
        """Allocate the next event id, durably, so ids never repeat across restarts."""
        self._seq_value += 1
        self._save_int("bus.seq", self._seq_value)
        return next(self._seq)

    # ---------- ingest ----------
    def push(self, event: dict) -> dict | None:
        """Normalise a OneBot post_type=message event. Returns the stored record."""
        if event.get("post_type") != "message":
            return None
        if str(event.get("self_id") or "") and str(event["self_id"]) != (self.self_id or str(event["self_id"])):
            pass  # multiple accounts not supported; keep the event anyway
        msg = event.get("message")
        text = event.get("raw_message") or ""
        segments = []
        if isinstance(msg, list):
            for seg in msg:
                kind = seg.get("type")
                data = seg.get("data") or {}
                if kind == "text":
                    segments.append({"type": "text", "text": data.get("text", "")})
                elif kind == "at":
                    segments.append({"type": "at", "qq": str(data.get("qq", ""))})
                elif kind == "image":
                    segments.append({"type": "image", "url": data.get("url", ""), "file": data.get("file", "")})
                elif kind == "reply":
                    segments.append({"type": "reply", "onebot_message_id": str(data.get("id", ""))})
                else:
                    segments.append({"type": kind or "unknown"})
            if not text:
                text = "".join(s.get("text", "") for s in segments if s.get("type") == "text")

        sender = event.get("sender") or {}
        topic = str(event.get("message_type") or "group")
        record = {
            "id": self._next_id(),
            "at": time.time(),
            "platform": "qq",
            "message_type": topic,
            "group_id": str(event.get("group_id") or ""),
            "user_id": str(event.get("user_id") or ""),
            "sender": sender.get("card") or sender.get("nickname") or str(event.get("user_id") or ""),
            "role": sender.get("role") or "",
            "message_id": str(event.get("message_id") or ""),
            "text": text,
            "segments": segments,
            "is_self": str(event.get("user_id") or "") == (self.self_id or ""),
        }
        record["mentions_me"] = bool(self.self_id) and any(
            s.get("type") == "at" and s.get("qq") == self.self_id for s in segments
        )
        with self._lock:
            hit = self.rules.match(record["text"]) if self.rules else None
            record["keyword_hit"] = hit
            if self.watch_groups and record["group_id"] not in self.watch_groups and record["message_type"] == "group":
                return record          # 落库但绝不入队，完全不唤醒
            if len(self._events) == self._events.maxlen:
                self._dropped += 1
                if self._dropped == 1 or self._dropped % 100 == 0:
                    log.warning("环形缓冲已满（%d），累计挤掉 %d 条；完整历史在 SQLite 里",
                                self._events.maxlen, self._dropped)
            self._events.append(record)
            paused = bool(self.control) and self.control.paused()
            if not record["is_self"] and not paused:
                if record["mentions_me"]:
                    self._pending_high.append(record["id"])
                elif hit:
                    self._pending_mid.append(record["id"])
                elif self.cooldown and (time.time() - self._last_own_send) > self.cooldown:
                    self._pending_low.append(record["id"])
        return record

    def note_own_send(self):
        with self._lock:
            self._last_own_send = time.time()

    # ---------- read ----------
    def _get(self, event_id: int) -> dict | None:
        for rec in self._events:
            if rec["id"] == event_id:
                return rec
        return None

    def status(self) -> dict:
        with self._lock:
            events = list(self._events)
            high = list(self._pending_high)
            mid = list(self._pending_mid)
            low = list(self._pending_low)
        return {
            "stored": len(events),
            "last_id": events[-1]["id"] if events else 0,
            "cursor": self._processed,
            "pending_total": len(high) + len(mid) + len(low),
            "dropped": self._dropped,
            "pending_high": len(high),
            "pending_mid": len(mid),
            "pending_low": len(low),
            "last_own_send": self._last_own_send,
            "cooldown_seconds": self.cooldown,
            "watch_groups": sorted(self.watch_groups),
            "paused": bool(self.control) and self.control.paused(),
            "mode": self.control.mode if self.control else "auto",
        }

    def pending(self, include_low: bool = True, limit: int = 50) -> list:
        """Return unprocessed events; never blocks."""
        with self._lock:
            ids = list(self._pending_high) + list(self._pending_mid)
            if include_low:
                ids += list(self._pending_low)
            ids = ids[:limit]
        out = []
        for i in ids:
            rec = self._get(i)
            if rec:
                out.append(rec)
        return out

    def since(self, cursor: int = 0, limit: int = 100, include_low: bool = True) -> list:
        """**纯读**：返回 id > cursor 的事件。include_low=False 时只给"值得处理"的。

        这里永不修改状态 —— 游标的推进只发生在 mark_processed。
        """
        with self._lock:
            events = [e for e in self._events if e["id"] > cursor]
        if not include_low:
            with self._lock:
                worth = set(self._pending_high) | set(self._pending_mid)
            events = [e for e in events if e["id"] in worth or e["is_self"]]
        return events[:limit]

    def mark_processed(self, through_id: int) -> int:
        """推进游标。只进不退；跳过尚未处理的事件会在日志里留警告。"""
        through_id = int(through_id)
        with self._lock:
            # 严格小于 through_id 才算「被跳过」——through_id 正是这条刚处理完的事件，
            # 用 <= 会把它自己也列进去，日志里就成了「跳过了 1 条：[它自己]」的假警报
            skipped = [i for i in (self._pending_high + self._pending_mid + self._pending_low)
                       if self._processed < i < through_id]
            if skipped:
                log.warning("mark_processed(%d) 跳过了 %d 条未处理事件：%s",
                            through_id, len(skipped), skipped[:10])
            self._processed = max(self._processed, through_id)
            self._pending_high = [i for i in self._pending_high if i > through_id]
            self._pending_mid = [i for i in self._pending_mid if i > through_id]
            self._pending_low = [i for i in self._pending_low if i > through_id]
            self._save_int("bus.cursor", self._processed)
            return self._processed
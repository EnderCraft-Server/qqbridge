"""In-memory event bus with a ring buffer and trigger rules.

The whole point: the model never waits. Events accumulate here (cheap, no model
cost) and the agent pulls whatever is pending whenever it happens to run.
"""
from __future__ import annotations

import itertools
import json
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
        self._dropped = 0            # events squeezed out of the ring buffer (observability)
        # Durable id sequence + cursor. Both live in the store when one is supplied, so a
        # restart resumes where it left off instead of rewinding to 1. Without this, an
        # external poller keeps its own monotonic cursor, sees every new id as "already
        # processed", and silently stops waking anyone until it is restarted.
        self._store = store
        # Event id: only for display/audit and the external poller's cursor. It **no longer
        # tracks "how far we processed"**.
        self._seq_value = max(self._load_int("bus.seq", 0), self._seed_from_history())
        self._seq = itertools.count(self._seq_value + 1)
        if self._seq_value:
            self._save_int("bus.seq", self._seq_value)

        # ---------- processed watermark: a timestamp, not an id cursor ----------
        # Why the id cursor was dropped: it was a monotonic max() and it was persisted. Any
        # mark_processed(large number) — the model inventing one, or some other number being
        # passed as the argument — pushed the watermark into a future that could never be
        # reached and cleared the queue at the same time, and then it **went permanently
        # silent**: every read API looked fine, the bot simply never spoke again, and a
        # restart could not rescue it (reproduced in testing).
        #
        # A timestamp closes that path structurally:
        #   1) the watermark can only be pushed to the time of **a message that really exists
        #      in the buffer**; it cannot go past it;
        #   2) push guarantees `at` is strictly increasing and unique, so no ties can cause a
        #      missed or repeated read.
        self._seen_at = self._load_float("bus.seen_at", 0.0)
        # ---------- one watermark per session (group / private chat) ----------
        # There used to be a single global watermark: once group A was handled and the
        # watermark moved, older messages in group B were never seen again — and the context
        # was the global last N messages, so group A's topic leaked into group B's judgement
        # (reproduced: it replied with another group's meme).
        # Now each session has its own watermark and they do not affect each other.
        self._seen_by_key: dict[str, float] = self._load_json("bus.seen_by_key", {})
        self._last_ts = 0.0          # at of the previous event; keeps `at` strictly increasing

        self._last_own_send = 0.0
        self.cooldown = cooldown_seconds
        self.self_id = self_id
        self.rules = rules
        # Only watch these groups; an empty set means all groups.
        self.watch_groups = set(str(g) for g in (watch_groups or []))
        # Runtime control (/stop suspension). None means not paused.
        self.control = control
        # Note: the old _pending_high/mid/low id lists are gone. They only ever grew (ids
        # evicted from the ring buffer stayed in them forever), and priority can be computed
        # from the event itself — no need to maintain a copy that rots.

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

    def _load_float(self, key: str, default: float = 0.0) -> float:
        if self._store is None:
            return default
        try:
            return max(0.0, float(self._store.get_state(key, default)))
        except (TypeError, ValueError):
            return default

    def _save_float(self, key: str, value: float) -> None:
        if self._store is not None:
            self._store.set_state(key, float(value))

    def _load_json(self, key: str, default):
        if self._store is None:
            return default
        try:
            raw = self._store.get_state(key, None)
            if raw in (None, ""):
                return default
            val = json.loads(raw) if isinstance(raw, str) else raw
            # Guard against a corrupt or legacy value of the wrong shape.
            return val if isinstance(val, type(default)) else default
        except (TypeError, ValueError):
            return default

    def _save_json(self, key: str, value) -> None:
        if self._store is not None:
            self._store.set_state(key, json.dumps(value, ensure_ascii=False))

    def _next_id(self) -> int:
        """Allocate the next event id, durably, so ids never repeat across restarts."""
        # Persist before handing the id out, so a crash right after cannot hand it out twice.
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
        # at must be **strictly increasing and unique**: the watermark is a timestamp, so if
        # two events landed on the same value, a > comparison would miss one of them.
        # time.time() does not have enough resolution on Windows, hence the explicit nudge.
        now = time.time()
        with self._lock:
            if now <= self._last_ts:
                now = self._last_ts + 1e-6
            self._last_ts = now
        record = {
            "id": self._next_id(),
            "at": now,
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
                return record          # stored, but never queued: no wakeup at all
            if len(self._events) == self._events.maxlen:
                self._dropped += 1
                # Log the first drop and then every 100th, so a full buffer cannot flood the log.
                if self._dropped == 1 or self._dropped % 100 == 0:
                    log.warning("环形缓冲已满（%d），累计挤掉 %d 条；完整历史在 SQLite 里",
                                self._events.maxlen, self._dropped)
            self._events.append(record)
            # No more pushing ids into _pending_*: pending-ness is decided entirely by
            # "at > watermark", and priority is computed from the event fields. One less copy
            # that can rot, one less class of inconsistency.
        return record

    def note_own_send(self):
        with self._lock:
            # Remember when we last spoke, for the cooldown logic.
            self._last_own_send = time.time()

    def backfill(self, records: list) -> int:
        """Feed historical messages from the DB back into the ring buffer at startup, so the
        console has something to show from the moment it boots.

        After a restart the buffer is empty and the console shows "no messages", which looks
        like the bot is not receiving anything — while the DB is actually intact, it just was
        not reloaded.

        Both kinds of records are accepted:
          id <= cursor  already processed, displayed as usual
          id >  cursor  left over from before the previous shutdown; **displayed only, never
                        replayed** — answering a message from hours ago after a restart is
                        stranger than missing it.
        So the watermark is pushed past them as well, keeping autoreply from treating them as
        new messages and answering them all over again.

        The group filter matches push, otherwise the console would show groups the live path
        never sees.
        """
        added: list = []
        unprocessed = 0
        with self._lock:
            have = {e["id"] for e in self._events}
            for rec in sorted(records, key=lambda r: r.get("id") or 0):
                eid = int(rec.get("id") or 0)
                # Skip junk ids and anything already buffered (backfill is idempotent).
                if eid <= 0 or eid in have:
                    continue
                gid = str(rec.get("group_id") or "")
                if self.watch_groups and gid not in self.watch_groups \
                        and rec.get("message_type") == "group":
                    continue
                if rec.get("at", 0) > self._seen_at:
                    unprocessed += 1
                added.append(rec)
                have.add(eid)
            if added:
                # Must re-sort the whole thing: new events may already be in the buffer, and
                # appending history directly would give [new, old, old...], scrambling the
                # console order.
                merged = sorted(list(self._events) + added, key=lambda r: r.get("id") or 0)
                self._events.clear()
                self._events.extend(merged[-self._events.maxlen:])
            # Backfilled history always counts as "already seen": answering a message from
            # hours ago after a restart is stranger than missing it. Pushing the watermark to
            # the newest historical timestamp is enough.
            newest_at = max((r.get("at") or 0.0) for r in self._events) if self._events else 0.0
            if newest_at > self._seen_at:
                self._seen_at = newest_at
                self._save_float("bus.seen_at", newest_at)
        if added:
            log.info("启动回填 %d 条历史消息到控制台缓冲%s", len(added),
                     f"，其中 {unprocessed} 条是上次没处理完的（只展示、不补回）"
                     if unprocessed else "")
        return len(added)

    # ---------- sessions (group / private chat) ----------
    @staticmethod
    def key_of(ev: dict) -> str:
        """Which session a message belongs to. Group chat keys on the group id, private chat
        on the other side's QQ number."""
        return str(ev.get("group_id") or ev.get("user_id") or "")

    def _seen_for(self, key: str) -> float:
        """This session's watermark. Falls back to the global one when there is no record
        (so an upgrade does not replay history)."""
        if key in self._seen_by_key:
            return self._seen_by_key[key]
        return self._seen_at

    # ---------- batch views (the reply loop uses these, never per-message) ----------
    def unseen(self, limit: int = 200, key: str | None = None) -> list:
        """Messages newer than the watermark, in ascending time order.

        This is the only timeline the reply loop uses: it takes a whole segment at once
        instead of one message at a time.
        Our own messages are excluded — nothing to answer there.
        When key is given, only that session is considered.
        """
        with self._lock:
            cut = self._seen_for(key) if key is not None else self._seen_at
            return [e for e in self._events
                    if e["at"] > cut and not e["is_self"]
                    and (key is None or self.key_of(e) == key)][:limit]

    def unseen_by_key(self, limit: int = 200) -> dict:
        """Unread messages bucketed by session: {key: [events...]}, each bucket in time order.

        The reply loop handles the buckets one at a time — one group is one independent
        conversation, no more merging every group's messages into one batch for the model.
        """
        with self._lock:
            # Walking the buffer in order keeps each bucket in ascending time order.
            out: dict[str, list] = {}
            for e in self._events:
                if e.get("is_self"):
                    continue
                k = self.key_of(e)
                if (e.get("at") or 0.0) <= self._seen_for(k):
                    continue
                out.setdefault(k, []).append(e)
        return {k: v[:limit] for k, v in out.items()}

    def context(self, limit: int = 40, max_age: float = 0.0,
                key: str | None = None) -> list:
        """The context shown to the model: the last limit messages (**including our own**), in
        id order.

        Only looking at "new messages" is not enough — you have to know what is being talked
        about to decide whether to join.
        When key is given, **only this session's** messages are taken: group A's context must
        never appear in group B's judgement.

        max_age > 0 cuts it again by time: keep only the last max_age seconds.
        A count-only limit has a trap — once the group goes quiet, 40 messages can span hours,
        and the model then answers today's message with hours-old context, which reads as a
        non-sequitur.
        """
        with self._lock:
            events = [e for e in self._events if key is None or self.key_of(e) == key]
        # Copy under the lock, then filter outside it: the time check touches no shared state.
        if max_age > 0:
            cut = time.time() - max_age
            fresh_ev = [e for e in events if (e.get("at") or 0) >= cut]
            # If the time cut clears everything, fall back to count: some old context
            # beats none.
            events = fresh_ev or events
        return events[-limit:]

    def _save_by_key(self) -> None:
        # Groups keep piling up, so cap this table; keep the 200 most recently active ones.
        if len(self._seen_by_key) > 200:
            keep = sorted(self._seen_by_key.items(), key=lambda kv: kv[1])[-200:]
            self._seen_by_key = dict(keep)
        self._save_json("bus.seen_by_key", self._seen_by_key)

    def mark_seen_at(self, through_at: float, key: str | None = None) -> float:
        """Push the watermark to through_at — **never past the newest event in the buffer**.

        This is the safety valve of the whole mechanism. It used to be
        `self._processed = max(_, through_id)` with no upper bound at all, so one invented
        large id pushed the watermark into the future, cleared the queue on the way, and was
        persisted — the bot went permanently silent from then on.
        Now even an absurd value is clamped to "the newest message received", which at worst
        means this batch no longer needs an answer; it can never swallow messages that have
        not arrived yet.
        """
        with self._lock:
            newest = max((e["at"] for e in self._events
                          if key is None or self.key_of(e) == key), default=0.0)
            target = min(float(through_at), newest)
            if key is None:
                if target > self._seen_at:
                    self._seen_at = target
                    self._save_float("bus.seen_at", self._seen_at)
                return self._seen_at
            cur = self._seen_by_key.get(key, self._seen_at)
            if target > cur:
                self._seen_by_key[key] = target
                self._save_by_key()
            return self._seen_by_key.get(key, self._seen_at)

    def mark_looked(self, through_id: int, key: str | None = None) -> float:
        """Advance the watermark by event id (used by the batch path).

        **If the id is not found, do nothing** — that is the key point: ids the caller gets
        always come from poll_events / unseen, so the normal path is guaranteed to find them;
        not finding one means the id was invented, and the only correct reaction is to stay
        put, not to "push it roughly over there".
        """
        rec = self._get(int(through_id))
        if rec is None:
            log.warning("mark_looked(%s)：缓冲里没有这个 id，水位线不动", through_id)
            return self._seen_at
        # With no explicit key, use this message's own session, so group A's message cannot
        # move group B's watermark.
        return self.mark_seen_at(rec["at"], key or self.key_of(rec))

    # ---------- read ----------
    def _get(self, event_id: int) -> dict | None:
        for rec in self._events:
            if rec["id"] == event_id:
                return rec
        return None

    def status(self) -> dict:
        with self._lock:
            events = list(self._events)
            unseen = [e for e in events if e["at"] > self._seen_at and not e["is_self"]]
            # Priority is computed on the spot; no dependency on a copy that rots.
            high = [e for e in unseen if e.get("mentions_me")]
            mid = [e for e in unseen if not e.get("mentions_me") and e.get("keyword_hit")]
            low = [e for e in unseen if not e.get("mentions_me") and not e.get("keyword_hit")]
            seen_at = self._seen_at
        return {
            "stored": len(events),
            "last_id": events[-1]["id"] if events else 0,
            # Compatibility field: the id of the event sitting at the watermark (0 if none).
            # The real "how far we processed" is seen_at (a timestamp) now.
            "cursor": next((e["id"] for e in reversed(events) if e["at"] <= seen_at), 0),
            "seen_at": seen_at,
            "last_at": events[-1]["at"] if events else 0.0,
            "pending_total": len(unseen),
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
        """Events after the watermark that are worth handling; non-blocking.

        Priority (@ > keyword > ordinary chatter) is computed on the spot, and **our own
        messages are always included** so the caller (poll_events) can filter as needed —
        same as before.
        include_low=False returns only high/medium priority.
        """
        with self._lock:
            cut = self._seen_at
            events = [e for e in self._events if e["at"] > cut]
        high = [e for e in events if e.get("mentions_me")]
        mid = [e for e in events if not e.get("mentions_me") and e.get("keyword_hit")]
        low = [e for e in events if not e.get("mentions_me") and not e.get("keyword_hit")]
        out = high + mid + (low if include_low else [])
        return out[:max(0, int(limit))]

    def since(self, cursor: int = 0, limit: int = 100, include_low: bool = True) -> list:
        """**Pure read**: return events with id > cursor. include_low=False gives only the
        "worth handling" ones.

        This never mutates state — the cursor only advances in mark_processed.
        """
        with self._lock:
            events = [e for e in self._events if e["id"] > cursor]
        # Our own messages always count as worth handling, so external pollers still see them.
        if not include_low:
            events = [e for e in events
                      if e.get("mentions_me") or e.get("keyword_hit") or e["is_self"]]
        return events[:limit]

    def mark_processed(self, through_id: int) -> float:
        """Advance the watermark by event id (used by the MCP poll_events path).

        **If the id is not found, do nothing.** This is the core guarantee after dropping the
        id cursor: `max(processed, through_id)` used to have no upper bound, so an invented
        99999999 pushed the watermark into the future, cleared the queue, and persisted it —
        the bot never received a message again and no restart could save it (reproduced in
        testing).
        Now the watermark can only land on **a message that really exists in the buffer**; an
        invented id is simply ignored.
        """
        rec = self._get(int(through_id))
        if rec is None:
            log.warning("mark_processed(%s)：缓冲里没有这个 id，水位线不动"
                        "（编造/过期的 id 不再能推高水位）", through_id)
            return self._seen_at
        return self.mark_seen_at(rec["at"])
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
        self._dropped = 0            # 被环形缓冲挤掉的条数（可观测）
        # Durable id sequence + cursor. Both live in the store when one is supplied, so a
        # restart resumes where it left off instead of rewinding to 1. Without this, an
        # An external poller keeps its own monotonic cursor and sees every
        # new id as "already processed", and silently stops waking anyone until it is restarted.
        self._store = store
        # 事件 id：只用于展示/审计/外部 poller 的游标，**不再承担"处理到哪了"的职责**。
        self._seq_value = max(self._load_int("bus.seq", 0), self._seed_from_history())
        self._seq = itertools.count(self._seq_value + 1)
        if self._seq_value:
            self._save_int("bus.seq", self._seq_value)

        # ---------- 已处理水位线：时间戳，不是 id 游标 ----------
        # 为什么弃用 id 游标：它是单调的 max()，而且会被持久化。任何一次
        # mark_processed(大数) —— 模型编一个、或者把别的数字当参数传进来 ——
        # 都会把水位推到未来永远够不到的地方，同时清空队列，然后**永久静默**：
        # 读接口一切正常，机器人再也不说话，重启也救不回来（实测复现过）。
        #
        # 换成时间戳之后，结构上就堵死了这条路：
        #   1) 水位只能推到**缓冲里真实存在的那条消息**的时间上，推不过去；
        #   2) push 保证 at 严格递增且互不相同，所以不存在并列导致的漏看/重看。
        self._seen_at = self._load_float("bus.seen_at", 0.0)
        # ---------- 按会话（群 / 私聊）各记各的水位线 ----------
        # 原来只有一个全局水位：A 群处理完把水位推过去，B 群里比它旧的消息
        # 就再也不会被看到了 —— 而且上下文取的是全局最近 N 条，
        # A 群的话题会混进 B 群的判断里（实测回复过另一个群的梗）。
        # 现在每个会话一份水位线，互不影响。
        self._seen_by_key: dict[str, float] = self._load_json("bus.seen_by_key", {})
        self._last_ts = 0.0          # 上一个事件的 at，用来保证严格递增

        self._last_own_send = 0.0
        self.cooldown = cooldown_seconds
        self.self_id = self_id
        self.rules = rules
        # 只监控这些群；空集合表示全部群
        self.watch_groups = set(str(g) for g in (watch_groups or []))
        # 运行时控制（/stop 挂起）。为 None 时视为不暂停。
        self.control = control
        # 注：原来的 _pending_high/mid/low 三个 id 列表已删除。
        # 它们只增不减（被挤出环形缓冲的 id 永远留在里面），
        # 而且优先级完全可以从事件自身算出来 —— 没必要维护一份会腐烂的副本。

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
            return val if isinstance(val, type(default)) else default
        except (TypeError, ValueError):
            return default

    def _save_json(self, key: str, value) -> None:
        if self._store is not None:
            self._store.set_state(key, json.dumps(value, ensure_ascii=False))

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
        # at 必须**严格递增且互不相同**：水位线是时间戳，要是两条撞上同一个值，
        # 用 > 比较就会漏掉其中一条。time.time() 在 Windows 上分辨率不够，
        # 所以显式兜一下。
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
                return record          # 落库但绝不入队，完全不唤醒
            if len(self._events) == self._events.maxlen:
                self._dropped += 1
                if self._dropped == 1 or self._dropped % 100 == 0:
                    log.warning("环形缓冲已满（%d），累计挤掉 %d 条；完整历史在 SQLite 里",
                                self._events.maxlen, self._dropped)
            self._events.append(record)
            # 不再往 _pending_* 里塞 id：待处理与否完全由「at > 水位线」决定，
            # 优先级从事件字段现算。少一份会腐烂的副本，就少一类不一致。
        return record

    def note_own_send(self):
        with self._lock:
            self._last_own_send = time.time()

    def backfill(self, records: list) -> int:
        """启动时把库里的历史消息灌回环形缓冲，让控制台一开机就有东西看。

        重启后缓冲是空的，控制台显示「暂无消息」，看着像机器人没在收消息 ——
        其实库里一条不少，只是没灌回来。

        两种记录都收：
          id <= cursor  已经处理过的，照常展示
          id >  cursor  上次没来得及处理的（正好在关机前那一下），**只展示、不补回**
        —— 重启后去回一条几小时前的消息，比漏掉它更奇怪。
        所以顺手把游标推过去，避免 autoreply 把它们当新消息重新回一遍。

        群过滤条件和 push 保持一致，否则控制台会看到实时路径永远看不到的群。
        """
        added: list = []
        unprocessed = 0
        with self._lock:
            have = {e["id"] for e in self._events}
            for rec in sorted(records, key=lambda r: r.get("id") or 0):
                eid = int(rec.get("id") or 0)
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
                # 必须整体重排：新事件可能已经先进了缓冲，
                # 直接 append 历史会变成 [新, 旧, 旧…]，控制台里顺序全乱。
                merged = sorted(list(self._events) + added, key=lambda r: r.get("id") or 0)
                self._events.clear()
                self._events.extend(merged[-self._events.maxlen:])
            # 回填的历史一律"已看过"：重启后去回一条几小时前的消息，
            # 比漏掉它更奇怪。水位线推到最新那条历史的时间即可。
            newest_at = max((r.get("at") or 0.0) for r in self._events) if self._events else 0.0
            if newest_at > self._seen_at:
                self._seen_at = newest_at
                self._save_float("bus.seen_at", newest_at)
        if added:
            log.info("启动回填 %d 条历史消息到控制台缓冲%s", len(added),
                     f"，其中 {unprocessed} 条是上次没处理完的（只展示、不补回）"
                     if unprocessed else "")
        return len(added)

    # ---------- 会话（群 / 私聊） ----------
    @staticmethod
    def key_of(ev: dict) -> str:
        """一条消息属于哪个会话。群聊用群号，私聊用对方 QQ 号。"""
        return str(ev.get("group_id") or ev.get("user_id") or "")

    def _seen_for(self, key: str) -> float:
        """这个会话的水位线。没有记录时退回全局水位（升级时不重放历史）。"""
        if key in self._seen_by_key:
            return self._seen_by_key[key]
        return self._seen_at

    # ---------- 批量视图（接话循环用这个，不再按条取） ----------
    def unseen(self, limit: int = 200, key: str | None = None) -> list:
        """水位线之后新来的消息，按时间升序。

        这是接话循环唯一的时间线：一次拿到一整段，而不是一条一条地取。
        不包含自己的发言 —— 自己说的不用回。
        key 给定时只看这个会话。
        """
        with self._lock:
            cut = self._seen_for(key) if key is not None else self._seen_at
            return [e for e in self._events
                    if e["at"] > cut and not e["is_self"]
                    and (key is None or self.key_of(e) == key)][:limit]

    def unseen_by_key(self, limit: int = 200) -> dict:
        """按会话分桶的未读消息：{key: [事件…]}，每个桶按时间升序。

        接话循环按桶逐个处理 —— 一个群一段独立会话，
        不再把所有群的消息揉成一批交给模型。
        """
        with self._lock:
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
        """给模型看的上下文：最近 limit 条（**含自己说过的话**），按 id 升序。

        只看「新消息」是不够的 —— 得知道在聊什么，才知道这句要不要接。
        key 给定时**只取这个会话的**：A 群的上下文绝不能出现在 B 群的判断里。

        max_age > 0 时再按时间切一刀：只保留最近 max_age 秒内的。
        只按条数限制有个坑 —— 群里一安静，40 条能横跨好几小时，
        模型就拿着几小时前的语境去接现在的话，看起来像答非所问。
        """
        with self._lock:
            events = [e for e in self._events if key is None or self.key_of(e) == key]
        if max_age > 0:
            cut = time.time() - max_age
            fresh_ev = [e for e in events if (e.get("at") or 0) >= cut]
            # 全被时间切没了就退回按条数：宁可给点旧上下文，也别给空
            events = fresh_ev or events
        return events[-limit:]

    def _save_by_key(self) -> None:
        # 群会越来越多，别让这张表无限长；留最近活跃的 200 个
        if len(self._seen_by_key) > 200:
            keep = sorted(self._seen_by_key.items(), key=lambda kv: kv[1])[-200:]
            self._seen_by_key = dict(keep)
        self._save_json("bus.seen_by_key", self._seen_by_key)

    def mark_seen_at(self, through_at: float, key: str | None = None) -> float:
        """把水位线推到 through_at —— **永远不会超过缓冲里最新那条的时间**。

        这是整套机制的安全阀。以前是 `self._processed = max(_, through_id)`，
        没有任何上界，于是一个编出来的大 id 就能把水位推到未来、
        顺手清空队列、还持久化下来 —— 机器人从此永久静默。
        现在就算传进来一个荒谬的值，也只会被钳到"最新收到的那条"为止，
        最多是让当前这批不需要再回了，绝不会吃掉还没到的消息。
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
        """按事件 id 推进水位线（批处理路径用）。

        **id 找不到就什么都不做** —— 这是关键：调用方拿到的 id 一定来自
        poll_events / unseen，所以正常路径必然找得到；找不到说明那个 id 是编的，
        这时唯一正确的反应是不动，而不是"大概推到那儿吧"。
        """
        rec = self._get(int(through_id))
        if rec is None:
            log.warning("mark_looked(%s)：缓冲里没有这个 id，水位线不动", through_id)
            return self._seen_at
        # 没显式给 key 就用这条消息自己的会话，避免拿 A 群的消息推 B 群的水位
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
            # 优先级现算，不再依赖一份会腐烂的 id 列表
            high = [e for e in unseen if e.get("mentions_me")]
            mid = [e for e in unseen if not e.get("mentions_me") and e.get("keyword_hit")]
            low = [e for e in unseen if not e.get("mentions_me") and not e.get("keyword_hit")]
            seen_at = self._seen_at
        return {
            "stored": len(events),
            "last_id": events[-1]["id"] if events else 0,
            # 兼容字段：报水位线处那条事件的 id（没有就是 0）。
            # 真正的"处理到哪了"现在是 seen_at（时间戳）。
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
        """水位线之后、值得处理的事件；非阻塞。

        优先级（@ > 关键词 > 普通闲聊）现算，并且**总是包含自己发言**，
        调用方（poll_events）自己按需要过滤 —— 这一点和以前一致。
        include_low=False 时只给高/中优先级。
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
        """**纯读**：返回 id > cursor 的事件。include_low=False 时只给"值得处理"的。

        这里永不修改状态 —— 游标的推进只发生在 mark_processed。
        """
        with self._lock:
            events = [e for e in self._events if e["id"] > cursor]
        if not include_low:
            events = [e for e in events
                      if e.get("mentions_me") or e.get("keyword_hit") or e["is_self"]]
        return events[:limit]

    def mark_processed(self, through_id: int) -> float:
        """按事件 id 推进水位线（MCP poll_events 那条路径用）。

        **找不到这个 id 就什么都不做。** 这是弃用 id 游标之后的核心保证：
        以前 `max(processed, through_id)` 没有任何上界，编一个 99999999 就能
        把水位推到未来、清空队列、再持久化 —— 机器人从此永远收不到消息，
        连重启都救不回来（实测复现过）。
        现在水位只能落在**缓冲里真实存在的那条消息**上，编的 id 找不到就直接忽略。
        """
        rec = self._get(int(through_id))
        if rec is None:
            log.warning("mark_processed(%s)：缓冲里没有这个 id，水位线不动"
                        "（编造/过期的 id 不再能推高水位）", through_id)
            return self._seen_at
        return self.mark_seen_at(rec["at"])
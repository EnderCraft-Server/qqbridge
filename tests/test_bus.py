# -*- coding: utf-8 -*-
"""Event bus self-test: ring buffer, cursor semantics, startup backfill.

The backfill case is new in this round: the moment the process restarts, the console
shows "暂无消息" (no messages), which looks like the bot is not receiving anything
while the database actually holds everything -- it just was not loaded back into the
buffer. The crucial part is that backfill must **not** let old messages be answered
a second time.

Run: python tests/test_bus.py
"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from qqbridge.bus import EventBus
from qqbridge.rules import Rules
from qqbridge.store import Store

ok = []
tmp = Path(tempfile.mkdtemp(prefix="qqbridge_bus_"))


def event(i, text="hi", gid="1060667115", uid="1001", is_self=False, mentions=False, segs=None):
    return {"post_type": "message", "message_type": "group", "group_id": gid,
            "user_id": uid, "message_id": str(i), "raw_message": text,
            "message": segs if segs is not None else [{"type": "text", "data": {"text": text}}],
            "sender": {"nickname": "群友", "card": "群友", "role": "member"},
            "self_id": 461282697}


# ---------- 1. Basic ingestion + cursor ----------
store = Store(tmp / "a.sqlite3")
rules = Rules(path=tmp / "kw.json", keywords=["开服"])
bus = EventBus(size=10, cooldown_seconds=90, rules=rules,
               watch_groups=["1060667115"], store=store)
# self_id decides whether an @ segment counts as "mentions me", so it has to match
# the self_id carried by the events.
bus.self_id = "461282697"
for i in range(5):
    rec = bus.push(event(i))
    # The bus never persists anything itself; the caller writes each record to the store.
    store.add_message(rec)
assert bus.status()["stored"] == 5
assert bus.since(0)[0]["id"] == 1, "id 应当从 1 开始"
assert bus.status()["pending_low"] == 5, "冷却期已过，普通闲聊该进低位队列"
assert not [e for e in bus.since(0) if e["mentions_me"]], "没人 @ 的时候不该判成 @我"
seen_before = bus.status()["seen_at"]
bus.mark_processed(3)
assert bus.status()["cursor"] == 3, "按 id 标完，兼容字段应当能映回那个 id"
assert bus.status()["seen_at"] > seen_before, "水位线（时间戳）该往前走"
assert len(bus.since(3)) == 2, "游标之后应当只剩 2 条"
assert bus.since(0)[0]["id"] == 1, "since() 是纯读，不该因为水位线推进而看不到历史"
# Core guarantee of the watermark model: a fabricated id cannot move the watermark
before = bus.status()["seen_at"]
bus.mark_processed(99999999)
assert bus.status()["seen_at"] == before, "不存在的 id 绝不能推高水位线"
assert len(bus.unseen()) == 2, "推不动，所以剩下的两条还在"
ok.append("入库 / 自增 id / 水位线推进 / 编造 id 推不动 / since 纯读 都正确")

# ---------- 2. Groups outside the watch scope: stored, but not queued ----------
other = bus.push(event(99, gid="999999"))
store.add_message(other)
assert bus.status()["stored"] == 5, "范围外的群不该进环形缓冲"
assert not [e for e in bus.since(0) if e["group_id"] == "999999"]
rows = store.recent_events(100)
assert any(r["group_id"] == "999999" for r in rows), "但必须落库"
ok.append("监控范围外的群：只落库、不入队、不进缓冲")

# ---------- 3. Restart: ids do not go backwards, the watermark survives ----------
bus2 = EventBus(size=10, cooldown_seconds=90, rules=rules,
                watch_groups=["1060667115"], store=store)
# The watermark is a timestamp and must come back verbatim after a restart, or
# already-handled messages get answered twice.
# (The old id cursor was dropped here: it was monotonic, could be fabricated, and
# could wedge the bot for good.)
assert bus2.status()["seen_at"] == bus.status()["seen_at"], "水位线没恢复，重启后会重发一遍"
assert bus2.status()["stored"] == 0, "新进程缓冲本来就是空的"
new_rec = bus2.push(event(100))
store.add_message(new_rec)
assert new_rec["id"] > 3, f"新 id 必须接着往后走，实际 {new_rec['id']}"
ok.append("重启后 id 不回退、游标保留、缓冲为空（所以控制台一片空白）")

# ---------- 4. Backfill: content on screen from boot, without replies on replay ----------
n = bus2.backfill(store.recent_events(100))
# The 5 old messages in the database plus the 1 just pushed: all 6 should be
# visible on the console.
# 5 history + 1 new; the new one is already in the buffer from the push above, so
# backfill only adds 5 of them.
assert n == 5, f"应当新增回填 5 条，实际 {n}"
assert bus2.status()["stored"] == 6, bus2.status()
pending_ids = {e["id"] for e in bus2.pending(include_low=True)}
assert pending_ids <= {new_rec["id"]}, f"回填的旧消息进了 pending，会被重新回一遍：{pending_ids}"
assert bus2.status()["cursor"] == new_rec["id"], "重启前没处理完的消息要把游标推过去，只展示不补回"
assert not bus2.since(bus2.status()["cursor"]), "回填后不该还剩可处理的事件"
assert bus2.backfill(store.recent_events(100)) == 0, "重复回填应当是幂等的"
ids = [e["id"] for e in bus2._events]
assert ids == sorted(ids), "回填后要按 id 升序"
ok.append(f"启动回填 {n} 条历史到缓冲（含上次没处理完的），只展示不补回、可重复调用")

# ---------- 5. A full ring buffer must be counted, not silently dropped ----------
# size=3 against 10 pushes: exactly 3 survive, the other 7 have to be counted as dropped.
bus3 = EventBus(size=3, cooldown_seconds=90, rules=rules, watch_groups=["1060667115"])
for i in range(10):
    bus3.push(event(i))
st = bus3.status()
assert st["stored"] == 3, st
assert st["dropped"] == 7, f"挤掉的条数要记下来，实际 {st['dropped']}"
ok.append("环形缓冲溢出会计数（dropped=7），不是静默丢弃")

# ---------- 6. Keyword and @-mention priority levels ----------
bus4 = EventBus(size=10, cooldown_seconds=90, rules=rules, watch_groups=["1060667115"])
bus4.self_id = "461282697"
bus4.push(event(1, text="今晚开服吗"))
bus4.push(event(2, text="随便聊聊"))
# With an empty raw_message the text has to be reassembled from the segments (real
# events normally carry raw_message, but a missing one must not lose the content).
bus4.push(event(3, text="", segs=[{"type": "at", "data": {"qq": "461282697"}},
                                  {"type": "text", "data": {"text": " 在吗"}}]))
st4 = bus4.status()
assert st4["pending_mid"] >= 1, "命中关键词该进中优先级"
assert st4["pending_high"] == 1, "@ 我该进高优先级"
高 = [e for e in bus4.pending(include_low=False) if e["mentions_me"]]
assert 高, "没识别出 @ 我"
assert 高[0]["text"].strip() == "在吗", f"raw_message 缺失时文本没从分段拼回来：{高[0]['text']!r}"
ok.append("关键词进中优先级、@ 进高优先级，且 @ 时文本拼接正确")

# ---------- 7. Per-conversation buckets: one watermark each (the core of this change) ----------
bus5 = EventBus(size=50, cooldown_seconds=90, rules=rules,
                watch_groups=["1060667115", "1083430859"])
bus5.push(event(1, text="A1", gid="1060667115"))
bus5.push(event(2, text="B1", gid="1083430859"))
bus5.push(event(3, text="A2", gid="1060667115"))
bus5.push(event(4, text="B2", gid="1083430859"))

buckets = bus5.unseen_by_key()
assert set(buckets) == {"1060667115", "1083430859"}, buckets.keys()
assert [e["text"] for e in buckets["1060667115"]] == ["A1", "A2"]
assert [e["text"] for e in buckets["1083430859"]] == ["B1", "B2"]
ok.append("unseen_by_key 按群分桶，各群只看自己的")

# Advance group A's watermark only; group B must be left untouched
bus5.mark_looked(3, key="1060667115")
buckets = bus5.unseen_by_key()
assert "1060667115" not in buckets, "A 群已经看完了，不该还有未读"
assert [e["text"] for e in buckets["1083430859"]] == ["B1", "B2"], "B 群被 A 群的水位线连累了"
ok.append("推进 A 群水位线不会吃掉 B 群的未读（这正是原来混在一起的病根）")

# Context is fetched per conversation and never across groups
ctx_a = bus5.context(50, key="1060667115")
ctx_b = bus5.context(50, key="1083430859")
assert [e["text"] for e in ctx_a] == ["A1", "A2"], ctx_a
assert [e["text"] for e in ctx_b] == ["B1", "B2"], ctx_b
assert bus5.key_of({"group_id": "123"}) == "123"
assert bus5.key_of({"group_id": "", "user_id": "456"}) == "456", "私聊要按对方 QQ 分桶"
ok.append("context 按会话取，A 群的上下文不会出现在 B 群")

# The watermark only advances to real message times and cannot be pushed past
# them (the safety valve still holds)
before = bus5._seen_by_key.get("1083430859", 0.0)
bus5.mark_seen_at(9e9, key="1083430859")
assert bus5._seen_by_key["1083430859"] <= max(e["at"] for e in bus5._events), "水位被推到了未来"
assert bus5._seen_by_key["1083430859"] > before
ok.append("按会话推进水位线时，安全阀依然把它钳在真实消息时间上")


print("\n".join("PASS  " + x for x in ok))
print("ALL PASS")
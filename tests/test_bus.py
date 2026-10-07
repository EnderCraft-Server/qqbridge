# -*- coding: utf-8 -*-
"""事件总线自测：环形缓冲、游标语义、启动回填。

回填这条是这轮新加的：进程一重启控制台就显示「暂无消息」，
看起来像机器人没在收消息，其实库里全都有 —— 只是没灌回缓冲。
关键是回填**不能**让旧消息被重新回复一遍。

运行：python tests/test_bus.py
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


# ---------- 1. 基本入库 + 游标 ----------
store = Store(tmp / "a.sqlite3")
rules = Rules(path=tmp / "kw.json", keywords=["开服"])
bus = EventBus(size=10, cooldown_seconds=90, rules=rules,
               watch_groups=["1060667115"], store=store)
bus.self_id = "461282697"
for i in range(5):
    rec = bus.push(event(i))
    store.add_message(rec)
assert bus.status()["stored"] == 5
assert bus.since(0)[0]["id"] == 1, "id 应当从 1 开始"
assert bus.status()["pending_low"] == 5, "冷却期已过，普通闲聊该进低位队列"
assert not [e for e in bus.since(0) if e["mentions_me"]], "没人 @ 的时候不该判成 @我"
bus.mark_processed(3)
assert bus.status()["cursor"] == 3
assert len(bus.since(3)) == 2, "游标之后应当只剩 2 条"
assert bus.since(0)[0]["id"] == 1, "since() 是纯读，不该因为游标推进而看不到历史"
ok.append("入库 / 自增 id / 游标推进 / since 纯读 都正确")

# ---------- 2. 监控范围外的群：落库但不入队 ----------
other = bus.push(event(99, gid="999999"))
store.add_message(other)
assert bus.status()["stored"] == 5, "范围外的群不该进环形缓冲"
assert not [e for e in bus.since(0) if e["group_id"] == "999999"]
rows = store.recent_events(100)
assert any(r["group_id"] == "999999" for r in rows), "但必须落库"
ok.append("监控范围外的群：只落库、不入队、不进缓冲")

# ---------- 3. 重启：id 不回退、游标不丢 ----------
bus2 = EventBus(size=10, cooldown_seconds=90, rules=rules,
                watch_groups=["1060667115"], store=store)
assert bus2.status()["cursor"] == 3, "游标没恢复，重启后会重发一遍"
assert bus2.status()["stored"] == 0, "新进程缓冲本来就是空的"
new_rec = bus2.push(event(100))
store.add_message(new_rec)
assert new_rec["id"] > 3, f"新 id 必须接着往后走，实际 {new_rec['id']}"
ok.append("重启后 id 不回退、游标保留、缓冲为空（所以控制台一片空白）")

# ---------- 4. 回填：控制台一开机就该有东西看，且不会重复回消息 ----------
n = bus2.backfill(store.recent_events(100))
# 库里 5 条旧消息 + 刚才那条新的，一共 6 条都该出现在控制台里
# 5 条历史 + 1 条新的；新的那条刚才 push 时就已经在缓冲里了，所以只新增 5 条
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

# ---------- 5. 环形缓冲满了要计数，不能悄悄丢 ----------
bus3 = EventBus(size=3, cooldown_seconds=90, rules=rules, watch_groups=["1060667115"])
for i in range(10):
    bus3.push(event(i))
st = bus3.status()
assert st["stored"] == 3, st
assert st["dropped"] == 7, f"挤掉的条数要记下来，实际 {st['dropped']}"
ok.append("环形缓冲溢出会计数（dropped=7），不是静默丢弃")

# ---------- 6. 关键词与 @ 的分级 ----------
bus4 = EventBus(size=10, cooldown_seconds=90, rules=rules, watch_groups=["1060667115"])
bus4.self_id = "461282697"
bus4.push(event(1, text="今晚开服吗"))
bus4.push(event(2, text="随便聊聊"))
# raw_message 为空时，文本要从分段里拼回来（真实事件一般带 raw_message，
# 但缺了也不能把内容丢了）
bus4.push(event(3, text="", segs=[{"type": "at", "data": {"qq": "461282697"}},
                                  {"type": "text", "data": {"text": " 在吗"}}]))
st4 = bus4.status()
assert st4["pending_mid"] >= 1, "命中关键词该进中优先级"
assert st4["pending_high"] == 1, "@ 我该进高优先级"
高 = [e for e in bus4.pending(include_low=False) if e["mentions_me"]]
assert 高, "没识别出 @ 我"
assert 高[0]["text"].strip() == "在吗", f"raw_message 缺失时文本没从分段拼回来：{高[0]['text']!r}"
ok.append("关键词进中优先级、@ 进高优先级，且 @ 时文本拼接正确")

print("\n".join("PASS  " + x for x in ok))
print("ALL PASS")
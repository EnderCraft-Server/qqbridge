# -*- coding: utf-8 -*-
"""qqbridge self-test: console gate (agreement/login), Qzone auto-posting, REST routes.

No network access and no touching the real data directory: everything runs inside a
temporary directory.
Run: python tests/test_auth_qzone.py
"""
import json, sys, tempfile, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from qqbridge.config import config
from qqbridge.auth import Auth, AGREEMENT_VERSION
from qqbridge.qzone_auto import QzoneAuto
tmp = Path(tempfile.mkdtemp(prefix="qqbridge_test_"))
ok = []

# ---- Auth ----
a = Auth(tmp / "auth.json")
assert a.state()["agreed"] is False and a.state()["has_account"] is False
st = a.accept("ender", "hunter2hunter")
assert st["agreed"] and st["has_account"] and st["username"] == "ender"
tok = a.login("ender", "hunter2hunter")
assert a.check(tok)
assert not a.check("bogus")
try:
    a.login("ender", "wrong-password"); raise SystemExit("密码错误竟然登录成功")
except ValueError:
    pass
tok2 = a.login("ender", "hunter2hunter")
assert a.check(tok2)
a.logout(tok2)
assert not a.check(tok2)
assert a.check(tok), "退出一个会话不应影响另一个"
a.change_password("hunter2hunter", "newpass12345")
try:
    a.login("ender", "hunter2hunter"); raise SystemExit("旧密码仍然可用")
except ValueError:
    pass
assert a.check(a.login("ender", "newpass12345"))
ok.append("Auth: 初始化/登录/登出/改密 全部正确")

# ---- QzoneAuto ----
class FakeLLM:
    configured = True
    async def chat(self, messages, tools=None, *, max_tokens=None, temperature=None):
        return {"text": "  今天服务器又崩了一次，重启完发现是我自己改坏的。  "}

sent = []
class FakeBot:
    async def call(self, action, **kw):
        sent.append((action, kw)); return {"status": "ok"}
async def fake_publish(text):
    sent.append(("publish", {"text": text}))

q = QzoneAuto(tmp / "qzone.json", FakeLLM(), FakeBot(), publish=fake_publish)
q.save({"enabled": True, "mode": "interval", "interval_hours": 12, "min_gap_hours": 4,
        "quiet_hours": [], "order": "rotate", "active_themes": ["daily", "code"]})
assert q.due() is True, "刚启用、没发过，应该到点"
assert q.pick_theme()["id"] in ("daily", "code")
# post_once() is a coroutine; asyncio is imported inline to keep this script linear.
r = __import__("asyncio").run(q.post_once())
assert r["ok"] and sent and sent[0][0] == "publish"
assert sent[0][1]["text"] == "今天服务器又崩了一次，重启完发现是我自己改坏的。", sent[0]
assert q.due() is False, "刚发过，min_gap 内不应再发"
assert q.status()["posted"] == 1
# Quiet hours; the window may wrap past midnight, so 02:00 is inside and 12:00 is out.
q.save({"quiet_hours": [["23:30", "08:00"]]})
assert q.in_quiet_hours(time.mktime(time.strptime("2024-01-01 02:00", "%Y-%m-%d %H:%M"))) is True
assert q.in_quiet_hours(time.mktime(time.strptime("2024-01-01 12:00", "%Y-%m-%d %H:%M"))) is False
# Explicit times of day (daily mode)
q.save({"mode": "daily", "daily_times": ["12:30", "21:00"], "quiet_hours": [], "enabled": True})
assert q._next_in() is not None
# Disabled means nothing is ever due
q.save({"enabled": False})
assert q.due() is False
# Theme time windows: the late-night theme must not be pickable during the day
q.save({"enabled": True, "mode": "interval", "order": "random", "quiet_hours": [],
        "active_themes": [t["id"] for t in q.cfg["themes"]]})
def _at(hhmm):
    return time.mktime(time.strptime("2024-06-01 " + hhmm, "%Y-%m-%d %H:%M"))
assert q.in_quiet_hours(_at("02:00")) is False
# pick_theme is random, so each time of day is sampled 300 times: a single draw could
# miss a theme purely by luck and make this test flaky.
day = {q.pick_theme(_at("14:00"))["id"] for _ in range(300)}
night = {q.pick_theme(_at("23:00"))["id"] for _ in range(300)}
assert "late" not in day, "深夜主题不该出现在下午:" + str(day)
assert "late" in night, "深夜主题应当出现在夜里:" + str(night)
q.save({"quiet_hours": [["23:30", "08:00"]]})
assert q.in_quiet_hours(_at("02:00")) is True and q.in_quiet_hours(_at("12:00")) is False
# Key regression: last_post_at has to survive a restart, otherwise every restart
# posts another item right away
q.save({"enabled": True, "mode": "interval", "interval_hours": 12, "min_gap_hours": 4,
        "quiet_hours": [], "last_post_at": time.time()})
q2 = QzoneAuto(tmp / "qzone.json", FakeLLM(), FakeBot())
assert q2.cfg.get("last_post_at"), "重启后 last_post_at 丢了，会立刻重复发说说"
assert q2.due() is False, "刚发过、min_gap 内，重启后不应立刻再发"
assert q2.status()["posted"] == 1, "历史也要读得回来"
ok.append("QzoneAuto: 到点判定/生成/发送/静默时段/主题时间窗/落库/重启不重复发 全部正确")

# ---- Routes (lifespan is never run, so no OneBot connection is attempted) ----
tmp2 = tmp / "app"; tmp2.mkdir()
config.data_dir = tmp2
from qqbridge.server import create_app
from fastapi.testclient import TestClient
c = TestClient(create_app())

r = c.get("/api/auth/state"); assert r.status_code == 200 and r.json()["agreed"] is False, r.text
r = c.get("/api/auth/agreements")
j = r.json()
assert "无法保证" in j["terms"] and "EnderCraft" in j["eula"] + j["terms"], "协议要点缺失"
# The gate in order: nothing accepted yet -> /api/state is 401 -> accept -> 200.
r = c.get("/api/state"); assert r.status_code == 401, "未登录竟然能读 /api/state"
r = c.post("/api/auth/accept", json={"username": "ender", "password": "pw12345678", "agreed": True})
assert r.status_code == 200, r.text
r = c.get("/api/state"); assert r.status_code == 200, "登录后应当能读 /api/state"
assert "qzone" in r.json() and "auth" in r.json()
r = c.get("/api/auth/state"); assert r.json()["logged_in"] is True
r = c.get("/api/qzone"); assert r.status_code == 200 and r.json()["themes"], r.text
n = len(r.json()["themes"])
r = c.post("/api/qzone", json={"enabled": True, "interval_hours": 6, "active_themes": ["daily"]})
assert r.status_code == 200 and r.json()["interval_hours"] == 6
r = c.post("/api/qzone", json={"themes": r.json()["themes"] + [{"id": "x1", "name": "新主题", "hint": "随便写"}]})
assert len(r.json()["themes"]) == n + 1
r = c.post("/api/auth/accept", json={"username": "other", "password": "pw12345678", "agreed": True})
assert r.status_code == 400, "重复初始化应当被拒绝"
# The image proxy's host allowlist: it must not become an SSRF hop for arbitrary URLs
for bad in ["https://evil.example.com/a.png", "http://multimedia.nt.qq.com.cn/a.png",
            "https://127.0.0.1/a.png", "https://169.254.169.254/latest/meta-data/",
            "file:///C:/Windows/win.ini", ""]:
    rr = c.get("/api/image", params={"u": bad})
    assert rr.status_code in (400, 422), f"该拦下的图片地址没拦住：{bad!r} -> {rr.status_code}"
rr = c.get("/api/image", params={"u": "https://multimedia.nt.qq.com.cn/a.png"})
assert rr.status_code not in (400, 401, 415), f"QQ 图床应当放行（这里只验白名单，不真联网）：{rr.status_code}"
ok.append("路由: 门禁/协议/登录/说说读写/图片代理白名单 全部符合预期")

r = c.post("/api/auth/logout"); assert r.status_code == 200
r = c.get("/api/state"); assert r.status_code == 401, "登出后应当被拦"

print("\n".join("PASS  " + x for x in ok))
print("ALL PASS")

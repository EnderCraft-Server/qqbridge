# -*- coding: utf-8 -*-
"""qqbridge 自测：控制台门禁（协议/登录）、自动说说、REST 路由。

不依赖网络、不碰真实 data 目录：全部在临时目录里跑。
运行：python tests/test_auth_qzone.py
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
    async def chat(self, messages, tools=None):
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
r = __import__("asyncio").run(q.post_once())
assert r["ok"] and sent and sent[0][0] == "publish"
assert sent[0][1]["text"] == "今天服务器又崩了一次，重启完发现是我自己改坏的。", sent[0]
assert q.due() is False, "刚发过，min_gap 内不应再发"
assert q.status()["posted"] == 1
# 静默时段
q.save({"quiet_hours": [["23:30", "08:00"]]})
assert q.in_quiet_hours(time.mktime(time.strptime("2024-01-01 02:00", "%Y-%m-%d %H:%M"))) is True
assert q.in_quiet_hours(time.mktime(time.strptime("2024-01-01 12:00", "%Y-%m-%d %H:%M"))) is False
# 每天时刻模式
q.save({"mode": "daily", "daily_times": ["12:30", "21:00"], "quiet_hours": [], "enabled": True})
assert q._next_in() is not None
# 关掉就不发
q.save({"enabled": False})
assert q.due() is False
# 主题时间窗：深夜主题在白天不能被选中
q.save({"enabled": True, "mode": "interval", "order": "random", "quiet_hours": [],
        "active_themes": [t["id"] for t in q.cfg["themes"]]})
def _at(hhmm):
    return time.mktime(time.strptime("2024-06-01 " + hhmm, "%Y-%m-%d %H:%M"))
assert q.in_quiet_hours(_at("02:00")) is False
day = {q.pick_theme(_at("14:00"))["id"] for _ in range(300)}
night = {q.pick_theme(_at("23:00"))["id"] for _ in range(300)}
assert "late" not in day, "深夜主题不该出现在下午:" + str(day)
assert "late" in night, "深夜主题应当出现在夜里:" + str(night)
q.save({"quiet_hours": [["23:30", "08:00"]]})
assert q.in_quiet_hours(_at("02:00")) is True and q.in_quiet_hours(_at("12:00")) is False
ok.append("QzoneAuto: 到点判定/生成/发送/静默时段/主题时间窗/落库 全部正确")

# ---- 路由（不跑 lifespan，避免连 OneBot）----
tmp2 = tmp / "app"; tmp2.mkdir()
config.data_dir = tmp2
from qqbridge.server import create_app
from fastapi.testclient import TestClient
c = TestClient(create_app())

r = c.get("/api/auth/state"); assert r.status_code == 200 and r.json()["agreed"] is False, r.text
r = c.get("/api/auth/agreements")
j = r.json()
assert "无法保证" in j["terms"] and "EnderCraft" in j["eula"] + j["terms"], "协议要点缺失"
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
r = c.post("/api/auth/logout"); assert r.status_code == 200
r = c.get("/api/state"); assert r.status_code == 401, "登出后应当被拦"
ok.append("路由: 门禁/协议/登录/说说读写 全部符合预期")

print("\n".join("PASS  " + x for x in ok))
print("ALL PASS")

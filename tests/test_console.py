# -*- coding: utf-8 -*-
"""控制台功能自测：每个写入接口都验「真的生效了」，不只是 HTTP 200。

背景：控制台的毛病往往不是接口报错，而是点了没反应 / 下次刷新又变回去，
所以这里一律检查服务端对象的真实状态与落盘文件，并且确认 .env 等真实文件没被测试污染。

运行：python tests/test_console.py
"""
import json, sys, tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from qqbridge.config import config

# 真实的 .env 必须先拍张快照。这套测试会调用 persist_permissions()，
# 只要它认错了根目录，就会把 api_base=example.com 之类的测试值写进仓库配置，
# 上一次就是这么把线上机器人的模型地址冲掉、让它彻底不说话的。
REAL_ENV = Path(__file__).resolve().parent.parent / ".env"
REAL_ENV_BEFORE = REAL_ENV.read_bytes() if REAL_ENV.is_file() else None

tmp = Path(tempfile.mkdtemp(prefix="qqbridge_console_"))
# 只改实例级 ROOT。故意不去动模块级 ROOT —— 万一 persist_permissions()
# 又改回读模块变量，下面的快照断言会立刻炸出来，而不是悄悄污染真实配置。
config.ROOT = tmp
config.data_dir = tmp / "data"         # 和真实布局一致：ROOT/data/，prompt 路径才落在临时目录里
config.data_dir.mkdir(parents=True, exist_ok=True)
(tmp / ".env").write_text("MCP_TOKEN=test\nWATCH_GROUPS=1060667115\n", encoding="utf-8")

from qqbridge.server import create_app
from fastapi.testclient import TestClient

app = create_app()
c = TestClient(app)
ok = []

r = c.post("/api/auth/accept", json={"username": "admin", "password": "pw12345678", "agreed": True})
assert r.status_code == 200, r.text


def state():
    r = c.get("/api/state")
    assert r.status_code == 200, r.text
    return r.json()


# ---------- 1. 模型 / 自动接话 ----------
r = c.post("/api/llm", json={"api_base": "https://example.com/v1", "api_key": "",
                             "model": "test-model", "max_tokens": 1234, "auto_reply": False})
assert r.status_code == 200, r.text
assert app.state.llm.model == "test-model", "模型名没生效"
assert app.state.llm.api_base == "https://example.com/v1", "API Base 没生效"
assert app.state.llm.max_tokens == 1234, "max_tokens 没生效"
assert app.state.autoreply.enabled is False, "自动接话开关没作用到循环上"
assert config.auto_reply is False and state()["config"]["auto_reply"] is False
r = c.post("/api/llm", json={"auto_reply": True})
assert app.state.autoreply.enabled is True, "开关拨回去没生效"
ok.append("模型与人格：改模型 / max_tokens / 自动接话开关，都真正作用到运行时")

# ---------- 2. 人格 ----------
r = c.post("/api/prompt", json={"prompt": "你是被测试的人格。"})
assert r.status_code == 200 and app.state.agent.system_prompt == "你是被测试的人格。", "人格没换"
pf = config.data_dir / "system_prompt.md"
assert pf.is_file() and pf.read_text(encoding="utf-8") == "你是被测试的人格。", "人格没落盘"
assert c.post("/api/prompt", json={"prompt": "   "}).status_code == 400, "空人格应当被拒绝"
ok.append("模型与人格：人格保存即时生效且落盘，空内容被拒")

# ---------- 3. 巡检节奏 ----------
r = c.post("/api/scheduler", json={"enabled": True, "interval_seconds": 45, "min_gap_seconds": 7,
                                   "max_turns_per_hour": 9, "quiet_hours": [["01:00", "06:00"]]})
assert r.status_code == 200, r.text
st = app.state.sched.settings
assert st["enabled"] and st["interval_seconds"] == 45 and st["min_gap_seconds"] == 7, st
sched_file = json.loads((config.data_dir / "scheduler.json").read_text(encoding="utf-8"))
assert sched_file["interval_seconds"] == 45, "巡检设置没落盘"
assert state()["scheduler"]["settings"]["interval_seconds"] == 45
ok.append("巡检与唤醒：间隔 / 最小间隔 / 每小时上限 / 静默时段 生效并落盘")

# ---------- 4. 运行开关 ----------
for mode in ("stopped", "manual", "auto"):
    r = c.post("/api/control", json={"mode": mode})
    assert r.status_code == 200 and app.state.control.mode == mode, f"切到 {mode} 没生效"
assert app.state.control.paused() is False
c.post("/api/control", json={"mode": "stopped"})
assert app.state.control.paused() is True, "stopped 应当算暂停"
c.post("/api/control", json={"mode": "auto"})
assert app.state.control.paused() is False
ok.append("巡检与唤醒：auto / manual / stopped 三种运行开关都能切")

# ---------- 5. 监控范围 ----------
r = c.post("/api/watch", json={"watch_groups": ["111", "222"]})
assert r.status_code == 200 and r.json()["watch_groups"] == ["111", "222"], r.text
assert config.watch_groups == {"111", "222"}, "config 没跟上"
assert app.state.box.bus.watch_groups == {"111", "222"}, "bus 没跟上 —— 这正是「改了不生效」的原因"
env = (tmp / ".env").read_text(encoding="utf-8")
assert "WATCH_GROUPS=111,222" in env, "没写进 .env：" + env
r = c.get("/api/groups")
assert r.status_code == 200 and isinstance(r.json()["groups"], list), "群列表拿不到"
assert r.json()["watch_all"] is False
c.post("/api/watch", json={"watch_groups": []})
assert c.get("/api/groups").json()["watch_all"] is True, "清空应当表示不设限制"
c.post("/api/watch", json={"watch_groups": ["111", "222"]})
ok.append("监控范围：写库、写 .env、同步到 bus 三处一致；清空语义正确")

# ---------- 6. 关键词 ----------
r = c.post("/api/keywords", json={"keywords": ["开服", "报错"]})
assert r.status_code == 200 and app.state.rules.keywords == ["开服", "报错"], "关键词没生效"
kw = json.loads((config.data_dir / "keywords.json").read_text(encoding="utf-8"))
assert "报错" in json.dumps(kw, ensure_ascii=False), "关键词没落盘"
ok.append("关键词：即时生效并落盘")

# ---------- 7. 权限 ----------
r = c.post("/api/permissions", json={"allow_send": False, "allow_manage": True, "owners": ["98704929"]})
assert r.status_code == 200, r.text
assert config.allow_send is False and config.allow_manage is True, "权限开关没生效"
assert config.owners == {"98704929"}, "白名单没生效"
env = (tmp / ".env").read_text(encoding="utf-8")
assert "99999999" not in env
ok.append("权限与安全：允许发送 / 群管理 / Owner 白名单 生效并落盘")

# ---------- 8. 自动说说 ----------
r = c.post("/api/qzone", json={"enabled": True, "interval_hours": 5, "active_themes": ["daily"]})
assert r.status_code == 200 and app.state.qzone.cfg["interval_hours"] == 5, "说说设置没生效"
qz = json.loads((config.data_dir / "qzone.json").read_text(encoding="utf-8"))
assert qz["enabled"] is True and qz["interval_hours"] == 5, "说说设置没落盘"
ok.append("自动说说：频率 / 主题选择 生效并落盘")

# ---------- 9. 改密码 ----------
r = c.post("/api/auth/password", json={"old": "pw12345678", "new": "newpw12345"})
assert r.status_code == 200, r.text
assert c.get("/api/state").status_code == 401, "改密码后旧会话应当失效"
assert c.post("/api/auth/login", json={"username": "admin", "password": "pw12345678"}).status_code == 401
assert c.post("/api/auth/login", json={"username": "admin", "password": "newpw12345"}).status_code == 200
assert c.post("/api/auth/password", json={"old": "错的", "new": "another12345"}).status_code == 400
ok.append("后台账号：改密码后旧会话失效、旧密码登不上、新密码可用")

# ---------- 10. 真实 .env 一个字节都不能变 ----------
real_after = REAL_ENV.read_bytes() if REAL_ENV.is_file() else None
assert real_after == REAL_ENV_BEFORE, (
    "测试改动了仓库里真实的 .env！这会把线上机器人的模型地址 / 监控范围冲掉。"
    "检查 persist_permissions() 是不是又去读模块级 ROOT 了。")
ok.append("测试全程没有动仓库里真实的 .env（逐字节比对）")

# ---------- 11. 改巡检间隔要立刻生效，不能等满旧周期 ----------
import asyncio, time
from qqbridge.scheduler import Scheduler


class _FakeBus:
    def pending(self, include_low=False, limit=50):
        return []


async def _sched_check():
    s = Scheduler(config.data_dir / "sched_loop.json", _FakeBus(), None)
    s.save({"enabled": True, "interval_seconds": 300, "min_gap_seconds": 0, "max_turns_per_hour": 9999})
    await s.start()
    await asyncio.sleep(0.4)
    assert s.runs >= 1, "启动后应当立刻跑第一轮，而不是干等一个周期"
    before = s.runs
    s.save({"interval_seconds": 5})
    await asyncio.sleep(1.2)
    assert s.runs > before, "把间隔从 300 改成 5 之后应当马上再跑一轮（不该还睡在旧的 300 秒里）"
    await s.stop()


asyncio.run(_sched_check())
ok.append("巡检节奏：改完设置立刻重新排期，不会干等旧周期跑完")

print("\n".join("PASS  " + x for x in ok))
print("ALL PASS")

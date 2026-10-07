# -*- coding: utf-8 -*-
"""接话逻辑自测：批处理闲聊 + 派活直连 Agent。

架构换过一次，这里钉的就是新架构：
  - 闲聊：攒一段，整段交给模型判断「要不要接、接什么」，处理完整批才推进标记
  - 派活：管理员在跟它说话（@ 或私聊）就直接叫内置 Agent，**不进批处理**
  - 不管哪条路，被 @ 都必须有回音

运行：python tests/test_autoreply.py
"""
import asyncio
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from qqbridge.agent import QqAgent
from qqbridge.autoreply import AutoReply
from qqbridge.config import config

config.admins = {"98704929"}          # 固定下来，别受 .env 影响


class FakeLLM:
    configured = True

    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    async def chat(self, messages, tools=None):
        self.calls.append(messages)
        return {"text": self.replies.pop(0) if self.replies else ""}


class FakeStore:
    def __init__(self):
        self.records = []

    def audit(self, actor, action, target, params, state, result=""):
        self.records.append({"action": action, "target": target, "params": params, "state": state})

    def states(self, action):
        return [r for r in self.records if r["action"] == action]


class FakeBus:
    def __init__(self, events):
        self.events = events
        self.looked = 0
        self.own_sends = 0

    def status(self):
        return {"cursor": self.looked}

    def unseen(self, limit=200):
        return [e for e in self.events if e["id"] > self.looked and not e.get("is_self")][:limit]

    def context(self, limit=40):
        return list(self.events)[-limit:]

    def mark_looked(self, through_id):
        self.looked = max(self.looked, through_id)
        return self.looked

    def mark_processed(self, through_id):
        return self.mark_looked(through_id)

    def note_own_send(self):
        self.own_sends += 1

    def pending(self, include_low=True, limit=50):
        return self.unseen(limit)


class FakeBot:
    def __init__(self):
        self.msgs = []

    async def call(self, action, **kw):
        self.msgs.append({"action": action, **kw})
        return {}


class FakeControl:
    def paused(self):
        return False


class FakeAgentLoop:
    def __init__(self, text="干完了"):
        self.text = text
        self.tasks = []

    async def run(self, system, task, history=None):
        self.tasks.append(task)
        return {"text": self.text, "steps": 1, "tool_calls": []}


def ev(i, text="在吗", *, uid="12345", mention=False, gid="1060667115",
       mtype="group", sender="路人", at=None):
    return {"id": i, "at": at if at is not None else time.time(), "group_id": gid,
            "user_id": uid, "sender": sender, "message_type": mtype, "text": text,
            "is_self": False, "mentions_me": mention, "keyword_hit": None,
            "segments": [{"type": "text", "text": text}]}


def run(events, replies, *, agent_text="干完了", agent_loop=None):
    llm = FakeLLM(replies)
    store = FakeStore()
    bus = FakeBus(events)
    bot = FakeBot()
    agent = QqAgent(llm, "你是被测试的机器人。", store=store)
    loop = agent_loop if agent_loop is not None else FakeAgentLoop(agent_text)
    ar = AutoReply(bus, store, agent, bot, FakeControl(), enabled=True,
                   agent_loop=loop, settle_seconds=0, max_batch_age=0)
    asyncio.run(ar._tick())
    return llm, store, bot, bus, loop


def sent(bot):
    return [m for m in bot.msgs if m["action"] == "send_group_msg"]


ok = []

# 1) 闲聊：整段一次性判断，想接就接
llm, store, bot, bus, _ = run([ev(1, "今天中午吃啥啊"), ev(2, "随便")],
                             ['{"reply": "吃我", "reason": "能接"}'])
assert len(sent(bot)) == 1 and sent(bot)[0]["message"] == "吃我", bot.msgs
assert len(llm.calls) == 1, f"整段只该问模型一次，实际 {len(llm.calls)} 次"
assert bus.looked == 2, f"处理完整批才推进，应当推到 2，实际 {bus.looked}"
ok.append("闲聊：一整段只问模型一次，接了并在整批处理完后推进标记")

# 2) 闲聊：模型选择沉默 -> 安静，但标记照推（不会把同一段看第二遍）
llm, store, bot, bus, _ = run([ev(1, "早"), ev(2, "冒泡")],
                             ['{"reply": "", "reason": "没意思"}'])
assert not bot.msgs, bot.msgs
assert bus.looked == 2, "沉默也要推进，否则同一段会被反复看"
assert store.states("auto_silent") and not store.states("auto_reply")
n_before = len(llm.calls)
asyncio.run(AutoReply(bus, store, QqAgent(FakeLLM([]), "p"), bot, FakeControl(),
                      settle_seconds=0, max_batch_age=0)._tick())
assert bus.looked == 2, "已经看过的段不该再被处理"
ok.append("闲聊：沉默时不发消息，但标记推进（同一段不会看第二遍）")

# 3) 非管理员 @ 了它 -> 走闲聊批量；模型仍沉默则触发兜底
llm, store, bot, bus, _ = run([ev(1, "@机器人 说话", mention=True)],
                             ['{"reply": "", "reason": "不想说"}', "在呢，咋了"])
assert len(sent(bot)) == 1 and sent(bot)[0]["message"] == "在呢，咋了", bot.msgs
assert len(llm.calls) == 2, "被 @ 时必须再兜底问一次"
ok.append("@ 兜底：非管理员 @ 它、模型沉默时会再问一次并发出")

# 4) 管理员 @ 它 -> 直接叫 Agent，不进批处理
loop = FakeAgentLoop("查到了，内存 32G")
llm, store, bot, bus, loop = run([ev(1, "@我 查下内存", uid="98704929", mention=True)],
                                 ['{"reply": "这是闲聊回复", "reason": "不该走到这"}'],
                                 agent_loop=loop)
assert len(loop.tasks) == 1, "管理员 @ 它应当直接调用 Agent"
assert "查下内存" in loop.tasks[0], loop.tasks[0]
assert sent(bot) and sent(bot)[0]["message"] == "查到了，内存 32G", bot.msgs
assert not llm.calls, "派活不该走批量闲聊决策（模型一次都不该被闲聊调用）"
ok.append("派活：管理员 @ 它 -> 直接调 Agent，完全不进批处理")

# 5) 管理员私聊 -> 同样直连 Agent
loop = FakeAgentLoop("好")
llm, store, bot, bus, loop = run([ev(1, "帮我看下", uid="98704929", gid="",
                                     mtype="private", mention=False)],
                                 [], agent_loop=loop)
assert len(loop.tasks) == 1, "私聊派活也该直连 Agent"
assert len(bot.msgs) == 1 and bot.msgs[0]["action"] == "send_private_msg", bot.msgs
assert bot.msgs[0]["message"] == "好"
ok.append("派活：管理员私聊 -> 同样直连 Agent（回复走单聊）")

# 6) 管理员在群里闲聊（没 @）-> 不该进 Agent，走批处理
loop = FakeAgentLoop("不该被调用")
llm, store, bot, bus, loop = run([ev(1, "今天好累", uid="98704929", mention=False)],
                                 ['{"reply": "", "reason": "没意思"}'], agent_loop=loop)
assert not loop.tasks, "管理员闲聊不该被当成派活（这正是「冒泡」被回的根因）"
assert not bot.msgs
ok.append("边界：管理员在群里闲聊（没 @）走批处理，不会被当成命令")

# 7) 攒一段：刚来的消息先等一会儿
llm = FakeLLM(['{"reply": "接", "reason": "x"}'])
store, bus, bot = FakeStore(), FakeBus([ev(1, "刚发的", at=time.time())]), FakeBot()
agent = QqAgent(llm, "p", store=store)
ar = AutoReply(bus, store, agent, bot, FakeControl(), settle_seconds=3, max_batch_age=45)
asyncio.run(ar._tick())
assert not bot.msgs and not llm.calls, "最后一条刚来，应当先等着看还有没有人接着说"
# 默认值也要跟着改，别只在测试里写死
assert AutoReply(bus, store, agent, bot, FakeControl()).settle_seconds == 3.0, \
    "默认静默窗口应当是 3 秒"
ok.append("攒一段：最后一条之后静默 3 秒内不出手（max_batch_age 兜底不会饿死）")

# 8) 纯文本多行回复不能被砍成第一行（老 bug 回归）
from qqbridge.agent import clean_reply, tidy_plain

STATUS = ("宿主机状态：\nCPU：AMD R7 9800X3D，负载23%\n内存：31.2GB 总，可用10.6GB\n"
          "磁盘：C 275G/516G 可用")
reply, how = clean_reply(STATUS)
assert how == "plain" and reply == STATUS, "多行纯文本又被砍了：" + repr(reply)
assert reply.count("\n") == 3
assert clean_reply('{"reply": "第一行\\n第二行", "reason": "x"}')[0] == "第一行\n第二行"
assert clean_reply('{"reply": "hi", ') == ("", "failed")
assert tidy_plain("a  \n\n\n\nb") == "a\n\nb"
ok.append("多行纯文本完整保留、JSON 多行保留、坏 JSON 依旧沉默")

print("\n".join("PASS  " + x for x in ok))
print("ALL PASS")

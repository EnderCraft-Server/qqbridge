# -*- coding: utf-8 -*-
"""接话逻辑自测：被 @ 时必须回一句，普通闲聊不回也不该乱发。

起因：群里 @ 了机器人却一路 auto_silent。除了模型链路挂掉，
还有一个纯粹的行为问题 —— 模型返回 {"reply": ""} 时直接静默，哪怕被点名。
这里把「@ 必回」钉死。

运行：python tests/test_autoreply.py
"""
import sys, asyncio
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from qqbridge.agent import QqAgent
from qqbridge.autoreply import AutoReply


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
        self.cursor = 0
        self.own_sends = 0

    def status(self):
        return {"cursor": self.cursor}

    def since(self, cursor=0, limit=100, include_low=True):
        return [e for e in self.events if e["id"] > cursor]

    def mark_processed(self, through_id):
        self.cursor = max(self.cursor, through_id)

    def note_own_send(self):
        self.own_sends += 1

    def pending(self, include_low=True, limit=50):
        return [e for e in self.events if e["id"] > self.cursor][:limit]


class FakeBot:
    def __init__(self):
        self.msgs = []

    async def call(self, action, **kw):
        self.msgs.append({"action": action, **kw})
        return {}


class FakeControl:
    def paused(self):
        return False


def make_event(**kw):
    ev = {"id": 1, "at": 0, "group_id": "1060667115", "user_id": "12345",
          "sender": "路人", "message_type": "group", "text": "在吗", "is_self": False,
          "mentions_me": False, "keyword_hit": None}
    ev.update(kw)
    return ev


def run_tick(replies, event):
    llm = FakeLLM(replies)
    del replies
    store = FakeStore()
    bus = FakeBus([event])
    bot = FakeBot()
    agent = QqAgent(llm, "你是被测试的机器人。", store=store)
    ar = AutoReply(bus, store, agent, bot, FakeControl(), enabled=True)
    asyncio.run(ar._tick())
    return llm, store, bot


ok = []

# 1) 被 @ 且模型沉默 -> 必须兜底回一句
llm, store, bot = run_tick(
    ['{"reply": "", "reason": "不想说话"}', "在呢，咋了"],
    make_event(mentions_me=True, text="@机器人 说话！"))
sent = [m for m in bot.msgs if m["action"] == "send_group_msg"]
assert len(sent) == 1, "@ 了居然没发消息：" + str(bot.msgs)
assert sent[0]["message"] == "在呢，咋了", sent[0]
assert len(llm.calls) == 2, "应当再问一次模型（兜底）"
assert store.states("auto_reply"), "应当记 auto_reply"
assert not store.states("auto_silent"), "不该记 auto_silent"
ok.append("@ 后模型沉默 -> 兜底再问一次，回了「" + sent[0]["message"] + "」")

# 2) 被 @ 且兜底也问不出话 -> 固定短句，绝不静默
llm, store, bot = run_tick(
    ['{"reply": "", "reason": "不想说话"}', ""],
    make_event(mentions_me=True, text="@机器人 冒泡"))
sent = [m for m in bot.msgs if m["action"] == "send_group_msg"]
assert len(sent) == 1, "@ 了两次都没话，也必须发固定短句"
assert sent[0]["message"] == AutoReply.FORCED_FALLBACK, sent[0]
ok.append("@ 后连兜底都没话说 -> 发固定短句「" + AutoReply.FORCED_FALLBACK + "」")

# 3) 被 @ 且模型解析失败（半截 JSON）-> 也要兜底，不能把 JSON 发出去
llm, store, bot = run_tick(
    ['{"reply": "你好", ', "收到"],
    make_event(mentions_me=True, text="@机器人 在吗"))
sent = [m for m in bot.msgs if m["action"] == "send_group_msg"]
assert len(sent) == 1 and sent[0]["message"] == "收到", bot.msgs
ok.append("@ 后输出是坏 JSON -> 不把 JSON 发出去，走兜底")

# 4) 没被 @ 且模型选择沉默 -> 就该沉默，别乱发
llm, store, bot = run_tick(
    ['{"reply": "", "reason": "插不上话"}'],
    make_event(mentions_me=False, text="今天天气不错"))
assert not bot.msgs, "普通闲聊模型选择沉默时不该发消息：" + str(bot.msgs)
assert store.states("auto_silent"), "应当记 auto_silent"
ok.append("普通闲聊 -> 模型选择沉默时安静待着，不刷屏")

# 5) 没被 @ 但模型要接 -> 正常发出去
llm, store, bot = run_tick(
    ['{"reply": "确实不错", "reason": "能接上"}'],
    make_event(mentions_me=False, text="今天天气不错"))
sent = [m for m in bot.msgs if m["action"] == "send_group_msg"]
assert len(sent) == 1 and sent[0]["message"] == "确实不错", bot.msgs
ok.append("普通闲聊 -> 模型想接就正常接")

print("\n".join("PASS  " + x for x in ok))
print("ALL PASS")

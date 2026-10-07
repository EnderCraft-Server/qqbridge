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

# ---------- 6. 纯文本回复不能被砍成第一行 ----------
# 真实翻车：模型查完宿主机写了 6 行状态，群里只收到「宿主机状态：」这一行。
from qqbridge.agent import parse_decision, tidy_plain, clean_reply

STATUS = (
    "宿主机状态：\n"
    "CPU：AMD R7 9800X3D 8核16线程，负载23%~29%\n"
    "内存：31.2GB 总，可用10.6GB\n"
    "显卡：RTX 3080 + 核显\n"
    "磁盘：C 275G/516G 可用，D 562G/930G\n"
    "系统：Win11 专业版 26200，已开机14小时"
)
reply, how = clean_reply(STATUS)
assert how == "plain", how
assert reply == STATUS, "多行纯文本被砍了：\n" + repr(reply)
assert "CPU" in reply and "内存" in reply and "磁盘" in reply

# 多行 JSON 里的 reply 也要完整保留
import json as _json
multi = _json.dumps({"reply": "第一行\n第二行\n第三行", "reason": "多行"}, ensure_ascii=False)
r2, how2 = clean_reply(multi)
assert how2 == "ok" and r2 == "第一行\n第二行\n第三行", (how2, repr(r2))

# 空行压缩、行尾空白清理，但不吃掉换行
assert tidy_plain("a  \n\n\n\nb\n") == "a\n\nb", repr(tidy_plain("a  \n\n\n\nb\n"))
# 超长才截断，且给足 800 字
assert len(tidy_plain("x" * 2000)) == 800
# 坏 JSON 仍然沉默，绝不把 JSON 发出去
assert clean_reply('{"reply": "hi", ') == ("", "failed")
ok.append("纯文本多行回复完整保留（不再只发第一行），坏 JSON 依旧沉默")

print("\n".join("PASS  " + x for x in ok))
print("ALL PASS")

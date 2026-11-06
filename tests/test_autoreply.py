# -*- coding: utf-8 -*-
"""Auto-reply logic self-test: per-conversation buckets, multi-part sends, dispatch.

  - Chit-chat: bucketed per group, one independent conversation per group, with no
    context crossing between groups
  - Collect a stretch of messages before deciding; this conversation's own watermark
    only advances once the whole batch has been handled
  - Dispatch: an admin @-mention / private message -> the built-in agent is called
    directly, bypassing batch processing
  - A reply may consist of several parts, sent as separate messages

Run: python tests/test_autoreply.py
"""
import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from qqbridge.agent import QqAgent
from qqbridge.autoreply import AutoReply
from qqbridge.config import config

config.admins = {"98704929"}


class FakeLLM:
    configured = True

    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    async def chat(self, messages, tools=None, **kw):
        # Records every prompt and hands back the scripted raw text in order, so the
        # assertions can tell how many times the model was asked.
        self.calls.append(messages)
        return {"text": self.replies.pop(0) if self.replies else ""}


# Audit sink mirroring Store.audit(); states() makes it easy to assert on one action.
class FakeStore:
    def __init__(self):
        self.records = []

    def audit(self, actor, action, target, params, state, result=""):
        self.records.append({"action": action, "target": target, "params": params, "state": state})

    def states(self, action):
        return [r for r in self.records if r["action"] == action]


class FakeBus:
    """Records a watermark per conversation, mirroring the real EventBus."""

    def __init__(self, events):
        self.events = events
        self.seen = {}
        self.own_sends = 0
        self.contexts = []

    @staticmethod
    def key_of(e):
        # Bucket key: the group id for group chats, the peer QQ for private chats.
        return str(e.get("group_id") or e.get("user_id") or "")

    def unseen_by_key(self, limit=200):
        # Self messages never count as unseen, and the watermark is a timestamp:
        # anything at or before it has already been looked at.
        out = {}
        for e in self.events:
            if e.get("is_self"):
                continue
            k = self.key_of(e)
            if (e.get("at") or 0) <= self.seen.get(k, 0.0):
                continue
            out.setdefault(k, []).append(e)
        return {k: v[:limit] for k, v in out.items()}

    def unseen(self, limit=200, key=None):
        return self.unseen_by_key(limit).get(key, [])

    def context(self, limit=40, max_age=0.0, key=None):
        # Read-only window used to build the prompt; every call is recorded so the
        # test can prove no group's context ever holds another group's messages.
        evs = [e for e in self.events if key is None or self.key_of(e) == key]
        evs = evs[-limit:]
        self.contexts.append(evs)
        return evs

    def mark_looked(self, through_id, key=None):
        # Move one conversation's watermark to the timestamp of the given message id.
        # max() matters: an event arriving out of order must not move it backwards.
        for e in self.events:
            if e["id"] == through_id:
                k = key or self.key_of(e)
                self.seen[k] = max(self.seen.get(k, 0.0), e.get("at") or 0.0)
                return self.seen[k]
        return 0.0

    def mark_processed(self, through_id):
        # The legacy single-key entry point still fans out to the per-key one.
        return self.mark_looked(through_id)

    def note_own_send(self):
        self.own_sends += 1


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

    async def run(self, system, task, history=None, **kw):
        self.tasks.append(task)
        return {"text": self.text, "steps": 1, "tool_calls": []}


# One normalized event. The timestamp defaults to the id, so ordering matches the
# order in which the events were built.
def ev(i, text="在吗", *, uid="12345", mention=False, gid="1060667115",
       mtype="group", sender="路人", at=None):
    return {"id": i, "at": at if at is not None else float(i), "group_id": gid,
            "user_id": uid, "sender": sender, "message_type": mtype, "text": text,
            "is_self": False, "mentions_me": mention, "keyword_hit": None,
            "segments": [{"type": "text", "text": text}]}


# Wire up the whole pipeline with every delay at zero, so a single _tick() is
# deterministic and the test never has to wait on a timer.
def run(events, replies, *, agent_text="干完了", agent_loop=None):
    llm = FakeLLM(replies)
    store = FakeStore()
    bus = FakeBus(events)
    bot = FakeBot()
    agent = QqAgent(llm, "你是被测试的机器人。", store=store)
    loop = agent_loop if agent_loop is not None else FakeAgentLoop(agent_text)
    ar = AutoReply(bus, store, agent, bot, FakeControl(), enabled=True,
                   agent_loop=loop, settle_seconds=0, max_batch_age=0, multi_send_gap=0)
    asyncio.run(ar._tick())
    return llm, store, bot, bus, loop


def sent(bot):
    # Group sends only; private replies go out with a different action.
    return [m for m in bot.msgs if m["action"] == "send_group_msg"]


ok = []

llm, store, bot, bus, _ = run([ev(1, "今天中午吃啥啊"), ev(2, "随便")],
                              ['{"replies": ["吃我"], "reason": "能接"}'])
assert len(sent(bot)) == 1 and sent(bot)[0]["message"] == "吃我", bot.msgs
assert len(llm.calls) == 1
assert bus.seen.get("1060667115") == 2.0, "应当推进本群水位线"
ok.append("闲聊：整段只问模型一次，接了并在整批处理完后推进水位线")

llm, store, bot, bus, _ = run([ev(1, "早"), ev(2, "冒泡")],
                              ['{"replies": [], "reason": "没意思"}'])
assert not bot.msgs
assert bus.seen.get("1060667115") == 2.0, "沉默也要推进"
assert store.states("auto_silent")
ok.append("闲聊：沉默时不发消息，但水位线照推（同一段不会看第二遍）")

events = [ev(1, "A 群在聊红石", gid="1060667115"),
          ev(2, "B 群在聊吃饭", gid="1083430859")]
llm, store, bot, bus, _ = run(events, [
    '{"replies": ["红石那个我熟"], "reason": "A 群能接"}',
    '{"replies": ["吃什么"], "reason": "B 群能接"}',
])
groups = [(m["group_id"], m["message"]) for m in sent(bot)]
assert groups == [(1060667115, "红石那个我熟"), (1083430859, "吃什么")], groups
for ctx in bus.contexts:
    gids = {bus.key_of(e) for e in ctx}
    assert len(gids) == 1, f"上下文串群了：{gids}"
assert bus.seen == {"1060667115": 1.0, "1083430859": 2.0}, bus.seen
ok.append("两个群各处理各的：回复发给各自群，上下文不跨群，水位线各记各的")

llm, store, bot, bus, _ = run(
    [ev(1, "冒泡", gid="1060667115"), ev(2, "这游戏真难", gid="1083430859")],
    ['{"replies": [], "reason": "A 群没意思"}',
     '{"replies": ["确实"], "reason": "B 群能接"}'])
assert len(sent(bot)) == 1 and sent(bot)[0]["group_id"] == 1083430859, bot.msgs
ok.append("一个群沉默不会吃掉另一个群的回复")

llm, store, bot, bus, _ = run([ev(1, "你玩mc吗")],
                              ['{"replies": ["玩", "你哪个服"], "reason": "想聊"}'])
assert len(sent(bot)) == 2, f"两条回复应当发两次，实际 {len(sent(bot))}"
assert [m["message"] for m in sent(bot)] == ["玩", "你哪个服"]
ok.append("多条回复分成多次 send（不是塞进一条里）")

llm, store, bot, bus, _ = run([ev(1, "@机器人 说话", mention=True)],
                              ['{"replies": [], "reason": "不想说"}', "在呢，咋了"])
assert len(sent(bot)) == 1 and sent(bot)[0]["message"] == "在呢，咋了", bot.msgs
ok.append("@ 兜底：模型沉默时会再问一次并发出")

loop = FakeAgentLoop("查到了")
llm, store, bot, bus, loop = run([ev(1, "@我 查下内存", uid="98704929", mention=True)],
                                 ['{"replies": ["不该走到这"], "reason": "x"}'],
                                 agent_loop=loop)
assert len(loop.tasks) == 1 and "查下内存" in loop.tasks[0]
assert sent(bot) and sent(bot)[0]["message"] == "查到了"
assert not llm.calls, "派活不该走批量闲聊决策"
ok.append("派活：管理员 @ 它 -> 直接调 Agent，完全不进批处理")

loop = FakeAgentLoop("好")
llm, store, bot, bus, loop = run([ev(1, "帮我看下", uid="98704929", gid="",
                                     mtype="private", mention=False)], [], agent_loop=loop)
assert len(loop.tasks) == 1
assert bot.msgs and bot.msgs[0]["action"] == "send_private_msg"
ok.append("派活：管理员私聊 -> 直接调 Agent")

loop = FakeAgentLoop("不该被调用")
llm, store, bot, bus, loop = run([ev(1, "今天好累", uid="98704929")],
                                 ['{"replies": [], "reason": "没意思"}'], agent_loop=loop)
assert not loop.tasks, "管理员闲聊不该被当成派活"
assert not bot.msgs
ok.append("边界：管理员在群里闲聊（没 @）走批处理，不会被当成命令")

# The newest event just arrived, so the 3s settle window has not elapsed yet: this
# tick must return without even asking the model.
llm = FakeLLM(['{"replies": ["接"], "reason": "x"}'])
store, bus, bot = FakeStore(), FakeBus([ev(1, "刚发的", at=time.time())]), FakeBot()
agent = QqAgent(llm, "p", store=store)
ar = AutoReply(bus, store, agent, bot, FakeControl(), settle_seconds=3, max_batch_age=45)
asyncio.run(ar._tick())
assert not bot.msgs and not llm.calls, "最后一条刚来，应当等静默期"
assert AutoReply(bus, store, agent, bot, FakeControl()).settle_seconds == 3.0
ok.append("攒一段：静默期内不出手（max_batch_age 兜底不会饿死）")

from qqbridge.agent import clean_reply, tidy_plain

STATUS = "宿主机状态：\nCPU：AMD R7 9800X3D\n内存：31.2GB"
assert clean_reply(STATUS)[0] == STATUS
assert clean_reply('{"reply": "第一行\\n第二行"}')[0] == "第一行\n第二行"
assert clean_reply('{"reply": "hi", ') == ("", "failed")
assert tidy_plain("a  \n\n\n\nb") == "a\n\nb"
ok.append("多行纯文本完整保留、坏 JSON 依旧沉默")


print("\n".join("PASS  " + x for x in ok))
print("ALL PASS")

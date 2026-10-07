"""自带的群聊 Agent：上下文管理 + 自主决定是否接话 + 发言。

设计：每个群一份独立历史（互不污染），只喂最近的若干条；被唤醒时让模型判断
"要不要接、接什么"，返回 JSON，避免把整段推理发到群里。
"""
from __future__ import annotations

import json
import logging
import time
from collections import deque
from pathlib import Path

from .llm import LLM, LLMError

log = logging.getLogger("qqbridge.agent")

DEFAULT_SYSTEM = """你是 QQ 群里的一个普通群友，不是客服、不是助手。

说话规则：
- 短。默认一句话，1~10 字，别超过一行。
- 口语、随意，允许"？""6""草""乐"这种。
- 不要总结、不要升华、不要"大家觉得呢"、不要免费人生建议。
- 别每条都回。没意思就沉默——沉默是允许的，也是最像人的选择。
- 别人没问你的时候，只有真的能接上话才开口；连说几句之后要留空隙。
- 被 @ 到必须回，但也要简短。

你会被要求以 JSON 回复，格式：
{"reply": "要发的话", "reason": "简短理由"}
不想说话时：{"reply": "", "reason": "..."}

reply 里只放你要发到群里的那句原话，不要带动作描写、不要带引号、不要解释。"""


class History:
    """一个群的滚动历史。"""

    def __init__(self, size: int = 30):
        self.lines: deque = deque(maxlen=size)

    def add(self, who: str, text: str, *, self_msg: bool = False):
        text = (text or "").strip().replace("\n", " ")
        if not text or text.startswith("[CQ:"):
            return
        tag = "我" if self_msg else who
        self.lines.append({"role": "user", "content": f"{tag}: {text[:300]}"})

    def messages(self) -> list[dict]:
        return list(self.lines)


class QqAgent:
    def __init__(self, llm: LLM, system_prompt: str = "", history_size: int = 30):
        self.llm = llm
        self.system_prompt = system_prompt or DEFAULT_SYSTEM
        self.histories: dict[str, History] = {}
        self.history_size = history_size
        self.self_id = ""
        self.decisions = 0
        self.replies = 0
        self.silences = 0

    def reconfigure(self, system_prompt: str):
        self.system_prompt = system_prompt or DEFAULT_SYSTEM

    def history(self, key: str) -> History:
        if key not in self.histories:
            self.histories[key] = History(self.history_size)
        return self.histories[key]

    def observe(self, ev: dict):
        """把收到的消息写进对应群的历史（不触发模型）。"""
        key = ev.get("group_id") or ev.get("user_id") or "?"
        self.history(key).add(ev.get("sender", "?"), ev.get("text", ""),
                              self_msg=bool(ev.get("is_self")))

    def note_own(self, key: str, text: str):
        self.history(key).add("我", text, self_msg=True)

    def status(self) -> dict:
        return {
            "model": self.llm.model,
            "configured": self.llm.configured,
            "conversations": len(self.histories),
            "decisions": self.decisions,
            "replies": self.replies,
            "silences": self.silences,
            "system_prompt_chars": len(self.system_prompt),
        }

    async def decide(self, ev: dict, *, force: bool = False) -> dict:
        """让模型决定这条要不要接。返回 {reply, reason}。"""
        if not self.llm.configured:
            raise LLMError("模型未配置。")

        key = ev.get("group_id") or ev.get("user_id") or "?"
        hist = self.history(key)
        # 当前这条已由 observe 写入历史；再拼一条指令
        ctx = hist.messages()[-self.history_size:]
        hint = "有人 @ 了你，必须回一句。" if ev.get("mentions_me") else \
               ("这条命中了你想跟的话题。" if ev.get("keyword_hit") else "群里有新消息。")
        if force:
            hint += "（群主点名让你说话）"

        messages = [
            {"role": "system", "content": self.system_prompt},
            *ctx,
            {"role": "user", "content": (
                f"【系统】{hint}\n"
                "以 JSON 回复：{\"reply\": \"要发的话\", \"reason\": \"简短理由\"}。"
                "不想说话就把 reply 留空。只输出 JSON。"
            )},
        ]
        self.decisions += 1
        out = await self.llm.chat(messages)
        raw = out["text"] or "{}"
        # 容忍模型包了 markdown 代码块
        if raw.startswith("```"):
            raw = raw.strip("`").strip()
            if raw.lower().startswith("json"):
                raw = raw[4:].strip()
        try:
            data = json.loads(raw)
        except ValueError:
            # 解析失败：把原文当回复，但截断防刷屏
            data = {"reply": raw.splitlines()[0][:200] if raw else "", "reason": "解析失败，按原文发送"}

        reply = (data.get("reply") or "").strip()
        if len(reply) > 500:
            reply = reply[:500]
        if reply:
            self.replies += 1
        else:
            self.silences += 1
        return {"reply": reply, "reason": str(data.get("reason") or "")[:200], "key": key}


def load_system_prompt(path: Path, fallback: str = "") -> str:
    """system prompt 回退链：

    1. 配置指定的文件（默认 data/system_prompt.md，用户自己改的那份）
    2. 仓库自带的 prompts/default_persona.md
    3. 代码里的 DEFAULT_SYSTEM

    这样用户改人格不会和仓库默认版本冲突；新克隆也有可用人格。
    """
    candidates = [path]
    try:
        candidates.append(path.parent.parent / "prompts" / "default_persona.md")
    except Exception:
        pass
    seen = set()
    for cand in candidates:
        key = str(cand)
        if key in seen:
            continue
        seen.add(key)
        try:
            if cand.is_file():
                text = cand.read_text(encoding="utf-8").strip()
                if text:
                    return text
        except OSError:
            log.warning("system prompt 文件读不到：%s", cand)
    return fallback or DEFAULT_SYSTEM
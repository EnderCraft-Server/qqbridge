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
    def __init__(self, llm: LLM, system_prompt: str = "", history_size: int = 30, store=None):
        self.llm = llm
        self.store = store
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
        # 默认应该是沉默：没点名、没命中关键词、也没人问你的时候，
        # 明确告诉模型「不必每条都接，挑你真正想说的那条」，它才会真的挑。
        if ev.get("mentions_me"):
            hint = "有人 @ 了你，必须回一句。"
        elif ev.get("keyword_hit"):
            hint = "这条命中了你想跟的话题。真有意思就接，只是撞词就留空。"
        else:
            hint = ("群里有新消息。**没意思就留空，不必每条都接** —— "
                    "挑你真正想说的那条回，想不到说什么就沉默。")
        if force:
            hint += "（群主点名让你说话）"

        messages = [
            {"role": "system", "content": self.system_prompt},
            *ctx,
            {"role": "user", "content": (
                f"【系统】{hint}\n"
                "以 JSON 回复：{\"reply\": \"要发的话\", \"reason\": \"简短理由\"}。"
                "不想说话就把 reply 留空 —— 这很常见，别勉强凑话。只输出 JSON。"
            )},
        ]
        self.decisions += 1
        out = await self.llm.chat(messages)
        raw = out["text"] or ""
        reply, how = clean_reply(raw)
        if len(reply) > 800:
            reply = reply[:800]
        if how == "failed":
            log.warning("decide: 输出解析失败，改判沉默。原文前 200 字：%s", raw[:200])
            if self.store:
                try:
                    self.store.audit("auto", "decide_parse_failed", key,
                                     {"raw": raw[:400]}, "SKIPPED")
                except Exception:
                    pass
        if reply:
            self.replies += 1
        else:
            self.silences += 1
        reason = str((parse_decision(raw)[0].get("reason") if how in ("ok", "fenced", "embedded") else "") or "")[:200]
        if how != "ok":
            reason = f"[{how}] " + reason
        return {"reply": reply, "reason": reason, "key": key, "parse": how}


def parse_decision(raw: str) -> tuple[dict, str]:
    """把模型输出解析成 {reply, reason}。

    返回 (data, how)，how 取值：
      ok       直接就是合法 JSON
      fenced   剥掉 markdown 代码块后是合法 JSON
      embedded 从文本里抠出第一个 {...} 后解析成功
      plain    不是 JSON，当纯文本回复
      failed   看着像 JSON 但解析不了 —— 调用方必须改判沉默，绝不能发出去
    """
    text = (raw or "").strip()
    if not text:
        return {"reply": "", "reason": ""}, "ok"

    def try_load(candidate: str):
        try:
            obj = json.loads(candidate)
            return obj if isinstance(obj, dict) else None
        except ValueError:
            return None

    obj = try_load(text)
    if obj is not None:
        return obj, "ok"

    stripped = text
    if stripped.startswith("```"):
        stripped = stripped.strip("`").strip()
        if stripped[:4].lower() == "json":
            stripped = stripped[4:].strip()
        obj = try_load(stripped)
        if obj is not None:
            return obj, "fenced"

    # 从文本里抠第一个大括号块（模型偶尔会在 JSON 前后加话）
    start = stripped.find("{")
    if start >= 0:
        depth, end = 0, -1
        for idx in range(start, len(stripped)):
            ch = stripped[idx]
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    end = idx
                    break
        if end > start:
            obj = try_load(stripped[start:end + 1])
            if obj is not None:
                return obj, "embedded"

    # 到这里说明没解析出 JSON。区分「本来就是纯文本」和「像 JSON 但坏了」
    looks_like_json = stripped[:1] in ("{", "[") or '"reply"' in stripped
    if looks_like_json:
        return {"reply": "", "reason": "JSON 解析失败"}, "failed"
    return {"reply": tidy_plain(stripped), "reason": "纯文本回复"}, "plain"


def tidy_plain(text: str) -> str:
    """整理纯文本回复：**保留换行**，只清行尾空白、把连续空行压成一个，最后卡长度。

    以前这里写的是 stripped.splitlines()[0][:200] —— 只取第一行。
    于是模型写好的多行回答被砍成开头那一句，群里只剩一个「？」或者「宿主机状态：」，
    看着像机器人在敷衍。QQ 消息本来就能换行，没必要砍。
    """
    lines = [ln.rstrip() for ln in (text or "").strip().splitlines()]
    out: list[str] = []
    for ln in lines:
        if not ln and (not out or not out[-1]):
            continue
        out.append(ln)
    return "\n".join(out).strip()[:800]


def clean_reply(raw: str) -> tuple[str, str]:
    """发送前的最后一道净化：保证进群的永远是一句人话，不会是 JSON。

    不管是 chat 模式还是 agent 模式，模型都可能吐 JSON —— 前者因为提示词要求，
    后者因为复用了同一份提示词。这里统一兜住。

    返回 (要发送的文本, 判定结果)。判定为 failed 时文本为空 = 沉默。
    """
    text, how = parse_decision(raw)
    if how == "ok":
        return (text.get("reply") or "").strip(), how
    if how in ("fenced", "embedded"):
        return (text.get("reply") or "").strip(), how
    if how == "plain":
        return (text.get("reply") or "").strip(), how
    # failed：像 JSON 但解析不了 —— 宁可沉默
    return "", how


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
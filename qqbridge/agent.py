"""自带的群聊 Agent：上下文管理 + 自主决定是否接话 + 发言。

设计：每个群一份独立历史（互不污染），只喂最近的若干条；被唤醒时让模型判断
"要不要接、接什么"，返回 JSON，避免把整段推理发到群里。
"""
from __future__ import annotations

import json
import logging
import re
import time
from collections import deque
from pathlib import Path

from . import academic, vision
from . import webpage
from .config import config
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

    async def decide_batch(self, batch: list[dict], *, fresh_from: int = 0) -> dict:
        """**整段**判断：把最近一段对话交给模型，让它决定要不要插一句。

        和按条 decide() 的根本区别：看的是「这一段」，不是「这一条」。
        人就是这个节奏 —— 隔一会儿扫一眼，判断整段值不值得接，
        而不是被每一条消息牵着回一句。这比逐条调 engagement 靠谱得多。

        fresh_from 之前的消息只是上下文（已经看过的），之后的是这次新冒出来的。

        只负责「闲聊要不要接、接什么」。**不判断有没有活要干** ——
        那件事由 autoreply 直接分派给内置 Agent，不掺和到这里来：
        Agent 是多步带工具的东西，让它在批量闲聊的 JSON 里捎带决定，
        既是两套逻辑搅在一起，也容易把该干的活降级成一句敷衍。

        返回 {reply, reason, key, parse, fresh, mention}。
        """
        if not self.llm.configured:
            raise LLMError("模型未配置。")

        key = (batch[-1].get("group_id") or "?") if batch else "?"

        lines = []
        fresh_texts: list[str] = []
        fresh_count = 0
        fresh_mention = False
        for ev in batch:
            who = "我" if ev.get("is_self") else (ev.get("sender") or ev.get("user_id") or "?")
            text = render_text(ev)
            is_fresh = ev.get("id", 0) > fresh_from
            if is_fresh:
                fresh_count += 1
                fresh_texts.append(text)
                if ev.get("mentions_me"):
                    fresh_mention = True
            lines.append(("[新] " if is_fresh else "    ") + f"{who}：{text}")

        hint = (
            "上面是你上次看群之后，群里新冒出来的一段（带 [新] 标记的那几行），"
            "前面没标记的是你已经在场看过的上下文，用来看懂在聊什么。\n\n"
            "你现在刚拿起手机扫了一眼。\n"
            "- 有人 @ 你、或者点了你的名字 —— **必须回一句**（被 @ 是欠一个回应，不是欠一场对骂；\n"
            "  连着被同一个人挑衅时，回一句之后就不再接，别对轰）。\n"
            "- 除此之外，**大多数时候你什么都不想说**。只有这段里真有让你想插一句的东西"
            "（有梗、能怼、你懂行、有话接）才回。\n"
            "- 只是「冒泡」「打卡」「早」「在吗」这种刷存在感的、或者单独一个「。」「？」「6」，一律不接。\n"
            "- 回就回一句，别逐条点评、别总结、别复述。\n"
            "- 不想说就留空 reply —— 这是最常见的正确答案。"
        )
        if fresh_count > 0 and not fresh_mention:
            hint += "\n\n（这一段里没有人 @ 你。）"

        # 学术模式：只拿「新来的那几条」判断 —— 旧上下文里出现过一道题，
        # 不该让后面十句闲聊都被答成论文。
        academic_on, academic_why = False, ""
        if config.academic_mode:
            academic_on, academic_why = academic.detect("\n".join(fresh_texts))
        if academic_on:
            system_content = academic.build_academic_system(self.system_prompt, json_mode=True)
            hint = ("【学术提问】带 [新] 标记的那几条里有学术/技术问题。"
                    "按学术模式认真作答：先结论后理由，该多长就多长，"
                    "不要用群友口吻敷衍，也不要怼人。")
            max_tokens = config.llm_academic_max_tokens
            reply_limit = config.academic_reply_chars
            log.info("学术模式触发（%s）：%s", academic_why, fresh_texts[-1][:40] if fresh_texts else "")
        else:
            system_content = self.system_prompt
            max_tokens = None
            reply_limit = 800

        # 群里发的图片：取回来一起喂给视觉模型。只带本轮新来的那几张 ——
        # 历史里的图不重复送，否则每轮都在为同一张图付 token。
        images = []
        if vision.vision_enabled():
            try:
                images = await vision.collect_for_turn(batch, fresh_from)
            except Exception:
                log.exception("vision: 收集图片失败，本轮按纯文本处理")
        # 消息里带链接：抓回正文再判断。带工具的 Agent 那条路管理员才走得通，
        # 闲聊这条路本来没有任何取网页的手段，链接就永远被无视了。
        pages = []
        if config.link_preview:
            try:
                pages = await webpage.collect_for_turn(batch, fresh_from)
            except Exception:
                log.exception("webpage: 抓链接失败，本轮按纯文本处理")
        if pages:
            for pg in pages:
                lines.append(f"[链接正文] {pg['url']} → {pg['text'][:1200]}")
            log.info("webpage: 本轮带上 %d 个链接正文", len(pages))

        convo_text = "以下是这个群最近的对话：\n" + "\n".join(lines)
        if images:
            convo_content: list = [{"type": "text", "text": convo_text}]
            for im in images:
                convo_content.append(
                    {"type": "text", "text": f"（{im['sender']} 发的图，见下）"})
                convo_content.append(
                    {"type": "image_url", "image_url": {"url": im["data_url"]}})
            convo_msg = {"role": "user", "content": convo_content}
            hint += ("\n\n本轮消息里带了图片，图就在下面 —— **先看清图里是什么再决定怎么回**，"
                     "不要假装看见了，也不要凭空猜内容；看不清就直说看不清。")
            log.info("vision: 本轮带上 %d 张图", len(images))
        else:
            convo_msg = {"role": "user", "content": convo_text}

        messages = [
            {"role": "system", "content": system_content},
            convo_msg,
            {"role": "user", "content":
                hint + '\n\n以 JSON 回复：{"reply": "要发的话", "reason": "一句理由"}。只输出 JSON。'},
        ]
        self.decisions += 1
        out = await self.llm.chat(messages, max_tokens=max_tokens)
        raw = out.get("text") or ""
        data, how = parse_decision(raw)
        reply, reason = "", ""
        if how == "failed":
            log.warning("decide_batch: 输出解析失败，改判沉默。原文前 200 字：%s", raw[:200])
            if self.store:
                try:
                    self.store.audit("auto", "decide_parse_failed", key, {"raw": raw[:400]}, "SKIPPED")
                except Exception:
                    pass
        else:
            reply = (data.get("reply") or "").strip()
            reason = str(data.get("reason") or "")[:200]
            reply, _ = clean_reply(json.dumps({"reply": reply}, ensure_ascii=False))
        # 学术模式按 academic_reply_chars 截断，不再套群聊的 800 字
        if len(reply) > reply_limit:
            reply = reply[:reply_limit]
        if reply:
            self.replies += 1
        else:
            self.silences += 1
        if how != "ok":
            reason = f"[{how}] " + reason
        return {"reply": reply, "reason": reason, "key": key,
                "parse": how, "fresh": fresh_count, "mention": fresh_mention,
                "academic": academic_on, "academic_why": academic_why}

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

        # 学术模式（单条路径同样适用）
        academic_on, academic_why = False, ""
        if config.academic_mode:
            academic_on, academic_why = academic.detect(ev.get("text") or "")
        if academic_on:
            system_content = academic.build_academic_system(self.system_prompt, json_mode=True)
            hint = "【学术提问】按学术模式认真作答：先结论后理由，该多长就多长，不怼人。"
            max_tokens = config.llm_academic_max_tokens
            reply_limit = config.academic_reply_chars
        else:
            system_content = self.system_prompt
            max_tokens = None
            reply_limit = 800

        messages = [
            {"role": "system", "content": system_content},
            *ctx,
            {"role": "user", "content": (
                f"【系统】{hint}\n"
                "以 JSON 回复：{\"reply\": \"要发的话\", \"reason\": \"简短理由\"}。"
                "不想说话就把 reply 留空 —— 这很常见，别勉强凑话。只输出 JSON。"
            )},
        ]
        self.decisions += 1
        out = await self.llm.chat(messages, max_tokens=max_tokens)
        raw = out["text"] or ""
        reply, how = clean_reply(raw)
        if len(reply) > reply_limit:
            reply = reply[:reply_limit]
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


CQ_LABEL = [
    (re.compile(r"\[CQ:image,[^\]]*\]"), "[图片]"),
    (re.compile(r"\[CQ:face,[^\]]*\]"), "[表情]"),
    (re.compile(r"\[CQ:record,[^\]]*\]"), "[语音]"),
    (re.compile(r"\[CQ:video,[^\]]*\]"), "[视频]"),
    (re.compile(r"\[CQ:at,qq=(\d+)[^\]]*\]"), r"@\1"),
    (re.compile(r"\[CQ:reply,[^\]]*\]"), ""),
    (re.compile(r"\[CQ:[^\]]*\]"), ""),
]


def render_text(ev: dict) -> str:
    """把一条消息渲染成给模型看的文本。

    优先用已经解析好的 segments —— 图片、表情只留个标记，
    别把一长串 CQ 码塞进上下文，既费 token 又干扰判断。
    没有 segments 时退回对 raw_message 做替换。
    """
    parts: list[str] = []
    for seg in ev.get("segments") or []:
        kind = seg.get("type")
        if kind == "text":
            parts.append(seg.get("text") or "")
        elif kind == "image":
            parts.append("[图片]")
        elif kind == "at":
            parts.append("@" + str(seg.get("qq") or ""))
        elif kind == "face":
            parts.append("[表情]")
        elif kind == "record":
            parts.append("[语音]")
        elif kind == "video":
            parts.append("[视频]")
    text = "".join(parts).strip()
    if not text:
        text = ev.get("text") or ""
        for pat, rep in CQ_LABEL:
            text = pat.sub(rep, text)
        text = text.strip()
    return (text or "[非文字内容]").replace("\n", " ")[:300]


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
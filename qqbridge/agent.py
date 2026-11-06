"""The built-in group-chat agent: context management + deciding on its own whether to
chime in + speaking.

Design: one independent history per group (so groups never pollute each other), and only the
most recent few messages are fed in; when woken, the model decides "whether to join in, and
with what", returning JSON, so a whole chain of reasoning never reaches the group.
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
from .textnorm import split_messages, tidy_outgoing

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
    """Rolling history for one group."""

    def __init__(self, size: int = 30):
        self.lines: deque = deque(maxlen=size)

    def add(self, who: str, text: str, *, self_msg: bool = False):
        text = (text or "").strip().replace("\n", " ")
        # Drop empty messages and raw CQ payloads; fold newlines so one message stays one line.
        if not text or text.startswith("[CQ:"):
            return
        tag = "我" if self_msg else who
        # 300 chars per message is plenty for context and keeps every turn cheap.
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
        # Histories are created lazily, so an idle group costs nothing until it speaks.
        if key not in self.histories:
            self.histories[key] = History(self.history_size)
        return self.histories[key]

    def observe(self, ev: dict):
        """Append an incoming message to the right group history (does not call the model)."""
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
        """**Whole-segment** decision: hand the latest stretch of conversation to the model and
        let it decide whether to add a line.

        The fundamental difference from per-message decide(): it looks at "this stretch", not
        "this message". That is the human rhythm — glance at the phone every so often and judge
        whether the whole stretch is worth joining, instead of being dragged into a reply by
        every single message. This is far more reliable than tuning engagement per message.

        Messages before fresh_from are context only (already seen); the ones after it are what
        just came in this time.

        It only handles "chat: join or not, and with what". **It does not decide whether there
        is work to do** — autoreply dispatches that straight to the built-in Agent instead of
        mixing it in here: the Agent is a multi-step thing with tools, and making it tack that
        decision onto a batched-chat JSON both tangles two kinds of logic together and tends
        to downgrade the actual task into one perfunctory line.

        Returns {reply, reason, key, parse, fresh, mention}.
        """
        if not self.llm.configured:
            raise LLMError("模型未配置。")

        # All messages in a batch belong to one group; take it from the last event.
        key = (batch[-1].get("group_id") or "?") if batch else "?"

        lines = []
        fresh_texts: list[str] = []
        fresh_count = 0
        fresh_mention = False
        for ev in batch:
            who = "我" if ev.get("is_self") else (ev.get("sender") or ev.get("user_id") or "?")
            text = render_text(ev)
            # Message ids are monotonic per group, so "id > fresh_from" is the new/seen split.
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
            "- 别逐条点评、别总结、别复述。\n"
            "- 想说的多就拆成 2~3 条短消息（replies 数组），程序会分几次发出去；\n"
            "  **不要在一个字符串里塞换行或空格来装多条**。\n"
            "- 每条都要短，能一句说完就一句。句尾不要加「。」。\n"
            "  想表达无语/尴尬就用「。。。」；想卖萌或玩梗可以在句尾加一个不闭合的「（」；\n"
            "  遇到很抽象的问题可以直接甩「？？？」。\n"
            "- 不想说就留空 replies —— 这是最常见的正确答案。"
        )
        if fresh_count > 0 and not fresh_mention:
            hint += "\n\n（这一段里没有人 @ 你。）"

        # Academic mode: judge from "just the new messages" only — once a question appears in
        # old context, the next ten lines of chit-chat must not all be answered like a paper.
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

        # Images posted in the group: fetch them and feed them to the vision model as well.
        # Only the ones new to this turn — historical images are not resent, otherwise we would
        # pay tokens for the same image every turn.
        images = []
        if vision.vision_enabled():
            try:
                images = await vision.collect_for_turn(batch, fresh_from)
            except Exception:
                log.exception("vision: 收集图片失败，本轮按纯文本处理")
        # Links in messages: fetch the body first, then decide. The tool-using Agent path is
        # only reachable by an admin, so the chit-chat path had no way at all to fetch a page
        # and links were simply ignored forever.
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
                hint + '\n\n以 JSON 回复：{"replies": ["第一句", "第二句"], "reason": "一句理由"}。'
                '只发一条就写成只有一项的数组。不说话就空数组。只输出 JSON。'},
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
            reason = str(data.get("reason") or "")[:200]
            raw_parts: list = []
            arr = data.get("replies")
            if isinstance(arr, list):
                raw_parts.extend(arr)
            one = (data.get("reply") or "").strip()      # also accept the old single-reply format
            if one:
                raw_parts.append(one)
            if academic_on:
                # academic answers stay unsplit and keep their terminal punctuation
                joined, _ = clean_reply(json.dumps(
                    {"reply": "\n".join(str(x) for x in raw_parts)}, ensure_ascii=False))
                parts = [joined[:reply_limit]] if joined else []
            else:
                parts = []
                for piece in split_messages(raw_parts):
                    cleaned, _ = clean_reply(piece)
                    if cleaned:
                        parts.append(cleaned[:reply_limit])
            reply = "\n".join(parts)                    # compat field: the merged view
        if parts:
            self.replies += len(parts)
        else:
            self.silences += 1
        if how != "ok":
            reason = f"[{how}] " + reason
        return {"reply": reply, "replies": parts, "reason": reason, "key": key,
                "parse": how, "fresh": fresh_count, "mention": fresh_mention,
                "academic": academic_on, "academic_why": academic_why}

    async def decide(self, ev: dict, *, force: bool = False) -> dict:
        """Let the model decide whether to take this message. Returns {reply, reason}."""
        if not self.llm.configured:
            raise LLMError("模型未配置。")

        key = ev.get("group_id") or ev.get("user_id") or "?"
        hist = self.history(key)
        # This message was already appended by observe; here we only add an instruction
        ctx = hist.messages()[-self.history_size:]
        # Silence should be the default: when you were not mentioned, nothing hit a keyword,
        # and nobody asked you, only an explicit "you do not have to answer everything, pick
        # the one you actually want to say" makes the model really pick.
        if ev.get("mentions_me"):
            hint = "有人 @ 了你，必须回一句。"
        elif ev.get("keyword_hit"):
            hint = "这条命中了你想跟的话题。真有意思就接，只是撞词就留空。"
        else:
            hint = ("群里有新消息。**没意思就留空，不必每条都接** —— "
                    "挑你真正想说的那条回，想不到说什么就沉默。")
        if force:
            hint += "（群主点名让你说话）"

        # Academic mode (applies to the single-message path too)
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
            # Unpack the history inline; the final user message carries the instruction.
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
    """Parse model output into {reply, reason}.

    Returns (data, how), where how is one of:
      ok       already valid JSON
      fenced   valid JSON once the markdown code fence is stripped
      embedded the first {...} block carved out of the text parsed successfully
      plain    not JSON at all, treat as a plain-text reply
      failed   looks like JSON but will not parse — the caller must turn this into silence and
               must never send it out
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

    # Carve out the first brace block (the model sometimes adds words around its JSON)
    start = stripped.find("{")
    if start >= 0:
        depth, end = 0, -1
        # Bracket-depth scan instead of a regex, so nested objects are captured whole.
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

    # Nothing parsed as JSON by now. Tell "plain text all along" apart from "broken JSON".
    looks_like_json = stripped[:1] in ("{", "[") or '"reply"' in stripped
    if looks_like_json:
        return {"reply": "", "reason": "JSON 解析失败"}, "failed"
    return {"reply": tidy_plain(stripped), "reason": "纯文本回复"}, "plain"


# Order matters: the specific types must be rewritten before the catch-all stripper below.
CQ_LABEL = [
    (re.compile(r"\[CQ:image,[^\]]*\]"), "[图片]"),
    (re.compile(r"\[CQ:face,[^\]]*\]"), "[表情]"),
    (re.compile(r"\[CQ:record,[^\]]*\]"), "[语音]"),
    (re.compile(r"\[CQ:video,[^\]]*\]"), "[视频]"),
    (re.compile(r"\[CQ:at,qq=(\d+)[^\]]*\]"), r"@\1"),
    (re.compile(r"\[CQ:reply,[^\]]*\]"), ""),
    (re.compile(r"\[CQ:[^\]]*\]"), ""),
]


# ---------- CQ code hardening ----------
# OneBot parses any [CQ:xxx] segment inside a string message. Our body text comes from the
# model, and the model is influenced by group messages — so **a group member can indirectly
# make the bot emit arbitrary CQ codes**:
#   caption = "[CQ:at,qq=all] come quick"  -> @everyone bomb
#   image = "https://a/x.png] [CQ:image,file=http://127.0.0.1:5700/b"
#     -> the first image passed review; the second went out unreviewed, with no SSRF check
# So: body text is always escaped; wherever we really do emit CQ codes (sending an image) we
# build them ourselves and pin the parameters down.
_CQ_ESCAPE = (("&", "&amp;"), ("[", "&#91;"), ("]", "&#93;"))


def escape_cq(text: str) -> str:
    """Escape CQ-code delimiters in body text so they display literally instead of becoming
    instructions.

    The order cannot be reversed: & must be replaced first, otherwise the &#91; produced by
    later replacements gets escaped a second time.
    """
    out = str(text or "")
    for ch, rep in _CQ_ESCAPE:
        out = out.replace(ch, rep)
    return out


def is_safe_cq_param(value: str) -> bool:
    """Whether this can be safely spliced into a CQ-code parameter slot.

    Anything that can close a CQ code early ([ ] & ,) is rejected outright — rejected means
    the send is refused, not "sanitize it and make do", because a sanitized URL is most
    likely no longer the image that was meant.
    """
    v = str(value or "")
    if not v or any(c in v for c in "[]&,") or any(c in v for c in " \t\r\n"):
        return False
    return True


def render_text(ev: dict) -> str:
    """Render one message as text for the model to read.

    Prefer the already-parsed segments — images and emoji become mere markers; a long
    CQ-code string is not stuffed into the context, which both costs tokens and disturbs
    judgement. Falls back to substituting on raw_message when there are no segments.
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
    # One line, 300 chars max: this text is fed to the model, not shown to a human.
    return (text or "[非文字内容]").replace("\n", " ")[:300]


def tidy_plain(text: str) -> str:
    """Tidy a plain-text reply: **keep line breaks**, only strip trailing whitespace,
    collapse runs of blank lines into one, and finally cap the length.

    This used to be stripped.splitlines()[0][:200] — first line only. So a multi-line answer
    the model had written properly got chopped down to its opening sentence, and the group
    was left with a "?" or "host status:", which looks like the bot is fobbing people off.
    QQ messages can contain line breaks anyway, so there is no reason to chop.
    """
    lines = [ln.rstrip() for ln in (text or "").strip().splitlines()]
    out: list[str] = []
    for ln in lines:
        if not ln and (not out or not out[-1]):
            continue
        out.append(ln)
    return "\n".join(out).strip()[:800]


def clean_reply(raw: str) -> tuple[str, str]:
    """The last wash before sending: what reaches the group is always human text, never JSON.

    Whether it is chat mode or agent mode, the model can spit out JSON — the former because
    the prompt asks for it, the latter because it reuses the same prompt. This catches all
    of it.

    Returns (text to send, verdict). On a failed verdict the text is empty = silence.
    """
    text, how = parse_decision(raw)
    if how == "ok":
        return (text.get("reply") or "").strip(), how
    if how in ("fenced", "embedded"):
        return (text.get("reply") or "").strip(), how
    if how == "plain":
        return (text.get("reply") or "").strip(), how
    # failed: looks like JSON but will not parse — prefer silence
    return "", how


def load_system_prompt(path: Path, fallback: str = "") -> str:
    """system prompt fallback chain:

    1. The file named by config (default data/system_prompt.md, the one the user edits)
    2. The repo's own prompts/default_persona.md
    3. DEFAULT_SYSTEM in this module

    That way a user editing the persona never conflicts with the repo default version, and a
    fresh clone still has a usable persona.
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
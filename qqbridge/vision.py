"""Images posted in the group: fetch them, and turn them into something the model can see.

Why the image URL cannot simply be handed to the model:
the QQ image CDN serves **signed, short-lived links** (the URL carries an rkey). If the
model provider fetched them itself, first it might not even be able to reach them (the
CDN lives in mainland China), and second the rkey expires -- by the time the model asks
for the file it may already be a 403. So we download the bytes ourselves and inline them
into the request as base64.

The host allowlist is kept identical to /api/image so that internal addresses can never
be fed into a model request (SSRF).

Usage control: images are expensive (a few hundred to a thousand-plus tokens each). Only
the **newly arrived** images of this turn are attached; images from history are not sent
again -- once a previous turn has seen them, that is enough.
"""
from __future__ import annotations

import base64
import logging
import re
from pathlib import Path
from urllib.parse import urlparse

from . import net

log = logging.getLogger("qqbridge.vision")

# Same allowlist as /api/image in server.py
IMAGE_HOST_OK = ("multimedia.nt.qq.com.cn", "multimedia.qpic.cn", "gchat.qpic.cn")
IMAGE_SUFFIX_OK = (".qpic.cn", ".qq.com")
IMAGE_MAX_BYTES = 6 * 1024 * 1024      # per-image size cap
MAX_IMAGES_PER_TURN = 3                # how many images one turn may attach

# Avoid re-downloading when the same image is reused
_cache: dict[str, str] = {}


def _host_allowed(url: str) -> str:
    """Return the allowed hostname, or an empty string when it is not allowed."""
    parsed = urlparse(url or "")
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or not host:
        return ""
    if not (host in IMAGE_HOST_OK or host.endswith(IMAGE_SUFFIX_OK)):
        return ""
    # Reject numeric-only hosts (raw IPs) outright
    if host.replace(".", "").isdigit():
        return ""
    return host


async def fetch_data_url(url: str) -> str | None:
    """Download one image and return a data:image/...;base64,... URL; None on failure.

    Never raises: every failure mode is reported the same way, as None.
    """
    host = _host_allowed(url)
    if not host:
        log.info("vision: 拒绝非白名单图床 %s", (url or "")[:60])
        return None
    if url in _cache:
        return _cache[url]
    try:
        import httpx
        resp = await net.arequest("GET", url, headers={"User-Agent": "qqbridge/1.0"},
                                  timeout=15.0, follow_redirects=True)
        resp.raise_for_status()
        body = resp.content
    except Exception as exc:
        log.warning("vision: 取图失败 %s: %s", host, type(exc).__name__)
        return None
    if not body:
        return None
    if len(body) > IMAGE_MAX_BYTES:
        log.warning("vision: 图片过大 %d bytes，跳过", len(body))
        return None
    ctype = (resp.headers.get("content-type") or "image/jpeg").split(";")[0].strip()
    if not ctype.startswith("image/"):
        # QQ often labels a PNG/GIF as .jpg, so fall back to sniffing the magic bytes
        ctype = _sniff(body) or "image/jpeg"
    data_url = "data:%s;base64,%s" % (ctype, base64.b64encode(body).decode())
    # Crude bound: once the cache grows past 40 entries it is dropped wholesale, so a
    # long-running process cannot leak memory through an ever-growing dict of base64 blobs.
    if len(_cache) > 40:
        _cache.clear()
    _cache[url] = data_url
    return data_url


def _sniff(b: bytes) -> str:
    # Magic-byte sniffing only; returns "" when nothing matches and the caller falls back.
    if b[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if b[:3] == b"GIF":
        return "image/gif"
    if b[:2] == b"\xff\xd8":
        return "image/jpeg"
    if b[:4] == b"RIFF" and b[8:12] == b"WEBP":
        return "image/webp"
    return ""


def image_urls_in(ev: dict) -> list[str]:
    """The image URLs carried by one message."""
    out = []
    for seg in ev.get("segments") or []:
        if seg.get("type") == "image" and seg.get("url"):
            out.append(seg["url"])
    if not out and ev.get("text"):
        # Fallback path: no segments were parsed, so dig the URLs out of the CQ codes
        import re
        out = re.findall(r"\[CQ:image,[^\]]*url=([^,\]]+)", ev["text"])
    return out


async def collect_for_turn(events: list[dict], fresh_from: int = 0,
                           limit: int = MAX_IMAGES_PER_TURN) -> list[dict]:
    """Pick the images to feed to the model this turn.

    Only messages with id > fresh_from are considered, and at most "limit" images are
    kept; the oldest ones are dropped when over the cap (images are expensive, so it is
    better to miss one than to let a single request grow without bound).
    Returns [{"sender":..., "id":..., "data_url":...}].
    """
    picked: list[dict] = []
    for ev in events:
        if ev.get("id", 0) <= fresh_from:
            continue
        for u in image_urls_in(ev):
            picked.append({"sender": ev.get("sender") or ev.get("user_id") or "?",
                           "id": ev.get("id"), "url": u})
    if not picked:
        return []
    if len(picked) > limit:
        log.info("vision: 本轮 %d 张图，只带最近 %d 张", len(picked), limit)
        picked = picked[-limit:]
    out = []
    for item in picked:
        data_url = await fetch_data_url(item["url"])
        if data_url:
            out.append({"sender": item["sender"], "id": item["id"], "data_url": data_url})
    return out


# ---------- Review before sending an image ----------
# This went wrong once: someone asked the bot for a photo of "No. 600 Wanping South
# Road" and it casually grabbed some random anime-girl picture and posted it to the group.
# Root cause: before sending, **nobody had ever looked at the image** -- the model just
# passed a link or a path straight through. This step adds "take a look first": it checks
# both whether the image is safe and whether it is the one that was asked for.

REVIEW_SYSTEM = (
    "你是群聊机器人的发图审核员。你的判断直接决定这张图会不会被发到 QQ 群里。\n"
    "先客观描述画面，再判断两件事：\n"
    "1) unsafe：是否包含未成年人的性化或不雅内容、裸露或色情、暴力血腥、违法或极端内容。"
    "涉及未成年人的任何性化内容一律判 unsafe。\n"
    "2) match：这张图是否符合「预期用途」那一栏的描述。\n"
    "宁可错杀：拿不准就判 unsafe=true 或 match=false。\n"
    "只输出 JSON 对象，字段：desc（一句话客观描述）、unsafe（布尔）、"
    "unsafe_reason（字符串）、match（布尔）、match_reason（字符串）。"
)


def _as_bool(v):
    """Coerce the reviewer's output to True/False; None means unrecognized (= reject).

    **Testing "is True" alone is not enough.** This was hit in practice: when the model
    answers {"unsafe": "true", "match": "false"} (strings rather than booleans, very common
    with weak models) both the "unsafe is True" and "match is False" checks fall through,
    which silently bypasses the whole image gate -- the dangerous picture goes out anyway.
    So the strings are recognized explicitly here, and anything unrecognized fails closed.
    """
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return bool(v)
    s = str(v).strip().lower()
    if s in ("true", "yes", "y", "1", "是", "安全", "匹配", "符合"):
        return True
    if s in ("false", "no", "n", "0", "否", "不安全", "不匹配", "不符合"):
        return False
    return None


async def to_data_url(image: str) -> str | None:
    """Turn a local file path or an http(s) image link into a data URL. None on failure."""
    raw = (image or "").strip()
    if not raw:
        return None
    parsed = urlparse(raw)
    if parsed.scheme in ("http", "https") and parsed.hostname:
        return await fetch_data_url(raw)
    if parsed.scheme:
        return None
    p = Path(raw)
    if not p.is_file():
        return None
    try:
        body = p.read_bytes()
    except OSError:
        return None
    if not body or len(body) > IMAGE_MAX_BYTES:
        return None
    # _sniff already returns a **full MIME type** (image/png). This code used to prepend
    # "image/" a second time, producing data:image/image/png -- the model then answered 400
    # "unsupported image", so local images could never pass the review and could never be
    # sent. The URL path uses the HTTP content-type and was fine; only local files hit this.
    mime = _sniff(body[:16])
    if not mime:
        return None
    if not mime.startswith("image/"):
        mime = "image/jpeg" if mime == "jpg" else "image/" + mime
    return "data:%s;base64,%s" % (mime, base64.b64encode(body).decode())


async def review(data_url: str, expect: str = "") -> dict:
    """Have the vision model look at the image; returns {ok, desc, unsafe, match, reason}.

    ok=False means **do not send**. When the review is unavailable the verdict is
    **always reject** (fail closed) -- better to send nothing than to drop an unvetted
    image into the group just because it could not be looked at.
    """
    from .config import config
    from .llm import LLM, LLMError

    if not vision_enabled():
        return {"ok": False, "desc": "", "unsafe": None, "match": None,
                "reason": "当前模型不是视觉模型，无法在发送前核验图片内容。"}
    llm = LLM(config.llm_api_base, config.llm_api_key, config.llm_model, timeout=45)
    ask = REVIEW_SYSTEM + "\n\n【预期用途】" + (expect.strip() or "未说明，只做安全审核")
    try:
        out = await llm.chat([
            {"role": "user", "content": [
                {"type": "text", "text": ask},
                {"type": "image_url", "image_url": {"url": data_url}},
            ]},
        ], max_tokens=400)
    except Exception as exc:
        # Keep the real reason -- with only the exception type there is no way to tell
        # during debugging whether it was "image too large", "bad format" or "network down".
        detail = str(exc).strip().replace("\n", " ")[:200]
        log.warning("vision: 发图前核验失败：%s | %s", type(exc).__name__, detail)
        return {"ok": False, "desc": "", "unsafe": None, "match": None,
                "reason": "核验调用失败（%s）：%s" % (type(exc).__name__, detail or "无详情")}

    import json as _json
    text = (out.get("text") or "").strip()
    # Models often wrap the JSON in prose or a code fence, so grab the outermost {...}
    # block rather than parsing the whole reply; anything unparseable simply becomes {}.
    found = re.search(r"\{[\s\S]*\}", text)
    data = {}
    if found:
        try:
            data = _json.loads(found.group(0))
        except ValueError:
            data = {}
    desc = str(data.get("desc") or "").strip()
    unsafe = _as_bool(data.get("unsafe"))
    match = _as_bool(data.get("match"))
    reason = str(data.get("unsafe_reason") or data.get("match_reason") or "").strip()

    if not desc or unsafe is None or match is None:
        return {"ok": False, "desc": desc, "unsafe": unsafe, "match": match,
                "reason": "核验输出无法解析，保险起见不发。"}
    if unsafe:
        return {"ok": False, "desc": desc, "unsafe": True, "match": match,
                "reason": "内容不适合发到群里：" + (reason or "判定为不安全内容")}
    if expect.strip() and not match:
        return {"ok": False, "desc": desc, "unsafe": False, "match": False,
                "reason": "图和要发的东西对不上（图里实际是：%s）。%s" % (desc, reason)}
    return {"ok": True, "desc": desc, "unsafe": False, "match": match, "reason": reason}


def vision_enabled() -> bool:
    """On only when the model name mentions vision -- otherwise images just cause errors."""
    from .config import config
    return bool(getattr(config, "vision_enabled", True)) and "vision" in (config.llm_model or "").lower()

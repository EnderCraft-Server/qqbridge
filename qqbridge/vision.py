"""群里发的图片：取回来、转成模型能看的形式。

为什么不能直接把图片 URL 丢给模型：
QQ 图床是**签名过的临时链接**（URL 里带 rkey）。让模型服务商自己去拉，
一是它未必出得去（图床在国内），二是 rkey 会过期 —— 等模型去取的时候可能已经 403。
所以这里自己下载、转成 base64 内联进请求。

主机白名单与 /api/image 保持一致，防止把内网地址喂进模型请求（SSRF）。

用量控制：图片很贵（一张几百到上千 token）。只带**本轮新来的**几张，
历史消息里的图片不重复送 —— 上一轮看过就看过了。
"""
from __future__ import annotations

import base64
import logging
import re
from pathlib import Path
from urllib.parse import urlparse

log = logging.getLogger("qqbridge.vision")

# 与 server.py 的 /api/image 同一份白名单
IMAGE_HOST_OK = ("multimedia.nt.qq.com.cn", "multimedia.qpic.cn", "gchat.qpic.cn")
IMAGE_SUFFIX_OK = (".qpic.cn", ".qq.com")
IMAGE_MAX_BYTES = 6 * 1024 * 1024      # 单张上限
MAX_IMAGES_PER_TURN = 3                # 一轮最多带几张

# 复用同一张图时别反复下载
_cache: dict[str, str] = {}


def _host_allowed(url: str) -> str:
    """返回允许的主机名；不允许则返回空串。"""
    parsed = urlparse(url or "")
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or not host:
        return ""
    if not (host in IMAGE_HOST_OK or host.endswith(IMAGE_SUFFIX_OK)):
        return ""
    # 纯数字主机（IP）一律拒绝
    if host.replace(".", "").isdigit():
        return ""
    return host


async def fetch_data_url(url: str) -> str | None:
    """下载一张图，返回 data:image/...;base64,... —— 失败返回 None，绝不抛。"""
    host = _host_allowed(url)
    if not host:
        log.info("vision: 拒绝非白名单图床 %s", (url or "")[:60])
        return None
    if url in _cache:
        return _cache[url]
    try:
        import httpx
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
            resp = await client.get(url, headers={"User-Agent": "qqbridge/1.0"})
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
        # QQ 常常报 .jpg 实际是 PNG/GIF，按 magic bytes 兜一下
        ctype = _sniff(body) or "image/jpeg"
    data_url = "data:%s;base64,%s" % (ctype, base64.b64encode(body).decode())
    if len(_cache) > 40:
        _cache.clear()
    _cache[url] = data_url
    return data_url


def _sniff(b: bytes) -> str:
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
    """一条消息里带的图片地址。"""
    out = []
    for seg in ev.get("segments") or []:
        if seg.get("type") == "image" and seg.get("url"):
            out.append(seg["url"])
    if not out and ev.get("text"):
        # 退化路径：没解析出 segments 时从 CQ 码里抠
        import re
        out = re.findall(r"\[CQ:image,[^\]]*url=([^,\]]+)", ev["text"])
    return out


async def collect_for_turn(events: list[dict], fresh_from: int = 0,
                           limit: int = MAX_IMAGES_PER_TURN) -> list[dict]:
    """挑出这一轮要喂给模型的图片。

    只取 id > fresh_from 的新消息，最多 limit 张（超了就丢最旧的，
    图片太贵，宁可漏看也不能让一轮请求无限膨胀）。
    返回 [{"sender":..., "id":..., "data_url":...}]。
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


# ---------- 发图前的校验 ----------
# 出过事：有人让 bot 发「宛平南路六百号」的照片，它随手抓了一张萝莉图发进群。
# 根因是发送前**没有任何人看过这张图** —— 模型只是把一条链接/路径转手丢出去。
# 这一步补上「先看一眼」：既查安不安全，也查是不是要的那张。

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


async def to_data_url(image: str) -> str | None:
    """把「本地文件路径」或「http(s) 图片链接」变成 data URL。失败返回 None。"""
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
    kind = _sniff(body[:16])
    if not kind:
        return None
    mime = "image/jpeg" if kind == "jpg" else "image/" + kind
    return "data:%s;base64,%s" % (mime, base64.b64encode(body).decode())


async def review(data_url: str, expect: str = "") -> dict:
    """让视觉模型看一眼这张图，返回 {ok, desc, unsafe, match, reason}。

    ok=False 表示**不要发**。校验不可用时**一律判不通过**（fail closed）——
    宁可发不出去，也不要因为「看不了」就把没过目的图丢进群。
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
        log.warning("vision: 发图前核验失败：%s", str(exc)[:120])
        return {"ok": False, "desc": "", "unsafe": None, "match": None,
                "reason": "核验调用失败（%s），保险起见不发。" % type(exc).__name__}

    import json as _json
    text = (out.get("text") or "").strip()
    found = re.search(r"\{[\s\S]*\}", text)
    data = {}
    if found:
        try:
            data = _json.loads(found.group(0))
        except ValueError:
            data = {}
    desc = str(data.get("desc") or "").strip()
    unsafe = data.get("unsafe")
    match = data.get("match")
    reason = str(data.get("unsafe_reason") or data.get("match_reason") or "").strip()

    if not desc or unsafe is None or match is None:
        return {"ok": False, "desc": desc, "unsafe": unsafe, "match": match,
                "reason": "核验输出无法解析，保险起见不发。"}
    if unsafe is True:
        return {"ok": False, "desc": desc, "unsafe": True, "match": match,
                "reason": "内容不适合发到群里：" + (reason or "判定为不安全内容")}
    if expect.strip() and match is False:
        return {"ok": False, "desc": desc, "unsafe": False, "match": False,
                "reason": "图和要发的东西对不上（图里实际是：%s）。%s" % (desc, reason)}
    return {"ok": True, "desc": desc, "unsafe": False, "match": match, "reason": reason}


def vision_enabled() -> bool:
    """模型名里带 vision 才启用 —— 否则塞图片过去只会报错。"""
    from .config import config
    return bool(getattr(config, "vision_enabled", True)) and "vision" in (config.llm_model or "").lower()

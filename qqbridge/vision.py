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


def vision_enabled() -> bool:
    """模型名里带 vision 才启用 —— 否则塞图片过去只会报错。"""
    from .config import config
    return bool(getattr(config, "vision_enabled", True)) and "vision" in (config.llm_model or "").lower()

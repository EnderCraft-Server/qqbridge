"""群里发的链接：抓回正文，让 bot 能"看"到，而不是只看到一串 URL。

为什么需要单独一个模块：
内置 Agent 那条路（管理员 @ 才走）本来就有 fetch_url。但日常闲聊走的是
**不带工具**的批量决策路径 —— 那边收到 "看看这个 https://..." 只能干瞪眼，
因为模型手上没有任何取网页的手段。于是链接就永远被忽略了。

安全：这里是**面向任意用户输入**的抓取，比 agent 的 fetch_url 更要收紧 ——
拒绝内网/回环/链路本地地址，避免被拿来探测内网（SSRF）。
"""
from __future__ import annotations

import ipaddress
import logging
import re
import socket
from urllib.parse import urlparse

log = logging.getLogger("qqbridge.webpage")

MAX_BYTES = 300 * 1024
MAX_TEXT = 4000          # 塞进上下文的正文上限（字符）
TIMEOUT = 15.0
UA = "Mozilla/5.0 (compatible; qqbridge/1.0)"

URL_RE = re.compile(r"https?://[^\s<>'\"）)】]+", re.I)


def _is_private_host(host: str) -> bool:
    """主机是不是内网/本机 —— 是就拒绝。"""
    try:
        infos = socket.getaddrinfo(host, None)
    except Exception:
        return True                      # 解析不出来，按不安全处理
    for info in infos:
        addr = info[4][0]
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError:
            return True
        if (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_reserved or ip.is_multicast or ip.is_unspecified):
            return True
    return False


def urls_in(text: str) -> list[str]:
    return URL_RE.findall(text or "")


def find_in_events(events: list[dict], fresh_from: int = 0, limit: int = 2) -> list[str]:
    """收集本轮新消息里的链接（去重、限量）。"""
    seen: list[str] = []
    for ev in events:
        if ev.get("id", 0) <= fresh_from:
            continue
        for u in urls_in(ev.get("text") or ""):
            u = u.rstrip(".,;，。；")
            if u not in seen:
                seen.append(u)
    return seen[-limit:]


async def fetch_text(url: str) -> dict | None:
    """抓一个网页并返回 {url, status, text}。失败返回 None，不抛。"""
    parsed = urlparse(url or "")
    host = (parsed.hostname or "").lower()
    if parsed.scheme not in ("http", "https") or not host:
        return None
    if _is_private_host(host):
        log.info("webpage: 拒绝内网地址 %s", host)
        return None
    try:
        import httpx
        async with httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=True,
                                     headers={"User-Agent": UA}) as client:
            resp = await client.get(url)
        raw = resp.content[:MAX_BYTES]
    except Exception as exc:
        log.warning("webpage: 抓取失败 %s: %s", host, type(exc).__name__)
        return None
    ctype = (resp.headers.get("content-type") or "").split(";")[0].strip().lower()
    text = raw.decode(resp.encoding or "utf-8", errors="replace")
    if "html" in ctype or "<html" in text[:500].lower():
        text = re.sub(r"<script[\s\S]*?</script>", " ", text, flags=re.I)
        text = re.sub(r"<style[\s\S]*?</style>", " ", text, flags=re.I)
        text = re.sub(r"<[^>]+>", " ", text)
        text = re.sub(r"&nbsp;?", " ", text)
        text = re.sub(r"\s{2,}", " ", text).strip()
    if not text.strip():
        return None
    return {"url": url, "status": resp.status_code, "text": text[:MAX_TEXT]}


async def collect_for_turn(events: list[dict], fresh_from: int = 0) -> list[dict]:
    """抓本轮出现的链接（最多两个），返回 [{url, status, text}]。"""
    out = []
    for u in find_in_events(events, fresh_from):
        got = await fetch_text(u)
        if got:
            out.append(got)
    return out

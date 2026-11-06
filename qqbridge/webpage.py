"""Links posted in the group: fetch the body so the bot can "see" the page rather
than just a bare URL.

Why this needs a module of its own:
the built-in Agent path (only taken when an admin @s the bot) already has
fetch_url. But everyday chit-chat goes through the **tool-less** batch decision
path -- there, "look at this https://..." only earns a blank stare, because the
model has no way to fetch a page at all. So links were simply always ignored.

Security: this fetches **arbitrary user input**, so it has to be tighter than the
agent's fetch_url -- reject intranet/loopback/link-local addresses so it cannot be
used to probe the internal network (SSRF).
"""
from __future__ import annotations

import ipaddress
import logging
import re
import socket
from urllib.parse import urlparse

from . import net

log = logging.getLogger("qqbridge.webpage")

MAX_BYTES = 300 * 1024
MAX_TEXT = 4000          # cap on the body fed into the context (characters)
TIMEOUT = 15.0
UA = "Mozilla/5.0 (compatible; qqbridge/1.0)"

URL_RE = re.compile(r"https?://[^\s<>'\"）)】]+", re.I)


def _is_private_host(host: str) -> bool:
    """Is the host intranet/local -- if so, reject it."""
    try:
        infos = socket.getaddrinfo(host, None)
    except Exception:
        return True                      # unresolvable, so treat it as unsafe
    for info in infos:
        addr = info[4][0]
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError:
            return True
        # Any single resolved address being non-public disqualifies the whole host:
        # one private A record is enough to aim the request at the intranet.
        if (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_reserved or ip.is_multicast or ip.is_unspecified):
            return True
    return False


def urls_in(text: str) -> list[str]:
    return URL_RE.findall(text or "")


def find_in_events(events: list[dict], fresh_from: int = 0, limit: int = 2) -> list[str]:
    """Collect links from this round's new messages (deduplicated, capped)."""
    seen: list[str] = []
    for ev in events:
        # Only messages newer than the last watermark: links from earlier rounds
        # have already been fetched once.
        if ev.get("id", 0) <= fresh_from:
            continue
        for u in urls_in(ev.get("text") or ""):
            # Trailing punctuation is almost always sentence punctuation, not
            # part of the URL itself.
            u = u.rstrip(".,;，。；")
            if u not in seen:
                seen.append(u)
    return seen[-limit:]


async def fetch_text(url: str) -> dict | None:
    """Fetch one page and return {url, status, text}. Returns None on failure, never raises."""
    parsed = urlparse(url or "")
    host = (parsed.hostname or "").lower()
    # Only plain HTTP(S); file:, javascript: and friends are not our business.
    if parsed.scheme not in ("http", "https") or not host:
        return None
    if _is_private_host(host):
        log.info("webpage: 拒绝内网地址 %s", host)
        return None
    try:
        import httpx
        resp = await net.arequest("GET", url, headers={"User-Agent": UA},
                                  timeout=TIMEOUT, follow_redirects=True)
        raw = resp.content[:MAX_BYTES]
    except Exception as exc:
        log.warning("webpage: 抓取失败 %s: %s", host, type(exc).__name__)
        return None
    ctype = (resp.headers.get("content-type") or "").split(";")[0].strip().lower()
    text = raw.decode(resp.encoding or "utf-8", errors="replace")
    # Strip markup down to plain text: the model should not spend context on tags.
    if "html" in ctype or "<html" in text[:500].lower():
        text = re.sub(r"<script[\s\S]*?</script>", " ", text, flags=re.I)
        text = re.sub(r"<style[\s\S]*?</style>", " ", text, flags=re.I)
        text = re.sub(r"<[^>]+>", " ", text)
        text = re.sub(r"&nbsp;?", " ", text)
        text = re.sub(r"\s{2,}", " ", text).strip()
    # Nothing readable left after stripping (empty page, image-only, pure script):
    # hand back None so the caller simply omits it.
    if not text.strip():
        return None
    return {"url": url, "status": resp.status_code, "text": text[:MAX_TEXT]}


async def collect_for_turn(events: list[dict], fresh_from: int = 0) -> list[dict]:
    """Fetch the links that appeared this round (at most two), returning [{url, status, text}]."""
    out = []
    for u in find_in_events(events, fresh_from):
        got = await fetch_text(u)
        if got:
            out.append(got)
    return out

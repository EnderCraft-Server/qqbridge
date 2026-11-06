"""Unified egress policy for outbound HTTP clients.

The pitfall (2026-10-25): the host machine had a VPN client running (system proxy
127.0.0.1:7897), and the bot process was started from a terminal carrying
HTTPS_PROXY. Once the VPN was shut down, httpx's default trust_env=True sent every
request (api.deepseek.com included) to that proxy with nobody listening anymore,
so each one raised ConnectError: All connection attempts failed and the whole bot
went mute -- auto-chat, replies and Agent all dead, only the fallback canned lines
could still go out.

onebot.py had already been patched separately for loopback addresses (loopback
never goes through a proxy), but that only saved local services; the real
outbound clients (model, image reading, web fetching, console image proxy) were
still running on bare defaults.

The rules here:
  1. **Loopback addresses always go direct**, a proxy cannot help and may well get
     in the way.
  2. **Remote addresses try the system proxy first and fall back to a direct
     connection when that fails**, and the outcome is remembered: every request
     after that follows the path that last succeeded, instead of burning a full
     timeout on the dead proxy each time.
  3. When neither works, raise the real reason (proxy address included) instead of
     yet another baffling "All connection attempts failed".
"""
from __future__ import annotations

import ipaddress
import logging
from urllib.parse import urlparse

import httpx

log = logging.getLogger("qqbridge.net")

# How the remote connection last succeeded: None = not probed yet,
# "proxy"/"direct" = the remembered verdict
_REMOTE_MODE: str | None = None

_CONN_ERRORS = (httpx.ConnectError, httpx.ConnectTimeout, httpx.ProxyError,
                httpx.ReadTimeout, httpx.RemoteProtocolError)


def is_loopback(host: str) -> bool:
    host = (host or "").strip("[]").lower()
    if host in ("localhost", "::1", "0.0.0.0"):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _order(host: str) -> list[bool]:
    """Which order to try trust_env in. True = use the proxy from the environment."""
    if is_loopback(host):
        return [False]                 # loopback is always direct, no second option
    if _REMOTE_MODE == "direct":
        return [False, True]
    if _REMOTE_MODE == "proxy":
        return [True, False]
    return [True, False]               # never probed: honour the system setting first,
                                       # retry direct on failure


def _remember(host: str, trust_env: bool) -> None:
    global _REMOTE_MODE
    if is_loopback(host):
        return
    mode = "proxy" if trust_env else "direct"
    # Only log on an actual change, otherwise a stable setup would spam the log.
    if _REMOTE_MODE != mode:
        if _REMOTE_MODE is not None:
            log.warning("出口策略切换：%s（%s）", mode,
                        "代理连不上，改直连" if mode == "direct" else "代理恢复")
        else:
            log.info("出口策略：%s（%s）", mode, host)
        _REMOTE_MODE = mode


def _kwargs(timeout: float, follow_redirects: bool, trust_env: bool, proxy: str | None) -> dict:
    kw = {"timeout": timeout, "follow_redirects": follow_redirects, "trust_env": trust_env}
    if proxy and trust_env:
        kw["proxy"] = proxy
    return kw


def _fail_hint(host: str, exc: Exception) -> str:
    hint = f"{type(exc).__name__}: {str(exc)[:140]}"
    if _REMOTE_MODE == "proxy":
        hint += "（走的是系统代理，可能代理没开）"
    return hint


async def arequest(method: str, url: str, *, headers: dict | None = None,
                   json=None, params=None, data=None, timeout: float = 15.0,
                   follow_redirects: bool = False,
                   proxy: str | None = None) -> httpx.Response:
    """Send a request, falling back to a direct connection when the proxy does not work."""
    host = (urlparse(url).hostname or "").lower()
    last: Exception | None = None
    # Try each strategy in order; the one that works is remembered for next time.
    for trust_env in _order(host):
        try:
            async with httpx.AsyncClient(**_kwargs(timeout, follow_redirects,
                                                   trust_env, proxy)) as client:
                resp = await client.request(method, url, headers=headers, json=json,
                                            params=params, data=data)
            _remember(host, trust_env)
            return resp
        except _CONN_ERRORS as exc:
            last = exc
            if trust_env:
                log.warning("%s 经代理连不上（%s），改试直连", host, type(exc).__name__)
            continue
    raise RuntimeError(_fail_hint(host, last or RuntimeError("连接失败")))


def request(method: str, url: str, *, headers: dict | None = None,
            timeout: float = 20.0, follow_redirects: bool = True,
            proxy: str | None = None) -> httpx.Response:
    """Synchronous version, for sync tools such as agenttools' fetch_url."""
    host = (urlparse(url).hostname or "").lower()
    last: Exception | None = None
    for trust_env in _order(host):
        try:
            with httpx.Client(**_kwargs(timeout, follow_redirects,
                                        trust_env, proxy)) as client:
                resp = client.request(method, url, headers=headers)
            _remember(host, trust_env)
            return resp
        except _CONN_ERRORS as exc:
            last = exc
            if trust_env:
                log.warning("%s 经代理连不上（%s），改试直连", host, type(exc).__name__)
            continue
    raise RuntimeError(_fail_hint(host, last or RuntimeError("连接失败")))


def mode() -> str:
    """Current remote egress policy, for the status panel."""
    return _REMOTE_MODE or "unknown"

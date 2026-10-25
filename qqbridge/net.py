"""统一的出口 HTTP 客户端策略。

踩过的坑（2026-10-25）：宿主机开着梯子（系统代理 127.0.0.1:7897），
机器人进程是从带着 HTTPS_PROXY 的终端里启的。梯子关掉之后，
httpx 默认 trust_env=True 会把所有请求（包括 api.deepseek.com）丢给那个
已经没人监听的代理，于是每一条都 ConnectError: All connection attempts failed，
整个机器人哑掉 —— 自动说说、接话、Agent 全挂，只有兜底短句还能发出去。

onebot.py 之前为回环地址单独打过补丁（回环绝不走代理），但那只能救本机服务；
真正对外的那几个客户端（模型、看图、抓网页、控制台图片代理）还是裸的默认值。

这里的规矩：
  1. **回环地址永远直连**，代理帮不上忙还会挡路。
  2. **远端地址先按系统代理走，连不上就自动退回直连**，并把结果记住：
     之后所有请求都按上次成功的路径走，不会每次都在死代理上耗一个超时。
  3. 两种都不通时，把真实原因（含代理地址）抛出去，别再报一句
     不知所云的 "All connection attempts failed"。
"""
from __future__ import annotations

import ipaddress
import logging
from urllib.parse import urlparse

import httpx

log = logging.getLogger("qqbridge.net")

# 远端地址上一次是怎么连通的：None=还没探过，"proxy"/"direct"=记住的结论
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
    """按什么顺序试 trust_env。True = 用环境里的代理。"""
    if is_loopback(host):
        return [False]                 # 回环一律直连，没有第二种可能
    if _REMOTE_MODE == "direct":
        return [False, True]
    if _REMOTE_MODE == "proxy":
        return [True, False]
    return [True, False]               # 没探过：先尊重系统设置，失败再直连


def _remember(host: str, trust_env: bool) -> None:
    global _REMOTE_MODE
    if is_loopback(host):
        return
    mode = "proxy" if trust_env else "direct"
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
    """发一个请求，代理不通用时自动退回直连。"""
    host = (urlparse(url).hostname or "").lower()
    last: Exception | None = None
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
    """同步版，给 agenttools 的 fetch_url 这类同步工具用。"""
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
    """当前远端出口策略，给状态面板用。"""
    return _REMOTE_MODE or "unknown"

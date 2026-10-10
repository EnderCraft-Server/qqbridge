"""Minimal OneBot v11 client: HTTP calls + a persistent WebSocket event stream.

Nothing here blocks on the model; the WS task runs for the process lifetime and
hands every event to a callback.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time

import httpx
import websockets
from urllib.parse import urlparse

log = logging.getLogger("qqbridge.onebot")


class OneBotError(RuntimeError):
    pass


class OneBot:
    def __init__(self, http: str, ws: str, token: str = "", ws_token: str = ""):
        self.http = http.rstrip("/")
        self.ws = ws
        self.token = token
        self.ws_token = ws_token or token
        self._client: httpx.AsyncClient | None = None
        self._ws_task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self.connected = False
        self.last_event_at = 0.0
        self.self_id = ""
        self.on_event = None

    # ---------- lifecycle ----------
    def _trust_env(self) -> bool:
        """要不要让 httpx 读系统代理设置。

        **回环地址一律不走代理。** 踩过的坑：宿主机开了梯子（系统代理
        127.0.0.1:7897）之后，httpx 默认 trust_env=True 会把
        http://127.0.0.1:3000/get_login_info 也丢给代理，
        而代理不转发回环地址 -> ConnectError: All connection attempts failed
        -> 拿不到 self_id -> @ 消息全部识别不出来。

        OneBot 挂在本机时不需要代理；挂在远端时才跟随系统设置。
        """
        host = (urlparse(self.http).hostname or "").lower()
        if host in ("127.0.0.1", "localhost", "::1", "[::1]"):
            return False
        try:
            import ipaddress
            if ipaddress.ip_address(host).is_loopback:
                return False
        except ValueError:
            pass
        return True

    async def start(self, on_event):
        self.on_event = on_event
        self._client = httpx.AsyncClient(timeout=30.0, trust_env=self._trust_env())
        self._stop.clear()
        self._ws_task = asyncio.create_task(self._ws_loop())
        try:
            info = await self.call("get_login_info")
            self.self_id = str(info.get("user_id") or "")
        except Exception as exc:  # pragma: no cover - startup diagnostics only
            log.warning("get_login_info failed: %s", exc)
        return self

    async def stop(self):
        self._stop.set()
        if self._ws_task:
            self._ws_task.cancel()
            try:
                await self._ws_task
            except (asyncio.CancelledError, Exception):
                pass
        if self._client:
            await self._client.aclose()

    # ---------- HTTP ----------
    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.token}"} if self.token else {}

    def _ws_headers(self) -> dict:
        return {"Authorization": f"Bearer {self.ws_token}"} if self.ws_token else {}

    async def call(self, action: str, **payload):
        if self._client is None:
            raise OneBotError("client not started")
        resp = await self._client.post(
            f"{self.http}/{action}", json=payload, headers=self._headers()
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("status") != "ok":
            raise OneBotError(f"{action}: {data.get('wording') or data.get('retcode')}")
        return data.get("data")

    # ---------- WebSocket ----------
    async def _ws_loop(self):
        """常驻事件流。三条纪律：

        1. connected 必须随时反映真实状态 —— 控制台右上角的「QQ 已连接」是排查
           「收不到消息」的第一入口，它撒谎的话后面全是白费功夫。
        2. 每次重连之前都要睡一下。原来正常断开（服务端主动关）走的是 async for
           自然结束那条路，不经过 except，于是不 sleep 直接重连：
           对方要是「接了立刻关」，这里就成了死循环猛敲对面。
        3. 退避只有连着活得够久才重置，否则短连接会把退避重置成 1 秒，同样等于没有。
        """
        backoff = 1.0
        headers = self._ws_headers()
        while not self._stop.is_set():
            started = time.monotonic()
            try:
                async with websockets.connect(
                    self.ws, additional_headers=headers, ping_interval=20, ping_timeout=20
                ) as sock:
                    self.connected = True
                    log.info("event stream connected: %s", self.ws)
                    async for raw in sock:
                        try:
                            event = json.loads(raw)
                        except ValueError:
                            continue
                        self.last_event_at = event.get("time") or 0
                        if self.on_event is not None:
                            try:
                                self.on_event(event)
                            except Exception:
                                log.exception("event handler failed")
                log.warning("event stream closed by peer; reconnect in %.0fs", backoff)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("event stream lost (%s); retry in %.0fs", exc, backoff)
            finally:
                self.connected = False

            # 活过 30 秒才算「连上了」，短命连接继续按退避往上翻
            if time.monotonic() - started > 30.0:
                backoff = 1.0
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=backoff)
            except asyncio.TimeoutError:
                pass
            backoff = min(backoff * 2, 30.0)
        self.connected = False
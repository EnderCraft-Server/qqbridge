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
        """Whether httpx should read the system proxy settings.

        **Loopback addresses never go through a proxy.** The pitfall: once the host
        machine turned on a VPN (system proxy 127.0.0.1:7897), httpx's default
        trust_env=True also handed http://127.0.0.1:3000/get_login_info to the
        proxy, and the proxy does not forward loopback addresses ->
        ConnectError: All connection attempts failed -> self_id could not be
        fetched -> every @ message became unrecognizable.

        OneBot on the local machine needs no proxy; only a remote one follows the
        system settings.
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
                # Swallow everything: shutdown must never raise.
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
        # OneBot reports logical failures as status != "ok", with wording/retcode
        # explaining why; raise with that rather than a bare HTTP error.
        if data.get("status") != "ok":
            raise OneBotError(f"{action}: {data.get('wording') or data.get('retcode')}")
        return data.get("data")

    # ---------- WebSocket ----------
    async def _ws_loop(self):
        """Persistent event stream. Three rules:

        1. connected must reflect the real state at all times -- the "QQ connected"
           badge in the top-right of the console is the first place anyone looks
           when "no messages arrive"; if it lies, everything after that is wasted
           effort.
        2. Always sleep before reconnecting. A clean disconnect (the server closing
           on purpose) used to run the async for loop to its natural end, bypassing
           except, and then reconnect with no sleep: if the peer "accepts and
           immediately closes", this becomes a hot loop hammering the other side.
        3. Backoff is only reset once the connection has lived long enough;
           otherwise a short-lived connection resets it to 1 second, which again
           amounts to no backoff at all.
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

            # Only a connection that survived 30 seconds counts as "connected";
            # short-lived ones keep pushing the backoff up
            if time.monotonic() - started > 30.0:
                backoff = 1.0
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=backoff)
            except asyncio.TimeoutError:
                pass
            backoff = min(backoff * 2, 30.0)
        self.connected = False
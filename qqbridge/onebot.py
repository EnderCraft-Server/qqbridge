"""Minimal OneBot v11 client: HTTP calls + a persistent WebSocket event stream.

Nothing here blocks on the model; the WS task runs for the process lifetime and
hands every event to a callback.
"""
from __future__ import annotations

import asyncio
import json
import logging

import httpx
import websockets

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
    async def start(self, on_event):
        self.on_event = on_event
        self._client = httpx.AsyncClient(timeout=30.0)
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
        backoff = 1.0
        headers = self._ws_headers()
        while not self._stop.is_set():
            try:
                async with websockets.connect(
                    self.ws, additional_headers=headers, ping_interval=20, ping_timeout=20
                ) as sock:
                    self.connected = True
                    backoff = 1.0
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
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.connected = False
                log.warning("event stream lost (%s); retry in %.0fs", exc, backoff)
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=backoff)
                except asyncio.TimeoutError:
                    pass
                backoff = min(backoff * 2, 30.0)
        self.connected = False
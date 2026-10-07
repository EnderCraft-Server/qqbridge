"""自动接话循环：完全在 qqbridge 进程内跑，不依赖外部 Agent。

流程：
  事件总线（已有） → 按优先级取待处理 → 交给 QqAgent 决定要不要接
  → 要接就 send_message → 记入历史 → 推进游标

与 MCP 工具的关系：两条路并存。auto_reply 打开时 bot 自己说话；
外部 Agent 仍可通过 MCP 工具操作同一个队列（谁先 mark_processed 谁生效）。
"""
from __future__ import annotations

import asyncio
import logging
import time

log = logging.getLogger("qqbridge.autoreply")


class AutoReply:
    def __init__(self, bus, store, agent, bot, control, *, enabled: bool = True,
                 idle_seconds: float = 1.5, max_per_minute: int = 8):
        self.bus = bus
        self.store = store
        self.agent = agent
        self.bot = bot
        self.control = control
        self.enabled = enabled
        self.idle_seconds = idle_seconds
        self.max_per_minute = max_per_minute
        self._task = None
        self._stop = asyncio.Event()
        self._sent: list[float] = []
        self.last_action = ""
        self.last_at = 0.0
        self.errors = 0

    # ---------- 限流 ----------
    def _rate_ok(self) -> bool:
        now = time.time()
        self._sent = [t for t in self._sent if now - t < 60]
        return len(self._sent) < self.max_per_minute

    # ---------- 生命周期 ----------
    async def start(self):
        self._stop.clear()
        self._task = asyncio.create_task(self._loop())
        return self

    async def stop(self):
        self._stop.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass

    def status(self) -> dict:
        return {
            "enabled": self.enabled,
            "running": bool(self._task and not self._task.done()),
            "sent_last_minute": len([t for t in self._sent if time.time() - t < 60]),
            "last_action": self.last_action,
            "last_at": self.last_at,
            "errors": self.errors,
            "agent": self.agent.status() if self.agent else {},
        }

    # ---------- 主循环 ----------
    async def _loop(self):
        log.info("autoreply loop started (enabled=%s)", self.enabled)
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.idle_seconds)
                break
            except asyncio.TimeoutError:
                pass
            if not self.enabled or not self.control_allows():
                continue
            try:
                await self._tick()
            except Exception:
                self.errors += 1
                log.exception("autoreply tick failed")

    def control_allows(self) -> bool:
        try:
            return not self.control.paused()
        except Exception:
            return True

    async def _tick(self):
        if not self.agent or not self.agent.llm.configured:
            return
        pending = self.bus.pending(include_low=True, limit=10)
        if not pending:
            return
        if not self._rate_ok():
            log.info("autoreply: 每分钟上限，暂停接话")
            return

        # 一次处理一条，避免刷屏
        ev = pending[0]
        self.agent.observe(ev)
        decided = await self.agent.decide(ev)
        key = decided["key"]
        reply = decided["reply"]

        # 无论接不接，这条都算处理过
        through = ev["id"]
        if reply:
            gid = ev.get("group_id") or ev.get("user_id") or ""
            try:
                if ev.get("message_type") == "private" or not ev.get("group_id"):
                    await self.bot.call("send_private_msg", user_id=int(ev["user_id"]), message=reply)
                else:
                    await self.bot.call("send_group_msg", group_id=int(gid), message=reply)
                self._sent.append(time.time())
                self.bus.note_own_send()
                self.agent.note_own(key, reply)
                self.store.audit("auto", "auto_reply", gid, {"reply": reply, "reason": decided["reason"]},
                                 "SUCCEEDED")
                self.last_action = f"回复 {gid}: {reply[:40]}"
                log.info("autoreply: %s -> %s", gid, reply[:60])
            except Exception as exc:
                self.errors += 1
                self.last_action = f"发送失败: {exc}"
                log.warning("autoreply send failed: %s", exc)
        else:
            self.last_action = f"沉默（{decided['reason'][:40]}）"
            self.store.audit("auto", "auto_silent", ev.get("group_id", ""), {"reason": decided["reason"]},
                             "SUCCEEDED")
        self.last_at = time.time()
        self.bus.mark_processed(through)

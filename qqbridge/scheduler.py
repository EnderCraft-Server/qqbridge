"""Self-driving watch loop.

Runs inside the bridge process: every N seconds it looks at the bus, and when
there is something worth acting on it records a "turn" the host can pick up.

Crucially this never blocks on the model. It only decides *when* to wake, and
the wake signal is a queue row, not an HTTP call that hangs.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path

log = logging.getLogger("qqbridge.scheduler")

DEFAULTS = {
    "enabled": False,
    "interval_seconds": 300,     # 用户指定：每隔多久看一次群
    "min_gap_seconds": 30,       # 两次唤醒之间的硬下限
    "quiet_hours": [],           # 例如 ["23:30", "07:00"]
    "max_turns_per_hour": 60,
}


class Scheduler:
    def __init__(self, path: Path, bus, store):
        self.path = path
        self.bus = bus
        self.store = store
        self.settings = dict(DEFAULTS)
        self.last_run = 0.0
        self.next_run = 0.0
        self.runs = 0
        self.turns: list[dict] = []
        self._task = None
        self._stop = asyncio.Event()
        # 设置一改就戳一下，让循环立刻按新间隔重新排期。
        # 否则把间隔从 300 秒改成 10 秒，用户还得再等最多 300 秒才看到变化，
        # 表现就是「控制台改了没用」。
        self._wake = asyncio.Event()
        self._dirty = True
        self.load()

    # ---------- settings ----------
    def load(self):
        if self.path.is_file():
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
                for k in DEFAULTS:
                    if k in data:
                        self.settings[k] = data[k]
            except (OSError, ValueError):
                log.warning("scheduler settings unreadable; using defaults")
        return self.settings

    def save(self, patch: dict | None = None):
        if patch:
            for k, v in patch.items():
                if k in DEFAULTS:
                    self.settings[k] = v
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.settings, ensure_ascii=False, indent=1), encoding="utf-8")
        interval = max(5.0, float(self.settings.get("interval_seconds") or 60))
        self.next_run = time.time() + interval
        self._dirty = True
        self._wake.set()          # 唤醒循环，让改动马上生效
        return self.settings

    # ---------- quiet hours ----------
    @staticmethod
    def _minutes(hhmm: str) -> int | None:
        try:
            h, m = str(hhmm).split(":")
            return int(h) * 60 + int(m)
        except (ValueError, AttributeError):
            return None

    def in_quiet_hours(self, now=None) -> bool:
        spans = self.settings.get("quiet_hours") or []
        if not spans:
            return False
        lt = time.localtime(now or time.time())
        cur = lt.tm_hour * 60 + lt.tm_min
        for span in spans:
            if not isinstance(span, (list, tuple)) or len(span) != 2:
                continue
            a, b = self._minutes(span[0]), self._minutes(span[1])
            if a is None or b is None:
                continue
            if a <= b:
                if a <= cur < b:
                    return True
            elif cur >= a or cur < b:      # wraps midnight
                return True
        return False

    # ---------- loop ----------
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

    async def _loop(self):
        log.info("scheduler loop started")
        while not self._stop.is_set():
            try:
                interval = float(self.settings.get("interval_seconds") or 60)
            except (TypeError, ValueError):
                interval = 60.0
            interval = max(5.0, interval)

            # 刚改过设置（或刚启动）就立刻跑一轮，不等满一个周期
            wait = 0.0 if self._dirty else interval
            self._dirty = False
            self.next_run = time.time() + wait

            if wait > 0:
                try:
                    await asyncio.wait_for(self._wake.wait(), timeout=wait)
                    self._wake.clear()
                    self._dirty = True      # 设置变了，用新间隔重新排期
                    continue
                except asyncio.TimeoutError:
                    pass
                except asyncio.CancelledError:
                    raise
            if self._stop.is_set():
                break

            if not self.settings.get("enabled"):
                continue
            if self.in_quiet_hours():
                log.info("scheduler: quiet hours, skipping")
                continue
            gap = time.time() - self.last_run
            if gap < float(self.settings.get("min_gap_seconds") or 0):
                continue
            recent = [t for t in self.turns if time.time() - t["at"] < 3600]
            if len(recent) >= int(self.settings.get("max_turns_per_hour") or 999):
                log.info("scheduler: hourly cap reached")
                continue
            turn = self.tick()
            log.info("scheduler tick #%s pending=%s", turn["run"], turn["pending"])

    def tick(self) -> dict:
        """One look at the queue. Cheap, synchronous, never blocks."""
        self.last_run = time.time()
        self.runs += 1
        pending = self.bus.pending(include_low=True, limit=50)
        turn = {
            "at": self.last_run,
            "run": self.runs,
            "pending": len(pending),
            "ids": [p["id"] for p in pending],
            "reason": "pending" if pending else "idle",
        }
        self.turns.append(turn)
        self.turns = self.turns[-200:]
        if pending:
            log.info("wake: %d pending event(s)", len(pending))
        return turn

    # ---------- introspection ----------
    def status(self) -> dict:
        now = time.time()
        return {
            "settings": self.settings,
            "running": bool(self._task and not self._task.done()),
            "runs": self.runs,
            "last_run": self.last_run,
            "seconds_until_next": max(0, round(self.next_run - now, 1)) if self.settings.get("enabled") else None,
            "in_quiet_hours": self.in_quiet_hours(),
            "recent_turns": self.turns[-10:],
            "bus": self.bus.status(),
        }
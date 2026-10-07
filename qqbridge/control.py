"""运行时控制：/stop /start /auto 三种模式。

- auto  : 默认。事件入队，插件按巡检间隔唤醒模型。
- stopped: 强制静默。事件照常落库，但不入队、不唤醒，直到管理员重新开启。
- manual: 手动模式。同样不自动唤醒，但保留队列供人工拉取。

只有 ADMIN_IDS 里的账号能切换；每次变更记审计。
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path

MODES = ("auto", "stopped", "manual")


class Control:
    def __init__(self, path: Path, bus, store):
        self.path = path
        self.bus = bus
        self.store = store
        self._lock = threading.RLock()
        self.mode = "auto"
        self.by = ""
        self.at = 0.0
        self.reason = ""
        self.load()

    def load(self):
        if self.path.is_file():
            try:
                d = json.loads(self.path.read_text(encoding="utf-8"))
                if d.get("mode") in MODES:
                    self.mode = d["mode"]
                    self.by = d.get("by", "")
                    self.at = d.get("at", 0.0)
                    self.reason = d.get("reason", "")
            except (OSError, ValueError):
                pass
        self._apply()
        return self.mode

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps({
            "mode": self.mode, "by": self.by, "at": self.at, "reason": self.reason
        }, ensure_ascii=False, indent=1), encoding="utf-8")

    def _apply(self):
        """暂停时清空待处理队列，避免恢复后一次性爆发。"""
        if self.mode == "stopped":
            with self.bus._lock:
                self.bus._pending_high.clear()
                self.bus._pending_mid.clear()
                self.bus._pending_low.clear()

    def paused(self) -> bool:
        return self.mode == "stopped"

    def set(self, mode: str, actor: str = "", reason: str = ""):
        if mode not in MODES:
            raise ValueError("模式必须是 auto / stopped / manual 之一。")
        with self._lock:
            self.mode = mode
            self.by = str(actor)
            self.at = time.time()
            self.reason = reason[:200]
            self._apply()
            self.save()
            self.store.audit(str(actor), "control:" + mode, "runtime",
                             {"reason": self.reason}, "SUCCEEDED", f"mode={mode}")
            return self.status()

    def status(self) -> dict:
        return {"mode": self.mode, "by": self.by, "at": self.at,
                "reason": self.reason, "paused": self.paused()}

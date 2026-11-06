"""Runtime control: the /stop /start /auto modes.

- auto  : default. Events are queued and the plugin wakes the model on its poll interval.
- stopped: force silence. Events are still persisted, but never queued or woken,
  until an admin turns it back on.
- manual: manual mode. Also does not wake automatically, but keeps the queue for
  a human to pull from.

Only the accounts in ADMIN_IDS may switch; every change is audited.
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path

MODES = ("auto", "stopped", "manual")
# Behaviour is fully automatic now (admins take the agent path, everyone else
# chat); this field is kept only for compatibility
AGENT_MODES = ("chat", "agent")


class Control:
    def __init__(self, path: Path, bus, store):
        self.path = path
        self.bus = bus
        self.store = store
        self._lock = threading.RLock()
        self.mode = "auto"
        self.agent_mode = "chat"
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
                if d.get("agent_mode") in AGENT_MODES:
                    self.agent_mode = d["agent_mode"]
                    self.by = d.get("by", "")
                    self.at = d.get("at", 0.0)
                    self.reason = d.get("reason", "")
            except (OSError, ValueError):
                # A missing or corrupt control file just keeps the defaults.
                pass
        self._apply()
        return self.mode

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps({
            "mode": self.mode, "agent_mode": self.agent_mode, "by": self.by, "at": self.at, "reason": self.reason
        }, ensure_ascii=False, indent=1), encoding="utf-8")

    def _apply(self):
        """Clear the pending queue while paused, so resuming does not burst.

        Under the watermark model, "clear the queue" just means pushing the
        watermark up to the newest entry -- everything after that is treated as
        already seen, so resuming will not flush it all out at once.
        Note this is mark_seen_at (with its clamping), not writing _seen_at by
        hand: the clamping logic may only exist in one place.
        """
        if self.mode == "stopped":
            with self.bus._lock:
                newest = max((e["at"] for e in self.bus._events), default=0.0)
            if newest:
                self.bus.mark_seen_at(newest)

    def paused(self) -> bool:
        return self.mode == "stopped"

    def set(self, mode: str, actor: str = "", reason: str = ""):
        if mode not in MODES:
            raise ValueError("模式必须是 auto / stopped / manual 之一。")
        with self._lock:
            self.mode = mode
            self.by = str(actor)
            self.at = time.time()
            # Cap the free-text reason so one caller cannot bloat the control file.
            self.reason = reason[:200]
            self._apply()
            self.save()
            self.store.audit(str(actor), "control:" + mode, "runtime",
                             {"reason": self.reason}, "SUCCEEDED", f"mode={mode}")
            return self.status()

    def set_agent_mode(self, agent_mode: str, actor: str = "", reason: str = ""):
        """Switch the chat / agent behaviour mode."""
        if agent_mode not in AGENT_MODES:
            raise ValueError("模式必须是 chat 或 agent。")
        with self._lock:
            self.agent_mode = agent_mode
            self.by = str(actor)
            self.at = time.time()
            # set() and set_agent_mode() share the same by/at/reason fields, so the
            # audit record says who flipped the switch and why.
            self.reason = (reason or "")[:200]
            self.save()
            self.store.audit(str(actor), "switch:" + agent_mode, "runtime",
                             {"reason": self.reason}, "SUCCEEDED", f"agent_mode={agent_mode}")
            return self.status()

    def is_agent(self) -> bool:
        return self.agent_mode == "agent"

    def status(self) -> dict:
        return {"mode": self.mode, "agent_mode": self.agent_mode, "by": self.by, "at": self.at,
                "reason": self.reason, "paused": self.paused()}
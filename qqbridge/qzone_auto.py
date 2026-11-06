"""Automatic Qzone posts: on a timer, have the model write one entry and publish it to the
bot's own QQ Qzone.

The themes are preset and can be added / edited / removed from the console; each post
picks one at random (or by rotation), hands it to the model, then sends the result through
OneBot's send_qzone_msg.
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
import time
from pathlib import Path

log = logging.getLogger("qqbridge.qzone_auto")

DEFAULT_THEMES = [
    {"id": "daily", "name": "日常随想",
     "hint": "生活里的小事、天气、心情、吃了什么。口语，像随手发的。"},
    {"id": "code", "name": "写代码",
     "hint": "调试、部署、踩坑、重构。可以吐槽但别太长。"},
    {"id": "mc", "name": "Minecraft / 服务器",
     "hint": "开服、插件、红石、玩家趣事、维护公告口吻。"},
    {"id": "late", "name": "深夜", "hours": [["22:30", "05:30"]],
     "hint": "半夜没睡、胡思乱想、安静的自言自语。短，带点情绪。"},
    {"id": "meme", "name": "玩梗吐槽",
     "hint": "网络热梗、自嘲、对某件事的随口吐槽。"},
    {"id": "weather", "name": "天气季节", "hours": [["06:00", "21:00"]],
     "hint": "换季、下雨、降温、热到融化。注意要与当前时间相符。"},
    {"id": "game", "name": "游戏",
     "hint": "在玩的游戏、通关、翻车、联机。别说教。"},
    {"id": "ai", "name": "AI 自省",
     "hint": "作为一个 AI 待在人类群里的感受。有趣、不自怜、不煽情。"},
]

DEFAULTS = {
    "enabled": False,
    "mode": "interval",              # interval | daily
    "interval_hours": 12,            # hours between posts
    "daily_times": ["12:30", "21:00"],   # posting times in daily mode
    "min_gap_hours": 4,              # hard floor between two posts, in any mode
    "quiet_hours": [["23:30", "08:00"]],   # never post inside these windows
    "order": "random",               # random | rotate
    "active_themes": ["daily", "code", "late"],   # theme ids eligible for posting
    "themes": DEFAULT_THEMES,
    "max_chars": 80,                 # hard cap on one post, in characters
    "history": [],                   # recent posts; trimmed to the newest 100
    # The two fields below are runtime state, yet they have to be persisted as well:
    # load() only accepts keys that exist in DEFAULTS, and without them the service would
    # forget that it already posted -- so every restart would post again right away.
    "last_post_at": 0,
    "last_text": "",
}


class QzoneAuto:
    def __init__(self, path: Path, llm, bot, store=None, *, publish=None):
        self.path = path
        self.llm = llm
        self.bot = bot
        self.store = store
        self.publish = publish            # injectable send function (for tests)
        self.cfg = dict(DEFAULTS)
        self._rotate = 0                  # cursor for order="rotate"
        self._task = None                 # the background loop task, if started
        self._stop = asyncio.Event()      # set by stop(); the loop also waits on it
        self.load()

    # ---------- Configuration ----------
    def load(self):
        if self.path.is_file():
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
                # Iterate over DEFAULTS rather than over the file: unknown or long-removed
                # keys in an old config file therefore never leak into self.cfg.
                for k in DEFAULTS:
                    if k in data:
                        self.cfg[k] = data[k]
            except (OSError, ValueError):
                log.warning("qzone.json 读不出来，用默认配置")
        self._merge_presets()
        return self.cfg

    def _merge_presets(self):
        """Restore preset fields that are missing from an old config file.

        Example: the hours window was added to the presets later on. Only absent keys are
        filled in -- anything the user edited stays exactly as it is.
        """
        presets = {t["id"]: t for t in DEFAULT_THEMES}
        for theme in self.cfg.get("themes") or []:
            base = presets.get(theme.get("id"))
            if not base:
                continue
            for k, v in base.items():
                theme.setdefault(k, v)

    def save(self, patch: dict | None = None):
        # A patch is filtered against DEFAULTS, so a malformed API call cannot inject
        # arbitrary keys into the persisted config.
        if patch:
            for k, v in patch.items():
                if k in DEFAULTS:
                    self.cfg[k] = v
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.cfg, ensure_ascii=False, indent=1), encoding="utf-8")
        return self.cfg

    def status(self) -> dict:
        h = self.cfg.get("history") or []
        return {
            "enabled": self.cfg.get("enabled"),
            "mode": self.cfg.get("mode"),
            "interval_hours": self.cfg.get("interval_hours"),
            "daily_times": self.cfg.get("daily_times"),
            "min_gap_hours": self.cfg.get("min_gap_hours"),
            "quiet_hours": self.cfg.get("quiet_hours"),
            "order": self.cfg.get("order"),
            "active_themes": self.cfg.get("active_themes"),
            "themes": self.cfg.get("themes"),
            "last_post_at": self.cfg.get("last_post_at", 0),
            "last_text": self.cfg.get("last_text", ""),
            "next_in_seconds": self._next_in(),
            "posted": len(h),
            "history": h[-10:],
        }

    def brief(self) -> dict:
        """Compact status for /api/state (history bodies left out to save bandwidth)."""
        return {
            "enabled": bool(self.cfg.get("enabled")),
            "mode": self.cfg.get("mode"),
            "interval_hours": self.cfg.get("interval_hours"),
            "daily_times": self.cfg.get("daily_times"),
            "min_gap_hours": self.cfg.get("min_gap_hours"),
            "quiet_hours": self.cfg.get("quiet_hours"),
            "order": self.cfg.get("order"),
            "active_themes": self.cfg.get("active_themes"),
            "theme_count": len(self.cfg.get("themes") or []),
            "posted": len(self.cfg.get("history") or []),
            "last_post_at": self.cfg.get("last_post_at", 0),
            "next_in_seconds": self._next_in(),
        }

    # ---------- Time ----------
    @staticmethod
    def _minutes(hhmm: str):
        # "HH:MM" -> minute of day. Bad values return None instead of raising, so one typo
        # in the config file disables a single slot rather than the whole scheduler.
        try:
            h, m = str(hhmm).split(":")
            return int(h) * 60 + int(m)
        except (ValueError, AttributeError):
            return None

    @classmethod
    def _in_windows(cls, spans, cur: int) -> bool:
        """Whether cur (minute of day) falls inside any of the given windows.

        Windows may wrap past midnight, e.g. ["22:30", "05:30"].
        """
        for span in spans or []:
            if not isinstance(span, (list, tuple)) or len(span) != 2:
                continue
            a, b = cls._minutes(span[0]), cls._minutes(span[1])
            if a is None or b is None:
                continue
            if a <= b:
                if a <= cur < b:
                    return True
            elif cur >= a or cur < b:
                # Wrapping window (a > b): it covers [a, midnight) plus [midnight, b).
                return True
        return False

    def in_quiet_hours(self, now=None) -> bool:
        # Local time on purpose: the schedule follows the clock of the machine it runs on.
        lt = time.localtime(now or time.time())
        return self._in_windows(self.cfg.get("quiet_hours"), lt.tm_hour * 60 + lt.tm_min)

    def _next_in(self):
        last = float(self.cfg.get("last_post_at") or 0)
        if self.cfg.get("mode") == "daily":
            # Daily mode: seconds until the next configured clock time, rolling over to
            # tomorrow when today's slots are all gone.
            times = sorted(t for t in (self._minutes(x) for x in (self.cfg.get("daily_times") or []))
                           if t is not None)
            if not times:
                return None
            lt = time.localtime()
            cur = lt.tm_hour * 60 + lt.tm_min
            # No later slot today means the next one is tomorrow's first slot.
            nxt = next((t for t in times if t > cur), times[0])
            delta = (nxt - cur) * 60
            # delta <= 0 means the chosen slot already passed today, so roll forward a full
            # day (delta == 0 is "right now", which becomes tomorrow's same slot).
            return delta if delta > 0 else delta + 86400
        gap = float(self.cfg.get("interval_hours") or 12) * 3600
        # Clamp at 0: an overdue post reports 0 rather than a negative wait.
        return max(0, int(last + gap - time.time()))

    def due(self, now=None) -> bool:
        now = now or time.time()
        if not self.cfg.get("enabled"):
            return False
        # Quiet hours beat everything else, including an overdue interval.
        if self.in_quiet_hours(now):
            return False
        last = float(self.cfg.get("last_post_at") or 0)
        # min_gap_hours is a hard floor and applies in daily mode too, so a config with
        # many close slots still cannot flood the timeline.
        min_gap = float(self.cfg.get("min_gap_hours") or 0) * 3600
        if last and now - last < min_gap:
            return False
        if self.cfg.get("mode") == "daily":
            times = sorted(t for t in (self._minutes(x) for x in (self.cfg.get("daily_times") or []))
                           if t is not None)
            if not times:
                return False
            lt = time.localtime(now)
            cur = lt.tm_hour * 60 + lt.tm_min
            for t in times:
                # This slot has already passed today and nothing was posted at it yet.
                # (cur - t) * 60 converts the minute gap to seconds, and the extra 60s of
                # slack stops a post made exactly on the slot from counting as "not yet".
                if cur >= t and last < now - (cur - t) * 60 - 60:
                    return True
            return False
        gap = float(self.cfg.get("interval_hours") or 12) * 3600
        # last == 0 means "never posted", and now - last is then huge, so the first check
        # after startup is due immediately.
        return (now - last) >= gap

    # ---------- Generation ----------
    def pick_theme(self, now=None):
        themes = self.cfg.get("themes") or DEFAULT_THEMES
        # An empty active list means "all themes"; and if none of the ids match anything
        # (e.g. a theme was deleted) the filter yields nothing, so the full set is used.
        active = self.cfg.get("active_themes") or [t["id"] for t in themes]
        pool = [t for t in themes if t.get("id") in active] or themes
        # A theme may carry an hours window (e.g. "late night" only fits 22:30-05:30).
        # Themes without hours are always eligible; themed ones are dropped while the
        # current time is outside their window. If everything gets filtered out, fall back
        # to the full pool so that some hour of the day cannot end up with nothing to post.
        lt = time.localtime(now or time.time())
        cur = lt.tm_hour * 60 + lt.tm_min
        timed = [t for t in pool
                 if not t.get("hours") or self._in_windows(t.get("hours"), cur)]
        if timed:
            pool = timed
        if self.cfg.get("order") != "rotate":
            return random.choice(pool)
        # Advance before reading, so consecutive calls land on different themes.
        self._rotate = (self._rotate + 1) % len(pool)
        return pool[self._rotate]

    async def generate(self, theme: dict) -> str:
        limit = int(self.cfg.get("max_chars") or 80)
        now = time.strftime("%Y-%m-%d %H:%M")
        messages = [
            {"role": "system", "content":
                "你在帮一个 QQ 机器人写它自己空间里的说说。\n"
                "要求：像真人随手发的，口语、简短、有生活感。\n"
                f"严格不超过 {limit} 个字。不要话题标签，不要表情符号堆砌，"
                "不要总结陈词，不要写成广告或公告（除非主题要求）。\n"
                "只输出说说正文本身，不要引号、不要解释。"},
            {"role": "user", "content": f"当前时间：{now}\n主题：{theme.get('name')}\n方向：{theme.get('hint')}"},
        ]
        out = await self.llm.chat(messages)
        # Some models wrap the post in quotes even when told not to.
        text = (out.get("text") or "").strip().strip('"').strip()
        if len(text) > limit:
            # The prompt asks for the limit, but models overshoot often enough that this
            # cut is the norm, not a rare fallback.
            text = text[:limit]
        return text

    async def post_once(self, *, force_theme: str = "") -> dict:
        themes = self.cfg.get("themes") or DEFAULT_THEMES
        # An unknown / empty force_theme falls back to the normal picker.
        theme = next((t for t in themes if t.get("id") == force_theme), None) or self.pick_theme()
        text = await self.generate(theme)
        if not text:
            return {"ok": False, "error": "模型没给出内容"}
        if self.publish is not None:
            await self.publish(text)
        else:
            # Same extension call the MCP tool uses; no permission check here because the
            # scheduler runs on behalf of the owner anyway.
            await self.bot.call("send_qzone_msg", content=text)
        now = time.time()
        hist = list(self.cfg.get("history") or [])
        hist.append({"at": now, "theme": theme.get("id"), "text": text})
        # Keep only the newest 100 entries: enough for the console, small enough to rewrite
        # the whole config file every time without it growing forever.
        self.cfg["history"] = hist[-100:]
        self.cfg["last_post_at"] = now
        self.cfg["last_text"] = text
        self.save()
        if self.store is not None:
            try:
                self.store.audit("qzone_auto", "publish", theme.get("id", ""),
                                 {"text": text}, "SUCCEEDED")
            except Exception:
                # Audit is best effort: a store failure must not lose a post that has
                # already gone out.
                pass
        log.info("说说已发（%s）：%s", theme.get("name"), text[:40])
        return {"ok": True, "theme": theme.get("name"), "text": text}

    # ---------- Loop ----------
    async def start(self):
        self._stop.clear()
        self._task = asyncio.create_task(self._loop())
        return self

    async def stop(self):
        self._stop.set()
        if self._task:
            self._task.cancel()
            try:
                # cancel() only marks the task; awaiting it is what actually waits for the
                # loop to unwind. Whatever it raises is swallowed on purpose -- shutdown
                # must not propagate a worker error to the caller.
                await self._task
            except (asyncio.CancelledError, Exception):
                pass

    async def _loop(self):
        log.info("自动说说循环启动（enabled=%s）", self.cfg.get("enabled"))
        while not self._stop.is_set():
            # Sleep in 60-second slices: config edits (enabled, interval, ...) are picked up
            # within a minute, and stop() can break out promptly instead of at the next post.
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=60)
                break
            except asyncio.TimeoutError:
                # A timeout is the normal tick; the only other way out of the wait is stop().
                pass
            if not self.cfg.get("enabled") or not self.llm.configured:
                continue
            try:
                if self.due():
                    await self.post_once()
            except Exception:
                # One failed post must never kill the loop.
                log.exception("自动说说失败")

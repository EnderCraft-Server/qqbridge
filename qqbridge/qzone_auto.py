"""自动发说说：定时用模型生成一条内容，发到机器人自己的 QQ 空间。

主题是预设的，可在控制台增删改；每条随机（或轮转）挑一个主题，
交给模型生成，再走 OneBot 的 send_qzone_msg 发出去。
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
    "interval_hours": 12,            # 每隔几小时发一条
    "daily_times": ["12:30", "21:00"],   # daily 模式下每天的发帖时刻
    "min_gap_hours": 4,
    "quiet_hours": [["23:30", "08:00"]],
    "order": "random",               # random | rotate
    "active_themes": ["daily", "code", "late"],
    "themes": DEFAULT_THEMES,
    "max_chars": 80,
    "history": [],
}


class QzoneAuto:
    def __init__(self, path: Path, llm, bot, store=None, *, publish=None):
        self.path = path
        self.llm = llm
        self.bot = bot
        self.store = store
        self.publish = publish            # 可注入的发送函数（便于测试）
        self.cfg = dict(DEFAULTS)
        self._rotate = 0
        self._task = None
        self._stop = asyncio.Event()
        self.load()

    # ---------- 配置 ----------
    def load(self):
        if self.path.is_file():
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
                for k in DEFAULTS:
                    if k in data:
                        self.cfg[k] = data[k]
            except (OSError, ValueError):
                log.warning("qzone.json 读不出来，用默认配置")
        self._merge_presets()
        return self.cfg

    def _merge_presets(self):
        """老配置文件里缺的预设字段（例如后加的 hours 时间窗）补回来。
        只补缺失的键，用户改过的内容不动。"""
        presets = {t["id"]: t for t in DEFAULT_THEMES}
        for theme in self.cfg.get("themes") or []:
            base = presets.get(theme.get("id"))
            if not base:
                continue
            for k, v in base.items():
                theme.setdefault(k, v)

    def save(self, patch: dict | None = None):
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
        """给 /api/state 用的精简状态（不带历史正文，省流量）。"""
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

    # ---------- 时间 ----------
    @staticmethod
    def _minutes(hhmm: str):
        try:
            h, m = str(hhmm).split(":")
            return int(h) * 60 + int(m)
        except (ValueError, AttributeError):
            return None

    @classmethod
    def _in_windows(cls, spans, cur: int) -> bool:
        """cur（当天第几分钟）是否落在任一时间窗内。支持跨零点。"""
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
                return True
        return False

    def in_quiet_hours(self, now=None) -> bool:
        lt = time.localtime(now or time.time())
        return self._in_windows(self.cfg.get("quiet_hours"), lt.tm_hour * 60 + lt.tm_min)

    def _next_in(self):
        last = float(self.cfg.get("last_post_at") or 0)
        if self.cfg.get("mode") == "daily":
            times = sorted(t for t in (self._minutes(x) for x in (self.cfg.get("daily_times") or []))
                           if t is not None)
            if not times:
                return None
            lt = time.localtime()
            cur = lt.tm_hour * 60 + lt.tm_min
            nxt = next((t for t in times if t > cur), times[0])
            delta = (nxt - cur) * 60
            return delta if delta > 0 else delta + 86400
        gap = float(self.cfg.get("interval_hours") or 12) * 3600
        return max(0, int(last + gap - time.time()))

    def due(self, now=None) -> bool:
        now = now or time.time()
        if not self.cfg.get("enabled"):
            return False
        if self.in_quiet_hours(now):
            return False
        last = float(self.cfg.get("last_post_at") or 0)
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
                # 该时刻已过、且今天还没发过
                if cur >= t and last < now - (cur - t) * 60 - 60:
                    return True
            return False
        gap = float(self.cfg.get("interval_hours") or 12) * 3600
        return (now - last) >= gap

    # ---------- 生成 ----------
    def pick_theme(self, now=None):
        themes = self.cfg.get("themes") or DEFAULT_THEMES
        active = self.cfg.get("active_themes") or [t["id"] for t in themes]
        pool = [t for t in themes if t.get("id") in active] or themes
        # 主题可以带 hours 时间窗（例如「深夜」只在 22:30~05:30 用）。
        # 没写 hours 的主题任何时候都能用；写了但当前不在窗内的会被剔除。
        # 全被剔除时退回全集，避免某个时段一条都发不出来。
        lt = time.localtime(now or time.time())
        cur = lt.tm_hour * 60 + lt.tm_min
        timed = [t for t in pool
                 if not t.get("hours") or self._in_windows(t.get("hours"), cur)]
        if timed:
            pool = timed
        if self.cfg.get("order") != "rotate":
            return random.choice(pool)
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
        text = (out.get("text") or "").strip().strip('"').strip()
        if len(text) > limit:
            text = text[:limit]
        return text

    async def post_once(self, *, force_theme: str = "") -> dict:
        themes = self.cfg.get("themes") or DEFAULT_THEMES
        theme = next((t for t in themes if t.get("id") == force_theme), None) or self.pick_theme()
        text = await self.generate(theme)
        if not text:
            return {"ok": False, "error": "模型没给出内容"}
        if self.publish is not None:
            await self.publish(text)
        else:
            await self.bot.call("send_qzone_msg", content=text)
        now = time.time()
        hist = list(self.cfg.get("history") or [])
        hist.append({"at": now, "theme": theme.get("id"), "text": text})
        self.cfg["history"] = hist[-100:]
        self.cfg["last_post_at"] = now
        self.cfg["last_text"] = text
        self.save()
        if self.store is not None:
            try:
                self.store.audit("qzone_auto", "publish", theme.get("id", ""),
                                 {"text": text}, "SUCCEEDED")
            except Exception:
                pass
        log.info("说说已发（%s）：%s", theme.get("name"), text[:40])
        return {"ok": True, "theme": theme.get("name"), "text": text}

    # ---------- 循环 ----------
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
        log.info("自动说说循环启动（enabled=%s）", self.cfg.get("enabled"))
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=60)
                break
            except asyncio.TimeoutError:
                pass
            if not self.cfg.get("enabled") or not self.llm.configured:
                continue
            try:
                if self.due():
                    await self.post_once()
            except Exception:
                log.exception("自动说说失败")

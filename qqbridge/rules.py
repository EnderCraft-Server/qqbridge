"""Trigger rules: decide which inbound messages deserve the model's attention.

Three tiers, highest first:
  mention  — someone @s the bot
  keyword  — text hits a configured keyword
  cooldown — any chatter, rate-limited by cooldown_seconds

Everything else is stored but never queued, so the model is not woken for noise.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

DEFAULT_KEYWORDS = [
    "机器人", "ai", "AI", "小鲸鱼", "大肥鱼", "deepseek", "DeepSeek",
    "帮我", "看下", "看看", "算一下", "解一下", "解释", "翻译",
]


class Rules:
    def __init__(self, path: Path | None = None, keywords: list[str] | None = None):
        self.path = path
        self.keywords = list(keywords if keywords is not None else DEFAULT_KEYWORDS)
        self.patterns: list[re.Pattern] = []
        self.reload()

    def reload(self):
        if self.path and self.path.is_file():
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(data, dict) and isinstance(data.get("keywords"), list):
                    self.keywords = [str(k) for k in data["keywords"]]
            except (OSError, ValueError):
                pass
        self.patterns = [re.compile(re.escape(k), re.IGNORECASE) for k in self.keywords if k]
        return self

    def save(self, keywords: list[str] | None = None):
        if keywords is not None:
            self.keywords = keywords
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(
                json.dumps({"keywords": self.keywords}, ensure_ascii=False, indent=1),
                encoding="utf-8")
        return self.reload()

    def match(self, text: str) -> str | None:
        for pat in self.patterns:
            if pat.search(text or ""):
                return pat.pattern
        return None

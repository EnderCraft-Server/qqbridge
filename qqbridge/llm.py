"""Built-in model client: an OpenAI-compatible /chat/completions caller.

The bot holds its own api_base / api_key / model, so it does not depend on any
external Agent host being around.
Only httpx is required -- no vendor SDK is pulled in.
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any

import httpx

from . import net

log = logging.getLogger("qqbridge.llm")


class LLMError(RuntimeError):
    pass


class LLM:
    def __init__(self, api_base: str, api_key: str, model: str,
                 timeout: float = 60.0, max_tokens: int = 2048, temperature: float = 1.0,
                 thinking: str = "disabled", reasoning_effort: str = ""):
        self.api_base = (api_base or "").rstrip("/")
        self.api_key = api_key or ""
        self.model = model or ""
        self.timeout = timeout
        self.max_tokens = max_tokens
        self.temperature = temperature
        # thinking=disabled turns the reasoning chain off (saves tokens and time);
        # set enabled to turn it back on
        self.thinking = (thinking or "").strip().lower()
        self.reasoning_effort = (reasoning_effort or "").strip().lower()
        self.last_error = ""
        self.calls = 0
        self.prompt_tokens = 0
        self.completion_tokens = 0

    @property
    def configured(self) -> bool:
        return bool(self.api_base and self.api_key and self.model)

    def describe(self) -> dict:
        return {
            "configured": self.configured,
            "api_base": self.api_base,
            "model": self.model,
            "has_key": bool(self.api_key),
            "thinking": self.thinking or "default",
            "reasoning_effort": self.reasoning_effort or "default",
            "timeout": self.timeout,
            "calls": self.calls,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "last_error": self.last_error,
        }

    def reconfigure(self, *, api_base=None, api_key=None, model=None,
                    max_tokens=None, temperature=None):
        if api_base is not None:
            self.api_base = api_base.rstrip("/")
        if api_key:
            self.api_key = api_key
        if model is not None:
            self.model = model
        if max_tokens is not None:
            self.max_tokens = int(max_tokens)
        if temperature is not None:
            self.temperature = float(temperature)
        return self.describe()

    async def chat(self, messages: list[dict], tools: list[dict] | None = None,
                   *, max_tokens: int | None = None,
                   temperature: float | None = None) -> dict:
        """One non-streaming chat call. Returns {text, tool_calls, usage}.

        max_tokens / temperature may override the instance defaults per call --
        academic mode needs the length cap lifted, but chit-chat should still stay
        cheap, so they can only be supplied at the call site, never changed
        globally.
        """
        if not self.configured:
            raise LLMError("模型未配置：需要在 .env 里填 LLM_API_BASE / LLM_API_KEY / LLM_MODEL。")
        body: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "max_tokens": self.max_tokens if max_tokens is None else int(max_tokens),
            "temperature": self.temperature if temperature is None else float(temperature),
            "stream": False,
        }
        # DeepSeek's thinking switch: thinking.type = enabled/disabled
        if self.thinking in ("enabled", "disabled"):
            body["thinking"] = {"type": self.thinking}
        if self.reasoning_effort in ("low", "high", "max") and self.thinking != "disabled":
            body["reasoning_effort"] = self.reasoning_effort
        if tools:
            body["tools"] = tools
        url = f"{self.api_base}/chat/completions"
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        t0 = time.time()
        try:
            # Do not use httpx defaults here: trust_env=True goes through the
            # system proxy, and once the proxy is dead the whole bot goes mute.
            # net.arequest falls back to a direct connection automatically.
            resp = await net.arequest("POST", url, json=body, headers=headers,
                                      timeout=self.timeout)
            resp.raise_for_status()
            data = resp.json()
        except httpx.HTTPStatusError as exc:
            # Trim the error body: a provider may answer with a long HTML page.
            detail = exc.response.text[:300] if exc.response is not None else ""
            self.last_error = f"HTTP {exc.response.status_code}: {detail}"
            raise LLMError(self.last_error) from None
        except Exception as exc:
            self.last_error = str(exc)[:200]
            raise LLMError(self.last_error) from None

        self.calls += 1
        # Accumulate usage so the console panel can show a running cost estimate.
        usage = data.get("usage") or {}
        self.prompt_tokens += int(usage.get("prompt_tokens") or 0)
        self.completion_tokens += int(usage.get("completion_tokens") or 0)
        choice = (data.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        text = (message.get("content") or "").strip()
        tool_calls = message.get("tool_calls") or []
        log.info("llm: %.1fs in=%s out=%s tools=%d",
                 time.time() - t0, usage.get("prompt_tokens"), usage.get("completion_tokens"),
                 len(tool_calls))
        self.last_error = ""
        return {"text": text, "tool_calls": tool_calls, "usage": usage, "raw": message}
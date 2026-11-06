"""Built-in Agent loop: lets the bot call tools for itself, with no external agent
host involved.

One run() = a "model -> tools -> feed back -> model" loop of at most N steps,
until the model stops calling tools or the step cap is reached. Every step is
audited; the tools' own reads and writes go to agent.log separately.
"""
from __future__ import annotations

import json
import logging

from .llm import LLM, LLMError

log = logging.getLogger("qqbridge.agentloop")

DEFAULT_MAX_STEPS = 30      # do not strangle a task with the step count; per-tool
                            # timeouts are the real backstop


class AgentLoop:
    def __init__(self, llm: LLM, tools, *, max_steps: int = DEFAULT_MAX_STEPS, store=None):
        self.llm = llm
        self.tools = tools
        self.max_steps = max_steps
        self.store = store
        self.runs = 0
        self.steps = 0
        self.tool_calls = 0
        self.errors = 0

    def status(self) -> dict:
        return {"runs": self.runs, "steps": self.steps, "tool_calls": self.tool_calls,
                "errors": self.errors, "max_steps": self.max_steps,
                "root": str(getattr(self.tools, "root", ""))}

    async def run(self, system: str, task: str, *, history: list[dict] | None = None,
                  max_tokens: int | None = None, tools=None) -> dict:
        """Run one full tool loop, returning {text, steps, tool_calls, log}.

        max_tokens applies to every step -- academic mode needs the cap lifted,
        ordinary dispatching keeps the default.

        tools may be a **copy dedicated to this turn** (see LocalTools.for_target).
        Defaults to self.tools. That is the key to concurrency safety: the target
        conversation must not live on a shared instance, otherwise two background
        tasks overwrite each other's target_group and send operations into the
        wrong group.
        """
        if not self.llm.configured:
            raise LLMError("模型未配置：无法运行内置 Agent。")
        tl = tools if tools is not None else self.tools
        messages: list[dict] = [{"role": "system", "content": system}]
        if history:
            messages.extend(history[-20:])
        messages.append({"role": "user", "content": task})

        trace: list[dict] = []
        self.runs += 1
        fail_seen: dict = {}       # stop burning steps on a call that keeps failing
                                   # the same way
        for step in range(1, self.max_steps + 1):
            self.steps += 1
            out = await self.llm.chat(messages, tools=tl.schemas(),
                                      max_tokens=max_tokens)
            calls = out.get("tool_calls") or []
            text = out.get("text") or ""

            if not calls:
                trace.append({"step": step, "kind": "final", "text": text[:300]})
                self._audit("agent_run", {"steps": step, "tool_calls": self.tool_calls,
                                          "text": text[:200]}, "SUCCEEDED")
                return {"text": text, "steps": step, "tool_calls": trace}

            # Echo the assistant's tool_calls back into the history, otherwise the
            # following tool messages have nothing to attach to.
            messages.append({"role": "assistant", "content": text or None, "tool_calls": calls})
            for call in calls:
                name = ((call.get("function") or {}).get("name")) or ""
                raw_args = (call.get("function") or {}).get("arguments") or "{}"
                try:
                    args = json.loads(raw_args) if isinstance(raw_args, str) else (raw_args or {})
                except ValueError:
                    # Malformed arguments: pass an empty dict so the tool can
                    # complain about the missing fields instead of losing the step.
                    args = {}
                try:
                    # Dispatch through the async path: blocking tools run in a
                    # thread pool, so the event loop is not stalled
                    result = await tl.dispatch_async(name, args)
                    ok = True
                except Exception as exc:
                    result = {"error": str(exc)[:300]}
                    ok = False
                    self.errors += 1
                self.tool_calls += 1
                if not ok:
                    fp = name + ":" + json.dumps(args, sort_keys=True, ensure_ascii=False)
                    fail_seen[fp] = fail_seen.get(fp, 0) + 1
                trace.append({"step": step, "kind": "tool", "name": name,
                              "args": {k: str(v)[:80] for k, v in (args or {}).items()},
                              "ok": ok})
                log.info("agent step %d: %s(%s) -> %s", step, name,
                         json.dumps(args, ensure_ascii=False)[:120], "ok" if ok else "err")
                messages.append({"role": "tool", "tool_call_id": call.get("id") or name,
                                 "content": json.dumps(result, ensure_ascii=False)[:20000]})

            # Same call with the same arguments failing 3 times in a row = an
            # infinite loop. That is exactly what used to happen: a command was
            # killed by the blacklist, the model rephrased it and tried again, and
            # all 30 steps were burned.
            if fail_seen and max(fail_seen.values()) >= 3:
                worst = max(fail_seen, key=lambda k: fail_seen[k]).split(":")[0]
                text = await self._wrap_up(
                    messages, f"工具 {worst} 用同样的参数连续失败了 3 次，判定卡住")
                self._audit("agent_run", {"steps": step, "reason": "repeat_failure",
                                          "tool": worst, "text": text[:200]}, "STOPPED")
                return {"text": text or "这条我卡住了，换个说法我再试。",
                        "steps": step, "tool_calls": trace}

        # Out of steps: do not drop "(step limit reached, no final answer)" filler
        # into the group; ask the model once more for a plain sentence (no tools),
        # so at least the user learns where it got stuck.
        text = await self._wrap_up(messages, f"已经用完 {self.max_steps} 步工具调用")
        self._audit("agent_run", {"steps": self.max_steps, "reason": "max_steps",
                                  "text": text[:200]}, "TRUNCATED")
        return {"text": text or "这轮没干完，工具调用次数到上限了，换个说法我再试。",
                "steps": self.max_steps, "tool_calls": trace}

    async def _wrap_up(self, messages: list[dict], why: str) -> str:
        """Wrap-up question: ask once more without tools, for one plain sentence that
        can go straight into the group.
        """
        try:
            out = await self.llm.chat(messages + [{"role": "user", "content":
                f"【系统】{why}，不要再调用任何工具。"
                "直接给用户一句人话：你已经知道什么、卡在哪里、需要他做什么，60 字以内。"}])
            return (out.get("text") or "").strip()
        except Exception as exc:
            log.warning("agent 收尾提问失败：%s", exc)
            return ""

    def _audit(self, action: str, detail: dict, state: str):
        if self.store is None:
            return
        try:
            self.store.audit("agent", action, "loop", detail, state)
        except Exception:
            pass
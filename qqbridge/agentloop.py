"""内置 Agent 循环：让 bot 自己调工具干活，不依赖任何外部 Agent 宿主。

一次 run() = 最多 N 步的「模型 → 工具 → 回填 → 模型」循环，直到模型不再调工具
或达到步数上限。每一步都落审计，工具自身的读写另有 agent.log。
"""
from __future__ import annotations

import json
import logging

from .llm import LLM, LLMError

log = logging.getLogger("qqbridge.agentloop")

DEFAULT_MAX_STEPS = 30      # 别用步数卡死任务；靠工具自身的超时兜底


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

    async def run(self, system: str, task: str, *, history: list[dict] | None = None) -> dict:
        """跑一次完整的工具循环，返回 {text, steps, tool_calls, log}。"""
        if not self.llm.configured:
            raise LLMError("模型未配置：无法运行内置 Agent。")
        messages: list[dict] = [{"role": "system", "content": system}]
        if history:
            messages.extend(history[-20:])
        messages.append({"role": "user", "content": task})

        trace: list[dict] = []
        self.runs += 1
        for step in range(1, self.max_steps + 1):
            self.steps += 1
            out = await self.llm.chat(messages, tools=self.tools.schemas())
            calls = out.get("tool_calls") or []
            text = out.get("text") or ""

            if not calls:
                trace.append({"step": step, "kind": "final", "text": text[:300]})
                self._audit("agent_run", {"steps": step, "tool_calls": self.tool_calls,
                                          "text": text[:200]}, "SUCCEEDED")
                return {"text": text, "steps": step, "tool_calls": trace}

            # 回填 assistant 的 tool_calls
            messages.append({"role": "assistant", "content": text or None, "tool_calls": calls})
            for call in calls:
                name = ((call.get("function") or {}).get("name")) or ""
                raw_args = (call.get("function") or {}).get("arguments") or "{}"
                try:
                    args = json.loads(raw_args) if isinstance(raw_args, str) else (raw_args or {})
                except ValueError:
                    args = {}
                try:
                    # 走 async 分发：阻塞工具在线程池里跑，不卡事件循环
                    result = await self.tools.dispatch_async(name, args)
                    ok = True
                except Exception as exc:
                    result = {"error": str(exc)[:300]}
                    ok = False
                    self.errors += 1
                self.tool_calls += 1
                trace.append({"step": step, "kind": "tool", "name": name,
                              "args": {k: str(v)[:80] for k, v in (args or {}).items()},
                              "ok": ok})
                log.info("agent step %d: %s(%s) -> %s", step, name,
                         json.dumps(args, ensure_ascii=False)[:120], "ok" if ok else "err")
                messages.append({"role": "tool", "tool_call_id": call.get("id") or name,
                                 "content": json.dumps(result, ensure_ascii=False)[:20000]})

        # 步数用尽
        self._audit("agent_run", {"steps": self.max_steps, "reason": "max_steps"}, "TRUNCATED")
        return {"text": "（步骤用尽，未得到最终答复）", "steps": self.max_steps, "tool_calls": trace}

    def _audit(self, action: str, detail: dict, state: str):
        if self.store is None:
            return
        try:
            self.store.audit("agent", action, "loop", detail, state)
        except Exception:
            pass
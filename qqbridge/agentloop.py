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
        fail_seen: dict = {}       # 同一个调用反复失败就别再耗步数了
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

            # 同一个调用带着同样的参数连续失败 3 次 = 死循环。
            # 之前就是这样：命令被黑名单误杀，模型换个写法接着试，把 30 步全烧光。
            if fail_seen and max(fail_seen.values()) >= 3:
                worst = max(fail_seen, key=lambda k: fail_seen[k]).split(":")[0]
                text = await self._wrap_up(
                    messages, f"工具 {worst} 用同样的参数连续失败了 3 次，判定卡住")
                self._audit("agent_run", {"steps": step, "reason": "repeat_failure",
                                          "tool": worst, "text": text[:200]}, "STOPPED")
                return {"text": text or "这条我卡住了，换个说法我再试。",
                        "steps": step, "tool_calls": trace}

        # 步数用尽：不要往群里丢「（步骤用尽，未得到最终答复）」这种废话，
        # 再问一次模型要一句人话（不带工具），至少告诉用户卡在哪。
        text = await self._wrap_up(messages, f"已经用完 {self.max_steps} 步工具调用")
        self._audit("agent_run", {"steps": self.max_steps, "reason": "max_steps",
                                  "text": text[:200]}, "TRUNCATED")
        return {"text": text or "这轮没干完，工具调用次数到上限了，换个说法我再试。",
                "steps": self.max_steps, "tool_calls": trace}

    async def _wrap_up(self, messages: list[dict], why: str) -> str:
        """收尾提问：不带工具再问一次，要一句能直接发进群的人话。"""
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
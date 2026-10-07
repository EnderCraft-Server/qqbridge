"""自动接话循环：完全在 qqbridge 进程内跑，不依赖外部 Agent。

流程：
  事件总线（已有） → 按优先级取待处理 → 交给 QqAgent 决定要不要接
  → 要接就 send_message → 记入历史 → 推进游标

与 MCP 工具的关系：两条路并存。auto_reply 打开时 bot 自己说话；
外部 Agent 仍可通过 MCP 工具操作同一个队列（谁先 mark_processed 谁生效）。
"""
from __future__ import annotations

import asyncio
import logging
import time

from .agent import clean_reply
from .config import config

log = logging.getLogger("qqbridge.autoreply")


class AutoReply:
    def __init__(self, bus, store, agent, bot, control, *, enabled: bool = True,
                 idle_seconds: float = 1.5, max_per_minute: int = 8, agent_loop=None,
                 agent_timeout: float = 90.0):
        self.bus = bus
        self.store = store
        self.agent = agent
        self.agent_loop = agent_loop
        self.agent_timeout = agent_timeout
        self.bot = bot
        self.control = control
        self.enabled = enabled
        self.idle_seconds = idle_seconds
        self.max_per_minute = max_per_minute
        self._task = None
        self._stop = asyncio.Event()
        self._sent: list[float] = []
        self._fail_count: dict[int, int] = {}      # ev.id -> 连续失败次数
        self.max_retry = 3
        self.last_action = ""
        self.last_at = 0.0
        self.errors = 0

    # ---------- 限流 ----------
    def _rate_ok(self) -> bool:
        now = time.time()
        self._sent = [t for t in self._sent if now - t < 60]
        return len(self._sent) < self.max_per_minute

    # ---------- 生命周期 ----------
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

    def status(self) -> dict:
        return {
            "enabled": self.enabled,
            "agent_mode": self.control.agent_mode if self.control else "chat",
            "running": bool(self._task and not self._task.done()),
            "sent_last_minute": len([t for t in self._sent if time.time() - t < 60]),
            "last_action": self.last_action,
            "last_at": self.last_at,
            "errors": self.errors,
            "agent": self.agent.status() if self.agent else {},
        }

    # ---------- 主循环 ----------
    async def _loop(self):
        log.info("autoreply loop started (enabled=%s)", self.enabled)
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.idle_seconds)
                break
            except asyncio.TimeoutError:
                pass
            if not self.enabled or not self.control_allows():
                continue
            try:
                await self._tick()
            except Exception as exc:
                self.errors += 1
                log.exception("autoreply tick failed")
                # 卡住保护：同一条反复失败就跳过它，别把整条队列堵死
                try:
                    pending = self.bus.pending(include_low=True, limit=1)
                    if pending:
                        eid = pending[0]["id"]
                        self._fail_count[eid] = self._fail_count.get(eid, 0) + 1
                        if self._fail_count[eid] >= self.max_retry:
                            log.warning("事件 %s 连续失败 %d 次，跳过并推进游标：%s",
                                        eid, self._fail_count[eid], str(exc)[:120])
                            self.store.audit("auto", "auto_skip", str(eid),
                                             {"error": str(exc)[:200],
                                              "tries": self._fail_count[eid]}, "SKIPPED")
                            self.bus.mark_processed(eid)
                            self._fail_count.pop(eid, None)
                except Exception:
                    log.exception("skip-guard failed")

    def control_allows(self) -> bool:
        try:
            return not self.control.paused()
        except Exception:
            return True

    async def _run_agent(self, ev: dict) -> dict:
        """agent 模式：把消息交给内置 Agent（带文件/命令工具）。"""
        key = ev.get("group_id") or ev.get("user_id") or "?"
        who = ev.get("sender") or ev.get("user_id")
        mentioned = bool(ev.get("mentions_me"))
        task = (
            f"群 {ev.get('group_id') or '私聊'} · {who}：{ev.get('text') or ''}\n\n"
            + ("【这条直接 @ 了你，必须回一句话，不能空】\n" if mentioned else "")
            + "有活就干（用工具办），干完回一句；没活就直接回一句。\n"
            "无论如何都要给出那句要发到群里的话 —— 留空只在完全无话可说时才允许。"
        )
        system = (self.agent.system_prompt or "") + (
            "\n\n【覆盖上面的 JSON 格式要求】不要输出 JSON，直接说人话。\n"
            "工具（一次选对，别反复试）：\n"
            "  fetch_url(url)         查网页/API —— 要上网就用这个，不要拼 curl\n"
            "  read_file(path)        读文件\n"
            "  write_file(path,content) 写文件\n"
            "  list_dir(path)         列目录\n"
            "  search_files(pattern)  按文件名找\n"
            "  run_command(cmd)       跑本地命令（仅本地文件/程序，别用来上网）\n"
            "调研类任务最多查 2~3 次就给结论，别死磕。回话要短。"
        )
        try:
            # 整体超时：agent 再能干也不能把整轮拖死
            out = await asyncio.wait_for(self.agent_loop.run(system, task),
                                         timeout=self.agent_timeout)
            reply, how = clean_reply(out.get("text") or "")
            if how == "failed":
                log.warning("agent 模式：输出解析失败，改判沉默")
        except asyncio.TimeoutError:
            self.errors += 1
            log.warning("agent 模式超时（%.0fs），本轮放弃", self.agent_timeout)
            reply = ""
        except Exception as exc:
            self.errors += 1
            log.warning("agent mode failed: %s", exc)
            reply = ""
        # agent 没给出话（超时/报错/沉默）时，回退到闲聊模式兜底 ——
        # 尤其被 @ 时不能因为工具链出问题就彻底不吭声
        if not reply:
            try:
                fallback = await self.agent.decide(ev)
                reply = fallback.get("reply") or ""
                if reply:
                    log.info("agent 模式无输出，已回退 chat 兜底")
            except Exception:
                log.exception("agent fallback failed")
        self.agent.decisions += 1
        if reply:
            self.agent.replies += 1
        else:
            self.agent.silences += 1
        return {"reply": reply[:800], "reason": "agent 模式", "key": key}

    async def _tick(self):
        if not self.agent or not self.agent.llm.configured:
            return
        pending = self.bus.pending(include_low=True, limit=10)
        if not pending:
            return
        if not self._rate_ok():
            log.info("autoreply: 每分钟上限，暂停接话")
            return

        # 一次处理一条，避免刷屏
        ev = pending[0]
        self.agent.observe(ev)

        in_agent_mode = bool(self.control) and self.control.is_agent()
        is_admin = str(ev.get("user_id") or "") in config.admins

        if in_agent_mode:
            # agent 模式：只认管理员，走工具循环
            if not is_admin:
                self.bus.mark_processed(ev["id"])
                self.last_action = "agent 模式忽略非管理员消息"
                self.last_at = time.time()
                return
            decided = await self._run_agent(ev)
        else:
            decided = await self.agent.decide(ev)

        key = decided["key"]
        reply = decided["reply"]

        # 无论接不接，这条都算处理过
        through = ev["id"]
        # 最后一道防线：发送前再净化一次。无论哪个模式、哪条路径产出的 reply，
        # 只要它看起来是 JSON，就在这里被拆掉或改判沉默。
        if reply:
            reply, how = clean_reply(reply)
            if not reply:
                log.warning("发送前净化：输出被判定为不可发送（%s），改判沉默", how)
        if reply:
            gid = ev.get("group_id") or ev.get("user_id") or ""
            try:
                if ev.get("message_type") == "private" or not ev.get("group_id"):
                    await self.bot.call("send_private_msg", user_id=int(ev["user_id"]), message=reply)
                else:
                    await self.bot.call("send_group_msg", group_id=int(gid), message=reply)
                self._sent.append(time.time())
                self.bus.note_own_send()
                self.agent.note_own(key, reply)
                self.store.audit("auto", "auto_reply", gid, {"reply": reply, "reason": decided["reason"]},
                                 "SUCCEEDED")
                self.last_action = f"回复 {gid}: {reply[:40]}"
                log.info("autoreply: %s -> %s", gid, reply[:60])
            except Exception as exc:
                self.errors += 1
                self.last_action = f"发送失败: {exc}"
                log.warning("autoreply send failed: %s", exc)
        else:
            self.last_action = f"沉默（{decided['reason'][:40]}）"
            self.store.audit("auto", "auto_silent", ev.get("group_id", ""), {"reason": decided["reason"]},
                             "SUCCEEDED")
        self.last_at = time.time()
        self._fail_count.pop(through, None)
        self.bus.mark_processed(through)
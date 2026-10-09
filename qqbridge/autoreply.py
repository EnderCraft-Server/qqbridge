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

from . import academic
from .agent import clean_reply, render_text
from .config import config

log = logging.getLogger("qqbridge.autoreply")

# 单条 QQ 消息的上限（留足余量）。超长内容按段落切开连发 —— 学术模式的答案
# 动辄几千字，硬塞一条会被服务端拒掉或者直接截断。
SEND_CHUNK = 1800


def split_message(text: str, limit: int = SEND_CHUNK) -> list[str]:
    """把长文本按段落边界切成若干条，尽量不切在句子中间。"""
    text = (text or "").strip()
    if len(text) <= limit:
        return [text] if text else []
    chunks: list[str] = []
    buf = ""
    for para in text.split("\n"):
        candidate = para if not buf else buf + "\n" + para
        if len(candidate) <= limit:
            buf = candidate
            continue
        if buf:
            chunks.append(buf)
        # 单段本身超长：硬切
        while len(para) > limit:
            chunks.append(para[:limit])
            para = para[limit:]
        buf = para
    if buf:
        chunks.append(buf)
    return [c for c in chunks if c.strip()]


class AutoReply:
    def __init__(self, bus, store, agent, bot, control, *, enabled: bool = True,
                 idle_seconds: float = 1.5, max_per_minute: int = 8, agent_loop=None,
                 agent_timeout: float = 90.0, settle_seconds: float = 3.0,
                 max_batch_age: float = 45.0, batch_limit: int = 200,
                 context_size: int = 40, context_max_age: float = 300.0,
                 max_backlog_age: float = 120.0, max_concurrent_tasks: int = 2):
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
        # 攒一段再看的节奏：最后一条之后静默多久出手；群里一直热闹时最多等多久
        self.settle_seconds = settle_seconds
        self.max_batch_age = max_batch_age
        self.batch_limit = batch_limit
        self.context_size = context_size
        # 上下文除了「最近多少条」，还限「最近多少秒」—— 只看条数时，群里一慢下来
        # 40 条能横跨好几小时，模型就会拿着几小时前的语境去接现在的话。
        self.context_max_age = context_max_age
        # 积压太久就丢掉，只接最近的：回一条三分钟前的消息比不回更糟。
        self.max_backlog_age = max_backlog_age
        # 派活的 agent loop 可能跑满 agent_timeout，绝不能堵着接话循环。
        self.max_concurrent_tasks = max_concurrent_tasks
        self._bg_tasks: set = set()
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
        # 后台的派活也要收干净，别让它关停之后还往群里发消息
        for t in list(self._bg_tasks):
            t.cancel()
        if self._bg_tasks:
            await asyncio.gather(*self._bg_tasks, return_exceptions=True)
        self._bg_tasks.clear()

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

    async def _run_task(self, task: str, gid: str = "") -> str:
        """把模型认出来的活交给内置 Agent 去干，返回要发到群里的那句话。

        gid 是这一轮的会话：agent 的 send_image 只能发到这里。
        目标由我们设，模型没有选择权 —— 不给它"发到哪个群"这个参数。
        """
        if not self.agent_loop:
            return ""
        try:
            self.agent_loop.tools.target_group = str(gid or "")
        except AttributeError:
            pass
        tools_block = (
            "\n\n【覆盖上面的 JSON 格式要求】不要输出 JSON，直接说人话。\n"
            "工具（一次选对，别反复试）：\n"
            "  fetch_url(url)            查网页 / API —— 要上网就用这个\n"
            "  read_file(path)           读文件\n"
            "  write_file(path,content)  写文件\n"
            "  list_dir(path)            列目录\n"
            "  search_files(pattern)     按文件名找\n"
            "  run_command(cmd)          跑本地命令\n"
            "  send_image(image,caption) 把一张图发到这个群（本地路径或 http/https 链接）\n"
            "                            —— 要发图就用它，别拿 run_command 去拼 CQ 码\n"
        )
        # 派活里也可能是学术题（管理员 @ 一道数学题）。命中就换学术人设并放开长度。
        academic_on, academic_why = False, ""
        if config.academic_mode:
            academic_on, academic_why = academic.detect(task)
        if academic_on:
            system = academic.build_academic_system(self.agent.system_prompt,
                                                    json_mode=False) + tools_block + (
                "这是学术/技术问题：**认真作答，不要用群友口吻敷衍**，"
                "该展开就展开，先结论后理由。查得到就查，查不到就说不知道，别编。"
            )
            max_tokens = config.llm_academic_max_tokens
            log.info("学术模式触发（派活，%s）", academic_why)
        else:
            system = (self.agent.system_prompt or "") + tools_block + "干完回一句短的，说清结果。别汇报过程。"
            max_tokens = None
        try:
            out = await asyncio.wait_for(self.agent_loop.run(system, task, max_tokens=max_tokens),
                                         timeout=self.agent_timeout)
            reply, how = clean_reply(out.get("text") or "")
            if how == "failed":
                log.warning("干活收尾的输出解析失败，改判沉默")
                return ""
            return reply
        except asyncio.TimeoutError:
            self.errors += 1
            log.warning("干活超时（%.0fs），本轮放弃", self.agent_timeout)
            return ""
        except Exception as exc:
            self.errors += 1
            log.warning("干活失败：%s", exc)
            return ""

    async def _send(self, target: dict, gid: str, reply: str, reason: str):
        try:
            private = target.get("message_type") == "private" or not target.get("group_id")
            # 学术模式的答案可能几千字，QQ 单条消息塞不下 —— 按段落切成几条发。
            chunks = split_message(reply) if len(reply) > SEND_CHUNK else [reply]
            for part in chunks:
                if private:
                    await self.bot.call("send_private_msg", user_id=int(target["user_id"]),
                                        message=part)
                else:
                    await self.bot.call("send_group_msg", group_id=int(gid), message=part)
                if len(chunks) > 1:
                    await asyncio.sleep(0.4)   # 连发别把服务端刷爆
            self._sent.append(time.time())
            self.bus.note_own_send()
            self.agent.note_own(gid, reply)
            self.store.audit("auto", "auto_reply", gid,
                             {"reply": reply, "reason": reason}, "SUCCEEDED")
            self.last_action = f"回复 {gid}: {reply[:40]}"
            log.info("autoreply: %s -> %s", gid, reply[:60])
        except Exception as exc:
            self.errors += 1
            self.last_action = f"发送失败: {exc}"
            log.warning("autoreply send failed: %s", exc)
    FORCED_FALLBACK = "在，你说。"

    async def _forced_reply(self, ev: dict) -> str:
        """被 @ 时的硬兜底：模型没给话就再问一次，只要一句人话。
        再不行用固定短句 —— 被点名还不吭声，用户只会觉得机器人死了。"""
        who = ev.get("sender") or ev.get("user_id") or "有人"
        key = ev.get("group_id") or ev.get("user_id") or "?"
        try:
            hist = self.agent.history(key).messages()[-10:]
            out = await asyncio.wait_for(self.agent.llm.chat([
                {"role": "system", "content": self.agent.system_prompt},
                *hist,
                {"role": "user", "content":
                    f"【系统】{who} 刚刚 @ 了你，内容是：「{ev.get('text') or ''}」。\n"
                    "直接说你要发到群里的那一句话。不要 JSON、不要解释、不要引号，一句就够。"},
            ]), timeout=self.agent_timeout)
            text = clean_reply(out.get("text") or "")[0] or (out.get("text") or "")
            text = text.strip().strip('"').strip()
            if text:
                log.info("@ 兜底回复：%s", text[:50])
                return text[:200]
        except Exception as exc:
            log.warning("@ 兜底回复失败：%s", exc)
        log.info("@ 兜底也没问出话，用固定短句")
        return self.FORCED_FALLBACK

    async def _tick(self):
        """批量接话：隔一会儿扫一眼整段，而不是一条一条地接。

        以前是按条处理 —— 取 pending[0]，处理完把游标推到它的 id。
        那一套带来两个问题：一是「跳着处理会把中间的消息一起划掉」这类游标 bug，
        二是天然把机器人变成复读机：每条消息都被单独判断一次要不要回，
        于是就开始逐条调 engagement，越调越拧巴。

        人不是这个节奏。现在是：攒一段 → 整段交给模型 → 回一句或者沉默 →
        处理完整批才推进标记。标记只会落在「模型真的看过的那条」上，
        结构上就不可能跳读。
        """
        if not self.agent or not self.agent.llm.configured:
            return

        fresh = self.bus.unseen(limit=self.batch_limit)
        if not fresh:
            return

        # 积压太久就丢掉旧的，只接最近的。
        # 上游一旦卡住（agent 跑满超时、模型变慢），积压会一直涨；等轮到某条时
        # 它已经是几分钟前的了 —— 回一条三分钟前的消息，比不回更像坏了。
        # 这里只对「整批里最老的那几条」动手，且保留至少一条，避免全丢。
        max_age = getattr(self, "max_backlog_age", 120.0)
        if max_age > 0 and len(fresh) > 1:
            cut = time.time() - max_age
            kept = [e for e in fresh if (e.get("at") or 0) >= cut]
            if kept and len(kept) < len(fresh):
                log.info("积压 %d 条已过期（>%.0fs），丢弃，只接最近 %d 条",
                         len(fresh) - len(kept), max_age, len(kept))
                self.store.audit("auto", "auto_drop_stale", "",
                                 {"dropped": len(fresh) - len(kept),
                                  "max_age": max_age}, "SKIPPED")
                fresh = kept

        # 派活的那条路**不进批处理**。Agent 自己就是多步带工具的，
        # 让它在「这段闲聊要不要接」的批量决策里捎带决定，是两套逻辑搅在一起，
        # 也容易把该干的活降级成一句敷衍。判据很直接：管理员在跟它说话
        # （@ 了它，或者私聊）＝ 派活，直接叫 Agent 过来。
        for ev in fresh:
            if self._wants_agent(ev):
                # 先把整批记进历史（纯内存，很快），再接活 —— 这样接话循环
                # 立刻就能看到最新语境，不用等 agent 干完。
                for e in fresh:
                    self.agent.observe(e)
                # 认领游标 + 扔后台：agent loop 最长要跑 agent_timeout(90s)，
                # 以前是 await 它，于是整个接话循环停摆 90 秒，闲聊全排队，
                # 等轮到的时候已经滞后好几分钟（实测 162 秒）。
                self.bus.mark_looked(fresh[-1]["id"])
                self._spawn_task(ev)
                return

        now = time.time()
        newest_at = fresh[-1].get("at") or 0.0
        oldest_at = fresh[0].get("at") or 0.0
        has_mention = any(e.get("mentions_me") for e in fresh)

        # 攒一段再出手：最后一条之后先静默 settle 秒，免得有人话说到一半就插嘴。
        # 被点名的不等 —— 叫你就该应。
        # 群里一直热闹、永远静不下来的情况靠 max_batch_age 兜底，否则永远轮不到。
        if not has_mention:
            if now - newest_at < self.settle_seconds and now - oldest_at < self.max_batch_age:
                return

        if not self._rate_ok() and not has_mention:
            log.info("autoreply: 每分钟上限，先攒着（积压 %d 条）", len(fresh))
            return

        through = fresh[-1]["id"]
        try:
            decided = await self._compose(fresh)
        except Exception as exc:
            self.errors += 1
            log.exception("autoreply tick failed")
            # 同一批连续失败就跳过它，别把后面的消息全堵死
            self._fail_count[through] = self._fail_count.get(through, 0) + 1
            if self._fail_count[through] >= self.max_retry:
                log.warning("这一批连续失败 %d 次，跳过并推进标记（%s）",
                            self._fail_count[through], str(exc)[:100])
                self.store.audit("auto", "auto_skip", str(through),
                                 {"error": str(exc)[:200],
                                  "tries": self._fail_count[through]}, "SKIPPED")
                self.bus.mark_looked(through)
                self._fail_count.pop(through, None)
            return
        self._fail_count.pop(through, None)
        self.last_at = time.time()

        target = fresh[-1]
        gid = target.get("group_id") or target.get("user_id") or ""
        reply = decided.get("reply") or ""

        # 发送前最后一道净化：不管哪条路径产出的，看着像 JSON 就在这儿拦下
        if reply:
            reply, how = clean_reply(reply)
            if not reply:
                log.warning("发送前净化：输出被判定为不可发送（%s），改判沉默", how)

        # 被 @ 就必须有回音：模型沉默、解析失败、超时、干活链路报错，统统兜底。
        # 被点名还一声不吭，用户只会以为机器人死了。
        if not reply and has_mention:
            reply = await self._forced_reply(target)
            decided["reason"] = ((decided.get("reason") or "") + " ← @ 兜底").strip()

        why = decided.get("reason") or ""
        if decided.get("academic"):
            why = f"[学术:{decided.get('academic_why') or ''}] " + why
        if reply:
            await self._send(target, gid, reply, why)
        else:
            self.last_action = f"沉默（{why[:40]}）"
            self.store.audit("auto", "auto_silent", gid,
                             {"reason": why,
                              "academic": bool(decided.get("academic")),
                              "batch": len(fresh)}, "SUCCEEDED")
        # 整批处理完才推进，而且推进到「模型真的看过的那条」
        self.bus.mark_looked(through)

    @staticmethod
    def _wants_agent(ev: dict) -> bool:
        """这条是不是在支使内置 Agent（带工具那种）。

        只有管理员开口才算数，而且必须是**在跟它说话**：@ 了它，或者私聊。
        群里管理员之间闲聊不算 —— 否则又回到「管理员说啥都当命令」的老毛病，
        那就是「冒泡」也会被回的根因。
        """
        if str(ev.get("user_id") or "") not in config.admins:
            return False
        return bool(ev.get("mentions_me")) or ev.get("message_type") == "private"

    def _spawn_task(self, ev: dict):
        """把派活扔进后台，绝不阻塞接话循环。

        以前是 `await self._handle_task(...)`，agent loop 最长跑 agent_timeout(90s)，
        这 90 秒里整个 `_loop` 停摆：闲聊不处理、游标不推进，等它干完接着一条条
        补，实测滞后到 162 秒。游标已由调用方认领，这里只负责异步执行。
        """
        running = [t for t in self._bg_tasks if not t.done()]
        self._bg_tasks = set(running)
        if len(running) >= self.max_concurrent_tasks:
            log.info("派活并发已满（%d），跳过这条：%s", len(running),
                     (ev.get("text") or "")[:40])
            self.store.audit("auto", "auto_task_skip", str(ev.get("id")),
                             {"running": len(running)}, "SKIPPED")
            return
        task = asyncio.create_task(self._handle_task(ev))
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)

    async def _handle_task(self, ev: dict):
        """直接调用内置 Agent 干活，不走批量决策。

        历史已在调用方 observe 过，游标也已认领 —— 这里只管干和回。
        """
        self.last_at = time.time()
        gid = ev.get("group_id") or ev.get("user_id") or ""
        who = ev.get("sender") or ev.get("user_id") or "?"
        convo = "\n".join(
            ("我" if x.get("is_self") else (x.get("sender") or "?")) + "：" + render_text(x)
            for x in self.bus.context(self.context_size,
                                       max_age=self.context_max_age)[-12:])
        task = (
            f"群 {gid} · {who} 对你说：{render_text(ev)}\n\n"
            f"【最近的对话，供你理解在聊什么】\n{convo}\n\n"
            "有活就干（用工具办），干完回一句；没活就直接回一句。"
        )
        try:
            reply = await self._run_task(task, gid)
        except Exception:
            self.errors += 1
            log.exception("处理派活时出错")
            reply = ""
        if not reply and ev.get("mentions_me"):
            reply = await self._forced_reply(ev)
        if reply:
            await self._send(ev, gid, reply, "agent 直派")
        else:
            self.last_action = "沉默（派活但没话可说）"
            self.store.audit("auto", "auto_silent", gid,
                             {"reason": "agent 直派，无输出"}, "SUCCEEDED")

    async def _compose(self, fresh: list) -> dict:
        """闲聊的批量决策。派活不走这里，见 _wants_agent / _handle_task。"""
        for ev in fresh:
            self.agent.observe(ev)
        batch = self.bus.context(self.context_size, max_age=self.context_max_age)
        return await self.agent.decide_batch(batch, fresh_from=fresh[0]["id"] - 1)
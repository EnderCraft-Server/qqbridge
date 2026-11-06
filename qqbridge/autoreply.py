"""Auto-reply loop: runs entirely inside the qqbridge process, no external Agent needed.

Flow:
  event bus (already here) → take pending items by priority → hand them to QqAgent to decide
  → if it wants to reply, send_message → record in history → advance the cursor

Relationship with the MCP tools: both paths coexist. When auto_reply is on the bot speaks
for itself; an external Agent can still work the same queue through the MCP tools (whoever
calls mark_processed first wins).
"""
from __future__ import annotations

import asyncio
import logging
import random
import time
from collections import deque

from . import academic
from .agent import clean_reply, escape_cq, render_text
from .textnorm import tidy_outgoing
from .config import config

log = logging.getLogger("qqbridge.autoreply")

# Upper bound for one QQ message (with plenty of headroom). Oversized content is split at
# paragraph boundaries and sent as several messages — academic-mode answers run to thousands
# of characters, and forcing one into a single message gets rejected or truncated by the server.
SEND_CHUNK = 1800


def split_message(text: str, limit: int = SEND_CHUNK) -> list[str]:
    """Split long text at paragraph boundaries into several messages, avoiding cuts
    in the middle of a sentence."""
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
        # A single paragraph is itself over the limit: hard cut.
        while len(para) > limit:
            chunks.append(para[:limit])
            para = para[limit:]
        buf = para
    if buf:
        chunks.append(buf)
    # Drop whitespace-only chunks that paragraph splitting may leave behind.
    return [c for c in chunks if c.strip()]


class AutoReply:
    def __init__(self, bus, store, agent, bot, control, *, enabled: bool = True,
                 idle_seconds: float = 1.5, max_per_minute: int = 8, agent_loop=None,
                 agent_timeout: float = 90.0, settle_seconds: float = 3.0,
                 max_batch_age: float = 45.0, batch_limit: int = 200,
                 context_size: int = 40, context_max_age: float = 300.0,
                 max_backlog_age: float = 120.0, max_concurrent_tasks: int = 2,
                 multi_send_gap: float = 1.2):
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
        # Pacing for "accumulate a bit, then look": how long to stay quiet after the last
        # message before acting, and the longest we wait while the group keeps chattering.
        self.settle_seconds = settle_seconds
        self.max_batch_age = max_batch_age
        self.batch_limit = batch_limit
        self.context_size = context_size
        # The context is bounded not only by "how many recent messages" but also by "how many
        # recent seconds" — with a count-only limit, 40 messages can span hours once the group
        # quiets down, and the model answers today's message with hours-old context.
        self.context_max_age = context_max_age
        # When sending in parts, pause between them. Firing them all at once feels robotic.
        self.multi_send_gap = multi_send_gap
        # An overly old backlog is dropped and only the recent part answered: replying to a
        # message from three minutes ago is worse than not replying.
        self.max_backlog_age = max_backlog_age
        # The dispatched agent loop can run for agent_timeout; never block the reply loop on it.
        self.max_concurrent_tasks = max_concurrent_tasks
        self._bg_tasks: set = set()
        # Wait queue for when concurrency is full. **Bounded**: an unbounded queue just hands
        # over the memory.
        self._task_queue: deque = deque()
        self.max_pending_tasks = 8   # hard cap on the wait queue
        self._task = None
        self._stop = asyncio.Event()
        self._sent: list[float] = []
        self._fail_count: dict[int, int] = {}      # ev.id -> consecutive failure count
        self.max_retry = 3   # consecutive failures before a batch is skipped
        self.last_action = ""
        self.last_at = 0.0
        self.errors = 0

    # ---------- rate limiting ----------
    def _rate_ok(self) -> bool:
        now = time.time()
        # Sliding 60-second window: forget timestamps older than a minute, then check the count.
        self._sent = [t for t in self._sent if now - t < 60]
        return len(self._sent) < self.max_per_minute

    # ---------- lifecycle ----------
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
        # Cancel background dispatched work too, so nothing posts to the group after shutdown.
        self._task_queue.clear()
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

    # ---------- main loop ----------
    async def _loop(self):
        log.info("autoreply loop started (enabled=%s)", self.enabled)
        while not self._stop.is_set():
            try:
                # Wait for idle_seconds, or wake up right away when stop() is signalled.
                await asyncio.wait_for(self._stop.wait(), timeout=self.idle_seconds)
                break
            except asyncio.TimeoutError:
                pass
            # Disabled, or paused through /stop: idle without consuming anything.
            if not self.enabled or not self.control_allows():
                continue
            try:
                await self._tick()
            except Exception as exc:
                self.errors += 1
                log.exception("autoreply tick failed")
                # Stuck guard: skip a message that keeps failing rather than blocking the queue.
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
            # A broken control object must not wedge the loop: fail open.
            return True

    async def _run_task(self, task: str, gid: str = "") -> str:
        """Hand the work the model identified to the built-in Agent and return the line to post.

        gid is this round's session: the agent's send_image can only post there.
        We set the target; the model gets no say — it is never given a "which group" parameter.
        """
        if not self.agent_loop:
            return ""
        # Each run gets its own copy of the tools — the target session must never live on a
        # shared instance. It used to be self.agent_loop.tools.target_group = gid, and with two
        # background tasks running at once the later one overwrote the earlier one, so group A's
        # mute/send_image landed in group B (reproduced in testing).
        try:
            run_tools = self.agent_loop.tools.for_target(gid)
        except AttributeError:
            run_tools = None
        tools_block = (
            "\n\n【覆盖上面的 JSON 格式要求】不要输出 JSON，直接说人话。\n"
            "工具（一次选对，别反复试）：\n"
            "  fetch_url(url)            查网页 / API —— 要上网就用这个\n"
            "  read_file(path)           读文件\n"
            "  write_file(path,content)  写文件\n"
            "  list_dir(path)            列目录\n"
            "  search_files(pattern)     按文件名找\n"
            "  run_command(cmd)          跑本地命令\n"
            "  manage_group(action,user_id,duration_seconds,value)  群管理：\n"
            "                            mute 禁言 / unmute 解除 / kick 踢出 / rename 改群名\n"
            "                            / card 改名片 / whole_ban 全员禁言\n"
            "                            —— 禁言必须给秒数；解除用 unmute（mute+0 就是解除，别搞反）\n"
            "  send_image(image,caption,expect)  把一张图发到这个群（本地路径或 http/https 链接）\n"
            "                            —— 要发图就用它，别拿 run_command 去拼 CQ 码\n"
            "                            expect 必填：这张图应该是什么。系统会先看图核验，\n"
            "                            不安全或和图对不上都会被打回，别硬发。\n"
        )
        # The dispatched work may also be an academic question (an admin @-ing a math problem).
        # On a hit, switch to the academic persona and lift the length cap.
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
            out = await asyncio.wait_for(self.agent_loop.run(system, task, max_tokens=max_tokens,
                                                             tools=run_tools),
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

    async def _send(self, target: dict, gid: str, replies, reason: str):
        """Post to the group. replies may be a string, or a list of "send in several parts".

        Parts must really be spaced out: popping out one at a time looks like a person typing,
        while firing three at once looks more like a program dumping data.
        """
        raw = [replies] if isinstance(replies, str) else list(replies or [])
        parts = [tidy_outgoing(p) for p in raw]
        parts = [p for p in parts if p]
        # A single part can still exceed the server limit (long academic answers); keep splitting.
        chunks: list[str] = []
        for p in parts:
            chunks.extend(split_message(p) if len(p) > SEND_CHUNK else [p])
        if not chunks:
            return
        reply = "\n".join(chunks)
        try:
            # A private message is typed "private" or carries no group id; send it through the
            # private API.
            private = target.get("message_type") == "private" or not target.get("group_id")
            for i, part in enumerate(chunks):
                if i:
                    # Pause before a follow-up part: fixed gap plus a little jitter, so the
                    # rhythm does not feel too even.
                    await asyncio.sleep(self.multi_send_gap + random.uniform(0.0, 0.8))
                # **CQ codes must be escaped before anything goes out.** The body is
                # model-generated and the model is influenced by group messages: a member only
                # has to coax it into emitting "[CQ:at,qq=all]" to ping everyone, or slip in a
                # [CQ:image,file=...] to bypass image review. After escaping they are literal.
                safe_part = escape_cq(part)
                if private:
                    await self.bot.call("send_private_msg", user_id=int(target["user_id"]),
                                        message=safe_part)
                else:
                    await self.bot.call("send_group_msg", group_id=int(gid), message=safe_part)
                self._sent.append(time.time())      # every message counts toward the rate limit
            # Let the bus know we just spoke, for its own-send cooldown.
            self.bus.note_own_send()
            self.agent.note_own(gid, reply)
            self.store.audit("auto", "auto_reply", gid,
                             {"replies": chunks, "reason": reason,
                              "parts": len(chunks)}, "SUCCEEDED")
            self.last_action = f"回复 {gid}: {reply[:40]}"
            log.info("autoreply: %s -> %s", gid, reply[:60])
        except Exception as exc:
            self.errors += 1
            self.last_action = f"发送失败: {exc}"
            log.warning("autoreply send failed: %s", exc)
    # Fixed line of last resort when we are @-ed and even the extra ask produced nothing.
    FORCED_FALLBACK = "在，你说。"
    # Receipt for a completely overflowing dispatch queue. Better one "I am swamped" than silence.
    TASK_OVERFLOW_REPLY = "这边活排满了，过会儿再说。"

    async def _forced_reply(self, ev: dict) -> str:
        """Hard fallback when @-ed: if the model gave nothing, ask once more for one plain line.
        If that still fails, use a fixed sentence — staying silent when named only convinces
        users the bot is dead."""
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
        """Bucket by session: one segment per group, each handled on its own.

        It used to merge **every group's** unread into a single batch: the context was the
        global last N messages, so group A's memes leaked into group B's judgement, and the
        reply still went to the group of the last message. Now each group has its own session
        and its own watermark.
        """
        if not self.agent or not self.agent.llm.configured:
            return
        buckets = self.bus.unseen_by_key(limit=self.batch_limit)
        for key, fresh in buckets.items():
            if not fresh:
                continue
            try:
                await self._tick_session(key, fresh)
            except Exception:
                self.errors += 1
                log.exception("会话 %s 处理失败，跳过（不影响其它群）", key)

    async def _tick_session(self, key: str, fresh: list):
        """One segment of unread messages for a single session.

        Batched replying: look at the whole segment every so often instead of answering one
        message at a time.

        It used to handle messages one by one — take pending[0], then push the cursor to its
        id. Two problems came with that: cursor bugs like "processing out of order sweeps away
        the messages in between", and it naturally turned the bot into a repeater: every
        message was judged separately on whether to answer it, so engagement had to be tuned
        per message and got more and more twisted.

        People are not on that tempo. Now: accumulate a segment → hand the whole segment to the
        model → answer once or stay silent → advance the mark only after the whole batch is
        handled. The mark only ever lands on "a message the model actually saw", so skipping
        ahead is structurally impossible.
        """
        # Drop the old part of the backlog and answer only the recent part.
        # Once upstream stalls (agent hits its timeout, model slows down) the backlog keeps
        # growing; by the time a message gets its turn it is already minutes old — answering a
        # three-minute-old message looks more broken than not answering at all.
        # Only the oldest entries of the batch are trimmed, and at least one is always kept,
        # so nothing disappears entirely.
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

        # The dispatch path **does not go through batch processing**. The Agent is itself a
        # multi-step tool user; letting it decide "should I join this chit-chat" inside the
        # batch decision mixes two logics and tends to degrade real work into one perfunctory
        # line. The test is direct: an admin talking to it (@-ing it, or a private message)
        # means dispatch, so call the Agent in right away.
        for ev in fresh:
            if self._wants_agent(ev):
                # Record the whole batch in history first (pure memory, fast), then take the
                # work — the reply loop then sees the latest context immediately instead of
                # waiting for the agent to finish.
                for e in fresh:
                    self.agent.observe(e)
                # Fire into the background: the agent loop can run for agent_timeout (90s) and
                # this used to be awaited, which stalled the whole reply loop for 90 seconds;
                # all the chit-chat queued up and was minutes behind by its turn (measured 162
                # seconds). When concurrency is full, _spawn_task queues it rather than dropping.
                scheduled = self._spawn_task(ev)
                self.bus.mark_looked(fresh[-1]["id"], key)
                if not scheduled:
                    # Even the wait queue is full (extreme backlog). The watermark has already
                    # moved, so there will be no retry — we must answer explicitly, or the user
                    # waits for a reply that never comes.
                    gid = ev.get("group_id") or ev.get("user_id") or ""
                    await self._send(ev, gid, self.TASK_OVERFLOW_REPLY, "派活队列溢出")
                return

        now = time.time()
        newest_at = fresh[-1].get("at") or 0.0
        oldest_at = fresh[0].get("at") or 0.0
        has_mention = any(e.get("mentions_me") for e in fresh)

        # Accumulate before acting: stay quiet for settle seconds after the last message so we
        # do not cut into a sentence half-spoken. A mention does not wait — when called, answer.
        # max_batch_age is the backstop for a group that never goes quiet, otherwise it would
        # never be our turn.
        if not has_mention:
            if now - newest_at < self.settle_seconds and now - oldest_at < self.max_batch_age:
                return

        if not self._rate_ok() and not has_mention:
            log.info("autoreply: 每分钟上限，先攒着（积压 %d 条）", len(fresh))
            return

        # If this decision succeeds, everything up to this id counts as handled.
        through = fresh[-1]["id"]
        try:
            decided = await self._compose(fresh, key)
        except Exception as exc:
            self.errors += 1
            log.exception("autoreply tick failed")
            # Skip a batch that keeps failing, so everything behind it does not get stuck.
            self._fail_count[through] = self._fail_count.get(through, 0) + 1
            if self._fail_count[through] >= self.max_retry:
                log.warning("这一批连续失败 %d 次，跳过并推进标记（%s）",
                            self._fail_count[through], str(exc)[:100])
                self.store.audit("auto", "auto_skip", str(through),
                                 {"error": str(exc)[:200],
                                  "tries": self._fail_count[through]}, "SKIPPED")
                self.bus.mark_looked(through, key)
                self._fail_count.pop(through, None)
            return
        self._fail_count.pop(through, None)
        self.last_at = time.time()

        # Reply to the newest message's conversation; the whole batch shares the same key.
        target = fresh[-1]
        gid = target.get("group_id") or target.get("user_id") or ""
        # The model can return several parts at once; _send sends them out over a few messages.
        replies = list(decided.get("replies") or [])
        if not replies and decided.get("reply"):
            replies = [decided["reply"]]

        # Last purge before sending: whatever path produced it, anything that looks like JSON
        # is stopped right here.
        cleaned: list[str] = []
        for part in replies:
            text, how = clean_reply(part)
            if text:
                cleaned.append(text)
            else:
                log.warning("发送前净化：输出被判定为不可发送（%s），丢弃这一条", how)
        replies = cleaned

        # A mention must get an answer: model silence, parse failure, timeout, errors in the
        # work path — all of them fall back. Staying mute when named convinces users the bot died.
        if not replies and has_mention:
            replies = [await self._forced_reply(target)]
            decided["reason"] = ((decided.get("reason") or "") + " ← @ 兜底").strip()

        why = decided.get("reason") or ""
        if decided.get("academic"):
            why = f"[学术:{decided.get('academic_why') or ''}] " + why
        if replies:
            await self._send(target, gid, replies, why)
        else:
            self.last_action = f"沉默（{why[:40]}）"
            self.store.audit("auto", "auto_silent", gid,
                             {"reason": why,
                              "academic": bool(decided.get("academic")),
                              "batch": len(fresh)}, "SUCCEEDED")
        # Advance only after the whole batch is handled, and only this session's own watermark.
        self.bus.mark_looked(through, key)

    @staticmethod
    def _wants_agent(ev: dict) -> bool:
        """Whether this message is directing the built-in Agent (the tool-using one).

        Only an admin counts, and it must be **talking to it**: @-ing it, or a private
        message. Chatter between admins in a group does not count — otherwise we are back to
        the old "whatever an admin says is a command" bug, which is the root cause of
        "bubbling" also getting a reply.
        """
        if str(ev.get("user_id") or "") not in config.admins:
            return False
        return bool(ev.get("mentions_me")) or ev.get("message_type") == "private"

    def _spawn_task(self, ev: dict) -> bool:
        """Throw the dispatched work into the background; never block the reply loop.

        It used to be `await self._handle_task(...)`, and the agent loop runs for up to
        agent_timeout (90s). For those 90 seconds the whole `_loop` stalled: chit-chat went
        unprocessed and the watermark did not move, then it caught up one by one afterwards —
        measured lag reached 162 seconds.

        **Queue when concurrency is full, do not drop.** The old code did `return` and nothing
        when concurrency was full, while the caller had already advanced the watermark — so an
        admin @-ed the bot, no task was created, and the message still counted as handled:
        **never a reply, never a retry, never a trace**.
        Now it goes into the wait queue and takes a slot the moment one frees up.
        Only when even that queue is full (extreme backlog) does it return False, and the
        caller tells the user explicitly.
        """
        self._drain_task_queue()
        if len(self._bg_tasks) >= self.max_concurrent_tasks:
            if len(self._task_queue) >= self.max_pending_tasks:
                log.warning("派活并发与等待队列都满了（%d/%d），这条只能放弃：%s",
                            len(self._bg_tasks), len(self._task_queue),
                            (ev.get("text") or "")[:40])
                self.store.audit("auto", "auto_task_overflow", str(ev.get("id")),
                                 {"running": len(self._bg_tasks),
                                  "queued": len(self._task_queue)}, "SKIPPED")
                return False
            self._task_queue.append(ev)
            log.info("派活并发已满，进等待队列（%d）：%s",
                     len(self._task_queue), (ev.get("text") or "")[:40])
            return True
        self._start_task(ev)
        return True

    def _start_task(self, ev: dict):
        # Keep a strong reference: asyncio only holds a weak one, so an unrooted task can be GC-ed.
        task = asyncio.create_task(self._handle_task(ev))
        self._bg_tasks.add(task)
        task.add_done_callback(self._on_task_done)

    def _on_task_done(self, task):
        self._bg_tasks.discard(task)
        # A slot freed up: start the next one immediately, do not leave the queue waiting.
        self._drain_task_queue()

    def _drain_task_queue(self):
        # Fill free slots from the queue in FIFO order: run on every dispatch attempt and
        # whenever a task finishes.
        while self._task_queue and len(self._bg_tasks) < self.max_concurrent_tasks:
            self._start_task(self._task_queue.popleft())

    async def _handle_task(self, ev: dict):
        """Call the built-in Agent to do the work directly, skipping the batch decision.

        The caller already observed the history and claimed the cursor — here it only does
        the work and replies.
        """
        self.last_at = time.time()
        gid = ev.get("group_id") or ev.get("user_id") or ""
        who = ev.get("sender") or ev.get("user_id") or "?"
        # Compact transcript of this session's last 12 messages, for the agent's context.
        convo = "\n".join(
            ("我" if x.get("is_self") else (x.get("sender") or "?")) + "：" + render_text(x)
            for x in self.bus.context(self.context_size, max_age=self.context_max_age,
                                       key=gid)[-12:])
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

    async def _compose(self, fresh: list, key: str) -> dict:
        """Batch decision for chit-chat. Dispatch does not come through here; see _wants_agent
        and _handle_task.

        The context takes **only this session's** messages — group A's conversation must never
        appear in group B's judgement.
        """
        for ev in fresh:
            self.agent.observe(ev)
        batch = self.bus.context(self.context_size, max_age=self.context_max_age, key=key)
        return await self.agent.decide_batch(batch, fresh_from=fresh[0]["id"] - 1)
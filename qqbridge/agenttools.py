"""Local toolset for the built-in agent: read/write files, list dirs, run commands, search.

**Every read and write is printed to the log and recorded in the audit table** — this is a
hard requirement: `data/agent.log` records, one per line, the time / action / path / result /
byte count.

Safety boundaries:
- File operations are confined to the workspace root (configurable); anything outside is
  rejected outright
- Commands go through a blacklist (formatting, shutdown, disk wiping, and the like)
- Every call has a size/count cap, so a single call cannot blow up memory
"""
from __future__ import annotations

import asyncio
import copy
import json
import logging
import os
import re
import subprocess
import time
from pathlib import Path
from urllib.parse import urlparse

from . import net
from .agent import escape_cq, is_safe_cq_param
from .config import config

log = logging.getLogger("qqbridge.agenttools")

MAX_READ_BYTES = 256 * 1024
MAX_WRITE_BYTES = 1024 * 1024
MAX_OUTPUT_BYTES = 64 * 1024
MAX_FETCH_BYTES = 512 * 1024
DEFAULT_CMD_TIMEOUT = 15          # seconds; kill it on timeout, do not stall the whole turn

ALLOWED_SCHEMES = ("http", "https")

# Dangerous-command blacklist.
#
# Lesson learned the hard way: the first entry used to be r"\bformat\b", meant to block
# "format a disk", but PowerShell's Format-List / Format-Table / Format-Wide all matched it —
# so a pure read like "systeminfo | ... | Format-List" got rejected, the model retried over
# and over until it ran out of steps, and finally posted "out of steps" into the group.
# Now only spellings that really destroy data are matched, even if that makes the list longer.
BANNED = [
    # Format: needs a drive letter or a /q /s /f switch. Format-Volume is a separate cmdlet,
    # so it gets its own pattern
    r"(?<![\w.-])format(?:\.com)?\s+[a-z]:",
    r"(?<![\w.-])format(?:\.com)?\s+/[qsf]",
    r"\bFormat-Volume\b",
    r"\bmkfs(?:\.\w+)?\b",
    # Shutdown / reboot
    r"(?<![\w.-])shutdown(?:\.exe)?\s+/[a-z]",
    r"\bStop-Computer\b", r"\bRestart-Computer\b",
    r"(?<![\w.-])reboot(?:\.exe)?\b",
    # Disks and partitions
    r"\bdiskpart\b", r"\bClear-Disk\b", r"\bInitialize-Disk\b",
    r"\bRemove-Partition\b",
    # Deleting a root directory
    r"rm\s+-rf\s+/",
    r"del\s+/[sq]\s+[a-z]:\\?\s*$",
    r"Remove-Item.*-Recurse.*[A-Z]:\\?\s*$",
    # Registry and critical processes
    r"reg\s+delete\s+HKLM",
    r"\btaskkill\b.*/f.*/im\s+winlogon",
]


class ToolError(RuntimeError):
    pass


def _is_loopback(host: str) -> bool:
    """Is this a local address? Decides whether the system proxy has to be bypassed.

    Once the host machine is behind a VPN/proxy, the system proxy swallows 127.0.0.1 too —
    that is already why the OneBot side cannot get self_id, so anything that talks to a
    local service has to bypass it.
    """
    import ipaddress
    h = (host or "").lower()
    if h in ("localhost", "127.0.0.1", "::1", "[::1]"):
        return True
    try:
        return ipaddress.ip_address(h).is_loopback
    except ValueError:
        return False


class LocalTools:
    """The tool set.

    ⚠️ `target_group` is **the session for this run**, not configuration. It cannot live on a
    shared instance: with two background tasks running concurrently (max_concurrent_tasks
    defaults to 2), group B overwrites group A's assignment, and when group A's model acts it
    reads group B's value — in one measured case both images went to the same group.
    In plain words: **it mutes the wrong person, kicks the wrong person, and posts the image
    into the wrong group.**

    So every run takes its own copy: `tools.for_target(gid)`.
    """

    def __init__(self, root: Path, store=None, logger_name: str = "agent", bot=None):
        self.root = Path(root).resolve()
        self.store = store
        # OneBot client used for sending images; when None, send_image refuses outright (the
        # tool is still listed).
        self.bot = bot
        # Which group/person this run sends to. Set by the caller (autoreply) before the run
        # starts — never read from model arguments, otherwise the model would be the one
        # picking the destination.
        self.target_group = ""
        self._image_sent: list[float] = []
        self.log_path = self.root / "data" / "agent.log"
        self.log_path.parent.mkdir(parents=True, exist_ok=True)

    def for_target(self, group_id: str) -> "LocalTools":
        """Return a copy that **serves only the given session**.

        Shallow copy: bot / store / root / log are shared with the original instance, only
        target_group is independent. `_image_sent` deliberately stays the same list object —
        image rate limiting is global, and a fresh copy must not double the 3-per-60s budget.
        """
        clone = copy.copy(self)
        clone.target_group = str(group_id or "")
        return clone

    # ---------- Logging ----------
    def _record(self, action: str, target: str, detail: dict, state: str, result: str = ""):
        line = json.dumps({
            "at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "action": action,
            "target": target,
            "detail": detail,
            "state": state,
            "result": result[:300],
        }, ensure_ascii=False)
        # Logging must never break a tool call: a failed file write only warns, and audit
        # errors are swallowed.
        try:
            with self.log_path.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except OSError:
            log.warning("agent.log 写入失败")
        log.info("[%s] %s %s -> %s", action, target, json.dumps(detail, ensure_ascii=False)[:200], state)
        if self.store is not None:
            try:
                self.store.audit("agent", action, target, detail, state, result)
            except Exception:
                pass

    # ---------- Path guardrails ----------
    def _safe(self, raw: str) -> Path:
        if not raw or not str(raw).strip():
            raise ToolError("路径不能为空。")
        p = Path(str(raw)).expanduser()
        if not p.is_absolute():
            p = self.root / p
        # Resolve first: ".." segments and symlinks must not slip past the prefix check below.
        p = p.resolve()
        try:
            p.relative_to(self.root)
        except ValueError:
            raise ToolError(f"越界：只允许操作 {self.root} 之内的路径。") from None
        return p

    # ---------- Tools ----------
    def list_dir(self, path: str = ".") -> dict:
        p = self._safe(path)
        if not p.is_dir():
            self._record("list_dir", str(p), {}, "FAILED", "不是目录")
            raise ToolError("不是目录。")
        rows = []
        for item in sorted(p.iterdir(), key=lambda x: (x.is_file(), x.name))[:200]:
            try:
                size = item.stat().st_size if item.is_file() else 0
            except OSError:
                size = 0
            rows.append({"name": item.name, "dir": item.is_dir(), "size": size})
        self._record("list_dir", str(p), {"count": len(rows)}, "SUCCEEDED")
        return {"path": str(p.relative_to(self.root)), "entries": rows}

    def read_file(self, path: str, offset: int = 1, limit: int = 400) -> dict:
        p = self._safe(path)
        if not p.is_file():
            self._record("read_file", str(p), {}, "FAILED", "文件不存在")
            raise ToolError("文件不存在。")
        size = p.stat().st_size
        if size > MAX_READ_BYTES:
            self._record("read_file", str(p), {"size": size}, "DENIED", "超过单次读取上限")
            raise ToolError(f"文件 {size} 字节，超过单次上限 {MAX_READ_BYTES}；请分段读。")
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            self._record("read_file", str(p), {}, "FAILED", str(exc))
            raise ToolError(f"读失败：{exc}") from None
        lines = text.splitlines()
        # Clamp the window: offset is 1-based, and limit is capped so one call cannot flood the
        # model.
        off = max(1, int(offset))
        seg = lines[off - 1: off - 1 + max(1, min(int(limit), 2000))]
        self._record("read_file", str(p), {"bytes": size, "lines": len(lines), "offset": off},
                     "SUCCEEDED")
        return {"path": str(p.relative_to(self.root)), "total_lines": len(lines),
                "offset": off, "content": "\n".join(seg)}

    def write_file(self, path: str, content: str, append: bool = False) -> dict:
        data = content if isinstance(content, str) else str(content)
        raw = data.encode("utf-8")
        if len(raw) > MAX_WRITE_BYTES:
            self._record("write_file", path, {"bytes": len(raw)}, "DENIED", "超过单次写入上限")
            raise ToolError(f"内容 {len(raw)} 字节，超过单次上限 {MAX_WRITE_BYTES}。")
        p = self._safe(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        try:
            # newline="" writes the bytes exactly as given — no CRLF translation on the way out.
            mode = "a" if append else "w"
            with p.open(mode, encoding="utf-8", newline="") as fh:
                fh.write(data)
        except OSError as exc:
            self._record("write_file", str(p), {}, "FAILED", str(exc))
            raise ToolError(f"写失败：{exc}") from None
        self._record("write_file", str(p), {"bytes": len(raw), "append": bool(append)}, "SUCCEEDED")
        return {"path": str(p.relative_to(self.root)), "bytes": len(raw), "append": bool(append)}

    def search_files(self, pattern: str, path: str = ".", limit: int = 50) -> dict:
        base = self._safe(path)
        rx = re.compile(pattern)
        hits, scanned = [], 0
        for dirpath, dirnames, filenames in os.walk(base):
            # Prune noisy dirs and cap the fan-out per level, so a big tree cannot stall this call.
            dirnames[:] = [d for d in dirnames if d not in
                           {".git", "__pycache__", "node_modules", ".venv", "data"}][:20]
            for name in filenames[:200]:
                scanned += 1
                if rx.search(name):
                    hits.append(str((Path(dirpath) / name).relative_to(self.root)))
                    if len(hits) >= limit:
                        break
            if len(hits) >= limit:
                break
        self._record("search_files", str(base), {"pattern": pattern, "scanned": scanned},
                     "SUCCEEDED")
        return {"pattern": pattern, "matches": hits[:limit], "scanned": scanned}

    def run_command(self, command: str, cwd: str = ".", timeout: int = DEFAULT_CMD_TIMEOUT) -> dict:
        cmd = (command or "").strip()
        if not cmd:
            raise ToolError("命令不能为空。")
        for bad in BANNED:
            if re.search(bad, cmd, re.IGNORECASE):
                self._record("run_command", cmd, {}, "DENIED", "命中危险命令黑名单")
                raise ToolError("该命令被安全策略拒绝。")
        work = self._safe(cwd)
        t0 = time.time()
        try:
            proc = subprocess.run(
                ["powershell", "-NoProfile", "-Command", cmd],
                cwd=str(work), capture_output=True, text=True,
                # Clamp to [1, 60]: the model may ask for a longer timeout but never gets one.
                timeout=max(1, min(int(timeout), 60)), encoding="utf-8", errors="replace",
            )
            # stdout is capped at MAX_OUTPUT_BYTES and stderr at 8 KB, so the tail stays small.
            out = (proc.stdout or "")[:MAX_OUTPUT_BYTES]
            err = (proc.stderr or "")[:8192]
            code = proc.returncode
        except subprocess.TimeoutExpired:
            self._record("run_command", cmd, {"cwd": str(work)}, "TIMEOUT", "")
            return {"ok": False, "error": "超时", "seconds": round(time.time() - t0, 2)}
        except Exception as exc:
            self._record("run_command", cmd, {"cwd": str(work)}, "FAILED", str(exc))
            return {"ok": False, "error": str(exc)[:300]}
        self._record("run_command", cmd,
                     {"cwd": str(work.relative_to(self.root)) if work != self.root else ".",
                      "exit": code, "seconds": round(time.time() - t0, 2)},
                     "SUCCEEDED" if code == 0 else "NONZERO")
        return {"ok": code == 0, "exit_code": code, "stdout": out, "stderr": err,
                "seconds": round(time.time() - t0, 2)}

    def fetch_url(self, url: str, max_bytes: int = 200000, strip_html: bool = True) -> dict:
        """Fetch a page or API and return its body. Saves the model from chaining curl commands."""
        target = (url or "").strip()
        parsed = urlparse(target)
        if parsed.scheme not in ALLOWED_SCHEMES or not parsed.hostname:
            self._record("fetch_url", target, {}, "DENIED", "只允许 http/https")
            raise ToolError("只允许抓取 http/https 地址。")
        import httpx
        try:
            # Egress policy is delegated to net: loopback goes direct, and an unreachable
            # remote proxy falls back to direct. This used to special-case loopback only, so
            # when the proxy died fetch_url died with it.
            resp = net.request("GET", target, timeout=20.0,
                               headers={"User-Agent": "Mozilla/5.0 (qqbridge)"})
            # max_bytes is what the caller asked for; MAX_FETCH_BYTES is the hard ceiling.
            raw = resp.content[: min(int(max_bytes), MAX_FETCH_BYTES)]
            text = raw.decode(resp.encoding or "utf-8", errors="replace")
        except Exception as exc:
            self._record("fetch_url", target, {}, "FAILED", str(exc)[:200])
            raise ToolError(f"抓取失败：{exc}") from None
        if strip_html and "html" in (resp.headers.get("content-type") or ""):
            text = re.sub(r"<script[\s\S]*?</script>", " ", text, flags=re.I)
            text = re.sub(r"<style[\s\S]*?</style>", " ", text, flags=re.I)
            text = re.sub(r"<[^>]+>", " ", text)
            text = re.sub(r"\s{2,}", " ", text).strip()
        self._record("fetch_url", target,
                     {"status": resp.status_code, "bytes": len(raw)}, "SUCCEEDED")
        return {"url": target, "status": resp.status_code, "content": text[:max_bytes],
                "bytes": len(raw)}

    # ---------- Sending images ----------
    IMAGE_WINDOW = 60.0            # rate-limit window (seconds)
    IMAGE_MAX_PER_WINDOW = 3       # max images per window, so it cannot spam the group
    IMAGE_MAX_BYTES = 8 * 1024 * 1024

    def _sniff_image(self, head: bytes) -> str:
        # Magic bytes rather than the file extension — a .jpg that is really HTML must not pass.
        if head[:8] == b"\x89PNG\r\n\x1a\n":
            return "png"
        if head[:3] == b"GIF":
            return "gif"
        if head[:2] == b"\xff\xd8":
            return "jpg"
        if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
            return "webp"
        return ""

    def _rate_ok(self) -> bool:
        now = time.time()
        # Sliding window: drop timestamps that fell out of the window before counting.
        self._image_sent = [t for t in self._image_sent if now - t < self.IMAGE_WINDOW]
        return len(self._image_sent) < self.IMAGE_MAX_PER_WINDOW

    async def send_image(self, image: str = "", caption: str = "", expect: str = "") -> dict:
        """Send one image to **the group this run belongs to**.

        Two sources, each with its own guardrails:
          - Local file: must be inside the workspace (goes through _safe), and the magic
            bytes must confirm it really is an image. Arbitrary paths "someone happened to
            mention" are refused, so the model cannot be steered into reading files outside
            the workspace.
          - http/https link: private/loopback addresses are refused (anti-SSRF), and the
            response must actually be an image.

        The target group never comes from arguments — the caller sets it, the model has no
        say in it.
        """
        raw = (image or "").strip()
        if not raw:
            raise ToolError("要给图片的路径或 URL。")
        if not self.bot:
            self._record("send_image", raw, {}, "DENIED", "未接入 OneBot 客户端")
            raise ToolError("发图未启用（没接上 OneBot 客户端）。")
        gid = (self.target_group or "").strip()
        if not gid:
            self._record("send_image", raw, {}, "DENIED", "没有目标会话")
            raise ToolError("这一轮没有可发送的目标会话。")
        if not self._rate_ok():
            self._record("send_image", raw, {"window": self.IMAGE_WINDOW}, "DENIED", "限流")
            raise ToolError(f"发图太频繁（{self.IMAGE_WINDOW:.0f} 秒内最多 "
                            f"{self.IMAGE_MAX_PER_WINDOW} 张），先攒着。")

        # Somebody must have looked at the image before it is sent — both to check that it
        # is safe and to check that it is the one that was asked for. This went wrong once:
        # someone wanted a photo of 600 Wanping South Road and a loli image got fetched and
        # posted into the group. The model was only passing a link/path along, and not a
        # single stage ever looked at the content. If the check fails we **do not send**; if
        # the check itself cannot run we **do not send** either (fail closed).
        reviewed = ""
        if config.send_image_review:
            from . import vision
            data_url = await vision.to_data_url(raw)
            if not data_url:
                self._record("send_image", raw, {}, "DENIED", "读不到图片内容，无法核验")
                raise ToolError("读不到这张图，没法核验，不发。")
            verdict = await vision.review(data_url, expect)
            if not verdict.get("ok"):
                self._record("send_image", raw,
                             {"desc": verdict.get("desc") or "",
                              "unsafe": verdict.get("unsafe"),
                              "match": verdict.get("match")},
                             "DENIED", verdict.get("reason") or "核验未通过")
                raise ToolError("发图前核验没通过：" + (verdict.get("reason") or "拿不准就不发"))
            reviewed = verdict.get("desc") or ""

        parsed = urlparse(raw)
        payload = ""
        if parsed.scheme in ALLOWED_SCHEMES and parsed.hostname:
            if not self._public_host(parsed.hostname):
                self._record("send_image", raw, {}, "DENIED", "内网地址")
                raise ToolError("不接受内网/本机地址。")
            # **The whole string has to be one clean URL.** urlparse only looks at the head,
            # so for
            # "https://a/x.png] [CQ:at,qq=all][CQ:image,file=http://127.0.0.1:5700/b"
            # the hostname is a and it passes the host check — but once spliced into CQ code
            # the first part closes early and the rest becomes a second instruction: @everyone,
            # or another image that never passed review and never got the SSRF check.
            if not is_safe_cq_param(raw):
                self._record("send_image", raw, {}, "DENIED", "URL 里含 CQ 码分隔符")
                raise ToolError("这个图片地址里含 [ ] & 或逗号/空白，不能作为 CQ 参数，拒绝发送。")
            payload = f"[CQ:image,file={raw}]"
            detail = {"via": "url"}
        elif parsed.scheme:
            self._record("send_image", raw, {}, "DENIED", f"不支持的协议 {parsed.scheme}")
            raise ToolError("只支持本地文件路径或 http/https 链接。")
        else:
            p = self._safe(raw)              # out-of-root paths raise right here
            if not p.is_file():
                self._record("send_image", str(p), {}, "FAILED", "文件不存在")
                raise ToolError(f"文件不存在：{p}")
            size = p.stat().st_size
            if size > self.IMAGE_MAX_BYTES:
                self._record("send_image", str(p), {"bytes": size}, "DENIED", "文件过大")
                raise ToolError("图片太大了。")
            head = p.open("rb").read(16)
            kind = self._sniff_image(head)
            if not kind:
                self._record("send_image", str(p), {"bytes": size}, "DENIED", "不是图片")
                raise ToolError("这个文件按内容判断不是图片。")
            # Local paths go to OneBot as file:// URIs so it cannot mis-resolve a relative path
            payload = f"[CQ:image,file=file:///{str(p).replace(chr(92), '/')}]"
            detail = {"via": "path", "bytes": size, "kind": kind}

        # The caption is model-written as well, so it is escaped too — otherwise
        # caption="[CQ:at,qq=all] come quick" is an @everyone bomb.
        cap = escape_cq(caption.strip())
        msg = payload if not cap else f"{cap}\n{payload}"
        try:
            result = await self.bot.call("send_group_msg", group_id=int(gid), message=msg)
        except Exception as exc:
            self._record("send_image", raw, detail, "FAILED", str(exc)[:200])
            raise ToolError(f"发送失败：{exc}") from None
        self._image_sent.append(time.time())
        self._record("send_image", raw, {**detail, "group": gid, "reviewed": reviewed[:150]},
                     "SUCCEEDED", str(result)[:120])
        return {"sent": True, "group_id": gid, "detail": detail, "reviewed": reviewed}

    @staticmethod
    def _public_host(host: str) -> bool:
        import ipaddress
        import socket
        try:
            infos = socket.getaddrinfo(host, None)
        except Exception:
            return False
        # Every resolved address must be public: a single private answer is enough to refuse,
        # so a name whose A records point at 127.0.0.1 cannot slip through this check.
        for info in infos:
            try:
                ip = ipaddress.ip_address(info[4][0])
            except ValueError:
                return False
            if (ip.is_private or ip.is_loopback or ip.is_link_local
                    or ip.is_reserved or ip.is_multicast or ip.is_unspecified):
                return False
        return True

    # ---------- Group management ----------
    # Only available on the "an admin talks to the bot directly" path (only autoreply's
    # _handle_task sets target_group). The idle-chat path does not have this tool, and the
    # model cannot name a target group either.
    MANAGE_ACTIONS = ("mute", "unmute", "kick", "rename", "card", "whole_ban")
    MUTE_MAX_SECONDS = 30 * 24 * 3600      # QQ caps a single mute at 30 days

    async def manage_group(self, action: str = "", user_id: str = "",
                           duration_seconds: int = 0, value: str = "") -> dict:
        """Group management. As with the target group, **the caller is the backstop for the
        permission surface**: no target_group means refusal.

        No leave (quitting the group) — that is irreversible and affects the whole group, so
        it is left for a human to click in their client.
        """
        act = (action or "").strip().lower()
        if act not in self.MANAGE_ACTIONS:
            raise ToolError("action 只能是：" + "/".join(self.MANAGE_ACTIONS))
        if not self.bot:
            raise ToolError("群管理未启用（没接上 OneBot 客户端）。")
        if not config.allow_manage:
            self._record("manage_group", act, {}, "DENIED", "ALLOW_MANAGE=false")
            raise ToolError("群管理被关闭了（ALLOW_MANAGE=false）。")
        gid = (self.target_group or "").strip()
        if not gid:
            self._record("manage_group", act, {}, "DENIED", "没有目标会话")
            raise ToolError("这条指令不是管理员在当前群当面说的，不能执行群管理。")

        uid = str(user_id or "").strip()
        dur = int(duration_seconds or 0)
        detail = {"group": gid, "action": act, "user_id": uid, "duration": dur}

        needs_user = act in ("mute", "unmute", "kick", "card")
        if needs_user and not uid:
            raise ToolError("这个动作需要 user_id（对方 QQ 号）。")
        if act == "mute" and dur <= 0:
            # duration=0 in set_group_ban **unmutes**, not a 0-second mute — do not invert it.
            raise ToolError("禁言必须给 duration_seconds（大于 0）；要解除请用 unmute。")
        if act == "mute":
            dur = min(dur, self.MUTE_MAX_SECONDS)
            detail["duration"] = dur
        if act == "rename" and not (value or "").strip():
            raise ToolError("改群名需要 value。")

        # Role check: QQ does not allow acting on the group owner, and admins of the same
        # rank usually cannot act on each other either.
        if needs_user:
            target = None
            try:
                target = self.store.member(gid, uid) if self.store else None
            except Exception:
                target = None
            role = (target or {}).get("role") or ""
            detail["target_role"] = role
            if role == "owner":
                self._record("manage_group", act, detail, "REJECTED", "目标是群主")
                raise ToolError("对方是群主，QQ 不允许对群主做这个操作。")
            if act in ("mute", "kick") and role == "admin":
                self._record("manage_group", act, detail, "REJECTED", "目标是管理员")
                raise ToolError("对方是管理员，同级之间通常动不了；要动手请群主在客户端操作。")

        try:
            if act == "mute":
                result = await self.bot.call("set_group_ban", group_id=int(gid),
                                             user_id=int(uid), duration=dur)
            elif act == "unmute":
                result = await self.bot.call("set_group_ban", group_id=int(gid),
                                             user_id=int(uid), duration=0)
            elif act == "kick":
                result = await self.bot.call("set_group_kick", group_id=int(gid),
                                             user_id=int(uid), reject_add_request=False)
            elif act == "rename":
                result = await self.bot.call("set_group_name", group_id=int(gid),
                                             group_name=value)
            elif act == "card":
                result = await self.bot.call("set_group_card", group_id=int(gid),
                                             user_id=int(uid), card=value)
            else:
                truthy = str(value).strip().lower() in ("1", "true", "yes", "on", "开")
                result = await self.bot.call("set_group_whole_ban", group_id=int(gid),
                                             enable=truthy)
        except Exception as exc:
            self._record("manage_group", act, detail, "FAILED", str(exc)[:200])
            raise ToolError("执行失败：" + str(exc)[:150]) from None

        self._record("manage_group", act, detail, "SUCCEEDED", str(result)[:120])
        # The audit only proves the request was sent — whether it actually took effect needs
        # a follow-up check; do not vouch for the server.
        return {"done": True, "action": act, "group_id": gid, "user_id": uid,
                "duration_seconds": dur, "result": result,
                "note": "这是接口返回值，不保证已生效；要确认请回查成员角色或禁言状态。"}

    # ---------- Schemas exposed to the model ----------
    @staticmethod
    def schemas() -> list[dict]:
        return [
            {"type": "function", "function": {
                "name": "list_dir", "description": "列出目录内容（限工作区内）",
                "parameters": {"type": "object", "properties": {
                    "path": {"type": "string", "description": "相对工作区的路径"}},
                    "required": ["path"]}}},
            {"type": "function", "function": {
                "name": "read_file", "description": "读取文本文件（单次上限 256KB）",
                "parameters": {"type": "object", "properties": {
                    "path": {"type": "string"},
                    "offset": {"type": "integer", "default": 1},
                    "limit": {"type": "integer", "default": 400}}, "required": ["path"]}}},
            {"type": "function", "function": {
                "name": "write_file", "description": "写入文本文件（单次上限 1MB）",
                "parameters": {"type": "object", "properties": {
                    "path": {"type": "string"}, "content": {"type": "string"},
                    "append": {"type": "boolean", "default": False}}, "required": ["path", "content"]}}},
            {"type": "function", "function": {
                "name": "search_files", "description": "按文件名正则搜索",
                "parameters": {"type": "object", "properties": {
                    "pattern": {"type": "string"}, "path": {"type": "string", "default": "."},
                    "limit": {"type": "integer", "default": 50}}, "required": ["pattern"]}}},
            {"type": "function", "function": {
                "name": "fetch_url",
                "description": "抓取一个 http/https 网页或 API 并返回正文（自动去 HTML 标签）。"
                               "要查资料、看网页内容时用这个，不要用 curl 拼命令。",
                "parameters": {"type": "object", "properties": {
                    "url": {"type": "string"}}, "required": ["url"]}}},
            {"type": "function", "function": {
                "name": "send_image",
                "description": "把一张图发到当前这个群。image 可以是工作区内的本地文件路径，"
                               "也可以是 http/https 图片链接。要发图就用这个 —— 别用 run_command "
                               "去拼 CQ 码。目标群由系统决定，你不用也不能指定。"
                               "**发送前系统会先用视觉核验这张图**：内容不安全、或者和图不对版，"
                               "都会被拦下并告诉你原因 —— 所以 expect 一定要如实写。",
                "parameters": {"type": "object", "properties": {
                    "image": {"type": "string", "description": "本地文件路径或 http/https 图片 URL"},
                    "caption": {"type": "string", "default": "", "description": "可选的配文"},
                    "expect": {"type": "string", "default": "",
                               "description": "这张图应该是什么（一句话）。系统会拿它和图的实际内容"
                                              "比对，对不上就不发。别乱写，也别为了过关而编。"}},
                    "required": ["image", "expect"]}}},
            {"type": "function", "function": {
                "name": "manage_group",
                "description": "QQ 群管理。**只有在管理员当面跟你说话时才能用** —— "
                               "目标群由系统决定，你不用也不能指定。"
                               "action: mute 禁言 / unmute 解除禁言 / kick 踢出 / "
                               "rename 改群名 / card 改群名片 / whole_ban 全员禁言。"
                               "禁言必须给 duration_seconds（秒，上限 30 天）；"
                               "解除禁言用 unmute，**不要**用 mute + 0（0 就是解除）。",
                "parameters": {"type": "object", "properties": {
                    "action": {"type": "string",
                               "enum": ["mute", "unmute", "kick", "rename", "card",
                                        "whole_ban"]},
                    "user_id": {"type": "string", "default": "",
                                "description": "对方 QQ 号；mute/unmute/kick/card 必填"},
                    "duration_seconds": {"type": "integer", "default": 0,
                                         "description": "仅 mute 用，秒，上限 2592000"},
                    "value": {"type": "string", "default": "",
                              "description": "rename 用新群名；card 用新名片；whole_ban 用 开/关"}},
                    "required": ["action"]}}},
            {"type": "function", "function": {
                "name": "run_command",
                "description": "执行一条 PowerShell 命令（最长 60 秒，默认 15 秒）。"
                               "仅用于本地文件/程序操作；抓网页请用 fetch_url。一次只跑一条。",
                "parameters": {"type": "object", "properties": {
                    "command": {"type": "string"}, "cwd": {"type": "string", "default": "."},
                    "timeout": {"type": "integer", "default": 15}}, "required": ["command"]}}},
        ]

    async def dispatch_async(self, name: str, args: dict) -> dict:
        """**Async dispatch**: synchronous tools are always pushed onto the thread pool and
        never block the event loop.

        Lesson from history: run_command is a synchronous subprocess.run, and calling it
        straight from async froze the whole service — during one 21-second curl, both HTTP
        and WebSocket were unresponsive.
        """
        # send_image is async on its own (it awaits HTTP), so it cannot be wrapped in a
        # thread — dropping it into the pool only yields a coroutine nobody awaits.
        if name == "send_image":
            return await self.send_image(**(args or {}))
        if name == "manage_group":
            return await self.manage_group(**(args or {}))
        return await asyncio.to_thread(self.dispatch, name, args)

    def dispatch(self, name: str, args: dict) -> dict:
        fn = {"list_dir": self.list_dir, "read_file": self.read_file,
              "write_file": self.write_file, "search_files": self.search_files,
              "run_command": self.run_command, "fetch_url": self.fetch_url}.get(name)
        # An unknown name is the model's mistake too; report it as a tool error, not as a crash.
        if fn is None:
            raise ToolError(f"未知工具 {name}")
        try:
            return fn(**(args or {}))
        except ToolError:
            raise
        except TypeError as exc:
            raise ToolError(f"参数不对：{exc}") from None
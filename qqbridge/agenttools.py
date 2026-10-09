"""内置 Agent 的本地工具集：读写文件、列目录、跑命令、搜索。

**所有读取与写入都会打印到日志并记入审计表** —— 这是硬要求：
`data/agent.log` 逐行记录 时间 / 动作 / 路径 / 结果 / 字节数。

安全边界：
- 文件操作被限制在 workspace 根目录内（可配置），越界直接拒绝
- 命令有黑名单（格式化、关机、删盘之类）
- 每次调用都有大小/条数上限，避免一次读爆内存
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import subprocess
import time
from pathlib import Path
from urllib.parse import urlparse

from .config import config

log = logging.getLogger("qqbridge.agenttools")

MAX_READ_BYTES = 256 * 1024
MAX_WRITE_BYTES = 1024 * 1024
MAX_OUTPUT_BYTES = 64 * 1024
MAX_FETCH_BYTES = 512 * 1024
DEFAULT_CMD_TIMEOUT = 15          # 秒；超时就掐，别拖住整轮

ALLOWED_SCHEMES = ("http", "https")

# 危险命令黑名单。
#
# 血的教训：第一条原来写的是 r"\bformat\b"，本意是挡「格式化磁盘」，
# 但 PowerShell 的 Format-List / Format-Table / Format-Wide 全都会命中 ——
# 于是「systeminfo | ... | Format-List」这种纯读命令被拒，
# 模型反复重试直到步数耗尽，最后往群里发了一句「步骤用尽」。
# 现在只匹配真正会毁数据的写法，宁可写长一点。
BANNED = [
    # 格式化：必须带盘符或 /q /s /f 参数。Format-Volume 是另一个 cmdlet，单列
    r"(?<![\w.-])format(?:\.com)?\s+[a-z]:",
    r"(?<![\w.-])format(?:\.com)?\s+/[qsf]",
    r"\bFormat-Volume\b",
    r"\bmkfs(?:\.\w+)?\b",
    # 关机 / 重启
    r"(?<![\w.-])shutdown(?:\.exe)?\s+/[a-z]",
    r"\bStop-Computer\b", r"\bRestart-Computer\b",
    r"(?<![\w.-])reboot(?:\.exe)?\b",
    # 磁盘与分区
    r"\bdiskpart\b", r"\bClear-Disk\b", r"\bInitialize-Disk\b",
    r"\bRemove-Partition\b",
    # 删根目录
    r"rm\s+-rf\s+/",
    r"del\s+/[sq]\s+[a-z]:\\?\s*$",
    r"Remove-Item.*-Recurse.*[A-Z]:\\?\s*$",
    # 注册表与关键进程
    r"reg\s+delete\s+HKLM",
    r"\btaskkill\b.*/f.*/im\s+winlogon",
]


class ToolError(RuntimeError):
    pass


class LocalTools:
    def __init__(self, root: Path, store=None, logger_name: str = "agent", bot=None):
        self.root = Path(root).resolve()
        self.store = store
        # 发图要用的 OneBot 客户端；为 None 时 send_image 直接拒绝（工具仍然可选）。
        self.bot = bot
        # 当前这一轮要发给哪个群/人。由调用方（autoreply）在跑之前设好 ——
        # 绝不从模型参数里取目标，否则等于让模型自己挑发到哪。
        self.target_group = ""
        self._image_sent: list[float] = []
        self.log_path = self.root / "data" / "agent.log"
        self.log_path.parent.mkdir(parents=True, exist_ok=True)

    # ---------- 日志 ----------
    def _record(self, action: str, target: str, detail: dict, state: str, result: str = ""):
        line = json.dumps({
            "at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "action": action,
            "target": target,
            "detail": detail,
            "state": state,
            "result": result[:300],
        }, ensure_ascii=False)
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

    # ---------- 路径护栏 ----------
    def _safe(self, raw: str) -> Path:
        if not raw or not str(raw).strip():
            raise ToolError("路径不能为空。")
        p = Path(str(raw)).expanduser()
        if not p.is_absolute():
            p = self.root / p
        p = p.resolve()
        try:
            p.relative_to(self.root)
        except ValueError:
            raise ToolError(f"越界：只允许操作 {self.root} 之内的路径。") from None
        return p

    # ---------- 工具 ----------
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
                timeout=max(1, min(int(timeout), 60)), encoding="utf-8", errors="replace",
            )
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
        """抓一个网页/接口并返回正文。比让模型拼 curl 省好几步。"""
        target = (url or "").strip()
        parsed = urlparse(target)
        if parsed.scheme not in ALLOWED_SCHEMES or not parsed.hostname:
            self._record("fetch_url", target, {}, "DENIED", "只允许 http/https")
            raise ToolError("只允许抓取 http/https 地址。")
        import httpx
        try:
            with httpx.Client(timeout=20.0, follow_redirects=True,
                              headers={"User-Agent": "Mozilla/5.0 (qqbridge)"}) as client:
                resp = client.get(target)
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

    # ---------- 发图 ----------
    IMAGE_WINDOW = 60.0            # 限流窗口（秒）
    IMAGE_MAX_PER_WINDOW = 3       # 窗口内最多发几张，防刷屏
    IMAGE_MAX_BYTES = 8 * 1024 * 1024

    def _sniff_image(self, head: bytes) -> str:
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
        self._image_sent = [t for t in self._image_sent if now - t < self.IMAGE_WINDOW]
        return len(self._image_sent) < self.IMAGE_MAX_PER_WINDOW

    async def send_image(self, image: str = "", caption: str = "", expect: str = "") -> dict:
        """把一张图发到**当前这一轮对应的群**。

        两条来源，各自的护栏不同：
          - 本地文件：必须在工作区内（走 _safe），且按 magic bytes 确认真是图片。
            不接受"用户随口给的任意路径"，避免被引导去读工作区外的文件。
          - http/https 链接：拒绝内网/回环（防 SSRF），并确认返回的确实是图。

        目标群不来自参数 —— 由调用方设定，模型没有选择权。
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

        # 发送前必须有人先看过这张图 —— 既查安不安全，也查是不是要的那张。
        # 出过事：有人要「宛平南路六百号」的照片，结果抓了张萝莉图发进群。
        # 模型只是把一条链接/路径转手丢出去，全程没有任何环节看过内容。
        # 校验不通过就**不发**；校验本身跑不起来也**不发**（fail closed）。
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
            payload = f"[CQ:image,file={raw}]"
            detail = {"via": "url"}
        elif parsed.scheme:
            self._record("send_image", raw, {}, "DENIED", f"不支持的协议 {parsed.scheme}")
            raise ToolError("只支持本地文件路径或 http/https 链接。")
        else:
            p = self._safe(raw)              # 越界会在这里抛
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
            # 本机路径交给 OneBot 时用 file:// URI，避免它按相对路径找错地方
            payload = f"[CQ:image,file=file:///{str(p).replace(chr(92), '/')}]"
            detail = {"via": "path", "bytes": size, "kind": kind}

        msg = payload if not caption.strip() else f"{caption.strip()}\n{payload}"
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
        for info in infos:
            try:
                ip = ipaddress.ip_address(info[4][0])
            except ValueError:
                return False
            if (ip.is_private or ip.is_loopback or ip.is_link_local
                    or ip.is_reserved or ip.is_multicast or ip.is_unspecified):
                return False
        return True

    # ---------- 给模型看的 schema ----------
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
                "name": "run_command",
                "description": "执行一条 PowerShell 命令（最长 60 秒，默认 15 秒）。"
                               "仅用于本地文件/程序操作；抓网页请用 fetch_url。一次只跑一条。",
                "parameters": {"type": "object", "properties": {
                    "command": {"type": "string"}, "cwd": {"type": "string", "default": "."},
                    "timeout": {"type": "integer", "default": 15}}, "required": ["command"]}}},
        ]

    async def dispatch_async(self, name: str, args: dict) -> dict:
        """**异步分发**：同步工具一律丢到线程池，绝不阻塞事件循环。

        历史教训：run_command 是同步 subprocess.run，直接在 async 里调用会把
        整个服务卡住 —— 一个 21 秒的 curl 期间，HTTP 和 WebSocket 全部无响应。
        """
        # send_image 自己就是异步的（要 await HTTP），不能再套线程 ——
        # 丢进线程池只会拿到一个没人 await 的协程。
        if name == "send_image":
            return await self.send_image(**(args or {}))
        return await asyncio.to_thread(self.dispatch, name, args)

    def dispatch(self, name: str, args: dict) -> dict:
        fn = {"list_dir": self.list_dir, "read_file": self.read_file,
              "write_file": self.write_file, "search_files": self.search_files,
              "run_command": self.run_command, "fetch_url": self.fetch_url}.get(name)
        if fn is None:
            raise ToolError(f"未知工具 {name}")
        try:
            return fn(**(args or {}))
        except ToolError:
            raise
        except TypeError as exc:
            raise ToolError(f"参数不对：{exc}") from None
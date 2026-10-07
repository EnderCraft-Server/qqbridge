"""内置 Agent 的本地工具集：读写文件、列目录、跑命令、搜索。

**所有读取与写入都会打印到日志并记入审计表** —— 这是硬要求：
`data/agent.log` 逐行记录 时间 / 动作 / 路径 / 结果 / 字节数。

安全边界：
- 文件操作被限制在 workspace 根目录内（可配置），越界直接拒绝
- 命令有黑名单（格式化、关机、删盘之类）
- 每次调用都有大小/条数上限，避免一次读爆内存
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

log = logging.getLogger("qqbridge.agenttools")

MAX_READ_BYTES = 256 * 1024
MAX_WRITE_BYTES = 1024 * 1024
MAX_OUTPUT_BYTES = 64 * 1024

BANNED = [
    r"\bformat\b", r"\bmkfs\b", r"\bshutdown\b", r"\breboot\b",
    r"rm\s+-rf\s+/", r"del\s+/[sq]\s+[a-z]:\\?\s*$",
    r"Remove-Item.*-Recurse.*[A-Z]:\\?\s*$", r"\bdiskpart\b",
    r"reg\s+delete\s+HKLM", r"\btaskkill\b.*/f.*/im\s+winlogon",
]


class ToolError(RuntimeError):
    pass


class LocalTools:
    def __init__(self, root: Path, store=None, logger_name: str = "agent"):
        self.root = Path(root).resolve()
        self.store = store
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

    def run_command(self, command: str, cwd: str = ".", timeout: int = 30) -> dict:
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
                timeout=max(1, min(int(timeout), 120)), encoding="utf-8", errors="replace",
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
                "name": "run_command", "description": "在工作区内执行 PowerShell 命令（最长 120 秒）",
                "parameters": {"type": "object", "properties": {
                    "command": {"type": "string"}, "cwd": {"type": "string", "default": "."},
                    "timeout": {"type": "integer", "default": 30}}, "required": ["command"]}}},
        ]

    def dispatch(self, name: str, args: dict) -> dict:
        fn = {"list_dir": self.list_dir, "read_file": self.read_file,
              "write_file": self.write_file, "search_files": self.search_files,
              "run_command": self.run_command}.get(name)
        if fn is None:
            raise ToolError(f"未知工具 {name}")
        try:
            return fn(**(args or {}))
        except ToolError:
            raise
        except TypeError as exc:
            raise ToolError(f"参数不对：{exc}") from None

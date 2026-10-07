"""群内命令：/stop /start /auto。只有管理员能用，命令本身不进入队列。"""
from __future__ import annotations

COMMANDS = {
    "/stop": "stopped",
    "/start": "manual",
    "/auto": "auto",
}
ALIASES = {"/pause": "stopped", "/resume": "manual", "/继续": "manual", "/停止": "stopped"}


def parse(text: str) -> str | None:
    """返回目标模式；不是控制命令则返回 None。"""
    raw = (text or "").strip()
    if not raw:
        return None
    head = raw.split()[0].lower()
    return COMMANDS.get(head) or ALIASES.get(head)

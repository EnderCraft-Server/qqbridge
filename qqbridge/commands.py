"""群内命令：/stop /start /auto。只有管理员能用，命令本身不进入队列。"""
from __future__ import annotations

COMMANDS = {
    "/stop": "stopped",
    "/start": "manual",
    "/auto": "auto",
}
ALIASES = {
    "/pause": "stopped", "/off": "stopped", "/停止": "stopped", "/关闭": "stopped",
    "/resume": "manual", "/on": "auto", "/继续": "manual", "/开启": "auto",
}


def parse(text: str) -> str | None:
    """返回目标运行模式（auto/stopped/manual）；不是控制命令则返回 None。"""
    raw = (text or "").strip()
    if not raw:
        return None
    head = raw.split()[0].lower()
    return COMMANDS.get(head) or ALIASES.get(head)


SWITCHES = {"agent": "agent", "chat": "chat", "chatbot": "chat", "聊天": "chat", "干活": "agent"}


def parse_switch(text: str) -> str | None:
    """解析 /switch agent | /switch chat。返回目标行为模式，或 None。

    只认 /switch 开头；缺参数时返回特殊值 "?"，便于回一句用法。
    """
    raw = (text or "").strip()
    if not raw.startswith("/switch"):
        return None
    parts = raw.split()
    if len(parts) < 2:
        return "?"
    return SWITCHES.get(parts[1].lower().strip())
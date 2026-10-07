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

# 自然语言切换：说「使用agent模式执行：xxx」也能切，不用记命令
import re as _re

_NATURAL = (
    _re.compile(r"^\s*(?:请)?(?:使用|用|切换到|切到|开启|进入)?\s*agent\s*模式\s*(?:执行|运行|来|干|做)?\s*[:：]?\s*(.*)$", _re.I | _re.S),
    _re.compile(r"^\s*(?:请)?(?:使用|用|切换到|切到|开启|进入)?\s*chat\s*模式\s*(?:执行|运行|来)?\s*[:：]?\s*(.*)$", _re.I | _re.S),
)
_CN = (
    _re.compile(r"^\s*(?:请)?(?:使用|用)?\s*(?:干活|工作|任务)\s*模式\s*[:：]?\s*(.*)$", _re.S),
    _re.compile(r"^\s*(?:请)?(?:使用|用)?\s*(?:聊天|闲聊)\s*模式\s*[:：]?\s*(.*)$", _re.S),
)


def parse_natural_switch(text: str):
    """识别「使用agent模式执行：xxx」这类说法。

    返回 (目标模式, 剩余内容)；不像切换指令则返回 None。
    只认明确带「模式」二字且以切换语开头的说法，避免误伤正常聊天。
    """
    raw = (text or "").strip()
    if not raw or "/switch" in raw:
        return None
    # 必须出现「模式」而且句首是切换意图，才认为是控制指令
    if "模式" not in raw:
        return None
    head = raw[:14].lower()
    if not any(k in head for k in ("使用", "用", "切换", "切到", "开启", "进入",
                                   "agent", "chat", "干活", "聊天", "闲聊")):
        return None
    for rx, mode in zip(_NATURAL, ("agent", "chat")):
        m = rx.match(raw)
        if m:
            return mode, (m.group(1) or "").strip()
    for rx, mode in zip(_CN, ("agent", "chat")):
        m = rx.match(raw)
        if m:
            return mode, (m.group(1) or "").strip()
    return None

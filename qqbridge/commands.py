"""群内命令：/stop /start /auto。只有管理员能用，命令本身不进入队列。"""
from __future__ import annotations

COMMANDS = {
    "/stop": "stopped",
    "/start": "manual",
    "/auto": "auto",
}
# 提示语：命令必须有回执，否则用户不知道生效没有
REPLIES = {
    "stopped": "已停止当前会话的自动接话功能，并已停止所有agent请求",
    "auto": "已开启对话自动接话功能，并接受所有agent请求",
    "manual": "已切换为手动模式：只保留工具，不自动接话",
}


def reply_for(mode: str) -> str:
    return REPLIES.get(mode, "已切换运行模式：" + mode)


def parse(text: str) -> str | None:
    """返回目标运行模式（auto/stopped/manual）；不是控制命令则返回 None。"""
    raw = (text or "").strip()
    if not raw:
        return None
    head = raw.split()[0].lower()
    return COMMANDS.get(head)



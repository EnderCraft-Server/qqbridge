"""In-group commands: /stop /start /auto. Admin-only, and never queued."""
from __future__ import annotations

COMMANDS = {
    "/stop": "stopped",
    "/start": "manual",
    "/auto": "auto",
}
# Reply text: a command must acknowledge, otherwise the user cannot tell whether
# it took effect
REPLIES = {
    "stopped": "已停止当前会话的自动接话功能，并已停止所有agent请求",
    "auto": "已开启对话自动接话功能，并接受所有agent请求",
    "manual": "已切换为手动模式：只保留工具，不自动接话",
}


def reply_for(mode: str) -> str:
    return REPLIES.get(mode, "已切换运行模式：" + mode)


def parse(text: str) -> str | None:
    """Return the target mode (auto/stopped/manual); None if this is not a control command."""
    raw = (text or "").strip()
    if not raw:
        return None
    # Only the first token counts, so "/stop please" still works.
    head = raw.split()[0].lower()
    return COMMANDS.get(head)



"""Configuration loaded from .env next to the project root."""
import os
from pathlib import Path
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")


def _bool(name: str, default: bool = False) -> bool:
    raw = (os.environ.get(name) or "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


def _int(name: str, default: int) -> int:
    try:
        return int((os.environ.get(name) or "").strip() or default)
    except ValueError:
        return default


def admins() -> set:
    """管理员：可以执行 /stop /start /auto。留空则退回 OWNER_IDS。"""
    raw = os.environ.get("ADMIN_IDS") or ""
    return {x.strip() for x in raw.replace("，", ",").split(",") if x.strip()}


def owners() -> set:
    raw = os.environ.get("OWNER_IDS") or ""
    return {x.strip() for x in raw.replace("，", ",").split(",") if x.strip()}


class Config:
    def __init__(self):
        self.http = (os.environ.get("ONEBOT_HTTP") or "http://127.0.0.1:3000").rstrip("/")
        self.ws = (os.environ.get("ONEBOT_WS") or "ws://127.0.0.1:3001").strip()
        self.token = (os.environ.get("ONEBOT_TOKEN") or "").strip()
        self.ws_token = (os.environ.get("ONEBOT_WS_TOKEN") or "").strip() or self.token
        self.host = os.environ.get("MCP_HOST") or "127.0.0.1"
        self.port = _int("MCP_PORT", 18900)
        self.path = os.environ.get("MCP_PATH") or "/mcp"
        self.ring_size = _int("RING_SIZE", 500)
        self.cooldown = _int("COOLDOWN_SECONDS", 90)
        self.allow_send = _bool("ALLOW_SEND", False)
        self.allow_manage = _bool("ALLOW_MANAGE", False)
        self.owners = owners()
        self.admins = admins() or self.owners
        self.mcp_token = (os.environ.get("MCP_TOKEN") or "").strip() or "change-me"
        raw_watch = (os.environ.get("WATCH_GROUPS") or "").strip()
        self.watch_groups = {x.strip() for x in raw_watch.replace("，", ",").split(",") if x.strip()}
        self.data_dir = ROOT / "data"

    def persist_permissions(self):
        """Write the safety switches back to .env so they survive a restart."""
        path = ROOT / ".env"
        try:
            lines = path.read_text(encoding="utf-8").splitlines() if path.is_file() else []
        except OSError:
            return False
        want = {
            "ALLOW_SEND": "true" if self.allow_send else "false",
            "ALLOW_MANAGE": "true" if self.allow_manage else "false",
            "OWNER_IDS": ",".join(sorted(self.owners)),
            "WATCH_GROUPS": ",".join(sorted(self.watch_groups)),
        }
        seen = set()
        out = []
        for line in lines:
            key = line.split("=", 1)[0].strip() if "=" in line else ""
            if key in want:
                out.append(f"{key}={want.pop(key)}")
                seen.add(key)
            else:
                out.append(line)
        for key, val in want.items():
            out.append(f"{key}={val}")
        path.write_text("\n".join(out) + "\n", encoding="utf-8")
        return True

    def describe(self) -> dict:
        return {
            "http": self.http,
            "ws": self.ws,
            "ws_token_set": bool(self.ws_token),
            "host": self.host,
            "port": self.port,
            "path": self.path,
            "owners": sorted(self.owners),
            "admins": sorted(self.admins),
            "allow_send": self.allow_send,
            "allow_manage": self.allow_manage,
            "cooldown_seconds": self.cooldown,
            "watch_groups": sorted(self.watch_groups),
        }


config = Config()
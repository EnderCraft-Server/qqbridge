"""Configuration loaded from .env next to the project root."""
import logging
import os
import secrets
from pathlib import Path
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")


# Values that count as "not configured": former defaults and common placeholders.
_WEAK_TOKENS = {
    "", "change-me", "changeme", "change_me", "default", "token",
    "password", "123456", "admin", "secret", "your-token", "xxx",
}


def _resolve_mcp_token() -> str:
    """Return the MCP token; when it is missing or a weak default, generate one
    and persist it to a small sidecar file next to the project.

    Why not rewrite .env:
    this runs at import time. Rewriting .env here would mean the act of
    "importing a module" mutates the user's production config, and it would
    overwrite the previous token. I got burned by exactly this: a test process
    replaced MCP_TOKEN in .env with a fresh value while DSH was still configured
    with the old one -- the MCP connection dropped on the spot. Config files must
    never be silently changed by library code.

    Why not simply refuse to start: that turns "token not configured" into
    "service will not boot", which is a hard outage for deployments that are
    already running.
    """
    raw = (os.environ.get("MCP_TOKEN") or "").strip()
    if raw and raw.lower() not in _WEAK_TOKENS:
        return raw

    # Sidecar file: .env is left alone, yet the token survives a restart
    # (otherwise the MCP client would have to be reconfigured every single time)
    side = ROOT / "data" / ".mcp_token"
    try:
        if side.is_file():
            saved = side.read_text(encoding="utf-8").strip()
            if saved and saved.lower() not in _WEAK_TOKENS:
                return saved
    except OSError:
        pass

    new = secrets.token_urlsafe(32)
    try:
        side.parent.mkdir(parents=True, exist_ok=True)
        side.write_text(new, encoding="utf-8")
        logging.getLogger(__name__).warning(
            "MCP_TOKEN 没配或用了弱默认值；已生成随机令牌存到 %s。"
            "请把它填到 .env 的 MCP_TOKEN，并同步到 MCP 客户端。", side)
    except OSError:
        logging.getLogger(__name__).warning(
            "MCP_TOKEN 没配，随机生成了一份仅本次进程有效的令牌。")
    return new


def _bool(name: str, default: bool = False) -> bool:
    """Read a boolean env var; unset or empty falls back to *default*."""
    raw = (os.environ.get(name) or "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


def _int(name: str, default: int) -> int:
    """Read an int env var; a garbage value falls back to *default*, not an exception."""
    try:
        return int((os.environ.get(name) or "").strip() or default)
    except ValueError:
        return default


def admins() -> set:
    """Admins: allowed to run /stop /start /auto. Empty falls back to OWNER_IDS."""
    raw = os.environ.get("ADMIN_IDS") or ""
    return {x.strip() for x in raw.replace("，", ",").split(",") if x.strip()}


def owners() -> set:
    raw = os.environ.get("OWNER_IDS") or ""
    return {x.strip() for x in raw.replace("，", ",").split(",") if x.strip()}


class Config:
    def __init__(self):
        # Per-instance root directory. persist_permissions() used to read the
        # module-level ROOT directly, so overriding config.ROOT in a test could
        # not intercept it and it wrote test values into the repository's real
        # .env. Now only self.ROOT counts, so there is one place to change.
        self.ROOT = ROOT
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
        # Token shared by MCP and the console. **It must never have a well-known
        # default** -- this used to fall back to "change-me", which doubled as the
        # console session token and the MCP bearer: anyone could use it to change
        # OWNER_IDS / ALLOW_SEND / ALLOW_MANAGE, rewrite the LLM key, or dispatch
        # agents. When unset we mint a strong random value, persist it, and warn.
        self.mcp_token = _resolve_mcp_token()
        # Built-in model
        self.llm_api_base = (os.environ.get("LLM_API_BASE") or "https://api.deepseek.com").rstrip("/")
        self.llm_api_key = (os.environ.get("LLM_API_KEY") or os.environ.get("DEEPSEEK_API_KEY") or "").strip()
        self.llm_model = (os.environ.get("LLM_MODEL") or "deepseek-chat").strip()
        self.llm_max_tokens = _int("LLM_MAX_TOKENS", 2048)
        # Images posted in the group: fetched and fed to the vision model (only
        # really takes effect when the model name contains "vision")
        self.vision_enabled = _bool("VISION_ENABLED", True)
        # Links posted in the group: fetch the body first, then decide (the
        # chit-chat path has no tools, so links would otherwise always be ignored)
        self.link_preview = _bool("LINK_PREVIEW", True)
        # Run the vision model over an image before sending (safety, plus does it
        # match the intended use). On by default; turning it off means images
        # nobody vetted go straight into the group -- we have had an incident with
        # inappropriate images of minors before, do not turn this off.
        self.send_image_review = _bool("SEND_IMAGE_REVIEW", True)
        # Academic mode: on an academic/technical question, drop the group-chat
        # persona and lift the length cap
        self.academic_mode = _bool("ACADEMIC_MODE", True)
        self.llm_academic_max_tokens = _int("LLM_ACADEMIC_MAX_TOKENS", 8192)
        # Academic answers are not truncated to chat length (chat defaults to 800 chars)
        self.academic_reply_chars = _int("ACADEMIC_REPLY_CHARS", 6000)
        self.llm_thinking = (os.environ.get("LLM_THINKING") or "disabled").strip().lower()
        self.llm_reasoning_effort = (os.environ.get("LLM_REASONING_EFFORT") or "").strip().lower()
        self.llm_timeout = float((os.environ.get("LLM_TIMEOUT") or "60").strip() or 60)
        self.llm_temperature_raw = (os.environ.get("LLM_TEMPERATURE") or "1.0").strip()
        try:
            self.llm_temperature = float(self.llm_temperature_raw)
        except ValueError:
            self.llm_temperature = 1.0
        # System prompt: the file wins, so it can be hot-edited
        self.system_prompt_file = (os.environ.get("SYSTEM_PROMPT_FILE") or "data/system_prompt.md").strip()
        # Whether the bot decides for itself when to join in (off = MCP tools
        # only, never speaks unprompted)
        self.auto_reply = _bool("AUTO_REPLY", True)
        # Built-in Agent: lets the model read/write files and run commands
        # (replaces an external agent host)
        self.agent_enabled = _bool("AGENT_ENABLED", True)
        self.agent_root = (os.environ.get("AGENT_ROOT") or str(ROOT)).strip()
        # Tool-call cap for a single task. Deliberately loose: the real backstops
        # are autoreply's agent_timeout (90 seconds) and the loop detection in
        # agentloop, not this number.
        self.agent_max_steps = _int("AGENT_MAX_STEPS", 60)

        raw_watch = (os.environ.get("WATCH_GROUPS") or "").strip()
        self.watch_groups = {x.strip() for x in raw_watch.replace("，", ",").split(",") if x.strip()}
        self.data_dir = ROOT / "data"

    def persist_permissions(self):
        """Write the safety switches back to .env so they survive a restart."""
        path = Path(getattr(self, "ROOT", ROOT)) / ".env"
        try:
            lines = path.read_text(encoding="utf-8").splitlines() if path.is_file() else []
        except OSError:
            return False
        want = {
            "ALLOW_SEND": "true" if self.allow_send else "false",
            "ALLOW_MANAGE": "true" if self.allow_manage else "false",
            "OWNER_IDS": ",".join(sorted(self.owners)),
            "WATCH_GROUPS": ",".join(sorted(self.watch_groups)),
            "LLM_API_BASE": self.llm_api_base,
            "LLM_MODEL": self.llm_model,
            "AUTO_REPLY": "true" if self.auto_reply else "false",
        }
        # Merge line by line instead of rewriting the file: keys we own are
        # replaced in place, everything else (comments, ordering, the user's own
        # settings) is preserved byte for byte.
        seen = set()
        out = []
        for line in lines:
            key = line.split("=", 1)[0].strip() if "=" in line else ""
            if key in want:
                out.append(f"{key}={want.pop(key)}")
                seen.add(key)
            else:
                out.append(line)
        # Keys that were not present at all get appended at the end.
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
            "llm": {
                "api_base": self.llm_api_base,
                "model": self.llm_model,
                "has_key": bool(self.llm_api_key),
                "max_tokens": self.llm_max_tokens,
                "academic_mode": self.academic_mode,
                "academic_max_tokens": self.llm_academic_max_tokens,
                "vision_enabled": self.vision_enabled,
                "temperature": self.llm_temperature,
                "thinking": self.llm_thinking or "default",
            },
            "system_prompt_file": self.system_prompt_file,
            "auto_reply": self.auto_reply,
            "agent": {
                "enabled": self.agent_enabled,
                "root": self.agent_root,
                "max_steps": self.agent_max_steps,
            },
        }


config = Config()
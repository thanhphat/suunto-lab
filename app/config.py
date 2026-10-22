import os
from pathlib import Path


def _env(name: str, default: str | None = None) -> str | None:
    v = os.environ.get(name)
    return v if v not in (None, "") else default


DATABASE_URL = _env("DATABASE_URL", "postgresql://suunto:suunto@localhost:5432/suunto")
# Read-only connection used by the AI agent's query_sql tool.
AGENT_DB_PASSWORD = _env("AGENT_DB_PASSWORD", "agent_ro_change_me")
AGENT_DATABASE_URL = _env("AGENT_DATABASE_URL")  # derived from DATABASE_URL when unset

FIT_STORAGE_DIR = Path(_env("FIT_STORAGE_DIR", "./data/fit"))
MAX_UPLOAD_BYTES = int(_env("MAX_UPLOAD_BYTES", str(20 * 1024 * 1024)))

# Single shared secret for the web app, the upload API and the iOS Shortcut.
API_TOKEN = _env("API_TOKEN")
# Secret for the MCP endpoint. Clients send "Authorization: Bearer <MCP_TOKEN>",
# or use the secret URL /mcp/<MCP_TOKEN> where a client cannot set headers (claude.ai).
MCP_TOKEN = _env("MCP_TOKEN")
MCP_ALLOWED_HOSTS = [h.strip() for h in (_env("MCP_ALLOWED_HOSTS", "") or "").split(",") if h.strip()]

ATHLETE_NAME = _env("ATHLETE_NAME", "Me")
DEFAULT_TZ = _env("DEFAULT_TZ", "Asia/Ho_Chi_Minh")

WORKER_POLL_SECONDS = float(_env("WORKER_POLL_SECONDS", "2"))


def agent_database_url() -> str:
    if AGENT_DATABASE_URL:
        return AGENT_DATABASE_URL
    from urllib.parse import urlsplit, urlunsplit
    u = urlsplit(DATABASE_URL)
    host = u.hostname or "localhost"
    port = f":{u.port}" if u.port else ""
    return urlunsplit((u.scheme, f"agent_ro:{AGENT_DB_PASSWORD}@{host}{port}", u.path, u.query, ""))

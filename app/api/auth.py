"""The agent API's bearer token: DEEPVAC_AGENT_API_TOKEN, or generated once into
data/agent_api_token. Kept apart from server.py so the app can read it
without loading the server."""

import os
import secrets

import app.common as common

TOKEN_FILE = "agent_api_token"
DEFAULT_URL = "http://127.0.0.1:8765"


def api_token() -> str:
    token = os.environ.get("DEEPVAC_AGENT_API_TOKEN", "").strip()
    if token:
        return token
    path = common.DATA_DIR / TOKEN_FILE
    if path.exists():
        return path.read_text(encoding="utf-8").strip()
    path.parent.mkdir(parents=True, exist_ok=True)
    token = secrets.token_urlsafe(32)
    path.write_text(token, encoding="utf-8")
    return token

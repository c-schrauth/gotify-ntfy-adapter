import os
import json
import base64
from typing import Any, Dict, Optional

import requests
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse

app = FastAPI()


def read_secret(path: str) -> str:
    """Read a Docker secret from /run/secrets/... (or any file path)."""
    with open(path, "r", encoding="utf-8") as f:
        return f.read().strip()


# --- Config (ENV / Secrets) -------------------------------------------------

NTFY_SERVER = os.getenv("NTFY_SERVER", "").rstrip("/")
NTFY_USERNAME = os.getenv("NTFY_USERNAME", "")
NTFY_PASSWORD = os.getenv("NTFY_PASSWORD", "")

# Support *_FILE pattern for Docker/Portainer secrets
NTFY_PASSWORD_FILE = os.getenv("NTFY_PASSWORD_FILE", "")
if NTFY_PASSWORD_FILE:
    NTFY_PASSWORD = read_secret(NTFY_PASSWORD_FILE)

DEFAULT_TOPIC = os.getenv("DEFAULT_TOPIC", "monitoring-adapter-info")
DEFAULT_TAGS = os.getenv("DEFAULT_TAGS", "gotify,adapter")
TITLE_PREFIX = os.getenv("TITLE_PREFIX", "")

# Token->Topic map can come from SECRET file or from ENV JSON
TOKEN_TOPIC_MAP: Dict[str, str] = {}
TOKEN_TOPIC_MAP_FILE = os.getenv("TOKEN_TOPIC_MAP_FILE", "")
try:
    if TOKEN_TOPIC_MAP_FILE:
        TOKEN_TOPIC_MAP = json.loads(read_secret(TOKEN_TOPIC_MAP_FILE))
    else:
        TOKEN_TOPIC_MAP = json.loads(os.getenv("TOKEN_TOPIC_MAP", "{}"))
    if not isinstance(TOKEN_TOPIC_MAP, dict):
        TOKEN_TOPIC_MAP = {}
except Exception:
    TOKEN_TOPIC_MAP = {}

if not NTFY_SERVER:
    raise RuntimeError("NTFY_SERVER is required (e.g. https://ntfy.example.com)")


# --- Helpers ----------------------------------------------------------------

def gotify_priority_to_ntfy(p: Any) -> str:
    """
    Gotify priority is commonly 0..10 (but may be any int).
    ntfy priority is 1..5.
    """
    try:
        p_int = int(p)
    except Exception:
        return "3"

    if p_int <= 0:
        return "1"
    if p_int <= 2:
        return "2"
    if p_int <= 4:
        return "3"
    if p_int <= 7:
        return "4"
    return "5"


def ntfy_auth_header() -> Dict[str, str]:
    """Return Basic Auth header for ntfy if username is set."""
    if not NTFY_USERNAME:
        return {}
    token = base64.b64encode(f"{NTFY_USERNAME}:{NTFY_PASSWORD}".encode("utf-8")).decode("ascii")
    return {"Authorization": f"Basic {token}"}


def publish_to_ntfy(topic: str, title: str, message: str, priority: str, tags: str) -> None:
    url = f"{NTFY_SERVER}/{topic}"
    headers: Dict[str, str] = {
        "Priority": priority,
        "Tags": tags,
    }
    if title:
        headers["Title"] = title

    headers.update(ntfy_auth_header())

    try:
        r = requests.post(url, data=message.encode("utf-8"), headers=headers, timeout=10)
    except requests.RequestException as e:
        raise HTTPException(status_code=502, detail=f"Failed to reach ntfy: {e}")

    if r.status_code >= 400:
        raise HTTPException(status_code=502, detail=f"ntfy returned {r.status_code}: {r.text[:200]}")


# --- Endpoints --------------------------------------------------------------

@app.get("/health")
def health():
    return {"ok": True}


@app.post("/message")
async def gotify_message(request: Request):
    """
    Minimal Gotify-compatible endpoint:
      POST /message?token=<apptoken>

    Accepts:
      - JSON body: { "title": "...", "message": "...", "priority": 5, "extras": {...} }
      - Form body: title=...&message=...&priority=...

    Publishes to ntfy:
      - Topic determined by TOKEN_TOPIC_MAP[token] or DEFAULT_TOPIC
      - Title/Priority/Tags mapped to ntfy headers
    """
    token = request.query_params.get("token", "")
    topic = TOKEN_TOPIC_MAP.get(token, DEFAULT_TOPIC)

    content_type = (request.headers.get("content-type") or "").lower()

    title: str = ""
    message: str = ""
    priority_val: Any = None
    extras: Optional[Dict[str, Any]] = None

    if "application/json" in content_type:
        payload = await request.json()
        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail="Invalid JSON payload")

        title = str(payload.get("title") or "")
        message = str(payload.get("message") or "")
        priority_val = payload.get("priority", None)

        extras_val = payload.get("extras", None)
        extras = extras_val if isinstance(extras_val, dict) else None
    else:
        form = await request.form()
        title = str(form.get("title") or "")
        message = str(form.get("message") or "")
        priority_val = form.get("priority", None)
        extras = None

    if not message and not title:
        raise HTTPException(status_code=400, detail="Missing title/message")

    # Build ntfy publish parameters
    ntfy_priority = gotify_priority_to_ntfy(priority_val)
    tags = DEFAULT_TAGS

    # Best-effort: mark presence of extras
    if extras:
        tags = f"{tags},extras"

    full_title = f"{TITLE_PREFIX}{title}".strip() if title else TITLE_PREFIX.strip()

    publish_to_ntfy(
        topic=topic,
        title=full_title,
        message=message if message else title,
        priority=ntfy_priority,
        tags=tags,
    )

    # Minimal Gotify-like response
    return JSONResponse(
        {
            "id": 0,
            "appid": 0,
            "title": title,
            "message": message,
            "priority": int(priority_val) if str(priority_val).isdigit() else 0,
        }
    )

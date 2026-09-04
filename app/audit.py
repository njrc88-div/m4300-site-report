"""A minimal sign-in / sign-out audit log for the local-login gate (see
app/auth.py). Append-only JSON Lines file, one event per line - simple
to write, simple to tail by hand, simple to read back for the in-app
"Audit Log" view (see /api/audit in main.py, admin-only).

Only meaningful when auth.AUTH_ENABLED is True - with no login configured
there's no identity to attribute an event to, so callers should (and do)
skip logging entirely rather than record events with no real user behind
them.
"""
from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime, timezone
from pathlib import Path

from fastapi import Request

logger = logging.getLogger("m4300_report.audit")

AUDIT_LOG_PATH = Path(os.environ.get("AUDIT_LOG_PATH", "/srv/data/audit.jsonl"))
_lock = threading.Lock()


def record_event(event: str, *, username: str, request: Request) -> None:
    """event: "sign_in" | "sign_out" | "sign_in_denied"."""
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "event": event,
        "username": username,
        "ip": request.client.host if request.client else None,
    }
    try:
        AUDIT_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with _lock:
            with AUDIT_LOG_PATH.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry) + "\n")
    except OSError as exc:
        # An audit log that can't be written to shouldn't take the actual
        # sign-in/out flow down with it - log the failure and move on.
        logger.warning("Couldn't write audit event %r: %s", event, exc)


def read_events(limit: int = 500) -> list[dict]:
    """Most recent events first. Reads the whole file - fine at the
    scale a sign-in/out log for a small team actually reaches; would need
    revisiting long before this ever approaches a real problem."""
    if not AUDIT_LOG_PATH.exists():
        return []
    events: list[dict] = []
    with _lock:
        with AUDIT_LOG_PATH.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    events.reverse()
    return events[:limit]

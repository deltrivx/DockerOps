"""Docker engine events as JSON list or SSE stream."""

from __future__ import annotations

import json
import time
from typing import Any, Iterator

from docker_client import docker_events, get_client


def recent_events(limit: int = 50, since_seconds: int = 3600) -> dict[str, Any]:
    """Collect a bounded batch of recent events (non-streaming snapshot)."""
    limit = max(1, min(int(limit), 500))
    since = int(time.time()) - max(60, int(since_seconds))
    items: list[dict[str, Any]] = []
    try:
        # Non-follow: use until=now so the stream ends
        until = int(time.time())
        for ev in docker_events(since=since, until=until):
            items.append(_normalize_event(ev))
            if len(items) >= limit:
                break
    except Exception as e:
        return {"ok": False, "error": str(e), "items": items}

    # Prefer newest last for UI append style; reverse so newest first
    items = list(reversed(items[-limit:]))
    return {"ok": True, "count": len(items), "items": items, "since": since}


def sse_docker_events(filters: dict | None = None) -> Iterator[str]:
    try:
        for ev in docker_events(filters=filters):
            payload = json.dumps(_normalize_event(ev), ensure_ascii=False)
            yield f"data: {payload}\n\n"
    except Exception as e:
        err = json.dumps({"error": str(e)}, ensure_ascii=False)
        yield f"event: error\ndata: {err}\n\n"


def _normalize_event(ev: dict[str, Any]) -> dict[str, Any]:
    actor = ev.get("Actor") or {}
    attrs = actor.get("Attributes") or {}
    return {
        "time": ev.get("time") or ev.get("timeNano"),
        "type": ev.get("Type"),
        "action": ev.get("Action"),
        "id": (actor.get("ID") or "")[:12],
        "name": attrs.get("name") or attrs.get("image") or "",
        "from": ev.get("from") or attrs.get("image"),
        "status": ev.get("status"),
        "attributes": {
            k: attrs[k]
            for k in ("name", "image", "exitCode", "signal", "com.docker.compose.project")
            if k in attrs
        },
    }


def engine_ping_ok() -> bool:
    try:
        return bool(get_client().ping())
    except Exception:
        return False

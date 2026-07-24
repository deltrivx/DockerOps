"""Container log helpers: plain text + SSE follow."""

from __future__ import annotations

import json
from typing import Any, Iterator

from docker_client import container_logs, container_logs_stream


def get_logs(
    container_id: str,
    *,
    tail: int = 200,
    timestamps: bool = True,
    since: int | None = None,
) -> dict[str, Any]:
    text = container_logs(container_id, tail=tail, timestamps=timestamps, since=since)
    lines = text.splitlines()
    return {
        "ok": True,
        "container_id": container_id,
        "tail": tail,
        "line_count": len(lines),
        "logs": text,
    }


def sse_log_events(
    container_id: str,
    *,
    tail: int = 100,
    timestamps: bool = True,
) -> Iterator[str]:
    """Yield Server-Sent Event frames for live logs."""
    try:
        for chunk in container_logs_stream(container_id, tail=tail, timestamps=timestamps):
            # Split multi-line chunks so SSE stays readable
            for line in chunk.splitlines() or [chunk]:
                payload = json.dumps({"line": line}, ensure_ascii=False)
                yield f"data: {payload}\n\n"
    except Exception as e:
        err = json.dumps({"error": str(e)}, ensure_ascii=False)
        yield f"event: error\ndata: {err}\n\n"

from __future__ import annotations

import time
from typing import Any

from db import add_monitor_snapshot, latest_monitor_snapshot
from doctor import diagnose_all
from docker_client import list_containers, ping


def collect_report(persist: bool = True) -> dict[str, Any]:
    engine = ping()
    containers: list[dict[str, Any]] = []
    if engine.get("ok"):
        try:
            containers = list_containers(all_containers=True)
        except Exception as e:
            containers = []
            engine = {**engine, "list_error": str(e)}

    diagnosis = diagnose_all()
    running = sum(1 for c in containers if (c.get("status") or "").lower() == "running")
    unhealthy = sum(1 for c in containers if (c.get("health") or "").lower() == "unhealthy")
    exited = sum(1 for c in containers if (c.get("status") or "").lower() in ("exited", "dead"))

    report = {
        "generated_at": time.time(),
        "engine": engine,
        "summary": {
            "total": len(containers),
            "running": running,
            "exited": exited,
            "unhealthy": unhealthy,
            "health_score": diagnosis.get("health_score"),
            "label": diagnosis.get("label"),
        },
        "containers": containers,
        "top_findings": (diagnosis.get("findings") or [])[:20],
        "advice": diagnosis.get("advice") or [],
    }

    if persist:
        snap = add_monitor_snapshot(report, int(diagnosis.get("health_score") or 0))
        report["snapshot_id"] = snap["id"]
    return report


def get_latest_or_collect() -> dict[str, Any]:
    latest = latest_monitor_snapshot()
    if latest and latest.get("payload"):
        # refresh if older than 5 minutes
        if time.time() - float(latest.get("created_at") or 0) < 300:
            payload = latest["payload"]
            if isinstance(payload, dict):
                payload = {**payload, "snapshot_id": latest["id"], "cached": True}
                return payload
    return collect_report(persist=True)

"""
Multi Docker endpoint helpers: capabilities, host validation, connection test.
"""
from __future__ import annotations

import re
from typing import Any

from config import get_settings

_HOST_RE = re.compile(
    r"^(unix://.+|tcp://[\w.\-\[\]]+:\d+|npipe:.+|ssh://.+)$",
    re.IGNORECASE,
)


def is_local_host(docker_host: str) -> bool:
    h = (docker_host or "").strip().lower()
    return h.startswith("unix://") or h.startswith("npipe:")


def validate_docker_host(docker_host: str) -> str:
    h = (docker_host or "").strip()
    if not h:
        raise ValueError("docker_host 不能为空")
    if not _HOST_RE.match(h):
        raise ValueError(
            "docker_host 格式无效，示例：unix:///var/run/docker.sock 或 tcp://192.168.1.10:2375"
        )
    return h


def endpoint_capabilities(ep: dict[str, Any] | None) -> dict[str, bool]:
    """Feature matrix for UI/API gating."""
    settings = get_settings()
    local = is_local_host((ep or {}).get("docker_host") or "")
    return {
        "local": local,
        "resources": bool(settings.resource_apis),
        "console": bool(settings.console_enabled),
        "compose": bool(settings.compose_enabled) and local,
        "unraid": bool(settings.unraid_enabled) and local,
        "update_detect": True,
    }


def public_endpoint(ep: dict[str, Any], *, active_id: str | None = None) -> dict[str, Any]:
    """Strip secrets for API responses (keep has_tls_key flags)."""
    caps = endpoint_capabilities(ep)
    return {
        "id": ep["id"],
        "name": ep["name"],
        "docker_host": ep["docker_host"],
        "tls_enabled": bool(ep.get("tls_enabled")),
        "verify_tls": bool(ep.get("verify_tls", True)),
        "has_tls_ca": bool((ep.get("tls_ca") or "").strip()),
        "has_tls_cert": bool((ep.get("tls_cert") or "").strip()),
        "has_tls_key": bool((ep.get("tls_key") or "").strip()),
        "is_default": bool(ep.get("is_default")),
        "enabled": bool(ep.get("enabled", True)),
        "notes": ep.get("notes") or "",
        "is_local": caps["local"],
        "capabilities": caps,
        "is_active": bool(active_id and ep["id"] == active_id),
        "created_at": ep.get("created_at"),
        "updated_at": ep.get("updated_at"),
    }


def require_local_endpoint(ep: dict[str, Any], feature: str) -> None:
    if not is_local_host(ep.get("docker_host") or ""):
        raise PermissionError(
            f"{feature} 仅支持本地 unix/npipe 端点；请切换到本机端点后再试。"
        )

"""Device model: one record per managed Docker host.

DockerOps grew two parallel notions of "a remote thing" — the ``endpoints``
table (a Docker API URL) and remote-agent sessions (an outbound dial). They
answered different questions and neither could express the thing the user
actually means, which is simply *this machine*. Every feature then had to know
about both shapes, and features that only made sense on one of them silently
degraded on the other.

This module collapses both into a single device record with an explicit
``connection`` type:

``local``
    DockerOps runs on the host. Nothing to configure.
``docker``
    A Docker API URL (``unix://``/``tcp://``/``ssh://``/``npipe:``) that the
    Docker SDK can talk to directly.
``ssh``
    An authenticated shell on the host. The engine is still reached through the
    mounted socket or an SSH-tunnelled one, but the important extra is that
    host-side scripts become reachable — which is what Unraid's template
    mechanism requires.
``agent``
    A DockerOps peer dialled over the remote-agent channel.

Platform (``unraid``/``fnos``/``generic``) is *detected*, never asked for: the
user should not have to know which of three connection styles applies to their
box before they can connect. Detection answers "what is this machine", the
connection type answers "how do we reach it", and the two are independent.
"""
from __future__ import annotations

import json
from typing import Any

from db import create_endpoint, delete_endpoint, get_endpoint, list_endpoints, update_endpoint

CONNECTION_TYPES = ("local", "docker", "ssh", "agent")
PLATFORMS = ("unraid", "fnos", "generic")

# Which platforms actually have a template mechanism worth surfacing. Only
# Unraid ships container templates as a first-class concept; FnOS and generic
# hosts are managed through compose or plain lifecycle calls.
TEMPLATE_PLATFORMS = ("unraid",)


def normalize_platform(value: str | None) -> str:
    v = (value or "").strip().lower()
    if v in PLATFORMS:
        return v
    # A few spelling variants that show up in practice.
    if v in {"unraid-os", "unraidos", "limetech"}:
        return "unraid"
    if v in {"fnos", "feiniu", "飞牛"}:
        return "fnos"
    return "generic"


def normalize_connection(value: str | None, *, docker_host: str = "") -> str:
    v = (value or "").strip().lower()
    if v in CONNECTION_TYPES:
        return v
    # Infer from a docker_host when the caller did not say explicitly.
    h = (docker_host or "").strip().lower()
    if h.startswith("ssh://"):
        return "ssh"
    if h.startswith("remote://"):
        return "agent"
    if h.startswith("unix://") or h.startswith("npipe:"):
        return "local"
    if h:
        return "docker"
    return "local"


def platform_capabilities(platform: str, connection: str) -> dict[str, bool]:
    """What this device can actually do, given platform *and* connection.

    Both axes matter. Unraid template rebuilds need the template scripts, which
    only exist on an Unraid host *and* are only reachable when there is a shell
    channel — a bare ``tcp://`` Docker URL can reach the engine but never the
    scripts, so advertising template rebuild for it would be a lie the user
    discovers by clicking.
    """
    platform = normalize_platform(platform)
    connection = normalize_connection(connection)
    has_shell = connection in {"local", "ssh"}

    return {
        "lifecycle": True,
        "logs": True,
        "images": True,
        "networks": True,
        "volumes": True,
        "system": True,
        "console": has_shell,
        "compose": True,
        # Compose projects can be *rebuilt* only where the project directory is
        # writable; discovery works everywhere. Reported per project too.
        "compose_manage": has_shell,
        "unraid_templates": platform in TEMPLATE_PLATFORMS,
        # The distinction that motivates this whole module: template-driven
        # rebuild needs Unraid + a shell channel.
        "template_rebuild": platform in TEMPLATE_PLATFORMS and has_shell,
        "host_scripts": has_shell,
        "update_detect": True,
    }


def public_device(ep: dict[str, Any], *, active_id: str | None = None) -> dict[str, Any]:
    """Serialise a device for the API, never leaking credentials."""
    connection = normalize_connection(
        ep.get("connection"), docker_host=ep.get("docker_host") or ""
    )
    platform = normalize_platform(ep.get("platform"))
    caps = platform_capabilities(platform, connection)
    return {
        "id": ep["id"],
        "name": ep.get("name") or "",
        "connection": connection,
        "platform": platform,
        # A detected-but-unconfirmed platform is shown differently so the user
        # knows the label came from probing rather than from them.
        "platform_detected": bool(ep.get("platform_detected")),
        "docker_host": ep.get("docker_host") or "",
        "address": ep.get("address") or "",
        "ssh_user": ep.get("ssh_user") or "",
        "has_ssh_password": bool((ep.get("ssh_password") or "").strip()),
        "has_ssh_key": bool((ep.get("ssh_key") or "").strip()),
        "tls_enabled": bool(ep.get("tls_enabled")),
        "verify_tls": bool(ep.get("verify_tls", True)),
        "has_tls_ca": bool((ep.get("tls_ca") or "").strip()),
        "has_tls_cert": bool((ep.get("tls_cert") or "").strip()),
        "has_tls_key": bool((ep.get("tls_key") or "").strip()),
        "is_default": bool(ep.get("is_default")),
        "enabled": bool(ep.get("enabled", True)),
        "notes": ep.get("notes") or "",
        "is_local": connection == "local",
        "is_active": bool(active_id and ep["id"] == active_id),
        "capabilities": caps,
        "created_at": ep.get("created_at"),
        "updated_at": ep.get("updated_at"),
        "last_probe": _safe_json(ep.get("last_probe")),
    }


def _safe_json(value: Any) -> Any:
    if not value:
        return None
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except Exception:
        return None


def device_connection_summary(devices: list[dict[str, Any]]) -> dict[str, Any]:
    """Group devices by connection type for the connect screen.

    The UI shows one section per connection type and hides the others once a
    device is active, so it needs the grouping precomputed rather than
    re-deriving it from a flat list on every render.
    """
    groups: dict[str, list[dict[str, Any]]] = {t: [] for t in CONNECTION_TYPES}
    for d in devices:
        groups.setdefault(d["connection"], []).append(d)
    return {
        "groups": {k: v for k, v in groups.items() if v},
        "counts": {k: len(v) for k, v in groups.items()},
        "total": len(devices),
    }


def find_device(device_id: str) -> dict[str, Any] | None:
    return get_endpoint(device_id)


def upsert_probe_result(device_id: str, probe: dict[str, Any]) -> dict[str, Any]:
    """Persist what a connection probe learned about a device.

    Called after a successful connect so the platform label and capabilities
    survive a restart instead of being re-probed on every page load.
    """
    platform = normalize_platform(probe.get("platform"))
    patch: dict[str, Any] = {
        "platform": platform,
        "platform_detected": True,
        "last_probe": json.dumps(
            {
                "platform": platform,
                "hostname": probe.get("hostname") or "",
                "os": probe.get("os") or "",
                "kernel": probe.get("kernel") or "",
                "docker": probe.get("docker") or "",
                "unraid_scripts": bool(probe.get("unraid_scripts")),
            },
            ensure_ascii=False,
        ),
    }
    return update_endpoint(device_id, **patch)


__all__ = [
    "CONNECTION_TYPES",
    "PLATFORMS",
    "TEMPLATE_PLATFORMS",
    "create_endpoint",
    "delete_endpoint",
    "device_connection_summary",
    "find_device",
    "list_endpoints",
    "normalize_connection",
    "normalize_platform",
    "platform_capabilities",
    "public_device",
    "update_endpoint",
    "upsert_probe_result",
]

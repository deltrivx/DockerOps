from __future__ import annotations

from typing import Any

import docker
from docker.errors import DockerException, NotFound

from config import get_settings

_client: docker.DockerClient | None = None


def get_client() -> docker.DockerClient:
    global _client
    if _client is None:
        settings = get_settings()
        _client = docker.DockerClient(base_url=settings.docker_host)
    return _client


def ping() -> dict[str, Any]:
    try:
        c = get_client()
        ok = c.ping()
        version = c.version()
        return {
            "ok": bool(ok),
            "api_version": version.get("ApiVersion"),
            "engine_version": version.get("Version"),
            "os": version.get("Os"),
            "arch": version.get("Arch"),
        }
    except DockerException as e:
        return {"ok": False, "error": str(e)}


def list_containers(all_containers: bool = True) -> list[dict[str, Any]]:
    c = get_client()
    items = c.containers.list(all=all_containers)
    return [_summarize(x) for x in items]


def get_container(container_id: str) -> dict[str, Any]:
    c = get_client()
    try:
        cont = c.containers.get(container_id)
    except NotFound:
        raise KeyError(container_id) from None
    return _detail(cont)


def _summarize(cont) -> dict[str, Any]:
    attrs = cont.attrs or {}
    state = attrs.get("State") or {}
    config = attrs.get("Config") or {}
    host = attrs.get("HostConfig") or {}
    labels = config.get("Labels") or {}
    health = (state.get("Health") or {}).get("Status")
    restart_count = state.get("RestartCount", 0)
    return {
        "id": cont.short_id,
        "name": cont.name,
        "image": _image_name(config),
        "status": cont.status,
        "state": state.get("Status") or cont.status,
        "health": health,
        "restart_count": restart_count,
        "created": attrs.get("Created"),
        "ports": attrs.get("NetworkSettings", {}).get("Ports") or {},
        "labels": labels,
        "restart_policy": (host.get("RestartPolicy") or {}).get("Name"),
    }


def _detail(cont) -> dict[str, Any]:
    base = _summarize(cont)
    attrs = cont.attrs or {}
    state = attrs.get("State") or {}
    host = attrs.get("HostConfig") or {}
    mounts = attrs.get("Mounts") or []
    networks = (attrs.get("NetworkSettings") or {}).get("Networks") or {}
    base.update(
        {
            "full_id": cont.id,
            "command": (attrs.get("Config") or {}).get("Cmd"),
            "env": _safe_env((attrs.get("Config") or {}).get("Env") or []),
            "mounts": [
                {
                    "type": m.get("Type"),
                    "source": m.get("Source"),
                    "destination": m.get("Destination"),
                    "rw": m.get("RW"),
                }
                for m in mounts
            ],
            "networks": list(networks.keys()),
            "started_at": state.get("StartedAt"),
            "finished_at": state.get("FinishedAt"),
            "oom_killed": state.get("OOMKilled"),
            "exit_code": state.get("ExitCode"),
            "memory_limit": host.get("Memory"),
            "nano_cpus": host.get("NanoCpus"),
            "privileged": host.get("Privileged"),
        }
    )
    # best-effort stats (may fail if not running)
    try:
        if cont.status == "running":
            stats = cont.stats(stream=False)
            base["stats"] = _parse_stats(stats)
    except Exception as e:
        base["stats_error"] = str(e)
    return base


def _image_name(config: dict) -> str:
    img = config.get("Image") or ""
    return img


def _safe_env(env_list: list[str]) -> list[str]:
    sensitive = ("PASSWORD", "SECRET", "TOKEN", "KEY", "PASSWD", "CREDENTIAL")
    out = []
    for item in env_list:
        if "=" not in item:
            out.append(item)
            continue
        k, v = item.split("=", 1)
        if any(s in k.upper() for s in sensitive):
            out.append(f"{k}=***")
        else:
            out.append(item)
    return out


def _parse_stats(stats: dict) -> dict[str, Any]:
    cpu = 0.0
    try:
        cpu_delta = stats["cpu_stats"]["cpu_usage"]["total_usage"] - stats["precpu_stats"]["cpu_usage"]["total_usage"]
        system_delta = stats["cpu_stats"]["system_cpu_usage"] - stats["precpu_stats"]["system_cpu_usage"]
        online = stats["cpu_stats"].get("online_cpus") or len(
            stats["cpu_stats"]["cpu_usage"].get("percpu_usage") or [1]
        )
        if system_delta > 0 and cpu_delta > 0:
            cpu = (cpu_delta / system_delta) * online * 100.0
    except Exception:
        cpu = 0.0

    mem_usage = 0
    mem_limit = 0
    try:
        mem_usage = stats["memory_stats"].get("usage") or 0
        mem_limit = stats["memory_stats"].get("limit") or 0
    except Exception:
        pass

    return {
        "cpu_percent": round(cpu, 2),
        "memory_usage": mem_usage,
        "memory_limit": mem_limit,
        "memory_percent": round((mem_usage / mem_limit) * 100, 2) if mem_limit else 0.0,
    }


def pull_image(image: str) -> dict[str, Any]:
    c = get_client()
    # stream pull and collect last status lines
    lines: list[str] = []
    for chunk in c.api.pull(image, stream=True, decode=True):
        status = chunk.get("status") or ""
        if status:
            lines.append(status)
    return {"image": image, "log_tail": lines[-20:]}

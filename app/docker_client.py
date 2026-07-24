from __future__ import annotations

from typing import Any

import docker
from docker.errors import DockerException, NotFound

from config import get_settings
from manager import classify_container, list_unraid_template_names

_client: docker.DockerClient | None = None
_template_names_cache: set[str] | None = None


def get_client() -> docker.DockerClient:
    global _client
    if _client is None:
        settings = get_settings()
        _client = docker.DockerClient(base_url=settings.docker_host)
    return _client


def refresh_template_name_cache() -> set[str]:
    global _template_names_cache
    _template_names_cache = list_unraid_template_names()
    return _template_names_cache


def _template_names() -> set[str]:
    global _template_names_cache
    if _template_names_cache is None:
        return refresh_template_name_cache()
    return _template_names_cache


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
    names = _template_names()
    return [_summarize(x, names) for x in items]


def get_container(container_id: str) -> dict[str, Any]:
    c = get_client()
    try:
        cont = c.containers.get(container_id)
    except NotFound:
        raise KeyError(container_id) from None
    return _detail(cont, _template_names())


def remove_container(container_id: str, force: bool = True) -> None:
    c = get_client()
    cont = c.containers.get(container_id)
    cont.remove(force=force)


def stop_container(container_id: str, timeout: int = 20) -> None:
    c = get_client()
    cont = c.containers.get(container_id)
    if cont.status == "running":
        cont.stop(timeout=timeout)


def create_and_start(run_kwargs: dict[str, Any], start: bool = True) -> dict[str, Any]:
    """Create container from Unraid-template kwargs (docker-py run/create)."""
    c = get_client()
    kwargs = dict(run_kwargs)
    name = kwargs.pop("name", None)
    image = kwargs.pop("image")
    kwargs.pop("detach", None)

    # Normalize network: docker-py prefers network= for named nets, network_mode for host/none/container:
    network = kwargs.pop("network", None)
    network_mode = kwargs.pop("network_mode", None)
    if network_mode:
        kwargs["network_mode"] = network_mode
    elif network and network not in ("bridge",):
        kwargs["network"] = network

    if start:
        cont = c.containers.run(image, name=name, detach=True, **kwargs)
    else:
        cont = c.containers.create(image, name=name, **kwargs)
    cont.reload()
    return _detail(cont, _template_names())


def connect_network(container_id: str, network: str) -> None:
    c = get_client()
    net = c.networks.get(network)
    net.connect(container_id)


def _summarize(cont, unraid_names: set[str] | None = None) -> dict[str, Any]:
    attrs = cont.attrs or {}
    state = attrs.get("State") or {}
    config = attrs.get("Config") or {}
    host = attrs.get("HostConfig") or {}
    labels = config.get("Labels") or {}
    health = (state.get("Health") or {}).get("Status")
    restart_count = state.get("RestartCount", 0)
    name = cont.name
    cls = classify_container(labels, name, unraid_template_names=unraid_names)
    return {
        "id": cont.short_id,
        "name": name,
        "image": _image_name(config),
        "status": cont.status,
        "state": state.get("Status") or cont.status,
        "health": health,
        "restart_count": restart_count,
        "created": attrs.get("Created"),
        "ports": attrs.get("NetworkSettings", {}).get("Ports") or {},
        "labels": labels,
        "restart_policy": (host.get("RestartPolicy") or {}).get("Name"),
        **cls,
    }


def _detail(cont, unraid_names: set[str] | None = None) -> dict[str, Any]:
    base = _summarize(cont, unraid_names)
    attrs = cont.attrs or {}
    state = attrs.get("State") or {}
    host = attrs.get("HostConfig") or {}
    mounts = attrs.get("Mounts") or []
    networks = (attrs.get("NetworkSettings") or {}).get("Networks") or {}
    base.update(
        {
            "full_id": cont.id,
            "command": (attrs.get("Config") or {}).get("Cmd"),
            "entrypoint": (attrs.get("Config") or {}).get("Entrypoint"),
            "env": _safe_env((attrs.get("Config") or {}).get("Env") or []),
            "env_raw": (attrs.get("Config") or {}).get("Env") or [],
            "mounts": [
                {
                    "type": m.get("Type"),
                    "source": m.get("Source"),
                    "destination": m.get("Destination"),
                    "rw": m.get("RW"),
                    "mode": m.get("Mode"),
                }
                for m in mounts
            ],
            "networks": list(networks.keys()),
            "network_details": {
                k: {
                    "ip": (v or {}).get("IPAddress"),
                    "gateway": (v or {}).get("Gateway"),
                    "mac": (v or {}).get("MacAddress"),
                }
                for k, v in networks.items()
            },
            "started_at": state.get("StartedAt"),
            "finished_at": state.get("FinishedAt"),
            "oom_killed": state.get("OOMKilled"),
            "exit_code": state.get("ExitCode"),
            "memory_limit": host.get("Memory"),
            "nano_cpus": host.get("NanoCpus"),
            "privileged": host.get("Privileged"),
            "devices": host.get("Devices") or [],
            "binds": host.get("Binds") or [],
            "port_bindings": (host.get("PortBindings") or {}),
            "restart_policy_full": host.get("RestartPolicy") or {},
            "extra_hosts": host.get("ExtraHosts") or [],
            "cap_add": host.get("CapAdd") or [],
            "cap_drop": host.get("CapDrop") or [],
            "runtime_host_config": {
                "NetworkMode": host.get("NetworkMode"),
                "PidMode": host.get("PidMode"),
                "IpcMode": host.get("IpcMode"),
                "PublishAllPorts": host.get("PublishAllPorts"),
            },
        }
    )
    try:
        if cont.status == "running":
            stats = cont.stats(stream=False)
            base["stats"] = _parse_stats(stats)
    except Exception as e:
        base["stats_error"] = str(e)
    return base


def _image_name(config: dict) -> str:
    return config.get("Image") or ""


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
        cpu_delta = (
            stats["cpu_stats"]["cpu_usage"]["total_usage"]
            - stats["precpu_stats"]["cpu_usage"]["total_usage"]
        )
        system_delta = (
            stats["cpu_stats"]["system_cpu_usage"] - stats["precpu_stats"]["system_cpu_usage"]
        )
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
    lines: list[str] = []
    for chunk in c.api.pull(image, stream=True, decode=True):
        status = chunk.get("status") or ""
        if status:
            lines.append(status)
    return {"image": image, "log_tail": lines[-20:]}

from __future__ import annotations

import contextvars
import os
import tempfile
import threading
from pathlib import Path
from typing import Any

import docker
from docker.errors import DockerException, NotFound
from docker.tls import TLSConfig

from config import get_settings
from manager import classify_container, list_unraid_template_names

# Request-scoped endpoint override (set by middleware / WS handlers)
_endpoint_ctx: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "dockerops_endpoint_id", default=None
)

_clients: dict[str, docker.DockerClient] = {}
_clients_lock = threading.Lock()
_template_names_cache: set[str] | None = None
# temp files for PEM material (tls) keyed by endpoint id
_tls_tmp: dict[str, list[str]] = {}


def set_request_endpoint(endpoint_id: str | None) -> contextvars.Token:
    return _endpoint_ctx.set(endpoint_id)


def reset_request_endpoint(token: contextvars.Token) -> None:
    _endpoint_ctx.reset(token)


def get_request_endpoint() -> str | None:
    return _endpoint_ctx.get()


def _write_pem_tmp(prefix: str, content: str) -> str:
    fd, path = tempfile.mkstemp(prefix=f"dockerops-{prefix}-", suffix=".pem")
    with os.fdopen(fd, "w") as f:
        f.write(content)
    try:
        os.chmod(path, 0o600)
    except Exception:
        pass
    return path


def _build_tls(ep: dict[str, Any]) -> TLSConfig | bool | None:
    if not ep.get("tls_enabled"):
        return None
    ca = (ep.get("tls_ca") or "").strip()
    cert = (ep.get("tls_cert") or "").strip()
    key = (ep.get("tls_key") or "").strip()
    verify = bool(ep.get("verify_tls", True))
    # Paths on disk
    ca_path = ca if ca and Path(ca).is_file() else None
    cert_path = cert if cert and Path(cert).is_file() else None
    key_path = key if key and Path(key).is_file() else None
    # Or PEM body → temp files
    tmp_paths: list[str] = []
    try:
        if ca and not ca_path and "BEGIN" in ca:
            ca_path = _write_pem_tmp("ca", ca)
            tmp_paths.append(ca_path)
        if cert and not cert_path and "BEGIN" in cert:
            cert_path = _write_pem_tmp("cert", cert)
            tmp_paths.append(cert_path)
        if key and not key_path and "BEGIN" in key:
            key_path = _write_pem_tmp("key", key)
            tmp_paths.append(key_path)
        if tmp_paths:
            _tls_tmp[ep["id"]] = tmp_paths
        if not ca_path and not cert_path:
            # tls enabled but no material: still request TLS (daemon may use system CA)
            return True if verify else TLSConfig(verify=False)
        client_cert = None
        if cert_path and key_path:
            client_cert = (cert_path, key_path)
        return TLSConfig(
            client_cert=client_cert,
            ca_cert=ca_path,
            verify=verify if ca_path or verify else False,
        )
    except Exception:
        for p in tmp_paths:
            try:
                os.unlink(p)
            except Exception:
                pass
        raise


def _resolve_endpoint_id(endpoint_id: str | None) -> str:
    if endpoint_id:
        return endpoint_id
    ctx = _endpoint_ctx.get()
    if ctx:
        return ctx
    # late import to avoid circular at module load
    from db import get_active_endpoint_id

    return get_active_endpoint_id()


def _load_endpoint(endpoint_id: str) -> dict[str, Any]:
    from db import get_endpoint, get_default_endpoint, ensure_default_endpoint

    ensure_default_endpoint()
    ep = get_endpoint(endpoint_id)
    if not ep:
        ep = get_default_endpoint()
    if not ep:
        settings = get_settings()
        return {
            "id": "env",
            "name": "本机",
            "docker_host": settings.docker_host,
            "tls_enabled": False,
            "tls_ca": "",
            "tls_cert": "",
            "tls_key": "",
            "verify_tls": True,
            "enabled": True,
        }
    return ep


def invalidate_client(endpoint_id: str | None = None) -> None:
    """Close and drop cached client(s)."""
    with _clients_lock:
        if endpoint_id is None:
            ids = list(_clients.keys())
        else:
            ids = [endpoint_id] if endpoint_id in _clients else []
        for eid in ids:
            c = _clients.pop(eid, None)
            if c is not None:
                try:
                    c.close()
                except Exception:
                    pass
            for p in _tls_tmp.pop(eid, []):
                try:
                    os.unlink(p)
                except Exception:
                    pass


def get_client(endpoint_id: str | None = None) -> docker.DockerClient:
    """
    Return a Docker client for the given endpoint (or request-scoped / active).
    Clients are cached per endpoint id.
    """
    eid = _resolve_endpoint_id(endpoint_id)
    with _clients_lock:
        existing = _clients.get(eid)
        if existing is not None:
            return existing
        ep = _load_endpoint(eid)
        # if id was missing, cache under resolved id
        eid = ep.get("id") or eid
        if eid in _clients:
            return _clients[eid]
        base_url = ep.get("docker_host") or get_settings().docker_host
        tls = _build_tls(ep)
        kwargs: dict[str, Any] = {"base_url": base_url}
        if tls is not None:
            kwargs["tls"] = tls
        client = docker.DockerClient(**kwargs)
        _clients[eid] = client
        return client


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


def connect_network(container_id: str, network: str, aliases: list[str] | None = None) -> dict[str, Any]:
    c = get_client()
    net = c.networks.get(network)
    kwargs: dict[str, Any] = {}
    if aliases:
        kwargs["aliases"] = aliases
    net.connect(container_id, **kwargs)
    return {"ok": True, "container": container_id, "network": network, "action": "connect"}


def disconnect_network(container_id: str, network: str, force: bool = False) -> dict[str, Any]:
    c = get_client()
    net = c.networks.get(network)
    net.disconnect(container_id, force=force)
    return {"ok": True, "container": container_id, "network": network, "action": "disconnect"}


def exec_create(
    container_id: str,
    *,
    cmd: list[str] | str,
    user: str = "",
    tty: bool = True,
    stdin: bool = True,
    stdout: bool = True,
    stderr: bool = True,
    env: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Create an interactive exec instance (docker exec). Returns {Id: ...}."""
    cont = _get_cont(container_id)
    if cont.status not in ("running", "paused"):
        raise RuntimeError(f"容器未运行（status={cont.status}），无法打开终端")
    c = get_client()
    kwargs: dict[str, Any] = {
        "container": cont.id,
        "cmd": cmd,
        "stdout": stdout,
        "stderr": stderr,
        "stdin": stdin,
        "tty": tty,
        "privileged": False,
    }
    if user:
        kwargs["user"] = user
    if env:
        kwargs["environment"] = env
    return c.api.exec_create(**kwargs)


def exec_resize(exec_id: str, height: int = 24, width: int = 80) -> None:
    c = get_client()
    c.api.exec_resize(exec_id, height=max(1, int(height)), width=max(1, int(width)))


def exec_start_socket(exec_id: str):
    """
    Start exec with socket=True for bidirectional TTY I/O.
    Returns (wrapper, raw_socket) where raw_socket supports recv/sendall.
    """
    c = get_client()
    sock = c.api.exec_start(exec_id, tty=True, socket=True, demux=False)
    raw = sock
    for attr in ("_sock", "sock", "socket"):
        inner = getattr(sock, attr, None)
        if inner is not None and hasattr(inner, "recv"):
            raw = inner
            break
    return sock, raw


def resolve_shell_cmd(shell: str | None = None) -> list[str]:
    """Map shell preference to exec command list."""
    s = (shell or "sh").strip().lower()
    mapping = {
        "sh": ["/bin/sh", "-l"],
        "bash": ["/bin/bash", "-l"],
        "ash": ["/bin/ash", "-l"],
        "zsh": ["/bin/zsh", "-l"],
    }
    if s in mapping:
        return mapping[s]
    # custom absolute path
    if s.startswith("/"):
        return [s]
    return ["/bin/sh", "-l"]


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


def pull_image_stream(image: str):
    """
    Yield docker pull progress chunks (decoded dicts from Engine API).
    Caller may collect status lines or forward to SSE.
    """
    c = get_client()
    for chunk in c.api.pull(image, stream=True, decode=True):
        if isinstance(chunk, dict):
            yield chunk
        else:
            yield {"status": str(chunk)}


def pull_image(image: str, on_progress=None) -> dict[str, Any]:
    """
    Pull image; optionally call on_progress(chunk_dict) for each stream frame.
    Returns {image, log_tail}.
    """
    lines: list[str] = []
    for chunk in pull_image_stream(image):
        if callable(on_progress):
            try:
                on_progress(chunk)
            except Exception:
                pass
        status = chunk.get("status") or ""
        if status:
            lines.append(status)
        err = chunk.get("error") or chunk.get("errorDetail", {}).get("message")
        if err:
            raise RuntimeError(str(err))
    return {"image": image, "log_tail": lines[-20:]}


# ── Lifecycle ──────────────────────────────────────────────


def _get_cont(container_id: str):
    c = get_client()
    try:
        return c.containers.get(container_id)
    except NotFound as e:
        raise KeyError(container_id) from e


def start_container(container_id: str) -> dict[str, Any]:
    cont = _get_cont(container_id)
    if cont.status != "running":
        cont.start()
    cont.reload()
    return _summarize(cont, _template_names())


def restart_container(container_id: str, timeout: int = 20) -> dict[str, Any]:
    cont = _get_cont(container_id)
    cont.restart(timeout=timeout)
    cont.reload()
    return _summarize(cont, _template_names())


def pause_container(container_id: str) -> dict[str, Any]:
    cont = _get_cont(container_id)
    cont.pause()
    cont.reload()
    return _summarize(cont, _template_names())


def unpause_container(container_id: str) -> dict[str, Any]:
    cont = _get_cont(container_id)
    cont.unpause()
    cont.reload()
    return _summarize(cont, _template_names())


def kill_container(container_id: str, signal: str = "SIGKILL") -> dict[str, Any]:
    cont = _get_cont(container_id)
    cont.kill(signal=signal)
    cont.reload()
    return _summarize(cont, _template_names())


def container_action_stop(container_id: str, timeout: int = 20) -> dict[str, Any]:
    cont = _get_cont(container_id)
    if cont.status == "running":
        cont.stop(timeout=timeout)
    cont.reload()
    return _summarize(cont, _template_names())


def rename_container(container_id: str, new_name: str) -> dict[str, Any]:
    cont = _get_cont(container_id)
    name = (new_name or "").strip().lstrip("/")
    if not name:
        raise ValueError("新名称不能为空")
    cont.rename(name)
    cont.reload()
    return {
        "id": cont.short_id,
        "name": cont.name,
        "status": cont.status,
        "message": f"已重命名为 {cont.name}",
    }


def container_stats(container_id: str) -> dict[str, Any]:
    cont = _get_cont(container_id)
    if cont.status != "running":
        return {"ok": True, "id": cont.short_id, "name": cont.name, "status": cont.status, "stats": None}
    stats = cont.stats(stream=False)
    return {
        "ok": True,
        "id": cont.short_id,
        "name": cont.name,
        "status": cont.status,
        "stats": _parse_stats(stats),
    }


def list_running_stats(limit: int = 12) -> list[dict[str, Any]]:
    """CPU/mem for running containers (Portainer-style activity).

    Docker ``stats(stream=False)`` waits ~1s per container. Collect in parallel
    with a hard timeout so overview never blocks for tens of seconds.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    c = get_client()
    items = c.containers.list(all=False)
    selected = items[: max(1, min(int(limit or 12), 24))]

    def _one(cont: Any) -> dict[str, Any]:
        try:
            stats = cont.stats(stream=False)
            parsed = _parse_stats(stats)
        except Exception as e:
            parsed = {"error": str(e)}
        return {
            "id": cont.short_id,
            "name": cont.name,
            "image": _image_name((cont.attrs or {}).get("Config") or {}),
            "status": cont.status,
            "stats": parsed,
        }

    if not selected:
        return []

    out: list[dict[str, Any]] = []
    # Cap workers; overall wall time ≈ slowest single stats call (~1–2s)
    workers = min(8, len(selected))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futs = {pool.submit(_one, cont): cont for cont in selected}
        try:
            for fut in as_completed(futs, timeout=6):
                try:
                    out.append(fut.result(timeout=0.2))
                except Exception as e:
                    cont = futs[fut]
                    out.append(
                        {
                            "id": getattr(cont, "short_id", ""),
                            "name": getattr(cont, "name", ""),
                            "image": "",
                            "status": getattr(cont, "status", ""),
                            "stats": {"error": str(e)},
                        }
                    )
        except TimeoutError:
            # Partial results are fine; ordered rebuild fills the rest as timeout
            pass
    # Stable order: match selected list
    by_id = {x.get("id"): x for x in out}
    ordered = []
    for cont in selected:
        sid = cont.short_id
        ordered.append(by_id.get(sid) or {
            "id": sid,
            "name": cont.name,
            "image": _image_name((cont.attrs or {}).get("Config") or {}),
            "status": cont.status,
            "stats": {"error": "timeout"},
        })
    return ordered


def image_history(image_id: str) -> list[dict[str, Any]]:
    c = get_client()
    hist = c.api.history(image_id)
    rows = []
    for h in hist or []:
        rows.append(
            {
                "id": (h.get("Id") or "")[:12],
                "created": h.get("Created"),
                "created_by": (h.get("CreatedBy") or "")[:200],
                "size": h.get("Size"),
                "tags": h.get("Tags") or [],
                "comment": h.get("Comment") or "",
            }
        )
    return rows


def container_action_remove(container_id: str, force: bool = False, volumes: bool = False) -> dict[str, Any]:
    cont = _get_cont(container_id)
    name = cont.name
    cid = cont.short_id
    cont.remove(force=force, v=volumes)
    return {"id": cid, "name": name, "removed": True}


# ── Logs / Events ──────────────────────────────────────────


def container_logs(
    container_id: str,
    *,
    tail: int = 200,
    timestamps: bool = True,
    since: int | None = None,
    until: int | None = None,
) -> str:
    cont = _get_cont(container_id)
    kwargs: dict[str, Any] = {
        "stdout": True,
        "stderr": True,
        "timestamps": timestamps,
        "tail": max(1, min(int(tail), 10000)),
    }
    if since is not None:
        kwargs["since"] = since
    if until is not None:
        kwargs["until"] = until
    raw = cont.logs(**kwargs)
    if isinstance(raw, bytes):
        return raw.decode("utf-8", errors="replace")
    return str(raw)


def container_logs_stream(
    container_id: str,
    *,
    tail: int = 100,
    timestamps: bool = True,
):
    """Generator yielding log line strings (follow mode)."""
    cont = _get_cont(container_id)
    stream = cont.logs(
        stdout=True,
        stderr=True,
        stream=True,
        follow=True,
        timestamps=timestamps,
        tail=max(1, min(int(tail), 5000)),
    )
    for chunk in stream:
        if isinstance(chunk, bytes):
            yield chunk.decode("utf-8", errors="replace")
        else:
            yield str(chunk)


def docker_events(since: int | None = None, until: int | None = None, filters: dict | None = None):
    """Generator of decoded Docker event dicts."""
    c = get_client()
    kwargs: dict[str, Any] = {"decode": True}
    if since is not None:
        kwargs["since"] = since
    if until is not None:
        kwargs["until"] = until
    if filters:
        kwargs["filters"] = filters
    for event in c.events(**kwargs):
        yield event


# ── Images ─────────────────────────────────────────────────


def _fmt_image_created(created: Any) -> str:
    """Normalize Docker Created (unix float/int or nanosecond ISO) to YYYY-MM-DD HH:MM."""
    if created is None or created == "":
        return "—"
    try:
        if isinstance(created, (int, float)):
            from datetime import datetime, timezone

            dt = datetime.fromtimestamp(float(created), tz=timezone.utc).astimezone()
            return dt.strftime("%Y-%m-%d %H:%M")
    except Exception:
        pass
    s = str(created).strip()
    # 2026-05-28T07:49:24.130421496Z → keep 3 fractional digits for fromisoformat
    import re

    s2 = re.sub(r"(\.\d{3})\d+", r"\1", s)
    s2 = s2.replace("Z", "+00:00")
    try:
        from datetime import datetime

        dt = datetime.fromisoformat(s2)
        return dt.strftime("%Y-%m-%d %H:%M")
    except Exception:
        m = re.match(r"(\d{4}-\d{2}-\d{2})[T\s](\d{2}:\d{2})", s)
        if m:
            return f"{m.group(1)} {m.group(2)}"
        return s[:16]


def list_images() -> list[dict[str, Any]]:
    """
    Portainer-like image list: short id, tags, size, created, dangling,
    used_by count + used_by_containers [{id,name,state}].
    """
    c = get_client()
    # key -> list of container refs (dedupe by container id later)
    usage: dict[str, list[dict[str, str]]] = {}

    def _add_usage(key: str, ref: dict[str, str]) -> None:
        if not key:
            return
        usage.setdefault(key, []).append(ref)
        if key.startswith("sha256:"):
            bare = key.replace("sha256:", "")
            usage.setdefault(bare[:12], []).append(ref)
            usage.setdefault(bare, []).append(ref)
        elif len(key) >= 12 and "/" not in key and ":" not in key:
            usage.setdefault(key[:12], []).append(ref)

    try:
        for cont in c.containers.list(all=True):
            attrs = cont.attrs or {}
            cfg = attrs.get("Config") or {}
            state = (attrs.get("State") or {}).get("Status") or cont.status or ""
            name = (cont.name or "").lstrip("/")
            short_cid = (cont.short_id or cont.id or "")[:12]
            ref = {"id": short_cid, "name": name, "state": state}
            tag0 = ""
            try:
                if cont.image and cont.image.tags:
                    tag0 = cont.image.tags[0]
            except Exception:
                tag0 = ""
            image_name = (cfg.get("Image") or tag0 or "").strip()
            image_id = attrs.get("Image") or ""
            try:
                if not image_id and cont.image is not None:
                    image_id = cont.image.id or ""
            except Exception:
                pass
            _add_usage(image_id, ref)
            _add_usage(image_name, ref)
    except Exception:
        usage = {}

    def _merge_refs(*keys: str) -> list[dict[str, str]]:
        seen: set[str] = set()
        out_refs: list[dict[str, str]] = []
        for key in keys:
            if not key:
                continue
            for ref in usage.get(key) or []:
                rid = ref.get("id") or ref.get("name") or ""
                if rid in seen:
                    continue
                seen.add(rid)
                out_refs.append(ref)
        return out_refs

    out: list[dict[str, Any]] = []
    for img in c.images.list():
        tags = img.tags or []
        attrs = img.attrs or {}
        full_id = img.id or ""
        short = img.short_id.replace("sha256:", "") if img.short_id else (full_id.replace("sha256:", "")[:12] if full_id else "")
        dangling = not bool(tags)
        keys = [full_id, short, *tags]
        if full_id.startswith("sha256:"):
            bare = full_id[7:]
            keys.extend([bare, bare[:12]])
        used_list = _merge_refs(*keys)
        created_raw = attrs.get("Created")
        out.append(
            {
                "id": short,
                "full_id": full_id,
                "tags": tags,
                "label": tags[0] if tags else short or full_id[:12],
                "size": attrs.get("Size") or 0,
                "created": created_raw,
                "created_fmt": _fmt_image_created(created_raw),
                "dangling": dangling,
                "used_by": len(used_list),
                "used_by_containers": used_list,
            }
        )
    out.sort(key=lambda x: (0 if not x.get("dangling") else 1, x.get("label") or ""))
    return out


def remove_image(image_id: str, force: bool = False, noprune: bool = False) -> dict[str, Any]:
    c = get_client()
    result = c.images.remove(image=image_id, force=force, noprune=noprune)
    return {"image": image_id, "result": result}


def container_image_id(container_id: str) -> str | None:
    """Return the image id currently used by a container (Engine Image field)."""
    if not container_id:
        return None
    c = get_client()
    try:
        cont = c.containers.get(container_id)
    except NotFound:
        return None
    except Exception:
        return None
    attrs = cont.attrs or {}
    iid = attrs.get("Image") or ""
    if not iid:
        try:
            img = getattr(cont, "image", None)
            if img is not None and getattr(img, "id", None):
                iid = img.id
        except Exception:
            iid = ""
    return iid or None


def resolve_image_id(image_ref: str) -> str | None:
    """Resolve a name/tag/digest ref to a local image id after pull."""
    if not image_ref:
        return None
    c = get_client()
    try:
        img = c.images.get(image_ref)
        return getattr(img, "id", None) or None
    except Exception:
        return None


def used_image_ids() -> set[str]:
    """All image ids currently referenced by any container (including stopped)."""
    c = get_client()
    used: set[str] = set()
    for ct in c.containers.list(all=True):
        try:
            img_id = (ct.attrs or {}).get("Image") or ""
            if img_id:
                used.add(img_id)
            img = getattr(ct, "image", None)
            if img is not None and getattr(img, "id", None):
                used.add(img.id)
        except Exception:
            continue
    return used


def cleanup_superseded_images(
    old_image_ids: list[str] | set[str] | None,
    *,
    dangling_prune: bool = True,
    keep_image_ids: list[str] | set[str] | None = None,
) -> dict[str, Any]:
    """
    After a successful recreate: remove superseded image ids that no container
    still references, then optionally prune dangling layers.

    Safe defaults:
    - never force-remove in-use images
    - do not run prune -a (tagged unused of other apps stay)
    """
    keep = {x for x in (keep_image_ids or []) if x}
    candidates = []
    seen: set[str] = set()
    for raw in old_image_ids or []:
        iid = (raw or "").strip()
        if not iid or iid in seen:
            continue
        seen.add(iid)
        candidates.append(iid)

    used = used_image_ids()
    removed: list[str] = []
    skipped: list[dict[str, str]] = []

    for iid in candidates:
        if iid in keep:
            skipped.append({"image": iid, "reason": "keep"})
            continue
        if iid in used:
            skipped.append({"image": iid, "reason": "in_use"})
            continue
        try:
            remove_image(iid, force=False, noprune=False)
            removed.append(iid)
        except Exception as e:
            # not found / conflict / still referenced mid-flight
            skipped.append({"image": iid, "reason": str(e)})

    prune_result: dict[str, Any] | None = None
    if dangling_prune:
        try:
            prune_result = prune_images(dangling=True)
        except Exception as e:
            prune_result = {"ok": False, "error": str(e), "dangling_only": True}

    return {
        "ok": True,
        "removed": removed,
        "skipped": skipped,
        "removed_count": len(removed),
        "dangling_prune": prune_result,
        "space_reclaimed": int((prune_result or {}).get("space_reclaimed") or 0),
    }


def prune_images(dangling: bool = True) -> dict[str, Any]:
    """
    Prune images via Docker Engine API.

    - dangling=True  → only untagged intermediate layers (docker image prune)
    - dangling=False → all images not used by any container (docker image prune -a)

    Critical: filters=None defaults to dangling-only on the daemon. Always pass
    dangling true/false explicitly so "清理未使用" removes tagged unused images.
    """
    c = get_client()
    # Engine accepts bool or "true"/"false"; always send explicit value.
    result = c.images.prune(filters={"dangling": bool(dangling)})
    deleted = result.get("ImagesDeleted") or []
    reclaimed = int(result.get("SpaceReclaimed") or 0)

    # Fallback when prune -a returns empty but unused tagged images remain
    # (seen on some engine/docker-py combos). Remove only images not referenced
    # by any container; never touch in-use images.
    if not dangling and not deleted:
        used_ids: set[str] = set()
        for ct in c.containers.list(all=True):
            try:
                img_id = (ct.attrs or {}).get("Image") or ""
                if img_id:
                    used_ids.add(img_id)
                img = getattr(ct, "image", None)
                if img is not None and getattr(img, "id", None):
                    used_ids.add(img.id)
            except Exception:
                continue
        removed: list[dict[str, Any]] = []
        extra_reclaimed = 0
        for im in c.images.list(all=False):
            iid = getattr(im, "id", None) or ""
            if not iid or iid in used_ids:
                continue
            tags = list(im.tags or [])
            try:
                size = int((im.attrs or {}).get("Size") or 0)
            except Exception:
                size = 0
            # Untag/remove each tag; then remove by id if still present
            targets = tags or [iid]
            ok_any = False
            for target in targets:
                try:
                    c.images.remove(image=target, force=False, noprune=False)
                    removed.append({"Untagged": target})
                    ok_any = True
                except Exception:
                    continue
            if not ok_any:
                try:
                    c.images.remove(image=iid, force=False, noprune=False)
                    removed.append({"Deleted": iid})
                    ok_any = True
                except Exception:
                    continue
            if ok_any:
                extra_reclaimed += size
        if removed:
            deleted = removed
            reclaimed = extra_reclaimed

    return {
        "images_deleted": deleted,
        "space_reclaimed": reclaimed,
        "dangling_only": bool(dangling),
    }


# ── Networks ───────────────────────────────────────────────


def list_networks() -> list[dict[str, Any]]:
    c = get_client()
    out: list[dict[str, Any]] = []
    for net in c.networks.list():
        attrs = net.attrs or {}
        ipam = attrs.get("IPAM") or {}
        configs = (ipam.get("Config") or [{}])[0] if ipam.get("Config") else {}
        containers = attrs.get("Containers") or {}
        out.append(
            {
                "id": net.short_id,
                "name": net.name,
                "driver": attrs.get("Driver"),
                "scope": attrs.get("Scope"),
                "internal": attrs.get("Internal"),
                "attachable": attrs.get("Attachable"),
                "subnet": configs.get("Subnet"),
                "gateway": configs.get("Gateway"),
                "containers": len(containers),
                "labels": attrs.get("Labels") or {},
            }
        )
    out.sort(key=lambda x: x.get("name") or "")
    return out


def create_network(
    name: str,
    *,
    driver: str = "bridge",
    internal: bool = False,
    attachable: bool = False,
    labels: dict[str, str] | None = None,
) -> dict[str, Any]:
    c = get_client()
    net = c.networks.create(
        name,
        driver=driver,
        internal=internal,
        attachable=attachable,
        labels=labels or {},
    )
    net.reload()
    attrs = net.attrs or {}
    return {
        "id": net.short_id,
        "name": net.name,
        "driver": attrs.get("Driver"),
        "created": True,
    }


def remove_network(network_id: str) -> dict[str, Any]:
    c = get_client()
    net = c.networks.get(network_id)
    name = net.name
    net.remove()
    return {"id": network_id, "name": name, "removed": True}


# ── Volumes ────────────────────────────────────────────────


def list_volumes() -> list[dict[str, Any]]:
    c = get_client()
    data = c.volumes.list()
    out: list[dict[str, Any]] = []
    for vol in data:
        attrs = vol.attrs or {}
        out.append(
            {
                "name": vol.name,
                "driver": attrs.get("Driver"),
                "mountpoint": attrs.get("Mountpoint"),
                "created": attrs.get("CreatedAt"),
                "labels": attrs.get("Labels") or {},
                "scope": attrs.get("Scope"),
            }
        )
    out.sort(key=lambda x: x.get("name") or "")
    return out


def create_volume(
    name: str,
    *,
    driver: str = "local",
    labels: dict[str, str] | None = None,
) -> dict[str, Any]:
    c = get_client()
    vol = c.volumes.create(name=name, driver=driver, labels=labels or {})
    attrs = vol.attrs or {}
    return {
        "name": vol.name,
        "driver": attrs.get("Driver"),
        "mountpoint": attrs.get("Mountpoint"),
        "created": True,
    }


def remove_volume(name: str, force: bool = False) -> dict[str, Any]:
    c = get_client()
    vol = c.volumes.get(name)
    vol.remove(force=force)
    return {"name": name, "removed": True}


def prune_volumes() -> dict[str, Any]:
    c = get_client()
    result = c.volumes.prune()
    return {
        "volumes_deleted": result.get("VolumesDeleted") or [],
        "space_reclaimed": result.get("SpaceReclaimed") or 0,
    }


# ── System ─────────────────────────────────────────────────


def system_info() -> dict[str, Any]:
    c = get_client()
    info = c.info()
    version = c.version()
    keys = [
        "Name",
        "ServerVersion",
        "OperatingSystem",
        "OSType",
        "Architecture",
        "NCPU",
        "MemTotal",
        "DockerRootDir",
        "Driver",
        "Containers",
        "ContainersRunning",
        "ContainersPaused",
        "ContainersStopped",
        "Images",
        "NEventsListener",
        "KernelVersion",
        "SystemTime",
    ]
    slim = {k: info.get(k) for k in keys if k in info}
    slim["ApiVersion"] = version.get("ApiVersion")
    slim["GitCommit"] = version.get("GitCommit")
    return slim


def system_df() -> dict[str, Any]:
    c = get_client()
    # docker-py: client.df()
    try:
        data = c.df()
    except Exception:
        data = c.api.df()
    return {
        "layers_size": data.get("LayersSize"),
        "images": [
            {
                "id": (i.get("Id") or "")[:12],
                "tags": i.get("RepoTags") or [],
                "size": i.get("Size"),
                "shared_size": i.get("SharedSize"),
                "containers": i.get("Containers"),
            }
            for i in (data.get("Images") or [])[:50]
        ],
        "containers": [
            {
                "id": (x.get("Id") or "")[:12],
                "names": x.get("Names") or [],
                "image": x.get("Image"),
                "size_rw": x.get("SizeRw"),
                "size_root_fs": x.get("SizeRootFs"),
                "state": x.get("State"),
            }
            for x in (data.get("Containers") or [])[:50]
        ],
        "volumes": [
            {
                "name": v.get("Name"),
                "size": (v.get("UsageData") or {}).get("Size"),
                "ref_count": (v.get("UsageData") or {}).get("RefCount"),
            }
            for v in (data.get("Volumes") or [])[:50]
        ],
        "build_cache": data.get("BuildCache") or [],
    }


def system_prune(
    *,
    containers: bool = True,
    images: bool = True,
    volumes: bool = False,
    networks: bool = True,
    dangling_images_only: bool = True,
) -> dict[str, Any]:
    c = get_client()
    result: dict[str, Any] = {"space_reclaimed": 0}

    if containers:
        r = c.containers.prune()
        result["containers_deleted"] = r.get("ContainersDeleted") or []
        result["space_reclaimed"] += r.get("SpaceReclaimed") or 0

    if images:
        filters = {"dangling": True} if dangling_images_only else None
        r = c.images.prune(filters=filters)
        result["images_deleted"] = r.get("ImagesDeleted") or []
        result["space_reclaimed"] += r.get("SpaceReclaimed") or 0

    if volumes:
        r = c.volumes.prune()
        result["volumes_deleted"] = r.get("VolumesDeleted") or []
        result["space_reclaimed"] += r.get("SpaceReclaimed") or 0

    if networks:
        r = c.networks.prune()
        result["networks_deleted"] = r.get("NetworksDeleted") or []

    return result

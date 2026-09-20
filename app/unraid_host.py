"""Run Unraid's own container template scripts on the Docker host.

Rebuilding a container by hand — ``docker stop`` → ``docker rm`` → ``docker run``
— loses the labels Unraid injects (``net.unraid.docker.managed`` and friends),
so the container drops out of template management and lands in the "third party"
group. Copying every parameter from the template does not help: Unraid only
decides which template owns a container by reading that label.

The supported paths are therefore the host's own scripts, which go through
``DockerTemplates::getUserTemplate()`` + ``xmlToCommand()`` and inject the
labels themselves:

- ``update_container``   pull the template's image, then recreate
- ``rebuild_container``  recreate from the image already present

Both must run on the Docker host. When DockerOps runs in a container with the
socket mounted it cannot execute them directly, so it reaches them over the
same channel used for host operations: ``nsenter``/``chroot`` into the host
namespace is not generally available, so the caller supplies an executor.
See ``unraid_host.py`` for how the executor is chosen.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any, Callable

from config import get_settings
from db import add_ops_record

# Host scripts, relative to the Docker host root.
UPDATE_SCRIPT = (
    "/usr/local/emhttp/plugins/dynamix.docker.manager/scripts/update_container"
)
REBUILD_SCRIPT = (
    "/usr/local/emhttp/plugins/dynamix.docker.manager/scripts/rebuild_container"
)
# Marker proving the label survived. Same string Unraid itself writes.
MANAGED_LABEL = "net.unraid.docker.managed"
MANAGED_VALUE = "dockerman"


class HostScriptUnavailable(RuntimeError):
    """Raised when the host scripts cannot be reached from inside the container."""


def script_available(executor: Callable[..., Any] | None = None) -> tuple[bool, str]:
    """Report whether the template scripts can be executed.

    Returns ``(ok, detail)``. A negative answer is not fatal: callers fall back
    to the SDK path and say so in the operation record, rather than quietly
    rebuilding a container in a way that strips its template ownership.
    """
    ex = executor or default_executor()
    if ex is None:
        return False, (
            "无法在宿主机执行 Unraid 模板脚本（DockerOps 运行在容器内，"
            "缺少宿主机执行通道）。"
        )
    try:
        probe = ex(f"test -x {UPDATE_SCRIPT} && test -x {REBUILD_SCRIPT}")
    except Exception as e:
        return False, f"宿主机执行通道不可用：{e}"
    if not probe.get("ok"):
        return False, (
            "宿主机上未找到 Unraid 模板脚本"
            "（仅 Unraid 提供 /usr/local/emhttp/plugins/dynamix.docker.manager/scripts）。"
        )
    return True, "ok"


def default_executor() -> Callable[..., Any] | None:
    """Pick an executor for running a command on the Docker host.

    Kept as a separate seam so the SSH device channel (added for generic
    connections) can supply its own executor without this module depending on
    it.
    """
    try:
        from host_exec import get_host_executor

        return get_host_executor()
    except Exception:
        return None


def _run_host(
    command: str,
    *,
    executor: Callable[..., Any] | None = None,
    timeout: int = 900,
) -> dict[str, Any]:
    ex = executor or default_executor()
    if ex is None:
        raise HostScriptUnavailable(
            "无法在宿主机执行 Unraid 模板脚本（缺少宿主机执行通道）。"
        )
    out = ex(command, timeout=timeout)
    if not isinstance(out, dict):
        return {"ok": True, "output": str(out)}
    return out


def read_container_label(
    name: str,
    label: str = MANAGED_LABEL,
    *,
    executor: Callable[..., Any] | None = None,
) -> str | None:
    """Read one label off a container by name.

    Uses the host so it works even before the SDK sees a freshly recreated
    container, and so the verification reads the same source Unraid reads.
    """
    cmd = (
        f"docker inspect {_q(name)} --format "
        f"'{{{{index .Config.Labels \"{label}\"}}}}'"
    )
    try:
        out = _run_host(cmd, executor=executor, timeout=60)
    except Exception:
        return None
    if not out.get("ok"):
        return None
    value = (out.get("stdout") or out.get("output") or "").strip()
    return value or None


def verify_managed_label(
    name: str,
    *,
    executor: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """Confirm the container is still owned by its Unraid template.

    This is the acceptance check for every rebuild: if the label is missing the
    container has silently become third-party, which is exactly the failure the
    template path exists to prevent.
    """
    got = read_container_label(name, MANAGED_LABEL, executor=executor)
    ok = (got or "").strip().lower() == MANAGED_VALUE
    return {
        "ok": ok,
        "label": MANAGED_LABEL,
        "expected": MANAGED_VALUE,
        "actual": got,
        "message": (
            "容器模板归属正常（dockerman）"
            if ok
            else f"容器已脱离模板管理：{MANAGED_LABEL}={got!r}，期望 {MANAGED_VALUE!r}"
        ),
    }


def container_running(
    name: str, *, executor: Callable[..., Any] | None = None
) -> bool | None:
    cmd = (
        f"docker inspect {_q(name)} --format '{{{{.State.Running}}}}' 2>/dev/null"
    )
    try:
        out = _run_host(cmd, executor=executor, timeout=60)
    except Exception:
        return None
    if not out.get("ok"):
        return None
    raw = (out.get("stdout") or out.get("output") or "").strip().lower()
    if raw in ("true", "false"):
        return raw == "true"
    return None


def start_container(
    name: str, *, executor: Callable[..., Any] | None = None
) -> dict[str, Any]:
    """Bring a container back up.

    ``rebuild_container`` deliberately stops containers that are not in
    ``/var/lib/docker/unraid-autostart``, leaving them in ``Exited (137)``.
    ``docker start`` is not a manual rebuild and does not affect ownership, so
    it is safe to call here.
    """
    try:
        return _run_host(f"docker start {_q(name)}", executor=executor, timeout=120)
    except Exception as e:
        return {"ok": False, "error": str(e)}


def pull_template_image(
    repository: str, *, executor: Callable[..., Any] | None = None
) -> dict[str, Any]:
    return _run_host(f"docker pull {_q(repository)}", executor=executor, timeout=1800)


def patch_template_repository(
    template_path: Path, repository: str
) -> dict[str, Any]:
    """Point the template at ``repository`` before an update.

    ``update_container`` pulls whatever ``<Repository>`` says, so updating to a
    new tag means editing the template first. Kept byte-faithful to the existing
    XML: only the text of that one element is replaced.
    """
    import re

    if not template_path.is_file():
        return {"ok": False, "message": f"模板不存在：{template_path}"}
    raw = template_path.read_text(encoding="utf-8")
    pattern = re.compile(r"(<Repository>)(.*?)(</Repository>)", re.DOTALL)
    if not pattern.search(raw):
        return {"ok": False, "message": "模板缺少 <Repository> 字段"}
    new_raw = pattern.sub(lambda m: m.group(1) + repository + m.group(3), raw, count=1)
    if new_raw == raw:
        return {"ok": True, "changed": False, "message": "模板镜像未变化"}
    backup = template_path.with_suffix(template_path.suffix + ".pre-update")
    shutil.copy2(template_path, backup)
    template_path.write_bytes(new_raw.encode("utf-8"))
    return {
        "ok": True,
        "changed": True,
        "backup": str(backup),
        "message": f"模板镜像已更新为 {repository}",
    }


def host_update_container(
    name: str,
    *,
    executor: Callable[..., Any] | None = None,
    pull: bool = True,
) -> dict[str, Any]:
    """Update a container through Unraid's template mechanism.

    ``pull=False`` uses ``rebuild_container`` (recreate from the image already
    on disk); ``pull=True`` uses ``update_container``, which pulls first. Both
    go through the host's own template parsing, so template ownership survives.
    """
    script = UPDATE_SCRIPT if pull else REBUILD_SCRIPT
    ok, detail = script_available(executor)
    if not ok:
        raise HostScriptUnavailable(detail)

    # update_container takes '*'-separated names; single name is fine as-is.
    cmd = f"{script} {_q(name)}"
    out = _run_host(cmd, executor=executor, timeout=1800)

    running = container_running(name, executor=executor)
    if running is False and pull:
        # update_container restores the running state on its own; rebuild does
        # not, so only step in when it demonstrably failed to come back.
        started = start_container(name, executor=executor)
        out["restarted"] = bool(started.get("ok"))
    elif running is False and not pull:
        started = start_container(name, executor=executor)
        out["restarted"] = bool(started.get("ok"))

    label = verify_managed_label(name, executor=executor)
    out["managed_label"] = label
    out["running"] = container_running(name, executor=executor)
    out["script"] = "update_container" if pull else "rebuild_container"
    return out


def cleanup_old_images(
    image_ids: list[str],
    *,
    keep_image_ids: list[str] | None = None,
    executor: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """Remove the images a rebuild replaced.

    Unraid's own scripts only drop the image recorded before the pull. When the
    template pointed at a moving tag (``:latest``), the id captured beforehand
    can be the same one that survived, so ids in ``keep_image_ids`` are never
    touched.
    """
    keep = {i for i in (keep_image_ids or []) if i}
    removed: list[str] = []
    failed: list[str] = []
    for iid in image_ids:
        if not iid or iid in keep:
            continue
        try:
            out = _run_host(f"docker rmi {_q(iid)}", executor=executor, timeout=300)
            (removed if out.get("ok") else failed).append(iid)
        except Exception:
            failed.append(iid)
    return {
        "ok": not failed,
        "removed": removed,
        "failed": failed,
        "removed_count": len(removed),
    }


def _q(value: str) -> str:
    """Quote a single shell argument.

    Names and image references reach this module from templates and the UI, so
    they are quoted rather than interpolated raw.
    """
    s = str(value)
    return "'" + s.replace("'", "'\\''") + "'"

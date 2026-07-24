from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from config import get_settings


def _read_os_release() -> dict[str, str]:
    data: dict[str, str] = {}
    for path in (Path("/etc/os-release"), Path("/host/etc/os-release")):
        if not path.is_file():
            continue
        try:
            for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
                if "=" not in line or line.startswith("#"):
                    continue
                k, v = line.split("=", 1)
                data[k.strip()] = v.strip().strip('"')
        except Exception:
            continue
        if data:
            break
    return data


def _path_exists(*candidates: str | Path) -> Path | None:
    for c in candidates:
        p = Path(c)
        if p.exists():
            return p
    return None


def detect_platform(force: str | None = None) -> str:
    """Return unraid | fnos | generic."""
    settings = get_settings()
    forced = (force or settings.platform or "auto").strip().lower()
    if forced in {"unraid", "fnos", "generic"}:
        return forced

    # Unraid signals
    if settings.unraid_templates_path().is_dir():
        return "unraid"
    if _path_exists("/boot/config/docker.cfg", "/boot/config/plugins/dockerMan"):
        return "unraid"
    if os.environ.get("HOST_OS", "").lower() == "unraid":
        return "unraid"

    # FnOS / 飞牛 signals
    osr = _read_os_release()
    blob = " ".join(
        [
            osr.get("NAME", ""),
            osr.get("ID", ""),
            osr.get("ID_LIKE", ""),
            osr.get("PRETTY_NAME", ""),
            osr.get("VERSION", ""),
        ]
    ).lower()
    if any(x in blob for x in ("fnos", "trimui", "飞牛", "trim")):
        return "fnos"
    if _path_exists(
        "/usr/trim",
        "/vol1/@appstore",
        "/vol1/1000/docker",
        "/ffppshare",
    ):
        return "fnos"

    return "generic"


def _compose_probe_dirs(platform: str) -> list[dict[str, Any]]:
    settings = get_settings()
    candidates: list[str] = []
    # User configured first
    for p in settings.compose_dirs():
        candidates.append(str(p))

    if platform == "fnos":
        candidates.extend(
            [
                "/compose",
                "/vol1/docker/compose",
                "/vol1/1000/docker/compose",
                "/vol1/@appdata/compose",
                "/ffppshare/docker/compose",
            ]
        )
    elif platform == "unraid":
        candidates.extend(
            [
                "/compose",
                "/boot/config/plugins/compose.manager/projects",
                "/mnt/user/appdata/compose",
            ]
        )
    else:
        candidates.extend(["/compose", "/opt/compose", "/srv/compose"])

    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for c in candidates:
        if c in seen:
            continue
        seen.add(c)
        path = Path(c)
        out.append(
            {
                "path": c,
                "exists": path.is_dir(),
                "writable": path.is_dir() and os.access(path, os.W_OK),
            }
        )
    return out


def platform_info() -> dict[str, Any]:
    settings = get_settings()
    platform = detect_platform()
    templates = settings.unraid_templates_path()
    osr = _read_os_release()
    compose_probes = _compose_probe_dirs(platform)

    mount_hints: list[str] = []
    if platform == "unraid":
        if not templates.is_dir():
            mount_hints.append(
                "挂载 /boot/config/plugins/dockerMan/templates-user → /unraid/templates-user"
            )
        mount_hints.append(
            "接管时 docker.sock 使用 rw；推荐用 unraid/my-dockerops.xml 安装以保持 dockerman"
        )
    elif platform == "fnos":
        mount_hints.append(
            "飞牛为引擎级接管：挂载 compose 工程目录到容器内，并设置 DOCKEROPS_COMPOSE_PROJECT_DIRS"
        )
        if not any(x["exists"] for x in compose_probes):
            mount_hints.append(
                "未检测到 compose 目录，请将飞牛 compose 路径挂到 /compose 并配置 DOCKEROPS_COMPOSE_PROJECT_DIRS=/compose"
            )
        mount_hints.append("不深绑 AppCenter FPK；与飞牛 Docker UI 共享同一 engine/compose 状态")
    else:
        mount_hints.append("通用主机：配置 DOCKEROPS_COMPOSE_PROJECT_DIRS 指向 compose 工程根目录")

    if settings.takeover_enabled:
        mount_hints.append("完整接管已开启：rw docker.sock ≈ 主机 root，请限制内网访问")
    else:
        mount_hints.append("完整接管关闭：启停/日志可用（登录后）；删除/prune/模板重建需 TAKEOVER=true")

    return {
        "ok": True,
        "platform": platform,
        "platform_forced": settings.platform,
        "os_release": {
            "NAME": osr.get("NAME"),
            "ID": osr.get("ID"),
            "PRETTY_NAME": osr.get("PRETTY_NAME"),
            "VERSION": osr.get("VERSION"),
        },
        "unraid": {
            "templates_path": str(templates),
            "available": templates.is_dir(),
            "enabled": settings.unraid_enabled,
        },
        "compose_probes": compose_probes,
        "resource_apis": settings.resource_apis,
        "console_enabled": settings.console_enabled,
        "takeover_enabled": settings.takeover_enabled,
        "mount_hints": mount_hints,
        "capabilities": {
            "lifecycle": settings.resource_apis,
            "logs": settings.resource_apis,
            "images": settings.resource_apis,
            "networks": settings.resource_apis,
            "volumes": settings.resource_apis,
            "system": settings.resource_apis,
            "events": settings.resource_apis,
            "console": settings.console_enabled,
            "compose": settings.compose_enabled,
            "unraid_templates": settings.unraid_enabled and templates.is_dir(),
        },
    }

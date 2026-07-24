from __future__ import annotations

from pathlib import Path
from typing import Any

from config import get_settings


MANAGER_COMPOSE = "compose"
MANAGER_UNRAID = "unraid"
MANAGER_THIRD_PARTY = "third_party"


def classify_container(
    labels: dict[str, Any] | None,
    name: str | None = None,
    *,
    unraid_template_names: set[str] | None = None,
) -> dict[str, Any]:
    """Classify how a container is managed (compose / unraid / third_party)."""
    labels = labels or {}
    project = labels.get("com.docker.compose.project") or ""
    service = labels.get("com.docker.compose.service") or ""
    working_dir = labels.get("com.docker.compose.project.working_dir") or ""
    config_files = labels.get("com.docker.compose.project.config_files") or ""
    unraid_managed = (labels.get("net.unraid.docker.managed") or "").lower()
    composeman = unraid_managed == "composeman"

    clean_name = (name or "").lstrip("/")
    has_template = bool(
        unraid_template_names is not None and clean_name in unraid_template_names
    )

    # Compose first (including Unraid Compose plugin)
    if project or composeman:
        return {
            "manager": MANAGER_COMPOSE,
            "compose_project": project or None,
            "compose_service": service or None,
            "compose_working_dir": working_dir or None,
            "compose_config_files": config_files or None,
            "unraid_managed": unraid_managed or None,
            "template_name": clean_name if has_template else None,
            "label": "Compose",
        }

    if unraid_managed == "dockerman" or has_template:
        return {
            "manager": MANAGER_UNRAID,
            "compose_project": None,
            "compose_service": None,
            "compose_working_dir": None,
            "compose_config_files": None,
            "unraid_managed": unraid_managed or "dockerman",
            "template_name": clean_name or None,
            "label": "Unraid",
        }

    return {
        "manager": MANAGER_THIRD_PARTY,
        "compose_project": None,
        "compose_service": None,
        "compose_working_dir": None,
        "compose_config_files": None,
        "unraid_managed": unraid_managed or None,
        "template_name": None,
        "label": "三方",
    }


def list_unraid_template_names() -> set[str]:
    settings = get_settings()
    if not settings.unraid_enabled:
        return set()
    root = settings.unraid_templates_path()
    if not root.is_dir():
        return set()
    names: set[str] = set()
    for p in root.glob("my-*.xml"):
        # my-Name.xml -> Name (best-effort; XML <Name> is authoritative when parsed)
        stem = p.stem
        if stem.lower().startswith("my-"):
            names.add(stem[3:])
        names.add(stem)
    # also parse <Name> lightly
    try:
        import xml.etree.ElementTree as ET

        for p in root.glob("*.xml"):
            try:
                root_el = ET.parse(p).getroot()
                n = (root_el.findtext("Name") or "").strip()
                if n:
                    names.add(n)
            except Exception:
                continue
    except Exception:
        pass
    return names


def managers_summary(containers: list[dict[str, Any]]) -> dict[str, Any]:
    settings = get_settings()
    counts = {MANAGER_COMPOSE: 0, MANAGER_UNRAID: 0, MANAGER_THIRD_PARTY: 0}
    for c in containers:
        m = c.get("manager") or MANAGER_THIRD_PARTY
        counts[m] = counts.get(m, 0) + 1

    templates_path = settings.unraid_templates_path()
    compose_dirs = [str(p) for p in settings.compose_dirs()]
    compose_dirs_ok = [str(p) for p in settings.compose_dirs() if p.is_dir()]

    # Lazy import avoids circular deps at module load
    from host_platform import detect_platform

    try:
        host_platform = detect_platform()
    except Exception:
        host_platform = "generic"

    return {
        "ok": True,
        "version": "0.4.1",
        "platform": host_platform,
        "takeover_enabled": settings.takeover_enabled,
        "resource_apis": settings.resource_apis,
        "console_enabled": settings.console_enabled,
        "compose_enabled": settings.compose_enabled,
        "unraid_enabled": settings.unraid_enabled,
        "counts": counts,
        "unraid": {
            "templates_path": str(templates_path),
            "available": templates_path.is_dir(),
            "writable": templates_path.is_dir() and os_access_write(templates_path),
        },
        "compose": {
            "project_dirs": compose_dirs,
            "project_dirs_available": compose_dirs_ok,
            "bin": settings.compose_bin,
        },
        "hints": _hints(settings, templates_path.is_dir()),
    }


def os_access_write(path: Path) -> bool:
    try:
        import os

        return os.access(path, os.W_OK)
    except Exception:
        return False


def _hints(settings, unraid_ok: bool) -> list[str]:
    tips: list[str] = []
    if not settings.takeover_enabled:
        tips.append(
            "完整接管关闭：登录后可启停/日志；删除/prune/compose up-down/模板重建需 DOCKEROPS_TAKEOVER_ENABLED=true。"
        )
    else:
        tips.append("完整接管已开启：请确认 docker.sock 为读写挂载，并限制内网访问。")
    if settings.resource_apis:
        tips.append("资源 API 已启用：容器生命周期、镜像、网络、卷、系统清理（可日常替代 Portainer）。")
    if settings.unraid_enabled and not unraid_ok:
        tips.append(
            f"未检测到 Unraid 模板目录 {settings.unraid_templates_user}。"
            "请挂载 /boot/config/plugins/dockerMan/templates-user → /unraid/templates-user。"
        )
    if settings.compose_enabled and not settings.compose_dirs():
        tips.append(
            "建议设置 DOCKEROPS_COMPOSE_PROJECT_DIRS（飞牛/通用主机挂载 compose 目录），便于引擎级双方接管。"
        )
    return tips

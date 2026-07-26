from __future__ import annotations

import json
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

from config import get_settings
from db import add_ops_record
from docker_client import list_containers


COMPOSE_FILE_NAMES = (
    "compose.yaml",
    "compose.yml",
    "docker-compose.yaml",
    "docker-compose.yml",
)


def list_projects() -> list[dict[str, Any]]:
    settings = get_settings()
    if not settings.compose_enabled:
        return []

    by_name: dict[str, dict[str, Any]] = {}

    # From running/stopped containers labels
    try:
        containers = list_containers(all_containers=True)
    except Exception:
        containers = []

    for c in containers:
        if c.get("manager") != "compose":
            continue
        project = c.get("compose_project") or "unknown"
        entry = by_name.setdefault(
            project,
            {
                "name": project,
                "source": "labels",
                "working_dir": c.get("compose_working_dir"),
                "config_files": [],
                "services": [],
                "containers": [],
                "running": 0,
                "total": 0,
            },
        )
        svc = c.get("compose_service") or c.get("name")
        if svc and svc not in entry["services"]:
            entry["services"].append(svc)
        entry["containers"].append(
            {
                "id": c.get("id"),
                "name": c.get("name"),
                "service": svc,
                "status": c.get("status"),
                "image": c.get("image"),
                "health": c.get("health"),
            }
        )
        entry["total"] += 1
        if (c.get("status") or "").lower() == "running":
            entry["running"] += 1
        wd = c.get("compose_working_dir")
        if wd and not entry.get("working_dir"):
            entry["working_dir"] = wd
        cf = c.get("compose_config_files")
        if cf:
            for part in str(cf).split(","):
                part = part.strip()
                if part and part not in entry["config_files"]:
                    entry["config_files"].append(part)

    # Scan configured project dirs
    for base in settings.compose_dirs():
        if not base.is_dir():
            continue
        for compose_file in _find_compose_files(base):
            project = compose_file.parent.name
            entry = by_name.setdefault(
                project,
                {
                    "name": project,
                    "source": "filesystem",
                    "working_dir": str(compose_file.parent),
                    "config_files": [],
                    "services": [],
                    "containers": [],
                    "running": 0,
                    "total": 0,
                },
            )
            path = str(compose_file)
            if path not in entry["config_files"]:
                entry["config_files"].append(path)
            if not entry.get("working_dir"):
                entry["working_dir"] = str(compose_file.parent)
            if entry.get("source") == "labels":
                entry["source"] = "labels+filesystem"

    return sorted(by_name.values(), key=lambda x: x["name"])


def get_project(name: str) -> dict[str, Any] | None:
    for p in list_projects():
        if p["name"] == name:
            return p
    return None


def backup_project(name: str, actor: str | None = None) -> dict[str, Any]:
    project = get_project(name)
    if not project:
        rec = add_ops_record(
            action="compose_backup",
            target=name,
            status="failed",
            detail={"error": "project_not_found"},
            actor=actor,
        )
        return {"ok": False, "record": rec, "message": f"未找到 Compose 项目：{name}"}

    settings = get_settings()
    ts = time.strftime("%Y%m%d-%H%M%S")
    dest = Path(settings.data_dir) / "backups" / "compose" / f"{name}-{ts}"
    dest.mkdir(parents=True, exist_ok=True)

    copied: list[str] = []
    for cf in project.get("config_files") or []:
        src = Path(cf)
        if src.is_file():
            target = dest / src.name
            shutil.copy2(src, target)
            copied.append(str(target))

    meta = {
        "created_at": time.time(),
        "actor": actor,
        "project": project,
        "note": "Compose 项目备份：compose 文件副本 + 容器摘要。数据卷需结合宿主机策略。",
    }
    (dest / "project.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    rec = add_ops_record(
        action="compose_backup",
        target=name,
        status="ok",
        detail={"path": str(dest), "files": copied, "services": project.get("services")},
        actor=actor,
    )
    return {
        "ok": True,
        "record": rec,
        "backup_path": str(dest),
        "message": f"已备份 Compose 项目 {name}",
        "project": project,
    }


def _emit_progress(on_progress, payload: dict[str, Any]) -> None:
    if not on_progress:
        return
    try:
        on_progress(payload)
    except Exception:
        pass


def safe_update_project(
    name: str,
    actor: str | None = None,
    *,
    service: str | None = None,
    recreate: bool = True,
    on_progress=None,
) -> dict[str, Any]:
    """Backup + compose pull + compose up (takeover required for up/recreate)."""
    settings = get_settings()
    project = get_project(name)
    if not project:
        rec = add_ops_record(
            action="compose_update",
            target=name,
            status="failed",
            detail={"error": "project_not_found"},
            actor=actor,
        )
        return {"ok": False, "record": rec, "message": f"未找到 Compose 项目：{name}"}

    _emit_progress(on_progress, {"event": "stage", "stage": "backup", "message": f"备份 Compose 项目 {name}", "container": name})
    backup = backup_project(name, actor=actor)
    if not backup.get("ok"):
        _emit_progress(on_progress, {"event": "error", "message": "备份失败", "container": name})
        return {"ok": False, "message": "备份失败，已中止更新", "backup": backup}

    _emit_progress(on_progress, {"event": "stage", "stage": "pull", "message": f"compose pull {name}", "container": name})
    pull = _run_compose(project, ["pull"] + ([service] if service else []), actor=actor)
    if not pull.get("ok"):
        rec = add_ops_record(
            action="compose_update",
            target=name,
            status="failed",
            detail={"step": "pull", "pull": pull, "backup": backup.get("backup_path")},
            actor=actor,
        )
        _emit_progress(on_progress, {"event": "error", "message": "compose pull 失败", "container": name})
        return {"ok": False, "record": rec, "backup": backup, "pull": pull, "message": "compose pull 失败"}

    # up / recreate requires takeover
    try:
        settings.takeover_guard()
    except PermissionError as e:
        rec = add_ops_record(
            action="compose_update",
            target=name,
            status="partial",
            detail={
                "step": "pull_only",
                "reason": str(e),
                "backup": backup.get("backup_path"),
                "pull": pull,
                "next_steps": [
                    "镜像已拉取。",
                    "开启 DOCKEROPS_TAKEOVER_ENABLED=true 后可由 DockerOps 执行 compose up。",
                    "或在原系统目录执行 docker compose up -d。",
                ],
            },
            actor=actor,
        )
        msg = "已备份并拉取镜像；接管未开启，未执行 compose up（原系统仍可接管）。"
        _emit_progress(on_progress, {"event": "stage", "stage": "done", "message": msg, "container": name, "ok": True})
        return {
            "ok": True,
            "partial": True,
            "record": rec,
            "backup": backup,
            "pull": pull,
            "message": msg,
        }

    _emit_progress(on_progress, {"event": "stage", "stage": "recreate", "message": f"compose up {name}", "container": name})
    up_args = ["up", "-d"]
    if recreate:
        up_args.append("--force-recreate")
    if service:
        up_args.append(service)
    up = _run_compose(project, up_args, actor=actor)
    status = "ok" if up.get("ok") else "failed"
    rec = add_ops_record(
        action="compose_update",
        target=name,
        status=status,
        detail={
            "backup": backup.get("backup_path"),
            "pull": pull,
            "up": up,
            "service": service,
        },
        actor=actor,
    )
    msg = f"Compose 项目 {name} 安全更新完成" if up.get("ok") else f"compose up 失败：{up.get('stderr')}"
    _emit_progress(
        on_progress,
        {
            "event": "stage",
            "stage": "done" if up.get("ok") else "error",
            "message": msg,
            "container": name,
            "ok": bool(up.get("ok")),
        },
    )
    return {
        "ok": bool(up.get("ok")),
        "record": rec,
        "backup": backup,
        "pull": pull,
        "up": up,
        "message": msg,
    }


def project_up(name: str, actor: str | None = None, service: str | None = None) -> dict[str, Any]:
    settings = get_settings()
    settings.takeover_guard()
    project = get_project(name)
    if not project:
        return {"ok": False, "message": f"未找到项目 {name}"}
    args = ["up", "-d"]
    if service:
        args.append(service)
    result = _run_compose(project, args, actor=actor)
    rec = add_ops_record(
        action="compose_up",
        target=name,
        status="ok" if result.get("ok") else "failed",
        detail=result,
        actor=actor,
    )
    return {"ok": result.get("ok"), "record": rec, "result": result, "message": "compose up 完成" if result.get("ok") else result.get("stderr")}


def project_down(name: str, actor: str | None = None) -> dict[str, Any]:
    settings = get_settings()
    settings.takeover_guard()
    project = get_project(name)
    if not project:
        return {"ok": False, "message": f"未找到项目 {name}"}
    result = _run_compose(project, ["down"], actor=actor)
    rec = add_ops_record(
        action="compose_down",
        target=name,
        status="ok" if result.get("ok") else "failed",
        detail=result,
        actor=actor,
    )
    return {"ok": result.get("ok"), "record": rec, "result": result, "message": "compose down 完成" if result.get("ok") else result.get("stderr")}


def _find_compose_files(base: Path) -> list[Path]:
    found: list[Path] = []
    # direct files in base
    for name in COMPOSE_FILE_NAMES:
        p = base / name
        if p.is_file():
            found.append(p)
    # one level of subdirs (common NAS layout)
    try:
        for child in base.iterdir():
            if not child.is_dir():
                continue
            for name in COMPOSE_FILE_NAMES:
                p = child / name
                if p.is_file():
                    found.append(p)
                    break
    except Exception:
        pass
    return found


def _run_compose(
    project: dict[str, Any],
    args: list[str],
    actor: str | None = None,
) -> dict[str, Any]:
    settings = get_settings()
    bin_name = settings.compose_bin or "docker"
    cmd: list[str]
    if bin_name.endswith("docker-compose") or bin_name == "docker-compose":
        cmd = [bin_name]
    else:
        cmd = [bin_name, "compose"]

    files = project.get("config_files") or []
    cwd = project.get("working_dir") or None
    for f in files:
        # only pass files that exist in this filesystem
        if Path(f).is_file():
            cmd.extend(["-f", f])
    name = project.get("name")
    if name:
        cmd.extend(["-p", name])
    cmd.extend(args)

    try:
        proc = subprocess.run(
            cmd,
            cwd=cwd if cwd and Path(cwd).is_dir() else None,
            capture_output=True,
            text=True,
            timeout=600,
        )
        return {
            "ok": proc.returncode == 0,
            "cmd": cmd,
            "cwd": cwd,
            "returncode": proc.returncode,
            "stdout": (proc.stdout or "")[-4000:],
            "stderr": (proc.stderr or "")[-4000:],
            "actor": actor,
        }
    except FileNotFoundError:
        return {
            "ok": False,
            "cmd": cmd,
            "error": "docker_compose_not_found",
            "stderr": "容器内未找到 docker/compose CLI。请使用含 Docker CLI 的镜像并挂载 docker.sock。",
        }
    except subprocess.TimeoutExpired:
        return {"ok": False, "cmd": cmd, "error": "timeout", "stderr": "compose 命令超时"}
    except Exception as e:
        return {"ok": False, "cmd": cmd, "error": str(e), "stderr": str(e)}

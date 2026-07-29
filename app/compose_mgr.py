from __future__ import annotations

import json
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

from config import get_settings
from db import add_ops_record
from docker_client import (
    cleanup_superseded_images,
    container_image_id,
    list_containers,
    remove_container,
    stop_container,
)


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


def _project_service_snapshot(
    project: dict[str, Any],
    service: str | None = None,
) -> list[dict[str, Any]]:
    """Capture compose service containers + current image ids before pull/recreate."""
    snap: list[dict[str, Any]] = []
    for c in project.get("containers") or []:
        svc = c.get("service") or c.get("name")
        if service and svc != service and c.get("name") != service:
            continue
        cid = c.get("id") or c.get("name")
        iid = None
        if cid:
            try:
                iid = container_image_id(cid)
            except Exception:
                iid = None
        snap.append(
            {
                "id": c.get("id"),
                "name": c.get("name"),
                "service": svc,
                "image": c.get("image"),
                "image_id": iid,
                "status": c.get("status"),
            }
        )
    return snap


def _hard_replace_stale_services(
    project: dict[str, Any],
    pre_snap: list[dict[str, Any]],
    *,
    service: str | None = None,
    actor: str | None = None,
) -> dict[str, Any]:
    """
    Unraid-like fallback: if a service container still runs a pre-update image id
    (or is missing after up), stop → remove → compose up -d --no-deps <service>.
    """
    # Refresh project view after up
    fresh = get_project(project.get("name") or "") or project
    by_service: dict[str, dict[str, Any]] = {}
    for c in fresh.get("containers") or []:
        svc = c.get("service") or c.get("name")
        if svc:
            by_service[svc] = c

    hard: list[dict[str, Any]] = []
    for item in pre_snap:
        svc = item.get("service")
        if not svc:
            continue
        if service and svc != service:
            continue
        old_iid = item.get("image_id")
        cur = by_service.get(svc)
        need = False
        reason = ""
        if not cur:
            need = True
            reason = "missing_after_up"
        else:
            cur_iid = None
            try:
                cur_iid = container_image_id(cur.get("id") or cur.get("name") or "")
            except Exception:
                cur_iid = None
            # Still on the exact pre-update image id → recreate did not take effect
            if old_iid and cur_iid and old_iid == cur_iid:
                need = True
                reason = "still_on_old_image"
            elif not cur_iid and old_iid:
                need = True
                reason = "image_id_unresolved"

        if not need:
            continue

        target_id = (cur or item).get("id") or (cur or item).get("name") or svc
        step: dict[str, Any] = {"service": svc, "reason": reason, "target": target_id}
        try:
            try:
                stop_container(target_id)
            except Exception as e:
                step["stop_error"] = str(e)
            try:
                remove_container(target_id, force=True)
            except Exception as e:
                step["remove_error"] = str(e)
            up_one = _run_compose(
                project,
                ["up", "-d", "--no-deps", "--force-recreate", "--remove-orphans", svc],
                actor=actor,
            )
            step["up"] = up_one
            step["ok"] = bool(up_one.get("ok"))
        except Exception as e:
            step["ok"] = False
            step["error"] = str(e)
        hard.append(step)

    ok_all = all(x.get("ok") for x in hard) if hard else True
    return {"ok": ok_all, "replaced": hard, "count": len(hard)}


def safe_update_project(
    name: str,
    actor: str | None = None,
    *,
    service: str | None = None,
    recreate: bool = True,
    remove_orphans: bool = True,
    cleanup_images: bool = True,
    on_progress=None,
) -> dict[str, Any]:
    """
    Backup + compose pull + compose up (takeover required for up/recreate).

    Aligns with Unraid update rules when takeover is on:
    - force-recreate without requiring a manual stop first
    - --remove-orphans to drop stale project containers
    - hard stop→remove→up fallback if a service stays on the old image
    - cleanup superseded image ids + dangling layers after success
    """
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

    pre_snap = _project_service_snapshot(project, service=service)
    old_image_ids = [x["image_id"] for x in pre_snap if x.get("image_id")]

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
                "old_image_ids": old_image_ids,
                "next_steps": [
                    "镜像已拉取，但未重建容器（完整接管未开启）。",
                    "开启 DOCKEROPS_TAKEOVER_ENABLED=true 并挂载 rw docker.sock 后重试，DockerOps 将自动 force-recreate / 清理孤立容器与旧镜像。",
                    "或在飞牛/原系统项目目录执行：docker compose up -d --force-recreate --remove-orphans",
                ],
            },
            actor=actor,
        )
        msg = (
            "已备份并拉取镜像；未重建容器（接管未开启）。"
            "请开启完整接管后重试，或在飞牛侧执行 compose up -d --force-recreate --remove-orphans。"
        )
        _emit_progress(on_progress, {"event": "stage", "stage": "done", "message": msg, "container": name, "ok": True, "partial": True})
        return {
            "ok": True,
            "partial": True,
            "record": rec,
            "backup": backup,
            "pull": pull,
            "message": msg,
            "old_image_ids": old_image_ids,
        }

    _emit_progress(
        on_progress,
        {
            "event": "stage",
            "stage": "recreate",
            "message": f"compose up {name}（force-recreate / remove-orphans）",
            "container": name,
        },
    )
    up_args = ["up", "-d"]
    if recreate:
        up_args.append("--force-recreate")
    if remove_orphans:
        up_args.append("--remove-orphans")
    if service:
        up_args.append(service)
    up = _run_compose(project, up_args, actor=actor)

    hard = {"ok": True, "replaced": [], "count": 0}
    if recreate:
        # Always attempt hard replace for services still on old image (also helps when up partially fails)
        _emit_progress(
            on_progress,
            {
                "event": "stage",
                "stage": "hard_replace",
                "message": f"检查并硬替换未切换镜像的服务 {name}",
                "container": name,
            },
        )
        hard = _hard_replace_stale_services(
            project, pre_snap, service=service, actor=actor
        )

    recreated_ok = bool(up.get("ok")) or (hard.get("count", 0) > 0 and hard.get("ok"))
    # If initial up failed but hard replace fixed all targeted services, treat as ok
    if not up.get("ok") and hard.get("count", 0) > 0 and hard.get("ok"):
        recreated_ok = True
    if up.get("ok") and hard.get("count", 0) > 0 and not hard.get("ok"):
        recreated_ok = False

    image_cleanup: dict[str, Any] | None = None
    if recreated_ok and cleanup_images and old_image_ids:
        _emit_progress(
            on_progress,
            {
                "event": "stage",
                "stage": "cleanup_images",
                "message": "清理被替换的旧镜像 / dangling 层",
                "container": name,
            },
        )
        try:
            image_cleanup = cleanup_superseded_images(old_image_ids, dangling_prune=True)
        except Exception as e:
            image_cleanup = {"ok": False, "error": str(e)}

    status = "ok" if recreated_ok else "failed"
    rec = add_ops_record(
        action="compose_update",
        target=name,
        status=status,
        detail={
            "backup": backup.get("backup_path"),
            "pull": pull,
            "up": up,
            "service": service,
            "remove_orphans": remove_orphans,
            "hard_replace": hard,
            "old_image_ids": old_image_ids,
            "image_cleanup": image_cleanup,
        },
        actor=actor,
    )
    if recreated_ok:
        parts = [f"Compose 项目 {name} 安全更新完成"]
        if remove_orphans:
            parts.append("已 remove-orphans")
        if hard.get("count"):
            parts.append(f"硬替换 {hard.get('count')} 个服务")
        if image_cleanup and image_cleanup.get("removed_count"):
            parts.append(f"清理旧镜像 {image_cleanup.get('removed_count')} 个")
        msg = "；".join(parts)
    else:
        err = up.get("stderr") or (hard.get("replaced") and "硬替换失败") or "compose up 失败"
        msg = f"compose up 失败：{err}"
    _emit_progress(
        on_progress,
        {
            "event": "stage",
            "stage": "done" if recreated_ok else "error",
            "message": msg,
            "container": name,
            "ok": recreated_ok,
        },
    )
    return {
        "ok": recreated_ok,
        "record": rec,
        "backup": backup,
        "pull": pull,
        "up": up,
        "hard_replace": hard,
        "orphans_removed": bool(remove_orphans),
        "old_image_ids": old_image_ids,
        "image_cleanup": image_cleanup,
        "message": msg,
    }


def project_up(
    name: str,
    actor: str | None = None,
    service: str | None = None,
    *,
    remove_orphans: bool = True,
) -> dict[str, Any]:
    settings = get_settings()
    settings.takeover_guard()
    project = get_project(name)
    if not project:
        return {"ok": False, "message": f"未找到项目 {name}"}
    args = ["up", "-d"]
    if remove_orphans:
        args.append("--remove-orphans")
    if service:
        args.append(service)
    result = _run_compose(project, args, actor=actor)
    rec = add_ops_record(
        action="compose_up",
        target=name,
        status="ok" if result.get("ok") else "failed",
        detail={**result, "remove_orphans": remove_orphans},
        actor=actor,
    )
    return {
        "ok": result.get("ok"),
        "record": rec,
        "result": result,
        "message": "compose up 完成" if result.get("ok") else result.get("stderr"),
    }


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

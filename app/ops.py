from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Callable

from config import get_settings
from db import add_ops_record, list_ops_records
from docker_client import get_container, pull_image
from host_platform import detect_platform

ProgressCb = Callable[[dict[str, Any]], None] | None


def _emit(on_progress: ProgressCb, payload: dict[str, Any]) -> None:
    if not on_progress:
        return
    try:
        on_progress(payload)
    except Exception:
        pass


def records(limit: int = 100) -> list[dict[str, Any]]:
    return list_ops_records(limit=limit)


def backup_container(container_id: str, actor: str | None = None) -> dict[str, Any]:
    """Backup by manager: compose project / unraid template / raw inspect."""
    try:
        detail = get_container(container_id)
    except KeyError:
        rec = add_ops_record(
            action="backup",
            target=container_id,
            status="failed",
            detail={"error": "container_not_found"},
            actor=actor,
        )
        return {"ok": False, "record": rec, "message": "容器不存在"}

    manager = detail.get("manager") or "third_party"
    name = (detail.get("name") or container_id).lstrip("/")

    if manager == "compose" and detail.get("compose_project"):
        from compose_mgr import backup_project

        result = backup_project(detail["compose_project"], actor=actor)
        result["manager"] = "compose"
        result["container"] = name
        return result

    if manager == "unraid":
        from unraid_mgr import backup_template

        tpl_name = detail.get("template_name") or name
        result = backup_template(tpl_name, actor=actor)
        result["manager"] = "unraid"
        return result

    return _backup_raw(detail, container_id, actor=actor)


def _backup_raw(detail: dict[str, Any], container_id: str, actor: str | None = None) -> dict[str, Any]:
    settings = get_settings()
    backup_dir = Path(settings.data_dir) / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%Y%m%d-%H%M%S")
    name = (detail.get("name") or container_id).lstrip("/")
    path = backup_dir / f"{name}-{ts}.json"
    payload = {
        "created_at": time.time(),
        "actor": actor,
        "manager": "third_party",
        "container": detail,
        "note": "三方容器元数据备份。建议 Adopt 为 Unraid 模板或纳入 Compose 以便双方接管。",
        "adopt_hint": "POST /api/unraid/adopt/{id}（需接管开启并挂载 templates-user）",
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    rec = add_ops_record(
        action="backup",
        target=name,
        status="ok",
        detail={"path": str(path), "image": detail.get("image"), "id": detail.get("id"), "manager": "third_party"},
        actor=actor,
    )
    return {
        "ok": True,
        "record": rec,
        "backup_path": str(path),
        "manager": "third_party",
        "message": "已写入三方容器备份元数据。",
    }


def safe_update(
    container_id: str,
    image: str | None = None,
    actor: str | None = None,
    on_progress: ProgressCb = None,
) -> dict[str, Any]:
    """
    Route update by manager:
    - compose  -> compose backup + pull + up (up needs takeover)
    - unraid   -> template backup + pull + template recreate (recreate needs takeover)
    - third    -> backup + pull only + adopt hint
    on_progress: optional callback for stage/pull events (SSE).
    """
    try:
        detail = get_container(container_id)
    except KeyError:
        rec = add_ops_record(
            action="update",
            target=container_id,
            status="failed",
            detail={"error": "container_not_found"},
            actor=actor,
        )
        _emit(on_progress, {"event": "error", "message": "容器不存在", "container": container_id})
        return {"ok": False, "record": rec, "message": "容器不存在"}

    manager = detail.get("manager") or "third_party"
    name = (detail.get("name") or container_id).lstrip("/")
    _emit(
        on_progress,
        {
            "event": "stage",
            "stage": "start",
            "message": f"开始更新 {name}（{manager}）",
            "container": name,
            "manager": manager,
        },
    )

    if manager == "compose" and detail.get("compose_project"):
        from compose_mgr import safe_update_project

        result = safe_update_project(
            detail["compose_project"],
            actor=actor,
            service=detail.get("compose_service"),
            recreate=True,
            on_progress=on_progress,
        )
        result["manager"] = "compose"
        result["routed_from"] = name
        return result

    if manager == "unraid":
        from unraid_mgr import safe_update_unraid

        tpl_name = detail.get("template_name") or name
        result = safe_update_unraid(
            tpl_name,
            actor=actor,
            repository=image,
            recreate=True,
            on_progress=on_progress,
        )
        result["manager"] = "unraid"
        result["routed_from"] = name
        return result

    # third_party: pull only
    return _safe_update_third_party(
        detail, container_id, image=image, actor=actor, on_progress=on_progress
    )


def _safe_update_third_party(
    detail: dict[str, Any],
    container_id: str,
    image: str | None = None,
    actor: str | None = None,
    on_progress: ProgressCb = None,
) -> dict[str, Any]:
    name = (detail.get("name") or container_id).lstrip("/")
    target_image = image or detail.get("image")
    if not target_image:
        rec = add_ops_record(
            action="update",
            target=name,
            status="failed",
            detail={"error": "no_image", "manager": "third_party"},
            actor=actor,
        )
        _emit(on_progress, {"event": "error", "message": "无法解析镜像名", "container": name})
        return {"ok": False, "record": rec, "message": "无法解析镜像名"}

    _emit(on_progress, {"event": "stage", "stage": "backup", "message": "备份元数据…", "container": name})
    backup = _backup_raw(detail, container_id, actor=actor)
    if not backup.get("ok"):
        _emit(on_progress, {"event": "error", "message": "备份失败", "container": name})
        return {"ok": False, "message": "备份失败，已中止更新", "backup": backup}

    def _pull_cb(chunk: dict[str, Any]) -> None:
        pd = chunk.get("progressDetail") or {}
        cur = pd.get("current")
        total = pd.get("total")
        percent = None
        if isinstance(cur, (int, float)) and isinstance(total, (int, float)) and total:
            percent = round(float(cur) / float(total) * 100, 1)
        _emit(
            on_progress,
            {
                "event": "pull",
                "status": chunk.get("status") or "",
                "id": chunk.get("id") or "",
                "current": cur,
                "total": total,
                "percent": percent,
                "container": name,
                "image": target_image,
            },
        )

    _emit(
        on_progress,
        {
            "event": "stage",
            "stage": "pull",
            "message": f"拉取镜像 {target_image}",
            "container": name,
            "image": target_image,
        },
    )
    try:
        pull_result = pull_image(target_image, on_progress=_pull_cb)
        pull_ok = True
        pull_err = None
    except Exception as e:
        pull_result = {}
        pull_ok = False
        pull_err = str(e)

    status = "ok" if pull_ok else "failed"
    try:
        host_plat = detect_platform()
    except Exception:
        host_plat = "generic"
    next_steps = [
        "三方容器仅执行了备份 + 拉镜像，未重建（避免破坏原部署方式）。",
        "若在 Unraid：POST /api/unraid/adopt/{id} 生成 my-*.xml 并按模板重建（非三方）。",
        "若在 Compose/飞牛：将服务纳入 compose 项目、挂载 DOCKEROPS_COMPOSE_PROJECT_DIRS，"
        "并开启 DOCKEROPS_TAKEOVER_ENABLED=true 后使用项目/一键更新（自动 force-recreate、"
        "remove-orphans 与清理旧镜像）。",
    ]
    if host_plat == "fnos":
        next_steps.insert(
            0,
            "当前主机疑似飞牛(FnOS)：三方容器无法自动替换运行中实例，"
            "请用 Compose 管理该服务后再更新，否则仍需在飞牛侧手动停容器/重建。",
        )
    rec = add_ops_record(
        action="update",
        target=name,
        status=status,
        detail={
            "manager": "third_party",
            "platform": host_plat,
            "image": target_image,
            "backup_path": backup.get("backup_path"),
            "pull": pull_result,
            "error": pull_err,
            "partial": True,
            "next_steps": next_steps,
        },
        actor=actor,
    )
    if pull_ok:
        msg = (
            "三方容器：已备份并拉镜像，未重建运行实例。"
            + (
                "飞牛环境请纳入 Compose 并开启接管后重试自动更新。"
                if host_plat == "fnos"
                else "可用 Adopt/Compose 纳入正规管理后再自动重建。"
            )
        )
    else:
        msg = f"拉镜像失败：{pull_err}"
    _emit(
        on_progress,
        {
            "event": "stage",
            "stage": "done" if pull_ok else "error",
            "message": msg,
            "container": name,
            "ok": pull_ok,
        },
    )
    return {
        "ok": pull_ok,
        "partial": bool(pull_ok),
        "manager": "third_party",
        "platform": host_plat,
        "record": rec,
        "backup": backup,
        "pull": pull_result,
        "message": msg,
        "next_steps": next_steps if pull_ok else None,
        "adopt_path": f"/api/unraid/adopt/{detail.get('id') or name}",
    }


def rollback_guide(container_id: str, actor: str | None = None) -> dict[str, Any]:
    settings = get_settings()
    name = container_id
    manager = "third_party"
    try:
        detail = get_container(container_id)
        name = (detail.get("name") or container_id).lstrip("/")
        manager = detail.get("manager") or "third_party"
    except Exception:
        detail = None

    backup_dir = Path(settings.data_dir) / "backups"
    latest = None
    guide: list[str] = []

    if manager == "compose" and detail and detail.get("compose_project"):
        cdir = backup_dir / "compose"
        project = detail["compose_project"]
        candidates = sorted(cdir.glob(f"{project}-*"), reverse=True) if cdir.exists() else []
        latest = str(candidates[0]) if candidates else None
        guide = [
            "1. 打开 compose 备份目录中的 compose 文件与 project.json。",
            "2. 在原项目 working_dir 恢复 compose 文件（如有改动）。",
            "3. 执行 docker compose pull && docker compose up -d（或 DockerOps 项目更新）。",
            "4. 双方接管同一项目目录，勿另起 docker run。",
        ]
    elif manager == "unraid":
        udir = backup_dir / "unraid"
        candidates = sorted(udir.glob(f"{name}-*"), reverse=True) if udir.exists() else []
        latest = str(candidates[0]) if candidates else None
        guide = [
            "1. 从备份目录恢复 my-*.xml 到 templates-user。",
            "2. 在 Unraid Docker 页 Edit → Apply，或 DockerOps 模板更新。",
            "3. 必须走模板重建以保持 net.unraid.docker.managed=dockerman。",
            "4. 不要用裸 docker run，否则会变成三方。",
        ]
    else:
        candidates = sorted(backup_dir.glob(f"{name}-*.json"), reverse=True) if backup_dir.exists() else []
        latest = str(candidates[0]) if candidates else None
        guide = [
            "1. 打开 latest_backup JSON，确认原 image / 挂载 / 端口。",
            "2. 建议先 Adopt 为 Unraid 模板或写入 compose，再回滚。",
            "3. 避免长期以三方方式裸跑。",
        ]

    rec = add_ops_record(
        action="rollback",
        target=name,
        status="recorded" if latest else "no_backup",
        detail={"latest_backup": latest, "manager": manager, "guide": guide},
        actor=actor,
    )
    return {
        "ok": bool(latest),
        "manager": manager,
        "record": rec,
        "latest_backup": latest,
        "message": "已记录回滚指引" if latest else "未找到可用备份，请先执行 backup",
    }

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from config import get_settings
from db import add_ops_record, list_ops_records
from docker_client import get_container, pull_image


def records(limit: int = 100) -> list[dict[str, Any]]:
    return list_ops_records(limit=limit)


def backup_container(container_id: str, actor: str | None = None) -> dict[str, Any]:
    """Record a backup snapshot (metadata + inspect dump) before update/rollback."""
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

    settings = get_settings()
    backup_dir = Path(settings.data_dir) / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%Y%m%d-%H%M%S")
    name = detail.get("name") or container_id
    path = backup_dir / f"{name}-{ts}.json"
    payload = {
        "created_at": time.time(),
        "actor": actor,
        "container": detail,
        "note": "元数据备份（inspect）。镜像层与数据卷需结合宿主机卷策略恢复。",
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    rec = add_ops_record(
        action="backup",
        target=name,
        status="ok",
        detail={"path": str(path), "image": detail.get("image"), "id": detail.get("id")},
        actor=actor,
    )
    return {
        "ok": True,
        "record": rec,
        "backup_path": str(path),
        "message": "已写入备份元数据，可用于追溯与回滚指引。",
    }


def safe_update(container_id: str, image: str | None = None, actor: str | None = None) -> dict[str, Any]:
    """
    Safe update flow:
    1) backup metadata
    2) pull new image
    3) record update intent / result

    Note: full recreate is environment-specific; we record steps and pull image,
    leaving recreate policy explicit for NAS users (compose/stack).
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
        return {"ok": False, "record": rec, "message": "容器不存在"}

    name = detail.get("name") or container_id
    target_image = image or detail.get("image")
    if not target_image:
        rec = add_ops_record(
            action="update",
            target=name,
            status="failed",
            detail={"error": "no_image"},
            actor=actor,
        )
        return {"ok": False, "record": rec, "message": "无法解析镜像名"}

    backup = backup_container(container_id, actor=actor)
    if not backup.get("ok"):
        return {"ok": False, "message": "备份失败，已中止更新", "backup": backup}

    pull_result: dict[str, Any]
    try:
        pull_result = pull_image(target_image)
        pull_ok = True
        pull_err = None
    except Exception as e:
        pull_result = {}
        pull_ok = False
        pull_err = str(e)

    status = "ok" if pull_ok else "failed"
    rec = add_ops_record(
        action="update",
        target=name,
        status=status,
        detail={
            "image": target_image,
            "backup_path": backup.get("backup_path"),
            "pull": pull_result,
            "error": pull_err,
            "next_steps": [
                "镜像已拉取（若成功）。",
                "请使用原有 compose/stack 重建容器以应用新镜像。",
                "失败时根据 backup 元数据与镜像 tag 回退。",
            ],
        },
        actor=actor,
    )
    return {
        "ok": pull_ok,
        "record": rec,
        "backup": backup,
        "pull": pull_result,
        "message": "安全更新流程完成（备份 + 拉镜像）" if pull_ok else f"拉镜像失败：{pull_err}",
    }


def rollback_guide(container_id: str, actor: str | None = None) -> dict[str, Any]:
    """Record a rollback action with latest backup pointer and guidance."""
    settings = get_settings()
    backup_dir = Path(settings.data_dir) / "backups"
    name = container_id
    try:
        detail = get_container(container_id)
        name = detail.get("name") or container_id
    except Exception:
        detail = None

    candidates = sorted(backup_dir.glob(f"{name}-*.json"), reverse=True) if backup_dir.exists() else []
    latest = str(candidates[0]) if candidates else None
    rec = add_ops_record(
        action="rollback",
        target=name,
        status="recorded" if latest else "no_backup",
        detail={
            "latest_backup": latest,
            "guide": [
                "1. 打开 latest_backup JSON，确认原 image / 挂载 / 端口。",
                "2. 将 compose 或 run 参数回退到备份中的镜像 tag。",
                "3. 重建容器并验证 Doctor 健康分。",
                "4. 数据卷本身通常不在镜像内，请勿误删命名卷。",
            ],
        },
        actor=actor,
    )
    return {
        "ok": bool(latest),
        "record": rec,
        "latest_backup": latest,
        "message": "已记录回滚指引" if latest else "未找到可用备份，请先执行 backup",
    }

"""Portainer-like daily resource operations (engine-level, cross-platform)."""

from __future__ import annotations

from typing import Any

from config import get_settings
from db import add_ops_record, audit
from docker_client import (
    container_action_remove,
    container_action_stop,
    create_network,
    create_volume,
    kill_container,
    list_images,
    list_networks,
    list_volumes,
    pause_container,
    prune_images,
    prune_volumes,
    pull_image,
    remove_image,
    remove_network,
    remove_volume,
    restart_container,
    start_container,
    system_df,
    system_info,
    system_prune,
    unpause_container,
)


def _resource_guard() -> None:
    if not get_settings().resource_apis:
        raise PermissionError("资源 API 已关闭（DOCKEROPS_RESOURCE_APIS=false）")


def _takeover_guard() -> None:
    get_settings().takeover_guard()


def _record(action: str, target: str, status: str, detail: dict, actor: str | None) -> dict:
    rec = add_ops_record(action=action, target=target, status=status, detail=detail, actor=actor)
    audit(action, actor=actor or "unknown", detail={"target": target, "status": status, **detail})
    return rec


# ── Lifecycle (login; remove needs takeover) ───────────────


def lifecycle(action: str, container_id: str, actor: str | None = None, **kwargs: Any) -> dict[str, Any]:
    _resource_guard()
    action = action.lower().strip()
    try:
        if action == "start":
            item = start_container(container_id)
        elif action == "stop":
            item = container_action_stop(container_id, timeout=int(kwargs.get("timeout") or 20))
        elif action == "restart":
            item = restart_container(container_id, timeout=int(kwargs.get("timeout") or 20))
        elif action == "pause":
            item = pause_container(container_id)
        elif action == "unpause":
            item = unpause_container(container_id)
        elif action == "kill":
            item = kill_container(container_id, signal=str(kwargs.get("signal") or "SIGKILL"))
        elif action == "remove":
            _takeover_guard()
            item = container_action_remove(
                container_id,
                force=bool(kwargs.get("force", False)),
                volumes=bool(kwargs.get("volumes", False)),
            )
        else:
            return {"ok": False, "message": f"未知动作: {action}"}
    except KeyError:
        rec = _record(f"container_{action}", container_id, "failed", {"error": "not_found"}, actor)
        return {"ok": False, "record": rec, "message": "容器不存在"}
    except PermissionError as e:
        raise
    except Exception as e:
        rec = _record(f"container_{action}", container_id, "failed", {"error": str(e)}, actor)
        return {"ok": False, "record": rec, "message": str(e)}

    name = item.get("name") or container_id
    rec = _record(f"container_{action}", name, "ok", {"id": item.get("id"), "state": item.get("status") or item.get("state")}, actor)
    return {"ok": True, "item": item, "record": rec, "message": f"{action} 完成：{name}"}


# ── Images ─────────────────────────────────────────────────


def images_list() -> dict[str, Any]:
    _resource_guard()
    items = list_images()
    return {"ok": True, "count": len(items), "items": items}


def images_pull(image: str, actor: str | None = None) -> dict[str, Any]:
    _resource_guard()
    try:
        result = pull_image(image)
        rec = _record("image_pull", image, "ok", result, actor)
        return {"ok": True, "result": result, "record": rec, "message": f"已拉取 {image}"}
    except Exception as e:
        rec = _record("image_pull", image, "failed", {"error": str(e)}, actor)
        return {"ok": False, "record": rec, "message": str(e)}


def images_remove(image_id: str, actor: str | None = None, force: bool = False) -> dict[str, Any]:
    _resource_guard()
    _takeover_guard()
    try:
        result = remove_image(image_id, force=force)
        rec = _record("image_remove", image_id, "ok", result, actor)
        return {"ok": True, "result": result, "record": rec, "message": f"已删除镜像 {image_id}"}
    except PermissionError:
        raise
    except Exception as e:
        rec = _record("image_remove", image_id, "failed", {"error": str(e)}, actor)
        return {"ok": False, "record": rec, "message": str(e)}


def images_prune(actor: str | None = None, dangling: bool = True) -> dict[str, Any]:
    _resource_guard()
    _takeover_guard()
    try:
        result = prune_images(dangling=dangling)
        rec = _record("image_prune", "images", "ok", result, actor)
        return {
            "ok": True,
            "result": result,
            "record": rec,
            "message": f"镜像清理完成，回收 {result.get('space_reclaimed') or 0} 字节",
        }
    except PermissionError:
        raise
    except Exception as e:
        rec = _record("image_prune", "images", "failed", {"error": str(e)}, actor)
        return {"ok": False, "record": rec, "message": str(e)}


# ── Networks ───────────────────────────────────────────────


def networks_list() -> dict[str, Any]:
    _resource_guard()
    items = list_networks()
    return {"ok": True, "count": len(items), "items": items}


def networks_create(name: str, actor: str | None = None, **kwargs: Any) -> dict[str, Any]:
    _resource_guard()
    _takeover_guard()
    try:
        item = create_network(name, **kwargs)
        rec = _record("network_create", name, "ok", item, actor)
        return {"ok": True, "item": item, "record": rec, "message": f"网络已创建：{name}"}
    except PermissionError:
        raise
    except Exception as e:
        rec = _record("network_create", name, "failed", {"error": str(e)}, actor)
        return {"ok": False, "record": rec, "message": str(e)}


def networks_remove(network_id: str, actor: str | None = None) -> dict[str, Any]:
    _resource_guard()
    _takeover_guard()
    try:
        item = remove_network(network_id)
        rec = _record("network_remove", network_id, "ok", item, actor)
        return {"ok": True, "item": item, "record": rec, "message": f"网络已删除：{item.get('name') or network_id}"}
    except PermissionError:
        raise
    except Exception as e:
        rec = _record("network_remove", network_id, "failed", {"error": str(e)}, actor)
        return {"ok": False, "record": rec, "message": str(e)}


# ── Volumes ────────────────────────────────────────────────


def volumes_list() -> dict[str, Any]:
    _resource_guard()
    items = list_volumes()
    return {"ok": True, "count": len(items), "items": items}


def volumes_create(name: str, actor: str | None = None, **kwargs: Any) -> dict[str, Any]:
    _resource_guard()
    _takeover_guard()
    try:
        item = create_volume(name, **kwargs)
        rec = _record("volume_create", name, "ok", item, actor)
        return {"ok": True, "item": item, "record": rec, "message": f"卷已创建：{name}"}
    except PermissionError:
        raise
    except Exception as e:
        rec = _record("volume_create", name, "failed", {"error": str(e)}, actor)
        return {"ok": False, "record": rec, "message": str(e)}


def volumes_remove(name: str, actor: str | None = None, force: bool = False) -> dict[str, Any]:
    _resource_guard()
    _takeover_guard()
    try:
        item = remove_volume(name, force=force)
        rec = _record("volume_remove", name, "ok", item, actor)
        return {"ok": True, "item": item, "record": rec, "message": f"卷已删除：{name}"}
    except PermissionError:
        raise
    except Exception as e:
        rec = _record("volume_remove", name, "failed", {"error": str(e)}, actor)
        return {"ok": False, "record": rec, "message": str(e)}


def volumes_prune(actor: str | None = None) -> dict[str, Any]:
    _resource_guard()
    _takeover_guard()
    try:
        result = prune_volumes()
        rec = _record("volume_prune", "volumes", "ok", result, actor)
        return {
            "ok": True,
            "result": result,
            "record": rec,
            "message": f"卷清理完成，回收 {result.get('space_reclaimed') or 0} 字节",
        }
    except PermissionError:
        raise
    except Exception as e:
        rec = _record("volume_prune", "volumes", "failed", {"error": str(e)}, actor)
        return {"ok": False, "record": rec, "message": str(e)}


# ── System ─────────────────────────────────────────────────


def sys_info() -> dict[str, Any]:
    _resource_guard()
    return {"ok": True, "info": system_info()}


def sys_df() -> dict[str, Any]:
    _resource_guard()
    return {"ok": True, "df": system_df()}


def sys_prune(actor: str | None = None, **kwargs: Any) -> dict[str, Any]:
    _resource_guard()
    _takeover_guard()
    try:
        result = system_prune(**kwargs)
        rec = _record("system_prune", "docker", "ok", result, actor)
        return {
            "ok": True,
            "result": result,
            "record": rec,
            "message": f"系统清理完成，回收 {result.get('space_reclaimed') or 0} 字节",
        }
    except PermissionError:
        raise
    except Exception as e:
        rec = _record("system_prune", "docker", "failed", {"error": str(e)}, actor)
        return {"ok": False, "record": rec, "message": str(e)}

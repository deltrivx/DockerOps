"""Per-container auto-update policy.

The platform-level switch says "check for updates on a schedule". That is not
the same question as "may this container be replaced automatically": some
containers are load-bearing (the reverse proxy, the storage array, the database
everything else talks to), and a schedule that silently recreates them is a
schedule that silently causes an outage.

So the policy is per container, and it also records *how* the container is
managed, because that determines whether an update can be applied at all:

``unraid``
    Rebuilt through the host's template scripts. Never by hand — see
    ``unraid_host`` for why.
``compose``
    Rebuilt by re-running the project it belongs to.
``third_party``
    No manager to rebuild it. An update can only pull the image; replacing the
    container would lose whatever created it.

Entries that cannot be applied are *reported*, not silently skipped: a user who
ticks a box and sees nothing happen has been told nothing.
"""
from __future__ import annotations

import json
import time
from typing import Any

from db import get_meta, set_meta

META_UPDATE_POLICY = "update_policy_v1"

# Containers never auto-updated regardless of policy, because replacing them
# while DockerOps itself runs inside one of them is self-defeating.
SELF_NAMES = {
    "dockerops",
    "dockerops-dev",
}

VALID_ACTIONS = ("auto", "notify", "ignore")


def _load() -> dict[str, Any]:
    raw = get_meta(META_UPDATE_POLICY)
    if not raw:
        return {"containers": {}, "default_action": "notify"}
    try:
        data = json.loads(raw)
        if not isinstance(data, dict):
            return {"containers": {}, "default_action": "notify"}
        data.setdefault("containers", {})
        data.setdefault("default_action", "notify")
        return data
    except Exception:
        return {"containers": {}, "default_action": "notify"}


def _save(data: dict[str, Any]) -> None:
    set_meta(META_UPDATE_POLICY, json.dumps(data, ensure_ascii=False))


def _normalize_key(name: str) -> str:
    return (name or "").lstrip("/").strip()


def _manager_can_recreate(manager: str | None, detail: dict[str, Any] | None = None) -> tuple[bool, str]:
    """Whether an update can actually replace the container, and why not."""
    m = (manager or "third_party").strip().lower()
    if m == "unraid":
        return True, ""
    if m == "compose":
        return True, ""
    return (
        False,
        "该容器不由任何管理器创建，自动更新只能拉取镜像，无法重建；"
        "请改用对应的 compose/模板方式管理后再开启。",
    )


def _is_self(name: str) -> bool:
    return _normalize_key(name).lower() in SELF_NAMES


def get_policy() -> dict[str, Any]:
    """Current policy, including a resolved view for the UI."""
    data = _load()
    return {
        "ok": True,
        "default_action": data.get("default_action", "notify"),
        "containers": data.get("containers", {}),
        "self_protected": sorted(SELF_NAMES),
    }


def set_policy(
    name: str,
    *,
    action: str,
    manager: str | None = None,
    image: str | None = None,
    actor: str | None = None,
) -> dict[str, Any]:
    """Set one container's auto-update action."""
    key = _normalize_key(name)
    if not key:
        raise ValueError("name_required")
    action = (action or "").strip().lower()
    if action not in VALID_ACTIONS:
        raise ValueError(f"action 必须是 {', '.join(VALID_ACTIONS)} 之一")

    data = _load()
    entry = {
        "action": action,
        "manager": (manager or "").strip().lower(),
        "image": image or "",
        "updated_at": time.time(),
    }

    blocked_by = ""
    if _is_self(key):
        # Asking to auto-update DockerOps is asking to restart the thing doing
        # the updating; store the choice but never act on it.
        entry["blocked"] = "self"
        entry["blocked_reason"] = "这是 DockerOps 自身容器，自动更新会在更新过程中中断自身"
        blocked_by = entry["blocked_reason"]
    elif action == "auto":
        can, why = _manager_can_recreate(entry["manager"])
        if not can:
            entry["blocked"] = "no_manager"
            entry["blocked_reason"] = why
            blocked_by = why

    data["containers"][key] = entry
    _save(data)

    if actor:
        try:
            from db import audit

            audit(
                "update_policy_set",
                actor=actor,
                detail={"container": key, "action": action, "blocked": bool(blocked_by)},
            )
        except Exception:
            pass

    return {
        "ok": True,
        "container": key,
        "entry": entry,
        "blocked": bool(blocked_by),
        "message": (
            f"已设置「{key}」为自动更新"
            if action == "auto" and not blocked_by
            else (blocked_by or f"已设置「{key}」为{action}")
        ),
    }


def set_policy_bulk(
    items: list[dict[str, Any]], *, actor: str | None = None
) -> dict[str, Any]:
    """Apply several policy changes; each is independent.

    Deliberately not transactional across containers: a bad entry should not
    discard the good ones the user set in the same action.
    """
    results: list[dict[str, Any]] = []
    for it in items or []:
        try:
            r = set_policy(
                it.get("name") or "",
                action=it.get("action") or "",
                manager=it.get("manager"),
                image=it.get("image"),
                actor=actor,
            )
            results.append({**r, "name": it.get("name")})
        except Exception as e:
            results.append(
                {"ok": False, "name": it.get("name"), "message": str(e)}
            )
    return {
        "ok": all(r.get("ok") for r in results) if results else True,
        "results": results,
        "applied": sum(1 for r in results if r.get("ok")),
        "total": len(results),
    }


def resolve_action(name: str, manager: str | None = None) -> dict[str, Any]:
    """What should be done for this container, and whether it is permitted."""
    key = _normalize_key(name)
    data = _load()
    entry = (data.get("containers") or {}).get(key)
    if not entry:
        return {
            "name": key,
            "action": data.get("default_action", "notify"),
            "source": "default",
            "allowed": False,
            "reason": "未单独设置，使用平台默认策略",
        }

    action = entry.get("action") or "notify"
    can, why = _manager_can_recreate(manager or entry.get("manager"))
    self_blocked = _is_self(key)

    allowed = action == "auto" and can and not self_blocked
    reason = ""
    if self_blocked:
        reason = "DockerOps 自身容器，不参与自动更新"
    elif action == "auto" and not can:
        reason = why
    elif action != "auto":
        reason = f"策略为 {action}"

    return {
        "name": key,
        "action": action,
        "source": "stored",
        "manager": entry.get("manager") or (manager or ""),
        "allowed": allowed,
        "reason": reason,
        "updated_at": entry.get("updated_at"),
    }


def eligible_targets(detected: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Split detected update candidates into apply / skip, with skip reasons.

    Callers must be able to show why a container was skipped; silently dropping
    candidates is how a schedule appears to work while doing nothing.
    """
    apply: list[dict[str, Any]] = []
    skip: list[dict[str, Any]] = []
    for d in detected or []:
        if not d.get("update_available"):
            continue
        res = resolve_action(d.get("name") or d.get("id") or "", d.get("manager"))
        if res["allowed"]:
            apply.append({**d, "policy": res})
        else:
            skip.append({**d, "policy": res, "skip_reason": res["reason"]})
    return apply, skip


def prune_missing(existing_names: set[str]) -> int:
    """Drop policy entries for containers that no longer exist."""
    data = _load()
    containers = data.get("containers") or {}
    gone = [k for k in containers if k not in existing_names]
    for k in gone:
        containers.pop(k, None)
    if gone:
        _save(data)
    return len(gone)


__all__ = [
    "META_UPDATE_POLICY",
    "VALID_ACTIONS",
    "eligible_targets",
    "get_policy",
    "prune_missing",
    "resolve_action",
    "set_policy",
    "set_policy_bulk",
]

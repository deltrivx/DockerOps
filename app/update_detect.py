"""Image update detection and one-click safe update batching.

Compares local image digests with registry remote digests via Docker
`inspect_distribution` (no full image download for check).

Persists last scan to app_meta (`update_status`) for Unraid-style list badges
without requiring a manual detect click every time.
"""
from __future__ import annotations

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from config import get_settings
from db import add_ops_record, audit, get_meta, set_meta
from docker_client import get_client, list_containers, pull_image
from ops import safe_update

META_UPDATE_STATUS = "update_status"
META_UPDATE_SETTINGS = "update_settings"

_bg_lock = threading.Lock()
_bg_running = False


def _local_digests(image_ref: str) -> set[str]:
    """Return RepoDigests / Id digests for a local image reference."""
    c = get_client()
    digests: set[str] = set()
    try:
        img = c.images.get(image_ref)
    except Exception:
        # try without tag ambiguity
        try:
            imgs = c.images.list(name=image_ref.split(":")[0] if ":" in image_ref else image_ref)
            if not imgs:
                return digests
            img = imgs[0]
        except Exception:
            return digests

    attrs = img.attrs or {}
    for d in attrs.get("RepoDigests") or []:
        if "@" in d:
            digests.add(d.split("@", 1)[1])
        else:
            digests.add(d)
    img_id = attrs.get("Id") or img.id
    if img_id:
        digests.add(img_id.replace("sha256:", "") if not img_id.startswith("sha256:") else img_id)
        if img_id.startswith("sha256:"):
            digests.add(img_id)
    return digests


def _remote_digest(image_ref: str) -> tuple[str | None, str | None]:
    """
    Query registry for remote digest without pulling layers.
    Returns (digest, error).
    """
    c = get_client()
    try:
        # docker-py low-level API
        info = c.api.inspect_distribution(image_ref)
        # Descriptor.digest preferred
        desc = (info or {}).get("Descriptor") or {}
        digest = desc.get("digest")
        if not digest:
            # Platforms may carry digests
            for p in (info or {}).get("Platforms") or []:
                d = (p or {}).get("digest") or ((p or {}).get("Descriptor") or {}).get("digest")
                if d:
                    digest = d
                    break
        return digest, None
    except Exception as e:
        return None, str(e)


def _normalize_image(image: str | None) -> str | None:
    if not image:
        return None
    s = image.strip()
    if not s or s.startswith("sha256:"):
        return None
    # skip images without registry tag that are purely local builds with no name
    if s.startswith("<") or s == "none":
        return None
    return s


def check_container_update(container: dict[str, Any]) -> dict[str, Any]:
    """Check a single container summary/detail for image updates."""
    image = _normalize_image(container.get("image"))
    name = (container.get("name") or container.get("id") or "").lstrip("/")
    base: dict[str, Any] = {
        "id": container.get("id"),
        "name": name,
        "image": image,
        "manager": container.get("manager") or "third_party",
        "status": container.get("status"),
        "update_available": False,
        "check_ok": False,
        "local_digests": [],
        "remote_digest": None,
        "message": "",
        "error": None,
    }
    if not image:
        base["message"] = "无有效镜像引用，跳过"
        base["error"] = "no_image"
        return base

    local = _local_digests(image)
    base["local_digests"] = sorted(local)[:8]

    remote, err = _remote_digest(image)
    if err:
        base["error"] = err
        base["message"] = f"检测失败：{err}"
        # still mark check attempted
        return base

    base["remote_digest"] = remote
    base["check_ok"] = True

    if not remote:
        base["message"] = "仓库未返回 digest"
        base["error"] = "no_remote_digest"
        return base

    # match if any local digest equals remote, or remote is contained in any local ref
    remote_short = remote.replace("sha256:", "")
    matched = False
    for d in local:
        ds = d.replace("sha256:", "")
        if d == remote or ds == remote_short or remote in d or d in remote:
            matched = True
            break
        # RepoDigests style may be repo@sha256:...
        if remote_short and remote_short in d:
            matched = True
            break

    if matched:
        base["update_available"] = False
        base["message"] = "已是最新"
    else:
        # If local has no digests (only image id), still flag if remote differs from id
        if not local:
            base["update_available"] = True
            base["message"] = "本地无 RepoDigest，建议更新"
        else:
            base["update_available"] = True
            base["message"] = "发现新版本"

    return base


def _save_update_status(result: dict[str, Any], only_running: bool) -> None:
    """Persist last detect map for Unraid-style list badges."""
    if not result.get("ok"):
        return
    by_id: dict[str, Any] = {}
    for item in result.get("items") or []:
        cid = item.get("id")
        if not cid:
            continue
        by_id[str(cid)] = {
            "id": cid,
            "name": item.get("name"),
            "image": item.get("image"),
            "manager": item.get("manager"),
            "status": item.get("status"),
            "update_available": bool(item.get("update_available")),
            "check_ok": bool(item.get("check_ok")),
            "message": item.get("message"),
            "remote_digest": item.get("remote_digest"),
            "error": item.get("error"),
        }
        # also index by short id and name for UI matching
        short = str(cid)[:12]
        by_id[short] = by_id[str(cid)]
        name = (item.get("name") or "").lstrip("/")
        if name:
            by_id[f"name:{name}"] = by_id[str(cid)]

    payload = {
        "checked_at": time.time(),
        "only_running": only_running,
        "summary": {
            "scanned": result.get("scanned"),
            "update_available_count": result.get("update_available_count"),
            "up_to_date_count": result.get("up_to_date_count"),
            "check_failed_count": result.get("check_failed_count"),
            "elapsed_sec": result.get("elapsed_sec"),
            "message": result.get("message"),
        },
        "items": result.get("items") or [],
        "by_id": by_id,
    }
    try:
        set_meta(META_UPDATE_STATUS, json.dumps(payload, ensure_ascii=False))
    except Exception:
        pass


def get_cached_update_status() -> dict[str, Any]:
    """Fast read of last detect cache (no registry calls)."""
    raw = get_meta(META_UPDATE_STATUS)
    if not raw:
        return {
            "ok": True,
            "cached": False,
            "checked_at": None,
            "summary": {},
            "items": [],
            "by_id": {},
            "update_available_count": 0,
            "message": "尚未检测",
        }
    try:
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError("bad cache")
        summary = data.get("summary") or {}
        return {
            "ok": True,
            "cached": True,
            "checked_at": data.get("checked_at"),
            "only_running": data.get("only_running"),
            "summary": summary,
            "items": data.get("items") or [],
            "by_id": data.get("by_id") or {},
            "update_available_count": summary.get("update_available_count") or 0,
            "message": summary.get("message") or "已缓存",
        }
    except Exception:
        return {
            "ok": True,
            "cached": False,
            "checked_at": None,
            "summary": {},
            "items": [],
            "by_id": {},
            "update_available_count": 0,
            "message": "缓存无效",
        }


def get_update_auto_settings() -> dict[str, Any]:
    """Merge env defaults with optional SQLite overrides."""
    s = get_settings()
    base = {
        "auto_check_enabled": bool(s.update_auto_check),
        "auto_check_interval_hours": int(s.update_check_interval_hours),
        "startup_delay_sec": int(s.update_check_startup_delay_sec),
        "source": "env",
    }
    raw = get_meta(META_UPDATE_SETTINGS)
    if raw:
        try:
            data = json.loads(raw)
            if isinstance(data, dict):
                if "auto_check_enabled" in data:
                    base["auto_check_enabled"] = bool(data["auto_check_enabled"])
                if "auto_check_interval_hours" in data:
                    try:
                        n = int(data["auto_check_interval_hours"])
                        base["auto_check_interval_hours"] = max(1, min(48, n))
                    except Exception:
                        pass
                base["source"] = "stored"
        except Exception:
            pass
    return base


def set_update_auto_settings(
    *,
    auto_check_enabled: bool | None = None,
    auto_check_interval_hours: int | None = None,
) -> dict[str, Any]:
    cur = get_update_auto_settings()
    if auto_check_enabled is not None:
        cur["auto_check_enabled"] = bool(auto_check_enabled)
    if auto_check_interval_hours is not None:
        cur["auto_check_interval_hours"] = max(1, min(48, int(auto_check_interval_hours)))
    store = {
        "auto_check_enabled": cur["auto_check_enabled"],
        "auto_check_interval_hours": cur["auto_check_interval_hours"],
    }
    set_meta(META_UPDATE_SETTINGS, json.dumps(store, ensure_ascii=False))
    cur["source"] = "stored"
    return cur


def detect_updates(
    container_ids: list[str] | None = None,
    only_running: bool = False,
    actor: str | None = None,
    *,
    persist: bool = True,
) -> dict[str, Any]:
    """Scan containers for available image updates (parallel digest checks)."""
    started = time.time()
    try:
        items = list_containers(all_containers=True)
    except Exception as e:
        return {"ok": False, "message": f"列举容器失败：{e}", "items": [], "count": 0}

    if only_running:
        items = [c for c in items if (c.get("status") or "").lower() == "running"]

    if container_ids:
        idset = set(container_ids)
        items = [
            c
            for c in items
            if c.get("id") in idset
            or (c.get("name") or "").lstrip("/") in idset
            or any((c.get("id") or "").startswith(x) for x in idset)
        ]

    results: list[dict[str, Any]] = []
    # Parallel registry checks (I/O bound)
    workers = min(8, max(1, len(items)))
    if items:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futs = {pool.submit(check_container_update, c): c for c in items}
            for fut in as_completed(futs):
                try:
                    results.append(fut.result())
                except Exception as e:
                    c = futs[fut]
                    results.append(
                        {
                            "id": c.get("id"),
                            "name": (c.get("name") or "").lstrip("/"),
                            "image": c.get("image"),
                            "manager": c.get("manager"),
                            "status": c.get("status"),
                            "update_available": False,
                            "check_ok": False,
                            "message": f"检测异常：{e}",
                            "error": str(e),
                        }
                    )
    # stable order by name
    results.sort(key=lambda r: (r.get("name") or r.get("id") or ""))

    available = [r for r in results if r.get("update_available")]
    failed = [r for r in results if r.get("error") and not r.get("check_ok")]
    up_to_date = [r for r in results if r.get("check_ok") and not r.get("update_available")]

    audit(
        "update_detect",
        actor=actor,
        detail={
            "scanned": len(results),
            "available": len(available),
            "failed": len(failed),
            "elapsed": round(time.time() - started, 2),
        },
    )

    out = {
        "ok": True,
        "scanned": len(results),
        "update_available_count": len(available),
        "up_to_date_count": len(up_to_date),
        "check_failed_count": len(failed),
        "elapsed_sec": round(time.time() - started, 2),
        "items": results,
        "available": available,
        "message": (
            f"扫描 {len(results)} 个容器：{len(available)} 个可更新，"
            f"{len(up_to_date)} 个最新，{len(failed)} 个检测失败"
        ),
    }
    if persist:
        _save_update_status(out, only_running=only_running)
    return out


def one_click_update(
    container_ids: list[str] | None = None,
    only_available: bool = True,
    only_running: bool = False,
    actor: str | None = None,
) -> dict[str, Any]:
    """
    Detect then safe-update selected (or all available) containers.
    Uses existing manager-aware safe_update path.
    """
    detect = detect_updates(container_ids=container_ids, only_running=only_running, actor=actor)
    if not detect.get("ok"):
        return detect

    targets = detect.get("available") if only_available else detect.get("items")
    if container_ids and not only_available:
        targets = detect.get("items")
    elif container_ids and only_available:
        # intersect available with requested ids if detect already filtered
        targets = detect.get("available") or []

    targets = targets or []
    results: list[dict[str, Any]] = []
    ok_n = 0
    fail_n = 0

    for t in targets:
        cid = t.get("id") or t.get("name")
        if not cid:
            continue
        if only_available and not t.get("update_available"):
            continue
        try:
            r = safe_update(cid, image=t.get("image"), actor=actor)
            success = bool(r.get("ok"))
            if success:
                ok_n += 1
            else:
                fail_n += 1
            results.append(
                {
                    "id": cid,
                    "name": t.get("name"),
                    "image": t.get("image"),
                    "manager": t.get("manager"),
                    "ok": success,
                    "message": r.get("message"),
                    "result": r,
                }
            )
        except Exception as e:
            fail_n += 1
            results.append(
                {
                    "id": cid,
                    "name": t.get("name"),
                    "ok": False,
                    "message": str(e),
                }
            )

    rec = add_ops_record(
        action="one_click_update",
        target=f"{len(results)} containers",
        status="ok" if fail_n == 0 else ("partial" if ok_n else "failed"),
        detail={"ok": ok_n, "failed": fail_n, "names": [x.get("name") for x in results]},
        actor=actor,
    )

    return {
        "ok": fail_n == 0,
        "detect": {
            "scanned": detect.get("scanned"),
            "update_available_count": detect.get("update_available_count"),
            "message": detect.get("message"),
        },
        "updated_ok": ok_n,
        "updated_failed": fail_n,
        "results": results,
        "record": rec,
        "message": f"一键更新完成：成功 {ok_n}，失败 {fail_n}",
    }


def pull_only_if_update(image: str, actor: str | None = None) -> dict[str, Any]:
    """Helper: check single image ref and pull if outdated."""
    fake = {"id": None, "name": image, "image": image, "manager": "third_party", "status": "-"}
    check = check_container_update(fake)
    if not check.get("update_available"):
        return {"ok": True, "pulled": False, "check": check, "message": check.get("message")}
    try:
        pull = pull_image(image)
        return {"ok": True, "pulled": True, "check": check, "pull": pull, "message": f"已拉取 {image}"}
    except Exception as e:
        return {"ok": False, "pulled": False, "check": check, "message": str(e)}


def run_background_detect_once(actor: str = "system") -> dict[str, Any]:
    """Silent detect used by scheduler (running containers only)."""
    return detect_updates(only_running=True, actor=actor, persist=True)


def start_update_auto_check_thread() -> None:
    """Daemon thread: delay then periodic digest scan (Unraid-style)."""
    global _bg_running
    with _bg_lock:
        if _bg_running:
            return
        _bg_running = True

    def _loop() -> None:
        s0 = get_settings()
        time.sleep(max(5, int(s0.update_check_startup_delay_sec or 60)))
        while True:
            try:
                cfg = get_update_auto_settings()
                if cfg.get("auto_check_enabled"):
                    try:
                        run_background_detect_once(actor="system-auto")
                    except Exception as e:
                        try:
                            audit("update_auto_check_error", actor="system", detail={"error": str(e)})
                        except Exception:
                            pass
                hours = int(cfg.get("auto_check_interval_hours") or 6)
                time.sleep(max(3600, hours * 3600))
            except Exception:
                time.sleep(3600)

    t = threading.Thread(target=_loop, name="dockerops-update-auto-check", daemon=True)
    t.start()

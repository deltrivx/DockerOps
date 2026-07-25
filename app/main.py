from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

from auth import (
    AuthUser,
    OptionalUser,
    auth_status,
    bootstrap_from_env,
    change_password,
    complete_setup,
    login,
    logout,
)
from compose_mgr import (
    backup_project,
    get_project,
    list_projects,
    project_down,
    project_up,
    safe_update_project,
)
from config import get_settings
from db import audit, init_db
from docker_client import get_container, list_containers, ping, refresh_template_name_cache
from docker_resources import (
    activity_stats,
    batch_lifecycle,
    images_list,
    images_prune,
    images_pull,
    images_remove,
    lifecycle,
    networks_create,
    networks_list,
    networks_remove,
    sys_df,
    sys_info,
    sys_prune,
    volumes_create,
    volumes_list,
    volumes_prune,
    volumes_remove,
)
from db import get_meta, set_meta
from docker_client import image_history, list_running_stats
from doctor import diagnose_all, diagnose_one
from events_stream import recent_events, sse_docker_events
from host_platform import platform_info
from logs_stream import get_logs, sse_log_events
from manager import managers_summary
from monitor import collect_report, get_latest_or_collect
from ops import backup_container, records, rollback_guide, safe_update
from unraid_mgr import (
    adopt_to_unraid,
    backup_template,
    get_template,
    list_templates,
    safe_update_unraid,
    templates_available,
)
from update_detect import detect_updates, one_click_update

APP_DIR = Path(__file__).resolve().parent
VERSION = "0.4.4"
CHANGELOG = [
    {
        "version": "0.4.4",
        "date": "2026-07-26",
        "items": [
            "修复首屏卡顿：活动容器 stats 不再阻塞总览（延后加载 + 并行采样）",
            "loadAll 竞态保护与顶部加载提示，避免显示异常后才刷新",
            "activity 默认采样并行化，超时保护",
        ],
    },
    {
        "version": "0.4.3",
        "date": "2026-07-26",
        "items": [
            "打开网页：无账号强制首次初始化；有账号强制登录门（不可 Esc 绕过）",
            "登录页「忘记密码」展示终端改密命令（docker exec … python -m tools.reset_password）",
            "新增 /api/auth/me 会话校验；改密后旧会话失效",
            "内置 CLI：tools.reset_password 支持列表/重置/清空用户",
        ],
    },
    {
        "version": "0.4.2",
        "date": "2026-07-25",
        "items": [
            "去除默认账号：无 admin/dockerops 内置凭据，登录/安装向导不预填",
            "账号与会话/审计/偏好仅存内置 SQLite（/data/dockerops.db）",
            "可选 env 预置须同时显式设置 DOCKEROPS_ADMIN_USER + PASSWORD；Settings 空默认不 bootstrap",
            "Compose / Unraid 模板 / 飞牛配方不再注入默认管理员",
        ],
    },
    {
        "version": "0.4.1",
        "date": "2026-07-25",
        "items": [
            "修复手机端无法点击：侧栏遮罩 [hidden] 被 CSS 覆盖导致拦截触摸",
            "修复 PC 粒子不可见：提高亮度/连线密度，卡片半透明露出粒子层",
            "无 Compose 项目时自动隐藏侧栏 Compose（Unraid 无插件/纯 docker run 不显示）",
        ],
    },
    {
        "version": "0.4.0",
        "date": "2026-07-25",
        "items": [
            "运维控制台总览：平台/引擎卡片、健康分、系统信息、活动容器资源",
            "Unraid 风格模块卡片 + PC/平板/手机响应式",
            "粒子特效增强 + 背景个性化设置",
            "说明与更新日志独立菜单",
            "Portainer 日常补齐：批量启停/重启、重命名、活动 stats、镜像 history",
            "侧栏平台徽章：飞牛系统 / Unraid 系统",
        ],
    },
    {
        "version": "0.3.3",
        "date": "2026-07-25",
        "items": [
            "修复飞牛 FPK 桌面 127.0.0.1 黑屏（CGI 智能跳转）",
            "品牌图标补全；Unraid 容器名 DockerOps",
        ],
    },
    {
        "version": "0.3.2",
        "date": "2026-07-24",
        "items": [
            "Unraid 风格侧栏 UI + 博客粒子背景",
            "一键检测 / 一键更新（Registry digest）",
            "资源表搜索过滤",
        ],
    },
    {
        "version": "0.3.1",
        "date": "2026-07-24",
        "items": [
            "首次管理员设置向导",
            "飞牛专业 FPK Release 附件",
        ],
    },
    {
        "version": "0.3.0",
        "date": "2026-07-24",
        "items": [
            "日常运维全覆盖：生命周期/日志/镜像/网络/卷/系统/事件",
            "Compose + Unraid 模板双方接管",
        ],
    },
]
settings = get_settings()
init_db()

app = FastAPI(
    title="DockerOps",
    description=(
        "面向 NAS 的 Docker 运维平台 — 首次设置向导、日常运维（生命周期/日志/镜像/网络/卷）、"
        "Compose 与 Unraid/飞牛引擎级双方接管、安全更新、Doctor 诊断。"
    ),
    version=VERSION,
    docs_url="/docs",
    redoc_url="/redoc",
)

templates = Jinja2Templates(directory=str(APP_DIR / "templates"))
app.mount("/static", StaticFiles(directory=str(APP_DIR / "static")), name="static")


class LoginBody(BaseModel):
    username: str
    password: str


class SetupBody(BaseModel):
    username: str = Field(..., min_length=3, max_length=32, description="管理员用户名")
    password: str = Field(..., min_length=6, max_length=128, description="管理员密码")
    password_confirm: str = Field(..., min_length=6, max_length=128, description="确认密码")


class ChangePasswordBody(BaseModel):
    old_password: str
    new_password: str = Field(..., min_length=6, max_length=128)
    new_password_confirm: str


class UpdateBody(BaseModel):
    image: str | None = Field(default=None, description="目标镜像；Unraid 可写回模板 Repository")


class ComposeUpdateBody(BaseModel):
    service: str | None = Field(default=None, description="仅更新指定 service；空则整个项目")
    recreate: bool = True


class UnraidUpdateBody(BaseModel):
    repository: str | None = Field(default=None, description="新镜像，写入模板 Repository")
    recreate: bool = True


class PullBody(BaseModel):
    image: str = Field(..., description="镜像名:tag")


class NetworkCreateBody(BaseModel):
    name: str
    driver: str = "bridge"
    internal: bool = False
    attachable: bool = False


class VolumeCreateBody(BaseModel):
    name: str
    driver: str = "local"


class PruneBody(BaseModel):
    containers: bool = True
    images: bool = True
    volumes: bool = False
    networks: bool = True
    dangling_images_only: bool = True


class RemoveBody(BaseModel):
    force: bool = False
    volumes: bool = False


class BatchBody(BaseModel):
    action: str = Field(..., description="start|stop|restart|pause|unpause|kill|remove")
    ids: list[str] = Field(default_factory=list)
    force: bool = False
    volumes: bool = False


class RenameBody(BaseModel):
    name: str = Field(..., min_length=1, max_length=128)


class PrefsBody(BaseModel):
    particles: bool | None = None
    particles_count: int | None = Field(default=None, ge=20, le=300)
    bg_theme: str | None = None  # cyber | deep | aurora | plain
    card_density: str | None = None  # comfortable | compact
    reduce_motion: bool | None = None


def _takeover_or_403() -> None:
    try:
        get_settings().takeover_guard()
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e)) from e


def _resource_or_403() -> None:
    if not get_settings().resource_apis:
        raise HTTPException(status_code=403, detail="资源 API 已关闭（DOCKEROPS_RESOURCE_APIS=false）")


def _perm_http(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e)) from e
    except KeyError:
        raise HTTPException(status_code=404, detail="资源不存在") from None


@app.on_event("startup")
def _startup() -> None:
    settings.ensure_dirs()
    refresh_template_name_cache()
    boot = bootstrap_from_env()
    plat = {}
    try:
        plat = platform_info()
    except Exception as e:
        plat = {"error": str(e)}
    audit(
        "startup",
        actor="system",
        detail={
            "version": VERSION,
            "takeover_enabled": settings.takeover_enabled,
            "resource_apis": settings.resource_apis,
            "platform": plat.get("platform"),
            "unraid_templates": templates_available(),
            "bootstrap": boot,
            "needs_setup": auth_status().get("needs_setup"),
        },
    )


@app.get("/api/health")
def api_health() -> dict[str, Any]:
    engine = ping()
    try:
        plat = platform_info()
        platform_name = plat.get("platform")
    except Exception:
        platform_name = "unknown"
    return {
        "ok": True,
        "service": "dockerops",
        "version": VERSION,
        "time": time.time(),
        "docker": engine,
        "platform": platform_name,
        "takeover_enabled": get_settings().takeover_enabled,
        "resource_apis": get_settings().resource_apis,
        "unraid_templates_available": templates_available(),
    }


@app.get("/api/platform")
def api_platform(actor: OptionalUser = None) -> dict[str, Any]:
    info = platform_info()
    info["viewer"] = actor
    return info


@app.get("/api/managers/summary")
def api_managers_summary(actor: OptionalUser = None) -> dict[str, Any]:
    try:
        items = list_containers(all_containers=True)
    except Exception as e:
        items = []
        summary = managers_summary([])
        summary["docker_error"] = str(e)
        summary["viewer"] = actor
        return summary
    summary = managers_summary(items)
    summary["viewer"] = actor
    return summary


@app.get("/api/auth/status")
def api_auth_status() -> dict[str, Any]:
    return auth_status()


@app.get("/api/auth/me")
def api_auth_me(actor: AuthUser) -> dict[str, Any]:
    """Validate session token; used by Web 登录门."""
    return {
        "ok": True,
        "username": actor,
        "auth_store": "sqlite",
        "authenticated": True,
    }


@app.post("/api/auth/setup")
def api_auth_setup(body: SetupBody) -> dict[str, Any]:
    if body.password != body.password_confirm:
        raise HTTPException(status_code=400, detail="两次输入的密码不一致")
    return complete_setup(body.username, body.password)


@app.post("/api/auth/login")
def api_login(body: LoginBody) -> dict[str, Any]:
    return login(body.username, body.password)


@app.post("/api/auth/logout")
def api_logout(request: Request, actor: OptionalUser) -> dict[str, Any]:
    auth = request.headers.get("Authorization") or ""
    if auth.lower().startswith("bearer "):
        logout(auth.split(" ", 1)[1].strip())
    return {"ok": True, "actor": actor}


@app.post("/api/auth/change-password")
def api_change_password(body: ChangePasswordBody, actor: AuthUser) -> dict[str, Any]:
    if body.new_password != body.new_password_confirm:
        raise HTTPException(status_code=400, detail="两次输入的新密码不一致")
    if actor == "api-token":
        raise HTTPException(status_code=400, detail="API Token 不能修改密码，请使用管理员账号登录")
    return change_password(actor, body.old_password, body.new_password)


@app.get("/api/containers")
def api_containers(actor: OptionalUser) -> dict[str, Any]:
    try:
        items = list_containers(all_containers=True)
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"Docker 不可用：{e}") from e
    return {"ok": True, "count": len(items), "items": items, "viewer": actor}


@app.post("/api/containers/batch")
def api_batch(body: BatchBody, actor: AuthUser) -> dict[str, Any]:
    """Static path registered before {container_id} routes."""
    _resource_or_403()
    if not body.ids:
        raise HTTPException(status_code=400, detail="请选择至少一个容器")
    return _perm_http(
        batch_lifecycle,
        body.action,
        body.ids,
        actor=actor,
        force=body.force,
        volumes=body.volumes,
    )


@app.get("/api/containers/{container_id}")
def api_container(container_id: str, actor: OptionalUser) -> dict[str, Any]:
    try:
        item = get_container(container_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="容器不存在") from None
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"Docker 不可用：{e}") from e
    return {"ok": True, "item": item, "viewer": actor}


# ── Lifecycle ──────────────────────────────────────────────


@app.post("/api/containers/{container_id}/start")
def api_start(container_id: str, actor: AuthUser) -> dict[str, Any]:
    _resource_or_403()
    return _perm_http(lifecycle, "start", container_id, actor=actor)


@app.post("/api/containers/{container_id}/stop")
def api_stop(container_id: str, actor: AuthUser) -> dict[str, Any]:
    _resource_or_403()
    return _perm_http(lifecycle, "stop", container_id, actor=actor)


@app.post("/api/containers/{container_id}/restart")
def api_restart(container_id: str, actor: AuthUser) -> dict[str, Any]:
    _resource_or_403()
    return _perm_http(lifecycle, "restart", container_id, actor=actor)


@app.post("/api/containers/{container_id}/pause")
def api_pause(container_id: str, actor: AuthUser) -> dict[str, Any]:
    _resource_or_403()
    return _perm_http(lifecycle, "pause", container_id, actor=actor)


@app.post("/api/containers/{container_id}/unpause")
def api_unpause(container_id: str, actor: AuthUser) -> dict[str, Any]:
    _resource_or_403()
    return _perm_http(lifecycle, "unpause", container_id, actor=actor)


@app.post("/api/containers/{container_id}/kill")
def api_kill(container_id: str, actor: AuthUser) -> dict[str, Any]:
    _resource_or_403()
    return _perm_http(lifecycle, "kill", container_id, actor=actor)


@app.delete("/api/containers/{container_id}")
def api_remove_container(
    container_id: str,
    actor: AuthUser,
    force: bool = False,
    volumes: bool = False,
) -> dict[str, Any]:
    _resource_or_403()
    return _perm_http(lifecycle, "remove", container_id, actor=actor, force=force, volumes=volumes)


@app.post("/api/containers/{container_id}/rename")
def api_rename(container_id: str, body: RenameBody, actor: AuthUser) -> dict[str, Any]:
    _resource_or_403()
    return _perm_http(lifecycle, "rename", container_id, actor=actor, name=body.name.strip())


@app.get("/api/activity")
def api_activity(actor: OptionalUser, limit: int = Query(12, ge=1, le=40)) -> dict[str, Any]:
    _resource_or_403()
    try:
        data = activity_stats(limit=limit)
        data["viewer"] = actor
        return data
    except Exception as e:
        raise HTTPException(status_code=503, detail=str(e)) from e


@app.get("/api/containers/{container_id}/stats")
def api_container_stats(container_id: str, actor: OptionalUser) -> dict[str, Any]:
    _resource_or_403()
    try:
        from docker_client import container_stats

        item = container_stats(container_id)
        return {"ok": True, "item": item, "viewer": actor}
    except KeyError:
        raise HTTPException(status_code=404, detail="容器不存在") from None
    except Exception as e:
        raise HTTPException(status_code=503, detail=str(e)) from e


@app.get("/api/images/{image_id:path}/history")
def api_image_history(image_id: str, actor: OptionalUser) -> dict[str, Any]:
    _resource_or_403()
    try:
        items = image_history(image_id)
        return {"ok": True, "count": len(items), "items": items, "viewer": actor}
    except Exception as e:
        raise HTTPException(status_code=503, detail=str(e)) from e


@app.get("/api/changelog")
def api_changelog(actor: OptionalUser = None) -> dict[str, Any]:
    return {"ok": True, "version": VERSION, "items": CHANGELOG, "viewer": actor}


def _default_prefs() -> dict[str, Any]:
    return {
        "particles": True,
        "particles_count": 90,
        "bg_theme": "cyber",
        "card_density": "comfortable",
        "reduce_motion": False,
    }


def _load_prefs() -> dict[str, Any]:
    import json

    raw = get_meta("ui_prefs")
    prefs = _default_prefs()
    if raw:
        try:
            data = json.loads(raw)
            if isinstance(data, dict):
                prefs.update({k: v for k, v in data.items() if k in prefs})
        except Exception:
            pass
    return prefs


@app.get("/api/prefs")
def api_get_prefs(actor: OptionalUser = None) -> dict[str, Any]:
    return {"ok": True, "prefs": _load_prefs(), "viewer": actor}


@app.put("/api/prefs")
def api_put_prefs(body: PrefsBody, actor: AuthUser) -> dict[str, Any]:
    import json

    prefs = _load_prefs()
    patch = body.model_dump(exclude_none=True)
    allowed_themes = {"cyber", "deep", "aurora", "plain"}
    allowed_density = {"comfortable", "compact"}
    if "bg_theme" in patch and patch["bg_theme"] not in allowed_themes:
        raise HTTPException(status_code=400, detail="无效背景主题")
    if "card_density" in patch and patch["card_density"] not in allowed_density:
        raise HTTPException(status_code=400, detail="无效卡片密度")
    prefs.update(patch)
    set_meta("ui_prefs", json.dumps(prefs, ensure_ascii=False))
    audit("ui_prefs_update", actor=actor or "unknown", detail=prefs)
    return {"ok": True, "prefs": prefs, "message": "个性化设置已保存"}


# ── Logs ───────────────────────────────────────────────────


@app.get("/api/containers/{container_id}/logs")
def api_logs(
    container_id: str,
    actor: OptionalUser = None,
    tail: int = Query(200, ge=1, le=10000),
    timestamps: bool = True,
    follow: bool = False,
    since: int | None = None,
):
    _resource_or_403()
    if follow:
        return StreamingResponse(
            sse_log_events(container_id, tail=min(tail, 500), timestamps=timestamps),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )
    try:
        result = get_logs(container_id, tail=tail, timestamps=timestamps, since=since)
        result["viewer"] = actor
        return result
    except KeyError:
        raise HTTPException(status_code=404, detail="容器不存在") from None
    except Exception as e:
        raise HTTPException(status_code=503, detail=str(e)) from e


# ── Images ─────────────────────────────────────────────────


@app.get("/api/images")
def api_images(actor: OptionalUser = None) -> dict[str, Any]:
    _resource_or_403()
    result = images_list()
    result["viewer"] = actor
    return result


@app.post("/api/images/pull")
def api_images_pull(body: PullBody, actor: AuthUser) -> dict[str, Any]:
    _resource_or_403()
    return _perm_http(images_pull, body.image, actor=actor)


@app.post("/api/images/prune")
def api_images_prune(actor: AuthUser, dangling: bool = True) -> dict[str, Any]:
    _resource_or_403()
    return _perm_http(images_prune, actor=actor, dangling=dangling)


@app.delete("/api/images/{image_id:path}")
def api_images_remove(image_id: str, actor: AuthUser, force: bool = False) -> dict[str, Any]:
    _resource_or_403()
    return _perm_http(images_remove, image_id, actor=actor, force=force)


# ── Networks ───────────────────────────────────────────────


@app.get("/api/networks")
def api_networks(actor: OptionalUser = None) -> dict[str, Any]:
    _resource_or_403()
    result = networks_list()
    result["viewer"] = actor
    return result


@app.post("/api/networks")
def api_networks_create(body: NetworkCreateBody, actor: AuthUser) -> dict[str, Any]:
    _resource_or_403()
    return _perm_http(
        networks_create,
        body.name,
        actor=actor,
        driver=body.driver,
        internal=body.internal,
        attachable=body.attachable,
    )


@app.delete("/api/networks/{network_id}")
def api_networks_remove(network_id: str, actor: AuthUser) -> dict[str, Any]:
    _resource_or_403()
    return _perm_http(networks_remove, network_id, actor=actor)


# ── Volumes ────────────────────────────────────────────────


@app.get("/api/volumes")
def api_volumes(actor: OptionalUser = None) -> dict[str, Any]:
    _resource_or_403()
    result = volumes_list()
    result["viewer"] = actor
    return result


@app.post("/api/volumes")
def api_volumes_create(body: VolumeCreateBody, actor: AuthUser) -> dict[str, Any]:
    _resource_or_403()
    return _perm_http(volumes_create, body.name, actor=actor, driver=body.driver)


@app.post("/api/volumes/prune")
def api_volumes_prune(actor: AuthUser) -> dict[str, Any]:
    _resource_or_403()
    return _perm_http(volumes_prune, actor=actor)


@app.delete("/api/volumes/{name}")
def api_volumes_remove(name: str, actor: AuthUser, force: bool = False) -> dict[str, Any]:
    _resource_or_403()
    return _perm_http(volumes_remove, name, actor=actor, force=force)


# ── System / Events ────────────────────────────────────────


@app.get("/api/system/info")
def api_system_info(actor: OptionalUser = None) -> dict[str, Any]:
    _resource_or_403()
    result = sys_info()
    result["viewer"] = actor
    return result


@app.get("/api/system/df")
def api_system_df(actor: OptionalUser = None) -> dict[str, Any]:
    _resource_or_403()
    result = sys_df()
    result["viewer"] = actor
    return result


@app.post("/api/system/prune")
def api_system_prune(body: PruneBody, actor: AuthUser) -> dict[str, Any]:
    _resource_or_403()
    return _perm_http(
        sys_prune,
        actor=actor,
        containers=body.containers,
        images=body.images,
        volumes=body.volumes,
        networks=body.networks,
        dangling_images_only=body.dangling_images_only,
    )


@app.get("/api/events")
def api_events(
    actor: OptionalUser = None,
    limit: int = Query(50, ge=1, le=500),
    since_seconds: int = Query(3600, ge=60, le=86400),
    follow: bool = False,
):
    _resource_or_403()
    if follow:
        return StreamingResponse(
            sse_docker_events(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )
    result = recent_events(limit=limit, since_seconds=since_seconds)
    result["viewer"] = actor
    return result


@app.get("/api/doctor")
def api_doctor(actor: OptionalUser) -> dict[str, Any]:
    result = diagnose_all()
    result["viewer"] = actor
    return result


@app.get("/api/doctor/{container_id}")
def api_doctor_one(container_id: str, actor: OptionalUser) -> dict[str, Any]:
    result = diagnose_one(container_id)
    if not result.get("ok", True) and result.get("error") == "container_not_found":
        raise HTTPException(status_code=404, detail=result.get("message"))
    result["viewer"] = actor
    return result


@app.get("/api/monitor/report")
def api_monitor(refresh: bool = False, actor: OptionalUser = None) -> dict[str, Any]:
    report = collect_report(persist=True) if refresh else get_latest_or_collect()
    report["viewer"] = actor
    return report


@app.get("/api/ops/records")
def api_ops_records(limit: int = 100, actor: OptionalUser = None) -> dict[str, Any]:
    return {"ok": True, "items": records(limit=min(max(limit, 1), 500)), "viewer": actor}


@app.post("/api/ops/backup/{container_id}")
def api_backup(container_id: str, actor: AuthUser) -> dict[str, Any]:
    return backup_container(container_id, actor=actor)


@app.post("/api/ops/update/{container_id}")
def api_update(container_id: str, body: UpdateBody, actor: AuthUser) -> dict[str, Any]:
    return safe_update(container_id, image=body.image, actor=actor)


@app.post("/api/ops/rollback/{container_id}")
def api_rollback(container_id: str, actor: AuthUser) -> dict[str, Any]:
    return rollback_guide(container_id, actor=actor)


class DetectBody(BaseModel):
    container_ids: list[str] | None = None
    only_running: bool = False


class OneClickUpdateBody(BaseModel):
    container_ids: list[str] | None = None
    only_available: bool = True
    only_running: bool = False


@app.post("/api/ops/detect-updates")
def api_detect_updates(body: DetectBody, actor: AuthUser) -> dict[str, Any]:
    """一键检测：比对本地与仓库镜像 digest，发现可更新容器。"""
    return detect_updates(
        container_ids=body.container_ids,
        only_running=body.only_running,
        actor=actor,
    )


@app.get("/api/ops/detect-updates")
def api_detect_updates_get(
    only_running: bool = False,
    actor: AuthUser = ...,
) -> dict[str, Any]:
    return detect_updates(only_running=only_running, actor=actor)


@app.post("/api/ops/one-click-update")
def api_one_click_update(body: OneClickUpdateBody, actor: AuthUser) -> dict[str, Any]:
    """一键更新：先检测，再按管理源安全更新（Compose/Unraid 模板/三方仅拉镜像）。"""
    return one_click_update(
        container_ids=body.container_ids,
        only_available=body.only_available,
        only_running=body.only_running,
        actor=actor,
    )


# ── Compose ──────────────────────────────────────────────


@app.get("/api/compose/projects")
def api_compose_projects(actor: OptionalUser = None) -> dict[str, Any]:
    if not get_settings().compose_enabled:
        return {"ok": True, "items": [], "disabled": True, "viewer": actor}
    items = list_projects()
    return {"ok": True, "count": len(items), "items": items, "viewer": actor}


@app.get("/api/compose/projects/{name}")
def api_compose_project(name: str, actor: OptionalUser = None) -> dict[str, Any]:
    p = get_project(name)
    if not p:
        raise HTTPException(status_code=404, detail="Compose 项目不存在")
    return {"ok": True, "item": p, "viewer": actor}


@app.post("/api/compose/projects/{name}/backup")
def api_compose_backup(name: str, actor: AuthUser) -> dict[str, Any]:
    return backup_project(name, actor=actor)


@app.post("/api/compose/projects/{name}/update")
def api_compose_update(name: str, body: ComposeUpdateBody, actor: AuthUser) -> dict[str, Any]:
    return safe_update_project(name, actor=actor, service=body.service, recreate=body.recreate)


@app.post("/api/compose/projects/{name}/up")
def api_compose_up(name: str, actor: AuthUser) -> dict[str, Any]:
    _takeover_or_403()
    return project_up(name, actor=actor)


@app.post("/api/compose/projects/{name}/down")
def api_compose_down(name: str, actor: AuthUser) -> dict[str, Any]:
    _takeover_or_403()
    return project_down(name, actor=actor)


# ── Unraid ───────────────────────────────────────────────


@app.get("/api/unraid/templates")
def api_unraid_templates(actor: OptionalUser = None) -> dict[str, Any]:
    if not get_settings().unraid_enabled:
        return {"ok": True, "items": [], "disabled": True, "viewer": actor}
    available = templates_available()
    items = list_templates() if available else []
    return {
        "ok": True,
        "available": available,
        "path": str(get_settings().unraid_templates_path()),
        "count": len(items),
        "items": items,
        "viewer": actor,
    }


@app.get("/api/unraid/templates/{name}")
def api_unraid_template(name: str, actor: OptionalUser = None) -> dict[str, Any]:
    tpl = get_template(name)
    if not tpl:
        raise HTTPException(status_code=404, detail="Unraid 模板不存在或目录未挂载")
    return {"ok": True, "item": tpl, "viewer": actor}


@app.post("/api/unraid/templates/{name}/backup")
def api_unraid_backup(name: str, actor: AuthUser) -> dict[str, Any]:
    return backup_template(name, actor=actor)


@app.post("/api/unraid/templates/{name}/update")
def api_unraid_update(name: str, body: UnraidUpdateBody, actor: AuthUser) -> dict[str, Any]:
    return safe_update_unraid(
        name, actor=actor, repository=body.repository, recreate=body.recreate
    )


@app.post("/api/unraid/adopt/{container_id}")
def api_unraid_adopt(container_id: str, actor: AuthUser) -> dict[str, Any]:
    _takeover_or_403()
    return adopt_to_unraid(container_id, actor=actor)


@app.get("/", response_class=HTMLResponse)
def index(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "title": "DockerOps",
            "subtitle": "NAS 日常运维 · 首次设置向导 · Tower / 飞牛引擎级接管",
        },
    )


@app.exception_handler(Exception)
async def unhandled(request: Request, exc: Exception):
    if isinstance(exc, HTTPException):
        return JSONResponse(status_code=exc.status_code, content={"ok": False, "detail": exc.detail})
    return JSONResponse(status_code=500, content={"ok": False, "detail": str(exc)})

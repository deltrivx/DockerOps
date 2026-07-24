from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

from auth import AuthUser, OptionalUser, login, logout
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
from doctor import diagnose_all, diagnose_one
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

APP_DIR = Path(__file__).resolve().parent
settings = get_settings()
init_db()

app = FastAPI(
    title="DockerOps",
    description=(
        "面向 NAS 的 Docker 运维平台 — Compose 双方接管、Unraid 模板升级、"
        "安全更新、Doctor 诊断、监控与可追溯运维。"
    ),
    version="0.2.0",
    docs_url="/docs",
    redoc_url="/redoc",
)

templates = Jinja2Templates(directory=str(APP_DIR / "templates"))
app.mount("/static", StaticFiles(directory=str(APP_DIR / "static")), name="static")


class LoginBody(BaseModel):
    username: str
    password: str


class UpdateBody(BaseModel):
    image: str | None = Field(default=None, description="目标镜像；Unraid 可写回模板 Repository")


class ComposeUpdateBody(BaseModel):
    service: str | None = Field(default=None, description="仅更新指定 service；空则整个项目")
    recreate: bool = True


class UnraidUpdateBody(BaseModel):
    repository: str | None = Field(default=None, description="新镜像，写入模板 Repository")
    recreate: bool = True


def _takeover_or_403() -> None:
    try:
        get_settings().takeover_guard()
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e)) from e


@app.on_event("startup")
def _startup() -> None:
    settings.ensure_dirs()
    refresh_template_name_cache()
    audit(
        "startup",
        actor="system",
        detail={
            "version": "0.2.0",
            "takeover_enabled": settings.takeover_enabled,
            "unraid_templates": templates_available(),
        },
    )


@app.get("/api/health")
def api_health() -> dict[str, Any]:
    engine = ping()
    return {
        "ok": True,
        "service": "dockerops",
        "version": "0.2.0",
        "time": time.time(),
        "docker": engine,
        "takeover_enabled": get_settings().takeover_enabled,
        "unraid_templates_available": templates_available(),
    }


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


@app.post("/api/auth/login")
def api_login(body: LoginBody) -> dict[str, Any]:
    return login(body.username, body.password)


@app.post("/api/auth/logout")
def api_logout(request: Request, actor: OptionalUser) -> dict[str, Any]:
    auth = request.headers.get("Authorization") or ""
    if auth.lower().startswith("bearer "):
        logout(auth.split(" ", 1)[1].strip())
    return {"ok": True, "actor": actor}


@app.get("/api/containers")
def api_containers(actor: OptionalUser) -> dict[str, Any]:
    try:
        items = list_containers(all_containers=True)
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"Docker 不可用：{e}") from e
    return {"ok": True, "count": len(items), "items": items, "viewer": actor}


@app.get("/api/containers/{container_id}")
def api_container(container_id: str, actor: OptionalUser) -> dict[str, Any]:
    try:
        item = get_container(container_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="容器不存在") from None
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"Docker 不可用：{e}") from e
    return {"ok": True, "item": item, "viewer": actor}


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
            "subtitle": "面向 NAS 的 Docker 运维平台 · Compose / Unraid 双方接管",
        },
    )


@app.exception_handler(Exception)
async def unhandled(request: Request, exc: Exception):
    if isinstance(exc, HTTPException):
        return JSONResponse(status_code=exc.status_code, content={"ok": False, "detail": exc.detail})
    return JSONResponse(status_code=500, content={"ok": False, "detail": str(exc)})

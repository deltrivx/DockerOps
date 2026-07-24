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
from config import get_settings
from db import audit, init_db
from doctor import diagnose_all, diagnose_one
from docker_client import get_container, list_containers, ping
from monitor import collect_report, get_latest_or_collect
from ops import backup_container, records, rollback_guide, safe_update

APP_DIR = Path(__file__).resolve().parent
settings = get_settings()
init_db()

app = FastAPI(
    title="DockerOps",
    description="面向 NAS 的 Docker 运维平台 — 安全更新、Doctor 诊断、监控与可追溯运维。",
    version="0.1.0",
    docs_url="/docs",
    redoc_url="/redoc",
)

templates = Jinja2Templates(directory=str(APP_DIR / "templates"))
app.mount("/static", StaticFiles(directory=str(APP_DIR / "static")), name="static")


class LoginBody(BaseModel):
    username: str
    password: str


class UpdateBody(BaseModel):
    image: str | None = Field(default=None, description="目标镜像，默认使用当前容器镜像名重新拉取")


@app.on_event("startup")
def _startup() -> None:
    settings.ensure_dirs()
    audit("startup", actor="system", detail={"version": "0.1.0"})


@app.get("/api/health")
def api_health() -> dict[str, Any]:
    engine = ping()
    return {
        "ok": True,
        "service": "dockerops",
        "version": "0.1.0",
        "time": time.time(),
        "docker": engine,
    }


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


@app.get("/", response_class=HTMLResponse)
def index(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "title": "DockerOps",
            "subtitle": "面向 NAS 的 Docker 运维平台",
        },
    )


@app.exception_handler(Exception)
async def unhandled(request: Request, exc: Exception):
    if isinstance(exc, HTTPException):
        return JSONResponse(status_code=exc.status_code, content={"ok": False, "detail": exc.detail})
    return JSONResponse(status_code=500, content={"ok": False, "detail": str(exc)})

from __future__ import annotations

import os
import re
from typing import Annotated, Any

from fastapi import Depends, HTTPException, Security, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from passlib.context import CryptContext

from config import get_settings
from db import (
    audit,
    create_session,
    create_user,
    delete_session,
    get_meta,
    get_session,
    get_user_by_username,
    set_meta,
    update_user_password,
    user_count,
)

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
bearer = HTTPBearer(auto_error=False)

_USERNAME_RE = re.compile(r"^[A-Za-z0-9_\-.]{3,32}$")


def verify_password(plain: str, hashed: str) -> bool:
    try:
        return pwd_context.verify(plain, hashed)
    except Exception:
        return False


def hash_password(plain: str) -> str:
    return pwd_context.hash(plain)


def validate_username(username: str) -> str:
    u = (username or "").strip()
    if not _USERNAME_RE.match(u):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="用户名需 3–32 位，仅字母数字及 _ - .",
        )
    return u


def validate_password(password: str) -> str:
    p = password or ""
    if len(p) < 6:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="密码至少 6 位")
    if len(p) > 128:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="密码过长")
    return p


def needs_setup() -> bool:
    return user_count() == 0


def bootstrap_from_env() -> dict[str, Any]:
    """
    If no users exist and DOCKEROPS_ADMIN_PASSWORD is explicitly set in the
    process environment, create the first admin from env and skip the wizard.
    Defaults in Settings alone do NOT auto-bootstrap (force first-run setup).
    """
    if user_count() > 0:
        return {"bootstrapped": False, "reason": "users_exist"}

    if "DOCKEROPS_ADMIN_PASSWORD" not in os.environ:
        return {"bootstrapped": False, "reason": "no_env_password"}

    settings = get_settings()
    username = (settings.admin_user or "admin").strip() or "admin"
    password = settings.admin_password or ""
    if len(password) < 6:
        return {"bootstrapped": False, "reason": "env_password_too_short"}

    try:
        validate_username(username)
    except HTTPException:
        username = "admin"

    create_user(username, hash_password(password), role="admin")
    set_meta("bootstrap_source", "env")
    set_meta("setup_completed", "1")
    audit("bootstrap_admin", actor="system", detail={"username": username, "source": "env"})
    return {"bootstrapped": True, "username": username, "source": "env"}


def auth_status() -> dict[str, Any]:
    count = user_count()
    setup = count == 0
    return {
        "ok": True,
        "needs_setup": setup,
        "user_count": count,
        "bootstrap_source": get_meta("bootstrap_source"),
        "env_password_configured": "DOCKEROPS_ADMIN_PASSWORD" in os.environ,
        "message": (
            "请完成首次管理员设置"
            if setup
            else "已配置管理员，可登录"
        ),
    }


def complete_setup(username: str, password: str) -> dict[str, Any]:
    if user_count() > 0:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="已完成初始化，请直接登录")

    username = validate_username(username)
    password = validate_password(password)

    create_user(username, hash_password(password), role="admin")
    set_meta("bootstrap_source", "wizard")
    set_meta("setup_completed", "1")
    token = create_session(username, get_settings().session_ttl_hours)
    audit("setup_complete", actor=username, detail={"source": "wizard"})
    return {
        "ok": True,
        "message": "管理员已创建，已自动登录",
        "access_token": token,
        "token_type": "bearer",
        "username": username,
        "expires_in_hours": get_settings().session_ttl_hours,
    }


def login(username: str, password: str) -> dict:
    if needs_setup():
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="尚未初始化，请先完成首次管理员设置",
        )

    user = get_user_by_username(username.strip())
    if not user or not verify_password(password, user["password_hash"]):
        # Legacy fallback: allow one-time env login only if hash verify fails and
        # username matches env admin AND password matches env AND meta says env bootstrap
        # (no plaintext fallback for security)
        audit("login_failed", actor=username)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="用户名或密码错误")

    token = create_session(user["username"], get_settings().session_ttl_hours)
    audit("login_ok", actor=user["username"])
    return {
        "access_token": token,
        "token_type": "bearer",
        "username": user["username"],
        "expires_in_hours": get_settings().session_ttl_hours,
    }


def change_password(username: str, old_password: str, new_password: str) -> dict[str, Any]:
    user = get_user_by_username(username)
    if not user or not verify_password(old_password, user["password_hash"]):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="原密码不正确")
    new_password = validate_password(new_password)
    update_user_password(username, hash_password(new_password))
    audit("password_changed", actor=username)
    return {"ok": True, "message": "密码已更新"}


def logout(token: str) -> None:
    sess = get_session(token)
    delete_session(token)
    if sess:
        audit("logout", actor=sess.get("username"))


def resolve_actor(
    creds: Annotated[HTTPAuthorizationCredentials | None, Security(bearer)],
) -> str | None:
    """Optional auth: returns username if valid, else None."""
    settings = get_settings()
    if not creds:
        return None
    token = creds.credentials
    if settings.api_token and token == settings.api_token:
        return "api-token"
    sess = get_session(token)
    if sess:
        return sess["username"]
    return None


def require_auth(
    creds: Annotated[HTTPAuthorizationCredentials | None, Security(bearer)],
) -> str:
    actor = resolve_actor(creds)
    if not actor:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="需要登录或有效 API Token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return actor


AuthUser = Annotated[str, Depends(require_auth)]
OptionalUser = Annotated[str | None, Depends(resolve_actor)]

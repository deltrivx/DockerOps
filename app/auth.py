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
    list_usernames,
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
    """True when SQLite users table is empty — no default account exists."""
    return user_count() == 0


def bootstrap_from_env() -> dict[str, Any]:
    """
    Optional install-time bootstrap only.

    Creates the first admin into SQLite when BOTH are explicitly present in the
    process environment and password is ≥6 chars:
      DOCKEROPS_ADMIN_USER
      DOCKEROPS_ADMIN_PASSWORD

    Settings defaults are empty and never auto-create accounts.
    After bootstrap (or wizard), all auth is SQLite-only.
    """
    if user_count() > 0:
        return {"bootstrapped": False, "reason": "users_exist", "store": "sqlite"}

    if "DOCKEROPS_ADMIN_PASSWORD" not in os.environ:
        return {"bootstrapped": False, "reason": "no_env_password", "store": "sqlite"}
    if "DOCKEROPS_ADMIN_USER" not in os.environ:
        return {"bootstrapped": False, "reason": "no_env_user", "store": "sqlite"}

    username_raw = (os.environ.get("DOCKEROPS_ADMIN_USER") or "").strip()
    password = os.environ.get("DOCKEROPS_ADMIN_PASSWORD") or ""
    if not username_raw:
        return {"bootstrapped": False, "reason": "env_user_empty", "store": "sqlite"}
    if len(password) < 6:
        return {"bootstrapped": False, "reason": "env_password_too_short", "store": "sqlite"}

    try:
        username = validate_username(username_raw)
    except HTTPException:
        return {"bootstrapped": False, "reason": "env_user_invalid", "store": "sqlite"}

    create_user(username, hash_password(password), role="admin")
    set_meta("bootstrap_source", "env")
    set_meta("setup_completed", "1")
    set_meta("auth_store", "sqlite")
    audit("bootstrap_admin", actor="system", detail={"username": username, "source": "env", "store": "sqlite"})
    return {"bootstrapped": True, "username": username, "source": "env", "store": "sqlite"}


def password_reset_hint(usernames: list[str] | None = None) -> dict[str, Any]:
    """Public, non-secret guidance for host-side password reset (no Web reset)."""
    names = usernames if usernames is not None else list_usernames()
    sample = names[0] if names else "YourUser"
    cmd_unraid = (
        f'docker exec -it DockerOps python -m tools.reset_password '
        f'--username {sample} --password "新密码至少6位"'
    )
    cmd_generic = (
        f'docker exec -it dockerops python -m tools.reset_password '
        f'--username {sample} --password "新密码至少6位"'
    )
    cmd_prompt = (
        f"docker exec -it DockerOps python -m tools.reset_password --username {sample}"
    )
    return {
        "title": "忘记密码？",
        "summary": "Web 端不提供自助重置。请在 NAS 主机终端用容器内工具改密（写入 SQLite）。",
        "usernames_hint": names,
        "container_names": ["DockerOps", "dockerops"],
        "commands": [
            {"label": "Unraid（容器名 DockerOps）", "cmd": cmd_unraid},
            {"label": "通用（容器名 dockerops）", "cmd": cmd_generic},
            {"label": "交互输入密码（不在命令行暴露）", "cmd": cmd_prompt},
        ],
        "notes": [
            "账号仅存 /data/dockerops.db（内置 SQLite），改密后立即生效，旧会话会失效。",
            "若容器名不同，将 DockerOps 换成实际名称：docker ps | grep -i dockerops",
            "无账号时打开网页会强制进入「首次初始化」，不会出现登录页。",
        ],
    }


def auth_status() -> dict[str, Any]:
    count = user_count()
    setup = count == 0
    names = [] if setup else list_usernames()
    return {
        "ok": True,
        "needs_setup": setup,
        "user_count": count,
        "usernames": names,
        "auth_store": "sqlite",
        "db": "dockerops.db",
        "bootstrap_source": get_meta("bootstrap_source"),
        "env_bootstrap_ready": (
            "DOCKEROPS_ADMIN_USER" in os.environ
            and "DOCKEROPS_ADMIN_PASSWORD" in os.environ
            and len(os.environ.get("DOCKEROPS_ADMIN_PASSWORD") or "") >= 6
        ),
        "password_reset": password_reset_hint(names),
        "message": (
            "尚未创建账号：请完成首次初始化（强制设置超级管理员，写入 SQLite）"
            if setup
            else "已有账号，请登录后使用运维控制台"
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
    set_meta("auth_store", "sqlite")
    token = create_session(username, get_settings().session_ttl_hours)
    audit("setup_complete", actor=username, detail={"source": "wizard", "store": "sqlite"})
    return {
        "ok": True,
        "message": "管理员已写入 SQLite，已自动登录",
        "access_token": token,
        "token_type": "bearer",
        "username": username,
        "auth_store": "sqlite",
        "expires_in_hours": get_settings().session_ttl_hours,
    }


def login(username: str, password: str) -> dict:
    if needs_setup():
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="尚未初始化，请先完成首次管理员设置（账号写入内置 SQLite）",
        )

    user = get_user_by_username(username.strip())
    if not user or not verify_password(password, user["password_hash"]):
        audit("login_failed", actor=username)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="用户名或密码错误")

    token = create_session(user["username"], get_settings().session_ttl_hours)
    audit("login_ok", actor=user["username"])
    return {
        "access_token": token,
        "token_type": "bearer",
        "username": user["username"],
        "auth_store": "sqlite",
        "expires_in_hours": get_settings().session_ttl_hours,
    }


def change_password(username: str, old_password: str, new_password: str) -> dict[str, Any]:
    user = get_user_by_username(username)
    if not user or not verify_password(old_password, user["password_hash"]):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="原密码不正确")
    new_password = validate_password(new_password)
    update_user_password(username, hash_password(new_password))
    audit("password_changed", actor=username)
    return {"ok": True, "message": "密码已更新（SQLite）"}


def logout(token: str) -> None:
    sess = get_session(token)
    delete_session(token)
    if sess:
        audit("logout", actor=sess.get("username"))


def actor_from_token(token: str | None) -> str | None:
    """Resolve username from raw bearer token string (header or query)."""
    if not token:
        return None
    t = token.strip()
    if not t:
        return None
    settings = get_settings()
    if settings.api_token and t == settings.api_token:
        return "api-token"
    sess = get_session(t)
    if sess:
        return sess["username"]
    return None


def resolve_actor(
    creds: Annotated[HTTPAuthorizationCredentials | None, Security(bearer)],
) -> str | None:
    """Optional auth: returns username if valid, else None."""
    if not creds:
        return None
    return actor_from_token(creds.credentials)


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

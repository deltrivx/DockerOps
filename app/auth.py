from __future__ import annotations

from typing import Annotated

from fastapi import Depends, HTTPException, Security, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from passlib.context import CryptContext

from config import get_settings
from db import audit, create_session, delete_session, get_session

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
bearer = HTTPBearer(auto_error=False)


def verify_password(plain: str, hashed: str) -> bool:
    try:
        return pwd_context.verify(plain, hashed)
    except Exception:
        return False


def hash_password(plain: str) -> str:
    return pwd_context.hash(plain)


def login(username: str, password: str) -> dict:
    settings = get_settings()
    if username != settings.admin_user or password != settings.admin_password:
        audit("login_failed", actor=username)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="用户名或密码错误")
    token = create_session(username, settings.session_ttl_hours)
    audit("login_ok", actor=username)
    return {
        "access_token": token,
        "token_type": "bearer",
        "username": username,
        "expires_in_hours": settings.session_ttl_hours,
    }


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

from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from config import get_settings

_lock = threading.Lock()


def _db_path() -> Path:
    return Path(get_settings().data_dir) / "dockerops.db"


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(str(_db_path()), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    """
    Built-in SQLite at {data_dir}/dockerops.db.

    Stores: users (bcrypt), sessions, ops_records, monitor_snapshots,
    audit_log, app_meta (ui prefs / setup flags / important key-values).
    No default accounts are seeded.
    """
    get_settings().ensure_dirs()
    with _lock:
        conn = connect()
        try:
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute("PRAGMA foreign_keys=ON;")
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS sessions (
                    token TEXT PRIMARY KEY,
                    username TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    expires_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS users (
                    id TEXT PRIMARY KEY,
                    username TEXT NOT NULL UNIQUE,
                    password_hash TEXT NOT NULL,
                    role TEXT NOT NULL DEFAULT 'admin',
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS ops_records (
                    id TEXT PRIMARY KEY,
                    action TEXT NOT NULL,
                    target TEXT,
                    status TEXT NOT NULL,
                    detail TEXT,
                    actor TEXT,
                    created_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS monitor_snapshots (
                    id TEXT PRIMARY KEY,
                    payload TEXT NOT NULL,
                    health_score INTEGER,
                    created_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS audit_log (
                    id TEXT PRIMARY KEY,
                    event TEXT NOT NULL,
                    actor TEXT,
                    detail TEXT,
                    created_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS app_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_sessions_expires ON sessions(expires_at);
                CREATE INDEX IF NOT EXISTS idx_ops_created ON ops_records(created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_audit_created ON audit_log(created_at DESC);
                """
            )
            # Ensure auth_store marker exists for ops visibility
            now = time.time()
            conn.execute(
                """
                INSERT INTO app_meta (key, value, updated_at) VALUES (?,?,?)
                ON CONFLICT(key) DO NOTHING
                """,
                ("auth_store", "sqlite", now),
            )
            conn.commit()
        finally:
            conn.close()


def add_ops_record(
    action: str,
    target: str | None,
    status: str,
    detail: dict[str, Any] | str | None = None,
    actor: str | None = None,
) -> dict[str, Any]:
    rid = uuid.uuid4().hex
    now = time.time()
    detail_s = detail if isinstance(detail, str) else json.dumps(detail or {}, ensure_ascii=False)
    with _lock:
        conn = connect()
        try:
            conn.execute(
                "INSERT INTO ops_records (id, action, target, status, detail, actor, created_at) VALUES (?,?,?,?,?,?,?)",
                (rid, action, target, status, detail_s, actor, now),
            )
            conn.commit()
        finally:
            conn.close()
    return {
        "id": rid,
        "action": action,
        "target": target,
        "status": status,
        "detail": detail if not isinstance(detail, str) else _try_json(detail),
        "actor": actor,
        "created_at": now,
    }


def list_ops_records(limit: int = 100) -> list[dict[str, Any]]:
    with _lock:
        conn = connect()
        try:
            rows = conn.execute(
                "SELECT * FROM ops_records ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        finally:
            conn.close()
    out: list[dict[str, Any]] = []
    for r in rows:
        out.append(
            {
                "id": r["id"],
                "action": r["action"],
                "target": r["target"],
                "status": r["status"],
                "detail": _try_json(r["detail"]),
                "actor": r["actor"],
                "created_at": r["created_at"],
            }
        )
    return out


def add_monitor_snapshot(payload: dict[str, Any], health_score: int) -> dict[str, Any]:
    rid = uuid.uuid4().hex
    now = time.time()
    with _lock:
        conn = connect()
        try:
            conn.execute(
                "INSERT INTO monitor_snapshots (id, payload, health_score, created_at) VALUES (?,?,?,?)",
                (rid, json.dumps(payload, ensure_ascii=False), health_score, now),
            )
            conn.commit()
        finally:
            conn.close()
    return {"id": rid, "health_score": health_score, "created_at": now, "payload": payload}


def latest_monitor_snapshot() -> dict[str, Any] | None:
    with _lock:
        conn = connect()
        try:
            row = conn.execute(
                "SELECT * FROM monitor_snapshots ORDER BY created_at DESC LIMIT 1"
            ).fetchone()
        finally:
            conn.close()
    if not row:
        return None
    return {
        "id": row["id"],
        "health_score": row["health_score"],
        "created_at": row["created_at"],
        "payload": _try_json(row["payload"]),
    }


def user_count() -> int:
    with _lock:
        conn = connect()
        try:
            row = conn.execute("SELECT COUNT(*) AS c FROM users").fetchone()
            return int(row["c"] if row else 0)
        finally:
            conn.close()


def get_user_by_username(username: str) -> dict[str, Any] | None:
    with _lock:
        conn = connect()
        try:
            row = conn.execute(
                "SELECT * FROM users WHERE username = ?", (username,)
            ).fetchone()
        finally:
            conn.close()
    if not row:
        return None
    return {
        "id": row["id"],
        "username": row["username"],
        "password_hash": row["password_hash"],
        "role": row["role"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def create_user(username: str, password_hash: str, role: str = "admin") -> dict[str, Any]:
    uid = uuid.uuid4().hex
    now = time.time()
    with _lock:
        conn = connect()
        try:
            conn.execute(
                "INSERT INTO users (id, username, password_hash, role, created_at, updated_at) VALUES (?,?,?,?,?,?)",
                (uid, username, password_hash, role, now, now),
            )
            conn.commit()
        finally:
            conn.close()
    return {
        "id": uid,
        "username": username,
        "role": role,
        "created_at": now,
        "updated_at": now,
    }


def update_user_password(username: str, password_hash: str) -> bool:
    now = time.time()
    with _lock:
        conn = connect()
        try:
            cur = conn.execute(
                "UPDATE users SET password_hash = ?, updated_at = ? WHERE username = ?",
                (password_hash, now, username),
            )
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()


def list_usernames() -> list[str]:
    with _lock:
        conn = connect()
        try:
            rows = conn.execute("SELECT username FROM users ORDER BY created_at ASC").fetchall()
            return [r["username"] for r in rows]
        finally:
            conn.close()


def delete_all_users_and_sessions() -> int:
    """Wipe auth tables (deploy-time admin reset)."""
    with _lock:
        conn = connect()
        try:
            cur = conn.execute("SELECT COUNT(*) AS c FROM users")
            n = int(cur.fetchone()["c"] or 0)
            conn.execute("DELETE FROM sessions")
            conn.execute("DELETE FROM users")
            conn.commit()
            return n
        finally:
            conn.close()


def get_meta(key: str) -> str | None:
    with _lock:
        conn = connect()
        try:
            row = conn.execute("SELECT value FROM app_meta WHERE key = ?", (key,)).fetchone()
            return row["value"] if row else None
        finally:
            conn.close()


def set_meta(key: str, value: str) -> None:
    now = time.time()
    with _lock:
        conn = connect()
        try:
            conn.execute(
                """
                INSERT INTO app_meta (key, value, updated_at) VALUES (?,?,?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
                """,
                (key, value, now),
            )
            conn.commit()
        finally:
            conn.close()


def create_session(username: str, ttl_hours: int) -> str:
    token = uuid.uuid4().hex + uuid.uuid4().hex
    now = time.time()
    exp = now + ttl_hours * 3600
    with _lock:
        conn = connect()
        try:
            conn.execute(
                "INSERT INTO sessions (token, username, created_at, expires_at) VALUES (?,?,?,?)",
                (token, username, now, exp),
            )
            conn.commit()
        finally:
            conn.close()
    return token


def get_session(token: str) -> dict[str, Any] | None:
    now = time.time()
    with _lock:
        conn = connect()
        try:
            row = conn.execute(
                "SELECT * FROM sessions WHERE token = ?", (token,)
            ).fetchone()
            if not row:
                return None
            if row["expires_at"] < now:
                conn.execute("DELETE FROM sessions WHERE token = ?", (token,))
                conn.commit()
                return None
            return {"token": row["token"], "username": row["username"], "expires_at": row["expires_at"]}
        finally:
            conn.close()


def delete_session(token: str) -> None:
    with _lock:
        conn = connect()
        try:
            conn.execute("DELETE FROM sessions WHERE token = ?", (token,))
            conn.commit()
        finally:
            conn.close()


def audit(event: str, actor: str | None = None, detail: dict[str, Any] | str | None = None) -> None:
    rid = uuid.uuid4().hex
    now = time.time()
    detail_s = detail if isinstance(detail, str) else json.dumps(detail or {}, ensure_ascii=False)
    with _lock:
        conn = connect()
        try:
            conn.execute(
                "INSERT INTO audit_log (id, event, actor, detail, created_at) VALUES (?,?,?,?,?)",
                (rid, event, actor, detail_s, now),
            )
            conn.commit()
        finally:
            conn.close()


def _try_json(s: str | None) -> Any:
    if s is None:
        return None
    try:
        return json.loads(s)
    except Exception:
        return s

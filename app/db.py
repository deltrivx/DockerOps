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
    get_settings().ensure_dirs()
    with _lock:
        conn = connect()
        try:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS sessions (
                    token TEXT PRIMARY KEY,
                    username TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    expires_at REAL NOT NULL
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
                """
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

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
                CREATE TABLE IF NOT EXISTS docker_endpoints (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL UNIQUE,
                    docker_host TEXT NOT NULL,
                    tls_enabled INTEGER NOT NULL DEFAULT 0,
                    tls_ca TEXT,
                    tls_cert TEXT,
                    tls_key TEXT,
                    verify_tls INTEGER NOT NULL DEFAULT 1,
                    is_default INTEGER NOT NULL DEFAULT 0,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    notes TEXT,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_sessions_expires ON sessions(expires_at);
                CREATE INDEX IF NOT EXISTS idx_ops_created ON ops_records(created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_audit_created ON audit_log(created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_endpoints_default ON docker_endpoints(is_default);
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
        # Seed default endpoint from env after table exists
        try:
            ensure_default_endpoint()
        except Exception:
            pass


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


# ── Docker endpoints (multi-engine) ──────────────────────────────────────────

META_ACTIVE_ENDPOINT = "active_endpoint_id"


def _row_endpoint(r: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": r["id"],
        "name": r["name"],
        "docker_host": r["docker_host"],
        "tls_enabled": bool(r["tls_enabled"]),
        "tls_ca": r["tls_ca"] or "",
        "tls_cert": r["tls_cert"] or "",
        "tls_key": r["tls_key"] or "",
        "verify_tls": bool(r["verify_tls"]),
        "is_default": bool(r["is_default"]),
        "enabled": bool(r["enabled"]),
        "notes": r["notes"] or "",
        "created_at": r["created_at"],
        "updated_at": r["updated_at"],
    }


def ensure_default_endpoint() -> dict[str, Any]:
    """
    Ensure at least one endpoint exists. Seeds from DOCKEROPS_DOCKER_HOST / default sock.
    Safe to call repeatedly.
    """
    settings = get_settings()
    host = (settings.docker_host or "unix:///var/run/docker.sock").strip()
    with _lock:
        conn = connect()
        try:
            row = conn.execute(
                "SELECT * FROM docker_endpoints ORDER BY is_default DESC, created_at ASC LIMIT 1"
            ).fetchone()
            if row:
                return _row_endpoint(row)
            eid = uuid.uuid4().hex
            now = time.time()
            conn.execute(
                """
                INSERT INTO docker_endpoints (
                    id, name, docker_host, tls_enabled, tls_ca, tls_cert, tls_key,
                    verify_tls, is_default, enabled, notes, created_at, updated_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    eid,
                    "本机",
                    host,
                    0,
                    "",
                    "",
                    "",
                    1,
                    1,
                    1,
                    "由环境 DOCKEROPS_DOCKER_HOST / 默认 sock 初始化",
                    now,
                    now,
                ),
            )
            conn.execute(
                """
                INSERT INTO app_meta (key, value, updated_at) VALUES (?,?,?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
                """,
                (META_ACTIVE_ENDPOINT, eid, now),
            )
            conn.commit()
            row = conn.execute("SELECT * FROM docker_endpoints WHERE id = ?", (eid,)).fetchone()
            return _row_endpoint(row)
        finally:
            conn.close()


def list_endpoints() -> list[dict[str, Any]]:
    ensure_default_endpoint()
    with _lock:
        conn = connect()
        try:
            rows = conn.execute(
                "SELECT * FROM docker_endpoints ORDER BY is_default DESC, name COLLATE NOCASE ASC"
            ).fetchall()
            return [_row_endpoint(r) for r in rows]
        finally:
            conn.close()


def get_endpoint(endpoint_id: str) -> dict[str, Any] | None:
    if not endpoint_id:
        return None
    with _lock:
        conn = connect()
        try:
            row = conn.execute(
                "SELECT * FROM docker_endpoints WHERE id = ?", (endpoint_id,)
            ).fetchone()
            return _row_endpoint(row) if row else None
        finally:
            conn.close()


def get_default_endpoint() -> dict[str, Any]:
    ensure_default_endpoint()
    with _lock:
        conn = connect()
        try:
            row = conn.execute(
                "SELECT * FROM docker_endpoints WHERE is_default = 1 AND enabled = 1 LIMIT 1"
            ).fetchone()
            if not row:
                row = conn.execute(
                    "SELECT * FROM docker_endpoints WHERE enabled = 1 ORDER BY created_at ASC LIMIT 1"
                ).fetchone()
            if not row:
                row = conn.execute(
                    "SELECT * FROM docker_endpoints ORDER BY created_at ASC LIMIT 1"
                ).fetchone()
            return _row_endpoint(row)
        finally:
            conn.close()


def get_active_endpoint_id() -> str:
    ensure_default_endpoint()
    raw = get_meta(META_ACTIVE_ENDPOINT)
    if raw:
        ep = get_endpoint(raw)
        if ep and ep.get("enabled"):
            return raw
    default = get_default_endpoint()
    set_meta(META_ACTIVE_ENDPOINT, default["id"])
    return default["id"]


def set_active_endpoint_id(endpoint_id: str) -> dict[str, Any]:
    ep = get_endpoint(endpoint_id)
    if not ep:
        raise KeyError("endpoint_not_found")
    if not ep.get("enabled"):
        raise ValueError("endpoint_disabled")
    set_meta(META_ACTIVE_ENDPOINT, endpoint_id)
    return ep


def create_endpoint(
    name: str,
    docker_host: str,
    *,
    tls_enabled: bool = False,
    tls_ca: str = "",
    tls_cert: str = "",
    tls_key: str = "",
    verify_tls: bool = True,
    is_default: bool = False,
    notes: str = "",
) -> dict[str, Any]:
    eid = uuid.uuid4().hex
    now = time.time()
    name = (name or "").strip()
    docker_host = (docker_host or "").strip()
    if not name:
        raise ValueError("name_required")
    if not docker_host:
        raise ValueError("docker_host_required")
    with _lock:
        conn = connect()
        try:
            if is_default:
                conn.execute("UPDATE docker_endpoints SET is_default = 0")
            conn.execute(
                """
                INSERT INTO docker_endpoints (
                    id, name, docker_host, tls_enabled, tls_ca, tls_cert, tls_key,
                    verify_tls, is_default, enabled, notes, created_at, updated_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    eid,
                    name,
                    docker_host,
                    1 if tls_enabled else 0,
                    tls_ca or "",
                    tls_cert or "",
                    tls_key or "",
                    1 if verify_tls else 0,
                    1 if is_default else 0,
                    1,
                    notes or "",
                    now,
                    now,
                ),
            )
            conn.commit()
            row = conn.execute("SELECT * FROM docker_endpoints WHERE id = ?", (eid,)).fetchone()
            return _row_endpoint(row)
        except sqlite3.IntegrityError as e:
            raise ValueError("name_exists") from e
        finally:
            conn.close()


def update_endpoint(endpoint_id: str, **fields: Any) -> dict[str, Any]:
    ep = get_endpoint(endpoint_id)
    if not ep:
        raise KeyError("endpoint_not_found")
    allowed = {
        "name",
        "docker_host",
        "tls_enabled",
        "tls_ca",
        "tls_cert",
        "tls_key",
        "verify_tls",
        "is_default",
        "enabled",
        "notes",
    }
    patch = {k: v for k, v in fields.items() if k in allowed and v is not None}
    if "name" in patch:
        patch["name"] = str(patch["name"]).strip()
        if not patch["name"]:
            raise ValueError("name_required")
    if "docker_host" in patch:
        patch["docker_host"] = str(patch["docker_host"]).strip()
        if not patch["docker_host"]:
            raise ValueError("docker_host_required")
    for b in ("tls_enabled", "verify_tls", "is_default", "enabled"):
        if b in patch:
            patch[b] = 1 if patch[b] else 0
    if not patch:
        return ep
    now = time.time()
    cols = ", ".join(f"{k} = ?" for k in patch)
    vals = list(patch.values()) + [now, endpoint_id]
    with _lock:
        conn = connect()
        try:
            if patch.get("is_default") == 1:
                conn.execute(
                    "UPDATE docker_endpoints SET is_default = 0 WHERE id != ?",
                    (endpoint_id,),
                )
            conn.execute(
                f"UPDATE docker_endpoints SET {cols}, updated_at = ? WHERE id = ?",
                vals,
            )
            conn.commit()
            row = conn.execute(
                "SELECT * FROM docker_endpoints WHERE id = ?", (endpoint_id,)
            ).fetchone()
            return _row_endpoint(row)
        except sqlite3.IntegrityError as e:
            raise ValueError("name_exists") from e
        finally:
            conn.close()


def delete_endpoint(endpoint_id: str) -> None:
    with _lock:
        conn = connect()
        try:
            n = conn.execute("SELECT COUNT(*) AS c FROM docker_endpoints").fetchone()["c"]
            if int(n) <= 1:
                raise ValueError("cannot_delete_last")
            row = conn.execute(
                "SELECT * FROM docker_endpoints WHERE id = ?", (endpoint_id,)
            ).fetchone()
            if not row:
                raise KeyError("endpoint_not_found")
            conn.execute("DELETE FROM docker_endpoints WHERE id = ?", (endpoint_id,))
            # if deleted was default, promote another
            if row["is_default"]:
                other = conn.execute(
                    "SELECT id FROM docker_endpoints ORDER BY created_at ASC LIMIT 1"
                ).fetchone()
                if other:
                    conn.execute(
                        "UPDATE docker_endpoints SET is_default = 1 WHERE id = ?",
                        (other["id"],),
                    )
            # fix active meta
            active = conn.execute(
                "SELECT value FROM app_meta WHERE key = ?", (META_ACTIVE_ENDPOINT,)
            ).fetchone()
            if active and active["value"] == endpoint_id:
                fallback = conn.execute(
                    "SELECT id FROM docker_endpoints WHERE is_default = 1 LIMIT 1"
                ).fetchone()
                if not fallback:
                    fallback = conn.execute(
                        "SELECT id FROM docker_endpoints ORDER BY created_at ASC LIMIT 1"
                    ).fetchone()
                if fallback:
                    now = time.time()
                    conn.execute(
                        """
                        INSERT INTO app_meta (key, value, updated_at) VALUES (?,?,?)
                        ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
                        """,
                        (META_ACTIVE_ENDPOINT, fallback["id"], now),
                    )
            conn.commit()
        finally:
            conn.close()

"""
Remote mode (Nezha-style): agent dials out to controller over WebSocket.

- No Docker Engine port exposure (2375/2376).
- Only DockerOps HTTP/WS on the controller public URL.
- Pair code TTL 60s; long-lived session token after redeem.
- MVP: controller + agent collab; managed lock UI is state-only for later polish.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
import secrets
import threading
import time
import uuid
from typing import Any

from db import (
    audit,
    connect,
    get_meta,
    set_meta,
    _lock,
)

META_REMOTE = "remote_settings"
PAIR_TTL_SEC = 60
SESSION_TTL_SEC = 30 * 24 * 3600  # 30 days
RPC_TIMEOUT_SEC = 45

_URL_RE = re.compile(r"^https?://[\w.\-\[\]:]+(/.*)?$", re.IGNORECASE)

# In-memory hubs (controller process)
_pair_waiters: dict[str, asyncio.Future] = {}
_sessions_ws: dict[str, Any] = {}  # session_id -> WebSocket
_rpc_waiters: dict[str, asyncio.Future] = {}
_hub_lock = threading.Lock()


def _now() -> float:
    return time.time()


def _hash_secret(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def _row_to_dict(row) -> dict[str, Any]:
    return {k: row[k] for k in row.keys()}


def ensure_remote_tables() -> None:
    with _lock:
        conn = connect()
        try:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS remote_pair_codes (
                    code_id TEXT PRIMARY KEY,
                    code_hash TEXT NOT NULL,
                    controller_name TEXT,
                    created_by TEXT,
                    created_at REAL NOT NULL,
                    expires_at REAL NOT NULL,
                    used_at REAL,
                    status TEXT NOT NULL DEFAULT 'pending'
                );
                CREATE TABLE IF NOT EXISTS remote_sessions (
                    session_id TEXT PRIMARY KEY,
                    token_hash TEXT NOT NULL,
                    role_side TEXT NOT NULL,
                    peer_name TEXT,
                    mode TEXT NOT NULL DEFAULT 'collab',
                    base_url TEXT,
                    created_at REAL NOT NULL,
                    expires_at REAL NOT NULL,
                    last_seen REAL,
                    revoked INTEGER NOT NULL DEFAULT 0,
                    online INTEGER NOT NULL DEFAULT 0,
                    meta TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_remote_pair_exp ON remote_pair_codes(expires_at);
                CREATE INDEX IF NOT EXISTS idx_remote_sess_exp ON remote_sessions(expires_at);
                """
            )
            conn.commit()
        finally:
            conn.close()


def default_remote_settings() -> dict[str, Any]:
    return {
        "enabled": False,
        "role": "",  # controller | agent | ""
        "agent_mode": "collab",  # collab | managed
        "display_name": "",
        "controller_base_url": "",
        "public_base_url": "",
        "active_session_id": "",
        "active_peer_name": "",
        "status": "idle",  # idle | waiting_pair | connected | managed_lock
    }


def get_remote_settings() -> dict[str, Any]:
    raw = get_meta(META_REMOTE)
    base = default_remote_settings()
    if not raw:
        return base
    try:
        data = json.loads(raw)
        if isinstance(data, dict):
            base.update({k: data[k] for k in base if k in data})
            # private runtime keys (e.g. _session_token) kept in meta JSON
            for k, v in data.items():
                if k.startswith("_"):
                    base[k] = v
    except Exception:
        pass
    return base


def set_remote_settings(patch: dict[str, Any], *, actor: str | None = None) -> dict[str, Any]:
    cur = get_remote_settings()
    allowed = set(default_remote_settings().keys())
    for k, v in (patch or {}).items():
        if k.startswith("_"):
            if v is None:
                cur.pop(k, None)
            else:
                cur[k] = v
            continue
        if k not in allowed:
            continue
        if k == "enabled":
            cur[k] = bool(v)
        elif k == "role":
            role = str(v or "").strip().lower()
            cur[k] = role if role in ("controller", "agent", "") else cur[k]
        elif k == "agent_mode":
            mode = str(v or "").strip().lower()
            cur[k] = mode if mode in ("collab", "managed") else cur[k]
        elif k == "status":
            st = str(v or "").strip().lower()
            if st in ("idle", "waiting_pair", "connected", "managed_lock"):
                cur[k] = st
        else:
            cur[k] = "" if v is None else str(v)
    if not cur["enabled"]:
        cur["role"] = ""
        cur["status"] = "idle"
        cur["active_session_id"] = ""
        cur["active_peer_name"] = ""
        cur.pop("_session_token", None)
    set_meta(META_REMOTE, json.dumps(cur, ensure_ascii=False))
    if actor:
        audit("remote_settings", actor=actor, detail={"enabled": cur["enabled"], "role": cur["role"]})
    return cur


def public_settings_view(st: dict[str, Any] | None = None) -> dict[str, Any]:
    """Strip private keys for API responses."""
    raw = dict(st or get_remote_settings())
    return {k: v for k, v in raw.items() if not str(k).startswith("_")}


def remote_endpoint_id(session_id: str) -> str:
    return f"remote:{session_id}"


def parse_remote_endpoint_id(endpoint_id: str | None) -> str | None:
    if not endpoint_id:
        return None
    s = str(endpoint_id).strip()
    if s.startswith("remote:"):
        sid = s[7:].strip()
        return sid or None
    return None


def normalize_base_url(url: str) -> str:
    u = (url or "").strip().rstrip("/")
    if not u:
        raise ValueError("请填写主控 DockerOps 访问地址（域名或 IP，需可打开网页）")
    if not re.match(r"^https?://", u, re.I):
        # bare host → assume https
        if "://" in u:
            raise ValueError("地址仅支持 http:// 或 https://")
        u = "https://" + u
    u = u.rstrip("/")
    if not _URL_RE.match(u):
        raise ValueError("地址格式无效，示例：https://ops.example.com 或 http://192.168.1.10:9080")
    return u


def _pair_material(code_id: str, secret_core: str) -> str:
    return f"{code_id.lower()}:{secret_core.upper().replace('-', '').replace(' ', '')}"


def create_pair_code(*, controller_name: str = "", created_by: str = "") -> dict[str, Any]:
    """Controller generates a 60s one-time pair code for agents."""
    ensure_remote_tables()
    # expire old
    _expire_pairs()
    code_id = uuid.uuid4().hex[:12]
    # human-friendly code: 10 chars
    secret = secrets.token_urlsafe(9).replace("-", "").replace("_", "")[:10].upper()
    display = f"{secret[:5]}-{secret[5:]}"
    full = f"{code_id}.{display}"
    now = _now()
    exp = now + PAIR_TTL_SEC
    with _lock:
        conn = connect()
        try:
            conn.execute(
                """
                INSERT INTO remote_pair_codes
                (code_id, code_hash, controller_name, created_by, created_at, expires_at, status)
                VALUES (?,?,?,?,?,?, 'pending')
                """,
                (
                    code_id,
                    _hash_secret(_pair_material(code_id, secret)),
                    controller_name or "主控",
                    created_by,
                    now,
                    exp,
                ),
            )
            conn.commit()
        finally:
            conn.close()
    st = get_remote_settings()
    st["status"] = "waiting_pair"
    set_remote_settings(st)
    return {
        "ok": True,
        "code_id": code_id,
        "pair_code": full,
        "display_code": display,
        "expires_at": exp,
        "expires_in": PAIR_TTL_SEC,
        "controller_name": controller_name or st.get("display_name") or "主控",
        "message": "请在被控端填写主控 DockerOps 地址并粘贴此凭证（60 秒内有效；哪吒同款拨出，不开 Docker 端口）",
    }


def _expire_pairs() -> None:
    now = _now()
    with _lock:
        conn = connect()
        try:
            conn.execute(
                "UPDATE remote_pair_codes SET status='expired' WHERE status='pending' AND expires_at < ?",
                (now,),
            )
            conn.commit()
        finally:
            conn.close()


def get_pair_status(code_id: str | None = None) -> dict[str, Any]:
    ensure_remote_tables()
    _expire_pairs()
    with _lock:
        conn = connect()
        try:
            if code_id:
                row = conn.execute(
                    "SELECT * FROM remote_pair_codes WHERE code_id = ?", (code_id,)
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT * FROM remote_pair_codes ORDER BY created_at DESC LIMIT 1"
                ).fetchone()
        finally:
            conn.close()
    if not row:
        return {"ok": True, "status": "none"}
    d = _row_to_dict(row)
    left = max(0, int(d["expires_at"] - _now())) if d["status"] == "pending" else 0
    return {
        "ok": True,
        "status": d["status"],
        "code_id": d["code_id"],
        "expires_at": d["expires_at"],
        "expires_in": left,
        "controller_name": d.get("controller_name") or "",
    }


def redeem_pair_code(
    pair_code: str,
    *,
    agent_name: str,
    mode: str = "collab",
    agent_base_hint: str = "",
) -> dict[str, Any]:
    """
    Validate pair code on controller (called from agent WS hello or HTTP redeem).
    Returns session credentials for both sides.
    """
    ensure_remote_tables()
    _expire_pairs()
    raw = (pair_code or "").strip()
    parts = raw.split(".", 1)
    if len(parts) != 2:
        raise ValueError("凭证格式无效，应为 codeId.XXXXX-XXXXX")
    code_id, rest = parts[0].strip().lower(), parts[1].strip()
    secret_core = rest.replace("-", "").replace(" ", "").upper()
    if len(code_id) < 8 or len(secret_core) < 8:
        raise ValueError("凭证格式无效")
    material_hash = _hash_secret(_pair_material(code_id, secret_core))
    with _lock:
        conn = connect()
        try:
            row = conn.execute(
                "SELECT * FROM remote_pair_codes WHERE code_id = ?", (code_id,)
            ).fetchone()
            if not row:
                raise ValueError("凭证不存在或已失效")
            d = _row_to_dict(row)
            if d["status"] != "pending":
                raise ValueError("凭证已使用或已过期")
            if d["expires_at"] < _now():
                conn.execute(
                    "UPDATE remote_pair_codes SET status='expired' WHERE code_id = ?",
                    (code_id,),
                )
                conn.commit()
                raise ValueError("凭证已过期，请在主控重新生成")
            if d["code_hash"] != material_hash:
                raise ValueError("凭证不正确")

            session_id = uuid.uuid4().hex
            session_token = secrets.token_urlsafe(32)
            now = _now()
            exp = now + SESSION_TTL_SEC
            mode_n = "managed" if str(mode).lower() == "managed" else "collab"
            conn.execute(
                """
                INSERT INTO remote_sessions
                (session_id, token_hash, role_side, peer_name, mode, base_url,
                 created_at, expires_at, last_seen, revoked, online, meta)
                VALUES (?,?, 'agent_link', ?, ?, ?, ?, ?, ?, 0, 0, ?)
                """,
                (
                    session_id,
                    _hash_secret(session_token),
                    agent_name or "被控",
                    mode_n,
                    agent_base_hint or "",
                    now,
                    exp,
                    now,
                    json.dumps({"controller_name": d.get("controller_name") or "主控"}, ensure_ascii=False),
                ),
            )
            conn.execute(
                "UPDATE remote_pair_codes SET status='used', used_at=? WHERE code_id=?",
                (now, code_id),
            )
            conn.commit()
        finally:
            conn.close()

    st = get_remote_settings()
    st["status"] = "connected"
    st["active_session_id"] = session_id
    st["active_peer_name"] = agent_name or "被控"
    # controller remembers last pair mode preference for UI
    if mode_n == "managed":
        st["agent_mode"] = "managed"
    set_remote_settings(st)
    audit(
        "remote_pair_ok",
        actor="system",
        detail={"session_id": session_id, "agent": agent_name, "mode": mode_n},
    )
    return {
        "ok": True,
        "session_id": session_id,
        "session_token": session_token,
        "mode": mode_n,
        "expires_at": exp,
        "controller_name": d.get("controller_name") or "主控",
        "agent_name": agent_name or "被控",
        "message": "配对成功",
    }


def list_remote_sessions(*, online_only: bool = False) -> list[dict[str, Any]]:
    ensure_remote_tables()
    with _lock:
        conn = connect()
        try:
            rows = conn.execute(
                "SELECT * FROM remote_sessions WHERE revoked = 0 AND expires_at > ? ORDER BY created_at DESC",
                (_now(),),
            ).fetchall()
        finally:
            conn.close()
    out = []
    for r in rows:
        d = _row_to_dict(r)
        sid = d["session_id"]
        is_online = sid in _sessions_ws or bool(d.get("online"))
        if online_only and not is_online:
            continue
        out.append(
            {
                "id": sid,
                "session_id": sid,
                "name": d.get("peer_name") or "远程节点",
                "peer_name": d.get("peer_name") or "远程节点",
                "mode": d.get("mode") or "collab",
                "online": is_online,
                "last_seen": d.get("last_seen"),
                "created_at": d.get("created_at"),
                "kind": "remote_agent",
                "is_local": False,
                "docker_host": f"remote://{sid[:8]}",
                "capabilities": {
                    "local": False,
                    "resources": True,
                    "console": False,
                    "compose": False,
                    "unraid": False,
                    "update_detect": True,
                    "remote": True,
                },
            }
        )
    return out


def get_session_by_token(token: str) -> dict[str, Any] | None:
    if not token:
        return None
    ensure_remote_tables()
    th = _hash_secret(token.strip())
    with _lock:
        conn = connect()
        try:
            row = conn.execute(
                "SELECT * FROM remote_sessions WHERE token_hash = ? AND revoked = 0",
                (th,),
            ).fetchone()
        finally:
            conn.close()
    if not row:
        return None
    d = _row_to_dict(row)
    if d["expires_at"] < _now():
        return None
    return d


def get_session(session_id: str) -> dict[str, Any] | None:
    if not session_id:
        return None
    ensure_remote_tables()
    with _lock:
        conn = connect()
        try:
            row = conn.execute(
                "SELECT * FROM remote_sessions WHERE session_id = ? AND revoked = 0",
                (session_id,),
            ).fetchone()
        finally:
            conn.close()
    if not row:
        return None
    d = _row_to_dict(row)
    if d["expires_at"] < _now():
        return None
    d["online"] = session_id in _sessions_ws or bool(d.get("online"))
    return d


async def notify_agent_mode(session_id: str, mode: str) -> bool:
    """Push mode_change to an online agent WebSocket. Returns True if sent."""
    ws = _sessions_ws.get(session_id)
    if ws is None:
        return False
    mode_n = "managed" if str(mode).lower() == "managed" else "collab"
    await ws.send_text(
        json.dumps(
            {"type": "mode_change", "mode": mode_n, "session_id": session_id},
            ensure_ascii=False,
        )
    )
    return True


def set_session_mode(session_id: str, mode: str, *, actor: str | None = None) -> dict[str, Any]:
    """Controller switches collab <-> managed for an active session."""
    mode_n = "managed" if str(mode).lower() == "managed" else "collab"
    sess = get_session(session_id)
    if not sess:
        raise ValueError("远程会话不存在或已断开")
    with _lock:
        conn = connect()
        try:
            conn.execute(
                "UPDATE remote_sessions SET mode = ?, last_seen = ? WHERE session_id = ?",
                (mode_n, _now(), session_id),
            )
            conn.commit()
        finally:
            conn.close()
    # best-effort notify if loop is running (async route also calls notify_agent_mode)
    ws = _sessions_ws.get(session_id)
    if ws is not None:
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(notify_agent_mode(session_id, mode_n))
        except RuntimeError:
            pass
    if actor:
        audit(
            "remote_mode_change",
            actor=actor,
            detail={"session_id": session_id, "mode": mode_n},
        )
    updated = get_session(session_id) or {}
    updated["mode"] = mode_n
    return {
        "ok": True,
        "session_id": session_id,
        "mode": mode_n,
        "online": bool(updated.get("online")),
        "peer_name": updated.get("peer_name") or "",
        "message": f"已切换为「{'托管锁定' if mode_n == 'managed' else '协同'}」",
    }


def agent_is_managed_locked() -> bool:
    """True when this process is agent under managed lock (local writes blocked)."""
    st = get_remote_settings()
    if not st.get("enabled") or st.get("role") != "agent":
        return False
    if (st.get("agent_mode") or "") == "managed" and st.get("status") in (
        "connected",
        "managed_lock",
        "waiting_pair",
    ):
        # prefer explicit managed_lock / connected with managed mode
        if st.get("status") == "managed_lock":
            return True
        if st.get("status") == "connected" and (st.get("agent_mode") or "") == "managed":
            return True
    return st.get("status") == "managed_lock"


def apply_agent_mode_change(mode: str) -> dict[str, Any]:
    """Called on agent when controller pushes mode_change."""
    mode_n = "managed" if str(mode).lower() == "managed" else "collab"
    st = get_remote_settings()
    st["agent_mode"] = mode_n
    st["status"] = "managed_lock" if mode_n == "managed" else "connected"
    set_remote_settings(st)
    return public_settings_view(st)


# Paths local UI may still POST while managed (auth / remote self-control / prefs read)
_MANAGED_LOCAL_WRITE_ALLOW = (
    "/api/auth",
    "/api/remote",
    "/api/prefs",
    "/api/system/settings",
    "/api/endpoints",
)


def managed_blocks_local_path(method: str, path: str) -> bool:
    """Whether managed lock should reject a local (non-RPC) mutating request."""
    if not agent_is_managed_locked():
        return False
    m = (method or "GET").upper()
    if m in ("GET", "HEAD", "OPTIONS"):
        return False
    p = (path or "").split("?", 1)[0]
    if any(p == a or p.startswith(a + "/") for a in _MANAGED_LOCAL_WRITE_ALLOW):
        return False
    # block docker/resource mutations from local UI
    if p.startswith("/api/"):
        return True
    return False


def touch_session(session_id: str, *, online: bool | None = None) -> None:
    with _lock:
        conn = connect()
        try:
            if online is None:
                conn.execute(
                    "UPDATE remote_sessions SET last_seen = ? WHERE session_id = ?",
                    (_now(), session_id),
                )
            else:
                conn.execute(
                    "UPDATE remote_sessions SET last_seen = ?, online = ? WHERE session_id = ?",
                    (_now(), 1 if online else 0, session_id),
                )
            conn.commit()
        finally:
            conn.close()


def revoke_session(session_id: str, *, actor: str | None = None) -> bool:
    with _lock:
        conn = connect()
        try:
            cur = conn.execute(
                "UPDATE remote_sessions SET revoked = 1, online = 0 WHERE session_id = ?",
                (session_id,),
            )
            conn.commit()
            n = cur.rowcount
        finally:
            conn.close()
    ws = _sessions_ws.pop(session_id, None)
    if ws is not None:
        try:
            asyncio.get_event_loop().create_task(ws.close())
        except Exception:
            pass
    st = get_remote_settings()
    if st.get("active_session_id") == session_id:
        st["active_session_id"] = ""
        st["active_peer_name"] = ""
        st["status"] = "idle"
        set_remote_settings(st)
    if actor:
        audit("remote_disconnect", actor=actor, detail={"session_id": session_id})
    return n > 0


def register_agent_ws(session_id: str, ws: Any) -> None:
    with _hub_lock:
        old = _sessions_ws.get(session_id)
        _sessions_ws[session_id] = ws
    touch_session(session_id, online=True)
    if old is not None and old is not ws:
        try:
            asyncio.get_event_loop().create_task(old.close())
        except Exception:
            pass


def unregister_agent_ws(session_id: str, ws: Any | None = None) -> None:
    with _hub_lock:
        cur = _sessions_ws.get(session_id)
        if ws is None or cur is ws:
            _sessions_ws.pop(session_id, None)
    touch_session(session_id, online=False)


def is_agent_online(session_id: str) -> bool:
    return session_id in _sessions_ws


async def rpc_to_agent(
    session_id: str,
    *,
    method: str,
    path: str,
    body: Any = None,
    query: dict[str, Any] | None = None,
    timeout: float = RPC_TIMEOUT_SEC,
) -> dict[str, Any]:
    ws = _sessions_ws.get(session_id)
    if ws is None:
        raise RuntimeError("远程节点不在线（被控未连接或已断开）")
    req_id = uuid.uuid4().hex
    loop = asyncio.get_event_loop()
    fut: asyncio.Future = loop.create_future()
    with _hub_lock:
        _rpc_waiters[req_id] = fut
    payload = {
        "type": "rpc",
        "id": req_id,
        "method": method.upper(),
        "path": path,
        "query": query or {},
        "body": body,
    }
    try:
        await ws.send_text(json.dumps(payload, ensure_ascii=False))
        result = await asyncio.wait_for(fut, timeout=timeout)
        return result
    except asyncio.TimeoutError as e:
        raise RuntimeError("远程节点响应超时") from e
    finally:
        with _hub_lock:
            _rpc_waiters.pop(req_id, None)


def resolve_rpc_response(message: dict[str, Any]) -> None:
    req_id = message.get("id")
    if not req_id:
        return
    with _hub_lock:
        fut = _rpc_waiters.get(req_id)
    if fut and not fut.done():
        fut.set_result(message)


def public_remote_status() -> dict[str, Any]:
    st = get_remote_settings()
    sessions = list_remote_sessions() if st.get("enabled") and st.get("role") == "controller" else []
    online_n = sum(1 for s in sessions if s.get("online"))
    locked = agent_is_managed_locked()
    role = st.get("role") or ""
    if not st.get("enabled"):
        hint = "远程模式关闭"
    elif role == "controller":
        hint = f"主控端 · 已配对 {len(sessions)} · 在线 {online_n}（被控拨出，无 Docker 端口）"
    elif role == "agent":
        if locked:
            hint = f"被控端 · 托管锁定中 · 主控：{st.get('active_peer_name') or st.get('controller_base_url') or '—'}"
        elif st.get("status") in ("connected", "managed_lock"):
            hint = f"被控端 · 协同已连接 · 主控：{st.get('active_peer_name') or '—'}"
        else:
            hint = "被控端 · 填写主控地址与凭证后连接"
    else:
        hint = "请选择主控端或被控端"
    return {
        "enabled": bool(st.get("enabled")),
        "role": role,
        "agent_mode": st.get("agent_mode") or "collab",
        "display_name": st.get("display_name") or "",
        "controller_base_url": st.get("controller_base_url") or "",
        "public_base_url": st.get("public_base_url") or "",
        "status": st.get("status") or "idle",
        "active_session_id": st.get("active_session_id") or "",
        "active_peer_name": st.get("active_peer_name") or "",
        "managed_locked": locked,
        "sessions": sessions,
        "online_count": online_n,
        "pair_ttl_sec": PAIR_TTL_SEC,
        "hint": hint,
    }


# ── Agent-side local RPC execution (runs on agent process) ─────────────

def execute_local_rpc(
    method: str,
    path: str,
    *,
    query: dict[str, Any] | None = None,
    body: Any = None,
    actor: str = "remote-controller",
) -> dict[str, Any]:
    """
    Map a subset of DockerOps HTTP API to local function calls on the agent.
    """
    method = (method or "GET").upper()
    path = (path or "").split("?", 1)[0]
    q = query or {}
    b = body if isinstance(body, dict) else {}

    # lazy imports to avoid cycles
    from docker_client import list_containers, ping, get_container
    from docker_resources import (
        lifecycle,
        images_list,
        networks_list,
        volumes_list,
        activity_stats,
        sys_info,
        sys_df,
    )
    from doctor import diagnose_all
    from host_platform import platform_info
    from manager import managers_summary
    from update_detect import detect_updates, get_cached_update_status, one_click_update
    from ops import safe_update, records
    from events_stream import recent_events
    from config import get_settings

    try:
        if path == "/api/health" and method == "GET":
            engine = ping()
            try:
                plat = platform_info()
            except Exception:
                plat = {}
            return {
                "ok": True,
                "service": "dockerops-agent",
                "version": "remote",
                "docker": engine,
                "platform": plat.get("platform"),
                "takeover_enabled": get_settings().takeover_enabled,
            }

        if path == "/api/containers" and method == "GET":
            items = list_containers(all=True)
            return {"ok": True, "count": len(items), "items": items}

        if path.startswith("/api/containers/") and method == "GET":
            cid = path.split("/api/containers/", 1)[1].strip("/")
            if not cid:
                return {"ok": False, "detail": "missing id", "status": 400}
            c = get_container(cid)
            return {"ok": True, "item": c}

        m = re.match(r"^/api/containers/([^/]+)/(start|stop|restart|pause|unpause|kill|remove)$", path)
        if m and method == "POST":
            cid, action = m.group(1), m.group(2)
            force = bool(q.get("force") or b.get("force"))
            volumes = bool(q.get("volumes") or b.get("volumes"))
            return lifecycle(action, cid, actor=actor, force=force, volumes=volumes)

        if path == "/api/ops/detect-updates" and method in ("GET", "POST"):
            only_running = bool(q.get("only_running", b.get("only_running", True)))
            if isinstance(only_running, str):
                only_running = only_running.lower() in ("1", "true", "yes")
            return detect_updates(only_running=only_running, actor=actor, persist=True)

        if path == "/api/ops/update-status" and method == "GET":
            cache = get_cached_update_status() or {}
            return {"ok": True, **cache}

        if path == "/api/ops/one-click-update" and method == "POST":
            return one_click_update(
                only_available=bool(b.get("only_available", True)),
                only_running=bool(b.get("only_running", True)),
                container_ids=b.get("container_ids"),
                actor=actor,
            )

        m = re.match(r"^/api/ops/update/([^/]+)$", path)
        if m and method == "POST":
            return safe_update(m.group(1), image=b.get("image"), actor=actor)

        if path == "/api/images" and method == "GET":
            return images_list()

        if path == "/api/networks" and method == "GET":
            return networks_list()

        if path == "/api/volumes" and method == "GET":
            return volumes_list()

        if path == "/api/system/info" and method == "GET":
            return {"ok": True, "info": sys_info()}

        if path == "/api/system/df" and method == "GET":
            return {"ok": True, "df": sys_df()}

        if path == "/api/activity" and method == "GET":
            return activity_stats()

        if path == "/api/doctor" and method == "GET":
            return diagnose_all()

        if path == "/api/summary" and method == "GET":
            return {"ok": True, **managers_summary()}

        if path == "/api/events" and method == "GET":
            limit = int(q.get("limit") or 30)
            return {"ok": True, "items": recent_events(limit=limit)}

        if path == "/api/ops/records" and method == "GET":
            return {"ok": True, "items": records(limit=int(q.get("limit") or 50))}

        if path == "/api/platform" and method == "GET":
            return {"ok": True, **platform_info()}

        return {"ok": False, "detail": f"远程代理不支持 {method} {path}", "status": 404}
    except Exception as e:
        return {"ok": False, "detail": str(e), "status": 500}

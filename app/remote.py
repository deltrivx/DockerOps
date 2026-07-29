"""
Remote mode v0.8: agent generates pair; controller dials into agent hub.

- 被控：填本机公网/IP → 生成 60s 凭证 → 托管 WSS 枢纽 → 执行 Docker RPC
- 主控：粘贴凭证 → 主动连被控 → 顶栏 remote 节点 → 下发 RPC
- 不开 Docker Engine 2375/2376；仅 DockerOps HTTP/WSS
- 协同：被控本地可写；托管：被控本地写 423，可「切换模式」
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import re
import secrets
import threading
import time
import uuid
from typing import Any
from urllib.parse import urlparse, urlunparse

from db import (
    audit,
    connect,
    get_meta,
    set_meta,
    _lock,
)

META_REMOTE = "remote_settings"
PAIR_TTL_SEC = 60
SESSION_TTL_SEC = 30 * 24 * 3600
RPC_TIMEOUT_SEC = 45
PAIR_PREFIX = "dop1"

_URL_RE = re.compile(r"^https?://[\w.\-\[\]:]+(/.*)?$", re.IGNORECASE)

# Agent-side hub: session_id -> inbound WebSocket (from controller)
_sessions_ws: dict[str, Any] = {}
_rpc_waiters: dict[str, asyncio.Future] = {}  # used on controller dial side too via remote_controller
_hub_lock = threading.Lock()


def _now() -> float:
    return time.time()


def _hash_secret(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def _row_to_dict(row) -> dict[str, Any]:
    return {k: row[k] for k in row.keys()}


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _b64url_decode(s: str) -> bytes:
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad)


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
                    status TEXT NOT NULL DEFAULT 'pending',
                    mode TEXT DEFAULT 'collab',
                    public_base_url TEXT,
                    agent_name TEXT,
                    meta TEXT
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
            # best-effort migrations for older 0.7 tables
            cols = {r[1] for r in conn.execute("PRAGMA table_info(remote_pair_codes)").fetchall()}
            for col, decl in (
                ("mode", "TEXT DEFAULT 'collab'"),
                ("public_base_url", "TEXT"),
                ("agent_name", "TEXT"),
                ("meta", "TEXT"),
            ):
                if col not in cols:
                    try:
                        conn.execute(f"ALTER TABLE remote_pair_codes ADD COLUMN {col} {decl}")
                    except Exception:
                        pass
            conn.commit()
        finally:
            conn.close()


def default_remote_settings() -> dict[str, Any]:
    return {
        "enabled": False,
        "role": "",  # controller | agent | ""
        "agent_mode": "",  # collab | managed | "" (未选，用于分步 UI)
        "display_name": "",
        "controller_base_url": "",  # legacy / unused in 0.8 dial direction
        "public_base_url": "",  # agent: address embedded in pair
        "active_session_id": "",
        "active_peer_name": "",
        "status": "idle",  # idle | waiting_pair | connected | managed_lock
        "ui_phase": "setup",  # setup | waiting | collab_banner | managed_full | mode_pick
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
            if mode in ("collab", "managed", ""):
                cur[k] = mode
        elif k == "status":
            st = str(v or "").strip().lower()
            if st in ("idle", "waiting_pair", "connected", "managed_lock"):
                cur[k] = st
        elif k == "ui_phase":
            phase = str(v or "").strip().lower()
            if phase in ("setup", "waiting", "collab_banner", "managed_full", "mode_pick"):
                cur[k] = phase
        else:
            cur[k] = "" if v is None else str(v)
    if not cur["enabled"]:
        cur["role"] = ""
        cur["status"] = "idle"
        cur["ui_phase"] = "setup"
        cur["active_session_id"] = ""
        cur["active_peer_name"] = ""
        cur.pop("_session_token", None)
        cur.pop("_controller_sessions", None)
    set_meta(META_REMOTE, json.dumps(cur, ensure_ascii=False))
    if actor:
        audit("remote_settings", actor=actor, detail={"enabled": cur["enabled"], "role": cur["role"]})
    return cur


def public_settings_view(st: dict[str, Any] | None = None) -> dict[str, Any]:
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


def normalize_base_url(url: str, *, label: str = "DockerOps 访问地址") -> str:
    u = (url or "").strip().rstrip("/")
    if not u:
        raise ValueError(f"请填写{label}（域名或 IP，需可打开网页）")
    if not re.match(r"^https?://", u, re.I):
        if "://" in u:
            raise ValueError("地址仅支持 http:// 或 https://")
        u = "https://" + u
    u = u.rstrip("/")
    if not _URL_RE.match(u):
        raise ValueError("地址格式无效，示例：https://ops.example.com 或 http://192.168.1.10:9080")
    return u


def hub_ws_url(base_url: str) -> str:
    u = urlparse(base_url)
    scheme = "wss" if u.scheme == "https" else "ws"
    return urlunparse((scheme, u.netloc, "/api/remote/hub", "", "", ""))


def _pair_material(code_id: str, secret_core: str) -> str:
    return f"{code_id.lower()}:{secret_core.upper().replace('-', '').replace(' ', '')}"


def encode_pair_code(
    *,
    code_id: str,
    secret: str,
    base_url: str,
    mode: str,
    agent_name: str,
    expires_at: float,
) -> str:
    payload = {
        "v": 1,
        "base": base_url,
        "id": code_id,
        "mode": mode,
        "name": agent_name,
        "exp": int(expires_at),
    }
    body = _b64url_encode(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    return f"{PAIR_PREFIX}.{body}.{secret}"


def parse_pair_code(pair_code: str) -> dict[str, Any]:
    """Parse dop1.<b64>.<secret> (0.8) or legacy codeId.XXXXX-XXXXX (rejected with hint)."""
    raw = (pair_code or "").strip()
    if not raw:
        raise ValueError("请粘贴连接凭证")
    parts = raw.split(".")
    if len(parts) >= 3 and parts[0].lower() == PAIR_PREFIX:
        try:
            payload = json.loads(_b64url_decode(parts[1]).decode("utf-8"))
        except Exception as e:
            raise ValueError("凭证格式无效（无法解析）") from e
        secret = parts[2].strip()
        code_id = str(payload.get("id") or "").strip().lower()
        base = str(payload.get("base") or "").strip()
        if not code_id or not base or not secret:
            raise ValueError("凭证内容不完整")
        return {
            "version": 1,
            "code_id": code_id,
            "secret": secret,
            "base_url": normalize_base_url(base, label="被控地址"),
            "mode": "managed" if str(payload.get("mode") or "").lower() == "managed" else "collab",
            "agent_name": str(payload.get("name") or "被控").strip() or "被控",
            "expires_at": float(payload.get("exp") or 0),
            "raw": raw,
        }
    # legacy 0.7 format — no embedded URL
    if len(parts) == 2 and not parts[0].lower().startswith("dop"):
        raise ValueError("这是旧版(0.7)凭证，无法用于 0.8。请在被控端重新生成连接凭证后粘贴到主控。")
    raise ValueError("凭证格式无效，应为 dop1.... 连接凭证")


def create_pair_code(
    *,
    public_base_url: str,
    mode: str = "",
    agent_name: str = "",
    created_by: str = "",
) -> dict[str, Any]:
    """Agent generates a 60s one-time pair code embedding its public base URL."""
    ensure_remote_tables()
    _expire_pairs()
    base = normalize_base_url(public_base_url, label="本机公网域名或 IP")
    mode_raw = str(mode or "").lower().strip()
    if mode_raw not in ("collab", "managed"):
        raise ValueError("请先选择协同或托管模式")
    mode_n = mode_raw
    name = (agent_name or get_remote_settings().get("display_name") or "被控").strip() or "被控"
    code_id = uuid.uuid4().hex[:12]
    secret = secrets.token_urlsafe(9).replace("-", "").replace("_", "")[:10].upper()
    now = _now()
    exp = now + PAIR_TTL_SEC
    full = encode_pair_code(
        code_id=code_id,
        secret=secret,
        base_url=base,
        mode=mode_n,
        agent_name=name,
        expires_at=exp,
    )
    with _lock:
        conn = connect()
        try:
            # invalidate other pending pairs
            conn.execute(
                "UPDATE remote_pair_codes SET status='expired' WHERE status='pending'"
            )
            conn.execute(
                """
                INSERT INTO remote_pair_codes
                (code_id, code_hash, controller_name, created_by, created_at, expires_at, status,
                 mode, public_base_url, agent_name, meta)
                VALUES (?,?,?,?,?,?, 'pending', ?, ?, ?, ?)
                """,
                (
                    code_id,
                    _hash_secret(_pair_material(code_id, secret)),
                    "",
                    created_by,
                    now,
                    exp,
                    mode_n,
                    base,
                    name,
                    json.dumps({"v": 1}, ensure_ascii=False),
                ),
            )
            conn.commit()
        finally:
            conn.close()
    st = get_remote_settings()
    st["status"] = "waiting_pair"
    st["ui_phase"] = "waiting"
    st["public_base_url"] = base
    st["agent_mode"] = mode_n
    st["display_name"] = name
    set_remote_settings(st)
    return {
        "ok": True,
        "code_id": code_id,
        "pair_code": full,
        "public_base_url": base,
        "mode": mode_n,
        "agent_name": name,
        "expires_at": exp,
        "expires_in": PAIR_TTL_SEC,
        "message": "请将连接凭证粘贴到主控端（60 秒内有效）。主控需能访问本机填的域名/IP。",
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
    st = get_remote_settings()
    if st.get("status") == "waiting_pair" and st.get("role") == "agent":
        # if no pending left, reset waiting UI
        with _lock:
            conn = connect()
            try:
                row = conn.execute(
                    "SELECT 1 FROM remote_pair_codes WHERE status='pending' AND expires_at >= ? LIMIT 1",
                    (now,),
                ).fetchone()
            finally:
                conn.close()
        if not row and st.get("ui_phase") == "waiting":
            st["status"] = "idle"
            st["ui_phase"] = "setup"
            set_remote_settings(st)


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
        "mode": d.get("mode") or "collab",
        "public_base_url": d.get("public_base_url") or "",
        "agent_name": d.get("agent_name") or "",
        "controller_name": d.get("controller_name") or "",
    }


def redeem_pair_code(
    pair_code: str,
    *,
    controller_name: str = "",
    controller_base_hint: str = "",
) -> dict[str, Any]:
    """
    Validate pair on agent hub when controller connects with hello.
    Returns session credentials.
    """
    ensure_remote_tables()
    _expire_pairs()
    parsed = parse_pair_code(pair_code)
    code_id = parsed["code_id"]
    secret_core = parsed["secret"].replace("-", "").replace(" ", "").upper()
    material_hash = _hash_secret(_pair_material(code_id, secret_core))
    ctrl_name = (controller_name or "主控").strip() or "主控"

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
                raise ValueError("凭证已过期，请在被控重新生成")
            if d["code_hash"] != material_hash:
                raise ValueError("凭证不正确")

            session_id = uuid.uuid4().hex
            session_token = secrets.token_urlsafe(32)
            now = _now()
            exp = now + SESSION_TTL_SEC
            mode_n = d.get("mode") or parsed.get("mode") or "collab"
            mode_n = "managed" if str(mode_n).lower() == "managed" else "collab"
            agent_name = d.get("agent_name") or parsed.get("agent_name") or "被控"
            public_base = d.get("public_base_url") or parsed.get("base_url") or ""

            conn.execute(
                """
                INSERT INTO remote_sessions
                (session_id, token_hash, role_side, peer_name, mode, base_url,
                 created_at, expires_at, last_seen, revoked, online, meta)
                VALUES (?,?, 'agent_hub', ?, ?, ?, ?, ?, ?, 0, 1, ?)
                """,
                (
                    session_id,
                    _hash_secret(session_token),
                    ctrl_name,
                    mode_n,
                    controller_base_hint or "",
                    now,
                    exp,
                    now,
                    json.dumps(
                        {
                            "controller_name": ctrl_name,
                            "agent_name": agent_name,
                            "public_base_url": public_base,
                            "side": "agent",
                        },
                        ensure_ascii=False,
                    ),
                ),
            )
            conn.execute(
                "UPDATE remote_pair_codes SET status='used', used_at=?, controller_name=? WHERE code_id=?",
                (now, ctrl_name, code_id),
            )
            conn.commit()
        finally:
            conn.close()

    st = get_remote_settings()
    st["status"] = "managed_lock" if mode_n == "managed" else "connected"
    st["ui_phase"] = "managed_full" if mode_n == "managed" else "collab_banner"
    st["active_session_id"] = session_id
    st["active_peer_name"] = ctrl_name
    st["agent_mode"] = mode_n
    st["public_base_url"] = public_base or st.get("public_base_url") or ""
    set_remote_settings(st)
    audit(
        "remote_pair_ok",
        actor="system",
        detail={"session_id": session_id, "controller": ctrl_name, "mode": mode_n},
    )
    return {
        "ok": True,
        "session_id": session_id,
        "session_token": session_token,
        "mode": mode_n,
        "expires_at": exp,
        "controller_name": ctrl_name,
        "agent_name": agent_name,
        "public_base_url": public_base,
        "message": "配对成功",
    }


def register_controller_session(
    *,
    session_id: str,
    session_token: str,
    agent_name: str,
    agent_base_url: str,
    mode: str,
    expires_at: float | None = None,
) -> dict[str, Any]:
    """Controller persists an outbound session after successful dial."""
    ensure_remote_tables()
    now = _now()
    exp = float(expires_at or (now + SESSION_TTL_SEC))
    mode_n = "managed" if str(mode).lower() == "managed" else "collab"
    with _lock:
        conn = connect()
        try:
            conn.execute("DELETE FROM remote_sessions WHERE session_id = ?", (session_id,))
            conn.execute(
                """
                INSERT INTO remote_sessions
                (session_id, token_hash, role_side, peer_name, mode, base_url,
                 created_at, expires_at, last_seen, revoked, online, meta)
                VALUES (?,?, 'controller_link', ?, ?, ?, ?, ?, ?, 0, 1, ?)
                """,
                (
                    session_id,
                    _hash_secret(session_token),
                    agent_name or "被控",
                    mode_n,
                    agent_base_url,
                    now,
                    exp,
                    now,
                    json.dumps(
                        {
                            "session_token": session_token,
                            "agent_base_url": agent_base_url,
                            "side": "controller",
                        },
                        ensure_ascii=False,
                    ),
                ),
            )
            conn.commit()
        finally:
            conn.close()
    st = get_remote_settings()
    st["status"] = "connected"
    st["active_session_id"] = session_id
    st["active_peer_name"] = agent_name or "被控"
    set_remote_settings(st)
    return get_session(session_id) or {"session_id": session_id}


def list_remote_sessions(*, online_only: bool = False, role_side: str | None = None) -> list[dict[str, Any]]:
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
        if role_side and d.get("role_side") != role_side:
            continue
        sid = d["session_id"]
        # online: agent hub has inbound ws; controller uses dial runtime (marked in meta/online col)
        is_online = bool(d.get("online")) or sid in _sessions_ws
        # controller side: remote_controller reports online via touch
        if online_only and not is_online:
            # still include if dial runtime says online — checked by caller sometimes
            if sid not in _sessions_ws and not d.get("online"):
                continue
        meta = {}
        try:
            meta = json.loads(d.get("meta") or "{}")
        except Exception:
            pass
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
                "base_url": d.get("base_url") or meta.get("agent_base_url") or "",
                "role_side": d.get("role_side"),
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


def list_controller_outbound_sessions() -> list[dict[str, Any]]:
    return list_remote_sessions(role_side="controller_link")


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
    try:
        d["meta_obj"] = json.loads(d.get("meta") or "{}")
    except Exception:
        d["meta_obj"] = {}
    return d


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
            loop = asyncio.get_event_loop()
            loop.create_task(ws.close())
        except Exception:
            pass
    st = get_remote_settings()
    if st.get("active_session_id") == session_id:
        st["active_session_id"] = ""
        st["active_peer_name"] = ""
        if st.get("role") == "agent":
            st["status"] = "idle"
            st["ui_phase"] = "setup"
        set_remote_settings(st)
    if actor:
        audit("remote_disconnect", actor=actor, detail={"session_id": session_id})
    return n > 0


def register_agent_ws(session_id: str, ws: Any) -> None:
    """Agent hub: register inbound controller websocket."""
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
    """True if RPC channel exists.

    - On agent process: inbound hub socket
    - On controller process: dial client marks online via remote_controller + DB online flag
    """
    if session_id in _sessions_ws:
        return True
    try:
        from remote_controller import is_session_dial_online

        if is_session_dial_online(session_id):
            return True
    except Exception:
        pass
    sess = get_session(session_id)
    return bool(sess and sess.get("online"))


async def notify_peer_mode(session_id: str, mode: str) -> bool:
    """Push mode_change on agent hub socket (to controller) if present."""
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


# backward-compatible name
async def notify_agent_mode(session_id: str, mode: str) -> bool:
    return await notify_peer_mode(session_id, mode)


def set_session_mode(session_id: str, mode: str, *, actor: str | None = None) -> dict[str, Any]:
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
    ws = _sessions_ws.get(session_id)
    if ws is not None:
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(notify_peer_mode(session_id, mode_n))
        except RuntimeError:
            pass
    st = get_remote_settings()
    if st.get("role") == "agent" and st.get("active_session_id") == session_id:
        st["agent_mode"] = mode_n
        st["status"] = "managed_lock" if mode_n == "managed" else "connected"
        st["ui_phase"] = "managed_full" if mode_n == "managed" else "collab_banner"
        set_remote_settings(st)
    if actor:
        audit(
            "remote_mode_change",
            actor=actor,
            detail={"session_id": session_id, "mode": mode_n},
        )
    updated = get_session(session_id) or {}
    return {
        "ok": True,
        "session_id": session_id,
        "mode": mode_n,
        "online": bool(updated.get("online")),
        "peer_name": updated.get("peer_name") or "",
        "message": f"已切换为「{'托管锁定' if mode_n == 'managed' else '协同'}」",
    }


def agent_is_managed_locked() -> bool:
    st = get_remote_settings()
    if not st.get("enabled") or st.get("role") != "agent":
        return False
    if st.get("ui_phase") == "managed_full":
        return True
    if st.get("status") == "managed_lock":
        return True
    if (st.get("agent_mode") or "") == "managed" and st.get("status") in (
        "connected",
        "managed_lock",
    ):
        return True
    return False


def apply_agent_mode_change(mode: str) -> dict[str, Any]:
    """Local agent applies mode (from UI switch or peer notify)."""
    mode_n = "managed" if str(mode).lower() == "managed" else "collab"
    st = get_remote_settings()
    st["agent_mode"] = mode_n
    st["status"] = "managed_lock" if mode_n == "managed" else "connected"
    st["ui_phase"] = "managed_full" if mode_n == "managed" else "collab_banner"
    set_remote_settings(st)
    sid = st.get("active_session_id") or ""
    if sid:
        try:
            set_session_mode(sid, mode_n)
        except Exception:
            pass
    return public_settings_view(st)


def agent_switch_mode_local(mode: str, *, actor: str | None = None) -> dict[str, Any]:
    """被控点击「切换模式」：可先回 mode_pick，或直接改 collab/managed。"""
    mode_n = str(mode or "").lower().strip()
    st = get_remote_settings()
    if st.get("role") != "agent":
        raise ValueError("仅被控端可切换本地远程展示模式")
    if mode_n == "mode_pick":
        st["ui_phase"] = "mode_pick"
        # keep connection; unlock writes temporarily while picking
        st["status"] = "connected"
        set_remote_settings(st, actor=actor)
        return {
            "ok": True,
            "message": "请重新选择协同或托管",
            "settings": public_settings_view(st),
            "status": public_remote_status(),
        }
    if mode_n not in ("collab", "managed"):
        raise ValueError("mode 须为 collab、managed 或 mode_pick")
    view = apply_agent_mode_change(mode_n)
    sid = st.get("active_session_id") or get_remote_settings().get("active_session_id") or ""
    if sid:
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(notify_peer_mode(sid, mode_n))
        except RuntimeError:
            pass
        # also ask controller dial path if we ever hold reverse — hub notify is enough for inbound
    if actor:
        audit("remote_agent_switch_mode", actor=actor, detail={"mode": mode_n})
    return {
        "ok": True,
        "mode": mode_n,
        "message": f"已切换为「{'托管锁定' if mode_n == 'managed' else '协同'}」",
        "settings": view,
        "status": public_remote_status(),
    }


_MANAGED_LOCAL_WRITE_ALLOW = (
    "/api/auth",
    "/api/remote",
    "/api/prefs",
    "/api/system/settings",
    "/api/endpoints",
)


def managed_blocks_local_path(method: str, path: str) -> bool:
    if not agent_is_managed_locked():
        return False
    # mode_pick allows local writes while choosing
    if get_remote_settings().get("ui_phase") == "mode_pick":
        return False
    m = (method or "GET").upper()
    if m in ("GET", "HEAD", "OPTIONS"):
        return False
    p = (path or "").split("?", 1)[0]
    if any(p == a or p.startswith(a + "/") for a in _MANAGED_LOCAL_WRITE_ALLOW):
        return False
    if p.startswith("/api/"):
        return True
    return False


def resolve_rpc_response(message: dict[str, Any]) -> None:
    req_id = message.get("id")
    if not req_id:
        return
    with _hub_lock:
        fut = _rpc_waiters.get(req_id)
    if fut and not fut.done():
        fut.set_result(message)


def register_rpc_waiter(req_id: str, fut: asyncio.Future) -> None:
    with _hub_lock:
        _rpc_waiters[req_id] = fut


def pop_rpc_waiter(req_id: str) -> None:
    with _hub_lock:
        _rpc_waiters.pop(req_id, None)


async def rpc_to_agent(
    session_id: str,
    *,
    method: str,
    path: str,
    body: Any = None,
    query: dict[str, Any] | None = None,
    timeout: float = RPC_TIMEOUT_SEC,
) -> dict[str, Any]:
    """
    Send RPC to remote agent.

    Prefer controller dial client (outbound WS). Fall back to hub map
    (only valid if this process is agent — normally RPC originates on controller).
    """
    try:
        from remote_controller import rpc_via_dial

        return await rpc_via_dial(
            session_id,
            method=method,
            path=path,
            body=body,
            query=query,
            timeout=timeout,
        )
    except RuntimeError:
        pass
    except Exception:
        pass

    ws = _sessions_ws.get(session_id)
    if ws is None:
        raise RuntimeError("远程节点不在线（未连接或已断开）")
    req_id = uuid.uuid4().hex
    loop = asyncio.get_event_loop()
    fut: asyncio.Future = loop.create_future()
    register_rpc_waiter(req_id, fut)
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
        pop_rpc_waiter(req_id)


def public_remote_status() -> dict[str, Any]:
    st = get_remote_settings()
    role = st.get("role") or ""
    if role == "controller":
        sessions = list_controller_outbound_sessions()
        # merge dial online flags
        try:
            from remote_controller import dial_online_map

            online_map = dial_online_map()
            for s in sessions:
                if online_map.get(s["session_id"]):
                    s["online"] = True
        except Exception:
            pass
    elif role == "agent":
        sessions = list_remote_sessions(role_side="agent_hub")
    else:
        sessions = []
    online_n = sum(1 for s in sessions if s.get("online"))
    locked = agent_is_managed_locked()
    if not st.get("enabled"):
        hint = "远程模式关闭"
    elif role == "controller":
        hint = f"主控端 · 已连接远程 {online_n}/{len(sessions)}（粘贴被控凭证连接，本机功能不受影响）"
    elif role == "agent":
        phase = st.get("ui_phase") or "setup"
        peer = st.get("active_peer_name") or "—"
        if phase == "waiting" or st.get("status") == "waiting_pair":
            hint = "被控端 · 等待主控连接（凭证 60 秒内有效）"
        elif phase == "collab_banner" or (st.get("status") == "connected" and not locked):
            hint = f"远程设备「{peer}」正在协同管理"
        elif phase == "managed_full" or locked:
            hint = f"当前由远程设备「{peer}」完全管理"
        elif phase == "mode_pick":
            hint = "请重新选择协同或托管模式"
        else:
            hint = "被控端 · 选择协同/托管并生成连接凭证"
    else:
        hint = "请选择主控端或被控端"
    return {
        "enabled": bool(st.get("enabled")),
        "role": role,
        "agent_mode": st.get("agent_mode") or "",
        "display_name": st.get("display_name") or "",
        "controller_base_url": st.get("controller_base_url") or "",
        "public_base_url": st.get("public_base_url") or "",
        "status": st.get("status") or "idle",
        "ui_phase": st.get("ui_phase") or "setup",
        "active_session_id": st.get("active_session_id") or "",
        "active_peer_name": st.get("active_peer_name") or "",
        "managed_locked": locked,
        "sessions": sessions,
        "online_count": online_n,
        "pair_ttl_sec": PAIR_TTL_SEC,
        "hint": hint,
    }


def execute_local_rpc(
    method: str,
    path: str,
    *,
    query: dict[str, Any] | None = None,
    body: Any = None,
    actor: str = "remote-controller",
) -> dict[str, Any]:
    """Map a subset of DockerOps HTTP API to local calls on the agent."""
    method = (method or "GET").upper()
    path = (path or "").split("?", 1)[0]
    q = query or {}
    b = body if isinstance(body, dict) else {}

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
            if "/logs" in cid or not cid:
                return {"ok": False, "detail": "missing id", "status": 400}
            # strip trailing action if any
            cid = cid.split("/", 1)[0]
            c = get_container(cid)
            return {"ok": True, "item": c}

        m = re.match(
            r"^/api/containers/([^/]+)/(start|stop|restart|pause|unpause|kill|remove)$",
            path,
        )
        if m and method == "POST":
            cid, action = m.group(1), m.group(2)
            force = bool(q.get("force") or b.get("force"))
            volumes = bool(q.get("volumes") or b.get("volumes"))
            return lifecycle(action, cid, actor=actor, force=force, volumes=volumes)

        m = re.match(r"^/api/containers/([^/]+)$", path)
        if m and method == "DELETE":
            return lifecycle(
                "remove",
                m.group(1),
                actor=actor,
                force=bool(q.get("force") or b.get("force")),
                volumes=bool(q.get("volumes") or b.get("volumes")),
            )

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

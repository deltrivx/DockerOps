"""
Agent-side dial-out client: connects to controller WebSocket hub (Nezha-style).
Runs in a background thread with its own asyncio loop.
"""
from __future__ import annotations

import asyncio
import json
import threading
import time
from typing import Any
from urllib.parse import urlparse, urlunparse

from remote import (
    apply_agent_mode_change,
    execute_local_rpc,
    get_remote_settings,
    set_remote_settings,
)

_agent_lock = threading.Lock()
_agent_thread: threading.Thread | None = None
_agent_stop = threading.Event()
_agent_state: dict[str, Any] = {
    "running": False,
    "connected": False,
    "last_error": "",
    "controller_url": "",
    "session_id": "",
}


def agent_runtime_state() -> dict[str, Any]:
    with _agent_lock:
        return dict(_agent_state)


def _set_state(**kwargs: Any) -> None:
    with _agent_lock:
        _agent_state.update(kwargs)


def _ws_url(base_url: str) -> str:
    u = urlparse(base_url)
    scheme = "wss" if u.scheme == "https" else "ws"
    netloc = u.netloc
    path = "/api/remote/agent/ws"
    return urlunparse((scheme, netloc, path, "", "", ""))


async def _agent_loop(base_url: str, pair_code: str, agent_name: str, mode: str) -> None:
    try:
        import websockets  # type: ignore
    except ImportError:
        # fallback: use httpx isn't enough for WS; starlette test client no
        # use fastapi-compatible: aiohttp if present, else fail clearly
        try:
            import aiohttp
        except ImportError as e:
            _set_state(last_error="缺少 websockets/aiohttp 依赖，无法拨出连接", running=False)
            raise RuntimeError("需要 websockets 或 aiohttp") from e
        await _agent_loop_aiohttp(base_url, pair_code, agent_name, mode)
        return

    url = _ws_url(base_url)
    _set_state(controller_url=base_url, last_error="", running=True)
    backoff = 2
    while not _agent_stop.is_set():
        try:
            async with websockets.connect(url, ping_interval=20, ping_timeout=20, max_size=8 * 1024 * 1024) as ws:
                st = get_remote_settings()
                token = st.get("_session_token") or ""
                sid = st.get("active_session_id") or ""
                if token and sid and not str(pair_code or "").strip():
                    hello = {
                        "type": "resume",
                        "session_id": sid,
                        "session_token": token,
                        "agent_name": agent_name,
                    }
                else:
                    hello = {
                        "type": "hello",
                        "pair_code": pair_code,
                        "agent_name": agent_name,
                        "mode": mode,
                        "version": "0.7.0",
                    }
                await ws.send(json.dumps(hello, ensure_ascii=False))
                raw = await asyncio.wait_for(ws.recv(), timeout=30)
                msg = json.loads(raw)
                if msg.get("type") == "error":
                    raise RuntimeError(msg.get("detail") or "主控拒绝连接")
                if msg.get("type") not in ("welcome", "resumed"):
                    raise RuntimeError(msg.get("detail") or "配对失败")
                session_id = msg.get("session_id") or ""
                session_token = msg.get("session_token") or token
                controller_name = msg.get("controller_name") or "主控"
                mode_n = msg.get("mode") or mode
                st = get_remote_settings()
                st["status"] = "managed_lock" if mode_n == "managed" else "connected"
                st["active_session_id"] = session_id
                st["active_peer_name"] = controller_name
                st["controller_base_url"] = base_url
                st["_session_token"] = session_token
                set_remote_settings(st)
                _set_state(connected=True, session_id=session_id, last_error="", running=True)
                backoff = 2
                pair_code = ""  # subsequent reconnects use resume

                while not _agent_stop.is_set():
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=35)
                    except asyncio.TimeoutError:
                        await ws.send(json.dumps({"type": "ping", "t": time.time()}))
                        continue
                    data = json.loads(raw)
                    typ = data.get("type")
                    if typ == "ping":
                        await ws.send(json.dumps({"type": "pong", "t": time.time()}))
                        continue
                    if typ == "bye":
                        break
                    if typ == "mode_change":
                        apply_agent_mode_change(data.get("mode") or "collab")
                        _set_state(mode=data.get("mode") or "collab")
                        continue
                    if typ == "rpc":
                        result = execute_local_rpc(
                            data.get("method") or "GET",
                            data.get("path") or "",
                            query=data.get("query") or {},
                            body=data.get("body"),
                            actor=f"remote:{controller_name}",
                        )
                        await ws.send(
                            json.dumps(
                                {
                                    "type": "rpc_result",
                                    "id": data.get("id"),
                                    "ok": bool(result.get("ok", True)),
                                    "status": result.get("status") or 200,
                                    "body": result,
                                },
                                ensure_ascii=False,
                            )
                        )
        except Exception as e:
            _set_state(connected=False, last_error=str(e))
            st = get_remote_settings()
            if st.get("status") in ("connected", "managed_lock"):
                # keep status but show offline via runtime
                pass
            if _agent_stop.is_set():
                break
            await asyncio.sleep(backoff)
            backoff = min(30, backoff * 2)
    _set_state(running=False, connected=False)


async def _agent_loop_aiohttp(base_url: str, pair_code: str, agent_name: str, mode: str) -> None:
    import aiohttp

    url = _ws_url(base_url)
    _set_state(controller_url=base_url, last_error="", running=True)
    backoff = 2
    while not _agent_stop.is_set():
        try:
            async with aiohttp.ClientSession() as session:
                async with session.ws_connect(url, heartbeat=20, max_msg_size=8 * 1024 * 1024) as ws:
                    st = get_remote_settings()
                    token = st.get("_session_token") or ""
                    sid = st.get("active_session_id") or ""
                    if token and sid and not pair_code:
                        hello = {
                            "type": "resume",
                            "session_id": sid,
                            "session_token": token,
                            "agent_name": agent_name,
                        }
                    else:
                        hello = {
                            "type": "hello",
                            "pair_code": pair_code,
                            "agent_name": agent_name,
                            "mode": mode,
                            "version": "0.7.0",
                        }
                    await ws.send_str(json.dumps(hello, ensure_ascii=False))
                    msg = await ws.receive_json(timeout=30)
                    if msg.get("type") == "error":
                        raise RuntimeError(msg.get("detail") or "主控拒绝连接")
                    if msg.get("type") not in ("welcome", "resumed"):
                        raise RuntimeError(msg.get("detail") or "配对失败")
                    session_id = msg.get("session_id") or ""
                    session_token = msg.get("session_token") or token
                    controller_name = msg.get("controller_name") or "主控"
                    mode_n = msg.get("mode") or mode
                    st = get_remote_settings()
                    st["status"] = "managed_lock" if mode_n == "managed" else "connected"
                    st["active_session_id"] = session_id
                    st["active_peer_name"] = controller_name
                    st["controller_base_url"] = base_url
                    st["_session_token"] = session_token
                    set_remote_settings(st)
                    _set_state(connected=True, session_id=session_id, last_error="", running=True)
                    backoff = 2
                    pair_code = ""

                    while not _agent_stop.is_set():
                        try:
                            raw_msg = await asyncio.wait_for(ws.receive(), timeout=35)
                        except asyncio.TimeoutError:
                            await ws.send_str(json.dumps({"type": "ping", "t": time.time()}))
                            continue
                        if raw_msg.type == aiohttp.WSMsgType.TEXT:
                            data = json.loads(raw_msg.data)
                        elif raw_msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                            break
                        else:
                            continue
                        typ = data.get("type")
                        if typ == "ping":
                            await ws.send_str(json.dumps({"type": "pong", "t": time.time()}))
                            continue
                        if typ == "bye":
                            break
                        if typ == "mode_change":
                            apply_agent_mode_change(data.get("mode") or "collab")
                            _set_state(mode=data.get("mode") or "collab")
                            continue
                        if typ == "rpc":
                            result = execute_local_rpc(
                                data.get("method") or "GET",
                                data.get("path") or "",
                                query=data.get("query") or {},
                                body=data.get("body"),
                                actor=f"remote:{controller_name}",
                            )
                            await ws.send_str(
                                json.dumps(
                                    {
                                        "type": "rpc_result",
                                        "id": data.get("id"),
                                        "ok": bool(result.get("ok", True)),
                                        "status": result.get("status") or 200,
                                        "body": result,
                                    },
                                    ensure_ascii=False,
                                )
                            )
        except Exception as e:
            _set_state(connected=False, last_error=str(e))
            if _agent_stop.is_set():
                break
            await asyncio.sleep(backoff)
            backoff = min(30, backoff * 2)
    _set_state(running=False, connected=False)


def start_agent_dial(*, base_url: str, pair_code: str, agent_name: str, mode: str = "collab") -> dict[str, Any]:
    global _agent_thread
    stop_agent_dial()
    _agent_stop.clear()

    def runner() -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(_agent_loop(base_url, pair_code, agent_name, mode))
        finally:
            try:
                loop.close()
            except Exception:
                pass

    t = threading.Thread(target=runner, name="dockerops-remote-agent", daemon=True)
    with _agent_lock:
        _agent_thread = t
    t.start()
    return {"ok": True, "message": "正在连接主控…", "runtime": agent_runtime_state()}


def stop_agent_dial() -> None:
    global _agent_thread
    _agent_stop.set()
    t = None
    with _agent_lock:
        t = _agent_thread
        _agent_thread = None
    if t and t.is_alive():
        t.join(timeout=2)
    _set_state(running=False, connected=False)

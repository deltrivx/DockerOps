"""
Controller-side dial-out client (v0.8).

主控粘贴被控生成的 dop1 凭证后，主动连接被控 /api/remote/hub，
持有多路 outbound WebSocket，经此下发 RPC。
"""
from __future__ import annotations

import asyncio
import json
import threading
import time
import uuid
from typing import Any, Callable, Awaitable

from remote import (
    RPC_TIMEOUT_SEC,
    get_remote_settings,
    get_session,
    hub_ws_url,
    list_controller_outbound_sessions,
    parse_pair_code,
    pop_rpc_waiter,
    register_controller_session,
    register_rpc_waiter,
    resolve_rpc_response,
    set_remote_settings,
    set_session_mode,
    touch_session,
)

_lock = threading.Lock()
_loop: asyncio.AbstractEventLoop | None = None
_thread: threading.Thread | None = None
_stop = threading.Event()

_dials: dict[str, dict[str, Any]] = {}
_tasks: dict[str, asyncio.Task] = {}
_runtime: dict[str, Any] = {
    "running": False,
    "connecting": False,
    "last_error": "",
    "last_pair_agent": "",
    "sessions": {},
}

RecvFn = Callable[[Any, float], Awaitable[dict[str, Any]]]


def dial_runtime_state() -> dict[str, Any]:
    with _lock:
        sessions = {
            sid: {
                "online": bool(info.get("online")),
                "agent_url": info.get("agent_url") or "",
                "agent_name": info.get("agent_name") or "",
                "mode": info.get("mode") or "collab",
            }
            for sid, info in _dials.items()
        }
        out = dict(_runtime)
        out["sessions"] = sessions
        out["online_count"] = sum(1 for s in sessions.values() if s.get("online"))
        return out


def dial_online_map() -> dict[str, bool]:
    with _lock:
        return {sid: bool(info.get("online")) for sid, info in _dials.items()}


def is_session_dial_online(session_id: str) -> bool:
    with _lock:
        info = _dials.get(session_id)
        return bool(info and info.get("online") and info.get("ws") is not None)


def _set_runtime(**kwargs: Any) -> None:
    with _lock:
        _runtime.update(kwargs)


def _ensure_loop() -> asyncio.AbstractEventLoop:
    global _loop, _thread
    with _lock:
        if _loop and _thread and _thread.is_alive():
            return _loop
        _stop.clear()
        ready = threading.Event()
        holder: dict[str, Any] = {}

        def runner() -> None:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            holder["loop"] = loop
            ready.set()
            try:
                loop.run_forever()
            finally:
                try:
                    loop.close()
                except Exception:
                    pass

        t = threading.Thread(target=runner, name="dockerops-remote-controller", daemon=True)
        _thread = t
        t.start()
        ready.wait(timeout=5)
        _loop = holder.get("loop")
        if _loop is None:
            raise RuntimeError("无法启动主控拨出线程")
        _set_runtime(running=True)
        return _loop


def _run_coro(coro, timeout: float = 60.0):
    loop = _ensure_loop()
    fut = asyncio.run_coroutine_threadsafe(coro, loop)
    return fut.result(timeout=timeout)


async def _ws_send(ws: Any, payload: dict[str, Any]) -> None:
    data = json.dumps(payload, ensure_ascii=False)
    if hasattr(ws, "send_str"):
        await ws.send_str(data)
    else:
        await ws.send(data)


async def _ws_recv_websockets(ws: Any, timeout: float = 35.0) -> dict[str, Any]:
    raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="replace")
    return json.loads(raw)


async def _ws_recv_aiohttp(ws: Any, timeout: float = 35.0) -> dict[str, Any]:
    import aiohttp

    msg = await asyncio.wait_for(ws.receive(), timeout=timeout)
    if msg.type == aiohttp.WSMsgType.TEXT:
        return json.loads(msg.data)
    if msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
        raise ConnectionError("WebSocket 已关闭")
    raise TimeoutError("无文本帧")


def _mark_offline(sid: str) -> None:
    if not sid:
        return
    with _lock:
        if sid in _dials:
            _dials[sid]["online"] = False
            _dials[sid]["ws"] = None
    try:
        touch_session(sid, online=False)
    except Exception:
        pass


async def _handshake_and_loop(
    ws: Any,
    recv: RecvFn,
    *,
    agent_base_url: str,
    pair_code: str,
    session_id: str,
    session_token: str,
    controller_name: str,
    expected_mode: str,
    agent_name: str,
) -> tuple[str, str]:
    if pair_code:
        hello = {
            "type": "hello",
            "pair_code": pair_code,
            "controller_name": controller_name,
            "version": "0.8.0",
        }
    else:
        if not session_id or not session_token:
            raise RuntimeError("缺少会话，请重新粘贴凭证连接")
        hello = {
            "type": "resume",
            "session_id": session_id,
            "session_token": session_token,
            "controller_name": controller_name,
        }

    await _ws_send(ws, hello)
    msg = await recv(ws, 30)
    if msg.get("type") == "error":
        raise RuntimeError(msg.get("detail") or "被控拒绝连接")
    if msg.get("type") not in ("welcome", "resumed"):
        raise RuntimeError(msg.get("detail") or "配对失败")

    sid = msg.get("session_id") or session_id
    token = msg.get("session_token") or session_token
    mode_n = msg.get("mode") or expected_mode or "collab"
    peer = msg.get("agent_name") or agent_name or "被控"

    register_controller_session(
        session_id=sid,
        session_token=token,
        agent_name=peer,
        agent_base_url=agent_base_url,
        mode=mode_n,
        expires_at=msg.get("expires_at"),
    )
    with _lock:
        _dials[sid] = {
            "ws": ws,
            "online": True,
            "agent_url": agent_base_url,
            "agent_name": peer,
            "mode": mode_n,
            "session_token": token,
            "loop": asyncio.get_running_loop(),
        }
    touch_session(sid, online=True)
    _set_runtime(connecting=False, last_error="", last_pair_agent=peer)
    st = get_remote_settings()
    st["status"] = "connected"
    st["active_session_id"] = sid
    st["active_peer_name"] = peer
    set_remote_settings(st)

    while not _stop.is_set():
        try:
            data = await recv(ws, 35)
        except asyncio.TimeoutError:
            await _ws_send(ws, {"type": "ping", "t": time.time()})
            continue
        typ = data.get("type")
        if typ == "ping":
            await _ws_send(ws, {"type": "pong", "t": time.time()})
            continue
        if typ == "pong":
            touch_session(sid, online=True)
            continue
        if typ == "bye":
            break
        if typ == "mode_change":
            mode_n = data.get("mode") or "collab"
            with _lock:
                if sid in _dials:
                    _dials[sid]["mode"] = mode_n
            try:
                set_session_mode(sid, mode_n)
            except Exception:
                pass
            continue
        if typ == "rpc_result":
            resolve_rpc_response(data)
            continue

    _mark_offline(sid)
    return sid, token


async def _maintain_forever(
    *,
    agent_base_url: str,
    pair_code: str = "",
    session_id: str = "",
    session_token: str = "",
    controller_name: str = "主控",
    expected_mode: str = "collab",
    agent_name: str = "被控",
) -> None:
    try:
        import websockets  # type: ignore
    except ImportError:
        websockets = None  # type: ignore

    url = hub_ws_url(agent_base_url)
    backoff = 2
    local_sid = session_id
    local_token = session_token
    first_pair = str(pair_code or "").strip()

    while not _stop.is_set():
        try:
            if websockets is not None:
                async with websockets.connect(
                    url, ping_interval=20, ping_timeout=20, max_size=8 * 1024 * 1024
                ) as ws:
                    local_sid, local_token = await _handshake_and_loop(
                        ws,
                        _ws_recv_websockets,
                        agent_base_url=agent_base_url,
                        pair_code=first_pair,
                        session_id=local_sid,
                        session_token=local_token,
                        controller_name=controller_name,
                        expected_mode=expected_mode,
                        agent_name=agent_name,
                    )
                    first_pair = ""
                    backoff = 2
            else:
                import aiohttp

                async with aiohttp.ClientSession() as session:
                    async with session.ws_connect(
                        url, heartbeat=20, max_msg_size=8 * 1024 * 1024
                    ) as ws:
                        local_sid, local_token = await _handshake_and_loop(
                            ws,
                            _ws_recv_aiohttp,
                            agent_base_url=agent_base_url,
                            pair_code=first_pair,
                            session_id=local_sid,
                            session_token=local_token,
                            controller_name=controller_name,
                            expected_mode=expected_mode,
                            agent_name=agent_name,
                        )
                        first_pair = ""
                        backoff = 2
        except Exception as e:
            _set_runtime(last_error=str(e), connecting=False)
            _mark_offline(local_sid)
            if first_pair:
                # one-time pair failed — stop spinning
                return
            if _stop.is_set():
                return
            await asyncio.sleep(backoff)
            backoff = min(30, backoff * 2)


def start_controller_connect(
    *,
    pair_code: str,
    controller_name: str = "主控",
) -> dict[str, Any]:
    parsed = parse_pair_code(pair_code)
    agent_url = parsed["base_url"]
    agent_name = parsed.get("agent_name") or "被控"
    mode = parsed.get("mode") or "collab"
    _set_runtime(connecting=True, last_error="", last_pair_agent=agent_name)
    st = get_remote_settings()
    st["status"] = "waiting_pair"
    name = (controller_name or st.get("display_name") or "主控").strip() or "主控"
    set_remote_settings(st)

    loop = _ensure_loop()

    async def boot() -> str:
        # cancel existing task for same URL
        old = _tasks.pop(agent_url, None)
        if old and not old.done():
            old.cancel()
        task = asyncio.create_task(
            _maintain_forever(
                agent_base_url=agent_url,
                pair_code=pair_code,
                controller_name=name,
                expected_mode=mode,
                agent_name=agent_name,
            )
        )
        _tasks[agent_url] = task
        deadline = time.time() + 25
        while time.time() < deadline:
            with _lock:
                for sid, info in _dials.items():
                    if info.get("agent_url") == agent_url and info.get("online"):
                        return sid
            if task.done():
                exc = task.exception()
                if exc:
                    raise exc
                err = dial_runtime_state().get("last_error") or "连接失败"
                raise RuntimeError(err)
            await asyncio.sleep(0.2)
        err = dial_runtime_state().get("last_error") or ""
        if err:
            raise RuntimeError(err)
        raise RuntimeError(
            "连接被控超时：请确认被控地址可达、凭证未过期，且被控正在等待连接"
        )

    try:
        sid = _run_coro(boot(), timeout=30)
    except Exception as e:
        _set_runtime(connecting=False, last_error=str(e))
        raise

    return {
        "ok": True,
        "session_id": sid,
        "agent_name": agent_name,
        "agent_base_url": agent_url,
        "mode": mode,
        "message": f"已连接被控「{agent_name}」",
        "runtime": dial_runtime_state(),
    }


def resume_controller_sessions() -> None:
    st = get_remote_settings()
    if not st.get("enabled") or st.get("role") != "controller":
        return
    sessions = list_controller_outbound_sessions()
    if not sessions:
        return
    loop = _ensure_loop()
    name = st.get("display_name") or "主控"
    for s in sessions:
        sid = s["session_id"]
        full = get_session(sid)
        if not full:
            continue
        meta = full.get("meta_obj") or {}
        if not meta:
            try:
                meta = json.loads(full.get("meta") or "{}")
            except Exception:
                meta = {}
        token = meta.get("session_token") or ""
        base = full.get("base_url") or meta.get("agent_base_url") or ""
        if not base or not token:
            continue

        async def _spawn(
            _base: str = base,
            _sid: str = sid,
            _token: str = token,
            _mode: str = s.get("mode") or "collab",
            _aname: str = s.get("peer_name") or "被控",
        ) -> None:
            old = _tasks.pop(_base, None)
            if old and not old.done():
                old.cancel()
            t = asyncio.create_task(
                _maintain_forever(
                    agent_base_url=_base,
                    session_id=_sid,
                    session_token=_token,
                    controller_name=name,
                    expected_mode=_mode,
                    agent_name=_aname,
                )
            )
            _tasks[_base] = t

        asyncio.run_coroutine_threadsafe(_spawn(), loop)


def stop_controller_dial(session_id: str | None = None) -> None:
    with _lock:
        if session_id:
            targets = [session_id]
        else:
            targets = list(_dials.keys())
        bases: list[str] = []
        for sid in targets:
            info = _dials.pop(sid, None)
            if not info:
                continue
            bases.append(info.get("agent_url") or "")
            ws = info.get("ws")
            loop = info.get("loop") or _loop
            if ws is not None and loop is not None:
                try:
                    asyncio.run_coroutine_threadsafe(
                        _ws_send(ws, {"type": "bye"}), loop
                    )
                except Exception:
                    pass
            try:
                touch_session(sid, online=False)
            except Exception:
                pass
    # cancel maintain tasks
    loop = _loop
    if loop:
        for base, task in list(_tasks.items()):
            if session_id is None or base in bases:
                if not task.done():
                    loop.call_soon_threadsafe(task.cancel)
                _tasks.pop(base, None)
    if session_id is None:
        _set_runtime(connecting=False)


def stop_all_controller_dials() -> None:
    stop_controller_dial(None)


async def rpc_via_dial(
    session_id: str,
    *,
    method: str,
    path: str,
    body: Any = None,
    query: dict[str, Any] | None = None,
    timeout: float = RPC_TIMEOUT_SEC,
) -> dict[str, Any]:
    with _lock:
        info = _dials.get(session_id)
        ws = info.get("ws") if info else None
        online = bool(info and info.get("online") and ws is not None)
    if not online or ws is None:
        raise RuntimeError("远程节点不在线（主控未连上被控或已断开）")

    req_id = uuid.uuid4().hex
    loop = asyncio.get_running_loop()
    fut: asyncio.Future = loop.create_future()
    register_rpc_waiter(req_id, fut)
    payload = {
        "type": "rpc",
        "id": req_id,
        "method": (method or "GET").upper(),
        "path": path,
        "query": query or {},
        "body": body,
    }
    try:
        await _ws_send(ws, payload)
        result = await asyncio.wait_for(fut, timeout=timeout)
        return result
    except asyncio.TimeoutError as e:
        raise RuntimeError("远程节点响应超时") from e
    finally:
        pop_rpc_waiter(req_id)

"""
Compatibility shim (v0.8).

0.7 used agent dial-out to controller. 0.8 reverses: controller dials agent hub.
Agent-side runtime is hub status + settings; dial client lives in remote_controller.
"""
from __future__ import annotations

from typing import Any

from remote import get_remote_settings, public_remote_status


def agent_runtime_state() -> dict[str, Any]:
    """Agent-facing runtime snapshot for UI (no outbound dial)."""
    st = get_remote_settings()
    status = public_remote_status()
    connected = st.get("status") in ("connected", "managed_lock") and bool(
        st.get("active_session_id")
    )
    # refine with hub online
    online = False
    try:
        from remote import is_agent_online

        sid = st.get("active_session_id") or ""
        online = bool(sid and is_agent_online(sid))
    except Exception:
        online = connected
    return {
        "running": st.get("role") == "agent" and st.get("enabled"),
        "connected": online,
        "last_error": "",
        "controller_url": "",
        "session_id": st.get("active_session_id") or "",
        "ui_phase": st.get("ui_phase") or "setup",
        "peer_name": st.get("active_peer_name") or "",
        "mode": st.get("agent_mode") or "collab",
        "hint": status.get("hint") or "",
    }


def start_agent_dial(**kwargs: Any) -> dict[str, Any]:
    """Removed in 0.8 — agent no longer dials out."""
    raise RuntimeError(
        "v0.8 起由被控生成凭证、主控主动连接。请升级双方到 0.8，并在被控生成连接凭证后粘贴到主控。"
    )


def stop_agent_dial() -> None:
    """No-op for 0.8 agent (no outbound dial thread)."""
    return

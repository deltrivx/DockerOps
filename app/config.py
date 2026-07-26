from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def _as_bool(v: object) -> bool:
    if isinstance(v, bool):
        return v
    if v is None:
        return False
    return str(v).strip().lower() in {"1", "true", "yes", "on"}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="DOCKEROPS_", extra="ignore")

    host: str = "0.0.0.0"
    port: int = 8080
    data_dir: str = "/data"
    # Optional one-shot bootstrap ONLY when both are explicitly set in process env.
    # Empty defaults: no built-in account; first admin is created via Web 向导 → SQLite.
    admin_user: str = ""
    admin_password: str = ""
    api_token: str = ""
    docker_host: str = "unix:///var/run/docker.sock"
    secret_key: str = ""
    session_ttl_hours: int = 24

    # Optional full takeover (compose up/down, unraid recreate, adopt, prune/remove)
    takeover_enabled: bool = False
    compose_enabled: bool = True
    compose_bin: str = "docker"
    compose_project_dirs: str = ""
    unraid_enabled: bool = True
    unraid_templates_user: str = "/unraid/templates-user"
    unraid_docker_cfg: str = "/unraid/docker.cfg"

    # Host platform: auto | unraid | fnos | generic
    platform: str = "auto"
    # Portainer-like daily resource APIs (lifecycle/logs/images/nets/vols/system)
    resource_apis: bool = True
    # Web exec console (high risk). Unraid template defaults true; generic still off unless set.
    console_enabled: bool = False

    # Unraid-style background update detect (registry digest cache)
    update_auto_check: bool = True
    update_check_interval_hours: int = 6
    update_check_startup_delay_sec: int = 60

    # Optional container-process proxy (also accepts bare HTTP_PROXY etc.)
    # Does NOT reconfigure Docker Engine host proxy for pull/inspect_distribution.
    http_proxy: str = ""
    https_proxy: str = ""
    no_proxy: str = ""

    @field_validator(
        "takeover_enabled",
        "compose_enabled",
        "unraid_enabled",
        "resource_apis",
        "console_enabled",
        "update_auto_check",
        mode="before",
    )
    @classmethod
    def _bool_fields(cls, v: object) -> bool:
        return _as_bool(v)

    @field_validator("update_check_interval_hours", mode="before")
    @classmethod
    def _interval_hours(cls, v: object) -> int:
        try:
            n = int(v)  # type: ignore[arg-type]
        except Exception:
            n = 6
        return max(1, min(48, n))

    def ensure_dirs(self) -> Path:
        root = Path(self.data_dir)
        root.mkdir(parents=True, exist_ok=True)
        (root / "backups").mkdir(parents=True, exist_ok=True)
        (root / "backups" / "compose").mkdir(parents=True, exist_ok=True)
        (root / "backups" / "unraid").mkdir(parents=True, exist_ok=True)
        (root / "reports").mkdir(parents=True, exist_ok=True)
        return root

    @property
    def resolved_secret(self) -> str:
        if self.secret_key:
            return self.secret_key
        # Do not derive from empty/default credentials; stable install-local fallback.
        return f"dockerops-data-{self.data_dir}-session-secret"

    def compose_dirs(self) -> list[Path]:
        if not self.compose_project_dirs.strip():
            return []
        out: list[Path] = []
        for part in self.compose_project_dirs.split(":"):
            p = part.strip()
            if p:
                out.append(Path(p))
        return out

    def unraid_templates_path(self) -> Path:
        return Path(self.unraid_templates_user)

    def unraid_templates_available(self) -> bool:
        p = self.unraid_templates_path()
        return p.is_dir()

    def takeover_guard(self) -> None:
        """Raise PermissionError if destructive takeover is disabled."""
        if not self.takeover_enabled:
            raise PermissionError(
                "完整接管未开启。设置 DOCKEROPS_TAKEOVER_ENABLED=true 并挂载 rw docker.sock / 模板目录后重试。"
            )


def _env_first(*keys: str) -> str:
    for k in keys:
        v = os.environ.get(k)
        if v is not None and str(v).strip():
            return str(v).strip()
    return ""


_PROXY_ENV_KEYS = (
    "HTTP_PROXY",
    "http_proxy",
    "HTTPS_PROXY",
    "https_proxy",
    "NO_PROXY",
    "no_proxy",
    "DOCKEROPS_HTTP_PROXY",
    "DOCKEROPS_HTTPS_PROXY",
    "DOCKEROPS_NO_PROXY",
)

# Snapshot: True only if container/process env had proxy BEFORE we apply SQLite values.
# Applying stored proxy must NOT flip this, or UI locks and redeploy looks like "reset".
_proxy_env_locked_at_boot: bool | None = None


def read_process_proxy() -> dict[str, str]:
    """
    Resolve proxy from process env (standard names first, then DOCKEROPS_*).
    """
    return {
        "http": _env_first("HTTP_PROXY", "http_proxy", "DOCKEROPS_HTTP_PROXY"),
        "https": _env_first("HTTPS_PROXY", "https_proxy", "DOCKEROPS_HTTPS_PROXY"),
        "no_proxy": _env_first("NO_PROXY", "no_proxy", "DOCKEROPS_NO_PROXY"),
    }


def _any_proxy_env_set() -> bool:
    return any(bool(os.environ.get(k, "").strip()) for k in _PROXY_ENV_KEYS)


def capture_proxy_env_lock_at_boot() -> bool:
    """
    Call once after Settings bootstrap (DOCKEROPS_* → env) and BEFORE applying SQLite proxy.
    """
    global _proxy_env_locked_at_boot
    if _proxy_env_locked_at_boot is None:
        _proxy_env_locked_at_boot = _any_proxy_env_set()
    return _proxy_env_locked_at_boot


def env_proxy_locked() -> bool:
    """
    True if proxy was configured via container env at process start.
    Web-saved SQLite proxy must remain editable across restarts.
    """
    global _proxy_env_locked_at_boot
    if _proxy_env_locked_at_boot is None:
        # Fallback if capture not called yet (tests): do not treat applied runtime env as lock
        return False
    return bool(_proxy_env_locked_at_boot)


def apply_proxy_to_environ(http: str = "", https: str = "", no_proxy: str = "") -> None:
    """Write proxy into process env for child processes / future HTTP clients."""
    mapping = {
        "HTTP_PROXY": (http or "").strip(),
        "HTTPS_PROXY": (https or "").strip(),
        "NO_PROXY": (no_proxy or "").strip(),
    }
    for k, v in mapping.items():
        lk = k.lower()
        if v:
            os.environ[k] = v
            os.environ[lk] = v
        else:
            os.environ.pop(k, None)
            os.environ.pop(lk, None)


def bootstrap_proxy_from_settings(s: Settings) -> None:
    """
    At startup: map DOCKEROPS_* proxy fields into standard env if standard empty.
    Does not override already-set HTTP_PROXY/HTTPS_PROXY/NO_PROXY.
    """
    if s.http_proxy and not _env_first("HTTP_PROXY", "http_proxy"):
        os.environ["HTTP_PROXY"] = s.http_proxy
        os.environ["http_proxy"] = s.http_proxy
    if s.https_proxy and not _env_first("HTTPS_PROXY", "https_proxy"):
        os.environ["HTTPS_PROXY"] = s.https_proxy
        os.environ["https_proxy"] = s.https_proxy
    if s.no_proxy and not _env_first("NO_PROXY", "no_proxy"):
        os.environ["NO_PROXY"] = s.no_proxy
        os.environ["no_proxy"] = s.no_proxy


@lru_cache
def get_settings() -> Settings:
    _ = os.environ.get("TZ", "Asia/Shanghai")
    s = Settings()
    if os.environ.get("DOCKER_HOST") and not os.environ.get("DOCKEROPS_DOCKER_HOST"):
        s.docker_host = os.environ["DOCKER_HOST"]
    bootstrap_proxy_from_settings(s)
    # Capture lock after Settings/env bootstrap, before any SQLite apply.
    capture_proxy_env_lock_at_boot()
    s.ensure_dirs()
    return s

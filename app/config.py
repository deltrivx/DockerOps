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
    admin_user: str = "admin"
    admin_password: str = "dockerops"
    api_token: str = ""
    docker_host: str = "unix:///var/run/docker.sock"
    secret_key: str = ""
    session_ttl_hours: int = 24

    # Optional full takeover (compose up/down, unraid recreate, adopt)
    takeover_enabled: bool = False
    compose_enabled: bool = True
    compose_bin: str = "docker"
    compose_project_dirs: str = ""
    unraid_enabled: bool = True
    unraid_templates_user: str = "/unraid/templates-user"
    unraid_docker_cfg: str = "/unraid/docker.cfg"

    @field_validator(
        "takeover_enabled",
        "compose_enabled",
        "unraid_enabled",
        mode="before",
    )
    @classmethod
    def _bool_fields(cls, v: object) -> bool:
        return _as_bool(v)

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
        return f"dockerops-{self.admin_user}-{self.admin_password}-secret"

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


@lru_cache
def get_settings() -> Settings:
    _ = os.environ.get("TZ", "Asia/Shanghai")
    s = Settings()
    if os.environ.get("DOCKER_HOST") and not os.environ.get("DOCKEROPS_DOCKER_HOST"):
        s.docker_host = os.environ["DOCKER_HOST"]
    s.ensure_dirs()
    return s

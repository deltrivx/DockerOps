from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


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

    def ensure_dirs(self) -> Path:
        root = Path(self.data_dir)
        root.mkdir(parents=True, exist_ok=True)
        (root / "backups").mkdir(parents=True, exist_ok=True)
        (root / "reports").mkdir(parents=True, exist_ok=True)
        return root

    @property
    def resolved_secret(self) -> str:
        if self.secret_key:
            return self.secret_key
        # Stable-ish secret derived from admin password for single-node installs.
        return f"dockerops-{self.admin_user}-{self.admin_password}-secret"


@lru_cache
def get_settings() -> Settings:
    # Allow plain TZ without prefix
    _ = os.environ.get("TZ", "Asia/Shanghai")
    s = Settings()
    # Also accept DOCKEROPS_DOCKER_HOST already mapped by env_prefix
    if os.environ.get("DOCKER_HOST") and not os.environ.get("DOCKEROPS_DOCKER_HOST"):
        s.docker_host = os.environ["DOCKER_HOST"]
    s.ensure_dirs()
    return s

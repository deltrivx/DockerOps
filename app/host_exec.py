"""Execute commands on a Docker host.

Some host operations cannot go through the Docker socket at all — most
importantly Unraid's container template scripts, which live on the host
filesystem and are the only supported way to rebuild a container without
stripping its template ownership. Those have to run where the host is.

Channels, in the order they are tried:

1. **local** — DockerOps runs on the host itself, so commands run directly.
2. **ssh** — the host is reachable over SSH, using paramiko (pure Python, no
   system ssh client needed in the image).

Why SSH rather than a remote Docker TCP endpoint: publishing an
unauthenticated ``tcp://host:2375`` so a convenience tool can reach the engine
is a security regression the user should not have to accept, and it still would
not reach the host scripts. One authenticated, encrypted channel covers all
three platforms and reaches both the engine and the host.

Why paramiko rather than shelling out to ``ssh``: the runtime image is
``python:3.12-slim`` and does not ship an ssh client. paramiko keeps the image
small and avoids depending on host key files being present.

Credentials belong to the device record; this module never persists them.
"""

from __future__ import annotations

import io
import os
import shlex
import socket
import subprocess
import threading
from pathlib import Path
from typing import Any, Callable

# Set once a channel is known to work, so we stop re-probing on every call.
_channel_cache: dict[str, Any] = {}
_cache_lock = threading.Lock()

# Where the host root appears inside the container, if it was bind-mounted.
HOST_ROOT_CANDIDATES = (
    "/host",
    "/mnt/host",
)

# Host paths that must be rewritten when the host root is mounted.
_REWRITABLE = (
    "/usr/local/emhttp",
    "/boot/config",
)


def _run_subprocess(
    argv: list[str], *, timeout: int = 900, input_text: str | None = None
) -> dict[str, Any]:
    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=timeout,
            input=input_text,
        )
        return {
            "ok": proc.returncode == 0,
            "code": proc.returncode,
            "stdout": proc.stdout or "",
            "stderr": proc.stderr or "",
        }
    except subprocess.TimeoutExpired:
        return {"ok": False, "code": None, "stdout": "", "stderr": "命令超时"}
    except FileNotFoundError as e:
        return {"ok": False, "code": None, "stdout": "", "stderr": f"命令不存在：{e}"}
    except Exception as e:
        return {"ok": False, "code": None, "stdout": "", "stderr": str(e)}


def detect_in_container() -> bool:
    """True when DockerOps itself runs inside a container."""
    if Path("/.dockerenv").exists():
        return True
    try:
        cgroup = Path("/proc/1/cgroup").read_text(encoding="utf-8", errors="ignore")
        return "docker" in cgroup or "containerd" in cgroup
    except Exception:
        return False


def _host_mount_available() -> bool:
    """True only when a host-root mount actually exposes the host filesystem.

    The directory merely existing is not enough: a container that bind-mounts
    ``/mnt/user/appdata`` at ``/host`` has a ``/host`` directory with nothing
    host-specific in it. Probing for a path only the host root would have keeps
    the check honest, so we do not claim a channel that cannot reach host
    scripts and then fail confusingly at rebuild time.
    """
    for root in HOST_ROOT_CANDIDATES:
        base = Path(root)
        if not base.is_dir():
            continue
        for marker in ("/etc/os-release", "/usr/local/emhttp", "/var/lib/docker"):
            if (base / marker.lstrip("/")).exists():
                return True
    return False


def host_script_path(script: str) -> str:
    """Translate a host path to something reachable from here."""
    if Path(script).exists():
        return script
    for root in HOST_ROOT_CANDIDATES:
        candidate = Path(root) / script.lstrip("/")
        if candidate.exists():
            return str(candidate)
    return script


def _translate_host_paths(command: str) -> str:
    for prefix in _REWRITABLE:
        translated = host_script_path(prefix)
        if translated != prefix:
            command = command.replace(prefix, translated)
    return command


def local_executor() -> Callable[..., Any] | None:
    """Executor for this machine, or ``None`` when it cannot be the host.

    Inside a container without the host root mounted, "local" is meaningless:
    the host scripts are simply not there, and pretending otherwise is what
    leads to hand-rolled rebuilds. Returning ``None`` lets callers report the
    limitation honestly.
    """
    if detect_in_container() and not _host_mount_available():
        return None

    def run(command: str, *, timeout: int = 900) -> dict[str, Any]:
        return _run_subprocess(
            ["/bin/sh", "-c", _translate_host_paths(command)], timeout=timeout
        )

    return run


class SshTarget:
    """Connection details for an SSH host.

    Kept as a plain object so it can be built from a device record, from
    environment variables, or from a test, without this module knowing about the
    database.
    """

    def __init__(
        self,
        host: str,
        *,
        port: int = 22,
        user: str = "root",
        password: str = "",
        key_path: str = "",
        key_body: str = "",
        timeout: int = 20,
        alias: str = "",
    ) -> None:
        self.host = host
        self.port = int(port or 22)
        self.user = user or "root"
        self.password = password or ""
        self.key_path = key_path or ""
        self.key_body = key_body or ""
        self.timeout = timeout
        self.alias = alias or ""

    def describe(self) -> dict[str, Any]:
        return {
            "host": self.host,
            "port": self.port,
            "user": self.user,
            "auth": "key" if (self.key_path or self.key_body) else ("password" if self.password else "none"),
            "alias": self.alias,
        }


def _load_private_key(target: SshTarget) -> Any:
    import paramiko

    if target.key_body.strip():
        body = target.key_body.strip()
        if not body.endswith("\n"):
            body += "\n"
        for cls in (
            paramiko.Ed25519Key,
            paramiko.ECDSAKey,
            paramiko.RSAKey,
        ):
            try:
                return cls.from_private_key(io.StringIO(body), password=target.password or None)
            except Exception:
                continue
        raise ValueError("私钥格式无法识别（支持 OpenSSH ed25519 / ECDSA / RSA）")
    if target.key_path:
        p = Path(target.key_path).expanduser()
        if not p.is_file():
            raise ValueError(f"私钥文件不存在：{target.key_path}")
        for cls in (
            paramiko.Ed25519Key,
            paramiko.ECDSAKey,
            paramiko.RSAKey,
        ):
            try:
                return cls.from_private_key_file(str(p), password=target.password or None)
            except Exception:
                continue
        raise ValueError(f"私钥无法解析：{target.key_path}")
    return None


def ssh_executor(target: SshTarget) -> Callable[..., Any]:
    """Build an executor that runs commands on ``target`` over SSH.

    A fresh connection per call keeps the implementation simple and avoids
    holding sockets open between requests. Host operations are infrequent
    (rebuilds, pulls), so the connection cost is not the bottleneck.
    """
    import paramiko

    def run(command: str, *, timeout: int = 900) -> dict[str, Any]:
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        try:
            connect_kwargs: dict[str, Any] = {
                "hostname": target.host,
                "port": target.port,
                "username": target.user,
                "timeout": target.timeout,
                "banner_timeout": target.timeout,
                "auth_timeout": target.timeout,
                "allow_agent": True,
                "look_for_keys": not (target.password or target.key_path or target.key_body),
            }
            key = None
            try:
                key = _load_private_key(target)
            except ValueError as e:
                return {"ok": False, "code": None, "stdout": "", "stderr": str(e)}
            if key is not None:
                connect_kwargs["pkey"] = key
            if target.password:
                connect_kwargs["password"] = target.password
            client.connect(**connect_kwargs)
            stdin, stdout, stderr = client.exec_command(command, timeout=timeout)
            out = stdout.read().decode("utf-8", errors="replace")
            err = stderr.read().decode("utf-8", errors="replace")
            code = stdout.channel.recv_exit_status()
            return {"ok": code == 0, "code": code, "stdout": out, "stderr": err}
        except paramiko.AuthenticationException:
            return {
                "ok": False,
                "code": None,
                "stdout": "",
                "stderr": "SSH 认证失败：用户名、密码或私钥不正确",
            }
        except paramiko.SSHException as e:
            return {"ok": False, "code": None, "stdout": "", "stderr": f"SSH 协议错误：{e}"}
        except socket.timeout:
            return {
                "ok": False,
                "code": None,
                "stdout": "",
                "stderr": f"连接 {target.host}:{target.port} 超时",
            }
        except OSError as e:
            return {
                "ok": False,
                "code": None,
                "stdout": "",
                "stderr": f"无法连接 {target.host}:{target.port}：{e}",
            }
        except Exception as e:
            return {"ok": False, "code": None, "stdout": "", "stderr": str(e)}
        finally:
            try:
                client.close()
            except Exception:
                pass

    return run


def env_ssh_target() -> SshTarget | None:
    """Read an SSH target from the process environment.

    Lets a container reach its own host without the user typing credentials
    into the UI, for the common case where DockerOps runs on the box it manages.
    """
    host = os.environ.get("DOCKEROPS_SSH_HOST", "").strip()
    if not host:
        return None
    return SshTarget(
        host,
        port=int(os.environ.get("DOCKEROPS_SSH_PORT", "22") or 22),
        user=os.environ.get("DOCKEROPS_SSH_USER", "root").strip() or "root",
        password=os.environ.get("DOCKEROPS_SSH_PASSWORD", ""),
        key_path=os.environ.get("DOCKEROPS_SSH_KEY_PATH", "").strip(),
        key_body=os.environ.get("DOCKEROPS_SSH_KEY", ""),
    )


def get_host_executor() -> Callable[..., Any] | None:
    """Executor for the host that owns the mounted Docker socket.

    Order: local run (if this process is on the host) → environment-configured
    SSH. Remote devices supply their own executor from the device record, which
    callers pass in explicitly.
    """
    with _cache_lock:
        if "resolved" in _channel_cache:
            return _channel_cache["resolved"]

        local = local_executor()
        if local is not None:
            probe = local("true", timeout=10)
            if probe.get("ok"):
                _channel_cache["resolved"] = local
                return local

        target = env_ssh_target()
        if target is not None:
            ex = ssh_executor(target)
            probe = ex("true", timeout=20)
            if probe.get("ok"):
                _channel_cache["resolved"] = ex
                return ex

        _channel_cache["resolved"] = None
        return None


def reset_host_executor_cache() -> None:
    """Drop the cached channel; call after connection settings change."""
    with _cache_lock:
        _channel_cache.clear()


def probe_host(executor: Callable[..., Any], *, timeout: int = 60) -> dict[str, Any]:
    """Report platform and capabilities of the target host.

    Run right after connecting so the UI can show what the target actually
    supports, instead of the user discovering it by clicking and failing.
    """
    script = (
        "echo HOSTNAME=$(hostname); "
        "docker --version 2>/dev/null || echo 'DOCKER=missing'; "
        "if [ -f /etc/os-release ]; then . /etc/os-release; echo OS=$PRETTY_NAME; fi; "
        "if [ -d /boot/config/plugins/dockerMan/templates-user ]; then echo UNRAID=yes; fi; "
        "if [ -x /usr/local/emhttp/plugins/dynamix.docker.manager/scripts/update_container ]; "
        "then echo UNRAID_SCRIPTS=yes; fi; "
        "if [ -d /usr/trim ] || [ -d /vol1/@appstore ]; then echo FNOS=yes; fi; "
        "uname -r"
    )
    out = executor(script, timeout=timeout)
    text = ((out.get("stdout") or "") + (out.get("stderr") or "")).strip()
    info: dict[str, str] = {}
    for line in text.splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            info[k.strip()] = v.strip()

    if info.get("UNRAID") == "yes":
        platform = "unraid"
    elif info.get("FNOS") == "yes":
        platform = "fnos"
    else:
        platform = "generic"

    return {
        "ok": bool(out.get("ok")),
        "platform": platform,
        "hostname": info.get("HOSTNAME", ""),
        "os": info.get("OS", ""),
        "kernel": text.splitlines()[-1].strip() if text else "",
        "docker": info.get("DOCKER", ""),
        "unraid_scripts": info.get("UNRAID_SCRIPTS") == "yes",
        "raw": text,
    }


def check_docker_socket(path: str = "/var/run/docker.sock") -> dict[str, Any]:
    """Confirm the Docker socket is present and usable."""
    p = Path(path)
    exists = p.exists()
    readable = os.access(p, os.R_OK) if exists else False
    return {
        "ok": exists and readable,
        "path": path,
        "exists": exists,
        "readable": readable,
    }


def ensure_keypair(
    key_path: str = "/data/.ssh/id_ed25519",
) -> dict[str, Any]:
    """Create an SSH keypair for reaching a host, if one is absent.

    The runtime image has no ``ssh-keygen``, and ``paramiko`` 3.5 cannot
    generate keys, so the pair is built with ``cryptography`` (already a
    paramiko dependency). The private key stays on the DockerOps volume and is
    never returned to the caller; only the public key is, to be installed on the
    target host.
    """
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ed25519

    p = Path(key_path)
    pub_path = p.with_suffix(p.suffix + ".pub")
    if p.is_file() and pub_path.is_file():
        return {
            "ok": True,
            "created": False,
            "key_path": str(p),
            "public_key": pub_path.read_text(encoding="utf-8").strip(),
        }

    p.parent.mkdir(parents=True, exist_ok=True)
    private = ed25519.Ed25519PrivateKey.generate()
    p.write_bytes(
        private.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.OpenSSH,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    os.chmod(p, 0o600)
    public = private.public_key().public_bytes(
        encoding=serialization.Encoding.OpenSSH,
        format=serialization.PublicFormat.OpenSSH,
    ).decode()
    pub_line = f"{public} dockerops"
    pub_path.write_text(pub_line + "\n", encoding="utf-8")
    return {
        "ok": True,
        "created": True,
        "key_path": str(p),
        "public_key": pub_line,
    }


def install_public_key(
    public_key: str,
    *,
    executor: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """Append a public key to the host's ``authorized_keys``.

    Used once when connecting to a host that will be managed over SSH, so the
    user does not have to edit ``authorized_keys`` by hand. Idempotent: the key
    is only appended when its body is not already present.
    """
    ex = executor or get_host_executor()
    if ex is None:
        return {"ok": False, "message": "没有可用的宿主机通道，无法写入授权公钥"}

    body = public_key.split()[1] if len(public_key.split()) >= 2 else public_key
    script = (
        "mkdir -p /root/.ssh && chmod 700 /root/.ssh && "
        "touch /root/.ssh/authorized_keys && chmod 600 /root/.ssh/authorized_keys && "
        f"grep -qF {shlex.quote(body)} /root/.ssh/authorized_keys || "
        f"echo {shlex.quote(public_key)} >> /root/.ssh/authorized_keys"
    )
    return ex(script, timeout=60)



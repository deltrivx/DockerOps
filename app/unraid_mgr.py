from __future__ import annotations

import json
import shutil
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any
from xml.dom import minidom

from config import get_settings
from db import add_ops_record
from docker_client import (
    connect_network,
    get_container,
    list_containers,
    pull_image,
    refresh_template_name_cache,
    remove_container,
    stop_container,
    create_and_start,
)


def templates_root() -> Path:
    return get_settings().unraid_templates_path()


def templates_available() -> bool:
    return get_settings().unraid_enabled and templates_root().is_dir()


def list_templates() -> list[dict[str, Any]]:
    if not templates_available():
        return []
    items: list[dict[str, Any]] = []
    root = templates_root()
    for path in sorted(root.glob("*.xml")):
        try:
            data = parse_template(path)
            data["path"] = str(path)
            data["file"] = path.name
            # match running container
            data["container"] = _find_container(data.get("name") or "")
            items.append(data)
        except Exception as e:
            items.append(
                {
                    "file": path.name,
                    "path": str(path),
                    "error": str(e),
                    "name": path.stem,
                }
            )
    return items


def get_template(name: str) -> dict[str, Any] | None:
    path = find_template_path(name)
    if not path:
        return None
    data = parse_template(path)
    data["path"] = str(path)
    data["file"] = path.name
    data["container"] = _find_container(data.get("name") or name)
    return data


def find_template_path(name: str) -> Path | None:
    if not templates_available():
        return None
    root = templates_root()
    candidates = [
        root / f"my-{name}.xml",
        root / f"my-{name}",
        root / f"{name}.xml",
    ]
    for c in candidates:
        if c.is_file():
            return c
    # scan by <Name>
    for path in root.glob("*.xml"):
        try:
            n = (ET.parse(path).getroot().findtext("Name") or "").strip()
            if n == name or n.lower() == name.lower():
                return path
        except Exception:
            continue
    return None


def parse_template(path: Path) -> dict[str, Any]:
    tree = ET.parse(path)
    root = tree.getroot()
    configs: list[dict[str, Any]] = []
    for cfg in root.findall("Config"):
        configs.append(
            {
                "name": cfg.attrib.get("Name"),
                "target": cfg.attrib.get("Target"),
                "default": cfg.attrib.get("Default"),
                "mode": cfg.attrib.get("Mode"),
                "description": cfg.attrib.get("Description"),
                "type": cfg.attrib.get("Type"),
                "display": cfg.attrib.get("Display"),
                "required": cfg.attrib.get("Required"),
                "mask": cfg.attrib.get("Mask"),
                "value": (cfg.text or "").strip(),
            }
        )
    return {
        "name": (root.findtext("Name") or "").strip(),
        "repository": (root.findtext("Repository") or "").strip(),
        "registry": (root.findtext("Registry") or "").strip(),
        "network": (root.findtext("Network") or "bridge").strip(),
        "extra_networks": (root.findtext("ExtraNetworks") or "").strip(),
        "privileged": (root.findtext("Privileged") or "false").strip().lower() == "true",
        "webui": (root.findtext("WebUI") or "").strip(),
        "icon": (root.findtext("Icon") or "").strip(),
        "extra_params": (root.findtext("ExtraParams") or "").strip(),
        "post_args": (root.findtext("PostArgs") or "").strip(),
        "shell": (root.findtext("Shell") or "").strip(),
        "overview": (root.findtext("Overview") or "").strip(),
        "category": (root.findtext("Category") or "").strip(),
        "memory": (root.findtext("Memory") or "").strip(),
        "cpuset": (root.findtext("CPUset") or "").strip(),
        "configs": configs,
    }


def backup_template(name: str, actor: str | None = None) -> dict[str, Any]:
    tpl = get_template(name)
    if not tpl:
        rec = add_ops_record(
            action="unraid_backup",
            target=name,
            status="failed",
            detail={"error": "template_not_found"},
            actor=actor,
        )
        return {"ok": False, "record": rec, "message": f"未找到 Unraid 模板：{name}"}

    settings = get_settings()
    ts = time.strftime("%Y%m%d-%H%M%S")
    dest = Path(settings.data_dir) / "backups" / "unraid" / f"{name}-{ts}"
    dest.mkdir(parents=True, exist_ok=True)

    src = Path(tpl["path"])
    shutil.copy2(src, dest / src.name)

    container_meta = None
    try:
        container_meta = get_container(name)
    except Exception:
        if tpl.get("container") and tpl["container"].get("id"):
            try:
                container_meta = get_container(tpl["container"]["id"])
            except Exception:
                container_meta = tpl.get("container")

    meta = {
        "created_at": time.time(),
        "actor": actor,
        "template": {k: v for k, v in tpl.items() if k != "container"},
        "container": container_meta,
        "note": "Unraid 模板 + inspect 备份。升级/回滚应走模板重建，避免变成三方容器。",
    }
    (dest / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    rec = add_ops_record(
        action="unraid_backup",
        target=name,
        status="ok",
        detail={"path": str(dest), "template": src.name, "repository": tpl.get("repository")},
        actor=actor,
    )
    return {
        "ok": True,
        "record": rec,
        "backup_path": str(dest),
        "template": tpl,
        "message": f"已备份 Unraid 模板 {name}",
    }


def safe_update_unraid(
    name: str,
    actor: str | None = None,
    *,
    repository: str | None = None,
    recreate: bool = True,
) -> dict[str, Any]:
    """Template-driven safe update (Unraid semantics, not docker run string)."""
    settings = get_settings()
    if not settings.unraid_enabled:
        return {"ok": False, "message": "Unraid 模式未启用"}

    tpl = get_template(name)
    if not tpl:
        rec = add_ops_record(
            action="unraid_update",
            target=name,
            status="failed",
            detail={"error": "template_not_found"},
            actor=actor,
        )
        return {"ok": False, "record": rec, "message": f"未找到模板 {name}"}

    backup = backup_template(name, actor=actor)
    if not backup.get("ok"):
        return {"ok": False, "message": "备份失败，已中止", "backup": backup}

    # optional repository patch on XML
    target_image = repository or tpl.get("repository")
    if repository and repository != tpl.get("repository"):
        try:
            settings.takeover_guard()
            _patch_repository(Path(tpl["path"]), repository)
            tpl = get_template(name) or tpl
            target_image = repository
        except PermissionError as e:
            return {"ok": False, "message": str(e), "backup": backup}

    if not target_image:
        return {"ok": False, "message": "模板缺少 Repository", "backup": backup}

    try:
        pull = pull_image(target_image)
        pull_ok = True
        pull_err = None
    except Exception as e:
        pull = {}
        pull_ok = False
        pull_err = str(e)

    if not pull_ok:
        rec = add_ops_record(
            action="unraid_update",
            target=name,
            status="failed",
            detail={"step": "pull", "error": pull_err, "backup": backup.get("backup_path")},
            actor=actor,
        )
        return {"ok": False, "record": rec, "backup": backup, "message": f"拉镜像失败：{pull_err}"}

    if not recreate:
        rec = add_ops_record(
            action="unraid_update",
            target=name,
            status="ok",
            detail={"step": "pull_only", "pull": pull, "backup": backup.get("backup_path")},
            actor=actor,
        )
        return {
            "ok": True,
            "partial": True,
            "record": rec,
            "backup": backup,
            "pull": pull,
            "message": "已备份并拉取；未重建容器",
        }

    try:
        settings.takeover_guard()
    except PermissionError as e:
        rec = add_ops_record(
            action="unraid_update",
            target=name,
            status="partial",
            detail={
                "step": "pull_only",
                "reason": str(e),
                "pull": pull,
                "backup": backup.get("backup_path"),
                "next_steps": [
                    "镜像已拉取。",
                    "开启接管后可由 DockerOps 按模板重建。",
                    "或在 Unraid Docker 页对应用点击 Apply Update。",
                ],
            },
            actor=actor,
        )
        return {
            "ok": True,
            "partial": True,
            "record": rec,
            "backup": backup,
            "pull": pull,
            "message": "已备份并拉取；接管未开启，未按模板重建（可在 Unraid 原系统 Apply）。",
        }

    # Template-driven recreate
    was_running = False
    old = None
    try:
        old = get_container(name)
        was_running = (old.get("status") or "").lower() == "running"
    except Exception:
        # try by id from list
        for c in list_containers(all_containers=True):
            if (c.get("name") or "").lstrip("/") == name:
                old = c
                was_running = (c.get("status") or "").lower() == "running"
                break

    try:
        if old:
            try:
                stop_container(old.get("id") or name)
            except Exception:
                pass
            remove_container(old.get("id") or name, force=True)

        run_kwargs = template_to_run_kwargs(tpl, start=was_running)
        created = create_and_start(run_kwargs, start=was_running)

        # Extra networks
        extra = (tpl.get("extra_networks") or "").strip()
        if extra:
            for net in [x.strip() for x in extra.split(",") if x.strip()]:
                try:
                    connect_network(created.get("full_id") or created.get("id") or name, net)
                except Exception:
                    pass

        refresh_template_name_cache()
        rec = add_ops_record(
            action="unraid_update",
            target=name,
            status="ok",
            detail={
                "backup": backup.get("backup_path"),
                "pull": pull,
                "repository": target_image,
                "recreated": True,
                "managed_label": "dockerman",
            },
            actor=actor,
        )
        return {
            "ok": True,
            "record": rec,
            "backup": backup,
            "pull": pull,
            "container": created,
            "message": f"已按 Unraid 模板安全更新并重建 {name}（dockerman，非三方）",
        }
    except Exception as e:
        rec = add_ops_record(
            action="unraid_update",
            target=name,
            status="failed",
            detail={"step": "recreate", "error": str(e), "backup": backup.get("backup_path"), "pull": pull},
            actor=actor,
        )
        return {
            "ok": False,
            "record": rec,
            "backup": backup,
            "pull": pull,
            "message": f"模板重建失败：{e}。请用备份 XML 在 Unraid 中恢复。",
        }


def adopt_to_unraid(container_id: str, actor: str | None = None) -> dict[str, Any]:
    """Generate my-Name.xml from inspect and optionally recreate under dockerman."""
    settings = get_settings()
    settings.takeover_guard()
    if not templates_available():
        return {
            "ok": False,
            "message": f"模板目录不可用：{settings.unraid_templates_user}。请挂载 dockerMan/templates-user。",
        }

    try:
        detail = get_container(container_id)
    except KeyError:
        return {"ok": False, "message": "容器不存在"}

    if detail.get("manager") == "compose":
        return {
            "ok": False,
            "message": "该容器属于 Compose 项目，请用 Compose 接管，勿 Adopt 为 Unraid 单模板（避免双管理冲突）。",
        }

    name = (detail.get("name") or container_id).lstrip("/")
    xml_path = templates_root() / f"my-{name}.xml"
    xml_body = inspect_to_template_xml(detail)
    xml_path.write_text(xml_body, encoding="utf-8")
    refresh_template_name_cache()

    # Recreate once so Unraid shows dockerman not 3rd party
    result = safe_update_unraid(name, actor=actor, recreate=True)
    rec = add_ops_record(
        action="unraid_adopt",
        target=name,
        status="ok" if result.get("ok") else "failed",
        detail={"template": str(xml_path), "update": result.get("record")},
        actor=actor,
    )
    return {
        "ok": bool(result.get("ok")),
        "record": rec,
        "template_path": str(xml_path),
        "update": result,
        "message": result.get("message") or f"已 Adopt 为 Unraid 模板 my-{name}.xml",
    }


def template_to_run_kwargs(tpl: dict[str, Any], start: bool = True) -> dict[str, Any]:
    """Map Unraid template to docker SDK create/run kwargs. Always dockerman-managed."""
    name = tpl.get("name")
    image = tpl.get("repository")
    environment: dict[str, str] = {
        "HOST_OS": "Unraid",
        "HOST_CONTAINERNAME": name or "",
    }
    # preserve TZ if present later from configs
    volumes: dict[str, dict[str, str]] = {}
    ports: dict[str, int | tuple[str, int] | None] = {}
    devices: list[str] = []
    labels: dict[str, str] = {
        "net.unraid.docker.managed": "dockerman",
    }
    if tpl.get("webui"):
        labels["net.unraid.docker.webui"] = tpl["webui"]
    if tpl.get("icon"):
        labels["net.unraid.docker.icon"] = tpl["icon"]

    network = tpl.get("network") or "bridge"
    privileged = bool(tpl.get("privileged"))

    for cfg in tpl.get("configs") or []:
        ctype = (cfg.get("type") or "").strip()
        target = cfg.get("target") or ""
        value = cfg.get("value") or cfg.get("default") or ""
        mode = (cfg.get("mode") or "rw").lower()
        if ctype == "Variable" and target:
            environment[target] = value
        elif ctype == "Path" and target and value:
            # volume: host value -> container target
            volumes[value] = {"bind": target, "mode": "ro" if "ro" in mode else "rw"}
        elif ctype == "Port" and target and value:
            # host value -> container target
            # target may be like 8080 or 8080/tcp
            container_port = target if "/" in target else f"{target}/tcp"
            try:
                ports[container_port] = int(str(value).split(":")[-1]) if str(value).isdigit() else value
            except Exception:
                ports[container_port] = value
        elif ctype == "Label" and target:
            labels[target] = value
        elif ctype == "Device" and value:
            devices.append(value if ":" in value else f"{value}:{value}")

    if "TZ" not in environment:
        environment["TZ"] = "Asia/Shanghai"

    kwargs: dict[str, Any] = {
        "image": image,
        "name": name,
        "detach": True,
        "environment": environment,
        "labels": labels,
        "privileged": privileged,
        "restart_policy": {"Name": "unless-stopped"},
    }
    if volumes:
        kwargs["volumes"] = volumes
    if ports and network not in ("host", "none"):
        kwargs["ports"] = ports
    if devices:
        kwargs["devices"] = devices
    if network and network not in ("", "bridge"):
        if network.startswith("container:"):
            kwargs["network_mode"] = network
        else:
            kwargs["network"] = network
    elif network == "host":
        kwargs["network_mode"] = "host"
    elif network == "none":
        kwargs["network_mode"] = "none"

    # ExtraParams: best-effort parse common flags only (avoid shell injection)
    extra = tpl.get("extra_params") or ""
    _apply_extra_params(kwargs, extra)

    return kwargs


def inspect_to_template_xml(detail: dict[str, Any]) -> str:
    name = (detail.get("name") or "container").lstrip("/")
    root = ET.Element("Container", version="2")
    _text(root, "Name", name)
    _text(root, "Repository", detail.get("image") or "")
    _text(root, "Registry", "")
    net = "bridge"
    networks = detail.get("networks") or []
    rth = detail.get("runtime_host_config") or {}
    nm = rth.get("NetworkMode") or ""
    if nm == "host":
        net = "host"
    elif nm == "none":
        net = "none"
    elif networks:
        net = networks[0]
    _text(root, "Network", net)
    _text(root, "ExtraNetworks", ",".join(networks[1:]) if len(networks) > 1 else "")
    _text(root, "Privileged", "true" if detail.get("privileged") else "false")
    webui = (detail.get("labels") or {}).get("net.unraid.docker.webui") or ""
    icon = (detail.get("labels") or {}).get("net.unraid.docker.icon") or ""
    _text(root, "WebUI", webui)
    _text(root, "Icon", icon)
    _text(root, "ExtraParams", "")
    _text(root, "PostArgs", "")
    _text(root, "Overview", f"Adopted by DockerOps from inspect of {name}")
    _text(root, "Category", "Tools:")
    _text(root, "Shell", "sh")

    # Env
    for item in detail.get("env_raw") or []:
        if "=" not in item:
            continue
        k, v = item.split("=", 1)
        if k in ("HOST_OS", "HOST_HOSTNAME", "HOST_CONTAINERNAME"):
            continue
        cfg = ET.SubElement(
            root,
            "Config",
            Name=k,
            Target=k,
            Default="",
            Mode="",
            Description="",
            Type="Variable",
            Display="always",
            Required="false",
            Mask="false",
        )
        cfg.text = v

    # Paths
    for m in detail.get("mounts") or []:
        if m.get("type") not in ("bind", "volume", None):
            # still export binds
            pass
        src = m.get("source") or ""
        dst = m.get("destination") or ""
        if not dst:
            continue
        mode = "rw" if m.get("rw", True) else "ro"
        cfg = ET.SubElement(
            root,
            "Config",
            Name=dst,
            Target=dst,
            Default="",
            Mode=mode,
            Description="",
            Type="Path",
            Display="always",
            Required="false",
            Mask="false",
        )
        cfg.text = src

    # Ports from port_bindings
    pb = detail.get("port_bindings") or {}
    for container_port, binds in pb.items():
        # container_port like 8080/tcp
        target = container_port.split("/")[0]
        host_port = ""
        if binds and isinstance(binds, list) and binds[0]:
            host_port = str(binds[0].get("HostPort") or "")
        cfg = ET.SubElement(
            root,
            "Config",
            Name=f"Port {target}",
            Target=target,
            Default="",
            Mode="tcp" if container_port.endswith("/tcp") else "udp",
            Description="",
            Type="Port",
            Display="always",
            Required="false",
            Mask="false",
        )
        cfg.text = host_port

    # Ensure managed label in template labels section
    cfg = ET.SubElement(
        root,
        "Config",
        Name="managed",
        Target="net.unraid.docker.managed",
        Default="dockerman",
        Mode="",
        Description="Unraid DockerMan",
        Type="Label",
        Display="advanced",
        Required="false",
        Mask="false",
    )
    cfg.text = "dockerman"

    rough = ET.tostring(root, encoding="unicode")
    try:
        parsed = minidom.parseString(rough)
        return parsed.toprettyxml(indent="  ")
    except Exception:
        return rough


def _patch_repository(path: Path, repository: str) -> None:
    tree = ET.parse(path)
    root = tree.getroot()
    el = root.find("Repository")
    if el is None:
        el = ET.SubElement(root, "Repository")
    el.text = repository
    tree.write(path, encoding="utf-8", xml_declaration=True)


def _text(parent: ET.Element, tag: str, value: str) -> None:
    el = ET.SubElement(parent, tag)
    el.text = value


def _find_container(name: str) -> dict[str, Any] | None:
    if not name:
        return None
    try:
        for c in list_containers(all_containers=True):
            if (c.get("name") or "").lstrip("/") == name:
                return {
                    "id": c.get("id"),
                    "name": c.get("name"),
                    "status": c.get("status"),
                    "image": c.get("image"),
                    "manager": c.get("manager"),
                }
    except Exception:
        return None
    return None


def _apply_extra_params(kwargs: dict[str, Any], extra: str) -> None:
    """Best-effort support for a few common ExtraParams tokens."""
    if not extra.strip():
        return
    tokens = extra.split()
    i = 0
    while i < len(tokens):
        t = tokens[i]
        if t == "--hostname" and i + 1 < len(tokens):
            kwargs["hostname"] = tokens[i + 1]
            i += 2
            continue
        if t.startswith("--hostname="):
            kwargs["hostname"] = t.split("=", 1)[1]
            i += 1
            continue
        if t == "--dns" and i + 1 < len(tokens):
            kwargs.setdefault("dns", []).append(tokens[i + 1])
            i += 2
            continue
        if t == "--pids-limit" and i + 1 < len(tokens):
            try:
                kwargs["pids_limit"] = int(tokens[i + 1])
            except Exception:
                pass
            i += 2
            continue
        if t in ("--rm",):
            kwargs["auto_remove"] = True
            i += 1
            continue
        i += 1

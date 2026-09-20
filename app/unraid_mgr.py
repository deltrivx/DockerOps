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
import unraid_host
from docker_client import (
    cleanup_superseded_images,
    container_image_id,
    get_container,
    list_containers,
    pull_image,
    refresh_template_name_cache,
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


def _emit_progress(on_progress, payload: dict[str, Any]) -> None:
    if not on_progress:
        return
    try:
        on_progress(payload)
    except Exception:
        pass


def safe_update_unraid(
    name: str,
    actor: str | None = None,
    *,
    repository: str | None = None,
    recreate: bool = True,
    on_progress=None,
) -> dict[str, Any]:
    """Update a container through its Unraid template.

    Unraid decides which template owns a container by reading
    ``net.unraid.docker.managed``. Rebuilding by hand — stop, remove, then a
    create call built from parsed template values — produces a container that
    looks identical but has lost that label, so Unraid files it under "third
    party" and the template stops working. Copying every value across does not
    help; the label is only written by Unraid's own template path.

    So the supported route is the host's own scripts:
    ``update_container`` (pull, then recreate) or ``rebuild_container``
    (recreate from what is on disk). When the host cannot be reached at all, the
    update is reported as a pull-only partial rather than falling back to a
    rebuild that would quietly break template ownership.
    """
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

    _emit_progress(
        on_progress,
        {"event": "stage", "stage": "backup", "message": f"备份模板 {name}", "container": name},
    )
    backup = backup_template(name, actor=actor)
    if not backup.get("ok"):
        _emit_progress(on_progress, {"event": "error", "message": "备份失败", "container": name})
        return {"ok": False, "message": "备份失败，已中止", "backup": backup}

    target_image = repository or tpl.get("repository")

    # Retarget the template before pulling: update_container reads <Repository>
    # to decide what to pull, so a new tag takes effect only if written first.
    if repository and repository != tpl.get("repository"):
        try:
            settings.takeover_guard()
        except PermissionError as e:
            return {"ok": False, "message": str(e), "backup": backup}
        patched = patch_template_repository(Path(tpl["path"]), repository)
        if not patched.get("ok"):
            return {"ok": False, "message": patched.get("message"), "backup": backup}
        tpl = get_template(name) or tpl
        target_image = repository

    if not target_image:
        return {"ok": False, "message": "模板缺少 Repository", "backup": backup}

    if not recreate:
        return _unraid_pull_only(
            name, tpl, target_image, backup, actor, on_progress,
            reason="调用方指定仅拉取",
        )

    try:
        settings.takeover_guard()
    except PermissionError as e:
        return _unraid_pull_only(
            name, tpl, target_image, backup, actor, on_progress, reason=str(e)
        )

    # ---- Preferred path: the host's own template scripts -------------------
    executor = _resolve_executor()
    scripts_ok, scripts_detail = unraid_host.script_available(executor)
    if scripts_ok:
        return _unraid_host_update(
            name, tpl, target_image, backup, actor, on_progress, executor
        )

    rec = add_ops_record(
        action="unraid_update",
        target=name,
        status="failed",
        detail={"step": "host_scripts", "reason": scripts_detail},
        actor=actor,
    )
    _emit_progress(
        on_progress,
        {"event": "error", "message": scripts_detail, "container": name},
    )
    return {
        "ok": False,
        "record": rec,
        "backup": backup,
        "host_scripts_available": False,
        "message": (
            f"{scripts_detail}"
            "为避免容器脱离模板管理，本次不做重建。"
            "请配置宿主机 SSH 通道（DOCKEROPS_SSH_HOST 等），"
            "或在 Unraid Docker 页面手动点击更新。"
        ),
    }


def _resolve_executor():
    """Host executor for this request, or ``None`` when unreachable."""
    try:
        from host_exec import get_host_executor

        return get_host_executor()
    except Exception:
        return None


def _unraid_pull_only(
    name: str,
    tpl: dict[str, Any],
    target_image: str,
    backup: dict[str, Any],
    actor: str | None,
    on_progress,
    *,
    reason: str,
) -> dict[str, Any]:
    """Pull the image but leave the container alone."""
    _emit_progress(
        on_progress,
        {"event": "stage", "stage": "pull", "message": f"拉取镜像 {target_image}", "container": name},
    )

    def _pull_cb(chunk: dict[str, Any]) -> None:
        pd = chunk.get("progressDetail") or {}
        cur = pd.get("current")
        total = pd.get("total")
        percent = None
        if isinstance(cur, (int, float)) and isinstance(total, (int, float)) and total:
            percent = round(float(cur) / float(total) * 100, 1)
        _emit_progress(
            on_progress,
            {
                "event": "pull",
                "status": chunk.get("status") or "",
                "id": chunk.get("id") or "",
                "current": cur,
                "total": total,
                "percent": percent,
                "container": name,
                "image": target_image,
            },
        )

    try:
        pull = pull_image(target_image, on_progress=_pull_cb)
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
            "message": f"已备份并拉取镜像；未重建容器（{reason}）",
        }
    except Exception as e:
        rec = add_ops_record(
            action="unraid_update",
            target=name,
            status="failed",
            detail={"step": "pull", "error": str(e), "backup": backup.get("backup_path")},
            actor=actor,
        )
        return {"ok": False, "record": rec, "backup": backup, "message": f"拉镜像失败：{e}"}


def _unraid_host_update(
    name: str,
    tpl: dict[str, Any],
    target_image: str,
    backup: dict[str, Any],
    actor: str | None,
    on_progress,
    executor,
) -> dict[str, Any]:
    """Rebuild through ``update_container`` / ``rebuild_container``."""
    _emit_progress(
        on_progress,
        {
            "event": "stage",
            "stage": "host_update",
            "message": f"调用宿主机模板脚本重建 {name}",
            "container": name,
        },
    )

    old_image_id = None
    try:
        old = get_container(name)
        old_image_id = container_image_id(old.get("full_id") or old.get("id") or name)
    except Exception:
        pass

    try:
        result = unraid_host.host_update_container(name, executor=executor, pull=True)
    except unraid_host.HostScriptUnavailable as e:
        rec = add_ops_record(
            action="unraid_update",
            target=name,
            status="failed",
            detail={"step": "host_update", "reason": str(e)},
            actor=actor,
        )
        return {"ok": False, "record": rec, "backup": backup, "message": str(e)}
    except Exception as e:
        rec = add_ops_record(
            action="unraid_update",
            target=name,
            status="failed",
            detail={"step": "host_update", "error": str(e), "backup": backup.get("backup_path")},
            actor=actor,
        )
        return {
            "ok": False,
            "record": rec,
            "backup": backup,
            "message": f"模板脚本重建失败：{e}。请用备份 XML 恢复。",
        }

    label = result.get("managed_label") or {}
    if not label.get("ok"):
        rec = add_ops_record(
            action="unraid_update",
            target=name,
            status="failed",
            detail={
                "step": "verify_label",
                "label": label,
                "backup": backup.get("backup_path"),
            },
            actor=actor,
        )
        msg = label.get("message") or "容器模板归属校验失败"
        _emit_progress(on_progress, {"event": "error", "message": msg, "container": name})
        return {
            "ok": False,
            "record": rec,
            "backup": backup,
            "managed_label": label,
            "message": f"{msg}。容器可能已脱离模板管理，请检查模板或手动重建。",
        }

    # Unraid's script drops the image it saw before the pull; ids still in use
    # by the new container are protected.
    image_cleanup = None
    if old_image_id:
        _emit_progress(
            on_progress,
            {
                "event": "stage",
                "stage": "cleanup_images",
                "message": "清理被替换的旧镜像 / dangling 层",
                "container": name,
            },
        )
        keep_ids: list[str] = []
        try:
            new_iid = container_image_id(name)
            if new_iid:
                keep_ids.append(new_iid)
        except Exception:
            pass
        try:
            image_cleanup = cleanup_superseded_images(
                [old_image_id], dangling_prune=True, keep_image_ids=keep_ids
            )
        except Exception as e:
            image_cleanup = {"ok": False, "error": str(e)}

    try:
        refresh_template_name_cache()
    except Exception:
        pass

    rec = add_ops_record(
        action="unraid_update",
        target=name,
        status="ok",
        detail={
            "backup": backup.get("backup_path"),
            "repository": target_image,
            "recreated": True,
            "via": result.get("script"),
            "managed_label": label,
            "restarted": result.get("restarted"),
            "old_image_id": old_image_id,
            "image_cleanup": image_cleanup,
        },
        actor=actor,
    )

    msg = f"已按 Unraid 模板更新并重建 {name}（{result.get('script')}）"
    if result.get("restarted"):
        msg += "；已重新启动容器"
    if image_cleanup and image_cleanup.get("removed_count"):
        msg += f"；已清理旧镜像 {image_cleanup.get('removed_count')} 个"

    _emit_progress(
        on_progress,
        {"event": "stage", "stage": "done", "message": msg, "container": name, "ok": True},
    )
    return {
        "ok": True,
        "record": rec,
        "backup": backup,
        "container": get_container(name) if _exists(name) else None,
        "managed_label": label,
        "old_image_id": old_image_id,
        "image_cleanup": image_cleanup,
        "message": msg,
    }


def _exists(name: str) -> bool:
    try:
        get_container(name)
        return True
    except Exception:
        return False


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

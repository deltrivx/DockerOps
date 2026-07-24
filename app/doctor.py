from __future__ import annotations

from typing import Any

from docker_client import get_container, list_containers, ping


def diagnose_all() -> dict[str, Any]:
    engine = ping()
    findings: list[dict[str, Any]] = []
    containers: list[dict[str, Any]] = []

    if not engine.get("ok"):
        findings.append(
            {
                "level": "critical",
                "code": "engine_unreachable",
                "message": f"无法连接 Docker 引擎：{engine.get('error', 'unknown')}",
                "score_impact": -50,
            }
        )
        return _pack(score=20, findings=findings, containers=[], engine=engine)

    try:
        containers = list_containers(all_containers=True)
    except Exception as e:
        findings.append(
            {
                "level": "critical",
                "code": "list_failed",
                "message": f"列出容器失败：{e}",
                "score_impact": -40,
            }
        )
        return _pack(score=30, findings=findings, containers=[], engine=engine)

    if not containers:
        findings.append(
            {
                "level": "info",
                "code": "no_containers",
                "message": "当前没有容器。若这是新环境，属正常。",
                "score_impact": 0,
            }
        )
        return _pack(score=90, findings=findings, containers=[], engine=engine)

    per_container = []
    total_impact = 0
    for c in containers:
        d = diagnose_container_summary(c)
        per_container.append(d)
        total_impact += d.get("score_impact", 0)
        findings.extend(d.get("findings") or [])

    # base 100, clamp
    score = max(0, min(100, 100 + total_impact))
    summary = _score_label(score)
    return _pack(
        score=score,
        findings=findings,
        containers=per_container,
        engine=engine,
        summary=summary,
    )


def diagnose_one(container_id: str) -> dict[str, Any]:
    try:
        detail = get_container(container_id)
    except KeyError:
        return {
            "ok": False,
            "error": "container_not_found",
            "message": f"未找到容器：{container_id}",
        }
    d = diagnose_container_summary(detail, detailed=True)
    d["detail"] = detail
    d["ok"] = True
    return d


def diagnose_container_summary(c: dict[str, Any], detailed: bool = False) -> dict[str, Any]:
    findings: list[dict[str, Any]] = []
    impact = 0
    name = c.get("name") or c.get("id")
    status = (c.get("status") or c.get("state") or "").lower()
    health = (c.get("health") or "").lower() if c.get("health") else None
    restarts = int(c.get("restart_count") or 0)

    if status in ("exited", "dead"):
        findings.append(
            {
                "level": "critical",
                "code": "not_running",
                "target": name,
                "message": f"容器 {name} 未运行（状态：{status}）。",
                "score_impact": -15,
            }
        )
        impact -= 15
    elif status == "restarting":
        findings.append(
            {
                "level": "warning",
                "code": "restarting",
                "target": name,
                "message": f"容器 {name} 正在反复重启。",
                "score_impact": -12,
            }
        )
        impact -= 12
    elif status == "paused":
        findings.append(
            {
                "level": "warning",
                "code": "paused",
                "target": name,
                "message": f"容器 {name} 已暂停。",
                "score_impact": -5,
            }
        )
        impact -= 5

    if health == "unhealthy":
        findings.append(
            {
                "level": "critical",
                "code": "unhealthy",
                "target": name,
                "message": f"容器 {name} 健康检查失败（unhealthy）。",
                "score_impact": -18,
            }
        )
        impact -= 18
    elif health == "starting":
        findings.append(
            {
                "level": "info",
                "code": "health_starting",
                "target": name,
                "message": f"容器 {name} 健康检查仍在启动中。",
                "score_impact": -2,
            }
        )
        impact -= 2

    if restarts >= 10:
        findings.append(
            {
                "level": "warning",
                "code": "high_restarts",
                "target": name,
                "message": f"容器 {name} 重启次数偏高（{restarts}）。",
                "score_impact": -10,
            }
        )
        impact -= 10
    elif restarts >= 3:
        findings.append(
            {
                "level": "info",
                "code": "some_restarts",
                "target": name,
                "message": f"容器 {name} 有过重启（{restarts} 次）。",
                "score_impact": -3,
            }
        )
        impact -= 3

    if c.get("oom_killed"):
        findings.append(
            {
                "level": "critical",
                "code": "oom",
                "target": name,
                "message": f"容器 {name} 曾被 OOM Killer 终止。",
                "score_impact": -20,
            }
        )
        impact -= 20

    stats = c.get("stats") or {}
    mem_pct = stats.get("memory_percent")
    cpu_pct = stats.get("cpu_percent")
    if isinstance(mem_pct, (int, float)) and mem_pct >= 90:
        findings.append(
            {
                "level": "warning",
                "code": "high_memory",
                "target": name,
                "message": f"容器 {name} 内存占用偏高（{mem_pct}%）。",
                "score_impact": -8,
            }
        )
        impact -= 8
    if isinstance(cpu_pct, (int, float)) and cpu_pct >= 95:
        findings.append(
            {
                "level": "warning",
                "code": "high_cpu",
                "target": name,
                "message": f"容器 {name} CPU 占用偏高（{cpu_pct}%）。",
                "score_impact": -6,
            }
        )
        impact -= 6

    if c.get("privileged"):
        findings.append(
            {
                "level": "info",
                "code": "privileged",
                "target": name,
                "message": f"容器 {name} 以 privileged 模式运行，请确认确有必要。",
                "score_impact": -1,
            }
        )
        impact -= 1

    mounts = c.get("mounts") or []
    if detailed and not mounts and status == "running":
        findings.append(
            {
                "level": "info",
                "code": "no_mounts",
                "target": name,
                "message": f"容器 {name} 没有挂载卷，数据可能随容器删除丢失。",
                "score_impact": 0,
            }
        )

    local_score = max(0, min(100, 100 + impact))
    return {
        "id": c.get("id"),
        "name": name,
        "status": status,
        "health": health,
        "restart_count": restarts,
        "score": local_score,
        "score_impact": impact,
        "findings": findings,
        "label": _score_label(local_score),
    }


def _score_label(score: int) -> str:
    if score >= 90:
        return "健康"
    if score >= 75:
        return "良好"
    if score >= 60:
        return "一般"
    if score >= 40:
        return "风险"
    return "危急"


def _pack(
    score: int,
    findings: list[dict[str, Any]],
    containers: list[dict[str, Any]],
    engine: dict[str, Any],
    summary: str | None = None,
) -> dict[str, Any]:
    # de-dup findings by code+target for top-level list readability
    critical = sum(1 for f in findings if f.get("level") == "critical")
    warning = sum(1 for f in findings if f.get("level") == "warning")
    return {
        "ok": True,
        "health_score": score,
        "label": summary or _score_label(score),
        "counts": {
            "containers": len(containers),
            "critical": critical,
            "warning": warning,
            "info": sum(1 for f in findings if f.get("level") == "info"),
        },
        "engine": engine,
        "findings": findings,
        "containers": containers,
        "advice": _advice(score, critical, warning),
    }


def _advice(score: int, critical: int, warning: int) -> list[str]:
    tips: list[str] = []
    if critical:
        tips.append("优先处理 critical 项：未运行、unhealthy、OOM 通常最影响可用性。")
    if warning:
        tips.append("关注 warning：高重启、高资源占用往往是故障前兆。")
    if score >= 90:
        tips.append("整体健康。建议保持定期巡检与更新前备份习惯。")
    elif score >= 60:
        tips.append("建议查看 Doctor 明细，逐项修复后再考虑批量更新。")
    else:
        tips.append("健康分偏低：暂缓非必要更新，先恢复关键服务再操作。")
    tips.append("更新前使用备份接口留下可追溯记录，失败时按运维记录回滚。")
    return tips

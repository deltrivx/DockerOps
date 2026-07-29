from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

from auth import (
    AuthUser,
    OptionalUser,
    actor_from_token,
    auth_status,
    bootstrap_from_env,
    change_password,
    complete_setup,
    login,
    logout,
)
from compose_mgr import (
    backup_project,
    get_project,
    list_projects,
    project_down,
    project_up,
    safe_update_project,
)
from config import (
    apply_proxy_to_environ,
    capture_proxy_env_lock_at_boot,
    env_proxy_locked,
    get_settings,
    read_process_proxy,
)
from db import (
    audit,
    create_endpoint,
    delete_endpoint,
    get_active_endpoint_id,
    get_endpoint,
    init_db,
    list_endpoints,
    set_active_endpoint_id,
    update_endpoint,
)
from docker_client import (
    connect_network,
    disconnect_network,
    exec_create,
    exec_resize,
    exec_start_socket,
    get_client,
    get_container,
    image_history,
    invalidate_client,
    list_containers,
    list_running_stats,
    ping,
    refresh_template_name_cache,
    reset_request_endpoint,
    resolve_shell_cmd,
    set_request_endpoint,
)
from endpoints import (
    endpoint_capabilities,
    is_local_host,
    public_endpoint,
    require_local_endpoint,
    validate_docker_host,
)
from docker_resources import (
    activity_stats,
    batch_lifecycle,
    images_list,
    images_prune,
    images_pull,
    images_remove,
    lifecycle,
    networks_create,
    networks_list,
    networks_remove,
    sys_df,
    sys_info,
    sys_prune,
    volumes_create,
    volumes_list,
    volumes_prune,
    volumes_remove,
)
from db import get_meta, set_meta  # noqa: E402 — re-export style keep near meta helpers
from doctor import diagnose_all, diagnose_one
from events_stream import recent_events, sse_docker_events
from host_platform import platform_info
from logs_stream import get_logs, sse_log_events
from manager import managers_summary
from monitor import collect_report, get_latest_or_collect
from ops import backup_container, records, rollback_guide, safe_update
from unraid_mgr import (
    adopt_to_unraid,
    backup_template,
    get_template,
    list_templates,
    safe_update_unraid,
    templates_available,
)
from update_detect import (
    detect_updates,
    get_cached_update_status,
    get_update_auto_settings,
    one_click_update,
    set_update_auto_settings,
    start_update_auto_check_thread,
)
from remote import (
    agent_is_managed_locked,
    agent_switch_mode_local,
    create_pair_code,
    execute_local_rpc,
    get_pair_status,
    get_remote_settings,
    get_session,
    get_session_by_token,
    is_agent_online,
    list_controller_outbound_sessions,
    list_remote_sessions,
    managed_blocks_local_path,
    normalize_base_url,
    parse_remote_endpoint_id,
    public_remote_status,
    public_settings_view,
    redeem_pair_code,
    register_agent_ws,
    remote_endpoint_id,
    resolve_rpc_response,
    revoke_session,
    rpc_to_agent,
    set_remote_settings,
    set_session_mode,
    touch_session,
    unregister_agent_ws,
)
from remote_agent import agent_runtime_state, stop_agent_dial
from remote_controller import (
    dial_runtime_state,
    resume_controller_sessions,
    start_controller_connect,
    stop_all_controller_dials,
    stop_controller_dial,
)

APP_DIR = Path(__file__).resolve().parent
VERSION = "0.8.2"
CHANGELOG = [
    {
        "version": "0.8.2",
        "date": "2026-07-29",
        "items": [
            "修正分步 UI：未选协同/托管时不露出公网地址与生成按钮（不再被服务端历史 mode 误展开）",
            "明确主控端无需公网：仅粘贴被控凭证；公网/IP 仅被控填写",
            "切换角色/关闭远程时重置向导态，避免主控界面夹带被控字段",
        ],
    },
    {
        "version": "0.8.1",
        "date": "2026-07-29",
        "items": [
            "远程设置改为分步动态显示：关→仅开关；开→选角色；被控再逐步出模式/公网地址/生成凭证",
            "等待连接时聚焦凭证与倒计时；主控节点表仅在有连接后显示",
            "切换模式回到精简重选界面；生成前校验已选协同/托管",
        ],
    },
    {
        "version": "0.8.0",
        "date": "2026-07-29",
        "items": [
            "远程模式重做：被控生成连接凭证，主控粘贴后主动连接被控（不再照抄哪吒拨出）",
            "凭证 dop1 内嵌被控公网/IP；60 秒一次性；主控可连多台被控",
            "被控协同横幅 / 托管全屏 +「切换模式」；主控本机功能不受影响",
            "不兼容 0.7 旧凭证：请双方升级后在被控重新生成",
        ],
    },
    {
        "version": "0.7.1",
        "date": "2026-07-29",
        "items": [
            "远程托管锁定：被控全屏锁定条 + 本地写操作 API 门禁（主控 RPC 仍可管）",
            "主控可对节点切换 协同/托管；被控实时 mode_change；状态条与凭证一键复制",
            "远程状态轮询刷新；会话列表展示模式并支持断开",
        ],
    },
    {
        "version": "0.7.0",
        "date": "2026-07-29",
        "items": [
            "远程模式一期（已由 0.8 取代配对方向）：主控/被控协同，60 秒配对凭证",
            "历史：被控主动连主控；0.8 起改为被控发码、主控连接被控",
            "主控顶栏端点可选在线远程节点；容器/镜像/更新等经 RPC 代理到被控",
        ],
    },
    {
        "version": "0.6.5",
        "date": "2026-07-29",
        "items": [
            "更新检测并入「容器」页：独立侧栏「更新检测」已移除",
            "容器表新增「更新」列与筛选；立即检测 / 一键更新保留在容器工具栏",
            "说明日志补充「域名 + Docker API」远程端点方案；设置页 Host 示例扩展",
        ],
    },
    {
        "version": "0.6.4",
        "date": "2026-07-29",
        "items": [
            "总览「近期 Docker 事件」隐藏横向/纵向滚动条外观，保留滚轮与触控滑动",
            "事件行长文本自动换行，减少不必要的横向溢出",
        ],
    },
    {
        "version": "0.6.3",
        "date": "2026-07-29",
        "items": [
            "移除侧栏品牌区关闭叉号；移动端仍可点遮罩或切换菜单关闭抽屉",
            "总览健康分下方诊断长文迁至「说明日志」页的健康诊断说明",
        ],
    },
    {
        "version": "0.6.2",
        "date": "2026-07-29",
        "items": [
            "Compose/飞牛更新对齐 Unraid：force-recreate 无需手动停容器，默认 --remove-orphans",
            "更新成功后自动清理被替换旧镜像与 dangling 层（不做 prune -a）",
            "Compose 硬替换兜底：若服务仍挂旧 image id，则 stop→remove→up --no-deps",
            "一键更新结束后 dangling 收尾；partial/三方/飞牛提示更明确",
        ],
    },
    {
        "version": "0.6.1",
        "date": "2026-07-27",
        "items": [
            "修复启动死锁：init_db 在持有 SQLite 锁时调用 ensure_default_endpoint 导致 uvicorn 永不监听",
        ],
    },
    {
        "version": "0.6.0",
        "date": "2026-07-27",
        "items": [
            "多 Docker 端点：本机 unix sock + 远程 tcp://（可选 TLS）",
            "顶栏切换活动端点；系统设置增删改 / 连通性测试",
            "远程端点开放容器/镜像/网络/卷/日志/终端/更新检测；Compose 与 Unraid 仅本地",
        ],
    },
    {
        "version": "0.5.8",
        "date": "2026-07-27",
        "items": [
            "状态列不再裁字：pill（running 等）保证最小宽度与 overflow 可见",
            "Unraid/容器等表移动端状态与操作列地板加宽，超出仍横滑",
        ],
    },
    {
        "version": "0.5.7",
        "date": "2026-07-27",
        "items": [
            "移动端表格不再硬挤：列设可读最小宽度，超出时 table-wrap 横向滑动",
            "保留勾选/名称 sticky；底部提示「左右滑动查看更多」",
        ],
    },
    {
        "version": "0.5.6",
        "date": "2026-07-27",
        "items": [
            "移动端内容区对齐：批量栏 meta/按钮分区 + 2 列等宽网格，按钮文字水平居中",
            "藏列时同步隐藏 colgroup 槽位并重配可见列宽，操作列可横排「停止+更多」",
            "工具栏筛选两列网格",
        ],
    },
    {
        "version": "0.5.5",
        "date": "2026-07-26",
        "items": [
            "刷新/重开保持当前页签（hash + localStorage），不再跳回总览",
            "有会话时静默后台加载控制台，不再闪「加载控制台」门闩",
            "移动端顶栏与操作按钮对齐；窄屏操作列加宽",
        ],
    },
    {
        "version": "0.5.4",
        "date": "2026-07-26",
        "items": [
            "操作按钮按单元格可用宽度自动外露，仅放不下时才收进「更多」",
        ],
    },
    {
        "version": "0.5.3",
        "date": "2026-07-26",
        "items": [
            "表格去空洞：操作列左对齐紧贴内容；镜像/路径列吃满中间空白",
            "常用操作外提：容器 启停/重启/日志/终端；更新/Compose/Unraid 主按钮不再塞进「更多」",
            "镜像：移除「清理 dangling」；「清理未使用」显式 prune -a + 未引用镜像回退删除",
        ],
    },
    {
        "version": "0.5.2",
        "date": "2026-07-26",
        "items": [
            "表格列宽重配：缩短镜像列、加宽操作列；桌面最多 3 个主按钮；检测结果列去空白",
            "更新进度弹窗：SSE 阶段（备份/拉取层进度/重建/完成）+ 一键更新逐项进度",
            "镜像页显示关联容器；一键清理未使用镜像；镜像详情弹窗",
        ],
    },
    {
        "version": "0.5.1",
        "date": "2026-07-26",
        "items": [
            "修复 Web 终端：exec_resize 须在 exec_start 之后，避免握手阻塞超时",
            "终端建立路径改为线程池执行 docker 调用，避免卡住事件循环",
        ],
    },
    {
        "version": "0.5.0",
        "date": "2026-07-26",
        "items": [
            "容器 Web 终端（docker exec + WebSocket + xterm，DOCKEROPS_CONSOLE_ENABLED）",
            "实时日志跟随（SSE）；详情增强（挂载/环境/网络/端口）；强制停止；网络连接/断开",
            "移动端表格藏次要列 + 操作 1 主按钮，改善窄屏挤压",
        ],
    },
    {
        "version": "0.4.9",
        "date": "2026-07-26",
        "items": [
            "镜像表布局重写：百分比列宽 + 名称单行省略，杜绝竖排/列错位",
            "代理写入 SQLite system_proxy 并在启动恢复；env 锁定仅认启动时容器变量，Web 保存不再误锁",
            "静态资源 ?v= 缓存破坏；创建时间服务端格式化",
        ],
    },
    {
        "version": "0.4.8",
        "date": "2026-07-26",
        "items": [
            "修复镜像/资源表严重错位：禁止对 td.actions 使用 display:flex（会破坏表格导致名称竖排）",
            "操作列保持 table-cell，仅内部 action-group 用 flex；修复创建时间纳秒 ISO 解析",
        ],
    },
    {
        "version": "0.4.7",
        "date": "2026-07-26",
        "items": [
            "表格列宽重配：紧凑固定列 + 弹性文本列，消除后半段断层与大片留白",
            "表格内 mono 强制单行省略，避免长路径/镜像名被挤压成多行",
            "操作列统一为最多 2 个主按钮 +「更多」；取消 60 秒自动刷新（仅手动刷新）",
        ],
    },
    {
        "version": "0.4.6",
        "date": "2026-07-26",
        "items": [
            "修复操作列按钮过多导致表格从操作处割裂：主操作 +「更多」下拉",
            "镜像列表重排：仓库/标签分行、固定列宽、按占用排序，避免混乱",
            "表格仅裁剪文本列，操作列始终可见",
        ],
    },
    {
        "version": "0.4.5",
        "date": "2026-07-26",
        "items": [
            "更新检测对齐 Unraid：后台自动扫描 + 状态缓存，容器列表/总览直接显示可更新徽标",
            "镜像页对齐 Portainer：标签/ID/占用/多选；修复长镜像名导致页面向右错位",
            "系统设置：代理配置（环境变量优先自动识别锁定，或 Web 保存到 SQLite）",
        ],
    },
    {
        "version": "0.4.4",
        "date": "2026-07-26",
        "items": [
            "修复首屏卡顿：活动容器 stats 不再阻塞总览（延后加载 + 并行采样）",
            "loadAll 竞态保护与顶部加载提示，避免显示异常后才刷新",
            "activity 默认采样并行化，超时保护",
        ],
    },
    {
        "version": "0.4.3",
        "date": "2026-07-26",
        "items": [
            "打开网页：无账号强制首次初始化；有账号强制登录门（不可 Esc 绕过）",
            "登录页「忘记密码」展示终端改密命令（docker exec … python -m tools.reset_password）",
            "新增 /api/auth/me 会话校验；改密后旧会话失效",
            "内置 CLI：tools.reset_password 支持列表/重置/清空用户",
        ],
    },
    {
        "version": "0.4.2",
        "date": "2026-07-25",
        "items": [
            "去除默认账号：无 admin/dockerops 内置凭据，登录/安装向导不预填",
            "账号与会话/审计/偏好仅存内置 SQLite（/data/dockerops.db）",
            "可选 env 预置须同时显式设置 DOCKEROPS_ADMIN_USER + PASSWORD；Settings 空默认不 bootstrap",
            "Compose / Unraid 模板 / 飞牛配方不再注入默认管理员",
        ],
    },
    {
        "version": "0.4.1",
        "date": "2026-07-25",
        "items": [
            "修复手机端无法点击：侧栏遮罩 [hidden] 被 CSS 覆盖导致拦截触摸",
            "修复 PC 粒子不可见：提高亮度/连线密度，卡片半透明露出粒子层",
            "无 Compose 项目时自动隐藏侧栏 Compose（Unraid 无插件/纯 docker run 不显示）",
        ],
    },
    {
        "version": "0.4.0",
        "date": "2026-07-25",
        "items": [
            "运维控制台总览：平台/引擎卡片、健康分、系统信息、活动容器资源",
            "Unraid 风格模块卡片 + PC/平板/手机响应式",
            "粒子特效增强 + 背景个性化设置",
            "说明与更新日志独立菜单",
            "Portainer 日常补齐：批量启停/重启、重命名、活动 stats、镜像 history",
            "侧栏平台徽章：飞牛系统 / Unraid 系统",
        ],
    },
    {
        "version": "0.3.3",
        "date": "2026-07-25",
        "items": [
            "修复飞牛 FPK 桌面 127.0.0.1 黑屏（CGI 智能跳转）",
            "品牌图标补全；Unraid 容器名 DockerOps",
        ],
    },
    {
        "version": "0.3.2",
        "date": "2026-07-24",
        "items": [
            "Unraid 风格侧栏 UI + 博客粒子背景",
            "一键检测 / 一键更新（Registry digest）",
            "资源表搜索过滤",
        ],
    },
    {
        "version": "0.3.1",
        "date": "2026-07-24",
        "items": [
            "首次管理员设置向导",
            "飞牛专业 FPK Release 附件",
        ],
    },
    {
        "version": "0.3.0",
        "date": "2026-07-24",
        "items": [
            "日常运维全覆盖：生命周期/日志/镜像/网络/卷/系统/事件",
            "Compose + Unraid 模板双方接管",
        ],
    },
]
settings = get_settings()
init_db()

app = FastAPI(
    title="DockerOps",
    description=(
        "面向 NAS 的 Docker 运维平台 — 首次设置向导、日常运维（生命周期/日志/镜像/网络/卷）、"
        "Compose 与 Unraid/飞牛引擎级双方接管、安全更新、Doctor 诊断。"
    ),
    version=VERSION,
    docs_url="/docs",
    redoc_url="/redoc",
)

templates = Jinja2Templates(directory=str(APP_DIR / "templates"))
app.mount("/static", StaticFiles(directory=str(APP_DIR / "static")), name="static")


class LoginBody(BaseModel):
    username: str
    password: str


class SetupBody(BaseModel):
    username: str = Field(..., min_length=3, max_length=32, description="管理员用户名")
    password: str = Field(..., min_length=6, max_length=128, description="管理员密码")
    password_confirm: str = Field(..., min_length=6, max_length=128, description="确认密码")


class ChangePasswordBody(BaseModel):
    old_password: str
    new_password: str = Field(..., min_length=6, max_length=128)
    new_password_confirm: str


class UpdateBody(BaseModel):
    image: str | None = Field(default=None, description="目标镜像；Unraid 可写回模板 Repository")


class ComposeUpdateBody(BaseModel):
    service: str | None = Field(default=None, description="仅更新指定 service；空则整个项目")
    recreate: bool = True


class UnraidUpdateBody(BaseModel):
    repository: str | None = Field(default=None, description="新镜像，写入模板 Repository")
    recreate: bool = True


class PullBody(BaseModel):
    image: str = Field(..., description="镜像名:tag")


class EndpointBody(BaseModel):
    name: str = Field(..., min_length=1, max_length=64)
    docker_host: str = Field(..., min_length=3, max_length=512)
    tls_enabled: bool = False
    tls_ca: str = ""
    tls_cert: str = ""
    tls_key: str = ""
    verify_tls: bool = True
    is_default: bool = False
    notes: str = ""
    enabled: bool | None = None


class EndpointPatchBody(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=64)
    docker_host: str | None = Field(default=None, min_length=3, max_length=512)
    tls_enabled: bool | None = None
    tls_ca: str | None = None
    tls_cert: str | None = None
    tls_key: str | None = None
    verify_tls: bool | None = None
    is_default: bool | None = None
    notes: str | None = None
    enabled: bool | None = None


class NetworkCreateBody(BaseModel):
    name: str
    driver: str = "bridge"
    internal: bool = False
    attachable: bool = False


class VolumeCreateBody(BaseModel):
    name: str
    driver: str = "local"


class PruneBody(BaseModel):
    containers: bool = True
    images: bool = True
    volumes: bool = False
    networks: bool = True
    dangling_images_only: bool = True


class RemoveBody(BaseModel):
    force: bool = False
    volumes: bool = False


class BatchBody(BaseModel):
    action: str = Field(..., description="start|stop|restart|pause|unpause|kill|remove")
    ids: list[str] = Field(default_factory=list)
    force: bool = False
    volumes: bool = False


class RenameBody(BaseModel):
    name: str = Field(..., min_length=1, max_length=128)


class NetworkAttachBody(BaseModel):
    network: str = Field(..., min_length=1, max_length=128)
    aliases: list[str] | None = None
    force: bool = False


class PrefsBody(BaseModel):
    particles: bool | None = None
    particles_count: int | None = Field(default=None, ge=20, le=300)
    bg_theme: str | None = None  # cyber | deep | aurora | plain
    card_density: str | None = None  # comfortable | compact
    reduce_motion: bool | None = None


class SystemSettingsBody(BaseModel):
    """Proxy + auto-update settings (not visual prefs)."""
    http_proxy: str | None = None
    https_proxy: str | None = None
    no_proxy: str | None = None
    auto_check_enabled: bool | None = None
    auto_check_interval_hours: int | None = Field(default=None, ge=1, le=48)


class RemoteSettingsBody(BaseModel):
    enabled: bool | None = None
    role: str | None = None  # controller | agent | ""
    agent_mode: str | None = None  # collab | managed
    display_name: str | None = None
    controller_base_url: str | None = None
    public_base_url: str | None = None


class RemotePairBody(BaseModel):
    """Agent generates pair (v0.8)."""
    public_base_url: str | None = None
    mode: str | None = None  # collab | managed
    agent_name: str | None = None
    display_name: str | None = None


class RemoteControllerConnectBody(BaseModel):
    """Controller pastes pair code only (URL embedded in dop1)."""
    pair_code: str = Field(..., min_length=8, max_length=2048)
    controller_name: str | None = None


class RemoteSessionModeBody(BaseModel):
    mode: str = Field(..., description="collab | managed | mode_pick")


META_SYSTEM_PROXY = "system_proxy"
META_ACTIVE_REMOTE = "active_remote_endpoint"


def _load_stored_proxy() -> dict[str, str]:
    raw = get_meta(META_SYSTEM_PROXY)
    if not raw:
        return {"http": "", "https": "", "no_proxy": ""}
    try:
        data = json.loads(raw)
        if not isinstance(data, dict):
            return {"http": "", "https": "", "no_proxy": ""}
        return {
            "http": str(data.get("http") or ""),
            "https": str(data.get("https") or ""),
            "no_proxy": str(data.get("no_proxy") or ""),
        }
    except Exception:
        return {"http": "", "https": "", "no_proxy": ""}


def _resolve_proxy_view() -> dict[str, Any]:
    """
    Env-at-boot wins (locked UI). Otherwise SQLite system_proxy is source of truth.
    Runtime process env may mirror stored values after apply — still source=stored.
    """
    capture_proxy_env_lock_at_boot()
    locked = env_proxy_locked()
    env_p = read_process_proxy()
    stored = _load_stored_proxy()
    if locked:
        return {
            "http": env_p.get("http") or "",
            "https": env_p.get("https") or "",
            "no_proxy": env_p.get("no_proxy") or "",
            "source": "env",
            "env_locked": True,
        }
    if any(stored.values()):
        return {
            "http": stored.get("http") or "",
            "https": stored.get("https") or "",
            "no_proxy": stored.get("no_proxy") or "",
            "source": "stored",
            "env_locked": False,
        }
    # no stored and not boot-locked — show empty (ignore leftover process env)
    return {
        "http": "",
        "https": "",
        "no_proxy": "",
        "source": "none",
        "env_locked": False,
    }


def _apply_stored_proxy_at_startup() -> None:
    """If no container-env proxy at boot, re-apply SQLite proxy into process env."""
    capture_proxy_env_lock_at_boot()
    if env_proxy_locked():
        return
    stored = _load_stored_proxy()
    if any(stored.values()):
        apply_proxy_to_environ(
            http=stored.get("http") or "",
            https=stored.get("https") or "",
            no_proxy=stored.get("no_proxy") or "",
        )


def _takeover_or_403() -> None:
    try:
        get_settings().takeover_guard()
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e)) from e


def _resource_or_403() -> None:
    if not get_settings().resource_apis:
        raise HTTPException(status_code=403, detail="资源 API 已关闭（DOCKEROPS_RESOURCE_APIS=false）")


def _active_ep() -> dict[str, Any]:
    eid = get_active_endpoint_id()
    ep = get_endpoint(eid)
    if not ep:
        from db import get_default_endpoint

        ep = get_default_endpoint()
    return ep


def _require_local_feature(feature: str) -> dict[str, Any]:
    ep = _active_ep()
    try:
        require_local_endpoint(ep, feature)
    except PermissionError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return ep


def _perm_http(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e)) from e
    except KeyError:
        raise HTTPException(status_code=404, detail="资源不存在") from None


def _test_endpoint_conn(ep: dict[str, Any]) -> dict[str, Any]:
    """Ping an endpoint without permanently polluting cache if it fails badly."""
    invalidate_client(ep["id"])
    token = set_request_endpoint(ep["id"])
    try:
        c = get_client(ep["id"])
        ok = c.ping()
        version = c.version()
        return {
            "ok": bool(ok),
            "api_version": version.get("ApiVersion"),
            "engine_version": version.get("Version"),
            "os": version.get("Os"),
            "arch": version.get("Arch"),
        }
    except Exception as e:
        invalidate_client(ep["id"])
        return {"ok": False, "error": str(e)}
    finally:
        reset_request_endpoint(token)


def _is_remote_api_path(path: str) -> bool:
    """Paths that can be proxied to a dialed-in agent."""
    if not path.startswith("/api/"):
        return False
    # never proxy remote-control / auth / static admin APIs
    skip_prefixes = (
        "/api/remote",
        "/api/auth",
        "/api/endpoints",
        "/api/system/settings",
        "/api/prefs",
        "/api/setup",
        "/api/login",
        "/api/logout",
        "/api/changelog",
        "/api/version",
        "/api/compose",
        "/api/unraid",
    )
    return not any(path == p or path.startswith(p + "/") for p in skip_prefixes)


@app.middleware("http")
async def endpoint_context_middleware(request: Request, call_next):
    """Bind X-DockerOps-Endpoint / ?endpoint= for the request lifetime.

    Remote agent sessions use id ``remote:{session_id}`` and are proxied over
    the agent dial-out WebSocket (no Docker Engine port).
    """
    # Managed lock: block local UI mutations on agent (controller RPC bypasses this —
    # RPC runs inside execute_local_rpc on agent without going through this path as client).
    # Local browser → agent HTTP still hits this middleware.
    try:
        if managed_blocks_local_path(request.method, request.url.path):
            return JSONResponse(
                status_code=423,
                content={
                    "ok": False,
                    "detail": "本机处于远程托管锁定：容器/镜像等写操作仅能由主控端发起。可在主控切换为「协同」或断开远程后本地操作。",
                    "managed_locked": True,
                },
            )
    except Exception:
        pass

    eid = (
        request.headers.get("X-DockerOps-Endpoint")
        or request.query_params.get("endpoint")
        or ""
    ).strip() or None

    # Persist active remote selection in meta when client sends remote:* header
    if eid and parse_remote_endpoint_id(eid):
        try:
            set_meta(META_ACTIVE_REMOTE, eid)
        except Exception:
            pass
    elif eid:
        # local/tcp endpoint selection clears remote active marker for this request path
        pass

    remote_sid = parse_remote_endpoint_id(eid) if eid else None
    if remote_sid:
        # Do not bind docker client; proxy instead for supported API paths
        if (
            request.method in ("GET", "POST", "PUT", "PATCH", "DELETE")
            and _is_remote_api_path(request.url.path)
        ):
            try:
                body = None
                if request.method in ("POST", "PUT", "PATCH"):
                    raw = await request.body()
                    if raw:
                        try:
                            body = json.loads(raw)
                        except Exception:
                            body = {"_raw": raw.decode("utf-8", errors="replace")}
                query = {k: v for k, v in request.query_params.multi_items()}
                result = await rpc_to_agent(
                    remote_sid,
                    method=request.method,
                    path=request.url.path,
                    body=body,
                    query=query,
                )
                # agent replies: {type,id,ok,status,body}
                if isinstance(result, dict) and "body" in result:
                    payload = result.get("body")
                    status = int(result.get("status") or (200 if result.get("ok", True) else 500))
                elif isinstance(result, dict):
                    status = int(result.get("status") or (200 if result.get("ok", True) else 500))
                    payload = {k: v for k, v in result.items() if k != "status"}
                else:
                    payload, status = result, 200
                if isinstance(payload, dict) and payload.get("status") and "ok" in payload:
                    # execute_local_rpc may embed HTTP-ish status
                    try:
                        st2 = int(payload.get("status"))
                        if 400 <= st2 < 600:
                            status = st2
                    except Exception:
                        pass
                resp = JSONResponse(content=payload, status_code=status if 100 <= status < 600 else 502)
                resp.headers["X-DockerOps-Endpoint"] = eid
                resp.headers["X-DockerOps-Remote"] = "1"
                return resp
            except RuntimeError as e:
                return JSONResponse(
                    status_code=502,
                    content={"ok": False, "detail": str(e)},
                    headers={"X-DockerOps-Endpoint": eid, "X-DockerOps-Remote": "1"},
                )
            except Exception as e:
                return JSONResponse(
                    status_code=502,
                    content={"ok": False, "detail": f"远程代理失败: {e}"},
                    headers={"X-DockerOps-Endpoint": eid, "X-DockerOps-Remote": "1"},
                )
        # non-proxied path with remote endpoint: fall through without docker bind
        token = set_request_endpoint(None)
        try:
            response = await call_next(request)
            response.headers["X-DockerOps-Endpoint"] = eid
            response.headers["X-DockerOps-Remote"] = "1"
            return response
        finally:
            reset_request_endpoint(token)

    if eid:
        ep = get_endpoint(eid)
        if not ep or not ep.get("enabled"):
            eid = None
    if not eid:
        try:
            # prefer stored remote active only if still online
            stored_remote = get_meta(META_ACTIVE_REMOTE)
            if stored_remote and parse_remote_endpoint_id(stored_remote):
                sid = parse_remote_endpoint_id(stored_remote)
                if sid and is_agent_online(sid):
                    # Re-enter remote path via header would need client; use local default here
                    pass
            eid = get_active_endpoint_id()
        except Exception:
            eid = None
    token = set_request_endpoint(eid)
    try:
        response = await call_next(request)
        if eid:
            response.headers["X-DockerOps-Endpoint"] = eid
        return response
    finally:
        reset_request_endpoint(token)


@app.on_event("startup")
def _startup() -> None:
    settings.ensure_dirs()
    try:
        from db import ensure_default_endpoint

        ensure_default_endpoint()
    except Exception:
        pass
    refresh_template_name_cache()
    boot = bootstrap_from_env()
    try:
        _apply_stored_proxy_at_startup()
    except Exception:
        pass
    plat = {}
    try:
        plat = platform_info()
    except Exception as e:
        plat = {"error": str(e)}
    active = {}
    try:
        active = _active_ep()
    except Exception:
        pass
    audit(
        "startup",
        actor="system",
        detail={
            "version": VERSION,
            "takeover_enabled": settings.takeover_enabled,
            "resource_apis": settings.resource_apis,
            "platform": plat.get("platform"),
            "unraid_templates": templates_available(),
            "bootstrap": boot,
            "needs_setup": auth_status().get("needs_setup"),
            "update_auto": get_update_auto_settings(),
            "proxy_source": _resolve_proxy_view().get("source"),
            "active_endpoint": active.get("name"),
            "active_docker_host": active.get("docker_host"),
        },
    )
    try:
        start_update_auto_check_thread()
    except Exception:
        pass
    # Resume controller outbound dials to saved agents (v0.8)
    try:
        resume_controller_sessions()
    except Exception:
        pass


@app.get("/api/health")
def api_health() -> dict[str, Any]:
    engine = ping()
    try:
        plat = platform_info()
        platform_name = plat.get("platform")
    except Exception:
        platform_name = "unknown"
    active = {}
    try:
        active = _active_ep()
    except Exception:
        pass
    remote_snip: dict[str, Any] = {}
    try:
        rs = public_remote_status()
        remote_snip = {
            "enabled": rs.get("enabled"),
            "role": rs.get("role"),
            "status": rs.get("status"),
            "agent_mode": rs.get("agent_mode"),
            "managed_locked": rs.get("managed_locked"),
            "active_peer_name": rs.get("active_peer_name"),
            "online_count": rs.get("online_count"),
            "hint": rs.get("hint"),
        }
    except Exception:
        remote_snip = {"enabled": False}
    return {
        "ok": True,
        "service": "dockerops",
        "version": VERSION,
        "time": time.time(),
        "docker": engine,
        "platform": platform_name,
        "endpoint": {
            "id": active.get("id"),
            "name": active.get("name"),
            "docker_host": active.get("docker_host"),
            "is_local": is_local_host(active.get("docker_host") or ""),
            "capabilities": endpoint_capabilities(active) if active else {},
        },
        "takeover_enabled": get_settings().takeover_enabled,
        "resource_apis": get_settings().resource_apis,
        "unraid_templates_available": templates_available(),
        "remote": remote_snip,
        "managed_locked": bool(remote_snip.get("managed_locked")),
    }


@app.get("/api/platform")
def api_platform(actor: OptionalUser = None) -> dict[str, Any]:
    info = platform_info()
    info["viewer"] = actor
    return info


@app.get("/api/managers/summary")
def api_managers_summary(actor: OptionalUser = None) -> dict[str, Any]:
    try:
        items = list_containers(all_containers=True)
    except Exception as e:
        items = []
        summary = managers_summary([])
        summary["docker_error"] = str(e)
        summary["viewer"] = actor
        return summary
    summary = managers_summary(items)
    summary["viewer"] = actor
    return summary


@app.get("/api/auth/status")
def api_auth_status() -> dict[str, Any]:
    return auth_status()


@app.get("/api/auth/me")
def api_auth_me(actor: AuthUser) -> dict[str, Any]:
    """Validate session token; used by Web 登录门."""
    return {
        "ok": True,
        "username": actor,
        "auth_store": "sqlite",
        "authenticated": True,
    }


@app.post("/api/auth/setup")
def api_auth_setup(body: SetupBody) -> dict[str, Any]:
    if body.password != body.password_confirm:
        raise HTTPException(status_code=400, detail="两次输入的密码不一致")
    return complete_setup(body.username, body.password)


@app.post("/api/auth/login")
def api_login(body: LoginBody) -> dict[str, Any]:
    return login(body.username, body.password)


@app.post("/api/auth/logout")
def api_logout(request: Request, actor: OptionalUser) -> dict[str, Any]:
    auth = request.headers.get("Authorization") or ""
    if auth.lower().startswith("bearer "):
        logout(auth.split(" ", 1)[1].strip())
    return {"ok": True, "actor": actor}


@app.post("/api/auth/change-password")
def api_change_password(body: ChangePasswordBody, actor: AuthUser) -> dict[str, Any]:
    if body.new_password != body.new_password_confirm:
        raise HTTPException(status_code=400, detail="两次输入的新密码不一致")
    if actor == "api-token":
        raise HTTPException(status_code=400, detail="API Token 不能修改密码，请使用管理员账号登录")
    return change_password(actor, body.old_password, body.new_password)


@app.get("/api/containers")
def api_containers(actor: OptionalUser) -> dict[str, Any]:
    try:
        items = list_containers(all_containers=True)
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"Docker 不可用：{e}") from e
    return {"ok": True, "count": len(items), "items": items, "viewer": actor}


@app.post("/api/containers/batch")
def api_batch(body: BatchBody, actor: AuthUser) -> dict[str, Any]:
    """Static path registered before {container_id} routes."""
    _resource_or_403()
    if not body.ids:
        raise HTTPException(status_code=400, detail="请选择至少一个容器")
    return _perm_http(
        batch_lifecycle,
        body.action,
        body.ids,
        actor=actor,
        force=body.force,
        volumes=body.volumes,
    )


@app.get("/api/containers/{container_id}")
def api_container(container_id: str, actor: OptionalUser) -> dict[str, Any]:
    try:
        item = get_container(container_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="容器不存在") from None
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"Docker 不可用：{e}") from e
    return {"ok": True, "item": item, "viewer": actor}


# ── Lifecycle ──────────────────────────────────────────────


@app.post("/api/containers/{container_id}/start")
def api_start(container_id: str, actor: AuthUser) -> dict[str, Any]:
    _resource_or_403()
    return _perm_http(lifecycle, "start", container_id, actor=actor)


@app.post("/api/containers/{container_id}/stop")
def api_stop(container_id: str, actor: AuthUser) -> dict[str, Any]:
    _resource_or_403()
    return _perm_http(lifecycle, "stop", container_id, actor=actor)


@app.post("/api/containers/{container_id}/restart")
def api_restart(container_id: str, actor: AuthUser) -> dict[str, Any]:
    _resource_or_403()
    return _perm_http(lifecycle, "restart", container_id, actor=actor)


@app.post("/api/containers/{container_id}/pause")
def api_pause(container_id: str, actor: AuthUser) -> dict[str, Any]:
    _resource_or_403()
    return _perm_http(lifecycle, "pause", container_id, actor=actor)


@app.post("/api/containers/{container_id}/unpause")
def api_unpause(container_id: str, actor: AuthUser) -> dict[str, Any]:
    _resource_or_403()
    return _perm_http(lifecycle, "unpause", container_id, actor=actor)


@app.post("/api/containers/{container_id}/kill")
def api_kill(container_id: str, actor: AuthUser) -> dict[str, Any]:
    _resource_or_403()
    return _perm_http(lifecycle, "kill", container_id, actor=actor)


@app.delete("/api/containers/{container_id}")
def api_remove_container(
    container_id: str,
    actor: AuthUser,
    force: bool = False,
    volumes: bool = False,
) -> dict[str, Any]:
    _resource_or_403()
    return _perm_http(lifecycle, "remove", container_id, actor=actor, force=force, volumes=volumes)


@app.post("/api/containers/{container_id}/rename")
def api_rename(container_id: str, body: RenameBody, actor: AuthUser) -> dict[str, Any]:
    _resource_or_403()
    return _perm_http(lifecycle, "rename", container_id, actor=actor, name=body.name.strip())


@app.get("/api/activity")
def api_activity(actor: OptionalUser, limit: int = Query(12, ge=1, le=40)) -> dict[str, Any]:
    _resource_or_403()
    try:
        data = activity_stats(limit=limit)
        data["viewer"] = actor
        return data
    except Exception as e:
        raise HTTPException(status_code=503, detail=str(e)) from e


@app.get("/api/containers/{container_id}/stats")
def api_container_stats(container_id: str, actor: OptionalUser) -> dict[str, Any]:
    _resource_or_403()
    try:
        from docker_client import container_stats

        item = container_stats(container_id)
        return {"ok": True, "item": item, "viewer": actor}
    except KeyError:
        raise HTTPException(status_code=404, detail="容器不存在") from None
    except Exception as e:
        raise HTTPException(status_code=503, detail=str(e)) from e


@app.get("/api/images/{image_id:path}/history")
def api_image_history(image_id: str, actor: OptionalUser) -> dict[str, Any]:
    _resource_or_403()
    try:
        items = image_history(image_id)
        return {"ok": True, "count": len(items), "items": items, "viewer": actor}
    except Exception as e:
        raise HTTPException(status_code=503, detail=str(e)) from e


@app.get("/api/changelog")
def api_changelog(actor: OptionalUser = None) -> dict[str, Any]:
    return {"ok": True, "version": VERSION, "items": CHANGELOG, "viewer": actor}


def _default_prefs() -> dict[str, Any]:
    return {
        "particles": True,
        "particles_count": 90,
        "bg_theme": "cyber",
        "card_density": "comfortable",
        "reduce_motion": False,
    }


def _load_prefs() -> dict[str, Any]:
    import json

    raw = get_meta("ui_prefs")
    prefs = _default_prefs()
    if raw:
        try:
            data = json.loads(raw)
            if isinstance(data, dict):
                prefs.update({k: v for k, v in data.items() if k in prefs})
        except Exception:
            pass
    return prefs


@app.get("/api/prefs")
def api_get_prefs(actor: OptionalUser = None) -> dict[str, Any]:
    return {"ok": True, "prefs": _load_prefs(), "viewer": actor}


@app.put("/api/prefs")
def api_put_prefs(body: PrefsBody, actor: AuthUser) -> dict[str, Any]:
    import json

    prefs = _load_prefs()
    patch = body.model_dump(exclude_none=True)
    allowed_themes = {"cyber", "deep", "aurora", "plain"}
    allowed_density = {"comfortable", "compact"}
    if "bg_theme" in patch and patch["bg_theme"] not in allowed_themes:
        raise HTTPException(status_code=400, detail="无效背景主题")
    if "card_density" in patch and patch["card_density"] not in allowed_density:
        raise HTTPException(status_code=400, detail="无效卡片密度")
    prefs.update(patch)
    set_meta("ui_prefs", json.dumps(prefs, ensure_ascii=False))
    audit("ui_prefs_update", actor=actor or "unknown", detail=prefs)
    return {"ok": True, "prefs": prefs, "message": "个性化设置已保存"}


# ── Logs ───────────────────────────────────────────────────


@app.get("/api/containers/{container_id}/logs")
def api_logs(
    container_id: str,
    actor: AuthUser,
    tail: int = Query(200, ge=1, le=10000),
    timestamps: bool = True,
    follow: bool = False,
    since: int | None = None,
):
    _resource_or_403()
    if follow:
        return StreamingResponse(
            sse_log_events(container_id, tail=min(tail, 500), timestamps=timestamps),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )
    try:
        result = get_logs(container_id, tail=tail, timestamps=timestamps, since=since)
        result["viewer"] = actor
        return result
    except KeyError:
        raise HTTPException(status_code=404, detail="容器不存在") from None
    except Exception as e:
        raise HTTPException(status_code=503, detail=str(e)) from e


# ── Images ─────────────────────────────────────────────────


@app.get("/api/images")
def api_images(actor: OptionalUser = None) -> dict[str, Any]:
    _resource_or_403()
    result = images_list()
    result["viewer"] = actor
    return result


@app.post("/api/images/pull")
def api_images_pull(body: PullBody, actor: AuthUser) -> dict[str, Any]:
    _resource_or_403()
    return _perm_http(images_pull, body.image, actor=actor)


@app.post("/api/images/prune")
def api_images_prune(actor: AuthUser, dangling: bool = True) -> dict[str, Any]:
    _resource_or_403()
    return _perm_http(images_prune, actor=actor, dangling=dangling)


@app.delete("/api/images/{image_id:path}")
def api_images_remove(image_id: str, actor: AuthUser, force: bool = False) -> dict[str, Any]:
    _resource_or_403()
    return _perm_http(images_remove, image_id, actor=actor, force=force)


# ── Networks ───────────────────────────────────────────────


@app.get("/api/networks")
def api_networks(actor: OptionalUser = None) -> dict[str, Any]:
    _resource_or_403()
    result = networks_list()
    result["viewer"] = actor
    return result


@app.post("/api/networks")
def api_networks_create(body: NetworkCreateBody, actor: AuthUser) -> dict[str, Any]:
    _resource_or_403()
    return _perm_http(
        networks_create,
        body.name,
        actor=actor,
        driver=body.driver,
        internal=body.internal,
        attachable=body.attachable,
    )


@app.delete("/api/networks/{network_id}")
def api_networks_remove(network_id: str, actor: AuthUser) -> dict[str, Any]:
    _resource_or_403()
    return _perm_http(networks_remove, network_id, actor=actor)


# ── Volumes ────────────────────────────────────────────────


@app.get("/api/volumes")
def api_volumes(actor: OptionalUser = None) -> dict[str, Any]:
    _resource_or_403()
    result = volumes_list()
    result["viewer"] = actor
    return result


@app.post("/api/volumes")
def api_volumes_create(body: VolumeCreateBody, actor: AuthUser) -> dict[str, Any]:
    _resource_or_403()
    return _perm_http(volumes_create, body.name, actor=actor, driver=body.driver)


@app.post("/api/volumes/prune")
def api_volumes_prune(actor: AuthUser) -> dict[str, Any]:
    _resource_or_403()
    return _perm_http(volumes_prune, actor=actor)


@app.delete("/api/volumes/{name}")
def api_volumes_remove(name: str, actor: AuthUser, force: bool = False) -> dict[str, Any]:
    _resource_or_403()
    return _perm_http(volumes_remove, name, actor=actor, force=force)


# ── System / Events ────────────────────────────────────────


@app.get("/api/system/info")
def api_system_info(actor: OptionalUser = None) -> dict[str, Any]:
    _resource_or_403()
    result = sys_info()
    result["viewer"] = actor
    return result


@app.get("/api/system/df")
def api_system_df(actor: OptionalUser = None) -> dict[str, Any]:
    _resource_or_403()
    result = sys_df()
    result["viewer"] = actor
    return result


@app.post("/api/system/prune")
def api_system_prune(body: PruneBody, actor: AuthUser) -> dict[str, Any]:
    _resource_or_403()
    return _perm_http(
        sys_prune,
        actor=actor,
        containers=body.containers,
        images=body.images,
        volumes=body.volumes,
        networks=body.networks,
        dangling_images_only=body.dangling_images_only,
    )


@app.get("/api/events")
def api_events(
    actor: AuthUser,
    limit: int = Query(50, ge=1, le=500),
    since_seconds: int = Query(3600, ge=60, le=86400),
    follow: bool = False,
):
    _resource_or_403()
    if follow:
        return StreamingResponse(
            sse_docker_events(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )
    result = recent_events(limit=limit, since_seconds=since_seconds)
    result["viewer"] = actor
    return result


@app.post("/api/containers/{container_id}/networks/connect")
def api_container_net_connect(
    container_id: str, body: NetworkAttachBody, actor: AuthUser
) -> dict[str, Any]:
    _resource_or_403()
    net = (body.network or "").strip()
    if not net:
        raise HTTPException(status_code=400, detail="network 不能为空")
    try:
        result = connect_network(container_id, net, aliases=body.aliases)
        audit(
            "network_connect",
            actor=actor or "unknown",
            detail={"container": container_id, "network": net, "aliases": body.aliases},
        )
        return {**result, "message": f"已连接到网络 {net}", "viewer": actor}
    except KeyError:
        raise HTTPException(status_code=404, detail="容器或网络不存在") from None
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@app.post("/api/containers/{container_id}/networks/disconnect")
def api_container_net_disconnect(
    container_id: str, body: NetworkAttachBody, actor: AuthUser
) -> dict[str, Any]:
    _resource_or_403()
    net = (body.network or "").strip()
    if not net:
        raise HTTPException(status_code=400, detail="network 不能为空")
    try:
        result = disconnect_network(container_id, net, force=body.force)
        audit(
            "network_disconnect",
            actor=actor or "unknown",
            detail={"container": container_id, "network": net, "force": body.force},
        )
        return {**result, "message": f"已从网络 {net} 断开", "viewer": actor}
    except KeyError:
        raise HTTPException(status_code=404, detail="容器或网络不存在") from None
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@app.websocket("/api/containers/{container_id}/console")
async def ws_container_console(websocket: WebSocket, container_id: str) -> None:
    """
    Interactive docker exec TTY over WebSocket.
    Auth: ?token= session bearer (browsers cannot set WS Authorization easily).
    Query: shell=sh|bash|ash, cols, rows
    Client → server: text input, or JSON {"type":"resize","cols":N,"rows":N}
    Server → client: binary/text stdout stream
    """
    await websocket.accept()
    settings = get_settings()
    if not settings.resource_apis:
        await websocket.close(code=4403, reason="资源 API 已关闭")
        return
    if not settings.console_enabled:
        await websocket.close(code=4403, reason="控制台未启用（DOCKEROPS_CONSOLE_ENABLED）")
        return

    token = websocket.query_params.get("token") or ""
    actor = actor_from_token(token)
    if not actor:
        await websocket.close(code=4401, reason="需要登录")
        return

    # Pin Docker endpoint for this console session (avoid cross-engine exec)
    ep_q = (websocket.query_params.get("endpoint") or "").strip()
    ep_row = get_endpoint(ep_q) if ep_q else None
    if ep_row and ep_row.get("enabled"):
        ep_id = ep_q
    else:
        try:
            ep_id = get_active_endpoint_id()
        except Exception:
            ep_id = None
    ep_token = set_request_endpoint(ep_id)

    shell = websocket.query_params.get("shell") or "sh"
    try:
        cols = max(20, min(int(websocket.query_params.get("cols") or 120), 400))
        rows = max(5, min(int(websocket.query_params.get("rows") or 30), 120))
    except Exception:
        cols, rows = 120, 30

    cmd = resolve_shell_cmd(shell)
    sock = None
    raw = None
    exec_id = None
    idle_sec = 30 * 60
    last_activity = time.time()
    loop = asyncio.get_event_loop()

    try:
        # Keep event loop free: docker-py calls are blocking (and resize-before-start
        # can hang until Docker times out the exec session).
        created = await loop.run_in_executor(
            None, lambda: exec_create(container_id, cmd=cmd, tty=True, stdin=True)
        )
        exec_id = created.get("Id")
        if not exec_id:
            await websocket.send_text("\r\n[error] exec_create 失败\r\n")
            await websocket.close(code=1011)
            return

        sock, raw = await loop.run_in_executor(None, lambda: exec_start_socket(exec_id))
        # Prefer short timeouts over non-blocking to avoid platform-specific BlockingIOError loops
        if hasattr(raw, "settimeout"):
            try:
                raw.settimeout(0.25)
            except Exception:
                pass
        elif hasattr(raw, "setblocking"):
            try:
                raw.setblocking(False)
            except Exception:
                pass

        # Resize only works after the exec session has started.
        try:
            await loop.run_in_executor(
                None, lambda: exec_resize(exec_id, height=rows, width=cols)
            )
        except Exception:
            pass

        await loop.run_in_executor(
            None,
            lambda: audit(
                "console_open",
                actor=actor,
                detail={
                    "container": container_id,
                    "shell": shell,
                    "cmd": cmd,
                    "cols": cols,
                    "rows": rows,
                },
            ),
        )

        async def pump_docker_to_ws() -> None:
            nonlocal last_activity
            while True:
                try:
                    data = await loop.run_in_executor(None, lambda: raw.recv(4096))
                except BlockingIOError:
                    await asyncio.sleep(0.02)
                    continue
                except TimeoutError:
                    if time.time() - last_activity > idle_sec:
                        break
                    continue
                except Exception as ex:
                    # socket.timeout on some platforms
                    if "timed out" in str(ex).lower():
                        if time.time() - last_activity > idle_sec:
                            break
                        continue
                    break
                if not data:
                    break
                last_activity = time.time()
                try:
                    if isinstance(data, bytes):
                        await websocket.send_bytes(data)
                    else:
                        await websocket.send_text(str(data))
                except Exception:
                    break

        async def pump_ws_to_docker() -> None:
            nonlocal last_activity, cols, rows
            while True:
                try:
                    msg = await asyncio.wait_for(websocket.receive(), timeout=2.0)
                except asyncio.TimeoutError:
                    if time.time() - last_activity > idle_sec:
                        break
                    continue
                except WebSocketDisconnect:
                    break
                except Exception:
                    break
                last_activity = time.time()
                if msg.get("type") == "websocket.disconnect":
                    break
                if "bytes" in msg and msg["bytes"] is not None:
                    try:
                        await loop.run_in_executor(None, lambda b=msg["bytes"]: raw.sendall(b))
                    except Exception:
                        break
                elif "text" in msg and msg["text"] is not None:
                    text = msg["text"]
                    # resize control frame
                    if text.startswith("{") and '"type"' in text:
                        try:
                            payload = json.loads(text)
                            if payload.get("type") == "resize":
                                cols = max(20, min(int(payload.get("cols") or cols), 400))
                                rows = max(5, min(int(payload.get("rows") or rows), 120))
                                if exec_id:
                                    await loop.run_in_executor(
                                        None, lambda: exec_resize(exec_id, height=rows, width=cols)
                                    )
                                continue
                        except Exception:
                            pass
                    try:
                        await loop.run_in_executor(
                            None, lambda t=text: raw.sendall(t.encode("utf-8", errors="replace"))
                        )
                    except Exception:
                        break

        await asyncio.gather(pump_docker_to_ws(), pump_ws_to_docker())
    except Exception as e:
        try:
            await websocket.send_text(f"\r\n[error] {e}\r\n")
        except Exception:
            pass
    finally:
        try:
            await loop.run_in_executor(
                None,
                lambda: audit(
                    "console_close",
                    actor=actor or "unknown",
                    detail={
                        "container": container_id,
                        "shell": shell,
                        "exec_id": (exec_id or "")[:24],
                        "endpoint": ep_id,
                    },
                ),
            )
        except Exception:
            pass
        try:
            if sock is not None:
                closer = getattr(sock, "close", None)
                if callable(closer):
                    closer()
        except Exception:
            pass
        try:
            reset_request_endpoint(ep_token)
        except Exception:
            pass
        try:
            if raw is not None and raw is not sock:
                closer = getattr(raw, "close", None)
                if callable(closer):
                    closer()
        except Exception:
            pass
        try:
            await websocket.close()
        except Exception:
            pass


@app.get("/api/doctor")
def api_doctor(actor: OptionalUser) -> dict[str, Any]:
    result = diagnose_all()
    result["viewer"] = actor
    return result


@app.get("/api/doctor/{container_id}")
def api_doctor_one(container_id: str, actor: OptionalUser) -> dict[str, Any]:
    result = diagnose_one(container_id)
    if not result.get("ok", True) and result.get("error") == "container_not_found":
        raise HTTPException(status_code=404, detail=result.get("message"))
    result["viewer"] = actor
    return result


@app.get("/api/monitor/report")
def api_monitor(refresh: bool = False, actor: OptionalUser = None) -> dict[str, Any]:
    report = collect_report(persist=True) if refresh else get_latest_or_collect()
    report["viewer"] = actor
    return report


@app.get("/api/ops/records")
def api_ops_records(limit: int = 100, actor: OptionalUser = None) -> dict[str, Any]:
    return {"ok": True, "items": records(limit=min(max(limit, 1), 500)), "viewer": actor}


@app.post("/api/ops/backup/{container_id}")
def api_backup(container_id: str, actor: AuthUser) -> dict[str, Any]:
    return backup_container(container_id, actor=actor)


@app.post("/api/ops/update/{container_id}")
def api_update(container_id: str, body: UpdateBody, actor: AuthUser) -> dict[str, Any]:
    return safe_update(container_id, image=body.image, actor=actor)


@app.post("/api/ops/update/{container_id}/stream")
def api_update_stream(container_id: str, body: UpdateBody, actor: AuthUser) -> StreamingResponse:
    """SSE: stage + pull layer progress for a single container safe-update."""
    import queue
    import threading

    q: queue.Queue = queue.Queue()

    def on_progress(payload: dict[str, Any]) -> None:
        q.put(payload)

    def worker() -> None:
        try:
            result = safe_update(
                container_id, image=body.image, actor=actor, on_progress=on_progress
            )
            q.put(
                {
                    "event": "done",
                    "ok": bool(result.get("ok")),
                    "message": result.get("message") or "",
                    "result": {
                        "ok": result.get("ok"),
                        "manager": result.get("manager"),
                        "partial": result.get("partial"),
                    },
                }
            )
        except Exception as e:
            q.put({"event": "error", "message": str(e)})
            q.put({"event": "done", "ok": False, "message": str(e)})
        finally:
            q.put(None)

    threading.Thread(target=worker, daemon=True).start()

    def gen():
        last_ping = time.time()
        while True:
            try:
                item = q.get(timeout=1.0)
            except queue.Empty:
                if time.time() - last_ping > 12:
                    yield ": ping\n\n"
                    last_ping = time.time()
                continue
            if item is None:
                break
            yield f"data: {json.dumps(item, ensure_ascii=False)}\n\n"
            last_ping = time.time()

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/api/ops/rollback/{container_id}")
def api_rollback(container_id: str, actor: AuthUser) -> dict[str, Any]:
    return rollback_guide(container_id, actor=actor)


class DetectBody(BaseModel):
    container_ids: list[str] | None = None
    only_running: bool = False


class OneClickUpdateBody(BaseModel):
    container_ids: list[str] | None = None
    only_available: bool = True
    only_running: bool = False


@app.post("/api/ops/detect-updates")
def api_detect_updates(body: DetectBody, actor: AuthUser) -> dict[str, Any]:
    """强制检测：比对本地与仓库镜像 digest，并写入状态缓存。"""
    return detect_updates(
        container_ids=body.container_ids,
        only_running=body.only_running,
        actor=actor,
        persist=True,
    )


@app.get("/api/ops/detect-updates")
def api_detect_updates_get(
    only_running: bool = False,
    actor: AuthUser = ...,
) -> dict[str, Any]:
    return detect_updates(only_running=only_running, actor=actor, persist=True)


@app.get("/api/ops/update-status")
def api_update_status(actor: OptionalUser = None) -> dict[str, Any]:
    """
    廉价读取上次检测缓存（无 registry 调用），供容器列表/总览徽标。
    Unraid 风格：不必每次手动点检测。
    """
    cache = get_cached_update_status()
    auto = get_update_auto_settings()
    return {
        "ok": True,
        "viewer": actor,
        "auto": auto,
        **cache,
    }


@app.post("/api/ops/one-click-update")
def api_one_click_update(body: OneClickUpdateBody, actor: AuthUser) -> dict[str, Any]:
    """一键更新：先检测，再按管理源安全更新（Compose/Unraid 模板/三方仅拉镜像）。"""
    return one_click_update(
        container_ids=body.container_ids,
        only_available=body.only_available,
        only_running=body.only_running,
        actor=actor,
    )


@app.post("/api/ops/one-click-update/stream")
def api_one_click_update_stream(body: OneClickUpdateBody, actor: AuthUser) -> StreamingResponse:
    """SSE: batch safe-update with per-item stage/pull progress."""
    import queue
    import threading

    q: queue.Queue = queue.Queue()

    def on_progress(payload: dict[str, Any]) -> None:
        q.put(payload)

    def worker() -> None:
        try:
            # one_click_update emits its own "done" via on_progress
            one_click_update(
                container_ids=body.container_ids,
                only_available=body.only_available,
                only_running=body.only_running,
                actor=actor,
                on_progress=on_progress,
            )
        except Exception as e:
            q.put({"event": "error", "message": str(e)})
            q.put({"event": "done", "ok": False, "message": str(e)})
        finally:
            q.put(None)

    threading.Thread(target=worker, daemon=True).start()

    def gen():
        last_ping = time.time()
        while True:
            try:
                item = q.get(timeout=1.0)
            except queue.Empty:
                if time.time() - last_ping > 12:
                    yield ": ping\n\n"
                    last_ping = time.time()
                continue
            if item is None:
                break
            yield f"data: {json.dumps(item, ensure_ascii=False)}\n\n"
            last_ping = time.time()

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/api/system/settings")
def api_get_system_settings(actor: OptionalUser = None) -> dict[str, Any]:
    """系统设置：代理 + 自动更新检测（非个性化 UI）。"""
    proxy = _resolve_proxy_view()
    auto = get_update_auto_settings()
    return {
        "ok": True,
        "viewer": actor,
        "proxy": proxy,
        "update_auto": auto,
        "hints": [
            "容器环境变量 HTTP_PROXY/HTTPS_PROXY/NO_PROXY 或 DOCKEROPS_*_PROXY 优先；设置页自动识别并锁定。",
            "镜像拉取与更新检测经 Docker 引擎出网；引擎侧代理需在 Unraid/宿主机配置，此处作用于 DockerOps 进程自身。",
        ],
    }


@app.put("/api/system/settings")
def api_put_system_settings(body: SystemSettingsBody, actor: AuthUser) -> dict[str, Any]:
    """保存系统代理（容器 env 未锁定时写 SQLite）与自动更新检测设置。"""
    messages: list[str] = []
    patch = body.model_dump(exclude_none=True)
    capture_proxy_env_lock_at_boot()

    # Auto-update settings (always writable)
    auto_patch: dict[str, Any] = {}
    if "auto_check_enabled" in patch:
        auto_patch["auto_check_enabled"] = patch["auto_check_enabled"]
    if "auto_check_interval_hours" in patch:
        auto_patch["auto_check_interval_hours"] = patch["auto_check_interval_hours"]
    auto = get_update_auto_settings()
    if auto_patch:
        auto = set_update_auto_settings(**auto_patch)
        messages.append("自动更新检测已保存")
        audit("update_auto_settings", actor=actor or "unknown", detail=auto)

    # Proxy: only container env-at-boot locks; Web values always go to SQLite app_meta
    proxy_keys = {"http_proxy", "https_proxy", "no_proxy"}
    if proxy_keys & set(patch.keys()):
        if env_proxy_locked():
            raise HTTPException(
                status_code=400,
                detail="代理已由容器环境变量配置（启动时），设置页只读。请修改容器变量 HTTP_PROXY/HTTPS_PROXY/NO_PROXY 或 DOCKEROPS_*_PROXY 后重建容器。",
            )
        base = _load_stored_proxy()
        if "http_proxy" in patch:
            base["http"] = str(patch["http_proxy"] or "").strip()
        if "https_proxy" in patch:
            base["https"] = str(patch["https_proxy"] or "").strip()
        if "no_proxy" in patch:
            base["no_proxy"] = str(patch["no_proxy"] or "").strip()
        payload = json.dumps(base, ensure_ascii=False)
        set_meta(META_SYSTEM_PROXY, payload)
        # verify round-trip so redeploy cannot silently lose proxy
        written = get_meta(META_SYSTEM_PROXY)
        if written != payload:
            raise HTTPException(status_code=500, detail="代理写入 SQLite 失败，请检查 /data 卷权限")
        apply_proxy_to_environ(http=base["http"], https=base["https"], no_proxy=base["no_proxy"])
        messages.append("代理已写入 SQLite 并应用到进程（重启后自动恢复）")
        audit("system_proxy_update", actor=actor or "unknown", detail={**base, "source": "stored", "persisted": True})

    proxy = _resolve_proxy_view()
    return {
        "ok": True,
        "proxy": proxy,
        "update_auto": auto,
        "message": "；".join(messages) if messages else "无变更",
    }


# ── Docker endpoints (multi-engine) ───────────────────────


@app.get("/api/endpoints")
def api_endpoints_list(actor: OptionalUser = None) -> dict[str, Any]:
    active_id = get_active_endpoint_id()
    # client may keep remote:* in localStorage; surface it if still valid
    stored_remote = get_meta(META_ACTIVE_REMOTE) or ""
    items = [public_endpoint(ep, active_id=active_id) for ep in list_endpoints()]
    # Merge remote agent sessions (controller outbound, v0.8)
    try:
        st = get_remote_settings()
        if st.get("enabled") and st.get("role") == "controller":
            try:
                from remote_controller import dial_online_map

                om = dial_online_map()
            except Exception:
                om = {}
            for s in list_controller_outbound_sessions():
                rid = remote_endpoint_id(s["session_id"])
                online = bool(s.get("online")) or bool(om.get(s["session_id"]))
                items.append(
                    {
                        "id": rid,
                        "name": f"远程 · {s.get('peer_name') or '节点'}",
                        "docker_host": s.get("docker_host") or f"remote://{s['session_id'][:8]}",
                        "tls_enabled": False,
                        "is_default": False,
                        "is_active": stored_remote == rid,
                        "is_local": False,
                        "enabled": True,
                        "kind": "remote_agent",
                        "online": online,
                        "mode": s.get("mode") or "collab",
                        "notes": "DockerOps 远程被控（主控拨入，无 Docker 端口）",
                        "capabilities": s.get("capabilities")
                        or {
                            "local": False,
                            "resources": True,
                            "console": False,
                            "compose": False,
                            "unraid": False,
                            "update_detect": True,
                            "remote": True,
                        },
                    }
                )
            if stored_remote and any(i["id"] == stored_remote for i in items):
                active_id = stored_remote
                for i in items:
                    i["is_active"] = i["id"] == active_id
    except Exception:
        pass
    return {
        "ok": True,
        "items": items,
        "active_id": active_id,
        "viewer": actor,
    }


@app.post("/api/endpoints")
def api_endpoints_create(body: EndpointBody, actor: AuthUser) -> dict[str, Any]:
    try:
        host = validate_docker_host(body.docker_host)
        ep = create_endpoint(
            name=body.name,
            docker_host=host,
            tls_enabled=body.tls_enabled,
            tls_ca=body.tls_ca or "",
            tls_cert=body.tls_cert or "",
            tls_key=body.tls_key or "",
            verify_tls=body.verify_tls,
            is_default=body.is_default,
            notes=body.notes or "",
        )
    except ValueError as e:
        code = str(e)
        msg = {
            "name_required": "名称不能为空",
            "docker_host_required": "Docker Host 不能为空",
            "name_exists": "名称已存在",
        }.get(code, code if "docker_host" in code or "格式" in code else str(e))
        raise HTTPException(status_code=400, detail=msg) from e
    audit("endpoint_create", actor=actor, detail={"id": ep["id"], "name": ep["name"], "host": ep["docker_host"]})
    active_id = get_active_endpoint_id()
    return {"ok": True, "item": public_endpoint(ep, active_id=active_id), "message": "端点已创建"}


@app.put("/api/endpoints/{endpoint_id}")
def api_endpoints_update(endpoint_id: str, body: EndpointPatchBody, actor: AuthUser) -> dict[str, Any]:
    data = body.model_dump(exclude_none=True)
    if "docker_host" in data:
        try:
            data["docker_host"] = validate_docker_host(data["docker_host"])
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
    try:
        ep = update_endpoint(endpoint_id, **data)
    except KeyError:
        raise HTTPException(status_code=404, detail="端点不存在") from None
    except ValueError as e:
        code = str(e)
        msg = {
            "name_required": "名称不能为空",
            "docker_host_required": "Docker Host 不能为空",
            "name_exists": "名称已存在",
        }.get(code, str(e))
        raise HTTPException(status_code=400, detail=msg) from e
    invalidate_client(endpoint_id)
    audit("endpoint_update", actor=actor, detail={"id": endpoint_id, "fields": list(data.keys())})
    active_id = get_active_endpoint_id()
    return {"ok": True, "item": public_endpoint(ep, active_id=active_id), "message": "端点已更新"}


@app.delete("/api/endpoints/{endpoint_id}")
def api_endpoints_delete(endpoint_id: str, actor: AuthUser) -> dict[str, Any]:
    try:
        delete_endpoint(endpoint_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="端点不存在") from None
    except ValueError as e:
        if str(e) == "cannot_delete_last":
            raise HTTPException(status_code=400, detail="不能删除最后一个端点") from e
        raise HTTPException(status_code=400, detail=str(e)) from e
    invalidate_client(endpoint_id)
    audit("endpoint_delete", actor=actor, detail={"id": endpoint_id})
    return {"ok": True, "message": "端点已删除", "active_id": get_active_endpoint_id()}


@app.post("/api/endpoints/{endpoint_id}/activate")
def api_endpoints_activate(endpoint_id: str, actor: AuthUser) -> dict[str, Any]:
    remote_sid = parse_remote_endpoint_id(endpoint_id)
    if remote_sid:
        sessions = {s["session_id"]: s for s in list_controller_outbound_sessions()}
        s = sessions.get(remote_sid)
        if not s:
            raise HTTPException(status_code=404, detail="远程节点不存在或已断开")
        set_meta(META_ACTIVE_REMOTE, endpoint_id)
        audit("endpoint_activate", actor=actor, detail={"id": endpoint_id, "kind": "remote_agent"})
        return {
            "ok": True,
            "item": {
                "id": endpoint_id,
                "name": f"远程 · {s.get('peer_name') or '节点'}",
                "docker_host": s.get("docker_host"),
                "is_active": True,
                "is_local": False,
                "kind": "remote_agent",
                "online": bool(s.get("online")),
                "capabilities": s.get("capabilities") or {},
            },
            "active_id": endpoint_id,
            "message": f"已切换到远程节点「{s.get('peer_name') or remote_sid[:8]}」"
            + ("（在线）" if s.get("online") else "（离线，操作将失败）"),
        }
    try:
        ep = set_active_endpoint_id(endpoint_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="端点不存在") from None
    except ValueError:
        raise HTTPException(status_code=400, detail="端点已禁用") from None
    try:
        set_meta(META_ACTIVE_REMOTE, "")
    except Exception:
        pass
    audit("endpoint_activate", actor=actor, detail={"id": endpoint_id, "name": ep.get("name")})
    return {
        "ok": True,
        "item": public_endpoint(ep, active_id=endpoint_id),
        "active_id": endpoint_id,
        "message": f"已切换到「{ep.get('name')}」",
    }


@app.post("/api/endpoints/{endpoint_id}/test")
async def api_endpoints_test(endpoint_id: str, actor: AuthUser) -> dict[str, Any]:
    remote_sid = parse_remote_endpoint_id(endpoint_id)
    if remote_sid:
        if not is_agent_online(remote_sid):
            return {
                "ok": False,
                "docker": {"ok": False, "error": "远程节点不在线"},
                "message": "远程节点不在线",
            }
        try:
            result = await rpc_to_agent(remote_sid, method="GET", path="/api/health")
            body = result.get("body") if isinstance(result, dict) and "body" in result else result
            ok = bool((body or {}).get("ok", True)) if isinstance(body, dict) else True
            return {
                "ok": ok,
                "docker": (body or {}).get("docker") if isinstance(body, dict) else body,
                "message": "远程节点连通正常" if ok else "远程节点异常",
                "remote": True,
            }
        except Exception as e:
            return {"ok": False, "docker": {"ok": False, "error": str(e)}, "message": str(e)}
    ep = get_endpoint(endpoint_id)
    if not ep:
        raise HTTPException(status_code=404, detail="端点不存在")
    result = _test_endpoint_conn(ep)
    return {
        "ok": bool(result.get("ok")),
        "docker": result,
        "item": public_endpoint(ep, active_id=get_active_endpoint_id()),
        "message": "连通正常" if result.get("ok") else f"连通失败：{result.get('error') or 'unknown'}",
    }


# ── Compose ──────────────────────────────────────────────


@app.get("/api/compose/projects")
def api_compose_projects(actor: OptionalUser = None) -> dict[str, Any]:
    if not get_settings().compose_enabled:
        return {"ok": True, "items": [], "disabled": True, "viewer": actor}
    ep = _active_ep()
    if not is_local_host(ep.get("docker_host") or ""):
        return {
            "ok": True,
            "items": [],
            "disabled": True,
            "remote_only_local": True,
            "message": "Compose 仅支持本地 unix 端点，请切换到本机端点",
            "viewer": actor,
        }
    items = list_projects()
    return {"ok": True, "count": len(items), "items": items, "viewer": actor}


@app.get("/api/compose/projects/{name}")
def api_compose_project(name: str, actor: OptionalUser = None) -> dict[str, Any]:
    p = get_project(name)
    if not p:
        raise HTTPException(status_code=404, detail="Compose 项目不存在")
    return {"ok": True, "item": p, "viewer": actor}


@app.post("/api/compose/projects/{name}/backup")
def api_compose_backup(name: str, actor: AuthUser) -> dict[str, Any]:
    _require_local_feature("Compose")
    return backup_project(name, actor=actor)


@app.post("/api/compose/projects/{name}/update")
def api_compose_update(name: str, body: ComposeUpdateBody, actor: AuthUser) -> dict[str, Any]:
    _require_local_feature("Compose")
    return safe_update_project(name, actor=actor, service=body.service, recreate=body.recreate)


@app.post("/api/compose/projects/{name}/up")
def api_compose_up(name: str, actor: AuthUser) -> dict[str, Any]:
    _takeover_or_403()
    _require_local_feature("Compose")
    return project_up(name, actor=actor)


@app.post("/api/compose/projects/{name}/down")
def api_compose_down(name: str, actor: AuthUser) -> dict[str, Any]:
    _takeover_or_403()
    _require_local_feature("Compose")
    return project_down(name, actor=actor)


# ── Unraid ───────────────────────────────────────────────


@app.get("/api/unraid/templates")
def api_unraid_templates(actor: OptionalUser = None) -> dict[str, Any]:
    if not get_settings().unraid_enabled:
        return {"ok": True, "items": [], "disabled": True, "viewer": actor}
    ep = _active_ep()
    if not is_local_host(ep.get("docker_host") or ""):
        return {
            "ok": True,
            "items": [],
            "available": False,
            "disabled": True,
            "remote_only_local": True,
            "path": str(get_settings().unraid_templates_path()),
            "message": "Unraid 模板仅支持本地 unix 端点，请切换到本机端点",
            "viewer": actor,
        }
    available = templates_available()
    items = list_templates() if available else []
    return {
        "ok": True,
        "available": available,
        "path": str(get_settings().unraid_templates_path()),
        "count": len(items),
        "items": items,
        "viewer": actor,
    }


@app.get("/api/unraid/templates/{name}")
def api_unraid_template(name: str, actor: OptionalUser = None) -> dict[str, Any]:
    _require_local_feature("Unraid 模板")
    tpl = get_template(name)
    if not tpl:
        raise HTTPException(status_code=404, detail="Unraid 模板不存在或目录未挂载")
    return {"ok": True, "item": tpl, "viewer": actor}


@app.post("/api/unraid/templates/{name}/backup")
def api_unraid_backup(name: str, actor: AuthUser) -> dict[str, Any]:
    _require_local_feature("Unraid 模板")
    return backup_template(name, actor=actor)


@app.post("/api/unraid/templates/{name}/update")
def api_unraid_update(name: str, body: UnraidUpdateBody, actor: AuthUser) -> dict[str, Any]:
    _require_local_feature("Unraid 模板")
    return safe_update_unraid(
        name, actor=actor, repository=body.repository, recreate=body.recreate
    )


@app.post("/api/unraid/adopt/{container_id}")
def api_unraid_adopt(container_id: str, actor: AuthUser) -> dict[str, Any]:
    _takeover_or_403()
    _require_local_feature("Unraid 模板")
    return adopt_to_unraid(container_id, actor=actor)


# ── Remote mode v0.8: agent issues pair; controller dials agent ──


def _remote_runtime_bundle() -> dict[str, Any]:
    st = get_remote_settings()
    if st.get("role") == "controller":
        return dial_runtime_state()
    return agent_runtime_state()


@app.get("/api/remote/status")
def api_remote_status(actor: OptionalUser = None) -> dict[str, Any]:
    st = public_remote_status()
    return {
        "ok": True,
        "remote": st,
        "runtime": _remote_runtime_bundle(),
        "viewer": actor,
    }


@app.get("/api/remote/settings")
def api_remote_settings_get(actor: AuthUser) -> dict[str, Any]:
    return {
        "ok": True,
        "settings": public_settings_view(),
        "status": public_remote_status(),
        "runtime": _remote_runtime_bundle(),
    }


@app.put("/api/remote/settings")
def api_remote_settings_put(body: RemoteSettingsBody, actor: AuthUser) -> dict[str, Any]:
    patch = body.model_dump(exclude_none=True)
    if "controller_base_url" in patch and patch["controller_base_url"]:
        try:
            patch["controller_base_url"] = normalize_base_url(
                patch["controller_base_url"], label="地址"
            )
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
    if "public_base_url" in patch and patch["public_base_url"]:
        try:
            patch["public_base_url"] = normalize_base_url(
                patch["public_base_url"], label="本机公网域名或 IP"
            )
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
    prev = get_remote_settings()
    cur = set_remote_settings(patch, actor=actor)
    if prev.get("enabled") and not cur.get("enabled"):
        stop_all_controller_dials()
        stop_agent_dial()
        if prev.get("active_session_id"):
            try:
                revoke_session(prev["active_session_id"], actor=actor)
            except Exception:
                pass
        try:
            set_meta(META_ACTIVE_REMOTE, "")
        except Exception:
            pass
    if cur.get("role") != "controller":
        stop_all_controller_dials()
    return {
        "ok": True,
        "settings": public_settings_view(cur),
        "status": public_remote_status(),
        "runtime": _remote_runtime_bundle(),
        "message": "远程模式设置已保存",
    }


@app.post("/api/remote/pair")
def api_remote_pair_create(body: RemotePairBody, actor: AuthUser) -> dict[str, Any]:
    """被控生成 60s 连接凭证（内嵌本机公网/IP）。"""
    st = get_remote_settings()
    if not st.get("enabled") or st.get("role") != "agent":
        raise HTTPException(status_code=400, detail="请先开启远程模式并选择「被控端」")
    base = (body.public_base_url or st.get("public_base_url") or "").strip()
    if not base:
        raise HTTPException(status_code=400, detail="请填写本机公网域名或 IP（主控需能访问）")
    mode = (body.mode or st.get("agent_mode") or "collab").lower()
    if mode not in ("collab", "managed"):
        mode = "collab"
    name = (
        body.agent_name
        or body.display_name
        or st.get("display_name")
        or "被控"
    ).strip() or "被控"
    try:
        result = create_pair_code(
            public_base_url=base,
            mode=mode,
            agent_name=name,
            created_by=actor or "admin",
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return result


@app.get("/api/remote/pair")
def api_remote_pair_status(
    actor: AuthUser,
    code_id: str | None = Query(default=None),
) -> dict[str, Any]:
    return get_pair_status(code_id)


@app.post("/api/remote/controller/connect")
def api_remote_controller_connect(
    body: RemoteControllerConnectBody, actor: AuthUser
) -> dict[str, Any]:
    """主控粘贴被控凭证并主动连接。"""
    st = get_remote_settings()
    if not st.get("enabled") or st.get("role") != "controller":
        raise HTTPException(status_code=400, detail="请先开启远程模式并选择「主控端」")
    name = (body.controller_name or st.get("display_name") or "主控").strip() or "主控"
    try:
        result = start_controller_connect(
            pair_code=body.pair_code.strip(),
            controller_name=name,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except RuntimeError as e:
        raise HTTPException(status_code=502, detail=str(e)) from e
    audit(
        "remote_controller_connect",
        actor=actor,
        detail={
            "session_id": result.get("session_id"),
            "agent": result.get("agent_name"),
            "base": result.get("agent_base_url"),
        },
    )
    return {
        "ok": True,
        "message": result.get("message") or "已连接被控",
        "session_id": result.get("session_id"),
        "agent_name": result.get("agent_name"),
        "mode": result.get("mode"),
        "runtime": dial_runtime_state(),
        "status": public_remote_status(),
        "settings": public_settings_view(),
    }


@app.post("/api/remote/controller/disconnect")
def api_remote_controller_disconnect_all(actor: AuthUser) -> dict[str, Any]:
    stop_all_controller_dials()
    set_remote_settings(
        {"status": "idle", "active_session_id": "", "active_peer_name": ""},
        actor=actor,
    )
    try:
        set_meta(META_ACTIVE_REMOTE, "")
    except Exception:
        pass
    return {"ok": True, "message": "已断开全部远程被控", "runtime": dial_runtime_state()}


@app.post("/api/remote/agent/disconnect")
def api_remote_agent_disconnect(actor: AuthUser) -> dict[str, Any]:
    """被控主动结束当前远程会话（撤销 + 回 setup）。"""
    st = get_remote_settings()
    sid = st.get("active_session_id") or ""
    if sid:
        try:
            revoke_session(sid, actor=actor)
        except Exception:
            pass
    set_remote_settings(
        {
            "status": "idle",
            "ui_phase": "setup",
            "active_session_id": "",
            "active_peer_name": "",
            "_session_token": None,
        },
        actor=actor,
    )
    audit("remote_agent_disconnect", actor=actor, detail={"session_id": sid})
    return {
        "ok": True,
        "message": "已断开远程连接，可重新生成凭证",
        "runtime": agent_runtime_state(),
        "status": public_remote_status(),
    }


@app.post("/api/remote/agent/switch-mode")
def api_remote_agent_switch_mode(body: RemoteSessionModeBody, actor: AuthUser) -> dict[str, Any]:
    """被控：切换协同/托管，或 mode_pick 回到模式选择。"""
    try:
        return agent_switch_mode_local(body.mode, actor=actor)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@app.post("/api/remote/sessions/{session_id}/disconnect")
def api_remote_session_disconnect(session_id: str, actor: AuthUser) -> dict[str, Any]:
    st = get_remote_settings()
    if st.get("role") == "controller":
        stop_controller_dial(session_id)
    ok = revoke_session(session_id, actor=actor)
    if not ok and st.get("role") != "controller":
        raise HTTPException(status_code=404, detail="会话不存在")
    stored = get_meta(META_ACTIVE_REMOTE) or ""
    if stored == remote_endpoint_id(session_id):
        set_meta(META_ACTIVE_REMOTE, "")
    return {"ok": True, "message": "已断开远程节点"}


@app.post("/api/remote/sessions/{session_id}/mode")
async def api_remote_session_mode(
    session_id: str, body: RemoteSessionModeBody, actor: AuthUser
) -> dict[str, Any]:
    """主控可请求改模式（被控 UI 仍以本地切换为主）。"""
    st = get_remote_settings()
    if not st.get("enabled") or st.get("role") != "controller":
        raise HTTPException(status_code=400, detail="仅主控端可请求切换远程节点模式")
    mode = (body.mode or "").strip().lower()
    if mode not in ("collab", "managed"):
        raise HTTPException(status_code=400, detail="mode 须为 collab 或 managed")
    try:
        result = set_session_mode(session_id, mode, actor=actor)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    return result


@app.get("/api/remote/sessions")
def api_remote_sessions_list(actor: AuthUser) -> dict[str, Any]:
    st = get_remote_settings()
    if st.get("role") == "controller":
        items = list_controller_outbound_sessions()
        try:
            from remote_controller import dial_online_map

            om = dial_online_map()
            for s in items:
                if om.get(s["session_id"]):
                    s["online"] = True
        except Exception:
            pass
        return {"ok": True, "items": items}
    return {"ok": True, "items": list_remote_sessions(role_side="agent_hub")}


@app.websocket("/api/remote/hub")
@app.websocket("/api/remote/agent/ws")  # alias for older clients
async def ws_remote_agent_hub(websocket: WebSocket) -> None:
    """
    被控枢纽：主控 dial-in。
    首包 hello{pair_code, controller_name} 或 resume{session_id, session_token}。
    之后接收 rpc → execute_local_rpc → rpc_result。
    """
    await websocket.accept()
    # Only meaningful when this instance is agent; still accept to return clear error
    st0 = get_remote_settings()
    session_id: str | None = None
    controller_name = "主控"
    try:
        raw = await asyncio.wait_for(websocket.receive_text(), timeout=30)
        msg = json.loads(raw)
    except Exception:
        try:
            await websocket.send_text(json.dumps({"type": "error", "detail": "握手超时或无效"}))
        except Exception:
            pass
        await websocket.close(code=4400)
        return

    if not st0.get("enabled") or st0.get("role") != "agent":
        await websocket.send_text(
            json.dumps(
                {
                    "type": "error",
                    "detail": "本机未开启被控端远程模式，无法接受主控连接",
                }
            )
        )
        await websocket.close(code=4403)
        return

    typ = (msg.get("type") or "").lower()
    try:
        if typ == "hello":
            pair_code = msg.get("pair_code") or ""
            controller_name = (msg.get("controller_name") or "主控").strip() or "主控"
            result = redeem_pair_code(
                pair_code,
                controller_name=controller_name,
                controller_base_hint=msg.get("controller_base_url") or "",
            )
            session_id = result["session_id"]
            register_agent_ws(session_id, websocket)
            await websocket.send_text(
                json.dumps(
                    {
                        "type": "welcome",
                        "session_id": session_id,
                        "session_token": result["session_token"],
                        "mode": result["mode"],
                        "controller_name": result.get("controller_name") or controller_name,
                        "agent_name": result.get("agent_name") or st0.get("display_name") or "被控",
                        "expires_at": result.get("expires_at"),
                        "message": "配对成功，主控已接入",
                    },
                    ensure_ascii=False,
                )
            )
        elif typ == "resume":
            token = msg.get("session_token") or ""
            want_sid = msg.get("session_id") or ""
            controller_name = (msg.get("controller_name") or "主控").strip() or "主控"
            sess = get_session_by_token(token)
            if not sess:
                await websocket.send_text(
                    json.dumps({"type": "error", "detail": "会话无效或已撤销，请重新粘贴凭证"})
                )
                await websocket.close(code=4401)
                return
            if want_sid and want_sid != sess["session_id"]:
                await websocket.send_text(
                    json.dumps({"type": "error", "detail": "会话 ID 不匹配"})
                )
                await websocket.close(code=4401)
                return
            session_id = sess["session_id"]
            register_agent_ws(session_id, websocket)
            meta = {}
            try:
                meta = json.loads(sess.get("meta") or "{}")
            except Exception:
                pass
            # refresh agent UI peer
            set_remote_settings(
                {
                    "status": "managed_lock"
                    if (sess.get("mode") or "") == "managed"
                    else "connected",
                    "ui_phase": "managed_full"
                    if (sess.get("mode") or "") == "managed"
                    else "collab_banner",
                    "active_session_id": session_id,
                    "active_peer_name": meta.get("controller_name") or controller_name,
                    "agent_mode": sess.get("mode") or "collab",
                }
            )
            await websocket.send_text(
                json.dumps(
                    {
                        "type": "resumed",
                        "session_id": session_id,
                        "session_token": token,
                        "mode": sess.get("mode") or "collab",
                        "controller_name": meta.get("controller_name") or controller_name,
                        "agent_name": meta.get("agent_name") or st0.get("display_name") or "被控",
                        "message": "已恢复连接",
                    },
                    ensure_ascii=False,
                )
            )
        else:
            await websocket.send_text(
                json.dumps({"type": "error", "detail": "首包须为 hello 或 resume"})
            )
            await websocket.close(code=4400)
            return
    except ValueError as e:
        await websocket.send_text(json.dumps({"type": "error", "detail": str(e)}))
        await websocket.close(code=4403)
        return
    except Exception as e:
        await websocket.send_text(json.dumps({"type": "error", "detail": str(e)}))
        await websocket.close(code=1011)
        return

    try:
        while True:
            raw = await websocket.receive_text()
            try:
                data = json.loads(raw)
            except Exception:
                continue
            mtype = data.get("type")
            if mtype == "pong":
                if session_id:
                    touch_session(session_id, online=True)
                continue
            if mtype == "ping":
                await websocket.send_text(json.dumps({"type": "pong", "t": time.time()}))
                continue
            if mtype == "rpc":
                # Controller commands → execute on this agent
                result = execute_local_rpc(
                    data.get("method") or "GET",
                    data.get("path") or "",
                    query=data.get("query") or {},
                    body=data.get("body"),
                    actor=f"remote:{controller_name}",
                )
                await websocket.send_text(
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
                continue
            if mtype == "mode_change":
                # controller requested mode — apply locally
                try:
                    from remote import apply_agent_mode_change

                    apply_agent_mode_change(data.get("mode") or "collab")
                except Exception:
                    pass
                continue
            if mtype == "rpc_result":
                resolve_rpc_response(data)
                continue
            if mtype == "bye":
                break
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        if session_id:
            unregister_agent_ws(session_id, websocket)


@app.get("/", response_class=HTMLResponse)
def index(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "title": "DockerOps",
            "subtitle": "NAS 日常运维 · 首次设置向导 · Tower / 飞牛引擎级接管",
            "version": VERSION,
            "asset_v": VERSION,
        },
    )


@app.exception_handler(Exception)
async def unhandled(request: Request, exc: Exception):
    if isinstance(exc, HTTPException):
        return JSONResponse(status_code=exc.status_code, content={"ok": False, "detail": exc.detail})
    return JSONResponse(status_code=500, content={"ok": False, "detail": str(exc)})

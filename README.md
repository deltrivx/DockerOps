# DockerOps —— 面向 NAS 的 Docker 运维平台

DockerOps 是一款专为 NAS、家庭服务器和轻量运维场景设计的 Docker 运维平台。

它不追求做成“什么都能改”的重型控制台，而是把精力放在更关键、也更危险的环节上：

- 更新是否安全
- 故障是否好查
- 运行是否稳定
- 操作是否可追溯

目标很明确：让 Docker 更新更安全、问题排查更简单、日常运维更省心。

> **v0.4.2**：去除默认账号；管理员/会话/偏好写入内置 SQLite（`/data/dockerops.db`）；登录与安装向导不预填。  
> **v0.4.1**：修复手机无法点击、PC 粒子不可见；无 Compose 项目时隐藏 Compose 菜单。  
> **v0.4.0**：运维总览模块卡片；Unraid 风格响应式 UI；粒子 + 背景个性化；说明与更新日志；批量启停 / 重命名 / stats。  
> 管理源：Compose / Unraid / 三方。**完整接管**默认关闭。镜像仅由 **GitHub Actions → GHCR** 构建，禁止本机构建再上传。

---

## 为什么需要 DockerOps

很多人在 NAS 或家庭服务器上跑 Docker 后，真正麻烦的往往不是“能不能跑起来”，而是：

- 更新前不敢动，怕一更新就挂
- 出问题后不知道从哪查
- 容器多了之后，健康状态全靠感觉
- 改过配置、升过版本，事后却记不清

DockerOps 就是为了解决这些问题。

---

## 核心能力

### 1. 安全更新：备份、回滚、可恢复

DockerOps 把更新当成一次可回退的运维动作，而不是“直接覆盖碰运气”。

更新前会先备份当前状态，更新过程中记录关键信息，更新失败时可快速回退。

**开启完整接管后的自动更新语义（对齐 Unraid，飞牛/Compose 同样适用）：**

- **Compose / 飞牛**：`compose pull` → `up -d --force-recreate --remove-orphans`（无需先手动停容器）  
  - 若某服务仍挂在更新前的 image id，会 **stop → remove → up --no-deps** 硬替换兜底  
  - 成功后清理**被替换的旧镜像 id** + **dangling** 层（默认不做 `prune -a`，避免误删其它未使用镜像）
- **Unraid**：模板备份 → pull → stop → remove → 按模板 create（dockerman）→ 同样清理旧镜像
- **三方容器**：仍只备份 + 拉镜像，不自动重建（请 Adopt / 纳入 Compose）
- **未开启接管**：只 pull，返回 partial；不会停容器、不会删镜像

适合：

- 家用 NAS
- 长期运行的服务
- 不敢随便动的线上/半线上环境

### 2. Doctor 诊断：不只告诉你“挂了”，还尽量告诉你“为什么”

DockerOps 内置 Doctor 诊断能力，会从多个维度检查容器和运行环境，例如：

- 容器状态
- 健康检查
- 重启次数
- 资源占用
- 挂载与网络配置
- 常见异常信号

最后会给出一个健康分，并附带可读的诊断说明。

目标不是堆一堆原始日志，而是尽快帮你定位问题。

### 3. 自动监控：让系统自己盯着

DockerOps 支持自动监控与巡检，可以持续观察容器运行状态，并生成监控报告。

你可以更早发现：

- 异常重启
- 健康检查失败
- 资源异常
- 服务不稳定趋势

适合不想整天盯着面板的人。

### 4. Web 控制台：中文界面，信息更直观

DockerOps 提供中文 Web 控制台，把容器状态、健康情况、监控信息和运维记录集中展示。

更适合 NAS 用户和家庭服务器用户日常查看，而不是只给专业运维看的冷冰冰接口页。

### 5. 安全与权限控制

DockerOps 内置基础安全能力，包括：

- 登录认证
- 会话管理
- API Token
- 权限控制
- 操作审计

即使部署在内网，也不建议裸奔。

### 6. REST API + Swagger

除了 Web 界面，DockerOps 也提供 REST API，并自带 Swagger 文档。

方便：

- 二次开发
- 脚本调用
- 自动化接入
- 和其他系统联动

### 7. 运维记录可追溯

DockerOps 会记录关键运维动作和诊断结果，方便你回头查看：

- 什么时候更新过
- 出过什么问题
- 做过哪些处理
- 当时系统状态如何

对排查问题和复盘非常有帮助。

### 8. Docker Compose 双方接管

识别 `com.docker.compose.*` 标签与配置的工程目录，按**项目**备份 / 更新（含 `--remove-orphans` 与旧镜像清理）：

- 备份 compose 文件 + 服务摘要
- `docker compose pull` +（接管开启时）`up -d --force-recreate`
- 与宿主机 CLI / 插件操作**同一项目**，不是另起一套

### 9. Unraid 模板升级（非 docker run 拼接）

约定挂载 `dockerMan/templates-user` 后：

- 读取 / 备份 `my-*.xml`
- 更新走 **模板语义重建**，强制 `net.unraid.docker.managed=dockerman`
- Unraid Docker 页仍显示为官方管理应用，**不是三方**
- 三方容器可 **Adopt** 生成模板并纳入 dockerMan

仓库提供 [`unraid/my-dockerops.xml`](unraid/my-dockerops.xml)，可将 DockerOps 自身装为 Unraid 应用。

### 10. 日常运维资源中心（v0.3，Portainer 日常替代）

登录后即可：

- 容器 **启动 / 停止 / 重启 / 暂停**、查看日志
- 镜像列表 / 拉取；网络与卷列表
- Docker 事件、Engine info / df

开启完整接管后额外支持：

- 删除容器、删除镜像、创建/删除网络与卷、prune、系统清理

### 11. 双 NAS 平台探测

自动识别 `unraid` / `fnos` / `generic`（`GET /api/platform`），并给出挂载建议。

| 主机 | 接管方式 | 文档 |
|------|----------|------|
| Unraid (Tower) | dockerMan 模板 + 可选 Compose | [`unraid/my-dockerops.xml`](unraid/my-dockerops.xml) |
| 飞牛 (FnOS) | **引擎级 Compose**（不深绑 AppCenter） | [`fnos/README.md`](fnos/README.md) |

---

## 设计理念

DockerOps 的设计思路不是“功能越多越好”，而是：

**更安全、更省心、更适合 NAS 场景。**

所以它更强调：

- 更新前先保护
- 出问题能诊断
- 运行中能观察
- 操作后能追溯

它适合这些用户：

- NAS 玩家
- 家庭服务器用户
- 个人运维
- 小团队轻量部署
- 不想把 Docker 管得太复杂，但又希望更稳一点的人

---

## 快速开始

### 使用 GHCR 镜像（推荐）

```bash
docker run -d \
  --name dockerops \
  --restart unless-stopped \
  -p 8080:8080 \
  -v /var/run/docker.sock:/var/run/docker.sock:ro \
  -v dockerops-data:/data \
  ghcr.io/deltrivx/dockerops:latest
```

打开：`http://<host>:8080/`

**账号与存储**

- **无默认账号**（不再内置 `admin` / `dockerops`）。
- 账号、会话、审计、UI 偏好写入内置 **SQLite**：`/data/dockerops.db`。
- **首次使用**：打开 Web 向导创建超级管理员（bcrypt 哈希入库）。
- **可选预置**：仅当进程环境**同时显式设置** `DOCKEROPS_ADMIN_USER` 与 `DOCKEROPS_ADMIN_PASSWORD`（≥6 位）时，启动时写入 SQLite 并跳过向导。Settings 空默认不会自动建号。

### Docker Compose

```bash
git clone https://github.com/deltrivx/DockerOps.git
cd DockerOps
cp .env.example .env
# 无需填写默认密码；首次打开 Web 创建管理员

docker compose up -d
```

### 健康检查

```bash
curl -sS http://127.0.0.1:8080/api/health
curl -sS http://127.0.0.1:8080/api/doctor
```

Swagger：`http://<host>:8080/docs`

---

## 环境变量

| 变量 | 默认值 | 说明 |
|---|---|---|
| `DOCKEROPS_HOST` | `0.0.0.0` | 监听地址 |
| `DOCKEROPS_PORT` | `8080` | 监听端口 |
| `DOCKEROPS_DATA_DIR` | `/data` | 数据目录（内置 SQLite `dockerops.db` / 报告 / 备份） |
| `DOCKEROPS_ADMIN_USER` | （空） | 可选。与 PASSWORD **同时**在进程环境显式设置时启动写入 SQLite；无默认值 |
| `DOCKEROPS_ADMIN_PASSWORD` | （空） | 可选。与 USER 同时设置且 ≥6 位时预置管理员；否则首次 Web 向导 |
| `DOCKEROPS_API_TOKEN` | （空） | 可选固定 API Token；为空则登录后颁发会话 Token |
| `DOCKEROPS_DOCKER_HOST` | `unix:///var/run/docker.sock` | Docker 引擎地址 |
| `DOCKEROPS_PLATFORM` | `auto` | `auto` / `unraid` / `fnos` / `generic` |
| `DOCKEROPS_RESOURCE_APIS` | `true` | 生命周期/日志/镜像/网络/卷/系统 API |
| `DOCKEROPS_CONSOLE_ENABLED` | `false` | Web 终端（高风险，默认关） |
| `DOCKEROPS_TAKEOVER_ENABLED` | `false` | 完整接管：compose up/down、模板重建、Adopt、删除/prune |
| `DOCKEROPS_COMPOSE_ENABLED` | `true` | 启用 Compose 发现 |
| `DOCKEROPS_COMPOSE_PROJECT_DIRS` | （空） | 额外 compose 目录，`:` 分隔 |
| `DOCKEROPS_UNRAID_ENABLED` | `true` | 启用 Unraid 模板 |
| `DOCKEROPS_UNRAID_TEMPLATES_USER` | `/unraid/templates-user` | 容器内模板路径 |
| `TZ` | `Asia/Shanghai` | 时区 |

### Unraid (Tower) 挂载约定

```text
/boot/config/plugins/dockerMan/templates-user  →  /unraid/templates-user
/var/run/docker.sock                           →  /var/run/docker.sock   (接管时用 rw)
```

推荐用 [`unraid/my-dockerops.xml`](unraid/my-dockerops.xml) 安装，保证 DockerOps 自身为 dockerman 应用。

### 飞牛 (FnOS) 挂载约定

```text
/var/run/docker.sock     →  同左 (接管 rw)
/vol1/docker/compose     →  /compose   （路径按实机调整）
DOCKEROPS_PLATFORM=fnos
DOCKEROPS_COMPOSE_PROJECT_DIRS=/compose
DOCKEROPS_UNRAID_ENABLED=false
```

详见 [`fnos/README.md`](fnos/README.md) 与 [`fnos/docker-compose.yml`](fnos/docker-compose.yml)。

### 完整接管 Compose 示例

```bash
# 使用 profile（rw socket）
docker compose --profile takeover up -d
# 并设置 DOCKEROPS_COMPOSE_PROJECT_DIRS=/compose 与对应 volume
```

### 权限模型

| 操作 | 条件 |
|------|------|
| 列表 / Doctor / 日志 | 可选登录 |
| 启停 / 重启 / 暂停 / 拉镜像 | 登录 |
| 删除 / prune / 建删网络卷 / compose up-down / 模板重建 / Adopt | 登录 + `TAKEOVER=true` |

---

## API 概览

| 方法 | 路径 | 说明 |
|---|---|---|
| `GET` | `/api/health` | 服务健康 |
| `GET` | `/api/platform` | 平台探测与挂载建议 |
| `GET` | `/api/managers/summary` | 管理源统计 / 接管状态 |
| `GET` | `/api/auth/status` | 是否需要首次设置 |
| `POST` | `/api/auth/setup` | 首次创建管理员（仅未初始化时） |
| `POST` | `/api/auth/login` | 登录 |
| `POST` | `/api/auth/change-password` | 修改密码 |
| `GET` | `/api/containers` | 容器列表（含 manager） |
| `POST` | `/api/containers/{id}/start\|stop\|restart\|pause\|unpause\|kill` | 生命周期（登录） |
| `DELETE` | `/api/containers/{id}` | 删除（需接管） |
| `GET` | `/api/containers/{id}/logs` | 日志；`follow=1` 为 SSE |
| `GET` | `/api/images` · `POST /api/images/pull` · `DELETE /api/images/{id}` · `POST /api/images/prune` | 镜像 |
| `GET\|POST\|DELETE` | `/api/networks` · `/api/volumes` | 网络 / 卷 |
| `GET` | `/api/system/info` · `/api/system/df` | 系统信息 |
| `POST` | `/api/system/prune` | 系统清理（需接管） |
| `GET` | `/api/events` | 近期事件；`follow=1` 为 SSE |
| `GET` | `/api/doctor` · `/api/monitor/report` · `/api/ops/*` | 诊断 / 监控 / 安全更新 |
| `GET\|POST` | `/api/compose/projects...` | Compose 项目 |
| `GET\|POST` | `/api/unraid/templates...` · `/api/unraid/adopt/{id}` | Unraid 模板 |

完整列表见 Swagger：`/docs`。

---

## 镜像构建说明

本仓库镜像 **仅通过 GitHub Actions 远程构建** 并推送到：

```text
ghcr.io/deltrivx/dockerops
```

触发条件：

- 推送到 `main`
- 创建 `v*` tag
- 手动 `workflow_dispatch`

标签策略：

- `latest`：默认分支最新成功构建
- `sha-<commit>`：按提交
- `vX.Y.Z`：语义化版本 tag

推送 `v*` tag 时额外：

- 构建飞牛 **FPK**（`dist/dockerops-<ver>-fnos.fpk`）
- 创建 **GitHub Release** 并附带 FPK / checksum / meta.json

请勿依赖本机构建 Docker 镜像再上传；以 Actions / GHCR 产物为准。FPK 为安装包装配（不含镜像层），可在 CI 或本地 `./scripts/build_fnos_fpk.sh` 生成。

---

## 项目结构

```text
DockerOps/
├── app/
│   ├── main.py              # FastAPI 入口 v0.4.2
│   ├── auth.py · db.py      # SQLite 账号/会话（无默认账号）
│   ├── host_platform.py     # Unraid / 飞牛 / generic 探测
│   ├── docker_resources.py  # 日常资源运维
│   ├── logs_stream.py · events_stream.py
│   ├── manager.py · compose_mgr.py · unraid_mgr.py · ops.py
│   ├── docker_client.py · doctor.py · monitor.py · auth.py · db.py
│   ├── static/ · templates/
├── unraid/my-dockerops.xml · icon.png
├── fnos/
│   ├── docker-compose.yml · README.md
│   └── fpk/                 # 飞牛专业安装包源（manifest/图标/向导/启停）
├── scripts/build_fnos_fpk.sh
├── .github/workflows/       # GHCR 镜像 + FPK Release
├── Dockerfile
├── docker-compose.yml
└── README.md
```

---

## 安全提示

- 生产/家用长期部署请修改默认密码，并设置强 `DOCKEROPS_API_TOKEN` 或仅内网访问
- 挂载 Docker socket 即拥有引擎级权限；本镜像默认 **只读挂载** socket，写操作在 API 层受控
- 不要把真实密码提交进 Git

---

## 路线图（简述）

- [x] 中文 Web 控制台
- [x] Doctor 健康分与诊断说明
- [x] 监控报告与运维记录
- [x] REST API + Swagger
- [x] 登录 / Token / 审计基础
- [x] Compose 项目发现与双方接管
- [x] Unraid 模板备份 / 升级 / Adopt（非三方）
- [x] 可选完整接管开关
- [x] 日常运维资源 API + UI（生命周期/日志/镜像/网络/卷/系统）
- [x] 平台探测（Unraid / 飞牛）与飞牛引擎级部署配方
- [x] 首次管理员设置向导 + 环境变量预置
- [x] 飞牛专业 FPK 安装包 + Release 附件
- [x] Unraid 风格 UI + 博客粒子背景
- [x] 一键检测 / 一键更新（digest 比对）
- [ ] Compose 栈文件编辑 / 更新检测
- [ ] 一键回滚执行（当前为指引）
- [ ] Web 终端（可选）
- [ ] 更细粒度的 RBAC
- [ ] 通知渠道（Webhook / 邮件）

---

## License

MIT

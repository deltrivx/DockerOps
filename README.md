# DockerOps —— 面向 NAS 的 Docker 运维平台

DockerOps 是一款专为 NAS、家庭服务器和轻量运维场景设计的 Docker 运维平台。

它不追求做成“什么都能改”的重型控制台，而是把精力放在更关键、也更危险的环节上：

- 更新是否安全
- 故障是否好查
- 运行是否稳定
- 操作是否可追溯

目标很明确：让 Docker 更新更安全、问题排查更简单、日常运维更省心。

> Web 控制台展示管理源（Compose / Unraid / 三方）；写操作需登录。**完整接管**（compose up/down、Unraid 模板重建、Adopt）默认关闭，开启后与原系统**双方接管**同一 compose 工程或 dockerMan 模板。镜像由 **GitHub Actions 远程构建** 发布到 GHCR，不在本机构建上传。

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

识别 `com.docker.compose.*` 标签与配置的工程目录，按**项目**备份 / 更新：

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
  -e DOCKEROPS_ADMIN_PASSWORD=change-me \
  ghcr.io/deltrivx/dockerops:latest
```

打开：`http://<host>:8080/`

默认账号：`admin` / 环境变量 `DOCKEROPS_ADMIN_PASSWORD`（未设置时为 `dockerops`）

### Docker Compose

```bash
git clone https://github.com/deltrivx/DockerOps.git
cd DockerOps
cp .env.example .env
# 编辑 .env 修改密码等

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
| `DOCKEROPS_DATA_DIR` | `/data` | 数据目录（SQLite / 报告 / 备份元数据） |
| `DOCKEROPS_ADMIN_USER` | `admin` | 管理员用户名 |
| `DOCKEROPS_ADMIN_PASSWORD` | `dockerops` | 管理员密码（务必修改） |
| `DOCKEROPS_API_TOKEN` | （空） | 可选固定 API Token；为空则登录后颁发会话 Token |
| `DOCKEROPS_DOCKER_HOST` | `unix:///var/run/docker.sock` | Docker 引擎地址 |
| `DOCKEROPS_TAKEOVER_ENABLED` | `false` | 完整接管：compose up/down、模板重建、Adopt |
| `DOCKEROPS_COMPOSE_ENABLED` | `true` | 启用 Compose 发现 |
| `DOCKEROPS_COMPOSE_PROJECT_DIRS` | （空） | 额外 compose 目录，`:` 分隔 |
| `DOCKEROPS_UNRAID_ENABLED` | `true` | 启用 Unraid 模板 |
| `DOCKEROPS_UNRAID_TEMPLATES_USER` | `/unraid/templates-user` | 容器内模板路径 |
| `TZ` | `Asia/Shanghai` | 时区 |

### Unraid 挂载约定

```text
/boot/config/plugins/dockerMan/templates-user  →  /unraid/templates-user
/var/run/docker.sock                           →  /var/run/docker.sock   (接管时用 rw)
```

### 完整接管 Compose 示例

```bash
# 使用 profile（rw socket）
docker compose --profile takeover up -d
# 并设置 DOCKEROPS_COMPOSE_PROJECT_DIRS=/compose 与对应 volume
```

---

## API 概览

| 方法 | 路径 | 说明 |
|---|---|---|
| `GET` | `/api/health` | 服务健康 |
| `GET` | `/api/managers/summary` | 管理源统计 / 接管状态 / 挂载提示 |
| `POST` | `/api/auth/login` | 登录，获取 Token |
| `GET` | `/api/containers` | 容器列表（含 manager 分类） |
| `GET` | `/api/containers/{id}` | 容器详情 |
| `GET` | `/api/doctor` | 全局 Doctor 诊断 + 健康分 |
| `GET` | `/api/doctor/{id}` | 单容器诊断 |
| `GET` | `/api/monitor/report` | 监控报告 |
| `GET` | `/api/ops/records` | 运维记录 |
| `POST` | `/api/ops/backup/{id}` | 按管理源备份 |
| `POST` | `/api/ops/update/{id}` | 按管理源安全更新（分流 compose/unraid/三方） |
| `POST` | `/api/ops/rollback/{id}` | 回滚指引 |
| `GET` | `/api/compose/projects` | Compose 项目列表 |
| `POST` | `/api/compose/projects/{name}/backup` | 项目备份 |
| `POST` | `/api/compose/projects/{name}/update` | 项目安全更新 |
| `POST` | `/api/compose/projects/{name}/up` | compose up（需接管） |
| `POST` | `/api/compose/projects/{name}/down` | compose down（需接管） |
| `GET` | `/api/unraid/templates` | Unraid 模板列表 |
| `POST` | `/api/unraid/templates/{name}/backup` | 模板备份 |
| `POST` | `/api/unraid/templates/{name}/update` | 模板安全升级 |
| `POST` | `/api/unraid/adopt/{id}` | 三方 Adopt 为 dockerman（需接管） |

写操作需要 `Authorization: Bearer <token>`。破坏性接管另需 `DOCKEROPS_TAKEOVER_ENABLED=true`。

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

请勿依赖本机构建再上传；以 Actions / GHCR 产物为准。

---

## 项目结构

```text
DockerOps/
├── app/
│   ├── main.py            # FastAPI 入口
│   ├── manager.py         # 管理源分类
│   ├── compose_mgr.py     # Compose 发现 / 备份 / 接管
│   ├── unraid_mgr.py      # Unraid 模板读写 / 重建 / Adopt
│   ├── ops.py             # 统一备份 / 更新分流
│   ├── docker_client.py
│   ├── doctor.py / monitor.py / auth.py / db.py
│   ├── static/ · templates/
├── unraid/my-dockerops.xml
├── .github/workflows/     # 远程构建 GHCR
├── Dockerfile             # 含 docker CLI + compose plugin
├── docker-compose.yml     # 含 takeover profile
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
- [ ] 更新向导 UI 增强
- [ ] 更细粒度的 RBAC
- [ ] 通知渠道（Webhook / 邮件）

---

## License

MIT

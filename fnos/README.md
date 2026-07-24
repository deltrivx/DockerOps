# 飞牛 (FnOS) 部署说明

DockerOps 在飞牛上采用 **Docker / Compose 引擎级双向接管**：

- 与飞牛官方 Docker UI / CLI 操作**同一 engine**
- Compose 项目与宿主机**同一目录、同一 project 名**
- 提供 **专业 FPK 安装包**（应用中心手动安装），由容器拉取 GHCR 镜像运行

## 方式 A：FPK 专业安装包（推荐）

1. 从 [GitHub Releases](https://github.com/deltrivx/DockerOps/releases) 下载 `dockerops-*-fnos.fpk`
2. 飞牛 **应用中心 → 手动安装** 选择该 FPK
3. 安装向导中配置：
   - Web 端口（默认 8080）
   - 是否开启完整接管
   - **可选**预置管理员用户名/密码（留空则首次打开网页设置）
   - 宿主机 Compose 工程目录
4. 启动应用后：
   - **桌面图标**：通过 CGI 智能跳转打开（使用当前飞牛主机名 + 端口，**不再使用 127.0.0.1**，避免远程打开黑屏）
   - 也可直接浏览器访问 `http://<飞牛IP>:<端口>`

> **v0.3.3 黑屏修复**：旧版 FPK 桌面入口写死 `http://127.0.0.1:端口`，在其它设备上会指向本机导致黑屏。请升级到 `dockerops-0.3.3-fnos.fpk` 或更高版本。

FPK 源码与打包脚本：

- 包内容：[`fnos/fpk/`](fpk/)（manifest / 图标 / wizard / `ui/index.cgi` 桌面入口 / 启停脚本）
- 打包：`./scripts/build_fnos_fpk.sh` → `dist/dockerops-<ver>-fnos.fpk`
- CI 在 tag `v*` 时自动附带到 Release

## 方式 B：Compose / docker run

## 快速部署

```bash
# 按实机修改 compose 路径与密码后
docker compose -f fnos/docker-compose.yml up -d
```

或直接：

```bash
docker run -d --name dockerops --restart unless-stopped \
  -p 8080:8080 \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -v dockerops-data:/data \
  -v /vol1/docker/compose:/compose \
  -e DOCKEROPS_PLATFORM=fnos \
  -e DOCKEROPS_TAKEOVER_ENABLED=true \
  -e DOCKEROPS_COMPOSE_PROJECT_DIRS=/compose \
  -e DOCKEROPS_UNRAID_ENABLED=false \
  -e DOCKEROPS_ADMIN_PASSWORD=change-me \
  ghcr.io/deltrivx/dockerops:latest
```

打开 `http://<飞牛IP>:8080/`，查看顶栏 **平台: 飞牛**。

## 路径约定

| 容器内 | 宿主机（示例，以实机为准） |
|--------|---------------------------|
| `/var/run/docker.sock` | 同左（接管时 **rw**） |
| `/compose` | 飞牛 compose 工程根（多项目时挂父目录） |
| `/data` | 任意持久卷 |

部署后可调用：

```bash
curl -sS http://127.0.0.1:8080/api/platform | jq .
```

`compose_probes` / `mount_hints` 会提示探测到的路径。

## 权限模型

| 操作 | 条件 |
|------|------|
| 列表 / Doctor / 日志 | 可匿名或登录 |
| 启停 / 重启 | 登录 |
| compose up/down、删除、prune、建删网络卷 | 登录 + `DOCKEROPS_TAKEOVER_ENABLED=true` |

## 与飞牛 UI 共存

- 在飞牛里 `docker compose up` 的项目，DockerOps 通过 labels 或 `COMPOSE_PROJECT_DIRS` 发现后可备份/更新。
- DockerOps `compose up/down` 后，飞牛 UI 看到的是**同一批容器**，不会另起同名栈。
- 不要把同一应用再导出成第二套 project 名称。

## 不做的事

- 不解析 / 改写 AppCenter FPK 安装库
- 不冒充应用商店升级通道

镜像仅由 GitHub Actions 构建推送到 `ghcr.io/deltrivx/dockerops`。

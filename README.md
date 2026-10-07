# 图片 WebDAV 服务

这是供 chatgpt2api 图片存储配置使用的独立服务：`/dav/` 需要账号密码，可上传图片；`/images/` 无需登录，可公开读取图片；`/admin/` 是需要登录的管理页面，可查看、预览、上传、删除图片及修改访问凭据。图片和设置分别保存在 Docker 数据卷中，重启容器不会丢失。

## 本地启动（Docker Desktop）

在 PowerShell 中执行：

```powershell
cd D:\test\image-webdav
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
docker compose up -d --build
```

打开 <http://127.0.0.1:8088/admin/>，用 `.env` 中的 `INITIAL_USERNAME` 和 `INITIAL_PASSWORD` 登录。本机现有 `.env` 将 `HTTP_BIND` 设为 `0.0.0.0`：Docker 监听所有网络接口，局域网设备可以通过 `http://本机局域网IP:8088/admin/` 访问（还需 Windows 防火墙放行 8088 端口）。新部署从 `.env.example` 复制的配置默认只监听 `127.0.0.1`。HTTP 会明文传输密码，建议只在可信网络测试，上线前务必更换测试密码并使用 HTTPS。

管理页可以修改账号密码及公开访问地址。首次启动后凭据保存在 Docker 数据卷中；**修改 `.env` 不会重置已有凭据**，应在管理页修改。备份时请同时备份图片与设置两个数据卷。

## 存储清理

管理页“服务配置”中的“存储清理”默认关闭。开启后设置保留时长（1-8760 小时）和检查间隔（1-168 小时），保存后首次检查会在一个检查间隔之后执行；重启容器不会重置到期时间。也可输入小时数、确认后立即执行一次清理，无需开启定时任务。页面会显示上次删除的文件数、释放空间和失败数。

清理范围仅为图片卷中的 `chatgpt2api/images/`，按**服务器 B 上的文件修改时间**判断，清除超过设定小时数的普通文件和由此产生的空目录；账号配置卷不受影响。旧文件删除后，已返回给用户的图片 URL 将变成 404；服务器 A 的本地副本或图片索引不会同步删除。请按图片需要保持可访问的时间设置保留时长。清理产生的记录保存在设置卷中。

已有线上部署更新此功能时，将 `manager/app.py`、`manager/cleanup.py`、`manager/index.html`、`manager/Dockerfile` 上传覆盖到原项目对应位置，再执行 `docker compose up -d --build`。保留现有 `.env` 和数据卷；更新后到管理页手动启用定时清理。

## 在服务器 A 中填写

| 配置项 | 本机测试值 |
| --- | --- |
| 存储模式 | 先选择“本地 + WebDAV” |
| WebDAV URL | A 在本机 Docker 内：`http://host.docker.internal:8088/dav`；A 运行在本机系统上：`http://127.0.0.1:8088/dav` |
| 用户名、密码 | 管理页使用的凭据 |
| WebDAV 根路径 | `chatgpt2api/images` |
| 公开访问前缀 | 仅在同一台电脑测试：`http://127.0.0.1:8088/images`；跨设备访问：`http://本机局域网IP:8088/images` |

如果 A 在另一台电脑或服务器上，WebDAV URL 也应使用运行本服务的电脑的可访问 IP 或域名，不能填写 A 自己的 `127.0.0.1`。返回给用户的图片地址同理：`127.0.0.1` 指的是**用户自己的设备**，跨设备使用时要填写用户也能访问到的 IP 或域名。管理页“公开地址”可填 `http://本机局域网IP:8088`，页面会据此展示对应的配置示例。

在 A 中保存设置、点击“测试 WebDAV”，然后生成一张 `response_format=url` 的图片，确认返回的 `/images/` URL 可在目标设备上无需登录打开，管理页中也能看到图片。`b64_json` 仍通过 A 的 API 响应返回，并非让用户从此服务下载。WebDAV 上传失败时，即使选择“本地 + WebDAV”，该次图片请求也可能失败。

## 在线上服务器 B 部署（使用现有 Nginx 反代）

上传 `compose.yaml`、`nginx.conf`、`manager/`（包括新的 `cleanup.py`）和 `.env.example` 到 B；不要上传本机 `.env`，以免带上测试密码。在 B 的项目目录执行：

```bash
if [ ! -f .env ]; then cp .env.example .env; fi
nano .env
chmod 600 .env
docker compose up -d --build
docker compose ps
```

在 `.env` 中更换强密码。若现有反代运行在宿主机，保持 `HTTP_BIND=127.0.0.1`，反代目标是 `http://127.0.0.1:8088`；若现有反代在独立容器中、必须通过宿主机已发布的 8088 端口访问，则设 `HTTP_BIND=0.0.0.0`，反代目标使用它能够访问的宿主机地址。后者**同时允许外部直接访问 HTTP 8088**：必须在云安全组/云防火墙禁止公网入站 8088，只对外放行 80/443；不要单靠 UFW 防护 Docker 发布端口。也可让反代容器加入本项目的 Docker 网络、直接反代到 `nginx:80`，这样无需发布 8088 到公网。

现有 Nginx 为域名配置 HTTPS，并将完整路径（包括 `/admin/`、`/dav/` 和 `/images/`）反代到本服务，允许 `MKCOL`、`PUT`、`DELETE`，请求体上限至少 50 MB。访问 `https://img.example.cn/admin/`（换成真实域名），在管理页将“访问域名”设为 `https://img.example.cn`。A 中填写 WebDAV URL `https://img.example.cn/dav`、根路径 `chatgpt2api/images`、公开访问前缀 `https://img.example.cn/images`。大陆服务器使用域名时，还需按实际情况办理备案等手续。**此方案不需要 Caddy，也不需要 `compose.prod.yaml`。**

## 检查服务

```powershell
docker compose ps
docker compose logs --tail=100 manager nginx
```

管理页可修改凭据、公开地址和存储清理规则；监听端口、WebDAV 路径、存储目录及 HTTPS 域名仍由 `.env`、`nginx.conf`、Compose 配置文件控制。`/images/` 对所有人开放，请勿存储私密图片。

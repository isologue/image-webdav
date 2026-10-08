# ImageDAV 图片存储服务

一个 Docker 容器直接提供 WebDAV、图片访问和简单登录管理页，不需要额外的网关容器或 Web 服务器配置。

## 一条命令启动

在项目目录执行：

```bash
docker compose up -d --build
```

不需要创建 `.env`。默认监听 `0.0.0.0:8088`，可通过 IP 访问。

- 管理页面：`http://服务器IP:8088/admin/`
- 默认用户名：`admin`
- 默认密码：`admin2026`
- WebDAV 地址：`http://服务器IP:8088/dav`
- 图片公开地址：`http://服务器IP:8088/images/文件路径`

本机测试打开 <http://127.0.0.1:8088/admin/>。管理页支持查看、预览、上传、删除、复制图片链接、修改账号密码、配置清理规则和退出登录。

管理页通过登录表单建立会话；WebDAV 使用同一组账号密码的 Basic 认证。图片链接无需登录。可在管理页修改默认密码；修改后管理页需要重新登录，服务器 A 的 WebDAV 密码也需同步更新。新密码至少 8 位。HTTP 也可直接使用；如已有 HTTPS 反代，只需让它指向本服务的 8088 端口，保留完整请求路径。

## 可选配置

需要修改监听端口或首次登录凭据时，将 `.env.example` 复制为 `.env` 并修改：

```dotenv
INITIAL_USERNAME=admin
INITIAL_PASSWORD=admin2026
HTTP_BIND=0.0.0.0
HTTP_PORT=8088
```

账号密码仅在新数据卷首次启动时初始化。之后保存在配置卷中，修改 `.env` 不会覆盖已有账号密码，应在管理页修改。

## 服务器 A 的图片存储配置

| 配置项 | 填写值 |
| --- | --- |
| WebDAV URL | `http://服务器B的IP:8088/dav` |
| 用户名 | `admin`（或管理页设置的用户名） |
| 密码 | `admin2026`（或管理页设置的密码） |
| WebDAV 根路径 | `chatgpt2api/images` |
| 公开访问前缀 | `http://服务器B的IP:8088/images` |
| 存储模式 | 本地 + WebDAV，或仅 WebDAV |

如使用域名，替换上表的协议、IP 和端口即可。在管理页“公开地址”填 `http://服务器B的IP:8088` 或实际域名，页面会展示对应配置。跨服务器访问不要填写 `127.0.0.1`。

支持 `OPTIONS`、`PROPFIND`、`MKCOL`、`PUT`、`GET`、`HEAD`、`DELETE` 等标准 DAV 操作，与服务器 A 的 WebDAV 测试、图片上传兼容。返回图片 URL 时用户通过 B 访问；`b64_json` 仍由 A 的 API 响应返回。

## 磁盘使用看板

登录后页面顶部显示图片目录文件大小合计、文件数量、所在磁盘总容量、可用空间、已用空间和使用率。每 60 秒自动刷新，支持手动刷新；上传、删除和立即清理后也会刷新。使用率达到 90% 时提示空间紧张。

目录统计包含 `chatgpt2api/images/` 下所有普通文件，不跟随符号链接；文件大小合计不等于文件系统实际分配空间。磁盘数据来自该目录所在的整个文件系统，包含其他服务的用量，不是本应用独占额度；Docker Desktop 下可能显示虚拟磁盘容量。若有目录或文件读取失败，看板会提示统计不完整。

## 定时和立即清理

进入“服务配置 → 存储清理”：

- 定时清理默认关闭，可设置保留时长（1–8760 小时）和检查间隔（1–168 小时）。例如保留 72 小时、每小时检查一次。
- 立即清理可独立输入小时数，确认后执行，不必开启定时清理。
- 显示下次检查时间、上次删除数量、释放空间和失败数量。

按 B 上文件的最后修改时间判断，只清理 `chatgpt2api/images/` 内过期的普通文件及空目录，不跟随符号链接。规则与结果重启后保留。旧文件删除后，图片 URL 会变成 404，不会同步清理 A 的本地副本或图片索引。

## 更新旧版部署

上传覆盖 `compose.yaml` 和整个 `manager/` 目录，保留原项目目录、`.env` 和数据卷。执行：

```bash
docker compose up -d --build --remove-orphans
docker compose ps
```

`--remove-orphans` 会移除旧版额外的容器，释放 8088 端口。已有图片、清理规则与账号密码继续保留；已有账号不会重置为默认账号。新版本运行只需一个应用容器。

## 文件存放与查看日志

图片存放在 Docker 命名卷 `images` 的 `chatgpt2api/images/` 下；配置、密码哈希、登录签名密钥和清理规则在 `settings` 卷。默认项目目录名为 `image-webdav` 时，卷名是 `image-webdav_images` 和 `image-webdav_settings`，不是项目目录内的普通文件。

```bash
docker compose ps
docker compose logs --tail=100 manager
```

不要使用 `docker compose down -v`，它会删除图片和配置卷。

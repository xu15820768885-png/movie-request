# 映单

一个私人影视请求系统：成员通过 TMDB 搜索并提交准确的电影或剧集，管理员手动处理资源、更新状态并留下回复。新请求会通过 Telegram 通知管理员。

## 主要功能

- 管理员和家人独立账号
- TMDB 中文搜索、海报、年份、类型和唯一 ID
- 服务端二次查询 TMDB，无法凭空提交未收录影片
- 待处理、已收到、寻找中、已完成和暂时无法完成
- 管理员文字回复
- Telegram 新需求和状态更新通知
- Telegram 菜单查看“求片需求”和“完成情况”
- Emby API 对照媒体库，已存在资源自动显示“已入库”
- 后台每 5 分钟自动同步 Emby，打开的页面每分钟刷新状态
- SQLite 数据库保存在 NAS 本地
- 手机、平板和电脑自适应界面

## 绿联 NAS 部署

将 `compose.yaml` 保存到 `/volume1/docker/movie-request`，然后在绿联 Docker 的“项目”中导入并部署。也可以在该目录运行：

默认通过 `ghcr.mirrorify.net` 加速拉取公开的 GHCR 镜像，适合国内网络；镜像内容仍来自本项目发布的 `ghcr.io/xu15820768885-png/movie-request:latest`。

Compose 默认将外部 HTTP/HTTPS 请求交给 `http://192.168.31.129:7890`，用于访问 TMDB；局域网段已加入 `NO_PROXY`，访问 NAS 和 Emby 不会绕行代理。如代理地址不同，请修改这三项环境变量。

```bash
docker compose pull
docker compose up -d
```

浏览器访问：

```text
http://NAS-IP:1802
```

数据保存在：

```text
/volume1/docker/movie-request/data
```

首次打开时创建管理员账号并填写 TMDB API Read Access Token。之后在“家人账号”中创建登录账号，在“系统设置”中配置 Telegram 和 Emby。

Emby 地址可以填写 `http://NAS-IP:8096`。建议在 Emby 后台专门创建一个供本系统使用的 API 密钥，方便以后单独撤销。

如果 Telegram 无法直连，在系统设置中填写 Mihomo HTTP 代理，例如 `http://192.168.31.129:7890`。新需求通知、机器人菜单和消息轮询都会使用该代理。

## 更新

```bash
docker compose pull
docker compose up -d
```

数据库位于 NAS 映射目录中，更新容器不会删除账号和求片记录。每次推送到 GitHub 的 `main` 分支都会自动构建并发布新的 Docker 镜像。

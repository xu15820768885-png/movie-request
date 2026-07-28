# 影巢固定公网出口

这套配置只转发影巢 API 的小型 HTTPS 请求，不承载影片数据。1 核 1G 云服务器足够使用。

## 云服务器部署

1. 把本目录复制到云服务器，例如 `/opt/hdhive-egress`。
2. 生成一个只用于代理的高强度密码文件：

   ```bash
   cd /opt/hdhive-egress
   docker run --rm httpd:2.4-alpine htpasswd -nbB hdhive '请替换为至少24位随机密码' > passwords
   # 文件里只有不可逆的密码哈希；Squid 的 proxy 用户需要读取它。
   chmod 644 passwords
   ```

3. 启动：

   ```bash
   docker compose up -d
   ```

   模板限制代理最多使用 128MB 内存、25% 单核和 128 个进程，并把
   Docker 日志轮转限制为 2 个 5MB 文件。访问明细日志默认关闭。

4. 云服务器安全组开放 TCP `3128`。如果能确定家中出口 IP，安全组进一步只允许该 IP；否则必须使用上面的随机强密码。
5. 映单管理员页面填写：

   ```text
   http://hdhive:随机强密码@38.55.106.163:3128
   ```

影巢客户端不会继承 NAS 的 `HTTP_PROXY`/`HTTPS_PROXY`，因此 TMDB、Telegram 和癫影继续沿用原有网络设置，只有影巢走这个固定公网出口。

## 验证

审核通过前可以先从 NAS 容器验证出口 IP：

```bash
curl -x 'http://hdhive:随机强密码@38.55.106.163:3128' https://api.ipify.org
```

返回值应为云服务器公网 IP `38.55.106.163`。不要把真实代理密码提交到 GitHub。

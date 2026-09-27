# SUSTech 羽毛球预约工具

本项目提供羽毛球场预约查询、Token 自动抓取、场地刷新等功能。

整体网络结构：

```text
企业微信预约页面
        │
        ▼
     Clash
        │
        ├── reservation.sustech.edu.cn
        │          │
        │          ▼
        │     mitmproxy :8080
        │          │
        │          ▼
        │   reservation 服务器
        │
        └── 其他网络流量
                   │
                   ▼
              Clash 原规则

浏览器 / 手机
        │
        ▼
   FastAPI :8000
```

其中：

- `8000`：本项目 Web 服务端口
- `8080`：mitmproxy 本地代理端口
- mitmproxy 只用于监听 `reservation.sustech.edu.cn`
- 其他网站不应转发到 `8080`
- Token 抓取流程为：刷新企业微信预约页面 → mitmproxy 捕获请求 → 写入 `config.toml`

---

## 1. 环境要求

推荐：

```text
Windows 10 / 11
Python 3.11+
uv
Clash Verge / Mihomo
Tailscale（可选，用于手机远程访问）
企业微信桌面端
```

进入项目目录后安装依赖。

如果项目已经带有 `pyproject.toml`：

```powershell
uv sync
```

否则根据项目实际依赖安装。

确认 Python：

```powershell
uv run python --version
```

确认 mitmproxy：

```powershell
uv run mitmdump --version
```

---

## 2. 配置 config.toml

复制或修改：

```text
config.toml
```

填写当前用户、场馆和预约配置。

示意：

```toml
[user]
id = "..."
customer_id = "..."
name = "..."
tel = "..."

[gym]
id = "..."
name = "..."

[order]
offset_days = 1
token = ""

[courts]
1 = "..."
2 = "..."
```

建议初次部署时：

```toml
token = ""
```

Token 由程序自动捕获并写入。

注意：

```text
config.toml 可能包含个人信息，不建议提交到公开 Git 仓库。
```

建议加入：

```gitignore
config.toml
```

或者提供一个：

```text
config.example.toml
```

供新设备复制。

---

# 3. 安装 mitmproxy 根证书

这是迁移到新电脑时最容易遗漏的一步。

mitmproxy 第一次运行后通常会在：

```text
%USERPROFILE%\.mitmproxy
```

生成：

```text
mitmproxy-ca-cert.cer
mitmproxy-ca-cert.p12
mitmproxy-ca-cert.pem
mitmproxy-ca.p12
mitmproxy-ca.pem
mitmproxy-dhparam.pem
```

例如：

```text
C:\Users\你的用户名\.mitmproxy
```

## 推荐：安装到 Windows 计算机根证书库

使用管理员 PowerShell：

```powershell
certutil -addstore Root "$env:USERPROFILE\.mitmproxy\mitmproxy-ca-cert.cer"
```

检查：

```powershell
certutil -store Root | findstr /i mitmproxy
```

能够看到：

```text
O=mitmproxy, CN=mitmproxy
```

即可。

也可以检查证书指纹：

```powershell
(Get-PfxCertificate "$env:USERPROFILE\.mitmproxy\mitmproxy-ca-cert.cer").Thumbprint
```

以及：

```powershell
Get-ChildItem Cert:\LocalMachine\Root |
  Where-Object {$_.Subject -like "*mitmproxy*"} |
  Format-List Subject, Thumbprint, NotBefore, NotAfter
```

两边 Thumbprint 应能对应。

---

## 换 Windows 账户时特别注意

mitmproxy 默认目录跟 Windows 用户有关：

```text
C:\Users\旧账户\.mitmproxy
```

换账户后会变成：

```text
C:\Users\新账户\.mitmproxy
```

因此不要假设旧账户生成的证书会自动被新账户使用。

最稳妥的做法是：

1. 在新账户启动一次 mitmproxy
2. 让它重新生成 `.mitmproxy`
3. 将新生成的 CA 安装到 Windows Root
4. 重启企业微信

如果希望固定使用同一个证书目录，可以显式指定 mitmproxy `confdir`。

---

# 4. 配置 Clash / Mihomo

不要把 Windows 全局代理直接设置成：

```text
127.0.0.1:8080
```

否则所有 HTTPS 流量都会经过 mitmproxy。

本项目只需要：

```text
reservation.sustech.edu.cn
```

经过 mitmproxy。

推荐结构：

```text
企业微信
   ↓
Clash
   ├── reservation.sustech.edu.cn → 127.0.0.1:8080
   └── 其他域名 → 原 Clash 规则
```

也就是说：

```text
8080 只处理预约网站
```

而不是作为整个系统的全局代理。

如果配置错误造成：

```text
mitmproxy → Clash → mitmproxy
```

会形成代理环路。

---

# 5. 启动后端

推荐：

```powershell
uv run uvicorn app:app --host 0.0.0.0 --port 8000
```

关键是：

```text
--host 0.0.0.0
```

不要只监听：

```text
127.0.0.1
```

否则只能电脑本机访问。

确认：

```powershell
netstat -ano | findstr :8000
```

正常应看到类似：

```text
0.0.0.0:8000
```

而不是：

```text
127.0.0.1:8000
```

本机测试：

```text
http://127.0.0.1:8000
```

---

# 6. Windows 防火墙

如果只在本机使用：

```text
http://127.0.0.1:8000
```

通常不需要额外开放防火墙。

但如果需要：

- 手机通过 Tailscale 访问
- 局域网其他电脑访问
- 其他设备访问该 FastAPI 服务

则需要允许 TCP `8000` 入站。

管理员 PowerShell：

```powershell
New-NetFirewallRule `
  -DisplayName "Court Server 8000" `
  -Direction Inbound `
  -Protocol TCP `
  -LocalPort 8000 `
  -Action Allow
```

检查：

```powershell
Get-NetFirewallRule -DisplayName "Court Server 8000"
```

删除：

```powershell
Remove-NetFirewallRule -DisplayName "Court Server 8000"
```

因此：

```text
迁移到新电脑后，如果还希望手机访问，防火墙规则需要重新创建。
```

防火墙规则不会跟项目文件一起迁移。

---

# 7. Tailscale 手机访问

这一步仅在需要手机远程打开预约界面时配置。

电脑和手机登录同一个 Tailnet。

电脑执行：

```powershell
tailscale ip -4
```

例如：

```text
100.x.x.x
```

然后手机访问：

```text
http://100.x.x.x:8000
```

注意：

```text
使用 http://
```

不是：

```text
https://
```

本项目的 FastAPI 默认没有配置 HTTPS。

---

## Tailscale 排查顺序

首先电脑本机测试：

```text
http://127.0.0.1:8000
```

再测试：

```text
http://电脑的Tailscale-IP:8000
```

例如：

```powershell
curl http://100.x.x.x:8000/api/mitm/state
```

判断方式：

```text
127.0.0.1 能访问
但 Tailscale IP 不能访问

→ 检查 uvicorn 是否使用 --host 0.0.0.0
```

如果：

```text
电脑通过 Tailscale IP 能访问
但手机不能访问

→ 优先检查 Windows 防火墙
→ 再检查 Tailscale 是否连接
```

查看 Tailnet：

```powershell
tailscale status
```

测试设备连接：

```powershell
tailscale ping <手机的Tailscale-IP>
```

---

# 8. 检查 mitmproxy

程序启动后应监听：

```text
127.0.0.1:8080
```

测试：

```powershell
curl.exe -x http://127.0.0.1:8080 `
  https://reservation.sustech.edu.cn/ `
  -v -o NUL
```

如果能够得到：

```text
HTTP/1.1 200 OK
```

说明：

```text
Windows
→ mitmproxy
→ reservation.sustech.edu.cn
```

这条 HTTPS 链路已经正常。

---

# 9. Token 自动获取

确保：

1. 企业微信已经登录
2. 已打开预约页面
3. Clash 已启用对应规则
4. mitmproxy 正在运行
5. mitmproxy CA 已信任

然后在 Web 页面点击：

```text
自动获取 Token
```

程序会：

```text
找到企业微信预约窗口
        ↓
发送 Ctrl+R
        ↓
页面刷新
        ↓
请求经过 Clash
        ↓
reservation 域名进入 mitmproxy
        ↓
提取请求中的 token
        ↓
写入 config.toml
```

日志出现：

```text
[TOKEN] captured: xxxxxxxx...
```

即代表成功。

---

# 10. 关于 TLS `unknown ca`

日志偶尔可能出现：

```text
Client TLS handshake failed.
The client does not trust the proxy's certificate
(tlsv1 alert unknown ca)
```

如果同时能够稳定看到：

```text
[TOKEN] captured
```

并且下面的测试：

```powershell
curl.exe -x http://127.0.0.1:8080 `
  https://reservation.sustech.edu.cn/ `
  -v -o NUL
```

能够得到：

```text
HTTP/1.1 200 OK
```

则说明：

```text
mitmproxy 主链路正常
Windows 信任当前 mitmproxy CA
预约 Token 请求能够成功解密
```

此时部分 `unknown ca` 很可能来自企业微信内部其他网络组件，它们可能采用不同的证书信任机制。

只要目标 Token 请求能稳定捕获，可以暂时忽略这些失败连接。

---

# 11. 多份 mitmproxy

系统中可能同时存在：

```text
uv tool 安装的 mitmproxy
项目 .venv 中安装的 mitmproxy
```

检查：

```powershell
where.exe mitmdump
```

或者：

```powershell
Get-Command mitmdump -All
```

项目后端会优先使用当前 Python 环境附近的 `mitmdump.exe`。

多个 mitmproxy 安装本身通常没有问题。

真正重要的是：

```text
它们使用的是哪一个 confdir / CA
```

通常默认：

```text
%USERPROFILE%\.mitmproxy
```

只要使用同一个 Windows 用户和同一个 `.mitmproxy` 目录，通常会共用同一套 CA。

---

# 12. 新电脑迁移 Checklist

复制项目后依次检查：

```text
[ ] 安装 Python / uv

[ ] uv sync / 安装项目依赖

[ ] 确认 config.toml
    不要直接公开提交个人信息

[ ] 安装 / 确认 Clash

[ ] 配置 reservation.sustech.edu.cn
    单域名转发至 127.0.0.1:8080

[ ] 启动一次 mitmproxy
    生成 %USERPROFILE%\.mitmproxy

[ ] 安装 mitmproxy 根证书到 Windows Root

[ ] 完全退出并重新启动企业微信

[ ] 启动后端：
    uv run uvicorn app:app --host 0.0.0.0 --port 8000

[ ] 本机访问：
    http://127.0.0.1:8000

[ ] 测试 mitmproxy：
    curl.exe -x http://127.0.0.1:8080 https://reservation.sustech.edu.cn/

[ ] 测试自动获取 Token

[ ] 如需手机访问：
    安装并登录 Tailscale

[ ] Windows 防火墙开放 TCP 8000

[ ] 获取 Tailscale IP：
    tailscale ip -4

[ ] 手机访问：
    http://<Tailscale-IP>:8000
```

---

# 13. 最常用故障判断

## 本机 localhost 无法访问

```text
127.0.0.1:8000 ❌
```

检查：

```text
FastAPI 是否启动
8000 是否被占用
Python 是否报错
```

---

## localhost 能访问，但电脑自己的 IP 不能访问

```text
127.0.0.1:8000 ✅
100.x.x.x:8000 ❌
```

检查：

```text
uvicorn 是否使用：
--host 0.0.0.0
```

---

## 电脑自己的 Tailscale IP 能访问，手机不能

```text
电脑访问 100.x.x.x:8000 ✅
手机访问 100.x.x.x:8000 ❌
```

优先检查：

```text
Windows Firewall TCP 8000
Tailscale 是否在线
Tailnet ACL
手机是否同时启用了其他 VPN
```

---

## Token 一直超时

检查日志是否出现：

```text
[TOKEN] captured
```

如果没有，依次检查：

```text
企业微信预约窗口是否打开
        ↓
Ctrl+R 是否成功
        ↓
Clash 单域名规则是否命中
        ↓
mitmproxy 是否监听 8080
        ↓
mitmproxy CA 是否被信任
```

---

## 推荐最终部署结构

```text
Windows
│
├── 项目
│   ├── app.py
│   ├── mitm_addon.py
│   ├── index.html
│   └── config.toml
│
├── .venv
│   └── mitmdump.exe
│
├── C:\Users\<username>\.mitmproxy
│   └── mitmproxy CA
│
├── Clash
│   └── reservation.sustech.edu.cn → 127.0.0.1:8080
│
├── Tailscale
│   └── 手机访问 :8000
│
└── Windows Firewall
    └── Allow TCP 8000
```

迁移时最重要的一句话：

> **项目代码可以复制，但证书信任、Clash 规则、Windows 防火墙和 Tailscale 登录都属于机器环境，需要在新电脑重新配置。**
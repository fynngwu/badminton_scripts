下面是用 **uv** 配置后的精简版教程，可直接替换原文。已去掉按顺序测试、常见错误等内容。

---

# Windows 10 / 11 部署教程（uv 精简版）

目标：全新电脑从零配置到可以运行：

```powershell
python app.py
```

项目地址：

```text
https://github.com/fynngwu/badminton_scripts
```

---

## 0. 新电脑必须重新配置的内容

这些不会跟着 Git 仓库自动迁移：

1. Python 环境和依赖包
2. `config.toml` 中的个人信息
3. mitmproxy 根证书
4. Clash Verge / Mihomo 转发规则
5. 企业微信登录状态和预约页面
6. 如需手机访问，还要配置防火墙 / Tailscale

不要直接复制旧电脑的 `.venv`、`.mitmproxy`、`config.toml`。

---

## 1. 安装基础软件和 uv

需要：

- Windows 10 / 11
- Git
- 企业微信 Windows 客户端
- Clash Verge Rev / 其他 Mihomo 客户端

Python 交给 uv 管理。

安装 uv，任选一种：

```powershell
winget install --id=astral-sh.uv -e
```

或：

```powershell
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

安装 Python 3.11：

```powershell
uv python install 3.11
uv --version
```

---

## 2. 下载项目

```powershell
git clone https://github.com/fynngwu/badminton_scripts.git
cd badminton_scripts
```

项目至少应包含：

```text
app.py
engine.py
utils.py
mitm_addon.py
index.html
fast_db.npz
requirements.txt
config.example.toml
```

不要只复制 `app.py`。

---

## 3. 用 uv 创建环境并安装依赖

在项目目录执行：

```powershell
uv venv --python 3.11
.\.venv\Scripts\Activate.ps1
```

如果 PowerShell 禁止执行脚本：

```powershell
Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
```

重新打开 PowerShell 后再次激活：

```powershell
.\.venv\Scripts\Activate.ps1
```

安装依赖：

```powershell
uv pip install -r requirements.txt
uv pip install pywin32
```

检查：

```powershell
python -c "import fastapi, cv2, httpx, mitmproxy, win32gui; print('dependencies OK')"
```

输出 `dependencies OK` 即可。

---

## 4. 创建 config.toml

```powershell
Copy-Item config.example.toml config.toml
```

编辑 `config.toml`，重点修改：

```toml
[user]
id          = "YOUR_USER_ID"
customer_id = "YOUR_CUSTOMER_ID"
name        = "YOUR_NAME"
tel         = "YOUR_PHONE"

[order]
offset_days = 1
token = ""
```

第一次部署保持：

```toml
token = ""
```

不要复制别人的 user id、customer_id、姓名、手机号、token。

如果还是同一个润扬羽毛球馆，场馆 ID 和场地 ID 可保留示例值。

---

## 5. 生成并安装 mitmproxy CA 证书

激活 `.venv` 后运行：

```powershell
mitmdump --listen-host 127.0.0.1 --listen-port 8080
```

第一次会生成：

```text
C:\Users\<你的Windows用户名>\.mitmproxy\
```

看到证书文件后按 `Ctrl + C` 退出。

以管理员身份打开 PowerShell，安装证书：

```powershell
certutil -addstore Root "$env:USERPROFILE\.mitmproxy\mitmproxy-ca-cert.cer"
```

检查：

```powershell
certutil -store Root | findstr /i mitmproxy
```

安装证书后，必须完全退出企业微信，再重新打开，重新进入预约页面。

---

## 6. 配置 Clash：只把预约域名转发到 mitmproxy

目标链路：

```text
企业微信预约页面
        ↓
Clash
        ↓
reservation.sustech.edu.cn → mitmproxy 127.0.0.1:8080
        ↓
预约系统服务器
```

在 Clash Verge Rev 左侧“订阅”中的“全局扩展脚本”加入：

```javascript
function main(config) {
  const name = "MITM-Reservation";

  config.proxies = (config.proxies || []).filter(p => p.name !== name);
  config.proxies.push({
    name,
    type: "http",
    server: "127.0.0.1",
    port: 8080
  });

  const rule = "DOMAIN,reservation.sustech.edu.cn,MITM-Reservation";
  config.rules = (config.rules || []).filter(r => r !== rule);
  config.rules.unshift(rule);

  return config;
}
```

保存并重新加载 Clash 配置。

不要把所有系统代理都改成 `127.0.0.1:8080`。

---

## 7. 准备企业微信预约页面

登录企业微信，进入：

```text
reservation.sustech.edu.cn
```

对应的羽毛球预约页面。

自动抓 Token 时，这个窗口必须存在。程序会寻找标题包含 `reservation` 的窗口，切到前台并发送 `Ctrl + R`。

注意：

- 企业微信必须已登录
- 预约页面必须已打开
- 自动刷新时不要锁屏

---

## 8. 启动程序

回到项目目录：

```powershell
cd <项目目录>
.\.venv\Scripts\Activate.ps1
python app.py
```

正常会看到：

```text
Uvicorn running on http://0.0.0.0:8000
```

浏览器打开：

```text
http://127.0.0.1:8000
```

网页中按需操作：

1. 自动获取 Token
2. 刷新场地
3. 启动 VID
4. 按需开启全自动模式

Token 会自动写入 `config.toml`。

---

## 9. 手机访问网页（可选）

电脑和手机安装 Tailscale，登录同一个 Tailnet。

电脑执行：

```powershell
tailscale ip -4
```

例如得到：

```text
100.88.xx.xx
```

管理员 PowerShell 添加防火墙规则：

```powershell
New-NetFirewallRule `
  -DisplayName "Badminton Server 8000" `
  -Direction Inbound `
  -Protocol TCP `
  -LocalPort 8000 `
  -Action Allow
```

手机打开：

```text
http://100.88.xx.xx:8000
```

注意是 `http://`，不是 `https://`。

---

## 10. 日常使用

以后只需要：

```powershell
cd <项目目录>
.\.venv\Scripts\Activate.ps1
python app.py
```

然后：

1. 登录企业微信
2. 打开预约页面
3. 打开 Clash
4. 浏览器打开 `http://127.0.0.1:8000`
5. 点击“自动获取 Token”
6. 点击“刷新场地”
7. 按需启动 VID / 全自动模式

---

## 11. 更新代码

```powershell
cd <项目目录>
git pull
```

如果依赖有变化：

```powershell
.\.venv\Scripts\Activate.ps1
uv pip install -r requirements.txt
uv pip install pywin32
```

自己的 `config.toml` 被 `.gitignore` 忽略，正常不会被覆盖。

---

## 最终确认

```text
[ ] uv 已安装，Python 3.11 已由 uv 安装
[ ] .venv 已创建，依赖安装成功
[ ] config.toml 已创建，个人字段已修改，token 留空
[ ] fast_db.npz 存在
[ ] mitmproxy CA 已加入 Windows Root
[ ] 安装 CA 后企业微信已完全重启
[ ] Clash 只把 reservation.sustech.edu.cn 转发到 127.0.0.1:8080
[ ] 企业微信已登录，预约页面已打开
[ ] python app.py 能启动，http://127.0.0.1:8000 能打开
[ ] 自动获取 Token、刷新场地、VID 正常
[ ] 如需手机访问，再配置 Tailscale 和防火墙
```
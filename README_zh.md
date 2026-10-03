# 🏸 SUSTech 羽毛球预约脚本：新电脑部署指南

> 适用于 Windows 10 / Windows 11。  
> 目标：在一台全新的电脑上，从零配置到可以直接运行：
>
> ```powershell
> python app.py
> ```
>
> 项目地址：`https://github.com/fynngwu/badminton_scripts`

---

## 0. 先看这里：新电脑需要重新配置什么？

代码从 GitHub 克隆下来只是第一步。下面这些属于“电脑环境”，**不会跟着 Git 仓库自动迁移**：

1. Python 环境和 Python 包
2. `config.toml` 中当前使用者的信息
3. mitmproxy 根证书
4. Clash Verge / Mihomo 的转发规则
5. 企业微信登录状态和预约页面
6. 如果需要手机远程访问，还要重新配置 Windows 防火墙 / Tailscale

推荐先完整按照本文执行一次，不要直接复制旧电脑的 `.venv`、`.mitmproxy` 或 `config.toml`。

---

# 1. 安装基础软件

至少需要：

- Windows 10 / 11
- Git
- Python 3.11
- 企业微信 Windows 客户端
- Clash Verge Rev / 其他 Mihomo 客户端

推荐使用 **Python 3.11 64-bit**。

安装 Python 时一定勾选：

```text
Add python.exe to PATH
```

安装完成后打开 PowerShell：

```powershell
python --version
pip --version
git --version
```

推荐看到类似：

```text
Python 3.11.x
```

如果 `python` 找不到，可以尝试：

```powershell
py -3.11 --version
```

---

# 2. 下载项目

在准备放代码的目录打开 PowerShell：

```powershell
git clone https://github.com/fynngwu/badminton_scripts.git
cd badminton_scripts
```

项目正常应该至少包含：

```text
badminton_scripts/
├─ app.py
├─ engine.py
├─ utils.py
├─ mitm_addon.py
├─ index.html
├─ fast_db.npz
├─ requirements.txt
├─ config.example.toml
└─ ...
```

其中：

- `app.py`：FastAPI Web 服务入口
- `engine.py`：状态机、VID 池、自动预约等运行逻辑
- `utils.py`：请求、验证码、窗口控制、mitmproxy 等底层功能
- `mitm_addon.py`：mitmproxy 抓 Token 的插件
- `index.html`：网页前端
- `fast_db.npz`：验证码识别所需数据库
- `config.toml`：本机用户配置，不应提交到 GitHub

**不要只复制 `app.py`。**

---

# 3. 创建 Python 虚拟环境

推荐每台电脑创建自己的 `.venv`。

在项目目录执行：

```powershell
python -m venv .venv
```

激活：

```powershell
.\.venv\Scripts\Activate.ps1
```

成功后 PowerShell 前面通常会出现：

```text
(.venv)
```

如果 PowerShell 提示禁止执行脚本，可以只对当前用户执行：

```powershell
Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
```

然后重新打开 PowerShell，再执行：

```powershell
.\.venv\Scripts\Activate.ps1
```

---

# 4. 安装依赖

先升级 pip：

```powershell
python -m pip install -U pip
```

然后：

```powershell
pip install -r requirements.txt
```


安装完成后检查：

```powershell
python -c "import fastapi, cv2, httpx, mitmproxy, win32gui; print('dependencies OK')"
```

如果输出：

```text
dependencies OK
```

说明主要依赖已经正常。

---

# 5. 创建自己的 config.toml

仓库不会提交真实的 `config.toml`，这是故意的，因为其中包含个人信息和 Token。

执行：

```powershell
Copy-Item config.example.toml config.toml
```

然后用 VS Code / 记事本打开：

```text
config.toml
```

重点修改：

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

第一次部署建议：

```toml
token = ""
```

后面由程序自动抓取 Token。

## 不要做这件事

不要把别人的：

```text
user id
customer_id
姓名
手机号
token
```

原样复制过来。

这些字段对应的是具体用户。

场馆 ID 和场地 ID 如果仍然使用同一个润扬羽毛球馆，一般可以保留仓库示例中的值。

---

# 6. 第一次启动 mitmproxy，生成 CA 证书

激活 `.venv` 后：

```powershell
mitmdump --listen-host 127.0.0.1 --listen-port 8080
```

第一次启动后，mitmproxy 会生成：

```text
C:\Users\<你的Windows用户名>\.mitmproxy\
```

里面应包含类似：

```text
mitmproxy-ca-cert.cer
mitmproxy-ca-cert.pem
mitmproxy-ca.pem
...
```

看到后按：

```text
Ctrl + C
```

退出 mitmdump。

---

# 7. 安装 mitmproxy 根证书

这是换电脑时最容易遗漏的一步。

以 **管理员身份** 打开 PowerShell，执行：

```powershell
certutil -addstore Root "$env:USERPROFILE\.mitmproxy\mitmproxy-ca-cert.cer"
```

检查：

```powershell
certutil -store Root | findstr /i mitmproxy
```

如果能看到类似：

```text
O=mitmproxy, CN=mitmproxy
```

说明证书已安装。

### 重要

安装证书以后，建议：

1. 完全退出企业微信
2. 再重新打开企业微信
3. 重新进入羽毛球预约网页

不要只关闭预约小窗口。

---

# 8. 配置 Clash Verge Rev

脚本抓 Token 的网络路径应该是：

```text
企业微信预约页面
        │
        ▼
      Clash
        │
        ├── reservation.sustech.edu.cn
        │               │
        │               ▼
        │       mitmproxy 127.0.0.1:8080
        │               │
        │               ▼
        │        预约系统服务器
        │
        └── 其他网站
                │
                ▼
           Clash 原来的规则
```

也就是说：

> **只把 reservation.sustech.edu.cn 交给 mitmproxy，不要把所有网络都送到 8080。**

在 Clash Verge Rev 的左边栏目的订阅中“全局扩展脚本”中，可以加入：

```javascript
function main(config) {
  const mitmProxy = {
    name: "MITM-Reservation",
    type: "http",
    server: "127.0.0.1",
    port: 8080
  };

  config.proxies = config.proxies || [];

  // 防止重复添加
  config.proxies = config.proxies.filter(
    p => p.name !== "MITM-Reservation"
  );

  config.proxies.push(mitmProxy);

  config.rules = config.rules || [];

  const reservationRule =
    "DOMAIN,reservation.sustech.edu.cn,MITM-Reservation";

  // 防止重复规则
  config.rules = config.rules.filter(
    r => r !== reservationRule
  );

  // 放到最前面，确保优先命中
  config.rules.unshift(reservationRule);

  return config;
}
```

保存并重新加载 Clash 配置。

### 不要这样做

不要把 Windows 系统代理直接改成：

```text
127.0.0.1:8080
```

8080 只是 mitmproxy 的本地监听端口。

正常情况下 Windows / 企业微信仍然走 Clash，只是 Clash 把预约域名单独转给 mitmproxy。

---

# 9. 确认企业微信预约页面已经打开

登录企业微信，然后进入：

```text
reservation.sustech.edu.cn
```

对应的羽毛球预约页面。

**自动抓 Token 时这个窗口必须存在。**

程序会寻找标题中包含：

```text
reservation.sustech.edu.cn
```

或：

```text
reservation
```

的窗口，然后自动将窗口切到前台并发送：

```text
Ctrl + R
```

因此：

- 企业微信必须处于登录状态
- 预约页面必须已经打开
- 不建议把预约窗口彻底关闭
- 自动刷新时不要锁屏

---

# 10. 启动程序

回到项目目录，确认虚拟环境已激活：

```powershell
.\.venv\Scripts\Activate.ps1
```

运行：

```powershell
python app.py
```

正常情况下会看到类似：

```text
Uvicorn running on http://0.0.0.0:8000
```

浏览器打开：

```text
http://127.0.0.1:8000
```

即可进入控制页面。

`python app.py` 会让 FastAPI 监听：

```text
0.0.0.0:8000
```

因此既可以本机访问，也可以在正确配置防火墙后从其他设备访问。

---

# 11. 第一次不要直接抢场，按这个顺序测试

建议第一次部署按照下面顺序检查。

## Test 1：网页能不能打开

打开：

```text
http://127.0.0.1:8000
```

如果页面正常出现，说明：

```text
Python
FastAPI
app.py
engine.py
index.html
```

基本正常。

---

## Test 2：mitmproxy 是否启动

程序运行后执行：

```powershell
netstat -ano | findstr :8080
```

正常应看到：

```text
127.0.0.1:8080
```

处于监听状态。

也可以在网页上查看 mitm 状态。

---

## Test 3：直接测试 mitmproxy HTTPS

另开一个 PowerShell：

```powershell
curl.exe -x http://127.0.0.1:8080 `
  https://reservation.sustech.edu.cn/ `
  -v -o NUL
```

如果 HTTPS 能正常建立并返回网页响应，说明：

```text
电脑
  ↓
mitmproxy
  ↓
reservation.sustech.edu.cn
```

链路基本正常。

如果出现：

```text
unknown ca
certificate verify failed
```

优先重新检查第 7 步的 mitmproxy CA 安装。

---

## Test 4：自动获取 Token

保证：

```text
企业微信已登录
预约页面已打开
Clash 正常运行
程序正在运行
```

然后网页点击：

```text
🔑 自动获取 Token
```

正常流程应该是：

```text
点击按钮
   ↓
程序找到企业微信预约窗口
   ↓
窗口被切到前台
   ↓
自动 Ctrl+R
   ↓
预约请求经过 Clash
   ↓
Clash 将 reservation 域名送到 127.0.0.1:8080
   ↓
mitmproxy 捕获请求
   ↓
mitm_addon.py 提取 Token
   ↓
Token 写入 config.toml
```

如果成功，页面 / 日志应该能够看到 Token 已更新。

此时也可以直接打开：

```text
config.toml
```

确认：

```toml
token = "..."
```

已经自动变化。

---

## Test 5：刷新场地

Token 正常后点击：

```text
刷新场地
```

如果网页能够显示各个球场的空闲 / 占用状态，说明：

```text
Token
用户 ID
场地 ID
getOrder API
```

均已经正常。

---

## Test 6：VID 验证码

点击：

```text
启动 VID
```

查看 VID 缓存是否开始增加。

如果这里报：

```text
fast_db.npz not found
```

说明项目文件不完整。

请确认：

```text
fast_db.npz
```

与：

```text
utils.py
```

位于同一个项目目录。

---

# 12. 最常见错误

## ① `ModuleNotFoundError: No module named 'win32gui'`

执行：

```powershell
pip install pywin32
```

并确认自己是在项目的 `.venv` 中运行。

检查：

```powershell
where.exe python
```

应该优先指向：

```text
...\badminton_scripts\.venv\Scripts\python.exe
```

---

## ② `No module named fastapi / cv2 / mitmproxy`

重新激活：

```powershell
.\.venv\Scripts\Activate.ps1
```

再执行：

```powershell
pip install -r requirements.txt
pip install pywin32
```

---

## ③ `fast_db.npz` 找不到

重新：

```powershell
git pull
```

并检查：

```powershell
Get-ChildItem fast_db.npz
```

不要只复制 Python 文件。

---

## ④ 网页打不开

先看：

```powershell
netstat -ano | findstr :8000
```

然后访问：

```text
http://127.0.0.1:8000
```

如果 8000 已被其他程序占用：

```powershell
netstat -ano | findstr :8000
```

记下 PID，再：

```powershell
tasklist | findstr <PID>
```

---

## ⑤ 自动获取 Token 提示找不到窗口

确认：

1. 企业微信已打开
2. 预约网页已经进入
3. 页面窗口标题与 reservation 有关
4. Windows 当前没有锁屏

也可以手工测试窗口刷新逻辑：

```powershell
python mitm_addon.py refresh
```

如果正常，企业微信预约窗口应该被切到前台并刷新。

---

## ⑥ Token 一直获取不到

按顺序排查：

```text
企业微信预约页面是否打开
        ↓
python mitm_addon.py refresh 是否能刷新窗口
        ↓
8080 是否监听
        ↓
Clash reservation 规则是否命中
        ↓
mitmproxy CA 是否安装
        ↓
企业微信是否在安装证书后彻底重启
```

---

## ⑦ mitmproxy 显示 `Client TLS handshake failed / unknown ca`

首先不要只看日志，要测试真正需要的预约域名：

```powershell
curl.exe -x http://127.0.0.1:8080 `
  https://reservation.sustech.edu.cn/ `
  -v -o NUL
```

如果 reservation 请求本身可以被正常代理，并且 Token 能抓到，那么部分企业微信内部的其他 TLS 请求失败通常不影响脚本使用。

如果 reservation 自己也失败，则重新安装 CA 并重启企业微信。

---

## ⑧ Clash 开启后网络异常

最常见原因是代理环路：

```text
Clash
  ↓
mitmproxy
  ↓
Clash
  ↓
mitmproxy
  ↓
...
```

确保：

- 只对 `reservation.sustech.edu.cn` 使用 `MITM-Reservation`
- 不要把整个系统代理改成 `127.0.0.1:8080`
- 如果使用 Clash TUN 模式后出现循环，先关闭 TUN，只保留普通系统代理验证流程

先把最简单的链路跑通，再考虑 TUN。

---

# 13. 如果需要手机访问网页

只在电脑本机用的话，这部分不用配置。

如果希望手机打开控制页面，推荐 Tailscale。

电脑和手机安装 Tailscale，并登录同一个 Tailnet。

电脑执行：

```powershell
tailscale ip -4
```

例如得到：

```text
100.88.xx.xx
```

管理员 PowerShell 添加 Windows 防火墙规则：

```powershell
New-NetFirewallRule `
  -DisplayName "Badminton Server 8000" `
  -Direction Inbound `
  -Protocol TCP `
  -LocalPort 8000 `
  -Action Allow
```

然后手机打开：

```text
http://100.88.xx.xx:8000
```

注意是：

```text
http://
```

不是：

```text
https://
```

---

# 14. 日常使用只需要这几步

以后电脑已经配置好后，一般只需要：

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
7. 再按需要启动 VID / 全自动模式

---

# 15. 更新代码

作者更新 GitHub 后：

```powershell
cd <项目目录>
git pull
```

如果 `requirements.txt` 有变化，再执行：

```powershell
pip install -r requirements.txt
pip install pywin32
```

自己的：

```text
config.toml
```

被 `.gitignore` 忽略，正常情况下不会被 `git pull` 覆盖。

---

# 16. 一键自检命令

遇到问题时，可以依次把下面这些命令的输出发给维护者：

```powershell
python --version
where.exe python
pip --version
git status
python -c "import fastapi, cv2, httpx, mitmproxy, win32gui; print('Python deps OK')"
Get-ChildItem fast_db.npz
netstat -ano | findstr :8000
netstat -ano | findstr :8080
certutil -store Root | findstr /i mitmproxy
```

再测试：

```powershell
curl.exe -x http://127.0.0.1:8080 `
  https://reservation.sustech.edu.cn/ `
  -v -o NUL
```

这样通常很快就能定位问题是在：

```text
Python
依赖
项目文件
FastAPI
mitmproxy
证书
Clash
企业微信
```

中的哪一层。

---

# 17. 新电脑最终 Checklist

部署完成前逐项确认：

```text
[ ] 安装 Git
[ ] 安装 Python 3.11 64-bit
[ ] git clone 项目
[ ] 创建 .venv
[ ] pip install -r requirements.txt

[ ] config.example.toml → config.toml
[ ] 修改成自己的 user / customer 信息
[ ] token 初始留空

[ ] fast_db.npz 存在

[ ] mitmdump 至少手动启动过一次
[ ] %USERPROFILE%\.mitmproxy 已生成
[ ] mitmproxy CA 已加入 Windows Root

[ ] Clash 已加入 MITM-Reservation 节点
[ ] reservation.sustech.edu.cn 已路由到 127.0.0.1:8080
[ ] 没有把整个系统直接代理到 8080

[ ] 企业微信已登录
[ ] 企业微信预约页面已打开
[ ] 安装 CA 后企业微信已经重启

[ ] python app.py 能启动
[ ] http://127.0.0.1:8000 能打开

[ ] 自动获取 Token 成功
[ ] 刷新场地成功
[ ] VID 求解正常

[ ] 如需手机访问，再配置 Tailscale
[ ] 如需手机访问，Windows 防火墙开放 TCP 8000
```

---

## 最后一句

迁移时最容易误以为“GitHub 拉下来就能跑”，但这个项目实际上有三层：

```text
代码层
  app.py / engine.py / utils.py / fast_db.npz
          ↓
Python 环境层
  FastAPI / OpenCV / mitmproxy / pywin32
          ↓
Windows 网络与桌面环境层
  mitmproxy CA / Clash / 企业微信窗口
```

三层都正常，`python app.py` 才会真正完整工作。

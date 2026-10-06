# uninstall.config.ps1
# ============================================================
# 1 = 执行卸载/恢复
# 0 = 保留不动
#
# 下面四项就是 setup.ps1 的核心安装内容，默认全部清理。
# ============================================================

# 删除 setup 创建的 .venv。
$RemoveVenv = 1

# 删除 setup 安装的 uv；如果 uv 在 setup 之前就已存在，则不会误删原有 uv。
# 若 setup 额外安装了 uv-managed Python 3.11，也会一起卸载。
$RemoveUv = 1

# 删除 Windows Root 中所有 Subject 含 mitmproxy 的 CA，
# 并删除 %USERPROFILE%\.mitmproxy（即重新生成 CA 所需的本地材料）。
$RemoveMitmProxyCA = 1

# 恢复 setup 运行前的 Clash Verge profiles\Script.js。
# 若安装前根本没有 Script.js，则删除 setup 创建的 Script.js。
$RestoreClashGlobalScript = 1

# config.toml 也是 setup 可能创建的文件，但里面有个人资料和 token。
# 默认保留。若你想彻底恢复“全新仓库”状态，改成 1。
$RemoveConfigToml = 1

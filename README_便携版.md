# 独立 MCP Client：Windows x64 便携版

## 直接使用

完整解压 `KohakuTerrarium-MCP-Client-Windows-x64-portable.zip` 到可写目录，然后双击 `Start-KT-MCP-Client.vbs`。不必另外安装 Python 或 KT，也不需要手填 kt_python、workspace_path、kt_home。

如果系统不允许 VBS，使用 `Start-KT-MCP-Client.cmd`。`Check-KT-MCP-Client.cmd` 只检查随包环境并初始化本地配置，不启动 endpoint、不请求服务器、不打印密钥。

请先关闭旧窗口再使用新的启动入口。直接执行 `python -m kt_mcp_client` 属于旧的源码运行入口，不负责便携版首次初始化。

## 随包内容

- `runtime/`：完整的独立 CPython 3.14.0、Tcl/Tk、KT 2.1.5 及其安装依赖。不是引用 C 盘 Python 的 venv。
- `tools/openssh/`：官方 Win32-OpenSSH 的客户端、所需 DLL 和许可证；不安装 SSH 服务。确切版本为 `10.0.0.0p2-Preview`，来源及 SHA256 在 `source.json`。
- `src/`：客户端源码，包括首次启动初始化逻辑。
- `control/`：独立 Studio 工具扩展源码；不会引用生产 Bridge 的 control 目录或收据库。
- `server/`：独立 provisioning 服务源码及 Nginx、systemd、SSH 配置示例；不会自动部署到 VPS。
- `scripts/build_portable.py`：可重复执行的打包逻辑。
- `requirements-runtime.lock.txt`、`runtime/bundle-manifest.json`：本次运行依赖快照。
- `tests/`：原有测试与便携版初始化/打包边界测试。

运行环境通过 DLL 对应的 `python314._pth` 限制模块搜索，不使用系统旧 KT 或用户 site-packages。Tk/SSL/SQLite/Win32/KT 会在构建时实测。

## 首次运行创建的本地内容

```text
项目目录/
  data/
    profile.json              自动生成的本机配置
    portable-location.json    安装位置标记，不是 endpoint proof
    tools.yaml                点击启动后由客户端生成
    kt-home/                  独立 KT 配置和 endpoint 状态
    runtime/                  独立进程记录、日志
  workspace/                  无正式注册表时的回退工作区
```

首次初始化会读取当前电脑可用的正式工作区注册表，并动态发现本机可用盘符根目录（例如 C:\、D:\、E:\）；这些根目录覆盖其下的全部子目录。没有可用注册表时仍保留包内 `workspace/` 作为 `localpc` 回退。目录只写入本机 profile，不在 GUI 中展示，也不会把其它电脑的目录写死。独立 `tools/list` 与正式版实测都是 23 个工具：9 个文件/命令工具、4 个任务工具、`workspaces` 和 9 个 Studio 工具。Studio 收据保存在本机独立 `data/control/receipts.sqlite3`，不会复用生产收据库。

因此，当前机的独立公网 URL 具备正式版同等文件权限，包括 C:\、D:\ 的读写以及 bash/python。不要把 URL 交给不受信任的客户端；独立端仍使用自己的 endpoint secret、SSH 私钥、8865/18766，生产 8765/18765 和生产 Bridge 不变。

默认本地端口 `8865`、预留远端端口 `18766`，不使用原生产的 `8765/18765`。远端端口尚未配置/授权时，不代表可以连接。

默认不再依赖 AppData 下的手工配置。需要自定义已有 profile 时，显式运行：

```powershell
.\runtime\python.exe -I -X utf8 .\launch.py --config "D:\MyIndependentConfig" --check
```

已有 profile 不自动覆盖。新增设备请分发干净 ZIP；不要复制使用后的 data。运行中的 KT 以及 data 内的绝对路径、进程身份和加密凭据不能通过移动目录安全迁移，程序会拒绝静默复用旧位置数据。

## 公网接入部署

本地环境打包完整不等于任意新设备都已授权；本项目当前设备的公网服务已部署并通过实际验收。GUI 点击按钮遵循 `setup → start → url → provisioning → SSH 隧道 → 公网验证` 流程。公开 URL 只能来自 KT 的实际返回，不能手工生成。

服务端使用 `server/` 中的 provisioning 服务、独立 Nginx route map 和独立 SSH 账号；当前设备分配远端 loopback 端口 `18766`。首次完成设备注册后，注册 token 会以 Windows DPAPI 保护形式写入本机 `data/profile.json`；不会进入源码、Git 或通用便携 ZIP，也不会从生产 Bridge 复制。环境变量 `KT_MCP_PROVISION_TOKEN` 仍可用于一次性脚本覆盖，`KT_MCP_PROVISION_URL` 可用于显式 HTTPS API 地址覆盖（默认 origin 使用 `/provision`）。不要把已注册设备的 `data/` 目录复制给其他设备。

GUI 要求输入本机获授权的 HTTPS origin 和对应验证 key；验证 key 仅以 DPAPI 保护形式保存。按钮会在注册后使用独立设备 SSH 反向隧道，并实际执行公网 MCP `initialize`、`tools/list` 和默认工作区无害 `tree` 只读调用；只有这些步骤全部成功才显示地址。没有服务器、origin、验证 key、隧道或公网路由时，界面会报告具体失败，不会显示虚假的成功地址。`--provision` 使用同一套门禁；`--status`/`--start`/`--stop` 仍可单独管理生命周期。

## 命令行

```powershell
.\runtime\python.exe -I -X utf8 .\launch.py --check
.\runtime\python.exe -I -X utf8 .\launch.py --status
.\runtime\python.exe -I -X utf8 .\launch.py --start
.\runtime\python.exe -I -X utf8 .\launch.py --stop
```

`--url` 和 `--provision` 可能输出包含访问凭据的完整 MCP URL，只在确有需要时使用，不要把输出贴到公开日志。

## 重新构建

在 Windows x64、完整 CPython 3.14.0（含 Tcl/Tk）的构建机器上，为项目建立专用 `.venv`，不使用生产 Bridge 的解释器。

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-runtime.lock.txt
.\.venv\Scripts\python.exe scripts\build_portable.py
```

构建过程复制专用环境中的依赖和 CPython 基础运行文件；首次下载官方 OpenSSH ZIP 时校验锁定的 SHA256。不会复制全局 Python 的第三方包。`runtime` 已存在时不会覆盖可能正在使用的环境；仅重新打包代码可以执行：

```powershell
.\runtime\python.exe -I -X utf8 scripts\build_portable.py --zip-only
```

输出在 `dist/`，附 `.sha256` 校验文件。打包采用目录/文件允许列表，排除 `.venv`、data、workspace、真实 .env、服务端注册数据库、路由文件、备份、缓存和运行日志。源码里的依赖声明和构建脚本进入版本控制；较大的 runtime 和 ZIP 作为发布产物，不提交到 Git。

## 单文件 EXE（可选）

如果希望只拿一个文件启动，可在 Windows x64 构建机执行：

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-build.txt
.\.venv\Scripts\python.exe scripts\build_single_exe.py --output "D:\Desktop\magpie-windows-amd64.exe"
```

生成的 EXE 是一个自解压启动器：它把已验证的便携包释放到稳定缓存后，继续调用原有 `runtime/pythonw.exe`，不会把 KT 或 SSH 逻辑重写一遍。若 EXE 放在本项目目录旁边，会优先复用现有 `data/`、DPAPI token、SSH 私钥和已验证地址；否则会在本机缓存中初始化一份新的 profile，不能自动继承另一台电脑的凭据。不要把 `data/` 打进 EXE 或分发包。

如果目标文件正在被旧版 tray 进程占用，请先退出旧进程，再执行覆盖；构建脚本不会强杀或覆盖正在使用的文件。

此包针对 Windows x64；未宣称已在没有 Python 的全新 Windows 虚拟机上完成验收。干净 ZIP 的换目录运行测试与本机隔离验证，不替代跨机器、公网端到端或签名安装包验证。


## 启动耗时与“未配置”提示（2026-10-20 修订）

GUI 和 `--provision` 会先在本地检查服务器注册地址及 `KT_MCP_PROVISION_TOKEN`。
没有凭据时立即提示具体缺项，不启动新 KT 实例、不提交请求，也不把失败伪装成成功。
这不影响显式的 `--start`/`--status`/`--stop` 管理操作。

注册流程使用上游已有的 `mcp-serve start --wait 5`，随后仅在必要时通过上游 `status --json`
有界轮询本地就绪；不再先等待 30 秒尚未建立的公网路由。普通 `--start` 的公网等待语义不变。
GUI 显示检查配置、初始化、启动本地 KT、提交注册四个阶段，以及本次/本阶段耗时。
本地 KT 就绪、注册成功与公网连接成功是三个不同的状态。

错误已分别显示为缺少注册凭据、注册地址无效、HTTP 404、HTTP 401/403、连接失败或超时。
HTTP 404 表示当前注册路由不可用；仅重新打开软件或填写任意 token 都不能部署服务器。
需要由管理员完成独立 `server/` 服务部署、匹配的注册凭据和设备 SSH 授权。
本次设备已完成独立 provisioning、SSH 隧道和公网 MCP 验收；没有修改原生产 Bridge、原生产端口或生产密钥。

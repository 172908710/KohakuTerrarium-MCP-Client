# KohakuTerrarium 独立 MCP Client

## 便携版入口（当前推荐）

项目现已包含随包 `runtime/`、OpenSSH 客户端和首次初始化逻辑。完整解压 `dist/KohakuTerrarium-MCP-Client-Windows-x64-portable.zip`，双击 `Start-KT-MCP-Client.vbs`；程序自动在项目内创建独立的 `data/profile.json` 和运行状态，不再需要手填 Python 和工作区路径。首次运行会读取本机正式工作区注册表并扫描本机可用盘符根目录；没有可用目录时才回退到包内 `workspace/`。VBS 不可用时可双击 `.cmd` 启动入口。

完整说明见 `README_便携版.md`。`Check-KT-MCP-Client.cmd` 可验证本地环境；打包不会部署 VPS 或自动完成公网接入。以下保留底层客户端和服务端接口说明。

这是一个与当前生产环境并行的 Windows MCP Client。**当前生产 MCP URL、生产 Bridge、生产 kt-home、生产 VPS/Nginx/SSH 配置和 MagicPot 均保持不变**；本项目使用独立 kt-home、独立 endpoint secret、独立 SSH 账号/私钥和独立远端端口，不会连接、重载或修改生产 Bridge。

## 与正式版对齐的工具和目录

新电脑首次初始化时会动态发现可用盘符根目录，覆盖其下的全部子目录；没有可用注册表时会使用该电脑自己的便携版回退目录，不会套用其它电脑的固定路径。目录只保存在本机 profile，不在 GUI 中展示。

独立公网 URL 的 `tools/list` 与正式版实测一致，都是 23 个：9 个文件/命令工具（read、write、edit、multi_edit、glob、grep、tree、bash、python）、4 个任务工具、`workspaces`，以及 9 个 Studio 会话/目标工具。Studio 扩展源码随项目提供，但每台设备使用自己的 `data/control/control.json` 和 `receipts.sqlite3`；不会复用生产 control 目录或收据数据库。Studio 工具连接本机已有的 `127.0.0.1:8001`，若该本机 Studio 不运行，这 9 个工具会返回服务不可用，但不会影响文件工具或 MCP initialize/tools/list。

这表示独立公网 URL 具有与正式版相同的文件权限：包括 C:\、D:\ 的读写以及 bash/python。不要把独立 URL 发给不受信任的客户端；它仍然不是生产 URL，生产 secret、8765/18765、Bridge 和生产 SSH 私钥保持不变。

## 最小 GUI

直接运行客户端会打开一个简化的中文窗口，包含：

- 当前生产 MCP 保持不变、独立端已按正式版权限对齐的提示；
- 可编辑的 HTTPS origin；每台电脑必须输入自己获授权的公网 origin 和对应验证 key；源码不内置真实域名；
- “启动并获取地址”按钮；
- 只读的完整返回 URL；
- “开机自动启动（登录后后台运行）”开关；
- 状态提示。

每次打开软件时，公网 origin 和验证 key 输入框均为空，不自动回填已保存的信息。已有本机配置仍保留，开机静默自启直接读取配置，不依赖输入框内容。

公网 origin 右侧的“粘贴”按钮只在点击时读取剪贴板；MCP 地址右侧的“复制”按钮仅在地址验证成功后可用。托盘退出在后台停止服务，界面不会等待停止命令而卡住；停止失败会保留界面提示重试，不会假装已经完全退出。

窗口不包含 profile、workspace、KT Python、SSH 或端口编辑项；用户只输入 origin 和验证 key，key 会以 DPAPI 保护保存。勾选开机自启后，Windows 登录时会静默启动并自动恢复本机配置。点击窗口右上角 `X` 会隐藏到任务栏通知区域，不会停止本地 MCP；右键托盘图标并选择“退出并停止本地 MCP”才会停止独立 KT 和 SSH 隧道。按钮在后台线程中按 **独立 KT setup → start → url → provisioning → SSH 隧道 → 公网验证** 的顺序执行：完整 `endpoint_url` 只来自本机独立 KT 的 `url` 命令，随后才向独立 provisioning API 注册。它不会启动或停止生产 runtime，也不会复制生产 secret。origin 是用户输入；如果 KT 返回的 endpoint origin 与它不一致，流程会 fail closed。

按钮需要**单独的服务端/VPS provisioning API**。客户端向 `KT_MCP_PROVISION_URL` 指定的 HTTPS API 发送 JSON POST；未设置该环境变量时，默认 origin 使用 `/provision`。请求包含 origin、保存在独立配置根中的非 secret `installation_id`，以及本机独立 KT 已产生的完整 `endpoint_url`。服务端只接受该 URL 中的 secret 作为唯一来源，不生成或替换 secret；服务端返回的 `url`/`endpoint_url` 必须与本机 endpoint 完全一致，并可附带 `remote_port`。注册 token 可在配置阶段写入 `profile.json` 的 Windows DPAPI 保护字段，双击重启后仍可使用；它不会进入通用便携包或子进程环境。API 本身不会建立隧道。

如果没有配置 API、API 返回 404、无法连接或响应格式/URL 不合法，界面会提示失败，不会伪造或显示 provisioning 返回的 URL，也不会复用生产 secret。注册和 SSH 隧道启动后，客户端还会实际执行公网 MCP `initialize`、`tools/list` 及默认工作区的无害 `tree` 只读调用；只有本地 KT endpoint、provisioning 响应和公网连通性全部验证成功时，完整 URL 才显示为成功结果。验证不会调用 write/edit/bash/python 或 Studio 变更工具。

直接使用旧源码入口 `python -m kt_mcp_client` 时，默认独立配置根为（便携版启动入口改用项目内 `data/`）：

```text
%LOCALAPPDATA%\KohakuTerrarium\MCPClientNext
```

也可用 `KT_MCP_CLIENT_CONFIG_DIR` 指定。配置根、profile、installation ID 和其他文件都与生产目录分离；程序仍拒绝使用当前生产 `KT-MCP-Bridge` 树。

## 运行

```powershell
$env:PYTHONPATH = "D:\Desktop\KohakuTerrarium-MCP-Client\src"
python -m kt_mcp_client
```

也可以双击 `Start-KT-MCP-Client.vbs`。CLI/backend 模块包括：

- `--provision`：独立 KT setup/start/url、provisioning、SSH 隧道和公网 MCP 验证成功后输出本机 KT 实际返回的完整 endpoint；
- `--url`：读取并输出本机独立 KT 的实际 MCP 地址；
- `--status`、`--start`、`--stop`：保留的 runtime/SSH 生命周期操作。

CLI 失败时只输出错误，不显示或保存伪造的 provisioning URL。

## 当前限制与 API 要求

本目录同时保留源码和自带运行环境的 ZIP 便携版；目前不是签名的 EXE/MSI 安装包。服务端必须先部署独立 provisioning API，并返回类似下面的 JSON；其中 URL 必须逐字对应客户端 POST 的本机 `endpoint_url`：

```json
{"url": "https://独立-origin.example/mcp/<secret-from-local-KT>", "remote_port": 18766}
```

POST body 必须包含：

```json
{"origin": "https://独立-origin.example", "installation_id": "32-lowercase-hex-characters", "endpoint_url": "https://独立-origin.example/mcp/<secret-from-local-KT>"}
```

客户端不会修改 VPS 文件，也不会自动部署 VPS 路由；API 服务端负责设备注册、路由和权限策略，但不得生成或替换本地 KT secret。当前生产 MCP URL 保持不变。

## Server-side scaffold (option 1)

The `server/` directory contains a separately deployable, dependency-free
provisioning service and persistent registry. An operator-managed VPS instance
may expose `POST /provision`; the checked-in code does not assume that the live
VPS has been updated. It registers the exact endpoint URL supplied by local KT,
 writes a reviewed route map, and returns a URL only when the reverse-proxy
reload hook succeeds. See `server/README.md` and the `.example` files for the
deployment gates. It never edits the existing production Bridge files or the
production `18765` route.

## VPS deployment status

本次已从本机通过 SSH 完成独立接入部署：`kt-mcp-provision.service` 运行在
VPS loopback `127.0.0.1:18080`，Nginx `/provision` 仅代理到该服务；设备路由
使用独立生成的 server-context `routes.map`，首个设备分配 `127.0.0.1:18766`。
新 SSH 账号和 Ed25519 密钥只允许 remote forward 到 `127.0.0.1:18766`；生产
账号、生产密钥、生产 `127.0.0.1:18765` 路由和本机 `8765` 均保留。部署备份位于
VPS `/root/kt-mcp-provision-backup-20261020-090411`；不要把 token、私钥或
`data/` 目录加入源码、Git 或通用 ZIP。若服务端配置被回滚，客户端必须继续把
注册或公网验证失败显示为失败，不得伪造地址。

## 测试

测试使用临时目录和 fake HTTP opener，不启动任何生产进程，也不要求 Tk 显示器。服务器 scaffold 的测试还会启动临时 loopback HTTP 端口；不会连接公网或修改 VPS：

```powershell
$env:PYTHONPATH = "D:\Desktop\KohakuTerrarium-MCP-Client\src"
PYTHONPATH=src python -m pytest -q
python -m compileall -q src server tests
```

## 单文件 EXE（可选）

可用项目内的 `scripts/build_single_exe.py` 生成一个单独的 Windows EXE 启动器。它封装现有便携包，不改变 KT、SSH、provisioning 或公网验证流程：

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-build.txt
.\.venv\Scripts\python.exe scripts\build_single_exe.py --output "D:\Desktop\magpie-windows-amd64.exe"
```

EXE 放在当前项目旁边时会复用现有本机 profile；单独复制到没有 `data/` 的电脑时，会在该电脑的稳定缓存中首次初始化新设备，不会复制或暴露原电脑的凭据。

如果目标文件正在被旧版 tray 进程占用，请先退出旧进程，再执行覆盖；构建脚本不会强杀或覆盖正在使用的文件。
同一配置目录只运行一个客户端。再次双击会唤回已运行的窗口，不会重复启动本机 MCP；开机自启发现已有实例时保持静默。不同配置目录可以分别运行。

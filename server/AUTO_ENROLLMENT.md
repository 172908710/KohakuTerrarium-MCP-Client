# 两项输入的自动设备接入（v2）

## 当前交付边界

本目录增加了客户端和服务器代码、隔离测试及部署模板。**代码存在、测试通过不表示线上 VPS 已升级。** 本次开发没有使用 root 密码登录，没有关闭任何用户窗口，没有修改生产 SSH/Nginx/防火墙，也没有重启现有 MCP。必须由服务器管理员完成下方一次性部署，并在一台全新客户电脑上验收后，才能作为客户正式版发布。

客户界面保持两个输入框：**公网 origin + 验证 key**。客户不输入 SSH 主机、用户名、私钥路径或 root 密码。更新后的客户端和服务器支持原有验证 key（`KT_MCP_PROVISION_ADMIN_TOKEN`）为多台新电脑自动开通，key 的值不变；每台电脑仍使用自己的私钥和独立转发端口。`ktd_` 专用 key 继续支持一把绑定一台设备。原本已配置 SSH 的旧电脑不被自动迁移。

## 数据流

1. 客户填入获授权的 HTTPS origin，以及原有共享验证 key 或单设备 `ktd_...` key。
2. 本机独立 KT 生成真实 MCP endpoint，客户端验证当前内存中的来源证明。
3. 客户端在自身配置目录 `device-ssh/` 生成 Ed25519 密钥；私钥只保存在客户电脑，Windows 文件 ACL 仅允许当前用户访问。重复请求沿用同一密钥，不生成新身份。
4. 客户端向已有 `POST /provision` 发送 v2 请求：原来的 origin、installation_id、endpoint_url，加 enrollment_version=2 和 ssh_public_key。**不上传私钥。** TLS 验证与禁止重定向保持不变。
5. 服务器验证 key，在同一 SQLite 事务中绑定 installation_id、公钥和独立转发端口，更新并验证路由。共享 key 可开通多台设备，但每个 installation_id 和公钥只能绑定一次；`ktd_` key 仍只绑定一台设备。同一设备重试幂等，不覆盖其他设备身份。路由失败时绑定一并回滚。
6. 响应返回专用隧道账号、主机、端口、服务器公开主机密钥及设备公钥指纹。客户端核对绑定，写入 known_hosts，保存 SSH 配置。主机密钥变化会拒绝，绝不关闭身份校验。
7. 客户端建立只用本机密钥的非交互式 SSH 反向隧道，仍需完成公网 MCP initialize、tools/list、只读 tree 检查，才显示成功地址。

数据库只存 `ktd_` key 的 SHA256，不存其明文；共享 key 沿用服务器受保护环境配置，不写入设备绑定表或安装包。已绑定的设备重试不受 `ktd_` 的“首次激活有效期”影响，但撤销后不能继续注册或发起新 SSH 认证。共享 key 轮换只改变之后的注册认证，不自动撤销已绑定设备。

## 已部署 v2 的共享 key 升级

客户端和服务器必须一起更新。仅替换 EXE，旧服务端仍会拒绝旧 key 的 v2 请求。不要再次运行用于首次安装的 `deploy_auto_enrollment.py`；已有设备入口（例如 TCP 2223）保持原样。

`scripts/update_shared_key_server.py` 是一次性服务器升级入口，需与 `server/` 和生成的 `source-sha256.json` 一起上传。默认仅预检，`--apply` 备份代码和数据库后只升级注册服务代码、重启独立 provisioning 服务；不更改原 key、SSH/Nginx 配置、2223 端口或防火墙。失败时尝试回滚代码，不回滚之后可能已有的新设备记录。

共享 key 是可重复使用的开通凭据：拿到它的人能开通新设备，不能把它当作公开注册码，也不要内置到 EXE。使用共享模式意味着放弃“一把客户 key 仅能激活一台”的限制；不意味着客户获得服务器 root 私钥或 shell 权限。

查看共享 key 开通的设备、单独撤销设备（只影响新认证，不终止现有 SSH 会话）：

```bash
/usr/bin/python3 -I /opt/kt-mcp-provision/server/enrollment_cli.py shared-devices
/usr/bin/python3 -I /opt/kt-mcp-provision/server/enrollment_cli.py revoke-device --installation-id <32位installation_id>
```

启动器的缓存升级只替换程序组件，保留 `data/`、installation ID、私钥和工作区；升级前需要从托盘退出旧版，不要通过删除 data 来解决缓存错误。

## 一次性服务器部署（管理员执行，不是客户操作）

请先核对现有部署位置、用户、端口、数据库和 systemd 服务，备份源文件、受保护环境文件及数据库一致性快照。保留现有生产 8765/18765、管理员 SSH 入口、已发放 key 和数据库，不使用旧环境文件里的秘密填入分发包。

### 1. 更新注册服务代码

同步 `server/app.py`、`service.py`、`registry.py`，新增 `enrollment.py`、`authorized_keys.py` 和 `enrollment_cli.py`，通常安装到 `/opt/kt-mcp-provision/server/`。

所有这些脚本及上级源码目录必须是 **root 拥有，服务用户不可写**。只有 `/var/lib/kt-mcp-provision/` 中的数据库和生成路由由现有服务账号管理。不要把整棵 `/opt/kt-mcp-provision` 改成服务用户可写。

### 2. 新增专用客户隧道入口

若管理员 SSH 端口只允许你自己的 IP，不要取消这项限制。新增单独的客户隧道监听端口（模板为 TCP 2222），与管理员端口 22 分离。云防火墙和主机防火墙仍需由管理员按实际策略放行客户所用端口；本项目不会自动修改防火墙。**仅 HTTPS 注册成功不代表 SSH 端口可达。**

创建独立系统账号 `kt-mcp-devices`，不加入任何管理组，不提供 shell/密码登录，不复用原生产账号。账号的 PAM/锁定状态必须在该 Linux 发行版上允许公钥认证；不要为了让其可用开启密码或 root 登录。

模板：

- `sshd-device-gateway.conf.example` -> `/etc/ssh/sshd_config_kt_devices`
- `kt-mcp-device-sshd.service.example` -> `/etc/systemd/system/kt-mcp-device-sshd.service`

模板使用 Linux OpenSSH `AuthorizedKeysCommand`：以 `kt-mcp-provision` 身份只读查询已提交的设备绑定；每把公钥仅允许其独立端口 `127.0.0.1:<allocated-port>`。全局还禁止 shell/session、local forward、agent/X11、PTY、Unix socket forwarding。

部署前至少执行：

```bash
/usr/sbin/sshd -t -f /etc/ssh/sshd_config_kt_devices
/usr/sbin/sshd -T -f /etc/ssh/sshd_config_kt_devices
```

检查 helper 使用的数据库路径、账号和端口池与注册服务一致。修改端口池时，模板里的 helper 参数也必须同步调整。此方案针对 Linux VPS；Windows sshd 不支持这些 AuthorizedKeysCommand 配置。

### 3. 启用服务器 v2 功能

在**现有受保护的注册服务环境文件**中增补以下配置，保留现有的管理员 token 和路由配置；不要将真实值提交 Git。

```text
KT_MCP_DEVICE_ENROLLMENT_ENABLED=1
KT_MCP_DEVICE_SSH_HOST=your-server.example
KT_MCP_DEVICE_SSH_PORT=2222
KT_MCP_DEVICE_SSH_USER=kt-mcp-devices
KT_MCP_DEVICE_SSH_HOST_KEY="ssh-ed25519 <服务器实际公开主机密钥的base64字段>"
```

主机公钥必须由管理员从实际 `HostKey` 对应的 `.pub` 文件核对，只用前两个字段（类型、base64）；不要填入私钥，不通过未经认证的 ssh-keyscan 盲目信任远端。当前 v2 支持 DNS/IPv4 主机名和 Ed25519 主机密钥。

配置检查后启动独立设备 sshd 服务，并重启独立 provisioning 服务。Nginx 仍使用原 `POST /provision` 位置，无需新增公开后台管理接口。不要重启原生产 Bridge 或改其路由。

### 4. 可选：签发单设备验证 key（共享旧 key 无需此步骤）

在服务器本机运行（不是在客户电脑运行）：

```bash
/usr/bin/python3 -I /opt/kt-mcp-provision/server/enrollment_cli.py issue --label customer-001
```

输出中的 `verification_key` 只显示一次，单独交付对应客户。`activation_id` 是管理用非明文标识，可以记录；label 不要放敏感资料。默认 168 小时内首次激活，可用 `--valid-hours` 调整。

查看和撤销：

```bash
/usr/bin/python3 -I /opt/kt-mcp-provision/server/enrollment_cli.py list
/usr/bin/python3 -I /opt/kt-mcp-provision/server/enrollment_cli.py revoke --activation-id <activation_id>
```

**撤销立即拒绝后续注册与新 SSH 认证，但不会杀死已建立的 SSH 连接。** 需要立即中止已有访问时，管理员还必须禁用该设备的路由并结束对应连接；不要把本命令描述成完整的实时断开功能。

### 5. 正式验收门槛

使用两个全新的配置目录/电脑，不复制 `data/` 或任何旧私钥。两台都只填 origin 和同一个原有共享验证 key，确认各自密钥自动生成、独立端口、SSH 建立及公网三步验证成功，重试不换身份。另用 `ktd_` key 验证仍只绑定一台；跨模式不能覆盖既有或已撤销的身份。测试禁止 shell、禁止其他 remote port、禁止 local forward，确认原生产连接仍正常。

本机单元测试模拟了注册服务器与路由 reload，验证真实 Windows ACL 和随包 ssh-keygen/ssh 配置解析；**这不代替客户网络到 VPS 的真实 SSH 和公网 MCP 验收。**

## 回滚

关闭 `KT_MCP_DEVICE_ENROLLMENT_ENABLED` 并重启独立注册服务可停止新设备接入；若需立即停止 v2 隧道，可停掉独立 `kt-mcp-device-sshd` 服务。保留数据库新增表和客户记录，避免丢失绑定后误重复发放。源码回滚应使用部署前备份；不要回滚或覆盖无关生产 Bridge/管理员 SSH/原路由。

## 参考

- OpenSSH sshd authorized_keys options: https://man.openbsd.org/sshd.8
- OpenSSH AuthorizedKeysCommand/MaxSessions/AllowTcpForwarding: https://man.openbsd.org/sshd_config.5
- Windows SSH key management and server feature limitations: https://learn.microsoft.com/en-us/windows-server/administration/openssh/openssh_keymanagement

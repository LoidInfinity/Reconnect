# 更新记录

## 0.1.0-beta.1 — 2026-11-21

- **单实例保护**：改用 Windows 命名互斥量（`Local\NetworkGuard.Monitor`）代替不可靠的 PID 文件判断。CLI 与 GUI 后台 worker 共用同一把锁，同一时间只能有一个守护在操作网络；重复启动会直接退出并记录原因。进程崩溃时由内核自动释放，不会像 PID 文件那样残留锁。
- GUI 额外使用 `Local\NetworkGuard.Gui` 锁，重复双击只保留一个窗口，提示“程序已在运行”。
- 版本号进入 beta 阶段。

## 0.1.0-alpha.4 — 2026-11-21

- **有线可用时绝不动无线**：有线链路已健康（持有有效 IP 与网关）时直接跳过 Wi-Fi 重连，避免重连抢占默认路由、主动打断一条本来正常的连接。只有全部连接失效时才从有线开始逐级尝试。
- **Wi-Fi 识别不再被 `netsh` 挂起拖死**：`netsh wlan show interfaces` 在网卡驱动重置（MAC 为 `00:00:00:00:00:00`、状态“正在断开连接”）或 wlansvc 异常时会挂起。现改为 10 秒超时，失败后回退到 PowerShell（不依赖 wlansvc）识别网卡，并明确记录回退原因，不再误报“未识别到 Wi-Fi 网卡”。
- `netsh wlan show profiles` 超时由 30 秒降为 10 秒；查询失败时退回配置中的参与名单作为候选，仍能尝试连接。
- 有线探测改为一次性读取（`get_physical_adapters` 结果复用），减少恢复轮次中的 PowerShell 调用次数。
- 新增只读探测 `has_healthy_wired_link`；`repair_wired_links` 支持传入已探测的适配器列表。
- 测试：新增/调整有线优先与网卡识别回退用例，全部用例改为 mock 适配器探测，不再在测试中真实执行 PowerShell。

## 0.1.0-alpha.3 — 2026-11-21

- **新增有线优先恢复**：检测到全断时先检查有线网卡，识别"网线插着但 DHCP 未下发地址"（IPv4 为 `169.254.x.x` 且无默认网关）的情况，执行 `ipconfig /release` + `/renew` 重新获取租约，必要时重置该有线网卡。虚拟网卡（VMware/Hyper-V/WSL 等）不参与判断。
- **有线存在不再跳过无线**：有线链路恢复后 HTTP 仍不可用时，继续执行 Wi-Fi 重连，避免"插着网线就不试无线"导致无线环境失效。
- 新增配置项与参数：`wired_recovery`、`wired_reset_on_failure`、`--no-wired-recovery`；GUI 增加"先修复有线连接"开关，恢复顺序调整为 代理 → DNS → 有线 → 无线 → 网卡重置。
- **新增终端（无 GUI）启动方式**：`start_cli.bat` / `python main.py`，支持 `--verbose` 在前台查看完整日志。
- 修复统计新增"有线修复后恢复"计数。
- 强化 `.gitignore`：补充 `*.log`、`*.pid`、`*.jsonl`、`NetworkGuard/`，并为不含真实信息的 `network_guard_config.example.json` 模板加例外，确认真实配置、日志、状态与 PID 不入库。

## 0.1.0-alpha.2 — 2026-11-03

- 核对初始脚本，补齐 GUI 后台心跳、PID／异常退出检测、休眠恢复检测和 DNS／代理修复分项统计。
- 接入 DNS 修复、系统代理清理、网卡重置、防休眠和外部命令超时保护；保留 Wi-Fi 逐条参与恢复设置。
- 兼容旧版 `allowed_profiles`、`--allowed-profile`、`--wifi-recovery` 及 `dns_reset_interfaces`。
- 默认检测目标改为 ToDesk、向日葵、AweSun 官网，DNS 监测域名同步更新；迁移旧默认值，保留自定义配置。
- 修复 ping 可达但 HTTP 失败时跳过 Wi-Fi 恢复的问题。先修复 DNS／代理，仍失败则尝试允许的 Wi-Fi。
- DNS 修复已恢复 HTTP 时，不再继续清理系统代理；参与恢复受限时不通过硬件重置绕过名单。
- 明确官网连通性检测不代表远控软件会话健康：任意一个检测网址成功时，不会触发恢复。
- 本地 `test/` 初始源码制作归档及 SHA-256 清单，原文件保留；排除归档、个人配置、日志、虚拟环境、构建产物和本地凭据。
- Windows 文件版本随应用版本生成，当前为 `0.1.0.2`；提供 ZIP 与 SHA-256 校验文件。

## 0.1.0-alpha.1

- 中文桌面 GUI、Wi-Fi 恢复参与开关、实时日志及 GitHub 更新检查。
- Windows x64 免安装目录包。

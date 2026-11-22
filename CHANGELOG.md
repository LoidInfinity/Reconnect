# 更新记录

## 0.1.0-rc.1 — 2026-11-22

首个发布候选版（Release Candidate）。功能与 beta.3 一致，主要变化是仓库结构与运行时目录。

**仓库结构**

- 源码集中到 `src/`，测试到 `tests/`，配置模板到 `config/`，对照文档到 `docs/`；根目录只保留 README、LICENSE、更新记录、`requirements-build.txt` 与四个启动脚本。
- `tests/__init__.py` 负责把 `src` 加入 `sys.path`，测试命令统一为 `python -m unittest discover -s tests -t . -p "test_*.py"`。

**运行时目录**

- 配置、状态、PID 收进 `data/`，日志单独放在 `data/logs/`（打包版对应 `%LOCALAPPDATA%\NetworkGuard` 及其 `logs` 子目录）。
- 首次启动自动迁移旧版本散落在根目录的文件，并把配置中的旧 `log_file` 升级为 `logs/network_guard.log`，现有设置不会丢失。
- `.gitignore` 追加 `/data/`、`/logs/`、`.vscode/`、`*.code-workspace`，个人 IDE 配置与自动生成的空工作区文件不再入库。

**构建与发布**

- `build.py` 移入 `src/`，命令变为 `python src/build.py`，GitHub Actions 工作流同步更新。
- 版本一致性测试与自动发布继续生效；标签中含 `-beta` / `-alpha` / `-rc` 时 Release 判定为预发行。

## 0.1.0-beta.3 — 2026-11-22

**崩溃日志回放更准确**

- 修复「上次运行异常退出」回放区块混入本次启动日志的问题。现在会在打开日志文件之前记录上次会话的结束位置，只回放这段时间之前的内容；若启动瞬间发生日志轮转，会自动改读 `.1` 备份。
- 回放的每一行都加 `[上次]` 前缀，分隔语明确说明「以下行属于上一次运行，不是本次启动」，避免与本次日志混淆。
- CLI（`python main.py`）与 GUI 后台守护使用同一套逻辑，两边表现一致。

**版本一致性测试**

- 新增 `test_version_consistency.py`：校验 `app_info.VERSION`、`CHANGELOG.md` 最新章节、README 的「当前版本」与发布示例标签是否一致，发版时漏改文档会直接测试失败。

## 0.1.0-beta.2 — 2026-11-21

**日志打包**

- 新增日志打包：界面左侧栏的「遇到问题」按钮，或终端 `python main.py --export-logs [目录]`。将当前日志与轮转备份合成一个 ZIP，并附一份说明（版本、系统、常见错误线索与反馈邮箱）。
- 「遇到问题」弹窗内含「打包日志」与「打开日志文件夹」两个按钮，并**直接写明将生成的压缩包名**（如 `NetworkGuard-logs-20261121-204831.zip`），无需再猜文件名；打包后同步显示实际生成的名字。
- 压缩包**不含配置文件**（含个人 Wi-Fi 名称），日志本身已脱敏；重复打包自动加序号，不覆盖上一次；若预定文件名被占用也会自动改用可用名。

**自动发布**

- 新增 GitHub Actions 工作流：推送 `v*` 标签时自动在 Windows 上跑测试、构建目录包与 ZIP、并创建**预发行** Release（含 SHA-256 校验文件）。

## 0.1.0-beta.1 — 2026-11-21

**有线优先恢复**

- 检测到全断时先检查有线网卡，识别“网线插着但 DHCP 未下发地址”（IPv4 为 `169.254.x.x` 且无默认网关）的情况，执行 `ipconfig /release` + `/renew` 重新获取租约，必要时重置该有线网卡。虚拟网卡（VMware/Hyper-V/WSL 等）不参与判断。
- **有线可用时绝不动无线**：有线链路已健康（持有有效 IP 与网关）时直接跳过 Wi-Fi 重连，避免重连抢占默认路由、主动打断一条本来正常的连接；仅当有线不可用或修复后仍不健康时，才继续尝试无线。
- 新增配置项与参数：`wired_recovery`、`wired_reset_on_failure`、`--no-wired-recovery`；GUI 增加“先修复有线连接”开关，恢复顺序为 代理 → DNS → 有线 → 无线 → 网卡重置。

**Wi-Fi 识别更稳定**

- `netsh wlan show interfaces` 在网卡驱动重置（MAC 为 `00:00:00:00:00:00`、状态“正在断开连接”）或 wlansvc 异常时会挂起。现改为 10 秒超时，失败后回退到 PowerShell（不依赖 wlansvc）识别网卡，并明确记录回退原因，不再误报“未识别到 Wi-Fi 网卡”。
- `netsh wlan show profiles` 超时由 30 秒降为 10 秒；查询失败时退回配置中的参与名单作为候选，仍能尝试连接。
- 有线探测改为一次性读取（`get_physical_adapters` 结果复用），减少恢复轮次中的 PowerShell 调用；新增只读探测 `has_healthy_wired_link`。

**单实例保护**

- 改用 Windows 命名互斥量（`Local\NetworkGuard.Monitor`）代替不可靠的 PID 文件判断。CLI 与 GUI 后台 worker 共用同一把锁，同一时间只能有一个守护在操作网络；重复启动会直接退出并记录原因。进程崩溃时由内核自动释放，不会像 PID 文件那样残留锁。
- GUI 额外使用 `Local\NetworkGuard.Gui` 锁，重复双击只保留一个窗口，提示“程序已在运行”。

**启动方式**

- 新增终端（无 GUI）启动方式：`start_cli.bat` / `python main.py`，支持 `--verbose` 在前台查看完整日志。

**其他**

- 修复统计新增“有线修复后恢复”计数。
- 强化 `.gitignore`：补充 `*.log`、`*.pid`、`*.jsonl`、`NetworkGuard/`，并为不含真实信息的 `network_guard_config.example.json` 模板加例外，确认真实配置、日志、状态与 PID 不入库。
- 测试：新增有线恢复与单实例用例，全部用例改为 mock 适配器探测，不再在测试中真实执行 PowerShell。

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

# 更新记录

## 0.1.0-rc.2 — 2026-11-23

**新增安装包（Setup.exe），由 GitHub Actions 在打标签时自动构建**

- 新增 `src/installer.iss`（Inno Setup 6）：安装界面可选安装目录，默认 `%LOCALAPPDATA%\Programs\NetworkGuard`，按当前用户安装、不触发 UAC；安装完成后**自动清除下载标记（Mark of the Web）**，安装目录里的 `.exe`/`.bat` 不再弹「无法验证发布者」。
- 安装包会创建开始菜单快捷方式与可选桌面快捷方式，并提供标准卸载项；卸载只删除程序目录，`%LOCALAPPDATA%\NetworkGuard` 下的配置与日志保留。
- `src/build.py` 增加安装包步骤：检测到 Inno Setup（`ISCC.exe`）时自动编译 `NetworkGuard-Setup-<版本>.exe` 并生成 `.sha256`；未安装时跳过并提示，不影响原有目录版与 ZIP。
- 发布工作流（`.github/workflows/release.yml`）在 `v*` 标签触发时安装 Inno Setup，构建目录版 ZIP 与安装包，并一并上传到 Release；原有的测试、版本一致性校验与 SHA-256 校验保持。
- 中文安装界面使用 Inno Setup 官方 `ChineseSimplified.isl`（随仓库提供，便于离线构建）。
- 本地已验证：静默安装到指定目录（999 个文件）、安装日志确认执行了 `Unblock-File`、真实注入的 `Zone.Identifier` 被清除、静默卸载后目录清理干净。

**修复「Wi-Fi 被系统关闭后不再重连」**

现场日志显示：断网瞬间 WLAN 接口收到「操作状态更改请求」，无线电被切到**软件关闭**（`无线电状态：硬件 开 / 软件 关`），此后 `netsh wlan connect` 一律返回 `2150899714`（“无线局域网接口电源关闭”）；程序把网卡重置跳过，网络靠 Windows 自己的 WLAN AutoConfig 在 96 秒后连回。

- **新增无线电恢复**：恢复流程最前面先读 Wi-Fi 无线电状态（WinRT `Windows.Devices.Radios`，普通用户权限即可），处于关闭/禁用时自动打开并等待系统回连；不需要管理员，也不需要重启。开关：配置项 `wifi_radio_recovery`（默认开）、命令行 `--no-radio-recovery`、界面「无线被关闭时自动打开无线电」。
- **无线被关闭时允许网卡重置**：原先只要配置了参与名单就永远跳过重置，导致无线电关着时无路可走。现在仅当「无线电开着且存在参与名单」才跳过（避免 Windows 自动连接被排除的配置）；无线电关闭时重置不会抢占任何现有连接，因此放行。
- **有线健康时连无线电都不碰**：修复“无线电优先”可能越过有线判断的问题。现在只要有一条有线链路持有有效 IP 与网关，本轮就跳过无线电、Wi-Fi 重连与网卡重置（连无线电状态都不读）。这对“插网线时 WLAN 被 OEM 策略关闭、只能二选一”的机型很关键——那种关闭不是故障，强行打开反而可能断掉正在工作的有线。
- **有线兜底不再空转**：识别 `Disconnected/Disabled`（网线未插、网卡禁用）与「未为 DHCP 启用适配器」（静态 IP）的有线网卡，跳过 `/release`、`/renew` 与重置，把重连预算留给无线；无管理员权限时也不再触发一次必然失败的网卡重置。
- **无线电状态可诊断**：启动诊断新增 Wi-Fi 无线电状态与运行权限（管理员/普通用户）；断线日志会明确写出「无线电已关闭」「系统拒绝打开」等原因，`netsh` 输出也会单独提示，不再混在“连接失败”里。
- **恢复归因修正**：无线电曾被关闭时，即使 DNS 同时恢复也不再把功劳记到「DNS 修复」上，新增「无线电恢复」统计项，避免误导排障。
- **编码探测修正**：`utf-16` 曾排在 `utf-8` 之前，任何偶数长度的 GBK 输出都会被“成功”解码成乱码，导致 `netsh wlan show interfaces` 解析失败并退化为 PowerShell 兜底（丢失 SSID 与无线电状态）。现在只在有 BOM 时按 UTF-16 解码，其余按 UTF-8 → GBK → GB18030。
- **ping 判定修正**：Windows 对「无法访问目标主机」也返回 0，旧逻辑会误报「Ping 检查成功」；现在要求回复带 TTL 且不含不可达标记。
- 新增 `tests/test_radio_recovery.py`（无线电解析、WinRT 开关、无线关闭时的重置策略、死有线跳过、编码与 ping 回归）。

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

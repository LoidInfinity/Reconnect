# Network Guard 网络守护

Windows Wi-Fi 自动恢复工具，提供中文桌面界面和命令行入口。

当前版本：**0.1.0-alpha.2**（预发布版）。更新记录见 [CHANGELOG.md](CHANGELOG.md)。

初始脚本的完整功能对照及本次补齐项见 [LEGACY_REVIEW.md](LEGACY_REVIEW.md)。默认联网检测按用户提供的日志改为 `https://www.todesk.com`、`https://sunlogin.oray.com`、`https://www.awesun.com`，DNS 监测域名同步更新。旧默认配置会在读取时迁移，自定义检测地址保持原样。

## Windows 打包版

解压 `NetworkGuard-0.1.0-alpha.2-windows-x64.zip`，进入 `NetworkGuard` 文件夹双击 `NetworkGuard.exe`。无需安装 Python；请保留同目录的 `_internal` 文件夹。

打包版配置、日志及状态文件保存在 `%LOCALAPPDATA%\NetworkGuard`，源码运行仍保存在项目目录。现有源码配置不会自动复制；可自行复制配置并检查其中日志和状态路径，或在新界面重新设置。

侧栏显示当前版本，点击“检查 GitHub 更新”会查询本仓库 GitHub Releases。Alpha 版本会检查包含预发布版在内的新版本，正式版只提示正式版更新。发现新版后可打开发布页手动下载；程序不自动下载或覆盖自身。暂无 Release、网络失败和请求限流会分别提示。版本按 SemVer 比较，支持 `v0.1.0-alpha.2` 等标签。

发布者需要创建对应 GitHub Release 并附上 ZIP，检查更新才能发现它；单独推送代码或标签不等于发布更新。

## 断线自动修复流程

检测到断网后，按“从轻到重”的顺序修复，前一步解决就不再动后面的：

1. **DNS 修复**：探测配置的域名，解析失败时清空 DNS 缓存并把网卡 DNS 回退为 DHCP 自动获取。
2. **系统代理清理**：FlClash / Clash / Mihomo 等本地代理退出后，Windows 仍把请求发往已失效的端口，导致 ping 通但应用断网。此步关闭系统代理开关（`ProxyEnable=0`、清空 PAC）并广播设置变更。
3. **Wi-Fi 重连**：按“上次成功 → 首选 → 其他已保存”的顺序尝试连接，受重连超时限制。
4. **网卡重置**：不限制参与恢复的 Wi-Fi 配置时，全部失败后尝试禁用再启用网卡。存在参与名单或禁用名单时跳过此步，避免 Windows 自动连接被排除的 Wi-Fi。

**Ping 成功不会阻止网络恢复**：HTTP 检测失败时先修复 DNS／代理，HTTP 仍失败则继续尝试参与名单内的 Wi-Fi（需启用 Wi-Fi 恢复）。代理清理与网卡重置各有冷却时间（分别为 60 秒、300 秒）。第 4 步需要管理员权限，无权限时会记录失败原因。

联网判定以 HTTP(S) 结果为准，ping 只用来区分“链路断了”和“链路正常但应用层坏了”。因为代理失效时 ping 依然通，用 ping 判定会漏报。

当前三个检测目标中任意一个 HTTP 成功，就判定应用层可达。这不是远控会话健康检查：官网可访问但软件登录、会话或中继连接失败时，仍可能不触发恢复。此类故障需要接入对应软件提供的状态或实际服务健康探测，不能仅靠 ping 或官网判断。

## 运行诊断

- **心跳日志**：默认每 60 秒输出一次，长时间无日志时才能区分“没事发生”和“程序卡死”。
- **休眠检测**：等待时间远超预期时判定发生过系统休眠，立即重置网络状态重新检测；守护默认阻止系统休眠（显示器仍可关闭）。
- **崩溃检测**：启动时若发现残留 PID 文件，说明上次异常退出，会回放崩溃前最后 15 行日志并记录退出原因。
- **命令超时**：`netsh` 卡在驱动层时 `kill()` 不返回，因此命令等待由守护线程限时，超时即放弃等待，主循环不会被拖死。

## 构建与测试

在 Windows x64、Python 3.10+（本版使用 Python 3.14）环境运行：

```powershell
python -m pip install -r requirements-build.txt
python -m unittest -v test_gui test_updates test_repairs test_legacy_compat test_recovery_policy
python build.py
```

`dist` 中生成完整程序目录、版本 ZIP 和 SHA-256 校验文件。构建使用 PyInstaller 无控制台模式，后台守护通过临时事件文件回传日志。构建产物不提交到源码仓库。

## 启动桌面界面

双击 `start_gui.bat`，或运行：

```powershell
.\.conda\python.exe gui.py
```

没有项目虚拟环境时，可使用安装了 Tkinter 的 Python 3.10+：`python gui.py`。无需第三方 GUI 依赖。

- **启动守护 / 停止守护**：启动后定期检测网络，断线时自动尝试修复。停止或关闭窗口会结束该窗口启动的守护进程。
- **已保存的 Wi-Fi**：刷新配置及网卡状态，将所选配置设为首选。程序不会读取或展示 Wi-Fi 密码。
- **参与恢复开关**：首次读取 Wi-Fi 列表时，仅当前已连接的配置默认参与恢复，其他配置默认不参与；未连接或无法识别当前配置时全部不参与。点击每条 Wi-Fi 右侧的“☑ 使用 / ☐ 不使用”（也可选中后按空格）自动保存选择。刷新、重启会保留已保存的选择，新发现的配置默认不参与。关闭的配置即使是首选或上次成功连接，也会跳过；全部关闭时不会发起 Wi-Fi 连接。GUI 守护从下一轮恢复读取开关，当前已开始的恢复轮次不受影响；命令行在下次启动时读取配置。升级旧版配置时按当前连接初始化，保留原有明确禁用项；允许参与的配置保存于 `enabled_profiles`。
- **守护设置**：编辑检测间隔、重连超时、网卡、首选配置和检测地址，并开关四类自动修复（清理系统代理、修复 DNS、重连 Wi-Fi、失败后重置网卡）与“守护期间阻止系统休眠”。首次启动守护时自动保存配置；运行中修改设置需停止并重新启动守护。
- **运行日志**：查看实时输出；“清空显示”仅清空界面，不删除磁盘日志。

打开界面只读取 Wi-Fi 信息，点击“启动守护”后才会检测和自动修复。网络可用性包含有线网络；网卡信息单独显示 Wi-Fi 状态。

默认配置、轮换日志、PID 和连接状态写入项目目录（已被 `.gitignore` 排除）；相对路径以程序目录为基准，因此开机自启时不会写错位置。可用 `python gui.py --config 路径` 指定配置文件。已有配置中的日志路径等高级字段会保留。

## 命令行

```powershell
python main.py --save-default-config
python main.py
python main.py --no-wifi-recovery        # 只用代理/DNS 修复，不重连 Wi-Fi（有线环境）
python main.py --no-cleanup-proxy --no-repair-dns   # 关闭对应修复步骤
python main.py --no-prevent-sleep --heartbeat-interval 120
```

命令行参数会覆盖配置文件中的同名项：`--interval`、`--reconnect-timeout`、`--check-target`、`--preferred-profile`、`--interface`、`--log-file`、`--log-max-bytes`、`--log-backup-count`、`--state-file`、`--pid-file`、`--heartbeat-interval`、`--no-prevent-sleep`、`--no-cleanup-proxy`、`--no-repair-dns`、`--no-reset-adapter`、`--no-wifi-recovery`、`--verbose`。

GUI 与命令行是独立实例。使用 GUI 前请停止其他正在运行的守护实例，避免并行恢复。

## 管理员自启

右键以管理员身份运行 `install_admin_task.bat`，会创建最高权限的登录计划任务 `NetworkGuard`，直接启动 `gui.py --worker`（无窗口后台守护），并清理旧的 `start_network_guard.bat`。修改网卡 DNS 和重置网卡可能需要管理员权限；当前用户的系统代理开关通常不需要。无相应权限时会记录修复失败原因。

## 隐私说明

- 磁盘日志会把 SSID、AP BSSID、网卡物理地址与 GUID 替换为 `<redacted>` / `<mac>` / `<guid>`，界面仍正常显示这些信息。
- 默认检测地址与 DNS 监测域名均为公共站点，不含个人或单位内部域名。
- 配置、日志、状态、PID 文件默认被 `.gitignore` 排除，不随仓库分发。

如果没有读到 Wi-Fi 信息，请检查无线网卡、WLAN AutoConfig 服务，以及 Windows 对 Wi-Fi 信息的访问权限。

界面行高和侧栏宽度随字体尺寸调整；小窗口或高 DPI 下可通过右侧滚动条查看完整内容，较长的 Wi-Fi 名称可用列表底部横向滚动条查看。

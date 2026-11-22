# Network Guard 网络守护

Windows 断网自动恢复工具：定时检测网络，断了按“从轻到重”自动修复有线和无线连接。

当前版本 **0.1.0-beta.3** · [更新记录](CHANGELOG.md) · [与初始脚本对照](LEGACY_REVIEW.md)

## 两种启动方式

### 方式一：桌面界面（GUI）

双击 `start_gui.bat`，或执行：

```powershell
python gui.py
```

可视化设置、实时日志、逐条控制哪些 Wi-Fi 参与恢复，适合日常使用。

### 方式二：终端（CLI，无窗口）

双击 `start_cli.bat`，或执行：

```powershell
python main.py              # 前台运行，Ctrl+C 停止
python main.py --verbose    # 控制台同时输出调试日志
```

适合服务器、开机脚本或不想开窗口的场景，日志同样写入 `network_guard.log`。

两种方式共用同一把单实例锁，同一时间只能有一个守护在操作网络：重复启动 CLI 直接以返回码 2 退出，GUI 的“启动守护”会提示已有实例，重复双击 GUI 只保留一个窗口。

## 断线自动修复流程

**能上网就什么都不做。** 仅在 HTTP 检测失败时按顺序修复，前一步成功即停止：

1. **DNS** — 域名解析失败则清空缓存，并把网卡 DNS 回退为 DHCP 自动获取。
2. **系统代理** — Clash / FlClash 等代理退出后 Windows 仍发往失效端口（ping 通但应用断），此步关闭系统代理开关并清空 PAC。
3. **有线（优先）** — 典型故障是“网线插着但 DHCP 没下发地址”：IPv4 回退成 `169.254.x.x` 且无默认网关，重置 DNS 治不好，必须 `ipconfig /release` + `/renew` 重新获取租约；仍无效且允许时重置该网卡。虚拟网卡不参与判断。
4. **Wi-Fi** — 仅在有线不可用时重连（无有线网卡，或修复后链路仍不健康）。**有线正常绝不碰无线**——重连会抢占默认路由，可能主动打断一条本来正常的连接。按“上次成功 → 首选 → 其他已保存”尝试。
5. **无线网卡重置** — 全部失败后禁用再启用网卡；存在参与名单时跳过，避免 Windows 自动连接被排除的 Wi-Fi。

补充说明：

- 有线已有有效 IP 与网关时，直接跳过无线步骤。
- **ping 通不算恢复**：代理失效时 ping 依然通，是否在线一律以 HTTP(S) 判定。
- 代理清理与网卡重置分别有 60 秒、300 秒冷却；有线重置与第 5 步需管理员权限。
- 三个检测地址（ToDesk / 向日葵 / AweSun 官网）任意一个可达即视为应用层可用。这不等于远控会话健康：官网能开但软件登录或中继失败时不会触发恢复。

## 运行诊断

- **心跳** — 默认 60 秒一次，用于区分“没事发生”和“程序卡死”。
- **网卡识别回退** — `netsh wlan show interfaces` 在驱动重置时会挂起（MAC 显示 `00:00:00:00:00:00`、状态“正在断开连接”）。程序超时 10 秒后改用 PowerShell 识别网卡，日志会写明“已改用 PowerShell 识别到 N 个无线网卡”。
- **命令超时** — 子进程卡死时由守护线程限时放弃等待，主循环不会被拖死。
- **休眠 / 崩溃检测** — 休眠后立即重置状态重新检测；启动时若发现残留 PID 文件，会回放上次会话最后 15 行日志（带 `[上次]` 标记，不会混入本次启动的日志）。

## 仓库结构

```
gui.py / main.py                     GUI 与 CLI 两个入口
repairs.py                           网络修复动作（DNS、代理、有线、Wi-Fi、网卡重置）
updates.py                           检查 GitHub Release 更新
app_info.py                          版本号与可写数据目录
build.py                             PyInstaller 打包脚本
tests/                               单元测试与版本一致性校验
start_gui.bat / start_cli.bat        双击启动
install_admin_task.bat / enable_wlan.bat   开机自启 / 手动启用无线网卡
network_guard_config.example.json    配置模板（不含真实信息）
CHANGELOG.md / LEGACY_REVIEW.md      更新记录 / 与初始脚本的对照
LICENSE
```

不会入库的内容：真实配置 `network_guard_config.json`、日志与 PID、`dist/`、`build/`、`.vscode/`、`*.code-workspace`。

## 构建与测试

需要 Windows x64 与 Python 3.10+：

```powershell
python -m pip install -r requirements-build.txt
python -m unittest discover -s tests -t . -p "test_*.py"   # 全部用例
python build.py                                            # 生成 dist/ 目录 + ZIP + SHA-256
```

打包版无需安装 Python，解压后双击 `NetworkGuard.exe`，请保留同目录的 `_internal` 文件夹。

### 发布新版本

改好 `app_info.py` 里的 `VERSION` 与 `CHANGELOG.md`，提交后打标签推送：

```powershell
git tag -a v0.1.0-beta.3 -m "0.1.0-beta.3"
git push origin v0.1.0-beta.3
```

`.github/workflows/release.yml` 会自动在 Windows 上跑测试、构建 ZIP 与 SHA-256，并创建 **预发行** Release（标签中含 `-beta`、`-alpha`、`-rc` 即判定为预发行）。标签必须与 `VERSION` 完全一致，否则工作流直接失败。程序内置的“检查 GitHub 更新”依赖这个 Release，只推送代码不算发布。

## 界面说明

- **启动 / 停止守护** — 停止或关闭窗口会结束该窗口启动的守护进程。
- **已保存的 Wi-Fi** — 刷新配置与网卡状态，将所选配置设为首选；不读取也不展示 Wi-Fi 密码。
- **参与恢复开关** — 只有勾选的配置参与重连；新发现的配置默认不参与，全部关闭时不会发起连接。选择保存于 `enabled_profiles`。
- **守护设置** — 检测间隔、重连超时、网卡、首选配置、检测地址、五类修复开关、防休眠。运行中修改需重启守护才生效。
- **运行日志** — 实时输出；“清空显示”只清界面，不删磁盘日志。
- **遇到问题** — 左侧栏按钮，弹出反馈窗口：显示将生成的压缩包名，可一键「打包日志」或「打开日志文件夹」。

打开界面只读取 Wi-Fi 信息，点“启动守护”后才开始检测与修复。可用 `python gui.py --config 路径` 指定配置文件。

## 命令行参数

参数会覆盖配置文件中的同名项：

| 参数 | 作用 |
| --- | --- |
| `--config 路径` | 指定配置文件 |
| `--interval 秒` / `--reconnect-timeout 秒` | 检测间隔 / 单次重连超时 |
| `--check-target 地址` | 自定义 HTTP 检测地址（可重复） |
| `--preferred-profile 名称` / `--interface 网卡` | 首选 Wi-Fi / 指定网卡 |
| `--no-wifi-recovery` | 不重连 Wi-Fi（纯有线环境） |
| `--no-wired-recovery` | 不检查有线（纯无线环境） |
| `--no-cleanup-proxy` / `--no-repair-dns` / `--no-reset-adapter` | 关闭对应修复步骤 |
| `--no-prevent-sleep` | 不阻止系统休眠 |
| `--heartbeat-interval 秒` | 心跳日志间隔 |
| `--verbose` | 控制台输出调试日志 |
| `--export-logs [目录]` | 把最近的日志打包成 ZIP 后退出 |
| `--save-default-config` | 生成默认配置后退出 |

默认先修有线再修无线；没有有线网卡时自动跳过。完整参数见 `python main.py --help`。

## 开机自启

右键以管理员身份运行 `install_admin_task.bat`：创建最高权限的登录计划任务 `NetworkGuard`，以无窗口方式运行 `gui.py --worker`，并清理旧的 `start_network_guard.bat`。

重置网卡、修改网卡 DNS 需要管理员权限；系统代理开关通常不需要。无权限时会记录失败原因。

## 文件位置

| 运行方式 | 配置 / 日志 / 状态 / PID |
| --- | --- |
| 源码运行 | 项目目录（相对路径以程序目录为基准，开机自启不会写错位置） |
| 打包运行 | `%LOCALAPPDATA%\NetworkGuard` |

## 隐私说明

- 日志中的 SSID、AP BSSID、网卡物理地址与 GUID 会被替换为 `<redacted>` / `<mac>` / `<guid>`，界面仍正常显示。
- 检测地址与 DNS 监测域名均为公共站点，不含个人或单位内部域名。
- 配置、日志、状态、PID 均被 `.gitignore` 排除。需要版本化配置时请提交不含真实信息的 `network_guard_config.example.json`；真实配置中的 Wi-Fi 名称与内网地址不应入库。

## 反馈问题

遇到无法自动恢复的断网时，请把日志发来：

- **界面** — 点左侧栏「遇到问题」，弹窗里已经写明将生成的压缩包名（如 `NetworkGuard-logs-20261121-204831.zip`）。点「打包日志」即可生成，点「打开日志文件夹」可直接定位文件。
- **终端** — 运行 `python main.py --export-logs`（可指定目录：`--export-logs D:\temp`），命令会打印生成的文件名。

压缩包内含最近的日志（含轮转备份，已脱敏）和一份诊断说明，**不包含配置文件**（配置文件含个人 Wi-Fi 名称）。

请将压缩包发送到 **1164275580@qq.com**，并在邮件里说明：什么时候断的、有线还是无线、能否自动恢复、是否重启过。

如未读到 Wi-Fi 信息，请检查无线网卡、WLAN AutoConfig 服务，以及 Windows 对 Wi-Fi 信息的访问权限。

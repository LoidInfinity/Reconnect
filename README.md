<p align="center">
  <img src="assets/app-icon.png" width="180" alt="Network Guard 图标">
</p>

# Network Guard 网络守护

Windows 断网自动恢复工具：定时检测网络，断了按“从轻到重”自动修复有线和无线连接。

当前版本 **0.2.0-alpha.1** · [更新记录](CHANGELOG.md) · [与初始脚本对照](docs/LEGACY_REVIEW.md) · [安全说明](docs/SECURITY.md)

## 安装版（Setup.exe）

发布页除 ZIP 外还提供 `NetworkGuard-Setup-<版本>.exe`：

- 安装界面可**自选安装目录**（默认 `%LOCALAPPDATA%\Programs\NetworkGuard`），按当前用户安装，不触发 UAC。
- 安装完成后**自动清除下载标记（Mark of the Web）**，安装目录里的 `.exe` / `.bat` 不会再弹「无法验证发布者」。
- 自动创建开始菜单快捷方式（**图形界面**与**命令行**两个入口）、可选桌面快捷方式与标准卸载项；卸载只删程序目录，`%LOCALAPPDATA%\NetworkGuard` 里的配置和日志保留。

安装包由 GitHub Actions 在推送 `v*` 标签时自动构建，无需本地环境。


## 两种启动方式

### 方式一：桌面界面（GUI）

双击 `start_gui.bat`，或执行：

```powershell
python src/gui.py
```

可视化设置、实时日志、逐条控制哪些 Wi-Fi 参与恢复，适合日常使用。

### 方式二：终端（CLI，无窗口）

双击 `start_cli.bat`，或执行：

```powershell
python src/main.py              # 前台运行，Ctrl+C 停止
python src/main.py --verbose    # 控制台同时输出调试日志
```

适合服务器、开机脚本或不想开窗口的场景，日志同样写入 `data\logs\network_guard.log`。

打包/安装版同样提供命令行入口：双击 `start_cli.bat`（它调用 `NetworkGuardCLI.exe`，参数与源码版一致），或从开始菜单打开「Network Guard（命令行）」。

两种方式共用同一把单实例锁，同一时间只能有一个守护在操作网络：重复启动 CLI 直接以返回码 2 退出，GUI 的“启动守护”会提示已有实例，重复双击 GUI 只保留一个窗口。

## 首次使用：解除 Windows 安全提示

从网上下载 ZIP 后，Windows 会给解压出来的文件打上“来自 Internet”的标记（Mark of the Web）。双击 `start_gui.bat` 时会弹出：

> 无法验证发布者，此文件没有包含有效的数字签名以验证其发布者。

这不是程序故障，也不是文件损坏——`.bat` 文件本身**不支持代码签名**，Windows 没有可校验的签名，只能依据来源判断。所以这个提示靠“买证书”是消不掉的，只能把标记去掉。

### 一句话解决：双击 `unblock.bat`

解压后先双击同目录的 `unblock.bat`，它会清理本目录（含子目录）所有文件的下载标记。跑完再启动就不会再提示，且不需要管理员权限。

> 第一次双击 `unblock.bat` 自己仍会弹一次警告，点「运行」即可——它要做的就是去掉包括自己在内的所有标记。

### 其他方式

**A. 解压前解除（最彻底）**：右键下载的 ZIP → 属性 → 在「常规」页底部勾选「解除锁定」→ 确定，然后再解压。这样解压出的文件从头就不带标记。

**B. PowerShell 一行**（已经解压过时用，路径换成实际位置）：

```powershell
Get-ChildItem -Path "D:\NetworkGuard" -Recurse | Unblock-File
```

**C. 图形界面**：全选解压目录里的所有文件 → 右键 → 属性 → 勾选「解除锁定」→ 确定。

### 程序会自己修好

第一次点「运行」之后，程序会在后台清理自己目录下所有文件的标记，此后启动（包括双击 bat）都不再提示。所以即使跳过上面所有步骤，代价也只是**多弹一次**。

### 关于「打开文件时总是询问」

那个复选框只是记住你的选择，**并不会去掉文件上的标记**；脚本宿主和部分安全软件仍会认为文件不可信。想彻底干净，还是要用 `unblock.bat` 或「解除锁定」。

### 开机自启为什么不受影响

`install_admin_task.bat` 创建的是**计划任务**，由任务计划程序直接拉起程序，不经过资源管理器，因此不会触发这个安全警告。只有放在「启动」文件夹里的快捷方式才会走同一套检查。

压缩包里附有 `READ-ME-FIRST.txt`，写明同样步骤，分发时无需另行解释。

## 断线自动修复流程

**能上网就什么都不做。** 仅在 HTTP 检测失败时按顺序修复，前一步成功即停止：

1. **Wi-Fi 无线电** — 先读无线电开关状态。被系统关掉时（飞行模式、Wi-Fi 开关被关、驱动进入低功耗），任何 `netsh wlan connect` 都会返回 `2150899714`“接口电源关闭”，此时用系统无线电 API 重新打开并等待回连。普通用户权限即可，不需要管理员。**这一步只在没有健康有线时执行。**
2. **DNS** — 域名解析失败则清空缓存，并把网卡 DNS 回退为 DHCP 自动获取。
3. **系统代理** — Clash / FlClash 等代理退出后 Windows 仍发往失效端口（ping 通但应用断），此步关闭系统代理开关并清空 PAC。
4. **有线（优先）** — 典型故障是“网线插着但 DHCP 没下发地址”：IPv4 回退成 `169.254.x.x` 且无默认网关，重置 DNS 治不好，必须 `ipconfig /release` + `/renew` 重新获取租约；仍无效且允许时重置该网卡。虚拟网卡不参与判断。网线未插、网卡被禁用、或网卡是静态 IP（未启用 DHCP）时直接跳过，不浪费重连预算。
5. **Wi-Fi** — 仅在有线不可用时重连（无有线网卡，或修复后链路仍不健康）。**有线正常绝不碰无线**——重连会抢占默认路由，可能主动打断一条本来正常的连接。按“上次成功 → 首选 → 其他已保存”尝试。
6. **无线网卡重置** — 全部失败后禁用再启用网卡；**无线电正常**且存在参与名单时跳过，避免 Windows 自动连接被排除的 Wi-Fi。无线电被系统关闭时不再跳过——此时没有“正在使用的其它配置”会被抢占，重置正是把无线电拉回来的最后手段。

**启动时还会做一件预防性设置：关闭网卡节能**（等同于设备管理器 → 网络适配器 → 电源管理 → 取消“允许计算机关闭此设备以节约电源”）。它只在启动/保存设置后执行一次，需要管理员权限；没有权限时只记一条警告，不影响其他修复。配置项 `disable_adapter_power_saving`（默认开），界面在「守护设置」里可关闭，命令行用 `--no-adapter-power-saving` 跳过。

补充说明：

- **有线健康时连无线电都不碰**：只要有一条有线链路持有有效 IP 与网关，本轮直接跳过无线电、Wi-Fi 重连与网卡重置——不读无线电状态、不打开无线电。很多笔记本/扩展坞在插网线时 WLAN 会被 OEM 策略关掉，那不是故障；此时强行打开无线反而可能把正在工作的有线弄断。只有确认有线不可用（未插网线、无有效租约、未启用 DHCP）才会动无线。
- 上述保护依赖“检查有线”，因此显式使用 `--no-wired-recovery`（纯无线环境）时不再探测有线，也就无法确认有线是否健康；默认配置不要关闭它。
- 有线已有有效 IP 与网关时，直接跳过无线步骤。
- **ping 通不算恢复**：代理失效时 ping 依然通，是否在线一律以 HTTP(S) 判定；`无法访问目标主机` 这类回复也不算 ping 通。
- 代理清理与网卡重置分别有 60 秒、300 秒冷却；有线重置、第 6 步与关闭网卡节能需管理员权限，无线电恢复不需要。
- 三个检测地址（ToDesk / 向日葵 / AweSun 官网）任意一个可达即视为应用层可用。这不等于远控会话健康：官网能开但软件登录或中继失败时不会触发恢复。

## 运行诊断

- **心跳** — 默认 60 秒一次，用于区分“没事发生”和“程序卡死”。
- **启动诊断** — 启动时记录运行权限（管理员 / 普通用户）、Wi-Fi 无线电状态、网卡与已保存配置，断线原因可以直接对着看。
- **无线电状态** — 日志出现「检测到 Wi-Fi 无线电已关闭」「无法自动打开 Wi-Fi 无线电」时，说明是系统关闭了无线（飞行模式/开关/驱动），程序会先尝试自动打开再重连。
- **网卡识别回退** — `netsh wlan show interfaces` 在驱动重置时会挂起（MAC 显示 `00:00:00:00:00:00`、状态“正在断开连接”）。程序超时 10 秒后改用 PowerShell 识别网卡，日志会写明“已改用 PowerShell 识别到 N 个无线网卡”。
- **命令超时** — 子进程卡死时由守护线程限时放弃等待，主循环不会被拖死。
- **休眠 / 崩溃检测** — 休眠后立即重置状态重新检测；启动时若发现残留 PID 文件，会回放上次会话最后 15 行日志（带 `[上次]` 标记，不会混入本次启动的日志）。

## 延迟监控与折线图

每一轮 HTTP 检测都会记录往返延迟，并在日志里显示：

```
本轮检测：网络可用 · 延迟 45 ms
心跳：守护运行中，恢复成功 1/2 · 最近延迟 52 ms
```

- 数据保存在 `data\latency.csv`（成功记录延迟，失败记录为标记行；超过 2 MB 自动裁剪）。
- **界面**：运行日志页点「延迟折线图」，生成 SVG 并用默认浏览器打开。
- 图表包含平均/最高/最新延迟、失败时刻红色竖线与时间轴；SVG 无需额外依赖，可直接发给他人或插入文档。

## 检查更新与更新通道

侧边栏「检查 GitHub 更新」会读取 Release 列表。渠道在「守护设置 → 检查更新」里切换（配置项 `update_channel`）：

| 渠道 | 会提示的版本 |
| --- | --- |
| **稳定版**（默认） | 只提示正式版（如 `0.1.0`、`0.2.0`） |
| **公测版** | 正式版 + `beta` / `rc` 预发行版 |
| **内测版** | 正式版 + `alpha` / `beta` / `rc` 全部版本 |

所以正式版用户默认看不到 `0.2.0-alpha.1`；把渠道切到「内测版」再点检查更新即可。预发行版只会在对应渠道内提示，避免普通用户被自动带到实验版本。

## 仓库结构

```
src/
  gui.py / main.py                   GUI 与 CLI 两个入口
  repairs.py                         网络修复动作（DNS、代理、有线、Wi-Fi、网卡重置）
  latency.py                         延迟记录与 SVG 折线图
  updates.py                         检查 GitHub Release 更新
  app_info.py                        版本号与可写数据目录
  build.py                           PyInstaller + Inno Setup 打包脚本
  installer.iss                      安装包脚本（自选目录、安装时清除下载标记）
  ChineseSimplified.isl              安装界面中文语言文件（Inno Setup 官方）
tests/                               单元测试与版本一致性校验
assets/                              图标：Q 版（GUI/README）、平面版（CLI）与多尺寸 ICO
config/network_guard_config.example.json   配置模板（不含真实信息）
docs/LEGACY_REVIEW.md                与初始脚本的对照
docs/SECURITY.md                     安全边界、SSID 注入防护与残余风险
start_gui.bat / start_cli.bat        双击启动
install_admin_task.bat / enable_wlan.bat   开机自启 / 手动启用无线网卡
requirements-build.txt / CHANGELOG.md / LICENSE
```

运行时自动生成的全部内容都收在 `data/`（配置、状态、PID、`latency.csv` 延迟记录与 `charts/` 折线图，以及 `data\logs\` 下的日志），不会弄脏项目目录，也不入库；同理不入库的还有 `dist/`、`build/`、`.vscode/`、`*.code-workspace`。

## 构建与测试

需要 Windows x64 与 Python 3.10+：

```powershell
python -m pip install -r requirements-build.txt
python -m unittest discover -s tests -t . -p "test_*.py"   # 全部用例
python src/build.py                                        # 目录 + ZIP + 安装包（若装了 Inno Setup）
```

`build.py` 会输出目录版、`NetworkGuard-*-windows-x64.zip` 和（检测到 `ISCC.exe` 时）`NetworkGuard-Setup-<版本>.exe`，三者都带 `.sha256`。安装包需要 Inno Setup 6；本机没有时脚本会跳过安装包，不影响其他产物。CI 上由工作流自动用 Chocolatey 安装。

打包版无需安装 Python。解压或安装后有两个入口：`NetworkGuard.exe`（图形界面）与 `start_cli.bat` / `NetworkGuardCLI.exe`（命令行模式），请保留同目录的 `_internal` 文件夹。

### 发布新版本

改好 `src/app_info.py` 里的 `VERSION` 与 `CHANGELOG.md`，提交后打标签推送：

```powershell
git tag -a v0.2.0-alpha.1 -m "0.2.0-alpha.1"
git push origin v0.2.0-alpha.1
```

`.github/workflows/release.yml` 会在 **推送 `v*` 标签时自动**触发：跑测试、校验标签与版本一致、构建 ZIP 与安装包、生成 SHA-256，并创建 **预发行** Release（标签中含 `-beta`、`-alpha`、`-rc` 即判定为预发行）。标签必须与 `VERSION` 完全一致，否则工作流直接失败。程序内置的“检查 GitHub 更新”依赖这个 Release，只推送代码不算发布。

## 界面说明

- **启动 / 停止守护** — 停止或关闭窗口会结束该窗口启动的守护进程。
- **已保存的 Wi-Fi** — 刷新配置与网卡状态，将所选配置设为首选；不读取也不展示 Wi-Fi 密码。点击表头可按名称/优先顺序/参与状态排序（再点反向）；「优先顺序」列显示 1…N；「按当前顺序设为优先」把列表当前顺序写回首选配置。「从列表移除未选定的」只在本软件的列表里隐藏未勾选的配置（Windows 中仍保留，密码不受影响），可用「恢复已隐藏」找回；隐藏名单保存在 `config\hidden_wifi.txt`，已加入 `.gitignore`。
- **参与恢复开关** — 只有勾选的配置参与重连；新发现的配置默认不参与，全部关闭时不会发起连接。选择保存于 `enabled_profiles`。
- **守护设置** — 检测间隔、重连超时、网卡、首选配置、检测地址、七类修复开关（含「关闭网卡节能」）、防休眠。运行中修改需重启守护才生效。
- **运行日志** — 实时输出；“清空显示”只清界面，不删磁盘日志；“延迟折线图”按最近 24 小时数据生成 SVG 并打开。
- **主界面不滚动** — 窗口按内容自适应，打开时即保证全部内容可见；设置页在极小窗口或高 DPI 下才会出现页内滚动条，列表与日志各自保留自己的滚动。
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
| `--no-radio-recovery` | 无线被系统关闭时不自动打开无线电 |
| `--no-adapter-power-saving` | 不修改网卡节能（默认取消“允许计算机关闭此设备”） |
| `--no-wired-recovery` | 不检查有线（纯无线环境） |
| `--no-cleanup-proxy` / `--no-repair-dns` / `--no-reset-adapter` | 关闭对应修复步骤 |
| `--no-prevent-sleep` | 不阻止系统休眠 |
| `--heartbeat-interval 秒` | 心跳日志间隔 |
| `--verbose` | 控制台输出调试日志 |
| `--export-logs [目录]` | 把最近的日志打包成 ZIP 后退出 |
| `--save-default-config` | 生成默认配置后退出 |

默认先修有线再修无线；没有有线网卡时自动跳过。完整参数见 `python src/main.py --help`。

## 开机自启

右键以管理员身份运行 `install_admin_task.bat`：创建最高权限的登录计划任务 `NetworkGuard`，以无窗口方式运行 `src\gui.py --worker`，并清理旧的 `start_network_guard.bat`。

重置网卡、修改网卡 DNS 需要管理员权限；打开 Wi-Fi 无线电与系统代理开关通常不需要。无权限时会记录失败原因。

## 文件位置

| 运行方式 | 数据目录（配置 / 状态 / PID） | 日志 |
| --- | --- | --- |
| 源码运行 | `data\` | `data\logs\` |
| 打包运行（ZIP / 安装版） | `%LOCALAPPDATA%\NetworkGuard` | `%LOCALAPPDATA%\NetworkGuard\logs\` |

安装版把程序装到所选目录（默认 `%LOCALAPPDATA%\Programs\NetworkGuard`），数据仍写到 `%LOCALAPPDATA%\NetworkGuard`，因此升级或重装不会丢配置与日志。

GUI 的 Wi-Fi 隐藏名单存在程序目录下的 `config\hidden_wifi.txt`（打包版为安装目录下的同名文件；无写入权限时回退到数据目录），该文件已加入 `.gitignore`，不会入库。

相对路径以数据目录为基准，因此开机自启（工作目录往往是 `System32`）也不会写错位置。旧版本散落在项目根目录的文件会在首次启动时自动迁移到上述位置。

## 安全提示

SSID / 配置名被当作**不可信输入**处理：所有命令走参数列表（`shell=False`），PowerShell 不再拼接变量，含换行/ANSI/双向控制符的名称不参与重连，日志与单行名单文件都会清洗；安装包也不再通过 PowerShell 拼接安装路径。详见 [安全说明](docs/SECURITY.md)。

程序**无法识别同名热点（邪恶双胞胎）**：重连只按配置名进行，AP 身份由 Windows 的 WPA/802.1X 负责。开放网络（无密码）容易被同名伪造，敏感场景请配合 VPN 或改用有认证的网络。

## 隐私说明

- 日志中的 SSID、AP BSSID、网卡物理地址与 GUID 会被替换为 `<redacted>` / `<mac>` / `<guid>`；Wi-Fi 配置名以 `<profile:xxxx>` 形式出现（可跨行关联，但不暴露名称），界面仍正常显示。
- 检测地址与 DNS 监测域名均为公共站点，不含个人或单位内部域名。
- 配置、日志、状态、PID 均被 `.gitignore` 排除。需要版本化配置时请提交不含真实信息的 `config/network_guard_config.example.json`；真实配置中的 Wi-Fi 名称与内网地址不应入库。

## 反馈问题

遇到无法自动恢复的断网时，请把日志发来：

- **界面** — 点左侧栏「遇到问题」，弹窗里已经写明将生成的压缩包名（如 `NetworkGuard-logs-20261121-204831.zip`）。点「打包日志」即可生成，点「打开日志文件夹」可直接定位文件。
- **终端** — 运行 `python src/main.py --export-logs`（可指定目录：`--export-logs D:\temp`），命令会打印生成的文件名。

压缩包内含最近的日志（含轮转备份，已脱敏）和一份诊断说明，**不包含配置文件**（配置文件含个人 Wi-Fi 名称）。

请将压缩包发送到 **1164275580@qq.com**，并在邮件里说明：什么时候断的、有线还是无线、能否自动恢复、是否重启过。

如未读到 Wi-Fi 信息，请检查无线网卡、WLAN AutoConfig 服务，以及 Windows 对 Wi-Fi 信息的访问权限。

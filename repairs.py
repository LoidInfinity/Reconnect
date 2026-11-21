"""Deep network repairs shared by the GUI worker and the CLI.

Ported from the legacy ``network_main.py`` so both entry points run one
implementation: system-proxy cleanup, DNS repair, Wi-Fi adapter reset,
sleep guard, and PID/crash bookkeeping.

Everything here is Windows-only and best-effort: failures are logged and
returned as ``False`` instead of raising, because a repair step must never
take the monitoring loop down.
"""
from __future__ import annotations
from contextlib import contextmanager

import atexit
import ctypes
import json
import socket
import sys
import time
from pathlib import Path
from typing import Any, Callable, Iterable

try:  # pragma: no cover - non-Windows
    import winreg  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover
    winreg = None  # type: ignore[assignment]

INTERNET_SETTINGS_KEY = r"Software\Microsoft\Windows\CurrentVersion\Internet Settings"

# WinINet broadcast so running browsers notice proxy changes immediately.
_INTERNET_OPTION_SETTINGS_CHANGED = 39
_INTERNET_OPTION_REFRESH = 37

# Windows sleep-prevention API.
_ES_CONTINUOUS = 0x80000000
_ES_SYSTEM_REQUIRED = 0x00000001
_ES_AWAYMODE_REQUIRED = 0x00000040

# Cooldowns stop repair loops from hammering the same subsystem.
ADAPTER_RESET_COOLDOWN = 300
PROXY_CLEANUP_COOLDOWN = 60

_last_adapter_reset_time = 0.0
_last_proxy_cleanup_time = 0.0


def _run(command: list[str], logger, timeout: int | None = 30):
    """Call ``main.run_command`` lazily to avoid a circular import."""
    import main as guard

    return guard.run_command(command, logger, timeout)


# ---------------------------------------------------------------- sleep guard


def prevent_sleep(logger) -> bool:
    """Keep the machine awake; the display may still turn off."""
    try:
        ctypes.windll.kernel32.SetThreadExecutionState(
            _ES_CONTINUOUS | _ES_SYSTEM_REQUIRED | _ES_AWAYMODE_REQUIRED
        )
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("无法设置休眠阻止: %s", exc)
        return False


def restore_sleep() -> None:
    try:
        ctypes.windll.kernel32.SetThreadExecutionState(_ES_CONTINUOUS)
    except Exception:  # noqa: BLE001
        pass


def enable_sleep_guard(logger) -> bool:
    """Prevent sleep for this thread and restore the policy at exit."""
    if not prevent_sleep(logger):
        return False
    atexit.register(restore_sleep)
    return True


def detect_sleep(logger, expected: float, actual: float) -> bool:
    """Report a suspend/resume gap when a wait took far longer than asked."""
    ratio = actual / expected if expected > 0 else 0
    if ratio > 2.5 and actual > 30:
        logger.warning(
            "⚠ 检测到可能的系统休眠/睡眠：预期等待 %.1f 秒，实际经过 %.0f 秒（%.1f 倍）",
            expected,
            actual,
            ratio,
        )
        return True
    return False


# ------------------------------------------------------------- system proxies


def _notify_proxy_settings_changed(logger) -> None:
    try:
        wininet = ctypes.windll.wininet
        wininet.InternetSetOptionW(0, _INTERNET_OPTION_SETTINGS_CHANGED, 0, 0)
        wininet.InternetSetOptionW(0, _INTERNET_OPTION_REFRESH, 0, 0)
    except Exception as exc:  # noqa: BLE001
        logger.debug("广播代理设置变更失败: %s", exc)


def get_system_proxy_settings() -> dict[str, Any]:
    if winreg is None:
        return {}
    settings: dict[str, Any] = {}
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, INTERNET_SETTINGS_KEY) as key:
            for name in ("ProxyEnable", "ProxyServer", "ProxyOverride", "AutoConfigURL"):
                try:
                    settings[name], _ = winreg.QueryValueEx(key, name)
                except FileNotFoundError:
                    settings[name] = None
    except OSError:
        pass
    return settings


def is_local_proxy(proxy_server: Any) -> bool:
    """True when the proxy points at this machine (FlClash/Clash/Mihomo)."""
    if not proxy_server or not isinstance(proxy_server, str):
        return False
    server = proxy_server.split(";")[0].strip().lower()
    if "=" in server:
        server = server.split("=", 1)[1].strip()
    host = server.rsplit(":", 1)[0] if ":" in server else server
    return host in ("127.0.0.1", "localhost", "::1", "0.0.0.0")


def disable_system_proxy(logger) -> bool:
    if winreg is None:
        logger.error("当前平台不支持读写系统代理设置。")
        return False
    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, INTERNET_SETTINGS_KEY, 0, winreg.KEY_SET_VALUE
        ) as key:
            winreg.SetValueEx(key, "ProxyEnable", 0, winreg.REG_DWORD, 0)
            winreg.SetValueEx(key, "AutoConfigURL", 0, winreg.REG_SZ, "")
        _notify_proxy_settings_changed(logger)
        return True
    except OSError as exc:
        logger.error("关闭系统代理失败: %s", exc)
        return False


def clear_proxy_settings(logger) -> bool:
    """Turn the system proxy off so traffic goes direct again.

    Targets the classic failure where a local proxy tool died but Windows
    still points every request at a dead port. Only the Windows switch is
    touched, never the proxy tool's own configuration.
    """
    global _last_proxy_cleanup_time

    if winreg is None:
        return False
    settings = get_system_proxy_settings()
    if not settings.get("ProxyEnable") and not settings.get("AutoConfigURL"):
        logger.info("当前未启用系统代理，无需清理。")
        return False

    now = time.time()
    if now - _last_proxy_cleanup_time < PROXY_CLEANUP_COOLDOWN:
        logger.warning("⏳ 距上次代理清理不足 %d 秒，本次跳过。", PROXY_CLEANUP_COOLDOWN)
        return False
    _last_proxy_cleanup_time = now

    proxy_server = settings.get("ProxyServer")
    logger.warning(
        "检测到系统代理已启用：ProxyEnable=%s，ProxyServer=%s，AutoConfigURL=%s",
        settings.get("ProxyEnable"),
        proxy_server or "(无)",
        settings.get("AutoConfigURL") or "(无)",
    )
    if proxy_server and not is_local_proxy(proxy_server):
        logger.warning("注意：代理服务器并非指向本机，仍按配置清理。")
    logger.debug("原始代理设置备份: %s", json.dumps(settings, ensure_ascii=False))

    if disable_system_proxy(logger):
        logger.info("✓ 已清理系统代理设置（ProxyEnable=0，PAC 已清空），流量改为直连。")
        return True
    return False


# --------------------------------------------------------------------- DNS


def check_dns_resolution(logger, hosts: Iterable[str]) -> list[str]:
    failed: list[str] = []
    for host in hosts:
        if not host:
            continue
        try:
            socket.gethostbyname(host)
            logger.debug("DNS 解析成功: %s", host)
        except Exception as exc:  # noqa: BLE001
            logger.debug("DNS 解析失败: %s, %s", host, exc)
            failed.append(host)
    return failed


def get_up_interface_names(logger) -> list[str]:
    """Physical adapters that are currently Up, virtual ones excluded."""
    try:
        completed = _run(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                "(Get-NetAdapter | Where-Object { $_.Status -eq 'Up' -and "
                "$_.InterfaceDescription -notmatch 'VMware|Hyper-V|Virtual|Loopback' }"
                ").Name",
            ],
            logger,
            15,
        )
        return [line.strip() for line in (completed.stdout or "").splitlines() if line.strip()]
    except Exception as exc:  # noqa: BLE001
        logger.debug("获取网卡列表失败: %s", exc)
        return []


def flush_dns_cache(logger) -> bool:
    try:
        completed = _run(["ipconfig", "/flushdns"], logger, 15)
        if completed.returncode == 0:
            logger.info("✓ 已清空 DNS 解析缓存")
            return True
        logger.warning("清空 DNS 缓存返回码非 0: %s", completed.returncode)
    except Exception as exc:  # noqa: BLE001
        logger.warning("清空 DNS 缓存失败: %s", exc)
    return False


def reset_interface_dns(logger, interface_names: Iterable[str]) -> bool:
    done = False
    for name in interface_names:
        if not name:
            continue
        try:
            completed = _run(
                ["netsh", "interface", "ipv4", "set", "dnsservers", f"name={name}", "source=dhcp"],
                logger,
                15,
            )
            if completed.returncode == 0:
                logger.info("✓ 已将网卡 '%s' 的 DNS 重置为自动获取", name)
                done = True
            else:
                logger.debug("重置网卡 '%s' DNS 返回码: %s", name, completed.returncode)
        except Exception as exc:  # noqa: BLE001
            logger.debug("重置网卡 '%s' DNS 失败: %s", name, exc)
    return done


def repair_dns(logger, interface_names: Iterable[str]) -> bool:
    """Flush the resolver cache and hand DNS back to DHCP."""
    logger.warning("⚠ 检测到 DNS 解析故障，尝试自动修复…")
    repaired = flush_dns_cache(logger)
    time.sleep(1)
    if reset_interface_dns(logger, interface_names):
        repaired = True
    if repaired:
        time.sleep(2)
    return repaired


# ------------------------------------------------- wired / wireless link state

VIRTUAL_ADAPTER_PATTERN = "VMware|Hyper-V|Virtual|Loopback|TAP|TUN|WSL|Bluetooth"


def get_physical_adapters(logger) -> list[dict[str, str]]:
    """Physical adapters with status, IPv4 address and default gateway.

    Virtual adapters are excluded: they are always ``Up`` and would make an
    outage look healthy. ``MediaType`` tells wired (802.3) from wireless
    (Native 802.11) so recovery can prefer the cable.
    """
    script = (
        "Get-NetAdapter | Where-Object { $_.Status -ne 'Not Present' -and "
        f"$_.InterfaceDescription -notmatch '{VIRTUAL_ADAPTER_PATTERN}' }} | "
        "ForEach-Object { "
        "$ip = Get-NetIPAddress -InterfaceIndex $_.ifIndex -AddressFamily IPv4 "
        "-ErrorAction SilentlyContinue | Select-Object -First 1; "
        "$gw = Get-NetRoute -InterfaceIndex $_.ifIndex -DestinationPrefix '0.0.0.0/0' "
        "-ErrorAction SilentlyContinue | Select-Object -First 1; "
        "[pscustomobject]@{ "
        "Name = $_.Name; Status = $_.Status; MediaType = $_.MediaType; "
        "IP = $ip.IPAddress; Gateway = $gw.NextHop } "
        "} | ConvertTo-Json -Compress -Depth 3"
    )
    try:
        completed = _run(["powershell", "-NoProfile", "-Command", script], logger, 20)
        raw = (completed.stdout or "").strip()
        if not raw:
            return []
        parsed = json.loads(raw)
        items = parsed if isinstance(parsed, list) else [parsed]
        return [item for item in items if isinstance(item, dict)]
    except Exception as exc:  # noqa: BLE001
        logger.debug("获取物理网卡信息失败: %s", exc)
        return []


def is_apipa(address: str | None) -> bool:
    """``169.254.x.x`` means DHCP never answered and Windows self-assigned."""
    return bool(address) and str(address).startswith("169.254.")


def is_wired(adapter: dict) -> bool:
    """True for Ethernet adapters (802.3), False for Wi-Fi (Native 802.11)."""
    media = str(adapter.get("MediaType") or "")
    name = str(adapter.get("Name") or "").casefold()
    if "802.11" in media:
        return False
    if media:
        return "802.3" in media
    # MediaType missing: fall back to the usual WLAN naming.
    return not any(word in name for word in ("wlan", "wi-fi", "wifi", "无线"))


def is_link_healthy(adapter: dict) -> bool:
    """Up + a real IPv4 lease (not APIPA) + a default gateway."""
    if str(adapter.get("Status") or "").casefold() != "up":
        return False
    address = adapter.get("IP")
    return bool(address) and not is_apipa(address) and bool(adapter.get("Gateway"))


def renew_dhcp(logger, interface_name: str) -> bool:
    """Release/renew the lease so an APIPA address can be replaced.

    This is the repair for "cable is plugged in but there is no IPv4
    address": DNS resets cannot fix a missing lease.
    """
    if not interface_name:
        return False
    logger.warning("⚠ 对网卡 '%s' 执行 DHCP 续约…", interface_name)
    _run(["ipconfig", "/release", interface_name], logger, 20)
    time.sleep(1)
    completed = _run(["ipconfig", "/renew", interface_name], logger, 40)
    if completed.returncode == 0:
        logger.info("✓ 网卡 '%s' DHCP 续约命令已完成", interface_name)
        return True
    logger.warning("网卡 '%s' DHCP 续约返回码: %s", interface_name, completed.returncode)
    return False


def _reset_named_adapter(logger, adapter_name: str) -> bool:
    """Disable/enable one named adapter, verifying the state afterwards."""
    script = (
        '$ErrorActionPreference = "Stop"; '
        "try { "
        f"  $adapter = Get-NetAdapter -Name '{adapter_name}' -ErrorAction Stop; "
        '  if ($adapter.Status -eq "Disabled") { '
        "    Enable-NetAdapter -Name $adapter.Name -Confirm:$false; "
        "  } else { "
        "    Disable-NetAdapter -Name $adapter.Name -Confirm:$false; "
        "    Start-Sleep -Seconds 3; "
        "    Enable-NetAdapter -Name $adapter.Name -Confirm:$false; "
        "  } "
        "  Start-Sleep -Seconds 3; "
        "  $now = Get-NetAdapter -Name $adapter.Name; "
        '  if ($now.Status -eq "Up") { Write-Output "OK:$($adapter.Name)" } '
        '  else { Write-Output "ENABLE_FAILED:STATUS=$($now.Status)" } '
        "} catch { "
        '  if ($_.Exception.Message -match "拒绝访问|Access is denied|Access Denied|PermissionDenied|Error 5") { '
        "    Write-Output 'ACCESS_DENIED' "
        "  } else { "
        '    Write-Output ("ERROR:" + $_.Exception.Message) '
        "  } "
        "}"
    )
    try:
        completed = _run(["powershell", "-NoProfile", "-Command", script], logger, 25)
        lines = (completed.stdout or "").strip().splitlines()
        last = lines[-1] if lines else ""
        if last.startswith("OK:"):
            logger.info("✓ 网卡 '%s' 已重置（状态确认为 Up）", last[3:])
            time.sleep(3)
            return True
        if last == "ACCESS_DENIED":
            logger.error("⛔ 网卡重置被拒绝：需要管理员权限。")
        elif last.startswith("ENABLE_FAILED"):
            logger.warning("网卡启用未达预期: %s", last)
        elif last.startswith("ERROR:"):
            logger.warning("网卡重置脚本错误: %s", last)
        else:
            logger.warning("网卡重置结果未知: %r", last)
    except Exception as exc:  # noqa: BLE001
        logger.warning("网卡 '%s' 重置失败: %s", adapter_name, exc)
    return False


def has_healthy_wired_link(logger) -> bool:
    """True when a wired adapter already holds a usable lease.

    Read-only probe: used to decide whether the wireless path may be skipped.
    Reconnecting Wi-Fi while the cable is working can steal the default route
    and briefly interrupt a perfectly good connection.
    """
    adapters = get_physical_adapters(logger)
    return any(is_wired(item) and is_link_healthy(item) for item in adapters)


def repair_wired_links(logger, allow_reset: bool = False,
                       adapters: list[dict] | None = None) -> bool:
    """Repair wired adapters: renew the lease first, reset the adapter last.

    Returns ``True`` when a repair action was attempted and the adapter now
    holds a usable lease, so the caller can re-test connectivity.
    """
    if adapters is None:
        adapters = get_physical_adapters(logger)
    wired = [item for item in adapters if is_wired(item)]
    if not wired:
        logger.debug("未检测到有线网卡，跳过有线修复。")
        return False

    unhealthy = [item for item in wired if not is_link_healthy(item)]
    if not unhealthy:
        logger.info("有线连接正常（已获得有效 IP 与网关）。")
        return False

    for adapter in unhealthy:
        name = str(adapter.get("Name") or "")
        if not name:
            continue
        logger.warning(
            "有线网卡 '%s' 异常：状态=%s，IP=%s，网关=%s%s",
            name,
            adapter.get("Status") or "?",
            adapter.get("IP") or "无",
            adapter.get("Gateway") or "无",
            "（DHCP 未响应，已回退 APIPA）" if is_apipa(adapter.get("IP")) else "",
        )
        renew_dhcp(logger, name)
        refreshed = get_physical_adapters(logger)
        mine = next((x for x in refreshed if x.get("Name") == name), None)
        if mine and is_link_healthy(mine):
            logger.info("✓ 有线网卡 '%s' 已恢复有效租约。", name)
            return True
        if allow_reset:
            logger.warning("DHCP 续约未能恢复 '%s'，尝试重置该网卡…", name)
            if _reset_named_adapter(logger, name):
                renew_dhcp(logger, name)
                refreshed = get_physical_adapters(logger)
                mine = next((x for x in refreshed if x.get("Name") == name), None)
                if mine and is_link_healthy(mine):
                    logger.info("✓ 有线网卡 '%s' 重置后已恢复。", name)
                    return True
    return False


# ------------------------------------------------------------ adapter reset


def reset_wifi_adapter(logger) -> bool:
    """Disable/enable the Wi-Fi adapter, the only recovery without a reboot.

    Guarded by a cooldown and a ``wlansvc`` check: toggling during a WLAN
    service crash can leave the adapter stuck Disabled (CM_PROB_DISABLED).
    """
    global _last_adapter_reset_time

    now = time.time()
    if now - _last_adapter_reset_time < ADAPTER_RESET_COOLDOWN:
        logger.warning(
            "⏳ 距上次网卡重置不足 %d 秒，跳过本次硬件重置。", ADAPTER_RESET_COOLDOWN
        )
        return False
    _last_adapter_reset_time = now

    try:
        service = _run(
            ["powershell", "-NoProfile", "-Command",
             "Get-Service wlansvc | Select-Object -ExpandProperty Status"],
            logger,
            10,
        )
        service_running = "Running" in (service.stdout or "")
    except Exception:  # noqa: BLE001
        service_running = True
    if not service_running:
        logger.error("⛔ WLAN AutoConfig 服务(wlansvc)不可用，跳过网卡重置。请先重启该服务。")
        return False

    logger.warning("⚠ 尝试重置 Wi-Fi 网卡以恢复硬件状态…")
    try:
        _run(["netsh", "wlan", "disconnect"], logger, 5)
    except Exception:  # noqa: BLE001
        pass
    time.sleep(2)

    script = (
        '$ErrorActionPreference = "Stop"; '
        "try { "
        '  $adapter = Get-NetAdapter -Name "*Wi-Fi*","*WLAN*" '
        '    | Where-Object { $_.Status -in @("Up","Disabled","Disconnected") } '
        "    | Select-Object -First 1; "
        "  if (-not $adapter) { Write-Output 'NO_ADAPTER'; exit 0 } "
        '  if ($adapter.Status -eq "Disabled") { '
        "    Enable-NetAdapter -Name $adapter.Name -Confirm:$false; "
        "    Start-Sleep -Seconds 3; "
        "    $enabled = Get-NetAdapter -Name $adapter.Name; "
        '    if ($enabled.Status -eq "Up") { Write-Output "OK:$($adapter.Name)" } '
        '    else { Write-Output "ENABLE_FAILED:STATUS=$($enabled.Status)"; exit 0 } '
        "  } else { "
        "    Disable-NetAdapter -Name $adapter.Name -Confirm:$false; "
        "    Start-Sleep -Seconds 3; "
        "    $disabled = Get-NetAdapter -Name $adapter.Name; "
        '    if ($disabled.Status -ne "Disabled") { '
        '      Write-Output "DISABLE_FAILED:STATUS=$($disabled.Status)"; exit 0 '
        "    } "
        "    Enable-NetAdapter -Name $adapter.Name -Confirm:$false; "
        "    Start-Sleep -Seconds 3; "
        "    $enabled = Get-NetAdapter -Name $adapter.Name; "
        '    if ($enabled.Status -eq "Up") { Write-Output "OK:$($adapter.Name)" } '
        '    else { Write-Output "ENABLE_FAILED:STATUS=$($enabled.Status)"; exit 0 } '
        "  } "
        "} catch { "
        '  if ($_.Exception.Message -match "拒绝访问|Access is denied|Access Denied|PermissionDenied|Error 5") { '
        "    Write-Output 'ACCESS_DENIED' "
        "  } else { "
        '    Write-Output ("ERROR:" + $_.Exception.Message) '
        "  } "
        "  exit 1 "
        "}"
    )

    try:
        completed = _run(["powershell", "-NoProfile", "-Command", script], logger, 25)
        lines = (completed.stdout or "").strip().splitlines()
        last = lines[-1] if lines else ""
        if last.startswith("OK:"):
            logger.info("✓ Wi-Fi 网卡 '%s' 已重置（状态确认为 Up）", last[3:])
            time.sleep(5)
            return True
        if last == "NO_ADAPTER":
            logger.warning("未找到可重置的 Wi-Fi 网卡")
        elif last == "ACCESS_DENIED":
            logger.error(
                "⛔ 网卡重置被拒绝：当前无管理员权限。请以管理员身份运行，否则无法禁用/启用网卡。"
            )
        elif last.startswith(("DISABLE_FAILED", "ENABLE_FAILED")):
            logger.warning("网卡重置未达预期状态: %s", last)
        elif last.startswith("ERROR:"):
            logger.warning("网卡重置脚本错误: %s", last)
        else:
            logger.warning("网卡重置结果未知: %r (返回码: %s)", last, completed.returncode)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Wi-Fi 网卡重置失败: %s", exc)
    return False


# ------------------------------------------------------ single instance lock

# Handles must outlive the call, otherwise the mutex is released immediately.
_single_instance_handles: list = []
# Names already owned by this process: a second call must stay True (the same
# Windows mutex is not re-entrant across CreateMutexW calls).
_single_instance_names: set[str] = set()

_ERROR_ALREADY_EXISTS = 183


def acquire_single_instance(name: str) -> bool:
    """Take a per-session named lock; ``False`` if another process holds it.

    A named mutex is stronger than a PID file: the kernel releases it when the
    owning process dies, so a crash never leaves a stale lock that blocks the
    next start. Scope is the logon session (``Local\\``), which is what we want
    here — one guard per user session. Calling it twice with the same name in
    one process is idempotent and returns ``True``.
    """
    if name in _single_instance_names:
        return True
    if sys.platform != "win32":  # pragma: no cover - this app is Windows-only
        return True
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.restype = wintypes.HANDLE
    kernel32.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
    handle = kernel32.CreateMutexW(None, True, f"Local\\NetworkGuard.{name}")
    if not handle:
        return False
    if ctypes.get_last_error() == _ERROR_ALREADY_EXISTS:
        kernel32.CloseHandle(handle)
        return False
    _single_instance_handles.append(handle)
    _single_instance_names.add(name)
    return True


def release_single_instance() -> None:
    """Release every lock this process holds. Safe to call twice."""
    global _single_instance_handles
    _single_instance_names.clear()
    if sys.platform != "win32":  # pragma: no cover
        return
    import ctypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    while _single_instance_handles:
        handle = _single_instance_handles.pop()
        try:
            kernel32.CloseHandle(handle)
        except Exception:  # noqa: BLE001
            pass


# ------------------------------------------------------- PID and crash state


def write_pid_file(pid_file: Path) -> None:
    import os

    try:
        pid_file.parent.mkdir(parents=True, exist_ok=True)
        pid_file.write_text(str(os.getpid()), encoding="utf-8")
    except OSError as exc:
        print(f"警告: 无法写入 PID 文件 {pid_file}: {exc}", file=sys.stderr)


def remove_pid_file(pid_file: Path) -> None:
    try:
        if pid_file.exists():
            pid_file.unlink()
    except OSError:
        pass


def remove_owned_pid_file(pid_file: Path, pid: int) -> None:
    try:
        if pid_file.read_text(encoding="utf-8").strip() == str(pid):
            pid_file.unlink()
    except OSError:
        pass


@contextmanager
def monitor_session(logger, pid_file: Path, log_file: Path, keep_awake: bool):
    """Bookkeeping for GUI/background workers, including orderly cleanup."""
    import os
    import signal

    check_previous_crash(pid_file, logger, log_file)
    write_pid_file(pid_file)
    previous = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}
    reason = "正常退出"
    held = False
    try:
        def handle_signal(signum, frame):
            raise SystemExit(f"收到信号 {signum}")
        for sig in previous:
            signal.signal(sig, handle_signal)
        if keep_awake:
            held = prevent_sleep(logger)
        logger.info("守护会话启动，PID: %s", os.getpid())
        yield
    except BaseException as exc:
        reason = type(exc).__name__
        raise
    finally:
        if held:
            restore_sleep()
        remove_owned_pid_file(pid_file, os.getpid())
        for sig, handler in previous.items():
            signal.signal(sig, handler)
        logger.info("守护会话退出：%s", reason)


def check_previous_crash(pid_file: Path, logger, log_file: Path | None = None) -> bool:
    """Detect a stale PID file and replay the tail of the previous log."""
    if not pid_file.exists():
        return False
    try:
        old_pid = int(pid_file.read_text(encoding="utf-8").strip())
    except (ValueError, OSError):
        remove_pid_file(pid_file)
        return False

    try:
        SYNCHRONIZE = 0x00100000
        handle = ctypes.windll.kernel32.OpenProcess(SYNCHRONIZE, False, old_pid)
        if handle:
            ctypes.windll.kernel32.CloseHandle(handle)
            logger.warning("发现 PID %s 的旧进程仍在运行，将覆盖 PID 文件继续启动。", old_pid)
            return False
    except Exception:  # noqa: BLE001
        pass

    logger.warning("⚠ 检测到上次运行异常退出！旧 PID: %s（进程已不存在，PID 文件残留）", old_pid)
    if log_file is not None and log_file.exists():
        try:
            lines = log_file.read_text(encoding="utf-8", errors="ignore").strip().splitlines()
            tail = lines[-15:]
            logger.warning("--- 崩溃前最后 %d 行日志 ---", len(tail))
            for line in tail:
                logger.warning("  %s", line)
            logger.warning("--- 崩溃前日志结束 ---")
        except Exception:  # noqa: BLE001
            pass
    remove_pid_file(pid_file)
    return True


def register_exit_handler(logger, pid_file: Path, reason: Callable[[], str | None]) -> None:
    """Record why the process ended and always clear the PID file."""

    def _on_exit() -> None:
        try:
            logger.info("程序退出。原因: %s", reason() or "未知原因")
        except Exception:  # noqa: BLE001
            pass
        remove_pid_file(pid_file)

    atexit.register(_on_exit)


def install_signal_handlers(logger, pid_file: Path, set_reason: Callable[[str], None]) -> None:
    """SIGINT/SIGTERM become a logged, orderly exit on Windows consoles."""
    import signal

    def _handler(signum, _frame) -> None:
        set_reason(f"收到信号 {signum}")
        logger.warning("收到信号 %s，正在退出…", signum)
        remove_pid_file(pid_file)
        raise SystemExit(0)

    for name in ("SIGINT", "SIGTERM"):
        sig = getattr(signal, name, None)
        if sig is not None:
            try:
                signal.signal(sig, _handler)
            except (ValueError, OSError):  # not main thread / unsupported
                pass


# ------------------------------------------------------------- log bundling

# Where users should send a bundle when they ask for help.
SUPPORT_EMAIL = "1164275580@qq.com"


def collect_log_files(log_file: Path, max_files: int = 20) -> list[Path]:
    """Current log plus rotated backups (``x.log``, ``x.log.1`` ...), newest first.

    Only files whose suffix after the log name is numeric are treated as
    backups, so an unrelated ``x.log.bak`` is ignored.
    """
    directory = log_file.parent
    if not directory.is_dir():
        return []
    stem = log_file.name
    found: list[Path] = []
    for item in directory.iterdir():
        if not item.is_file():
            continue
        if item.name == stem or (
            item.name.startswith(stem + ".") and item.name[len(stem) + 1:].isdigit()
        ):
            found.append(item)
    found.sort(key=lambda path: path.stat().st_mtime, reverse=True)
    return found[:max_files]


def export_logs(log_file: Path, version: str = "", destination: Path | None = None,
                max_files: int = 20) -> Path | None:
    """Bundle recent logs plus a diagnostics sheet into one ZIP.

    Log lines are already redacted when written (SSID / BSSID / MAC / GUID
    become placeholders), so the archive is safe to hand to support. The
    configuration file is deliberately excluded: it holds personal Wi-Fi
    profile names and internal addresses.

    Returns the archive path, or ``None`` when no log file exists yet.
    """
    import platform
    import zipfile
    from datetime import datetime

    files = collect_log_files(log_file, max_files)
    if not files:
        return None

    target_dir = destination or log_file.parent
    target_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    archive = target_dir / f"NetworkGuard-logs-{stamp}.zip"
    # 同一秒内连续打包时追加序号，避免静默覆盖上一次的结果。
    sequence = 2
    while archive.exists():
        archive = target_dir / f"NetworkGuard-logs-{stamp}-{sequence}.zip"
        sequence += 1

    total_kb = sum(item.stat().st_size for item in files) / 1024
    note = [
        "# Network Guard 日志包",
        "",
        f"生成时间: {datetime.now():%Y-%m-%d %H:%M:%S}",
        f"版本: {version or '未知'}",
        f"操作系统: {platform.system()} {platform.release()}",
        f"Python: {sys.version.split()[0]}",
        f"日志文件数: {len(files)}（合计 {total_kb:.0f} KB，新的在前）",
        "",
        "包含的日志:",
        *(
            f"  - {item.name}  ({item.stat().st_size / 1024:.0f} KB)"
            for item in files
        ),
        "",
        "日志已自动脱敏：SSID、BSSID、网卡物理地址与 GUID 已替换为占位符。",
        "配置文件未打包（可能含个人 Wi-Fi 名称）。",
        "",
        "常见线索:",
        "  - 出现“错误 1220” → WLAN AutoConfig 服务(wlansvc)异常或网卡正在重置",
        "  - 出现“未检测到有线网卡”或 IP 为 169.254.x.x → DHCP 未下发地址",
        "  - 出现“已改用 PowerShell 识别到” → netsh 查询挂起，已自动回退",
        "  - 出现“已有另一个守护实例” → 重复启动被单实例锁拦下",
        "",
        f"反馈问题时请描述现象，并把本压缩包发送至 {SUPPORT_EMAIL}。",
    ]

    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as bundle:
        bundle.writestr("README.txt", "\n".join(note) + "\n")
        for item in files:
            bundle.write(item, arcname=item.name)
    return archive

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

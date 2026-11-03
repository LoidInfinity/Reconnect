from __future__ import annotations

import argparse
import json
import logging
from logging.handlers import RotatingFileHandler
import os
import platform
import re
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

import repairs
from app_info import DATA_DIR


APP_NAME = "network_guard"
DEFAULT_CONFIG_FILE = DATA_DIR / "network_guard_config.json"
DEFAULT_LOG_FILE = DATA_DIR / "network_guard.log"
DEFAULT_STATE_FILE = DATA_DIR / "network_guard_state.txt"
DEFAULT_PID_FILE = DATA_DIR / "network_guard.pid"
DEFAULT_CHECK_TARGETS = (
	"https://www.todesk.com",
	"https://sunlogin.oray.com",
	"https://www.awesun.com",
)
# Domains used to tell a DNS fault apart from a plain link outage. Neutral
# public hosts only: no personal or workplace domain belongs in the source.
DEFAULT_DNS_HOSTS = ("www.todesk.com", "sunlogin.oray.com", "www.awesun.com")
LEGACY_CHECK_TARGETS = ("https://www.msftconnecttest.com/connecttest.txt", "https://www.baidu.com", "https://www.cloudflare.com")
INTERIM_CHECK_TARGETS = ("https://connect.rom.miui.com/generate_204", "https://connectivitycheck.platform.hicloud.com/generate_204")


def migrate_config(data: dict[str, Any]) -> dict[str, Any]:
	"""Upgrade known defaults and legacy keys without replacing custom targets."""
	data = dict(data)
	if data.get("check_targets") in (list(LEGACY_CHECK_TARGETS), list(INTERIM_CHECK_TARGETS)):
		data["check_targets"] = list(DEFAULT_CHECK_TARGETS)
	if data.get("dns_check_hosts") in (["www.baidu.com", "www.cloudflare.com"], ["connect.rom.miui.com", "connectivitycheck.platform.hicloud.com"]):
		data["dns_check_hosts"] = list(DEFAULT_DNS_HOSTS)
	if "enabled_profiles" not in data and data.get("allowed_profiles"):
		data["enabled_profiles"] = data["allowed_profiles"]
	return data

_MAC_RE = re.compile(r"\b(?:[0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}\b")
_GUID_RE = re.compile(r"\b[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}\b")
_NETWORK_FIELD_RE = re.compile(
	r"(?i)\b(SSID|AP BSSID|BSSID|Profile|配置文件|物理地址)\s*[:：]\s*[^|\n]*"
)
_DATACLASS_FIELD_RE = re.compile(r"(?i)\b(ssid|bssid|profile)='[^']*'")


def redact(text: str) -> str:
	"""Mask device identifiers before they reach a log line.

	``netsh wlan show interfaces`` prints the SSID, the AP BSSID and the
	adapter MAC. Those identify a person's location and hardware, so they are
	scrubbed from disk logs while the GUI still shows them in its own widgets.
	"""
	if not text:
		return ""
	text = _GUID_RE.sub("<guid>", text)
	text = _MAC_RE.sub("<mac>", text)
	text = _NETWORK_FIELD_RE.sub(lambda match: f"{match.group(1)}: <redacted>", text)
	text = _DATACLASS_FIELD_RE.sub(lambda match: f"{match.group(1)}='<redacted>'", text)
	return text


@dataclass(frozen=True)
class WifiInterface:
	name: str
	state: str
	ssid: str | None = None
	bssid: str | None = None
	signal: str | None = None
	profile: str | None = None


def load_config(config_file: Path) -> dict[str, Any]:
	if not config_file.exists():
		return {}
	try:
		with open(config_file, "r", encoding="utf-8") as f:
			return migrate_config(json.load(f))
	except (json.JSONDecodeError, OSError) as exc:
		print(f"警告: 无法读取配置文件 {config_file}: {exc}", file=sys.stderr)
		return {}


def save_default_config(config_file: Path) -> None:
	default_config = {
		"interval": 10,
		"reconnect_timeout": 35,
		"check_targets": list(DEFAULT_CHECK_TARGETS),
		"preferred_profiles": [],
		"disabled_profiles": [],
		"interface": None,
		"log_file": DEFAULT_LOG_FILE.name,
		"log_max_bytes": 2 * 1024 * 1024,
		"log_backup_count": 5,
		"state_file": DEFAULT_STATE_FILE.name,
		"pid_file": DEFAULT_PID_FILE.name,
		"heartbeat_interval": 60,
		"prevent_sleep": True,
		"cleanup_proxy_on_disconnect": True,
		"repair_dns_on_failure": True,
		"dns_check_hosts": list(DEFAULT_DNS_HOSTS),
		"wifi_recovery": True,
		"adapter_reset_on_failure": True,
		"verbose": False,
	}
	try:
		with open(config_file, "w", encoding="utf-8") as f:
			json.dump(default_config, f, indent=2, ensure_ascii=False)
		print(f"已生成默认配置文件: {config_file}")
	except OSError as exc:
		print(f"警告: 无法写入配置文件 {config_file}: {exc}", file=sys.stderr)


def build_arg_parser() -> argparse.ArgumentParser:
	parser = argparse.ArgumentParser(
		description="检测网络中断并自动连接到已保存密码的 Wi-Fi。",
	)
	parser.add_argument(
		"--config",
		default=str(DEFAULT_CONFIG_FILE),
		help="配置文件路径，默认为当前目录的 network_guard_config.json。",
	)
	parser.add_argument(
		"--save-default-config",
		action="store_true",
		help="生成默认配置文件并退出。",
	)
	parser.add_argument(
		"--interval",
		type=int,
		default=None,
		help="检测间隔，单位秒（命令行参数会覆盖配置文件）。",
	)
	parser.add_argument(
		"--reconnect-timeout",
		type=int,
		default=None,
		help="单次自动重连的最长等待时间，单位秒。",
	)
	parser.add_argument(
		"--check-target",
		action="append",
		default=[],
		help="自定义联网检测目标，可重复传入。",
	)
	parser.add_argument(
		"--preferred-profile",
		action="append",
		default=[],
		help="优先尝试连接的 Wi-Fi 配置名称，可重复传入。",
	)
	parser.add_argument(
		"--interface",
		default=None,
		help="指定 Wi-Fi 网卡名称，不填则自动识别。",
	)
	parser.add_argument(
		"--log-file",
		default=None,
		help="日志文件路径。",
	)
	parser.add_argument(
		"--log-max-bytes",
		type=int,
		default=None,
		help="日志文件单个大小上限（字节）。",
	)
	parser.add_argument(
		"--log-backup-count",
		type=int,
		default=None,
		help="日志备份文件数量。",
	)
	parser.add_argument(
		"--state-file",
		default=None,
		help="记录最近一次成功连接的 Wi-Fi 配置名称。",
	)
	parser.add_argument(
		"--pid-file",
		default=None,
		help="PID 文件路径，用于崩溃检测。",
	)
	parser.add_argument(
		"--heartbeat-interval",
		type=int,
		default=None,
		help="心跳日志间隔，单位秒，默认 60。",
	)
	parser.add_argument(
		"--no-prevent-sleep",
		action="store_true",
		help="不阻止系统休眠（默认阻止，以保持守护持续运行）。",
	)
	parser.add_argument(
		"--no-cleanup-proxy",
		action="store_true",
		help="断网时不清理系统代理（默认会关闭系统代理，修复代理工具失效导致的断网）。",
	)
	parser.add_argument(
		"--no-repair-dns",
		action="store_true",
		help="DNS 解析故障时不自动修复（默认清空缓存并重置网卡 DNS）。",
	)
	parser.add_argument(
		"--no-reset-adapter",
		action="store_true",
		help="全部配置失败后不重置 Wi-Fi 网卡（默认会尝试禁用/启用网卡）。",
	)
	parser.add_argument(
		"--no-wifi-recovery",
		action="store_true",
		help="只做代理/DNS 修复，不重连 Wi-Fi（有线环境建议加此参数）。",
	)
	parser.add_argument("--allowed-profile", action="append", default=[], help="兼容旧版：仅允许指定 Wi-Fi 配置参与恢复，可重复。")
	parser.add_argument("--wifi-recovery", action="store_true", help="兼容旧版：启用 Wi-Fi 恢复。")
	parser.add_argument(
		"--verbose",
		action="store_true",
		help="同时在控制台输出调试日志。",
	)
	return parser


def configure_logging(log_file: Path, verbose: bool, max_bytes: int = 2 * 1024 * 1024, backup_count: int = 5) -> logging.Logger:
	logger = logging.getLogger(APP_NAME)
	logger.setLevel(logging.DEBUG)
	logger.handlers.clear()

	formatter = logging.Formatter("%(asctime)s | %(levelname)s | %(message)s")

	file_handler = RotatingFileHandler(
		log_file,
		maxBytes=max_bytes,
		backupCount=backup_count,
		encoding="utf-8",
	)
	file_handler.setLevel(logging.DEBUG)
	file_handler.setFormatter(formatter)

	console_handler = logging.StreamHandler(sys.stdout)
	console_handler.setLevel(logging.DEBUG if verbose else logging.INFO)
	console_handler.setFormatter(formatter)

	logger.addHandler(file_handler)
	logger.addHandler(console_handler)
	logger.propagate = False
	return logger


def ensure_windows(logger: logging.Logger) -> None:
	if platform.system().lower() != "windows":
		logger.error("当前程序仅支持 Windows，因为需要使用 netsh 管理 Wi-Fi 连接。")
		raise SystemExit(1)


def run_command(command: list[str], logger: logging.Logger, timeout: int | None = 30) -> subprocess.CompletedProcess[str]:
	"""Run a command but never let a wedged child freeze the monitor loop.

	``netsh`` can block inside the WLAN driver, where ``kill()`` does not
	return and ``subprocess.run(timeout=)`` would hang forever. The wait is
	therefore bounded by a daemon thread: if it is still alive we abandon it
	and report a timeout, because a daemon thread cannot keep the process up.
	"""
	logger.debug("执行命令: %s (超时: %s 秒)", " ".join(command), timeout or "无限")
	if timeout is None:
		return _execute(command, logger, None)

	bucket: list[subprocess.CompletedProcess[str]] = []
	failure: list[BaseException] = []

	def _target() -> None:
		try:
			bucket.append(_execute(command, logger, timeout))
		except BaseException as exc:  # noqa: BLE001
			failure.append(exc)

	thread = threading.Thread(target=_target, daemon=True)
	started = time.time()
	thread.start()
	thread.join(timeout)
	repairs.detect_sleep(logger, timeout, time.time() - started)

	if thread.is_alive():
		logger.warning("⚠ 命令超时 (%s 秒) 且子进程无响应，已放弃等待: %s", timeout, " ".join(command))
		return subprocess.CompletedProcess(
			args=command,
			returncode=-1,
			stdout="",
			stderr=f"Command timeout after {timeout} seconds",
		)
	if failure:
		raise failure[0]
	if bucket:
		return bucket[0]
	return subprocess.CompletedProcess(args=command, returncode=-1, stdout="", stderr="")


def _execute(command: list[str], logger: logging.Logger, timeout: int | None) -> subprocess.CompletedProcess[str]:
	try:
		completed = subprocess.run(
			command,
			capture_output=True,
			shell=False,
			timeout=timeout,
			creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
		)
	except subprocess.TimeoutExpired:
		logger.warning("命令执行超时 (%s 秒): %s", timeout, " ".join(command))
		return subprocess.CompletedProcess(
			args=command,
			returncode=-1,
			stdout="",
			stderr=f"Command timeout after {timeout} seconds",
		)
	
	stdout_text = decode_output(completed.stdout, logger)
	stderr_text = decode_output(completed.stderr, logger)
	
	result = subprocess.CompletedProcess(
		args=completed.args,
		returncode=completed.returncode,
		stdout=stdout_text,
		stderr=stderr_text,
	)
	
	stdout = (stdout_text or "").strip()
	stderr = (stderr_text or "").strip()
	if stdout:
		logger.debug("命令输出: %s", redact(stdout.replace("\r", " ").replace("\n", " | ")))
	if stderr:
		logger.debug("命令错误输出: %s", redact(stderr.replace("\r", " ").replace("\n", " | ")))
	logger.debug("命令返回码: %s", completed.returncode)
	return result


def decode_output(data: bytes | None, logger: logging.Logger) -> str:
	if not data:
		return ""
	encodings = ["gbk", "gb2312", "utf-16", "utf-8", "latin1"]
	for enc in encodings:
		try:
			text = data.decode(enc)
			if text.strip():
				logger.debug("成功用编码 %s 解码输出", enc)
				return text
		except (UnicodeDecodeError, LookupError):
			continue
	logger.debug("尝试所有编码都失败，回退到 utf-8 并忽略错误")
	return data.decode("utf-8", errors="ignore")


def read_state_file(state_file: Path) -> str | None:
	if not state_file.exists():
		return None
	try:
		value = state_file.read_text(encoding="utf-8").strip()
	except OSError:
		return None
	return value or None


def write_state_file(state_file: Path, profile_name: str, logger: logging.Logger) -> None:
	try:
		state_file.write_text(profile_name.strip(), encoding="utf-8")
	except OSError as exc:
		logger.debug("写入状态文件失败: %s", exc)


def resolve_path(path_str: str) -> Path:
	"""相对路径一律基于程序目录。

	开机自启时工作目录可能是 System32，按 cwd 解析会把日志和状态文件
	写到系统目录里，下次又读不到，看起来就像"配置没生效"。
	"""
	path = Path(path_str)
	if not path.is_absolute():
		path = DATA_DIR / path
	return path


def parse_wifi_interfaces(output: str) -> list[WifiInterface]:
	blocks = re.split(r"\n\s*\n", output.strip())
	interfaces: list[WifiInterface] = []
	for block in blocks:
		if not re.search(r"\bState\b|\b状态\b", block, re.IGNORECASE):
			continue
		name = extract_value(block, ["Name", "名称"])
		state = extract_value(block, ["State", "状态"]) or "Unknown"
		ssid = extract_value(block, ["SSID"])
		bssid = extract_value(block, ["BSSID"])
		signal = extract_value(block, ["Signal", "信号"])
		if name:
			profile = extract_value(block, ["Profile", "配置文件"])
			interfaces.append(WifiInterface(name=name, state=state, ssid=ssid, bssid=bssid, signal=signal, profile=profile))
	return interfaces


def extract_value(text: str, labels: Iterable[str]) -> str | None:
	for label in labels:
		pattern = rf"^\s*{re.escape(label)}\s*[:：]\s*(.+)"
		match = re.search(pattern, text, re.IGNORECASE | re.MULTILINE)
		if match:
			value = match.group(1).strip()
			if value:
				return value
	return None


def get_wifi_interfaces(logger: logging.Logger, interface_name: str | None) -> list[WifiInterface]:
	completed = run_command(["netsh", "wlan", "show", "interfaces"], logger)
	if completed.returncode != 0:
		return []
	interfaces = parse_wifi_interfaces(completed.stdout or "")
	if interface_name:
		interfaces = [item for item in interfaces if item.name.casefold() == interface_name.casefold()]
	logger.debug("识别到 Wi-Fi 网卡: %s", interfaces)
	return interfaces


def get_saved_profiles(logger: logging.Logger) -> list[str]:
	completed = run_command(["netsh", "wlan", "show", "profiles"], logger)
	if completed.returncode != 0:
		return []
	profiles: list[str] = []
	text = completed.stdout or ""
	for line in text.splitlines():
		if "All User Profile" in line or "所有用户配置文件" in line:
			match = re.search(r"[:：]\s*(.+)$", line)
			if match:
				profile = match.group(1).strip()
				if profile:
					profiles.append(profile)
	deduped: list[str] = []
	seen: set[str] = set()
	for profile in profiles:
		key = profile.casefold()
		if key in seen:
			continue
		seen.add(key)
		deduped.append(profile)
	logger.debug("已保存的 Wi-Fi 配置: %s", deduped)
	return deduped


def ping_reachable(logger: logging.Logger, timeout_seconds: int = 2) -> bool:
	"""链路层是否通：ping 得通公共地址就说明链路（Wi-Fi/以太网）没断。"""
	for host in ("223.5.5.5", "119.29.29.29"):
		try:
			timeout_ms = max(1, timeout_seconds * 1000)
			completed = run_command(
				["ping", "-n", "1", "-w", str(timeout_ms), host], logger, timeout_seconds + 5
			)
			if completed.returncode == 0:
				logger.debug("Ping 检查成功: %s", host)
				return True
		except Exception as exc:  # noqa: BLE001
			logger.debug("Ping 检查失败: %s, %s", host, exc)
	return False


def http_reachable(logger: logging.Logger, targets: Iterable[str], timeout_seconds: int = 4) -> bool:
	"""应用层是否通。每次新建 opener，避免缓存住旧的系统代理设置。"""
	import urllib.request

	for target in targets:
		try:
			opener = urllib.request.build_opener()
			with opener.open(target, timeout=timeout_seconds) as response:
				# Dedicated 204 endpoints must not accept a portal/login page as online.
				if target.rstrip("/").endswith("/generate_204"):
					if response.status == 204 and response.geturl() == target:
						return True
					logger.debug("联网探测未返回原地址的 204：%s", target)
					continue
				if 200 <= getattr(response, "status", 200) < 400:
					logger.debug("HTTP 检测成功: %s", target)
					return True
		except Exception as exc:  # noqa: BLE001
			logger.debug("HTTP 检测失败: %s, %s", target, exc)
	return False


def is_internet_reachable(logger: logging.Logger, targets: Iterable[str], timeout_seconds: int = 4) -> bool:
	"""以 HTTP(S) 结果为准，而不是 ping。

	代理工具失效时 ping 依然通、只有应用层断，用 ping 判定会漏报。因此
	ping 只用来区分"链路断了"和"链路正常但应用层坏了"（``ping_reachable``），
	判定是否在线一律看 HTTP。未配置检测地址时使用默认 HTTP 目标。
	"""
	targets = list(targets) or list(DEFAULT_CHECK_TARGETS)
	return http_reachable(logger, targets, timeout_seconds)


def connect_wifi(profile_name: str, logger: logging.Logger, interface_name: str | None) -> bool:
	command = ["netsh", "wlan", "connect", f"name={profile_name}"]
	if interface_name:
		command.append(f"interface={interface_name}")
	logger.debug("发起 Wi-Fi 连接请求: %s", profile_name)
	completed = run_command(command, logger, timeout=15)
	if completed.returncode != 0:
		logger.warning("尝试连接 Wi-Fi 配置失败: %s (返回码: %s)", profile_name, completed.returncode)
		return False
	logger.info("已发起连接请求: %s", profile_name)
	return True


def choose_profiles_to_try(
	preferred_profiles: list[str],
	last_successful: str | None,
	saved_profiles: list[str],
	disabled_profiles: list[str] | None = None,
	enabled_profiles: list[str] | None = None,
) -> list[str]:
	disabled = {p.casefold() for p in (disabled_profiles or [])}
	saved_profiles = [p for p in saved_profiles if p.casefold() not in disabled]
	if enabled_profiles is not None:
		enabled = {p.casefold() for p in enabled_profiles}
		saved_profiles = [p for p in saved_profiles if p.casefold() in enabled]
	saved_lower = {p.casefold(): p for p in saved_profiles}
	ordered: list[str] = []
	
	if last_successful:
		key = last_successful.casefold()
		if key in saved_lower:
			ordered.append(saved_lower[key])
	
	for candidate in preferred_profiles:
		if not candidate:
			continue
		key = candidate.casefold()
		if key not in saved_lower:
			continue
		if any(p.casefold() == key for p in ordered):
			continue
		ordered.append(saved_lower[key])
	
	for profile in saved_profiles:
		if any(profile.casefold() == existing.casefold() for existing in ordered):
			continue
		ordered.append(profile)
	
	return ordered


def recover_network(
	logger: logging.Logger,
	interface_name: str | None,
	preferred_profiles: list[str],
	state_file: Path,
	reconnect_timeout: int,
	check_targets: list[str],
	disabled_profiles: list[str] | None = None,
	enabled_profiles: list[str] | None = None,
	cleanup_proxy: bool = False,
	repair_dns_enabled: bool = False,
	dns_check_hosts: Iterable[str] | None = None,
	wifi_recovery: bool = True,
	adapter_reset: bool = False,
	dns_reset_interfaces: Iterable[str] | None = None,
	repair_stats: dict[str, int] | None = None,
) -> bool:
	"""按"DNS → 系统代理 → Wi-Fi 重连 → 网卡重置"的顺序修复，从轻到重。

	ping 成功只说明 ICMP 可达，不能证明 HTTP 或远控流量正常。
	应用层修复后 HTTP 仍失败时，继续按参与恢复名单尝试 Wi-Fi 重连。
	"""
	repaired = False

	# 1) DNS：域名解析失败说明 DNS 被破坏，而不是链路断
	if repair_dns_enabled:
		hosts = list(dns_check_hosts or ())
		failed = repairs.check_dns_resolution(logger, hosts) if hosts else []
		if failed:
			repaired = True
			logger.warning("检测到 DNS 解析故障（%s 无法解析），尝试自动修复。", ", ".join(failed))
			if repairs.repair_dns(logger, list(dns_reset_interfaces or []) or repairs.get_up_interface_names(logger)):
				remaining = repairs.check_dns_resolution(logger, hosts)
				if remaining:
					logger.warning("DNS 修复后仍有域名解析失败: %s", ", ".join(remaining))
				else:
					logger.info("✓ DNS 修复后解析恢复正常。")
					if repair_stats is not None:
						repair_stats["dns_repairs"] = repair_stats.get("dns_repairs", 0) + 1
					if is_internet_reachable(logger, check_targets):
						logger.info("✓ DNS 修复后网络已恢复，无需继续清理代理。")
						return True
		else:
			logger.debug("DNS 解析正常，跳过修复。")

	# 2) 系统代理：代理工具退出后 Windows 仍指向失效端口
	if cleanup_proxy:
		repaired = True
		if repairs.clear_proxy_settings(logger):
			time.sleep(2)
			if is_internet_reachable(logger, check_targets):
				if repair_stats is not None:
					repair_stats["proxy_cleanups"] = repair_stats.get("proxy_cleanups", 0) + 1
				logger.info("✓ 清理系统代理后网络已恢复。")
				return True

	# 应用层修完再判定一次，避免白连一次 Wi-Fi
	if repaired:
		if is_internet_reachable(logger, check_targets):
			logger.info("✓ 应用层修复后网络已恢复。")
			return True

	if not wifi_recovery:
		logger.info("Wi-Fi 重连恢复已禁用，跳过。")
		return False
	if ping_reachable(logger):
		logger.warning("Ping 可达但 HTTP 检测仍失败，继续执行允许的 Wi-Fi 恢复，不将 ping 成功视为网络恢复。")

	interfaces = get_wifi_interfaces(logger, interface_name)
	if interfaces:
		logger.info(
			"当前 Wi-Fi 状态: %s",
			redact("; ".join(f"{item.name}={item.state}({item.ssid or 'no-ssid'})" for item in interfaces)),
		)
	else:
		logger.warning("未识别到可用的 Wi-Fi 网卡（netsh 可能已超时或网卡卡死）。")

	saved_profiles = get_saved_profiles(logger)
	last_successful = read_state_file(state_file)
	candidates = choose_profiles_to_try(preferred_profiles, last_successful, saved_profiles, disabled_profiles, enabled_profiles)

	if not candidates:
		logger.warning("没有可参与恢复的 Wi-Fi 配置（未保存配置或已全部禁用）。")
		# Resetting an adapter can auto-join an excluded profile. Respect the
		# participation switches even when profile discovery fails.
		if adapter_reset and enabled_profiles is None and not disabled_profiles:
			logger.info("无可用配置，尝试硬件重置 Wi-Fi 网卡…")
			if repairs.reset_wifi_adapter(logger):
				time.sleep(10)
				return is_internet_reachable(logger, check_targets)
		return False

	logger.info("开始自动恢复，共有 %d 个候选配置，限定时间 %d 秒", len(candidates), reconnect_timeout)
	logger.info("恢复优先级顺序: %s", " → ".join(candidates))
	deadline = time.time() + reconnect_timeout

	for idx, profile_name in enumerate(candidates, 1):
		if time.time() > deadline:
			logger.warning("自动恢复已超过限定时间，停止进一步尝试。")
			return False

		remaining_time = int(deadline - time.time())
		logger.info("[%d/%d] 尝试连接 Wi-Fi 配置: %s (剩余时间: %d 秒)", idx, len(candidates), profile_name, remaining_time)
		
		if not connect_wifi(profile_name, logger, interface_name):
			logger.debug("配置 %s 连接请求发送失败，跳过", profile_name)
			continue

		attempt_deadline = min(deadline, time.time() + 20)
		check_interval = 3
		attempts = 0
		
		logger.debug("等待配置 %s 的网络恢复（最多 20 秒）...", profile_name)
		while time.time() < attempt_deadline:
			attempts += 1
			if is_internet_reachable(logger, check_targets):
				elapsed_time = int(time.time() - (deadline - reconnect_timeout))
				logger.info("✓ 网络已恢复！配置: %s，总耗时: %d 秒，尝试次数: %d", profile_name, elapsed_time, attempts)
				write_state_file(state_file, profile_name, logger)
				return True
			
			remaining = int(attempt_deadline - time.time())
			if remaining > 0:
				logger.debug("  等待中... (已尝试 %d 次，剩余 %d 秒)", attempts, remaining)
			
			time.sleep(check_interval)

		logger.warning("配置 %s 已发起连接，但在 20 秒内仍未恢复联网 (尝试 %d 次)", profile_name, attempts)

	logger.error("✗ 已尝试全部 %d 个候选 Wi-Fi 配置，网络仍未恢复。", len(candidates))

	# 3) 最后手段：禁用再启用网卡，突破驱动层挂起（需管理员权限）
	if adapter_reset and (enabled_profiles is not None or disabled_profiles):
		logger.info("为遵守 Wi-Fi 参与恢复限制，跳过可能自动连接其他配置的网卡重置。")
	if adapter_reset and enabled_profiles is None and not disabled_profiles:
		logger.info("所有候选配置均失败，尝试硬件重置 Wi-Fi 网卡…")
		if repairs.reset_wifi_adapter(logger):
			time.sleep(10)
			if is_internet_reachable(logger, check_targets):
				logger.info("✓ 网卡重置后网络已恢复。")
				return True
			logger.warning("网卡已重置但网络仍未恢复。")
	return False


def output_startup_diagnostics(args: argparse.Namespace, logger: logging.Logger) -> None:
	logger.info("========== 程序启动诊断信息 ==========")
	
	logger.info("Python 版本: %s", sys.version.split()[0])
	logger.info("操作系统: %s %s", platform.system(), platform.release())
	logger.info("处理器: %s", platform.processor() or "Unknown")
	
	logger.info("命令行参数:")
	logger.info("  - 轮询间隔: %s 秒", args.interval)
	logger.info("  - 重连超时: %s 秒", args.reconnect_timeout)
	logger.info("  - 指定网卡: %s", args.interface or "自动识别")
	if args.preferred_profile:
		logger.info("  - 优先 Wi-Fi 配置: %s", ", ".join(args.preferred_profile))
	if args.check_target:
		logger.info("  - 自定义检测目标: %s", ", ".join(args.check_target))
	
	logger.info("当前网卡状态:")
	interfaces = get_wifi_interfaces(logger, args.interface)
	if interfaces:
		for iface in interfaces:
			logger.info(
				"  - %s: %s (SSID: %s, 信号: %s)",
				iface.name,
				iface.state,
				"<redacted>" if iface.ssid else "未连接",
				iface.signal or "N/A",
			)
	else:
		logger.info("  - 未识别到 Wi-Fi 网卡或网卡不可用")
	
	logger.info("已保存的 Wi-Fi 配置:")
	profiles = get_saved_profiles(logger)
	if profiles:
		for i, profile in enumerate(profiles, 1):
			logger.info("  %d. %s", i, profile)
	else:
		logger.warning("  - 未找到任何已保存的 Wi-Fi 配置")
	
	logger.info("恢复策略:")
	logger.info("  - 系统代理清理: %s", "启用（断网时关闭系统代理）" if args.cleanup_proxy else "禁用")
	logger.info("  - DNS 故障修复: %s", "启用（清空缓存 + 网卡 DNS 回退 DHCP）" if args.repair_dns else "禁用")
	logger.info("  - Wi-Fi 重连恢复: %s", "启用" if args.wifi_recovery else "禁用（有线环境）")
	logger.info("  - 失败后重置网卡: %s", "启用" if args.adapter_reset else "禁用")
	logger.info("  - 阻止系统休眠: %s", "启用" if args.prevent_sleep else "禁用")
	logger.info("  - 心跳间隔: %s 秒", args.heartbeat_interval)
	logger.info("  - DNS 监测域名: %s", ", ".join(args.dns_check_hosts or []) or "(未配置)")

	logger.info("========== 诊断完成，监控循环启动 ==========")


def monitor_loop(args: argparse.Namespace, logger: logging.Logger) -> None:
	state_file = Path(args.state_file)
	pid_file = Path(args.pid_file)
	check_targets = list(dict.fromkeys(args.check_target or list(DEFAULT_CHECK_TARGETS)))
	dns_check_hosts = list(dict.fromkeys(args.dns_check_hosts or []))
	logger.info("程序启动，PID: %s，轮询间隔=%s 秒，重连超时=%s 秒", os.getpid(), args.interval, args.reconnect_timeout)
	logger.info("联网检测目标: %s", check_targets)
	logger.info("DNS 解析监测域名: %s", dns_check_hosts or "(未配置)")

	# PID 文件让下一次启动能判断"上次是不是崩了"，退出原因写进日志
	repairs.write_pid_file(pid_file)
	reason: dict[str, str | None] = {"value": None}
	repairs.install_signal_handlers(logger, pid_file, lambda text: reason.update(value=text))
	repairs.register_exit_handler(logger, pid_file, lambda: reason["value"])

	last_online = None
	recovery_stats = {"attempts": 0, "successes": 0, "failures": 0, "dns_repairs": 0, "proxy_cleanups": 0}
	last_heartbeat = time.time()

	while True:
		try:
			# 心跳：长时间无输出时，才能区分"没事发生"和"程序卡死"
			now = time.time()
			if now - last_heartbeat >= max(1, args.heartbeat_interval):
				logger.info(
					"💓 心跳: 运行中，统计: %d/%d 次恢复成功",
					recovery_stats["successes"],
					recovery_stats["attempts"],
				)
				for handler in logger.handlers:
					handler.flush()
				last_heartbeat = now

			online = is_internet_reachable(logger, check_targets)
			if online:
				if last_online is not True:
					logger.info("网络状态恢复为正常。")
				last_online = True
			else:
				if last_online is not False:
					logger.warning("检测到网络不可用，准备自动修复。")
				last_online = False
				recovery_stats["attempts"] += 1
				
				recovered = recover_network(
					logger=logger,
					interface_name=args.interface,
					preferred_profiles=args.preferred_profile,
					state_file=state_file,
					reconnect_timeout=args.reconnect_timeout,
					check_targets=check_targets,
					disabled_profiles=getattr(args, "disabled_profiles", []),
					enabled_profiles=getattr(args, "enabled_profiles", None),
					cleanup_proxy=args.cleanup_proxy,
					repair_dns_enabled=args.repair_dns,
					dns_check_hosts=dns_check_hosts,
					wifi_recovery=args.wifi_recovery,
					adapter_reset=args.adapter_reset,
					dns_reset_interfaces=getattr(args, "dns_reset_interfaces", []),
					repair_stats=recovery_stats,
				)
				
				if recovered:
					recovery_stats["successes"] += 1
					success_rate = 100 * recovery_stats["successes"] / recovery_stats["attempts"]
					logger.info("恢复成功率统计: %d/%d (%.1f%%)", recovery_stats["successes"], recovery_stats["attempts"], success_rate)
					logger.info("分项统计：DNS 修复 %d 次，代理清理后恢复 %d 次", recovery_stats["dns_repairs"], recovery_stats["proxy_cleanups"])
				else:
					recovery_stats["failures"] += 1
					failure_rate = 100 * recovery_stats["failures"] / recovery_stats["attempts"]
					logger.warning("恢复失败率统计: %d/%d (%.1f%%)", recovery_stats["failures"], recovery_stats["attempts"], failure_rate)
		except KeyboardInterrupt:
			reason["value"] = "用户按下 Ctrl+C"
			if recovery_stats["attempts"] > 0:
				success_rate = 100 * recovery_stats["successes"] / recovery_stats["attempts"]
				logger.info("程序退出前的恢复统计: 成功 %d/%d (%.1f%%)", recovery_stats["successes"], recovery_stats["attempts"], success_rate)
			logger.info("收到中断信号，程序退出。")
			repairs.remove_pid_file(pid_file)
			return
		except SystemExit:
			raise
		except Exception as exc:  # noqa: BLE001
			reason["value"] = f"未捕获异常: {exc}"
			logger.exception("监控循环出现异常: %s", exc)
			logger.info("将在 10 秒后继续运行…")
			time.sleep(10)
			continue

		sleep_seconds = max(1, args.interval)
		before = time.time()
		time.sleep(sleep_seconds)
		if repairs.detect_sleep(logger, sleep_seconds, time.time() - before):
			# 休眠后网络状态不可信，立刻重新判断
			logger.info("系统休眠恢复，重置网络状态缓存并立即重新检测…")
			last_online = None


def main() -> None:
	parser = build_arg_parser()
	args = parser.parse_args()
	
	if args.save_default_config:
		save_default_config(Path(args.config))
		return

	config_file = Path(args.config)
	config_data = load_config(config_file)
	
	if not config_file.exists() and not config_data:
		print(f"配置文件不存在: {config_file}", file=sys.stderr)
		print(f"请运行以下命令生成默认配置文件:", file=sys.stderr)
		print(f"  python main.py --save-default-config", file=sys.stderr)
		sys.exit(1)
	
	interval = args.interval if args.interval is not None else config_data.get("interval", 10)
	reconnect_timeout = args.reconnect_timeout if args.reconnect_timeout is not None else config_data.get("reconnect_timeout", 35)
	log_file_str = args.log_file or config_data.get("log_file", DEFAULT_LOG_FILE.name)
	log_max_bytes = args.log_max_bytes if args.log_max_bytes is not None else config_data.get("log_max_bytes", 2 * 1024 * 1024)
	log_backup_count = args.log_backup_count if args.log_backup_count is not None else config_data.get("log_backup_count", 5)
	state_file_str = args.state_file or config_data.get("state_file", DEFAULT_STATE_FILE.name)
	pid_file_str = args.pid_file or config_data.get("pid_file", DEFAULT_PID_FILE.name)
	heartbeat_interval = args.heartbeat_interval if args.heartbeat_interval is not None else int(config_data.get("heartbeat_interval", 60))
	interface = args.interface or config_data.get("interface")
	verbose = args.verbose or config_data.get("verbose", False)
	
	check_targets = args.check_target if args.check_target else config_data.get("check_targets", list(DEFAULT_CHECK_TARGETS))
	preferred_profiles = args.preferred_profile if args.preferred_profile else config_data.get("preferred_profiles", [])
	dns_check_hosts = config_data.get("dns_check_hosts", list(DEFAULT_DNS_HOSTS))

	prevent_sleep = not getattr(args, "no_prevent_sleep", False) and config_data.get("prevent_sleep", True)
	cleanup_proxy = not getattr(args, "no_cleanup_proxy", False) and config_data.get("cleanup_proxy_on_disconnect", True)
	repair_dns = not getattr(args, "no_repair_dns", False) and config_data.get("repair_dns_on_failure", True)
	adapter_reset = not getattr(args, "no_reset_adapter", False) and config_data.get("adapter_reset_on_failure", True)
	wifi_recovery = not getattr(args, "no_wifi_recovery", False) and (args.wifi_recovery or config_data.get("wifi_recovery", True))

	log_file = resolve_path(log_file_str)
	log_file.parent.mkdir(parents=True, exist_ok=True)
	state_file = resolve_path(state_file_str)
	state_file.parent.mkdir(parents=True, exist_ok=True)
	pid_file = resolve_path(pid_file_str)
	pid_file.parent.mkdir(parents=True, exist_ok=True)

	logger = configure_logging(log_file, verbose, log_max_bytes, log_backup_count)
	ensure_windows(logger)

	logger.info("配置文件: %s", config_file)
	logger.info("日志文件: %s", log_file)
	logger.info("日志轮换配置: 单个文件 %d MB，保留 %d 个备份", log_max_bytes // (1024 * 1024), log_backup_count)
	logger.info("状态文件: %s", state_file)
	logger.info("PID 文件: %s", pid_file)

	if prevent_sleep and repairs.enable_sleep_guard(logger):
		logger.info("已设置阻止系统休眠（显示器可关闭，系统保持运行）。")
	elif not prevent_sleep:
		logger.info("休眠阻止已禁用，系统可能按正常策略进入休眠。")

	repairs.check_previous_crash(pid_file, logger, log_file)
	
	args.interval = interval
	args.reconnect_timeout = reconnect_timeout
	args.log_max_bytes = log_max_bytes
	args.log_backup_count = log_backup_count
	args.heartbeat_interval = max(1, heartbeat_interval)
	args.check_target = check_targets
	args.preferred_profile = preferred_profiles
	args.dns_check_hosts = dns_check_hosts
	args.disabled_profiles = config_data.get("disabled_profiles", [])
	args.enabled_profiles = args.allowed_profile or config_data.get("enabled_profiles")
	args.dns_reset_interfaces = config_data.get("dns_reset_interfaces", [])
	args.interface = interface
	args.verbose = verbose
	args.state_file = str(state_file)
	args.pid_file = str(pid_file)
	args.prevent_sleep = prevent_sleep
	args.cleanup_proxy = cleanup_proxy
	args.repair_dns = repair_dns
	args.adapter_reset = adapter_reset
	args.wifi_recovery = wifi_recovery
	
	output_startup_diagnostics(args, logger)
	
	monitor_loop(args, logger)


if __name__ == "__main__":
	main()

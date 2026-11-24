from __future__ import annotations

import argparse
import hashlib
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
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

import repairs
import latency
from app_info import (DATA_DIR, LOG_DIR, LOG_NAME, LOG_SUBPATH, VERSION,
                      clear_internet_marks_async, migrate_legacy_layout)


APP_NAME = "network_guard"
DEFAULT_CONFIG_FILE = DATA_DIR / "network_guard_config.json"
DEFAULT_LOG_FILE = LOG_DIR / LOG_NAME
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
	if data.get("log_file") == LOG_NAME:
		# 旧版本把日志放在数据目录根下，现在统一收进 logs/ 子目录
		data["log_file"] = LOG_SUBPATH
	return data

_MAC_RE = re.compile(r"\b(?:[0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}\b")
_GUID_RE = re.compile(r"\b[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}\b")
_NETWORK_FIELD_RE = re.compile(
	r"(?i)\b(SSID|AP BSSID|BSSID|Profile|配置文件|物理地址)\s*[:：]\s*[^|\n]*"
)
_DATACLASS_FIELD_RE = re.compile(r"(?i)\b(ssid|bssid|profile)='[^']*'")
_PROFILE_LINE_RE = re.compile(r"(所有用户配置文件\s*:\s*)([^|]+)")


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

	def _mask_profile_line(match: re.Match) -> str:
		name = match.group(2).strip()
		if name in ("<无>", "<none>", ""):
			return match.group(0)
		return f"{match.group(1)}<profile>"

	text = _PROFILE_LINE_RE.sub(_mask_profile_line, text)
	text = _NETWORK_FIELD_RE.sub(lambda match: f"{match.group(1)}: <redacted>", text)
	text = _DATACLASS_FIELD_RE.sub(lambda match: f"{match.group(1)}='<redacted>'", text)
	return text


# Characters that can rewrite a terminal/log line or smuggle a second line:
# control (Cc), format/bidi (Cf) and surrogate (Cs) code points. An SSID is
# attacker-controlled input, so it must never reach a log or a line-based
# file verbatim.
_UNSAFE_NAME_CATEGORIES = ("Cc", "Cf", "Cs")
_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")


def has_unsafe_name_chars(value: str) -> bool:
	return any(unicodedata.category(ch) in _UNSAFE_NAME_CATEGORIES for ch in value)


def is_safe_profile_name(value: str | None) -> bool:
	"""Whether a Wi-Fi profile/SSID name may be used for connect attempts.

	Names with control characters are refused: they can corrupt log lines,
	the single-line state file, or netsh argument parsing.
	"""
	if not value or not value.strip() or len(value) > 512:
		return False
	return not has_unsafe_name_chars(value)


def display_name(value: str | None, limit: int = 80) -> str:
	"""One-line, terminal-safe rendering of an attacker-controlled name."""
	text = "".join(
		ch for ch in str(value or "")
		if unicodedata.category(ch) not in _UNSAFE_NAME_CATEGORIES
	)
	text = " ".join(text.split())
	return text[:limit] or "(空)"


def mask_profile(value: str | None) -> str:
	"""Stable pseudonym for a Wi-Fi profile name in logs.

	The name is often the SSID (personal/campus/venue), so disk logs only keep
	a short hash: still correlatable across lines, but not the network name.
	"""
	digest = hashlib.sha1(str(value or "").encode("utf-8", "replace")).hexdigest()[:4]
	return f"<profile:{digest}>"


class SafeFormatter(logging.Formatter):
	"""Strip CR and ANSI escapes so a crafted SSID cannot forge log lines."""

	def format(self, record: logging.LogRecord) -> str:
		return _ANSI_ESCAPE_RE.sub("", super().format(record)).replace("\r", "")


# Result of the last successful HTTP probe, shown in per-cycle logs and used by
# the latency chart window.
_last_probe: dict[str, Any] = {"ms": None, "target": None, "at": None}


def last_latency_ms() -> float | None:
	value = _last_probe.get("ms")
	return float(value) if isinstance(value, (int, float)) else None


@dataclass(frozen=True)
class WifiInterface:
	name: str
	state: str
	ssid: str | None = None
	bssid: str | None = None
	signal: str | None = None
	profile: str | None = None
	radio_hardware: str | None = None
	radio_software: str | None = None


def normalize_radio_value(value: str | None) -> str | None:
	"""Map the localized on/off words to ``on`` / ``off``."""
	if not value:
		return None
	text = value.strip().casefold()
	if text in ("开", "on", "开启", "已开启"):
		return "on"
	if text in ("关", "off", "关闭", "已关闭"):
		return "off"
	return None


def parse_radio_state(block: str) -> tuple[str | None, str | None]:
	"""Extract the radio switch state from one ``netsh`` interface block.

	``netsh`` only prints this block while the radio is off::

	    无线电状态           : 硬件 开
	                             软件 关

	so absence means "no information", never "off".
	"""
	if not re.search(r"无线电状态|Radio\s+status", block, re.IGNORECASE):
		return None, None
	hardware = re.search(r"(?:硬件|Hardware)\s*[:：]?\s*(开|关|开启|关闭|On|Off)", block, re.IGNORECASE)
	software = re.search(r"(?:软件|Software)\s*[:：]?\s*(开|关|开启|关闭|On|Off)", block, re.IGNORECASE)
	return (
		normalize_radio_value(hardware.group(1)) if hardware else None,
		normalize_radio_value(software.group(1)) if software else None,
	)


def wifi_radio_off(interfaces: Iterable[WifiInterface]) -> bool:
	"""True when a Wi-Fi interface reports its radio switched off."""
	for item in interfaces:
		if normalize_radio_value(item.radio_software) == "off":
			return True
		if normalize_radio_value(item.radio_hardware) == "off":
			return True
	return False


def _describe_radio_state(state: str | None) -> str:
	return {
		"on": "已开启",
		"off": "已关闭（软件开关），断网且无可用有线时会自动打开",
		"disabled": "已被系统禁用，需要检查飞行模式/物理开关",
		"absent": "未检测到 Wi-Fi 无线电设备",
	}.get(state or "", "未知（无法读取）")


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
		"log_file": LOG_SUBPATH,
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
		"wifi_radio_recovery": True,
		"disable_adapter_power_saving": True,
		"update_channel": "stable",
		"check_updates_on_startup": True,
		"skipped_update": "",
		"wired_recovery": True,
		"wired_reset_on_failure": True,
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
		"--export-logs",
		nargs="?",
		const="",
		default=None,
		metavar="目录",
		help=(
			"把最近的日志打包成 ZIP 后退出（可指定输出目录）。"
			f"反馈问题请将压缩包发送到 {repairs.SUPPORT_EMAIL} 并说明情况。"
		),
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
		help="只做代理/DNS/有线修复，不重连 Wi-Fi。",
	)
	parser.add_argument(
		"--no-radio-recovery",
		action="store_true",
		help="Wi-Fi 无线电被系统关闭时不自动打开（默认会自动打开并重连）。",
	)
	parser.add_argument(
		"--no-wired-recovery",
		action="store_true",
		help="断网时不检查/修复有线连接（默认优先修复有线，再尝试无线）。",
	)
	parser.add_argument(
		"--no-adapter-power-saving",
		action="store_true",
		help="不修改网卡节能设置（默认会取消“允许计算机关闭此设备以节约电源”，需管理员权限）。",
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

	formatter = SafeFormatter("%(asctime)s | %(levelname)s | %(message)s")

	# 必须在打开 handler 之前取样：崩溃回放只能包含上一次会话写入的内容，
	# 否则本次启动的诊断日志会被误当成“崩溃前的最后几行”。
	startup_log_offset = repairs.log_size_before_startup(log_file)

	file_handler = RotatingFileHandler(
		log_file,
		maxBytes=max_bytes,
		backupCount=backup_count,
		encoding="utf-8",
	)
	file_handler.setLevel(logging.DEBUG)
	file_handler.setFormatter(formatter)

	console_handler = logging.StreamHandler(sys.stdout)
	# Windows 控制台默认 GBK，日志里的符号/emoji 会触发 UnicodeEncodeError；
	# 保留原编码，只把无法编码的字符替换掉，避免整行日志丢失。
	try:
		sys.stdout.reconfigure(errors="replace")
	except (AttributeError, ValueError):
		pass
	console_handler.setLevel(logging.DEBUG if verbose else logging.INFO)
	console_handler.setFormatter(formatter)

	logger.addHandler(file_handler)
	logger.addHandler(console_handler)
	logger.propagate = False
	setattr(logger, "startup_log_offset", startup_log_offset)
	return logger


def ensure_windows(logger: logging.Logger) -> None:
	if platform.system().lower() != "windows":
		logger.error("当前程序仅支持 Windows，因为需要使用 netsh 管理 Wi-Fi 连接。")
		raise SystemExit(1)


def run_command(command: list[str], logger: logging.Logger, timeout: int | None = 30,
		env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
	"""Run a command but never let a wedged child freeze the monitor loop.

	``netsh`` can block inside the WLAN driver, where ``kill()`` does not
	return and ``subprocess.run(timeout=)`` would hang forever. The wait is
	therefore bounded by a daemon thread: if it is still alive we abandon it
	and report a timeout, because a daemon thread cannot keep the process up.

	``env`` lets callers pass attacker-controlled strings as environment
	variables instead of splicing them into a PowerShell script.
	"""
	logger.debug("执行命令: %s (超时: %s 秒)", " ".join(command), timeout or "无限")
	if timeout is None:
		return _execute(command, logger, None, env)

	bucket: list[subprocess.CompletedProcess[str]] = []
	failure: list[BaseException] = []

	def _target() -> None:
		try:
			bucket.append(_execute(command, logger, timeout, env))
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


def _execute(command: list[str], logger: logging.Logger, timeout: int | None,
		env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
	try:
		completed = subprocess.run(
			command,
			capture_output=True,
			shell=False,
			timeout=timeout,
			env=env,
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
	"""Decode console output without guessing wrong.

	``netsh`` writes the OEM code page (GBK on Chinese Windows) while other
	tools emit UTF-8. UTF-16 must only be attempted with a BOM: almost any
	even-length byte string decodes "successfully" as UTF-16 and turns a GBK
	``netsh`` summary into mojibake that then fails to parse.
	"""
	if not data:
		return ""
	if data.startswith((b"\xff\xfe", b"\xfe\xff")):
		try:
			text = data.decode("utf-16")
			if text.strip():
				logger.debug("成功用编码 utf-16 解码输出")
				return text
		except UnicodeDecodeError:
			logger.debug("带 BOM 的 utf-16 解码失败，改用其他编码")
	if data.startswith(b"\xef\xbb\xbf"):
		try:
			text = data.decode("utf-8-sig")
			if text.strip():
				logger.debug("成功用编码 utf-8-sig 解码输出")
				return text
		except UnicodeDecodeError:
			logger.debug("带 BOM 的 utf-8 解码失败，改用其他编码")
	for enc in ("utf-8", "gbk", "gb18030"):
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
	if not is_safe_profile_name(value):
		return None
	return value or None


def write_state_file(state_file: Path, profile_name: str, logger: logging.Logger) -> None:
	# The state file is line-based; a crafted profile name must not add lines.
	if not is_safe_profile_name(profile_name):
		logger.debug("跳过写入含控制字符的状态文件内容")
		return
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
			radio_hardware, radio_software = parse_radio_state(block)
			interfaces.append(WifiInterface(name=name, state=state, ssid=ssid, bssid=bssid,
				signal=signal, profile=profile,
				radio_hardware=radio_hardware, radio_software=radio_software))
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


def wifi_interfaces_from_powershell(logger: logging.Logger) -> list[WifiInterface]:
	"""Wi-Fi adapters seen through PowerShell, which does not use wlansvc.

	Used only as a fallback: ``netsh wlan show interfaces`` blocks while the
	driver is resetting (MAC reads as ``00:00:00:00:00:00``, state "正在断开连接")
	or when wlansvc is unhealthy. This path cannot report the SSID, but it does
	prove the adapter exists and shows whether it holds a usable address.
	"""
	fallback: list[WifiInterface] = []
	for item in repairs.get_physical_adapters(logger):
		if repairs.is_wired(item):
			continue
		fallback.append(
			WifiInterface(
				name=str(item.get("Name") or ""),
				state=str(item.get("Status") or "Unknown"),
				ssid=None,
				bssid=None,
				signal=None,
			)
		)
	return fallback


def get_wifi_interfaces(logger: logging.Logger, interface_name: str | None) -> list[WifiInterface]:
	completed = run_command(["netsh", "wlan", "show", "interfaces"], logger, timeout=10)
	interfaces: list[WifiInterface] = []
	if completed.returncode != 0:
		logger.debug("netsh 查询 Wi-Fi 接口失败（返回码 %s），可能是 wlansvc 异常或网卡正在重置",
			completed.returncode)
	else:
		interfaces = parse_wifi_interfaces(completed.stdout or "")
		if not interfaces:
			raw = (completed.stdout or "").strip().replace("\n", " | ")
			logger.debug("netsh 输出无法解析出 Wi-Fi 接口: %s", raw[:200] or "(空)")
	if not interfaces:
		# netsh 依赖 wlansvc，驱动重置期间会挂起或报错；PowerShell 仍能读到网卡。
		interfaces = wifi_interfaces_from_powershell(logger)
		if interfaces:
			logger.warning(
				"netsh 未能读取 Wi-Fi 接口（可能 wlansvc 异常或网卡正在重置），"
				"已改用 PowerShell 识别到 %d 个无线网卡: %s",
				len(interfaces),
				"; ".join(f"{item.name}={item.state}" for item in interfaces),
			)
	if interface_name:
		interfaces = [item for item in interfaces if item.name.casefold() == interface_name.casefold()]
	logger.debug("识别到 Wi-Fi 网卡: %s", redact(str(interfaces)))
	return interfaces


def get_saved_profiles(logger: logging.Logger) -> list[str]:
	completed = run_command(["netsh", "wlan", "show", "profiles"], logger, timeout=10)
	if completed.returncode != 0:
		logger.debug("netsh 查询已保存配置失败（返回码 %s）", completed.returncode)
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
	logger.debug("已保存的 Wi-Fi 配置: %s", [mask_profile(name) for name in deduped])
	return deduped


_PING_FAILURE_RE = re.compile(
	r"无法访问目标主机|目标主机不可达|目标网络不可达|传输失败|请求超时|"
	r"destination (?:host|net(?:work)?) unreachable|general failure|request timed out|"
	r"100%\s*(?:packet\s*)?loss",
	re.IGNORECASE,
)
_PING_REPLY_RE = re.compile(r"\bTTL[=＝]\s*\d+", re.IGNORECASE)


def ping_reachable(logger: logging.Logger, timeout_seconds: int = 2) -> bool:
	"""链路层是否通：ping 得通公共地址就说明链路（Wi-Fi/以太网）没断。

	Windows returns exit code 0 for "无法访问目标主机" replies, so the exit code
	alone is not proof: a real reply must carry a TTL and no unreachable marker.
	"""
	for host in ("223.5.5.5", "119.29.29.29"):
		try:
			timeout_ms = max(1, timeout_seconds * 1000)
			completed = run_command(
				["ping", "-n", "1", "-w", str(timeout_ms), host], logger, timeout_seconds + 5
			)
			if completed.returncode != 0:
				continue
			text = f"{completed.stdout or ''}\n{completed.stderr or ''}"
			if _PING_FAILURE_RE.search(text):
				logger.debug("Ping 未真正到达 %s: %s", host, text.strip()[:120])
				continue
			if not _PING_REPLY_RE.search(text):
				logger.debug("Ping 返回码为 0 但没有有效回复: %s", host)
				continue
			logger.debug("Ping 检查成功: %s", host)
			return True
		except Exception as exc:  # noqa: BLE001
			logger.debug("Ping 检查失败: %s, %s", host, exc)
	return False


def http_reachable(logger: logging.Logger, targets: Iterable[str], timeout_seconds: int = 4) -> bool:
	"""应用层是否通；任一地址成功即视为在线。

	每次探测都会记录每个地址的延迟（最多前 5 个），单个地址超时不会触发
	网络重置——只有全部地址都失败才算断网。每次新建 opener，避免缓存住旧
	的系统代理设置。
	"""
	import urllib.request

	_last_probe.update(ms=None, target=None, at=None)
	selected = latency.effective_targets(targets) or list(DEFAULT_CHECK_TARGETS)
	online = False
	for target in selected:
		started = time.perf_counter()
		try:
			opener = urllib.request.build_opener()
			with opener.open(target, timeout=timeout_seconds) as response:
				elapsed_ms = (time.perf_counter() - started) * 1000
				# Dedicated 204 endpoints must not accept a portal/login page as online.
				if target.rstrip("/").endswith("/generate_204"):
					if response.status == 204 and response.geturl() == target:
						_record_probe(target, elapsed_ms, logger)
						online = True
					else:
						logger.debug("联网探测未返回原地址的 204：%s", target)
						latency.record_latency(target, None, False)
					continue
				if 200 <= getattr(response, "status", 200) < 400:
					_record_probe(target, elapsed_ms, logger)
					online = True
				else:
					latency.record_latency(target, None, False)
		except Exception as exc:  # noqa: BLE001
			logger.debug("HTTP 检测失败: %s, %s", target, exc)
			latency.record_latency(target, None, False)
	return online


def _record_probe(target: str, elapsed_ms: float, logger: logging.Logger) -> None:
	_last_probe.update(ms=elapsed_ms, target=target, at=time.time())
	latency.record_latency(target, elapsed_ms, True)
	logger.debug("HTTP 检测成功: %s (%d ms)", target, elapsed_ms)


def is_internet_reachable(logger: logging.Logger, targets: Iterable[str], timeout_seconds: int = 4) -> bool:
	"""以 HTTP(S) 结果为准，而不是 ping。

	代理工具失效时 ping 依然通、只有应用层断，用 ping 判定会漏报。因此
	ping 只用来区分"链路断了"和"链路正常但应用层坏了"（``ping_reachable``），
	判定是否在线一律看 HTTP。未配置检测地址时使用默认 HTTP 目标。
	"""
	targets = list(targets) or list(DEFAULT_CHECK_TARGETS)
	return http_reachable(logger, targets, timeout_seconds)


_RADIO_OFF_OUTPUT_RE = re.compile(
	r"电源关闭|电源已关闭|无线电.{0,6}关|powered\s+off|radio\s+is\s+off|0x80342002|2150899714",
	re.IGNORECASE,
)


def connect_wifi(profile_name: str, logger: logging.Logger, interface_name: str | None) -> bool:
	if not is_safe_profile_name(profile_name):
		logger.warning("跳过名称含控制字符的 Wi-Fi 配置：%s", display_name(profile_name))
		return False
	command = ["netsh", "wlan", "connect", f"name={profile_name}"]
	if interface_name:
		command.append(f"interface={interface_name}")
	logger.debug("发起 Wi-Fi 连接请求: %s", display_name(profile_name))
	completed = run_command(command, logger, timeout=15)
	if completed.returncode != 0:
		output = f"{completed.stdout or ''}\n{completed.stderr or ''}"
		if _RADIO_OFF_OUTPUT_RE.search(output):
			logger.error(
				"⛔ 系统报告无线接口电源关闭（无线电被关闭），连接请求被拒绝: %s",
				display_name(profile_name),
			)
		else:
			logger.warning("尝试连接 Wi-Fi 配置失败: %s (返回码: %s)",
				display_name(profile_name), completed.returncode)
		return False
	logger.info("已发起连接请求: %s", display_name(profile_name))
	return True


def choose_profiles_to_try(
	preferred_profiles: list[str],
	last_successful: str | None,
	saved_profiles: list[str],
	disabled_profiles: list[str] | None = None,
	enabled_profiles: list[str] | None = None,
) -> list[str]:
	disabled = {p.casefold() for p in (disabled_profiles or [])}
	# netsh 查询失败（wlansvc 异常/驱动重置）时拿不到已保存列表，
	# 退回用户配置的参与名单，至少还能尝试连接。
	if not saved_profiles and enabled_profiles:
		saved_profiles = [p for p in enabled_profiles if p]
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
	wired_recovery: bool = True,
	wired_reset: bool = False,
	dns_reset_interfaces: Iterable[str] | None = None,
	repair_stats: dict[str, int] | None = None,
	radio_recovery: bool = True,
) -> bool:
	"""按"无线电 → DNS → 系统代理 → 有线连接 → Wi-Fi 重连 → 网卡重置"修复。

	无线电排在最前：系统把 Wi-Fi 无线电关掉时（飞行模式、Wi-Fi 开关被关、
	驱动进入低功耗），``netsh wlan connect`` 只会返回 0x80342002，任何配置
	都连不上，而这一步恰好不需要管理员权限。

	有线优先：全部连接失效时先修有线（DHCP 续约 → 必要时重置网卡），
	再尝试无线。有线存在不会跳过无线：有线链路修好后 HTTP 仍不可用
	时继续往下走，因为插着网线不代表能上网。

	ping 成功只说明 ICMP 可达，不能证明 HTTP 或远控流量正常。
	"""
	repaired = False
	radio_off_at_entry = False
	radio_blocked = False

	# 硬约束：只要有一条健康的有线链路（Up + 有效 IP + 默认网关），
	# 本轮就绝不碰无线——不打开无线电、不重连 Wi-Fi、不重置网卡。
	# 很多笔记本/扩展坞在插网线时无法同时连无线（OEM 会在有线接通时关掉
	# WLAN 无线电），此时"帮忙"打开无线反而可能把正在工作的有线弄断。
	adapters: list[dict] = []
	wired_healthy_at_entry = False
	if wired_recovery:
		adapters = repairs.get_physical_adapters(logger)
		wired_at_entry = [item for item in adapters if repairs.is_wired(item)]
		wired_healthy_at_entry = any(repairs.is_link_healthy(item) for item in wired_at_entry)
		if wired_healthy_at_entry:
			logger.info(
				"有线连接健康（已获得有效 IP 与网关），本轮不会改动无线电或 Wi-Fi。"
			)

	def try_radio_repair(reason: str) -> bool:
		"""Re-enable the radio; True only when HTTP comes back by itself."""
		nonlocal radio_blocked
		logger.warning("检测到 Wi-Fi 无线电已关闭（%s），尝试重新打开。", reason)
		if not repairs.enable_wifi_radio(logger):
			radio_blocked = True
			logger.error(
				"⛔ 无法自动打开 Wi-Fi 无线电（飞行模式、物理开关、组策略或权限限制）。"
			)
			if repair_stats is not None:
				repair_stats["radio_blocked"] = repair_stats.get("radio_blocked", 0) + 1
			return False
		for _ in range(5):
			if is_internet_reachable(logger, check_targets):
				if repair_stats is not None:
					repair_stats["radio_repairs"] = repair_stats.get("radio_repairs", 0) + 1
				logger.info("✓ 重新打开无线电后网络已恢复。")
				return True
			time.sleep(3)
		logger.warning("无线电已打开但网络尚未恢复，继续后续修复。")
		return False

	def reset_permitted() -> bool:
		"""Whether resetting the Wi-Fi adapter is allowed by the policy switches."""
		if not adapter_reset:
			return False
		if enabled_profiles is None and not disabled_profiles:
			return True
		# 无线电是关的：没有"正在使用的其它配置"会被抢占，重置只剩好处。
		return radio_off_at_entry or radio_blocked

	# 0) 无线电：被系统关掉时，一切 Wi-Fi 连接请求都会被拒绝。
	#    有线健康时跳过：有的机器插上网线后 WLAN 会被 OEM 策略关掉，
	#    那不是故障，硬开无线电可能反过来断掉正在工作的有线。
	if wifi_recovery and radio_recovery and not wired_healthy_at_entry:
		radio_state = repairs.wifi_radio_state(logger)
		if radio_state in ("off", "disabled"):
			radio_off_at_entry = True
			if try_radio_repair(radio_state):
				return True

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
					if is_internet_reachable(logger, check_targets):
						if radio_off_at_entry:
							# 无线电刚被重新打开时，链路恢复也会让 DNS 变好，
							# 不能把这份功劳记到 DNS 修复上。
							if repair_stats is not None:
								repair_stats["radio_repairs"] = repair_stats.get("radio_repairs", 0) + 1
							logger.info("✓ 网络已恢复（本轮无线电曾关闭，按链路恢复统计）。")
						else:
							if repair_stats is not None:
								repair_stats["dns_repairs"] = repair_stats.get("dns_repairs", 0) + 1
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

	# 3) 有线连接优先：全部连接失效时先检查/修复有线，再尝试无线。
	#    典型故障是"网线插着但 DHCP 没给 IPv4 地址"（169.254.x.x），
	#    这种情况重置 DNS 治不好，必须重新获取租约。
	if wired_healthy_at_entry:
		# 有线本来就能正常上网时绝不动无线：重连 Wi-Fi 会抢占默认路由，
		# 在只能二选一的机型上还可能直接断开有线。只有全部失效才往下走无线。
		logger.info("有线连接可用，已跳过无线重连以避免中断现有网络。")
		return False

	if wired_recovery:
		wired = [item for item in adapters if repairs.is_wired(item)]
		if not wired:
			logger.info("未检测到有线网卡，直接尝试无线连接。")
		else:
			media_down = [item for item in wired if repairs.is_wired_media_down(item)]
			if len(media_down) == len(wired):
				# 网线未插、网卡被禁用：续约只会立刻失败，白白吃掉重连预算。
				logger.info(
					"有线链路全部断开（%s 媒体未连接/网线未插），跳过有线修复，直接尝试无线。",
					", ".join(str(item.get("Name") or "?") for item in media_down),
				)
			elif repairs.repair_wired_links(logger, allow_reset=wired_reset, adapters=adapters):
				if repair_stats is not None:
					repair_stats["wired_repairs"] = repair_stats.get("wired_repairs", 0) + 1
				if is_internet_reachable(logger, check_targets):
					logger.info("✓ 有线连接修复后网络已恢复。")
					return True
				logger.warning("有线链路已修复但 HTTP 仍不可用，继续尝试无线连接。")
			else:
				logger.warning("有线连接修复未成功，继续尝试无线连接。")
	else:
		logger.info("有线连接修复已禁用，跳过。")

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

	# WinRT 探测失败（旧系统/被禁用）时，netsh 的"无线电状态"是最后的线索。
	if (
		wifi_recovery
		and radio_recovery
		and not radio_off_at_entry
		and interfaces
		and wifi_radio_off(interfaces)
	):
		radio_off_at_entry = True
		if try_radio_repair("netsh 报告无线电关闭"):
			return True

	saved_profiles = get_saved_profiles(logger)
	last_successful = read_state_file(state_file)
	candidates = choose_profiles_to_try(preferred_profiles, last_successful, saved_profiles, disabled_profiles, enabled_profiles)
	# SSID/配置名是外部输入：带控制字符（换行、ANSI、双向覆盖等）的一律不参与，
	# 避免日志/状态文件被伪造，也避免把异常参数交给 netsh。
	unsafe = [name for name in candidates if not is_safe_profile_name(name)]
	if unsafe:
		logger.warning("跳过 %d 个名称含控制字符/异常的 Wi-Fi 配置，不再尝试连接。", len(unsafe))
		candidates = [name for name in candidates if is_safe_profile_name(name)]

	if not candidates:
		logger.warning("没有可参与恢复的 Wi-Fi 配置（未保存配置或已全部禁用）。")
		if reset_permitted():
			logger.info("无可用配置，尝试硬件重置 Wi-Fi 网卡…")
			if repairs.reset_wifi_adapter(logger):
				time.sleep(10)
				if is_internet_reachable(logger, check_targets):
					logger.info("✓ 网卡重置后网络已恢复。")
					return True
		elif adapter_reset:
			logger.info("为遵守 Wi-Fi 参与恢复限制，跳过可能自动连接其他配置的网卡重置。")
		if radio_blocked:
			logger.error(
				"无线被系统关闭、有线连接也不可用：请关闭飞行模式/打开 Wi-Fi 开关，"
				"或以管理员身份运行（install_admin_task.bat）后重试。"
			)
		return False

	logger.info("开始自动恢复，共有 %d 个候选配置，限定时间 %d 秒", len(candidates), reconnect_timeout)
	logger.info("恢复优先级顺序: %s", " → ".join(mask_profile(name) for name in candidates))
	deadline = time.time() + reconnect_timeout

	for idx, profile_name in enumerate(candidates, 1):
		if time.time() > deadline:
			logger.warning("自动恢复已超过限定时间，停止进一步尝试。")
			return False

		remaining_time = int(deadline - time.time())
		logger.info("[%d/%d] 尝试连接 Wi-Fi 配置: %s (剩余时间: %d 秒)", idx, len(candidates), mask_profile(profile_name), remaining_time)
		
		if not connect_wifi(profile_name, logger, interface_name):
			logger.debug("配置 %s 连接请求发送失败，跳过", mask_profile(profile_name))
			continue

		attempt_deadline = min(deadline, time.time() + 20)
		check_interval = 3
		attempts = 0
		
		logger.debug("等待配置 %s 的网络恢复（最多 20 秒）...", mask_profile(profile_name))
		while time.time() < attempt_deadline:
			attempts += 1
			if is_internet_reachable(logger, check_targets):
				elapsed_time = int(time.time() - (deadline - reconnect_timeout))
				logger.info("✓ 网络已恢复！配置: %s，总耗时: %d 秒，尝试次数: %d", mask_profile(profile_name), elapsed_time, attempts)
				write_state_file(state_file, profile_name, logger)
				return True
			
			remaining = int(attempt_deadline - time.time())
			if remaining > 0:
				logger.debug("  等待中... (已尝试 %d 次，剩余 %d 秒)", attempts, remaining)
			
			time.sleep(check_interval)

		logger.warning("配置 %s 已发起连接，但在 20 秒内仍未恢复联网 (尝试 %d 次)", mask_profile(profile_name), attempts)

	logger.error("✗ 已尝试全部 %d 个候选 Wi-Fi 配置，网络仍未恢复。", len(candidates))

	# 3) 最后手段：禁用再启用网卡，突破驱动层挂起（需管理员权限）
	if reset_permitted():
		logger.info("所有候选配置均失败，尝试硬件重置 Wi-Fi 网卡…")
		if repairs.reset_wifi_adapter(logger):
			time.sleep(10)
			if is_internet_reachable(logger, check_targets):
				logger.info("✓ 网卡重置后网络已恢复。")
				return True
			logger.warning("网卡已重置但网络仍未恢复。")
	elif adapter_reset:
		logger.info("为遵守 Wi-Fi 参与恢复限制，跳过可能自动连接其他配置的网卡重置。")

	if radio_blocked:
		logger.error(
			"无线被系统关闭、有线连接也不可用：请关闭飞行模式/打开 Wi-Fi 开关，"
			"或以管理员身份运行（install_admin_task.bat）后重试。"
		)
	return False


def output_startup_diagnostics(args: argparse.Namespace, logger: logging.Logger) -> None:
	logger.info("========== 程序启动诊断信息 ==========")
	
	logger.info("Python 版本: %s", sys.version.split()[0])
	logger.info("操作系统: %s %s", platform.system(), platform.release())
	logger.info("处理器: %s", platform.processor() or "Unknown")
	logger.info("运行权限: %s", "管理员" if repairs.has_admin_privileges() else "普通用户（无法禁用/启用网卡）")
	logger.info("Wi-Fi 无线电: %s", _describe_radio_state(repairs.wifi_radio_state(logger)))
	
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
			logger.info("  %d. %s", i, mask_profile(profile))
	else:
		logger.warning("  - 未找到任何已保存的 Wi-Fi 配置")
	
	logger.info("恢复策略:")
	logger.info("  - 系统代理清理: %s", "启用（断网时关闭系统代理）" if args.cleanup_proxy else "禁用")
	logger.info("  - DNS 故障修复: %s", "启用（清空缓存 + 网卡 DNS 回退 DHCP）" if args.repair_dns else "禁用")
	logger.info("  - Wi-Fi 重连恢复: %s", "启用" if args.wifi_recovery else "禁用（有线环境）")
	logger.info("  - 无线电恢复: %s", "启用（无线被关闭时自动打开）" if getattr(args, "radio_recovery", True) else "禁用")
	logger.info("  - 关闭网卡节能: %s", "启用（取消“允许计算机关闭此设备以节约电源”）"
	            if getattr(args, "disable_adapter_power_saving", True) else "禁用")
	logger.info("  - 失败后重置网卡: %s", "启用" if args.adapter_reset else "禁用")
	logger.info("  - 阻止系统休眠: %s", "启用" if args.prevent_sleep else "禁用")
	logger.info("  - 心跳间隔: %s 秒", args.heartbeat_interval)
	logger.info("  - DNS 监测域名: %s", ", ".join(args.dns_check_hosts or []) or "(未配置)")

	logger.info("========== 诊断完成，监控循环启动 ==========")


def monitor_loop(args: argparse.Namespace, logger: logging.Logger) -> None:
	# 单实例保护：命名互斥量比 PID 文件可靠，进程崩溃时内核会自动释放。
	# CLI 与 GUI 后台 worker 共用同一把锁，保证同一时间只有一个守护在操作网络。
	if not repairs.acquire_single_instance("Monitor"):
		logger.error("⛔ 已有另一个守护实例在运行，本次启动已取消。请先停止已有实例。")
		print("已有一个 Network Guard 守护实例在运行，本次启动已取消。", file=sys.stderr)
		raise SystemExit(2)

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
	recovery_stats = {"attempts": 0, "successes": 0, "failures": 0, "dns_repairs": 0,
		"proxy_cleanups": 0, "wired_repairs": 0, "radio_repairs": 0, "radio_blocked": 0}
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
				latency_text = f"{last_latency_ms():.0f} ms" if last_latency_ms() is not None else "未知"
				logger.info("本轮检测：网络可用 · 延迟 %s", latency_text)
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
					wired_recovery=getattr(args, "wired_recovery", True),
					wired_reset=getattr(args, "wired_reset", False),
					dns_reset_interfaces=getattr(args, "dns_reset_interfaces", []),
					repair_stats=recovery_stats,
					radio_recovery=getattr(args, "radio_recovery", True),
				)
				
				if recovered:
					recovery_stats["successes"] += 1
					success_rate = 100 * recovery_stats["successes"] / recovery_stats["attempts"]
					logger.info("恢复成功率统计: %d/%d (%.1f%%)", recovery_stats["successes"], recovery_stats["attempts"], success_rate)
					logger.info("分项统计：DNS 修复 %d 次，代理清理后恢复 %d 次，有线修复后恢复 %d 次，无线电恢复 %d 次",
						recovery_stats["dns_repairs"], recovery_stats["proxy_cleanups"],
						recovery_stats.get("wired_repairs", 0), recovery_stats.get("radio_repairs", 0))
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
	migrate_legacy_layout()
	clear_internet_marks_async()
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
	raw_target_count = len(check_targets)
	check_targets = list(check_targets)[:5]
	preferred_profiles = args.preferred_profile if args.preferred_profile else config_data.get("preferred_profiles", [])
	dns_check_hosts = config_data.get("dns_check_hosts", list(DEFAULT_DNS_HOSTS))

	prevent_sleep = not getattr(args, "no_prevent_sleep", False) and config_data.get("prevent_sleep", True)
	cleanup_proxy = not getattr(args, "no_cleanup_proxy", False) and config_data.get("cleanup_proxy_on_disconnect", True)
	repair_dns = not getattr(args, "no_repair_dns", False) and config_data.get("repair_dns_on_failure", True)
	adapter_reset = not getattr(args, "no_reset_adapter", False) and config_data.get("adapter_reset_on_failure", True)
	wifi_recovery = not getattr(args, "no_wifi_recovery", False) and (args.wifi_recovery or config_data.get("wifi_recovery", True))
	radio_recovery = not getattr(args, "no_radio_recovery", False) and config_data.get("wifi_radio_recovery", True)
	wired_recovery = not getattr(args, "no_wired_recovery", False) and config_data.get("wired_recovery", True)
	wired_reset = adapter_reset and config_data.get("wired_reset_on_failure", True)
	disable_adapter_power_saving = (
		not getattr(args, "no_adapter_power_saving", False)
		and config_data.get("disable_adapter_power_saving", True)
	)

	log_file = resolve_path(log_file_str)
	log_file.parent.mkdir(parents=True, exist_ok=True)

	# 打包日志不需要启动守护，也不需要创建日志文件，因此放在配置日志之前。
	if args.export_logs is not None:
		destination = resolve_path(args.export_logs) if args.export_logs else None
		archive = repairs.export_logs(log_file, VERSION, destination)
		if archive is None:
			print(f"没有找到日志文件: {log_file}", file=sys.stderr)
			print("请先运行一段时间，让程序产生日志后再打包。", file=sys.stderr)
			sys.exit(1)
		print(f"日志已打包: {archive}")
		print(f"遇到问题请把该压缩包发送到 {repairs.SUPPORT_EMAIL}，并描述具体情况。")
		return

	state_file = resolve_path(state_file_str)
	state_file.parent.mkdir(parents=True, exist_ok=True)
	pid_file = resolve_path(pid_file_str)
	pid_file.parent.mkdir(parents=True, exist_ok=True)

	logger = configure_logging(log_file, verbose, log_max_bytes, log_backup_count)
	if raw_target_count > 5:
		logger.warning("检测地址最多使用前 5 个，其余 %d 个已忽略。", raw_target_count - 5)
	ensure_windows(logger)

	# 单实例预检查：放在耗时的启动诊断（netsh 可能超时 10 秒）之前，
	# 这样重复启动能立刻失败退出，而不是等一轮网络探测。
	if not repairs.acquire_single_instance("Monitor"):
		logger.error("⛔ 已有另一个守护实例在运行，本次启动已取消。请先停止已有实例。")
		print("已有一个 Network Guard 守护实例在运行，本次启动已取消。", file=sys.stderr)
		sys.exit(2)

	logger.info("配置文件: %s", config_file)
	logger.info("日志文件: %s", log_file)
	logger.info("日志轮换配置: 单个文件 %d MB，保留 %d 个备份", log_max_bytes // (1024 * 1024), log_backup_count)
	logger.info("状态文件: %s", state_file)
	logger.info("PID 文件: %s", pid_file)

	if prevent_sleep and repairs.enable_sleep_guard(logger):
		logger.info("已设置阻止系统休眠（显示器可关闭，系统保持运行）。")
	elif not prevent_sleep:
		logger.info("休眠阻止已禁用，系统可能按正常策略进入休眠。")

	repairs.check_previous_crash(pid_file, logger, log_file, getattr(logger, "startup_log_offset", None))
	
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
	args.wired_recovery = wired_recovery
	args.wired_reset = wired_reset
	args.cleanup_proxy = cleanup_proxy
	args.repair_dns = repair_dns
	args.adapter_reset = adapter_reset
	args.wifi_recovery = wifi_recovery
	args.radio_recovery = radio_recovery
	args.disable_adapter_power_saving = disable_adapter_power_saving
	
	output_startup_diagnostics(args, logger)
	
	if args.disable_adapter_power_saving:
		repairs.disable_adapter_power_saving(logger, args.interface)
	
	monitor_loop(args, logger)


if __name__ == "__main__":
	main()

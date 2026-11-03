from __future__ import annotations

import argparse
import json
import logging
from logging.handlers import RotatingFileHandler
import platform
import re
import socket
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable


APP_NAME = "network_guard"
DEFAULT_CONFIG_FILE = Path(__file__).with_name("network_guard_config.json")
DEFAULT_LOG_FILE = Path(__file__).with_name("network_guard.log")
DEFAULT_STATE_FILE = Path(__file__).with_name("network_guard_state.txt")
DEFAULT_CHECK_TARGETS = (
	"https://www.msftconnecttest.com/connecttest.txt",
	"https://www.baidu.com",
	"https://www.cloudflare.com",
)


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
			return json.load(f)
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
		"log_file": str(DEFAULT_LOG_FILE),
		"log_max_bytes": 2 * 1024 * 1024,
		"log_backup_count": 5,
		"state_file": str(DEFAULT_STATE_FILE),
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
	logger.debug("执行命令: %s (超时: %s 秒)", " ".join(command), timeout or "无限")
	try:
		completed = subprocess.run(
			command,
			capture_output=True,
			shell=False,
			timeout=timeout,
			creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
		)
	except subprocess.TimeoutExpired as exc:
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
		logger.debug("命令输出: %s", stdout.replace("\r", " ").replace("\n", " | "))
	if stderr:
		logger.debug("命令错误输出: %s", stderr.replace("\r", " ").replace("\n", " | "))
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


def is_internet_reachable(logger: logging.Logger, targets: Iterable[str], timeout_seconds: int = 4) -> bool:
	ping_targets = ["1.1.1.1", "8.8.8.8"]
	for host in ping_targets:
		try:
			timeout_ms = max(1, timeout_seconds * 1000)
			completed = run_command(["ping", "-n", "1", "-w", str(timeout_ms), host], logger)
			if completed.returncode == 0:
				logger.debug("Ping 检查成功: %s", host)
				return True
		except Exception as exc:  # noqa: BLE001
			logger.debug("Ping 检查失败: %s, %s", host, exc)

	import urllib.request

	for target in targets:
		try:
			with urllib.request.urlopen(target, timeout=timeout_seconds) as response:
				if 200 <= getattr(response, "status", 200) < 400:
					logger.debug("联网检测成功: %s", target)
					return True
		except Exception as exc:  # noqa: BLE001
			logger.debug("联网检测失败: %s, %s", target, exc)
	return False


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
) -> bool:
	interfaces = get_wifi_interfaces(logger, interface_name)
	if interfaces:
		logger.info("当前 Wi-Fi 状态: %s", "; ".join(f"{item.name}={item.state}({item.ssid or 'no-ssid'})" for item in interfaces))
	else:
		logger.info("未识别到可用的 Wi-Fi 网卡，仍将尝试对已保存配置发起连接。")

	saved_profiles = get_saved_profiles(logger)
	last_successful = read_state_file(state_file)
	candidates = choose_profiles_to_try(preferred_profiles, last_successful, saved_profiles, disabled_profiles, enabled_profiles)

	if not candidates:
		logger.warning("没有可参与恢复的 Wi-Fi 配置（未保存配置或已全部禁用）。")
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
			logger.info("  - %s: %s (SSID: %s, 信号: %s)", iface.name, iface.state, iface.ssid or "未连接", iface.signal or "N/A")
	else:
		logger.info("  - 未识别到 Wi-Fi 网卡或网卡不可用")
	
	logger.info("已保存的 Wi-Fi 配置:")
	profiles = get_saved_profiles(logger)
	if profiles:
		for i, profile in enumerate(profiles, 1):
			logger.info("  %d. %s", i, profile)
	else:
		logger.warning("  - 未找到任何已保存的 Wi-Fi 配置")
	
	logger.info("========== 诊断完成，监控循环启动 ==========")


def monitor_loop(args: argparse.Namespace, logger: logging.Logger) -> None:
	state_file = Path(args.state_file)
	check_targets = list(dict.fromkeys(args.check_target or list(DEFAULT_CHECK_TARGETS)))
	logger.info("程序启动，轮询间隔=%s 秒，重连超时=%s 秒", args.interval, args.reconnect_timeout)
	logger.info("联网检测目标: %s", check_targets)

	last_online = None
	recovery_stats = {"attempts": 0, "successes": 0, "failures": 0}
	
	while True:
		try:
			online = is_internet_reachable(logger, check_targets)
			if online:
				if last_online is not True:
					logger.info("网络状态恢复为正常。")
				last_online = True
			else:
				if last_online is not False:
					logger.warning("检测到网络不可用，准备执行 Wi-Fi 自动恢复。")
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
				)
				
				if recovered:
					recovery_stats["successes"] += 1
					success_rate = 100 * recovery_stats["successes"] / recovery_stats["attempts"]
					logger.info("恢复成功率统计: %d/%d (%.1f%%)", recovery_stats["successes"], recovery_stats["attempts"], success_rate)
				else:
					recovery_stats["failures"] += 1
					failure_rate = 100 * recovery_stats["failures"] / recovery_stats["attempts"]
					logger.warning("恢复失败率统计: %d/%d (%.1f%%)", recovery_stats["failures"], recovery_stats["attempts"], failure_rate)
		except KeyboardInterrupt:
			if recovery_stats["attempts"] > 0:
				success_rate = 100 * recovery_stats["successes"] / recovery_stats["attempts"]
				logger.info("程序退出前的恢复统计: 成功 %d/%d (%.1f%%)", recovery_stats["successes"], recovery_stats["attempts"], success_rate)
			logger.info("收到中断信号，程序退出。")
			return
		except Exception as exc:  # noqa: BLE001
			logger.exception("监控循环出现异常: %s", exc)

		time.sleep(max(1, args.interval))


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
	log_file_str = args.log_file or config_data.get("log_file", str(DEFAULT_LOG_FILE))
	log_max_bytes = args.log_max_bytes if args.log_max_bytes is not None else config_data.get("log_max_bytes", 2 * 1024 * 1024)
	log_backup_count = args.log_backup_count if args.log_backup_count is not None else config_data.get("log_backup_count", 5)
	state_file_str = args.state_file or config_data.get("state_file", str(DEFAULT_STATE_FILE))
	interface = args.interface or config_data.get("interface")
	verbose = args.verbose or config_data.get("verbose", False)
	
	check_targets = args.check_target if args.check_target else config_data.get("check_targets", list(DEFAULT_CHECK_TARGETS))
	preferred_profiles = args.preferred_profile if args.preferred_profile else config_data.get("preferred_profiles", [])

	log_file = Path(log_file_str)
	log_file.parent.mkdir(parents=True, exist_ok=True)
	state_file = Path(state_file_str)
	state_file.parent.mkdir(parents=True, exist_ok=True)

	logger = configure_logging(log_file, verbose, log_max_bytes, log_backup_count)
	ensure_windows(logger)

	logger.info("配置文件: %s", config_file)
	logger.info("日志文件: %s", log_file)
	logger.info("日志轮换配置: 单个文件 %d MB，保留 %d 个备份", log_max_bytes // (1024 * 1024), log_backup_count)
	logger.info("状态文件: %s", state_file)
	
	args.interval = interval
	args.reconnect_timeout = reconnect_timeout
	args.log_max_bytes = log_max_bytes
	args.log_backup_count = log_backup_count
	args.check_target = check_targets
	args.preferred_profile = preferred_profiles
	args.disabled_profiles = config_data.get("disabled_profiles", [])
	args.enabled_profiles = config_data.get("enabled_profiles")
	args.interface = interface
	args.verbose = verbose
	args.state_file = state_file_str
	
	output_startup_diagnostics(args, logger)
	
	monitor_loop(args, logger)


if __name__ == "__main__":
	main()

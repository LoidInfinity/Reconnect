"""Network Guard desktop UI. Run with python gui.py (Windows)."""
from __future__ import annotations

import argparse
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
import time
import tempfile
import traceback
import webbrowser
import tkinter as tk
from tkinter import messagebox, ttk
from tkinter import font as tkfont
from urllib.parse import urlparse

import main as guard
from app_info import (VERSION, APP_DIR, DATA_DIR, FROZEN, worker_command, resource_path,
                     hidden_profiles_path)
from updates import CHANNEL_LABELS, CHANNELS, check_updates

UPDATE_LABEL_TO_CHANNEL = {label: channel for channel, label in CHANNEL_LABELS.items()}

BASE = APP_DIR
BG = "#f3f6fb"
INK = "#172b4d"
MUTED = "#64748b"


def wrap_label(label, parent, margin: int = 0) -> None:
    """Keep a label's text wrapped to its container width."""
    parent.bind(
        "<Configure>",
        lambda event: label.configure(wraplength=max(80, event.width - margin)),
        add="+",
    )
    label.configure(justify="left", wraplength=400)
BLUE = "#2563eb"


def wheel_scrolls_container(widget) -> bool:
    """Wheel over a list/text view must scroll that view, not the whole tab."""
    return not isinstance(widget, (ttk.Treeview, tk.Text))


def load_hidden_profiles(path: Path) -> list[str]:
    """Read the GUI-only hidden list (one Wi-Fi name per line)."""
    for candidate in (path, DATA_DIR / "hidden_wifi.txt"):
        try:
            if candidate.is_file():
                names = [line.strip() for line in candidate.read_text(encoding="utf-8").splitlines()]
                return [name for name in names if name]
        except OSError:
            continue
    return []


def save_hidden_profiles(path: Path, names: list[str]) -> Path:
    """Write the hidden list; fall back to the data dir if config/ is read-only."""
    cleaned: list[str] = []
    seen: set[str] = set()
    for name in names:
        text = str(name or "")
        if not text.strip() or guard.has_unsafe_name_chars(text):
            continue
        key = text.casefold()
        if key not in seen:
            seen.add(key)
            cleaned.append(text)
    body = "\n".join(cleaned)
    error: OSError | None = None
    for candidate in (path, DATA_DIR / "hidden_wifi.txt"):
        try:
            candidate.parent.mkdir(parents=True, exist_ok=True)
            candidate.write_text(body + ("\n" if body else ""), encoding="utf-8")
            return candidate
        except OSError as exc:
            error = exc
    raise error if error else OSError("无法保存隐藏名单")


def read_settings(path: Path) -> dict:
    defaults = dict(interval=10, reconnect_timeout=35,
                    check_targets=list(guard.DEFAULT_CHECK_TARGETS),
                    preferred_profiles=[], disabled_profiles=[], enabled_profiles=None, interface=None, verbose=False,
                    log_file=guard.LOG_SUBPATH,
                    state_file=guard.DEFAULT_STATE_FILE.name,
                    log_max_bytes=2097152, log_backup_count=5,
                    heartbeat_interval=60, pid_file=guard.DEFAULT_PID_FILE.name,
                    prevent_sleep=True, cleanup_proxy_on_disconnect=True,
                    repair_dns_on_failure=True, dns_check_hosts=list(guard.DEFAULT_DNS_HOSTS), dns_reset_interfaces=[],
                    wired_recovery=True, wired_reset_on_failure=True,
                    wifi_recovery=True, wifi_radio_recovery=True, adapter_reset_on_failure=True,
                    disable_adapter_power_saving=True, update_channel="stable")
    if path.exists():
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        if not isinstance(data, dict):
            raise ValueError("配置文件必须是 JSON 对象")
        defaults.update(guard.migrate_config(data))
    return defaults


def validate_settings(data: dict) -> dict:
    result = dict(data)
    for key, label, minimum, maximum in (
        ("interval", "检测间隔", 1, 86400),
        ("reconnect_timeout", "重连超时", 1, 3600),
        ("log_max_bytes", "日志大小", 1, 1073741824),
        ("log_backup_count", "日志备份数", 0, 100),
        ("heartbeat_interval", "心跳间隔", 1, 86400),
    ):
        try:
            value = int(str(data[key]))
        except (ValueError, TypeError, KeyError):
            raise ValueError(f"{label}必须是整数") from None
        if not minimum <= value <= maximum:
            raise ValueError(f"{label}须在 {minimum}–{maximum} 之间")
        result[key] = value
    for key in ("prevent_sleep", "cleanup_proxy_on_disconnect", "repair_dns_on_failure",
                "wired_recovery", "wired_reset_on_failure",
                "wifi_recovery", "wifi_radio_recovery", "adapter_reset_on_failure",
                "disable_adapter_power_saving"):
        if not isinstance(data.get(key, True), bool):
            raise ValueError(f"{key} 必须是 true 或 false")
        result[key] = data.get(key, True)
    data = dict(data, disabled_profiles=data.get("disabled_profiles", []), dns_reset_interfaces=data.get("dns_reset_interfaces", []))
    for key in ("check_targets", "preferred_profiles", "disabled_profiles", "dns_check_hosts", "dns_reset_interfaces"):
        if not isinstance(data[key], list) or any(not isinstance(x, str) for x in data[key]):
            raise ValueError(f"{key} 必须是字符串列表")
        result[key] = list(dict.fromkeys(x.strip() for x in data[key] if x.strip()))
    if not result["check_targets"]:
        raise ValueError("请至少填写一个 HTTP / HTTPS 检测地址")
    for target in result["check_targets"]:
        parsed = urlparse(target)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise ValueError(f"检测地址无效：{target}")
    if data.get("interface") is not None and not isinstance(data["interface"], str):
        raise ValueError("网卡名称必须是文字")
    interface_value = data.get("interface")
    if interface_value and guard.has_unsafe_name_chars(interface_value):
        raise ValueError("网卡名称含控制字符")
    for key in ("preferred_profiles", "disabled_profiles", "dns_reset_interfaces"):
        for item in result[key]:
            if guard.has_unsafe_name_chars(item):
                raise ValueError(f"{key} 含控制字符")
    channel = data.get("update_channel", "stable")
    if channel not in CHANNELS:
        raise ValueError("更新渠道必须是 stable、beta 或 alpha")
    result["update_channel"] = channel
    for key in ("log_file", "state_file", "pid_file"):
        if not isinstance(data[key], str) or not data[key].strip():
            raise ValueError(f"{key} 不能为空")
    enabled = data.get("enabled_profiles")
    if enabled is not None and (not isinstance(enabled, list) or any(not isinstance(x, str) for x in enabled)):
        raise ValueError("enabled_profiles 必须是字符串列表或 null")
    result["enabled_profiles"] = enabled
    return result


def initial_enabled_profiles(profiles: list[str], interfaces: list[dict], disabled: list[str]) -> list[str]:
    """Seed once from the actual connected profile, not a stale SSID."""
    connected = {(item.get("profile") or item.get("ssid") or "").casefold()
                 for item in interfaces if item.get("state", "").strip().casefold() in ("connected", "已连接")}
    excluded = {name.casefold() for name in disabled}
    return [name for name in profiles if name.casefold() in connected and name.casefold() not in excluded]


def save_settings(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def emit(kind: str, **data) -> None:
    print(json.dumps(dict(kind=kind, **data), ensure_ascii=False), flush=True)


class EventLog(logging.Handler):
    def emit(self, record):
        emit("log", text=self.format(record), level=record.levelname)


def worker(config_path: Path) -> None:
    """An isolated process keeps slow network calls off the UI thread."""
    # 与 CLI 共用同一把 Monitor 锁：同一时间只允许一个守护操作网络设置。
    if not guard.repairs.acquire_single_instance("Monitor"):
        emit("log", text="已有另一个守护实例在运行，本次未启动。", level="ERROR")
        emit("status", text="已有守护在运行 · 请先停止", tone="error")
        return
    guard.migrate_legacy_layout()
    config = validate_settings(read_settings(config_path))
    log_path = guard.resolve_path(config["log_file"])
    log_path.parent.mkdir(parents=True, exist_ok=True)
    state_path = guard.resolve_path(config["state_file"])
    state_path.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger(guard.APP_NAME)
    logger.setLevel(logging.DEBUG)
    logger.handlers.clear()
    logger.propagate = False
    # 必须在打开日志 handler 之前取样：崩溃回放只应包含上一次会话写入的内容，
    # 否则本次启动写入的日志会被误当成"崩溃前的最后几行"。
    startup_log_size = guard.repairs.log_size_before_startup(log_path)
    disk = RotatingFileHandler(log_path, maxBytes=config["log_max_bytes"],
                               backupCount=config["log_backup_count"], encoding="utf-8")
    live = EventLog()
    live.setLevel(logging.DEBUG if config["verbose"] else logging.INFO)
    for handler in (disk, live):
        handler.setFormatter(logging.Formatter("%(asctime)s | %(levelname)s | %(message)s", "%H:%M:%S"))
        logger.addHandler(handler)
    guard.ensure_windows(logger)
    pid_file = guard.resolve_path(config["pid_file"])
    with guard.repairs.monitor_session(logger, pid_file, log_path, config["prevent_sleep"], startup_log_size):
        _monitor_worker(config_path, config, logger)


def _monitor_worker(config_path, config, logger):
    state_path = guard.resolve_path(config["state_file"])
    attempts = successes = 0
    radio_alerted = False
    repair_stats = {"dns_repairs": 0, "proxy_cleanups": 0, "wired_repairs": 0,
                    "radio_repairs": 0, "radio_blocked": 0}
    last_heartbeat = time.monotonic()
    logger.info("守护已启动，检测间隔 %s 秒", config["interval"])
    logger.info("自动修复：代理清理 %s，DNS 修复 %s，有线修复 %s，Wi-Fi 重连 %s，无线电恢复 %s，网卡重置 %s，网卡节能 %s",
                "开" if config["cleanup_proxy_on_disconnect"] else "关",
                "开" if config["repair_dns_on_failure"] else "关",
                "开" if config["wired_recovery"] else "关",
                "开" if config["wifi_recovery"] else "关",
                "开" if config["wifi_radio_recovery"] else "关",
                "开" if config["adapter_reset_on_failure"] else "关",
                "关" if config.get("disable_adapter_power_saving", True) else "开")
    if config.get("disable_adapter_power_saving", True):
        guard.repairs.disable_adapter_power_saving(logger, config.get("interface"))
    while True:
        try:
            now = time.monotonic()
            if now - last_heartbeat >= config["heartbeat_interval"]:
                last_latency = guard.last_latency_ms()
                logger.info("心跳：守护运行中，恢复成功 %d/%d · 最近延迟 %s", successes, attempts,
                            f"{last_latency:.0f} ms" if last_latency is not None else "未知")
                for handler in logger.handlers:
                    handler.flush()
                last_heartbeat = now
            emit("status", text="正在检测", tone="busy")
            online = guard.is_internet_reachable(logger, config["check_targets"])
            if not online:
                # Reload only participation switches; other settings apply on restart.
                participation = validate_settings(read_settings(config_path))
                config["disabled_profiles"] = participation["disabled_profiles"]
                config["enabled_profiles"] = participation["enabled_profiles"] or []
                attempts += 1
                emit("status", text="正在修复网络", tone="busy")
                online = guard.recover_network(logger, config["interface"],
                    config["preferred_profiles"], state_path,
                    config["reconnect_timeout"], config["check_targets"],
                    disabled_profiles=config["disabled_profiles"],
                    enabled_profiles=config["enabled_profiles"],
                    cleanup_proxy=config["cleanup_proxy_on_disconnect"],
                    repair_dns_enabled=config["repair_dns_on_failure"],
                    dns_check_hosts=config["dns_check_hosts"],
                    wifi_recovery=config["wifi_recovery"],
                    adapter_reset=config["adapter_reset_on_failure"],
                    wired_recovery=config["wired_recovery"],
                    wired_reset=config["wired_reset_on_failure"] and config["adapter_reset_on_failure"],
                    dns_reset_interfaces=config["dns_reset_interfaces"], repair_stats=repair_stats,
                    radio_recovery=config["wifi_radio_recovery"])
                successes += int(online)
                if online:
                    radio_alerted = False
                elif repair_stats.get("radio_blocked") and not radio_alerted:
                    emit("status", text="无线被系统关闭且无可用有线 · 请手动打开 Wi-Fi", tone="error")
                    radio_alerted = True
            interfaces = guard.get_wifi_interfaces(logger, config["interface"])
            emit("result", online=online, attempts=attempts, successes=successes, **repair_stats,
                 interfaces=[vars(item) for item in interfaces], checked=time.strftime("%H:%M:%S"))
            latency_value = guard.last_latency_ms()
            if online:
                logger.info("本轮检测：网络可用 · 延迟 %s",
                            f"{latency_value:.0f} ms" if latency_value is not None else "未知")
            else:
                logger.info("本轮检测：网络不可用")
        except Exception:
            logger.exception("检测发生异常，将在下一轮重试")
            emit("status", text="检测异常 · 请查看日志", tone="error")
        before = time.time()
        time.sleep(config["interval"])
        if guard.repairs.detect_sleep(logger, config["interval"], time.time() - before):
            logger.info("系统休眠恢复，立即重新检测网络。")


def apply_window_icon(window) -> None:
    """Use the bundled Q-version icon for the window and taskbar."""
    try:
        icon = resource_path("assets/NetworkGuard.ico")
        if icon.is_file():
            window.iconbitmap(default=str(icon))
    except Exception:  # noqa: BLE001 - a missing icon must never block startup
        pass


class GuardApp:
    def __init__(self, root: tk.Tk, config_path: Path):
        self.root, self.config_path = root, config_path
        self.events = queue.Queue()
        self.process = None
        self.generation = 0
        self.stopping = False
        self.closing = False
        self.refreshing = False
        self.checking_updates = False
        self.config_error = None
        try:
            self.config = validate_settings(read_settings(config_path))
        except (ValueError, OSError, TypeError, KeyError) as exc:
            self.config_error = str(exc)
            self.config = read_settings(BASE / "__unused_defaults__.json")
        # 隐藏名单单独存在 config/hidden_wifi.txt（已被 .gitignore 排除），
        # 不再写进主配置；旧配置里的 hidden_profiles 自动迁移一次。
        self.hidden_file = hidden_profiles_path()
        legacy_hidden = self.config.get("hidden_profiles") or []
        if legacy_hidden:
            merged = load_hidden_profiles(self.hidden_file)
            seen = {name.casefold() for name in merged}
            for name in legacy_hidden:
                if name.casefold() not in seen:
                    merged.append(name)
                    seen.add(name.casefold())
            try:
                save_hidden_profiles(self.hidden_file, merged)
            except OSError:
                pass
            self.config.pop("hidden_profiles", None)
        self._profile_order: list[str] = []
        self._raw_names: dict[str, str] = {}
        self._sort_column: str | None = None
        self._sort_ascending = True
        root.title(f"Network Guard · 网络守护 {VERSION}")
        apply_window_icon(root)
        root.geometry("1040x760")
        root.configure(bg=BG)
        root.protocol("WM_DELETE_WINDOW", self.close)
        self.build_ui()
        self.load_fields()
        self.fit_window()
        # 首次映射后内容宽度才确定，此时再校正一次窗口尺寸，
        # 避免初始测量偏小导致底部被裁掉。
        def fit_after_map(event=None):
            if event is not None and event.widget is not self.root:
                return
            try:
                self.root.unbind("<Map>", self._map_binding)
            except Exception:  # noqa: BLE001
                pass
            self.fit_window()

        self._map_binding = self.root.bind("<Map>", fit_after_map, add="+")
        self.root.after(100, self.poll)
        if self.config_error:
            self.root.after(200, lambda: messagebox.showwarning("配置读取失败", self.config_error + "\n已载入默认值；保存将覆盖原配置。"))
        self.refresh()

    def _scrollable_tab(self, notebook, title, padding=12):
        """Add a tab whose body only scrolls when it really does not fit.

        The main window itself never scrolls; this keeps the long settings form
        (and the Wi-Fi list on small screens) usable without the easy-to-miss
        page-level scrollbar that used to hide content.
        """
        outer = ttk.Frame(notebook)
        canvas = tk.Canvas(outer, bg=BG, highlightthickness=0)
        bar = ttk.Scrollbar(outer, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=bar.set)
        canvas.pack(side="left", fill="both", expand=True)
        content = ttk.Frame(canvas, padding=padding)
        window = canvas.create_window((0, 0), window=content, anchor="nw")

        def sync(event=None):
            canvas.configure(scrollregion=canvas.bbox("all"))
            width = canvas.winfo_width()
            if width <= 50:
                # Hidden tab: lay the form out at a realistic width so the
                # window can still be sized from its requested height.
                width = max(content.winfo_reqwidth(), int(canvas.cget("width")))
            canvas.itemconfigure(window, width=width)
            wanted = content.winfo_reqheight()
            if int(canvas.cget("height")) != wanted:
                canvas.configure(height=wanted)
            height = canvas.winfo_height()
            overflow = height > 50 and content.winfo_reqheight() > height
            if overflow and not bar.winfo_ismapped():
                bar.pack(side="right", fill="y", before=canvas)
            elif not overflow and bar.winfo_ismapped():
                bar.pack_forget()

        def on_wheel(event):
            if not wheel_scrolls_container(event.widget):
                return None
            canvas.yview_scroll(-1 if event.delta > 0 else 1, "units")
            return "break"

        def bind_wheel(widget):
            widget.bind("<MouseWheel>", on_wheel)
            for child in widget.winfo_children():
                bind_wheel(child)

        content.bind("<Configure>", sync)
        canvas.bind("<Configure>", sync)
        notebook.add(outer, text=title)
        self.root.after_idle(sync)
        return content, bind_wheel, canvas

    def _checkbutton(self, parent, text, variable):
        """Native check mark instead of clam's X-shaped indicator.

        The clam theme draws a cross for a selected checkbutton, which reads
        like "off" or "error"; tk.Checkbutton renders a familiar tick.
        """
        return tk.Checkbutton(parent, text=text, variable=variable, bg=BG,
                              activebackground=BG, selectcolor="white",
                              font=("Microsoft YaHei UI", 10), anchor="w",
                              bd=0, highlightthickness=0)

    def build_ui(self):
        style = ttk.Style()
        style.theme_use("clam")
        style.configure("TFrame", background=BG)
        style.configure("TLabel", background=BG, foreground=INK, font=("Microsoft YaHei UI", 10))
        style.configure("TButton", font=("Microsoft YaHei UI", 10), padding=(15, 9))
        style.configure("Primary.TButton", background=BLUE, foreground="white")
        style.map("Primary.TButton", background=[("active", "#1d4ed8"), ("disabled", "#94a3b8")])
        style.configure("TEntry", padding=7, font=("Microsoft YaHei UI", 10))
        style.configure("TCheckbutton", font=("Microsoft YaHei UI", 10))
        style.configure("TNotebook.Tab", padding=(20, 10), font=("Microsoft YaHei UI", 10))
        line_height = tkfont.Font(family="Microsoft YaHei UI", size=10).metrics("linespace")
        style.configure("Treeview", rowheight=line_height + 16, font=("Microsoft YaHei UI", 10))
        style.configure("Treeview.Heading", font=("Microsoft YaHei UI", 10, "bold"))
        sidebar_width = tkfont.Font(family="Microsoft YaHei UI", size=18, weight="bold").measure("◉  CONNECT") + 48
        self.sidebar_width = sidebar_width
        sidebar = tk.Frame(self.root, bg="#11233f", width=sidebar_width)
        sidebar.pack(side="left", fill="y")
        sidebar.pack_propagate(False)
        for text, size, color in [("◉  CONNECT", 18, "#ffffff"), ("NETWORK GUARD", 10, "#8ca8d4"),
                                  ("网络守护控制台", 13, "#ffffff")]:
            tk.Label(sidebar, text=text, bg="#11233f", fg=color,
                     font=("Microsoft YaHei UI", size, "bold")).pack(anchor="w", padx=20, pady=(30 if size == 18 else 10, 0))
        tk.Label(sidebar, text="Windows Wi-Fi\n自动检测 · 断线恢复", justify="left", bg="#11233f",
                 fg="#9eb2d0", font=("Microsoft YaHei UI", 10), pady=25).pack(side="bottom", padx=20, anchor="w")
        self.update_button = ttk.Button(sidebar, text="检查 GitHub 更新", command=self.check_for_updates)
        self.update_button.pack(side="bottom", padx=20, pady=(0, 2), anchor="w")
        self.support_button = ttk.Button(sidebar, text="遇到问题", command=self.open_support)
        self.support_button.pack(side="bottom", padx=20, pady=(2, 8), anchor="w")
        tk.Label(sidebar, text=f"v{VERSION}", bg="#11233f", fg="#9eb2d0",
                 font=("Microsoft YaHei UI", 10)).pack(side="bottom", padx=20, anchor="w")
        # 主界面直接铺满窗口，不再用 Canvas 包住整页。
        # 先前整页可滚动，但滚动条很不显眼，用户会误以为看到的就是全部内容；
        # 现在按内容自适应窗口大小，只有日志区保留自己的滚动条。
        body = ttk.Frame(self.root, padding=(18, 14))
        body.pack(side="left", fill="both", expand=True)
        self.body = body

        header = ttk.Frame(body)
        header.pack(fill="x")
        header.columnconfigure(0, weight=1)
        titles = ttk.Frame(header)
        titles.grid(row=0, column=0, sticky="nsew")
        headline = ttk.Label(titles, text="让连接持续在线", font=("Microsoft YaHei UI", 19, "bold"))
        headline.pack(anchor="w")
        wrap_label(headline, titles, 24)
        subtitle = ttk.Label(titles, text="检测网络状态，断线时自动恢复已保存的 Wi-Fi。", foreground=MUTED)
        subtitle.pack(anchor="w", pady=(2, 0))
        wrap_label(subtitle, titles, 24)

        card = tk.Frame(header, bg="white", padx=16, pady=10)
        card.grid(row=0, column=1, sticky="ne", padx=(16, 0))
        self.status = tk.Label(card, text="●  守护未启动", bg="white", fg=MUTED,
                               font=("Microsoft YaHei UI", 15, "bold"))
        self.status.pack(anchor="w")
        self.network = tk.Label(card, text="正在读取网卡信息…", bg="white", fg=MUTED,
                                font=("Microsoft YaHei UI", 10), justify="left", wraplength=330)
        self.network.pack(anchor="w", pady=(6, 0))
        self.stats = tk.Label(card, text="最近检测 —    |    本次恢复成功 0 / 0", bg="white", fg=MUTED,
                              font=("Microsoft YaHei UI", 10), justify="left", wraplength=330)
        self.stats.pack(anchor="w", pady=(4, 0))
        actions = ttk.Frame(body)
        actions.pack(fill="x", pady=(10, 8))
        self.start_button = ttk.Button(actions, text="▶  启动守护", style="Primary.TButton", command=self.start)
        self.start_button.pack(side="left")
        self.stop_button = ttk.Button(actions, text="■  停止守护", command=self.stop, state="disabled")
        self.stop_button.pack(side="left", padx=8)
        self.refresh_button = ttk.Button(actions, text="刷新 Wi-Fi", command=self.refresh)
        self.refresh_button.pack(side="right")
        # 先占住底部提示条，窗口再矮也不会把「就绪」那行挤掉；
        # 页签内部自行滚动，整页仍然不滚动。
        self.notice = ttk.Label(body, text="就绪 · 点击启动后开始自动检测与恢复", foreground=MUTED)
        self.notice.pack(side="bottom", anchor="w", pady=(8, 0))
        wrap_label(self.notice, body, 24)
        notebook = self.notebook = ttk.Notebook(body)
        notebook.pack(fill="both", expand=True)
        networks, bind_networks_wheel, _ = self._scrollable_tab(notebook, "已保存的 Wi-Fi")
        settings, bind_settings_wheel, settings_canvas = self._scrollable_tab(notebook, "守护设置")
        logs = ttk.Frame(notebook, padding=10)
        notebook.add(logs, text="运行日志")
        hint = ttk.Label(networks, text="默认仅当前连接的 Wi-Fi 参与恢复。点击右侧开关或选中后按空格修改。", foreground=MUTED)
        hint.pack(anchor="w", pady=(0, 8))
        wrap_label(hint, networks, 30)
        table = ttk.Frame(networks)
        table.pack(fill="both", expand=True)
        self.profiles = ttk.Treeview(table, columns=("name", "priority", "enabled"), show="headings", selectmode="browse", height=5)
        self._heading_labels = {"name": "Wi-Fi 配置名称", "priority": "优先顺序", "enabled": "参与恢复（点击切换）"}
        for column, label in self._heading_labels.items():
            self.profiles.heading(column, text=label, command=lambda chosen=column: self.sort_by(chosen))
        measure = tkfont.Font(family="Microsoft YaHei UI", size=10, weight="bold").measure
        self.profiles.column("name", width=180, minwidth=100)
        self.profiles.column("priority", width=measure("优先顺序") + 24, minwidth=70, stretch=False, anchor="center")
        self.profiles.column("enabled", width=measure("参与恢复（点击切换）") + 24, stretch=False, anchor="center")
        table_y = ttk.Scrollbar(table, orient="vertical", command=self.profiles.yview)
        table_y.pack(side="right", fill="y")
        table_x = ttk.Scrollbar(table, orient="horizontal", command=self.profiles.xview)
        table_x.pack(side="bottom", fill="x")
        self.profiles.configure(yscrollcommand=table_y.set, xscrollcommand=table_x.set)
        self.profiles.pack(fill="both", expand=True)
        self.profiles.bind("<Button-1>", self.toggle_clicked)
        self.profiles.bind("<space>", self.toggle_selected)
        network_actions = ttk.Frame(networks)
        network_actions.pack(fill="x", pady=8)
        ttk.Button(network_actions, text="将所选 Wi-Fi 设为首选", command=self.prefer).pack(side="left")
        self.delete_button = ttk.Button(network_actions, text="从列表移除未选定的", command=self.hide_unselected)
        self.delete_button.pack(side="left", padx=8)
        self.restore_button = ttk.Button(network_actions, text="恢复已隐藏", command=self.restore_hidden, state="disabled")
        self.restore_button.pack(side="left")
        self.apply_order_button = ttk.Button(network_actions, text="按当前顺序设为优先", command=self.apply_list_order)
        self.apply_order_button.pack(side="left", padx=8)
        self.update_restore_button()
        self.profile_hint = ttk.Label(networks, text="正在读取…", foreground=MUTED)
        self.profile_hint.pack(anchor="w")
        wrap_label(self.profile_hint, networks, 30)
        recovery_hint = ttk.Label(networks, text="开关自动保存，从下一轮恢复生效。关闭的 Wi-Fi 即使是首选或上次成功连接，也会跳过。", foreground=MUTED)
        recovery_hint.pack(anchor="w", pady=(6, 0))
        wrap_label(recovery_hint, networks, 30)
        bind_networks_wheel(networks)
        settings.columnconfigure(1, weight=1)
        self.fields = {}
        for row, (key, label) in enumerate((("interval", "检测间隔（秒）"), ("reconnect_timeout", "重连超时（秒）"), ("interface", "网卡名称（留空自动）"))):
            ttk.Label(settings, text=label).grid(row=row, column=0, sticky="w", padx=(0, 16), pady=3)
            var = tk.StringVar()
            self.fields[key] = var
            ttk.Entry(settings, textvariable=var, font=("Microsoft YaHei UI", 10), width=16).grid(row=row, column=1, sticky="ew", pady=3)
        for row, key, label in [(3, "preferred_profiles", "首选配置（每行一个）"), (4, "check_targets", "检测地址（每行一个）")]:
            ttk.Label(settings, text=label).grid(row=row, column=0, sticky="nw", pady=3)
            widget = tk.Text(settings, height=2, width=24, font=("Microsoft YaHei UI", 10), relief="solid", bd=1)
            widget.grid(row=row, column=1, sticky="ew", pady=3)
            self.fields[key] = widget
        self.verbose = tk.BooleanVar()
        self.verbose_button = self._checkbutton(settings, "显示调试日志", self.verbose)
        self.verbose_button.grid(row=5, column=1, sticky="w")
        recovery = ttk.Frame(settings)
        recovery.grid(row=6, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        ttk.Label(recovery, text="断线自动修复（从轻到重依次尝试）",
                  font=("Microsoft YaHei UI", 10, "bold")).grid(
                      row=0, column=0, columnspan=2, sticky="w", pady=(0, 4))
        self.switches = {}
        switch_widgets = []
        for index, (key, text) in enumerate((
            ("cleanup_proxy_on_disconnect", "清理系统代理（代理工具失效导致的应用层断网）"),
            ("repair_dns_on_failure", "修复 DNS（清空缓存 + 网卡 DNS 回退自动获取）"),
            ("wired_recovery", "先修复有线连接（DHCP 续约，解决拿不到 IP 地址）"),
            ("wifi_recovery", "再重连已保存的 Wi-Fi（有线恢复不了才走这一步）"),
            ("wifi_radio_recovery", "无线被关闭时自动打开无线电（飞行模式/Wi-Fi 开关被关）"),
            ("disable_adapter_power_saving", "关闭网卡节能（取消“允许计算机关闭此设备”，需管理员）"),
            ("adapter_reset_on_failure", "全部失败后禁用/启用网卡（需管理员权限）"),
            ("prevent_sleep", "守护期间阻止系统休眠"),
        )):
            var = tk.BooleanVar()
            self.switches[key] = var
            row, column = divmod(index, 2)
            widget = self._checkbutton(recovery, text, var)
            widget.grid(row=1 + row, column=column, sticky="w", padx=(0, 18), pady=1)
            switch_widgets.append(widget)
        self.switch_buttons = switch_widgets
        recovery.columnconfigure(0, weight=1)
        recovery.columnconfigure(1, weight=1)
        # 两列布局所需宽度；窗口太窄时自动退回单列，避免文字被横向裁掉
        widest = max((widget.winfo_reqwidth() for widget in switch_widgets), default=0)
        if widest:
            settings_canvas.configure(width=widest * 2 + 56)
        switch_columns = [2]

        def layout_switches(width):
            columns = 2 if widest and width >= widest * 2 + 56 else 1
            if columns == switch_columns[0]:
                return
            switch_columns[0] = columns
            for index, widget in enumerate(switch_widgets):
                row, column = divmod(index, columns)
                widget.grid_configure(row=1 + row, column=column)
            recovery.columnconfigure(1, weight=2 if columns == 2 else 0)

        def relayout_switches(event=None):
            width = settings_canvas.winfo_width()
            if width > 50:
                layout_switches(width)

        settings_canvas.bind("<Configure>", relayout_switches, add="+")
        update_row = ttk.Frame(settings)
        update_row.grid(row=7, column=0, columnspan=2, sticky="w", pady=(6, 0))
        ttk.Label(update_row, text="检查更新:").pack(side="left")
        self.update_channel = tk.StringVar()
        self.update_channel_combo = ttk.Combobox(
            update_row, textvariable=self.update_channel, state="readonly",
            values=tuple(CHANNEL_LABELS.values()), width=8)
        self.update_channel_combo.pack(side="left", padx=(6, 0))
        ttk.Label(update_row, text="（内测版含 alpha/beta/rc）", foreground=MUTED).pack(side="left", padx=(6, 0))
        ttk.Button(settings, text="保存设置", command=self.save).grid(row=8, column=1, sticky="e", pady=6)
        bind_settings_wheel(settings)
        toolbar = ttk.Frame(logs)
        toolbar.pack(fill="x", pady=(0, 8))
        ttk.Label(toolbar, text="实时输出 · 最多显示 1500 行", foreground=MUTED).pack(side="left")
        ttk.Button(toolbar, text="清空显示", command=self.clear_logs).pack(side="right")
        ttk.Button(toolbar, text="延迟折线图", command=self.show_latency_chart).pack(side="right", padx=(0, 8))
        self.log_text = tk.Text(logs, bg="#102139", fg="#d6e2f3", font=("Consolas", 10),
                                width=24, height=10, wrap="word", relief="flat", state="disabled")
        scroll = ttk.Scrollbar(logs, command=self.log_text.yview)
        scroll.pack(side="right", fill="y")
        self.log_text.configure(yscrollcommand=scroll.set)
        self.log_text.pack(fill="both", expand=True)
        self.log_text.tag_configure("ERROR", foreground="#fda4af")
        self.log_text.tag_configure("WARNING", foreground="#fcd34d")

    def fit_window(self):
        """Size the window to its content; never shrink below it.

        The old design wrapped the whole page in a Canvas whose scrollbars were
        easy to miss, so users assumed the visible part was everything. The
        window now grows to fit the content, and only the log view keeps an
        internal scrollbar.
        """
        self.root.update_idletasks()
        needed_width = self.sidebar_width + self.body.winfo_reqwidth()
        needed_height = self.body.winfo_reqheight() + 8
        screen_width = self.root.winfo_screenwidth()
        screen_height = self.root.winfo_screenheight()
        max_width = max(880, screen_width - 40)
        max_height = max(640, screen_height - 80)
        width = min(max(needed_width, 1040), max_width)
        height = min(max(needed_height, 720), max_height)
        self.root.geometry(f"{width}x{height}")
        self.root.minsize(min(needed_width, max_width), min(needed_height, max_height))

    def load_fields(self):
        for key, widget in self.fields.items():
            if isinstance(widget, tk.StringVar):
                widget.set(str(self.config.get(key) or ""))
            else:
                widget.delete("1.0", "end")
                widget.insert("1.0", "\n".join(self.config[key]))
        self.verbose.set(self.config["verbose"])
        for key, var in self.switches.items():
            var.set(bool(self.config[key]))
        self.update_channel.set(CHANNEL_LABELS.get(self.config.get("update_channel", "stable"), "稳定版"))

    def save(self):
        data = dict(self.config)
        for key, widget in self.fields.items():
            data[key] = widget.get().strip() if isinstance(widget, tk.StringVar) else widget.get("1.0", "end").splitlines()
        data["interface"] = data["interface"] or None
        data["verbose"] = self.verbose.get()
        for key, var in self.switches.items():
            data[key] = bool(var.get())
        data["update_channel"] = UPDATE_LABEL_TO_CHANNEL.get(self.update_channel.get(), "stable")
        try:
            data = validate_settings(data)
            save_settings(self.config_path, data)
        except (ValueError, OSError) as exc:
            messagebox.showerror("无法保存设置", str(exc), parent=self.root)
            return False
        self.config = data
        self.notice.configure(text="设置已保存" + (" · 重启守护后生效" if self.process else ""))
        self.update_priorities()
        return True

    def prefer(self):
        selection = self.profiles.selection()
        if not selection:
            self.notice.configure(text="请先选择一个 Wi-Fi 配置")
            return
        name = self._name_of(selection[0])
        widget = self.fields["preferred_profiles"]
        existing = [x.strip() for x in widget.get("1.0", "end").splitlines() if x.strip() and x.strip().casefold() != name.casefold()]
        widget.delete("1.0", "end")
        widget.insert("1.0", "\n".join([name] + existing))
        self.save()

    def hide_unselected(self):
        """Remove unchecked networks from this app's list only.

        Windows keeps the saved profile and its password; the name lands in
        ``hidden_profiles`` so it stays hidden after a refresh and can be
        brought back with the restore button.
        """
        enabled = {name.casefold() for name in (self.config.get("enabled_profiles") or [])}
        if not enabled:
            self.notice.configure(text="请先勾选要保留的 Wi-Fi，再从列表移除其余项")
            return
        names = [self._name_of(item) for item in self.profiles.get_children()]
        targets = [name for name in names if name.casefold() not in enabled]
        if not targets:
            self.notice.configure(text="列表中没有未选定的 Wi-Fi")
            return
        preview = "、".join(targets[:6]) + ("…" if len(targets) > 6 else "")
        if not messagebox.askyesno(
            "从列表移除未选定的 Wi-Fi",
            f"将从本软件的列表中移除以下 {len(targets)} 个未选定的 Wi-Fi：\n\n{preview}\n\n"
            "不会删除 Windows 中保存的配置，密码也不会丢失；之后可用「恢复已隐藏」找回。",
            parent=self.root,
        ):
            return

        hidden = load_hidden_profiles(self.hidden_file)
        existing = {name.casefold() for name in hidden}
        for name in targets:
            if name.casefold() not in existing:
                hidden.append(name)
                existing.add(name.casefold())
        target_keys = {name.casefold() for name in targets}
        data = dict(
            self.config,
            preferred_profiles=[name for name in self.config.get("preferred_profiles", [])
                                if name.casefold() not in target_keys],
            disabled_profiles=[name for name in self.config.get("disabled_profiles", [])
                               if name.casefold() not in target_keys],
        )
        try:
            save_hidden_profiles(self.hidden_file, hidden)
            save_settings(self.config_path, data)
        except OSError as exc:
            self.notice.configure(text="移除失败，无法保存设置：" + str(exc))
            return
        self.config = data
        self._profile_order = [name for name in self._profile_order if name.casefold() not in target_keys]
        for item in self.profiles.get_children():
            if self._name_of(item).casefold() in target_keys:
                self.profiles.delete(item)
        self.update_priorities()
        self.update_restore_button()
        self.notice.configure(text=f"已从列表移除 {len(targets)} 个未选定的 Wi-Fi（Windows 中仍保留）")

    def restore_hidden(self):
        hidden = load_hidden_profiles(self.hidden_file)
        if not hidden:
            return
        try:
            save_hidden_profiles(self.hidden_file, [])
        except OSError as exc:
            self.notice.configure(text="恢复失败，无法保存隐藏名单：" + str(exc))
            return
        self.update_restore_button()
        self.notice.configure(text=f"已恢复 {len(hidden)} 个隐藏的 Wi-Fi，正在刷新列表…")
        self.refresh()

    def update_restore_button(self):
        hidden = load_hidden_profiles(self.hidden_file)
        if hidden:
            self.restore_button.configure(text=f"恢复已隐藏（{len(hidden)}）", state="normal")
        else:
            self.restore_button.configure(text="恢复已隐藏", state="disabled")

    def _name_of(self, item: str) -> str:
        """Raw profile name for a row (display text may be sanitized)."""
        return self._raw_names.get(item, self.profiles.item(item, "values")[0])

    def priority_order(self) -> list[str]:
        """Effective recovery order: preferred first, then the saved order."""
        names = self._profile_order or [
            self._name_of(item) for item in self.profiles.get_children()
        ]
        known = {name.casefold(): name for name in names}
        preferred = [name for name in self.config.get("preferred_profiles", [])
                     if name.casefold() in known]
        preferred_keys = {name.casefold() for name in preferred}
        return preferred + [name for name in names if name.casefold() not in preferred_keys]

    def update_priorities(self):
        disabled = {x.casefold() for x in self.config.get("disabled_profiles", [])}
        enabled = {x.casefold() for x in (self.config.get("enabled_profiles") or [])}
        rank = {name.casefold(): index + 1 for index, name in enumerate(self.priority_order())}
        for item in self.profiles.get_children():
            name = self._name_of(item)
            priority = rank.get(name.casefold())
            self.profiles.item(item, values=(
                guard.display_name(name),
                str(priority) if priority else "—",
                "☑ 使用" if name.casefold() in enabled and name.casefold() not in disabled else "☐ 不使用",
            ))

    def sort_by(self, column: str):
        """Sort the list by a column; clicking the same header flips direction."""
        if self._sort_column == column:
            self._sort_ascending = not self._sort_ascending
        else:
            self._sort_column = column
            self._sort_ascending = True
        self._apply_sort()

    def _apply_sort(self):
        if not self._sort_column:
            return
        rows = [(self._name_of(item), list(self.profiles.item(item, "values")))
                for item in self.profiles.get_children()]
        if self._sort_column == "priority":
            def key(pair):
                return int(pair[1][1]) if str(pair[1][1]).isdigit() else 10 ** 6
        elif self._sort_column == "enabled":
            def key(pair):
                return 0 if str(pair[1][2]).startswith("☑") else 1
        else:
            def key(pair):
                return str(pair[1][0]).casefold()
        rows.sort(key=key, reverse=not self._sort_ascending)
        self.profiles.delete(*self.profiles.get_children())
        self._raw_names.clear()
        for name, values in rows:
            item = self.profiles.insert("", "end", values=values)
            self._raw_names[item] = name
        self._update_sort_arrows()

    def _update_sort_arrows(self):
        for column, label in self._heading_labels.items():
            if column == self._sort_column:
                label += " ↑" if self._sort_ascending else " ↓"
            self.profiles.heading(column, text=label)

    def apply_list_order(self):
        """Persist the current (possibly sorted) row order as priority order."""
        order = [self._name_of(item) for item in self.profiles.get_children()]
        if not order:
            self.notice.configure(text="列表为空，无法设置优先顺序")
            return
        data = dict(self.config, preferred_profiles=order)
        try:
            save_settings(self.config_path, data)
        except OSError as exc:
            self.notice.configure(text="保存优先顺序失败：" + str(exc))
            return
        self.config = data
        self.update_priorities()
        self.notice.configure(text=f"已按当前列表顺序更新优先顺序（共 {len(order)} 个）")

    def toggle_clicked(self, event):
        item = self.profiles.identify_row(event.y)
        if item and self.profiles.identify_column(event.x) == "#3":
            self.profiles.selection_set(item)
            self.profiles.focus(item)
            self.toggle_profile(item)
            return "break"

    def toggle_selected(self, event=None):
        selected = self.profiles.selection()
        if selected:
            self.toggle_profile(selected[0])
        return "break"

    def toggle_profile(self, item):
        name = self._name_of(item)
        disabled = list(self.config["disabled_profiles"])
        enabled = list(self.config["enabled_profiles"] or [])
        was_disabled = name.casefold() not in {x.casefold() for x in enabled} or name.casefold() in {x.casefold() for x in disabled}
        disabled = [x for x in disabled if x.casefold() != name.casefold()] if was_disabled else disabled + [name]
        enabled = [x for x in enabled if x.casefold() != name.casefold()]
        if was_disabled:
            enabled.append(name)
        data = dict(self.config, disabled_profiles=disabled, enabled_profiles=enabled)
        try:
            save_settings(self.config_path, data)
        except OSError as exc:
            messagebox.showerror("无法保存 Wi-Fi 开关", str(exc), parent=self.root)
            return
        self.config = data
        self.update_priorities()
        self.notice.configure(text=f"已{'允许' if was_disabled else '禁止'}“{name}”参与恢复 · 下一轮恢复生效")

    def refresh(self):
        if self.refreshing:
            return
        self.refreshing = True
        self.refresh_button.configure(state="disabled")
        self.profile_hint.configure(text="正在读取已保存的配置…")
        def task():
            logger = logging.getLogger("gui.inspect")
            logger.addHandler(logging.NullHandler())
            try:
                profiles = guard.get_saved_profiles(logger)
                interfaces = guard.get_wifi_interfaces(logger, None)
                self.events.put((None, dict(kind="inventory", profiles=profiles, interfaces=[vars(x) for x in interfaces])))
            except Exception as exc:
                self.events.put((None, dict(kind="inventory_error", text=str(exc))))
        threading.Thread(target=task, daemon=True).start()

    def start(self):
        if self.config["enabled_profiles"] is None:
            self.notice.configure(text="请先等待 Wi-Fi 列表读取完成；读取失败时请点击刷新")
            return
        if self.process or not self.save():
            return
        self.generation += 1
        generation = self.generation
        event_path = None
        try:
            command = worker_command(self.config_path)
            if FROZEN or sys.stdout is None:
                with tempfile.NamedTemporaryFile(prefix="network-guard-", suffix=".jsonl", delete=False) as stream:
                    event_path = Path(stream.name)
                command += ["--event-file", str(event_path)]
            self.process = subprocess.Popen(command,
                cwd=BASE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                encoding="utf-8", errors="replace", env=dict(os.environ, PYTHONIOENCODING="utf-8"),
                creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0)
        except OSError as exc:
            if event_path:
                event_path.unlink(missing_ok=True)
            messagebox.showerror("无法启动", str(exc), parent=self.root)
            return
        process = self.process
        self.worker_pid_file = guard.resolve_path(self.config["pid_file"])
        self.start_button.configure(state="disabled")
        self.stop_button.configure(state="normal")
        self.set_status("正在启动", "busy")
        self.stats.configure(text="最近检测 —    |    本次恢复成功 0 / 0")
        def read_output():
            source = event_path.open(encoding="utf-8", errors="replace") if event_path else process.stdout
            pending = ""
            try:
                while True:
                    line = source.readline()
                    if not line:
                        if event_path and process.poll() is None:
                            time.sleep(0.1)
                            continue
                        break
                    if event_path:
                        pending += line
                        if not pending.endswith("\n") and process.poll() is None:
                            continue
                        line, pending = pending, ""
                    try:
                        event = json.loads(line)
                    except ValueError:
                        event = dict(kind="log", text=line.rstrip(), level="ERROR")
                    self.events.put((generation, event))
            finally:
                source.close()
                if event_path:
                    process.wait()
                    process.stdout.close()
                    event_path.unlink(missing_ok=True)
            self.events.put((generation, dict(kind="exit", code=process.wait())))
        threading.Thread(target=read_output, daemon=True).start()

    def check_for_updates(self):
        if self.checking_updates:
            return
        self.checking_updates = True
        self.update_button.configure(state="disabled", text="正在检查…")
        def task():
            try:
                channel = UPDATE_LABEL_TO_CHANNEL.get(self.update_channel.get(), "stable")
                self.events.put((None, dict(kind="update", result=check_updates(channel=channel))))
            except Exception as exc:
                self.events.put((None, dict(kind="update", error=str(exc))))
        threading.Thread(target=task, daemon=True).start()

    def stop(self):
        if self.process and not self.stopping:
            self.stopping = True
            self.stop_button.configure(state="disabled")
            self.set_status("正在停止", "busy")
            try:
                self.process.terminate()
            except OSError:
                pass

    def close(self):
        self.closing = True
        if self.process:
            self.stop()
        else:
            self.root.destroy()

    def set_status(self, text, tone):
        self.status.configure(text="●  " + text, fg={"ok": "#15803d", "error": "#dc2626", "busy": BLUE}.get(tone, MUTED))

    def show_interfaces(self, interfaces):
        self.network.configure(text="  /  ".join(f"{x['name']} · {x.get('ssid') or x['state']} · 信号 {x.get('signal') or '—'}" for x in interfaces) or "未识别到可用 Wi-Fi 网卡 · 请检查无线开关或系统权限")

    def append_log(self, text, level="INFO"):
        bottom = self.log_text.yview()[1] >= 0.99
        self.log_text.configure(state="normal")
        self.log_text.insert("end", text + "\n", level)
        lines = int(self.log_text.index("end-1c").split(".")[0])
        if lines > 1500:
            self.log_text.delete("1.0", f"{lines - 1500 + 1}.0")
        self.log_text.configure(state="disabled")
        if bottom:
            self.log_text.see("end")

    def clear_logs(self):
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")

    def show_latency_chart(self):
        """Generate the latency SVG chart and open it in the default viewer."""
        try:
            chart = guard.latency.write_chart()
        except OSError as exc:
            self.notice.configure(text=f"生成延迟折线图失败：{exc}")
            return
        points, failures = guard.latency.read_series()
        self.notice.configure(
            text=f"延迟折线图已生成：{chart.name}（成功 {len(points)} 次，失败 {len(failures)} 次）"
        )
        try:
            os.startfile(chart)  # type: ignore[attr-defined]
        except OSError:
            pass

    def open_support(self):
        """Open the problem-report dialog: bundle logs or open the log folder."""
        SupportDialog(self.root, self.config_path)

    def poll(self):
        for _ in range(200):
            try:
                generation, event = self.events.get_nowait()
            except queue.Empty:
                break
            if generation is not None and generation != self.generation:
                continue
            kind = event["kind"]
            if kind == "update":
                self.checking_updates = False
                self.update_button.configure(state="normal", text="检查 GitHub 更新")
                if "error" in event:
                    messagebox.showwarning("检查更新失败", event["error"], parent=self.root)
                elif event["result"]["available"]:
                    result = event["result"]
                    channel = CHANNEL_LABELS.get(result.get("channel", "stable"), "稳定版")
                    if messagebox.askyesno("发现新版本", f"当前版本：{VERSION}\n新版本：{result['latest']}（{channel}）\n\n是否打开 GitHub 发布页下载？", parent=self.root):
                        webbrowser.open(result["url"])
                else:
                    text = "仓库尚未发布可识别的版本。" if event["result"]["latest"] is None else "当前已是最新版本。"
                    messagebox.showinfo("检查更新", f"当前版本：{VERSION}\n{text}", parent=self.root)
            elif kind == "log":
                self.append_log(event["text"], event.get("level", "INFO"))
            elif kind in ("inventory", "inventory_error"):
                self.refreshing = False
                self.refresh_button.configure(state="normal")
                if kind == "inventory_error":
                    self.profile_hint.configure(text="读取失败：" + event["text"])
                else:
                    hidden = {name.casefold() for name in load_hidden_profiles(self.hidden_file)}
                    visible = [name for name in event["profiles"] if name.casefold() not in hidden]
                    self._profile_order = list(visible)
                    if self.config["enabled_profiles"] is None:
                        enabled = initial_enabled_profiles(visible, event["interfaces"], self.config["disabled_profiles"])
                        data = dict(self.config, enabled_profiles=enabled)
                        try:
                            if not self.config_error:
                                save_settings(self.config_path, data)
                        except OSError as exc:
                            self.profile_hint.configure(text="无法保存默认 Wi-Fi 开关：" + str(exc))
                            continue
                        self.config = data
                    self.profiles.delete(*self.profiles.get_children())
                    self._raw_names.clear()
                    for name in visible:
                        item = self.profiles.insert("", "end", values=(guard.display_name(name), "—"))
                        self._raw_names[item] = name
                    name_font = tkfont.Font(family="Microsoft YaHei UI", size=10)
                    name_width = max([180] + [name_font.measure(name) + 24 for name in visible])
                    self.profiles.column("name", width=name_width, minwidth=name_width)
                    self.update_priorities()
                    self._apply_sort()
                    if event["profiles"]:
                        hint = f"共 {len(visible)} 个已保存配置"
                        if len(visible) < len(event["profiles"]):
                            hint += f"（已从列表隐藏 {len(event['profiles']) - len(visible)} 个）"
                        self.profile_hint.configure(text=hint)
                    else:
                        self.profile_hint.configure(text="未读取到配置；请先在 Windows 中连接 Wi-Fi，并检查 WLAN 服务及权限。")
                    self.update_restore_button()
                    self.show_interfaces(event["interfaces"])
            elif kind == "status" and not self.stopping:
                self.set_status(event["text"], event["tone"])
            elif kind == "result" and not self.stopping:
                self.set_status("网络可用 · 守护中" if event["online"] else "网络不可用 · 等待重试", "ok" if event["online"] else "error")
                self.show_interfaces(event["interfaces"])
                self.stats.configure(text=f"最近检测 {event['checked']}    |    本次恢复成功 {event['successes']} / {event['attempts']}")
                self.stats.configure(text=self.stats.cget("text") + f"    |    DNS 修复 {event.get('dns_repairs', 0)} · 代理恢复 {event.get('proxy_cleanups', 0)}")
            elif kind == "exit":
                stopped = self.stopping
                if stopped and self.process:
                    guard.repairs.remove_owned_pid_file(self.worker_pid_file, self.process.pid)
                self.process = None
                self.stopping = False
                self.start_button.configure(state="normal")
                self.stop_button.configure(state="disabled")
                self.set_status("守护已停止" if stopped else "守护已退出 · 请查看日志", "idle" if stopped else "error")
                self.append_log("守护已停止。" if stopped else f"守护进程退出，返回码 {event['code']}")
                if self.closing:
                    self.root.destroy()
                    return
        self.root.after(100, self.poll)


class SupportDialog(tk.Toplevel):
    """问题反馈窗口：预览压缩包名、打包日志、打开日志目录。"""

    def __init__(self, parent, config_path: Path):
        super().__init__(parent)
        self.title("遇到问题 · 日志反馈")
        self.configure(bg=BG)
        self.resizable(False, False)
        self.transient(parent)
        try:
            config = read_settings(config_path)
            self.log_path = guard.resolve_path(config["log_file"])
        except Exception:  # noqa: BLE001
            self.log_path = guard.resolve_path(guard.LOG_SUBPATH)
        self.proposed: Path | None = None
        self._build()
        self.refresh_proposal()
        self.update_idletasks()
        self._center(parent)
        self.grab_set()

    def _center(self, parent) -> None:
        x = parent.winfo_rootx() + (parent.winfo_width() - self.winfo_reqwidth()) // 2
        y = parent.winfo_rooty() + (parent.winfo_height() - self.winfo_reqheight()) // 3
        self.geometry(f"+{max(x, 0)}+{max(y, 0)}")

    def _build(self) -> None:
        box = ttk.Frame(self, padding=20)
        box.pack(fill="both", expand=True)
        ttk.Label(box, text="遇到问题？可以把日志打包发来排查。",
                  font=("Microsoft YaHei UI", 11, "bold")).pack(anchor="w")
        self.state_label = ttk.Label(box, text="", foreground=MUTED, justify="left")
        self.state_label.pack(anchor="w", pady=(12, 0))
        self.mail_label = ttk.Label(box, text="", foreground=MUTED, justify="left")
        self.mail_label.pack(anchor="w", pady=(6, 0))
        buttons = ttk.Frame(box)
        buttons.pack(fill="x", pady=(18, 0))
        ttk.Button(buttons, text="打包日志", command=self.do_export).pack(side="left")
        ttk.Button(buttons, text="打开日志文件夹", command=self.open_folder).pack(side="left", padx=(8, 0))
        ttk.Button(buttons, text="关闭", command=self.destroy).pack(side="right")
        wrap_label(self.state_label, box, 36)
        wrap_label(self.mail_label, box, 36)

    def refresh_proposal(self) -> None:
        """Recompute the archive name so the text can name the real file."""
        self.proposed = guard.repairs.proposed_archive_path(self.log_path)
        name = self.proposed.name
        self.state_label.configure(text=f"打包日志将日志打包为 {name}")
        self.mail_label.configure(
            text=f"请将生成的 {name} 发送到 {guard.repairs.SUPPORT_EMAIL}，并描述具体情况。"
        )

    def do_export(self) -> None:
        try:
            archive = guard.repairs.export_logs(
                self.log_path, VERSION, archive_path=self.proposed
            )
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("打包失败", str(exc), parent=self)
            return
        if archive is None:
            messagebox.showinfo(
                "打包日志",
                "还没有生成日志文件。请先启动守护运行一段时间再打包。",
                parent=self,
            )
            return
        name = archive.name
        self.state_label.configure(text=f"已打包为 {name}")
        self.mail_label.configure(
            text=f"请将生成的 {name} 发送到 {guard.repairs.SUPPORT_EMAIL}，并描述具体情况。"
        )
        # 下一次点击重新计算名字，避免覆盖刚生成的包。
        self.proposed = None

    def open_folder(self) -> None:
        folder = self.log_path.parent
        try:
            folder.mkdir(parents=True, exist_ok=True)
            if self.log_path.exists():
                subprocess.run(["explorer", f"/select,{self.log_path}"], check=False)
            else:
                subprocess.run(["explorer", str(folder)], check=False)
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("打开失败", str(exc), parent=self)


def main():
    guard.migrate_legacy_layout()
    guard.clear_internet_marks_async()
    parser = argparse.ArgumentParser(description="Network Guard 图形界面")
    parser.add_argument("--config", type=Path, default=guard.DEFAULT_CONFIG_FILE)
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--event-file", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--smoke-test", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    args.config = args.config.resolve()
    if args.worker:
        if args.event_file:
            with args.event_file.open("a", encoding="utf-8", buffering=1) as stream:
                sys.stdout = sys.stderr = stream
                try:
                    worker(args.config)
                except Exception:
                    traceback.print_exc()
                    raise SystemExit(1)
        else:
            worker(args.config)
        return
    if sys.platform == "win32":
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    root = tk.Tk()
    # 只允许打开一个界面窗口，避免两个窗口各起一个守护互相打架。
    if not args.smoke_test and not guard.repairs.acquire_single_instance("Gui"):
        root.withdraw()
        from tkinter import messagebox
        messagebox.showwarning("Network Guard", "程序已在运行，请勿重复打开。")
        root.destroy()
        return
    if args.smoke_test:
        # Offline packaged smoke test: construct every tab, without netsh/network.
        from unittest.mock import patch
        with patch.object(GuardApp, "refresh"):
            app = GuardApp(root, args.config)
        for tab in app.notebook.tabs():
            app.notebook.select(tab)
            root.update()
        args.smoke_test.write_text(json.dumps(dict(version=VERSION, frozen=FROZEN, tabs=len(app.notebook.tabs()))), encoding="utf-8")
        root.destroy()
        return
    GuardApp(root, args.config)
    root.mainloop()


if __name__ == "__main__":
    main()

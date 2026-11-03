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
import tkinter as tk
from tkinter import messagebox, ttk
from tkinter import font as tkfont
from urllib.parse import urlparse

import main as guard

BASE = Path(__file__).resolve().parent
BG = "#f3f6fb"
INK = "#172b4d"
MUTED = "#64748b"
BLUE = "#2563eb"


def read_settings(path: Path) -> dict:
    defaults = dict(interval=10, reconnect_timeout=35,
                    check_targets=list(guard.DEFAULT_CHECK_TARGETS),
                    preferred_profiles=[], disabled_profiles=[], enabled_profiles=None, interface=None, verbose=False,
                    log_file=str(guard.DEFAULT_LOG_FILE),
                    state_file=str(guard.DEFAULT_STATE_FILE),
                    log_max_bytes=2097152, log_backup_count=5)
    if path.exists():
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        if not isinstance(data, dict):
            raise ValueError("配置文件必须是 JSON 对象")
        defaults.update(data)
    return defaults


def validate_settings(data: dict) -> dict:
    result = dict(data)
    for key, label, minimum, maximum in (
        ("interval", "检测间隔", 1, 86400),
        ("reconnect_timeout", "重连超时", 1, 3600),
        ("log_max_bytes", "日志大小", 1, 1073741824),
        ("log_backup_count", "日志备份数", 0, 100),
    ):
        try:
            value = int(str(data[key]))
        except (ValueError, TypeError, KeyError):
            raise ValueError(f"{label}必须是整数") from None
        if not minimum <= value <= maximum:
            raise ValueError(f"{label}须在 {minimum}–{maximum} 之间")
        result[key] = value
    data = dict(data, disabled_profiles=data.get("disabled_profiles", []))
    for key in ("check_targets", "preferred_profiles", "disabled_profiles"):
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
    for key in ("log_file", "state_file"):
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
    config = validate_settings(read_settings(config_path))
    log_path = Path(config["log_file"])
    log_path.parent.mkdir(parents=True, exist_ok=True)
    state_path = Path(config["state_file"])
    state_path.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger(guard.APP_NAME)
    logger.setLevel(logging.DEBUG)
    logger.handlers.clear()
    logger.propagate = False
    disk = RotatingFileHandler(log_path, maxBytes=config["log_max_bytes"],
                               backupCount=config["log_backup_count"], encoding="utf-8")
    live = EventLog()
    live.setLevel(logging.DEBUG if config["verbose"] else logging.INFO)
    for handler in (disk, live):
        handler.setFormatter(logging.Formatter("%(asctime)s | %(levelname)s | %(message)s", "%H:%M:%S"))
        logger.addHandler(handler)
    guard.ensure_windows(logger)
    attempts = successes = 0
    logger.info("守护已启动，检测间隔 %s 秒", config["interval"])
    while True:
        try:
            emit("status", text="正在检测", tone="busy")
            online = guard.is_internet_reachable(logger, config["check_targets"])
            if not online:
                # Reload only participation switches; other settings apply on restart.
                participation = validate_settings(read_settings(config_path))
                config["disabled_profiles"] = participation["disabled_profiles"]
                config["enabled_profiles"] = participation["enabled_profiles"] or []
                attempts += 1
                emit("status", text="正在恢复网络", tone="busy")
                online = guard.recover_network(logger, config["interface"],
                    config["preferred_profiles"], state_path,
                    config["reconnect_timeout"], config["check_targets"],
                    disabled_profiles=config["disabled_profiles"], enabled_profiles=config["enabled_profiles"])
                successes += int(online)
            interfaces = guard.get_wifi_interfaces(logger, config["interface"])
            emit("result", online=online, attempts=attempts, successes=successes,
                 interfaces=[vars(item) for item in interfaces], checked=time.strftime("%H:%M:%S"))
            logger.info("本轮检测：%s", "网络可用" if online else "网络不可用")
        except Exception:
            logger.exception("检测发生异常，将在下一轮重试")
            emit("status", text="检测异常 · 请查看日志", tone="error")
        time.sleep(config["interval"])


class GuardApp:
    def __init__(self, root: tk.Tk, config_path: Path):
        self.root, self.config_path = root, config_path
        self.events = queue.Queue()
        self.process = None
        self.generation = 0
        self.stopping = False
        self.closing = False
        self.refreshing = False
        self.config_error = None
        try:
            self.config = validate_settings(read_settings(config_path))
        except (ValueError, OSError, TypeError, KeyError) as exc:
            self.config_error = str(exc)
            self.config = read_settings(BASE / "__unused_defaults__.json")
        root.title("Network Guard · 网络守护")
        root.geometry("1080x780")
        root.minsize(900, 700)
        root.configure(bg=BG)
        root.protocol("WM_DELETE_WINDOW", self.close)
        self.build_ui()
        self.load_fields()
        self.root.after(100, self.poll)
        if self.config_error:
            self.root.after(200, lambda: messagebox.showwarning("配置读取失败", self.config_error + "\n已载入默认值；保存将覆盖原配置。"))
        self.refresh()

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
        sidebar = tk.Frame(self.root, bg="#11233f", width=sidebar_width)
        sidebar.pack(side="left", fill="y")
        sidebar.pack_propagate(False)
        for text, size, color in [("◉  CONNECT", 18, "#ffffff"), ("NETWORK GUARD", 10, "#8ca8d4"),
                                  ("网络守护控制台", 13, "#ffffff")]:
            tk.Label(sidebar, text=text, bg="#11233f", fg=color,
                     font=("Microsoft YaHei UI", size, "bold")).pack(anchor="w", padx=20, pady=(30 if size == 18 else 10, 0))
        tk.Label(sidebar, text="Windows Wi-Fi\n自动检测 · 断线恢复", justify="left", bg="#11233f",
                 fg="#9eb2d0", font=("Microsoft YaHei UI", 10), pady=25).pack(side="bottom", padx=20, anchor="w")
        # A scrollable content area preserves full widget heights at high DPI
        # and in small windows instead of letting pack clip the bottom controls.
        viewport = ttk.Frame(self.root)
        viewport.pack(side="left", fill="both", expand=True)
        self.content_canvas = tk.Canvas(viewport, bg=BG, highlightthickness=0)
        content_scroll = ttk.Scrollbar(viewport, orient="vertical", command=self.content_canvas.yview)
        content_scroll.pack(side="right", fill="y")
        content_x = ttk.Scrollbar(viewport, orient="horizontal", command=self.content_canvas.xview)
        content_x.pack(side="bottom", fill="x")
        self.content_canvas.pack(fill="both", expand=True)
        self.content_canvas.configure(yscrollcommand=content_scroll.set, xscrollcommand=content_x.set)
        body = ttk.Frame(self.content_canvas, padding=20)
        body_window = self.content_canvas.create_window((0, 0), window=body, anchor="nw")
        def resize_content(event=None):
            width = max(self.content_canvas.winfo_width(),
                        actions.winfo_reqwidth() + 40, settings.winfo_reqwidth() + 44)
            self.content_canvas.itemconfigure(body_window, width=width)
            self.content_canvas.configure(scrollregion=self.content_canvas.bbox("all"))
        body.bind("<Configure>", resize_content)
        self.content_canvas.bind("<Configure>", resize_content)
        def scroll_content(event):
            if not isinstance(event.widget, (tk.Text, ttk.Treeview)):
                self.content_canvas.yview_scroll(-int(event.delta / 120), "units")
        self.root.bind("<MouseWheel>", scroll_content, add="+")
        def wrap_label(label, parent, margin=0):
            parent.bind("<Configure>", lambda e: label.configure(wraplength=max(80, e.width - margin)), add="+")
            label.configure(justify="left", wraplength=400)
        headline = ttk.Label(body, text="让连接持续在线", font=("Microsoft YaHei UI", 23, "bold"))
        headline.pack(anchor="w")
        wrap_label(headline, body, 40)
        subtitle = ttk.Label(body, text="检测网络状态，在断线时自动尝试已保存的 Wi-Fi。", foreground=MUTED)
        subtitle.pack(anchor="w", pady=(8, 20))
        wrap_label(subtitle, body, 40)
        card = tk.Frame(body, bg="white", padx=20, pady=15)
        card.pack(fill="x")
        self.status = tk.Label(card, text="●  守护未启动", bg="white", fg=MUTED,
                               font=("Microsoft YaHei UI", 18, "bold"))
        self.status.pack(anchor="w")
        self.network = tk.Label(card, text="正在读取网卡信息…", bg="white", fg=MUTED, font=("Microsoft YaHei UI", 10))
        self.network.pack(anchor="w", pady=(10, 0))
        self.stats = tk.Label(card, text="最近检测 —    |    本次恢复成功 0 / 0", bg="white", fg=MUTED, font=("Microsoft YaHei UI", 10))
        self.stats.pack(anchor="w", pady=(6, 0))
        for label in (self.status, self.network, self.stats):
            wrap_label(label, card, 40)
        actions = ttk.Frame(body)
        actions.pack(fill="x", pady=16)
        self.start_button = ttk.Button(actions, text="▶  启动守护", style="Primary.TButton", command=self.start)
        self.start_button.pack(side="left")
        self.stop_button = ttk.Button(actions, text="■  停止守护", command=self.stop, state="disabled")
        self.stop_button.pack(side="left", padx=8)
        self.refresh_button = ttk.Button(actions, text="刷新 Wi-Fi", command=self.refresh)
        self.refresh_button.pack(side="right")
        notebook = self.notebook = ttk.Notebook(body)
        notebook.pack(fill="both", expand=True)
        networks = ttk.Frame(notebook, padding=15)
        settings = ttk.Frame(notebook, padding=15)
        logs = ttk.Frame(notebook, padding=12)
        notebook.add(networks, text="已保存的 Wi-Fi")
        notebook.add(settings, text="守护设置")
        notebook.add(logs, text="运行日志")
        hint = ttk.Label(networks, text="默认仅当前连接的 Wi-Fi 参与恢复。点击右侧开关或选中后按空格修改。", foreground=MUTED)
        hint.pack(anchor="w", pady=(0, 12))
        wrap_label(hint, networks, 30)
        table = ttk.Frame(networks)
        table.pack(fill="both", expand=True)
        self.profiles = ttk.Treeview(table, columns=("name", "priority", "enabled"), show="headings", selectmode="browse", height=6)
        self.profiles.heading("name", text="Wi-Fi 配置名称")
        self.profiles.heading("priority", text="优先顺序")
        self.profiles.heading("enabled", text="参与恢复（点击切换）")
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
        ttk.Button(networks, text="将所选 Wi-Fi 设为首选", command=self.prefer).pack(anchor="w", pady=10)
        self.profile_hint = ttk.Label(networks, text="正在读取…", foreground=MUTED)
        self.profile_hint.pack(anchor="w")
        wrap_label(self.profile_hint, networks, 30)
        recovery_hint = ttk.Label(networks, text="开关自动保存，从下一轮恢复生效。关闭的 Wi-Fi 即使是首选或上次成功连接，也会跳过。", foreground=MUTED)
        recovery_hint.pack(anchor="w", pady=(8, 0))
        wrap_label(recovery_hint, networks, 30)
        settings.columnconfigure(1, weight=1)
        self.fields = {}
        for row, (key, label) in enumerate((("interval", "检测间隔（秒）"), ("reconnect_timeout", "重连超时（秒）"), ("interface", "网卡名称（留空自动）"))):
            ttk.Label(settings, text=label).grid(row=row, column=0, sticky="w", padx=(0, 16), pady=5)
            var = tk.StringVar()
            self.fields[key] = var
            ttk.Entry(settings, textvariable=var, font=("Microsoft YaHei UI", 10), width=16).grid(row=row, column=1, sticky="ew", pady=5)
        for row, key, label in [(3, "preferred_profiles", "首选配置（每行一个）"), (4, "check_targets", "检测地址（每行一个）")]:
            ttk.Label(settings, text=label).grid(row=row, column=0, sticky="nw", pady=5)
            widget = tk.Text(settings, height=3, width=24, font=("Microsoft YaHei UI", 10), relief="solid", bd=1)
            widget.grid(row=row, column=1, sticky="ew", pady=5)
            self.fields[key] = widget
        self.verbose = tk.BooleanVar()
        ttk.Checkbutton(settings, text="显示调试日志", variable=self.verbose).grid(row=5, column=1, sticky="w")
        ttk.Button(settings, text="保存设置", command=self.save).grid(row=6, column=1, sticky="e", pady=8)
        toolbar = ttk.Frame(logs)
        toolbar.pack(fill="x", pady=(0, 8))
        ttk.Label(toolbar, text="实时输出 · 最多显示 1500 行", foreground=MUTED).pack(side="left")
        ttk.Button(toolbar, text="清空显示", command=self.clear_logs).pack(side="right")
        self.log_text = tk.Text(logs, bg="#102139", fg="#d6e2f3", font=("Consolas", 10),
                                width=24, height=10, wrap="word", relief="flat", state="disabled")
        scroll = ttk.Scrollbar(logs, command=self.log_text.yview)
        scroll.pack(side="right", fill="y")
        self.log_text.configure(yscrollcommand=scroll.set)
        self.log_text.pack(fill="both", expand=True)
        self.log_text.tag_configure("ERROR", foreground="#fda4af")
        self.log_text.tag_configure("WARNING", foreground="#fcd34d")
        self.notice = ttk.Label(body, text="就绪 · 点击启动后开始自动检测与恢复", foreground=MUTED)
        self.notice.pack(anchor="w", pady=(12, 0))
        wrap_label(self.notice, body, 40)

    def load_fields(self):
        for key, widget in self.fields.items():
            if isinstance(widget, tk.StringVar):
                widget.set(str(self.config.get(key) or ""))
            else:
                widget.delete("1.0", "end")
                widget.insert("1.0", "\n".join(self.config[key]))
        self.verbose.set(self.config["verbose"])

    def save(self):
        data = dict(self.config)
        for key, widget in self.fields.items():
            data[key] = widget.get().strip() if isinstance(widget, tk.StringVar) else widget.get("1.0", "end").splitlines()
        data["interface"] = data["interface"] or None
        data["verbose"] = self.verbose.get()
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
        name = self.profiles.item(selection[0], "values")[0]
        widget = self.fields["preferred_profiles"]
        existing = [x.strip() for x in widget.get("1.0", "end").splitlines() if x.strip() and x.strip().casefold() != name.casefold()]
        widget.delete("1.0", "end")
        widget.insert("1.0", "\n".join([name] + existing))
        self.save()

    def update_priorities(self):
        preferred = [x.casefold() for x in self.config["preferred_profiles"]]
        disabled = {x.casefold() for x in self.config["disabled_profiles"]}
        enabled = {x.casefold() for x in (self.config["enabled_profiles"] or [])}
        for item in self.profiles.get_children():
            name = self.profiles.item(item, "values")[0]
            priority = str(preferred.index(name.casefold()) + 1) if name.casefold() in preferred else "—"
            self.profiles.item(item, values=(name, priority, "☑ 使用" if name.casefold() in enabled and name.casefold() not in disabled else "☐ 不使用"))

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
        name = self.profiles.item(item, "values")[0]
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
        try:
            self.process = subprocess.Popen([sys.executable, "-u", str(Path(__file__).resolve()), "--worker", "--config", str(self.config_path)],
                cwd=BASE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                encoding="utf-8", errors="replace", env=dict(os.environ, PYTHONIOENCODING="utf-8"),
                creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0)
        except OSError as exc:
            messagebox.showerror("无法启动", str(exc), parent=self.root)
            return
        process = self.process
        self.start_button.configure(state="disabled")
        self.stop_button.configure(state="normal")
        self.set_status("正在启动", "busy")
        self.stats.configure(text="最近检测 —    |    本次恢复成功 0 / 0")
        def read_output():
            with process.stdout:
                for line in process.stdout:
                    try:
                        event = json.loads(line)
                    except ValueError:
                        event = dict(kind="log", text=line.rstrip(), level="ERROR")
                    self.events.put((generation, event))
            self.events.put((generation, dict(kind="exit", code=process.wait())))
        threading.Thread(target=read_output, daemon=True).start()

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

    def poll(self):
        for _ in range(200):
            try:
                generation, event = self.events.get_nowait()
            except queue.Empty:
                break
            if generation is not None and generation != self.generation:
                continue
            kind = event["kind"]
            if kind == "log":
                self.append_log(event["text"], event.get("level", "INFO"))
            elif kind in ("inventory", "inventory_error"):
                self.refreshing = False
                self.refresh_button.configure(state="normal")
                if kind == "inventory_error":
                    self.profile_hint.configure(text="读取失败：" + event["text"])
                else:
                    if self.config["enabled_profiles"] is None:
                        enabled = initial_enabled_profiles(event["profiles"], event["interfaces"], self.config["disabled_profiles"])
                        data = dict(self.config, enabled_profiles=enabled)
                        try:
                            if not self.config_error:
                                save_settings(self.config_path, data)
                        except OSError as exc:
                            self.profile_hint.configure(text="无法保存默认 Wi-Fi 开关：" + str(exc))
                            continue
                        self.config = data
                    self.profiles.delete(*self.profiles.get_children())
                    for name in event["profiles"]:
                        self.profiles.insert("", "end", values=(name, "—"))
                    name_font = tkfont.Font(family="Microsoft YaHei UI", size=10)
                    name_width = max([180] + [name_font.measure(name) + 24 for name in event["profiles"]])
                    self.profiles.column("name", width=name_width, minwidth=name_width)
                    self.update_priorities()
                    self.profile_hint.configure(text=f"共 {len(event['profiles'])} 个已保存配置" if event["profiles"] else "未读取到配置；请先在 Windows 中连接 Wi-Fi，并检查 WLAN 服务及权限。")
                    self.show_interfaces(event["interfaces"])
            elif kind == "status" and not self.stopping:
                self.set_status(event["text"], event["tone"])
            elif kind == "result" and not self.stopping:
                self.set_status("网络可用 · 守护中" if event["online"] else "网络不可用 · 等待重试", "ok" if event["online"] else "error")
                self.show_interfaces(event["interfaces"])
                self.stats.configure(text=f"最近检测 {event['checked']}    |    本次恢复成功 {event['successes']} / {event['attempts']}")
            elif kind == "exit":
                stopped = self.stopping
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


def main():
    parser = argparse.ArgumentParser(description="Network Guard 图形界面")
    parser.add_argument("--config", type=Path, default=guard.DEFAULT_CONFIG_FILE)
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    args.config = args.config.resolve()
    if args.worker:
        worker(args.config)
        return
    if sys.platform == "win32":
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    root = tk.Tk()
    GuardApp(root, args.config)
    root.mainloop()


if __name__ == "__main__":
    main()

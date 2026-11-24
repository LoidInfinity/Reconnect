import json
from datetime import datetime
import os
from pathlib import Path
import tempfile
import tkinter as tk
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import gui


class SettingsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "config.json"
        self.config = gui.read_settings(self.path)

    def test_roundtrip_preserves_advanced_settings(self):
        self.config.update(preferred_profiles=["家庭 Wi-Fi"], custom_key="preserved")
        gui.save_settings(self.path, gui.validate_settings(self.config))
        self.assertEqual(gui.read_settings(self.path), self.config)

    def test_adapter_power_saving_defaults_on(self):
        self.assertTrue(self.config["disable_adapter_power_saving"])
        with self.assertRaises(ValueError):
            gui.validate_settings(dict(self.config, disable_adapter_power_saving="yes"))

    def test_update_channel_defaults_to_stable(self):
        self.assertEqual(self.config["update_channel"], "stable")
        with self.assertRaises(ValueError):
            gui.validate_settings(dict(self.config, update_channel="nightly"))

    def test_control_characters_are_rejected(self):
        for change in (dict(interface="WLAN\nx"), dict(preferred_profiles=["a\nb"]),
                       dict(disabled_profiles=["a\x1bb"]), dict(dns_reset_interfaces=["x\ry"])):
            with self.subTest(change=change), self.assertRaises(ValueError):
                gui.validate_settings(dict(self.config, **change))


    def test_invalid_inputs_rejected(self):
        for change in [dict(interval="0"), dict(interval="1.2"),
                       dict(check_targets=["file:///local"]), dict(check_targets=[]),
                       dict(preferred_profiles="wrong type"), dict(disabled_profiles="wrong type"), dict(log_file="")]:
            with self.subTest(change=change), self.assertRaises(ValueError):
                gui.validate_settings(dict(self.config, **change))

    def test_bad_json_is_not_silently_overwritten(self):
        self.path.write_text("invalid", encoding="utf-8")
        with self.assertRaises(ValueError):
            gui.read_settings(self.path)
        self.assertEqual(self.path.read_text(), "invalid")

    def test_initial_defaults_use_connected_profile_not_ssid(self):
        interfaces = [dict(state="connected", profile="Home profile", ssid="Home SSID"),
                      dict(state="disconnected", profile="Office", ssid="Office")]
        self.assertEqual(gui.initial_enabled_profiles(["Home profile", "Home SSID", "Office"], interfaces, []), ["Home profile"])
        self.assertEqual(gui.initial_enabled_profiles(["Home profile"], interfaces, ["HOME PROFILE"]), [])
        self.assertEqual(gui.initial_enabled_profiles(["Home"], [], []), [])
        self.assertEqual(gui.initial_enabled_profiles(["Home"], [dict(state="已连接", ssid="Home")], []), ["Home"])

    def test_interface_parser_extracts_profile(self):
        interfaces = gui.guard.parse_wifi_interfaces("Name : WLAN\nState : connected\nBSSID : aa:bb\nSSID : Public\nProfile : Saved Profile\nSignal : 90%")
        self.assertEqual(interfaces[0].profile, "Saved Profile")
        self.assertEqual(interfaces[0].ssid, "Public")


class WorkerTests(unittest.TestCase):
    def test_disabled_profiles_never_become_candidates(self):
        choose = gui.guard.choose_profiles_to_try
        self.assertEqual(choose(["Home", "Office"], "HOME", ["Home", "Office", "Guest"], ["home"]), ["Office", "Guest"])
        self.assertEqual(choose([], "Home", ["Home"], ["HOME"]), [])
        self.assertEqual(choose([], None, ["Home"]), ["Home"])
        self.assertEqual(choose(["New"], "New", ["Home", "New"], enabled_profiles=["home"]), ["Home"])
        self.assertEqual(choose([], "Home", ["Home"], enabled_profiles=[]), [])

    def test_all_disabled_does_not_connect(self):
        with patch.object(gui.guard, "ping_reachable", return_value=True), \
             patch.object(gui.guard.repairs, "wifi_radio_state", return_value="on"), \
             patch.object(gui.guard, "get_wifi_interfaces", return_value=[]), \
             patch.object(gui.guard, "get_saved_profiles", return_value=["Home"]), \
             patch.object(gui.guard, "read_state_file", return_value="Home"), \
             patch.object(gui.guard, "connect_wifi") as connect:
            self.assertFalse(gui.guard.recover_network(gui.logging.getLogger("test"), None,
                ["Home"], Path("unused"), 35, [], disabled_profiles=["home"]))
            connect.assert_not_called()

    def test_offline_recovery_emits_statistics(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            config = gui.read_settings(path)
            config.update(log_file=str(Path(directory) / "test.log"), state_file=str(Path(directory) / "state.txt"), pid_file=str(Path(directory) / "worker.pid"), prevent_sleep=False, disabled_profiles=["Guest"], enabled_profiles=["Home"])
            gui.save_settings(path, config)
            events = []
            try:
                with patch.object(gui.guard, "ensure_windows"), \
                     patch.object(gui.guard.repairs, "acquire_single_instance", return_value=True), \
                     patch.object(gui.guard, "is_internet_reachable", return_value=False), \
                     patch.object(gui.guard, "recover_network", return_value=True) as recover, \
                     patch.object(gui.guard, "get_wifi_interfaces", return_value=[]), \
                     patch.object(gui, "emit", side_effect=lambda kind, **data: events.append(dict(kind=kind, **data))), \
                     patch.object(gui.time, "sleep", side_effect=KeyboardInterrupt):
                    with self.assertRaises(KeyboardInterrupt):
                        gui.worker(path)
                    recover.assert_called_once()
                    self.assertEqual(recover.call_args.kwargs["disabled_profiles"], ["Guest"])
                    self.assertEqual(recover.call_args.kwargs["enabled_profiles"], ["Home"])
                result = next(x for x in events if x["kind"] == "result")
                self.assertEqual((result["online"], result["attempts"], result["successes"]), (True, 1, 1))
            finally:
                logger = gui.logging.getLogger(gui.guard.APP_NAME)
                for handler in logger.handlers[:]:
                    handler.close()
                    logger.removeHandler(handler)


class HideProfilesTests(unittest.TestCase):
    """隐藏名单、优先序号与排序逻辑（无需真实 Tk 窗口）。"""

    class _Profiles:
        def __init__(self, rows):
            self._rows = [list(row) for row in rows]
            self.headings = {}

        def get_children(self):
            return list(range(len(self._rows)))

        def item(self, index, option=None, values=None):
            if values is not None:
                self._rows[index] = list(values)
                return None
            return tuple(self._rows[index])

        def delete(self, *items):
            for index in sorted(items, reverse=True):
                del self._rows[index]

        def insert(self, parent, position, values):
            self._rows.append(list(values))

        def heading(self, column, text=None):
            self.headings[column] = text

        def names(self):
            return [row[0] for row in self._rows]

        def priorities(self):
            return [row[1] for row in self._rows]

    class _App:
        def __init__(self, directory, config, rows, hidden=()):
            self.directory = Path(directory)
            self.config_path = self.directory / "config.json"
            self.config = dict(config)
            self.profiles = HideProfilesTests._Profiles(rows)
            self.hidden_file = self.directory / "hidden_wifi.txt"
            self._profile_order = [row[0] for row in rows]
            self._raw_names = {}
            self._sort_column = None
            self._sort_ascending = True
            self._heading_labels = {"name": "Wi-Fi 配置名称", "priority": "优先顺序",
                                    "enabled": "参与恢复（点击切换）"}
            self.notices = []
            self.refresh_count = 0
            self.root = None
            self.restore_states = []
            self.notice = SimpleNamespace(
                configure=lambda **kwargs: self.notices.append(kwargs.get("text", ""))
            )
            self.restore_button = SimpleNamespace(
                configure=lambda **kwargs: self.restore_states.append(kwargs)
            )
            if hidden:
                gui.save_hidden_profiles(self.hidden_file, list(hidden))

        def refresh(self):
            self.refresh_count += 1

        def update_restore_button(self):
            gui.GuardApp.update_restore_button(self)

        def update_priorities(self):
            gui.GuardApp.update_priorities(self)

        def _name_of(self, item):
            return self.profiles.item(item, "values")[0]

        def priority_order(self):
            return gui.GuardApp.priority_order(self)

        def sort_by(self, column):
            gui.GuardApp.sort_by(self, column)

        def _apply_sort(self):
            gui.GuardApp._apply_sort(self)

        def _update_sort_arrows(self):
            gui.GuardApp._update_sort_arrows(self)

        def apply_list_order(self):
            gui.GuardApp.apply_list_order(self)

    def _app(self, directory, config, rows, hidden=()):
        return self._App(directory, config, rows, hidden)

    @staticmethod
    def _rows(*names):
        return [(name, "—", "☐ 不使用") for name in names]

    def test_hides_only_unchecked_profiles_into_config_file(self):
        with tempfile.TemporaryDirectory() as directory:
            app = self._app(directory, dict(enabled_profiles=["Home"], disabled_profiles=["Office"],
                                            preferred_profiles=["Office"]),
                            self._rows("Home", "Office"))
            with patch.object(gui.messagebox, "askyesno", return_value=True) as ask, \
                 patch.object(gui.subprocess, "run") as run:
                gui.GuardApp.hide_unselected(app)
            ask.assert_called_once()
            run.assert_not_called()  # 绝不执行 netsh 删除
            self.assertEqual(gui.load_hidden_profiles(app.hidden_file), ["Office"])
            saved = gui.read_settings(app.config_path)
            self.assertEqual(saved["disabled_profiles"], [])
            self.assertEqual(saved["preferred_profiles"], [])
            self.assertEqual(app.profiles.names(), ["Home"])
            self.assertEqual(app.refresh_count, 0)
            self.assertIn("Windows 中仍保留", app.notices[-1])
            self.assertIn("恢复已隐藏（1）", app.restore_states[-1]["text"])

    def test_cancel_changes_nothing(self):
        with tempfile.TemporaryDirectory() as directory:
            app = self._app(directory, dict(enabled_profiles=["Home"], disabled_profiles=["Office"]),
                            self._rows("Home", "Office"))
            with patch.object(gui.messagebox, "askyesno", return_value=False), \
                 patch.object(gui.subprocess, "run") as run:
                gui.GuardApp.hide_unselected(app)
            run.assert_not_called()
            self.assertEqual(app.config["disabled_profiles"], ["Office"])
            self.assertFalse(app.hidden_file.exists())

    def test_no_unchecked_profile_is_a_noop(self):
        with tempfile.TemporaryDirectory() as directory:
            app = self._app(directory, dict(enabled_profiles=["Home"]), self._rows("Home"))
            gui.GuardApp.hide_unselected(app)
            self.assertIn("没有未选定", app.notices[-1])

    def test_empty_whitelist_refuses_to_hide_everything(self):
        with tempfile.TemporaryDirectory() as directory:
            for enabled in (None, []):
                with self.subTest(enabled=enabled):
                    app = self._app(directory, dict(enabled_profiles=enabled),
                                    self._rows("Home", "Office"))
                    gui.GuardApp.hide_unselected(app)
                    self.assertIn("请先勾选", app.notices[-1])
                    self.assertEqual(app.profiles.names(), ["Home", "Office"])

    def test_restore_hidden_clears_the_file(self):
        with tempfile.TemporaryDirectory() as directory:
            app = self._app(directory, dict(enabled_profiles=["Home"]), self._rows("Home"),
                            hidden=["Office", "Cafe"])
            gui.GuardApp.restore_hidden(app)
            self.assertEqual(gui.load_hidden_profiles(app.hidden_file), [])
            self.assertEqual(app.refresh_count, 1)
            self.assertEqual(app.restore_states[-1]["state"], "disabled")

    def test_priority_column_numbers_every_row(self):
        with tempfile.TemporaryDirectory() as directory:
            app = self._app(directory, dict(enabled_profiles=["Home", "Office", "Cafe"],
                                            preferred_profiles=["Office"]),
                            self._rows("Home", "Office", "Cafe"))
            app.update_priorities()
            self.assertEqual(app.priority_order(), ["Office", "Home", "Cafe"])
            self.assertEqual(app.profiles.priorities(), ["2", "1", "3"])

    def test_sorting_headers_reorder_rows(self):
        with tempfile.TemporaryDirectory() as directory:
            app = self._app(directory, dict(enabled_profiles=["Home", "Office", "Cafe"],
                                            preferred_profiles=["Office"]),
                            self._rows("Home", "Office", "Cafe"))
            app.update_priorities()
            app.sort_by("priority")
            self.assertEqual(app.profiles.names(), ["Office", "Home", "Cafe"])
            app.sort_by("priority")  # 再点一次反向
            self.assertEqual(app.profiles.names(), ["Cafe", "Home", "Office"])
            app.sort_by("name")
            self.assertEqual(app.profiles.names(), ["Cafe", "Home", "Office"])
            self.assertIn("↑", app.profiles.headings["name"])

    def test_apply_list_order_persists_current_order(self):
        with tempfile.TemporaryDirectory() as directory:
            app = self._app(directory, dict(enabled_profiles=["Home", "Office", "Cafe"]),
                            self._rows("Home", "Office", "Cafe"))
            app.sort_by("name")
            app.apply_list_order()
            saved = gui.read_settings(app.config_path)
            self.assertEqual(saved["preferred_profiles"], ["Cafe", "Home", "Office"])
            self.assertEqual(app.profiles.priorities(), ["1", "2", "3"])
            self.assertIn("已按当前列表顺序", app.notices[-1])

class WindowTests(unittest.TestCase):
    def setUp(self):
        # 无交互桌面的环境（如 CI）无法真正完成窗口布局，
        # 布局断言在这里没有意义，直接跳过而不是让构建失败。
        if os.environ.get("SKIP_GUI_WINDOW_TESTS"):
            self.skipTest("已通过 SKIP_GUI_WINDOW_TESTS 跳过界面布局测试")
        try:
            probe = tk.Tk()
        except tk.TclError as exc:
            self.skipTest(f"当前环境无法创建 Tk 窗口: {exc}")
        probe.destroy()

    def test_high_dpi_controls_are_not_clipped(self):
        for scaling in (1.333, 2.0, 2.667):
            with self.subTest(scaling=scaling), tempfile.TemporaryDirectory() as directory:
                root = tk.Tk()
                try:
                    root.tk.call("tk", "scaling", scaling)
                    with patch.object(gui.GuardApp, "refresh"):
                        app = gui.GuardApp(root, Path(directory) / "config.json")
                    root.geometry("900x700")
                    root.update()
                    for tab in app.notebook.tabs():
                        app.notebook.select(tab)
                        root.update()
                        hidden_tabs = {str(item) for item in app.notebook.tabs() if str(item) != str(app.notebook.select())}
                        def check_widgets(widget):
                            if str(widget) in hidden_tabs:
                                return
                            if widget.winfo_ismapped() and isinstance(widget, (gui.ttk.Label, gui.ttk.Button, gui.ttk.Entry, tk.Checkbutton, tk.Label)):
                                self.assertGreaterEqual(widget.winfo_height(), widget.winfo_reqheight(), str(widget))
                                self.assertGreaterEqual(widget.winfo_width(), widget.winfo_reqwidth(), str(widget))
                            for child in widget.winfo_children():
                                check_widgets(child)
                        check_widgets(root)
                    font = gui.tkfont.Font(family="Microsoft YaHei UI", size=10)
                    self.assertGreater(int(gui.ttk.Style().lookup("Treeview", "rowheight")), font.metrics("linespace"))
                finally:
                    for timer in root.tk.call("after", "info"):
                        root.after_cancel(timer)
                    root.destroy()

    def test_main_window_has_no_outer_scrollbar(self):
        """回归：整页不再用 Canvas 包裹，不能出现主界面滚动条。

        旧版把整页放进 Canvas，滚动条不显眼，用户会误以为看到的就是全部。
        现在窗口按内容自适应，只有日志区保留自己的滚动条。
        """
        with tempfile.TemporaryDirectory() as directory:
            root = tk.Tk()
            root.tk.call("tk", "scaling", 1.333)
            try:
                with patch.object(gui.GuardApp, "refresh"):
                    app = gui.GuardApp(root, Path(directory) / "config.json")
                root.update()
                self.assertFalse(hasattr(app, "content_canvas"))
                direct_scrollbars = [w for w in root.winfo_children() if isinstance(w, gui.ttk.Scrollbar)]
                self.assertEqual(direct_scrollbars, [])
                self.assertLessEqual(app.body.winfo_reqheight(), root.winfo_height())
                self.assertLessEqual(app.sidebar_width + app.body.winfo_reqwidth(), root.winfo_width())
            finally:
                for timer in root.tk.call("after", "info"):
                    root.after_cancel(timer)
                root.destroy()

    def test_window_settings_events_and_layout(self):
        with tempfile.TemporaryDirectory() as directory:
            root = tk.Tk()
            root.tk.call("tk", "scaling", 1.333)
            try:
                with patch.object(gui.GuardApp, "refresh"):
                    app = gui.GuardApp(root, Path(directory) / "config.json")
                root.update()
                app.events.put((None, dict(kind="inventory", profiles=["家庭 Wi-Fi"], interfaces=[dict(name="WLAN", state="已连接", ssid="家庭 Wi-Fi")])))
                app.poll()
                item = app.profiles.get_children()[0]
                app.profiles.selection_set(item)
                app.prefer()
                self.assertEqual(json.loads(app.config_path.read_text(encoding="utf-8"))["preferred_profiles"], ["家庭 Wi-Fi"])
                app.toggle_selected()
                self.assertEqual(gui.read_settings(app.config_path)["disabled_profiles"], ["家庭 Wi-Fi"])
                self.assertEqual(gui.read_settings(app.config_path)["enabled_profiles"], [])
                self.assertEqual(app.profiles.item(item, "values")[2], "☐ 不使用")
                # A refresh must not turn a manually disabled current network back on.
                app.events.put((None, dict(kind="inventory", profiles=["家庭 Wi-Fi", "New"], interfaces=[dict(name="WLAN", state="已连接", ssid="家庭 Wi-Fi")])))
                app.poll()
                item = app.profiles.get_children()[0]
                app.profiles.selection_set(item)
                self.assertTrue(all(app.profiles.item(row, "values")[2] == "☐ 不使用" for row in app.profiles.get_children()))
                app.toggle_selected()
                self.assertEqual(gui.read_settings(app.config_path)["disabled_profiles"], [])
                self.assertEqual(gui.read_settings(app.config_path)["enabled_profiles"], ["家庭 Wi-Fi"])
                with patch.object(gui, "save_settings", side_effect=OSError("read only")), patch.object(gui.messagebox, "showerror"):
                    app.toggle_selected()
                self.assertEqual(app.config["disabled_profiles"], [])
                app.events.put((app.generation, dict(kind="result", online=True, successes=1, attempts=2, checked="12:00:00", interfaces=[])))
                app.poll()
                self.assertIn("网络可用", app.status.cget("text"))
                self.assertIn("1 / 2", app.stats.cget("text"))
                # 窗口不再整页滚动：最小尺寸下底部提示也必须完整可见
                root.geometry("900x700")
                notebook = app.notebook
                for tab in notebook.tabs():
                    notebook.select(tab)
                    root.update()
                    self.assertLessEqual(app.notice.winfo_rooty() + app.notice.winfo_height(), root.winfo_rooty() + root.winfo_height())
                    self.assertEqual(app.notice.winfo_height(), app.notice.winfo_reqheight())
                with patch.object(gui.subprocess, "Popen") as popen, patch.object(gui.threading, "Thread"):
                    process = popen.return_value
                    app.start()
                    app.start()
                    popen.assert_called_once()
                    app.stop()
                    process.terminate.assert_called_once()
                    app.events.put((app.generation, dict(kind="exit", code=1)))
                    app.poll()
                    self.assertIsNone(app.process)
                    self.assertIn("已停止", app.status.cget("text"))
                # 滚轮在列表/日志上滚动它们自己，而不是整个页签
                self.assertFalse(gui.wheel_scrolls_container(app.profiles))
                self.assertFalse(gui.wheel_scrolls_container(app.log_text))
                self.assertTrue(gui.wheel_scrolls_container(app.notice))
                # 设置项使用带对勾的原生复选框，而不是 clam 主题的 X 形
                self.assertEqual(len(app.switch_buttons), 8)
                for button in app.switch_buttons:
                    self.assertIsInstance(button, tk.Checkbutton)
                    self.assertNotIsInstance(button, gui.ttk.Checkbutton)
                self.assertIsInstance(app.verbose_button, tk.Checkbutton)
                self.assertEqual(app.update_channel.get(), "稳定版")
                self.assertEqual(list(app.update_channel_combo.cget("values")),
                                 ["稳定版", "公测版", "内测版"])
                with patch.object(gui.guard.latency, "read_series",
                                  return_value=([(datetime.now(), 30.0), (datetime.now(), 45.0)], [])), \
                     patch.object(gui.guard.latency, "write_chart") as write_chart:
                    app.show_latency_chart()
                    app.root.update_idletasks()
                    window = app._latency_window
                    self.assertIsNotNone(window)
                    self.assertGreater(len(window.canvas.find_all()), 0)
                    window.destroy()
                    write_chart.assert_not_called()
            finally:
                for timer in root.tk.call("after", "info"):
                    root.after_cancel(timer)
                root.destroy()


if __name__ == "__main__":
    unittest.main()

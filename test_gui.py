import json
from pathlib import Path
import tempfile
import tkinter as tk
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


class WindowTests(unittest.TestCase):
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
                        def check_widgets(widget):
                            if widget.winfo_ismapped() and isinstance(widget, (gui.ttk.Label, gui.ttk.Button, gui.ttk.Entry, gui.ttk.Checkbutton, tk.Label)):
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

    def test_window_settings_events_and_layout(self):
        with tempfile.TemporaryDirectory() as directory:
            root = tk.Tk()
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
                # Check all tabs at the minimum supported window size.
                root.geometry("900x700")
                notebook = app.notebook
                for tab in notebook.tabs():
                    notebook.select(tab)
                    root.update()
                    app.content_canvas.yview_moveto(1)
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
            finally:
                for timer in root.tk.call("after", "info"):
                    root.after_cancel(timer)
                root.destroy()


if __name__ == "__main__":
    unittest.main()

"""Security regressions for attacker-controlled SSID / profile names.

A Wi-Fi name is untrusted input: it can contain quotes, semicolons, control
characters, ANSI escapes or bidi overrides. These tests pin down that such
names never become shell/PowerShell syntax, never forge log lines or
line-based files, and are not used for reconnect attempts.
"""
import logging
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import gui
import main
import repairs

ROOT = Path(__file__).resolve().parent.parent


def make_logger(name: str) -> logging.Logger:
    logger = logging.getLogger(name)
    logger.handlers = [logging.NullHandler()]
    logger.propagate = False
    return logger


class NameSanitizerTests(unittest.TestCase):
    def test_control_and_bidi_names_are_unsafe(self):
        for name in ("bad\nname", "bad\rname", "bad\x1b[31mname", "bad\u202ename", "bad\x00name"):
            with self.subTest(name=name):
                self.assertFalse(main.is_safe_profile_name(name))
        for name in ("Cafe", "宿舍 Wi-Fi", "Café-5G", "A" * 32):
            with self.subTest(name=name):
                self.assertTrue(main.is_safe_profile_name(name))

    def test_display_name_is_single_line_and_escape_free(self):
        text = main.display_name("Cafe\x1b[31m\r\nINJECT\u202e")
        self.assertNotIn("\x1b", text)
        self.assertNotIn("\n", text)
        self.assertNotIn("\r", text)
        self.assertNotIn("\u202e", text)
        self.assertIn("Cafe", text)

    def test_profile_names_are_pseudonymized(self):
        masked = main.mask_profile("SecretCafe")
        self.assertNotIn("SecretCafe", masked)
        self.assertEqual(masked, main.mask_profile("SecretCafe"))
        self.assertNotEqual(masked, main.mask_profile("OtherCafe"))

    def test_netsh_profile_list_is_redacted(self):
        text = main.redact("所有用户配置文件 : SecretCafe | 所有用户配置文件 : <无>")
        self.assertNotIn("SecretCafe", text)
        self.assertIn("<profile>", text)
        self.assertIn("<无>", text)

    def test_formatter_strips_ansi_and_carriage_returns(self):
        formatter = main.SafeFormatter("%(message)s")
        record = logging.LogRecord("x", logging.INFO, "", 0, "ssid \x1b[2J\rnext", None, None)
        rendered = formatter.format(record)
        self.assertNotIn("\x1b", rendered)
        self.assertNotIn("\r", rendered)
        self.assertIn("next", rendered)


class CommandInjectionTests(unittest.TestCase):
    def setUp(self):
        self.logger = make_logger("test.security.commands")

    def test_profile_with_control_chars_is_never_connected(self):
        with patch.object(main, "run_command") as run:
            self.assertFalse(main.connect_wifi("bad\nname", self.logger, None))
        run.assert_not_called()

    def test_profile_name_stays_one_argument(self):
        completed = SimpleNamespace(returncode=0, stdout="", stderr="")
        with patch.object(main, "run_command", return_value=completed) as run:
            self.assertTrue(main.connect_wifi("Cafe; rm -rf *", self.logger, None))
        self.assertEqual(run.call_args[0][0],
                         ["netsh", "wlan", "connect", "name=Cafe; rm -rf *"])

    def test_state_file_rejects_multiline_names(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.txt"
            main.write_state_file(path, "Home\nInjected", self.logger)
            self.assertFalse(path.exists())
            main.write_state_file(path, "Home", self.logger)
            self.assertEqual(path.read_text(encoding="utf-8"), "Home")
            path.write_text("Evil\r\nInjected", encoding="utf-8")
            self.assertIsNone(main.read_state_file(path))

    def test_adapter_name_is_passed_through_environment_not_script(self):
        captured = {}

        def fake_run(command, logger, timeout=None, env=None):
            captured["command"] = command
            captured["env"] = env
            return SimpleNamespace(stdout="OK:以太网", returncode=0, stderr="")

        hostile = "Ethernet'; calc; '"
        with patch.object(repairs, "_run", side_effect=fake_run), \
             patch.object(repairs.time, "sleep"):
            self.assertTrue(repairs._reset_named_adapter(self.logger, hostile))
        script = captured["command"][-1]
        self.assertNotIn("calc", script)
        self.assertIn("$env:NG_ADAPTER_NAME", script)
        self.assertEqual(captured["env"]["NG_ADAPTER_NAME"], hostile)


class HiddenFileTests(unittest.TestCase):
    def test_hidden_file_is_line_safe(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "hidden_wifi.txt"
            gui.save_hidden_profiles(path, ["Home", "bad\nname", "home", "Cafe"])
            self.assertEqual(gui.load_hidden_profiles(path), ["Home", "Cafe"])


class RecoveryFilterTests(unittest.TestCase):
    def test_unsafe_profiles_are_not_attempted(self):
        logger = make_logger("test.security.recovery")
        with patch.object(repairs, "wifi_radio_state", return_value="on"), \
             patch.object(main, "is_internet_reachable", return_value=False), \
             patch.object(main, "http_reachable", return_value=False), \
             patch.object(repairs, "get_physical_adapters", return_value=[]), \
             patch.object(main, "get_wifi_interfaces", return_value=[]), \
             patch.object(main, "get_saved_profiles", return_value=["Good", "Bad\nName"]), \
             patch.object(main, "read_state_file", return_value=None), \
             patch.object(main, "connect_wifi", return_value=False) as connect, \
             patch.object(repairs, "check_dns_resolution", return_value=[]), \
             patch.object(repairs, "clear_proxy_settings", return_value=False), \
             patch.object(main.time, "sleep"):
            self.assertFalse(main.recover_network(
                logger, None, [], Path("unused"), 5, ["https://example.org"],
                cleanup_proxy=True, repair_dns_enabled=True, adapter_reset=False))
        connect.assert_called_once_with("Good", logger, None)


class InstallerScriptTests(unittest.TestCase):
    def test_installer_deletes_motw_without_powershell(self):
        text = (ROOT / "src" / "installer.iss").read_text(encoding="utf-8")
        self.assertIn("DeleteZoneIdentifier", text)
        self.assertIn(":Zone.Identifier", text)
        self.assertNotIn("powershell.exe", text)


if __name__ == "__main__":
    unittest.main()

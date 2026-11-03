import json
import logging
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import gui
import main
import repairs


class ConfigMigrationTests(unittest.TestCase):
    def test_old_defaults_and_allowlist(self):
        data = main.migrate_config(dict(check_targets=list(main.LEGACY_CHECK_TARGETS),
            dns_check_hosts=["www.baidu.com", "www.cloudflare.com"], allowed_profiles=["Home"]))
        self.assertEqual(data["check_targets"], list(main.DEFAULT_CHECK_TARGETS))
        self.assertEqual(data["dns_check_hosts"], list(main.DEFAULT_DNS_HOSTS))
        self.assertEqual(data["enabled_profiles"], ["Home"])

    def test_custom_targets_and_explicit_empty_allowlist_preserved(self):
        original = dict(check_targets=["https://example.org/health"], dns_check_hosts=["example.org"],
                        allowed_profiles=["Home"], enabled_profiles=[])
        self.assertEqual(main.migrate_config(original), original)

    def test_gui_imports_legacy_configuration(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(json.dumps(dict(allowed_profiles=["Home"], dns_reset_interfaces=["Ethernet"])))
            result = gui.validate_settings(gui.read_settings(path))
            self.assertEqual(result["enabled_profiles"], ["Home"])
            self.assertEqual(result["dns_reset_interfaces"], ["Ethernet"])

    def test_old_cli_flags_still_parse(self):
        args = main.build_arg_parser().parse_args(["--wifi-recovery", "--allowed-profile", "Home"])
        self.assertTrue(args.wifi_recovery)
        self.assertEqual(args.allowed_profile, ["Home"])


class ProbeTests(unittest.TestCase):
    def test_requested_targets_are_defaults(self):
        self.assertEqual(main.DEFAULT_CHECK_TARGETS,
                         ("https://www.todesk.com", "https://sunlogin.oray.com", "https://www.awesun.com"))

    def test_normal_site_can_follow_redirect(self):
        response = Mock(status=200)
        response.geturl.return_value = "https://awesun.aweray.com/en"
        manager = Mock()
        manager.__enter__ = Mock(return_value=response)
        manager.__exit__ = Mock(return_value=False)
        opener = Mock()
        opener.open.return_value = manager
        with patch("urllib.request.build_opener", return_value=opener):
            self.assertTrue(main.http_reachable(logging.getLogger("test"), ["https://www.awesun.com"]))

    def test_204_probe_rejects_login_pages_and_redirects(self):
        target = "https://example.org/generate_204"
        for status, final_url, expected in ((204, target, True), (200, target, False),
                                           (204, "https://example.org/login", False)):
            with self.subTest(status=status, final_url=final_url):
                response = Mock(status=status)
                response.geturl.return_value = final_url
                manager = Mock()
                manager.__enter__ = Mock(return_value=response)
                manager.__exit__ = Mock(return_value=False)
                opener = Mock()
                opener.open.return_value = manager
                with patch("urllib.request.build_opener", return_value=opener):
                    self.assertEqual(main.http_reachable(logging.getLogger("test"), [target]), expected)


class RecoveryTests(unittest.TestCase):
    def test_dns_recovery_does_not_clear_working_proxy(self):
        with patch.object(repairs, "check_dns_resolution", side_effect=[["example.org"], []]), \
             patch.object(repairs, "repair_dns", return_value=True) as dns, \
             patch.object(repairs, "get_up_interface_names") as discover, \
             patch.object(main, "is_internet_reachable", return_value=True), \
             patch.object(repairs, "clear_proxy_settings") as proxy:
            self.assertTrue(main.recover_network(logging.getLogger("test"), None, [], Path("unused"), 35,
                [], repair_dns_enabled=True, cleanup_proxy=True, dns_check_hosts=["example.org"],
                dns_reset_interfaces=["Ethernet"]))
            dns.assert_called_once_with(unittest.mock.ANY, ["Ethernet"])
            discover.assert_not_called()
            proxy.assert_not_called()

    def test_no_wifi_enabled_never_resets_adapter(self):
        with patch.object(main, "ping_reachable", return_value=True), \
             patch.object(main, "get_wifi_interfaces", return_value=[]), \
             patch.object(main, "get_saved_profiles", return_value=["Home"]), \
             patch.object(main, "read_state_file", return_value="Home"), \
             patch.object(repairs, "reset_wifi_adapter") as reset, \
             patch.object(main, "connect_wifi") as connect:
            self.assertFalse(main.recover_network(logging.getLogger("test"), None, [], Path("unused"),
                35, [], enabled_profiles=[], adapter_reset=True))
            reset.assert_not_called()
            connect.assert_not_called()


class RuntimeTests(unittest.TestCase):
    def test_session_cleans_pid_and_sleep_on_exception(self):
        with tempfile.TemporaryDirectory() as directory:
            pid = Path(directory) / "test.pid"
            with patch.object(repairs, "prevent_sleep", return_value=True), \
                 patch.object(repairs, "restore_sleep") as restore:
                with self.assertRaises(KeyboardInterrupt):
                    with repairs.monitor_session(logging.getLogger("test"), pid, Path(directory) / "log.txt", True):
                        self.assertTrue(pid.exists())
                        raise KeyboardInterrupt
                self.assertFalse(pid.exists())
                restore.assert_called_once()

    def test_cleanup_does_not_remove_another_workers_pid(self):
        with tempfile.TemporaryDirectory() as directory:
            pid = Path(directory) / "test.pid"
            pid.write_text("12345")
            repairs.remove_owned_pid_file(pid, 67890)
            self.assertTrue(pid.exists())

    def test_gui_heartbeat_and_sleep_resume_are_wired(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            config = gui.read_settings(path)
            config["heartbeat_interval"] = 60
            logger = Mock()
            logger.handlers = []
            with patch.object(gui.guard, "is_internet_reachable", return_value=True), \
                 patch.object(gui.guard, "get_wifi_interfaces", return_value=[]), \
                 patch.object(gui, "emit"), \
                 patch.object(gui.time, "monotonic", side_effect=[0, 61, 62]), \
                 patch.object(gui.time, "time", side_effect=[0, 600, 600]), \
                 patch.object(gui.time, "sleep", side_effect=[None, KeyboardInterrupt]), \
                 patch.object(repairs, "detect_sleep", return_value=True) as detect:
                with self.assertRaises(KeyboardInterrupt):
                    gui._monitor_worker(path, config, logger)
            detect.assert_called_once_with(logger, config["interval"], 600)
            self.assertTrue(any("心跳" in str(call) for call in logger.info.call_args_list))


if __name__ == "__main__":
    unittest.main()

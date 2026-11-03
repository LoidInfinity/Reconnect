"""Regression coverage for ICMP reachable but application traffic broken."""
import argparse
from contextlib import ExitStack
import logging
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import gui
import main
import repairs


class RecoveryPolicyTests(unittest.TestCase):
    def setUp(self):
        self.logger = logging.getLogger("test.recovery_policy")
        self.logger.handlers = [logging.NullHandler()]
        self.logger.propagate = False

    def test_ping_does_not_override_failed_http(self):
        for targets in (list(main.DEFAULT_CHECK_TARGETS), []):
            with self.subTest(targets=targets), \
                 patch.object(main, "http_reachable", return_value=False) as http, \
                 patch.object(main, "ping_reachable", return_value=True) as ping:
                self.assertFalse(main.is_internet_reachable(self.logger, targets))
                http.assert_called_once_with(self.logger, list(main.DEFAULT_CHECK_TARGETS), 4)
                ping.assert_not_called()

    def test_gui_and_cli_recover_when_http_fails_but_ping_succeeds(self):
        for entry in ("gui", "cli"):
            with self.subTest(entry=entry), tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
                config_path = Path(directory) / "config.json"
                config = gui.read_settings(config_path)
                config.update(enabled_profiles=["Home"], state_file=str(Path(directory) / "state.txt"),
                              pid_file=str(Path(directory) / "worker.pid"), prevent_sleep=False)
                gui.save_settings(config_path, config)
                # Initial detection fails; DNS/proxy attempt fails; selected Wi-Fi
                # reconnect finally restores HTTP. No real network commands run.
                http = stack.enter_context(patch.object(main, "http_reachable", side_effect=[False, False, True]))
                ping = stack.enter_context(patch.object(main, "ping_reachable", return_value=True))
                stack.enter_context(patch.object(repairs, "check_dns_resolution", return_value=[]))
                dns = stack.enter_context(patch.object(repairs, "repair_dns"))
                proxy = stack.enter_context(patch.object(repairs, "clear_proxy_settings", return_value=False))
                stack.enter_context(patch.object(main, "get_wifi_interfaces", return_value=[]))
                stack.enter_context(patch.object(main, "get_saved_profiles", return_value=["Guest", "Home"]))
                connect = stack.enter_context(patch.object(main, "connect_wifi", return_value=True))
                reset = stack.enter_context(patch.object(repairs, "reset_wifi_adapter"))
                stack.enter_context(patch.object(main.time, "sleep", side_effect=KeyboardInterrupt))
                for name in ("write_pid_file", "install_signal_handlers", "register_exit_handler"):
                    stack.enter_context(patch.object(repairs, name))
                events = []
                stack.enter_context(patch.object(gui, "emit", side_effect=lambda kind, **data: events.append(dict(kind=kind, **data))))
                with self.assertRaises(KeyboardInterrupt):
                    if entry == "gui":
                        gui._monitor_worker(config_path, config, self.logger)
                    else:
                        args = argparse.Namespace(**config, check_target=config["check_targets"], preferred_profile=[],
                            cleanup_proxy=True, repair_dns=True, adapter_reset=True)
                        main.monitor_loop(args, self.logger)
                self.assertEqual(http.call_count, 3)
                ping.assert_called_once()
                proxy.assert_called_once()
                dns.assert_not_called()  # DNS resolves normally in this scenario.
                connect.assert_called_once_with("Home", self.logger, None)
                reset.assert_not_called()
                self.assertEqual(Path(config["state_file"]).read_text(), "Home")
                if entry == "gui":
                    result = next(event for event in events if event["kind"] == "result")
                    self.assertEqual((result["online"], result["attempts"], result["successes"]), (True, 1, 1))

    def test_proxy_recovery_succeeds_without_wifi_reconnect(self):
        with patch.object(repairs, "clear_proxy_settings", return_value=True) as proxy, \
             patch.object(main, "http_reachable", return_value=True), \
             patch.object(main, "ping_reachable", return_value=True), \
             patch.object(main, "connect_wifi") as connect, \
             patch.object(main.time, "sleep"):
            self.assertTrue(main.recover_network(self.logger, None, [], Path("unused"), 35,
                list(main.DEFAULT_CHECK_TARGETS), cleanup_proxy=True, enabled_profiles=["Home"]))
            proxy.assert_called_once()
            connect.assert_not_called()

    def test_disabled_wifi_recovery_is_still_respected(self):
        with patch.object(repairs, "clear_proxy_settings", return_value=False) as proxy, \
             patch.object(main, "http_reachable", return_value=False), \
             patch.object(main, "ping_reachable", return_value=True), \
             patch.object(main, "connect_wifi") as connect:
            self.assertFalse(main.recover_network(self.logger, None, [], Path("unused"), 35,
                list(main.DEFAULT_CHECK_TARGETS), cleanup_proxy=True, wifi_recovery=False))
            proxy.assert_called_once()
            connect.assert_not_called()


if __name__ == "__main__":
    unittest.main()

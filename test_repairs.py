import unittest
from pathlib import Path

import main
import repairs


class RedactionTests(unittest.TestCase):
    def test_mac_and_guid_are_masked(self):
        text = "adapter aa:bb:cc:dd:ee:ff guid 11111111-2222-3333-4444-555555555555"
        masked = main.redact(text)
        self.assertNotIn("aa:bb:cc", masked)
        self.assertNotIn("11111111", masked)
        self.assertIn("<mac>", masked)
        self.assertIn("<guid>", masked)

    def test_labelled_adapter_field_is_masked(self):
        masked = main.redact("物理地址 : aa:bb:cc:dd:ee:ff")
        self.assertNotIn("aa:bb:cc", masked)
        self.assertIn("<redacted>", masked)

    def test_ssid_profile_and_bssid_are_masked(self):
        text = "SSID : sample-net | AP BSSID : 11:22:33:44:55:66 | 配置文件 : sample-net"
        masked = main.redact(text)
        for secret in ("sample-net", "11:22:33"):
            self.assertNotIn(secret, masked)
        self.assertIn("SSID: <redacted>", masked)

    def test_dataclass_repr_is_masked(self):
        text = "WifiInterface(name='WLAN 2', ssid='sample-net', signal='84%', profile='sample-net')"
        masked = main.redact(text)
        self.assertNotIn("sample-net", masked)
        self.assertIn("WLAN 2", masked)
        self.assertIn("84%", masked)

    def test_plain_text_is_untouched(self):
        text = "已保存的 Wi-Fi 配置: 3 个"
        self.assertEqual(main.redact(text), text)
        self.assertEqual(main.redact(""), "")


class SleepDetectionTests(unittest.TestCase):
    def test_normal_wait_is_not_reported(self):
        class Logger:
            def __init__(self):
                self.warnings = []

            def warning(self, *args):
                self.warnings.append(args)

        logger = Logger()
        self.assertFalse(repairs.detect_sleep(logger, 10, 12))
        self.assertEqual(logger.warnings, [])

    def test_suspend_gap_is_reported(self):
        class Logger:
            def __init__(self):
                self.warnings = []

            def warning(self, *args):
                self.warnings.append(args)

        logger = Logger()
        self.assertTrue(repairs.detect_sleep(logger, 10, 600))
        self.assertEqual(len(logger.warnings), 1)


class ProxyDetectionTests(unittest.TestCase):
    def test_local_proxy_variants(self):
        for value in ("127.0.0.1:7899", "localhost:7890", "http=127.0.0.1:7899",
                      "socks=::1:1080", "0.0.0.0:8080"):
            with self.subTest(value=value):
                self.assertTrue(repairs.is_local_proxy(value))

    def test_remote_or_missing_proxy(self):
        for value in ("10.0.0.5:8080", "", None, 0, "example.com:80"):
            with self.subTest(value=value):
                self.assertFalse(repairs.is_local_proxy(value))


class PathTests(unittest.TestCase):
    def test_relative_paths_resolve_against_app_dir(self):
        resolved = main.resolve_path("network_guard.log")
        self.assertTrue(resolved.is_absolute())
        self.assertEqual(resolved.parent, main.DATA_DIR)

    def test_absolute_paths_pass_through(self):
        target = Path("C:/tmp/network_guard.log")
        self.assertEqual(main.resolve_path(str(target)), target)


class DefaultConfigTests(unittest.TestCase):
    def test_repair_switches_have_non_personal_defaults(self):
        import json
        import tempfile

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            main.save_default_config(path)
            data = json.loads(path.read_text(encoding="utf-8"))
        for key in ("prevent_sleep", "cleanup_proxy_on_disconnect", "repair_dns_on_failure",
                    "wifi_recovery", "adapter_reset_on_failure", "heartbeat_interval", "pid_file"):
            self.assertIn(key, data)
        # Defaults must not point at a machine-specific path or a private domain.
        self.assertFalse(Path(data["log_file"]).is_absolute())
        self.assertEqual(data["disabled_profiles"], [])
        self.assertEqual(data["dns_check_hosts"], list(main.DEFAULT_DNS_HOSTS))


if __name__ == "__main__":
    unittest.main()

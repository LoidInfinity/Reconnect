"""Regression coverage for the "Wi-Fi radio switched off" outage.

The real incident: Windows turned the WLAN radio off in software at 23:37:17,
``netsh wlan connect`` answered 2150899714 ("接口电源关闭"), the program skipped
its only hardware remedy because a participation list was configured, and the
network only came back 96 seconds later when WLAN AutoConfig reconnected on its
own. These tests pin down the detection, the radio repair, and the policy that
lets an adapter reset run while the radio is off.
"""
from contextlib import ExitStack
import logging
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

import main
import repairs


def make_logger(name: str) -> logging.Logger:
    logger = logging.getLogger(name)
    logger.handlers = [logging.NullHandler()]
    logger.propagate = False
    return logger


RADIO_OFF_BLOCK = """    名称                   : WLAN 2
    说明            : RZ616 Wi-Fi 6E 160MHz
    状态                  : 已断开连接
    无线电状态           : 硬件 开
                             软件 关
"""

RADIO_ON_ENGLISH_BLOCK = """    Name                   : WLAN 2
    State                  : connected
    SSID                   : sample-net
    Radio status           : Hardware On
                             Software On
"""


class RadioStateParsingTests(unittest.TestCase):
    def test_localized_radio_off_is_parsed(self):
        interfaces = main.parse_wifi_interfaces(RADIO_OFF_BLOCK)
        self.assertEqual(len(interfaces), 1)
        item = interfaces[0]
        self.assertEqual((item.radio_hardware, item.radio_software), ("on", "off"))
        self.assertTrue(main.wifi_radio_off(interfaces))

    def test_english_radio_on_is_parsed(self):
        interfaces = main.parse_wifi_interfaces(RADIO_ON_ENGLISH_BLOCK)
        self.assertEqual((interfaces[0].radio_hardware, interfaces[0].radio_software), ("on", "on"))
        self.assertFalse(main.wifi_radio_off(interfaces))

    def test_missing_radio_block_is_not_guessed_as_off(self):
        interfaces = main.parse_wifi_interfaces("Name : WLAN 2\nState : connected\nSSID : sample-net\n")
        self.assertEqual((interfaces[0].radio_hardware, interfaces[0].radio_software), (None, None))
        self.assertFalse(main.wifi_radio_off(interfaces))

    def test_hardware_switch_off_also_counts(self):
        interfaces = main.parse_wifi_interfaces(
            "名称 : WLAN 2\n状态 : 已断开连接\n无线电状态 : 硬件 关\n             软件 开\n"
        )
        self.assertTrue(main.wifi_radio_off(interfaces))


class DecodeOutputTests(unittest.TestCase):
    """utf-16 used to be tried before utf-8, turning GBK netsh output into mojibake."""

    def setUp(self):
        self.logger = make_logger("test.decode")

    def test_gbk_netsh_output_decodes(self):
        text = "无线电状态           : 硬件 开\n                             软件 关"
        decoded = main.decode_output(text.encode("gbk"), self.logger)
        self.assertIn("无线电状态", decoded)
        self.assertIn("软件 关", decoded)

    def test_utf8_output_decodes(self):
        text = "接口 'WLAN 2' 已连接"
        self.assertEqual(main.decode_output(text.encode("utf-8"), self.logger), text)

    def test_utf16_with_bom_decodes(self):
        text = "无线电已被关闭"
        decoded = main.decode_output(b"\xff\xfe" + text.encode("utf-16-le"), self.logger)
        self.assertEqual(decoded, text)

    def test_even_length_gbk_is_not_misread_as_utf16(self):
        payload = "系统上有 1 个接口: 名称 WLAN 2".encode("gbk")
        self.assertEqual(len(payload) % 2, 0)
        decoded = main.decode_output(payload, self.logger)
        self.assertIn("系统上有", decoded)
        self.assertNotIn("\u0a0d", decoded)


class PingTests(unittest.TestCase):
    def setUp(self):
        self.logger = make_logger("test.ping")

    def test_unreachable_reply_with_zero_exit_code_is_not_success(self):
        output = (
            "正在 Ping 223.5.5.5 具有 32 字节的数据:\n"
            "来自 10.192.3.0 的回复: 无法访问目标主机。\n"
            "223.5.5.5 的 Ping 统计信息:\n    数据包: 已发送 = 1，已接收 = 1，丢失 = 0 (0% 丢失)，"
        )
        completed = SimpleNamespace(returncode=0, stdout=output, stderr="")
        with patch.object(main, "run_command", return_value=completed):
            self.assertFalse(main.ping_reachable(self.logger))

    def test_real_reply_with_ttl_is_success(self):
        output = "来自 223.5.5.5 的回复: 字节=32 时间=10ms TTL=116"
        completed = SimpleNamespace(returncode=0, stdout=output, stderr="")
        with patch.object(main, "run_command", return_value=completed):
            self.assertTrue(main.ping_reachable(self.logger))

    def test_request_timeout_is_failure(self):
        output = "请求超时。\n数据包: 已发送 = 1，已接收 = 0，丢失 = 1 (100% 丢失)，"
        completed = SimpleNamespace(returncode=0, stdout=output, stderr="")
        with patch.object(main, "run_command", return_value=completed):
            self.assertFalse(main.ping_reachable(self.logger))


class RadioHelperTests(unittest.TestCase):
    def setUp(self):
        self.logger = make_logger("test.radio_helpers")
        repairs._radio_cache.update(state=None, at=0.0)

    def tearDown(self):
        repairs._radio_cache.update(state=None, at=0.0)

    def test_state_is_read_from_the_winrt_probe(self):
        completed = SimpleNamespace(returncode=0, stdout="STATE|Off\n", stderr="")
        with patch.object(repairs, "_run", return_value=completed):
            self.assertEqual(repairs.wifi_radio_state(self.logger), "off")

    def test_missing_radio_is_reported_as_absent(self):
        completed = SimpleNamespace(returncode=0, stdout="NO_WIFI_RADIO\n", stderr="")
        with patch.object(repairs, "_run", return_value=completed):
            self.assertEqual(repairs.wifi_radio_state(self.logger), "absent")

    def test_enable_requires_confirmation(self):
        ok = SimpleNamespace(returncode=0, stdout="SET|Allowed\nAFTER|On\n", stderr="")
        denied = SimpleNamespace(returncode=0, stdout="SET|DeniedByUser\nAFTER|Off\n", stderr="")
        with patch.object(repairs, "_run", return_value=ok):
            self.assertTrue(repairs.enable_wifi_radio(self.logger))
        with patch.object(repairs, "_run", return_value=denied):
            with self.assertLogs(self.logger, level="ERROR") as captured:
                self.assertFalse(repairs.enable_wifi_radio(self.logger))
        self.assertIn("拒绝", "\n".join(captured.output))

    def test_connect_wifi_reports_powered_off_adapter(self):
        output = (
            "功能 WlanGetAvailableNetworkList 返回错误 2150899714:\n"
            "无线局域网接口电源关闭，它不支持请求的操作。"
        )
        completed = SimpleNamespace(returncode=1, stdout=output, stderr="")
        with patch.object(main, "run_command", return_value=completed):
            with self.assertLogs(self.logger, level="ERROR") as captured:
                self.assertFalse(main.connect_wifi("Home", self.logger, None))
        self.assertIn("电源关闭", "\n".join(captured.output))


class RadioRecoveryFlowTests(unittest.TestCase):
    def setUp(self):
        self.logger = make_logger("test.radio_flow")
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.state_file = Path(directory.name) / "state.txt"
        self.stats = {"attempts": 1, "successes": 0, "failures": 0, "dns_repairs": 0,
                      "proxy_cleanups": 0, "wired_repairs": 0, "radio_repairs": 0,
                      "radio_blocked": 0}

    def _recover(self, **overrides):
        params = dict(
            logger=self.logger,
            interface_name=None,
            preferred_profiles=[],
            state_file=self.state_file,
            reconnect_timeout=35,
            check_targets=["https://example.org"],
            cleanup_proxy=True,
            repair_dns_enabled=True,
            dns_check_hosts=["example.org"],
            wifi_recovery=True,
            wired_recovery=True,
            adapter_reset=True,
            repair_stats=self.stats,
        )
        params.update(overrides)
        return main.recover_network(**params)

    def test_radio_restored_before_any_reconnect_attempt(self):
        with patch.object(repairs, "wifi_radio_state", return_value="off"), \
             patch.object(repairs, "enable_wifi_radio", return_value=True) as enable, \
             patch.object(repairs, "get_physical_adapters", return_value=[]), \
             patch.object(main, "is_internet_reachable", return_value=True), \
             patch.object(main, "http_reachable", return_value=True), \
             patch.object(main, "connect_wifi") as connect, \
             patch.object(repairs, "clear_proxy_settings") as proxy:
            self.assertTrue(self._recover(enabled_profiles=["Home"]))
        enable.assert_called_once()
        connect.assert_not_called()
        proxy.assert_not_called()  # radio recovery already restored HTTP
        self.assertEqual(self.stats["radio_repairs"], 1)

    def test_healthy_wired_never_touches_the_radio(self):
        """Reported risk: a working cable must never be disturbed by Wi-Fi work.

        Some laptops/WWAN docks cannot keep LAN and WLAN up together, and the
        OEM switch turns the WLAN radio off while the cable is plugged in.
        That is not a fault: the radio must not be re-enabled, Wi-Fi must not be
        reconnected and no adapter may be reset while the wired link is healthy.
        """
        healthy_wired = [{"Name": "以太网", "Status": "Up", "MediaType": "802.3",
                          "MediaConnectState": "Connected", "IP": "192.168.1.5",
                          "Gateway": "192.168.1.1"}]
        with patch.object(repairs, "wifi_radio_state") as radio, \
             patch.object(repairs, "enable_wifi_radio") as enable, \
             patch.object(repairs, "get_physical_adapters", return_value=healthy_wired), \
             patch.object(repairs, "repair_wired_links") as wired, \
             patch.object(main, "is_internet_reachable", return_value=False), \
             patch.object(main, "http_reachable", return_value=False), \
             patch.object(repairs, "check_dns_resolution", return_value=[]), \
             patch.object(repairs, "clear_proxy_settings", return_value=False), \
             patch.object(main, "get_wifi_interfaces") as interfaces, \
             patch.object(main, "connect_wifi") as connect, \
             patch.object(repairs, "reset_wifi_adapter") as reset, \
             patch.object(main.time, "sleep"):
            self.assertFalse(self._recover(enabled_profiles=["Home"]))
        radio.assert_not_called()
        enable.assert_not_called()
        wired.assert_not_called()
        interfaces.assert_not_called()
        connect.assert_not_called()
        reset.assert_not_called()

    def test_online_cycle_never_calls_any_repair(self):
        """When HTTP already works the loop must not touch adapters at all."""
        import argparse

        with tempfile.TemporaryDirectory() as directory:
            args = argparse.Namespace(
                interval=10, reconnect_timeout=35, interface=None, preferred_profile=[],
                check_target=["https://example.org"], dns_check_hosts=["example.org"],
                state_file=str(Path(directory) / "state.txt"),
                pid_file=str(Path(directory) / "guard.pid"),
                heartbeat_interval=3600, cleanup_proxy=True, repair_dns=True,
                wifi_recovery=True, radio_recovery=True, adapter_reset=True,
                wired_recovery=True, wired_reset=True, dns_reset_interfaces=[],
                disabled_profiles=[], enabled_profiles=["Home"],
            )
            with patch.object(repairs, "acquire_single_instance", return_value=True), \
                 patch.object(repairs, "write_pid_file"), \
                 patch.object(repairs, "install_signal_handlers"), \
                 patch.object(repairs, "register_exit_handler"), \
                 patch.object(repairs, "remove_pid_file"), \
                 patch.object(main, "is_internet_reachable", return_value=True), \
                 patch.object(main, "recover_network") as recover, \
                 patch.object(main.time, "sleep", side_effect=KeyboardInterrupt):
                with self.assertRaises(KeyboardInterrupt):
                    main.monitor_loop(args, self.logger)
        recover.assert_not_called()

    def test_netsh_radio_hint_triggers_recovery_when_winrt_is_unavailable(self):
        interfaces = [main.WifiInterface(name="WLAN 2", state="已断开连接",
                                         radio_hardware="on", radio_software="off")]
        with patch.object(repairs, "wifi_radio_state", return_value=None), \
             patch.object(repairs, "enable_wifi_radio", return_value=True) as enable, \
             patch.object(repairs, "get_physical_adapters", return_value=[]), \
             patch.object(main, "get_wifi_interfaces", return_value=interfaces), \
             patch.object(main, "is_internet_reachable", return_value=True), \
             patch.object(main, "http_reachable", return_value=True), \
             patch.object(main, "connect_wifi") as connect:
            self.assertTrue(self._recover(enabled_profiles=["Home"],
                                          cleanup_proxy=False, repair_dns_enabled=False))
        enable.assert_called_once()
        connect.assert_not_called()
        self.assertEqual(self.stats["radio_repairs"], 1)

    def test_radio_off_allows_adapter_reset_despite_participation_list(self):
        dead_wired = [{"Name": "以太网", "Status": "Disconnected", "MediaType": "802.3",
                       "MediaConnectState": "Disconnected", "IP": "202.38.206.207",
                       "Gateway": "202.38.206.254"}]
        with patch.object(repairs, "wifi_radio_state", return_value="off"), \
             patch.object(repairs, "enable_wifi_radio", return_value=False), \
             patch.object(main, "is_internet_reachable", return_value=False), \
             patch.object(main, "http_reachable", return_value=False), \
             patch.object(repairs, "check_dns_resolution", return_value=[]), \
             patch.object(repairs, "clear_proxy_settings", return_value=False), \
             patch.object(repairs, "get_physical_adapters", return_value=dead_wired), \
             patch.object(repairs, "repair_wired_links") as wired, \
             patch.object(main, "get_wifi_interfaces", return_value=[]), \
             patch.object(main, "get_saved_profiles", return_value=["Home"]), \
             patch.object(main, "connect_wifi", return_value=False), \
             patch.object(repairs, "reset_wifi_adapter", return_value=False) as reset, \
             patch.object(main.time, "sleep"):
            self.assertFalse(self._recover(enabled_profiles=["Home"]))
        wired.assert_not_called()  # unplugged cable never burns the budget
        reset.assert_called_once()  # radio is off: nothing to steal
        self.assertEqual(self.stats["radio_blocked"], 1)

    def test_participation_list_still_blocks_reset_when_radio_is_on(self):
        with patch.object(repairs, "wifi_radio_state", return_value="on"), \
             patch.object(repairs, "enable_wifi_radio") as enable, \
             patch.object(main, "is_internet_reachable", return_value=False), \
             patch.object(main, "http_reachable", return_value=False), \
             patch.object(repairs, "check_dns_resolution", return_value=[]), \
             patch.object(repairs, "clear_proxy_settings", return_value=False), \
             patch.object(repairs, "get_physical_adapters", return_value=[]), \
             patch.object(main, "get_wifi_interfaces", return_value=[]), \
             patch.object(main, "get_saved_profiles", return_value=["Home"]), \
             patch.object(main, "connect_wifi", return_value=False), \
             patch.object(repairs, "reset_wifi_adapter") as reset, \
             patch.object(main.time, "sleep"):
            self.assertFalse(self._recover(enabled_profiles=["Home"]))
        enable.assert_not_called()
        reset.assert_not_called()

    def test_radio_off_without_profiles_still_resets_adapter(self):
        with patch.object(repairs, "wifi_radio_state", return_value="off"), \
             patch.object(repairs, "enable_wifi_radio", return_value=False), \
             patch.object(main, "is_internet_reachable", return_value=False), \
             patch.object(main, "http_reachable", return_value=False), \
             patch.object(repairs, "check_dns_resolution", return_value=[]), \
             patch.object(repairs, "clear_proxy_settings", return_value=False), \
             patch.object(repairs, "get_physical_adapters", return_value=[]), \
             patch.object(main, "get_wifi_interfaces", return_value=[]), \
             patch.object(main, "get_saved_profiles", return_value=[]), \
             patch.object(repairs, "reset_wifi_adapter", return_value=False) as reset, \
             patch.object(main.time, "sleep"):
            self.assertFalse(self._recover(enabled_profiles=["Home"]))
        reset.assert_called_once()

    def test_radio_recovery_switch_is_respected(self):
        with patch.object(repairs, "wifi_radio_state") as state, \
             patch.object(repairs, "enable_wifi_radio") as enable, \
             patch.object(main, "is_internet_reachable", return_value=False), \
             patch.object(main, "http_reachable", return_value=False), \
             patch.object(repairs, "get_physical_adapters", return_value=[]), \
             patch.object(main, "get_wifi_interfaces", return_value=[]), \
             patch.object(main, "get_saved_profiles", return_value=["Home"]), \
             patch.object(main, "connect_wifi", return_value=False), \
             patch.object(main.time, "sleep"):
            self.assertFalse(self._recover(enabled_profiles=["Home"], radio_recovery=False,
                                           cleanup_proxy=False, repair_dns_enabled=False))
        state.assert_not_called()
        enable.assert_not_called()


class DeadWiredLinkTests(unittest.TestCase):
    def setUp(self):
        self.logger = make_logger("test.dead_wired")

    def test_media_down_adapter_is_recognized(self):
        for status in ("Disconnected", "已断开连接", "Disabled"):
            with self.subTest(status=status):
                self.assertTrue(repairs.is_wired_media_down({"Status": status}))
        self.assertFalse(repairs.is_wired_media_down({"Status": "Up", "MediaConnectState": "Connected"}))

    def test_unplugged_cable_is_not_renewed(self):
        adapters = [{"Name": "以太网", "Status": "Disconnected", "MediaType": "802.3"}]
        with patch.object(repairs, "renew_dhcp") as renew:
            self.assertFalse(repairs.repair_wired_links(self.logger, adapters=adapters))
        renew.assert_not_called()

    def test_static_ip_adapter_is_reported_and_not_reset(self):
        completed = SimpleNamespace(returncode=0, stdout="没有为 DHCP 启用适配器 以太网。", stderr="")
        with patch.object(repairs, "_run", return_value=completed), \
             patch.object(repairs.time, "sleep"):
            self.assertFalse(repairs.renew_dhcp(self.logger, "以太网"))

    def test_media_disconnected_renew_is_not_success(self):
        completed = SimpleNamespace(
            returncode=0,
            stdout="不能在 以太网 上执行任何操作，它已断开媒体连接。",
            stderr="",
        )
        with patch.object(repairs, "_run", return_value=completed), \
             patch.object(repairs.time, "sleep"):
            self.assertFalse(repairs.renew_dhcp(self.logger, "以太网"))

    def test_reset_is_skipped_without_admin_rights(self):
        broken = [{"Name": "以太网", "Status": "Up", "MediaType": "802.3",
                   "IP": "169.254.3.4", "Gateway": None}]
        with patch.object(repairs, "get_physical_adapters", return_value=broken), \
             patch.object(repairs, "renew_dhcp", return_value=True), \
             patch.object(repairs, "has_admin_privileges", return_value=False), \
             patch.object(repairs, "_reset_named_adapter") as reset:
            self.assertFalse(repairs.repair_wired_links(self.logger, allow_reset=True, adapters=broken))
        reset.assert_not_called()

    def test_plain_renew_failure_still_allows_reset(self):
        broken = [{"Name": "以太网", "Status": "Up", "MediaType": "802.3",
                   "IP": "169.254.3.4", "Gateway": None}]
        with patch.object(repairs, "get_physical_adapters", return_value=broken), \
             patch.object(repairs, "renew_dhcp", return_value=False), \
             patch.object(repairs, "has_admin_privileges", return_value=True), \
             patch.object(repairs, "_reset_named_adapter", return_value=True) as reset:
            repairs.repair_wired_links(self.logger, allow_reset=True, adapters=broken)
        reset.assert_called_once()

    def test_apipa_adapter_with_admin_is_reset(self):
        broken = [{"Name": "以太网", "Status": "Up", "MediaType": "802.3",
                   "IP": "169.254.3.4", "Gateway": None}]
        fixed = [{"Name": "以太网", "Status": "Up", "MediaType": "802.3",
                  "IP": "192.168.1.5", "Gateway": "192.168.1.1"}]
        with patch.object(repairs, "get_physical_adapters", side_effect=[broken, fixed]), \
             patch.object(repairs, "renew_dhcp", return_value=True), \
             patch.object(repairs, "has_admin_privileges", return_value=True), \
             patch.object(repairs, "_reset_named_adapter", return_value=True) as reset:
            self.assertTrue(repairs.repair_wired_links(self.logger, allow_reset=True, adapters=broken))
        reset.assert_called_once_with(self.logger, "以太网")


if __name__ == "__main__":
    unittest.main()

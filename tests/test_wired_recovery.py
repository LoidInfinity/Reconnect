"""Coverage for wired-first recovery and wired/wireless coexist behaviour.

The requirement is: never skip wireless just because a cable is present, and
when everything is down, repair the wired link before the wireless one.
"""
import logging
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import main
import repairs


class AdapterClassificationTests(unittest.TestCase):
    def test_wired_and_wireless_are_told_apart(self):
        wifi = {"Name": "WLAN", "Status": "Up", "MediaType": "Native 802.11",
                "IP": "169.254.10.20", "Gateway": None}
        wired = {"Name": "以太网", "Status": "Up", "MediaType": "802.3",
                 "IP": "192.168.1.5", "Gateway": "192.168.1.1"}
        self.assertFalse(repairs.is_wired(wifi))
        self.assertTrue(repairs.is_wired(wired))

    def test_apipa_lease_is_unhealthy(self):
        self.assertTrue(repairs.is_apipa("169.254.206.18"))
        self.assertFalse(repairs.is_apipa("192.168.1.5"))
        self.assertFalse(repairs.is_apipa(None))

    def test_link_health_needs_address_and_gateway(self):
        healthy = {"Status": "Up", "IP": "192.168.1.5", "Gateway": "192.168.1.1"}
        apipa = {"Status": "Up", "IP": "169.254.1.2", "Gateway": None}
        no_gateway = {"Status": "Up", "IP": "192.168.1.5", "Gateway": None}
        down = {"Status": "Disconnected", "IP": "192.168.1.5", "Gateway": "192.168.1.1"}
        self.assertTrue(repairs.is_link_healthy(healthy))
        self.assertFalse(repairs.is_link_healthy(apipa))
        self.assertFalse(repairs.is_link_healthy(no_gateway))
        self.assertFalse(repairs.is_link_healthy(down))

    def test_virtual_adapters_are_filtered_out(self):
        payload = (
            '[{"Name":"WLAN","Status":"Up","MediaType":"Native 802.11",'
            '"IP":"169.254.1.2","Gateway":null}]'
        )
        completed = type("R", (), {"stdout": payload, "returncode": 0})()
        with patch.object(repairs, "_run", return_value=completed):
            adapters = repairs.get_physical_adapters(logging.getLogger("t"))
        self.assertEqual(len(adapters), 1)
        self.assertEqual(adapters[0]["Name"], "WLAN")
        self.assertIn("VMware", repairs.VIRTUAL_ADAPTER_PATTERN)


class WiredFirstRecoveryTests(unittest.TestCase):
    BROKEN_WIRED = [{"Name": "以太网", "Status": "Up", "MediaType": "802.3",
                     "IP": "169.254.3.4", "Gateway": None}]
    HEALTHY_WIRED = [{"Name": "以太网", "Status": "Up", "MediaType": "802.3",
                      "IP": "192.168.1.5", "Gateway": "192.168.1.1"}]
    WIFI_ONLY = [{"Name": "WLAN", "Status": "Up", "MediaType": "Native 802.11",
                  "IP": "169.254.1.2", "Gateway": None}]

    def setUp(self):
        self.logger = logging.getLogger("test.wired_recovery")
        self.logger.handlers = [logging.NullHandler()]
        self.logger.propagate = False
        # Keep the state file out of the project directory.
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.state_file = Path(directory.name) / "state.txt"

    def _recover(self, **overrides):
        params = dict(
            logger=self.logger,
            interface_name=None,
            preferred_profiles=[],
            state_file=self.state_file,
            reconnect_timeout=5,
            check_targets=list(main.DEFAULT_CHECK_TARGETS),
            cleanup_proxy=False,
            repair_dns_enabled=False,
            wifi_recovery=True,
            wired_recovery=True,
            adapter_reset=False,
        )
        params.update(overrides)
        with patch.object(repairs, "wifi_radio_state", return_value="on"):
            return main.recover_network(**params)

    def test_wired_repair_runs_before_wireless(self):
        with patch.object(repairs, "get_physical_adapters", return_value=self.BROKEN_WIRED), \
             patch.object(main, "http_reachable", return_value=True), \
             patch.object(repairs, "repair_wired_links", return_value=True) as wired, \
             patch.object(main, "connect_wifi") as connect:
            self.assertTrue(self._recover())
        wired.assert_called_once()
        connect.assert_not_called()

    def test_healthy_wired_skips_wireless(self):
        # A working cable must never be disturbed: no Wi-Fi reconnect at all.
        with patch.object(repairs, "get_physical_adapters", return_value=self.HEALTHY_WIRED), \
             patch.object(main, "http_reachable", return_value=False), \
             patch.object(repairs, "repair_wired_links") as wired, \
             patch.object(main, "connect_wifi") as connect:
            self.assertFalse(self._recover())
        wired.assert_not_called()
        connect.assert_not_called()

    def test_no_wired_adapter_falls_back_to_wireless(self):
        with patch.object(repairs, "get_physical_adapters", return_value=self.WIFI_ONLY), \
             patch.object(main, "http_reachable", side_effect=[False, True]), \
             patch.object(main, "ping_reachable", return_value=False), \
             patch.object(main, "get_wifi_interfaces", return_value=[]), \
             patch.object(main, "get_saved_profiles", return_value=["Home"]), \
             patch.object(main, "connect_wifi", return_value=True) as connect, \
             patch.object(main.time, "sleep"):
            self.assertTrue(self._recover())
        connect.assert_called_once_with("Home", self.logger, None)

    def test_wired_repaired_but_http_down_falls_through_to_wireless(self):
        with patch.object(repairs, "get_physical_adapters", return_value=self.BROKEN_WIRED), \
             patch.object(main, "http_reachable", side_effect=[False, True]), \
             patch.object(repairs, "repair_wired_links", return_value=True) as wired, \
             patch.object(main, "ping_reachable", return_value=False), \
             patch.object(main, "get_wifi_interfaces", return_value=[]), \
             patch.object(main, "get_saved_profiles", return_value=["Home"]), \
             patch.object(main, "connect_wifi", return_value=True) as connect, \
             patch.object(main.time, "sleep"):
            self.assertTrue(self._recover())
        wired.assert_called_once()
        connect.assert_called_once()

    def test_wired_disabled_skips_wired_probe(self):
        with patch.object(repairs, "get_physical_adapters") as adapters, \
             patch.object(main, "http_reachable", return_value=False), \
             patch.object(repairs, "repair_wired_links") as wired, \
             patch.object(main, "ping_reachable", return_value=False), \
             patch.object(main, "get_wifi_interfaces", return_value=[]), \
             patch.object(main, "get_saved_profiles", return_value=["Home"]), \
             patch.object(main, "connect_wifi", return_value=False), \
             patch.object(main.time, "sleep"):
            self.assertFalse(self._recover(wired_recovery=False))
        adapters.assert_not_called()
        wired.assert_not_called()

    def test_wired_repair_counted_in_stats(self):
        stats = {"attempts": 1, "successes": 0, "failures": 0,
                 "dns_repairs": 0, "proxy_cleanups": 0, "wired_repairs": 0}
        with patch.object(repairs, "get_physical_adapters", return_value=self.BROKEN_WIRED), \
             patch.object(main, "http_reachable", return_value=True), \
             patch.object(repairs, "repair_wired_links", return_value=True):
            self.assertTrue(self._recover(repair_stats=stats))
        self.assertEqual(stats["wired_repairs"], 1)


class RepairWiredLinksTests(unittest.TestCase):
    def setUp(self):
        self.logger = logging.getLogger("test.wired_links")
        self.logger.handlers = [logging.NullHandler()]
        self.logger.propagate = False

    def test_healthy_wired_needs_no_repair(self):
        adapters = [{"Name": "以太网", "Status": "Up", "MediaType": "802.3",
                     "IP": "192.168.1.5", "Gateway": "192.168.1.1"}]
        with patch.object(repairs, "get_physical_adapters", return_value=adapters), \
             patch.object(repairs, "renew_dhcp") as renew:
            self.assertFalse(repairs.repair_wired_links(self.logger))
        renew.assert_not_called()

    def test_apipa_wired_triggers_dhcp_renew(self):
        broken = [{"Name": "以太网", "Status": "Up", "MediaType": "802.3",
                   "IP": "169.254.3.4", "Gateway": None}]
        fixed = [{"Name": "以太网", "Status": "Up", "MediaType": "802.3",
                  "IP": "192.168.1.5", "Gateway": "192.168.1.1"}]
        with patch.object(repairs, "get_physical_adapters", side_effect=[broken, fixed]), \
             patch.object(repairs, "renew_dhcp", return_value=True) as renew:
            self.assertTrue(repairs.repair_wired_links(self.logger))
        renew.assert_called_once_with(self.logger, "以太网", unittest.mock.ANY)

    def test_no_wired_adapter_is_skipped(self):
        wifi_only = [{"Name": "WLAN", "Status": "Up", "MediaType": "Native 802.11",
                      "IP": "192.168.1.9", "Gateway": "192.168.1.1"}]
        with patch.object(repairs, "get_physical_adapters", return_value=wifi_only), \
             patch.object(repairs, "renew_dhcp") as renew:
            self.assertFalse(repairs.repair_wired_links(self.logger))
        renew.assert_not_called()


class HealthyWiredBlocksWirelessTests(unittest.TestCase):
    """可用的网线存在时，不允许出现任何无线动作（包括读取/打开无线电）。

    背景：部分笔记本/扩展坞在插网线时会被 OEM 策略关闭 WLAN 无线电，也就是
    “有线和无线只能二选一”。那种关闭是策略而不是故障；把它当故障强行打开
    无线电，可能反过来断开正在工作的有线。因此“有线健康”必须在任何无线
    步骤之前判定，并拦截全部无线入口。
    """

    HEALTHY_WIRED = [{"Name": "以太网", "Status": "Up", "MediaType": "802.3",
                      "MediaConnectState": "Connected",
                      "IP": "192.168.1.5", "Gateway": "192.168.1.1"}]

    def setUp(self):
        self.logger = logging.getLogger("test.healthy_wired_blocks_wireless")
        self.logger.handlers = [logging.NullHandler()]
        self.logger.propagate = False
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.state_file = Path(directory.name) / "state.txt"

    def _recover(self, **overrides):
        params = dict(
            logger=self.logger,
            interface_name=None,
            preferred_profiles=[],
            state_file=self.state_file,
            reconnect_timeout=5,
            check_targets=list(main.DEFAULT_CHECK_TARGETS),
            cleanup_proxy=False,
            repair_dns_enabled=False,
            wifi_recovery=True,
            radio_recovery=True,
            wired_recovery=True,
            adapter_reset=True,
        )
        params.update(overrides)
        return main.recover_network(**params)

    def test_no_wireless_entry_point_is_touched(self):
        with patch.object(repairs, "get_physical_adapters", return_value=self.HEALTHY_WIRED), \
             patch.object(repairs, "wifi_radio_state") as radio_state, \
             patch.object(repairs, "enable_wifi_radio") as radio_on, \
             patch.object(repairs, "repair_wired_links") as wired_repair, \
             patch.object(repairs, "reset_wifi_adapter") as adapter_reset, \
             patch.object(main, "get_wifi_interfaces") as interfaces, \
             patch.object(main, "get_saved_profiles") as profiles, \
             patch.object(main, "connect_wifi") as connect, \
             patch.object(main, "http_reachable", return_value=False), \
             patch.object(main, "ping_reachable") as ping:
            self.assertFalse(self._recover(enabled_profiles=["Home"]))
        for mock in (radio_state, radio_on, wired_repair, adapter_reset,
                     interfaces, profiles, connect, ping):
            mock.assert_not_called()

    def test_dns_repair_still_runs_but_never_falls_through_to_wireless(self):
        with patch.object(repairs, "get_physical_adapters", return_value=self.HEALTHY_WIRED), \
             patch.object(repairs, "check_dns_resolution", side_effect=[["example.org"], []]), \
             patch.object(repairs, "repair_dns", return_value=True) as dns_repair, \
             patch.object(repairs, "get_up_interface_names", return_value=["以太网"]), \
             patch.object(repairs, "wifi_radio_state") as radio_state, \
             patch.object(main, "is_internet_reachable", return_value=False), \
             patch.object(main, "get_wifi_interfaces") as interfaces, \
             patch.object(main, "connect_wifi") as connect:
            self.assertFalse(self._recover(enabled_profiles=["Home"], repair_dns_enabled=True,
                                           dns_check_hosts=["example.org"]))
        dns_repair.assert_called_once()
        for mock in (radio_state, interfaces, connect):
            mock.assert_not_called()


if __name__ == "__main__":
    unittest.main()

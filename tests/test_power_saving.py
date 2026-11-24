"""Adapter power-saving switch (Device Manager → Power Management)."""
import logging
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import repairs


def make_logger(name="test.power"):
    logger = logging.getLogger(name)
    logger.handlers = [logging.NullHandler()]
    logger.propagate = False
    return logger


class AdapterPowerSavingTests(unittest.TestCase):
    def setUp(self):
        self.logger = make_logger("test.power.adapter")

    def test_requires_admin(self):
        with patch.object(repairs, "has_admin_privileges", return_value=False), \
             patch.object(repairs, "_run") as run:
            self.assertFalse(repairs.disable_adapter_power_saving(self.logger))
        run.assert_not_called()

    def test_adapter_name_is_passed_through_environment(self):
        captured = {}

        def fake_run(command, logger, timeout=None, env=None):
            captured["command"] = command
            captured["env"] = env
            return SimpleNamespace(stdout="OK:以太网", returncode=0, stderr="")

        hostile = "Ethernet'; calc; '"
        with patch.object(repairs, "has_admin_privileges", return_value=True), \
             patch.object(repairs, "_run", side_effect=fake_run):
            self.assertTrue(repairs.disable_adapter_power_saving(self.logger, hostile))
        script = captured["command"][-1]
        self.assertNotIn("calc", script)
        self.assertIn("$env:NG_ADAPTER_NAME", script)
        self.assertIn("Disable-NetAdapterPowerManagement", script)
        self.assertEqual(captured["env"]["NG_ADAPTER_NAME"], hostile)

    def test_access_denied_is_reported(self):
        with patch.object(repairs, "has_admin_privileges", return_value=True), \
             patch.object(repairs, "_run", return_value=SimpleNamespace(
                 stdout="ERROR:WLAN:Access is denied", returncode=0, stderr="")):
            self.assertFalse(repairs.disable_adapter_power_saving(self.logger))

    def test_no_adapter_is_not_an_error(self):
        with patch.object(repairs, "has_admin_privileges", return_value=True), \
             patch.object(repairs, "_run", return_value=SimpleNamespace(
                 stdout="NO_ADAPTER", returncode=0, stderr="")):
            self.assertFalse(repairs.disable_adapter_power_saving(self.logger))


if __name__ == "__main__":
    unittest.main()

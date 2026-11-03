import io
import json
from pathlib import Path
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError

import app_info
import updates


class UpdateTests(unittest.TestCase):
    def test_semver_prerelease_order(self):
        versions = ["0.1.0-alpha.1", "0.1.0-alpha.2", "0.1.0-alpha.10", "0.1.0-beta.1", "0.1.0-rc.1", "0.1.0", "0.2.0"]
        self.assertEqual(sorted(reversed(versions), key=updates.version_key), versions)
        self.assertEqual(updates.version_key("v0.1.0+build"), updates.version_key("0.1.0"))

    def test_alpha_updates_and_drafts(self):
        releases = [dict(tag_name="v0.1.0-alpha.2", prerelease=True), dict(tag_name="v9.0.0", draft=True), dict(tag_name="bad")]
        result = updates.select_release(releases)
        self.assertTrue(result["available"])
        self.assertEqual(result["latest"], "v0.1.0-alpha.2")
        self.assertTrue(result["url"].startswith(app_info.RELEASES_URL + "/tag/"))
        self.assertFalse(updates.select_release(releases, "0.1.0")["available"])

    def test_empty_same_and_older(self):
        self.assertIsNone(updates.select_release([])["latest"])
        for version in ("0.1.0-alpha.1", "0.0.9"):
            self.assertFalse(updates.select_release([dict(tag_name=version)])["available"])

    def test_api_success_and_errors(self):
        with patch.object(updates, "urlopen", return_value=io.BytesIO(json.dumps([dict(tag_name="v0.2.0")]).encode())):
            self.assertTrue(updates.check_updates()["available"])
        for error in (URLError("offline"), HTTPError("url", 403, "limit", {}, None)):
            with patch.object(updates, "urlopen", side_effect=error), self.assertRaises(RuntimeError):
                updates.check_updates()
        with patch.object(updates, "urlopen", return_value=io.BytesIO(b"{}")), self.assertRaises(RuntimeError):
            updates.check_updates()

    def test_packaged_worker_invokes_executable(self):
        with patch.object(app_info, "FROZEN", True):
            command = app_info.worker_command(Path("config.json"))
            self.assertEqual(command[1:], ["--worker", "--config", "config.json"])
            self.assertNotIn("gui.py", command)


if __name__ == "__main__":
    unittest.main()

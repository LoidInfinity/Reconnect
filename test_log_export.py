"""Coverage for the log-bundling path users use when reporting problems."""
import tempfile
import unittest
import zipfile
from pathlib import Path

import repairs


def _touch(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


class CollectLogFilesTests(unittest.TestCase):
    def test_picks_current_and_rotated_files_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            log = _touch(directory / "network_guard.log", "current\n")
            _touch(directory / "network_guard.log.1", "older\n")
            _touch(directory / "network_guard.log.2", "oldest\n")
            _touch(directory / "network_guard.log.bak", "ignore me\n")
            _touch(directory / "unrelated.log", "ignore me\n")

            names = [item.name for item in repairs.collect_log_files(log)]

            self.assertIn("network_guard.log", names)
            self.assertIn("network_guard.log.1", names)
            self.assertIn("network_guard.log.2", names)
            self.assertNotIn("network_guard.log.bak", names)
            self.assertNotIn("unrelated.log", names)

    def test_missing_directory_is_tolerated(self):
        self.assertEqual(repairs.collect_log_files(Path("no") / "such" / "x.log"), [])

    def test_max_files_caps_the_list(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            log = _touch(directory / "network_guard.log", "a\n")
            for index in range(1, 6):
                _touch(directory / f"network_guard.log.{index}", f"{index}\n")

            self.assertEqual(len(repairs.collect_log_files(log, max_files=3)), 3)


class ExportLogsTests(unittest.TestCase):
    def test_bundle_contains_logs_and_readme_with_contact(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = _touch(Path(tmp) / "network_guard.log", "2026-11-21 | INFO | start\n")

            archive = repairs.export_logs(log, "0.1.0-beta.1")

            self.assertIsNotNone(archive)
            self.assertTrue(archive.exists())
            self.assertTrue(archive.name.startswith("NetworkGuard-logs-"))
            with zipfile.ZipFile(archive) as bundle:
                names = bundle.namelist()
                self.assertIn("README.txt", names)
                self.assertIn("network_guard.log", names)
                note = bundle.read("README.txt").decode("utf-8")
            self.assertIn(repairs.SUPPORT_EMAIL, note)
            self.assertIn("0.1.0-beta.1", note)

    def test_no_log_files_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(repairs.export_logs(Path(tmp) / "missing.log"))

    def test_config_and_secrets_never_enter_the_bundle(self):
        """配置文件含个人 Wi-Fi 名称，绝不能被打包带走。"""
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            log = _touch(directory / "network_guard.log", "line\n")
            _touch(directory / "network_guard_config.json",
                   '{"enabled_profiles": ["MyHomeNetwork"]}')

            archive = repairs.export_logs(log, "test")

            with zipfile.ZipFile(archive) as bundle:
                self.assertNotIn("network_guard_config.json", bundle.namelist())
                payload = "".join(
                    bundle.read(name).decode("utf-8", "ignore") for name in bundle.namelist()
                )
            self.assertNotIn("MyHomeNetwork", payload)

    def test_destination_directory_receives_the_archive(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            log = _touch(directory / "network_guard.log", "line\n")
            target = directory / "out"

            archive = repairs.export_logs(log, "test", destination=target)

            self.assertEqual(archive.parent, target)
            self.assertTrue(archive.exists())

    def test_repeated_exports_do_not_overwrite(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = _touch(Path(tmp) / "network_guard.log", "line\n")

            first = repairs.export_logs(log, "test")
            second = repairs.export_logs(log, "test")

            self.assertNotEqual(first.name, second.name)


if __name__ == "__main__":
    unittest.main()

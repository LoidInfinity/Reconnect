"""In-app update helpers: asset choice, checksum verification, apply script."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import updater


def asset(name, size=10):
    return {"name": name, "browser_download_url": "https://example.org/" + name, "size": size}


SETUP = asset("NetworkGuard-Setup-0.2.0.exe", 100)
SETUP_SHA = asset("NetworkGuard-Setup-0.2.0.exe.sha256", 70)
ZIP = asset("NetworkGuard-0.2.0-windows-x64.zip", 100)
ZIP_SHA = asset("NetworkGuard-0.2.0-windows-x64.zip.sha256", 70)


class AssetTests(unittest.TestCase):
    def test_installer_and_portable_assets(self):
        chosen = updater.select_assets([SETUP, SETUP_SHA, ZIP, ZIP_SHA], "installer")
        self.assertEqual(chosen["name"], SETUP["name"])
        self.assertEqual(chosen["checksum"]["name"], SETUP_SHA["name"])
        chosen = updater.select_assets([SETUP, SETUP_SHA, ZIP, ZIP_SHA], "portable")
        self.assertEqual(chosen["name"], ZIP["name"])

    def test_missing_checksum_or_mode_is_rejected(self):
        with self.assertRaises(updater.UpdateError):
            updater.select_assets([SETUP], "installer")
        with self.assertRaises(updater.UpdateError):
            updater.select_assets([SETUP, SETUP_SHA], "source")
        with self.assertRaises(updater.UpdateError):
            updater.select_assets([SETUP, SETUP_SHA], "portable")

    def test_sha_parsing(self):
        value = "a" * 64
        self.assertEqual(updater.parse_sha256(value + "  file.exe\n"), value)
        for bad in ("", "xyz", "a" * 63):
            with self.subTest(bad=bad), self.assertRaises(updater.UpdateError):
                updater.parse_sha256(bad)


class FetchTests(unittest.TestCase):
    def test_checksum_mismatch_is_rejected(self):
        payload = b"not the expected file"
        expected = updater.hashlib.sha256(b"different").hexdigest()

        def fake_download(asset, destination, progress=None, cancelled=None):
            destination.parent.mkdir(parents=True, exist_ok=True)
            if asset["name"].endswith(".sha256"):
                destination.write_text(expected + "  file\n", encoding="utf-8")
            else:
                destination.write_bytes(payload)
            return destination

        with tempfile.TemporaryDirectory() as directory, \
             patch.object(updater, "download", side_effect=fake_download):
            with self.assertRaises(updater.UpdateError):
                updater.fetch_update({"assets": [ZIP, ZIP_SHA]}, "portable", Path(directory))

    def test_matching_checksum_returns_package(self):
        payload = b"payload"
        expected = updater.hashlib.sha256(payload).hexdigest()

        def fake_download(asset, destination, progress=None, cancelled=None):
            destination.parent.mkdir(parents=True, exist_ok=True)
            if asset["name"].endswith(".sha256"):
                destination.write_text(expected + "  file\n", encoding="utf-8")
            else:
                destination.write_bytes(payload)
            return destination

        with tempfile.TemporaryDirectory() as directory, \
             patch.object(updater, "download", side_effect=fake_download):
            package = updater.fetch_update({"assets": [ZIP, ZIP_SHA]}, "portable", Path(directory))
            self.assertEqual(package.read_bytes(), payload)


class ScriptTests(unittest.TestCase):
    def test_installer_script_waits_and_runs_silently(self):
        with tempfile.TemporaryDirectory() as directory:
            workdir = Path(directory)
            package = workdir / "setup.exe"
            package.write_bytes(b"x")
            script = updater.build_apply_script(
                package, "installer", Path(r"C:\Apps\NetworkGuard"),
                Path(r"C:\Apps\NetworkGuard\NetworkGuard.exe"), [111, 222], workdir)
            text = script.read_text(encoding="utf-8-sig")
            self.assertIn("$targets = @(111,222)", text)
            self.assertIn("/VERYSILENT", text)
            self.assertIn("Start-Process -FilePath", text)
            self.assertIn("'C:\\Apps\\NetworkGuard\\NetworkGuard.exe'", text)

    def test_portable_script_extracts_and_copies(self):
        with tempfile.TemporaryDirectory() as directory:
            workdir = Path(directory)
            package = workdir / "update.zip"
            package.write_bytes(b"x")
            script = updater.build_apply_script(
                package, "portable", Path(r"D:\O'Brien"), Path(r"D:\O'Brien\NetworkGuard.exe"), [7], workdir)
            text = script.read_text(encoding="utf-8-sig")
            self.assertIn("Expand-Archive", text)
            self.assertIn("Copy-Item", text)
            self.assertIn("Join-Path $extract 'NetworkGuard'", text)
            self.assertIn("'D:\\O''Brien'", text)


class ModeTests(unittest.TestCase):
    def test_source_mode_when_not_frozen(self):
        with patch.object(updater.sys, "frozen", False, create=True):
            self.assertEqual(updater.install_mode(), "source")

    def test_installer_detected_by_uninstaller(self):
        with tempfile.TemporaryDirectory() as directory:
            exe = Path(directory) / "NetworkGuard.exe"
            exe.write_bytes(b"x")
            (Path(directory) / "unins000.exe").write_bytes(b"x")
            with patch.object(updater.sys, "frozen", True, create=True), \
                 patch.object(updater.sys, "executable", str(exe)):
                self.assertEqual(updater.install_mode(), "installer")


if __name__ == "__main__":
    unittest.main()

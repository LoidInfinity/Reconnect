"""Packaging regression: the shipped bundle must expose both GUI and CLI.

The rc.2 installer only contained the windowed ``NetworkGuard.exe``; the
``start_cli.bat`` shipped in the source tree pointed at ``src/main.py``, which
does not exist in the package, so installed users had no command-line entry.
These tests pin the CLI entry into the build script and the installer.
"""
from pathlib import Path
import unittest

import build

ROOT = Path(__file__).resolve().parent.parent


class PackagingEntryPointTests(unittest.TestCase):
    def test_build_script_compiles_a_console_cli(self):
        source = (ROOT / "src" / "build.py").read_text(encoding="utf-8")
        self.assertIn("NetworkGuardCLI", source)
        self.assertIn("--console", source)
        self.assertIn("start_cli.bat", source)

    def test_cli_launcher_starts_the_console_exe(self):
        self.assertIn("NetworkGuardCLI.exe", build.CLI_LAUNCHER)
        self.assertIn("%*", build.CLI_LAUNCHER)

    def test_installer_ships_and_shortcuts_the_cli(self):
        installer = (ROOT / "src" / "installer.iss").read_text(encoding="utf-8")
        self.assertIn('Source: "{#SourceRoot}\\*"', installer)
        self.assertIn(r"{app}\start_cli.bat", installer)
        self.assertIn("命令行", installer)

    def test_readme_documents_both_packaged_entries(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("NetworkGuardCLI.exe", readme)
        self.assertIn("start_cli.bat", readme)


class IconAssetTests(unittest.TestCase):
    ASSETS = ROOT / "assets"

    def test_required_icon_files_exist(self):
        for name in ("app-icon.png", "cli-icon.png", "NetworkGuard.ico", "NetworkGuardCLI.ico"):
            with self.subTest(name=name):
                path = self.ASSETS / name
                self.assertTrue(path.is_file(), name)
                self.assertGreater(path.stat().st_size, 0, name)

    def test_ico_files_are_real_icons(self):
        for name in ("NetworkGuard.ico", "NetworkGuardCLI.ico"):
            with self.subTest(name=name), (self.ASSETS / name).open("rb") as handle:
                self.assertEqual(handle.read(4), b"\x00\x00\x01\x00")

    def test_readme_starts_with_the_project_icon(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        head = readme.lstrip()
        self.assertTrue(head.startswith('<p align="center">'), head[:60])
        before_title = readme.split("# Network Guard", 1)[0]
        self.assertIn("assets/app-icon.png", before_title)

    def test_build_and_installer_wire_the_icons(self):
        build = (ROOT / "src" / "build.py").read_text(encoding="utf-8")
        self.assertIn("NetworkGuard.ico", build)
        self.assertIn("NetworkGuardCLI.ico", build)
        self.assertEqual(build.count("--icon"), 2)
        installer = (ROOT / "src" / "installer.iss").read_text(encoding="utf-8")
        self.assertIn(r"SetupIconFile=..\assets\NetworkGuard.ico", installer)
        self.assertIn(r'IconFilename: "{app}\NetworkGuardCLI.exe"', installer)

    def test_resource_path_finds_the_bundled_icon(self):
        import app_info

        self.assertTrue(app_info.resource_path("assets/NetworkGuard.ico").is_file())

    def test_gui_applies_the_window_icon(self):
        import gui

        calls = []

        class Window:
            def iconbitmap(self, **kwargs):
                calls.append(kwargs)

        gui.apply_window_icon(Window())
        self.assertEqual(len(calls), 1)
        self.assertIn("NetworkGuard.ico", calls[0]["default"])


if __name__ == "__main__":
    unittest.main()

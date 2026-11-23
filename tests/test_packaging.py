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


if __name__ == "__main__":
    unittest.main()

"""Build a Windows x64 folder distribution and versioned ZIP."""
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import sys

from app_info import VERSION

ROOT = Path(__file__).resolve().parent


def main():
    if sys.platform != "win32":
        raise SystemExit("Build on Windows with a 64-bit Python installation.")
    build = ROOT / "build"
    build.mkdir(exist_ok=True)
    version_file = build / "version_info.txt"
    core, _, prerelease = VERSION.partition("-")
    revision = int(prerelease.rsplit(".", 1)[-1]) if prerelease else 0
    windows_version = tuple(int(part) for part in core.split(".")) + (revision,)
    version_file.write_text(f'''VSVersionInfo(
  ffi=FixedFileInfo(filevers={windows_version}, prodvers={windows_version},
    mask=0x3f, flags=0x2, OS=0x40004, fileType=0x1, subtype=0x0, date=(0, 0)),
  kids=[StringFileInfo([StringTable('040904B0', [
    StringStruct('FileDescription', 'Network Guard'),
    StringStruct('FileVersion', '{VERSION}'),
    StringStruct('ProductName', 'Network Guard'),
    StringStruct('ProductVersion', '{VERSION}'),
    StringStruct('OriginalFilename', 'NetworkGuard.exe')])]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])])''', encoding="utf-8")
    build_env = dict(os.environ, PYINSTALLER_CONFIG_DIR=str(build / "cache"))
    # Conda places Tcl/Tk and other runtime DLLs outside the Python DLL folder.
    conda_bin = Path(sys.prefix) / "Library" / "bin"
    if conda_bin.is_dir():
        build_env["PATH"] = str(conda_bin) + os.pathsep + build_env.get("PATH", "")
    subprocess.run([sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
        "--onedir", "--windowed", "--name", "NetworkGuard",
        "--version-file", str(version_file), "--distpath", str(ROOT / "dist"),
        "--workpath", str(build / "pyinstaller"), "--specpath", str(build), str(ROOT / "gui.py")],
        cwd=ROOT, check=True, env=build_env)
    output = ROOT / "dist" / "NetworkGuard"
    shutil.copy2(ROOT / "README.md", output / "README.md")
    if (ROOT / "LEGACY_REVIEW.md").exists():
        shutil.copy2(ROOT / "LEGACY_REVIEW.md", output / "LEGACY_REVIEW.md")
    if (ROOT / "LICENSE").exists():
        shutil.copy2(ROOT / "LICENSE", output / "LICENSE")
    if (ROOT / "CHANGELOG.md").exists():
        shutil.copy2(ROOT / "CHANGELOG.md", output / "CHANGELOG.md")
    archive = Path(shutil.make_archive(str(ROOT / "dist" / f"NetworkGuard-{VERSION}-windows-x64"),
                                      "zip", ROOT / "dist", "NetworkGuard"))
    with archive.open("rb") as stream:
        checksum = hashlib.sha256()
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            checksum.update(chunk)
        digest = checksum.hexdigest()
    archive.with_suffix(".zip.sha256").write_text(f"{digest}  {archive.name}\n", encoding="ascii")
    print(archive)


if __name__ == "__main__":
    main()

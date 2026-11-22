"""Build a Windows x64 folder distribution and versioned ZIP."""
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import sys

from app_info import VERSION

SOURCE_DIR = Path(__file__).resolve().parent
ROOT = SOURCE_DIR.parent


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
        "--workpath", str(build / "pyinstaller"), "--specpath", str(build),
        "--paths", str(SOURCE_DIR), str(SOURCE_DIR / "gui.py")],
        cwd=ROOT, check=True, env=build_env)
    output = ROOT / "dist" / "NetworkGuard"
    shutil.copy2(ROOT / "README.md", output / "README.md")
    legacy_review = ROOT / "docs" / "LEGACY_REVIEW.md"
    if legacy_review.exists():
        shutil.copy2(legacy_review, output / "LEGACY_REVIEW.md")
    if (ROOT / "LICENSE").exists():
        shutil.copy2(ROOT / "LICENSE", output / "LICENSE")
    if (ROOT / "CHANGELOG.md").exists():
        shutil.copy2(ROOT / "CHANGELOG.md", output / "CHANGELOG.md")
    # 从网上下载的 ZIP 解压后，Windows 会给每个文件打上"来自 Internet"标记，
    # 双击 .bat 时提示"无法验证发布者"。.bat 无法代码签名，只能靠解除锁定。
    (output / "READ-ME-FIRST.txt").write_text(
        "如果双击 start_gui.bat 时 Windows 提示\n"
        "  \"无法验证发布者，此文件没有包含有效的数字签名以验证其发布者\"\n"
        "这是 Windows 对「从网上下载的文件」的安全提示，不是程序故障，也不是文件损坏。\n"
        "（.bat 文件无法进行代码签名，所以这个提示只能靠解除锁定来消除。）\n"
        "\n"
        "一次性解决办法，任选其一：\n"
        "\n"
        "A. 解压前解除（推荐）\n"
        "   右键下载的 ZIP 文件 -> 属性 -> 在「常规」页最下方勾选「解除锁定」-> 确定，然后再解压。\n"
        "   这样解压出来的文件都不再带标记，之后双击不会再提示。\n"
        "\n"
        "B. 已经解压了（PowerShell 一行命令）\n"
        "   把路径换成你自己的解压目录：\n"
        "   Get-ChildItem -Path \"D:\\NetworkGuard\" -Recurse | Unblock-File\n"
        "\n"
        "C. 图形界面\n"
        "   全选本文件夹里的所有文件 -> 右键 -> 属性 -> 勾选「解除锁定」-> 确定\n"
        "\n"
        "解除后双击 start_gui.bat 即可正常启动。\n",
        encoding="utf-8-sig",
    )
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

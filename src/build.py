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


def sha256_of(path: Path) -> str:
    checksum = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            checksum.update(chunk)
    return checksum.hexdigest()


def find_iscc() -> Path | None:
    """Locate Inno Setup's command-line compiler.

    GitHub Actions 的 windows-latest 自 2025 年起不再预装 Inno Setup，
    工作流会用 Chocolatey 安装；本地可用源码或 per-user 安装。
    """
    candidates: list[Path] = []
    env = os.environ.get("ISCC")
    if env:
        candidates.append(Path(env))
    which = shutil.which("iscc") or shutil.which("ISCC")
    if which:
        candidates.append(Path(which))
    local = os.environ.get("LOCALAPPDATA")
    if local:
        candidates.append(Path(local) / "InnoSetup6" / "ISCC.exe")
    for base in (os.environ.get("ProgramFiles(x86)"), os.environ.get("ProgramFiles")):
        if base:
            candidates.append(Path(base) / "Inno Setup 6" / "ISCC.exe")
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def build_installer(windows_version: tuple[int, ...]) -> Path | None:
    """Compile the per-user Setup.exe (optional, needs Inno Setup)."""
    iscc = find_iscc()
    if iscc is None:
        print("未找到 Inno Setup（ISCC.exe），跳过安装包构建。")
        return None
    setup = ROOT / "dist" / f"NetworkGuard-Setup-{VERSION}.exe"
    completed = subprocess.run(
        [
            str(iscc),
            f"/DMyAppVersion={VERSION}",
            "/DMyWinVersion=" + ".".join(str(part) for part in windows_version),
            f"/DSourceRoot={ROOT / 'dist' / 'NetworkGuard'}",
            str(SOURCE_DIR / "installer.iss"),
        ],
        cwd=ROOT,
    )
    if completed.returncode != 0:
        raise SystemExit(f"安装包编译失败（ISCC 返回码 {completed.returncode}）")
    if not setup.exists():
        raise SystemExit(f"安装包未生成: {setup}")
    setup.with_suffix(".exe.sha256").write_text(
        f"{sha256_of(setup)}  {setup.name}\n", encoding="ascii"
    )
    print(setup)
    return setup


def write_version_file(path: Path, exe_name: str, windows_version: tuple[int, ...]) -> None:
    path.write_text(f'''VSVersionInfo(
  ffi=FixedFileInfo(filevers={windows_version}, prodvers={windows_version},
    mask=0x3f, flags=0x2, OS=0x40004, fileType=0x1, subtype=0x0, date=(0, 0)),
  kids=[StringFileInfo([StringTable('040904B0', [
    StringStruct('FileDescription', 'Network Guard'),
    StringStruct('FileVersion', '{VERSION}'),
    StringStruct('ProductName', 'Network Guard'),
    StringStruct('ProductVersion', '{VERSION}'),
    StringStruct('OriginalFilename', '{exe_name}')])]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])])''', encoding="utf-8")


CLI_LAUNCHER = """@echo off
rem ============================================================
rem  Network Guard - CLI (terminal) mode
rem  Runs the monitor loop in this console window.
rem  Press Ctrl+C to stop. Logs also go to the data directory.
rem
rem  Usage:
rem    start_cli.bat
rem    start_cli.bat --config other.json
rem    start_cli.bat --verbose
rem    start_cli.bat --no-wifi-recovery
rem ============================================================
cd /d "%~dp0"

echo Network Guard (CLI mode). Press Ctrl+C to stop.
echo.
"%~dp0NetworkGuardCLI.exe" %*

set "CODE=%errorlevel%"

echo.
echo [exit] NetworkGuardCLI.exe exited with code %CODE%
pause
"""


def build_cli(build: Path, output: Path, build_env: dict[str, str],
              windows_version: tuple[int, ...]) -> Path:
    """Compile the console CLI entry next to the windowed GUI build.

    The GUI is built with ``--windowed``, so the packaged app had no console
    entry at all: ``start_cli.bat`` only worked in the source tree where
    ``src/main.py`` exists. This second pass builds ``NetworkGuardCLI.exe``
    (console subsystem) and drops ``start_cli.bat`` beside it.
    """
    version_file = build / "version_info_cli.txt"
    write_version_file(version_file, "NetworkGuardCLI.exe", windows_version)
    cli_dist = build / "cli-dist"
    subprocess.run([sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
        "--onefile", "--console", "--name", "NetworkGuardCLI",
        "--icon", str(ROOT / "assets" / "NetworkGuardCLI.ico"),
        "--version-file", str(version_file), "--distpath", str(cli_dist),
        "--workpath", str(build / "pyinstaller-cli"), "--specpath", str(build),
        "--paths", str(SOURCE_DIR), str(SOURCE_DIR / "main.py")],
        cwd=ROOT, check=True, env=build_env)
    target = output / "NetworkGuardCLI.exe"
    shutil.copy2(cli_dist / "NetworkGuardCLI.exe", target)
    with (output / "start_cli.bat").open("w", encoding="ascii", newline="\r\n") as handle:
        handle.write(CLI_LAUNCHER)
    return target


def main():
    if sys.platform != "win32":
        raise SystemExit("Build on Windows with a 64-bit Python installation.")
    build = ROOT / "build"
    build.mkdir(exist_ok=True)
    version_file = build / "version_info.txt"
    core, _, prerelease = VERSION.partition("-")
    revision = int(prerelease.rsplit(".", 1)[-1]) if prerelease else 0
    windows_version = tuple(int(part) for part in core.split(".")) + (revision,)
    write_version_file(version_file, "NetworkGuard.exe", windows_version)
    build_env = dict(os.environ, PYINSTALLER_CONFIG_DIR=str(build / "cache"))
    # Conda places Tcl/Tk and other runtime DLLs outside the Python DLL folder.
    conda_bin = Path(sys.prefix) / "Library" / "bin"
    if conda_bin.is_dir():
        build_env["PATH"] = str(conda_bin) + os.pathsep + build_env.get("PATH", "")
    subprocess.run([sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
        "--onedir", "--windowed", "--name", "NetworkGuard",
        "--icon", str(ROOT / "assets" / "NetworkGuard.ico"),
        "--add-data", f"{ROOT / 'assets' / 'NetworkGuard.ico'}{os.pathsep}assets",
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
    if (ROOT / "unblock.bat").exists():
        shutil.copy2(ROOT / "unblock.bat", output / "unblock.bat")
    # 打包版同时提供 GUI 与控制台两个入口，CLI 缺失曾导致安装后无法命令行运行
    cli_exe = build_cli(build, output, build_env, windows_version)
    print(cli_exe)
    # 从网上下载的 ZIP 解压后，Windows 会给每个文件打上"来自 Internet"标记，
    # 双击 .bat 时提示"无法验证发布者"。.bat 无法代码签名，只能靠去掉标记。
    (output / "READ-ME-FIRST.txt").write_text(
        "如果双击 NetworkGuard.exe 时 Windows 提示\n"
        "  \"无法验证发布者，此文件没有包含有效的数字签名以验证其发布者\"\n"
        "这是 Windows 对「从网上下载的文件」的安全提示，不是程序故障，也不是文件损坏。\n"
        "（.bat / 未签名 .exe 没有可校验的签名，只能靠去掉\"下载标记\"来消除。）\n"
        "\n"
        "本目录有两个入口：\n"
        "  NetworkGuard.exe      图形界面\n"
        "  start_cli.bat         命令行模式（NetworkGuardCLI.exe，日志同时写入数据目录）\n"
        "\n"
        "最快：双击本文件夹里的 unblock.bat，等它跑完即可。\n"
        "      它会清理本目录下所有文件的下载标记，之后不再提示。\n"
        "\n"
        "其他办法：\n"
        "A. 解压前解除（推荐）\n"
        "   右键下载的 ZIP 文件 -> 属性 -> 在「常规」页最下方勾选「解除锁定」-> 确定，然后再解压。\n"
        "B. 已经解压了（PowerShell 一行命令，把路径换成实际位置）\n"
        "   Get-ChildItem -Path \"D:\\NetworkGuard\" -Recurse | Unblock-File\n"
        "C. 图形界面\n"
        "   全选本文件夹里的所有文件 -> 右键 -> 属性 -> 勾选「解除锁定」-> 确定\n"
        "\n"
        "补充：\n"
        "- 从安装包（NetworkGuard-Setup-*.exe）安装时，安装程序已自动清除下载标记。\n"
        "- 程序首次启动后会自动清理一遍，所以就算跳过上面的步骤，第一次点「运行」之后也不再提示。\n"
        "- 计划任务开机自启（install_admin_task.bat）不经过资源管理器，本来就不会弹这个提示。\n",
        encoding="utf-8-sig",
    )
    archive = Path(shutil.make_archive(str(ROOT / "dist" / f"NetworkGuard-{VERSION}-windows-x64"),
                                      "zip", ROOT / "dist", "NetworkGuard"))
    archive.with_suffix(".zip.sha256").write_text(
        f"{sha256_of(archive)}  {archive.name}\n", encoding="ascii"
    )
    print(archive)
    build_installer(windows_version)


if __name__ == "__main__":
    main()

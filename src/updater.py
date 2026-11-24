"""In-app update: download a verified release asset and apply it after exit.

Installed copies update through the Inno Setup installer (same per-user
directory, data kept in %LOCALAPPDATA%\\NetworkGuard). Portable copies update
the ZIP in place. Both paths verify the published .sha256 before running a
small PowerShell helper, because the running exe cannot replace itself.
"""
from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib.request import Request, urlopen

MAX_BYTES = 300 * 1024 * 1024
CHUNK = 64 * 1024
WORKDIR_PREFIX = "NetworkGuard-update-"


class UpdateError(RuntimeError):
    """A user-facing update failure."""


class UpdateCancelled(Exception):
    """Raised when the user cancels a download."""


def install_mode() -> str:
    """'installer', 'portable' or 'source'."""
    if not getattr(sys, "frozen", False):
        return "source"
    app_dir = Path(sys.executable).resolve().parent
    return "installer" if (app_dir / "unins000.exe").is_file() else "portable"


def _is_main_asset(name: str, mode: str) -> bool:
    lowered = name.lower()
    if mode == "installer":
        return lowered.endswith(".exe") and "setup" in lowered
    if mode == "portable":
        return lowered.endswith("-windows-x64.zip")
    return False


def select_assets(assets: list, mode: str) -> dict:
    if mode not in ("installer", "portable"):
        raise UpdateError("源码运行不支持自动更新，请下载安装包或发布页版本。")
    main = next((item for item in assets if _is_main_asset(str(item.get("name", "")), mode)), None)
    if main is None:
        raise UpdateError("该版本没有提供适合当前安装方式的更新包。")
    name = str(main.get("name", ""))
    checksum = next((item for item in assets if str(item.get("name")) == name + ".sha256"), None)
    if checksum is None:
        raise UpdateError("更新包缺少 SHA-256 校验文件，已取消更新。")
    return {"main": main, "checksum": checksum, "name": name}


def parse_sha256(text: str) -> str:
    token = text.strip().split()[0] if text.strip() else ""
    if not re.fullmatch(r"[0-9a-fA-F]{64}", token):
        raise UpdateError("SHA-256 校验文件格式不正确。")
    return token.lower()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def download(asset: dict, destination: Path, progress=None, cancelled=None) -> Path:
    url = asset.get("browser_download_url")
    if not isinstance(url, str) or not url.startswith("https://"):
        raise UpdateError("更新包地址无效。")
    destination.parent.mkdir(parents=True, exist_ok=True)
    request = Request(url, headers={"User-Agent": "NetworkGuard-updater"})
    with urlopen(request, timeout=60) as response, destination.open("wb") as handle:
        total = int(response.headers.get("Content-Length") or asset.get("size") or 0)
        done = 0
        while True:
            if cancelled is not None and cancelled():
                raise UpdateCancelled()
            chunk = response.read(CHUNK)
            if not chunk:
                break
            done += len(chunk)
            if done > MAX_BYTES:
                raise UpdateError("更新包超过大小限制，已取消下载。")
            handle.write(chunk)
            if progress is not None:
                progress(done, total)
    return destination


def fetch_update(result: dict, mode: str, workdir: Path, progress=None, cancelled=None) -> Path:
    """Download the right asset plus its checksum into ``workdir``."""
    assets = result.get("assets") or []
    chosen = select_assets(assets, mode)
    checksum_path = download(chosen["checksum"], workdir / (chosen["name"] + ".sha256"))
    expected = parse_sha256(checksum_path.read_text(encoding="utf-8", errors="replace"))
    package = download(chosen["main"], workdir / chosen["name"], progress=progress, cancelled=cancelled)
    actual = sha256_file(package)
    if actual != expected:
        raise UpdateError("更新包 SHA-256 校验失败，文件可能不完整。")
    return package


def new_workdir() -> Path:
    return Path(tempfile.mkdtemp(prefix=WORKDIR_PREFIX))


def cleanup_workdirs(max_age_seconds: int = 6 * 3600) -> None:
    import time

    root = Path(tempfile.gettempdir())
    now = time.time()
    for path in root.glob(WORKDIR_PREFIX + "*"):
        try:
            if path.is_dir() and now - path.stat().st_mtime > max_age_seconds:
                shutil.rmtree(path, ignore_errors=True)
        except OSError:
            continue


def _ps(value) -> str:
    """Quote a path as a PowerShell single-quoted literal."""
    return "'" + str(value).replace("'", "''") + "'"


def build_apply_script(package: Path, mode: str, app_dir: Path, exe: Path,
                       pids: list[int], workdir: Path) -> Path:
    lines = [
        "$ErrorActionPreference = 'Stop'",
        "$targets = @(" + ",".join(str(int(pid)) for pid in pids) + ")",
        "while ($true) {",
        "  $alive = $false",
        "  foreach ($id in $targets) {",
        "    if (Get-Process -Id $id -ErrorAction SilentlyContinue) { $alive = $true }",
        "  }",
        "  if (-not $alive) { break }",
        "  Start-Sleep -Milliseconds 400",
        "}",
        "Start-Sleep -Milliseconds 700",
        f"$name = [System.IO.Path]::GetFileNameWithoutExtension({_ps(exe)})",
        "while (Get-Process -Name $name -ErrorAction SilentlyContinue) {",
        "  Start-Sleep -Milliseconds 400",
        "}",
    ]
    if mode == "installer":
        lines += [
            f"$setup = {_ps(package)}",
            "try {",
            "  $proc = Start-Process -FilePath $setup -ArgumentList '/VERYSILENT','/SUPPRESSMSGBOXES','/NORESTART','/LANG=zh' -Wait -PassThru",
            f"  ('exit=' + $proc.ExitCode) | Out-File -FilePath {_ps(workdir / 'install.log')} -Encoding utf8",
            "} catch {",
            f"  $_ | Out-File -FilePath {_ps(workdir / 'install-error.log')} -Encoding utf8",
            "}",
        ]
    elif mode == "portable":
        extract = workdir / "extract"
        lines += [
            f"$zip = {_ps(package)}",
            f"$extract = {_ps(extract)}",
            "Expand-Archive -LiteralPath $zip -DestinationPath $extract -Force",
            f"$source = Join-Path $extract 'NetworkGuard'",
            "if (-not (Test-Path -LiteralPath $source)) { throw '更新包结构不正确' }",
            f"Copy-Item -Path (Join-Path $source '*') -Destination {_ps(app_dir)} -Recurse -Force",
        ]
    else:
        raise UpdateError("当前运行方式不支持自动更新。")
    lines += [
        "try {",
        f"  Start-Process -FilePath {_ps(exe)}",
        "} catch {",
        f"  $_ | Out-File -FilePath {_ps(Path(tempfile.gettempdir()) / 'NetworkGuard-update-error.log')} -Encoding utf8",
        "}",
        "Start-Sleep -Milliseconds 1500",
        f"Remove-Item -LiteralPath {_ps(workdir)} -Recurse -Force -ErrorAction SilentlyContinue",
    ]
    script = workdir / "apply-update.ps1"
    script.write_text("\n".join(lines) + "\n", encoding="utf-8-sig")
    return script


def launch_apply(package: Path, mode: str, app_dir: Path, exe: Path,
                 pids: list[int], workdir: Path) -> Path:
    script = build_apply_script(package, mode, app_dir, exe, pids, workdir)
    flags = 0
    if os.name == "nt":
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    subprocess.Popen(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script)],
        cwd=str(workdir), creationflags=flags, close_fds=True,
    )
    return script

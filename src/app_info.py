"""Release identity and writable runtime paths."""
import os
from pathlib import Path
import sys

VERSION = "0.1.0-rc.1"
REPOSITORY = "LoidInfinity/Reconnect"
RELEASES_URL = f"https://github.com/{REPOSITORY}/releases"
FROZEN = bool(getattr(sys, "frozen", False))
# 源码布局：<repo>/src/app_info.py；打包后：<dist>/NetworkGuard/_internal/app_info.py
SOURCE_DIR = Path(__file__).resolve().parent
APP_DIR = Path(sys.executable).resolve().parent if FROZEN else SOURCE_DIR.parent
# 运行时数据集中在一个目录：源码版是 <repo>/data，打包版是 %LOCALAPPDATA%\NetworkGuard
DATA_DIR = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "NetworkGuard" if FROZEN else APP_DIR / "data"
LOG_DIR = DATA_DIR / "logs"
LOG_NAME = "network_guard.log"
# 配置里保存相对 DATA_DIR 的路径，整个数据目录可以整体搬迁
LOG_SUBPATH = f"logs/{LOG_NAME}"
LEGACY_RUNTIME_FILES = ("network_guard_config.json", "network_guard_state.txt", "network_guard.pid")


def _move_if_missing(source: Path, target: Path) -> None:
    try:
        if source.is_file() and not target.exists():
            source.replace(target)
    except OSError:
        pass


def migrate_legacy_layout() -> None:
    """把旧版本散落在程序目录里的运行时文件收进 DATA_DIR。

    早期版本把配置、状态、PID 和日志直接写在仓库根目录，日志一多就很乱。
    这里做一次性搬迁；失败也不影响启动，文件之后会在新位置重新生成。
    """
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        DATA_DIR.mkdir(parents=True, exist_ok=True)
    except OSError:
        return
    if DATA_DIR != APP_DIR:
        # 源码版：旧文件留在仓库根目录
        for name in LEGACY_RUNTIME_FILES:
            _move_if_missing(APP_DIR / name, DATA_DIR / name)
    # 日志：源码版旧位置是仓库根目录，打包版旧位置是数据目录本身
    for origin in {APP_DIR, DATA_DIR}:
        for old in sorted(origin.glob(f"{LOG_NAME}*")):
            _move_if_missing(old, LOG_DIR / old.name)


def worker_command(config_path: Path) -> list[str]:
    prefix = [sys.executable] if FROZEN else [sys.executable, "-u", str(SOURCE_DIR / "gui.py")]
    return prefix + ["--worker", "--config", str(config_path)]


def clear_internet_marks(directory: Path | None = None) -> int:
    """删除浏览器下载时附加的 Zone.Identifier 数据流。

    Windows 用它判断文件"来自 Internet"，双击 .bat / .exe 会弹出
    「无法验证发布者…没有有效的数字签名」。`.bat` 无法代码签名，所以只能把
    标记去掉。返回清理掉的文件数。
    """
    target = directory or APP_DIR
    removed = 0
    try:
        entries = list(target.rglob("*"))
    except OSError:
        return 0
    for path in entries:
        try:
            if not path.is_file():
                continue
            mark = f"{path}:Zone.Identifier"
            if os.path.exists(mark):
                os.remove(mark)
                removed += 1
        except OSError:
            continue
    return removed


def clear_internet_marks_async() -> None:
    """后台清理一次，成功后再双击启动就不会再弹安全警告。

    源码运行时程序目录里有 .git / dist / build，逐文件扫描代价大且没有意义，
    只在打包版执行。
    """
    if not FROZEN:
        return
    import threading

    def run() -> None:
        try:
            clear_internet_marks()
        except Exception:  # noqa: BLE001
            pass

    try:
        threading.Thread(target=run, daemon=True).start()
    except Exception:  # noqa: BLE001
        pass

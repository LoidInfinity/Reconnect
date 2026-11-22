"""Network Guard 的单元测试。

运行方式（在仓库根目录）：

    python -m unittest discover -s tests -t . -p "test_*.py"

源码在 ``src/``，这里把它加入 ``sys.path``，测试里才能 ``import main`` / ``import gui``。
"""
import sys
from pathlib import Path

SRC_DIR = Path(__file__).resolve().parent.parent / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

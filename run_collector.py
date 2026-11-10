"""隔离模式（python -I）下的采集 CLI 启动器。

显式登记工程路径后转交 collector 的命令行入口，确保在 -I 下
（不读 PYTHONPATH、不隐式加入当前目录）也能从列表出发采集。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from collector.__main__ import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())

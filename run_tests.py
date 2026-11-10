"""隔离模式（python -I）下的单测入口，显式登记工程路径后转交 unittest。"""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

if __name__ == "__main__":
    unittest.main(module=None, argv=[sys.argv[0]] + sys.argv[1:])

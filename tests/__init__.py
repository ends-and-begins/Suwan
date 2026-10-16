"""测试包：导入前自动把 src 加入 sys.path，便于直接 unittest 发现。"""
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

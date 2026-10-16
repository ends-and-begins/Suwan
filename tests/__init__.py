"""测试包：导入项目前禁用 dotenv 和真实网络；完整门禁在临时副本执行。"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from scripts._offline import install_offline_guard

install_offline_guard()
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

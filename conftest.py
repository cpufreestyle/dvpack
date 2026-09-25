"""让 `python -m pytest` 无需安装包即可 import dvpack。"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

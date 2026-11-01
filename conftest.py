"""让测试可以 ``from core.xxx import ...``（把插件根目录加入 sys.path）。"""

import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

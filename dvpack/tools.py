"""便携工具链路径。全部在项目内 tools/ 下，不依赖 PATH、不改系统。"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_TOOLS = ROOT / "tools"


def _exe(name: str) -> Path:
    """Windows 用 .exe，其它平台用裸名（Mac 上跑端侧实验时同一套代码可用）。"""
    return _TOOLS / f"{name}{'.exe' if os.name == 'nt' else ''}"


FFMPEG = _exe("ffmpeg/bin/ffmpeg")
FFPROBE = _exe("ffmpeg/bin/ffprobe")
DOVI_TOOL = _exe("bin/dovi_tool")


def require(path: Path) -> Path:
    """项目内的工具优先；找不到时回退 PATH 里的同名可执行文件。

    Windows 打包好的产物用 tools/ 下自带的那份；Mac / CI 上没打包，
    回退到系统 ffmpeg 才能跑端侧实验。找不到仍然报错，行为和以前一致。
    """
    if path.exists():
        return path
    found = shutil.which(path.name)
    if found:
        return Path(found)
    raise FileNotFoundError(f"缺少工具 {path}；先跑 tools/ 的下载步骤")

"""便携工具链路径。全部在项目内 tools/ 下，不依赖 PATH、不改系统。"""

from __future__ import annotations

import os
import shutil
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_TOOLS = ROOT / "tools"


def _exe(name: str) -> Path:
    """Windows 用 .exe，其它平台用裸名（Mac 上跑端侧实验时同一套代码可用）。"""
    return _TOOLS / f"{name}{'.exe' if os.name == 'nt' else ''}"


FFMPEG = _exe("ffmpeg/bin/ffmpeg")
FFPROBE = _exe("ffmpeg/bin/ffprobe")
DOVI_TOOL = _exe("bin/dovi_tool")
#: vtfrc 用 macOS 私有的 VTFrameProcessor（tahoe 才有）做光流插帧，只有 Mac 有二进制；
#: 非 Mac 平台由 require() 回退 PATH，源码与编译命令见 tools/build_vtfrc.sh。
VTFRC = _exe("bin/vtfrc")


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


DEFAULT_FFMPEG_TIMEOUT = 3600.0
PROBE_TIMEOUT = 600.0


def _env_float(name: str) -> float | None:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return None
    try:
        value = float(raw)
    except ValueError:
        return None
    return value if value > 0 else None


def ffmpeg_timeout(seconds: float | None = None) -> float:
    """ffmpeg 打包类命令的执行上限（秒）。

    上游 Jellyfin 流半路卡死时 ffmpeg 会永远等下去：worker 持着
    _pack_lock_for 不放 → 同 item 的后续 /jf/pack 全堵在锁上 → 真机症状
    「点了没反应」，只能重启 serve。2026-10-05 补执行超时堵这个洞。

    短包给 5 倍时长余量（20s 秒开包 → 160s，180s warm → 960s），
    全片/未知给 3600s（4K DV 拷贝实测分钟级，仍留两个数量级余量）。
    DVPACK_FFMPEG_TIMEOUT 环境变量整体覆盖。
    """
    override = _env_float("DVPACK_FFMPEG_TIMEOUT")
    if override:
        return override
    if seconds and seconds > 0:
        return max(120.0, min(DEFAULT_FFMPEG_TIMEOUT, seconds * 5.0 + 60.0))
    return DEFAULT_FFMPEG_TIMEOUT


def probe_timeout() -> float:
    """ffprobe 探测类上限：秒级操作，10 分钟足够判死。

    与打包共用同一个覆盖开关：整机过载时统一放大。
    """
    return _env_float("DVPACK_FFMPEG_TIMEOUT") or PROBE_TIMEOUT


def robust_rmtree(path: Path, retries: int = 3, delay: float = 0.4) -> bool:
    """删目录；删不动时先重试，仍失败改名 ._trash_{name}_{pid} 留给 sweep 收。

    POSIX 上 unlink-while-open 照删不误，行为同 shutil.rmtree(ignore_errors=True)。
    Windows 上文件句柄不让删（http.server 在播分片占着句柄），ignore_errors=True
    会静默留半个目录，越攒越多。重试仍失败就改名成 serve._sweep_stale_swaps
    认得的 ._trash_ 前缀，等本 pid 死了随下一轮 sweep 收掉。
    返回 True=删净；False=留下 trash 待扫。
    """
    for _ in range(max(1, retries)):
        shutil.rmtree(path, ignore_errors=True)
        if not path.exists():
            return True
        time.sleep(delay)
    if not path.exists():
        return True
    try:
        os.rename(path, path.parent / f"._trash_{path.name}_{os.getpid()}")
    except OSError:
        return False
    return False

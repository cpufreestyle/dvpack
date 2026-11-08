"""dvassert.sh 冒烟：断言器自己不能烂。

2026-10-05 实测过它三处烂点：Tier 断言停在旧口径（tier=1，点亮版是
0）、$FF 未加引号被「AI Shared」空格路径静默拆词、dvcC 用 ffprobe
trace grep 跨 ffmpeg 版本假 FAIL。这个用例把脚本对现役 pack 的输出
钉死：任一 PASS 行消失或出现 FAIL 即回归。

skip 条件：tools/bin 缺二进制（无网机器）、本地 serve 不在 8899。
"""

from __future__ import annotations

import os
import re
import shutil
import socket
import subprocess
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "dvassert.sh"
PACK = os.environ.get("DVPACK_DVASSERT_PACK", "_jf/534f5cb2ba7f9d500f9f2154e8cff079/master.m3u8")
SERVE = "http://127.0.0.1:8899/" + PACK


def _serve_up():
    # 对本机 serve 发裸 HTTP/1.0 请求看 200：不进 urllib，跳过条件只关心
    # serve 活着与否。目标由常量 SERVE 决定，端口/路径直接来自它。
    parts = urlsplit(SERVE)
    host, port = parts.hostname or "127.0.0.1", parts.port or 80
    try:
        with socket.create_connection((host, port), timeout=3) as sock:
            sock.sendall(
                b"GET " + (parts.path or "/").encode()
                + b" HTTP/1.0\r\nHost: " + host.encode() + b"\r\n\r\n")
            return sock.recv(64).split(b" ", 2)[1] == b"200"
    except (OSError, IndexError):
        return False


def _tools_ready():
    bundled = ["bin/mp4dump", "bin/dovi_tool", "ffmpeg/bin/ffmpeg"]
    if all((ROOT / "tools" / rel).is_file() for rel in bundled):
        return True
    return bool(shutil.which("mp4dump") and shutil.which("dovi_tool"))


@pytest.mark.skipif(not _tools_ready(), reason="dovi_tool / mp4dump 不在 tools/bin 或 PATH")
@pytest.mark.skipif(not _serve_up(), reason="本地 serve(8899) 未运行")
def test_dvassert_passes_on_live_pack():
    proc = subprocess.run(
        ["bash", str(SCRIPT), SERVE],
        capture_output=True,
        text=True,
        timeout=180,
        cwd=str(ROOT),
    )
    out = proc.stdout + proc.stderr
    # 不能抓裸子串 FAIL：汇总行本身就是「PASS=9 FAIL=0」。只许 FAIL=0 存在，
    # 且任何一行都不许以 FAIL 开头（工具缺失 / 断言失败的行首形态）。
    assert "FAIL=0" in out, out[-2000:]
    assert re.search(r"^FAIL\s", out, re.M) is None, out[-2000:]
    assert "PASS=9" in out, out[-2000:]
    assert proc.returncode == 0

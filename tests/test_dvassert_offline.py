"""dvassert.sh 的离线面：不依赖 serve / 不依赖真片源的分支。

线上那条（tests/test_dvassert_script.py::test_dvassert_passes_on_live_pack）
要求 tools/bin 二进制齐全 且 本地 serve 在 8899——两个条件任一不成立就
skip，于是断言器自己的失败面（工具缺失、master 拉不下来）长期零覆盖。
2026-10-07 实测就是漏洞：bash 3.2 在 set -u 下把紧跟全角括号的 $name
当未绑定变量，缺工具分支整段崩（rc=1 而不是打印 FAIL），没有任何用例报警。

这里只用 /usr/bin:/bin 这种瘦 PATH + 指向不存在目录的 DVPACK_TOOLS，把
「工具缺失」和「master 拉不下来」两条退出路径钉死。全程不起 HTTP 服务。
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "dvassert.sh"

# 一个必然什么都没有的 PATH：/usr/bin:/bin 里没有 dovi_tool / mp4dump，
# 把 pick() 的 PATH 回退也一并堵死，才能走到 missing tool 分支。
BARE_PATH = "/usr/bin:/bin"
BARE_ENV = {"PATH": BARE_PATH, "DVPACK_TOOLS": "/nonexistent-tools-dir"}


def _run(args=(), env=None, cwd=ROOT):
    proc = subprocess.run(
        ["bash", str(SCRIPT), *args],
        capture_output=True,
        text=True,
        timeout=60,
        cwd=str(cwd),
        env={**os.environ, **(env or {})},
    )
    return proc


class TestMissingTools:
    """tools/bin 与 PATH 双双缺失：必须逐条点名，而不是崩在 set -u 上。"""

    def test_reports_each_missing_tool(self):
        proc = _run(env=BARE_ENV)
        out = proc.stdout + proc.stderr
        for tool in ("ffmpeg", "ffprobe", "dovi_tool", "mp4dump"):
            assert f"missing tool: {tool}" in out, out

    def test_exits_2_not_a_bash_crash(self):
        """rc=2 是脚本约定的「工具没装」；rc=1 说明崩在别处（2026-10-07 的 bug）。"""
        proc = _run(env=BARE_ENV)
        out = proc.stdout + proc.stderr
        assert proc.returncode == 2, out
        assert "unbound variable" not in out, out

    def test_does_not_need_network_or_serve(self):
        """这条用例必须能在完全离线的机器上跑（不起 serve、不联网）。"""
        proc = _run(env=BARE_ENV)
        assert "cannot fetch master" not in (proc.stdout + proc.stderr)


class TestUnreachableMaster:
    """工具齐全但 master 拉不下来：FAIL + exit 2，不许静默 0 通过。"""

    @pytest.mark.skipif(
        not (ROOT / "tools" / "bin" / "dovi_tool").is_file(),
        reason="tools/bin 缺二进制（无网机器）",
    )
    def test_cannot_fetch_master_fails(self):
        # 127.0.0.1:1 是保留端口，没有任何监听——curl 必然连不上
        proc = _run(["http://127.0.0.1:1/master.m3u8"])
        out = proc.stdout + proc.stderr
        assert proc.returncode == 2, out
        assert "cannot fetch master" in out, out

    @pytest.mark.skipif(
        not (ROOT / "tools" / "bin" / "dovi_tool").is_file(),
        reason="tools/bin 缺二进制（无网机器）",
    )
    def test_default_master_is_local_8899(self):
        """不带参数时打本机 serve：地址写死 8899，改端口要同步改这里。"""
        text = SCRIPT.read_text(encoding="utf-8")
        assert "http://127.0.0.1:8899/master.m3u8" in text

    def test_script_is_bash_syntax_clean(self):
        proc = subprocess.run(
            ["bash", "-n", str(SCRIPT)], capture_output=True, text=True, timeout=30,
        )
        assert proc.returncode == 0, proc.stderr

    def test_quotes_surround_tool_vars(self):
        """2026-10-05 踩过：$FF 不加引号被「AI Shared」空格路径静默拆词。"""
        text = SCRIPT.read_text(encoding="utf-8")
        assert chr(34) + "$MP4D" + chr(34) in text
        assert chr(34) + "$DV" + chr(34) in text
        assert chr(34) + "$FF" + chr(34) in text

    def test_dolby_vision_criteria_present(self):
        """点亮口径（Tier=0 + dvcC 五字段）必须一条不少地写在脚本里。"""
        text = SCRIPT.read_text(encoding="utf-8")
        for needle in (
            "VIDEO-RANGE=PQ",
            "dvh1",
            "Tier = 0",
            "dv_profile = 5",
            "rpu_present_flag = 1",
            "bl_present_flag = 1",
            "el_present_flag = 0",
            "dv_bl_signal_compatibility_id = 0",
        ):
            assert needle in text, needle

    def test_braced_var_before_fullwidth_paren(self):
        """回归：2026-10-07 bash 3.2 set -u 把 $name（全角括号）当未绑定变量。"""
        text = SCRIPT.read_text(encoding="utf-8")
        assert "$name（" not in text, "必须写 ${name}（"
        assert "${name}（" in text

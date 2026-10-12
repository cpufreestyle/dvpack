"""CLI `init` 子命令：dovi_tool info 口径的独立诊断入口。"""

from __future__ import annotations

from pathlib import Path

import pytest

from dvpack.__main__ import main

PACK = Path("_out/dv_p7_fix/_jf/534f5cb2ba7f9d500f9f2154e8cff079")
INIT = PACK / "init.mp4"


@pytest.mark.skipif(not INIT.is_file(), reason="现机 pack 产物不在测试环境")
def test_init_reports_healthy_pack(capsys):
    assert main(["init", str(PACK)]) == 0
    out = capsys.readouterr().out
    assert "Sample : dvh1" in out
    assert "DV box : dvcC" in out
    assert "参数集 [32, 33, 34]" in out
    assert "结论   : 通过" in out


def test_init_missing_file_returns_2(capsys):
    assert main(["init", "/nonexistent/init.mp4"]) == 2
    err = capsys.readouterr().err
    assert "找不到" in err

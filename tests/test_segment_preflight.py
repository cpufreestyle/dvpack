"""分片时间轴静态预检测试（Infuse FCPTSIncorrectnessDetector 学习项）。

合成分片足够：这里的判据是纯 ISOBMFF 字段比较，不依赖 ffmpeg。真文件
端到端路径由 test_remux.py 的真片源用例覆盖。
"""

from __future__ import annotations

from pathlib import Path

from dvpack.remux import (
    RemuxOutput,
    SegmentTimestampProblem,
    validate_segments,
)


def _box(btype: str, payload: bytes) -> bytes:
    return (len(payload) + 8).to_bytes(4, "big") + btype.encode() + payload


def _full_box(btype: str, version: int, flags: int, payload: bytes) -> bytes:
    return _box(btype, bytes([version]) + flags.to_bytes(3, "big") + payload)


def _trun(durations: list[int]) -> bytes:
    # sample-duration-present (0x100) + sample-size-present (0x200)
    body = len(durations).to_bytes(4, "big")
    for d in durations:
        body += d.to_bytes(4, "big") + (512).to_bytes(4, "big")
    return _full_box("trun", 0, 0x300, body)


def _tfdt(base: int) -> bytes:
    return _full_box("tfdt", 1, 0, base.to_bytes(8, "big"))


def _segment(base: int, durations: list[int]) -> bytes:
    traf = _box("traf", _tfdt(base) + _trun(durations))
    moof = _box("moof", traf)
    mdat = _box("mdat", b"" * 16)
    return moof + mdat


def _output(tmp_path: Path, name: str, data: bytes) -> RemuxOutput:
    segment = tmp_path / name
    segment.write_bytes(data)
    return RemuxOutput(playlist=tmp_path / "v.m3u8", init=tmp_path / "init.mp4", segments=(segment,))


def test_clean_timeline_has_no_problems(tmp_path: Path):
    output = RemuxOutput(
        playlist=tmp_path / "v.m3u8",
        init=tmp_path / "init.mp4",
        segments=(),
    )
    s1 = _output(tmp_path, "1.m4s", _segment(0, [100, 100, 100]))
    s2 = _output(tmp_path, "2.m4s", _segment(300, [100, 100]))
    output = RemuxOutput(s1.playlist, s1.init, (tmp_path / "1.m4s", tmp_path / "2.m4s"))
    assert validate_segments(output) == ()


def test_tfdt_regression_is_rejected(tmp_path: Path):
    s1 = _output(tmp_path, "1.m4s", _segment(0, [100, 100]))
    s2 = _output(tmp_path, "2.m4s", _segment(100, [100]))  # < 0+100+100
    output = RemuxOutput(s1.playlist, s1.init, (tmp_path / "1.m4s", tmp_path / "2.m4s"))
    problems = validate_segments(output)
    assert len(problems) == 1
    assert "tfdt" in problems[0].field
    assert problems[0].actual == 100


def test_zero_duration_is_rejected(tmp_path: Path):
    output = _output(tmp_path, "1.m4s", _segment(0, [100, 0, 100]))
    problems = validate_segments(output)
    assert len(problems) == 1
    assert problems[0].field == "sample#1 duration"
    assert problems[0].actual == 0


def test_gapless_handoff_is_accepted(tmp_path: Path):
    """相邻分片 tfdt 恰好衔接（无 gap）是 ffmpeg fMP4 的标准形态，必须放行。"""
    s1 = _output(tmp_path, "1.m4s", _segment(0, [50, 50]))
    s2 = _output(tmp_path, "2.m4s", _segment(100, [50]))
    output = RemuxOutput(s1.playlist, s1.init, (tmp_path / "1.m4s", tmp_path / "2.m4s"))
    assert validate_segments(output) == ()

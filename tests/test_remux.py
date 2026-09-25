"""remux 测试：命令行构成 + 真文件端到端 + 一条"少了 flag 会怎样"的反面控制。

反面控制很重要：`-strict unofficial` 看起来像多余的历史包袱，很容易被后人当噪音删掉，
而删掉的后果是 init 段不再有 DV 配置 box、电视静默地当普通 HDR 放。
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
from dvpack.bmff import locate_dv_boxes, parse_file
from dvpack.p5 import DvcConfig
from dvpack.probe import parse_probe_json
from dvpack.remux import (
    RemuxError,
    check_rpu_consistency,
    hls_command,
    remux,
    remux_and_verify,
    resolve_output,
)
from dvpack.tools import FFMPEG

SRC = Path(os.environ.get("DVPACK_SOURCE_MKV", ""))

needs_source = pytest.mark.skipif(
    not (SRC.is_file() and FFMPEG.exists()), reason="缺片源或 ffmpeg"
)


def test_command_carries_both_dv_flags():
    argv = hls_command(Path("in.mkv"), Path("out/video.m3u8"), seconds=30)
    joined = " ".join(argv)
    assert "-tag:v dvh1" in joined
    assert "-strict unofficial" in joined
    assert "-hls_segment_type fmp4" in joined
    assert " -c copy " in f" {' '.join(argv)} "
    assert argv[-1] == str(Path("out/video.m3u8"))


def test_video_only_mapping_is_explicit():
    """字幕不映射，否则 ffmpeg 会拿 webvtt muxer 去封 SSA 直接失败。"""
    argv = hls_command(Path("in.mkv"), Path("v.m3u8"))
    assert argv.count("0:v:0") == 1
    assert not any("0:s" in arg for arg in argv)


def test_resolve_output_reads_init_uri_from_playlist(tmp_path: Path):
    (tmp_path / "video.m3u8").write_text(
        '#EXTM3U\n#EXT-X-MAP:URI="init.mp4"\n#EXTINF:6.0,\ns0.m4s\n#EXTINF:6.0,\ns1.m4s\n',
        encoding="utf-8",
    )
    (tmp_path / "init.mp4").write_bytes(b"\0")
    (tmp_path / "s0.m4s").write_bytes(b"\0")
    (tmp_path / "s1.m4s").write_bytes(b"\0")
    output = resolve_output(tmp_path / "video.m3u8")
    assert output.init.name == "init.mp4"
    assert [p.name for p in output.segments] == ["s0.m4s", "s1.m4s"]
    assert output.exists


def test_resolve_output_rejects_mpegts_playlist(tmp_path: Path):
    (tmp_path / "v.m3u8").write_text("#EXTM3U\n#EXTINF:6.0,\ns0.ts\n", encoding="utf-8")
    with pytest.raises(RemuxError, match="EXT-X-MAP"):
        resolve_output(tmp_path / "v.m3u8")


def test_remux_refuses_non_dovi_source(monkeypatch, tmp_path: Path):
    import dvpack.remux as module

    plain = parse_probe_json(
        {"streams": [{"index": 0, "codec_type": "video", "codec_name": "hevc"}], "format": {}},
        Path("x.mkv"),
    )
    monkeypatch.setattr(module, "probe", lambda _path: plain)
    with pytest.raises(RemuxError, match="DOVI"):
        remux("x.mkv", tmp_path)


@needs_source
def test_end_to_end_resign_lands_on_apple_template(tmp_path: Path):
    """转签落地的端到端判据：init 段的 dvcC 必须逐字节等于 Apple 官方 P5 模板。

    只比 ffprobe 字段不够——ffprobe 读 dvvC 与 dvcC 都返回同一组值，看不出差别；
    比字节模板 + 比 box 名，才是 tvOS 真正看到的形态。
    """
    _, output = remux(SRC, tmp_path, seconds=6, segment_seconds=2)
    data = output.init.read_bytes()
    record = DvcConfig.parse(next(iter(locate_dv_boxes(data))).payload)
    assert (record.profile, record.level, record.rpu_present, record.el_present,
            record.bl_present, record.compat_id) == (5, record.level, True, False, True, 0)
    assert record.encode()[:5].hex().startswith("01000a35"), record.encode().hex()
    assert b"dvcC" in data and b"dvvC" not in data


@needs_source
def test_end_to_end_rpu_is_self_consistent_for_profile_5(tmp_path: Path):
    """单层 P5 的 RPU 必须自洽——这是"徽标能不能亮"的可观测代理。

    P7 源丢 EL 后 RPU 仍带 NLQ、未禁用残差，是伪 P5，tvOS 拒播；真机实验的
    教训就是别只信信令。这条保证 P8 通路的产物不犯同一个错。
    """
    _, output = remux(SRC, tmp_path, seconds=6, segment_seconds=2)
    assert check_rpu_consistency(output) == ()


_P7_SRC = Path(os.environ.get("DVPACK_SOURCE_P7_MKV", ""))

needs_p7_source = pytest.mark.skipif(
    not (_P7_SRC.is_file() and FFMPEG.exists()), reason="缺 P7 片源或 ffmpeg"
)


@needs_p7_source
def test_verify_reports_inconsistent_rpu_as_problem(tmp_path: Path):
    """反面控制：mode 1 造出来的伪 P5 必须被 RPU 自洽性判据抓到。

    真机教训：P7 源丢 EL 后 RPU 仍是 nlq=linear_dz、残差未禁用——信令转签得
    再看不出破绽，tvOS 也拒播。mode 5 是修复姿势（nlq=none + 残差禁用）：
    这条用例走同一条链路、只把 dovi_tool convert 的模式换成 1，保证判据
    闻得出没走 mode 5 的产物。
    """
    import subprocess
    import sys

    out = tmp_path / "dv_p7"
    proc = subprocess.run(
        [sys.executable, "scripts/p7_to_p5.py", str(_P7_SRC), "-o", str(out), "--seconds", "6", "--mode", "1"],
        capture_output=True, text=True, encoding="utf-8",
    )
    assert proc.returncode == 0, proc.stderr
    output = resolve_output(out / "video.m3u8")
    problems = check_rpu_consistency(output)
    assert any("RPU" in problem for problem in problems), problems


def test_end_to_end_produces_dvh1_with_config_box(tmp_path: Path):
    verification = remux_and_verify(SRC, tmp_path, seconds=6, segment_seconds=2)
    assert verification.ok, verification.problems
    assert verification.signalling == "dvh1+dvcC"
    # 源是 P8：信令必须按 Apple 规范转签成 P5，而不是"和源逐字段相同"
    assert verification.dovi_relabelled
    assert verification.output_dovi.profile == 5
    assert verification.target_dovi.profile == 5


@needs_source
def test_missing_strict_unofficial_loses_the_config_box(tmp_path: Path):
    """反面控制：只改 fourcc 不给 `-strict unofficial`，DV 信令就不成立。"""
    playlist = tmp_path / "video.m3u8"
    argv = [
        str(FFMPEG), "-hide_banner", "-loglevel", "error", "-y", "-t", "6", "-i", str(SRC),
        "-map", "0:v:0", "-c", "copy", "-tag:v", "dvh1",
        "-f", "hls", "-hls_segment_type", "fmp4", "-hls_time", "2", "-hls_list_size", "0",
        "video.m3u8",
    ]
    subprocess.run(argv, capture_output=True, text=True, encoding="utf-8", check=True, cwd=tmp_path)
    video = parse_file(resolve_output(playlist).init).video
    assert video.sample_entry_type == "dvh1", "fourcc 改名与配置 box 是两件事"
    assert video.dvcc is None, "少了 -strict unofficial 却写出了配置 box，说明判据失效"
    assert video.signalling == "dvh1 但缺 dvC"

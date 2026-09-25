"""remux 测试：命令行构成 + 真文件端到端 + 一条"少了 flag 会怎样"的反面控制。

反面控制很重要：`-strict unofficial` 看起来像多余的历史包袱，很容易被后人当噪音删掉，
而删掉的后果是 init 段不再有 DV 配置 box、电视静默地当普通 HDR 放。
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
from dvpack.bmff import parse_file
from dvpack.probe import parse_probe_json
from dvpack.remux import RemuxError, hls_command, remux, remux_and_verify, resolve_output
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
def test_end_to_end_produces_dvh1_with_config_box(tmp_path: Path):
    verification = remux_and_verify(SRC, tmp_path, seconds=6, segment_seconds=2)
    assert verification.ok, verification.problems
    assert verification.signalling == "dvh1+dvvC"
    assert verification.dovi_preserved
    assert verification.output_dovi.profile == 5


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
    assert video.signalling == "dvh1 但缺 dvvC"

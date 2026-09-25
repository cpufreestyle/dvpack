"""master playlist 的写法测试。

重点是那三个决定电视是否走 DV 分支的属性：CODECS、VIDEO-RANGE、HDCP-LEVEL，
以及 Profile 5 不该出现 SUPPLEMENTAL-CODECS（它没有回退层）。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from dvpack.playlist import (
    bandwidth_from_segments,
    master_playlist,
    variant_from_source,
    write_master_playlist,
)
from dvpack.probe import parse_probe_json


def _source(*, stream_overrides: dict | None = None, **side) -> object:
    """造一份 SourceInfo：side 改 DOVI 字段，stream_overrides 改容器实测字段。"""
    stream = {
        "index": 0,
        "codec_type": "video",
        "codec_name": "hevc",
        "width": 3840,
        "height": 2160,
        "color_transfer": "smpte2084",
        "avg_frame_rate": "24000/1001",
        "side_data_list": [
            {
                "side_data_type": "DOVI configuration record",
                "dv_version_major": 1,
                "dv_version_minor": 0,
                "dv_profile": 5,
                "dv_level": 6,
                "rpu_present_flag": 1,
                "el_present_flag": 0,
                "bl_present_flag": 1,
                "dv_bl_signal_compatibility_id": 0,
                "dv_md_compression": "none",
            }
        ],
    }
    stream["side_data_list"][0].update(side)
    stream.update(stream_overrides or {})
    return parse_probe_json({"streams": [stream], "format": {"duration": "10"}}, Path("x.mkv"))


def test_profile5_variant_carries_full_apple_signalling():
    variant = variant_from_source(_source(), "video.m3u8", bandwidth=13_000_000)
    line = variant.stream_inf()
    assert 'CODECS="dvh1.05.06"' in line
    assert "VIDEO-RANGE=PQ" in line
    assert "HDCP-LEVEL=TYPE-1" in line
    assert "RESOLUTION=3840x2160" in line
    assert "SUPPLEMENTAL-CODECS" not in line, "P5 无回退层，写兼容 brand 是错的"


def test_frame_rate_rounds_up_not_down():
    line = variant_from_source(_source(), "v.m3u8", bandwidth=1).stream_inf()
    assert "FRAME-RATE=24" in line, "23.976 必须向上取整成 24"


def test_profile81_gets_hdr10_compat_brand():
    dovi8 = _source(dv_profile=8, dv_level=7, dv_bl_signal_compatibility_id=1)
    line = variant_from_source(dovi8, "v.m3u8", bandwidth=1).stream_inf()
    assert 'CODECS="dvh1.08.07"' in line
    assert 'SUPPLEMENTAL-CODECS="dvh1.08.07,db1p"' in line


def test_profile84_pairs_with_hlg_brand():
    dovi84 = _source(
        stream_overrides={"color_transfer": ""},
        dv_profile=8,
        dv_level=7,
        dv_bl_signal_compatibility_id=4,
    )
    line = variant_from_source(dovi84, "v.m3u8", bandwidth=1).stream_inf()
    assert "VIDEO-RANGE=HLG" in line
    assert "db4h" in line


def test_no_dovi_record_refuses_to_build_variant():
    plain = parse_probe_json(
        {"streams": [{"index": 0, "codec_type": "video", "codec_name": "hevc"}], "format": {}},
        Path("x.mkv"),
    )
    with pytest.raises(ValueError, match="DOVI"):
        variant_from_source(plain, "v.m3u8", bandwidth=1)


def test_master_playlist_layout():
    variant = variant_from_source(_source(), "video.m3u8", bandwidth=100)
    text = master_playlist([variant])
    assert text.startswith("#EXTM3U\n#EXT-X-VERSION:7\n#EXT-X-INDEPENDENT-SEGMENTS\n")
    assert text.splitlines()[3].startswith("#EXT-X-STREAM-INF:")
    assert text.splitlines()[4] == "video.m3u8"
    assert text.endswith("\n")


def test_written_playlist_uses_lf_on_windows(tmp_path: Path):
    """Windows 文本模式会换成 CRLF；这条钉住我们写的是 LF。"""
    out = write_master_playlist(tmp_path / "master.m3u8", [variant_from_source(_source(), "v.m3u8", bandwidth=1)])
    assert b"\r\n" not in out.read_bytes()


def test_bandwidth_uses_measured_segment_bytes_with_margin(tmp_path: Path):
    for name, size in (("s0.m4s", 1000), ("s1.m4s", 3000)):
        (tmp_path / name).write_bytes(b"\0" * size)
    segments = [tmp_path / "s0.m4s", tmp_path / "s1.m4s"]
    assert bandwidth_from_segments(segments, 10.0) == int(4000 * 8 / 10 * 1.2)
    with pytest.raises(ValueError):
        bandwidth_from_segments(segments, 0)

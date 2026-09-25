"""探测逻辑测试。

JSON 样本是真实 ffprobe 输出（本机 ffprobe 实测抓取，非编造），
不是编造的结构，所以 side_data 字段名一旦与本文件不符，说明 ffprobe 版本变了。
"""

from __future__ import annotations

import copy
import os
from pathlib import Path

import pytest
from dvpack.probe import (
    DoviRecord,
    NotProbeable,
    UnknownTransfer,
    mvp_verdict,
    parse_fraction,
    parse_probe_json,
    probe,
)

P5_L6 = {
    "index": 0,
    "codec_type": "video",
    "codec_name": "hevc",
    "profile": "Main 10",
    "width": 3840,
    "height": 2160,
    "bit_depth": 10,
    "color_space": "bt2020nc",
    "color_transfer": "smpte2084",
    "color_primaries": "bt2020",
    "avg_frame_rate": "24000/1001",
    "r_frame_rate": "24000/1001",
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

P8_L7 = copy.deepcopy(P5_L6)
P8_L7["side_data_list"][0].update(
    {"dv_profile": 8, "dv_level": 7, "dv_bl_signal_compatibility_id": 1}
)

DUAL_LAYER_P7 = copy.deepcopy(P5_L6)
DUAL_LAYER_P7["side_data_list"][0].update(
    {"dv_profile": 7, "dv_level": 6, "el_present_flag": 1}
)

NO_DOVI = {k: v for k, v in P5_L6.items() if k != "side_data_list"}


def _wrap(stream: dict) -> dict:
    return {
        "streams": [stream],
        "format": {
            "format_name": "matroska,webm",
            "duration": "3291.7",
            "size": "18455048213",
        },
    }


def test_parses_dovi_record_from_real_json():
    source = parse_probe_json(_wrap(P5_L6), Path("x.mkv"))
    dovi = source.dovi
    assert dovi == DoviRecord(
        profile=5,
        level=6,
        rpu_present=True,
        el_present=False,
        bl_present=True,
        compat_id=0,
        md_compression="none",
        spec_version="1.0",
    )
    assert source.video.bit_depth == 10
    assert source.video.frame_rate == 24000 / 1001


@pytest.mark.parametrize(
    "profile,level,want",
    [
        (5, 6, "dvh1.05.06"),  # Apple 附录里的真实例子
        (5, 1, "dvh1.05.01"),
        (8, 9, "dvh1.08.09"),
    ],
)
def test_apple_codec_is_two_digit_zero_padded(profile, level, want):
    record = _dovi(profile=profile, level=level)
    assert record.target_apple_codec == want


def _dovi(**over) -> DoviRecord:
    base = dict(
        profile=5,
        level=6,
        rpu_present=True,
        el_present=False,
        bl_present=True,
        compat_id=0,
        md_compression="none",
        spec_version="1.0",
    )
    return DoviRecord(**{**base, **over})


def test_video_range_prefers_measured_transfer():
    assert parse_probe_json(_wrap(P5_L6), Path("x.mkv")).video.apple_video_range == "PQ"
    hlg = copy.deepcopy(P5_L6)
    hlg["color_transfer"] = "arib-std-b67"
    assert parse_probe_json(_wrap(hlg), Path("x.mkv")).video.apple_video_range == "HLG"


def test_video_range_falls_back_to_profile_when_container_has_no_colour_tags():
    """实测形态：本机 MKV rip 不写 colour tags，color_transfer 是空字符串。"""
    no_colour = copy.deepcopy(P5_L6)
    no_colour["color_transfer"] = ""
    no_colour["color_space"] = ""
    no_colour["color_primaries"] = ""
    source = parse_probe_json(_wrap(no_colour), Path("x.mkv"))
    assert source.video.color_transfer == ""
    assert source.video.apple_video_range == "PQ"


def test_hlg_base_layer_detected_from_compat_id_when_transfer_missing():
    hlg = copy.deepcopy(P5_L6)
    hlg["color_transfer"] = ""
    hlg["side_data_list"][0].update({"dv_profile": 8, "dv_bl_signal_compatibility_id": 4})
    assert parse_probe_json(_wrap(hlg), Path("x.mkv")).video.apple_video_range == "HLG"


def test_unmappable_range_raises_instead_of_guessing():
    """非 DV 片源且容器没给 transfer：宁可报错，也不猜一个 VIDEO-RANGE。"""
    bad = copy.deepcopy(P5_L6)
    bad["color_transfer"] = ""
    del bad["side_data_list"]
    source = parse_probe_json(_wrap(bad), Path("x.mkv"))
    with pytest.raises(UnknownTransfer):
        _ = source.video.apple_video_range


def test_single_layer_p5_is_supported():
    verdict = mvp_verdict(parse_probe_json(_wrap(P5_L6), Path("x.mkv")))
    assert verdict.supported and verdict.warnings == ()


def test_p5_level9_is_supported_but_flagged_as_risk():
    risky = copy.deepcopy(P5_L6)
    risky["side_data_list"][0]["dv_level"] = 9
    verdict = mvp_verdict(parse_probe_json(_wrap(risky), Path("x.mkv")))
    assert verdict.supported
    assert any("level 9" in w for w in verdict.warnings)


def test_p8_level7_is_not_flagged_because_that_level_is_normal_for_p8():
    """风险项来自"Profile 5 在 level 7/9 不点亮 DV"的报告，套到 P8 上就是噪音。

    片库里 P8 L7 有 2 个文件（我们的验收对象之一），以前每次跑矩阵都被误报一次。
    """
    verdict = mvp_verdict(parse_probe_json(_wrap(P8_L7), Path("x.mkv")))
    assert verdict.supported and verdict.warnings == ()


def test_dual_layer_profile_7_is_refused():
    verdict = mvp_verdict(parse_probe_json(_wrap(DUAL_LAYER_P7), Path("x.mkv")))
    assert not verdict.supported
    assert "双层" in verdict.reason


@pytest.mark.parametrize("stream", [NO_DOVI])
def test_missing_dovi_is_refused(stream):
    verdict = mvp_verdict(parse_probe_json(_wrap(stream), Path("x.mkv")))
    assert not verdict.supported
    assert "DOVI" in verdict.reason


def test_missing_rpu_is_refused_because_it_would_silently_degrade():
    norev = copy.deepcopy(P5_L6)
    norev["side_data_list"][0]["rpu_present_flag"] = 0
    verdict = mvp_verdict(parse_probe_json(_wrap(norev), Path("x.mkv")))
    assert not verdict.supported
    assert "RPU" in verdict.reason


def test_no_video_stream_raises():
    obj = {"streams": [{"index": 1, "codec_type": "audio"}], "format": {}}
    with pytest.raises(NotProbeable):
        parse_probe_json(obj, Path("x.mkv"))


@pytest.mark.parametrize(
    "text,want",
    [("24000/1001", 23.976), ("0/0", 0.0), ("", 0.0), (None, 0.0), ("25/1", 25.0)],
)
def test_parse_fraction(text, want):
    assert parse_fraction(text) == pytest.approx(want, abs=1e-3)


def test_probe_real_library_file_matches_container_ffprobe():
    """跑一次本机 ffprobe，确认与扫盘时（容器内 ffprobe 8.1.2）看到的字段一致。"""
    real = Path(os.environ.get("DVPACK_SOURCE_P8_L7_MKV", ""))
    if not real.is_file():
        pytest.skip("片库不在本机")
    source = probe(real)
    assert source.dovi.profile == 8
    assert source.dovi.level == 7
    assert source.dovi.el_present is False
    assert source.dovi.rpu_present is True
    assert source.dovi.compat_id == 1
    assert source.video.width == 3840

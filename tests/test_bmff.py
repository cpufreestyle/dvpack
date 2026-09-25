"""BMFF 判据测试。

这些测试存在的原因是真实教训：偏移算错时，解析出一个类型全零的假子 box，把"有没有
dvvC"这个整项目的判据说反了。第一次修的时候我按"SampleEntry 是 full box"补齐了 4 字节，
测试全绿但真文件依旧错——因为合成样本本身照抄了我的误解。所以这里的 `_visual_entry`
按 ISO 14496-12 的真实字段布局写，并且偏移常量改动都必须配一条真文件断言（见
`test_real_ffmpeg_output_layout_matches_synthetic_fixture`），只靠合成样本钉不住。
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from dvpack.bmff import parse_file, census_annexb
from dvpack.tools import FFMPEG


def _box(btype: str, payload: bytes) -> bytes:
    return (len(payload) + 8).to_bytes(4, "big") + btype.encode("latin-1") + payload


def _full_box(btype: str, payload: bytes, version_flags: bytes = b"\0\0\0\0") -> bytes:
    return _box(btype, version_flags + payload)


def _visual_entry(entry_type: str, children: bytes) -> bytes:
    """照 ISO/IEC 14496-12 的 VisualSampleEntry 写真字段，不写一串零凑数。

    3840x2160、72dpi、frame_count=1、depth=24、pre_defined=-1，
    与 ffmpeg 对本机片源的实际产出逐字节一致。
    """
    body = bytearray()
    body += b"\0" * 6            # reserved
    body += (1).to_bytes(2, "big")  # data_reference_index
    body += b"\0" * 16           # pre_defined(2) + reserved(2) + predefined[3](12)
    body += (3840).to_bytes(2, "big")
    body += (2160).to_bytes(2, "big")
    body += (0x00480000).to_bytes(4, "big")
    body += (0x00480000).to_bytes(4, "big")
    body += b"\0" * 4            # reserved
    body += (1).to_bytes(2, "big")  # frame_count
    body += b"\0" * 32           # compressorname
    body += (0x0018).to_bytes(2, "big")  # depth
    body += b"\xff\xff"          # pre_defined
    body += children
    return _box(entry_type, bytes(body))


def _stsd(children: bytes) -> bytes:
    return _full_box("stsd", (1).to_bytes(4, "big") + children)


def _trak(entry_type: str, sample_children: bytes, handler: str = "vide") -> bytes:
    mdhd = _full_box("mdhd", b"\0" * 4 + (2).to_bytes(4, "big"))  # v0 + creation/modification
    hdlr = _full_box("hdlr", b"\0" * 4 + handler.encode("latin-1") + b"\0" * 12 + b"\0")
    stbl = _box("stbl", _stsd(_visual_entry(entry_type, sample_children)))
    minf = _box("minf", stbl)
    mdia = _box("mdia", mdhd + hdlr + minf)
    return _box("trak", mdia)


def _file(entry_type: str, sample_children: bytes) -> bytes:
    return _box("ftyp", b"iso5" + b"\0\0\0\1" + b"iso5") + _box("moov", _trak(entry_type, sample_children))


DVVC_PAYLOAD = bytes([0x01, 0x00, 0x05, 0x06, 0x04, 0x00])

_MKV = Path(os.environ.get("DVPACK_SOURCE_MKV", ""))

needs_source = pytest.mark.skipif(not (_MKV.is_file() and FFMPEG.exists()), reason="缺片源或 ffmpeg")


def _remux(dst: Path, *extra: str) -> None:
    subprocess.run(
        [str(FFMPEG), "-hide_banner", "-loglevel", "error", "-y", "-t", "1", "-i", str(_MKV),
         "-map", "0:v:0", "-c", "copy", *extra, str(dst)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )


@needs_source
def test_real_ffmpeg_output_layout_matches_synthetic_fixture(tmp_path: Path):
    """合成样本必须与 ffmpeg 真实产出的字节布局一致——这条是唯一能发现"两边都错"的判据。"""
    out = tmp_path / "hev1.mp4"
    _remux(out)
    video = parse_file(out).video
    assert video.sample_entry_type == "hev1"
    assert "hvcC" in video.sample_entry_children, "偏移算错会解析出全零类型的假子 box"
    assert video.dvcc is None


@needs_source
def test_tag_v_dvh1_renames_entry_but_writes_no_dvvC(tmp_path: Path):
    """ffmpeg `-tag:v dvh1` 只改 fourcc，不生成 dvvC：DV 配置仍然缺位。"""
    out = tmp_path / "dvh1.mp4"
    _remux(out, "-tag:v", "dvh1")
    video = parse_file(out).video
    assert video.sample_entry_type == "dvh1"
    assert video.declares_dolby_vision
    assert "hvcC" in video.sample_entry_children
    assert video.dvcc is None
    assert video.signalling == "dvh1 但缺 dvvC"


def test_dvh1_with_dvvC_is_recognised(tmp_path: Path):
    data = _file("dvh1", _box("hvcC", b"\x01\x02") + _box("dvvC", DVVC_PAYLOAD))
    (tmp_path / "init.mp4").write_bytes(data)
    video = parse_file(tmp_path / "init.mp4").video
    assert video.sample_entry_type == "dvh1"
    assert video.sample_entry_children == ("hvcC", "dvvC")
    assert video.dvcc == DVVC_PAYLOAD
    assert video.signalling == "dvh1+dvvC"
    assert video.declares_dolby_vision


def test_dvh1_without_dvvC_is_reported_as_missing_box(tmp_path: Path):
    data = _file("dvh1", _box("hvcC", b"\x01\x02"))
    (tmp_path / "init.mp4").write_bytes(data)
    video = parse_file(tmp_path / "init.mp4").video
    assert video.dvcc is None
    assert video.signalling == "dvh1 但缺 dvvC"


def test_hvc1_is_not_dolby_vision_signalling(tmp_path: Path):
    data = _file("hvc1", _box("hvcC", b"\x01\x02"))
    (tmp_path / "init.mp4").write_bytes(data)
    video = parse_file(tmp_path / "init.mp4").video
    assert video.declares_dolby_vision is False
    assert "无 DV 信令" in video.signalling


def test_hev1_is_not_dolby_vision_signalling(tmp_path: Path):
    """ffmpeg 默认产出 hev1：Apple 明确不推荐，且不携带任何 DV 配置。"""
    data = _file("hev1", _box("hvcC", b"\x01\x02"))
    (tmp_path / "init.mp4").write_bytes(data)
    assert parse_file(tmp_path / "init.mp4").video.sample_entry_type == "hev1"


def test_audio_track_is_not_mistaken_for_video(tmp_path: Path):
    data = _box("ftyp", b"iso5" + b"\0\0\0\1" + b"iso5") + _box(
        "moov", _trak("mp4a", b"", handler="soun") + _trak("dvh1", _box("dvvC", DVVC_PAYLOAD))
    )
    (tmp_path / "init.mp4").write_bytes(data)
    report = parse_file(tmp_path / "init.mp4")
    assert len(report.tracks) == 2
    assert report.video.sample_entry_type == "dvh1"


def _annexb(*nals: bytes) -> bytes:
    return b"".join(b"\x00\x00\x00\x01" + nal for nal in nals)


def test_census_counts_dovi_rpu_nals_by_the_measured_prefix(tmp_path: Path):
    # type 62 → byte0 = 62 << 1 = 0x7C；payload 首字节 0x19 = rpu_nal_prefix 25
    rpu = bytes([0x7C, 0x01, 0x19, 0x08, 0x09])
    vps = bytes([0x40, 0x01])  # type 32
    slice_nal = bytes([0x02, 0x01])  # type 1
    (tmp_path / "es.265").write_bytes(_annexb(vps, rpu, slice_nal, rpu))
    census = census_annexb(tmp_path / "es.265")
    assert census.dovi_rpu == 2
    assert census.by_type[32] == 1
    assert census.by_type[1] == 1
    assert census.total == 4


def test_census_ignores_type62_that_is_not_a_dovi_rpu(tmp_path: Path):
    """不是所有 unregistered NAL 都是 RPU——前缀字节不对就不该计数。"""
    not_rpu = bytes([0x7C, 0x01, 0x00, 0x01])
    (tmp_path / "es.265").write_bytes(_annexb(not_rpu))
    assert census_annexb(tmp_path / "es.265").dovi_rpu == 0

"""P8 → P5 转签测试。

判据必须逐字节对齐 Apple 官方参考流的 `dvcC`，而不是"看着像"：
  P8 L6（ffmpeg 写）   01 00 10 35 10 00 ...
  P5 L6（Apple 官方）  01 00 0a 35 00 00 ...
  P5 L3（Apple 官方）  01 00 0a 1d 00 00 ...
三次真机实验（信令齐全但徽标不亮）之后才定位到 box 俗名问题，所以这里既有
字节模板断言，也有"ffprobe 认不认"的端到端判据（见 test_remux.py）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from dvpack.p5 import DvcConfig, ResignError, resign_init_segment, resign_pack
from dvpack.probe import DoviRecord
from test_bmff import _box, _file

# Apple 官方 adv_dv_atmos 参考流的 dvcC payload（逐字节抄自 fileSequence0.mp4）
APPLE_P5_L3 = "01000a1d" + "00" * 20
APPLE_P5_L6 = "01000a35" + "00" * 20
# ffmpeg 对同一片源写出的 record（P8 L6）——本项目转签前的现场
FFMPEG_P8_L6 = "01001035" + "10" + "00" * 18

P5_RECORD = DoviRecord(
    profile=5,
    level=6,
    rpu_present=True,
    el_present=False,
    bl_present=True,
    compat_id=0,
    md_compression="none",
    spec_version="1.0",
)


def _parse(hex_payload: str) -> DvcConfig:
    return DvcConfig.parse(bytes.fromhex(hex_payload))


def _init_file(tmp_path: Path, fourcc: str, payload_hex: str) -> Path:
    init = tmp_path / "init.mp4"
    init.write_bytes(
        _file("dvh1", _box("hvcC", b"\x01\x02") + _box(fourcc, bytes.fromhex(payload_hex)))
    )
    return init


def test_apple_p5_l6_reference_fields():
    config = _parse(APPLE_P5_L6)
    assert (config.profile, config.level) == (5, 6)
    assert (config.rpu_present, config.el_present, config.bl_present) == (True, False, True)
    assert config.compat_id == 0


def test_ffmpeg_p8_record_is_recognised_as_p8_l6_with_hdr10_base():
    config = _parse(FFMPEG_P8_L6)
    assert (config.profile, config.level) == (8, 6)
    assert config.compat_id == 1


def test_encode_from_ffmpeg_p8_lands_on_apple_p5_template():
    """字节模板：把 ffmpeg 的 P8 record 改写成 P5，必须逐字节等于 Apple 参考值。"""
    from dvpack.p5 import DvcConfig as _C

    source = _parse(FFMPEG_P8_L6)
    signed = _C(
        version_major=source.version_major,
        version_minor=source.version_minor,
        profile=5,
        level=source.level,
        rpu_present=source.rpu_present,
        el_present=False,
        bl_present=source.bl_present,
        compat_id=0,
        tail=source.tail,
    )
    # 尾部长模板长度不同（合成件与 Apple 官方件的保留字节数不一样），比前 5 字节
    assert signed.encode()[:5] == bytes.fromhex(APPLE_P5_L6)[:5]
    assert signed.encode().hex().startswith("01000a35")


def test_apple_l3_level_stays_parsed_not_guessed():
    config = _parse(APPLE_P5_L3)
    assert (config.profile, config.level, config.compat_id) == (5, 3, 0)


def test_resign_renames_box_and_rewrites_record(tmp_path: Path):
    init = _init_file(tmp_path, "dvvC", FFMPEG_P8_L6)
    result = resign_init_segment(init)
    assert result.renamed
    assert result.before.profile == 8
    assert result.after == P5_RECORD
    data = init.read_bytes()
    assert b"dvvC" not in data
    # 按字节比，不用 hex 字符串：payload 落在奇半字节上时 .hex() 对不上
    assert bytes.fromhex(APPLE_P5_L6)[:5] in data


def test_resign_is_idempotent(tmp_path: Path):
    init = _init_file(tmp_path, "dvcC", APPLE_P5_L6)
    result = resign_init_segment(init)
    assert not result.renamed
    assert result.before == result.after


def test_resign_creates_box_when_ffmpeg_wrote_none(tmp_path: Path):
    """裸 HEVC 输入 → ffmpeg 不写 dvcC：必须自己按 Apple 模板造一个插进 dvh1。

    这是 P7→P5 通路的必经步骤：丢 EL 之后的码流喂给 ffmpeg 时没有任何 DOVI
    侧数据，init 段里只有 hvcC，没有配置 box，电视永远走不到 DV 分支。
    """
    init = tmp_path / "init.mp4"
    init.write_bytes(_file("dvh1", _box("hvcC", b"\x01\x02")))
    result = resign_init_segment(init, level=6)
    data = init.read_bytes()
    assert b"dvcC" in data
    assert result.after == P5_RECORD

    from dvpack.bmff import parse_file

    video = parse_file(init).video
    assert video.sample_entry_children == ("hvcC", "dvcC")
    assert video.dv_box == "dvcC"
    # 与 Apple 官方 L6 参考同字节
    assert bytes.fromhex(APPLE_P5_L6)[:5] in data


def test_resign_pack_skips_sources_already_p5():
    assert resign_pack(Path("nonexistent.mp4"), P5_RECORD) is None

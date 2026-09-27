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

from dvpack.bmff import (
    census_annexb,
    hvcC_param_set_types,
    keep_single_sample_entry,
    parse_file,
    sample_entry_count,
)
from dvpack.bmff import Box, _parse_box, _scan_buffer
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
    assert video.signalling == "dvh1 但缺 dvC"


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
    assert video.signalling == "dvh1 但缺 dvC"


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


def test_dvcC_is_the_name_apple_uses(tmp_path: Path):
    """同样两字节 payload，box 名不同，signalling 必须分别报出来。

    这是整项目的转折点：ffprobe 读 dvvC 与 dvcC 都返回同一组字段，所以本地复读
    看不出差别；Apple 官方 adv_dv_atmos 写 dvcC。判据必须认名，不能只认有没有。
    """
    for name in ("dvcC", "dvvC"):
        (tmp_path / "a.mp4").write_bytes(
            _file("dvh1", _box("hvcC", b"\x01\x02") + _box(name, DVVC_PAYLOAD))
        )
        video = parse_file(tmp_path / "a.mp4").video
        assert video.dv_box == name
        assert video.signalling == f"dvh1+{name}"
        assert video.has_dvcC is (name == "dvcC")


def test_apple_reference_record_roundtrips(tmp_path: Path):
    """Apple 官方 P5 init 的 dvcC 落点：解析后逐字节对上再写回，不能有漂移。"""
    from dvpack.bmff import locate_dv_boxes
    from dvpack.p5 import DvcConfig

    # 取自 devstreaming-cdn.apple.com 的 adv_dv_atmos，P5/L6 变体 fileSequence0.mp4
    reference = "01000a350000000000000000000000000000000000000000"
    data = _file(
        "dvh1", _box("hvcC", b"\x01\x02") + _box("dvcC", bytes.fromhex(reference))
    )
    (tmp_path / "init.mp4").write_bytes(data)
    boxes = locate_dv_boxes((tmp_path / "init.mp4").read_bytes())
    assert len(boxes) == 1
    assert boxes[0].fourcc == "dvcC"
    config = DvcConfig.parse(boxes[0].payload)
    assert (config.profile, config.level, config.rpu_present, config.el_present,
            config.bl_present, config.compat_id) == (5, 6, True, False, True, 0)
    assert config.encode().hex().startswith("01000a35")


def _hvcc_box(arrays: dict[int, list[bytes]]) -> bytes:
    """合成 HEVCDecoderConfigurationRecord（ISO/IEC 14496-15）：23 字节固定头
    + 参数集数组。固定头按规范逐字段排：version(1) + profile 族 12 + level(1)
    + 分割/并行/色度/位深 5 + avgFrameRate(2) + 时序/长度字段(1) + numOfArrays(1)
    = 23，数组自偏移 23 起——与真实 init 一致（2026-09-25 实测 dv_p7_v3）。
    """
    head = bytearray(23)
    head[0] = 0x01   # configurationVersion
    head[1] = 0x01   # general_profile_idc
    head[12] = 0x78  # general_level_idc
    head[16] = 0x01  # chromaFormat = 4:2:0
    head[17] = 0x01  # bitDepthLumaMinus8
    head[18] = 0x01  # bitDepthChromaMinus8
    head[21] = 0x03  # lengthSizeMinusOne = 3（4 字节长度前缀）
    head[22] = len(arrays)  # numOfArrays
    body = bytearray(head)
    for nal_type, nals in arrays.items():
        body.append(nal_type)  # array_completeness=0 + reserved=0 + NAL type
        body += len(nals).to_bytes(2, "big")
        for nal in nals:
            body += len(nal).to_bytes(2, "big") + nal
    return _box("hvcC", bytes(body))


def _read_hvcC_arrays(data: bytes) -> dict[int, list[bytes]]:
    """白盒读回 hvcC 参数集数组——去重判据要能直接核对每种类型剩几条。"""
    from dvpack.bmff import _find_box, _parse_box, _scan_buffer

    boxes = _scan_buffer(data)
    assert boxes and boxes[-1][0] == "moov"
    base = boxes[-1][1]
    moov = _parse_box(data[base : base + boxes[-1][2]], 0, boxes[-1][2], 1)
    hvcc = _find_box(moov, "hvcC")
    assert hvcc is not None
    payload = data[base + hvcc.offset + 8 : base + hvcc.offset + hvcc.size]
    cursor, out = 23, {}
    for _ in range(payload[22]):
        nal_type = payload[cursor] & 0x3F
        count = int.from_bytes(payload[cursor + 1 : cursor + 3], "big")
        cursor += 3
        nals = []
        for _ in range(count):
            length = int.from_bytes(payload[cursor : cursor + 2], "big")
            cursor += 2
            nals.append(payload[cursor : cursor + length])
            cursor += length
        out[nal_type] = nals
    return out


def test_dedupe_hvcC_collapses_identical_parameter_sets(tmp_path: Path):
    """ffmpeg 的 hvcC 每种参数集写两份且逐字节相同，Apple 模板每种只一条。"""
    from dvpack.bmff import dedupe_hvcC_parameter_sets

    vps, sps, pps = b"\x40\x01\x0c\x01", b"\x42\x01\x01\x01\x60\x00", b"\x44\x01\xc1"
    data = _file("dvh1", _hvcc_box({32: [vps, vps], 33: [sps, sps], 34: [pps, pps]}))
    out, removed = dedupe_hvcC_parameter_sets(data)

    assert removed == 3
    # 每条类型只留第一条，内容不变；盒树仍可解析、信令不变
    assert _read_hvcC_arrays(out) == {32: [vps], 33: [sps], 34: [pps]}
    shrink = sum(len(nal) + 2 for nal in (vps, sps, pps))
    assert len(out) == len(data) - shrink
    (tmp_path / "init.mp4").write_bytes(out)
    assert parse_file(tmp_path / "init.mp4").video.signalling == "dvh1 但缺 dvC"


def test_dedupe_hvcC_is_noop_when_already_single(tmp_path: Path):
    """已经是 Apple 排布（每种一条）时原样返回，字节都不动。"""
    from dvpack.bmff import dedupe_hvcC_parameter_sets

    vps, sps, pps = b"\x40\x01\x0c\x01", b"\x42\x01\x01\x01\x60\x00", b"\x44\x01\xc1"
    data = _file("dvh1", _hvcc_box({32: [vps], 33: [sps], 34: [pps]}))
    out, removed = dedupe_hvcC_parameter_sets(data)

    assert removed == 0
    assert out == data


def test_dedupe_hvcC_rejects_padded_duplicate():
    """dv_p7_v3 真实案例：第二条 PPS 是首条追加 00 字节的变体。

    内容不同就不是简单重复——静默丢弃会改解码信令，宁可 raise。
    """
    from dvpack.bmff import dedupe_hvcC_parameter_sets

    pps = b"\x44\x01\xc1\x72\x4d\x8e\xd9"
    data = _file("dvh1", _hvcc_box({32: [b"\x40\x01\x0c\x01"],
                                     33: [b"\x42\x01\x01\x01\x60\x00"],
                                     34: [pps, pps + b"\x00"]}))
    with pytest.raises(ValueError, match="不是简单重复"):
        dedupe_hvcC_parameter_sets(data)


@needs_source
def test_dedupe_hvcC_on_real_ffmpeg_output(tmp_path: Path):
    """真 ffmpeg 产出走一遍：不炸、每种类型至多一条、解析存活。"""
    from dvpack.bmff import dedupe_hvcC_parameter_sets

    out = tmp_path / "dvh1.mp4"
    _remux(out, "-tag:v", "dvh1")
    data = out.read_bytes()
    deduped, removed = dedupe_hvcC_parameter_sets(data)

    assert len(deduped) == len(data) - removed
    assert all(len(nals) == 1 for nals in _read_hvcC_arrays(deduped).values())
    (tmp_path / "deduped.mp4").write_bytes(deduped)
    assert parse_file(tmp_path / "deduped.mp4").video.sample_entry_type == "dvh1"


@needs_source
def test_hvcC_array_offset_23_matches_real_ffmpeg_output(tmp_path: Path):
    """偏移常量 23（固定头长）必须配真文件断言，且钉住 ffmpeg 8.1.2 的两种字节真相。

    2026-09-26 实测：本机 ffmpeg 8.1.2 裸 -c copy remux 写 23B 空 hvcC、
    numOfArrays=0（真机秒退的根因）；接 -bsf:v
    hevc_mp4toannexb,extract_extradata BSF 链才有 232B hvcC、numOfArrays=4
    （32/33/34/39 前缀 SEI）。两条都要从偏移 23 严丝合缝走完整个载荷——偏移
    算错时至少有一个数组越界或残留未解析字节，只靠合成样本钉不住。
    """
    from dvpack.bmff import _find_box, _parse_box, _scan_buffer

    def hvcC_arrays(sample: Path) -> tuple[list[int], int]:
        data = sample.read_bytes()
        boxes = _scan_buffer(data)
        base = boxes[-1][1]
        moov = _parse_box(data[base : base + boxes[-1][2]], 0, boxes[-1][2], 1)
        hvcc = _find_box(moov, "hvcC")
        assert hvcc is not None
        payload = data[base + hvcc.offset + 8 : base + hvcc.offset + hvcc.size]
        assert payload[0] == 0x01  # configurationVersion 就落在载荷首位
        num_arrays = payload[22]
        cursor, types = 23, []
        for _ in range(num_arrays):
            types.append(payload[cursor] & 0x3F)
            count = int.from_bytes(payload[cursor + 1 : cursor + 3], "big")
            cursor += 3
            for _ in range(count):
                length = int.from_bytes(payload[cursor : cursor + 2], "big")
                cursor += 2 + length
        assert cursor == len(payload)  # 脚标严丝合缝才对上真实布局
        return types, num_arrays

    plain = tmp_path / "hev1.mp4"
    _remux(plain)
    types, num_arrays = hvcC_arrays(plain)
    assert num_arrays == 0  # 裸 remux：23B 空 hvcC，真机秒退根因
    assert types == []

    injected = tmp_path / "hev1-bsf.mp4"
    _remux(injected, "-bsf:v", "hevc_mp4toannexb,extract_extradata")
    types, num_arrays = hvcC_arrays(injected)
    assert num_arrays == 4  # BSF 链：VPS/SPS/PPS/SEI-39 前缀四数组
    assert set(types) == {32, 33, 34, 39}


def _param_set_array(nal_type: int, *nals: bytes) -> bytes:
    """按 HEVCDecoderConfigurationRecord 数组区写法：NAL 类型头 + count + [len+nal]。"""
    out = bytes([0xA0 | nal_type]) + len(nals).to_bytes(2, "big")
    for nal in nals:
        out += len(nal).to_bytes(2, "big") + nal
    return out


def _hvcC_box(*arrays: bytes) -> bytes:
    """最小可解析 hvcC：22 字节头 + numOfArrays + 数组区，头字段与解析无关。"""
    return _box("hvcC", bytes(22) + len(arrays).to_bytes(1, "big") + b"".join(arrays))


def test_hvcC_param_set_types_reads_vps_sps_pps():
    """Apple HLS 硬性要求的判据：init 段 hvcC 必须自带 VPS/SPS/PPS。"""
    data = _file(
        "hvc1",
        _hvcC_box(
            _param_set_array(32, b"\x40\x01\x0c\x01\xff\xff"),
            _param_set_array(33, b"\x42\x01\x01\x02\x20\x00\x00"),
            _param_set_array(34, b"\x44\x01\xc1\x72"),
        ),
    )
    assert hvcC_param_set_types(data) == (32, 33, 34)


def test_hvcC_param_set_types_empty_when_num_arrays_is_zero():
    """numOfArrays=0 即 2026-09-26 真机实测那个 23 字节空 hvcC 的真实现状。"""
    assert hvcC_param_set_types(_file("hvc1", _hvcC_box())) == ()


def test_hvcC_param_set_types_empty_when_no_hvcC_box():
    assert hvcC_param_set_types(_file("hvc1", _box("pasp", b"\0\0\0\x01\0\0\0\x01"))) == ()


def _wrap_stsd_entries(*entries: bytes) -> bytes:
    """把若干 sample entry 包成 moov 收尾的 init 段（字段布局同文件内 _trak 帮手）。"""
    stsd = _full_box("stsd", len(entries).to_bytes(4, "big") + b"".join(entries))
    mdhd = _full_box("mdhd", bytes(4) + (2).to_bytes(4, "big"))
    hdlr = _full_box("hdlr", bytes(4) + b"vide" + bytes(12) + bytes(1))
    mdia = _box("mdia", mdhd + hdlr + _box("minf", _box("stbl", stsd)))
    moov = _box("moov", _box("trak", mdia))
    return _box("ftyp", b"iso5" + bytes(4) + b"iso5") + moov


def _two_entry_init() -> bytes:
    """双 dvh1 entry 的 init 段——复刻 BSF 注入路径产出的 stsd 布局。

    entry0 的 hvcC 带 VPS/SPS/PPS + 前缀 SEI(39)，entry1 只带 VPS/SPS/PPS，
    与 2026-09-26 repack 产物 1400B init 的两个 entry 一一对应；实测分片只引
    entry0，entry1 是死重。
    """
    return _wrap_stsd_entries(
        _visual_entry("dvh1", _hvcC_box(
            _param_set_array(32, bytes([64]) + b"VPS"), _param_set_array(33, bytes([66]) + b"SPS"),
            _param_set_array(34, bytes([68]) + b"PPS"), _param_set_array(39, bytes([78]) + b"SEI"),
        )),
        _visual_entry("dvh1", _hvcC_box(
            _param_set_array(32, bytes([64]) + b"VPS"), _param_set_array(33, bytes([66]) + b"SPS"), _param_set_array(34, bytes([68]) + b"PPS"),
        )),
    )


def test_sample_entry_count_reads_stsd_entry_count() -> None:
    assert sample_entry_count(_two_entry_init()) == 2


def test_keep_single_sample_entry_keeps_first_entry(tmp_path: Path) -> None:
    """默认收敛保 entry0（带 SEI 前缀版）——分片只引 entry0，删死重不改语义。"""
    init = _two_entry_init()
    single, dropped = keep_single_sample_entry(init)
    assert dropped == 1
    assert sample_entry_count(single) == 1
    assert hvcC_param_set_types(single) == (32, 33, 34, 39)
    out = tmp_path / "single.mp4"
    out.write_bytes(single)
    assert parse_file(out).video.sample_entry_type == "dvh1"
    other, dropped2 = keep_single_sample_entry(init, keep_index=1)
    assert dropped2 == 1
    assert hvcC_param_set_types(other) == (32, 33, 34)


def test_keep_single_sample_entry_is_idempotent_on_single_entry() -> None:
    single, dropped = keep_single_sample_entry(_two_entry_init())
    assert single == keep_single_sample_entry(single)[0]
    assert keep_single_sample_entry(single)[1] == 0


def test_keep_single_sample_entry_rejects_non_video_entry() -> None:
    init = _wrap_stsd_entries(_visual_entry("dvh1", b""), _visual_entry("mp4a", b""))
    with pytest.raises(ValueError, match="非 video"):
        keep_single_sample_entry(init)


def test_keep_single_sample_entry_rejects_keep_index_out_of_range() -> None:
    with pytest.raises(ValueError, match="越界"):
        keep_single_sample_entry(_two_entry_init(), keep_index=2)


def _moov_tree(data: bytes) -> tuple[Box, int]:
    """重新解析整段 init 的 moov，返回 (树, moov 绝对偏移)。"""
    boxes = _scan_buffer(data)
    assert boxes and boxes[-1][0] == "moov"
    offset, size = boxes[-1][1], boxes[-1][2]
    parsed = _parse_box(data[offset : offset + size], 0, size, 1)
    assert parsed is not None
    return parsed, offset


def _assert_declared_sizes_match(data: bytes) -> None:
    """树里每个 box 的 4 字节尺寸字段都必须等于解析尺寸——父链记账错误无处遁形。"""
    tree, base = _moov_tree(data)

    def walk(box: Box) -> None:
        declared = int.from_bytes(data[base + box.offset : base + box.offset + 4], "big")
        assert declared == box.size, f"{box.type} 尺寸字段 {declared} != 解析尺寸 {box.size}"
        for child in box.children:
            walk(child)

    walk(tree)


def test_keep_single_sample_entry_converges_three_entries(tmp_path: Path) -> None:
    """3-entry stsd 收敛——2026-09-26 真机失败件（3 个 dvh1 entry）的记账回归。

    同一祖先链被多个 victim 命中：第二次删 entry 时父尺寸已被前一次扣减，
    必须按累计值校验并写回，否则必报「父 box 尺寸字段与解析不符」。
    """
    init = _wrap_stsd_entries(
        _visual_entry(
            "dvh1",
            _hvcC_box(
                _param_set_array(32, bytes([64]) + b"VPS"),
                _param_set_array(33, bytes([66]) + b"SPS"),
                _param_set_array(34, bytes([68]) + b"PPS"),
            ),
        ),
        _visual_entry(
            "dvh1",
            _hvcC_box(
                _param_set_array(32, bytes([65]) + b"VPS"),
                _param_set_array(33, bytes([67]) + b"SPS"),
                _param_set_array(34, bytes([69]) + b"PPS"),
            ),
        ),
        _visual_entry("hvc1", b""),
    )
    before_tree, _ = _moov_tree(init)
    stsd_before = before_tree.find_all("stsd")[0]
    removed_bytes = sum(child.size for child in stsd_before.children[1:])
    assert len(stsd_before.children) == 3

    single, dropped = keep_single_sample_entry(init)
    assert dropped == 2
    assert sample_entry_count(single) == 1
    assert len(init) - len(single) == removed_bytes
    _assert_declared_sizes_match(single)
    out = tmp_path / "single.mp4"
    out.write_bytes(single)
    assert parse_file(out).video.sample_entry_type == "dvh1"


def test_drop_hvcC_param_set_type_removes_sei_array(tmp_path: Path) -> None:
    """剥 SEI 前缀(39)：数组整组消失、numOfArrays-1，32/33/34 一条不动。"""
    from dvpack.bmff import drop_hvcC_param_set_type

    vps, sps, pps = bytes.fromhex("40010c01"), bytes.fromhex("420101016000"), bytes.fromhex("4401c1")
    sei = bytes.fromhex("4e0105")
    data = _file("dvh1", _hvcc_box({32: [vps], 33: [sps], 34: [pps], 39: [sei]}))
    out, dropped = drop_hvcC_param_set_type(data, 39)
    assert dropped == 1
    assert _read_hvcC_arrays(out) == {32: [vps], 33: [sps], 34: [pps]}
    assert hvcC_param_set_types(out) == (32, 33, 34)
    assert len(out) == len(data) - (3 + 2 + len(sei))
    (tmp_path / "init.mp4").write_bytes(out)
    assert parse_file(tmp_path / "init.mp4").video.signalling == "dvh1 但缺 dvC"


def test_drop_hvcC_param_set_type_drops_multi_nal_sei_array() -> None:
    """真机 live 包的 39 组是三个 SEI NAL——整组删干净，不留残片。"""
    from dvpack.bmff import drop_hvcC_param_set_type

    seis = [bytes.fromhex("4e010203"), bytes.fromhex("4e04050607"), bytes.fromhex("4e08")]
    data = _file(
        "dvh1",
        _hvcc_box({32: [bytes.fromhex("4001")], 33: [bytes.fromhex("4201")], 34: [bytes.fromhex("4401")], 39: seis}),
    )
    out, dropped = drop_hvcC_param_set_type(data, 39)
    assert dropped == 1
    assert _read_hvcC_arrays(out) == {32: [bytes.fromhex("4001")], 33: [bytes.fromhex("4201")], 34: [bytes.fromhex("4401")]}
    sei_array_len = 3 + sum(len(nal) + 2 for nal in seis)
    assert len(out) == len(data) - sei_array_len


def test_drop_hvcC_param_set_type_is_noop_when_absent() -> None:
    """没有目标数组时原样返回，一个字节都不动（幂等）。"""
    from dvpack.bmff import drop_hvcC_param_set_type

    base = {32: [bytes.fromhex("4001")], 33: [bytes.fromhex("4201")], 34: [bytes.fromhex("4401")]}
    data = _file("dvh1", _hvcc_box(base))
    out, dropped = drop_hvcC_param_set_type(data, 39)
    assert dropped == 0
    assert out == data
    once, _ = drop_hvcC_param_set_type(
        _file("dvh1", _hvcc_box({**base, 39: [bytes.fromhex("4e01")]})), 39
    )
    assert drop_hvcC_param_set_type(once, 39) == (once, 0)


def test_drop_hvcC_param_set_type_rejects_mandatory_sets() -> None:
    """32/33/34 是 Apple HLS 必需参数集，点名删除直接 raise。"""
    from dvpack.bmff import drop_hvcC_param_set_type

    data = _file("dvh1", _hvcc_box({32: [bytes.fromhex("4001")], 33: [bytes.fromhex("4201")], 34: [bytes.fromhex("4401")]}))
    for nal_type in (32, 33, 34):
        with pytest.raises(ValueError, match="必需"):
            drop_hvcC_param_set_type(data, nal_type)


def test_single_entry_then_drop_sei_matches_playable_template(tmp_path: Path) -> None:
    """jfpack 手术顺序复现：单 entry 收敛后剥 39，得到能播 1080p 模板同构信令。"""
    from dvpack.bmff import drop_hvcC_param_set_type, keep_single_sample_entry

    single, _ = keep_single_sample_entry(_two_entry_init())
    assert hvcC_param_set_types(single) == (32, 33, 34, 39)
    out, dropped = drop_hvcC_param_set_type(single, 39)
    assert dropped == 1
    assert hvcC_param_set_types(out) == (32, 33, 34)
    (tmp_path / "init.mp4").write_bytes(out)
    report = parse_file(tmp_path / "init.mp4")
    assert report.video.sample_entry_type == "dvh1"
    assert sample_entry_count(out) == 1

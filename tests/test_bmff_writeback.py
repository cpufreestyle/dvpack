"""dvpack.bmff 的三个写回函数：dv_level 夹档、compressor 清零、原子写盘。

这三个都是「改 init 段字节」的操作，错一个位不会报错，只会让 AVPlayer 拉完
200 后静默停播——比失败更难查。2026-10-02 实测口径：Jellyfin 有 4K 片源在
dvcC 里签 level 9，原样带进 init 后 tvOS 不按 DV 源处理（徽标不亮、颜色也
不对）；点亮版 4K 包 dv_level=6、Apple 官方 1080p 参考流 dv_level=3。

tests/test_bmff.py 覆盖解析与参数集去重；这里补写回面。
"""

from __future__ import annotations

import struct

import pytest

from dvpack.bmff import (
    atomic_write,
    clamp_dvcC_level,
    clear_compressor_name,
    expected_dv_level,
    locate_dv_boxes,
)


def _box(fourcc: bytes, payload: bytes) -> bytes:
    return struct.pack(">I", 8 + len(payload)) + fourcc + payload


#: VisualSampleEntry 的 fixed fields 长度（bmff._VISUAL_ENTRY_EXTRA）：
#: 配置 box（dvcC/dvvC）跟在其后，同属 sample entry 的子 box。
_VISUAL_EXTRA = 78
#: compressorname 定长 32 字节在 VisualSampleEntry 里的起点。
_COMPRESSOR_AT = 50


def _sample_entry(compressor: str | None = None, dv_level: int | None = None) -> bytes:
    """造一个 dvh1 VisualSampleEntry：fixed fields 加可选的 dvcC 子 box。"""
    children = b""
    if dv_level is not None:
        children += _box(b"dvcC", bytes([5, 0, 0, dv_level]))
    entry = bytearray(8 + _VISUAL_EXTRA + len(children))
    entry[0:4] = struct.pack(">I", len(entry))
    entry[4:8] = b"dvh1"
    if compressor:
        raw = compressor.encode("ascii")
        entry[_COMPRESSOR_AT] = len(raw)
        entry[_COMPRESSOR_AT + 1 : _COMPRESSOR_AT + 1 + len(raw)] = raw
    entry[82:84] = b"\x00\x18"  # depth
    entry[84:86] = b"\xff\xff"  # pre_defined
    entry[8 + _VISUAL_EXTRA :] = children
    return bytes(entry)


def _wrap_stsd(entry: bytes) -> bytes:
    """把 sample entry 包进 stsd/stbl/minf/mdia/trak/moov。"""
    stsd = _box(b"stsd", b"\x00\x00\x00\x00" + b"\x00\x00\x00\x01" + entry)
    stbl = _box(b"stbl", stsd)
    minf = _box(b"minf", stbl)
    mdia = _box(b"mdia", minf)
    trak = _box(b"trak", mdia)
    return _box(b"moov", trak)


def _init_with_dvcC(level_byte: int) -> bytes:
    """level_byte 是 dvcC payload[3]：高 5 位 dv_level、低 3 位 el/bl 标志。"""
    return _wrap_stsd(_sample_entry(dv_level=level_byte))



class TestExpectedDvLevel:
    """分辨率到 level 档位：签高了 tvOS 直接不认 DV 源。"""

    @pytest.mark.parametrize(
        ("size", "expected"),
        [
            ((640, 480), 3),
            ((1280, 720), 3),
            ((1920, 1080), 3),      # Apple 官方 1080p P5 参考流
            ((2560, 1440), 6),
            ((3840, 2160), 6),      # 点亮版 4K 包
            ((7680, 4320), 9),
            ((15360, 8640), 9),
        ],
    )
    def test_resolution_maps_to_tier(self, size, expected):
        width, height = size
        assert expected_dv_level(width, height) == expected

    def test_boundaries_are_inclusive(self):
        """1080 与 2160 是闭区间边界：1080 归 3、1081 归 6。"""
        assert expected_dv_level(1920, 1080) == 3
        assert expected_dv_level(1920, 1081) == 6
        assert expected_dv_level(1920, 2160) == 6
        assert expected_dv_level(1920, 2161) == 9

    def test_width_is_ignored(self):
        """档位只由高度决定：超宽屏（32:9）不能因此升档。"""
        assert expected_dv_level(5120, 1440) == 6
        assert expected_dv_level(1, 2160) == 6


class TestClampDvcCLevel:
    """只降不升：源签得比分辨率档位保守时不动，不把能力签高。"""

    def test_reduces_level_above_tier(self):
        data = _init_with_dvcC(9 << 3)
        out, original = clamp_dvcC_level(data, 6)
        assert original == 9
        assert locate_dv_boxes(out)[0].payload[3] >> 3 == 6

    @pytest.mark.parametrize("level", [3, 6])
    def test_keeps_level_at_or_below_tier(self, level):
        """没动就不能改写一个字节。"""
        data = _init_with_dvcC(level << 3)
        out, original = clamp_dvcC_level(data, 6)
        assert original is None
        assert out == data

    def test_preserves_low_bits(self):
        """el/bl 等存在位在低 3 位，夹档不许顺手抹掉。"""
        data = _init_with_dvcC((9 << 3) | 0b101)
        out, _ = clamp_dvcC_level(data, 6)
        payload = locate_dv_boxes(out)[0].payload
        assert payload[3] >> 3 == 6
        assert payload[3] & 0x07 == 0b101

    def test_preserves_profile_byte(self):
        """只动 payload[1]，profile 位域（payload[0]）不许被碰。"""
        data = _init_with_dvcC(9 << 3)
        out, _ = clamp_dvcC_level(data, 6)
        assert locate_dv_boxes(out)[0].payload[0] == 5

    def test_only_one_byte_differs(self):
        data = _init_with_dvcC(9 << 3)
        out, _ = clamp_dvcC_level(data, 6)
        assert len(out) == len(data)
        differing = [i for i in range(len(data)) if data[i] != out[i]]
        assert len(differing) == 1, differing

    def test_no_dvcC_is_a_noop(self):
        """没有配置 box（非 DV init）不该抛，原样返回。"""
        data = _wrap_stsd(_sample_entry())
        out, original = clamp_dvcC_level(data, 6)
        assert original is None and out == data

    def test_ignores_dvcC_shaped_bytes_outside_moov(self):
        """只在 moov 内定位：顶层假装成 dvcC 的字节不许被改。"""
        fake = _box(b"dvcC", bytes([5, 9 << 3]))
        out, original = clamp_dvcC_level(fake, 6)
        assert original is None and out == fake

    def test_4k_source_signed_9_is_the_real_world_case(self):
        """2026-10-02 现场：Jellyfin 4K 片源签 9，tvOS 不按 DV 源处理。"""
        data = _init_with_dvcC(9 << 3)
        out, original = clamp_dvcC_level(data, expected_dv_level(3840, 2160))
        assert original == 9
        assert locate_dv_boxes(out)[0].payload[3] >> 3 == 6



class TestClearCompressorName:
    """清空 sample entry 的 compressorname（点亮版与 Apple 参考流都是空）。"""

    def test_clears_a_printable_name(self):
        data = _wrap_stsd(_sample_entry(compressor="DOVI Coding"))
        out, original = clear_compressor_name(data, "dvh1")
        assert original == "DOVI Coding"
        entry_start = out.index(b"dvh1") - 4
        assert out[entry_start + _COMPRESSOR_AT] == 0
        cleared = out[entry_start + _COMPRESSOR_AT + 1 : entry_start + _COMPRESSOR_AT + 32]
        assert cleared == b"\x00" * 31
    def test_empty_stays_empty(self):
        data = _wrap_stsd(_sample_entry())
        out, original = clear_compressor_name(data, "dvh1")
        assert original is None
        assert out == data

    def test_depth_and_predefined_are_untouched(self):
        """清零只覆盖 32 字节字段区，其后的 depth/pre_defined 保持原样。"""
        data = _wrap_stsd(_sample_entry(compressor="DOVI Coding"))
        out, _ = clear_compressor_name(data, "dvh1")
        trailer = out.index(b"\x00\x18\xff\xff")
        assert out[trailer : trailer + 2] == b"\x00\x18"
        assert out[trailer + 2 : trailer + 4] == b"\xff\xff"

    def test_rejects_implausible_length_byte(self):
        """长度字节越界说明定位错了，宁可 raise 也不静默写坏。"""
        entry = bytearray(_sample_entry())
        entry[_COMPRESSOR_AT] = 99
        with pytest.raises(ValueError, match="长度字节可疑"):
            clear_compressor_name(_wrap_stsd(bytes(entry)), "dvh1")

    def test_rejects_unprintable_name(self):
        entry = bytearray(_sample_entry())
        entry[_COMPRESSOR_AT] = 4
        entry[_COMPRESSOR_AT + 1 : _COMPRESSOR_AT + 5] = b"\x01\x02\x03\x04"
        with pytest.raises(ValueError, match="不可打印"):
            clear_compressor_name(_wrap_stsd(bytes(entry)), "dvh1")

    def test_rejects_wrong_trailer(self):
        """字段后不是 depth/pre_defined 就是定位可疑，不许猜。"""
        entry = bytearray(_sample_entry(compressor="DOVI Coding"))
        entry[82:84] = b"\x00\x00"
        with pytest.raises(ValueError, match="depth"):
            clear_compressor_name(_wrap_stsd(bytes(entry)), "dvh1")

    def test_no_moov_is_a_noop(self):
        """顶层不是 moov 收尾的布局：原样返回，不猜。"""
        data = _box(b"ftyp", b"isom") + _box(b"free", b"")
        out, original = clear_compressor_name(data, "dvh1")
        assert original is None and out == data

    def test_other_fourcc_is_left_alone(self):
        """只清指定 sample entry：hvc1 的不该被 dvh1 的清理逻辑碰到。"""
        data = _wrap_stsd(_sample_entry(compressor="DOVI Coding"))
        out, original = clear_compressor_name(data, "hvc1")
        assert original is None and out == data

    def test_keeps_dvcC_intact(self):
        """清 compressor 不许顺手动到同一 entry 里的配置 box。"""
        data = _wrap_stsd(_sample_entry(compressor="DOVI Coding", dv_level=9 << 3))
        out, _ = clear_compressor_name(data, "dvh1")
        assert locate_dv_boxes(out)[0].payload[3] == 9 << 3

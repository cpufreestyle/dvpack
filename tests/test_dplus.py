
"""dplus 信令规范化回归：jfpack 链路 hvcC 的 prefix SEI 数组。

v7 之前的 bug：patch_init_signalling 只认 VPS/SPS/PPS 三个参数集数组头，
jfpack 产物（源 mkv CodecPrivate 直出）的 hvcC 多带一个 prefix SEI 数组
（数组头 0x27），直接 raise，apply_dplus 被 signature 校验安全跳过，
tier/non_packed 停在 0——电视上 DV 徽标不亮、颜色发灰。flat 链路没有
SEI 数组所以一直没暴露。dplus 长期零测试覆盖，这就是 bug 潜伏的原因。
fixture = Jellyfin 片源 534f5cb2ba7f9d500f9f2154e8cff079 的 init 段
（jftest2，未规范化版本）。
"""

from __future__ import annotations

import struct
from pathlib import Path

import pytest
from dvpack.dplus import (
    DplusSignalError,
    DplusStats,
    _clear_bit,
    _hvcC_bit,
    patch_init_signalling,
    signalling_signature_ok,
)

FIXTURE = Path(__file__).parent / "fixtures" / "jf_init_sei.mp4"


@pytest.fixture(scope="module")
def jf_init() -> bytes:
    return FIXTURE.read_bytes()


def _walk_arrays(data: bytes):
    """按 patch_init_signalling 的口径走一遍 hvcC 数组，返回
    (数组头位置, 数组头, [(nal 起, nal 止, nal 类型)])。
    """
    base = _hvcC_bit(data, 0) // 8
    narrays = data[base + 22]
    pos = base + 23
    out = []
    for _ in range(narrays):
        header_pos = pos
        header = data[pos]
        count = struct.unpack_from(">H", data, pos + 1)[0]
        pos += 3
        nals = []
        for _k in range(count):
            length = struct.unpack_from(">H", data, pos)[0]
            nal_start = pos + 2
            nal_end = nal_start + length
            nals.append((nal_start, nal_end, data[nal_start + 2] & 0x3F))
            pos = nal_end
        out.append((header_pos, header, nals))
    return out


def test_jf_init_signature_accepted(jf_init):
    # 修复前这里返回 False：SEI 数组头 0x27 不在 _ARRAY_TYPES 里被 raise
    assert signalling_signature_ok(jf_init)


def test_jf_init_patch_sets_tier_and_non_packed_cleared(jf_init):
    """2026-10-02 点亮口径：tier / non_packed 一律清 0（与 Apple 官方 P5
    参考流同构；实测「两位置 1」的 variant 真机不亮，见 _clear_bit 注释）。
    """
    patched, stats = patch_init_signalling(jf_init)
    base = _hvcC_bit(jf_init, 0) // 8
    assert patched[base + 1] == 0x02  # hvcC general_tier_flag 清 0
    assert patched[base + 6] == 0x90  # hvcC non_packed_constraint 清 0
    assert len(patched) == len(jf_init)  # 只清位，不动长度
    assert stats.arrays == 3  # VPS/SPS/PPS 三个数组头补 complete 位
    assert stats.vui == 3  # hvcC/VPS/SPS 三处 VUI 按点亮口径写入
    assert stats.param_sets == 0  # hvcC PTL 位全清 0，无置位计数


def test_jf_init_sei_array_bytes_untouched(jf_init):
    patched, _ = patch_init_signalling(jf_init)
    for header_pos, header, nals in _walk_arrays(jf_init):
        if header & 0x3F != 39:
            continue
        for start, end, _nal_type in nals:
            assert patched[start:end] == jf_init[start:end]
        assert patched[header_pos] == jf_init[header_pos]


def test_patch_is_idempotent(jf_init):
    once, _ = patch_init_signalling(jf_init)
    twice, stats = patch_init_signalling(once)
    assert twice == once
    assert stats == DplusStats()


def test_version_guard_still_fires(jf_init):
    base = _hvcC_bit(jf_init, 0) // 8
    broken = bytearray(jf_init)
    broken[base] = 0  # hvcC configurationVersion != 1
    with pytest.raises(DplusSignalError):
        patch_init_signalling(bytes(broken))
    assert not signalling_signature_ok(bytes(broken))


def test_unknown_array_header_still_raises(jf_init):
    # 错位哨兵不许松：SEI 之外的一切未知数组头仍然报错
    base = _hvcC_bit(jf_init, 0) // 8
    broken = bytearray(jf_init)
    broken[base + 23] = 0x1B  # v7 误报的形态：未知 NAL 类型 27
    with pytest.raises(DplusSignalError):
        patch_init_signalling(bytes(broken))
    assert not signalling_signature_ok(bytes(broken))


def test_tier_guard_only_applies_to_ptl_bytes():
    # tier 位前两位是 profile_space，非零说明定位可疑，拒绝盲改
    # bit2=1 且 profile_space 非零（0xe0=111xxxxx → 3）才会触发卫兵；
    # bi2 本身是 0 时 _clear_bit 先返回 False，走不到卫兵
    buf = bytearray(b"\xe0\x00")
    with pytest.raises(DplusSignalError):
        _clear_bit(buf, 2, "hvcC general_tier_flag", tier=True)
    # 卫兵只挂 tier=True 调用点：non_packed 位前两位是 progressive/
    # interlaced 源标志，源片本来就可以是 1（jfpack 产物实测 0x90）
    buf2 = bytearray(b"\x90\x00")
    assert _clear_bit(buf2, 0, "non packed") is True
    # 清位语义：清完再清是 no-op，返回 False
    assert _clear_bit(buf2, 0, "non packed") is False

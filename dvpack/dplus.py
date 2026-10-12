from __future__ import annotations

import re
import struct
from dataclasses import dataclass, field
from pathlib import Path

from .bmff import atomic_write
from .remux import (
    DV_LEAVE_COLORIMETRY_UNSPECIFIED,
    FFMPEG,
    RemuxOutput,
    _run,
    require,
)

NAL_TYPE_VPS = 32
NAL_TYPE_SPS = 33
NAL_TYPE_PREFIX_SEI = 39

_NAL_HEADER_BITS = 16
_SPS_PTL_BIT = 8
_VPS_PTL_BIT = 32
_PTL_TIER_BIT = 2
_PTL_NON_PACKED_BIT = 42
BIT_TIER = 0x20
ARRAY_COMPLETE_BIT = 0x80
_ARRAY_TYPES = (0x20, 0x21, 0x22)
_MOOV_RANGE = (b"moov",)

# SPS/VUI 颜色信令的位偏移（相对 SPS RBSP 起点）。这三个偏移只对本片源的 SPS
# 结构成立（ffmpeg trace_headers 逐位实测），换片源必须重新量，不能照抄。
# range 位随源（remux.source_full_range 推导）：ffmpeg 侧 hevc_metadata 按
# 源口径重写，这里只做二次确认/兜底。2026-09-26 像素级实测（signalstats
# YMIN=0/YMAX=1016 + 容器 color_range=pc + RPU bl_video_full_range_flag=1）
# 证明 Jellyfin 测试片是全色域源，三处口径必须一致地签全色域；旧代码一律
# 签 limited 是真机「颜色不对 + 徽标不亮」的根因。06:48 点亮那次是 P7 源
# （BL 本就 limited），limited 结论不能外推到原生全色域源。
_VUI_FULL_RANGE_BIT = 653
_VUI_PRIMARIES_BIT0 = 655
_VUI_TRANSFER_BIT0 = 663
_VUI_MATRIX_BIT0 = 671
_VUI_BITS = 8
_BT2020_PRIMARIES = 9
_PQ_TRANSFER = 16
_BT2020_MATRIX = 9


class DplusSignalError(RuntimeError):
    pass


@dataclass
class DplusStats:
    param_sets: int = 0
    arrays: int = 0
    vui: int = 0

    def __add__(self, other: DplusStats) -> DplusStats:
        return DplusStats(
            self.param_sets + other.param_sets,
            self.arrays + other.arrays,
            self.vui + other.vui,
        )


@dataclass(frozen=True)
class SignallingReport:
    ok: bool
    observed: dict[str, frozenset[int]] = field(default_factory=dict)
    problems: tuple[str, ...] = ()


def _nal_type(data, nal_start: int) -> int:
    return ((data[nal_start] << 8) | data[nal_start + 1]) >> 9 & 0x3F


def _clear_bit(
    data: bytearray, bit: int, what: str, *, tier: bool = False
) -> bool:
    """EBSP 裸字节清位（hvcC 的 PTL byte，不涉 00 00 03 转义）。

    2026-10-02 由置位改清位：两个点亮版 init/分片实测 tier=0、non_packed=0
    （与 Apple 官方 P5 参考流同构），把两位置 1 的那一臂（variant D）用户
    实测「D 没亮」，本模块归一到点亮口径。tier=True 时该 byte 前两位是
    profile_space，非零说明定位可疑，拒绝盲改；non_packed 位（constraint
    byte）前两位是 progressive/interlaced 源标志，源片本来就可以是 1
    （实测本片源 0x90），与 PTL 对齐无关，不能拿来当对齐哨兵——旧版拿它
    当哨兵，jfpack 产物直接被误拒。
    """
    byte, off = divmod(bit, 8)
    cur = data[byte]
    if not (cur >> (7 - off)) & 1:
        return False
    if tier and cur >> 6:
        raise DplusSignalError(
            f"{what}：profile_space 非零，bit 定位可疑，拒绝盲改"
        )
    data[byte] = cur & ~(0x80 >> off) & 0xFF
    return True


def _clear_ptl_flag(rbsp: bytearray, bit: int, *, tier: bool = False) -> bool:
    """RBSP 空间内清 PTL 标志位；已是 0 就跳过。

    口径同 _clear_bit：2026-10-02 起归一到 tier=0 / non_packed=0 的点亮
    口径。tier 位前两位是 profile_space，非零拒绝盲改（对齐哨兵）；
    non_packed 前两位是 progressive/interlaced 源标志，源片为 1 也正常，
    不当哨兵，对齐由先跑的 tier 哨兵背书。
    """
    if not _read_bits(rbsp, bit, 1):
        return False
    if tier and _read_bits(rbsp, bit - 2, 2):
        raise DplusSignalError("profile_space 非零，bit 定位可疑，拒绝盲改")
    _write_bits(rbsp, bit, 1, 0)
    return True


def _patch_param_set(
    data: bytearray, nal_start: int, nal_end: int, stats: DplusStats
) -> None:
    """VPS/SPS 的 tier / non_packed 约束位清 0。

   必须走 EBSP->RBSP：本片源 SPS 的 non_packed 位位于 00 00 03 转义字节之后，
   直接在 EBSP 上数字节会写到转义字节本身，把码流写坏。

    _SPS/_VPS_PTL_BIT 等偏移是相对 NAL 头之后的 RBSP 载荷量的，rbsp 数组含 2
    字节 NAL 头，定位时要加回 _NAL_HEADER_BITS。
    """
    nal = bytes(data[nal_start:nal_end])
    rbsp, idx = _ebsp_to_rbsp(nal)
    nal_type = _nal_type(data, nal_start)
    if nal_type not in (NAL_TYPE_VPS, NAL_TYPE_SPS):
        return
    header = _SPS_PTL_BIT if nal_type == NAL_TYPE_SPS else _VPS_PTL_BIT
    changed = 0
    if _clear_ptl_flag(
        rbsp, _NAL_HEADER_BITS + header + _PTL_TIER_BIT, tier=True
    ):
        changed += 1
    if _clear_ptl_flag(rbsp, _NAL_HEADER_BITS + header + _PTL_NON_PACKED_BIT):
        changed += 1
    if not changed:
        return
    for j, b in enumerate(rbsp):
        if b != nal[idx[j]]:
            data[nal_start + idx[j]] = b
            stats.param_sets += 1


def _ebsp_to_rbsp(nal: bytes) -> tuple[bytearray, list[int]]:
    """EBSP -> RBSP，并保留每个 RBSP 字节在 EBSP 里的原下标。

    VUI 的位偏移是按 RBSP 语义（去掉 emulation prevention 03）数出来的，改完
    必须按 idx 映射回 EBSP 原位，长度才不变、moof/trun 尺寸字段才继续有效。
    """
    rbsp = bytearray()
    idx: list[int] = []
    zero_run = 0
    for i, b in enumerate(nal):
        if zero_run >= 2 and b == 0x03:
            zero_run = 0
            continue
        rbsp.append(b)
        idx.append(i)
        zero_run = zero_run + 1 if b == 0 else 0
    return rbsp, idx


def _read_bits(buf: bytearray, start: int, nbits: int) -> int:
    cur = 0
    for k in range(nbits):
        cur = (cur << 1) | ((buf[(start + k) // 8] >> (7 - (start + k) % 8)) & 1)
    return cur


def _write_bits(buf: bytearray, start: int, nbits: int, value: int) -> None:
    for k in range(nbits):
        bit = (value >> (nbits - 1 - k)) & 1
        pos = start + k
        byte, off = pos // 8, pos % 8
        if bit:
            buf[byte] |= 0x80 >> off
        else:
            buf[byte] &= ~(0x80 >> off)


def _set_vui_bits(rbsp: bytearray, start: int, value: int, expect: int) -> bool:
    cur = _read_bits(rbsp, start, _VUI_BITS)
    if cur == value:
        return False
    if cur != expect:
        raise DplusSignalError(
            f"VUI bits@{start}: expect {expect}, got {cur}，拒绝盲改"
        )
    _write_bits(rbsp, start, _VUI_BITS, value)
    return True


def _patch_sps_vui(
    data: bytearray,
    nal_start: int,
    nal_end: int,
    stats: DplusStats,
    full_range: int | None = None,
) -> None:
    """SPS/VUI 颜色信令：range 位对齐源口径，BT.709(2,2,2) -> BT.2020/PQ。

    full_range 由调用方从源推导（remux.source_full_range）：容器、SPS VUI、
    RPU 三处口径必须一致。None = 只动颜色三件套，range 保持原值。

    2026-09-27 真机 A/B 实证推翻「照 Apple 参考留白」的结论：
      · 点亮  _jf/534f.old.signalled → SPS VUI 016a 12 20 12（显式 PQ）
      · 不亮  _jf/localtest001      → SPS VUI 016a 04 04 04（unspecified）
    ffprobe 侧同证：tv + bt2020nc/smpte2084/bt2020 对比 tv + 全 unknown。
    所以这里必须显式写三件套，range 位按 full_range 对齐源口径。
    """
    # 2026-09-27 真机 A/B 实证：显式 BT.2020/PQ 是点火条件，不是阻碍。
    #  点亮（_jf/534f.old.signalled）SPS VUI = 016a 12 20 12 → colour_description
    #  不亮（_jf/localtest001）        SPS VUI = 016a 04 04 04 → unspecified
    # ffprobe 侧同样：tv + bt2020nc/smpte2084/bt2020 对比 tv + 全 unknown。
    # 之前照 Apple 参考「留白」的假设被证伪，已回退。
    # 位偏移只对本片源 SPS 结构成立（实测校准），长度不匹配就整体跳过，
    # 宁可留原值也不改错字节。
    nal = bytes(data[nal_start:nal_end])
    rbsp, idx = _ebsp_to_rbsp(nal)
    if _VUI_FULL_RANGE_BIT >= len(rbsp) * 8 - 1:
        return
    changed = 0
    if full_range is not None and _read_bits(rbsp, _VUI_FULL_RANGE_BIT, 1) != full_range:
        _write_bits(rbsp, _VUI_FULL_RANGE_BIT, 1, full_range)
        changed = 1
    if _set_vui_bits(rbsp, _VUI_PRIMARIES_BIT0, _BT2020_PRIMARIES, 2):
        changed += 1
    if _set_vui_bits(rbsp, _VUI_TRANSFER_BIT0, _PQ_TRANSFER, 2):
        changed += 1
    if _set_vui_bits(rbsp, _VUI_MATRIX_BIT0, _BT2020_MATRIX, 2):
        changed += 1
    if not changed:
        return
    for j, b in enumerate(rbsp):
        if b != nal[idx[j]]:
            data[nal_start + idx[j]] = b
            stats.vui += 1


def _boxes(data: bytes, start: int, end: int):
    off = start
    while off + 8 <= end:
        size = struct.unpack_from(">I", data, off)[0]
        if size < 8 or off + size > end:
            return
        yield data[off + 4:off + 8], off, size
        off += size


def _hvcC_bit(data: bytes, offset: int) -> int:
    for typ, off, size in _boxes(data, 0, len(data)):
        if typ in _MOOV_RANGE:
            i = data.find(b"hvcC", off + 8, off + size)
            if i < 0:
                break
            box = struct.unpack_from(">I", data, i - 4)[0]
            if box < 8 or i - 4 + box > len(data):
                raise DplusSignalError("hvcC 尺寸越界")
            return (i + 4) * 8 + offset
    raise DplusSignalError("init 段 moov 里找不到 hvcC")


def _mdat_range(data: bytes) -> tuple[int, int]:
    j = data.find(b"mdat")
    while j >= 0:
        size = struct.unpack_from(">I", data, j - 4)[0]
        if 8 <= size <= len(data) and j - 4 + size <= len(data):
            return j + 4, j - 4 + size
        j = data.find(b"mdat", j + 1)
    raise DplusSignalError("分片里找不到 mdat")


def patch_init_signalling(
    data: bytes, full_range: int | None = None
) -> tuple[bytes, DplusStats]:
    buf = bytearray(data)
    stats = DplusStats()
    base = _hvcC_bit(buf, 0) // 8
    if buf[base] != 1:
        raise DplusSignalError("hvcC configurationVersion != 1，定位可疑")
    if _clear_bit(
        buf, base * 8 + 8 + _PTL_TIER_BIT, "hvcC general_tier_flag", tier=True
    ):
        stats.param_sets += 1
    if _clear_bit(
        buf,
        base * 8 + 8 + _PTL_NON_PACKED_BIT,
        "hvcC general_non_packed_constraint_flag",
    ):
        stats.param_sets += 1
    narrays = buf[base + 22]
    pos = base + 23
    for _ in range(narrays):
        # 数组头低 6 位是 NAL 类型。jfpack 链路（源 mkv CodecPrivate 直出）
        # 的 hvcC 会带 prefix SEI 数组（数组头 0x27），flat 链路只有 VPS/SPS/
        # PPS 三个。SEI 数组不属于信令规范化目标，整体跳过不碰；其余未知
        # 类型仍然报错——保留错位哨兵职责，不让换片源时静默写坏码流。
        nal_type = buf[pos] & 0x3F
        if nal_type == NAL_TYPE_PREFIX_SEI:
            sei_count = struct.unpack_from(">H", buf, pos + 1)[0]
            pos += 3
            for _k in range(sei_count):
                length = struct.unpack_from(">H", buf, pos)[0]
                pos += 2 + length
            continue
        if not buf[pos] & ARRAY_COMPLETE_BIT:
            if buf[pos] not in _ARRAY_TYPES:
                raise DplusSignalError(
                    f"hvcC 参数集数组头 {buf[pos]:02x} 不在预期 "
                    f"{tuple(hex(v) for v in _ARRAY_TYPES)} 里"
                )
            buf[pos] |= ARRAY_COMPLETE_BIT
            stats.arrays += 1
        count = struct.unpack_from(">H", buf, pos + 1)[0]
        pos += 3
        for _k in range(count):
            length = struct.unpack_from(">H", buf, pos)[0]
            nal_start = pos + 2
            pos = nal_start + length
            _patch_param_set(buf, nal_start, pos, stats)
            if _nal_type(buf, nal_start) == NAL_TYPE_SPS:
                _patch_sps_vui(buf, nal_start, pos, stats, full_range)
    return bytes(buf), stats


def patch_segment_signalling(
    data: bytes, full_range: int | None = None
) -> tuple[bytes, DplusStats]:
    buf = bytearray(data)
    stats = DplusStats()
    start, end = _mdat_range(bytes(buf))
    off = start
    while off + 4 <= end:
        length = struct.unpack_from(">I", buf, off)[0]
        if length < 3 or off + 4 + length > end:
            break
        nal_start = off + 4
        if _nal_type(buf, nal_start) in (NAL_TYPE_VPS, NAL_TYPE_SPS):
            _patch_param_set(buf, nal_start, off + 4 + length, stats)
            if _nal_type(buf, nal_start) == NAL_TYPE_SPS:
                _patch_sps_vui(buf, nal_start, off + 4 + length, stats, full_range)
        off += 4 + length
    return bytes(buf), stats


def signalling_signature_ok(data: bytes) -> bool:
    """init/分片的信令签名能否被本模块识别（dry-run，一个字节都不写）。

    jfpack 生产链路打包后先问这句：签名不认（换片源、SPS 结构不同）就原样
    跳过规范化，不让打签失败把整次打包炸穿成 502。range 位已由 ffmpeg 的
    hevc_metadata 在打包时统一重写，这里只兜 tier/non_packed 两位。
    """
    try:
        if data[4:8] == b"ftyp":
            patch_init_signalling(data)
        else:
            patch_segment_signalling(data)
    except (DplusSignalError, IndexError, struct.error):
        return False
    return True



def apply_dplus(output: RemuxOutput, full_range: int | None = None) -> DplusStats:
    init_bytes, stats = patch_init_signalling(output.init.read_bytes(), full_range)
    atomic_write(output.init, init_bytes)
    for segment in output.segments:
        data, seg_stats = patch_segment_signalling(segment.read_bytes(), full_range)
        atomic_write(segment, data)
        stats += seg_stats
    return stats

_TRACE_FIELD = re.compile(r"\]\s+(\d+)\s+(\S+)\s+(.*?)\s*=\s*(-?\d+)")

def expected_signalling(full_range: int = 0) -> tuple[tuple[str, int], ...]:
    """HLS DV 信令期望值；range 位随源（0=limited / 1=全色域）。

    2026-09-26 像素级实测：Jellyfin 测试片 YMIN=0/YMAX=1016 + 容器 pc +
    RPU bl_video_full_range_flag=1，是全色域源，三处必须一致地签全色域；
    06:48 点亮那次是 P7 源（BL 本就 limited），limited 不是永远正确的口径。
    颜色三件套与 tier/non_packed 与源范围无关，恒为 9/16/9；tier/non_packed
    恒为 0/0——2026-10-02 逐 box 对照：两个点亮版 init（live-A 1056B、Sept 27
    VUI 臂）都是 tier=0/non_packed=0，而把两位置 1 的 variant D 用户实测
    「D 没亮」。签 1/1 是被证伪的假设残留下来的默认值。
    """
    return (
        ("video_full_range_flag", 1 if full_range else 0),
        ("colour_primaries", 9),
        ("transfer_characteristics", 16),
        ("matrix_coefficients", 9),
        ("general_tier_flag", 0),
        ("general_non_packed_constraint_flag", 0),
    )


# 兼容常量（limited 口径）：旧引用方直接用。
EXPECTED_SIGNALLING = expected_signalling()


def trace_signalling(playlist: Path) -> dict[str, frozenset[int]]:
    # bsf 挂 ffmpeg 输出侧。ffprobe -bsf:v trace_headers 在本机 ffmpeg
    # 8.1.2（homebrew ffmpeg-full）上直接报 Option not found，虽然 -bsfs
    # 列表里有这个 bsf——输入侧拒收，导致这个校验函数静默返回空、看似
    # 「没解析到」。改用 ffmpeg -c copy 输出侧挂载，实测正常出字段。
    proc = _run(
        [
            str(require(FFMPEG)),
            "-v",
            "verbose",
            "-i",
            str(playlist),
            "-c",
            "copy",
            "-bsf:v",
            "trace_headers",
            "-f",
            "null",
            "-",
        ]
    )
    text = proc.stdout + proc.stderr
    found: dict[str, set[int]] = {name: set() for name, _ in EXPECTED_SIGNALLING}
    for line in text.splitlines():
        match = _TRACE_FIELD.search(line)
        if not match:
            continue
        name = match.group(2)
        if name in found:
            found[name].add(int(match.group(4)))
    return {name: frozenset(values) for name, values in found.items()}


def verify_signalling(playlist: Path, full_range: int = 0) -> SignallingReport:
    observed = trace_signalling(playlist)
    problems: list[str] = []
    for name, expected in expected_signalling(full_range):
        values = observed.get(name, frozenset())
        if not values:
            problems.append(f"没解析到 {name}")
        elif values != {expected}:
            problems.append(f"{name} = {sorted(values)}，期望 {{{expected}}}")
    return SignallingReport(not problems, observed, tuple(problems))

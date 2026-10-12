"""ISOBMFF 结构判据：sample entry fourcc、dvvC 是否存在、in-band RPU 普查。

职责划分是刻意的：dvvC 里的 profile / level / flags **不在这里解析**。ffprobe 能读它，
而且它是权威实现；自己解析 bit layout 只会引入"我以为我对"的风险。比对源与产出的
DoviRecord 请用 dvpack.probe——两边都走同一个 ffprobe，差异只可能来自打包链本身。

这个模块只回答 ffprobe 答不了的三件事：
  1. sample entry 的 fourcc 到底是 dvh1 还是被写成了 hvc1
  2. dvvC / dvcC  box 在不在，payload 是什么
  3. in-band RPU NAL 的数量，remux 前后有没有对得上
"""

from __future__ import annotations

import os
import struct
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from .dvprofile import (
    DV_BOX_NAMES,
    DV_SAMPLE_ENTRIES,
    MANDATORY_PARAM_SETS,
    NON_DV_SAMPLE_ENTRIES,
    RPU_NAL_PREFIX,
    RPU_NAL_TYPES,
)

VIDEO_SAMPLE_ENTRIES = DV_SAMPLE_ENTRIES | NON_DV_SAMPLE_ENTRIES

# 纯容器：box header 之后直接是子 box
_PURE_CONTAINERS = frozenset(
    {"moov", "trak", "mdia", "minf", "stbl", "edts", "dinf", "moof", "traf", "mvex", "moof"}
)
# full box 容器：header 之后先跳过 version(1)+flags(3)；stsd 还要再跳 entry_count(4)
_FULL_CONTAINERS = frozenset({"meta", "ipro", "sinf", "rinf", "stsd"})

# VisualSampleEntry 在子 box 之前有 78 字节固定字段，按 ISO/IEC 14496-12 逐段算：
#   SampleEntry   reserved(6) + data_reference_index(2)              = 8
#   Visual 前导   pre_defined(2) + reserved(2) + predefined[3](12)   = 16
#   Visual 主体   w(2)+h(2)+hres(4)+vres(4)+reserved(4)+frame_count(2)
#                 +compressorname(32)+depth(2)+pre_defined(2)        = 54
# 那 16 字节前导最容易被漏掉，而 dvh1 就是照 hvc1（VisualSampleEntry）派生的。
_VISUAL_ENTRY_EXTRA = 8 + 16 + 54

_RPU_NAL_TYPES = RPU_NAL_TYPES  # HEVC unregistered prefix / suffix NAL，DV 用它带 RPU
_RPU_NAL_PREFIX = RPU_NAL_PREFIX  # type 62 NAL header 后的 rpu_nal_prefix=25
_MANDATORY_PARAM_SETS = MANDATORY_PARAM_SETS  # VPS/SPS/PPS，见 dvprofile 注释


@dataclass(frozen=True)
class Box:
    type: str
    offset: int
    size: int
    children: tuple[Box, ...] = ()

    def find_all(self, name: str) -> list[Box]:
        found: list[Box] = []
        if self.type == name:
            found.append(self)
        for child in self.children:
            found.extend(child.find_all(name))
        return found

    def child(self, name: str) -> Box | None:
        return next((c for c in self.children if c.type == name), None)


@dataclass(frozen=True)
class TrackReport:
    track_id: int | None
    handler: str | None
    sample_entry_type: str | None
    sample_entry_children: tuple[str, ...]
    dvcc: bytes | None
    dv_box: str | None = None

    @property
    def is_video(self) -> bool:
        return self.handler == "vide"

    @property
    def declares_dolby_vision(self) -> bool:
        """Apple 要求 dvh1（基于 hvc1）。dvhe 基于 hev1，被 Apple 明确标注不推荐。"""
        return self.sample_entry_type in DV_SAMPLE_ENTRIES

    @property
    def has_dvvC(self) -> bool:
        """DV 配置 box 在不在。`dvvC` 与 `dvcC` 任一俗名都算在。"""
        return self.dvcc is not None

    @property
    def has_dvcC(self) -> bool:
        """只认规范名 `dvcC`。

        ffmpeg 写的是俗名 `dvvC`，ffprobe 两种都读——所以本地复读永远看不出差别，
        而 Apple 官方参考流（adv_dv_atmos）用的是 `dvcC`。转签见 dvpack.p5。
        """
        return self.dv_box == "dvcC"

    @property
    def signalling(self) -> str:
        """DV 信令成立 = sample entry 是 dvh1 且带配置 box。两个条件缺一不可。

        报的是**实际写下的那个名字**：`dvvC`（ffmpeg 俗名）与 `dvcC`（Apple 规范名）
        对 ffprobe 等价，对 tvOS 不等价，所以这里必须区分。
        """
        if self.sample_entry_type == "dvh1" and self.has_dvvC:
            return f"dvh1+{self.dv_box}"
        if self.sample_entry_type == "dvh1":
            return "dvh1 但缺 dvC"
        if self.has_dvvC:
            return f"有 {self.dv_box} 但 sample entry 是 {self.sample_entry_type}"
        return f"{self.sample_entry_type}，无 DV 信令"


@dataclass(frozen=True)
class BmffReport:
    path: Path
    tracks: tuple[TrackReport, ...]
    top_level: tuple[str, ...]
    problems: tuple[str, ...]

    @property
    def video(self) -> TrackReport | None:
        return next((t for t in self.tracks if t.is_video), None)

    @property
    def summary(self) -> str:
        video = self.video
        return "没有视频轨" if video is None else video.signalling


def _children_start(data: bytes, btype: str, body_at: int) -> int | None:
    """返回子 box 起始偏移；None 表示这是叶子 box。"""
    if btype in _PURE_CONTAINERS:
        return body_at
    if btype in _FULL_CONTAINERS:
        skip = 8 if btype == "stsd" else 4
        return body_at + skip
    if btype in VIDEO_SAMPLE_ENTRIES:
        return body_at + _VISUAL_ENTRY_EXTRA
    return None


def _parse_box(data: bytes, at: int, end: int, depth: int) -> Box | None:
    """解析一个 box。返回 None 表示这里已经不是一个合法 box（该停下了）。"""
    if end - at < 8:
        return None
    size = int.from_bytes(data[at : at + 4], "big")
    btype = data[at + 4 : at + 8].decode("latin-1")
    header = 8
    if size == 1:
        if end - at < 16:
            return None
        size = int.from_bytes(data[at + 8 : at + 16], "big")
        header = 16
    elif size == 0:
        size = end - at
    if size < header or at + size > end:
        return None

    children: tuple[Box, ...] = ()
    start = _children_start(data, btype, at + header)
    if start is not None and depth < 10:
        children = _parse_siblings(data, start, at + size, depth + 1)
    return Box(btype, at, size, children)


def _parse_siblings(data: bytes, at: int, end: int, depth: int) -> tuple[Box, ...]:
    boxes: list[Box] = []
    cursor = at
    while cursor < end:
        box = _parse_box(data, cursor, end, depth)
        if box is None:
            break
        boxes.append(box)
        cursor += box.size
    return tuple(boxes)


def _scan_top_level(path: Path) -> tuple[tuple[str, int, int], ...]:
    """只 seek + 读 box header，定位顶层 box。

    不能"读前 N MB"就完事：ffmpeg 默认把 moov 写在文件末尾，几十 GB 的产出用窗口
    去截就永远看不见 moov。
    """
    found: list[tuple[str, int, int]] = []
    with path.open("rb") as handle:
        handle.seek(0, 2)
        total = handle.tell()
        cursor = 0
        while cursor + 8 <= total:
            handle.seek(cursor)
            header = handle.read(16)
            if len(header) < 8:
                break
            size = int.from_bytes(header[:4], "big")
            btype = header[4:8].decode("latin-1")
            if size == 1:
                if len(header) < 16:
                    break
                size = int.from_bytes(header[8:16], "big")
            elif size == 0:
                size = total - cursor
            if size < 8 or cursor + size > total:
                break
            found.append((btype, cursor, size))
            cursor += size
    return tuple(found)


def parse_file(path: str | Path, *, max_moov_bytes: int = 32 * 1024 * 1024) -> BmffReport:
    """定位 moov 并只读它，产出结构报告。fragmented 媒体段没有 moov，看 init 段。"""
    path = Path(path)
    tops = _scan_top_level(path)
    if not tops:
        return BmffReport(path, (), (), ("文件头无法解析为 ISOBMFF",))

    names = tuple(name for name, _, _ in tops)
    moov = next(((off, size) for name, off, size in tops if name == "moov"), None)
    if moov is None:
        return BmffReport(path, (), names, ("没有 moov：媒体段属正常，请解析 init 段",))

    offset, size = moov
    problems: list[str] = []
    if size > max_moov_bytes:
        problems.append(f"moov 有 {size} 字节，超过 {max_moov_bytes} 上限，只报告存在不解析")
        return BmffReport(path, (), names, tuple(problems))

    with path.open("rb") as handle:
        handle.seek(offset)
        buf = handle.read(size)
    parsed = _parse_box(buf, 0, len(buf), 1)
    tracks = tuple(_report_track(trak, buf) for trak in (parsed.find_all("trak") if parsed else []))
    return BmffReport(path, tracks, names, tuple(problems))


# DOVI configuration record 的两种 box 名，集中定义在 dvprofile。


@dataclass(frozen=True)
class DvBox:
    """配置 box 在文件里的落点。payload_len 与 box 尺寸分离，写回时才好核对。"""

    fourcc: str
    file_offset: int
    payload_offset: int
    payload: bytes


def locate_dv_boxes(data: bytes) -> tuple[DvBox, ...]:
    """在 init 段的 moov 树里定位 DV 配置 box，返回可写回的落点。

    只在 moov 内搜索：init 段的 DV 配置只可能出现在 stsd 的 sample entry 里，
    而 stsd 必然在 moov 树下。分片里出现同名字节属于巧合，不能拿来改。
    偏移是**文件内绝对偏移**，调用方直接改 bytes 即可，无需再换算。
    """
    tops = _scan_buffer(data)
    moov = next(((off, size) for name, off, size in tops if name == "moov"), None)
    if moov is None:
        return ()
    moov_offset, moov_size = moov
    buf = data[moov_offset : moov_offset + moov_size]
    parsed = _parse_box(buf, 0, len(buf), 1)
    if parsed is None:
        return ()
    found: list[DvBox] = []
    for name in DV_BOX_NAMES:
        for box in parsed.find_all(name):
            payload_start = 8
            found.append(
                DvBox(
                    fourcc=name,
                    file_offset=moov_offset + box.offset,
                    payload_offset=moov_offset + box.offset + payload_start,
                    payload=buf[box.offset + payload_start : box.offset + box.size],
                )
            )
    return tuple(found)


def _scan_buffer(data: bytes) -> tuple[tuple[str, int, int], ...]:
    """顶层 box 扫描的 buffer 版；与 _scan_top_level 同一套规则，少读一次盘。"""
    found: list[tuple[str, int, int]] = []
    cursor = 0
    total = len(data)
    while cursor + 8 <= total:
        size = int.from_bytes(data[cursor : cursor + 4], "big")
        btype = data[cursor + 4 : cursor + 8].decode("latin-1")
        if size == 1:
            size = int.from_bytes(data[cursor + 8 : cursor + 16], "big")
        elif size == 0:
            size = total - cursor
        if size < 8 or cursor + size > total:
            break
        found.append((btype, cursor, size))
        cursor += size
    return tuple(found)


def _report_track(trak: Box, data: bytes) -> TrackReport:
    box, payload = _find_dv_payload(trak, data)
    return TrackReport(
        track_id=_track_id(trak, data),
        handler=_handler(trak, data),
        sample_entry_type=_sample_entry(trak, data)[0],
        sample_entry_children=_sample_entry(trak, data)[1],
        dvcc=payload,
        dv_box=box.type if box else None,
    )


def _find_dv_payload(trak: Box, data: bytes) -> tuple[Box | None, bytes | None]:
    """按 Apple 规范名优先的顺序找配置 box，返回 (box, payload)。

    返回 box 而不只返回 payload，是因为转签要按 box 的真实 offset 写回原文件。
    """
    for name in ("dvcC", "dvvC"):
        found = next(iter(trak.find_all(name)), None)
        if found is not None:
            return found, data[found.offset + 8 : found.offset + found.size]
    return None, None


def _track_id(trak: Box, data: bytes) -> int | None:
    mdhd = next(iter(trak.find_all("mdhd")), None)
    if mdhd is None:
        return None
    body = data[mdhd.offset + 8 : mdhd.offset + mdhd.size]
    if len(body) < 5:
        return None
    skip = 20 if body[0] == 1 else 12
    return int.from_bytes(body[skip : skip + 4], "big") if len(body) >= skip + 4 else None


def _handler(trak: Box, data: bytes) -> str | None:
    hdlr = next(iter(trak.find_all("hdlr")), None)
    if hdlr is None:
        return None
    # hdlr: header(8) + version/flags(4) + predefined(4) + handler_type(4)
    body = data[hdlr.offset + 16 : hdlr.offset + hdlr.size]
    return body[:4].decode("latin-1") if len(body) >= 4 else None


def _sample_entry(trak: Box, data: bytes) -> tuple[str | None, tuple[str, ...]]:
    stsd = next(iter(trak.find_all("stsd")), None)
    if stsd is None or not stsd.children:
        return None, ()
    entry = stsd.children[0]
    return entry.type, tuple(c.type for c in entry.children)



@dataclass(frozen=True)
class EsCensus:
    """HEVC Annex B 裸流的 NAL 普查。"""

    total: int
    by_type: Counter[int]
    dovi_rpu: int

    @property
    def has_inband_rpu(self) -> bool:
        return self.dovi_rpu > 0

    def __str__(self) -> str:
        types = " ".join(f"t{k}:{v}" for k, v in sorted(self.by_type.items()))
        return f"NAL 共 {self.total}，DOVI RPU {self.dovi_rpu}｜{types}"


def census_annexb(path: str | Path, *, read_limit: int | None = None) -> EsCensus:
    """统计裸流里各 NAL 类型数量，以及 DOVI RPU NAL 的数量。

    源 MKV 与产出 MP4 都先 dump 成 Annex B（`-c copy -f hevc`），于是两边可以在
    同一表示下对比——remux 只该改变长度前缀写法，不该改变 NAL 集合。
    """
    path = Path(path)
    with path.open("rb") as handle:
        data = handle.read(read_limit) if read_limit else handle.read()

    by_type: Counter[int] = Counter()
    rpu = 0
    starts = _annexb_starts(data)
    for start in starts:
        nal_type = (data[start] >> 1) & 0x3F
        by_type[nal_type] += 1
        # NAL header 占 2 字节，之后第一个 payload 字节应是 rpu_nal_prefix
        if nal_type in _RPU_NAL_TYPES and data[start + 2 : start + 3] == bytes([_RPU_NAL_PREFIX]):
            rpu += 1
    return EsCensus(total=len(starts), by_type=by_type, dovi_rpu=rpu)


def _annexb_starts(data: bytes) -> list[int]:
    """返回每个 NAL 的首字节偏移。

    只匹配 `00 00 01`：4 字节起始码 `00 00 00 01` 会在第二个 0 处被这个模式命中，
    偏移同样落在 NAL 首字节上，所以不必单独处理。
    """
    starts: list[int] = []
    i = 0
    n = len(data)
    while i + 3 <= n:
        if data[i] == 0 and data[i + 1] == 0 and data[i + 2] == 1:
            at = i + 3
            if at < n:
                starts.append(at)
            i = at
        else:
            i += 1
    return starts


def insert_box_after(data: bytes, sibling_type: str, new_type: str, payload: bytes) -> bytes:
    """在 init 段的 moov 里，把一个新 box 插到指定兄弟 box 之后，并修正父链尺寸。

    fMP4 init 段的 moov 是最后一个顶层 box、内部 stbl/stsd 里没有指向 mdat 的
    chunk offset（ffmpeg 对 fragmented 输出就是这种布局），所以插入只需要把
    dvh1/stsd/stbl/minf/mdia/trak/moov 的尺寸逐个加上新 box 的长度，
    不需要重算任何偏移。若不成立，函数宁可报错也不要静默产出错文件。
    """
    from struct import pack

    boxes = _scan_buffer(data)
    if not boxes or boxes[-1][0] != "moov":
        raise ValueError("只支持 moov 收尾的 init 段；本文件的顶层 box 不是这种布局")
    moov_offset, moov_size = boxes[-1][1], boxes[-1][2]
    buf = bytearray(data[moov_offset : moov_offset + moov_size])
    parsed = _parse_box(bytes(buf), 0, len(buf), 1)
    if parsed is None:
        raise ValueError("moov 解析失败")
    target = _find_box(parsed, sibling_type)
    if target is None:
        raise ValueError(f"init 段里找不到兄弟 box {sibling_type}")

    insert_at = target.offset + target.size
    box = bytes(pack(">I", len(payload) + 8)) + new_type.encode("latin-1") + bytes(payload)
    buf[insert_at:insert_at] = box

    # 父链逐个加长：从包含兄弟 box 的 sample entry 一路到 moov
    chain = _ancestor_chain(parsed, target)
    for box_node in chain:
        off = box_node.offset
        if off < 0 or buf[off : off + 4] != bytes(pack(">I", box_node.size)):
            raise ValueError(f"父 box {box_node.type} 的尺寸字段与解析不符，拒绝改写")
        size = box_node.size + len(box)
        if size > 0xFFFFFFFF:
            raise ValueError("尺寸超出 32 位，本项目的载荷远不到这个量级")
        buf[off : off + 4] = bytes(pack(">I", size))

    return bytes(data[:moov_offset]) + bytes(buf) + bytes(data[moov_offset + moov_size :])


def _find_box(root: Box, btype: str) -> Box | None:
    return next(iter(root.find_all(btype)), None)


def _ancestor_chain(root: Box, node: Box) -> tuple[Box, ...]:
    """node 往上到 moov 的父链（不含 moov 自身，moov 由顶层尺寸统一处理）。"""
    chain: list[Box] = []

    def walk(current: Box, path: tuple[Box, ...]) -> bool:
        for child in current.children:
            if child is node or child.offset == node.offset:
                chain[:] = path
                return True
            if walk(child, path + (child,)):
                return True
        return False

    # path 初始就把 root（moov）带上：插 box 会把 moov 自身也撑大，
    # 少改这一层的话外层按旧尺寸截断，整棵树都不完整。
    walk(root, (root,))
    return tuple(reversed(chain))


def hvcC_param_set_types(data: bytes) -> tuple[int, ...]:
    """init 段 hvcC 里已收录的参数集 NAL 类型（去重升序）；没有 hvcC 或无参数集时返回 ()。

    判据用途：Apple HLS/fMP4 要求 init 段的 hvcC 自带 VPS(32)/SPS(33)/PPS(34)。
    2026-09-26 真机实证：MKV 源 extradata 只有 23 字节 hvcC 头（numOfArrays=0）时，
    tvOS 拉到 init.mp4 200 + video0.m4s 200 后再也不拉下一分片——分片里 in-band
    参数集明明齐全（nal_types 32/33/34 都有），但 Apple 不从分片反哺 init，
    "播了但立刻失败"就是这么来的。
    """
    trees = _parse_siblings(data, 0, len(data), 1)
    hvcc = next((box for tree in trees for box in tree.find_all("hvcC")), None)
    if hvcc is None:
        return ()
    payload = data[hvcc.offset + 8 : hvcc.offset + hvcc.size]
    if len(payload) < 23:
        return ()
    types: list[int] = []
    cursor = 23
    for _ in range(payload[22]):
        if cursor + 3 > len(payload):
            break
        nal_type = payload[cursor] & 0x3F
        count = int.from_bytes(payload[cursor + 1 : cursor + 3], "big")
        cursor += 3
        for _ in range(count):
            if cursor + 2 > len(payload):
                break
            length = int.from_bytes(payload[cursor : cursor + 2], "big")
            cursor += 2
            if cursor + length > len(payload):
                break
            # NAL 头第 1 字节：forbidden(1)+type(6)，类型在高 6 位，要右移
            if (payload[cursor] >> 1) & 0x3F == nal_type:
                types.append(nal_type)
            cursor += length
    return tuple(sorted(set(types)))


def dedupe_hvcC_parameter_sets(data: bytes) -> tuple[bytes, int]:
    """hvcC 参数集数组去重：每种 NAL 类型只留第一条，返回 (新 bytes, 删除条数)。

    ffmpeg 写的 hvcC 把 VPS(32)/SPS(33)/PPS(34) 各写两份，Apple 官方参考流
    （adv_dv_atmos）各只一条——2026-09-25 逐 box A/B 实测这是我们 init 段与
    Apple 模板的唯一剩余差异（段内 VPS/SPS/PPS 重复已由 filter_units 三链清零，
    见 scripts/p7_to_p5.py 的 hls 步骤）。tvOS 是否因重复拒走 DV 直通未知，
    先做字节级对齐再真人验证。

    手术法同 insert_box_after：只动 moov 收尾的 init 段，重写 hvcC 载荷、父链
    尺寸逐个修正。同类型第 2+ 条与首条**逐字节相同**才删；出现内容不同的第二条
    参数集时 raise——那不是简单重复，静默丢弃会改解码信令（实测 dv_p7_v3 的
    init 就有一条 PPS 是首条追加 00 字节的变体，老打包路径产物）。
    """
    boxes = _scan_buffer(data)
    if not boxes or boxes[-1][0] != "moov":
        raise ValueError("只支持 moov 收尾的 init 段；本文件的顶层 box 不是这种布局")
    moov_offset, moov_size = boxes[-1][1], boxes[-1][2]
    buf = bytearray(data[moov_offset : moov_offset + moov_size])
    parsed = _parse_box(bytes(buf), 0, len(buf), 1)
    if parsed is None:
        raise ValueError("moov 解析失败")
    hvcc = _find_box(parsed, "hvcC")
    if hvcc is None:
        raise ValueError("init 段里找不到 hvcC box")

    payload = bytes(buf[hvcc.offset + 8 : hvcc.offset + hvcc.size])
    if len(payload) < 23:
        raise ValueError("hvcC 载荷短于 23 字节，不是合法的 HEVCDecoderConfigurationRecord")

    arrays: list[bytes] = []
    removed = 0
    cursor = 23
    for _ in range(payload[22]):
        if cursor + 3 > len(payload):
            raise ValueError("hvcC 数组头越界，拒绝改写")
        header = payload[cursor : cursor + 1]
        count = int.from_bytes(payload[cursor + 1 : cursor + 3], "big")
        cursor += 3
        nals: list[bytes] = []
        for _ in range(count):
            if cursor + 2 > len(payload):
                raise ValueError("hvcC NAL 长度字段越界，拒绝改写")
            length = int.from_bytes(payload[cursor : cursor + 2], "big")
            cursor += 2
            if cursor + length > len(payload):
                raise ValueError("hvcC NAL 载荷越界，拒绝改写")
            nals.append(payload[cursor : cursor + length])
            cursor += length
        for extra in nals[1:]:
            if extra != nals[0]:
                raise ValueError(
                    f"hvcC 里 NAL type {header[0] & 0x3F} 的第 2+ 条参数集与首条不同，"
                    "不是简单重复，拒绝去重"
                )
            removed += 1
        kept = nals[:1]
        rebuilt = header + len(kept).to_bytes(2, "big")
        for nal in kept:
            rebuilt += len(nal).to_bytes(2, "big") + nal
        arrays.append(rebuilt)

    if removed == 0:
        return data, 0
    new_payload = payload[:22] + len(arrays).to_bytes(1, "big") + b"".join(arrays)
    new_box = (len(new_payload) + 8).to_bytes(4, "big") + b"hvcC" + new_payload
    buf[hvcc.offset : hvcc.offset + hvcc.size] = new_box

    # 父链逐个缩短：从包含 hvcC 的 sample entry 一路到 moov（_ancestor_chain 的
    # path 初始值就带 moov，所以链尾是它自己）。父 box 的尺寸字段都位于 hvcC
    # 之前，不受本次改写影响；逐个校验旧值再写新值，不符就拒写。
    shrink = hvcc.size - len(new_box)
    for box_node in _ancestor_chain(parsed, hvcc):
        off = box_node.offset
        if off < 0 or buf[off : off + 4] != box_node.size.to_bytes(4, "big"):
            raise ValueError(f"父 box {box_node.type} 的尺寸字段与解析不符，拒绝改写")
        buf[off : off + 4] = (box_node.size - shrink).to_bytes(4, "big")

    return bytes(data[:moov_offset]) + bytes(buf) + bytes(data[moov_offset + moov_size :]), removed


def drop_hvcC_param_set_type(data: bytes, nal_type: int) -> tuple[bytes, int]:
    """从 init 段 hvcC 里删掉整个指定 NAL 类型的参数集数组，返回 (新 bytes, 删除数组数)。

    变体 B 的依据（2026-09-26 实测）：已验证能播的 1080p 流 hvcC 148B = 仅
    (32,[24]),(33,[78]),(34,[8])，无 type-39；live 4K 单 entry 包 hvcC 232B
    = 上述三组 + (39,[6,22,13]) 三个前缀 SEI NAL。剥 39 即与能播模板同构。

    手术法同 dedupe_hvcC_parameter_sets：只动 moov 收尾的 init 段，保留的
    数组逐字节照抄、numOfArrays 扣减删除数、hvcC 载荷重写、父链尺寸逐个
    校验后扣减；字段与解析不符即 raise，不静默产出。32/33/34 是 Apple HLS
    init 段必需参数集，点名要删直接 raise；数组不存在时原样返回、删除数 0
    （幂等）。
    """
    if nal_type in _MANDATORY_PARAM_SETS:
        raise ValueError("VPS/SPS/PPS(32/33/34) 是 Apple HLS init 段必需参数集，拒绝删除")
    boxes = _scan_buffer(data)
    if not boxes or boxes[-1][0] != "moov":
        raise ValueError("只支持 moov 收尾的 init 段；本文件的顶层 box 不是这种布局")
    moov_offset, moov_size = boxes[-1][1], boxes[-1][2]
    buf = bytearray(data[moov_offset : moov_offset + moov_size])
    parsed = _parse_box(bytes(buf), 0, len(buf), 1)
    if parsed is None:
        raise ValueError("moov 解析失败")
    hvcc = _find_box(parsed, "hvcC")
    if hvcc is None:
        raise ValueError("init 段里找不到 hvcC box")

    payload = bytes(buf[hvcc.offset + 8 : hvcc.offset + hvcc.size])
    if len(payload) < 23:
        raise ValueError("hvcC 载荷短于 23 字节，不是合法的 HEVCDecoderConfigurationRecord")

    arrays: list[bytes] = []
    dropped = 0
    cursor = 23
    for _ in range(payload[22]):
        start = cursor
        if cursor + 3 > len(payload):
            raise ValueError("hvcC 数组头越界，拒绝改写")
        array_type = payload[cursor] & 0x3F
        count = int.from_bytes(payload[cursor + 1 : cursor + 3], "big")
        cursor += 3
        for _ in range(count):
            if cursor + 2 > len(payload):
                raise ValueError("hvcC NAL 长度字段越界，拒绝改写")
            length = int.from_bytes(payload[cursor : cursor + 2], "big")
            cursor += 2
            if cursor + length > len(payload):
                raise ValueError("hvcC NAL 载荷越界，拒绝改写")
            cursor += length
        if array_type == nal_type:
            dropped += 1
        else:
            arrays.append(payload[start:cursor])

    if dropped == 0:
        return bytes(data), 0
    new_payload = payload[:22] + len(arrays).to_bytes(1, "big") + b"".join(arrays)
    new_box = (len(new_payload) + 8).to_bytes(4, "big") + b"hvcC" + new_payload
    buf[hvcc.offset : hvcc.offset + hvcc.size] = new_box

    # 父链逐个缩短：从包含 hvcC 的 sample entry 一路到 moov（_ancestor_chain 的
    # path 初始值就带 moov，所以链尾是它自己）。父 box 的尺寸字段都位于 hvcC
    # 之前，不受本次改写影响；逐个校验旧值再写新值，不符就拒写。
    shrink = hvcc.size - len(new_box)
    for box_node in _ancestor_chain(parsed, hvcc):
        off = box_node.offset
        if off < 0 or buf[off : off + 4] != box_node.size.to_bytes(4, "big"):
            raise ValueError(f"父 box {box_node.type} 的尺寸字段与解析不符，拒绝改写")
        buf[off : off + 4] = (box_node.size - shrink).to_bytes(4, "big")

    return (
        bytes(data[:moov_offset]) + bytes(buf) + bytes(data[moov_offset + moov_size :]),
        dropped,
    )


def remove_box(data: bytes, target_type: str) -> tuple[bytes, int]:
    """从 init 段的 moov 里删掉所有指定类型的 box，返回 (新 bytes, 删除数)。

    手术法与 insert_box_after 对称：只动 moov 收尾的 init 段。删除不需要重算
    任何偏移（fMP4 init 的 stbl 里没有指向 mdat 的 chunk offset），只把祖先链
    的尺寸字段逐个扣减。找不到该类型时原样返回、删除数 0，调用方按需把 0 当
    异常；父链尺寸或待删 box 的头字段与解析不符时 raise，不静默产出。

    Apple 官方 P5 参考流（adv_dv_atmos）的 init 段没有 btrt / edts，ffmpeg
    产出各带一个——“按 Apple 模板清非信令 box”的对照实验用这个函数。
    """
    boxes = _scan_buffer(data)
    if not boxes or boxes[-1][0] != "moov":
        raise ValueError("只支持 moov 收尾的 init 段；本文件的顶层 box 不是这种布局")
    moov_offset, moov_size = boxes[-1][1], boxes[-1][2]
    parsed = _parse_box(bytes(data[moov_offset : moov_offset + moov_size]), 0, moov_size, 1)
    if parsed is None:
        raise ValueError("moov 解析失败")

    matches = [
        (node, _ancestor_chain(parsed, node)) for node in parsed.find_all(target_type)
    ]
    if not matches:
        return bytes(data), 0

    buf = bytearray(data)
    shrunk: dict[int, int] = {}
    # 偏移从大到小删：先删的不动后面的 box 位置；同一祖先被多次命中时按累计
    # 扣减记账，否则第二次校验旧尺寸必然误报。
    for node, chain in sorted(matches, key=lambda item: item[0].offset, reverse=True):
        start = moov_offset + node.offset
        if buf[start : start + 4] != node.size.to_bytes(4, "big"):
            raise ValueError(f"待删 box {target_type} 的尺寸字段与解析不符，拒绝改写")
        if buf[start + 4 : start + 8] != target_type.encode("latin-1"):
            raise ValueError(f"待删 box 的类型字段不是 {target_type}，拒绝改写")
        buf[start : start + node.size] = b""
        for ancestor in chain:
            off = moov_offset + ancestor.offset
            current = ancestor.size - shrunk.get(off, 0)
            if buf[off : off + 4] != current.to_bytes(4, "big"):
                raise ValueError(f"父 box {ancestor.type} 的尺寸字段与解析不符，拒绝改写")
            buf[off : off + 4] = (current - node.size).to_bytes(4, "big")
            shrunk[off] = shrunk.get(off, 0) + node.size
    return bytes(buf), len(matches)


def sample_entry_count(data: bytes) -> int:
    """init 段 moov 里第一个 stsd 的 sample entry 数（Apple 官方模板 = 1）。"""
    boxes = _scan_buffer(data)
    if not boxes or boxes[-1][0] != "moov":
        raise ValueError("只支持 moov 收尾的 init 段；本文件的顶层 box 不是这种布局")
    moov_offset, moov_size = boxes[-1][1], boxes[-1][2]
    parsed = _parse_box(bytes(data[moov_offset : moov_offset + moov_size]), 0, moov_size, 1)
    if parsed is None:
        raise ValueError("moov 解析失败")
    stsd = next((b for b in parsed.find_all("stsd")), None)
    if stsd is None:
        raise ValueError("init 段里找不到 stsd box")
    return len(stsd.children)


def keep_single_sample_entry(data: bytes, keep_index: int = 0) -> tuple[bytes, int]:
    """init 段 stsd 收敛成单个 video sample entry，返回 (新 bytes, 删除 entry 数)。

    ffmpeg movenc 在流中 extradata 变化时会追加第二个 dvh1 entry：BSF 参数集
    注入路径的产物就是 entry0 hvcC 带 VPS/SPS/PPS + 前缀 SEI(39) 数组、
    entry1 只带 VPS/SPS/PPS。2026-09-26 真机实测：trex
    default_sample_description_index=1、11 个分片 tfhd 全无
    sample-description-index 覆盖——分片只引 entry0，第二个 entry 是死重。
    Apple 官方 P5 参考流与已验证能播的 1080p 流都是单 entry，tvOS 对
    stsd entry_count>1 的行为未验证，按能播模板收敛。

    手术法同 remove_box：victim 字节整段删除、stsd entry_count 减 1、父链
    （stsd→stbl→minf→mdia→trak→moov）尺寸字段逐个校验后扣减；字段与解析
    不符即 raise，不静默产出。keep_index 越界、stsd 混入非 video entry 同样
    raise。已是单 entry 时原样返回、删除数 0（幂等）。
    """
    boxes = _scan_buffer(data)
    if not boxes or boxes[-1][0] != "moov":
        raise ValueError("只支持 moov 收尾的 init 段；本文件的顶层 box 不是这种布局")
    moov_offset, moov_size = boxes[-1][1], boxes[-1][2]
    parsed = _parse_box(bytes(data[moov_offset : moov_offset + moov_size]), 0, moov_size, 1)
    if parsed is None:
        raise ValueError("moov 解析失败")
    stsd = next((b for b in parsed.find_all("stsd")), None)
    if stsd is None:
        raise ValueError("init 段里找不到 stsd box")
    entries = stsd.children
    if len(entries) <= 1:
        return bytes(data), 0
    if any(entry.type not in VIDEO_SAMPLE_ENTRIES for entry in entries):
        raise ValueError("stsd 里混入非 video sample entry，拒绝收敛")
    if not 0 <= keep_index < len(entries):
        raise ValueError(f"keep_index={keep_index} 越界（共 {len(entries)} 个 entry）")
    victims = [entry for index, entry in enumerate(entries) if index != keep_index]

    count_off = moov_offset + stsd.offset + 12  # header(8) + version/flags(4)
    buf = bytearray(data)
    old_count = int.from_bytes(buf[count_off : count_off + 4], "big")
    if old_count != len(entries):
        raise ValueError(
            f"stsd entry_count={old_count} 与实际 entry 数 {len(entries)} 不符，拒绝改写"
        )
    buf[count_off : count_off + 4] = (old_count - len(victims)).to_bytes(4, "big")

    # 同一祖先链（stsd→stbl→minf→mdia→trak→moov）会被多个 victim 命中，
    # 按累计扣减记账，否则第二次校验原始尺寸必然误报（同 remove_box）。
    shrunk: dict[int, int] = {}
    for victim in sorted(victims, key=lambda item: item.offset, reverse=True):
        start = moov_offset + victim.offset
        if buf[start : start + 4] != victim.size.to_bytes(4, "big"):
            raise ValueError(f"待删 sample entry {victim.type} 的尺寸字段与解析不符，拒绝改写")
        if buf[start + 4 : start + 8] != victim.type.encode("latin-1"):
            raise ValueError("待删 sample entry 的类型字段与解析不符，拒绝改写")
        del buf[start : start + victim.size]
        for ancestor in _ancestor_chain(parsed, victim):
            off = moov_offset + ancestor.offset
            current = ancestor.size - shrunk.get(off, 0)
            if buf[off : off + 4] != current.to_bytes(4, "big"):
                raise ValueError(f"父 box {ancestor.type} 的尺寸字段与解析不符，拒绝改写")
            buf[off : off + 4] = (current - victim.size).to_bytes(4, "big")
            shrunk[off] = shrunk.get(off, 0) + victim.size
    return bytes(buf), len(victims)


def expected_dv_level(width: int, height: int) -> int:
    """分辨率 → Dolby Vision level 档位。

    2026-10-02 实测口径：Apple 官方 1080p P5 参考流 dv_level=3；两个点亮版
    4K 包 dv_level=6；dv_level=9 是 8K 档。Jellyfin 有 4K 片源在 dvcC 里签
    9，原样带进 init 后 tvOS 不按 DV 源处理（徽标不亮、颜色也不对）。
    """
    if height <= 1080:
        return 3
    if height <= 2160:
        return 6
    return 9


def clamp_dvcC_level(data: bytes, level: int) -> tuple[bytes, int | None]:
    """dvcC 的 dv_level 高于档位时压到档位，返回 (bytes, 原值 or None)。

    只降不升：源签得比分辨率档位还保守时不动，不把能力签高。dvcC 无校验和，
    改 dv_level 所在字节即可；定位走 locate_dv_boxes 的绝对偏移。

    dvcC 位域（2026-10-02 用三个样本互证标定）：payload[2] 高 7 位是
    dv_profile、低 1 位是 rpu_present_flag；payload[3] 高 5 位是 dv_level、
    低 3 位是 el/bl 等存在位。实测 0x4d→9（不亮版）、0x35→6（点亮版）、
    0x1d→3（Apple 1080p 官方参考流），一律 level<<3 | flags。
    """
    for box in locate_dv_boxes(data):
        if box.fourcc != "dvcC" or len(box.payload) < 4:
            continue
        current = box.payload[3] >> 3
        if current <= level:
            return data, None
        buf = bytearray(data)
        buf[box.payload_offset + 3] = (level << 3) | (box.payload[3] & 0x07)
        return bytes(buf), current
    return data, None


def clear_compressor_name(data: bytes, fourcc: str = "dvh1") -> tuple[bytes, str | None]:
    """清空 sample entry 的 compressor 名称字段，返回 (bytes, 原名 or None)。

    VisualSampleEntry 的 compressorname 是定长 32 字节（首字节长度 + 31 字节
    名称），落在 entry 起始 +50 处，其后是 depth(0x0018) / pre_defined(0xffff)。
    2026-10-02 对照：点亮版 init 与 Apple 官方参考流该字段都是空，Jellyfin
    部分片源带 "DOVI Coding"，ffmpeg 原样抄进 init。字段非空时按上述结构
    逐项校验才清零，校验不过就 raise，不静默写坏 sample entry。
    """
    boxes = _scan_buffer(data)
    moov = next(((off, size) for name, off, size in boxes if name == "moov"), None)
    if moov is None:
        return data, None
    moov_offset, moov_size = moov
    buf = bytes(data[moov_offset : moov_offset + moov_size])
    parsed = _parse_box(buf, 0, len(buf), 1)
    if parsed is None:
        return data, None
    for entry in parsed.find_all(fourcc):
        field = moov_offset + entry.offset + 50
        length = data[field]
        if length == 0:
            continue
        if not 1 <= length <= 31:
            raise ValueError(f"{fourcc} compressor 长度字节可疑：{length}")
        name = data[field + 1 : field + 1 + length]
        if not all(32 <= b < 127 for b in name):
            raise ValueError(f"{fourcc} compressor 名称不可打印：{name!r}")
        if data[field + 32 : field + 34] != b"\x00\x18":
            raise ValueError(f"{fourcc} compressor 字段后不是 depth，定位可疑")
        if data[field + 34 : field + 36] != b"\xff\xff":
            raise ValueError(f"{fourcc} compressor 字段后不是 pre_defined，定位可疑")
        out = bytearray(data)
        out[field : field + 32] = b"\x00" * 32
        return bytes(out), name.decode("ascii")
    return data, None


def atomic_write(path: Path, data: bytes) -> None:
    """fix-up 写盘走原子替换：同目录临时文件 → fsync → os.replace。

    2026-09-26 两次事故：init.mp4 被截成 0 字节（写盘中途被打断？多进程
    同时 fix-up？原因未明）。半页初始化段不会让打包报错，只会让 AVPlayer
    拉完 200 后静默停播——比失败更难排查。临时文件与目标同文件系统，
    os.replace 的替换是原子的，读者只会见到旧文件或新文件。
    """
    tmp = path.with_name(f"{path.name}.tmp")
    with open(tmp, "wb") as handle:
        handle.write(bytes(data))
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


# --------------------------------------------------------------------------
# sidx 归一化（2026-10-03《纽伦堡》ab476f22 现场）
#
# MKV 源（视频 + DTS-HD + 5 条 PGSSUB）走完 ffmpeg HLS fMP4 muxer 后，每个
# 分片的第一个 sidx box 的 reference_ID 与 timescale 是未初始化内存值：
# 实测 ref_id∈{0,1,2,3…}（视频轨应为 1）、timescale∈{0, 519984, 578016, …}
# （应为 init 视频轨的 16000，含 0 → 段时长除零），1437/1437 段全坏，无一
# 例外。对照组：mp4 源（097c703b / ff951e2e）300+ 段 ref_id/timescale 全对
# ——坏包 100% 坏、能播包 100% 好，这是唯一的包间结构性差异。sidx 的
# earliest / duration 本身是对的（音频 sidx 交叉验证一致），所以原地覆写
# 这 8 个字节即可，零重编码、零磁盘新增。真机症状：AVPlayer 解析完
# master/video.m3u8 后一个分片都不请求，"无法播放"。
#
# fMP4 HLS 的 sidx#1 约定是视频轨：Apple 官方参考流与本机 ffmpeg 9.0.2
# 原生参照包同构（sidx#1 ref_id=视频轨、first_offset=52；sidx#2 ref_id=音频
# 轨、first_offset=0）。这里只认第一个 sidx，且只在该值明显不是音频轨时
# 才覆写——顺序颠倒或无 sidx 的段一律跳过并计入异常，绝不错伤。
# --------------------------------------------------------------------------

_SIDX_HEAD_BYTES = 256  # styp(24) + sidx(52) + sidx(52) = 128，读 256 足够且不碰 mdat


def init_video_track(init_path: str | Path) -> tuple[int, int]:
    """init 段视频轨的 (track_ID, mdhd timescale)。

    track_ID 取自 tkhd（payload+20 于 version 1、+12 于 version 0），
    timescale 取自同轨 mdhd（偏移规则相同）。找不到视频轨就抛
    ValueError——sidx 归一的参照值错了，整包每个分片都会跟着错。
    """
    data = Path(init_path).read_bytes()
    video_id = None
    video_ts = None
    for off, btype, size, hdr in _iter_box_offsets(data, 0, len(data)):
        if btype != b"moov":
            continue
        for trak_off, trak_type, trak_size, trak_hdr in _iter_box_offsets(
                data, off + hdr, off + size):
            if trak_type != b"trak":
                continue
            handler = None
            track_id = None
            timescale = None
            for child_off, child_type, child_size, child_hdr in _iter_box_offsets(
                    data, trak_off + trak_hdr, trak_off + trak_size):
                if child_type == b"tkhd":
                    body = child_off + child_hdr
                    version = data[body]
                    skip = 20 if version == 1 else 12
                    track_id = int.from_bytes(data[body + skip:body + skip + 4], "big")
                elif child_type == b"mdia":
                    for mid, mtype, msize, mhdr in _iter_box_offsets(
                            data, child_off + child_hdr, child_off + child_size):
                        if mtype == b"hdlr":
                            # payload = ver/flags(4) + predefined(4) + handler(4)
                            body = mid + mhdr + 8
                            handler = data[body:body + 4].decode("latin-1")
                        elif mtype == b"mdhd":
                            body = mid + mhdr
                            version = data[body]
                            skip = 20 if version == 1 else 12
                            timescale = int.from_bytes(
                                data[body + skip:body + skip + 4], "big")
            if handler == "vide" and track_id is not None and timescale:
                video_id, video_ts = track_id, timescale
                break
        if video_id is not None:
            break
    if video_id is None or not video_ts:
        raise ValueError("init 段没有可用的视频轨：%s" % Path(init_path).name)
    return video_id, video_ts


def _iter_box_offsets(data: bytes, start: int, end: int):
    """产出 (offset, type, size, header)——与 parpack._iter_boxes 同一口径。"""
    i = start
    while i + 8 <= end:
        size = struct.unpack_from(">I", data, i)[0]
        header = 8
        if size == 1:
            size = struct.unpack_from(">Q", data, i + 8)[0]
            header = 16
        elif size == 0:
            size = end - i
        if size < header or i + size > end:
            return
        yield i, bytes(data[i + 4:i + 8]), size, header
        i += size


def _segment_sidx_heads(path: str | Path) -> list[tuple[int, int, int, int]]:
    """分片头部每个 sidx 的 (payload_off, ref_id, timescale, size)。

    只读前 _SIDX_HEAD_BYTES 字节，绝不碰 mdat——1000+ 段的全片包里这是
    O(段数×256B) 的读放大，而不是 O(整包)。
    """
    with open(path, "rb") as handle:
        head = handle.read(_SIDX_HEAD_BYTES)
    heads = []
    for off, btype, size, hdr in _iter_box_offsets(head, 0, len(head)):
        if btype != b"sidx":
            continue
        payload = off + hdr
        version = head[payload]
        if size != 52 or version != 1:
            raise ValueError(
                f"{Path(path).name}: sidx size={size} version={version}，"
                "本归一化只支持 size=52/version=1")
        heads.append((payload,
                     int.from_bytes(head[payload + 4:payload + 8], "big"),
                     int.from_bytes(head[payload + 8:payload + 12], "big"),
                     size))
    return heads


def normalize_sidx(segments, init_path) -> tuple[int, list[str]]:
    """把每段第一个 sidx 的 reference_ID/timescale 覆写成 init 视频轨真值。

    返回 (修复段数, 异常说明)。幂等：本来就是对的分片直接跳过，无写盘。
    """
    video_id, video_ts = init_video_track(init_path)
    fixed = 0
    anomalies: list[str] = []
    for seg in segments:
        heads = _segment_sidx_heads(seg)
        if not heads:
            anomalies.append(f"{Path(seg).name}: 无 sidx")
            continue
        first = heads[0]
        if len(heads) >= 2 and (first[1], first[2]) == (heads[1][1], heads[1][2]):
            anomalies.append(f"{Path(seg).name}: 前两个 sidx 完全相同，放弃")
            continue
        payload_off, ref_id, timescale, _size = first
        if ref_id == video_id and timescale == video_ts:
            continue
        with open(seg, "r+b") as handle:
            handle.seek(payload_off + 4)
            handle.write(struct.pack(">I", video_id))
            handle.seek(payload_off + 8)
            handle.write(struct.pack(">I", video_ts))
        fixed += 1
    return fixed, anomalies


def verify_sidx(segments, init_path) -> None:
    """发布前守门：任一段第一个 sidx 与 init 视频轨对不上就抛 ValueError。"""
    video_id, video_ts = init_video_track(init_path)
    bad = []
    for seg in segments:
        heads = _segment_sidx_heads(seg)
        if not heads:
            bad.append(f"{Path(seg).name}: 无 sidx")
            continue
        ref_id, timescale = heads[0][1], heads[0][2]
        if ref_id != video_id or timescale != video_ts:
            bad.append(f"{Path(seg).name}: ref_id={ref_id} timescale={timescale}")
        if len(bad) >= 5:
            break
    if bad:
        raise ValueError(
            "sidx 与 init 视频轨不一致（%d+ 处）：%s；期望 ref_id=%d "
            "timescale=%d。tvOS 会解析完播放列表后一个分片都不请求"
            % (len(bad), "; ".join(bad[:5]), video_id, video_ts))

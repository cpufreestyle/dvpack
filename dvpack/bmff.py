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

from collections import Counter
from dataclasses import dataclass
from pathlib import Path

DV_SAMPLE_ENTRIES = frozenset({"dvh1", "dvhe", "dav1"})
VIDEO_SAMPLE_ENTRIES = DV_SAMPLE_ENTRIES | {"hvc1", "hev1", "avc1", "avc3", "av01", "vp09"}

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

_RPU_NAL_TYPES = frozenset({62, 63})  # HEVC unregistered prefix / suffix NAL，DV 用它带 RPU
# type 62 的 NAL header 是 `7c 01`，其后第一个 payload 字节是 rpu_nal_prefix=25。
# 实测自本机片源，且与 dovi_tool `info` 输出的 rpu_nal_prefix 一致。
_RPU_NAL_PREFIX = 0x19


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

    @property
    def is_video(self) -> bool:
        return self.handler == "vide"

    @property
    def declares_dolby_vision(self) -> bool:
        """Apple 要求 dvh1（基于 hvc1）。dvhe 基于 hev1，被 Apple 明确标注不推荐。"""
        return self.sample_entry_type in DV_SAMPLE_ENTRIES

    @property
    def has_dvvC(self) -> bool:
        return self.dvcc is not None

    @property
    def signalling(self) -> str:
        """DV 信令成立 = sample entry 是 dvh1 且带 dvvC。两个条件缺一不可。"""
        if self.sample_entry_type == "dvh1" and self.has_dvvC:
            return "dvh1+dvvC"
        if self.sample_entry_type == "dvh1":
            return "dvh1 但缺 dvvC"
        if self.has_dvvC:
            return f"有 dvvC 但 sample entry 是 {self.sample_entry_type}"
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


def _report_track(trak: Box, data: bytes) -> TrackReport:
    return TrackReport(
        track_id=_track_id(trak, data),
        handler=_handler(trak, data),
        sample_entry_type=_sample_entry(trak, data)[0],
        sample_entry_children=_sample_entry(trak, data)[1],
        dvcc=_payload(trak, data, "dvvC") or _payload(trak, data, "dvcC"),
    )


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


def _payload(trak: Box, data: bytes, name: str) -> bytes | None:
    found = next(iter(trak.find_all(name)), None)
    if found is None:
        return None
    return data[found.offset + 8 : found.offset + found.size]


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

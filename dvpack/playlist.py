"""master playlist：把探测出来的 DV 信令写成 Apple 要求的属性串。

ffmpeg 的 HLS 输出只给媒体列表，不带 `CODECS` / `VIDEO-RANGE`，而这两个属性正是
tvOS 决定要不要走杜比视界解码的入口。所以列表由我们自己写，分片和 init 段交给 ffmpeg。

信令分工：CODECS 写 hvc1（HEVC 编码轨），dvh1 走 SUPPLEMENTAL-CODECS。
写反或写漏 dvh1 都会被 Apple TV 整份拒收（AVFoundationErrorDomain -11848 /
CoreMediaErrorDomain -15517，2026-10-03 真机现场）。

依据是 Apple HLS Authoring Specification 的附录：`dvh1.<profile>.<level>` 两段都是两位
十进制（`dvh1.05.06` = Profile 5 Level 6），并且"兼容 brand 与 VIDEO-RANGE 互为交叉校验，
少写任一个都是错误的"。
"""

from __future__ import annotations

import math
import os
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Iterable, Sequence

from .dvprofile import HEVC_LEVEL_CAP, VIDEO_RANGE_TO_BRAND, hevc_base_codec as _hevc_codec
from .probe import DoviRecord, SourceInfo
from .subs import SUBTITLES_GROUP, SubtitleTrack

#: Apple TV 4K（3rd gen，tvOS 26/27）对 master 变体声明的 HEVC level 有上限：
#: 写超过 5.1（L153）会被整份 master 拒掉——集中定义见 dvprofile.HEVC_LEVEL_CAP。
_SUPPLEMENTAL_BY_RANGE = VIDEO_RANGE_TO_BRAND
_HEVC_LEVEL_CAP = HEVC_LEVEL_CAP


def hevc_base_codec() -> str:
    """master 的 CODECS 视频段：hvc1（HEVC 编码轨），**不带** DV 信令。

    dvh1.* 是采样条目用的 fourcc，写进 CODECS 会被客户端解析成 HEVC
    general_profile_idc=5（Apple 平台不存在这个 profile）+ level 过高，
    于是整份 master 被拒——真机症状是 ITEM FAILED err=Cannot Open，
    连一个分片都不请求（2026-10-03 现场，_jf 直连通路）。
    DV 信令移到 SUPPLEMENTAL-CODECS，见 supplemental_codecs()。
    """
    return _hevc_codec(_HEVC_LEVEL_CAP)


@dataclass(frozen=True)
class Variant:
    uri: str
    bandwidth: int
    codecs: str
    video_range: str
    resolution: tuple[int, int] | None = None
    frame_rate: float | None = None
    hdcp_level: str | None = None
    supplemental_codecs: str | None = None
    #: 关联的字幕轨组；master 写了 #EXT-X-MEDIA 字幕轨时由 master_playlist 填上
    subtitles: str | None = None

    def stream_inf(self) -> str:
        # 属性顺序不强制，但 BANDWIDTH 必须在最前（RFC 8216 的写法惯例）
        parts = [f"BANDWIDTH={self.bandwidth}"]
        if self.codecs:
            parts.append(f'CODECS="{self.codecs}"')
        if self.resolution:
            parts.append(f"RESOLUTION={self.resolution[0]}x{self.resolution[1]}")
        if self.frame_rate:
            # RFC 8216：FRAME-RATE 必须向上取整
            parts.append(f"FRAME-RATE={math.ceil(self.frame_rate)}")
        if self.hdcp_level:
            parts.append(f"HDCP-LEVEL={self.hdcp_level}")
        if self.video_range:
            parts.append(f"VIDEO-RANGE={self.video_range}")
        if self.subtitles:
            parts.append(f'SUBTITLES="{self.subtitles}"')
        if self.supplemental_codecs:
            parts.append(f'SUPPLEMENTAL-CODECS="{self.supplemental_codecs}"')
        return "#EXT-X-STREAM-INF:" + ",".join(parts)


def supplemental_codecs(source: SourceInfo, target: DoviRecord | None = None) -> str | None:
    """DV 信令写进 SUPPLEMENTAL-CODECS，而不是 CODECS。

    2026-10-03 修正：旧代码只对 profile 8 写、P5 留空，理由是「Apple 官方
    P5 变体不写 SUPPLEMENTAL」——这条认知已被真机推翻。P5 同样带 base layer
    （bl_present_flag=1），所以同样要写 db1p。

    写法参照 bitstorm-labs/sashimi-apple#282（Apple-conformant，tvOS 实测
    能点亮 DV）：CODECS="hvc1.2.4.L150.B0,ec-3" + VIDEO-RANGE=PQ
    + SUPPLEMENTAL-CODECS="dvh1.08.07/db1p"。
    """
    dovi = target if target is not None else source.dovi
    if dovi is None:
        return None
    brand = _SUPPLEMENTAL_BY_RANGE.get(source.video.apple_video_range) or "db1p"
    return f"{dovi.target_apple_codec}/{brand}"


def variant_from_source(
    source: SourceInfo,
    uri: str,
    *,
    bandwidth: int,
    hdcp_level: str | None = "TYPE-1",
    target: DoviRecord | None = None,
    audio_codec: str | None = None,
) -> Variant:
    """DV 信令全部来自探测结果，不在这里二次推断。

    target 是转签后的信令（默认等于源）。默认值不变，所以 P8 直打包的通路
    行为和以前一致；P5 通路的 codecs / supplemental 都按 target 走。

    audio_codec 写进 CODECS（默认取 source.audio_codec）。**必须列全**：产物是
    音视频同流时，变体里既有 dvh1 又有 aac/ec-3，CODECS 只写视频会让 AVPlayer
    拒收整个 master——真机症状是黑屏且日志里只有一次 master.m3u8、一个分片
    都没再请求（2026-09-27）。音频不在本变体里的调用方传 None。
    """
    dovi = target if target is not None else source.dovi
    if dovi is None:
        raise ValueError(f"{source.path} 没有 DOVI 记录，不该走 DV 打包")
    video = source.video
    if target is not None and dovi.profile == 5:
        # Apple 官方 P5 变体（adv_dv_atmos）不写 HDCP-LEVEL，照着参考来
        hdcp_level = None
    # CODECS 只写 hvc1（HEVC 编码轨）；dvh1 信令走 SUPPLEMENTAL-CODECS。
    # 直接写 dvh1.<profile>.<level> 会让 Apple TV 拒掉整份 master
    # （-11848 / -15517），见 hevc_base_codec() 的说明。
    codec = hevc_base_codec()
    audio = source.audio_codec if audio_codec is None else audio_codec
    if audio:
        # 顺序按 RFC 6381 惯例：视频在前、音频在后。
        codec = f"{codec},{audio}"
    return Variant(
        uri=uri,
        bandwidth=bandwidth,
        codecs=codec,
        video_range=video.apple_video_range,
        resolution=(video.width, video.height) if video.width and video.height else None,
        frame_rate=video.frame_rate or None,
        hdcp_level=hdcp_level,
        supplemental_codecs=supplemental_codecs(source, target=target),
    )


def bandwidth_from_segments(
    segments: Iterable[Path],
    seconds: float,
    segment_seconds: Sequence[float] | None = None,
    *,
    margin: float = 1.2,
) -> int:
    """实测带宽：按分片峰值码率取整，再留 20% 余量。

    Apple HLS Authoring Specification 要求 BANDWIDTH 是变体的峰值码率上界。
    整片平均值偏低（DV 片源的 RPU + 高动态段能把单段冲到均值的 3 倍），写低了
    AVPlayer 会按错误码率配缓冲：真机症状是起播后周期性 buffer underrun、tvOS
    拉段节奏退化，最后 burst 一段后彻底停。2026-10-03 纽伦堡实测：整片
    21.66 Mbps、峰值 70.49 Mbps、master 却写 24.48 Mbps。

    segment_seconds 是逐段的 #EXTINF 秒数，能拿到就必须给：jfpack 走的是
    -hls_time 6.0 加 fMP4 关键帧切分，同一个 148 分钟的片子里段长能从 0.041s
    跨到 14.917s。拿 max(段字节) / 平均段长 反推峰值，会把一条 71MB/14.9s 的
    长段算成 92 Mbps，比真峰值 70.5 高 31%——AVPlayer 按假峰值去要缓冲，起播
    只会更重。真峰值是 max(段字节 * 8 / 该段 EXTINF)。只有等长切分（parpack 的
    -segment_time）才能缺省，那时平均段长就是单段时长。

    段长条数与分片数不一致说明调用方拿错了列表，宁可报错也不要写错的 BANDWIDTH。
    """
    sizes = [path.stat().st_size for path in segments]
    count = len(sizes)
    if count == 0:
        raise ValueError("至少需要一个分片")
    if seconds <= 0:
        raise ValueError("时长必须为正")
    total = sum(sizes)
    average = total * 8 / seconds
    if segment_seconds is None:
        # 等长切分：平均段长即单段时长，这是缺省时对该段长的最好估计。
        peak = max(sizes) * 8 / (seconds / count)
    else:
        durations = list(segment_seconds)
        if len(durations) != count:
            raise ValueError(
                f"分片时长 {len(durations)} 个与分片 {count} 个不一致"
            )
        if any(duration <= 0 for duration in durations):
            raise ValueError("分片时长必须为正")
        peak = max(size * 8 / duration for size, duration in zip(sizes, durations))
    return int(max(average, peak) * margin)


def playlist_segment_seconds(playlist: Path) -> list[float]:
    """逐段读 HLS 媒体列表的 #EXTINF 秒数。

    BANDWIDTH 只有拿到逐段长才算得出真峰值（见 bandwidth_from_segments），而这个
    数字只存在于列表里——ffprobe 只给总时长。坏行跳过，让调用方靠长度校验去发现
    问题，而不是在这里猜一个值。
    """
    durations: list[float] = []
    for line in Path(playlist).read_text(encoding="utf-8").splitlines():
        if not line.startswith("#EXTINF:"):
            continue
        try:
            durations.append(float(line.split(":", 1)[1].split(",", 1)[0]))
        except ValueError:
            continue
    return durations


def master_playlist(
    variants: Iterable[Variant],
    *,
    independent_segments: bool = True,
    subtitles: Iterable[SubtitleTrack] | None = None,
) -> str:
    lines = ["#EXTM3U", "#EXT-X-VERSION:7"]
    if independent_segments:
        lines.append("#EXT-X-INDEPENDENT-SEGMENTS")
    tracks = list(subtitles) if subtitles is not None else []
    for track in tracks:
        # #EXT-X-MEDIA 必须排在第一条 #EXT-X-STREAM-INF 之前（RFC 8216）
        lines.append(track.media_line())
    for variant in variants:
        if tracks:
            # 字幕轨与变体之间唯一的关联就是这个 group 引用
            variant = replace(variant, subtitles=SUBTITLES_GROUP)
        lines += [variant.stream_inf(), variant.uri]
    return "\n".join(lines) + "\n"


def write_master_playlist(path: str | Path, variants: Iterable[Variant], **kwargs) -> Path:
    """用 write_bytes 落盘：文本模式在 Windows 上会把 `\n` 翻成 `\r\n`。

    两种换行 HLS 都接受，但钉死 LF 可以让产出的列表在跨平台比对时没有噪音。
    """
    target = Path(path)
    # 必须原子替换：write_bytes 是"打开即截断 → 写"。打包中途磁盘打满
    # （ENOSPC：87GB _jf 产物曾把 926GB 盘吃到 100%）时，这一步会把**正在被
    # AVPlayer 播放的 master.m3u8 冲成 0 字节**——电视侧症状正是
    # ITEM FAILED err=Cannot Open / "播放失败"（2026-10-03 现场）。
    payload = master_playlist(variants, **kwargs).encode("utf-8")
    tmp = target.with_name(target.name + ".tmp")
    tmp.write_bytes(payload)
    os.replace(tmp, target)
    return target


def parse_stream_inf(line: str) -> dict[str, str]:
    """解析 #EXT-X-STREAM-INF 的属性串。

    引号值里的逗号不是分隔符（CODECS="hvc1.2.4.L153.B0,ec-3" 一刀切会把它
    劈成两个假属性）。裸值按逗号切。返回值都是去引号的原文，类型转换交给
    调用方——升级路径宁可丢一个可选属性，也不猜一个值。
    """
    raw = line.split(":", 1)[1] if ":" in line else ""
    attrs: dict[str, str] = {}
    index = 0
    while index < len(raw):
        eq = raw.find("=", index)
        if eq < 0:
            break
        key = raw[index:eq].strip()
        if not key:
            break
        cursor = eq + 1
        if cursor < len(raw) and raw[cursor] == '"':
            end = raw.find('"', cursor + 1)
            if end < 0:
                break
            attrs[key] = raw[cursor + 1:end]
            index = end + 1
        else:
            end = raw.find(",", cursor)
            if end < 0:
                attrs[key] = raw[cursor:].strip()
                break
            attrs[key] = raw[cursor:end].strip()
            index = end
        if index < len(raw) and raw[index] == ",":
            index += 1
    return attrs


def variant_from_master(master_text: str) -> Variant:
    """从已落盘的 master.m3u8 解析回 Variant，供字幕升级原样重写。

    BANDWIDTH 是实测分片峰值（bandwidth_from_segments 要把全部分片 stat
    一遍），给已有包加字幕轨时重算比打包还贵；解析旧 master 只需一次读
    文件。属性缺失或畸形直接抛错：宁可炸也不能把变体写残——AVPlayer 对
    残变体是整份拒收（黑屏、一个分片都不请求）。
    """
    lines = master_text.splitlines()
    inf = next((i for i, line in enumerate(lines)
                if line.startswith("#EXT-X-STREAM-INF:")), None)
    if inf is None:
        raise ValueError("master 里没有 #EXT-X-STREAM-INF 变体")
    if inf + 1 >= len(lines) or not lines[inf + 1].strip():
        raise ValueError("#EXT-X-STREAM-INF 后面没有变体 URI")
    attrs = parse_stream_inf(lines[inf])
    if "BANDWIDTH" not in attrs:
        raise ValueError("#EXT-X-STREAM-INF 缺 BANDWIDTH")
    resolution: tuple[int, int] | None = None
    if attrs.get("RESOLUTION"):
        try:
            width, height = attrs["RESOLUTION"].lower().split("x")
            resolution = (int(width), int(height))
        except ValueError:
            resolution = None
    frame_rate: float | None = None
    if attrs.get("FRAME-RATE"):
        try:
            frame_rate = float(attrs["FRAME-RATE"])
        except ValueError:
            frame_rate = None
    return Variant(
        uri=lines[inf + 1].strip(),
        bandwidth=int(attrs["BANDWIDTH"]),
        codecs=attrs.get("CODECS", ""),
        video_range=attrs.get("VIDEO-RANGE") or "PQ",
        resolution=resolution,
        frame_rate=frame_rate,
        hdcp_level=attrs.get("HDCP-LEVEL"),
        supplemental_codecs=attrs.get("SUPPLEMENTAL-CODECS"),
    )

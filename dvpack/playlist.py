"""master playlist：把探测出来的 DV 信令写成 Apple 要求的属性串。

ffmpeg 的 HLS 输出只给媒体列表，不带 `CODECS` / `VIDEO-RANGE`，而这两个属性正是
tvOS 决定要不要走杜比视界解码的入口。所以列表由我们自己写，分片和 init 段交给 ffmpeg。

依据是 Apple HLS Authoring Specification 的附录：`dvh1.<profile>.<level>` 两段都是两位
十进制（`dvh1.05.06` = Profile 5 Level 6），并且"兼容 brand 与 VIDEO-RANGE 互为交叉校验，
少写任一个都是错误的"。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from .probe import SourceInfo

# Apple 附录：db1p = 与 HDR10 兼容（对应 8.1），db4h = 与 HLG 兼容（对应 8.4）。
# Profile 5 按定义没有任何回退层，所以它不该带 SUPPLEMENTAL-CODECS。
_SUPPLEMENTAL_BY_RANGE = {"PQ": "db1p", "HLG": "db4h"}


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
        if self.supplemental_codecs:
            parts.append(f'SUPPLEMENTAL-CODECS="{self.supplemental_codecs}"')
        return "#EXT-X-STREAM-INF:" + ",".join(parts)


def supplemental_codecs(source: SourceInfo) -> str | None:
    dovi = source.dovi
    if dovi is None or dovi.profile != 8:
        return None
    brand = _SUPPLEMENTAL_BY_RANGE.get(source.video.apple_video_range)
    return f"{dovi.target_apple_codec},{brand}" if brand else None


def variant_from_source(
    source: SourceInfo,
    uri: str,
    *,
    bandwidth: int,
    hdcp_level: str = "TYPE-1",
) -> Variant:
    """DV 信令全部来自探测结果，不在这里二次推断。"""
    dovi = source.dovi
    if dovi is None:
        raise ValueError(f"{source.path} 没有 DOVI 记录，不该走 DV 打包")
    video = source.video
    return Variant(
        uri=uri,
        bandwidth=bandwidth,
        codecs=dovi.target_apple_codec,
        video_range=video.apple_video_range,
        resolution=(video.width, video.height) if video.width and video.height else None,
        frame_rate=video.frame_rate or None,
        hdcp_level=hdcp_level,
        supplemental_codecs=supplemental_codecs(source),
    )


def bandwidth_from_segments(segments: Iterable[Path], seconds: float, *, margin: float = 1.2) -> int:
    """实测带宽：产出分片的真实字节数 ÷ 时长，再留 20% 余量。

    用容器算出来的平均值会偏低（那是整片平均，不是峰值），Apple 要求 BANDWIDTH
    不低于分片峰值码率，写低了客户端会误判、起播就卡。
    """
    total = sum(path.stat().st_size for path in segments)
    if seconds <= 0:
        raise ValueError("时长必须为正")
    return int(total * 8 / seconds * margin)


def master_playlist(variants: Iterable[Variant], *, independent_segments: bool = True) -> str:
    lines = ["#EXTM3U", "#EXT-X-VERSION:7"]
    if independent_segments:
        lines.append("#EXT-X-INDEPENDENT-SEGMENTS")
    for variant in variants:
        lines += [variant.stream_inf(), variant.uri]
    return "\n".join(lines) + "\n"


def write_master_playlist(path: str | Path, variants: Iterable[Variant], **kwargs) -> Path:
    """用 write_bytes 落盘：文本模式在 Windows 上会把 `\n` 翻成 `\r\n`。

    两种换行 HLS 都接受，但钉死 LF 可以让产出的列表在跨平台比对时没有噪音。
    """
    target = Path(path)
    target.write_bytes(master_playlist(variants, **kwargs).encode("utf-8"))
    return target

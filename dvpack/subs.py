"""双语字幕：文本字幕 -> tvOS 能选的中/英两条 HLS 字幕轨。

源只认文本：Jellyfin 的文本字幕流、片库边车 .srt/.vtt、在线下载的 .srt/.vtt
（tools/subfetch.py 的抓取产物）。

产物（与 video.m3u8 同目录）：
  sub_zh.vtt / sub_en.vtt   整套文本，供 byte-range 切片
  sub_zh.m3u8 / sub_en.m3u8 HLS 字幕轨，切片全部指回同一个 .vtt

切片不新建小文件是硬约束：_out 常年 99% 满，为字幕再铺一堆碎文件等于和自己
抢空间（2026-10-03 现场 16Gi free）。
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence

SUBTITLES_GROUP = "subs"

# 双语口径：中/英两条轨都不自动选中。
#
# 2026-10-04：这里原来是 (("zh", "中文", True), ...)，DEFAULT=YES 让 AVPlayer
# 一解析 master 就自动选中中文字幕轨，后果全在真机上现形：
#   * AVPlayer 自己拉 sub_zh.m3u8，然后按 #EXT-X-BYTERANGE 一段段拉 sub_zh.vtt
#     ——28 分钟播放打了 475 次 sub_zh.vtt（服务端日志实测）。
#   * 系统字幕叠在 dvplay 自绘的双语字幕条上面，用户看到的是重影/位置错乱。
#   * 英文轨 DEFAULT=NO，AVPlayer 一次都没拉过 sub_en.m3u8（整份日志只有打包
#     时 localhost 的 3 次），上文说的"双语"实际是系统管中文、App 管英文。
# dvplay 不依赖系统字幕轨（它自己 fetch sub_zh.vtt / sub_en.vtt 自绘），所以
# 两条轨的 DEFAULT / AUTOSELECT 都置 NO：菜单里仍选得到，起播时不会自动加载。
BILINGUAL_TRACKS = (("zh", "中文", False), ("en", "English", False))

# cue 时间轴分隔符。两种小数点都认：WebVTT 用句点，字幕站点常见的逗号也要放行。
_ARROW = " --> "


def _stamp(text: str) -> float | None:
    """"00:01:02.345" -> 62.345；认不出就 None。"""
    text = text.strip()
    if not text:
        return None
    if "," in text:
        text = text.replace(",", ".")
    head, _, millis = text.partition(".")
    parts = head.split(":")
    if len(parts) != 3 or not all(p.strip().isdigit() for p in parts):
        return None
    if millis and not millis.isdigit():
        return None
    hours, minutes, seconds = (int(p) for p in parts)
    total = hours * 3600 + minutes * 60 + seconds
    if millis:
        total += int(millis.ljust(3, "0")[:3]) / 1000
    return float(total)


@dataclass(frozen=True)
class SubtitleTrack:
    """一条已经落盘的 HLS 字幕轨。"""

    language: str
    name: str
    vtt: Path
    default: bool = False

    @property
    def playlist_name(self) -> str:
        return f"sub_{self.language}.m3u8"

    @property
    def source_name(self) -> str:
        return f"sub_{self.language}.vtt"

    def media_line(self) -> str:
        """master 里的 #EXT-X-MEDIA，必须排在第一条 STREAM-INF 之前。"""
        default = "YES" if self.default else "NO"
        # AUTOSELECT 显式写 NO：不写时按 HLS 规范默认 YES，客户端（AVPlayer、
        # Infuse）仍会按 LANGUAGE 匹配用户偏好自动挂上，字幕轨照样被预加载。
        # 只有 DEFAULT/AUTOSELECT 同时为 NO，起播才真的不碰字幕轨。
        autoselect = "YES" if self.default else "NO"
        return ",".join([
            "#EXT-X-MEDIA:TYPE=SUBTITLES",
            f'GROUP-ID="{SUBTITLES_GROUP}"',
            f'LANGUAGE="{self.language}"',
            f'NAME="{self.name}"',
            f"DEFAULT={default}",
            f"AUTOSELECT={autoselect}",
            f'URI="{self.playlist_name}"',
        ])


def _cue_seconds(chunk: bytes) -> float:
    """一条 cue 块的时长（结束 - 开始）。取不到就 0。"""
    text = chunk.decode("utf-8", "replace")
    line = ""
    for raw in text.splitlines():
        if _ARROW in raw:
            line = raw
            break
    if not line:
        return 0.0
    left, _, right = line.partition(_ARROW)
    start, stop = _stamp(left), _stamp(right)
    if start is None or stop is None or stop <= start:
        return 0.0
    return stop - start


def vtt_chunks(data: bytes) -> list[tuple[int, int]]:
    """把 .vtt 切成 (offset, length)：header 一段，其后一条 cue 一段。

    切点落在 cue 块的起点（上一个空行之后），按返回的 span 拼回去与原文件
    逐字节一致。header 里的 STYLE/REGION 跟着第一条 cue 一起被 bytemap 指走，
    不会丢——offset 把原文件的边界钉死了。
    """
    spans: list[tuple[int, int]] = []
    pos = 0
    cue_at = data.find(_ARROW.encode("ascii"))
    if cue_at < 0:
        return [(0, len(data))] if data.strip() else []
    while pos < len(data):
        end = data.find(b"\n\n", pos)
        if end < 0:
            spans.append((pos, len(data)))
            break
        spans.append((pos, end + 1))
        pos = end + 2
        if pos > cue_at:
            break
    if pos < cue_at:
        pos = cue_at
    while pos < len(data):
        end = data.find(b"\n\n", pos)
        if end < 0:
            spans.append((pos, len(data)))
            break
        spans.append((pos, end + 1))
        pos = end + 2
    return [(start, stop) for start, stop in spans if stop > start]


def _normalize_timing_line(line: str) -> str:
    """SRT 时间戳行走 WebVTT：逗号小数点改句点，其余一个字符都不动。

    只动时间戳行——cue 正文里的逗号（对白、人名分隔）跟着行原样放行。
    """
    if _ARROW in line:
        return line.replace(",", ".")
    return line


def to_vtt(text: str) -> bytes:
    """SRT / WebVTT 文本统一成 WebVTT 字节：

    - 输出始终以 WEBVTT 头开始；输入自带头时不重复写，紧跟的空行也跳过
    - SRT 序号行丢掉；cue 正文里独立的数字不会被误伤
    - SRT 时间戳行的逗号小数点改成句点，cue 正文原样
    """
    body = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    if body and body[0].strip() == "WEBVTT":
        # 输入已经是 WebVTT：它在的头不重复写，紧跟的空行也跳过
        body = body[1:]
        while body and not body[0].strip():
            body = body[1:]
    out = ["WEBVTT", ""]
    for index, line in enumerate(body):
        if _ARROW in line:
            out.append(_normalize_timing_line(line))
            continue
        if line.strip().isdigit():
            # SRT 序号行：只认紧贴的下一行就是时间戳行的情形。cue 正文里独立的
            # 数字（比分、门牌）后面跟的是空行，不会被误伤。
            following = body[index + 1] if index + 1 < len(body) else ""
            if _ARROW in following:
                continue
        out.append(line)
    return ("\n".join(out).rstrip("\n") + "\n").encode("utf-8")


def subtitle_playlist_text(
    track: SubtitleTrack, vtt: Path, chunks: Sequence[tuple[int, int]]
) -> str:
    """字幕轨的 HLS 媒体列表：所有段落用 byte-range 指回同一个 .vtt。

    chunks 是 vtt_chunks 的 (start, stop) 绝对边界。第一个 cue 之前的内容
    （WEBVTT 头等）并进第一条 cue 的段：单独成段的话 EXTINF 只能是 0，真机对
    0 时长段的处理不稳（跳过或卡黑）。段之间隔着空行分隔符，段落自身仍是
    合法的 cue 块。
    """
    data = Path(vtt).read_bytes()
    arrow = _ARROW.encode("ascii")
    spans = [span for span in chunks if span[1] > span[0]]
    first_cue = None
    for index, (start, stop) in enumerate(spans):
        if arrow in data[start:stop]:
            first_cue = index
            break
    if first_cue is None:
        raise ValueError(f"{vtt} 里没有找到任何 cue 时间戳行")
    cue_start, cue_stop = spans[first_cue]
    segments = [(_cue_seconds(data[cue_start:cue_stop]), spans[0][0], cue_stop - spans[0][0])]
    for start, stop in spans[first_cue + 1 :]:
        segments.append((_cue_seconds(data[start:stop]), start, stop - start))
    segments = [segment for segment in segments if segment[0] > 0]
    if not segments:
        raise ValueError(f"{vtt} 里没有可用时长的 cue")
    lines = [
        "#EXTM3U",
        "#EXT-X-VERSION:7",
        f"#EXT-X-TARGETDURATION:{max(1, math.ceil(max(item[0] for item in segments)))}",
        "#EXT-X-MEDIA-SEQUENCE:0",
        "#EXT-X-PLAYLIST-TYPE:VOD",
    ]
    for duration, offset, length in segments:
        lines.append(f"#EXTINF:{duration:.3f},")
        lines.append(f"#EXT-X-BYTERANGE:{length}@{offset}")
        lines.append(track.source_name)
    lines.append("#EXT-X-ENDLIST")
    return "\n".join(lines) + "\n"


def write_subtitle_tracks(out_dir: str | Path, texts: Mapping[str, str]) -> list[SubtitleTrack]:
    """把文本字幕按 BILINGUAL_TRACKS 写成 HLS 中/英两条轨（与 video.m3u8 同目录）。

    缺语言或空文本就跳过该轨；两条都没内容返回 []。段全部 byte-range 指回同一个
    .vtt，不再铺碎文件——_out 常年九成满，字幕不该和自己抢空间。
    """
    base = Path(out_dir)
    tracks: list[SubtitleTrack] = []
    for language, name, default in BILINGUAL_TRACKS:
        text = (texts or {}).get(language, "")
        if not text or not text.strip():
            continue
        payload = to_vtt(text)
        vtt = base / f"sub_{language}.vtt"
        vtt.write_bytes(payload)
        track = SubtitleTrack(language=language, name=name, vtt=vtt, default=default)
        playlist = subtitle_playlist_text(track, vtt, vtt_chunks(payload))
        (base / track.playlist_name).write_bytes(playlist.encode("utf-8"))
        tracks.append(track)
    return tracks


def master_has_subtitle_tracks(master_path: str | Path) -> bool:
    """master 里已经有 #EXT-X-MEDIA:TYPE=SUBTITLES 了吗。"""
    try:
        text = Path(master_path).read_text(encoding="utf-8")
    except OSError:
        return False
    return "TYPE=SUBTITLES" in text


def upgrade_master_subtitles(
    out_dir: str | Path, texts: Mapping[str, str]
) -> list[SubtitleTrack]:
    """给已在播的包补字幕轨：只重写 master + 落字幕文件，视频分片一个字节都不碰。

    在线腿/字幕缓存命中时用。重新打包要分钟级（34 分钟的片子 2~3 分钟，
    AVPlayer 等 302 超时即「点片没反应」），而加字幕轨本质只是 master 里几条
    #EXT-X-MEDIA 加一句 SUBTITLES="subs"——AVPlayer 下次重载 playlist
    （约每 13s）就认新轨。

    变体属性从旧 master 原样解析回写（variant_from_master）：BANDWIDTH 是
    实测分片峰值，重算要把全部分片 stat 一遍，比打包还贵。

    已在播保护：master 已有字幕轨就原样返回 []（别把 DVplay 自绘双语的时间
    线又翻一遍）；texts 空同样返回 []。抛异常的只有畸形 master——那本来就没
    法安全重写。
    """
    from .playlist import variant_from_master

    out_dir = Path(out_dir)
    master = out_dir / "master.m3u8"
    if master_has_subtitle_tracks(master):
        return []
    variant = variant_from_master(master.read_text(encoding="utf-8"))
    tracks = write_subtitle_tracks(out_dir, texts)
    if not tracks:
        return []
    # 原子替换复用打包同一条路：直接写会把 AVPlayer 正在读的 master 冲成
    # 0 字节（ENOSPC 现场，见 playlist.write_master_playlist）。
    from .playlist import write_master_playlist

    write_master_playlist(master, [variant], subtitles=tracks)
    return tracks

"""remux：MKV → CMAF/fMP4 + HLS，产出后自己校验 DV 信令。

不重编码。三条实测事实决定了这里的 flag（ffmpeg 实测，P5 L6 片源）：

  1. `-c copy` 完整保留带内 RPU——源与产出都是 1443 条 type-62 NAL，remux 只改长度前缀。
  2. 默认 sample entry 是 `hev1`；`-tag:v dvh1` 才改成 `dvh1`（Apple 要求的写法）。
  3. 光改 fourcc 不够：不给 `-strict unofficial`，ffmpeg 会明确拒绝写配置 box
     （stderr: "Not writing 'dvcC'/'dvvC' box. Requires -strict unofficial."）。

两个 flag 一起给，init 段里就是 `dvh1` + 配置 box，ffprobe 复读出的 DoviRecord 与源逐字段相同。
Shaka Packager 3.9.3 走同一条路（先 ffmpeg 打标再 packager）产出的 init 段与此逐字节同构，
所以没必要引入第二个打包器。
"""

from __future__ import annotations

import json
import re
import struct
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .bmff import hvcC_param_set_types, parse_file
from .p5 import resign_pack
from .probe import DoviRecord, SourceInfo, mvp_verdict, probe
from .tools import FFPROBE, FFMPEG, ffmpeg_timeout, require

# 见模块 docstring 第 2、3 条：缺任一条都不会得到合法 DV 信令
DV_VIDEO_FLAGS = ("-tag:v", "dvh1", "-strict", "unofficial")

# init 段的色彩信令必须全部留白：AVPlayer 的 Dolby Vision 通路要求容器与
# SPS VUI 都不抢着描述颜色，把色彩管理完全交给带内 RPU。2026-09-27 与
# Apple 官方 DV 参考流拆出的 4k/init.mp4 逐字段对比证实：
#   Apple：color_range=tv，color_space/transfer/primaries 均 unknown，
#          stsd 无 colr 盒，SPS VUI 不写 colour_description。
#   dvpack 旧产物：color_range=pc + 显式 bt2020/smpte2084/bt2020nc。
# 显式 full-range + 显式 PQ 会把 AVPlayer 推到 SDR/HDR 直通路径，不进 DV
# 专属 tone-mapping，真机症状即「Dolby Vision 徽标不亮 + 颜色发灰/发绿」。
# Apple 敢留白是因为 P5 按定义就是 RPU 独占色彩管理，BL 只当容器。
# 这里的常量仅供 dvassert/报告引用，不再作为 ffmpeg 参数下发。
DV_P5_COLORIMETRY = ("bt2020", "smpte2084", "bt2020nc")
DV_LEAVE_COLORIMETRY_UNSPECIFIED = False


def colorimetry_flags(
    colorimetry: tuple[str, str, str] | None = None,
    *,
    full_range: int = 0,
) -> tuple[str, ...]:
    """让 fmp4 muxer 把 colour 信息写进 init 段 stsd 的 colr 盒（nclx 形式）。

    colorimetry 为 (primaries, transfer, space)。

    DV 链路一律返回空 tuple：2026-09-27 与 Apple 官方 DV 参考
    （_out/apple_ref/4k/init.mp4）逐字段对比确认，容器层显式签 any-range
    都会把 AVPlayer 推离 DV 专属 tone-mapping 通路，真机即「Dolby Vision
    徽标不亮 + 颜色发灰/发绿」。Apple 那份参考 color_range=tv 但
    space/transfer/primaries 全 unknown、stsd 里也没有 colr 盒——FFmpeg
    侧的等效做法就是「一个 color_* 标志都不给」：muxer 不写 colr，
    SPS VUI 不写 colour_description，色彩完全交给带内 RPU。

    colorimetry/full_range 形参保留，供 dvassert 与 p7_to_p5 的受限场景
    显式传入、绕过留白策略。
    """
    if DV_LEAVE_COLORIMETRY_UNSPECIFIED and colorimetry is None:
        # color 三件套留白（见 DV_LEAVE_COLORIMETRY_UNSPECIFIED 注释）；
        # range 位显式钉 tv：实测源 MKV 的 color_range=pc 会被 -c copy
        # 一路透传进 init（ffprobe 报 color_range=pc），而 Apple 官方 DV
        # 参考 4k/init.mp4 是 color_range=tv + 三件套全 unknown。DV 链路
        # 只需要把 pc 拉回 tv，不签任何色彩描述。
        return ("-color_range", "tv")

    if colorimetry is None:
        colorimetry = DV_P5_COLORIMETRY
    return (
        "-color_primaries",
        colorimetry[0],
        "-color_trc",
        colorimetry[1],
        "-colorspace",
        colorimetry[2],
        "-color_range",
        "pc" if full_range else "tv",
    )

# DV Profile 5 的 BL 按 Dolby 规范就是 limited（16-235），所以打包一律签 tv。
# 2026-09-27 真机实证：点亮的 534f pack 是 tv + 显式 PQ；把源容器里的 pc 透传
# 进 SPS（-color_range pc）时徽标不亮。full-range 的 DV BL 极罕见，为它保留
# 的「按源推导」分支已经害过真机，删掉。
_FULL_RANGE_TOKENS = frozenset({"never-match-anymore"})


def source_full_range(source: SourceInfo) -> int:
    """源容器的 color_range → SPS VUI video_full_range_flag（0/1）。

    恒定回 0（limited）：DV Profile 5 的 BL 按 Dolby 规范就是 limited，
    2026-09-27 真机实证——把源容器 pc 透传进 SPS（-color_range pc）时
    徽标不亮，full-range 的 DV BL 极罕见，「按源推导」分支已删。
    色彩口径全交给带内 RPU（见 dv_sps_vui_bsf 注释）。形参 source 保留，
    供 dvassert 与 p7_to_p5 的受限场景保持同构。
    """
    return 1 if source.video.color_range.lower() in _FULL_RANGE_TOKENS else 0


# MKV 源的 extradata 只有 hvcC 头：Jellyfin 这条链 2026-09-26 实测 CodecPrivate
# 23 字节（numOfArrays=0）→ `-c copy` 无参数集可抄 → init 段 hvcC 空着，
# VPS/SPS/PPS 全走分片 in-band。Apple HLS/fMP4 要求 init 的 hvcC 自带参数集：
# 缺了就"播了但立刻失败"（真机：拉完 init.mp4 200 + video0.m4s 200 后，
# 00:26:05 起再无任何请求，ConnectionResetError 是电视主动断开）。
# 补法走 libavcodec 现成的两个 BSF 模块，不重编码、不动时间戳：先把长度前缀
# NAL 换成 Annex-B start code，再让 extract_extradata 从首分片扫出参数集写回
# 流 extradata，muxer 随后把完整 hvcC 抄进 init 段（实测 23B → 183B）。
# 三条死路别再走：MKV→裸 HEVC→fmp4 两步法（hvcC 对但时间戳死成
# TARGETDURATION:0）；单独喂 extract_extradata（"No start code is found"，
# 该模块只认 Annex-B）；多个 -bsf 参数（ffmpeg 只保留最后一个，必须逗号成链）。
PARAM_SET_INJECTION_BSF = ("hevc_mp4toannexb", "extract_extradata")
PARAM_SET_BSF_FORMATS = ("matroska",)

# SPS VUI 的颜色/range 重写交给 ffmpeg 自带的 hevc_metadata 模块（2026-09-26
# 实测本机 ffmpeg 8.1.2 可写 full_range 与三件套），不再手搓位翻转：
# 颜色三件套固定 BT.2020/PQ/BT.2020nc；range 位随源走（dv_sps_vui_bsf）。
# 源 mkv 的 SPS primaries=2/transfer=2，范围位也未必与容器一致，直通的
# 产物真机症状「颜色明显不对 + DV 徽标不亮」。口径出处：Apple 官方 DV P5
# 参考 init 无 colr、SPS 无 VUI 颜色，ffprobe 报 color_range=tv + master
# VIDEO-RANGE=PQ——但那份参考本身是 limited 源，limited 只对 limited 源
# 成立。PTL 高位（tier/non_packed）hevc_metadata 改不了，归 dvpack.dplus。
def dv_sps_vui_bsf(full_range: int = 0) -> tuple[str, ...]:
    """SPS VUI 信令 BSF：显式签 BT.2020/PQ/BT.2020nc + limited range。

    2026-09-27 真机 A/B 实证：
      · 点亮  SPS VUI 016a 12 20 12，ffprobe tv + bt2020nc/smpte2084/bt2020
      · 不亮  SPS VUI 016a 04 04 04，ffprobe tv + 全 unknown
    显式 colour_description_present_flag 是点火条件，不是阻碍——之前照
    Apple 参考「留白」的假设被证伪。range 恒签 0（limited）：DV P5 的 BL
    按 Dolby 规范就是 16-235，源容器里的 pc 透传进来会让徽标灭。
    """
    return (
        "hevc_metadata=video_full_range_flag=0:"
        "colour_primaries=9:transfer_characteristics=16:matrix_coefficients=9",
    )


# 兼容常量（limited 口径）：scripts/p7_to_p5.py 按 P7 源引用——P7 BL 本就
# limited。新代码一律走 dv_sps_vui_bsf(source_full_range(source))。
DV_SPS_VUI_BSF = dv_sps_vui_bsf()


def param_set_injection_bsf(source: SourceInfo) -> tuple[str, ...]:
    """源容器的 extradata 不带参数集时返回注入链，否则空链。

    matroska（MKV rip，Jellyfin 主场景）需要；mp4/mov 源自带参数集，
    不走这条，无谓改写 init 布局。
    """
    fmt = source.format_name.lower()
    if any(name in fmt for name in PARAM_SET_BSF_FORMATS):
        return PARAM_SET_INJECTION_BSF
    return ()


def dv_signalling_bsf(
    source: SourceInfo, full_range: int | None = None
) -> tuple[str, ...]:
    """HEVC -c copy 链路的 DV 信令规范化 BSF：VUI 颜色重写成 P5 口径。

    range 位随源：full_range=None 时由 source_full_range 从容器 color_range
    推导（2026-09-26：Jellyfin 全色域源三证齐全，见 source_full_range）。

    MKV 源的 extradata 只有 hvcC 头，得先把长度前缀 NAL 换 Annex-B、再把
    （重写后的）参数集抽回 extradata，init 段 hvcC 才拿得到正确的 SPS；
    mp4/mov 源自带参数集，直接挂 hevc_metadata 即可。
    """
    if full_range is None:
        full_range = source_full_range(source)
    vui = dv_sps_vui_bsf(full_range)
    fmt = source.format_name.lower()
    if any(name in fmt for name in PARAM_SET_BSF_FORMATS):
        return ("hevc_mp4toannexb", *vui, "extract_extradata")
    return vui


# 画面 + 音频一起封装（音视频混在同一条 fMP4 流里，不做 EXT-X-MEDIA 分离）。
#
# 历史：这里曾是 VIDEO_ONLY_MAPS = ("-map", "0:v:0")，理由是「E-AC3 进 fMP4 的
# HLS 兼容性没实测，带进去会让徽标归因变困难」。但纯视频轨＝**电视上完全没声音**，
# 而实测 Jellyfin 常见 DV 源的音轨是 AAC/AC-3，都能直接 copy 进 fMP4。
# 音视频同流（muxed）无需 EXT-X-MEDIA 声明，是最省事也最兼容的写法。
VIDEO_AUDIO_MAPS = ("-map", "0:v:0", "-map", "0:a:0?")
# 兼容旧调用点（只要画面）。
VIDEO_ONLY_MAPS = ("-map", "0:v:0")


def audio_pack_mode(source: SourceInfo, maps: tuple[str, ...]):
    """音轨打包口径，返回 (maps, declared_audio_codec, transcode)。

    master 的 CODECS 必须和实际 init/media 逐字一致——声明了媒体里没有的
    编码（2026-09-27：写死 ec-3 而源是 dts）或反过来（媒体里有轨却不在
    CODECS 里声明），AVPlayer 都直接拒收整个 master：真机黑屏、日志里只有
    一次 master.m3u8、init.mp4 一次都不请求。

    · maps 不含音频（客户端 audio=0）→ 纯视频包，不声明编码；
    · 源音轨编码 Apple 认识（aac/ac-3/ec-3/flac）→ 原样 copy，声明源编码；
    · 有轨但不认识（dts/truehd…）→ Apple 平台解不了，转码 aac 5.1 保住
       声音，master 声明 mp4a.40.2，init/media 里就是 AAC。2026-10-03 狂飙
      ab476f 真机复现：dts 原轨进包即黑屏 + 红色禁止图标。
    """
    if not any(m.endswith("0:a:0?") or m.endswith("0:a:0") for m in maps):
        return maps, "", False
    if source.audio_codec:
        return maps, source.audio_codec, False
    if not source.audio_codec_name:
        return maps, "", False
    return maps, "mp4a.40.2", True

# 时间轴规整：真机「黑屏卡住」的根因（2026-09-27）。
#
# 源的 DTS 非单调（remux 一直报 "Non-monotonic DTS; previous: 124, current: 83"），
# `-c copy` 又原样搬进分片，于是：
#   * 末段被切出只有 3 帧（EXTINF:0.060000）的残片，TARGETDURATION 却是 6；
#   * 该片内 DTS 乱序（60.060 → 60.220 → 60.140）。
# AVPlayer 读到这种分片会停在一个无法呈现的缓冲区上 = 黑屏、不再发请求。
#
# 修法用 setts 位流过滤器把时间戳重写成干净的 CFR 序列——纯时间戳操作，
# **不重编码像素**（DV 必须保持 -c copy，重编码会毁掉 RPU）。
# time_base 取源的帧率（50fps 源 → 1/50），ts=N 逐帧递增，duration=1 定长。
def setts_bsf(source: SourceInfo) -> str:
    """保留真实帧间隔，只把 PTS/DTS 平移到零点。

    旧实现 `setts=time_base=1/fps:ts=N:duration=1` 会把每帧时间重写成序号，
    HLS muxer 因此失去关键帧时间戳，60s 输入退化成 1 个 100MB+ 分片。
    直接不修又会暴露源时间戳里的重复 DTS，ffmpeg 解码整个 playlist 时报
    `non monotonically increasing dts`。2026-09-27 A/B 实测，
    `PTS-STARTPTS / DTS-STARTDTS` 同时满足两个条件：保留真实帧间隔与关键帧
    位置（11 个分片），并把时间轴归零成严格单调。
    """
    return "setts=pts=PTS-STARTPTS:dts=DTS-STARTDTS"


class RemuxError(Exception):
    pass


@dataclass(frozen=True)
class SegmentTimestampProblem:
    """产出分片的时间轴问题（Infuse FCPTSIncorrectnessDetector 的离线对应物）。

    只记录事实与位置；修复属于 setts/时间轴规整层。verify() 的 ffprobe 复读
    在后面，这一步用纯 ISOBMFF 解析把"ffmpeg 正常退出但时间轴已坏"的产物
    提前拦下，省一次端到端校验。
    """

    segment: Path
    field: str
    expected: int
    actual: int

    def describe(self) -> str:
        return (
            f"{self.segment.name} {self.field}：期望 {self.expected}，实得 {self.actual}"
        )


def _boxes(data: bytes, start: int, end: int):
    """在 [start, end) 内产出 (type, box_start, body_start, box_end)。"""
    pos = start
    while pos + 8 <= end:
        size = int.from_bytes(data[pos : pos + 4], "big")
        if size == 1:
            if pos + 16 > end:
                return
            size = int.from_bytes(data[pos + 8 : pos + 16], "big")
        elif size == 0:
            size = end - pos
        if size < 8 or pos + size > end:
            return
        btype = data[pos + 4 : pos + 8].decode("latin-1", "replace")
        yield btype, pos, pos + 8, pos + size
        pos += size


def _find_box(data: bytes, name: str, start: int = 0, end: int | None = None):
    for btype, at, body, stop in _boxes(data, start, end if end is not None else len(data)):
        if btype == name:
            return at, body, stop
    return None


def _segment_timeline(segment: Path) -> tuple[int, list[tuple[int, int]]] | None:
    """读一个 .m4s 的 (tfdt 基准时间, [(sample_index, duration), ...])。

    tfdt 只有一个：dvpack 的产物是单轨分片（音视频同流走一个 track）。trun
    的 sample 表按 ISO 14496-12 §8.8.8 位域展开。
    """
    data = segment.read_bytes()
    moof = _find_box(data, "moof")
    if moof is None:
        return None
    _, moof_body, moof_end = moof
    traf = _find_box(data, "traf", moof_body, moof_end)
    if traf is None:
        return None
    _, traf_body, traf_end = traf
    tfdt = _find_box(data, "tfdt", traf_body, traf_end)
    trun = _find_box(data, "trun", traf_body, traf_end)
    if tfdt is None or trun is None:
        return None
    _, tfdt_body, _ = tfdt
    version = data[tfdt_body]
    if version == 1:
        if len(data) < tfdt_body + 12:
            return None
        base_time = int.from_bytes(data[tfdt_body + 4 : tfdt_body + 12], "big")
    else:
        if len(data) < tfdt_body + 8:
            return None
        base_time = int.from_bytes(data[tfdt_body + 4 : tfdt_body + 8], "big")

    _, trun_body, _ = trun
    flags = int.from_bytes(data[trun_body + 1 : trun_body + 4], "big")
    count = int.from_bytes(data[trun_body + 4 : trun_body + 8], "big")
    cursor = trun_body + 8
    per_sample = 0
    if flags & 0x000001:
        cursor += 4
    if flags & 0x000004:
        cursor += 4
    if flags & 0x000100:
        per_sample += 4
    if flags & 0x000200:
        per_sample += 4
    if flags & 0x000400:
        per_sample += 4
    if flags & 0x000800:
        per_sample += 4
    if not flags & 0x000100:
        # 无逐帧 duration（Apple 官方参考分片就是这种形态：duration 走
        # trex 默认值）。帧级校验做不了，但段级 tfdt 校验仍然可行——
        # 返回空 sample 表，让调用方只按 base_time 比较相邻分片。
        return base_time, []
    samples: list[tuple[int, int]] = []
    for index in range(count):
        at = cursor + index * per_sample
        if at + 4 > len(data):
            return None
        samples.append((index, int.from_bytes(data[at : at + 4], "big")))
    return base_time, samples


def validate_segments(output: RemuxOutput) -> tuple[SegmentTimestampProblem, ...]:
    """产出分片静态预检：tfdt 必须沿分片序列递增，trun duration 必须为正。

    这两层各抓一种真机事故：tfdt 回退会让 AVPlayer 停在无法呈现的缓冲区
    （黑屏不再请求）；duration=0/越界负值对应 ffmpeg 报 non-monotonic DTS
    后切出的残片。静态检测发生在 ffprobe 复读之前，坏包在转签与上电视前
    就被拒绝。
    """
    problems: list[SegmentTimestampProblem] = []
    previous_end: int | None = None
    for segment in output.segments:
        timeline = _segment_timeline(segment)
        if timeline is None:
            continue
        base_time, samples = timeline
        if previous_end is not None and base_time < previous_end:
            problems.append(
                SegmentTimestampProblem(
                    segment=segment,
                    field="tfdt baseMediaDecodeTime 回退",
                    expected=previous_end,
                    actual=base_time,
                )
            )
        total = base_time
        for index, duration in samples:
            if duration <= 0:
                problems.append(
                    SegmentTimestampProblem(
                        segment=segment,
                        field=f"sample#{index} duration",
                        expected=1,
                        actual=duration,
                    )
                )
                continue
            total += duration
        previous_end = total
    return tuple(problems)


@dataclass(frozen=True)
class RemuxOutput:
    playlist: Path
    init: Path
    segments: tuple[Path, ...]

    @property
    def exists(self) -> bool:
        return self.playlist.exists() and self.init.exists() and bool(self.segments)


@dataclass(frozen=True)
class Verification:
    """产出自检：能不能拿这条链路的结果去点电视。"""

    ok: bool
    signalling: str
    source_dovi: DoviRecord | None
    output_dovi: DoviRecord | None
    problems: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()
    target_dovi: DoviRecord | None = None

    @property
    def dovi_preserved(self) -> bool:
        return self.source_dovi is not None and self.source_dovi == self.output_dovi

    @property
    def dovi_relabelled(self) -> bool:
        """转签是否真的落地：产出信令 == 目标信令（P5），而不是"和源一样"。"""
        return self.target_dovi is not None and self.target_dovi == self.output_dovi


def hls_command(
    src: Path,
    playlist: Path,
    *,
    seconds: float | None = None,
    segment_seconds: float = 6.0,
    maps: tuple[str, ...] = VIDEO_AUDIO_MAPS,
    colorimetry: tuple[str, str, str] | None = None,
    full_range: int = 0,
    bsf: tuple[str, ...] | None = None,
    setts: str | None = None,
    transcode_audio: bool = False,
) -> list[str]:
    """拼 ffmpeg argv。

    `bsf` 与 `setts` 会被并进**同一条** -bsf:v 逗号链：多个 -bsf 参数时 ffmpeg
    只保留最后一个（实测），必须合成一条。`setts` 排最后：它只改时间戳，
    放前面不影响参数集注入与 VUI 重写。
    """
    argv = [str(require(FFMPEG)), "-hide_banner", "-loglevel", "error", "-y"]
    if seconds:
        argv += ["-t", str(seconds)]
    argv += [
        "-i",
        str(src),
        *maps,
        "-c",
        "copy",
        *DV_VIDEO_FLAGS,
        *colorimetry_flags(colorimetry, full_range=full_range),
    ]
    if bsf or setts:
        # -bsf:v 只吃一条链：多个 -bsf 参数 ffmpeg 只保留最后一个（2026-09-26 实测），
        # 所以 bsf 与 setts 必须合成一条。setts 排最后（只改时间戳，不影响前者）。
        chain = list(bsf or ())
        if setts:
            chain.append(setts)
        argv += ["-bsf:v", ",".join(chain)]
    if any(m == "0:a:0?" for m in maps):
        # 音视频同流：音频显式声明 codec（-c copy 已覆盖常规情形）。源音轨
        # 编码 Apple 不认（dts 等）时 transcode_audio=True：转码 aac 5.1，
        # master 同步声明 aac，信令与产物同源，不会出现「master 声明 ec-3
        # 而媒体里没有轨」那类整包拒收。实测 ffmpeg 8.1.2 dts→aac 6ch 直通。
        argv += ["-c:a", "aac" if transcode_audio else "copy"]
        if transcode_audio:
            argv += ["-b:a", "384k"]
    argv += [
        "-f",
        "hls",
        "-hls_segment_type",
        "fmp4",
        # 绝不能给 -hls_flags independent_segments：2026-09-27 实测该 flag 在
        # fmp4 模式下会打乱 hls_time 的分段决策，60s 输入只产出 1 个 100MB+
        # 分片（真机：Apple TV 拉完 video.m3u8 后既不请求 init.mp4 也不再
        # 拉分片，直接黑屏）。去掉后恢复每关键帧切（14.389/2.837/2.669…）。
        # 列表里的 #EXT-X-INDEPENDENT-SEGMENTS 由 fmp4 的每分片自带
        # 独立 traf 自然成立，不需要这个 flag。
        "-hls_time",
        str(segment_seconds),
        "-hls_list_size",
        "0",
        # VOD 口径（Apple 参考流同款 #EXT-X-PLAYLIST-TYPE:VOD）：定长包
        # 明示没有更多分片，AVPlayer 不会守着列表等追加。
        "-hls_playlist_type",
        "vod",
        str(playlist),
    ]
    return argv


def _run(
    argv: list[str], cwd: Path | None = None, timeout: float | None = None
) -> subprocess.CompletedProcess:
    """跑 ffmpeg/ffprobe，带执行上限（见 tools.ffmpeg_timeout）。

    timeout=None 走全局限；调用方知道时长语义时显式传（打包链路传 seconds
    派生值）。超时 → RemuxError：serve 的 PACK_ERRORS 捕获后回 502 并让
    锁自然释放，不再把同 item 的后续点击永久堵死。
    """
    cap = timeout if timeout is not None else ffmpeg_timeout()
    try:
        return subprocess.run(
            argv,
            capture_output=True,
            text=True,
            encoding="utf-8",
            cwd=cwd,
            timeout=cap,
        )
    except subprocess.TimeoutExpired as exc:
        head = " ".join(str(a) for a in argv[:3])
        raise RemuxError(
            f"ffmpeg/ffprobe 执行超时（>{cap:.0f}s 已杀）：{head}……"
            "（上游流卡死或机器过载；锁已释放，可重试）"
        ) from exc


# Profile 5 单层的 RPU 必须自洽：不能带 NLQ，且残差（增强层）必须声明禁用。
# 2026-09-25 实测：P7 源（Halo）丢 EL 后 RPU 仍是 nlq=linear_dz、
# disable_residual_flag=0——伪 P5，tvOS 拒播。P8 源是 nlq=none、
# disable_residual=1，真单层自洽。这两个字段是"重签不自洽"的可观测代理。
_SELF_CONSISTENT_NLQ = "none"


def check_rpu_consistency(output: RemuxOutput, *, frames: int = 4) -> tuple[str, ...]:
    """读前几帧的 RPU 侧数据，判断单层 P5 的 RPU 是否自洽。返回问题列表。

    只取前几帧：DV 流的 RPU 参数逐帧相同的情况占绝大多数，真逐帧不同时
    至少能抓到 IDR 那一帧；为省时间不做全片扫描。
    """
    argv = [
        str(require(FFPROBE)),
        "-v",
        "quiet",
        "-select_streams",
        "v:0",
        "-show_frames",
        "-show_entries",
        "frame=side_data_list",
        "-read_intervals",
        f"%+#{frames}",
        "-of",
        "json",
        str(output.playlist),
    ]
    proc = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8")
    if proc.returncode != 0:
        return (f"ffprobe 读分片 RPU 失败：{proc.stderr.strip() or '未知错误'}",)

    problems: list[str] = []
    seen: set[tuple[object, object]] = set()
    try:
        frames_json = json.loads(proc.stdout).get("frames") or []
    except json.JSONDecodeError:
        return ("分片 RPU 侧数据不是合法 JSON",)

    for frame in frames_json:
        for sidedata in frame.get("side_data_list") or []:
            if sidedata.get("side_data_type") != "Dolby Vision Metadata":
                continue
            nlq = sidedata.get("nlq_method_idc_name")
            residual = sidedata.get("disable_residual_flag")
            if (nlq, residual) in seen:
                continue
            seen.add((nlq, residual))
            if nlq is not None and nlq != _SELF_CONSISTENT_NLQ:
                problems.append(
                    f"RPU 带 NLQ（{nlq}）：Profile 5 单层不能有 NLQ"
                )
            if residual is not None and not int(residual):
                problems.append(
                    "RPU 未禁用残差（disable_residual_flag=0）：码流缺增强层，"
                    "RPU 与码流不自洽，tvOS 会拒播"
                )
    return tuple(problems)


def resolve_output(playlist: Path) -> RemuxOutput:
    """从媒体列表里读 init 段与分片，不猜文件名。

    ffmpeg 的分片名跟着列表名走，init 段却固定叫 `init.mp4`——猜名字会在多码率时撞车。
    """
    text = playlist.read_text(encoding="utf-8")
    match = re.search(r'#EXT-X-MAP:URI="([^"]+)"', text)
    if not match:
        raise RemuxError(f"{playlist.name} 里没有 EXT-X-MAP，不是 fMP4 媒体列表")
    uris = re.findall(r"^(?!#)[^#].+$", text, flags=re.MULTILINE)
    return RemuxOutput(
        playlist=playlist,
        init=playlist.parent / match.group(1),
        segments=tuple(playlist.parent / uri for uri in uris),
    )


def trim_stub_tail(output: RemuxOutput, *, min_seconds: float = 1.0) -> RemuxOutput:
    """删掉片尾的「残尾分片」并重写列表。

    真机黑屏的第二半：末段经常只剩 0.06s（1~3 帧）而 TARGETDURATION=6。就算
    时间戳已被 setts 规整成单调，这种片尾仍可能让 AVPlayer 在一个几乎没有
    可呈现内容的缓冲区上停住。60s 预览里丢掉最后 0.06s 无感知，但能把这个
    风险彻底摘掉。

    只从**尾部**丢，且保底留一段——中间分片永不动。
    """
    text = output.playlist.read_text()
    # 逐行扫：EXTINF 与紧随其后的 URI 成对，其余行（头、MAP、ENDLIST）原样保留。
    lines = text.splitlines(keepends=True)
    entries: list[tuple[int, float, str]] = []  # (extinf 行号, 时长, URI)
    for i, line in enumerate(lines):
        if line.startswith("#EXTINF:"):
            uri = next((lines[j] for j in range(i + 1, len(lines))
                        if not lines[j].startswith("#")), "")
            entries.append((i, float(line[len("#EXTINF:"):].split(",")[0]), uri.strip()))
    if len(entries) < 2:
        return output

    drop = 0
    for idx in range(len(entries) - 1, 0, -1):  # idx==0 保底：首段永不丢
        if entries[idx][1] >= min_seconds:
            break
        drop += 1
    if not drop:
        return output

    doomed = entries[len(entries) - drop:]
    keep_extinf_lines = {e[0] for e in entries[: len(entries) - drop]}
    drop_uris = {e[2] for e in doomed}
    out_lines = []
    skip_next_uri = False
    for i, line in enumerate(lines):
        if line.startswith("#EXTINF:") and i not in keep_extinf_lines:
            skip_next_uri = True
            continue
        if skip_next_uri and not line.startswith("#"):
            skip_next_uri = False
            continue
        out_lines.append(line)
    output.playlist.write_text("".join(out_lines))
    for uri in drop_uris:
        target = output.playlist.parent / uri
        if target.is_file():
            target.unlink()
    return RemuxOutput(
        playlist=output.playlist,
        init=output.init,
        segments=tuple(s for s in output.segments if s.name not in drop_uris),
    )


def remux(
    src: str | Path,
    out_dir: str | Path,
    *,
    seconds: float | None = None,
    segment_seconds: float = 6.0,
    maps: tuple[str, ...] = VIDEO_AUDIO_MAPS,
    name: str = "video",
    normalize_timestamps: bool = True,
) -> tuple[SourceInfo, RemuxOutput]:
    """探测 → 闸门 → remux → 定位产出。闸门不过就不动 ffmpeg。

    normalize_timestamps（默认开）会用 setts 把时间戳规整成 CFR —— 这是真机
    黑屏的根因修复：源 DTS 非单调时，-c copy 会切出 3 帧残片且片内乱序，
    AVPlayer 直接停播。关掉它只用于复现旧行为。
    """
    source = probe(src)
    verdict = mvp_verdict(source)
    if not verdict.supported:
        raise RemuxError(f"拒绝打包：{verdict.reason}")

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    # ffmpeg 的 HLS muxer 里，分片跟着列表路径走，init 段却按**当前目录**落盘
    # （实测：列表给 _out/spike/x.m3u8 时 init.mp4 掉进了项目根目录）。
    # 把 cwd 钉在 out_dir 上，两种文件才会落在一起，列表里的相对 URI 也才成立。
    playlist = Path(f"{name}.m3u8")
    proc = _run(
        hls_command(
            Path(src),
            playlist,
            seconds=seconds,
            segment_seconds=segment_seconds,
            maps=maps,
            bsf=param_set_injection_bsf(source),
            setts=setts_bsf(source) if normalize_timestamps else None,
        ),
        cwd=out_dir,
        timeout=ffmpeg_timeout(seconds),
    )
    if not (out_dir / playlist).exists():
        raise RemuxError(f"ffmpeg 未产出列表：{proc.stderr.strip() or '未知错误'}")

    output = resolve_output(out_dir / playlist)
    if not output.exists:
        raise RemuxError(f"产出不完整：init 或分片缺失（{proc.stderr.strip()}）")
    # 残尾分片（末段仅 0.06s / 1~3 帧）先摘掉再转签：少一个让 AVPlayer
    # 停住的黑屏风险源，代价是丢掉预览最后几十毫秒。
    output = trim_stub_tail(output)

    # 静态时间轴预检（Infuse FCPTSIncorrectnessDetector 思路）：在转签与
    # ffprobe 复读之前用纯盒链解析抓 tfdt 回退 / 零 duration。坏包在这里
    # 拒绝，不进后续任何一步。
    problems = validate_segments(output)
    if problems:
        head = "；".join(p.describe() for p in problems[:3])
        raise RemuxError(f"时间轴静态预检失败：{head}")

    # Apple HLS §1.9 只认 Profile 5：源是 P8 单层时把 init 段转签成 P5。
    # 只动配置 box 签名与 profile/level/compat，分片里的 RPU 一个字节都不碰。
    resign_pack(output.init, source.dovi)
    return source, output


def verify(source: SourceInfo, output: RemuxOutput, *, target: DoviRecord | None = None) -> Verification:
    """独立判据：读 init 段的 box 结构，再用 ffprobe 复读配置记录比对**目标**。

    target 默认等于源信令，所以没转签的通路行为和以前一致；转签后传进来的
    是 profile5_target，比对的就是"产出是否真成了 P5 + dvcC"。
    """
    expected = target if target is not None else source.dovi
    problems: list[str] = []
    report = parse_file(output.init)
    video = report.video
    signalling = video.signalling if video else "init 段里没有视频轨"
    if video is None:
        problems.append(signalling)
    else:
        if not video.declares_dolby_vision:
            problems.append(f"sample entry 是 {video.sample_entry_type}，不是 dvh1")
        if not video.has_dvvC:
            problems.append("缺 DV 配置 box：电视只会当普通 HDR 放")
        elif not video.has_dvcC:
            problems.append(
                f"配置 box 是俗名 {video.dv_box}：ffprobe 读得出，tvOS 只认规范名 dvcC"
            )
        # Apple HLS/fMP4 的 init 段 hvcC 必须自带 VPS/SPS/PPS。2026-09-26
        # 真机实测：参数集只走分片 in-band 时，tvOS 拉完 init 与首分片就停播。
        present = hvcC_param_set_types(output.init.read_bytes())
        missing = [nal for nal in (32, 33, 34) if nal not in present]
        if missing:
            problems.append(
                f"init 段 hvcC 缺参数集 {missing}（现有 {list(present)}）："
                "Apple HLS 要求 VPS/SPS/PPS 随 init 下发，tvOS 会拉到首分片后停播"
            )

    try:
        out_dovi = probe(output.init).dovi
    except Exception as exc:  # noqa: BLE001
        out_dovi = None
        problems.append(f"ffprobe 读 init 段失败：{type(exc).__name__}")
    if out_dovi != expected:
        problems.append(f"DoviRecord 不符：目标 {expected} ≠ 产出 {out_dovi}")

    if out_dovi is not None and not out_dovi.el_present:
        # 单层 RPU 必须自洽，否则就是"伪 P5"——信令看着对，tvOS 拒播
        problems.extend(check_rpu_consistency(output))

    return Verification(
        ok=not problems,
        signalling=signalling,
        source_dovi=source.dovi,
        output_dovi=out_dovi,
        problems=tuple(problems),
        target_dovi=expected,
    )


def remux_and_verify(src: str | Path, out_dir: str | Path, **kwargs) -> Verification:
    source, output = remux(src, out_dir, **kwargs)
    return verify(source, output, target=source.dovi.profile5_target if source.dovi else None)

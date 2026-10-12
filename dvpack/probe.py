"""探测：读 MKV 里的 DOVI configuration record，产出打包所需的事实。

这里只做"读"和"派生"，不碰打包。所有派生都建立在实测字段上：
VIDEO-RANGE 由 ffprobe 实测的 color_transfer 决定，而不是由 dv_bl_signal_compatibility_id
查表决定——那份表的各个来源互相矛盾，没在真机上验证之前不写进代码。
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .dvprofile import (
    PROFILE_VIDEO_RANGE_FALLBACK,
    RISKY_P5_LEVELS,
    TRANSFER_TO_VIDEO_RANGE,
)
from .tools import FFPROBE, probe_timeout, require

DOVI_SIDEDATA = "DOVI configuration record"

SHOW_ENTRIES = (
    "stream=index,codec_type,codec_name,profile,width,height,bit_depth,"
    "color_range,color_space,color_transfer,color_primaries,"
    "avg_frame_rate,r_frame_rate,tags,"
    "stream_side_data"
    ":format=format_name,duration,size"
)

#: 唯一有官方依据的 compat_id 取值（Apple 附录：db4h ↔ HLG）
_COMPAT_HLG = 4
_TRANSFER_TO_VIDEO_RANGE = TRANSFER_TO_VIDEO_RANGE
_DVI_PROFILE_VIDEO_RANGE = PROFILE_VIDEO_RANGE_FALLBACK
_RISKY_P5_LEVELS = RISKY_P5_LEVELS


@dataclass(frozen=True)
class DoviRecord:
    profile: int
    level: int
    rpu_present: bool
    el_present: bool
    bl_present: bool
    compat_id: int
    md_compression: str
    spec_version: str

    @property
    def target_apple_codec(self) -> str:
        """打包后希望出现在 master playlist 里的 CODECS 值。

        Apple 写法是 dvh1.<profile>.<level>，两段均为两位十进制：
        'in dvh1.05.03 the Profile is 5 and the Level is 3'
        （HLS Authoring Specification for Apple devices，附录）。
        这是**目标**，不是既成事实——实际是否写成 dvh1 由 dvpack.bmff 校验。
        """
        return f"dvh1.{self.profile:02d}.{self.level:02d}"

    @property
    def is_single_layer(self) -> bool:
        return self.el_present is False

    @property
    def profile5_target(self) -> DoviRecord:
        """Apple HLS 规范 §1.9 只认 Profile 5，这是单层源转签后的目标信令。

        字节模板取自 Apple 官方参考流 `adv_dv_atmos` 的 P5 变体
        （devstreaming-cdn.apple.com/videos/streaming/examples/adv_dv_atmos），
        实测 `dvcC` payload = `01 00 0a 35 00 00...`：profile 5、level 6、
        rpu/bl 置位、el 清零、`dv_bl_signal_compatibility_id = 0`。
        level 原样保留——把高 level 悄悄压成 7 是谎报，判不过来。
        """
        return DoviRecord(
            profile=5,
            level=self.level,
            rpu_present=self.rpu_present,
            el_present=False,
            bl_present=True,
            compat_id=0,
            md_compression=self.md_compression,
            spec_version=self.spec_version,
        )


@dataclass(frozen=True)
class VideoStream:
    index: int
    codec: str
    profile: str
    width: int
    height: int
    bit_depth: int
    frame_rate: float
    color_transfer: str
    color_space: str
    color_primaries: str
    dovi: DoviRecord | None
    color_range: str = ""

    @property
    def apple_video_range(self) -> str:
        """容器实测优先；DV 流按 profile 定义兜底——MKV rip 常常不带 colour tags。

        compat_id == 4 走 HLG：这条对应 Apple 附录里 `dvh1.08.07/db4h` 与
        `VIDEO-RANGE=HLG` 的配对，是本模块唯一敢用的 compat_id 取值。
        """
        mapped = _TRANSFER_TO_VIDEO_RANGE.get(self.color_transfer)
        if mapped is not None:
            return mapped
        dovi = self.dovi
        if dovi is not None:
            if dovi.compat_id == _COMPAT_HLG:
                return "HLG"
            fallback = _DVI_PROFILE_VIDEO_RANGE.get(dovi.profile)
            if fallback is not None:
                return fallback
            raise UnknownTransfer(
                f"Profile {dovi.profile} 没有兜底映射，容器也没给 color_transfer"
            )
        raise UnknownTransfer(
            f"color_transfer={self.color_transfer!r} 无法映射到 VIDEO-RANGE"
        )


@dataclass(frozen=True)
class SourceInfo:
    path: Path
    format_name: str
    duration: float
    size: int
    video: VideoStream
    #: 第一条音轨的 RFC 6381 codec 名（"aac" / "ec-3" / "ac-3"），没有音轨则空串。
    #:
    #: 2026-09-27：master 的 CODECS 必须列全变体里出现的**每一种** codec。
    #: 这里曾经只写 dvh1.05.09，而产物已经带上了 aac 音轨——对不上，AVPlayer
    #: 直接拒收整个 master：真机日志里只看到一次 /master.m3u8 200，之后
    #: **一个分片都没再请求**，症状就是黑屏。别再把这里当装饰字段。
    audio_codec: str = ""
    #: 第一条音轨的原始 ffprobe codec_name（"dts" / "truehd" / "eac3"…）。
    #: 与 audio_codec 的分工：后者只收 Apple 认识的编码，不认识返回空串；
    #: 原始名用于区分「根本没有音轨」（只能纯视频包）与「有轨但 Apple 不认」
    #: （转码 aac 保住声音）——2026-10-03 狂飙 ab476f 的 dts 音轨。
    audio_codec_name: str = ""
    #: 全部字幕流（文本 + 图像）。2026-10-04 双语字幕链路用：只取 is_text 的
    #: 流下载，track_language 归一成 zh/en 后按语言各出一条 HLS 字幕轨。
    subtitle_streams: tuple["SubtitleStream", ...] = ()

    @property
    def dovi(self) -> DoviRecord | None:
        return self.video.dovi


class UnknownTransfer(Exception):
    pass


class NotProbeable(Exception):
    pass


@dataclass(frozen=True)
class Verdict:
    """这个源文件能不能进 MVP 的打包链路。"""

    supported: bool
    reason: str
    warnings: tuple[str, ...] = ()


def parse_fraction(text: str | None) -> float:
    if not text or "/" not in text:
        return 0.0
    num, _, den = text.partition("/")
    try:
        n, d = float(num), float(den)
    except ValueError:
        return 0.0
    return n / d if d else 0.0


def _parse_dovi(sidedata: dict) -> DoviRecord:
    return DoviRecord(
        profile=int(sidedata["dv_profile"]),
        level=int(sidedata["dv_level"]),
        rpu_present=bool(int(sidedata.get("rpu_present_flag", 0))),
        el_present=bool(int(sidedata.get("el_present_flag", 0))),
        bl_present=bool(int(sidedata.get("bl_present_flag", 0))),
        compat_id=int(sidedata.get("dv_bl_signal_compatibility_id", -1)),
        md_compression=str(sidedata.get("dv_md_compression", "unknown")),
        spec_version=(
            f"{sidedata.get('dv_version_major', '?')}."
            f"{sidedata.get('dv_version_minor', '?')}"
        ),
    )


_SUBTITLE_TEXT_CODECS = frozenset(
    {"subrip", "webvtt", "mov_text", "text", "ass", "ssa"}
)

#: MKV / 流媒体字幕语言 tag → HLS 字幕轨语言。Jellyfin 的 MKV rip 语言 tag
#: 五花八门（chi / zho / chs / cmn 都见过），不归一的话 tvOS 字幕选择器里
#: 中英两条轨会裂成一堆单语言组。归一只认白名单，其余一律空串（= 跳过）。
_LANGUAGE_ALIASES = {
    "zh": "zh", "chi": "zh", "zho": "zh", "chs": "zh", "cmn": "zh",
    "en": "en", "eng": "en",
}


def _normalize_language(tag: str) -> str:
    """语言 tag 归一成 zh / en；取不到就空串（调用方跳过该轨）。"""
    head = (tag or "").strip().lower().split("-")[0]
    return _LANGUAGE_ALIASES.get(head, "")


@dataclass(frozen=True)
class SubtitleStream:
    """ffprobe 报出来的一条字幕流。

    只描述事实：index 给 Jellyfin 字幕下载端点用，codec 决定要不要下载
    （pgssub/dvdsub 是图像，tvOS 的 WebVTT 轨塞不进去），language 归一后
    决定落中轨还是英轨。
    """

    index: int
    codec: str
    language: str = ""

    @property
    def is_text(self) -> bool:
        return (self.codec or "").lower() in _SUBTITLE_TEXT_CODECS

    @property
    def track_language(self) -> str:
        return _normalize_language(self.language)


def _parse_subtitles(streams: list[dict]) -> tuple[SubtitleStream, ...]:
    """挑出全部字幕流（文本 + 图像都留，让调用方自己筛）。"""
    subs: list[SubtitleStream] = []
    for s in streams:
        if s.get("codec_type") != "subtitle":
            continue
        tags = s.get("tags") or {}
        # ffprobe 的 tag 名大小写随容器漂（MKV 走小写 language），两种都认。
        language = tags.get("language") or tags.get("LANGUAGE") or ""
        subs.append(
            SubtitleStream(
                index=int(s["index"]),
                codec=(s.get("codec_name") or "").lower(),
                language=str(language),
            )
        )
    return tuple(subs)


def _parse_video(stream: dict) -> VideoStream:
    dovi = None
    for sidedata in stream.get("side_data_list") or []:
        if sidedata.get("side_data_type") == DOVI_SIDEDATA:
            dovi = _parse_dovi(sidedata)
            break
    return VideoStream(
        index=int(stream["index"]),
        codec=stream.get("codec_name", ""),
        profile=stream.get("profile", ""),
        width=int(stream.get("width") or 0),
        height=int(stream.get("height") or 0),
        bit_depth=int(stream.get("bit_depth") or 0),
        frame_rate=parse_fraction(stream.get("avg_frame_rate"))
        or parse_fraction(stream.get("r_frame_rate")),
        color_transfer=stream.get("color_transfer") or "",
        color_space=stream.get("color_space") or "",
        color_primaries=stream.get("color_primaries") or "",
        color_range=stream.get("color_range") or "",
        dovi=dovi,
    )


#: ffprobe 的 codec_name → RFC 6381 codec 串（master 的 CODECS 要写后者）。
#: 没收录的直接不认——宁可少写，也不能写错把整个 master 弄成非法
#:（AVPlayer 会直接拒收，连 init.mp4 都不请求）。2026-10-03《纽伦堡》实测：
#: CODECS 写 "aac" 时电视只拉 master.m3u8 + video.m3u8 就放弃；HLS 的 AAC
#: 码串是 RFC 6381 风格（mp4a.40.2），不是 ffprobe 的 codec_name "aac"。
_AUDIO_RFC6381 = {
    "aac": "mp4a.40.2",
    "ac3": "ac-3",
    "eac3": "ec-3",
    "mp3": "mp4a.40.34",
    "opus": "opus",
    "flac": "flac",
}


def _audio_codec(streams: list[dict]) -> str:
    """取第一条音轨的 RFC 6381 名；没有音轨或 codec 不认识就返回空串。"""
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    if audio is None:
        return ""
    return _AUDIO_RFC6381.get((audio.get("codec_name") or "").lower(), "")


def _audio_codec_name(streams: list[dict]) -> str:
    """第一条音轨的原始 codec_name；没有音轨则空串。"""
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    if audio is None:
        return ""
    return (audio.get("codec_name") or "").lower()


def parse_probe_json(obj: dict, path: Path) -> SourceInfo:
    """从 ffprobe 的 JSON 结果构造 SourceInfo。纯函数，测试不打进程。"""
    fmt = obj.get("format") or {}
    streams = obj.get("streams") or []
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    if video is None:
        raise NotProbeable(f"{path} 里没有视频流")
    return SourceInfo(
        path=Path(path),
        format_name=fmt.get("format_name", ""),
        duration=float(fmt.get("duration") or 0.0),
        size=int(fmt.get("size") or 0),
        video=_parse_video(video),
        audio_codec=_audio_codec(streams),
        audio_codec_name=_audio_codec_name(streams),
        subtitle_streams=_parse_subtitles(streams),
    )


def probe(path: str | Path) -> SourceInfo:
    """调用 ffprobe 探测一个本地文件。"""
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(path)
    return parse_probe_json(ffprobe_json(path), path)


def ffprobe_json(source: str | Path) -> dict:
    """ffprobe 直读一个本地文件或 http(s) 地址，返回原始 JSON。

    Jellyfin 片源在 Mac 上没有本地路径，dvpack.jfpack 靠这层直读 URL；
    `probe()` 只服务本地文件（它必须先 path.exists()），两者共用同一份 argv。

    远程输入不加 `-timeout`：局域网里的 5GB MKV 单次探测实测数秒完成，
    加超时反而会把"片源还没索引完"误判成失败。
    """
    argv = [
        str(require(FFPROBE)),
        # -v error 而不是 quiet：quiet 会把失败原因一起吞掉（stderr 全空），
        # 2026-09-26 launchd/TCC 事故里第一次排障就盲在这里。
        # JSON 仍走 stdout，错误走 stderr，互不污染。
        "-v",
        "error",
        "-print_format",
        "json",
        "-show_format",
        "-show_streams",
        "-show_entries",
        SHOW_ENTRIES,
        str(source),
    ]
    # subprocess 级执行超时（不是 ffmpeg 的 -timeout 网络选项）：Jellyfin 流
    # 半路卡死时 ffprobe 永不返回，调用线程（serve 的请求线程）跟着挂死。
    # 超时转 NotProbeable，走调用方既有的坏源分支。
    cap = probe_timeout()
    try:
        proc = subprocess.run(
            argv, capture_output=True, text=True, encoding="utf-8", timeout=cap
        )
    except subprocess.TimeoutExpired as exc:
        raise NotProbeable(
            f"ffprobe 执行超时（>{cap:.0f}s）：{source}（上游流卡死）"
        ) from exc
    if proc.returncode != 0 or not proc.stdout.strip():
        # ffprobe 的失败原因全在 stderr（-v quiet 下尤其），丢了它现场全盲：
        # launchd 出站被 TCC 拦成 EHOSTUNREACH 那次，就靠这行才定位到根因。
        detail = (proc.stderr or "").strip().splitlines()
        tail = detail[-1].strip()[:300] if detail else ""
        raise NotProbeable(
            f"ffprobe 失败（{proc.returncode}）：{source}" + (f" — {tail}" if tail else "")
        )
    return json.loads(proc.stdout)


def mvp_verdict(source: SourceInfo) -> Verdict:
    """MVP 只覆盖单层 BL+RPU 的 Profile 5 / 8。

    Profile 8 允许打包，但会带一句归因说明：Apple HLS Authoring
    Specification §1.9 规定 HLS 里的杜比视界 MUST be Profile 5（≤Level 7），
    P8 单层在 tvOS 上只被当 HDR10 播放、徽标不亮。
    2026-09-25 真机实测（Apple TV 4K / tvOS 27.2 + DV 电视）：P8 打包物
    复判齐全依然不亮；同月深夜定位到 ffmpeg 写的是 box 俗名 `dvvC`，而 Apple
    官方 P5 参考流用的是规范名 `dvcC`（ffprobe 两种都读，本地无法发现）。
    现在的处置是把 init 段转签成 P5（见 dvpack.p5），是否点亮待真机确认。
    """
    dovi = source.dovi
    if dovi is None:
        return Verdict(False, "无 DOVI configuration record，不是杜比视界片源")
    if not dovi.rpu_present:
        return Verdict(False, "RPU 缺失：即使打包完成电视也只能当普通 HDR 放")
    if not dovi.is_single_layer:
        return Verdict(
            False,
            f"Profile {dovi.profile} 带增强层（双层），MVP 未覆盖——需要先丢弃 EL",
        )
    if dovi.profile not in (5, 8):
        return Verdict(False, f"Profile {dovi.profile} 不在 MVP 范围（仅 5 / 8）")

    warnings: list[str] = []
    if dovi.profile == 8:
        warnings.append(
            "Profile 8（单层 HDR10 兼容）：Apple 规范 §1.9 要求 DV MUST be Profile 5，"
            "打包时会把 init 段转签成 P5（dvcC，逐字节对照过 Apple 官方 P5 参考流）——"
            "tvOS 侧是否点亮待真机确认；不应期就把本包当 HDR10 兼容层用"
        )
    if dovi.profile == 5 and dovi.level in _RISKY_P5_LEVELS:
        warnings.append(
            f"level {dovi.level}：Infuse 社区报告 Profile 5 在此 level 不点亮 DV，"
            "本项目的验收矩阵会真机判定它"
        )
    if dovi.md_compression not in ("none", ""):
        warnings.append(
            f"RPU 元数据压缩方式为 {dovi.md_compression}，透传时无需解压，"
            "但 tvOS 侧兼容性待验"
        )
    return Verdict(True, f"单层 Profile {dovi.profile}，RPU 完整", tuple(warnings))

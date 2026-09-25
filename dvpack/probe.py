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

from .tools import FFPROBE, require

DOVI_SIDEDATA = "DOVI configuration record"

SHOW_ENTRIES = (
    "stream=index,codec_type,codec_name,profile,width,height,bit_depth,"
    "color_space,color_transfer,color_primaries,avg_frame_rate,r_frame_rate,"
    "stream_side_data"
    ":format=format_name,duration,size"
)

# 实测 transfer → Apple HLS 的 VIDEO-RANGE 属性值。
# 实测优先，但 MKV rip 常常不写 colour tags（实测片源里就常缺），
# 所以 DV 分支再按 profile 定义兜底。
_TRANSFER_TO_VIDEO_RANGE = {
    "smpte2084": "PQ",
    "bt2020lc": "PQ",  # 部分容器把 PQ 报成这个
    "arib-std-b67": "HLG",
    "bt709": "SDR",
}

# Profile 5 按定义就是单层 BT.2020/PQ、无回退层；7 的基底是 HDR10；
# 8 默认 8.1（HDR10 基底），8.4 才是 HLG——8.4 会在实测 transfer 里露出来，走上面那张表。
_DVI_PROFILE_VIDEO_RANGE = {5: "PQ", 7: "PQ", 8: "PQ", 10: "PQ"}

# 唯一有官方依据的 compat_id 取值（Apple 附录：db4h ↔ HLG）
_COMPAT_HLG = 4

# Infuse 官方社区有报告：Profile 5 在 level 7 / 9 时不点亮 DV。
# 未在自己的硬件上复现，所以只当风险项报出来，不当硬性拒绝。
# 只限 Profile 5——Profile 8 在 level 7/9 是流媒体片源的常态，报它是噪音。
_RISKY_P5_LEVELS = frozenset({7, 9})


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
        dovi=dovi,
    )


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
    )


def probe(path: str | Path) -> SourceInfo:
    """调用 ffprobe 探测一个文件。"""
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(path)
    argv = [
        str(require(FFPROBE)),
        "-v",
        "quiet",
        "-print_format",
        "json",
        "-show_format",
        "-show_streams",
        "-show_entries",
        SHOW_ENTRIES,
        str(path),
    ]
    proc = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8")
    if proc.returncode != 0 or not proc.stdout.strip():
        raise NotProbeable(f"ffprobe 失败（{proc.returncode}）：{path}")
    return parse_probe_json(json.loads(proc.stdout), path)


def mvp_verdict(source: SourceInfo) -> Verdict:
    """MVP 只覆盖单层 BL+RPU 的 Profile 5 / 8。"""
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

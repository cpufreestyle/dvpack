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
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .bmff import hvcC_param_set_types, parse_file
from .p5 import resign_pack
from .probe import DoviRecord, SourceInfo, mvp_verdict, probe
from .tools import FFPROBE, FFMPEG, require

# 见模块 docstring 第 2、3 条：缺任一条都不会得到合法 DV 信令
DV_VIDEO_FLAGS = ("-tag:v", "dvh1", "-strict", "unofficial")

# init 段必须显式声明色彩信号，否则徽标点不亮。DV rip（MKV/TS）普遍不带
# colour tag，HEVC SPS 里也没有 VUI——2026-09-26 ffprobe 实测本片源帧级
# color_space/transfer/primaries 全为空。不补 colr 盒，AVPlayer 只能按
# SDR BT.709 做映射：真机症状「颜色明显不对 + DV 徽标不亮」就是这么来的
# （同源 infuse 直读文件反而点亮，差别在直读路径色彩信号由 App 侧注入）。
# 默认三件套即 Dolby Vision Profile 5 的容器定义：BT.2020 原色 /
# SMPTE ST 2084(PQ) 传递函数 / BT.2020 非恒定亮度矩阵。
DV_DEFAULT_COLORIMETRY = ("bt2020", "smpte2084", "bt2020nc")


def colorimetry_flags(
    colorimetry: tuple[str, str, str] | None = None,
    *,
    full_range: int = 0,
) -> tuple[str, ...]:
    """让 fmp4 muxer 把 colour 信息写进 init 段 stsd 的 colr 盒（nclx 形式）。

    colorimetry 为 (primaries, transfer, space)；None 时用 DV 默认三件套。
    full_range=1（全色域源）→ `-color_range pc`；0 → tv。口径必须与源一致：
    2026-09-26 对 Jellyfin 测试片做像素级实测（signalstats 逐帧
    YMIN=0/YMAX=1016；limited 黑位应为 64），容器 color_range=pc、RPU
    bl_video_full_range_flag=1——三证俱全，底层是真·全色域 10bit。旧代码
    写死 tv，把全色域像素按 limited 解释（黑位抬升、颜色发灰），还与 RPU
    自相矛盾，真机即「颜色明显不对 + 徽标不亮」。06:48 点亮那次是 P7 源
    （BL 本就 limited），limited 结论不能推广到原生全色域源；Infuse 直读
    同源 MKV 能点亮，走的就是容器/RPU 的全色域口径。
    """
    primaries, transfer, space = colorimetry or DV_DEFAULT_COLORIMETRY
    return (
        "-color_primaries",
        primaries,
        "-color_trc",
        transfer,
        "-colorspace",
        space,
        "-color_range",
        "pc" if full_range else "tv",
    )

# 全色域口径从源推导，不写死：pc/full/jpeg→1，tv/limited/mpeg→0，
# unknown/缺省→0（保守兜底——P7 Halo 转签场景 BL 本就 limited）。
_FULL_RANGE_TOKENS = frozenset({"pc", "full", "jpeg"})


def source_full_range(source: SourceInfo) -> int:
    """源容器的 color_range → SPS VUI video_full_range_flag（0/1）。

    容器、SPS VUI、RPU 三处口径必须一致：2026-09-26 实测本片源容器 pc、
    RPU bl_video_full_range_flag=1、像素 YMIN=0/YMAX=1016，任何一处签错
    （旧代码三处全签 limited）真机都是「颜色不对 + 徽标不亮」。
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
    """SPS VUI 信令 BSF：range 位随源，颜色三件套固定 P5 口径。"""
    return (
        f"hevc_metadata=video_full_range_flag={1 if full_range else 0}:"
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


# MVP 只要画面，音频另立里程碑：E-AC3 进 fMP4 的 HLS 兼容性还没实测，
# 带进去会让"徽标没亮"这件事变得无法归因。
VIDEO_ONLY_MAPS = ("-map", "0:v:0")


class RemuxError(Exception):
    pass


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
    maps: tuple[str, ...] = VIDEO_ONLY_MAPS,
    colorimetry: tuple[str, str, str] | None = None,
    full_range: int = 0,
    bsf: tuple[str, ...] | None = None,
) -> list[str]:
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
    if bsf:
        # -bsf:v 只吃一条链：多个 -bsf 参数 ffmpeg 只保留最后一个（2026-09-26 实测）
        argv += ["-bsf:v", ",".join(bsf)]
    argv += [
        "-f",
        "hls",
        "-hls_segment_type",
        "fmp4",
        "-hls_flags",
        "independent_segments",
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


def _run(argv: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(argv, capture_output=True, text=True, encoding="utf-8", cwd=cwd)


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


def remux(
    src: str | Path,
    out_dir: str | Path,
    *,
    seconds: float | None = None,
    segment_seconds: float = 6.0,
    maps: tuple[str, ...] = VIDEO_ONLY_MAPS,
    name: str = "video",
) -> tuple[SourceInfo, RemuxOutput]:
    """探测 → 闸门 → remux → 定位产出。闸门不过就不动 ffmpeg。"""
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
        ),
        cwd=out_dir,
    )
    if not (out_dir / playlist).exists():
        raise RemuxError(f"ffmpeg 未产出列表：{proc.stderr.strip() or '未知错误'}")

    output = resolve_output(out_dir / playlist)
    if not output.exists:
        raise RemuxError(f"产出不完整：init 或分片缺失（{proc.stderr.strip()}）")

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

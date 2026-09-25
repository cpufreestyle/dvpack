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

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .bmff import parse_file
from .probe import DoviRecord, SourceInfo, mvp_verdict, probe
from .tools import FFMPEG, require

# 见模块 docstring 第 2、3 条：缺任一条都不会得到合法 DV 信令
DV_VIDEO_FLAGS = ("-tag:v", "dvh1", "-strict", "unofficial")

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

    @property
    def dovi_preserved(self) -> bool:
        return self.source_dovi is not None and self.source_dovi == self.output_dovi


def hls_command(
    src: Path,
    playlist: Path,
    *,
    seconds: float | None = None,
    segment_seconds: float = 6.0,
    maps: tuple[str, ...] = VIDEO_ONLY_MAPS,
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
        str(playlist),
    ]
    return argv


def _run(argv: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(argv, capture_output=True, text=True, encoding="utf-8", cwd=cwd)


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
        hls_command(Path(src), playlist, seconds=seconds, segment_seconds=segment_seconds, maps=maps),
        cwd=out_dir,
    )
    if not (out_dir / playlist).exists():
        raise RemuxError(f"ffmpeg 未产出列表：{proc.stderr.strip() or '未知错误'}")

    output = resolve_output(out_dir / playlist)
    if not output.exists:
        raise RemuxError(f"产出不完整：init 或分片缺失（{proc.stderr.strip()}）")
    return source, output


def verify(source: SourceInfo, output: RemuxOutput) -> Verification:
    """独立判据：读 init 段的 box 结构，再用 ffprobe 复读配置记录比对源。"""
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

    try:
        out_dovi = probe(output.init).dovi
    except Exception as exc:  # noqa: BLE001
        out_dovi = None
        problems.append(f"ffprobe 读 init 段失败：{type(exc).__name__}")
    if out_dovi != source.dovi:
        problems.append(f"DoviRecord 不一致：源 {source.dovi} ≠ 产出 {out_dovi}")

    return Verification(
        ok=not problems,
        signalling=signalling,
        source_dovi=source.dovi,
        output_dovi=out_dovi,
        problems=tuple(problems),
    )


def remux_and_verify(src: str | Path, out_dir: str | Path, **kwargs) -> Verification:
    source, output = remux(src, out_dir, **kwargs)
    return verify(source, output)

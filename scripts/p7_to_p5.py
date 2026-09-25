#!/usr/bin/env python3
"""Profile 7（双层）→ tvOS 可点亮的 Profile 5 HLS：全程走 dovi_tool。

链路（每一步都有出处，不自己造轮子）：
  1. ffmpeg  MKV → 裸 HEVC（BL+EL+RPU）
  2. dovi_tool -m 5 convert -d   丢 EL + 把 RPU 改写成 profile 8.1 兼容
     —— mode 2/5 实测产出 nlq_method_idc=none 且 disable_residual_flag=1 的
        自洽单层 RPU；mode 1 仍带 NLQ。原始 P7 是 nlq=linear_dz + 残差启用，
        那就是"伪 P5"，tvOS 拒播（真机实证）。
  3. ffmpeg  → CMAF/fMP4 + HLS（dvh1 tag）
  4. dvpack.p5.resign_init_segment   裸 HEVC 输入没有 DOVI 侧数据，ffmpeg 一个
     配置 box 都不写，所以自己按 Apple 官方模板造 dvcC 插进 sample entry
  5. master 列表 dvh1.05.06 + VIDEO-RANGE=PQ，无 SUPPLEMENTAL / HDCP（照 Apple）

用法：
    python scripts/p7_to_p5.py <src.mkv> -o _out/dv_p7 [--seconds 60]

依赖：dovi_tool（项目 tools/bin/，2.3.4，从 Homebrew bottle 取得；GitHub
直连被墙但 bottle 镜像可达）、ffmpeg。
"""

from __future__ import annotations

import argparse
import json
import math
import re
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dvpack.bmff import parse_file  # noqa: E402
from dvpack.p5 import resign_init_segment  # noqa: E402
from dvpack.probe import probe  # noqa: E402
from dvpack.remux import check_rpu_consistency, resolve_output  # noqa: E402
from dvpack.tools import DOVI_TOOL, FFMPEG, FFPROBE, require  # noqa: E402

def _tools() -> tuple[str, str, str]:
    return str(require(DOVI_TOOL)), str(require(FFMPEG)), str(require(FFPROBE))


def extract_es(src: Path, dst: Path, seconds: float | None, ffmpeg: str) -> None:
    argv = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y"]
    if seconds:
        argv += ["-t", str(seconds)]
    argv += ["-i", str(src), "-map", "0:v:0", "-c", "copy", "-f", "hevc", str(dst)]
    proc = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8")
    if proc.returncode != 0 or not dst.exists():
        raise SystemExit(f"ffmpeg 抽流失败：{proc.stderr.strip() or '未知错误'}")


def convert_to_single_layer(raw: Path, out: Path, dovi_tool: str, *, mode: int = 5) -> None:
    """dovi_tool convert：丢 EL + RPU 改写。mode 5 = 8.1 兼容且保留 luma/chroma 映射。"""
    proc = subprocess.run(
        [dovi_tool, "-m", str(mode), "convert", "-i", str(raw), "-o", str(out), "-d"],
        capture_output=True, text=True, encoding="utf-8",
    )
    if proc.returncode != 0 or not out.exists():
        raise SystemExit(f"dovi_tool convert 失败：{proc.stderr.strip() or '未知错误'}")


def pack(bl: Path, out_dir: Path, ffmpeg: str, *, time_base: str = "24000/1001") -> Path:
    """单层 HEVC → CMAF/fMP4 + HLS。flag 依据见 dvpack.remux 的模块注释。

    cwd 钉在 out_dir 且 argv 里只给裸文件名：ffmpeg 的 init.mp4 按 **当前目录**
    落盘，给绝对路径会掉到别处去（dvpack.remux 里同一条实测）。

    setts 位流过滤器不能去：dovi_tool 输出的裸 HEVC 没有任何 pts（raw demuxer
    不产时间戳，实测 packet pts 全 N/A），hls muxer 于是写出 EXTINF:0.000000、
    TARGETDURATION:0——RFC 8216 要求正整数，AVPlayer 直接当非法列表：电视只
    轮询 media playlist、永远不拉 init.mp4 和分片（2026-09-25 真机实证，serve
    日志里 playlist 请求每秒级重试、零分片请求）。setts 按帧序号补 CFR 时间戳，
    零重编码、码流一字节不动。
    """
    out_dir = out_dir.resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    argv = [
        ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
        "-i", str(bl), "-map", "0:v:0", "-c", "copy",
        "-bsf:v", f"setts=time_base={time_base}:ts=N:duration=1",
        "-tag:v", "dvh1", "-strict", "unofficial",
        "-f", "hls", "-hls_segment_type", "fmp4",
        "-hls_flags", "independent_segments",
        "-hls_time", "6", "-hls_list_size", "0",
        "video.m3u8",
    ]
    proc = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8", cwd=out_dir)
    playlist = out_dir / "video.m3u8"
    if not playlist.exists():
        raise SystemExit(f"ffmpeg 打包失败：{proc.stderr.strip() or '未知错误'}")
    return playlist


def write_master(out_dir: Path, *, width: int, height: int, fps: float, bandwidth: int) -> Path:
    """master 列表照 Apple 官方 P5 变体：dvh1.05.06 + PQ，无回退层/HDCP 属性。"""
    attrs = [f"BANDWIDTH={bandwidth}", 'CODECS="dvh1.05.06"']
    if width and height:
        attrs.append(f"RESOLUTION={width}x{height}")
    if fps:
        attrs.append(f"FRAME-RATE={math.ceil(fps)}")
    attrs.append("VIDEO-RANGE=PQ")
    text = (
        "#EXTM3U\n#EXT-X-VERSION:7\n#EXT-X-INDEPENDENT-SEGMENTS\n"
        f"#EXT-X-STREAM-INF:{','.join(attrs)}\nvideo.m3u8\n"
    )
    path = out_dir / "master.m3u8"
    path.write_text(text, encoding="utf-8")
    return path


def main() -> int:
    ap = argparse.ArgumentParser(description="Profile 7 → Profile 5 HLS（dovi_tool + dvpack）")
    ap.add_argument("src", type=Path)
    ap.add_argument("-o", "--out", type=Path, default=Path("_out/dv_p7"))
    ap.add_argument("--seconds", type=float, default=None)
    ap.add_argument("--mode", type=int, default=5, help="dovi_tool convert 模式，默认 5")
    args = ap.parse_args()

    dovi_tool, ffmpeg, ffprobe = _tools()
    source = probe(args.src)
    level = source.dovi.level if source.dovi else 6

    fps_json = json.loads(
        subprocess.run(
            [ffprobe, "-v", "quiet", "-print_format", "json", "-show_streams",
             "-select_streams", "v:0", str(args.src)],
            capture_output=True, text=True, encoding="utf-8",
        ).stdout
    )["streams"][0]
    time_base = fps_json.get("r_frame_rate") or "24000/1001"

    with tempfile.TemporaryDirectory() as tmp:
        raw = Path(tmp) / "bl_el.hevc"
        mel = Path(tmp) / "mel.hevc"
        extract_es(args.src, raw, args.seconds, ffmpeg)
        convert_to_single_layer(raw, mel, dovi_tool, mode=args.mode)

        playlist = pack(mel, args.out, ffmpeg, time_base=time_base)
        output = resolve_output(playlist)

        # 裸 HEVC 输入没有 DOVI 配置 box：按 Apple 官方模板造一个插进去
        result = resign_init_segment(
            output.init, profile=5, compat_id=0, el_present=False, level=level
        )

        stream = json.loads(
            subprocess.run(
                [ffprobe, "-v", "quiet", "-print_format", "json", "-show_streams",
                 "-select_streams", "v:0", str(playlist)],
                capture_output=True, text=True, encoding="utf-8",
            ).stdout
        )["streams"][0]
        width, height = int(stream.get("width", 0)), int(stream.get("height", 0))
        num, _, den = (stream.get("avg_frame_rate") or "0/1").partition("/")
        fps = (float(num) / float(den)) if float(den or 1) else 0.0
        segments = [p for p in (args.out / line for line in playlist.read_text().splitlines()
                                if line and not line.startswith("#")) if p.exists()]
        duration = args.seconds or 1.0
        bandwidth = int(sum(p.stat().st_size for p in segments) * 8 / duration * 1.2)
        write_master(args.out, width=width, height=height, fps=fps, bandwidth=bandwidth)

    video = parse_file(output.init).video
    print(f"源          : {args.src}（P{source.dovi.profile} L{source.dovi.level}）")
    print(f"dovi_tool   : mode {args.mode} convert -d（丢 EL，RPU 改写）")
    print(f"配置 box    : {result.describe()}；init 信令 = {video.signalling}")
    print(f"问题        : {check_rpu_consistency(output) or '无：RPU 自洽（nlq=none、残差禁用）'}")
    print(f"产出        : {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

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
    [--compat-id 1] [--chrm]

依赖：dovi_tool（项目 tools/bin/，2.3.4，从 Homebrew bottle 取得；GitHub
直连被墙但 bottle 镜像可达）、ffmpeg。
"""

from __future__ import annotations

import argparse
import json
import math
import re
import struct
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dvpack.bmff import (  # noqa: E402
    dedupe_hvcC_parameter_sets,
    insert_box_after,
    parse_file,
    remove_box,
)
from dvpack.p5 import resign_init_segment  # noqa: E402
from dvpack.probe import probe  # noqa: E402
from dvpack.remux import (  # noqa: E402
    DV_SPS_VUI_BSF,
    check_rpu_consistency,
    resolve_output,
)
from dvpack.tools import DOVI_TOOL, FFMPEG, FFPROBE, require  # noqa: E402

def _tools() -> tuple[str, str, str]:
    return str(require(DOVI_TOOL)), str(require(FFMPEG)), str(require(FFPROBE))


# P7→P5 的 VUI 口径与 remux 链路一致（见 dvpack.remux.DV_SPS_VUI_BSF）：
# SPS 的 full_range/颜色一律用 ffmpeg 自带 hevc_metadata 重写成 P5 口径，
# 不手搓位翻转；PTL 的 tier/non_packed 由 dvpack.dplus 补。
_MP4TOANNEXB_AND_EXTRACT = ("hevc_mp4toannexb", *DV_SPS_VUI_BSF, "extract_extradata")
assert len(DV_SPS_VUI_BSF) == 1, "hevc_metadata 与其参数必须是一个 '=' 连接的整体"


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


def chrm_payload() -> bytes:
    """chrm box payload：BT.2020 容器，primaries=9 / transfer=16(PQ) /
    matrix=9(2020nc) / full_range=0。

    Apple 官方 P5 init 里有 chrm（+fiel），我们产物只有 colr/pasp。本 flag 用于
    “补齐 Apple 模板差异”实验，默认关——差异是否影响系统 DV 判定未知，先保留开关。
    """
    return struct.pack(">HHHB", 9, 16, 9, 0)


def pack(bl: Path, out_dir: Path, ffmpeg: str, *, frame_rate: str = "24000/1001") -> Path:
    """单层 HEVC，或已封装的完整 mp4 → CMAF/fMP4 + HLS。

    `.mp4`/`.m4v` 直达 mp4_to_hls（省掉合成，也避开 `bl.with_suffix(".mp4")`
    把输入覆盖成同名输出时 ffmpeg 的 "output same as input" 报错）；其余当裸
    HEVC，先 hevc_to_mp4 合成 mid 再进 mp4_to_hls。flag 依据见
    dvpack.remux 的模块注释。

    cwd 钉在 out_dir 且 argv 里只给裸文件名：ffmpeg 的 init.mp4 按 **当前目录**
    落盘，给绝对路径会掉到别处去（dvpack.remux 里同一条实测）。

    时间戳走**输入帧率**（-framerate 交给 hevc 裸流 demuxer 合成 CFR pts），
    分两步封 mp4→HLS——不能一步直接从裸流打 hls：裸 hevc demuxer 只给 pts
    不给 duration，hls muxer 算不出分段时长，写出 EXTINF:0.000000、
    TARGETDURATION:0（RFC 8216 要求正整数，AVPlayer 直接当非法列表拒播）。
    先把裸流转成完整 mp4，每样本真实时长落到容器里，第二步 hls muxer 才能
    算出正确的 EXTINF。

    两条已废弯路都留档：
    1) 无时间戳：dovi_tool 输出的裸 HEVC packet pts 全 N/A，hls 写出
       EXTINF:0——电视只轮询 playlist、零分片请求（2026-09-25 serve 日志实证）。
    2) `-bsf:v setts=ts=N:duration=1`：duration 落进**输出时基**（mp4
       mdhd=16000）的 1 刻度 = 62.5µs，24s 的 579 帧全挤在 ~36ms 里，EXTINF
       仍写 23.17——播放器解约 2 帧即"片尾"：画面冻结首帧、item/player
       currentTime 双钟停在 0.097s、tcs=paused，DV/HDR 判定无从发生
       （2026-09-25 探针 18 轮全"无新帧"实证）。
    """
    if bl.suffix.lower() in {".mp4", ".m4v"}:
        return mp4_to_hls(bl, out_dir, ffmpeg)
    mid = bl.with_suffix(".mp4")
    hevc_to_mp4(bl, mid, ffmpeg, frame_rate=frame_rate)
    return mp4_to_hls(mid, out_dir, ffmpeg)


def hevc_to_mp4(bl: Path, mid: Path, ffmpeg: str, *, frame_rate: str = "24000/1001") -> None:
    """裸 HEVC → 完整 mp4；pts+duration 都来自 -framerate（理由见 pack 的 docstring）。

    mid 落在源文件旁边（调用方的临时目录或 _out 下），不污染 serve 根目录。
    """
    mid.parent.mkdir(parents=True, exist_ok=True)
    argv_mid = [
        ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
        "-framerate", frame_rate,
        "-i", str(bl), "-map", "0:v:0", "-c", "copy",
        "-bsf:v", ",".join(_MP4TOANNEXB_AND_EXTRACT),
        str(mid),
    ]
    proc = subprocess.run(argv_mid, capture_output=True, text=True, encoding="utf-8")
    if not mid.exists():
        raise SystemExit(f"ffmpeg 合成中间 mp4 失败：{proc.stderr.strip() or '未知错误'}")


def mp4_to_hls(mid: Path, out_dir: Path, ffmpeg: str) -> Path:
    """完整 mp4 → CMAF/fMP4 + HLS。输入给绝对路径，输出只给裸文件名。

    cwd 钉在 out_dir：ffmpeg 的 init.mp4 与分片按 **当前目录** 落盘，输出给绝对
    路径会掉到别处去（dvpack.remux 里同一条实测）。
    """
    out_dir = out_dir.resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    argv = [
        ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
        "-i", str(mid), "-map", "0:v:0", "-c", "copy",
        # filter_units 只认数字类型：VPS/SPS/PPS 每种一个实例，逗号链式传递
        # （2026-09-25 实测）。dovi_tool 裸流每段开头把 32/33/34 重复两遍，
        # Apple 官方排布是 AUD+SEI+slice+RPU 零参数集；type 62（RPU）不在
        # 删除列表，不受影响。
        "-bsf:v", ",".join(
            (*_MP4TOANNEXB_AND_EXTRACT, "filter_units=remove_types=32",
             "filter_units=remove_types=33", "filter_units=remove_types=34")
        ),
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


def write_master(
    out_dir: Path, *, width: int, height: int, fps: float, bandwidth: int, hdcp: bool = False
) -> Path:
    """master 列表照 Apple 官方 P5 变体：dvh1.05.06 + PQ。

    hdcp=True 时补 HDCP-LEVEL=TYPE-1（旧版 HLS 扩展，Apple 仍认）：dvplay 的
    起播前校验把它缺失列为“电视侧可能降级输出”警告。2026-09-25 实测：缺它时
    分片能拉能放，但 tvOS 系统 DV 角标 10+ 帧截图未见——是否是最后一环待验证。
    """
    attrs = [f"BANDWIDTH={bandwidth}", 'CODECS="dvh1.05.06"']
    if width and height:
        attrs.append(f"RESOLUTION={width}x{height}")
    if fps:
        attrs.append(f"FRAME-RATE={math.ceil(fps)}")
    attrs.append("VIDEO-RANGE=PQ")
    if hdcp:
        attrs.append("HDCP-LEVEL=TYPE-1")
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
    ap.add_argument("--hdcp", action="store_true", help="master 列表补 HDCP-LEVEL=TYPE-1")
    ap.add_argument(
        "--compat-id", type=int, default=0,
        help="dvcC compat_id（Apple 官方模板为 0；1 为实验变体）")
    ap.add_argument(
        "--chrm", action="store_true",
        help="dvcC 后补 chrm box（Apple init 有 colr/pasp/chrm/fiel，我们只有 colr/pasp）")
    ap.add_argument(
        "--from-mp4", action="store_true",
        help="输入已是单层 mp4：跳过 ffmpeg 抽流与 dovi_tool convert 直接重打包"
             "（从仓库中间产物重建 Apple 对齐包时用）")
    ap.add_argument(
        "--no-btrt-edts", action="store_true",
        help="删 init 段的 btrt/edts（Apple 官方 P5 init 无这两个 box）")
    args = ap.parse_args()

    dovi_tool, ffmpeg, ffprobe = _tools()
    source = probe(args.src)
    level = source.dovi.level if source.dovi else 6

    src_json = json.loads(
        subprocess.run(
            [ffprobe, "-v", "quiet", "-print_format", "json", "-show_streams",
             "-select_streams", "v:0", str(args.src)],
            capture_output=True, text=True, encoding="utf-8",
        ).stdout
    )["streams"][0]
    time_base = src_json.get("r_frame_rate") or "24000/1001"
    src_num, _, src_den = (src_json.get("r_frame_rate") or "0/1").rpartition("/")
    src_fps = (float(src_num) / float(src_den)) if float(src_den or 1) else 0.0

    with tempfile.TemporaryDirectory() as tmp:
        if args.from_mp4:
            # 输入已是单层 mp4（如 _intermediate.mp4）：跳过抽流与 dovi_tool，
            # pack() 对 .mp4 直达 mp4_to_hls（见其 docstring）。
            converted = args.src
        else:
            raw = Path(tmp) / "bl_el.hevc"
            mel = Path(tmp) / "mel.hevc"
            extract_es(args.src, raw, args.seconds, ffmpeg)
            convert_to_single_layer(raw, mel, dovi_tool, mode=args.mode)
            converted = mel

        playlist = pack(converted, args.out, ffmpeg, frame_rate=time_base)
        output = resolve_output(playlist)

        # 裸 HEVC 输入没有 DOVI 配置 box：按 Apple 官方模板造一个插进去
        result = resign_init_segment(
            output.init, profile=5, compat_id=args.compat_id, el_present=False, level=level
        )

        if args.chrm:
            # chrm 插在 dvcC 之后：anchor 刚由 resign 保证存在（插入或转签都留 dvcC）
            try:
                with_chrm = insert_box_after(
                    output.init.read_bytes(), "dvcC", "chrm", chrm_payload())
            except ValueError as exc:
                raise SystemExit(f"插入 chrm 失败：{exc}")
            output.init.write_bytes(bytes(with_chrm))

        # hvcC 参数集去重：ffmpeg 每种参数集写两份（dovi_tool extradata 原样
        # 照搬），Apple 官方模板各一条——2026-09-25 逐 box A/B 的唯一剩余
        # init 差异。内容不同的第二条会 raise，不静默丢信令。
        deduped, removed = dedupe_hvcC_parameter_sets(output.init.read_bytes())
        if removed:
            output.init.write_bytes(deduped)

        if args.no_btrt_edts:
            # Apple 官方 P5 init 没有 btrt/edts，删掉做字节级对齐实验
            for btype in ("btrt", "edts"):
                stripped, dropped = remove_box(output.init.read_bytes(), btype)
                if dropped:
                    output.init.write_bytes(stripped)
                    print(f"删 {btype}        : {dropped} 个（Apple 官方 init 无此 box）")

        stream = json.loads(
            subprocess.run(
                [ffprobe, "-v", "quiet", "-print_format", "json", "-show_streams",
                 "-select_streams", "v:0", str(playlist)],
                capture_output=True, text=True, encoding="utf-8",
            ).stdout
        )["streams"][0]
        width, height = int(stream.get("width", 0)), int(stream.get("height", 0))
        fps = src_fps or source.video.frame_rate
        segments = [p for p in (args.out / line for line in playlist.read_text().splitlines()
                                if line and not line.startswith("#")) if p.exists()]
        duration = args.seconds or 1.0
        bandwidth = int(sum(p.stat().st_size for p in segments) * 8 / duration * 1.2)
        write_master(
            args.out, width=width, height=height, fps=fps, bandwidth=bandwidth,
            hdcp=args.hdcp,
        )

    video = parse_file(output.init).video
    dovi_note = (
        f"P{source.dovi.profile} L{source.dovi.level}" if source.dovi
        else "无 DOVI 记录（mp4 中间产物：配置 box 交给转签步骤补）"
    )
    print(f"源          : {args.src}（{dovi_note}）")
    if not args.from_mp4:
        print(f"dovi_tool   : mode {args.mode} convert -d（丢 EL，RPU 改写）")
    print(f"配置 box    : {result.describe()}；init 信令 = {video.signalling}")
    print(f"问题        : {check_rpu_consistency(output) or '无：RPU 自洽（nlq=none、残差禁用）'}")
    if removed:
        print(f"hvcC 去重   : 删 {removed} 条重复参数集（VPS/SPS/PPS 各留 1，对齐 Apple 模板）")
    if args.compat_id != 0 or args.chrm:
        print(f"实验开关    : compat_id={args.compat_id} chrm={args.chrm}")
    print(f"产出        : {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

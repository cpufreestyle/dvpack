"""服务端 FRC（帧率转换）插帧：给现役 DV pack 追加一条高帧率 HLS 变体。

诉求是「利用 4060ti 和 ATV 芯片做实时插针」。落到链路上，光流只能走 macOS 的
VideoToolbox（VTFrameProcessor，私有 API，源码在 tools/src/vtfrc.m），编码只能走
这台 Mac 的 x265 / hevc_videotoolbox——4060ti 不在链路上，ATV 芯片只负责解码播放。
所以本模块做的是服务端 FRC：产出 2 倍帧率的 DV HLS 变体，prepend 进现役
master.m3u8 的首位，Apple TV 侧零改动、直选。

三条实测先验，改代码前先读：

1. vtfrc 的 n 帧输入 -> 2n-1 帧输出（首尾保留源帧，每两帧之间插一帧），
   帧字节数 = W*H*3（p010 bi-planar，与 ffmpeg -pix_fmt p010 rawvideo 逐字节同构）。
2. RPU 必须与输出帧一一对应。dovi_tool inject-rpu 的 RPU 条数与视频帧数不一致时
   （305 帧 / 306 条）会告警并跳过尾部元数据，所以只造恰好 2n-1 个单位，
   不靠尾部的 skipped 兜。
3. master 的变体属性必须照抄原变体。现役 _jf/<hash> 与 _jf.a_nocolr 用的是两套
   约定（前者 CODECS=hvc1...+SUPPLEMENTAL-CODECS 且 URI 绝对；后者
   CODECS=dvh1.05.06,ec-3 且 URI 相对），换约定会被 AVPlayer 整份拒收。

帧数账（按 GOP 切分后逐 worker 累加，合计恰好 2n-1）：
    worker0     fed=T0            emitted=2T0-1   kept=2T0-1
    worker k>=1 fed=Tk+1(多喂一帧凑帧对)  emitted=2Tk+1   kept=2Tk（丢首帧重复）
多喂的那一帧是上一个 chunk 的尾帧；它的插帧产物与上一段末帧重复，由编码侧
select=gte(n,1) 丢掉。这样每个 chunk 的首尾都与源帧严格对齐，合起来不需要
任何时间戳重排。
"""

from __future__ import annotations

import argparse
import os
import posixpath
import re
import shutil
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Sequence

from .bmff import atomic_write, census_annexb, parse_file
from .dplus import apply_dplus, verify_signalling
from .p5 import resign_init_segment
from .playlist import (
    Variant,
    bandwidth_from_segments,
    parse_stream_inf,
    playlist_segment_seconds,
    variant_from_master,
)
from .probe import probe
from .remux import dv_sps_vui_bsf, resolve_output
from .subs import SUBTITLES_GROUP
from .tools import (
    DOVI_TOOL,
    FFMPEG,
    FFPROBE,
    VTFRC,
    ffmpeg_timeout,
    require,
)

#: 光流与编码都是 CPU/GPU 重活，实测 4K50 一帧从源到插帧产物约 0.15s（vtfrc ~17
#: out_fps）+ x265 ultrafast ~15fps。并发 2 路是这台 Mac 的实测上限，再高只会
#: 让两路一起掉出实时。prewarm 的 MAX_JOBS 同款口径：硬顶，不让你踩。
MAX_JOBS = 2

#: 磁盘预留。插帧中途磁盘打满会留下半个 stage 目录，而 serve 的 LRU/换装都只认
#: 完整包；12GiB 是实测整片 4K 链路（BL mp4 + chunks + 注入 + HLS）峰值的上界。
DISK_RESERVE_BYTES = 12 * 1024 * 1024 * 1024

#: 分片时长兜底：源 playlist 没有 #EXT-X-TARGETDURATION 时用它。
SEGMENT_SECONDS = 6.0

#: range 位恒定 limited。remux.source_full_source 对源实时推导，但插帧产物是新
#: 编码的 BL，dv_sps_vui_bsf 与 dplus 期望值都必须用同一个常量，否则四道闸里
#: 第三道（信令复核）会拿两个口径互相打架。
FULL_RANGE = 0

#: 只支持 2 倍。RPU 复制映射（输出第 j 帧取第 j//2 条）与 2n-1 的帧数账都建立在
#: 整倍率上；1.5 倍之类的帧率会让插帧相位与元数据错位。
RATIO = 2

#: 关键帧间隔上限（秒）。x265 的 -g 按目标帧率推：6 秒一个 GOP 与源 pack 的
#: 104 帧 @50fps 同量级，起播与 seek 的手感不变。
GOP_SECONDS = 6.0

#: CLI 编码器名 -> ffmpeg 编码器名。homebrew ffmpeg 里没有裸 "x265"，写错会让
#: ffmpeg 秒退 Unknown encoder 'x265'——症状反而是解码侧 EPIPE 级联报错，极难查。
#: vt 腿与 CLI 同名，直通。
FF_ENCODER_NAMES = {"x265": "libx265", "hevc_videotoolbox": "hevc_videotoolbox"}

_RESULT_RE = re.compile(r"RESULT consumed=(\d+) emitted=(\d+)")


class FlowError(Exception):
    """插帧链路的前置校验或后置闸门失败。

    与 RemuxError 同级：调用方（CLI / serve）只负责转述，不需要分辨是哪一类。
    """


@dataclass(frozen=True)
class ChunkPlan:
    """一个 worker 负责的源帧区间 [start, end)。

    worker0 从 0 起；worker k(k>=1) 的 start 落在关键帧上，为了凑出光流帧对，
    要多喂一帧（fed = end-start+1），于是该段输出首帧与上一段末帧重复，由编码侧
    select=gte(n,1) 丢掉。见模块 docstring 的帧数账。
    """

    index: int
    start: int
    end: int
    fed: int
    emitted: int
    kept: int

    def seek_seconds(self, src_fps: int) -> float:
        """解码侧 -ss：worker0 是 0，其余回退一帧（源帧率下）。"""
        return max(0, self.start - 1) / float(src_fps)


@dataclass
class FlowResult:
    """一次插帧（或一次 remaster）的产出账，供 CLI 打印与调用方记账。"""

    pack: Path
    out_dir: Path
    src_fps: int
    dst_fps: int
    src_frames: int
    dst_frames: int
    chunks: int
    segments: int
    seconds: float
    bandwidth: int
    size_bytes: int
    uri: str
    master: Path
    master_written: bool
    elapsed: float


# --------------------------------------------------------------------------
# 纯函数：RPU 切分 / 交错、帧数账、金丝雀、master 外科手术。测试不打进程。
# --------------------------------------------------------------------------


def split_rpu_units(blob: bytes) -> list[bytes]:
    """把 extract-rpu 的产物按起始码切成一个个 RPU 单位。

    实测 rpu.bin = 每单位 4 字节起始码 00 00 00 01 + payload 首字节 0x19，
    共 4500 单位（与源帧数一致）。切完必须能逐字节拼回原文件——拼不回去说明
    起始码匹配到了假位置，后续 inject 会把元数据发给错误的帧。
    """
    starts = [m.start() for m in re.finditer(b"\x00\x00\x00\x01", blob)]
    if not starts:
        return []
    units = [blob[starts[i]:starts[i + 1]] for i in range(len(starts) - 1)]
    units.append(blob[starts[-1]:])
    if b"".join(units) != blob:
        raise FlowError("RPU 切分不自洽：拼回去与原字节不一致")
    return units


def interleave_rpu_units(units: Sequence[bytes], out_frames: int) -> list[bytes]:
    """把 n 条 RPU 铺成 2n-1 条：输出第 j 帧取第 j//RATIO 条。

    插帧产物排在两个源帧之间（src0, interp, src1, interp, ...），取 floor 等于
    「沿用前一帧的元数据」——元数据不跳到还没显示的帧前面，tone mapping 不会
    超前。要求 out_frames 恰好 2n-1：多给会被 inject-rpu 跳过尾部并告警，
    少给则尾部帧没有 RPU（徽标直接灭）。
    """
    expected = RATIO * len(units) - 1
    if out_frames != expected:
        raise FlowError(
            f"RPU 单位 {len(units)} 个只能撑起 {expected} 帧，要求 {out_frames}"
        )
    return [units[j // RATIO] for j in range(out_frames)]


def plan_chunks(n: int, keys: Sequence[int], jobs: int) -> list[ChunkPlan]:
    """按关键帧把 n 帧等分成至多 MAX_JOBS 段，算出每段的帧数账。

    只认关键帧当切点：裸 .m4s 单独 ffprobe 会报 trun track id unknown，所以
    关键帧必须整 playlist 数，且 GOP 不等长（实测 4500 帧共 45 个 K，间隔 104），
    这里按「第一个 >= 理想切点的关键帧」推进，密度不够就少切一段。
    """
    if n < 2:
        raise FlowError(f"只有 {n} 帧，凑不出一个帧对")
    workers = max(1, min(int(jobs), MAX_JOBS))
    unique = sorted({key for key in keys if 0 <= key < n})
    if not unique or unique[0] != 0:
        raise FlowError("关键帧清单里没有 0 号帧，切分基点不可信")
    starts = [0]
    for k in range(1, workers):
        ideal = (n * k) // workers
        for key in unique:
            if ideal <= key < n and key > starts[-1]:
                starts.append(key)
                break
    bounds = starts + [n]
    plans: list[ChunkPlan] = []
    for index, (start, end) in enumerate(zip(bounds, bounds[1:])):
        # worker0 不需要前一帧；其余多喂一帧凑帧对。
        fed = end - start if index == 0 else end - start + 1
        emitted = RATIO * fed - 1
        kept = emitted if index == 0 else emitted - 1
        plans.append(
            ChunkPlan(index=index, start=start, end=end, fed=fed,
                      emitted=emitted, kept=kept)
        )
    return plans


def parse_vtfrc_result(stderr_text: str) -> tuple[int, int] | None:
    """从 vtfrc 的 stderr 里抓 RESULT consumed=N emitted=M；没有则 None。"""
    match = _RESULT_RE.search(stderr_text)
    if not match:
        return None
    return int(match.group(1)), int(match.group(2))


def check_canary(plan: ChunkPlan, stderr_text: str) -> None:
    """vtfrc 自报帧数必须与切分计划一致，否则整条链路作废。

    不一致意味着 -ss 落点或 -frames:v 取值错了，硬跑下去会得到一段悄悄的
    音画错位——那种包在真机上的症状是「能放但越来越飘」，比放不出来更难查。
    """
    result = parse_vtfrc_result(stderr_text)
    if result is None:
        raise FlowError(
            f"chunk{plan.index}：vtfrc 没打 RESULT 行，金丝雀缺失\n"
            f"{stderr_text[-500:]}"
        )
    consumed, emitted = result
    if consumed != plan.fed or emitted != plan.emitted:
        raise FlowError(
            f"chunk{plan.index}：vtfrc 报 consumed={consumed} emitted={emitted}，"
            f"账面上是 fed={plan.fed} emitted={plan.emitted}\n"
            f"解码侧喂多/喂少了：十有八九是 bl.mp4 的 edit list 把 -ss 落点顶偏，"
            f"先查首帧 pts 是否归零"
        )


def flow_dir_name(dst_fps: int) -> str:
    return f"flow{int(dst_fps)}"


def flow_uri(base_uri: str, dst_fps: int) -> str:
    """flow 变体 URI：与原变体同目录下的 flow<fps>/video.m3u8。

    用 posixpath 而不是 Path：master 里可能是绝对 URI（/_jf/<hash>/video.m3u8），
    Path 会把它当成本地路径折叠掉前导斜杠。
    """
    tag = flow_dir_name(dst_fps)
    return posixpath.join(posixpath.dirname(base_uri), tag, "video.m3u8")


def _detect_newline(text: str) -> str:
    return "\r\n" if "\r\n" in text else "\n"


def strip_flow_lines(master_text: str, tag: str) -> str:
    """摘掉指向 <tag>/ 的旧 flow 变体（幂等清场）。

    serve 的 _pack_full_swap 全片升级后会整目录换装，flow<fps>/ 被丢掉而
    master 回到原样；重新插帧要再 prepend 一次。不清场就会叠出两条指向不存在
    目录的变体，AVPlayer 选到即黑屏。只认「URI 里有 /<tag>/」这一条判据，
    其余行（含字幕轨与其它变体）逐字节保留。
    """
    newline = _detect_newline(master_text)
    lines = master_text.splitlines()
    pattern = re.compile(r"(?:^|/)" + re.escape(tag) + r"/")
    kept: list[str] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        if line.startswith("#EXT-X-STREAM-INF:"):
            cursor = index + 1
            while cursor < len(lines) and not lines[cursor].strip():
                cursor += 1
            uri = lines[cursor].strip() if cursor < len(lines) else ""
            if pattern.search(uri):
                index = cursor + 1
                continue
        elif not line.startswith("#") and pattern.search(line.strip()):
            # 半截写入留下的孤串 URI 行（没有 #EXT-X-STREAM-INF 带头）。
            # 它同样是坏指纹：AVPlayer 选到即黑屏，必须一起清掉。
            index += 1
            continue
        kept.append(line)
        index += 1
    text = newline.join(kept)
    if master_text.endswith(("\n", "\r")):
        text += newline
    return text


def prepend_variant_lines(master_text: str, variant_lines: Sequence[str]) -> str:
    """把变体行插到第一条 #EXT-X-STREAM-INF 之前。

    flow 变体必须排首位：avplayer 按 master 里的顺序挑第一条能满足带宽与解码
    能力的变体，排在后面的高帧率变体在片源本身就能流畅播放时根本不会被选中，
    「插了帧但电视不放」就是这么来的。
    """
    newline = _detect_newline(master_text)
    lines = master_text.splitlines()
    at = next(
        (i for i, line in enumerate(lines) if line.startswith("#EXT-X-STREAM-INF:")),
        None,
    )
    if at is None:
        raise FlowError("master 里没有 #EXT-X-STREAM-INF，flow 变体插不进去")
    text = newline.join(lines[:at] + list(variant_lines) + lines[at:])
    if master_text.endswith(("\n", "\r")):
        text += newline
    return text


def apply_flow_variant(master: Path, *, bandwidth: int, frame_rate: float) -> Variant:
    """把 flow 变体外科手术式 prepend 进 master，返回写进去的 Variant。

    属性一律照抄原变体：variant_from_master 解析回 Variant 后只改 URI /
    BANDWIDTH / FRAME-RATE，CODECS、VIDEO-RANGE、RESOLUTION、HDCP-LEVEL、
    SUPPLEMENTAL-CODECS 全部原样。URI 由原变体 URI 派生，所以绝对/相对两种
    约定都不用调用方操心。
    """
    raw = master.read_text(encoding="utf-8")
    tag = flow_dir_name(int(frame_rate))
    text = strip_flow_lines(raw, tag)
    base = variant_from_master(text)
    inf_line = next(
        line for line in text.splitlines()
        if line.startswith("#EXT-X-STREAM-INF:")
    )
    attrs = parse_stream_inf(inf_line)
    subtitles = attrs.get("SUBTITLES") or None
    if subtitles is None and "#EXT-X-MEDIA:TYPE=SUBTITLES" in text:
        # variant_from_master 不解析 SUBTITLES，主里有字幕轨时得自己补上 group
        # 引用，否则新变体会把整份 master 的字幕轨变成孤儿轨。
        subtitles = SUBTITLES_GROUP
    variant = replace(
        base,
        uri=flow_uri(base.uri, int(frame_rate)),
        bandwidth=int(bandwidth),
        frame_rate=float(frame_rate),
        subtitles=subtitles,
    )
    payload = prepend_variant_lines(text, [variant.stream_inf(), variant.uri])
    # 原子替换：pack  Upgrade / 播放中重写 master 时不能出现 0 字节窗口。
    atomic_write(master, payload.encode("utf-8"))
    return variant


# --------------------------------------------------------------------------
# 外部工具封装
# --------------------------------------------------------------------------


def run_command(
    argv: Sequence[str], *, cwd: Path | None = None, timeout: float | None = None
) -> subprocess.CompletedProcess:
    """跑外部工具；超时/找不到/非零返回码统一转 FlowError（stderr 一并带上）。

    非零 rc 也炸是 2026-10-06 的教训：step 6 重混的 ffmpeg 在 cwd=stage 下
    第二路输入用了相对路径，rc=254 且 stderr 被 capture 吞掉，症状拖到 6 步
    之后才以 init.mp4 缺失的 FileNotFoundError 冒出来，真错误全程不可见。
    """
    cap = timeout if timeout is not None else ffmpeg_timeout()
    try:
        proc = subprocess.run(
            [str(a) for a in argv],
            capture_output=True,
            text=True,
            encoding="utf-8",
            cwd=str(cwd) if cwd else None,
            timeout=cap,
        )
    except subprocess.TimeoutExpired as exc:
        raise FlowError(
            f"执行超时（>{cap:.0f}s 已杀）：{' '.join(str(a) for a in argv[:4])}……"
        ) from exc
    except FileNotFoundError as exc:
        raise FlowError(f"找不到可执行文件：{argv[0]}") from exc
    if proc.returncode != 0:
        raise FlowError(
            f"外部工具失败（rc={proc.returncode}）："
            f"{' '.join(str(a) for a in argv[:4])}……\n{_tail_of(proc.stderr)}"
        )
    return proc


def _tail(path: Path, lines: int = 6) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return "\n".join(text.splitlines()[-lines:])


def count_packets(path: Path) -> int:
    """数一个媒体（裸 HEVC 或 HLS playlist）的视频帧数。"""
    proc = run_command([
        str(require(FFPROBE)), "-v", "error", "-count_packets",
        "-select_streams", "v:0", "-show_entries", "stream=nb_read_packets",
        "-of", "csv=p=0", str(path),
    ])
    if proc.returncode != 0:
        raise FlowError(f"数不出帧数：{path}\n{_tail_of(proc.stderr)}")
    values = [line.strip() for line in proc.stdout.splitlines() if line.strip()]
    if not values:
        raise FlowError(f"{path} 的帧数输出为空")
    # HLS/裸流都会带 side_data_list，csv 模式会多吐一个空字段（"4500,"），
    # 所以只取首个数字而不是整行 int()。
    match = re.search(r"\d+", values[-1])
    if match is None:
        raise FlowError(f"{path} 帧数输出不是数字：{values[-1]!r}")
    return int(match.group())


def keyframe_indices(path: Path) -> list[int]:
    """逐包读 flags，返回关键帧的下标（整 playlist 数，不假设等长 GOP）。"""
    proc = run_command([
        str(require(FFPROBE)), "-v", "error",
        "-select_streams", "v:0", "-show_entries", "packet=flags",
        "-of", "csv=p=0", str(path),
    ])
    if proc.returncode != 0:
        raise FlowError(f"读不出关键帧：{path}\n{_tail_of(proc.stderr)}")
    return [i for i, line in enumerate(proc.stdout.splitlines()) if "K" in line]


def bl_first_pts(path: Path) -> float:
    """BL 裸流重封后的 mp4 首帧 pts；时间轴归零就必须是 0。

    裸 HEVC 带 B 帧时前几帧 dts 为负，直接 -c copy 进 mp4 会把偏移写进 edit
    list（elst media_time>0），presentation 轴比 media 轴早几帧。输入侧 -ss
    走的是 presentation 轴，于是落点整体偏移，chunk 尾帧数悄悄变少。
    """
    proc = run_command([
        str(require(FFPROBE)), "-v", "error",
        "-select_streams", "v:0", "-read_intervals", "%+#1",
        "-show_entries", "packet=pts_time", "-of", "csv=p=0", str(path),
    ])
    if proc.returncode != 0:
        raise FlowError(f"读不出 {path} 首帧 pts\n{_tail_of(proc.stderr)}")
    values = [line.strip() for line in proc.stdout.splitlines() if line.strip()]
    if not values:
        raise FlowError(f"{path} 首帧 pts 输出为空")
    # csv 模式可能带 side_data 尾逗号，取首个数字而不是整行 float()
    match = re.search(r"-?\d+(?:\.\d+)?", values[0])
    if match is None:
        raise FlowError(f"{path} 首帧 pts 不是数字：{values[0]!r}")
    return float(match.group())


def rpu_census(path: Path) -> int:
    """普查裸流里的 DV RPU NAL 条数（NAL 62/63 且 payload 首字节 0x19）。"""
    return census_annexb(path).dovi_rpu


def playlist_segments(playlist: Path) -> list[Path]:
    return list(resolve_output(playlist).segments)


def playlist_seconds(playlist: Path) -> float:
    return sum(playlist_segment_seconds(playlist))


def target_duration(playlist: Path) -> float:
    """源 playlist 的 #EXT-X-TARGETDURATION；拿不到就退到 SEGMENT_SECONDS。"""
    for line in playlist.read_text(encoding="utf-8").splitlines():
        if line.startswith("#EXT-X-TARGETDURATION:"):
            try:
                return float(line.split(":", 1)[1].strip())
            except ValueError:
                break
    return SEGMENT_SECONDS


def _tail_of(text: str | None, lines: int = 6) -> str:
    if not text:
        return ""
    return "\n".join(text.splitlines()[-lines:])


def _pack_bytes(pack: Path) -> int:
    total = 0
    try:
        entries = list(pack.iterdir())
    except OSError:
        return 0
    for entry in entries:
        try:
            if entry.is_file():
                total += entry.stat().st_size
        except OSError:
            continue
    return total


def _disk_gate(pack: Path, source_bytes: int) -> None:
    """磁盘闸：插帧要 BL + 2 倍帧数的编码产物 + 注入副本，峰值约源包 2 倍。"""
    from .serve import _free_bytes  # 局部 import：serve 依赖面大，避免循环

    free = _free_bytes(pack)
    need = max(DISK_RESERVE_BYTES, source_bytes * 2 + DISK_RESERVE_BYTES)
    if free < need:
        raise FlowError(
            f"磁盘只剩 {free / 2**30:.1f}GiB，插帧需要 {need / 2**30:.1f}GiB"
            f"（源包 {source_bytes / 2**30:.1f}GiB x2 + {DISK_RESERVE_BYTES / 2**30:.0f}GiB 预留）"
        )


def _int_fps(value: float) -> int:
    """vtfrc 的 SRC/DST 走 atoi，非整数帧率会被静默截断——入口就拦掉。"""
    if value <= 0:
        raise FlowError("读不到帧率（init.mp4 没报 avg/r_frame_rate）")
    rounded = int(round(value))
    if abs(value - rounded) > 1e-6:
        raise FlowError(f"帧率 {value} 不是整数：vtfrc 的 SRC/DST 走 atoi 会截断")
    return rounded


def _worker_timeout(out_frames: int, dst_fps: int) -> float:
    """按目标帧率给 8 倍时长余量（vtfrc + x265 都是分钟级重活）。"""
    return ffmpeg_timeout(out_frames / float(dst_fps) * 8 + 300)


# --------------------------------------------------------------------------
# 三段 worker：解码 -> vtfrc 光流 -> 编码。进程间内核管道直连。
# --------------------------------------------------------------------------


def _bl_remux_argv(src_fps: int, bl_hevc: Path, bl_mp4: Path) -> list[str]:
    """BL 裸流重封 mp4 的 argv；时间轴必须归零，否则 -ss 落点整段偏移。

    裸 HEVC 带 B 帧时前几帧 dts 为负，直接 -c copy 进 mp4 会把这截偏移写进
    edit list（elst media_time>0）：presentation 轴比 media 轴早几帧，而输入侧
    -ss 走 presentation 轴，于是落点整体后移，chunk 尾帧数悄悄变少
    （实测 2169 -> 2166）。make_zero 后 mp4 无 elst、首帧 pts=0，
    -ss (n-1)/fps 才精确落在第 n-1 帧上。
    """
    return [
        str(require(FFMPEG)), "-v", "error", "-y",
        "-framerate", str(src_fps), "-i", str(bl_hevc),
        "-avoid_negative_ts", "make_zero",
        "-c", "copy", str(bl_mp4),
    ]


def _chunk_encode_argv(
    index: int, chunk: Path, W: int, H: int, dst_fps: int, encoder: str, preset: str
) -> list[str]:
    argv = [
        str(require(FFMPEG)), "-v", "error", "-y",
        "-f", "rawvideo", "-pix_fmt", "p010", "-s", f"{W}x{H}",
        "-framerate", str(dst_fps), "-i", "-",
    ]
    if index >= 1:
        # 该段首帧与上一段末帧是同一个源帧的插帧产物，重复；丢掉它。
        argv += ["-vf", "select=gte(n\\,1)", "-fps_mode", "passthrough"]
    argv += [
        "-an", "-c:v", FF_ENCODER_NAMES.get(encoder, encoder),
        "-preset", preset, "-pix_fmt", "p010",
        "-g", str(int(round(dst_fps * GOP_SECONDS))),
        "-tag:v", "dvh1", "-strict", "unofficial",
        "-bsf:v", dv_sps_vui_bsf(FULL_RANGE)[0],
    ]
    if encoder == "x265":
        # x265 自己的日志会把 vtfrc 的金丝雀埋在噪音里；私有选项必须放在输出
        # 文件之前，放后面 ffmpeg 会当成尾随参数直接忽略。
        argv += ["-x265-params", "log-level=error"]
    argv += ["-f", "hevc", str(chunk)]
    return argv


def _run_chunk(
    stage: Path,
    plan: ChunkPlan,
    *,
    W: int,
    H: int,
    src_fps: int,
    dst_fps: int,
    quality: int,
    encoder: str,
    preset: str,
    timeout: float,
) -> None:
    """跑一个 worker 的三段管道，并按金丝雀与落盘帧数复核。

    禁 shell=True，三段进程用内核管道直连：中间那份 p010 裸流在 4K 下是
    W*H*3 = 24.9MB/帧，落临时文件会把磁盘和 IO 全吃穿。
    """
    bl = stage / "bl.mp4"
    chunk = stage / f"chunk{plan.index}.hevc"
    log_path = stage / f"chunk{plan.index}.log"
    dec = [
        str(require(FFMPEG)), "-v", "error", "-y",
        "-ss", f"{plan.seek_seconds(src_fps):.9f}", "-i", str(bl),
        "-frames:v", str(plan.fed),
        "-f", "rawvideo", "-pix_fmt", "p010", "-",
    ]
    frc = [
        str(require(VTFRC)), str(W), str(H), str(src_fps), str(dst_fps),
        "--quality", str(quality),
    ]
    enc = _chunk_encode_argv(plan.index, chunk, W, H, dst_fps, encoder, preset)
    canary: list[str] = []
    procs = []
    with log_path.open("wb") as log:
        try:
            dec_proc = subprocess.Popen(dec, stdout=subprocess.PIPE, stderr=log)
            frc_proc = subprocess.Popen(
                frc, stdin=dec_proc.stdout, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            dec_proc.stdout.close()  # 父进程不留副本，否则 EOF 传不过去
            enc_proc = subprocess.Popen(enc, stdin=frc_proc.stdout, stderr=log)
            frc_proc.stdout.close()
            procs = [dec_proc, frc_proc, enc_proc]

            def _pump() -> None:
                for raw in iter(frc_proc.stderr.readline, b""):
                    canary.append(raw.decode("utf-8", "replace"))

            watcher = threading.Thread(target=_pump, daemon=True)
            watcher.start()
            try:
                enc_rc = enc_proc.wait(timeout=timeout)
                frc_rc = frc_proc.wait(timeout=timeout)
                dec_rc = dec_proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                for proc in procs:
                    proc.kill()
                raise FlowError(
                    f"chunk{plan.index} 三段管道执行超时（>{timeout:.0f}s）"
                )
            watcher.join(timeout=5.0)
        except BaseException:
            for proc in procs:
                if proc.poll() is None:
                    proc.kill()
            raise

    if dec_rc != 0:
        raise FlowError(f"chunk{plan.index} 解码失败（rc={dec_rc}）\n{_tail(log_path)}")
    if frc_rc != 0:
        raise FlowError(
            f"chunk{plan.index} vtfrc 失败（rc={frc_rc}）\n{''.join(canary)[-500:]}"
        )
    if enc_rc != 0:
        raise FlowError(f"chunk{plan.index} 编码失败（rc={enc_rc}）\n{_tail(log_path)}")
    check_canary(plan, "".join(canary))
    kept = count_packets(chunk)
    if kept != plan.kept:
        raise FlowError(
            f"chunk{plan.index} 落盘 {kept} 帧，账面上是 {plan.kept} 帧"
        )


# --------------------------------------------------------------------------
# 主链路
# --------------------------------------------------------------------------



def _mp4_remux_argv(*, dv_full: Path, dv_mp4: Path, dst_fps: int) -> list[str]:
    """step 6a argv：插帧后的裸 HEVC 先封成 mp4，把逐帧时长补实。

    裸 HEVC 输入的 demuxer 只给 pts、不填 pkt duration，ffmpeg hls muxer 按
    pkt duration 写 #EXTINF——裸流直进 HLS 会写出 #EXT-X-TARGETDURATION:0 /
    #EXTINF:0.000000（/tmp/extinfmtx 矩阵实测：去 genpts、输出 -r、
    fps_mode passthrough、avoid_negative_ts 全部无效，rc 还是 0），ATV 收到
    零时长列表直接播放失败。经一道 mov 容器，muxer 按帧间隔把 stts 时长写实，
    再进 HLS 才拿得到非零逐包时长（同矩阵 V5 实测 EXTINF 0.2；4K 全量 6s
    段实测 EXTINF 6.2/6.4、TARGETDURATION 6）。

    -framerate dst_fps 与 _bl_remux_argv 同款，是兜底不是主令：VUI 缺位时顶上，
    VUI 在位时 demuxer 听 VUI 的 time_scale（实测 VUI=50 的裸流，输入写
    -framerate 100，pts 仍是 20ms 间隔）——所以 build 里紧跟一道
    avg_frame_rate 复核，对不上 dst_fps 当场 FlowError，绝不允许两倍速
    EXTINF 流到电视上。
    -avoid_negative_ts make_zero 与 BL 同款：x265 带 B 帧，前几帧 dts 为负，
    不归零会把偏移写进 elst，presentation 轴比 media 轴早几帧。

    两路路径都 resolve()：与 _hls_remux_argv 同一条纪律，禁相对路径。
    """
    return [
        str(require(FFMPEG)), "-v", "error", "-y",
        "-framerate", str(dst_fps), "-f", "hevc", "-i", str(dv_full.resolve()),
        "-avoid_negative_ts", "make_zero",
        "-c", "copy", str(dv_mp4.resolve()),
    ]


def _hls_remux_argv(
    *,
    video_mp4: Path,
    src_playlist: Path,
    seg_seconds: float,
) -> list[str]:
    """step 6b argv：带时长的 dv_full.mp4 + 源 playlist 音频 → fMP4 HLS。

    第一路输入是 mp4 不是裸流（见 _mp4_remux_argv：裸流 demuxer 不填 pkt
    duration，直进 hls muxer 会写零时长 EXTINF），所以既不需要 -f hevc 也不
    需要 -framerate——mp4 demuxer 自带时间轴。两路输入一律 resolve() 成绝对
    路径：本命令带 cwd=stage，相对路径的源 playlist 会在 stage 里被吞掉
    （实测 rc=254，ffmpeg 什么都写不出来，下游只看到 init.mp4 缺失）。
    输入跟 cwd 解耦，只认绝对路径。
    """
    return [
        str(require(FFMPEG)), "-v", "error", "-y",
        "-i", str(video_mp4.resolve()),
        "-i", str(src_playlist.resolve()),
        "-map", "0:v:0", "-map", "1:a:0?",
        "-c", "copy", "-tag:v", "dvh1", "-strict", "unofficial", "-c:a", "copy",
        "-f", "hls", "-hls_segment_type", "fmp4",
        "-hls_time", str(seg_seconds), "-hls_list_size", "0",
        "-hls_playlist_type", "vod", "-hls_fmp4_init_filename", "init.mp4",
        "video.m3u8",
    ]


def _build_flow(
    pack: Path,
    stage: Path,
    *,
    W: int,
    H: int,
    src_fps: int,
    dst_fps: int,
    level: int,
    src_frames: int,
    plans: Sequence[ChunkPlan],
    quality: int,
    encoder: str,
    preset: str,
    log,
) -> Path:
    """把源 pack 走完 抽流->去 RPU->光流->编码->注入->重混，产物全落 stage。

    返回重混后的 media playlist 路径（stage/video.m3u8）。
    """
    video_playlist = pack / "video.m3u8"
    seg_seconds = target_duration(video_playlist)
    src_hevc = stage / "src.hevc"
    rpu_bin = stage / "rpu.bin"
    bl_hevc = stage / "bl.hevc"
    bl_mp4 = stage / "bl.mp4"
    dv_hevc = stage / "dv.hevc"
    dv_rpu = stage / "dv_rpu.bin"
    dv_full = stage / "dv_full.hevc"
    dv_full_mp4 = stage / "dv_full.mp4"
    out_frames = RATIO * src_frames - 1

    log(f"[flow] {pack.name}：{src_fps}->{dst_fps}fps，{src_frames}->{out_frames} 帧，"
        f"{len(plans)} 段，{W}x{H}")

    log("[flow] 1/6 抽裸 HEVC（fMP4 -> Annex-B）")
    run_command([
        str(require(FFMPEG)), "-v", "error", "-y", "-i", str(video_playlist),
        "-map", "0:v:0", "-c", "copy", "-f", "hevc", str(src_hevc),
    ])
    if not src_hevc.is_file() or src_hevc.stat().st_size == 0:
        raise FlowError("抽裸 HEVC 得到空文件：源 pack 的视频轨不可拷贝")

    log("[flow] 2/6 摘 RPU / 去增强层，留 BL")
    run_command([
        str(require(DOVI_TOOL)), "extract-rpu", "-i", str(src_hevc), "-o", str(rpu_bin),
    ])
    run_command([
        str(require(DOVI_TOOL)), "remove", "-i", str(src_hevc), "-o", str(bl_hevc),
    ])
    os.unlink(src_hevc)
    units = split_rpu_units(rpu_bin.read_bytes())
    if len(units) != src_frames:
        raise FlowError(f"RPU 单位 {len(units)} 个 != 源帧数 {src_frames}")
    del units

    log("[flow] 3/6 BL 重封 mp4（让输入侧 -ss 逐帧精确）")
    run_command(_bl_remux_argv(src_fps, bl_hevc, bl_mp4))
    os.unlink(bl_hevc)
    first_pts = bl_first_pts(bl_mp4)
    if abs(first_pts) > 1e-6:
        raise FlowError(
            f"BL mp4 首帧 pts={first_pts}：时间轴没归零，-ss 落点会偏；"
            f"-avoid_negative_ts make_zero 没生效？"
        )

    log(f"[flow] 4/6 光流插帧 + 编码（{encoder}/{preset}）")
    timeout = _worker_timeout(out_frames, dst_fps)
    if len(plans) == 1:
        _run_chunk(
            stage, plans[0], W=W, H=H, src_fps=src_fps, dst_fps=dst_fps,
            quality=quality, encoder=encoder, preset=preset, timeout=timeout,
        )
    else:
        with ThreadPoolExecutor(max_workers=len(plans)) as pool:
            futures = [
                pool.submit(
                    _run_chunk, stage, plan, W=W, H=H, src_fps=src_fps,
                    dst_fps=dst_fps, quality=quality, encoder=encoder,
                    preset=preset, timeout=timeout,
                )
                for plan in plans
            ]
            for future in futures:
                future.result()

    log("[flow] 5/6 拼段 + RPU 交错 + inject-rpu")
    with dv_hevc.open("wb") as out:
        for plan in plans:
            part = stage / f"chunk{plan.index}.hevc"
            with part.open("rb") as handle:
                shutil.copyfileobj(handle, out, 16 * 1024 * 1024)
    interleaved = interleave_rpu_units(split_rpu_units(rpu_bin.read_bytes()), out_frames)
    dv_rpu.write_bytes(b"".join(interleaved))
    del interleaved
    os.unlink(rpu_bin)
    run_command([
        str(require(DOVI_TOOL)), "inject-rpu", "-i", str(dv_hevc),
        "-r", str(dv_rpu), "-o", str(dv_full),
    ], timeout=ffmpeg_timeout(out_frames / float(dst_fps) * 10 + 300))
    os.unlink(dv_hevc)

    log("[flow] 6/6 裸流先封 mp4 补时长，再重混 HLS（音频从源 playlist 拷，时长不变所以音画同步）")
    run_command(
        _mp4_remux_argv(dv_full=dv_full, dv_mp4=dv_full_mp4, dst_fps=dst_fps),
        timeout=ffmpeg_timeout(out_frames / float(dst_fps) * 10 + 300),
    )
    if not dv_full_mp4.is_file() or dv_full_mp4.stat().st_size == 0:
        raise FlowError("裸流重封 mp4 得到空文件：dv_full.hevc 不可拷贝")
    mp4_fps = _int_fps(probe(dv_full_mp4).video.frame_rate)
    if mp4_fps != dst_fps:
        raise FlowError(
            f"dv_full.mp4 帧率 {mp4_fps} != {dst_fps}：裸流 VUI 的 time_scale 不对"
            f"（-framerate 压不过 VUI），拿它重混会把 EXTINF 写成"
            f"{dst_fps / mp4_fps:.0f} 倍速、音画错位"
        )
    run_command(
        _hls_remux_argv(
            video_mp4=dv_full_mp4,
            src_playlist=video_playlist,
            seg_seconds=seg_seconds,
        ),
        cwd=stage,
        timeout=ffmpeg_timeout(out_frames / float(dst_fps) * 10 + 300),
    )

    resign_init_segment(
        stage / "init.mp4", profile=5, compat_id=0, el_present=False, level=level
    )
    return stage / "video.m3u8"


def _verify_es(stage: Path) -> None:
    """换装前最后一道：stage 里只许有 HLS 产物。

    中间文件（bl.mp4、chunk*.hevc、dv*.hevc、日志）漏进去会被 serve 当分片床
    出去，也会污染 flow 目录的体积账。
    """
    for entry in sorted(stage.iterdir()):
        if entry.name == "init.mp4" or entry.name.startswith("video"):
            continue
        raise FlowError(f"stage 里多了 {entry.name}，拒绝换装")


def _cleanup_intermediates(stage: Path, plans: Sequence[ChunkPlan]) -> None:
    names = [
        "bl.mp4", "rpu.bin", "dv_rpu.bin", "dv.hevc", "dv_full.hevc", "dv_full.mp4",
    ]
    for plan in plans:
        names.append(f"chunk{plan.index}.hevc")
        names.append(f"chunk{plan.index}.log")
    for name in names:
        path = stage / name
        if path.exists():
            os.unlink(path)


def _assert_segment_durations(playlist: Path, *, out_frames: int, dst_fps: int) -> None:
    """EXTINF 看门狗：零时长、缺 TARGETDURATION、总时长对不上账，整包作废。

    2026-10-06 EXTINF=0 事故的回归闸：裸 HEVC 直进 hls muxer，demuxer 只给
    pts 不填 pkt duration，ffmpeg rc=0 但写出 #EXT-X-TARGETDURATION:0 /
    #EXTINF:0.000000（/tmp/extinfmtx 矩阵 V1-V4/V6 实测全灭），ATV 收到
    零时长列表直接播放失败。修法是两步法（_mp4_remux_argv 先封 mp4，mov
    muxer 把 stts 时长写实），这道闸保证修法哪天被改回去会当场炸，而不是把
    废品流上市。

    总时长按 out_frames/dst_fps 对账，不按 -hls_time：hls muxer 按关键帧切
    段，段长会在 GOP 边界上浮动（实测 6.24s 目标切出 6.2/6.4s 的段），只有
    合计是守恒量。
    """
    durations = playlist_segment_seconds(playlist)
    if not durations:
        raise FlowError(f"{playlist.name} 里读不出 #EXTINF：空播放列表")
    zero = [value for value in durations if value <= 0]
    if zero:
        raise FlowError(
            f"{len(zero)}/{len(durations)} 段 EXTINF <= 0："
            f"裸流直进 HLS 的老 bug 回来了"
        )
    target = 0.0
    for line in playlist.read_text(encoding="utf-8").splitlines():
        if not line.startswith("#EXT-X-TARGETDURATION:"):
            continue
        try:
            target = float(line.split(":", 1)[1].strip())
        except ValueError as exc:
            raise FlowError(f"#EXT-X-TARGETDURATION 行读不懂：{line!r}") from exc
    if target <= 0:
        raise FlowError(
            "#EXT-X-TARGETDURATION 缺失或 <= 0：零时长列表过不了播放器"
        )
    expected = out_frames / float(dst_fps)
    total = sum(durations)
    if abs(total - expected) > 0.5:
        raise FlowError(
            f"EXTINF 合计 {total:.3f}s，账面上 {expected:.3f}s"
            f"（{out_frames} 帧 / {dst_fps}fps）"
        )


def _verify_gates(
    stage: Path, *, out_frames: int, dst_fps: int, dv_full: Path, level: int, log
) -> None:
    """五道闸：帧数、RPU、信令、init 段、EXTINF 时长。任何一道不过就整包作废。"""
    playlist = stage / "video.m3u8"
    init = stage / "init.mp4"
    for path in (playlist, init):
        if not path.is_file():
            raise FlowError(f"stage 里缺 {path.name}")

    log("[flow] 闸 1/4 帧数")
    frames = count_packets(playlist)
    if frames != out_frames:
        raise FlowError(f"HLS 产物 {frames} 帧，账面上是 {out_frames} 帧")

    log("[flow] 闸 2/4 RPU 普查")
    rpu = rpu_census(dv_full)
    if rpu != out_frames:
        raise FlowError(f"RPU {rpu} 条，输出 {out_frames} 帧——尾帧没有元数据徽标会灭")

    log("[flow] 闸 3/4 DV 信令")
    report = verify_signalling(playlist, full_range=FULL_RANGE)
    if not report.ok:
        # hevc_videotoolbox 会把 PTL 的 non_packed 位置 1（x265 不会）。
        # 先试 dplus 自愈，仍不过才判死——否则只能用 x265。
        log(f"[flow] 信令不合规，试 dplus 自愈：{'; '.join(report.problems)}")
        apply_dplus(resolve_output(playlist), full_range=FULL_RANGE)
        retry = verify_signalling(playlist, full_range=FULL_RANGE)
        if not retry.ok:
            raise FlowError("DV 信令不合规且自愈无效：" + "; ".join(retry.problems))

    log("[flow] 闸 4/4 init 段")
    resign_init_segment(init, profile=5, compat_id=0, el_present=False, level=level)
    info = probe(init)
    dovi = info.dovi
    if dovi is None or dovi.profile != 5 or not dovi.rpu_present or dovi.el_present:
        raise FlowError(f"init 段复读不出 P5 单层信令：{dovi}")
    if dovi.level != level:
        raise FlowError(f"init 段 level {dovi.level} != 源 level {level}")
    report_bmff = parse_file(init)
    video = report_bmff.video
    if video is None:
        raise FlowError("init 段里没有视频轨")
    if video.sample_entry_type != "dvh1":
        raise FlowError(
            f"sample entry 是 {video.sample_entry_type}，不是 dvh1——电视不会走 DV 解码通路"
        )
    if not video.has_dvcC:
        raise FlowError("init 段缺规范名 dvcC 配置 box")

    log("[flow] 闸 5/5 EXTINF 时长")
    _assert_segment_durations(playlist, out_frames=out_frames, dst_fps=dst_fps)


def flow_pack(
    pack_dir: str | Path,
    *,
    dst_fps: int = 0,
    jobs: int = MAX_JOBS,
    quality: int = 2,
    encoder: str = "x265",
    preset: str = "ultrafast",
    force: bool = False,
    log=print,
) -> FlowResult:
    """给一个现役 pack 追加 flow<fps>/ 高帧率变体，并 prepend 进 master。

    dst_fps=0 表示自动取 2 倍源帧率。整条链路幂等：flow<fps>/ 已存在时直接
    FlowError，确认重来传 force（旧目录在开跑前删，失败不留半成品）。
    """
    started = time.time()
    pack = Path(pack_dir)
    for name in ("master.m3u8", "video.m3u8", "init.mp4"):
        if not (pack / name).is_file():
            raise FlowError(f"{pack} 不是完整 pack：缺 {name}")

    info = probe(pack / "init.mp4")
    dovi = info.dovi
    if dovi is None:
        raise FlowError(f"{pack / 'init.mp4'} 没有 DOVI configuration record")
    if dovi.profile != 5:
        raise FlowError(
            f"源是 Profile {dovi.profile}：插帧链路只服务已转签成 P5 的 pack"
        )
    if not dovi.rpu_present:
        raise FlowError("RPU 缺失：插帧没有元数据可复制")
    if dovi.el_present:
        raise FlowError("带增强层（双层）不在本链路范围")
    W, H = info.video.width, info.video.height
    if W <= 0 or H <= 0 or W % 2 or H % 2:
        raise FlowError(f"尺寸 {W}x{H} 非法：vtfrc 只吃偶数边的 p010")

    src_fps = _int_fps(info.video.frame_rate)
    want = RATIO * src_fps if not dst_fps else int(dst_fps)
    if want != RATIO * src_fps:
        raise FlowError(
            f"--fps {want} 不支持：只做 {RATIO} 倍（源 {src_fps} -> {RATIO * src_fps}）。"
            "RPU 复制映射与 2n-1 帧数账只对整倍率成立"
        )
    dst_fps = want

    video_playlist = pack / "video.m3u8"
    src_frames = count_packets(video_playlist)
    out_frames = RATIO * src_frames - 1
    plans = plan_chunks(src_frames, keyframe_indices(video_playlist), jobs)

    tag = flow_dir_name(dst_fps)
    out_dir = pack / tag
    stage = pack / f".{tag}.tmp"
    if out_dir.exists():
        if not force:
            raise FlowError(f"{out_dir} 已存在；确认重来加 --force")
        shutil.rmtree(out_dir)

    _disk_gate(pack, _pack_bytes(pack))

    try:
        if stage.exists():
            shutil.rmtree(stage)
        stage.mkdir(parents=True)
        dv_full = stage / "dv_full.hevc"
        _build_flow(
            pack, stage, W=W, H=H, src_fps=src_fps, dst_fps=dst_fps,
            level=dovi.level, src_frames=src_frames, plans=plans,
            quality=quality, encoder=encoder, preset=preset, log=log,
        )
        _verify_gates(stage, out_frames=out_frames, dst_fps=dst_fps,
                      dv_full=dv_full, level=dovi.level, log=log)
        _cleanup_intermediates(stage, plans)
        _verify_es(stage)
        os.replace(stage, out_dir)
    except BaseException:
        # 无论哪种异常（含 KeyboardInterrupt）都不留半成品：serve 的 LRU 与
        # _dir_size_bytes 会把它当成真包统计。
        shutil.rmtree(stage, ignore_errors=True)
        raise

    uri = flow_uri(variant_from_master(
        strip_flow_lines((pack / "master.m3u8").read_text(encoding="utf-8"), tag)
    ).uri, dst_fps)
    segments = playlist_segments(out_dir / "video.m3u8")
    seconds = playlist_seconds(out_dir / "video.m3u8")
    bandwidth = bandwidth_from_segments(
        segments, seconds, playlist_segment_seconds(out_dir / "video.m3u8")
    )
    variant = apply_flow_variant(
        pack / "master.m3u8", bandwidth=bandwidth, frame_rate=dst_fps
    )
    size_bytes = sum(path.stat().st_size for path in out_dir.iterdir())
    log(f"[flow] 完成：{out_dir}（{len(segments)} 段 / {seconds:.1f}s / "
        f"{size_bytes / 2**20:.0f}MiB / BANDWIDTH={bandwidth}）")
    log(f"[flow] master 已 prepend：{variant.stream_inf()}")
    return FlowResult(
        pack=pack, out_dir=out_dir, src_fps=src_fps, dst_fps=dst_fps,
        src_frames=src_frames, dst_frames=out_frames, chunks=len(plans),
        segments=len(segments), seconds=seconds, bandwidth=bandwidth,
        size_bytes=size_bytes, uri=uri, master=pack / "master.m3u8",
        master_written=True, elapsed=time.time() - started,
    )


def remaster_only(
    pack_dir: str | Path, *, dst_fps: int = 0, log=print
) -> FlowResult:
    """只把 flow 行补回 master，秒级。

    serve 的 _pack_full_swap 全片升级会整目录换装，flow<fps>/ 被丢掉；重新插帧
    要 8-12 分钟，而这个只重写一行 master。产物必须在位，否则 FlowError。
    """
    started = time.time()
    pack = Path(pack_dir)
    for name in ("master.m3u8", "video.m3u8", "init.mp4"):
        if not (pack / name).is_file():
            raise FlowError(f"{pack} 不是完整 pack：缺 {name}")
    src_fps = _int_fps(probe(pack / "init.mp4").video.frame_rate)
    dst = RATIO * src_fps if not dst_fps else int(dst_fps)
    tag = flow_dir_name(dst)
    out_dir = pack / tag
    playlist = out_dir / "video.m3u8"
    if not playlist.is_file():
        raise FlowError(f"{playlist} 不在位：先插帧，或换一个 --fps")
    segments = playlist_segments(playlist)
    seconds = playlist_seconds(playlist)
    bandwidth = bandwidth_from_segments(
        segments, seconds, playlist_segment_seconds(playlist)
    )
    variant = apply_flow_variant(pack / "master.m3u8", bandwidth=bandwidth,
                                frame_rate=dst)
    size_bytes = sum(path.stat().st_size for path in out_dir.iterdir())
    log(f"[flow] master 已补行：{variant.stream_inf()} -> {variant.uri}")
    return FlowResult(
        pack=pack, out_dir=out_dir, src_fps=src_fps, dst_fps=dst,
        src_frames=0, dst_frames=0, chunks=0, segments=len(segments),
        seconds=seconds, bandwidth=bandwidth, size_bytes=size_bytes,
        uri=variant.uri, master=pack / "master.m3u8", master_written=True,
        elapsed=time.time() - started,
    )


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def add_arguments(parser: argparse.ArgumentParser) -> None:
    """flow 参数定义。python -m dvpack.flow 直跑与 dvpack flow 共用。"""
    parser.add_argument("pack", type=Path,
                        help="pack 目录（含 master.m3u8 / video.m3u8 / init.mp4）")
    parser.add_argument("--fps", type=int, default=0,
                        help="目标帧率；0=自动取 2 倍源帧率")
    parser.add_argument("--jobs", type=int, default=MAX_JOBS,
                        help=f"并发 worker 数，默认 {MAX_JOBS}，硬顶 {MAX_JOBS}")
    parser.add_argument("--quality", type=int, default=2, choices=(1, 2),
                        help="vtfrc 画质优先级：1=优先速度，2=优先画质")
    parser.add_argument("--enc", default="x265", choices=("x265", "hevc_videotoolbox"),
                        help="编码器；vt 会让 PTL non_packed 置 1，靠 dplus 自愈兜")
    parser.add_argument("--preset", default="ultrafast",
                        help="x265 preset；vt 腿忽略")
    parser.add_argument("--force", action="store_true",
                        help="flow<fps>/ 已存在时删旧重来")
    parser.add_argument("--remaster", action="store_true",
                        help="只把 flow 行补回 master（秒级），不重新插帧")


def run_from_args(args) -> int:
    pack = Path(args.pack)
    try:
        if getattr(args, "remaster", False):
            result = remaster_only(pack, dst_fps=args.fps)
            print(f"补行完成：{result.out_dir}"
                  f"（{result.segments} 段 / {result.seconds:.1f}s，"
                  f"耗时 {result.elapsed:.1f}s）")
            return 0
        result = flow_pack(
            pack, dst_fps=args.fps, jobs=args.jobs, quality=args.quality,
            encoder=args.enc, preset=args.preset, force=args.force,
        )
    except FlowError as exc:
        print(f"失败：{exc}", file=sys.stderr)
        return 2
    print(f"插帧完成：{result.src_fps}->{result.dst_fps}fps，"
          f"{result.src_frames}->{result.dst_frames} 帧，"
          f"{result.chunks} 段并发，耗时 {result.elapsed / 60:.1f} 分钟")
    print(f"  aTV 填 : http://<mac-ip>:<port>/{result.uri.lstrip('/')}")
    return 0 if result.master_written else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="dvpack flow",
        description="服务端 FRC 插帧：给现役 DV pack 追加 2 倍帧率 HLS 变体")
    add_arguments(parser)
    return run_from_args(parser.parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())

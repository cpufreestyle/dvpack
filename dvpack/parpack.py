"""Parallel chunked packing: split the source by time into N chunks and
run an independent ffmpeg per chunk so the HTTP pull from Jellyfin happens on
N connections at once.

Why: single-process packing of a 5GB / 106-min source takes 10+ minutes and the
ffmpeg process only uses ~5% CPU. The bottleneck is not decoding (-c copy never
re-encodes), it is one HTTP connection pulling data sequentially. Running N
chunks concurrently cuts wall clock to roughly 1/N.

Chunk internals reuse the exact same flags as the single-process path (same
BSF chain, same DV signalling, same colr/range policy); this module only adds
splitting + stitching.
"""
from __future__ import annotations

import os
import re
import shutil
import struct
import math
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import replace
from pathlib import Path
from typing import Mapping

from .bmff import clamp_dvcC_level, expected_dv_level
from .bmff import (clear_compressor_name, keep_single_sample_entry,
                   locate_dv_boxes, sample_entry_count)
from .bmff import normalize_sidx, verify_sidx
from .playlist import (Variant, bandwidth_from_segments, hevc_base_codec,
                       variant_from_source, write_master_playlist)
from .probe import UnknownTransfer
from .p5 import resign_pack
from .remux import (
    FFMPEG,
    FFPROBE,
    VIDEO_AUDIO_MAPS,
    _run,
    ffmpeg_timeout,
    require,
)
from .tools import probe_timeout
from .subs import write_subtitle_tracks


class ParallelPackError(RuntimeError):
    pass


def probe_duration(src):
    proc = _run([
        str(require(FFPROBE)), "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        src,
    ], timeout=probe_timeout())
    if proc.returncode != 0:
        raise ParallelPackError("ffprobe failed: " + proc.stderr.strip()[:300])
    try:
        return float(proc.stdout.strip())
    except ValueError as exc:
        raise ParallelPackError("bad duration: " + proc.stdout.strip()) from exc


def _iter_boxes(data, start, end):
    """Yield (offset, fourcc, size, header_size) for each box in [start, end).

    Parses the real box tree instead of scanning for a fourcc with
    bytes.find(): a plain find cannot tell which traf a tfdt belongs to, so
    every track ends up shifted by whatever single offset the caller passed.
    """
    i = start
    while i + 8 <= end:
        size = struct.unpack_from(">I", data, i)[0]
        header = 8
        if size == 1:
            size = struct.unpack_from(">Q", data, i + 8)[0]
            header = 16
        elif size == 0:
            size = end - i
        if size < header or i + size > end:
            return
        yield i, bytes(data[i + 4:i + 8]), size, header
        i += size


def track_timescales(init_path):
    """Map track_ID -> mdhd timescale for every track in an init segment.

    tfdt is denominated in its own track's timescale, and no source seen so
    far shares one value between tracks: video runs 12800/16000 while audio
    runs 48000. Shifting both by the video timescale desyncs the audio by
    3-4x, which is why this returns a table instead of a number.
    """
    data = Path(init_path).read_bytes()
    scales = {}
    for moov_off, box_type, box_size, box_hdr in _iter_boxes(data, 0, len(data)):
        if box_type != b"moov":
            continue
        for trak_off, trak_type, trak_size, trak_hdr in _iter_boxes(
                data, moov_off + box_hdr, moov_off + box_size):
            if trak_type != b"trak":
                continue
            track_id = None
            timescale = None
            for child_off, child_type, child_size, child_hdr in _iter_boxes(
                    data, trak_off + trak_hdr, trak_off + trak_size):
                if child_type == b"tkhd":
                    body = child_off + child_hdr
                    version = data[body]
                    track_id = struct.unpack_from(
                        ">I", data, body + (20 if version == 1 else 12))[0]
                elif child_type == b"mdia":
                    for mdia_off, mdia_type, mdia_size, mdia_hdr in _iter_boxes(
                            data, child_off + child_hdr, child_off + child_size):
                        if mdia_type != b"mdhd":
                            continue
                        body = mdia_off + mdia_hdr
                        version = data[body]
                        timescale = struct.unpack_from(
                            ">I", data, body + (20 if version == 1 else 12))[0]
            if track_id is not None and timescale:
                scales[track_id] = timescale
    if not scales:
        raise ParallelPackError("init segment carries no mdhd timescale")
    return scales


def read_tfdt(seg_path):
    """Return [(track_ID, version, base_media_decode_time), ...] for a segment.

    Walks moof -> traf -> tfhd/tfdt so a segment holding several tracks (or
    several traf per track) reports every one of them.
    """
    data = Path(seg_path).read_bytes()
    rows = []
    for moof_off, box_type, box_size, box_hdr in _iter_boxes(data, 0, len(data)):
        if box_type != b"moof":
            continue
        for traf_off, traf_type, traf_size, traf_hdr in _iter_boxes(
                data, moof_off + box_hdr, moof_off + box_size):
            if traf_type != b"traf":
                continue
            track_id = None
            tfdt = None
            for child_off, child_type, _size, child_hdr in _iter_boxes(
                    data, traf_off + traf_hdr, traf_off + traf_size):
                if child_type == b"tfhd":
                    track_id = struct.unpack_from(
                        ">I", data, child_off + child_hdr + 4)[0]
                elif child_type == b"tfdt":
                    tfdt = (child_off, child_hdr)
            if track_id is None or tfdt is None:
                continue
            tfdt_off, tfdt_hdr = tfdt
            body = tfdt_off + tfdt_hdr
            version = data[body]
            width = ">Q" if version == 1 else ">I"
            rows.append((track_id, version,
                         struct.unpack_from(width, data, body + 4)[0]))
    return rows



def verify_tfdt_chain(stage_dir, scales, tolerance_seconds=0.1,
                      cross_track_tolerance_seconds=0.12):
    """Stage 之后、publish 之前自检：每轨 tfdt 链必须与 playlist 的 EXTINF
    逐段对齐。返回校验过的分片数。

    这条自检本可以在最初就抓住 2026-10-03《纽伦堡》无法播放的 bug。当时的
    真实现场是：tfdt 在三个 chunk 接缝处归零重启（video3/video6/video10），
    而位移恒为 0——media_timescale() 把偏移读错了 4 字节，拿到
    modification_time 低位而不是 timescale，于是 int(start * 0) == 0。零偏移
    和「本来就不该有偏移」在输出上完全一样，属于静默故障，只能靠断言兜。

    判据（逐轨、逐段）：
      1. 每段都带 moof/traf/tfdt（read_tfdt 已保证，没有就抛）；
      2. 段的 tfhd track_ID 集合全程一致，且都在 init 的 timescales 里；
      3. 首段各轨 tfdt 约等于 0；
     4. tfdt[i+1] - tfdt[i] 约等于 EXTINF[i] * 该轨 timescale —— 注意是
        第 i 段（前一段）的 EXTINF，不是第 i+1 段的。用错段号这条自检本身
        就会误报，实测把一条正确的链判成了断链。
     5. 音轨 tfdt 时刻 − 视轨 tfdt 时刻（下称 rel）全程恒定。单轨连续只
        保证每轨自己不断，两轨之间的错位整条漏得过去：2026-10-03 两个
        陈旧预打包 item（21b7527b / ff951e2e）的音频 tfdt 在 chunk 接缝
        被视频时基的偏移错移，实测 rel 从 +0.5s 三级跳到 +2.172s、
        ff951e2e 稳在 +1.856s，而单轨链条每一条都是 EXTINF 连续的。

    实测残差参考：纽伦堡音频 48000 时基下 10.005s 段实测 480256 ticks，即
    0.0053s；容差取 0.1s，比它宽一个数量级，仍能抓住 4.7s 的 GOP 级错位
    （ffmpeg -ss 落在关键帧上，标称 start 与真实媒体时间就差这么多）。

    rel 交叉阈值的实证：干净产物（纽伦堡 1437 段全量实测）rel 在
    0.000~0.085s 之间抖，AAC priming 让音频恒定领先视频几十毫秒；要抓的
    故障是 ≥0.5s 的 chunk 接缝跳变。0.12s 夹在中间，误报与漏报都只剩
    一个数量级的窄边。
    """
    stage_dir = Path(stage_dir)
    pl_text = (stage_dir / "video.m3u8").read_text()
    names = [line for line in pl_text.splitlines()
             if line and not line.startswith("#") and line.endswith(".m4s")]
    extinfs = [float(x) for x in
               re.findall(r"^#EXTINF:([0-9.]+),", pl_text, re.M)]
    if len(names) != len(extinfs):
        raise ParallelPackError(
            "staged playlist has %d segments but %d EXTINF"
            % (len(names), len(extinfs)))
    if not names:
        raise ParallelPackError("staged playlist has no segment")
    expected_tracks = None
    previous = None
    previous_extinf = None
    first_rel = None
    for index, (name, extinf) in enumerate(zip(names, extinfs)):
        entries = {track_id: value
                   for track_id, _version, value in read_tfdt(stage_dir / name)}
        tracks = tuple(sorted(entries))
        if expected_tracks is None:
            expected_tracks = tracks
            unknown = sorted(set(tracks) - set(scales))
            if unknown:
                raise ParallelPackError(
                    "segments carry track IDs %s absent from init timescales %s"
                    % (unknown, sorted(scales)))
        elif tracks != expected_tracks:
            raise ParallelPackError(
                "%s track set %s != %s; playlist joins unlike chunks"
                % (name, list(tracks), list(expected_tracks)))
        if index == 0:
            for track_id, value in entries.items():
                limit = tolerance_seconds * scales[track_id]
                if abs(value) > limit:
                    raise ParallelPackError(
                        "first segment %s track %d starts at tfdt %d, expected 0"
                        % (name, track_id, value))
        if previous is not None:
            for track_id, value in entries.items():
                expected = (previous[track_id]
                            + previous_extinf * scales[track_id])
                limit = tolerance_seconds * scales[track_id]
                if abs(value - expected) > limit:
                    raise ParallelPackError(
                        "%s track %d tfdt %d != previous + EXTINF (%.0f, "
                        "off %.3fs); the pack is not a continuous timeline"
                        % (name, track_id, value, expected,
                           (value - expected) / scales[track_id]))
        if len(entries) >= 2:
            # 以字典序最小的 track_ID 为基准（ffmpeg 产物 video=1 audio=2）
            base_id = sorted(entries)[0]
            base_sec = entries[base_id] / scales[base_id]
            for track_id in sorted(entries):
                if track_id == base_id:
                    continue
                rel = entries[track_id] / scales[track_id] - base_sec
                if first_rel is None:
                    first_rel = rel
                    continue
                if abs(rel - first_rel) > cross_track_tolerance_seconds:
                    raise ParallelPackError(
                        "%s track %d rel %+.3fs vs track %d, drifted "
                        "%+.3fs from segment 0 (%.3fs); audio misaligned "
                        "to video at this seam"
                        % (name, track_id, rel, base_id,
                           rel - first_rel, first_rel))
        previous_extinf = extinf
        previous = entries
    return len(names)


# EXTINF 低于这个值的分片视为「残片」：装不下可呈现帧的缓冲区会让 AVPlayer
# 停住（remux.trim_stub_tail 的真机教训，同一现象在并行通路的接缝处重现）。
STUB_EXTINF_SECONDS = 0.5


def _is_stub(extinf: str) -> bool:
    try:
        return float(extinf) < STUB_EXTINF_SECONDS
    except ValueError:
        return False


def target_duration_for(durations) -> int:
    """RFC 8216 §4.3.3.1 的 TARGETDURATION：每个 EXTINF 四舍五入后必须
    小于等于它，所以安全下限就是 max(EXTINF) 向上取整。

    2026-10-03 之前并行通路把它钉死成 int(segment_seconds)+3（6s 切片 → 9），
    而分块接缝的首片是「chunk 起点→下一个关键帧+segment_seconds」的残片，
    长 GOP 源（纽伦堡 x265 4K）实测 14.917s。15 > 9 → 整份 media playlist
    违规，tvOS 判非法列表：master.m3u8 与 video.m3u8 都 200，随后一个
    video*.m4s 都不请求，用户侧只剩「无法播放」。
    """
    values = [float(d) for d in durations]
    if not values:
        raise ParallelPackError("no segment durations for TARGETDURATION")
    return max(1, math.ceil(max(values)))


def segment_plan(lines, stage):
    """按 media playlist 的真实顺序产出「段文件 ↔ 该段 EXTINF」配对。

    bandwidth_from_segments 的峰值口径是逐段用「段字节 × 8 ÷ 该段时长」取
    最大。调用方图省事用 sorted(stage.glob("video*.m4s")) 字典序
    （video0,video1,video10,...），段数一多顺序全乱，也拿不到逐段时长，
    只能退回 max(段字节)/平均段长——两头都写不对。2026-10-03《纽伦堡》
    实测：整片 21.66Mbps、真峰值 70.49Mbps，master 先后写成 24.48Mbps
    与 9386598087，AVPlayer 按假 BANDWIDTH 配缓冲：起播后周期性 buffer
    underrun、拉段节奏退化、burst 一段后彻底停摆，正是真机「播几十秒
    失败」的根因。jfpack 秒开路用 playlist_segment_seconds 早就是同口径。
    """
    names = [line.strip() for line in lines
             if line.strip() and not line.startswith("#")]
    seconds = [float(m) for m in re.findall(
        r"^#EXTINF:([0-9.]+),", chr(10).join(lines), re.M)]
    if len(names) != len(seconds):
        raise ParallelPackError(
            "media playlist has %d segments but %d EXTINF"
            % (len(names), len(seconds)))
    files = [stage / name for name in names]
    missing = [path.name for path in files if not path.exists()]
    if missing:
        raise ParallelPackError(
            "staged segments missing: " + ", ".join(missing[:5]))
    return files, seconds


def assemble_media_playlist(groups):
    """Join each chunk's (segment name, EXTINF) sequence into one media playlist.

    Returns (lines, ops, dropped): lines can be written straight to disk; ops is
    the [(chunk index, source segment name, target segment name,
    offset_seconds), ...] move list left to the caller (move + rewrite_tfdt --
    pure computation here, no disk); dropped counts the seam stub segments.

    Both rules were learned on the real device:
    1. TARGETDURATION is recomputed from the true maximum segment length,
       never pinned to int(segment_seconds)+3. A chunk seam's first segment
       spans "chunk start -> next keyframe + segment_seconds", which measured
       14.917s on long-GOP source (Nuremberg x265 4K, keyframe interval about
       10s); writing 9 breaks RFC 8216 section 4.3.3.1 and tvOS rejects the
       whole playlist -- on device master.m3u8 and video.m3u8 both returned
       200, then not a single video*.m4s was requested and the user only saw
       AVFoundationErrorDomain "Cannot Open".
   2. Stub tail segments at chunk seams (1-3 frames, 0.041s measured on
      Nuremberg) are dropped: AVPlayer stalls on a buffer with almost no
      presentable content. remux.trim_stub_tail already deletes them on the
      single-process path for the same reason. Only tens of milliseconds are
      lost, and the very first segment is never dropped (out_idx > 0 only).
    3. ops carries offset_seconds = the start of that segment's chunk ON THE
       PUBLISHED TIMELINE, i.e. the EXTINF sum of every kept segment in the
       chunks before it. It is neither the per-segment cumulative value nor
       the chunk's nominal start. A chunk is cut at
       `-ss <nominal start>`, and ffmpeg lands that on the previous keyframe,
       so the real media time of a chunk's first sample is one GOP earlier
       (Nuremberg: nominal 2224s vs real 2219.3s, 4.7s of error). Using the
       nominal start shifted tfdt by the wrong amount and left the seam
       samples at a decode time that disagreed with their EXTINF, which is
       exactly the "playlist 200 + zero segments requested" device failure.
    4. One offset per chunk, not one per segment. A chunk's own tfdt chain
       already runs from 0 and increases continuously (setts=PTS-STARTPTS, or
       the default zeroing of -ss placed before -i), so stacking the
       per-segment position on top double counts time ffmpeg already counted:
       measured live, the first seam segment jumped by a whole EXTINF (10.000s
       on the controlled repro). verify_tfdt_chain catches that in one pass
       instead of letting it reach the TV.
    """
    out_idx = 0
    dropped = 0
    # 每个 chunk 在「已发布」时间轴上的起点：同一 chunk 内所有段共用一个位移
    # 量，chunk 之间用前几个 chunk 实留分片的 EXTINF 累加值推进。
    chunk_start = 0.0
    lines = ["#EXTM3U", "#EXT-X-VERSION:7", "#EXT-X-INDEPENDENT-SEGMENTS",
             "#EXT-X-TARGETDURATION:1", "#EXT-X-MEDIA-SEQUENCE:0",
             "#EXT-X-PLAYLIST-TYPE:VOD", "#EXT-X-MAP:URI=\"init.mp4\""]
    ops = []
    extinfs = []
    for group_index, group in enumerate(groups):
        names, durations = group
        if len(names) != len(durations):
            raise ParallelPackError(
                "group %d has %d segments but %d EXTINF"
                % (group_index, len(names), len(durations)))
        kept = 0.0
        for src_name, extinf in zip(names, durations):
            if out_idx > 0 and _is_stub(extinf):
                dropped += 1
                continue
            dst_name = "video%d.m4s" % out_idx
            ops.append((group_index, src_name, dst_name, chunk_start))
            lines.append("#EXTINF:%s," % extinf)
            lines.append(dst_name)
            extinfs.append(extinf)
            kept += float(extinf)
            out_idx += 1
        chunk_start += kept
    if out_idx <= 0:
        raise ParallelPackError("staged playlist has no segment")
    lines[3] = "#EXT-X-TARGETDURATION:%d" % target_duration_for(extinfs)
    lines.append("#EXT-X-ENDLIST")
    return lines, ops, dropped


def rewrite_tfdt(seg_path, track_offsets):
    """Shift every moof/traf/tfdt by its own track's offset, in that track's
    timescale. Returns the number of patched tfdt boxes.

    Two separate defects made the 2026-10-03《纽伦堡》full pack unplayable,
    and both hid behind this function:

    1. Only version==1 tfdt was patched. ffmpeg's HLS muxer emits v0 (32-bit)
       on most sources, so `patched` stayed 0 and the seam was never shifted.
       Symptom on device: master.m3u8 and video.m3u8 both 200, then not one
       video*.m4s requested -- AVFoundationErrorDomain -11848.
    The first guess was wrong and is recorded here so it is not repeated:
    ffmpeg's HLS muxer actually emits v1 (64-bit) tfdt, and this function
    already handled both. The real defect was upstream, in media_timescale(),
    which read creation_time/modification_time instead of timescale and so
    returned 0 -- every seam was shifted by int(start * 0) == 0 ticks. A zero
    offset is indistinguishable from a no-op unless it is asserted, which is
    why rewrite_tfdt now raises instead of returning quietly and why stderr
    must never be trusted to report a bad parse.
    2. A single `offset_ticks` was applied to every traf, so the audio track
       (timescale 48000) was moved by the video track's offset (12800/16000)
       and drifted by 3-4x at every chunk seam.

    A silent no-op is the expensive failure mode here, so patching nothing
    now raises instead of returning quietly.
    """
    data = bytearray(Path(seg_path).read_bytes())
    patched = 0
    for moof_off, box_type, box_size, box_hdr in _iter_boxes(data, 0, len(data)):
        if box_type != b"moof":
            continue
        for traf_off, traf_type, traf_size, traf_hdr in _iter_boxes(
                data, moof_off + box_hdr, moof_off + box_size):
            if traf_type != b"traf":
                continue
            track_id = None
            tfdt = None
            for child_off, child_type, _size, child_hdr in _iter_boxes(
                    data, traf_off + traf_hdr, traf_off + traf_size):
                if child_type == b"tfhd":
                    track_id = struct.unpack_from(
                        ">I", data, child_off + child_hdr + 4)[0]
                elif child_type == b"tfdt":
                    tfdt = (child_off, child_hdr)
            if track_id is None or tfdt is None:
                continue
            offset = int(track_offsets.get(track_id, 0))
            tfdt_off, tfdt_hdr = tfdt
            body = tfdt_off + tfdt_hdr
            version = data[body]
            if version == 1:
                current = struct.unpack_from(">Q", data, body + 4)[0]
                struct.pack_into(">Q", data, body + 4, current + offset)
            else:
                current = struct.unpack_from(">I", data, body + 4)[0]
                shifted = current + offset
                if shifted > 0xFFFFFFFF:
                    raise ParallelPackError(
                        "tfdt v0 overflow: track %d at %d + %d exceeds 32 bits"
                        % (track_id, current, offset))
                struct.pack_into(">I", data, body + 4, shifted)
            patched += 1
    if patched == 0:
        raise ParallelPackError(
            "%s holds no moof/traf/tfdt; nothing was shifted"
            % Path(seg_path).name)
    Path(seg_path).write_bytes(bytes(data))
    return patched


def probe_keyframe_at_or_before(src, nominal, window=20.0, tail=2.0):
    """返回 nominal 之前最近的关键帧时间，向前探测 window 秒。

    分块按标称边界切并用 -ss 落点，但视频轨是 -c copy，ffmpeg 只能落在不晚于
    该点的关键帧上；音频轨被重编码，能精确落在标称点。退到真实关键帧后，两轨
    从同一内容位置各自归零，chunk 内音画才不会恒定错位（音画不同步根因）。
    """
    if nominal <= 0:
        return 0.0
    lo = nominal - window
    if lo < 0:
        lo = 0.0
    hi = nominal + tail
    proc = _run([
        str(require(FFPROBE)), "-v", "error", "-select_streams", "v:0",
        "-skip_frame", "nokey", "-show_entries", "frame=pts_time",
        "-of", "csv=p=0", "-read_intervals", "%.3f%%%.3f" % (lo, hi), src,
    ], timeout=probe_timeout())
    if proc.returncode != 0:
        raise ParallelPackError(
            "keyframe probe failed at %.3f: %s"
            % (nominal, proc.stderr.strip()[:200]))
    best = None
    for tok in proc.stdout.replace(",", " ").split():
        try:
            t = float(tok)
        except ValueError:
            continue
        if t <= nominal + 1e-6 and (best is None or t > best):
            best = t
    if best is None:
        raise ParallelPackError(
            "no keyframe at or before %.3f within [%.3f, %.3f]"
            % (nominal, lo, hi))
    return best


def _run_part(i, src, seg_dir, start, span, segment_seconds, maps, colorimetry,
              full_range, bsf, setts, transcode_audio=False, keyframe=0.0):
    seg_dir.mkdir(parents=True, exist_ok=True)
    argv = [str(require(FFMPEG)), "-hide_banner", "-loglevel", "error", "-y",
            "-ss", "%.3f" % start, "-t", "%.3f" % span, "-i", src,
            *maps, "-c", "copy", "-tag:v", "dvh1", "-strict", "unofficial",
            "-color_primaries", colorimetry[0],
            "-color_trc", colorimetry[1],
            "-colorspace", colorimetry[2],
            "-color_range", "pc" if full_range else "tv"]
    if bsf or setts:
        chain = list(bsf or ())
        if setts:
            chain.append(setts)
        argv += ["-bsf:v", ",".join(chain)]
    if any(m.endswith("0:a:0?") or m.endswith("0:a:0") for m in maps):
        # 源音轨 Apple 不认（dts 等）时转码 aac，master 同步声明 aac，
        # 保证 CODECS 与实际 init/media 一致；其余情形原样 copy。
        argv += ["-c:a", "aac" if transcode_audio else "copy"]
        if transcode_audio:
            argv += ["-b:a", "384k"]
    argv += ["-f", "hls", "-hls_segment_type", "fmp4",
             "-hls_time", str(segment_seconds),
             "-hls_list_size", "0", "-hls_playlist_type", "vod",
             str(seg_dir / "video.m3u8")]
    proc = _run(argv, timeout=ffmpeg_timeout(span))
    if proc.returncode != 0 or not (seg_dir / "video.m3u8").exists():
        raise ParallelPackError("part failed: " + proc.stderr.strip()[:400])
    # 落点断言：copy 落关键帧则首帧 pts 应约等于 0；非 0 说明 -ss 没落到
    # probe 到的关键帧（输入布局与探测矛盾），宁可报错也不要产出错位片。
    playlist = seg_dir / "video.m3u8"
    chk = _run([
        str(require(FFPROBE)), "-v", "error", "-select_streams", "v:0",
        "-read_intervals", "0%0.5", "-show_entries", "frame=pts_time",
            "-of", "csv=p=0", playlist,
    ], timeout=probe_timeout())
    if chk.returncode == 0:
        toks = chk.stdout.replace(",", " ").split()
        if toks:
            try:
                first_pts = float(toks[0])
            except ValueError:
                first_pts = 0.0
            # 三种落点约定都算正确，取离首帧 pts 最近者判距：setts 生效时首帧
            # pts 归零；chunk 0 走 -ss 0 时 ffmpeg 连偏移都不加，源首帧 pts
            # （实测 0.021s）原样留下；setts 未覆盖到时首帧 pts 约等于探到的
            # 关键帧绝对时间。容差 0.25s：比一个帧时长，比本源最小 GOP
            # （实测 1.754s）短一个数量级。旧的 0.001s 会把它自己算出的
            # chunk 0 全程误报成落点失败，进而每次点片都白烧 20G+40 分钟。
            landed = min(abs(first_pts), abs(first_pts - keyframe))
            if landed > 0.25:
                raise ParallelPackError(
                    "part %d video first pts %.3f, keyframe %.3f: did not "
                    "land on the probed keyframe" % (i, first_pts, keyframe))
    return i, (seg_dir / "video.m3u8").read_text()


def parallel_pack(src, out_dir, *, seconds=0.0, segment_seconds=6.0,
                 workers=4, maps=VIDEO_AUDIO_MAPS,
                 colorimetry=("bt2020", "smpte2084", "bt2020nc"),
                 full_range=0, bsf=None, setts=None, audio_codec="",
                 transcode_audio=False, source=None,
                 subtitle_texts: Mapping[str, str] | None = None):
    """Pack in parallel; returns the full Duration master.m3u8 path."""
    # source 是调用方探测好的 SourceInfo：master 的 DV 信令、宽高、帧率、
    # 音轨 codec 全部以它为准，与秒开包（jfpack.pack_source）同一个数据源，
    # 两条通路写出的 master.m3u8 才逐字一致。默认 None 只为兼容旧调用方。
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    # The published entry points (master.m3u8 / video.m3u8 / init.mp4) and
    # their segments must NOT be wiped here: the instant-first warm pack is
    # playing on the TV right now while this background full pack runs.
    # Deleting them makes the TV's next master fetch see an empty file and
    # AVPlayer fails instantly -- the "播放失败" symptom (handoff v24).
    # Stale segments are cleaned after publish, once the new playlist is live.
    for stale in out_dir.glob("_stage"):
        shutil.rmtree(stale, ignore_errors=True)
    for stale in out_dir.glob("_part*"):
        shutil.rmtree(stale, ignore_errors=True)
    # Idempotency for a second run of THIS function is preserved by wiping
    # the part dirs above: _run_part always starts from a clean _part<i>.
    total = seconds if seconds else probe_duration(src)
    if workers <= 1 or total <= segment_seconds * 2:
        raise ParallelPackError("duration too short for parallel pack")

    # 音频 aac 精确落在标称点、视频 copy 只落在其前关键帧，两轨各自归零后
    # 内容错位即音画不同步根因。把每个标称边界退到它前面最近的关键帧，两轨
    # 才从同一内容位置出发；start 为绝对秒，chunk 长度随之重算。
    span = total / workers
    nominal_starts = [i * span for i in range(workers)]
    starts = [probe_keyframe_at_or_before(src, s) for s in nominal_starts]
    bounds = starts + [total]
    spans = [bounds[k + 1] - bounds[k] for k in range(workers)]
    for idx, chunk_span in enumerate(spans):
        if chunk_span <= segment_seconds:
            raise ParallelPackError(
                "chunk %d span %.3f <= segment_seconds %.3f; keyframe probe "
                "contradicts source layout" % (idx, chunk_span, segment_seconds))
    seg_dirs = [out_dir / ("_part%d" % i) for i in range(workers)]

    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = [ex.submit(_run_part, i, src, seg_dirs[i], starts[i], spans[i],
                          segment_seconds, maps, colorimetry, full_range,
                          bsf, setts, transcode_audio, starts[i])
                          for i in range(workers)]
        pairs = [f.result() for f in as_completed(futs)]
        # result is (index, playlist_text); as_completed is unordered, so
        # zipping it against ordered starts/seg_dirs mis-pairs them.
        pairs.sort(key=lambda p: p[0])
        texts = [t for _, t in pairs]

    ordered = sorted(zip(starts, seg_dirs, texts), key=lambda r: r[0])

    # Staging: rewrite tfdt + rename into a scratch dir, then atomically swap
    # video.m3u8 into place. Writing the playlist before every segment has been
    # moved leaves a half pack that AVPlayer can play for 4 segments and then
    # stalls on (observed live).
    stage = out_dir / "_stage"
    scales = track_timescales(ordered[0][1] / "init.mp4")
    stage.mkdir(parents=True, exist_ok=True)
    groups = []
    for start, seg_dir, text in ordered:
        names = re.findall(r"^(video\d+\.m4s)$", text, re.M)
        extinfs = re.findall(r"^#EXTINF:([0-9.]+),", text, re.M)
        if len(names) != len(extinfs):
            raise ParallelPackError(
                "part at %.1fs has %d segs but %d EXTINF"
                % (start, len(names), len(extinfs)))
        groups.append((names, extinfs))
    lines, ops, dropped = assemble_media_playlist(groups)
    out_idx = len(ops)
    for group_index, src_name, dst_name, offset_seconds in ops:
        _start, seg_dir, _text = ordered[group_index]
        target = stage / dst_name
        shutil.move(str(seg_dir / src_name), str(target))
        # 每轨按自己的时基位移：视频 16000 / 音频 48000 用同一个 offset_ticks
        # 会让音频在每个接缝漂移 3 倍。基准用 EXTINF 累计值而不是 chunk 标称
        # start——ffmpeg -ss 落在关键帧上，标称 start 与真实媒体时间差整个
        # GOP（纽伦堡实测 2224s 标称 vs 2219.3s 真实）。
        track_offsets = {
            tid: int(round(offset_seconds * ts)) for tid, ts in scales.items()}
        rewrite_tfdt(target, track_offsets)
    (stage / "video.m3u8").write_text(chr(10).join(lines) + chr(10))
    shutil.copyfile(ordered[0][1] / "init.mp4", stage / "init.mp4")
    # tfdt 链自检：位移写错量级（含写成 0）时，产物与正确包几乎不可区分，
    # AVPlayer 的表现都是「拉完 playlist 后一个分片都不请求」，只能断言兜住。
    # 2026-10-03《纽伦堡》的正身根因就是位移恒 0（media_timescale 返回 0）。
    # 这条守门必须在发布之前拦住它，别让黑屏从这条缝溜进真机。
    verify_tfdt_chain(stage, scales)
    # 信令、宽高、帧率一律走 playlist.py 的权威构建器，产物与秒开包逐字一致。
    # 2026-10-03 之前这里手搓 master：CODECS 写 "dvh1.05.06" 且漏
    # SUPPLEMENTAL-CODECS。tvOS 要求 dvh1 只能出现在 SUPPLEMENTAL-CODECS
    # （依据见 playlist.py 模块注释），写进 CODECS 会被整份拒收——现场正是
    # 这里要修的那条：master.m3u8 与 video.m3u8 都 200，随后一个分片都不
    # 请求，AVFoundationErrorDomain -11848 / -15517 "Cannot Open"。全片包
    # 发布时用坏 master 覆盖秒开包的好 master，用户侧只剩「点片没反应」。
    # 注意：手搓时代靠 init 段探宽高/帧率也走不通——只有 init 无样本的 fmp4
    # 会让 ffprobe 拿 mdhd timescale 兜底，2093113 实测报 16000/1（源实为
    # 24000/1001），FRAME-RATE 与源矛盾。现在一切以探测结果为准。
    # 带宽口径必须按 media playlist 的真实顺序配对「段字节 ↔ 该段 EXTINF」：
    # sorted(stage.glob()) 是字典序（video0,video1,video10,...），既打乱配对、
    # 又拿不到逐段时长，bandwidth_from_segments 只能退回 max(段字节)/平均段长
    # 反推峰值。2026-10-03《纽伦堡》就这样写出过 24.48 Mbps（真峰值 70.49）与
    # 9386598087 两份 master——AVPlayer 按假带宽配缓冲：起播后周期性 buffer
    # underrun、拉段节奏退化、burst 一段后彻底停摆，正是真机「播几十秒失败」的
    # 成因。jfpack 秒开路（playlist_segment_seconds）早已同口径，这里对齐。
    seg_names = [line.strip() for line in lines
                 if line.strip() and not line.startswith("#")]
    seg_seconds = [float(m) for m in re.findall(
        r"^#EXTINF:([0-9.]+),", chr(10).join(lines), re.M)]
    if len(seg_names) != len(seg_seconds):
        raise ParallelPackError(
            "staged media playlist: %d segments vs %d EXTINF"
            % (len(seg_names), len(seg_seconds)))
    seg_files = [stage / name for name in seg_names]
    for seg_path in seg_files:
        if not seg_path.exists():
            raise ParallelPackError("staged segment missing: " + seg_path.name)
    playlist_total = sum(seg_seconds)
    if source is not None:
        w, h = source.video.width, source.video.height
        frame_rate = source.video.frame_rate or 25.0
        dovi = source.dovi
    else:
        # 兜底：调用方没给探测结果时才自己探宽高/帧率。
        probe = _run([
            str(require(FFPROBE)), "-v", "error",
            "-select_streams", "v:0",
            "-show_entries", "stream=width,height,r_frame_rate",
            "-of", "default=noprint_wrappers=1:nokey=1",
            src,
        ])
        if probe.returncode != 0:
            raise ParallelPackError(
                "ffprobe source failed: " + probe.stderr.strip()[:300])
        w, h, fr = [t.strip() for t in probe.stdout.splitlines()[:3]]
        num, _, den = fr.partition("/")
        frame_rate = float(num) / float(den) if den else float(num)
        video_range = "PQ"
        dovi = None
    bandwidth = bandwidth_from_segments(
        seg_files, playlist_total, segment_seconds=seg_seconds)
    if dovi is not None:
        # level 钳法与 jfpack.pack_source 完全一致：master 认 dvh1.05.06，
        # init 的 dvcC 也必须跟着钳——只改一边，AVPlayer 认的是 master 那个值。
        dv_level = expected_dv_level(w, h)
        target = dovi.profile5_target
        if target.level > dv_level:
            target = replace(target, level=dv_level)
        variant = variant_from_source(source, "video.m3u8", bandwidth=bandwidth,
                                      target=target, audio_codec=audio_codec)
        # init 段归一与 jfpack.pack_source 同一顺序：先把 stsd 收敛成单 entry
        # （P8/P8.1 源 movenc 追加的第二条 dvh1 是死重，多个 dvcC 会让转签
        # 撞 ResignError），再 resign 成 Apple 规范（dvvC→dvcC、P8→P5），
        # 最后钳 level、清 compressor 名。2026-10-03 的纽伦堡全片包正是死在
        # 这里：init 留 ffmpeg 俗名 dvvC + 原 profile 8，与 master 的
        # dvh1.05.06 自相矛盾，tvOS 拉完 master/video.m3u8 后一个分片都不
        # 请求，AVFoundationErrorDomain -11848 "Cannot Open"。
        init_path = stage / "init.mp4"
        init_bytes, dropped = keep_single_sample_entry(
            init_path.read_bytes())
        if dropped:
            init_path.write_bytes(init_bytes)
        resign_pack(init_path, dovi)
        init_bytes, old_level = clamp_dvcC_level(
            init_path.read_bytes(), dv_level)
        if old_level is not None:
            init_path.write_bytes(init_bytes)
        init_bytes, old_name = clear_compressor_name(init_path.read_bytes())
        if old_name is not None:
            init_path.write_bytes(init_bytes)
        # tvOS 终检：与 jfpack.pack_source 同一个守门，绝不上线 AVPlayer 会
        # 整包拒收的 init。坏了当场炸穿，别让黑屏从这条缝溜进真机。
        init_final = init_path.read_bytes()
        try:
            entries = sample_entry_count(init_final)
        except ValueError:
            entries = 0
        if entries != 1:
            raise ParallelPackError(
                "init 的 video sample entry 数=%d（应为 1），会被 tvOS 拒收"
                % entries)
        dv_boxes = locate_dv_boxes(init_final)
        if not dv_boxes:
            raise ParallelPackError(
                "DV 源但 init 缺 dvcC 配置盒，会被 tvOS 拒收")
        if any(box.fourcc != "dvcC" for box in dv_boxes):
            names = ",".join(box.fourcc for box in dv_boxes)
            raise ParallelPackError(
                "init 的 DV 配置盒非规范名 dvcC（%s），会被 tvOS 拒收"
                % names)
    else:
        # 非 DV 源保底：只写 hvc1 基础轨 + 实测 range，绝不带 dvh1 信令。
        # source 没有 DOVI 记录时 apple_video_range 可能抛 UnknownTransfer
        # （容器既没给 color_transfer 又没 DV 兜底，2026-10-03 实测定制片源
        # 会走到这条）——打包不该被一行信令炸穿，按 PQ 收尾。
        try:
            video_range = source.video.apple_video_range
        except UnknownTransfer:
            video_range = "PQ"
        codecs = hevc_base_codec()
        if audio_codec:
            codecs = f"{codecs},{audio_codec}"
        variant = Variant(uri="video.m3u8", bandwidth=bandwidth, codecs=codecs,
                          video_range=video_range, resolution=(w, h),
                          frame_rate=frame_rate)
    # 字幕轨（若有）：sub_*.vtt + sub_*.m3u8 落在 stage 根，与 video.m3u8 同目录
    tracks = write_subtitle_tracks(stage, subtitle_texts) if subtitle_texts else []
    write_master_playlist(stage / "master.m3u8", [variant], subtitles=tracks)
    # Self-check before publishing: a malformed EXT-X-MAP line makes
    # AVPlayer parse the playlist then request zero segments (observed
    # live as master.m3u8 200 + video.m3u8 200 + no video*.m4s).
    pl_text = (stage / "video.m3u8").read_text()
    map_line = [l for l in pl_text.splitlines() if "X-MAP" in l]
    if not map_line or not map_line[0].startswith("#EXT-X-MAP"):
        raise ParallelPackError(
            "bad EXT-X-MAP line %r" % (map_line[:1],))
    for tag in ("#EXT-X-INDEPENDENT-SEGMENTS", "#EXT-X-TARGETDURATION:",
                "#EXT-X-MEDIA-SEQUENCE:", "#EXT-X-PLAYLIST-TYPE:",
                "#EXT-X-ENDLIST"):
        if tag not in pl_text:
            raise ParallelPackError("missing tag %s" % tag)
    if not re.findall(r"^(video\d+\.m4s)$", pl_text, re.M):
        raise ParallelPackError("no segments in playlist")
    # RFC 8216 §4.3.3.1 守门：EXTINF 四舍五入后必须 ≤ TARGETDURATION。这条
    # 曾经是硬编码 int(segment_seconds)+3 漏掉的，长 GOP 源（纽伦堡）全片包
    # 首片 14.917s 而 TARGETDURATION=9，tvOS 整份 media playlist 判非法——
    # master.m3u8/video.m3u8 全 200 但一个分片都不请求。坏列表当场炸穿，
    # 别让黑屏从这条缝溜进真机。
    td_line = [l for l in pl_text.splitlines()
               if l.startswith("#EXT-X-TARGETDURATION:")]
    if not td_line:
        raise ParallelPackError("missing #EXT-X-TARGETDURATION")
    try:
        td_value = int(td_line[0].split(":", 1)[1])
    except ValueError:
        raise ParallelPackError("bad TARGETDURATION line %r" % td_line[0])
    durations = [float(x) for x in
                 re.findall(r"^#EXTINF:([0-9.]+),", pl_text, re.M)]
    if durations and max(durations) > td_value + 1e-9:
        raise ParallelPackError(
            "TARGETDURATION=%d < max EXTINF=%.3f，tvOS 会整份拒收"
            % (td_value, max(durations)))

    # sidx 归一 + 守门（2026-10-03《纽伦堡》ab476f22 现场）：ffmpeg movenc 遇 MKV
    # 源（视频 + DTS-HD + 5 条 PGSSUB）会把每段第一个 sidx 的 reference_ID 与
    # timescale 写成未初始化内存值——ref_id∈{0,1,2,3…}、timescale∈{0, 519984, …}，
    # 含 0 即段时长除零；1437/1437 段全坏。mp4 源（097c703b / ff951e2e）对照
    # 300+ 段全对，坏包 100% 坏、能播包 100% 好。tvOS 症状：master/video.m3u8
    # 全 200 但一个分片都不请求，「无法播放」。按 init 视频轨真值原地覆写 8 字节
    # （零重编码），再断言全对齐才发布。
    _fixed_sidx, _sidx_notes = normalize_sidx(seg_files, stage / "init.mp4")
    if _sidx_notes:
        raise ParallelPackError(
            "sidx 结构异常，拒绝发布：%s" % "; ".join(_sidx_notes[:5]))
    verify_sidx(seg_files, stage / "init.mp4")

   # Publish: replace the three entry points from the staged tree.
    # A shorter warm pack may already live in out_dir (instant-first). Its
    # video*.m4s share the same numbering as the staged full pack, so stale
    # survivors make the new playlist point at old content whose tfdt does
    # not line up -- AVPlayer then loads the playlist and requests nothing.
    # Publish order matters: the TV may still be playing the instant-first
    # warm pack. Swap in init, then segments, then video.m3u8, and flip
    # master.m3u8 LAST -- any intermediate state a reloading AVPlayer can
    # observe (old master + new video.m3u8 + segments in place) is
    # self-consistent, so it never sees an empty or half-swapped playlist.
    os.replace(str(stage / "init.mp4"), str(out_dir / "init.mp4"))
    for seg in stage.glob("video*.m4s"):
        os.replace(str(seg), str(out_dir / seg.name))
    os.replace(str(stage / "video.m3u8"), str(out_dir / "video.m3u8"))
    # 字幕轨随 video.m3u8 之后、master 之前发布：master 是最后翻转的入口，
    # 此前的中间态对重载的 AVPlayer 都自洽。sub_* 不铺碎文件，就这几个。
    published_subs = [sub.name for sub in stage.glob("sub_*")]
    for sub in stage.glob("sub_*"):
        os.replace(str(sub), str(out_dir / sub.name))
    os.replace(str(stage / "master.m3u8"), str(out_dir / "master.m3u8"))
    shutil.rmtree(stage, ignore_errors=True)
    # Now that the new playlist is live, segments left over from a shorter
    # previous pack (higher numbering than the new pack) are unreferenced
    # garbage. This is the first safe moment to remove them.
    keep = {"video%d.m4s" % i for i in range(out_idx)}
    for stale in out_dir.glob("video*.m4s"):
        if stale.name not in keep:
            stale.unlink()
    # 字幕轨同理：只留本次发布的，旧语言残留一并清掉
    keep.update(published_subs)
    for stale in out_dir.glob("sub_*"):
        if stale.name not in keep:
            stale.unlink()

    for d in seg_dirs:
        shutil.rmtree(d, ignore_errors=True)
    return out_dir / "master.m3u8"

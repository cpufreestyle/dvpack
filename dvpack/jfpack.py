"""jfpack：把 Jellyfin 片源（http URL）直接打成 HLS，产物喂给 tvOS。

与 remux 的差别只有输入层：remux 吃本地路径，这里吃 URL。ffprobe/ffmpeg 对
http 输入只发 Range 请求，实测局域网 5GB MKV 的 ffprobe 探测 0.6s 完成，
ffmpeg 取片段同样是局部读——不需要先把 5GB 拉到 Mac 上。

打包链路一条不换：-c copy + -tag:v dvh1 -strict unofficial 保证 RPU 一字节
不丢、init 段带规范名 dvcC；Apple HLS 规范 §1.9 只认 Profile 5，单层源的
转签交给 dvpack.p5。双层 P7 片源不走这里——那是 scripts/p7_to_p5.py 的
dovi_tool 两步链路；本模块只放行 mvp_verdict 已认可的单层 P5/P8。
"""

from __future__ import annotations

import os
import subprocess
import tempfile
from dataclasses import dataclass, replace
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from typing import Mapping

from .bmff import (
    atomic_write,
    drop_hvcC_param_set_type,
    keep_single_sample_entry,
    locate_dv_boxes,
    remove_box,
    sample_entry_count,
    normalize_sidx,
    verify_sidx,
)
from .bmff import (
    clear_compressor_name,
    clamp_dvcC_level,
    expected_dv_level,
)
from .dplus import apply_dplus, signalling_signature_ok

from .p5 import resign_pack
from .playlist import (
    bandwidth_from_segments,
    playlist_segment_seconds,
    variant_from_source,
    write_master_playlist,
)
from .probe import SourceInfo, ffprobe_json, mvp_verdict, parse_probe_json
from .relay import ffmpeg_visible_source
from .relay import _curl as _system_curl
from .tools import ffmpeg_timeout, probe_timeout, require
from .subs import write_subtitle_tracks
from .remux import (
    FFMPEG,
    FFPROBE,
    VIDEO_AUDIO_MAPS,
    audio_pack_mode,
    RemuxOutput,
    Verification,
    dv_signalling_bsf,
    hls_command,
    setts_bsf,
    source_full_range,
    resolve_output,
    trim_stub_tail,
    verify,
)
from .remux import _run


class JfPackError(Exception):
    pass


def playlist_duration(playlist: Path) -> float:
    """ffprobe 一个 HLS playlist 的总时长；全片打包时 seconds=0 用它计带宽。"""
    proc = _run([
        str(require(FFPROBE)),
        "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(playlist),
    ], timeout=probe_timeout())
    if proc.returncode != 0:
        raise JfPackError(f"无法读取产物时长：{proc.stderr.strip()}")
    try:
        return float(proc.stdout.strip())
    except ValueError as exc:
        raise JfPackError(f"产物时长无法解析：{proc.stdout.strip()}") from exc


def _source_colorimetry(source: SourceInfo) -> tuple[str, str, str] | None:
    """源容器给了完整 colour tag 就沿用它；DV rip 三件套普遍为空 → None。

    None 交给 hls_command 落 DV 默认三件套（remux.DV_DEFAULT_COLORIMETRY）。
    """
    primaries = source.video.color_primaries
    transfer = source.video.color_transfer
    space = source.video.color_space
    if not (primaries and transfer and space):
        return None
    return (primaries, transfer, space)


_SUBTITLE_EXTENSIONS = {
    "subrip": "srt",
    "mov_text": "srt",
    "text": "srt",
    "webvtt": "vtt",
    "ass": "ass",
    "ssa": "ass",
}


def jellyfin_subtitle_urls(src: str, item_id: str, stream_index: int, ext: str) -> list[str]:
    """从片源 URL 派生 Jellyfin 的字幕下载端点候选（同 host、同 api_key）。

    Jellyfin 10.9+ 的字幕端点是 /Videos/<itemId>/<mediaSourceId>/Subtitles/
    <streamIndex>/Stream.<ext>；老服务器（<10.9）是 /Videos/<itemId>/
    Subtitles/<streamIndex>/Stream.<ext>。2026-10-05 实测 Jellyfin 12.1 上
    老格式 404、新格式 200——内嵌 SRT 现成（纽伦堡 42 条文本轨含简中）。
    单媒体源时 mediaSourceId 就用 itemId；src 带了
    mediaSourceId 参数优先用它。streamIndex 是 ffprobe 报的**全局** index。
    候选按新→旧排，调用方依次试，拿到即止。
    """
    parts = urlsplit(src)
    query = parse_qs(parts.query, keep_blank_values=True)
    api_key = (query.get("api_key") or [""])[0]
    media_source = (query.get("mediaSourceId") or [""])[0] or item_id
    urls: list[str] = []
    for path in (
        f"/Videos/{item_id}/{media_source}/Subtitles/{stream_index}/Stream.{ext}",
        f"/Videos/{item_id}/Subtitles/{stream_index}/Stream.{ext}",
    ):
        url = f"{parts.scheme}://{parts.netloc}{path}"
        if api_key:
            url += f"?api_key={api_key}"
        urls.append(url)
    return urls


def _curl_bytes(url: str, timeout: float = 20.0) -> bytes:
    """用系统 curl 抓字节（launchd/TCC 只放行 Apple 平台二进制，见 relay.py）。

    字幕文件最多几 MB，30s 足够；失败一律抛 JfPackError 让调用方静默跳过。
    """
    proc = subprocess.run(
        [_system_curl(), "-sfL", "--max-time", str(int(timeout)), url],
        capture_output=True,
    )
    if proc.returncode != 0 or not proc.stdout:
        detail = (proc.stderr or b"").decode("utf-8", "replace").strip()[:200]
        raise JfPackError(f"字幕下载失败（{proc.returncode}）：{url} {detail}")
    return proc.stdout


def _decode_subtitle(payload: bytes, codec: str) -> str:
    """字幕字节 → SRT/WebVTT 文本。ass/ssa 先经 ffmpeg 转 srt。

    SRT 常见 GB18030 编码（国内字幕站点产物），utf-8 解不动就按 GBK 系兜底。
    """
    name = (codec or "").lower()
    if name in ("ass", "ssa"):
        handle, temp_path = tempfile.mkstemp(suffix=f".{name}")
        os.close(handle)
        try:
            with open(temp_path, "wb") as fh:
                fh.write(payload)
            proc = _run(
                [
                    str(require(FFMPEG)),
                    "-v", "error",
                    "-i", temp_path,
                    "-f", "srt",
                    "-",
                ], timeout=probe_timeout())
            if proc.returncode != 0 or not proc.stdout.strip():
                raise JfPackError(
                    f"{name} 字幕转 srt 失败：{proc.stderr.strip()[:200]}"
                )
            return proc.stdout
        finally:
            try:
                os.unlink(temp_path)
            except OSError:
                pass
    try:
        return payload.decode("utf-8")
    except UnicodeDecodeError:
        return payload.decode("gb18030")


def fetch_subtitle_texts(
    src: str, source: SourceInfo, *, item_id: str | None = None
) -> dict[str, str]:
    """抓源里的中/英文本字幕，按语言归一返回 {"zh": srt, "en": srt}。

    任何一条失败都静默跳过（字幕是增强项，不该让打包炸穿）；图像字幕
    （pgssub/dvdsub）和未知语言直接不要。
    """
    if item_id is None:
        # src 形如 /Videos/<itemId>/stream.mkv?static=true&api_key=...
        segments = [s for s in urlsplit(src).path.split("/") if s]
        if len(segments) >= 2 and segments[0].lower() == "videos":
            item_id = segments[1]
        else:
            item_id = ""
    if not item_id:
        return {}
    texts: dict[str, str] = {}
    for sub in source.subtitle_streams:
        if not sub.is_text:
            continue
        language = sub.track_language
        if not language or language in texts:
            continue
        ext = _SUBTITLE_EXTENSIONS.get(sub.codec)
        if not ext:
            continue
        for url in jellyfin_subtitle_urls(src, item_id, sub.index, ext):
            try:
                payload = _curl_bytes(url)
            except (JfPackError, OSError):
                continue  # 换下一个端点候选（新格式失败可能是老服务器）
            texts[language] = _decode_subtitle(payload, sub.codec)
            break
    return texts


@dataclass(frozen=True)
class PackResult:
    """一次打包的全部事实：源信令、产出布局、自检结论、master 列表。"""

    source: SourceInfo
    output: RemuxOutput
    verification: Verification
    master: Path
    notes: tuple[str, ...] = ()


def pack_source(
    src: str,
    out_dir: str | Path,
    *,
    seconds: float = 60.0,  # 0 = full source duration
    segment_seconds: float = 6.0,
    name: str = "video",
    drop_param_set_type: int | None = 39,
    drop_colr: bool = False,
    master_uri: str = "video.m3u8",
    maps: tuple[str, ...] = VIDEO_AUDIO_MAPS,
    #: 中英文本字幕 {"zh": srt, "en": srt}。打包前由调用方从片源抓取
    #: （fetch_subtitle_texts），这里只负责落成 HLS 字幕轨并挂进 master。
    #: 缺省 None = 老行为，master 一个字幕组都不写。
    subtitle_texts: Mapping[str, str] | None = None,
    # 绝不能开：setts=time_base=1/24:ts=N:duration=1 会把每帧 PTS/DTS 重写成
    # 序号，真实时间戳丢失后 ffmpeg HLS muxer 无法按 hls_time 切段——60s
    # 输入只产出 1 个 100MB+ 分片（真机：Apple TV 拉完 video.m3u8 后既不
    # 请求 init.mp4 也不拉分片，直接黑屏）。2026-09-27 逐项 A/B 实测确认。
    normalize_timestamps: bool = True,
) -> PackResult:
    """URL → HLS。闸门不过就不动 ffmpeg；产出不完整就报错，不留半个包。

    master_uri 给绝对路径（/_jf/<id>/video.m3u8）时，master 列表无论被直出
    还是被重定向到别的路径，都能解析到媒体列表——serve 的 /jf/pack 端点靠
    这一点。
    """
    # ffmpeg 视角的源地址：http(s) 走进程内回环中继，绕开 macOS 本地网络
    # TCC 对 launchd/无身份进程的出站封锁（实测 EHOSTUNREACH，2026-09-26；
    # /usr/bin/curl 是豁免的系统二进制，中继借它转发）。本地路径原样。
    fetch_src = ffmpeg_visible_source(src)
    source = parse_probe_json(ffprobe_json(fetch_src), Path(src))
    # 音轨口径与 master 信令必须同源：不认识的音轨转 aac，没音轨纯视频。
    # 三者产出的 master CODECS 都与实际 init/media 一致——反过来就是
    # AVPlayer 整包拒收：黑屏、日志里只一次 master.m3u8、init.mp4 一次
    # 都不请求（2026-09-27 与 2026-10-03 狂飙 ab476f 两次真机复现）。
    maps, declared_audio, transcode_audio = audio_pack_mode(source, maps)
    verdict = mvp_verdict(source)
    if not verdict.supported:
        raise JfPackError(f"拒绝打包：{verdict.reason}")

    # 全色域口径从源推导，不写死 limited：2026-09-26 实测本片源容器
    # color_range=pc、RPU bl_video_full_range_flag=1、signalstats 逐帧
    # YMIN=0/YMAX=1016——容器/码流/像素三证俱全的全色域 10bit。旧代码
    # 三处签 limited（colr、SPS VUI、RPU 口径互相矛盾），真机症状
    # 「颜色明显不对 + DV 徽标不亮」。limited 只对 limited 源成立
    # （06:48 点亮那次是 P7 源，BL 本就 limited）。
    full_range = source_full_range(source)

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    playlist = Path(f"{name}.m3u8")
    # init 段按 cwd 落盘（ffmpeg HLS muxer 的实测行为，与 remux 同一条约束），
    # 所以 cwd 必须钉在 out_dir，否则 init 掉到别处、列表里的相对 URI 失效。
    proc = _run(
        hls_command(
            fetch_src,
            playlist,
            seconds=seconds,
            segment_seconds=segment_seconds,
            colorimetry=_source_colorimetry(source),
            full_range=full_range,
            bsf=dv_signalling_bsf(source),
            maps=maps,
            setts=setts_bsf(source) if normalize_timestamps else None,
            transcode_audio=transcode_audio,
        ),
        cwd=out_dir,
        timeout=ffmpeg_timeout(seconds),
    )
    if not (out_dir / playlist).exists():
        raise JfPackError(f"ffmpeg 未产出列表：{proc.stderr.strip() or '未知错误'}")

    output = resolve_output(out_dir / playlist)
    if not output.exists:
        raise JfPackError(f"产出不完整：init 或分片缺失（{proc.stderr.strip()}）")
    # 残尾分片（末段仅 0.06s / 1~3 帧）是真机黑屏的一个来源：即使时间戳已
    # 被 setts 规整成单调，这种几乎无可呈现内容的片尾仍可能让 AVPlayer 停住。
    # 60s 预览丢掉最后几十毫秒无感知，先摘掉。
    output = trim_stub_tail(output)

    # 全片完整性闸（2026-10-04 深夜 34 部毒包事故）：源流被 Jellyfin 压断时，
    # 回环中继曾把截断的 body 当干净 EOF 转发，ffmpeg 正常退出、产出只有
    # 2~4 段却带 ENDLIST 的「全片」。上层 marker 照写 seconds=0.0 →
    # _cache_fresh 永久命中、_maybe_renew_pack 见 seconds==0 直接跳过 →
    # 电视点片只能放 24 秒且永不自愈（真机症状「有些无法点击播放」）。
    # seconds=0 必须证明产物时长对得上源时长，差超过 40% 就当失败炸穿：
    # 宁可 502 让用户重试，不留毒包。EXTINF 就地求和即可，不再跑一次
    # 全片 ffprobe（playlist_duration 会把整片分片 demux 一遍，分钟级）。
    if seconds == 0 and source.duration > 30.0:
        packed_seconds = sum(playlist_segment_seconds(output.playlist))
        if packed_seconds < source.duration * 0.6:
            raise JfPackError(
                f"源流中途断裂：源 {source.duration:.1f}s 产物仅 {packed_seconds:.1f}s"
            )

    # 0 字节闸门：2026-09-26 两次 init 被截成 0 字节（写盘中途被打断？
    # 原因未明）。AVPlayer 拉完 200 后静默停播，比打包失败更难排查，
    # 宁可当场炸穿返 502。
    empty = [p.name for p in (output.init, *output.segments) if p.stat().st_size == 0]
    if empty:
        raise JfPackError(f"产出有 0 字节文件：{empty}")

    # sidx 归一 + 守门：与 parpack 全片包同一道闸。ffmpeg movenc 遇 MKV 源
    # （含 PGSSUB 多字幕流）会把每段第一个 sidx 的 reference_ID/timescale 写成
    # 未初始化内存值（2026-10-03《纽伦堡》1437/1437 段全坏，tvOS 表现
    # 「无法播放」）。按 init 视频轨真值原地覆写 8 字节后断言全对齐。
    _fixed_sidx, _sidx_notes = normalize_sidx(output.segments, output.init)
    if _sidx_notes:
        raise JfPackError("sidx 结构异常，拒绝打包：%s" % "; ".join(_sidx_notes[:5]))
    verify_sidx(output.segments, output.init)

    # 单层 P8 转签成 P5；P5 源这里空过。分片里的 RPU 一个字节都不碰。
    # stsd 收敛成单 entry：movenc 追加的第二个 dvh1 是死重——trex 默认
    # sample_description_index=1、全部分片 tfhd 无覆盖，只引 entry0。Apple
    # 官方参考流与已验证能播的 1080p 流都是单 entry，tvOS 对 entry_count>1
    # 行为未验证（2026-09-26 真机日志 + 逐分片 tfhd 实测）。必须在 resign 之前，
    # 否则 P8/P8.1 源的多个 dvcC 会让 locate_dv_boxes 撞 ResignError。
    init_bytes, dropped = keep_single_sample_entry(output.init.read_bytes())
    if dropped:
        atomic_write(output.init, init_bytes)

    resign_pack(output.init, source.dovi)

    # 变体 A（默认开）：剥掉 hvcC 里的前缀 SEI(39) 参数集数组，与 Apple 官方
    # DV P5 参考流同构（其 hvcC 只有 32/33/34 三个）。2026-09-26 真机 v2 正在
    # 验证这一臂（v1 只去 colr，v2 再加剥 39）。drop_param_set_type=None 可回退
    # 到 movenc/extract_extradata 原样直出的对照臂。
    if drop_param_set_type is not None:
        init_bytes, dropped_arrays = drop_hvcC_param_set_type(
            output.init.read_bytes(), drop_param_set_type
        )
        if dropped_arrays:
            atomic_write(output.init, init_bytes)

    # init 段必须保留 colr 盒。2026-09-27 真机 A/B 实证：
    #   点亮版 534f.old.signalled（1056B）: dvh1 下有 [colr] size=8+11 (nclx)
    #   不亮版 localtest001   (1463B）: dvh1 下无 colr，只剩 hvcC/dvcC/pasp
    #   两者 SPS/VPS/PPS/dvcC 逐字节一致、ffprobe colorimetry 也相同，
    #   唯一结构差异就是 colr。colr 与 dvcC/colr/PQ/RPU 并称 DV 四要素，
    #   删掉它徽标就灭。movenc 写出的 colr 本来就是 nclx(PQ) 形式。
    if drop_colr:
        init_bytes, removed_colr = remove_box(output.init.read_bytes(), "colr")
        if removed_colr:
            atomic_write(output.init, init_bytes)

    # 2026-10-02 逐 box 对照「点亮版 init」与「当晚不亮版 init」后补的两处容器
    # 归一，只动 init 段、不碰码流语义字节：
    #   · dvcC dv_level：不亮版的片源签 9（8K 档）而实际是 4K，tvOS 直接不认
    #     DV 源；按分辨率钳回 6（点亮版 dvh1.05.06、Apple 1080p 参考流 05.03
    #     同口径，位域 payload[3] 高 5 位，低 3 位 el/bl 标志原样保留）。
    #   · dvh1 sample entry 的 compressor 名：点亮版与 Apple 官方参考流都是空，
    #     Jellyfin 部分片源带 "DOVI Coding" 被 ffmpeg 原样抄进 init，清掉。
    level_notes: list[str] = []
    dv_level = expected_dv_level(source.video.width, source.video.height)
    init_bytes, old_level = clamp_dvcC_level(output.init.read_bytes(), dv_level)
    if old_level is not None:
        atomic_write(output.init, init_bytes)
        level_notes.append(f"dvcC dv_level {old_level}→{dv_level}")
    init_bytes, old_name = clear_compressor_name(init_bytes)
    if old_name is not None:
        atomic_write(output.init, init_bytes)
        level_notes.append(f"compressor 名 {old_name!r} 已清空")

    target = source.dovi.profile5_target
    if target.level > dv_level:
        # master 的 CODECS 走 target_apple_codec：只钳 init 的 dvcC 而 master
        # 还写 dvh1.05.09，两边不一致，AVPlayer 认的是 master 里那个值。
        # level 9 是 8K 档（Apple 官方 4K 参考流 dvh1.05.06），probe 也早把
        # P5 的 level 7/9 列进 _RISKY_P5_LEVELS。
        target = replace(target, level=dv_level)
        level_notes.append(f"master CODECS level 已钳到 {dv_level}")
    effective_seconds = seconds
    if effective_seconds == 0:
        effective_seconds = playlist_duration(output.playlist)
    # EXTINF 长短不齐（关键帧切分，0.04s 到 14.9s 都有），逐段长一起算才拿
    # 得到真峰值；只给总量会把最长的段按平均段长算爆，虚报 30% 以上。
    segment_durations = playlist_segment_seconds(output.playlist)
    bandwidth = bandwidth_from_segments(
        output.segments, effective_seconds, segment_durations
    )
    # master 的 CODECS 必须与实际 -map 产物一致：declared_audio 由
    # audio_pack_mode 按 maps 与源音轨实情算出（纯视频 ""、认识的编码
    # 原样、不认识的编码转码后声明 aac），不能回落到 source.audio_codec
    # ——master 宣称有音频而 init/media 里没有，AVPlayer 直接拒收整个
    # master，真机症状是黑屏、日志里只有一次 master.m3u8 加 video.m3u8、
    # init.mp4 一次都不请求（2026-09-27 实测复现）。
    variant = variant_from_source(
        source,
        master_uri,
        bandwidth=bandwidth,
        target=target,
        audio_codec=declared_audio,
    )
    # 双语字幕轨与 master 同目录落盘（byte-range 切片，不铺碎文件）。
    # 必须走 write_master_playlist 的原子替换：打包中途磁盘打满时直接写会把
    # 电视正在播的 master 冲成 0 字节（ENOSPC → 「播放失败」，见 playlist.py）。
    tracks = write_subtitle_tracks(out_dir, subtitle_texts) if subtitle_texts else []
    master = write_master_playlist(
        out_dir / "master.m3u8", [variant], subtitles=tracks
    )

    verification = verify(source, output, target=target)
    notes = [f"带宽 {bandwidth} bps：{len(output.segments)} 个分片 / {effective_seconds:.3f}s"]
    notes += list(verdict.warnings)
    notes += level_notes
    # 信令规范化：VUI 颜色三件套已由 ffmpeg 侧 hevc_metadata 重写成 P5
    # 口径、range 位按源重写（dv_signalling_bsf + source_full_range），
    # 这里补 hevc_metadata 改不了的 2 位 PTL（tier=1、non_packed=1）并二次
    # 确认 range 位，对齐 expected_signalling(full_range)。签名不认就原样
    # 跳过：打包不因打签失败炸穿。
    if signalling_signature_ok(output.init.read_bytes()) and (
        not output.segments
        or signalling_signature_ok(output.segments[0].read_bytes())
    ):
        stats = apply_dplus(output, full_range=full_range)
        notes.append(
            f"信令规范化 param_sets={stats.param_sets} "
            f"arrays={stats.arrays} vui={stats.vui}"
        )
    else:
        notes.append("SPS/VUI 签名不识别，跳过度数信令规范化")
    # tvOS 终检：绝不上线一条 AVPlayer 会整包拒收的 init。2026-10-03 现场，
    # Profile 8 mkv 片的旧缓存 init 残留 ffmpeg 俗名 dvvC + 第二条 dvh1 entry，
    # tvOS 拉完 master 直接 ITEM FAILED(-11848，CoreMedia -15517 "The media
    # cannot be used on this device")、init 一次都不请求就黑屏。上方
    # keep_single + resign(P8→P5) + drop + clamp 本应产出单 entry + 规范名
    # dvcC；坏了当场 502，别让黑屏从这条缝溜进真机。
    init_final = output.init.read_bytes()
    try:
        video_entries = sample_entry_count(init_final)
    except ValueError:
        video_entries = 0
    if video_entries != 1:
        raise JfPackError(
            f"init 的 video sample entry 数={video_entries}（应为 1），会被 tvOS 拒收"
        )
    if source.dovi is not None:
        dv_boxes = locate_dv_boxes(init_final)
        if not dv_boxes:
            raise JfPackError("DV 源但 init 缺 dvcC/dvvC 配置盒，会被 tvOS 拒收")
        if any(box.fourcc != "dvcC" for box in dv_boxes):
            names = ",".join(box.fourcc for box in dv_boxes)
            raise JfPackError(
                f"init 的 DV 配置盒非规范名 dvcC（{names}），会被 tvOS 拒收"
            )
    return PackResult(
        source=source,
        output=output,
        verification=verification,
        master=master,
        notes=tuple(notes),
    )

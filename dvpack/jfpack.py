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

from dataclasses import dataclass
from pathlib import Path

from .bmff import (
    atomic_write,
    drop_hvcC_param_set_type,
    keep_single_sample_entry,
    remove_box,
)
from .dplus import apply_dplus, signalling_signature_ok

from .p5 import resign_pack
from .playlist import bandwidth_from_segments, variant_from_source, write_master_playlist
from .probe import SourceInfo, ffprobe_json, mvp_verdict, parse_probe_json
from .relay import ffmpeg_visible_source
from .remux import (
    RemuxOutput,
    Verification,
    dv_signalling_bsf,
    hls_command,
    source_full_range,
    resolve_output,
    verify,
)
from .remux import _run


class JfPackError(Exception):
    pass


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
    seconds: float = 60.0,
    segment_seconds: float = 6.0,
    name: str = "video",
    drop_param_set_type: int | None = 39,
    drop_colr: bool = True,
    master_uri: str = "video.m3u8",
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
        ),
        cwd=out_dir,
    )
    if not (out_dir / playlist).exists():
        raise JfPackError(f"ffmpeg 未产出列表：{proc.stderr.strip() or '未知错误'}")

    output = resolve_output(out_dir / playlist)
    if not output.exists:
        raise JfPackError(f"产出不完整：init 或分片缺失（{proc.stderr.strip()}）")

    # 0 字节闸门：2026-09-26 两次 init 被截成 0 字节（写盘中途被打断？
    # 原因未明）。AVPlayer 拉完 200 后静默停播，比打包失败更难排查，
    # 宁可当场炸穿返 502。
    empty = [p.name for p in (output.init, *output.segments) if p.stat().st_size == 0]
    if empty:
        raise JfPackError(f"产出有 0 字节文件：{empty}")

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

    # init 段不落 colr 盒：Apple 官方 DV P5 参考流 init 无 colr，颜色声明走 SPS
    # VUI（hevc_metadata 三件套已由 dv_signalling_bsf 写入）已足够；带 colr
    # nclx(PQ) 有让 AVFoundation 按 HDR10 解释的嫌疑，v1 起是一号嫌疑人。
    # drop_colr=False 回到 movenc 写 colr 的对照臂（真机症状：颜色不对+不亮）。
    if drop_colr:
        init_bytes, removed_colr = remove_box(output.init.read_bytes(), "colr")
        if removed_colr:
            atomic_write(output.init, init_bytes)

    target = source.dovi.profile5_target
    bandwidth = bandwidth_from_segments(output.segments, seconds)
    variant = variant_from_source(source, master_uri, bandwidth=bandwidth, target=target)
    master = write_master_playlist(out_dir / "master.m3u8", [variant])

    verification = verify(source, output, target=target)
    notes = [f"带宽 {bandwidth} bps：{len(output.segments)} 个分片 / {seconds}s"]
    notes += list(verdict.warnings)
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
    return PackResult(
        source=source,
        output=output,
        verification=verification,
        master=master,
        notes=tuple(notes),
    )

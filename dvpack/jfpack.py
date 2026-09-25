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

from .p5 import resign_pack
from .playlist import bandwidth_from_segments, variant_from_source, write_master_playlist
from .probe import SourceInfo, ffprobe_json, mvp_verdict, parse_probe_json
from .remux import RemuxOutput, Verification, hls_command, resolve_output, verify
from .remux import _run


class JfPackError(Exception):
    pass


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
    master_uri: str = "video.m3u8",
) -> PackResult:
    """URL → HLS。闸门不过就不动 ffmpeg；产出不完整就报错，不留半个包。

    master_uri 给绝对路径（/_jf/<id>/video.m3u8）时，master 列表无论被直出
    还是被重定向到别的路径，都能解析到媒体列表——serve 的 /jf/pack 端点靠
    这一点。
    """
    source = parse_probe_json(ffprobe_json(src), Path(src))
    verdict = mvp_verdict(source)
    if not verdict.supported:
        raise JfPackError(f"拒绝打包：{verdict.reason}")

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    playlist = Path(f"{name}.m3u8")
    # init 段按 cwd 落盘（ffmpeg HLS muxer 的实测行为，与 remux 同一条约束），
    # 所以 cwd 必须钉在 out_dir，否则 init 掉到别处、列表里的相对 URI 失效。
    proc = _run(
        hls_command(str(src), playlist, seconds=seconds, segment_seconds=segment_seconds),
        cwd=out_dir,
    )
    if not (out_dir / playlist).exists():
        raise JfPackError(f"ffmpeg 未产出列表：{proc.stderr.strip() or '未知错误'}")

    output = resolve_output(out_dir / playlist)
    if not output.exists:
        raise JfPackError(f"产出不完整：init 或分片缺失（{proc.stderr.strip()}）")

    # 单层 P8 转签成 P5；P5 源这里空过。分片里的 RPU 一个字节都不碰。
    resign_pack(output.init, source.dovi)

    target = source.dovi.profile5_target
    bandwidth = bandwidth_from_segments(output.segments, seconds)
    variant = variant_from_source(source, master_uri, bandwidth=bandwidth, target=target)
    master = write_master_playlist(out_dir / "master.m3u8", [variant])

    verification = verify(source, output, target=target)
    notes = [f"带宽 {bandwidth} bps：{len(output.segments)} 个分片 / {seconds}s"]
    notes += list(verdict.warnings)
    return PackResult(
        source=source,
        output=output,
        verification=verification,
        master=master,
        notes=tuple(notes),
    )

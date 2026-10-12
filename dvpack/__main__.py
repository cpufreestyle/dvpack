"""命令行入口：判定 → 打包 → 起服务，三条命令对应链路的三步。

    python -m dvpack check <文件.mkv>          # 只探测，看它能不能进 MVP
    python -m dvpack pack  <文件.mkv> -o _out/demo --seconds 60
    python -m dvpack serve _out/demo           # 打印给 aTV 的 URL
    python -m dvpack prewarm _out/dv_p7_fix    # Jellyfin 片库批量预热
    python -m dvpack flow _out/dv_p7_fix/_jf/<id>   # 服务端 FRC 插帧，追加 100fps 变体
    python -m dvpack flow _out/dv_p7_fix/_jf/<id> --remaster  # 只补 master 的 flow 行

`check` 单独留着是因为片库有 63 个 DV 文件，验收矩阵要逐个判定，不该为了看结论就跑一遍 remux。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .playlist import (
    bandwidth_from_segments,
    playlist_segment_seconds,
    variant_from_source,
    write_master_playlist,
)
from .bmff import hvcC_param_set_types, parse_file
from .probe import UnknownTransfer, mvp_verdict, probe
from .remux import RemuxError, remux, verify
from .flow import FlowError
from .serve import lan_address, serve as serve_files
from . import prewarm
from . import configcmd
from . import flow
from . import subwarm


def _check(path: Path) -> int:
    source = probe(path)
    dovi = source.dovi
    verdict = mvp_verdict(source)
    print(f"{path.name}")
    print(f"  DOVI     : {dovi}")
    print(f"  判定     : {'可打包' if verdict.supported else '不可打包'} —— {verdict.reason}")
    for warning in verdict.warnings:
        print(f"  风险     : {warning}")
    if verdict.supported:
        # CODECS 报的是**打包后**的值：单层 P8/P7 会被转签成 P5（Apple §1.9 只认 P5）
        target = dovi.profile5_target
        print(f"  CODECS   : {target.target_apple_codec}")
        if target != dovi:
            print(f"             （源是 {dovi.target_apple_codec}，打包时转签成 P5）")
        try:
            print(f"  VIDEO-RANGE: {source.video.apple_video_range}")
        except UnknownTransfer as exc:
            print(f"  风险     : {exc}")
    return 0 if verdict.supported else 1


def _pack(path: Path, out: Path, seconds: float | None) -> int:
    out.mkdir(parents=True, exist_ok=True)
    source, output = remux(path, out, seconds=seconds)
    target = source.dovi.profile5_target if source.dovi else None
    result = verify(source, output, target=target)
    print(f"打包: {path.name} → {out}")
    print(f"  信令   : {result.signalling}")
    print(f"  源     : {source.dovi}")
    if target is not None and target != source.dovi:
        print(f"  转签   : → {target}（Apple 官方 P5 写法）")
    print(f"  复读   : {result.output_dovi}")
    print(f"  分片   : {len(output.segments)} 段")
    for problem in result.problems:
        print(f"  问题   : {problem}")
    if not result.ok:
        print("自检未通过，不写 master 列表。")
        return 1

    # 打包了前 N 秒就用 N 算带宽，否则用整片时长。低报会让客户端选错码率、起播就卡。
    duration = seconds or source.duration
    variant = variant_from_source(
        source,
        output.playlist.name,
        bandwidth=bandwidth_from_segments(
            output.segments, duration, playlist_segment_seconds(output.playlist)
        ),
        target=target,
    )
    write_master_playlist(out / "master.m3u8", [variant])
    print(f"  列表   : {out / 'master.m3u8'}")
    return 0


def _serve(root: Path, port: int, host: str) -> int:
    httpd = serve_files(root, port, host=host)
    displayed = host if host != "0.0.0.0" else lan_address()
    print(f"根目录 : {Path(root).resolve()}")
    print(f"aTV 填 : http://{displayed}:{port}/master.m3u8")
    print("Ctrl-C 停止")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0


def _init(path: Path) -> int:
    """dovi_tool info 口径的独立诊断：对已落盘的 HLS/FMP4 pack 照一遍。

    pack 产物是分段的 init.mp4 + video*.m4s，不是单个媒体文件，
    `check` 走不了（那是给原片源用的）。这个子命令复用 bmff/probe 的
    判据：sample entry 必须 dvh1、配置 box 必须规范名 dvcC、init 段 hvcC
    必须自带 VPS/SPS/PPS。适用于 serve 产物与 Apple 官方参考流的对比。
    """
    init = path / "init.mp4" if path.is_dir() else path
    if not init.is_file():
        print(f"失败：找不到 {init}", file=sys.stderr)
        return 2
    report = parse_file(init)
    video = report.video
    print(f"文件    : {init}")
    if video is None:
        print("  结论   : 不通过 —— init 段里没有视频轨")
        return 1
    print(f"  Sample : {video.sample_entry_type}")
    print(f"  DV box : {video.dv_box or '（无）'}（规范名要求 dvcC）")
    present = hvcC_param_set_types(init.read_bytes())
    print(f"  hvcC   : 参数集 {sorted(present)}（要求 VPS/SPS/PPS=32/33/34）")
    try:
        dovi = probe(init).dovi
        print(f"  DOVI   : {dovi}")
    except Exception as e:
        dovi = None
        print(f"  DOVI   : ffprobe 复读失败 —— {e}")
    ok = True
    if not video.declares_dolby_vision:
        print("  问题   : sample entry 不是 dvh1，电视不会走 DV 解码通路")
        ok = False
    if not video.has_dvcC:
        print("  问题   : 缺规范名 dvcC 配置 box（俗名 dvvC ffprobe 读得出，tvOS 只认 dvcC）")
        ok = False
    missing = [nal for nal in (32, 33, 34) if nal not in present]
    if missing:
        print(f"  问题   : init 段 hvcC 缺参数集 {missing}（Apple 要求 VPS/SPS/PPS 随 init 下发）")
        ok = False
    if dovi is None:
        print("  问题   : ffprobe 复读不出 DV record（RPU 位或 profile 读失败）")
        ok = False
    print(f"  结论   : {'通过' if ok else '不通过'}")
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="dvpack", description="杜比视界 HLS 打包与投喂")
    sub = parser.add_subparsers(dest="cmd", required=True)

    for name, help_text in (
        ("check", "探测并判定"),
        ("pack", "打包成 CMAF/HLS"),
        ("init", "诊断已打包的 init 段（dovi_tool info 口径）"),
    ):
        sp = sub.add_parser(name, help=help_text)
        sp.add_argument("path", type=Path)
        if name == "pack":
            sp.add_argument("-o", "--out", type=Path, default=Path("_out/demo"))
            sp.add_argument("--seconds", type=float, default=None, help="只打包前 N 秒，链路验证用")

    sv = sub.add_parser("serve", help="起局域网 HTTP 服务")
    sv.add_argument("root", type=Path, nargs="?", default=Path("_out/demo"))
    sv.add_argument("--port", type=int, default=8765)
    sv.add_argument("--host", default="0.0.0.0", help="监听地址；默认全部网卡")

    pw = sub.add_parser("prewarm", help="Jellyfin 片库批量预热：全片包提前打进 serve 缓存")
    prewarm.add_arguments(pw)
    cf = sub.add_parser("config", help="Api-Key 配置：--init 写骨架 / --os-key 落 key / 裸跑展示现状")
    configcmd.add_arguments(cf)
    fl = sub.add_parser("flow", help="服务端 FRC 插帧：给现役 pack 追加 2 倍帧率 DV 变体")
    flow.add_arguments(fl)
    sw = sub.add_parser("subwarm",
                        help="Jellyfin 全库字幕批量预热：中英双语提前抓进 L2 _subs 缓存")
    subwarm.add_arguments(sw)

    args = parser.parse_args(argv)
    try:
        if args.cmd == "check":
            return _check(args.path)
        if args.cmd == "pack":
            return _pack(args.path, args.out, args.seconds)
        if args.cmd == "init":
            return _init(args.path)
        if args.cmd == "prewarm":
            return prewarm.run_from_args(args)
        if args.cmd == "config":
            return configcmd.run_from_args(args)
        if args.cmd == "flow":
            return flow.run_from_args(args)
        if args.cmd == "subwarm":
            return subwarm.run_from_args(args)
        return _serve(args.root, args.port, args.host)
    except (RemuxError, FlowError, UnknownTransfer, FileNotFoundError) as exc:
        print(f"失败：{exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

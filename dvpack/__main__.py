"""命令行入口：判定 → 打包 → 起服务，三条命令对应链路的三步。

    python -m dvpack check <文件.mkv>          # 只探测，看它能不能进 MVP
    python -m dvpack pack  <文件.mkv> -o _out/demo --seconds 60
    python -m dvpack serve _out/demo           # 打印给 aTV 的 URL

`check` 单独留着是因为片库有 63 个 DV 文件，验收矩阵要逐个判定，不该为了看结论就跑一遍 remux。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .playlist import bandwidth_from_segments, variant_from_source, write_master_playlist
from .probe import UnknownTransfer, mvp_verdict, probe
from .remux import RemuxError, remux, verify
from .serve import lan_address, serve as serve_files


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
        print(f"  CODECS   : {dovi.target_apple_codec}")
        try:
            print(f"  VIDEO-RANGE: {source.video.apple_video_range}")
        except UnknownTransfer as exc:
            print(f"  风险     : {exc}")
    return 0 if verdict.supported else 1


def _pack(path: Path, out: Path, seconds: float | None) -> int:
    out.mkdir(parents=True, exist_ok=True)
    source, output = remux(path, out, seconds=seconds)
    result = verify(source, output)
    print(f"打包: {path.name} → {out}")
    print(f"  信令   : {result.signalling}")
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
        source, output.playlist.name, bandwidth=bandwidth_from_segments(output.segments, duration)
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="dvpack", description="杜比视界 HLS 打包与投喂")
    sub = parser.add_subparsers(dest="cmd", required=True)

    for name, help_text in (("check", "探测并判定"), ("pack", "打包成 CMAF/HLS")):
        sp = sub.add_parser(name, help=help_text)
        sp.add_argument("path", type=Path)
        if name == "pack":
            sp.add_argument("-o", "--out", type=Path, default=Path("_out/demo"))
            sp.add_argument("--seconds", type=float, default=None, help="只打包前 N 秒，链路验证用")

    sv = sub.add_parser("serve", help="起局域网 HTTP 服务")
    sv.add_argument("root", type=Path, nargs="?", default=Path("_out/demo"))
    sv.add_argument("--port", type=int, default=8765)
    sv.add_argument("--host", default="0.0.0.0", help="监听地址；默认全部网卡")

    args = parser.parse_args(argv)
    try:
        if args.cmd == "check":
            return _check(args.path)
        if args.cmd == "pack":
            return _pack(args.path, args.out, args.seconds)
        return _serve(args.root, args.port, args.host)
    except (RemuxError, UnknownTransfer, FileNotFoundError) as exc:
        print(f"失败：{exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

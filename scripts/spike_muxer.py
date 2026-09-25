"""第 0 步实测：ffmpeg 的哪种写法能在 ISOBMFF 里留下合法的杜比视界信令。

判据（四条全看，缺一条都不算过）：
  1. stsd 里的 sample entry fourcc 是 dvh1 还是被写成 hvc1
  2. dvvC / dvcC box 在不在
  3. in-band RPU NAL 数量与源是否一致（remux 只该改长度前缀，不该丢 NAL）
  4. ffprobe 重新读产出文件时，还能不能报出同一条 DOVI configuration record
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dvpack.bmff import census_annexb, parse_file  # noqa: E402
from dvpack.probe import probe  # noqa: E402
from dvpack.tools import FFMPEG, require  # noqa: E402

SRC = Path(os.environ.get("DVPACK_SOURCE_MKV", ""))
OUT = Path(__file__).resolve().parent.parent / "_out" / "spike"
CLIP = 60  # 秒，够统计 RPU，不必整片

VARIANTS: dict[str, list[str]] = {
    "a_copy_default": ["-c", "copy", "-map", "0:v:0"],
    "b_tag_dvh1": ["-c", "copy", "-map", "0:v:0", "-tag:v", "dvh1"],
    "c_faststart": ["-c", "copy", "-map", "0:v:0", "-movflags", "+faststart"],
    "d_hls_fmp4": ["-c", "copy", "-map", "0:v:0", "-f", "hls", "-hls_segment_type", "fmp4",
                   "-hls_time", "10", "-hls_list_size", "0"],
}


def run(argv: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        [str(require(FFMPEG)), "-hide_banner", "-loglevel", "error", "-y", *argv],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


def dump_es(src: Path, dst: Path, extra: list[str] = ()) -> bool:
    proc = run(["-i", str(src), "-map", "0:v:0", "-c", "copy", *extra, "-f", "hevc", str(dst)])
    return dst.exists() and proc.returncode == 0


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    source = probe(SRC)
    print(f"源: {SRC.name}")
    print(f"    DOVI {source.dovi}")
    print(f"    期望 CODECS = {source.dovi.target_apple_codec} / {source.video.apple_video_range}")

    src_es = OUT / "src.265"
    if not dump_es(SRC, src_es, ["-ss", "0", "-t", str(CLIP)]):
        print("源 ES dump 失败")
        return 1
    base = census_annexb(src_es)
    print(f"    源 RPU 普查: {base}")

    rows = []
    for name, args in VARIANTS.items():
        target = OUT / (f"{name}.m3u8" if name.startswith("d_") else f"{name}.mp4")
        proc = run(["-ss", "0", "-t", str(CLIP), "-i", str(SRC), *args, str(target)])
        init = OUT / f"{name}-init.mp4" if name.startswith("d_") else target
        if not init.exists():
            rows.append((name, "产出失败", "-", "-", proc.stderr.strip()[:70]))
            continue

        report = parse_file(init)
        es = OUT / f"{name}.265"
        ok_es = dump_es(target if not name.startswith("d_") else init, es)
        census = census_annexb(es) if ok_es else None
        try:
            reprobed = probe(target if not name.startswith("d_") else init).dovi
            reprobe_txt = f"P{reprobed.profile}L{reprobed.level}" if reprobed else "ffprobe 看不到 DOVI"
        except Exception as exc:  # noqa: BLE001
            reprobe_txt = f"探测失败 {type(exc).__name__}"
        rows.append(
            (
                name,
                report.summary,
                str(census.dovi_rpu) if census else "-",
                f"{(census.dovi_rpu or 0) == base.dovi_rpu}" if census else "-",
                reprobe_txt,
            )
        )

    print("\n变体              | 信令                          | RPU 数 | 与源一致 | ffprobe 复查")
    print("-" * 96)
    for name, signalling, rpu, same, reprobe in rows:
        print(f"{name:<17} | {signalling:<28} | {rpu:>6} | {same:>8} | {reprobe}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

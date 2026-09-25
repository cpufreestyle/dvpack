"""从片库扫描结果里挑出验收矩阵，并用真实代码路径逐个判定。

矩阵要覆盖的不是"文件多"，而是**信令形状的不同分支**：
P5 的 level 6（基准）、level 7 / 9（Infuse 社区报告不点亮 DV 的区间）、
P8（丢 RPU 会静默退化成 HDR10，是最好的反面探针），以及一个非 DV 对照（应当被拒绝）。
"""

from __future__ import annotations

import csv
import os
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dvpack.probe import NotProbeable, mvp_verdict, probe  # noqa: E402

LIBRARY = Path(os.environ.get("DVPACK_LIBRARY", str(Path.home() / "Movies")))
SCAN = Path(__file__).resolve().parent.parent / "_scratch" / "dovi_library.csv"
# same series, same quality, differing only in level (a probe that isolates
# whether level itself breaks DV lighting).
# Provide it via DVPACK_PAIRED: paths relative to LIBRARY, separated by os.pathsep.
PAIRED = tuple(p for p in os.environ.get("DVPACK_PAIRED", "").split(os.pathsep) if p)
WANTED = [("5", "6"), ("5", "7"), ("5", "9"), ("8", "6"), ("8", "7")]


def load_rows() -> list[dict]:
    with SCAN.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def pick(rows: list[dict]) -> list[tuple[str, dict]]:
    """每个目标分组挑最小的那个文件——探针要跑得快，判定结果与文件大小无关。"""
    dv = [r for r in rows if r["profile"] and r["profile"].isdigit()]
    picks: list[tuple[str, dict]] = []
    for profile, level in WANTED:
        group = [r for r in dv if r["profile"] == profile and r["level"] == level and r["el"] != "1"]
        if not group:
            continue
        present = [r for r in group if (LIBRARY / r["path"]).is_file()]
        if not present:
            continue
        shortest = min(present, key=lambda r: (LIBRARY / r["path"]).stat().st_size)
        picks.append((f"P{profile} L{level}", shortest))
    for rel in PAIRED:
        row = next((r for r in rows if r["path"] == rel), None)
        if row:
            picks.append((f"配对 P{row['profile']} L{row['level']}", row))
    control = next((r for r in rows if not r["profile"]), None)
    if control:
        picks.append(("非 DV 对照", control))
    return picks


def main() -> int:
    rows = load_rows()
    shapes = Counter((r["profile"], r["level"]) for r in rows if r["profile"])
    print(f"片库 {len(rows)} 个文件，DV 信令形状分布：")
    for (profile, level), count in sorted(shapes.items(), key=lambda kv: (-kv[1], kv[0])):
        print(f"  P{profile} L{level}: {count}")

    print("\n判定（走 dvpack.probe，与打包时同一份代码）：")
    print(f"{'分组':<14} {'CODECS':<12} {'RANGE':<6} {'判定':<10} 文件")
    lines = []
    for group, row in pick(rows):
        path = LIBRARY / row["path"]
        try:
            source = probe(path)
        except (NotProbeable, FileNotFoundError) as exc:
            print(f"{group:<14} 探测失败: {exc}")
            continue
        dovi = source.dovi
        verdict = mvp_verdict(source)
        codec = dovi.target_apple_codec if dovi else "-"
        try:
            video_range = source.video.apple_video_range
        except Exception as exc:  # noqa: BLE001
            video_range = f"?{type(exc).__name__}"
        print(f"{group:<14} {codec:<12} {video_range:<6} "
              f"{'可打包' if verdict.supported else '拒绝':<10} {row['path'][:56]}")
        for warning in verdict.warnings:
            print(f"{'':<14}   风险: {warning}")
        lines.append(
            f"| {group} | {path} | {codec} | {video_range} | "
            f"{'可打包' if verdict.supported else verdict.reason} |"
        )

    out = Path("_scratch/matrix.md")
    out.write_text(
        "| 分组 | 文件 | CODECS | VIDEO-RANGE | 判定 |\n| --- | --- | --- | --- | --- |\n"
        + "\n".join(lines)
        + "\n",
        encoding="utf-8",
        newline="\n",
    )
    print(f"\n已写 {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

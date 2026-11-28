"""原生播放器链路：片库审计 + 单文件 MP4 重封装（不切 HLS、不起服务）。

链路出处：`cpufreestyle/tcl-q10g-tv-opt`，2026-11-22 真机验收、2026-11-28 复核。
那台 TCL Q10G 的显示接口对第三方 App 只暴露 SDR（`supportedColorModes [0]`），
Nova 强制 DV 模式无效、Kodi 21.3 播放即 SIGSEGV；**唯一点亮杜比视界徽标的入口是
电视自带的媒体中心**，而它要求片源的容器与 SPS VUI 都把色彩说清楚：
limited range + BT.2020/PQ/BT.2020nc + `dvh1`/`dvcC`。原片 `color_range=pc`
（full range）的直接后果就是「画面偏色 + 徽标不亮」。

这与 Apple/HLS 那条链的口径**相反**，别混用：
  · Apple tvOS（`dvpack pack`）：init 段色彩留白，交给带内 RPU（见 remux 模块注释）；
  · 电视原生播放器（本模块）：色彩三件套必须显式签满，range 必须拉回 tv。
两者共用 `remux.colorimetry_flags()` / `remux.dv_signalling_bsf()` 这唯一实现，
差异只在输出形态：这里是**一个完整 MP4**，放回 NAS 让电视直接浏览。

2026-11-28 实验室闭环（本机 ffmpeg 8.1.2 + dovi_tool，输入是真 DV P5 流）：
  1. 把已验收样本降级成 `color_range=pc` 的 MKV，复现原片缺陷；
  2. 用本模块的命令重封装：ffprobe 复读 `tv + bt2020nc/smpte2084/bt2020`，
     DOVI record 与样本逐字段相同（P5 L7 rpu=1 el=0 bl=1 compat=0）；
  3. `dvpack init <产物>` 通过：sample entry `dvh1`、配置 box `dvcC`、
     hvcC 自带参数集 [32,33,34]；
  4. 产物 8s 窗口 `dovi_tool extract-rpu` 提出 58909 字节 RPU——带内元数据没丢。

> 顺带修正 dvpack 里的旧结论：`remux` 与 `bmff` 的注释都写着「ffmpeg 写的是俗名
> `dvvC`」，实测 ffmpeg 8.1.2 在 `-tag:v dvh1 -strict unofficial` 下写的就是规范名
> `dvcC`（`dvpack init` 复读证实）。老版本才需要 `p5` 的转签补救。
"""

from __future__ import annotations

import csv
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

from . import remux
from .bmff import parse_file
from .probe import NotProbeable, SourceInfo, probe
from .tools import DOVI_TOOL, FFMPEG, ffmpeg_timeout, require

#: 会被扫进片库的扩展名。Jellyfin/NAS 上 DV 片源就这几种容器。
MEDIA_EXTS = frozenset({".mkv", ".mp4", ".m4v", ".mov", ".ts", ".webm"})

#: ffprobe 的 format_name 里出现任一子串即认为是 MP4 家族（能拆 stsd 才有 fourcc/dvC 可判）
_MP4_HINTS = ("mp4", "mov", "isom", "m4v", "3gp")

#: 验收口径：limited range + BT.2020/PQ/BT.2020nc
TARGET_RANGE = "tv"
TARGET_COLOR = remux.DV_P5_COLORIMETRY

#: 产物名后缀。带后缀是为了批处理时绝不和原片同名，也不会有覆盖风险。
DEFAULT_SUFFIX = "-dv"

#: RPU 带内普查只抽前 N 秒（整片扫太慢，RPU 逐帧存在，抽样足够判死）
RPU_WINDOW_SECONDS = 8.0

ACTION_FIX = "fix"
ACTION_OK = "ok"
ACTION_SKIP = "skip"
ACTION_ERROR = "error"


@dataclass(frozen=True)
class NativeVerdict:
    """一个文件在「原生播放器」口径下的判定。

    action: fix=要重封装 / ok=已合规 / skip=本来就不该进这条链 / error=探测失败
    reasons: 需要动的项（fix）或不动的理由（skip），逐条可读。
    """

    action: str
    reasons: tuple[str, ...] = ()
    container: str = ""
    colour: str = ""
    dovi: str = ""
    signalling: str = ""

    @property
    def needs_fix(self) -> bool:
        return self.action == ACTION_FIX


def _is_mp4ish(source: SourceInfo) -> bool:
    fmt = source.format_name.lower()
    return any(hint in fmt for hint in _MP4_HINTS)


def classify(source: SourceInfo, dv_box: str = "", sample_entry: str = "") -> NativeVerdict:
    """按原生播放器口径判定；`dv_box` / `sample_entry` 来自 bmff 拆 moov（非 MP4 传空）。"""
    video = source.video
    dovi = video.dovi
    colour = (
        f"{video.color_range or '?'}/{video.color_primaries or '?'}/"
        f"{video.color_transfer or '?'}/{video.color_space or '?'}"
    )
    base = dict(container=source.format_name, colour=colour, dovi=str(dovi))

    if video.codec != "hevc":
        # 电视的 media_codecs 还列了 AVC/AV1 的 DV 解码器，但那条路在这台机器上没验收过
        return NativeVerdict(
            ACTION_SKIP, (f"视频轨是 {video.codec or '?'}，原生链路只验过 HEVC 单层 DV",), **base
        )
    if dovi is None:
        # fourcc 已经写成 dvh1 / 容器里还有 dvC box，却没有 DOVI record：
        # 这是「被 naive 的 ffmpeg copy 洗过一遍」的形态——2026-11-28 实测，
        # 源是已验收样本，`-c copy -tag:v dvh1` 不给 `-strict unofficial`
        # 会把 dvcC 静默丢掉（ffprobe 复读只剩 fourcc=dvh1、无 side data）。
        # 带内 RPU 还在码流里，要重封装补回 box，不能当非 DV 跳过。
        if sample_entry in ("dvh1", "dvhe") or dv_box:
            return NativeVerdict(
                ACTION_FIX,
                ("容器里没有 DOVI configuration record（dvcC 丢失），"
                 "但 fourcc 是 DV 写法——带内 RPU 可能仍在，需重封装补回 box",),
                **base,
            )
        return NativeVerdict(ACTION_SKIP, ("无 DOVI configuration record，不是杜比视界片源",), **base)
    if not dovi.rpu_present:
        return NativeVerdict(ACTION_SKIP, ("RPU 缺失：重封装也点不亮，电视只会当普通 HDR",), **base)
    if not dovi.is_single_layer:
        return NativeVerdict(
            ACTION_SKIP,
            (f"Profile {dovi.profile} 带增强层（双层），要先丢 EL 才能进这条链",),
            **base,
        )
    if dovi.profile not in (5, 8):
        return NativeVerdict(
            ACTION_SKIP, (f"Profile {dovi.profile} 不在验收范围（只验过 5 / 8）",), **base
        )

    reasons: list[str] = []
    if not _is_mp4ish(source):
        reasons.append(f"容器是 {source.format_name or '?'}，原生播放器要 MP4")
    if (video.color_range or "").lower() != TARGET_RANGE:
        reasons.append(
            f"color_range={video.color_range or '未声明'}"
            "（full range / 未声明会让原生播放器偏色且不亮标，必须签 tv）"
        )
    got = (video.color_primaries, video.color_transfer, video.color_space)
    if any(not v for v in got) or got != TARGET_COLOR:
        reasons.append(
            "色彩三件套 "
            f"{got[0] or '?'}-{got[1] or '?'}-{got[2] or '?'} ≠ "
            f"{TARGET_COLOR[0]}-{TARGET_COLOR[1]}-{TARGET_COLOR[2]}"
        )
    if sample_entry and sample_entry != "dvh1":
        reasons.append(f"sample entry 是 {sample_entry}，不是 dvh1")
    if dv_box and dv_box != "dvcC":
        reasons.append(f"DV 配置 box 是 {dv_box}，要规范名 dvcC")
    if dovi.profile == 8:
        reasons.append(
            "Profile 8：这台电视只真机验收过 P5；产物按源签 dvcC 未转签 P5，"
            "先当试验片看待"
        )

    signalling = f"{sample_entry or '（无 fourcc）'}+{dv_box or '（无 dvC）'}"
    if reasons:
        return NativeVerdict(ACTION_FIX, tuple(reasons), signalling=signalling, **base)
    return NativeVerdict(ACTION_OK, (), signalling=signalling, **base)


def inspect(path: str | Path) -> NativeVerdict:
    """探测一个文件：ffprobe 读色彩/DV record，MP4 再拆 moov 读 fourcc 与 dvC box。

    拆 moov 只做有界读（`bmff.parse_file` 只把 moov 读进内存），扫全库不会把整片拖一遍。
    """
    path = Path(path)
    try:
        source = probe(path)
    except (NotProbeable, FileNotFoundError, OSError) as exc:
        return NativeVerdict(ACTION_ERROR, (f"探测失败：{exc}",))
    dv_box = sample_entry = ""
    if _is_mp4ish(source):
        report = parse_file(path)
        video = report.video
        if video is not None:
            dv_box = video.dv_box or ""
            sample_entry = video.sample_entry_type or ""
    return classify(source, dv_box, sample_entry)


def native_command(src: Path, dst: Path, source: SourceInfo) -> list[str]:
    """重封装命令：不重编码，只把容器、fourcc、dvcC、SPS VUI 颜色签成验收口径。

    flag 全部取自 remux 的唯一实现，避免第二套 DV 信令写法漂移：
      · `DV_VIDEO_FLAGS`：`-tag:v dvh1 -strict unofficial`，少了 -strict ffmpeg 拒绝写配置 box；
      · `colorimetry_flags()`：容器层写 colr（当前口径 = 显式 BT.2020/PQ/BT.2020nc + tv）；
      · `dv_signalling_bsf(source)`：SPS VUI 重写 + MKV 源的参数集注入链。
    只搬第 1 路视频 + 第 1 路音轨（与 2026-11-22 验收样本一致），字幕不带——
    原生播放器读 MP4 里 mov_text 字幕的行为没在这台电视上验过，不猜。
    """
    bsf = remux.dv_signalling_bsf(source)
    argv = [
        str(require(FFMPEG)),
        "-hide_banner",
        "-v",
        "error",
        "-y",
        "-i",
        str(src),
        *remux.VIDEO_AUDIO_MAPS,
        "-c",
        "copy",
        *remux.DV_VIDEO_FLAGS,
        *remux.colorimetry_flags(),
    ]
    if bsf:
        argv += ["-bsf:v", ",".join(bsf)]
    argv.append(str(dst))
    return argv


@dataclass
class NativeResult:
    src: Path
    dst: Path
    status: str  # fixed / exists / dry-run / verify-fail / failed / skipped / no-space
    verdict: NativeVerdict | None = None
    detail: str = ""


def _rpu_bytes(path: Path, seconds: float = RPU_WINDOW_SECONDS) -> int:
    """产物里带内 RPU 的字节数（抽 N 秒 → Annex-B → dovi_tool extract-rpu）。

    dovi_tool 不在 PATH 也不在 tools/bin 时返回 -1，调用方按「未复核」处理而不是失败。
    """
    try:
        tool = require(DOVI_TOOL)
    except FileNotFoundError:
        return -1
    with tempfile.TemporaryDirectory(prefix="dvnative.") as tmp:
        clip = Path(tmp) / "clip.hevc"
        rpu = Path(tmp) / "rpu.bin"
        cut = subprocess.run(
            [
                str(require(FFMPEG)), "-hide_banner", "-v", "error", "-y",
                "-t", str(seconds), "-i", str(path),
                "-map", "0:v:0", "-c", "copy", "-bsf:v", "hevc_mp4toannexb",
                str(clip),
            ],
            capture_output=True,
            text=True,
            timeout=ffmpeg_timeout(seconds * 4),
        )
        if cut.returncode != 0 or not clip.is_file() or clip.stat().st_size == 0:
            return 0
        got = subprocess.run(
            [str(tool), "extract-rpu", "-i", str(clip), "-o", str(rpu)],
            capture_output=True,
            text=True,
            timeout=ffmpeg_timeout(seconds * 4),
        )
        if got.returncode != 0 or not rpu.is_file():
            return 0
        return rpu.stat().st_size


def remux_native(
    src: Path,
    dst: Path,
    *,
    source: SourceInfo | None = None,
    dry_run: bool = False,
    force: bool = False,
    check_rpu: bool = True,
) -> NativeResult:
    """把一个文件按原生口径重封装到 `dst`。原片永远只读。"""
    dst = Path(dst)
    if dry_run:
        src_source = source or probe(src)
        return NativeResult(src, dst, "dry-run", detail=" ".join(native_command(src, dst, src_source)))
    if dst.exists() and not force:
        return NativeResult(src, dst, "exists", detail="产物已存在，加 --force 才会覆盖")

    try:
        src_source = source or probe(src)
    except (NotProbeable, FileNotFoundError, OSError) as exc:
        return NativeResult(src, dst, "failed", detail=f"探测失败：{exc}")

    dst.parent.mkdir(parents=True, exist_ok=True)
    # 先落临时名：中途失败 / 磁盘满不会留下一个「看着像成片」的半成品骗过后面的复检，
    # 也不会污染同名的既有产物。命令必须写这个临时路径，写 dst 的复检等于自欺。
    # 后缀必须留在最后——mp4 muxer 靠扩展名猜格式，`x.mp4.part` 会直接
    # "Error opening output files: Invalid argument"（2026-11-28 实测踩到）。
    tmp_dst = dst.with_name(f"{dst.stem}.part{dst.suffix}")
    argv = native_command(src, tmp_dst, src_source)
    try:
        proc = subprocess.run(
            argv, capture_output=True, text=True, timeout=ffmpeg_timeout(src_source.duration)
        )
    except subprocess.TimeoutExpired:
        tmp_dst.unlink(missing_ok=True)
        return NativeResult(src, dst, "failed", detail="ffmpeg 超时已杀")
    if proc.returncode != 0:
        tmp_dst.unlink(missing_ok=True)
        tail = (proc.stderr or "").strip().splitlines()[-1:] or ["无 stderr"]
        return NativeResult(src, dst, "failed", detail=tail[0][:300])
    tmp_dst.replace(dst)

    verdict = inspect(dst)
    detail = f"{verdict.container} {verdict.colour} {verdict.signalling}"
    if verdict.action != ACTION_OK:
        dst.unlink(missing_ok=True)
        return NativeResult(src, dst, "verify-fail", verdict, "; ".join(verdict.reasons) or detail)
    if check_rpu:
        n = _rpu_bytes(dst)
        if n == 0:
            dst.unlink(missing_ok=True)
            return NativeResult(src, dst, "verify-fail", verdict, "产物里抽不到带内 RPU（重封装丢了元数据）")
        if n > 0:
            detail += f" RPU={n}B"
        else:
            detail += " RPU=未复核（缺 dovi_tool）"
    return NativeResult(src, dst, "fixed", verdict, detail)


@dataclass
class AuditRow:
    path: str
    action: str
    container: str
    colour: str
    dovi: str
    signalling: str
    reasons: str
    size: int = 0

    def as_dict(self) -> dict[str, str | int]:
        return {
            "path": self.path,
            "action": self.action,
            "container": self.container,
            "colour": self.colour,
            "dovi": self.dovi,
            "signalling": self.signalling,
            "reasons": self.reasons,
            "size": self.size,
        }


CSV_FIELDS = ("path", "action", "container", "colour", "dovi", "signalling", "reasons", "size")


def iter_media(roots: list[Path], *, exts: frozenset[str] = MEDIA_EXTS) -> list[Path]:
    """收集待扫文件：跳过隐藏项和 `_` 前缀目录（Jellyfin/dvpack 的工作目录约定）。"""
    found: list[Path] = []
    seen: set[Path] = set()
    for root in roots:
        root = Path(root)
        if root.is_file():
            if root.suffix.lower() in exts and root not in seen:
                seen.add(root)
                found.append(root)
            continue
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*")):
            name = path.name
            if not path.is_file() or path.suffix.lower() not in exts:
                continue
            if any(part.startswith((".", "_")) for part in path.relative_to(root).parts):
                continue
            if path not in seen:
                seen.add(path)
                found.append(path)
    return found


def audit_row(path: Path) -> AuditRow:
    verdict = inspect(path)
    try:
        size = path.stat().st_size
    except OSError:
        size = 0
    return AuditRow(
        path=str(path),
        action=verdict.action,
        container=verdict.container,
        colour=verdict.colour,
        dovi=verdict.dovi,
        signalling=verdict.signalling,
        reasons=" | ".join(verdict.reasons),
        size=size,
    )


def mirror_target(src: Path, root: Path, out_dir: Path, suffix: str) -> Path:
    """把原片在库里的相对目录结构搬到产物目录，媒体中心/刮削器看到的层级不变。

    纯字面路径运算（不碰文件系统）：root 不在 src 之下就退化成平铺，
    产物名一律带 `suffix`，绝不与原片同名。
    """
    src, root = Path(src), Path(root)
    try:
        rel_parent = src.parent.relative_to(root)
    except ValueError:
        rel_parent = Path()
    return Path(out_dir) / rel_parent / f"{src.stem}{suffix}.mp4"


def summarize(rows: list[AuditRow]) -> str:
    counts: dict[str, int] = {}
    for row in rows:
        counts[row.action] = counts.get(row.action, 0) + 1
    order = (ACTION_FIX, ACTION_OK, ACTION_SKIP, ACTION_ERROR)
    parts = [f"{a}={counts.get(a, 0)}" for a in order if counts.get(a, 0)]
    total_mb = sum(r.size for r in rows) / 1024 / 1024
    fix_mb = sum(r.size for r in rows if r.action == ACTION_FIX) / 1024 / 1024
    return (
        f"共 {len(rows)} 个文件（{total_mb:.0f}MB）：" + " ".join(parts)
        + f"；待重封装占用 {fix_mb:.0f}MB，产物另需同等空间"
    )


def add_arguments(parser) -> None:
    parser.add_argument("paths", type=Path, nargs="+", help="片库目录或单个文件（可给多个）")
    parser.add_argument("--csv", type=Path, default=None, help="审计报告写到 CSV")
    parser.add_argument("-o", "--out", type=Path, default=Path("_out/native"), help="产物目录（默认 _out/native）")
    parser.add_argument("--suffix", default=DEFAULT_SUFFIX, help=f"产物名后缀（默认 {DEFAULT_SUFFIX}）")
    parser.add_argument("--force", action="store_true", help="产物已存在时仍然重做并覆盖")
    parser.add_argument("--dry-run", action="store_true", help="只打印 ffmpeg 命令，不落盘")
    parser.add_argument("--limit", type=int, default=0, help="最多处理 N 个待修文件（0=不限）")
    parser.add_argument("--no-rpu-check", action="store_true", help="跳过带内 RPU 复核（快，但不建议）")
    parser.add_argument(
        "--reserve",
        type=float,
        default=2.0,
        help="产物目录所在卷至少保留多少 GB 空闲，不够就停批（默认 2）",
    )


def _free_bytes(path: Path) -> int:
    """产物目录所在卷的剩余空间；目录还没建就往上找到第一个存在的祖先。"""
    here = Path(path)
    for cand in [here, *here.parents]:
        if cand.exists():
            return shutil.disk_usage(cand).free
    return shutil.disk_usage("/").free


def run_from_args(args, *, fix: bool) -> int:
    """audit 与 native 共用一条扫描链：audit 只报，native 在报的基础上真出片。"""
    files = iter_media(args.paths)
    if not files:
        print("没找到媒体文件（只扫 " + ", ".join(sorted(MEDIA_EXTS)) + "，且跳过 . / _ 目录）")
        return 1

    rows: list[AuditRow] = []
    results: list[NativeResult] = []
    fixed = 0
    reserve_bytes = int(args.reserve * 1024**3)
    stopped = ""

    for path in files:
        row = audit_row(path)
        rows.append(row)
        label = row.action
        extra = f" [{row.colour}]" if row.action == ACTION_FIX else ""
        print(f"{label:>6}  {path}  {row.reasons or row.signalling}{extra}")
        if not fix or row.action != ACTION_FIX:
            continue
        if args.limit and fixed >= args.limit:
            if fixed == args.limit:
                print(f"SKIP   已达 --limit {args.limit}，后面的待修文件只列不修")
            continue
        if stopped:
            continue
        root = next((r for r in args.paths if Path(r).is_dir() and path.is_relative_to(Path(r))), path.parent)
        dst = mirror_target(path, root, args.out, args.suffix)
        if not args.dry_run:
            free = _free_bytes(dst.parent)
            if free < row.size + reserve_bytes:
                stopped = f"磁盘余量 {free / 1024**3:.1f}GB 装不下 {path.name}（需要 {row.size / 1024**3:.1f}GB + 保留 {args.reserve:g}GB），本批停在这里"
                print(f"STOP   {stopped}")
                continue
        res = remux_native(
            path,
            dst,
            dry_run=args.dry_run,
            force=args.force,
            check_rpu=not args.no_rpu_check,
        )
        results.append(res)
        print(f"{res.status:>8}  {res.src.name} → {res.dst}  {res.detail}")
        if res.status == "fixed":
            fixed += 1
        elif res.status in ("failed", "verify-fail"):
            print("         （产物已丢弃，原片未动；原因见 detail）", file=sys.stderr)

    print(summarize(rows))
    if fix:
        done = sum(1 for r in results if r.status == "fixed")
        dry = sum(1 for r in results if r.status == "dry-run")
        if args.dry_run:
            print(f"dry-run：{dry} 条命令待执行，未落盘。去掉 --dry-run 即出片。")
        else:
            print(f"重封装完成 {done} 个，产物在 {args.out}；原片一律未改动。")
    if args.csv:
        args.csv.parent.mkdir(parents=True, exist_ok=True)
        with args.csv.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS)
            writer.writeheader()
            for row in rows:
                writer.writerow(row.as_dict())
        print(f"报告：{args.csv}")
    bad = [r for r in rows if r.action == ACTION_ERROR] + [r for r in results if r.status not in ("fixed", "exists", "dry-run")]
    return 1 if (stopped or bad) else 0

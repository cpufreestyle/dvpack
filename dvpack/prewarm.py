"""prewarm：Jellyfin 片库批量预热——把全片包提前打进 serve 现役缓存。

为什么存在：/jf/pack 的秒开通道（INSTANT_PACK_SECONDS 起播 + 后台补全片）
解决了「点片没反应」，但每部首片点下去仍要等后台全片 ffmpeg 跑几分钟。
用户 2026-10-02 的建议：拿片单后台批量打包，点片即秒回全片。

与 serve 的契约（判据全套复用 serve 现役逻辑，一条不自己搓）：
- src 形状：http://<base>/Videos/<itemId>/stream.<ext>?static=true&api_key=<token>
  与 dvplay 发来的 packURL 逐字一致——ext 取自 Jellyfin item Path 后缀，
  2026-10-05 实测现役 marker 的 stream.mkv/stream.mp4 与 Path 后缀一一对应。
  指纹差一个字节 serve 就判未命中、整片重打，预热白做。
- 全片打包复用 serve._pack_full_swap：兄弟临时目录打完、marker 落定
  （seconds=0.0 + plen）再整体换装；在播 stub 的视频分片一字节不碰。
- 字幕复用 serve._subtitle_texts（本地级）；无字幕片且在线腿开着时同步跑
  serve._online_subs_body——分钟级的公网抓取只允许出现在这种批处理里，
  302 热路径永远不碰。
- item 锁复用 serve._pack_lock_for：serve 正给同一 item 补全片时排队等，
  绝不让两个 ffmpeg 抢同一目录。
- 并发默认 1 路、硬顶 2 路：2026-10-04 深夜 34 路 worker 同时从 Jellyfin
  抽流，服务器被压到逐个断流，中继把截断 body 当干净 EOF 转发，产出一批
  24 秒的「全片」毒包。批量预热同样是从片库抽流，不能重蹈覆辙。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
import traceback
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

from . import jfmeta, serve, subsource
from .jfmeta import JfMetaError, classify_subtitles, list_items, subtitle_languages
from .remux import VIDEO_AUDIO_MAPS

#: 并发硬顶（理由见模块 docstring 的毒包事故）。默认 1 路顺序打。
MAX_JOBS = 2

#: 每个 item 开打前的磁盘下限，与 serve.MIN_FREE_BYTES 同口径（12GB）。
#: 预热不擅自 LRU 淘汰别人的包（那是 serve 点片时的职权），不够就跳过。
DISK_RESERVE_BYTES = 12 * 1024 ** 3

#: 决策/结局动作文案，CLI 输出与测试断言共用。
ACTION_PACK = "pack"
ACTION_HIT = "hit"
ACTION_SKIP = "skip"
ACTION_FAILED = "failed"


def build_src(base_url: str, item_id: str, ext: str, token: str) -> str:
    """重建 dvplay 同款 packURL：容器后缀 + static=true + api_key。

    ext 必须来自 item Path 后缀（.mkv/.mp4）：它参与 _src_fingerprint，
    猜错一个字母 serve 就判缓存未命中、整片重打，预热白做。api_key 不进
    指纹（_src_fingerprint 会剥），放这里只为让 Jellyfin 放行。

    小写归一对的是 dvplay 的取法：packURL 用 Jellyfin MediaSource.Container
    （探测值天然小写，实测与 Path 后缀逐字一致），而 prewarm 从 Path 后缀
    取；遇 .MP4 这类命名只有两侧都归小写，指纹才能保证同款。
    """
    return "%s/Videos/%s/stream.%s?static=true&api_key=%s" % (
        base_url.rstrip("/"), item_id, ext.lstrip(".").lower(), token)


def _display_name(name: str, path: str) -> str:
    """Name 是刮削残渣时退回文件名茎。

    实测（2026-10-05）70 条片库有 28 条 Name 是 `.Once.Upon.a.Bite`
    这种以路径分隔符开头的残渣（风味人间全季刮削崩了），打印出来没法
    认片，退回带发行名的文件名茎。真标题不会以 . / - _ 开头；stem
    本身就等于 Name 的（库本就无元数据）不受影响。
    """
    name = (name or "").strip()
    stem = Path(path).stem if path else ""
    if not name:
        return stem or "(未命名)"
    if name[0] in "./-_":
        return stem
    return name

def _disp_width(text: str) -> int:
    """显示宽度：CJK 宽字符算 2 列，对齐用。"""
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1
               for c in text)


def _disp_clip(text: str, width: int) -> str:
    """按显示宽度截断加省略号，超宽一律收到 width 列以内。"""
    if _disp_width(text) <= width:
        return text
    chars = []
    used = 0
    for ch in text:
        step = 2 if unicodedata.east_asian_width(ch) in "WF" else 1
        if used + step > width - 1:
            break
        chars.append(ch)
        used += step
    return "".join(chars) + "…"


def _disp_pad(text: str, width: int) -> str:
    return text + " " * max(0, width - _disp_width(text))


@dataclass
class PreheatItem:
    """片库一条：枚举事实 + 预热决策要用的最小集。"""

    item_id: str
    name: str
    year: int | None
    path: str
    ext: str  # "" = Path 推不出容器后缀 → src 无法重建，只能跳过
    kind: str  # classify_subtitles 四分类：text/pgs/mixed/none
    langs: set[str] = field(default_factory=set)

    @property
    def label(self) -> str:
        year = " (%d)" % self.year if self.year else ""
        return "《%s》%s" % (self.name, year)


def enumerate_items(base_url: str, token: str,
                    include_types: str = "Movie,Episode,MusicVideo,Video") -> list[PreheatItem]:
    """片库 → PreheatItem 列表。Path/MediaStreams 两跳都由 jfmeta.list_items 带全。"""
    out: list[PreheatItem] = []
    for it in list_items(base_url, token, include_types=include_types):
        item_id = it.get("Id") or ""
        if not item_id:
            continue
        path = it.get("Path") or ""
        ext = Path(path).suffix.lstrip(".").lower()
        streams = it.get("MediaStreams") or []
        kind = classify_subtitles(streams)
        langs = subtitle_languages(streams) if kind in ("text", "mixed") else set()
        out.append(PreheatItem(
            item_id=item_id,
            name=_display_name(it.get("Name") or "", path),
            year=it.get("ProductionYear"),
            path=path,
            ext=ext,
            kind=kind,
            langs=langs,
        ))
    return out


@dataclass
class PreheatDecision:
    """一条 item 的预热决策。src/out_dir 只有 pack/hit 才填——skip 无从重建。"""

    item: PreheatItem
    action: str  # ACTION_PACK / ACTION_HIT / ACTION_SKIP
    reason: str
    src: str = ""
    out_dir: Path | None = None


def _read_meta(out_dir: Path) -> dict:
    """读 marker；坏 JSON/不存在当空 dict（与 serve 内联判据同口径）。"""
    try:
        return json.loads((out_dir / "source.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _pack_mtime(out_dir: Path) -> float:
    """产物最后使用时间：master/video 列表的 mtime 取大（同 serve LRU 口径）。"""
    stamp = 0.0
    for name in ("video.m3u8", "master.m3u8"):
        try:
            stamp = max(stamp, (out_dir / name).stat().st_mtime)
        except OSError:
            continue
    return stamp


def _full_pack_fresh(out_dir: Path, src: str, older_than_days: float,
                     now: float) -> tuple[bool, str]:
    """(现役缓存里的全片包是否够新, 原因)。

    判据 = serve._cache_fresh(seconds=0.0) 且非毒包；_pack_playable 单独
    不够用——20 秒秒开 stub 也「可播」，但那正是预热要升级成全片的对象。
    older_than_days>0 时，连新鲜全片包也按 mtime 过期重打（片库内容会变）。
    """
    meta = _read_meta(out_dir)
    if serve._pack_poisoned(meta, out_dir):
        return False, "毒包（自称全片实则超短），重打"
    if not serve._cache_fresh(out_dir, src, 0.0):
        seconds = meta.get("seconds")
        if seconds == 0.0:
            return False, "全片包缺 master/分片，重打"
        if seconds:
            return False, "现役包只有 %.0fs 预览段，升级全片" % float(seconds)
        return False, "无现役缓存"
    if older_than_days > 0:
        age_days = (now - _pack_mtime(out_dir)) / 86400.0
        if age_days >= older_than_days:
            return False, "全片包已 %.1f 天，超龄重打" % age_days
    return True, "全片包已在"


def plan_prewarm(root: Path, base_url: str, token: str, items: list[PreheatItem],
                 *, older_than_days: float = 0.0, limit: int = 0,
                 now: float | None = None) -> list[PreheatDecision]:
    """把片库分成三堆：hit（全片包已在）/ pack（要打）/ skip（打不了）。

    limit 只数 pack：命中的老包照常汇报但不占额度，超额的记 skip 原因。
    """
    now = time.time() if now is None else now
    decisions: list[PreheatDecision] = []
    packed = 0
    for item in items:
        if not item.ext:
            decisions.append(PreheatDecision(
                item=item, action=ACTION_SKIP,
                reason="Path 无容器后缀，src 无法重建（指纹会对不上）"))
            continue
        src = build_src(base_url, item.item_id, item.ext, token)
        out_dir = Path(root) / "_jf" / item.item_id
        fresh, reason = _full_pack_fresh(out_dir, src, older_than_days, now)
        if fresh:
            decisions.append(PreheatDecision(
                item=item, action=ACTION_HIT, reason=reason,
                src=src, out_dir=out_dir))
            continue
        if limit and packed >= limit:
            decisions.append(PreheatDecision(
                item=item, action=ACTION_SKIP,
                reason="超过 --limit %d" % limit))
            continue
        packed += 1
        decisions.append(PreheatDecision(
            item=item, action=ACTION_PACK, reason=reason,
            src=src, out_dir=out_dir))
    return decisions


@dataclass
class PreheatReport:
    """一批预热的结局。failed 非空时 CLI 退 1——批处理工具要能当监控看。"""

    root: Path
    packed: list[tuple[str, str, float]] = field(default_factory=list)   # (label, id, GB)
    hits: list[tuple[str, str]] = field(default_factory=list)
    skipped: list[tuple[str, str, str]] = field(default_factory=list)   # (label, id, reason)
    failed: list[tuple[str, str, str]] = field(default_factory=list)    # (label, id, error)
    elapsed: float = 0.0

    @property
    def ok(self) -> bool:
        return not self.failed


def _pack_one(root: Path, decision: PreheatDecision,
              log) -> tuple[str, str, float]:
    """打包一部。回 (status, 说明, GB)；绝不对调用抛异常。

    status ∈ ACTION_PACK / ACTION_HIT / ACTION_SKIP / ACTION_FAILED。
    """
    item = decision.item
    src, out_dir = decision.src, decision.out_dir
    try:
        if serve._free_bytes(Path(root)) < DISK_RESERVE_BYTES:
            log("[预热] 跳过 %s：磁盘可用不足 %.0fGB（不淘汰别人的包）"
                % (item.label, DISK_RESERVE_BYTES / 1024 ** 3))
            return ACTION_SKIP, "磁盘可用不足 %.0fGB" % (
                DISK_RESERVE_BYTES / 1024 ** 3), 0.0
        # item 锁与 serve 同款：点片正在补全片就排队，等完复查多半已命中。
        with serve._pack_lock_for(item.item_id):
            fresh, reason = _full_pack_fresh(out_dir, src, 0.0, time.time())
            if fresh:
                return ACTION_HIT, reason, 0.0
            # 不清 _part*：那是 serve 并行分包在自己 pack 目录内的职权，
            # glob 匹配不到目标、反而可能删 serve 在飞 stub 的分段目录。
            resolution = serve._subtitle_texts(src, root=root)
            texts = resolution.texts
            if resolution.level:
                log("[预热] 字幕 %s 来自 %s %s"
                    % (item.label, resolution.level, resolution.note))
            started = time.time()
            serve._pack_full_swap(src, out_dir, VIDEO_AUDIO_MAPS, texts,
                                  item.item_id)
            took = time.time() - started
            if not texts and subsource.online_enabled():
                receipt = serve._online_subs_body(root, src, item.item_id, out_dir)
                log("[预热] 在线字幕 %s：%s" % (item.label, receipt))
        size_gb = serve._dir_size_bytes(out_dir) / 1024 ** 3
        log("[预热] 打包 %s 全片 %.1fGB 用时 %.0fs" % (item.label, size_gb, took))
        return ACTION_PACK, "全片 %.1fGB 用时 %.0fs" % (size_gb, took), size_gb
    except Exception as exc:  # 批量不能被一部片打断；真实原因进 traceback
        traceback.print_exc()
        log("[预热] 失败 %s：%s" % (item.label, exc))
        return ACTION_FAILED, str(exc), 0.0


def run_prewarm(root: Path, decisions: list[PreheatDecision], *,
                jobs: int = 1, log=print) -> PreheatReport:
    """按计划打包。默认 1 路顺序；jobs 硬顶 MAX_JOBS（毒包事故教训）。"""
    jobs = max(1, min(int(jobs), MAX_JOBS))
    report = PreheatReport(root=Path(root))
    started = time.time()
    try:
        for decision in decisions:
            if decision.action == ACTION_HIT:
                report.hits.append((decision.item.label, decision.item.item_id))
            elif decision.action == ACTION_SKIP:
                report.skipped.append(
                    (decision.item.label, decision.item.item_id, decision.reason))
        queue = [d for d in decisions if d.action == ACTION_PACK]
        progress = [0]
        progress_guard = threading.Lock()

        def handle(decision: PreheatDecision) -> None:
            status, detail, size_gb = _pack_one(report.root, decision, log)
            if status == ACTION_PACK:
                report.packed.append(
                    (decision.item.label, decision.item.item_id, size_gb))
            elif status == ACTION_HIT:
                report.hits.append((decision.item.label, decision.item.item_id))
            elif status == ACTION_SKIP:
                report.skipped.append(
                    (decision.item.label, decision.item.item_id, detail))
            else:
                report.failed.append(
                    (decision.item.label, decision.item.item_id, detail))
            with progress_guard:
                progress[0] += 1
                done = progress[0]
            log("[预热] 进度 %d/%d（剩 %d）" % (done, len(queue), len(queue) - done))

        if jobs == 1 or len(queue) <= 1:
            for decision in queue:
                handle(decision)
        else:
            cursor = [0]
            cursor_guard = threading.Lock()

            def worker() -> None:
                while True:
                    with cursor_guard:
                        if cursor[0] >= len(queue):
                            return
                        decision = queue[cursor[0]]
                        cursor[0] += 1
                    handle(decision)

            threads = [threading.Thread(target=worker, daemon=True,
                                        name="dvpack-prewarm-%d" % index)
                       for index in range(jobs)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
    finally:
        report.elapsed = time.time() - started
    total = (len(report.packed) + len(report.hits)
             + len(report.skipped) + len(report.failed))
    log("[预热] 汇总：打包 %d / 命中 %d / 跳过 %d / 失败 %d（共 %d，用时 %.0fs）"
        % (len(report.packed), len(report.hits), len(report.skipped),
           len(report.failed), total, report.elapsed))
    return report


def add_arguments(parser: argparse.ArgumentParser) -> None:
    """prewarm 参数定义。python -m dvpack.prewarm 直跑与 dvpack prewarm 共用。"""
    parser.add_argument("root", type=Path, nargs="?", default=Path("_out/dv_p7_fix"),
                        help="serve 现役缓存根（含 _jf/ 的那层），默认 _out/dv_p7_fix")
    parser.add_argument("--base", default=os.environ.get("DVPACK_JF_BASE", ""),
                        help="Jellyfin 地址，如 http://192.168.1.68:8096（或 DVPACK_JF_BASE）")
    parser.add_argument("--token", default=os.environ.get("DVPACK_JF_TOKEN", ""),
                        help="现成 api_key（或 DVPACK_JF_TOKEN）；不给则用 --pw 登录")
    parser.add_argument("--user", default="root", help="登录用户名（配合 --pw）")
    parser.add_argument("--pw", default=os.environ.get("DVPACK_JF_PASSWORD", ""),
                        help="登录密码（或 DVPACK_JF_PASSWORD）")
    parser.add_argument("--include-types", default="Movie,Episode,MusicVideo,Video",
                        help="Jellyfin IncludeItemTypes；有剧集就带上 Episode")
    parser.add_argument("--limit", type=int, default=0,
                        help="本轮最多打包几部；0=不限（命中的不占额度）")
    parser.add_argument("--older-than", type=float, default=0.0,
                        help="把 N 天前的「新鲜」全片包也判为超龄重打；0=只补缺失/半片")
    parser.add_argument("--jobs", type=int, default=1,
                        help="并发打包路数，默认 1，硬顶 %d（Jellyfin 抽流事故教训）" % MAX_JOBS)
    parser.add_argument("--dry-run", action="store_true",
                        help="只列计划（命中/打包/跳过与原因），不动手打包")


def _resolve_token(args) -> str:
    token = (args.token or "").strip()
    if token:
        return token
    if not args.pw:
        raise JfMetaError(
            "缺凭据：--token / DVPACK_JF_TOKEN，或 --pw / DVPACK_JF_PASSWORD")
    token = jfmeta.login(args.base, args.user, args.pw)
    print("[预热] 登录成功，token 前 8 位 %s…" % token[:8])
    return token


def run_from_args(args) -> int:
    """argparse 命名空间 → 枚举 → 计划 →（dry-run 或）打包。"""
    base = (args.base or "").strip().rstrip("/")
    if not base:
        print("失败：缺 Jellyfin 地址（--base 或 DVPACK_JF_BASE）", file=sys.stderr)
        return 2
    try:
        token = _resolve_token(args)
    except JfMetaError as exc:
        print("失败：%s" % exc, file=sys.stderr)
        return 2
    try:
        items = enumerate_items(base, token, args.include_types)
    except JfMetaError as exc:
        print("失败：%s" % exc, file=sys.stderr)
        return 2
    root = Path(args.root)
    print("[预热] 片库 %d 条，缓存根 %s" % (len(items), root))
    decisions = plan_prewarm(root, base, token, items,
                             older_than_days=args.older_than, limit=args.limit)
    for decision in decisions:
        verb = {ACTION_PACK: "打包", ACTION_HIT: "命中",
                ACTION_SKIP: "跳过"}[decision.action]
        cell = _disp_pad(_disp_clip(decision.item.label, 46), 46)
        print("  [%s] %s %s" % (verb, cell, decision.reason))
    todo = [d for d in decisions if d.action == ACTION_PACK]
    if args.dry_run:
        print("[预热] dry-run：命中 %d / 待打包 %d / 跳过 %d，未动手"
              % (sum(1 for d in decisions if d.action == ACTION_HIT),
                 len(todo),
                 sum(1 for d in decisions if d.action == ACTION_SKIP)))
        return 0
    if not todo:
        print("[预热] 没有待打包的条目。")
        return 0
    try:
        report = run_prewarm(root, decisions, jobs=args.jobs)
    except KeyboardInterrupt:
        # worker 是 daemon 线程，主线程收到中断时它们可能还在打包——死 pid
        # 临时目录由 serve._sweep_stale_swaps 在下次打包时清理，不堆垃圾。
        print("[预热] 已中断；已完成的保留，死进程临时目录下次启动自动清扫",
              file=sys.stderr)
        return 130
    return 0 if report.ok else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="dvpack prewarm",
        description="Jellyfin 片库批量预热：把全片包提前打进 serve 现役缓存")
    add_arguments(parser)
    return run_from_args(parser.parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())

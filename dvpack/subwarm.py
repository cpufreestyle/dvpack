"""subwarm：Jellyfin 全库字幕批量预热——在线字幕提前抓进 L2 _subs 缓存。

为什么存在：serve 的字幕分级（见 subsource.py 模块头）是
  L1 Jellyfin 内嵌文本轨 → L2 _subs 缓存 → L3 在线（tools/subfetch.py）
在线腿原本只在两种时机触发：点播打包后 serve 后台 worker、prewarm 打包顺带。
于是「零字幕的纪录片」（2026-10-05 全库实测：70 部里 28 部）不点就永远没字幕；
点一部也只服务一部。用户 2026-10-04 明确要求「所有视频 调用中英双语字幕」。
Bazarr 的答案是定时「search all missing subtitles」——本模块是 dvpack 的同款。

与 serve 的契约（判据全套复用现役实现，一条不自己搓）：
- 片库枚举复用 prewarm.enumerate_items（jfmeta.list_items 两跳，带 Path/MediaStreams），
  残渣 Name 的茎名兜底也一并继承——喂给 subfetch 的检索片名带发行名反而更准
- src 形状复用 prewarm.build_src，与 dvplay packURL 逐字一致：指纹差一个字节，
  serve._subtitle_texts 就取不到预热落好的缓存
- L1/L2 判据复用 jfmeta.classify_subtitles + subtitle_languages、
  subsource.load_cached_texts / cache_blocked
- 在线抓取复用 subsource.fetch_online_texts（tools/subfetch.py：os → subhd → subcat）
- 抓到的字幕只做两件事：subsource.save_cached_texts 落 L2；该 item 已有现役包时，
  在 serve._pack_lock_for 同一把锁下调 subs.upgrade_master_subtitles 换轨
  （serve._online_subs_body 同款口径，不重打包视频）
- 负缓存照走：6h 内失败的同一指纹 cache_blocked 直接跳过，防日志爆炸

铁律不变：分钟级的公网抓取只允许出现在这种批处理里；/jf/pack 的 302 热路径
永远不碰在线腿。默认 1 路顺序抓——在线腿全是公网请求，并发只会换来 429 和
负缓存。
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path

from . import serve, subsource
from .prewarm import PreheatItem, build_src, enumerate_items

#: 决策/结局动作文案，CLI 输出与测试断言共用（与 prewarm ACTION_* 同风格）。
ACTION_L1 = "l1"        # 内嵌文本轨已中英齐：白拿，不抓
ACTION_HIT = "hit"      # L2 _subs 缓存已中英齐：白拿，不抓
ACTION_FETCH = "fetch"  # 缺语言，动在线腿
ACTION_SKIP = "skip"    # 抓不了（无后缀 / 负缓存期内 / 超额）
ACTION_FAILED = "failed"

#: 双语口径与 subs.BILINGUAL_TRACKS 对齐：中英两条轨。
DEFAULT_LANGS = ("zh", "en")


@dataclass
class SubwarmDecision:
    """一条 item 的字幕决策。cache_dir 只有能重建 src 的才填。"""

    item: PreheatItem
    action: str
    reason: str
    src: str = ""
    cache_dir: Path | None = None
    missing: tuple[str, ...] = ()   # 还缺哪几种语言（fetch 时用）


@dataclass
class SubwarmReport:
    """一批字幕预热的结局。failed 非空时 CLI 退 1——批处理工具要能当监控看。"""

    root: Path
    fetched: list[tuple[str, str, str]] = field(default_factory=list)   # (label, id, langs)
    l1: list[tuple[str, str]] = field(default_factory=list)
    hits: list[tuple[str, str]] = field(default_factory=list)
    skipped: list[tuple[str, str, str]] = field(default_factory=list)   # (label, id, reason)
    failed: list[tuple[str, str, str]] = field(default_factory=list)    # (label, id, error)
    elapsed: float = 0.0

    @property
    def ok(self) -> bool:
        return not self.failed


def plan_subwarm(root, items: list[PreheatItem], *, base_url: str, token: str,
                 langs=DEFAULT_LANGS, limit: int = 0, force: bool = False,
                 ) -> list[SubwarmDecision]:
    """把片库分成几堆：l1（内嵌齐白拿）/ hit（缓存齐白拿）/ fetch（要抓）/ skip。

    limit 只数 fetch：白拿的照常汇报但不占额度，超额的记 skip 原因。
    force 无视内嵌齐与负缓存，全部重抓（刷新缓存用）。
    """
    want = tuple(langs)
    decisions: list[SubwarmDecision] = []
    queued = 0
    for item in items:
        if not item.ext:
            decisions.append(SubwarmDecision(
                item=item, action=ACTION_SKIP,
                reason="Path 无容器后缀，src 无法重建（指纹会对不上）"))
            continue
        src = build_src(base_url, item.item_id, item.ext, token)
        cache_dir = subsource.subtitle_cache_dir(root, serve._src_fingerprint(src))
        if not force and item.kind in ("text", "mixed") and set(want) <= item.langs:
            decisions.append(SubwarmDecision(
                item=item, action=ACTION_L1,
                reason="内嵌文本轨 %s" % "+".join(sorted(set(want) & item.langs)),
                src=src, cache_dir=cache_dir))
            continue
        cached, _meta = subsource.load_cached_texts(cache_dir)
        missing = tuple(lang for lang in want if lang not in cached)
        if not missing and not force:
            decisions.append(SubwarmDecision(
                item=item, action=ACTION_HIT,
                reason="L2 缓存 %s" % "+".join(sorted(set(want) & set(cached))),
                src=src, cache_dir=cache_dir))
            continue
        if not force and subsource.cache_blocked(cache_dir):
            decisions.append(SubwarmDecision(
                item=item, action=ACTION_SKIP,
                reason="负缓存期内，跳过（--force 重试）",
                src=src, cache_dir=cache_dir))
            continue
        if limit and queued >= limit:
            decisions.append(SubwarmDecision(
                item=item, action=ACTION_SKIP, reason="超过 --limit %d" % limit,
                src=src, cache_dir=cache_dir))
            continue
        queued += 1
        have = "+".join(sorted(item.langs)) if item.langs else "无"
        decisions.append(SubwarmDecision(
            item=item, action=ACTION_FETCH,
            reason="缺 %s（内嵌 %s）" % ("+".join(missing) or "+".join(want), have),
            src=src, cache_dir=cache_dir, missing=missing))
    return decisions


def _fetch_one(root: Path, decision: SubwarmDecision, log,
               timeout: float | None = None) -> tuple[str, str, str]:
    """抓一部。回 (status, 抓到的语言串, 说明)；绝不对调用方抛异常。"""
    item, src, cache_dir = decision.item, decision.src, decision.cache_dir
    try:
        # cat.vtt 落在临时目录，save_cached_texts 才写正式的 <lang>.vtt；
        # 缓存目录里不留 subfetch 中间产物（与 serve 后台 worker 口径一致）。
        with tempfile.TemporaryDirectory(prefix="dvpack-subwarm-") as tmp:
            texts, note = subsource.fetch_online_texts(
                src=src, item_id=item.item_id, name=item.name,
                out_dir=Path(tmp), timeout=timeout)
        if not texts:
            subsource.record_online_failure(cache_dir, note)
            return ACTION_FAILED, "", note
        got = "+".join(lang for lang in subsource.LANGS if lang in texts)
        subsource.save_cached_texts(cache_dir, texts, subsource.ONLINE)
        subsource.clear_online_failure(cache_dir)
        # 已有现役包的顺手换 master 字幕轨，等下次续期/重打包白拿几天屏幕时间。
        out_dir = Path(root) / "_jf" / item.item_id
        if (out_dir / "master.m3u8").exists():
            try:
                from . import subs
                with serve._pack_lock_for(item.item_id):
                    subs.upgrade_master_subtitles(out_dir, texts)
            except Exception:
                traceback.print_exc()
                return ACTION_FETCH, got, "已入缓存，master 升级失败（%s）" % note
        return ACTION_FETCH, got, note
    except Exception as exc:  # 批量不能被一部片打断；真实原因进 traceback
        traceback.print_exc()
        return ACTION_FAILED, "", str(exc)


def run_subwarm(root: Path, decisions: list[SubwarmDecision], *, log=print,
                timeout: float | None = None) -> SubwarmReport:
    """按计划抓字幕。默认 1 路顺序（并发只会换来公站 429）。"""
    report = SubwarmReport(root=Path(root))
    started = time.time()
    try:
        for decision in decisions:
            if decision.action == ACTION_L1:
                report.l1.append((decision.item.label, decision.item.item_id))
            elif decision.action == ACTION_HIT:
                report.hits.append((decision.item.label, decision.item.item_id))
            elif decision.action == ACTION_SKIP:
                report.skipped.append(
                    (decision.item.label, decision.item.item_id, decision.reason))
        queue = [d for d in decisions if d.action == ACTION_FETCH]
        done = 0
        for decision in queue:
            status, got, note = _fetch_one(report.root, decision, log, timeout)
            if status == ACTION_FETCH:
                report.fetched.append(
                    (decision.item.label, decision.item.item_id, got))
                # 第三位只放语言串：run_from_args 用 set(got.split("+")) 判
                # 「新齐语言」数，note（可能含 master 升级失败）不该混进来。
                log("[字幕预热] 抓到 %s %s（%s）"
                    % (decision.item.label, got or "?", note))
            else:
                report.failed.append(
                    (decision.item.label, decision.item.item_id, note))
                log("[字幕预热] 失败 %s：%s" % (decision.item.label, note))
            done += 1
            log("[字幕预热] 进度 %d/%d（剩 %d）" % (done, len(queue), len(queue) - done))
    finally:
        report.elapsed = time.time() - started
    log("[字幕预热] 汇总：内嵌齐 %d / 缓存齐 %d / 抓到 %d / 跳过 %d / 失败 %d"
        "（共 %d，用时 %.0fs）"
        % (len(report.l1), len(report.hits), len(report.fetched),
           len(report.skipped), len(report.failed), len(decisions),
           report.elapsed))
    return report


def add_arguments(parser: argparse.ArgumentParser) -> None:
    """subwarm 参数定义。python -m dvpack.subwarm 直跑与 dvpack subwarm 共用。"""
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
    parser.add_argument("--langs", default=",".join(DEFAULT_LANGS),
                        help="要补齐的语言，默认 zh,en（双语口径与 subs.BILINGUAL_TRACKS 对齐）")
    parser.add_argument("--limit", type=int, default=0,
                        help="本轮最多抓几部；0=不限（白拿的不占额度）")
    parser.add_argument("--force", action="store_true",
                        help="无视内嵌齐与负缓存，重新抓（刷新缓存用）")
    parser.add_argument("--timeout", type=float, default=None,
                        help="单部在线抓取墙上限秒；默认 subsource.online_timeout()")
    parser.add_argument("--dry-run", action="store_true",
                        help="只列计划（内嵌齐/缓存齐/待抓/跳过与原因），不动手抓")


def run_from_args(args) -> int:
    """argparse 命名空间 → 枚举 → 计划 →（dry-run 或）抓。"""
    base = (args.base or "").strip().rstrip("/")
    if not base:
        print("失败：缺 Jellyfin 地址（--base 或 DVPACK_JF_BASE）", file=sys.stderr)
        return 2
    from .jfmeta import JfMetaError, login

    langs = tuple(part.strip() for part in (args.langs or "").split(",") if part.strip())
    token = (args.token or "").strip()
    if not token:
        if not args.pw:
            print("失败：缺凭据（--token / DVPACK_JF_TOKEN，或 --pw / DVPACK_JF_PASSWORD）",
                  file=sys.stderr)
            return 2
        try:
            token = login(base, args.user, args.pw)
        except JfMetaError as exc:
            print("失败：%s" % exc, file=sys.stderr)
            return 2
        print("[字幕预热] 登录成功，token 前 8 位 %s…" % token[:8])
    try:
        items = enumerate_items(base, token, args.include_types)
    except JfMetaError as exc:
        print("失败：%s" % exc, file=sys.stderr)
        return 2
    root = Path(args.root)
    print("[字幕预热] 片库 %d 条，缓存根 %s，目标语言 %s"
          % (len(items), root, "+".join(langs) or "(空)"))
    decisions = plan_subwarm(root, items, base_url=base, token=token,
                             langs=langs or DEFAULT_LANGS,
                             limit=args.limit, force=args.force)
    verbs = {ACTION_L1: "内嵌", ACTION_HIT: "缓存", ACTION_FETCH: "抓取",
             ACTION_SKIP: "跳过"}
    for decision in decisions:
        print("  [%s] %s %s" % (verbs.get(decision.action, "?"),
                                decision.item.label, decision.reason))
    todo = [d for d in decisions if d.action == ACTION_FETCH]
    if args.dry_run:
        print("[字幕预热] dry-run：内嵌齐 %d / 缓存齐 %d / 待抓 %d / 跳过 %d，未动手"
              % (sum(1 for d in decisions if d.action == ACTION_L1),
                 sum(1 for d in decisions if d.action == ACTION_HIT),
                 len(todo),
                 sum(1 for d in decisions if d.action == ACTION_SKIP)))
        return 0
    if not todo:
        print("[字幕预热] 没有待抓的条目。")
        return 0
    try:
        report = run_subwarm(root, decisions, timeout=args.timeout)
    except KeyboardInterrupt:
        print("[字幕预热] 已中断；已抓到的保留，失败的留负缓存下次自动重试",
              file=sys.stderr)
        return 130
    wanted = set(langs) or set(DEFAULT_LANGS)
    fresh = sum(1 for _label, _id, got in report.fetched
                if wanted <= set(got.split("+")))
    print("[字幕预热] 本轮新齐 %s 的 %d 部；全库到齐以 dry-run 复核为准"
          % ("+".join(sorted(wanted)), fresh))
    return 0 if report.ok else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="dvpack subwarm",
        description="Jellyfin 全库字幕批量预热：在线字幕提前抓进 L2 _subs 缓存")
    add_arguments(parser)
    return run_from_args(parser.parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())

"""dvpack.prewarm：Jellyfin 片库批量预热的计划/打包决策（全部 mock）。

契约测试的重点是「指纹必须对上」：build_src 的形状错一个字节，serve 就判
缓存未命中、整片重打，预热白做——所以 src 形状与 _src_fingerprint 的换算
在这里焊死。serve 的缓存判据（_cache_fresh/_pack_poisoned）用真函数跑真
临时文件，不 mock。
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from dvpack import jfmeta, prewarm, serve, subsource
from dvpack.jfpack import JfPackError
from dvpack.prewarm import (
    ACTION_HIT,
    ACTION_PACK,
    ACTION_SKIP,
    PreheatDecision,
    PreheatItem,
    build_src,
    enumerate_items,
    main,
    plan_prewarm,
    run_prewarm,
)

BASE = "http://192.168.1.68:8096"
TOKEN = "tok-abc"


def _item(item_id="i1", name="测试片", ext="mkv", **kw):
    return PreheatItem(
        item_id=item_id, name=name, year=2025,
        path="/media/%s.%s" % (name, ext), ext=ext,
        kind=kw.pop("kind", "text"), langs=kw.pop("langs", {"zh", "en"}), **kw)


_PACK_CALLS = {"n": 0}


def _flaky_pack(*a):
    """计数 fake：第一次抛 JfPackError，之后静默成功——验批不断。"""
    _PACK_CALLS["n"] += 1
    if _PACK_CALLS["n"] == 1:
        raise JfPackError("拒绝打包：测试")


def _write_pack(out_dir: Path, *, seconds: float, plen: float | None,
                extinf_total: float, master: bool = True) -> None:
    """按现役 marker/playlist 形状造一份盘上产物。"""
    out_dir.mkdir(parents=True, exist_ok=True)
    src = build_src(BASE, out_dir.name, "mkv", TOKEN)
    meta: dict = {"src": serve._src_fingerprint(src), "seconds": seconds}
    if plen is not None:
        meta["plen"] = plen
    (out_dir / "source.json").write_text(json.dumps(meta), encoding="utf-8")
    if master:
        (out_dir / "master.m3u8").write_text("#EXTM3U\n", encoding="utf-8")
    seg = extinf_total / 10.0
    lines = ["#EXTM3U", "#EXT-X-TARGETDURATION:6"]
    for n in range(10):
        lines += ["#EXTINF:%.1f," % seg, "%04d.m4s" % n]
    lines.append("#EXT-X-ENDLIST")
    (out_dir / "video.m3u8").write_text("\n".join(lines) + "\n", encoding="utf-8")


class TestBuildSrc:
    def test_shape_matches_serve_fingerprint(self):
        """指纹必须与 dvplay 真流量一致：剥 api_key 后只剩 static=true。"""
        src = build_src(BASE, "abc", "mkv", TOKEN)
        assert src == ("http://192.168.1.68:8096/Videos/abc/stream.mkv"
                       "?static=true&api_key=tok-abc")
        assert serve._src_fingerprint(src) == (
            "http://192.168.1.68:8096/Videos/abc/stream.mkv?static=true")

    def test_ext_leading_dot_and_upper_tolerated(self):
        src = build_src(BASE, "abc", ".MP4", TOKEN)
        assert "/stream.mp4?static=true" in src


class TestEnumerateItems:
    def test_maps_fields_and_skips_empty_id(self, monkeypatch):
        raw = [
            {"Id": "a1", "Name": "片A", "ProductionYear": 2024,
             "Path": "/media/a.mkv",
             "MediaStreams": [
                 {"Type": "Subtitle", "Codec": "subrip", "Language": "zho"},
                 {"Type": "Subtitle", "Codec": "subrip", "Language": "eng"},
             ]},
            {"Id": "", "Name": "幽灵条目", "Path": "/x/y.mp4",
             "MediaStreams": []},
        ]
        monkeypatch.setattr(prewarm, "list_items", lambda *a, **k: raw)
        items = enumerate_items(BASE, TOKEN)
        assert [i.item_id for i in items] == ["a1"]
        first = items[0]
        assert first.ext == "mkv" and first.kind == "text"
        assert first.langs == {"zh", "en"}
        assert first.label == "《片A》 (2024)"

    def test_pgs_only_kind(self, monkeypatch):
        raw = [{"Id": "b2", "Name": "图像字幕片", "Path": "/media/b.mkv",
                "MediaStreams": [
                    {"Type": "Subtitle", "Codec": "hdmv_pgs",
                     "Language": "zho"}]}]
        monkeypatch.setattr(prewarm, "list_items", lambda *a, **k: raw)
        items = enumerate_items(BASE, TOKEN)
        assert items[0].kind == "pgs" and items[0].langs == set()


class TestPlanPrewarm:
    def test_missing_cache_means_pack(self, tmp_path):
        decisions = plan_prewarm(tmp_path, BASE, TOKEN, [_item()])
        assert len(decisions) == 1 and decisions[0].action == ACTION_PACK
        assert "无现役缓存" in decisions[0].reason
        assert decisions[0].out_dir == tmp_path / "_jf" / "i1"

    def test_fresh_full_pack_is_hit(self, tmp_path):
        out = tmp_path / "_jf" / "i1"
        _write_pack(out, seconds=0.0, plen=2000.0, extinf_total=1500.0)
        decisions = plan_prewarm(tmp_path, BASE, TOKEN, [_item()])
        assert decisions[0].action == ACTION_HIT

    def test_stub_upgrades_to_full(self, tmp_path):
        """秒开 20s stub 也「可播」，但必须升级全片——命中判据只看全片包。"""
        out = tmp_path / "_jf" / "i1"
        _write_pack(out, seconds=20.0, plen=None, extinf_total=20.0)
        decisions = plan_prewarm(tmp_path, BASE, TOKEN, [_item()])
        assert decisions[0].action == ACTION_PACK
        assert "预览段" in decisions[0].reason

    def test_poisoned_pack_repacked(self, tmp_path):
        """自称全片（seconds=0）实则 30s：毒包判据必须导向重打。"""
        out = tmp_path / "_jf" / "i1"
        _write_pack(out, seconds=0.0, plen=1200.0, extinf_total=30.0)
        decisions = plan_prewarm(tmp_path, BASE, TOKEN, [_item()])
        assert decisions[0].action == ACTION_PACK
        assert "毒包" in decisions[0].reason

    def test_missing_master_rebuild_reason(self, tmp_path):
        out = tmp_path / "_jf" / "i1"
        _write_pack(out, seconds=0.0, plen=None, extinf_total=1500.0,
                    master=False)
        decisions = plan_prewarm(tmp_path, BASE, TOKEN, [_item()])
        assert decisions[0].action == ACTION_PACK
        assert "缺 master" in decisions[0].reason

    def test_no_ext_skipped(self, tmp_path):
        decisions = plan_prewarm(tmp_path, BASE, TOKEN, [_item(ext="")])
        assert decisions[0].action == ACTION_SKIP
        assert decisions[0].src == "" and decisions[0].out_dir is None

    def test_limit_counts_only_packs(self, tmp_path):
        items = [_item(item_id="i%d" % n, name="片%d" % n) for n in range(3)]
        for n in (1, 2):
            out = tmp_path / "_jf" / ("i%d" % n)
            _write_pack(out, seconds=0.0, plen=2000.0, extinf_total=1500.0)
        decisions = plan_prewarm(tmp_path, BASE, TOKEN, items, limit=1)
        packs = [d for d in decisions if d.action == ACTION_PACK]
        hits = [d for d in decisions if d.action == ACTION_HIT]
        skips = [d for d in decisions if d.action == ACTION_SKIP]
        assert len(packs) == 1 and len(hits) == 2
        assert len(skips) == 0  # 命中不占额度，未被 limit 挤出
        decisions = plan_prewarm(tmp_path, BASE, TOKEN, items, limit=0)
        assert sum(d.action == ACTION_PACK for d in decisions) == 1

    def test_limit_overflows_rest_to_skip(self, tmp_path):
        items = [_item(item_id="i%d" % n, name="片%d" % n) for n in range(3)]
        decisions = plan_prewarm(tmp_path, BASE, TOKEN, items, limit=1)
        packs = [d for d in decisions if d.action == ACTION_PACK]
        skips = [d for d in decisions if d.action == ACTION_SKIP]
        assert len(packs) == 1 and len(skips) == 2
        assert "超过 --limit" in skips[0].reason

    def test_older_than_repacks_stale_hits(self, tmp_path):
        out = tmp_path / "_jf" / "i1"
        _write_pack(out, seconds=0.0, plen=2000.0, extinf_total=1500.0)
        old = time.time() - 8 * 86400
        for name in ("master.m3u8", "video.m3u8"):
            os.utime(out / name, (old, old))
        now = time.time()
        assert plan_prewarm(tmp_path, BASE, TOKEN, [_item()])[0].action == ACTION_HIT
        decisions = plan_prewarm(tmp_path, BASE, TOKEN, [_item()],
                                 older_than_days=7.0, now=now)
        assert decisions[0].action == ACTION_PACK
        assert "超龄" in decisions[0].reason


class TestRunPrewarm:
    def _decision(self, tmp_path, item=None):
        item = item or _item()
        return PreheatDecision(
            item=item, action=ACTION_PACK, reason="t",
            src=build_src(BASE, item.item_id, item.ext, TOKEN),
            out_dir=tmp_path / "_jf" / item.item_id)

    def _mock_serve(self, monkeypatch, texts=None):
        calls: dict = {}
        monkeypatch.setattr(serve, "_free_bytes", lambda p: 1024 ** 4)
        monkeypatch.setattr(serve, "_dir_size_bytes", lambda p: 21 * 1024 ** 3)
        monkeypatch.setattr(
            serve, "_subtitle_texts",
            lambda src, **k: SimpleNamespace(
                texts=texts if texts is not None else {"zh": "a", "en": "b"},
                level="jellyfin", note="2 轨"))
        monkeypatch.setattr(
            serve, "_online_subs_body",
            lambda *a: "轨 zh+en（mock）")
        monkeypatch.setattr(
            serve, "_pack_full_swap",
            lambda src, out, maps, texts, iid: calls.update(
                src=src, out=out, maps=maps, texts=texts, iid=iid))
        return calls

    def test_packs_full_through_serve_swap(self, tmp_path, monkeypatch):
        calls = self._mock_serve(monkeypatch)
        report = run_prewarm(tmp_path, [self._decision(tmp_path)])
        assert len(report.packed) == 1 and report.ok
        assert calls["iid"] == "i1"
        assert calls["out"] == tmp_path / "_jf" / "i1"
        assert calls["texts"] == {"zh": "a", "en": "b"}
        # 全片口径：maps 走音画齐整的默认档
        assert calls["maps"] == prewarm.VIDEO_AUDIO_MAPS

    def test_online_leg_only_when_no_texts(self, tmp_path, monkeypatch):
        self._mock_serve(monkeypatch, texts={})
        monkeypatch.setattr(subsource, "online_enabled", lambda: True)
        seen: list = []
        monkeypatch.setattr(serve, "_online_subs_body",
                            lambda *a: seen.append(a) or "轨 zh+en")
        report = run_prewarm(tmp_path, [self._decision(tmp_path)])
        assert report.ok and len(seen) == 1

    def test_online_leg_off_by_default_when_disabled(self, tmp_path, monkeypatch):
        self._mock_serve(monkeypatch, texts={})
        monkeypatch.setattr(subsource, "online_enabled", lambda: False)
        seen: list = []
        monkeypatch.setattr(serve, "_online_subs_body",
                            lambda *a: seen.append(a))
        run_prewarm(tmp_path, [self._decision(tmp_path)])
        assert seen == []

    def test_failure_isolated_from_batch(self, tmp_path, monkeypatch):
        """第一部抛错不能掀翻整批：后面的条目照常打包，failed 只记 1 条。"""
        self._mock_serve(monkeypatch)
        monkeypatch.setattr(serve, "_pack_full_swap", _flaky_pack)
        decisions = [self._decision(tmp_path, item=_item(item_id="i1")),
                     self._decision(tmp_path, item=_item(item_id="i2",
                                                          name="第二部")),
                     self._decision(tmp_path, item=_item(item_id="i3",
                                                          name="第三部"))]
        report = run_prewarm(tmp_path, decisions)
        assert len(report.failed) == 1 and not report.ok
        assert len(report.packed) == 2
        assert "拒绝打包" in report.failed[0][2]

    def test_disk_guard_skips(self, tmp_path, monkeypatch):
        self._mock_serve(monkeypatch)
        monkeypatch.setattr(serve, "_free_bytes", lambda p: 1.0)
        report = run_prewarm(tmp_path, [self._decision(tmp_path)])
        assert len(report.skipped) == 1 and len(report.packed) == 0

    def test_hit_recheck_inside_lock(self, tmp_path, monkeypatch):
        """排队期间 serve 可能已把全片补完：持锁复查必须转命中，不重打。"""
        self._mock_serve(monkeypatch)
        out = tmp_path / "_jf" / "i1"
        _write_pack(out, seconds=0.0, plen=2000.0, extinf_total=1500.0)
        monkeypatch.setattr(serve, "_pack_full_swap",
                            lambda *a: pytest.fail("不该重打"))
        report = run_prewarm(tmp_path, [self._decision(tmp_path)])
        assert len(report.hits) == 1 and len(report.packed) == 0

    def test_jobs_two_runs_all(self, tmp_path, monkeypatch):
        self._mock_serve(monkeypatch)
        decisions = [self._decision(tmp_path, item=_item(item_id="i%d" % n,
                                                          name="片%d" % n))
                     for n in range(4)]
        report = run_prewarm(tmp_path, decisions, jobs=2)
        assert len(report.packed) == 4

    def test_hits_and_skips_prefilled(self, tmp_path, monkeypatch):
        self._mock_serve(monkeypatch)
        hit = PreheatDecision(item=_item(item_id="h1", name="老片"),
                              action=ACTION_HIT, reason="全片包已在")
        skip = PreheatDecision(item=_item(item_id="s1", name="无后缀"),
                               action=ACTION_SKIP, reason="无后缀")
        report = run_prewarm(tmp_path, [hit, skip])
        assert len(report.hits) == 1 and len(report.skipped) == 1


class TestCli:
    def test_dry_run_lists_plan(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setattr(prewarm, "enumerate_items",
                            lambda *a, **k: [_item(), _item(item_id="i2", name="片B")])
        rc = main(["--dry-run", "--base", BASE, "--token", TOKEN, str(tmp_path)])
        assert rc == 0
        out = capsys.readouterr().out
        assert "dry-run" in out and "待打包 2" in out
        assert not (tmp_path / "_jf").exists()  # 一个字节都没写

    def test_missing_base_is_2(self, monkeypatch):
        monkeypatch.delenv("DVPACK_JF_BASE", raising=False)
        assert main(["--token", TOKEN]) == 2

    def test_missing_credentials_is_2(self, monkeypatch):
        monkeypatch.delenv("DVPACK_JF_TOKEN", raising=False)
        monkeypatch.delenv("DVPACK_JF_PASSWORD", raising=False)
        assert main(["--base", BASE]) == 2

    def test_login_path_used(self, tmp_path, monkeypatch):
        seen: list = []
        monkeypatch.setattr(prewarm.jfmeta, "login",
                            lambda base, user, pw: seen.append((base, user, pw))
                            or "login-token")
        monkeypatch.setattr(prewarm, "enumerate_items", lambda *a, **k: [])
        rc = main(["--base", BASE, "--pw", "secret", str(tmp_path), "--dry-run"])
        assert rc == 0 and seen == [(BASE, "root", "secret")]

    def test_enumeration_failure_is_2(self, tmp_path, monkeypatch):
        def boom(*a, **k):
            raise jfmeta.JfMetaError("片库枚举失败：连不上")
        monkeypatch.setattr(prewarm, "enumerate_items", boom)
        assert main(["--base", BASE, "--token", TOKEN, str(tmp_path)]) == 2

    def test_main_entry_wires_subcommand(self, tmp_path, monkeypatch):
        """dvpack prewarm 子命令必须通到 run_from_args（不是 2/usage error）。"""
        from dvpack.__main__ import main as cli_main
        monkeypatch.setattr(prewarm, "enumerate_items",
                            lambda *a, **k: [_item()])
        rc = cli_main(["prewarm", "--dry-run", "--base", BASE,
                       "--token", TOKEN, str(tmp_path)])
        assert rc == 0


class TestDisplayName:
    """刮削残渣 Name 回退文件名茎：打印出来也得认得出是哪部。"""

    def test_scrape_artifact_falls_back_to_stem(self):
        stem = "[风味人间 第三季].Once.Upon.a.Bite.2021.S03E03"
        assert prewarm._display_name(
            ".Once.Upon.a.Bite", "/media/" + stem + ".mp4") == stem

    def test_normal_name_untouched(self):
        assert prewarm._display_name("测试片", "/media/测试片.mkv") == "测试片"

    def test_stem_named_item_untouched(self):
        # 本就无元数据：Name 就是整条文件名，保持原样
        assert prewarm._display_name(
            "Chefs.Table.Legends.S01E01.DV",
            "/media/Chefs.Table.Legends.S01E01.DV.mkv"
        ) == "Chefs.Table.Legends.S01E01.DV"

    def test_empty_and_missing_path(self):
        assert prewarm._display_name("", "/media/a b.mkv") == "a b"
        assert prewarm._display_name("", "") == "(未命名)"


class TestDispWidth:
    def test_cjk_counts_wide(self):
        assert prewarm._disp_width("《测试》") == 8  # 《》也是宽字符
        assert prewarm._disp_width("abc") == 3

    def test_clip_keeps_width_and_ellipsis(self):
        clipped = prewarm._disp_clip("风味人间第三季超长标题文字", 9)
        assert clipped.endswith("…")
        assert prewarm._disp_width(clipped) <= 9
        assert prewarm._disp_clip("短", 9) == "短"

    def test_pad_aligns_cjk(self):
        assert prewarm._disp_pad("中", 4) == "中  "
        assert prewarm._disp_pad("ab", 4) == "ab  "
        assert prewarm._disp_pad("abcd", 2) == "abcd"


class TestPackOneKeepsServeSlices:
    def test_part_dirs_left_to_serve(self, tmp_path, monkeypatch):
        """预热不清 _part*：serve 并行分包在自己 pack 目录内的分段目录是它的
        职权，prewarm 误删会打断正在播的 stub 补全（跨进程 race 教训）。"""
        root = tmp_path / "_out"
        out = tmp_path / "_jf" / "i1"
        out.mkdir(parents=True)
        inflight = out / "_part00.m4s.d"
        inflight.mkdir()
        (inflight / "x.m4s").write_text("x", encoding="utf-8")

        decision = plan_prewarm(tmp_path, BASE, TOKEN, [_item()])[0]
        assert decision.action == ACTION_PACK

        monkeypatch.setattr(serve, "_free_bytes", lambda root: 1 << 40)
        monkeypatch.setattr(
            serve, "_subtitle_texts",
            lambda src, root=None: SimpleNamespace(texts=["t"], level=None))
        calls: list = []
        monkeypatch.setattr(serve, "_pack_full_swap",
                            lambda *a: calls.append(a))
        monkeypatch.setattr(serve, "_dir_size_bytes", lambda d: 1024)

        status, _note, _gb = prewarm._pack_one(root, decision,
                                               lambda *a: None)
        assert status == ACTION_PACK and len(calls) == 1
        assert inflight.is_dir(), "预热不得清理 serve 在飞的 _part* 分段目录"


class TestRunFromArgsInterrupt:
    def test_ctrl_c_returns_130(self, tmp_path, capsys, monkeypatch):
        """Ctrl-C：rc=130 体面退，已完成的结果不丢、不甩 traceback。"""
        import argparse

        parser = argparse.ArgumentParser()
        prewarm.add_arguments(parser)
        args = parser.parse_args(
            [str(tmp_path / "_out"), "--base", BASE, "--token", TOKEN,
             "--limit", "1"])
        monkeypatch.setattr(prewarm, "enumerate_items",
                            lambda base, token, types: [_item()])

        def interrupted(root, decisions, **kw):
            raise KeyboardInterrupt

        monkeypatch.setattr(prewarm, "run_prewarm", interrupted)
        assert prewarm.run_from_args(args) == 130
        assert "已中断" in capsys.readouterr().err


class TestProgressLine:
    def test_progress_line_counts_each_pack(self, tmp_path, capsys,
                                            monkeypatch):
        """过夜长跑盲等最致命：每包完一行进度，收尾一行汇总。"""
        root = tmp_path / "_out"
        decisions = plan_prewarm(tmp_path, BASE, TOKEN,
                                 [_item("i1"), _item("i2")])
        monkeypatch.setattr(serve, "_free_bytes", lambda r: 1 << 40)
        monkeypatch.setattr(
            serve, "_subtitle_texts",
            lambda src, root=None: SimpleNamespace(texts=["t"], level=None))
        monkeypatch.setattr(serve, "_pack_full_swap", lambda *a: None)
        monkeypatch.setattr(serve, "_dir_size_bytes", lambda d: 1024)

        report = prewarm.run_prewarm(root, decisions)
        assert len(report.packed) == 2 and report.ok
        out = capsys.readouterr().out
        assert "进度 1/2" in out and "进度 2/2" in out
        assert "汇总：打包 2" in out

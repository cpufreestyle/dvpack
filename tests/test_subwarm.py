"""dvpack.subwarm：全库字幕批量预热的计划与执行（全部 mock）。

契约测试三件事：
1. 指纹链路：build_src 的 src 喂给 subtitle_cache_dir / serve._src_fingerprint，
   形状差一个字节缓存就命中不了、在线腿重复白抓——这里焊死。
2. plan_subwarm 决策表：l1 / hit / fetch / skip（无 ext、负缓存、超额）与
   limit 只数 fetch、force 无视一切的口径。
3. run_subwarm 执行面：fetch 失败进 failed 而不断批；master 换轨失败
   不丢缓存；report.ok 决定 CLI 退出码。
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

from dvpack import serve, subsource, subwarm
from dvpack.prewarm import PreheatItem, build_src

BASE = "http://192.168.1.68:8096"
TOKEN = "tok-abc"


def _item(item_id="i1", name="测试片", ext="mkv", kind="none", langs=None):
    return PreheatItem(
        item_id=item_id, name=name, year=2025,
        path="/media/%s.%s" % (name, ext), ext=ext,
        kind=kind, langs=langs or set())


def _src(item_id="i1", ext="mkv"):
    return build_src(BASE, item_id, ext, TOKEN)


def _cache(root: Path, item_id="i1", ext="mkv") -> Path:
    return subsource.subtitle_cache_dir(root, serve._src_fingerprint(_src(item_id, ext)))


def _save(cache: Path, langs=("zh", "en"), reason=None):
    if reason is not None:
        subsource.record_online_failure(cache, reason)
        return
    texts = {lang: "WEBVTT\n\n00:00:01.000 --> 00:00:02.000\n%s 字幕" % lang
             for lang in langs}
    subsource.save_cached_texts(cache, texts, subsource.ONLINE)


class TestPlan:
    def test_no_ext_is_skip(self, tmp_path):
        decisions = subwarm.plan_subwarm(tmp_path, [_item(ext="")],
                                        base_url=BASE, token=TOKEN)
        assert decisions[0].action == subwarm.ACTION_SKIP
        assert "后缀" in decisions[0].reason

    def test_l1_when_embedded_bilingual(self, tmp_path):
        decisions = subwarm.plan_subwarm(tmp_path, [_item(kind="text", langs={"zh", "en"})],
                                        base_url=BASE, token=TOKEN)
        assert decisions[0].action == subwarm.ACTION_L1
        assert "内嵌" in decisions[0].reason

    def test_pgs_only_means_fetch(self, tmp_path):
        decisions = subwarm.plan_subwarm(tmp_path, [_item(kind="pgs")],
                                        base_url=BASE, token=TOKEN)
        assert decisions[0].action == subwarm.ACTION_FETCH
        assert decisions[0].src == _src()
        assert decisions[0].cache_dir == _cache(tmp_path)

    def test_hit_when_cache_bilingual(self, tmp_path):
        _save(_cache(tmp_path))
        decisions = subwarm.plan_subwarm(tmp_path, [_item()],
                                        base_url=BASE, token=TOKEN)
        assert decisions[0].action == subwarm.ACTION_HIT
        assert "缓存" in decisions[0].reason

    def test_negative_cache_is_skip_until_force(self, tmp_path):
        _save(_cache(tmp_path), reason="出站被拦")
        decisions = subwarm.plan_subwarm(tmp_path, [_item()],
                                        base_url=BASE, token=TOKEN)
        assert decisions[0].action == subwarm.ACTION_SKIP
        assert "负缓存" in decisions[0].reason
        forced = subwarm.plan_subwarm(tmp_path, [_item()],
                                      base_url=BASE, token=TOKEN, force=True)
        assert forced[0].action == subwarm.ACTION_FETCH

    def test_limit_only_counts_fetch(self, tmp_path):
        items = [_item("a"), _item("b"), _item("c")]
        decisions = subwarm.plan_subwarm(tmp_path, items,
                                        base_url=BASE, token=TOKEN, limit=2)
        fetches = [d for d in decisions if d.action == subwarm.ACTION_FETCH]
        skips = [d for d in decisions if d.action == subwarm.ACTION_SKIP]
        assert len(fetches) == 2 and len(skips) == 1
        assert "limit" in skips[0].reason

    def test_force_reignites_l1_and_hit(self, tmp_path):
        _save(_cache(tmp_path))
        embedded = _item("e1", kind="text", langs={"zh", "en"})
        decisions = subwarm.plan_subwarm(tmp_path, [embedded, _item("f1")],
                                        base_url=BASE, token=TOKEN, force=True)
        assert all(d.action == subwarm.ACTION_FETCH for d in decisions)


def _fetch_ok(root, decision, log, timeout=None):
    _save(decision.cache_dir)
    return subwarm.ACTION_FETCH, "zh+en", "subfetch rc=0"


def _fetch_fail(root, decision, log, timeout=None):
    return subwarm.ACTION_FAILED, "", "出站被拦"


def _master_pack(root: Path, item_id="i1") -> Path:
    out = root / "_jf" / item_id
    out.mkdir(parents=True, exist_ok=True)
    (out / "master.m3u8").write_text(
        "#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=8000000,RESOLUTION=3840x2160\nvideo.m3u8\n",
        encoding="utf-8")
    return out


class TestRun:
    def test_l1_hit_skip_bookkeeping(self, tmp_path):
        # 缓存要存到 h1 自己的指纹下：plan 用每个 item 的 item_id 重建 src，
        # 写到别的指纹下只会让 h1 判成 fetch（白跑一次在线腿）。
        _save(_cache(tmp_path, "h1"))
        decisions = subwarm.plan_subwarm(
            tmp_path,
            [_item("e1", kind="text", langs={"zh", "en"}), _item("h1"), _item("s1", ext="")],
            base_url=BASE, token=TOKEN)
        decisions.append(subwarm.SubwarmDecision(
            item=_item("f1"), action=subwarm.ACTION_FETCH, reason="test",
            src=_src("f1"), cache_dir=_cache(tmp_path, "f1")))
        # fetch 结局必须受控：本测试验证的是 l1/hit/skip/fetch 四类记账，
        # 不是在线抓取本身。不 mock 时会真打字幕站，资源有无决定断言成败
        # （2026-10-12 全套跑挂的根因：SubHD 无命中 → failed ≠ fetched）。
        with mock.patch.object(subwarm, "_fetch_one", _fetch_ok):
            report = subwarm.run_subwarm(tmp_path, decisions,
                                         log=lambda *a, **k: None)
        assert len(report.l1) == 1 and len(report.hits) == 1
        assert len(report.skipped) == 1 and len(report.fetched) == 1
        assert report.ok
        assert report.elapsed >= 0

    def test_l1_hit_skip_bookkeeping_mocked_fetch(self, tmp_path, monkeypatch):
        # h1 无预存缓存 → fetch（mock 抓到 zh+en）；s1 无后缀 → skip。
        # 记账：l1=1 / hits=0 / fetched=1 / skipped=1。
        monkeypatch.setattr(subsource, "fetch_online_texts",
                            lambda *, src, item_id, name, out_dir, timeout=None:
                            ({"zh": "WEBVTT\n\n1\n", "en": "WEBVTT\n\n2\n"},
                             "subfetch rc=0"))
        decisions = subwarm.plan_subwarm(
            tmp_path,
            [_item("e1", kind="text", langs={"zh", "en"}), _item("h1"), _item("s1", ext="")],
            base_url=BASE, token=TOKEN)
        report = subwarm.run_subwarm(tmp_path, decisions, log=lambda *a, **k: None)
        assert len(report.l1) == 1 and len(report.hits) == 0
        assert len(report.skipped) == 1 and len(report.fetched) == 1
        assert report.fetched[0][2] == "zh+en"
        assert report.ok
        assert report.elapsed >= 0

    def test_failed_isolation(self, tmp_path):
        decision = subwarm.SubwarmDecision(
            item=_item(), action=subwarm.ACTION_FETCH, reason="t",
            src=_src(), cache_dir=_cache(tmp_path))
        with mock.patch.object(subwarm, "_fetch_one", _fetch_fail):
            report = subwarm.run_subwarm(tmp_path, [decision], log=lambda *a, **k: None)
        assert len(report.failed) == 1 and not report.ok
        assert "出站被拦" in report.failed[0][2]

    def test_fetch_success_writes_cache(self, tmp_path):
        decision = subwarm.SubwarmDecision(
            item=_item(), action=subwarm.ACTION_FETCH, reason="t",
            src=_src(), cache_dir=_cache(tmp_path))
        with mock.patch.object(subwarm, "_fetch_one", _fetch_ok):
            report = subwarm.run_subwarm(tmp_path, [decision], log=lambda *a, **k: None)
        assert report.ok and len(report.fetched) == 1
        assert (decision.cache_dir / "zh.vtt").exists()

    def test_real_fetch_writes_cache_and_upgrades_master(self, tmp_path, monkeypatch):
        pack = _master_pack(tmp_path)
        texts = {"zh": "WEBVTT\n\n00:00:01.000 --> 00:00:02.000\n你好",
                 "en": "WEBVTT\n\n00:00:01.000 --> 00:00:02.000\nHello"}
        monkeypatch.setattr(subsource, "fetch_online_texts",
                            lambda *, src, item_id, name, out_dir, timeout=None:
                            (texts, "subfetch rc=0"))
        decision = subwarm.SubwarmDecision(
            item=_item(), action=subwarm.ACTION_FETCH, reason="t",
            src=_src(), cache_dir=_cache(tmp_path))
        report = subwarm.run_subwarm(tmp_path, [decision], log=lambda *a, **k: None)
        assert report.ok and report.fetched[0][2] == "zh+en"
        assert (decision.cache_dir / "zh.vtt").exists()
        assert (pack / "sub_zh.vtt").exists()
        assert "TYPE=SUBTITLES" in (pack / "master.m3u8").read_text(encoding="utf-8")

    def test_real_fetch_failure_records_negative_cache(self, tmp_path, monkeypatch):
        monkeypatch.setattr(subsource, "fetch_online_texts",
                            lambda *, src, item_id, name, out_dir, timeout=None:
                            ({}, "subfetch 无产出"))
        decision = subwarm.SubwarmDecision(
            item=_item(), action=subwarm.ACTION_FETCH, reason="t",
            src=_src(), cache_dir=_cache(tmp_path))
        report = subwarm.run_subwarm(tmp_path, [decision], log=lambda *a, **k: None)
        assert not report.ok and "无产出" in report.failed[0][2]
        assert subsource.cache_blocked(decision.cache_dir)

    def test_master_upgrade_failure_keeps_cache(self, tmp_path, monkeypatch):
        # item_id 默认 i1：_jf/i1/master.m3u8 由 _master_pack 造出，
        # upgrade 抛在「缓存已写、master 换轨」之间 → 记账升级失败但缓存保留。
        _master_pack(tmp_path)
        texts = {"zh": "WEBVTT\n\n00:00:01.000 --> 00:00:02.000\n你好",
                 "en": "WEBVTT\n\n00:00:01.000 --> 00:00:02.000\nHello"}
        monkeypatch.setattr(subsource, "fetch_online_texts",
                            lambda *, src, item_id, name, out_dir, timeout=None:
                            (texts, "subfetch rc=0"))
        # subs 是 _fetch_one 内部的延迟 import，patch 模块属性同样生效。
        monkeypatch.setattr("dvpack.subs.upgrade_master_subtitles",
                            mock.Mock(side_effect=ValueError("畸形 master")))
        decision = subwarm.SubwarmDecision(
            item=_item(), action=subwarm.ACTION_FETCH, reason="t",
            src=_src(), cache_dir=_cache(tmp_path))
        assert (tmp_path / "_jf" / "i1" / "master.m3u8").exists()
        report = subwarm.run_subwarm(tmp_path, [decision], log=lambda *a, **k: None)
        assert report.ok
        # fetched 第三位只放语言串（run_from_args 据此判新齐）；升级失败
        # 走 log 的那一行，缓存照样落盘。
        assert report.fetched[0][2].startswith("zh+en")
        assert (decision.cache_dir / "zh.vtt").exists()

    def test_never_raises_on_exception(self, tmp_path, monkeypatch):
        monkeypatch.setattr(subsource, "fetch_online_texts",
                            mock.Mock(side_effect=OSError("炸了")))
        decision = subwarm.SubwarmDecision(
            item=_item(), action=subwarm.ACTION_FETCH, reason="t",
            src=_src(), cache_dir=_cache(tmp_path))
        report = subwarm.run_subwarm(tmp_path, [decision], log=lambda *a, **k: None)
        assert not report.ok and "炸了" in report.failed[0][2]


class TestCli:
    def test_missing_base_is_error(self, capsys):
        assert subwarm.run_from_args(SimpleNamespace(
            base="", token="", user="root", pw="", langs="zh,en",
            include_types="Movie", limit=0, force=False, timeout=None,
            dry_run=True)) == 2

    def test_missing_credentials_is_error(self, monkeypatch):
        assert subwarm.run_from_args(SimpleNamespace(
            base=BASE, token="", user="root", pw="", langs="zh,en",
            include_types="Movie", limit=0, force=False, timeout=None,
            dry_run=True)) == 2

    def test_dry_run_reports_without_fetching(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setattr(subwarm, "enumerate_items",
                            lambda *a, **k: [_item(kind="pgs")])
        rc = subwarm.run_from_args(SimpleNamespace(
            base=BASE, token=TOKEN, user="root", pw="", langs="zh,en",
            include_types="Movie", limit=0, force=False, timeout=None,
            dry_run=True, root=tmp_path))
        assert rc == 0
        assert "待抓 1" in capsys.readouterr().out

    def test_no_todo_returns_zero(self, tmp_path, monkeypatch):
        _save(_cache(tmp_path))
        monkeypatch.setattr(subwarm, "enumerate_items",
                            lambda *a, **k: [_item()])
        rc = subwarm.run_from_args(SimpleNamespace(
            base=BASE, token=TOKEN, user="root", pw="", langs="zh,en",
            include_types="Movie", limit=0, force=False, timeout=None,
            dry_run=False, root=tmp_path))
        assert rc == 0

    def test_failed_batch_returns_one(self, tmp_path, monkeypatch):
        monkeypatch.setattr(subwarm, "enumerate_items",
                            lambda *a, **k: [_item(kind="pgs")])
        monkeypatch.setattr(subsource, "fetch_online_texts",
                            lambda *, src, item_id, name, out_dir, timeout=None:
                            ({}, "公网不可达"))
        rc = subwarm.run_from_args(SimpleNamespace(
            base=BASE, token=TOKEN, user="root", pw="", langs="zh,en",
            include_types="Movie", limit=0, force=False, timeout=None,
            dry_run=False, root=tmp_path))
        assert rc == 1

    def test_main_entry(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setattr(subwarm, "enumerate_items", lambda *a, **k: [])
        monkeypatch.setenv("DVPACK_JF_BASE", BASE)
        monkeypatch.setenv("DVPACK_JF_TOKEN", TOKEN)
        assert subwarm.main(["--dry-run", str(tmp_path)]) == 0
        assert "待抓 0" in capsys.readouterr().out

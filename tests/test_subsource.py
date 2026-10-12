"""dvpack.subsource：字幕来源分级（文本轨 → 缓存 → 在线）的决策逻辑。

真机链路（Jellyfin 文本轨抓取、subfetch 公网抓取）另在 2026-10-05 手工验过
（_subtitle_texts 1.5s 拿到 zh+en 真 SRT；本机出站被拦，换网络环境即通）；
这里只把分级决策、负缓存、命令形状焊死，全部 mock。
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

from dvpack import subsource
from dvpack.subsource import (
    CACHE,
    JELLYFIN,
    SubtitleResolution,
    cache_blocked,
    clear_online_failure,
    fetch_online_texts,
    load_cached_texts,
    online_enabled,
    online_timeout,
    record_online_failure,
    resolve_local_texts,
    retry_after_seconds,
    save_cached_texts,
    subtitle_cache_dir,
)


class TestPolicy:
    def test_online_default_on(self, monkeypatch):
        monkeypatch.delenv(subsource.ONLINE_ENV, raising=False)
        assert online_enabled() is True

    @pytest.mark.parametrize("word", ["0", "false", "NO", "off"])
    def test_online_off_words(self, monkeypatch, word):
        monkeypatch.setenv(subsource.ONLINE_ENV, word)
        assert online_enabled() is False

    def test_retry_after_default_six_hours(self, monkeypatch):
        monkeypatch.delenv(subsource.RETRY_HOURS_ENV, raising=False)
        assert retry_after_seconds() == 6 * 3600

    def test_retry_after_env_hours(self, monkeypatch):
        monkeypatch.setenv(subsource.RETRY_HOURS_ENV, "1.5")
        assert retry_after_seconds() == 1.5 * 3600

    def test_retry_after_bad_env_falls_back(self, monkeypatch):
        monkeypatch.setenv(subsource.RETRY_HOURS_ENV, "一小时")
        assert retry_after_seconds() == 6 * 3600

    def test_timeout_env_and_floor(self, monkeypatch):
        monkeypatch.delenv(subsource.TIMEOUT_ENV, raising=False)
        assert online_timeout() == 240.0
        monkeypatch.setenv(subsource.TIMEOUT_ENV, "1")
        assert online_timeout() == 5.0

class TestCacheDir:
    def test_deterministic_and_scoped(self, tmp_path):
        a = subtitle_cache_dir(tmp_path, "http://jf/videos/abc/stream.mkv?static=true")
        b = subtitle_cache_dir(tmp_path, "http://jf/videos/abc/stream.mkv?static=true")
        c = subtitle_cache_dir(tmp_path, "http://jf/videos/xyz/stream.mkv?static=true")
        assert a == b
        assert a != c
        assert a.parent == tmp_path / "_subs"
        assert len(a.name) == 16


class TestCacheIO:
    def test_roundtrip(self, tmp_path):
        cache = subtitle_cache_dir(tmp_path, "fp")
        save_cached_texts(cache, {"zh": "1\n00:00:01,000 --> 00:00:02,000\n中文",
                                  "en": "1\n00:00:01,000 --> 00:00:02,000\nhi"},
                          subsource.ONLINE)
        texts, meta = load_cached_texts(cache)
        assert set(texts) == {"zh", "en"}
        assert "中文" in texts["zh"]
        assert meta["source"] == subsource.ONLINE
        assert meta["cues"]["zh"] == 1

    def test_missing_cache_is_empty(self, tmp_path):
        texts, meta = load_cached_texts(tmp_path / "nope")
        assert texts == {} and meta == {}

    def test_corrupt_meta_tolerated(self, tmp_path):
        cache = tmp_path / "c"
        cache.mkdir()
        (cache / "meta.json").write_text("{坏 json", encoding="utf-8")
        texts, meta = load_cached_texts(cache)
        assert texts == {} and meta == {}

    def test_failure_negative_cache_and_clear(self, tmp_path):
        cache = subtitle_cache_dir(tmp_path, "fp")
        record_online_failure(cache, "出站被拦")
        assert cache_blocked(cache) is True
        _, meta = load_cached_texts(cache)
        assert "出站被拦" in meta["reason"]
        # TTL 归零后放行
        assert cache_blocked(cache, retry_after=0.0) is False
        clear_online_failure(cache)
        assert cache_blocked(cache) is False

    def test_empty_text_not_cached(self, tmp_path):
        cache = subtitle_cache_dir(tmp_path, "fp")
        save_cached_texts(cache, {"zh": "   ", "en": ""}, subsource.ONLINE)
        assert load_cached_texts(cache)[0] == {}


class TestResolveLocal:
    def _info(self):
        return SimpleNamespace(path=Path("x.mkv"), subtitle_streams=[])

    def test_jellyfin_wins(self, tmp_path):
        cache = subtitle_cache_dir(tmp_path, "fp")
        save_cached_texts(cache, {"zh": "缓存字幕"}, subsource.ONLINE)
        calls = []

        def fetcher(src, info):
            calls.append((src, info))
            return {"zh": "文本轨字幕", "en": "text track"}

        res = resolve_local_texts("http://jf/s.mkv", self._info(),
                                  cache_dir=cache, fetcher=fetcher)
        assert res.level == JELLYFIN and res.texts["zh"] == "文本轨字幕"
        assert calls  # L1 真的被问过

    def test_cache_fallback(self, tmp_path):
        cache = subtitle_cache_dir(tmp_path, "fp")
        save_cached_texts(cache, {"zh": "缓存字幕"}, subsource.ONLINE)
        res = resolve_local_texts("http://jf/s.mkv", self._info(),
                                  cache_dir=cache,
                                  fetcher=lambda s, i: {})
        assert res.level == CACHE and "缓存字幕" in res.texts["zh"]
        assert res.ok

    def test_no_cache_dir_skips_l2(self):
        res = resolve_local_texts("http://jf/s.mkv", self._info(),
                                  fetcher=lambda s, i: {})
        assert res.level == "" and res.ok is False

    def test_nothing_anywhere(self, tmp_path):
        cache = subtitle_cache_dir(tmp_path, "fp")
        res = resolve_local_texts("http://jf/s.mkv", self._info(),
                                  cache_dir=cache, fetcher=lambda s, i: {})
        assert res.texts == {} and res.level == ""

    def test_local_levels_never_fetch_online(self, tmp_path):
        # 分级硬约束：本地路径绝不联网。fetch_online_texts 一被调用就炸。
        cache = subtitle_cache_dir(tmp_path, "fp")
        save_cached_texts(cache, {"zh": "缓存字幕"}, subsource.ONLINE)
        with mock.patch.object(subsource, "fetch_online_texts",
                               side_effect=AssertionError("热路径联网了")):
            res = resolve_local_texts("http://jf/s.mkv", self._info(),
                                      cache_dir=cache, fetcher=lambda s, i: {})
        assert res.level == CACHE


class TestFetchOnline:
    def _run_factory(self, out_dir: Path, langs=("zh", "en")):
        def run(cmd, **kwargs):
            for language in langs:
                (out_dir / f"sub_{language}.cat.vtt").write_text(
                    "WEBVTT\n\n00:00:01.000 --> 00:00:02.000\n%s 字幕" % language,
                    encoding="utf-8")
            return SimpleNamespace(returncode=0, stdout="ok", stderr="")
        return run

    def test_command_shape(self, tmp_path, monkeypatch):
        # env 里的 key 经过子进程继承照样生效，只是不该出现在 argv 里
        monkeypatch.setenv("OPENSUBTITLES_KEY", "os-key-123")
        seen = {}

        def run(cmd, **kwargs):
            seen["cmd"] = cmd
            seen["kwargs"] = kwargs
            return self._run_factory(tmp_path)(cmd, **kwargs)

        texts, note = fetch_online_texts(
            src="http://jf:8096/videos/abc/stream.mkv?static=true&api_key=t0k",
            item_id="abc", name="纽伦堡 2025", out_dir=tmp_path, run=run)
        cmd = seen["cmd"]
        assert cmd[0] == __import__("sys").executable
        assert str(subsource._subfetch_script()) in " ".join(str(c) for c in cmd)
        assert "--name" in cmd and cmd[cmd.index("--name") + 1] == "纽伦堡 2025"
        assert "--langs" in cmd and cmd[cmd.index("--langs") + 1] == "zh,en"
        assert "--src" in cmd and "api_key=t0k" in cmd[cmd.index("--src") + 1]
        # key 不由父进程传递：子进程 subfetch 自己按 env > 配置文件解析
        assert "--os-key" not in cmd
        assert seen["kwargs"]["capture_output"] is True
        assert seen["kwargs"]["timeout"] == 240.0
        assert set(texts) == {"zh", "en"}
        assert "字幕" in texts["zh"]
        assert "rc=0" in note

    def test_without_name_skips_entirely(self, tmp_path):
        with mock.patch.object(subprocess, "run",
                               side_effect=AssertionError("没片名不该起子进程")):
            texts, note = fetch_online_texts(
                src="http://jf/s.mkv", item_id="abc", name="", out_dir=tmp_path)
        assert texts == {}
        assert "片名" in note

    def test_timeout_returns_reason(self, tmp_path):
        def run(cmd, **kwargs):
            raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout"))

        texts, note = fetch_online_texts(
            src="http://jf/s.mkv", item_id="abc", name="某片", out_dir=tmp_path,
            run=run)
        assert texts == {} and "超时" in note

    def test_oserror_returns_reason(self, tmp_path):
        def run(cmd, **kwargs):
            raise OSError("no such python")

        texts, note = fetch_online_texts(
            src="http://jf/s.mkv", item_id="abc", name="某片", out_dir=tmp_path,
            run=run)
        assert texts == {} and "subfetch 起不来" in note

    def test_no_output_gives_stdout_tail(self, tmp_path):
        proc = SimpleNamespace(returncode=1, stdout="[zh] 未找到", stderr="boom")
        texts, note = fetch_online_texts(
            src="http://jf/s.mkv", item_id="abc", name="某片", out_dir=tmp_path,
            run=lambda cmd, **kw: proc)
        assert texts == {}
        assert "rc=1" in note and "未找到" in note

    def test_only_zh_found(self, tmp_path):
        run = self._run_factory(tmp_path, langs=("zh",))
        texts, note = fetch_online_texts(
            src="http://jf/s.mkv", item_id="abc", name="某片", out_dir=tmp_path,
            run=run)
        assert set(texts) == {"zh"} and "zh" in note

    def test_empty_file_ignored(self, tmp_path):
        (tmp_path / "sub_zh.cat.vtt").write_text("  \n", encoding="utf-8")
        proc = SimpleNamespace(returncode=0, stdout="", stderr="")
        texts, note = fetch_online_texts(
            src="http://jf/s.mkv", item_id="abc", name="某片", out_dir=tmp_path,
            run=lambda cmd, **kw: proc)
        assert texts == {}

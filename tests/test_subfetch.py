"""subfetch 的 OS moviehash：与 subliminal 金标准对齐 + 检索分级 + 兜底。

hash 算法向量不是「我们觉得对」而是与 subliminal 2.7.1 现场逐字节
比对生成的（2026-10-05）；服务端相关路径全部 mock，离线可跑。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

import subfetch  # noqa: E402


import argparse  # noqa: E402
import io  # noqa: E402
import shutil  # noqa: E402
import subprocess  # noqa: E402
import time  # noqa: E402
import urllib.parse  # noqa: E402
import zipfile  # noqa: E402

import pytest  # noqa: E402


def _synth_vector(size: int = 307200) -> bytes:
    """确定性合成文件：subliminal 参考向量同款生成器，可复现。"""
    return bytes((i * 37 + 11) % 256 for i in range(size))


def test_hash_matches_subliminal_vector():
    """参考向量由 subliminal.refiners.hash.hash_opensubtitles 现场比对
    生成（subliminal 2.7.1），值焊死防未来改回归。"""
    buf = _synth_vector()
    assert subfetch.opensubtitles_hash(buf[:65536], buf[-65536:], len(buf)) \
        == "6020dfa05f247000"


def test_hash_rejects_small_files():
    # <128KB 服务端不收 hash（subliminal 同口径）
    assert subfetch.opensubtitles_hash(b"\0" * 65536, b"\0" * 65536, 131071) is None


def test_hash_rejects_truncated_ranges():
    # 首/尾任一段不足 64KB：残缺输入凑的 hash 匹配不到，还可能挤占额度
    buf = _synth_vector()
    assert subfetch.opensubtitles_hash(buf[:65536], buf[-100:], len(buf)) is None
    assert subfetch.opensubtitles_hash(buf[:100], buf[-65536:], len(buf)) is None


def test_os_search_hash_query_uses_moviehash_params(monkeypatch):
    seen = {}

    def fake_curl_bytes(url, headers=None, **_kw):
        seen["url"] = url
        return json.dumps({"data": []}).encode()

    monkeypatch.setattr(subfetch, "curl_bytes", fake_curl_bytes)
    subfetch.os_search(None, "en", "KEY", moviehash="abc123", bytesize=307200)
    assert "moviehash=abc123" in seen["url"]
    assert "moviebytesize=307200" in seen["url"]
    assert "query=" not in seen["url"]  # hash 判据下不带 query


def test_os_search_name_query_has_no_hash_params(monkeypatch):
    seen = {}

    def fake_curl_bytes(url, headers=None, **_kw):
        seen["url"] = url
        return json.dumps({"data": []}).encode()

    monkeypatch.setattr(subfetch, "curl_bytes", fake_curl_bytes)
    subfetch.os_search("Nuremberg 2025", "en", "KEY")
    assert "query=Nuremberg" in seen["url"]
    assert "moviehash" not in seen["url"]


def _os_item(file_id: int, downloads: int = 100):
    return {
        "file_id": file_id,
        "language": "en",
        "release": "rel.%d" % file_id,
        "title": "Nuremberg",
        "year": 2025,
        "imdb": None,
        "tmdb": None,
        "fps": None,
        "downloads": downloads,
        "hi": False,
        "trusted": True,
        "url": "",
    }


def test_os_fetch_prefers_hash_tier(monkeypatch):
    monkeypatch.setattr(subfetch, "os_source_hash", lambda src: ("deadbeef", 307200))
    modes = []

    def fake_search(query, lang, key, per_page=20, moviehash=None, bytesize=None):
        modes.append("hash" if moviehash else "name")
        if moviehash:
            return [_os_item(7)]
        raise AssertionError("hash 命中后不应再走名称检索")

    monkeypatch.setattr(subfetch, "os_search", fake_search)
    monkeypatch.setattr(
        subfetch, "os_download",
        lambda fid, key: (b"1\n00:00:01,000 --> 00:00:03,000\nWorld\n", "http://dl"),
    )
    cues, label = subfetch.os_fetch(
        "Nuremberg 2025", "en", "KEY", "2025",
        src="http://192.168.1.68:8096/Videos/x/stream.mkv?static=true",
    )
    assert modes == ["hash"]
    assert label == "os:rel.7"
    assert cues


def test_os_fetch_falls_back_to_name_when_hash_unavailable(monkeypatch):
    # 源不可达/hash 拿不到（None）时老实回落到名称检索，不丧坐标题
    monkeypatch.setattr(subfetch, "os_source_hash", lambda src: (None, 0))
    modes = []

    def fake_search(query, lang, key, per_page=20, moviehash=None, bytesize=None):
        modes.append("hash" if moviehash else "name")
        if moviehash:
            return []
        return [_os_item(9)]

    monkeypatch.setattr(subfetch, "os_search", fake_search)
    monkeypatch.setattr(
        subfetch, "os_download",
        lambda fid, key: (b"1\n00:00:01,000 --> 00:00:03,000\nWorld\n", "http://dl"),
    )
    cues, label = subfetch.os_fetch(
        "Nuremberg 2025", "en", "KEY", "2025",
        src="http://192.168.1.68:8096/Videos/x/stream.mkv?static=true",
    )
    assert modes == ["name"]
    assert label == "os:rel.9"


def test_os_fetch_hash_tier_survives_search_failure(monkeypatch):
    # hash 检索接口炸了也不能把整个字幕拖死：转名称检索
    monkeypatch.setattr(subfetch, "os_source_hash", lambda src: ("deadbeef", 307200))

    def fake_search(query, lang, key, per_page=20, moviehash=None, bytesize=None):
        if moviehash:
            raise RuntimeError("503 upstream")
        return [_os_item(11)]

    monkeypatch.setattr(subfetch, "os_search", fake_search)
    monkeypatch.setattr(
        subfetch, "os_download",
        lambda fid, key: (b"1\n00:00:01,000 --> 00:00:03,000\nWorld\n", "http://dl"),
    )
    cues, label = subfetch.os_fetch(
        "Nuremberg 2025", "en", "KEY", "2025",
        src="http://192.168.1.68:8096/Videos/x/stream.mkv?static=true",
    )
    assert label == "os:rel.11"


# --- 7z/PGS：SubHD 下载包形态（v52，纽伦堡实证下载链大量是 .7z） ---

_SRT = "1\n00:00:01,000 --> 00:00:02,000\n你好 hello\n"


def _zip_bytes(files: dict) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, body in files.items():
            zf.writestr(name, body)
    return buf.getvalue()


def test_subtitle_bytes_plain_text_passthrough():
    raw = _SRT.encode("utf-8")
    assert subfetch._subtitle_bytes(raw) == (raw, "raw")


def test_subtitle_bytes_zip_prefers_ass_over_srt():
    raw = _zip_bytes({"movie.srt": _SRT, "movie.ass": "[Events]\n"})
    out, member = subfetch._subtitle_bytes(raw)
    assert member == "movie.ass"
    assert out == b"[Events]\n"


def test_subtitle_bytes_zip_image_only_raises():
    raw = _zip_bytes({"movie.chi.sup": b"PG\x00\x3d", "movie.eng.sup": b"PG\x00\x3c"})
    with pytest.raises(RuntimeError, match="图像字幕"):
        subfetch._subtitle_bytes(raw)


def _stub_bsdtar(monkeypatch, listing, extracted=None):
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        if cmd[1] == "-tf":
            return subprocess.CompletedProcess(cmd, 0,
                                               stdout=listing.encode("utf-8"), stderr=b"")
        return subprocess.CompletedProcess(cmd, 0, stdout=extracted, stderr=b"")

    monkeypatch.setattr(subfetch.subprocess, "run", fake_run)
    return calls


def test_subtitle_bytes_7z_picks_text_over_pgs(monkeypatch):
    calls = _stub_bsdtar(monkeypatch, "movie.srt\nmovie.chi.sup\n", _SRT.encode("utf-8"))
    out, member = subfetch._subtitle_bytes(b"7z\xbc\xaf\x27\x1cSTUB")
    assert member == "movie.srt"
    assert subfetch.os_decode(out) == _SRT
    assert calls[0][1] == "-tf" and calls[1][1] == "-xOf"


def test_subtitle_bytes_7z_image_only_raises(monkeypatch):
    calls = _stub_bsdtar(monkeypatch, "movie.chi.sup\nmovie.eng.sup\n")
    with pytest.raises(RuntimeError, match="PGS"):
        subfetch._subtitle_bytes(b"7z\xbc\xaf\x27\x1cSTUB")
    assert len(calls) == 1  # 图像包只列出、不解包


def test_subtitle_bytes_7z_real_bsdtar(tmp_path):
    """真实 bsdtar 合成 7z 再解：防 argv 写错（stub 测不出 -tf/-xOf 拼写）。"""
    if not shutil.which("bsdtar"):
        pytest.skip("本机无 bsdtar")
    (tmp_path / "movie.srt").write_text(_SRT, encoding="utf-8")
    (tmp_path / "movie.chi.sup").write_bytes(b"PG\x00\x3djunk")
    arc = tmp_path / "t.7z"
    subprocess.run([shutil.which("bsdtar"), "--format", "7zip", "-cf", str(arc),
                    "movie.srt", "movie.chi.sup"], cwd=tmp_path, check=True,
                   capture_output=True)
    out, member = subfetch._subtitle_bytes(arc.read_bytes())
    assert member == "movie.srt"
    assert subfetch.parse_cues(subfetch.os_decode(out))


def test_subhd_fetch_moves_past_pgs_only_candidate(monkeypatch, capsys):
    """首选是 PGS 包不能静默失败：必须打原因并换下一个候选（纽伦堡实证链路）。"""
    entries = [("s1", "纽伦堡.Nuremberg.2025.1080p.Remux.BluRay.AVC.DTS-HD.MA.5.1"),
               ("s2", "Nuremberg.AVC.2025.2160p.WEB-DL.zh&en")]
    monkeypatch.setattr(subfetch, "subhd_entries", lambda q, cap=30: list(entries))

    def fake_cues(sid, url=None):
        if sid == "s1":
            raise RuntimeError("7z 内只有图像字幕(PGS .sup): ['a.chi.sup']")
        return [(1.0, 2.0, "你好世界")]

    monkeypatch.setattr(subfetch, "subhd_cues", fake_cues)
    a = argparse.Namespace(name=entries[0][1], subhd_sid="", subhd_url="", sub_file="")
    got = subfetch.subhd_fetch(a, "zh", ["纽伦堡"], "纽伦堡")
    assert got and got[0] == [(1.0, 2.0, "你好世界")]
    assert "PGS" in capsys.readouterr().out


# --- v51：检索词变体 + 匹配判据 + key 配置（上轮改动补测） ---

def test_build_queries_variants_and_dedup():
    q = subfetch.build_queries("风味人间.Once.Upon.a.Bite.S05E03.2024.2160p.WEB-DL")
    assert q[0] == "风味人间.Once.Upon.a.Bite.S05E03.2024.2160p.WEB-DL"
    assert "风味人间" in q                      # 中文站吃裸中文题名
    assert "Once Upon a Bite s05e03" in q       # 拉丁题名 + 季集
    assert len(q) <= 5 and len(set(q)) == len(q)


def test_build_queries_latin_title_of_cjk_release():
    q = subfetch.build_queries("纽伦堡.Nuremberg.2025.2160p.WEB-DL.x265")
    assert "纽伦堡" in q and "Nuremberg" in q


def test_build_queries_drops_bare_season_and_resolution_tokens():
    """只剩季号/分辨率尾巴时会漏成 "S"/"p" 单字母词（上轮修过的回归）。"""
    q = subfetch.build_queries("Kung.Fu.Panda.S03.2160p.WEB-DL")
    assert "Kung Fu Panda" in q
    assert "Kung Fu Panda S" not in q
    assert all(len(x) >= 3 for x in q)


def test_core_coverage_same_film_different_release():
    """老口径 Jaccard 对同片跨压制只有 0.08，覆盖率必须 >= 闸 0.5。"""
    assert subfetch.core_coverage("纽伦堡.Nuremberg.2025.2160p.WEB-DL.x265",
                                  "纽伦堡.Nuremberg.2025.1080p.Remux.BluRay.AVC") >= 0.5


def test_core_coverage_different_film_scores_zero():
    assert subfetch.core_coverage("风味人间.Once.Upon.a.Bite.S05.2024.2160p",
                                  "Nuremberg.2025.WEB-DL") == 0.0


def test_se_key_normalizes_variants():
    assert subfetch._se_key("S05E03") == "s5e3"
    assert subfetch._se_key("S05.E03") == "s5e3"
    assert subfetch._se_key("s05 e03") == "s5e3"
    assert subfetch._se_key("Nuremberg 2025") is None


def test_os_api_key_priority(monkeypatch, tmp_path):
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"opensubtitles_key": "CFGKEY"}), encoding="utf-8")
    monkeypatch.setenv(subfetch.CONFIG_ENV, str(cfg))
    monkeypatch.setenv("OPENSUBTITLES_KEY", "ENVKEY")
    assert subfetch.os_api_key() == "ENVKEY"
    assert subfetch.os_api_key("CLIKEY") == "CLIKEY"      # CLI 最高
    monkeypatch.delenv("OPENSUBTITLES_KEY")
    assert subfetch.os_api_key() == "CFGKEY"               # env 缺 -> 配置文件
    monkeypatch.setenv("OPENSUBTITLES_KEY", "   ")
    assert subfetch.os_api_key() == "CFGKEY"               # 空白 env 不算
    cfg.write_text("not json {", encoding="utf-8")
    assert subfetch.os_api_key() == ""                     # 坏配置回空不抛


def test_os_probe_key_bad_key_reason(monkeypatch):
    monkeypatch.setattr(subfetch, "_curl_probe",
                        lambda url, headers, timeout=12.0:
                        (0, json.dumps({"message": "You cannot consume this service"}).encode(), b""))
    assert subfetch.os_probe_key("BADKEY") == "You cannot consume this service"


def test_os_probe_key_network_failure_not_bad(monkeypatch):
    """网络层失败不能判失效——否则离线会把好 key 永久拉黑。"""
    monkeypatch.setattr(subfetch, "_curl_probe",
                        lambda *a, **k: (23, b"", b"connection reset"))
    assert subfetch.os_probe_key("GOODKEY") == ""


def test_os_probe_key_healthy_payload(monkeypatch):
    monkeypatch.setattr(subfetch, "_curl_probe",
                        lambda *a, **k: (0, json.dumps({"data": []}).encode(), b""))
    assert subfetch.os_probe_key("GOODKEY") == ""


def test_os_probe_key_url_carries_type_and_long_query(monkeypatch):
    """v56：探针 URL 必须带 type=movie 且 query 长度 >1——缺任一项服务端回
    400 且不带 data 字段，好 key 会被误判失效关进 6 小时负缓存，整条在线字幕腿躺平。"""
    seen = {}

    def fake(url, headers, timeout=12.0):
        seen["url"] = url
        return (0, json.dumps({"data": []}).encode(), b"")

    monkeypatch.setattr(subfetch, "_curl_probe", fake)
    assert subfetch.os_probe_key("GOODKEY") == ""
    qs = urllib.parse.parse_qs(urllib.parse.urlparse(seen["url"]).query)
    assert qs.get("type") == ["movie"]
    assert len(qs["query"][0]) > 1


def test_os_probe_key_400_stays_transient(monkeypatch):
    """v56：400 类请求侧错误（如参数写错）不判 key 失效、不落负缓存。"""
    monkeypatch.setattr(
        subfetch, "_curl_probe",
        lambda *a, **k: (0, json.dumps(
            {"errors": ["Not enough parameters"], "status": 400}).encode(), b""))
    assert subfetch.os_probe_key("GOODKEY") == ""


def _fake_os_search_bytes(seen):
    def fake(url, timeout=30, headers=None, method="GET", data=None, pin=None,
             cookies=False):
        seen["url"] = url
        return json.dumps({"data": [{"id": "1", "type": "subtitle",
                                    "attributes": {"language": "en",
                                                   "release": "x",
                                                   "download_count": 1}}]}).encode()
    return fake


def test_os_search_tvshow_carries_season_episode(monkeypatch):
    """v57：剧集名必须 type=tvshow + season/episode——movie 型下
    服务端过滤掉剧集库，实测 0 命中。"""
    seen = {}
    monkeypatch.setattr(subfetch, "curl_bytes", _fake_os_search_bytes(seen))
    subfetch.os_search("True Haunting S01E01 1080p", "en", "KEY")
    qs = urllib.parse.parse_qs(urllib.parse.urlparse(seen["url"]).query)
    assert qs["type"] == ["tvshow"]
    assert qs["season_number"] == ["1"]
    assert qs["episode_number"] == ["1"]


def test_os_search_movie_stays_type_movie(monkeypatch):
    seen = {}
    monkeypatch.setattr(subfetch, "curl_bytes", _fake_os_search_bytes(seen))
    subfetch.os_search("Nuremberg 2025 2160p", "en", "KEY")
    qs = urllib.parse.parse_qs(urllib.parse.urlparse(seen["url"]).query)
    assert qs["type"] == ["movie"]
    assert "season_number" not in qs


def test_os_key_bad_marker_hides_key_itself():
    marker = subfetch._os_key_bad_marker("SUPERSECRET")
    assert "SUPERSECRET" not in marker.name
    assert marker.name.startswith("dvpack-os-key-bad-")


def test_os_key_bad_cached_ttl_expires(monkeypatch, tmp_path):
    marker = tmp_path / "marker.json"
    monkeypatch.setattr(subfetch, "_os_key_bad_marker", lambda key: marker)
    subfetch.mark_os_key_bad("KEY", "403 nope")
    assert subfetch.os_key_bad_cached("KEY") == "403 nope"
    meta = json.loads(marker.read_text(encoding="utf-8"))
    meta["failed_at"] = time.time() - 8 * 3600   # 默认 TTL 6h
    marker.write_text(json.dumps(meta), encoding="utf-8")
    assert subfetch.os_key_bad_cached("KEY") == ""


def test_os_key_ready_prefers_cache_over_network(monkeypatch, tmp_path):
    marker = tmp_path / "marker.json"
    monkeypatch.setattr(subfetch, "_os_key_bad_marker", lambda key: marker)
    calls = []
    monkeypatch.setattr(subfetch, "os_probe_key",
                        lambda key, timeout=12.0: calls.append(key) or "")
    subfetch.mark_os_key_bad("KEY", "no consume")
    assert subfetch.os_key_ready("KEY") == "no consume"
    assert calls == []   # TTL 内命中负缓存，不再探测


NBG_CLEAN = "Nuremberg.2025.2160p.WEB-DL.x264.mkv"


def _args(name, entry=""):
    return argparse.Namespace(name=name, entry=entry, subhd_sid="",
                              subhd_url="", sub_file="")


class TestCoreCoverage:
    """core_coverage 口径：剥发行标签/年份/季集后的片名覆盖率（subhd/subcat 共用）。"""

    def test_strips_year_season_resolution_tags(self):
        assert subfetch.core_tokens("Dune.Part.Two.2024.S05E03.2160p.HEVC.10bit.mkv") \
            == ["dune", "part", "two"]

    def test_cjk_name_halved_but_still_passes(self):
        # 中文名+英文名都在 name 里、候选只有英文：覆盖率 0.5 仍过闸（<0.5 才拒）
        cov = subfetch.core_coverage(
            "纽伦堡.Nuremberg.2025.2160p.WEB-DL.mkv", "Nuremberg.2025.1080p.Remux")
        assert cov == 0.5

    def test_raw_href_scored_zero_documents_slug_need(self):
        # 回归锚：href 原样入参时 / 不分词，整条路径粘成单 token，覆盖率恒 0
        # ——调用方必须先剥 slug（见 TestSubcatCoreCoverage）。
        assert subfetch.core_coverage("Nuremberg.2025.mkv",
                                      "subs/1001/Nuremberg.2025.1080p.html") == 0.0


_SRT_B = ("1" + chr(10) + "00:00:01,000 --> 00:00:02,000" + chr(10) + "hello" + chr(10)).encode()


class TestSubcatCoreCoverage:
    """subcat 兜底闸与 subhd 同口径：core_coverage ≥ 0.5，且候选标题用 slug。"""

    def _patch(self, monkeypatch, entries, langs, calls):
        monkeypatch.setattr(subfetch, "search_entries",
                            lambda q, cap=14: list(entries))
        monkeypatch.setattr(subfetch, "entry_langs", lambda href: dict(langs))

        def fake_curl(url, **_kw):
            calls.append(url)
            return _SRT_B

        monkeypatch.setattr(subfetch, "curl_bytes", fake_curl)

    def test_fallback_accepts_same_movie_noisy_slug(self, monkeypatch):
        href = "subs/1001/Nuremberg.2025.1080p.Remux.AVC.TrueHD.5.1.html"
        calls = []
        self._patch(monkeypatch, [href],
                    {"zh": "/subs/1001/Nuremberg.2025.1080p.Remux-chs.srt"}, calls)
        # title 故意不让过严格轮，强制走 allow_any 兜底闸
        got = subfetch.subcat_fetch(_args(NBG_CLEAN), "zh", ["Nuremberg 2025"],
                                    "不匹配的标题")
        assert got is not None
        cues, label = got
        assert cues and label == "subcat:" + href
        assert calls  # 过了闸就真的去下载

    def test_fallback_rejects_other_movie(self, monkeypatch):
        href = "subs/2002/Oppenheimer.2023.2160p.WEB-DL.html"
        self._patch(monkeypatch, [href],
                    {"zh": "/subs/2002/Oppenheimer.2023-chs.srt"}, [])
        assert subfetch.subcat_fetch(_args(NBG_CLEAN), "zh", ["Nuremberg 2025"],
                                     "不匹配的标题") is None

    def test_direct_entry_skips_search_entirely(self, monkeypatch):
        searched = []
        monkeypatch.setattr(subfetch, "search_entries",
                            lambda q, cap=14: searched.append(q) or [])
        monkeypatch.setattr(subfetch, "entry_langs",
                            lambda href: {"zh": "/subs/1001/Nuremberg.2025-chs.srt"})
        monkeypatch.setattr(subfetch, "curl_bytes", lambda url, **_kw: _SRT_B)
        got = subfetch.subcat_fetch(_args("Nuremberg.2025.mkv",
                                          entry="subs/1001/Nuremberg.2025.html"),
                                    "zh", ["Nuremberg 2025"], "nuremberg")
        assert got is not None and searched == []

    def test_season_mismatch_ranks_below_match(self, monkeypatch):
        # 同剧不同集覆盖率都是 1.0：错集拍在前面也必须被 s05e03 加权压下去
        wrong = "subs/3001/The.Show.2024.S05E04.1080p.WEB-DL.html"
        right = "subs/3002/The.Show.2024.S05E03.1080p.WEB-DL.html"
        calls = []
        self._patch(monkeypatch, [wrong, right], {"zh": "/x-chs.srt"}, calls)
        got = subfetch.subcat_fetch(
            _args("The.Show.2024.S05E03.1080p.WEB-DL.mkv"), "zh",
            ["The Show S05E03"], None)
        assert got is not None
        assert got[1] == "subcat:" + right


# --- v55：年份判据（subliminal guess_matches 口径，os/subhd/subcat 三条腿共用） ---

class TestYearJudgement:
    """同片名重制片（狮子王 1994/2019）core_coverage 同为 1.0，只有年份能分开。"""

    def test_source_year_first_token(self):
        assert subfetch.source_year("纽伦堡.Nuremberg.2025.2160p.WEB-DL.x265") == "2025"
        assert subfetch.source_year("The.Lion.King.2019.1080p.BluRay") == "2019"

    def test_source_year_unquotes_cjk_name(self):
        # Jellyfin 的中文 item 名是百分号编码，不转会整条漏判
        assert subfetch.source_year("%E7%BA%BD%E4%BC%A6%E5%A0%A1.Nuremberg.2025.mkv") == "2025"

    def test_source_year_missing(self):
        assert subfetch.source_year("风味人间.Once.Upon.a.Bite.S05E03") is None
        assert subfetch.source_year("") is None

    def test_cand_year_ignores_resolution_and_bit_depth(self):
        assert subfetch.cand_year("Nuremberg.2025.2160p.10bit.WEB-DL.CHS") == "2025"
        assert subfetch.cand_year("The.Lion.King.1080p.DTS-HD.MA.5.1") is None

    def test_year_delta_penalty_tiers(self):
        assert subfetch.year_delta_penalty("2025", "2025") == 0.0
        assert subfetch.year_delta_penalty("2025", "2026") == 0.1
        assert subfetch.year_delta_penalty("2025", "2027") == 0.6
        assert subfetch.year_delta_penalty("2025", "1994") == 0.6
        # 任一侧缺年份都不罚：未知不等于错误，否则会误伤没写年份的候选
        assert subfetch.year_delta_penalty("2025", None) == 0.0
        assert subfetch.year_delta_penalty(None, "2025") == 0.0

    def test_slug_of_strips_prefix_and_suffix(self):
        assert subfetch._slug_of("subs/1001/Nuremberg.2025.html") == "Nuremberg.2025"
        assert subfetch._slug_of("subs/1001/%E7%BA%BD%E4%BC%A6%E5%A0%A1.html") == "纽伦堡"


def test_subhd_year_mismatch_ranks_below_match(monkeypatch):
    """回归锚：老代码两个候选同分（cov 1.0 + sim 顶格 0.2 + CHS 0.15），

    稳定排序让检索结果里排前面的 1994 版赢——2019 的片拿到 1994 的字幕。
    年份惩罚补上后必须是 2019 版赢。
    """
    src = "The.Lion.King.2019.2160p.UHD.BluRay.x265"
    entries = [("s_old", "The.Lion.King.1994.1080p.BluRay.CHS"),
               ("s_new", "The.Lion.King.2019.2160p.WEB-DL.CHS")]
    monkeypatch.setattr(subfetch, "subhd_entries", lambda q, cap=30: list(entries))

    def fake_cues(sid, url=None):
        return [(100.0, 102.0, "新版2019" if sid == "s_new" else "旧版1994")]

    monkeypatch.setattr(subfetch, "subhd_cues", fake_cues)
    a = argparse.Namespace(name=src, subhd_sid="", subhd_url="", sub_file="")
    got = subfetch.subhd_fetch(a, "zh", ["The Lion King"], "lion")
    assert got and got[0] == [(100.0, 102.0, "新版2019")]


class TestSubcatYearMismatch:
    """subcat 腿同口径：错年份候选必须被压到同年候选之后。"""

    def test_fallback_rejects_year_mismatch(self, monkeypatch):
        old = "subs/4001/The.Lion.King.1994.1080p.BluRay.html"
        new = "subs/4002/The.Lion.King.2019.2160p.WEB-DL.html"
        monkeypatch.setattr(subfetch, "search_entries",
                            lambda q, cap=14: [old, new])
        monkeypatch.setattr(subfetch, "entry_langs",
                            lambda href: {"zh": "/x-chs.srt"})
        monkeypatch.setattr(subfetch, "curl_bytes", lambda url, **_kw: _SRT_B)
        got = subfetch.subcat_fetch(
            _args("The.Lion.King.2019.2160p.UHD.BluRay.x265.mkv"), "zh",
            ["The Lion King"], None)
        assert got is not None
        assert got[1] == "subcat:" + new

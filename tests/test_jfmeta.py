"""dvpack.jfmeta：MediaBrowser 头登录 + 字幕构成四分类的决策逻辑。

真机链路（登录→批量 MediaStreams→审计）另在 2026-10-05 手工验过
（70 部：text 42 / pgs 0 / none 28）；这里只把决策逻辑焊死，全部 mock。
"""

import io
import json
import urllib.error
from unittest import mock

import pytest

from dvpack.jfmeta import (
    JfMetaError,
    auth_header,
    classify_subtitles,
    login,
    media_streams,
    subtitle_languages,
    summarize_subtitles,
    token_from_src,
)


def _sub(codec: str, lang: str = "") -> dict:
    return {"Type": "Subtitle", "Codec": codec, "Language": lang}


class TestAuthHeader:
    def test_device_only(self):
        h = auth_header()
        assert h.startswith("MediaBrowser ")
        assert "Client=\"dvpack\"" in h
        assert "Device=\"Mac\"" in h
        assert "Token=" not in h

    def test_with_token(self):
        assert 'Token="tok123"' in auth_header("tok123")


class TestLogin:
    def _open(self, payload: dict):
        buf = io.BytesIO(json.dumps(payload).encode())
        cm = mock.MagicMock()
        cm.__enter__.return_value = buf
        cm.__exit__.return_value = False
        return cm

    def test_ok(self):
        captured = {}

        def fake_open(req, timeout=None):
            captured["url"] = req.full_url
            captured["body"] = json.loads(req.data)
            captured["auth"] = req.headers["Authorization"]
            return self._open({"AccessToken": "tok-abc", "User": {}})

        with mock.patch("urllib.request.urlopen", side_effect=fake_open):
            tok = login("http://jf:8096", "root", "pw")
        assert tok == "tok-abc"
        assert captured["url"] == "http://jf:8096/Users/AuthenticateByName"
        assert captured["body"] == {"Username": "root", "Pw": "pw"}
        assert "MediaBrowser" in captured["auth"]

    def test_missing_token_raises(self):
        with mock.patch("urllib.request.urlopen",
                        side_effect=lambda req, timeout=None: self._open({"User": {}})):
            with pytest.raises(JfMetaError):
                login("http://jf:8096", "root", "pw")

    def test_network_error_raises(self):
        with mock.patch("urllib.request.urlopen",
                        side_effect=urllib.error.URLError("no route")):
            with pytest.raises(JfMetaError):
                login("http://jf:8096", "root", "pw")


class TestTokenFromSrc:
    def test_present(self):
        assert token_from_src(
            "http://h/Videos/id1/stream.mkv?static=true&api_key=tok9") == "tok9"

    def test_absent(self):
        assert token_from_src("http://h/Videos/id1/stream.mkv?static=true") == ""


class TestClassify:
    def test_text(self):
        assert classify_subtitles([
            {"Type": "Video", "Codec": "hevc"}, _sub("subrip", "eng"),
            _sub("subrip", "zho")]) == "text"

    def test_pgs_only(self):
        assert classify_subtitles([_sub("hdmv_pgs", "eng")]) == "pgs"

    def test_mixed(self):
        assert classify_subtitles([_sub("subrip", "eng"), _sub("hdmv_pgs", "chi")]) == "mixed"

    def test_none(self):
        assert classify_subtitles([{"Type": "Video", "Codec": "hevc"}]) == "none"
        assert classify_subtitles([]) == "none"

    def test_unknown_codec_counts_as_pgs(self):
        # 不认识的 codec 无法 HLS 直写，按图像字幕兜底（宁可走在线下载也不静默丢）
        assert classify_subtitles([_sub("mystery", "eng")]) == "pgs"


class TestLangs:
    def test_normalized(self):
        assert subtitle_languages([
            _sub("subrip", "zho"), _sub("subrip", "zh-hans"),
            _sub("subrip", "eng"), _sub("subrip", "spa")]) == {"zh", "en", "spa"}

    def test_pgs_excluded(self):
        assert subtitle_languages([_sub("hdmv_pgs", "zho"), _sub("hdmv_pgs", "eng")]) == set()


class TestMediaStreams:
    def test_batches_of_20(self):
        calls = []

        def fake_open(req, timeout=None):
            calls.append(req.full_url)
            ids = req.full_url.split("ids=")[1:]
            payload = {"Items": [{"Id": iid, "MediaStreams": [_sub("subrip", "eng")]}
                                 for iid in ids]}
            buf = io.BytesIO(json.dumps(payload).encode())
            cm = mock.MagicMock()
            cm.__enter__.return_value = buf
            cm.__exit__.return_value = False
            return cm

        ids = ["id%d" % i for i in range(25)]
        with mock.patch("urllib.request.urlopen", side_effect=fake_open):
            items = media_streams("http://jf:8096", "tok", ids)
        assert len(calls) == 2  # 20 + 5
        assert len(items) == 25


class TestSummarize:
    def test_counts(self):
        items = [
            {"Id": "a", "Name": "n1", "ProductionYear": 2024,
             "MediaStreams": [_sub("subrip", "zho"), _sub("subrip", "eng")]},
            {"Id": "b", "Name": "n2", "ProductionYear": None,
             "MediaStreams": [_sub("subrip", "eng")]},
            {"Id": "c", "Name": "n3", "ProductionYear": 2021, "MediaStreams": []},
            {"Id": "d", "Name": "n4", "ProductionYear": 2020,
             "MediaStreams": [_sub("hdmv_pgs", "eng")]},
        ]
        s = summarize_subtitles(items)
        assert (s.total, s.text, s.pgs, s.mixed, s.none) == (4, 2, 1, 0, 1)
        assert s.bilingual == 1
        row = {r.item_id: r for r in s.rows}
        assert row["a"].langs == {"zh", "en"}
        assert row["c"].langs == set()
        assert row["d"].kind == "pgs"

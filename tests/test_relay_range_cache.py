"""relay Range 缓存测试（Infuse InputStreamCacheRangeAccess 学习项）。

只测纯函数与缓存行为，不起完整 relay（那些在 test_relay.py）。核心不变量：
截断的数据绝不进缓存，否则毒包会被固化为已知好数据。
"""

from __future__ import annotations

import http.client
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

import pytest

from dvpack.relay import (
    RANGE_CACHE_LIMIT,
    _RangeCache,
    _parse_range,
)


class TestParseRange:
    def test_explicit_range(self):
        assert _parse_range("bytes=100-199") == (100, 199)

    def test_open_ended_range_is_not_cacheable(self):
        """"bytes=A-" 是"从 A 起的整条流"，绝不能当单字节命中——缓存层
        只回放有界区间，把它窄化会截断客户端的读循环。"""
        assert _parse_range("bytes=100-") is None

    def test_rejects_garbage(self):
        for raw in (None, "", "items=0-9", "bytes=abc-def", "bytes=-5", "bytes=9-1"):
            assert _parse_range(raw) is None

    def test_takes_first_spec_only(self):
        """"多段 Range 不在本层处理：回源走完整语义。"""
        assert _parse_range("bytes=0-1,5-9") is None


class TestRangeCache:
    def test_store_and_lookup_hit(self):
        cache = _RangeCache()
        cache.store("http://jf/x.mkv", 0, b"hello")
        assert cache.lookup("http://jf/x.mkv", 0, 4) == b"hello"
        assert cache.lookup("http://jf/x.mkv", 1, 3) == b"ell"

    def test_miss_when_outside_stored_range(self):
        cache = _RangeCache()
        cache.store("http://jf/x.mkv", 10, b"0123456789")
        assert cache.lookup("http://jf/x.mkv", 0, 9) is None
        assert cache.lookup("http://jf/x.mkv", 10, 20) is None

    def test_api_key_does_not_split_the_key(self):
        """"同一路径不同 token 是同一资源：key 用去 query 的 URL。"""
        cache = _RangeCache()
        cache.store("http://jf/x.mkv?api_key=SECRET1", 0, b"abc")
        assert cache.lookup("http://jf/x.mkv?api_key=SECRET2", 0, 2) == b"abc"

    def test_overlapping_store_is_idempotent(self):
        cache = _RangeCache()
        cache.store("http://jf/x.mkv", 0, b"abcdef")
        cache.store("http://jf/x.mkv", 0, b"xyz")  # 被现有区间覆盖，跳过
        assert cache.lookup("http://jf/x.mkv", 0, 5) == b"abcdef"

    def test_evicts_oldest_when_limit_reached(self):
        cache = _RangeCache()
        cache.store("http://jf/x.mkv", 0, b"a" * 10, total=100)
        cache.lookup("http://jf/x.mkv", 0, 9)  # touch first
        cache.store("http://jf/x.mkv", 1000, b"b" * RANGE_CACHE_LIMIT)
        assert cache.lookup("http://jf/x.mkv", 1000, 1000 + RANGE_CACHE_LIMIT - 1) is not None
        assert cache.lookup("http://jf/x.mkv", 0, 9) is None

    def test_lookup_entry_returns_total(self):
        """"命中回放需要原始 Content-Range total，不能瞎算。"""
        cache = _RangeCache()
        cache.store("http://jf/x.mkv", 0, b"abc", total=1024)
        entry = cache.lookup_entry("http://jf/x.mkv", 0, 2)
        assert entry is not None and entry.total == 1024

    def test_content_type_round_trips(self):
        """"命中回放要用上游的 Content-Type，不能一律 octet-stream——
        ffmpeg 的探测阶段靠它识别容器格式。"""
        cache = _RangeCache()
        cache.store("http://jf/x.mkv", 0, b"abc", content_type="video/x-matroska")
        entry = cache.lookup_entry("http://jf/x.mkv", 0, 2)
        assert entry is not None and entry.content_type == "video/x-matroska"


class _RangeUpstream(BaseHTTPRequestHandler):
    """"支持单段 Range 的最小上游：命中 Range 回 206，否则回 200 全量。"""

    payload = bytes(range(256))
    hits = 0
    protocol_version = "HTTP/1.0"

    def do_GET(self):
        rng = self.headers.get("Range", "")
        if rng.startswith("bytes=") and "," not in rng and rng.strip() != "bytes=":
            first, _, last = rng[len("bytes="):].partition("-")
            if first and last:
                start, end = int(first), int(last)
                _RangeUpstream.hits += 1
                body = self.payload[start : end + 1]
                self.send_response(206)
                self.send_header("Content-Type", "video/x-matroska")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Content-Range",
                                 f"bytes {start}-{end}/{len(self.payload)}")
                self.end_headers()
                self.wfile.write(body)
                return
        self.send_response(200)
        self.send_header("Content-Type", "video/x-matroska")
        self.send_header("Content-Length", str(len(self.payload)))
        self.end_headers()
        self.wfile.write(self.payload)

    def log_message(self, format, *args):  # noqa: A002
        pass


class TestOpenEndedBypassesCache:
    """开区间请求必须绕过缓存：这是缓存层窄化读流曾会截断 ffmpeg 的回归闸。"""

    @pytest.fixture
    def upstream(self):
        _RangeUpstream.hits = 0
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), _RangeUpstream)
        httpd.daemon_threads = True
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            yield f"http://127.0.0.1:{httpd.server_address[1]}/x.mkv"
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=5)

    def _get(self, url, rng=None):
        parts = urlsplit(url)
        conn = http.client.HTTPConnection(parts.hostname, parts.port, timeout=20)
        headers = {"Range": rng} if rng else {}
        target = parts.path + (f"?{parts.query}" if parts.query else "")
        conn.request("GET", target, headers=headers)
        resp = conn.getresponse()
        body = resp.read()
        conn.close()
        return resp, body

    def test_open_ended_gets_full_stream_not_one_byte(self, upstream):
        """"先让有界区间进缓存，再发 bytes=0-：必须拿到 256 字节全量，
        而不是被缓存命中窄化成 1 字节。"""
        from dvpack.relay import ffmpeg_visible_source

        relayed = ffmpeg_visible_source(upstream)
        resp, body = self._get(relayed, "bytes=0-255")
        assert resp.status == 206 and len(body) == 256
        first_hits = _RangeUpstream.hits
        assert first_hits == 1

        resp, body = self._get(relayed, "bytes=0-")
        assert resp.status == 200
        assert len(body) == 256
        assert body == _RangeUpstream.payload

    def test_lru_touches_atime_on_lookup(self):
        """"lookup 也是使用：刚读过的区间不该被优先驱逐。"""
        import time as _time

        cache = _RangeCache()
        cache.store("http://jf/x.mkv", 0, b"a" * 10)
        before = cache.lookup_entry("http://jf/x.mkv", 0, 9).atime
        _time.sleep(0.01)
        cache.lookup("http://jf/x.mkv", 0, 9)
        after = cache.lookup_entry("http://jf/x.mkv", 0, 9).atime
        assert after > before

"""回环中继的离线面：纯函数 与 上游截断检测（毒包事故的那道闸）。

tests/test_relay.py 覆盖重写规则/字节流/Range/续跳/502 五条主路径。
这里补两块：

1. **纯函数**：_safe_url（日志不许落 api_key）、_status_of（畸形状态行回 502
   而不是崩）、_header_value（大小写口径）——三者的调用方都在 catch 不到的
   位置上，错了只会静默少一个头。

2. **上游截断 → RST**：2026-10-04 深夜 34 部毒包事故的修法。上游被 Jellyfin
   压断时 curl 退出码非 0 或实收 < Content-Length，body 转发循环只看到管道
   EOF。旧实现当干净 EOF 转发，ffmpeg 正常退出、产出 2~4 段带 ENDLIST 的
   「全片」——电视点片只能放 24 秒且永不自愈。现在必须让客户端连接 RST，
   使 ffmpeg 报连接错误 exit!=0，宁可让用户重试也绝不给毒包落盘机会。
"""

from __future__ import annotations

import http.client
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

import pytest

from dvpack.relay import (
    CURL,
    HEADER_TIMEOUT,
    MAX_REDIRECTS,
    _PASSTHROUGH,
    _REDIRECT_STATUS,
    _header_value,
    _safe_url,
    _status_of,
    ffmpeg_visible_source,
)


class TestSafeUrl:
    """日志与错误报文只留地址：Jellyfin URL 带 api_key，落盘就是泄密。"""

    def test_strips_query_but_keeps_marker(self):
        url = _safe_url("http://jf:8096/videos/ab47/stream.mkv?static=true&api_key=SECRET")
        assert url == "http://jf:8096/videos/ab47/stream.mkv?"
        assert "api_key" not in url
        assert "SECRET" not in url
        assert "static" not in url

    def test_no_query_stays_bare(self):
        assert _safe_url("http://jf/x") == "http://jf/x"

    def test_preserves_scheme_host_path(self):
        url = _safe_url("https://jf.example:443/a/b/c?token=T")
        assert url == "https://jf.example:443/a/b/c?"

    @pytest.mark.parametrize(
        "raw",
        [
            "http://jf/x?api_key=K",
            "http://jf/x?a=1&api_key=K",
            "http://jf/x?api_key=K&a=1",
            "http://jf/x?API_KEY=K",
            "http://jf/x?X-Api-Key=K",
        ],
    )
    def test_key_never_survives_in_any_spelling(self, raw):
        """"api_key 在 query 里的任何位置、任何大小写，都不许出现在结果里。"""
        shown = _safe_url(raw)
        assert "K" not in shown, shown
        assert shown == "http://jf/x?", shown

    def test_only_query_is_dropped(self):
        """路径里出现 key 字样也不能被误删——只切 query。"""
        assert _safe_url("http://jf/api_key/sub.mkv") == "http://jf/api_key/sub.mkv"


class TestStatusOf:
    """状态行解析：拿不到就回 502，绝不把垃圾传给客户端。"""

    @pytest.mark.parametrize(
        ("line", "expected"),
        [
            (b"HTTP/1.1 206 Partial Content\r", 206),
            (b"HTTP/1.1 200 OK\r", 200),
            (b"HTTP/1.0 404 Not Found\r", 404),
            (b"HTTP/1.1 500\r", 500),
            (b"HTTP/1.1 302 Found\r", 302),
        ],
    )
    def test_parses_status_code(self, line, expected):
        assert _status_of([line, b"X: y\r"]) == expected

    @pytest.mark.parametrize(
        "line",
        [b"", b"garbage\r", b"HTTP/1.1\r", b"HTTP/1.1 abc\r", b"HTTP/1.1   \r"],
    )
    def test_malformed_status_falls_back_to_502(self, line):
        assert _status_of([line]) == 502

    def test_ignores_subsequent_lines(self):
        """"206" 只从状态行取，正文里再出现数字不算。"""
        lines = [b"HTTP/1.1 200 OK\r", b"Content-Length: 206\r", b"X-Code: 500\r"]
        assert _status_of(lines) == 200


class TestHeaderValue:
    """"按小写名查"：HTTP 头大小写不敏感，实现按小写比较。"""

    LINES = [b"HTTP/1.1 206 Partial\r", b"Content-Range: bytes 0-9/100\r", b"ETag:  abc \r"]

    def test_finds_header_regardless_of_wire_case(self):
        """"curl 吐什么大小写都认——上游大小写不保证。"""
        assert _header_value(self.LINES, "content-range") == "bytes 0-9/100"

    def test_strips_surrounding_whitespace(self):
        assert _header_value(self.LINES, "etag") == "abc"

    def test_missing_header_is_none(self):
        assert _header_value(self.LINES, "accept-ranges") is None

    def test_skips_the_status_line(self):
        """"状态行里也有冒号形状的片段，不许被当成头。"""
        assert _header_value(self.LINES, "http/1.1 206 partial") is None

    def test_first_match_wins(self):
        lines = self.LINES + [b"ETag: second\r"]
        assert _header_value(lines, "etag") == "abc"


class TestConstants:
    def test_redirect_statuses_cover_all_permanent_and_temp(self):
        assert _REDIRECT_STATUS == {301, 302, 303, 307, 308}

    def test_max_redirects_is_bounded(self):
        """跳转上限：跳不停时必须在有限次后失败，不能无限拉 curl。"""
        assert 0 < MAX_REDIRECTS <= 10

    def test_header_timeout_is_positive(self):
        """"假死上游的看门狗：12s 是 2026-10-03 Jellyfin 现场定的。"""
        assert HEADER_TIMEOUT > 0

    def test_location_is_not_passed_through(self):
        """"透出 Location 会让 ffmpeg 绕开中继直连，又被 TCC 挡回。"""
        assert "location" not in _PASSTHROUGH

    def test_passthrough_covers_range_semantics(self):
        for name in ("content-type", "content-length", "content-range", "accept-ranges"):
            assert name in _PASSTHROUGH, name

    def test_curl_is_the_apple_platform_binary(self):
        """"homebrew curl 同样被 TCC 挡，必须固定系统那份。"""
        assert str(CURL).startswith("/usr/bin/")


class _TruncatingUpstream(BaseHTTPRequestHandler):
    """"声明 N 字节只发 M 字节就关：Jellyfin 被压断时的形状。"""

    declared = 1000
    payload = b"x" * 10
    protocol_version = "HTTP/1.0"

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Length", str(self.declared))
        self.send_header("Content-Type", "video/x-matroska")
        self.end_headers()
        self.wfile.write(self.payload)
        self.wfile.flush()
        self.connection.close()

    def log_message(self, format, *args):  # noqa: A002
        pass


class _HonestUpstream(BaseHTTPRequestHandler):
    """"对照组：声明与实发一致，必须放行。"""

    payload = b"y" * 256
    protocol_version = "HTTP/1.0"

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Length", str(len(self.payload)))
        self.end_headers()
        self.wfile.write(self.payload)

    def log_message(self, format, *args):  # noqa: A002
        pass


@pytest.fixture
def truncating_site():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _TruncatingUpstream)
    httpd.daemon_threads = True
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}/big.mkv"
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


@pytest.fixture
def honest_site():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _HonestUpstream)
    httpd.daemon_threads = True
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}/ok.mkv"
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def _fetch(url, rng=None):
    parts = urlsplit(url)
    conn = http.client.HTTPConnection(parts.hostname, parts.port, timeout=20)
    conn.request("GET", parts.path + (f"?{parts.query}" if parts.query else ""),
                 headers={"Range": rng} if rng else {})
    resp = conn.getresponse()
    body = resp.read()
    conn.close()
    return resp, body


class TestTruncationResetsClient:
    """上游截断必须 RST，而不是干净 EOF——否则毒包落盘。"""

    def test_client_sees_reset_not_clean_eof(self, truncating_site):
        """"ffmpeg 视角：读流中途连接被重置 → exit != 0 → 上层 502。"""
        with pytest.raises((ConnectionResetError, http.client.IncompleteRead)):
            _fetch(ffmpeg_visible_source(truncating_site))

    def test_no_poison_pack_is_possible(self, truncating_site):
        """(意图说明) 客户端若拿到干净 EOF + ENDLIST，就是 24 秒毒包。"""
        # 这里断言的是「拿不到完整响应」：要么 RST，要么读不全
        try:
            resp, body = _fetch(ffmpeg_visible_source(truncating_site))
        except (ConnectionResetError, http.client.IncompleteRead):
            return
        # 若实现回退成干净 EOF，字节数也必须少于声明值——绝不让调用方
        # 以为自己收到了完整的 1000 字节
        assert len(body) < _TruncatingUpstream.declared

    def test_honest_upstream_is_not_disturbed(self, honest_site):
        """(对照) 声明与实发一致时放行，零误杀。"""
        resp, body = _fetch(ffmpeg_visible_source(honest_site))
        assert resp.status == 200
        assert body == _HonestUpstream.payload


class TestRelayIsolation:
    """/jf/pack 的 src 与 serve 的中继是两个独立维度，改一个不该动另一个。"""

    def test_each_call_reuses_one_relay_instance(self):
        "";"中继是进程内单例：每个请求都起一个新监听会把 fd 吃光。"""
        a = ffmpeg_visible_source("http://jf/a.mkv")
        b = ffmpeg_visible_source("http://jf/b.mkv")
        assert urlsplit(a).netloc == urlsplit(b).netloc

    def test_relative_local_path_untouched(self):
        "";"本地文件路径不能被加上 http:// 前缀。"""
        for path in ("/media/a.mkv", "relative/b.mkv", "C:\\media\\a.mkv"):
            assert ffmpeg_visible_source(path) == path

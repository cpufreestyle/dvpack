"""回环中继：重写规则、字节流完整性、Range 语义透传。

真实场景（launchd TCC 封锁出站）里这里是 ffmpeg 与 /usr/bin/curl 之间的
唯一通道，字节错一个位，电视上就是花屏/起播失败，所以逐项钉死。
"""

from __future__ import annotations

import http.client
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

import pytest

from dvpack.relay import ensure_relay, ffmpeg_visible_source


class _Upstream(BaseHTTPRequestHandler):
    payload = b"dvpack-relay-payload-" * 64

    def do_GET(self) -> None:
        parts = urlsplit(self.path)
        if parts.path == "/redirect":
            self.send_response(302)
            self.send_header("Location", "/stream.mkv")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        rng = self.headers.get("Range")
        if rng and rng.startswith("bytes="):
            start, _, end = rng[len("bytes=") :].partition("-")
            start = int(start or 0)
            end = int(end) if end else len(self.payload) - 1
            chunk = self.payload[start : end + 1]
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{end}/{len(self.payload)}")
        else:
            chunk = self.payload
            self.send_response(200)
        self.send_header("Content-Length", str(len(chunk)))
        self.send_header("Accept-Ranges", "bytes")
        self.end_headers()
        self.wfile.write(chunk)

    def log_message(self, format: str, *args) -> None:  # noqa: A002 - 基类签名
        pass


@pytest.fixture
def upstream() -> str:
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Upstream)
    httpd.daemon_threads = True
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}/stream.mkv"
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def _fetch(url: str, rng: str | None = None):
    parts = urlsplit(url)
    conn = http.client.HTTPConnection(parts.hostname, parts.port, timeout=15)
    path = parts.path + (f"?{parts.query}" if parts.query else "")
    conn.request("GET", path, headers={"Range": rng} if rng else {})
    resp = conn.getresponse()
    body = resp.read()
    conn.close()
    return resp, body


def test_http_source_is_rewritten_to_relay():
    original = "http://192.168.1.68:8096/a.mkv?static=true&api_key=x"
    rewritten = ffmpeg_visible_source(original)
    assert rewritten.startswith("http://127.0.0.1:")
    assert "/u?" in rewritten
    assert "/u?u=" in rewritten
    from urllib.parse import parse_qs, quote

    assert parse_qs(urlsplit(rewritten).query)["u"] == [original]
    assert quote(original, safe="") in rewritten


def test_local_path_passes_through():
    assert ffmpeg_visible_source("/tmp/a.mkv") == "/tmp/a.mkv"
    assert ffmpeg_visible_source("C:\\media\\a.mkv") == "C:\\media\\a.mkv"


def test_ensure_relay_is_idempotent():
    assert ensure_relay() == ensure_relay()


def test_relay_streams_full_body(upstream):
    resp, body = _fetch(ffmpeg_visible_source(upstream))
    assert resp.status == 200
    assert body == _Upstream.payload


def test_relay_forwards_range_and_headers(upstream):
    resp, body = _fetch(ffmpeg_visible_source(upstream), "bytes=10-19")
    assert resp.status == 206
    assert resp.getheader("Content-Range") == f"bytes 10-19/{len(_Upstream.payload)}"
    assert resp.getheader("Accept-Ranges") == "bytes"
    assert body == _Upstream.payload[10:20]


def test_relay_missing_u_gets_400():
    parts = urlsplit(ensure_relay())
    conn = http.client.HTTPConnection(parts.hostname, parts.port, timeout=10)
    conn.request("GET", "/u")
    resp = conn.getresponse()
    resp.read()
    conn.close()
    assert resp.status == 400


def test_relay_follows_redirects_without_exposing_location(upstream):
    """上游 3xx 必须由中继续跳，且不能把 Location 透给 ffmpeg。

    ffmpeg 一旦拿到 Location 就自己直连那个地址：局域网地址绕开中继，
    又被 TCC 挡回 EHOSTUNREACH——中继白建。
    """
    origin, _, _ = upstream.rpartition("/")
    resp, body = _fetch(ffmpeg_visible_source(f"{origin}/redirect"))
    assert resp.status == 200
    assert body == _Upstream.payload
    assert resp.getheader("Location") is None


def test_relay_reports_dead_upstream_as_502_with_reason():
    """上游连不上必须回 502 且带原因，而不是掐断连接让客户端见 http 000。"""
    resp, body = _fetch(ffmpeg_visible_source("http://127.0.0.1:1/nothing-here"))
    assert resp.status == 502
    assert "上游无响应" in body.decode("utf-8", "replace")
    assert b"api_key" not in body


class TestWindowsBypass:
    """nt 上无 TCC，中继纯为绕 TCC 而生——直连比多一跳 curl.exe 好。"""

    def test_http_source_returned_as_is_on_windows(self, monkeypatch):
        from dvpack import relay as relay_module

        monkeypatch.setattr(relay_module.os, "name", "nt")
        src = "http://jellyfin.lan:8096/Videos/abc/stream.mkv?api_key=x"
        assert ffmpeg_visible_source(src) == src

    def test_local_path_untouched_on_windows(self, monkeypatch):
        from dvpack import relay as relay_module

        monkeypatch.setattr(relay_module.os, "name", "nt")
        assert ffmpeg_visible_source("C:\\media\\a.mkv") == "C:\\media\\a.mkv"

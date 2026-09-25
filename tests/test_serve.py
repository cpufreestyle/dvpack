"""HLS 服务测试：Range 的边界用真 HTTP 请求打，不只测纯函数。

AVPlayer 起播就是靠 `Range: bytes=0-1` 探 init 段，之后按区间取分片；
这里返错状态码或算错 Content-Range，端上的表现是"转圈不报错"，最难查。
"""

from __future__ import annotations

import http.client
import ipaddress
import os
from pathlib import Path

import pytest
from dvpack import serve as serve_module
from dvpack.serve import (
    FULL,
    PARTIAL,
    UNSATISFIABLE,
    ByteRange,
    _is_private_ipv4,
    lan_address,
    parse_range,
    serve,
    stream_file,
)


@pytest.mark.parametrize(
    ("header", "length", "expected"),
    [
        (None, 10, (FULL, None)),
        ("", 10, (FULL, None)),
        ("bytes=0-4", 10, (PARTIAL, ByteRange(0, 4))),
        ("bytes=5-", 10, (PARTIAL, ByteRange(5, 9))),
        ("bytes=-3", 10, (PARTIAL, ByteRange(7, 9))),
        ("bytes=0-9999", 10, (PARTIAL, ByteRange(0, 9))),  # 超出部分截断，不报错
        ("BYTES=0-4", 10, (PARTIAL, ByteRange(0, 4))),  # unit 大小写放宽：认
        ("bytes=0-1,3-4", 10, (FULL, None)),  # 多区间：忽略 Range 发整个响应
        ("bytes=8-2", 10, (UNSATISFIABLE, None)),
        ("bytes=99-", 10, (UNSATISFIABLE, None)),
        ("bytes=abc-def", 10, (FULL, None)),
        ("bytes=", 10, (FULL, None)),
        ("bytes=-0", 10, (FULL, None)),
        ("bytes=0-9", 0, (FULL, None)),  # 空文件无从切起
    ],
)
def test_parse_range(header, length, expected):
    assert parse_range(header, length) == expected


def test_byte_range_length_is_inclusive():
    assert ByteRange(3, 5).length == 3


@pytest.fixture
def site(tmp_path: Path):
    root = tmp_path / "public"
    root.mkdir()
    (root / "master.m3u8").write_bytes(b"#EXTM3U\nvideo.m3u8\n")
    (root / "s0.m4s").write_bytes(bytes(range(256)) * 4)  # 1024 字节，内容可寻址
    (tmp_path / "outside.txt").write_text("不该被读到", encoding="utf-8")  # 在服务根目录之外
    httpd = serve(root, 0)
    httpd.daemon_threads = True  # 别让残留的 keep-alive 线程活到解释器退出
    thread = _serve_in_thread(httpd)
    try:
        yield httpd.server_address[1]
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def _serve_in_thread(httpd):
    import threading

    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    return thread


def _get(port: int, path: str, headers: dict | None = None):
    # 显式要求关闭连接：否则处理线程会停在 keep-alive 的 recv 上，
    # 解释器退出时被强杀（实测偶发 0xC0000409 硬崩，而不是正常结束）。
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request("GET", path, headers={"Connection": "close", **(headers or {})})
    resp = conn.getresponse()
    body = resp.read()
    conn.close()
    return resp, body


def test_playlist_mime_and_no_store(site):
    resp, body = _get(site, "/master.m3u8")
    assert resp.status == 200
    assert resp.getheader("Content-Type") == "application/vnd.apple.mpegurl"
    assert resp.getheader("Cache-Control") == "no-store"
    assert body == b"#EXTM3U\nvideo.m3u8\n"


def test_segment_range_returns_206_with_exact_slice(site):
    resp, body = _get(site, "/s0.m4s", {"Range": "bytes=100-199"})
    assert resp.status == 206
    assert resp.getheader("Content-Range") == "bytes 100-199/1024"
    assert resp.getheader("Content-Length") == "100"
    assert body == bytes(range(256))[100:200]
    assert resp.getheader("Accept-Ranges") == "bytes"


def test_open_ended_range_streams_to_eof(site):
    resp, body = _get(site, "/s0.m4s", {"Range": "bytes=1023-"})
    assert resp.status == 206
    assert body == bytes([255])


def test_unsatisfiable_range_returns_416(site):
    resp, _ = _get(site, "/s0.m4s", {"Range": "bytes=5000-6000"})
    assert resp.status == 416
    assert resp.getheader("Content-Range") == "bytes */1024"


def test_multi_range_request_gets_whole_file(site):
    resp, body = _get(site, "/s0.m4s", {"Range": "bytes=0-9,20-29"})
    assert resp.status == 200
    assert len(body) == 1024
    assert resp.getheader("Content-Range") is None


def test_head_has_length_but_no_body(site):
    conn = http.client.HTTPConnection("127.0.0.1", site, timeout=5)
    conn.request("HEAD", "/s0.m4s", headers={"Connection": "close"})
    resp = conn.getresponse()
    assert resp.status == 200
    assert resp.getheader("Content-Length") == "1024"
    assert resp.read() == b""
    conn.close()


def test_missing_file_is_404(site):
    resp, _ = _get(site, "/nope.m4s")
    assert resp.status == 404


def test_traversal_cannot_escape_root(site):
    """`..` 会被 stdlib 丢掉，所以逃不出服务根目录——每个变体都要 404。"""
    for path in ("/../outside.txt", "/%2e%2e/outside.txt", "/public/../outside.txt"):
        resp, body = _get(site, path)
        assert resp.status == 404, path
        assert "不该被读到" not in body.decode("utf-8", "replace")


def test_serve_rejects_missing_root(tmp_path: Path):
    with pytest.raises(FileNotFoundError):
        serve(tmp_path / "没有这个目录")


class _FlakySink:
    """写完 fail_after 字节就开始抛 ConnectionResetError，模拟客户端半路断开。"""

    def __init__(self, fail_after: int):
        self.fail_after = fail_after
        self.bytes_written = 0

    def write(self, block: bytes) -> None:
        if self.bytes_written >= self.fail_after:
            raise ConnectionResetError(10054, "远程主机强迫关闭了一个现有的连接")
        self.bytes_written += len(block)


def test_client_abort_during_transfer_is_not_an_error(tmp_path: Path):
    """AVPlayer/ffprobe 会发起推测性请求再断开；这不该变成服务端异常。"""
    blob = tmp_path / "big.m4s"
    blob.write_bytes(b"\0" * 4096)
    sink = _FlakySink(2048)
    with blob.open("rb") as handle:
        sent = stream_file(handle, sink, 4096, chunk=1024)
    assert (sink.bytes_written, sent) == (2048, 2048)


def test_client_that_resets_on_first_byte_still_returns_quietly(tmp_path: Path):
    blob = tmp_path / "big.m4s"
    blob.write_bytes(b"\0" * 4096)
    with blob.open("rb") as handle:
        assert stream_file(handle, _FlakySink(0), 4096, chunk=1024) == 0


def _patch_ifaces(monkeypatch, addresses):
    """按当前平台 patch 对应的枚举函数，让用例与操作系统无关。"""
    name = "_windows_iface_addresses" if os.name == "nt" else "_posix_iface_addresses"
    monkeypatch.setattr(serve_module, name, lambda: addresses)


@pytest.mark.parametrize(
    ("address", "private"),
    [
        ("192.168.1.109", True),
        ("192.168.255.255", True),
        ("10.8.0.2", True),
        ("172.16.0.1", True),
        ("172.31.255.255", True),
        ("172.15.0.1", False),
        ("172.32.0.1", False),
        ("198.18.0.1", False),  # 基准测试段：TUN 代理的默认网卡地址，不能当 LAN
        ("127.0.0.1", False),
        ("169.254.1.1", False),
        ("8.8.8.8", False),
        ("192.168.1", False),
        ("256.0.0.1", False),
        ("junk", False),
    ],
)
def test_private_ipv4(address, private):
    """私网判定要排掉 198.18/15——标准库的 ipaddress 把它算 private，正好是坑。"""
    assert _is_private_ipv4(address) is private


def test_lan_address_prefers_192_segment(monkeypatch):
    """家用路由器绝大多数在 192.168 段，候选里有时它必须赢。"""
    _patch_ifaces(monkeypatch, ["198.18.0.1", "10.8.0.2", "172.16.3.4", "192.168.1.109"])
    assert lan_address() == "192.168.1.109"


def test_lan_address_prefers_172_over_10(monkeypatch):
    """没有 192.168 时按 172.16/12 → 10/8 的顺序取，保持跨机确定性。"""
    _patch_ifaces(monkeypatch, ["10.1.2.3", "172.20.1.1"])
    assert lan_address() == "172.20.1.1"


def test_lan_address_falls_back_to_default_route(monkeypatch):
    """枚举一无所获（极简容器常见）时退回 UDP connect 探测，保住老行为。"""
    _patch_ifaces(monkeypatch, [])
    monkeypatch.setattr(serve_module, "_default_route_address", lambda: "192.168.1.109")
    assert lan_address() == "192.168.1.109"


def test_lan_address_last_resort_is_loopback(monkeypatch):
    _patch_ifaces(monkeypatch, [])
    monkeypatch.setattr(serve_module, "_default_route_address", lambda: None)
    assert lan_address() == "127.0.0.1"


def test_lan_address_on_this_host():
    """本机真跑一遍枚举：必须返回一个可解析的 IPv4 字面量。"""
    ipaddress.ip_address(lan_address())

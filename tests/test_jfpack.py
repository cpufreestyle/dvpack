"""jfpack 与 /jf/pack 端点：参数校验、缓存、302 路由，外加真片源 E2E。

端点参数错了，电视上的表现只有"转圈"或黑屏，服务端日志里留一个 400——
所以 src 私网白名单、id 白名单、seconds 夹取都在这里钉死，不许悄悄回退。
真片源 E2E 与 test_remux 共用 DVPACK_SOURCE_MKV，没有就跳过。
"""

from __future__ import annotations

import http.client
import json
import os
import socket
from pathlib import Path

import pytest
from dvpack import serve as serve_module
from dvpack.jfpack import pack_source
from dvpack.serve import ID_PATTERN, _cache_fresh, _src_fingerprint, _valid_pack_src, serve
from dvpack.tools import FFMPEG

SRC = Path(os.environ.get("DVPACK_SOURCE_MKV", ""))

needs_source = pytest.mark.skipif(
    not (SRC.is_file() and FFMPEG.exists()), reason="缺片源或 ffmpeg"
)


def _serve_in_thread(httpd):
    import threading

    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    return thread


def _get(port: int, path: str):
    # 显式 Connection: close：否则处理线程停在 keep-alive recv 上，
    # 解释器退出时被强杀而不是正常结束（test_serve.py 同款教训）。
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request("GET", path, headers={"Connection": "close"})
    resp = conn.getresponse()
    body = resp.read()
    conn.close()
    return resp, body


def _patch_resolver(monkeypatch, addresses):
    """钉死 getaddrinfo：这些用例不许真连 DNS。"""

    def resolver(host, port, proto=0):
        if not addresses:
            raise socket.gaierror("测试环境没有 DNS")
        return [
            (socket.AF_INET, socket.SOCK_STREAM, proto, "", (addr, port))
            for addr in addresses
        ]

    monkeypatch.setattr(serve_module.socket, "getaddrinfo", resolver)


def test_private_http_source_passes():
    """字面量私网 IP 不碰 DNS，端口带不带都放行。"""
    assert _valid_pack_src("http://192.168.1.68:8096/Videos/a/stream.mkv?api_key=k")
    assert _valid_pack_src("http://192.168.1.68/stream.mkv")
    assert _valid_pack_src("https://10.0.0.2/stream.mkv")


def test_public_or_non_http_source_is_rejected(monkeypatch):
    """serve 听 0.0.0.0：公网、非 http、无主机名一律挡掉。"""
    _patch_resolver(monkeypatch, ["8.8.8.8"])
    assert not _valid_pack_src("http://8.8.8.8/stream.mkv")
    _patch_resolver(monkeypatch, ["192.168.1.68"])
    assert not _valid_pack_src("ftp://192.168.1.68/stream.mkv")
    assert not _valid_pack_src("file:///etc/passwd")
    assert not _valid_pack_src("")
    assert not _valid_pack_src("http:///stream.mkv")  # 没有主机名


def test_dns_pointing_public_is_rejected_even_with_private_name(monkeypatch):
    """域名解析出公网地址（含一个私网一个公网的混合）都拒。"""
    _patch_resolver(monkeypatch, ["192.168.1.68", "8.8.8.8"])
    assert not _valid_pack_src("http://mixed.example.com/x")
    _patch_resolver(monkeypatch, [])  # 解析失败
    assert not _valid_pack_src("http://nope.example.com/x")


@pytest.mark.parametrize(
    ("item_id", "matches"),
    [
        ("534f5cb2ba7f9d500f9f2154e8cff079", True),
        ("e01", True),
        ("with-dash_and.dot", True),
        ("", False),
        ("../../etc/passwd", False),
        ("a b", False),
        ("x" * 65, False),
        ("dir/child", False),
    ],
)
def test_item_id_pattern(item_id, matches):
    assert bool(ID_PATTERN.match(item_id)) is matches


def test_fingerprint_drops_api_key_only():
    """api_key 是会过期的秘密，不落盘；其它查询参数要原样保留。"""
    src = "http://192.168.1.68:8096/Videos/a/stream.mkv?static=true&api_key=secret&MediaSourceId=x"
    fp = _src_fingerprint(src)
    assert "api_key" not in fp
    assert "secret" not in fp
    assert fp == "http://192.168.1.68:8096/Videos/a/stream.mkv?static=true&MediaSourceId=x"


def test_fingerprint_without_query_is_unchanged():
    assert _src_fingerprint("http://192.168.1.68/stream.mkv") == "http://192.168.1.68/stream.mkv"


@pytest.fixture
def pack_site(tmp_path: Path, monkeypatch):
    """起一个真 HTTP 服务，pack_source 换成记录器——只测路由，不动 ffmpeg。"""
    root = tmp_path / "public"
    root.mkdir()
    calls = []

    def fake_pack(src, out_dir, *, seconds=60.0, master_uri="video.m3u8", **kwargs):
        calls.append(
            {
                "src": src,
                "out_dir": Path(out_dir),
                "seconds": seconds,
                "master_uri": master_uri,
            }
        )
        out = Path(out_dir)
        out.mkdir(parents=True, exist_ok=True)
        (out / "master.m3u8").write_text(f"#EXTM3U\n{master_uri}\n", encoding="utf-8")

    monkeypatch.setattr(serve_module, "pack_source", fake_pack)
    httpd = serve(root, 0)
    httpd.daemon_threads = True
    thread = _serve_in_thread(httpd)
    try:
        yield httpd.server_address[1], root, calls
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


PRIVATE_SRC = "http://192.168.1.68:8096/Videos/a/stream.mkv?static=true&api_key=secret"


def test_public_src_gets_400_before_any_packing(pack_site):
    port, _, calls = pack_site
    resp, _ = _get(port, "/jf/pack?src=http://8.8.8.8/stream.mkv&id=e01")
    assert resp.status == 400
    assert calls == []


def test_bad_item_id_gets_400(pack_site):
    port, _, calls = pack_site
    # ".." / "." 过得 ID_PATTERN，但会把 out_dir 变成静态根目录本身
    for bad in ("", "..", ".", "...", "%2e%2e", "dir%2fchild"):
        resp, _ = _get(port, f"/jf/pack?src={PRIVATE_SRC}&id={bad}")
        assert resp.status == 400, bad
    assert calls == []


def test_bad_seconds_gets_400(pack_site):
    port, _, calls = pack_site
    resp, _ = _get(port, f"/jf/pack?src={PRIVATE_SRC}&id=e01&seconds=abc")
    assert resp.status == 400
    assert calls == []


def test_pack_redirects_to_absolute_master(pack_site):
    port, root, calls = pack_site
    resp, _ = _get(port, f"/jf/pack?src={PRIVATE_SRC}&id=e01&seconds=5")
    assert resp.status == 302
    assert resp.getheader("Location") == "/_jf/e01/master.m3u8"
    assert len(calls) == 1
    assert calls[0]["seconds"] == 5.0
    # master 变体 URI 必须是绝对路径，否则重定向后相对 URI 解析错位置
    assert calls[0]["master_uri"] == "/_jf/e01/video.m3u8"
    followed, body = _get(port, resp.getheader("Location"))
    assert followed.status == 200
    assert "/_jf/e01/video.m3u8" in body.decode("utf-8")


@pytest.mark.parametrize(
    ("requested", "expected"),
    [
        ("0", 1.0),  # 下限
        ("-5", 1.0),
        ("99999", 600.0),  # 上限
        ("30", 30.0),  # 区间内原样
    ],
)
def test_seconds_are_clamped(pack_site, requested, expected):
    port, _, calls = pack_site
    resp, _ = _get(port, f"/jf/pack?src={PRIVATE_SRC}&id=e01&seconds={requested}")
    assert resp.status == 302
    assert calls[-1]["seconds"] == expected


def test_fingerprint_file_has_no_api_key(pack_site):
    port, root, calls = pack_site
    resp, _ = _get(port, f"/jf/pack?src={PRIVATE_SRC}&id=e01&seconds=6")
    assert resp.status == 302
    marker = root / "_jf" / "e01" / "source.json"
    meta = json.loads(marker.read_text(encoding="utf-8"))
    dumped = json.dumps(meta)
    assert "api_key" not in dumped
    assert "secret" not in dumped
    assert meta["seconds"] == 6.0


def test_same_source_and_seconds_hits_cache(pack_site):
    """电视重试、点完再点：同一个 item 同一参数不重复打包。"""
    port, _, calls = pack_site
    for _ in range(3):
        resp, _ = _get(port, f"/jf/pack?src={PRIVATE_SRC}&id=e01&seconds=6")
        assert resp.status == 302
    assert len(calls) == 1
    # seconds 变了（换时长）必须重新打
    resp, _ = _get(port, f"/jf/pack?src={PRIVATE_SRC}&id=e01&seconds=7")
    assert resp.status == 302
    assert len(calls) == 2


def test_cache_fresh_needs_marker_and_matching_fingerprint(tmp_path: Path):
    out = tmp_path / "pack"
    out.mkdir()
    assert _cache_fresh(out, PRIVATE_SRC, 6) is False  # 什么都没有
    (out / "master.m3u8").write_text("#EXTM3U\n", encoding="utf-8")
    (out / "source.json").write_text(
        json.dumps({"src": _src_fingerprint(PRIVATE_SRC), "seconds": 6}),
        encoding="utf-8",
    )
    assert _cache_fresh(out, PRIVATE_SRC, 6) is True
    assert _cache_fresh(out, PRIVATE_SRC, 7) is False
    assert _cache_fresh(out, "http://192.168.1.68/other.mkv", 6) is False


@needs_source
def test_pack_source_end_to_end(tmp_path: Path):
    """真片源：路径进，完整 HLS 出，verify 自己会查 dvcC 与 RPU 一致性。"""
    result = pack_source(str(SRC), tmp_path / "out", seconds=6, segment_seconds=6.0)
    assert result.verification.ok, result.verification.problems
    assert "dvcC" in result.verification.signalling
    assert result.master.is_file()
    assert result.output.init.is_file() and result.output.segments

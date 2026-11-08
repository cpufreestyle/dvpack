"""HLS 服务测试：Range 的边界用真 HTTP 请求打，不只测纯函数。

AVPlayer 起播就是靠 `Range: bytes=0-1` 探 init 段，之后按区间取分片；
这里返错状态码或算错 Content-Range，端上的表现是"转圈不报错"，最难查。
"""

from __future__ import annotations

import http.client
import ipaddress
import errno
import json
import os
import time
import subprocess
import sys
from types import SimpleNamespace
from pathlib import Path

import pytest
from dvpack import serve as serve_module
from dvpack.playlist import Variant, write_master_playlist
from dvpack.serve import (
    FULL,
    PARTIAL,
    UNSATISFIABLE,
    ByteRange,
    _is_private_ipv4,
    lan_address,
    _open_ended_range,
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


def test_open_ended_range_detection():
    """只有「bytes=X-」这种不带上界的才算开区间；有界区间一律不算。"""
    assert _open_ended_range("bytes=100-")
    assert _open_ended_range("bytes=0-")
    assert not _open_ended_range("bytes=100-500")
    assert not _open_ended_range("bytes=-500")  # 末尾 N 字节是有界的
    assert not _open_ended_range(None)
    assert not _open_ended_range("")


def test_open_ended_range_is_capped(monkeypatch):
    """开区间请求被截成有界段，Content-Range 仍如实描述真正发了多少。"""
    monkeypatch.setattr(serve_module, "MAX_OPEN_RANGE_SEND", 2048)
    length = 10 * 1024 * 1024
    verdict, rng = parse_range("bytes=1024-", length)
    assert verdict == PARTIAL
    assert rng == ByteRange(1024, length - 1)
    capped_end = rng.start + serve_module.MAX_OPEN_RANGE_SEND - 1
    assert capped_end == 1024 + 2047
    # 截断后的区间长度正好是上限，且不越界
    assert ByteRange(rng.start, capped_end).length == 2048
    assert capped_end < length


def test_open_ended_range_cap_disabled_by_zero(monkeypatch):
    """上限设 0 时照单全收（恢复旧行为）。

    截断闸门是 `MAX_OPEN_RANGE_SEND > 0`：0 时 capped_end 会退化成
    start-1（比 start 还小），靠这道闸门挡住，而不是靠数值比较。
    """
    monkeypatch.setattr(serve_module, "MAX_OPEN_RANGE_SEND", 0)
    length = 10 * 1024 * 1024
    _verdict, rng = parse_range("bytes=1024-", length)
    assert not (serve_module.MAX_OPEN_RANGE_SEND > 0)
    assert rng == ByteRange(1024, length - 1)  # 原样照单全收


def test_bounded_range_is_never_capped():
    """有界区间是客户端点名要的确切字节，判据上就不该被截。"""
    assert not _open_ended_range("bytes=1024-9999999")
    assert not _open_ended_range("bytes=-2048")


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


@pytest.fixture
def jfsite(tmp_path: Path):
    """带 _jf pack 缓存的服务根：删一部片要连它的缓存一起删。"""
    root = tmp_path / "public"
    root.mkdir()
    (root / "master.m3u8").write_bytes(b"#EXTM3U\n")
    cache = root / "_jf"
    victim = cache / "ab476f22b8fe82ec91a77886310c06fc"
    victim.mkdir(parents=True)
    (victim / "video.m3u8").write_bytes(b"#EXTM3U\n0.m4s\n")
    (victim / "0.m4s").write_bytes(b"x" * 4096)
    keep = cache / "ff951e2e8ce84efcf5a7bcb7de918d8f"
    keep.mkdir(parents=True)
    (keep / "video.m3u8").write_bytes(b"#EXTM3U\n")
    httpd = serve(root, 0)
    httpd.daemon_threads = True
    thread = _serve_in_thread(httpd)
    try:
        yield httpd.server_address[1], victim, keep
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def test_purge_deletes_pack_cache_and_reports_freed_bytes(jfsite):
    port, victim, keep = jfsite
    resp, body = _get(port, "/jf/purge?id=ab476f22b8fe82ec91a77886310c06fc")
    assert resp.status == 200
    payload = json.loads(body)
    assert payload["id"] == "ab476f22b8fe82ec91a77886310c06fc"
    assert payload["existed"] is True
    assert payload["freed_bytes"] > 4096
    assert not victim.exists()
    assert keep.is_dir()  # 只删指定的那一部，别把别人的缓存顺手带走


def test_purge_unknown_item_is_a_noop(jfsite):
    port, _, keep = jfsite
    resp, body = _get(port, "/jf/purge?id=00000000000000000000000000000000")
    assert resp.status == 200
    assert json.loads(body)["existed"] is False
    assert keep.is_dir()


@pytest.mark.parametrize("bad", ["", ".", "..", "...", "a/../b", "x" * 65])
def test_purge_rejects_bad_id(jfsite, bad):
    "" ".. 能穿过 ID_PATTERN：不拦住就是 rmtree 整个服务根目录。" ""
    port, _, keep = jfsite
    resp, _ = _get(port, "/jf/purge?id=" + bad)
    assert resp.status == 400
    assert keep.is_dir()
    assert (keep / "video.m3u8").exists()


# ── 在线字幕腿（_online_subs_body）：只由后台线程跑，永不进 302 热路径 ───
_SRC = "http://192.168.1.68:8096/videos/534f/stream.mkv?api_key=secret"
_TEXTS = {
    "zh": "1\n00:00:01,000 --> 00:00:03,000\n世界\n",
    "en": "1\n00:00:01,000 --> 00:00:03,000\nWorld\n",
}


def _fake_pack(out_dir: Path) -> None:
    variant = Variant(
        uri="/_jf/534f/video.m3u8",
        bandwidth=12_000_000,
        codecs="hvc1.2.4.L153.B0,ec-3",
        video_range="PQ",
        resolution=(3840, 2160),
        frame_rate=24.0,
        hdcp_level="TYPE-1",
        supplemental_codecs="dvh1.05.06/db1p",
    )
    write_master_playlist(out_dir / "master.m3u8", [variant])


def test_subtitle_texts_hot_path_never_goes_online(tmp_path: Path, monkeypatch):
    """点片热路径只做本地两级解析；在线抓取分钟级，绝不能堵在 302 前。"""
    from dvpack import jfpack, relay, subsource

    monkeypatch.setattr(relay, "ffmpeg_visible_source", lambda src: src)
    monkeypatch.setattr(jfpack, "fetch_subtitle_texts", lambda src, info: {})

    def _boom(**_kwargs):
        raise AssertionError("在线腿不许进热路径")

    monkeypatch.setattr(subsource, "fetch_online_texts", _boom)
    resolution = serve_module._subtitle_texts(_SRC, root=tmp_path, info=object())
    assert resolution.texts == {}


def test_online_subs_body_fetches_caches_and_upgrades(tmp_path: Path, monkeypatch):
    """抓到字幕：落 _subs 缓存 + 给已在播的包只换 master 字幕轨。"""
    from dvpack import subsource

    out_dir = tmp_path / "pack"
    out_dir.mkdir()
    _fake_pack(out_dir)
    monkeypatch.setattr(serve_module, "_jf_item_name", lambda src, item_id: "奎迪2")
    monkeypatch.setattr(subsource, "fetch_online_texts",
                        lambda **kw: (dict(_TEXTS), "subhd rc=0"))
    receipt = serve_module._online_subs_body(tmp_path, _SRC, "534f", out_dir)
    assert "zh+en" in receipt
    cache_dir = subsource.subtitle_cache_dir(
        tmp_path, serve_module._src_fingerprint(_SRC))
    cached, meta = subsource.load_cached_texts(cache_dir)
    assert set(cached) == {"zh", "en"}
    assert meta["source"] == subsource.ONLINE
    assert not subsource.cache_blocked(cache_dir)
    master = (out_dir / "master.m3u8").read_text(encoding="utf-8")
    assert 'TYPE=SUBTITLES' in master and 'URI="sub_zh.m3u8"' in master


def test_online_subs_body_uses_passed_name_without_lookup(tmp_path: Path, monkeypatch):
    """调用方直传真片名：不再麻烦 /Items 反查（新登录会踢掉旧 token 权限）。"""
    from dvpack import subsource

    out_dir = tmp_path / "pack"
    out_dir.mkdir()
    _fake_pack(out_dir)

    def _boom(src, item_id):
        raise AssertionError("直传了名就不许再查 /Items")

    monkeypatch.setattr(serve_module, "_jf_item_name", _boom)
    seen: dict = {}

    def _fetch(**kw):
        seen.update(kw)
        return (dict(_TEXTS), "subhd rc=0")

    monkeypatch.setattr(subsource, "fetch_online_texts", _fetch)
    receipt = serve_module._online_subs_body(tmp_path, _SRC, "5360", out_dir,
                                             "真片名")
    assert "zh+en" in receipt
    assert seen["name"] == "真片名"


def test_online_subs_body_skips_while_negative_cache_blocks(tmp_path: Path, monkeypatch):
    """负缓存 TTL 内直接短路：不查片名、不起子进程、不烧公网。"""
    from dvpack import subsource

    def _boom(**_kwargs):
        raise AssertionError("负缓存期内不许联网")

    monkeypatch.setattr(subsource, "fetch_online_texts", _boom)
    def _no_name(src, item_id):
        raise AssertionError("短路时不许查片名")

    monkeypatch.setattr(serve_module, "_jf_item_name", _no_name)
    cache_dir = subsource.subtitle_cache_dir(
        tmp_path, serve_module._src_fingerprint(_SRC))
    subsource.record_online_failure(cache_dir, "subhd rc=35")
    assert subsource.cache_blocked(cache_dir)
    receipt = serve_module._online_subs_body(tmp_path, _SRC, "534f", tmp_path)
    assert "负缓存" in receipt


def test_online_subs_body_records_failure_when_fetch_empty(tmp_path: Path, monkeypatch):
    """没抓到：落负缓存（TTL 内不重试），master 不许被动过。"""
    from dvpack import subsource

    out_dir = tmp_path / "pack"
    out_dir.mkdir()
    _fake_pack(out_dir)
    monkeypatch.setattr(serve_module, "_jf_item_name", lambda src, item_id: "风味人间")
    monkeypatch.setattr(subsource, "fetch_online_texts",
                        lambda **kw: ({}, "subhd rc=35"))
    receipt = serve_module._online_subs_body(tmp_path, _SRC, "5350", out_dir)
    assert "未拿到" in receipt and "rc=35" in receipt
    cache_dir = subsource.subtitle_cache_dir(
        tmp_path, serve_module._src_fingerprint(_SRC))
    assert subsource.cache_blocked(cache_dir)
    master = (out_dir / "master.m3u8").read_text(encoding="utf-8")
    assert "TYPE=SUBTITLES" not in master


def test_online_subs_body_respects_off_switch(tmp_path: Path, monkeypatch):
    from dvpack import subsource

    monkeypatch.setenv(subsource.ONLINE_ENV, "0")
    def _boom(**_kwargs):
        raise AssertionError("开关关了还抓什么")
    monkeypatch.setattr(subsource, "fetch_online_texts", _boom)
    receipt = serve_module._online_subs_body(tmp_path, _SRC, "534f", tmp_path)
    assert "已关" in receipt and subsource.ONLINE_ENV in receipt


def test_online_subs_body_hands_item_name_to_fetcher(tmp_path: Path, monkeypatch):
    """Jellyfin 真片名必须传到抓取侧；空片名 = 在线腿整体跳过。"""
    from dvpack import subsource

    seen: dict = {}

    def fake_fetch(**kw):
        seen.update(kw)
        return ({}, "空片名" if not kw.get("name") else "other")

    monkeypatch.setattr(subsource, "fetch_online_texts", fake_fetch)
    monkeypatch.setattr(serve_module, "_jf_item_name", lambda src, item_id: "")
    receipt = serve_module._online_subs_body(tmp_path, _SRC, "5360", tmp_path)
    assert seen["item_id"] == "5360" and seen["name"] == ""
    assert "未拿到" in receipt


class TestPackFullSwapTmpName:
    def test_upgrade_tmp_name_carries_pid(self, tmp_path, monkeypatch):
        """临时目录名带 pid：prewarm CLI 与 serve 守护进程是两个进程，
        _pack_lock_for 只是进程内 threading.Lock 互锁不到——固定名临时目录
        会被并发互 rmtree，毒包事故同款机理的跨进程版。"""
        seen: dict = {}

        def fake_parallel(src, tmp, maps, texts):
            seen["tmp"] = Path(tmp)
            return True

        monkeypatch.setattr(serve_module, "_parallel_full", fake_parallel)
        monkeypatch.setattr(
            serve_module, "_write_marker",
            lambda tmp, marker: (Path(tmp) / "source.json").write_text("{}"))
        monkeypatch.setattr(serve_module, "_src_duration_seconds",
                            lambda src: 42.0)
        monkeypatch.setattr(serve_module, "_rename_swap",
                            lambda src, dst: os.rename(src, dst))

        out = tmp_path / "_jf" / "i1"
        out.mkdir(parents=True)
        serve_module._pack_full_swap(
            "http://127.0.0.1:8899/Videos/i1/stream.mkv?static=true",
            out, [], [], "i1")

        assert seen["tmp"].name == "._upgrade_i1_%d" % os.getpid()
        assert seen["tmp"].parent == tmp_path / "_jf"
        assert (out / "source.json").exists()  # 换装后新内容到位
        assert not seen["tmp"].exists()  # 临时目录已让位


class TestSweepStaleSwaps:
    def test_dead_pid_dirs_swept_live_and_named_kept(self, tmp_path):
        """死 pid 的 ._upgrade_*/._trash_* 扫掉；活 pid 和 v48 前固定名
        （判不出活性）都不碰。"""
        import subprocess
        import sys

        child = subprocess.Popen([sys.executable, "-c", "pass"])
        child.wait()  # 回收掉，pid 确定已死（不留僵尸）
        dead = child.pid
        parent = tmp_path / "_jf"
        parent.mkdir()
        (parent / ("._upgrade_aaaa_%d" % dead)).mkdir()
        (parent / ("._trash_bbbb_%d" % dead)).mkdir()
        live = parent / ("._upgrade_cccc_%d" % os.getpid())
        live.mkdir()
        named = parent / "._upgrade_82caf5"
        named.mkdir()
        (parent / "i1").mkdir()

        assert serve_module._sweep_stale_swaps(parent) == 2
        assert not (parent / ("._upgrade_aaaa_%d" % dead)).exists()
        assert not (parent / ("._trash_bbbb_%d" % dead)).exists()
        assert live.exists() and named.exists() and (parent / "i1").exists()


class TestPackFullSwapInterrupt:
    def test_tmp_cleaned_on_keyboard_interrupt(self, tmp_path, monkeypatch):
        """Ctrl-C 也要清自己的 tmp：全靠 except BaseException 兜底。"""
        def boom(src, tmp, maps, texts):
            (Path(tmp) / "junk.m4s").write_text("x")
            raise KeyboardInterrupt

        monkeypatch.setattr(serve_module, "_parallel_full", boom)
        out = tmp_path / "_jf" / "i1"
        out.mkdir(parents=True)
        expected_tmp = tmp_path / "_jf" / ("._upgrade_i1_%d" % os.getpid())
        with pytest.raises(KeyboardInterrupt):
            serve_module._pack_full_swap(
                "http://127.0.0.1:8899/Videos/i1/stream.mkv?static=true",
                out, [], [], "i1")
        assert not expected_tmp.exists()


class TestWindowsCompat:
    """Windows 部署适配三处：swap 守卫 / pid 探测 / nt 分支派发。

    改坏了在 Mac 上无症状，真机症状是全片打包直接崩（AttributeError 穿透
    OSError 守卫）或死 pid 回收目录白等 45 分钟。
    """

    def test_rename_swap_off_darwin_raises_enosys(self, monkeypatch, tmp_path):
        monkeypatch.setattr(serve_module.os, "name", "nt")
        src, dst = tmp_path / "a", tmp_path / "b"
        src.mkdir()
        dst.mkdir()
        with pytest.raises(OSError) as exc:
            serve_module._rename_swap(src, dst)
        assert exc.value.errno == errno.ENOSYS
        assert src.is_dir() and dst.is_dir()

    def test_pid_alive_true_for_self(self):
        assert serve_module._pid_alive(os.getpid()) is True

    def test_pid_alive_false_for_reaped_child(self):
        proc = subprocess.Popen([sys.executable, "-c", "pass"])
        proc.wait()
        assert serve_module._pid_alive(proc.pid) is False

    def test_pid_alive_dispatches_nt_helper(self, monkeypatch):
        monkeypatch.setattr(serve_module.os, "name", "nt")
        monkeypatch.setattr(serve_module, "_pid_alive_nt", lambda pid: pid == 4242)
        assert serve_module._pid_alive(4242) is True
        assert serve_module._pid_alive(os.getpid()) is False


def _pack_playlist_type(monkeypatch, **kw):
    """跑一趟 pack_source（探测链全假），只为了看 playlist_type 有没有到 hls_command。"""
    from dvpack import jfpack
    seen = {}

    def spy(*a, **kwargs):
        seen.update(kwargs)
        return ["ffmpeg"]

    class _Proc:
        stderr = ""

    monkeypatch.setattr(jfpack, "ffmpeg_visible_source", lambda src: src)
    monkeypatch.setattr(jfpack, "ffprobe_json", lambda src: {})
    monkeypatch.setattr(jfpack, "parse_probe_json",
                        lambda payload, path: SimpleNamespace(duration=120.0))
    monkeypatch.setattr(jfpack, "mvp_verdict", lambda source: SimpleNamespace(supported=True))
    monkeypatch.setattr(jfpack, "audio_pack_mode", lambda s, m: (m, "aac", False))
    monkeypatch.setattr(jfpack, "source_full_range", lambda source: 0)
    monkeypatch.setattr(jfpack, "_source_colorimetry", lambda source: None)
    monkeypatch.setattr(jfpack, "dv_signalling_bsf", lambda source: ())
    monkeypatch.setattr(jfpack, "setts_bsf", lambda source: None)
    monkeypatch.setattr(jfpack, "hls_command", spy)
    monkeypatch.setattr(jfpack, "_run", lambda *a, **k: _Proc())
    with pytest.raises(jfpack.JfPackError):
        jfpack.pack_source("http://127.0.0.1/x.mkv?static=true", "/tmp/x", **kw)
    return seen.get("playlist_type")


def _playlist(entries, *, event=False, endlist=False):
    """按 ffmpeg hls muxer 的形状造一份媒体列表。"""
    lines = ["#EXTM3U", "#EXT-X-VERSION:7", "#EXT-X-TARGETDURATION:6",
             "#EXT-X-MEDIA-SEQUENCE:0"]
    lines.append("#EXT-X-PLAYLIST-TYPE:EVENT" if event else "#EXT-X-PLAYLIST-TYPE:VOD")
    lines.append('#EXT-X-MAP:URI="init.mp4"')
    for extinf, uri in entries:
        lines += ["#EXTINF:%.6f," % extinf, uri]
    if endlist:
        lines.append("#EXT-X-ENDLIST")
    return "\n".join(lines) + "\n"


class TestStubGrowthPlayback:
    """秒开 stub 并段续播：真机症状「片子只能放 20 秒」的服务端半边成因。

    链路是 _align_stub_to_full_pack（对齐+开环）→ _grow_playback（并段）。
    任一半断了，电视都停在 20 秒处，而 serve 日志一片安静。
    """

    def _stub(self, tmp_path, entries):
        out = tmp_path / "_jf" / "i1"
        out.mkdir(parents=True)
        (out / "init.mp4").write_bytes(b"INIT-SHARED")
        (out / "master.m3u8").write_text("#EXTM3U\n", encoding="utf-8")
        (out / "video.m3u8").write_text(_playlist(entries, endlist=True), encoding="utf-8")
        (out / "source.json").write_text(
            json.dumps({"src": "x", "seconds": 20.0}), encoding="utf-8")
        return out

    def test_align_drops_truncated_tail_and_opens_loop(self, tmp_path):
        """20s 源打出 6/6/6/2.173：砍 2.173 那段，同时把 VOD 翻成 EVENT。"""
        out = self._stub(tmp_path, [(6.0, "video0.m4s"), (6.0, "video1.m4s"),
                                    (6.0, "video2.m4s"), (2.173, "video3.m4s")])
        assert serve_module._align_stub_to_full_pack(out) == 3
        text = (out / "video.m3u8").read_text(encoding="utf-8")
        assert "#EXT-X-ENDLIST" not in text
        assert "#EXT-X-PLAYLIST-TYPE:EVENT" in text
        assert serve_module._playlist_entries(text) == [
            (6.0, "video0.m4s"), (6.0, "video1.m4s"), (6.0, "video2.m4s")]

    def test_align_is_idempotent(self, tmp_path):
        """已经对齐又已开环：再调一次返回原段数、一个字节都不改盘。"""
        out = self._stub(tmp_path, [(6.0, "video0.m4s"), (6.0, "video1.m4s"),
                                    (6.0, "video2.m4s"), (2.173, "video3.m4s")])
        serve_module._align_stub_to_full_pack(out)
        before = (out / "video.m3u8").read_bytes()
        assert serve_module._align_stub_to_full_pack(out) == 3
        assert (out / "video.m3u8").read_bytes() == before

    def test_align_keeps_single_segment_but_still_opens(self, tmp_path):
        """只有一段时不能砍空（保底留一段），但开环照样要做。"""
        out = self._stub(tmp_path, [(2.0, "video0.m4s")])
        assert serve_module._align_stub_to_full_pack(out) == 1
        text = (out / "video.m3u8").read_text(encoding="utf-8")
        assert "#EXT-X-ENDLIST" not in text
        assert serve_module._playlist_entries(text) == [(2.0, "video0.m4s")]

    def test_align_prefix_is_strict_prefix_of_full_pack(self, tmp_path):
        """砍完的 stub 必须是全片包的严格前缀——并段接缝长度为零的前提。"""
        full = [(6.0, "video%d.m4s" % n) for n in range(6)]
        out = self._stub(tmp_path, full[:3] + [(2.173, "video3.m4s")])
        kept = serve_module._align_stub_to_full_pack(out)
        assert serve_module._playlist_entries(
            (out / "video.m3u8").read_text(encoding="utf-8")) == full[:kept]

    def test_align_on_missing_playlist_is_noop(self, tmp_path):
        out = tmp_path / "_jf" / "i1"
        out.mkdir(parents=True)
        assert serve_module._align_stub_to_full_pack(out) == 0
        assert not (out / "video.m3u8").exists()

    def test_open_stub_if_needed_opens_stub_marker(self, tmp_path):
        out = self._stub(tmp_path, [(6.0, "video0.m4s"), (6.0, "video1.m4s")])
        assert serve_module._open_stub_if_needed(out) is True
        assert "#EXT-X-ENDLIST" not in (out / "video.m3u8").read_text(encoding="utf-8")

    def test_open_stub_if_needed_leaves_full_pack_alone(self, tmp_path):
        """自称全片（seconds==0）不碰：ENDLIST 是正确终态，轮询到即宣告完整。"""
        out = self._stub(tmp_path, [(6.0, "video0.m4s"), (6.0, "video1.m4s")])
        (out / "source.json").write_text(
            json.dumps({"src": "x", "seconds": 0.0, "plen": 120.0}), encoding="utf-8")
        before = (out / "video.m3u8").read_bytes()
        assert serve_module._open_stub_if_needed(out) is False
        assert (out / "video.m3u8").read_bytes() == before

    def test_open_stub_if_needed_leaves_poisoned_pack_alone(self, tmp_path):
        """毒包要走自愈重打，开环只会让坏包播得更久。"""
        out = self._stub(tmp_path, [(6.0, "video0.m4s")] * 3)
        (out / "source.json").write_text(json.dumps(
            {"src": "x", "seconds": 0.0, "plen": 1200.0}), encoding="utf-8")
        before = (out / "video.m3u8").read_bytes()
        assert serve_module._open_stub_if_needed(out) is False
        assert (out / "video.m3u8").read_bytes() == before


class TestSealedPrefix:
    def test_holds_back_in_flight_segment(self, tmp_path):
        """最后那一项往往还在写：分片文件数 - 1 才是可发布前缀。"""
        for n in range(5):
            (tmp_path / ("video%d.m4s" % n)).write_bytes(b"x")
        (tmp_path / "video.m3u8").write_text(_playlist(
            [(6.0, "video%d.m4s" % n) for n in range(5)], endlist=True), encoding="utf-8")
        entries, sealed = serve_module._sealed_prefix(tmp_path)
        assert sealed == 4
        assert entries == [(6.0, "video%d.m4s" % n) for n in range(4)]

    def test_no_segments_yet_is_zero(self, tmp_path):
        (tmp_path / "video.m3u8").write_text(_playlist([(6.0, "video0.m4s")]), encoding="utf-8")
        assert serve_module._sealed_prefix(tmp_path) == ([], 0)

    def test_missing_playlist_is_zero(self, tmp_path):
        assert serve_module._sealed_prefix(tmp_path) == ([], 0)


class TestGrowPlayback:
    def _packs(self, tmp_path):
        out = tmp_path / "_jf" / "i1"
        out.mkdir(parents=True)
        (out / "init.mp4").write_bytes(b"INIT-SHARED")
        (out / "video.m3u8").write_text(_playlist(
            [(6.0, "video%d.m4s" % n) for n in range(3)], event=True), encoding="utf-8")
        full = tmp_path / "._upgrade_i1_1"
        full.mkdir(parents=True)
        (full / "init.mp4").write_bytes(b"INIT-SHARED")
        for n in range(6):
            (full / ("video%d.m4s" % n)).write_bytes(b"seg%d" % n)
        (full / "video.m3u8").write_text(_playlist(
            [(6.0, "video%d.m4s" % n) for n in range(6)], endlist=True), encoding="utf-8")
        return full, out

    def _run_until(self, full, out, want):
        import threading
        stop = threading.Event()
        thread = threading.Thread(target=serve_module._grow_playback,
                                  args=(full, out, stop, "i1"), daemon=True)
        thread.start()
        try:
            deadline = time.monotonic() + 10.0
            while time.monotonic() < deadline:
                entries = serve_module._playlist_entries(
                    (out / "video.m3u8").read_text(encoding="utf-8"))
                if len(entries) >= want:
                    return entries
                time.sleep(0.05)
            raise AssertionError("并段没到 %d 段：%r" % (want, entries))
        finally:
            stop.set()
            thread.join(timeout=5.0)

    def test_publishes_sealed_prefix_into_live_pack(self, tmp_path):
        full, out = self._packs(tmp_path)
        entries = self._run_until(full, out, 5)
        assert entries == [(6.0, "video%d.m4s" % n) for n in range(5)]
        # 分片是真复制过来的，不是只改列表
        assert (out / "video4.m4s").read_bytes() == b"seg4"
        assert "#EXT-X-ENDLIST" not in (out / "video.m3u8").read_text(encoding="utf-8")

    def test_different_init_aborts_growth(self, tmp_path):
        """init 不一致 = 两套参数集：并段就是把解不动的分片喂给电视，必须整体退化成等换装。"""
        full, out = self._packs(tmp_path)
        (full / "init.mp4").write_bytes(b"INIT-OTHER")
        import threading
        stop = threading.Event()
        thread = threading.Thread(target=serve_module._grow_playback,
                                  args=(full, out, stop, "i1"), daemon=True)
        thread.start()
        thread.join(timeout=5.0)
        assert not thread.is_alive()
        assert not (out / "video3.m4s").exists()
        assert len(serve_module._playlist_entries(
            (out / "video.m3u8").read_text(encoding="utf-8"))) == 3

    def test_never_shrinks_the_live_playlist(self, tmp_path):
        """全片包刚开写时 tmp 里只有 1~2 段，绝不能把 3 段的在播列表砍短。

        2026-11-07 真机采样抓到 3→2→4：播放器正握着第 3 段，列表一短它就断。
        发布量的起点必须是在播列表已有的段数，不是 0。
        """
        full, out = self._packs(tmp_path)
        # 只留 2 段的全片包：模拟 ffmpeg 刚开写的那个瞬间
        for n in range(2, 6):
            (full / ("video%d.m4s" % n)).unlink()
        (full / "video.m3u8").write_text(_playlist(
            [(6.0, "video%d.m4s" % n) for n in range(2)], endlist=True), encoding="utf-8")
        import threading
        stop = threading.Event()
        thread = threading.Thread(target=serve_module._grow_playback,
                                  args=(full, out, stop, "i1"), daemon=True)
        thread.start()
        thread.join(timeout=5.0)
        stop.set()
        thread.join(timeout=5.0)
        entries = serve_module._playlist_entries(
            (out / "video.m3u8").read_text(encoding="utf-8"))
        assert len(entries) == 3, "在播列表被砍短了：%r" % (entries,)

    def test_publishes_only_forward(self, tmp_path):
        """并段过程中在播列表长度单调不减（真机采样断言同款）。"""
        full, out = self._packs(tmp_path)
        seen = []
        import threading
        stop = threading.Event()

        def watcher():
            while not stop.is_set():
                seen.append(len(serve_module._playlist_entries(
                    (out / "video.m3u8").read_text(encoding="utf-8"))))
                stop.wait(0.02)

        threading.Thread(target=watcher, daemon=True).start()
        self._run_until(full, out, 5)
        stop.set()
        assert seen, "没采到样"
        assert all(b >= a for a, b in zip(seen, seen[1:])), "在播列表变短：%s" % seen
        assert seen[-1] >= 5


def _bmff_box(btype: str, payload: bytes) -> bytes:
    return (len(payload) + 8).to_bytes(4, "big") + btype.encode("latin-1") + payload


def _bmff_full_box(btype: str, payload: bytes) -> bytes:
    return _bmff_box(btype, b"\0\0\0\0" + payload)


def _bmff_param_set_array(nal_type: int, *nals: bytes, complete: bool = True) -> bytes:
    out = bytes([(0xA0 if complete else 0x00) | nal_type]) + len(nals).to_bytes(2, "big")
    for nal in nals:
        out += len(nal).to_bytes(2, "big") + nal
    return out


def _bmff_dvh1(*arrays: bytes) -> bytes:
    """dvh1 sample entry：78 字节 VisualSampleEntry 固定字段 + 子盒（只放 hvcC）。"""
    hvcC = _bmff_box("hvcC", bytes(22) + len(arrays).to_bytes(1, "big") + b"".join(arrays))
    return _bmff_box("dvh1", bytes(78) + hvcC)


def _bmff_init(*entries: bytes) -> bytes:
    """最小可解析 init 段：ftyp + moov/trak/mdia/minf/stbl/stsd。"""
    stsd = _bmff_full_box("stsd", len(entries).to_bytes(4, "big") + b"".join(entries))
    # hdlr 载荷：version/flags(4) + predefined(4) + handler_type(4)，少一段
    # _handler 就从 offset+16 起读不到 handler，签名直接判「解析不了」。
    hdlr = _bmff_full_box("hdlr", b"\0\0\0\0" + b"\0\0\0\0" + b"vide")
    mdia = _bmff_box("mdia", hdlr + _bmff_box("minf", _bmff_box("stbl", stsd)))
    return _bmff_box("ftyp", b"iso5") + _bmff_box("moov", _bmff_box("trak", mdia))


class TestGrowPlaybackInitGate:
    """并段闸门判「同一套可解码流」，不是「同一个字节串」。

    2026-11-08 真机症状「播放卡住」的根因：20 秒秒开 stub 与全片后台包的 init
    只差 movenc 追加的第二个 dvh1 entry、hvcC 数组 completeness 标志、多出来的
    前缀 SEI(39) 数组。逐字节判把这种包整体放弃并段，电视停在 20 秒处转圈。
    """

    _VPS = b"\x40\x01\x0c\x01\xff\xff\x02\x20\x00\x00\x03\x00"
    _SPS = b"\x42\x01\x01\x02\x20\x00\x00\x03\x00\x90\x00\x00\x03\x00\x00\x03\x00"
    _PPS = b"\x44\x01\xc1\x72\xc8\x46\x92\xfc\x90"
    _SEI39 = b"\x4e\x01\x81\x01\x03\x80"

    def _mandatory(self, *, complete: bool) -> list[bytes]:
        return [_bmff_param_set_array(32, self._VPS, complete=complete),
                _bmff_param_set_array(33, self._SPS, complete=complete),
                _bmff_param_set_array(34, self._PPS, complete=complete)]

    def _packs(self, tmp_path, *, stub_init: bytes, full_init: bytes):
        out = tmp_path / "_jf" / "i1"
        out.mkdir(parents=True)
        (out / "init.mp4").write_bytes(stub_init)
        (out / "video.m3u8").write_text(_playlist(
            [(6.0, "video%d.m4s" % n) for n in range(3)], event=True), encoding="utf-8")
        full = tmp_path / "._upgrade_i1_1"
        full.mkdir(parents=True)
        (full / "init.mp4").write_bytes(full_init)
        for n in range(6):
            (full / ("video%d.m4s" % n)).write_bytes(b"seg%d" % n)
        (full / "video.m3u8").write_text(_playlist(
            [(6.0, "video%d.m4s" % n) for n in range(6)], endlist=True), encoding="utf-8")
        return full, out

    def test_grows_when_init_differs_only_in_undecodable_ways(self, tmp_path):
        """entry 数量 / completeness 标志 / 前缀 SEI 数组不同，但参数集相同：放行。"""
        stub = _bmff_init(_bmff_dvh1(*self._mandatory(complete=True)))
        full = _bmff_init(
            _bmff_dvh1(*self._mandatory(complete=False),
                       _bmff_param_set_array(39, self._SEI39, complete=False)),
            _bmff_dvh1(*self._mandatory(complete=False)),
        )
        assert stub != full
        full_dir, out = self._packs(tmp_path, stub_init=stub, full_init=full)
        import threading
        stop = threading.Event()
        thread = threading.Thread(target=serve_module._grow_playback,
                                  args=(full_dir, out, stop, "i1"), daemon=True)
        thread.start()
        try:
            deadline = time.monotonic() + 10.0
            while time.monotonic() < deadline:
                entries = serve_module._playlist_entries(
                    (out / "video.m3u8").read_text(encoding="utf-8"))
                if len(entries) >= 5:
                    break
                time.sleep(0.05)
        finally:
            stop.set()
            thread.join(timeout=5.0)
        assert len(entries) >= 5, "并段被字节差异误杀：%r" % (entries,)
        assert (out / "video4.m4s").read_bytes() == b"seg4"

    def test_aborts_when_parameter_sets_really_differ(self, tmp_path):
        """参数集内容不同 = 两套解码能力：仍然必须整体退化成等换装。"""
        other_sps = self._SPS[:-1] + b"\x01"
        stub = _bmff_init(_bmff_dvh1(*self._mandatory(complete=True)))
        full = _bmff_init(_bmff_dvh1(
            _bmff_param_set_array(32, self._VPS, complete=False),
            _bmff_param_set_array(33, other_sps, complete=False),
            _bmff_param_set_array(34, self._PPS, complete=False),
        ))
        full_dir, out = self._packs(tmp_path, stub_init=stub, full_init=full)
        import threading
        stop = threading.Event()
        thread = threading.Thread(target=serve_module._grow_playback,
                                  args=(full_dir, out, stop, "i1"), daemon=True)
        thread.start()
        thread.join(timeout=5.0)
        stop.set()
        thread.join(timeout=5.0)
        assert not (out / "video3.m4s").exists()
        assert len(serve_module._playlist_entries(
            (out / "video.m3u8").read_text(encoding="utf-8"))) == 3


class TestCopyAtomic:
    def test_open_reader_keeps_old_inode(self, tmp_path):
        """AVPlayer 随时可能正在读 dst；os.replace 保证它握着的旧 fd 不被打断。"""
        src, dst = tmp_path / "src", tmp_path / "dst"
        src.write_bytes(b"new-bytes")
        dst.write_bytes(b"old-bytes")
        with dst.open("rb") as reader:
            serve_module._copy_atomic(src, dst)
            assert reader.read() == b"old-bytes"
        assert dst.read_bytes() == b"new-bytes"
        assert not (tmp_path / "dst.growing").exists()

    def test_unreadable_source_leaves_dst_intact(self, tmp_path):
        src, dst = tmp_path / "src", tmp_path / "dst"
        dst.write_bytes(b"old-bytes")
        with pytest.raises(OSError):
            serve_module._copy_atomic(src, dst)
        assert dst.read_bytes() == b"old-bytes"


class TestSealFullPlaylist:
    """全片包用 EVENT 打、换装前收回 VOD+ENDLIST。

    少了 EVENT，ffmpeg 直到进程退出才写 video.m3u8，grower 全程空转，
    续播退化回「只能放 20 秒」；少了收回，电视拿到一份永无止境的 EVENT
    列表，播完也不收场。
    """

    def test_event_is_sealed_to_vod_with_endlist(self, tmp_path):
        (tmp_path / "video.m3u8").write_text(_playlist(
            [(6.0, "video%d.m4s" % n) for n in range(4)], event=True), encoding="utf-8")
        assert serve_module._seal_full_playlist(tmp_path) == 4
        text = (tmp_path / "video.m3u8").read_text(encoding="utf-8")
        assert "#EXT-X-PLAYLIST-TYPE:VOD" in text
        assert "#EXT-X-ENDLIST" in text
        assert serve_module._playlist_entries(text) == [
            (6.0, "video%d.m4s" % n) for n in range(4)]

    def test_already_vod_is_left_alone(self, tmp_path):
        before = _playlist([(6.0, "video0.m4s")], endlist=True)
        (tmp_path / "video.m3u8").write_text(before, encoding="utf-8")
        assert serve_module._seal_full_playlist(tmp_path) == 1
        assert (tmp_path / "video.m3u8").read_text(encoding="utf-8") == before

    def test_missing_playlist_is_zero(self, tmp_path):
        assert serve_module._seal_full_playlist(tmp_path) == 0

    def test_targetduration_covers_longest_segment(self, tmp_path):
        (tmp_path / "video.m3u8").write_text(_playlist(
            [(6.0, "video0.m4s"), (14.4, "video1.m4s")], event=True), encoding="utf-8")
        serve_module._seal_full_playlist(tmp_path)
        assert "#EXT-X-TARGETDURATION:15" in (tmp_path / "video.m3u8").read_text(encoding="utf-8")


class TestPlaylistTypePlumbing:
    """EVENT 口径必须真的传到 ffmpeg argv，否则 grower 读不到东西。"""

    def test_hls_command_defaults_to_vod(self):
        from dvpack.remux import hls_command
        argv = hls_command(Path("in.mkv"), Path("out.m3u8"))
        assert argv[argv.index("-hls_playlist_type") + 1] == "vod"

    def test_hls_command_accepts_event(self):
        from dvpack.remux import hls_command
        argv = hls_command(Path("in.mkv"), Path("out.m3u8"), playlist_type="event")
        assert argv[argv.index("-hls_playlist_type") + 1] == "event"

    def test_pack_source_passes_playlist_type_through(self, monkeypatch):
        """EVENT 必须真的走到 hls_command——少这一跳 grower 就读不到已封段。"""
        assert _pack_playlist_type(monkeypatch, playlist_type="event") == "event"

    def test_pack_source_defaults_to_vod(self, monkeypatch):
        """秒开 stub 仍走 VOD：定长包 + ENDLIST，电视不会守着它等追加。"""
        assert _pack_playlist_type(monkeypatch) == "vod"



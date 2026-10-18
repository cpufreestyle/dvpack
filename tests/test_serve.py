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
import subprocess
import sys
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

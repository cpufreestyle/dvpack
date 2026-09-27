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
from dvpack.bmff import hvcC_param_set_types
from dvpack.jfpack import _source_colorimetry, pack_source
from dvpack.probe import NotProbeable, SourceInfo, VideoStream
from dvpack.serve import ID_PATTERN, _cache_fresh, _src_fingerprint, _valid_pack_src, serve
from dvpack.tools import FFMPEG

SRC = Path(os.environ.get("DVPACK_SOURCE_MKV", ""))


def _source_with_colour_tags(primaries, transfer, space):
    video = VideoStream(
        index=0,
        codec="hevc",
        profile="Main 10",
        width=3840,
        height=2160,
        bit_depth=10,
        frame_rate=23.976,
        color_transfer=transfer,
        color_space=space,
        color_primaries=primaries,
        dovi=None,
    )
    return SourceInfo(
        path=Path("in.mkv"),
        format_name="matroska",
        duration=1.0,
        size=1,
        video=video,
    )


def test_source_colorimetry_falls_back_when_source_is_unflagged():
    """DV rip 三件套为空（2026-09-26 实测）→ None，由 hls_command 落 DV 默认。"""
    assert _source_colorimetry(_source_with_colour_tags("", "", "")) is None


def test_source_colorimetry_keeps_declared_tags():
    assert _source_colorimetry(
        _source_with_colour_tags("bt2020", "smpte2084", "bt2020nc")
    ) == ("bt2020", "smpte2084", "bt2020nc")

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


def test_probe_failure_returns_502_not_dropped_connection(pack_site, monkeypatch):
    """ffprobe 失败（launchd 出站被 TCC 拦成 EHOSTUNREACH）必须回 502。

    实测教训：NotProbeable 不在 catch 面里时，异常炸穿 socket，
    客户端拿到 http 000，serve 日志只剩半截 traceback。
    """

    def boom(*args, **kwargs):
        raise NotProbeable("ffprobe 失败（1）：http://192.168.1.68/x — No route to host")

    monkeypatch.setattr(serve_module, "pack_source", boom)
    port, _, _ = pack_site
    resp, _ = _get(port, f"/jf/pack?src={PRIVATE_SRC}&id=e01&seconds=5")
    assert resp.status == 502


def test_ffprobe_error_message_carries_stderr(monkeypatch):
    """ffprobe 失败必须把 stderr 尾巴带进异常——只知 failed 不知为何等于没报。"""
    import dvpack.probe as probe_module

    class FakeProc:
        returncode = 1
        stdout = ""
        stderr = "http://192.168.1.68/x: No route to host\n"

    def fake_run(argv, **kwargs):
        return FakeProc()

    monkeypatch.setattr(probe_module.subprocess, "run", fake_run)
    with pytest.raises(NotProbeable, match="No route to host"):
        probe_module.ffprobe_json("http://192.168.1.68/x")


@needs_source
def test_pack_source_injects_param_sets_for_matroska(tmp_path: Path):
    """Jellyfin 主场景（MKV）打包后 init 段 hvcC 必须自带参数集——真机停播根因回归。"""
    result = pack_source(str(SRC), tmp_path / "out", seconds=6, segment_seconds=6.0)
    types = hvcC_param_set_types(result.output.init.read_bytes())
    assert {32, 33, 34} <= set(types), f"init 段 hvcC 参数集缺失，现有 {types}"


@needs_source
def test_pack_source_converges_to_single_sample_entry(tmp_path: Path) -> None:
    """E2E：pack_source 产物 init 必须收敛为单 dvh1 entry——2026-09-26 实测
    trex default_sample_description_index=1、11 个分片 tfhd 全无
    sample-description-index 覆盖，只引 entry0，第二个 entry 是死重；按 Apple
    官方参考流与已验证能播的 1080p 流（单 dvh1 entry）同一结构收敛。收敛
    手术对已收敛的 init 必须幂等。"""
    from dvpack.bmff import keep_single_sample_entry, sample_entry_count

    result = pack_source(str(SRC), tmp_path / "out", seconds=6, segment_seconds=6.0)
    init = result.output.init.read_bytes()
    entries = sample_entry_count(init)
    assert entries == 1, f"stsd 应收敛为单 entry，现有 {entries}"
    types = hvcC_param_set_types(init)
    assert {32, 33, 34} <= set(types), f"init 段 hvcC 参数集缺失，现有 {types}"
    single, dropped = keep_single_sample_entry(init)
    assert single == init and dropped == 0, "已收敛 init 再过单 entry 手术必须幂等"
JF_SRC = os.environ.get("DVPACK_SOURCE_URL", "")
needs_jf = pytest.mark.skipif(not JF_SRC, reason="缺 DVPACK_SOURCE_URL")


@needs_jf
def test_jfpack_drops_colr_and_sei_array_by_default(tmp_path: Path):
    """Jellyfin 主链默认产出 Apple 同构信号：init 无 colr、hvcC 只有 32/33/34。

    v2 臂（无 colr + 无 39 数组）2026-09-26 已在真机验证；v1（只去 colr）
    未拿到「亮」的结论前，两臂都保留，默认走 v2。"""
    result = pack_source(
        JF_SRC, tmp_path / "out", seconds=6, segment_seconds=6.0
    )
    init = result.output.init.read_bytes()
    from dvpack.bmff import remove_box as _remove_box

    _, colr_count = _remove_box(init, "colr")
    assert colr_count == 0, "init 段不应带 colr 盒（Apple 官方 P5 参考无 colr）"
    types = hvcC_param_set_types(init)
    assert 39 not in types, f"hvcC 不应带前缀 SEI(39) 数组，现有 {types}"
    assert {32, 33, 34} <= set(types)
    assert result.verification.ok, result.verification.problems


@needs_jf
def test_jfpack_can_keep_colr_control_arm(tmp_path: Path):
    """对照臂：显式 drop_colr=False + drop_param_set_type=None 回到原样直出。"""
    result = pack_source(
        JF_SRC,
        tmp_path / "control",
        seconds=6,
        segment_seconds=6.0,
        drop_colr=False,
        drop_param_set_type=None,
    )
    init = result.output.init.read_bytes()
    from dvpack.bmff import remove_box as _remove_box

    _, colr_count = _remove_box(init, "colr")
    assert colr_count == 1, "对照臂应保留 colr 盒"
    types = hvcC_param_set_types(init)
    assert 39 in types, f"对照臂应保留 39 数组，现有 {types}"
    assert result.verification.ok, result.verification.problems

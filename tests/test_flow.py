"""dvpack.flow：服务端 FRC 插帧的纯函数与 master 外科手术（不打进程）。

这里只焊三件事，全都是真机踩过坑的：
  * RPU 切分/交错必须逐字节可逆，且输出条数恰好 2n-1（dovi_tool inject-rpu
    对不上条数会跳过尾部元数据，症状是尾段 DV 徽标灭）；
  * 按 GOP 切分后的帧数账必须合计 2n-1，否则音画会静默错位；
  * master prepend 必须幂等、必须照抄原变体属性、必须保住字幕轨——flow100/
    会被 serve 的全片升级目录换装丢掉，所以 prepend 要能反复重跑。

真 ffmpeg / vtfrc / dovi_tool 的链路由真源 smoke 覆盖，不在这里 mock。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from dvpack import flow
from dvpack.flow import (
    FlowError,
    apply_flow_variant,
    check_canary,
    count_packets,
    flow_dir_name,
    flow_uri,
    interleave_rpu_units,
    parse_vtfrc_result,
    plan_chunks,
    prepend_variant_lines,
    split_rpu_units,
    strip_flow_lines,
    _assert_segment_durations,
    _bl_remux_argv,
    _chunk_encode_argv,
    _hls_remux_argv,
    _int_fps,
    _mp4_remux_argv,
    _verify_es,
)

#: 现役 _jf/<hash> 的 master 约定：CODECS=hvc1 + SUPPLEMENTAL-CODECS，
#: URI 绝对，带一条 #EXT-X-MEDIA 字幕轨。
_JF_MASTER = (
    "#EXTM3U\n"
    "#EXT-X-VERSION:7\n"
    "#EXT-X-INDEPENDENT-SEGMENTS\n"
    '#EXT-X-MEDIA:TYPE=SUBTITLES,GROUP-ID="subs",LANGUAGE="zh",NAME="中文",'
    'DEFAULT=NO,AUTOSELECT=NO,URI="sub_zh.m3u8"\n'
    "#EXT-X-STREAM-INF:BANDWIDTH=24797971,"
    'CODECS="hvc1.2.4.L153.B0,mp4a.40.2",RESOLUTION=3840x2160,FRAME-RATE=50,'
    'VIDEO-RANGE=PQ,SUBTITLES="subs",SUPPLEMENTAL-CODECS="dvh1.05.06/db1p"\n'
    "/_jf/ab476f/video.m3u8\n"
)

#: 另一套约定（_jf.a_nocolr）：CODECS 直接写 dvh1，URI 相对，无字幕轨。
_NOCOLR_MASTER = (
    "#EXTM3U\n"
    "#EXT-X-VERSION:7\n"
    "#EXT-X-INDEPENDENT-SEGMENTS\n"
    "#EXT-X-STREAM-INF:BANDWIDTH=29477971,"
    'CODECS="dvh1.05.06,ec-3",RESOLUTION=3840x2160,FRAME-RATE=50,VIDEO-RANGE=PQ\n'
    "video.m3u8\n"
)


def _rpu_blob(count: int) -> bytes:
    """造 count 个 RPU 单位：4 字节起始码 + 首字节 0x19 + 少量 payload。"""
    parts = []
    for i in range(count):
        parts.append(b"\x00\x00\x00\x01\x19" + bytes([i % 256, 0x2A, 0x00]))
    return b"".join(parts)


# ---------------------------------------------------------------- RPU 切分/交错


def test_split_rpu_units_roundtrip():
    blob = _rpu_blob(45)
    units = split_rpu_units(blob)
    assert len(units) == 45
    assert b"".join(units) == blob
    assert all(u[:5] == b"\x00\x00\x00\x01\x19" for u in units)


def test_split_rpu_units_empty_blob():
    assert split_rpu_units(b"") == []


def test_interleave_rpu_units_keeps_previous_frame_metadata():
    units = [b"U0", b"U1", b"U2", b"U3", b"U4"]
    out = interleave_rpu_units(units, 2 * len(units) - 1)
    # 插帧产物排在两个源帧之间，沿用前一帧的元数据（floor），不超前。
    assert out == [b"U0", b"U0", b"U1", b"U1", b"U2", b"U2", b"U3", b"U3", b"U4"]


def test_interleave_rpu_units_rejects_off_by_one():
    # 多一条：inject-rpu 会告警并跳过尾部；少一条：尾帧没有 RPU。
    with pytest.raises(FlowError):
        interleave_rpu_units(_rpu_blob(5), 2 * 5)
    with pytest.raises(FlowError):
        interleave_rpu_units(_rpu_blob(5), 2 * 5 - 2)


# ---------------------------------------------------------------- 帧数账


def test_plan_chunks_ledger_sums_to_2n_minus_1():
    # 实测 GOP 不等长（4500 帧共 45 个 K，间隔 104），这里按 104 造。
    n = 1040
    keys = list(range(0, n, 104))
    plans = plan_chunks(n, keys, jobs=2)
    assert len(plans) == 2
    assert plans[0].start == 0
    assert plans[1].start in keys and plans[1].start > 0
    # worker0 不喂前一帧；worker k>=1 多喂一帧凑帧对。
    assert plans[0].fed == plans[0].end - plans[0].start
    assert plans[1].fed == plans[1].end - plans[1].start + 1
    assert plans[0].kept == 2 * plans[0].fed - 1
    assert plans[1].kept == 2 * plans[1].fed - 2
    assert sum(plan.kept for plan in plans) == 2 * n - 1
    # 丢首帧的代价：worker k>=1 的产出比 emitted 少一帧。
    assert plans[1].kept == plans[1].emitted - 1


def test_plan_chunks_caps_jobs_at_max():
    plans = plan_chunks(1000, list(range(0, 1000, 100)), jobs=99)
    assert len(plans) <= 2


def test_plan_chunks_falls_back_to_one_worker_when_gop_too_sparse():
    # 关键帧不够密时少切一段，而不是切到非关键帧上（起播会黑一下）。
    plans = plan_chunks(1000, [0], jobs=2)
    assert len(plans) == 1
    assert plans[0].kept == 2 * 1000 - 1


def test_plan_chunks_requires_keyframe_zero():
    with pytest.raises(FlowError):
        plan_chunks(100, [1, 2, 3], jobs=2)


def test_plan_chunks_rejects_single_frame():
    with pytest.raises(FlowError):
        plan_chunks(1, [0], jobs=1)


# ---------------------------------------------------------------- 金丝雀


def test_parse_vtfrc_result_reads_canary_line():
    text = "plan W=384 H=216 src=50 dst=100\nres\nRESULT consumed=8 emitted=15 pairs=7\n"
    assert parse_vtfrc_result(text) == (8, 15)
    assert parse_vtfrc_result("no result here") is None


def test_check_canary_accepts_matching_ledger():
    plans = plan_chunks(9, [0, 4, 8], jobs=2)
    for plan in plans:
        check_canary(plan, "RESULT consumed=%d emitted=%d" % (plan.fed, plan.emitted))


def test_check_canary_rejects_mismatch():
    plan = plan_chunks(9, [0, 4, 8], jobs=2)[0]
    with pytest.raises(FlowError):
        check_canary(plan, "RESULT consumed=8 emitted=15")


def test_check_canary_rejects_missing_line():
    plan = plan_chunks(9, [0, 4, 8], jobs=2)[0]
    with pytest.raises(FlowError):
        check_canary(plan, "session 0.3s")


# ---------------------------------------------------------------- URI


def test_flow_uri_relative_and_absolute():
    assert flow_uri("video.m3u8", 100) == "flow100/video.m3u8"
    assert flow_uri("/_jf/ab476f/video.m3u8", 100) == "/_jf/ab476f/flow100/video.m3u8"
    assert flow_dir_name(120) == "flow120"


# ---------------------------------------------------------------- master 手术


def test_strip_flow_lines_is_idempotent_and_keeps_everything_else():
    master = _JF_MASTER + "flow100/video.m3u8\n"
    once = strip_flow_lines(master, "flow100")
    assert "flow100/" not in once
    assert _JF_MASTER in once
    assert strip_flow_lines(once, "flow100") == once
    # 字幕轨与 #EXT-X-INDEPENDENT-SEGMENTS 一行不少。
    assert '#EXT-X-MEDIA:TYPE=SUBTITLES' in once
    assert "#EXT-X-INDEPENDENT-SEGMENTS" in once


def test_strip_flow_lines_ignores_other_fps_tags():
    master = _JF_MASTER + "flow120/video.m3u8\n"
    # 换目标帧率时清的是另一条，不去碰别人的 flow 行。
    assert "flow120/video.m3u8" in strip_flow_lines(master, "flow100")


def test_prepend_variant_lines_puts_flow_first_and_keeps_crlf():
    master = _JF_MASTER.replace("\n", "\r\n")
    text = prepend_variant_lines(master, ["#EXT-X-STREAM-INF:BANDWIDTH=1", "u.m3u8"])
    lines = text.split("\r\n")
    # _JF_MASTER 共 6 行头部（含 1 条 MEDIA），flow 变体插首条 STREAM-INF 前。
    assert lines[4] == "#EXT-X-STREAM-INF:BANDWIDTH=1"
    assert lines[5] == "u.m3u8"
    assert text.endswith("\r\n")


def test_prepend_variant_lines_rejects_master_without_variant():
    with pytest.raises(FlowError):
        prepend_variant_lines("#EXTM3U\n#EXT-X-ENDLIST\n", ["x"])


def test_apply_flow_variant_copies_attributes_verbatim(tmp_path: Path):
    master = tmp_path / "master.m3u8"
    master.write_text(_JF_MASTER, encoding="utf-8")
    variant = apply_flow_variant(master, bandwidth=30_000_000, frame_rate=100)
    # 属性照抄原变体，只改 URI / BANDWIDTH / FRAME-RATE。
    assert variant.codecs == "hvc1.2.4.L153.B0,mp4a.40.2"
    assert variant.supplemental_codecs == "dvh1.05.06/db1p"
    assert variant.video_range == "PQ"
    assert variant.resolution == (3840, 2160)
    assert variant.frame_rate == 100
    assert variant.uri == "/_jf/ab476f/flow100/video.m3u8"
    assert variant.bandwidth == 30_000_000
    # 字幕轨必须保住：variant_from_master 不解析 SUBTITLES，得自己补 group。
    assert variant.subtitles == "subs"

    text = master.read_text(encoding="utf-8")
    assert text.index("flow100/video.m3u8") < text.index("/_jf/ab476f/video.m3u8")
    assert '#EXT-X-MEDIA:TYPE=SUBTITLES' in text
    assert 'SUBTITLES="subs"' in text
    assert 'SUPPLEMENTAL-CODECS="dvh1.05.06/db1p"' in text


def test_apply_flow_variant_is_idempotent(tmp_path: Path):
    # serve 全片升级会丢掉 flow100/，master 要能反复 prepend 而不叠加。
    master = tmp_path / "master.m3u8"
    master.write_text(_JF_MASTER, encoding="utf-8")
    for _ in range(3):
        apply_flow_variant(master, bandwidth=30_000_000, frame_rate=100)
    text = master.read_text(encoding="utf-8")
    assert text.count("flow100/video.m3u8") == 1
    assert text.count("#EXT-X-STREAM-INF:") == 2


def test_apply_flow_variant_handles_the_other_master_convention(tmp_path: Path):
    master = tmp_path / "master.m3u8"
    master.write_text(_NOCOLR_MASTER, encoding="utf-8")
    variant = apply_flow_variant(master, bandwidth=29_000_000, frame_rate=100)
    # 另一套约定（CODECS 直接写 dvh1、无 SUPPLEMENTAL-CODECS）也要原样穿过去。
    assert variant.codecs == "dvh1.05.06,ec-3"
    assert variant.supplemental_codecs is None
    assert variant.subtitles is None
    assert variant.uri == "flow100/video.m3u8"


def test_apply_flow_variant_replaces_stale_bandwidth(tmp_path: Path):
    master = tmp_path / "master.m3u8"
    master.write_text(_JF_MASTER + "flow100/video.m3u8\n", encoding="utf-8")
    apply_flow_variant(master, bandwidth=31_500_000, frame_rate=100)
    text = master.read_text(encoding="utf-8")
    assert "BANDWIDTH=31500000" in text
    assert "BANDWIDTH=24797971" in text
    assert text.count("flow100/video.m3u8") == 1


# ---------------------------------------------------------------- 杂项闸门


def test_chunk_encode_argv_uses_libx265(tmp_path: Path):
    # homebrew ffmpeg 里没有裸 "x265"；且私有选项必须位于输出文件之前，
    # 放后面会被 ffmpeg 当尾随参数忽略。
    chunk = tmp_path / "chunk0.hevc"
    argv = _chunk_encode_argv(0, chunk, 3840, 2160, 100, "x265", "ultrafast")
    assert argv[argv.index("-c:v") + 1] == "libx265"
    assert "x265" not in [a for a in argv if a != "-x265-params"]
    assert argv.index("-x265-params") < argv.index(str(chunk))
    # vt 腿同名直通，且不掺 x265 私有参数。
    argv_vt = _chunk_encode_argv(1, chunk, 3840, 2160, 100,
                                 "hevc_videotoolbox", "fast")
    assert argv_vt[argv_vt.index("-c:v") + 1] == "hevc_videotoolbox"
    assert "-x265-params" not in argv_vt
    # k>=1 丢首帧的 select 仍在位。
    assert r"select=gte(n\,1)" in argv_vt


def test_bl_remux_argv_zeroes_the_timeline(tmp_path: Path):
    # 裸 HEVC 前几帧 dts 为负；不归零就封 mp4 会写进 edit list，
    # presentation 轴偏移让 -ss 落点整段后移，chunk 尾帧数悄悄变少。
    hevc = tmp_path / "bl.hevc"
    mp4 = tmp_path / "bl.mp4"
    argv = _bl_remux_argv(50, hevc, mp4)
    assert argv[argv.index("-framerate") + 1] == "50"
    assert argv.index("-avoid_negative_ts") < argv.index(str(mp4))
    assert argv[argv.index("-avoid_negative_ts") + 1] == "make_zero"
    assert argv[-1] == str(mp4)
    assert argv[argv.index("-i") + 1] == str(hevc)


def test_int_fps_rejects_fractional():
    assert _int_fps(50.0) == 50
    with pytest.raises(FlowError):
        _int_fps(23.976)


def test_verify_es_rejects_stray_files(tmp_path: Path):
    (tmp_path / "init.mp4").write_bytes(b"")
    (tmp_path / "video.m3u8").write_text("", encoding="utf-8")
    (tmp_path / "video0.m4s").write_bytes(b"")
    _verify_es(tmp_path)
    (tmp_path / "chunk0.hevc").write_bytes(b"")
    with pytest.raises(FlowError):
        _verify_es(tmp_path)


def test_count_packets_tolerates_trailing_comma(monkeypatch, tmp_path: Path):
    # HLS 输入让 ffprobe 附带 side_data_list，csv 模式输出是 "4500,"。
    class _Proc:
        returncode = 0
        stdout = "4500,\n"
        stderr = ""

    monkeypatch.setattr(flow, "run_command", lambda *a, **k: _Proc())
    assert count_packets(tmp_path / "video.m3u8") == 4500


def test_hls_remux_argv_inputs_are_absolute(tmp_path: Path, monkeypatch):
    # 2026-10-06：step 6 带 cwd=stage，相对路径的源 playlist 让 ffmpeg
    # rc=254 且 stderr 被吞，症状拖成 6 步后 init.mp4 缺失。两路输入必须
    # resolve 成绝对路径，与 cwd 彻底解耦。
    monkeypatch.chdir(tmp_path)
    (tmp_path / "pack").mkdir()
    argv = _hls_remux_argv(
        video_mp4=Path("stage") / "dv_full.mp4",
        src_playlist=Path("pack") / "video.m3u8",
        seg_seconds=6.24,
    )
    inputs = [argv[i + 1] for i, item in enumerate(argv) if item == "-i"]
    assert len(inputs) == 2
    assert all(Path(value).is_absolute() for value in inputs)
    assert inputs[0].endswith("dv_full.mp4")
    assert inputs[1].endswith("video.m3u8")
    assert "-f" not in argv or argv[argv.index("-f") + 1] != "hevc"
    assert "-framerate" not in argv
    assert argv[argv.index("-hls_fmp4_init_filename") + 1] == "init.mp4"
    assert argv[-1] == "video.m3u8"


def test_mp4_remux_argv_inputs_are_absolute(tmp_path: Path, monkeypatch):
    # 2026-10-06 EXTINF=0：裸 HEVC 直进 hls muxer，demuxer 只给 pts 不填
    # pkt duration，ffmpeg rc=0 但写出 #EXTINF:0.000000，ATV 播放失败。
    # 修法是先过一道 mp4 容器把 stts 时长写实，argv 必须是 -framerate
    # dst_fps 的 hevc 输入，两路路径 resolve 成绝对路径。
    monkeypatch.chdir(tmp_path)
    argv = _mp4_remux_argv(
        dv_full=Path("stage") / "dv_full.hevc",
        dv_mp4=Path("stage") / "dv_full.mp4",
        dst_fps=100,
    )
    inputs = [argv[i + 1] for i, item in enumerate(argv) if item == "-i"]
    assert inputs == [str((tmp_path / "stage" / "dv_full.hevc").resolve())]
    assert argv[argv.index("-framerate") + 1] == "100"
    assert argv[argv.index("-avoid_negative_ts") + 1] == "make_zero"
    assert argv[-1] == str((tmp_path / "stage" / "dv_full.mp4").resolve())


def test_assert_segment_durations_rejects_zero(tmp_path: Path):
    # 2026-10-06 EXTINF=0 事故的看门狗：零时长、缺 TARGETDURATION、合计
    # 对不上账，任一命中整包作废；合计守恒（6.24+5.04=11.28=1128/100）放过。
    good = tmp_path / "good.m3u8"
    good.write_text(
        "#EXTM3U\n"
        "#EXT-X-VERSION:7\n"
        "#EXT-X-TARGETDURATION:6\n"
        "#EXTINF:6.24,\n"
        "v0.m4s\n"
        "#EXTINF:5.04,\n"
        "v1.m4s\n"
        "#EXT-X-ENDLIST\n",
        encoding="utf-8",
    )
    _assert_segment_durations(good, out_frames=1128, dst_fps=100)
    _assert_segment_durations(good, out_frames=1131, dst_fps=100)

    zero = tmp_path / "zero.m3u8"
    zero.write_text(
        "#EXTM3U\n"
        "#EXT-X-TARGETDURATION:0\n"
        "#EXTINF:0.000000,\n"
        "v0.m4s\n"
        "#EXT-X-ENDLIST\n",
        encoding="utf-8",
    )
    with pytest.raises(FlowError):
        _assert_segment_durations(zero, out_frames=1128, dst_fps=100)

    no_target = tmp_path / "no_target.m3u8"
    no_target.write_text(
        "#EXTM3U\n"
        "#EXTINF:6.24,\n"
        "v0.m4s\n"
        "#EXTINF:5.04,\n"
        "v1.m4s\n"
        "#EXT-X-ENDLIST\n",
        encoding="utf-8",
    )
    with pytest.raises(FlowError):
        _assert_segment_durations(no_target, out_frames=1128, dst_fps=100)

    short = tmp_path / "short.m3u8"
    short.write_text(
        "#EXTM3U\n"
        "#EXT-X-TARGETDURATION:6\n"
        "#EXTINF:6.24,\n"
        "v0.m4s\n"
        "#EXT-X-ENDLIST\n",
        encoding="utf-8",
    )
    with pytest.raises(FlowError):
        _assert_segment_durations(short, out_frames=1128, dst_fps=100)

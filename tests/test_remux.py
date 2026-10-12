"""remux 测试：命令行构成 + 真文件端到端 + 一条"少了 flag 会怎样"的反面控制。

反面控制很重要：`-strict unofficial` 看起来像多余的历史包袱，很容易被后人当噪音删掉，
而删掉的后果是 init 段不再有 DV 配置 box、电视静默地当普通 HDR 放。
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
from dvpack.bmff import hvcC_param_set_types, locate_dv_boxes, parse_file
from dvpack.p5 import DvcConfig
from dvpack.probe import SourceInfo, VideoStream, parse_probe_json
from dvpack.remux import (
    PARAM_SET_INJECTION_BSF,
    DV_SPS_VUI_BSF,
    RemuxError,
    check_rpu_consistency,
    dv_signalling_bsf,
    dv_sps_vui_bsf,
    hls_command,
    param_set_injection_bsf,
    remux,
    remux_and_verify,
    resolve_output,
    setts_bsf,
    source_full_range,
)
from dvpack.tools import FFMPEG

SRC = Path(os.environ.get("DVPACK_SOURCE_MKV", ""))

needs_source = pytest.mark.skipif(
    not (SRC.is_file() and FFMPEG.exists()), reason="缺片源或 ffmpeg"
)


def test_command_carries_both_dv_flags():
    argv = hls_command(Path("in.mkv"), Path("out/video.m3u8"), seconds=30)
    joined = " ".join(argv)
    assert "-tag:v dvh1" in joined
    assert "-strict unofficial" in joined
    assert "-hls_segment_type fmp4" in joined
    assert " -c copy " in f" {' '.join(argv)} "
    assert argv[-1] == str(Path("out/video.m3u8"))


def test_video_only_mapping_is_explicit():
    """字幕不映射，否则 ffmpeg 会拿 webvtt muxer 去封 SSA 直接失败。"""
    argv = hls_command(Path("in.mkv"), Path("v.m3u8"))
    assert argv.count("0:v:0") == 1
    assert not any("0:s" in arg for arg in argv)


# ── 真机「黑屏卡住」与「没声音」的回归闸（2026-09-27）──────────────────────
# 两个独立根因，都在这里锁住：
#   1. 时间轴：源 DTS 非单调 + `-c copy` → 末段切成 3 帧残片（EXTINF:0.06，
#      TARGETDURATION 却是 6）且片内 DTS 乱序（60.060→60.220→60.140）。
#      AVPlayer 停在无法呈现的缓冲区 = 黑屏、不再发请求（分片全 200）。
#   2. 声音：默认 maps 曾只取 0:v:0，产出**完全没有音轨**。


def test_setts_bsf_shifts_only():
    """setts 只平移时间戳、不重排帧：2026-09-27 A/B 实测，
    time_base=1/fps:ts=N:duration=1 会把 60s 输入退化成 1 个 100MB+ 分片；
    PTS-STARTPTS/DTS-STARTDTS 保留真实帧间隔（11 片）且归零成严格单调。"""

    class _V:
        frame_rate = 50.0

    class _S:
        video = _V()

    assert setts_bsf(_S()) == "setts=pts=PTS-STARTPTS:dts=DTS-STARTDTS"


def test_setts_bsf_frame_rate_irrelevant():
    """帧率缺失（0/None）不改变口径：移位字符串恒定，不可能出现 1/0。"""

    class _V:
        frame_rate = 0.0

    class _S:
        video = _V()

    assert setts_bsf(_S()) == "setts=pts=PTS-STARTPTS:dts=DTS-STARTDTS"


def test_default_maps_include_audio():
    """默认必须带上音轨——纯视频轨＝电视上完全没声音。"""
    argv = hls_command(Path("in.mkv"), Path("v.m3u8"))
    joined = " ".join(argv)
    assert "0:a:0?" in joined  # 带 ? ：无音轨的源也不至于让 ffmpeg 报错
    assert "-c:a copy" in joined


def test_setts_joins_the_single_bsf_chain():
    """-bsf:v 只吃一条链（多个 -bsf 参数 ffmpeg 只留最后一个，实测）。

    setts 必须与参数集注入/VUI 重写**合并在同一条**逗号链里，且排在最后：
    它只改时间戳，放最后不影响前面几环对参数集与 SPS 的改写。
    """
    argv = hls_command(
        Path("in.mkv"),
        Path("v.m3u8"),
        bsf=("hevc_mp4toannexb", "extract_extradata"),
        setts="setts=time_base=1/50:ts=N:duration=1",
    )
    assert argv.count("-bsf:v") == 1
    chain = argv[argv.index("-bsf:v") + 1]
    parts = chain.split(",")
    assert parts[0] == "hevc_mp4toannexb"
    assert "extract_extradata" in parts
    assert parts[-1].startswith("setts=")


def test_no_setts_when_normalization_disabled():
    """关掉规整 → 链里不该出现 setts（用于复现旧行为，不是默认路径）。"""
    argv = hls_command(Path("in.mkv"), Path("v.m3u8"), bsf=("extract_extradata",))
    chain = argv[argv.index("-bsf:v") + 1]
    assert "setts" not in chain


def test_command_writes_colr_signal_by_default():
    """DV rip 无 colour tag：init 段必须显式带 PQ/BT.2020 的 colr 盒。

    2026-09-26 实测：不写 colr 时 AVPlayer 按 SDR BT.709 映射——真机
    「颜色不对 + 徽标不亮」的直接根因。
    """
    argv = hls_command(Path("in.mkv"), Path("out/video.m3u8"))
    joined = " ".join(argv)
    assert "-color_primaries bt2020" in joined
    assert "-color_trc smpte2084" in joined
    assert "-colorspace bt2020nc" in joined
    assert "-color_range tv" in joined


def test_command_honours_source_colorimetry():
    """HLG 源（compat_id=4）沿用它自己的 transfer，不套 PQ 默认。"""
    argv = hls_command(
        Path("in.mkv"),
        Path("out/video.m3u8"),
        colorimetry=("bt2020", "arib-std-b67", "bt2020nc"),
    )
    joined = " ".join(argv)
    assert "-color_trc arib-std-b67" in joined
    assert "smpte2084" not in joined


def test_command_signals_full_range_when_requested():
    """全色域源 → -color_range pc。2026-09-26 像素级实测（signalstats 逐帧
    YMIN=0/YMAX=1016，limited 黑位应为 64）证明测试片底层是全色域 10bit，
    写死 tv 会把黑位抬升、真机「颜色不对 + 徽标不亮」。"""
    argv = hls_command(Path("in.mkv"), Path("out/video.m3u8"), full_range=1)
    joined = " ".join(argv)
    assert "-color_range pc" in joined
    assert "-color_range tv" not in joined


def test_resolve_output_reads_init_uri_from_playlist(tmp_path: Path):
    (tmp_path / "video.m3u8").write_text(
        '#EXTM3U\n#EXT-X-MAP:URI="init.mp4"\n#EXTINF:6.0,\ns0.m4s\n#EXTINF:6.0,\ns1.m4s\n',
        encoding="utf-8",
    )
    (tmp_path / "init.mp4").write_bytes(b"\0")
    (tmp_path / "s0.m4s").write_bytes(b"\0")
    (tmp_path / "s1.m4s").write_bytes(b"\0")
    output = resolve_output(tmp_path / "video.m3u8")
    assert output.init.name == "init.mp4"
    assert [p.name for p in output.segments] == ["s0.m4s", "s1.m4s"]
    assert output.exists


def test_resolve_output_rejects_mpegts_playlist(tmp_path: Path):
    (tmp_path / "v.m3u8").write_text("#EXTM3U\n#EXTINF:6.0,\ns0.ts\n", encoding="utf-8")
    with pytest.raises(RemuxError, match="EXT-X-MAP"):
        resolve_output(tmp_path / "v.m3u8")


def test_remux_refuses_non_dovi_source(monkeypatch, tmp_path: Path):
    import dvpack.remux as module

    plain = parse_probe_json(
        {"streams": [{"index": 0, "codec_type": "video", "codec_name": "hevc"}], "format": {}},
        Path("x.mkv"),
    )
    monkeypatch.setattr(module, "probe", lambda _path: plain)
    with pytest.raises(RemuxError, match="DOVI"):
        remux("x.mkv", tmp_path)


@needs_source
def test_end_to_end_resign_lands_on_apple_template(tmp_path: Path):
    """转签落地的端到端判据：init 段的 dvcC 必须逐字节等于 Apple 官方 P5 模板。

    只比 ffprobe 字段不够——ffprobe 读 dvvC 与 dvcC 都返回同一组值，看不出差别；
    比字节模板 + 比 box 名，才是 tvOS 真正看到的形态。
    """
    _, output = remux(SRC, tmp_path, seconds=6, segment_seconds=2)
    data = output.init.read_bytes()
    record = DvcConfig.parse(next(iter(locate_dv_boxes(data))).payload)
    assert (record.profile, record.level, record.rpu_present, record.el_present,
            record.bl_present, record.compat_id) == (5, record.level, True, False, True, 0)
    assert record.encode()[:5].hex().startswith("01000a35"), record.encode().hex()
    assert b"dvcC" in data and b"dvvC" not in data


@needs_source
def test_end_to_end_rpu_is_self_consistent_for_profile_5(tmp_path: Path):
    """单层 P5 的 RPU 必须自洽——这是"徽标能不能亮"的可观测代理。

    P7 源丢 EL 后 RPU 仍带 NLQ、未禁用残差，是伪 P5，tvOS 拒播；真机实验的
    教训就是别只信信令。这条保证 P8 通路的产物不犯同一个错。
    """
    _, output = remux(SRC, tmp_path, seconds=6, segment_seconds=2)
    assert check_rpu_consistency(output) == ()


@needs_source
def test_end_to_end_output_carries_audio(tmp_path: Path):
    """端到端：产物里必须有音轨。真机症状「有画面没声音」的直接回归闸。"""
    import re
    import subprocess

    _, output = remux(SRC, tmp_path, seconds=6, segment_seconds=2)
    probe = FFMPEG.parent / "ffprobe"
    out = subprocess.run(
        [str(probe), "-v", "error", "-show_entries", "stream=codec_type",
         "-of", "csv=p=0", str(output.playlist)],
        capture_output=True, text=True,
    )
    assert "audio" in out.stdout, out.stdout + out.stderr


@needs_source
def test_end_to_end_no_stub_tail_segment(tmp_path: Path):
    """端到端：不许出现「几帧的残尾段」。

    真机黑屏的形态就是末段 EXTINF:0.060000（3 帧）而 TARGETDURATION=6——
    AVPlayer 拿到这种片会停在一个无法呈现的缓冲区上，黑屏且不再发请求。
    两道防线：setts 把时间戳规整成 CFR，trim_stub_tail 再把过短的片尾摘掉。
    """
    import re

    _, output = remux(SRC, tmp_path, seconds=6, segment_seconds=2)
    durations = [
        float(m)
        for m in re.findall(r"#EXTINF:([\d.]+)", output.playlist.read_text())
    ]
    assert durations, "列表里没有 EXTINF"
    assert min(durations) > 0.5, f"出现残尾分片：{durations}"


@needs_source
def test_end_to_end_timestamps_are_monotonic(tmp_path: Path):
    """端到端：视频时间戳必须单调递增——这是黑屏最直接的根因。

    修复前实测同一源非单调对数 1471/3003（末段 60.060→60.220→60.140 乱序）。
    没有 setts 时 `-c copy` 会把源的乱序 DTS 原样搬进分片，AVPlayer 随即停播。
    """
    import subprocess

    _, output = remux(SRC, tmp_path, seconds=6, segment_seconds=2)
    probe = FFMPEG.parent / "ffprobe"
    out = subprocess.run(
        [str(probe), "-v", "error", "-select_streams", "v",
         "-show_entries", "packet=pts_time", "-of", "csv=p=0",
         str(output.playlist)],
        capture_output=True, text=True,
    )
    stamps = [float(x) for x in out.stdout.split() if x.strip()]
    assert len(stamps) > 10, f"没读到视频包：{out.stderr}"
    bad = [i for i, (a, b) in enumerate(zip(stamps, stamps[1:])) if b <= a]
    assert not bad, f"{len(bad)} 处时间戳非单调，首处在第 {bad[0]} 个包"
    assert stamps[0] == pytest.approx(0.0, abs=0.04), stamps[0]


def test_trim_stub_tail_drops_short_trailing_segments(tmp_path: Path):
    """trim_stub_tail：只从尾部删过短分片，且真删文件、重写列表。"""
    from dvpack.remux import RemuxOutput, trim_stub_tail

    segs = []
    for i, dur in enumerate((5.0, 5.0, 5.0, 0.06)):
        p = tmp_path / f"s{i}.m4s"
        p.write_bytes(b"x")
        segs.append(p)
    pl = tmp_path / "v.m3u8"
    pl.write_text(
        "#EXTM3U\n#EXT-X-MAP:URI=\"init.mp4\"\n"
        + "".join(f"#EXTINF:{d},\ns{i}.m4s\n" for i, d in enumerate((5.0, 5.0, 5.0, 0.06)))
        + "#EXT-X-ENDLIST\n"
    )
    out = trim_stub_tail(RemuxOutput(pl, tmp_path / "init.mp4", tuple(segs)))
    text = pl.read_text()
    assert "s3.m4s" not in text, text
    assert not segs[3].exists(), "残尾分片文件应被删除"
    assert len(out.segments) == 3
    assert all(s.exists() for s in out.segments)


def test_trim_stub_tail_keeps_first_segment_even_if_short(tmp_path: Path):
    """保底：只有一段时绝不删空——空列表比残尾更糟。"""
    from dvpack.remux import RemuxOutput, trim_stub_tail

    p = tmp_path / "s0.m4s"
    p.write_bytes(b"x")
    pl = tmp_path / "v.m3u8"
    pl.write_text("#EXTM3U\n#EXTINF:0.06,\ns0.m4s\n#EXT-X-ENDLIST\n")
    out = trim_stub_tail(RemuxOutput(pl, tmp_path / "init.mp4", (p,)))
    assert len(out.segments) == 1
    assert p.exists()


def test_trim_stub_tail_is_noop_when_all_segments_are_long(tmp_path: Path):
    """没有短片尾时原样返回，列表与文件都不该被动。"""
    from dvpack.remux import RemuxOutput, trim_stub_tail

    segs = []
    for i in range(2):
        p = tmp_path / f"s{i}.m4s"
        p.write_bytes(b"x")
        segs.append(p)
    pl = tmp_path / "v.m3u8"
    body = "#EXTM3U\n#EXTINF:5.0,\ns0.m4s\n#EXTINF:5.0,\ns1.m4s\n#EXT-X-ENDLIST\n"
    pl.write_text(body)
    out = trim_stub_tail(RemuxOutput(pl, tmp_path / "init.mp4", tuple(segs)))
    assert pl.read_text() == body
    assert len(out.segments) == 2


_P7_SRC = Path(os.environ.get("DVPACK_SOURCE_P7_MKV", ""))

needs_p7_source = pytest.mark.skipif(
    not (_P7_SRC.is_file() and FFMPEG.exists()), reason="缺 P7 片源或 ffmpeg"
)


@needs_p7_source
def test_verify_reports_inconsistent_rpu_as_problem(tmp_path: Path):
    """反面控制：mode 1 造出来的伪 P5 必须被 RPU 自洽性判据抓到。

    真机教训：P7 源丢 EL 后 RPU 仍是 nlq=linear_dz、残差未禁用——信令转签得
    再看不出破绽，tvOS 也拒播。mode 5 是修复姿势（nlq=none + 残差禁用）：
    这条用例走同一条链路、只把 dovi_tool convert 的模式换成 1，保证判据
    闻得出没走 mode 5 的产物。
    """
    import subprocess
    import sys

    out = tmp_path / "dv_p7"
    proc = subprocess.run(
        [sys.executable, "scripts/p7_to_p5.py", str(_P7_SRC), "-o", str(out), "--seconds", "6", "--mode", "1"],
        capture_output=True, text=True, encoding="utf-8",
    )
    assert proc.returncode == 0, proc.stderr
    output = resolve_output(out / "video.m3u8")
    problems = check_rpu_consistency(output)
    assert any("RPU" in problem for problem in problems), problems


@needs_source
def test_end_to_end_produces_dvh1_with_config_box(tmp_path: Path):
    verification = remux_and_verify(SRC, tmp_path, seconds=6, segment_seconds=2)
    assert verification.ok, verification.problems
    assert verification.signalling == "dvh1+dvcC"
    # 源是 P8：信令必须按 Apple 规范转签成 P5，而不是"和源逐字段相同"
    assert verification.dovi_relabelled
    assert verification.output_dovi.profile == 5
    assert verification.target_dovi.profile == 5


@needs_source
def test_missing_strict_unofficial_loses_the_config_box(tmp_path: Path):
    """反面控制：只改 fourcc 不给 `-strict unofficial`，DV 信令就不成立。"""
    playlist = tmp_path / "video.m3u8"
    argv = [
        str(FFMPEG), "-hide_banner", "-loglevel", "error", "-y", "-t", "6", "-i", str(SRC),
        "-map", "0:v:0", "-c", "copy", "-tag:v", "dvh1",
        "-f", "hls", "-hls_segment_type", "fmp4", "-hls_time", "2", "-hls_list_size", "0",
        "video.m3u8",
    ]
    subprocess.run(argv, capture_output=True, text=True, encoding="utf-8", check=True, cwd=tmp_path)
    video = parse_file(resolve_output(playlist).init).video
    assert video.sample_entry_type == "dvh1", "fourcc 改名与配置 box 是两件事"
    assert video.dvcc is None, "少了 -strict unofficial 却写出了配置 box，说明判据失效"
    assert video.signalling == "dvh1 但缺 dvC"


def test_hls_command_passes_bsf_chain_as_single_argument():
    """多个 -bsf 参数 ffmpeg 只保留最后一个（2026-09-26 实测），必须一次喂成逗号链。"""
    argv = hls_command(
        Path("in.mkv"), Path("out/video.m3u8"), seconds=30, bsf=PARAM_SET_INJECTION_BSF
    )
    index = argv.index("-bsf:v")
    assert argv[index + 1] == "hevc_mp4toannexb,extract_extradata"


def test_param_set_injection_bsf_selector():
    """MKV extradata 只有 hvcC 头 → 注入；mp4 自带参数集 → 不注入。"""
    def source(format_name: str) -> SourceInfo:
        video = VideoStream(
            index=0,
            codec="hevc",
            profile="Main 10",
            width=3840,
            height=2160,
            bit_depth=10,
            frame_rate=23.976,
            color_transfer="",
            color_space="",
            color_primaries="",
            dovi=None,
        )
        return SourceInfo(
            path=Path("in.mkv"),
            format_name=format_name,
            duration=1.0,
            size=1,
            video=video,
        )

    assert param_set_injection_bsf(source("matroska,webm")) == PARAM_SET_INJECTION_BSF
    assert param_set_injection_bsf(source("mov,mp4,m4a,3gp,3g2,mj2")) == ()


def _mk_source(color_range: str, format_name: str = "matroska,webm") -> SourceInfo:
    video = VideoStream(
        index=0,
        codec="hevc",
        profile="Main 10",
        width=3840,
        height=2160,
        bit_depth=10,
        frame_rate=23.976,
        color_transfer="",
        color_space="",
        color_primaries="",
        dovi=None,
        color_range=color_range,
    )
    return SourceInfo(
        path=Path("in.mkv"),
        format_name=format_name,
        duration=1.0,
        size=1,
        video=video,
    )


def test_dv_sps_vui_bsf_constant_limited():
    """range 位恒 limited：DV P5 的 BL 按 Dolby 规范就是 limited，
    2026-09-27 真机实证透传容器 pc 徽标不亮，「按源推导」分支已删。

    scripts/p7_to_p5.py 断言 len(DV_SPS_VUI_BSF)==1（P7 BL 本就 limited），
    常量必须继续是一个元素的 tuple。
    """
    assert dv_sps_vui_bsf(1) == (
        "hevc_metadata=video_full_range_flag=0:"
        "colour_primaries=9:transfer_characteristics=16:matrix_coefficients=9",
    )
    assert dv_sps_vui_bsf(0)[0].startswith(
        "hevc_metadata=video_full_range_flag=0:"
    )
    assert DV_SPS_VUI_BSF == dv_sps_vui_bsf()
    assert len(DV_SPS_VUI_BSF) == 1


def test_source_full_range_always_limited():
    """容器 color_range 一律签 limited：P5 BL 恒 limited（点亮版口径）。"""
    for token in ("pc", "full", "jpeg", "PC"):
        assert source_full_range(_mk_source(token)) == 0, token
    for token in ("tv", "limited", "mpeg", "unknown", ""):
        assert source_full_range(_mk_source(token)) == 0, token


def test_dv_signalling_bsf_constant_limited():
    """不按源推导 range了：恒定 flag=0（P5 BL 点亮口径）；
    MKV 链保持 annexb 化 + extradata 回填。"""
    assert dv_signalling_bsf(_mk_source("pc")) == (
        "hevc_mp4toannexb",
        "hevc_metadata=video_full_range_flag=0:"
        "colour_primaries=9:transfer_characteristics=16:matrix_coefficients=9",
        "extract_extradata",
    )
    assert "video_full_range_flag=0" in ",".join(dv_signalling_bsf(_mk_source("tv")))
    assert dv_signalling_bsf(_mk_source("pc", "mov,mp4,m4a")) == dv_sps_vui_bsf(1)


@needs_source
def test_mkv_remux_init_hvcC_carries_param_sets(tmp_path: Path):
    """真机停播根因的回归闸门：MKV 源 remux 后 init 段 hvcC 必须自带 VPS/SPS/PPS。

    2026-09-26 真机实证：参数集只走分片 in-band 时，tvOS 拉完 init 与
    video0.m4s 后再不拉下一分片；BSF 链注入后 hvcC 23B → 183B。
    """
    _, output = remux(SRC, tmp_path / "out", seconds=6, segment_seconds=6.0)
    types = hvcC_param_set_types(output.init.read_bytes())
    assert {32, 33, 34} <= set(types), f"init 段 hvcC 参数集缺失，现有 {types}"

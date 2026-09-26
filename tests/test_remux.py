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


def test_dv_sps_vui_bsf_follows_source_range():
    """range 位随源；颜色三件套恒为 9/16/9，兼容常量保 limited。

    scripts/p7_to_p5.py 断言 len(DV_SPS_VUI_BSF)==1（P7 BL 本就 limited），
    常量必须继续是一个元素的 tuple。
    """
    assert dv_sps_vui_bsf(1) == (
        "hevc_metadata=video_full_range_flag=1:"
        "colour_primaries=9:transfer_characteristics=16:matrix_coefficients=9",
    )
    assert dv_sps_vui_bsf(0)[0].startswith(
        "hevc_metadata=video_full_range_flag=0:"
    )
    assert DV_SPS_VUI_BSF == dv_sps_vui_bsf()
    assert len(DV_SPS_VUI_BSF) == 1


def test_source_full_range_maps_container_tokens():
    """容器 color_range → range 位：pc/full/jpeg 全色域，其余保守 limited。"""
    for token in ("pc", "full", "jpeg", "PC"):
        assert source_full_range(_mk_source(token)) == 1, token
    for token in ("tv", "limited", "mpeg", "unknown", ""):
        assert source_full_range(_mk_source(token)) == 0, token


def test_dv_signalling_bsf_derives_range_from_source():
    """不显式给 full_range 时按源推导；MKV 链保持 annexb 化 + extradata 回填。"""
    assert dv_signalling_bsf(_mk_source("pc")) == (
        "hevc_mp4toannexb",
        "hevc_metadata=video_full_range_flag=1:"
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

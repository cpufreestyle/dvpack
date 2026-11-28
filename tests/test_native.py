"""原生播放器链路（dvpack.native）的离线测试。

JSON 样本沿用 tests/test_probe.py 的做法：真实 ffprobe 输出结构，不打 ffmpeg。
`remux_native` 用假 subprocess 驱动，专门锁两类曾经真跑挂过的行为：
  · 命令必须写临时名，且 `.part` 要夹在后缀之前（`x.mp4.part` 会让 mp4 muxer 拒开）；
  · 复检不过必须删掉产物，不能留一个看着像成片的东西进片库。
"""

from __future__ import annotations

import copy
from pathlib import Path

import pytest
from dvpack import native
from dvpack.probe import parse_probe_json

VIDEO_P5 = {
    "index": 0,
    "codec_type": "video",
    "codec_name": "hevc",
    "profile": "Main 10",
    "width": 3840,
    "height": 2160,
    "bit_depth": 10,
    "color_range": "tv",
    "color_space": "bt2020nc",
    "color_transfer": "smpte2084",
    "color_primaries": "bt2020",
    "avg_frame_rate": "50/1",
    "r_frame_rate": "50/1",
    "side_data_list": [
        {
            "side_data_type": "DOVI configuration record",
            "dv_version_major": 1,
            "dv_version_minor": 0,
            "dv_profile": 5,
            "dv_level": 7,
            "rpu_present_flag": 1,
            "el_present_flag": 0,
            "bl_present_flag": 1,
            "dv_bl_signal_compatibility_id": 0,
            "dv_md_compression": "none",
        }
    ],
}

DOVI = VIDEO_P5["side_data_list"][0]


def source(*, fmt="mov,mp4,m4a,3gp,3g2,mj2", **video_overrides) -> "native.SourceInfo":
    video = copy.deepcopy(VIDEO_P5)
    for key, value in video_overrides.items():
        if value is None:
            video.pop(key, None)
            if key == "side_data_list":
                video["side_data_list"] = []
        else:
            video[key] = value
    streams = [video, {"index": 1, "codec_type": "audio", "codec_name": "eac3"}]
    return parse_probe_json(
        {"streams": streams, "format": {"format_name": fmt, "duration": "30.0", "size": "54940651"}},
        Path("/movie/f.mp4"),
    )


def test_conformant_mp4_is_ok():
    verdict = native.classify(source(), "dvcC", "dvh1")
    assert verdict.action == native.ACTION_OK
    assert verdict.signalling == "dvh1+dvcC"


def test_full_range_mkv_needs_fix_and_says_why():
    verdict = native.classify(source(fmt="matroska,webm", color_range="pc"), "", "")
    assert verdict.action == native.ACTION_FIX
    joined = " ".join(verdict.reasons)
    assert "容器" in joined and "color_range=pc" in joined


def test_missing_color_trio_needs_fix():
    verdict = native.classify(
        source(color_primaries="unknown", color_transfer="unknown", color_space="unknown"),
        "dvcC",
        "dvh1",
    )
    assert verdict.action == native.ACTION_FIX
    assert any("色彩三件套" in r for r in verdict.reasons)


def test_wrong_sample_entry_or_dv_box_needs_fix():
    assert any("sample entry" in r for r in native.classify(source(), "dvcC", "hev1").reasons)
    assert any("dvcC" in r for r in native.classify(source(), "dvvC", "dvh1").reasons)


def test_plain_hdr10_is_skipped_not_fixed():
    verdict = native.classify(source(side_data_list=None), "", "hev1")
    assert verdict.action == native.ACTION_SKIP
    assert "不是杜比视界" in verdict.reasons[0]


def test_dvh1_without_record_is_fixable_not_skipped():
    """被 naive ffmpeg copy 洗过的形态：fourcc 还在、DOVI record 没了，不能当非 DV 跳过。"""
    verdict = native.classify(source(side_data_list=None), "", "dvh1")
    assert verdict.action == native.ACTION_FIX
    assert "dvcC 丢失" in verdict.reasons[0]


def test_dual_layer_and_unknown_profile_are_skipped():
    dual = copy.deepcopy(DOVI)
    dual["el_present_flag"] = 1
    p8 = copy.deepcopy(DOVI)
    p8.update({"dv_profile": 8, "dv_bl_signal_compatibility_id": 1})
    odd = copy.deepcopy(DOVI)
    odd["dv_profile"] = 7
    assert native.classify(source(side_data_list=[dual]), "dvcC", "dvh1").action == native.ACTION_SKIP
    assert native.classify(source(side_data_list=[odd]), "dvcC", "dvh1").action == native.ACTION_SKIP
    verdict8 = native.classify(source(side_data_list=[p8]), "dvcC", "dvh1")
    assert verdict8.action == native.ACTION_FIX
    assert any("Profile 8" in r for r in verdict8.reasons)


def test_no_rpu_is_skipped():
    no_rpu = copy.deepcopy(DOVI)
    no_rpu["rpu_present_flag"] = 0
    assert native.classify(source(side_data_list=[no_rpu]), "dvcC", "dvh1").action == native.ACTION_SKIP


def test_non_hevc_is_skipped():
    assert native.classify(source(codec_name="av1"), "dvcC", "dvh1").action == native.ACTION_SKIP


# --------------------------------------------------------------------------- 命令组装


def test_native_command_carries_verified_signalling():
    cmd = native.native_command(Path("/a/b.mkv"), Path("/o/b-dv.mp4"), source(fmt="matroska,webm"))
    text = " ".join(cmd)
    assert "-c copy" in text, "原生口径只搬码流，重编码会毁掉 RPU"
    assert "-tag:v dvh1 -strict unofficial" in text
    for flag in ("-color_range tv", "-color_primaries bt2020", "-color_trc smpte2084", "-colorspace bt2020nc"):
        assert flag in text, f"原生口径要显式签满色彩三件套 + limited：缺 {flag}"
    assert "-map 0:v:0 -map 0:a:0?" in text, "与 2026-11-22 验收样本同一选流口径（不带字幕）"
    # MKV 源要注入参数集 + 重写 SPS VUI 的 range 位
    assert "hevc_mp4toannexb" in text and "extract_extradata" in text
    assert "hevc_metadata=video_full_range_flag=0:colour_primaries=9:transfer_characteristics=16:matrix_coefficients=9" in text


def test_native_command_skips_annexb_chain_for_mp4_source():
    cmd = " ".join(native.native_command(Path("/a/b.mp4"), Path("/o/b-dv.mp4"), source()))
    assert "hevc_mp4toannexb" not in cmd, "MP4 源自带参数集，无谓改写会白搬一遍"
    assert "hevc_metadata=" in cmd


def test_mirror_target_keeps_library_layout():
    root = Path("/nas/movie2")
    dst = native.mirror_target(root / "山/1080p.mkv", root, Path("/out"), "-dv")
    assert dst == Path("/out/山/1080p-dv.mp4"), "目录层级要保持，媒体中心/刮削才认得"


def test_iter_media_skips_hidden_and_underscore_dirs(tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "_subs").mkdir()
    (tmp_path / ".cache").mkdir()
    (tmp_path / "a" / "movie.mkv").write_bytes(b"x")
    (tmp_path / "a" / "note.txt").write_text("x")
    (tmp_path / "_subs" / "srt.mkv").write_bytes(b"x")
    (tmp_path / ".cache" / "c.mp4").write_bytes(b"x")
    found = [p.relative_to(tmp_path).as_posix() for p in native.iter_media([tmp_path])]
    assert found == ["a/movie.mkv"]


def test_summarize_counts_and_size():
    rows = [
        native.AuditRow("a", native.ACTION_FIX, "matroska", "pc/x/y/z", "P5", "", "容器", 1024),
        native.AuditRow("b", native.ACTION_OK, "mp4", "tv/x/y/z", "P5", "dvh1+dvcC", "", 1024),
        native.AuditRow("c", native.ACTION_SKIP, "mp4", "", "", "", "非 DV", 1024),
    ]
    text = native.summarize(rows)
    assert "共 3 个文件" in text and "fix=1" in text and "ok=1" in text and "skip=1" in text


# --------------------------------------------------------------------------- 落盘行为


class _Proc:
    def __init__(self, returncode=0, stderr=""):
        self.returncode = returncode
        self.stderr = stderr
        self.stdout = ""


def _stub_run(monkeypatch, *, returncode=0, stderr=""):
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(list(argv))
        if returncode == 0:
            Path(argv[-1]).write_bytes(b"payload")
        return _Proc(returncode, stderr)

    monkeypatch.setattr(native.subprocess, "run", fake_run)
    return calls


def _stub_verdict_ok(monkeypatch):
    monkeypatch.setattr(
        native,
        "inspect",
        lambda p: native.NativeVerdict(
            native.ACTION_OK, container="mov,mp4", colour="tv/bt2020/smpte2084/bt2020nc",
            dovi="P5", signalling="dvh1+dvcC",
        ),
    )
    monkeypatch.setattr(native, "_rpu_bytes", lambda p: 1234)


def test_remux_native_writes_temp_name_then_renames(tmp_path, monkeypatch):
    """回归：命令必须写临时名（否则复检读的是半成品），且 `.part` 要在扩展名之前。"""
    monkeypatch.setattr(native, "probe", lambda p: source(fmt="matroska,webm"))
    _stub_verdict_ok(monkeypatch)
    calls = _stub_run(monkeypatch)
    dst = tmp_path / "out" / "m-dv.mp4"

    res = native.remux_native(tmp_path / "m.mkv", dst)

    assert res.status == "fixed"
    assert dst.read_bytes() == b"payload"
    assert Path(calls[0][-1]).name == "m-dv.part.mp4"
    assert not list(tmp_path.rglob("*.part*")), "临时名要收干净"


def test_remux_native_discards_output_when_verify_fails(tmp_path, monkeypatch):
    monkeypatch.setattr(native, "probe", lambda p: source(fmt="matroska,webm"))
    monkeypatch.setattr(
        native,
        "inspect",
        lambda p: native.NativeVerdict(
            native.ACTION_FIX, ("容器里没有 DOVI configuration record",), signalling="dvh1+（无 dvC）"
        ),
    )
    _stub_run(monkeypatch)
    dst = tmp_path / "out" / "m-dv.mp4"

    res = native.remux_native(tmp_path / "m.mkv", dst)

    assert res.status == "verify-fail"
    assert not dst.exists(), "复检不过不能留成片进片库"
    assert not list(tmp_path.rglob("*.part*"))


def test_remux_native_reports_ffmpeg_failure_without_touching_old_output(tmp_path, monkeypatch):
    monkeypatch.setattr(native, "probe", lambda p: source(fmt="matroska,webm"))
    _stub_verdict_ok(monkeypatch)
    _stub_run(monkeypatch, returncode=1, stderr="Invalid argument")
    dst = tmp_path / "out" / "m-dv.mp4"
    dst.parent.mkdir(parents=True)
    dst.write_bytes(b"previous")

    res = native.remux_native(tmp_path / "m.mkv", dst, force=True)

    assert res.status == "failed"
    assert "Invalid argument" in res.detail
    assert dst.read_bytes() == b"previous"


def test_remux_native_refuses_existing_output_without_force(tmp_path, monkeypatch):
    monkeypatch.setattr(native, "probe", lambda p: source(fmt="matroska,webm"))
    calls = _stub_run(monkeypatch)
    dst = tmp_path / "out" / "m-dv.mp4"
    dst.parent.mkdir(parents=True)
    dst.write_bytes(b"keep me")

    res = native.remux_native(tmp_path / "m.mkv", dst)

    assert res.status == "exists"
    assert calls == [] and dst.read_bytes() == b"keep me"


def test_remux_native_dry_run_prints_command_and_writes_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(native, "probe", lambda p: source(fmt="matroska,webm"))
    res = native.remux_native(tmp_path / "m.mkv", tmp_path / "out" / "m-dv.mp4", dry_run=True)
    assert res.status == "dry-run"
    assert "-tag:v dvh1" in res.detail
    assert not (tmp_path / "out").exists()

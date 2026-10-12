"""master playlist 的写法测试。

重点是那三个决定电视是否走 DV 分支的属性：CODECS、VIDEO-RANGE、HDCP-LEVEL，
 2026-10-03 起 CODECS 视频段只写 hvc1 基线（level 封顶 153），DV 信令一律
 走 SUPPLEMENTAL-CODECS：dvh1.* 写进 CODECS 会被 tvOS 按 general_profile_idc=5
 解析、整份 master 拒收（真机 ITEM FAILED -11848，一个分片都不请求）。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from dvpack.playlist import (
    bandwidth_from_segments,
    master_playlist,
    parse_stream_inf,
    playlist_segment_seconds,
    variant_from_master,
    variant_from_source,
    write_master_playlist,
)
from dvpack.probe import parse_probe_json
from dvpack.subs import write_subtitle_tracks


def _source(*, stream_overrides: dict | None = None, audio: str | None = None, **side) -> object:
    """造一份 SourceInfo：side 改 DOVI 字段，stream_overrides 改容器实测字段。

    audio 给 ffprobe 的音轨 codec_name（"aac" / "eac3"…）；None = 无音轨。
    """
    stream = {
        "index": 0,
        "codec_type": "video",
        "codec_name": "hevc",
        "width": 3840,
        "height": 2160,
        "color_transfer": "smpte2084",
        "avg_frame_rate": "24000/1001",
        "side_data_list": [
            {
                "side_data_type": "DOVI configuration record",
                "dv_version_major": 1,
                "dv_version_minor": 0,
                "dv_profile": 5,
                "dv_level": 6,
                "rpu_present_flag": 1,
                "el_present_flag": 0,
                "bl_present_flag": 1,
                "dv_bl_signal_compatibility_id": 0,
                "dv_md_compression": "none",
            }
        ],
    }
    stream["side_data_list"][0].update(side)
    stream.update(stream_overrides or {})
    streams = [stream]
    if audio:
        streams.append({"index": 1, "codec_type": "audio", "codec_name": audio})
    return parse_probe_json({"streams": streams, "format": {"duration": "10"}}, Path("x.mkv"))


def test_profile5_variant_carries_full_apple_signalling():
    variant = variant_from_source(_source(), "video.m3u8", bandwidth=13_000_000)
    line = variant.stream_inf()
    assert 'CODECS="hvc1.2.4.L153.B0"' in line
    assert "VIDEO-RANGE=PQ" in line
    assert "HDCP-LEVEL=TYPE-1" in line
    assert "RESOLUTION=3840x2160" in line
    assert 'SUPPLEMENTAL-CODECS="dvh1.05.06/db1p"' in line, "P5 同样带 base layer，必须写"


def test_frame_rate_rounds_up_not_down():
    line = variant_from_source(_source(), "v.m3u8", bandwidth=1).stream_inf()
    assert "FRAME-RATE=24" in line, "23.976 必须向上取整成 24"


# ── CODECS 必须列全变体里的每一种 codec（2026-09-27 真机黑屏回归闸）──────
# 打包改成「音视频同流」之后，产物里多了 aac，但 master 仍只写 dvh1.05.09。
# AVPlayer 校验不过 → 拒收整个 master：日志里只剩一次 /master.m3u8 200，
# 之后**一个分片都没请求**，屏幕全黑。这条把它钉死。


def test_codecs_lists_audio_when_variant_is_muxed():
    line = variant_from_source(_source(audio="aac"), "v.m3u8", bandwidth=1).stream_inf()
    # AAC 在 master CODECS 里写 RFC 6381 的 mp4a.40.2，不是 ffprobe 的 "aac"：
    # 写 "aac" 时 AVPlayer 拉完 master/video.m3u8 就拒收，init.mp4 零请求。
    assert 'CODECS="hvc1.2.4.L153.B0,mp4a.40.2"' in line


def test_codecs_omits_comma_when_source_has_no_audio():
    """无音轨的源不能留下悬空的逗号（那同样是非法 CODECS）。"""
    line = variant_from_source(_source(), "v.m3u8", bandwidth=1).stream_inf()
    assert 'CODECS="hvc1.2.4.L153.B0"' in line
    assert "CODECS=" in line and "hvc1.2.4.L153.B0," not in line


def test_eac3_source_declares_ec3():
    """ffprobe 的 eac3 要写成 RFC 6381 的 ec-3，不能把 ffprobe 的名字直接搬。"""
    line = variant_from_source(_source(audio="eac3"), "v.m3u8", bandwidth=1).stream_inf()
    assert 'CODECS="hvc1.2.4.L153.B0,ec-3"' in line


def test_unknown_audio_codec_is_left_out():
    """不认识的音频 codec 宁可漏写也不能写错——写错会让整个 master 变非法。"""
    line = variant_from_source(_source(audio="some_weird_codec"), "v.m3u8", bandwidth=1).stream_inf()
    assert 'CODECS="hvc1.2.4.L153.B0"' in line


def test_audio_codec_can_be_overridden_to_none_for_split_variants():
    """音视频分离的变体：调用方传 None，CODECS 只写视频。"""
    src = _source(audio="aac")
    line = variant_from_source(src, "v.m3u8", bandwidth=1, audio_codec="").stream_inf()
    assert 'CODECS="hvc1.2.4.L153.B0"' in line


def test_profile81_gets_hdr10_compat_brand():
    dovi8 = _source(dv_profile=8, dv_level=7, dv_bl_signal_compatibility_id=1)
    line = variant_from_source(dovi8, "v.m3u8", bandwidth=1).stream_inf()
    assert 'CODECS="hvc1.2.4.L153.B0"' in line
    assert 'SUPPLEMENTAL-CODECS="dvh1.08.07/db1p"' in line


def test_profile84_pairs_with_hlg_brand():
    dovi84 = _source(
        stream_overrides={"color_transfer": ""},
        dv_profile=8,
        dv_level=7,
        dv_bl_signal_compatibility_id=4,
    )
    line = variant_from_source(dovi84, "v.m3u8", bandwidth=1).stream_inf()
    assert "VIDEO-RANGE=HLG" in line
    assert "db4h" in line


def test_no_dovi_record_refuses_to_build_variant():
    plain = parse_probe_json(
        {"streams": [{"index": 0, "codec_type": "video", "codec_name": "hevc"}], "format": {}},
        Path("x.mkv"),
    )
    with pytest.raises(ValueError, match="DOVI"):
        variant_from_source(plain, "v.m3u8", bandwidth=1)


def test_master_playlist_layout():
    variant = variant_from_source(_source(), "video.m3u8", bandwidth=100)
    text = master_playlist([variant])
    assert text.startswith("#EXTM3U\n#EXT-X-VERSION:7\n#EXT-X-INDEPENDENT-SEGMENTS\n")
    assert text.splitlines()[3].startswith("#EXT-X-STREAM-INF:")
    assert text.splitlines()[4] == "video.m3u8"
    assert text.endswith("\n")


def test_written_playlist_uses_lf_on_windows(tmp_path: Path):
    """Windows 文本模式会换成 CRLF；这条钉住我们写的是 LF。"""
    out = write_master_playlist(tmp_path / "master.m3u8", [variant_from_source(_source(), "v.m3u8", bandwidth=1)])
    assert b"\r\n" not in out.read_bytes()


def test_bandwidth_uses_measured_segment_bytes_with_margin(tmp_path: Path):
    for name, size in (("s0.m4s", 1000), ("s1.m4s", 3000)):
        (tmp_path / name).write_bytes(b"x" * size)
    segments = [tmp_path / "s0.m4s", tmp_path / "s1.m4s"]
    # 平均 3200 bps，峰值段 3000B / 5s = 4800 bps；BANDWIDTH 取峰值而不是均值。
    assert bandwidth_from_segments(segments, 10.0) == int(4800 * 1.2)
    # 逐段长与平均段长一致时，两条路必须给出同一个答案。
    assert bandwidth_from_segments(segments, 10.0, [5.0, 5.0]) == int(4800 * 1.2)
    with pytest.raises(ValueError):
        bandwidth_from_segments(segments, 0)


def test_bandwidth_never_drops_below_peak_segment_bitrate(tmp_path: Path):
    """DV 片源峰值能到均值 3 倍；写低会让 AVPlayer 配错缓冲、起播后卡死。"""
    sizes = [100] * 9 + [3000]
    for n, size in enumerate(sizes):
        (tmp_path / ("s%d.m4s" % n)).write_bytes(b"x" * size)
    segments = [tmp_path / ("s%d.m4s" % n) for n in range(len(sizes))]
    peak_bitrate = 3000 * 8 / 4.0
    declared = bandwidth_from_segments(segments, 40.0, [4.0] * 10)
    assert declared >= peak_bitrate
    assert declared == int(peak_bitrate * 1.2)


def test_bandwidth_uses_real_segment_durations_not_average(tmp_path: Path):
    """不给逐段长时，最长段会被按平均段长算爆——《纽伦堡》当场虚报 31%。

    ffmpeg 是 -hls_time 6.0 加 fMP4 关键帧切分，EXTINF 从 0.041s 到 14.917s。
    1437 段里字节最大的那条落在 14.9s 的长段上，按平均段长 6.19s 反推得
    92 Mbps，真峰值只有 70.5 Mbps。AVPlayer 按假峰值要缓冲，起播只会更重。
    """
    (tmp_path / "short.m4s").write_bytes(b"x" * 200)
    (tmp_path / "long.m4s").write_bytes(b"x" * 4000)
    segments = [tmp_path / "short.m4s", tmp_path / "long.m4s"]
    seconds = 12.0
    naive_peak = 4000 * 8 / (seconds / len(segments))
    real_peak = 4000 * 8 / 10.0
    assert naive_peak > real_peak
    # 缺省（等长切分口径）仍是虚高的那个，保持向后兼容
    assert bandwidth_from_segments(segments, seconds) == int(naive_peak * 1.2)
    # 给了逐段长就必须落到真峰值上
    assert bandwidth_from_segments(
        segments, seconds, [2.0, 10.0]) == int(real_peak * 1.2)


def test_bandwidth_rejects_mismatched_segment_durations(tmp_path: Path):
    """段长条数对不上就是拿错了列表：宁可报错，也不写一个错码率的 BANDWIDTH。"""
    (tmp_path / "s0.m4s").write_bytes(b"x" * 1000)
    segments = [tmp_path / "s0.m4s"]
    with pytest.raises(ValueError):
        bandwidth_from_segments(segments, 1.0, [1.0, 1.0])
    with pytest.raises(ValueError):
        bandwidth_from_segments(segments, 1.0, [0.0])


def test_bandwidth_ignores_below_average_packs(tmp_path: Path):
    """峰值不高于均值时不应被拉低——至少等于整片平均。"""
    for name, size in (("s0.m4s", 1000), ("s1.m4s", 1000), ("s2.m4s", 1000)):
        (tmp_path / name).write_bytes(b"x" * size)
    segments = [tmp_path / ("s%d.m4s" % n) for n in range(3)]
    assert bandwidth_from_segments(segments, 10.0) == int(3000 * 8 / 10 * 1.2)


def test_bandwidth_rejects_empty_segments():
    with pytest.raises(ValueError):
        bandwidth_from_segments([], 10.0)


def test_playlist_segment_seconds_reads_each_extinf(tmp_path: Path):
    """逐段 EXTINF：带宽算真峰值唯一的数据来源，坏行跳过不猜。"""
    playlist = tmp_path / "video.m3u8"
    playlist.write_text(
        """#EXTM3U
#EXT-X-TARGETDURATION:15
#EXTINF:10.833000,
video0.m4s
#EXTINF:0.041000,
video1.m4s
#EXT-X-ENDLIST
""",
        encoding="utf-8",
    )
    assert playlist_segment_seconds(playlist) == [10.833, 0.041]


def test_master_playlist_declares_subtitle_media_before_streams(tmp_path: Path):
    """#EXT-X-MEDIA 必须排在第一条 #EXT-X-STREAM-INF 之前（RFC 8216）。"""
    variant = variant_from_source(_source(), "video.m3u8", bandwidth=100)
    texts = {
        "zh": "1\n00:00:01,000 --> 00:00:03,000\n世界\n",
        "en": "1\n00:00:01,000 --> 00:00:03,000\nWorld\n",
    }
    tracks = write_subtitle_tracks(tmp_path, texts)
    lines = master_playlist([variant], subtitles=tracks).splitlines()
    media = [index for index, line in enumerate(lines) if line.startswith("#EXT-X-MEDIA")]
    streams = [index for index, line in enumerate(lines) if line.startswith("#EXT-X-STREAM-INF")]
    assert len(media) == 2 and max(media) < min(streams)
    # 2026-10-04 真机口径：两条轨 DEFAULT/AUTOSELECT 都 NO——系统轨起播
    # 自动挂载会和 dvplay 自绘双语字幕重影，且英文轨从没被 AVPlayer 拉过
    # （见 subs.py BILINGUAL_TRACKS 注释）。菜单里仍选得到，起播不碰字幕轨。
    assert 'LANGUAGE="zh"' in lines[media[0]] and "DEFAULT=NO" in lines[media[0]]
    assert "AUTOSELECT=NO" in lines[media[0]]
    assert 'LANGUAGE="en"' in lines[media[1]] and "DEFAULT=NO" in lines[media[1]]
    assert "AUTOSELECT=NO" in lines[media[1]]
    assert 'SUBTITLES="subs"' in lines[streams[0]]
    assert lines[streams[0] + 1] == "video.m3u8"


def test_master_playlist_without_subtitles_stays_clean():
    variant = variant_from_source(_source(), "video.m3u8", bandwidth=100)
    text = master_playlist([variant])
    assert "#EXT-X-MEDIA" not in text
    assert "SUBTITLES" not in text


# ── 字幕升级要解析旧 master：引号逗号 / 缺属性（subsource → subs 链）──────

class TestParseStreamInf:
    """CODECS="hvc1.2.4.L153.B0,ec-3" 的引号逗号不是属性分隔符。"""

    def test_quoted_comma_is_not_a_separator(self):
        line = ('#EXT-X-STREAM-INF:BANDWIDTH=1280000,'
                'CODECS="hvc1.2.4.L153.B0,ec-3",RESOLUTION=3840x2160')
        attrs = parse_stream_inf(line)
        assert attrs["CODECS"] == "hvc1.2.4.L153.B0,ec-3"
        assert attrs["RESOLUTION"] == "3840x2160"
        assert attrs["BANDWIDTH"] == "1280000"
        assert len(attrs) == 3

    def test_supplemental_codecs_survives_a_trailing_comma(self):
        line = ('#EXT-X-STREAM-INF:BANDWIDTH=100,VIDEO-RANGE=PQ,'
                'SUPPLEMENTAL-CODECS="dvh1.05.06/db1p",')
        attrs = parse_stream_inf(line)
        assert attrs["SUPPLEMENTAL-CODECS"] == "dvh1.05.06/db1p"
        assert attrs["VIDEO-RANGE"] == "PQ"

    def test_line_without_attributes_yields_empty_dict(self):
        assert parse_stream_inf("#EXT-X-STREAM-INF") == {}


class TestVariantFromMaster:
    """给已在播的包加字幕轨：变体必须逐字解析回来，一个属性都不能丢。"""

    def test_roundtrip_through_master_is_byte_identical(self):
        variant = variant_from_source(_source(audio="eac3"), "/_jf/abc/video.m3u8", bandwidth=13_000_000)
        text = master_playlist([variant])
        assert master_playlist([variant_from_master(text)]) == text

    def test_absolute_variant_uri_is_preserved(self):
        variant = variant_from_source(_source(), "/_jf/abc/video.m3u8", bandwidth=1_000)
        parsed = variant_from_master(master_playlist([variant]))
        assert parsed.uri == "/_jf/abc/video.m3u8"

    def test_resolution_and_frame_rate_come_back(self):
        variant = variant_from_source(_source(), "v.m3u8", bandwidth=1_000)
        parsed = variant_from_master(master_playlist([variant]))
        assert parsed.resolution == (3840, 2160)
        assert parsed.frame_rate == 24.0, "23.976 向上取整成 24 后再读回"
        assert parsed.hdcp_level == "TYPE-1"
        assert parsed.video_range == "PQ"

    def test_missing_optional_attributes_stay_none(self):
        text = ('#EXTM3U\n#EXT-X-VERSION:7\n'
                '#EXT-X-STREAM-INF:BANDWIDTH=100,CODECS="hvc1.2.4.L153.B0"\n'
                'video.m3u8\n')
        parsed = variant_from_master(text)
        assert parsed.hdcp_level is None
        assert parsed.supplemental_codecs is None
        assert parsed.resolution is None
        assert parsed.frame_rate is None
        assert parsed.video_range == "PQ", "缺 VIDEO-RANGE 时按 DV 包兜底"

    def test_master_without_stream_inf_raises(self):
        with pytest.raises(ValueError):
            variant_from_master("#EXTM3U\n#EXT-X-VERSION:7\n")

    def test_stream_inf_without_uri_raises(self):
        with pytest.raises(ValueError):
            variant_from_master("#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=100\n")

    def test_missing_bandwidth_raises(self):
        with pytest.raises(ValueError):
            variant_from_master("#EXTM3U\n#EXT-X-STREAM-INF:CODECS=\"hvc1\"\nvideo.m3u8\n")

"""双语字幕：SRT/WebVTT 转换与 HLS 切片。

覆盖三条红线：to_vtt 必须补 WEBVTT 头并丢掉 SRT 序号行（否则 AVPlayer 把整份
字幕轨拒收）；byte-range 段不能出现 EXTINF:0（header 并进第一条 cue 就是为了
这个）；切片拼回去必须等于源文件（分隔字节一个都不能丢错）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from dvpack.subs import (
    SubtitleTrack,
    master_has_subtitle_tracks,
    subtitle_playlist_text,
    to_vtt,
    upgrade_master_subtitles,
    vtt_chunks,
    write_subtitle_tracks,
)

from dvpack.playlist import Variant, write_master_playlist

# 覆盖全部分支的最小素材：序号行、逗号小数点、空行分隔、两条 cue。
SRT_ZH = "1\n00:00:01,000 --> 00:00:03,500\n世界\n\n2\n00:00:04,000 --> 00:00:06,000\n再见\n"
SRT_EN = "1\n00:00:01,000 --> 00:00:03,500\nWorld\n\n2\n00:00:04,000 --> 00:00:06,000\nBye\n"


def test_to_vtt_adds_header_drops_numbers_and_commas():
    out = to_vtt(SRT_ZH).decode("utf-8")
    assert out.startswith("WEBVTT\n\n")
    assert "00:00:01.000 --> 00:00:03.500" in out
    assert ",000 -->" not in out
    assert "1\n00:00" not in out
    assert "世界" in out


def test_to_vtt_existing_header_passes_through_once():
    once = to_vtt(SRT_ZH).decode("utf-8")
    twice = to_vtt(once).decode("utf-8")
    assert twice.count("WEBVTT") == 1
    assert twice.rstrip("\n") == once.rstrip("\n")


def test_vtt_chunks_pieces_rejoin_with_blank_separators():
    payload = to_vtt(SRT_ZH)
    pieces = [payload[start:stop] for start, stop in vtt_chunks(payload)]
    # span 是 (start, stop) 绝对边界，段间只隔一个换行（空行分隔符的前半）
    assert b"\n".join(pieces) == payload
    # 第一条段是 header，其后每条段含一条 cue
    assert len(pieces) == 3
    assert pieces[0] == b"WEBVTT\n"


def test_vtt_chunks_without_cues():
    assert vtt_chunks(b"") == []
    assert vtt_chunks(b"garbage") == [(0, 7)]


def test_subtitle_playlist_text_layout(tmp_path: Path):
    payload = to_vtt(SRT_ZH)
    (tmp_path / "sub_zh.vtt").write_bytes(payload)
    track = SubtitleTrack(language="zh", name="中文", vtt=tmp_path / "sub_zh.vtt", default=True)
    text = subtitle_playlist_text(track, tmp_path / "sub_zh.vtt", vtt_chunks(payload))
    lines = text.splitlines()
    assert lines[0] == "#EXTM3U"
    assert "#EXT-X-PLAYLIST-TYPE:VOD" in lines
    assert lines[-1] == "#EXT-X-ENDLIST"
    # header 并进第一条 cue：两条 cue 只产出两个段，不会出现 EXTINF:0.000
    assert text.count("#EXT-X-BYTERANGE:") == 2
    assert "EXTINF:0.000" not in text
    assert "EXTINF:2.500" in text
    assert "#EXT-X-TARGETDURATION:3" in lines
    first = next(line for line in lines if line.startswith("#EXT-X-BYTERANGE:"))
    assert first.endswith("@0"), "第一条段从文件头开始，覆盖 header + 首条 cue"


def test_subtitle_playlist_text_needs_at_least_one_cue(tmp_path: Path):
    payload = to_vtt(SRT_ZH)
    (tmp_path / "sub_zh.vtt").write_bytes(payload)
    track = SubtitleTrack(language="zh", name="中文", vtt=tmp_path / "sub_zh.vtt")
    with pytest.raises(ValueError):
        subtitle_playlist_text(track, tmp_path / "sub_zh.vtt", [])


def test_write_subtitle_tracks_bilingual(tmp_path: Path):
    tracks = write_subtitle_tracks(tmp_path, {"zh": SRT_ZH, "en": SRT_EN})
    assert [track.language for track in tracks] == ["zh", "en"]
    # 2026-10-04 真机口径：两条轨都 default=False，起播不自动挂字幕
    # （系统轨自动加载会和 App 自绘字幕重影，见 BILINGUAL_TRACKS 注释）
    assert tracks[0].default is False
    assert tracks[1].default is False
    assert tracks[1].default is False
    for name in ("sub_zh.vtt", "sub_zh.m3u8", "sub_en.vtt", "sub_en.m3u8"):
        assert (tmp_path / name).is_file()
    assert (tmp_path / "sub_zh.m3u8").read_text(encoding="utf-8").endswith("#EXT-X-ENDLIST\n")


def test_write_subtitle_tracks_single_language(tmp_path: Path):
    tracks = write_subtitle_tracks(tmp_path, {"en": SRT_EN})
    assert [track.language for track in tracks] == ["en"]
    assert not (tmp_path / "sub_zh.vtt").exists()


def test_write_subtitle_tracks_blank_text_writes_nothing(tmp_path: Path):
    assert write_subtitle_tracks(tmp_path, {"zh": "", "en": "   \n"}) == []
    assert list(tmp_path.iterdir()) == []


# ── 在线腿给已在播的包补字幕：只动 master，视频分片零改动 ─────────────────


def _minimal_pack(out_dir: Path) -> None:
    """造一个打到一半的最小 DV 包（master + 空视频列表 + 一个分片）。"""
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
    (out_dir / "video.m3u8").write_text("#EXTM3U\n#EXT-X-ENDLIST\n", encoding="utf-8")
    (out_dir / "0.m4s").write_bytes(b"\x00\x00\x00\x08free" + b"seg" * 32)


def test_upgrade_rewrites_master_and_leaves_segments_untouched(tmp_path: Path):
    _minimal_pack(tmp_path)
    before = {
        "0.m4s": (tmp_path / "0.m4s").read_bytes(),
        "video.m3u8": (tmp_path / "video.m3u8").read_bytes(),
    }
    tracks = upgrade_master_subtitles(tmp_path, {"zh": SRT_ZH, "en": SRT_EN})
    assert [track.language for track in tracks] == ["zh", "en"]
    upgraded = (tmp_path / "master.m3u8").read_text(encoding="utf-8")
    assert 'TYPE=SUBTITLES' in upgraded
    assert 'SUBTITLES="subs"' in upgraded
    assert 'URI="sub_zh.m3u8"' in upgraded
    assert 'URI="sub_en.m3u8"' in upgraded
    # 2026-10-04 真机口径：两条轨起播都不自动挂（与 App 自绘双语分工）
    media = [line for line in upgraded.splitlines() if line.startswith("#EXT-X-MEDIA")]
    assert len(media) == 2
    for line in media:
        assert "DEFAULT=NO" in line and "AUTOSELECT=NO" in line
    # #EXT-X-MEDIA 仍排在 STREAM-INF 之前（RFC 8216，升级路径同样适用）
    lines = upgraded.splitlines()
    assert max(i for i, l in enumerate(lines) if l.startswith("#EXT-X-MEDIA")) < min(
        i for i, l in enumerate(lines) if l.startswith("#EXT-X-STREAM-INF"))
    # 变体属性逐字不变：DV 信令丢一个，AVPlayer 就整份拒收（黑屏）
    variant_line = next(l for l in lines if l.startswith("#EXT-X-STREAM-INF:"))
    for attr in (
        "BANDWIDTH=12000000",
        'CODECS="hvc1.2.4.L153.B0,ec-3"',
        "RESOLUTION=3840x2160",
        "FRAME-RATE=24",
        "HDCP-LEVEL=TYPE-1",
        "VIDEO-RANGE=PQ",
        'SUPPLEMENTAL-CODECS="dvh1.05.06/db1p"',
    ):
        assert attr in variant_line
    assert lines[lines.index(variant_line) + 1] == "/_jf/534f/video.m3u8"
    # 视频侧一个字节都不碰（重新打包要分钟级，302 早超时了）
    assert (tmp_path / "0.m4s").read_bytes() == before["0.m4s"]
    assert (tmp_path / "video.m3u8").read_bytes() == before["video.m3u8"]
    assert (tmp_path / "sub_zh.vtt").is_file() and (tmp_path / "sub_en.vtt").is_file()


def test_upgrade_is_noop_when_master_already_has_tracks(tmp_path: Path):
    _minimal_pack(tmp_path)
    first = upgrade_master_subtitles(tmp_path, {"zh": SRT_ZH, "en": SRT_EN})
    assert first
    upgraded = (tmp_path / "master.m3u8").read_text(encoding="utf-8")
    # 再调一次不许把时间线翻一遍：返回空、master 原样
    assert upgrade_master_subtitles(tmp_path, {"zh": SRT_ZH, "en": SRT_EN}) == []
    assert (tmp_path / "master.m3u8").read_text(encoding="utf-8") == upgraded
    assert master_has_subtitle_tracks(tmp_path / "master.m3u8")


def test_upgrade_with_blank_texts_writes_nothing(tmp_path: Path):
    _minimal_pack(tmp_path)
    original = (tmp_path / "master.m3u8").read_text(encoding="utf-8")
    assert upgrade_master_subtitles(tmp_path, {"zh": "", "en": "  \n"}) == []
    assert (tmp_path / "master.m3u8").read_text(encoding="utf-8") == original
    assert not (tmp_path / "sub_zh.vtt").exists()


def test_upgrade_malformed_master_raises(tmp_path: Path):
    _minimal_pack(tmp_path)
    (tmp_path / "master.m3u8").write_text("#EXTM3U\n#EXT-X-VERSION:7\n", encoding="utf-8")
    with pytest.raises(ValueError):
        upgrade_master_subtitles(tmp_path, {"zh": SRT_ZH})
    assert not master_has_subtitle_tracks(tmp_path / "master.m3u8")

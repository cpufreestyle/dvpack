"""用 m3u8 参考库（globocom HLS 金标准）复读 dvpack 生成的列表。

手写字符串断言盯「我们想写什么」；独立解析器盯「客户端实际读到什么」——
两者互补，回归时后者能抓住前者结构上放过的错（属性拼写、URI、分组）。
参考库不在运行环境时（importorskip）这一层静默跳过，其余测试不受影响。
"""

from __future__ import annotations

from pathlib import Path

import pytest

m3u8 = pytest.importorskip("m3u8", reason="m3u8 参考库未安装（pyproject dev extra）")

from dvpack.playlist import master_playlist, variant_from_source
from dvpack.subs import write_subtitle_tracks


@pytest.fixture(scope="module")
def source():
    from tests.test_playlist import _source

    return _source()


def test_master_roundtrips_through_reference_parser(tmp_path: Path, source):
    variant = variant_from_source(source, "video.m3u8", bandwidth=100)
    tracks = write_subtitle_tracks(
        tmp_path,
        {
            "zh": "1\n00:00:01,000 --> 00:00:03,000\n世界\n",
            "en": "1\n00:00:01,000 --> 00:00:03,000\nWorld\n",
        },
    )
    pl = m3u8.loads(master_playlist([variant], subtitles=tracks))

    assert pl.version == 7
    assert pl.is_independent_segments is True
    # MEDIA 先于 STREAM-INF：参考库按解析顺序分桶，解析不出乱序
    assert [(m.type, m.group_id, m.language, m.default, m.autoselect, m.uri)
            for m in pl.media] == [
        ("SUBTITLES", "subs", "zh", "NO", "NO", "sub_zh.m3u8"),
        ("SUBTITLES", "subs", "en", "NO", "NO", "sub_en.m3u8"),
    ]
    assert len(pl.playlists) == 1
    assert pl.playlists[0].uri == "video.m3u8"
    assert pl.playlists[0].stream_info.bandwidth == 100
    # DV 信号落在 codecs 串里：解析器认得出来才说明没被属性拼写破坏
    assert "hvc1" in pl.playlists[0].stream_info.codecs


def test_subtitle_media_playlist_roundtrips(tmp_path: Path):
    tracks = write_subtitle_tracks(
        tmp_path,
        {"zh": "1\n00:00:01,000 --> 00:00:03,000\n世界\n"},
    )
    pl = m3u8.loads((tmp_path / "sub_zh.m3u8").read_text(encoding="utf-8"))

    assert pl.is_variant is False
    assert len(pl.segments) == 1
    seg = pl.segments[0]
    # byte-range 指向唯一 .vtt：整份字幕一个文件，电视按区间取
    assert seg.uri == "sub_zh.vtt"
    assert seg.byterange == "45@0"
    assert seg.duration == 2.0
    assert pl.is_endlist is True

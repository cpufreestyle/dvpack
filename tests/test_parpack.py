"""并行全片通路（parpack）的 media playlist 缝合测试。

这里守的是 2026-10-03《纽伦堡》无法播放的那条真机教训：分块接缝的首片是
「chunk 起点 → 下一个关键帧 + segment_seconds」的残片，长 GOP 源（x265 4K，
关键帧间隔约 10s）实测 14.917s。并行通路一度把 TARGETDURATION 钉死成
int(segment_seconds)+3 = 9，15 > 9 违反 RFC 8216 §4.3.3.1，tvOS 把整份
media playlist 判非法——现场 master.m3u8 与 video.m3u8 都 200，随后一个
video*.m4s 都不请求，用户侧只剩「无法播放」。

另一条是接缝残尾分片（纽伦堡实测 0.041s）必须丢掉：单进程通路的
remux.trim_stub_tail 早就因为同一现象删过它，AVPlayer 会在一个几乎没有可
呈现内容的缓冲区上停住。
"""

from __future__ import annotations

import pytest
from pathlib import Path
from dvpack import parpack
from dvpack.parpack import (
    STUB_EXTINF_SECONDS,
    ParallelPackError,
    _is_stub,
    assemble_media_playlist,
    target_duration_for,
)

# 纽伦堡（ab476f22b8fe82ec91a77886310c06fc）现场的实测数字，别改成好看的值。
SEAM_RESIDUE = "14.917"
SEAM_STUB = "0.041"
NORMAL = "6.006"


def _group(*extinfs: str):
    """一路 chunk 的 (分片名序列, EXTINF 序列)，名字每路都从 video0 起算。"""
    return (["video%d.m4s" % i for i in range(len(extinfs))], list(extinfs))


def test_target_duration_rises_to_cover_long_gop_seam_segment() -> None:
    """残片 14.917s 时 TARGETDURATION 必须是 15，而不是 segment_seconds+3。"""
    assert target_duration_for([NORMAL, SEAM_RESIDUE]) == 15
    # 反面对照：旧逻辑钉死 9 的那条路。
    assert int(6.0) + 3 == 9
    assert 9 < 14.917


def test_target_duration_is_at_least_one() -> None:
    assert target_duration_for(["0.100"]) == 1


def test_target_duration_rejects_no_durations() -> None:
    with pytest.raises(ParallelPackError):
        target_duration_for([])


def test_stub_threshold_splits_nuremberg_seam_pair() -> None:
    assert not _is_stub(SEAM_RESIDUE)
    assert _is_stub(SEAM_STUB)
    assert not _is_stub("not-a-float")
    assert 0 < STUB_EXTINF_SECONDS < 1.0


def test_playlist_target_duration_covers_measured_seam_residue() -> None:
    lines, _ops, _dropped = assemble_media_playlist(
        [_group(NORMAL, NORMAL), _group(SEAM_RESIDUE)])
    assert lines[3] == "#EXT-X-TARGETDURATION:15"
    # 每条 EXTINF 四舍五入后都必须 <= TARGETDURATION（RFC 8216 §4.3.3.1）。
    for value in (NORMAL, SEAM_RESIDUE):
        assert round(float(value)) <= 15


def test_playlist_drops_seam_stub_tail_only() -> None:
    lines, ops, dropped = assemble_media_playlist(
        [_group(NORMAL), _group(SEAM_RESIDUE, SEAM_STUB), _group(NORMAL)])
    assert dropped == 1
    # 残片本身是有效分片，必须留下，且编号不许留空洞。
    assert ops == [(0, "video0.m4s", "video0.m4s", 0.0),
                   (1, "video0.m4s", "video1.m4s", float(NORMAL)),
                   (2, "video0.m4s", "video2.m4s",
                    float(NORMAL) + float(SEAM_RESIDUE))]
    assert SEAM_STUB not in chr(10).join(lines)


def test_playlist_keeps_first_segment_even_when_stub() -> None:
    """首片永不丢：丢掉它整份 playlist 就没有起播点了。"""
    lines, ops, dropped = assemble_media_playlist([_group(SEAM_STUB, NORMAL)])
    assert dropped == 0
    assert ops == [(0, "video0.m4s", "video0.m4s", 0.0),
                   (0, "video1.m4s", "video1.m4s", 0.0)]
    assert "#EXTINF:%s," % SEAM_STUB in lines


def test_playlist_numbers_destinations_without_gaps() -> None:
    """接缝残片被丢后，目标编号必须连续，否则 playlist 指向不存在的文件。"""
    _lines, ops, _dropped = assemble_media_playlist(
        [_group(NORMAL, NORMAL), _group(SEAM_RESIDUE, SEAM_STUB),
         _group(NORMAL, NORMAL)])
    assert [dst for _gi, _src, dst, _off in ops] == [
        "video%d.m4s" % i for i in range(len(ops))]


def test_playlist_ends_with_endlist_and_map() -> None:
    lines, _ops, _dropped = assemble_media_playlist(
        [_group(NORMAL), _group(NORMAL)])
    assert lines[0] == "#EXTM3U"
    assert "#EXT-X-MAP:URI=" + chr(34) + "init.mp4" + chr(34) in lines
    assert lines[-1] == "#EXT-X-ENDLIST"


def test_playlist_rejects_mismatched_group() -> None:
    with pytest.raises(ParallelPackError):
        assemble_media_playlist([(["video0.m4s"], ["6.006", "6.006"])])


def test_playlist_rejects_when_nothing_survives() -> None:
    '''没有可发出的分片时（空组列表）必须炸穿，不能产出 0 分片 playlist。'''
    with pytest.raises(ParallelPackError):
        assemble_media_playlist([])
    with pytest.raises(ParallelPackError):
        assemble_media_playlist([([], [])])
def test_playlist_offsets_are_chunk_starts() -> None:
    """位移量按 chunk 给，不按段给。

    两个坑各有一条实测证据：
    - 不能用 chunk 标称 start：ffmpeg 的 -ss 会退到上一个关键帧，chunk 真实
      起点比标称早一整个 GOP（纽伦堡 2224s 标称 vs 2219.3s 真实，差 4.7s）；
    - 不能用逐段累计值：chunk 自己的 tfdt 链已经从 0 连续递增（受控实验实测
      接缝首段因此跳了一整个 EXTINF，10.000s），再叠段内位置就是重复计时。
    唯一自洽的基准是「本 chunk 之前所有实留分片的 EXTINF 累加」。
    """
    _lines, ops, _dropped = assemble_media_playlist(
        [_group(NORMAL, NORMAL), _group(SEAM_RESIDUE, NORMAL)])
    assert [_off for _gi, _src, _dst, _off in ops] == [
        pytest.approx(0.0),
        pytest.approx(0.0),
        pytest.approx(2 * float(NORMAL)),
        pytest.approx(2 * float(NORMAL)),
    ]


def test_playlist_offsets_ignore_dropped_stub_tail() -> None:
    """被丢的残尾时长不计入下一个 chunk 的位移基准。"""
    _lines, ops, dropped = assemble_media_playlist(
        [_group(NORMAL), _group(SEAM_RESIDUE, SEAM_STUB), _group(NORMAL)])
    assert dropped == 1
    assert [_off for _gi, _src, _dst, _off in ops] == [
        pytest.approx(0.0),
        pytest.approx(float(NORMAL)),
        pytest.approx(float(NORMAL) + float(SEAM_RESIDUE)),
    ]

def _write_stage(tmp_path, rels, *, v_ts=16000, a_ts=48000, seg_dur=6.0,
                 single_track=False):
    """造一个 stage 目录，read_tfdt 按 rels 表（音−视 tfdt 时刻差）合成。

    rels[i] 是该段音频 tfdt 相对视频 tfdt 的超前秒数；single_track=True 时
    只发 video 一轨，用来回归「单轨没有交叉校验」这条护栏。
    """
    names = ["video%d.m4s" % i for i in range(len(rels))]
    lines = ["#EXTM3U", "#EXT-X-VERSION:7", "#EXT-X-TARGETDURATION:15",
             '#EXT-X-MAP:URI="init.mp4"']
    for name in names:
        lines.append("#EXTINF:%.3f," % seg_dur)
        lines.append(name)
    lines.append("#EXT-X-ENDLIST")
    (tmp_path / "video.m3u8").write_text("\n".join(lines) + "\n", encoding="utf-8")
    table = dict(zip(names, rels))

    def fake_read_tfdt(seg_path):
        name = Path(seg_path).name
        rel = table[name]
        index = names.index(name)
        video = int(round(index * seg_dur * v_ts))
        if single_track:
            return [(1, 0, video)]
        return [(1, 0, video),
                (2, 0, int(round((index * seg_dur + rel) * a_ts)))]

    return fake_read_tfdt


def test_verify_tfdt_chain_accepts_constant_audio_lead(tmp_path, monkeypatch) -> None:
    """纽伦堡现场：音频 tfdt 恒定领先视频 0.075s（全 1437 段 rel 0.000~0.085）。

    2026-10-04 全量复测 ab476f 的实测分布，交叉校验必须原样放行。
    """
    monkeypatch.setattr(parpack, "read_tfdt",
                        _write_stage(tmp_path, [0.075] * 8))
    assert parpack.verify_tfdt_chain(tmp_path, {1: 16000, 2: 48000}) == 8


def test_verify_tfdt_chain_rejects_audio_seam_jump(tmp_path, monkeypatch) -> None:
    """陈旧预打包 item 的现场复刻：chunk 接缝音频 rel 跳 +0.504s。

    两轨各自的 tfdt 链仍是 EXTINF 连续的（判据 4 全过），只有交叉校验拦得住。
    """
    rels = [0.075] * 8
    rels[5] += 0.504
    monkeypatch.setattr(parpack, "read_tfdt", _write_stage(tmp_path, rels))
    with pytest.raises(ParallelPackError, match="track 2"):
        parpack.verify_tfdt_chain(tmp_path, {1: 16000, 2: 48000})


def test_verify_tfdt_chain_rejects_slow_audio_drift(tmp_path, monkeypatch) -> None:
    """慢漂（每段 +0.1s）在第三段越过 0.12s 阈值，同样必须拦。"""
    rels = [0.075 + 0.1 * i for i in range(8)]
    monkeypatch.setattr(parpack, "read_tfdt", _write_stage(tmp_path, rels))
    with pytest.raises(ParallelPackError, match="drifted"):
        parpack.verify_tfdt_chain(tmp_path, {1: 16000, 2: 48000})


def test_verify_tfdt_chain_passes_single_track(tmp_path, monkeypatch) -> None:
    """只有一轨时交叉校验不生效，回归护栏。"""
    monkeypatch.setattr(
        parpack, "read_tfdt",
        _write_stage(tmp_path, [0.0] * 4, single_track=True))
    assert parpack.verify_tfdt_chain(tmp_path, {1: 16000}) == 4

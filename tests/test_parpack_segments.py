"""dvpack.parpack 的段表配对：media playlist 顺序 与 字典序陷阱。

segment_plan 的存在理由：调用方若用 sorted(glob) 字典序，段数一多顺序全乱，
也拿不到逐段时长，只能退回 max(段字节)/平均段长。2026-10-03《纽伦堡》实测：
整片 21.66Mbps、真峰值 70.49Mbps，master 先后写成 24.48Mbps 与 9386596087，
AVPlayer 按假 BANDWIDTH 配缓冲，起播后周期性 underrun、burst 一段后彻底停摆，
正是真机「播几十秒失败」的根因。

这里把顺序/时长配对钉死，换实现就会响。
"""

from __future__ import annotations

import pytest

from dvpack.parpack import ParallelPackError, segment_plan, target_duration_for


def _playlist(*rows):
    """按 media playlist 的真实形状造行：EXTINF 紧跟自己那一段的名字。"""
    lines = ["#EXTM3U", "#EXT-X-VERSION:7",
             "#EXT-X-TARGETDURATION:6", "#EXT-X-MEDIA-SEQUENCE:0",
             "#EXT-X-PLAYLIST-TYPE:VOD", '#EXT-X-MAP:URI="init.mp4"']
    for name, seconds in rows:
        lines.append("#EXTINF:%s," % seconds)
        lines.append(name)
    lines.append("#EXT-X-ENDLIST")
    return lines

class TestSegmentPlan:
    """段表配对：顺序照 playlist，不照字典序。"""

    def test_pairs_follow_playlist_order_not_lexical(self, tmp_path):
        """10 段以上时字典序会把 video10 排到 video2 前面。"""
        rows = [("video%d.m4s" % i, "6.000") for i in range(12)]
        for name, _ in rows:
            (tmp_path / name).write_bytes(b"x")
        files, seconds = segment_plan(_playlist(*rows), tmp_path)
        assert [p.name for p in files] == ["video%d.m4s" % i for i in range(12)]
        assert seconds == [6.0] * 12

    def test_playlist_order_wins_over_sorted_order(self, tmp_path):
        """playlist 自己打乱顺序时，段表必须跟着 playlist。"""
        rows = [("video3.m4s", "6.000"), ("video1.m4s", "5.500"),
                ("video10.m4s", "7.250")]
        for name, _ in rows:
            (tmp_path / name).write_bytes(b"x")
        files, seconds = segment_plan(_playlist(*rows), tmp_path)
        assert [p.name for p in files] == ["video3.m4s", "video1.m4s", "video10.m4s"]
        assert seconds == [6.0, 5.5, 7.25]

    def test_each_segment_gets_its_own_duration(self, tmp_path):
        """逐段时长是峰值带宽的计算依据，不能共用平均值。"""
        rows = [("video0.m4s", "6.006"), ("video1.m4s", "14.917"),
                ("video2.m4s", "5.998")]
        for name, _ in rows:
            (tmp_path / name).write_bytes(b"x")
        files, seconds = segment_plan(_playlist(*rows), tmp_path)
        assert seconds == [6.006, 14.917, 5.998]
        assert [p.stat().st_size for p in files] == [1, 1, 1]

    def test_comment_and_blank_lines_are_ignored(self, tmp_path):
        for i in range(2):
            (tmp_path / ("video%d.m4s" % i)).write_bytes(b"x")
        lines = ["#EXTM3U", '#EXT-X-MAP:URI="init.mp4"', "",
                 "#EXTINF:6.000,", "video0.m4s", "", "#EXTINF:6.000,",
                 "# a comment", "video1.m4s"]
        files, seconds = segment_plan(lines, tmp_path)
        assert [p.name for p in files] == ["video0.m4s", "video1.m4s"]
        assert seconds == [6.0, 6.0]

    def test_count_mismatch_raises(self, tmp_path):
        """名字与 EXTINF 数量不齐就是坏列表，不能猜。"""
        lines = ["#EXTINF:6.000,", "video0.m4s", "#EXTINF:6.000,"]
        with pytest.raises(ParallelPackError, match="EXTINF"):
            segment_plan(lines, tmp_path)

    def test_extra_extinf_raises(self, tmp_path):
        (tmp_path / "video0.m4s").write_bytes(b"x")
        lines = ["#EXTINF:6.000,", "video0.m4s", "#EXTINF:6.000,"]
        with pytest.raises(ParallelPackError):
            segment_plan(lines, tmp_path)

    def test_missing_segment_file_raises_with_names(self, tmp_path):
        """缺文件必须点名缺哪些，否则排障时只能靠 ls 对。"""
        lines = ["#EXTINF:6.000,", "video0.m4s", "#EXTINF:6.000,", "video1.m4s"]
        (tmp_path / "video0.m4s").write_bytes(b"x")
        with pytest.raises(ParallelPackError) as exc:
            segment_plan(lines, tmp_path)
        assert "video1.m4s" in str(exc.value)

    def test_missing_segment_lists_at_most_five(self, tmp_path):
        """缺一大堆时只列前五个，别把日志刷爆。"""
        lines = []
        for i in range(9):
            lines += ["#EXTINF:6.000,", "video%d.m4s" % i]
        with pytest.raises(ParallelPackError) as exc:
            segment_plan(lines, tmp_path)
        assert "video0.m4s" in str(exc.value)
        assert "video5.m4s" not in str(exc.value)

    def test_empty_playlist_is_a_legal_empty_pair(self, tmp_path):
        """没有 EXTINF 也没有段名：0 对 0，是合法的空表。"""
        files, seconds = segment_plan(["#EXTM3U", "#EXT-X-ENDLIST"], tmp_path)
        assert files == [] and seconds == []


class TestTargetDurationFor:
    """RFC 8216 §4.3.3.1：TARGETDURATION 必须 >= 每个 EXTINF 四舍五入。"""

    @pytest.mark.parametrize(
        ("values", "expected"),
        [
            (["6.000", "6.000"], 6),
            (["6.000", "14.917"], 15),      # 纽伦堡接缝残片
            (["0.041"], 1),                 # 残片也不能低于 1
            (["6.006"], 7),                 # 不是 int(6.006)
        ],
    )
    def test_ceiling_of_max(self, values, expected):
        assert target_duration_for(values) == expected

    def test_empty_raises(self):
        with pytest.raises(ParallelPackError, match="no segment"):
            target_duration_for([])

    def test_never_below_one(self):
        """RFC 要求正整数：全残片时也不能写 0。"""
        assert target_duration_for(["0.001", "0.040"]) == 1

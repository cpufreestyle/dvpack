"""dvpack.dplus 的信令期望值：六字段口径与 range 位随源。

expected_signalling 决定 verify_signalling 断什么、apply_dplus 签什么。
2026-10-02 逐 box 对照：两个点亮版 init 都是 tier=0/non_packed=0，而把两位
置 1 的 variant D 用户实测「D 没亮」——签 1/1 是被证伪的假设残留下来的默认值。
颜色三件套恒为 9/16/9（BT.2020 + PQ + 2020nc），与源范围无关；range 位随源，
因为 2026-09-26 像素级实测证明全色域源（YMIN=0/YMAX=1016）三处必须一致签全色域。

verify_signalling 走真实 ffmpeg（tests/test_dvassert_script.py 与现场 dvassert），
这里只测它的**判定逻辑**：把 trace 结果换成假数据，验 problem 文案与 ok 判定。
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from dvpack.dplus import (
    EXPECTED_SIGNALLING,
    SignallingReport,
    expected_signalling,
    verify_signalling,
)


#: 与源范围无关、恒定的五个字段（dplus.expected_signalling 的口径）。
RANGE_INDEPENDENT = (
    ("colour_primaries", 9),
    ("transfer_characteristics", 16),
    ("matrix_coefficients", 9),
    ("general_tier_flag", 0),
    ("general_non_packed_constraint_flag", 0),
)


class TestExpectedSignalling:
    """六字段口径：range 位随源，其余五个恒定。"""

    def test_returns_named_pairs(self):
        rows = expected_signalling()
        assert rows, "期望值不能为空"
        for name, value in rows:
            assert isinstance(name, str) and name
            assert isinstance(value, int)

    def test_default_is_limited_range(self):
        "";"默认 0 = limited：BL 本就是 limited 的那类源。"""
        rows = dict(expected_signalling())
        assert rows["video_full_range_flag"] == 0

    @pytest.mark.parametrize("full_range", [0, 1])
    def test_range_bit_follows_the_argument(self, full_range):
        rows = dict(expected_signalling(full_range))
        assert rows["video_full_range_flag"] == full_range

    @pytest.mark.parametrize("full_range", [0, 1, 2, -1])
    def test_only_range_bit_moves(self, full_range):
        "";"range 位是唯一随参数变化的字段，其余五个必须钉死。"""
        rows = dict(expected_signalling(full_range))
        for name, value in RANGE_INDEPENDENT:
            assert rows[name] == value, name
        assert len(rows) == 6, rows

    def test_colour_triplet_is_bt2020_pq(self):
        rows = dict(expected_signalling())
        assert rows["colour_primaries"] == 9, "BT.2020 primaries"
        assert rows["transfer_characteristics"] == 16, "SMPTE 2084 PQ"
        assert rows["matrix_coefficients"] == 9, "BT.2020 non-constant luminance"

    def test_tier_and_non_packed_are_both_zero(self):
        "";"两个点亮版 init 的实测口径；置 1 的 variant D 用户实测不亮。"""
        rows = dict(expected_signalling())
        assert rows["general_tier_flag"] == 0
        assert rows["general_non_packed_constraint_flag"] == 0

    def test_tier_and_non_packed_never_follow_range(self):
        "";"全色域源也不能把 tier/non_packed 签起来。"""
        for full_range in (0, 1):
            rows = dict(expected_signalling(full_range))
            assert rows["general_tier_flag"] == 0, full_range
            assert rows["general_non_packed_constraint_flag"] == 0, full_range

    def test_order_is_stable(self):
        "";"返回 tuple 而非 dict：顺序变了 diff 会整段重排。"""
        assert expected_signalling() == expected_signalling()
        assert [n for n, _ in expected_signalling(1)][0] == "video_full_range_flag"


class TestExpectedSignallingConstant:
    """"兼容常量必须等于默认口径——旧引用方直接用，不许悄悄漂移。"""

    def test_constant_matches_the_function(self):
        assert EXPECTED_SIGNALLING == expected_signalling()

    def test_constant_is_limited(self):
        assert dict(EXPECTED_SIGNALLING)["video_full_range_flag"] == 0


class TestVerifySignalling:
    "";"判定逻辑：trace 换成假数据，验 ok 与 problem 文案。"""

    @pytest.fixture
    def fake_trace(self, monkeypatch):
        "";"把 trace_signalling 换成可控假数据，不起 ffmpeg。"""
        def _install(observed):
            monkeypatch.setattr("dvpack.dplus.trace_signalling",
                               lambda playlist: observed)
        return _install

    def test_all_match_means_ok(self, fake_trace):
        observed = {name: frozenset({value}) for name, value in expected_signalling()}
        fake_trace(observed)
        report = verify_signalling(Path("whatever.m3u8"))
        assert isinstance(report, SignallingReport)
        assert report.ok is True
        assert report.problems == ()

    def test_missing_field_is_a_problem(self, fake_trace):
        "";"没解析到不等于通过：静默漏一个字段就是漏签一个开关。"""
        observed = {name: frozenset({value}) for name, value in expected_signalling()}
        observed["general_tier_flag"] = frozenset()
        fake_trace(observed)
        report = verify_signalling(Path("x.m3u8"))
        assert report.ok is False
        assert any("没解析到 general_tier_flag" in p for p in report.problems)

    def test_wrong_value_reports_both_sides(self, fake_trace):
        "";"problem 必须同时给实测与期望，否则排障时还得再跑一遍。"""
        observed = {name: frozenset({value}) for name, value in expected_signalling()}
        observed["general_tier_flag"] = frozenset({1})
        fake_trace(observed)
        report = verify_signalling(Path("x.m3u8"))
        assert report.ok is False
        problem = next(p for p in report.problems if "general_tier_flag" in p)
        assert "1" in problem
        assert "0" in problem

    def test_multiple_values_in_one_field_is_a_problem(self, fake_trace):
        "";"同一字段出现两个值说明分片之间不一致，必须报。"""
        observed = {name: frozenset({value}) for name, value in expected_signalling()}
        observed["colour_primaries"] = frozenset({9, 1})
        fake_trace(observed)
        report = verify_signalling(Path("x.m3u8"))
        assert report.ok is False
        assert any("colour_primaries" in p for p in report.problems)

    @pytest.mark.parametrize("full_range", [0, 1])
    def test_range_bit_is_checked_against_the_argument(self, fake_trace, full_range):
        "";"limited 口径下签了全色域也要报——反之亦然。"""
        rows = expected_signalling(full_range)
        flipped = 1 - full_range
        observed = {}
        for name, value in rows:
            observed[name] = frozenset({flipped if name == "video_full_range_flag" else value})
        fake_trace(observed)
        report = verify_signalling(Path("x.m3u8"), full_range)
        assert report.ok is False
        assert any("video_full_range_flag" in p for p in report.problems)

    def test_full_range_match_means_ok(self, fake_trace):
        observed = {name: frozenset({value}) for name, value in expected_signalling(1)}
        fake_trace(observed)
        assert verify_signalling(Path("x.m3u8"), 1).ok is True

    def test_extra_observed_field_is_ignored(self, fake_trace):
        "";"trace 多解析出无关字段不该判失败。"""
        observed = {name: frozenset({value}) for name, value in expected_signalling()}
        observed["some_unrelated_field"] = frozenset({12345})
        fake_trace(observed)
        assert verify_signalling(Path("x.m3u8")).ok is True

    def test_empty_trace_fails_every_field(self, fake_trace):
        fake_trace({})
        report = verify_signalling(Path("x.m3u8"))
        assert report.ok is False
        assert len(report.problems) == 6, report.problems

    def test_report_carries_observed_for_diagnostics(self, fake_trace):
        "";"报告要带实测值：出问题时日志里得有现场。"""
        observed = {name: frozenset({value}) for name, value in expected_signalling()}
        fake_trace(observed)
        report = verify_signalling(Path("x.m3u8"))
        assert report.observed == observed


class TestSignallingReportShape:
    "";"报告三元组：ok / observed / problems，缺一不可。"""

    def test_fields_are_readable(self):
        report = SignallingReport(True, {"a": frozenset({1})}, ())
        assert report.ok is True
        assert report.observed == {"a": frozenset({1})}
        assert report.problems == ()

    def test_problems_is_a_tuple(self):
        "";"tuple 而不是 list：报告是不可变快照。"""
        report = SignallingReport(False, {}, ("缺字段",))
        assert isinstance(report.problems, tuple)
        assert report.ok is False


"""dvpack.probe 的两个归一：字幕语言 tag 与 帧率分数。

用户要求「所有视频调用中英双语字幕」。语言归一是那条链路的第一跳：
Jellyfin 的 MKV rip 语言 tag 五花八门（chi / zho / chs / cmn 都见过），
不归一的话 tvOS 字幕选择器里中英两条轨会裂成一堆单语言组，症状是
「有字幕但选不出来」，不报错。归一只认白名单，其余一律空串（= 跳过）。

parse_fraction 决定帧率：ffprobe 的 r_frame_rate 形如 24000/1001，
分母为 0 或非数字时不能抛（探测链路到处用它），只能给 0.0 让上层走默认。
"""

from __future__ import annotations

import pytest

from dvpack.probe import (
    _LANGUAGE_ALIASES,
    _SUBTITLE_TEXT_CODECS,
    _normalize_language,
    parse_fraction,
)


class TestNormalizeLanguage:
    """语言 tag 归一成 zh / en；取不到就空串。"""

    @pytest.mark.parametrize(
        ("tag", "expected"),
        [
            ("zh", "zh"),
            ("ZH", "zh"),
            ("chi", "zh"),
            ("zho", "zh"),
            ("chs", "zh"),
            ("cmn", "zh"),
            ("en", "en"),
            ("eng", "en"),
            ("EN", "en"),
            ("zh-CN", "zh"),
            ("zh-Hans", "zh"),
            ("zh-Hant-TW", "zh"),
            ("en-US", "en"),
            ("en-GB", "en"),
        ],
    )
    def test_known_tags_normalize(self, tag, expected):
        assert _normalize_language(tag) == expected

    @pytest.mark.parametrize(
        "tag",
        [
            "und",
            "ja", "jpn",
            "ko", "kor",
            "fr", "fra",
            "es", "spa",
            "pt", "por",
            "ru", "rus",
            "zhx",
            "english",
        ],
    )
    def test_unknown_tags_give_empty_string(self, tag):
        """白名单外的语言一律空串，调用方据此跳过该轨。"""
        assert _normalize_language(tag) == ""

    def test_empty_and_whitespace_give_empty_string(self):
        assert _normalize_language("") == ""
        assert _normalize_language("   ") == ""

    def test_none_is_tolerated(self):
        """ffprobe 可能给 None：探测链路不能在这里抛。"""
        assert _normalize_language(None) == ""

    def test_surrounding_whitespace_is_stripped(self):
        assert _normalize_language("  zh  ") == "zh"
        assert _normalize_language("\tENG\n") == "en"

    def test_case_is_folded(self):
        assert _normalize_language("ZHO") == "zh"
        assert _normalize_language("CmN") == "zh"

    def test_result_is_always_zh_en_or_empty(self):
        """返回值域只有三个值，别的都是 bug。"""
        for tag in ("zh", "en", "ja", "", "zh-CN", "eng", "xx-yy"):
            assert _normalize_language(tag) in ("zh", "en", "")

    def test_alias_table_maps_only_to_zh_and_en(self):
        """表里不许出现第三种语言。"""
        assert set(_LANGUAGE_ALIASES.values()) <= {"zh", "en"}

    def test_every_chinese_spelling_is_in_the_table(self):
        """现场见过的五种中文写法必须一个不少。"""
        for tag in ("zh", "chi", "zho", "chs", "cmn"):
            assert _LANGUAGE_ALIASES.get(tag) == "zh", tag

    def test_both_english_spellings_are_in_the_table(self):
        for tag in ("en", "eng"):
            assert _LANGUAGE_ALIASES.get(tag) == "en", tag


class TestSubtitleTextCodecs:
    """文本 codec 白名单：决定一条字幕轨要不要下载。"""

    @pytest.mark.parametrize(
        "codec",
        ["subrip", "webvtt", "mov_text", "text", "ass", "ssa"],
    )
    def test_text_codecs_are_listed(self, codec):
        assert codec in _SUBTITLE_TEXT_CODECS

    @pytest.mark.parametrize(
        "codec",
        ["pgssub", "hdmv_pgs", "dvdsub", "dvbsub", "dvd_sub", "xsub"],
    )
    def test_image_codecs_are_absent(self, codec):
        """图像字幕不进名单：tvOS 的 WebVTT 轨塞不进去。"""
        assert codec not in _SUBTITLE_TEXT_CODECS

    def test_table_itself_is_lowercase(self):
        """表本身是小写；调用方负责 lower。"""
        assert all(c == c.lower() for c in _SUBTITLE_TEXT_CODECS)


class TestParseFraction:
    """帧率分数：ffprobe 形如 24000/1001，坏输入给 0.0 而不是抛。"""

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("24000/1001", 24000 / 1001),
            ("50/1", 50.0),
            ("60/1", 60.0),
            ("25/1", 25.0),
            ("30000/1001", 30000 / 1001),
            ("24/1", 24.0),
        ],
    )
    def test_valid_fractions(self, text, expected):
        assert parse_fraction(text) == pytest.approx(expected)

    @pytest.mark.parametrize(
        "text",
        [
            "",
            None,
            "25",
            "abc",
            "abc/def",
            "0/0",
            "1/0",
            "//",
            " / ",
        ],
    )
    def test_bad_input_gives_zero_not_an_exception(self, text):
        """探测链路到处用它，抛异常会让整个打包失败。"""
        assert parse_fraction(text) == 0.0

    def test_negative_denominator_is_not_special_cased(self):
        """负数分母按数学处理，不做防御性改写。"""
        assert parse_fraction("50/-1") == -50.0

    def test_zero_numerator_is_a_real_zero(self):
        assert parse_fraction("0/1001") == 0.0

    def test_whitespace_around_parts_is_tolerated(self):
        """ffprobe 偶尔在值里带空白。"""
        assert parse_fraction(" 50 / 1 ") == 50.0

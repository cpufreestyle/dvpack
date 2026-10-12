"""dvpack.jfpack 的字幕链路：端点派生 与 编码兜底。

用户明确要求「所有视频调用中英双语字幕」。这条链路上有两处形状必须钉死：

1. jellyfin_subtitle_urls：Jellyfin 10.9+ 与老服务器的端点形状不同，
   2026-10-05 实测 12.1 上老格式 404、新格式 200。候选必须按新到旧排，
   且 api_key 要跟着带——漏一个字符就是整轨抓不到，而失败是静默的。

2. _decode_subtitle：SRT 常见 GB18030（国内字幕站点产物），utf-8 解不动
   必须按 GBK 系兜底。兜底缺失的症状是中文轨整条消失或乱码，不报错。
"""

from __future__ import annotations

import pytest

from dvpack.jfpack import _decode_subtitle, jellyfin_subtitle_urls

JF = "http://192.168.1.68:8096"


def _src(item_id="ab47", **query):
    """造一个 dvplay 会发出的 packURL。"""
    parts = ["static=true", "api_key=SECRET"]
    parts += [f"{k}={v}" for k, v in query.items()]
    return f"{JF}/Videos/{item_id}/stream.mkv?" + "&".join(parts)


class TestSubtitleUrls:
    """端点派生：新格式优先、老格式兜底、api_key 跟随。"""

    def test_returns_two_candidates_new_first(self):
        urls = jellyfin_subtitle_urls(_src(), "ab47", 2, "srt")
        assert len(urls) == 2
        # 新格式（带 mediaSourceId）在前：老服务器才会用到第二个
        assert urls[0].endswith("/Videos/ab47/ab47/Subtitles/2/Stream.srt?api_key=SECRET")
        assert urls[1].endswith("/Videos/ab47/Subtitles/2/Stream.srt?api_key=SECRET")

    def test_media_source_id_comes_from_query_when_present(self):
        """src 带了 mediaSourceId 就优先用它（多媒体源条目）。"""
        urls = jellyfin_subtitle_urls(_src(mediaSourceId="ms42"), "x", 5, "srt")
        assert "/Videos/x/ms42/Subtitles/5/Stream.srt" in urls[0]

    def test_media_source_id_defaults_to_item_id(self):
        """单媒体源时 mediaSourceId 就是 itemId。"""
        urls = jellyfin_subtitle_urls(_src(), "ab47", 3, "vtt")
        assert "/Videos/ab47/ab47/Subtitles/3/Stream.vtt" in urls[0]

    def test_explicit_media_source_beats_item_id(self):
        """给了 mediaSourceId 就不能退回 itemId。"""
        urls = jellyfin_subtitle_urls(_src(mediaSourceId="real-ms"), "item-x", 1, "srt")
        assert "real-ms" in urls[0]
        assert "/item-x/item-x/" not in urls[0]

    def test_ext_and_index_are_interpolated(self):
        for ext, idx in (("srt", 2), ("vtt", 3), ("ass", 7)):
            urls = jellyfin_subtitle_urls(_src(), "ab47", idx, ext)
            assert f"/Subtitles/{idx}/Stream.{ext}" in urls[0]

    def test_api_key_is_carried_on_both_candidates(self):
        """漏带 api_key 的话 Jellyfin 直接 401，而调用方只当「抓不到」静默跳过。"""
        urls = jellyfin_subtitle_urls(_src(), "ab47", 2, "srt")
        assert all("api_key=SECRET" in u for u in urls)

    def test_no_api_key_means_no_query_string(self):
        """没有 key 时不该拼出一个光秃秃的问号。"""
        src = f"{JF}/Videos/y/stream.mkv?static=true"
        urls = jellyfin_subtitle_urls(src, "y", 1, "srt")
        assert all("?" not in u for u in urls), urls

    def test_scheme_and_host_are_preserved(self):
        """同 host 同 scheme：换一个就可能被 TCC 挡回 EHOSTUNREACH。"""
        src = "https://jf.example:443/Videos/z/stream.mkv?static=true&api_key=K"
        urls = jellyfin_subtitle_urls(src, "z", 1, "srt")
        assert all(u.startswith("https://jf.example:443/") for u in urls)

    def test_blank_api_key_is_dropped(self):
        """空串 key 不带：拼上去只会让服务端拒签。"""
        src = f"{JF}/Videos/z/stream.mkv?static=true&api_key="
        urls = jellyfin_subtitle_urls(src, "z", 1, "srt")
        assert all("api_key" not in u for u in urls)


class TestDecodeSubtitle:
    """编码兜底：utf-8 优先，解不动按 GBK 系。"""

    SRT_ZH = "1\n00:00:01,000 --> 00:00:02,000\n你好\n"
    SRT_EN = "1\n00:00:01,000 --> 00:00:02,000\nHello\n"

    def test_utf8_passes_through_unchanged(self):
        payload = self.SRT_EN.encode("utf-8")
        assert _decode_subtitle(payload, "subrip") == self.SRT_EN

    def test_utf8_chinese_passes_through(self):
        payload = self.SRT_ZH.encode("utf-8")
        assert _decode_subtitle(payload, "subrip") == self.SRT_ZH

    def test_gb18030_falls_back(self):
        """国内站点产物：utf-8 解不动时必须按 GBK 系兜底，否则中文整条乱码。"""
        payload = self.SRT_ZH.encode("gb18030")
        assert _decode_subtitle(payload, "subrip") == self.SRT_ZH

    def test_gbk_variant_also_covered(self):
        "";"gbk 与 gb18030 在这个语料上等价，兜底必须两种都吃。"""
        payload = self.SRT_ZH.encode("gbk")
        assert _decode_subtitle(payload, "subrip") == self.SRT_ZH

    def test_codec_name_is_case_insensitive(self):
        """ffprobe 的 codec_name 大小写随容器漂。"""
        payload = self.SRT_EN.encode("utf-8")
        for codec in ("subrip", "SUBRIP", "SubRip", "mov_text", "MOV_TEXT"):
            assert _decode_subtitle(payload, codec) == self.SRT_EN

    def test_codec_name_does_not_change_text_path(self):
        "";"文本 codec 只做解码，不该触发 ffmpeg 转换。"""
        payload = self.SRT_EN.encode("utf-8")
        for codec in ("subrip", "text", "webvtt"):
            assert _decode_subtitle(payload, codec) == self.SRT_EN

    def test_unknown_codec_still_decodes_as_text(self):
        "";"未知 codec 不当图像字幕丢掉，至少把字节解成文本。"""
        payload = self.SRT_EN.encode("utf-8")
        assert _decode_subtitle(payload, "some_unknown_codec") == self.SRT_EN

    def test_empty_payload_gives_empty_text(self):
        assert _decode_subtitle(b"", "subrip") == ""

    def test_returns_str_not_bytes(self):
        assert isinstance(_decode_subtitle(self.SRT_EN.encode("utf-8"), "subrip"), str)

    @pytest.mark.parametrize("codec", ["ass", "ssa", "ASS", "SSA"])
    def test_ass_and_ssa_go_through_ffmpeg(self, codec):
        "";"ass/ssa 是容器格式，必须经 ffmpeg 转 srt——这里只验它不按文本直译。"""
        # 直译会让 ASS 的 [Script Info] 头部混进正文字幕
        ass = b"[Script Info]\nScriptType: v4.00+\n\n[Events]\n"
        with pytest.raises(Exception):
            # ffmpeg 对空 Events 段会给非零退出 -> JfPackError
            _decode_subtitle(ass, codec)


class TestSubtitleExtensionMap:
    "";"codec 到下载扩展名的映射：映射不到就不该发起下载。"""

    @pytest.mark.parametrize(
        ("codec", "ext"),
        [
            ("subrip", "srt"),
            ("mov_text", "srt"),
            ("text", "srt"),
            ("webvtt", "vtt"),
            ("ass", "ass"),
            ("ssa", "ass"),
        ],
    )
    def test_text_codecs_map_to_an_extension(self, codec, ext):
        from dvpack.jfpack import _SUBTITLE_EXTENSIONS
        assert _SUBTITLE_EXTENSIONS[codec] == ext

    @pytest.mark.parametrize("codec", ["pgssub", "dvdsub", "hdmv_pgs", "dvbsub"])
    def test_image_codecs_have_no_extension(self, codec):
        "";"图像字幕不进映射：tvOS 的 WebVTT 轨塞不进去。"""
        from dvpack.jfpack import _SUBTITLE_EXTENSIONS
        assert codec not in _SUBTITLE_EXTENSIONS

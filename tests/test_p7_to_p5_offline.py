"""scripts/p7_to_p5.py 的离线面：chrm 载荷 与 master 变体写法。

这个脚本是 P7（双层）→ tvOS 可点亮 P5 的**参考实现**：链路每一步都在 docstring
里留了出处，main() 全走真实 ffmpeg/dovi_tool（tests/test_remux.py 的 needs_source
覆盖）。这里只把两个纯函数焊死——它们决定 init 与 master 的信令形状，
而信令错一个字段 tvOS 就整份拒收（真机 -11848：一个分片都不请求）。
"""

from __future__ import annotations

import importlib.util
import math
import struct
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "p7_to_p5.py"


def _load_module():
    """按文件路径加载：scripts/ 不是包，import dvpack 之外的顶层脚本要这样拿。"""
    spec = importlib.util.spec_from_file_location("p7_to_p5_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


p7 = pytest.fixture(scope="module")(_load_module)


class TestChrmPayload:
    """"--chrm" 的载荷：BT.2020 容器 + PQ 传输。

    Apple 官方 P5 init 带 chrm（+fiel），dvpack 产物只有 colr/pasp；这个开关
    就是为做「补齐模板差异」实验留的。形状错了实验结论就无效。
    """

    def test_is_seven_bytes_big_endian(self, p7):
        payload = p7.chrm_payload()
        assert isinstance(payload, bytes)
        # >HHHB = 2+2+2+1 = 7 字节，无填充
        assert len(payload) == 7
        primaries, transfer, matrix, full_range = struct.unpack(">HHHB", payload)

    def test_bt2020_primaries_and_matrix(self, p7):
        primaries, transfer, matrix, full_range = struct.unpack(">HHHB", p7.chrm_payload())
        assert primaries == 9, "BT.2020 primaries"
        assert matrix == 9, "BT.2020 non-constant luminance"

    def test_pq_transfer(self, p7):
        _p, transfer, _m, _r = struct.unpack(">HHHB", p7.chrm_payload())
        assert transfer == 16, "SMPTE 2084 PQ"

    def test_full_range_flag_is_zero(self, p7):
        _p, _t, _m, full_range = struct.unpack(">HHHB", p7.chrm_payload())
        assert full_range == 0, "DV 走 narrow/limited range"

    def test_is_stable_across_calls(self, p7):
        assert p7.chrm_payload() == p7.chrm_payload()


class TestWriteMaster:
    """master 变体：dvh1.05.06 硬编码 + PQ，其余属性按传入取舍。

    level 为什么钉 06：脚本的调用方一律按 Apple 官方模板转签 P5 L6（见 main()
    里 resign_init_segment 的 level 参数），master 与 init 必须同口径。
    """

    def _write(self, p7, tmp_path, **kw):
        args = {"width": 3840, "height": 2160, "fps": 23.976, "bandwidth": 24_000_000}
        args.update(kw)
        return p7.write_master(tmp_path, **args).read_text(encoding="utf-8")

    def test_declares_pq_range(self, p7, tmp_path):
        assert "VIDEO-RANGE=PQ" in self._write(p7, tmp_path)

    def test_codecs_is_p5_level6(self, p7, tmp_path):
        text = self._write(p7, tmp_path)
        assert 'CODECS="dvh1.05.06"' in text

    def test_points_at_video_playlist(self, p7, tmp_path):
        text = self._write(p7, tmp_path)
        lines = text.splitlines()
        assert lines[-1] == "video.m3u8", lines
        assert lines[-2].startswith("#EXT-X-STREAM-INF:")

    def test_resolution_and_frame_rate_round_up(self, p7, tmp_path):
        text = self._write(p7, tmp_path)
        assert "RESOLUTION=3840x2160" in text
        assert "FRAME-RATE=24" in text, "23.976 必须向上取整，RFC 要求整数"

    @pytest.mark.parametrize("fps", [23.976, 24.0, 25.0, 50.0, 59.94])
    def test_frame_rate_never_rounds_down(self, p7, tmp_path, fps):
        text = self._write(p7, tmp_path, fps=fps)
        assert f"FRAME-RATE={math.ceil(fps)}" in text

    def test_zero_resolution_is_omitted(self, p7, tmp_path):
        text = self._write(p7, tmp_path, width=0, height=0)
        assert "RESOLUTION" not in text

    def test_zero_fps_is_omitted(self, p7, tmp_path):
        text = self._write(p7, tmp_path, fps=0)
        assert "FRAME-RATE" not in text

    def test_hdcp_flag_adds_type1(self, p7, tmp_path):
        text = self._write(p7, tmp_path, hdcp=True)
        assert "HDCP-LEVEL=TYPE-1" in text

    def test_hdcp_absent_by_default(self, p7, tmp_path):
        text = self._write(p7, tmp_path)
        assert "HDCP-LEVEL" not in text

    def test_version7_and_independent_segments(self, p7, tmp_path):
        text = self._write(p7, tmp_path)
        assert "#EXT-X-VERSION:7" in text
        assert "#EXT-X-INDEPENDENT-SEGMENTS" in text

    def test_bandwidth_is_written_verbatim(self, p7, tmp_path):
        text = self._write(p7, tmp_path, bandwidth=12_345_678)
        assert "BANDWIDTH=12345678" in text

    def test_returns_the_path_it_wrote(self, p7, tmp_path):
        path = p7.write_master(tmp_path, width=1920, height=1080, fps=24.0, bandwidth=1)
        assert path == tmp_path / "master.m3u8"
        assert path.is_file()

    def test_attributes_are_comma_joined_without_spaces(self, p7, tmp_path):
        text = self._write(p7, tmp_path)
        inf = next(l for l in text.splitlines() if l.startswith("#EXT-X-STREAM-INF:"))
        body = inf.split(":", 1)[1]
        assert " " not in body, "属性之间只许逗号，空格会让 AVPlayer 解析歧义"
        assert "," in body


class TestBitstreamFilterChain:
    """抽流用的 bsf 链必须与 remux 链路同口径（单一数据源，不许两份漂移）。"""

    def test_chain_matches_remux_vui_filter(self, p7):
        from dvpack.remux import DV_SPS_VUI_BSF
        assert p7.DV_SPS_VUI_BSF is DV_SPS_VUI_BSF or p7.DV_SPS_VUI_BSF == DV_SPS_VUI_BSF

    def test_chain_starts_with_annexb_and_ends_with_extradata(self, p7):
        chain = p7._MP4TOANNEXB_AND_EXTRACT
        assert chain[0] == "hevc_mp4toannexb"
        assert chain[-1] == "extract_extradata"
        assert any(tok.startswith("hevc_metadata=") for tok in chain)

    def test_vui_filter_is_one_token(self, p7):
        """hevc_metadata 与其参数是一个 = 连接的整体，拆开会被 ffmpeg 当成两个 bsf。"""
        assert len(p7.DV_SPS_VUI_BSF) == 1
        assert "=" in p7.DV_SPS_VUI_BSF[0]


class TestScriptContract:
    """源码层面的口径钉死：这些字符串改了就等于改信令。"""

    def test_dovi_tool_mode_default_is_5(self):
        text = SCRIPT.read_text(encoding="utf-8")
        assert 'ap.add_argument("--mode", type=int, default=5' in text

    def test_convert_passes_dash_d(self):
        """-d = drop EL：单层化必须真丢第二层，否则 tvOS 判伪 P5。"""
        text = SCRIPT.read_text(encoding="utf-8")
        assert '"convert", "-i", str(raw), "-o", str(out), "-d"' in text

    def test_compat_id_default_is_zero(self):
        text = SCRIPT.read_text(encoding="utf-8")
        assert 'ap.add_argument(\n        "--compat-id", type=int, default=0' in text

    def test_resign_uses_profile5_no_el(self):
        text = SCRIPT.read_text(encoding="utf-8")
        assert "profile=5" in text and "el_present=False" in text

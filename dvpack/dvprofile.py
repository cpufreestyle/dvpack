"""Dolby Vision 信令字典：profile / level / box 名 / HLS 属性的唯一事实来源。

历史上这些字段散在 bmff / p5 / playlist / probe 四处，同一事实写了两三遍
（`_SUPPLEMENTAL_BY_RANGE`、`DoviProfile02..92`、`_DVI_PROFILE_VIDEO_RANGE`）。
静态学习 Infuse 后把它集中成一张表：它的 `DolbyVisionConfiguration` +
`FCDolbyStreamParser` 正是"配置记录"与"动态元数据"两套正交结构；dvpack 的
`bmff`/`p5` 负责字节，`probe` 负责事实，这张表负责两者之间共享的字段名。

边界保持不变：这张表只收"两端共用的事实"——profile 枚举、Apple 认的
sample entry 与配置 box 名、codec 字符串模板、master 级 HEVC level 上限、
TRANSFER→VIDEO-RANGE 与 SUPPLEMENTAL brand 配对。ffprobe 与实测仍优先于查表
（transfer 实测到 HLG 时以实测为准），p5 的位域编码不搬进这里。
"""

from __future__ import annotations

#: DV 配置 record 的 box 名。`dvcC` 是 Apple/HLS 规范名，`dvvC` 是 ffmpeg
#: 俗名（ffprobe 两者都读，tvOS 只认前者）。解析必须两个都认：源可能是 ffmpeg
#: 直出，也可能是转签后的产物。
DV_BOX_NAMES: tuple[str, ...] = ("dvcC", "dvvC")
DV_BOX_CANONICAL = "dvcC"
DV_BOX_FFMPEG = "dvvC"

#: ISOBMFF 里的 DV sample entry。Apple 要求 dvh1（基于 hvc1）；dvhe 基于 hev1
#: 被明确标注不推荐；dav1 是 AV1 DV。bmff 的盒树解析靠这份名单进 VisualSampleEntry。
DV_SAMPLE_ENTRIES = frozenset({"dvh1", "dvhe", "dav1"})
#: HEVC / AVC 的非 DV sample entry（同上，盒树解析用）。
NON_DV_SAMPLE_ENTRIES = frozenset({"hvc1", "hev1", "avc1", "avc3", "av01", "vp09"})

#: master playlist 的 HEVC level 上限（5.1 = 153）。超过 5.1 会被 Apple TV
#: 整份 master 拒收（-11848 / -15517），见 playlist.hevc_base_codec 注释。
HEVC_LEVEL_CAP = 153

#: master 的 CODECS 视频段模板。tier 位固定 4（Main tier 在 L5 已是 High）。
HEVC_BASE_CODEC_TEMPLATE = "hvc1.2.4.L{level}.B0"

#: 实测 color_transfer → Apple HLS VIDEO-RANGE。实测优先；DV 分支的兜底映射
#: 见 PROFILE_VIDEO_RANGE_FALLBACK。
TRANSFER_TO_VIDEO_RANGE = {
    "smpte2084": "PQ",
    "bt2020lc": "PQ",
    "arib-std-b67": "HLG",
    "bt709": "SDR",
}

#: dv_bl_signal_compatibility_id → SUPPLEMENTAL-CODECS 的 brand（Apple 附录）。
#: 8.1→db1p（HDR10 兼容）、8.4→db4h（HLG 兼容）。
COMPAT_ID_TO_BRAND = {1: "db1p", 2: "db4h", 4: "db4h"}

#: 有 base layer（bl_present_flag=1，P5 与 P8 都带）的 DV 流写 master 时用的
#: brand。P5 的官方变体不写 SUPPLEMENTAL-CODECS，但真机已证实 P5 同样带 BL、
#: 同样要写 db1p；缺它 AVPlayer 不进 DV 通路。
BASE_LAYER_BRAND = "db1p"

#: VIDEO-RANGE → SUPPLEMENTAL brand 配对。db1p 与 HDR10 兼容（8.1），db4h 与
#: HLG 兼容（8.4）。
VIDEO_RANGE_TO_BRAND = {"PQ": BASE_LAYER_BRAND, "HLG": "db4h"}

#: profile → VIDEO-RANGE 兜底映射（容器没写 colour tags 的 MKV rip 用）。
#: Profile 5 按定义单层 BT.2020/PQ、无回退层；7 的基底是 HDR10；8 默认 8.1
#: （HDR10 基底），8.4 是 HLG——8.4 会从实测 transfer 露出来，走上面那张表。
PROFILE_VIDEO_RANGE_FALLBACK = {5: "PQ", 7: "PQ", 8: "PQ", 10: "PQ"}

#: Infuse 社区报告过不点亮的 P5 level；只在自身硬件复现前当风险项，不拒收。
RISKY_P5_LEVELS = frozenset({7, 9})

#: HEVC RPU NAL 类型（unregistered prefix/suffix）与 rpu_nal_prefix。实测自
#: 本机片源，与 dovi_tool info 输出一致。bmff 的 RPU 普查用。
RPU_NAL_TYPES = frozenset({62, 63})
RPU_NAL_PREFIX = 0x19

#: Apple HLS 要求随 init 段 hvcC 下发的参数集：VPS/SPS/PPS。
MANDATORY_PARAM_SETS = frozenset({32, 33, 34})


def hevc_base_codec(level: int = HEVC_LEVEL_CAP) -> str:
    """master 的 CODECS 视频段：hvc1 基线（DV 信令不进 CODECS，见 playlist）。"""
    return HEVC_BASE_CODEC_TEMPLATE.format(level=level)

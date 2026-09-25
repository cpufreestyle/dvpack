"""P8 → Profile 5 转签：把 init 段的 `dvvC` 改成 Apple 规范名 `dvcC` 并改写 profile。

为什么必须转签，以及为什么不能只靠 ffprobe 自检：

  * Apple HLS Authoring Specification §1.9：HLS 里的杜比视界 MUST be Profile 5
    （single layer 10-bit HEVC，≤Level 7）。P8 单层（HDR10 兼容）只被 tvOS 当
    HDR10 播放，DV 徽标不亮——2026-09-25 真机实测四次，这是硬结论。
  * ffmpeg 把配置 box 写成俗名 `dvvC`，而 Apple 官方参考流 `adv_dv_atmos` 写的是
    规范名 `dvcC`。**ffprobe / ffmpeg 两种名字都读**，所以本地复读"dv_profile=5""
    即便信令有问题也无从发现，只有按 Apple 规范解析的 tvOS 会认不出来。
  * 字节模板取自 Apple 官方 P5 变体：`01 00 0a 35 00 00...`（L6）与
    `01 00 0a 1d 00 00...`（L3），与 ffmpeg 写的 `01 00 10 35 10 00...`（P8 L6）
    同构，差别只在 profile、level、el_present、compat_id 四个字段。

转签只改 init 段的配置 box，不碰分片里的 ES：单层源本来就没有 EL，RPU 逐字节
透传，画面信息一个比特都不动。Apple 官方 P5 流的分片 NAL 排布
（AUD / SEI / VCL / RPU）与我们的 `-c copy` 产物一致，已用 NAL 普查核对过。

bit layout 照着上面两组参考字节反推，写完后**必须由 ffprobe 复读验证**
（ffprobe 读 `dvcC` 与 `dvvC` 字段相同）——自己不解析 box 就没法写，写了必须复读。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .bmff import DvBox, locate_dv_boxes
from .probe import DoviRecord


class ResignError(Exception):
    pass


@dataclass(frozen=True)
class DvcConfig:
    """只覆盖我们真正会写的字段；版本号与尾巴字节原样保留。

    参照字节：
      ffmpeg  P8 L6  `01 00 10 35 10 00 ...`
      Apple   P5 L6  `01 00 0a 35 00 00 ...`
      Apple   P5 L3  `01 00 0a 1d 00 00 ...`
    """

    version_major: int
    version_minor: int
    profile: int
    level: int
    rpu_present: bool
    el_present: bool
    bl_present: bool
    compat_id: int
    tail: bytes

    # 字段落位（byte0 起）：
    #   0     version_major
    #   1     version_minor
    #   2     profile(7) | level[5](1)
    #   3     level[4:0](5) | rpu_present(1) | el_present(1) | bl_present(1)
    #   4     compat_id(4) | 低半字节保留
    #   5..   其余字段，Apple 官方与 ffmpeg 都写全零，原样保留
    @classmethod
    def parse(cls, payload: bytes) -> DvcConfig:
        if len(payload) < 5:
            raise ResignError(f"DOVI 配置 record 只有 {len(payload)} 字节，不足 5")
        return cls(
            version_major=payload[0],
            version_minor=payload[1],
            profile=payload[2] >> 1,
            level=((payload[2] & 0x01) << 5) | (payload[3] >> 3),
            rpu_present=bool(payload[3] & 0x04),
            el_present=bool(payload[3] & 0x02),
            bl_present=bool(payload[3] & 0x01),
            compat_id=payload[4] >> 4,
            tail=payload[5:],
        )

    def encode(self) -> bytes:
        head = bytearray(5)
        head[0] = self.version_major & 0xFF
        head[1] = self.version_minor & 0xFF
        head[2] = ((self.profile & 0x7F) << 1) | ((self.level >> 5) & 0x01)
        head[3] = (
            (self.level & 0x1F) << 3
            | (int(self.rpu_present) << 2)
            | (int(self.el_present) << 1)
            | int(self.bl_present)
        )
        head[4] = (self.compat_id & 0x0F) << 4
        return bytes(head) + bytes(self.tail)

    def to_dovi_record(self) -> DoviRecord:
        """桥接 probe.DoviRecord，供 verify() 与 ffprobe 结果比对。"""
        return DoviRecord(
            profile=self.profile,
            level=self.level,
            rpu_present=self.rpu_present,
            el_present=self.el_present,
            bl_present=self.bl_present,
            compat_id=self.compat_id,
            md_compression="none",
            spec_version=f"{self.version_major}.{self.version_minor}",
        )


@dataclass(frozen=True)
class ResignResult:
    """一次转签的"改前 / 改后"，供命令行打印与 verify() 复读。"""

    path: Path
    box_fourcc: str
    before: DoviRecord
    after: DoviRecord
    renamed: bool

    @property
    def changed(self) -> bool:
        return self.renamed or self.before != self.after

    def describe(self) -> str:
        mark = "dvvC→dvcC，" if self.renamed else ""
        return (
            f"{mark}P{self.before.profile}→P{self.after.profile}"
            f"（L{self.before.level}→L{self.after.level}）"
        )


def resign_init_segment(
    init: Path,
    *,
    profile: int = 5,
    compat_id: int = 0,
    el_present: bool = False,
) -> ResignResult:
    """把 init 段的配置 box 转签成 Apple 规范写法，就地改写。

    box 名统一改成 `dvcC`（Apple 规范名）；profile / el_present / compat_id 按参数写；
    版本号、level 与其余字节原样保留——把 level 悄悄压低是谎报，判不过来。
    """
    data = init.read_bytes()
    boxes = locate_dv_boxes(data)
    if not boxes:
        raise ResignError(f"{init.name} 里没有 DV 配置 box，无从转签")
    if len(boxes) > 1:
        raise ResignError(f"{init.name} 里有 {len(boxes)} 个 DV 配置 box，多轨 DV 产物不在 MVP 范围")

    box: DvBox = boxes[0]
    before = DvcConfig.parse(box.payload).to_dovi_record()
    after_config = DvcConfig.parse(box.payload)
    after_config = DvcConfig(
        version_major=after_config.version_major,
        version_minor=after_config.version_minor,
        profile=profile,
        level=after_config.level,
        rpu_present=after_config.rpu_present,
        el_present=el_present,
        bl_present=after_config.bl_present,
        compat_id=compat_id,
        tail=after_config.tail,
    )
    after = after_config.to_dovi_record()

    patched = bytearray(data)
    if box.fourcc != "dvcC":
        patched[box.file_offset + 4 : box.file_offset + 8] = b"dvcC"
    # 长度不变：只改签名与 profile/level/compat 字段，外面的 box 尺寸都不用重算
    start = box.payload_offset
    patched[start : start + len(after_config.encode())] = after_config.encode()
    if len(patched) != len(data):
        raise ResignError("转签后文件长度变了，box 尺寸必须原样保住")
    init.write_bytes(bytes(patched))
    return ResignResult(
        path=init,
        box_fourcc=box.fourcc,
        before=before,
        after=after,
        renamed=box.fourcc != "dvcC",
    )


def resign_pack(init: Path, source_dovi: DoviRecord) -> ResignResult | None:
    """打包后置步骤：源不是 P5 单层时，把产物按 Apple 规范转签成 P5。已合规则空过。"""
    if source_dovi.profile == 5 and not source_dovi.el_present:
        return None
    return resign_init_segment(init, profile=5, compat_id=0, el_present=False)

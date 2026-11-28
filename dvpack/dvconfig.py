"""Dolby Vision 配置 record 的唯一位域实现（dvcC/dvvC 同布局）。

Infuse 静态学习笔记第 3 条的落地：DV 信令按「配置记录」单独建模。此前
p5.DvcConfig 是唯一结构化实现，bmff.clamp_dvcC_level 还在裸读 payload[3]
——位域知识写了两处。现在收拢成一个纯数据结构：parse/encode 互逆，
`to_dovi_record()` 桥接 probe 事实层；bmff（clamp 读 level）与 p5（转签
读写整段配置）共用这一份实现，位域知识从此只有一份。

布局（16 个 payload 位 = profile(7) → level(6) → rpu/el/bl present 各 1）：
  byte0  version_major
  byte1  version_minor
  byte2  profile(7) | level[5](1)
  byte3  level[4:0](5) | rpu_present(1) | el_present(1) | bl_present(1)
  byte4  compat_id(4) | 低半字节保留（encode 归零——Apple/ffmpeg 都写零）
  byte5.. tail 原样保留

参照字节：
  ffmpeg  P8 L6  `01 00 10 35 10 00 ...`
  Apple   P5 L6  `01 00 0a 35 00 00 ...`
  Apple   P5 L3  `01 00 0a 1d 00 00 ...`
"""

from __future__ import annotations

from dataclasses import dataclass

from .probe import DoviRecord


class ResignError(Exception):
    pass


@dataclass(frozen=True)
class DvcConfig:
    """只覆盖我们真正会写的字段；版本号与尾巴字节原样保留。"""

    version_major: int
    version_minor: int
    profile: int
    level: int
    rpu_present: bool
    el_present: bool
    bl_present: bool
    compat_id: int
    tail: bytes

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

"""DV 动态元数据（RPU）建模与操作。

对应 Infuse 静态学习笔记第 6 条：

  Infuse 把 DV 拆成两个正交结构——
    DecoderConfigurationRecord（静态，init 段里的 dvcC/dvvC）
    AVDOVIMetadata（动态，per-frame RPU）
  分别由 DolbyVisionConfiguration 和 FCDolbyStreamParser 承载。
  GENERATE_DOVI_RESHAPE 提示 Infuse 在缺失 reshape 时会合成。

本模块覆盖「动态」那一半：

  RpuScan   -- 对已打包的 fmp4/HLS 分片做 RPU 存在性普查（FCDolbyStreamParser 对应物）
  RpuFrame  -- 单帧 RPU 存在性记录（AVDOVIMetadata 对应物，轻量子集）
  extract_rpu(mp4) -> Path       -- 用 dovi_tool extract-rpu 把 RPU bin 提出来
  inject_rpu_m5(mp4, rpu) -> Path -- 用 dovi_tool -m 5 inject-rpu 重写 RPU
  scan_fmp4(path) -> RpuScan     -- 不调外部进程，纯 Python 读 mdat NAL 普查
  reshape_rpu_roundtrip(mp4)     -- extract → inject-m5 → scan 三步联动

使用场景：single_fmp4 DV 徽标不亮时，先用 scan_fmp4 确认 RPU 存在性；
若 RPU 存在但 Infuse 不识别，再走 inject_rpu_m5 路径重建 RPU。
"""

from __future__ import annotations

import struct
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from .dvprofile import RPU_NAL_PREFIX, RPU_NAL_TYPES
from .tools import DOVI_TOOL, ffmpeg_timeout, require

_RPU_NAL_TYPES = RPU_NAL_TYPES    # frozenset({62, 63})
_RPU_NAL_PREFIX = RPU_NAL_PREFIX  # 0x19

_DEFAULT_NALU_LENGTH_SIZE = 4


@dataclass(frozen=True)
class RpuFrame:
    """单帧 RPU 存在性记录（对应 AVDOVIMetadata 的轻量子集）。

    sample_index -- 在分片内从 0 起的样本编号
    rpu_count    -- 该样本里找到的 RPU NAL 数（正常应为 1）
    """

    sample_index: int
    rpu_count: int

    @property
    def has_rpu(self) -> bool:
        return self.rpu_count > 0

    @property
    def is_anomalous(self) -> bool:
        """每帧应恰好有 1 个 RPU；0 或 >1 都值得关注。"""
        return self.rpu_count != 1


@dataclass
class RpuScan:
    """对一个 fmp4/MP4 文件的 RPU 普查结果（FCDolbyStreamParser 对应物）。

    frames           -- 每个视频样本的 RpuFrame 记录列表
    total_samples    -- 视频轨总样本数
    nalu_length_size -- 读到的 NALU 长度前缀字节数（通常 4）
    """

    frames: list[RpuFrame] = field(default_factory=list)
    total_samples: int = 0
    nalu_length_size: int = _DEFAULT_NALU_LENGTH_SIZE

    @property
    def rpu_present(self) -> bool:
        return any(f.has_rpu for f in self.frames)

    @property
    def rpu_count(self) -> int:
        return sum(f.rpu_count for f in self.frames)

    @property
    def missing_frames(self) -> list[int]:
        return [f.sample_index for f in self.frames if f.rpu_count == 0]

    @property
    def anomalous_frames(self) -> list[int]:
        return [f.sample_index for f in self.frames if f.is_anomalous]

    @property
    def coverage_pct(self) -> float:
        if not self.total_samples:
            return 0.0
        with_rpu = sum(1 for f in self.frames if f.has_rpu)
        return with_rpu / self.total_samples * 100.0

    def summary(self) -> str:
        lines = [
            f"总样本 {self.total_samples}，RPU {self.rpu_count}，"
            f"覆盖率 {self.coverage_pct:.1f}%",
        ]
        if self.missing_frames:
            shown = self.missing_frames[:10]
            tail = f"…（共 {len(self.missing_frames)} 帧）" if len(self.missing_frames) > 10 else ""
            lines.append(f"缺 RPU 的帧：{shown}{tail}")
        multi = [f.sample_index for f in self.frames if f.rpu_count > 1]
        if multi:
            lines.append(f"多 RPU 的帧（>1）：{multi[:10]}")
        return "\n".join(lines)


def scan_fmp4(path: str | Path, *, max_samples: int | None = None) -> RpuScan:
    """从 fmp4/MP4 文件读视频轨，统计每帧 RPU NAL 数量（纯 Python，不调外部进程）。

    优先走 trun 路径（fmp4 HLS 分片），回退到 stbl（完整 MP4）。
    """
    path = Path(path)
    data = path.read_bytes()
    scan = RpuScan()

    nalu_length_size = _read_nalu_length_size(data)
    scan.nalu_length_size = nalu_length_size

    samples = _collect_samples_trun(data)
    if not samples:
        samples = _collect_samples_stbl(data)

    limit = max_samples if max_samples is not None else len(samples)
    for idx, (offset, size) in enumerate(samples[:limit]):
        if offset + size > len(data):
            break
        sample_data = data[offset : offset + size]
        rpu_count = _count_rpu_in_sample(sample_data, nalu_length_size)
        scan.frames.append(RpuFrame(sample_index=idx, rpu_count=rpu_count))

    scan.total_samples = len(scan.frames)
    return scan


def _read_nalu_length_size(data: bytes) -> int:
    """从 hvcC / dvcC / dvvC box payload 读 lengthSizeMinusOne。"""
    offset = 0
    while offset + 8 <= len(data):
        box_size = struct.unpack_from(">I", data, offset)[0]
        if box_size < 8 or offset + box_size > len(data):
            break
        box_type = data[offset + 4 : offset + 8]
        if box_type in (b"hvcC", b"dvcC", b"dvvC"):
            payload_start = offset + 8
            if payload_start + 22 <= len(data):
                return (data[payload_start + 21] & 0x03) + 1
        offset += max(1, box_size)
    return _DEFAULT_NALU_LENGTH_SIZE


def _collect_samples_trun(data: bytes) -> list[tuple[int, int]]:
    """从 moof+mdat 对里的 trun box 收集 (file_offset, sample_size) 列表。"""
    samples: list[tuple[int, int]] = []
    offset = 0
    while offset + 8 <= len(data):
        box_size = struct.unpack_from(">I", data, offset)[0]
        if box_size < 8:
            break
        box_type = data[offset + 4 : offset + 8]
        if box_type == b"moof":
            moof_start = offset
            moof_end = offset + box_size
            inner = offset + 8
            while inner + 8 <= moof_end:
                inner_size = struct.unpack_from(">I", data, inner)[0]
                inner_type = data[inner + 4 : inner + 8]
                if inner_type == b"trun":
                    _parse_trun(data, inner, moof_start, samples)
                inner += max(8, inner_size)
        offset += max(8, box_size)
    return samples


def _parse_trun(
    data: bytes, trun_offset: int, moof_start: int, out: list[tuple[int, int]]
) -> None:
    """解析单个 trun box，把 (absolute_file_offset, size) 追加到 out。"""
    if trun_offset + 20 > len(data):
        return
    flags = struct.unpack_from(">I", data, trun_offset + 8)[0] & 0x00FFFFFF
    sample_count = struct.unpack_from(">I", data, trun_offset + 12)[0]

    pos = trun_offset + 16
    data_offset = 0
    if flags & 0x001:
        if pos + 4 > len(data):
            return
        data_offset = struct.unpack_from(">i", data, pos)[0]
        pos += 4
    if flags & 0x004:
        pos += 4

    abs_base = moof_start + data_offset

    for _ in range(sample_count):
        size = 0
        if flags & 0x100:
            if pos + 4 > len(data):
                return
            pos += 4  # sample_duration
        if flags & 0x200:
            if pos + 4 > len(data):
                return
            size = struct.unpack_from(">I", data, pos)[0]
            pos += 4
        if flags & 0x400:
            pos += 4  # sample_flags
        if flags & 0x800:
            pos += 4  # sample_composition_time_offset
        if size > 0:
            out.append((abs_base, size))
            abs_base += size


def _collect_samples_stbl(data: bytes) -> list[tuple[int, int]]:
    """从完整 MP4 的 stbl（stco/co64 + stsz）收集 (file_offset, size) 列表。"""
    stco_offsets: list[int] = []
    sample_sizes: list[int] = []

    pos = 0
    while pos + 8 <= len(data):
        box_size = struct.unpack_from(">I", data, pos)[0]
        if box_size < 8:
            break
        box_type = data[pos + 4 : pos + 8]
        if box_type in (b"stco", b"co64") and not stco_offsets:
            count = struct.unpack_from(">I", data, pos + 12)[0]
            entry_start = pos + 16
            fmt, entry_size = (">I", 4) if box_type == b"stco" else (">Q", 8)
            for i in range(count):
                off = struct.unpack_from(fmt, data, entry_start + i * entry_size)[0]
                stco_offsets.append(off)
        elif box_type == b"stsz" and not sample_sizes:
            default_size = struct.unpack_from(">I", data, pos + 12)[0]
            count = struct.unpack_from(">I", data, pos + 16)[0]
            if default_size:
                sample_sizes = [default_size] * count
            else:
                entry_start = pos + 20
                for i in range(count):
                    sz = struct.unpack_from(">I", data, entry_start + i * 4)[0]
                    sample_sizes.append(sz)
        pos += max(8, box_size)

    if not stco_offsets or not sample_sizes:
        return []
    return list(zip(stco_offsets, sample_sizes))


def _count_rpu_in_sample(sample: bytes, nalu_length_size: int) -> int:
    """统计单个 fmp4 样本（HVCC 长度前缀格式）里的 RPU NAL 数量。"""
    count = 0
    pos = 0
    ls = nalu_length_size
    fmt = {1: ">B", 2: ">H", 4: ">I"}.get(ls, ">I")
    while pos + ls <= len(sample):
        nal_size = struct.unpack_from(fmt, sample, pos)[0]
        pos += ls
        if nal_size == 0 or pos + nal_size > len(sample):
            break
        nal_type = (sample[pos] >> 1) & 0x3F
        if nal_type in _RPU_NAL_TYPES:
            if pos + 2 < len(sample) and sample[pos + 2] == _RPU_NAL_PREFIX:
                count += 1
        pos += nal_size
    return count


class DvMetaError(Exception):
    """RPU 提取或注入失败。"""


def extract_rpu(mp4: str | Path, output: str | Path | None = None) -> Path:
    """用 dovi_tool extract-rpu 从 MP4/fmp4 里提取 RPU 二进制。

    参数：
        mp4    -- 输入 fmp4/MP4 文件（需含 DV 视频轨）
        output -- 输出 rpu.bin 路径；None 时放在 mp4 同目录下 <stem>.rpu.bin

    返回提取产物的 Path。失败时抛 DvMetaError，附 stderr。
    """
    mp4 = Path(mp4)
    if output is None:
        output = mp4.with_suffix(".rpu.bin")
    output = Path(output)

    tool = require(DOVI_TOOL)
    cmd = [str(tool), "extract-rpu", "-i", str(mp4), "-o", str(output)]
    result = subprocess.run(cmd, capture_output=True, timeout=ffmpeg_timeout())
    if result.returncode != 0:
        raise DvMetaError(
            f"dovi_tool extract-rpu 失败（exit {result.returncode}）:\n"
            + result.stderr.decode(errors="replace")
        )
    if not output.exists() or output.stat().st_size == 0:
        raise DvMetaError(f"extract-rpu 命令成功但产物不存在或为空：{output}")
    return output


def inject_rpu_m5(
    mp4: str | Path,
    rpu_bin: str | Path,
    output: str | Path | None = None,
) -> Path:
    """用 dovi_tool -m 5 inject-rpu 把 RPU 写回 MP4，同时把 profile 升/改为 5。

    对应 Infuse 的 GENERATE_DOVI_RESHAPE：当 single_fmp4 源的 RPU reshape
    信令缺失或格式不对时，-m 5 会重写 RPU 使其符合 Profile 5 播放器的期望。

    参数：
        mp4     -- 输入文件（通常是 p5.resign 后、dvcC 已改好的 init+分片）
        rpu_bin -- extract_rpu() 的产物
        output  -- None 时在 mp4 同目录写 <stem>.m5<ext>
    """
    mp4 = Path(mp4)
    rpu_bin = Path(rpu_bin)
    if output is None:
        output = mp4.with_name(mp4.stem + ".m5" + mp4.suffix)
    output = Path(output)

    tool = require(DOVI_TOOL)
    cmd = [
        str(tool), "-m", "5",
        "inject-rpu",
        "-i", str(mp4),
        "--rpu-in", str(rpu_bin),
        "-o", str(output),
    ]
    result = subprocess.run(cmd, capture_output=True, timeout=ffmpeg_timeout())
    if result.returncode != 0:
        raise DvMetaError(
            f"dovi_tool -m 5 inject-rpu 失败（exit {result.returncode}）:\n"
            + result.stderr.decode(errors="replace")
        )
    if not output.exists() or output.stat().st_size == 0:
        raise DvMetaError(f"inject-rpu 命令成功但产物不存在或为空：{output}")
    return output


def reshape_rpu_roundtrip(
    mp4: str | Path,
    *,
    work_dir: str | Path | None = None,
    keep_intermediates: bool = False,
) -> tuple[Path, RpuScan, RpuScan]:
    """extract -> inject-m5 -> scan 三步联动，返回 (m5_path, scan_before, scan_after)。

    这是 GENERATE_DOVI_RESHAPE 路径的完整落地：
      1. scan_fmp4(mp4) 记录原始 RPU 状况；
      2. extract_rpu 提出 RPU bin；
      3. inject_rpu_m5 写回，产物命名 <stem>.m5<ext>；
      4. scan_fmp4(m5_path) 核对注入后每帧都有 RPU。

    若注入前/后覆盖率都是 100% 且没有异常帧，调用方可用 m5_path 替换原文件。
    keep_intermediates=True 时保留 rpu.bin；默认用完即删。
    """
    mp4 = Path(mp4)
    _own_tmp = False
    if work_dir is None:
        _tmp = tempfile.mkdtemp(prefix="dvmeta_")
        work_dir_path = Path(_tmp)
        _own_tmp = True
    else:
        work_dir_path = Path(work_dir)
        work_dir_path.mkdir(parents=True, exist_ok=True)

    scan_before = scan_fmp4(mp4)
    rpu_bin = work_dir_path / (mp4.stem + ".rpu.bin")
    extract_rpu(mp4, rpu_bin)

    m5_path = inject_rpu_m5(mp4, rpu_bin)
    scan_after = scan_fmp4(m5_path)

    if not keep_intermediates:
        try:
            rpu_bin.unlink(missing_ok=True)
        except OSError:
            pass
        if _own_tmp:
            try:
                work_dir_path.rmdir()
            except OSError:
                pass

    return m5_path, scan_before, scan_after

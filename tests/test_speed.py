"""自适应窗口测试。

这套逻辑的价值全在「量得准」：如果拿 socket.write 的耗时当速率，内核接收
窗口会把它伪装成几百 Mbps，窗口越调越大，慢链路反而更堵。所以这里钉的是
两条口径——连续区间请求的间隔（主）与真实反压的写入耗时（辅）。
"""

from __future__ import annotations

import pytest

from dvpack.speed import (
    MIN_SAMPLE_BYTES,
    SpeedMeter,
    SpeedRegistry,
)

MB = 1024 * 1024


def test_meter_records_first_sample():
    meter = SpeedMeter()
    assert meter.record(MB, 1.0, now=100.0)
    assert meter.estimate_rate(now=100.0) == pytest.approx(MB, rel=1e-6)


def test_meter_ignores_tiny_or_instant_samples():
    meter = SpeedMeter()
    assert not meter.record(MIN_SAMPLE_BYTES - 1, 1.0, now=100.0)
    assert not meter.record(MB, 0.001, now=100.0)
    assert meter.estimate_rate(now=100.0) is None


def test_meter_ewma_smooths_spikes():
    meter = SpeedMeter(alpha=0.5)
    meter.record(MB, 1.0, now=100.0)          # 1 MiB/s
    meter.record(MB, 0.125, now=101.0)        # 8 MiB/s
    assert meter.estimate_rate(now=101.0) == pytest.approx(4.5 * MB, rel=1e-6)


def test_meter_estimate_goes_stale():
    meter = SpeedMeter()
    meter.record(MB, 1.0, now=100.0)
    assert meter.estimate_rate(now=100.0) is not None
    assert meter.estimate_rate(now=100.0 + 121.0) is None


def test_contiguous_advance_measures_consumer_rate():
    """客户端吃完上一段才来要下一段：间隔就是真实内容吞吐。"""
    registry = SpeedRegistry()
    registry.note_response("tv", "video1.m4s", 0, MB, now=100.0)
    rate = registry.poll_advance("tv", "video1.m4s", MB, now=102.0)
    assert rate == pytest.approx(MB / 2, rel=1e-5)       # 1MiB / 2s
    assert registry.estimate_mbps("tv", now=102.0) == pytest.approx(4.19, rel=1e-2)


def test_seek_is_not_mistaken_for_throughput():
    """跳播/重连会打断连续性，不能拿来当速率样本。"""
    registry = SpeedRegistry()
    registry.note_response("tv", "video1.m4s", 0, MB, now=100.0)
    assert registry.poll_advance("tv", "video1.m4s", 8 * MB, now=102.0) is None
    assert registry.estimate_rate("tv", now=102.0) is None


def test_different_files_do_not_pair():
    registry = SpeedRegistry()
    registry.note_response("tv", "video1.m4s", 0, MB, now=100.0)
    assert registry.poll_advance("tv", "video2.m4s", MB, now=102.0) is None


def test_forget_breaks_pairing():
    registry = SpeedRegistry()
    registry.note_response("tv", "video1.m4s", 0, MB, now=100.0)
    registry.forget("tv")
    assert registry.poll_advance("tv", "video1.m4s", MB, now=102.0) is None


def test_clients_are_isolated():
    registry = SpeedRegistry()
    registry.note_response("tv", "video1.m4s", 0, MB, now=100.0)
    registry.poll_advance("tv", "video1.m4s", MB, now=102.0)
    assert registry.estimate_rate("mac", now=102.0) is None
    assert registry.estimate_rate("tv", now=102.0) is not None


def _seed(registry: SpeedRegistry, key: str, nbytes: int, seconds: float,
          *, now: float = 100.0) -> None:
    """造一个「连续区间请求」样本：写响应于 now 开始，now+seconds 来要下一段。"""
    registry.note_response(key, "video.m4s", 0, nbytes, now=now)
    registry.poll_advance(key, "video.m4s", nbytes, now=now + seconds)


def test_cap_falls_back_to_static_max_without_sample():
    registry = SpeedRegistry()
    assert registry.cap_bytes("tv", target_seconds=1.5, max_bytes=2 * MB) == 2 * MB


def test_cap_scales_with_measured_rate():
    registry = SpeedRegistry()
    registry.note_response("tv", "video1.m4s", 0, MB, now=100.0)
    registry.poll_advance("tv", "video1.m4s", MB, now=102.0)   # ≈4.19 Mbps
    cap = registry.cap_bytes("tv", target_seconds=1.5, max_bytes=2 * MB, now=102.0)
    expected = int(MB / 2 * 1.5)
    assert cap == expected
    assert 256 * 1024 <= cap <= 2 * MB


def test_cap_is_clamped_to_floor_and_ceiling():
    slow = SpeedRegistry()
    _seed(slow, "tv", MIN_SAMPLE_BYTES, 120.0)                # 约 4 kbps
    assert slow.cap_bytes("tv", target_seconds=1.5,
                          max_bytes=2 * MB, now=100.0) == 256 * 1024

    fast = SpeedRegistry()
    _seed(fast, "tv", 2 * MB, 0.5)                            # 33.6 Mbps
    assert fast.cap_bytes("tv", target_seconds=60.0,
                          max_bytes=2 * MB, now=100.0) == 2 * MB


def test_cap_zero_means_disabled():
    registry = SpeedRegistry()
    _seed(registry, "tv", MB, 2.0)
    assert registry.cap_bytes("tv", target_seconds=1.5, max_bytes=0) == 0


def test_snapshot_is_sorted_and_reports_samples():
    registry = SpeedRegistry()
    _seed(registry, "b", MB, 2.0)
    _seed(registry, "a", MB, 2.0)
    rows = registry.snapshot(now=100.0)
    assert [row["client"] for row in rows] == ["a", "b"]
    assert rows[0]["samples"] == 1
    assert rows[0]["mbps"] == pytest.approx(4.19, rel=1e-2)

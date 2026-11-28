"""单连接吞吐测量与自适应 Range 窗口。

静态学习自 Infuse 的 ``FCInputStreamSpeedMeter`` / ``InputStreamStats``：
它把「这条链路现在有多快」变成一个可观测、可反馈的量；dvpack 之前只有一个
写死的 2MiB 开区间上限（``MAX_OPEN_RANGE_SEND``），慢链路上多发出去的那些
字节要占满真实空口时间，等于把本该留给下一段的带宽扔掉。

为什么不能直接用 ``socket.write`` 的耗时：客户端只要把数据收进 TCP 接收
窗口，write 就立刻返回，**首几 MB 的写入耗时并不代表链路速率**。所以主口径
是「同一分片的连续区间请求间隔」::

    客户端 bytes=A-      → 服务端发 N 字节，记下 (name, A, N, t0)
    客户端 bytes=(A+N)-  → 距 t0 过了 Δt，吞吐 = N / Δt

量到的正是消费者真实吃掉上一段的速率，也就是链路可用的内容吞吐。

反过来，**不能**拿「写响应花了多久」当速率：实测在 4 Mbps 的模拟链路上，
2MiB 响应的写入只阻塞了 1.15 秒（内核缓冲吃掉了大半），算出来 14.6 Mbps，
比真实值高两倍多。所以这里只认上面的配对口径。

全部走 EWMA，并按客户端 IP 分桶——本机 curl 自测不会污染电视那一路的估计。
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field

#: 小于这个量的样本噪声太大（TCP 慢启动、首包突发），不参与估计。
MIN_SAMPLE_BYTES = 64 * 1024
#: 连续请求间隔短于此值时视为「一次性突发」，分辨不出速率。
MIN_GAP_SECONDS = 0.05
#: 超过这么久没有新样本，估计作废，回落到静态上限。
STALE_SECONDS = 120.0

_MIN_RATE = 8 * 1024.0            # 64 kbps，按字节/秒记
_MAX_RATE = 156_250_000.0         # 1.25 GB/s = 10 Gbps


def _clamp_rate(value: float) -> float:
    return max(_MIN_RATE, min(_MAX_RATE, value))


@dataclass
class SpeedMeter:
    """一条链路的 EWMA 吞吐估计。"""

    alpha: float = 0.35
    _rate: float | None = field(default=None, init=False, repr=False)
    _at: float = field(default=0.0, init=False, repr=False)
    _samples: int = field(default=0, init=False, repr=False)

    def record(self, nbytes: int, seconds: float, *, now: float | None = None) -> bool:
        """记录一个样本；样本太小/太短则忽略。"""
        if nbytes < MIN_SAMPLE_BYTES or seconds < MIN_GAP_SECONDS:
            return False
        rate = _clamp_rate(nbytes / seconds)
        if self._rate is None:
            self._rate = rate
        else:
            self._rate = self._rate + self.alpha * (rate - self._rate)
        self._at = time.monotonic() if now is None else now
        self._samples += 1
        return True

    def estimate_rate(self, *, now: float | None = None) -> float | None:
        """字节/秒；没有可用样本时返回 None。"""
        if self._rate is None:
            return None
        moment = time.monotonic() if now is None else now
        if moment - self._at > STALE_SECONDS:
            return None
        return self._rate

    def snapshot(self, *, now: float | None = None) -> dict:
        rate = self.estimate_rate(now=now)
        return {
            "mbps": None if rate is None else round(rate * 8 / 1e6, 2),
            "samples": self._samples,
        }


@dataclass
class _Pending:
    """上一次响应的位置，用来判断下一次请求是不是紧接着往前要。

    ``at`` 记的是**开始写响应**的时刻，不是写完的时刻：写进内核发送缓冲
    往往秒回，真正的时间花在客户端慢慢把缓冲排空上。只有从「开始写」量到
    「下次来要」，间隔才等于这一段被完整消费掉的周期。
    """

    name: str = ""
    start: int = -1
    sent: int = 0
    at: float = 0.0


class SpeedRegistry:
    """按客户端分桶的吞吐估计 + 自适应窗口换算。"""

    def __init__(self, *, alpha: float = 0.35, stale_seconds: float = STALE_SECONDS):
        self._alpha = alpha
        self._stale = stale_seconds
        self._lock = threading.Lock()
        self._meters: dict[str, SpeedMeter] = {}
        self._pending: dict[str, _Pending] = {}

    def _meter(self, key: str) -> SpeedMeter:
        meter = self._meters.get(key)
        if meter is None:
            meter = SpeedMeter(alpha=self._alpha)
            self._meters[key] = meter
        return meter

    def poll_advance(self, key: str, name: str, start: int,
                     *, now: float | None = None) -> float | None:
        """新请求到来时调用：与上次响应首尾相接就算出一份真实消费速率。"""
        moment = time.monotonic() if now is None else now
        with self._lock:
            pending = self._pending.get(key)
            if pending is None or pending.name != name:
                return None
            if pending.start < 0 or start != pending.start + pending.sent:
                return None
            gap = moment - pending.at
            meter = self._meter(key)
            if not meter.record(pending.sent, gap, now=moment):
                return None
            return meter.estimate_rate(now=moment)

    def note_response(self, key: str, name: str, start: int, sent: int,
                      *, now: float | None = None) -> None:
        moment = time.monotonic() if now is None else now
        with self._lock:
            self._pending[key] = _Pending(name=name, start=start, sent=sent, at=moment)

    def forget(self, key: str) -> None:
        """丢掉待配对的位置（HEAD/探测请求不该参与速率配对）。"""
        with self._lock:
            self._pending.pop(key, None)

    def estimate_rate(self, key: str, *, now: float | None = None) -> float | None:
        """字节/秒。"""
        with self._lock:
            meter = self._meters.get(key)
            if meter is None:
                return None
            return meter.estimate_rate(now=now)

    def estimate_mbps(self, key: str, *, now: float | None = None) -> float | None:
        rate = self.estimate_rate(key, now=now)
        return None if rate is None else rate * 8 / 1e6

    def cap_bytes(self, key: str, *, target_seconds: float, max_bytes: int,
                  min_bytes: int = 256 * 1024,
                  now: float | None = None) -> int:
        """把「链路速率 × 目标时长」换算成本次响应的字节上限。"""
        if max_bytes <= 0:
            return 0
        rate = self.estimate_rate(key, now=now)
        if rate is None:
            return max_bytes
        cap = int(rate * max(0.1, target_seconds))
        cap = max(min_bytes, min(max_bytes, cap))
        return cap

    def snapshot(self, *, now: float | None = None) -> list[dict]:
        moment = time.monotonic() if now is None else now
        with self._lock:
            items = []
            for key, meter in self._meters.items():
                info = meter.snapshot(now=moment)
                info["client"] = key
                items.append(info)
        items.sort(key=lambda entry: entry["client"])
        return items

    def reset(self) -> None:
        with self._lock:
            self._meters.clear()
            self._pending.clear()

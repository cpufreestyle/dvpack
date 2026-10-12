"""ffmpeg/ffprobe 执行超时：卡死的上游流不能把 serve 永久堵死。

背景：_run 原先无超时，Jellyfin 流半路卡死 → ffmpeg 永等 → worker 持着
_pack_lock_for 不放 → 同 item 后续 /jf/pack 全堵 → 真机「点了没反应」
且只能重启 serve。2026-10-05 补 subprocess 级超时，超时转既有错误类型
（RemuxError / NotProbeable 都在 serve 的 PACK_ERRORS 里 → 502 通道）。
"""

from __future__ import annotations

import subprocess

import pytest

from dvpack import probe as probe_module
from dvpack import remux as remux_module
from dvpack.probe import NotProbeable
from dvpack.remux import RemuxError
from dvpack.tools import DEFAULT_FFMPEG_TIMEOUT, PROBE_TIMEOUT, ffmpeg_timeout, probe_timeout


def _hang(*args, **kwargs):
    raise subprocess.TimeoutExpired(cmd="ffmpeg", timeout=kwargs.get("timeout", 1))


def test_run_timeout_becomes_remux_error(monkeypatch):
    monkeypatch.setattr(subprocess, "run", _hang)
    with pytest.raises(RemuxError, match="执行超时"):
        remux_module._run(["ffmpeg", "-i", "x"], timeout=0.01)


def test_run_env_override_flows_into_subprocess(monkeypatch):
    seen = {}

    def fake_run(argv, **kwargs):
        seen["timeout"] = kwargs.get("timeout")
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setenv("DVPACK_FFMPEG_TIMEOUT", "12.5")
    remux_module._run(["ffmpeg", "-version"])
    assert seen["timeout"] == 12.5


def test_probe_timeout_becomes_not_probeable(monkeypatch):
    monkeypatch.setattr(subprocess, "run", _hang)
    with pytest.raises(NotProbeable, match="执行超时"):
        probe_module.ffprobe_json("http://192.168.1.68:8096/Videos/x/stream.mkv")


def test_ffmpeg_timeout_formula():
    # 短包 5 倍时长余量、下限 120s；长包/未知给全局限
    assert ffmpeg_timeout(None) == DEFAULT_FFMPEG_TIMEOUT
    assert ffmpeg_timeout(0) == DEFAULT_FFMPEG_TIMEOUT
    assert ffmpeg_timeout(20) == 160.0
    assert ffmpeg_timeout(180) == 960.0
    assert ffmpeg_timeout(10_000) == DEFAULT_FFMPEG_TIMEOUT  # 封顶
    assert ffmpeg_timeout(1) == 120.0  # 下限


def test_probe_timeout_default_and_env(monkeypatch):
    assert probe_timeout() == PROBE_TIMEOUT
    monkeypatch.setenv("DVPACK_FFMPEG_TIMEOUT", "30")
    assert probe_timeout() == 30.0


def test_env_override_garbage_falls_back(monkeypatch):
    monkeypatch.setenv("DVPACK_FFMPEG_TIMEOUT", "abc")
    assert ffmpeg_timeout(20) == 160.0
    monkeypatch.setenv("DVPACK_FFMPEG_TIMEOUT", "-5")
    assert ffmpeg_timeout(20) == 160.0

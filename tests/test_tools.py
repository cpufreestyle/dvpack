"""dvpack.tools：便携工具链的定位契约——项目内优先、PATH 回退、缺失报错。

为什么值得焊死：flow/dplus/probe/remux/jfpack/parpack/serve 七个模块共 27 处
 `require(...)` 调用全部走这一个入口。它回退 PATH 的行为错了（例如把不存在的
路径硬返回），真机症状是打包链在别的机器上「莫名能跑」或「莫名跑不了」，
日志里只有一句 FileNotFoundError，无从判断找的是哪个文件。

超时口径在 tests/test_timeouts.py 已焊死（公式、env 覆盖、上下限），这里只管
路径与回退。
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from dvpack import tools
from dvpack.tools import DOVI_TOOL, FFMPEG, FFPROBE, VTFRC, require  # noqa: F401


class TestPaths:
    def test_paths_live_under_repo_tools(self):
        """四件套必须都在 tools/ 下：换机器跑不能靠 PATH 缘分。"""
        for _name, path in (("ffmpeg", FFMPEG), ("ffprobe", FFPROBE),
                            ("dovi_tool", DOVI_TOOL), ("vtfrc", VTFRC)):
            assert path.is_relative_to(tools.ROOT / "tools")

    def test_ffmpeg_and_ffprobe_are_bundled(self):
        """这两个是打包链硬依赖，仓库里必须自带（无网机器能跑的前提）。"""
        assert FFMPEG.is_file(), FFMPEG
        assert FFPROBE.is_file(), FFPROBE

    def test_dovi_tool_is_bundled(self):
        assert DOVI_TOOL.is_file(), DOVI_TOOL

    def test_vtfrc_is_mac_only_binary(self):
        """VTFRC 走 macOS 私有 VTFrameProcessor，非 Mac 平台没有二进制——
        require 靠 PATH 回退兜住，这里只登记这个不对称。"""
        if os.name == "nt":
            assert VTFRC.name.endswith(".exe")
        elif os.name == "posix":
            # Mac 上随仓库带二进制（tools/build_vtfrc.sh 产物）；其他 POSIX
            # 平台没有，require 的 PATH 回退就是为它留的路。
            assert VTFRC.name == "vtfrc"
            assert VTFRC.is_file() or tools.shutil.which("vtfrc") is not None


class TestRequire:
    def test_existing_path_returned_unchanged(self, tmp_path, monkeypatch):
        exe = tmp_path / "ffmpeg"
        exe.write_text("#!/bin/sh\n", encoding="utf-8")

        def boom(name):
            raise AssertionError("存在时不该查 PATH")

        monkeypatch.setattr(tools.shutil, "which", boom)
        assert require(exe) == exe

    def test_falls_back_to_path_when_missing(self, tmp_path, monkeypatch):
        missing = tmp_path / "tools" / "ffmpeg"
        found = tmp_path / "usr-local-bin" / "ffmpeg"
        found.parent.mkdir(parents=True)
        found.write_text("#!/bin/sh\n", encoding="utf-8")
        monkeypatch.setattr(tools.shutil, "which",
                           lambda name: str(found) if name == "ffmpeg" else None)
        assert require(missing) == found

    def test_uses_basename_for_path_lookup(self, tmp_path, monkeypatch):
        """PATH 查的是文件名（ffmpeg），不是 tools/ffmpeg/bin/ffmpeg。"""
        seen = []

        def which(name):
            seen.append(name)
            return None

        monkeypatch.setattr(tools.shutil, "which", which)
        with pytest.raises(FileNotFoundError):
            require(tmp_path / "ffmpeg" / "bin" / "ffmpeg")
        assert seen == ["ffmpeg"]

    def test_missing_everywhere_raises_with_path(self, tmp_path, monkeypatch):
        monkeypatch.setattr(tools.shutil, "which", lambda name: None)
        gone = tmp_path / "vtfrc"
        with pytest.raises(FileNotFoundError, match="缺少工具") as exc:
            require(gone)
        assert "vtfrc" in str(exc.value)

    def test_error_message_names_original_path(self, tmp_path, monkeypatch):
        monkeypatch.setattr(tools.shutil, "which", lambda name: None)
        # 用真不存在的路径：仓库里自带 vtfrc，直接 require(VTFRC) 命不中缺失分支
        gone = tmp_path / "bin" / "definitely-not-here"
        with pytest.raises(FileNotFoundError) as exc:
            require(gone)
        assert str(gone) in str(exc.value)
        assert "缺少工具" in str(exc.value)


class TestExeSuffix:
    def test_windows_names_get_exe_suffix(self, monkeypatch):
        monkeypatch.setattr(tools.os, "name", "nt")
        assert tools._exe("ffmpeg/bin/ffmpeg").name == "ffmpeg.exe"

    def test_posix_names_stay_bare(self, monkeypatch):
        monkeypatch.setattr(tools.os, "name", "posix")
        assert tools._exe("ffmpeg/bin/ffmpeg").name == "ffmpeg"

    def test_tools_dir_is_repo_root_relative(self):
        """跨进程/跨 cwd 调用（launchd 守护、prewarm 子进程）路径必须稳定。"""
        assert tools._TOOLS == tools.ROOT / "tools"
        assert tools._TOOLS.is_dir()

"""dvpack.configcmd：`dvpack config` 的纯函数面（掩码、写盘权限、双 api 等价）。

主流程（--init / --os-key / 裸跑的 stdout 断言）在 tests/test_config_cmd.py
已覆盖。这里补三个焊点：
1. `_mask` 的短/长 key 边界——key 是秘密，输出里露出超过 6 个字符就是泄漏；
2. `_write_config` 的 600 权限与目录自动建——同机多用户读不到别人的 key；
3. `_FallbackConfig` 与 tools/subfetch.py 的**行为等价**——装包分发时
   subfetch.py 不在手边，回退实现的行为必须逐字一致，否则 key 解析顺序漂移，
   在线字幕腿静默失效（key 取不到 → 6 小时负缓存 → 「字幕抓不下来」）。
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from dvpack import configcmd
from dvpack.configcmd import SKELETON, _FallbackConfig, _mask, _write_config


class TestMask:
    @pytest.mark.parametrize(
        ("key", "expected"),
        [
            ("", "***"),
            ("short", "***"),
            ("1234567", "***"),   # 7 字符 = 边界内（<8）
            ("12345678", "1234…78"),  # 8 字符 = 刚好越过边界
            ("4kQhoCbBVA1b2AizgiGpDQKb9V7qR9U", "4kQh…9U"),
            ("  padded-key-long  ", "padd…ng"),  # 两端空白先剥掉再判断
        ],
    )
    def test_mask_shapes(self, key, expected):
        assert _mask(key) == expected

    def test_mask_never_leaks_middle(self):
        """中间字符一个都不许出现在输出里——key 是秘密。"""
        secret = "ABCDEFGH12345678WXYZ"
        shown = _mask(secret)
        assert len(shown) < len(secret)
        assert secret[4:-2] not in shown


class TestWriteConfig:
    def test_creates_parent_dirs(self, tmp_path):
        target = tmp_path / "deep" / "nested" / "config.json"
        _write_config(target, dict(SKELETON))
        assert target.is_file()
        assert json.loads(target.read_text(encoding="utf-8")) == {"opensubtitles_key": ""}

    def test_mode_is_600(self, tmp_path):
        target = tmp_path / "config.json"
        _write_config(target, {"opensubtitles_key": "K"})
        assert stat.S_IMODE(target.stat().st_mode) == 0o600

    def test_overwrites_existing_content(self, tmp_path):
        target = tmp_path / "config.json"
        target.write_text("旧内容", encoding="utf-8")
        _write_config(target, {"opensubtitles_key": "NEW"})
        assert json.loads(target.read_text(encoding="utf-8"))["opensubtitles_key"] == "NEW"

    def test_keeps_unicode_unsloshed(self, tmp_path):
        """ensure_ascii=False：中文 key 名不该被转义。"""
        target = tmp_path / "config.json"
        _write_config(target, {"说明": "中文值"})
        assert "中文值" in target.read_text(encoding="utf-8")

    def test_rewrite_restores_600(self, tmp_path):
        """重写已有文件后权限仍须是 600（chmod 在 write 之后显式调）。"""
        target = tmp_path / "config.json"
        _write_config(target, dict(SKELETON))
        os.chmod(target, 0o644)
        _write_config(target, {"opensubtitles_key": "K2"})
        assert stat.S_IMODE(target.stat().st_mode) == 0o600


class TestFallbackEquivalence:
    """回退实现与 tools/subfetch.py 的行为差一个字节，key 解析就会漂移。"""

    def test_config_path_honours_env(self, monkeypatch, tmp_path):
        monkeypatch.setenv("DVPACK_CONFIG", str(tmp_path / "x.json"))
        assert _FallbackConfig.config_path() == tmp_path / "x.json"

    def test_config_path_defaults_home(self, monkeypatch):
        monkeypatch.delenv("DVPACK_CONFIG", raising=False)
        assert _FallbackConfig.config_path() == Path.home() / ".dvpack" / "config.json"

    def test_config_path_expands_user(self, monkeypatch):
        monkeypatch.setenv("DVPACK_CONFIG", "~/somewhere/config.json")
        assert _FallbackConfig.config_path().is_absolute()
        assert not str(_FallbackConfig.config_path()).startswith("~")

    def test_load_config_tolerates_garbage(self, monkeypatch, tmp_path):
        monkeypatch.setenv("DVPACK_CONFIG", str(tmp_path / "c.json"))
        assert _FallbackConfig.load_config() == {}
        (tmp_path / "c.json").write_text("{坏 json", encoding="utf-8")
        assert _FallbackConfig.load_config() == {}

    def test_load_config_rejects_non_dict(self, monkeypatch, tmp_path):
        monkeypatch.setenv("DVPACK_CONFIG", str(tmp_path / "c.json"))
        (tmp_path / "c.json").write_text('["不是", "字典"]', encoding="utf-8")
        assert _FallbackConfig.load_config() == {}

    def test_api_key_precedence_cli_over_env_over_file(self, monkeypatch, tmp_path):
        monkeypatch.setenv("DVPACK_CONFIG", str(tmp_path / "c.json"))
        _write_config(tmp_path / "c.json", {"opensubtitles_key": "FILEKEY"})
        monkeypatch.setenv("OPENSUBTITLES_KEY", "ENVKEY")
        assert _FallbackConfig.os_api_key("CLIKEY") == "CLIKEY"
        assert _FallbackConfig.os_api_key() == "ENVKEY"
        monkeypatch.delenv("OPENSUBTITLES_KEY")
        assert _FallbackConfig.os_api_key() == "FILEKEY"

    def test_api_key_blank_and_whitespace_skipped(self, monkeypatch, tmp_path):
        monkeypatch.setenv("DVPACK_CONFIG", str(tmp_path / "c.json"))
        _write_config(tmp_path / "c.json", {"opensubtitles_key": "   "})
        monkeypatch.delenv("OPENSUBTITLES_KEY", raising=False)
        assert _FallbackConfig.os_api_key() == ""
        assert _FallbackConfig.os_api_key("   ") == ""

    def test_api_key_strips_whitespace(self, monkeypatch, tmp_path):
        monkeypatch.setenv("DVPACK_CONFIG", str(tmp_path / "c.json"))
        _write_config(tmp_path / "c.json", {"opensubtitles_key": "  TRIMMED  "})
        monkeypatch.delenv("OPENSUBTITLES_KEY", raising=False)
        assert _FallbackConfig.os_api_key() == "TRIMMED"


class TestApiResolution:
    def test_repo_api_is_subfetch_when_present(self):
        """仓库布局下优先加载 tools/subfetch.py（同一份解析，不两套口径）。"""
        assert configcmd._api() is not _FallbackConfig

    def test_falls_back_when_script_missing(self, monkeypatch, tmp_path):
        monkeypatch.setattr(configcmd, "_api", lambda: _FallbackConfig)
        assert configcmd._api() is _FallbackConfig

"""CLI `config` 子命令：OpenSubtitles Api-Key 的 onboard 入口。"""

from __future__ import annotations

import json
import stat

import pytest

from dvpack.__main__ import main


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    path = tmp_path / "dvpack" / "config.json"
    monkeypatch.setenv("DVPACK_CONFIG", str(path))
    monkeypatch.delenv("OPENSUBTITLES_KEY", raising=False)
    return path


def test_init_writes_skeleton_with_600(cfg, capsys):
    assert main(["config", "--init"]) == 0
    assert cfg.is_file()
    assert json.loads(cfg.read_text(encoding="utf-8")) == {"opensubtitles_key": ""}
    assert stat.S_IMODE(cfg.stat().st_mode) == 0o600
    assert "600" in capsys.readouterr().out


def test_init_twice_keeps_existing_key(cfg, capsys):
    assert main(["config", "--init"]) == 0
    cfg.write_text(json.dumps({"opensubtitles_key": "KEEPME"}) + "\n", encoding="utf-8")
    assert main(["config", "--init"]) == 0
    assert "不动它" in capsys.readouterr().out
    assert json.loads(cfg.read_text(encoding="utf-8"))["opensubtitles_key"] == "KEEPME"


def test_os_key_writes_and_masks_output(cfg, capsys):
    key = "4kQhoCbBVA1b2AizgiGpDQKb9V7qR9U"
    assert main(["config", "--os-key", key]) == 0
    assert json.loads(cfg.read_text(encoding="utf-8"))["opensubtitles_key"] == key
    assert stat.S_IMODE(cfg.stat().st_mode) == 0o600
    out = capsys.readouterr().out
    assert "4kQh\u2026" in out
    assert key not in out


def test_bare_shows_config_file_source(cfg, capsys):
    main(["config", "--os-key", "ABCD1234EFGH"])
    capsys.readouterr()
    assert main(["config"]) == 0
    out = capsys.readouterr().out
    assert str(cfg) in out
    assert "\u6765\u6e90\uff1a\u914d\u7f6e\u6587\u4ef6" in out
    assert "ABCD\u2026GH" in out


def test_env_key_beats_config_file(cfg, monkeypatch, capsys):
    main(["config", "--os-key", "ABCD1234EFGH"])
    monkeypatch.setenv("OPENSUBTITLES_KEY", "ENVKEY123456")
    capsys.readouterr()
    assert main(["config"]) == 0
    out = capsys.readouterr().out
    assert "\u6765\u6e90\uff1a$OPENSUBTITLES_KEY" in out
    assert "ENVK\u202656" in out


def test_missing_config_hints_init(cfg, capsys):
    assert main(["config"]) == 0
    out = capsys.readouterr().out
    assert "\uff08\u4e0d\u5b58\u5728\uff09" in out
    assert "--init" in out


def test_fallback_precedence_matches_repo_impl(cfg, monkeypatch):
    from dvpack import configcmd
    assert configcmd._api() is not configcmd._FallbackConfig
    monkeypatch.setattr(configcmd, "_api", lambda: configcmd._FallbackConfig)
    assert main(["config", "--os-key", "FILEKEY000001"]) == 0
    monkeypatch.setenv("OPENSUBTITLES_KEY", "ENVKEY0000002")
    assert configcmd._api().os_api_key() == "ENVKEY0000002"
    assert configcmd._api().os_api_key("CLIKEY0000003") == "CLIKEY0000003"
    monkeypatch.delenv("OPENSUBTITLES_KEY")
    assert configcmd._api().os_api_key() == "FILEKEY000001"
    assert configcmd._api().os_api_key() != ""

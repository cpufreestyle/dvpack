"""dvpack config：Api-Key 配置的引导子命令（展示 / --init / --os-key）。

参照 rclone config 与 aws configure 的 onboarding 思路：把「每拉起一个
serve/prewarm 都要手抄 export OPENSUBTITLES_KEY」收敛成一条命令——落盘
~/.dvpack/config.json（600 权限）。解析顺序不重复造轮子：直接复用
tools/subfetch.py 的 config_path/load_config/os_api_key（--os-key >
$OPENSUBTITLES_KEY > 配置文件，v51 收敛处）；仓库布局外的安装退回本地
等价实现，行为逐字一致。
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import stat
from pathlib import Path


SKELETON = {"opensubtitles_key": ""}


class _FallbackConfig:
    """tools/subfetch.py 不在手边时的等价实现（装包/单文件分发场景）。"""

    @staticmethod
    def config_path() -> Path:
        raw = (os.environ.get("DVPACK_CONFIG") or "").strip()
        return Path(raw).expanduser() if raw else Path.home() / ".dvpack" / "config.json"

    @staticmethod
    def load_config() -> dict:
        try:
            data = json.loads(_FallbackConfig.config_path().read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    @staticmethod
    def os_api_key(cli_key=None) -> str:
        for candidate in (cli_key, os.environ.get("OPENSUBTITLES_KEY"),
                          _FallbackConfig.load_config().get("opensubtitles_key")):
            if isinstance(candidate, str) and candidate.strip():
                return candidate.strip()
        return ""


def _api():
    """优先加载仓库布局下的 tools/subfetch.py；失败退回等价实现。"""
    script = Path(__file__).resolve().parent.parent / "tools" / "subfetch.py"
    if script.is_file():
        try:
            spec = importlib.util.spec_from_file_location("_dvpack_subfetch", script)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            return module
        except Exception:
            pass
    return _FallbackConfig


def _write_config(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)  # 600：key 不给同机其他用户读


def _mask(key: str) -> str:
    key = key.strip()
    if len(key) < 8:
        return "***"
    return key[:4] + "…" + key[-2:]


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--init", action="store_true",
                        help="写配置文件骨架（已存在则不动，不覆盖已有 key）")
    parser.add_argument("--os-key", default=None, metavar="KEY",
                        help="把 OpenSubtitles Api-Key 写进配置文件（600 权限）")


def run_from_args(args) -> int:
    api = _api()
    path = api.config_path()
    key = (args.os_key or "").strip()

    if args.init:
        if path.exists():
            print(f"已存在，不动它：{path}")
        else:
            _write_config(path, dict(SKELETON))
            print(f"已写骨架（600 权限）：{path}")
            print('编辑 opensubtitles_key，或直接：python -m dvpack config --os-key <key>')
        return 0

    if key:
        data = api.load_config()
        data["opensubtitles_key"] = key
        _write_config(path, data)
        print(f"opensubtitles_key 已写入 {path}（600 权限）：{_mask(key)}")
        print("生效范围：serve / prewarm / subfetch 全部走同一份解析，无需再 export")
        return 0

    data = api.load_config()
    env_key = (os.environ.get("OPENSUBTITLES_KEY") or "").strip()
    file_key = data.get("opensubtitles_key")
    file_key = file_key.strip() if isinstance(file_key, str) else ""
    if env_key:
        source, shown = "$OPENSUBTITLES_KEY", _mask(env_key)
    elif file_key:
        source, shown = "配置文件", _mask(file_key)
    else:
        source, shown = "未设置", "——"
    print(f"配置文件：{path}" + ("" if path.exists() else "（不存在）"))
    print(f"OpenSubtitles key：{shown}（来源：{source}）")
    print("解析顺序：--os-key > $OPENSUBTITLES_KEY > 配置文件")
    if not path.exists():
        print("建骨架：python -m dvpack config --init")
    elif not file_key and not env_key:
        print("写 key ：python -m dvpack config --os-key <key>")
    return 0

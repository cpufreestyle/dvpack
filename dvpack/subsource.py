"""字幕来源分级：文本轨 → 缓存 → 在线（仅后台）。

分级焊死不可换序：

  L1 Jellyfin 文本字幕流（jfpack.fetch_subtitle_texts）。局域网秒级；片库
     索引到的内嵌 subrip/ass **和外挂边车 .srt/.vtt**（Jellyfin 把边车也
     列成文本流）都走这里。2026-10-05 全库实测：70 部里 42 部有文本轨
     （39 部中英齐），PGS-only 0 部、零字幕 28 部（纪录片）。
  L2 _subs/ 缓存：先前在线腿抓到的字幕落盘，重打包/换弹/续期直接命中，
     不重复联网，也不重复付公网时延。
  L3 在线（tools/subfetch.py：OpenSubtitles → SubHD → SubtitleCat）。公网
     往返分钟级且出站可能被拦，**只允许后台 worker 调用**：/jf/pack 的
     302 热路径绝不联网——AVPlayer 等 302 超时即「点片没反应/播放失败」。
     抓到的字幕只做两件事：落 L2 缓存 + 给已在播的包只换字幕轨
     （subs.upgrade_master_subtitles，不重打包视频）。

负缓存：在线抓取失败也落 meta（failed_at + 原因），同一指纹
DVPACK_SUBS_RETRY_HOURS 内不再重试——否则 28 部无字幕片每次点片都后台
烧一次公网，日志还被刷爆。
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping

from .bmff import atomic_write

#: 命中的来源层级；空串 = 颗粒无收。
JELLYFIN = "jellyfin"
CACHE = "cache"
ONLINE = "online"

#: 双语口径与 subs.BILINGUAL_TRACKS 对齐；缓存只存这两种语言。
LANGS = ("zh", "en")

#: 在线腿默认开；显式置 0/false/no/off 关。<br>
#: 「开」的含义仅限后台：热路径（resolve_local_texts）永远不联网。
ONLINE_ENV = "DVPACK_SUBS_ONLINE"

#: 负缓存 TTL（秒）：在线失败后多久才允许再试。默认 6 小时。
RETRY_HOURS_ENV = "DVPACK_SUBS_RETRY_HOURS"
_RETRY_HOURS_DEFAULT = 6.0

#: 在线子进程墙上限：subfetch 自己的 curl 超时加起来可能几分钟，兜底砍掉。
TIMEOUT_ENV = "DVPACK_SUBS_ONLINE_TIMEOUT"
_TIMEOUT_DEFAULT = 240.0

_OFF_WORDS = frozenset({"0", "false", "no", "off"})


@dataclass(frozen=True)
class SubtitleResolution:
    """一次字幕解析的事实：文本 + 命中层级 + 备注（供日志与排障）。"""

    texts: dict[str, str] = field(default_factory=dict)
    level: str = ""
    note: str = ""

    @property
    def ok(self) -> bool:
        return bool(self.texts)


def online_enabled() -> bool:
    """在线腿开关。缺省开——但只有后台路径会走到它。"""
    return os.environ.get(ONLINE_ENV, "1").strip().lower() not in _OFF_WORDS


def retry_after_seconds() -> float:
    """负缓存 TTL（秒）。"""
    raw = os.environ.get(RETRY_HOURS_ENV, "").strip()
    if not raw:
        return _RETRY_HOURS_DEFAULT * 3600
    try:
        hours = float(raw)
    except ValueError:
        return _RETRY_HOURS_DEFAULT * 3600
    return max(0.0, hours) * 3600


def online_timeout() -> float:
    """在线子进程墙上限（秒）。"""
    raw = os.environ.get(TIMEOUT_ENV, "").strip()
    if not raw:
        return _TIMEOUT_DEFAULT
    try:
        return max(5.0, float(raw))
    except ValueError:
        return _TIMEOUT_DEFAULT


def subtitle_cache_dir(root: str | Path, fingerprint: str) -> Path:
    """_subs/<指纹 sha1 前 16 位>：api_key 不落盘，指纹本身也不落盘。"""
    digest = hashlib.sha1(fingerprint.encode("utf-8")).hexdigest()[:16]
    return Path(root) / "_subs" / digest


def _cache_meta(cache_dir: Path) -> dict:
    try:
        payload = json.loads((cache_dir / "meta.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def load_cached_texts(cache_dir: str | Path) -> tuple[dict[str, str], dict]:
    """读缓存字幕与 meta。没缓存或文件坏 → ({}, {})，绝不抛。"""
    base = Path(cache_dir)
    texts: dict[str, str] = {}
    for language in LANGS:
        try:
            body = (base / f"{language}.vtt").read_text(
                encoding="utf-8", errors="replace")
        except OSError:
            continue
        if body.strip():
            texts[language] = body
    return texts, _cache_meta(base)


def save_cached_texts(
    cache_dir: str | Path, texts: Mapping[str, str], source: str, **extra
) -> None:
    """在线字幕落缓存（原子写）。只存 zh/en；空文本跳过。"""
    base = Path(cache_dir)
    base.mkdir(parents=True, exist_ok=True)
    for language in LANGS:
        body = (texts or {}).get(language) or ""
        if not body.strip():
            continue
        atomic_write(base / f"{language}.vtt", body.encode("utf-8"))
    meta = {
        "source": source,
        "fetched_at": time.time(),
        "cues": {language: body.count("-->") for language, body in texts.items()},
    }
    meta.update(extra)
    atomic_write(
        base / "meta.json", json.dumps(meta, ensure_ascii=False).encode("utf-8")
    )


def record_online_failure(cache_dir: str | Path, reason: str) -> None:
    """负缓存：失败也落 meta，retry_after 内 load 侧直接放弃重试。"""
    base = Path(cache_dir)
    base.mkdir(parents=True, exist_ok=True)
    meta = _cache_meta(base)
    meta.update({"failed_at": time.time(), "reason": reason[:500]})
    atomic_write(
        base / "meta.json", json.dumps(meta, ensure_ascii=False).encode("utf-8")
    )


def clear_online_failure(cache_dir: str | Path) -> None:
    """抓成功了：抹掉负缓存，别让旧 failed_at 继续挡路。"""
    base = Path(cache_dir)
    meta = _cache_meta(base)
    if not meta.pop("failed_at", None) and not meta.pop("reason", None):
        return
    atomic_write(
        base / "meta.json", json.dumps(meta, ensure_ascii=False).encode("utf-8")
    )


def cache_blocked(cache_dir: str | Path, retry_after: float | None = None) -> bool:
    """负缓存还在 TTL 内吗（True = 别试了）。"""
    meta = _cache_meta(Path(cache_dir))
    failed_at = meta.get("failed_at")
    if not failed_at:
        return False
    window = retry_after_seconds() if retry_after is None else retry_after
    try:
        return (time.time() - float(failed_at)) < window
    except (TypeError, ValueError):
        return False


def resolve_local_texts(
    src: str,
    info,
    *,
    cache_dir: str | Path | None = None,
    fetcher=None,
) -> SubtitleResolution:
    """本地级解析：L1 Jellyfin 文本轨 → L2 _subs 缓存。绝不联网抓新字幕。

    fetcher 可注入（测试用）；默认 jfpack.fetch_subtitle_texts。info 是
    probe.SourceInfo；为 None 时跳过 L1 只问缓存。
    """
    if info is not None:
        from .jfpack import fetch_subtitle_texts

        fetch = fetcher or fetch_subtitle_texts
        try:
            texts = dict(fetch(src, info) or {})
        except Exception:
            texts = {}
        if texts:
            return SubtitleResolution(texts, JELLYFIN)
    if cache_dir is not None:
        texts, meta = load_cached_texts(cache_dir)
        if texts:
            return SubtitleResolution(
                texts, CACHE, "缓存于 %s" % meta.get("fetched_at", "?")
            )
    return SubtitleResolution({}, "")


def _subfetch_script() -> Path:
    """tools/subfetch.py 的路径：本仓库布局下与包目录平级。"""
    return Path(__file__).resolve().parent.parent / "tools" / "subfetch.py"


def fetch_online_texts(
    *,
    src: str,
    item_id: str,
    name: str,
    out_dir: str | Path,
    timeout: float | None = None,
    run=None,
) -> tuple[dict[str, str], str]:
    """跑 tools/subfetch.py 抓中/英字幕，读回 sub_<lang>.cat.vtt。

    子进程自己联网，请求线程/打包 worker 都不陪它等（serve 只在后台线程
    调这里）。任何一步失败回 ({}, 原因)——在线腿是增强项，炸了也不能影响
    播放；原因交给调用方落负缓存。

    name 是 Jellyfin 真片名（在线检索的依据）；拿不到就整体跳过——用
    stream.mkv 这种 URL 尾名去搜只会浪费公网。
    """
    if not name:
        return {}, "缺片名，跳过在线检索"
    script = _subfetch_script()
    if not script.exists():
        return {}, "subfetch.py 不在 %s" % script.parent
    limit = online_timeout() if timeout is None else timeout
    cmd = [
        sys.executable, str(script),
        "--item", item_id or "unknown",
        "--name", name,
        "--langs", ",".join(LANGS),
        "--jf-dir", str(out_dir),
        "--src", src,
    ]
    # OS key 不在这里解析：子进程 subfetch.py 自己按 --os-key > env >
    # ~/.dvpack/config.json 的顺序取（config_path/os_api_key），且天然继承
    # serve/prewarm 的环境变量；父进程再读一遍 env 只会漏掉配置文件那条路。
    runner = run or subprocess.run
    try:
        proc = runner(
            cmd, capture_output=True, text=True, timeout=limit, check=False
        )
    except subprocess.TimeoutExpired:
        return {}, "subfetch 超时 %ss" % limit
    except OSError as exc:
        return {}, "subfetch 起不来：%s" % exc
    texts: dict[str, str] = {}
    for language in LANGS:
        candidate = Path(out_dir) / f"sub_{language}.cat.vtt"
        try:
            body = candidate.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if body.strip():
            texts[language] = body
    if not texts:
        tail = ((proc.stdout or "") + (proc.stderr or "")).strip()[-300:]
        return {}, "subfetch 无产出（rc=%s）：%s" % (proc.returncode, tail)
    return texts, "subfetch rc=%s %s" % (proc.returncode, "+".join(sorted(texts)))

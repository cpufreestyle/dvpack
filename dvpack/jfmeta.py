"""Jellyfin 元数据客户端：MediaBrowser 授权头登录 + 全库字幕构成审计。

存在理由（2026-10-05 破案）：/Users/AuthenticateByName 恒回 400 的真因不是
账号密码、不是 body 格式，而是缺 **Authorization: MediaBrowser** 头——Jellyfin
的设备授权约定（dvplay 一直带这个头所以 App 能登录；此前 curl 用
X-Emby-Authorization 或裸 POST 一律 400 Error processing request）。头一补，
token 到手，/Items?ids=..&Fields=MediaStreams 就能批量看到每条字幕流。

为什么值得审计：字幕链路分级决策需要事实。2026-10-05 对本库（70 部）实测统计：
subrip 文本轨 42 部（其中 39 部中英齐）、PGS-only **0** 部、零字幕 28 部
（纪录片）。PGS-only 为零意味着「PGS 自动 OCR 接打包链路」受众为零——字幕链路
只保留文本轨与在线下载两条腿，来源分级：Jellyfin 内嵌文本轨（现成）
→ 边车 .srt/.vtt → 在线下载（subfetch，换网络环境）。

serve 集成不需要账号密码：dvplay 传的 src 自带 api_key（客户端登录 token），
token_from_src() 直接取来用即可查 MediaStreams。
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from urllib.parse import parse_qs, urlsplit

# dvplay 同款授权头（JellyfinClient.swift authHeader）；值可被环境变量覆写，
# 避免多机共用一个虚拟设备名。
DEFAULT_CLIENT = "dvpack"
DEFAULT_DEVICE = "Mac"
DEFAULT_VERSION = "1.0"

# 文本字幕（HLS 直写轨的前提）：Jellyfin 的 Codec 字段取值。
TEXT_CODECS = frozenset({"subrip", "mov_text", "text", "webvtt", "ass", "ssa"})
# 图像字幕（PGS/DVD 位图）：无法直写文本轨，只能在线下载替换或忽略。
IMAGE_CODECS = frozenset({"hdmv_pgs", "pgssub", "dvd_sub", "dvbsub", "dvdsub"})


class JfMetaError(RuntimeError):
    """Jellyfin 元数据接口失败（登录被拒、批量查询失败等）。"""


def auth_header(token: str | None = None) -> str:
    """MediaBrowser 授权头。缺 Token= 时是裸设备声明（登录用）。

    Jellyfin 12 的约定：/Users/AuthenticateByName 少了这个头直接 400
    "Error processing request."——补回来 200。
    """
    parts = [
        'MediaBrowser Client="%s"' % os.environ.get("DVPACK_JF_CLIENT", DEFAULT_CLIENT),
        'Device="%s"' % os.environ.get("DVPACK_JF_DEVICE", DEFAULT_DEVICE),
        'DeviceId="%s"' % os.environ.get("DVPACK_JF_DEVICE_ID", "dvpack-mac"),
        'Version="%s"' % os.environ.get("DVPACK_JF_VERSION", DEFAULT_VERSION),
    ]
    if token:
        parts.append('Token="%s"' % token)
    return ", ".join(parts)


def login(base_url: str, username: str, password: str, timeout: float = 15.0) -> str:
    """账号密码登录换 AccessToken。失败抛 JfMetaError（不吞）。"""
    body = json.dumps({"Username": username, "Pw": password}).encode("utf-8")
    req = urllib.request.Request(
        base_url.rstrip("/") + "/Users/AuthenticateByName",
        data=body,
        headers={
            "Authorization": auth_header(),
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.load(resp)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise JfMetaError("登录失败：%s" % exc) from exc
    token = (payload or {}).get("AccessToken") or ""
    if not token:
        raise JfMetaError("登录响应无 AccessToken")
    return token


def token_from_src(src: str) -> str:
    """从片源 URL 取现成 api_key（dvplay packURL 带的就是登录 token）。

    serve 侧白拿：不存账号密码，DV 请求里本来就有客户端 token。
    """
    query = parse_qs(urlsplit(src).query, keep_blank_values=True)
    return (query.get("api_key") or [""])[0]


def media_streams(base_url: str, token: str, item_ids: list[str],
                  timeout: float = 20.0) -> list[dict]:
    """批量查 /Items?ids=..&Fields=MediaStreams，返回 Items 数组。

    每批 20 个 id（同 dvplay 翻页节奏）；任一批失败抛 JfMetaError——调用方
    决定降级，审计 CLI 直接摊给用户看。
    """
    out: list[dict] = []
    for start in range(0, len(item_ids), 20):
        chunk = item_ids[start:start + 20]
        query = "&".join("ids=%s" % iid for iid in chunk)
        url = "%s/Items?Fields=MediaStreams,ProductionYear,Path&%s" % (
            base_url.rstrip("/"), query)
        req = urllib.request.Request(url, headers={
            "Authorization": auth_header(token),
            "Accept": "application/json",
        })
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                payload = json.load(resp)
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise JfMetaError("MediaStreams 查询失败：%s" % exc) from exc
        out.extend(payload.get("Items") or [])
    return out


def item_name(base_url: str, token: str, item_id: str,
              timeout: float = 15.0) -> str:
    """单条目查询，返回 Name（在线字幕检索要用的真片名）。

    在线腿只拿得到 stream.mkv 这种 URL 尾名，拿去搜字幕必空；Jellyfin 的
    Name 才是「纽伦堡 2025」这种可检索形式。查不到抛 JfMetaError，调用方
    降级为跳过在线腿。
    """
    if not item_id:
        raise JfMetaError("缺 item id")
    url = "%s/Items?ids=%s&Fields=Path,ProductionYear" % (
        base_url.rstrip("/"), item_id)
    req = urllib.request.Request(url, headers={
        "Authorization": auth_header(token),
        "Accept": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.load(resp)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise JfMetaError("条目查询失败：%s" % exc) from exc
    items = payload.get("Items") or []
    if not items:
        raise JfMetaError("条目 %s 不存在或无权访问" % item_id)
    return (items[0].get("Name") or "").strip()


def classify_subtitles(media_streams: list[dict]) -> str:
    """字幕构成四分类：text（有文本轨）/ pgs（只有图像轨）/ mixed / none。

    决策表：text → _subtitle_texts 现成抓；pgs → 在线下载（subfetch）；
    mixed → 按 text 走（图像轨忽略）；none → 在线下载。
    """
    subs = [s for s in (media_streams or []) if s.get("Type") == "Subtitle"]
    if not subs:
        return "none"
    codecs = {(s.get("Codec") or "").lower() for s in subs}
    has_text = bool(codecs & TEXT_CODECS)
    has_image = bool(codecs & IMAGE_CODECS)
    if has_text and has_image:
        return "mixed"
    if has_text:
        return "text"
    return "pgs"


def subtitle_languages(media_streams: list[dict]) -> set[str]:
    """文本字幕轨语言集合，归一到 zh/en/其它 ISO 码。

    Jellyfin 报三字母（zho/eng）；zh/en 前缀匹配同时收 zh-hans 等变体。
    """
    langs: set[str] = set()
    for s in media_streams or []:
        if s.get("Type") != "Subtitle":
            continue
        if (s.get("Codec") or "").lower() not in TEXT_CODECS:
            continue
        raw = (s.get("Language") or "").lower()
        if raw.startswith("zh"):
            langs.add("zh")
        elif raw.startswith("en"):
            langs.add("en")
        elif raw:
            langs.add(raw)
    return langs


@dataclass
class AuditRow:
    item_id: str
    name: str
    year: int | None
    kind: str  # classify_subtitles 的四分类
    langs: set[str] = field(default_factory=set)
    path: str = ""


@dataclass
class AuditSummary:
    total: int = 0
    text: int = 0
    pgs: int = 0
    mixed: int = 0
    none: int = 0
    bilingual: int = 0  # text/mixed 里 zh+en 齐的
    rows: list[AuditRow] = field(default_factory=list)


def summarize_subtitles(items: list[dict]) -> AuditSummary:
    """把 /Items 批量结果压成一张审计表（PGS-only=0 这类判决的依据）。"""
    summary = AuditSummary()
    for item in items:
        streams = item.get("MediaStreams") or []
        kind = classify_subtitles(streams)
        langs = subtitle_languages(streams) if kind in ("text", "mixed") else set()
        summary.rows.append(AuditRow(
            item_id=item.get("Id") or "",
            name=item.get("Name") or "",
            year=item.get("ProductionYear"),
            kind=kind,
            langs=langs,
            path=item.get("Path") or "",
        ))
        summary.total += 1
        setattr(summary, kind, getattr(summary, kind) + 1)
        if {"zh", "en"} <= langs:
            summary.bilingual += 1
    return summary


def list_items(base_url: str, token: str,
               include_types: str = "Movie,Episode,MusicVideo,Video",
               limit: int = 200, timeout: float = 30.0) -> list[dict]:
    """递归枚举片库条目（含 MediaStreams），返回 /Items 形状的 dict 列表。

    prewarm 与审计 CLI 共用。两跳：先 /Items?Recursive=true 拿 Path/
    ProductionYear，再 media_streams 每批 20 个补 MediaStreams——单跳
    Fields=MediaStreams 在递归+全字段时响应过重，Jellyfin 也常直接忽略。
    Path 必须带到：dvplay 的 packURL 容器后缀（stream.mkv/stream.mp4）
    就是从它推的，预热重建 src 少了它指纹就对不上现役缓存。
    """
    req = urllib.request.Request(
        "%s/Items?Recursive=true&IncludeItemTypes=%s&Fields=Path,ProductionYear"
        "&Limit=%d" % (base_url.rstrip("/"), include_types, limit),
        headers={"Authorization": auth_header(token), "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            page = json.load(resp)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise JfMetaError("片库枚举失败：%s" % exc) from exc
    items = page.get("Items") or []
    fetched = media_streams(base_url, token, [it["Id"] for it in items if it.get("Id")])
    by_id = {it.get("Id"): it for it in fetched}
    for it in items:
        it["MediaStreams"] = (by_id.get(it["Id"]) or {}).get("MediaStreams", [])
    return items


def main() -> None:
    import argparse

    ap = argparse.ArgumentParser(description="Jellyfin 全库字幕构成审计")
    ap.add_argument("--base", required=True, help="http://host:8096")
    ap.add_argument("--user", default="root")
    ap.add_argument("--pw", default=os.environ.get("DVPACK_JF_PASSWORD", ""),
                    help="密码（或环境变量 DVPACK_JF_PASSWORD）")
    ap.add_argument("--token", default="", help="直接用现成 api_key（跳过登录）")
    ap.add_argument("--ids", default="", help="只审计这些 item id（逗号分隔）")
    ap.add_argument("--list-types", default="Movie,Episode,MusicVideo,Video")
    a = ap.parse_args()
    token = a.token
    if not token:
        if not a.pw:
            raise SystemExit("缺密码：--pw 或 DVPACK_JF_PASSWORD")
        token = login(a.base, a.user, a.pw)
        print("[jfmeta] 登录成功，token 前 8 位 %s…" % token[:8])

    if a.ids:
        items = media_streams(a.base, token, [x for x in a.ids.split(",") if x])
    else:
        items = list_items(a.base, token, include_types=a.list_types)
    summary = summarize_subtitles(items)
    print("总计 %d 部：text %d（中英齐 %d）/ pgs %d / mixed %d / none %d" % (
        summary.total, summary.text, summary.bilingual, summary.pgs,
        summary.mixed, summary.none))
    for row in summary.rows:
        flag = {"text": "T", "pgs": "P", "mixed": "M", "none": "-"}[row.kind]
        print("[%s] %-32s %-4s %s" % (
            flag, (row.name or "")[:32], row.year or "",
            ",".join(sorted(row.langs)) or "无字幕"))


if __name__ == "__main__":
    main()

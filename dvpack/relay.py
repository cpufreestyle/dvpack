"""回环 HTTP 中继：帮被 macOS 本地网络隐私（TCC）挡住的进程读局域网片源。

背景（2026-09-26 全矩阵实测）：launchd 拉起的第三方二进制（uv、python、
homebrew ffmpeg）出站连 RFC1918 一律 EHOSTUNREACH（"No route to host"）；
Apple 平台二进制（/usr/bin/curl、/sbin/ping）豁免；回环连接从不被拦。
serve 只收入站没问题（电视拉分片如常），但 /jf/pack 要 ffprobe/ffmpeg
出站读 Jellyfin 就死——客户端只见 502。

做法：serve 进程内起一个只听 127.0.0.1 的中继。ffmpeg 视角的 src 换成
http://127.0.0.1:<port>/u?u=<url-encoded>；中继按请求拉起 /usr/bin/curl，
透传 Range 与状态码，字节流边下边回。两端都不碰 TCC。
3xx 由中继自己续跳、不透出 Location：ffmpeg 跟重定向会直连局域网地址，
绕开中继再被 TCC 挡回，中继就白建了。

curl 固定 /usr/bin/curl：PATH 里的 homebrew curl 实测被同一道墙挡回。
"""

from __future__ import annotations

import shutil
import subprocess
import threading
import socket
import struct
import time
from dataclasses import dataclass, field
from urllib.parse import parse_qs, quote, urljoin, urlsplit
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# 只认 Apple 平台二进制这个：PATH 里的第三方 curl 同样会被墙。
CURL = Path("/usr/bin/curl")


def _curl() -> str:
    if CURL.exists():
        return str(CURL)
    found = shutil.which("curl")
    if not found:
        raise FileNotFoundError("找不到 curl；中继需要系统 curl 转发请求")
    return found


_lock = threading.Lock()
_base_url: str | None = None


def ensure_relay() -> str:
    """（幂等）确保回环中继在跑，返回基地址 http://127.0.0.1:<port>。"""
    global _base_url
    with _lock:
        if _base_url is not None:
            return _base_url
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), _RelayHandler)
        httpd.daemon_threads = True
        port = httpd.server_address[1]
        threading.Thread(target=httpd.serve_forever, name="dvpack-relay", daemon=True).start()
        _base_url = f"http://127.0.0.1:{port}"
        return _base_url


def ffmpeg_visible_source(src: str) -> str:
    """http(s) 片源换成中继地址；本地路径原样返回。"""
    if urlsplit(src).scheme in ("http", "https"):
        # 必须带参数名：光 quote 出来的串没有 key，parse_qs 取不到 'u'，
        # 实测把每个请求都判成 400（2026-09-26 第一次接线就死在这）。
        return f"{ensure_relay()}/u?u={quote(src, safe='')}"
    return src


# 上游响应里需要透传的头；其余（Server/Date/Transfer-Encoding 等）由本端重算。
# location 故意不收：3xx 由中继自己续跳（见 _open_upstream）。透出去的话
# ffmpeg 会跟重定向直连局域网地址，绕开中继、又被 TCC 挡回 EHOSTUNREACH。
_PASSTHROUGH = (
    "content-type",
    "content-length",
    "content-range",
    "accept-ranges",
    "last-modified",
    "etag",
)

MAX_REDIRECTS = 5
_REDIRECT_STATUS = frozenset({301, 302, 303, 307, 308})

# Range 缓存（Infuse InputStreamCacheRangeAccess/ReadAheadLoader 学习项）：
#
# ffprobe/ffmpeg 的 I/O 形状是「多次小 Range 请求 + 少量顺序大读」。Jellyfin
# 端对同一个 URL 会收到 probe、pack、poster、字幕解析多次重叠请求。这里
# 给每个源 URL 维护一份区间记账：同进程内第二次请求同一区间时直接复用
# 上次拉到的字节，不重复打 Jellyfin（2026-10-04 的 34 路压垮事故就是
# 同一目录 34 个并发请求）。
#
# 刻意不做磁盘缓存：serve 已有产物级缓存（/_jf/<id>/），这一层只省
# 同一进程内的重复网络 I/O。区间上限 32MB，防长片全片驻内存。
RANGE_CACHE_LIMIT = 32 * 1024 * 1024


@dataclass
class _RangeEntry:
    start: int
    end: int  # inclusive
    data: bytes
    total: int | None = None
    content_type: str | None = None
    atime: float = field(default_factory=lambda: 0.0)


class _RangeCache:
    """线程安全的 per-URL Range 缓存：区间合并 + LRU 驱逐。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._ranges: dict[str, list[_RangeEntry]] = {}
        self._total = 0

    def _key(self, url: str) -> str:
        return _safe_url(url)  # key 不带 api_key，防不同 token 同路径分裂

    def lookup(self, url: str, start: int, end: int) -> bytes | None:
        with self._lock:
            entries = self._ranges.get(self._key(url))
            if not entries:
                return None
            hits = [e for e in entries if e.start <= start and end <= e.end]
            if not hits:
                return None
            entry = hits[0]
            entry.atime = time.time()
            return entry.data[start - entry.start : end - entry.start + 1]

    def lookup_entry(self, url: str, start: int, end: int) -> _RangeEntry | None:
        """命中并返回整条 entry（命中路径要回放 total）。"""
        with self._lock:
            entries = self._ranges.get(self._key(url))
            if not entries:
                return None
            hits = [e for e in entries if e.start <= start and end <= e.end]
            if not hits:
                return None
            hits[0].atime = time.time()
            return hits[0]

    def store(self, url: str, start: int, data: bytes, total: int | None = None,
              content_type: str | None = None) -> None:
        with self._lock:
            key = self._key(url)
            entries = self._ranges.setdefault(key, [])
            end = start + len(data) - 1
            # 与现有区间重叠就跳过：先来的数据已覆盖了那个区间
            for existing in entries:
                if existing.start <= start and end <= existing.end:
                    return
            entry = _RangeEntry(start=start, end=end, data=data, total=total,
                                content_type=content_type, atime=time.time())
            entries.append(entry)
            self._total += len(data)
            while self._total > RANGE_CACHE_LIMIT and entries:
                oldest = min(entries, key=lambda e: e.atime)
                entries.remove(oldest)
                self._total -= len(oldest.data)


_range_cache = _RangeCache()


def _parse_range(header: str | None) -> tuple[int, int] | None:
    """"Range: bytes=A-B"（单个有界区间）→ (A, B)；其余形态回 None（不猜）。

    开区间（bytes=A-）与多段 Range 必须回 None：本层只会按"完整覆盖即命中"
    回放，把开区间当 (A,A) 单字节处理会让命中路径只回 1 字节——客户端要的
    是从 A 起的整条流，ffmpeg 的读循环会当场断掉。这些请求走上游完整语义。
    """
    if not header or not header.startswith("bytes="):
        return None
    spec = header[len("bytes="):].strip()
    if "," in spec:
        return None  # 多段 Range 超出本层：回源转发
    first, sep, last = spec.partition("-")
    if not sep or not first or not last:
        return None
    try:
        start, end = int(first), int(last)
    except ValueError:
        return None
    if start < 0 or end < start:
        return None
    return start, end

# 上游假死（TCP 接受连接但一个字节都不回，2026-10-03 Jellyfin 现场）时，
# 等响应头的 readline 会永久阻塞：ffprobe/ffmpeg 挂在半死的 curl 上，
# /jf/pack 永不返回，电视上的症状是「点了海报没反应」。给响应头阶段
# 加看门狗：超时就杀掉 curl，让上层 502 快失败。body 流式转发不设上限
# ——ffprobe 拉全片是分钟级，误杀会截断源文件。
HEADER_TIMEOUT = 12.0


class _UpstreamError(Exception):
    """上游没给出可用响应：curl 起不来、连不上、或 3xx 跳不停。"""


def _safe_url(target: str) -> str:
    """日志与错误报文里只留地址，不落 query——Jellyfin URL 带 api_key。"""
    parts = urlsplit(target)
    query = "?" if parts.query else ""
    return f"{parts.scheme}://{parts.netloc}{parts.path}{query}"


def _reap(proc: subprocess.Popen, detail: bool = False) -> str:
    """收尸 curl 进程并回收管道；detail=True 时返回 stderr 最后一行。"""
    tail = ""
    if proc.stdout is not None:
        try:
            proc.stdout.close()
        except OSError:
            pass
    if detail and proc.stderr is not None:
        err = proc.stderr.read() or b""
        lines = err.decode("utf-8", "replace").strip().splitlines()
        tail = lines[-1].strip()[:300] if lines else ""
    if proc.stderr is not None:
        try:
            proc.stderr.close()
        except OSError:
            pass
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)
    return tail


def _status_of(head_lines: list[bytes]) -> int:
    parts = head_lines[0].decode("latin-1").split(None, 2)
    if len(parts) > 1 and parts[1].isdigit():
        return int(parts[1])
    return 502


def _header_value(head_lines: list[bytes], name: str) -> str | None:
    for raw in head_lines[1:]:
        key, _, value = raw.decode("latin-1").partition(":")
        if key.strip().lower() == name:
            return value.strip()
    return None


def _curl_response(target: str, method: str, rng: str | None):
    """拉起一次 curl 读响应头，返回 (proc, head_lines)。

    -i 让头和体都走 stdout：按行读到空行即响应头结束，proc.stdout 停在
    body 第一个字节上，由调用方接着流式转发。
    """
    argv = [_curl(), "-sS", "-N", "-i", "-X", method]
    if rng:
        argv += ["-H", f"Range: {rng}"]
    argv.append(target)
    proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    head_lines: list[bytes] = []
    done = threading.Event()

    def _watchdog() -> None:
        if not done.wait(HEADER_TIMEOUT):
            proc.kill()  # 杀掉挂死的 curl，readline 随即拿到 EOF

    watcher = threading.Thread(
        target=_watchdog, name="dvpack-relay-watchdog", daemon=True
    )
    watcher.start()
    saw_blank = False
    try:
        while True:
            line = proc.stdout.readline() if proc.stdout is not None else b""
            if not line:
                break  # curl 被杀或上游断开
            if not line.strip():
                saw_blank = True
                break
            head_lines.append(line)
    finally:
        done.set()
    if head_lines and not saw_blank:
        detail = _reap(proc, detail=True)
        raise _UpstreamError(
            f"上游响应头不完整：{_safe_url(target)}"
            + (f" -- {detail}" if detail else "")
        )
    if not head_lines:
        detail = _reap(proc, detail=True)
        raise _UpstreamError(
            f"上游无响应：{_safe_url(target)}" + (f" -- {detail}" if detail else "")
        )
    return proc, head_lines


class _RelayHandler(BaseHTTPRequestHandler):
    # HTTP/1.0：响应写完即关连接，不维护 keep-alive 状态，ffmpeg 无副作用。
    protocol_version = "HTTP/1.0"
    server_version = "dvpack-relay/0.1"

    def do_GET(self) -> None:
        self._relay(with_body=True)

    def _fail(self, code: int, detail: str) -> None:
        """回错误码，原因写进 UTF-8 正文。

        不能走 send_error：状态行按 latin-1 编码，中文原因会抛
        UnicodeEncodeError——错误路径自己炸穿连接，客户端又只剩 http 000。
        """
        body = detail.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        try:
            self.wfile.write(body)
        except OSError:
            pass  # 客户端已经走了

    def do_HEAD(self) -> None:
        self._relay(with_body=False)

    def _open_upstream(self, target: str):
        """读上游，3xx 自己续跳，直到拿到能转发的响应。"""
        for _ in range(MAX_REDIRECTS + 1):
            proc, head_lines = _curl_response(
                target, self.command, self.headers.get("Range")
            )
            status = _status_of(head_lines)
            location = _header_value(head_lines, "location")
            if status not in _REDIRECT_STATUS or not location:
                return proc, head_lines, status
            _reap(proc)
            target = urljoin(target, location)
        raise _UpstreamError(f"重定向超过 {MAX_REDIRECTS} 跳：{_safe_url(target)}")

    def _relay(self, *, with_body: bool) -> None:
        target = (parse_qs(urlsplit(self.path).query).get("u") or [""])[0]
        if not target:
            self._fail(400, "missing u")
            return

        # Range 缓存命中：不再起 curl。只缓存完整收到的区间（见下方 store）。
        rng = _parse_range(self.headers.get("Range"))
        entry = None
        if rng is not None and with_body:
            entry = _range_cache.lookup_entry(target, rng[0], rng[1])
        if entry is not None:
            cached = entry.data[rng[0] - entry.start : rng[1] - entry.start + 1]
            self.send_response(206)
            self.send_header("Content-Type",
                              entry.content_type or "application/octet-stream")
            total = entry.total if entry.total is not None else entry.end + 1
            self.send_header(
                "Content-Range", f"bytes {rng[0]}-{rng[1]}/{total}"
            )
            self.send_header("Content-Length", str(len(cached)))
            self.end_headers()
            try:
                self.wfile.write(cached)
            except OSError:
                pass
            return

        try:
            proc, head_lines, status = self._open_upstream(target)
        except _UpstreamError as exc:
            # 502 也要带原因：这一层再聋一次，就回到 launchd 事故那个
            # "只知 failed 不知为何" 的现场。
            self._fail(502, str(exc))
            return
        self.send_response(status)
        for raw in head_lines[1:]:
            name, _, value = raw.decode("latin-1").partition(":")
            if name.strip().lower() in _PASSTHROUGH:
                self.send_header(name.strip(), value.strip())
        self.end_headers()

        sent = 0
        received: bytes = b""
        store_candidate = rng is not None and with_body
        content_range_start: int | None = None
        content_range_total: int | None = None
        upstream_type = _header_value(head_lines, "content-type")
        content_range = _header_value(head_lines, "content-range")
        if content_range and content_range.startswith("bytes "):
            try:
                body_spec, _, total_spec = content_range[len("bytes "):].partition("/")
                start_spec, _, _ = body_spec.partition("-")
                content_range_start = int(start_spec)
                content_range_total = int(total_spec) if total_spec.isdigit() else None
            except ValueError:
                content_range_start = None
        if with_body and proc.stdout is not None:
            while True:
                chunk = proc.stdout.read(65536)
                if not chunk:
                    break
                sent += len(chunk)
                if store_candidate and sent <= RANGE_CACHE_LIMIT:
                    received += chunk
                try:
                    self.wfile.write(chunk)
                except OSError:
                    break  # ffmpeg 半路走掉（推测请求/切码率），安静收场

        _reap(proc)

        # 截断检测（2026-10-04 深夜 34 部毒包事故）：上游被 Jellyfin 压断时
        # curl 要么退出码非 0、要么实收字节少于 Content-Length，但 body 转发
        # 循环只看到管道 EOF——旧实现当干净 EOF 转发，ffmpeg 正常退出、产出
        # 2~4 段带 ENDLIST 的「全片」毒包，电视点片只能放 24 秒且永不自愈。
        # 这里对客户端连接发 RST（SO_LINGER (1,0)），让 ffmpeg 报连接错误
        # exit!=0，上层 502 失败——宁可让用户重试，绝不给毒包落盘机会。
        if with_body and proc.stdout is not None:
            declared = _header_value(head_lines, "content-length")
            expected = int(declared) if declared and declared.isdigit() else None
            if expected is not None and (proc.returncode != 0 or sent < expected):
                print(
                    f"[relay] 上游截断：curl rc={proc.returncode} "
                    f"实收 {sent}B < 声明 {expected}B，RST 客户端连接",
                    flush=True,
                )
                try:
                    self.connection.setsockopt(
                        socket.SOL_SOCKET,
                        socket.SO_LINGER,
                        struct.pack("ii", 1, 0),
                    )
                except OSError:
                    pass
            # 完整收到的区间才进缓存：截断事故（实收 < Content-Length）
            # 的数据绝不能被复用，否则缓存会把毒数据固化为"已知好数据"。
            complete = (
                proc.returncode == 0
                and expected is not None
                and sent == expected
                and status == 206
                and content_range_start is not None
                and sent == len(received)
            )
            if complete:
                _range_cache.store(target, content_range_start, received,
                                   total=content_range_total,
                                   content_type=upstream_type)

    def log_message(self, format: str, *args) -> None:  # noqa: A002 - 基类签名
        pass  # 每次探测/分片都记会把 serve 日志淹掉，出错由 502 状态码暴露

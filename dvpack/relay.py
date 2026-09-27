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
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urljoin, urlsplit

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
    while True:
        line = proc.stdout.readline() if proc.stdout is not None else b""
        if not line or not line.strip():
            break
        head_lines.append(line)
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

        if with_body and proc.stdout is not None:
            while True:
                chunk = proc.stdout.read(65536)
                if not chunk:
                    break
                try:
                    self.wfile.write(chunk)
                except OSError:
                    break  # ffmpeg 半路走掉（推测请求/切码率），安静收场

        _reap(proc)

    def log_message(self, format: str, *args) -> None:  # noqa: A002 - 基类签名
        pass  # 每次探测/分片都记会把 serve 日志淹掉，出错由 502 状态码暴露

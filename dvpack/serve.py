"""局域网 HLS 服务：Apple TV 上的 AVPlayer 只能从 HTTP 地址取流。

只服务打包产物，不做发现/浏览。三件事必须对，否则端上表现是"起播就卡"或"根本不播"：
MIME 类型、单区间 Range、列表与分片不同的缓存策略。
"""

from __future__ import annotations

import os
import socket
from dataclasses import dataclass
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# Windows 的注册表常把 .m3u8 映射成乱七八糟的类型，所以不用 mimetypes，显式给表。
MIME_TYPES = {
    ".m3u8": "application/vnd.apple.mpegurl",
    ".mp4": "video/mp4",
    ".m4s": "video/iso.segment",
    ".vtt": "text/vtt",
    ".html": "text/html; charset=utf-8",
    ".json": "application/json",
}

FULL, PARTIAL, UNSATISFIABLE = "full", "partial", "unsatisfiable"


@dataclass(frozen=True)
class ByteRange:
    start: int
    end: int  # 闭区间

    @property
    def length(self) -> int:
        return self.end - self.start + 1


def parse_range(value: str | None, length: int) -> tuple[str, ByteRange | None]:
    """把 Range 头算成 (结论, 区间)。

    多区间只回 `FULL`：RFC 9110 规定服务端不支持多区间时必须忽略 Range 发整个响应，
    回 416 会让客户端直接放弃这个资源。
    """
    if not value or length <= 0:
        return FULL, None
    unit, _, spec = value.partition("=")
    if unit.strip().lower() != "bytes" or not spec or "," in spec:
        return FULL, None
    first, _, last = spec.partition("-")
    try:
        if not first and not last:
            return FULL, None
        if not first:  # bytes=-N：末尾 N 字节
            size = int(last)
            if size <= 0:
                return FULL, None
            return PARTIAL, ByteRange(max(0, length - size), length - 1)
        start = int(first)
        end = int(last) if last else length - 1
    except ValueError:
        return FULL, None
    if start >= length or start > end:
        return UNSATISFIABLE, None
    return PARTIAL, ByteRange(start, min(end, length - 1))


class HlsRequestHandler(SimpleHTTPRequestHandler):
    protocol_version = "HTTP/1.1"  # AVPlayer 会复用连接，1.0 会让它反复重建

    def guess_type(self, path: str) -> str:
        return MIME_TYPES.get(Path(path).suffix.lower()) or super().guess_type(path)

    def do_GET(self) -> None:
        self._respond(body=True)

    def do_HEAD(self) -> None:
        self._respond(body=False)

    def _cache_header(self) -> str:
        # 列表会变（尤其以后做直播式分片追加），分片不会
        return "no-store" if self.path.split("?")[0].endswith(".m3u8") else "public, max-age=3600"

    def _respond(self, *, body: bool) -> None:
        filesystem = self.translate_path(self.path)
        if not os.path.isfile(filesystem):
            # 状态行的 reason phrase 必须是 latin-1，中文会让整个连接被掐断
            # （实测：send_error(404, "没有这个文件") 触发 UnicodeEncodeError，
            #  客户端看到的是 RemoteDisconnected）
            self.send_error(404, "no such file")
            return

        length = os.stat(filesystem).st_size
        verdict, rng = parse_range(self.headers.get("Range"), length)
        if verdict == UNSATISFIABLE:
            self.send_response(416)
            self.send_header("Content-Range", f"bytes */{length}")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        start = rng.start if rng else 0
        size = rng.length if rng else length
        self.send_response(206 if rng else 200)
        self.send_header("Content-Type", self.guess_type(filesystem))
        self.send_header("Content-Length", str(size))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Cache-Control", self._cache_header())
        if rng:
            self.send_header("Content-Range", f"bytes {rng.start}-{rng.end}/{length}")
        self.end_headers()
        if not body:
            return
        with open(filesystem, "rb") as handle:
            handle.seek(start)
            self._pipe(handle, size)

    def _pipe(self, handle, remaining: int, chunk: int = 256 * 1024) -> None:
        stream_file(handle, self.wfile, remaining, chunk=chunk)


def stream_file(handle, sink, remaining: int, *, chunk: int = 256 * 1024) -> int:
    """把 handle 的 remaining 字节写进 sink，客户端中途走掉就安静收场。

    AVPlayer 和 ffprobe 都会发起推测性请求再半路断开（切码率、seek、探测分片头），
    届时往 socket 写会抛 ConnectionResetError。不吞掉它会刷屏 traceback，
    真机排障时把有价值的日志埋掉。
    """
    sent = 0
    try:
        while remaining > 0:
            block = handle.read(min(chunk, remaining))
            if not block:
                return sent
            sink.write(block)
            sent += len(block)
            remaining -= len(block)
    except (ConnectionResetError, BrokenPipeError, TimeoutError):
        return sent
    return sent


def lan_address() -> str:
    """本机在局域网里的地址，不是 127.0.0.1——电视连不上回环。

    UDP connect 不发包，只是让内核告诉我们要用哪个源地址，所以不需要外网通。
    """
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe_socket:
        probe_socket.connect(("8.8.8.8", 80))
        return probe_socket.getsockname()[0]


def serve(root: str | Path, port: int = 8765, *, host: str = "0.0.0.0") -> ThreadingHTTPServer:
    root = Path(root).resolve()
    if not root.is_dir():
        raise FileNotFoundError(f"目录不存在：{root}")
    httpd = ThreadingHTTPServer(
        (host, port), partial(HlsRequestHandler, directory=str(root))
    )
    return httpd

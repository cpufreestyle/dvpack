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


def _is_private_ipv4(address: str) -> bool:
    """RFC1918 私网判定：10/8、172.16/12、192.168/16。

    不能直接用 `ipaddress.is_private`——标准库里 198.18.0.0/15（基准测试段）
    也算 private，而那正是一众 TUN 代理（Surge/Clash 一类）的默认网卡地址，
    拿它喂给电视等于把不可达地址打印出去。
    """
    parts = address.split(".")
    if len(parts) != 4 or not all(part.isdigit() for part in parts):
        return False
    first, second = int(parts[0]), int(parts[1])
    if not (0 <= first <= 255 and 0 <= second <= 255):
        return False
    if first == 10:
        return True
    if first == 172 and 16 <= second <= 31:
        return True
    return first == 192 and second == 168


def _posix_iface_addresses() -> list[str]:
    """getifaddrs 枚举全部网卡的 IPv4 地址（含 TUN），一条都不漏。

    纯 ctypes 零依赖；取地址失败或平台没有 getifaddrs 时返回空列表，
    由调用方退回老策略。
    """
    import ctypes
    from ctypes.util import find_library

    class sockaddr_in(ctypes.Structure):
        # macOS/BSD 的 sockaddr 带 sa_len 前缀、sa_family_t 是 1 字节；Linux 是
        # 2 字节无前缀。两边共用这个布局：family 落在第 1 字节，sin_addr 都在
        # 偏移 4（macOS 的 sin_len 占了第 0 字节）。
        _fields_ = (
            ("sin_len", ctypes.c_ubyte),
            ("sin_family", ctypes.c_ubyte),
            ("sin_port", ctypes.c_uint16),
            ("sin_addr", ctypes.c_ubyte * 4),
            ("sin_zero", ctypes.c_ubyte * 8),
        )

    class ifaddrs(ctypes.Structure):
        pass

    ifaddrs._fields_ = (
        ("ifa_next", ctypes.POINTER(ifaddrs)),
        ("ifa_name", ctypes.c_char_p),
        ("ifa_flags", ctypes.c_uint),
        ("ifa_addr", ctypes.POINTER(sockaddr_in)),
        ("ifa_netmask", ctypes.POINTER(sockaddr_in)),
        ("ifa_dstaddr", ctypes.POINTER(sockaddr_in)),
        ("ifa_data", ctypes.c_void_p),
    )

    libc = ctypes.CDLL(find_library("c") or "libc.so.6", use_errno=True)
    libc.getifaddrs.argtypes = (ctypes.POINTER(ctypes.POINTER(ifaddrs)),)
    libc.freeifaddrs.argtypes = (ctypes.POINTER(ifaddrs),)
    head = ctypes.POINTER(ifaddrs)()
    if libc.getifaddrs(ctypes.byref(head)) != 0:
        return []
    addresses: list[str] = []
    try:
        entry = head
        while entry:
            sockaddr = entry.contents.ifa_addr
            if sockaddr and sockaddr.contents.sin_family == socket.AF_INET:
                addresses.append(socket.inet_ntoa(bytes(sockaddr.contents.sin_addr)))
            entry = entry.contents.ifa_next
    finally:
        libc.freeifaddrs(head)
    return addresses


def _windows_iface_addresses() -> list[str]:
    """GetAdaptersInfo 枚举全部网卡的 IPv4 地址表（iphlpapi）。

    TUN 网卡也会出现在表里，跟着一起被 `_is_private_ipv4` 过滤掉。
    """
    import ctypes

    class IP_ADDR_STRING(ctypes.Structure):
        pass

    IP_ADDR_STRING._fields_ = (
        ("Next", ctypes.POINTER(IP_ADDR_STRING)),
        ("IpAddress", ctypes.c_char * 16),
        ("IpMask", ctypes.c_char * 16),
        ("Context", ctypes.c_ulong),
    )

    class IP_ADAPTER_INFO(ctypes.Structure):
        _fields_ = (
            ("Next", ctypes.POINTER(IP_ADAPTER_INFO)),
            ("ComboIndex", ctypes.c_ulong),
            ("AdapterName", ctypes.c_char * 256),
            ("Description", ctypes.c_char * 128),
            ("AddressLength", ctypes.c_uint),
            ("Address", ctypes.c_byte * 8),
            ("Index", ctypes.c_ulong),
            ("Type", ctypes.c_uint),
            ("DhcpEnabled", ctypes.c_uint),
            ("CurrentIpAddress", ctypes.POINTER(IP_ADDR_STRING)),
            ("IpAddressList", IP_ADDR_STRING),
            ("GatewayList", IP_ADDR_STRING),
            ("DhcpServer", IP_ADDR_STRING),
            ("HaveWins", ctypes.c_int),
            ("PrimaryWinsServer", IP_ADDR_STRING),
            ("SecondaryWinsServer", IP_ADDR_STRING),
            ("LeaseObtained", ctypes.c_ulonglong),
            ("LeaseExpires", ctypes.c_ulonglong),
        )

    iphlpapi = ctypes.WinDLL("iphlpapi")
    GetAdaptersInfo = iphlpapi.GetAdaptersInfo
    GetAdaptersInfo.argtypes = (ctypes.POINTER(IP_ADAPTER_INFO), ctypes.POINTER(ctypes.c_ulong))
    size = ctypes.c_ulong()
    ERROR_BUFFER_OVERFLOW = 111  # 第一次调用只问要多大缓冲区
    if GetAdaptersInfo(None, ctypes.byref(size)) != ERROR_BUFFER_OVERFLOW:
        return []
    buffer = ctypes.create_string_buffer(size.value)
    first = ctypes.cast(buffer, ctypes.POINTER(IP_ADAPTER_INFO))
    if GetAdaptersInfo(first, ctypes.byref(size)) != 0:
        return []
    addresses: list[str] = []
    adapter = first
    while adapter:
        node = adapter.contents.IpAddressList
        while node:
            addresses.append(node.IpAddress.decode("ascii"))
            node = node.Next
        adapter = adapter.contents.Next
    return addresses


def _lan_rank(address: str) -> int:
    """私网段内部的优先级：192.168 在家用路由器里占比最高，其次 172.16/12、10/8。

    机器同时挂企业 VPN（10/8）和家用 LAN 时，按这个顺序才大概率命中电视所在网段。
    """

    first = int(address.split(".")[0])
    if first == 192:
        return 0
    if first == 172:
        return 1
    return 2


def _default_route_address() -> str | None:
    """UDP connect 不发包，只是让内核报默认路由的源地址，所以不需要外网通。"""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe_socket:
        probe_socket.connect(("8.8.8.8", 80))
        return probe_socket.getsockname()[0]


def lan_address() -> str:
    """本机在局域网里的地址，不是 127.0.0.1——电视连不上回环。

    先枚举网卡、取 RFC1918 私网段里最像家用路由器的一段（实测：Mac 上
    TUN 代理把默认路由劫到 198.18.0.1，老实现只做 UDP connect，打印给
    Apple TV 的 URL 根本连不上）。枚举一无所获才退回默认路由探测。
    """
    addresses = _windows_iface_addresses() if os.name == "nt" else _posix_iface_addresses()
    private = sorted(
        (address for address in addresses if _is_private_ipv4(address)),
        key=lambda address: (_lan_rank(address), address),
    )
    if private:
        return private[0]
    return _default_route_address() or "127.0.0.1"


def serve(root: str | Path, port: int = 8765, *, host: str = "0.0.0.0") -> ThreadingHTTPServer:
    root = Path(root).resolve()
    if not root.is_dir():
        raise FileNotFoundError(f"目录不存在：{root}")
    httpd = ThreadingHTTPServer(
        (host, port), partial(HlsRequestHandler, directory=str(root))
    )
    return httpd

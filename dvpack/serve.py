"""局域网 HLS 服务：Apple TV 上的 AVPlayer 只能从 HTTP 地址取流。

只服务打包产物，不做发现/浏览。三件事必须对，否则端上表现是"起播就卡"或"根本不播"：
MIME 类型、单区间 Range、列表与分片不同的缓存策略。
"""

from __future__ import annotations

import ctypes
import errno
import os
import socket
import shutil
import subprocess
import time
import traceback

try:  # Windows 没有 resource 模块；抬 fd 上限只对 POSIX 有意义
    import resource
except ImportError:  # pragma: no cover
    resource = None  # type: ignore[assignment]
from dataclasses import dataclass
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import json
import re
import threading
from urllib.parse import parse_qs, urlsplit

from .jfpack import JfPackError, pack_source
from .p5 import ResignError
from .probe import NotProbeable
from .remux import FFMPEG, VIDEO_AUDIO_MAPS, VIDEO_ONLY_MAPS, RemuxError
from .tools import require, robust_rmtree
from .subsource import SubtitleResolution

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

# /jf/pack：Jellyfin 片源按需打包端点。电视侧只出一个地址，片源在 Jellyfin
# （Docker）里、容器是 mkv、AVPlayer 不认——由这个端点现打现取。
JFPACK_PATH = "/jf/pack"
JFPURGE_PATH = "/jf/purge"
# /jf/poster：Jellyfin 侧无 Primary 海报图的条目（现场 5 部）由 serve 从
# 片源抽一帧兜底，落 _posters/<id>.jpg。电视侧 posterURL 取不到 tag 时
# 回落这个端点；首请求后台抽帧先回 404，下次进片库页海报即在。
JFPOSTER_PATH = "/jf/poster"
ID_PATTERN = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
SECONDS_LIMITS = (1.0, 600.0)
# True = first pack a short segment so the TV starts instantly, then
# upgrade to full duration in the background (parallel).
INSTANT_FIRST = True
# 首包预览时长：AVPlayer 等 /jf/pack 的 302 有超时，全片打包太慢会让电视
# 直接放弃（「点片没反应」）。先打这个长度让播放器秒起播，全片后台补。
WARM_PACK_SECONDS = 180.0
# 秒开通道：未命中缓存时先只打这么长，几秒内就能 302 让 AVPlayer 起播；
# 全片随后由后台并行补。电视不会因为等打包而黑屏/无反应。
# 2026-10-03：300.0 是「假秒开」——按 24Mbps 的 4K DV 片算，300 秒源
# 要拉 750MB，实测 20s+ 才落 playlist，AVPlayer 等 302 超时就掐连接
# （serve 日志里是 BrokenPipeError），真机症状「点片没反应/播放失败」。
# 20 秒源约 2~4 秒出片，够播放器起播又不让它等。
INSTANT_PACK_SECONDS = 20.0

# pack_source 的已知失败面。catch 不全就会炸穿连接——真链路实测过：
# NotProbeable 穿堂而过，客户端只收到 http 000，serve 日志只剩半截。
# 2026-09-26：launchd 出站被 TCC 拦成 EHOSTUNREACH，就是这个报文形状。
PACK_ERRORS = (JfPackError, NotProbeable, RemuxError, ResignError, ValueError)

_PACK_LOCKS_GUARD = threading.Lock()
# renew counter: item_id -> playlist reload count
_RENEW_GUARD = threading.Lock()
_RENEW_COUNT: dict[str, int] = {}
# renew attempts per item in this process. 全片打包失败（磁盘满 / 上游抽风）时
# marker 停在预热口径，旧代码每 6 次列表重载就再试一遍全片——实测这就是
# 「连点几部片就把盘吃到 100%」的推手，必须按 item 限额。
_RENEW_TRIES: dict[str, int] = {}
_PACK_LOCKS: dict[str, threading.Lock] = {}

# 全片打包（秒开后补 / 续期换装）的全局并发闸。2026-10-04 深夜事故：34 个
# worker 同时拉流，Jellyfin(nuc9) 被压到逐个断流，中继把截断 body 当干净
# EOF 转发，ffmpeg 正常退出、产出 24 秒「全片」毒包。规矩：同一 item 只允许
# 一个 worker，全局最多 2 路 ffmpeg。信号量恒在 item 锁外层（锁序一致，
# 不会和 _jf_pack 的持锁路径打结）。
_FULL_PACK_SEM = threading.BoundedSemaphore(2)
_FULL_PACK_GUARD = threading.Lock()
_FULL_PACK_INFLIGHT: set[str] = set()

# 在线字幕腿（subfetch 公网抓取）的同 item 去重：一部片同时只跑一个抓取
# worker。与 _FULL_PACK_INFLIGHT 同一套思路——点了又点不该攒下一堆线程。
_ONLINE_GUARD = threading.Lock()
_ONLINE_INFLIGHT: set[str] = set()
def _src_fingerprint(src: str) -> str:
    """缓存指纹不带 api_key：token 是会过期的秘密，不落盘。"""
    parts = urlsplit(src)
    kept = [item for item in parts.query.split("&") if not item.lower().startswith("api_key=")]
    return parts._replace(query="&".join(kept)).geturl()


def _pack_src_problem(src: str) -> str | None:
    """_valid_pack_src 的拒绝原因；None = 放行。

    只为 400 日志服务：真机症状是「点片播放失败」，服务端只有一句
    src must be a private-network http url，看不出是主机名解析不了、
    解析到了 loopback/TUN 地址、还是根本没带参数。"""
    try:
        parts = urlsplit(src)
    except ValueError:
        return "urlsplit failed"
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return "scheme/host missing"
    try:
        infos = socket.getaddrinfo(parts.hostname, parts.port or 80, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        return "resolve failed: " + str(parts.hostname) + " (" + str(exc) + ")"
    addresses = {info[4][0] for info in infos}
    if not addresses:
        return "no address: " + str(parts.hostname)
    if not all(_is_private_ipv4(addr) for addr in addresses):
        return "non-private: " + str(parts.hostname) + " -> " + ",".join(sorted(addresses))
    return None


def _valid_pack_src(src: str) -> bool:
    return _pack_src_problem(src) is None


def _pack_lock_for(item_id: str) -> threading.Lock:
    with _PACK_LOCKS_GUARD:
        lock = _PACK_LOCKS.get(item_id)
        if lock is None:
            lock = threading.Lock()
            _PACK_LOCKS[item_id] = lock
        return lock


def _playlist_extinf_sum(playlist: Path) -> float:
    """就地解析 media playlist 的 EXTINF 求和——不跑 ffprobe，读路径零成本。"""
    total = 0.0
    try:
        text = playlist.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return 0.0
    for line in text.splitlines():
        if line.startswith("#EXTINF:"):
            try:
                total += float(line[len("#EXTINF:"):].split(",", 1)[0])
            except ValueError:
                continue
    return total


def _pack_poisoned(meta: dict, out_dir: Path) -> bool:
    """自称全片（marker seconds=0）但 playlist 明显短于源时长的毒包。

    2026-10-04 深夜事故产物：34 个 marker 写 seconds=0.0、实际只有 2~4 段
    （约 24s）+ ENDLIST。_cache_fresh 对它永久命中、_maybe_renew_pack 见
    seconds==0 直接跳过，电视点片只能放 24 秒且永不自愈。判据：写 marker 时
    记下的源时长 plen，EXTINF 和 < plen*0.6 即毒；老 marker 没有 plen 时
    （旧代码产物）用绝对下限兜底——真全片不可能短于 2 分钟。
    """
    if meta.get("seconds") != 0:
        return False
    playlist = out_dir / "video.m3u8"
    if not playlist.exists():
        return False
    extinf_sum = _playlist_extinf_sum(playlist)
    plen = meta.get("plen") or 0.0
    if plen > 0:
        return extinf_sum < float(plen) * 0.6
    return extinf_sum < 120.0


def _write_marker(out_dir: Path, meta: dict) -> None:
    """原子写 source.json：tmp + os.replace。旧代码直写，ffmpeg 还在跑时
    被读了一半的 JSON 会让 _cache_fresh 把整个包判失效、反复重打。"""
    out_dir.mkdir(parents=True, exist_ok=True)
    tmp = out_dir / ".source.json.tmp"
    tmp.write_text(json.dumps(meta), encoding="utf-8")
    os.replace(tmp, out_dir / "source.json")


def _src_duration_seconds(src: str) -> float:
    """源时长（秒）；探测失败回 0.0。只在写 seconds=0 marker 的后台路径调用，
    不碰 /jf/pack 热路径。"""
    try:
        from .relay import ffmpeg_visible_source
        from .probe import ffprobe_json, parse_probe_json

        fetch = ffmpeg_visible_source(src)
        info = parse_probe_json(ffprobe_json(fetch), Path(src))
        return info.duration or 0.0
    except Exception:
        return 0.0


def _rename_swap(src: Path, dst: Path) -> None:
    """原子交换两个目录（macOS renamex_np RENAME_SWAP）。

    换装期间 /_jf/<id>/video.m3u8 这条路径永远有内容（旧或新 inode），
    AVPlayer 每 ~13s 的 playlist 重载不会撞见 404——两步 rename 中间那条
    「目录消失一瞬」的缝，实测就是中场播放失败。
    """
    if os.name != "darwin":
        # Windows 没有 renamex_np：ctypes.CDLL(None).renamex_np 直接
        # AttributeError，而调用方只捕 OSError。主动抛 ENOSYS 让它落进
        # 已有的两步回落（rename→._trash_*、rename tmp→dst），语义与
        # macOS 上 swap 失败时一致：路径不断流，中间脏目录由 sweep 收。
        raise OSError(
            errno.ENOSYS,
            "renamex_np is darwin-only; caller falls back to two-step rename",
        )
    libc = ctypes.CDLL(None)
    func = libc.renamex_np
    func.restype = ctypes.c_int
    func.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint32]
    rc = func(
        os.fsencode(str(src)), os.fsencode(str(dst)), 0x00000002  # RENAME_SWAP
    )
    if rc != 0:
        raise OSError(ctypes.get_errno(), f"renamex_np swap failed: {src} -> {dst}")


_SWAP_TMP_PREFIXES = ("._upgrade_", "._trash_")
_SWAP_STALE_SECONDS = 45 * 60


def _pid_alive(pid: int) -> bool:
    """pid 是否还活着。查无此进程=False；权限不够等拿不准的一律算活。"""
    if os.name == "nt":
        return _pid_alive_nt(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    return True


def _pid_alive_nt(pid: int) -> bool:
    """Windows 版：OpenProcess 查询，拿不到句柄再看错误码。

    os.kill(pid, 0) 在 Windows 上对死进程抛裸 OSError（WinError 87，不是
    ProcessLookupError）→ 被「拿不准算活」分支吞 → 死 pid 的回收目录白等
    45 分钟 mtime 闸。ERROR_ACCESS_DENIED(5)=进程存在但打不开（算活）；
    其余（87 ERROR_INVALID_PARAMETER 等）=查无此进程。
    """
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if handle:
        kernel32.CloseHandle(handle)
        return True
    return ctypes.get_last_error() == 5  # ERROR_ACCESS_DENIED


def _sweep_stale_swaps(parent: Path) -> int:
    """清死进程留下的 ._upgrade_*/._trash_* 临时目录，返回清掉几个。

    pid 后缀是跨进程并发的保险，但也丢了旧固定名的自愈——固定名下次重跑
    会被 rmtree 顶掉，带 pid 的不会，中断一多就堆垃圾。按 pid 活性补回
    自愈：只碰后缀 pid 已死的；v48 前的老固定名（无 pid 后缀）判不出
    活性，不碰，留给人工。
    """
    swept = 0
    now = time.time()
    for prefix in _SWAP_TMP_PREFIXES:
        for path in parent.glob(prefix + "*"):
            pid_text = path.name[len(prefix):].rsplit("_", 1)[-1]
            if not pid_text.isdigit():
                # v48 前的老固定名（无 pid 后缀）判不出活性，不碰，留给人工。
                continue
            if _pid_alive(int(pid_text)):
                # pid 还活 != 里面还有打包在跑：换装成功后 tmp 装着被替下的
                # 旧包，主进程不删就永久残留。按 mtime 补第二道闸（全片打包
                # 约 10 分钟，45 分钟阈值留足余量）。
                try:
                    if now - path.stat().st_mtime < _SWAP_STALE_SECONDS:
                        continue
                except OSError:
                    continue
            robust_rmtree(path)
            swept += 1
    return swept


def _pack_full_swap(src: str, out_dir: Path, maps, texts, item_id: str) -> None:
    """全片打进兄弟临时目录，marker 落定后整体换装。

    绝不在播 stub 上原地重写：ffmpeg hls muxer 开写即把 video.m3u8 截断成
    1 段，AVPlayer 正读着它 → 中场播放失败（2026-10-04 交接定案）。
    """
    # 临时目录名带 pid：prewarm CLI 与 serve 守护进程是两个进程，_pack_lock_for
    # 只是进程内 threading.Lock 互锁不到。同 item 并发打包若撞固定名临时
    # 目录会互 rmtree 对方在飞的目录——毒包事故同款机理的跨进程版。
    parent = out_dir.parent
    _sweep_stale_swaps(parent)
    tmp = parent / f"._upgrade_{item_id}_{os.getpid()}"
    if tmp.exists():
        robust_rmtree(tmp)
    tmp.mkdir(parents=True)
    try:
        if not _parallel_full(src, tmp, maps, texts):
            pack_source(
                src,
                tmp,
                seconds=0.0,
                maps=maps,
                master_uri=f"/_jf/{item_id}/video.m3u8",
                subtitle_texts=texts,
            )
        _write_marker(
            tmp,
            {
                "src": _src_fingerprint(src),
                "seconds": 0.0,
                "plen": _src_duration_seconds(src),
            },
        )
        if out_dir.exists():
            try:
                _rename_swap(tmp, out_dir)
            except OSError:
                # renamex_np 不可用时的两步回落：旧目录先挪去回收位再换上新
                # 目录，中途那一瞬路径缺失由 _respond 的 404 兜底（tvOS 侧
                # 该次重载失败，下一次重载即恢复）。
                trash = out_dir.parent / f"._trash_{item_id}_{os.getpid()}"
                if trash.exists():
                    robust_rmtree(trash)
                os.rename(out_dir, trash)
                try:
                    os.rename(tmp, out_dir)
                except OSError:
                    if not out_dir.exists():
                        os.rename(trash, out_dir)
                    raise
                robust_rmtree(trash)
            # renamex_np 是「交换」不是「移动」：成功后 tmp 路径下装的是被
            # 替下去的旧包。不删就永远烂在 _jf/._upgrade_* 里（2026-10-18
            # 现场 9 个残根全是它），pid 不死 sweep 也清不动。
            robust_rmtree(tmp)
        else:
            os.rename(tmp, out_dir)
    except BaseException:
        # BaseException 而非常规 Exception：Ctrl-C 来的 KeyboardInterrupt
        # 也要清掉自己的 tmp，否则死 pid 残骸只能等下一轮打包时清扫。
        robust_rmtree(tmp)
        raise


# 海报抽帧（2026-10-04 海报优化）：Jellyfin 侧完全无图的条目由 serve 从片源
# 抽一帧顶上。与打包同一条约束——出站读 Jellyfin 必须走回环中继（TCC）。
_POSTER_SEM = threading.BoundedSemaphore(2)
_POSTER_INFLIGHT: set[str] = set()
_POSTER_GUARD = threading.Lock()
_POSTER_BAD: dict[str, float] = {}
_POSTER_BAD_TTL = 900.0


def _poster_bad_recent(item_id: str) -> bool:
    """该 item 的片源最近已知抽不出帧（磁盘全零之类）。

    只用 TTL 软缓存：到期后允许再试一次，这样用户在 Jellyfin 重下/重导
    文件后不用重启 serve 也能拿到真海报。
    """
    with _POSTER_GUARD:
        failed_at = _POSTER_BAD.get(item_id)
        if failed_at is None:
            return False
        if time.monotonic() - failed_at >= _POSTER_BAD_TTL:
            _POSTER_BAD.pop(item_id, None)
            return False
        return True


def _make_poster(src: str, dest: Path) -> None:
    """从片源 seek 到 12% 处抽一帧当海报（避开片头黑场/台标）。

    落盘 tmp + os.replace 原子替换；首帧过黑（JPEG 体积异常小）时换 35%
    处再抽一次——占位图也比一片死黑的 poster 强。
    """
    from .relay import ffmpeg_visible_source
    from .probe import ffprobe_json, parse_probe_json

    fetch = ffmpeg_visible_source(src)
    try:
        info = parse_probe_json(ffprobe_json(fetch), Path(src))
        duration = info.duration or 0.0
    except Exception:
        duration = 0.0
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.parent / (dest.name + ".tmp")
    last_error = ""
    if duration > 30:
        # 长片：12%（避片头黑场/台标）起抽，过黑再退 35%/50%
        candidates = [duration * 0.12, duration * 0.35 if duration > 60 else duration * 0.5]
    else:
        # 短片（现场 iw3 只 2s）：老代码 seek 到固定 2.0s 正好 EOF，
        # ffmpeg rc=0 但一帧不产，海报永远抽不出来。直接抽第 0 帧。
        candidates = [0.0, 0.0]
    for ts in candidates:
        argv = [
            str(require(FFMPEG)),
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-ss",
            f"{ts:.3f}",
            "-i",
            fetch,
            "-map",
            "0:v:0",
            "-frames:v",
            "1",
            "-vf",
            "scale=400:600:force_original_aspect_ratio=increase,crop=400:600",
            # 片源是 limited-range（tv range）YUV 时 mjpeg 编码器直接拒收：
            # 「Non full-range YUV is non-standard」rc=-22。显式翻成 full-range
            # yuvj420p，海报才落得下来（2026-10-04 纽伦堡/Sean.Combs 现场）。
            "-pix_fmt",
            "yuvj420p",
            "-q:v",
            "4",
            "-f",
            "image2",
            str(tmp),
        ]
        proc = subprocess.run(argv, capture_output=True, text=True)
        if proc.returncode != 0 or not tmp.exists():
            last_error = proc.stderr.strip() or f"rc={proc.returncode}"
            continue
        if tmp.stat().st_size >= 4000 or ts == candidates[-1]:
            os.replace(tmp, dest)
            return
        last_error = f"帧过黑（{tmp.stat().st_size}B）"
        tmp.unlink(missing_ok=True)
    raise RuntimeError(f"海报抽帧失败：{last_error or '未知错误'}")


def _cache_fresh(out_dir: Path, src: str, seconds: float) -> bool:
    marker = out_dir / "source.json"
    if not (out_dir / "master.m3u8").exists() or not marker.exists():
        return False
    try:
        meta = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    if _pack_poisoned(meta, out_dir):
        return False
    return meta.get("src") == _src_fingerprint(src) and meta.get("seconds") == seconds


def _pack_playable(out_dir: Path, src: str) -> bool:
    """磁盘上是否已有一份能立刻起播的产物（秒开段也算，不要求全片）。

    判据只要 master.m3u8 + video.m3u8 在、source.json 的 src 指纹对得上：
    全片与否交给 _maybe_renew_pack / _spawn_full_pack 在后台补，
    请求线程一秒都不该花在 ffmpeg 上。

    2026-10-03 现场：旧实现在 _jf_pack 里只问 _cache_fresh(out_dir, src, seconds)，
    而秒开包的 marker 记的是 INSTANT_PACK_SECONDS，客户端要的是 0.0（全片），
    两个数永远不相等 → 每一次点击都重新现打，还要跟后台全片包抢
    _pack_lock_for；/jf/pack 阻塞几十秒到十几分钟，AVPlayer 等 302 超时
    直接抑连接，真机症状就是「点片没反应 / 播放失败」，serve 日志里留 BrokenPipeError。
    """
    marker = out_dir / "source.json"
    if not (out_dir / "master.m3u8").exists():
        return False
    if not (out_dir / "video.m3u8").exists():
        return False
    if not marker.exists():
        return False
    try:
        meta = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    if _pack_poisoned(meta, out_dir):
        return False
    return meta.get("src") == _src_fingerprint(src)


# 打包前的磁盘闸。一部 4K DV 片全片 5~15GB，_jf 产物按片缓存、旧代码从不淘汰，
# 连点几部就把盘吃到 100%（2026-10-03 现场：9 部 87GB，926GB 盘可用只剩 1.1GB）。
# 打满之后每个分片都 ENOSPC、playlist 被冲空、/jf/pack 回 502，电视侧只剩
# 「播放失败 / Cannot Open」，而真正的原因在 serve 日志里埋着。
# 24GB 而不是 12GB：单部 4K DV 全片包实测 4.4~7.7G、最坏 15G，且全局并发
# 2 路全片 + 秒开包同时写盘。闸小于单包最坏体积时，pack 放一半 ENOSPC、
# playlist 冲空——2026-10-18 现场（15G 可用仍连挂）就是这么来的。
MIN_FREE_BYTES = 24 * 1024 ** 3


def _free_bytes(path: Path) -> float:
    # path 可能还不存在（首包时 _jf/<id> 尚未创建）：disk_usage 对不存在
    # 的路径抛 OSError，旧实现直接回 0.0，磁盘闸把每次首包都误判成「盘满
    # 507」（tests/test_jfpack.py 现场）。逐级上溯到最近的已存在目录再查。
    probe = path
    while not probe.exists() and probe.parent != probe:
        probe = probe.parent
    try:
        return float(shutil.disk_usage(probe).free)
    except OSError:
        return 0.0


def _dir_size_bytes(path: Path) -> float:
    try:
        return float(sum(f.stat().st_size for f in path.rglob("*") if f.is_file()))
    except OSError:
        return 0.0


def _jf_cache_entries(root: Path) -> list[tuple[Path, float]]:
    """_jf 缓存目录 + 最后使用时间（取 video/master 列表的 mtime）。"""
    cache = root / "_jf"
    if not cache.is_dir():
        return []
    entries: list[tuple[Path, float]] = []
    for child in cache.iterdir():
        if not child.is_dir():
            continue
        # ._upgrade_* / ._trash_* 是换装临时目录，不是缓存条目：它们没有
        # master 列表（stamp=0）会被 LRU 当成最旧产物误杀，而里面正跑着
        # 几分钟的全片 ffmpeg。
        if child.name.startswith("."):
            continue
        stamp = 0.0
        for probe in ("video.m3u8", "master.m3u8"):
            try:
                stamp = max(stamp, (child / probe).stat().st_mtime)
            except OSError:
                continue
        entries.append((child, stamp))
    return entries


def _ensure_disk_room(root: Path, need_bytes: float, keep: Path | None = None) -> bool:
    """腾出 need_bytes 可用空间；不够就地按 LRU 淘汰最旧的 _jf 产物。

    返回 False 表示连淘汰都腾不出来，调用方必须拒绝打包而不是硬写——
    硬写的结局是把正在播放的 playlod 状态写坏，比明确报错更难查。
    """
    if _free_bytes(root) >= need_bytes:
        return True
    entries = sorted(
        (entry for entry in _jf_cache_entries(root) if keep is None or entry[0] != keep),
        key=lambda entry: entry[1],
    )
    for child, _stamp in entries:
        if _free_bytes(root) >= need_bytes:
            break
        size = _dir_size_bytes(child)
        print(f"[serve] 磁盘不足，淘汰旧缓存 {child.name}（约 {size / 2**30:.1f}GB）", flush=True)
        robust_rmtree(child)
    return _free_bytes(root) >= need_bytes


def _touch_active_pack(filesystem: str) -> None:
    """播放中的包给 video.m3u8 续命。

    LRU 按 video.m3u8 的 mtime 淘汰旧包，而播放过程只读分片、不改 mtime：
    看了一半的包时间戳停在开播那一刻，等新片打包触发 _ensure_disk_room 时，
    在播包会被当成「最旧」误杀 → 中场 404「播放失败」。每次 serve 视频段/
    playlist 时 touch 一下即防；开销一次 utime，可忽略。
    """
    try:
        path = Path(filesystem)
        parts = path.parts
        if "_jf" not in parts or not path.name.startswith("video"):
            return
        jf = parts.index("_jf")
        # 只认 .../_jf/<id>/video... 这一层，子目录（subs/ 等）不碰
        if jf + 2 != len(parts) - 1:
            return
        playlist = path.parent / "video.m3u8"
        if playlist.is_file():
            os.utime(playlist, None)
    except OSError:
        pass


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
    # keep-alive 没设读超时：电视切台/退后台/换网卡后那条连接就缸在
    # ESTABLISHED/CLOSE_WAIT 没人收，一条会话漏一个 fd（ThreadingHTTPServer 还得配一个线程）。
    # 筍到几百个就 os.pipe() EMFILE（errno 24）：ffprobe 起不来、pack_source
    # 抛异常、/jf/pack 回 502，真机症状正是「播放失败」。
    # 设了超时，rfile.readline() 到点抛 TimeoutError，
    # BaseHTTPRequestHandler 自动 close_connection，线程随 socket 一起回收。
    timeout = 30

    def guess_type(self, path: str) -> str:
        return MIME_TYPES.get(Path(path).suffix.lower()) or super().guess_type(path)


    # 预热包续期：首包只打 WARM_PACK_SECONDS 秒（源可能是 106 分钟），电视播完
    # 这 3 分钟就带 ENDLIST 结束，用户看到的是「反复循环 / 只能看一小段」。
    # AVPlayer 对 VOD playlist 每 ~13s 重载一次 video.m3u8，正好当钩子：重载
    # 够多次就说明预热段被消费完了，此时用全片重打并原子替换。
    _RENEW_AFTER_LOADS = 6
    # 同一 item 在本进程内最多补几次全片。全片打包失败时 marker 停在预热口径，
    # 无上限就等于每 6 次列表重载再打一遍全片——实测把 926GB 盘吃到 100%，
    # 之后每个分片 ENOSPC、/jf/pack 502，电视侧只剩「播放失败」。
    _RENEW_MAX_TRIES = 3

    def _maybe_renew_pack(self) -> None:
        path = self.path.split("?")[0]
        m = re.match(r"^/_jf/([A-Za-z0-9_.-]+)/video\.m3u8$", path)
        if not m:
            return
        item_id = m.group(1)
        out_dir = Path(self.directory) / "_jf" / item_id
        marker = out_dir / "source.json"
        if not marker.exists():
            return
        try:
            meta = json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        cached_seconds = meta.get("seconds")
        poisoned = _pack_poisoned(meta, out_dir)
        # 已经是全片（seconds==0）或未知口径就不动，避免反复重打；毒包例外：
        # 自称全片实则 24 秒，必须立刻重打自愈，不能等用户再点一次。
        if (cached_seconds == 0 or cached_seconds is None) and not poisoned:
            return
        if not poisoned:
            with _RENEW_GUARD:
                _RENEW_COUNT[item_id] = _RENEW_COUNT.get(item_id, 0) + 1
                loads = _RENEW_COUNT[item_id]
            if loads < self._RENEW_AFTER_LOADS:
                return
        with _RENEW_GUARD:
            tries = _RENEW_TRIES.get(item_id, 0)
            if tries >= self._RENEW_MAX_TRIES:
                return
            _RENEW_TRIES[item_id] = tries + 1
        if not _ensure_disk_room(out_dir.parent, MIN_FREE_BYTES, keep=out_dir):
            print(f"[serve] 磁盘不足，取消 {item_id} 全片续期", flush=True)
            return
        src = meta.get("src", "")
        if not (src.startswith("http://") or src.startswith("https://")):
            return
        # 指纹还原不出 api_key（故意不落盘），所以只能用当前 src 试；
        # Jellyfin 的 static 直连在会话存活期内不需要 api_key。
        # 全片换装分钟级：丢后台线程，绝不能占着电视的 playlist 重载请求
        # 同步跑（旧实现占着请求线程几分钟，重载超时即中场播放失败）。
        def worker() -> None:
            with _FULL_PACK_SEM:
                with _pack_lock_for(item_id):
                    # 双检：等锁/等信号量期间可能另一个线程刚换装完
                    try:
                        now = json.loads(marker.read_text(encoding="utf-8"))
                    except (OSError, json.JSONDecodeError):
                        return
                    if now.get("seconds") == 0 and not _pack_poisoned(now, out_dir):
                        return
                    try:
                        resolution = _subtitle_texts(src, root=self.directory)
                        _pack_full_swap(src, out_dir, VIDEO_AUDIO_MAPS,
                                        resolution.texts, item_id)
                        with _RENEW_GUARD:
                            _RENEW_COUNT[item_id] = 0
                        if not resolution.texts:
                            self._spawn_online_subs(src, item_id, out_dir)
                    except Exception:
                        traceback.print_exc()

        threading.Thread(target=worker, daemon=True, name=f"dvpack-renew-{item_id}").start()

    def do_GET(self) -> None:
        path = self.path.split("?")[0]
        if path == JFPACK_PATH:
            self._jf_pack()
            return
        if path == JFPOSTER_PATH:
            self._jf_poster()
            return
        if path == JFPURGE_PATH:
            self._jf_purge()
            return
        self._maybe_renew_pack()
        self._respond(body=True)

    def do_HEAD(self) -> None:
        self._respond(body=False)

    def _cache_header(self) -> str:
        # 一律 no-store：重打包会换掉 init 段口径（2026-09-26 换 range 重打包，
        # ATV 握着 max-age=3600 的旧 init 就是「颜色不对 + 不亮」的复发现场）。
        # 局域网带宽不是瓶颈，缓存命中也换不来什么。
        return "no-store"

    def _respond(self, *, body: bool) -> None:
        filesystem = self.translate_path(self.path)
        if not os.path.isfile(filesystem):
            # 状态行的 reason phrase 必须是 latin-1，中文会让整个连接被掐断
            # （实测：send_error(404, "没有这个文件") 触发 UnicodeEncodeError，
            #  客户端看到的是 RemoteDisconnected）
            self.send_error(404, "no such file")
            return
        # 在播包续命：LRU 按 video.m3u8 mtime 淘汰，播放只读不改 mtime，
        # 不 touch 的话看了一半的包会被新打包的磁盘闸当中场误杀
        _touch_active_pack(filesystem)

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

    def _jf_pack(self) -> None:
        query = parse_qs(urlsplit(self.path).query)
        src = (query.get("src") or [""])[0]
        item_id = (query.get("id") or [""])[0]
        raw_seconds = (query.get("seconds") or ["0"])[0]  # 0 = full source duration
        # audio=0 → 视频-only 打包（drop 音轨）。tvOS 上 eac3 音轨与 video track
        # 的 mdhd timescale 不同步时 AVPlayer 会在某一片之后 stall，实测停在第 8 片
        # 附近并触发自动重播。留 audio=1（默认）保持音画完整。
        raw_audio = (query.get("audio") or ["1"])[0]
        if not _valid_pack_src(src):
            # 400 的真实原因必须落盘：否则真机「点片播放失败」只能看到一句
            # 地址不合规，无从判断是主机名、loopback 还是参数缺失。src 走
            # _src_fingerprint 脱敏（剥 api_key）。
            print(
                "[serve] /jf/pack 400 ("
                + (_pack_src_problem(src) or "unknown")
                + ") src="
                + _src_fingerprint(src),
                flush=True,
            )
            self.send_error(400, "src must be a private-network http url")
            return
        if not ID_PATTERN.match(item_id):
            self.send_error(400, "bad item id")
            return
        # 点号过得 ID_PATTERN，但纯点号 id（..、...）会把 out_dir 变成
        # <root>/_jf/.. ——即静态根目录本身，打包产物直接写进 serve 根
        if set(item_id) <= {"."}:
            self.send_error(400, "bad item id")
            return
        try:
            parsed_seconds = float(raw_seconds)
        except ValueError:
            self.send_error(400, "bad seconds")
            return
        # 0 = full source duration. hls_command omits -t for falsy seconds,
        # so the client gets the complete Jellyfin movie instead of a 60s preview.
        if parsed_seconds == 0:
            seconds = 0.0
        else:
            seconds = min(SECONDS_LIMITS[1], max(SECONDS_LIMITS[0], parsed_seconds))

        if raw_audio not in ("0", "1"):
            self.send_error(400, "bad audio")
            return
        maps = VIDEO_ONLY_MAPS if raw_audio == "0" else VIDEO_AUDIO_MAPS

        out_dir = Path(self.directory) / "_jf" / item_id
        # 无锁快路：产物已在盘上（秒开段或全片）秒回 302。这次读盘绝不能排在
        # _pack_lock_for 后面——后台全片 worker 持同一把非重入锁跑好几分钟，
        # 期间每个 /jf/pack 轮询都阻塞在锁上直到客户端超时，serve 日志连请求
        # 行都不打，真机症状「无法播放」（2026-10-03 纽伦堡现场：秒开包早已
        # 在盘，点击后 /jf/pack 零日志）。
        if _pack_playable(out_dir, src) or _cache_fresh(out_dir, src, seconds):
            self._send_pack_redirect(item_id)
            return
        # Declared outside the lock so the post-lock spawn can always see it
        # (cache-hit path never assigns it).
        _need_full_upgrade = False
        # 字幕抓取放在持锁前：ffprobe + 字幕 HTTP 要几秒，不该占着打包锁
        # 字幕抓取放在持锁前：ffprobe + 字幕 HTTP 要几秒，不该占着打包锁。
        # 只走本地级（文本轨 → _subs 缓存）：在线腿分钟级，绝不上这条热路径。
        resolution = _subtitle_texts(src, root=self.directory)
        texts = resolution.texts
        if resolution.level:
            print("[serve] 字幕 %s 来自 %s %s" % (
                item_id, resolution.level, resolution.note), flush=True)
        # 同一个 item 的请求（电视重试、点完再点）串行化，避免并发写同一份包
        with _pack_lock_for(item_id):
            # 已有可播产物（秒开段或全片）就绝不再现打：旧实现只问 _cache_fresh(out_dir,
            # src, seconds)，而秒开包的 marker 记的是 INSTANT_PACK_SECONDS、客户端要的是 0.0（全片），
            # 两个数永远不相等 → 每次点击都重新现打并与后台全片包抢锁，
            # /jf/pack 阻塞几十秒到十几分钟，AVPlayer 超时抖连接。真机症状：点片没反应。
            if not _pack_playable(out_dir, src) and not _cache_fresh(out_dir, src, seconds):
                # 毒包整目录淘汰（marker 自称全片实则只有 24 秒）：不给重打
                # 留半份坏产物当基底。判据在 _pack_poisoned。
                try:
                    _meta_now = json.loads(
                        (out_dir / "source.json").read_text(encoding="utf-8")
                    )
                except (OSError, json.JSONDecodeError):
                    _meta_now = {}
                if _pack_poisoned(_meta_now, out_dir):
                    print(f"[serve] 淘汰毒包 {item_id}，整目录重打", flush=True)
                    robust_rmtree(out_dir)
                # 磁盘闸：连淘汰都腾不出 12GB 就明确回 507。硬写的结局是 ffmpeg
                # 每写一片 ENOSPC、playlist 被冲空，电视侧只剩「播放失败」而
                # serve 日志里埋着真正的 Errno 28（2026-10-03 现场）。
                if not _ensure_disk_room(out_dir.parent, MIN_FREE_BYTES, keep=out_dir):
                    print(f"[serve] 磁盘不足，拒绝打包 {item_id}", flush=True)
                    self.send_error(507, "disk full, pack refused")
                    return
                # 上次并行打包失败留下的 _part* 中间态：既占盘又是脏线索。
                for stale in out_dir.glob("_part*"):
                    robust_rmtree(stale)
                # 首包时长权衡：
                # · 打全片：34 分钟的片要 2~3 分钟，AVPlayer 等 302 超时即放弃，
                #   真机症状就是「点片没反应 / 播放失败」。
                # · 打太短：ffmpeg 的 HLS muxer 只在进程结束时落最终 playlist，
                #   12 秒源只产出 2 片，电视播完即停（后台线程也补不上来，因为
                #   playlist 不增量）。
                # 所以取 WARM_PACK_SECONDS：打包约 20~40s（可接受），且能切出
                # 十几个分片形成完整播放体验；看完全片由缓存+后续重打覆盖。
                warm = min(WARM_PACK_SECONDS, seconds) if seconds else WARM_PACK_SECONDS
                _par_ok = False
                try:
                    if seconds == 0 and INSTANT_FIRST:
                        # 秒开：先打 INSTANT_PACK_SECONDS（几秒完成）让电视立刻
                        # 起播，全片丢后台并行补。避免第一次点片干等几分钟。
                        pack_source(
                            src,
                            out_dir,
                            seconds=INSTANT_PACK_SECONDS,
                            maps=maps,
                            master_uri=f"/_jf/{item_id}/video.m3u8",
                            subtitle_texts=texts,
                        )
                        _need_full_upgrade = True
                    elif seconds == 0:
                        # 全片直接并行打：实测 34 分钟/5GB 的源单进程要
                        # 2~3 分钟（ffmpeg 只吃 ~5% CPU，瓶颈是单连接拉流），
                        # 4 路并行把墙钟压到约 1/4。失败自动回落单进程预热。
                        if _parallel_full(src, out_dir, maps, texts):
                            _par_ok = True
                        else:
                            pack_source(src, out_dir, seconds=warm, maps=maps,
                                        master_uri=f"/_jf/{item_id}/video.m3u8",
                                        subtitle_texts=texts)
                    else:
                        pack_source(
                            src,
                            out_dir,
                            seconds=warm,
                            maps=maps,
                            master_uri=f"/_jf/{item_id}/video.m3u8",
                            subtitle_texts=texts,
                        )
                except PACK_ERRORS:
                    # 端点必须回 502，不能把连接掐断（客户端只见 http 000）；
                    # 真实原因落进 serve 日志，排障不再现场全盲。
                    traceback.print_exc()
                    self.send_error(502, "pack failed")
                    return
                # 指纹不带 api_key，见 _src_fingerprint
                # 记实际产出的口径：并行全片成功就记 0.0（_cache_fresh 由此
                # 认这份全片包，后续请求秒回）；否则记 warm 让续期去补。
                if seconds == 0 and _par_ok:
                    marker_seconds = 0.0
                elif seconds == 0 and INSTANT_FIRST:
                    # 秒开只产出一段；记实际长度，_maybe_renew_pack 会在
                    # 电视播完这段后并行补全片并把 marker 改成 0.0。
                    marker_seconds = INSTANT_PACK_SECONDS
                else:
                    marker_seconds = warm
                marker_meta = {"src": _src_fingerprint(src), "seconds": marker_seconds}
                if marker_seconds == 0.0:
                    # plen=源时长，供读路径毒包识别；30 秒以内的短片没有全片
                    # 闸也不需要它。
                    marker_meta["plen"] = _src_duration_seconds(src)
                _write_marker(out_dir, marker_meta)

        # Spawn the full-duration upgrade only after the lock is released:
        # the worker takes the same non-reentrant lock, and holding it while
        # the 302 is still being written made the client see an empty reply.
        if _need_full_upgrade:
            self._spawn_full_pack(src, item_id, maps)
        if not texts:
            # 无字幕片（全库 28 部）：后台在线腿试一把。抓取分钟级且只跑
            # 守护线程，绝不占这条 302 请求——AVPlayer 等超时即「点片没反应」。
            self._spawn_online_subs(src, item_id, out_dir)

        self._send_pack_redirect(item_id)

    def _send_pack_redirect(self, item_id: str) -> None:
        """302 到 master.m3u8：URLSession 与 AVPlayer 都会自动跟随。"""
        # 302 让绝对路径的 master 落地；URLSession（ManifestInspector）与
        # AVPlayer 都会自动跟随。必须指 master.m3u8 而不是
        # media playlist——CODECS="dvh1.05.xx" 与 VIDEO-RANGE=PQ 只写在 master
        # 里（playlist.py 论证），直出 media playlist 会把这两个信号全
        # 丢掉，tvOS 不走 DV 解码通路。2026-09-27 起徽标不亮的根因。
        self.send_response(302)
        self.send_header("Location", f"/_jf/{item_id}/master.m3u8")
        self.send_header("Content-Length", "0")
        self.end_headers()


    def _jf_purge(self) -> None:
        """App 删远端影片后联动删本机 pack 缓存：GET /jf/purge?id=<itemId>。

        Jellyfin DELETE /Items 只删服务器那一侧，本机 _jf/<id>（一部 4K DV
        5~21GB）没人动就成了孤儿包，磁盘迟早被吃满。带 id 走 _pack_lock_for
        同锁：不能在一部片正在后台补全片的 worker 脚下抽目录。抢不到锁就
        503 让 App 稍后重试（App 侧失败只记日志，JF 删除已成功）。
        """
        query = parse_qs(urlsplit(self.path).query)
        item_id = (query.get("id") or [""])[0]
        # 点引用既是合法文件名又能穿过 ID_PATTERN（点就在字符类里），而 pathlib
        # 不会提前展开它们：id=.. 时 out_dir.parent 依旧等于 cache，rmtree 会把
        # 整个服务根目录一起端走。resolve 后必须仍是 _jf 下的一级子目录。
        if not ID_PATTERN.fullmatch(item_id) or item_id.strip(".") == "":
            self.send_error(400, "bad id")
            return
        cache = Path(self.directory) / "_jf"
        out_dir = cache / item_id
        cache_root = cache.resolve()
        try:
            target = out_dir.resolve().relative_to(cache_root)
        except ValueError:
            self.send_error(400, "bad id")
            return
        if not target.parts or len(target.parts) != 1:
            self.send_error(400, "bad id")
            return
        lock = _pack_lock_for(item_id)
        if not lock.acquire(timeout=5.0):
            self.send_error(503, "pack in progress")
            return
        try:
            existed = out_dir.exists()
            freed = _dir_size_bytes(out_dir) if existed else 0.0
            if existed:
                robust_rmtree(out_dir)
            # 海报兜底产物一起删：App 里删片要「本地彻底删除」，抽帧海报
            # 虽是增强项也不该在片删掉后继续占着 _posters。
            poster = Path(self.directory) / "_posters" / (item_id + ".jpg")
            if poster.exists():
                poster.unlink(missing_ok=True)
        finally:
            lock.release()
        print("[serve] purge %s：释放 %.2fGB（existed=%s）" % (item_id, freed / 2**30, existed), flush=True)
        payload = {"id": item_id, "existed": bool(existed), "freed_bytes": int(freed)}
        body = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _jf_poster(self) -> None:
        """海报兜底端点：GET /jf/poster?id=<itemId>&src=<匿名静态流>。

        Jellyfin 侧无 Primary 图的条目（现场 5 部：iw3、Sean.Combs 纪录片
        4 集），App 的 posterURL 拿不到 tag 时回落这里。命中缓存直接发图；
        未命中起后台抽帧线程（同 item 去重 + 全局 2 路信号量），本次回 404
        让卡片显示占位图——同步抽一帧要 5~20s，占着电视的海报请求会把整面
        墙的加载拖死。下次进片库页海报即在（PosterImage 每 URL 只加载一次）。
        """
        query = parse_qs(urlsplit(self.path).query)
        item_id = (query.get("id") or [""])[0]
        src = (query.get("src") or [""])[0]
        if not ID_PATTERN.fullmatch(item_id) or item_id.strip(".") == "":
            self.send_error(400, "bad id")
            return
        if not _valid_pack_src(src):
            self.send_error(400, "bad src")
            return
        posters = Path(self.directory) / "_posters"
        dest = posters / (item_id + ".jpg")
        # 与 _jf_purge 同一道防越界：resolve 后必须仍停在 _posters 一级。
        try:
            dest.resolve().relative_to(posters.resolve())
        except ValueError:
            self.send_error(400, "bad id")
            return
        if dest.exists():
            self._send_poster(dest)
            return
        if _poster_bad_recent(item_id):
            self.send_error(404, "poster source dead")
            return
        with _POSTER_GUARD:
            fresh = item_id not in _POSTER_INFLIGHT
            if fresh:
                _POSTER_INFLIGHT.add(item_id)
        if fresh:
            print(f"[serve] 海报抽帧入队 {item_id}", flush=True)
            threading.Thread(
                target=self._poster_worker, args=(src, dest, item_id), daemon=True
            ).start()
        # 404 而不是挂起：占位图先顶上，抽帧完下一次进页即真图。
        self.send_error(404, "poster pending")

    def _poster_worker(self, src: str, dest: Path, item_id: str) -> None:
        try:
            with _POSTER_SEM:
                _make_poster(src, dest)
                print(f"[serve] 海报抽帧完成 {item_id}", flush=True)
        except Exception as exc:
            with _POSTER_GUARD:
                _POSTER_BAD[item_id] = time.monotonic()
            head = str(exc).strip().splitlines()
            detail = head[0][:160] if head else exc.__class__.__name__
            print(
                f"[serve] 海报抽帧失败 {item_id}（源无有效媒体流，负缓存 "
                f"{int(_POSTER_BAD_TTL)}s）：{exc.__class__.__name__}: {detail}",
                flush=True,
            )
        finally:
            with _POSTER_GUARD:
                _POSTER_INFLIGHT.discard(item_id)

    def _send_poster(self, dest: Path) -> None:
        data = dest.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "image/jpeg")
        self.send_header("Content-Length", str(len(data)))
        # 海报落盘后不再变（换片源=新 item id），给一小时缓存少拉几趟。
        self.send_header("Cache-Control", "max-age=3600")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def _spawn_full_pack(self, src: str, item_id: str, maps: tuple[str, ...]) -> None:
        """后台补全片：首包只打了 FAST_PACK_SECONDS 秒，这里接着打完整片。

        跑在 daemon 线程里，不阻塞 302 响应。补完后把 source.json 的 seconds
        改回 0.0，这样 _cache_fresh 之后会认这份全片包，AVPlayer 拉到尾片时
        自然接上，无需重新点片。
        """
        def worker() -> None:
            out_dir = Path(self.directory) / "_jf" / item_id
            # 同 item 去重：秒开期间连点会攒下多个 worker，旧代码 34 个同时
            # 拉流把 Jellyfin 压到逐个断流——毒包事故的直接推手。
            with _FULL_PACK_GUARD:
                if item_id in _FULL_PACK_INFLIGHT:
                    return
                _FULL_PACK_INFLIGHT.add(item_id)
            try:
                if not _ensure_disk_room(out_dir.parent, MIN_FREE_BYTES, keep=out_dir):
                    print(f"[serve] 磁盘不足，取消 {item_id} 后台全片打包", flush=True)
                    return
                resolution = _subtitle_texts(src, root=self.directory)
                texts = resolution.texts
                # 全局最多 2 路全片 ffmpeg；信号量在 item 锁外层，锁序一致
                # （SEM → item lock），不会和 _jf_pack 的持锁路径打结。
                with _FULL_PACK_SEM:
                    with _pack_lock_for(item_id):
                        try:
                            for stale in out_dir.glob("_part*"):
                                robust_rmtree(stale)
                            # 排队复查：等锁/等信号量时前一个 worker 可能已把
                            # 全片打完并把 marker 翻成 0.0。不复查就把整片再
                            # 拉一遍——白耗约 10 分钟 4 路 ffmpeg，还把磁盘推
                            # 向 ENOSPC（502/播放失败的直接推手）。
                            _marker = out_dir / "source.json"
                            if _marker.exists() and (out_dir / "video.m3u8").exists():
                                try:
                                    _meta = json.loads(_marker.read_text(encoding="utf-8"))
                                except (OSError, json.JSONDecodeError):
                                    _meta = {}
                                if (_meta.get("seconds") == 0.0
                                        and _meta.get("src") == _src_fingerprint(src)
                                        and not _pack_poisoned(_meta, out_dir)):
                                    return
                            # 换装进兄弟临时目录：绝不在播 stub 上原地重写
                            # video.m3u8（ffmpeg 开写即截断成 1 段 →
                            # AVPlayer 中场播放失败）。
                            _pack_full_swap(src, out_dir, maps, texts, item_id)
                            if not texts:
                                self._spawn_online_subs(src, item_id, out_dir)
                        except Exception:
                            traceback.print_exc()
            finally:
                with _FULL_PACK_GUARD:
                    _FULL_PACK_INFLIGHT.discard(item_id)

        # The caller holds _pack_lock_for(item_id) while we are spawned, and
        # the warm segments it just wrote are being served to the TV right now.
        # The lock is not reentrant, so the worker blocks until the request
        # thread releases it, then repacks to full duration.
        threading.Thread(target=worker, daemon=True).start()

    def _spawn_online_subs(self, src: str, item_id: str, out_dir: Path) -> None:
        """后台在线补字幕：只在本地级颗粒无收时值得跑，绝不进 302 热路径。

        抓到的字幕落 _subs/ 缓存，并给已在播的包只换字幕轨；失败落负缓存，
        TTL 内不重试。同 item 去重 + 守护线程：serve 退出不留活口。
        """
        with _ONLINE_GUARD:
            if item_id in _ONLINE_INFLIGHT:
                return
            _ONLINE_INFLIGHT.add(item_id)

        def worker() -> None:
            try:
                receipt = _online_subs_body(self.directory, src, item_id, out_dir)
                print("[serve] 在线字幕 %s：%s" % (item_id, receipt), flush=True)
            finally:
                with _ONLINE_GUARD:
                    _ONLINE_INFLIGHT.discard(item_id)

        threading.Thread(target=worker, daemon=True,
                         name=f"dvpack-subonline-{item_id}").start()


def _subtitle_texts(src: str, *, root: str | Path | None = None,
                    info=None) -> SubtitleResolution:
    """本地级字幕解析：L1 Jellyfin 文本轨 → L2 _subs/ 缓存。

    在线腿（subfetch 公网抓取）绝不在这里跑——分钟级，AVPlayer 等 302 超时
    就是「点片没反应 / 播放失败」。在线由后台 worker（_spawn_online_subs）
    触发，抓完只给 master 换字幕轨。ffprobe / 字幕 HTTP 任何一步失败都静默
    降级成无字幕：字幕是增强项，不是播放前提。

    root 给 serve 根目录时才启用 L2 缓存；info 可复用调用方已探过的 SourceInfo，
    省一次 ffprobe。
    """
    from .subsource import resolve_local_texts, subtitle_cache_dir

    try:
        from .relay import ffmpeg_visible_source
        from .probe import ffprobe_json, parse_probe_json
        fetch = ffmpeg_visible_source(src)
        if info is None:
            info = parse_probe_json(ffprobe_json(fetch), Path(src))
    except Exception:
        traceback.print_exc()
        return SubtitleResolution()
    cache_dir = subtitle_cache_dir(root, _src_fingerprint(src)) if root else None
    return resolve_local_texts(src, info, cache_dir=cache_dir)


def _jf_item_name(src: str, item_id: str) -> str:
    """在线腿的检索片名：Jellyfin /Items/<id> 的 Name。

    src 里只有 stream.mkv 这种 URL 尾名，拿去搜字幕必空；拿不到真片名就
    返回空串，在线腿整体跳过（fetch_online_texts 见空名即退）。
    """
    try:
        from .jfmeta import item_name, token_from_src
        parts = urlsplit(src)
        token = token_from_src(src)
        if not token:
            return ""
        return item_name(f"{parts.scheme}://{parts.netloc}", token, item_id)
    except Exception:
        traceback.print_exc()
        return ""


def _online_subs_body(directory, src: str, item_id: str, out_dir: Path,
                      name: str = "") -> str:
    """在线字幕腿同步体（serve 后台线程 / prewarm 批处理调用）：抓 → 落缓存 → 只换字幕轨。

    回执一句话给 serve 日志。全程兜异常：在线腿炸了也不能影响播放。
    步骤与 subsource 分级一一对应，换序先读 subsource docstring。

    name 由调用方直传真片名（prewarm 枚举时就有真名）：Jellyfin 任何一次
    新登录都会把旧 token 的 /Items 权限踢掉（2026-10-18 实测），靠
    _jf_item_name 从 src 反查只会 401「缺片名」空转——调用方有名就别再
    麻烦 /Items。serve 后台线程没有现成名，留空走反查。
    """
    from . import subs, subsource

    if not subsource.online_enabled():
        return "在线腿已关（%s=0）" % subsource.ONLINE_ENV
    cache_dir = subsource.subtitle_cache_dir(directory, _src_fingerprint(src))
    if subsource.cache_blocked(cache_dir):
        return "负缓存期内，跳过"
    if not name:
        name = _jf_item_name(src, item_id)
    texts, note = subsource.fetch_online_texts(
        src=src, item_id=item_id, name=name, out_dir=out_dir)
    if not texts:
        subsource.record_online_failure(cache_dir, note)
        return "未拿到：%s" % note
    subsource.save_cached_texts(cache_dir, texts, subsource.ONLINE)
    # 只换字幕轨必须进 item 锁：_pack_full_swap 在同一把锁下整体换装，
    # 并发时升级会写进即将被抛弃的旧 inode（RENAME_SWAP 换的是目录本身）。
    # master 不存在（正在打临时目录）也在这里变成一句失败回执——缓存已落，
    # 下次续期/重打包自然带上字幕。
    try:
        with _pack_lock_for(item_id):
            tracks = subs.upgrade_master_subtitles(out_dir, texts)
    except Exception:
        traceback.print_exc()
        return "已入缓存，master 升级失败（%s）" % note
    if not tracks:
        return "已入缓存，master 已有字幕轨（%s）" % note
    return "轨 %s（%s）" % ("+".join(t.language for t in tracks), note)


def _parallel_full(src, out_dir, maps, subtitle_texts=None):
    """全片打包走并行分块：源拉取是瓶颈（单进程只吃 ~5% CPU），
    4 路同时拉把墙钟压到约 1/4。失败时调用方退回单进程。"""
    # 2026-10-04 实测判决：并行分块在 chunk 接缝处音频 tfdt 链真实断裂
    # （纽伦堡 _real120 短测报 off -0.391s，比好包的 ±0.025s 差一个数量级），
    # 而单进程 pack_source 全程无 -ss、音画同源起步，8 分钟出 1436 段全片，
    # 逐段实测 a-v 只 0.000~0.084s。既然单进程又快又对，默认不再并行，
    # 免得每次点片先白烧 20G 磁盘和十几分钟再回退。要恢复并行先修
    # parpack 的 audio tfdt 接缝偏移，再设 DVPACK_PARALLEL=1 放行。
    if os.environ.get("DVPACK_PARALLEL") != "1":
        return False
    try:
        from .parpack import parallel_pack, ParallelPackError
        from .relay import ffmpeg_visible_source
        from .probe import ffprobe_json, parse_probe_json
        from .remux import (VIDEO_AUDIO_MAPS, dv_signalling_bsf,
                            source_full_range, audio_pack_mode)
        from .jfpack import _source_colorimetry
        from pathlib import Path as _P
        fetch = ffmpeg_visible_source(src)
        info = parse_probe_json(ffprobe_json(fetch), _P(src))
        # 与 jfpack.pack_source 同一道闸：master 的 CODECS 必须和实际
        # init/media 逐字一致。不认识的音轨（dts 等）转码 aac——既不是塞
        # 原轨也不是静音。declared/transcode 一起透传给 parallel_pack。
        maps, declared_audio, transcode_audio = audio_pack_mode(info, maps)
        # source=info 让 parpack 复用 playlist.py 的权威 master 构建器，全片
        # 包的 master.m3u8 与秒开包逐字一致（CODECS=hvc1 + VIDEO-RANGE +
        # SUPPLEMENTAL-CODECS）。缺了它 parpack 会退回自己手搓 master。
        parallel_pack(fetch, out_dir, seconds=0.0, segment_seconds=6.0,
                      workers=4, maps=maps,
                      colorimetry=_source_colorimetry(info)
                          or ("bt2020", "smpte2084", "bt2020nc"),
                      full_range=source_full_range(info),
                      bsf=dv_signalling_bsf(info),
                      setts="setts=pts=PTS-STARTPTS:dts=DTS-STARTDTS",
                     audio_codec=declared_audio,
                     transcode_audio=transcode_audio,
                      source=info,
                      subtitle_texts=subtitle_texts)
        return True
    except ParallelPackError:
        return False
    except Exception:
        traceback.print_exc()
        return False


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
        pass

    IP_ADAPTER_INFO._fields_ = (
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


def _raise_fd_limit() -> None:
    """把 RLIMIT_NOFILE 软限抬到硬限。

    serve 常被 nohup/setsid 脱到 launchd 名下，继承到的软限只有 256。
    结合 HTTP/1.1 keep-alive 的连接漏水，百来个带宽就会 EMFILE。
    """
    if resource is None:
        return
    try:
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    except (ValueError, OSError):
        return
    target = hard if hard != resource.RLIM_INFINITY else max(soft, 8192)
    if target <= soft:
        return
    try:
        resource.setrlimit(resource.RLIMIT_NOFILE, (target, hard))
    except (ValueError, OSError):
        pass


def serve(root: str | Path, port: int = 8765, *, host: str = "0.0.0.0") -> ThreadingHTTPServer:
    _raise_fd_limit()
    root = Path(root).resolve()
    if not root.is_dir():
        raise FileNotFoundError(f"目录不存在：{root}")
    httpd = ThreadingHTTPServer(
        (host, port), partial(HlsRequestHandler, directory=str(root))
    )
    return httpd

# Infuse 静态学习笔记：架构对照与 dvpack 优化建议

> 方法：对 `/Applications/Infuse.app/Contents/MacOS/Infuse`（arm64 slice，81MB，未 FairPlay 加密，
> SHA-256 ab2cc61a…44356）做静态符号/字符串提取。产出限定为「学习笔记 + 行为理解」，
> 未将任何反编译代码搬进 dvpack。
>
> 工具：`rea` 1.0.0（report 在 /tmp/infuse-rea/re-Infuse-arm64.md）、`strings`、`nm`、路径树提取。

## 0. 结论先行

Infuse 是一个 **ffmpeg 全家桶 + 自研播放内核 + 多协议网络层** 的成熟播放器。
对 dvpack 最有价值的三点，按收益排序：

1. **InputStream 分层装饰器链**（ReadAhead → Cache → RangeAccess → SpeedMeter）——
   dvpack 的 serve/prewarm 目前是「整段缓存 + 全片重打」，可以学它的按需分层。
2. **Demuxer 策略化 + 环形/分块双 PacketStorage**——dvpack 目前 remux 完即弃，
   没有「边下边解」的概念；这对 HLS 直播/长片内存占用直接相关。
3. **DV 信令按「配置记录」单独建模**（`DolbyVisionConfiguration` + `AVDOVIMetadata`）——
   dvpack 的 bmff/p5 已经做得比较细，但缺一个统一的「信令来源」抽象。

## 1. 二进制画像（Observed）

| 项 | 值 |
|---|---|
| 架构 | Mach-O universal (x86_64 + arm64)，arm64 slice 81,339,088 B |
| 入口 | 0x2002a30dc，image base 0x100000000 |
| 加密 | 无（otool 无 LC_ENCRYPTION_INFO；有 _MASReceipt 但 cryptid=0） |
| 熵 | 6.56 bits/byte（非加壳） |
| 函数 | 3,184 个 / 120,610 指令 |
| 链接库 | 162 个，其中第三方 61 个 framework |

内嵌第三方库（对 dvpack 有参考意义）：

- **媒体栈**：libavcodec / libavformat / libavfilter / libavutil / libswscale / libswresample
  / libavdevice、libdav1d、libbluray、libnfs、libudfread
- **网络**：SocketRocket（WebSocket，Plex/Emby 实时通知）、libcurl 系自研封装
- **协议**：SMB（StratospherixLibSMB，商业 SDK）、NFS、WebDAV、FTP、UPnP
- **加密/杂项**：CryptoPP（符号 `__ZN8CryptoPP…` 大量存在，用于 MEGA/网盘鉴权）、
  MEGA SDK（`__ZN4mega…`）、nanopb（Protobuf 编解码，Firebase）、OpenCC（简繁转换）
- **字幕**：libass（构建路径 `/Users/tsyganov/Firecore/Libraries/Static/libass`）、
  自研 SSA/SRT/SUB/VobSub 解析器

注意 rea 报告里「0 imported functions / no capability-relevant imports」是 Mach-O
弱符号表解析的局限，不是事实；真实能力要看 framework 列表与 ObjC/Swift 符号。

## 2. 架构对照（Infuse ↔ dvpack）

### 2.1 Demuxing 层

Infuse（源码路径 `Classes/CustomPlayer/Demuxing/`）：

```
Demuxers/
  Factory/DemuxerFactory.swift          # 按容器类型选 demuxer
  Swift/Components/Core/DemuxerContext.swift
  Swift/Components/LifecycleStrategy/
      DemuxerLifecycleStrategy.Default.swift
      DemuxerLifecycleStrategy.HLS.swift   # HLS 专用生命周期
  Swift/Components/PacketStorage/
      PacketStorageStrategy.Chunks.swift
      PacketStorageStrategy.RingBuffer.swift
  Base/PacketBuffers/
      FCPacketsBuffer.m / FCPacketBuffersCollection.m / FCPacketsBufferFactory.m
DemuxingStreams/
  Base/FCBaseDemuxingStream.m
  Buffered/FCBufferedDemuxingStream.m
  ffmpeg/FCFFMPEGDemuxingStream.m
  ffmpeg/FCFFMPEGDemuxingStream+StreamCorrectionAndValidation.m
HLS/
  DemuxerComponents/DemuxerLifecycleStrategy.HLS.swift
  DemuxingStream/HLSDemuxingStream.swift
  DemuxingStream/MediaFile/HLSMediaFileFactory.Plex.swift
  PlaylistHandler/HLSPlaylistRequestFactory.{Plex,Emby}.swift
PacketsProcessing/Video/
  KeyframeDetector/FCVideoKeyframeDetectorPacketProcessor.m
  PTS Fixer/{FCPTSFixingPacketsProcessor,FCPTSIncorrectnessDetector,FCPivotingPTSFixer}.m
```

关键设计：**LifecycleStrategy 与 PacketStorage 都是可替换策略**，HLS 和本地文件走不同
生命周期；包缓冲有 Chunks 和 RingBuffer 两种实现。PTS 修复被拆成「检测 + 两种修复
策略（线性/pivoting）」，并且是 packet processor 形式挂在流水线上。

dvpack 现状：`remux.py` 直接调 ffmpeg 出文件，`bmff.py` 事后解析盒链。
没有「demuxer 抽象」，也没有 packet 级 processor 链。

**可借鉴（按可行性排序）**：

- dvpack 的 `bmff.py` 已有盒树遍历，可以把它升级成一个轻量 `Demuxer` 接口
  （`read_box()` / `iter_samples()` / `find_boxes()`），让 p5/bmff/parpack 共用同一份
  遍历实现，而不是各自 `find_all`。
- PTS 检测逻辑（`FCPTSIncorrectnessDetector`）值得抄思路：dvpack 现在靠 ffprobe 复读
  判对错，可以加一个「trun/tfdt 时间戳单调性 + 首帧 PTS==0」的静态预检，在 ffmpeg
  开跑前就拒掉坏源，省一次全片 remux。

### 2.2 DV / HDR 信令

Infuse：

```
Classes/CustomPlayer/Demuxing/StreamDescriptors/Video/DolbyVisionConfiguration.swift
-[FCHEVCDecodingEngine createDolbyVisionDescWithHevcDesc:dolbyVisionConf:]
-[FCMetalShaderCache doviSourceWithMeta:]
@28@0:8r^{AVDOVIDecoderConfigurationRecord=CCCCCCCCC}16i24
@24@0:8^{AVDOVIMetadata=QQQQQi}16
DOVIPresent / DOVIProfile / DOVIVersion
DoviProfile02 … DoviProfile92（完整 profile 枚举）
FCDolbyStreamParser / FCDolbyVisionMetadata
GENERATE_DOVI_RESHAPE
ReindexPlexDoViMeta
MigrationStageDolbyVisionMetadata / MigrationStageDolbyVisionSetting
```

关键设计：DV 被拆成两个正交结构——**DecoderConfigurationRecord**（静态，init 段里的
dvcC/dvvC）和 **AVDOVIMetadata**（动态，per-frame RPU），分别由
`DolbyVisionConfiguration` 和 `FCDolbyStreamParser` 承载。渲染侧
`doviSourceWithMeta:` 把两者合起来交给 Metal shader。

dvpack 现状：`bmff.py` 查 dvvC 存在性与 RPU NAL 数量，`p5.py` 做 P8→P5 转签，
`probe.py` 走 ffprobe。分工已经接近 Infuse 的「配置记录 / 动态元数据」切分，但
**没有把 profile 枚举集中定义**——`p5.py` 里是散落的魔数（`01 00 0a 35` 等）。

**可借鉴**：

- 建一个 `dvpack/dvprofile.py`，集中定义 DV profile → (level, el_present, compat_id,
  兼容 brand, VIDEO-RANGE) 的映射表，让 p5/bmff/playlist 都从表里取，消除三处
  各写一份的风险。现在 playlist.py 已有 `_SUPPLEMENTAL_BY_RANGE` 这种局部表，
  正好可以并进去。
- `GENERATE_DOVI_RESHAPE` 这个字符串提示 Infuse 会在缺失 reshape 时合成——
  dvpack 的 HANDOFF 里提过 dovi_tool `-m 5` 重写 RPU 的方案，这是一致的思路，
  可作为「single_fmp4 仍不亮」时的下一步实验。

### 2.3 网络输入流（最值得学的一层）

Infuse（`Classes/Network/`）：

```
Common/InputStreams/
  Factory/FCInputStreamFactory.m
  Wrappers/BufferingStream/{FCBufferingInputStream,FCPositioningDataBuffer}.m
  Wrappers/CachingStream/Cache/InputStreamCache.{swift,Disk.swift,Disk.IO.swift}
  Wrappers/CachingStream/InputStream/ReadAheadLoader.swift
  Wrappers/CachingStream/InputStreamCacheManager.swift
  Wrappers/RetryingStream/FCRetryingInputStream.m
  Wrappers/SequentialSharing/SequentialSharingInputStream.swift
  Wrappers/SpeedMeter/FCInputStreamSpeedMeter.m
  Wrappers/strm/STRMInputStream.swift
NetworkShares/Curl/Stream/HTTPStream/
  HTTPInputStream.swift
  Internal/{HTTPStreamConnection,HTTPStreamConnectionPool,HTTPStreamOpener}.swift
  Transport/FCCurlHTTPRequest.m
  Config/{FCConnectionHTTPInputStreamConfig,FCURLHTTPInputStreamConfig,FCHTTPHeadersHandler}.m
  Utils/{FCCurlLogger,FCCurlStreamsCounter}.m
Symbols:
  _TtC6infuse16InputStreamCache / 20ReadAheadInputStream / 24ReadOnlyCacheInputStream
  _TtC6infuse27InputStreamCacheRangeAccess / 28SequentialSharingInputStream
  _TtC6infuse23InputStreamCacheManager / 26InputStreamSpeedStatistics
  _TtC6infuse24HTTPStreamConnectionPool / 22CurlHTTPStreamProfiler
  _TtC6infuse16InputStreamStats / 19DemuxingStreamStats / 11StreamClock
```

关键设计（这就是装饰器模式的教科书用法）：一条真实的 HTTP 流被一层层包起来
`curl transport → HTTPStreamOpener → ReadAhead → DiskCache → RangeAccess →
Retrying → SpeedMeter`，每层只做一件事，且对上层透明。**RangeAccess 把随机读请求
映射到已缓存区间**，多个消费者（视频轨 + 字幕轨 + 缩略图）通过
`SequentialSharingInputStream` 共享同一下载。连接池化避免每次 seek 重连 TLS。

dvpack 现状：

- `serve.py`：ThreadingHTTPServer + 单区间 Range，m3u8 与 mp4 不同缓存策略，
  注释里已经踩过 MIME/Range 的坑。
- `relay.py`：回环中继绕 TCC，`/usr/bin/curl` 固定路径，3xx 自己续跳。
- `prewarm.py`：批量预热，并发默认 1、硬顶 2（2026-10-04 压垮 Jellyfin 的教训）。

**可借鉴（这份笔记里最具体的一条）**：

dvpack 的预热是「整片打进 serve 缓存」。Infuse 的做法是「RangeAccess + 只读缓存流」。
对 dvpack 的 `/jf/pack` 热路径可以做：

1. 给 ffmpeg 的输入套一个 **range-aware 缓存层**：先按 `-ss` 起始位置只预取前 N MB，
   ffmpeg 边解边补，而不是等 relay 把整个 body 拉完。这直接改善 HANDOFF 里
   「点片没反应」之外的「起播前等全片」问题。
2. 多消费者共享：现在音频轨、字幕轨、视频轨各自从 Jellyfin 抽一次。
   Infuse 用 `SequentialSharingInputStream` 让它们共享同一下载流。dvpack 的
   remux 已经是一次性多 map（`VIDEO_AUDIO_MAPS`），这点其实已经比 Infuse 好，
   但 `prewarm` 的字幕在线抓取（`serve._online_subs_body`）还是独立 HTTP，
   可以并入同一次抽流。
3. `FCInputStreamSpeedMeter` / `InputStreamStats`：dvpack 的 serve 已有统计，
   但可以补一个「单连接速率 → 自适应并发/预取窗口」的反馈，替代现在写死的并发 1/2。

### 2.4 字幕

Infuse：

```
Classes/CustomPlayer/Subtitles/
  Decoding/TextDecoder/FCTextSubtitlesDecoder.m
  Decoding/assDecoder/FCASSSubtitlesDecoder.m (+LogsParsing)
  Decoding/ffmpegDecoder/FCFFMPEGSubtitlesDecoder.m
  Demuxing/ExternalStreams/{SRT/FCsrtLikeDemuxingStream.m, TextStreamReader}
  Demuxing/Parsers/{SSA/FCSSAParser+Sections.m, SUB/FCSUBParser.m, Legacy/FCSUBTaggedTextParser.m}
  Rendering/Metal/{FCMetalSubtitleRenderer, DrawUnitBuffer/FCSubtitleUnitsDrawBuffer}.m
Classes/Services/Subtitles/
  OpenSubtitles/Requests/{Search,Download,Languages}/…
  AutoDownload/FCSubAutoDownloadService.m
  Cache/{FCSubtitlesCache, DAO/FCSubtitlesDao}.m
infuse_ios_mac/Classes/Players/FCMPPlayer/HLS/FCHLSSubtitlesTracksManager.m
```

dvpack 现状：`subs.py`（SUBTITLES_GROUP 常量 + SubtitleTrack）、`subsource.py`、
`subwarm.py`、`jfmeta.classify_subtitles`。

**可借鉴**：Infuse 把「字幕轨解析」和「字幕服务（OpenSubtitles 搜索/下载/自动下载）」
彻底分层，且 **Demuxing 层的字幕走 ExternalStreams**（不混进媒体轨）。
dvpack 的 subsource 已有在线/本地分级，但可以更明确地划出
「字幕轨信令（group/语言/default）」与「字幕内容获取」两层，让 subwarm 只依赖前者。

## 3. 对 dvpack 的具体优化建议（按 ROI 排序）

| # | 建议 | 落点 | 依据 | 风险 |
|---|---|---|---|---|
| 1 | 建 `dvprofile.py` 集中 DV profile/level/brand/RANGE 映射 | p5.py, bmff.py, playlist.py | Infuse 的 DoviProfile02..92 全枚举 + `DolbyVisionConfiguration` | 低，纯重构 | **已落地**：[dvprofile.py](../dvpack/dvprofile.py)；bmff/p5/playlist/probe 已迁移，行为不变 |
| 2 | ffmpeg 前加「盒链 + PTS 静态预检」，坏源提前拒 | remux.py 入口 | Infuse `FCFFMPEGDemuxingStream+StreamCorrectionAndValidation` / `FCPTSIncorrectnessDetector` | 低 | **已落地**：remux.validate_segments()；tfdt 片间单调 + duration>0，真实产物与 Apple 参考分片通过，篡改 tfdt 被拒 |
| 3 | relay 加 range-aware 预取窗口，替代整 body 缓冲 | relay.py + serve.py | Infuse `ReadAheadLoader` + `InputStreamCacheRangeAccess` | 中，要重测 TCC/seek | **已落地（Range 缓存）**：relay 内置 per-URL 32MB LRU 区间缓存，重复 Range 不再打 Jellyfin；截断响应绝不进缓存 |
| 4 | `/jf/pack` 多轨共享同一下载流 | prewarm.py / serve._online_subs_body | Infuse `SequentialSharingInputStream` | 中 | **进程内已落地**：probe/pack/poster/字幕的重复 Range 共享同一份缓存 |
| 5 | 速率统计 → 自适应预取窗口/并发 | serve.py 统计 | Infuse `FCInputStreamSpeedMeter` / `InputStreamStats` | 中 |
| 6 | DV 动态元数据（RPU）单独建模，为 dovi_tool `-m 5` 铺路 | 新增 `dvmeta.py` | Infuse `FCDolbyStreamParser` / `AVDOVIMetadata` / `GENERATE_DOVI_RESHAPE` | 高，待真机 |

第 1、2 条是纯静态、可离线验证的重构，建议先做；3、4 需要重跑 HANDOFF 里的
真机矩阵；6 依赖 single_fmp4 是否亮 DV 的结论。

## 4. 复现命令

```bash
# 报告
rea /Applications/Infuse.app/Contents/MacOS/Infuse   # 需可写 work_dir，或用 MCP decompile_and_analyze

# 本笔记用的提取
lipo -thin arm64 /Applications/Infuse.app/Contents/MacOS/Infuse -o /tmp/infuse-rea/Infuse-arm64
strings -n 5 /tmp/infuse-rea/Infuse-arm64 > /tmp/infuse-rea/all.txt
rg -o "/Users/mercdev/jenkins/workspace/FireCore/Infuse/Infuse/[^\"']*" /tmp/infuse-rea/all.txt \
  | sed 's/.*Infuse\/Infuse\///' | sort -u > /tmp/infuse-rea/paths.txt   # 880 个源文件路径
```

## 5. 边界

本笔记只做架构与设计模式层面的学习。未破解授权机制、未复制 Infuse 代码进 dvpack、
未分析其 DRM/付费逻辑。dvpack 的模块集成仍限于 ffmpeg / dovi_tool / Bento4 等开源组件。


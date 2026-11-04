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


---

# 第二轮（2026-10-30）：补挖 Services / GUI / 缓存层——对标 dvplay 的已知病灶

> 同一 arm64 slice（SHA-256 e1f6dd3b…50d，78MB）重抽 strings 后补的定向提取。
> 上一轮只覆盖 Demuxing / DV 信令 / 网络流 / 字幕四层；本轮补 `Classes/Services`
> （263 文件）、`Classes/GUI/Controllers`（109 文件）与缩略图缓存体系。
> 863 个源文件路径已提取至 `/tmp/infuse-rea/paths.txt`（/tmp 会被系统清，下轮需重抽）。

## 6. 缩略图三级缓存（对标 dvplay「海报有些没显示」）

Infuse 的命名面（二进制符号实见）：

```
ThumbnailsMemoryCache / ThumbnailsSqliteDiskCacheDAOObjc / ThumbnailsLinksCache
ThumbnailsReferencesCache / ThumbnailsCachePolicy / CachePolicyForRequest
ThumbnailsCacheHousekeepingModel / HousekeepingService(Default|Stage|Request|InContainer)
DiskCacheP / DiskCacheDAOObjc / DiskCacheDisabled / DiskCacheTitle
ThumbnailLoader(+Factory|Type|Result) / ThumbnailProcessor(+Factory|ScaleMode)
ThumbnailOperation(+Factory|WithType) / ThumbnailKey / ThumbnailLinkID / ThumbnailPathWithItemID
ThumbnailIsLoading(+WithItem) / ThumbnailBlurRadius / ThumbnailHeightConstraint / ThumbnailOfSize
```

可借鉴的三点：

1. **Key 与 Link 分离**：`ThumbnailKey`（item→key）、`ThumbnailLinkID`（指向 URL 的稳定
   标识）、`ThumbnailPathWithItemID`（磁盘路径由 itemID 决定）。dvplay 现在海报没显示
   的排查难点正在于「是没拿到 URL、还是 URL 拿到了图没下到、还是下到了没渲染」——
   三级 Key/Link/Path 一分开，这三个阶段各自可观测、可分别缓存。
2. **HousekeepingService 常驻清理**：`HousekeepingServiceDefaultStage` 按 stage 推进，
   配 `CachePolicyForRequest` 逐请求定策略（是否允许回源、是否进磁盘、TTL）。
   dvplay 的 serve 磁盘闸是全局 LRU，海报这种小文件和大分片挤同一个池。
3. **IsLoading 单独一个符号**：`ThumbnailIsLoading(+WithItem)` 说明「加载中」是一等
   状态，由 loader 持有而非 view 自己记。dvplay 海报墙在焦点快速移动时反复触发同一张
   图的请求，缺的就是这个去重态——这也与上一轮 `SequentialSharingInputStream` 同源。

## 7. 仓库分层与分页（对标 dvplay「片库读取失败」「点击海报播放失败」）

```
ItemsControllers/Common/Repository/IVCItemsRepository.m
                                /IVCItemsRepositorySet.m
                                /Loading/Collections/FCCollectionsLoader.swift
                                /MediaItems/FoldersFetching/IVCMediaItemsRepositoryFetchingFactory.swift
Symbols: RepositoryID / RepositoryError / RepositoryResolverSubscription /
        ItemsRepositoryFetchingFactory / ItemsRepositoryDisplayFlags / ItemsRepositoryContext
Enumerators/FCBatchLoadingEnumerator.m
Symbols: PaginationRequest / PaginationResponse / PaginationError / PageSize(×15)
```

可借鉴：

1. **Repository 是显式的一等抽象**，带自己的 `RepositoryID` 与 `RepositoryError`，
   视图层只面对 Repository 而不认识 Plex/Emby/Jellyfin。dvplay 现在直连 dvpack serve
   的 JSON，服务端一抖动就是「片库读取失败」这种无差别错误；分出 Repository 层才能
   把「网络错 / 解析错 / 空库」区分成可展示的不同文案。
2. **`ItemsRepositorySet` + `RepositoryResolverSubscription`**：多个数据源（本地 +
   Plex + 云盘）合成一个集合，用订阅而非回调广播变化。dvpack 侧 `jfmeta.py` 已经
   在聚合 Jellyfin 元数据，缺的是把「集合 + 订阅」的契约显式化。
3. **`PaginationRequest/PaginationResponse` + `FCBatchLoadingEnumerator`**：分批加载
   是一等公民。dvplay 海报墙一次拉全库，片一多就「有些无法点击播放」；分批加载天然
   把「加载中/失败/可交互」限定在当前页。
4. `DisplayFlags` / `Context` 说明视图状态（显示什么、在哪个上下文）与数据是分开传的。

## 8. 索引健壮性（对标 dvplay「片库读取失败」「闪退」）

```
Indexing/Core/Indexer/CrashProtection/FCIndexingCountersManager.m
Indexing/Core/Indexer/IndexingState/FCShareIndexingState.m
                      /StateManager/ShareIndexingStateManager.Default.swift
Indexing/Core/Utils/DirectoryWatcher.swift
Indexing/Core/FolderIndexer/FCFolderIndexer.m
Indexing/MetaDiff/{DefaultIncrementalMetadataRefreshService,MetaDiffFetcher}.swift
Indexing/Crawling/Tree/{TreeCrawlingService,Filters/Index/CrawledItemsTreeFilter}.swift
                     Strategies/{FolderCrawlingStrategy,RecursiveCrawlingStrategy}.swift
```

可借鉴：

1. **CrashProtection + 计数器**：索引崩溃被单独一个 Manager 计数、记状态。dvplay 在
   海报墙移动焦点时闪退，且是偶发；Infuse 的思路是把易崩流程（索引/抓取）与主 UI
   进程隔离并计数重试，而不是让一次异常冒到 UI 层。
2. **IndexingState 是按 share 的状态机**（`FCShareIndexingState` +
   `ShareIndexingStateManager`），每个共享源独立状态。dvpack 的多源（Jellyfin/SMB/
   本地）目前没有按源的状态记录，一个源卡住会影响整体观感。
3. **DirectoryWatcher + 增量刷新**：`MetaDiffFetcher` 只对变化的元数据做增量刷新，
   而不是全量重扫。dvplay 每次进片库全量拉，海报多了就慢；增量是明确的优化方向。
4. **TreeCrawlingService + 两种 CrawlingStrategy（Folder/Recursive）**：策略可换。

## 9. 排序策略化（对标 dvplay「优化片子分类」）

```
Symbols: SortingStrategyFactory / SortingBaseStrategy / SortingStrategyP
         SortingByTitleStrategy / SortingByTitleAndPersonsComeLastStrategy
         SortingByReleaseDateStrategy / SortingByRecentlyAdded
         SortingByItemIDStrategy / SortingByFileNameStrategy / SortingByDateStrategy
         SortingMode(ByTitle|ByReleaseDate|ByFileName) / SortingModeForCollections
         SortingResult / SortingStrategyEpisodesMode
         GroupingInfo / GroupingOptions / OrderingInfo / OrderingParams
```

可借鉴：排序是「工厂 + 策略基类 + 命名策略」的结构，且 **`SortingByTitleAndPersonsComeLast`
单独成一个策略**（演员/人物条目排在片名之后）——说明它把「排序」当成会被用户改、
会因集合类型（Collections 用 `SortingModeForCollections`）而不同的可替换行为。
dvplay 的分类现在是服务端固定顺序；把排序抽成策略，用户就能按「最近添加 / 发行年 /
   标题」切换，且不影响缓存 key。

## 10. 其他值得记的命名面

- **播放进度与收藏**：`PlaybackProgressDAO`、`WatchedStatusForMovieMetaID/EpisodeMetaID`、
  `ResumePlaybackMode`、`ResumePct`；`FavoritesManager`、`HomeOrderRouter`、
  `FavoritesProvider(+Delegate)`、`FavoriteLocation`。Infuse 把「看过没/看到哪」
  和「收藏在哪」都做成可同步插件（见 `iCloudSync/Model/Plugins/Favorites`）。
- **iCloudSync 是插件化同步**：`Model/Plugins/{Favorites,Links,VFS,PlaybackSettings,
  UpNextAndProgress,MetadataCache}` 各一个 Plugin，`FCSyncModelTrigger` 触发，
  `ConflictResolution/SyncGroupDuplicatesFilterStrategy` 处理冲突。dvplay 若要跨设备
  共享「看到哪」，这是现成的分层范式（本地 DAO + 同步插件 + 冲突策略）。
- **tvOS 专属代码极少**：路径里只有 `Controller_tvOS/`、`non-tvOS/`、`iOS+tvOS/`
  三类目录，证明它用同一套 UI 代码跑三端，靠目录约定隔离差异。dvplay 单攻 tvOS，
  这一点反过来提示：别为 tvOS 单独复制一份 UI 逻辑，差异要收敛到少量扩展点。
- **`ItemsControllers/Common/Actions/`** 把「删除确认 / 移动 / 菜单」拆成
  Handler + Operation 两层（`IVCDeletionHandler` / `IVCDeletionOperation`）。dvplay
  用户要的「ATV 里删了本地一起删」，正是 Handler（交互）+ Operation（真删）的划分。

## 11. 本轮结论（回答「还有什么可借鉴」）

按对 dvplay 当前病灶的针对性排序：

1. 缩略图三级缓存 + Key/Link/Path 分离 + IsLoading 去重（海报不显示、快速移动焦点重复请求）；
2. Repository 一等抽象 + 分批加载（片库读取失败、海报点不动、错误文案不可区分）；
3. 索引 CrashProtection 计数 + 按 share 状态机 + 增量刷新（片库读取失败、偶发闪退）；
4. 排序策略化（片子分类可切换）；
5. 删除 Handler/Operation 分层（ATV 删除联动本地）。

仍未做（承接上一轮遗留）：#5 速率统计→自适应预取、#6 RPU 动态元数据建模。

## 12. 落地核对（2026-10-30 第二轮，逐条比对 dvplay 现状）

本节把上面的建议与 dvplay 代码实际比对，避免「报了的其实早做了」。

**dvplay 已自行实现，比笔记记的更细（不必再做）**：

- 缩略图缓存：`PosterImage` 已有 `NSCache`（150 条 / 48MB 双上限）+
  降采样（`ThumbnailDecode`，300pt 上限按屏幕 scale）+ 邻位预取 +
  失败重试一次（0.6s 间隔，注释即写明治「海报有些没显示」）。
- 货架缓存：`JellyfinLibraryView` 用 `shelfSignature`（只含片源/搜索/筛选/
  排序，焦点不进签名）治焦点移动 324ms 重建；`@FocusState focusedItemId`
  显式给初始焦点，治「点片没反应」。
- 分批加载：`JellyfinClient.fetchItems` 已按 Limit 50 分页 +
  `startIndex > 1000` 保险丝。
- 删除分层：`IVCDeletionHandler/Operation` 对应物已有——`jfDelete` +
  `purgePackCache`（Jellyfin DELETE 后联动清 dvpack `/jf/purge`）。
- 播放进度/续播：`continueWatching(from:)` 货架 + `resumeProgress` 已实现。

**本轮新落地（第 7 条 Repository/错误分类的一部分）**：

- 新增 `dvplay/LibraryLoadState.swift`：`LibraryLoadState` +
  `LibraryLoadFailure`（unreachable / unauthorized / server / malformed /
  empty / cancelled 六类，各带中文文案 + `isTransient`）。
- `jfLoadItems` 改为按类型给文案（不再一律 `error.localizedDescription`）+
  瞬时失败自动重试三轮（0.8s / 1.6s 退避）+ 空库不算故障 + 取消不刷屏。
- 动机：原来 `Failure` 已分好类却在 catch 里被降级，网络闪断 / token 失效 /
  Jellyfin 500 在电视上同为一句「片库读取失败」，用户无从判断该重试还是重登。
- 验证：`xcodebuild generic/platform=tvOS` 编译通过；分类映射表用
  swift 脚本逐码验证（-1009/-1001/-1003/-1004/-1006→unreachable，
  -1013→unauthorized，401/403→unauthorized，5xx→server，-999→cancelled）。

**仍未做**：#5 速率统计→自适应预取、#6 RPU 动态元数据建模、
按 share 的索引状态机、DirectoryWatcher 增量刷新。

**核对后确认 dvplay 已实现、不再作为建议（2026-10-30）**：

- 排序策略化（第 9 节）：dvplay 已有 `SortMode`（名称/年份/体积）+ 顶部循环切换 UI，
  且 name 模式专门处理了剧集分组（seriesGroupKey + season/episode）与
  E01/E10 乱序。比 Infuse 的 `SortingByTitleStrategy` 更贴合中文片库，
  抽成策略类只会把已调好的逻辑打散，不做。
- 续播/收藏（第 10 节）：`continueWatching(from:)` + `resumeProgress` 已有。
- 删除分层（第 10 节）：`jfDelete` + `purgePackCache` 已覆盖
  Handler/Operation 的划分，且多做了 dvpack 侧缓存联动。

## 12. 结论：Infuse.app 保留，不删除（2026-10-30 用户问及「如果没有可学的就删」）

**判断：仍有两项不可替代的用途，建议保留。**

1. **它是 DV 徽标的唯一验收基准，且已写进 HANDOFF 的事实链。** HANDOFF.md 中 31 处
   提及，反复记载「徽标验收只认 Infuse」「只有 Infuse 这类自带真 DV 输出通路的 app
   才会点亮；通用 AVPlayer app 不触发」「已验证 Infuse 能点亮我们的流」
   （HANDOFF v21/v22）。删除它，dvpack 的打包产物就失去对照判据——dvplay 自研
   app 至今点不亮，没有 Infuse 就无法区分「包不对」还是「app 没走 DV 通路」。
2. **静态分析的原始产物在 /tmp，已被系统清过一轮**（`/tmp/infuse-rea/` 2026-10-18
   已不存在，本轮重抽）。二进制本体是这些产物唯一的再生产源，删了下次要重新
   从 308MB 里切 slice。

**成本**：308MB（MacOS 161M + Frameworks 102M + Resources 45M）。当前根卷剩 70GiB，
占比 0.4%，磁盘不构成删除理由。

**已无参考价值的部分**：付费/IAP/订阅逻辑（`Classes/Services/IAP`、GUI/IAPs）——
按既定边界从未分析、也不建议分析，与 dvpack 目标无关。

**结论**：可学的东西已沉淀为本笔记 11 节，二进制保留作验收基准。若将来 dvplay
自己能稳定点亮 DV，Infuse 的基准作用消失，届时可再评估删除。

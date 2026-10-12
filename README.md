# dvpack

Jellyfin 片库 → Dolby Vision HLS 打包 → tvOS 播放的一体化工具链。
解决 NUC 上的 Jellyfin 转码（QuickSync 转 4K·10bit·DV）丢 DV 层、徽标不亮、
颜色不对的问题：打包在 Mac 侧完成，只搬码流不重编码，电视拿到的是带
dvcC/RPU 的规范 P5 流。

## 组成

| 部件 | 位置 | 角色 |
| --- | --- | --- |
| `dvpack`（Python） | 本仓库 | HLS 打包 + 海报抽帧 + 局域网投喂服务 |
| `dvplay`（Swift/tvOS） | `../dvpack` 平级目录 | Apple TV 客户端：片库海报墙 + 播放 |
| Jellyfin | NUC `192.168.1.68:8096`（docker） | 片源与元数据，root/135790 |

Mac 侧 serve 跑在 `8899`（launchctl `com.dvpack.serve`，根目录
`_out/dv_p7_fix`），Apple TV（`192.168.1.100`）经海报墙点片起播。

## CLI

```
python -m dvpack check <原片.mkv>          # 只探测判定，不进打包
python -m dvpack init  <pack目录|init.mp4>  # dovi_tool info 口径诊断已落盘 pack
python -m dvpack pack  <原片.mkv> -o _out/demo --seconds 60
python -m dvpack serve _out/dv_p7_fix      # 起局域网服务（打印给 aTV 的地址）
```

Jellyfin 片库的实时链路由 serve 的 `/jf/pack`、`/jf/poster`、`/jf/purge`
端点驱动（点片 → 秒开 20s + 后台全片并行补），CLI 主要用于验收单个文件。

片库也可批量预热（`dvpack/prewarm.py`，v48）：枚举全库按 serve 现役缓存判据排
计划（命中/打包/跳过与原因），再按 serve 同款换装流程逐部补全片。src 指纹与
serve 判据逐字一致（`stream.<ext>?static=true`，ext 取 Jellyfin Container 探测
值），对不上就是白预热；`--jobs` 硬顶 2、磁盘预留 12GB，不动正在播的 stub。

```
python -m dvpack prewarm --base http://192.168.1.68:8096 --pw <密码> --dry-run
python -m dvpack prewarm --base http://192.168.1.68:8096 --pw <密码> --limit 2
```


字幕批量预热（`dvpack subwarm`，v58）：全库部提前抓中英双语进 L2 `_subs` 缓存——
serve 点片热路径永不跑在线腿，批量抓取也永远 1 路顺序，防公站 429。

```
python -m dvpack subwarm --base http://192.168.1.68:8096 --pw <密码> --dry-run
python -m dvpack subwarm --base http://192.168.1.68:8096 --pw <密码> --limit 5
python -m dvpack subwarm --base http://192.168.1.68:8096 --pw <密码> --force   # 刷新已抓缓存
```

## 字幕

字幕来源分级（2026-10-05 全库审计后定，工具 `dvpack/jfmeta.py`）：
① Jellyfin 内嵌文本轨——全库 70 部里 42 部现成（39 部中英齐），`_subtitle_texts`
直抓，**主力**；② 边车 `.srt/.vtt`；③ 在线下载 `tools/subfetch.py`；
全库 PGS-only = 0，不设 OCR 兜底（2026-10-05 起 OCR 已从链路拔除）。
字幕构成随时可审计：

```
python -m dvpack.jfmeta --base http://192.168.1.68:8096 --pw <密码>
# 总计 70 部：text 42（中英齐 39）/ pgs 0 / mixed 0 / none 28
```

在线下载（换网络环境用；本网出站被拦时全部不可达）：
`tools/subfetch.py` 与本地已有文本字幕（边车/先前抓取）择优，OpenSubtitles 走 moviehash
精确匹配
（算法与 subliminal 同口径，见 `tests/test_subfetch.py` 的焊死向量），
失败回落 SubHD / SubtitleCat。

候选排序口径（2026-10-05 v55 起，参照 subliminal `guess_matches` 的
year 匹配，三条腿共用一份判据）：片名覆盖率 `core_coverage` + 名称
相似度（封顶 0.2）+ 季集加权（同集 +0.2 / 错集 -0.3）+ 年份差惩罚
（差 >1 年 -0.6 / 差 1 年 -0.1 / 未知不罚）+ 语言标记（简英/双语
+0.15）。同片名重制片（狮子王 1994/2019）覆盖率同为 1.0，只有年份
把错版字幕压下去；日志会打 `年份 1994≠2019(-0.60)` 便于回溯。

OpenSubtitles 检索细节（v56/v57 修掉两条致命伤后）：探针 URL 必须带
`type=movie` 且 query 长度 >1——缺任一项服务端回 400「Not enough
parameters」「Query is too short」，无 data 字段，会被探针误判
成 key 失效关进 6 小时负缓存（v56 前整条 OS 腿就这样躺平的）。剧集名必须走
`type=tvshow` + `season_number`/`episode_number`：`type=movie` 下服务端把剧集库过滤光
（实测 True.Haunting.S01E01：movie 型 0 命中，tvshow 型 155 命中、带季集参数精筚后 2 条）。


serve 里的分级执行（2026-10-05 v47 起）：L1/L2 在点片 302 热路径同步跑，
失败静默降级无字幕；L3 在线抓取只由后台守护线程跑（分钟级，绝不阻塞
302——AVPlayer 等超时即「点片没反应」），抓到后只给 `master.m3u8` 换字幕
轨、视频分片零改动。开关与缓存：

- `DVPACK_SUBS_ONLINE=0`：整个在线腿关闭（默认开）
- `DVPACK_SUBS_RETRY_HOURS=6`：抓取失败后的负缓存 TTL，期内不重试
- `DVPACK_SUBS_ONLINE_TIMEOUT=240`：在线子进程墙上限（秒）
- 字幕缓存落在 serve 根的 `_subs/<src指纹>`（原子写，不带 api_key）
```

Api-Key 配置（v54 起集中在一条命令，不用每次 export）：key 落在
`~/.dvpack/config.json`（600 权限，同机其他用户读不到）。

```
python -m dvpack config --init                 # 写骨架，不覆盖已有 key
python -m dvpack config --os-key <key>          # 落 key（600 权限）
python -m dvpack config                         # 裸跑：看当前 key 与来源
```

路径可用 `DVPACK_CONFIG=/path/to/config.json` 覆盖（测试/多机分发用）。
解析顺序（serve / prewarm / subfetch 共用同一份，v51 收敛处）：
`--os-key` > `$OPENSUBTITLES_KEY` > 配置文件。

python tools/subfetch.py --item <jfItemId> --name <片名> \
    --jf-dir <pack目录> --langs zh,en --source auto
```

PGS-only 片（无文本层）与零字幕片一样走在线下载（③），OCR 兜底已于
2026-10-05 拔除（全库 PGS-only = 0，无人受益）。

## 测试

```
uv run pytest            # 或：uv run --project . pytest
```

依赖在 `pyproject.toml` 的 `dev` extra 里。离线可跑（网络腿全部 mock）。

## 参照的金标准项目

- **subliminal**（字幕）：OS moviehash 算法逐字节对齐；检索分级
  （hash 精确 → 片名兜底）同思路。
- **dovi_tool**（杜比视界）：`dvpack init` 复制其 `info` 诊断口径
  （sample entry / dvcC / 参数集 / RPU）。
- **m3u8**（globocom HLS 库）：master/subtitle 列表产物经独立解析器
  复读校验（`tests/test_playlist_m3u8_roundtrip.py`）。

## 目录

- `dvpack/` — 核心：`serve.py`（HTTP+端点）、`jfpack.py`（打包编排）、
  `bmff.py`（MP4 盒读写）、`dplus.py`（DV 信令）、`remux.py`（ffmpeg 编排）、
  `playlist.py`（HLS 列表）、`subs.py`（字幕轨）、`probe.py`（ffprobe）
- `tools/` — `subfetch.py`（字幕抓取）
- `tests/` — 套件：260+ 例，真机口径全部钉死在断言里
- `scripts/` — `dvassert.sh`、`p7_to_p5.py`、`matrix.py` 等验收脚本
- `HANDOFF.md` — 逐轮会话交接日志（v1→最新），排障历史与踩坑都在里面
- `docs/` — 早期交接存档

## 环境事实（2026-10-05）

- GitHub / PyPI 官网 / OpenSubtitles 本机不可达；清华 PyPI 源可达
  （`pyproject.toml` 已配 uv index）
- Mac 内网 IP 会漂移（曾 .109→.106），serve 地址以启动日志为准
- tvOS UDID：`00008110-0002149A0A9B801E`（devicectl）

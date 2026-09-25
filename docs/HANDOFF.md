# dvpack / dvplay 项目交接

> 本文件是仓库内的公开版本，已脱敏：不含本机绝对路径、不含片源库位置与片名、
> 不含凭据存放细节。未脱敏版本放在本地 `_scratch/`（不进仓库）。

## 1. 项目目标与成功判据

唯一验收判据：**Apple TV 上原生点亮 Dolby Vision 徽标**（tvOS 原生 DV 路径，
不是转 SDR/HDR10 的降级播放）。

架构是"方案 A"：
- Windows 侧打包（本仓库 `dvpack`）：MKV → CMAF/fMP4 HLS，保留 DV 信令
- tvOS 侧自写极简播放器（`dvplay`，**尚未开工**）：AVPlayer + 端侧诊断页
- 不做：转码、Dolby 授权链路、复用第三方播放器

DV 信令链（任何一环断了徽标就不亮）：

```
MKV DOVI configuration record
  -> dvh1 sample entry + DV config box（dvcC / dvvC）
  -> HLS CODECS="dvh1.PP.LL" + VIDEO-RANGE=PQ + HDCP-LEVEL=TYPE-1
  -> tvOS AVPlayer -> 电视徽标
```

## 2. Windows 侧已完成（实测，非计划）

跟踪集：18 个文件 / 2187 行；`python -m pytest -q` → **71 passed**。

| 模块 | 职责 |
|---|---|
| `dvpack/probe.py` | ffprobe → `DoviRecord` → Apple codec string + 可支持性判定 |
| `dvpack/bmff.py` | 解析 fMP4，校验 `dvh1`/`dvvC` 与 RPU 保真 |
| `dvpack/remux.py` | ffmpeg 封装调用 |
| `dvpack/playlist.py` | 主/媒体列表生成（刻意写 LF） |
| `dvpack/serve.py` | 局域网 HLS 静态服务 |
| `dvpack/tools.py` | `tools/` 下便携 ffmpeg + dovi_tool 的定位 |
| `scripts/spike_muxer.py` | muxer 选型实测 |
| `scripts/matrix.py` | 验收矩阵：扫片库 → 按 profile/level 出 8 个验收对象 |

## 3. 关键实测结论（踩出来的，重跑很贵）

**ffmpeg 封装行为**
- `-c copy` 保留 in-band RPU（能过）。
- 单加 `-tag:v dvh1` **不写** DV config box。
- config box 的写入被 `-strict unofficial` 门控；不加就没有 `dvcC`/`dvvC`。
- `-tag:v dvhe` 直接失败。
- **ffmpeg 写 HLS init segment 是相对 CWD，不是相对 playlist 目录**——这是个静默
  错位坑，`serve.py`/`remux.py` 里已按此处理。

**Apple HLS Authoring Spec 口径**
- `dvh1.<profile 两位>.<level 两位>`。
- Profile 8 才需要 `SUPPLEMENTAL-CODECS`（`db1p` = HDR10 兼容 / `db4h` = HLG）。
- 兼容 brand 与 `VIDEO-RANGE` 互相交叉校验。

**Profile 语义与 canary 选择**
- P5 单层 BT.2020/PQ，无回退 → 徽标不亮就是真不亮（好探针）。
- P8 HDR10 兼容 → 会静默降级（坏探针）。
- P7 双层需要先丢 EL。

**判定风险项的边界**（`probe.py` 里）
- Infuse 社区报告 Profile 5 在 level 7/9 不点亮 DV；本机未复现，所以只报 warning
  不做硬性拒绝，且**只限 P5**——P8 在 level 7/9 是流媒体片源常态，报它是噪音。
- 这条曾经误报，回归测试在
  `tests/test_probe.py::test_p8_level7_is_not_flagged_because_that_level_is_normal_for_p8`。

**片库分布**：五类，P5 L9 最多、P5 L6 次之，其次 P8 L6 / P8 L7 / P5 L7。最大组 L9
恰好落在 Infuse 用户报"不点亮"的位置，所以矩阵必须覆盖 P5 L6/L7/L9 + P8 L6/L7 +
非 DV 对照。

## 4. 仓库与发布状态

- GitHub：`cpufreestyle/dvpack`（私有，SSH 远端 `git@github.com:cpufreestyle/dvpack.git`）。
- 提交历史：`feat: dvpack——Windows 侧杜比视界 HLS 打包链路` +
  `chore: 锁定 LF 换行`，共 2 个。
- Release：`v0.1.0`，指向第二个提交（仅打包链路，不含本文档）。
- 本地另有一块物理盘上的裸仓库做离线副本。
- `.gitattributes` 用 `* text=auto eol=lf` 锁 LF：本机 `core.autocrlf=true` 会把
  `.py` 写成 CRLF，而同一份代码还要在 Mac 上跑端侧实验，锁 LF 让两端字节一致。

## 5. 认证边界（踩过很久，结论照抄即可）

- 建仓正道：GitHub 官方市场的 `GitHub` MCP 连接器（`https://api.githubcopilot.com/mcp/`，
  GitHub 侧授权）。授权后有 `create_repository`，本次就是用它建的私有空库
  （`private` 默认 true、`autoInit=false`）。这是"零令牌、零网页点击"的唯一路径。
- 新装的连接器**不会在当轮出现工具**，要等下一轮或重开会话。
- 推送用 SSH；不要用 MCP 的 `push_files`——那会在远端生成不同历史。
- **不要代填、不要索取登录凭据**。若必须走令牌：用户自己
  `Read-Host -AsSecureString` 写进文件，脚本运行时读，令牌不进任何脚本或文本。
  GitHub 用 classic PAT + `repo` 权限；fine-grained 不能建仓。
- 非 TTY 下 `gh auth login --web` 的设备码长轮询在这条链路必挂（实测两种死法），
  别再试。
- Gitee 判别法：匿名 `git -c credential.helper= ls-remote` 成功 ⇒ 公开；
  带凭据 `404` ⇒ 不存在；空仓库 `ls-remote` 是 exit 0 + 零输出。

## 6. 未做 / 下一步（按价值排序）

1. **`dvplay` tvOS 客户端**（零进度）。需要 Mac + Xcode；Windows 侧配套的是一个
   端侧诊断页（列出 `ext-x-APPLE-STRING`、`VIDEO-RANGE`、`HDCP-LEVEL`，用于比对
   徽标是否亮）。
2. **端侧验收矩阵实跑**：P5 L6 / P5 L7 / P5 L9 / P8 L6 / P8 L7 + 非 DV 对照，
   共 8 行，`scripts/matrix.py` 已能生成。
3. **`dvcC` vs `dvvC` 四字符接受度**是未验证的设备侧风险；应急方案是 4 字节 box
   name 改写，`bmff.py` 已能定位该 box。
4. **音频轨**：E-AC3 → fMP4 HLS 尚未接；Apple 对音频同样有 `CODECS` 口径要求。
5. **spec 文档**（MVP 设计）一直没写，要不要补由维护者定。
6. Gitee 侧：除非明确要，别再折腾。

## 7. 维护注意事项

- **公开前先清洗**：若干跟踪文件里有真实场景相关的路径串（片库目录名等）。
  私有库无碍，一旦转公开或推到新库（新库可能默认公开）就会暴露片库结构。
  发布前全局搜一遍本地媒体库根目录名，确认零命中。
- 片库本身是自建媒体库，不能删；所在盘常年高占用。
- 分页文件大小曾被手动固定，改回去之前别动系统设置。
- 自建媒体服务器的容器 GPU 从未真正接入，转码是 `none`（软件）；改配置要用合法枚举值。
- 终端回显中文易乱码（cp936）；要判断结果就让脚本只输出 ASCII，或写文件后查看。

## 8. 常用命令

```bash
python -m pytest -q                      # 71 passed
python scripts/matrix.py                 # 验收矩阵
python -m dvpack probe <file.mkv>        # ffprobe -> DoviRecord + codec string
python -m dvpack serve                   # 局域网 HLS 服务（Apple TV 上诊断页填这台机器 IP:端口）
git push origin master                   # 正常推送（在能运行 git 传输的环境里）

## 9. 本地片源配置（敏感信息一律走环境变量，不进仓库）

仓库里不含任何本机路径。本地跑验收/测试时按需设置：

| 变量 | 用途 | 默认 |
| --- | --- | --- |
| `DVPACK_LIBRARY` | 片库根目录，`scripts/matrix.py` 用它扫盘 | `~/Movies` |
| `DVPACK_SOURCE_MKV` | 真文件测试用的单个 MKV（`tests/`，`scripts/spike_muxer.py`） | 空（相关用例跳过） |
| `DVPACK_SOURCE_P8_L7_MKV` | `tests/test_probe.py` 的 P8 L7 样本 | 空（用例跳过） |
| `DVPACK_PAIRED` | 同系列只差 level 的一对，相对 `DVPACK_LIBRARY`，`os.pathsep` 分隔 | 空（跳过配对探针） |
```

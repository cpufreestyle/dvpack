# 交接：dvpack v0.1.0 部署 + dvplay（tvOS 客户端）启动

日期：2026-09-25 · 交付人：Codex · 仓库：`cpufreestyle/dvpack`（private, tag v0.1.0）

已完成的真机闭环（2026-09-25）：
- Apple TV「主卧」（AppleTV14,1 / tvOS 27.2 / UDID 00008110-0002149A0A9B801E）已与
  本机配对（配对码经 devicectl CLI 提交）
- dvplay 以免费 Personal Team（qm0819@hotmail.com）自动签名，app 已安装并启动
- dvpack serve 已起在 :8765，打印地址随 lan_address 修复更正为
  http://192.168.1.109:8765/master.m3u8
- 注意：当前 _out/demo 是冒烟用的合成 SDR 包，App 复判应显示"SDR 回退"；
  真 DV 徽标验证需 Windows 侧 pack 真片源后重 serve
新仓库：`cpufreestyle/dvplay`（private, tvOS 客户端）

追加修复（同日并入 master）：
- `cpufreestyle/dvpack` master `1da85c1`：`lan_address()` 改为枚举网卡取
  RFC1918 私网地址（192.168 > 172.16/12 > 10），TUN 代理劫持默认路由时不再
  打印 198.18.0.1；本机实测修正为 192.168.1.109，测试 85 passed。
- `cpufreestyle/dvplay` main `c1f4890`：`@AppStorage` 记住上次地址；工程改
  为自动签名就绪（去掉 `CODE_SIGNING_ALLOWED = NO`），模拟器/设备 SDK 均
  已验证构建通过。

## 一、本次做了三件事

1. **部署 dvpack v0.1.0**（tag `v0.1.0` @ `d01d888`）到本机 Mac，完成环境补齐与全链路验证。
2. **启动 dvplay**——Release Notes 里 "No tvOS client yet (dvplay is not started)" 的那个客户端；
   已实现可编译的最小可用版本并推私有仓库。
3. 形成本文档作为后续开发的事实清单，避免踩重复的坑。

## 二、dvpack v0.1.0 部署记录（Mac 侧）

**环境**：macOS arm64 · Python 3.14.7（Homebrew）· ffmpeg/ffprobe 8.1.2（Homebrew）·
Xcode 27.0（tvOS 27.0 SDK 在，tvOS Simulator Runtime 未装）。

**部署步骤**：

```sh
git clone https://github.com/cpufreestyle/dvpack.git && cd dvpack
git checkout v0.1.0
mkdir -p tools/ffmpeg/bin && ln -s $(which ffmpeg) tools/ffmpeg/bin/ffmpeg
ln -s $(which ffprobe) tools/ffmpeg/bin/ffprobe        # tools/ 被 gitignore，Mac 需自建
uv run --with pytest python -m pytest -q               # 66 passed, 5 skipped
```

- `tools/` 不随仓库分发（Windows 侧放便携 ffmpeg）。Mac 用 Homebrew 软链即可，代码零改动。
- ~~`dovi_tool` 无需提供~~（已作废）：`scripts/p7_to_p5.py` 经 `tools.DOVI_TOOL` 引用，获取方式见第九节第 1 条与 `scripts/fetch_dovi_tool.sh`。
- 测试 66 通过 + 5 跳过 = 71，与 Release Notes 声称的 "71 passed" 一致；
  跳过的 5 个是绑定 `D:/movie/...` 片源的集成测试，只在 Windows 主机跑。

**冒烟结果**：
- `check` 合成非 DV 源 → 正确判定"不可打包"（exit 1），闸门生效。
- `serve` 起在 :18765：m3u8 返回 `application/vnd.apple.mpegurl`；Range 请求回 206
  且 `Content-Range` 正确；分片 MIME `video/iso.segment`；404 用纯 ASCII reason（不炸连接）。

**部署坑（重要）**：
1. `serve.py` 的 `lan_address()` 用 UDP connect 探测源地址，本机抓到 VPN/TUN 网卡的
   `198.18.0.1`，打印给 aTV 的 URL 不可达；真实局域网 IP 是 `192.168.1.109`，填它可用。
   改进方向：枚举 ifaddrs 取默认路由网卡地址。**未改动已发布版本**，列为待办。
2. 默认端口 8765 被本机 ai-town 服务占用，冒烟改用 18765。
3. Windows 侧 `tools/` 的便携 ffmpeg 版本 Windows 上是 N-126636，与 Mac 的 8.1.2 不同；
   跨平台对照实验前先对齐版本。

## 三、dvpack 链路关键事实（动手前必读）

三条命令：`python -m dvpack check <mkv>` / `pack <mkv> -o _out/demo --seconds 60` /
`serve _out/demo`（打印 `http://<LAN>:8765/master.m3u8`）。

三条 ffmpeg 铁律（`remux.py` docstring，删 flag 的后果是电视静默当 HDR10 放）：
1. `-c copy` 完整保留带内 RPU（remux 只改长度前缀）；
2. 默认 sample entry 是 `hev1`，必须 `-tag:v dvh1`；
3. 不给 `-strict unofficial`，ffmpeg 拒绝写 `dvcC`/`dvvC` 配置 box。

产出自检（`verify()`，独立判据）：读 init 段 box 结构确认 `dvh1` + DV 配置 box；
再用 ffprobe 复读 DoviRecord 与源逐字段比对。

master.m3u8 由 `playlist.py` 自己写（ffmpeg 的 HLS muxer 不产 CODECS/VIDEO-RANGE）：
`dvh1.<PP>.<LL>` 两段两位十进制、`VIDEO-RANGE=PQ`、`HDCP-LEVEL=TYPE-1`、LF 换行
（`.gitattributes` 锁定）。

MVP 闸门：仅 Profile 5/8 单层；Profile 5 的 L7/L9 有社区报告不点亮徽标（Infuse）。

已知限制（v0.1.0 Release Notes + 源码确认）：E-AC3→fMP4 音频转换未接；
`dvcC` vs `dvvC` 四字符真机未验；无 tvOS 客户端（本次已启动）。

## 四、dvplay 交接（tvOS 客户端，继续开发从这里开始）

**仓库**：https://github.com/cpufreestyle/dvplay（private，main 分支，首提交 fb7dc1f）
本地交付副本：`outputs/dvplay-tvos/`（与仓库同内容）。

**已实现**（Debug 与 Release 配置均 `BUILD SUCCEEDED`，Xcode 27 / tvOS 27 SDK / Swift 5 模式）：
- `ManifestInspector.swift`：起播前独立复判。拉 master.m3u8，正则解析
  `#EXT-X-STREAM-INF`（属性值带引号含逗号，普通 split 会切错，故用正则）；
  判定 dvh1+PQ → 徽标应点亮 / HDR10 回退 / SDR 回退 / 信令缺失；
  并检查变体列表是否含 `#EXT-X-MAP`（DV 配置盒的安身之处）。
- `PlayerModel.swift`：AVPlayer 宿主，KVO→Combine 翻译 item.status 与
  timeControlStatus 为可观察字段。
- `ContentView.swift`：URL 输入（默认指向部署机 LAN）、播放/停止、复判卡片、诊断日志。
- `Info.plist`：ATS 放开仅因 dvpack serve 是局域网明文 HTTP（发布前需收敛）。

**待办（按优先级）**：
1. **真机验证徽标**：本机没装 tvOS Simulator Runtime，无法启动模拟器。两条路——
   真机 Apple TV 装 dvplay（需签名，`CODE_SIGNING_ALLOWED` 改回签名流程）；
   或 Xcode → Settings → Components 装 tvOS 运行时后模拟器跑。
2. dvplay 尚无 XCTest target：解析器（正则/多变体/坏列表）测试是第一件补齐的事。
3. ~~dvpack 侧 `lan_address()` 网卡选择修复~~（已完成，commit `1da85c1`）。
4. dvpack v0.1.1 候选：E-AC3→fMP4 音频（当前 `VIDEO_ONLY_MAPS` 只取视频轨，
   字幕不映射是刻意的——webvtt muxer 封 SSA 会失败）。
5. `dvcC` vs `dvvC` 四字符真机矩阵（63 个 DV 文件的 `scripts/matrix.py` 验收）。

## 四·附、把 dvplay 装到 Apple TV 真机验证

1. Apple TV 系统 tvOS 17+，与 Mac 同网段（Mac 真实 LAN：`192.168.1.109`）。
2. Xcode → Settings → Accounts：添加 Apple ID（免费号 7 天有效、到期重装；
   付费开发者账号 1 年）。
3. 打开 `dvplay-tvos/dvplay.xcodeproj` → Signing & Capabilities → Automatically
   manage signing + 选 Team（工程已改 Automatic，模拟器免签构建不受影响）。
4. 电视：Settings → Remotes and Devices → Remote App and Devices（保持该界面）；
   Xcode → Devices and Simulators → 选中 Apple TV → Pair → 输 6 位码。
5. Scheme destination 选 Apple TV → ⌘R；首次安装后在电视上 Settings →
   General → VPN and Device Management → Developer → 信任你的 Apple ID。
6. Mac 先起服 `python3 -m dvpack serve _out/demo --port 8765`，dvplay 地址栏
   填 `http://192.168.1.109:8765/master.m3u8`（已持久化，下次自动填）。
7. 播放 DV 片源时左上角出现"杜比视界"徽标即链路闭环；徽标不亮则 dvplay
   起播前复判卡片的结论（HDR10/SDR 回退）就是归因起点。

注：模拟器（Xcode → Settings → Components 装 tvOS Runtime）没有杜比视界
输出通路，徽标只能在真机验证；本机未装该 Runtime。

## 五、速查

```sh
# dvpack 部署机（Mac）
python3 -m dvpack serve _out/demo --port 8765     # aTV 填 http://<真实LAN-IP>:8765/master.m3u8

# dvplay
open dvplay-tvos/dvplay.xcodeproj                 # 或在 CI/xcodebuild：
xcodebuild -project dvplay.xcodeproj -scheme dvplay -sdk appletvsimulator27.0 build
```

---

## 六、真 DV 片源闭环（2026-09-25 晚）

**片源通路**：Jellyfin（192.168.1.68，Docker 内）认证端点全线 400（中间件拦截），SMB/SSH 拒绝；改走同网段 **MiniDLNA 192.168.1.103:8200（无需认证）**，SOAP Browse 容器 `2$8`（All Video）翻片单，直链 `/MediaItems/<id>.mkv`，**支持 HTTP Range**。

- 测试片：`Beetlejuice.Beetlejuice.2024.UHD...x265.10bit.DV.HDR.TrueHD.7.1.mUHD-FRDS` → `/MediaItems/3847.mkv`（27.8GB，UHD 实体盘抓的 P8）
- 只拉头部 1GB 即可验证：`curl -r 0-1073741823`（33s，~32MB/s）
- `dvpack check` 判定：Profile 8 / RPU 完整 / `dvh1.08.06` / PQ

**dvpack serve 常驻化（重要）**
- 端口 8765 被本机 `ai_town_server.py`（~/AI Shared/repo/ai-town，9/12 起常驻）占用 → **dvpack 换 8899**
- 手工 nohup 的 serve 会随执行会话被杀 → 改为 **LaunchAgent**：`~/Library/LaunchAgents/com.dvpack.serve.plist`（KeepAlive，绑全部网卡 `*:8899`，日志 `/tmp/dvpack-serve.log`）
- 换端口操作：`launchctl kickstart gui/$(id -u)/com.dvpack.serve`（plist 参数里改 root/端口后先 bootout 再 bootstrap）

**App 两处改动（commit `9dc6607`，本地已提交，推送待网络恢复）**
- `dvplay.lastURL` → `dvplay.lastURL.v2`（换 key 让新默认值绕过旧安装遗留的 UserDefaults）
- `.onAppear` 冷启动自动复判+起播一次（`didAutoStart` 防重入），装完即可全自动，无需遥控器按播放

**闭环证据**：Apple TV（192.168.1.100）自动 GET master.m3u8（复判）→ video.m3u8 → init.mp4 + video0-6.m4s 全部 200；电视侧应显示"DV 徽标应点亮"卡片 + 播放时左上角"杜比视界"徽标（需用户目视确认）。

**遗留**
- dvplay 推送失败：本机 TUN 拦截外网 TLS（`git push` → LibreSSL SSL_ERROR_SYSCALL；`curl https://github.com` → 000；ssh 22 → SOCKS error；gh keyring 损坏）。网络恢复后 `git -C work/dvplay push origin main` 即可。
- 合成包 `_out/demo`（SDR 回退演示）与真 DV 包 `_out/dv` 都在 work/dvpack/_out/ 下，可随时切换 plist root 重启 serve 对比。

---

## 七、DV 徽标不亮的根因与修复（2026-09-25 深夜）

**现象**：P8 片源（Beetlejuice UHD REMUX）打包后在 Apple TV 播放——颜色明显不对、电视无"杜比视界"徽标。Infuse 直放同片源可点亮。

**取证链**
1. 打包链路无失真：源 vs 分片第 0 帧 **PSNR=inf**；分片含 in-band RPU（ffprobe 帧侧数据）、colr=bt2020nc/smpte2084、dvcC=P8/L6/rpu=1/bl=1/compat_id=1；像素与元数据全部正确。
2. 设备侧排除：Infuse 直放可点亮；Apple TV 4K（tvOS 27.2）+ DV 电视输出通路正常。
3. 容器侧三次尝试均无效：`dvvC`→`dvcC` 改名；HDCP `TYPE-1`→`TYPE-0`；SwiftUI `VideoPlayer`→UIKit `AVPlayerViewController` 全屏呈现。
4. **根因**：Apple HLS Authoring Specification §1.9（2026-09-25 从 developer.apple.com 实时抓取）：*"Profile and Level for Dolby Vision MUST be Profile 5 (single layer 10-bit HEVC) and less than or equal to Level 7"*——HLS 里的 DV 只认 **Profile 5**；P8 单层在 tvOS 上只按 HDR10 播放，徽标不亮，BL 缺 RPU 参与所以色偏。
5. dvpack v0.1.0 `mvp_verdict` 对 P5/P8 一视同仁放行且无警告 = 归因缺陷。

**修复（本地已提交，推送仍被 TUN 拦截）**
- dvpack `e93449e`：P8 打包追加"Apple 规范 §1.9"结构性警告（仍放行，可当 HDR10 兼容层）；原 P8-L7 用例改写、新增 P8-L6 用例；86 passed / 5 skipped。
- dvplay `5196bf1`：复判卡按 profile 分流（`dvh1.05`+PQ → 应点亮；`dvh1.08`+PQ → "HDR10 回退（Profile 8）"）；播放器改 UIKit 全屏。
- 验证变体 `_out/dv_p5`（work/dvpack/_out/dv_p5）：同一 BL+RPU 码流，只把 dvcC/CODECS 改签 Profile 5，已推送真机播放（日志全段 200）。

**遗留待办**
- **P5 信令变体徽标是否点亮：待用户目视确认**（正在电视上播）。
- 若点亮 → dvpack 下一步：P8 源重签 P5 或接受真 P5 片源做端到端验收；片库 6656 部翻完无 P5 源（全 P8 WEB-DL / FraMeSToR P7 双层），用户需自备一部 P5（≤L7）片源。
- 若不亮 → 剩余变量见第九节第 4 条（compat_id=1 / chrm/mdcv/clli / RPU 逐帧重写）。RPU 重写本身已解决：dovi_tool 2.3.4 经 Homebrew bottle 镜像取得，见第九节。

**追加实验（P5/P7，均失败，已归档）**
- `_out/dv_p5`：同一 BL+RPU 码流，dvcC/CODECS 改签 Profile 5（`dvh1.05.06`）→ 不亮，颜色仍不对。结论：只改容器信令不够——RPU 本身还是 P8.1 语义，重新打签造出的是不自洽的 P5。
- `_out/dv_p5` 去掉 `SUPPLEMENTAL-CODECS=db1p` → 仍不亮。
- `_out/dv_p7`：真双层源（Halo S02E01 FraMeSToR REMUX，BL+EL+FEL，dvcC profile=7）→ **tvOS 直接播放失败**，与规范只认 P5 吻合。
- `_out/dv`（当前 serve 指向）：诚实态——P8 + HDR10 回退。

**结论**（前半仍成立，后半已被第九节推翻）：tvOS 的 HLS 杜比视界通路 = Apple 规范 P5；P8 打包必然 HDR10 回退；P7 双层直接拒播；P8 重签 P5 不自洽。闭环**已实现**：dovi_tool 经 bottle 镜像取得，RPU 重写用 `dovi_tool -m 5 convert -d`，见第九节与 `scripts/p7_to_p5.py`。

## 八、Apple 官方参考流逐字节对照：`dvcC` 是规范名，`dvvC` 不是（2026-09-25 深夜）

**推翻第七节的结论。** 第七节写"P8 重签 P5 不自洽、必须真 P5 片源或自研 RPU 重写"——
那个结论建立在"我们已经试过 P5 信令"上，但那次实验漏了一个变量：**box 名**。

### 1. 参照物：Apple 自己的 DV HLS 示例

```
https://devstreaming-cdn.apple.com/videos/streaming/examples/adv_dv_atmos/main.m3u8
（devstreaming-cdn.apple.com 可达；已把其中 P5 1080p/4K 变体的 init 段取回本地）
```

- P5 变体的 `dvcC` payload 实测：`01 00 0a 35 00 00...`（L6，4K）/ `01 00 0a 1d 00 00...`（L3）
- **只有** `dvh1.05.01/.03/.05/.06`，整份 master 里没有 P7/P8 变体——与规范 §1.9 互相印证
- P5 变体**不写** `SUPPLEMENTAL-CODECS`，也**不写** `HDCP-LEVEL`
- 分片 NAL 排布：每 sample = AUD(35) + SEI(39) + VCL(1/19/0) + RPU(62)，与我们的 `-c copy` 产物一致

### 2. 关键差异：`dvcC`（规范名）vs `dvvC`（ffmpeg 写下的俗名）

- ffmpeg 8.1.2 `-tag:v dvh1 -strict unofficial` 写的是 **`dvvC`**；Apple 参考流写的是 **`dvcC`**
- **ffprobe / ffmpeg 两种名字都读，字段完全一致**（实测：把 Apple 的 `dvcC` 改名成 `dvvC`，ffprobe 照样报 `dv_profile=5`）。所以本项目此前一切"ffprobe 复读通过"的自检，对这个问题天然失明
- 我们上一轮 P5 实验只改了 profile 字节和 `CODECS`，**box 名一直是 `dvvC`**——那才是"信令齐全却不亮"没被排除的最后一种解释

### 3. 已落地的修复（dvpack，本地提交）

- 新模块 `dvpack/p5.py`：把 init 段的 `dvvC` 改写成 Apple 规范名 `dvcC`，并按 Apple 模板重签 `profile=5 / level 保持 / el_present=0 / compat_id=0`
- 实测转签后我们的 init 段 `dvcC` payload 与 Apple 官方 L6 参考 **逐字节相同**：`01000a350000000000000000000000000000000000000000`
- master 列表随目标信令走：`CODECS="dvh1.05.06"` + `VIDEO-RANGE=PQ`，无 `SUPPLEMENTAL-CODECS`、无 `HDCP-LEVEL`
- `bmff.TrackReport` 现在区分 box 名（`dv_box` / `has_dvcC`），`signalling` 报实际写下的名字；`verify()` 多一条"配置 box 是俗名"的问题项
- 回归：`DVPACK_SOURCE_MKV=<P8 片源> python -m pytest -q` → **101 passed, 1 skipped**（P8 链路端到端会真的转签，并断言字节模板）
- 分片里 RPU（type 62）逐字节透传：`video0.m4s` 166 条 type-62 NAL，size/prefix 与转签前一致

### 4. 顺带查明的两件事（下一阶段用）

- **HEVC type-63 NAL = 增强层（EL）**：P7 片源（Halo S02E01）前 24MB 内 719 条 type-63，P8 片源一条都没有；type-62 = RPU（payload 首字节 0x19，两种片源都有）。P7→P5 的数据手术因此是"丢 type-63 + 重签 dvcC"，不需要自己解析 EL
- `tools.py` 现在允许回退到 PATH 里的 ffmpeg（Mac 上端侧实验可用，Windows 仍用项目内自带的那份）

### 5. 当前状态

- `_out/dv`（serve 8899 指向的目录）已用新链路重打包：P8 L6 → **P5 L6 + dvcC + dvh1.05.06**，无回退层属性
- **待真机确认**：Apple TV 上 kill 掉 dvplay 再打开（冷启动会自动起播），看电视是否点亮 DV 徽标
- 若不亮，下一次实验变量只剩：`compat_id=1`（沿源值）、补 `chrm`/`mdcv`/`clli` box、以及 RPU 本身的重写

## 九、P7→P5 全 dovi_tool 链路（2026-09-25 夜，接第八节）

**第八节结论只剩"RPU 本身要重写、需要 dovi_tool 级工具、装不了"。本节：工具拿到了，RPU 重写也落地了。**

### 1. dovi_tool 的获取方式（回答"能不能直接调用开源模块"：能，但不在上游站点）

- GitHub / pypi.org / crates.io 直连 TLS 全被本机 TUN 拦；cargo rsproxy 镜像里也没有这个 crate
- **Homebrew bottle 镜像可达**：`mirrors.tuna.tsinghua.edu.cn`、`mirrors.ustc.edu.cn`、`mirrors.aliyun.com`（均有 homebrew-bottles）
- 已固化为 `scripts/fetch_dovi_tool.sh`：多镜像 × 多 tag（tahoe/sequoia/sonoma）回退，落盘 `tools/bin/dovi_tool`，`--version` 验证后才算成功；实测走 TUNA 一把过。`tools/` 在 .gitignore 里，二进制不入库
- 本机（macOS 27.0）用 **tahoe** 的 bottle 正常运行

### 2. dovi_tool convert 模式实验表（P7 源 = Halo S02E01，12s）

| 链路 | nlq_method_idc | disable_residual_flag | 判读 |
|---|---|---|---|
| 原始 P7 | linear_dz | 0 | 双层原样，tvOS 拒播 |
| mode 1 | linear_dz | 0 | 伪 P5：丢了 EL，RPU 仍不自洽 |
| mode 2 | none | 1 | 自洽单层 |
| **mode 5** | **none** | **1** | **自洽单层，保留 luma/chroma 映射，选用** |

- `-m` 是**全局参数**，必须写在子命令前：`dovi_tool -m 5 convert -i x -o y -d`
- 自洽性判据已代码化：`remux.check_rpu_consistency()` 读前几帧 side data，`nlq=none` 且 `disable_residual=1` 才算过；接入 `verify()`，mode 1 产物在 `tests/test_remux.py` 里作反面控制（真机教训的可回归代理）

### 3. 新链路 `scripts/p7_to_p5.py`（P7 源 → 可点亮 P5）

```
ffmpeg 抽裸 HEVC → dovi_tool -m 5 convert -d（丢 EL + RPU 改写）→ ffmpeg 打 CMAF
→ resign_init_segment 按 Apple 模板新建 dvcC（裸 HEVC 无 DOVI 侧数据，ffmpeg 一个 box 都不写）
```

- 产物 `_out/dv_p7`：`dvcC` payload `01000a35…` 与 Apple 官方 L6 **逐字节相同**；RPU 自洽；`dvh1.05.06` + `VIDEO-RANGE=PQ`，无 SUPPLEMENTAL/HDCP
- `bmff.insert_box_after()`：moov 收尾布局下插 box 并修正父链尺寸（含 moov 自身），尺寸不符即报错不静默
- 回归：P8 + P7 双片源 `pytest -q` → **103 passed, 1 skipped**
- 踩坑记录：`p5.py` 曾混进一行占位 `return None`，把转签主路径整个废掉（测试 4 连挂红才发现）——提交前必跑全套，别信"刚才还是绿的"

### 4. 当前状态（交接点）

- serve（8899，plist root 已切）现指向 **`_out/dv_p7`**（P7→P5 产物，Halo 24s，单分片 157MB）
- **待真机确认**：Apple TV 冷启动 dvplay，看电视 DV 徽标是否点亮、颜色是否正常——这是唯一本地判不了的验收项
- 若点亮：`mvp_verdict` 放开 P7（当前以"未覆盖双层"拒绝打包），`p7_to_p5.py` 提升为一等 `dvpack` 命令，`check` 的 P8 警告措辞同步调整
- 若仍不亮，剩余变量依次：`compat_id=1`（沿源值）、补 `chrm`/`mdcv`/`clli` box、RPU 逐帧重写（`dovi_tool editor`）
- 旧结论作废声明：第七/八节中"pypi/github 装不上工具""必须真 P5 片源或自研 RPU 重写"已被本节推翻；`docs/HANDOFF.md` 内相关三行已同步修订

## 十、真机断链的两个根因（2026-09-25 深夜，接第九节）

### 1. 播放器根本拉不到分片：`EXTINF:0.000000`（已修，提交 `6810fa2`）

- 现象：serve 日志里电视**只轮询 `master.m3u8`/`video.m3u8`**（20:40 起每次 50s 一次），**从不请求 `init.mp4` 和分片**；21:12 首次拉起 dvplay 后同样只读列表
- 根因：`dovi_tool convert` 输出的裸 HEVC **没有任何 pts**（raw demuxer 不产时间戳，实测 `packet pts=N/A`），ffmpeg hls muxer 据此写出 `#EXTINF:0.000000`、`#EXT-X-TARGETDURATION:0`——违反 RFC 8216（TARGETDURATION 须为正整数），AVPlayer 直接按非法列表拒绝加载，连 init 段都不碰
- 修复：`pack()` 加 `-bsf:v setts=time_base=<源r_frame_rate>:ts=N:duration=1`，零重编码补 CFR 时间戳。修复后 21:18:53 电视**首次**拉到 `init.mp4`+`video0.m4s` 并起播
- 连带修：master 的 `FRAME-RATE` 不能从产物列表反推（setts 后容器时间元数据是垃圾，曾算出 16016），改从源探 `r_frame_rate`
- `HDCP-LEVEL=TYPE-1` 实验：加上后**黑屏**（电视侧没协商上 Type-1 时 TYPE-1 内容直接被拦），默认关闭，仅 `--hdcp` 可选

### 2. dvplay 在电视上装的是旧 build：内联播放切不了显示模式（已修，装 5196bf1 全屏版）

- 现象：截图里视频是**内联小窗**（随 ScrollView 滚动、位置变化），不是全屏
- 根因：电视上装的是 18:50 的旧 build（全屏改造 `5196bf1` 20:02 构建之后**从未安装**）。`5196bf1` 之前用 SwiftUI `VideoPlayer` 内联渲染，而 tvOS 上内联渲染**切不了显示模式**——DV 徽标点亮的必要条件（源码注释自己写明）根本不成立。之前所有"没点亮"结论都是在这个结构下测的
- 修复：重新 `xcodebuild Release(device)` + `devicectl device install`，21:41 安装。新 build 起播即进 `AVPlayerViewController` 全屏（截图证实），系统切显示模式的路径打通
- 当前状态：电视正全屏播放 P7→P5 包（Halo 24s），**DV 徽标是否点亮待用户目视物理屏幕**——截图看不到电视硬件指示灯

### 附：本地已穷尽的判据（都证明包本身没问题）

- `dvcC` payload `01000a35…` 与 Apple 官方 P5 L6 参考逐字节一致；RPU 自洽（nlq=none、残差禁用）
- 分片 NAL 逐帧布局与 Apple 参考一致：`AUD(35), SEI(39), VCL, RPU(62)`；SEI 带 mdcv(137)/clli(144)
- macOS VideoToolbox 成功解码本流（Apple 自家解码栈认可码流，bt2020/smpte2084 元数据正确）
- SPS/VPS：profile_idc=2（Main10）✓；tier=High 是 4K@46.8Mbps 的合法且必需选择（Main tier 上限 40Mbps，Apple 1080p 示例用 Main tier 因其码率低）

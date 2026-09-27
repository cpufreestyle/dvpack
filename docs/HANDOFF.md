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
## 十一、Jellyfin 接入：/jf/pack 端点与模块直调（2026-09-26 凌晨，接第十节）

### 1. 链路定型：Jellyfin 直连 URL → 现打 HLS → tvOS

- 电视侧只出一个地址：dvplay 判定 DV 条目后返回 `http://<packHost>/jf/pack?src=<Jellyfin 直连 URL 含 api_key>&id=<itemId>&seconds=60`
- `dvpack/jfpack.py` 新文件：`pack_source` = probe（http Range）→ `mvp_verdict` 闸门 → `remux.hls_command` → `p5.resign_pack` → master/媒体列表 → `verify`。单层 P8 转签 P5，P5 空过；**不碰 RPU 一个字节**
- 实测：5GB MKV 的 ffprobe over HTTP **0.58s**（Jellyfin 认 Range），不需要先把片拉到 Mac
- `serve.py` `/jf/pack`：src 只放行 **全部解析地址落在 RFC1918** 的 http(s)（服务听 0.0.0.0，防 SSRF）；id 走 `^[A-Za-z0-9_.-]{1,64}$`；seconds 夹 1..600；per-id 锁；缓存指纹 `src` **去掉 api_key**（token 不落盘）；打包后 302 到绝对路径 master（`/_jf/<id>/video.m3u8`，重定向后相对 URI 也不会解析错）
- **测试抓出的真 bug**：`..` 能过 id 正则（点号在白名单里），`out_dir` 会变成 `<root>/_jf/..` 即静态根目录本身，产物写进 serve 根。修法：`set(item_id) <= {"."}` 的纯点号 id 一律 400。教训同 test_serve 的 traversal 用例——白名单字符 ≠ 安全
- Swift 三处 `authHeader` 重复拼接修复（`"Authorization: " + header` 的 header 自带冒号，之前 TV 上 400 "无法链接服务器"）；DV 路由接 `packHost`（`dvplay.pack.host`，默认 `192.168.1.109:8899`）
- Swift 编译错误记录：`item.isDV` 不存在（`isDV` 在 `MediaSource` 上，`Item` 的对应属性叫 `isDolbyVisionSource`），已改；`xcodebuild -destination generic/platform=tvOS` **BUILD SUCCEEDED**
- `URLComponents.queryItems` 塞嵌套 URL 经 swiftc 实测：值里的 `&`/`=` 会被编码成 `%26`/`%3D`，Python `parse_qs` 解回来后 src 完整——`?&=` 不会被咬烂，注释里的结论成立

### 2. 模块直调方式（"能不能直接调开源/付费项目的模块" = 能，本项目已在做）

- 现成机制：`dvpack/tools.py require(path)` = 项目 `tools/bin/` 优先、PATH 回退；调用方 `remux._run(argv, cwd)` subprocess 外包。dvpack 自己只做编排（probe/闸门/校验），编解码与 box 正确性全部外包
- 已直调：`dovi_tool` 2.3.4（RPU 转换/转签）、`ffmpeg`/`ffprobe`（Range 直读 http）、**Bento4 全套 23 个工具**（brew 无 bottle，改从官网 `Bento4-SDK-1-6-0-641.universal-apple-macosx.zip` 拉，解压 `bin/` 已复制进 `tools/bin/`，`mp4dump`/`mp4info`/`mp4dash`/`mp4hls` 等随取随用）
- 付费/闭源模块同理：用户提供二进制 → 丢进 `tools/bin/` → `require()` 直接调，**零代码改动**。欠账：Apple `mediafilesegmenter`（parity 参照物）brew/GitHub 均无，官网下载需 Apple ID——等用户登录后补
- 另一条路（未用）：uv 装 Python 库直接 import；ctypes 直调 dylib。CLI 外包的收益是故障域清晰（一条命令一个进程，日志即证据）

### 3. 本轮测试与提交

- `tests/test_jfpack.py` 24 条新测试：src 校验（公网/DNS 混合/非 http）、id 白名单+纯点号、seconds 夹取、指纹去 api_key、302 与缓存命中；真片源 E2E 用 `DVPACK_SOURCE_MKV` 门控跳过
- 全量 `uv run --with pytest python -m pytest -q`：**123 passed / 10 skipped / 1 failed**（`test_remux.py::test_end_to_end_produces_dvh1_with_config_box` 为既有环境失败，无真片源环境变量，与改动无关）
- 未做：真机验收（电视 `00008110-0002149A0A9B801E` 仍休眠，截图 error 25005）；Bento4 比对出的结构差异（c608 轨/pasp/chrm/fiel/swre 缺失）与徽标的因果未证

## 十二、/jf/pack 502 的真根因：macOS 本地网络隐私（TCC）+ 回环中继（2026-09-26 凌晨，接第十一节）

## 1. 根因矩阵（launchctl submit -l <label> 一个 sleep 后读 -o/-e 文件实测）

- launchd 上下文：/usr/bin/curl、/sbin/ping（Apple 平台二进制）**成功**；/opt/homebrew/bin/ffprobe、/opt/homebrew/bin/curl、uv python（~/.local/share/uv/...）、Xcode python3.11 连 192.168.1.68 全部 **EHOSTUNREACH / No route to host**
- 同一 env -i 复制 serve 环境（HOME/TMPDIR/PATH）在交互 shell 里 rc=0 成功 → 不是环境变量问题
- 结论：macOS「本地网络」隐私按**负责进程/二进制身份**生效。LaunchAgent 拉起的第三方二进制出站连 RFC1918 被 TCC 拒；Apple 平台二进制豁免；回环 127.0.0.1 从不被拦
- 原始盲点：probe.py 用 -v quiet 把 stderr 吞干净，ServeAgent 又没身份 → 报错只剩 rc=1。**不要**再花时间装静态 ffmpeg：身份才是问题，换二进制规避不了
- 附带证据：用户早前成功播放 _out/dv_p7_fix（Apple 参考包）说明 serve 收电视入站正常——只有出站读 Jellyfin 死

## 2. 修复：进程内回环 HTTP 中继（dvpack/relay.py 新文件）

- ensure_relay() 幂等起 ThreadingHTTPServer(127.0.0.1, 0) daemon 线程；ffmpeg_visible_source(src) 把 http(s) 改写为 http://127.0.0.1:<port>/u?u=<quote(src, safe='')>；本地路径原样返回
- handler 用 /usr/bin/curl（CURL 常量，找不到才 shutil.which）拉起 -sS -N -i -X <method>，透传 Range，按行读响应头后转发 body（65536 块）；passthrough 头只有 content-type/length/range、accept-ranges、last-modified、etag
- **3xx 由中继自己续跳，不透出 Location**：ffmpeg 跟重定向会直连局域网地址，绕开中继再被 TCC 挡回，中继就白建。MAX_REDIRECTS=5
- **第一次接线就死的 bug**：quote(src, safe='') 不带参数名 → parse_qs 取不到 'u' → 每个请求 400（relay 自身与两侧测试同时挂）。参数名 u= 是指针
- 错误路径自身也会炸连接：send_error 的 message 走 latin-1 状态行，中文原因直接 UnicodeEncodeError。改为 _fail()：ASCII 状态行 + UTF-8 正文 + Content-Length
- 错误报文只留 scheme://netloc/path，不落 query（Jellyfin URL 带 api_key）
- probe.py：-v quiet 改 -v error，NotProbeable message 追加 stderr 最后一行 [:300]
- serve.py：PACK_ERRORS=(JfPackError, NotProbeable, RemuxError, ResignError, ValueError)，_jf_pack 命中先 traceback.print_exc() 再 502，客户端不再见 http 000
- 测试：tests/test_relay.py 8 条（改写规则/本地路径/幂等/全量 body/Range 透传/3xx 续跳/上游死 502/缺 u 400）；test_jfpack.py 补 2 条（502 不掐连接、stderr 进异常）

## 3. serve 的启动身份：app 壳，不是 LaunchAgent

- ~/Applications/dvpack-serve.app（LSUIElement 后台 app）：Contents/MacOS/dvpack-serve 是 zsh 脚本，**不用 exec**——exec 会把身份替换成 ~/.local/bin/uv，TCC 重新归属 launchd（实测仍 EHOSTUNREACH，已踩过），让 shell 留在进程树里当父
- 旧 LaunchAgent 已摘：launchctl bootout gui/$(id -u)/com.dvpack.serve；plist 改名 com.dvpack.serve.plist.disabled 防开机自启抢 8899
- 重启姿势：pkill -f "dvpack serve"；open ~/Applications/dvpack-serve.app；curl master 200 确认

## 4. 实测结果（2026-09-26 01:55，serve PID 64653/64654）

- 本机与局域网（192.168.1.109:8899）两条路径 /jf/pack 都 **302** → master.m3u8 200
- master：CODECS="dvh1.05.06"，RESOLUTION=3840x2160，VIDEO-RANGE=PQ；video.m3u8 11 分片 / 60s，带 EXT-X-ENDLIST
- init.mp4：DOVI configuration record profile=5 level=6 rpu_present=1；dvcC（规范名，非俗名 dvvC）
- ffmpeg 走中继时 query 串逐字节不变：本地 http.server 假上游实测 ffprobe 读出 hevc 1920x1080、ffmpeg rc=0 产出 init.mp4 + video0.m4s
- 全量 uv run --with pytest python -m pytest -q：**133 passed / 10 skipped / 1 failed**（唯一失败仍是 test_remux 既有环境失败）

## 5. 欠账

- **真机验收**：Apple TV 00008110-0002149A0A9B801E 休眠，devicectl process launch 与 --no-activate 均被拒（BSTransactionError Code=1 "System is asleep - foreground app launch forbidden"），需用户物理用遥控器唤醒
- Apple mediafilesegmenter parity 欠账仍在（官网下载需 Apple ID）
- DV 徽标点亮与颜色是否正常待真机目视；Bento4 比对出的结构差异（c608 轨/pasp/chrm/fiel/swre）与徽标的因果未证
- dvpack 未提交改动：relay.py / probe.py / serve.py / jfpack.py / test_relay.py + 上回合 bmff.py / scripts/p7_to_p5.py / test_bmff.py；禁 push

## 十三、「颜色不对 + 徽标不亮」的根因修复：init 段缺 colr 盒（2026-09-26 02:10，接第十二节）

### 1. 根因（实测证据，与 infuse 对照）

- 片源 534f...stream.mkv：ffprobe **帧级**（读 VUI）color_space/color_transfer/color_primaries 全空——HEVC SPS 无 VUI 色彩标记，MKV 容器也无 colour tag。色彩信息只存在于 dvcC/RPU
- ffmpeg -c copy 打 fmp4 时不会凭空编色彩信号：mp4dump 旧产物 init.mp4 的 [dvh1] 子盒只有 hvcC / dvcC / pasp，**无 colr**
- AVPlayer 收不到 PQ/BT.2020 信号 → 按 SDR BT.709 伽马映射 → 真机症状「颜色明显不对」+ 电视 DV 指示灯不亮。同源 infuse 直读文件能亮（直读路径 App 侧自己注入色彩信号），设备与电视无故障
- 之前几节的排查方向（c608/pasp/chrm/fiel/swre 差异、compat_id）都不是根因：缺的是最基础的 colr，它不在怀疑名单里是因为历次 ffprobe 检查都看码流、没人 dump 过 init 的 stsd 子盒

### 2. 修复（dvpack 侧 3 个文件）

- dvpack/remux.py：新增 colorimetry_flags() 与 DV_DEFAULT_COLORIMETRY = ("bt2020", "smpte2084", "bt2020nc")；hls_command() 加 colorimetry 参数，输出侧插 -color_primaries/-color_trc/-colorspace——fmp4 muxer 据此把 colr（nclx 形式）写进 init 的 stsd
- dvpack/jfpack.py：新增 _source_colorimetry()：源三件套齐全（HDR10/HLG 片）就沿用；为空（DV rip 常态，本例）回落 DV 默认 PQ 三件套
- 测试：test_remux.py 新增默认写 colr + 沿用源 colorimetry 两例；test_jfpack.py 新增派生函数两例

### 3. 验证（2026-09-26 02:09 实测）

- 修复后产物 mp4dump：[dvh1] 子盒为 hvcC / [colr] / dvcC / pasp；master 仍 CODECS="dvh1.05.06" + VIDEO-RANGE=PQ
- DVPACK_SOURCE_MKV 指向本地片源后，既有 E2E test_end_to_end_produces_dvh1_with_config_box **passed**（此前失败只因没给片源路径）
- 全量：**137 passed / 10 skipped / 1 failed**（唯一失败即该 E2E 的缺片源形态，勿修）
- serve 已用新代码重启（PID 68042/68043），旧缓存 _out/dv_p7_fix/_jf 已 mv 到 _jf.stale-pre-colr-20260926（缓存按 src+seconds 判定新鲜，不失效电视会拿到旧 init）；本机 /jf/pack 302 → init.mp4 200 且含 colr

### 4. 下一步（需要真机）

- 用户在电视上**重新起播**该片：看 DV 指示灯是否点亮、颜色是否正常；serve 日志 /tmp/dvpack-serve.log 见 /_jf/<id>/init.mp4 200 即可
- 若徽标仍不亮，下一个实验变量（按嫌疑排序）：dvcC 的 dv_bl_signal_compatibility_id=0（DV P5 主流母版为 1 或 2，源值就是 0；手段：p5.resign 的 compat_id 参数，p7/p8 路径已在用）
- 片源本地样本：/tmp/dvboxdiff/src2.mkv（100MB 前段，/usr/bin/curl -r 0-100000000 取；TCC 豁免二进制）

---

## 十四、根 master 指错流：dvplay 默认 URL 重指到修复流（2026-09-26 02:26 实测）

### 1. 根因（本轮最大发现）

- dvplay 启动默认 URL 是**根** /master.m3u8（work/dvplay/dvplay/ContentView.swift:15，@AppStorage("dvplay.lastURL.v2")），启动即 preflight 它
- 而 serve 根目录（_out/dv_p7_fix）里的 master.m3u8 / video.m3u8 / fileSequence* 是 **mp42hls 产的 1080p 旧流**：CODECS="dvh1.05.03"、RESOLUTION=1920x1080；dump_tree 扒 fileSequence0.mp4：dvh1(326B) > hvcC(156B, 含参数集) + dvcC(01 00 0a 1d 00 00 = P5 L3) + **无 colr**
- 即之前若干轮真机复播**根本没播到修复过的流**——徽标不亮/颜色不对/播放失败至少一半原因在此；Jellyfin 选片流程（/jf/pack → 302 → /_jf/<id>/master.m3u8）本身作用域正确

### 2. 处置

- 旧根 master 备份为 _out/dv_p7_fix/master.m3u8.stale-1080p-no-colr-20260926
- 新根 master.m3u8 = item master 原文：BANDWIDTH=17058152,CODECS="dvh1.05.06",RESOLUTION=3840x2160,FRAME-RATE=24,VIDEO-RANGE=PQ，URI=/_jf/534f5cb2ba7f9d500f9f2154e8cff079/video.m3u8
- stale 遗留 fileSequence* + video.m3u8 移至 _out/dv_p7_fix/stale-root-1080p-20260926/

### 3. 线上信号核对（脚本：/tmp/dvboxdiff/dump_tree.py）

- /_jf/534f.../init.mp4：dvh1(184B) > hvcC(31B, 空参数集、VPS/SPS/PPS 走分片 in-band) + colr(nclx bt2020/PQ/bt2020nc) + dvcC(01 00 0a 35 00 00 = P5 L6, rpu=1, el=0, bl=1, compat=0，与 Apple 官方参考逐字节一致) + pasp
- ES 普查（/tmp/dvboxdiff/census_seg.py，手工抽 mdat 转 Annex B，ffmpeg demux 不了无 init 的分片）：video0.m4s NAL 共 2096，DOVI RPU 345(t62)、SEI 359(t39)、VPS×4/SPS×4/PPS×4

### 4. 真机进展

- Apple TV 休眠 → devicectl launch 连续被拒（System is asleep - foreground app launch forbidden）；devicectl 无 wake 子命令（simulate 只有 biometrics/location/statusBar），最终 device reboot --style full --wait-for-device 解除：12s 后 available (paired)，配对保持，launch 成功
- dvplay 重启后自动 preflight 修复流：192.168.1.100 命中 GET /_jf/.../init.mp4 200 + video0.m4s 200（preflight 只拉 init+首片即停，之后无持续分片请求=等用户起播）
- **仍待用户在电视上手动起播确认**：DV 徽标是否点亮、颜色是否正常；若亮，下一里程碑是音频（_jf pack 现为 video-only，VIDEO_ONLY_MAPS）

### 5. 环境坑新增

- dump 脚本解析 init 时：stsd 入口偏移 +16（含 entry_count），VisualSampleEntry 子盒从 box 内 +86 开始（6 预留 + 2 dref + 70 固定字段）；错位会解析出假 box
- Apple TV IP 是 192.168.1.100（dvplay 侧），电脑 192.168.1.109（ContentPack host）；serve 日志按 client IP 区分电视/本机请求

---

## 十五、单 entry 归一手术：BSF 注入产物收敛为 Apple 官方模板结构（2026-09-26 03:20）

### 1. 事实链（全部本回合实测）

- ffmpeg 8.1.2 字节真相（/tmp/dvboxdiff/plain.mp4 vs inj.mp4）：裸 -c:v copy remux → hvcC 23B、numOfArrays=0（真机秒退根因）；-bsf:v hevc_mp4toannexb,extract_extradata → hvcC 232B、4 数组（32/33/34/39 前缀 SEI），mp4 直 remux 也生效
- BSF 双 entry 产物 init 1400B：stsd entry_count=2；entry0 hvcC 232B（VPS+SPS+PPS+SEI-39 前缀数组），entry1 hvcC 183B（纯 VPS/SPS/PPS）
- 分片索引实测（/tmp/dvboxdiff/idxprobe.py、allfrag.py）：trex default_sample_description_index=1，11 个分片 tfhd flags=0x20038 全无 sample-description-index 覆盖 → 分片只引 entry0，entry1 是死重
- 对照：Apple 官方 P5 参考流（adv_dv_atmos）与已验证能播的 1080p 流（stale-root-1080p-20260926/fileSequence0.mp4）均为单 dvh1 entry

### 2. 代码改动（work/dvpack，未提交，HEAD 9d01e0b）

- dvpack/bmff.py 新增 sample_entry_count(data) 与 keep_single_sample_entry(data, keep_index=0)：手术法同 remove_box——victim 字节整段删除、stsd entry_count 减 1、父链（stsd → stbl → minf → mdia → trak → moov）尺寸字段逐个校验后扣减；字段与解析不符即 raise；混入非 video entry（VIDEO_SAMPLE_ENTRIES 外）、keep_index 越界同样 raise；已是单 entry 时原样返回 dropped=0（幂等）
- dvpack/jfpack.py：pack_source() 在 resign_pack 后接入 keep_single_sample_entry，dropped 非 0 才写回

### 3. 测试（全量 159 passed / 2 skipped）

- tests/test_bmff.py：test_hvcC_array_offset_23_matches_real_ffmpeg_output 改为钉 ffmpeg 8.1.2 两种真相——plain remux numOfArrays=0 / types=[]；BSF 链 numOfArrays=4、set(types)=={32,33,34,39}
- tests/test_bmff.py 新增 keep_single_sample_entry 单测 5 条：计数 2、默认保 entry0（参数集 32/33/34/39）、keep_index=1 保 entry1（32/33/34）、单 entry 幂等、混入 mp4a raise"非 video"、keep_index=2 raise"越界"
- tests/test_jfpack.py 新增 E2E test_pack_source_converges_to_single_sample_entry：产物 init 单 entry + hvcC 含 32/33/34 + 收敛函数幂等
- 新踩坑：hvcC_param_set_types 会校验 NAL 首字节高 6 位 == 数组类型，合成样本必须以 bytes([type << 1]) 开头；parse_file 只收 Path 不收 bytes

### 4. 上线（2026-09-26 03:20，serve 未重启）

- _jf.new-single/534f... repack 产物：init 1056B；mp4dump 验 stsd entry_count=1、单 dvh1（385B）、hvcC 232B、dvcC 24B；EXTINF 全 >0；verify ok（dvh1+dvcC，P5 L6，rpu=1）
- 热替换：mv _jf → _jf.stale-bsf-double-entry-20260926，mv _jf.new-single → _jf
- serve 侧核验：/usr/bin/curl --noproxy '*' master/init/video.m3u8 全 200，init size=1056（serve PID 68043，launchd 下，端口 8899；LaunchAgent plist 已禁用，别指望开机自启）
- /tmp/dvpack-serve.log 994 行基线（含本机 127.0.0.1 探针；电视 192.168.1.100 起播前无请求）

### 5. 待验证与下一变体

- 用户在电视上重新起播该片：看 DV 徽标 + 颜色；日志里 192.168.1.100 应连续拉 video1..10.m4s（此前 23B 空 hvcC 是 video0 后即断）
- 失败则下一变体：剥 entry0 hvcC 的 type-39 SEI 前缀数组（232B → 183B，与 entry1 结构一致）
- 徽标+颜色确认后：音频里程碑（现 _jf pack 为 video-only，E-AC3 5.1 需单独接 fmp4）

## 十六、变体 B（剥 type-39 SEI 数组）：全链路验证 + 待命（2026-09-26 03:33，接第十五节）

### 1. E2E 本地验证（live 包 = 变体 A：单 entry + 39 SEI 组）

- ffmpeg/ffprobe 实际在 /opt/homebrew/bin/（8.1.2）；tools/bin/ffmpeg 不存在，require() 按设计回退 PATH——直调本机 brew 版即可，别再找项目内二进制
- ffprobe 过 http://127.0.0.1:8899/_jf/534f.../video.m3u8：hevc Main10 / 3840x2160 / yuv420p10le / bt2020nc + smpte2084（PQ）+ bt2020；SIDE_DATA：DOVI configuration record，dv_profile=5 dv_level=6 rpu_present=1
- 列表：EXT-X-VERSION:7、TARGETDURATION:14、EXTINF 全 >0（0.164~14.389）、ENDLIST、MAP=init.mp4
- -t 12 -c:v copy -f null -：零告警 → init+分片组合无误（trex/flags 语义未被手术破坏）
- 真解码 -f null -（全程 60s）：6 条 "Application provided invalid, non monotonically increasing dts"

### 2. 重复 DTS 定位：dovi_tool/CMAF 阶段引入，非切片放大

- 对照 /tmp/dvboxdiff/src2.mkv 直解：0 条 → 源干净
- 中间产物 /tmp/dvboxdiff/inj.mp4（分片前 CMAF 整文件）：1 条 → 缺陷在 dovi_tool 转换或 CMAF 封装阶段，HLS 切片把它放大到 6
- 重复 dts 值簇 3, 493, 496, 499, 503, 506 集中在 video1/video2 边界带；copy 路径零告警 → 分片字节时间戳本身单调，decoder 出口才现形
- 判读：约 1440 帧里 6 帧，AVPlayer 容忍度未验证；不阻塞徽标实验，记录备查
- 排除项：ffprobe 单读 .m4s 不可行（无 moov，须挂 init），frame 级 dts 导出这条路无效，别浪费时间

### 3. 变体 B 落地（待命，未上线）

- dvpack/bmff.py 新增 drop_hvcC_param_set_type(data, nal_type)：整组删数组、numOfArrays 扣减、保留组逐字节照抄、父链尺寸逐个校验扣减（手术法同 dedupe_hvcC_parameter_sets）；点名删 32/33/34 直接 raise「必需」；无目标组时 (data, 0) 幂等
- dvpack/jfpack.py pack_source 加 drop_param_set_type 参数门控，默认 None → live 变体 A 行为零变化
- 单测 +5：剥单 SEI 组、剥三 NAL SEI 组（复现真机 (39,[6,22,13])）、缺失幂等、拒删 32/33/34、单 entry 后剥 39 得能播模板同构
- 全量 pytest：164 passed / 2 skipped（上回合 159 → +5）
- repack：_out/dv_p7_fix/_jf.new-no39/534f.../（脚本 /tmp/dvboxdiff/repack_no39.py，master_uri 与 live 相同，热替换即生效）；verify ok（dvh1+dvcC，P5 L6，rpu=1 el=0 bl=1 compat=0）
- A/B 对照（mp4dump，init 1056B → 1006B = 整 50B SEI 组）：

| 盒 | A live | B new-no39 |
|---|---|---|
| hvcC | 8+232 | 8+182 |
| dvh1 | 8+385 | 8+335 |
| stsd | 12+397 | 12+347 |
| dvcC | 8+24 | 8+24（不动） |
| entry_count | 1 | 1 |

- bmff.hvcC_param_set_types：A=(32,33,34,39) → B=(32,33,34)
- 11/11 分片 md5 逐字节相同 → A/B 唯一差异就是 init 里那 50B，测量干净
- B 本地播放列表 -c:v copy 零告警；DOVI side data profile 5 保持

### 4. 电视状态与下一步

- 192.168.1.100 最后请求仍停在 02:26:05（双 entry 时代拉过 video0 即断）；03:20 live 换成变体 A 后电视零请求——用户还没复播
- 用户回来 → 电视上对该片重新起播 → 看 /tmp/dvpack-serve.log 里 192.168.1.100 是否连续拉 video1..10.m4s + 问徽标/颜色
- 失败分支（播放失败/徽标不亮）：mv _jf _jf.stale-single-20260926 && mv _jf.new-no39 _jf（serve 不重启）→ 请用户复播
- 成功分支：音频里程碑（E-AC3 5.1 → fmp4，现 pack 为 video-only）→ memos add → update_goal complete → HANDOFF 十七节

---

## 十八、"能不能直接调模块"的一次完整回答 + Apple 官方 init 逐字段对照 + 变体 C（2026-09-26 03:45，接第十七节）

**用户提问**："你能不能直接调用开源项目或者付费项目里面的模块"。回答分三层，本项目已在做第一层。

### 1. 三层的边界（结论，不是设想）

- **CLI 二进制直调（已落地，0 代码改动）**：`dvpack/tools.py require()` = `tools/bin/` 优先、PATH 回退；
  调用方 `remux._run(argv, cwd)` subprocess 外包。已直调：`dovi_tool` 2.3.4、`ffmpeg`/`ffprobe` 8.1.2、
  **Bento4 全套**。任何开源 / 付费 / 闭源二进制（用户给可执行文件 → 丢 `tools/bin/`）都能直接调。
- **库级链接（受 tvOS slice 限制，对本症状基本无效）**：dvplay 可 SPM/XCFramework 引 `ffmpeg-kit`
  （有 tvOS slice）/ `VLCKit`（LGPL，有 tvOS）。但 tvOS 硬解必须走 VideoToolbox，DV 徽标由
  AVFoundation/VideoToolbox 按 fMP4 init 段信令决定——换库不改信令，测试里换库对徽标无差。
- **付费 SDK（可评估，不可拆）**：Bitmovin / TheoPlayer / castLabs 有 tvOS SDK + 试用授权，
  但仍跑在 AVFoundation 上；**Infuse 是闭源 App，内核不能拆、不能被调用**，只能当参照物。

### 2. 本轮新证据：`tools/utils/` 补齐，Bento4 `mp4hls` 实测不可用

- `tools/bin/mp4hls`（以及 `mp4dash`）此前调用必失败：wrapper 是 `exec python3 $BASEDIR/../utils/mp4-hls.py`，
  而当时只从 SDK 复制了 `bin/`、漏了 `utils/`。已重新拉 SDK 并补齐：
  `curl https://www.bok.net/Bento4/binaries/Bento4-SDK-1-6-0-641.universal-apple-macosx.zip`
  （注意：`www.bento4.com/downloads/stable/...` 这个路径是 **404**，真实 href 在 `www.bok.net`）→
  `cp -R utils/. tools/utils/`。23 个 `bin/` 二进制与本地逐一比对无缺失，缺的只是 python 前端。
- **实测结论：Bento4 `mp4hls` 不能用于 dvpack。** 对 `inj.mp4` 跑 `mp4hls --hls-version=7 --segment-duration=6`：
  产物是 **MPEG-TS 分片**（`segment-0.ts`）、master 里 `CODECS="hev1.2.4.L150.90"`、
  没有 init 段、`dvcC`/`dvh1` 全部丢失——"调一个模块"直接会把杜比视界信令抹掉。
  `mp4fragment` 同样不保留 DV box（它对 pre-resign 输入产物 0 个 dvcC/dvh1/colr）。
  教训：付费 / 开源模块不是黑盒增益，先验证它在目标通路上**保留信令**再采用。

### 3. Apple 官方参考流 init 段逐字段对照（`_out/apple_ref/fileSequence0.mp4` vs 我们的 init）

| 字段 | Apple 官方 P5 1080p | 我们（变体 A，4K） |
|---|---|---|
| `ftyp` | `iso5` / minor 1 / brands `isom,iso5,hlsf` | `iso5` / minor 200 / `iso5,iso6,dby1,mp41` |
| `colr` | **没有** | 有：`nclx` prim=9 transfer=16 matrix=9 full=0x80（11 字节，格式合法） |
| `stsd` 尾部 | `hvcC, dvcC, pasp, chrm, fiel, pasp` | `hvcC, colr, dvcC, pasp` |
| `chrm`/`fiel` | 有（payload `00 00` / `01 00`） | 无 |
| `mdhd` timescale | 24000 | 16000 |
| `hdlr` name | `Core Media Video` | `VideoHandler` |
| `language` | `eng` | `und` |
| `tkhd` flags | 1（无 `edts`） | 3（带 `edts/elst`） |
| `udta` | `swre`（"mediafilesegmenter: Version 1.20 …"） | `meta/ilst/.too = Lavf62.12.102` |
| 第 2 轨 | CEA-608 `c608` + 对应 `trex` | 无（单轨） |
| `dvcC` | profile=5 level=3 compat_id=0 rpu=1 el=0 | profile=5 level=6 compat_id=0 rpu=1 el=0 |

**已排除**：`colr` 本身不是坏 box——`nclx` 11 字节 payload 是标准长度，字段合法（BT.2020 / PQ / BT.2020nc）。
所以差异不是"写错了"，而是"写了 Apple 不写的东西"。最可疑的是 **`colr` 存在性**与 **ftyp 缺 `hlsf`**。

### 4. 变体 C 已落盘待命（init 整形为 Apple 模板，分片一字节不动）

- 脚本 `work/mk_variant_c.py`（复用 `bmff.remove_box` / `insert_box_after`）。产物
  `_out/dv_p7_fix/_jf.apple-tmpl/534f5cb2ba7f9d500f9f2154e8cff079/`
- 改动：`ftyp` → `iso5/1/isom+iso5+hlsf`；删 `colr`；`stsd` 尾部补 `chrm/fiel/pasp`（字节抄 Apple）；
  `udta` 清空（去 ffmpeg `meta/.too`）。init **1056B → 979B**。
- 校验：11/11 分片 md5 与变体 A **完全相同**；`_scan_buffer` = `['ftyp','moov']`；
  `hvcC_param_set_types = (32,33,34,39)`；ffprobe 过 playlist → `dv_profile=5 dv_level=6`、Main10/3840x2160/yuv420p10le；
  `ffmpeg -c:v copy -f null -` 零告警。容器侧 color_* 变为 unknown（与 Apple 参考流一致，色彩靠 dvcC + RPU 承载）。
- 踩坑（复用价值高）：`mk_variant_c.py` 里 moov 尺寸修正一开始写错成
  `buf[moov_start+4:moov_start+8]`（那是 fourcc 字段），把 `moov` 写成 `moo\x1c`、文件整体短 90B。
  判据：改完必须 `_scan_buffer()` 顶层仍是 `['ftyp','moov']`，且 `moov` 四字节类型字段没被碰。

### 5. 下一步（按序，同第十七节）

1. 用户回电视，对 dvplay 里该片**重新起播**（自 02:26 起 192.168.1.100 无任何请求，serve 日志基线 1205 行）。
2. 起播后先问徽标/颜色（变体 A）。不亮 → 热替换 C：
   `mv _out/dv_p7_fix/_jf _out/dv_p7_fix/_jf.stale-A-20260926 && mv _out/dv_p7_fix/_jf.apple-tmpl _out/dv_p7_fix/_jf`
   serve 不重启（plist root 指向 `_out/dv_p7_fix`），再请复播。
3. 仍不亮 → 变体 B（剥 hvcC NAL type 39：`_jf.new-no39/`）与 C 可叠加成 C+B。
4. 都不亮 → 结论收敛到"不是 init 容器信令"，转查码流/RPU 与电视侧 HDMI 协商。

---

## 十八、"能不能直接调模块"的完整回答 + Apple 官方 init 逐字段对照 + 变体 C（2026-09-26 03:45）

**用户提问**："你能不能直接调用开源项目或者付费项目里面的模块"。

### 1. 三层的边界

- **CLI 二进制直调（已落地，0 代码改动）**：`dvpack/tools.py require()` = `tools/bin/` 优先、PATH 回退，调用方 `remux._run(argv, cwd)` subprocess 外包。已直调 `dovi_tool` 2.3.4、`ffmpeg`/`ffprobe` 8.1.2、Bento4 全套。任何开源/付费/闭源二进制（用户给可执行文件 -> 丢 `tools/bin/`）都能直接调。
- **库级链接（受 tvOS slice 限制，对本症状无效）**：dvplay 可引 `ffmpeg-kit`（有 tvOS slice）/ `VLCKit`（LGPL，有 tvOS）。但 tvOS 硬解必须走 VideoToolbox，DV 徽标由 AVFoundation/VideoToolbox 按 init 段信令决定，换库不改信令。
- **付费 SDK（可评估，不可拆）**：Bitmovin / TheoPlayer / castLabs 有 tvOS SDK + 试用授权，但仍跑在 AVFoundation 上；Infuse 是闭源 App，内核不能拆、不能被调用，只能当参照物。

### 2. 本轮新证据：`tools/utils/` 补齐，Bento4 `mp4hls` 实测不可用

- `tools/bin/mp4hls`（含 `mp4dash`）此前必失败：wrapper 是 `exec python3 $BASEDIR/../utils/mp4-hls.py`，而本地只复制了 SDK 的 `bin/`、漏了 `utils/`。已补齐：下载 `https://www.bok.net/Bento4/binaries/Bento4-SDK-1-6-0-641.universal-apple-macosx.zip`（注意 `www.bento4.com/downloads/stable/...` 是 **404**，真实 href 在 `www.bok.net`），`cp -R utils/. tools/utils/`。23 个 `bin/` 二进制与 SDK 逐一比对无缺失。
- **实测：Bento4 `mp4hls` 不能用于 dvpack。** 对 `inj.mp4` 跑 `mp4hls --hls-version=7 --segment-duration=6`：产物是 MPEG-TS 分片（`segment-0.ts`）、master 里 `CODECS="hev1.2.4.L150.90"`、无 init 段、`dvcC`/`dvh1` 全丢。`mp4fragment` 同样不保留 DV box。教训：模块不是黑盒增益，先验证它在目标通路上保留信令再采用。

### 3. Apple 官方参考流 init 逐字段对照（`_out/apple_ref/fileSequence0.mp4` vs 我们）

| 字段 | Apple 官方 P5 1080p | 变体 A（我们，4K） |
|---|---|---|
| `ftyp` | `iso5`/minor 1/`isom,iso5,hlsf` | `iso5`/minor 200/`iso5,iso6,dby1,mp41` |
| `colr` | **没有** | 有 `nclx` prim=9 transfer=16 matrix=9 full=0x80（11 字节，合法） |
| `stsd` 尾部 | `hvcC, dvcC, pasp, chrm, fiel, pasp` | `hvcC, colr, dvcC, pasp` |
| `mdhd` timescale | 24000 | 16000 |
| `hdlr` name | `Core Media Video` | `VideoHandler` |
| `language` | `eng` | `und` |
| `tkhd` flags | 1（无 edts） | 3（带 edts/elst） |
| `udta` | `swre` = "mediafilesegmenter: Version 1.20..." | `meta/ilst/.too` = Lavf62.12.102 |
| 第 2 轨 | CEA-608 `c608` + 对应 trex | 无（单轨） |
| `dvcC` | P5 L3 compat_id 0 | P5 L6 compat_id 0 |

已排除：`colr` 不是坏 box，`nclx` 11 字节 payload 是标准长度。差异性质是"写了 Apple 不写的东西"，最可疑为 `colr` 存在性与 ftyp 缺 `hlsf`。

### 4. 变体 C 已落盘待命（init 整形为 Apple 模板，分片一字节不动）

- 脚本 `work/mk_variant_c.py`（复用 `bmff.remove_box` / `insert_box_after`）；产物 `_out/dv_p7_fix/_jf.apple-tmpl/534f.../`
- 改动：`ftyp` -> `iso5/1/isom+iso5+hlsf`；删 `colr`；`stsd` 尾部补 `chrm/fiel/pasp`（字节抄 Apple）；`udta` 清空。init `1056B -> 979B`。
- 校验：11/11 分片 md5 与 A 完全相同；`_scan_buffer` = `['ftyp','moov']`；`hvcC_param_set_types = (32,33,34,39)`；ffprobe `dv_profile=5 dv_level=6`、Main10/3840x2160/yuv420p10le；`-c:v copy -f null -` 零告警。
- 踩坑：moov 尺寸修正一开始错写成 `buf[moov_start+4:moov_start+8]`（那是 fourcc），把 `moov` 写成 `moo\x1c`、文件短 90B。判据：改完必须 `_scan_buffer()` 顶层仍为 `['ftyp','moov']`。

### 5. 下一步

1. 用户回电视对 dvplay 该片重新起播（自 02:26 起 192.168.1.100 无请求，serve 日志基线 1205 行），问徽标/颜色。
2. 不亮则热替换 C：`mv _out/dv_p7_fix/_jf _jf.stale-A-20260926 && mv _out/dv_p7_fix/_jf.apple-tmpl _jf`，serve 不重启，再复播。
3. 仍不亮则把 C 与 B（剥 hvcC NAL 39，`_jf.new-no39/`）叠成 C+B。
4. 都不亮则结论收敛到"不是 init 容器信令"，转查码流/RPU 与 HDMI 协商。

## 24. C+B 四项校验通过 + 部署 8899（2026-09-26 05:18-05:20）

### C+B init 校验（全过）
- 尺寸：C+B 971B（`/tmp/jfcb_flat/init.mp4` = `_out/dv_p7_fix/_jf.cb/<id>/init.mp4`）；live C 979B
- 分片：video0-10.m4s md5 与 live C 逐字节 SAME
- ffprobe：hevc / Main 10 / 3840x2160 / dv_profile=5 / dv_level=6
- 顶层 box `['ftyp','moov']` 与 Apple 官方 `fileSequence0.mp4` 相同；hvcC 182B 三数组（VPS 24 / SPS 111 / PPS 9），`naltype=39` SEI 数组已消失
- ffmpeg 整流：`-i /tmp/jfcb_flat/master.m3u8 -c copy -f null -` rc=0 零告警

### 部署与状态
- 05:19 停旧 serve（PID 7926，根 `/tmp/apple_gt/root` 文件保留），杀空跑 15253；tmux `jfcb` 起 serve `/tmp/jfcb_flat --port 8899`（PID 17568）
- curl 复核全 200（init 971B / video0 7.9MB）；TV（192.168.1.100）已顺序拉取 video0-8.m4s，lsof 三条 ESTABLISHED
- gtdv2 循环仍在自动重启 app（round 39/60 @05:19:52，约 05:31 自然结束）；app PID 437
- autoplay URL 未变：`http://192.168.1.109:8899/master.m3u8`

### 待目视判定
- 官方样片 35 轮用户未答复；本轮直接测 C+B——灯亮+颜色对 = 修复成立（与官方样片亮否无关）
- 亮 -> 进 Jellyfin 端到端复验（/jf/pack -> 302 -> master 200 -> 徽标）
- 不亮 -> app 层（AVPlayerLayer / tvOS 27 怪癖）或 Plan B（shaka-packager / GPAC / ffmpeg 重 pack 官方样片逐 box 对照）
- 次要嫌疑（仍未对齐）：hvcC byte1 tier 0x02 vs Apple 0x22、constraint 0x90 vs 0xb0、SPS/PPS 尺寸差异
- 官方样片复部署：`tmux new-session -d -s jfgt '<serve /tmp/apple_gt/root --port 8899>'`

## 25. Loop 挂死根因 + FFmpeg 路线证伪（2026-09-26 05:22）

### Loop 挂死与恢复
- 05:20:25 iter-40 的 `xcrun devicectl device process launch` 挂死（PID 17717，锁住 devicectl），整条循环停摆
- 05:22:54 `kill -9 17717` 后 loop 立即恢复（iter-40 @05:22:56）
- 教训：不要手动 launch app 与 loop 竞争 devicectl（此前 05:19 手动 launch 疑似诱发竞争）；后续 app 生命周期只交给 loop

### FFmpeg 8.1.2 重 pack 官方样片（/tmp/apple_ff，`-sn -c copy -tag:v dvh1 -f hls -hls_segment_type fmp4`）
- hvcC 149B 完整克隆 Apple（head `012220000000b0...` 逐字节相同，三数组 24/79/8）
- **但 dvcC 丢失**：mp4dump 只见 `[dvh1]` + `[hvcC]`，无 `dvcC` box——FFmpeg mp4 muxer 不写 DOVI 配置记录
- master.m3u8 无 `CODECS="dvh1.05.06"`（只有 RESOLUTION）；默认 2s 分段 + 滚动 5 段窗口
- 结论：FFmpeg 路线对 DV 信令不可用，init 必须手工对齐（C+B 路线正确）；FFmpeg 仅可作分片/整流验证工具

### C+B 部署态（本轮确认）
- serve `/tmp/jfcb_flat` PID 17568（tmux `jfcb`），TV 于 05:20:24-25 顺序拉 video0-8.m4s（全 200），05:21:10/05:21:56/05:22:42 持续刷新 video.m3u8 = 播放中
- 待用户目视：DV 徽标 + 颜色；不亮 -> app 层（AVPlayerLayer / tvOS 27）或对齐 hvcC 残余字节（tier 0x22/constraint 0xb0）

## 26. 变体 D+：能力信令 + VUI 颜色信令双修正并上线（2026-09-26 05:55-06:04）

### 逐字节对照结论（我方 init vs Apple 官方 fileSequence0.mp4）
- hvcC payload 头：[1] 02→22（tier Main→High）、[6] 90→b0（non_packed_constraint 0→1）、三个 array 头 0x20/0x21/0x22→0xa0/0xa1/0xa2（array_completeness 0→1）
- hvcC 内 VPS（len 24，与 Apple 同长）：[6] 02→22、[12] 90→b0；SPS：[3] 02→22、[9] 90→b0
- 分片 mdat 内嵌 VPS/SPS 同点位同翻：11 分片 22 VPS + 22 SPS，改动 88 字节
- DOVI 记录已一致：dv_profile=5 / dv_level=6 / bl_signal_compatibility_id=0

### 新发现：VUI 颜色信令错误（修正并入 D+）
- 我方 SPS/VUI：video_signal_type_present=1, full_range=1, primaries=2(BT.709), transfer=2, matrix=2——把 BT.2020 PQ 杜比视界显式标成 BT.709 全范围
- Apple 官方：video_signal_type_present=0（不带 VUI 颜色信令）；ffprobe：Apple color_range=tv，我方 color_range=pc
- D+ 修正（仅翻 bit 不改长度）：full_range 1→0，2/2/2→9/16/9（BT.2020/PQ/BT.2020NCL）；hvcC 内 4B + 22 个分片 SPS 各 4B = 88B
- segment 合计与 C+B 差 176B（88 信令 + 88 颜色），所有文件长度零变化

### mk_variant_d.py 修复记录
- patch_init 扩为四合一：PTL 头 + hvcC 内 VPS/SPS + 全部 array completeness + SPS VUI 颜色；patch_segment 同步 VUI
- 之前坏的根因：_hvcC_payload 定义在 main() 之后 → NameError；已上移至 patch_init 前
- EBSP/RBSP 转义映射按位翻转，长度不变 → moof/trun 尺寸字段全部保持有效
- 断言保护：VUI 位期望值不符即 AssertionError；位偏移 650/653/655/663/671 由 ffmpeg trace_headers 实测读出（多实例一致）

### 校验（全过）
- init：信令 9B（PTL 2 + array×3 + VPS/SPS 各 2）+ VUI 4B = 13B；信令位与 Apple 官方逐字节一致（SPS/PPS 本体长度差异属编码器差异，未动）
- 分片：11 文件尺寸零变化，总 diff 176B；22 VPS + 22 SPS 全翻
- ffmpeg -i /tmp/jfd_flat/master.m3u8 -c copy -f null - rc=0 零告警
- trace_headers 复核：full_range=0 / primaries=9 / transfer=16 / matrix=9；ffprobe color_range=tv

### 部署
- 06:02 停 tmux jfcb（旧 serve 17568），杀空跑 apple_gt serve 17552；起 tmux jfd：serve /tmp/jfd_flat --port 8899（PID 27143），curl init md5 与本地一致，master/video0/init 全 200
- gtdv2 循环重建（06:02:11，seq 1..60）；06:02 首次 devicectl launch 挂死（PID 27198），kill 后立即恢复（iter-1 @06:03:59，app PID 451，30s/次）
- autoplay URL 不变：http://192.168.1.109:8899/master.m3u8

### 待目视判定
- DV 徽标 + 颜色（SDR→BT.709 误标已修正；若颜色仍不对，下一步查 app 侧 pixelBuffer/AVPlayerItem 输出通路）
- 亮+色正 → Jellyfin 端到端复验（/jf/pack → 302 → /_jf.d/<id>/master.m3u8 200 → 徽标）
- 不亮 → tier/constraint/array_completeness/VUI 已全部排除，转 app 层（AVFoundation 对 dvh1 信令判定）或 HDMI/EDID 协商
## 第十二节（2026-09-26 11:09）— Jellyfin 打包链默认产出 v2 信号

- 结论（实测）：FFmpeg 8.1.2 重打包丢 dvcC（`/tmp/jfd_flat/ffA/` 为证），不能当
  DV 打包器；Bento4/Shaka/mediafilesegmenter 字节透传 colr 与 hvcC 39 数组。
  任何引擎都需一个信令修正点 → 架构 = 成熟引擎打包 + 两个受单测保护的小补丁。
- `dvpack/jfpack.py` `pack_source`：默认 `drop_param_set_type=39` + 新增
  `drop_colr=True`（默认去 colr），即默认产出与 Apple 官方 P5 参考同构：init 无
  colr、hvcC 仅 32/33/34、单 dvh1 entry、dvcC 完好。显式 drop_colr=False +
  drop_param_set_type=None 可回到 movenc 原样直出对照臂（tests/test_jfpack.py）。
- 已重打 True.Haunting（`_jf/534f5cb2...`，60s/11 段/verify ok/987B init），换进
  `/jf/pack` 缓存（旧包留 `_jf/534f.old.signalled`）；根 `master.m3u8` 指向
  `_jf/534f.../video.m3u8`。电视 192.168.1.100 11:09 冷启动正在拉这条路。
- 待用户目视：DV 徽标是否点亮（v1 只去 colr、v2=去 colr+剥 39，现 Jellyfin 链
  默认走 v2 臂）。v3 备选：ftyp 加 hlsf/isom、dvcC level 6→5、tier 1→0、清 udta。
- 预存问题（非本轮引入）：tests/test_remux.py:168 E2E 漏挂 needs_source，未设
  DVPACK_SOURCE_MKV 必挂。

## 27. 「能不能直接调开源/付费项目里的模块」的库级实测（2026-09-26 11:40）

用户再次提出第十八节的问题，本轮把「库级直调」从设想变成实测。结论：**付费/闭源无模块可调，
开源库级直调在本项目的 probe 通路上是负优化**——不是没试，是试了拿不到关键字段。

### 1. 本地已具备的直调条件（与第十八节的判断一致，供复现）

- PyAV **18.0.0** 已装在 Python 3.14 用户目录：`~/Library/Python/3.14/lib/python/site-packages/av`
  （`av/.dylibs/` 自带 libavformat.62 / libavcodec.62 / libavutil.60 dylib，不依赖 brew ffmpeg）
- `cargo` / `rustc` **1.97.1**（Homebrew）→ libdovi cdylib 可编
- `xcodebuild` 27.0 → tvOS slice 可编（dvplay 侧）

### 2. PyAV in-process 探测实测（对 `tests/fixtures/jf_init_sei.mp4`）

拿得到（`import av` 即用，无 subprocess、无 argv、无 stderr 解析）：

- `codec_context.name` / `codec_context.profile`（`Main 10`）/ `pix_fmt`（`yuv420p10le` → 反推 bit_depth）
- `color_primaries=9` / `color_trc=16` / `colorspace=9` / `color_range=2`——**裸 int，PyAV 18 不导出
  ffmpeg 的枚举名**（`av.video.codeccontext` 里只有 `VideoCodecContext`，无 `VideoColorSpace`），
  要自己建 9→`bt2020`、16→`smpte2084`、9→`bt2020nc`、2→`tv` 的映射表才能对上 ffprobe 的字面量
- `stream.average_rate` / `stream.time_base`（1/16000）/ `container.format.name`
  （`mov,mp4,m4a,3gp,3g2,mj2`）/ `container.duration` / `container.size`
- **http(s) URL 同样可直开**（libavformat 内建协议），jfpack 的远程 probe 也能同源替换

拿不到（致命）：`ffprobe -show_streams` 的 `side_data_list` 里的 **"DOVI configuration record"
（dv_version_major/minor、profile、level、bl_present、el_present、dv_bl_signal_compatibility_id …）
在 PyAV 18 的公开 API 里不存在**。它是 stream 级 `codecpar.coded_side_data`（`AV_PKT_DATA_DOVI_CONF`），
而 PyAV 的 `packet.iter_sidedata()` 只走 packet 级：实测该 fixture 前 6 个 packet 的
`list(pkt.iter_sidedata())` 全是 `[]`，同一文件 ffprobe 却有完整 DOVI 记录。

后果：`probe()` 换 PyAV 会静默丢掉 `DoviRecord` → `mvp_verdict` 直接返回
"无 DOVI configuration record，不是杜比视界片源" → 整条 /jf/pack 链在闸门上被误杀。
**所以库级 probe = 负优化，`ffprobe_json()` 保持 subprocess 不动。**

### 3. 付分类别的硬边界

- **Infuse / MainConcept / Elecard**：闭源 App / SDK，无任何可链接模块，不能拆、不能被调用，
  只能当参照物（同第十八节）。
- **Bitmovin / TheoPlayer / castLabs**：有 tvOS SDK + 试用授权，但它们是"SDK"不是"模块"，
  且播放内核仍走 AVFoundation/VideoToolbox ——换过去不改 init 段信令，对徽标无影响（已验证过）。
- **libdovi cdylib（MIT）**：cargo 可编，但它解的是**位流 RPU**，不解 MP4/MKV 里的
  configuration record；后者项目已在 `dvpack/bmff.py` 用纯 Python 解掉了。为了一个子集
  再加一条 ctypes FFI 边界不划算。

### 4. 本轮未动的部分

probe / remux / pack 三条链全部保持原状（含用户正在真机验收的 v2 信号）。
若哪天真要上 PyAV，唯一划算的切点是 `remux._run()` 的 demux/copy 段，且必须先验证
PyAV 能否写回 `dvh1` sample entry + `-strict unofficial`（muxer `strict` 选项）——
这两件事在本轮都没有验证，不要假设可达。
---
## 第二十九节、「全色域源被按 limited 签」：probe.SHOW_ENTRIES 漏了 color_range（2026-09-26 14:00）

### 1. 症状与伪根因史

用户真机原话：「颜色明显不对，而且电视机上面的 dv 标志没有点亮」「没亮 而且颜色也
不对」。此前几轮把根因先后归给缺 colr 盒、缺 dvcC、P7 而非 P5、dvplay 内联旧
build——都只对了一部分。**真正的根因只有一个字段，且是"加了一半"的那种 bug。**

### 2. 根因：字段加了，但没向 ffprobe 请求

dvpack/probe.py 的 VideoStream.color_range 字段和 _parse_video() 的解析一直都在，
remux.source_full_range() 的口径也改成随源推导了；但 SHOW_ENTRIES 的
"-show_entries" 列表里**没有 color_range**。于是：

1. /tmp/src60.mkv 实测 ffprobe -show_streams 报 color_range=pc（全色域源）；
2. dvpack 自研 probe 从不请求该字段 → SourceInfo.video.color_range == ''；
3. source_full_range() 只认 {'pc','full','jpeg'}，'' 不在内 → **恒返回 0**；
4. hls_command(full_range=0) → -color_range tv；dv_sps_vui_bsf(0) →
   video_full_range_flag=0；apply_dplus(full_range=0) 同口径。

**全色域源被三处一致地签成 limited** → 颜色不对；容器口径与 RPU
bl_video_full_range_flag=true 互相矛盾 → tvOS 不认 DV 输出模式 → 电视不点亮。

> 06:48 那次"点亮"是 P7 源（BL 本就 limited），结论被错误外推到全色域源上，
> 这是误判的主要来源。

### 3. 一行修复

SHOW_ENTRIES 补 color_range。修完实测：probe('/tmp/src60.mkv').video.color_range → 'pc'，
source_full_range() → 1，verify_signalling(video.m3u8, full_range=1) → ok=True 且
video_full_range_flag=1。

### 4. 交叉验证：dvcC 与 ffmpeg 原生输出逐字节相同

用 ffmpeg 原生 dvcC 路径（"-c copy -tag:v dvh1 -strict unofficial"，strict 必须放
**输出侧**，放输入侧无效）产出对照文件，与本包手搓 dvcC 对比：

- dvcC 字节：01000a350000000000000000000000000000000000000000（两侧完全相同）
- 字段：dv_profile=5、dv_level=6、rpu_present=1、bl_present=1、compat_id=0
- colr 盒：两侧均无
- color_range：两侧均 pc

即 dvpack/p5.py 手搓 dvcC 的产物与成熟模块一致，可继续用。

### 5. 与网上方案的对照结论

- **Apple HLS Authoring Specification**（developer.apple.com 的
  hls-authoring-specification-for-apple-devices.md，加 .md 后缀可直接取 markdown）：
  1.9 DV MUST Profile 5 单层 10bit 且 ≤ Level 7；1.10 SHOULD 用 dvh1 而非 dvhe/hev1
  （参数集存 sample description）；9.16 VIDEO-RANGE 非全 SDR 必填。
  **全文没有任何一处要求 DV 走 limited range**——"limited 才对"是伪结论。
- **Dolby《Dolby Vision Streams Within the HLS Format v2.0》**：
  CODECS="dvh1.05.07,..." + VIDEO-RANGE=PQ；编码串格式 [fourCC].[profile].[level]；
  P5 示例即 dvh1.05.07 + VIDEO-RANGE=PQ。**现有 master 列表口径合规，勿改。**
- **jellyfin#16179**（同症状真实事故）：master 里写 CODECS="hvc1..." 导致 Apple TV
  掉 HDR 模式，改 dvh1 即恢复。本链 dvh1.05.06 正确。
- **ffmpeg 原生 dvcC**：见第 4 节。

### 6. 回归测试（4 条新增）

- test_show_entries_requests_color_range——直接守这一行；
- test_show_entries_covers_every_field_parse_video_reads——**结构性防线**：反查
  _parse_video 读到的每个字段是否都在 SHOW_ENTRIES 里被请求（side_data_list ↔
  stream_side_data 走别名表登记），以后再加任何字段都不会被静默漏掉；
- test_color_range_passes_through_to_source_info（pc → 1）；
- test_missing_color_range_is_limited_not_full（空 → 0，不猜全色域）。

pytest：168 passed, 1 failed, 15 skipped。唯一失败仍是缺 DVPACK_SOURCE_MKV 的
test_end_to_end_produces_dvh1_with_config_box，与本次改动无关的 known-fail 基线。

### 7. 线上状态（2026-09-26 14:02）

serve 已在 tmux jfd（端口 8899）重启吃新代码；旧包备份到 /tmp/xa2/oldpkg-tw。
重打包后的线上产物：

- init.mp4：[dvh1] + [hvcC] + [dvcC](profile 5 / level 6 / compat 0)，
  color_range=pc，bt2020nc / smpte2084 / bt2020；
- segment：RPU bl_video_full_range_flag=true、dovi_profile=5、
  disable_residual_flag=true；
- master：CODECS="dvh1.05.06"、VIDEO-RANGE=PQ，响应头 no-store。

### 8. 待办 / 账目

- **真机目视验收未通过前不得标完成**：Apple TV 重播后由用户确认"点亮 + 颜色正常"。
- 若仍不亮：A/B 臂是 drop_colr=False（回到 movenc 写 colr 的对照臂），由
  jfpack.pack_source 的 drop_colr 参数控制。
- Jellyfin App 全链路（JellyfinClient，192.168.1.68:8096，docker，root/135790）尚未开始。


---

## 十九、信令路线穷尽 + 单文件二分实验（2026-09-26 下午，接第十八节）

### 1. serve 真实根目录 = /tmp/jfd_flat（不是 _out/dv_p7_fix）
- 8899 实际由 app 壳 ~/Applications/dvpack-serve.app 起（handoff 十一节所载），根 = /tmp/jfd_flat
- com.dvpack.serve.gt.plist（指向 /tmp/apple_gt/root）是崩的："No module named dvpack"；com.dvpack.serve.plist 已 .disabled
- /_jf.cb、/_jf.new-no39、/_jf.d、/_jf.retired-* 全 404 → 历轮对 _out/dv_p7_fix 的热替换对线上无效

### 2. 信令假设被真机证伪（快照考古）
- /tmp/jfd_flat/_jf/534f.old.signalled（01:46，1056B）：colr(tv)+SPS VUI 9/16/9+tier/non_packed+SEI-39
- /tmp/jfd_flat/_jf/534f.old.signalled-20260926（03:08，987B）：无 colr+VUI 9/16/9+tier/non_packed+剥 SEI-39（变体 C）
- 电视请求时间线：02:26 双 entry 坏包（video0 即断 =「播放失败」）；04:00、04:16 播变体 C-family → 仍不亮 + 颜色不对
- 变体 B（带 colr，_jf.new-no39）从未上线 → colr / VUI 三件套 / tier / non_packed / SEI-39 / dvcC=P5L6 全部真机测过且失败，Infuse 播 Apple 官方 DV5 同机点亮作正对照
- 结论：问题不在容器信令，在码流/RPU 或 HLS 传输层

### 3. 单文件 fMP4 二分实验（本轮上线，待真机）
- /tmp/jfd_flat/single_fmp4.mp4（107MB）= init.mp4 + 11×(moof+mdat) 拼接，码流字节零改动
- URL: http://192.168.1.109:8899/single_fmp4.mp4（HTTP 200 video/mp4）
- 判定树：
  - 单文件**亮+颜色对** → 根因在 HLS 封装（多 moof/styp/sidx/无音频/EXTINF），下一步改封装形态
  - 单文件**不亮** → 根因在码流/RPU（源 P8 语义重签 P5 不自洽 → dovi_tool -m 5 convert 真重写 RPU，或换真 P5 源）


## 二十、Apple 官方参照逐字节对比（2026-09-26 深夜）

- 正对照基线：Apple 官方 adv_dv_atmos 4K DV5 变体（hdrhls798_dolbyvision，dvh1.05.06+ec-3+VIDEO-RANGE=PQ），用户电视实测点亮+颜色正常。本地缓存 /tmp/jfd_flat/apple_ref/。
- **dvcC 与 Apple 官方逐字节相同**（01 00 0a 35 + 20×00，32B）→ dvcC 维度排除。
- **RPU NAL 头前 11 字节与 Apple 完全一致**（7c 01 19 08 09 00 40 61 b6 50 6e）→ RPU profile 同族，「P8 语义重签 P5」假说基本证伪。
- **hvcC SEI-array 假说已被真机证伪**：线上 _jf init（987B）本来就是 3 array（jfpack drop39 生效），04:00/04:16 真机仍不亮。root init（1056B/4 array）是另一废弃变体。exp1_single 与 single_fmp4 等价无增量。
- 每帧结构对比：Apple = AUD+SEI39+slice+RPU(319B)；我们 = SEI39(同族)+4 slice+RPU(227B)。Apple 段: emsg 起/无 styp/sidx/多 trun；我们: styp+sidx+单巨 moof(345 samples)+sample0 带 VPS/SPS/PPS。
- Apple ftyp brands: iso5/isom/iso5/hlsf；我们: iso5/iso6/dvby1/mp41。
- SPS：我们 111B（hevc_metadata VUI 9/16/9+full range），Apple 71B。前 18 字节一致。
- 产物（均已 serv）：single_fmp4.mp4（裸缝合二分刀）；exp2hls/（剥 styp/sidx 的 HLS）。等用户回报。


## 二十一、exp4 产物重建 + emsg/init 全量对比（2026-09-26 下午）

### A. 发现并修复上一轮产物的构建损坏（关键教训）
- 定位读取校验发现：exp4hls/video0.m4s 与 exp4_single.mp4 被上一轮手术写坏——moof size 字段被写成 mdat 的值（0x790897，正确 0x15f4）、mdat type 被清零；且 exp4hls 仅 video0 被处理过，video1-10 的 in-band 参数集根本没剥。若直接真机测试 = 解析失败，浪费轮次。
- 重建方式（free 盒前置，零 bulk I/O，文件总大小不变）：剥除的 N 字节由文件头部 free(N) 盒吃掉，moof 整体后移 N 字节，mdat size 减 N，trun sample0 size 减 N：[free(N)][moof][mdat(size-N)]。仅搬移 ~5.6KB moof，不碰 8MB mdat 负载。
- 重建后全部产物经定位校验通过：11 段 exp4hls + exp4_single + single_fmp4 + exp2hls，盒链 EXACT 且 trun 采样总字节 == mdat size-8。线上 HTTP 探针确认 serve 已吐出 free 盒开头的新字节。

### B. in-band 附加块真实结构（NAL 级，勿再凭猜）
- mdat payload 起始序列：VPS(24B)+SPS(111B)+PPS(9B)+SEI(pt=129,6B)+SEI(buffering_period,22B)+帧自己的 pic_timing SEI(39/pt=1,13B)+IDR slice...
- 源 MKV 首帧自带 tier=MAIN(0x02) 的 VPS/SPS/PPS；BSF 将 tier 改写为 HIGH(0x22)，与 Apple 官方一致（源文件的 frame0 与我方产物字节不完全相同，不能用源 frame0 做字节对齐，必须 NAL 游走）。
- 剥除规则：跳过 nal type {32 VPS,33 SPS,34 PPS} 与 SEI(pt≠1)，停在 pic_timing(pt=1)。切点逐段不同：v0=192B，其余段=191B（PPS 8B）。
- trun flags 两种形态：0xf01（无 first_sample_flags → sample0 size@T+20）与 0xb05（有 first_sample_flags → size@T+24）。必须按 flags 计算偏移；且 'trun' 文本位置 T 比盒起点大 4（T=盒起点+4）。写死偏移会损坏 flags 字段（本轮已踩坑）。

### C. Apple 官方 emsg 真相 = 红鲱鱼（排除）
- emsg(141B)：version1，scheme "https://aomedia.org/emsg/ID3"，value 空，timescale=351，id=2701131776，message_data=ID3v2.4(69B) 内仅一个 TXXX 帧：" *** ID3 timed metadata from 'emsg' @ -- 00:00:00.0 *** "。
- 纯占位调试信息，与 DV 无关。嵌 emsg 方案作废。

### D. init 层全量对比结论（我方 vs Apple 官方 4K DV5）
- 逐字节相同：stsd fourcc=dvh1；hvcC 均为 3 array（VPS/SPS/PPS，均无 SEI array）；hvcC PTL tier=HIGH；dvcC 32B 逐字节相同（01 00 0a 35 + 20×00）；hvcC 内 VPS NAL 逐字节相同（同为 tier-HIGH 版）；pasp 相同。
- 仅装饰性差异：SPS 111B vs Apple 79B（我方多 VUI 9/16/9+full range）；PPS 9B vs 8B；Apple 多 chrm/fiel/第二 pasp；ftyp brands（dvby1 vs hlsf）；elst 有无；mdhd timescale 1000 vs 24000；udta（Lavf vs swre）；Apple 多 clcp 闭字幕轨。
- **结论：init 层无任何 DV 相关差异。若 single_fmp4 仍不亮，重点转向分段层（多 moof 结构/trun 形态）或码流层。**

### E. 线上产物状态（均定位校验通过）
| URL（http://192.168.1.109:8899/） | 内容 |
|---|---|
| single_fmp4.mp4 | 107MB，init+11×(moof+mdat)，无 styp/sidx，sample0 带 in-band 参数（二分刀①） |
| exp2hls/master.m3u8 | HLS 剥 styp/sidx（②） |
| exp4_single.mp4 | ①基础上 sample0 剥 192B in-band（③） |
| exp4hls/master.m3u8 | 剥 styp/sidx + 全 11 段剥 in-band 191/192B（④） |

### F. 判定树（等真机，URL 未变）
- ①亮 → 根因在 HLS 包装层（styp/sidx/分段/m3u8）→ dvpack segmenter 改 Apple parity。
- ①不亮+②亮 → styp/sidx。②不亮+④亮 → in-band 参数集。全不亮 → 分段层（Apple 式多 moof：其 seg0 = emsg+moof(4 trun)+mdat 多段结构）或码流层（dovi_tool -m 5 重写 RPU）。
- 音频仍未打包（video-only）；Jellyfin 走 stream.mkv?static=true 免鉴权直链。

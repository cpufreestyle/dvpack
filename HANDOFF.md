
# HANDOFF v20 (2026-09-27 10:12) — 终局：P8 也不亮。徽标与 profile 无关，收工

## 决定性结果
- **P5**（`_jf`/`_appl`，`dvh1.05.06`，compat_id=0）→ 不亮，颜色正常。
- **P8**（Nuremberg UHD BluRay，`dvh1.08.06`，单层 P8、compat_id=1、RPU 内增强）→ **不亮，颜色正常**。
- **Infuse** 播同一条 P5 → **亮**。
- 极简纯 UIKit app（零 SwiftUI，与 Infuse 同构）播 P5 和 P8 → 都不亮。

⇒ **徽标亮不亮与 profile(P5/P8) 无关，也与 SwiftUI/呈现方式无关。**
  画面颜色始终正确 = DV 解码 + PQ 色调映射正常（这才是 DV 的实际画质收益，已拿到）。
  徽标只是一盏「Apple TV 有没有把该流按 Dolby Vision 输出」的指示灯，
  **只有 Infuse 这类自带真 DV 输出通路的 app 才会点亮**；通用 AVPlayer app 不触发。
  **此问题在 dvplay 侧追不到、也不值得继续追。**

## P8 是怎么测出来的（关键：绕开 dovi_tool）
- 全库 WEB-DL DV 都是单层 P5；**只有 UHD BluRay 原生 P8**（Nuremberg `ab476f22…`、Chefs.Table 等）。
- dovi_tool 对这批内容全解析不了（`Invalid RPU length`），**但不需要它**：P8 源的 RPU 已在流里。
- dvpack `remux` 用 `-c copy` **原样保留 RPU**（源/产物 type-62 NAL 一致），产物 init 的 `dvvC`
  直接就是 P8 信令；**只跳过最后那步 `resign_pack`（转签成 P5）**，手写 master 为 `dvh1.08.06` 即可。
  - P8 源 remux 会产出 2 个 `dvh1` 样本项（stsd entry_count=2），dvpack 的「单 box 守卫」会报
    `多轨 DV 产物不在 MVP 范围`——**但报在转签步，remux 本身已成功**，产物可直接用；
    分片 tfhd 未置 sample-description-index（默认取第 1 项），第 2 项无害。

## 产物现状
- serve 常驻 :8899，master 现指向 `_p8`（P8 流）。切回 P5：写回指向 `_jf`/`_appl` 的 master 即可。
- 想让 dvpack 支持「保留 P8」：在 `remux()` 里对 P8 源跳过 `resign_pack`，并让 playlist 写
  `dvh1.08.xx`（当前 `profile5_target` 恒转 P5）。

## 给用户的一句话结论
想看到 DV 徽标/真正的 Dolby Vision 输出 → **用 Infuse 指向我们的 serve 地址**
（`http://192.168.1.109:8899/master.m3u8`，已验证 Infuse 能点亮我们的流）；
dvplay 只是个排障客户端，测「颜色对不对」够用，测「徽标亮不亮」注定不亮。


# HANDOFF v19 (2026-09-27 10:20) — P8 路线：已拿到 Jellyfin 源，但全库单层 P5 + dovi_tool 解析不了 → P8 做不出来

## 拿源成功（Jellyfin 凭据已验证可用）
- 主机 `192.168.1.68:8096`（Jellyfin 12.1.0，docker）。账号 root / 135790。
- **认证坑**：Jellyfin 只认 `Authorization: MediaBrowser Client="dvplay", Device="Apple TV", DeviceId="<id>", Version="1.0.0"`；
  用 `X-Emby-Authorization` 或不带头 → **HTTP 400 "Error processing request"**（不是 401，别误判成密码错）。
  带 token 的请求：`Authorization: MediaBrowser Token="<token>", Client=...`。
- 静态直链源：`/Videos/<sourceId>/stream.<container>?static=true&api_key=<token>`（curl 可用；ffmpeg 直连该 URL 会
  `No route to host`，须先 curl 下载再本地处理）。Accept-Ranges 支持，~27MB/s。
- dvpack 打包用的那个源：item `534f5cb2ba7f9d500f9f2154e8cff079`（True.Haunting.S01E01.mkv，5.1GB）。

## P8 结论（为什么现在做不出 P8）
- **全库都是单层 P5，没有原生 P7/P8 可直接测**：dvpack check 源 = `profile=5, el_present=False`；
  连 `dvhe` 标签的 mp4（32cfe789）dvcC 也是 `dv_profile=5, el_present_flag=0`。P5 无增强层 → 转不成真双层 P8。
- **dovi_tool 2.3.4 解析不了本库 RPU**：MKV/裸HEVC/fMP4/mp4 全部失败，报 `Invalid RPU length: 16`（或
  `No NALU start codes`）。换 2.0.3、1.6.0 **同样报错** ⇒ 非版本回归，是这批内容 RPU 的问题。
  RPU 以 **suffix-SEI（NAL type 62, rpu_type=1）** 存储，dvcC 声明 `rpu_present=1`。
- **ffmpeg 重封装丢 RPU**：`-c copy` 切出的 clip 经探测**无 DV**——ffmpeg 不认 DV NAL 62/63。整个链路
  只有 dvpack 自己的解析器能正确提 RPU（remux.py `-c copy` 保留 1443 条 type-62 NAL）。
- ⇒ 手头**没有可用的 P8 测试流**，dovi_tool 路径断了。

## 要继续 P8，最快的是「拿到一个现成 P8 样例」
用户直接给一个 DV P8 文件（dvh1.08.xx / dvhe.08）最快：我立刻 serve、极简 app 播放、看徽标。
否则只能继续深挖 dovi_tool/RPU 不兼容（不确定、耗时）。


# HANDOFF v18 (2026-09-27 09:45) — 关键新证据：颜色正确=DV 已正确呈现，徽标纯属「DV 输出信令」层

## 决定性实验 A 的结果（极简纯 UIKit 播放器，零 SwiftUI）
- 构造：`UIApplicationDelegate`+`UIWindowSceneDelegate`，根 VC 直接 `AVPlayerViewController`，
  `AVPlayer(url:)` 播我们 serve 的 master，播完自动重播。**完全没有 SwiftUI/UIHostingController**，
  结构与 Infuse 这类纯 UIKit app 一致。踩坑：pre-scene 的 `UIWindow(frame:)` 在 tvOS 启动即退，
  必须 `UIWindowScene` 生命周期 + Info.plist 补 `UIApplicationSceneManifest`。
- 结果（用户实测）：**「没亮（徽标），但是颜色不错」**。

## 这句话把结论钉死了
- **「颜色不错」= 杜比视界其实已经被正确解码 + 正确做了 PQ/BT.2020 色调映射**，画面就是 DV 的
  正确表现。→ 解码、打包、RPU、range、容器全都对。
- 剩下的「徽标不亮」= **不是解码/画面问题，而是「Apple TV 有没有把这条流按 Dolby Vision 输出
  （DV 输出信令 / HDMI infoframe / RPU 驱动）」的指示灯**。这是输出信令层，与画质无关。
- 且这个极简纯 UIKit app（和 Infuse 同构）**同样不亮**，只给正确颜色 ⇒
  **不是 SwiftUI、不是 dvplay 的代码问题**：在 tvOS 上，一个「通用 AVPlayer app」播这条
  **P5** 流，会正确呈现 DV 颜色但**不触发完整 DV 输出/徽标**；**Infuse 能点亮是因为它有自己的
  真 DV 输出通路**（专用解码/输出路径），不是普通 AVPlayer 能比的。
- ⇒ 结论：dvplay/极简 app 这条线**追不到 DV 输出徽标，也不值得继续追**。画面（DV 正确颜色）
  已经拿到了，徽标只是个指示灯。

## 如果非要把徽标点亮（唯一的打包方向：换 profile）
既然通用 AVPlayer app 对 **P5（单层、无兼容）**只给 PQ 颜色不给 DV 输出，方向是产
**双层 profile** 让系统识别完整 DV 结构并切 DV 输出：
- **P8 `dvhe.08`**（双层 BL+EL、带 IP/PC 兼容，最容易被系统认成 DV 并切输出），或
- 直接以源本来的 **P7 `dvhe.07`**（BL+EL+RPU 合成）伺服/播放（dvpack 现在把它降成了 P5）。
- 可用现成 DV P8 样例先验证「P8 在通用 app 里是否会点亮」——若 P8 亮，问题就定位为
  「P5 单层不触发 DV 输出」，随后让 dvpack 改产 P8 即可。

# HANDOFF v17 (2026-09-27 09:30) — 定位收敛：文件/电视都没问题，问题锁定在 dvplay 的 app 上下文
# HANDOFF v17 (2026-09-27 09:30) — 定位收敛：文件/电视都没问题，问题锁定在 dvplay 的 app 上下文

## 已用实验排除（每条都有实测支撑，别再重复走）
1. **不是打包问题**：
   - A（pc 全色域，dvcC 与 Apple 参考逐字节一致）不亮；D（tv 限定范围）不亮。
   - **Apple 官方 DV 参考流本身**经 dvplay 也不亮（该参考原封是坏的：video.m3u8 的
     MAP 指向 0 字节 fileSequence0.mp4，已修 MAP→init785.mp4、组装到 /tmp/jfd_flat/_appl
     并验证全 200 才测）。
   - 逐 box 比对 `_jf` init vs Apple 官方 init：样本描述符等价（都 `dvh1+hvcC+dvcC(P5,
     compat_id=0)+pasp`，**两边都不带 colr**，dvcC 逐字节一致）。range/colr/compat_id/
     mdcv·clli 等包装层差异全部排除。
2. **不是电视/HDMI/设置**：用户确认——同一台 Apple TV、同一根线，**Infuse 能点亮**。
3. **不是文件在别处不行**：我们的 serve 地址（Apple 参考流 `http://192.168.1.109:8899/_appl/
   video.m3u8`）在外部播放器里**能点亮**。
   ⇒ 结论：**文件 100% 没问题、电视链路没问题；问题 100% 在 dvplay 这个 app 的呈现上下文。**

## 已尝试且都【没修好】的 dvplay 客户端改动（真实 bug，但都不是根因）
- 起播时机：去掉 `PlayerModel.play()` 里的 `player.play()`，改由
  `DVPlayerViewController.viewDidAppear`（上屏后）才起播 + 0.4s 兜底。→ 仍不亮。
- 呈现嵌套：弃用 SwiftUI `.fullScreenCover`（PlayerVC 嵌在 UIHostingController 里），
  改为从 key window 顶层 VC 直接 `present(DVPlayerViewController)`。→ 仍不亮。
- 独立窗口：再改为播放器独占一个全屏 `UIWindow`（脱离 SwiftUI scene，对齐 Infuse 做法）。→ 仍不亮。
- 确认过这些改动真的编进包里了（装机 binary 含新字符串/DVPlayerViewController 符号），
  所以「没亮」是真阴性，不是旧包。

## 现在卡在哪 / 下一步（决定性实验，二选一）
无法在本机直接读到「系统为什么没切 DV 显示模式」：tvOS 上
- `devicectl ... launch --console` 收 os_log 不支持（稳定报 CoreDeviceError 10002）；
- `devicectl device copy from --domain-type appDataContainer` 拉 app Documents 也拉不到。
（dvplay 里已加运行时探针 dumpDiagnostics，把格式描述里的 DV 扩展写到
 Documents/dvdiag.txt + os_log，但目前拉不出来。）

**决定性实验 A（推荐，最干净）**：在同一个 dvplay 工程里做一个「极简 UIKit 播放器」构建
——完全绕开 SwiftUI（UIApplicationDelegate + 根 VC 直接就是 AVPlayerViewController，
AVPlayer(url:) 播我们 serve 的 URL）。
  - 若它**点亮** ⇒ 确实是 SwiftUI 宿主/焦点的问题，dvplay 把主界面降级/把播放完全交给
    UIKit 即可；
  - 若它**也不亮** ⇒ 这不是 dvplay 的锅，而是「通用 AVPlayer app 播这条 P5 流，系统就不
    切 DV 显示模式」，Infuse 是靠它自有的 DV 通路点亮的。此时 dvplay 追不到、也不值得追，
    实际观影直接用 Infuse 指向我们的 serve 地址（已验证能点亮）。

**决定性实验 B（更省事，直接给用户价值）**：dvplay 不追这个徽标了。核心目标（把 Jellyfin
的 DV 重封成合规 dvh1 P5 包、用 HLS 伺服、能在真机播放）已经达成，且我们的 DV 流在
Infuse 里能点亮=用户能正常看到 DV。dvplay 只是个排障客户端，徽标在它里不亮不影响产物正确性。

## 其它现场信息
- serve 常驻 :8899（tmux 会话 jfd，LaunchAgent com.dvpack.serve），当前 master.m3u8 指向
  `_appl`（Apple 官方参考流）。
- 装机签名坑（iCloud FileProvider xattr → codesign detritus）见 v15 的完整命令。
- 循环播放已验证可用（video0 连拉两轮、每轮到 video10 ENDLIST 自动重播，budget 200）。

## 已完成的判别（把「打包」彻底排除）
- 播放 A（pc，全色域 dvcC 与 Apple 参考逐字节一致）→ **不亮**。
- 播放 D（tv 限定范围）→ **不亮**。
- 播放 **Apple 官方 DV 参考流本身**（`_out/apple_ref`，`dvh1.05.03`，
  init785 + 3 段）经 dvplay → **也不亮**。
  - 该参考原封是坏的（`video.m3u8` 的 MAP 指向 0 字节的 `fileSequence0.mp4`），
    已修好 MAP→`init785.mp4`、组装到 `/tmp/jfd_flat/_appl` 并验证全 200 才测。
- 逐 box 比对 `_jf` init vs Apple 官方 init：样本描述符结构等价
  （都是 `dvh1 + hvcC + dvcC(P5, compat_id=0) + pasp`，**两边都不带 colr**，
  dvcC 逐字节一致）。range 位 / colr / compat_id / mdcv·clli 等包装层差异已排除。
- 客户端代码核对：dvplay 走系统 `AVPlayerViewController` 全屏（正确通路），
  `AVPlayerItem(url:)`→`AVPlayer` 直喂，未挂 video output 探针
  （`probeEnabled=false`），无 `preferredMaximumResolution`/`videoComposition`/
  peakBitRate 等会逼原生直通 SDR 化的干预——写法是规范的。

**结论**：连 Apple 官方 DV 流经 dvplay 都不点亮徽标 ⇒ 根因不在我们的 DV 打包、
也不在 dvplay 的播放写法，而在**这台 Apple TV/电视链路的 DV 输出/设置层**
（或判定「DV 有没有被系统真正启用」的方式）。继续改包不会有任何变化。

## 下一步（唯一剩下的分水岭，都在用户侧的电视/设置上）
1. **基线对照**：在同一台 Apple TV、同一根 HDMI、同一个杜比视界片源，
   用**已知能点亮的 app**（Apple TV 自带/Netflix/Infuse）播一次。
   - 那里亮、只有 dvplay 不亮 → 锁定 dvplay/流在该 app 下的差异。
   - 哪里都不亮 → 是设置/链路层，改包永远无效。
2. **Apple TV 设置** → 视频/电视 → 「匹配动态范围」(Match Dynamic Range)
   设为允许 HDR/DV（不是仅 SDR）。
3. **电视侧**：对应 HDMI 口的「杜比视界/Dolby Vision」输入模式为开。
   （硬件没问题 ≠ 设置没问题；这是设置不是硬件。）

---

# HANDOFF v15 (2026-09-27 08:22) — 循环播放版已装、验证在循环；签名坑固化

## dvplay 循环播放版：已装机 + 实测在循环
- 装的是含 `bindEndOfItem` 自动重播（budget 200 ≈ 2.7h 整夜挂）的 Release build。
- 实测：`video0` 于 08:20:33、08:21:33 各拉一次、每轮都跑到 `video10`（ENDLIST）→
  到点重换 item 再放，**无缝循环成立**。旧 21:00 build 只跑到 `video8` 且不复播。
- 现 serve 的 `master.m3u8` 临时指向 `_testtv`（tv 限定范围 D 探针），所以循环放的是 D。
  要看 A（全色域 pc）对比：`cp /tmp/jfd_flat/master.m3u8.bak.a /tmp/jfd_flat/master.m3u8`
  再冷启 dvplay（先 `devicectl ... terminate --pid`，`--bundle-id` 无效）。

## 真机签名坑（这次咬了 4 次构建）—— 下次直接用这段
`~/Documents` 在 iCloud/FileProvider 盘里，会往 .app 里盖 `com.apple.provenance` /
`com.apple.FinderInfo` / `com.apple.fileprovider.fpfs#P`，xcodebuild 的 CodeSign 步
报 **"resource fork, Finder information, or similar detritus not allowed"** 直接挂。
且首次失败发生在 **link 之后、sign 之前**，留下一个 unsigned 的 .app，第二次增量构建
会误判"已是最新"跳过 sign → 假 BUILD SUCCEEDED 但 `codesign -dv` 显示未签名 →
装机报 `0xe800801c No code signature found`。
正确姿势（非交互 shell 里）：
1. `security unlock-keychain -p '' ~/Library/Keychains/login.keychain-db`（钥匙串锁着也会 sign 挂）
2. 改了 PlayerModel.swift 后要清增量产物，别信增量：
   `rm -rf build_dd/Build/Products/Release-appletvos/dvplay.app build_dd/Build/Intermediates.noindex/dvplay.build/Release-appletvos`
3. 构建到 sign 步会挂（detritus）—— 无所谓，产物已 link 好；手工去 xattr + 签：
   ```sh
   APP=build_dd/Build/Products/Release-appletvos/dvplay.app
   XCEN=build_dd/Build/Intermediates.noindex/dvplay.build/Release-appletvos/dvplay.build/dvplay.app.xcent
   xattr -cr "$APP"
   codesign --force --sign 86B55AD39E19F176D597B0E47983D80F6A177B47 \
     --entitlements "$XCEN" --timestamp=none --generate-entitlement-der "$APP"
   codesign -dv "$APP"   # 确认 Signature size 非 0
   ```
4. `xcrun devicectl device install app --device 00008110-0002149A0A9B801E "$APP"`

# HANDOFF v14 (2026-09-26 23:05) — 本地全绿、serve 已恢复、只差真机一眼

## 本地已完成并验证（本次会话）
1. **serve 恢复**：tmux 会话曾整体死掉，已重启在 :8899。恢复命令固化在
   v13 末行。四路 URL 实测全 200：`/master.m3u8`、`/_testtv/<id>/video.m3u8`(D)、
   `/_jf/<id>/video.m3u8`(A 本体)、`/limited_tv.mkv`(C)。
   - 线上 `master.m3u8` → `_jf` 包，init `color_range=pc`（按源全色域）
   - 变体 D `_testtv` init `color_range=tv` + DOVI P5/L6 完好（仅 SPS/容器翻 tv，RPU 仍 full=不一致，属探针包）
2. **测试回归修复并单独提交 `11001f1`**：`test_end_to_end_produces_dvh1_with_config_box`
   漏 `@needs_source`，未设 `DVPACK_SOURCE_MKV` 时 `SRC=Path('.')` → ffprobe
   `Is a directory`。补守卫后 **168 passed, 16 skipped** 全绿。
   - ⚠️ 该提交连带提交了 test_remux.py 的既有 WIP；**HEAD 单独 checkout 不保证自洽**
     （部分用例引用未提交的 remux.py range 逻辑）。以工作树为准。
3. **打包链路在当前代码上复验**：`check → pack → verify` 打到新目录，产物
   `dvh1+dvcC` / `dvh1.05.06` + `VIDEO-RANGE=PQ` / RPU 自洽 / P5 保持。合成 P5 源
   自然签成 `color_range=tv`（与本地板 Apple 参考一致）。

## 未提交的真机待决 WIP（不轻动）
`dvpack/dplus.py`、`dvpack/remux.py`(source_full_range/dv_signalling_bsf 按源推导)、
`dvpack/relay.py`、`dvpack/serve.py`、`scripts/p7_to_p5.py` 等——range 位永久修法
正等真机结果定型，故未入库。

## 播放/循环（2026-09-27）
- 给"主卧"Apple TV 装了含 200× 自动重播的 dvplay（PlayerModel.bindEndOfItem）。
  实测循环稳：`video0` 拉取 08:17:58→08:18:59→08:20:31→08:21:32，约每 60–90s 一圈。
- **构建坑（重要）**：build_dd 在 `~/Documents/...` 下被 iCloud/file-provider 打
  `com.apple.fileprovider.fpfs#P`+`com.apple.FinderInfo` xattr → codesign 报
  "resource fork, Finder information, or similar detritus not allowed"。
  **解法：`-derivedDataPath /tmp/dvplay_dd`（脱离同步目录）**，一次 BUILD SUCCEEDED。
  装法：`xcrun devicectl device install app --device <udid> /tmp/dvplay_dd/Build/Products/Release-appletvos/dvplay.app`。
- 冷启动：先 `devicectl device process terminate --pid <pid>`（须 pid，bundle-id 无效）再 launch。
- **当前 master.m3u8 指向 `_testtv`（tv 限定范围候选）**；原版在 `master.m3u8.bak.a`（`_jf` pc）。
  切回 A：`cp master.m3u8.bak.a master.m3u8`。设备 UDID：00008110-0002149A0A9B801E。

## 唯一验收项 = 一眼物理屏幕（本地判不了）
serve 起后在 dvplay 依次填：
- A（按源全色域，线上）: `http://192.168.1.109:8899/master.m3u8`
- D（限定范围 tv 探针）: `http://192.168.1.109:8899/_testtv/534f5cb2ba7f9d500f9f2154e8cff079/video.m3u8`
- C（限定 mkv）: `http://192.168.1.109:8899/limited_tv.mkv`

判读分支：
- D/C 亮、A 不亮 → 坐实 range=0 是根因；永久修法：DV 输出**三处一致**强制 limited
  （容器 + SPS VUI `video_full_range_flag=0` + RPU `bl_video_full_range_flag=0`，
  RPU 用 `dovi_tool editor`），线上 master 切 limited。**注意** D 现状只翻 SPS 没翻 RPU，
  若 D 亮要按"三处一致"重打成自洽包再定稿。
- A 亮 → 按源推导即正解，现有默认代码即终态，收尾。
- 都不亮 → 转 compat_id / 补 chrm·mdcv·clli / 逐帧 RPU 重写（`dovi_tool editor`）。

# HANDOFF v13 (2026-09-26 20:15) — 手术版 C/D 已上线待真机判定
- 权威工具：**ffmpeg trace_headers bsf** 可用：`ffmpeg -v verbose -i X -c copy -bsf:v trace_headers -f hevc -y /dev/null 2> log` → log 里有完整 SPS 语法树。位偏移含 NAL 头16位且基于去 EPB 域；RBSP bit = tracebit - 16。bit=653(video_full_range_flag) → RBSP637 → unescaped byte79 bit5 ^0x04；tier(26→byte1 bit2 ^0x20)；non_packed(66→byte6 bit2 ^0x20)。
- mkv 结构真相：DOVI 配置在 EBML 元素 **0x41ED**（24B: 01 00 0a 35 + 20×00，即 dvcC payload）；CodecPrivate 是 23B 残桩（不可信）；**SPS 内嵌在每个关键帧**（15 份 111B，模式 42 01 01 02 20）。CodecID=V_MPEGH/ISO/HEVC + 轨2=A_EAC3。
- ffmpeg 重 mux 必丢 DOVI（-tag:v dvh1 被 matroska 拒"Tag dvh1 incompatible with output codec id 173"；bsf→mp4 丢 dvcC）→ **字节手术是唯一保 DOVI 路径**。
- **C已完成**：/tmp/jfd_flat/limited_tv.mkv = baseline_ff.mkv 三比特手术。验证：raw 抽流 trace tier=1/nonpacked=1/range=0；dvpack check DOVI=P5/L6 可打包；serve 200 全量。
- **D已完成**：/tmp/jfd_flat/_testtv/534f5cb2ba7f9d500f9f2154e8cff079/ = jf 产品包副本，init.mp4 + **每个 videoN.m4s 的 in-band SPS**（ffmpeg hls 每段都写 in-band SPS！模式 42 01 01 22 20 00 00 03 00 b0）同部位翻 range bit。验证：init ffprobe pc→tv；serve m3u8 200；trace 流 range=0。
- serve 事故与恢复：turn_aborted 时 tmux 整个死过。恢复命令：`cd work/dvpack && tmux new-session -d -s jfd -x 200 -y 50 "/Users/a1-6/.local/share/uv/python/cpython-3.11-macos-aarch64-none/bin/python3.11 -m dvpack serve /tmp/jfd_flat --port 8899 2>&1 | tee -a /tmp/jfd-live.log"`。另注：~/Applications/dvpack-serve.app 指的是 _out/dv_p7_fix 目录，别再指错。
- 已删无效文件：limited_ff.mkv/limited_ff2.mkv/limited_sps_tv.mkv(0B)/master.*.m3u8/exp*/single_fmp4。
- 真机待测（async 已发，电视=192.168.100 访问 192.168.1.109:8899）：A=/baseline_ff.mkv、C=/limited_tv.mkv、D=/_testtv/534f5cb2ba7f9d500f9f2154e8cff079/video.m3u8
- 下一步按序：①用户回报 → C/D 亮 = 根因坐实；②永久修 dvpack/dplus.py dv_signalling_bdf 的 video_full_range_flag（DV 输出强制 0）+ 若需可加 tier/non_packed 位；③重打 pack 后验证 init SPS range=tv + DOVI 完好，master.m3u8 上线；④音轨（源有 A_EAC3 轨，live 包 VIDEO_ONLY 需补映射）；⑤Jellyfin（192.168.1.68:8096 root/135790 docker）端到端；⑥update_goal complete（如需目视）。

---

# HANDOFF v21 (2026-09-27 12:00) — dvplay 电视端「有声音、画面全黑」未解决

> 本节记录**当前唯一未解决问题**的完整现状与下一步。v1–v20 的 DV 徽标结论仍然有效
> （结论：P5/P8 都不点亮 DV 徽标，颜色始终正确，与 profile 无关），本节不推翻它。

## 症状
电视（Apple TV，UDID `00008110-0002149A0A9B801E`，IP `192.168.1.100` = zhuwo.local）上
跑 dvplay 播放时：**有声音、画面全黑**。早前症状是「点了播放无反应」，已通过下述
play() 修复解决；现暴露出更深一层。

## 已确认的事实（都有日志/工具佐证，不要重复排查）
1. **电视自 11:34:54 之后再未向 serve 发过任何请求**。
   - serve 日志 `/tmp/jfd-live.log` 最后一条电视请求 = 11:34:54 的 `_jf/18a33.../video10.m4s`。
   - 之后只有 127.0.0.1（我 curl）和 192.168.1.109（本机 LAN IP）。
   - 日志本身是活的（mtime 随 curl 更新），serve 进程在 LISTEN 8899，curl LAN IP 返回 200。
2. **电视网络正常**：`ping 192.168.1.100` 0% 丢包，ARP 有 MAC。
3. **当前 master 指向的 A 包是纯视频、无音轨**（ffprobe：仅 1 条 hevc）。
   ⇒ **听到的声音不可能来自这条流**。
4. **Jellyfin Sessions API**：存在 client=dvplay / device=Apple TV 的会话，
   但 `NowPlayingItem = None`、`PlayState.ErrorCode = None`
   ⇒ app 也没在从 Jellyfin 拉流。
   （查询需 `Authorization: MediaBrowser Token="..."` 头，**不是** `X-Emby-Authorization`；
    用错头会 400。）
5. Jellyfin 凭据：host `192.168.1.68:8096`，用户 `root` / `135790`；
   token 缓存在 `/tmp/jf_token.txt`。

## 本轮已修的真实 bug（均已提交，dvplay commit `11e6c9b`）
1. **冷启动自动起播**：`onAppear` 里直接 `play()` → `fullScreenCover` 盖住整页，
   用户连输入框都点不到。这才是当初「无法选择用户名」的真正根因。
2. **`play()` 不再 await `inspector.inspect()`**：原先先等两次网络诊断（各 10s 超时）
   才建 player，请求一慢就 = 「黑屏 + 零分片请求」。改为先起播、复判并行。
3. **`stop()` 现在 `cancellables.removeAll()`**：旧 item 的 status / timeControlStatus /
   DidPlayToEndTime 订阅此前一直残留，切流后仍写状态、仍触发自动重播。
4. 新增 `PlayerModel.currentURLText`，界面**橙色大字**显示「在播：<地址>」。

## 尝试过但**无效**的方向（别再走）
- **DV 徽标方向已彻底查清并放弃**：P5(`dvh1.05.06`)、P8(`dvh1.08.06`)、
  Apple 官方参考流、经 dvplay 全都不亮徽标，但**颜色始终正确**（DV 解码 + PQ 色调映射正常）。
  Infuse 播同一条流**能点亮**。极简纯 UIKit 播放器（与 Infuse 同构、零 SwiftUI）
  同样只给正确颜色、不亮徽标。⇒ 通用 AVPlayer app 在 tvOS 上播 P5/P8 都不触发
  DV 输出信令；只有 Infuse 这类自带真 DV 输出通路的 app 才点亮。
  **要看到 DV 徽标就用 Infuse 指向 `http://192.168.1.109:8899/master.m3u8`。**
- **键盘/输入框方向已放弃**（用户明示「键盘不用管」）：曾把 SwiftUI TextField 换成
  UITextField 的 `UIViewRepresentable`（`TVTextField`），反复失败——
  焦点令牌模型、手势识别器、`touchesEnded` 抢焦点、密码 AutoFill 规避，四轮全不行。
  最终**已整体回退**到 SwiftUI 原生 `TextField`/`SecureField` + `@FocusState`，
  并移除外层 `ScrollView`（tvOS 上 ScrollView 内输入框常拿不到焦点）。
  `TVTextField.swift` 及其 pbxproj 引用**已删除**，勿复活。

## 环境要点（踩过的坑）
- **构建必须用 `-derivedDataPath /tmp/dvp_dd`**。用仓库内 `build_dd` 必定 codesign 失败：
  `resource fork, Finder information, or similar detritus not allowed`
  —— FileProvider 同步卷给 `.app` 打 `com.apple.FinderInfo` xattr，**与代码无关**。
  已加 `.gitignore` 忽略 `build_dd/`。
- 安装：`xcrun devicectl device install app --device <udid> /tmp/dvp_dd/Build/Products/Release-appletvos/dvplay.app`
- 重启：**须先 terminate 且用 pid**（bundle-id 无效）再 launch：
  `devicectl device process terminate --device <udid> --pid <pid>`。
- **三条本机诊断通道在该 tvOS 上全部不可用**：
  - `devicectl --console` → `CoreDeviceError 10002 (0x2712)` / NSPOSIXErrorDomain 22
  - `devicectl copy from` → 7000（Documents、Preferences 都拉不到）
  - `devicectl device screenshot` → **不接受 `--device`**
  ⇒ 拿不到 app 内部状态/os_log/屏幕截图。**这是本次卡死的根本原因。**
  `log stream --device-udid` 同样未验证可用，值得优先试。
- serve 恢复命令（tmux 死过，需重建）：
  `cd work/dvpack && tmux new-session -d -s jfd -x 200 -y 50 "/Users/a1-6/.local/share/uv/python/cpython-3.11-macos-aarch64-none/bin/python3.11 -m dvpack serve /tmp/jfd_flat --port 8899 2>&1 | tee -a /tmp/jfd-live.log"`

## 流产物现状（`/tmp/jfd_flat`）
- `master.m3u8` → **DV A 包** `_jf/534f5cb2ba7f9d500f9f2154e8cff079/video.m3u8`
  （`dvh1.05.06` 4K24，纯视频无音轨；**11:34 验证过电视能正常拉流播放**）。
  备份：同目录 `master.m3u8.bak.a`（同内容）。
- `_sdr/`：**SDR 对照流**（HEVC Main10 + E-AC3，20s，ffmpeg `-c copy` 生成）。
  本轮新造，用途是区分「app 有问题」vs「DV 流有问题」——**尚未在电视上验过**。
  master 目前指回 DV 包，未用它。
- `_fix50/`：4K50 DV（`dvh1.05.09`，video+aac 双轨，结构已验完整），**从未真机验证**。
- `_p8/`：单层 P8（`dvh1.08.06`），真机播过，颜色正常、徽标不亮。

## 下一步（按此顺序）
1. **先确认电视到底在播什么** —— 界面上「在播：」那行橙色大字会直接显示完整地址。
   - 显示 Jellyfin 直连地址 → 走原片链路，问题在 Jellyfin/网络侧
   - 显示旧地址（`_p8`/`_jf/18a33...`）→ 输入框残留，清掉重输
   - **这行不出现** → app 压根没起播，按钮未生效
2. **换诊断信道**：`log stream --device-udid 00008110-0002149A0A9B801E --predicate
   'subsystem == "com.cpufreestyle.dvplay"'`（app 用 os.Logger 写到
   subsystem=bundle id、category="probe"），看 `item status=` / `timeControl=` / `item failed err=`。
   这是最可能拿到真相的一条路。
3. **跑 SDR 对照**：`printf '#EXTM3U\n#EXT-X-VERSION:7\n#EXT-X-INDEPENDENT-SEGMENTS\n#EXT-X-STREAM-INF:BANDWIDTH=17058152,CODECS="hvc1.2.4.L153.B0",RESOLUTION=3840x2160,FRAME-RATE=24\n_sdr/video.m3u8\n' > /tmp/jfd_flat/master.m3u8`
   然后按播放。**出画面 = app 健康，问题限于 DV 流**；仍黑 = app 自身问题。
   验完务必 `cp master.m3u8.bak.a master.m3u8` 切回。
4. 若确认 app 侧问题：注意 `AVPlayerViewController` 是 SwiftUI `fullScreenCover` 呈现的，
   而 v20 已证明「独立 `UIWindow` 顶层 present」也无效，两条呈现路径都试过了。
   下一步可试 `AVPlayerViewController` 的 `entersFullScreenWhenPlaybackBegins` 等属性、
   或改用 `AVQueuePlayer` + 直接喂 item。
5. **别再碰 DV 徽标**（结论已定，见上）。要看徽标就用 Infuse。


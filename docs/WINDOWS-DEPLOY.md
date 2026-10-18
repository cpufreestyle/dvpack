# dvpack Windows 部署手册（192.168.1.68 · Jellyfin 宿主）

目标：把 dvpack 从 Mac（IP 会漂移、磁盘只剩 30Gi）整体搬到跑 Jellyfin 的
Windows 机，Apple TV 上的 dvplay 改指这台机，片库打包/投喂/字幕全在本地闭环。

## 0. 前提

- 机器：192.168.1.68，Windows 10/11 x64，本地盘空闲 >30GiB（serve 磁盘闸 24GiB）。
- 网络：与 Apple TV（192.168.1.100）同局域网；建议路由器给 .68 做 DHCP 保留——
  Windows 无 Bonjour，dvplay 里不能填 dvpack.local，只能填 IP，IP 漂了就全断。
- 电源：关睡眠/休眠（ATV 直连这台机放片，睡了就断）。脚本 -DisableSleep 可代劳。
- Jellyfin：保持常开（这台机原来跑 Docker Desktop + Jellyfin；若用宿主方式跑，8096 要通）。

## 1. 拷仓库

从 Mac 整目录拷贝到 C:\dvpack（路径别带空格、尽量短）：

    \\<mac-ip>\AI Shared\repo\dvpack  ->  C:\dvpack

tools/ 虽在 .gitignore 里，但目录拷贝照搬——三个 Windows 二进制
（tools\bin\dovi_tool.exe、tools\ffmpeg\bin\ffmpeg.exe、tools\ffmpeg\bin\ffprobe.exe）
已随包暂存好，deploy 脚本检测到就跳过下载；缺了才会回 GitHub 拉。

**不要用 git clone 到 .68**：tools/ 整个目录都在 .gitignore 里——clone 出来三个
.exe 尚能靠 deploy.ps1 回 GitHub 补下，但 tools\subfetch.py 也会丢，在线字幕
（OpenSubtitles → SubHD → SubtitleCat 整条 L3 腿）直接静默失效：包内兜底只做
key 解析等价、不抓字幕（dvpack/subsource.py 见 script 不在即返回）。整目录拷贝
是唯一把 tools/ 带全的方式。

## 2. 跑部署脚本

管理员 PowerShell（右键开始菜单 -> Windows PowerShell (管理员)）：

    cd C:\dvpack
    powershell -ExecutionPolicy Bypass -File scripts\deploy.ps1 -JfBase http://192.168.1.68:8096 -DisableSleep

脚本依次做：

1. 装 uv（已装则跳过）-> 建 .venv -> uv pip install -e .（零第三方运行时依赖）；
2. 补齐 Windows 二进制（缺失才下载：dovi_tool 2.3.4、BtbN FFmpeg win64-gpl 全静态版）；
3. 写 %USERPROFILE%\.dvpack\config.json 并落入 OpenSubtitles Api-Key；
4. 建缓存目录 C:\dvpack\cache，放行防火墙入站 tcp/8899；
5. 生成启动器 run-serve.ps1 + 开机自启计划任务 dvpack-serve（登录即起）；
6. 立即起一份 serve 并自检（http://127.0.0.1:8899/ 回 404 = 正常活着）；
7. 打印本机所有 IPv4——这就是填进 dvplay 的地址。

常用参数：

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| -RepoRoot | 脚本上级目录 | 仓库没放 C:\dvpack 时指定 |
| -CacheRoot | C:\dvpack\cache | HLS 缓存目录，大盘可改 |
| -Port | 8899 | 与 dvplay 里填的端口一致 |
| -JfBase | 空 | Jellyfin 地址，只影响 prewarm 预热 |
| -JfPassword | 空 | 明文写进 run-serve.ps1，共享机器慎用 |
| -NoAutostart | 关 | 不建开机自启 |
| -DisableSleep | 关 | 关睡眠/休眠/息屏计时 |

## 3. Apple TV 侧

1. dvplay 里地址改成 http://192.168.1.68:8899（脚本最后打印的那个 IP）；
2. 进片库，点一部 Dolby Vision 片——10 秒内起播、电视亮 DV 徽标即通。

## 4. 预热片库（可选但建议）

serve 的 /jf/pack 是点播打包（点一部打一部）；想让整库提前打好，跑 prewarm：

    C:\dvpack\.venv\Scripts\dvpack.exe prewarm C:\dvpack\cache --base http://192.168.1.68:8096 --user root --pw <密码> --dry-run

--dry-run 只看计划不动手；去掉即真打。并发 --jobs 2（硬顶内）可提速。
打包产物落 C:\dvpack\cache\_jf\<item>\，serve 磁盘闸 24GiB，低于自动 LRU 淘汰。

## 5. 日常运维

- 看日志：C:\dvpack\logs\serve.log（启动器滚动追加）；
- 重启服务：任务计划程序里跑 dvpack-serve，或手动 powershell -File C:\dvpack\run-serve.ps1；
- 改端口/缓存目录：编辑 run-serve.ps1 一行即可，计划任务不用重建；
- 卸载自启：schtasks /Delete /TN dvpack-serve /F；防火墙规则同名 dvpack tcp 8899。

## 6. 迁移成功后 Mac 侧收尾

- dvplay 不再指向 Mac 后，Mac 上 serve/prewarm 可停；
- _out/dv_p7_fix/_jf（约 119GiB）与 _out/.../_subs 字幕缓存可清——
  先确认 ATV 在新服务上连放三部无误再删；
- ~/.dvpack/config.json（OpenSubtitles key）两边都有，Mac 侧留着无害。

## 7. 已验证 / 未验证

- 已验证（Mac 侧）：全量 pytest 786 passed / 19 skipped；三个 Windows 二进制
  均为 PE32+ x86-64（dovi_tool 2.3.4 与 Mac 同版本；ffmpeg 为 BtbN 全静态版）。
- 未验证：deploy.ps1 未在真机 Windows 跑过（编写机无 PowerShell）——
  首次真机运行若报错，把报错行号发回来，按行修。已知 Windows 适配点
  （renamex_np ENOSYS 回落 / kernel32 pid 探测 / 文件锁删除降级 / TCC 中继旁路 /
  curl+tar 回退 PATH）均有单测锚定，Mac 侧回归零失败。

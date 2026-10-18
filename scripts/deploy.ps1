#Requires -Version 5.1
<#
.SYNOPSIS
  dvpack Windows 部署：uv 装环境 → Windows 二进制就位 → 写配置 → 防火墙 →
  开机自启 → 起一份 serve 并自检。在跑 Jellyfin 的那台 Windows 机上运行。
.DESCRIPTION
  以管理员 PowerShell 跑（防火墙规则要管理员；非管理员也能跑，跳过防火墙
  并给出手动命令）：
      powershell -ExecutionPolicy Bypass -File scripts\deploy.ps1

  默认假设仓库已整目录拷到 C:\dvpack（脚本按自身位置推断仓库根，也可
  -RepoRoot 指定）。tools\ 下的三个 Windows 二进制（dovi_tool.exe /
  ffmpeg.exe / ffprobe.exe）已随拷贝带过来时自动跳过下载；缺失才回 GitHub
  拉（dovi_tool 2.3.4、BtbN win64-gpl 全静态版）。

  路径别带空格：schtasks 计划任务的 /TR 参数 quoting 很脆，短路径最稳。
.PARAMETER RepoRoot
  仓库根（含 pyproject.toml）。默认脚本上级目录。
.PARAMETER CacheRoot
  HLS 缓存目录，serve 的 root（_jf 打包产物落这）。默认 C:\dvpack\cache；
  所在盘空闲建议 >30GiB（serve 磁盘闸 24GiB，低于就拒新打包）。
.PARAMETER Port
  端口，默认 8899。Apple TV 上 dvplay 填 http://<本机IP>:8899。
.PARAMETER JfBase
  Jellyfin 地址，如 http://192.168.1.68:8096。只影响 prewarm 批量预热。
.PARAMETER JfPassword
  Jellyfin 登录密码（--user 默认 root；prewarm 手动跑时可覆盖）。
  注意：会以明文写进 run-serve.ps1，共享机器慎用。
.PARAMETER OsKey
  OpenSubtitles Api-Key，默认填已申请的那个。
.PARAMETER NoAutostart
  不建开机自启计划任务（只起当前这份 serve）。
.PARAMETER DisableSleep
  顺手关睡眠/休眠计时（ATV 要直连这台机放片，睡了就断）。
.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts\deploy.ps1 -JfBase http://192.168.1.68:8096 -DisableSleep
#>
[CmdletBinding()]
param(
    [string] $RepoRoot = (Split-Path -Parent $PSScriptRoot),
    [string] $CacheRoot = "C:\dvpack\cache",
    [int]    $Port = 8899,
    [string] $JfBase = "",
    [string] $JfPassword = "",
    [string] $OsKey = "4kQhoCbBVA1b2Aizgi1GpDQKb9V7qR9U",
    [switch] $NoAutostart,
    [switch] $DisableSleep
)

$ErrorActionPreference = "Stop"
$DoviVersion = "2.3.4"

function Info([string] $m) { Write-Host "==> $m" -ForegroundColor Cyan }
function Ok([string] $m) { Write-Host "  [ok] $m" -ForegroundColor Green }
function Warn([string] $m) { Write-Host "  [!!] $m" -ForegroundColor Yellow }

if ($RepoRoot -match "\s") { throw "仓库路径带空格（$RepoRoot）：schtasks 计划任务 quoting 会崩，挪到 C:\dvpack 这类短路径" }
if (-not (Test-Path (Join-Path $RepoRoot "pyproject.toml"))) {
    throw "仓库根没找到 pyproject.toml：$RepoRoot（-RepoRoot 指定，或把仓库放到 C:\dvpack）"
}
$RepoRoot = (Resolve-Path $RepoRoot).Path
Push-Location $RepoRoot
try {
    Info "仓库根：$RepoRoot"

    # ---------- 1. uv ----------
    if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
        Info "装 uv（包管理器；PyPI 走 pyproject 里配好的清华源）"
        irm https://astral.sh/uv/install.ps1 | iex
        $env:Path = "$env:USERPROFILE\.local\bin;$env:Path"
        if (-not (Get-Command uv -ErrorAction SilentlyContinue)) { throw "uv 装完仍不在 PATH，重开终端再跑本脚本" }
    }
    Ok "uv $(uv --version)"

    # ---------- 2. venv + 安装 ----------
    if (-not (Test-Path ".venv\Scripts\python.exe")) {
        Info "建 .venv（uv 按 requires-python 自动拉 3.11+）"
        uv venv
    }
    Ok ".venv 就位"
    Info "安装 dvpack（零第三方运行时依赖）"
    uv pip install -e .
    $Py = Join-Path $RepoRoot ".venv\Scripts\python.exe"
    $Cli = Join-Path $RepoRoot ".venv\Scripts\dvpack.exe"
    & $Py -c "import sys, dvpack; print('  [ok] dvpack @', dvpack.__file__, '| py', sys.version.split()[0])"

    # ---------- 3. Windows 二进制（缺失才下载） ----------
    $ffUrl = "https://github.com/BtbN/FFmpeg-Builds/releases/latest/download/ffmpeg-master-latest-win64-gpl.zip"
    $bins = @(
        @{ Path = "$RepoRoot\tools\bin\dovi_tool.exe";  Url = "https://github.com/quietvoid/dovi_tool/releases/download/$DoviVersion/dovi_tool-$DoviVersion-x86_64-pc-windows-msvc.zip"; Inner = "dovi_tool.exe" },
        @{ Path = "$RepoRoot\tools\ffmpeg\bin\ffmpeg.exe";  Url = $ffUrl; Inner = "ffmpeg.exe" },
        @{ Path = "$RepoRoot\tools\ffmpeg\bin\ffprobe.exe"; Url = $ffUrl; Inner = "ffprobe.exe" }
    )
    $extracted = @{}
    try {
        foreach ($b in $bins) {
            if (Test-Path $b.Path) { Ok "$(Split-Path -Leaf $b.Path) 已在（跳过下载）"; continue }
            if (-not $extracted.ContainsKey($b.Url)) {
                $work = Join-Path $env:TEMP ("dvpack-bin-" + [Guid]::NewGuid().ToString("N"))
                New-Item -ItemType Directory -Path $work | Out-Null
                Info "下载 $(Split-Path -Leaf $b.Url)（$(Split-Path -Leaf $b.Path)）"
                $ProgressPreference = "SilentlyContinue"  # IWR 进度条能把百兆下载拖慢一个数量级
                $zip = Join-Path $work "pkg.zip"
                Invoke-WebRequest -Uri $b.Url -OutFile $zip -UseBasicParsing
                Expand-Archive -Path $zip -DestinationPath (Join-Path $work "x") -Force
                $extracted[$b.Url] = Join-Path $work "x"
            }
            $hit = Get-ChildItem -Path $extracted[$b.Url] -Recurse -Filter $b.Inner | Select-Object -First 1
            if (-not $hit) { throw "解包里没找到 $($b.Inner)：$($b.Url)" }
            New-Item -ItemType Directory -Force -Path (Split-Path -Parent $b.Path) | Out-Null
            Copy-Item $hit.FullName $b.Path -Force
            Ok "$($b.Inner) $((Get-Item $b.Path).Length) 字节"
        }
    }
    finally {
        foreach ($dir in $extracted.Values) { Remove-Item (Split-Path -Parent $dir) -Recurse -Force -ErrorAction SilentlyContinue }
    }

    # ---------- 4. OpenSubtitles key ----------
    if ($OsKey) {
        & $Cli config --init | Out-Null
        & $Cli config --os-key $OsKey
        Ok "OpenSubtitles key 已写入 %USERPROFILE%\.dvpack\config.json"
    }

    # ---------- 5. 缓存目录 ----------
    New-Item -ItemType Directory -Force -Path $CacheRoot | Out-Null
    Ok "缓存目录 $CacheRoot（所在盘空闲建议 >30GiB）"

    # ---------- 6. 启动器 + 开机自启 ----------
    $Launcher = Join-Path $RepoRoot "run-serve.ps1"
    $jfLines = ""
    if ($JfBase) { $jfLines += "`$env:DVPACK_JF_BASE = '$JfBase'`r`n" }
    if ($JfPassword) {
        $jfLines += "`$env:DVPACK_JF_PASSWORD = '$JfPassword'`r`n"
        Warn "JfPassword 以明文写进 $Launcher——共享机器请在脚本里删掉这行，prewarm 改用 --pw 手传"
    }
    $launcherBody = @"
# dvpack serve 启动器（scripts\deploy.ps1 生成；开机自启与手动起都用它）
# 改端口/缓存目录：改下面这行；改完计划任务无需重建（任务只是拉本文件）。
`$ErrorActionPreference = "Stop"
`$env:PYTHONUNBUFFERED = "1"
$jfLines`$log = Join-Path "$RepoRoot" "logs"
New-Item -ItemType Directory -Force -Path `$log | Out-Null
Set-Location "$RepoRoot"
& "$Py" -u -m dvpack serve "$CacheRoot" --port $Port --host 0.0.0.0 2>&1 |
    Out-File -FilePath (Join-Path `$log "serve.log") -Append -Encoding utf8
"@
    Set-Content -Path $Launcher -Value $launcherBody -Encoding UTF8
    Ok "启动器 $Launcher"

    if (-not $NoAutostart) {
        schtasks /Create /TN "dvpack-serve" /SC ONLOGON /RL LIMITED /F /TR "powershell -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File $Launcher" | Out-Null
        Ok "开机自启计划任务 dvpack-serve 已建（登录即起）"
    }

    # ---------- 7. 防火墙 ----------
    $isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
    if ($isAdmin) {
        if (-not (Get-NetFirewallRule -DisplayName "dvpack tcp $Port" -ErrorAction SilentlyContinue)) {
            New-NetFirewallRule -DisplayName "dvpack tcp $Port" -Direction Inbound -Protocol TCP -LocalPort $Port -Action Allow | Out-Null
        }
        Ok "防火墙已放行入站 tcp/$Port"
    }
    else {
        Warn "非管理员，没建防火墙规则。ATV 连不上时用管理员执行："
        Write-Host "      New-NetFirewallRule -DisplayName 'dvpack tcp $Port' -Direction Inbound -Protocol TCP -LocalPort $Port -Action Allow"
    }

    # ---------- 8. 起一份 + 自检 ----------
    Start-Process -FilePath "powershell" -ArgumentList @("-NoProfile", "-ExecutionPolicy", "Bypass", "-WindowStyle", "Hidden", "-File", $Launcher) -WorkingDirectory $RepoRoot
    Start-Sleep -Seconds 5
    $healthy = $false
    try {
        Invoke-WebRequest -Uri "http://127.0.0.1:$Port/" -UseBasicParsing -TimeoutSec 5 | Out-Null
        $healthy = $true
    }
    catch {
        if ($_.Exception.Response -and [int]$_.Exception.Response.StatusCode -eq 404) { $healthy = $true }  # dvpack 对 / 回 404 = 活着
    }
    if ($healthy) { Ok "自检通过：http://127.0.0.1:$Port/ 有响应（404 是正常的，dvpack 只认具体路径）" }
    else { Warn "自检没通：看 $RepoRoot\logs\serve.log，或手动跑 $Launcher 看报错" }

    # ---------- 9. 收尾 ----------
    if ($DisableSleep) {
        powercfg /change standby-timeout-ac 0 | Out-Null
        powercfg /change hibernate-timeout-ac 0 | Out-Null
        powercfg /change monitor-timeout-ac 0 | Out-Null
        Ok "已关睡眠/休眠/息屏（AC 电源）"
    }
    $ips = Get-NetIPAddress -AddressFamily IPv4 |
        Where-Object { $_.IPAddress -notlike "127.*" -and $_.IPAddress -notlike "169.254.*" } |
        Select-Object -ExpandProperty IPAddress
    Write-Host ""
    Info "Apple TV 上 dvplay 填下面任一地址："
    foreach ($ip in $ips) { Write-Host "    http://$ip`:$Port" }
    Write-Host ""
    Info "手动预热片库（可选，首次建议先 dry-run 看计划）："
    Write-Host "    $Cli prewarm `"$CacheRoot`" --base $JfBase --user root --pw <密码> --dry-run"
    Info "别忘了：路由器给这台机做 DHCP 保留；睡眠保持关闭（ATV 直连）。"
}
finally { Pop-Location }

# 启动 N 个求解进程，共用一个落盘缓存目录与同一个 token。
#
# 为什么必须共用 --cache-dir：单进程只串行求解，扩容=加进程；但如果每个进程各有一套缓存，
# 同一个域会被每个进程各解一次（4 进程 × 18s），吞吐数字好看、实际是重复劳动。
# 共用缓存目录后，"加进程"才真正等于"加冷路径吞吐"。
#
# 用法：
#   pwsh scripts/run-pool.ps1                       # 默认 4 进程，端口 8878..8881
#   pwsh scripts/run-pool.ps1 -Count 2 -BasePort 8900 -CacheDir D:\cfcache
#   pwsh scripts/run-pool.ps1 -Prewarm "steamdb.info,glassdoor.com" -PrewarmFile hosts.txt
#
# 停止：Ctrl+C 会向所有子进程发 SIGTERM 等价信号（优雅退出），日志在 logs/ 下。

[CmdletBinding()]
param(
    [int]$Count = 4,
    [int]$BasePort = 8878,
    [string]$BindHost = "127.0.0.1",
    [string]$CacheDir = "$env:LOCALAPPDATA\cfsolve\cache",
    [string]$TokenFile = ".env",
    [string]$Prewarm = "",
    [string]$PrewarmFile = "",
    [double]$PrewarmInterval = 60,
    [double]$RenewBefore = 300,
    [string]$BrowserOs = ""
)

$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot
$python = (Get-Command python -ErrorAction SilentlyContinue)
if (-not $python) { throw "找不到 python，请先在 PATH 里准备好" }

if (-not (Test-Path $CacheDir)) { New-Item -ItemType Directory -Path $CacheDir -Force | Out-Null }
$logDir = Join-Path $repo "logs"
if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Path $logDir -Force | Out-Null }

$service = Join-Path $repo "solver\service.py"
if (-not (Test-Path $service)) { throw "找不到 $service" }

$procs = @()
$ports = @()
for ($i = 0; $i -lt $Count; $i++) {
    $port = $BasePort + $i
    $ports += $port
    $solverArgs = @(
        $service,
        "--port", $port,
        "--host", $BindHost,
        "--cache-dir", $CacheDir,
        "--token-file", $TokenFile,
        "--prewarm-interval", $PrewarmInterval,
        "--renew-before", $RenewBefore
    )
    if ($Prewarm) { $solverArgs += @("--prewarm", $Prewarm) }
    if ($PrewarmFile) { $solverArgs += @("--prewarm-file", $PrewarmFile) }
    if ($BrowserOs) { $solverArgs += @("--browser-os", $BrowserOs) }

    $log = Join-Path $logDir ("worker-{0}.log" -f $i)
    Write-Host ("启动 worker {0} -> {1}:{2}  日志 {3}" -f $i, $BindHost, $port, $log)
    $procs += Start-Process -FilePath $python.Source -ArgumentList $solverArgs -PassThru `
        -RedirectStandardOutput $log -RedirectStandardError (Join-Path $logDir ("worker-{0}.err.log" -f $i)) `
        -WorkingDirectory $repo
}

Write-Host ""
Write-Host "已启动 $Count 个进程。Go 侧这样接："
Write-Host ""
$urls = ($ports | ForEach-Object { '"http://{0}:{1}"' -f $BindHost, $_ }) -join ", "
Write-Host ("  pool, err := cfsolve.NewPool([]string{{{0}}}, cfsolve.WithPoolToken(token))" -f $urls)
Write-Host ""
Write-Host "健康检查："
foreach ($port in $ports) {
    Write-Host ("  curl -s http://{0}:{1}/health" -f $BindHost, $port)
}

# Ctrl+C 时优雅收尾：先发终止信号，等 10s，仍在就强杀
try {
    Write-Host ""
    Write-Host "按 Ctrl+C 停止全部 worker。"
    Wait-Process -Id ($procs | ForEach-Object { $_.Id })
}
finally {
    foreach ($p in $procs) {
        if (-not $p.HasExited) {
            try { $p.CloseMainWindow() | Out-Null } catch { }
            try { Stop-Process -Id $p.Id -ErrorAction SilentlyContinue } catch { }
        }
    }
    Write-Host "已停止。"
}

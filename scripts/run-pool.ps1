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
    [string]$BrowserOs = "",
    # worker 卡死时 solver 会以退出码 75 退出（v6.2 看门狗）。默认由本脚本拉起；
    # -NoRestart 用于调试（进程死了就让脚本一起退，方便看崩溃现场）。
    [switch]$NoRestart = $false
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
#: 每个 worker 的重启配方（重启时按同一份参数重拉）
$workerSpecs = @()
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
    $workerSpecs += ,@{ Index = $i; Port = $port; Args = $solverArgs; Log = $log }
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
$script:stopping = $false
try {
    Write-Host ""
    if ($NoRestart) {
        Write-Host "按 Ctrl+C 停止全部 worker（-NoRestart：进程退出不会拉起）。"
        Wait-Process -Id ($procs | ForEach-Object { $_.Id })
    }
    else {
        Write-Host "按 Ctrl+C 停止全部 worker。worker 异常退出会按完片重启（退出码 75 = 看门狗判定卡死）。"
        # 监督循环：任一 worker 退出就按同一份参数重拉。
        # 为什么必须循环而不是 Wait-Process：v6.2 看门狗在 worker 卡死时会以 75 退出——
        # 没有这个循环，'自重启' 在本地部署里就等于 '服务停了'。
        while (-not $script:stopping) {
            $i = 0
            foreach ($spec in $workerSpecs) {
                $proc = $procs[$i]
                if ($proc -and -not $proc.HasExited) { $i++; continue }
                $code = if ($proc) { $proc.ExitCode } else { -1 }
                $why = if ($code -eq 75) { "看门狗判定 worker 卡死" } else { "异常退出（code=$code）" }
                Write-Host ("[监督] worker {0}（:{1}）{2}，3s 后重启" -f $spec.Index, $spec.Port, $why) -ForegroundColor Yellow
                Start-Sleep -Seconds 3
                if ($script:stopping) { break }
                $procs[$i] = Start-Process -FilePath $python.Source -ArgumentList $spec.Args -PassThru `
                    -RedirectStandardOutput $spec.Log -RedirectStandardError ($spec.Log -replace '\.log$', '.err.log') `
                    -WorkingDirectory $repo
                Write-Host ("[监督] worker {0} 已重启（PID {1}）" -f $spec.Index, $procs[$i].Id)
                $i++
            }
            Start-Sleep -Seconds 1
        }
    }
}
finally {
    $script:stopping = $true
    foreach ($p in $procs) {
        if (-not $p.HasExited) {
            try { $p.CloseMainWindow() | Out-Null } catch { }
            try { Stop-Process -Id $p.Id -ErrorAction SilentlyContinue } catch { }
        }
    }
    Write-Host "已停止。"
}

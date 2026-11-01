<#
.SYNOPSIS
    CF-Bypass-Research 统一入口（v4.0 T-41）。

.DESCRIPTION
    为什么需要它：这套栈有 Python + Go + Node 三段，每次改动要跑的命令组合不一样，
    散在 README 里就一定会漏跑（而漏跑的那一条恰好是抓回归的那条）。
    这里把"什么场景跑什么"固化成子命令，CI 与本机用同一套。

    没有 Node 的话 `findings` 会失败 —— 那是环境缺项，不是逻辑错，判为 WARN。

.EXAMPLE
    .\run.ps1 doctor          # 环境自检（含起一次服务做 /health 探活）
    .\run.ps1 test            # Python 全量测试 + 覆盖率门槛 80%
    .\run.ps1 go              # Go vet + test -race
    .\run.ps1 verify          # 以上全跑（提交前必跑）
    .\run.ps1 smoke -Port 8878
    .\run.ps1 e2e            # 真进程 E2E（离线；--SkipBrowser 跳过真浏览器那条）
    .\run.ps1 bench           # 离线压测三模式
#>
[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [ValidateSet('doctor', 'test', 'quick', 'go', 'findings', 'smoke', 'e2e', 'start', 'stop', 'bench', 'verify', 'help')]
    [string]$Command = 'help',

    [int]$Port = 8878,
    [switch]$NoAuth,
    [switch]$SkipBrowser
)

$ErrorActionPreference = 'Stop'
$Root = $PSScriptRoot
Push-Location $Root
try {
    $py = (Get-Command python -ErrorAction SilentlyContinue)
    if (-not $py) { throw "找不到 python（请先装 Python ≥3.10 并加入 PATH）" }

    function Write-Step($msg) { Write-Host "`n=== $msg ===" -ForegroundColor Cyan }

    switch ($Command) {
        'doctor' {
            & python scripts/doctor.py --port $Port
            exit $LASTEXITCODE
        }

        'test' {
            Write-Step 'Python 全量测试 + 覆盖率门槛（policy.md: ≥80%）'
            & python -m pytest tests/ -q --cov=solver --cov-report=term-missing --cov-fail-under=80
            exit $LASTEXITCODE
        }

        'quick' {
            Write-Step '定向快测（改一处代码时用这个，别每次跑全量）'
            & python -m pytest tests/test_cache.py tests/test_cache_sqlite.py -q
            $first = $LASTEXITCODE
            & python -m pytest tests/ -q -k "route or guard or ttl or budget"
            if ($LASTEXITCODE -ne 0 -or $first -ne 0) { exit 1 }
            exit $LASTEXITCODE
        }

        'go' {
            Write-Step 'Go: vet + test -race'
            Push-Location (Join-Path $Root 'gocfsolve')
            try {
                & go vet ./...
                if ($LASTEXITCODE -ne 0) { throw 'go vet 失败' }
                & go test -race ./...
                exit $LASTEXITCODE
            }
            finally { Pop-Location }
        }

        'findings' {
            Write-Step '结论库一致性校验（findings.json ↔ 仓库文件）'
            if (Get-Command node -ErrorAction SilentlyContinue) {
                & node scripts/validate-findings.mjs
                exit $LASTEXITCODE
            }
            Write-Host 'WARN: 未安装 node，跳过 findings 校验' -ForegroundColor Yellow
            exit 0
        }

        'smoke' {
            Write-Step "起服务并做冒烟（port=$Port）"
            $auth = @()
            if ($NoAuth) { $auth = @('--no-auth') }
            $cacheDir = Join-Path $env:TEMP ("cfsolve-smoke-" + [guid]::NewGuid().ToString('N').Substring(0, 8))
            $svcArgs = @('solver/service.py', '--port', $Port, '--cache-dir', $cacheDir) + $auth
            $svc = Start-Process -FilePath 'python' -PassThru -ArgumentList $svcArgs
            try {
                $deadline = (Get-Date).AddSeconds(120)
                $ready = $false
                while ((Get-Date) -lt $deadline) {
                    try {
                        $r = Invoke-RestMethod "http://127.0.0.1:$Port/health" -TimeoutSec 3
                        if ($r.state -eq 'ready') { $ready = $true; break }
                    } catch { Start-Sleep -Milliseconds 500 }
                }
                if (-not $ready) { throw '服务 120s 内未就绪（看 solver/service.py 的启动输出）' }
                Write-Host "OK  /health ready（browser=$($r.browser), cache=$($r.cache_backend)）" -ForegroundColor Green

                $d = Invoke-RestMethod "http://127.0.0.1:$Port/detect?url=https://example.com/" -TimeoutSec 20
                Write-Host "OK  /detect example.com → protected=$($d.protected) route=$($d.recommendation.route)" -ForegroundColor Green

                $bad = $null
                try {
                    Invoke-RestMethod "http://127.0.0.1:$Port/detect?url=http://127.0.0.1:$Port/health" -TimeoutSec 10 | Out-Null
                } catch { $bad = $_ }
                if (-not $bad) { throw 'SSRF 守卫失效：内网 URL 没有被拒绝' }
                Write-Host 'OK  SSRF 守卫拦住了内网 URL' -ForegroundColor Green
            }
            finally {
                if ($svc -and -not $svc.HasExited) { $svc.Kill() }
                Remove-Item -Recurse -Force $cacheDir -ErrorAction SilentlyContinue
            }
        }

        'e2e' {
            Write-Step '真进程 E2E 验收（离线；起两个真服务进程，覆盖鉴权/守卫/metrics/route=auto/审计/优雅退出）'
            $args2 = @('scripts/e2e_local.py', '--json-out', 'evidence/e2e-local.json')
            if ($SkipBrowser) { $args2 += '--skip-browser' }
            & python @args2
            exit $LASTEXITCODE
        }

        'start' {
            Write-Step "前台启动（port=$Port，Ctrl+C 退出）"
            $auth = @()
            if ($NoAuth) { $auth = @('--no-auth') }
            & python solver/service.py --port $Port --cache-dir ./cache @auth
            exit $LASTEXITCODE
        }

        'stop' {
            Write-Step "优雅停止监听 $Port 的服务"
            $conns = netstat -ano | Select-String ":$Port\s+.*LISTENING"
            if (-not $conns) { Write-Host "没有进程监听 $Port"; exit 0 }

            # 先试 /shutdown：Windows 上 Stop-Process/kill -TERM 等价于强制终止，
            # 正在求解的调用方会拿到 WinError 10054（实测），滚动重启会掉请求。
            $token = $env:CF_SOLVE_TOKEN
            if (-not $token -and (Test-Path (Join-Path $Root '.env'))) {
                $line = Get-Content (Join-Path $Root '.env') | Where-Object { $_ -match '^CF_SOLVE_TOKEN=' } | Select-Object -First 1
                if ($line) { $token = $line.Split('=', 2)[1].Trim() }
            }
            $graceful = $false
            try {
                $headers = @{}
                if ($token) { $headers['Authorization'] = "Bearer $token" }
                Invoke-RestMethod -Uri "http://127.0.0.1:$Port/shutdown" -Method Post -Headers $headers `
                    -ContentType 'application/json' -Body '{}' -TimeoutSec 5 | Out-Null
                $graceful = $true
                Write-Host "已请求优雅退出（等 15s 让在途请求收尾）"
                for ($i = 0; $i -lt 30; $i++) {
                    Start-Sleep -Milliseconds 500
                    if (-not (netstat -ano | Select-String ":$Port\s+.*LISTENING")) { break }
                }
            } catch {
                Write-Host "优雅退出不可用（$($_.Exception.Message)），回退为强制终止" -ForegroundColor Yellow
            }

            $conns = netstat -ano | Select-String ":$Port\s+.*LISTENING"
            foreach ($line in $conns) {
                $procId = ($line.ToString() -split '\s+')[-1]
                if ($procId -match '^\d+$') {
                    Write-Host "强制终止 PID $procId（$(if ($graceful) {'优雅退出未在 15s 内完成'} else {'无 token 或端点不可用'})）"
                    Stop-Process -Id $procId -Force -ErrorAction SilentlyContinue
                }
            }
        }

        'bench' {
            Write-Step '离线压测（不打真实站点）'
            & python scripts/loadtest.py --mode hot --workers 8 --duration 5
            & python scripts/loadtest.py --mode cold --workers 16 --solve-delay 0.05
            & python scripts/loadtest.py --mode share --processes 4 --iterations 2
            exit $LASTEXITCODE
        }

        'verify' {
            Write-Step 'verify: Python 测试 + 覆盖率'
            & python -m pytest tests/ -q --cov=solver --cov-report=term --cov-fail-under=80
            if ($LASTEXITCODE -ne 0) { throw 'Python 测试未通过' }

            Write-Step 'verify: Python 语法'
            & python -m compileall -q solver scripts tests
            if ($LASTEXITCODE -ne 0) { throw 'compileall 失败' }

            Write-Step 'verify: Go'
            Push-Location (Join-Path $Root 'gocfsolve')
            try {
                & go vet ./...
                if ($LASTEXITCODE -ne 0) { throw 'go vet 失败' }
                & go test -race ./...
                if ($LASTEXITCODE -ne 0) { throw 'go test 失败' }
            }
            finally { Pop-Location }

            Write-Step 'verify: findings'
            if (Get-Command node -ErrorAction SilentlyContinue) {
                & node scripts/validate-findings.mjs
                if ($LASTEXITCODE -ne 0) { throw 'findings 校验失败' }
            }
            else { Write-Host 'WARN: 无 node，跳过 findings' -ForegroundColor Yellow }

            Write-Step 'verify: 真进程 E2E（离线）'
            & python scripts/e2e_local.py --json-out evidence/e2e-local.json
            if ($LASTEXITCODE -ne 0) { throw 'E2E 验收失败（看 evidence/e2e-local.json 的 failed 项）' }

            Write-Host "`n全部通过 ✅" -ForegroundColor Green
        }

        default {
            Get-Help $PSCommandPath -Detailed
        }
    }
}
finally {
    Pop-Location
}

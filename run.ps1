<#
.SYNOPSIS
    CF-Bypass-Research 统一入口（v4.0 T-41；v5.1 T-80 起内部转发到 run.py）。

.DESCRIPTION
    为什么需要它：这套栈有 Python + Go + Node 三段，每次改动要跑的命令组合不一样，
    散在 README 里就一定会漏跑（而漏跑的那一条恰好是抓回归的那条）。
    这里把"什么场景跑什么"固化成子命令，CI 与本机用同一套。

    v5.1 T-80：本脚本只做参数转发到 run.py（跨平台单一实现），避免两套逻辑漂移。
    tests/test_runpy.py::test_runpy_targets_match_runps1 锁死两套命令集合相等。

    没有 Node 的话 `findings` 会失败 —— 那是环境缺项，不是逻辑错，判为 WARN。

.EXAMPLE
    .\run.ps1 doctor          # 环境自检（含起一次服务做 /health 探活）
    .\run.ps1 test            # Python 全量测试 + 覆盖率门槛 80%
    .\run.ps1 go              # Go vet + test -race
    .\run.ps1 verify          # 以上全跑（提交前必跑）
    .\run.ps1 smoke -Port 8878
    .\run.ps1 e2e -SkipBrowser
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

    # —— 参数转发（保持 run.ps1 原有参数名不变：-Port / -NoAuth / -SkipBrowser）——
    $pyArgs = @('run.py', $Command, '--port', $Port)
    if ($NoAuth) { $pyArgs += '--no-auth' }
    if ($SkipBrowser) { $pyArgs += '--skip-browser' }
    & python @pyArgs
    exit $LASTEXITCODE
}
finally {
    Pop-Location
}

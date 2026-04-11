#Requires -Version 5.1
<#
.SYNOPSIS
  Запуск filewalk_review_runner на N шагов (по умолчанию 50) для allow-list / коротких команд.

.DESCRIPTION
  - Сам задаёт COLLOQUIUM_PASSWORD_FILE на cqds_mcp_auth.secret (или устаревший copilot_mcp_tool.secret) рядом с mcp-tools, если не задан.
  - Без -Offset: читает смещение из logs\filewalk_batch_offset.txt (или 0), после запуска пишет offset+MaxTurns.
  - -AutoOffset — то же, что «без -Offset» (для явного флага в allow-list).
  - Логи: фон — filewalk_*_stdout.log и *_stderr.log; с -Wait после завершения сливаются в *_run.log.
  - PID — процесс python.exe (без cmd-обёртки).

.EXAMPLE
  .\run_filewalk_batch.ps1
  .\run_filewalk_batch.ps1 -Offset 400
  .\run_filewalk_batch.ps1 -AutoOffset -MaxTurns 50
#>
[CmdletBinding()]
param(
    [int]$MaxTurns = 50,
    [int]$ProjectId = 2,
    [int]$MaxFiles = 300,
    [int]$Offset = -1,
    [switch]$AutoOffset,
    [string]$Url = "http://localhost:8008",
    [string]$Username = "copilot",
    [string]$Python = "C:\Apps\Python3\python.exe",
    [int]$FlushEvery = 5,
    [switch]$Wait
)

$ErrorActionPreference = "Stop"
$McpTools = $PSScriptRoot
$Runner = Join-Path $McpTools "scripts\filewalk_review_runner.py"
$Logs = Join-Path $McpTools "logs"
$StateFile = Join-Path $Logs "filewalk_batch_offset.txt"
$SecretNew = Join-Path $McpTools "cqds_mcp_auth.secret"
$SecretLegacy = Join-Path $McpTools "copilot_mcp_tool.secret"
if (Test-Path $SecretNew) { $Secret = $SecretNew }
elseif (Test-Path $SecretLegacy) { $Secret = $SecretLegacy }
else { $Secret = $SecretNew }

if (-not (Test-Path $Runner)) { throw "Не найден runner: $Runner" }
if (-not (Test-Path $Secret)) { throw "Не найден secret: ожидается $SecretNew или $SecretLegacy" }
if (-not $env:COLLOQUIUM_PASSWORD_FILE) { $env:COLLOQUIUM_PASSWORD_FILE = $Secret }

if (-not (Test-Path $Logs)) { New-Item -ItemType Directory -Path $Logs -Force | Out-Null }

if ($PSBoundParameters.ContainsKey('Offset') -and $Offset -ge 0) {
    $useOffset = $Offset
} elseif ($AutoOffset -or -not $PSBoundParameters.ContainsKey('Offset')) {
    if (Test-Path $StateFile) {
        $raw = (Get-Content $StateFile -Raw -ErrorAction SilentlyContinue).Trim()
        $parsed = 0
        if ([int]::TryParse($raw, [ref]$parsed)) { $useOffset = $parsed } else { $useOffset = 0 }
    } else {
        $useOffset = 0
    }
} else {
    throw "Укажите неотрицательный -Offset N или опустите -Offset для чтения из $StateFile"
}

$stamp = Get-Date -Format "yyyyMMdd_HHmmss"
$batchTag = "batch{0}" -f $stamp
$out = Join-Path $Logs ("filewalk_{0}.json" -f $stamp)
$runLog = Join-Path $Logs ("filewalk_{0}_run.log" -f $stamp)
$stdoutLog = Join-Path $Logs ("filewalk_{0}_stdout.log" -f $stamp)
$stderrLog = Join-Path $Logs ("filewalk_{0}_stderr.log" -f $stamp)

$argList = @(
    $Runner,
    "--url", $Url,
    "--username", $Username,
    "--project-id", "$ProjectId",
    "--max-files", "$MaxFiles",
    "--offset", "$useOffset",
    "--max-total-turns", "$MaxTurns",
    "--files-per-chat", "1",
    "--list-timeout", "240",
    "--wait-timeout", "180",
    "--per-file-sleep", "0.85",
    "--batch-sleep", "0",
    "--chat-name-prefix", "$batchTag-",
    "--flush-every", "$FlushEvery",
    "--out", $out
)

$next = $useOffset + $MaxTurns
Set-Content -Path $StateFile -Value "$next" -Encoding utf8

# Прямой запуск python (без cmd): иначе строка для cmd ломается, Python уходит в REPL 3.13 + _pyrepl
# и сыплет WinError в лог на десятки МБ. -u — небуферизованный stdout.
$procArgs = @("-u") + $argList
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"

if ($Wait) {
    $p = Start-Process -FilePath $Python -ArgumentList $procArgs -WorkingDirectory $McpTools -Wait -PassThru -NoNewWindow `
        -RedirectStandardOutput $stdoutLog -RedirectStandardError $stderrLog
    $code = $p.ExitCode
    "--- stdout ---" | Out-File -FilePath $runLog -Encoding utf8
    Get-Content -Path $stdoutLog -Raw -ErrorAction SilentlyContinue | Out-File -FilePath $runLog -Encoding utf8 -Append
    "`n--- stderr ---" | Out-File -FilePath $runLog -Encoding utf8 -Append
    Get-Content -Path $stderrLog -Raw -ErrorAction SilentlyContinue | Out-File -FilePath $runLog -Encoding utf8 -Append
    Remove-Item -Path $stdoutLog, $stderrLog -Force -ErrorAction SilentlyContinue
    Write-Output ("DONE exit={0} out={1} log={2}" -f $code, $out, $runLog)
    exit $code
}

$p = Start-Process -FilePath $Python -ArgumentList $procArgs -WorkingDirectory $McpTools -WindowStyle Hidden -PassThru `
    -RedirectStandardOutput $stdoutLog -RedirectStandardError $stderrLog
Write-Output ("STARTED pid={0} offset={1} out={2} log={3} logErr={4}" -f $p.Id, $useOffset, $out, $stdoutLog, $stderrLog)
exit 0

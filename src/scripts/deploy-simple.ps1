param(
  [string]$ProjectRoot = "",
  [string]$ComposeFile = "docker-compose.yml",
  [string]$EnvFile = ".env"
)

$ErrorActionPreference = "Stop"

function Info($msg) { Write-Host $msg }
function Fail($msg) { throw $msg }

if ([string]::IsNullOrWhiteSpace($ProjectRoot)) {
  $ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
}

Set-Location $ProjectRoot

function New-RandomPassword([int]$Length = 28) {
  $bytes = New-Object byte[] 96
  [System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
  $raw = [Convert]::ToBase64String($bytes) -replace '[^A-Za-z0-9]', ''
  if ($raw.Length -lt $Length) {
    $raw += ([Guid]::NewGuid().ToString('N'))
  }
  return $raw.Substring(0, $Length)
}

function New-McpToken {
  $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
  $b = New-Object byte[] 24
  $rng.GetBytes($b)
  return ([BitConverter]::ToString($b)).Replace("-", "").ToLowerInvariant()
}

function Set-EnvValue([string]$Path, [string]$Key, [string]$Value) {
  if (-not (Test-Path $Path)) {
    New-Item -ItemType File -Path $Path -Force | Out-Null
  }

  $content = Get-Content -Path $Path -Raw -ErrorAction SilentlyContinue
  if ($null -eq $content) { $content = "" }
  $pattern = "(?m)^" + [regex]::Escape($Key) + "=.*$"
  $line = "$Key=$Value"
  if ([regex]::IsMatch($content, $pattern)) {
    $content = [regex]::Replace($content, $pattern, [System.Text.RegularExpressions.MatchEvaluator]{ param($m) $line })
  } else {
    if ($content -ne "" -and -not $content.EndsWith("`n")) { $content += "`r`n" }
    $content += "$line`r`n"
  }
  Set-Content -Path $Path -Value $content -Encoding utf8
}

function Ensure-CqdsDbPasswordFile {
  $secretsDir = Join-Path $ProjectRoot "secrets"
  if (-not (Test-Path $secretsDir)) {
    New-Item -ItemType Directory -Path $secretsDir -Force | Out-Null
  }
  $f = Join-Path $secretsDir "cqds_db_password"
  if ((Test-Path $f) -and ((Get-Item $f).Length -gt 0)) {
    Info "#INFO: $f already present, leaving unchanged"
    return
  }
  $pw = New-RandomPassword
  $utf8NoBom = New-Object System.Text.UTF8Encoding $false
  [System.IO.File]::WriteAllText($f, $pw, $utf8NoBom)
  Info "#INFO: created $f (random password for PostgreSQL init and role cqds)"
}

function Ensure-McpAuthTokenEnvFile {
  $envPathLocal = Join-Path $ProjectRoot $EnvFile
  if (-not [string]::IsNullOrWhiteSpace($env:MCP_AUTH_TOKEN)) {
    Info "#INFO: MCP_AUTH_TOKEN is set in the environment, skip $EnvFile"
    return
  }
  if (Test-Path $envPathLocal) {
    $hit = Select-String -Path $envPathLocal -Pattern '^MCP_AUTH_TOKEN=.' -Quiet
    if ($hit) {
      Info "#INFO: $EnvFile already defines MCP_AUTH_TOKEN"
      return
    }
  }
  $tok = New-McpToken
  Set-EnvValue -Path $envPathLocal -Key 'MCP_AUTH_TOKEN' -Value $tok
  Info "#INFO: wrote MCP_AUTH_TOKEN to $EnvFile (required by docker-compose.yml)"
}

function Test-DockerComposeV2 {
  $old = $ErrorActionPreference
  $ErrorActionPreference = "SilentlyContinue"
  try {
    & docker compose version 2>$null | Out-Null
    return ($LASTEXITCODE -eq 0)
  } finally {
    $ErrorActionPreference = $old
  }
}

Info "#STEP 1/3: secrets (PostgreSQL file + MCP token for compose)"
Ensure-CqdsDbPasswordFile
Ensure-McpAuthTokenEnvFile

if (-not (Test-DockerComposeV2)) {
  Fail "need Docker Compose v2 (docker compose)"
}

Info "#STEP 2/3: build ($ComposeFile)"
& docker compose -f $ComposeFile build
if ($LASTEXITCODE -ne 0) { Fail "docker compose build failed" }

Info "#STEP 3/3: up -d"
& docker compose -f $ComposeFile up -d
if ($LASTEXITCODE -ne 0) { Fail "docker compose up failed" }

Info "#SUCCESS: stack started (see docker compose ps)"

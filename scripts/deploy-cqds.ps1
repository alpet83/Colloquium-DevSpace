param(
  [string]$TargetDir = "p:\opt\docker\cqds.test",
  [string]$MainRepo = "p:\GitHub\Colloquium-DevSpace",
  [string]$SandwichRepo = "p:\GitHub\Sandwich-pack",
  [switch]$NonInteractive,
  [switch]$GeneratePassword,
  [string]$DbPassword,
  [switch]$StopExisting,
  [switch]$RestoreLatestBackup,
  [switch]$SkipBuild,
  [switch]$SkipUp
)

$ErrorActionPreference = "Stop"

function Write-Info([string]$msg) { Write-Host "[INFO] $msg" -ForegroundColor Cyan }
function Write-Warn([string]$msg) { Write-Host "[WARN] $msg" -ForegroundColor Yellow }

function Read-YesNo([string]$prompt, [bool]$defaultYes = $true) {
  if ($NonInteractive) { return $defaultYes }
  $suffix = if ($defaultYes) { "[Y/n]" } else { "[y/N]" }
  $answer = Read-Host "$prompt $suffix"
  if ([string]::IsNullOrWhiteSpace($answer)) { return $defaultYes }
  return @("y", "yes") -contains $answer.Trim().ToLowerInvariant()
}

function New-RandomPassword([int]$length = 24) {
  $chars = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789!@#$%^&*()-_=+"
  $bytes = New-Object byte[] $length
  [System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
  $sb = New-Object System.Text.StringBuilder
  foreach ($b in $bytes) {
    [void]$sb.Append($chars[$b % $chars.Length])
  }
  return $sb.ToString()
}

function Ensure-Dir([string]$path) {
  if (-not (Test-Path $path)) {
    New-Item -Path $path -ItemType Directory -Force | Out-Null
  }
}

function Wait-PostgresHealthy([int]$maxAttempts = 30, [int]$delaySeconds = 2) {
  for ($i = 1; $i -le $maxAttempts; $i++) {
    $status = docker inspect --format "{{.State.Health.Status}}" cqds-postgres 2>$null
    if ($status -eq "healthy") { return }
    Start-Sleep -Seconds $delaySeconds
  }
  throw "PostgreSQL container did not become healthy in time"
}

function Get-LatestBackup([string]$targetDir) {
  $parent = Split-Path -Parent $targetDir
  $candidates = @(
    "$targetDir\data\backups\pg",
    "$parent\cqds\data\backups\pg"
  )

  $files = @()
  foreach ($dir in $candidates) {
    if (Test-Path $dir) {
      $files += Get-ChildItem -Path $dir -Filter *.dump -File -ErrorAction SilentlyContinue
    }
  }

  if (-not $files) { return $null }
  return $files | Sort-Object LastWriteTime -Descending | Select-Object -First 1
}

function Show-AdminPasswordFragment([string]$targetDir) {
  Write-Host ""
  Write-Host "==== colloquium_core.log fragment (admin password) ====" -ForegroundColor Yellow

  $pattern = "Создан пользователь admin с временным паролем|temporary password|admin.+password"
  $logFile = Join-Path $targetDir "logs\colloquium_core.log"

  if (Test-Path $logFile) {
    $lines = Get-Content -Path $logFile -Encoding UTF8 -ErrorAction SilentlyContinue
    if ($lines) {
      $hits = @()
      for ($i = 0; $i -lt $lines.Count; $i++) {
        if ($lines[$i] -match $pattern) {
          $hits += $i
        }
      }
      if ($hits.Count -gt 0) {
        $idx = $hits[$hits.Count - 1]
        $start = [Math]::Max(0, $idx - 2)
        $end = [Math]::Min($lines.Count - 1, $idx + 2)
        for ($j = $start; $j -le $end; $j++) {
          Write-Host $lines[$j]
        }
        return
      }
    }
  }

  Write-Warn "Admin temporary password line not found in $logFile"
  Write-Host "Recent colloquium-core logs:" -ForegroundColor Yellow
  docker compose logs --no-color --tail=80 colloquium-core | Select-String -Pattern $pattern -Context 2,2
  Write-Host "If empty, admin user may already exist and password generation was skipped." -ForegroundColor Yellow
}

function Copy-Tree([string]$source, [string]$target) {
  robocopy $source $target /E /R:1 /W:1 /NFL /NDL /NJH /NJS /NP | Out-Null
  if ($LASTEXITCODE -ge 8) {
    throw "robocopy failed: $source -> $target (code $LASTEXITCODE)"
  }
}

Write-Info "CQDS deploy script (PowerShell)"

if (-not (Test-Path $MainRepo)) { throw "Main repo not found: $MainRepo" }
if (-not (Test-Path $SandwichRepo)) { throw "Sandwich repo not found: $SandwichRepo" }

if (-not $NonInteractive) {
  $inputTarget = Read-Host "Target directory [$TargetDir]"
  if (-not [string]::IsNullOrWhiteSpace($inputTarget)) { $TargetDir = $inputTarget }
}

Ensure-Dir $TargetDir

$stopNow = $StopExisting.IsPresent
if (-not $stopNow) {
  $stopNow = Read-YesNo "Stop and remove currently running CQDS containers to avoid name/port conflicts?" $true
}

if ($stopNow) {
  Write-Info "Stopping current CQDS containers"
  try {
    Push-Location "p:\opt\docker\cqds"
    if (Test-Path ".\docker-compose.yml") {
      docker compose down --remove-orphans | Out-Null
    }
  } catch {
    Write-Warn "docker compose down in p:\opt\docker\cqds failed: $($_.Exception.Message)"
  } finally {
    Pop-Location
  }

  foreach ($name in @("colloquium-core", "cqds-postgres", "mcp-sandbox", "frontend", "nginx-router")) {
    try {
      docker rm -f $name | Out-Null
    } catch {
      Write-Warn "Container '$name' is not present or cannot be removed: $($_.Exception.Message)"
    }
  }
}

Write-Info "Preparing target structure in $TargetDir"
Ensure-Dir "$TargetDir\data"
Ensure-Dir "$TargetDir\logs"
Ensure-Dir "$TargetDir\projects"
Ensure-Dir "$TargetDir\secrets"
Ensure-Dir "$TargetDir\postgres"

Write-Info "Copying Colloquium src files"
Copy-Tree "$MainRepo\src" $TargetDir

Write-Info "Copying Sandwich lib"
Ensure-Dir "$TargetDir\agent"
Copy-Tree "$SandwichRepo\src\lib" "$TargetDir\agent\lib"
Copy-Item "$SandwichRepo\requirements.txt" "$TargetDir\agent\requirements_sandwich.txt" -Force

Write-Info "Copying docs near deployment root"
$parentDir = Split-Path -Parent $TargetDir
Ensure-Dir "$parentDir\docs"
Copy-Tree "$MainRepo\docs" "$parentDir\docs"
if (Test-Path "$SandwichRepo\README.md") {
  Copy-Item "$SandwichRepo\README.md" "$parentDir\docs\SANDWICH.md" -Force
}

if (-not $DbPassword) {
  if ($GeneratePassword -or ($NonInteractive -and -not $DbPassword)) {
    $DbPassword = New-RandomPassword
  } else {
    $generate = Read-YesNo "Generate random PostgreSQL root/user password?" $true
    if ($generate) {
      $DbPassword = New-RandomPassword
    } else {
      while (-not $DbPassword) {
        $DbPassword = Read-Host "Enter PostgreSQL password (will be stored in secrets/cqds_db_password)"
      }
    }
  }
}

Set-Content -Path "$TargetDir\secrets\cqds_db_password" -Value $DbPassword -NoNewline -Encoding UTF8
Write-Info "Password file created: $TargetDir\secrets\cqds_db_password"

Push-Location $TargetDir
try {
  $env:DB_ROOT_PASSWD = $DbPassword

  if (-not $SkipBuild) {
    Write-Info "Building docker images"
    docker compose build
  } else {
    Write-Info "Build skipped"
  }

  if (-not $SkipUp) {
    Write-Info "Starting base services (postgres + sandbox)"
    docker compose up -d postgres mcp-sandbox
    Wait-PostgresHealthy

    Write-Info "Reconciling cqds database role/password"
    docker exec -u postgres cqds-postgres psql -d postgres -v ON_ERROR_STOP=1 -c "DO `$$ BEGIN IF NOT EXISTS (SELECT FROM pg_catalog.pg_roles WHERE rolname = 'cqds') THEN CREATE ROLE cqds LOGIN PASSWORD '$DbPassword'; ELSE ALTER ROLE cqds WITH LOGIN PASSWORD '$DbPassword'; END IF; END `$$;"
    $dbExists = docker exec -u postgres cqds-postgres psql -d postgres -tAc "SELECT 1 FROM pg_database WHERE datname = 'cqds'"
    if ($dbExists.ToString().Trim() -ne "1") {
      docker exec -u postgres cqds-postgres createdb -O cqds cqds
    }
    docker exec -u postgres cqds-postgres psql -d postgres -v ON_ERROR_STOP=1 -c "GRANT ALL PRIVILEGES ON DATABASE cqds TO cqds;"

    Write-Info "Checking bootstrap schema in cqds database"
    $schemaCheck = docker exec -e "PGPASSWORD=$DbPassword" cqds-postgres psql -U cqds -d cqds -tAc "SELECT EXISTS (SELECT 1 FROM information_schema.tables WHERE table_schema = 'public' AND table_name = 'users')"
    $schemaReady = $false
    if ($schemaCheck) {
      $schemaReady = $schemaCheck.ToString().Trim().ToLowerInvariant() -eq "t"
    }
    if (-not $schemaReady) {
      Write-Info "users table is missing, importing prototype schema"
      docker exec -e "PGPASSWORD=$DbPassword" cqds-postgres psql -v ON_ERROR_STOP=1 -U cqds -d cqds -f /docker-entrypoint-initdb.d/02-cqds-schema.sql
    } else {
      Write-Info "Bootstrap schema already exists"
    }

    $backup = Get-LatestBackup $TargetDir
    if ($backup) {
      $restore = $RestoreLatestBackup.IsPresent
      if (-not $restore -and -not $NonInteractive) {
        $restore = Read-YesNo "Backup found ($($backup.FullName)). Restore it now?" $false
      }
      if ($restore) {
        Write-Info "Restoring backup: $($backup.FullName)"
        docker run --rm --network "container:cqds-postgres" -e "PGPASSWORD=$DbPassword" -v "$($backup.DirectoryName):/backups" postgres:17-alpine sh -lc "pg_restore --clean --if-exists --no-owner --no-privileges -h 127.0.0.1 -U cqds -d cqds /backups/$($backup.Name) || true"
      } else {
        Write-Info "Backup restore skipped"
      }
    } else {
      Write-Info "No backup dumps found, continuing with bootstrap schema only"
    }

    Write-Info "Starting app services"
    docker compose up -d colloquium-core frontend nginx-router
    Start-Sleep -Seconds 5
    docker compose ps
    Show-AdminPasswordFragment $TargetDir
  } else {
    Write-Info "Startup skipped"
  }
}
finally {
  Pop-Location
}

Write-Host ""
Write-Host "Deployment completed." -ForegroundColor Green
Write-Host "Target: $TargetDir"
Write-Host "DB password file: $TargetDir\secrets\cqds_db_password"

<#
.SYNOPSIS
  Start AiSquared Travel Planner (FastAPI backend + built React UI + in-process scheduler).

.DESCRIPTION
  Serves the app at http://127.0.0.1:8765 (BACKEND_PORT in travel-planner\.env overrides it).
  Builds the frontend first when frontend\dist is missing or -Build is given.
  Price checks only run while this process is running; use install-startup-task.ps1 to start
  it automatically at Windows logon.

.EXAMPLE
  .\scripts\run.ps1            # start and open the UI in Microsoft Edge
  .\scripts\run.ps1 -Build     # rebuild the frontend first
  .\scripts\run.ps1 -NoBrowser # start without opening a browser window (used by the startup task)
#>
param(
    [switch]$Build,
    [switch]$NoBrowser
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $Root ".venv\Scripts\python.exe"
$Backend = Join-Path $Root "backend"
$Frontend = Join-Path $Root "frontend"

if (-not (Test-Path $Python)) {
    throw "Python venv not found at $Python. Create it: py -3.13 -m venv .venv; .venv\Scripts\python.exe -m pip install -r backend\requirements.txt"
}

$Port = 8765
$EnvFile = Join-Path $Root ".env"
if (Test-Path $EnvFile) {
    $line = Select-String -Path $EnvFile -Pattern '^\s*BACKEND_PORT\s*=\s*(\d+)\s*$' | Select-Object -First 1
    if ($line) { $Port = [int]$line.Matches[0].Groups[1].Value }
}
$Url = "http://127.0.0.1:$Port"

# Already running (e.g. started by the logon task): just open the UI instead of failing on the port.
try {
    Invoke-WebRequest -UseBasicParsing "$Url/api/health" -TimeoutSec 2 | Out-Null
    Write-Host "AiSquared Travel Planner is already running at $Url" -ForegroundColor Green
    if (-not $NoBrowser) { Start-Process "microsoft-edge:$Url" }
    return
} catch { }

if ($Build -or -not (Test-Path (Join-Path $Frontend "dist\index.html"))) {
    Write-Host "Building frontend..." -ForegroundColor Cyan
    Push-Location $Frontend
    try {
        if (-not (Test-Path "node_modules")) { npm install --no-fund --no-audit }
        npm run build
        if ($LASTEXITCODE -ne 0) { throw "Frontend build failed" }
    } finally { Pop-Location }
}

$env:PYTHONUTF8 = "1"
Write-Host "Starting AiSquared Travel Planner at $Url" -ForegroundColor Green

if (-not $NoBrowser) {
    # Open Edge once the server answers (background job so uvicorn keeps the console).
    Start-Job -ArgumentList $Url -ScriptBlock {
        param($u)
        for ($i = 0; $i -lt 60; $i++) {
            try { Invoke-WebRequest -UseBasicParsing "$u/api/health" -TimeoutSec 2 | Out-Null; break } catch { Start-Sleep -Milliseconds 500 }
        }
        Start-Process "microsoft-edge:$u"
    } | Out-Null
}

Push-Location $Backend
try {
    & $Python -m uvicorn app.main:app --host 127.0.0.1 --port $Port
} finally { Pop-Location }

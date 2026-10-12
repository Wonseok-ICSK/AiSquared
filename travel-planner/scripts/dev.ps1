<#
.SYNOPSIS
  Development mode: backend with auto-reload (scheduler disabled) + Vite dev server.

.DESCRIPTION
  Backend: http://127.0.0.1:8765 (uvicorn --reload, SCHEDULER_ENABLED=false so reloads never
  interrupt scheduled browser jobs). Frontend: http://127.0.0.1:5174 (Vite, proxies /api).
  Port 5173 is left free for the other app in the parent folder.
#>
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $Root ".venv\Scripts\python.exe"

$backendCmd = "`$env:PYTHONUTF8='1'; `$env:SCHEDULER_ENABLED='false'; Set-Location '$Root\backend'; & '$Python' -m uvicorn app.main:app --host 127.0.0.1 --port 8765 --reload"
Start-Process pwsh -ArgumentList "-NoExit", "-Command", $backendCmd

Push-Location (Join-Path $Root "frontend")
try {
    if (-not (Test-Path "node_modules")) { npm install --no-fund --no-audit }
    npm run dev
} finally { Pop-Location }

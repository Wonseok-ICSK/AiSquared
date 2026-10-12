<#
.SYNOPSIS
  Register (or remove) a per-user Windows scheduled task that starts the app at logon,
  so scheduled price checks keep running without opening a terminal.

.EXAMPLE
  .\scripts\install-startup-task.ps1            # register the task for the current user
  .\scripts\install-startup-task.ps1 -Remove    # unregister it
#>
param([switch]$Remove)

$ErrorActionPreference = "Stop"
$TaskName = "AiSquared Travel Planner"
$Root = Split-Path -Parent $PSScriptRoot
$RunScript = Join-Path $Root "scripts\run.ps1"

if ($Remove) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
    Write-Host "Removed scheduled task '$TaskName'."
    return
}

$shell = (Get-Command pwsh -ErrorAction SilentlyContinue).Source
if (-not $shell) { $shell = (Get-Command powershell).Source }

$action = New-ScheduledTaskAction -Execute $shell `
    -Argument "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$RunScript`" -NoBrowser" `
    -WorkingDirectory $Root
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 5)
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings `
    -Principal $principal -Description "Starts AiSquared Travel Planner (flight price monitor) at logon." -Force | Out-Null
Write-Host "Registered '$TaskName' (runs at logon for $env:USERNAME). Start it now with: Start-ScheduledTask -TaskName '$TaskName'"

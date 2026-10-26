#Requires -Version 5.1
<#
.SYNOPSIS
    Stops and removes the TradeCopier Windows Scheduled Task (preserves data directory unless -PurgeData is passed).
#>

param(
    [switch]$PurgeData
)

$ErrorActionPreference = "Continue"

Write-Host "Stopping and removing Scheduled Task 'TradeCopier'..."
Stop-ScheduledTask -TaskName "TradeCopier" -ErrorAction SilentlyContinue
Unregister-ScheduledTask -TaskName "TradeCopier" -Confirm:$false -ErrorAction SilentlyContinue

if ($PurgeData) {
    $DataDir = if ($env:TC_DATA_DIR) { $env:TC_DATA_DIR } else { Join-Path $env:ProgramData "TradeCopier" }
    if (Test-Path $DataDir) {
        Write-Warning "Deleting data directory $DataDir ..."
        Remove-Item -Recurse -Force $DataDir
    }
}

Write-Host "Uninstall complete."

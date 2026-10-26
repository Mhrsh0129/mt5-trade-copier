#Requires -Version 5.1
<#
.SYNOPSIS
    One-time setup script for the self-hosted MT5 Trade Copier on Windows 10/11.
    Run from an elevated PowerShell prompt:
        powershell -ExecutionPolicy Bypass -File .\setup.ps1
#>

param(
    [string]$BrokerName = "",
    [string]$BrokerMt5Dir = "",
    [int]$Port = 8000,
    [switch]$SkipPowerPlan,
    [switch]$SkipScheduledTask
)

$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $RepoRoot

Write-Host "=== MT5 Trade Copier Setup ===" -ForegroundColor Cyan

# 1. Check Administrator privileges (needed for power settings & boot scheduled task)
$IsAdmin = ([Security.Principal.WindowsPrincipal] [Security.Principal.WindowsIdentity]::GetCurrent()
).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $IsAdmin) {
    Write-Warning "Not running as Administrator. Power plan lid-close hardening and boot Scheduled Task require an elevated PowerShell prompt."
}

# 2. Locate Python 3.11+ and create/update .venv
Write-Host "[1/6] Checking Python 3.11+ environment..."
$PythonCmd = $null
foreach ($candidate in @("py -3.12", "py -3.11", "py -3.14", "python")) {
    try {
        $ver = Invoke-Expression "$candidate -c `"import sys; assert sys.version_info >= (3,11); print(sys.executable)`"" 2>$null
        if ($LASTEXITCODE -eq 0 -and $ver) {
            $PythonCmd = $ver.Trim()
            break
        }
    } catch {}
}
if (-not $PythonCmd) {
    Write-Host "Installing Python 3.12 via winget..."
    winget install -e --id Python.Python.3.12 --accept-package-agreements --accept-source-agreements
    $PythonCmd = (py -3.12 -c "import sys; print(sys.executable)").Trim()
}
Write-Host "Using Python: $PythonCmd"

$VenvPython = Join-Path $RepoRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $VenvPython)) {
    & $PythonCmd -m venv (Join-Path $RepoRoot ".venv") --system-site-packages
}
try {
    & $VenvPython -m pip install --upgrade -r (Join-Path $RepoRoot "requirements.txt")
} catch {
    Write-Warning "pip install reported an issue (offline/cached packages in system-site-packages will be used)."
}

# 3. Create data directory, DPAPI-protected Fernet key, SQLite DB, and lock key file ACLs
Write-Host "[2/6] Initializing data directory, DPAPI encryption key, and SQLite database..."
& $VenvPython -m copier init --no-prompt

$DataDir = if ($env:TC_DATA_DIR) { $env:TC_DATA_DIR } else { Join-Path $env:ProgramData "TradeCopier" }
$SecretsDir = Join-Path $DataDir "secrets"
if (Test-Path $SecretsDir) {
    try {
        & icacls $SecretsDir /inheritance:r /grant:r "*S-1-5-32-544:(OI)(CI)F" /grant:r "*S-1-5-18:(OI)(CI)F" /grant:r "$($env:USERNAME):(OI)(CI)F" | Out-Null
    } catch {
        Write-Warning "Could not tighten ACL on $SecretsDir : $_"
    }
}

# 4. Prompt for Admin Password (entered twice)
Write-Host "[3/6] Setting dashboard admin password..."
& $VenvPython -m copier set-password

# 5. Optional initial broker MT5 template registration
Write-Host "[4/6] Broker MT5 terminal template..."
if (-not $BrokerMt5Dir) {
    $BrokerMt5Dir = Read-Host "Enter path to an installed broker MT5 folder containing terminal64.exe (or press Enter to add later in Dashboard)"
}
if ($BrokerMt5Dir) {
    if (-not $BrokerName) {
        $BrokerName = Read-Host "Enter a name for this broker template (e.g. ICMarkets)"
    }
    if ($BrokerName) {
        & $VenvPython -m copier template add $BrokerName $BrokerMt5Dir
    }
}

# 6. Laptop Power & Lid-Close Hardening (never sleep on AC or battery, lid close = Do Nothing)
if (-not $SkipPowerPlan -and $IsAdmin) {
    Write-Host "[5/6] Applying laptop 24/7 power plan (never sleep, disable lid-close sleep, disable hibernate)..."
    & powercfg /change standby-timeout-ac 0
    & powercfg /change standby-timeout-dc 0
    & powercfg /change hibernate-timeout-ac 0
    & powercfg /change hibernate-timeout-dc 0
    & powercfg /hibernate off
    # Subgroup 4f971e89-eebd-4455-a8de-9e59040e7347 (Power buttons and lid) -> Lid close action 5ca83367-6e45-459f-a27b-476b1d01c936 = 0 (Do nothing)
    & powercfg /setacvalueindex SCHEME_CURRENT 4f971e89-eebd-4455-a8de-9e59040e7347 5ca83367-6e45-459f-a27b-476b1d01c936 0
    & powercfg /setdcvalueindex SCHEME_CURRENT 4f971e89-eebd-4455-a8de-9e59040e7347 5ca83367-6e45-459f-a27b-476b1d01c936 0
    & powercfg /setactive SCHEME_CURRENT
} else {
    Write-Host "[5/6] Skipping power plan changes (run as Administrator without -SkipPowerPlan to apply)."
}

# 7. Register Windows Scheduled Task for auto-start on boot/logon with auto-restart on crash
if (-not $SkipScheduledTask -and $IsAdmin) {
    Write-Host "[6/6] Registering Windows Scheduled Task 'TradeCopier' (auto-start + auto-restart on crash)..."
    $Action = New-ScheduledTaskAction -Execute $VenvPython -Argument "-m copier.supervisor --port $Port" -WorkingDirectory $RepoRoot
    $TriggerBoot = New-ScheduledTaskTrigger -AtStartup
    $TriggerLogon = New-ScheduledTaskTrigger -AtLogOn
    $Settings = New-ScheduledTaskSettingsSet `
        -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries `
        -StartWhenAvailable `
        -ExecutionTimeLimit ([TimeSpan]::Zero) `
        -RestartCount 999 `
        -RestartInterval (New-TimeSpan -Minutes 1)
    Register-ScheduledTask -TaskName "TradeCopier" -Action $Action -Trigger @($TriggerBoot, $TriggerLogon) -Settings $Settings -Force | Out-Null
    Start-ScheduledTask -TaskName "TradeCopier"
    Write-Host "Scheduled Task 'TradeCopier' registered and started." -ForegroundColor Green
} else {
    Write-Host "[6/6] Skipping Scheduled Task registration (run as Administrator to register)."
    Write-Host "You can start the copier manually with: .\.venv\Scripts\python -m copier.supervisor --port $Port"
}

Write-Host ""
Write-Host "Setup complete! Dashboard URL: http://127.0.0.1:$Port" -ForegroundColor Green

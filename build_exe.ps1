<#
.SYNOPSIS
    Builds the standalone Windows executable (dist\TradeCopier.exe) and
    client-ready zip package (dist\TradeCopier-Client.zip).
#>
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root

$Py = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path $Py)) {
    throw "Virtual environment not found at $Py"
}

Write-Host "[1/3] Running PyInstaller to build standalone TradeCopier.exe..." -ForegroundColor Cyan
& $Py -m PyInstaller `
    --noconfirm `
    --clean `
    --onefile `
    --name "TradeCopier" `
    --add-data "copier\web\templates;copier\web\templates" `
    --hidden-import "uvicorn.logging" `
    --hidden-import "uvicorn.loops" `
    --hidden-import "uvicorn.loops.auto" `
    --hidden-import "uvicorn.protocols" `
    --hidden-import "uvicorn.protocols.http" `
    --hidden-import "uvicorn.protocols.http.auto" `
    --hidden-import "uvicorn.protocols.websockets" `
    --hidden-import "uvicorn.protocols.websockets.auto" `
    --hidden-import "uvicorn.lifespan" `
    --hidden-import "uvicorn.lifespan.on" `
    --hidden-import "MetaTrader5" `
    --hidden-import "sqlalchemy.dialects.sqlite" `
    --hidden-import "multipart" `
    --hidden-import "python_multipart" `
    "run_tradecopier.py"

$ExePath = Join-Path $Root "dist\TradeCopier.exe"
if (-not (Test-Path $ExePath)) {
    throw "Build failed: $ExePath was not created."
}

$SizeMB = [Math]::Round((Get-Item $ExePath).Length / 1MB, 2)
Write-Host "[2/3] Built $ExePath ($SizeMB MB)" -ForegroundColor Green

$ZipPath = Join-Path $Root "dist\TradeCopier-Client.zip"
if (Test-Path $ZipPath) { Remove-Item $ZipPath -Force }
Compress-Archive -Path $ExePath -DestinationPath $ZipPath -Force
Write-Host "[3/3] Packaged client zip: $ZipPath" -ForegroundColor Green

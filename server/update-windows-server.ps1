# Update AG Inventory on the server to the newest version from GitHub.
# Your data (data\) and backups (backups\) are kept. A backup is made first.
# Run in PowerShell as Administrator, from the AG_Inventory folder:
#     powershell -ExecutionPolicy Bypass -File server\update-windows-server.ps1
# If you copied the program files by hand instead of using git, add -NoGit.
param([switch]$NoGit)
$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

Write-Host '== Backing up the data first' -ForegroundColor Cyan
& .venv\Scripts\python.exe server\backup_all.py
if ($LASTEXITCODE -ne 0) { Write-Host 'Backup failed, update stopped.' -ForegroundColor Red; exit 1 }

Write-Host '== Stopping AG Inventory' -ForegroundColor Cyan
& "$PSScriptRoot\stop-server.ps1"

if (-not $NoGit) {
    Write-Host '== Getting the new version' -ForegroundColor Cyan
    git pull --ff-only
    if ($LASTEXITCODE -ne 0) {
        Write-Host 'git pull failed. The app is started again on the old version.' -ForegroundColor Red
        Start-ScheduledTask -TaskName 'AG Inventory'
        exit 1
    }
}

Write-Host '== Updating packages' -ForegroundColor Cyan
& .venv\Scripts\python.exe -m pip install -r requirements.txt --quiet

Write-Host '== Starting AG Inventory' -ForegroundColor Cyan
Start-ScheduledTask -TaskName 'AG Inventory'
Write-Host 'Done. The database is updated automatically on start. Check logs\server.log if a page does not open.' -ForegroundColor Green

# Mizan: one-time setup on Windows Server (or any always-on Windows PC).
#
# Run in PowerShell as Administrator, from the AG_Inventory folder:
#     powershell -ExecutionPolicy Bypass -File server\setup-windows-server.ps1
# Options:
#     -Port 5000                  the port shop devices use (default 5000)
#     -BackupTime 23:00           when the daily backup runs (default 23:00)
#     -BackupCopyTo E:\AG_Backups also keep backup copies on a second disk or network share
#     -NoFirewall                 do not open the port in Windows Firewall (for example behind IIS)
#
# It is safe to run again: it updates what is already there.
param(
    [int]$Port = 5000,
    [string]$BackupTime = '23:00',
    [string]$BackupCopyTo = '',
    [switch]$NoFirewall
)
$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root
$TaskName = 'AG Inventory'
$BackupTaskName = 'AG Inventory daily backup'
$FirewallRule = "AG Inventory (TCP $Port)"

function Step($text) { Write-Host ''; Write-Host "== $text" -ForegroundColor Cyan }

$admin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $admin) {
    Write-Host 'Please run PowerShell as Administrator (right-click PowerShell > Run as administrator).' -ForegroundColor Red
    exit 1
}

Step 'Checking Python'
$python = $null
foreach ($cmd in @('py', 'python')) {
    if (Get-Command $cmd -ErrorAction SilentlyContinue) {
        $args3 = @(); if ($cmd -eq 'py') { $args3 = @('-3') }
        try {
            & $cmd @args3 -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" 2>$null
            if ($LASTEXITCODE -eq 0) { $python = @($cmd) + $args3; break }
        } catch { }
    }
}
if (-not $python) {
    Write-Host 'Python 3.10 or newer was not found. Install it from https://www.python.org/downloads/' -ForegroundColor Red
    Write-Host 'and tick "Add python.exe to PATH" and "Install for all users", then run this script again.'
    exit 1
}
$pyExe = $python[0]; $pyArgs = @(); if ($python.Count -gt 1) { $pyArgs = $python[1..($python.Count - 1)] }
& $pyExe @pyArgs --version

Step 'Installing the app (virtual environment and packages)'
if (-not (Test-Path '.venv\Scripts\python.exe')) { & $pyExe @pyArgs -m venv .venv }
& .venv\Scripts\python.exe -m pip install --upgrade pip --quiet
& .venv\Scripts\python.exe -m pip install -r requirements.txt --quiet
if ($LASTEXITCODE -ne 0) { Write-Host 'Installing packages failed. Check the internet connection and try again.' -ForegroundColor Red; exit 1 }

Step "Creating the '$TaskName' task (starts at boot, no login needed)"
$action = New-ScheduledTaskAction -Execute 'cmd.exe' -Argument "/c `"`"$Root\server\run-server.bat`" $Port`"" -WorkingDirectory $Root
$trigger = New-ScheduledTaskTrigger -AtStartup
$principal = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest
$settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 999 `
    -RestartInterval (New-TimeSpan -Minutes 1) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Principal $principal `
    -Settings $settings -Description 'Runs the Mizan web app' -Force | Out-Null

Step "Creating the '$BackupTaskName' task (every day at $BackupTime)"
$backupArgs = "`"$Root\server\backup_all.py`""
if ($BackupCopyTo) { $backupArgs += " --copy-to `"$BackupCopyTo`"" }
$bAction = New-ScheduledTaskAction -Execute "$Root\.venv\Scripts\python.exe" -Argument $backupArgs -WorkingDirectory $Root
$bTrigger = New-ScheduledTaskTrigger -Daily -At $BackupTime
$bSettings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskName $BackupTaskName -Action $bAction -Trigger $bTrigger -Principal $principal `
    -Settings $bSettings -Description 'Backs up all Mizan data to backups\server' -Force | Out-Null

if (-not $NoFirewall) {
    Step "Opening port $Port in Windows Firewall"
    Get-NetFirewallRule -DisplayName $FirewallRule -ErrorAction SilentlyContinue | Remove-NetFirewallRule
    New-NetFirewallRule -DisplayName $FirewallRule -Direction Inbound -Protocol TCP -LocalPort $Port `
        -Action Allow -Profile Domain, Private | Out-Null
}

Step 'Starting Mizan'
& "$PSScriptRoot\stop-server.ps1" -Quiet
Start-ScheduledTask -TaskName $TaskName
$ok = $false
for ($i = 0; $i -lt 30 -and -not $ok; $i++) {
    Start-Sleep -Seconds 1
    try { Invoke-WebRequest "http://localhost:$Port/login" -UseBasicParsing -TimeoutSec 3 | Out-Null; $ok = $true } catch { }
}
$ips = Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
    Where-Object { $_.IPAddress -notlike '127.*' -and $_.IPAddress -notlike '169.254.*' } | ForEach-Object { $_.IPAddress }
Write-Host ''
if ($ok) {
    Write-Host 'Mizan is running.' -ForegroundColor Green
    Write-Host "  On this server:      http://localhost:$Port"
    foreach ($ip in $ips) { Write-Host "  From shop devices:   http://${ip}:$Port" }
    Write-Host 'It starts by itself whenever the server starts. Log: logs\server.log'
} else {
    Write-Host 'The task was created, but the app did not answer yet. Look in logs\server.log for the reason.' -ForegroundColor Yellow
}

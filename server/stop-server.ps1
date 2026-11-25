# Stop AG Inventory on the server: the scheduled task and every copy of serve.py still running
# (also copies started by hand with start.bat). Start it again with:
#     Start-ScheduledTask -TaskName 'AG Inventory'
# Run in PowerShell as Administrator:
#     powershell -ExecutionPolicy Bypass -File server\stop-server.ps1
param([switch]$Quiet)
$TaskName = 'AG Inventory'
if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
}
# the task's run-server.bat window, then the Python processes running serve.py
Get-CimInstance Win32_Process -Filter "Name='cmd.exe'" |
    Where-Object { $_.CommandLine -like '*run-server.bat*' } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
$found = Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='pythonw.exe'" |
    Where-Object { $_.CommandLine -like '*serve.py*' }
foreach ($p in $found) { Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue }
if (-not $Quiet) { Write-Host "AG Inventory stopped ($(@($found).Count) app process(es) ended)." }

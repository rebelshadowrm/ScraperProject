param([string]$MonitorDirectory = '', [string]$RepairSnapshot = '')
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
if (-not $MonitorDirectory) {
    $MonitorDirectory = Join-Path $projectRoot ('Tests/full_pull_monitor/run_' + (Get-Date -Format 'yyyyMMdd_HHmmss'))
}
$monitorRoot = [System.IO.Path]::GetFullPath($MonitorDirectory)
New-Item -ItemType Directory -Force -Path $monitorRoot | Out-Null
$pythonPath = Join-Path $projectRoot '.venv/Scripts/python.exe'
# Native redirection avoids PowerShell formatting large stderr streams during
# long pulls. The scraper records provider coverage in Sheets/PullRuns separately.
$pullArguments = @('-u', 'main.py', '--all', '--mode', 'snapshot')
if ($RepairSnapshot) {
    $manifestPath = [System.IO.Path]::GetFullPath($RepairSnapshot)
    if (-not (Test-Path -LiteralPath $manifestPath -PathType Leaf)) { throw 'Repair manifest does not exist.' }
    $pullArguments = @('-u', 'main.py', '--repair-snapshot', ('"' + $manifestPath + '"'))
}
$pullProcess = Start-Process -FilePath $pythonPath -ArgumentList $pullArguments `
    -WorkingDirectory $projectRoot -WindowStyle Hidden -PassThru `
    -RedirectStandardOutput (Join-Path $monitorRoot 'stdout.log') `
    -RedirectStandardError (Join-Path $monitorRoot 'stderr.log')
$pullProcess.Id | Set-Content -LiteralPath (Join-Path $monitorRoot 'process_id.txt')
$processHandle = $pullProcess.Handle
Write-Output ('Full pull PID ' + $pullProcess.Id + '; logs: ' + $monitorRoot)
$pullProcess.WaitForExit()
$pullProcess.Refresh()
if ($null -eq $pullProcess.ExitCode) { throw 'Full pull exit code is unavailable; inspect its manifest.' }
$pullProcess.ExitCode | Set-Content -LiteralPath (Join-Path $monitorRoot 'exit_code.txt')
Write-Output ('Full pull exit code: ' + $pullProcess.ExitCode)
exit $pullProcess.ExitCode

param([int]$Port = 8766, [switch]$NoBrowser)
$ErrorActionPreference = 'Stop'
$taskRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$python = Join-Path $taskRoot '.venv\Scripts\python.exe'
$url = "http://127.0.0.1:$Port"
$env:PILOT01_EXECUTION_BACKEND = 'api'
$ready = $false
try { $ready = (Invoke-RestMethod "$url/api/health" -TimeoutSec 2).app -eq 'pilot01-console' } catch { }
if (-not $ready) {
    $logDirectory = Join-Path $taskRoot 'outputs\experiment_console\server'
    New-Item -ItemType Directory -Force -Path $logDirectory | Out-Null
    $logId = Get-Date -Format 'yyyyMMdd-HHmmss'
    $process = Start-Process -FilePath $python -ArgumentList @('-m','pilot01.experiment.console','--port',"$Port") -WorkingDirectory $taskRoot -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $logDirectory "$logId.stdout.log") -RedirectStandardError (Join-Path $logDirectory "$logId.stderr.log")
    for ($i = 0; $i -lt 60; $i++) {
        Start-Sleep -Milliseconds 250
        try { $ready = (Invoke-RestMethod "$url/api/health" -TimeoutSec 1).app -eq 'pilot01-console' } catch { }
        if ($ready) { break }
        if ($process.HasExited) { throw "Console did not start. Check $logDirectory" }
    }
    if (-not $ready) { throw "Console is not ready. Check $logDirectory" }
}
if (-not $NoBrowser) { Start-Process $url }
Write-Output "Experiment console: $url (opening the console does not call the model API)."

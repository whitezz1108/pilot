param(
    [ValidatePattern('^[A-Za-z0-9_.-]+$')]
    [string]$Server = 't640',
    [ValidatePattern('^/[A-Za-z0-9_./-]+$')]
    [string]$RemoteRoot = '/home/evan/ai/staging/protocol-refactor-native-v2-20261101',
    [ValidateRange(1024, 65535)]
    [int]$LocalPort = 8766,
    [switch]$NoBrowser
)

$ErrorActionPreference = 'Stop'
$localHealth = "http://127.0.0.1:$LocalPort/api/health"

function Test-ConsoleHealth([string]$Url) {
    try {
        $response = Invoke-RestMethod -Uri $Url -TimeoutSec 2
        return $response.app -eq 'pilot01-console' -and $response.ready -eq $true
    }
    catch {
        return $false
    }
}

if (-not (Test-ConsoleHealth $localHealth)) {
    $remoteHealth = & ssh -o BatchMode=yes -o ConnectTimeout=8 $Server `
        'curl -fsS http://127.0.0.1:8766/api/health' 2>$null
    if ($LASTEXITCODE -ne 0) {
        $remoteCommand = "cd '$RemoteRoot' && mkdir -p outputs/experiment_console/server && " +
            "nohup env PYTHONPATH='$RemoteRoot/src' PILOT01_EXECUTION_BACKEND=local-gpu " +
            "PILOT01_GPU_MONITOR_MODE=local PYTHONDONTWRITEBYTECODE=1 " +
            "'$RemoteRoot/.venv/bin/python' -m pilot01.experiment.console --port 8766 " +
            ">outputs/experiment_console/server/lan-console.stdout.log " +
            "2>outputs/experiment_console/server/lan-console.stderr.log </dev/null &"
        & ssh -o BatchMode=yes -o ConnectTimeout=8 $Server $remoteCommand
        if ($LASTEXITCODE -ne 0) {
            throw "Could not start the experiment console on $Server. Check the remote path, virtual environment, and SSH access."
        }
        $ready = $false
        foreach ($attempt in 1..30) {
            Start-Sleep -Milliseconds 500
            & ssh -o BatchMode=yes -o ConnectTimeout=8 $Server `
                'curl -fsS http://127.0.0.1:8766/api/health' 2>$null | Out-Null
            if ($LASTEXITCODE -eq 0) {
                $ready = $true
                break
            }
        }
        if (-not $ready) {
            throw "The remote console was not ready within 15 seconds. Check $RemoteRoot/outputs/experiment_console/server/lan-console.stderr.log"
        }
    }

    $forward = "${LocalPort}:127.0.0.1:8766"
    $tunnel = Start-Process -FilePath 'ssh' `
        -ArgumentList @('-N', '-o', 'BatchMode=yes', '-o', 'ExitOnForwardFailure=yes', '-L', $forward, $Server) `
        -WindowStyle Hidden -PassThru
    $ready = $false
    foreach ($attempt in 1..20) {
        Start-Sleep -Milliseconds 250
        if ($tunnel.HasExited) {
            throw "The SSH tunnel exited with code $($tunnel.ExitCode). Check the local port and SSH connection."
        }
        if (Test-ConsoleHealth $localHealth) {
            $ready = $true
            break
        }
    }
    if (-not $ready) {
        Stop-Process -Id $tunnel.Id -ErrorAction SilentlyContinue
        throw 'The SSH tunnel started, but the console health endpoint is not reachable.'
    }
    Write-Host "SSH tunnel process PID: $($tunnel.Id). Stop that process to disconnect local access."
}

Write-Host "Pilot01 experiment console: http://127.0.0.1:$LocalPort"
if (-not $NoBrowser) {
    Start-Process "http://127.0.0.1:$LocalPort"
}

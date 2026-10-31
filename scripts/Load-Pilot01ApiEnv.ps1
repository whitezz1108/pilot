param(
    [string]$Path = (Join-Path $PSScriptRoot '..\config\api.local.env')
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$resolvedPath = (Resolve-Path -LiteralPath $Path).Path
$allowedNames = @('PILOT01_BASE_URL', 'PILOT01_API_KEY', 'PILOT01_MODEL_ID')
$settings = @{}

foreach ($rawLine in [System.IO.File]::ReadAllLines($resolvedPath)) {
    $line = $rawLine.Trim()
    if ($line.Length -eq 0 -or $line.StartsWith('#')) {
        continue
    }
    if ($line -notmatch '^(PILOT01_[A-Z0-9_]+)=(.*)$') {
        throw "Invalid setting line in $resolvedPath. Expected NAME=value."
    }

    $name = $Matches[1]
    $value = $Matches[2].Trim()
    if ($name -notin $allowedNames -and $name -notmatch '^PILOT01_(BASE_URL|API_KEY)_([2-9]|[12][0-9]|3[0-2])$') {
        throw "Unsupported setting $name in $resolvedPath."
    }
    if ($settings.ContainsKey($name)) {
        throw "Duplicate setting $name in $resolvedPath."
    }
    if ($value.Length -ge 2 -and (
        ($value.StartsWith('"') -and $value.EndsWith('"')) -or
        ($value.StartsWith("'") -and $value.EndsWith("'"))
    )) {
        $value = $value.Substring(1, $value.Length - 2)
    }
    $settings[$name] = $value
}

foreach ($name in $allowedNames) {
    if (-not $settings.ContainsKey($name) -or [string]::IsNullOrWhiteSpace($settings[$name])) {
        throw "Set $name in $resolvedPath before loading the API environment."
    }
}

$baseUrl = $settings['PILOT01_BASE_URL']
$parsedUrl = $null
if (-not [System.Uri]::TryCreate($baseUrl, [System.UriKind]::Absolute, [ref]$parsedUrl) -or
    $parsedUrl.Scheme -notin @('http', 'https')) {
    throw 'PILOT01_BASE_URL must be an absolute http or https URL.'
}
if ($parsedUrl.AbsolutePath.TrimEnd('/') -match '/chat/completions$') {
    throw 'PILOT01_BASE_URL must end before /chat/completions; the adapter appends that path.'
}

foreach ($name in $settings.Keys) {
    if ($name -match '^PILOT01_(BASE_URL|API_KEY)_([2-9]|[12][0-9]|3[0-2])$') {
        $slot = $Matches[2]
        if (-not $settings.ContainsKey("PILOT01_BASE_URL_$slot") -or
            -not $settings.ContainsKey("PILOT01_API_KEY_$slot") -or
            [string]::IsNullOrWhiteSpace($settings["PILOT01_API_KEY_$slot"])) {
            throw "API slot $slot requires both BASE_URL and API_KEY settings."
        }
        $slotUrl = $null
        if (-not [System.Uri]::TryCreate($settings["PILOT01_BASE_URL_$slot"], [System.UriKind]::Absolute, [ref]$slotUrl) -or
            $slotUrl.Scheme -notin @('http', 'https') -or
            $slotUrl.AbsolutePath.TrimEnd('/') -match '/chat/completions$') {
            throw "Invalid BASE_URL for API slot $slot."
        }
    }
}

foreach ($name in $settings.Keys) {
    [System.Environment]::SetEnvironmentVariable($name, $settings[$name], 'Process')
}
Write-Host 'Pilot API settings loaded into this PowerShell process. No API call was made.'

param([ValidateSet('api','frontend','test','validate')][string]$Task='api')
$ErrorActionPreference='Stop'
Set-Location (Split-Path $PSScriptRoot -Parent)
$taskPython = if (Test-Path '.tools/python/python.exe') { (Resolve-Path '.tools/python/python.exe').Path } else { 'python' }
$env:SAM_CLI_TELEMETRY='0'
$env:__SAM_CLI_APP_DIR=Join-Path (Get-Location) '.tools\sam-config'
switch ($Task) {
    'api' { & $taskPython -m backend.local --seed }
    'frontend' { Push-Location frontend; try { npm.cmd run dev } finally { Pop-Location } }
    'test' { & $taskPython -m pytest -q }
    'validate' { & $taskPython -m samcli validate --lint; if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }; Push-Location frontend; try { npm.cmd run build } finally { Pop-Location } }
}

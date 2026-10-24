[CmdletBinding()]
param(
    [string]$SessionId,
    [string]$Python = 'python',
    [string]$Model = '',
    [switch]$Register,
    [switch]$TakeWriter,
    [switch]$Background
)
$ErrorActionPreference = 'Stop'
if ($Register -and -not $SessionId -and -not $Background) { throw '-Register requires -SessionId or -Background' }
if ($Background -and ($SessionId -or $Model -or $TakeWriter)) { throw '-Background cannot be combined with desktop binding options' }
$venvPython = Join-Path $PSScriptRoot '.venv/Scripts/python.exe'
if (-not (Test-Path -LiteralPath $venvPython)) {
    & $Python -c 'import sys; assert sys.version_info >= (3,11), "Python 3.11+ required"'
    if ($LASTEXITCODE -ne 0) { throw 'Install Python 3.11+ or pass -Python with its absolute executable path' }
    & $Python -m venv (Join-Path $PSScriptRoot '.venv')
    if ($LASTEXITCODE -ne 0) { throw 'Failed to create Python environment' }
}
& $venvPython -m pip install -r (Join-Path $PSScriptRoot 'requirements.txt')
if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed' }
$configArgs = @((Join-Path $PSScriptRoot 'configure.py'))
if ($Background) {
    $configArgs += '--background'
    if ($Register) { $configArgs += '--register' }
} elseif ($SessionId) {
    $configArgs += @('--session-id', $SessionId)
    if ($Model) { $configArgs += @('--model', $Model) }
    if ($Register) { $configArgs += '--register' }
    if ($TakeWriter) { $configArgs += '--take-writer' }
} else {
    $configArgs += '--list'
}
& $venvPython @configArgs
if ($LASTEXITCODE -ne 0) { throw 'Configuration failed; see the message above' }
if (-not $SessionId -and -not $Background) {
    Write-Host 'Choose a dedicated chat from the list, then run:'
    Write-Host '.\setup.ps1 -SessionId <UUID> -Register'
}

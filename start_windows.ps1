param(
    [string]$PythonBin = "python",
    [string]$VenvDir = ".venv",
    [string]$BindHost = "127.0.0.1",
    [int]$Port = 8000
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

if (-not (Get-Command $PythonBin -ErrorAction SilentlyContinue)) {
    throw "Python not found: $PythonBin"
}

if (-not (Test-Path $VenvDir)) {
    & $PythonBin -m venv $VenvDir
}

$venvPython = Join-Path $VenvDir "Scripts\python.exe"
if (-not (Test-Path $venvPython)) {
    throw "Venv python not found: $venvPython"
}

& $venvPython -m pip install --upgrade pip
& $venvPython -m pip install -r requirements.txt

$env:PYTHONUTF8 = "1"
Write-Host "Starting API on http://$BindHost`:$Port"
& $venvPython -m uvicorn rest_api:app --host $BindHost --port $Port

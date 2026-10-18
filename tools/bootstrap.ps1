[CmdletBinding()]
param([string]$Python = 'python', [switch]$Bambu)
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$venv = Join-Path $root '.venv'
if (Test-Path -LiteralPath $venv) { throw 'The .venv directory already exists. Activate it instead; bootstrap does not overwrite an environment.' }
& $Python -m venv $venv
if ($LASTEXITCODE -ne 0) { throw 'Python 3.11 or newer is required.' }
$runtime = Join-Path $venv 'Scripts/python.exe'
$package = if ($Bambu) { "$root[bambu]" } else { $root }
& $runtime -m pip install $package
if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed. Inspect the error; the environment is preserved.' }
Write-Host "Installed. Run: & '$venv\Scripts\inkboard.exe' init --directory `"`$env:LOCALAPPDATA\Inkboard\home`" --wizard"

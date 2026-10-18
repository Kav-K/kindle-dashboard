[CmdletBinding()]
param([string]$Python = 'python', [switch]$Bambu)
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$venv = Join-Path $root '.venv'
if (Test-Path -LiteralPath $venv) { throw 'The .venv directory already exists. Activate it instead; bootstrap does not overwrite an environment.' }
$pythonHelp = 'Python 3.11 or newer is required. Install Python with Add python.exe to PATH enabled, open a new terminal, or rerun with -Python the full path to python.exe. No environment was created.'
try { & $Python -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' }
catch { throw $pythonHelp }
if ($LASTEXITCODE -ne 0) { throw $pythonHelp }
& $Python -m venv $venv
if ($LASTEXITCODE -ne 0) { throw 'Virtual environment creation failed. Inspect the Python error before retrying.' }
$runtime = Join-Path $venv 'Scripts/python.exe'
$package = if ($Bambu) { "$root[bambu]" } else { $root }
& $runtime -m pip install $package
if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed. Inspect the error; the environment is preserved.' }
$quotedCommand = (Join-Path $venv 'Scripts\inkboard.exe').Replace("'", "''")
Write-Host "Installed. Run: & '$quotedCommand' init --directory `"`$env:LOCALAPPDATA\Inkboard\home`" --wizard"

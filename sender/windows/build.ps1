# Windows Python 3.11/3.12 and pip must be installed.
$ErrorActionPreference = "Stop"
Set-Location (Resolve-Path (Join-Path $PSScriptRoot "..\.."))
python -m pip install -r sender\windows\requirements.txt
if ($LASTEXITCODE -ne 0) { throw "Python dependencies failed" }
python -m PyInstaller --noconfirm --clean --onefile --windowed --name CalSync --paths sender\windows sender\windows\app.py
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed" }
Write-Host "Executable: dist\CalSync.exe"

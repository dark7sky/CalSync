# Build CalSync.exe inside a project-local virtual environment (.venv).
# Corporate network: pip must use the system proxy explicitly and trust the
# SSL-intercepting proxy for pypi.org (CA pinning fails otherwise).
$ErrorActionPreference = "Stop"
Set-Location (Resolve-Path (Join-Path $PSScriptRoot "..\.."))

$venv = Join-Path (Get-Location) ".venv"
if (-not (Test-Path (Join-Path $venv "Scripts\python.exe"))) {
    Write-Host "Creating virtual environment: $venv"
    python -m venv $venv
    if ($LASTEXITCODE -ne 0) { throw "venv creation failed" }
}
$py = Join-Path $venv "Scripts\python.exe"

$proxy = ""
try {
    $reg = Get-ItemProperty "HKCU:\Software\Microsoft\Windows\CurrentVersion\Internet Settings" -ErrorAction SilentlyContinue
    if ($reg -and $reg.ProxyEnable -and $reg.ProxyServer) {
        $proxy = [string]$reg.ProxyServer
        if ($proxy -notmatch "^https?://") { $proxy = "http://$proxy" }
    }
} catch { }

$pipArgs = @("-m", "pip", "install", "--upgrade", "pip")
if ($proxy) { $pipArgs += @("--proxy", $proxy, "--trusted-host", "pypi.org", "--trusted-host", "files.pythonhosted.org") }
& $py @pipArgs
if ($LASTEXITCODE -ne 0) { throw "pip upgrade failed" }

$pipArgs = @("-m", "pip", "install", "-r", "sender\windows\requirements.txt")
if ($proxy) { $pipArgs += @("--proxy", $proxy, "--trusted-host", "pypi.org", "--trusted-host", "files.pythonhosted.org") }
& $py @pipArgs
if ($LASTEXITCODE -ne 0) { throw "Python dependencies failed" }

& $py -m PyInstaller --noconfirm --clean --onefile --windowed --name CalSync --icon sender\windows\assets\CalSync.ico --paths sender\windows --collect-all playwright --copy-metadata keyring --add-data "$venv\Lib\site-packages\playwright\driver;playwright\driver" --add-data "sender\windows\assets\CalSync.ico;assets" sender\windows\app.py
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed" }
Write-Host "Executable: dist\CalSync.exe"

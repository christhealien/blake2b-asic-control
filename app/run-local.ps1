# Run Blake2b ASIC Control on Windows, no Umbrel or Docker needed.
#
#   Right-click this file -> "Run with PowerShell"   (or: powershell -ExecutionPolicy Bypass -File run-local.ps1)
#   Other devices on your network too:  powershell -ExecutionPolicy Bypass -File run-local.ps1 -Listen 0.0.0.0
#
# Needs: Git for Windows and Python 3.10+ (tick "Add python.exe to PATH" when installing).
# Everything goes into .\local next to this script. Delete .\local to start over.
param([string]$Listen = "127.0.0.1", [int]$Port = 8787)
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
$L = Join-Path $PSScriptRoot "local"
$Repo = if ($env:UPSTREAM_REPO) { $env:UPSTREAM_REPO } else { "https://github.com/Maveth/goldshell-config.git" }
$Ref = "92838b350f324bfca2d1b70eef48b0861927fbe5"

if (-not (Get-Command git -ErrorAction SilentlyContinue)) { throw "git is needed: install Git for Windows first" }
$py = (Get-Command python -ErrorAction SilentlyContinue)
if (-not $py) { $py = (Get-Command py -ErrorAction SilentlyContinue) }
if (-not $py) { throw "Python is needed: install Python 3.10+ and tick 'Add to PATH'" }

$built = Join-Path $L "app\.built"
if (-not (Test-Path (Join-Path $L "app\webui\static\profiles.html")) -or
    ((Get-Item "install_addons.py").LastWriteTime -gt (Get-Item $built -ErrorAction SilentlyContinue).LastWriteTime)) {
  Write-Host "== setting up (the dashboard + this add-on)"
  Remove-Item -Recurse -Force (Join-Path $L "app"), (Join-Path $L "src") -ErrorAction SilentlyContinue
  New-Item -ItemType Directory -Force (Join-Path $L "app"), (Join-Path $L "data\tuner") | Out-Null
  git clone -q $Repo (Join-Path $L "src")
  git -C (Join-Path $L "src") checkout -q $Ref
  Copy-Item -Recurse (Join-Path $L "src\sc-lite\webui") (Join-Path $L "app\webui")
  Copy-Item -Recurse (Join-Path $L "src\sc-lite\python") (Join-Path $L "app\python")
  Remove-Item -Recurse -Force (Join-Path $L "src")
  $files = "tuner_addon.py","tuner.html","fan_addon.py","profiles.html","schedule_addon.py","shares_addon.py","health_addon.py","miner_safety.py","notify_addon.py","rejects_addon.py","hashrate_addon.py","report_addon.py","schedule.html","addon.js",
           "auth_addon.py","login.html","theme.css","theme.js","widget_server.py","install_addons.py"
  foreach ($f in $files) { Copy-Item $f (Join-Path $L "app\webui") }
  Copy-Item "asic_tuner.py" (Join-Path $L "app\python")
  Push-Location (Join-Path $L "app\webui"); & $py.Source install_addons.py | Out-Null; Pop-Location
  New-Item -ItemType File -Force $built | Out-Null
  Write-Host "   add-on installed"
}

$vpy = Join-Path $L "venv\Scripts\python.exe"
$haveCrypto = $false
if (Test-Path $vpy) { & $vpy -c "import Crypto" 2>$null; $haveCrypto = ($LASTEXITCODE -eq 0) }
if (-not $haveCrypto) {
  Write-Host "== creating a Python environment (one time)"
  if (-not (Test-Path $vpy)) { & $py.Source -m venv (Join-Path $L "venv") }
  & $vpy -m pip install -q --upgrade pip
  & $vpy -m pip install -q "pycryptodome>=3.18"
  if ($LASTEXITCODE -ne 0) { throw "couldn't install pycryptodome (needs internet)" }
}

New-Item -ItemType Directory -Force (Join-Path $L "data\tuner") | Out-Null
$mj = Join-Path $L "data\miners.json"   # start with no miners (not the dashboard's example one)
if (-not (Test-Path $mj)) { '{"fan_defaults": {"profile": "curve-60", "enabled_default": false}, "miners": []}' | Set-Content -Encoding ascii $mj }

$env:SCLITE_WEBUI_HOST = $Listen
$env:SCLITE_WEBUI_PORT = "$Port"
$env:SCLITE_WEBUI_MINERS = Join-Path $L "data\miners.json"
$env:SCLITE_TUNER_DATA = Join-Path $L "data\tuner"
$env:PYTHONUNBUFFERED = "1"
$m = Select-String -Path (Join-Path $PSScriptRoot "Dockerfile") -Pattern "B2AC_VERSION=([0-9.]+)" | Select-Object -First 1
if ($m) { $env:B2AC_VERSION = $m.Matches[0].Groups[1].Value }
Write-Host "== open http://127.0.0.1:$Port  (Ctrl+C to stop)"
Write-Host "   first visit: create a login; add miners under Settings, or show demo miners there"
Set-Location (Join-Path $L "app\webui")
& $vpy -u server.py

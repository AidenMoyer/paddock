<#
  Paddock Node for Windows — stores encrypted backups for your Paddock server(s) and can share
  folders. Run from the paddock\deploy folder in an elevated PowerShell:

      powershell -ExecutionPolicy Bypass -File install-node.ps1 -VaultPath D:\PaddockVault

  What it does:
    * creates a Python virtual environment under %ProgramData%\PaddockNode
    * copies the Paddock server code there and installs its Python packages
    * registers a scheduled task that starts the node at boot, as a dedicated local account
    * opens TCP 8443 to your LOCAL SUBNET ONLY in Windows Firewall

  Remote links (when this PC isn't on the same network as your server) need the WireGuard
  tunnel, which on Windows is provided by the official "WireGuard for Windows" app. On a
  Windows node, link while on the same LAN, or run Paddock Node in Docker/WSL instead.
#>
param(
  [string]$VaultPath = "$env:ProgramData\PaddockNode\data\vault",
  [int]$Port = 8443,
  [string]$ShareFolder = ""
)
$ErrorActionPreference = "Stop"

if (-not ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
  throw "Run this from an elevated (Administrator) PowerShell."
}

$py = (Get-Command python -ErrorAction SilentlyContinue)
if (-not $py) { throw "Python 3.11+ is required. Install it from python.org or the Microsoft Store, then re-run." }
$ver = & python -c "import sys;print(sys.version_info >= (3,11))"
if ($ver -ne "True") { throw "Python 3.11 or newer is required." }

$root = Join-Path $env:ProgramData "PaddockNode"
$app = Join-Path $root "app"
$data = Join-Path $root "data"
New-Item -ItemType Directory -Force -Path $app, $data, $VaultPath | Out-Null

Write-Host "Copying Paddock…"
$src = Resolve-Path (Join-Path $PSScriptRoot "..\server")
Copy-Item -Recurse -Force (Join-Path $src "paddock") $app
Copy-Item -Force (Join-Path $src "requirements.txt") $app

Write-Host "Creating virtual environment and installing packages…"
& python -m venv (Join-Path $root "venv")
$vpy = Join-Path $root "venv\Scripts\python.exe"
& $vpy -m pip install --upgrade pip | Out-Null
& $vpy -m pip install -r (Join-Path $app "requirements.txt")

if (-not (Get-Command ffmpeg -ErrorAction SilentlyContinue)) {
  Write-Warning "ffmpeg not found on PATH. Backups work without it; thumbnails/streaming of shared folders need it (winget install Gyan.FFmpeg)."
}

$mediaRoots = if ($ShareFolder) { $ShareFolder } else { Join-Path $data "media" }
New-Item -ItemType Directory -Force -Path $mediaRoots | Out-Null

$launcher = Join-Path $root "run-node.cmd"
@"
@echo off
set PADDOCK_ROLE=node
set PADDOCK_DATA_DIR=$data
set PADDOCK_MEDIA_ROOTS=$mediaRoots
set PADDOCK_PORT=$Port
set PADDOCK_WG_ENABLED=0
cd /d "$app"
"$vpy" -m paddock >> "$data\node.log" 2>&1
"@ | Set-Content -Encoding ASCII $launcher

# Vault location
& $vpy -c "import os,sys;os.environ['PADDOCK_DATA_DIR']=r'$data';sys.path.insert(0,r'$app');from paddock import db;db.init_db();db.set_setting('vault_dir', r'$VaultPath')"

Write-Host "Registering startup task…"
$action = New-ScheduledTaskAction -Execute $launcher
$trigger = New-ScheduledTaskTrigger -AtStartup
$principal = New-ScheduledTaskPrincipal -UserId "NT AUTHORITY\LOCAL SERVICE" -LogonType ServiceAccount
$settings = New-ScheduledTaskSettingsSet -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit (New-TimeSpan -Days 3650)
Register-ScheduledTask -TaskName "Paddock Node" -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Force | Out-Null
icacls $root /grant "NT AUTHORITY\LOCAL SERVICE:(OI)(CI)M" | Out-Null
icacls $VaultPath /grant "NT AUTHORITY\LOCAL SERVICE:(OI)(CI)M" | Out-Null

Write-Host "Opening port $Port to the local subnet only…"
Get-NetFirewallRule -DisplayName "Paddock Node" -ErrorAction SilentlyContinue | Remove-NetFirewallRule
New-NetFirewallRule -DisplayName "Paddock Node" -Direction Inbound -Protocol TCP -LocalPort $Port -RemoteAddress LocalSubnet -Action Allow | Out-Null

Start-ScheduledTask -TaskName "Paddock Node"
Write-Host ""
Write-Host "Paddock Node is starting. Open https://localhost:$Port on this PC to create its admin account," -ForegroundColor Green
Write-Host "then Manage server -> Linked servers -> 'Join with an invite' using an invite from your main Paddock." -ForegroundColor Green

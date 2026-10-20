# Run Paddock on this laptop for real testing (HTTPS on the LAN, your Videos folder as the media root).
#   powershell -ExecutionPolicy Bypass -File scripts\run-laptop.ps1
$here = Split-Path -Parent $PSScriptRoot
$ip = (Get-NetIPAddress -AddressFamily IPv4 -InterfaceAlias "Wi-Fi" -ErrorAction SilentlyContinue | Select-Object -First 1).IPAddress
$env:PADDOCK_DATA_DIR = Join-Path $here ".laptopdata"
# "This PC": every local drive is browsable from Manage server → Libraries → Browse
$drives = Get-PSDrive -PSProvider FileSystem | Where-Object { $_.Used -ne $null -and $_.Root -match '^[A-Z]:\\$' } | ForEach-Object { $_.Root }
$env:PADDOCK_MEDIA_ROOTS = ($drives -join ';')
$env:PADDOCK_HOST = "0.0.0.0"
$env:PADDOCK_PORT = "8443"
$env:PADDOCK_TLS = "1"
$env:PADDOCK_HOSTNAMES = "$ip,localhost"
$env:PADDOCK_WG_ENABLED = "0"
# put the Android app where /download/android serves it
$apk = Join-Path $here "..\android\app\build\outputs\apk\debug\app-debug.apk"
$dl = Join-Path $env:PADDOCK_DATA_DIR "downloads"
New-Item -ItemType Directory -Force -Path $dl | Out-Null
if (Test-Path $apk) { Copy-Item -Force $apk (Join-Path $dl "paddock.apk") }
Set-Location $here
& (Join-Path $here ".venv\Scripts\python.exe") -m paddock

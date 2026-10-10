# Start the TrustLane server.
#   .\run.ps1            -> this laptop only, 20 s match window (good for demos)
#   .\run.ps1 -Lan       -> also reachable from a phone on the same Wi-Fi
#   .\run.ps1 -Window 60 -> production-like 60 s match window
param([int]$Window = 20, [switch]$Lan)

$hostAddr = if ($Lan) { "0.0.0.0" } else { "127.0.0.1" }
$env:TRUSTLANE_MATCH_WINDOW = "$Window"
if ($Lan) {
    $ip = (Get-NetIPAddress -AddressFamily IPv4 | Where-Object { $_.PrefixOrigin -eq "Dhcp" } | Select-Object -First 1).IPAddress
    Write-Host "Phone app: http://${ip}:8000/phone"
}
Write-Host "Open http://localhost:8000  (match window: $Window s)"
& "$PSScriptRoot\.venv\Scripts\python.exe" -m uvicorn server.main:create_app --factory --app-dir $PSScriptRoot --host $hostAddr --port 8000

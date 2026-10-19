# Start the TrustLane server.
#   .\run.ps1              -> this laptop only, 20 s match window (good for demos)
#   .\run.ps1 -Lan         -> also reachable from phones on the same Wi-Fi (and from Docker monitoring)
#   .\run.ps1 -Lan -Https  -> HTTPS with a self-signed certificate (phone camera scanning needs https)
#   .\run.ps1 -Window 60   -> production-like 60 s match window
param([int]$Window = 20, [switch]$Lan, [switch]$Https)

$py = "$PSScriptRoot\.venv\Scripts\python.exe"
$hostAddr = if ($Lan) { "0.0.0.0" } else { "127.0.0.1" }
$scheme = if ($Https) { "https" } else { "http" }
$env:TRUSTLANE_MATCH_WINDOW = "$Window"
$tls = @()
if ($Https) {
    if (-not (Test-Path "$PSScriptRoot\data\tls\cert.pem")) { & $py -m server.devcert }
    $tls = @("--ssl-certfile", "$PSScriptRoot\data\tls\cert.pem", "--ssl-keyfile", "$PSScriptRoot\data\tls\key.pem")
}
if ($Lan) {
    $ip = (Get-NetIPAddress -AddressFamily IPv4 | Where-Object { $_.PrefixOrigin -eq "Dhcp" } | Select-Object -First 1).IPAddress
    Write-Host "Phone app: ${scheme}://${ip}:8000/phone"
}
Write-Host "Open ${scheme}://localhost:8000  (match window: $Window s)"
& $py -m uvicorn server.main:create_app --factory --app-dir $PSScriptRoot --host $hostAddr --port 8000 @tls

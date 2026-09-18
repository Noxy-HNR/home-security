# Windows: create .env from .env.example with generated passwords. Safe to re-run: it only
# fills values that are still empty. (Linux/macOS: scripts/setup.sh does the same.)
#   powershell -ExecutionPolicy Bypass -File scripts\setup.ps1
$ErrorActionPreference = "Stop"
Set-Location (Split-Path $PSScriptRoot -Parent)

if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    Write-Error "Install Docker Desktop first: https://docs.docker.com/desktop/setup/install/windows-install/"
}

function New-Secret([int]$Length = 24) {
    $chars = [char[]]"ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789"
    $bytes = New-Object byte[] $Length
    [System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
    -join ($bytes | ForEach-Object { $chars[$_ % $chars.Length] })
}

if (-not (Test-Path .env)) {
    Copy-Item .env.example .env
    Write-Host "Created .env"
}
$lines = [System.Collections.Generic.List[string]](Get-Content .env)

function Get-Var([string]$Key) {
    $line = $lines | Where-Object { $_ -match "^$Key=" } | Select-Object -Last 1
    if ($line) { $line.Substring($Key.Length + 1) } else { "" }
}
function Set-Var([string]$Key, [string]$Value) {
    $i = $lines.FindIndex([Predicate[string]]{ param($l) $l -match "^$Key=" })
    if ($i -ge 0) { $lines[$i] = "$Key=$Value" } else { $lines.Add("$Key=$Value") }
}

if (-not (Get-Var "MQTT_PASSWORD")) { Set-Var "MQTT_PASSWORD" (New-Secret 24) }
if (-not (Get-Var "DASHBOARD_PASSWORD")) {
    $pw = New-Secret 16
    Set-Var "DASHBOARD_PASSWORD" $pw
    Write-Host "Generated DASHBOARD_PASSWORD: $pw   (change it in .env if you like)"
}
# LF endings and no BOM, so docker compose and the containers read it cleanly
[System.IO.File]::WriteAllText((Join-Path (Get-Location) ".env"), ($lines -join "`n") + "`n", (New-Object System.Text.UTF8Encoding $false))

Write-Host @"

Next (docs/RUNBOOK.md has the details):
  1. In .env set NVR_HOST, NVR_RTSP_USER, NVR_RTSP_PASSWORD
  2. docker compose run --rm --no-deps dashboard python -m app.probe    # finds the stream URLs
  3. Paste the NVR_MAIN_URL / NVR_SUB_URL / CAMERAS lines it prints into .env
  4. docker compose up -d --build
"@

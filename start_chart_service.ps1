# start_chart_service.ps1
# One-click startup: loads Polygon creds, launches Flask, launches ngrok,
# then prints the live public URL to paste into n8n.
#
# FIRST-TIME SETUP:
#   1. Edit the paths below ($ProjectDir, $NgrokExe) to match your machine.
#   2. Create a file called polygon.env.ps1 in the SAME folder as this script
#      (NOT inside your git repo / n8n export) containing exactly:
#        $env:POLYGON_API_KEY = "your_polygon_api_key_here"
#      Keeping it separate means your key never ends up in a script you
#      might later share, back up, or commit somewhere.
#
# EVERY TIME AFTER: just double-click run.bat (see bottom of this file's
# companion) or right-click this .ps1 -> Run with PowerShell.

$ErrorActionPreference = "Stop"

# ---- EDIT THESE TWO PATHS FOR YOUR MACHINE ----
$ProjectDir = "C:\Users\Yazeed\Downloads\chart server polygon"
$NgrokExe   = "C:\Users\Yazeed\Downloads\chart server polygon\ngrok.exe"
# ------------------------------------------------

$CredsFile = Join-Path $PSScriptRoot "polygon.env.ps1"
$FlaskPort = 5001

if (-not (Test-Path $CredsFile)) {
    Write-Host "Missing $CredsFile - create it first (see comment at top of this script)." -ForegroundColor Red
    exit 1
}

Write-Host "Loading Polygon credentials..." -ForegroundColor Cyan
. $CredsFile   # dot-source so $env:POLYGON_API_KEY lands in THIS script's scope

if (-not $env:POLYGON_API_KEY) {
    Write-Host "polygon.env.ps1 didn't set POLYGON_API_KEY. Check the file." -ForegroundColor Red
    exit 1
}

# ---- Start Flask in its own window, passing the env vars through ----
Write-Host "Starting chart_service.py on port $FlaskPort..." -ForegroundColor Cyan
$flaskProc = Start-Process powershell -PassThru -WindowStyle Normal -ArgumentList @(
    "-NoExit", "-Command",
    "cd '$ProjectDir'; " +
    "`$env:POLYGON_API_KEY='$($env:POLYGON_API_KEY)'; " +
    "python chart_service.py"
)

# ---- Wait until Flask is actually accepting connections ----
Write-Host "Waiting for Flask to come up..." -ForegroundColor Cyan
$flaskReady = $false
for ($i = 0; $i -lt 30; $i++) {
    Start-Sleep -Seconds 1
    try {
        $null = Invoke-WebRequest -Uri "http://127.0.0.1:$FlaskPort/generate-chart" -Method Options -TimeoutSec 2 -ErrorAction Stop
        $flaskReady = $true
        break
    } catch {
        # Flask up but route rejects OPTIONS is still "up" - a connection refusal is not
        if ($_.Exception.Message -notmatch "actively refused") { $flaskReady = $true; break }
    }
}
if (-not $flaskReady) {
    Write-Host "Flask didn't come up after 30s - check the Flask window for errors." -ForegroundColor Red
    exit 1
}
Write-Host "Flask is up." -ForegroundColor Green

# ---- Make sure ngrok's CRL check won't block startup ----
# Some networks/firewalls block ngrok's certificate-revocation check, which
# makes the agent hang in "reconnecting (failed to fetch CRL...)" forever
# and never open a tunnel. Setting crl_noverify fixes that.
#
# Rather than trying to surgically patch the existing YAML (fragile - one
# earlier attempt at that left a stale bad line in place), we just REBUILD
# the config file from scratch in guaranteed-valid v3 format, pulling the
# authtoken/api_key out of whatever was there before with a plain regex so
# nothing gets lost.
$NgrokConfigDir = Join-Path $env:LOCALAPPDATA "ngrok"
$NgrokConfigFile = Join-Path $NgrokConfigDir "ngrok.yml"
try {
    if (-not (Test-Path $NgrokConfigDir)) {
        New-Item -ItemType Directory -Path $NgrokConfigDir -Force | Out-Null
    }

    $existingAuthtoken = $null
    $existingApiKey = $null
    if (Test-Path $NgrokConfigFile) {
        $rawContent = Get-Content -Path $NgrokConfigFile -Raw
        if ($rawContent -match "authtoken\s*:\s*(\S+)") { $existingAuthtoken = $matches[1] }
        if ($rawContent -match "api_key\s*:\s*(\S+)") { $existingApiKey = $matches[1] }
    }

    $newConfig = New-Object System.Collections.Generic.List[string]
    $newConfig.Add("version: 3")
    $newConfig.Add("agent:")
    $newConfig.Add("  crl_noverify: true")
    if ($existingAuthtoken) { $newConfig.Add("  authtoken: $existingAuthtoken") }
    if ($existingApiKey) { $newConfig.Add("  api_key: $existingApiKey") }

    Set-Content -Path $NgrokConfigFile -Value $newConfig
} catch {
    Write-Host "Couldn't auto-patch ngrok config for CRL check (non-fatal): $($_.Exception.Message)" -ForegroundColor DarkYellow
}

# ---- Start ngrok, capturing its output to a log file we can read ----
# (instead of only printing to its own separate window, which is easy to
# miss and means someone has to go read it manually)
Write-Host "Starting ngrok..." -ForegroundColor Cyan

# Kill any ngrok.exe left running from an earlier failed attempt - since we
# run it hidden, a crashed/aborted previous run can leave one orphaned in
# the background, which then locks the log file and/or holds port 4040.
Get-Process -Name "ngrok" -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
Start-Sleep -Milliseconds 500

$NgrokLogFile = Join-Path $PSScriptRoot "ngrok.log"
Remove-Item $NgrokLogFile -Force -ErrorAction SilentlyContinue
Remove-Item "$NgrokLogFile.err" -Force -ErrorAction SilentlyContinue
$ngrokProc = Start-Process $NgrokExe -PassThru -WindowStyle Hidden `
    -ArgumentList "http $FlaskPort --log=stdout --log-format=logfmt" `
    -RedirectStandardOutput $NgrokLogFile -RedirectStandardError "$NgrokLogFile.err"

# ---- Poll ngrok's local API until the tunnel is registered ----
# Bumped from 20s to 60s: ngrok can be slow to come up, especially right
# after a network hiccup like a CRL fetch retry.
Write-Host "Waiting for ngrok tunnel (up to 60s)..." -ForegroundColor Cyan
$publicUrl = $null
$lastError = $null
for ($i = 0; $i -lt 60; $i++) {
    Start-Sleep -Seconds 1
    try {
        $tunnels = Invoke-RestMethod -Uri "http://127.0.0.1:4040/api/tunnels" -TimeoutSec 2
        $https = $tunnels.tunnels | Where-Object { $_.proto -eq "https" } | Select-Object -First 1
        if ($https) { $publicUrl = $https.public_url; break }
    } catch {
        $lastError = $_.Exception.Message
    }
    if ($ngrokProc.HasExited) {
        Write-Host ""
        Write-Host "ngrok.exe exited unexpectedly (exit code $($ngrokProc.ExitCode))." -ForegroundColor Red
        Write-Host "---- ngrok's own log output: ----" -ForegroundColor Red
        if (Test-Path $NgrokLogFile) { Get-Content $NgrokLogFile | Write-Host -ForegroundColor Yellow }
        if (Test-Path "$NgrokLogFile.err") { Get-Content "$NgrokLogFile.err" | Write-Host -ForegroundColor Yellow }
        Write-Host "----------------------------------" -ForegroundColor Red
        Write-Host "That text above is the real reason it quit (common causes: invalid/missing authtoken, port 4040 already in use, or free-plan session-limit already reached by another running ngrok). Send that log text back if you need help reading it." -ForegroundColor Yellow
        exit 1
    }
}

if (-not $publicUrl) {
    Write-Host "Couldn't read the ngrok URL automatically after 60s." -ForegroundColor Yellow
    Write-Host "Check ngrok.log in this folder - if it still says 'reconnecting (failed to fetch CRL...)', your network/firewall/VPN is blocking crl.ngrok.com and needs to allowlist it." -ForegroundColor Yellow
    if ($lastError) { Write-Host "Last error talking to ngrok's local API: $lastError" -ForegroundColor DarkYellow }
    Write-Host "Otherwise, copy the https URL shown in the ngrok window and use that + /generate-chart manually." -ForegroundColor Yellow
    exit 1
}

$endpoint = "$publicUrl/generate-chart"
Set-Clipboard -Value $endpoint

Write-Host ""
Write-Host "READY." -ForegroundColor Green
Write-Host "Chart endpoint: $endpoint" -ForegroundColor Green
Write-Host "(already copied to your clipboard - paste into the n8n HTTP Request node URL field)" -ForegroundColor Green
Write-Host ""
Write-Host "Leave the Flask window open (ngrok now runs hidden in the background - its activity is logged to ngrok.log in this folder if you ever need to check it). Close this window whenever." -ForegroundColor DarkGray

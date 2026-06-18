# One command to run the VFS Malta slot checker locally (CDP method).
#
#   .\run.ps1
#
# It:
#   1. Launches your real Chrome with remote debugging (if not already running).
#   2. Waits for the CDP endpoint to come up.
#   3. Runs the bot, which attaches to that Chrome, waits for the VFS login form
#      (handling the Cloudflare spinner / Turnstile checkbox), logs in and reads
#      the slots.
#
# Just solve any Cloudflare "Verify you are human" checkbox in the Chrome window
# if it asks — the bot waits up to 2 minutes for the login form.

$ErrorActionPreference = "Stop"
$Root       = $PSScriptRoot
$ChromePath = "C:\Program Files\Google\Chrome\Application\chrome.exe"
$DebugPort  = 9222
$ProfileDir = Join-Path $env:LOCALAPPDATA "vfs-chrome-debug-profile"
$StartUrl   = "https://visa.vfsglobal.com/are/en/mlt/login"
$Python     = Join-Path $Root ".venv\Scripts\python.exe"

function Test-Cdp {
    try {
        Invoke-RestMethod -Uri "http://127.0.0.1:$DebugPort/json/version" -TimeoutSec 2 | Out-Null
        return $true
    } catch { return $false }
}

# 1. Launch Chrome with debugging only if it's not already up.
if (Test-Cdp) {
    Write-Host "Chrome (debug) already running on port $DebugPort."
} else {
    if (-not (Test-Path $ChromePath)) {
        throw "Chrome not found at $ChromePath. Edit `$ChromePath in run.ps1."
    }
    Write-Host "Launching Chrome with remote debugging..."
    Start-Process -FilePath $ChromePath -ArgumentList @(
        "--remote-debugging-port=$DebugPort",
        "--user-data-dir=$ProfileDir",
        "--no-first-run",
        "--no-default-browser-check",
        $StartUrl
    )

    # 2. Wait for CDP to come up (up to ~20s).
    Write-Host "Waiting for Chrome to be ready..."
    for ($i = 0; $i -lt 20; $i++) {
        if (Test-Cdp) { break }
        Start-Sleep -Seconds 1
    }
    if (-not (Test-Cdp)) { throw "Chrome debug endpoint never came up on port $DebugPort." }
}

Write-Host ""
Write-Host "Chrome ready. If a Cloudflare 'Verify you are human' box appears, tick it."
Write-Host "Running the slot checker..."
Write-Host ""

# 3. Run the bot (it attaches via CDP per config.ini).
& $Python -m src.main

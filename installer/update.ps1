# comfy-mcp updater for Windows (PowerShell 5.1+ / PowerShell 7).
# Usage:
#   .\update.ps1              # interactive: show changes, ask for confirmation
#   .\update.ps1 -Check       # only show whether updates exist (changes nothing)
#   .\update.ps1 -Yes         # update without questions (for scripts)
#   .\update.ps1 -NoKill      # do not stop stale LM Studio MCP processes
#
# Safe by construction: PC_URL lives in local.env (untracked), watcher state
# (.lmstudio_seen.json) and VERSION are untracked too — tracked files always
# match origin, so reset --hard loses no settings and never conflicts.
param(
    [switch]$Check,
    [switch]$Yes,
    [switch]$NoKill
)
$ErrorActionPreference = "Stop"
$Dir = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)  # repo root
Set-Location $Dir
$DefaultPC = "http://192.168.1.218:8188"

if (-not (Test-Path (Join-Path $Dir ".git"))) {
    Write-Error "Not a git clone - update.ps1 cannot work here. Re-clone and run installer/install.ps1."
}

git fetch origin main --quiet
$Local = git rev-parse --short HEAD
$Remote = git rev-parse --short origin/main

if ($Local -eq $Remote) {
    $v = if (Test-Path "VERSION") { Get-Content VERSION -Raw } else { "unknown" }
    Write-Host "Already up to date: $Local (install: $($v.Trim()))"
    exit 0
}

Write-Host "Update: $Local -> $Remote. New commits:"
git log --oneline "HEAD..origin/main" | Select-Object -First 20
if ($Check) {
    Write-Host "-Check: nothing changed. Run .\update.ps1 -Yes to update."
    exit 0
}

# migration for old installs: if the installer baked a non-default PC_URL into
# the server files, preserve it into local.env before reset --hard
if (-not (Test-Path "local.env")) {
    $hit = Select-String -Path "comfy_mcp_server.py","edit_mcp_server.py","upscale_mcp_server.py" `
        -Pattern 'PC\s*=\s*"(http://[^"]+)"' | Select-Object -First 1
    if ($hit) {
        $baked = $hit.Matches[0].Groups[1].Value
        if ($baked -and $baked -ne $DefaultPC) {
            Set-Content -Path "local.env" -Value "COMFY_PC_URL=$baked" -Encoding ASCII
            Write-Host "Migration: previous address $baked saved to local.env"
        }
    }
}

if (-not $Yes) {
    $ans = Read-Host "Update to $Remote? [y/N]"
    if ($ans -notmatch '^[yY]') { Write-Host "Cancelled."; exit 0 }
}

git reset --hard origin/main | Out-Null
git rev-parse --short HEAD | Set-Content VERSION
Write-Host "Code updated: $(Get-Content VERSION)"

$VenvPython = Join-Path $Dir ".venv\Scripts\python.exe"
if (-not (Test-Path $VenvPython)) {
    Write-Error ".venv not found - run installer/install.ps1 first (creates venv and registers mcp.json)."
}
& $VenvPython -m pip install -q "mcp<2" websocket-client Pillow trimesh matplotlib scikit-image
if ($LASTEXITCODE -ne 0) { Write-Host "WARNING: pip sync failed - check manually" }

if (-not $NoKill) {
    Get-CimInstance Win32_Process -Filter "Name like 'python%'" |
        Where-Object { $_.CommandLine -match '(comfy_mcp_server|edit_mcp_server|upscale_mcp_server|comfy_3d_mcp_server)\.py' } |
        ForEach-Object {
            Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
            Write-Host "stopped stale MCP process $($_.ProcessId)"
        }
}

$PC = $DefaultPC
if (Test-Path "local.env") {
    $line = (Get-Content "local.env" | Where-Object { $_ -match '^COMFY_PC_URL=' } | Select-Object -First 1)
    if ($line) { $PC = $line.Substring("COMFY_PC_URL=".Length).Trim() }
}
try {
    Invoke-WebRequest -Uri "$PC/system_stats" -TimeoutSec 5 -UseBasicParsing | Out-Null
    Write-Host "ComfyUI reachable: $PC"
} catch {
    Write-Host "WARNING: ComfyUI ($PC) did not respond - code updated, render path unverified."
}

Write-Host ""
Write-Host "Done: $Dir @ $(Get-Content VERSION)"
Write-Host "Restart the LM Studio MCP session (Developer -> MCP -> Restart) to pick up the new code."

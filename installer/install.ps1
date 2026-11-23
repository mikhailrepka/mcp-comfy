# comfy-mcp installer for Windows (PowerShell 5.1+ / PowerShell 7).
# Usage:
#   .\install.ps1 <PC_URL>
#   .\install.ps1                                # default http://localhost:8188
# One computer (LM Studio + ComfyUI together): use http://localhost:8188
param(
    [string]$PC_URL = "http://localhost:8188"
)

$ErrorActionPreference = "Stop"
$Dir = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)  # repo root
Set-Location $Dir

foreach ($f in @("comfy_mcp_server.py", "edit_mcp_server.py", "upscale_mcp_server.py", "jobstore.py")) {
    if (-not (Test-Path (Join-Path $Dir $f))) {
        Write-Error "Not found: $Dir\$f - the script must live in the installer folder inside the comfy-mcp repo"
    }
}

# 1. python >= 3.10
function Get-Python {
    $cands = @()
    foreach ($v in @("3.15", "3.14", "3.13", "3.12", "3.11", "3.10")) {
        $cands += "python$v"
    }
    $cands += @("python")
    foreach ($cand in $cands) {
        $cmd = Get-Command $cand -ErrorAction SilentlyContinue
        if ($cmd) {
            $ver = & $cmd.Source -c "import sys; print(1 if sys.version_info >= (3,10) else 0)" 2>$null
            if ($ver -eq "1") { return $cmd.Source }
        }
    }
    # Windows: also try the py launcher with explicit versions
    $py = Get-Command py -ErrorAction SilentlyContinue
    if ($py) {
        foreach ($v in @("3.14", "3.13", "3.12", "3.11", "3.10")) {
            $ver = & py "-$v" -c "import sys; print(1 if sys.version_info >= (3,10) else 0)" 2>$null
            if ($ver -eq "1") { return "py -$v" }
        }
    }
    return $null
}

$Py = Get-Python
if (-not $Py) {
    Write-Host "Python >= 3.10 not found."
    Write-Host "Install a supported version from https://www.python.org/downloads/ (check 'Add python.exe to PATH')"
    Write-Host "or: winget install Python.Python.3.14"
    Write-Host "Then re-run .\install.ps1"
    exit 1
}
Write-Host "Python: $Py"

# 2. venv + dependencies
$VenvPython = Join-Path $Dir ".venv\Scripts\python.exe"
if (-not (Test-Path $VenvPython)) {
    Invoke-Expression "$Py -m venv `"$Dir\.venv`""
}
& $VenvPython -m pip install -q "mcp<2" websocket-client Pillow
if ($LASTEXITCODE -ne 0) { Write-Error "pip install failed" }
Write-Host "venv ready: $Dir\.venv"

# 3. ComfyUI URL -> local.env (untracked) + VERSION (short commit).
#    Server files are NOT modified: they read local.env at startup, so tracked
#    files always match origin and updates are a clean git pull.
Set-Content -Path (Join-Path $Dir "local.env") -Value "COMFY_PC_URL=$PC_URL" -Encoding ASCII
Write-Host "PC_URL -> local.env (COMFY_PC_URL=$PC_URL)"
$Ver = git rev-parse --short HEAD 2>$null
if (-not $Ver) { $Ver = "unknown" }
Set-Content -Path (Join-Path $Dir "VERSION") -Value $Ver
Write-Host "Install version: $Ver"

# 4. working folders
foreach ($dirName in @("comfy_images", "comfy_edit_inbox")) {
    $p = Join-Path $env:USERPROFILE $dirName
    if (-not (Test-Path $p)) { New-Item -ItemType Directory -Path $p | Out-Null }
}

# 5. register in ~/.lmstudio/mcp.json (merge, backup)
$McpPath = Join-Path $env:USERPROFILE ".lmstudio\mcp.json"
$legacy = @("comfy_mcp_server", "edit_mcp_server")
$Servers = [ordered]@{}
$Servers["comfy-image-gen"] = @{ command = $VenvPython; args = @((Join-Path $Dir "comfy_mcp_server.py")) }
$Servers["comfy-image-edit"] = @{ command = $VenvPython; args = @((Join-Path $Dir "edit_mcp_server.py")) }
$Servers["comfy-image-upscale"] = @{ command = $VenvPython; args = @((Join-Path $Dir "upscale_mcp_server.py")) }

if (Test-Path $McpPath) {
    Copy-Item $McpPath "$McpPath.bak" -Force
    $Cfg = Get-Content $McpPath -Raw | ConvertFrom-Json
} else {
    $Cfg = [pscustomobject]@{ mcpServers = [pscustomobject]@{} }
    New-Item -ItemType Directory -Path (Split-Path -Parent $McpPath) -Force | Out-Null
}
if (-not $Cfg.PSObject.Properties["mcpServers"]) {
    $Cfg | Add-Member -NotePropertyName mcpServers -NotePropertyValue ([pscustomobject]@{})
}
foreach ($k in @($Cfg.mcpServers.PSObject.Properties.Name)) {
    if ($legacy -contains $k) { $Cfg.mcpServers.PSObject.Properties.Remove($k) }
}
foreach ($name in $Servers.Keys) {
    $entry = $Servers[$name]
    if ($Cfg.mcpServers.PSObject.Properties[$name]) {
        $Cfg.mcpServers.PSObject.Properties[$name].Value = $entry
    } else {
        $Cfg.mcpServers | Add-Member -NotePropertyName $name -NotePropertyValue ([pscustomobject]$entry)
    }
}
$Cfg | ConvertTo-Json -Depth 10 | Set-Content -Path $McpPath -Encoding UTF8
Write-Host "Registered in $McpPath : $($Servers.Keys -join ', ')"
if (Test-Path "$McpPath.bak") { Write-Host "Backup of previous config: $McpPath.bak" }

# 6. check ComfyUI
try {
    $resp = Invoke-WebRequest -Uri "$PC_URL/system_stats" -TimeoutSec 5 -UseBasicParsing
    Write-Host "ComfyUI reachable: $PC_URL"
} catch {
    Write-Host "WARNING: $PC_URL did not respond (check that ComfyUI is running and, for remote access, started with --listen 0.0.0.0)"
}

Write-Host ""
Write-Host "Done. Restart LM Studio (Developer -> MCP -> Restart) -"
Write-Host "comfy-image-gen, comfy-image-edit and comfy-image-upscale will appear."
Write-Host "System prompt for the model: see README.md in the repo root."

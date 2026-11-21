# comfy-mcp uninstaller for Windows: removes the servers from ~/.lmstudio/mcp.json
# (venv and repo files are untouched). Config backup: .bak
$ErrorActionPreference = "Stop"

$McpPath = Join-Path $env:USERPROFILE ".lmstudio\mcp.json"
if (-not (Test-Path $McpPath)) { Write-Host "$McpPath not found - nothing to remove"; exit 0 }

Copy-Item $McpPath "$McpPath.bak" -Force
$Cfg = Get-Content $McpPath -Raw | ConvertFrom-Json
$removed = @()
foreach ($k in @($Cfg.mcpServers.PSObject.Properties.Name)) {
    if ($k -like "comfy*") {
        $Cfg.mcpServers.PSObject.Properties.Remove($k)
        $removed += $k
    }
}
$Cfg | ConvertTo-Json -Depth 10 | Set-Content -Path $McpPath -Encoding UTF8
Write-Host "Removed from ${McpPath}: $($removed -join ', ')"
Write-Host "Restart LM Studio for the changes to take effect."

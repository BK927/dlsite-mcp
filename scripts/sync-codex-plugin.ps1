[CmdletBinding()]
param(
  [ValidateSet("local", "cloud")]
  [string]$Profile = "local",
  [string]$Url = ""
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$PluginId = "dlsite-mcp"
$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$UserDirectory = [Environment]::GetFolderPath("UserProfile")
$PluginsRoot = [IO.Path]::GetFullPath((Join-Path $UserDirectory "plugins"))
$MarketplacePath = Join-Path $UserDirectory ".agents\plugins\marketplace.json"
$SkillRoot = Join-Path $UserDirectory ".codex\skills\.system\plugin-creator"
$SkillScripts = Join-Path $SkillRoot "scripts"
$Scaffold = Join-Path $SkillScripts "create_basic_plugin.py"
$Validator = Join-Path $SkillScripts "validate_plugin.py"
$Cachebuster = Join-Path $SkillScripts "update_plugin_cachebuster.py"
$MarketplaceReader = Join-Path $SkillScripts "read_marketplace_name.py"

function Invoke-PluginPython {
  param(
    [Parameter(Mandatory = $true)][string]$Script,
    [string[]]$Arguments = @()
  )
  & uv run --quiet --no-project --with pyyaml python $Script @Arguments
  if ($LASTEXITCODE -ne 0) {
    throw "Plugin helper failed: $Script"
  }
}

function Assert-PluginChildPath {
  param([Parameter(Mandatory = $true)][string]$Path)
  $root = [IO.Path]::GetFullPath($PluginsRoot).TrimEnd(
    [IO.Path]::DirectorySeparatorChar,
    [IO.Path]::AltDirectorySeparatorChar
  )
  $candidate = [IO.Path]::GetFullPath($Path)
  if (-not $candidate.StartsWith($root + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {
    throw "Plugin path escaped the expected plugins directory: $candidate"
  }
  return $candidate
}

foreach ($path in @($Scaffold, $Validator, $Cachebuster, $MarketplaceReader)) {
  if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
    throw "Required plugin-creator helper was not found: $path"
  }
}
foreach ($command in @("uv", "codex")) {
  if (-not (Get-Command $command -ErrorAction SilentlyContinue)) {
    throw "$command is required."
  }
}
if ($Profile -eq "cloud") {
  $parsed = $null
  if (-not [Uri]::TryCreate($Url, [UriKind]::Absolute, [ref]$parsed) -or $parsed.Scheme -ne "https" -or -not $parsed.AbsolutePath.TrimEnd('/').EndsWith('/mcp')) {
    throw "Cloud profile requires an absolute HTTPS -Url ending in /mcp."
  }
}

Invoke-PluginPython -Script $Validator -Arguments @($ProjectRoot)
$MarketplaceName = (& uv run --quiet --no-project --with pyyaml python $MarketplaceReader).Trim()
if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($MarketplaceName)) {
  throw "Could not read the personal marketplace name."
}

$Destination = Assert-PluginChildPath -Path (Join-Path $PluginsRoot $PluginId)
$hasEntry = $false
if (Test-Path -LiteralPath $MarketplacePath) {
  $marketplace = Get-Content -Raw -LiteralPath $MarketplacePath | ConvertFrom-Json
  $hasEntry = @($marketplace.plugins | Where-Object { $_.name -eq $PluginId }).Count -eq 1
}
if (-not $hasEntry) {
  if (Test-Path -LiteralPath $Destination) {
    throw "The plugin directory exists without a marketplace entry; move it aside before first install."
  }
  Push-Location $SkillRoot
  try {
    Invoke-PluginPython -Script $Scaffold -Arguments @($PluginId, "--with-mcp", "--with-marketplace")
  }
  finally {
    Pop-Location
  }
}

$nonce = [Guid]::NewGuid().ToString("N")
$Staging = Assert-PluginChildPath -Path (Join-Path $PluginsRoot ".$PluginId.staging.$nonce")
$Backup = Assert-PluginChildPath -Path (Join-Path $PluginsRoot ".$PluginId.backup.$nonce")
$movedExisting = $false

try {
  New-Item -ItemType Directory -Path (Join-Path $Staging ".codex-plugin") -Force | Out-Null
  Copy-Item -LiteralPath (Join-Path $ProjectRoot ".codex-plugin\plugin.json") `
    -Destination (Join-Path $Staging ".codex-plugin\plugin.json")

  if ($Profile -eq "local") {
    $config = [ordered]@{
      mcpServers = [ordered]@{
        $PluginId = [ordered]@{
          type = "stdio"
          command = "uv"
          args = @("run", "--directory", $ProjectRoot, "python", "-m", "dlsite_mcp.server")
        }
      }
    }
  }
  else {
    $config = [ordered]@{
      mcpServers = [ordered]@{
        $PluginId = [ordered]@{
          type = "http"
          url = $Url
          bearer_token_env_var = "DLSITE_MCP_ACCESS_TOKEN"
        }
      }
    }
  }
  $config | ConvertTo-Json -Depth 8 | Set-Content `
    -LiteralPath (Join-Path $Staging ".mcp.json") -Encoding utf8

  Invoke-PluginPython -Script $Cachebuster -Arguments @($Staging)
  Invoke-PluginPython -Script $Validator -Arguments @($Staging)

  if (Test-Path -LiteralPath $Destination) {
    Move-Item -LiteralPath $Destination -Destination $Backup
    $movedExisting = $true
  }
  try {
    Move-Item -LiteralPath $Staging -Destination $Destination
  }
  catch {
    if ($movedExisting -and -not (Test-Path -LiteralPath $Destination)) {
      Move-Item -LiteralPath $Backup -Destination $Destination
      $movedExisting = $false
    }
    throw
  }
  if ($movedExisting) {
    Remove-Item -LiteralPath $Backup -Recurse -Force
    $movedExisting = $false
  }
}
finally {
  if (Test-Path -LiteralPath $Staging) {
    Remove-Item -LiteralPath $Staging -Recurse -Force
  }
  if ($movedExisting -and -not (Test-Path -LiteralPath $Destination) -and (Test-Path -LiteralPath $Backup)) {
    Move-Item -LiteralPath $Backup -Destination $Destination
  }
}

& codex plugin add "$PluginId@$MarketplaceName"
if ($LASTEXITCODE -ne 0) {
  throw "Codex plugin installation failed."
}

Write-Host "Synchronized $PluginId with the '$Profile' profile." -ForegroundColor Green
Write-Host "Start a new Codex task to load the updated MCP surface."

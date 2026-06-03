param(
    [string]$TunnelName = "",
    [string]$Hostname = "",
    [int]$Port = 0,
    [switch]$InstallService
)

$ErrorActionPreference = "Stop"

function Read-DefaultedValue {
    param(
        [string]$Prompt,
        [string]$Default
    )
    $value = Read-Host "$Prompt [$Default]"
    if ([string]::IsNullOrWhiteSpace($value)) {
        return $Default
    }
    return $value.Trim()
}

function Require-Command {
    param([string]$Name)
    $cmd = Get-Command $Name -ErrorAction SilentlyContinue
    if (-not $cmd) {
        throw "$Name was not found on PATH. Install cloudflared first: https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/"
    }
    return $cmd.Source
}

$ProjectDir = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$ConfigYaml = Join-Path $ProjectDir "config.yaml"

if (-not $TunnelName) {
    $TunnelName = Read-DefaultedValue "Cloudflare tunnel name" "kelsey-state-machine"
}
if (-not $Hostname) {
    $Hostname = Read-Host "Public hostname, for example kelsey.example.com"
}
if ([string]::IsNullOrWhiteSpace($Hostname)) {
    throw "Hostname is required. It must be a DNS name in a domain managed by Cloudflare."
}
$Hostname = $Hostname.Trim()

if ($Port -le 0) {
    $Port = 8000
    if (Test-Path $ConfigYaml) {
        $match = Select-String -Path $ConfigYaml -Pattern "^\s*port\s*:\s*(\d+)" | Select-Object -First 1
        if ($match -and $match.Matches[0].Groups.Count -gt 1) {
            $Port = [int]$match.Matches[0].Groups[1].Value
        }
    }
}

$Cloudflared = Require-Command "cloudflared"
$CloudflaredDir = Join-Path $env:USERPROFILE ".cloudflared"
$TunnelConfigPath = Join-Path $CloudflaredDir "kelsey-config.yml"
$DefaultServiceConfigPath = Join-Path $CloudflaredDir "config.yml"

New-Item -ItemType Directory -Force -Path $CloudflaredDir | Out-Null

Write-Host ""
Write-Host "Project: $ProjectDir"
Write-Host "Origin:  http://localhost:$Port"
Write-Host "Host:    https://$Hostname"
Write-Host "Tunnel:  $TunnelName"
Write-Host ""

Write-Host "Step 1/5: Logging in to Cloudflare if needed..."
Write-Host "A browser window may open. Pick the Cloudflare account and zone that owns $Hostname."
& $Cloudflared tunnel login

Write-Host ""
Write-Host "Step 2/5: Creating or finding named tunnel..."
$tunnelsJson = & $Cloudflared tunnel list --output json 2>$null
$tunnels = @()
if ($LASTEXITCODE -eq 0 -and -not [string]::IsNullOrWhiteSpace($tunnelsJson)) {
    $tunnels = $tunnelsJson | ConvertFrom-Json
}

$tunnel = $tunnels | Where-Object { $_.name -eq $TunnelName } | Select-Object -First 1
if (-not $tunnel) {
    & $Cloudflared tunnel create $TunnelName
    if ($LASTEXITCODE -ne 0) {
        throw "cloudflared tunnel create failed."
    }
    $tunnelsJson = & $Cloudflared tunnel list --output json
    $tunnels = $tunnelsJson | ConvertFrom-Json
    $tunnel = $tunnels | Where-Object { $_.name -eq $TunnelName } | Select-Object -First 1
}
if (-not $tunnel) {
    throw "Could not resolve tunnel id for $TunnelName."
}
$TunnelId = [string]$tunnel.id
$CredentialsFile = Join-Path $CloudflaredDir "$TunnelId.json"
if (-not (Test-Path $CredentialsFile)) {
    throw "Tunnel credentials file was not found: $CredentialsFile"
}

Write-Host "Tunnel id: $TunnelId"

Write-Host ""
Write-Host "Step 3/5: Routing DNS hostname to tunnel..."
& $Cloudflared tunnel route dns $TunnelName $Hostname
if ($LASTEXITCODE -ne 0) {
    throw "cloudflared tunnel route dns failed. Check that the domain is on Cloudflare and you selected the correct zone."
}

Write-Host ""
Write-Host "Step 4/5: Writing cloudflared config..."
$CredentialsYamlPath = $CredentialsFile.Replace("\", "/")
$config = @"
tunnel: $TunnelId
credentials-file: $CredentialsYamlPath
protocol: quic

ingress:
  - hostname: $Hostname
    service: http://localhost:$Port
  - service: http_status:404
"@
$config | Set-Content -Path $TunnelConfigPath -Encoding ascii
Set-Content -Path (Join-Path $ProjectDir "deploy\web_url.txt") -Value "https://$Hostname" -Encoding ascii

Write-Host "Config: $TunnelConfigPath"

if ($InstallService) {
    Write-Host ""
    Write-Host "Step 5/5: Installing cloudflared Windows service..."
    Write-Host "If this fails, re-run this script from an Administrator PowerShell."
    if ((Test-Path $DefaultServiceConfigPath) -and ((Resolve-Path $DefaultServiceConfigPath).Path -ne (Resolve-Path $TunnelConfigPath).Path)) {
        $backupPath = "$DefaultServiceConfigPath.bak-$(Get-Date -Format yyyyMMddHHmmss)"
        Copy-Item -LiteralPath $DefaultServiceConfigPath -Destination $backupPath -Force
        Write-Host "Backed up existing default config to: $backupPath"
    }
    Copy-Item -LiteralPath $TunnelConfigPath -Destination $DefaultServiceConfigPath -Force
    Write-Host "Default service config: $DefaultServiceConfigPath"
    & $Cloudflared service install
    if ($LASTEXITCODE -ne 0) {
        throw "cloudflared service install failed. Try running PowerShell as Administrator."
    }
    Write-Host "Service installed."
} else {
    Write-Host ""
    Write-Host "Step 5/5: Service install skipped."
}

Write-Host ""
Write-Host "Done."
Write-Host "Run tunnel now with:"
Write-Host "  cloudflared tunnel --config `"$TunnelConfigPath`" run"
Write-Host ""
Write-Host "Open:"
Write-Host "  https://$Hostname/?kelsey_token=YOUR_KELSEY_ADMIN_TOKEN"

#Requires -Version 7
<#
.SYNOPSIS
    Authenticate with AWS for the AgentCore + Entra Agent ID PoC.
    Checks current identity first; if already authenticated, skips login.
    Supports both IAM access-key profiles and IAM Identity Center (SSO) profiles.

.PARAMETER AwsConfig
    AWS CLI profile name. Default: agentid-poc

.PARAMETER Region
    AWS region. Default: eu-central-1

.EXAMPLE
    .\aws-auth.ps1
    .\aws-auth.ps1 -AwsConfig my-profile -Region us-east-1
#>
[CmdletBinding()]
param(
    [string]$AwsConfig,
    [string]$Region
)

$ErrorActionPreference = 'Stop'

# Load .env file from repo root if present
$dotenv = Join-Path (Split-Path $PSScriptRoot -Parent) ".env"
if (Test-Path $dotenv) {
    Get-Content $dotenv | Where-Object { $_ -notmatch '^\s*#' -and $_ -match '=' } | ForEach-Object {
        $k, $v = ($_ -split '=', 2) | ForEach-Object { $_.Trim() }
        if (-not [string]::IsNullOrWhiteSpace($k) -and -not [string]::IsNullOrWhiteSpace($v)) {
            Set-Variable -Name "_env_$k" -Value $v -Scope Script
        }
    }
}

# Apply .env fallbacks (command-line params win; .env fills in the rest)
if (-not $AwsConfig) { $AwsConfig = $Script:_env_AWS_PROFILE }
if (-not $Region)    { $Region    = $Script:_env_AWS_REGION }

# Hardcoded defaults for anything still missing
if (-not $AwsConfig) { $AwsConfig = 'agentid-poc' }
if (-not $Region)    { $Region    = 'eu-central-1' }

$env:AWS_PROFILE        = $AwsConfig
$env:AWS_REGION         = $Region
$env:AWS_DEFAULT_REGION = $Region

Write-Host "[aws-auth] Profile: $AwsConfig  Region: $Region"

# --- Check existing identity --------------------------------------------------
Write-Host "Checking current AWS identity..."
$identityJson = aws sts get-caller-identity --profile $AwsConfig --region $Region --output json 2>&1
if ($LASTEXITCODE -eq 0) {
    $identity = $identityJson | ConvertFrom-Json
    Write-Host "Already authenticated:" -ForegroundColor Green
    Write-Host "  Account : $($identity.Account)"
    Write-Host "  UserId  : $($identity.UserId)"
    Write-Host "  Arn     : $($identity.Arn)"
    exit 0
}

# --- Not authenticated - determine profile type and login --------------------
Write-Host "Not authenticated. Attempting login..."

# Check if profile uses SSO (sso_start_url in config)
$profileConfig = aws configure list --profile $AwsConfig 2>&1
$isSso = ($profileConfig | Select-String 'sso').Count -gt 0

if ($isSso) {
    Write-Host "SSO profile detected. Opening browser for sign-in..." -ForegroundColor Cyan
    aws sso login --profile $AwsConfig
    if ($LASTEXITCODE -ne 0) { throw "SSO login failed." }
} else {
    Write-Host "IAM profile detected. Verifying access key configuration..." -ForegroundColor Cyan
    $configured = aws configure get aws_access_key_id --profile $AwsConfig 2>&1
    if (-not $configured -or $LASTEXITCODE -ne 0) {
        Write-Host "No access key found. Running 'aws configure --profile $AwsConfig'..." -ForegroundColor Yellow
        aws configure --profile $AwsConfig
    }
}

# --- Verify identity after login ---------------------------------------------
Write-Host "Verifying identity..."
$identityJson = aws sts get-caller-identity --profile $AwsConfig --region $Region --output json 2>&1
if ($LASTEXITCODE -ne 0) {
    Write-Host $identityJson -ForegroundColor Red
    throw "Authentication failed. Run 'aws configure --profile $AwsConfig' or check SSO settings."
}

$identity = $identityJson | ConvertFrom-Json
Write-Host "Authenticated successfully:" -ForegroundColor Green
Write-Host "  Account : $($identity.Account)"
Write-Host "  UserId  : $($identity.UserId)"
Write-Host "  Arn     : $($identity.Arn)"
Write-Host ""
Write-Host "Next step: .\aws-deploy.ps1 -ArtifactBucket <bucket> -EntraTenantId <id> ..."

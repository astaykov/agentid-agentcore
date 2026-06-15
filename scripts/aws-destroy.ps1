#Requires -Version 7
<#
.SYNOPSIS
    Tear down the AgentCore + Entra Agent ID PoC CloudFormation stack.
    Reads ArtifactBucket/ArtifactKey from stack outputs before deleting,
    then optionally removes the artifact ZIP from S3.
    Safe to run even if the stack does not exist.

.PARAMETER AwsConfig
    AWS CLI profile name. Default: agentid-poc

.PARAMETER Region
    AWS region. Default: eu-central-1

.PARAMETER StackName
    CloudFormation stack name. Default: agentid-poc

.PARAMETER KeepArtifactObject
    If set, skip deleting the agent ZIP from S3 after stack deletion.

.EXAMPLE
    .\aws-destroy.ps1
    .\aws-destroy.ps1 -StackName my-stack -KeepArtifactObject
#>
[CmdletBinding()]
param(
    [string]$AwsConfig,
    [string]$Region,
    [string]$StackName,
    [switch]$KeepArtifactObject
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
if (-not $StackName) { $StackName = $Script:_env_STACK_NAME }

# Hardcoded defaults for anything still missing
if (-not $AwsConfig) { $AwsConfig = 'agentid-poc' }
if (-not $Region)    { $Region    = 'eu-central-1' }
if (-not $StackName) { $StackName = 'agentid-poc' }

$env:AWS_PROFILE        = $AwsConfig
$env:AWS_REGION         = $Region
$env:AWS_DEFAULT_REGION = $Region

$RepoRoot  = (Get-Item (Join-Path $PSScriptRoot '..')).FullName
$stateFile = Join-Path $RepoRoot '.aws-state\agentcore-state.json'

Write-Host "[aws-destroy] Stack: $StackName  Region: $Region  Profile: $AwsConfig"
Write-Host ""

function Invoke-Aws {
    param([string[]]$CliArgs)
    $common = @('--profile', $AwsConfig, '--region', $Region)
    & aws @CliArgs @common
    if ($LASTEXITCODE -ne 0) { throw "aws $($CliArgs -join ' ') failed (exit $LASTEXITCODE)" }
}

# --- Read artifact info from stack outputs or state file ---------------------
$artifactBucket = $null
$artifactKey    = $null
$stackExists    = $false

Write-Host "Checking stack '$StackName'..."
$stackJson = aws cloudformation describe-stacks `
    --stack-name $StackName --profile $AwsConfig --region $Region --output json 2>&1
if ($LASTEXITCODE -eq 0) {
    $stackExists = $true
    $stackData   = $stackJson | ConvertFrom-Json
    $stackStatus = $stackData.Stacks[0].StackStatus
    $outputs     = $stackData.Stacks[0].Outputs

    $bucketOutput = $outputs | Where-Object { $_.OutputKey -eq 'ArtifactKey' }
    $keyOutput    = $outputs | Where-Object { $_.OutputKey -eq 'ArtifactKey' }

    # ArtifactBucket is passed as a parameter, read from stack parameters
    $params = $stackData.Stacks[0].Parameters
    $bParam = $params | Where-Object { $_.ParameterKey -eq 'ArtifactBucket' }
    $kParam = $params | Where-Object { $_.ParameterKey -eq 'ArtifactKey' }
    if ($bParam) { $artifactBucket = $bParam.ParameterValue }
    if ($kParam) { $artifactKey    = $kParam.ParameterValue }

    Write-Host "Stack found (status: $stackStatus)."
    Write-Host "  ArtifactBucket : $artifactBucket"
    Write-Host "  ArtifactKey    : $artifactKey"
} else {
    Write-Host "Stack '$StackName' not found."
    if (Test-Path $stateFile) {
        Write-Host "Reading artifact info from state file..."
        $state = Get-Content $stateFile -Raw | ConvertFrom-Json
        $artifactBucket = $state.ArtifactBucket
        $artifactKey    = $state.ArtifactKey
        Write-Host "  ArtifactBucket : $artifactBucket"
        Write-Host "  ArtifactKey    : $artifactKey"
    }
}

Write-Host ""

# --- Delete the stack ---------------------------------------------------------
if ($stackExists) {
    Write-Host "Deleting stack '$StackName'..."
    Invoke-Aws @('cloudformation', 'delete-stack', '--stack-name', $StackName)

    Write-Host "Waiting for deletion to complete (this may take several minutes)..."
    Invoke-Aws @('cloudformation', 'wait', 'stack-delete-complete', '--stack-name', $StackName)

    Write-Host "Stack deleted."
    Write-Host ""
} else {
    Write-Host "Nothing to delete."
}

# --- Clean up S3 artifact object ---------------------------------------------
if (-not $KeepArtifactObject -and $artifactBucket -and $artifactKey) {
    $s3Uri = "s3://$artifactBucket/$artifactKey"
    Write-Host "Removing artifact $s3Uri ..."
    try {
        aws s3 rm $s3Uri --profile $AwsConfig --region $Region 2>&1 | Out-Null
        Write-Host "Artifact removed."
    } catch {
        Write-Warning "Could not remove artifact: $_"
    }
} elseif ($KeepArtifactObject) {
    Write-Host "Skipping artifact cleanup (-KeepArtifactObject set)."
}

# --- Clean up state file -----------------------------------------------------
if (Test-Path $stateFile) {
    Remove-Item $stateFile -Force
    Write-Host "State file removed."
}

Write-Host ""
Write-Host "Teardown complete."

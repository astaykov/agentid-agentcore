#Requires -Version 7
<#
.SYNOPSIS
    Deploy the AgentCore + Entra Agent ID PoC CloudFormation stack.
    Uses 'aws cloudformation deploy' (idempotent, no-op if nothing changed).
    Auto-creates the S3 artifact bucket if it does not exist, then uploads the ZIP.
    Builds the agent ZIP first (unless -SkipZipBuild is set).

.PARAMETER AwsConfig
    AWS CLI profile name. Default: agentid-poc

.PARAMETER Region
    AWS region. Default: eu-central-1

.PARAMETER StackName
    CloudFormation stack name. Default: agentid-poc

.PARAMETER RuntimeNamePrefix
    Prefix used as the AgentCore Runtime name. Default: agentid_core

.PARAMETER EndpointName
    AgentCore RuntimeEndpoint name. Default: default

.PARAMETER ZipPath
    Path to the agent runtime ZIP (relative to repo root). Default: build/agent-runtime.zip

.PARAMETER PythonRuntime
    AgentCore Python runtime version string. Default: PYTHON_3_12

.PARAMETER BedrockModelId
    Amazon Bedrock model ID. Default: eu.amazon.nova-micro-v1:0

.PARAMETER ArtifactBucket
    S3 bucket name for the agent ZIP artifact. Required.

.PARAMETER ArtifactKey
    S3 key for the agent ZIP artifact. Default: agent-runtime.zip

.PARAMETER DockerPlatform
    Docker --platform flag for build-zip.ps1. Default: linux/amd64

.PARAMETER TargetPlatform
    pip --platform target for ARM64 cross-compilation. Default: manylinux2014_aarch64

.PARAMETER TargetPythonVersion
    Python version for pip cross-compile. Default: 3.12

.PARAMETER TargetAbi
    Python ABI tag for pip cross-compile. Default: cp312

.PARAMETER SkipZipBuild
    Skip running build-zip.ps1 before deploying.

.PARAMETER EntraTenantId
    Microsoft Entra tenant ID (GUID). Required.

.PARAMETER AgentIdentityId
    Entra Agent Identity object ID. Required.

.PARAMETER BlueprintSecretArn
    ARN of Secrets Manager secret for Blueprint credentials. Required.

.PARAMETER EchoApiClientId
    Entra app registration client ID for the Echo API (JWT audience). Required.

.PARAMETER McpServerUrl
    Base URL of the MCP server. Required.

.PARAMETER McpServerScope
    OAuth 2.0 scope for the MCP server. Required.

.PARAMETER EchoApiScope
    OAuth 2.0 scope for the Echo API. Default: api://echo-api/access_as_user

.PARAMETER AgentCoreAppClientId
    Entra app registration client ID that is the token audience (aud) for AgentCore inbound identity.
    Typically the Blueprint app reg client ID. Required.

.EXAMPLE
    .\aws-deploy.ps1 `
        -ArtifactBucket agentid-poc-artifacts `
        -EntraTenantId "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx" `
        -AgentIdentityId "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx" `
        -BlueprintSecretArn "arn:aws:secretsmanager:eu-central-1:123456789012:secret:agentid-poc" `
        -EchoApiClientId "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx" `
        -McpServerUrl "https://mcp.example.com" `
        -McpServerScope "api://mcp-server/.default"
#>
[CmdletBinding()]
param(
    [string]$AwsConfig,
    [string]$Region,
    [string]$StackName,
    [string]$RuntimeNamePrefix,
    [string]$EndpointName,
    [string]$ZipPath,
    [string]$DockerPlatform,
    [string]$TargetPlatform,
    [string]$TargetPythonVersion,
    [string]$TargetAbi,
    [string]$PythonRuntime,
    [string]$BedrockModelId,
    [string]$ArtifactBucket,
    [string]$ArtifactKey,
    [switch]$SkipZipBuild,
    [string]$EntraTenantId,
    [string]$AgentIdentityId,
    [string]$BlueprintSecretArn,
    [string]$EchoApiClientId,
    [string]$McpServerUrl,
    [string]$McpServerScope,
    [string]$EchoApiScope,
    [string]$AgentCoreAppClientId
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
if (-not $AwsConfig)          { $AwsConfig          = $Script:_env_AWS_PROFILE }
if (-not $Region)             { $Region             = $Script:_env_AWS_REGION }
if (-not $StackName)          { $StackName          = $Script:_env_STACK_NAME }
if (-not $RuntimeNamePrefix)  { $RuntimeNamePrefix  = $Script:_env_RUNTIME_NAME_PREFIX }
if (-not $BedrockModelId)     { $BedrockModelId     = $Script:_env_BEDROCK_MODEL_ID }
if (-not $PythonRuntime)      { $PythonRuntime      = $Script:_env_PYTHON_RUNTIME }
if (-not $ArtifactBucket)     { $ArtifactBucket     = $Script:_env_ARTIFACT_BUCKET }
if (-not $ArtifactKey)        { $ArtifactKey        = $Script:_env_ARTIFACT_KEY }
if (-not $ZipPath)            { $ZipPath            = $Script:_env_ZIP_PATH }
if (-not $DockerPlatform)     { $DockerPlatform     = $Script:_env_DOCKER_PLATFORM }
if (-not $TargetPlatform)     { $TargetPlatform     = $Script:_env_TARGET_PLATFORM }
if (-not $TargetPythonVersion){ $TargetPythonVersion= $Script:_env_TARGET_PYTHON_VERSION }
if (-not $TargetAbi)          { $TargetAbi          = $Script:_env_TARGET_ABI }
if (-not $EntraTenantId)      { $EntraTenantId      = $Script:_env_ENTRA_TENANT_ID }
if (-not $AgentIdentityId)    { $AgentIdentityId    = $Script:_env_AGENT_IDENTITY_ID }
if (-not $BlueprintSecretArn) { $BlueprintSecretArn = $Script:_env_BLUEPRINT_SECRET_ARN }
if (-not $EchoApiClientId)   { $EchoApiClientId   = $Script:_env_ECHO_API_CLIENT_ID }
if (-not $EchoApiScope)      { $EchoApiScope       = $Script:_env_ECHO_API_SCOPE }
if (-not $McpServerUrl)       { $McpServerUrl       = $Script:_env_MCP_SERVER_URL }
if (-not $McpServerScope)     { $McpServerScope     = $Script:_env_MCP_SERVER_SCOPE }
if (-not $AgentCoreAppClientId){ $AgentCoreAppClientId = $Script:_env_AGENTCORE_APP_CLIENT_ID }

# Hardcoded defaults for anything still missing
if (-not $AwsConfig)          { $AwsConfig          = 'agentid-poc' }
if (-not $Region)             { $Region             = 'eu-central-1' }
if (-not $StackName)          { $StackName          = 'agentid-poc' }
if (-not $RuntimeNamePrefix)  { $RuntimeNamePrefix  = 'agentid_core' }
if (-not $EndpointName)       { $EndpointName       = 'default' }
if (-not $ZipPath)            { $ZipPath            = 'build/agent-runtime.zip' }
if (-not $DockerPlatform)     { $DockerPlatform     = 'linux/amd64' }
if (-not $TargetPlatform)     { $TargetPlatform     = 'manylinux2014_aarch64' }
if (-not $TargetPythonVersion){ $TargetPythonVersion= '3.12' }
if (-not $TargetAbi)          { $TargetAbi          = 'cp312' }
if (-not $PythonRuntime)      { $PythonRuntime      = 'PYTHON_3_12' }
if (-not $BedrockModelId)     { $BedrockModelId     = 'eu.amazon.nova-micro-v1:0' }
if (-not $ArtifactKey)        { $ArtifactKey        = 'agent-runtime.zip' }
if (-not $EchoApiScope)       { $EchoApiScope       = 'api://echo-api/access_as_user' }

# Validate required parameters (must be resolved via arg, .env, or have a hardcoded default)
# Note: ArtifactBucket is intentionally NOT required here -- it is auto-generated after auth
$missing = @()
if (-not $EntraTenantId)      { $missing += 'EntraTenantId      (ENTRA_TENANT_ID in .env)' }
if (-not $AgentIdentityId)    { $missing += 'AgentIdentityId    (AGENT_IDENTITY_ID in .env)' }
if (-not $BlueprintSecretArn) { $missing += 'BlueprintSecretArn (BLUEPRINT_SECRET_ARN in .env)' }
if (-not $EchoApiClientId)    { $missing += 'EchoApiClientId    (ECHO_API_CLIENT_ID in .env)' }
if (-not $McpServerUrl)       { $missing += 'McpServerUrl       (MCP_SERVER_URL in .env)' }
if (-not $McpServerScope)     { $missing += 'McpServerScope     (MCP_SERVER_SCOPE in .env)' }
if (-not $AgentCoreAppClientId){ $missing += 'AgentCoreAppClientId (AGENTCORE_APP_CLIENT_ID in .env)' }
if ($missing.Count -gt 0) {
    throw "Missing required parameters:`n  $($missing -join "`n  ")`nSet them in .env (copy .env.example) or pass as arguments."
}

$env:AWS_PROFILE        = $AwsConfig
$env:AWS_REGION         = $Region
$env:AWS_DEFAULT_REGION = $Region

$RepoRoot     = (Get-Item (Join-Path $PSScriptRoot '..')).FullName
$TemplateFile = Join-Path $RepoRoot 'infra\cloudformation\stack.yaml'
$ZipPathFull  = Join-Path $RepoRoot ($ZipPath -replace '/', '\')

Write-Host "[aws-deploy] Stack: $StackName  Region: $Region  Profile: $AwsConfig"
Write-Host ""

function Invoke-Aws {
    param([string[]]$CliArgs)
    $common = @('--profile', $AwsConfig, '--region', $Region)
    & aws @CliArgs @common
    if ($LASTEXITCODE -ne 0) { throw "aws $($CliArgs -join ' ') failed (exit $LASTEXITCODE)" }
}

function Invoke-AwsJson {
    param([string[]]$CliArgs)
    $common = @('--profile', $AwsConfig, '--region', $Region, '--output', 'json')
    $result = (& aws @CliArgs @common) | ConvertFrom-Json
    if ($LASTEXITCODE -ne 0) { throw "aws $($CliArgs -join ' ') failed (exit $LASTEXITCODE)" }
    return $result
}

# --- 1. Build ZIP -------------------------------------------------------------
if (-not $SkipZipBuild) {
    Write-Host "--- Step 1: Building agent ZIP (ARM64) ---"
    $buildScript = Join-Path $PSScriptRoot 'build-zip.ps1'
    & $buildScript `
        -OutputZip           $ZipPath `
        -DockerPlatform      $DockerPlatform `
        -TargetPlatform      $TargetPlatform `
        -TargetPythonVersion $TargetPythonVersion `
        -TargetAbi           $TargetAbi
    if ($LASTEXITCODE -ne 0) { throw "ZIP build failed." }
    Write-Host ""
} elseif (-not (Test-Path (Join-Path $RepoRoot ($ZipPath -replace '/', '\')))) {
    throw "ZIP not found at '$ZipPath' and -SkipZipBuild was set."
}

# Resolve ArtifactBucket after auth (auto-generate from account+region if not set)
if (-not $ArtifactBucket) {
    $identity = Invoke-AwsJson @('sts', 'get-caller-identity')
    $accountId = $identity.Account
    $ArtifactBucket = ("agentcore-artifacts-{0}-{1}" -f $accountId, $Region).ToLower()
    Write-Host "ArtifactBucket not set -- using auto-generated: $ArtifactBucket"
}

# --- 2. Ensure S3 artifact bucket exists -------------------------------------
Write-Host "--- Step 2: Ensuring S3 bucket '$ArtifactBucket' exists ---"
$bucketExists = $false
try {
    aws s3api head-bucket --bucket $ArtifactBucket --profile $AwsConfig --region $Region 2>&1 | Out-Null
    $bucketExists = ($LASTEXITCODE -eq 0)
} catch { $bucketExists = $false }

if (-not $bucketExists) {
    Write-Host "Bucket not found. Creating s3://$ArtifactBucket ..."
    if ($Region -eq 'us-east-1') {
        Invoke-Aws @('s3api', 'create-bucket', '--bucket', $ArtifactBucket)
    } else {
        Invoke-Aws @('s3api', 'create-bucket', '--bucket', $ArtifactBucket,
            '--create-bucket-configuration', "LocationConstraint=$Region")
    }
    Write-Host "Bucket created."
} else {
    Write-Host "Bucket already exists."
}
Write-Host ""

# --- 3. Upload ZIP to S3 -----------------------------------------------------
# Compute deployId and versioned key before the upload so both use the same key.
$deployId    = (Get-Date -Format "yyyyMMddHHmmss")
# Use a versioned S3 key so AgentCore always re-downloads the artifact.
# A fixed key (agent-runtime.zip) is NOT enough - AgentCore caches by key name
# and won't reload even when the S3 object is replaced in-place.
$ArtifactKey = "agent-runtime-$deployId.zip"

Write-Host "--- Step 3: Uploading $ZipPath to s3://$ArtifactBucket/$ArtifactKey ---"
if (-not (Test-Path $ZipPathFull)) {
    throw "ZIP not found at $ZipPathFull. Run build-zip.ps1 first or remove -SkipZipBuild."
}
Invoke-Aws @('s3', 'cp', $ZipPathFull, "s3://$ArtifactBucket/$ArtifactKey")
Write-Host ""

# --- 4. Deploy CloudFormation stack ------------------------------------------
Write-Host "--- Step 4: Deploying CloudFormation stack '$StackName' ---"
# Include deployId in the runtime name to force CFN to REPLACE (not update-in-place)
# the AgentRuntime resource on every deploy. AgentCore caches container state per
# runtime ID; only a brand-new runtime guaranteed to load the new S3 artifact.
$RuntimeName = "${RuntimeNamePrefix}_${deployId}"

# If the stack is stuck in ROLLBACK_COMPLETE, delete it first
$stackStatus = $null
try {
    $stackStatus = (aws cloudformation describe-stacks --stack-name $StackName --region $Region --profile $AwsConfig --query "Stacks[0].StackStatus" --output text 2>&1)
} catch {}
if ($stackStatus -match "ROLLBACK_COMPLETE|ROLLBACK_FAILED") {
    Write-Host "Stack is in '$stackStatus' -- deleting before redeploy..."
    Invoke-Aws @('cloudformation', 'delete-stack', '--stack-name', $StackName)
    Invoke-Aws @('cloudformation', 'wait', 'stack-delete-complete', '--stack-name', $StackName)
    Write-Host "Stack deleted. Proceeding with fresh deploy."
}

aws cloudformation deploy `
    --profile           $AwsConfig `
    --region            $Region `
    --template-file     $TemplateFile `
    --stack-name        $StackName `
    --capabilities      CAPABILITY_NAMED_IAM `
    --parameter-overrides `
        "RuntimeName=$RuntimeName" `
        "EndpointName=$EndpointName" `
        "ArtifactBucket=$ArtifactBucket" `
        "ArtifactKey=$ArtifactKey" `
        "PythonRuntime=$PythonRuntime" `
        "BedrockModelId=$BedrockModelId" `
        "EntraTenantId=$EntraTenantId" `
        "AgentIdentityId=$AgentIdentityId" `
        "BlueprintSecretArn=$BlueprintSecretArn" `
        "EchoApiClientId=$EchoApiClientId" `
        "McpServerUrl=$McpServerUrl" `
        "McpServerScope=$McpServerScope" `
        "EchoApiScope=$EchoApiScope" `
        "AgentCoreAppClientId=$AgentCoreAppClientId" `
        "EntraDiscoveryUrl=https://login.microsoftonline.com/$EntraTenantId/v2.0/.well-known/openid-configuration" `
        "DeployId=$deployId"

if ($LASTEXITCODE -ne 0) { throw "CloudFormation deploy failed." }
Write-Host ""

# --- 5. Save state for teardown -----------------------------------------------
$stateDir  = Join-Path $RepoRoot '.aws-state'
$stateFile = Join-Path $stateDir 'agentcore-state.json'
if (-not (Test-Path $stateDir)) { New-Item -ItemType Directory -Path $stateDir | Out-Null }

@{
    StackName      = $StackName
    Region         = $Region
    AwsConfig      = $AwsConfig
    ArtifactBucket = $ArtifactBucket
    ArtifactKey    = $ArtifactKey
} | ConvertTo-Json | Set-Content -Path $stateFile -Encoding UTF8

Write-Host "State saved to $stateFile"
Write-Host ""

# --- 6. Show endpoint info ---------------------------------------------------
Write-Host "--- Stack outputs ---"
aws cloudformation describe-stacks `
    --profile    $AwsConfig `
    --region     $Region `
    --stack-name $StackName `
    --query      'Stacks[0].Outputs' `
    --output     table

# Compute and display the HTTPS invoke URL for the SPA
$outputs = (aws cloudformation describe-stacks `
    --profile $AwsConfig --region $Region --stack-name $StackName `
    --query 'Stacks[0].Outputs' --output json | ConvertFrom-Json)

$runtimeArn = ($outputs | Where-Object { $_.OutputKey -eq 'RuntimeArn' }).OutputValue
$endpointId = ($outputs | Where-Object { $_.OutputKey -eq 'EndpointId' }).OutputValue

if ($runtimeArn -and $endpointId) {
    # URL-encode the ARN for embedding in an HTTPS path
    $encodedArn = [Uri]::EscapeDataString($runtimeArn)
    $invokeUrl = "https://bedrock-agentcore.$Region.amazonaws.com/runtimes/$encodedArn/invocations?qualifier=$endpointId"
    Write-Host ""
    Write-Host "--- SPA agentCoreEndpoint (paste into spa/msal-config.js) ---"
    Write-Host $invokeUrl
}

$echoApiUrl = ($outputs | Where-Object { $_.OutputKey -eq 'EchoApiUrl' }).OutputValue
if ($echoApiUrl) {
    Write-Host ""
    Write-Host "--- Echo API base URL ---"
    Write-Host $echoApiUrl
    Write-Host "  POST $echoApiUrl/echo  (requires Entra Bearer token, audience: $EchoApiClientId)"
    Write-Host "  GET  $echoApiUrl/health (no auth)"
}

Write-Host ""
Write-Host "Deployment complete. Stack '$StackName' is live."
Write-Host "Next: Update the SPA config (spa/msal-config.js) with the agentCoreEndpoint URL above."
Write-Host "Then: run .\scripts\dev.ps1 to start the local dev server."

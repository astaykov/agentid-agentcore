# Deployment Guide — Entra Agent ID + AWS AgentCore PoC

**Audience:** Engineers deploying the PoC. Assumes Entra artefacts already created.

---

## Prerequisites

## What you need from Entra (already created)

Collect these values from the Entra admin center before starting:

| Value | Where to find it |
|---|---|
| **Tenant ID** | Entra admin center → Overview → Tenant ID |
| **Blueprint Client ID** | Entra admin center → Agents → Agent Blueprints → *{your blueprint}* → Blueprint App ID |
| **Blueprint Client Secret** | Value you noted when you created the secret (not visible after creation). Entra admin center → Agents → Agent Blueprints → *{your blueprint}* → Credentials |
| **Agent Identity Object ID** | Entra admin center → Agents → Agent identities → *{your identity}* → Object ID. This is the `fmi_path` / `AGENT_IDENTITY_ID` value. |
| **SPA Client ID** | Entra admin center → App registrations → *agentid-poc-spa* → Application (client) ID |
| **Blueprint App ID URI** | `api://{blueprint-client-id}` — the audience the SPA requests when acquiring a token |
| **Echo API Client ID** | Entra admin center → App registrations → *agentid-poc-echo-api* → Application (client) ID |

### What you need locally

- AWS CLI v2 installed and configured (`aws configure` or a named profile)
- PowerShell 7+ — verify: `pwsh --version`
- Docker Desktop (for local SPA serving and echo API testing)
- Git (optional, to clone this repository)

---

## Step 1: Store Blueprint credentials in AWS Secrets Manager

This is the **only manual AWS step** — run it once before deploying the stack.
The stack references this secret by ARN rather than accepting the plaintext secret as a parameter.

```powershell
# Build the secret JSON and create the secret
$secretValue = (@{
    clientId     = "YOUR_BLUEPRINT_CLIENT_ID"
    clientSecret = "YOUR_BLUEPRINT_CLIENT_SECRET"
} | ConvertTo-Json -Compress)

$secretArn = aws secretsmanager create-secret `
    --name        "agentid-poc/blueprint-credentials" `
    --description "Entra Agent ID Blueprint credentials for AgentCore PoC" `
    --secret-string $secretValue `
    --query "ARN" --output text

Write-Host "Secret ARN: $secretArn"
# SAVE THIS ARN — you need it for the -BlueprintSecretArn parameter in Step 2
```

---

## Step 2: Deploy the CloudFormation stack

The stack is deployed via `deploy.ps1`, which uses change sets and is **safe to re-run** (no-op if nothing has changed).

> **Region note:** `eu-central-1` is supported. The AgentCore control-plane API (`bedrock-agentcore-control`) is available there.

```powershell

.\scripts\aws-deploy.ps1 `
    -EntraTenantId      "YOUR_TENANT_ID" `
    -BlueprintClientId  "YOUR_BLUEPRINT_CLIENT_ID" `
    -BlueprintSecretArn "arn:aws:secretsmanager:REGION:ACCOUNT:secret:agentid-poc/blueprint-credentials-SUFFIX" `
    -McpServerUrl       "https://your-mcp-server.example.com" `
    -McpServerScope     "api://your-mcp-scope/.default" `
    -EchoApiScope       "api://YOUR_ECHO_API_CLIENT_ID/access_as_user" `
    -AgentModelId       "amazon.nova-micro-v1:0"
```

> **Optional parameters with defaults:**
> - `-StackName` — default: `agentid-poc`
> - `-Region` — default: `eu-central-1`
> - `-EchoApiScope` — default: `api://echo-api-client-id/access_as_user` (override with your actual Echo API client ID)
> - `-AgentModelId` — default: `amazon.nova-micro-v1:0`

**What the script does:**

1. Detects whether the stack is new or existing; chooses `CREATE` or `UPDATE` change-set type.
2. Automatically deletes and recreates the stack if it is in `ROLLBACK_COMPLETE`.
3. Submits the change set and waits for it to be ready; exits cleanly if there are no changes.
4. Executes the change set and waits for `CREATE_COMPLETE` / `UPDATE_COMPLETE`.
5. Prints a summary of the key stack outputs on completion.

**Resources provisioned:**

**First deploy takes ~1–2 minutes**

**Check progress at any time:**

```powershell
aws cloudformation describe-stacks `
    --stack-name agentid-poc `
    --query "Stacks[0].StackStatus" --output text
```

---

**Prerequisite:** Docker Desktop must be running (needed for the container build).

**The runtime takes 2–3 minutes to reach READY status** after creation. Poll status:

```powershell
aws bedrock-agentcore-control get-agent-runtime `
    --agent-runtime-id YOUR_RUNTIME_ID --region eu-central-1 --profile agentid-poc
```

---

## Step 3: Collect stack outputs

After the stack reaches `CREATE_COMPLETE` or `UPDATE_COMPLETE`:

```powershell
$outputs = aws cloudformation describe-stacks `
    --stack-name agentid-poc `
    --query "Stacks[0].Outputs" | ConvertFrom-Json

$outputs | Format-Table OutputKey, OutputValue
```

Note these values:

| Output Key | What it is | Used in |
|---|---|---|
| `AgentCoreExecutionRoleArn` | IAM role ARN for AgentCore Runtime | `create-runtime.ps1` (auto-read) |
| `AgentECRRepositoryUri` | ECR repo URI for the agent container | `create-runtime.ps1` (auto-read) |
| `AgentRuntimeEndpoint` | Invocation base URL (`https://bedrock-agentcore.{region}.amazonaws.com`) | `msal-config.js` |
| `SidecarServiceArn` | ARN of the Fargate Entra SDK sidecar ECS service | Reference / troubleshooting |
| `VpcId` | VPC ID | Reference only |

The AgentCore Runtime ARN and ID are shown by `create-runtime.ps1` on successful creation. To retrieve them later:

```powershell
aws bedrock-agentcore-control list-agent-runtimes `
    --region eu-central-1 --profile agentid-poc
```

---

## Step 4: Configure the SPA

Edit `spa/msal-config.js` — replace the four placeholders with real values:

```javascript
const msalConfig = {
  auth: {
    clientId: "YOUR_SPA_CLIENT_ID",      // SPA app registration Client ID
    authority: "https://login.microsoftonline.com/YOUR_TENANT_ID",
    redirectUri: window.location.origin,
  },
  cache: { cacheLocation: "sessionStorage", storeAuthStateInCookie: false }
};

const agentCoreScopes = ["api://YOUR_BLUEPRINT_CLIENT_ID/access_as_user"];
const agentCoreEndpoint = "https://bedrock-agentcore.YOUR_REGION.amazonaws.com"; // AgentRuntimeEndpoint from Step 3
```

**How it works:** The SPA acquires an Entra access token scoped to `api://{blueprint-client-id}/access_as_user` and sends it as `Authorization: Bearer {token}` in the request to AgentCore. AgentCore's JWT authorizer validates the token against the Entra OIDC discovery endpoint (`iss`), the allowed audience (`aud` = `AgentCoreAppClientId`), and the allowed scope (`scp` = `access_agent`). The Python agent then uses the token to call the sidecar for an OBO exchange.

---

## Step 5: Full OBO flow test — with Entra token

This tests the complete Entra Agent ID OBO flow: SPA → AgentCore → sidecar → Echo API.

### Option A — SPA in browser (recommended)

```powershell
# Serve the SPA locally using nginx
docker run --rm -p 3000:80 -v "${PWD}/spa:/usr/share/nginx/html:ro" nginx:alpine
```

1. Open `http://localhost:3000` in a browser.
2. Sign in with an Entra account that has consent for `api://{blueprint-client-id}/access_as_user`.
3. Type a message and click **Send**.
4. The response body from the Echo API is displayed — this confirms the full OBO chain completed.

---

## Step 7: Deploy the Echo REST API (optional)

The Echo API validates the downstream OBO token issued by the sidecar. For quick local testing, run it with Docker:

```powershell
cd echo-api

$env:ENTRA_TENANT_ID    = "YOUR_TENANT_ID"
$env:ECHO_API_CLIENT_ID = "YOUR_ECHO_API_CLIENT_ID"

docker build -t agentid-echo-api .
docker run --rm -p 8080:8080 `
    -e ENTRA_TENANT_ID=$env:ENTRA_TENANT_ID `
    -e ECHO_API_CLIENT_ID=$env:ECHO_API_CLIENT_ID `
    agentid-echo-api
```
---

## Teardown

```powershell

# Deletes the AgentCore Runtime first, then the CloudFormation stack:
.\scripts\aws-destroy.ps1 -StackName "agentid-poc"
# Automatically waits for deletion to complete (~5 minutes)

# If create-runtime.ps1 was never run (no runtime to delete):
.\scripts\aws-destroy.ps1 -SkipRuntimeDelete
```

To also delete the Blueprint credentials secret:

```powershell
aws secretsmanager delete-secret `
    --secret-id "agentid-poc/blueprint-credentials" `
    --force-delete-without-recovery
```

---

## Troubleshooting

| Symptom | Diagnosis & Fix |
|---|---|
| Stack stuck in `ROLLBACK_COMPLETE` | `deploy.ps1` handles this automatically on re-run. To inspect what failed: `aws cloudformation describe-stack-events --stack-name agentid-poc --query "StackEvents[?ResourceStatus=='CREATE_FAILED']"` |
| `401 Unauthorized` from AgentCore | Verify the `EntraTenantId` and `AgentCoreAppClientId` match the issuer and audience in the token. The JWT authorizer checks `iss` against the OIDC discovery URL, `aud` against `AgentCoreAppClientId`, and `scp` against `access_agent`. |
| `No READY endpoints found` from `invoke.ps1` | The AgentCore Runtime may still be initialising. Wait 2–3 minutes after creation and retry. Check runtime status: `aws bedrock-agentcore-control list-agent-runtimes --region eu-central-1 --profile agentid-poc` |
| `Runtime 'agentid-poc' not found` from `invoke.ps1` | Run `create-runtime.ps1` first. |
| Sidecar health check failing (ECS service unstable) | Check sidecar logs in CloudWatch: log group `/ecs/agentid-poc/entra-sidecar`. Common causes: `AGENT_IDENTITY_ID` env var not set, Blueprint secret ARN wrong, or Blueprint client ID mismatch. |
| OBO exchange fails with `invalid_grant` | Ensure the Agent Identity is correctly linked to the Blueprint in Entra admin center (Agents → Agent identities). The `fmi_path` (Agent Identity Object ID) must be the Object ID of the Agent Identity child object, not a client ID. |
| Docker image pull fails for sidecar | `mcr.microsoft.com/entra-sdk/auth-sidecar` requires public internet access. Confirm the NAT Gateway is healthy and the private subnet route table routes `0.0.0.0/0` to the NAT Gateway. |
| `EmptyOnDelete` error during stack teardown | If your AWS region doesn't support `EmptyOnDelete` on ECR repositories, manually delete all images first: `aws ecr batch-delete-image --repository-name agentid-poc-agent --image-ids $(aws ecr list-images --repository-name agentid-poc-agent --query 'imageIds' --output json)` |

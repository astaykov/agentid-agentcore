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

## Step 1: Enable AWS outbound identity federation

This account-level setting has no native CloudFormation resource. Enable it once with
the AWS CLI. The operator needs `iam:EnableOutboundWebIdentityFederation` and
`iam:GetOutboundWebIdentityFederationInfo`:

```powershell
aws iam enable-outbound-web-identity-federation --profile agentid-poc

aws iam get-outbound-web-identity-federation-info `
    --profile agentid-poc `
    --query IssuerIdentifier `
    --output text
```

If federation is already enabled, the enable command returns `FeatureEnabled`. This is
safe to ignore; the get command returns the existing issuer URL.

Alternatively, open **AWS IAM → Access management → Account settings → Outbound
identity federation**, select **Enable**, and copy the token issuer URL.

---

## Step 2: Deploy the CloudFormation stack

The stack is deployed via `deploy.ps1`, which uses change sets and is **safe to re-run** (no-op if nothing has changed).

> **Region note:** `eu-central-1` is supported. The AgentCore control-plane API (`bedrock-agentcore-control`) is available there.

```powershell

.\scripts\aws-deploy.ps1 `
    -EntraTenantId      "YOUR_TENANT_ID" `
    -AgentIdentityId    "YOUR_AGENT_IDENTITY_OBJECT_ID" `
    -AgentCoreAppClientId "YOUR_BLUEPRINT_CLIENT_ID" `
    -AgentCoreAllowedScope "agent.invoke" `
    -EchoApiClientId    "YOUR_ECHO_API_CLIENT_ID" `
    -McpServerUrl       "https://your-mcp-server.example.com" `
    -McpServerScope     "api://your-mcp-scope/.default" `
    -EchoApiScope       "api://YOUR_ECHO_API_CLIENT_ID/access_as_user" `
    -BedrockModelId     "amazon.nova-micro-v1:0"
```

> **Optional parameters with defaults:**
> - `-StackName` — default: `agentid-poc`
> - `-Region` — default: `eu-central-1`
> - `-EchoApiScope` — default: `api://echo-api-client-id/access_as_user` (override with your actual Echo API client ID)
> - `-BedrockModelId` — default: `eu.amazon.nova-micro-v1:0`
> - `-AgentCoreAllowedScope` — default: `agent.invoke`; set this to the exact
>   value in the inbound JWT's `scp` claim, for example `agent.invoke`.
> - `-EnableAuthDiagnostics` — logs non-secret AWS assertion header/claim metadata;
>   never logs the raw assertion. Use only while troubleshooting.

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

On WSL/Ubuntu, build the deployment ZIP directly without Docker:

```bash
./scripts/build-zip.sh
```

The script selects Python 3.12 ARM64 wheels, disables bytecode compilation, removes
all `__pycache__`, `.pyc`, and `.pyo` files, and writes
`build/agent-runtime.zip`. Deploy it from PowerShell with `-SkipZipBuild`.

**The runtime takes 2–3 minutes to reach READY status** after creation. Poll status:

```powershell
aws bedrock-agentcore-control get-agent-runtime `
    --agent-runtime-id YOUR_RUNTIME_ID --region eu-central-1 --profile agentid-poc
```

---

## Step 3: Configure the Blueprint federated identity credential

After the stack reaches `CREATE_COMPLETE` or `UPDATE_COMPLETE`:

```powershell
$outputs = aws cloudformation describe-stacks `
    --stack-name agentid-poc `
    --query "Stacks[0].Outputs" | ConvertFrom-Json

$outputs | Format-Table OutputKey, OutputValue
```

Retrieve the AWS values used by the Blueprint FIC:

```powershell
$issuer = aws iam get-outbound-web-identity-federation-info `
    --profile agentid-poc `
    --query IssuerIdentifier `
    --output text

$subject = aws cloudformation describe-stacks `
    --stack-name agentid-poc `
    --profile agentid-poc `
    --query "Stacks[0].Outputs[?OutputKey=='ExecutionRoleArn'].OutputValue | [0]" `
    --output text
```

In the Entra admin center, open the Blueprint's management page, select **Credentials**
under **Developer settings**, open **Federated credentials**, and select **Add
credential → Other issuer**. Enter:

| Field | Exact value |
|---|---|
| Name | `aws-agentcore-runtime` |
| Issuer | `$issuer` |
| Subject | `$subject` |
| Audience | `api://AzureADTokenExchange` |

Issuer, subject, and audience matching is case-sensitive. Use the `ExecutionRoleArn`
stack output unchanged. Do not use the STS assumed-role session ARN, runtime ARN, or
Agent Identity Object ID as the subject.

The role name includes the stack name. If you deploy under a different stack name or
replace the role, update the FIC subject.

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

const agentCoreScopes = ["api://YOUR_BLUEPRINT_CLIENT_ID/agent.invoke"];
const agentCoreEndpoint = "https://bedrock-agentcore.YOUR_REGION.amazonaws.com"; // AgentRuntimeEndpoint from Step 3
```

**How it works:** The SPA acquires an Entra access token scoped to `api://{blueprint-client-id}/agent.invoke` and sends it as `Authorization: Bearer {token}` in the request to AgentCore. AgentCore's JWT authorizer validates the token against the Entra OIDC discovery endpoint (`iss`), the allowed audience (`aud` = `AgentCoreAppClientId`), and the allowed scope (`scp` = `agent.invoke`). The Python agent obtains an AWS-signed assertion from regional STS, authenticates the Blueprint through its FIC, and performs the FMI/OBO exchange.

---

## Step 5: Full OBO flow test — with Entra token

This tests the complete Entra Agent ID OBO flow: SPA → AgentCore → Entra → Echo API.

### Option A — SPA in browser (recommended)

```powershell
# Serve the SPA locally using nginx
docker run --rm -p 3000:80 -v "${PWD}/spa:/usr/share/nginx/html:ro" nginx:alpine
```

1. Open `http://localhost:3000` in a browser.
2. Sign in with an Entra account that has consent for `api://{blueprint-client-id}/agent.invoke`.
3. Type a message and click **Send**.
4. The response body from the Echo API is displayed — this confirms the full OBO chain completed.

---

## Step 7: Deploy the Echo REST API (optional)

The Echo API validates the downstream OBO token issued by Entra. For quick local testing, run it with Docker:

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

## Troubleshooting

| Symptom | Diagnosis & Fix |
|---|---|
| Stack stuck in `ROLLBACK_COMPLETE` | `deploy.ps1` handles this automatically on re-run. To inspect what failed: `aws cloudformation describe-stack-events --stack-name agentid-poc --query "StackEvents[?ResourceStatus=='CREATE_FAILED']"` |
| JSON-RPC `Authorization denied` / HTTP 403 from AgentCore | The request was rejected before the container ran. Verify `iss`, `aud`, and `scp`. Set `AGENTCORE_ALLOWED_SCOPE` to the exact scope value in `scp` (for example `agent.invoke`), not the full scope URI. Then redeploy. |
| `No READY endpoints found` from `invoke.ps1` | The AgentCore Runtime may still be initialising. Wait 2–3 minutes after creation and retry. Check runtime status: `aws bedrock-agentcore-control list-agent-runtimes --region eu-central-1 --profile agentid-poc` |
| `Runtime 'agentid-poc' not found` from `invoke.ps1` | Run `create-runtime.ps1` first. |
| `OutboundWebIdentityFederationDisabled` | Enable the account setting in Step 1. It is deliberately not managed by the CloudFormation stack. |
| `AADSTS70021` / no matching federated identity record | Verify that the FIC issuer equals the account issuer, its subject exactly equals the stack's `ExecutionRoleArn`, and its audience is `api://AzureADTokenExchange`. Allow several minutes after creating or updating the FIC. |
| Need to inspect the AWS assertion | Redeploy with `-EnableAuthDiagnostics`, invoke the agent, and find `AWS assertion metadata` in the runtime's CloudWatch logs. Compare `iss`, `sub`, and `aud` with the FIC. The raw JWT is intentionally never logged. Redeploy without the switch afterward. |
| OBO exchange fails with `invalid_grant` | Ensure the Agent Identity is correctly linked to the Blueprint in Entra admin center (Agents → Agent identities). The `fmi_path` (Agent Identity Object ID) must be the Object ID of the Agent Identity child object, not a client ID. |

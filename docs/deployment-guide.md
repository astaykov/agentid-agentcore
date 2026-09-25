# Deployment guide

Deploy the three-agent AgentCore proof of concept after completing
[Entra setup](entra-setup-guide.md).

## Prerequisites

- PowerShell 7, AWS CLI v2, Docker Desktop, and an authenticated AWS profile.
- All Entra IDs and grants from the Entra guide.
- AWS permission to enable and query outbound identity federation.

Copy `.env.example` to `.env`. Set all required Entra, Echo, MCP, and A2A values:

```text
ENTRA_TENANT_ID
AGENT_IDENTITY_ID
AGENTCORE_APP_CLIENT_ID
ECHO_API_CLIENT_ID
MCP_SERVER_URL
MCP_SERVER_SCOPE
BLUEPRINT2_CLIENT_ID
AGENT2_IDENTITY_ID
BLUEPRINT3_CLIENT_ID
AGENT3_IDENTITY_ID
```

`AGENTCORE_APP_CLIENT_ID` is Blueprint 1's client ID. The SPA must request its
`agent.invoke` scope.

## 1. Enable AWS federation

Run once per AWS account:

```powershell
aws iam enable-outbound-web-identity-federation --profile agentid-poc
aws iam get-outbound-web-identity-federation-info `
  --profile agentid-poc --query IssuerIdentifier --output text
```

`FeatureEnabled` means it was already enabled.

## 2. Build and deploy

Deploy Agent 1, Agent 2, Agent 3, Echo, and the disabled schedule:

```powershell
.\scripts\aws-deploy.ps1 -EnableA2A -AutonomousScheduleState DISABLED
```

The script builds one ARM64 ZIP, uploads a versioned artifact, creates or updates
the CloudFormation stack, and prints its outputs. It creates a new timestamped
Agent 1 runtime each time; update the SPA endpoint after every deployment.

If you built the ZIP separately, preserve that artifact and skip the build:

```powershell
.\scripts\aws-deploy.ps1 -SkipZipBuild -EnableA2A -AutonomousScheduleState DISABLED
```

For a normal redeploy, omit `-EnableA2A`; the script retains the existing A2A
configuration. Do not pass `-EnableA2A:$false`; disable A2A only through an
intentional CloudFormation change.

## 3. Configure the three Blueprint FICs

Use the deployment outputs:

| Blueprint | Stack output used as FIC subject |
|---|---|
| Blueprint 1 | `ExecutionRoleArn` |
| Blueprint 2 | `SpecialistRoleArn` |
| Blueprint 3 | `AutonomousRoleArn` |

For each Blueprint, create or update a FIC with:

| Field | Value |
|---|---|
| Issuer | AWS `IssuerIdentifier` from step 1 |
| Subject | Exact role ARN from the table |
| Audience | `api://AzureADTokenExchange` |

Matching is case-sensitive. The FIC subject is an IAM role ARN, not a runtime ARN,
assumed-role session ARN, or identity ID.

## 4. Configure and run the SPA

Create local `spa/msal-config.js` from its sample. Set the SPA client ID, tenant ID,
Blueprint 1 client ID, and the `agentCoreEndpoint` printed by deployment:

```javascript
const agentCoreScopes = ["api://{blueprint-1-client-id}/agent.invoke"];
```

Start the SPA:

```powershell
.\scripts\dev.ps1
```

Open `http://localhost:3000`, sign in as an assigned user, and invoke Agent 1.
Directory questions exercise Agent 1 -> Agent 2 -> MCP. Echo requests exercise
Agent 1 -> Echo.

## 5. Verify Agent 3, then schedule it

Manually invoke Agent 3 first:

```powershell
aws lambda invoke --profile agentid-poc --region eu-central-1 `
  --function-name "<AutonomousFunctionName>" `
  --cli-binary-format raw-in-base64-out `
  --payload '{"prompt":"Explain one benefit of agent-to-agent collaboration."}' `
  response.json
```

Check both `FunctionError` and `response.json`. A successful Lambda invocation
request does not prove the A2A call succeeded.

Enable the five-minute schedule only after manual verification:

```powershell
.\scripts\aws-deploy.ps1 -SkipZipBuild -AutonomousScheduleState ENABLED
```

Disable it with the same command and `DISABLED`. Monitor the
`AutonomousFailureQueueUrl` output and CloudWatch logs; scheduled calls incur AWS
and Bedrock costs.

## Troubleshooting

| Symptom | Check |
|---|---|
| AgentCore returns 403 without a container log | Blueprint 1 audience and `agent.invoke` scope |
| Agent 2 rejects delegated access | Blueprint 2 `user_impersonation`, `Agent2.Tools.User`, and Agent Identity 1 grant |
| Agent 3 cannot call Agent 2 | Blueprint 2 app-role assignment to Agent Identity 3 |
| OBO fails | Correct Agent Identity object ID, resource-specific grant, and FIC propagation |
| `AADSTS70021` | FIC issuer, exact role-ARN subject, and `api://AzureADTokenExchange` audience |
| FIC diagnosis needed | Redeploy with `-EnableAuthDiagnostics`; remove it after diagnosis |

## Teardown

```powershell
.\scripts\aws-destroy.ps1 -StackName agentid-poc
```

CloudFormation does not remove Entra Blueprints, Agent Identities, FICs, grants, or
role assignments; remove those separately when retiring the environment.

# Architecture Simplification Assessment: Replace Sidecar with MSAL Python FIC/FMI

**Author:** Keyser (Lead Architect)  
**Date:** 2026-06-08  
**Requested by:** Anton Staykov  
**Status:** Assessment — not yet approved for implementation

---

## Executive Summary

The Entra SDK sidecar exists exclusively to perform a two-stage OBO token exchange that MSAL
Python now supports natively via the Federated Identity Credential / Federated Managed Identity
(FIC/FMI) APIs. Removing the sidecar eliminates **32 CloudFormation resources**, two Docker
artefacts, one Python source file, and the only reason the project needed a VPC, NAT gateway, or
any network infrastructure at all. The simplified stack is three resources: IAM role +
AgentCore Runtime + AgentCore RuntimeEndpoint.

---

## 1. What Gets Removed

### 1.1 CloudFormation Resources (all sidecar-only)

The following 32 logical resources in `infra/cloudformation/stack.yaml` exist exclusively to host
the Fargate sidecar. None of them serve the AgentCore Runtime.

| Category | Logical Resource ID |
|---|---|
| **VPC & networking** | `VPC`, `PublicSubnet1`, `PublicSubnet2`, `PrivateSubnet1`, `PrivateSubnet2` |
| **Internet / NAT egress** | `InternetGateway`, `VPCGatewayAttachment`, `EIP`, `NatGateway` |
| **Public route table** | `PublicRouteTable`, `PublicRoute`, `PublicSubnet1RouteTableAssociation`, `PublicSubnet2RouteTableAssociation` |
| **Private route table** | `PrivateRouteTable`, `PrivateRoute`, `PrivateSubnet1RouteTableAssociation`, `PrivateSubnet2RouteTableAssociation` |
| **Security groups** | `SidecarALBSG`, `FargateSidecarSG`, `SidecarALBSGIngressHTTP`, `SidecarALBSGEgressFargate`, `FargateSidecarSGIngressALB`, `FargateSidecarSGEgressHTTPS` |
| **ECS** | `ECSCluster`, `SidecarTaskDef`, `SidecarService` |
| **IAM (sidecar only)** | `SidecarTaskRole`, `SidecarExecutionRole` |
| **ALB** | `SidecarALB`, `SidecarTargetGroup`, `SidecarListener` |
| **Logging** | `SidecarLogGroup` |

**Total: 32 resources deleted.** The remaining stack is 3 resources: `AgentCoreExecutionRole`,
`AgentRuntime`, `AgentRuntimeEndpoint`.

### 1.2 CFN Stack Outputs Removed

| Output Key | Reason |
|---|---|
| `SidecarServiceArn` | ECS service gone |
| `SidecarUrl` | ALB gone |
| `VpcId` | VPC gone |
| `PrivateSubnet1Id` | Subnets gone |
| `PrivateSubnet2Id` | Subnets gone |

All five remaining outputs (`RuntimeId`, `RuntimeArn`, `EndpointId`, `EndpointArn`,
`ExecutionRoleArn`, `ArtifactBucketName`, `ArtifactKey`) are kept as-is.

### 1.3 CFN Parameters Removed or Repurposed

The following parameters were used exclusively for sidecar container environment injection; they
are removed from the stack:

- None need to be removed outright — all env-var parameters (`EchoApiUrl`, `EchoApiScope`,
  `McpServerUrl`, `McpServerScope`) move from the sidecar `ContainerDefinitions.Environment` to
  the `AgentRuntime.EnvironmentVariables` map. They remain useful as CFN parameters.

The `SIDECAR_URL` entry in `AgentRuntime.EnvironmentVariables` is deleted (it referenced
`!Sub 'http://${SidecarALB.DNSName}'`).

### 1.4 Files to Delete

| File / Directory | Reason |
|---|---|
| `agent/src/sidecar_client.py` | Entire HTTP client for the sidecar; replaced by MSAL Python calls inline |
| `infra/sidecar/docker-compose.yml` | Local-run helper for the sidecar container |
| `infra/sidecar/.env.example` | Sidecar environment template |
| `infra/sidecar/` (directory) | Empty after above two files removed |

### 1.5 Docker Infrastructure Removed

| Item | Notes |
|---|---|
| `mcr.microsoft.com/entra-sdk/auth-sidecar:latest` | The MCR image is no longer pulled at deploy time or at runtime |
| ECS task definition (`SidecarTaskDef`) | Defines the container; gone with ECS |
| `agent/Dockerfile` | Was already not used by AgentCore (ZIP-based runtime); confirm it can be deleted or keep for local dev only |

---

## 2. What Stays / Gets Simpler

### 2.1 AgentCore Runtime

Unchanged in type and deployment pattern. The `AgentRuntime` CFN resource uses the same S3 ZIP
artifact, same `PYTHON_3_12` runtime, same `PUBLIC` network mode. The runtime already runs
without a VPC — that was always the correct posture.

### 2.2 S3 Artifact Bucket

Unchanged. The `ArtifactBucket` parameter and the S3 `GetObject`/`ListBucket` IAM policy in
`AgentCoreExecutionRole` are kept as-is.

### 2.3 IAM Role — `AgentCoreExecutionRole`

**Already has `secretsmanager:GetSecretValue` on `BlueprintSecretArn`** (line 477 of
`stack.yaml`, `Sid: SecretsAccess`). No IAM changes required. The agent code will call
`boto3.client('secretsmanager').get_secret_value(SecretId=os.environ['BLUEPRINT_SECRET_ARN'])`
to load the Blueprint client secret at runtime before MSAL calls.

### 2.4 Blueprint Credentials (Secrets Manager)

The Blueprint `client_id` and `client_secret` are still required — MSAL Python needs them to
perform the same two-stage exchange. The secret in Secrets Manager is unchanged. The `BlueprintSecretArn`
CFN parameter stays. The `BlueprintClientId` plain-text parameter also stays as an env var on
`AgentRuntime` (it's not sensitive; it's already exposed in container env today).

### 2.5 Entra Objects

Zero changes. The four Entra objects (SPA, Blueprint, Agent Identity, Echo API app registration)
and their consent grants are defined by the Entra tenant, not by the AWS stack. They are
unaffected.

---

## 3. What the Simplified `stack.yaml` Looks Like

```
Parameters (kept, unchanged):
  RuntimeName, EndpointName, ArtifactBucket, ArtifactKey, PythonRuntime,
  BedrockModelId, EntraTenantId, BlueprintClientId, AgentIdentityId,
  BlueprintSecretArn, EchoApiUrl, EchoApiScope, McpServerUrl, McpServerScope

Resources (3 total):
  AgentCoreExecutionRole   # IAM::Role — bedrock-agentcore principal
                           #   InvokeModel, SecretsAccess, Logs, S3 (unchanged)
  AgentRuntime             # BedrockAgentCore::Runtime — S3 ZIP, PUBLIC network
                           #   EnvironmentVariables: BEDROCK_MODEL_ID, ENTRA_TENANT_ID,
                           #     BLUEPRINT_CLIENT_ID, AGENT_IDENTITY_ID, ECHO_API_URL,
                           #     ECHO_API_SCOPE, MCP_SERVER_URL, MCP_SERVER_SCOPE,
                           #     BLUEPRINT_SECRET_ARN
                           #   NOTE: SIDECAR_URL removed
  AgentRuntimeEndpoint     # BedrockAgentCore::RuntimeEndpoint (unchanged)

Outputs (7 kept):
  RuntimeId, RuntimeArn, EndpointId, EndpointArn,
  ExecutionRoleArn, ArtifactBucketName, ArtifactKey
```

The stack description line changes from the current verbose multi-component description to:

> "AgentCore + Entra Agent ID PoC. Deploys AgentCore Runtime (AWS::BedrockAgentCore::Runtime),
> RuntimeEndpoint, and supporting IAM role. MSAL Python handles Entra Agent ID OBO token
> exchange in-process."

---

## 4. Simplified Deployment Flow

### Before (current)

```
aws-deploy.ps1
  └─ CloudFormation stack (35+ resources)
       ├─ VPC + subnets + IGW + NAT (~17 resources, ~3 min)
       ├─ Security groups + rules (4 resources)
       ├─ ECS cluster + task def + service (3 resources)
       ├─ ALB + target group + listener (3 resources)
       ├─ IAM roles ×3 (SidecarTask, SidecarExec, AgentCoreExec)
       ├─ CloudWatch log group
       └─ AgentCore Runtime + Endpoint
  └─ Fargate service cold-start: ~60-90 seconds after stack CREATE_COMPLETE
  └─ ALB health check stabilisation: additional 30-60 seconds
Total: ~8-12 minutes, ~$0.045/hr NAT gateway + ~$0.016/hr ALB + Fargate CPU/RAM
```

### After (simplified)

```
aws-deploy.ps1
  └─ CloudFormation stack (3 resources)
       ├─ AgentCoreExecutionRole (IAM)
       └─ AgentRuntime + AgentRuntimeEndpoint (BedrockAgentCore)
Total: ~2-3 minutes, no persistent networking costs
```

No container image to pull. No ECS service health stabilisation wait. No NAT gateway billing
the moment the stack is live.

---

## 5. Agent Code Changes Required

### 5.1 `agent/src/agent.py`

- Remove: `from sidecar_client import SidecarClient` and `sidecar = SidecarClient()`
- Replace `sidecar.call_service(...)` in `call_echo_api` with a direct MSAL Python FIC/FMI
  token acquisition followed by an outbound HTTP call using the acquired token
- Read Blueprint secret at startup via `boto3` (or lazily per-invocation):
  ```python
  import boto3, msal
  secret_arn = os.environ["BLUEPRINT_SECRET_ARN"]
  secret_value = boto3.client("secretsmanager").get_secret_value(SecretId=secret_arn)["SecretString"]
  ```
- Acquire token using MSAL FIC/FMI flow:
  ```python
  # Stage 1: acquire T1 (impersonation token via fmi_path)
  # Stage 2: OBO exchange using T1 as client_assertion + Tc as assertion
  ```

### 5.2 `agent/requirements.txt`

| Change | Reason |
|---|---|
| Add `msal>=1.30.0` | FIC/FMI support added in 1.30.0 |
| Add `boto3>=1.34.0` | Secrets Manager call |
| Remove `requests>=2.31.0` | No longer needed for sidecar HTTP; keep if used for downstream API calls |

---

## 6. Risk Assessment

### 6.1 MSAL Python FIC/FMI — GA Status

| Question | Finding |
|---|---|
| Is FIC (`client_credentials` with `client_assertion`) supported in MSAL Python? | ✅ Yes — `msal.ConfidentialClientApplication` with `client_assertion` parameter, since MSAL Python ≥1.20. |
| Is the `fmi_path` parameter (used in Stage 1 of the Agent ID OBO) exposed in MSAL Python? | ⚠️ **Needs verification.** `fmi_path` is a non-standard extension parameter. MSAL Python supports `extra_body_params` or `extra_claims` in some token acquisition methods. If `fmi_path` is not natively mapped, it can be passed via raw token request parameters. |
| Is the two-stage exchange (T1 acquisition + OBO with T1 as `client_assertion`) documented for MSAL Python? | ⚠️ The Entra SDK sidecar was the reference implementation. The raw MSAL calls are documented in `entra-setup-guide.md` Steps 4–5 and can be replicated with `acquire_token_on_behalf_of()` + `client_assertion` override. |
| Agent ID / Agent Identity Object type — production support? | ⚠️ Agent Identity Objects are a new Entra object type. MSAL Python is not specifically aware of them; they work via standard token endpoint parameters (`fmi_path`, `client_assertion`). The risk is that the exact parameter names change before GA. |

### 6.2 Mitigation Options

| Risk | Mitigation |
|---|---|
| `fmi_path` not surfaced cleanly in MSAL Python | Use `msal.ConfidentialClientApplication.acquire_token_for_client()` with the dedicated `fmi_path="..."` kwarg (added in MSAL Python 1.36.0, PR #876) — it is sent as the `fmi_path` parameter in the token-request body and the token is cached per-path. NOTE: there is no `extra_body_params` kwarg in MSAL Python; unknown kwargs leak to `requests` and raise `TypeError`. |
| MSAL FMI not yet GA | Pin `msal>=1.30.0,<2.0`; test in a separate feature branch before merging to main |
| Behaviour divergence from sidecar | Write a thin integration test that exchanges a real Tc and validates TR audience/subject before deleting the sidecar code |
| Rollback complexity | Keep `sidecar_client.py` and CFN sidecar resources on a `feat/msal-native` branch; delete only after smoke-test passes |

### 6.3 Reasons to Keep the Sidecar

There is **no strong architectural reason** to keep the sidecar if MSAL Python `extra_body_params`
can carry `fmi_path`. The sidecar was adopted because:

1. The .NET Entra SDK had first-class FMI support before MSAL Python caught up.
2. It provided a clean separation: Python agent never had to know token mechanics.

Neither reason outweighs the operational cost and complexity of running a long-lived Fargate
service just to perform HTTP token exchanges. The only reason to keep it would be if MSAL Python
proves unable to replicate the exact `fmi_path` + `client_assertion` sequence — which should be
determinable with a single integration test before committing to the removal.

### 6.4 Overall Recommendation

**Proceed with removal.** The risk is low and the simplification is substantial (32 CFN resources,
~$60/month in idle NAT/ALB costs, deployment complexity). Implement on a branch, validate with an
integration test that exercises the real Entra token exchange, then merge and delete.

---

## 7. Summary Table

| Dimension | Before | After |
|---|---|---|
| CFN resources | ~35 | 3 |
| VPC / networking | Yes (VPC, 4 subnets, IGW, NAT, route tables) | None |
| Containers | 1 Fargate task (`auth-sidecar`) | None |
| IAM roles | 3 (SidecarTask, SidecarExec, AgentCoreExec) | 1 (AgentCoreExec) |
| Secrets Manager access | Two roles read the same secret | One role (AgentCoreExec, already configured) |
| Python files | `agent.py`, `sidecar_client.py` | `agent.py` only |
| Python deps | `bedrock-agentcore`, `strands-agents`, `requests` | `bedrock-agentcore`, `strands-agents`, `msal`, `boto3` |
| Deploy time | ~10 min | ~2-3 min |
| Persistent infra cost | ~$75-90/month (NAT + ALB + Fargate) | ~$0 (no idle resources) |
| Entra objects | Unchanged (4 objects) | Unchanged (4 objects) |
| OBO flow correctness | Validated (sidecar) | To be validated (MSAL Python integration test) |

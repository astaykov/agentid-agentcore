# MSAL Python FIC/FMI — Findings & Migration Path

**Author**: Fenster (Entra Identity Engineer)  
**Date**: 2026-06-08  
**Requested by**: Anton Staykov  
**Reference**: [MSAL Developer Guide — Agent Identity Flow](https://github.com/AzureAD/microsoft-authentication-library-for-dotnet/wiki/How-to-Use-FIC-and-FMI-in-Agentic-Scenarios)

---

## 1. What FIC/FMI Is and How It Works for Entra Agent ID

### Terminology

| Term | Full Name | What It Is |
|------|-----------|------------|
| FMI | Federated Managed Identity | A trust relationship between a Blueprint app registration and an Agent app registration |
| FIC | Federated Identity Credential | The credential exchange mechanism used in each leg of the flow |
| Blueprint app | — | The real credential holder (cert). Performs Leg 1 only. |
| Agent app | — | The per-agent app registration. Obtains tokens on behalf of users. |

### The Three-Leg Flow

```
Leg 1 (FMI):    Blueprint cert  → Entra ID  → T1  (FMI token, scoped to Agent)
Leg 2 (FIC):    T1 as assertion → Entra ID  → T2  (Agent instance token)
Leg 3 (UserFIC):T2 + user UPN  → Entra ID  → TR  (User-scoped token for downstream resource)
```

- **T1** is cached in the Blueprint CCA. `fmi_path = agent_app_id` differentiates per-agent.
- **T2** is cached in the Agent CCA (app-level cache).
- **TR** is cached in the Agent CCA (user-level cache, keyed by `homeAccountId`).

### Minimum SDK Versions

| SDK | Min Version |
|-----|-------------|
| MSAL Python | **1.37.0** |
| MSAL .NET | 4.83.3 (OID-based UserFIC: 4.84.2+) |
| MSAL Java | 1.25.0 |
| MSAL Go | 1.8.0 |

Current `msal` is not in `requirements.txt` at all — it must be added.

---

## 2. Python MSAL Call Pattern — Concrete Code

This is the direct Python equivalent of what the sidecar did internally.

### 2a. Module-Level Setup (replace `SidecarClient()`)

```python
import os
import msal
import boto3
import base64
import json

# ---- Load Blueprint certificate from Secrets Manager at startup ----
def _load_cert_credential() -> dict:
    """
    Load the Blueprint certificate from Secrets Manager.
    The secret must contain a JSON object with keys:
      - "private_key_pem": PEM-encoded private key (string)
      - "public_certificate": PEM-encoded certificate chain (string or True for SNI-only)
      - "thumbprint": hex SHA-1 thumbprint of the certificate
    """
    secret_arn = os.environ["BLUEPRINT_SECRET_ARN"]
    client = boto3.client("secretsmanager")
    raw = client.get_secret_value(SecretId=secret_arn)
    secret = json.loads(raw["SecretString"])
    return {
        "private_key_pem": secret["private_key_pem"],
        "public_certificate": secret["public_certificate"],   # PEM chain; enables SNI
        "thumbprint": secret["thumbprint"],
    }


TENANT_ID         = os.environ["ENTRA_TENANT_ID"]
BLUEPRINT_CLIENT_ID = os.environ["BLUEPRINT_CLIENT_ID"]
AGENT_APP_ID      = os.environ["AGENT_IDENTITY_ID"]   # Agent app registration client ID
ECHO_API_SCOPE    = os.environ.get("ECHO_API_SCOPE", "api://echo-api/access_as_user")
MCP_SERVER_SCOPE  = os.environ.get("MCP_SERVER_SCOPE", "")
ECHO_API_URL      = os.environ["ECHO_API_URL"]
AUTHORITY         = f"https://login.microsoftonline.com/{TENANT_ID}"
FIC_SCOPE         = ["api://AzureADTokenExchange/.default"]

# ---- Blueprint CCA (long-lived, owns the real certificate) ----
_blueprint_app = msal.ConfidentialClientApplication(
    BLUEPRINT_CLIENT_ID,
    client_credential=_load_cert_credential(),
    authority=AUTHORITY,
)

# ---- Assertion callback: performs Leg 1 (FMI token for this agent) ----
def _assertion_cb(ctx=None):
    result = _blueprint_app.acquire_token_for_client(
        FIC_SCOPE,
        fmi_path=AGENT_APP_ID,          # differentiates per-agent in Blueprint cache
    )
    if "access_token" not in result:
        raise RuntimeError(f"FMI Leg 1 failed: {result.get('error')}: {result.get('error_description')}")
    return result["access_token"]

# ---- Agent CCA (long-lived, one per agent app ID) ----
_agent_app = msal.ConfidentialClientApplication(
    AGENT_APP_ID,
    client_credential={"client_assertion": _assertion_cb},  # callback, not static string
    authority=AUTHORITY,
)
```

### 2b. Acquire a User-Scoped Token for the Echo API (Legs 2 + 3)

```python
def get_echo_api_token(user_upn: str) -> str:
    """
    Perform the full 3-leg agent identity flow and return a Bearer token
    for the Echo API scoped to the given user.

    Leg 1 (FMI)    — handled transparently by _assertion_cb
    Leg 2 (FIC)    — acquire_token_for_client with FIC scope → T2
    Leg 3 (UserFIC)— acquire_token_by_user_federated_identity_credential → TR
    """
    # Leg 2: Instance token (T2) — cached; only hits network on first call or expiry
    t2_result = _agent_app.acquire_token_for_client(FIC_SCOPE)
    if "access_token" not in t2_result:
        raise RuntimeError(f"Leg 2 failed: {t2_result.get('error')}")

    # Leg 3: User-scoped resource token (TR)
    tr_result = _agent_app.acquire_token_by_user_federated_identity_credential(
        scopes=[ECHO_API_SCOPE],
        assertion=t2_result["access_token"],
        username=user_upn,              # UPN from the inbound Entra token claim
    )
    if "access_token" not in tr_result:
        raise RuntimeError(f"Leg 3 failed: {tr_result.get('error')}")
    return tr_result["access_token"]


def get_echo_api_token_silent(user_upn: str) -> str | None:
    """
    Try the cache first; fall back to full flow on cache miss.
    Call this for all subsequent requests from the same user.
    """
    accounts = _agent_app.get_accounts(username=user_upn)
    if accounts:
        result = _agent_app.acquire_token_silent(
            scopes=[ECHO_API_SCOPE],
            account=accounts[0],
        )
        if result and "access_token" in result:
            return result["access_token"]
    # Cache miss — full 3-leg flow
    return get_echo_api_token(user_upn)
```

### 2c. Get Authorization Header for the MCP Server

```python
def get_mcp_authorization_header(user_upn: str) -> str:
    """Return a ready-to-use 'Authorization: Bearer {TR}' header value for MCP Server."""
    t2_result = _agent_app.acquire_token_for_client(FIC_SCOPE)
    if "access_token" not in t2_result:
        raise RuntimeError(f"Leg 2 failed: {t2_result.get('error')}")

    tr_result = _agent_app.acquire_token_by_user_federated_identity_credential(
        scopes=[MCP_SERVER_SCOPE],
        assertion=t2_result["access_token"],
        username=user_upn,
    )
    if "access_token" not in tr_result:
        raise RuntimeError(f"Leg 3 (MCP) failed: {tr_result.get('error')}")
    return f"Bearer {tr_result['access_token']}"
```

### 2d. How to Extract `user_upn` from the Inbound Token

The inbound token `Tc` is a JWT. The `upn` or `preferred_username` claim contains the user's UPN. Decode without verification (Entra ID already validated it upstream):

```python
import base64, json

def extract_upn_from_bearer(bearer_header: str) -> str:
    """
    Extract UPN from 'Bearer {jwt}' without signature verification.
    The token is already validated by the upstream layer.
    """
    token = bearer_header.removeprefix("Bearer ").strip()
    payload_b64 = token.split(".")[1]
    # Pad base64 to a multiple of 4
    payload_b64 += "=" * (-len(payload_b64) % 4)
    claims = json.loads(base64.urlsafe_b64decode(payload_b64))
    return claims.get("upn") or claims.get("preferred_username") or claims.get("email", "")
```

---

## 3. What Changes in `agent/src/agent.py`

### What to Remove

| Item | Reason |
|------|--------|
| `from sidecar_client import SidecarClient` | Sidecar is gone |
| `sidecar = SidecarClient()` | Sidecar is gone |
| `sidecar.call_service(...)` call | Replaced by direct MSAL + `requests` |
| `AGENT_IDENTITY_ID` env var usage (partially) | Renamed; now used as `AGENT_APP_ID` |
| `sidecar_client.py` file | Delete entirely |

### What to Add

| Item | Reason |
|------|--------|
| `import msal` | New dependency |
| `import boto3` | Load cert from Secrets Manager |
| `import requests` | Direct HTTP call to Echo API |
| `_blueprint_app`, `_agent_app` module-level | Long-lived CCA instances |
| `get_echo_api_token(user_upn)` | 3-leg flow |
| `extract_upn_from_bearer(header)` | Decode UPN from inbound token |

### Revised `call_echo_api` Tool

```python
@tool
def call_echo_api(message: str) -> str:
    """Call the Entra-protected Echo REST API on behalf of the authenticated user."""
    if not _current_inbound_token:
        return "Error: no authentication token available"

    try:
        user_upn = extract_upn_from_bearer(_current_inbound_token)
        token = get_echo_api_token_silent(user_upn)

        resp = requests.post(
            f"{ECHO_API_URL}/echo",
            json={"message": message},
            headers={"Authorization": f"Bearer {token}"},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        return str(data.get("echo", data))
    except Exception as exc:
        logger.error("call_echo_api failed: %s", type(exc).__name__)
        return f"Error calling Echo API: {type(exc).__name__}"
```

### Environment Variables

| Old | New | Change |
|-----|-----|--------|
| `SIDECAR_URL` | — | Remove; sidecar gone |
| `AGENT_IDENTITY_ID` | `AGENT_APP_ID` or keep name | This is the **Agent app registration client ID** (not object ID — see gap below) |
| — | `ECHO_API_SCOPE` | New; was embedded in sidecar config |
| — | `MCP_SERVER_SCOPE` | New; was embedded in sidecar config |

---

## 4. What to Remove from `requirements.txt`

```diff
 bedrock-agentcore>=0.1.0
 strands-agents>=0.1.0
 requests>=2.31.0
+msal>=1.37.0
+boto3>=1.34.0
```

`requests` stays (now used directly by agent.py). `sidecar_client.py` imported it before.

---

## 5. What to Remove from `infra/cloudformation/stack.yaml`

The sidecar required an entire VPC + ECS + ALB stack. All of it can be dropped:

### Resources to Remove (25 CloudFormation resources)

**Networking (entire VPC)**
- `VPC`, `PublicSubnet1`, `PublicSubnet2`, `PrivateSubnet1`, `PrivateSubnet2`
- `InternetGateway`, `VPCGatewayAttachment`
- `EIP`, `NatGateway`
- `PublicRouteTable`, `PublicRoute`, `PublicSubnet1RouteTableAssociation`, `PublicSubnet2RouteTableAssociation`
- `PrivateRouteTable`, `PrivateRoute`, `PrivateSubnet1RouteTableAssociation`, `PrivateSubnet2RouteTableAssociation`

**Security Groups**
- `SidecarALBSG`, `FargateSidecarSG`
- `SidecarALBSGIngressHTTP`, `SidecarALBSGEgressFargate`
- `FargateSidecarSGIngressALB`, `FargateSidecarSGEgressHTTPS`

**ECS / Fargate**
- `ECSCluster`, `SidecarTaskRole`, `SidecarExecutionRole`
- `SidecarLogGroup`, `SidecarTaskDef`, `SidecarService`

**ALB**
- `SidecarALB`, `SidecarTargetGroup`, `SidecarListener`

### Changes to Existing Resources

**`AgentRuntime` EnvironmentVariables** — remove `SIDECAR_URL`, add `ECHO_API_SCOPE` and `MCP_SERVER_SCOPE`:
```yaml
EnvironmentVariables:
  BEDROCK_MODEL_ID: !Ref BedrockModelId
  ENTRA_TENANT_ID: !Ref EntraTenantId
  BLUEPRINT_CLIENT_ID: !Ref BlueprintClientId
  AGENT_IDENTITY_ID: !Ref AgentIdentityId   # now = Agent app client ID, not object ID
  ECHO_API_URL: !Ref EchoApiUrl
  ECHO_API_SCOPE: !Ref EchoApiScope
  MCP_SERVER_SCOPE: !Ref McpServerScope
  BLUEPRINT_SECRET_ARN: !Ref BlueprintSecretArn
  # SIDECAR_URL removed
```

**`AgentRuntime` NetworkConfiguration** — stays `PUBLIC` (AgentCore connects to Entra ID `login.microsoftonline.com` and to Echo API over the public internet via AgentCore's built-in egress; no private VPC needed).

### Outputs to Remove
- `SidecarServiceArn`, `SidecarUrl`, `VpcId`, `PrivateSubnet1Id`, `PrivateSubnet2Id`

### Parameters to Remove
- None to remove (all existing params are still used). `McpServerUrl` / `McpServerScope` / `EchoApiScope` were already there; `BlueprintSecretArn` stays for the cert secret.

---

## 6. The `BlueprintSecretArn` — Critical Credential Gap

> ⚠️ **This is the most important gap.**

The current stack stores a **client secret** in Secrets Manager (`AzureAd__ClientSecret`). The MSAL FMI/FIC flow **requires a certificate** (PFX or PEM) — a client secret cannot be used for Leg 1 because Entra ID requires Subject Name + Issuer (SNI/x5c) authentication for FMI token requests.

### What must change

| Current | Required |
|---------|----------|
| Secrets Manager secret: `{ "AzureAd__ClientSecret": "..." }` | Secrets Manager secret: `{ "private_key_pem": "...", "public_certificate": "...", "thumbprint": "..." }` |
| Blueprint app registration: client secret credential | Blueprint app registration: certificate credential (self-signed or CA) |

### Migration steps

1. Generate a certificate for the Blueprint app (or reuse an existing one).
2. Register the certificate in the Blueprint app registration in Entra ID (under **Certificates & secrets → Certificates**).
3. Remove the client secret from the Blueprint app registration (or leave it — no harm — but it won't be used).
4. Store the cert material in Secrets Manager as JSON with keys `private_key_pem`, `public_certificate` (full PEM chain), `thumbprint`.
5. Update the CloudFormation stack's `BlueprintSecretArn` parameter to point to the new secret.
6. The `SidecarTaskRole` and `AgentCoreExecutionRole` already have `secretsmanager:GetSecretValue` on that ARN — no IAM change needed.

---

## 7. What the Sidecar Did (and Now Python Does)

| Sidecar endpoint | What it did | Python replacement |
|-----------------|-------------|-------------------|
| `GET /AuthorizationHeader/{service}?AgentIdentity={oid}` | Ran Legs 1+2+3, returned `Bearer {TR}` header | `get_mcp_authorization_header(user_upn)` |
| `{METHOD} /Request/{service}?AgentIdentity={oid}` | Ran Legs 1+2+3, then proxied the HTTP call | `get_echo_api_token_silent(user_upn)` + `requests.post(...)` |
| Health check `/health` | Sidecar liveness | Not needed |

The sidecar also maintained its own in-memory token cache. MSAL Python's `ConfidentialClientApplication` has a built-in in-memory cache that serves the same purpose. For multi-process deployments, a distributed cache (e.g. Redis via `msal-extensions`) can be added later, but for a single AgentCore container instance the default in-memory cache is correct.

---

## 8. Can MSAL Python Fully Replace the Sidecar?

**Yes, for this use case.** The only thing the sidecar provided was:
1. The FMI token exchange (Legs 1–3) — now done by MSAL Python directly.
2. HTTP proxying to downstream services — now done by `requests` in agent.py.

There is no protocol gap: `acquire_token_by_user_federated_identity_credential` is the direct Python equivalent of the sidecar's internal call.

---

## 9. Gaps and Unknowns

| # | Gap | Severity | Notes |
|---|-----|----------|-------|
| 1 | **Certificate required for Blueprint** | 🔴 Blocker | Client secret cannot be used. Must migrate to cert. See §6. |
| 2 | **`AGENT_IDENTITY_ID` semantics** | 🟡 Needs verification | In the sidecar, `AgentIdentity` was the Agent Identity **Object ID** (OID). In MSAL's `fmi_path`, it should be the Agent app registration **client ID** (GUID). Verify which GUID is registered in the Blueprint's FMI configuration in Entra ID. |
| 3 | **UPN extraction from inbound token** | 🟡 Needs testing | The `upn` claim is present in v1.0 Entra tokens; `preferred_username` is in v2.0. Verify which token version the SPA acquires and which claim is populated. |
| 4 | **Token cache persistence** | 🟢 Low risk | MSAL Python uses in-memory cache per process. AgentCore containers are single-process per invocation. Cache warms on first call; no cross-invocation persistence. Acceptable for PoC. For production, consider `msal-extensions` with a Redis-backed cache. |
| 5 | **`acquire_token_by_user_federated_identity_credential` availability** | 🟡 Confirm version | This method was added in MSAL Python 1.37.0. Confirm the correct package version is pinned and available in the AgentCore Python 3.12 runtime. |
| 6 | **MCP Server call pattern** | 🟢 Not yet implemented | The current `agent.py` has no MCP tool. When adding it, use `get_mcp_authorization_header(user_upn)` as shown in §2c. |

---

## 10. Summary of File Changes

| File | Action | Detail |
|------|--------|--------|
| `agent/requirements.txt` | Modify | Add `msal>=1.37.0` and `boto3>=1.34.0` |
| `agent/src/agent.py` | Modify | Remove sidecar, add MSAL CCAs, add direct HTTP call |
| `agent/src/sidecar_client.py` | **Delete** | Entirely replaced by MSAL Python |
| `infra/cloudformation/stack.yaml` | Modify | Remove ~25 VPC/ECS/ALB resources; update AgentRuntime env vars |
| Secrets Manager secret | Re-provision | Replace client secret JSON with certificate JSON (out of band) |
| Entra Blueprint app registration | Re-configure | Add certificate credential (out of band) |

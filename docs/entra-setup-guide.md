# Entra Agent ID OBO Integration Specification

## 1. The OBO Flow — Step by Step

The Agent ID OBO flow is a **two-stage token exchange** that differs from standard OAuth2 OBO. The agent identity blueprint and the agent identity are *separate* Entra objects; the exchange must prove both the agent credential AND carry the user's delegated token.

### Inputs

| Input | Description |
|---|---|
| `Tc` | The user's access token issued to the **SPA** by Entra. Audience = Blueprint client_id. The SPA sends this to AgentCore; AgentCore passes it through to the Python agent. |
| Blueprint `client_credential` | A short-lived AWS IAM JWT issued for the AgentCore execution role and trusted through a Blueprint federated identity credential. |

### Step-by-step

```
Step 1 — SPA authenticates user
  MSAL.js → Entra /authorize (auth code + PKCE)
  Scope requested: api://{blueprint-client-id}/agent.invoke
  → Entra issues Tc (user JWT, aud = {blueprint-client-id})

Step 2 — SPA calls AgentCore
  POST {agentcore-endpoint}
  Authorization: Bearer {Tc}
  → AgentCore JWT authorizer validates Tc (OIDC discovery from Entra)
  → AgentCore passes Tc through to the Python agent as the inbound bearer token

Step 3 — Python agent extracts Tc from Authorization header
  The agent reads the inbound token from the header. The raw JWT (no "Bearer " prefix) is
  stored in a module-level variable for the duration of the invocation.

Step 4 — MSAL Python: Stage 1 (FMI / T1)
  Blueprint ConfidentialClientApplication calls acquire_token_for_client:
  POST https://login.microsoftonline.com/{tenant-id}/oauth2/v2.0/token
  client_id={blueprint-client-id}
  &scope=api://AzureADTokenExchange/.default
  &fmi_path={agent-identity-object-id}          <- Agent Identity Object ID
  &client_assertion={AWS-IAM-JWT}                <- five-minute STS token
  &grant_type=client_credentials
  → Entra issues T1 (aud = Entra ID Token Exchange, sub = agent-identity)
  T1 is cached by the long-lived Blueprint CCA instance.

Step 5 — MSAL Python: Stage 2 (OBO / TR)
  A transient ConfidentialClientApplication is built with T1 as client_assertion.
  POST https://login.microsoftonline.com/{tenant-id}/oauth2/v2.0/token
  client_id={agent-identity-object-id}
  &scope=api://{echo-api-client-id}/access_as_user
  &client_assertion_type=urn:ietf:params:oauth:client-assertion-type:jwt-bearer
  &client_assertion={T1}                        <- proves agent identity
  &grant_type=urn:ietf:params:oauth:grant-type:jwt-bearer
  &assertion={Tc}
  &requested_token_use=on_behalf_of
  → Entra validates: T1.aud == Entra Token Exchange, Tc.aud == blueprint-client-id
  → Entra issues TR (resource token, aud = echo-api-client-id, sub = user)

Step 6 — Python agent calls Echo REST API directly
  POST https://{echo-api-host}/echo
  Authorization: Bearer {TR}
  → Echo API validates TR; aud matches its own client_id; sub = original user
  → Agent returns only the response body to the LLM — TR is never passed to LLM
```

### What makes Agent ID OBO different from standard OBO

| Standard OAuth2 OBO | Agent ID OBO |
|---|---|
| One token exchange: assertion=user_token, client authenticates with secret/cert | Two-stage: first get T1 (blueprint→agent-identity impersonation), then OBO with T1 as client_assertion |
| `client_assertion` = the client's own secret/cert JWT | `client_assertion` = T1, a token Entra just issued to prove the agent identity relationship |
| Single app registration | Two Entra objects: Blueprint (under Agents) + Agent Identity (child object of Blueprint, Object ID only) |
| `fmi_path` parameter not used | `fmi_path={agent-identity-object-id}` required in Stage 1 to target the child identity |

---

## 2. Entra Objects Required

There are **four Entra objects** in this PoC. They are **not all app registrations** — see the type column carefully.

| # | Name | Type | Has client_id? | Has client_secret? |
|---|---|---|---|---|
| 2a | `agentid-poc-spa` | App Registration (SPA, public client) | ✅ Yes | ❌ No |
| 2b | `agentid-poc-blueprint` | Agent Identity Blueprint (created under Agents, not App registrations) | ✅ Yes | ❌ No; trusts the AWS role through a FIC |
| 2c | `agentid-poc-identity` | Agent Identity Object (new Entra object type, child of Blueprint) | ✅ Yes (same as object id) | ❌ No |
| 2d | `agentid-poc-echo-api` | App Registration (Web API) | ✅ Yes | Optional |

### 2a. App Registration — SPA Frontend (`agentid-poc-spa`)

This is a standard public client SPA app registration. MSAL.js signs in users against this.

| Property | Value |
|---|---|
| Display name | `agentid-poc-spa` |
| Type | **App Registration** — Single-page application (public client) |
| Platform | SPA — no client secret |
| Redirect URI | `http://localhost:3000` (dev) / `https://{spa-host}` (prod) |
| API permissions | `api://{blueprint-client-id}/agent.invoke` (delegated) |
| Admin consent required | No (user consent sufficient if tenant allows it) |

### 2b. Agent Identity Blueprint (`agentid-poc-blueprint`)

This is the **parent credential holder**. MSAL Python authenticates as the Blueprint
with an AWS IAM JWT whose issuer and execution-role subject are trusted by a federated
identity credential. It is a **Blueprint object**, not a standard app registration.

Created in: **Entra admin center → Identity  → Agents → Agent Blueprints → New** (the exact label may be "Agent Blueprints" or similar under the Agents blade)

| Property | Value |
|---|---|
| Display name | `agentid-poc-blueprint` |
| Type | **Agent Identity Blueprint (Agents blade — not App registrations)** |
| Federated credential | Issuer = AWS account token issuer; subject = exact AgentCore `ExecutionRoleArn`; audience = `api://AzureADTokenExchange` |
| Expose an API & Scope via Manifest | Highly redacted part - important is the `identifierUris` and `api:oauth2PermissionScopes` (ref Blueprint manifest bellow) |

After enabling AWS outbound identity federation and deploying the stack, collect the
two account/stack-specific values:

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

From the Blueprint's management page, select **Credentials** under **Developer
settings**, open **Federated credentials**, and select **Add credential → Other
issuer**, then set:

```text
Name:     aws-agentcore-runtime
Issuer:   <the value of $issuer>
Subject:  <the value of $subject>
Audience: api://AzureADTokenExchange
```

Use the values exactly; matching is case-sensitive. The subject is the IAM role ARN
from CloudFormation, not an STS assumed-role ARN, runtime ARN, or Agent Identity ID.

Excerpt from the Agent Blueprint manifest

```json
{
	"appId": "785ae446-ed0f-4d9c-a75f-a386a3115a18",
	"displayName": "[ai] AWS Agent Core Blueprint",
	"identifierUris": [
		"api://785ae446-ed0f-4d9c-a75f-a386a3115a18"
	],
	"id": "785ae446-ed0f-4d9c-a75f-a386a3115a18",
	"api": {
		"acceptMappedClaims": null,
		"knownClientApplications": [],
		"requestedAccessTokenVersion": 2,
		"oauth2PermissionScopes": [
			{
				"adminConsentDescription": "Allow the application to access the agent on behalf of the signed-in user.",
				"adminConsentDisplayName": "Access Agent Core Agents",
				"id": "1a795649-226d-4f61-8e5c-e1ff2a84317f",
				"isEnabled": true,
				"type": "User",
				"userConsentDescription": null,
				"userConsentDisplayName": null,
				"value": "access_agent"
			}
		],
		"preAuthorizedApplications": []
	}
} 
``` 

> **Note:** The blueprint is the **audience** of `Tc` only. This is the initial authorization for the SPA front-end to the Agent.

### 2c. Agent Identity Object (`agentid-poc-identity`)

This is a **new Entra object type**, a child object of the Blueprint. It does **not** have its own `client_secret`. It inherits credentials from the Blueprint; the Blueprint impersonates it via the `fmi_path` parameter. The client_id of the Agent Identity is same as its object id.

#### How to create it

**Option A — Entra admin center (recommended for PoC):**

1. Navigate to: **Entra admin center → Identity → Agents → Agent identities → New Agent Identity**
2. Fill in:
   - **Parent Blueprint:** select `agentid-poc-blueprint` (the app registration created in 2b)
   - **Display name:** `agentid-poc-identity`
   - **Sponsors and Owners:** leave defaults or add your peers
   
3. After creation, note the **Object ID** (`{agent-identity-object-id}`) — this is the only identifier the agent identity has.


**Option B — Microsoft Graph API:**

```http
POST https://graph.microsoft.com/v1.0/serviceprincipals/Microsoft.Graph.AgentIdentity
Content-Type: application/json

{
    "displayName": "[ai] Agentcore",
    "agentAppId": "{ agent_blueprint_appId }",
		"sponsors@odata.bind": [
    	"https://graph.microsoft.com/v1.0/users/{your-human-user-object-id}"
  	]
}
```

Response includes the `id` field — this is the `{agent-identity-object-id}` used in `fmi_path`.

#### Fields and properties

| Property | Value |
|---|---|
| Display name | `agentid-poc-identity` |
| Parent Blueprint | `{blueprint-client-id}` (the Blueprint app registration above) |
| **Object ID** | `{agent-identity-object-id}` — the ONLY identifier; used as `fmi_path` in token requests |
| client_id | ✅ **None** — client_id is same as the object id |
| client_secret | ❌ **None** — credentials are managed in the Blueprint |

> The agent identity does **not** have its own client secret. The Blueprint impersonates it using `fmi_path={agent-identity-object-id}` in the `client_credentials` token request.

### 2d. App Registration — Echo REST API (`agentid-poc-echo-api`)

This is a standard confidential-client/API-only app registration that exposes a scope for the agent to call.

Created in: **Entra admin center → Identity → App registrations → New registration**

| Property | Value |
|---|---|
| Display name | `agentid-poc-echo-api` |
| Type | **App Registration** (Web API) |
| Expose an API → App ID URI | `api://{echo-api-client-id}` |
| Expose an API → Scope | `access_as_user` (delegated) |
| Who can consent | Admins and users |
| API permissions | None (it is a resource, not a consumer) |
| Token validation | Validate `aud == {echo-api-client-id}`, `iss == https://login.microsoftonline.com/{tenant-id}/v2.0` |

### 2e. Permission Grant Summary

```
SPA  ──requests──►  api://{blueprint-client-id}/agent.invoke   (user delegates to SPA)
Agent Identity  ──requests──►  api://{echo-api-client-id}/access_as_user  (admin consent required)
```

### 2f. Admin Consent Requirements

- `api://{echo-api-client-id}/access_as_user` on the Agent Identity Object requires **tenant admin consent** (or user consent if the scope's `userConsentRequired` is set accordingly).
- If `InheritDelegatedPermissions=true` on the agent identity, consent granted to the blueprint flows down — but the Echo API permission must still be explicitly added to the agent identity object.

TODO:
Add reference to Access Packages, Admin Consent URL, Admin Consent MS Graph
---

## 3. Python Agent Code Pattern

The Entra SDK sidecar has been replaced by **MSAL Python 1.37.0+** running directly
inside the agent container. There is no sidecar process or localhost HTTP call.

### 3a. Module-level setup

```python
import msal, boto3, os

ENTRA_TENANT_ID    = os.environ["ENTRA_TENANT_ID"]
BLUEPRINT_CLIENT_ID = os.environ["BLUEPRINT_CLIENT_ID"]
AGENT_IDENTITY_ID  = os.environ["AGENT_IDENTITY_ID"]   # Agent Identity Object ID
AUTHORITY = "https://login.microsoftonline.com/{}".format(ENTRA_TENANT_ID)
FMI_SCOPE = ["api://AzureADTokenExchange/.default"]

_sts = None

def _get_aws_assertion(*args, **kwargs) -> str:
    global _sts
    if _sts is None:
        _sts = boto3.client("sts", region_name=os.environ["AWS_DEFAULT_REGION"])
    return _sts.get_web_identity_token(
        Audience=["api://AzureADTokenExchange"],
        SigningAlgorithm="RS256",
        DurationSeconds=300,
    )["WebIdentityToken"]

# Long-lived Blueprint CCA — in-memory token cache persists across invocations
_blueprint_app = msal.ConfidentialClientApplication(
    BLUEPRINT_CLIENT_ID,
    client_credential={"client_assertion": _get_aws_assertion},
    authority=AUTHORITY,
)
```

`_get_aws_assertion` is a delegate, not an eagerly evaluated function call. Creating
the CCA does not mint a JWT. MSAL invokes the delegate only when it must send a token
request, and the delegate returns a newly minted assertion each time.

### 3b. Two-stage token exchange (_get_downstream_token)

```python
def _get_downstream_token(inbound_user_token: str, scopes: list) -> str:
    # Stage 1 (FMI / T1): Blueprint CCA impersonates the Agent Identity
    t1_result = _blueprint_app.acquire_token_for_client(
        scopes=FMI_SCOPE,
        fmi_path=AGENT_IDENTITY_ID,
    )
    if "access_token" not in t1_result:
        raise RuntimeError("FMI Stage 1 failed: {}".format(t1_result.get("error")))

    # Stage 2 (OBO / TR): T1 is the client_assertion in the OBO grant
    obo_app = msal.ConfidentialClientApplication(
        AGENT_IDENTITY_ID,
        client_credential={"client_assertion": t1_result["access_token"]},
        authority=AUTHORITY,
    )
    tr_result = obo_app.acquire_token_on_behalf_of(
        user_assertion=inbound_user_token,
        scopes=scopes,
    )
    if "access_token" not in tr_result:
        raise RuntimeError("OBO Stage 2 failed: {}".format(tr_result.get("error")))
    return tr_result["access_token"]
```

### 3c. call_echo_api tool

```python
@tool
def call_echo_api(prompt: str) -> str:
    """Call the Echo API on behalf of the authenticated user."""
    inbound_token = _current_token   # set per-invocation, never logged
    if not inbound_token:
        return "Error: no authentication token available"
    try:
        tr = _get_downstream_token(inbound_token, [ECHO_API_SCOPE])
        resp = requests.post(
            "{}/echo".format(ECHO_API_URL),
            json={"message": prompt},
            headers={"Authorization": "Bearer {}".format(tr)},
            timeout=10,
        )
        resp.raise_for_status()
        return str(resp.json().get("echo", resp.json()))
    except Exception as exc:
        return "Error calling Echo API: {}".format(type(exc).__name__)
```

### 3d. How the agent accesses the inbound user token

AgentCore passes the original `Authorization: Bearer {Tc}` through to the Python
agent payload. The agent reads it from the payload dict (keys `"token"`,
`"authorization"`, or `"access_token"`), strips the `"Bearer "` prefix, and stores
the raw JWT in a module-level variable for the duration of the invocation. The token
is cleared in the `finally` block after the invocation completes.

```python
# The MSAL OBO call requires the raw JWT (no "Bearer " prefix)
inbound_token = payload.get("token") or payload.get("authorization") or ""
if inbound_token.lower().startswith("bearer "):
    inbound_token = inbound_token[7:].strip()
```


---

## 4. Agent Environment Variables

MSAL Python reads all Entra configuration from environment variables. There is no
sidecar process, no `appsettings.json`, and no `SIDECAR_URL`.

### 4a. Required environment variables

```bash
# Entra tenant
ENTRA_TENANT_ID={tenant-id}

# Blueprint app registration client ID
BLUEPRINT_CLIENT_ID={blueprint-client-id}

# Agent Identity Object ID (NOT a client_id — see section 2c)
AGENT_IDENTITY_ID={agent-identity-object-id}

# Echo API
ECHO_API_URL=https://{echo-api-host}
ECHO_API_SCOPE=api://{echo-api-client-id}/access_as_user

# MCP Server (optional)
MCP_SERVER_URL=https://{mcp-server-host}
MCP_SERVER_SCOPE=api://{mcp-server-client-id}/MCP.User.Read.All
```

### 4b. AWS execution-role assertion

The runtime calls the regional STS `GetWebIdentityToken` API with its temporary
execution-role credentials. The CloudFormation role policy restricts the audience to
`api://AzureADTokenExchange` and the duration to at most 300 seconds. No long-lived
Blueprint credential is stored in AWS.

### 4c. AgentCore container — no sidecar container

There is only one container in the AgentCore task: the Python agent. No sidecar
container is needed. The agent connects to `login.microsoftonline.com` and to the
Echo API directly over the public internet via AgentCore's built-in egress.

```json
{
  "containerDefinitions": [
    {
      "name": "python-agent",
      "image": "{ecr-repo}/agentid-poc-agent:latest",
      "environment": [
        {"name": "ENTRA_TENANT_ID",     "value": "{tenant-id}"},
        {"name": "BLUEPRINT_CLIENT_ID", "value": "{blueprint-client-id}"},
        {"name": "AGENT_IDENTITY_ID",   "value": "{agent-identity-object-id}"},
        {"name": "ECHO_API_URL",        "value": "https://{echo-api-host}"},
        {"name": "ECHO_API_SCOPE",      "value": "api://{echo-api-client-id}/access_as_user"}
      ],
      "secrets": []
    }
  ]
}
```

---

## 5. Token Flow Diagram (Text)

```
┌─────────┐  MSAL.js   ┌───────────────────────────────────────────────────┐
│   SPA   │───────────►│                   Microsoft Entra ID              │
│(browser)│ auth code  │  tenant: {tenant-id}                              │
└────┬────┘ + PKCE     └───────────────────────────────────────────────────┘
     │                        │
     │  (1) GET Tc            │ issues Tc
     │  scope:                │ (JWT, aud={blueprint-client-id},
     │  api://{blueprint}/    │  sub=user, scope=agent.invoke)
     │  agent.invoke          │
     │◄───────────────────────┘
     │
     │  (2) POST {agentcore-endpoint}
     │  Authorization: Bearer {Tc}
     ▼
┌─────────────┐
│  AgentCore  │  JWT authorizer validates Tc
│  Runtime    │  (OIDC discovery: login.microsoftonline.com/{tenant}/v2.0)
│  (AWS)      │  allowedAudience = {blueprint-client-id}
└──────┬──────┘
       │  (3) invokes Python agent
       │  passes Authorization: Bearer {Tc} through
       ▼
┌──────────────────┐
│  Python Agent    │  extracts raw JWT from payload
│  (AgentCore      │  stores in _current_token (per-invocation)
│   container)     │  calls _get_downstream_token(Tc, [ECHO_API_SCOPE])
└────────┬─────────┘
         │
         │  (4) Stage 1 (FMI/T1) — MSAL Python in-process
         │  acquire_token_for_client
         │  fmi_path={agent-identity-object-id}
         │  client_credential={secret/cert}
         ▼
┌────────────────────────────────────┐
│          Microsoft Entra ID        │
│  issues T1 (aud={blueprint-id},    │
│            sub={agent-identity})   │
└─────────────────┬──────────────────┘
                  │
┌─────────────────▼──────────────────┐
│  (5) Stage 2 (OBO/TR)              │
│  acquire_token_on_behalf_of        │
│  client_assertion=T1               │
│  user_assertion=Tc                 │
│  scopes=[ECHO_API_SCOPE]           │
└─────────────────┬──────────────────┘
                  │
┌─────────────────▼──────────────────┐
│          Microsoft Entra ID        │
│  validates T1.aud==blueprint       │
│  validates Tc.aud==TokenExchange   │
│  issues TR (aud={echo-api-id},     │
│             sub=user)              │
└─────────────────┬──────────────────┘
                  │  TR returned to agent
┌─────────────────▼──────────────────┐
│  Python Agent (continued)          │
│  POST {ECHO_API_URL}/echo          │
│  Authorization: Bearer {TR}        │
└─────────────────┬──────────────────┘
                  │
                  ▼
┌─────────────────────────────────────┐
│  Echo REST API                      │
│  validates TR (aud={echo-api-id},   │
│  sub=user); returns response        │
└─────────────────────────────────────┘
```

---

## 6. What the SPA Must Send to AgentCore

### 6a. What scope the SPA requests from Entra

```javascript
// MSAL.js config (SPA)
const msalConfig = {
  auth: {
    clientId: '{spa-client-id}',
    authority: 'https://login.microsoftonline.com/{tenant-id}',
    redirectUri: 'http://localhost:3000',
  }
};

// Token acquisition
const tokenRequest = {
  scopes: ['api://{blueprint-client-id}/agent.invoke'],
};
const tokenResponse = await msalInstance.acquireTokenSilent(tokenRequest);
const userToken = tokenResponse.accessToken;  // This is Tc
```

The SPA must request **exactly** the delegated scope `api://{blueprint-client-id}/agent.invoke`. This causes Entra to issue `Tc` with:
- `aud = {blueprint-client-id}`  — validated by MSAL OBO in Stage 2
- `scp = agent.invoke`
- `sub = {user-object-id}`

### 6b. How AgentCore's JWT authorizer validates Tc

AgentCore is configured to point at Entra's OIDC discovery endpoint:

```yaml
# AgentCore JWT authorizer configuration (CloudFormation)
authorizerConfiguration:
  customJWTAuthorizer:
    discoveryUrl: https://login.microsoftonline.com/{tenant-id}/v2.0/.well-known/openid-configuration
    allowedAudience:
      - {blueprint-client-id}
      - api://{blueprint-client-id}
    allowedScopes:
      - access_agent
```

The authorizer validates exactly three things:
- `iss` — the issuer, validated automatically via the OIDC `discoveryUrl` (no extra config).
- `aud` — must equal the Blueprint client ID (`AgentCoreAppClientId`).
- `scp` — must contain `access_agent` (the delegated scope), via `allowedScopes`.

It no longer validates the calling client (`azp` / `allowedClients`). AgentCore fetches Entra's JWKS, validates the signature, and validates expiry. **It does not do a token exchange** — it simply gate-keeps the request. The raw `Tc` is passed through to the agent.

### 6c. How the Python agent accesses the inbound user token

AgentCore passes the inbound token through to the Python agent payload. The agent
reads it from the payload dict, strips the `"Bearer "` prefix, and passes the raw
JWT to `_get_downstream_token` as `inbound_user_token`:

```python
# agent.py — extract raw JWT from AgentCore payload
inbound_token = payload.get("token") or payload.get("authorization") or ""
if inbound_token.lower().startswith("bearer "):
    inbound_token = inbound_token[7:].strip()
# inbound_token is now the raw JWT — ready for acquire_token_on_behalf_of
```

### 6d. Does AgentCore pass the raw Bearer token or a claims object?

**AgentCore passes the raw Bearer token** (`Authorization: Bearer {Tc}`) to the agent
payload. It does NOT decode it into a claims object. MSAL Python's
`acquire_token_on_behalf_of` expects the raw JWT string (no `"Bearer "` prefix) as
the `user_assertion` parameter.

---

## Appendix: Placeholder Reference

| Placeholder | Description | Where used |
|---|---|---|
| `{tenant-id}` | Entra tenant GUID | `ENTRA_TENANT_ID` env var, AgentCore discoveryUrl, MSAL authority |
| `{blueprint-client-id}` | Client ID of the Blueprint (Agents section) | `BLUEPRINT_CLIENT_ID` env var, OBO `client_id`, audience of Tc and T1 |
| `{aws-token-issuer}` | Account-specific issuer returned by IAM | Blueprint FIC issuer |
| `{runtime-execution-role-arn}` | CloudFormation `ExecutionRoleArn` output | Blueprint FIC subject |
| `{agent-identity-object-id}` | Object ID of the Agent Identity Object (NOT a client_id) | `AGENT_IDENTITY_ID` env var, `fmi_path` in Stage 1 token request |
| `{echo-api-client-id}` | Client ID of the Echo REST API app registration | `ECHO_API_SCOPE` env var, agent identity permission |
| `{echo-api-host}` | Hostname of the Echo REST API | `ECHO_API_URL` env var |
| `{mcp-server-client-id}` | Client ID of MCP server app registration (if needed) | `MCP_SERVER_SCOPE` env var |
| `{spa-client-id}` | Client ID of the SPA app registration | MSAL.js config |
| `{agentcore-endpoint}` | AWS AgentCore Runtime invoke URL | SPA HTTP calls |

> ⚠️ **Important:** The Agent Identity Object (`agentid-poc-identity`) does **NOT** have a `client_id`. It only has an `{agent-identity-object-id}` (Object ID). Any reference to `{agent-identity-client-id}` in older drafts of this spec was incorrect and has been removed.

> The Blueprint has no long-lived client secret. Its FIC trusts only the AWS account
> issuer and the exact AgentCore runtime execution-role ARN.
# Entra setup

This guide configures the Entra objects used by the three-agent deployment. Create
objects and grants before deploying AWS; deployment does not provision Entra objects.

## Object inventory

| Name | Type | Used by |
|---|---|---|
| SPA | Public-client app registration | User sign-in and Agent 1 invocation |
| Blueprint 1 / Agent Identity 1 | Agent Identity Blueprint / child identity | Agent 1 orchestrator |
| Blueprint 2 / Agent Identity 2 | Agent Identity Blueprint / child identity | Agent 2 A2A specialist |
| Blueprint 3 / Agent Identity 3 | Agent Identity Blueprint / child identity | Agent 3 autonomous Lambda |
| Echo API | Web API app registration | Agent 1 OBO demonstration |

For every Blueprint, create its BlueprintPrincipal/service principal in the tenant.
The Blueprint is the credential holder; each Agent Identity is a separate service
principal that cannot hold a secret or certificate.

Record these IDs:

```text
Blueprint 1 application (client) ID
Agent Identity 1 object ID
Blueprint 2 application (client) ID
Agent Identity 2 object ID
Blueprint 3 application (client) ID
Agent Identity 3 object ID
SPA application (client) ID
Echo API application (client) ID
```

Agent Identity **object IDs** are the `fmi_path` values and are also passed as
`AGENT_IDENTITY_ID`; do not substitute a Blueprint application ID.

## Configure the SPA and Blueprint 1

1. Create a single-tenant SPA registration. Add `http://localhost:3000` and any
   production origin as SPA redirect URIs.
2. On Blueprint 1, set `identifierUris` to `api://{blueprint-1-client-id}` and
   access-token version to `2`.
3. Expose a delegated **User** scope named `agent.invoke`.
4. Add `api://{blueprint-1-client-id}/agent.invoke` as a delegated permission on
   the SPA registration. Grant tenant consent, or preauthorize the SPA on the
   Blueprint when user consent is allowed.
5. Configure local `spa/msal-config.js` to request exactly:

   ```javascript
   const agentCoreScopes = ["api://{blueprint-1-client-id}/agent.invoke"];
   ```

The AgentCore authorizer checks this audience and scope before Agent 1 runs.

## Configure Blueprint 2 authorization

On Blueprint 2, set `identifierUris` to `api://{blueprint-2-client-id}` and
access-token version to `2`. Expose delegated **User** scope `user_impersonation`.
Add these roles to the Blueprint 2 manifest, using generated GUIDs:

```json
{
  "appRoles": [
    {
      "id": "<user-role-guid>",
      "value": "Agent2.Tools.User",
      "displayName": "Use Agent 2 directory tools",
      "description": "Use Agent 2 directory tools on behalf of a user.",
      "allowedMemberTypes": ["User"],
      "isEnabled": true
    },
    {
      "id": "<application-role-guid>",
      "value": "Agent2.Chat.Application",
      "displayName": "Call Agent 2 without tools",
      "description": "Allow autonomous tool-free calls to Agent 2.",
      "allowedMemberTypes": ["Application"],
      "isEnabled": true
    }
  ]
}
```

Make both assignments on the **Blueprint 2 resource service principal**:

| Principal | Assignment | Result |
|---|---|---|
| Demonstration users or groups | `Agent2.Tools.User` | Agent 1 can request delegated Agent 2 directory access for that user |
| Agent Identity 3 | `Agent2.Chat.Application` | Agent 3 receives an app-only Blueprint 2 token |

Role definitions, role assignments, and delegated consent are separate controls.
Blueprint role assignment is currently managed through Microsoft Graph or access
packages rather than a complete Entra admin-center experience.

## Grant delegated access per Agent Identity

Do not rely on `requiredResourceAccess`, a browser admin-consent URL, or a grant on
the BlueprintPrincipal. Create delegated grants for the Agent Identity service
principals that actually request OBO tokens:

| Client Agent Identity | Resource | Delegated scopes |
|---|---|---|
| Agent Identity 1 | Blueprint 2 resource service principal | `user_impersonation` |
| Agent Identity 2 | MCP resource service principal | Values in `MCP_SERVER_SCOPE` |
| Agent Identity 1 | Echo API resource service principal | `access_as_user` |

Create each grant with `POST /oauth2PermissionGrants`, `clientId` set to the Agent
Identity service-principal object ID, `resourceId` set to the resource
service-principal object ID, `consentType` set to `AllPrincipals`, and an explicit
future `expiryTime`. Use `POST /servicePrincipals/{principal-id}/appRoleAssignments`
for the Agent Identity 3 application-role assignment instead.

The [authorization options for Entra Agent ID](https://dev.to/astaykov/authorization-options-for-your-ai-agents-with-entra-agent-id-4p0k)
article covers role assignment, user assignment, Conditional Access, and access
packages. If assignment is required, assign approved users/groups to Blueprint 1
and to each downstream resource they must access; user assignment gates both
invocation and OBO access.

## Configure the Echo API

Create a single-tenant web API registration. Set `api://{echo-api-client-id}` as
its App ID URI and expose delegated scope `access_as_user`. The deployed API
validates its client ID as the audience.

## Add AWS federated credentials

Deploy the stack once to obtain the role outputs. Then add one FIC to each
Blueprint:

| Blueprint | FIC subject |
|---|---|
| Blueprint 1 | `ExecutionRoleArn` |
| Blueprint 2 | `SpecialistRoleArn` |
| Blueprint 3 | `AutonomousRoleArn` |

All three FICs use the account issuer from AWS IAM and audience
`api://AzureADTokenExchange`:

```powershell
$issuer = aws iam get-outbound-web-identity-federation-info `
  --profile agentid-poc --query IssuerIdentifier --output text
```

The subjects must exactly match the stack outputs. They are IAM role ARNs, not
runtime ARNs, assumed-role session ARNs, or Agent Identity IDs.

## Token chain

```text
SPA -> Blueprint 1: agent.invoke
Agent 1 -> Blueprint 2: OBO user_impersonation + Agent2.Tools.User
Agent 2 -> MCP: OBO delegated MCP scopes

Agent 3 -> Blueprint 2: client credentials + Agent2.Chat.Application
```

Each Agent Identity first uses its Blueprint's FIC to obtain an FMI token, then
uses that identity-bound token for OBO or client credentials. Use `/.default` for
the client-credentials exchange. Allow time for new grants to propagate before
testing.

## Deployment values

Copy `.env.example` to `.env` and set:

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

Continue with the [deployment guide](deployment-guide.md).

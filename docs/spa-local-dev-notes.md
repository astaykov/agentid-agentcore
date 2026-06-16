# SPA Local Dev Notes

## What does the SPA call?

**AgentCore only.** The SPA sends every message to `agentCoreEndpoint` (a single `POST` with
`{ message }` and a Bearer token). It never calls the Echo API directly.

The PoC flow is: `SPA → AgentCore (AWS) → Echo API` — the SPA is only aware of AgentCore.
The Echo API is the deployed Lambda + API Gateway; the agent calls it on the user's behalf.

---

## Values to fill in `msal-config.js` before the SPA works

All four placeholders in `spa/msal-config.js` must be replaced with real values:

| Placeholder | What to put there |
|---|---|
| `SPA_CLIENT_ID` | Client ID of the **agentid-poc-spa** Entra app registration |
| `TENANT_ID` | Entra tenant ID |
| `BLUEPRINT_CLIENT_ID` | Client ID of the **Blueprint / AgentCore** Entra app registration (used to build the scope `api://<id>/access_as_user`) |
| `AGENTCORE_ENDPOINT_URL` | The AgentCore invoke URL from CloudFormation Outputs |

These are intentionally left as literals so they are never committed with real values.
Fill them in locally before opening the SPA in a browser.

---

## Dockerfile assessment

```dockerfile
FROM nginx:alpine
COPY . /usr/share/nginx/html
EXPOSE 80
```

✅ Correct. The SPA is plain HTML/JS with no build step — nginx serves static files directly.
`docker-compose.yml` maps host port **3000 → container port 80**, so the SPA is reachable at
`http://localhost:3000` when running locally.

The `redirectUri` in `msal-config.js` is `window.location.origin`, which automatically resolves
to `http://localhost:3000` in this setup — make sure this URI is registered in the Entra app.

---

## Summary

| Item | Status |
|---|---|
| SPA → Echo API direct calls | ❌ None (correct per architecture) |
| SPA → AgentCore | ✅ Single endpoint, Bearer-token authenticated |
| `msal-config.js` ready to use | ⚠️ 4 placeholders must be filled in locally |
| Dockerfile correct | ✅ Simple nginx static serve, no build step |
| `redirectUri` | ✅ Dynamic (`window.location.origin`) — register `http://localhost:3000` in Entra |

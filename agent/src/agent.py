"""
AgentCore Agent - Entra Agent ID PoC
Uses MSAL Python FIC/FMI directly for Entra Agent ID OBO token acquisition.
Token NEVER passed to LLM.

PoC note: Blueprint credential uses client_secret. Migrate to certificate
(SNI/x5c) for production - client_secret cannot satisfy FMI SNI requirements
in a hardened Entra tenant policy.
"""
import os
import json
import base64
import logging
import threading
import requests
import msal
import boto3
from botocore.config import Config as BotocoreConfig
from typing import Any

from bedrock_agentcore import BedrockAgentCoreApp, RequestContext
from strands import Agent, tool

print("app.py: starting import", flush=True)

try:
    from strands.tools.mcp import MCPClient
    # Microsoft-hosted MCP endpoints speak Streamable HTTP. Prefer it; fall back
    # to the legacy SSE transport only if Streamable HTTP is unavailable in the
    # installed mcp SDK.
    try:
        from mcp.client.streamable_http import streamablehttp_client
    except ImportError:
        streamablehttp_client = None
    try:
        from mcp.client.sse import sse_client
    except ImportError:
        sse_client = None
    _MCP_AVAILABLE = streamablehttp_client is not None or sse_client is not None
except ImportError:
    streamablehttp_client = None
    sse_client = None
    _MCP_AVAILABLE = False

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

if not _MCP_AVAILABLE:
    logger.warning("MCP libraries not available; MCP integration disabled")


# ---------------------------------------------------------------------------
# Custom exceptions
# ---------------------------------------------------------------------------
class TokenAcquisitionError(Exception):
    """
    Raised when the Entra Agent ID FMI/OBO token exchange fails (Stage-1 FMI or
    Stage-2 OBO), for ANY downstream scope (Echo API or MCP server).

    Distinct from generic errors so callers can tell an AUTH failure apart from
    optional-MCP transport/wiring problems: a token failure is surfaced to the
    end user verbatim (demo requirement), whereas an MCP transport failure stays
    on the non-fatal graceful-fallback path. Messages carry the non-secret MSAL
    error/error_description (e.g. AADSTS codes) ONLY - never a token or signature.
    """


# ---------------------------------------------------------------------------
# Environment variables (all required unless noted)
# ---------------------------------------------------------------------------
ENTRA_TENANT_ID = os.environ["ENTRA_TENANT_ID"]
BLUEPRINT_SECRET_ARN = os.environ["BLUEPRINT_SECRET_ARN"]
AGENT_IDENTITY_ID = os.environ["AGENT_IDENTITY_ID"]
ECHO_API_URL = os.environ["ECHO_API_URL"]
ECHO_API_SCOPE = os.environ.get("ECHO_API_SCOPE", "api://echo-api/access_as_user")
MCP_SERVER_URL = os.environ.get("MCP_SERVER_URL", "")
MCP_SERVER_SCOPE = os.environ.get("MCP_SERVER_SCOPE", "")
DEFAULT_MODEL_ID = os.getenv("BEDROCK_MODEL_ID", "eu.amazon.nova-micro-v1:0")

AUTHORITY = "https://login.microsoftonline.com/{}".format(ENTRA_TENANT_ID)
FMI_SCOPE = ["api://AzureADTokenExchange/.default"]

app = BedrockAgentCoreApp()
print("app.py: BedrockAgentCoreApp created", flush=True)

# ---------------------------------------------------------------------------
# Blueprint ConfidentialClientApplication - lazy-initialized on first call.
# Loaded once per process so the in-memory token cache persists across
# invocations. Secrets Manager is NOT called at import time to avoid blocking
# the 30s AgentCore init window.
# ---------------------------------------------------------------------------
_blueprint_lock = threading.Lock()
_blueprint_app: msal.ConfidentialClientApplication | None = None
_blueprint_client_id: str = ""


def _load_blueprint_secret() -> tuple[str, str]:
    """
    Fetch Blueprint credentials from Secrets Manager.
    Expected secret JSON: {"clientId": "...", "clientSecret": "..."}
    Returns (clientId, clientSecret) - never logged or stored beyond this call.
    Region is derived from the ARN when possible, so the secret can live in any
    region (e.g., us-east-1) regardless of where the runtime is deployed.
    """
    # Prefer the region embedded in the ARN (arn:aws:sm:{region}:{acct}:secret:{name})
    parts = BLUEPRINT_SECRET_ARN.split(":")
    region = (
        parts[3]
        if len(parts) >= 4 and parts[3]
        else (os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION", "eu-central-1"))
    )
    logger.info("Loading blueprint secret from region %s", region)
    sm = boto3.client(
        "secretsmanager",
        region_name=region,
        config=BotocoreConfig(connect_timeout=5, read_timeout=10),
    )
    raw = sm.get_secret_value(SecretId=BLUEPRINT_SECRET_ARN)
    secret = json.loads(raw["SecretString"])
    return secret["clientId"], secret["clientSecret"]


def _ensure_blueprint_app() -> tuple["msal.ConfidentialClientApplication", str]:
    """Return (blueprint_app, client_id), initializing lazily on first call."""
    global _blueprint_app, _blueprint_client_id
    if _blueprint_app is not None:
        return _blueprint_app, _blueprint_client_id
    with _blueprint_lock:
        if _blueprint_app is not None:
            return _blueprint_app, _blueprint_client_id
        client_id, client_secret = _load_blueprint_secret()
        _blueprint_client_id = client_id
        _blueprint_app = msal.ConfidentialClientApplication(
            client_id,
            client_credential=client_secret,
            authority=AUTHORITY,
        )
        del client_secret
        logger.info("Blueprint MSAL app initialised for client %s", client_id[:8] + "...")
    return _blueprint_app, _blueprint_client_id


# ---------------------------------------------------------------------------
# Agent (OBO) ConfidentialClientApplication - lazy-initialized singleton.
# Built ONCE per process and reused so its in-memory token cache survives
# across invocations (mirrors _ensure_blueprint_app). A per-call transient CCA
# could never cache. Authenticates with a CALLABLE client_assertion provider
# that performs Stage-1 FMI via the persistent Blueprint CCA on demand.
# ---------------------------------------------------------------------------
_agent_lock = threading.Lock()
_agent_app: msal.ConfidentialClientApplication | None = None


def _ensure_agent_app() -> "msal.ConfidentialClientApplication":
    """
    Return the persistent agent (OBO) CCA, building it once on first call.

    The CCA is keyed to AGENT_IDENTITY_ID + AUTHORITY and reused on every
    downstream exchange, so its in-memory token cache persists across
    invocations within the container process. This persistence is what makes
    token caching possible at all - a NEW CCA constructed per call would throw
    its cache away every time.

    Stage 1 (FMI / T1) is performed inside a CALLABLE assertion provider
    (client_credential={"client_assertion": <callable>}) rather than a static
    string. MSAL invokes the callable only when it must send a token request on
    the wire; the in-memory cache transparently avoids unnecessary calls, and
    MSAL refreshes T1 transparently on expiry. A static string client_assertion
    is discouraged by MSAL (it emits a DeprecationWarning) because the JWT has a
    fixed expiry and cannot be refreshed.
    """
    global _agent_app
    if _agent_app is not None:
        return _agent_app
    with _agent_lock:
        if _agent_app is not None:
            return _agent_app

        def _agent_assertion(*args, **kwargs) -> str:
            # MSAL Python may invoke this with zero arguments or with a single
            # context dict (client_id/token_endpoint/fmi_path) depending on the
            # version; *args/**kwargs tolerates both call shapes.
            #
            # Stage 1 (FMI): the persistent Blueprint CCA mints T1 - the
            # impersonation assertion proving the blueprint->agent-identity
            # relationship. T1 is cached by _blueprint_app per fmi_path; this
            # callable is only reached when MSAL needs a fresh assertion.
            blueprint_app, _ = _ensure_blueprint_app()
            t1_result = blueprint_app.acquire_token_for_client(
                scopes=FMI_SCOPE,
                fmi_path=AGENT_IDENTITY_ID,
            )
            if "access_token" not in t1_result:
                raise TokenAcquisitionError(
                    "FMI Stage 1 failed: {} - {}".format(
                        t1_result.get("error"),
                        t1_result.get("error_description"),
                    )
                )
            return t1_result["access_token"]

        _agent_app = msal.ConfidentialClientApplication(
            AGENT_IDENTITY_ID,
            client_credential={"client_assertion": _agent_assertion},
            authority=AUTHORITY,
        )
        logger.info(
            "Agent MSAL app initialised for identity %s",
            AGENT_IDENTITY_ID[:8] + "...",
        )
    return _agent_app

# Module-level token storage - set per-invocation, cleared in finally block.
# NEVER logged or returned to the LLM.
_current_token: str = ""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _extract_prompt(payload: Any) -> str:
    """Normalize common payload formats to plain prompt text."""
    if not isinstance(payload, dict):
        return str(payload) if payload else "Hello"
    for key in ("prompt", "message", "text"):
        val = payload.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    return "Hello"


def _extract_inbound_token(context: Any = None) -> str:
    """
    Extract the inbound Entra user JWT from the `Authorization` header AgentCore
    forwards to the container.

    AgentCore delivers the validated Entra user token in the `Authorization`
    header (the runtime's `RequestHeaderAllowlist` includes `Authorization`).
    The 'Bearer ' prefix is stripped so a raw JWT is ready for MSAL OBO.
    """
    raw = ""
    headers = getattr(context, "request_headers", None) or {}
    for key, val in headers.items():
        if isinstance(val, str) and val.strip() and key.lower() == "authorization":
            raw = val.strip()
            break

    # MSAL acquire_token_on_behalf_of expects the raw JWT, not "Bearer {jwt}"
    if raw.lower().startswith("bearer "):
        raw = raw[7:].strip()
    return raw


def _nonsecret_jwt_claims(token: str) -> dict:
    """
    Decode ONLY non-secret routing claims (iss/aud/appid/azp/scp) from a JWT
    payload, for diagnostics. Returns {} on any parse failure.

    SAFETY: never decodes or returns the signature, never returns the raw token,
    and only surfaces routing/audience claims that are not secrets. This lets one
    invoke prove whether the resolved token is the Entra user JWT
    (iss=login.microsoftonline.com, aud=Blueprint) or an AWS-issued workload
    token, WITHOUT logging anything sensitive.
    """
    try:
        parts = token.split(".")
        if len(parts) < 2:
            return {}
        payload_b64 = parts[1]
        padding = "=" * (-len(payload_b64) % 4)
        decoded = base64.urlsafe_b64decode(payload_b64 + padding)
        claims = json.loads(decoded)
        return {
            "iss": claims.get("iss"),
            "aud": claims.get("aud"),
            "appid": claims.get("appid"),
            "azp": claims.get("azp"),
            "scp": claims.get("scp"),
        }
    except Exception:
        return {}


def _inbound_user_oid(token: str) -> str:
    """
    Decode ONLY the user's object-id (`oid`) claim from a JWT, used to match the
    requesting user to their entry in the agent CCA's token cache. Returns "" on
    any parse failure.

    SAFETY: `oid` is a non-secret GUID identifier (not a credential); it is used
    here purely as a cache-account key and is NEVER logged. Matching on `oid`
    ensures one user can never be served another user's cached token.
    """
    try:
        parts = token.split(".")
        if len(parts) < 2:
            return ""
        payload_b64 = parts[1]
        padding = "=" * (-len(payload_b64) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload_b64 + padding))
        return claims.get("oid") or ""
    except Exception:
        return ""


def _get_downstream_token(inbound_user_token: str, scopes: list) -> str:
    """
    Two-stage Entra Agent ID FMI/OBO token exchange, with token caching.

    Stage 1 (FMI / T1):
      A CALLABLE assertion provider registered on the persistent agent CCA has
      the Blueprint CCA call acquire_token_for_client with fmi_path set to the
      Agent Identity Object ID. Entra issues T1: an impersonation assertion that
      proves the blueprint->agent-identity relationship. T1 is cached by
      _blueprint_app; MSAL invokes the callable only when it needs a fresh
      assertion on the wire and refreshes T1 transparently on expiry.

    Stage 2 (OBO / TR):
      The persistent agent CCA (_ensure_agent_app) performs the jwt-bearer OBO
      grant via acquire_token_on_behalf_of, with T1 as the client_assertion.
      TR is scoped to the target resource and bound to the original user (sub
      claim preserved).

    Caching: because the agent CCA is built ONCE and reused, its in-memory token
    cache survives across invocations. Before the network exchange we try
    acquire_token_silent for the requesting user + scopes; on a cache hit it
    returns the cached TR (token_source=cache) or silently refreshes it from the
    cached refresh token. acquire_token_on_behalf_of itself does NOT read the
    cache (verified against MSAL Python 1.37.x: it always calls the IdP and
    reports token_source=identity_provider), so this explicit silent pre-check
    is what actually serves cached tokens. The OBO call both fetches a fresh TR
    and POPULATES the cache (account + access token + refresh token) for the
    next silent retrieval. The cache is keyed per (user, scopes), so the Echo
    API and MCP downstream tokens are cached independently.

    Returns the resource token (TR) access_token string.
    """
    agent_app = _ensure_agent_app()

    # Silent retrieval first: serve a cached TR (or refresh-token-refreshed TR)
    # for THIS user without a full FMI+OBO network round-trip. Match the cached
    # account by the inbound token's oid so one user can never be handed another
    # user's cached token. If the user cannot be identified, skip silent and go
    # straight to the OBO network exchange (no caching, but never unsafe).
    oid = _inbound_user_oid(inbound_user_token)
    if oid:
        matched = [
            a for a in agent_app.get_accounts()
            if (a.get("home_account_id") or "").split(".")[0] == oid
        ]
        if len(matched) == 1:
            cached = agent_app.acquire_token_silent(scopes, account=matched[0])
            if cached and "access_token" in cached:
                logger.info(
                    "downstream token: token_source=%s (silent) scopes=%s",
                    cached.get("token_source"), scopes,
                )
                return cached["access_token"]

    # Cache miss / unknown user: jwt-bearer OBO network exchange. Stage-1 T1 is
    # supplied lazily by the agent CCA's callable assertion provider. This call
    # also populates the cache (account + AT + RT) for subsequent silent calls.
    tr_result = agent_app.acquire_token_on_behalf_of(
        user_assertion=inbound_user_token,
        scopes=scopes,
    )
    if "access_token" not in tr_result:
        raise TokenAcquisitionError(
            "OBO Stage 2 failed: {} - {}".format(
                tr_result.get("error"), tr_result.get("error_description")
            )
        )
    logger.info(
        "downstream token: token_source=%s (network) scopes=%s",
        tr_result.get("token_source"), scopes,
    )
    return tr_result["access_token"]


def _build_mcp_client(mcp_token: str) -> "MCPClient":
    """
    Construct a Strands MCPClient for the Entra-protected, Microsoft-hosted MCP
    server, authenticating with a per-request OBO token (MCP_SERVER_SCOPE).

    The transport is created LAZILY inside a callable (as MCPClient requires);
    the bearer is captured in the closure so the token is only materialized when
    the client's context manager is entered, never at import time. Streamable
    HTTP is preferred (the transport Microsoft-hosted MCP uses); SSE is a
    fallback. The token value is never logged.
    """
    headers = {"Authorization": "Bearer {}".format(mcp_token)}
    if streamablehttp_client is not None:
        return MCPClient(
            lambda: streamablehttp_client(url=MCP_SERVER_URL, headers=headers)
        )
    if sse_client is not None:
        return MCPClient(lambda: sse_client(url=MCP_SERVER_URL, headers=headers))
    raise RuntimeError("No MCP transport available (streamable_http/sse missing)")


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

@tool
def call_echo_api(prompt: str) -> str:
    """Call the Entra-protected Echo REST API on behalf of the authenticated user.

    MSAL Python performs the two-stage Entra Agent ID FMI/OBO token exchange.
    The LLM receives only the API response text - never the token.
    """
    inbound_token = _current_token
    if not inbound_token:
        logger.warning("call_echo_api: no inbound token available")
        return "Error: no authentication token available"

    try:
        tr = _get_downstream_token(
            inbound_user_token=inbound_token,
            scopes=[ECHO_API_SCOPE],
        )
        _tr_claims = _nonsecret_jwt_claims(tr)
        logger.info("echo TR claims: iss=%s aud=%s scp=%s", _tr_claims.get("iss"), _tr_claims.get("aud"), _tr_claims.get("scp"))
        resp = requests.post(
            "{}/echo".format(ECHO_API_URL),
            json={"message": prompt},
            headers={"Authorization": "Bearer {}".format(tr)},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        return str(data.get("echo", data))
    except TokenAcquisitionError as exc:
        # DEMO: an OBO/FMI failure for the Echo scope must reach the user with
        # its EXACT text, not be reworded by the LLM. Return a model-resistant
        # marker so the system prompt can instruct the LLM to relay it verbatim.
        # The MSAL error/error_description (AADSTS codes) is non-secret; no
        # token or signature is ever included.
        logger.error("call_echo_api token acquisition failed: %s", exc)
        return "\u26a0\ufe0f TOKEN ACQUISITION FAILED (exact error): {}".format(exc)
    except Exception as exc:
        logger.exception("call_echo_api failed: %s", exc)
        return "Error calling Echo API: {}".format(exc)


def _make_mcp_unavailable_tool(error_message: str):
    """
    Build a stand-in Microsoft Graph tool for when the MCP OBO token could NOT
    be acquired. It advertises the same intent to the LLM (Entra/Azure AD/
    directory queries) but, when invoked, returns the verbatim
    "TOKEN ACQUISITION FAILED" marker. This defers the MCP token error to the
    point where the user actually needs MCP: plain chat and Echo-API requests
    proceed normally and never see the MCP failure.

    The captured message is the non-secret MSAL error/error_description (AADSTS
    codes) only - never a token or signature.
    """
    @tool
    def microsoft_graph_query(query: str) -> str:
        """Query Microsoft Entra / Azure AD directory data: users, groups, roles,
        or other directory information."""
        return "\u26a0\ufe0f TOKEN ACQUISITION FAILED (exact error): {}".format(
            error_message
        )

    return microsoft_graph_query


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

@app.entrypoint
async def invoke(payload: Any = None, context: RequestContext = None) -> dict:
    """AgentCore runtime entrypoint."""
    global _current_token

    prompt = _extract_prompt(payload)
    # The validated Entra user token arrives in the `Authorization` header.
    _current_token = _extract_inbound_token(context)

    # Diagnostic only: log which inbound header KEYS arrived, whether a token was
    # resolved, and the NON-SECRET routing claims (iss/aud/appid/azp/scp) of that
    # token. NEVER log header values, the raw token, or the signature. The claims
    # are the definitive test: iss=login.microsoftonline.com + aud=<Blueprint>
    # => the token IS the Entra user JWT and MSAL OBO will work; an AWS-issued
    # iss => it is an AgentCore workload token and OBO must use the native
    # AgentCore Identity exchange instead.
    _inbound_header_keys = list((getattr(context, "request_headers", None) or {}).keys())
    _claims = _nonsecret_jwt_claims(_current_token) if _current_token else {}
    logger.info(
        "inbound context: context_is_none=%s header_keys=%s token_present=%s "
        "token_claims(iss=%s aud=%s appid=%s azp=%s scp=%s)",
        context is None,
        _inbound_header_keys,
        bool(_current_token),
        _claims.get("iss"),
        _claims.get("aud"),
        _claims.get("appid"),
        _claims.get("azp"),
        _claims.get("scp"),
    )

    logger.info("ALL header keys: %s", list((getattr(context, "request_headers", None) or {}).keys()))

    system_prompt_mcp = (
        "You are a helpful assistant. You have access to two types of tools:\n\n"
        "1. Microsoft Graph MCP tools — use these for any question about Microsoft Entra, "
        "Azure AD, users, groups, roles, or directory data.\n"
        "2. call_echo_api — use this tool when the user asks you to call, test, or invoke "
        "the Echo API, or asks about API connectivity.\n\n"
        "For all other questions (general knowledge, explanations, conversational queries) "
        "answer directly without calling any tool.\n\n"
        "Never expose tokens, credentials, or internal error details in your response.\n"
        "EXCEPTION: if a tool returns text that begins with \"\u26a0\ufe0f TOKEN ACQUISITION FAILED\", "
        "relay that text to the user VERBATIM - exactly as written, without rewording, "
        "summarizing, translating, or omitting any part of it."
    )
    system_prompt_echo = (
        "You are a helpful assistant. You have access to one tool:\n\n"
        "1. call_echo_api — use this tool when the user asks you to call, test, or invoke "
        "the Echo API, or asks about API connectivity.\n\n"
        "For all other questions (general knowledge, Entra/directory questions, "
        "conversational queries) answer directly without calling any tool.\n\n"
        "Never expose tokens, credentials, or internal error details in your response.\n"
        "EXCEPTION: if a tool returns text that begins with \"\u26a0\ufe0f TOKEN ACQUISITION FAILED\", "
        "relay that text to the user VERBATIM - exactly as written, without rewording, "
        "summarizing, translating, or omitting any part of it."
    )

    try:
        result = None
        # Captured MCP OBO failure, surfaced LAZILY. An MCP-only token failure
        # must NOT block plain chat or the Echo API: we record the error here and
        # expose a Microsoft Graph stand-in tool (below) that relays it verbatim
        # only if the LLM actually invokes it.
        mcp_token_error: str | None = None

        # MCP is optional. Acquire a per-request OBO token for MCP_SERVER_SCOPE
        # (the SAME FMI/OBO exchange used for the Echo API) and run the agent
        # INSIDE the MCP client's context manager so list_tools_sync() has a
        # live session. The MCP token is acquired per-invoke from the now-working
        # _current_token; it is NEVER built at import time.
        if _MCP_AVAILABLE and MCP_SERVER_URL and MCP_SERVER_SCOPE and _current_token:
            mcp_scopes = [s.strip() for s in MCP_SERVER_SCOPE.split() if s.strip()]
            mcp_token = None
            try:
                mcp_token = _get_downstream_token(
                    inbound_user_token=_current_token,
                    scopes=mcp_scopes,
                )
            except TokenAcquisitionError as exc:
                # Do NOT fail the whole invocation. Defer the error to the actual
                # MCP tool call: capture the non-secret AADSTS message and let the
                # Graph stand-in tool relay it verbatim ONLY when the user needs
                # directory data. Echo-only and no-tool runs proceed normally.
                logger.error(
                    "MCP token acquisition failed (deferred to MCP tool call): %s",
                    exc,
                )
                mcp_token_error = str(exc)

            if mcp_token:
                try:
                    mcp_client = _build_mcp_client(mcp_token)
                    with mcp_client:
                        # MCPClient is NOT itself a tool: discover the actual tool
                        # objects from the live session and pass THOSE to the Agent.
                        mcp_tools = list(mcp_client.list_tools_sync())
                        logger.info(
                            "MCP client connected to %s; %d tool(s) available",
                            MCP_SERVER_URL,
                            len(mcp_tools),
                        )
                        agent = Agent(
                            model=DEFAULT_MODEL_ID,
                            system_prompt=system_prompt_mcp,
                            tools=[*mcp_tools, call_echo_api],
                        )
                        result = agent(prompt)
                except Exception:
                    # NON-token MCP wiring failure (transport/list_tools/TypeError).
                    # Genuinely optional - log the FULL error + traceback and fall
                    # back to Echo-API-only.
                    logger.exception("MCP setup failed; continuing with Echo API only")
                    result = None

        if result is None:
            # No live MCP session. If the MCP token failed, expose the Graph
            # stand-in tool so the failure surfaces ONLY when the user asks for
            # directory data; otherwise run Echo-only.
            if mcp_token_error is not None:
                agent = Agent(
                    model=DEFAULT_MODEL_ID,
                    system_prompt=system_prompt_mcp,
                    tools=[call_echo_api, _make_mcp_unavailable_tool(mcp_token_error)],
                )
            else:
                agent = Agent(
                    model=DEFAULT_MODEL_ID,
                    system_prompt=system_prompt_echo,
                    tools=[call_echo_api],
                )
            result = agent(prompt)

        text = (
            result.message.get("content", [{}])[0].get("text", "")
            if hasattr(result, "message")
            else str(result)
        )
        return {"status": "success", "response": text}
    except Exception as exc:
        logger.exception("invoke failed: %s", exc)
        return {"status": "error", "error": str(exc)}
    finally:
        # Clear token after invocation - never persist in memory between calls
        _current_token = ""


if __name__ == "__main__":
    app.run()

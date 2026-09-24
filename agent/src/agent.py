"""SPA-facing orchestrator: Echo API plus delegated A2A; no direct MCP access."""

import asyncio
import logging
import os
from typing import Any

import httpx
from bedrock_agentcore import BedrockAgentCoreApp, RequestContext
from strands import Agent, tool

from a2a_client import A2AError, call_agent
from identity import AgentIdentity, TokenAcquisitionError

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
app = BedrockAgentCoreApp()
identity = AgentIdentity.from_environment()


def _extract_prompt(payload: Any) -> str:
    if not isinstance(payload, dict):
        return str(payload) if payload else "Hello"
    for key in ("prompt", "message", "text"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return "Hello"


def _extract_inbound_token(context: Any = None) -> str:
    for key, value in (getattr(context, "request_headers", None) or {}).items():
        if key.lower() == "authorization" and isinstance(value, str):
            scheme, _, token = value.partition(" ")
            if scheme.lower() == "bearer":
                return token.strip()
    return ""


def respond(prompt: str, inbound_token: str) -> str:
    # Closures belong to this invocation; no process-global user credential.
    failures: list[Exception] = []

    @tool
    def call_echo_api(prompt: str) -> str:
        """Call the Echo API on behalf of the signed-in user."""
        try:
            token = identity.delegated_token(inbound_token, [os.environ["ECHO_API_SCOPE"]])
            with httpx.Client(timeout=10, follow_redirects=False) as client:
                response = client.post(
                    os.environ["ECHO_API_URL"].rstrip("/") + "/echo",
                    json={"message": prompt},
                    headers={"Authorization": f"Bearer {token}"},
                )
                response.raise_for_status()
                return str(response.json()["echo"])
        except (TokenAcquisitionError, httpx.HTTPError) as exc:
            failures.append(exc)
            return "Echo API request failed; no result is available."

    @tool
    def ask_directory_agent(question: str) -> str:
        """Delegate Microsoft Entra, users, groups, and directory questions to Agent 2."""
        runtime = os.environ.get("A2A_RUNTIME_ARN", "")
        scope = os.environ.get("A2A_SCOPE", "")
        try:
            if not runtime or not scope:
                raise A2AError("Agent 2 is not configured; deploy with -EnableA2A")
            token = identity.delegated_token(inbound_token, [scope])
            return call_agent(runtime, token, question, "directory")
        except (TokenAcquisitionError, A2AError, httpx.HTTPError) as exc:
            failures.append(exc)
            return "Agent 2 request failed; no directory result is available."

    agent = Agent(
        model=os.environ.get("BEDROCK_MODEL_ID", "eu.amazon.nova-micro-v1:0"),
        system_prompt=(
            "You are Agent 1, an orchestrator. Delegate all Microsoft Entra, users, "
            "groups and directory queries to ask_directory_agent. Use call_echo_api "
            "for Echo API or connectivity requests. Answer general questions directly. "
            "Never invent tool results. Report tool failures as failures."
        ),
        tools=[call_echo_api, ask_directory_agent],
    )
    result = str(agent(prompt))
    if failures:
        # Tool failures must not be turned into an apparently successful answer.
        raise failures[0]
    return result


@app.entrypoint
async def invoke(payload: Any = None, context: RequestContext = None) -> dict:
    token = _extract_inbound_token(context)
    if not token:
        logger.warning("Orchestrator request denied: missing bearer token")
        return {"status": "error", "error": "Authentication token required"}
    try:
        response = await asyncio.to_thread(respond, _extract_prompt(payload), token)
        return {"status": "success", "response": response}
    except (TokenAcquisitionError, A2AError) as exc:
        logger.warning("Orchestrator downstream access failed: %s", exc)
        return {"status": "error", "error": str(exc)}
    except httpx.HTTPError:
        logger.warning("Orchestrator downstream HTTP request failed")
        return {"status": "error", "error": "Downstream HTTP request failed"}


if __name__ == "__main__":
    app.run()

"""Small synchronous A2A 0.3 client with a deployment-pinned destination."""

import logging
import uuid

import httpx
from a2a.types import AgentCard, SendMessageResponse
from bedrock_agentcore.runtime import build_runtime_url
from pydantic import ValidationError

from a2a_errors import SAFE_A2A_ERROR_MESSAGES

logger = logging.getLogger(__name__)
FORWARDED_AUTHORIZATION_HEADER = (
    "X-Amzn-Bedrock-AgentCore-Runtime-Custom-Authorization"
)


class A2AError(RuntimeError):
    pass


def call_agent(runtime_arn: str, token: str, prompt: str, skill: str) -> str:
    if not runtime_arn or not token:
        raise A2AError("A2A runtime and access token must be configured")
    url = build_runtime_url(runtime_arn)
    request_id = str(uuid.uuid4())
    headers = {
        "Authorization": f"Bearer {token}",
        FORWARDED_AUTHORIZATION_HEADER: f"Bearer {token}",
        "X-Amzn-Bedrock-AgentCore-Runtime-Session-Id": str(uuid.uuid4()),
    }
    with httpx.Client(
        headers=headers, timeout=httpx.Timeout(180, connect=10), follow_redirects=False
    ) as client:
        card_response = client.get(
            url + "/.well-known/agent-card.json"
        )
        _check_response(card_response)
        try:
            card = AgentCard.model_validate(card_response.json())
        except (ValueError, ValidationError):
            raise A2AError("Endpoint returned an invalid Agent Card") from None
        if card.protocol_version != "0.3.0" or skill not in {s.id for s in card.skills}:
            raise A2AError("Agent Card does not advertise the requested A2A capability")
        # Never send credentials to a URL supplied by discovery or model output.
        response = client.post(
            url,
            json={
                "jsonrpc": "2.0",
                "id": request_id,
                "method": "message/send",
                "params": {
                    "message": {
                        "kind": "message",
                        "role": "user",
                        "messageId": str(uuid.uuid4()),
                        "parts": [{"kind": "text", "text": prompt}],
                        "metadata": {"skill": skill},
                    },
                    "configuration": {"blocking": True},
                },
            },
        )
        _check_response(response)
        try:
            reply = SendMessageResponse.model_validate(response.json()).root
        except (ValueError, ValidationError):
            raise A2AError("Endpoint returned an invalid A2A response") from None
        if reply.id != request_id:
            raise A2AError("A2A response ID does not match the request")
        if hasattr(reply, "error"):
            if reply.error.message in SAFE_A2A_ERROR_MESSAGES:
                raise A2AError(reply.error.message)
            raise A2AError(f"A2A request failed (JSON-RPC code {reply.error.code})")
        result = reply.result
        if result.kind == "message":
            parts = result.parts
        else:
            if result.status.state.value != "completed":
                raise A2AError(f"A2A task did not complete: {result.status.state.value}")
            parts = [part for artifact in result.artifacts or [] for part in artifact.parts]
        text = "\n".join(part.root.text for part in parts if part.root.kind == "text")
        if not text:
            raise A2AError("A2A response contained no text")
        logger.info("A2A completed request_id=%s skill=%s", request_id, skill)
        return text


def _check_response(response: httpx.Response) -> None:
    if not response.is_success:
        # Do not expose arbitrary response bodies or authenticated request headers.
        raise A2AError(f"A2A endpoint returned HTTP {response.status_code}")

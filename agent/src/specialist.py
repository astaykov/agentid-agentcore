"""Agent 2: native A2A on AgentCore with distinct delegated and app-only policies."""

import asyncio
import logging
import os

import jwt
import uvicorn
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.types import (
    AgentCapabilities, AgentCard, AgentSkill, HTTPAuthSecurityScheme,
    InternalError, InvalidParamsError, SecurityScheme, UnsupportedOperationError,
)
from a2a.utils import new_agent_text_message
from a2a.utils.errors import ServerError
from bedrock_agentcore.runtime import BedrockCallContextBuilder, build_a2a_app
from bedrock_agentcore.runtime.models import PingStatus
from mcp.client.streamable_http import streamablehttp_client
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse
from strands import Agent
from strands.tools.mcp import MCPClient

from a2a_errors import (
    MCP_AUTHENTICATION_FAILED,
    MCP_CONSENT_REQUIRED,
    SKILL_NOT_AUTHORIZED,
)
from authorization import Authorizer, AuthorizationError, Principal, authorize_skill
from identity import AgentIdentity, TokenAcquisitionError

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
FORWARDED_AUTHORIZATION_HEADER = (
    "x-amzn-bedrock-agentcore-runtime-custom-authorization"
)


class AuthenticationMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, authorizer: Authorizer):
        super().__init__(app)
        self.authorizer = authorizer

    async def dispatch(self, request, call_next):
        if request.url.path in {"/ping", "/.well-known/agent-card.json"}:
            return await call_next(request)
        authorization = request.headers.get(
            FORWARDED_AUTHORIZATION_HEADER, request.headers.get("authorization", "")
        )
        authorization_source = (
            FORWARDED_AUTHORIZATION_HEADER
            if FORWARDED_AUTHORIZATION_HEADER in request.headers
            else "authorization"
        )
        scheme, separator, token = authorization.partition(" ")
        try:
            principal = await asyncio.to_thread(
                self.authorizer.authenticate, authorization
            )
        except jwt.PyJWKClientConnectionError:
            logger.warning("Entra signing keys unavailable")
            return JSONResponse({"error": "Identity provider unavailable"}, status_code=503)
        except (jwt.InvalidTokenError, jwt.PyJWKClientError) as exc:
            logger.warning(
                "A2A authentication denied path=%s authorization_source=%s "
                "authorization_present=%s "
                "bearer_scheme=%s token_present=%s validation_error=%s",
                request.url.path,
                authorization_source,
                bool(authorization),
                scheme.lower() == "bearer",
                bool(separator and token.strip()),
                type(exc).__name__,
            )
            return JSONResponse(
                {"error": "Invalid or missing access token"}, status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
            )
        except AuthorizationError as exc:
            logger.warning(
                "A2A authorization denied path=%s reason=%s",
                request.url.path,
                exc,
            )
            try:
                payload = await request.json()
                request_id = payload.get("id") if isinstance(payload, dict) else None
            except ValueError:
                request_id = None
            return JSONResponse({
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": -32001, "message": exc.user_message},
            })
        request.state.principal = principal
        return await call_next(request)


class AuthenticatedContextBuilder(BedrockCallContextBuilder):
    def build(self, request):
        context = super().build(request)
        context.state["principal"] = request.state.principal
        return context


def respond(identity: AgentIdentity, principal: Principal, prompt: str, skill: str) -> str:
    authorize_skill(principal, skill)
    model = os.environ.get("BEDROCK_MODEL_ID", "eu.amazon.nova-micro-v1:0")
    if skill == "chat":
        return str(Agent(
            model=model,
            system_prompt="You are Agent 2. Answer directly. You have no external tools.",
            tools=[],
        )(prompt))
    scopes = os.environ["MCP_SERVER_SCOPE"].split()
    token = identity.delegated_token(principal.token, scopes)
    with MCPClient(lambda: streamablehttp_client(
        url=os.environ["MCP_SERVER_URL"], headers={"Authorization": f"Bearer {token}"}
    )) as mcp:
        agent = Agent(
            model=model,
            system_prompt=(
                "You are Agent 2, a Microsoft directory specialist. Use the provided "
                "MCP tools for directory data. Never invent results or hide tool failures."
            ),
            tools=list(mcp.list_tools_sync()),
        )
        return str(agent(prompt))


class SpecialistExecutor(AgentExecutor):
    def __init__(self, identity: AgentIdentity):
        self.identity = identity
        self.active = 0

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        principal = context.call_context.state["principal"]
        message = context.message
        if message is None or any(part.root.kind != "text" for part in message.parts):
            raise ServerError(InvalidParamsError(message="Only text messages are supported"))
        skill = (message.metadata or {}).get("skill", "chat")
        prompt = context.get_user_input()
        if not prompt.strip() or len(prompt) > 16000:
            raise ServerError(InvalidParamsError(message="Text must contain 1-16000 characters"))
        try:
            authorize_skill(principal, skill)
        except AuthorizationError:
            logger.warning("A2A skill denied mode=%s skill=%s", principal.mode, skill)
            raise ServerError(InvalidParamsError(message=SKILL_NOT_AUTHORIZED)) from None
        request_id = context.call_context.state["request_id"]
        logger.info(
            "A2A authorized request_id=%s mode=%s caller=%s user_or_agent=%s skill=%s",
            request_id, principal.mode, principal.caller_id, principal.object_id, skill,
        )
        self.active += 1
        try:
            text = await asyncio.to_thread(respond, self.identity, principal, prompt, skill)
            await event_queue.enqueue_event(new_agent_text_message(text))
        except TokenAcquisitionError as exc:
            logger.warning("A2A downstream authentication failed request_id=%s: %s", request_id, exc)
            message = (
                MCP_CONSENT_REQUIRED
                if "AADSTS65001" in str(exc)
                else MCP_AUTHENTICATION_FAILED
            )
            raise ServerError(InternalError(message=message)) from None
        finally:
            self.active -= 1

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        raise ServerError(UnsupportedOperationError())


def create_app():
    identity = AgentIdentity.from_environment()
    authorizer = Authorizer(
        identity.tenant_id, [identity.blueprint_id, f"api://{identity.blueprint_id}"],
        os.environ["AGENT1_IDENTITY_ID"], os.environ["AGENT3_IDENTITY_ID"],
    )
    executor = SpecialistExecutor(identity)
    card = AgentCard(
        name="Entra A2A specialist",
        description="User-delegated directory assistance and tool-free agent chat.",
        url="http://localhost:9000/",
        version="1.0.0",
        protocol_version="0.3.0",
        capabilities=AgentCapabilities(streaming=False),
        default_input_modes=["text"],
        default_output_modes=["text"],
        security_schemes={
            "entra": SecurityScheme(root=HTTPAuthSecurityScheme(
                scheme="bearer", bearer_format="JWT",
                description=(
                    "Entra token for Blueprint 2. Delegated: user_impersonation plus "
                    "Agent2.Tools.User, caller Agent 1. App-only: "
                    "Agent2.Chat.Application, caller Agent 3."
                ),
            ))
        },
        security=[{"entra": []}],
        skills=[
            AgentSkill(id="directory", name="Directory assistance",
                       description="Delegated user access to Microsoft MCP tools.",
                       tags=["directory", "delegated"]),
            AgentSkill(id="chat", name="Tool-free chat",
                       description="Plain AI response without external tools.",
                       tags=["chat"]),
        ],
    )
    app = build_a2a_app(
        executor, card, context_builder=AuthenticatedContextBuilder(),
        ping_handler=lambda: PingStatus.HEALTHY_BUSY if executor.active else PingStatus.HEALTHY,
    )
    app.add_middleware(AuthenticationMiddleware, authorizer=authorizer)
    return app


if __name__ == "__main__":
    uvicorn.run(create_app(), host="0.0.0.0", port=9000)

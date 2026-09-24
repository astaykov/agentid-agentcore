"""Agent 3: scheduled, app-only A2A caller with no MCP credentials or tools."""

import logging
import os

from strands import Agent

from a2a_client import call_agent
from identity import AgentIdentity

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)
identity = AgentIdentity.from_environment()


def handler(event, context):
    prompt = event.get("prompt", "Suggest one principle for reliable AI agent collaboration.")
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError("A nonempty prompt is required")
    planner = Agent(
        model=os.environ.get("BEDROCK_MODEL_ID", "eu.amazon.nova-micro-v1:0"),
        system_prompt=(
            "You are Agent 3, an autonomous agent. Turn the supplied task into one "
            "concise question for another AI agent. Do not request tools, directory "
            "access, or personal data. Output only the question."
        ),
        tools=[],
    )
    question = str(planner(prompt))
    token = identity.application_token(os.environ["A2A_SCOPE"])
    response = call_agent(os.environ["A2A_RUNTIME_ARN"], token, question, "chat")
    logger.info(
        "Autonomous A2A completed aws_request_id=%s agent_id=%s response=%s",
        context.aws_request_id, identity.agent_id, response,
    )
    return {"status": "success", "response": response}

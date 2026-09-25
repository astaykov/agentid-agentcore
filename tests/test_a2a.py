"""Offline contract tests: python -m unittest discover -s tests -v."""

import asyncio
import json
import os
from pathlib import Path
import sys
import time
import unittest
from unittest.mock import MagicMock, patch

import httpx
import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from starlette.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "agent" / "src"))
ENV = {
    "ENTRA_TENANT_ID": "00000000-0000-0000-0000-000000000001",
    "BLUEPRINT_CLIENT_ID": "00000000-0000-0000-0000-000000000002",
    "AGENT_IDENTITY_ID": "00000000-0000-0000-0000-000000000003",
    "AGENT1_IDENTITY_ID": "00000000-0000-0000-0000-000000000004",
    "AGENT3_IDENTITY_ID": "00000000-0000-0000-0000-000000000005",
}
with patch.dict(os.environ, ENV):
    import agent
    import autonomous
    import specialist
from a2a_client import A2AError, FORWARDED_AUTHORIZATION_HEADER, call_agent
from a2a_errors import MCP_CONSENT_REQUIRED, USER_ROLE_REQUIRED
from authorization import Authorizer, AuthorizationError, Principal, authorize_skill
from identity import AgentIdentity, TokenAcquisitionError

PRIVATE_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
RUNTIME = "arn:aws:bedrock-agentcore:eu-central-1:123456789012:runtime/specialist-abc"


def claims(application=False):
    result = {
        "iss": f"https://login.microsoftonline.com/{ENV['ENTRA_TENANT_ID']}/v2.0",
        "aud": ENV["BLUEPRINT_CLIENT_ID"],
        "tid": ENV["ENTRA_TENANT_ID"],
        "oid": ENV["AGENT3_IDENTITY_ID"] if application else "human-user-id",
        "azp": ENV["AGENT3_IDENTITY_ID"] if application else ENV["AGENT1_IDENTITY_ID"],
        "roles": ["Agent2.Chat.Application" if application else "Agent2.Tools.User"],
        "iat": int(time.time()),
        "exp": int(time.time()) + 600,
    }
    if application:
        result["idtyp"] = "app"
    else:
        result["scp"] = "user_impersonation"
    return result


def signed_token(application=False, **overrides):
    values = claims(application)
    values.update(overrides)
    return jwt.encode(values, PRIVATE_KEY, algorithm="RS256", headers={"kid": "test"})


def rpc(skill="chat", text="hello", request_id="request-1"):
    return {
        "jsonrpc": "2.0", "id": request_id, "method": "message/send",
        "params": {"message": {
            "role": "user", "messageId": "message-1",
            "parts": [{"kind": "text", "text": text}],
            "metadata": {"skill": skill},
        }},
    }


class AuthorizationTests(unittest.TestCase):
    def setUp(self):
        self.authorizer = Authorizer(
            ENV["ENTRA_TENANT_ID"], ENV["BLUEPRINT_CLIENT_ID"],
            ENV["AGENT1_IDENTITY_ID"], ENV["AGENT3_IDENTITY_ID"],
        )
        self.authorizer.keys = MagicMock()
        self.authorizer.keys.get_signing_key_from_jwt.return_value.key = PRIVATE_KEY.public_key()

    def test_both_token_types(self):
        for application, mode in [(False, "delegated"), (True, "application")]:
            with self.subTest(mode=mode):
                result = self.authorizer.authenticate("Bearer " + signed_token(application))
                self.assertEqual(result.mode, mode)
                self.assertNotIn(result.token, repr(result))

    def test_invalid_jwts(self):
        variants = [
            {"aud": "different-resource"}, {"iss": "https://attacker.invalid"},
            {"exp": int(time.time()) - 60}, {"nbf": int(time.time()) + 600},
        ]
        for override in variants:
            with self.subTest(override=override), self.assertRaises(jwt.InvalidTokenError):
                self.authorizer.authenticate("Bearer " + signed_token(**override))
        other_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        forged = jwt.encode(claims(), other_key, algorithm="RS256")
        with self.assertRaises(jwt.InvalidSignatureError):
            self.authorizer.authenticate("Bearer " + forged)

    def test_accepts_blueprint_api_uri_audience(self):
        self.authorizer = Authorizer(
            ENV["ENTRA_TENANT_ID"],
            [ENV["BLUEPRINT_CLIENT_ID"], f"api://{ENV['BLUEPRINT_CLIENT_ID']}"],
            ENV["AGENT1_IDENTITY_ID"],
            ENV["AGENT3_IDENTITY_ID"],
        )
        self.authorizer.keys = MagicMock()
        self.authorizer.keys.get_signing_key_from_jwt.return_value.key = PRIVATE_KEY.public_key()
        result = self.authorizer.authenticate(
            "Bearer " + signed_token(aud=f"api://{ENV['BLUEPRINT_CLIENT_ID']}")
        )
        self.assertEqual(result.mode, "delegated")

    def test_delegated_requires_scope_role_caller_and_tenant(self):
        for override in [
            {"roles": []}, {"roles": "Agent2.Tools.User"}, {"scp": "other"},
            {"scp": ["user_impersonation"]}, {"azp": "other-agent"},
            {"tid": "other-tenant"}, {"idtyp": "app"}, {"oid": ""},
        ]:
            with self.subTest(override=override), self.assertRaises(AuthorizationError):
                self.authorizer.authenticate("Bearer " + signed_token(**override))

    def test_app_only_requires_application_role_and_agent3(self):
        for override in [
            {"roles": []}, {"roles": ["Agent2.Tools.User"]},
            {"azp": ENV["AGENT1_IDENTITY_ID"]}, {"oid": "other-agent"},
            {"scp": "user_impersonation"},
        ]:
            with self.subTest(override=override), self.assertRaises(AuthorizationError):
                self.authorizer.authenticate("Bearer " + signed_token(True, **override))

    def test_missing_authentication(self):
        for value in ["", "Basic abc", "Bearer", "Bearer "]:
            with self.subTest(value=value), self.assertRaises(jwt.InvalidTokenError):
                self.authorizer.authenticate(value)

    def test_skill_policy(self):
        principal = self.authorizer.authenticate("Bearer " + signed_token(True))
        authorize_skill(principal, "chat")
        for skill in ["directory", "unknown", ["chat"]]:
            with self.subTest(skill=skill), self.assertRaises(AuthorizationError):
                authorize_skill(principal, skill)


class ProtocolTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, ENV)
        self.env.start()
        self.keys = patch(
            "jwt.PyJWKClient.get_signing_key_from_jwt",
            return_value=MagicMock(key=PRIVATE_KEY.public_key()),
        )
        self.keys.start()
        self.client = TestClient(specialist.create_app())
        self.addCleanup(self.env.stop)
        self.addCleanup(self.keys.stop)
        self.addCleanup(self.client.close)

    def test_health_and_agent_card_discovery(self):
        self.assertEqual(self.client.get("/ping").json(), {"status": "Healthy"})
        result = self.client.get("/.well-known/agent-card.json")
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json()["protocolVersion"], "0.3.0")
        self.assertEqual({s["id"] for s in result.json()["skills"]}, {"chat", "directory"})
        self.assertEqual(result.json()["security"], [{"entra": []}])

    def test_discovery_accepts_forwarded_authorization_header(self):
        result = self.client.get(
            "/.well-known/agent-card.json",
            headers={
                "X-Amzn-Bedrock-AgentCore-Runtime-Custom-Authorization":
                    "Bearer " + signed_token(True)
            },
        )
        self.assertEqual(result.status_code, 200)

    def test_missing_invocation_authentication_logs_no_token_metadata(self):
        with self.assertLogs("specialist", "WARNING") as captured:
            result = self.client.post("/", json=rpc())
        self.assertEqual(result.status_code, 401)
        self.assertEqual(len(captured.output), 1)
        self.assertIn("authorization_source=authorization", captured.output[0])
        self.assertIn("authorization_present=False", captured.output[0])
        self.assertIn("bearer_scheme=False", captured.output[0])
        self.assertIn("token_present=False", captured.output[0])
        self.assertIn("validation_error=InvalidTokenError", captured.output[0])

    def test_missing_role_returns_safe_actionable_error(self):
        with patch("specialist.respond") as model:
            result = self.client.post("/", json=rpc(), headers={
                "Authorization": "Bearer " + signed_token(roles=[]),
            })
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json()["error"]["message"], USER_ROLE_REQUIRED)
        model.assert_not_called()

    def test_mcp_consent_failure_returns_safe_actionable_error(self):
        with patch(
            "specialist.respond",
            side_effect=TokenAcquisitionError(
                "OBO failed: invalid_grant AADSTS65001 (Entra correlation ID: test)"
            ),
        ):
            result = self.client.post("/", json=rpc("directory"), headers={
                "Authorization": "Bearer " + signed_token(),
            })
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json()["error"]["message"], MCP_CONSENT_REQUIRED)

    def test_client_surfaces_only_allowlisted_remote_errors(self):
        original_client = httpx.Client

        def transport(request):
            if request.method == "GET":
                result = self.client.get("/.well-known/agent-card.json")
                return httpx.Response(result.status_code, json=result.json())
            payload = json.loads(request.content)
            return httpx.Response(200, json={
                "jsonrpc": "2.0",
                "id": payload["id"],
                "error": {"code": -32603, "message": MCP_CONSENT_REQUIRED},
            })

        with patch("a2a_client.httpx.Client", side_effect=lambda **kwargs: original_client(
            transport=httpx.MockTransport(transport), **kwargs
        )):
            with self.assertRaisesRegex(A2AError, MCP_CONSENT_REQUIRED):
                call_agent(RUNTIME, "token", "question", "directory")

        def unknown_error(request):
            if request.method == "GET":
                result = self.client.get("/.well-known/agent-card.json")
                return httpx.Response(result.status_code, json=result.json())
            payload = json.loads(request.content)
            return httpx.Response(200, json={
                "jsonrpc": "2.0",
                "id": payload["id"],
                "error": {"code": -32603, "message": "sensitive internal detail"},
            })

        with patch("a2a_client.httpx.Client", side_effect=lambda **kwargs: original_client(
            transport=httpx.MockTransport(unknown_error), **kwargs
        )):
            with self.assertRaisesRegex(A2AError, r"JSON-RPC code -32603"):
                call_agent(RUNTIME, "token", "question", "directory")

    def test_application_cannot_request_mcp(self):
        with patch("specialist.respond") as model:
            result = self.client.post("/", json=rpc("directory"), headers={
                "Authorization": "Bearer " + signed_token(True),
            })
        self.assertIn("error", result.json())
        model.assert_not_called()

    def test_application_response_uses_no_tools_or_tokens(self):
        with patch("specialist.Agent") as model, patch("specialist.MCPClient") as mcp:
            model.return_value.return_value = "plain response"
            with patch.object(AgentIdentity, "delegated_token") as obo:
                result = self.client.post("/", json=rpc(), headers={
                    "Authorization": "Bearer " + signed_token(True),
                })
        self.assertEqual(result.json()["result"]["parts"][0]["text"], "plain response")
        self.assertEqual(model.call_args.kwargs["tools"], [])
        mcp.assert_not_called()
        obo.assert_not_called()

    def test_delegated_mcp_uses_received_assertion(self):
        principal = Principal("delegated", ENV["ENTRA_TENANT_ID"], "user", "agent1", "user-token")
        identity = MagicMock()
        identity.delegated_token.return_value = "mcp-token"
        with patch.dict(os.environ, {"MCP_SERVER_URL": "https://mcp.invalid", "MCP_SERVER_SCOPE": "scope1 scope2"}):
            with patch("specialist.MCPClient") as mcp, patch("specialist.Agent") as model:
                mcp.return_value.__enter__.return_value.list_tools_sync.return_value = ["directory-tool"]
                model.return_value.return_value = "directory response"
                self.assertEqual(specialist.respond(identity, principal, "query", "directory"), "directory response")
        identity.delegated_token.assert_called_once_with("user-token", ["scope1", "scope2"])
        self.assertEqual(model.call_args.kwargs["tools"], ["directory-tool"])

    def test_client_server_round_trip(self):
        original_client = httpx.Client
        requests = []

        def forward(request):
            requests.append(request)
            path = "/.well-known/agent-card.json" if request.method == "GET" else "/"
            result = self.client.request(
                request.method, path, headers=dict(request.headers), content=request.content,
            )
            return httpx.Response(result.status_code, json=result.json())

        with patch("specialist.respond", return_value="A2A works"):
            with patch("a2a_client.httpx.Client", side_effect=lambda **kwargs: original_client(
                transport=httpx.MockTransport(forward), **kwargs
            )):
                access_token = signed_token(True)
                result = call_agent(RUNTIME, access_token, "question", "chat")
        self.assertEqual(result, "A2A works")
        self.assertEqual([request.url.query for request in requests], [b"", b""])
        self.assertTrue(all(
            request.headers[FORWARDED_AUTHORIZATION_HEADER] == "Bearer " + access_token
            for request in requests
        ))

    def test_request_credentials_do_not_mix(self):
        async def exercise():
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=specialist.create_app()), base_url="http://test",
            ) as client:
                responses = await asyncio.gather(*[
                    client.post("/", json=rpc(), headers={
                        "Authorization": "Bearer " + signed_token(oid=f"user-{n}")
                    }) for n in range(4)
                ])
            return [response.json()["result"]["parts"][0]["text"] for response in responses]

        def answer(identity, principal, prompt, skill):
            time.sleep(0.02)
            return principal.object_id

        with patch("specialist.respond", side_effect=answer):
            self.assertEqual(asyncio.run(exercise()), [f"user-{n}" for n in range(4)])


class IdentityTests(unittest.TestCase):
    def test_distinct_grants_share_fmi_not_user_tokens(self):
        with patch("identity.msal.ConfidentialClientApplication") as cca:
            blueprint, child = MagicMock(), MagicMock()
            cca.side_effect = [blueprint, child]
            blueprint.acquire_token_for_client.return_value = {"access_token": "t1"}
            child.acquire_token_on_behalf_of.return_value = {"access_token": "delegated"}
            child.acquire_token_for_client.return_value = {"access_token": "application"}
            identity = AgentIdentity("tenant", "blueprint", "child")
            self.assertEqual(identity.delegated_token("user1", ["scope"]), "delegated")
            self.assertEqual(identity.delegated_token("user2", ["scope"]), "delegated")
            self.assertEqual(identity.application_token("api://resource/.default"), "application")
            provider = cca.call_args.kwargs["client_credential"]["client_assertion"]
            self.assertEqual(provider(), "t1")
            blueprint.acquire_token_for_client.assert_called_once_with(
                scopes=["api://AzureADTokenExchange/.default"], fmi_path="child",
            )
            self.assertEqual(
                [call.kwargs["user_assertion"] for call in child.acquire_token_on_behalf_of.call_args_list],
                ["user1", "user2"],
            )
            child.acquire_token_for_client.assert_called_once_with(scopes=["api://resource/.default"])
            self.assertEqual(cca.call_count, 2)

    def test_token_failure_does_not_dump_response(self):
        with self.assertRaises(TokenAcquisitionError) as failure:
            AgentIdentity._token({"error": "invalid_grant", "refresh_token": "secret"}, "OBO")
        self.assertNotIn("secret", str(failure.exception))

    def test_autonomous_agent_calls_a2a_with_app_token(self):
        with patch.dict(os.environ, {"A2A_RUNTIME_ARN": RUNTIME, "A2A_SCOPE": "api://bp2/.default"}):
            with patch("autonomous.Agent") as model, patch("autonomous.identity") as identity:
                with patch("autonomous.call_agent", return_value="answer") as remote:
                    model.return_value.return_value = "generated question"
                    identity.application_token.return_value = "app-token"
                    result = autonomous.handler({"prompt": "task"}, MagicMock(aws_request_id="run"))
        remote.assert_called_once_with(RUNTIME, "app-token", "generated question", "chat")
        identity.delegated_token.assert_not_called()
        self.assertEqual(result["response"], "answer")


if __name__ == "__main__":
    unittest.main()

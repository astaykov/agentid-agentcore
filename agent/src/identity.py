"""AWS-federated Entra Agent Identity token acquisition shared by all agents."""

import logging
import os
import threading

import boto3
import jwt
import msal
import requests
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

logger = logging.getLogger(__name__)
EXCHANGE_SCOPE = ["api://AzureADTokenExchange/.default"]


class TokenAcquisitionError(RuntimeError):
    pass


class AgentIdentity:
    def __init__(self, tenant_id: str, blueprint_id: str, agent_id: str):
        self.tenant_id = tenant_id
        self.blueprint_id = blueprint_id
        self.agent_id = agent_id
        self._lock = threading.Lock()
        self._blueprint = None
        self._agent = None

    @classmethod
    def from_environment(cls):
        return cls(
            os.environ["ENTRA_TENANT_ID"],
            os.environ["BLUEPRINT_CLIENT_ID"],
            os.environ["AGENT_IDENTITY_ID"],
        )

    def _aws_assertion(self, *args, **kwargs):
        sts = boto3.client(
            "sts",
            region_name=os.environ.get("AWS_REGION") or os.environ["AWS_DEFAULT_REGION"],
            config=Config(connect_timeout=5, read_timeout=10, retries={"max_attempts": 3}),
        )
        assertion = sts.get_web_identity_token(
            Audience=["api://AzureADTokenExchange"],
            SigningAlgorithm="RS256",
            DurationSeconds=300,
        )["WebIdentityToken"]
        if os.environ.get("AUTH_DIAGNOSTICS_ENABLED", "").lower() in {"true", "1", "yes"}:
            claims = jwt.decode(assertion, options={"verify_signature": False})
            logger.info(
                "AWS federation metadata iss=%s sub=%s aud=%s iat=%s exp=%s",
                *(claims.get(name) for name in ("iss", "sub", "aud", "iat", "exp")),
            )
        return assertion

    @staticmethod
    def _token(result: dict, stage: str) -> str:
        if "access_token" not in result:
            # Do not serialize the result: it can contain credentials or user data.
            code = result.get("error", "unknown_error")
            correlation = result.get("correlation_id", "unavailable")
            aadsts = " ".join(
                f"AADSTS{value}" for value in result.get("error_codes", [])
                if isinstance(value, int)
            )
            raise TokenAcquisitionError(
                f"{stage} failed: {code} {aadsts} (Entra correlation ID: {correlation})"
            )
        return result["access_token"]

    def _fmi_assertion(self, *args, **kwargs):
        return self._token(
            self._blueprint.acquire_token_for_client(
                scopes=EXCHANGE_SCOPE, fmi_path=self.agent_id
            ),
            "FMI",
        )

    def _client(self):
        with self._lock:
            if self._agent is None:
                authority = f"https://login.microsoftonline.com/{self.tenant_id}"
                self._blueprint = msal.ConfidentialClientApplication(
                    self.blueprint_id,
                    authority=authority,
                    client_credential={"client_assertion": self._aws_assertion},
                )
                self._agent = msal.ConfidentialClientApplication(
                    self.agent_id,
                    authority=authority,
                    client_credential={"client_assertion": self._fmi_assertion},
                )
            return self._agent

    def delegated_token(self, user_assertion: str, scopes: list[str]) -> str:
        if not user_assertion or not scopes:
            raise ValueError("Delegated access requires an inbound token and scopes")
        # Exchange this request's assertion, rather than reusing a user cache entry
        # independently of the authorization context of the incoming request.
        try:
            result = self._client().acquire_token_on_behalf_of(
                user_assertion=user_assertion, scopes=scopes
            )
        except (BotoCoreError, ClientError, requests.RequestException) as exc:
            raise TokenAcquisitionError(
                f"OBO token exchange unavailable ({type(exc).__name__})"
            ) from None
        return self._token(result, "OBO")

    def application_token(self, scope: str) -> str:
        if not scope.endswith("/.default"):
            raise ValueError("Application access requires the resource /.default scope")
        try:
            result = self._client().acquire_token_for_client(scopes=[scope])
        except (BotoCoreError, ClientError, requests.RequestException) as exc:
            raise TokenAcquisitionError(
                f"Application token exchange unavailable ({type(exc).__name__})"
            ) from None
        return self._token(result, "Client credentials")

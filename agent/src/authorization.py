"""Validate Entra access tokens and authorize Agent 2 capabilities."""

from dataclasses import dataclass, field

import jwt

from a2a_errors import (
    APPLICATION_CALLER_REJECTED,
    APPLICATION_ROLE_REQUIRED,
    DELEGATED_CALLER_REJECTED,
    DELEGATED_SCOPE_REQUIRED,
    TOKEN_TYPE_REJECTED,
    USER_ROLE_REQUIRED,
)


class AuthorizationError(RuntimeError):
    def __init__(self, message: str, user_message: str = "Agent 2 denied this request."):
        super().__init__(message)
        self.user_message = user_message


@dataclass(frozen=True)
class Principal:
    mode: str
    tenant_id: str
    object_id: str
    caller_id: str
    token: str = field(repr=False)


class Authorizer:
    def __init__(
        self, tenant_id: str, audience: str | list[str], agent1_id: str, agent3_id: str
    ):
        self.tenant_id = tenant_id
        self.audience = [audience] if isinstance(audience, str) else audience
        self.agent1_id = agent1_id
        self.agent3_id = agent3_id
        self.issuer = f"https://login.microsoftonline.com/{tenant_id}/v2.0"
        self.keys = jwt.PyJWKClient(
            f"https://login.microsoftonline.com/{tenant_id}/discovery/v2.0/keys",
            timeout=10,
        )

    def authenticate(self, authorization: str) -> Principal:
        scheme, separator, token = authorization.partition(" ")
        if scheme.lower() != "bearer" or not separator or not token.strip():
            raise jwt.InvalidTokenError("Bearer token required")
        token = token.strip()
        key = self.keys.get_signing_key_from_jwt(token).key
        claims = jwt.decode(
            token,
            key,
            algorithms=["RS256"],
            audience=self.audience,
            issuer=self.issuer,
            options={"require": ["exp", "iat", "iss", "aud", "tid", "oid"]},
        )
        return self.authorize_claims(claims, token)

    def authorize_claims(self, claims: dict, token: str) -> Principal:
        """Only call with cryptographically validated claims."""
        if (
            claims.get("tid") != self.tenant_id
            or not isinstance(claims.get("oid"), str)
            or not claims["oid"]
        ):
            raise AuthorizationError("Tenant is not authorized")
        roles = claims.get("roles", [])
        if not isinstance(roles, list) or any(not isinstance(role, str) for role in roles):
            raise AuthorizationError("Invalid roles claim")
        caller = claims.get("azp")
        scope = claims.get("scp")
        if scope is not None:
            if not isinstance(scope, str):
                raise AuthorizationError("Delegated scope claim is invalid")
            if "user_impersonation" not in scope.split():
                raise AuthorizationError(
                    "Delegated user_impersonation scope is required",
                    DELEGATED_SCOPE_REQUIRED,
                )
            if "Agent2.Tools.User" not in roles:
                raise AuthorizationError(
                    "Delegated Agent2.Tools.User role is required",
                    USER_ROLE_REQUIRED,
                )
            if caller != self.agent1_id:
                raise AuthorizationError(
                    "Delegated caller is not Agent Identity 1",
                    DELEGATED_CALLER_REJECTED,
                )
            if claims.get("idtyp") == "app":
                raise AuthorizationError(
                    "Delegated token cannot use the app token type",
                    TOKEN_TYPE_REJECTED,
                )
            mode = "delegated"
        else:
            if "Agent2.Chat.Application" not in roles:
                raise AuthorizationError(
                    "Application Agent2.Chat.Application role is required",
                    APPLICATION_ROLE_REQUIRED,
                )
            if caller != self.agent3_id:
                raise AuthorizationError(
                    "Application caller is not Agent Identity 3",
                    APPLICATION_CALLER_REJECTED,
                )
            if claims.get("oid") != self.agent3_id:
                raise AuthorizationError(
                    "Application subject is not Agent Identity 3",
                    APPLICATION_CALLER_REJECTED,
                )
            mode = "application"
        return Principal(mode, self.tenant_id, claims["oid"], caller, token)


def authorize_skill(principal: Principal, skill: str) -> None:
    if not isinstance(skill, str) or skill not in {"chat", "directory"}:
        raise AuthorizationError("Unknown skill")
    if skill == "directory" and principal.mode != "delegated":
        raise AuthorizationError("MCP access requires user delegation")

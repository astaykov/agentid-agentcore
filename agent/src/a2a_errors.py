"""Safe A2A error messages that may be shown to end users."""

USER_ROLE_REQUIRED = (
    "Your account is not assigned the Agent2.Tools.User role on Blueprint 2. "
    "Ask an administrator to assign the role, then sign in again."
)
DELEGATED_SCOPE_REQUIRED = (
    "Agent Identity 1 lacks delegated user_impersonation consent for Blueprint 2. "
    "Ask an administrator to grant it."
)
DELEGATED_CALLER_REJECTED = "Agent 2 rejected the calling agent identity."
APPLICATION_ROLE_REQUIRED = (
    "Agent Identity 3 lacks the Agent2.Chat.Application role on Blueprint 2."
)
APPLICATION_CALLER_REJECTED = "Agent 2 rejected the autonomous agent identity."
TOKEN_TYPE_REJECTED = "Agent 2 rejected the access token type."
SKILL_NOT_AUTHORIZED = "This agent identity is not authorized for the requested capability."
MCP_CONSENT_REQUIRED = (
    "Agent Identity 2 lacks consent for one or more configured MCP permissions. "
    "Ask an administrator to grant the missing delegated permissions."
)
MCP_AUTHENTICATION_FAILED = (
    "Agent 2 could not acquire delegated access to the MCP service. "
    "Ask an administrator to verify Agent Identity 2 permissions."
)

SAFE_A2A_ERROR_MESSAGES = {
    USER_ROLE_REQUIRED,
    DELEGATED_SCOPE_REQUIRED,
    DELEGATED_CALLER_REJECTED,
    APPLICATION_ROLE_REQUIRED,
    APPLICATION_CALLER_REJECTED,
    TOKEN_TYPE_REJECTED,
    SKILL_NOT_AUTHORIZED,
    MCP_CONSENT_REQUIRED,
    MCP_AUTHENTICATION_FAILED,
}

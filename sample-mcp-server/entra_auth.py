"""Entra ID access-token validation for the sample server when AUTH_MODE=entra.

The server is an OAuth 2.0 protected resource. It accepts only Entra access tokens that were
issued to its own API app registration for a signed-in user (delegated scope, by default
user_impersonation), and hands the validated claims to the tools for authorization.

Settings (environment variables):
  ENTRA_TENANT_ID       tenant that issues the tokens
  ENTRA_API_APP_ID      application (client) ID of the server's API app; tokens must carry
                        aud api://<id> (v1 tokens) or <id> (v2 tokens)
  ENTRA_REQUIRED_SCOPE  delegated scope every request needs (default user_impersonation)
  MCP_RESOURCE_URL      public URL of /mcp, advertised in the protected-resource metadata.
                        Defaults to the Container App's own URL, else http://localhost:<PORT>/mcp.
"""
from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import anyio.to_thread
import jwt
from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
from mcp.server.auth.provider import AccessToken
from mcp.server.auth.settings import AuthSettings

log = logging.getLogger("sample_mcp.auth")

LOGIN = "https://login.microsoftonline.com"
# Claims kept on AccessToken.claims for the tools. The raw token is never returned to callers.
KEPT_CLAIMS = ("iss", "tid", "oid", "sub", "name", "preferred_username", "upn", "unique_name",
               "roles", "scp", "azp", "appid", "ver", "idtyp")


def resource_url(env: Mapping[str, str]) -> str:
    if url := env.get("MCP_RESOURCE_URL"):
        return url
    # Azure Container Apps sets these in every container.
    if (app := env.get("CONTAINER_APP_NAME")) and (suffix := env.get("CONTAINER_APP_ENV_DNS_SUFFIX")):
        return f"https://{app}.{suffix}/mcp"
    return f"http://localhost:{env.get('PORT', '8000')}/mcp"


@dataclass(frozen=True)
class EntraSettings:
    tenant_id: str
    api_app_id: str
    resource_url: str
    scope: str = "user_impersonation"

    @classmethod
    def from_env(cls, env: Mapping[str, str] = os.environ) -> EntraSettings:
        missing = [k for k in ("ENTRA_TENANT_ID", "ENTRA_API_APP_ID") if not env.get(k)]
        if missing:
            raise RuntimeError(f"AUTH_MODE=entra requires {' and '.join(missing)}")
        return cls(tenant_id=env["ENTRA_TENANT_ID"].strip().lower(),
                   api_app_id=env["ENTRA_API_APP_ID"].strip().lower(),
                   resource_url=resource_url(env),
                   scope=env.get("ENTRA_REQUIRED_SCOPE") or "user_impersonation")

    @property
    def scope_uri(self) -> str:
        return f"api://{self.api_app_id}/{self.scope}"

    @property
    def audiences(self) -> tuple[str, ...]:
        return f"api://{self.api_app_id}", self.api_app_id  # v1 tokens, v2 tokens

    @property
    def issuers(self) -> tuple[str, ...]:
        return f"{LOGIN}/{self.tenant_id}/v2.0", f"https://sts.windows.net/{self.tenant_id}/"

    @property
    def jwks_uri(self) -> str:
        return f"{LOGIN}/{self.tenant_id}/discovery/v2.0/keys"

    def auth_settings(self) -> AuthSettings:
        # The verifier checks the Entra audience itself; Entra tokens do not carry the MCP URL
        # as an RFC 8707 resource, so the SDK's resource check stays off.
        return AuthSettings(issuer_url=self.issuers[0], resource_server_url=self.resource_url,
                            required_scopes=[self.scope_uri], validate_token_resource=False)


class EntraTokenVerifier:
    """MCP TokenVerifier for Entra access tokens (v1 or v2) issued to the server's API app.

    App-only tokens (no scp claim) verify but carry no scopes, so the SDK answers 403
    insufficient_scope: the server only serves requests made on behalf of a user.
    """

    def __init__(self, settings: EntraSettings, jwks: jwt.PyJWKClient | None = None):
        self.settings = settings
        self.jwks = jwks or jwt.PyJWKClient(settings.jwks_uri, lifespan=3600)

    async def verify_token(self, token: str) -> AccessToken | None:
        try:
            claims = await anyio.to_thread.run_sync(self.decode, token)
        except jwt.PyJWTError as e:  # includes JWKS fetch failures, so the server fails closed
            log.warning("rejected bearer token: %s: %s [%s]", type(e).__name__, e, describe_unverified(token))
            return None
        return AccessToken(
            token=token,
            client_id=claims.get("azp") or claims.get("appid") or "",
            scopes=[f"api://{self.settings.api_app_id}/{s}" for s in claims.get("scp", "").split()],
            expires_at=int(claims["exp"]),
            resource=self.settings.resource_url,
            subject=claims["oid"],
            claims={k: claims[k] for k in KEPT_CLAIMS if k in claims},
        )

    def decode(self, token: str) -> dict[str, Any]:
        key = self.jwks.get_signing_key_from_jwt(token)
        claims = jwt.decode(token, key, algorithms=["RS256"], audience=self.settings.audiences,
                            issuer=self.settings.issuers, leeway=60,
                            options={"require": ["exp", "iat", "iss", "aud", "tid", "oid"]})
        if str(claims["tid"]).lower() != self.settings.tenant_id:
            raise jwt.InvalidIssuerError("tid is not ENTRA_TENANT_ID")
        return claims


def describe_unverified(token: str) -> str:
    """Non-identifying details of a rejected token, to diagnose audience, issuer or client mismatches."""
    try:
        header = jwt.get_unverified_header(token)
        c = jwt.decode(token, options={"verify_signature": False})
    except jwt.PyJWTError:
        return "not a JWT"
    return (f"aud={c.get('aud')} iss={c.get('iss')} client={c.get('azp') or c.get('appid')} "
            f"ver={c.get('ver')} scp={c.get('scp')} alg={header.get('alg')} kid={header.get('kid')}")


def caller(request: Any) -> AccessToken | None:
    """The validated token of the HTTP request that carried the current MCP message.

    Read from the request rather than the SDK's auth contextvar, which belongs to whichever
    request started the session's task.
    """
    user = getattr(request, "scope", {}).get("user") if request is not None else None
    return user.access_token if isinstance(user, AuthenticatedUser) else None


def describe(token: AccessToken | None) -> dict[str, Any]:
    """The caller as the server sees them, for the whoami tool. Never includes the token itself."""
    if token is None:
        return {"authenticated": False}
    c = token.claims or {}
    return {
        "authenticated": True,
        "name": c.get("name"),
        "username": c.get("preferred_username") or c.get("upn") or c.get("unique_name"),
        "oid": c.get("oid"),
        "tid": c.get("tid"),
        "roles": sorted(c.get("roles", [])),
        "scopes": sorted(c.get("scp", "").split()),
        "client_app_id": token.client_id,
        "token_version": c.get("ver"),
    }

"""Entra access tokens from the Azure CLI login, cached per scope and shared by the runners.

AzureCliCredential shells out to `az` (seconds per call), so a token is reused until shortly before
it expires instead of being fetched for every request.
"""
from __future__ import annotations

import base64
import json
import threading
import time

from azure.core.exceptions import ClientAuthenticationError
from azure.identity import AzureCliCredential

FOUNDRY_SCOPE = "https://ai.azure.com/.default"
REFRESH_MARGIN_S = 300
_ATTEMPTS = 3
# Retrying cannot fix these; fail fast with the CLI's own message.
_PERMANENT = ("az login", "not found on path", "not installed")

_lock = threading.Lock()
_cache: dict[str, tuple[str, int]] = {}
_credential = None


def get_token(scope: str = FOUNDRY_SCOPE) -> str:
    """Bearer token for `scope`, fetched with `az` only when missing or about to expire."""
    global _credential
    with _lock:  # one refresh at a time; concurrent callers reuse its result
        cached = _cache.get(scope)
        if cached and cached[1] - REFRESH_MARGIN_S > time.time():
            return cached[0]
        if _credential is None:
            # `az` can exceed the 10 s default on a cold start or a slow network.
            _credential = AzureCliCredential(process_timeout=30)
        for attempt in range(_ATTEMPTS):
            try:
                tok = _credential.get_token(scope)
                break
            except ClientAuthenticationError as e:
                if attempt == _ATTEMPTS - 1 or any(s in str(e) for s in _PERMANENT):
                    raise
                time.sleep(2 * (attempt + 1))
        _cache[scope] = (tok.token, tok.expires_on)
        return tok.token


def caller_claims(scope: str = FOUNDRY_SCOPE) -> dict[str, str]:
    """The signed-in user's oid, tid, name and username (preferred_username, else upn, else unique_name)
    from the token for `scope`. Claims the token lacks are left out; the signature is not checked."""
    payload = get_token(scope).split(".")[1]
    claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    out = {k: claims[k] for k in ("oid", "tid", "name") if isinstance(claims.get(k), str)}
    for k in ("preferred_username", "upn", "unique_name"):
        if isinstance(claims.get(k), str):
            out["username"] = claims[k]
            break
    return out

"""Entra mode of the sample server: token validation, HTTP 401/403, whoami, and region authorization.

Tokens are signed with a locally generated RSA key; the verifier's JWKS client serves its public key,
so nothing here touches the network.
"""
import asyncio
import contextlib
import json
import logging
import sys
import time
import unittest
import uuid
from pathlib import Path

import httpx2
import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "sample-mcp-server"))
_root = logging.getLogger()
_saved = (_root.level, list(_root.handlers))
import data  # noqa: E402
import entra_auth  # noqa: E402
import server  # noqa: E402  (MCPServer() calls logging.basicConfig at INFO; undone below)
_root.setLevel(_saved[0])
_root.handlers[:] = _saved[1]
logging.getLogger("sample_mcp.auth").setLevel(logging.ERROR)  # rejections are expected here

TENANT, API_APP, CLIENT_APP, OID = (str(uuid.uuid4()) for _ in range(4))
BASE = "http://localhost:8000"
SETTINGS = entra_auth.EntraSettings(tenant_id=TENANT, api_app_id=API_APP, resource_url=f"{BASE}/mcp")
SCOPE_URI = f"api://{API_APP}/user_impersonation"
NA_EU = ["Customers.Read.NorthAmerica", "Customers.Read.Europe"]


def _rsa_key(kid: str):
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(private.public_key()))
    return private, {**public_jwk, "kid": kid, "use": "sig"}


KEY, JWK = _rsa_key("key-1")
IMPOSTER_KEY, _ = _rsa_key("key-1")  # same kid, different key


def jwks_client() -> jwt.PyJWKClient:
    client = jwt.PyJWKClient("https://login.invalid/keys")
    client.fetch_data = lambda: {"keys": [JWK]}
    return client


def make_token(version: int = 2, key=KEY, kid: str = "key-1", **claims) -> str:
    """A user token like Entra issues for the API app; a claim set to None is removed."""
    now = int(time.time())
    body = {"iat": now, "nbf": now, "exp": now + 3600, "tid": TENANT, "oid": OID, "sub": "pairwise-subject",
            "name": "Test User", "scp": "user_impersonation", "roles": NA_EU}
    if version == 2:
        body.update(aud=API_APP, iss=f"https://login.microsoftonline.com/{TENANT}/v2.0", ver="2.0",
                    azp=CLIENT_APP, preferred_username="test.user@contoso.example")
    else:
        body.update(aud=f"api://{API_APP}", iss=f"https://sts.windows.net/{TENANT}/", ver="1.0",
                    appid=CLIENT_APP, upn="test.user@contoso.example")
    body.update(claims)
    body = {k: v for k, v in body.items() if v is not None}
    return jwt.encode(body, key, algorithm="RS256", headers={"kid": kid})


def verify(token: str):
    return asyncio.run(entra_auth.EntraTokenVerifier(SETTINGS, jwks_client()).verify_token(token))


class VerifierTests(unittest.TestCase):
    def test_accepts_v2_user_token(self):
        info = verify(make_token(2))
        self.assertIsNotNone(info)
        self.assertEqual(info.scopes, [SCOPE_URI])
        self.assertEqual(info.client_id, CLIENT_APP)
        self.assertEqual(info.subject, OID)
        self.assertEqual(info.claims["preferred_username"], "test.user@contoso.example")

    def test_accepts_v1_user_token(self):
        info = verify(make_token(1))
        self.assertIsNotNone(info)
        self.assertEqual(info.client_id, CLIENT_APP)
        self.assertEqual(entra_auth.describe(info)["username"], "test.user@contoso.example")

    def test_rejects_invalid_tokens(self):
        other_tenant = str(uuid.uuid4())
        for label, token in [
            ("wrong audience", make_token(aud="api://some-other-app")),
            ("wrong issuer", make_token(iss=f"https://login.microsoftonline.com/{other_tenant}/v2.0")),
            ("wrong tenant claim", make_token(tid=other_tenant)),
            ("expired", make_token(exp=int(time.time()) - 600)),
            ("bad signature", make_token(key=IMPOSTER_KEY)),
            ("unknown key", make_token(kid="key-2")),
            ("no oid", make_token(oid=None)),
            ("unsigned", jwt.encode({"aud": API_APP}, None, algorithm="none", headers={"kid": "key-1"})),
            ("not a JWT", "not-a-token"),
        ]:
            with self.subTest(label):
                self.assertIsNone(verify(token))

    def test_app_only_token_has_no_scopes(self):
        info = verify(make_token(scp=None, roles=["Customers.Read.All"]))
        self.assertIsNotNone(info)
        self.assertEqual(info.scopes, [])

    def test_settings_from_env(self):
        env = {"ENTRA_TENANT_ID": TENANT.upper(), "ENTRA_API_APP_ID": API_APP,
               "CONTAINER_APP_NAME": "sample-mcp-entra", "CONTAINER_APP_ENV_DNS_SUFFIX": "env.example.io"}
        s = entra_auth.EntraSettings.from_env(env)
        self.assertEqual(s.tenant_id, TENANT)
        self.assertEqual(s.resource_url, "https://sample-mcp-entra.env.example.io/mcp")
        self.assertEqual(entra_auth.EntraSettings.from_env({**env, "MCP_RESOURCE_URL": "https://x/mcp"}).resource_url,
                         "https://x/mcp")
        with self.assertRaisesRegex(RuntimeError, "ENTRA_API_APP_ID"):
            entra_auth.EntraSettings.from_env({"ENTRA_TENANT_ID": TENANT})


class RegionPolicyTests(unittest.TestCase):
    def test_visible_regions(self):
        self.assertEqual(data.visible_regions(NA_EU), ["Europe", "North America"])
        self.assertEqual(data.visible_regions(["Customers.Read.All"]), ["APAC", "Europe", "North America"])
        self.assertEqual(data.visible_regions(["Something.Else"]), [])

    def test_every_customer_region_has_a_role(self):
        self.assertEqual({c["region"] for c in data.CUSTOMERS.values()}, set(data.REGION_ROLES.values()))


@contextlib.asynccontextmanager
async def http_app():
    _saved_root = (_root.level, list(_root.handlers))
    mcp = server.create_server(SETTINGS, verifier=entra_auth.EntraTokenVerifier(SETTINGS, jwks_client()))
    _root.setLevel(_saved_root[0])
    _root.handlers[:] = _saved_root[1]
    app = mcp.streamable_http_app(host="0.0.0.0")  # as in production: no DNS-rebinding host check
    async with app.router.lifespan_context(app):
        yield app


async def _mcp(token: str, fn):
    async with http_app() as app, httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=app), headers={"Authorization": "Bearer " + token},
    ) as http, Client(streamable_http_client(f"{BASE}/mcp", http_client=http), mode="legacy") as client:
        return await fn(client)


def call(token: str, tool: str, args: dict) -> tuple[bool, dict]:
    result = asyncio.run(_mcp(token, lambda c: c.call_tool(tool, args)))
    return result.is_error, json.loads("\n".join(c.text for c in result.content if c.type == "text"))


async def _raw(method: str, path: str, token: str | None = None) -> httpx2.Response:
    headers = {"Accept": "application/json, text/event-stream"}
    if token:
        headers["Authorization"] = "Bearer " + token
    body = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
        "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "test", "version": "0"}}}
    async with http_app() as app, httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app)) as http:
        return await http.request(method, BASE + path, headers=headers, json=body if method == "POST" else None)


class HttpTests(unittest.TestCase):
    def test_no_token_is_401_with_resource_metadata(self):
        r = asyncio.run(_raw("POST", "/mcp"))
        self.assertEqual(r.status_code, 401)
        self.assertIn(f'resource_metadata="{BASE}/.well-known/oauth-protected-resource/mcp"',
                      r.headers["www-authenticate"])

    def test_invalid_token_is_401(self):
        self.assertEqual(asyncio.run(_raw("POST", "/mcp", make_token(aud="api://other"))).status_code, 401)

    def test_app_only_token_is_403_insufficient_scope(self):
        r = asyncio.run(_raw("POST", "/mcp", make_token(scp=None)))
        self.assertEqual(r.status_code, 403)
        self.assertIn("insufficient_scope", r.headers["www-authenticate"])

    def test_protected_resource_metadata(self):
        r = asyncio.run(_raw("GET", "/.well-known/oauth-protected-resource/mcp"))
        self.assertEqual(r.status_code, 200)
        prm = r.json()
        self.assertEqual(prm["resource"], f"{BASE}/mcp")
        self.assertEqual(prm["authorization_servers"], [f"https://login.microsoftonline.com/{TENANT}/v2.0"])
        self.assertEqual(prm["scopes_supported"], [SCOPE_URI])

    def test_lists_whoami_in_entra_mode(self):
        result = asyncio.run(_mcp(make_token(), lambda c: c.list_tools()))
        self.assertEqual(sorted(t.name for t in result.tools),
                         ["get_customer", "get_market_conditions", "get_stock_quote", "whoami"])

    def test_whoami_describes_the_caller(self):
        is_error, me = call(make_token(), "whoami", {})
        self.assertFalse(is_error)
        self.assertTrue(me["authenticated"])
        self.assertEqual((me["oid"], me["tid"], me["client_app_id"]), (OID, TENANT, CLIENT_APP))
        self.assertEqual(me["username"], "test.user@contoso.example")
        self.assertEqual(me["roles"], sorted(NA_EU))
        self.assertEqual(me["regions_visible"], ["Europe", "North America"])
        self.assertNotIn(make_token().split(".")[0], json.dumps(me))  # no token material

    def test_region_authorization(self):
        token = make_token()
        for tool, args, denied in [
            ("get_customer", {"customer_id": "C1001"}, False),  # North America
            ("get_customer", {"customer_id": "C1002"}, False),  # Europe
            ("get_customer", {"customer_id": "C1004"}, True),   # APAC
            ("get_customer", {"name": "Adventure Works"}, True),
            ("get_stock_quote", {"customer_id": "C1004"}, True),
            ("get_stock_quote", {"ticker": "ADVW"}, False),     # public market data
            ("get_market_conditions", {"industry": "retail"}, False),
        ]:
            with self.subTest(tool=tool, args=args):
                is_error, result = call(token, tool, args)
                self.assertFalse(is_error)  # a denial is a result the agent can explain, not a tool error
                self.assertEqual(result.get("access_denied", False), denied)
                if denied:
                    self.assertNotIn("name", result)
                    self.assertIn("Customers.Read.APAC", result["required_roles"])

    def test_all_regions_role_sees_apac(self):
        is_error, result = call(make_token(roles=["Customers.Read.All"]), "get_customer", {"customer_id": "C1004"})
        self.assertFalse(is_error)
        self.assertEqual(result["name"], "Adventure Works Retail")

    def test_no_roles_sees_no_customers_but_market_data(self):
        token = make_token(roles=None)
        self.assertTrue(call(token, "get_customer", {"customer_id": "C1001"})[1]["access_denied"])
        self.assertEqual(call(token, "get_market_conditions", {"industry": "energy"})[1]["outlook"], "negative")

    def test_tool_errors_unchanged(self):
        result = asyncio.run(_mcp(make_token(), lambda c: c.call_tool("get_customer", {"customer_id": "BAD"})))
        self.assertTrue(result.is_error)
        self.assertIn("not found", result.content[0].text)


if __name__ == "__main__":
    unittest.main()

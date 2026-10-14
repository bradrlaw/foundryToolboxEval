r"""Create the Foundry project connection and toolbox for an MCP server (idempotent).

Usage:
  python scripts/deploy_toolbox.py --mcp-url https://<host>/mcp --resource-group <rg> [--subscription <id>]
         [--connection sample-mcp-conn] [--server-label sample] [--toolbox sample-customer-toolbox]
         [--auth-type None|UserEntraToken|OAuth2] [--audience <uri>] [--client-id <app-id>]
         [--client-secret-env <VAR>] [--scopes <scope> ...] [--tenant <id>] [--secret-days 180]
         [--description "..."] [--no-promote] [--delete] [--dry-run]

Reads FOUNDRY_PROJECT_ENDPOINT from .env (account and project names are derived from it) and
uses your `az login`. Needs Contributor on the Foundry resource (connection) and Azure AI User
on the project (toolbox). Each run publishes a new toolbox version; the first version becomes the
default, later ones are promoted to default unless --no-promote is given.

--auth-type sets how the toolbox authenticates to the MCP server:
  None            no credentials (default).
  UserEntraToken  Foundry exchanges the caller's Entra token on behalf of the user for --audience
                  (default api://$ENTRA_API_APP_ID).
  OAuth2          each user consents once, through the Entra client app --client-id (default
                  $ENTRA_OAUTH_CLIENT_APP_ID), to --scopes (default api://$ENTRA_API_APP_ID/user_impersonation
                  and offline_access). Unless --client-secret-env names a variable that holds the client secret,
                  each run adds a new secret to the app, sends it only to the connection, then removes the
                  older secrets made for the same connection. The secret is never printed or saved.
                  Foundry's redirect URI for the connection is added to the app's web redirect URIs.
--delete removes the toolbox and connection instead (with --auth-type OAuth2, also the connection's
client secrets and redirect URI). --dry-run prints the requests without sending them.
"""
from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

from dotenv import load_dotenv

from _az import GRAPH, az, graph, rest, where

ROOT = Path(__file__).resolve().parent.parent
AI = "https://ai.azure.com"
AUTH_TYPES = {"none": "None", "userentratoken": "UserEntraToken", "user-entra-token": "UserEntraToken",
              "oauth2": "OAuth2"}
# The identity and OAuth2 connection fields need the preview API version (as azd uses); None keeps GA.
CONN_API = {"None": "2025-06-01", "UserEntraToken": "2025-04-01-preview", "OAuth2": "2025-04-01-preview"}


def auth_type(value: str) -> str:
    """ARM authType from None, UserEntraToken or OAuth2 in any case, or azd's user-entra-token."""
    try:
        return AUTH_TYPES[value.lower()]
    except KeyError:
        raise argparse.ArgumentTypeError(f"use None, UserEntraToken or OAuth2, not {value!r}") from None


def entra_oauth_urls(tenant: str) -> tuple[str, str]:
    """The Entra v2 authorize and token endpoints of a tenant."""
    base = f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0"
    return f"{base}/authorize", f"{base}/token"


def connection_body(auth: str, target: str, *, audience: str | None = None, tenant: str | None = None,
                    client_id: str | None = None, client_secret: str | None = None,
                    scopes: list[str] | None = None) -> dict:
    props = {"category": "RemoteTool", "target": target, "authType": auth, "metadata": {"type": "custom_MCP"}}
    if auth == "UserEntraToken":
        props["audience"] = audience
    elif auth == "OAuth2":
        authorize, token = entra_oauth_urls(tenant)
        props |= {"authorizationUrl": authorize, "tokenUrl": token, "refreshUrl": token, "scopes": scopes,
                  "credentials": {"clientId": client_id, "clientSecret": client_secret}}
    return {"properties": props}


def version_body(description: str, server_label: str, mcp_url: str, connection: str) -> dict:
    return {
        "description": description,
        "tools": [{
            "type": "mcp",
            "server_label": server_label,
            "server_url": mcp_url,
            "require_approval": "never",
            "project_connection_id": connection,
        }],
    }


def stale_secrets(credentials: list[dict], label: str, keep: str | None = None) -> list[str]:
    """Key IDs of the app's client secrets named `label` (made for this connection), except `keep`."""
    return [c["keyId"] for c in credentials if c.get("displayName") == label and c["keyId"] != keep]


def add_secret(app: dict, label: str, days: int) -> dict:
    end = (datetime.now(timezone.utc) + timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return graph("POST", f"/applications/{app['id']}/addPassword",
                 {"passwordCredential": {"displayName": label, "endDateTime": end}})


def remove_secrets(app: dict, key_ids: list[str]) -> None:
    for key_id in key_ids:
        graph("POST", f"/applications/{app['id']}/removePassword", {"keyId": key_id})


def find_app(client_id: str) -> dict | None:
    found = where("applications", f"appId eq '{client_id}'")
    return found[0] if found else None


def redirect_url(conn_base: str) -> str | None:
    """Foundry's OAuth redirect URI for the connection (properties.redirectUrl), if it has one yet."""
    for version in ("2025-04-01-preview", "2025-06-01"):
        if url := rest("GET", f"{conn_base}?api-version={version}").get("properties", {}).get("redirectUrl"):
            return url
    return None


def register_redirect(app: dict | None, client_id: str, redirect: str | None) -> None:
    """Add Foundry's redirect URI to the client app's web redirect URIs, or say how to."""
    if not redirect:
        print("the connection has no redirectUrl yet; rerun to add it to the client app")
        return
    uris = ((app or {}).get("web") or {}).get("redirectUris") or []
    if redirect in uris:
        print(f"redirect URI already on the client app: {redirect}")
        return
    if app:
        try:
            graph("PATCH", f"/applications/{app['id']}", {"web": {"redirectUris": [*uris, redirect]}})
            print(f"redirect URI added to the client app: {redirect}")
            return
        except SystemExit as exc:
            print(exc)
    print(f"add this web redirect URI to client app {client_id}: {redirect}")


def unregister_redirect(app: dict, redirect: str | None) -> bool:
    """Remove a deleted connection's redirect URI (each OAuth2 connection has its own) from the client app."""
    uris = (app.get("web") or {}).get("redirectUris") or []
    if not redirect or redirect not in uris:
        return False
    graph("PATCH", f"/applications/{app['id']}", {"web": {"redirectUris": [u for u in uris if u != redirect]}})
    return True


def main(argv: list[str] | None = None) -> int:
    load_dotenv(ROOT / ".env")
    project_url = os.environ.get("FOUNDRY_PROJECT_ENDPOINT", "").rstrip("/")
    if not project_url:
        raise SystemExit("Set FOUNDRY_PROJECT_ENDPOINT in .env (see .env.example)")
    api_app = os.environ.get("ENTRA_API_APP_ID")

    ap = argparse.ArgumentParser(description="Create the Foundry project connection and toolbox for an MCP server.")
    ap.add_argument("--mcp-url", help="MCP server URL, e.g. https://<host>/mcp")
    ap.add_argument("--resource-group", required=True, help="resource group of the Foundry resource")
    ap.add_argument("--subscription", help="default: current az subscription")
    ap.add_argument("--connection", default="sample-mcp-conn")
    ap.add_argument("--server-label", default="sample")
    ap.add_argument("--toolbox", default=os.environ.get("FOUNDRY_TOOLBOX_NAME", "sample-customer-toolbox"))
    ap.add_argument("--description", default="Sample customer, market and stock tools for toolbox testing")
    ap.add_argument("--auth-type", type=auth_type, default="None", help="None (default), UserEntraToken or OAuth2")
    ap.add_argument("--audience", help="UserEntraToken: token audience (default api://$ENTRA_API_APP_ID)")
    ap.add_argument("--client-id", help="OAuth2: client app ID (default $ENTRA_OAUTH_CLIENT_APP_ID)")
    ap.add_argument("--client-secret-env", metavar="VAR",
                    help="OAuth2: environment variable that holds the client secret (no secret rotation)")
    ap.add_argument("--scopes", nargs="+",
                    help="OAuth2: scopes (default api://$ENTRA_API_APP_ID/user_impersonation offline_access)")
    ap.add_argument("--tenant", help="OAuth2: Entra tenant of the client app (default $ENTRA_TENANT_ID or your az tenant)")
    ap.add_argument("--secret-days", type=int, default=180, help="OAuth2: lifetime of a new client secret (default 180)")
    ap.add_argument("--no-promote", action="store_true")
    ap.add_argument("--delete", action="store_true")
    ap.add_argument("--dry-run", action="store_true", help="print the requests instead of sending them")
    args = ap.parse_args(argv)
    auth = args.auth_type
    if not args.delete and not args.mcp_url:
        ap.error("--mcp-url is required unless --delete")
    if args.audience and auth != "UserEntraToken":
        ap.error("--audience needs --auth-type UserEntraToken")
    if extra := [f for f in ("client_id", "client_secret_env", "scopes", "tenant") if getattr(args, f)]:
        if auth != "OAuth2":
            ap.error(f"--{extra[0].replace('_', '-')} needs --auth-type OAuth2")

    audience = client_id = scopes = tenant = None
    if auth == "UserEntraToken":
        audience = args.audience or (f"api://{api_app}" if api_app else None)
        if not audience:
            ap.error("UserEntraToken needs --audience (or ENTRA_API_APP_ID in .env)")
    if auth == "OAuth2":
        client_id = args.client_id or os.environ.get("ENTRA_OAUTH_CLIENT_APP_ID")
        scopes = args.scopes or ([f"api://{api_app}/user_impersonation", "offline_access"] if api_app else None)
        if not client_id or not (scopes or args.delete):
            ap.error("OAuth2 needs --client-id and --scopes (or ENTRA_OAUTH_CLIENT_APP_ID and ENTRA_API_APP_ID in .env)")

    parsed = urlparse(project_url)
    account = parsed.hostname.split(".")[0]
    project = parsed.path.rstrip("/").split("/")[-1]
    label = f"Foundry connection {account}/{project}/{args.connection}"  # names this connection's client secrets

    sub = args.subscription
    if not sub:
        code, sub, err = az("account", "show", "--query", "id", "-o", "tsv")
        if code != 0 or not sub:
            raise SystemExit(f"az login first: {err}")
    if auth == "OAuth2" and not args.delete:
        tenant = args.tenant or os.environ.get("ENTRA_TENANT_ID")
        if not tenant:
            code, tenant, err = az("account", "show", "--query", "tenantId", "-o", "tsv")
            if code != 0 or not tenant:
                raise SystemExit(f"az login first: {err}")

    conn_base = (
        f"https://management.azure.com/subscriptions/{sub}/resourceGroups/{args.resource_group}"
        f"/providers/Microsoft.CognitiveServices/accounts/{account}/projects/{project}"
        f"/connections/{args.connection}"
    )
    conn_url = f"{conn_base}?api-version={CONN_API[auth]}"
    tb_url = f"{project_url}/toolboxes/{args.toolbox}"

    if args.delete:
        if args.dry_run:
            print(f"DELETE {tb_url}?api-version=v1\nDELETE {conn_url}")
            if auth == "OAuth2":
                print(f"POST {GRAPH}/applications/<app {client_id}>/removePassword for each secret named {label!r}")
                print(f"PATCH {GRAPH}/applications/<app {client_id}> without the connection's redirect URI")
            return 0
        redirect = None
        if auth == "OAuth2":
            try:
                redirect = redirect_url(conn_base)
            except SystemExit:
                pass  # the connection is already gone
        rest("DELETE", f"{tb_url}?api-version=v1", resource=AI)
        print(f"deleted toolbox {args.toolbox}")
        rest("DELETE", conn_url)
        print(f"deleted connection {args.connection}")
        if auth == "OAuth2" and (app := find_app(client_id)):
            old = stale_secrets(app.get("passwordCredentials") or [], label)
            remove_secrets(app, old)
            print(f"removed {len(old)} client secret(s) made for the connection")
            if unregister_redirect(app, redirect):
                print(f"removed its redirect URI from the client app: {redirect}")
        return 0

    rotate = auth == "OAuth2" and not args.client_secret_env
    placeholder = f"<value of ${args.client_secret_env}>" if args.client_secret_env else "<new client secret>"
    conn_body = connection_body(auth, args.mcp_url, audience=audience, tenant=tenant, client_id=client_id,
                                client_secret=placeholder if auth == "OAuth2" else None, scopes=scopes)
    version_req = version_body(args.description, args.server_label, args.mcp_url, args.connection)

    if args.dry_run:
        if rotate:
            print(f"POST {GRAPH}/applications/<app {client_id}>/addPassword "
                  f"(displayName {label!r}, {args.secret_days} days)\n")
        print(f"PUT {conn_url}\n{json.dumps(conn_body, indent=2)}\n")
        if rotate:
            print(f"POST {GRAPH}/applications/<app {client_id}>/removePassword for older secrets named {label!r}")
        if auth == "OAuth2":
            print(f"GET {conn_base} -> properties.redirectUrl, added to the web redirect URIs of app {client_id}\n")
        print(f"POST {tb_url}/versions?api-version=v1\n{json.dumps(version_req, indent=2)}\n")
        if not args.no_promote:
            print(f"PATCH {tb_url}?api-version=v1 (default_version -> the new version, if it isn't already)")
        return 0

    app = new_key = None
    if auth == "OAuth2":
        app = find_app(client_id)
        if args.client_secret_env:
            secret = os.environ.get(args.client_secret_env)
            if not secret:
                raise SystemExit(f"environment variable {args.client_secret_env} is not set")
        elif app is None:
            raise SystemExit(f"client app {client_id} is not in your az login tenant; "
                             "pass its secret with --client-secret-env")
        else:
            created = add_secret(app, label, args.secret_days)
            secret, new_key = created["secretText"], created["keyId"]
        conn_body["properties"]["credentials"]["clientSecret"] = secret
    try:
        rest("PUT", conn_url, conn_body)
    except SystemExit:
        if new_key:
            remove_secrets(app, [new_key])
        raise
    print(f"connection {args.connection} ({auth}) -> {args.mcp_url}")
    if new_key:
        old = stale_secrets(app.get("passwordCredentials") or [], label, keep=new_key)
        remove_secrets(app, old)
        print(f"client secret rotated: expires in {args.secret_days} days, {len(old)} older removed")
    if auth == "OAuth2":
        register_redirect(app, client_id, redirect_url(conn_base))

    version = rest("POST", f"{tb_url}/versions?api-version=v1", version_req, resource=AI)
    ver = version.get("version")
    print(f"toolbox {args.toolbox} version {ver} created")

    current = rest("GET", f"{tb_url}?api-version=v1", resource=AI)
    if not args.no_promote and current.get("default_version") != ver:
        rest("PATCH", f"{tb_url}?api-version=v1", {"default_version": ver}, resource=AI)
        print(f"default version -> {ver}")

    print(f"consumer endpoint: {tb_url}/mcp?api-version=v1")
    if auth == "OAuth2":
        print("Each user's first call returns a consent link (JSON-RPC -32006 CONSENT_REQUIRED); open it once.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

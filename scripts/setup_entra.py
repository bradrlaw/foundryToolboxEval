r"""Register the Entra apps behind the sample server's AUTH_MODE=entra (idempotent).

Usage:
  python scripts/setup_entra.py [--roles NorthAmerica Europe] [--oauth-client] [--write-env]
         [--trusted-client <appId> ...] [--api-app-name <name>] [--client-app-name <name>] [--delete]

Creates or updates, in the tenant of your `az login`:
  1. The API app registration whose tokens the sample server accepts: identifier URI api://<appId>,
     delegated scope user_impersonation, v2 access tokens, and the Customers.Read.* app roles.
  2. Its service principal, and your own app-role assignments. --roles sets exactly which of these
     roles you hold (default NorthAmerica Europe, so APAC customers are denied).
  3. Tenant-wide consent for trusted client apps to request the API scope for any user (default: the
     Azure CLI, which the tool runner uses to call the server directly).
  4. With --oauth-client: a confidential client app for OAuth2 identity-passthrough connections,
     with admin consent for the API scope, openid and offline_access. deploy_toolbox.py adds its
     client secret and Foundry's redirect URI.

Needs a directory role that can register apps and grant tenant-wide consent (Cloud Application
Administrator or higher). ENTRA_API_APP_ID and ENTRA_OAUTH_CLIENT_APP_ID in .env select existing
apps; otherwise apps are found by display name or created. Prints the .env values (--write-env saves
them to .env). --delete removes both app registrations.
"""
from __future__ import annotations

import argparse
import os
import re
import uuid
from pathlib import Path

from dotenv import load_dotenv

from _az import az, graph, where

ROOT = Path(__file__).resolve().parent.parent
SCOPE = "user_impersonation"
AZURE_CLI_APP_ID = "04b07795-8ddb-461a-bbee-02f9e1bf7b46"
MS_GRAPH_APP_ID = "00000003-0000-0000-c000-000000000000"
GRAPH_SCOPE_IDS = {"openid": "37f7f235-527c-4136-accd-4a02d197296e",
                   "offline_access": "7427e0e9-2fba-42fe-b0c0-848c9e6a8182"}
# Must match ALL_REGIONS_ROLE and REGION_ROLES in sample-mcp-server/data.py.
ROLES = {
    "Customers.Read.All": "Read customers in all regions",
    "Customers.Read.NorthAmerica": "Read North America customers",
    "Customers.Read.Europe": "Read Europe customers",
    "Customers.Read.APAC": "Read APAC customers",
}
ROLE_PREFIX = "Customers.Read."


def role_values(names: list[str]) -> list[str]:
    """Full role values from short (APAC) or full (Customers.Read.APAC) names, case-insensitive."""
    by_lower = {v.lower(): v for v in ROLES}
    out = []
    for n in names:
        value = by_lower.get(n.lower()) or by_lower.get((ROLE_PREFIX + n).lower())
        if not value:
            raise SystemExit(f"unknown role {n!r}; choose from {', '.join(v.removeprefix(ROLE_PREFIX) for v in ROLES)}")
        out.append(value)
    return sorted(set(out))


def with_missing(existing: list[dict], desired: list[dict]) -> list[dict]:
    """Existing scopes or roles plus the desired ones that are missing by value, with new ids.

    Existing entries are left as they are: Graph rejects changes to an enabled scope or role.
    """
    have = {e["value"] for e in existing}
    return existing + [{"id": str(uuid.uuid4()), **d} for d in desired if d["value"] not in have]


def merge_access(existing: list[dict], needed: list[dict]) -> list[dict]:
    """requiredResourceAccess with every needed permission added, keeping what is already there."""
    merged = {e["resourceAppId"]: {**e, "resourceAccess": list(e["resourceAccess"])} for e in existing}
    for entry in needed:
        target = merged.setdefault(entry["resourceAppId"], {"resourceAppId": entry["resourceAppId"], "resourceAccess": []})
        have = {a["id"] for a in target["resourceAccess"]}
        target["resourceAccess"] += [a for a in entry["resourceAccess"] if a["id"] not in have]
    return list(merged.values())


def upsert_env(text: str, values: dict[str, str]) -> str:
    """Set KEY=value lines in .env text, replacing an existing or commented-out line for the key."""
    lines = text.splitlines()
    for key, value in values.items():
        pattern = re.compile(rf"^\s*#?\s*{re.escape(key)}\s*=")
        idx = next((i for i, line in enumerate(lines) if pattern.match(line)), None)
        if idx is None:
            lines.append(f"{key}={value}")
        else:
            lines[idx] = f"{key}={value}"
    return "\n".join(lines) + "\n"


def find_app(app_id: str | None, name: str, required: bool = True) -> dict | None:
    if app_id:
        found = where("applications", f"appId eq '{app_id}'")
        if not found and required:
            raise SystemExit(f"app {app_id} is not in this tenant; remove it from .env to create a new one")
        return found[0] if found else None
    found = where("applications", f"displayName eq '{name}'")
    if len(found) > 1:
        raise SystemExit(f"{len(found)} app registrations are named {name!r}; put the right app ID in .env")
    return found[0] if found else None


def service_principal(app_id: str) -> dict:
    found = where("servicePrincipals", f"appId eq '{app_id}'")
    return found[0] if found else graph("POST", "/servicePrincipals", {"appId": app_id})


def ensure_grant(client_sp: str, resource_sp: str, scopes: list[str]) -> None:
    """Tenant-wide (admin) consent for client_sp to request these delegated scopes of resource_sp."""
    grants = [g for g in where("oauth2PermissionGrants", f"clientId eq '{client_sp}'")
              if g["resourceId"] == resource_sp and g["consentType"] == "AllPrincipals"]
    if not grants:
        graph("POST", "/oauth2PermissionGrants", {"clientId": client_sp, "consentType": "AllPrincipals",
                                                  "resourceId": resource_sp, "scope": " ".join(scopes)})
        return
    have = (grants[0].get("scope") or "").split()
    if missing := [s for s in scopes if s not in have]:
        graph("PATCH", f"/oauth2PermissionGrants/{grants[0]['id']}", {"scope": " ".join(have + missing)})


def setup_api(name: str) -> tuple[dict, dict]:
    app = find_app(os.environ.get("ENTRA_API_APP_ID"), name)
    if app is None:
        app = graph("POST", "/applications", {"displayName": name, "signInAudience": "AzureADMyOrg"})
        print(f"created API app {name} ({app['appId']})")
    uri = f"api://{app['appId']}"
    scope = {"value": SCOPE, "type": "User", "isEnabled": True,
             "adminConsentDisplayName": "Use the sample customer tools as the signed-in user",
             "adminConsentDescription": "Lets the app call the sample MCP server's tools on behalf of the signed-in user.",
             "userConsentDisplayName": "Use the sample customer tools as you",
             "userConsentDescription": "Lets the app call the sample MCP server's tools on your behalf."}
    roles = [{"value": v, "displayName": d, "description": d, "allowedMemberTypes": ["User"], "isEnabled": True}
             for v, d in ROLES.items()]
    api = app.get("api") or {}
    graph("PATCH", f"/applications/{app['id']}", {
        "identifierUris": app["identifierUris"] if uri in app["identifierUris"] else [*app["identifierUris"], uri],
        # PATCH replaces the whole api object, so send back what is already there.
        "api": {**api, "requestedAccessTokenVersion": 2,
                "oauth2PermissionScopes": with_missing(api.get("oauth2PermissionScopes", []), [scope])},
        "appRoles": with_missing(app.get("appRoles", []), roles),
    })
    app = graph("GET", f"/applications/{app['id']}")
    return app, service_principal(app["appId"])


def scope_id(api_app: dict) -> str:
    return next(s["id"] for s in api_app["api"]["oauth2PermissionScopes"] if s["value"] == SCOPE)


def trust_clients(api_app: dict, api_sp: dict, client_ids: list[str]) -> None:
    """Pre-authorize the clients for the API scope and grant tenant-wide consent, so no user is prompted."""
    api = api_app["api"]
    pre = [p for p in api.get("preAuthorizedApplications", []) if p["appId"] not in client_ids]
    pre += [{"appId": c, "delegatedPermissionIds": [scope_id(api_app)]} for c in client_ids]
    graph("PATCH", f"/applications/{api_app['id']}", {"api": {**api, "preAuthorizedApplications": pre}})
    for client in client_ids:
        ensure_grant(service_principal(client)["id"], api_sp["id"], [SCOPE])


def sync_roles(api_app: dict, api_sp: dict, user_id: str, wanted: list[str]) -> None:
    """Make the user's assignments among ROLES exactly `wanted`; other assignments are left alone."""
    ids = {r["value"]: r["id"] for r in api_app["appRoles"]}
    ours = {ids[v] for v in ROLES}
    assigned = {a["appRoleId"]: a["id"]
                for a in graph("GET", f"/servicePrincipals/{api_sp['id']}/appRoleAssignedTo?$top=999")["value"]
                if a["principalId"] == user_id and a["appRoleId"] in ours}
    want = {ids[v] for v in wanted}
    for role in want - assigned.keys():
        graph("POST", f"/servicePrincipals/{api_sp['id']}/appRoleAssignedTo",
              {"principalId": user_id, "resourceId": api_sp["id"], "appRoleId": role})
    for role in assigned.keys() - want:
        graph("DELETE", f"/servicePrincipals/{api_sp['id']}/appRoleAssignedTo/{assigned[role]}")


def setup_oauth_client(name: str, api_app: dict, api_sp: dict) -> dict:
    needed = [{"resourceAppId": api_app["appId"], "resourceAccess": [{"id": scope_id(api_app), "type": "Scope"}]},
              {"resourceAppId": MS_GRAPH_APP_ID,
               "resourceAccess": [{"id": i, "type": "Scope"} for i in GRAPH_SCOPE_IDS.values()]}]
    app = find_app(os.environ.get("ENTRA_OAUTH_CLIENT_APP_ID"), name)
    if app is None:
        app = graph("POST", "/applications", {"displayName": name, "signInAudience": "AzureADMyOrg",
                                              "requiredResourceAccess": needed})
        print(f"created OAuth client app {name} ({app['appId']})")
    else:
        graph("PATCH", f"/applications/{app['id']}",
              {"requiredResourceAccess": merge_access(app.get("requiredResourceAccess", []), needed)})
    client_sp = service_principal(app["appId"])
    ensure_grant(client_sp["id"], api_sp["id"], [SCOPE])
    ensure_grant(client_sp["id"], service_principal(MS_GRAPH_APP_ID)["id"], list(GRAPH_SCOPE_IDS))
    return app


def main(argv: list[str] | None = None) -> int:
    load_dotenv(ROOT / ".env")
    ap = argparse.ArgumentParser(description="Register the Entra apps for the sample server's AUTH_MODE=entra.")
    ap.add_argument("--roles", nargs="*", default=["NorthAmerica", "Europe"],
                    help="region roles you hold: All, NorthAmerica, Europe, APAC (default NorthAmerica Europe; "
                         "no values removes them all)")
    ap.add_argument("--oauth-client", action="store_true", help="also set up the OAuth2 passthrough client app")
    ap.add_argument("--trusted-client", nargs="+", default=[AZURE_CLI_APP_ID], metavar="APP_ID",
                    help="client apps consented tenant-wide for the API scope (default: Azure CLI)")
    ap.add_argument("--api-app-name", default="foundry-toolbox-eval-sample-api")
    ap.add_argument("--client-app-name", default="foundry-toolbox-eval-sample-oauth-client")
    ap.add_argument("--write-env", action="store_true", help="save the values to .env")
    ap.add_argument("--delete", action="store_true", help="delete both app registrations")
    args = ap.parse_args(argv)
    roles = role_values(args.roles)

    code, tenant, err = az("account", "show", "--query", "tenantId", "-o", "tsv")
    if code != 0 or not tenant:
        raise SystemExit(f"az login first: {err}")

    if args.delete:
        for key, name in (("ENTRA_OAUTH_CLIENT_APP_ID", args.client_app_name), ("ENTRA_API_APP_ID", args.api_app_name)):
            if app := find_app(os.environ.get(key), name, required=False):
                graph("DELETE", f"/applications/{app['id']}")
                print(f"deleted {app['displayName']} ({app['appId']}); remove {key} from .env")
        return 0

    code, user_id, err = az("ad", "signed-in-user", "show", "--query", "id", "-o", "tsv")
    if code != 0 or not user_id:
        raise SystemExit(f"cannot read the signed-in user: {err}")

    api_app, api_sp = setup_api(args.api_app_name)
    trust_clients(api_app, api_sp, args.trusted_client)
    sync_roles(api_app, api_sp, user_id, roles)
    values = {"ENTRA_TENANT_ID": tenant, "ENTRA_API_APP_ID": api_app["appId"]}
    print(f"API app      {api_app['displayName']}  scope api://{api_app['appId']}/{SCOPE}")
    print(f"your roles   {', '.join(roles) or '(none)'}")
    print(f"trusted      {', '.join(args.trusted_client)}")
    if args.oauth_client:
        client = setup_oauth_client(args.client_app_name, api_app, api_sp)
        values["ENTRA_OAUTH_CLIENT_APP_ID"] = client["appId"]
        print(f"OAuth client {client['displayName']}")
    print("\n" + "\n".join(f"{k}={v}" for k, v in values.items()))
    if args.write_env:
        env = ROOT / ".env"
        env.write_text(upsert_env(env.read_text(encoding="utf-8") if env.exists() else "", values), encoding="utf-8")
        print(f"saved to {env}")
    print("\nRole changes apply to new tokens only; Foundry and the Azure CLI cache tokens for up to about an hour.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

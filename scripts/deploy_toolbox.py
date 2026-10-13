"""Create the Foundry project connection and toolbox for an MCP server (idempotent).

Usage:
  python scripts/deploy_toolbox.py --mcp-url https://<host>/mcp --resource-group <rg> [--subscription <id>]
         [--connection sample-mcp-conn] [--server-label sample] [--toolbox sample-customer-toolbox]
         [--description "..."] [--delete]

Reads FOUNDRY_PROJECT_ENDPOINT from .env (account and project names are derived from it) and
uses your `az login`. Needs Contributor on the Foundry resource (connection) and Azure AI User
on the project (toolbox). Each run publishes a new toolbox version; the first version becomes the
default, later ones are promoted to default unless --no-promote is given.
--delete removes the toolbox and connection instead.
"""
import argparse
import json
import os
import subprocess
import tempfile
import time
from pathlib import Path
from urllib.parse import urlparse

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")
PROJECT_URL = os.environ.get("FOUNDRY_PROJECT_ENDPOINT", "")
if not PROJECT_URL:
    raise SystemExit("Set FOUNDRY_PROJECT_ENDPOINT in .env (see .env.example)")

ap = argparse.ArgumentParser()
ap.add_argument("--mcp-url", help="MCP server URL, e.g. https://<host>/mcp")
ap.add_argument("--resource-group", required=True, help="resource group of the Foundry resource")
ap.add_argument("--subscription", help="default: current az subscription")
ap.add_argument("--connection", default="sample-mcp-conn")
ap.add_argument("--server-label", default="sample")
ap.add_argument("--toolbox", default=os.environ.get("FOUNDRY_TOOLBOX_NAME", "sample-customer-toolbox"))
ap.add_argument("--description", default="Sample customer, market and stock tools for toolbox testing")
ap.add_argument("--no-promote", action="store_true")
ap.add_argument("--delete", action="store_true")
args = ap.parse_args()
if not args.delete and not args.mcp_url:
    ap.error("--mcp-url is required unless --delete")

parsed = urlparse(PROJECT_URL)
ACCOUNT = parsed.hostname.split(".")[0]
PROJECT = parsed.path.rstrip("/").split("/")[-1]
PROJECT_URL = PROJECT_URL.rstrip("/")


def az(*cmd):
    # shell=True so az.cmd resolves on Windows
    out = subprocess.run(["az", *cmd], capture_output=True, text=True, shell=True)
    return out.returncode, out.stdout.strip(), out.stderr.strip()


def rest(method, url, body=None, resource=None):
    cmd = ["rest", "--method", method, "--url", url]
    if resource:
        cmd += ["--resource", resource]
    path = None
    if body is not None:
        # A body file avoids shell quoting problems with az rest on Windows.
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump(body, f)
            path = f.name
        cmd += ["--headers", "Content-Type=application/json", "--body", f"@{path}"]
    try:
        for attempt in range(6):
            code, out, err = az(*cmd)
            # Local DNS/connection drops are transient; retry those only.
            if code == 0 or not any(s in err for s in ("NameResolutionError", "ConnectionError", "Connection aborted")):
                break
            time.sleep(10 * (attempt + 1))
    finally:
        if path:
            os.unlink(path)
    if code != 0:
        lines = [l for l in err.splitlines() if l.startswith("ERROR")]
        raise SystemExit(f"{method} {url} failed:\n" + "\n".join(lines[:3] or [err[:500]]))
    return json.loads(out) if out else {}


sub = args.subscription
if not sub:
    code, sub, err = az("account", "show", "--query", "id", "-o", "tsv")
    if code != 0 or not sub:
        raise SystemExit(f"az login first: {err}")

conn_url = (
    f"https://management.azure.com/subscriptions/{sub}/resourceGroups/{args.resource_group}"
    f"/providers/Microsoft.CognitiveServices/accounts/{ACCOUNT}/projects/{PROJECT}"
    f"/connections/{args.connection}?api-version=2025-06-01"
)
tb_url = f"{PROJECT_URL}/toolboxes/{args.toolbox}"

if args.delete:
    rest("DELETE", f"{tb_url}?api-version=v1", resource="https://ai.azure.com")
    print(f"deleted toolbox {args.toolbox}")
    rest("DELETE", conn_url)
    print(f"deleted connection {args.connection}")
    raise SystemExit(0)

rest("PUT", conn_url, {"properties": {
    "category": "RemoteTool",
    "target": args.mcp_url,
    "authType": "None",
    "metadata": {"type": "custom_MCP"},
}})
print(f"connection {args.connection} -> {args.mcp_url}")

version = rest("POST", f"{tb_url}/versions?api-version=v1", {
    "description": args.description,
    "tools": [{
        "type": "mcp",
        "server_label": args.server_label,
        "server_url": args.mcp_url,
        "require_approval": "never",
        "project_connection_id": args.connection,
    }],
}, resource="https://ai.azure.com")
ver = version.get("version")
print(f"toolbox {args.toolbox} version {ver} created")

current = rest("GET", f"{tb_url}?api-version=v1", resource="https://ai.azure.com")
if not args.no_promote and current.get("default_version") != ver:
    rest("PATCH", f"{tb_url}?api-version=v1", {"default_version": ver}, resource="https://ai.azure.com")
    print(f"default version -> {ver}")

print(f"consumer endpoint: {tb_url}/mcp?api-version=v1")

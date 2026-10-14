"""Create the Foundry prompt agent that uses the toolbox, or add a new version of it on later runs.

Usage: python scripts/deploy_agent.py [--model gpt-4-1-mini] [--dry-run]
Reads FOUNDRY_PROJECT_ENDPOINT, FOUNDRY_AGENT_NAME and FOUNDRY_TOOLBOX_NAME from .env and uses your
`az login` (needs Azure AI User on the project).
"""
import argparse
import json
import os
from pathlib import Path

from dotenv import load_dotenv

from _az import rest

load_dotenv(Path(__file__).resolve().parent.parent / ".env")
PROJECT = os.environ.get("FOUNDRY_PROJECT_ENDPOINT", "").rstrip("/")
if not PROJECT:
    raise SystemExit("Set FOUNDRY_PROJECT_ENDPOINT in .env (see .env.example)")
AGENT = os.environ.get("FOUNDRY_AGENT_NAME", "customer-assistant")
TOOLBOX = os.environ.get("FOUNDRY_TOOLBOX_NAME", "sample-customer-toolbox")
INSTRUCTIONS = (
    "You are a customer-intelligence assistant. Use the available tools to look up customer data, "
    "industry market conditions, and stock quotes. Never invent prices or customer facts; "
    "if a tool errors, say so plainly."
)

ap = argparse.ArgumentParser(description="Create a new version of the Foundry prompt agent connected to the toolbox.")
ap.add_argument("--model", default="gpt-4-1-mini", help="model deployment name (default: gpt-4-1-mini)")
ap.add_argument("--dry-run", action="store_true", help="print the request instead of sending it")
args = ap.parse_args()

url = f"{PROJECT}/agents/{AGENT}/versions?api-version=v1"
body = {"definition": {
    "kind": "prompt",
    "model": args.model,
    "instructions": INSTRUCTIONS,
    "tools": [{
        "type": "mcp",
        "server_label": "sample",
        "server_url": f"{PROJECT}/toolboxes/{TOOLBOX}/mcp?api-version=v1",
        "require_approval": "never",
    }],
}}

if args.dry_run:
    print(f"POST {url}\n{json.dumps(body, indent=2)}")
    raise SystemExit(0)

version = rest("POST", url, body, resource="https://ai.azure.com")
print(f"agent {AGENT} version {version.get('version')} created (model {args.model}, toolbox {TOOLBOX})")

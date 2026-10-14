"""Create the Foundry prompt agent that uses the toolbox, or add a new version of it on later runs.

Usage: python scripts/deploy_agent.py [--agent customer-assistant] [--toolbox sample-customer-toolbox]
                                     [--model gpt-4-1-mini] [--delete] [--dry-run]
Reads FOUNDRY_PROJECT_ENDPOINT from .env; --agent and --toolbox default to FOUNDRY_AGENT_NAME and
FOUNDRY_TOOLBOX_NAME. Uses your `az login` (needs Azure AI User on the project). --delete removes the
agent and all its versions.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from dotenv import load_dotenv

from _az import rest

ROOT = Path(__file__).resolve().parent.parent
INSTRUCTIONS = (
    "You are a customer-intelligence assistant. Use the available tools to look up customer data, "
    "industry market conditions, and stock quotes. Never invent prices or customer facts; "
    "if a tool errors, say so plainly. If a tool result has access_denied, tell the user they are not "
    "authorized to see that customer and do not guess its details."
)


def agent_body(project: str, toolbox: str, model: str) -> dict:
    return {"definition": {
        "kind": "prompt",
        "model": model,
        "instructions": INSTRUCTIONS,
        "tools": [{
            "type": "mcp",
            "server_label": "sample",
            "server_url": f"{project}/toolboxes/{toolbox}/mcp?api-version=v1",
            "require_approval": "never",
        }],
    }}


def main(argv: list[str] | None = None) -> int:
    load_dotenv(ROOT / ".env")
    project = os.environ.get("FOUNDRY_PROJECT_ENDPOINT", "").rstrip("/")
    if not project:
        raise SystemExit("Set FOUNDRY_PROJECT_ENDPOINT in .env (see .env.example)")

    ap = argparse.ArgumentParser(description="Create a new version of the Foundry prompt agent connected to the toolbox.")
    ap.add_argument("--agent", default=os.environ.get("FOUNDRY_AGENT_NAME", "customer-assistant"),
                    help="agent name (default: FOUNDRY_AGENT_NAME, else customer-assistant)")
    ap.add_argument("--toolbox", default=os.environ.get("FOUNDRY_TOOLBOX_NAME", "sample-customer-toolbox"),
                    help="toolbox the agent calls (default: FOUNDRY_TOOLBOX_NAME, else sample-customer-toolbox)")
    ap.add_argument("--model", default="gpt-4-1-mini", help="model deployment name (default: gpt-4-1-mini)")
    ap.add_argument("--delete", action="store_true", help="delete the agent (all versions) instead")
    ap.add_argument("--dry-run", action="store_true", help="print the request instead of sending it")
    args = ap.parse_args(argv)

    if args.delete:
        url = f"{project}/agents/{args.agent}?api-version=v1"
        if args.dry_run:
            print(f"DELETE {url}")
        else:
            rest("DELETE", url, resource="https://ai.azure.com")
            print(f"deleted agent {args.agent}")
        return 0

    url = f"{project}/agents/{args.agent}/versions?api-version=v1"
    body = agent_body(project, args.toolbox, args.model)
    if args.dry_run:
        print(f"POST {url}\n{json.dumps(body, indent=2)}")
        return 0

    version = rest("POST", url, body, resource="https://ai.azure.com")
    print(f"agent {args.agent} version {version.get('version')} created (model {args.model}, toolbox {args.toolbox})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

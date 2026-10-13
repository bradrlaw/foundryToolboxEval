"""Create/update the Foundry prompt agent that uses the sample toolbox (new agent version per run).

Usage: python scripts/deploy_agent.py [--model gpt-4-1-mini]
Requires `az login` against the tenant that owns the project.
"""
import argparse
import json
import os
import subprocess
import tempfile
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")
PROJECT = os.environ.get("FOUNDRY_PROJECT_ENDPOINT", "")
if not PROJECT:
    raise SystemExit("Set FOUNDRY_PROJECT_ENDPOINT in .env (see .env.example)")
AGENT = os.environ.get("FOUNDRY_AGENT_NAME", "customer-assistant")
TOOLBOX = os.environ.get("FOUNDRY_TOOLBOX_NAME", "sample-customer-toolbox")
INSTRUCTIONS = (
    "You are a customer-intelligence assistant. Use the available tools to look up customer data, "
    "industry market conditions, and stock quotes. Never invent prices or customer facts; "
    "if a tool errors, say so plainly."
)

ap = argparse.ArgumentParser()
ap.add_argument("--model", default="gpt-4-1-mini")
args = ap.parse_args()

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

# A body file avoids shell quoting problems with az rest on Windows.
with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
    json.dump(body, f)
try:
    out = subprocess.run(
        ["az", "rest", "--method", "post", "--resource", "https://ai.azure.com",
         "--url", f"{PROJECT}/agents/{AGENT}/versions?api-version=v1",
         "--headers", "Content-Type=application/json", "--body", f"@{f.name}"],
        capture_output=True, text=True, shell=True,
    )
finally:
    os.unlink(f.name)
print(out.stdout or out.stderr)

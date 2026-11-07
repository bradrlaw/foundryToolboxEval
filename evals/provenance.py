"""What a run ran against, saved in its result file so runs can be compared: the repo commit, the toolbox
versions, and for agent runs the agent version and its model.

Best effort: whatever can't be read is recorded as null, and a failed lookup never fails a run. Versions are read
only from the configured project (FOUNDRY_PROJECT_ENDPOINT), which already gets the runners' Foundry token, so a
lookup never sends that token to a host named in an agent definition.
"""
from __future__ import annotations

import json
import re
import subprocess
import urllib.request
from urllib.parse import quote, unquote

from evals import auth
from evals.cases import ROOT

TIMEOUT_S = 15
# A toolbox consumer endpoint, <project>/toolboxes/<name>/mcp. It serves the toolbox's default version.
TOOLBOX_ENDPOINT = re.compile(r"^(?P<project>https://[^?#]+)/toolboxes/(?P<name>[^/?#]+)/mcp/?(?:[?#]|$)")


def _git(*args: str) -> str | None:
    try:
        done = subprocess.run(["git", "--no-optional-locks", *args], cwd=ROOT, capture_output=True,
                              encoding="utf-8", errors="replace", timeout=10, check=True)
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout.strip()


def repo() -> dict:
    """{commit, dirty}: the checked-out commit, and whether the working tree differs from it."""
    commit = _git("rev-parse", "HEAD") or None
    status = _git("status", "--porcelain") if commit else None
    return {"commit": commit, "dirty": None if status is None else bool(status)}


def _get(url: str) -> dict | None:
    """A project resource as JSON, or None if it can't be read."""
    try:
        token = auth.get_token(auth.FOUNDRY_SCOPE)
        req = urllib.request.Request(f"{url}?api-version=v1", headers={"Authorization": f"Bearer {token}"})
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
            data = json.loads(resp.read())
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def _same(a: str, b: str | None) -> bool:
    return bool(b) and a.rstrip("/").lower() == b.rstrip("/").lower()


def toolbox(endpoint: str, project: str | None) -> dict | None:
    """{name, version} of the toolbox behind a consumer endpoint, or None if the URL isn't one.

    The version is the toolbox's default version, the one the endpoint serves. It is null unless the toolbox is
    in `project` and the lookup succeeds."""
    m = TOOLBOX_ENDPOINT.match(endpoint or "")
    if not m:
        return None
    found = _get(f"{m['project']}/toolboxes/{m['name']}") if _same(m["project"], project) else None
    version = (found or {}).get("default_version")
    return {"name": unquote(m["name"]), "version": None if version is None else str(version)}


def agent(project: str, name: str, version: str | None) -> dict:
    """{name, version, model, toolboxes} of an agent version, read from `project`. The toolboxes are those its
    MCP tools call; model and the toolbox versions are null when they can't be read."""
    definition: dict = {}
    if version:
        found = _get(f"{project.rstrip('/')}/agents/{quote(name, safe='')}/versions/{quote(str(version), safe='')}")
        if isinstance((found or {}).get("definition"), dict):
            definition = found["definition"]
    tools = definition.get("tools") if isinstance(definition.get("tools"), list) else []
    toolboxes = [tb for t in tools if isinstance(t, dict) and (tb := toolbox(str(t.get("server_url") or ""), project))]
    model = definition.get("model")
    return {"name": name, "version": version, "model": model if isinstance(model, str) else None,
            "toolboxes": toolboxes}

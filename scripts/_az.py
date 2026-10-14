"""Shared helpers for the deploy scripts: run the Azure CLI without a shell and call REST APIs through it."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import time
from urllib.parse import quote

# Worth retrying: local DNS or connection drops, throttling and gateway errors.
TRANSIENT = ("NameResolutionError", "ConnectionError", "Connection aborted", "Too Many Requests",
             "Bad Gateway", "Service Unavailable", "Gateway Timeout")
ATTEMPTS = 6
GRAPH = "https://graph.microsoft.com/v1.0"
# Graph is eventually consistent: a new app, service principal or role can be invisible for a few seconds.
GRAPH_REPLICATION = ("Request_ResourceNotFound", "does not reference a valid application object",
                     "does not exist or one of its queried reference-property objects are not present",
                     "Permission being assigned was not found")


def az(*args: str) -> tuple[int, str, str]:
    """Run `az` without a shell (resolving az.cmd on Windows). Returns (exit code, stdout, stderr), stripped."""
    exe = shutil.which("az")
    if not exe:
        raise SystemExit("Azure CLI (az) not found on PATH: https://aka.ms/install-azure-cli")
    out = subprocess.run([exe, *args], capture_output=True, text=True, encoding="utf-8", errors="replace",
                         env={**os.environ, "PYTHONIOENCODING": "utf-8"})
    return out.returncode, out.stdout.strip(), out.stderr.strip()


def rest(method: str, url: str, body: dict | None = None, resource: str | None = None,
         retry: tuple[str, ...] = ()) -> dict:
    """`az rest` with retries on transient errors, plus any error text in `retry`. Exits with the CLI's
    error lines if the call fails."""
    cmd = ["rest", "--method", method, "--url", url]
    if resource:
        cmd += ["--resource", resource]
    path = None
    if body is not None:
        # A body file avoids quoting JSON on the command line.
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as f:
            json.dump(body, f)
            path = f.name
        cmd += ["--headers", "Content-Type=application/json", "--body", f"@{path}"]
    try:
        for attempt in range(ATTEMPTS):
            code, out, err = az(*cmd)
            if code == 0 or attempt == ATTEMPTS - 1 or not any(s in err for s in TRANSIENT + retry):
                break
            time.sleep(10 * (attempt + 1))
    finally:
        if path:
            os.unlink(path)
    if code != 0:
        start = err.find("ERROR")  # skip CLI warnings; the error's JSON body spans several lines
        raise SystemExit(f"{method} {url} failed:\n" + err[max(start, 0):][:800])
    return json.loads(out) if out else {}


def graph(method: str, path: str, body: dict | None = None) -> dict:
    """Microsoft Graph v1.0 call as the signed-in user, retrying replication delays."""
    return rest(method, GRAPH + path, body, resource="https://graph.microsoft.com", retry=GRAPH_REPLICATION)


def where(collection: str, odata_filter: str) -> list[dict]:
    """Graph objects in a collection matching an OData filter, e.g. where("applications", "appId eq '...'")."""
    return graph("GET", f"/{collection}?$filter={quote(odata_filter)}").get("value", [])

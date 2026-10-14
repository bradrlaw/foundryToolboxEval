"""Shared helpers for the deploy scripts: run the Azure CLI without a shell and call REST APIs through it."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import time

# Worth retrying: local DNS or connection drops, throttling and gateway errors.
TRANSIENT = ("NameResolutionError", "ConnectionError", "Connection aborted", "Too Many Requests",
             "Bad Gateway", "Service Unavailable", "Gateway Timeout")
ATTEMPTS = 6


def az(*args: str) -> tuple[int, str, str]:
    """Run `az` without a shell (resolving az.cmd on Windows). Returns (exit code, stdout, stderr), stripped."""
    exe = shutil.which("az")
    if not exe:
        raise SystemExit("Azure CLI (az) not found on PATH: https://aka.ms/install-azure-cli")
    out = subprocess.run([exe, *args], capture_output=True, text=True, encoding="utf-8", errors="replace",
                         env={**os.environ, "PYTHONIOENCODING": "utf-8"})
    return out.returncode, out.stdout.strip(), out.stderr.strip()


def rest(method: str, url: str, body: dict | None = None, resource: str | None = None) -> dict:
    """`az rest` with retries on transient errors. Exits with the CLI's error lines if the call fails."""
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
            if code == 0 or attempt == ATTEMPTS - 1 or not any(s in err for s in TRANSIENT):
                break
            time.sleep(10 * (attempt + 1))
    finally:
        if path:
            os.unlink(path)
    if code != 0:
        start = err.find("ERROR")  # skip CLI warnings; the error's JSON body spans several lines
        raise SystemExit(f"{method} {url} failed:\n" + err[max(start, 0):][:800])
    return json.loads(out) if out else {}

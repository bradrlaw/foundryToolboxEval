"""Run an ASSERT config with its artifacts under results/assert in this repo.

Usage: python scripts/run_assert.py evals/configs/<config>.yaml [--history chain|replay] [assert-ai run options...]
Example: python scripts/run_assert.py evals/configs/ungrounded_figures.yaml --override run=nightly

ASSERT resolves a missing or relative artifacts_root inside site-packages, so this wrapper passes the
repo's absolute results/assert path as an override. It runs from the repo root so ASSERT loads .env.

--history sets how the target carries ASSERT's multi-turn conversations: chain (the default, or
$FOUNDRY_AGENT_HISTORY) continues each turn from the previous turn's stored response, replay resends the
conversation as text. ASSERT reuses a run's cached transcripts, so compare the two under different run names.

After a run that called the agent, the wrapper writes provenance.json into the run's directory,
results/assert/results/<suite>/<run>: what the run ran against (commit, agent version and model, toolbox
versions), its history mode, how many agent calls it made, how many of them continued a stored response, and their
tokens. A rerun that reuses cached transcripts calls no agent and leaves the file as it was.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
HISTORY_MODES = ("chain", "replay")
RUN_KEYS = ("suite", "run", "results_dir")


def take_history(args: list[str]) -> tuple[str | None, list[str]]:
    """The value of --history (or --history=VALUE), and the other arguments, which go to assert-ai."""
    history, rest, i = None, [], 0
    while i < len(args):
        if args[i] == "--history" and i + 1 < len(args):
            history, i = args[i + 1], i + 2
        elif args[i].startswith("--history="):
            history, i = args[i].split("=", 1)[1], i + 1
        else:
            rest.append(args[i])
            i += 1
    return history, rest


def overrides(args: list[str]) -> dict:
    """Values set with --override KEY=VALUE (or --override=KEY=VALUE), parsed as YAML as ASSERT does."""
    found, i = {}, 0
    while i < len(args):
        if args[i] == "--override" and i + 1 < len(args):
            item, i = args[i + 1], i + 2
        elif args[i].startswith("--override="):
            item, i = args[i].split("=", 1)[1], i + 1
        else:
            i += 1
            continue
        key, sep, value = item.partition("=")
        if sep:
            found[key.strip()] = yaml.safe_load(value)
    return found


def run_dir(config: dict, args: list[str]) -> Path | None:
    """The run's results directory, results/assert/results/<suite>/<run>, from the config and its overrides. None
    when the config sets no suite or run, or moves the results with results_dir."""
    values = {k: config.get(k) for k in RUN_KEYS}
    values.update({k: v for k, v in overrides(args).items() if k in RUN_KEYS})
    if values["results_dir"] or not values["suite"] or not values["run"]:
        return None
    return ROOT / "results" / "assert" / "results" / str(values["suite"]) / str(values["run"])


def _most_common(values) -> str | None:
    found = Counter(v for v in values if v)
    return found.most_common(1)[0][0] if found else None


def served_provenance(served: list[dict], project: str) -> dict:
    """provenance.json for a run's agent calls, from what the target logged for each (target.SERVED_LOG)."""
    from evals.run_agent_evals import run_provenance, total_usage

    name = _most_common(s.get("agent") for s in served)
    return {"agent": name, "timestamp": datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"),
            "history": _most_common(s.get("history") for s in served),
            "provenance": run_provenance(served, project, name),
            "requests": len(served), "continued": sum(bool(s.get("previous_response_id")) for s in served),
            "usage": total_usage(served)}


def _read_served(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def main(argv: list[str]) -> int:
    if not argv or argv[0].startswith("-"):
        print(__doc__.strip(), file=sys.stderr)
        return 0 if argv and argv[0] in ("-h", "--help") else 2
    config = Path(argv[0]).resolve()
    if not config.is_file():
        print(f"config not found: {config}", file=sys.stderr)
        return 2
    history, rest = take_history(argv[1:])
    if history is not None and history not in HISTORY_MODES:
        print(f"--history must be chain or replay, not {history!r}", file=sys.stderr)
        return 2
    exe = shutil.which("assert-ai", path=str(Path(sys.executable).parent)) or shutil.which("assert-ai")
    if not exe:
        print("assert-ai not found; run: pip install -r requirements.txt", file=sys.stderr)
        return 2
    raw = yaml.safe_load(config.read_text(encoding="utf-8")) or {}
    if not raw.get("run") and "run" not in overrides(rest):
        # Name the run as ASSERT would, so its directory is known.
        rest += ["--override", "run=" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")]
    # Override values are parsed as YAML; a quoted string keeps paths with spaces or '#' intact.
    artifacts = json.dumps((ROOT / "results" / "assert").as_posix())
    cmd = [exe, "run", "--config", str(config), "--override", f"artifacts_root={artifacts}", *rest]
    load_dotenv(ROOT / ".env")
    with tempfile.TemporaryDirectory() as tmp:
        log = Path(tmp) / "served.jsonl"
        env = {**os.environ, "FOUNDRY_EVAL_SERVED_LOG": str(log)}
        if history:
            env["FOUNDRY_AGENT_HISTORY"] = history
        code = subprocess.run(cmd, cwd=ROOT, env=env).returncode
        served = _read_served(log)
    where = run_dir(raw, rest)
    if served and where and where.is_dir():
        found = served_provenance(served, os.environ.get("FOUNDRY_PROJECT_ENDPOINT", ""))
        (where / "provenance.json").write_text(json.dumps(found, indent=2), encoding="utf-8")
        print(f"provenance: {where / 'provenance.json'}")
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

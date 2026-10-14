"""Run an ASSERT config with its artifacts under results/assert in this repo.

Usage: python scripts/run_assert.py evals/configs/<config>.yaml [assert-ai run options...]
Example: python scripts/run_assert.py evals/configs/ungrounded_figures.yaml --override run=nightly

ASSERT resolves a missing or relative artifacts_root inside site-packages, so this wrapper passes the
repo's absolute results/assert path as an override. It runs from the repo root so ASSERT loads .env.
"""
import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def main(argv: list[str]) -> int:
    if not argv or argv[0].startswith("-"):
        print(__doc__.strip(), file=sys.stderr)
        return 0 if argv and argv[0] in ("-h", "--help") else 2
    config = Path(argv[0]).resolve()
    if not config.is_file():
        print(f"config not found: {config}", file=sys.stderr)
        return 2
    exe = shutil.which("assert-ai", path=str(Path(sys.executable).parent)) or shutil.which("assert-ai")
    if not exe:
        print("assert-ai not found; run: pip install -r requirements.txt", file=sys.stderr)
        return 2
    # Override values are parsed as YAML; a quoted string keeps paths with spaces or '#' intact.
    artifacts = json.dumps((ROOT / "results" / "assert").as_posix())
    cmd = [exe, "run", "--config", str(config), "--override", f"artifacts_root={artifacts}", *argv[1:]]
    return subprocess.run(cmd, cwd=ROOT).returncode


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

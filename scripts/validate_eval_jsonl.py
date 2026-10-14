"""Validate tool-test and agent-eval JSONL files against schemas/ plus cross-field rules.

Usage: python scripts/validate_eval_jsonl.py [--kind tool|agent] FILE.jsonl [...]
The kind is inferred from the folder (data/tool-tests, data/agent-evals) or the first record.
"""
import argparse
import sys
from pathlib import Path

from evals.cases import SCHEMA_FILES, check_files, expand_paths


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--kind", choices=sorted(SCHEMA_FILES), help="override kind detection for every file")
    parser.add_argument("files", nargs="+", type=Path)
    args = parser.parse_args()
    cases, problems = check_files(expand_paths(args.files), args.kind)
    for p in problems:
        print(p)
    counts = ", ".join(f"{len(v)} {k}" for k, v in sorted(cases.items())) or "no cases"
    print(f"FAILED ({len(problems)} problems)" if problems else f"OK ({counts})")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())

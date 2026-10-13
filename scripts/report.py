"""Markdown reports for tool-test, agent-eval, and ASSERT runs, with optional baseline comparison.

Usage:
  python scripts/report.py tool   [RUN.json] [--baseline RUN.json]
  python scripts/report.py agent  [RUN.json] [--baseline RUN.json]
  python scripts/report.py assert RUN_DIR    [--baseline RUN_DIR]
With no RUN given, the newest file under results/tool-tests or results/agent-evals is used.
Add --out FILE to write the report instead of printing it.
"""
from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _id(r: dict) -> str:
    return r.get("case_id") or f"{r['conversation_id']}#{r['turn_number']}"


def _latency(r: dict) -> float | None:
    return r.get("latency_p50_ms", r.get("latency_ms"))


def by_category(results: list[dict]) -> dict[str, tuple[int, int]]:
    out: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for r in results:
        out[r["category"]][0] += r["passed"]
        out[r["category"]][1] += 1
    return {k: (v[0], v[1]) for k, v in sorted(out.items())}


def diff_cases(current: list[dict], baseline: list[dict], latency_tolerance: float = 0.25) -> dict[str, list]:
    base = {_id(r): r for r in baseline}
    cur = {_id(r): r for r in current}
    d: dict[str, list] = {"regressions": [], "fixes": [], "new": [], "removed": [], "slower": []}
    for cid, r in cur.items():
        b = base.get(cid)
        if b is None:
            d["new"].append(cid)
            continue
        if b["passed"] and not r["passed"]:
            d["regressions"].append(cid)
        elif not b["passed"] and r["passed"]:
            d["fixes"].append(cid)
        bl, cl = _latency(b), _latency(r)
        if bl and cl and cl > bl * (1 + latency_tolerance):
            d["slower"].append((cid, bl, cl))
    d["removed"] = [c for c in base if c not in cur]
    return d


def case_report(title: str, current: dict, baseline: dict | None) -> str:
    res = current["results"]
    passed = sum(r["passed"] for r in res)
    lines = [f"# {title}", "", f"Run `{current.get('timestamp', '?')}`: **{passed}/{len(res)} passed**", ""]
    lines += ["| Category | Passed | Total |", "| --- | ---: | ---: |"]
    lines += [f"| {c} | {p} | {t} |" for c, (p, t) in by_category(res).items()]
    lats = sorted(x for x in (_latency(r) for r in res) if x)
    if lats:
        lines += ["", f"Latency (per-case p50): median {lats[len(lats) // 2]:.0f} ms, max {lats[-1]:.0f} ms"]
    failed = [r for r in res if not r["passed"]]
    if failed:
        lines += ["", "## Failures", ""]
        for r in failed:
            lines.append(f"- `{_id(r)}`: {'; '.join(r['failures'])}")
    if baseline:
        d = diff_cases(res, baseline["results"])
        lines += ["", f"## Vs. baseline `{baseline.get('timestamp', '?')}`", ""]
        lines.append(f"- Regressions: {', '.join(f'`{c}`' for c in d['regressions']) or 'none'}")
        lines.append(f"- Fixed: {', '.join(f'`{c}`' for c in d['fixes']) or 'none'}")
        lines.append(f"- New cases: {len(d['new'])}; removed: {len(d['removed'])}")
        if d["slower"]:
            lines.append("- Slower (>25% p50): " + ", ".join(f"`{c}` {b:.0f}->{n:.0f} ms" for c, b, n in d["slower"]))
    return "\n".join(lines) + "\n"


def assert_rates(run_dir: Path) -> dict:
    """Violation rate per dimension and case type from an ASSERT run's scores.jsonl."""
    counts: dict[tuple[str, str], list[int]] = defaultdict(lambda: [0, 0])
    status: dict[str, int] = defaultdict(int)
    for line in (run_dir / "scores.jsonl").read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        r = json.loads(line)
        status[r.get("judge_status", "?")] += 1
        dims = (r.get("verdict") or {}).get("dimensions")
        if r.get("judge_status") != "ok" or not dims:
            continue
        for dim, violated in dims.items():
            counts[(r["type"], dim)][0] += bool(violated)
            counts[(r["type"], dim)][1] += 1
    return {"rates": {k: tuple(v) for k, v in sorted(counts.items())}, "status": dict(status)}


def assert_report(run_dir: Path, baseline_dir: Path | None) -> str:
    cur = assert_rates(run_dir)
    base = assert_rates(baseline_dir) if baseline_dir else None
    lines = [f"# ASSERT: {run_dir.parent.name} / {run_dir.name}", "",
             "Judge status: " + ", ".join(f"{k}={v}" for k, v in sorted(cur["status"].items())), "",
             "| Case type | Dimension | Violated | Scored | Rate |" + (" Baseline rate |" if base else ""),
             "| --- | --- | ---: | ---: | ---: |" + (" ---: |" if base else "")]
    for (ctype, dim), (v, n) in cur["rates"].items():
        row = f"| {ctype} | {dim} | {v} | {n} | {v / n:.0%} |"
        if base:
            bv, bn = base["rates"].get((ctype, dim), (0, 0))
            row += f" {bv / bn:.0%} ({bv}/{bn}) |" if bn else " n/a |"
        lines.append(row)
    return "\n".join(lines) + "\n"


def newest(folder: Path) -> Path:
    files = sorted(folder.glob("run-*.json"))
    if not files:
        raise SystemExit(f"no runs found in {folder}")
    return files[-1]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("kind", choices=["tool", "agent", "assert"])
    ap.add_argument("run", nargs="?", type=Path)
    ap.add_argument("--baseline", type=Path)
    ap.add_argument("--out", type=Path)
    args = ap.parse_args()

    if args.kind == "assert":
        if not args.run:
            raise SystemExit("assert needs a run directory (results/assert/results/<suite>/<run>)")
        text = assert_report(args.run, args.baseline)
    else:
        folder = ROOT / "results" / ("tool-tests" if args.kind == "tool" else "agent-evals")
        run = args.run or newest(folder)
        current = json.loads(run.read_text(encoding="utf-8"))
        baseline = json.loads(args.baseline.read_text(encoding="utf-8")) if args.baseline else None
        text = case_report("Tool tests" if args.kind == "tool" else "Agent evals", current, baseline)

    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text, encoding="utf-8")
    else:
        print(text)


if __name__ == "__main__":
    main()

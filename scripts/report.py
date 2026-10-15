"""Markdown reports for tool-test, agent-eval, and ASSERT runs, with optional baseline comparison.

Tool and agent reports say why cases failed: failures grouped by the reason code the runners record with each
failure (evals/reasons.py). ASSERT reports list each violation with the judge's justification, and the cases
that weren't scored with the target's error.

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
from collections import Counter, defaultdict
from pathlib import Path

from evals.reasons import AGENT_REASONS, TOOL_REASONS

ROOT = Path(__file__).resolve().parent.parent
UNRECORDED = "unrecorded"
UNRECORDED_MEANING = "The result file predates failure reasons; see the messages."
TARGET_ERROR = "[TARGET ERROR:"
MAX_TEXT = 300


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


def _clip(text: str, limit: int = MAX_TEXT) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 3] + "..."


def failure_reasons(r: dict) -> list[tuple[str, str]]:
    """(reason, message) for each of a result's failures."""
    reasons = r.get("reasons") or []
    pairs = [(reasons[i] if i < len(reasons) else UNRECORDED, m) for i, m in enumerate(r.get("failures") or [])]
    return pairs or ([] if r["passed"] else [(UNRECORDED, "no failure message recorded")])


def _case_count(items: list[tuple[dict, str]]) -> int:
    return len({_id(r) for r, _ in items})


def by_reason(results: list[dict]) -> dict[str, list[tuple[dict, str]]]:
    """Failures of failed cases grouped by reason, the reason that fails the most cases first."""
    groups: dict[str, list[tuple[dict, str]]] = defaultdict(list)
    for r in results:
        if not r["passed"]:
            for reason, message in failure_reasons(r):
                groups[reason].append((r, message))
    return dict(sorted(groups.items(), key=lambda kv: (-_case_count(kv[1]), kv[0])))


def _flags(r: dict, reason: str) -> str:
    flags = []
    if r.get("platform_abort") and r.get("abort_reason") != reason:
        flags.append(f"platform abort: {r['abort_reason']}" if r.get("abort_reason") else "platform abort")
    if r.get("history_gap"):
        flags.append("history gap")
    return "".join(f" [{f}]" for f in flags)


def why_failed(results: list[dict], meanings: dict[str, str]) -> list[str]:
    """A reason summary table, then each failure under its reason."""
    groups = by_reason(results)
    if not groups:
        return []
    meanings = {**meanings, UNRECORDED: UNRECORDED_MEANING}
    lines = ["", "## Why cases failed", "", "| Reason | Failed cases | Meaning |", "| --- | ---: | --- |"]
    lines += [f"| `{k}` | {_case_count(v)} | {meanings.get(k, '')} |" for k, v in groups.items()]
    lines += ["", "## Failures by reason"]
    for reason, items in groups.items():
        lines += ["", f"### `{reason}`", ""]
        for r, message in items:
            tool = f" {r['tool']}" if r.get("tool") else ""
            lines.append(f"- `{_id(r)}`{tool}{_flags(r, reason)}: {message}")
    return lines


def case_report(title: str, current: dict, baseline: dict | None, meanings: dict[str, str] | None = None) -> str:
    res = current["results"]
    passed = sum(r["passed"] for r in res)
    lines = [f"# {title}", "", f"Run `{current.get('timestamp', '?')}`: **{passed}/{len(res)} passed**", ""]
    lines += ["| Category | Passed | Total |", "| --- | ---: | ---: |"]
    lines += [f"| {c} | {p} | {t} |" for c, (p, t) in by_category(res).items()]
    lats = sorted(x for x in (_latency(r) for r in res) if x)
    if lats:
        lines += ["", f"Latency (per-case p50): median {lats[len(lats) // 2]:.0f} ms, max {lats[-1]:.0f} ms"]
    aborts = [r for r in res if r.get("platform_abort")]
    gaps = sum(bool(r.get("history_gap")) for r in res)
    if aborts or gaps:
        why = Counter(r["abort_reason"] for r in aborts if r.get("abort_reason"))
        detail = f" ({', '.join(f'{k} {n}' for k, n in why.most_common())})" if why else ""
        lines += ["", f"Platform aborts: {len(aborts)}{detail}. Turns with a history gap: {gaps}."]
    lines += why_failed(res, meanings or {})
    if baseline:
        d = diff_cases(res, baseline["results"])
        cur = {_id(r): r for r in res}
        regressions = [f"`{c}` ({', '.join(dict.fromkeys(k for k, _ in failure_reasons(cur[c])))})"
                       for c in d["regressions"]]
        lines += ["", f"## Vs. baseline `{baseline.get('timestamp', '?')}`", ""]
        lines.append(f"- Regressions: {', '.join(regressions) or 'none'}")
        lines.append(f"- Fixed: {', '.join(f'`{c}`' for c in d['fixes']) or 'none'}")
        lines.append(f"- New cases: {len(d['new'])}; removed: {len(d['removed'])}")
        if d["slower"]:
            lines.append("- Slower (>25% p50): " + ", ".join(f"`{c}` {b:.0f}->{n:.0f} ms" for c, b, n in d["slower"]))
    return "\n".join(lines) + "\n"


def _jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def assert_rates(run_dir: Path) -> dict:
    """Violation rate per dimension and case type from an ASSERT run's scores.jsonl."""
    counts: dict[tuple[str, str], list[int]] = defaultdict(lambda: [0, 0])
    status: dict[str, int] = defaultdict(int)
    for r in _jsonl(run_dir / "scores.jsonl"):
        status[r.get("judge_status", "?")] += 1
        dims = (r.get("verdict") or {}).get("dimensions")
        if r.get("judge_status") != "ok" or not dims:
            continue
        for dim, violated in dims.items():
            counts[(r["type"], dim)][0] += bool(violated)
            counts[(r["type"], dim)][1] += 1
    return {"rates": {k: tuple(v) for k, v in sorted(counts.items())}, "status": dict(status)}


def assert_cases(run_dir: Path) -> tuple[list[dict], list[dict]]:
    """Scored cases with a violated dimension, and cases the judge didn't score."""
    violations, unscored = [], []
    for r in sorted(_jsonl(run_dir / "scores.jsonl"), key=lambda r: (str(r.get("test_case_id")), str(r.get("type")))):
        if r.get("judge_status") != "ok":
            unscored.append(r)
        elif any(((r.get("verdict") or {}).get("dimensions") or {}).values()):
            violations.append(r)
    return violations, unscored


def _readable_error(text: str) -> str:
    """'Responses API 400: {json}' as 'Responses API 400 (code): message' when the body parses."""
    head, sep, body = text.partition(": {")
    try:
        error = json.loads("{" + body)["error"] if sep else None
    except (ValueError, KeyError, TypeError):
        return text
    if isinstance(error, dict) and error.get("message"):
        return f"{head}{' (' + str(error['code']) + ')' if error.get('code') else ''}: {error['message']}"
    return text


def target_errors(run_dir: Path) -> dict[tuple[str, str], str]:
    """The target's error, without ASSERT's traceback, for each transcript that stopped with target_error."""
    path = run_dir / "inference_set.jsonl"
    found = {}
    for rec in _jsonl(path) if path.exists() else []:
        if rec.get("stop_reason") != "target_error":
            continue
        for event in rec.get("events") or []:
            content = ((event.get("edit") or {}).get("message") or {}).get("content")
            if isinstance(content, str) and content.startswith(TARGET_ERROR):
                text = content[len(TARGET_ERROR):].split("\nTraceback (most recent call last):")[0]
                found[(rec.get("type"), rec.get("test_case_id"))] = _readable_error(text.strip().removesuffix("]"))
    return found


def _assert_case(r: dict) -> str:
    design = dict(r.get("dimensions") or {})
    detail = "; ".join(x for x in (design.pop("behavior", ""), ", ".join(f"{k}={v}" for k, v in design.items())) if x)
    return f"`{r.get('test_case_id', '?')}` ({r.get('type', '?')}{': ' + detail if detail else ''})"


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
    violations, unscored = assert_cases(run_dir)
    if violations:
        lines += ["", "## Violations", ""]
        for r in violations:
            verdict = r["verdict"]
            why = verdict.get("dimension_justifications") or {}
            lines.append(f"- {_assert_case(r)}")
            lines += [f"  - {dim}: {_clip(why.get(dim) or verdict.get('justification') or 'no justification')}"
                      for dim, violated in verdict["dimensions"].items() if violated]
    if unscored:
        errors = target_errors(run_dir)
        lines += ["", "## Not scored", ""]
        for r in unscored:
            line = f"- {_assert_case(r)}: {r.get('judge_error') or r.get('judge_status', '?')}"
            if error := errors.get((r.get("type"), r.get("test_case_id"))):
                line += f". Target error: {_clip(error)}"
            lines.append(line)
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
        if args.kind == "tool":
            text = case_report("Tool tests", current, baseline, TOOL_REASONS)
        else:
            text = case_report("Agent evals", current, baseline, AGENT_REASONS)

    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text, encoding="utf-8")
    else:
        print(text)


if __name__ == "__main__":
    main()

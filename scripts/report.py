"""Markdown reports for tool-test, agent-eval, and ASSERT runs, with optional baseline comparison.

Tool and agent reports say why cases failed: failures grouped by the reason code the runners record with each
failure (evals/reasons.py). They also say what the run ran against (agent version and model, toolbox versions,
commit) and, against a baseline, what of that changed. Agent runs repeated with --repetitions count a turn as passed
only if every run passed, and add pass@1, pass@k and pass^k and the flaky turns. Agent reports also count the tool
calls beyond those each turn expects. ASSERT reports list each violation with the judge's justification, and the
cases that weren't scored with the target's error.

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
import statistics
from collections import Counter, defaultdict
from pathlib import Path

from evals.reasons import AGENT_REASONS, TOOL_REASONS
from evals.results import by_case, extra_calls_by_case, pass_rate_text, pass_rates, result_id

ROOT = Path(__file__).resolve().parent.parent
UNRECORDED = "unrecorded"
UNRECORDED_MEANING = "The result file predates failure reasons; see the messages."
TARGET_ERROR = "[TARGET ERROR:"
MAX_TEXT = 300


def _latency(r: dict) -> float | None:
    return r.get("latency_p50_ms", r.get("latency_ms"))


def per_case(results: list[dict]) -> list[dict]:
    """One result per case. A case that ran several times passes only if every run passed; it keeps each distinct
    failure once, and its latency is the median over its runs. A case that ran once keeps its result."""
    out = []
    for runs in by_case(results).values():
        if len(runs) == 1:
            out.append(runs[0])
            continue
        pairs = list(dict.fromkeys(pair for r in runs for pair in failure_reasons(r)))
        lats = [x for x in map(_latency, runs) if x is not None]
        out.append({**runs[0], "passed": all(r["passed"] for r in runs), "failures": [m for _, m in pairs],
                    "reasons": [k for k, _ in pairs],
                    "latency_p50_ms": round(statistics.median(lats), 1) if lats else None})
    return out


def by_category(results: list[dict]) -> dict[str, tuple[int, int]]:
    out: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for r in results:
        out[r["category"]][0] += r["passed"]
        out[r["category"]][1] += 1
    return {k: (v[0], v[1]) for k, v in sorted(out.items())}


def diff_cases(current: list[dict], baseline: list[dict], latency_tolerance: float = 0.25) -> dict[str, list]:
    base = {result_id(r): r for r in baseline}
    cur = {result_id(r): r for r in current}
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
    return len({result_id(r) for r, _ in items})


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
    if r.get("platform_abort") and reason not in (r.get("abort_reason"), "platform_abort"):
        flags.append(f"platform abort: {r['abort_reason']}" if r.get("abort_reason") else "platform abort")
    if r.get("history_gap"):
        flags.append("history gap")
    return "".join(f" [{f}]" for f in flags)


def why_failed(results: list[dict], meanings: dict[str, str]) -> list[str]:
    """A reason summary table, then each failure under its reason. A failure that recurs over a case's runs is
    listed once, with the number of runs it occurred in."""
    groups = by_reason(results)
    if not groups:
        return []
    runs = Counter(result_id(r) for r in results)
    meanings = {**meanings, UNRECORDED: UNRECORDED_MEANING}
    lines = ["", "## Why cases failed", "", "| Reason | Failed cases | Meaning |", "| --- | ---: | --- |"]
    lines += [f"| `{k}` | {_case_count(v)} | {meanings.get(k, '')} |" for k, v in groups.items()]
    lines += ["", "## Failures by reason"]
    for reason, items in groups.items():
        lines += ["", f"### `{reason}`", ""]
        nth: Counter = Counter()  # the same message twice in one result stays two lines
        merged: dict[tuple, list[dict]] = {}
        for r, message in items:
            nth[(id(r), message)] += 1
            merged.setdefault((result_id(r), _flags(r, reason), message, nth[(id(r), message)]), []).append(r)
        for (cid, flags, message, _), found in merged.items():
            tool = f" {found[0]['tool']}" if found[0].get("tool") else ""
            share = f" [{len(found)}/{runs[cid]} runs]" if runs[cid] > 1 else ""
            lines.append(f"- `{cid}`{tool}{flags}{share}: {message}")
    return lines


def _version(item: dict) -> str | None:
    return None if item.get("version") is None else f"v{item['version']}"


def ran_against(run: dict) -> dict[str, str | None]:
    """What a run ran against, from its provenance: {component: version}, None where the version is unknown.
    Empty for runs recorded before provenance existed."""
    p = run.get("provenance") or {}
    out: dict[str, str | None] = {}
    agent = p.get("agent") or {}
    if agent.get("name"):
        out[f"agent `{agent['name']}`"] = _version(agent)
        if agent.get("model"):
            out["model"] = f"`{agent['model']}`"
    for tb in p.get("toolboxes") or []:
        out[f"toolbox `{tb['name']}`"] = _version(tb)
    repo = p.get("repo") or {}
    if repo.get("commit"):
        out["commit"] = f"`{repo['commit'][:7]}`" + (" with uncommitted changes" if repo.get("dirty") else "")
    return out


def _known(value: str | None) -> str:
    return value or "(version unknown)"


def provenance_changes(current: dict, baseline: dict) -> list[str]:
    """What the run ran against that differs from the baseline. An unknown version is not counted as a change."""
    cur, base = ran_against(current), ran_against(baseline)
    out = []
    for k in [*cur, *(k for k in base if k not in cur)]:
        if k not in base:
            out.append(f"{k} {_known(cur[k])} (not in the baseline)")
        elif k not in cur:
            out.append(f"{k} {_known(base[k])} (only in the baseline)")
        elif cur[k] and base[k] and cur[k] != base[k]:
            out.append(f"{k} {base[k]} -> {cur[k]}")
    return out


def case_report(title: str, current: dict, baseline: dict | None, meanings: dict[str, str] | None = None) -> str:
    res = current["results"]
    cases = per_case(res)
    passed = sum(r["passed"] for r in cases)
    rates = pass_rates(res)
    run = f"Run `{current.get('timestamp', '?')}`"
    if rates:
        flaky = ", ".join(f"`{c}` {p}/{n}" for c, p, n in rates["flaky"]) or "none"
        lines = [f"# {title}", "", f"{run}, {rates['k']} runs of each case: **{passed}/{len(cases)} passed every run**",
                 "", f"{pass_rate_text(rates)} ({sum(r['passed'] for r in res)}/{len(res)} runs passed). "
                     f"Flaky (passed some runs, not all): {flaky}.", ""]
    else:
        lines = [f"# {title}", "", f"{run}: **{passed}/{len(cases)} passed**", ""]
    if ran := ran_against(current):
        lines += ["Ran against " + ", ".join(f"{k} {_known(v)}" for k, v in ran.items()) + ".", ""]
    lines += ["| Category | Passed | Total |", "| --- | ---: | ---: |"]
    lines += [f"| {c} | {p} | {t} |" for c, (p, t) in by_category(cases).items()]
    lats = sorted(x for x in (_latency(r) for r in cases) if x)
    if lats:
        lines += ["", f"Latency (per-case p50): median {lats[len(lats) // 2]:.0f} ms, max {lats[-1]:.0f} ms"]
    if usage := current.get("usage"):
        lines += ["", f"Tokens: {usage.get('total_tokens') or 0:,} ({usage.get('input_tokens') or 0:,} input, "
                      f"{usage.get('output_tokens') or 0:,} output)"]
    if any(r.get("extra_calls") is not None for r in res):
        extra = extra_calls_by_case(res)
        detail = f"{sum(extra.values())} ({', '.join(f'`{c}` {n}' for c, n in extra.items())})" if extra else "none"
        lines += ["", f"Extra tool calls (beyond those expected{', over all runs' if rates else ''}): {detail}"]
    aborts = [r for r in res if r.get("platform_abort")]
    gaps = sum(bool(r.get("history_gap")) for r in res)
    if aborts or gaps:
        why = Counter(r["abort_reason"] for r in aborts if r.get("abort_reason"))
        detail = f" ({', '.join(f'{k} {n}' for k, n in why.most_common())})" if why else ""
        lines += ["", f"Platform aborts: {len(aborts)}{detail}. Turns with a history gap: {gaps}."]
    lines += why_failed(res, meanings or {})
    if baseline:
        d = diff_cases(cases, per_case(baseline["results"]))
        cur = {result_id(r): r for r in cases}
        regressions = [f"`{c}` ({', '.join(dict.fromkeys(k for k, _ in failure_reasons(cur[c])))})"
                       for c in d["regressions"]]
        lines += ["", f"## Vs. baseline `{baseline.get('timestamp', '?')}`", ""]
        if ran_against(current) and ran_against(baseline):
            lines.append(f"- Changed since the baseline: {'; '.join(provenance_changes(current, baseline)) or 'none'}")
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

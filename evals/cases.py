"""Load and validate tool-test and agent-eval JSONL files against the schemas in schemas/.

The JSON Schemas carry the structural rules. Cross-field rules they cannot express clearly are
checked here: fields a case's kind or pass_criteria ignores, duplicate ids, turn-number gaps, the
regex syntax of `re:` params, and unknown {{caller.<field>}} placeholders.

Any string in a case may use {{caller.oid}}, {{caller.tid}}, {{caller.name}} or {{caller.username}}; the
runners replace them with the signed-in user's values (expand_caller) so per-user cases stay portable.
"""
from __future__ import annotations

import glob
import json
import re
from collections import defaultdict
from functools import cache
from pathlib import Path
from typing import Callable, Iterable

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError, ValidationError
from jsonschema.validators import validator_for

ROOT = Path(__file__).resolve().parent.parent
SCHEMA_FILES = {"tool": "tool-test.schema.json", "agent": "agent-eval.schema.json"}
KIND_FOLDERS = {"tool-tests": "tool", "agent-evals": "agent"}

COMMON_TOOL_FIELDS = {"case_id", "kind", "category", "notes"}
TOOL_KIND_FIELDS = {
    "call": {"tool", "arguments", "expect_error", "assertions", "repetitions", "max_latency_ms"},
    "list": {"expect_tools", "require_schemas", "max_latency_ms"},
    "auth": {"token", "expect_status"},
}
ASSERTION_FIELDS = {
    "contains": {"value"},
    "regex": {"pattern"},
    "json_path_equals": {"path", "value"},
    "json_path_exists": {"path"},
    "schema": {"schema"},
}
CALLER_FIELDS = ("oid", "tid", "name", "username")
_CALLER = re.compile(r"\{\{\s*caller\.(\w+)\s*\}\}")
_AGENT_REGEX_FIELDS = ("expected_response_pattern", "response_must_not_match")


class CaseError(ValueError):
    """Invalid case files; `problems` holds one `file:line: message` entry per problem."""

    def __init__(self, problems: list[str]):
        super().__init__("\n".join(problems))
        self.problems = problems


def format_error(error: ValidationError) -> str:
    return f"{error.json_path}: {error.message}"


def schema_validator(schema: dict) -> Draft202012Validator:
    """Validator for a user-supplied schema (draft 2020-12 unless it declares $schema)."""
    cls = validator_for(schema, default=Draft202012Validator)
    return cls(schema, format_checker=cls.FORMAT_CHECKER)


@cache
def _case_validator(kind: str) -> Draft202012Validator:
    schema = json.loads((ROOT / "schemas" / SCHEMA_FILES[kind]).read_text(encoding="utf-8"))
    validator_for(schema).check_schema(schema)
    return schema_validator(schema)


def detect_kind(path: Path, first: object = None) -> str | None:
    """'tool' or 'agent' from the folder name (data/tool-tests, data/agent-evals) or the first record."""
    for part in reversed(path.resolve().parent.parts):
        if part in KIND_FOLDERS:
            return KIND_FOLDERS[part]
    if isinstance(first, dict):
        if "conversation_id" in first:
            return "agent"
        if "case_id" in first:
            return "tool"
    return None


def _regex_problem(pattern: str, where: str) -> list[str]:
    try:
        re.compile(pattern)
        return []
    except re.error as e:
        return [f"{where}: invalid regex {pattern!r}: {e}"]


def _tool_problems(case: dict) -> list[str]:
    kind = case.get("kind", "call")
    problems = [f"$.{k}: not used by kind {kind!r}"
                for k in sorted(set(case) - COMMON_TOOL_FIELDS - TOOL_KIND_FIELDS[kind])]
    for i, a in enumerate(case.get("assertions", [])):
        problems += [f"$.assertions[{i}].{k}: not used by assertion type {a['type']!r}"
                     for k in sorted(set(a) - {"type"} - ASSERTION_FIELDS[a["type"]])]
        if a["type"] == "schema":
            try:
                validator_for(a["schema"], default=Draft202012Validator).check_schema(a["schema"])
            except SchemaError as e:
                problems.append(f"$.assertions[{i}].schema: invalid JSON Schema: {e.message}")
    return problems


def _agent_problems(case: dict) -> list[str]:
    problems = []
    criteria = case["pass_criteria"]
    if criteria == "tool_called_correctly" and case.get("expected_response_pattern"):
        problems.append("$.expected_response_pattern: ignored unless pass_criteria is response_matches or both")
    if criteria == "response_matches":
        problems += [f"$.{k}: ignored when pass_criteria is response_matches"
                     for k in ("expected_tool_calls", "ordered") if case.get(k)]
    expected = {c["tool"] for c in case["expected_tool_calls"]}
    problems += [f"$.must_not_call: {name!r} is also an expected call"
                 for name in sorted(expected & set(case.get("must_not_call", [])))]
    for i, call in enumerate(case["expected_tool_calls"]):
        for key, value in call["params"].items():
            if isinstance(value, str) and value.startswith("re:"):
                problems += _regex_problem(value[3:], f"$.expected_tool_calls[{i}].params.{key}")
    return problems


def _strings(value: object, path: str = "$"):
    """(JSON path, text) for every string in a case."""
    if isinstance(value, str):
        yield path, value
    elif isinstance(value, dict):
        for k, v in value.items():
            yield from _strings(v, f"{path}.{k}")
    elif isinstance(value, list):
        for i, v in enumerate(value):
            yield from _strings(v, f"{path}[{i}]")


def _placeholder_problems(case: dict) -> list[str]:
    return [f"{path}: unknown placeholder {m[0]}; caller fields are {', '.join(CALLER_FIELDS)}"
            for path, text in _strings(case) for m in _CALLER.finditer(text) if m[1] not in CALLER_FIELDS]


def caller_fields(cases: Iterable[dict]) -> set[str]:
    """Fields named by {{caller.<field>}} placeholders in the cases."""
    return {m[1] for case in cases for _, text in _strings(case) for m in _CALLER.finditer(text)}


def _fill(text: str, caller: dict[str, str], regex: bool = False) -> str:
    return _CALLER.sub(lambda m: re.escape(caller[m[1]]) if regex else caller[m[1]], text)


def _fill_all(value: object, caller: dict[str, str]) -> object:
    if isinstance(value, str):
        return _fill(value, caller)
    if isinstance(value, dict):
        return {k: _fill_all(v, caller) for k, v in value.items()}
    if isinstance(value, list):
        return [_fill_all(v, caller) for v in value]
    return value


def expand_caller(cases: list[dict], caller: dict[str, str]) -> list[dict]:
    """Copies of `cases` with {{caller.<field>}} replaced by `caller` values, regex-escaped in regex fields
    (regex assertion patterns, expected_response_pattern, response_must_not_match, re: params).

    Raises KeyError for a field `caller` lacks; check caller_fields first.
    """
    out = []
    for case in cases:
        new = _fill_all(case, caller)
        for key in _AGENT_REGEX_FIELDS:
            if isinstance(case.get(key), str):
                new[key] = _fill(case[key], caller, regex=True)
        for call, new_call in zip(case.get("expected_tool_calls", []), new.get("expected_tool_calls", [])):
            for key, value in call.get("params", {}).items():
                if isinstance(value, str) and value.startswith("re:"):
                    new_call["params"][key] = _fill(value, caller, regex=True)
        for a, new_a in zip(case.get("assertions", []), new.get("assertions", [])):
            if a.get("type") == "regex" and isinstance(a.get("pattern"), str):
                new_a["pattern"] = _fill(a["pattern"], caller, regex=True)
        out.append(new)
    return out


def fill_caller(cases: list[dict], get_caller: Callable[[], dict[str, str]]) -> list[dict]:
    """expand_caller with the signed-in user's claims, fetched only when some case has a placeholder.

    Raises CaseError naming each placeholder the claims cannot fill.
    """
    fields = caller_fields(cases)
    if not fields:
        return cases
    caller = get_caller()
    missing = sorted(fields - caller.keys())
    if missing:
        raise CaseError([f"{{{{caller.{f}}}}}: no matching claim in the signed-in user's token" for f in missing])
    return expand_caller(cases, caller)


def _identity(kind: str, case: object) -> tuple[str, object] | None:
    """(label, key) for duplicate and turn-order checks; None if the identity fields are malformed.

    Schema-invalid records still count here so one bad field doesn't cause bogus follow-on errors.
    """
    if not isinstance(case, dict):
        return None
    if kind == "tool":
        case_id = case.get("case_id")
        return ("case_id", case_id) if isinstance(case_id, str) else None
    conv, turn = case.get("conversation_id"), case.get("turn_number")
    if isinstance(conv, str) and type(turn) is int and turn >= 1:
        return ("conversation turn", (conv, turn))
    return None


def _read_jsonl(path: Path) -> tuple[list[tuple[int, object]], list[str]]:
    rows, problems = [], []
    for n, line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
        if not line.strip():
            continue
        try:
            rows.append((n, json.loads(line)))
        except json.JSONDecodeError as e:
            problems.append(f"{path}:{n}: invalid JSON: {e}")
    return rows, problems


def check_files(paths: Iterable[Path | str], kind: str | None = None) -> tuple[dict[str, list[dict]], list[str]]:
    """Validate JSONL files. Returns the valid cases grouped by kind, and every problem found.

    `kind` forces 'tool' or 'agent' for every file; otherwise each file's kind is detected.
    Duplicate ids and turn gaps are checked across all files of the same kind.
    """
    cases: dict[str, list[dict]] = defaultdict(list)
    problems: list[str] = []
    seen: dict[str, dict[object, str]] = defaultdict(dict)
    turns: dict[str, list[tuple[int, str]]] = defaultdict(list)
    for path in map(Path, paths):
        if not path.is_file():
            problems.append(f"{path}: file not found")
            continue
        rows, file_problems = _read_jsonl(path)
        problems += file_problems
        file_kind = kind or detect_kind(path, rows[0][1] if rows else None)
        if file_kind is None:
            problems.append(f"{path}: cannot tell tool tests from agent evals; pass --kind")
            continue
        validator = _case_validator(file_kind)
        for n, case in rows:
            where = f"{path}:{n}"
            errors = sorted(validator.iter_errors(case), key=lambda e: e.json_path)
            problems += [f"{where}: {format_error(e)}" for e in errors]
            if not errors:
                kind_problems = (_tool_problems if file_kind == "tool" else _agent_problems)(case)
                problems += [f"{where}: {p}" for p in kind_problems + _placeholder_problems(case)]
                cases[file_kind].append(case)
            identity = _identity(file_kind, case)
            if identity is None:
                continue
            label, key = identity
            if key in seen[file_kind]:
                problems.append(f"{where}: duplicate {label} {key!r} (first at {seen[file_kind][key]})")
                continue
            seen[file_kind][key] = where
            if file_kind == "agent":
                turns[key[0]].append((key[1], where))
    for conv, numbered in turns.items():
        numbers = sorted({t for t, _ in numbered})
        if numbers != list(range(1, len(numbers) + 1)):
            problems.append(f"{numbered[0][1]}: conversation {conv!r} has turns {numbers}; number them 1..{len(numbers)}")
    return dict(cases), problems


def expand_paths(args: Iterable[Path | str]) -> list[Path]:
    """Expand glob patterns, including `**` (PowerShell passes them through unexpanded); keep other paths as given."""
    out: list[Path] = []
    for arg in map(str, args):
        matches = sorted(glob.glob(arg, recursive=True)) if glob.has_magic(arg) else []
        out += [Path(m) for m in matches] or [Path(arg)]
    return out


def load_cases(paths: Iterable[Path | str], kind: str) -> list[dict]:
    """Parse and validate case files of one kind; raise CaseError listing every problem."""
    cases, problems = check_files(expand_paths(paths), kind)
    if problems:
        raise CaseError(problems)
    return cases.get(kind, [])

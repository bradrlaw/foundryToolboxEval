"""Lightweight validator for eval JSONL files (stdlib only).

Usage: python scripts\\validate_eval_jsonl.py --kind tool|agent <file.jsonl> [...]
"""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCHEMAS = {"tool": "tool-test.schema.json", "agent": "agent-eval.schema.json"}
TYPES = {"string": str, "integer": int, "number": (int, float), "object": dict, "array": list, "boolean": bool, "null": type(None)}


def type_ok(value, expected):
    names = expected if isinstance(expected, list) else [expected]
    if isinstance(value, bool) and "boolean" not in names:
        return False
    return any(isinstance(value, TYPES[n]) for n in names)


def check(value, schema, path, errors):
    if "type" in schema and not type_ok(value, schema["type"]):
        errors.append(f"{path}: expected {schema['type']}")
        return
    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{path}: {value!r} not in {schema['enum']}")
    if isinstance(value, str) and len(value) < schema.get("minLength", 0):
        errors.append(f"{path}: too short")
    if isinstance(value, int) and not isinstance(value, bool) and value < schema.get("minimum", value):
        errors.append(f"{path}: below minimum")
    if isinstance(value, dict):
        for req in schema.get("required", []):
            if req not in value:
                errors.append(f"{path}: missing '{req}'")
        props = schema.get("properties", {})
        for key, sub in value.items():
            if key in props:
                check(sub, props[key], f"{path}.{key}", errors)
            elif schema.get("additionalProperties") is False:
                errors.append(f"{path}: unexpected '{key}'")
    if isinstance(value, list) and "items" in schema:
        for i, item in enumerate(value):
            check(item, schema["items"], f"{path}[{i}]", errors)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--kind", choices=SCHEMAS, required=True)
    parser.add_argument("files", nargs="+")
    args = parser.parse_args()
    schema = json.loads((ROOT / "schemas" / SCHEMAS[args.kind]).read_text(encoding="utf-8"))
    failed = False
    for name in args.files:
        for n, line in enumerate(Path(name).read_text(encoding="utf-8").splitlines(), 1):
            if not line.strip():
                continue
            errors = []
            try:
                check(json.loads(line), schema, "$", errors)
            except json.JSONDecodeError as e:
                errors.append(f"invalid JSON: {e}")
            if args.kind == "tool" and not errors:
                case = json.loads(line)
                kind = case.get("kind", "call")
                needs = {"call": ["tool", "arguments"], "list": ["expect_tools"], "auth": ["token", "expect_status"]}[kind]
                errors += [f"$: kind {kind} requires '{k}'" for k in needs if k not in case]
            for err in errors:
                failed = True
                print(f"{name}:{n}: {err}")
    print("FAILED" if failed else "OK")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()

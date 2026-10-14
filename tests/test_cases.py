import json
import tempfile
import unittest
from pathlib import Path

from evals.cases import ROOT, CaseError, check_files, detect_kind, expand_paths, load_cases


def problems_for(kind: str, *records: dict) -> list[str]:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "cases.jsonl"
        path.write_text("\n".join(json.dumps(r) for r in records), encoding="utf-8")
        return check_files([path], kind)[1]


def call(**kw):
    return {"case_id": "c1", "category": "x", "tool": "t", "arguments": {}, **kw}


def turn(**kw):
    return {"conversation_id": "c", "turn_number": 1, "user_message": "hi", "expected_tool_calls": [],
            "pass_criteria": "tool_called_correctly", "category": "x", **kw}


class DataFileTests(unittest.TestCase):
    def test_all_repo_case_files_are_valid(self):
        files = sorted((ROOT / "data").rglob("*.jsonl"))
        self.assertTrue(files)
        cases, problems = check_files(files)
        self.assertEqual(problems, [])
        self.assertEqual(set(cases), {"tool", "agent"})


class ToolCaseTests(unittest.TestCase):
    def test_valid_call(self):
        self.assertEqual(problems_for("tool", call(expect_error="protocol")), [])

    def test_kind_requirements(self):
        self.assertIn("'tool' is a required property", problems_for("tool", {"case_id": "a", "category": "x"})[0])
        self.assertIn("'expect_tools'", problems_for("tool", {"case_id": "a", "category": "x", "kind": "list"})[0])
        self.assertIn("'token'", problems_for("tool", {"case_id": "a", "category": "x", "kind": "auth", "expect_status": 401})[0])

    def test_fields_the_kind_ignores_are_flagged(self):
        p = problems_for("tool", {"case_id": "a", "category": "x", "kind": "list", "expect_tools": ["t"], "assertions": []})
        self.assertEqual(len(p), 1)
        self.assertIn("$.assertions: not used by kind 'list'", p[0])

    def test_assertion_rules(self):
        self.assertIn("is not of type 'string'", problems_for("tool", call(assertions=[{"type": "contains", "value": 5}]))[0])
        self.assertIn("is not a 'regex'", problems_for("tool", call(assertions=[{"type": "regex", "pattern": "("}]))[0])
        self.assertIn("does not match", problems_for("tool", call(assertions=[{"type": "json_path_exists", "path": "ticker"}]))[0])
        self.assertIn("not used by assertion type 'contains'", problems_for("tool", call(assertions=[{"type": "contains", "value": "a", "path": "$.a"}]))[0])
        self.assertIn("invalid JSON Schema", problems_for("tool", call(assertions=[{"type": "schema", "schema": {"type": "nope"}}]))[0])

    def test_expect_error_values(self):
        self.assertIn("is not one of", problems_for("tool", call(expect_error="any"))[0])

    def test_duplicate_case_ids(self):
        p = problems_for("tool", call(), call())
        self.assertEqual(len(p), 1)
        self.assertIn("duplicate case_id 'c1'", p[0])

    def test_invalid_json_line(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bad.jsonl"
            path.write_text("{not json}\n", encoding="utf-8")
            self.assertIn("invalid JSON", check_files([path], "tool")[1][0])


class AgentCaseTests(unittest.TestCase):
    def test_valid_turn(self):
        self.assertEqual(problems_for("agent", turn(pass_criteria="both", expected_response_pattern="x")), [])

    def test_response_criteria_need_a_pattern(self):
        self.assertIn("expected_response_pattern", problems_for("agent", turn(pass_criteria="both"))[0])

    def test_settings_the_criteria_ignore_are_flagged(self):
        self.assertIn("ignored unless", problems_for("agent", turn(expected_response_pattern="x"))[0])
        p = problems_for("agent", turn(pass_criteria="response_matches", expected_response_pattern="x",
                                       expected_tool_calls=[{"tool": "t", "params": {}}]))
        self.assertIn("ignored when pass_criteria is response_matches", p[0])

    def test_conflicts_and_regex_params(self):
        p = problems_for("agent", turn(expected_tool_calls=[{"tool": "t", "params": {"a": "re:("}}], must_not_call=["t"]))
        self.assertEqual(len(p), 2)
        self.assertTrue(any("also an expected call" in x for x in p))
        self.assertTrue(any("invalid regex" in x for x in p))

    def test_duplicate_turns_and_gaps(self):
        self.assertIn("duplicate conversation turn", problems_for("agent", turn(), turn())[0])
        self.assertIn("number them 1..2", problems_for("agent", turn(), turn(turn_number=3))[0])

    def test_invalid_turn_still_counts_for_turn_order(self):
        p = problems_for("agent", turn(pass_criteria="both"), turn(turn_number=2))
        self.assertEqual(len(p), 1)
        self.assertIn("expected_response_pattern", p[0])

    def test_load_cases_raises_with_every_problem(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "c.jsonl"
            path.write_text(json.dumps(turn(pass_criteria="both")) + "\n" + json.dumps(turn(turn_number=2)), encoding="utf-8")
            with self.assertRaises(CaseError) as ctx:
                load_cases([path], "agent")
            self.assertEqual(len(ctx.exception.problems), 1)


class KindDetectionTests(unittest.TestCase):
    def test_detect_kind_by_folder_then_content(self):
        self.assertEqual(detect_kind(ROOT / "data" / "tool-tests" / "x.jsonl"), "tool")
        self.assertEqual(detect_kind(ROOT / "data" / "agent-evals" / "x.jsonl"), "agent")
        self.assertEqual(detect_kind(Path("elsewhere/x.jsonl"), {"conversation_id": "c"}), "agent")
        self.assertEqual(detect_kind(Path("elsewhere/x.jsonl"), {"case_id": "c"}), "tool")
        self.assertIsNone(detect_kind(Path("elsewhere/x.jsonl"), {}))

    def test_expand_paths_globs(self):
        files = expand_paths([ROOT / "data" / "tool-tests" / "*.jsonl"])
        self.assertTrue(files)
        self.assertTrue(all(f.suffix == ".jsonl" for f in files))
        self.assertEqual({detect_kind(f) for f in expand_paths([ROOT / "data" / "**" / "*.jsonl"])}, {"tool", "agent"})
        self.assertEqual(expand_paths(["missing.jsonl"]), [Path("missing.jsonl")])


if __name__ == "__main__":
    unittest.main()

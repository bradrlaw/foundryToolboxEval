import argparse
import contextlib
import email.message
import io
import json
import os
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

from evals import run_cloud_eval as cloud

PROJECT = "https://example.test/api/projects/p"
USAGE = {"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110}


def http_error(code, body="", **headers):
    hdrs = email.message.Message()
    for k, v in headers.items():
        hdrs[k.replace("_", "-")] = v
    return urllib.error.HTTPError("https://example.test", code, "error", hdrs, io.BytesIO(body.encode()))


class Reply(io.BytesIO):
    """What urlopen returns: a body, with its status."""

    def __init__(self, data, status=200):
        super().__init__(data if isinstance(data, bytes) else json.dumps(data).encode())
        self.status = status


def score(name, value, label="pass", passed=True, status="completed", reason="fine", error=None):
    """An evaluator's result in an output item. An evaluator that ran reports its tokens."""
    sample = {"usage": USAGE} if status == "completed" else {}
    if error:
        sample["error"] = {"code": "UserError", "message": error}
    return {"type": "azure_ai_evaluator", "name": name, "metric": name, "score": value, "label": label,
            "passed": passed, "status": status, "reason": None if error else reason, "threshold": 3,
            "sample": sample}


ADHERES = score("task_adherence", 1)
STRAYS = score("task_adherence", 0, label="fail", passed=False, reason="The 4% figure is in no tool result.")
RESOLVES = score("intent_resolution", 5)
NO_CALLS = score("tool_call_accuracy", None, label="not_applicable", passed=None, status="skipped",
                 reason="Not applicable: No tool calls found")
CALLS_OK = score("tool_call_accuracy", 5)
NO_DEFINITIONS = score("tool_call_accuracy", None, label=None, passed=None, status="error",
                       error="(UserError) Missing inputs for line 1: 'data.tool_definitions'")


def item(*results, error=None, **source):
    """An output item: what was judged (its datasource item, with keys such as sample.output_text), and each
    evaluator's result."""
    return {"object": "eval.run.output_item", "id": "1", "datasource_item": source, "sample": {"error": error},
            "results": list(results)}


def messages(text, *calls):
    """A target run's output messages: the agent's tool calls with their results, then its answer."""
    out = [{"role": "system", "content": "You are an assistant."}]
    for i, (name, args) in enumerate(calls):
        out += [{"role": "assistant", "content": [{"type": "tool_call", "tool_call_id": f"call_{i}", "name": name,
                                                   "arguments": args}]},
                {"role": "tool", "tool_call_id": f"call_{i}",
                 "content": [{"type": "tool_result", "tool_result": '{"ok": true}'}]}]
    return out + [{"role": "assistant", "content": [{"type": "output_text", "text": text}]}]


def answered(case, text, *calls, results=(ADHERES, RESOLVES, CALLS_OK)):
    """A target run's output item for a reply the agent gave."""
    it = item(*results, case=case, query="q", response_id=f"resp_{case}", trace_id="trace_1", agent_version=4,
              **{"sample.output_items": messages(text, *calls), "sample.output_text": text})
    it["sample"] = {"error": None, "latency_ms": 4273.66, "model": "gpt-4-1-mini",
                    "usage": {"prompt_tokens": 938, "completion_tokens": 120, "total_tokens": 1058, "cached_tokens": 0}}
    return it


def turn(n, response_id, passed=True, conversation="c", **kw):
    """A turn of an agent-eval run."""
    return {"conversation_id": conversation, "turn_number": n, "repetition": 1, "category": "lookup",
            "passed": passed, "failures": [] if passed else ["expected call t {} not made"],
            "reasons": [] if passed else ["missing_call"], "response_id": response_id, "previous_response_id": None,
            "agent_version": "4", "model": "gpt-4-1-mini", "tool_calls": [], "response": f"reply {n}", **kw}


LOOKUP = {"conversation_id": "lookup", "turn_number": 1, "user_message": "Look up customer C1.",
          "expected_tool_calls": [{"tool": "sample___get_customer", "params": {"customer_id": "C1"}}],
          "pass_criteria": "tool_called_correctly", "category": "lookup"}
FOLLOW_UP = {"conversation_id": "lookup", "turn_number": 2, "user_message": "And its industry?",
             "expected_tool_calls": [], "pass_criteria": "response_matches", "expected_response_pattern": "retail",
             "category": "follow-up"}
WHO = {"conversation_id": "who", "turn_number": 1, "user_message": "Who am I?", "expected_tool_calls": [],
       "pass_criteria": "response_matches", "expected_response_pattern": "Ada", "category": "identity"}


class ApiTests(unittest.TestCase):
    def setUp(self):
        patches = [mock.patch.object(cloud.auth, "get_token", return_value="tok"), mock.patch.object(cloud, "time"),
                   mock.patch.object(cloud.urllib.request, "urlopen")]
        _, self.clock, self.urlopen = [p.start() for p in patches]
        for p in patches:
            self.addCleanup(p.stop)
        self.api = cloud.EvalsAPI(PROJECT + "/")

    def test_requests_go_to_the_projects_evaluation_api_with_a_token(self):
        self.urlopen.return_value = Reply({"id": "e1"}, status=201)
        self.assertEqual(self.api.call("POST", "/evals", {"name": "n"}), {"id": "e1"})
        req = self.urlopen.call_args.args[0]
        self.assertEqual((req.full_url, req.get_method()), (PROJECT + "/openai/v1/evals", "POST"))
        self.assertEqual(req.get_header("Authorization"), "Bearer tok")
        self.assertEqual(json.loads(req.data), {"name": "n"})

    def test_a_get_is_retried_on_transient_errors_and_broken_connections(self):
        self.urlopen.side_effect = [http_error(503), urllib.error.URLError("no DNS"), TimeoutError("timed out"),
                                    Reply({"status": "completed"})]
        self.assertEqual(self.api.call("GET", "/evals/e1/runs/r1"), {"status": "completed"})
        self.assertEqual([c.args[0] for c in self.clock.sleep.call_args_list], [5.0, 10.0, 20.0])

    def test_a_post_is_resent_only_when_the_service_surely_did_not_process_it(self):
        self.urlopen.side_effect = [
            http_error(429, retry_after="2"), urllib.error.URLError("no DNS"), Reply({"id": "r1"})]
        self.assertEqual(self.api.start("e1", {}), "r1")
        self.assertEqual([c.args[0] for c in self.clock.sleep.call_args_list], [2.0, 10.0])
        self.urlopen.reset_mock()
        self.urlopen.side_effect = [http_error(500, json.dumps({"error": {"message": "boom"}})), Reply({"id": "r2"})]
        with self.assertRaisesRegex(cloud.CloudEvalError, "^evaluation API 500: boom$"):
            self.api.start("e1", {})
        self.urlopen.side_effect = [ConnectionResetError(), Reply({"id": "r2"})]
        with self.assertRaises(ConnectionResetError):  # the request went out
            self.api.start("e1", {})
        self.assertEqual(self.urlopen.call_count, 2)

    def test_a_reply_without_json_or_an_id_is_an_error(self):
        self.urlopen.return_value = Reply(b"<html>sign in</html>")
        with self.assertRaisesRegex(cloud.CloudEvalError, "expected JSON, got: <html>"):
            self.api.call("GET", "/evals")
        self.urlopen.return_value = Reply({"object": "eval.run"})
        with self.assertRaisesRegex(cloud.CloudEvalError, "the reply has no id"):
            self.api.start("e1", {})


class PagingTests(unittest.TestCase):
    def test_an_evaluation_with_the_same_name_is_reused_from_any_page(self):
        api = cloud.EvalsAPI(PROJECT)
        pages = [{"data": [{"id": "e1", "name": "other"}], "has_more": True, "last_id": "e1"},
                 {"data": [{"id": "e2", "name": "toolbox-eval-x"}], "has_more": False}]
        with mock.patch.object(api, "call", side_effect=pages) as call:
            self.assertEqual(api.eval_id({"name": "toolbox-eval-x"}), ("e2", False))
        self.assertEqual([c.args for c in call.call_args_list],
                         [("GET", "/evals?limit=100"), ("GET", "/evals?limit=100&after=e1")])

    def test_an_evaluation_is_created_when_none_has_the_name(self):
        api = cloud.EvalsAPI(PROJECT)
        with mock.patch.object(api, "call", side_effect=[{"data": [], "has_more": False}, {"id": "e9"}]) as call:
            self.assertEqual(api.eval_id({"name": "toolbox-eval-x"}), ("e9", True))
        self.assertEqual(call.call_args.args, ("POST", "/evals", {"name": "toolbox-eval-x"}))

    def test_output_items_follow_the_last_id_and_stop_if_it_does_not_move(self):
        api = cloud.EvalsAPI(PROJECT)
        pages = [{"data": [{"id": "1"}, {"id": "2"}], "has_more": True, "last_id": "2"},
                 {"data": [{"id": "3"}], "has_more": True},  # no last_id: the last item's id
                 {"data": [], "has_more": True, "last_id": "3"}]
        with mock.patch.object(api, "call", side_effect=pages) as call:
            self.assertEqual(api.output_items("e1", "r1"), [{"id": "1"}, {"id": "2"}, {"id": "3"}])
        self.assertEqual(call.call_args_list[1].args, ("GET", "/evals/e1/runs/r1/output_items?limit=100&after=2"))
        self.assertEqual(call.call_count, 3)


class WaitTests(unittest.TestCase):
    def wait(self, states, clock, timeout_s=600):
        api = cloud.EvalsAPI(PROJECT)
        out = io.StringIO()
        with mock.patch.object(api, "call", side_effect=states), mock.patch.object(cloud, "time") as time, \
                contextlib.redirect_stdout(out):
            time.monotonic.side_effect = clock
            run = api.wait("e1", "r1", timeout_s)
        return run, out.getvalue(), [c.args[0] for c in time.sleep.call_args_list]

    def test_polls_until_the_run_ends_showing_each_change(self):
        states = [{"status": "queued"}, {"status": "in_progress", "result_counts": {"passed": 1, "failed": 0}},
                  {"status": "in_progress", "result_counts": {"passed": 1, "failed": 0}},
                  {"status": "completed", "result_counts": {"passed": 2, "failed": 1}}]
        run, shown, slept = self.wait(states, lambda: 0.0)
        self.assertEqual(run["status"], "completed")
        self.assertEqual(shown, "  queued\n  in_progress: passed 1\n  completed: passed 2, failed 1\n")
        self.assertEqual(slept, [5.0, 10.0, 15.0])

    def test_returns_the_last_state_when_time_runs_out(self):
        run, _, slept = self.wait([{"status": "in_progress"}] * 2, [0.0, 598.0, 601.0])
        self.assertEqual(run["status"], "in_progress")
        self.assertEqual(slept, [2.0])  # never past the deadline


class BuilderTests(unittest.TestCase):
    EVALUATORS = ("task_adherence", "tool_call_accuracy")

    def test_an_evaluation_is_named_for_its_settings(self):
        one = cloud.eval_definition("responses", self.EVALUATORS, "judge-a")
        self.assertEqual(one, cloud.eval_definition("responses", self.EVALUATORS, "judge-a"))
        self.assertRegex(one["name"], r"^toolbox-eval-responses-[0-9a-f]{8}$")
        self.assertNotEqual(one["name"], cloud.eval_definition("responses", self.EVALUATORS, "judge-b")["name"])
        self.assertNotIn("data_mapping", one["testing_criteria"][0])

    def test_target_evaluators_find_their_inputs_and_tool_evaluators_get_the_tool_definitions(self):
        target = cloud.eval_definition("target", self.EVALUATORS, "judge-a")
        self.assertTrue(target["name"].startswith("toolbox-eval-target-"))
        adherence, accuracy = target["testing_criteria"]
        self.assertEqual(adherence["evaluator_name"], "builtin.task_adherence")
        self.assertEqual(adherence["initialization_parameters"], {"deployment_name": "judge-a"})
        self.assertEqual(adherence["data_mapping"], {"query": "{{item.query}}", "response": "{{sample.output_items}}"})
        self.assertEqual(accuracy["data_mapping"]["tool_definitions"], "{{sample.tool_definitions}}")
        self.assertNotIn("tool_calls", accuracy["data_mapping"])

    def test_a_target_run_sends_the_first_turn_of_each_conversation(self):
        firsts, left_out = cloud.first_turns([FOLLOW_UP, WHO, LOOKUP])
        self.assertEqual((firsts, left_out), ([LOOKUP, WHO], ["lookup#2"]))
        source = cloud.target_run("n", firsts, "my-agent")["data_source"]
        self.assertEqual(source["source"]["content"],
                         [{"item": {"query": "Look up customer C1.", "case": "lookup#1"}},
                          {"item": {"query": "Who am I?", "case": "who#1"}}])
        self.assertEqual(source["target"], {"type": "azure_ai_agent", "name": "my-agent"})


class VerdictTests(unittest.TestCase):
    def verdict(self, *results, source="responses", **kw):
        found, scores, failures = cloud.verdict(item(*results, **kw), source)
        return found, [(f.reason, f.message) for f in failures]

    def test_a_turn_passes_when_an_evaluator_passes_it_and_none_fails_or_errors(self):
        self.assertEqual(self.verdict(ADHERES, RESOLVES, NO_CALLS), ("pass", []))
        self.assertEqual(self.verdict(NO_CALLS), ("skipped", []))

    def test_a_failing_evaluator_fails_the_turn_with_its_score_and_reason(self):
        self.assertEqual(self.verdict(STRAYS, RESOLVES, NO_DEFINITIONS), ("fail", [
            ("task_adherence", "task_adherence fail (score 0): The 4% figure is in no tool result."),
            ("evaluator_error", "tool_call_accuracy error: (UserError) Missing inputs for line 1: "
                                "'data.tool_definitions'")]))

    def test_an_evaluator_that_could_not_judge_the_turn_makes_it_an_error(self):
        self.assertEqual(self.verdict(ADHERES, NO_DEFINITIONS)[0], "error")
        unsure = {**RESOLVES, "passed": None, "score": 2.5}
        self.assertEqual(self.verdict(unsure), ("error", [
            ("evaluator_error", "intent_resolution gave no pass or fail (score 2.5): fine")]))

    def test_an_item_error_is_the_agents_in_a_target_run(self):
        error = {"code": "server_error", "message": "The agent is unavailable."}
        self.assertEqual(self.verdict(source="target", error=error),
                         ("error", [("agent_error", "the agent call failed: The agent is unavailable.")]))
        self.assertEqual(self.verdict(error=error),
                         ("not_judged", [("not_judged", "not judged: The agent is unavailable.")]))

    def test_a_turn_without_results_is_not_judged(self):
        self.assertEqual(cloud.verdict(None, "responses")[0], "not_judged")
        self.assertEqual(self.verdict()[0], "not_judged")


class JudgeTests(unittest.TestCase):
    def test_stored_responses_are_matched_by_id_and_keep_the_turns_deterministic_result(self):
        turns = [turn(1, "resp_1"), turn(2, "resp_2", passed=False, previous_response_id="resp_1"),
                 turn(3, None, passed=False)]
        items = [item(STRAYS, RESOLVES, CALLS_OK, response_id="resp_2"),
                 item(ADHERES, RESOLVES, NO_CALLS, response_id="resp_1")]
        out = cloud.judge_responses(turns, items)
        self.assertEqual([(r["verdict"], r["passed"]) for r in out],
                         [("pass", True), ("fail", False), ("not_judged", False)])
        self.assertEqual(out[1]["deterministic"], {"passed": False, "failures": ["expected call t {} not made"],
                                                   "reasons": ["missing_call"]})
        self.assertEqual(out[1]["previous_response_id"], "resp_1")
        self.assertEqual(out[1]["scores"]["task_adherence"]["score"], 0)
        self.assertEqual(out[2]["failures"], ["not judged: the turn has no stored response"])

    def test_target_replies_get_the_deterministic_checks_too(self):
        items = [answered("lookup#1", "C1 is Contoso.", ("sample___get_customer", {"customer_id": "C1"})),
                 answered("who#1", "I can't tell who you are.", results=(ADHERES, RESOLVES, NO_CALLS)),
                 item(case="down#1", error={"code": "server_error", "message": "unavailable"})]
        down = {**WHO, "conversation_id": "down"}
        lost = {**WHO, "conversation_id": "lost"}
        out = cloud.judge_target([LOOKUP, WHO, down, lost], items)
        self.assertEqual([r["verdict"] for r in out], ["pass", "pass", "error", "not_judged"])
        lookup = out[0]
        self.assertEqual(lookup["deterministic"], {"passed": True, "failures": [], "reasons": []})
        self.assertEqual(lookup["tool_calls"], [{"name": "sample___get_customer", "arguments": {"customer_id": "C1"}}])
        self.assertEqual((lookup["latency_ms"], lookup["agent_version"], lookup["response_id"], lookup["trace_id"]),
                         (4273.7, "4", "resp_lookup#1", "trace_1"))
        self.assertEqual(lookup["usage"], {"input_tokens": 938, "output_tokens": 120, "total_tokens": 1058})
        # The judge passed it, but the reply doesn't name the caller.
        self.assertEqual(out[1]["deterministic"]["reasons"], ["response_mismatch"])
        self.assertEqual([out[2]["deterministic"], out[3]["deterministic"]], [None, None])
        self.assertEqual(out[2]["reasons"], ["agent_error"])

    def test_tool_call_arguments_sent_as_text_are_parsed(self):
        sent = [{"role": "assistant", "content": [{"type": "tool_call", "name": "t", "arguments": '{"a": 1}'},
                                                  {"type": "tool_call", "name": "u", "arguments": "not json"}]}]
        self.assertEqual(cloud.tool_calls(sent), [{"name": "t", "arguments": {"a": 1}}, {"name": "u", "arguments": {}}])

    def test_judge_tokens_are_summed_over_the_evaluators_that_ran(self):
        items = [item(ADHERES, NO_CALLS), item(STRAYS, NO_DEFINITIONS)]
        self.assertEqual(cloud.judge_usage(items), {"input_tokens": 200, "output_tokens": 20, "total_tokens": 220})
        self.assertIsNone(cloud.judge_usage([item(NO_CALLS)]))

    def test_evaluator_names_may_have_the_builtin_prefix(self):
        self.assertEqual(cloud.evaluator_list("builtin.task_adherence, intent_resolution,task_adherence"),
                         ("task_adherence", "intent_resolution"))
        with self.assertRaises(argparse.ArgumentTypeError):
            cloud.evaluator_list("Task Adherence")


class MainTests(unittest.TestCase):
    AGENT = {"name": "my-agent", "version": "4", "model": "gpt-4-1-mini", "toolboxes": [{"name": "tb", "version": "2"}]}

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        patches = [mock.patch.dict(os.environ, {"FOUNDRY_PROJECT_ENDPOINT": PROJECT + "/"}),
                   mock.patch.object(cloud, "load_dotenv"), mock.patch.object(cloud, "AGENT_RUNS", self.dir / "runs"),
                   mock.patch("evals.auth.get_token", return_value="tok"),
                   mock.patch("evals.auth.caller_claims", return_value={"name": "Ada (Admin)"}),
                   mock.patch("evals.provenance.repo", return_value={"commit": "abc1234", "dirty": False}),
                   mock.patch("evals.provenance.agent", return_value=self.AGENT)]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.api = {}
        for name in ("eval_id", "start", "wait", "output_items"):
            p = mock.patch.object(cloud.EvalsAPI, name)
            self.api[name] = p.start()
            self.addCleanup(p.stop)
        for name in ("FOUNDRY_EVAL_JUDGE_DEPLOYMENT", "FOUNDRY_AGENT_NAME", "GITHUB_ACTIONS"):
            os.environ.pop(name, None)  # restored with the rest of the environment
        self.api["eval_id"].return_value = ("e1", True)
        self.api["start"].return_value = "r1"
        self.api["wait"].return_value = {"status": "completed", "result_counts": {"passed": 1, "failed": 1},
                                         "report_url": "https://portal.test/r1"}
        self.api["output_items"].return_value = []

    def agent_run(self, results, **kw):
        path = self.dir / "runs" / "run-20260101T000000Z.json"
        path.parent.mkdir(exist_ok=True)
        path.write_text(json.dumps({"agent": "my-agent", "results": results, **kw}), encoding="utf-8")
        return path

    def main(self, *argv, out="out"):
        stdout, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(err):
            code = cloud.main([*argv, "--out", str(self.dir / out)])
        saved = [json.loads(p.read_text(encoding="utf-8")) for p in (self.dir / out).glob("run-*.json")]
        return code, (saved[0] if saved else None), stdout.getvalue(), err.getvalue()

    def test_judges_the_newest_agent_runs_stored_responses(self):
        provenance = {"repo": {"commit": "abc1234", "dirty": False}, "agent": {"name": "my-agent", "version": "4"}}
        self.agent_run([turn(1, "resp_1"), turn(2, "resp_2", previous_response_id="resp_1")], history="chain",
                       provenance=provenance)
        self.api["output_items"].return_value = [item(ADHERES, RESOLVES, NO_CALLS, response_id="resp_1"),
                                                 item(STRAYS, RESOLVES, CALLS_OK, response_id="resp_2")]
        code, saved, out, _ = self.main()
        self.assertEqual(code, 1)
        self.api["eval_id"].assert_called_once_with(
            cloud.eval_definition("responses", cloud.DEFAULT_EVALUATORS, "gpt-4-1-mini"))
        # The evaluators see as many turns as the longest conversation has.
        self.api["start"].assert_called_once_with("e1", cloud.responses_run(
            "responses of run-20260101T000000Z.json", ["resp_1", "resp_2"], 2))
        self.assertIn("FAIL c#2 lookup: task_adherence 0, intent_resolution 5, tool_call_accuracy 5 (checks PASS)\n"
                      "       - task_adherence fail (score 0): The 4% figure is in no tool result.", out)
        self.assertIn("Judge vs. checks: agree on 1/2; judge failed only: c#2; checks failed only: none", out)
        self.assertIn("Judge tokens: 550 (500 input, 50 output)", out)
        self.assertIn("Portal: https://portal.test/r1", out)
        self.assertEqual({k: saved[k] for k in ("kind", "source", "agent_name", "agent_run", "history", "max_turns",
                                                "judge", "eval_id", "eval_run_id", "status", "provenance")},
                         {"kind": "cloud", "source": "responses", "agent_name": "my-agent",
                          "agent_run": "run-20260101T000000Z.json", "history": "chain", "max_turns": 2,
                          "judge": "gpt-4-1-mini", "eval_id": "e1", "eval_run_id": "r1", "status": "completed",
                          "provenance": provenance})
        self.assertEqual([r["verdict"] for r in saved["results"]], ["pass", "fail"])
        self.assertIsNone(saved["agent_usage"])

    def test_fail_on_unjudged_reports_the_judges_failures_but_fails_only_turns_it_could_not_judge(self):
        self.agent_run([turn(1, "resp_1"), turn(2, "resp_2")])
        self.api["output_items"].return_value = [item(STRAYS, RESOLVES, response_id="resp_1"),
                                                 item(ADHERES, RESOLVES, response_id="resp_2")]
        code, saved, out, _ = self.main("--fail-on", "unjudged")
        self.assertEqual(code, 0)
        self.assertEqual([r["verdict"] for r in saved["results"]], ["fail", "pass"])
        self.assertIn("FAIL c#1 lookup", out)
        self.assertIn("The judge's failures don't fail the run (--fail-on unjudged).", out)
        for unjudged, (turns, items) in enumerate((
                ([turn(1, "resp_1"), turn(2, "resp_2")],
                 [item(STRAYS, response_id="resp_1"), item(ADHERES, NO_DEFINITIONS, response_id="resp_2")]),
                ([turn(1, "resp_1"), turn(2, None, passed=False)], [item(STRAYS, response_id="resp_1")]))):
            with self.subTest(["error", "not_judged"][unjudged]):
                self.agent_run(turns)
                self.api["output_items"].return_value = items
                self.assertEqual(self.main("--fail-on", "unjudged", out=f"out{unjudged}")[0], 1)

    def test_target_mode_has_foundry_call_the_agent_with_first_turns(self):
        cases = self.dir / "cases.jsonl"
        who = {**WHO, "expected_response_pattern": "{{caller.name}}"}
        cases.write_text("".join(json.dumps(c) + "\n" for c in (LOOKUP, FOLLOW_UP, who)), encoding="utf-8")
        self.api["output_items"].return_value = [
            answered("lookup#1", "C1 is Contoso.", ("sample___get_customer", {"customer_id": "C1"})),
            answered("who#1", "You are Ada (Admin).", results=(ADHERES, RESOLVES, NO_CALLS))]
        os.environ.update(GITHUB_ACTIONS="true", FOUNDRY_EVAL_JUDGE_DEPLOYMENT="env-judge")
        code, saved, out, _ = self.main("--target", str(cases), "--agent", "my-agent", "--evaluators",
                                        "task_adherence,builtin.tool_call_accuracy")
        self.assertEqual(code, 0)
        self.api["eval_id"].assert_called_once_with(
            cloud.eval_definition("target", ("task_adherence", "tool_call_accuracy"), "env-judge"))
        body = self.api["start"].call_args.args[1]
        self.assertTrue(body["name"].startswith("my-agent first turns "))
        self.assertEqual([i["item"]["case"] for i in body["data_source"]["source"]["content"]], ["lookup#1", "who#1"])
        self.assertIn("later turns left out: 1", out)
        self.assertIn("Agent tokens: 2,116 (1,876 input, 240 output)", out)
        self.assertNotIn("Portal:", out)  # live CI logs are public
        self.assertEqual((saved["source"], saved["agent_name"], saved["left_out"], saved["judge"]),
                         ("target", "my-agent", ["lookup#2"], "env-judge"))
        self.assertEqual(saved["provenance"]["agent"], {"name": "my-agent", "version": "4", "model": "gpt-4-1-mini"})
        self.assertEqual([r["deterministic"]["passed"] for r in saved["results"]], [True, True])

    def test_setup_errors_exit_2_before_the_evaluation_starts(self):
        not_agent = self.dir / "cloud-run.json"
        not_agent.write_text(json.dumps({"kind": "cloud", "results": []}), encoding="utf-8")
        for argv, expected in (
                (["x.json", "--target", "y.jsonl"], "not both"),
                (["--agent", "a"], "--agent goes with --target"),
                (["--target", "y.jsonl", "--max-turns", "2"], "--max-turns goes with an agent run"),
                ([], "no runs in"),
                ([str(not_agent)], "is not an agent-eval run")):
            code, saved, _, err = self.main(*argv)
            self.assertEqual((code, saved), (2, None), argv)
            self.assertIn(expected, err)
        self.assertIn("has no stored responses", self.main(str(self.agent_run([turn(1, None)])))[3])
        self.api["start"].assert_not_called()

    def test_a_run_that_fails_or_does_not_end_is_a_setup_error(self):
        self.agent_run([turn(1, "resp_1")])
        for state, expected in (({"status": "failed", "error": {"code": "x", "message": "quota exceeded"}},
                                 "the evaluation run failed (evaluation e1, run r1): quota exceeded"),
                                ({"status": "in_progress"}, "still in_progress after 30 minutes")):
            self.api["wait"].return_value = state
            code, saved, _, err = self.main()
            self.assertEqual((code, saved), (2, None))
            self.assertIn(expected, err)
        self.api["output_items"].assert_not_called()

    def test_an_api_error_is_a_setup_error(self):
        self.agent_run([turn(1, "resp_1")])
        self.api["eval_id"].side_effect = cloud.CloudEvalError(403, json.dumps({"error": {"message": "denied"}}))
        code, _, _, err = self.main()
        self.assertEqual(code, 2)
        self.assertIn("evaluation API 403: denied", err)


if __name__ == "__main__":
    unittest.main()

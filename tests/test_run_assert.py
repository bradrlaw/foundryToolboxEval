import contextlib
import importlib.util
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("run_assert", ROOT / "scripts" / "run_assert.py")
run_assert = importlib.util.module_from_spec(spec)
spec.loader.exec_module(run_assert)

SERVED = [
    {"agent": "assistant", "history": "chain", "previous_response_id": None, "response_id": "resp_1",
     "agent_version": "4", "model": "gpt-4-1-mini", "usage": {"input_tokens": 90, "output_tokens": 10, "total_tokens": 100},
     "abort_reason": None},
    {"agent": "assistant", "history": "chain", "previous_response_id": "resp_1", "response_id": "resp_2",
     "agent_version": "4", "model": "gpt-4-1-mini", "usage": {"input_tokens": 9, "output_tokens": 1, "total_tokens": 10},
     "abort_reason": None},
    {"agent": "assistant", "history": "chain", "previous_response_id": None, "response_id": None,
     "agent_version": None, "model": None, "usage": None, "abort_reason": None},
]


class ArgumentTests(unittest.TestCase):
    def test_history_is_taken_out_of_what_goes_to_assert_ai(self):
        for args in (["--history", "replay", "--override", "run=x"], ["--override", "run=x", "--history=replay"]):
            self.assertEqual(run_assert.take_history(args), ("replay", ["--override", "run=x"]))

    def test_the_run_directory_follows_the_config_and_its_overrides(self):
        config = {"suite": "s", "run": "baseline"}
        results = ROOT / "results" / "assert" / "results"
        self.assertEqual(run_assert.run_dir(config, []), results / "s" / "baseline")
        self.assertEqual(run_assert.run_dir(config, ["--override", "run=v2", "--override=suite=t"]), results / "t" / "v2")
        self.assertEqual(run_assert.run_dir(config, ["--override", "run=2026"]), results / "s" / "2026")
        self.assertIsNone(run_assert.run_dir({"suite": "s"}, []))
        self.assertIsNone(run_assert.run_dir(config, ["--override", "results_dir=elsewhere"]))


class MainTests(unittest.TestCase):
    def main(self, config_text, served, *argv, existing=None):
        """Run main against a config, with assert-ai faked to log `served`. Returns the exit code, what assert-ai
        got (command and environment), and the run directory's provenance.json if there is one."""
        got = {}

        def fake_assert_ai(cmd, cwd, env):
            got.update(cmd=cmd, env=env)
            with open(env["FOUNDRY_EVAL_SERVED_LOG"], "a", encoding="utf-8") as f:
                f.writelines(json.dumps(s) + "\n" for s in served)
            return mock.Mock(returncode=0)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = root / "c.yaml"
            config.write_text(config_text, encoding="utf-8")
            run_dir = root / "results" / "assert" / "results" / "s" / "r"
            run_dir.mkdir(parents=True)
            if existing:
                (run_dir / "provenance.json").write_text(json.dumps(existing), encoding="utf-8")
            agent = {"name": "assistant", "version": "4", "model": "gpt-4-1-mini",
                     "toolboxes": [{"name": "tb", "version": "2"}]}
            with mock.patch.object(run_assert, "ROOT", root), \
                    mock.patch.object(run_assert.shutil, "which", return_value="assert-ai"), \
                    mock.patch.object(run_assert.subprocess, "run", side_effect=fake_assert_ai), \
                    mock.patch.dict(os.environ, {"FOUNDRY_PROJECT_ENDPOINT": "https://example.test/api/projects/p"}), \
                    mock.patch("evals.provenance.repo", return_value={"commit": "abc", "dirty": False}), \
                    mock.patch("evals.provenance.agent", return_value=agent), \
                    contextlib.redirect_stdout(io.StringIO()):
                code = run_assert.main([str(config), *argv])
            path = run_dir / "provenance.json"
            saved = json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
        return code, got, saved

    def test_a_run_that_called_the_agent_records_what_served_it(self):
        code, got, saved = self.main("suite: s\nrun: r\n", SERVED, "--history", "chain")
        self.assertEqual(code, 0)
        self.assertNotIn("--history", got["cmd"])
        self.assertEqual(got["env"]["FOUNDRY_AGENT_HISTORY"], "chain")
        self.assertEqual({k: saved[k] for k in ("agent", "history", "requests", "continued", "usage")},
                         {"agent": "assistant", "history": "chain", "requests": 3, "continued": 1,
                          "usage": {"input_tokens": 99, "output_tokens": 11, "total_tokens": 110}})
        self.assertEqual(saved["provenance"], {"repo": {"commit": "abc", "dirty": False},
                                               "agent": {"name": "assistant", "version": "4", "model": "gpt-4-1-mini"},
                                               "toolboxes": [{"name": "tb", "version": "2"}]})

    def test_a_rerun_from_cached_transcripts_keeps_the_provenance_it_had(self):
        _, _, saved = self.main("suite: s\nrun: r\n", [], existing={"requests": 36})
        self.assertEqual(saved, {"requests": 36})

    def test_a_config_without_a_run_name_gets_one_so_its_directory_is_known(self):
        _, got, _ = self.main("suite: s\n", [])
        self.assertEqual(got["cmd"][-2], "--override")
        self.assertRegex(got["cmd"][-1], r"^run=\d{8}T\d{6}$")

    def test_an_unknown_history_mode_is_refused(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(run_assert.main([__file__, "--history", "stateless"]), 2)
        self.assertIn("--history must be chain or replay", err.getvalue())


if __name__ == "__main__":
    unittest.main()

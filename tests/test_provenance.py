import io
import subprocess
import unittest
import urllib.error
from unittest import mock

from evals import provenance

PROJECT = "https://res.services.ai.example/api/projects/p"
ENDPOINT = f"{PROJECT}/toolboxes/tb/mcp?api-version=v1"


class RepoTests(unittest.TestCase):
    def test_commit_and_whether_the_tree_has_changes(self):
        outputs = {"rev-parse": "abc123\n", "status": " M README.md\n"}

        def git(args, **kwargs):
            return subprocess.CompletedProcess(args, 0, stdout=outputs[args[2]], stderr="")

        with mock.patch.object(provenance.subprocess, "run", side_effect=git):
            self.assertEqual(provenance.repo(), {"commit": "abc123", "dirty": True})
            outputs["status"] = ""
            self.assertEqual(provenance.repo(), {"commit": "abc123", "dirty": False})

    def test_without_git_or_a_repo_both_are_unknown(self):
        for error in (FileNotFoundError("git"), subprocess.CalledProcessError(128, "git")):
            with mock.patch.object(provenance.subprocess, "run", side_effect=error):
                self.assertEqual(provenance.repo(), {"commit": None, "dirty": None})


class GetTests(unittest.TestCase):
    def test_sends_a_foundry_token_and_returns_none_on_any_failure(self):
        with mock.patch.object(provenance.auth, "get_token", return_value="tok") as token, \
                mock.patch.object(provenance.urllib.request, "urlopen", return_value=io.BytesIO(b'{"a": 1}')) as get:
            self.assertEqual(provenance._get(f"{PROJECT}/toolboxes/tb"), {"a": 1})
        token.assert_called_once_with(provenance.auth.FOUNDRY_SCOPE)
        req = get.call_args.args[0]
        self.assertEqual((req.full_url, req.get_header("Authorization")),
                         (f"{PROJECT}/toolboxes/tb?api-version=v1", "Bearer tok"))

        for token, response in ((RuntimeError("az login"), None), ("tok", urllib.error.URLError("down")),
                                ("tok", io.BytesIO(b"[]"))):
            with mock.patch.object(provenance.auth, "get_token", side_effect=[token]), \
                    mock.patch.object(provenance.urllib.request, "urlopen", side_effect=[response]):
                self.assertIsNone(provenance._get(PROJECT))


class ToolboxTests(unittest.TestCase):
    def test_reads_the_default_version_from_the_project(self):
        with mock.patch.object(provenance, "_get", return_value={"default_version": 3}) as get:
            self.assertEqual(provenance.toolbox(ENDPOINT, PROJECT + "/"), {"name": "tb", "version": "3"})
        get.assert_called_once_with(f"{PROJECT}/toolboxes/tb")
        with mock.patch.object(provenance, "_get", return_value=None):
            self.assertEqual(provenance.toolbox(ENDPOINT, PROJECT), {"name": "tb", "version": None})

    def test_only_toolboxes_in_the_project_are_looked_up(self):
        with mock.patch.object(provenance, "_get", return_value={"default_version": "3"}) as get:
            self.assertIsNone(provenance.toolbox("https://mcp.example/mcp", PROJECT))
            # A toolbox in another project, or with no project configured: its name only, and no token sent.
            other = "https://other.example/api/projects/p/toolboxes/tb/mcp"
            self.assertEqual(provenance.toolbox(other, PROJECT), {"name": "tb", "version": None})
            self.assertEqual(provenance.toolbox(ENDPOINT, None), {"name": "tb", "version": None})
        get.assert_not_called()


class AgentTests(unittest.TestCase):
    def test_reads_the_model_and_toolboxes_of_the_version(self):
        definition = {"model": "gpt-4-1-mini", "tools": [
            {"type": "mcp", "server_url": ENDPOINT},
            {"type": "mcp", "server_url": "https://mcp.example/mcp"},
            {"type": "code_interpreter"},
        ]}

        def get(url):
            return {"definition": definition} if "/agents/" in url else {"default_version": "3"}

        with mock.patch.object(provenance, "_get", side_effect=get) as lookup:
            out = provenance.agent(PROJECT, "my agent", "2")
        self.assertEqual(out, {"name": "my agent", "version": "2", "model": "gpt-4-1-mini",
                               "toolboxes": [{"name": "tb", "version": "3"}]})
        self.assertEqual(lookup.call_args_list[0].args[0], f"{PROJECT}/agents/my%20agent/versions/2")

    def test_unknown_version_or_unreadable_definition(self):
        with mock.patch.object(provenance, "_get", return_value=None) as get:
            self.assertEqual(provenance.agent(PROJECT, "a", None),
                             {"name": "a", "version": None, "model": None, "toolboxes": []})
            get.assert_not_called()
            self.assertEqual(provenance.agent(PROJECT, "a", "2"),
                             {"name": "a", "version": "2", "model": None, "toolboxes": []})


if __name__ == "__main__":
    unittest.main()

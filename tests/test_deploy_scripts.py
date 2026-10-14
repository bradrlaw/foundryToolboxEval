"""scripts/deploy_toolbox.py and deploy_agent.py: request bodies and dry runs, without calling Azure."""
import contextlib
import importlib.util
import io
import json
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))  # the scripts import _az


def load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


deploy_toolbox = load("deploy_toolbox")
deploy_agent = load("deploy_agent")

PROJECT = "https://acct.services.ai.azure.com/api/projects/proj"
ENV = {"FOUNDRY_PROJECT_ENDPOINT": PROJECT, "FOUNDRY_TOOLBOX_NAME": "tb", "FOUNDRY_AGENT_NAME": "agent",
       "ENTRA_TENANT_ID": "tenant-id", "ENTRA_API_APP_ID": "api-id", "ENTRA_OAUTH_CLIENT_APP_ID": "client-id"}


def no_azure(*args, **kwargs):
    raise AssertionError("a dry run must not call Azure")


def run(module, argv, env=ENV):
    """main(argv) with exactly these ENV variables set and every Azure call failing; returns stdout."""
    out = io.StringIO()
    with contextlib.ExitStack() as stack:
        stack.enter_context(mock.patch.dict(os.environ, env))
        for key in set(ENV) - set(env):
            os.environ.pop(key, None)
        stack.enter_context(mock.patch.object(module, "load_dotenv", lambda *a, **k: None))
        for name in ("az", "rest", "graph", "where"):
            if hasattr(module, name):
                stack.enter_context(mock.patch.object(module, name, no_azure))
        stack.enter_context(contextlib.redirect_stdout(out))
        module.main(argv)
    return out.getvalue()


class ConnectionBodyTests(unittest.TestCase):
    def test_none_body_is_unchanged(self):
        self.assertEqual(deploy_toolbox.connection_body("None", "https://h/mcp"), {"properties": {
            "category": "RemoteTool", "target": "https://h/mcp", "authType": "None", "metadata": {"type": "custom_MCP"}}})

    def test_user_entra_token_body(self):
        props = deploy_toolbox.connection_body("UserEntraToken", "https://h/mcp", audience="api://x")["properties"]
        self.assertEqual((props["authType"], props["audience"]), ("UserEntraToken", "api://x"))
        self.assertNotIn("credentials", props)
        self.assertNotIn("authorizationUrl", props)

    def test_oauth2_body(self):
        props = deploy_toolbox.connection_body("OAuth2", "https://h/mcp", tenant="t", client_id="c", client_secret="s",
                                               scopes=["api://a/user_impersonation", "offline_access"])["properties"]
        self.assertEqual(props["authorizationUrl"], "https://login.microsoftonline.com/t/oauth2/v2.0/authorize")
        self.assertEqual(props["tokenUrl"], "https://login.microsoftonline.com/t/oauth2/v2.0/token")
        self.assertEqual(props["refreshUrl"], props["tokenUrl"])
        self.assertEqual(props["scopes"], ["api://a/user_impersonation", "offline_access"])
        self.assertEqual(props["credentials"], {"clientId": "c", "clientSecret": "s"})
        self.assertNotIn("audience", props)

    def test_auth_type_names(self):
        self.assertEqual(deploy_toolbox.auth_type("user-entra-token"), "UserEntraToken")
        self.assertEqual(deploy_toolbox.auth_type("OAUTH2"), "OAuth2")
        self.assertEqual(deploy_toolbox.auth_type("none"), "None")
        with self.assertRaises(Exception):
            deploy_toolbox.auth_type("ApiKey")

    def test_stale_secrets(self):
        creds = [{"keyId": "1", "displayName": "foundry a/p/c"}, {"keyId": "2", "displayName": "foundry a/p/c"},
                 {"keyId": "3", "displayName": "other"}, {"keyId": "4", "displayName": None}]
        self.assertEqual(deploy_toolbox.stale_secrets(creds, "foundry a/p/c", keep="2"), ["1"])
        self.assertEqual(deploy_toolbox.stale_secrets(creds, "foundry a/p/c"), ["1", "2"])


class DeployToolboxDryRunTests(unittest.TestCase):
    ARGS = ["--mcp-url", "https://h/mcp", "--resource-group", "rg", "--subscription", "sub", "--dry-run"]

    def test_none_keeps_the_ga_api(self):
        out = run(deploy_toolbox, self.ARGS)
        self.assertIn("/accounts/acct/projects/proj/connections/sample-mcp-conn?api-version=2025-06-01", out)
        self.assertIn(f"POST {PROJECT}/toolboxes/tb/versions?api-version=v1", out)
        self.assertNotIn("Password", out)

    def test_user_entra_token_defaults_the_audience(self):
        out = run(deploy_toolbox, self.ARGS + ["--auth-type", "UserEntraToken"])
        self.assertIn("?api-version=2025-04-01-preview", out)
        self.assertIn('"audience": "api://api-id"', out)

    def test_user_entra_token_needs_an_audience(self):
        env = {k: v for k, v in ENV.items() if k != "ENTRA_API_APP_ID"}
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            run(deploy_toolbox, self.ARGS + ["--auth-type", "UserEntraToken"], env=env)

    def test_oauth2_plans_rotation_without_a_secret(self):
        out = run(deploy_toolbox, self.ARGS + ["--auth-type", "oauth2", "--connection", "c"])
        self.assertIn("addPassword (displayName 'Foundry connection acct/proj/c', 180 days)", out)
        self.assertIn("removePassword for older secrets named 'Foundry connection acct/proj/c'", out)
        self.assertIn('"clientSecret": "<new client secret>"', out)
        self.assertIn('"clientId": "client-id"', out)
        self.assertIn('"api://api-id/user_impersonation"', out)
        self.assertIn("login.microsoftonline.com/tenant-id/oauth2/v2.0/authorize", out)
        self.assertIn("properties.redirectUrl", out)

    def test_oauth2_secret_from_env_skips_rotation(self):
        out = run(deploy_toolbox, self.ARGS + ["--auth-type", "OAuth2", "--client-secret-env", "MY_SECRET"])
        self.assertNotIn("Password", out)
        self.assertIn('"clientSecret": "<value of $MY_SECRET>"', out)

    def test_oauth2_delete_removes_its_secrets(self):
        out = run(deploy_toolbox, ["--resource-group", "rg", "--subscription", "sub", "--dry-run", "--delete",
                                   "--auth-type", "OAuth2"])
        self.assertIn(f"DELETE {PROJECT}/toolboxes/tb?api-version=v1", out)
        self.assertIn("removePassword for each secret named 'Foundry connection acct/proj/sample-mcp-conn'", out)
        self.assertIn("without the connection's redirect URI", out)

    def test_unregister_redirect_removes_only_that_uri(self):
        app = {"id": "obj", "web": {"redirectUris": ["https://a/redirect/1", "https://a/redirect/2"]}}
        with mock.patch.object(deploy_toolbox, "graph") as graph:
            self.assertTrue(deploy_toolbox.unregister_redirect(app, "https://a/redirect/1"))
            graph.assert_called_once_with("PATCH", "/applications/obj", {"web": {"redirectUris": ["https://a/redirect/2"]}})
            graph.reset_mock()
            self.assertFalse(deploy_toolbox.unregister_redirect(app, "https://a/redirect/3"))
            self.assertFalse(deploy_toolbox.unregister_redirect(app, None))
            graph.assert_not_called()

    def test_flags_for_another_auth_type_are_rejected(self):
        for extra in (["--audience", "api://x"], ["--auth-type", "OAuth2", "--audience", "api://x"],
                      ["--client-id", "c"], ["--auth-type", "UserEntraToken", "--scopes", "s"]):
            with self.subTest(extra=extra), self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
                run(deploy_toolbox, self.ARGS + extra)


class DeployAgentTests(unittest.TestCase):
    def test_named_agent_and_toolbox(self):
        out = run(deploy_agent, ["--agent", "a-entra", "--toolbox", "tb-entra", "--dry-run"])
        first, body = out.split("\n", 1)
        self.assertEqual(first, f"POST {PROJECT}/agents/a-entra/versions?api-version=v1")
        tool = json.loads(body)["definition"]["tools"][0]
        self.assertEqual(tool["server_url"], f"{PROJECT}/toolboxes/tb-entra/mcp?api-version=v1")
        self.assertEqual(tool["require_approval"], "never")

    def test_names_default_from_env(self):
        out = run(deploy_agent, ["--dry-run"])
        self.assertIn(f"{PROJECT}/agents/agent/versions", out)
        self.assertIn(f"{PROJECT}/toolboxes/tb/mcp", out)

    def test_delete(self):
        out = run(deploy_agent, ["--agent", "a-entra", "--delete", "--dry-run"])
        self.assertEqual(out.strip(), f"DELETE {PROJECT}/agents/a-entra?api-version=v1")


if __name__ == "__main__":
    unittest.main()

"""Pure helpers of scripts/setup_entra.py, and its roles against the sample server's region policy."""
import importlib.util
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))  # setup_entra imports _az
sys.path.insert(0, str(ROOT / "sample-mcp-server"))
spec = importlib.util.spec_from_file_location("setup_entra", ROOT / "scripts" / "setup_entra.py")
setup_entra = importlib.util.module_from_spec(spec)
sys.modules["setup_entra"] = setup_entra
spec.loader.exec_module(setup_entra)
import data  # noqa: E402


class SetupEntraTests(unittest.TestCase):
    def test_roles_match_the_server(self):
        self.assertEqual(set(setup_entra.ROLES), set(data.REGION_ROLES) | {data.ALL_REGIONS_ROLE})

    def test_role_values(self):
        self.assertEqual(setup_entra.role_values(["apac", "Customers.Read.Europe", "APAC"]),
                         ["Customers.Read.APAC", "Customers.Read.Europe"])
        self.assertEqual(setup_entra.role_values([]), [])
        with self.assertRaises(SystemExit):
            setup_entra.role_values(["Antarctica"])

    def test_with_missing_keeps_existing_entries(self):
        existing = [{"id": "keep", "value": "user_impersonation", "isEnabled": True, "type": "Admin"}]
        merged = setup_entra.with_missing(existing, [{"value": "user_impersonation", "type": "User"},
                                                     {"value": "other", "type": "User"}])
        self.assertEqual(merged[0], existing[0])
        self.assertEqual([m["value"] for m in merged], ["user_impersonation", "other"])
        self.assertTrue(merged[1]["id"])

    def test_merge_access(self):
        existing = [{"resourceAppId": "api", "resourceAccess": [{"id": "a", "type": "Scope"}]},
                    {"resourceAppId": "x", "resourceAccess": [{"id": "x1", "type": "Role"}]}]
        needed = [{"resourceAppId": "api", "resourceAccess": [{"id": "a", "type": "Scope"}, {"id": "b", "type": "Scope"}]},
                  {"resourceAppId": "graph", "resourceAccess": [{"id": "g", "type": "Scope"}]}]
        merged = {e["resourceAppId"]: [a["id"] for a in e["resourceAccess"]]
                  for e in setup_entra.merge_access(existing, needed)}
        self.assertEqual(merged, {"api": ["a", "b"], "x": ["x1"], "graph": ["g"]})
        self.assertEqual(len(existing[0]["resourceAccess"]), 1)  # input not mutated

    def test_upsert_env(self):
        text = "FOUNDRY_PROJECT_ENDPOINT=https://x\n# ENTRA_TENANT_ID=<tenant>\nENTRA_API_APP_ID=old\n"
        out = setup_entra.upsert_env(text, {"ENTRA_TENANT_ID": "t", "ENTRA_API_APP_ID": "new", "ENTRA_OAUTH_CLIENT_APP_ID": "c"})
        self.assertEqual(out, "FOUNDRY_PROJECT_ENDPOINT=https://x\nENTRA_TENANT_ID=t\nENTRA_API_APP_ID=new\n"
                              "ENTRA_OAUTH_CLIENT_APP_ID=c\n")
        self.assertEqual(setup_entra.upsert_env("", {"A": "1"}), "A=1\n")


if __name__ == "__main__":
    unittest.main()

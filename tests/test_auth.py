import base64
import json
import time
import unittest
from unittest import mock

from azure.core.credentials import AccessToken
from azure.core.exceptions import ClientAuthenticationError

from evals import auth


class FakeCredential:
    def __init__(self, lifetime_s=3600, failures=0, error="transient"):
        self.calls, self.lifetime_s, self.failures, self.error = 0, lifetime_s, failures, error

    def get_token(self, scope):
        self.calls += 1
        if self.calls <= self.failures:
            raise ClientAuthenticationError(self.error)
        return AccessToken(f"{scope}#{self.calls}", int(time.time()) + self.lifetime_s)


class TokenCacheTests(unittest.TestCase):
    def setUp(self):
        auth._cache.clear()
        self.addCleanup(auth._cache.clear)

    def use(self, cred):
        patcher = mock.patch.object(auth, "_credential", cred)
        patcher.start()
        self.addCleanup(patcher.stop)
        return cred

    def test_token_is_cached_per_scope(self):
        cred = self.use(FakeCredential())
        self.assertEqual(auth.get_token("s1"), auth.get_token("s1"))
        auth.get_token("s2")
        self.assertEqual(cred.calls, 2)

    def test_token_near_expiry_is_refreshed(self):
        cred = self.use(FakeCredential(lifetime_s=auth.REFRESH_MARGIN_S - 1))
        auth.get_token("s")
        auth.get_token("s")
        self.assertEqual(cred.calls, 2)

    def test_transient_failures_are_retried(self):
        cred = self.use(FakeCredential(failures=1))
        with mock.patch.object(auth.time, "sleep"):
            self.assertTrue(auth.get_token("s").endswith("#2"))
        self.assertEqual(cred.calls, 2)

    def test_not_logged_in_fails_fast(self):
        cred = self.use(FakeCredential(failures=5, error="Please run 'az login' to set up an account"))
        with self.assertRaises(ClientAuthenticationError):
            auth.get_token("s")
        self.assertEqual(cred.calls, 1)


def jwt(**claims):
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return f"e30.{payload}.sig"


class CallerClaimsTests(unittest.TestCase):
    def test_identity_claims_from_the_token(self):
        token = jwt(oid="o-1", tid="t-1", name="Ada", upn="ada@example.com", unique_name="other", aud="x")
        with mock.patch.object(auth, "get_token", return_value=token) as get:
            self.assertEqual(auth.caller_claims("scope"),
                             {"oid": "o-1", "tid": "t-1", "name": "Ada", "username": "ada@example.com"})
        get.assert_called_once_with("scope")

    def test_username_prefers_preferred_username_and_missing_claims_are_omitted(self):
        token = jwt(oid="o-1", preferred_username="p@example.com", upn="u@example.com")
        with mock.patch.object(auth, "get_token", return_value=token):
            self.assertEqual(auth.caller_claims(), {"oid": "o-1", "username": "p@example.com"})


if __name__ == "__main__":
    unittest.main()

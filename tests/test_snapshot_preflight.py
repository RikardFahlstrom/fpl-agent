"""The auth preflight: a snapshot must not silently lose the squad half of the data."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fpl_agent import snapshot


class ReadinessTests(unittest.TestCase):
    def setUp(self):
        self._saved = {k: os.environ.get(k) for k in
                       ("FPL_AUTO_LOGIN", "FPL_EMAIL", "FPL_PASSWORD")}
        self.addCleanup(self._restore)
        for key in self._saved:
            os.environ.pop(key, None)
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.cache = Path(self._tmp.name) / "session.json"

    def _restore(self):
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def _readiness(self):
        with mock.patch.object(snapshot, "cache_path", return_value=self.cache):
            return snapshot.auth_readiness()

    def test_nothing_configured_is_incomplete(self):
        result = self._readiness()
        self.assertFalse(result.complete)
        self.assertIn("FPL_AUTO_LOGIN", result.missing)
        self.assertIn("FPL_EMAIL", result.missing)
        self.assertIn("FPL_PASSWORD", result.missing)

    def test_credentials_without_the_flag_still_incomplete(self):
        """Credentials alone do nothing: without FPL_AUTO_LOGIN no session is created."""
        os.environ["FPL_EMAIL"] = "someone@example.com"
        os.environ["FPL_PASSWORD"] = "secret"
        result = self._readiness()
        self.assertFalse(result.complete)
        self.assertEqual(result.missing, ["FPL_AUTO_LOGIN"])

    def test_flag_with_credentials_is_complete(self):
        os.environ["FPL_AUTO_LOGIN"] = "true"
        os.environ["FPL_EMAIL"] = "someone@example.com"
        os.environ["FPL_PASSWORD"] = "secret"
        self.assertTrue(self._readiness().complete)

    def test_a_cached_session_removes_the_need_for_credentials(self):
        """Credentials only exist to create a session; a cached one is already a session."""
        self.cache.write_text("{}")
        os.environ["FPL_AUTO_LOGIN"] = "true"
        result = self._readiness()
        self.assertTrue(result.complete)
        self.assertEqual(result.missing, [])

    def test_blank_credentials_do_not_count_as_set(self):
        os.environ["FPL_AUTO_LOGIN"] = "true"
        os.environ["FPL_EMAIL"] = "   "
        os.environ["FPL_PASSWORD"] = ""
        result = self._readiness()
        self.assertFalse(result.complete)
        self.assertIn("FPL_EMAIL", result.missing)

    def test_detail_never_contains_the_credentials(self):
        os.environ["FPL_AUTO_LOGIN"] = "true"
        os.environ["FPL_EMAIL"] = "someone@example.com"
        os.environ["FPL_PASSWORD"] = "hunter2"
        text = " ".join(self._readiness().detail)
        self.assertNotIn("hunter2", text)
        self.assertNotIn("someone@example.com", text)


if __name__ == "__main__":
    unittest.main()

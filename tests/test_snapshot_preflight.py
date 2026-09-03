"""The auth preflight: a snapshot must not silently lose the squad half of the data."""
import argparse
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fpl_agent import headless_auth
from fpl_agent.engine import snapshot, storage


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


class SessionEstablishmentTests(unittest.IsolatedAsyncioTestCase):
    """Regression: the preflight said the squad would be captured, then nothing logged in.

    Checking the configuration proves the settings exist. It does not produce a session,
    and the original code created a bare client and reported success while silently
    capturing the market alone.
    """

    def setUp(self):
        self._saved = os.environ.get("FPL_AUTO_LOGIN")
        self.addCleanup(self._restore)

    def _restore(self):
        if self._saved is None:
            os.environ.pop("FPL_AUTO_LOGIN", None)
        else:
            os.environ["FPL_AUTO_LOGIN"] = self._saved

    async def test_without_auto_login_no_session_is_attempted(self):
        os.environ.pop("FPL_AUTO_LOGIN", None)
        with mock.patch.object(headless_auth, "bootstrap_session") as bootstrap:
            client, authenticated = await headless_auth.authenticated_client()
        bootstrap.assert_not_called()
        self.assertFalse(authenticated)
        self.assertIsNotNone(client)

    async def test_a_session_is_established_when_configured(self):
        os.environ["FPL_AUTO_LOGIN"] = "true"
        sentinel = object()
        with mock.patch.object(headless_auth, "bootstrap_session",
                               return_value="session-1") as bootstrap, \
             mock.patch.object(headless_auth.sessions, "get_client", return_value=sentinel):
            client, authenticated = await headless_auth.authenticated_client()
        bootstrap.assert_awaited_once()
        self.assertTrue(authenticated)
        self.assertIs(client, sentinel)

    async def test_a_failed_login_reports_unauthenticated(self):
        os.environ["FPL_AUTO_LOGIN"] = "true"
        with mock.patch.object(headless_auth, "bootstrap_session", return_value=None):
            client, authenticated = await headless_auth.authenticated_client()
        self.assertFalse(authenticated)
        self.assertIsNotNone(client, "the public market is still capturable")

    async def test_a_raising_login_does_not_crash_the_snapshot(self):
        os.environ["FPL_AUTO_LOGIN"] = "true"
        with mock.patch.object(headless_auth, "bootstrap_session",
                               side_effect=RuntimeError("chromium missing")):
            client, authenticated = await headless_auth.authenticated_client()
        self.assertFalse(authenticated)

    async def test_a_session_without_a_registered_client_is_not_authenticated(self):
        os.environ["FPL_AUTO_LOGIN"] = "true"
        with mock.patch.object(headless_auth, "bootstrap_session", return_value="session-1"), \
             mock.patch.object(headless_auth.sessions, "get_client", return_value=None):
            client, authenticated = await headless_auth.authenticated_client()
        self.assertFalse(authenticated)


def _history_row(element_id: int, round_: int = 2) -> dict:
    return {"element": element_id, "round": round_, "fixture": 1, "opponent_team": 2,
            "was_home": True, "minutes": 90, "total_points": 5, "starts": 1}


class StubClient:
    """A client that answers for some players and refuses for others, as FPL's API does.

    `failing` ids raise; `empty` ids return a real answer that happens to carry no
    history, which is what a player who has not appeared yet looks like.
    """

    def __init__(self, failing=(), empty=()):
        self.failing = set(failing)
        self.empty = set(empty)
        self.calls: list[int] = []

    async def get_element_summary(self, element_id: int) -> dict:
        self.calls.append(element_id)
        if element_id in self.failing:
            raise RuntimeError("503 Service Unavailable")
        if element_id in self.empty:
            return {"history": []}
        return {"history": [_history_row(element_id)]}

    async def close(self) -> None:
        pass


class BackfillFailureTests(unittest.IsolatedAsyncioTestCase):
    """Regression: a backfill in which every call failed reported success.

    The FPL API refuses element-summary requests intermittently. The old code caught each
    failure, logged a warning and returned normally, so settle saw a finished gameweek,
    read every absent player_gameweek row as a zero, and reported a confident bias
    against actuals that had never been fetched. That is the GW3 settle bug in a new coat.
    """

    def setUp(self):
        self.conn = storage.connect(":memory:")
        self.addCleanup(self.conn.close)

    def _players(self, element_ids):
        self.conn.executemany(
            "INSERT OR REPLACE INTO player VALUES (?,?,?,?,?,?,'t','t')",
            [(eid, f"P{eid}", "F", "S", 1, 3) for eid in element_ids])
        self.conn.commit()

    def _stored_rounds(self) -> set:
        return {r["element_id"] for r in
                self.conn.execute("SELECT element_id FROM player_gameweek")}

    async def test_failed_calls_are_counted_and_the_rest_are_still_written(self):
        ids = list(range(1, 21))
        self._players(ids)
        client = StubClient(failing={3, 7, 11})

        result = await snapshot.backfill_actuals(self.conn, client, ids)

        self.assertEqual(result.failed, 3)
        self.assertEqual(result.attempted, 20)
        self.assertAlmostEqual(result.failure_rate, 0.15)
        # The rows that did arrive are kept: a partial warehouse converges on the next
        # run, and throwing them away would cost data the caller can already reason about.
        self.assertEqual(result.rows, 17)
        self.assertEqual(self._stored_rounds(), set(ids) - {3, 7, 11})

    async def test_a_player_with_no_history_yet_is_not_a_failure(self):
        """The distinction the old code lost: refused-by-the-API vs has-not-played-yet.

        Both used to collapse into an empty list, so a total outage was indistinguishable
        from a league that had played no football.
        """
        ids = [1, 2, 3, 4]
        self._players(ids)
        client = StubClient(empty={2})

        result = await snapshot.backfill_actuals(self.conn, client, ids)

        self.assertEqual(result.failed, 0)
        self.assertEqual(result.failure_rate, 0.0)
        self.assertEqual(result.rows, 3)
        self.assertEqual(self._stored_rounds(), {1, 3, 4})

    async def test_a_clean_backfill_reports_no_failures(self):
        ids = [1, 2, 3]
        self._players(ids)
        result = await snapshot.backfill_actuals(self.conn, StubClient(), ids)
        self.assertEqual((result.rows, result.attempted, result.failed), (3, 3, 0))

    def test_the_failure_rate_of_an_empty_backfill_is_zero(self):
        """No players attempted is not a 100% failure; it must not trip the threshold."""
        result = snapshot.BackfillResult(rows=0, attempted=0, failed=0)
        self.assertEqual(result.failure_rate, 0.0)


class BackfillExitCodeTests(unittest.IsolatedAsyncioTestCase):
    """A nightly job that half-fetched the league must fail where someone can see it."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db = Path(self._tmp.name) / "fpl.db"
        conn = storage.connect(self.db)
        conn.executemany(
            "INSERT OR REPLACE INTO player VALUES (?,?,?,?,?,?,'t','t')",
            [(eid, f"P{eid}", "F", "S", 1, 3) for eid in range(1, 21)])
        conn.commit()
        conn.close()

    def _args(self):
        return argparse.Namespace(db=self.db, force=False, backfill=False,
                                  backfill_only=True, allow_partial=False, kind="test")

    async def _run_with(self, client):
        with mock.patch.object(snapshot, "authenticated_client",
                               return_value=(client, True)):
            return await snapshot._run(self._args())

    async def test_a_backfill_over_the_failure_threshold_exits_non_zero(self):
        client = StubClient(failing={3, 7, 11})  # 15%, well over the 5% tolerated
        with self.assertLogs(snapshot.logger, level="ERROR") as logs:
            code = await self._run_with(client)
        self.assertNotEqual(code, 0)
        self.assertIn("3 of 20", " ".join(logs.output))

    async def test_a_backfill_within_the_threshold_still_exits_zero(self):
        """One player in twenty failing is ordinary flakiness, not a broken warehouse."""
        client = StubClient(failing={3})  # 5%, at the limit and not over it
        self.assertEqual(await self._run_with(client), 0)

    async def test_a_clean_backfill_exits_zero(self):
        self.assertEqual(await self._run_with(StubClient()), 0)


if __name__ == "__main__":
    unittest.main()

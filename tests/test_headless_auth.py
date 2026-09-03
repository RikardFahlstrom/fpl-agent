import asyncio
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fpl_agent import headless_auth, tools
from fpl_agent.client import FPLClient
from fpl_agent.state import store


class _FakeResponse:
    def __init__(self, status_code: int, payload=None):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _FakeSession:
    """Stands in for httpx.AsyncClient, returning queued responses in order."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = 0

    async def get(self, url, headers=None, params=None):
        self.calls += 1
        return self._responses.pop(0)

    async def post(self, url, json=None, headers=None):
        self.calls += 1
        return self._responses.pop(0)


class EnvFlagTests(unittest.TestCase):
    def test_recognised_values_map_to_booleans(self) -> None:
        with mock.patch.dict(os.environ, {"FPL_TEST_FLAG": "yes"}):
            self.assertTrue(headless_auth.env_flag("FPL_TEST_FLAG"))
        with mock.patch.dict(os.environ, {"FPL_TEST_FLAG": "off"}):
            self.assertFalse(headless_auth.env_flag("FPL_TEST_FLAG"))

    def test_unset_returns_the_default(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertFalse(headless_auth.env_flag("FPL_TEST_FLAG"))
            self.assertTrue(headless_auth.env_flag("FPL_TEST_FLAG", default=True))

    def test_unparseable_value_is_rejected_rather_than_assumed(self) -> None:
        with mock.patch.dict(os.environ, {"FPL_TEST_FLAG": "maybe"}):
            with self.assertRaises(RuntimeError) as caught:
                headless_auth.env_flag("FPL_TEST_FLAG")
        self.assertIn("FPL_TEST_FLAG", str(caught.exception))


class CredentialTests(unittest.TestCase):
    def test_missing_variables_are_named_in_the_error(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(RuntimeError) as caught:
                headless_auth.load_credentials()
        message = str(caught.exception)
        self.assertIn("FPL_EMAIL", message)
        self.assertIn("FPL_PASSWORD", message)

    def test_partial_credentials_name_only_what_is_missing(self) -> None:
        with mock.patch.dict(os.environ, {"FPL_EMAIL": "a@b.c"}, clear=True):
            with self.assertRaises(RuntimeError) as caught:
                headless_auth.load_credentials()
        message = str(caught.exception)
        self.assertIn("FPL_PASSWORD", message)
        self.assertNotIn("FPL_EMAIL", message)


class TokenCacheTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.cache_file = Path(self._tmp.name) / "session.json"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _client(self) -> FPLClient:
        client = FPLClient()
        client.set_api_token("test-token")
        client.user_info = {"player": {"entry": 431892}}
        return client

    def test_cache_round_trips_and_is_owner_only(self) -> None:
        with mock.patch.dict(os.environ, {"FPL_TOKEN_CACHE": str(self.cache_file)}):
            headless_auth.save_cached_session(self._client(), expires_in=3600)
            data = headless_auth._read_cache()

        self.assertEqual(data["api_token"], "Bearer test-token")
        self.assertEqual(data["entry_id"], 431892)

        mode = stat.S_IMODE(self.cache_file.stat().st_mode)
        self.assertEqual(
            mode, 0o600, f"token cache must not be readable by others (got {oct(mode)})"
        )

    def test_expired_cache_is_ignored(self) -> None:
        with mock.patch.dict(os.environ, {"FPL_TOKEN_CACHE": str(self.cache_file)}):
            headless_auth.save_cached_session(self._client(), expires_in=-10)
            self.assertIsNone(headless_auth._read_cache())

    def test_unreadable_cache_does_not_raise(self) -> None:
        self.cache_file.write_text("{ not json")
        with mock.patch.dict(os.environ, {"FPL_TOKEN_CACHE": str(self.cache_file)}):
            self.assertIsNone(headless_auth._read_cache())

    def test_missing_cache_is_not_an_error(self) -> None:
        with mock.patch.dict(os.environ, {"FPL_TOKEN_CACHE": str(self.cache_file)}):
            self.assertIsNone(headless_auth._read_cache())

    async def test_rejected_cached_token_is_discarded(self) -> None:
        """A token the API no longer accepts must not be left on disk."""

        class _RejectedClient:
            def __init__(self, store=None):
                self.api_token = None
                self.user_info = None

            def set_api_token(self, token):
                self.api_token = token

            def set_reauth_hook(self, hook):
                pass

            async def get_me(self):
                raise RuntimeError("HTTP 401")

            async def close(self):
                pass

        with mock.patch.dict(os.environ, {"FPL_TOKEN_CACHE": str(self.cache_file)}):
            headless_auth.save_cached_session(self._client(), expires_in=3600)
            self.assertTrue(self.cache_file.exists())
            with mock.patch.object(headless_auth, "FPLClient", _RejectedClient):
                session_id = await headless_auth.load_cached_session()

        self.assertIsNone(session_id)
        self.assertFalse(
            self.cache_file.exists(), "a rejected token must be removed from disk"
        )

    async def test_valid_cached_token_restores_a_session(self) -> None:
        class _AcceptedClient:
            def __init__(self, store=None):
                self.api_token = None
                self.user_info = None

            def set_api_token(self, token):
                self.api_token = token

            def set_reauth_hook(self, hook):
                pass

            async def get_me(self):
                return {"player": {"entry": 431892}}

            async def close(self):
                pass

        with mock.patch.dict(os.environ, {"FPL_TOKEN_CACHE": str(self.cache_file)}):
            headless_auth.save_cached_session(self._client(), expires_in=3600)
            with mock.patch.object(headless_auth, "FPLClient", _AcceptedClient):
                session_id = await headless_auth.load_cached_session()

        try:
            self.assertIsNotNone(session_id)
            restored = store.get_client(session_id)
            self.assertEqual(restored.api_token, "Bearer test-token")
            self.assertTrue(self.cache_file.exists(), "a working token stays cached")
        finally:
            store.active_sessions.pop(session_id, None)


class InteractiveLoginPersistenceTests(unittest.IsolatedAsyncioTestCase):
    """An interactive login must not write a credential to disk by default."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.cache_file = Path(self._tmp.name) / "session.json"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    async def _login(self) -> None:
        class _Automation:
            def __init__(self, email, password):
                self.refresh_token = None
                self.expires_in = 3600
                self.failure_reason = None

            async def login_and_get_token(self):
                return "Bearer interactive-token"

        async def _set_login_success(request_id, session_id, client):
            client.user_info = {"player": {"entry": 1}}

        with mock.patch.object(headless_auth, "FPLAutomation", _Automation), \
             mock.patch.object(headless_auth.store, "set_login_success", _set_login_success):
            session_id, error = await headless_auth.establish_session("a@b.c", "pw")

        self.assertIsNone(error)
        self.assertIsNotNone(session_id)
        headless_auth.store.active_sessions.pop(session_id, None)

    async def test_interactive_login_does_not_cache_the_token(self) -> None:
        with mock.patch.dict(
            os.environ,
            {"FPL_TOKEN_CACHE": str(self.cache_file), "FPL_AUTO_LOGIN": "false"},
        ):
            await self._login()
        self.assertFalse(
            self.cache_file.exists(),
            "interactive logins must stay in memory, as documented",
        )

    async def test_unattended_login_does_cache_the_token(self) -> None:
        with mock.patch.dict(
            os.environ,
            {"FPL_TOKEN_CACHE": str(self.cache_file), "FPL_AUTO_LOGIN": "true"},
        ):
            await self._login()
        self.assertTrue(self.cache_file.exists())


class ReauthOnExpiryTests(unittest.IsolatedAsyncioTestCase):
    async def _client_with(self, responses) -> tuple[FPLClient, _FakeSession]:
        client = FPLClient()
        client.set_api_token("stale-token")
        session = _FakeSession(responses)
        client.session = session
        client._session_loop = asyncio.get_running_loop()
        return client, session

    async def test_expired_token_reauthenticates_once_and_retries(self) -> None:
        client, session = await self._client_with(
            [_FakeResponse(401), _FakeResponse(200, {"player": {"entry": 1}})]
        )
        calls = []

        async def hook(target: FPLClient) -> bool:
            calls.append(target)
            target.set_api_token("fresh-token")
            return True

        client.set_reauth_hook(hook)
        result = await client.get_me()

        self.assertEqual(result, {"player": {"entry": 1}})
        self.assertEqual(len(calls), 1, "re-auth should fire exactly once")
        self.assertEqual(session.calls, 2, "request should be retried once")
        self.assertEqual(client.api_token, "Bearer fresh-token")

    async def test_persistent_rejection_does_not_loop(self) -> None:
        client, session = await self._client_with(
            [_FakeResponse(401), _FakeResponse(401)]
        )
        calls = []

        async def hook(target: FPLClient) -> bool:
            calls.append(target)
            return True

        client.set_reauth_hook(hook)
        with self.assertRaises(RuntimeError):
            await client.get_me()

        self.assertEqual(len(calls), 1, "must not re-authenticate on the retry")
        self.assertEqual(session.calls, 2, "must stop after a single retry")

    async def test_failed_reauth_surfaces_the_original_error(self) -> None:
        client, session = await self._client_with([_FakeResponse(401)])

        async def hook(target: FPLClient) -> bool:
            return False

        client.set_reauth_hook(hook)
        with self.assertRaises(RuntimeError):
            await client.get_me()
        self.assertEqual(session.calls, 1, "no retry when re-auth reports failure")

    async def test_no_hook_behaves_as_before(self) -> None:
        client, session = await self._client_with([_FakeResponse(401)])
        with self.assertRaises(RuntimeError):
            await client.get_me()
        self.assertEqual(session.calls, 1)


class ReadOnlyGuardTests(unittest.IsolatedAsyncioTestCase):
    async def test_read_only_blocks_transfers_before_touching_the_account(self) -> None:
        previous = tools.get_active_session()
        # No session is registered, so if the guard fails to fire the tool would
        # report an authentication error instead of a read-only refusal.
        tools.set_active_session(None)
        try:
            with mock.patch.dict(os.environ, {"FPL_READ_ONLY": "true"}):
                result = await tools.make_transfers(["Salah"], ["Haaland"])
        finally:
            tools.set_active_session(previous)

        self.assertIn("read-only", result)
        self.assertIn("FPL_READ_ONLY", result)

    async def test_transfers_are_allowed_when_not_read_only(self) -> None:
        previous = tools.get_active_session()
        tools.set_active_session(None)
        try:
            with mock.patch.dict(os.environ, {"FPL_READ_ONLY": "false"}):
                result = await tools.make_transfers(["Salah"], ["Haaland"])
        finally:
            tools.set_active_session(previous)

        # Falls through the guard to the normal authentication check.
        self.assertIn("Not authenticated", result)


if __name__ == "__main__":
    unittest.main()

import asyncio
import json
import os
import stat
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import httpx

from fpl_agent import headless_auth
from fpl_agent.mcp import tools
from fpl_agent.client import FPLClient
from fpl_agent.sessions import sessions


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


class _FakeTokenEndpoint:
    """Stands in for httpx.AsyncClient around the refresh grant.

    Records the request so a test can assert the wire shape, not just that
    something was called.
    """

    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error
        self.requests = []

    def __call__(self, *args, **kwargs):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False

    async def post(self, url, data=None, headers=None):
        self.requests.append({"url": url, "data": data, "headers": headers})
        if self.error is not None:
            raise self.error
        return self.response


class _NoBrowser:
    """Fails the test if a browser login is constructed."""

    def __init__(self, *args, **kwargs):
        raise AssertionError(
            "FPLAutomation was constructed: this path launched headless Chromium"
        )


class _AcceptingClient:
    """An FPLClient that accepts whatever token it is given."""

    def __init__(self, reference=None):
        self.api_token = None
        self.user_info = None

    def set_api_token(self, token):
        self.api_token = token if token.startswith("Bearer ") else f"Bearer {token}"

    def set_reauth_hook(self, hook):
        pass

    async def get_me(self):
        return {"player": {"entry": 431892}}

    async def close(self):
        pass


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

    def test_expired_cache_keeps_its_refresh_token(self) -> None:
        """Expiry makes the access token unusable, not the whole cache."""
        with mock.patch.dict(os.environ, {"FPL_TOKEN_CACHE": str(self.cache_file)}):
            headless_auth.save_cached_session(
                self._client(), refresh_token="refresh-1", expires_in=-10
            )
            data = headless_auth._read_cache()
            self.assertIsNotNone(data)
            self.assertFalse(headless_auth.token_is_fresh(data))
        self.assertEqual(data["refresh_token"], "refresh-1")

    def test_a_token_expiring_within_the_margin_is_already_expired(self) -> None:
        """A token that dies mid-run is no use; refresh it before the run starts."""
        with mock.patch.dict(os.environ, {"FPL_TOKEN_CACHE": str(self.cache_file)}):
            headless_auth.save_cached_session(self._client(), expires_in=30)
            self.assertFalse(headless_auth.token_is_fresh(headless_auth._read_cache()))
            headless_auth.save_cached_session(self._client(), expires_in=3600)
            self.assertTrue(headless_auth.token_is_fresh(headless_auth._read_cache()))

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
            def __init__(self, reference=None):
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
            def __init__(self, reference=None):
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
            restored = sessions.get_client(session_id)
            self.assertEqual(restored.api_token, "Bearer test-token")
            self.assertTrue(self.cache_file.exists(), "a working token stays cached")
        finally:
            sessions.active_sessions.pop(session_id, None)


class RefreshGrantTests(unittest.IsolatedAsyncioTestCase):
    """The cached token outlives its eight hours via the refresh grant, not a browser."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.cache_file = Path(self._tmp.name) / "session.json"
        self.env = {
            "FPL_TOKEN_CACHE": str(self.cache_file),
            "FPL_TOKEN_ENDPOINT": "https://stub.invalid/as/token",
            "FPL_OAUTH_CLIENT_ID": "test-client-id",
            "FPL_AUTO_LOGIN": "true",
        }

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _write_expired_cache(self, refresh_token="refresh-1") -> None:
        with mock.patch.dict(os.environ, self.env):
            headless_auth._write_cache(
                {
                    "api_token": "Bearer stale-token",
                    "entry_id": 431892,
                    "obtained_at": time.time() - 28_800,
                    "refresh_token": refresh_token,
                    "expires_at": time.time() - 60,
                }
            )

    def _cached(self) -> dict:
        return json.loads(self.cache_file.read_text())

    async def _load(self, endpoint: _FakeTokenEndpoint):
        """Run load_cached_session with the token endpoint stubbed and no browser."""
        with mock.patch.dict(os.environ, self.env), \
             mock.patch.object(headless_auth.httpx, "AsyncClient", endpoint), \
             mock.patch.object(headless_auth, "FPLAutomation", _NoBrowser), \
             mock.patch.object(headless_auth, "FPLClient", _AcceptingClient):
            return await headless_auth.load_cached_session()

    async def test_expired_cache_refreshes_instead_of_launching_a_browser(self) -> None:
        """The acceptance case: expired token in, session out, no Chromium."""
        self._write_expired_cache()
        endpoint = _FakeTokenEndpoint(
            _FakeResponse(200, {"access_token": "fresh-token", "expires_in": 28800})
        )

        session_id = await self._load(endpoint)
        try:
            self.assertIsNotNone(session_id, "the refresh grant should restore a session")
            self.assertEqual(sessions.get_client(session_id).api_token, "Bearer fresh-token")
        finally:
            sessions.active_sessions.pop(session_id, None)

        self.assertEqual(len(endpoint.requests), 1)
        request = endpoint.requests[0]
        self.assertEqual(request["url"], "https://stub.invalid/as/token")
        self.assertEqual(
            request["data"],
            {
                "grant_type": "refresh_token",
                "refresh_token": "refresh-1",
                "client_id": "test-client-id",
            },
            "the refresh must be the RFC 6749 public-client form post",
        )
        self.assertEqual(
            request["headers"]["Content-Type"], "application/x-www-form-urlencoded"
        )

    async def test_a_rotated_refresh_token_is_persisted(self) -> None:
        """Dropping a rotated token would silently restore nightly browser logins."""
        self._write_expired_cache()
        endpoint = _FakeTokenEndpoint(
            _FakeResponse(
                200,
                {
                    "access_token": "fresh-token",
                    "refresh_token": "refresh-2",
                    "expires_in": 28800,
                },
            )
        )
        session_id = await self._load(endpoint)
        sessions.active_sessions.pop(session_id, None)

        self.assertEqual(self._cached()["refresh_token"], "refresh-2")

    async def test_a_response_without_rotation_keeps_the_old_refresh_token(self) -> None:
        """RFC 6749 makes a new refresh token optional; absence is not a deletion."""
        self._write_expired_cache()
        endpoint = _FakeTokenEndpoint(
            _FakeResponse(200, {"access_token": "fresh-token", "expires_in": 28800})
        )
        session_id = await self._load(endpoint)
        sessions.active_sessions.pop(session_id, None)

        self.assertEqual(self._cached()["refresh_token"], "refresh-1")

    async def test_the_refreshed_cache_keeps_the_entry_id(self) -> None:
        """The token response knows nothing about the entry; the cache must not forget it."""
        self._write_expired_cache()
        endpoint = _FakeTokenEndpoint(
            _FakeResponse(200, {"access_token": "fresh-token", "expires_in": 28800})
        )
        session_id = await self._load(endpoint)
        sessions.active_sessions.pop(session_id, None)

        cached = self._cached()
        self.assertEqual(cached["entry_id"], 431892)
        self.assertEqual(cached["api_token"], "Bearer fresh-token")
        self.assertTrue(headless_auth.token_is_fresh(cached))

    async def test_a_refused_refresh_falls_back_without_raising(self) -> None:
        self._write_expired_cache()
        endpoint = _FakeTokenEndpoint(_FakeResponse(400, {"error": "invalid_grant"}))

        session_id = await self._load(endpoint)

        self.assertIsNone(session_id, "a refused refresh must fall back, not fake a session")
        self.assertEqual(self._cached()["refresh_token"], "refresh-1")

    async def test_an_unreachable_token_endpoint_falls_back_without_raising(self) -> None:
        """A network blip must not take the nightly job down, nor burn the refresh token."""
        self._write_expired_cache()
        endpoint = _FakeTokenEndpoint(error=httpx.ConnectError("no route to host"))

        session_id = await self._load(endpoint)

        self.assertIsNone(session_id)
        self.assertTrue(
            self.cache_file.exists(),
            "a transient failure must not discard the refresh token",
        )

    async def test_a_cache_with_no_refresh_token_reaches_no_endpoint(self) -> None:
        self._write_expired_cache(refresh_token=None)
        endpoint = _FakeTokenEndpoint(_FakeResponse(200, {"access_token": "unused"}))

        session_id = await self._load(endpoint)

        self.assertIsNone(session_id)
        self.assertEqual(endpoint.requests, [])

    async def test_reauth_hook_refreshes_before_reaching_for_a_browser(self) -> None:
        """A 401 mid-run is what the refresh grant is for."""
        self._write_expired_cache()
        endpoint = _FakeTokenEndpoint(
            _FakeResponse(200, {"access_token": "fresh-token", "expires_in": 28800})
        )
        client = FPLClient()
        client.set_api_token("stale-token")

        with mock.patch.dict(os.environ, self.env), \
             mock.patch.object(headless_auth.httpx, "AsyncClient", endpoint), \
             mock.patch.object(headless_auth, "FPLAutomation", _NoBrowser):
            reauthenticated = await headless_auth.reauth_hook(client)

        self.assertTrue(reauthenticated)
        self.assertEqual(client.api_token, "Bearer fresh-token")
        self.assertEqual(len(endpoint.requests), 1)


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
             mock.patch.object(headless_auth.sessions, "set_login_success", _set_login_success):
            session_id, error = await headless_auth.establish_session("a@b.c", "pw")

        self.assertIsNone(error)
        self.assertIsNotNone(session_id)
        headless_auth.sessions.active_sessions.pop(session_id, None)

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

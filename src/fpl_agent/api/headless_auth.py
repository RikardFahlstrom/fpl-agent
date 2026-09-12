"""Credential-based authentication for unattended (scheduled) runs.

The interactive flow in `web.py` asks a human to submit a form. Everything after
that submit is identical for an unattended run, so the shared part lives here in
`establish_session` and both callers use it.

The browser is the last resort, not the first move. A cached access token lives
eight hours and the scheduled run comes round every twenty-four, so the order is
always: a live cached token, then the OAuth refresh grant, then Chromium.

Nothing in this module is used unless `FPL_AUTO_LOGIN` is enabled, so the
existing interactive behaviour is unchanged by default.
"""

import json
import logging
import os
import time
import uuid
from pathlib import Path
from typing import Any, Optional

import httpx

from .auth import FPLAutomation
from .client import FPLClient
from .reference import reference
from .sessions import sessions

logger = logging.getLogger("fpl_headless_auth")

_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}

# The account service is PingFederate at https://account.premierleague.com/as. Its
# discovery document lists `refresh_token` in grant_types_supported, and the FPL
# single-page app is a public client using PKCE with no client secret, so a
# refresh is the plain RFC 6749 section 6 form post with no authentication.
# Both values are overridable from the environment: a client-id rotation should
# be a config change rather than a code change, and the tests point
# FPL_TOKEN_ENDPOINT at a local stub.
DEFAULT_TOKEN_ENDPOINT = "https://account.premierleague.com/as/token"
DEFAULT_OAUTH_CLIENT_ID = "bfcbaf69-aade-4c1b-8f00-c1cb8a193030"

# Treat a token expiring within this many seconds as already expired: the host
# clock may be skewed, and a long capture run must not lose its token half way
# through.
EXPIRY_MARGIN_SECONDS = 120


def env_flag(name: str, default: bool = False) -> bool:
    """Read a boolean environment variable, rejecting values that are neither."""
    configured = os.environ.get(name, "").strip().lower()
    if not configured:
        return default
    if configured in _TRUE:
        return True
    if configured in _FALSE:
        return False
    raise RuntimeError(f"{name} must be true or false when set (got {configured!r}).")


def load_credentials() -> tuple[str, str]:
    """Read FPL credentials from the environment, naming whatever is missing."""
    email = os.environ.get("FPL_EMAIL", "").strip()
    password = os.environ.get("FPL_PASSWORD", "")
    missing = [
        name
        for name, value in (("FPL_EMAIL", email), ("FPL_PASSWORD", password))
        if not value
    ]
    if missing:
        raise RuntimeError(
            "Unattended login needs " + " and ".join(missing) + " to be set. "
            "Inject them from your secret store; never commit them."
        )
    return email, password


# --------------------------------------------------------------------------
# Token cache
# --------------------------------------------------------------------------
# The cached token is a bearer credential for the FPL account and is enough to
# execute transfers. It is written 0600 and should be treated as being as
# sensitive as the password itself.


def cache_path() -> Path:
    configured = os.environ.get("FPL_TOKEN_CACHE", "").strip()
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".config" / "fpl-mcp" / "session.json"


def _write_cache(payload: dict[str, Any]) -> None:
    """Write the cache atomically and owner-only.

    Separate from `save_cached_session` because a refresh produces a new token
    with no `FPLClient` behind it, and both paths must write the same shape.
    """
    path = cache_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # Create with 0600 from the outset rather than chmod-ing afterwards, so the
        # token is never briefly readable by other users on the host.
        tmp = path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as handle:
            json.dump(payload, handle)
        os.replace(tmp, path)
        logger.info("Stored FPL session token at %s", path)
    except OSError as error:
        # A cache failure must not break an otherwise working login.
        logger.warning("Could not write the FPL token cache (%s).", error)


def save_cached_session(
    client: FPLClient,
    *,
    refresh_token: Optional[str] = None,
    expires_in: Optional[int] = None,
) -> None:
    """Persist the session so a restart does not need another browser login."""
    _write_cache(
        {
            "api_token": client.api_token,
            "entry_id": (client.user_info or {}).get("player", {}).get("entry"),
            "obtained_at": time.time(),
            "refresh_token": refresh_token,
            "expires_at": time.time() + expires_in if expires_in else None,
        }
    )


def save_refreshed_session(previous: dict, token: dict) -> dict:
    """Cache a refreshed access token, carrying forward what the grant did not return.

    Two things are only in the old cache and must survive the refresh:

    - **The refresh token, when the response does not rotate it.** PingFederate
      may or may not return a new `refresh_token`; RFC 6749 section 6 makes it
      optional. Overwriting the old one with the absent new one would leave a
      cache that cannot refresh again, and it would look like it worked - the
      next run would silently go back to launching Chromium.
    - **`entry_id`**, which the token response knows nothing about and which
      would otherwise drop to null on every refresh.

    Returns the payload that was written.
    """
    access = token.get("access_token")
    rotated = token.get("refresh_token")
    rotated = rotated if isinstance(rotated, str) and rotated else None
    expires_in = token.get("expires_in")
    payload: dict[str, Any] = {
        "api_token": access if access.startswith("Bearer ") else f"Bearer {access}",
        "entry_id": previous.get("entry_id"),
        "obtained_at": time.time(),
        "refresh_token": rotated or previous.get("refresh_token"),
        "expires_at": (
            time.time() + expires_in
            if isinstance(expires_in, (int, float))
            else None
        ),
    }
    logger.info(
        "Refreshed the access token (rotated refresh token %s, expires_in %s).",
        "present" if rotated else "absent",
        expires_in if isinstance(expires_in, (int, float)) else "unstated",
    )
    _write_cache(payload)
    return payload


def clear_cached_session() -> None:
    try:
        cache_path().unlink(missing_ok=True)
    except OSError as error:
        logger.warning("Could not remove the FPL token cache (%s).", error)


def _read_cache() -> Optional[dict]:
    """Return the cache as written, or None if there is nothing usable in it.

    An expired cache is *not* nothing: it still carries the refresh token, which
    is the whole point of having one. Expiry is asked separately, via
    `token_is_fresh`, so the caller can refresh instead of reaching for a browser.
    """
    path = cache_path()
    try:
        with path.open() as handle:
            data = json.load(handle)
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError) as error:
        logger.warning("Ignoring an unreadable FPL token cache (%s).", error)
        return None
    if not isinstance(data, dict) or not data.get("api_token"):
        return None
    return data


def token_is_fresh(data: dict) -> bool:
    """Whether the cached access token can still be used without refreshing."""
    expires_at = data.get("expires_at")
    if not isinstance(expires_at, (int, float)):
        # An unknown expiry is not an expired one; let /me be the judge.
        return True
    return expires_at > time.time() + EXPIRY_MARGIN_SECONDS


# --------------------------------------------------------------------------
# Refresh grant
# --------------------------------------------------------------------------


def token_endpoint() -> str:
    return os.environ.get("FPL_TOKEN_ENDPOINT", "").strip() or DEFAULT_TOKEN_ENDPOINT


def oauth_client_id() -> str:
    return os.environ.get("FPL_OAUTH_CLIENT_ID", "").strip() or DEFAULT_OAUTH_CLIENT_ID


async def refresh_access_token(refresh_token: Optional[str]) -> Optional[dict]:
    """Trade a refresh token for a new access token, or return None.

    The cached access token lives eight hours and the scheduled run comes round
    every twenty-four, so without this every unattended run drives headless
    Chromium at the account service - the flakiest thing in the system and the
    one that earns a "too many attempts".

    Never raises: a dead refresh token, a refusal or a network failure must
    degrade to a browser login, not take the nightly job down with it.
    """
    if not refresh_token:
        return None
    try:
        async with httpx.AsyncClient(timeout=30.0) as session:
            response = await session.post(
                token_endpoint(),
                # RFC 6749 section 6, public client: form-encoded, no secret.
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": refresh_token,
                    "client_id": oauth_client_id(),
                },
                headers={
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Accept": "application/json",
                    "User-Agent": "Mozilla/5.0",
                },
            )
        if response.status_code >= 400:
            # Log the status, never the body: it can echo the token back.
            logger.info(
                "The FPL refresh grant was refused (HTTP %s).", response.status_code
            )
            return None
        data = response.json()
    except Exception as error:
        logger.info(
            "Could not reach the FPL token endpoint (%s: %s).",
            type(error).__name__,
            error,
        )
        return None
    if not isinstance(data, dict) or not isinstance(data.get("access_token"), str):
        # Name the fields, never their values - the style `auth.py` uses.
        fields = ", ".join(sorted(str(key) for key in data)) if isinstance(data, dict) else "none"
        logger.info("The refresh response carried no access token (fields: %s).", fields)
        return None
    return data


async def _refresh_cached_token(data: dict) -> Optional[dict]:
    """Refresh the token in `data` and persist the result. Returns the new cache."""
    token = await refresh_access_token(data.get("refresh_token"))
    if not token:
        return None
    return save_refreshed_session(data, token)


# --------------------------------------------------------------------------
# Session restore
# --------------------------------------------------------------------------


async def _session_from_cache(data: dict) -> Optional[str]:
    """Register a session for a cached token, or None if /me rejects it."""
    client = FPLClient(reference=reference)
    client.set_api_token(data["api_token"])
    client.set_reauth_hook(reauth_hook)
    try:
        client.user_info = await client.get_me()
    except Exception as error:
        logger.info("The cached FPL token was rejected by /me (%s).", error)
        await client.close()
        return None

    session_id = str(uuid.uuid4())
    sessions.active_sessions[session_id] = client
    # Match the interactive path: dispose this loop's pool so the MCP loop can
    # lazily create its own without tripping the loop-boundary guard in FPLClient.
    await client.close()
    return session_id


async def load_cached_session() -> Optional[str]:
    """Rebuild a session from the token cache, refreshing it if need be.

    Order: a live cached token, then the refresh grant, then - only if both are
    gone - the browser, which the caller reaches by getting None back.
    """
    data = _read_cache()
    if not data:
        return None

    refreshed = False
    if not token_is_fresh(data):
        logger.info("The cached FPL token has expired; trying the refresh grant.")
        renewed = await _refresh_cached_token(data)
        if not renewed:
            # Leave the cache in place. The refresh may have failed for a
            # transient reason, and that refresh token is the only thing
            # standing between the nightly job and a browser login.
            logger.info(
                "Could not refresh the expired FPL token; falling back to a browser login."
            )
            return None
        data, refreshed = renewed, True

    session_id = await _session_from_cache(data)
    if session_id is None and not refreshed:
        # The cache thought the token was live but the API disagreed - clock
        # drift, or a revocation. One refresh before giving up on the cache.
        renewed = await _refresh_cached_token(data)
        if renewed:
            data, refreshed = renewed, True
            session_id = await _session_from_cache(data)

    if session_id is None:
        clear_cached_session()
        logger.info(
            "The cached FPL session could not be revived; falling back to a browser login."
        )
        return None

    if refreshed:
        logger.info("Refreshed the FPL access token; no browser login needed.")
    else:
        logger.info("Restored the FPL session from cache; no browser login needed.")
    return session_id


# --------------------------------------------------------------------------
# Login
# --------------------------------------------------------------------------


async def establish_session(
    email: str,
    password: str,
    request_id: Optional[str] = None,
) -> tuple[Optional[str], Optional[str]]:
    """Drive the browser login and register the resulting session.

    Shared by the interactive web form and the unattended path.
    Returns (session_id, error); exactly one is set.
    """
    request_id = request_id or str(uuid.uuid4())
    if request_id not in sessions.pending_logins:
        sessions.create_login_request(request_id)

    auth = FPLAutomation(email, password)
    token = await auth.login_and_get_token()

    if not token:
        failure = auth.failure_reason or "Could not capture an authenticated FPL session."
        sessions.set_login_failure(request_id, failure)
        return None, failure

    session_id = str(uuid.uuid4())
    client = FPLClient(reference=reference)
    client.set_api_token(token)
    client.set_reauth_hook(reauth_hook)
    # Fetches /me, stores user_info, and closes this loop's HTTP pool.
    await sessions.set_login_success(request_id, session_id, client)
    # Only persist for unattended runs. An interactive login keeps the token in
    # memory as it always has, rather than silently writing a credential to disk.
    if env_flag("FPL_AUTO_LOGIN"):
        save_cached_session(
            client,
            refresh_token=auth.refresh_token,
            expires_in=auth.expires_in,
        )
    return session_id, None


async def authenticate_headless() -> Optional[str]:
    """Log in using credentials from the environment, with no human present."""
    email, password = load_credentials()
    logger.info("Starting an unattended FPL login for %s.", email)
    session_id, error = await establish_session(email, password)
    if error:
        logger.error("Unattended FPL login failed: %s", error)
        return None
    return session_id


async def bootstrap_session() -> Optional[str]:
    """Restore a session at startup: cache first, then a credential login."""
    session_id = await load_cached_session()
    if session_id:
        sessions.active_session_id = session_id
        return session_id

    try:
        session_id = await authenticate_headless()
    except RuntimeError as error:
        logger.error("%s", error)
        return None
    if session_id:
        sessions.active_session_id = session_id
    return session_id


async def reauth_hook(client: FPLClient) -> bool:
    """Re-authenticate an existing client in place after a 401.

    Registered on clients so that every tool benefits without any tool knowing
    about re-authentication.
    """
    if not env_flag("FPL_AUTO_LOGIN"):
        return False

    # A 401 mid-run is exactly what the refresh grant is for; the browser is the
    # fallback, not the first move.
    cached = _read_cache()
    if cached:
        renewed = await _refresh_cached_token(cached)
        if renewed:
            client.set_api_token(renewed["api_token"])
            logger.info(
                "Refreshed the FPL access token after a 401; no browser login needed."
            )
            return True

    logger.info("Falling back to a browser login after a 401.")
    try:
        session_id = await authenticate_headless()
    except RuntimeError as error:
        logger.error("%s", error)
        return False
    if not session_id:
        return False

    refreshed = sessions.get_client(session_id)
    if not refreshed or not refreshed.api_token:
        return False
    # Mutate the client the tools already hold rather than swapping it out.
    client.set_api_token(refreshed.api_token)
    client.user_info = refreshed.user_info
    sessions.active_session_id = session_id
    logger.info("Re-authenticated the FPL session after an expired token.")
    return True


async def authenticated_client() -> tuple[FPLClient, bool]:
    """Establish a session if one is configured, and return the client to capture with.

    Checking the configuration is not the same as having a session: the preflight only
    proves the settings exist, and something has to actually log in. Returns the
    authenticated client when that succeeds, otherwise a bare client that can still read
    the public market.
    """
    if not env_flag("FPL_AUTO_LOGIN"):
        return FPLClient(reference=reference), False

    try:
        session_id = await bootstrap_session()
    except Exception as e:
        logger.error("could not establish a session: %s", e)
        return FPLClient(reference=reference), False

    if not session_id:
        logger.error(
            "login did not produce a session. The credential path drives a headless "
            "browser, so check `uv run playwright install chromium` has been run and "
            "that FPL_EMAIL / FPL_PASSWORD are correct.")
        return FPLClient(reference=reference), False

    client = sessions.get_client(session_id)
    if client is None:
        logger.error("session %s established but no client was registered", session_id)
        return FPLClient(reference=reference), False

    logger.info("session established")
    return client, True

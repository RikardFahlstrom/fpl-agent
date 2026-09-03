"""Credential-based authentication for unattended (scheduled) runs.

The interactive flow in `web.py` asks a human to submit a form. Everything after
that submit is identical for an unattended run, so the shared part lives here in
`establish_session` and both callers use it.

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

from .auth import FPLAutomation
from .client import FPLClient
from .state import store

logger = logging.getLogger("fpl_headless_auth")

_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}


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


def save_cached_session(
    client: FPLClient,
    *,
    refresh_token: Optional[str] = None,
    expires_in: Optional[int] = None,
) -> None:
    """Persist the session so a restart does not need another browser login."""
    path = cache_path()
    payload: dict[str, Any] = {
        "api_token": client.api_token,
        "entry_id": (client.user_info or {}).get("player", {}).get("entry"),
        "obtained_at": time.time(),
        "refresh_token": refresh_token,
        "expires_at": time.time() + expires_in if expires_in else None,
    }
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


def clear_cached_session() -> None:
    try:
        cache_path().unlink(missing_ok=True)
    except OSError as error:
        logger.warning("Could not remove the FPL token cache (%s).", error)


def _read_cache() -> Optional[dict]:
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
    expires_at = data.get("expires_at")
    if isinstance(expires_at, (int, float)) and expires_at <= time.time():
        logger.info("Cached FPL token has expired; a fresh login is needed.")
        return None
    return data


async def load_cached_session() -> Optional[str]:
    """Rebuild a session from the token cache, validating it against /me."""
    data = _read_cache()
    if not data:
        return None

    client = FPLClient(store=store)
    client.set_api_token(data["api_token"])
    client.set_reauth_hook(reauth_hook)
    try:
        client.user_info = await client.get_me()
    except Exception as error:
        logger.info("Cached FPL session is no longer usable (%s); discarding it.", error)
        await client.close()
        clear_cached_session()
        return None

    session_id = str(uuid.uuid4())
    store.active_sessions[session_id] = client
    # Match the interactive path: dispose this loop's pool so the MCP loop can
    # lazily create its own without tripping the loop-boundary guard in FPLClient.
    await client.close()
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
    if request_id not in store.pending_logins:
        store.create_login_request(request_id)

    auth = FPLAutomation(email, password)
    token = await auth.login_and_get_token()

    if not token:
        failure = auth.failure_reason or "Could not capture an authenticated FPL session."
        store.set_login_failure(request_id, failure)
        return None, failure

    session_id = str(uuid.uuid4())
    client = FPLClient(store=store)
    client.set_api_token(token)
    client.set_reauth_hook(reauth_hook)
    # Fetches /me, stores user_info, and closes this loop's HTTP pool.
    await store.set_login_success(request_id, session_id, client)
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
        store.active_session_id = session_id
        return session_id

    try:
        session_id = await authenticate_headless()
    except RuntimeError as error:
        logger.error("%s", error)
        return None
    if session_id:
        store.active_session_id = session_id
    return session_id


async def reauth_hook(client: FPLClient) -> bool:
    """Re-authenticate an existing client in place after a 401.

    Registered on clients so that every tool benefits without any tool knowing
    about re-authentication.
    """
    if not env_flag("FPL_AUTO_LOGIN"):
        return False
    try:
        session_id = await authenticate_headless()
    except RuntimeError as error:
        logger.error("%s", error)
        return False
    if not session_id:
        return False

    refreshed = store.get_client(session_id)
    if not refreshed or not refreshed.api_token:
        return False
    # Mutate the client the tools already hold rather than swapping it out.
    client.set_api_token(refreshed.api_token)
    client.user_info = refreshed.user_info
    store.active_session_id = session_id
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
        return FPLClient(store=store), False

    try:
        session_id = await bootstrap_session()
    except Exception as e:
        logger.error("could not establish a session: %s", e)
        return FPLClient(store=store), False

    if not session_id:
        logger.error(
            "login did not produce a session. The credential path drives a headless "
            "browser, so check `uv run playwright install chromium` has been run and "
            "that FPL_EMAIL / FPL_PASSWORD are correct.")
        return FPLClient(store=store), False

    client = store.get_client(session_id)
    if client is None:
        logger.error("session %s established but no client was registered", session_id)
        return FPLClient(store=store), False

    logger.info("session established")
    return client, True

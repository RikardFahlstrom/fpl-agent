"""Shared machinery for the MCP tools: the server, the session, and the guards.

The active session lives here and only here. It is module state that the login tools
write and every other tool reads, so it must have exactly one home - splitting the tools
across modules while leaving a copy in each would silently give each module its own
session.
"""

import functools
import inspect
import logging
import os
import uuid
from datetime import datetime, timezone

from mcp.server.fastmcp import FastMCP

from ...headless_auth import env_flag
from ...models import TransferPayload
from ...rotowire_scraper import RotoWireLineupScraper
from ...sessions import sessions
from ...reference import reference

# Define the server
mcp = FastMCP(
    "FPL Manager",
    host=os.environ.get("FPL_MCP_HOST", "127.0.0.1"),
    port=int(os.environ.get("FPL_MCP_PORT", "8021")),
)
BASE_URL = os.environ.get("FPL_AUTH_BASE_URL", "http://127.0.0.1:8020")

logger = logging.getLogger("fpl_tools")


# The interactive session, set by the login tools. Read through this module rather than
# imported by value, so every tool sees the same one.
_active_session_id: str | None = None


def set_active_session(session_id: str | None) -> None:
    global _active_session_id
    _active_session_id = session_id


def get_active_session() -> str | None:
    return _active_session_id


def _optional_int(value: object) -> int | None:
    """Normalize nullable numeric fields returned by the FPL API."""
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _pick_price_text(pick: dict) -> str:
    price = _optional_int(pick.get("selling_price"))
    if price is None:
        price = _optional_int(pick.get("purchase_price"))
    return f"£{price / 10:.1f}m" if price is not None else "Price unavailable"


def _mapping_contract(value: object) -> dict:
    """Describe a JSON object without returning any user values."""
    if not isinstance(value, dict):
        return {"type": type(value).__name__, "keys": [], "null_fields": []}
    return {
        "type": "object",
        "keys": sorted(str(key) for key in value),
        "null_fields": sorted(str(key) for key, item in value.items() if item is None),
        "field_types": {
            str(key): "null" if item is None else type(item).__name__
            for key, item in sorted(value.items(), key=lambda row: str(row[0]))
        },
    }


def _records_contract(value: object) -> dict:
    """Describe an array of JSON objects without returning record values."""
    records = value if isinstance(value, list) else []
    mappings = [record for record in records if isinstance(record, dict)]
    keys = {str(key) for record in mappings for key in record}
    null_fields = {
        str(key)
        for record in mappings
        for key, item in record.items()
        if item is None
    }
    return {
        "type": "array" if isinstance(value, list) else type(value).__name__,
        "count": len(records),
        "item_keys": sorted(keys),
        "nullable_item_fields": sorted(null_fields),
    }


NOT_AUTHENTICATED = "Error: Not authenticated. Please use login_to_fpl first."

def _difficulty_bar(difficulty: int) -> str:
    """Render a fixture difficulty rating as filled/empty pips out of 5."""
    return "●" * difficulty + "○" * (5 - difficulty)


def _is_ambiguous(matches) -> bool:
    """True when several players match and the best one is not near-exact."""
    return len(matches) > 1 and matches[0][1] < 0.95


def _is_confident(matches) -> bool:
    """True for a lone match, or a near-exact one that clearly beats the runner-up."""
    return len(matches) == 1 or (
        matches[0][1] >= 0.95 and matches[0][1] - matches[1][1] > 0.2
    )


def _get_client():
    """Internal helper to get the active client"""
    # Fall back to a session established without a human present (restored from
    # the token cache, or a credential login at startup).
    session_id = _active_session_id or sessions.active_session_id
    if not session_id:
        return None
    return sessions.get_client(session_id)


def _read_only() -> bool:
    """Whether account-modifying tools are disabled for this process."""
    return env_flag("FPL_READ_ONLY")


async def _ensure_reference_data(client, *, fixtures: bool = False) -> None:
    """Load bootstrap (and optionally fixtures) into the store on first use.

    Best effort: on failure the callers' own "data not available" guards report it,
    rather than surfacing a transport error from an unrelated tool.
    """
    try:
        await reference.ensure_bootstrap_data(client)
        if fixtures:
            await reference.ensure_fixtures_data(client)
    except Exception as e:
        logger.error(f"Failed to load reference data: {e}")


def _with_client(*, fixtures: bool = False):
    """Give a tool an authenticated client with reference data already loaded.

    The wrapped function takes the client as its first parameter. That parameter is
    stripped from the signature FastMCP advertises, so it never reaches the tool
    schema - callers see only the tool's real arguments. Loading the reference data
    here rather than in each tool is what keeps it from being forgotten.
    """
    def decorate(fn):
        @functools.wraps(fn)
        async def wrapper(*args, **kwargs):
            client = _get_client()
            if not client:
                return NOT_AUTHENTICATED
            await _ensure_reference_data(client, fixtures=fixtures)
            return await fn(client, *args, **kwargs)

        signature = inspect.signature(fn)
        wrapper.__signature__ = signature.replace(
            parameters=list(signature.parameters.values())[1:]
        )
        return wrapper
    return decorate


def _format_player_details(player: 'ElementData') -> str:
    """Helper function to format detailed player information"""
    price = player.now_cost / 10
    news_indicator = " ⚠️" if player.news else ""
    status_indicator = "" if player.status == 'a' else f" [{player.status}]"
    
    output = [
        f"**{player.web_name}** ({player.first_name} {player.second_name})",
        f"Team: {player.team_name}",
        f"Position: {player.position}",
        f"Price: £{price:.1f}m",
        "",
        "**Performance:**",
        f"├─ Form: {player.form}",
        f"├─ Points per Game: {player.points_per_game}",
        f"├─ Total Points: {getattr(player, 'total_points', 'N/A')}",
        f"├─ Minutes: {getattr(player, 'minutes', 'N/A')}",
        "",
        f"**Status:** {player.status}{status_indicator}{news_indicator}",
    ]
    
    if player.news:
        output.extend([
            "",
            f"**News:** {player.news}"
        ])
    
    if hasattr(player, 'selected_by_percent'):
        output.extend([
            "",
            "**Popularity:**",
            f"├─ Selected by: {getattr(player, 'selected_by_percent', 'N/A')}%",
            f"├─ Transfers in (GW): {getattr(player, 'transfers_in_event', 'N/A')}",
            f"├─ Transfers out (GW): {getattr(player, 'transfers_out_event', 'N/A')}",
        ])
    
    if hasattr(player, 'goals_scored'):
        output.extend([
            "",
            "**Stats:**",
            f"├─ Goals: {getattr(player, 'goals_scored', 0)}",
            f"├─ Assists: {getattr(player, 'assists', 0)}",
            f"├─ Clean Sheets: {getattr(player, 'clean_sheets', 0)}",
            f"├─ Bonus Points: {getattr(player, 'bonus', 0)}",
        ])
    
    return "\n".join(output)

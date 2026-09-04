"""Tools that answer from the warehouse rather than the live API.

Every other tool in this package is a view onto `bootstrap-static` and needs a session
to fetch it. This one needs neither: `make deadline` already did the fetching, the
projecting and the ranking, and left the answer in SQLite. A stored recommendation is
available offline, so requiring a login to read it would be a guard against nothing.

There is exactly one transfer recommender, and it is `engine/recommend.py`. This module
opens the warehouse, calls it, and renders the result with the engine's own renderer.
It derives nothing. The tool it replaced re-derived its own advice from the live API and
so had none of the engine's corrections - it charged no points hit, ranked on raw form
rather than net expected points, and could not see an active wildcard - which meant an
MCP client was told to make a transfer the CLI would refuse.
"""

import sqlite3
from pathlib import Path

from ...engine import recommend as engine
from ...engine import status, storage
from ...engine.projection import HORIZON_GAMEWEEKS, HorizonMissing
from .core import mcp

# The same default the CLI uses, so both interfaces read the same file. It is relative,
# and the server is started with `uv --directory /path/to/fpl-agent`, so it resolves
# against the repo. The absolute path goes into the error message rather than being
# assumed correct.
DB_PATH = storage.DEFAULT_DB_PATH

_RUN_DEADLINE = "Run `make deadline` in the repo, then ask again."


@mcp.tool()
async def recommend_transfers(weeks: int = HORIZON_GAMEWEEKS, top: int = 8) -> str:
    """
    Rank transfers on projected points, using the stored projections and your captured
    squad. This is the same ranking `fpl-agent recommend` prints: gains are net of the
    points hit the move would cost, bench slots are discounted, price-change urgency and
    league ownership are shown, and an active wildcard or free hit is called out.
    Requires `make deadline` to have been run for the current gameweek.
    """
    # Read-only, via `status.connect_readonly` rather than `storage.connect`. The CLI
    # opens the same file for writing because `--record` may append a decision; this
    # tool never records, and `storage.connect` would create an empty warehouse and
    # run the schema when the path is wrong, turning "there is no database" into "there
    # is a healthy database with no snapshots in it".
    try:
        conn = status.connect_readonly(DB_PATH)
    except FileNotFoundError:
        return (f"No warehouse at {Path(DB_PATH).resolve()}. Nothing has been captured "
                f"yet, so there is no recommendation to read. {_RUN_DEADLINE}")

    try:
        context = engine.transfer_context(conn)
        recommendations = engine.recommend(conn, weeks, top)
    except HorizonMissing as missing:
        # The run order was wrong, not the code. `recommend` reads stored projections
        # and refuses to run its own, so say which command was skipped.
        return f"Cannot recommend: {missing}\n\n{_RUN_DEADLINE}"
    except LookupError as missing:
        # No snapshot, no squad on it, or no target gameweek.
        return f"Cannot recommend: {missing}\n\n{_RUN_DEADLINE}"
    except sqlite3.OperationalError as broken:
        return (f"Cannot read the warehouse at {Path(DB_PATH).resolve()}: {broken}. "
                f"{_RUN_DEADLINE}")
    finally:
        conn.close()

    return engine.render(context, recommendations, weeks).lstrip("\n")

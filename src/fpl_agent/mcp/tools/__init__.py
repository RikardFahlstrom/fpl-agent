"""MCP tools, grouped by what they are about.

Importing this package registers every tool with the server. `mcp_tools` was a single
2,600-line module holding thirty-four of them, which made it the hardest file in the
project to review or change safely.
"""

from .core import (  # noqa: F401
    NOT_AUTHENTICATED, _difficulty_bar, _ensure_reference_data, _format_player_details,
    _get_client, _is_ambiguous, _is_confident, _mapping_contract, _optional_int,
    _pick_price_text, _read_only, _records_contract, _with_client,
    get_active_session, mcp, set_active_session,
)
from .session import (  # noqa: F401
    begin_web_login,
    check_login_status,
    get_auth_status,
    get_authenticated_schema_diagnostics,
    login_to_fpl,
    poll_web_login,
)
from .squad import (  # noqa: F401
    analyze_squad_recent_performance,
    get_manager_snapshot,
    get_my_info,
    get_my_performance,
    get_my_squad,
    make_transfers,
    recommend_chip_strategy,
    recommend_transfers,
)
from .players import (  # noqa: F401
    compare_players,
    find_player,
    get_player_details,
    get_player_summary,
    get_top_players,
    search_players,
)
from .teams import (  # noqa: F401
    analyze_team_fixtures,
    get_team_info,
    list_all_teams,
    search_players_by_team,
)
from .gameweeks import (  # noqa: F401
    get_current_gameweek,
    get_fixtures_for_gameweek,
    get_gameweek_info,
    list_all_gameweeks,
)
from .leagues import (  # noqa: F401
    compare_managers,
    get_league_standings,
    get_manager_gameweek_team,
)
from .injuries import (  # noqa: F401
    check_player_availability,
    get_injury_and_lineup_predictions,
)

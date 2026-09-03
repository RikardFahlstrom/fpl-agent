"""
FPL MCP Resources - Read-only data access via URI templates.

Resources expose FPL data that can be accessed multiple times efficiently.
They represent GET-like operations without side effects.

Most resources are a URI-addressable view of the identically-named tool in
mcp_tools and delegate to it, so the formatting lives in exactly one place.
Only the resources whose output has no tool equivalent are implemented here.
"""

from .tools import (
    _ensure_reference_data,
    _format_player_details,
    _get_client,
    analyze_team_fixtures,
    get_current_gameweek,
    get_fixtures_for_gameweek,
    get_gameweek_info,
    get_injury_and_lineup_predictions,
    get_league_standings,
    get_manager_gameweek_team,
    get_my_info,
    get_my_performance,
    get_my_squad,
    get_player_summary,
    get_players_to_avoid,
    get_team_info,
    list_all_gameweeks,
    list_all_teams,
    mcp,
    search_players_by_team,
)
from .state import store

# Resources name the tool that establishes a session, so their wording differs
# slightly from the tools' own guard.
NOT_AUTHENTICATED = "Error: Not authenticated. Please use login_to_fpl tool first."


async def _ready_client():
    """Return the active client with reference data loaded, or None."""
    client = _get_client()
    if client:
        await _ensure_reference_data(client)
    return client


# ============================================================================
# BOOTSTRAP DATA RESOURCES (Static)
# ============================================================================

@mcp.resource("fpl://bootstrap/players")
async def get_all_players_resource() -> str:
    """Get all FPL players with basic stats and prices."""
    if not await _ready_client():
        return NOT_AUTHENTICATED

    if not store.bootstrap_data or not store.bootstrap_data.elements:
        return "Error: Player data not available."

    try:
        players = store.bootstrap_data.elements

        output = [f"**All FPL Players ({len(players)} total)**\n"]

        # Group by position
        positions = {'GKP': [], 'DEF': [], 'MID': [], 'FWD': []}
        for p in players:
            if p.position in positions:
                positions[p.position].append(p)

        for pos, players_list in positions.items():
            output.append(f"\n**{pos} ({len(players_list)} players):**")
            # Sort by price descending, show top 10
            sorted_players = sorted(players_list, key=lambda x: x.now_cost, reverse=True)[:10]
            for p in sorted_players:
                price = p.now_cost / 10
                news_indicator = " ⚠️" if p.news else ""
                output.append(
                    f"├─ {p.web_name:15s} ({p.team_name:15s}) | £{price:4.1f}m | "
                    f"Form: {p.form:4s} | PPG: {p.points_per_game:4s}{news_indicator}"
                )
            if len(players_list) > 10:
                output.append(f"└─ ... and {len(players_list) - 10} more")

        return "\n".join(output)
    except Exception as e:
        return f"Error: {str(e)}"


@mcp.resource("fpl://bootstrap/teams")
async def get_all_teams_resource() -> str:
    """Get all Premier League teams with strength ratings."""
    if not _get_client():
        return NOT_AUTHENTICATED
    return await list_all_teams()


@mcp.resource("fpl://bootstrap/gameweeks")
async def get_all_gameweeks_resource() -> str:
    """Get all gameweeks with their status for the season."""
    if not _get_client():
        return NOT_AUTHENTICATED
    return await list_all_gameweeks()


@mcp.resource("fpl://current-gameweek")
async def get_current_gameweek_resource() -> str:
    """Get the current or upcoming gameweek information."""
    if not _get_client():
        return NOT_AUTHENTICATED
    return await get_current_gameweek()


# ============================================================================
# PLAYER RESOURCES (Dynamic with URI templates)
# ============================================================================

@mcp.resource("fpl://player/{player_name}")
async def get_player_resource(player_name: str) -> str:
    """Get detailed information about a specific player by name."""
    if not await _ready_client():
        return NOT_AUTHENTICATED

    if not store.bootstrap_data:
        return "Error: Player data not available."

    matches = store.find_players_by_name(player_name, fuzzy=True)

    if not matches:
        return f"No player found matching '{player_name}'"

    if len(matches) > 1 and matches[0][1] < 0.95:
        output = [f"Found {len(matches)} players matching '{player_name}':\n"]
        for player, score in matches[:10]:
            price = player.now_cost / 10
            news_indicator = " ⚠️" if player.news else ""
            status_indicator = "" if player.status == 'a' else f" [{player.status}]"

            output.append(
                f"├─ {player.first_name} {player.second_name} ({player.web_name}) - "
                f"{player.team_name} {player.position} | £{price:.1f}m | "
                f"Form: {player.form} | PPG: {player.points_per_game}{status_indicator}{news_indicator}"
            )
        output.append("\nPlease specify the full name for more details.")
        return "\n".join(output)

    return _format_player_details(matches[0][0])


@mcp.resource("fpl://player/{player_name}/summary")
async def get_player_summary_resource(player_name: str) -> str:
    """Get comprehensive player summary including fixtures, history, and past seasons."""
    if not await _ready_client():
        return NOT_AUTHENTICATED

    # Resolved here as well as in the tool, so an ambiguous name can point at the
    # sibling resource rather than at the find_player tool.
    matches = store.find_players_by_name(player_name, fuzzy=True)
    if not matches:
        return f"No player found matching '{player_name}'"
    if len(matches) > 1 and matches[0][1] < 0.95:
        return f"Ambiguous player name. Use fpl://player/{player_name} to see all matches"

    return await get_player_summary(player_name)


# ============================================================================
# TEAM RESOURCES
# ============================================================================

@mcp.resource("fpl://team/{team_name}")
async def get_team_resource(team_name: str) -> str:
    """Get detailed information about a Premier League team including strength ratings."""
    if not _get_client():
        return NOT_AUTHENTICATED
    return await get_team_info(team_name)


@mcp.resource("fpl://team/{team_name}/squad")
async def get_team_squad_resource(team_name: str) -> str:
    """Get all players from a specific team organized by position."""
    if not _get_client():
        return NOT_AUTHENTICATED
    return await search_players_by_team(team_name)


@mcp.resource("fpl://team/{team_name}/fixtures/{num_gameweeks}")
async def get_team_fixtures_resource(team_name: str, num_gameweeks: int = 5) -> str:
    """Get upcoming fixtures for a team with difficulty ratings. Default num_gameweeks is 5."""
    if not _get_client():
        return NOT_AUTHENTICATED
    return await analyze_team_fixtures(team_name, num_gameweeks)


# ============================================================================
# GAMEWEEK RESOURCES
# ============================================================================

@mcp.resource("fpl://gameweek/{gameweek_number}")
async def get_gameweek_resource(gameweek_number: int) -> str:
    """Get detailed information about a specific gameweek."""
    if not _get_client():
        return NOT_AUTHENTICATED
    return await get_gameweek_info(gameweek_number)


@mcp.resource("fpl://gameweek/{gameweek_number}/fixtures")
async def get_gameweek_fixtures_resource(gameweek_number: int) -> str:
    """Get all fixtures for a specific gameweek."""
    if not _get_client():
        return NOT_AUTHENTICATED
    return await get_fixtures_for_gameweek(gameweek_number)


# ============================================================================
# USER-SPECIFIC RESOURCES (Require authentication)
# ============================================================================

@mcp.resource("fpl://my/info")
async def get_my_info_resource() -> str:
    """Get your FPL account information including leagues."""
    if not _get_client():
        return NOT_AUTHENTICATED
    return await get_my_info()


@mcp.resource("fpl://my/squad")
async def get_my_squad_resource() -> str:
    """Get your current team squad with chips and transfer information."""
    if not _get_client():
        return NOT_AUTHENTICATED
    return await get_my_squad()


@mcp.resource("fpl://my/performance")
async def get_my_performance_resource() -> str:
    """Get your FPL performance including ranks and league standings."""
    if not _get_client():
        return NOT_AUTHENTICATED
    return await get_my_performance()


# ============================================================================
# LEAGUE RESOURCES
# ============================================================================

@mcp.resource("fpl://league/{league_name}/standings/{page}")
async def get_league_standings_resource(league_name: str, page: int = 1) -> str:
    """Get standings for a specific league by name. Default page is 1."""
    if not _get_client():
        return NOT_AUTHENTICATED
    return await get_league_standings(league_name, page)


@mcp.resource("fpl://manager/{manager_name}/team/{league_name}/{gameweek}")
async def get_manager_team_resource(manager_name: str, league_name: str, gameweek: int) -> str:
    """Get a manager's team selection for a specific gameweek."""
    if not _get_client():
        return NOT_AUTHENTICATED
    return await get_manager_gameweek_team(manager_name, league_name, gameweek)


# ============================================================================
# INJURY / LINEUP RESOURCES
# ============================================================================

@mcp.resource("fpl://injuries")
async def get_injuries_resource() -> str:
    """Get injury and lineup predictions from RotoWire."""
    if not _get_client():
        return NOT_AUTHENTICATED
    return await get_injury_and_lineup_predictions()


@mcp.resource("fpl://injuries/avoid")
async def get_players_to_avoid_resource() -> str:
    """Get players to avoid based on injury and lineup status."""
    if not _get_client():
        return NOT_AUTHENTICATED
    return await get_players_to_avoid()

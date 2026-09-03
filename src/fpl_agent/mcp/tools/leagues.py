"""League standings and rival-manager tools."""

from ...state import store
from .core import mcp
from .core import _with_client


@mcp.tool()
@_with_client()
async def get_league_standings(client, league_name: str, page: int = 1) -> str:
    """
    Get standings for a specific FPL league by name.
    Shows manager rankings, points, and team names within the league.
    Use this to see how managers are performing in one of your leagues.
    Example: "Greatest Fantasy Footy", "Work League"
    """
    
    try:
        # Find league by name
        league_info = await store.find_league_by_name(client, league_name)
        if not league_info:
            return f"Could not find a league named '{league_name}' in your leagues. Use get_my_info to see your leagues."
        
        league_id = league_info['id']
        
        # Fetch league standings from API
        standings_data = await client.get_league_standings(
            league_id=league_id,
            page_standings=page
        )
        
        league_data = standings_data.get('league', {})
        standings = standings_data.get('standings', {})
        results = standings.get('results', [])
        
        if not results:
            return f"No standings found for league '{league_name}'"
        
        output = [
            f"**{league_data.get('name', league_name)}**",
            f"Total Entries: {standings.get('has_next', False) and 'Many' or len(results)}",
            f"Page: {page}",
            "",
            "**Standings:**",
            ""
        ]
        
        for entry in results:
            rank_change = entry['rank'] - entry['last_rank']
            rank_indicator = "↑" if rank_change < 0 else "↓" if rank_change > 0 else "="
            
            output.append(
                f"{entry['rank']:3d}. {rank_indicator} {entry['entry_name']:30s} | "
                f"{entry['player_name']:20s} | "
                f"GW: {entry['event_total']:3d} | Total: {entry['total']:4d}"
            )
        
        if standings.get('has_next'):
            output.append(f"\n📄 More entries available. Use page={page + 1} to see next page.")
        
        return "\n".join(output)
    except Exception as e:
        return f"Error fetching league standings: {str(e)}"


@mcp.tool()
@_with_client()
async def get_manager_gameweek_team(client, manager_name: str, league_name: str, gameweek: int) -> str:
    """
    Get a manager's team selection for a specific gameweek by their name.
    Shows the 15 players picked, captain/vice-captain, formation, and points scored.
    Provide the manager's name (or team name), the league they're in, and gameweek number.
    Example: manager_name="Jaakko", league_name="Greatest Fantasy Footy", gameweek=13
    """
    
    try:
        # Find league first
        league_info = await store.find_league_by_name(client, league_name)
        if not league_info:
            return f"Could not find league '{league_name}'. Use get_my_info to see your leagues."
        
        # Find manager in league
        manager_info = await store.find_manager_by_name(client, league_info['id'], manager_name)
        if not manager_info:
            return f"Could not find manager '{manager_name}' in league '{league_name}'"
        
        manager_team_id = manager_info['entry']
        
        # Fetch gameweek picks from API
        picks_data = await client.get_manager_gameweek_picks(manager_team_id, gameweek)
        
        picks = picks_data.get('picks', [])
        entry_history = picks_data.get('entry_history', {})
        auto_subs = picks_data.get('automatic_subs', [])
        
        if not picks:
            return f"No team data found for {manager_info['player_name']} in gameweek {gameweek}"
        
        # Rehydrate player names
        element_ids = [pick['element'] for pick in picks]
        players_info = store.rehydrate_player_names(element_ids)
        
        output = [
            f"**{manager_info['entry_name']}** - {manager_info['player_name']}",
            f"Gameweek {gameweek}",
            f"Points: {entry_history.get('points', 0)} | Total: {entry_history.get('total_points', 0)}",
            f"Overall Rank: {entry_history.get('overall_rank', 'N/A'):,}",
            f"Team Value: £{entry_history.get('value', 0)/10:.1f}m | Bank: £{entry_history.get('bank', 0)/10:.1f}m",
            f"Transfers: {entry_history.get('event_transfers', 0)} (Cost: {entry_history.get('event_transfers_cost', 0)}pts)",
            f"Points on Bench: {entry_history.get('points_on_bench', 0)}",
            ""
        ]
        
        if picks_data.get('active_chip'):
            output.append(f"**Active Chip:** {picks_data['active_chip']}")
            output.append("")
        
        starting_xi = [p for p in picks if p['position'] <= 11]
        bench = [p for p in picks if p['position'] > 11]
        
        output.append("**Starting XI:**")
        for pick in starting_xi:
            player = players_info.get(pick['element'], {})
            role = " (C)" if pick['is_captain'] else " (VC)" if pick['is_vice_captain'] else ""
            multiplier = f" x{pick['multiplier']}" if pick['multiplier'] > 1 else ""
            
            output.append(
                f"{pick['position']:2d}. {player.get('web_name', 'Unknown'):15s} "
                f"({player.get('team', 'UNK'):3s} {player.get('position', 'UNK')}) | "
                f"£{player.get('price', 0):.1f}m{role}{multiplier}"
            )
        
        output.append("\n**Bench:**")
        for pick in bench:
            player = players_info.get(pick['element'], {})
            output.append(
                f"{pick['position']:2d}. {player.get('web_name', 'Unknown'):15s} "
                f"({player.get('team', 'UNK'):3s} {player.get('position', 'UNK')}) | "
                f"£{player.get('price', 0):.1f}m"
            )
        
        if auto_subs:
            output.append("\n**Automatic Substitutions:**")
            for sub in auto_subs:
                player_out = store.get_player_name(sub['element_out'])
                player_in = store.get_player_name(sub['element_in'])
                output.append(f"├─ {player_out} → {player_in}")
        
        return "\n".join(output)
    except Exception as e:
        return f"Error fetching manager's gameweek team: {str(e)}"


@mcp.tool()
@_with_client()
async def compare_managers(client, manager_names: list[str], league_name: str, gameweek: int) -> str:
    """
    Compare multiple managers' teams for a specific gameweek side-by-side using their names.
    Shows differences in player selection, captaincy choices, and points scored.
    Provide 2-4 manager names (or team names), the league they're in, and gameweek number.
    Example: manager_names=["Jaakko", "Lewis"], league_name="Greatest Fantasy Footy", gameweek=13
    """
    
    if len(manager_names) < 2:
        return "Error: Please provide at least 2 manager names to compare."
    
    if len(manager_names) > 4:
        return "Error: Maximum 4 managers can be compared at once."
    
    try:
        # Find league first
        league_info = await store.find_league_by_name(client, league_name)
        if not league_info:
            return f"Could not find league '{league_name}'"
        
        # Find all managers
        manager_ids = []
        manager_infos = []
        for name in manager_names:
            manager_info = await store.find_manager_by_name(client, league_info['id'], name)
            if not manager_info:
                return f"Could not find manager '{name}' in league '{league_name}'"
            manager_ids.append(manager_info['entry'])
            manager_infos.append(manager_info)
        
        # Fetch all teams
        teams_data = []
        for team_id in manager_ids:
            picks_data = await client.get_manager_gameweek_picks(team_id, gameweek)
            teams_data.append((team_id, picks_data))
        
        output = [f"**Manager Comparison - Gameweek {gameweek}**\n"]
        
        # Summary comparison
        output.append("**Performance Summary:**")
        for i, (team_id, data) in enumerate(teams_data):
            entry_history = data.get('entry_history', {})
            manager_info = manager_infos[i]
            output.append(
                f"├─ {manager_info['player_name']} ({manager_info['entry_name']}): "
                f"{entry_history.get('points', 0)}pts | "
                f"Rank: {entry_history.get('overall_rank', 'N/A'):,} | "
                f"Transfers: {entry_history.get('event_transfers', 0)} "
                f"(-{entry_history.get('event_transfers_cost', 0)}pts)"
            )
        
        output.append("\n**Captain Choices:**")
        for i, (team_id, data) in enumerate(teams_data):
            picks = data.get('picks', [])
            captain_pick = next((p for p in picks if p['is_captain']), None)
            if captain_pick:
                captain_name = store.get_player_name(captain_pick['element'])
                multiplier = captain_pick.get('multiplier', 2)
                manager_info = manager_infos[i]
                output.append(f"├─ {manager_info['player_name']}: {captain_name} (x{multiplier})")
        
        # Find common and unique players
        all_players = {}
        for i, (team_id, data) in enumerate(teams_data):
            picks = data.get('picks', [])
            starting_xi = [p['element'] for p in picks if p['position'] <= 11]
            all_players[team_id] = set(starting_xi)
        
        common_players = set.intersection(*all_players.values()) if len(all_players) > 1 else set()
        
        if common_players:
            output.append(f"\n**Common Players ({len(common_players)}):**")
            for element_id in list(common_players)[:10]:
                player_name = store.get_player_name(element_id)
                output.append(f"├─ {player_name}")
        
        # Unique players per team
        output.append("\n**Unique Selections:**")
        for i, team_id in enumerate(manager_ids):
            other_teams = [t for t in manager_ids if t != team_id]
            other_players = set()
            for other_id in other_teams:
                other_players.update(all_players.get(other_id, set()))
            
            unique = all_players[team_id] - other_players
            if unique:
                manager_info = manager_infos[i]
                output.append(f"\n{manager_info['player_name']} only:")
                for element_id in list(unique)[:5]:
                    player_name = store.get_player_name(element_id)
                    output.append(f"├─ {player_name}")
        
        return "\n".join(output)
    except Exception as e:
        return f"Error comparing managers: {str(e)}"

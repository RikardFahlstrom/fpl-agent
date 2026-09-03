"""Player search, detail and comparison tools."""

from ...reference import reference
from .core import mcp
from .core import _format_player_details, _is_ambiguous, _is_confident, _with_client


@mcp.tool()
@_with_client()
async def search_players(client, name_query: str) -> str:
    """
    Search for players by name. Returns price, form, and basic stats.
    Use player names (not IDs) for all operations.
    """
    
    players = await client.get_players()
    query = name_query.lower()
    matches = [
        p for p in players
        if query in p.web_name.lower()
        or query in f"{p.first_name} {p.second_name}".lower()
    ]
    
    if not matches: return "No players found."
    
    return "\n".join([
        f"{p.web_name} ({p.team_name}) | £{p.price}m | Form: {p.form}" 
        for p in matches[:10]
    ])


@mcp.tool()
@_with_client()
async def get_top_players(client) -> str:
    """
    Get top performing players by position (GKP, DEF, MID, FWD) based on points per game.
    Returns top 3 goalkeepers and top 10 for each outfield position.
    """
    
    try:
        top_players = await client.get_top_players_by_position()
        
        output = ["**Top Players by Position (Points per Game)**\n"]
        
        for position, players in top_players.items():
            if not players:
                continue
            output.append(f"\n**{position}:**")
            for p in players:
                news_indicator = " ⚠️" if p['news'] else ""
                output.append(
                    f"├─ {p['name']} ({p['team']}) - £{p['price']:.1f}m | "
                    f"PPG: {p['points_per_game']:.1f} | Total: {p['total_points']}{news_indicator}"
                )
        
        return "\n".join(output)
    except Exception as e:
        return f"Error: {str(e)}"


@mcp.tool()
@_with_client()
async def find_player(client, player_name: str) -> str:
    """
    Find a player by name with intelligent fuzzy matching.
    Handles variations in spelling, partial names, and common nicknames.
    If multiple players match, returns disambiguation options.
    """
    
    if not reference.bootstrap_data:
        return "Error: Player data not available."
    
    try:
        matches = reference.find_players_by_name(player_name, fuzzy=True)
        
        if not matches:
            return f"No players found matching '{player_name}'. Try a different spelling or use the player's surname."
        
        if _is_confident(matches):
            player = matches[0][0]
            return _format_player_details(player)
        
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
    except Exception as e:
        return f"Error: {str(e)}"


@mcp.tool()
@_with_client()
async def get_player_details(client, player_name: str) -> str:
    """
    Get detailed information about a specific player by name.
    Includes price, form, team, position, and current status.
    """
    
    matches = reference.find_players_by_name(player_name, fuzzy=True)
    
    if not matches:
        return f"No player found matching '{player_name}'"
    
    if _is_ambiguous(matches):
        return f"Ambiguous player name. Please use find_player to see all matches for '{player_name}'"
    
    player = matches[0][0]
    return _format_player_details(player)


@mcp.tool()
@_with_client()
async def compare_players(client, player_names: list[str]) -> str:
    """
    Compare multiple players side-by-side using their names.
    Provide a list of 2-5 player names to compare their stats, prices, and form.
    Useful for transfer decisions.
    """
    
    if not reference.bootstrap_data:
        return "Error: Player data not available."
    
    if len(player_names) < 2:
        return "Error: Please provide at least 2 player names to compare."
    
    if len(player_names) > 5:
        return "Error: Maximum 5 players can be compared at once."
    
    try:
        players_to_compare = []
        ambiguous = []
        
        for name in player_names:
            matches = reference.find_players_by_name(name, fuzzy=True)
            
            if not matches:
                return f"Error: No player found matching '{name}'"
            
            if _is_confident(matches):
                players_to_compare.append(matches[0][0])
            else:
                ambiguous.append((name, matches[:3]))
        
        if ambiguous:
            output = ["Cannot compare - ambiguous player names:\n"]
            for name, matches in ambiguous:
                output.append(f"\n'{name}' could be:")
                for player, score in matches:
                    output.append(f"  - {player.first_name} {player.second_name} ({player.team_name})")
            output.append("\nPlease use more specific names or full names.")
            return "\n".join(output)
        
        output = [f"**Player Comparison ({len(players_to_compare)} players)**\n"]
        output.append("=" * 80)
        
        for player in players_to_compare:
            price = player.now_cost / 10
            news_indicator = " ⚠️" if player.news else ""
            status_indicator = "" if player.status == 'a' else f" [{player.status}]"
            
            output.extend([
                f"\n**{player.web_name}** ({player.first_name} {player.second_name})",
                f"├─ Team: {player.team_name} | Position: {player.position}",
                f"├─ Price: £{price:.1f}m",
                f"├─ Form: {player.form} | Points per Game: {player.points_per_game}",
                f"├─ Total Points: {getattr(player, 'total_points', 'N/A')}",
                f"├─ Status: {player.status}{status_indicator}{news_indicator}",
            ])
            
            if player.news:
                output.append(f"├─ News: {player.news}")
            
            if hasattr(player, 'selected_by_percent'):
                output.append(f"├─ Selected by: {getattr(player, 'selected_by_percent', 'N/A')}%")
            
            if hasattr(player, 'minutes'):
                output.append(f"├─ Minutes played: {getattr(player, 'minutes', 'N/A')}")
            
            output.append("=" * 80)
        
        return "\n".join(output)
    except Exception as e:
        return f"Error: {str(e)}"


@mcp.tool()
@_with_client()
async def get_player_summary(client, player_name: str) -> str:
    """
    Get comprehensive player summary including upcoming fixtures, gameweek history, and past season performance.
    Provide the player's name to get detailed stats, fixture difficulty, and historical performance.
    """
    
    try:
        # Find player by name
        matches = reference.find_players_by_name(player_name, fuzzy=True)
        if not matches:
            return f"No player found matching '{player_name}'"
        
        if _is_ambiguous(matches):
            return f"Ambiguous player name. Please use find_player to see all matches for '{player_name}'"
        
        player = matches[0][0]
        player_id = player.id
        
        # Fetch detailed summary from API
        summary_data = await client.get_element_summary(player_id)
        
        # Enrich history and fixtures with team names
        history = summary_data.get('history', [])
        history = reference.enrich_gameweek_history(history)
        
        fixtures = summary_data.get('fixtures', [])
        fixtures = reference.enrich_fixtures(fixtures)
        
        output = [
            f"**{player.web_name}** ({player.first_name} {player.second_name})",
            f"Team: {player.team_name} | Position: {player.position} | Price: £{player.now_cost/10:.1f}m",
            "",
        ]
        
        # Upcoming Fixtures
        if fixtures:
            output.append(f"**Upcoming Fixtures ({len(fixtures)}):**")
            for fixture in fixtures[:5]:
                opponent_name = fixture.get('team_h_short') if not fixture['is_home'] else fixture.get('team_a_short', 'Unknown')
                home_away = "H" if fixture['is_home'] else "A"
                difficulty = "●" * fixture['difficulty']
                
                output.append(
                    f"├─ GW{fixture['event']}: vs {opponent_name} ({home_away}) | "
                    f"Difficulty: {difficulty} ({fixture['difficulty']}/5)"
                )
            output.append("")
        
        # Recent Gameweek History
        if history:
            recent_history = history[-5:]
            output.append(f"**Recent Performance (Last {len(recent_history)} GWs):**")
            
            for gw in recent_history:
                opponent_name = gw.get('opponent_team_short', 'Unknown')
                home_away = "H" if gw['was_home'] else "A"
                
                output.append(
                    f"├─ GW{gw['round']}: {gw['total_points']}pts vs {opponent_name} ({home_away}) | "
                    f"{gw['minutes']}min | G:{gw['goals_scored']} A:{gw['assists']} "
                    f"CS:{gw['clean_sheets']} | Bonus: {gw['bonus']}"
                )
            
            total_points = sum(gw['total_points'] for gw in recent_history)
            avg_points = total_points / len(recent_history)
            total_minutes = sum(gw['minutes'] for gw in recent_history)
            avg_minutes = total_minutes / len(recent_history)
            
            output.extend([
                "",
                f"**Recent Averages:**",
                f"├─ Points per game: {avg_points:.1f}",
                f"├─ Minutes per game: {avg_minutes:.0f}",
                ""
            ])
        
        # Past Season Performance
        history_past = summary_data.get('history_past', [])
        if history_past:
            output.append(f"**Past Seasons ({len(history_past)} seasons):**")
            for season in history_past[-3:]:
                output.append(
                    f"├─ {season['season_name']}: {season['total_points']}pts | "
                    f"{season['minutes']}min | G:{season['goals_scored']} A:{season['assists']} | "
                    f"£{season['start_cost']/10:.1f}m → £{season['end_cost']/10:.1f}m"
                )
        
        return "\n".join(output)
    except Exception as e:
        return f"Error fetching player summary: {str(e)}"

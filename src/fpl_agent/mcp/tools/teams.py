"""Club-level tools: information, squads and fixture runs."""

from ...state import store
from .core import mcp
from .core import _difficulty_bar, _with_client


@mcp.tool()
@_with_client()
async def get_team_info(client, team_name: str) -> str:
    """
    Get detailed information about a specific Premier League team by name.
    Includes strength ratings for home/away attack/defence.
    Example: "Arsenal", "Man City", "Liverpool"
    """
    
    if not store.bootstrap_data:
        return "Error: Team data not available."
    
    # Find team by name
    matching_teams = [
        t for t in store.bootstrap_data.teams
        if team_name.lower() in t.name.lower() or team_name.lower() in t.short_name.lower()
    ]
    
    if not matching_teams:
        return f"No team found matching '{team_name}'"
    
    if len(matching_teams) > 1:
        team_list = ", ".join([f"{t.name} ({t.short_name})" for t in matching_teams])
        return f"Multiple teams found: {team_list}. Please be more specific."
    
    team = matching_teams[0]
    team_dict = store.get_team_by_id(team.id)
    
    output = [
        f"**{team_dict['name']} ({team_dict['short_name']})**",
        ""
    ]
    
    if team_dict.get('strength'):
        output.append(f"Overall Strength: {team_dict['strength']}")
    
    if team_dict.get('strength_overall_home') or team_dict.get('strength_overall_away'):
        output.extend([
            "",
            "**Overall Strength:**",
            f"Home: {team_dict.get('strength_overall_home', 'N/A')}",
            f"Away: {team_dict.get('strength_overall_away', 'N/A')}",
        ])
    
    if team_dict.get('strength_attack_home') or team_dict.get('strength_attack_away'):
        output.extend([
            "",
            "**Attack Strength:**",
            f"Home: {team_dict.get('strength_attack_home', 'N/A')}",
            f"Away: {team_dict.get('strength_attack_away', 'N/A')}",
        ])
    
    if team_dict.get('strength_defence_home') or team_dict.get('strength_defence_away'):
        output.extend([
            "",
            "**Defence Strength:**",
            f"Home: {team_dict.get('strength_defence_home', 'N/A')}",
            f"Away: {team_dict.get('strength_defence_away', 'N/A')}",
        ])
    
    return "\n".join(output)


@mcp.tool()
@_with_client()
async def list_all_teams(client) -> str:
    """
    List all Premier League teams with their basic information.
    Useful for finding team names or comparing team strengths.
    """
    
    teams = store.get_all_teams()
    if not teams:
        return "Error: Team data not available."
    
    output = ["**Premier League Teams:**\n"]
    
    teams_sorted = sorted(teams, key=lambda t: t['name'])
    
    for team in teams_sorted:
        strength_info = ""
        if team.get('strength_overall_home') and team.get('strength_overall_away'):
            avg_strength = (team['strength_overall_home'] + team['strength_overall_away']) / 2
            strength_info = f" | Strength: {avg_strength:.0f}"
        
        output.append(
            f"{team['name']:20s} ({team['short_name']}){strength_info}"
        )
    
    return "\n".join(output)


@mcp.tool()
@_with_client()
async def search_players_by_team(client, team_name: str) -> str:
    """
    Search for all players from a specific team by team name.
    Returns player names, positions, prices, and form.
    Example: "Arsenal", "Liverpool", "Man City"
    """
    
    if not store.bootstrap_data:
        return "Error: Player data not available."
    
    try:
        matching_teams = [
            t for t in store.bootstrap_data.teams
            if team_name.lower() in t.name.lower() or team_name.lower() in t.short_name.lower()
        ]
        
        if not matching_teams:
            return f"No teams found matching '{team_name}'"
        
        if len(matching_teams) > 1:
            team_list = ", ".join([f"{t.name} ({t.short_name})" for t in matching_teams])
            return f"Multiple teams found: {team_list}. Please be more specific."
        
        team = matching_teams[0]
        
        players = [
            p for p in store.bootstrap_data.elements
            if p.team == team.id
        ]
        
        if not players:
            return f"No players found for {team.name}"
        
        position_order = {'GKP': 1, 'DEF': 2, 'MID': 3, 'FWD': 4}
        players_sorted = sorted(
            players,
            key=lambda p: (position_order.get(p.position or 'ZZZ', 5), -p.now_cost)
        )
        
        output = [f"**{team.name} ({team.short_name}) Squad:**\n"]
        
        current_position = None
        for p in players_sorted:
            if p.position != current_position:
                current_position = p.position
                output.append(f"\n**{current_position}:**")
            
            price = p.now_cost / 10
            news_indicator = " ⚠️" if p.news else ""
            status_indicator = "" if p.status == 'a' else f" [{p.status}]"
            
            output.append(
                f"├─ {p.web_name:20s} | £{price:4.1f}m | "
                f"Form: {p.form:4s} | PPG: {p.points_per_game:4s}{status_indicator}{news_indicator}"
            )
        
        return "\n".join(output)
    except Exception as e:
        return f"Error: {str(e)}"


@mcp.tool()
@_with_client(fixtures=True)
async def analyze_team_fixtures(client, team_name: str, num_gameweeks: int = 5) -> str:
    """
    Analyze upcoming fixtures for a specific team to assess difficulty.
    Shows next N gameweeks with opponent strength and home/away status.
    Useful for identifying good times to bring in or sell team assets.
    Provide team name and number of gameweeks to analyze (default: 5).
    """
    
    if not store.bootstrap_data or not store.fixtures_data:
        return "Error: Team or fixtures data not available."
    
    try:
        matching_teams = [
            t for t in store.bootstrap_data.teams
            if team_name.lower() in t.name.lower() or team_name.lower() in t.short_name.lower()
        ]
        
        if not matching_teams:
            return f"No team found matching '{team_name}'"
        
        if len(matching_teams) > 1:
            team_list = ", ".join([f"{t.name} ({t.short_name})" for t in matching_teams])
            return f"Multiple teams found: {team_list}. Please be more specific."
        
        team = matching_teams[0]
        
        current_gw = store.get_current_gameweek()
        if not current_gw:
            return "Error: Could not determine current gameweek"
        
        team_fixtures = store.upcoming_fixtures(
            team.id, from_gameweek=current_gw.id, limit=num_gameweeks
        )
        
        if not team_fixtures:
            return f"No upcoming fixtures found for {team.name}"
        
        # Enrich fixtures with team names
        team_fixtures_enriched = store.enrich_fixtures(team_fixtures)
        team_fixtures_sorted = sorted(team_fixtures_enriched, key=lambda x: x.get('event') or 999)
        
        output = [
            f"**{team.name} ({team.short_name}) - Next {len(team_fixtures_sorted)} Fixtures**\n"
        ]
        
        total_difficulty = 0
        for fixture in team_fixtures_sorted:
            is_home = fixture.get('team_h') == team.id
            opponent_name = fixture.get('team_a_name') if is_home else fixture.get('team_h_name', 'Unknown')
            
            difficulty = fixture.get('team_h_difficulty') if is_home else fixture.get('team_a_difficulty')
            total_difficulty += difficulty
            
            difficulty_str = _difficulty_bar(difficulty)
            home_away = "H" if is_home else "A"
            kickoff = fixture.get('kickoff_time', '')[:10] if fixture.get('kickoff_time') else "TBD"
            
            output.append(
                f"GW{fixture.get('event')}: vs {opponent_name:20s} ({home_away}) | "
                f"{difficulty_str} ({difficulty}/5) | {kickoff}"
            )
        
        avg_difficulty = total_difficulty / len(team_fixtures_sorted)
        output.extend([
            "",
            f"**Average Difficulty:** {avg_difficulty:.1f}/5",
            f"**Assessment:** {'Favorable' if avg_difficulty < 3 else 'Moderate' if avg_difficulty < 3.5 else 'Difficult'} run of fixtures"
        ])
        
        return "\n".join(output)
    except Exception as e:
        return f"Error analyzing fixtures: {str(e)}"

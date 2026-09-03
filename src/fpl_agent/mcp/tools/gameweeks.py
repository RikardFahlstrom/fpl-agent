"""Gameweek and fixture tools."""

from ...reference import reference
from datetime import datetime, timezone
from .core import mcp
from .core import _with_client


@mcp.tool()
@_with_client()
async def get_current_gameweek(client) -> str:
    """
    Get the current or upcoming gameweek information.
    Returns the gameweek that is currently active (before deadline) or the next gameweek (after deadline).
    Use this to determine which gameweek to plan transfers for.
    """
    
    if not reference.bootstrap_data or not reference.bootstrap_data.events:
        return "Error: Gameweek data not available."
    
    try:
        now = datetime.now(timezone.utc)
        
        for event in reference.bootstrap_data.events:
            if event.is_current:
                deadline = datetime.fromisoformat(event.deadline_time.replace('Z', '+00:00'))
                if now < deadline:
                    return (
                        f"**Current Gameweek: {event.name}**\n"
                        f"Deadline: {event.deadline_time}\n"
                        f"Status: Active - deadline not yet passed\n"
                        f"Finished: {event.finished}\n"
                        f"Average Score: {event.average_entry_score or 'N/A'}\n"
                        f"Highest Score: {event.highest_score or 'N/A'}"
                    )
                else:
                    break
        
        for event in reference.bootstrap_data.events:
            if event.is_next:
                return (
                    f"**Upcoming Gameweek: {event.name}**\n"
                    f"Deadline: {event.deadline_time}\n"
                    f"Status: Next gameweek (current deadline has passed)\n"
                    f"Released: {event.released}\n"
                    f"Can Enter: {event.can_enter}"
                )
        
        for event in reference.bootstrap_data.events:
            if not event.finished:
                return (
                    f"**Upcoming Gameweek: {event.name}**\n"
                    f"Deadline: {event.deadline_time}\n"
                    f"Status: Upcoming\n"
                    f"Released: {event.released}"
                )
        
        return "Error: No active or upcoming gameweek found."
    except Exception as e:
        return f"Error: {str(e)}"


@mcp.tool()
@_with_client()
async def get_gameweek_info(client, gameweek_number: int) -> str:
    """
    Get detailed information about a specific gameweek by number (1-38).
    Includes deadline, scores, top players, and statistics.
    """
    
    if not reference.bootstrap_data or not reference.bootstrap_data.events:
        return "Error: Gameweek data not available."
    
    try:
        event = next((e for e in reference.bootstrap_data.events if e.id == gameweek_number), None)
        if not event:
            return f"Error: Gameweek {gameweek_number} not found."
        
        output = [
            f"**{event.name}**",
            f"Deadline: {event.deadline_time}",
            f"Status: {'Current' if event.is_current else 'Previous' if event.is_previous else 'Next' if event.is_next else 'Upcoming'}",
            f"Finished: {event.finished}",
            f"Released: {event.released}",
            ""
        ]
        
        if event.finished:
            output.extend([
                "**Statistics:**",
                f"Average Score: {event.average_entry_score}",
                f"Highest Score: {event.highest_score}",
                ""
            ])
            
            if event.top_element_info:
                top_player = reference.get_player_name(event.top_element_info.id)
                output.extend([
                    "**Top Performer:**",
                    f"Player: {top_player}",
                    f"Points: {event.top_element_info.points}",
                    ""
                ])
        
        if event.most_captained:
            most_cap = reference.get_player_name(event.most_captained)
            most_vc = reference.get_player_name(event.most_vice_captained)
            most_sel = reference.get_player_name(event.most_selected)
            most_trans = reference.get_player_name(event.most_transferred_in)
            
            output.extend([
                "**Popular Choices:**",
                f"Most Captained: {most_cap}",
                f"Most Vice-Captained: {most_vc}",
                f"Most Selected: {most_sel}",
                f"Most Transferred In: {most_trans}",
            ])
        
        return "\n".join(output)
    except Exception as e:
        return f"Error: {str(e)}"


@mcp.tool()
@_with_client()
async def list_all_gameweeks(client) -> str:
    """
    List all gameweeks with their status (finished, current, upcoming).
    Useful for getting an overview of the season.
    """
    
    if not reference.bootstrap_data or not reference.bootstrap_data.events:
        return "Error: Gameweek data not available."
    
    try:
        output = ["**All Gameweeks:**\n"]
        
        for event in reference.bootstrap_data.events:
            status = []
            if event.is_current:
                status.append("CURRENT")
            if event.is_previous:
                status.append("PREVIOUS")
            if event.is_next:
                status.append("NEXT")
            if event.finished:
                status.append("FINISHED")
            
            status_str = f" [{', '.join(status)}]" if status else ""
            avg_score = f" | Avg: {event.average_entry_score}" if event.average_entry_score else ""
            
            output.append(
                f"GW{event.id}: {event.name}{status_str} | "
                f"Deadline: {event.deadline_time[:10]}{avg_score}"
            )
        
        return "\n".join(output)
    except Exception as e:
        return f"Error: {str(e)}"


@mcp.tool()
@_with_client(fixtures=True)
async def get_fixtures_for_gameweek(client, gameweek: int) -> str:
    """
    Get all fixtures for a specific gameweek with team names and kickoff times.
    Useful for planning transfers and understanding fixture difficulty.
    """
    
    if not reference.fixtures_data:
        return "Error: Fixtures data not available."
    
    try:
        gw_fixtures = [f for f in reference.fixtures_data if f.event == gameweek]
        
        if not gw_fixtures:
            return f"No fixtures found for gameweek {gameweek}"
        
        # Enrich fixtures with team names
        gw_fixtures_enriched = reference.enrich_fixtures(gw_fixtures)
        
        output = [
            f"**Gameweek {gameweek} Fixtures ({len(gw_fixtures_enriched)} matches)**\n"
        ]
        
        gw_fixtures_sorted = sorted(gw_fixtures_enriched, key=lambda x: x.get('kickoff_time') or "")
        
        for fixture in gw_fixtures_sorted:
            home_name = fixture.get('team_h_short', 'Unknown')
            away_name = fixture.get('team_a_short', 'Unknown')
            
            status = "✓" if fixture.get('finished') else "○"
            score = f"{fixture.get('team_h_score')}-{fixture.get('team_a_score')}" if fixture.get('finished') else "vs"
            kickoff = fixture.get('kickoff_time', '')[:16] if fixture.get('kickoff_time') else "TBD"
            
            output.append(
                f"{status} {home_name} {score} {away_name} | "
                f"Kickoff: {kickoff} | "
                f"Difficulty: H:{fixture.get('team_h_difficulty')} A:{fixture.get('team_a_difficulty')}"
            )
        
        return "\n".join(output)
    except Exception as e:
        return f"Error fetching fixtures: {str(e)}"

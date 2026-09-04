"""Availability tools, backed by RotoWire lineup predictions."""

from ...rotowire_scraper import RotoWireLineupScraper
from .core import mcp
from .core import _with_client


@mcp.tool()
@_with_client()
async def get_injury_and_lineup_predictions(client) -> str:
    """
    Get predicted lineups and injury status for upcoming Premier League matches from RotoWire.
    This is crucial for understanding which players are likely to play and who to avoid.
    Shows OUT, DOUBTFUL, and EXPECTED players with confidence ratings.
    """
    
    try:
        scraper = RotoWireLineupScraper()
        lineup_statuses = await scraper.scrape_premier_league_lineups()
        
        if not lineup_statuses:
            return "No lineup predictions available at this time. RotoWire may not have published lineups yet."
        
        out_players = [s for s in lineup_statuses if s.status == 'OUT']
        doubtful_players = [s for s in lineup_statuses if s.status == 'DOUBTFUL']
        expected_players = [s for s in lineup_statuses if s.status == 'EXPECTED']
        
        output = ["**Premier League Lineup Predictions & Injury Status**\n"]
        
        if out_players:
            output.append(f"**🚫 OUT ({len(out_players)} players):**")
            for player in sorted(out_players, key=lambda x: x.team):
                output.append(
                    f"├─ {player.player_name} ({player.team}) - {player.reason} "
                    f"[Confidence: {player.confidence:.0%}]"
                )
            output.append("")
        
        if doubtful_players:
            output.append(f"**⚠️ DOUBTFUL ({len(doubtful_players)} players):**")
            for player in sorted(doubtful_players, key=lambda x: x.team):
                output.append(
                    f"├─ {player.player_name} ({player.team}) - {player.reason} "
                    f"[Confidence: {player.confidence:.0%}]"
                )
            output.append("")
        
        if expected_players:
            output.append(f"**✅ EXPECTED TO START ({len(expected_players)} key players):**")
            for player in sorted(expected_players, key=lambda x: x.team):
                output.append(
                    f"├─ {player.player_name} ({player.team}) - {player.reason} "
                    f"[Confidence: {player.confidence:.0%}]"
                )
        
        output.append("\n**Note:** This data is scraped from RotoWire and updates as lineups are confirmed.")
        
        return "\n".join(output)
    except Exception as e:
        return f"Error fetching lineup predictions: {str(e)}"


@mcp.tool()
@_with_client()
async def check_player_availability(client, player_name: str) -> str:
    """
    Check if a specific player is available to play based on RotoWire lineup predictions.
    Useful before making a transfer to verify the player is not injured or suspended.
    Provide player name (can be partial match).
    """
    
    try:
        scraper = RotoWireLineupScraper()
        lineup_statuses = await scraper.scrape_premier_league_lineups()
        
        if not lineup_statuses:
            return f"No lineup data available to check {player_name}'s status."
        
        matches = [
            s for s in lineup_statuses
            if player_name.lower() in s.player_name.lower()
        ]
        
        if not matches:
            return f"✅ {player_name} not found in injury/lineup reports. Likely available to play."
        
        if len(matches) > 1:
            output = [f"Found {len(matches)} players matching '{player_name}':\n"]
            for match in matches:
                status_emoji = "🚫" if match.status == "OUT" else "⚠️" if match.status == "DOUBTFUL" else "✅"
                output.append(
                    f"{status_emoji} {match.player_name} ({match.team}) - {match.status}: {match.reason} "
                    f"[Confidence: {match.confidence:.0%}]"
                )
            return "\n".join(output)
        
        player = matches[0]
        status_emoji = "🚫" if player.status == "OUT" else "⚠️" if player.status == "DOUBTFUL" else "✅"
        
        return (
            f"{status_emoji} **{player.player_name} ({player.team})**\n"
            f"Status: {player.status}\n"
            f"Reason: {player.reason}\n"
            f"Confidence: {player.confidence:.0%}\n\n"
            f"{'❌ AVOID - Player is not expected to play' if player.status == 'OUT' else '⚠️ RISKY - Player may not play' if player.status == 'DOUBTFUL' else '✅ AVAILABLE - Player expected to play'}"
        )
    except Exception as e:
        return f"Error checking player availability: {str(e)}"

"""Tools for the authenticated manager's own squad, transfers and chips."""

from ..models import TransferPayload
from ..state import store
from .core import NOT_AUTHENTICATED
from .core import logger
from datetime import datetime, timezone
from .core import mcp
from .core import _difficulty_bar, _ensure_reference_data, _get_client, _is_ambiguous, _optional_int, _pick_price_text, _read_only, _with_client


@mcp.tool()
async def get_manager_snapshot() -> dict:
    """Return the authenticated current squad as structured read-only application data."""
    client = _get_client()
    if not client:
        return {"status": "not_authenticated"}
    entry_id = store.get_user_entry_id(client)
    if not entry_id:
        return {"status": "entry_unavailable"}
    my_team = await client.get_my_team(entry_id)
    players = await client.get_players()
    player_map = {player.id: player for player in players}
    picks = []
    for pick in my_team.get("picks") or []:
        element = _optional_int(pick.get("element"))
        position = _optional_int(pick.get("position"))
        if element is None or position is None:
            continue
        player = player_map.get(element)
        purchase_price = _optional_int(pick.get("purchase_price"))
        selling_price = _optional_int(pick.get("selling_price"))
        if selling_price is None:
            selling_price = purchase_price
        picks.append(
            {
                "element": element,
                "position": position,
                "is_captain": bool(pick.get("is_captain")),
                "is_vice_captain": bool(pick.get("is_vice_captain")),
                "purchase_price": purchase_price,
                "selling_price": selling_price,
                "name": player.web_name if player else f"Player {element}",
                "team": player.team_name if player else "Unknown",
            }
        )
    transfers = my_team.get("transfers") or {}
    transfer_limit = _optional_int(transfers.get("limit"))
    transfers_made = _optional_int(transfers.get("made"))
    free_transfers = (
        None
        if transfer_limit is None
        else max(0, transfer_limit - (transfers_made or 0))
    )
    return {
        "status": "connected",
        "observed_at": datetime.now().astimezone().isoformat(),
        "entry_id": int(entry_id),
        "picks": picks,
        "bank": _optional_int(transfers.get("bank")),
        "squad_value": _optional_int(transfers.get("value")),
        "free_transfers": free_transfers,
        "transfer_cost": _optional_int(transfers.get("cost")),
        "chips": my_team.get("chips") or [],
    }


@mcp.tool()
@_with_client()
async def get_my_info(client) -> str:
    """
    Get your FPL account information including entry ID, leagues, and basic stats.
    Use this to see what leagues you're in and your overall performance.
    """
    
    if not client.user_info:
        return "Error: User information not available. Please try logging in again."
    
    try:
        player_info = client.user_info.get('player', {})
        leagues = client.user_info.get('leagues', {})
        classic_leagues = leagues.get('classic', [])
        
        output = [
            f"**Your FPL Account**",
            f"Name: {player_info.get('first_name')} {player_info.get('last_name')}",
            f"Region: {player_info.get('region_name')} ({player_info.get('region_iso_code_short')})",
            ""
        ]
        
        if classic_leagues:
            output.append(f"**Your Leagues ({len(classic_leagues)}):**")
            for league in classic_leagues[:10]:  # Show first 10
                output.append(f"├─ {league.get('name')}")
            if len(classic_leagues) > 10:
                output.append(f"└─ ... and {len(classic_leagues) - 10} more")
        
        return "\n".join(output)
    except Exception as e:
        return f"Error: {str(e)}"


@mcp.tool()
@_with_client()
async def get_my_squad(client) -> str:
    """Get your current team squad, chips status, and transfer information."""
    
    try:
        entry_id = store.get_user_entry_id(client)
        if not entry_id:
            return "Error: Could not determine your entry ID. Please try logging in again."
        
        my_team = await client.get_my_team(entry_id)
        all_players = await client.get_players()
        p_map = {p.id: p for p in all_players}
        
        # Transfer info
        transfers = my_team.get('transfers') or {}
        bank = _optional_int(transfers.get('bank'))
        transfer_limit = _optional_int(transfers.get('limit'))
        transfers_made = _optional_int(transfers.get('made')) or 0
        free_transfers = (
            None
            if transfer_limit is None
            else max(0, transfer_limit - transfers_made)
        )
        transfer_cost = _optional_int(transfers.get('cost'))
        squad_value = _optional_int(transfers.get('value'))

        squad_value_text = (
            f"£{squad_value / 10:.1f}m" if squad_value is not None else "Not available"
        )
        bank_text = f"£{bank / 10:.1f}m" if bank is not None else "Not available"
        free_transfers_text = (
            str(free_transfers) if free_transfers is not None else "Not applicable before GW1"
        )
        transfer_cost_text = (
            f"{transfer_cost} pts" if transfer_cost is not None else "Not applicable"
        )
        
        output = [
            f"**My Team**",
            f"Squad Value: {squad_value_text} | Bank: {bank_text}",
            f"Free Transfers: {free_transfers_text} | Transfer Cost: {transfer_cost_text}",
            ""
        ]
        
        # Chips info
        chips = my_team.get('chips', [])
        if chips:
            available_chips = [c for c in chips if c['status_for_entry'] == 'available']
            played_chips = [c for c in chips if c['status_for_entry'] == 'played']
            
            if available_chips:
                chip_icons = {
                    'bboost': '📊',
                    'freehit': '🎯',
                    '3xc': '⭐',
                    'wildcard': '🃏'
                }
                chips_str = ', '.join([f"{chip_icons.get(c['name'], '🎴')} {c['name'].upper()}" for c in available_chips])
                output.append(f"**Available Chips:** {chips_str}")
            
            if played_chips:
                output.append(f"**Played Chips:** {', '.join([c['name'].upper() for c in played_chips])}")
            
            output.append("")
        
        # Squad
        output.append("**Starting XI:**")
        starting = [p for p in my_team['picks'] if p['position'] <= 11]
        for pick in starting:
            p = p_map.get(pick['element'])
            role = " (C)" if pick['is_captain'] else " (VC)" if pick['is_vice_captain'] else ""
            output.append(
                f"{pick['position']:2d}. {p.web_name} ({p.team_name}): "
                f"{_pick_price_text(pick)}{role}"
            )
        
        output.append("\n**Bench:**")
        bench = [p for p in my_team['picks'] if p['position'] > 11]
        for pick in bench:
            p = p_map.get(pick['element'])
            output.append(
                f"{pick['position']:2d}. {p.web_name} ({p.team_name}): "
                f"{_pick_price_text(pick)}"
            )
            
        return "\n".join(output)
    except Exception as e:
        return f"Error: {str(e)}"


@mcp.tool()
async def make_transfers(player_names_out: list[str], player_names_in: list[str]) -> str:
    """
    Execute transfers using player names. IRREVERSIBLE.
    Provide lists of player names to transfer out and in.
    Example: player_names_out=["Salah"], player_names_in=["Haaland"]
    """
    # Deliberately not @_with_client: the read-only refusal has to come before the
    # session guard, because logging in would not change the answer.
    if _read_only():
        return (
            "Error: This server is running in read-only mode (FPL_READ_ONLY), so "
            "transfers are disabled. Report the recommendation instead of executing it."
        )

    client = _get_client()
    if not client:
        return NOT_AUTHENTICATED
    await _ensure_reference_data(client)
    
    if len(player_names_out) != len(player_names_in):
        return "Error: Number of players out must match number of players in."
    
    try:
        # Resolve player names to IDs
        ids_out = []
        ids_in = []
        
        for name in player_names_out:
            matches = store.find_players_by_name(name, fuzzy=True)
            if not matches:
                return f"Error: Could not find player '{name}' to transfer out."
            if _is_ambiguous(matches):
                return f"Error: Ambiguous player name '{name}'. Please be more specific."
            ids_out.append(matches[0][0].id)
        
        for name in player_names_in:
            matches = store.find_players_by_name(name, fuzzy=True)
            if not matches:
                return f"Error: Could not find player '{name}' to transfer in."
            if _is_ambiguous(matches):
                return f"Error: Ambiguous player name '{name}'. Please be more specific."
            ids_in.append(matches[0][0].id)
        
        # Get entry ID
        entry_id = store.get_user_entry_id(client)
        if not entry_id:
            return "Error: Could not determine your entry ID."
        
        # Execute transfers
        gw = await client.get_current_gameweek()
        my_team = await client.get_my_team(entry_id)
        current_map = {p['element']: p['selling_price'] for p in my_team['picks']}
        
        all_players = await client.get_players()
        cost_map = {p.id: p.now_cost for p in all_players}
        
        transfers = []
        for i in range(len(ids_out)):
            if ids_out[i] not in current_map:
                player_name = store.get_player_name(ids_out[i])
                return f"Error: You do not own {player_name}"
            transfers.append({
                "element_out": ids_out[i],
                "element_in": ids_in[i],
                "selling_price": current_map[ids_out[i]],
                "purchase_price": cost_map[ids_in[i]]
            })
            
        payload = TransferPayload(entry=entry_id, event=gw, transfers=transfers)
        res = await client.execute_transfers(payload)
        return f"Success: {res}"
    except Exception as e:
        return f"Transfer failed: {str(e)}"


@mcp.tool()
@_with_client()
async def get_my_performance(client) -> str:
    """
    Get your FPL performance including overall rank, gameweek rank, points, and league standings.
    Use this to check how you're doing in FPL.
    """
    
    try:
        entry_id = store.get_user_entry_id(client)
        if not entry_id:
            return "Error: Could not determine your entry ID."
        
        entry_data = await client.get_manager_entry(entry_id)
        
        output = [
            f"**{entry_data['name']}**",
            f"Manager: {entry_data['player_first_name']} {entry_data['player_last_name']}",
            f"Region: {entry_data['player_region_name']} ({entry_data['player_region_iso_code_short']})",
            f"Years Active: {entry_data['years_active']}",
            "",
            "**Current Season Performance:**",
            f"├─ Overall Points: {entry_data['summary_overall_points']:,}",
            f"├─ Overall Rank: {entry_data['summary_overall_rank']:,}",
            f"├─ Gameweek {entry_data['current_event']} Points: {entry_data['summary_event_points']}",
            f"├─ Gameweek {entry_data['current_event']} Rank: {entry_data['summary_event_rank']:,}",
            "",
            "**Team Value:**",
            f"├─ Squad Value: £{entry_data['last_deadline_value']/10:.1f}m",
            f"├─ Bank: £{entry_data['last_deadline_bank']/10:.1f}m",
            f"├─ Total Transfers: {entry_data['last_deadline_total_transfers']}",
            "",
        ]
        
        leagues = entry_data.get('leagues', {})
        classic_leagues = leagues.get('classic', [])
        
        if classic_leagues:
            output.append(f"**Leagues ({len(classic_leagues)}):**")
            
            overall_league = next((l for l in classic_leagues if l['name'] == 'Overall'), None)
            if overall_league:
                output.extend([
                    f"\n**Overall League:**",
                    f"├─ Rank: {overall_league['entry_rank']:,} / {overall_league['rank_count']:,}",
                    f"├─ Percentile: Top {overall_league['entry_percentile_rank']}%",
                ])
            
            other_leagues = [l for l in classic_leagues if l['name'] != 'Overall' and l['league_type'] == 'x']
            if other_leagues:
                output.append(f"\n**Private Leagues (Top 5):**")
                sorted_leagues = sorted(other_leagues, key=lambda x: x['entry_rank'])[:5]
                
                for league in sorted_leagues:
                    output.append(
                        f"├─ {league['name']}: "
                        f"Rank {league['entry_rank']}/{league['rank_count']} "
                        f"(Top {league['entry_percentile_rank']}%)"
                    )
        
        cup = leagues.get('cup', {})
        cup_status = cup.get('status', {})
        if cup_status.get('qualification_state'):
            output.extend([
                "",
                "**Cup Status:**",
                f"├─ Qualification: {cup_status['qualification_state']}",
            ])
        
        return "\n".join(output)
    except Exception as e:
        return f"Error fetching your performance: {str(e)}"


@mcp.tool()
@_with_client()
async def analyze_squad_recent_performance(client, num_gameweeks: int = 5) -> str:
    """
    Analyze recent gameweek performance for all players in your current squad.
    Shows detailed stats from the last N gameweeks to identify underperforming players
    who might be candidates for transfer, and inform players who are performing well.
    
    Args:
        num_gameweeks: Number of recent gameweeks to analyze (default: 5)
    
    Returns:
        Detailed analysis of each squad player's recent form with transfer recommendations
    """
    
    try:
        entry_id = store.get_user_entry_id(client)
        if not entry_id:
            return "Error: Could not determine your entry ID."
        
        # Get current squad
        my_team = await client.get_my_team(entry_id)
        picks = my_team['picks']
        
        # Get all players for price info
        all_players = await client.get_players()
        p_map = {p.id: p for p in all_players}
        
        output = [
            f"**Squad Performance Analysis (Last {num_gameweeks} Gameweeks)**\n",
            f"Bank: £{my_team['transfers']['bank']/10:.1f}m\n"
        ]
        
        # Analyze each player
        player_analyses = []
        
        for pick in picks:
            element_id = pick['element']
            player = p_map.get(element_id)
            if not player:
                continue
            
            # Fetch detailed player summary
            try:
                summary = await client.get_element_summary(element_id)
                history = summary.get('history', [])
                
                # Enrich history with team names
                history = store.enrich_gameweek_history(history)
                
                if not history:
                    player_analyses.append({
                        'player': player,
                        'pick': pick,
                        'avg_points': 0,
                        'avg_minutes': 0,
                        'total_points': 0,
                        'games_played': 0,
                        'recent_form': 'No data',
                        'recent_gws': [],
                        'transfers_balance': 0,
                        'last_gw_transfers': 0,
                        'transfer_sentiment': 'No data'
                    })
                    continue
                
                # Get last N gameweeks
                recent_gws = history[-num_gameweeks:]
                
                # Calculate stats
                total_points = sum(gw['total_points'] for gw in recent_gws)
                total_minutes = sum(gw['minutes'] for gw in recent_gws)
                games_played = len([gw for gw in recent_gws if gw['minutes'] > 0])
                avg_points = total_points / len(recent_gws) if recent_gws else 0
                avg_minutes = total_minutes / len(recent_gws) if recent_gws else 0
                
                # Calculate recent form trend (last 3 vs previous games)
                if len(recent_gws) >= 3:
                    last_3 = recent_gws[-3:]
                    prev_games = recent_gws[:-3] if len(recent_gws) > 3 else []
                    
                    last_3_avg = sum(gw['total_points'] for gw in last_3) / 3
                    prev_avg = sum(gw['total_points'] for gw in prev_games) / len(prev_games) if prev_games else last_3_avg
                    
                    if last_3_avg > prev_avg * 1.2:
                        form_trend = "📈 Improving"
                    elif last_3_avg < prev_avg * 0.8:
                        form_trend = "📉 Declining"
                    else:
                        form_trend = "➡️ Stable"
                else:
                    form_trend = "➡️ Stable"
                
                # Calculate transfer trends from recent gameweeks
                recent_transfers_balance = sum(gw.get('transfers_balance', 0) for gw in recent_gws)
                last_gw_transfers = recent_gws[-1].get('transfers_balance', 0) if recent_gws else 0
                
                # Determine transfer sentiment
                if recent_transfers_balance < -100000:
                    transfer_sentiment = "🔴 Heavy selling"
                elif recent_transfers_balance < -50000:
                    transfer_sentiment = "🟠 Moderate selling"
                elif recent_transfers_balance < -10000:
                    transfer_sentiment = "🟡 Light selling"
                elif recent_transfers_balance > 100000:
                    transfer_sentiment = "🟢 Heavy buying"
                elif recent_transfers_balance > 50000:
                    transfer_sentiment = "🟢 Moderate buying"
                elif recent_transfers_balance > 10000:
                    transfer_sentiment = "🟢 Light buying"
                else:
                    transfer_sentiment = "⚪ Stable"
                
                player_analyses.append({
                    'player': player,
                    'pick': pick,
                    'avg_points': avg_points,
                    'avg_minutes': avg_minutes,
                    'total_points': total_points,
                    'games_played': games_played,
                    'recent_form': form_trend,
                    'recent_gws': recent_gws,
                    'transfers_balance': recent_transfers_balance,
                    'last_gw_transfers': last_gw_transfers,
                    'transfer_sentiment': transfer_sentiment
                })
                
            except Exception as e:
                logger.error(f"Error fetching summary for player {element_id}: {e}")
                continue
        
        # Sort by average points (ascending to show worst performers first)
        player_analyses.sort(key=lambda x: x['avg_points'])
        
        # Categorize players
        underperformers = []
        solid_performers = []
        star_performers = []
        
        for analysis in player_analyses:
            avg_pts = analysis['avg_points']
            if avg_pts < 2.5:
                underperformers.append(analysis)
            elif avg_pts < 5:
                solid_performers.append(analysis)
            else:
                star_performers.append(analysis)
        
        # Output underperformers (transfer candidates)
        if underperformers:
            output.append(f"**🚨 UNDERPERFORMERS - Transfer Candidates ({len(underperformers)} players)**\n")
            for analysis in underperformers:
                player = analysis['player']
                pick = analysis['pick']
                role = " (C)" if pick['is_captain'] else " (VC)" if pick['is_vice_captain'] else ""
                bench = " [BENCH]" if pick['position'] > 11 else ""
                
                # Get last gameweek info
                last_gw = analysis['recent_gws'][-1] if analysis.get('recent_gws') else None
                last_gw_str = ""
                if last_gw:
                    opp_name = last_gw.get('opponent_team_short', f"Team {last_gw.get('opponent_team', '?')}")
                    ha = "H" if last_gw['was_home'] else "A"
                    last_gw_str = f" | Last GW: {last_gw['total_points']}pts, {last_gw['minutes']}min vs {opp_name}({ha})"
                    
                    # Add warning if didn't play last game
                    if last_gw['minutes'] == 0:
                        last_gw_str += " ⚠️ DNP"
                
                # Format transfer balance
                transfers_str = f"{analysis['transfers_balance']:+,}" if analysis['transfers_balance'] != 0 else "0"
                
                output.extend([
                    f"\n**{player.web_name}** ({player.team_name} {player.position}) £{pick['selling_price']/10:.1f}m{role}{bench}",
                    f"├─ Recent Form: {analysis['recent_form']}{last_gw_str}",
                    f"├─ Avg Points/Game: {analysis['avg_points']:.1f} (Last {num_gameweeks} GWs)",
                    f"├─ Total Points: {analysis['total_points']} in {analysis['games_played']} games",
                    f"├─ Avg Minutes: {analysis['avg_minutes']:.0f}/90",
                    f"├─ Community Sentiment: {analysis['transfer_sentiment']} ({transfers_str} net transfers)",
                ])
                
                # Show last 3 gameweeks detail
                if analysis.get('recent_gws'):
                    last_3 = analysis['recent_gws'][-3:]
                    gw_details = []
                    for gw in last_3:
                        opp_name = gw.get('opponent_team_short', f"Team {gw.get('opponent_team', '?')}")
                        ha = "H" if gw['was_home'] else "A"
                        mins_str = f", {gw['minutes']}min" if gw['minutes'] < 90 else ""
                        gw_details.append(f"GW{gw['round']}: {gw['total_points']}pts{mins_str} vs {opp_name}({ha})")
                    output.append(f"├─ Last 3 GWs: {' | '.join(gw_details)}")
                
                # Add recommendation with last game context and transfer sentiment
                recommendations = []
                
                if last_gw and last_gw['minutes'] == 0:
                    recommendations.append("Did not play last game - check injury/rotation status urgently")
                elif analysis['avg_minutes'] < 60:
                    recommendations.append("Low minutes - consider transferring out")
                elif analysis['avg_points'] < 2:
                    recommendations.append("Poor returns - strong transfer candidate")
                else:
                    recommendations.append("Underperforming - monitor closely")
                
                # Add transfer sentiment context
                if analysis['transfers_balance'] < -50000:
                    recommendations.append(f"Community is heavily selling ({analysis['transfers_balance']:,} net)")
                elif analysis['transfers_balance'] < -10000:
                    recommendations.append(f"Community losing confidence ({analysis['transfers_balance']:,} net)")
                
                rec_icon = "🚨" if (last_gw and last_gw['minutes'] == 0) or analysis['transfers_balance'] < -50000 else "⚠️"
                output.append(f"└─ {rec_icon} **RECOMMENDATION**: {' | '.join(recommendations)}")
        
        # Output solid performers
        if solid_performers:
            output.append(f"\n\n**✅ SOLID PERFORMERS - Keep ({len(solid_performers)} players)**\n")
            for analysis in solid_performers:
                player = analysis['player']
                pick = analysis['pick']
                role = " (C)" if pick['is_captain'] else " (VC)" if pick['is_vice_captain'] else ""
                
                # Get last game info
                last_gw = analysis['recent_gws'][-1] if analysis.get('recent_gws') else None
                last_gw_str = ""
                if last_gw:
                    last_gw_str = f" | Last: {last_gw['total_points']}pts"
                    if last_gw['minutes'] == 0:
                        last_gw_str += " ⚠️ DNP"
                    elif last_gw['minutes'] < 60:
                        last_gw_str += f" ({last_gw['minutes']}min)"
                
                # Add transfer sentiment if significant
                sentiment_str = ""
                if abs(analysis['transfers_balance']) > 10000:
                    sentiment_str = f" | {analysis['transfer_sentiment']}"
                
                output.append(
                    f"├─ {player.web_name} ({player.team_name} {player.position}): "
                    f"{analysis['avg_points']:.1f} pts/game | {analysis['recent_form']}{last_gw_str}{sentiment_str}"
                )
        
        # Output star performers
        if star_performers:
            output.append(f"\n\n**⭐ STAR PERFORMERS - Essential ({len(star_performers)} players)**\n")
            for analysis in star_performers:
                player = analysis['player']
                pick = analysis['pick']
                role = " (C)" if pick['is_captain'] else " (VC)" if pick['is_vice_captain'] else ""
                
                # Get last game info
                last_gw = analysis['recent_gws'][-1] if analysis.get('recent_gws') else None
                last_gw_str = ""
                if last_gw:
                    last_gw_str = f" | Last: {last_gw['total_points']}pts"
                    if last_gw['minutes'] == 0:
                        last_gw_str += " ⚠️ DNP"
                    elif last_gw['minutes'] < 60:
                        last_gw_str += f" ({last_gw['minutes']}min)"
                
                # Add transfer sentiment if significant
                sentiment_str = ""
                if abs(analysis['transfers_balance']) > 10000:
                    sentiment_str = f" | {analysis['transfer_sentiment']}"
                
                output.append(
                    f"├─ {player.web_name} ({player.team_name} {player.position}): "
                    f"{analysis['avg_points']:.1f} pts/game | {analysis['recent_form']}{last_gw_str}{sentiment_str}{role}"
                )
        
        # Summary recommendations
        output.extend([
            "\n\n**📊 SUMMARY**",
            f"├─ Underperformers: {len(underperformers)} players averaging <2.5 pts/game",
            f"├─ Solid Performers: {len(solid_performers)} players averaging 2.5-5 pts/game",
            f"├─ Star Performers: {len(star_performers)} players averaging >5 pts/game",
        ])
        
        if underperformers:
            output.append(f"\n**💡 TRANSFER PRIORITY**: Focus on replacing {underperformers[0]['player'].web_name} first")
        
        return "\n".join(output)
        
    except Exception as e:
        return f"Error analyzing squad performance: {str(e)}"


@mcp.tool()
@_with_client(fixtures=True)
async def recommend_chip_strategy(client) -> str:
    """
    Analyze your available chips and recommend optimal timing based on upcoming fixtures.
    Considers double gameweeks, blank gameweeks, and fixture difficulty to suggest when to play each chip.
    """
    
    try:
        entry_id = store.get_user_entry_id(client)
        if not entry_id:
            return "Error: Could not determine your entry ID."
        
        my_team = await client.get_my_team(entry_id)
        chips = my_team.get('chips', [])
        
        if not chips:
            return "Error: Chip data not available."
        
        available_chips = [c for c in chips if c['status_for_entry'] == 'available']
        
        if not available_chips:
            return "✅ All chips have been played! No chip strategy needed."
        
        # Get current gameweek
        current_gw = store.get_current_gameweek()
        if not current_gw:
            return "Error: Could not determine current gameweek."
        
        current_gw_id = current_gw.id
        
        # Analyze next 10 gameweeks for DGW/BGW
        fixtures_ahead = []
        for gw_num in range(current_gw_id, min(current_gw_id + 10, 39)):
            gw_fixtures = [f for f in store.fixtures_data if f.event == gw_num]
            
            # Count teams playing
            teams_playing = set()
            team_fixture_count = {}
            
            for fixture in gw_fixtures:
                teams_playing.add(fixture.team_h)
                teams_playing.add(fixture.team_a)
                team_fixture_count[fixture.team_h] = team_fixture_count.get(fixture.team_h, 0) + 1
                team_fixture_count[fixture.team_a] = team_fixture_count.get(fixture.team_a, 0) + 1
            
            # Detect DGW (teams playing twice)
            dgw_teams = [tid for tid, count in team_fixture_count.items() if count >= 2]
            
            # Detect BGW (less than 60% of teams playing)
            total_teams = len(store.bootstrap_data.teams) if store.bootstrap_data else 20
            is_bgw = len(teams_playing) < (total_teams * 0.6)
            
            fixtures_ahead.append({
                'gw': gw_num,
                'teams_playing': len(teams_playing),
                'dgw_teams': dgw_teams,
                'is_dgw': len(dgw_teams) > 0,
                'is_bgw': is_bgw,
                'fixtures': gw_fixtures
            })
        
        output = [
            "**Chip Strategy Recommendations**\n",
            f"Current Gameweek: {current_gw_id}",
            f"Available Chips: {', '.join([c['name'].upper() for c in available_chips])}\n"
        ]
        
        # Analyze each available chip
        chip_recommendations = []
        
        for chip in available_chips:
            chip_name = chip['name']
            chip_type = chip.get('chip_type', 'unknown')
            play_time = chip.get('play_time_type', 'unknown')
            
            if chip_name == 'wildcard':
                # Wildcard strategy
                rec = {
                    'chip': '🃏 WILDCARD',
                    'priority': 'MEDIUM',
                    'recommendations': []
                }
                
                # Check for DGW in next 5 gameweeks
                upcoming_dgws = [fw for fw in fixtures_ahead[:5] if fw['is_dgw']]
                
                if upcoming_dgws:
                    next_dgw = upcoming_dgws[0]
                    rec['recommendations'].append(
                        f"Consider using 1 GW before GW{next_dgw['gw']} (DGW with {len(next_dgw['dgw_teams'])} teams)"
                    )
                    rec['priority'] = 'HIGH'
                else:
                    rec['recommendations'].append(
                        "No immediate DGW detected. Use when you need major squad overhaul"
                    )
                
                # Check squad health
                picks = my_team['picks']
                all_players = await client.get_players()
                p_map = {p.id: p for p in all_players}
                
                injured_count = sum(1 for pick in picks if p_map.get(pick['element']) and p_map[pick['element']].status != 'a')
                
                if injured_count >= 3:
                    rec['recommendations'].append(f"⚠️ {injured_count} players unavailable - consider using soon")
                    rec['priority'] = 'HIGH'
                
                rec['recommendations'].append(
                    "💡 Pro tip: Use before a DGW to maximize new players' potential"
                )
                
                chip_recommendations.append(rec)
            
            elif chip_name == 'freehit':
                # Free Hit strategy
                rec = {
                    'chip': '🎯 FREE HIT',
                    'priority': 'LOW',
                    'recommendations': []
                }
                
                # Check for BGW
                upcoming_bgws = [fw for fw in fixtures_ahead[:8] if fw['is_bgw']]
                
                if upcoming_bgws:
                    next_bgw = upcoming_bgws[0]
                    rec['recommendations'].append(
                        f"🎯 SAVE for GW{next_bgw['gw']} (BGW - only {next_bgw['teams_playing']} teams playing)"
                    )
                    rec['priority'] = 'HIGH' if next_bgw['gw'] - current_gw_id <= 3 else 'MEDIUM'
                else:
                    # Check for DGW as backup
                    upcoming_dgws = [fw for fw in fixtures_ahead[:8] if fw['is_dgw']]
                    if upcoming_dgws:
                        next_dgw = upcoming_dgws[0]
                        rec['recommendations'].append(
                            f"Consider GW{next_dgw['gw']} (DGW) if no BGW expected"
                        )
                    else:
                        rec['recommendations'].append(
                            "No BGW or DGW detected. Save for emergency or late-season BGW"
                        )
                
                rec['recommendations'].append(
                    "💡 Pro tip: Best used in blank gameweeks when few teams play"
                )
                
                chip_recommendations.append(rec)
            
            elif chip_name == '3xc':
                # Triple Captain strategy
                rec = {
                    'chip': '⭐ TRIPLE CAPTAIN',
                    'priority': 'MEDIUM',
                    'recommendations': []
                }
                
                # Find premium players in squad
                picks = my_team['picks']
                all_players = await client.get_players()
                p_map = {p.id: p for p in all_players}
                
                premium_players = []
                for pick in picks:
                    player = p_map.get(pick['element'])
                    if player and player.now_cost >= 90:  # £9m+
                        premium_players.append({
                            'player': player,
                            'pick': pick
                        })
                
                if not premium_players:
                    rec['recommendations'].append("⚠️ No premium players (£9m+) in squad")
                    rec['priority'] = 'LOW'
                else:
                    # Check their upcoming fixtures
                    best_candidates = []
                    
                    for pp in premium_players:
                        player = pp['player']
                        
                        # Check next 5 fixtures
                        player_fixtures = []
                        for fw in fixtures_ahead[:5]:
                            for fixture in fw['fixtures']:
                                if fixture.team_h == player.team or fixture.team_a == player.team:
                                    is_home = fixture.team_h == player.team
                                    difficulty = fixture.team_h_difficulty if is_home else fixture.team_a_difficulty
                                    
                                    player_fixtures.append({
                                        'gw': fw['gw'],
                                        'is_dgw': player.team in fw['dgw_teams'],
                                        'difficulty': difficulty,
                                        'is_home': is_home
                                    })
                        
                        # Score the player
                        score = 0
                        best_gw = None
                        
                        for pf in player_fixtures:
                            gw_score = 0
                            if pf['is_dgw']:
                                gw_score += 50  # DGW is huge
                            gw_score += (6 - pf['difficulty']) * 10  # Easier fixtures better
                            if pf['is_home']:
                                gw_score += 5
                            
                            # Add form bonus
                            try:
                                form_score = float(player.form) * 5
                                gw_score += form_score
                            except:
                                pass
                            
                            if gw_score > score:
                                score = gw_score
                                best_gw = pf['gw']
                        
                        if best_gw:
                            best_candidates.append({
                                'player': player,
                                'score': score,
                                'best_gw': best_gw,
                                'has_dgw': any(pf['is_dgw'] for pf in player_fixtures)
                            })
                    
                    if best_candidates:
                        best_candidates.sort(key=lambda x: x['score'], reverse=True)
                        top_candidate = best_candidates[0]
                        
                        if top_candidate['has_dgw']:
                            rec['recommendations'].append(
                                f"🌟 STRONG: Use on {top_candidate['player'].web_name} in GW{top_candidate['best_gw']} (DGW)"
                            )
                            rec['priority'] = 'HIGH'
                        else:
                            rec['recommendations'].append(
                                f"Consider {top_candidate['player'].web_name} in GW{top_candidate['best_gw']} (good fixtures)"
                            )
                    else:
                        rec['recommendations'].append("Wait for better fixture opportunities")
                
                rec['recommendations'].append(
                    "💡 Pro tip: Best used on premium players in double gameweeks"
                )
                
                chip_recommendations.append(rec)
            
            elif chip_name == 'bboost':
                # Bench Boost strategy
                rec = {
                    'chip': '📊 BENCH BOOST',
                    'priority': 'LOW',
                    'recommendations': []
                }
                
                # Analyze bench quality
                picks = my_team['picks']
                all_players = await client.get_players()
                p_map = {p.id: p for p in all_players}
                
                bench_picks = [p for p in picks if p['position'] > 11]
                bench_quality = []
                
                for pick in bench_picks:
                    player = p_map.get(pick['element'])
                    if player:
                        try:
                            minutes = int(player.minutes) if hasattr(player, 'minutes') else 0
                            bench_quality.append({
                                'player': player,
                                'minutes': minutes,
                                'ppg': float(player.points_per_game) if player.points_per_game else 0
                            })
                        except:
                            pass
                
                avg_bench_minutes = sum(b['minutes'] for b in bench_quality) / len(bench_quality) if bench_quality else 0
                
                if avg_bench_minutes < 300:  # Less than ~3.5 games worth
                    rec['recommendations'].append(
                        f"⚠️ Weak bench (avg {avg_bench_minutes:.0f} mins) - improve before using"
                    )
                    rec['priority'] = 'LOW'
                else:
                    # Check for DGW
                    upcoming_dgws = [fw for fw in fixtures_ahead[:6] if fw['is_dgw']]
                    
                    if upcoming_dgws:
                        # Check if bench players have DGW
                        bench_dgw_count = 0
                        for bq in bench_quality:
                            for fw in upcoming_dgws:
                                if bq['player'].team in fw['dgw_teams']:
                                    bench_dgw_count += 1
                                    break
                        
                        if bench_dgw_count >= 2:
                            best_dgw = upcoming_dgws[0]
                            rec['recommendations'].append(
                                f"🎯 STRONG: Use in GW{best_dgw['gw']} ({bench_dgw_count} bench players have DGW)"
                            )
                            rec['priority'] = 'HIGH'
                        else:
                            rec['recommendations'].append(
                                f"Consider GW{upcoming_dgws[0]['gw']} (DGW) but only {bench_dgw_count} bench players benefit"
                            )
                    else:
                        rec['recommendations'].append(
                            "Wait for a double gameweek to maximize returns"
                        )
                
                rec['recommendations'].append(
                    "💡 Pro tip: Best used when bench players have double gameweeks"
                )
                
                chip_recommendations.append(rec)
        
        # Sort by priority
        priority_order = {'HIGH': 0, 'MEDIUM': 1, 'LOW': 2}
        chip_recommendations.sort(key=lambda x: priority_order.get(x['priority'], 3))
        
        # Output recommendations
        for rec in chip_recommendations:
            urgency_color = {
                'HIGH': '🔴',
                'MEDIUM': '🟡',
                'LOW': '🟢'
            }
            
            output.append(f"\n**{rec['chip']}** {urgency_color[rec['priority']]} {rec['priority']} PRIORITY")
            for recommendation in rec['recommendations']:
                output.append(f"├─ {recommendation}")
        
        # Add fixture overview
        output.append("\n\n**Upcoming Fixture Overview:**")
        for fw in fixtures_ahead[:6]:
            status = []
            if fw['is_dgw']:
                status.append(f"DGW ({len(fw['dgw_teams'])} teams)")
            if fw['is_bgw']:
                status.append(f"BGW ({fw['teams_playing']} teams)")
            
            status_str = " - " + ", ".join(status) if status else ""
            output.append(f"├─ GW{fw['gw']}: {fw['teams_playing']} teams playing{status_str}")
        
        return "\n".join(output)
        
    except Exception as e:
        return f"Error analyzing chip strategy: {str(e)}"


@mcp.tool()
@_with_client(fixtures=True)
async def recommend_transfers(client) -> str:
    """
    Analyze your squad and recommend transfer strategy based on available free transfers,
    upcoming fixtures, player form, and injury status. Considers the economics of points hits.
    """
    
    try:
        entry_id = store.get_user_entry_id(client)
        if not entry_id:
            return "Error: Could not determine your entry ID."
        
        my_team = await client.get_my_team(entry_id)
        picks = my_team['picks']
        transfers = my_team.get('transfers') or {}

        transfer_limit = _optional_int(transfers.get('limit'))
        if transfer_limit is None:
            return (
                "**Transfer Recommendations**\n\n"
                "Transfers are not applicable before GW1. Build and refine the initial "
                "15-player squad instead; transfer advice becomes available once the "
                "season starts."
            )
        free_transfers = max(
            0,
            transfer_limit - (_optional_int(transfers.get('made')) or 0),
        )
        transfer_cost = _optional_int(transfers.get('cost')) or 0
        
        # Get all players
        all_players = await client.get_players()
        p_map = {p.id: p for p in all_players}
        
        # Get current gameweek
        current_gw = store.get_current_gameweek()
        if not current_gw:
            return "Error: Could not determine current gameweek."
        
        current_gw_id = current_gw.id
        
        output = [
            "**Transfer Recommendations**\n",
            f"Free Transfers Available: {free_transfers}",
            f"Transfer Cost: {transfer_cost} points per additional transfer",
            f"Current Gameweek: {current_gw_id}\n"
        ]
        
        # Analyze each player
        player_priorities = []
        
        for pick in picks:
            player = p_map.get(pick['element'])
            if not player:
                continue
            
            # Get player's next 5 fixtures
            player_fixtures = []
            for fixture in store.upcoming_fixtures(
                player.team, from_gameweek=current_gw_id, limit=5
            ):
                is_home = fixture.team_h == player.team
                difficulty = fixture.team_h_difficulty if is_home else fixture.team_a_difficulty
                
                player_fixtures.append({
                    'gw': fixture.event,
                    'difficulty': difficulty,
                    'is_home': is_home
                })
            
            # Calculate priority score (higher = more urgent to transfer out)
            priority_score = 0
            reasons = []
            
            # 1. Availability status (most important)
            if player.status != 'a':
                priority_score += 100
                status_map = {'i': 'Injured', 'd': 'Doubtful', 's': 'Suspended', 'u': 'Unavailable'}
                reasons.append(f"🚨 {status_map.get(player.status, 'Unavailable')}")
            
            # 2. Did not play last game
            try:
                summary = await client.get_element_summary(player.id)
                history = summary.get('history', [])
                if history:
                    last_gw = history[-1]
                    if last_gw['minutes'] == 0:
                        priority_score += 50
                        reasons.append("⚠️ DNP last game")
            except:
                pass
            
            # 3. Fixture difficulty (next 3 games)
            if player_fixtures:
                avg_difficulty = sum(f['difficulty'] for f in player_fixtures[:3]) / min(3, len(player_fixtures))
                if avg_difficulty >= 4:
                    priority_score += 30
                    reasons.append(f"Hard fixtures (avg {avg_difficulty:.1f}/5)")
                elif avg_difficulty >= 3.5:
                    priority_score += 15
                    reasons.append(f"Tough fixtures (avg {avg_difficulty:.1f}/5)")
            
            # 4. Poor form
            try:
                form = float(player.form) if player.form else 0
                if form < 2:
                    priority_score += 25
                    reasons.append(f"Poor form ({form})")
                elif form < 3:
                    priority_score += 10
                    reasons.append(f"Low form ({form})")
            except:
                pass
            
            # 5. Low minutes
            try:
                minutes = int(player.minutes) if hasattr(player, 'minutes') else 0
                if minutes < 200:  # Less than ~2 full games
                    priority_score += 20
                    reasons.append(f"Low minutes ({minutes})")
            except:
                pass
            
            if priority_score > 0:
                player_priorities.append({
                    'player': player,
                    'pick': pick,
                    'priority_score': priority_score,
                    'reasons': reasons,
                    'fixtures': player_fixtures[:3]
                })
        
        # Sort by priority
        player_priorities.sort(key=lambda x: x['priority_score'], reverse=True)
        
        # Strategic recommendations based on free transfers
        output.append("**Strategic Advice:**\n")
        
        if free_transfers == 0:
            output.extend([
                "🔴 **0 Free Transfers**",
                "├─ Only take a hit (-4pts) if:",
                "│  • Player is injured/suspended (unavailable)",
                "│  • Replacement has a double gameweek",
                "│  • Replacement expected to score 6+ more points (to break even)",
                "└─ Otherwise, wait for next gameweek to bank a free transfer\n"
            ])
        elif free_transfers == 1:
            output.extend([
                "🟡 **1 Free Transfer**",
                "├─ Consider banking if no urgent issues",
                "├─ Use it if you have:",
                "│  • Injured/suspended player",
                "│  • Player with very poor fixtures",
                "└─ Banking gives you 2 FT next week for more flexibility\n"
            ])
        else:  # 2 or more
            output.extend([
                "🟢 **2 Free Transfers**",
                "├─ Good flexibility to fix issues",
                "├─ Address top 2 priority problems",
                "├─ Don't waste transfers - only make valuable moves",
                "└─ Unused transfers don't roll over beyond 2\n"
            ])
        
        # Show top transfer candidates
        if player_priorities:
            output.append("**Players to Consider Transferring Out:**\n")
            
            for i, pp in enumerate(player_priorities[:5], 1):
                player = pp['player']
                pick = pp['pick']
                
                bench_indicator = " [BENCH]" if pick['position'] > 11 else ""
                
                output.extend([
                    f"**{i}. {player.web_name}** ({player.team_name} {player.position}) {_pick_price_text(pick)}{bench_indicator}",
                    f"├─ Priority Score: {pp['priority_score']} - {', '.join(pp['reasons'])}"
                ])
                
                # Show next 3 fixtures
                if pp['fixtures']:
                    fixtures_str = []
                    for f in pp['fixtures']:
                        ha = "H" if f['is_home'] else "A"
                        diff_str = _difficulty_bar(f['difficulty'])
                        fixtures_str.append(f"GW{f['gw']}({ha}): {diff_str}")
                    output.append(f"├─ Next fixtures: {' | '.join(fixtures_str)}")
                
                # Transfer recommendation
                if pp['priority_score'] >= 100:
                    output.append(f"└─ 🚨 **URGENT**: Transfer out immediately")
                elif pp['priority_score'] >= 50:
                    output.append(f"└─ ⚠️ **HIGH PRIORITY**: Strong transfer candidate")
                elif pp['priority_score'] >= 30:
                    output.append(f"└─ 🟡 **MEDIUM**: Consider if you have spare FT")
                else:
                    output.append(f"└─ 🟢 **LOW**: Monitor, not urgent")
                
                output.append("")
        else:
            output.append("✅ **No immediate transfer concerns!**\n")
            output.append("Your squad looks healthy. Consider banking your free transfer.\n")
        
        # Points hit economics
        output.extend([
            "\n**Points Hit Economics:**",
            "├─ Each additional transfer costs 4 points",
            "├─ Replacement must score 6+ more points to break even:",
            "│  • 4 points to recover the hit",
            "│  • 2+ points to actually gain value",
            "└─ Only take hits for injured players or exceptional opportunities\n"
        ])
        
        # Timing advice
        output.extend([
            "**Timing Considerations:**",
            "├─ Make transfers early in the week to monitor price changes",
            "├─ But wait for Friday press conferences for injury news",
            "├─ Check lineup predictions before finalizing",
            "└─ Consider banking transfers for future flexibility"
        ])
        
        return "\n".join(output)
        
    except Exception as e:
        return f"Error analyzing transfers: {str(e)}"

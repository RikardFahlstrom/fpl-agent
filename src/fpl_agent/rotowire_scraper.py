"""RotoWire scraper for Premier League predicted lineups and injury status.

The page publishes, per fixture, both teams' expected starting elevens and their
unavailable players. The previous parser read only the injury flags and attributed every
player to team "Unknown", because it looked for a `lineup__abbr` element that the page
does not use - team codes live in `lineup__team`. The predicted lineups, which are the
more valuable half, were discarded entirely.

Two things to know when consuming this:

- A **Predicted** lineup is a forecast. Confirmed lineups appear about an hour before
  kickoff, which is after the FPL deadline, so a decision is nearly always made against a
  prediction. `MatchLineup.confirmed` says which you are holding.
- RotoWire's team codes match FPL's `short_name` for 19 of 20 clubs. Nottingham Forest is
  `NOT` here and `NFO` in FPL; without the alias below an entire club goes missing.
- Each team's list holds the eleven, then an "Injuries" separator, then the unavailable
  players. **The tail is not a bench** - RotoWire does not publish one here - and a player
  can appear on both sides of the separator, as a doubtful starter does. Splitting on the
  separator rather than on a count keeps that distinction, and a count would silently
  turn an injury list into a bench if the page ever listed twelve.
"""

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import httpx
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

# RotoWire code -> FPL short_name, for the codes that differ.
TEAM_ALIASES = {"NOT": "NFO"}

STARTING_XI = 11

# RotoWire injury shorthand -> the status the FPL tools expect.
INJURY_STATUS = {
    "OUT": "OUT",
    "QUES": "DOUBTFUL",
    "DOUBT": "DOUBTFUL",
    "GTD": "DOUBTFUL",
    "SUSP": "OUT",
}


@dataclass
class PlayerLineupStatus:
    """Player lineup status from RotoWire."""
    player_name: str
    team: str
    status: str  # OUT, DOUBTFUL, EXPECTED, CONFIRMED
    reason: str
    confidence: float


@dataclass
class LineupPlayer:
    """One player in a published lineup, or in the injury list beneath it."""
    name: str
    team: str
    position: str          # RotoWire's own notation: GK, DC, DMC, AMR, FW ...
    is_starter: bool
    injury: Optional[str] = None   # RotoWire shorthand, e.g. OUT, QUES

    @property
    def available(self) -> bool:
        return self.injury is None


@dataclass
class MatchLineup:
    """Both teams' lineups for one fixture, and the injury list published beneath them."""
    home_team: str
    away_team: str
    confirmed: bool                       # False means predicted, which is the usual case
    players: List[LineupPlayer] = field(default_factory=list)
    injuries: List[LineupPlayer] = field(default_factory=list)

    @property
    def status(self) -> str:
        return "CONFIRMED" if self.confirmed else "PREDICTED"

    def starters(self, team: Optional[str] = None) -> List[LineupPlayer]:
        return [p for p in self.players
                if p.is_starter and (team is None or p.team == team)]

    def unavailable(self, team: Optional[str] = None) -> List[LineupPlayer]:
        """The injury list. Not a bench: RotoWire does not publish substitutes here."""
        return [p for p in self.injuries if team is None or p.team == team]


class RotoWireLineupScraper:
    """Scraper for RotoWire Premier League lineup predictions."""

    def __init__(self):
        self.base_url = "https://www.rotowire.com/soccer/lineups.php"
        self.headers = {
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.5",
            "Connection": "keep-alive",
            "Upgrade-Insecure-Requests": "1",
        }

    async def _fetch(self) -> Optional[BeautifulSoup]:
        try:
            async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
                response = await client.get(self.base_url, headers=self.headers)
            if response.status_code != 200:
                logger.error("Failed to fetch RotoWire page: HTTP %s", response.status_code)
                return None
            return BeautifulSoup(response.text, "html.parser")
        except Exception as e:
            logger.error("Failed to fetch RotoWire lineups: %s", e)
            return None

    async def scrape_match_lineups(self) -> List[MatchLineup]:
        """Predicted (or confirmed) lineups for every fixture on the page."""
        soup = await self._fetch()
        if soup is None:
            return []
        matches = self.parse_match_lineups(soup)
        logger.info(
            "Scraped %s fixtures: %s starters, %s unavailable",
            len(matches),
            sum(len(m.starters()) for m in matches),
            sum(len(m.unavailable()) for m in matches),
        )
        return matches

    async def scrape_premier_league_lineups(self) -> List[PlayerLineupStatus]:
        """Flat player statuses, as the FPL tools consume them.

        Derived from the lineups rather than from injury flags alone, so players now
        carry their real team and expected starters populate the EXPECTED bucket, which
        was previously always empty.
        """
        matches = await self.scrape_match_lineups()
        statuses = [status for match in matches for status in self.to_statuses(match)]
        counts: Dict[str, int] = {}
        for entry in statuses:
            counts[entry.status] = counts.get(entry.status, 0) + 1
        logger.info("Player statuses: %s", counts or "none")
        return statuses

    # -- parsing ---------------------------------------------------------------------

    @staticmethod
    def normalise_team(code: str) -> str:
        code = (code or "").strip().upper()
        return TEAM_ALIASES.get(code, code)

    def parse_match_lineups(self, soup: BeautifulSoup) -> List[MatchLineup]:
        """Parse every fixture box. Takes soup so it can be tested against saved HTML."""
        matches: List[MatchLineup] = []
        for box in soup.select(".lineup__box"):
            codes = [t.get_text(" ", strip=True) for t in box.select(".lineup__team")]
            if len(codes) < 2:
                continue                      # promotional or non-match boxes
            home, away = (self.normalise_team(codes[0]), self.normalise_team(codes[1]))

            status_text = box.select_one(".lineup__status")
            confirmed = bool(status_text) and "confirm" in status_text.get_text().lower()

            match = MatchLineup(home_team=home, away_team=away, confirmed=confirmed)
            for selector, team in ((".lineup__list.is-home", home),
                                   (".lineup__list.is-visit", away)):
                container = box.select_one(selector)
                if container is None:
                    continue
                lineup, unavailable, side_confirmed = self._parse_side(container, team)
                match.players.extend(lineup)
                match.injuries.extend(unavailable)
                # The per-team status is more precise than the box-level one.
                if side_confirmed is not None:
                    match.confirmed = side_confirmed

            if match.players:
                matches.append(match)
        return matches

    def _parse_side(self, container, team: str):
        """Split one team's list at the "Injuries" separator.

        Returns (lineup, unavailable, confirmed). `confirmed` is None when the side
        carries no status of its own.
        """
        lineup: List[LineupPlayer] = []
        unavailable: List[LineupPlayer] = []
        confirmed: Optional[bool] = None
        past_separator = False

        for entry in container.find_all(recursive=False):
            classes = entry.get("class") or []

            if "lineup__status" in classes:
                confirmed = "confirm" in entry.get_text().lower()
                continue
            if "lineup__title" in classes:
                past_separator = True      # everything below is the injury list
                continue
            if "lineup__player" not in classes:
                continue

            link = entry.find("a")
            if link is None:
                continue
            name = (link.get("title") or link.get_text(strip=True) or "").strip()
            if not name:
                continue

            position = entry.select_one(".lineup__pos")
            injury = entry.select_one(".lineup__inj")
            player = LineupPlayer(
                name=name,
                team=team,
                position=position.get_text(strip=True) if position else "",
                is_starter=not past_separator,
                injury=injury.get_text(strip=True).upper() if injury else None,
            )
            (unavailable if past_separator else lineup).append(player)

        return lineup, unavailable, confirmed

    @staticmethod
    def to_statuses(match: MatchLineup) -> List[PlayerLineupStatus]:
        """Flatten one fixture into the status records the FPL tools expect."""
        statuses: List[PlayerLineupStatus] = []
        # The injury list first, so a doubtful starter - who appears on both sides of the
        # separator - is reported as doubtful rather than as an expected starter.
        seen: set = set()
        for player in match.injuries + match.players:
            if (player.name, player.team) in seen:
                continue
            seen.add((player.name, player.team))
            if player.injury:
                status = INJURY_STATUS.get(player.injury, "DOUBTFUL")
                reason = f"Listed as {player.injury} on RotoWire"
                confidence = 0.95 if status == "OUT" else 0.6
            elif player.is_starter:
                status = "CONFIRMED" if match.confirmed else "EXPECTED"
                reason = (f"{'Confirmed' if match.confirmed else 'Predicted'} to start "
                          f"({player.position})")
                confidence = 0.95 if match.confirmed else 0.75
            else:
                continue          # a fit substitute is not a prediction worth reporting
            statuses.append(PlayerLineupStatus(
                player_name=player.name, team=player.team,
                status=status, reason=reason, confidence=confidence))
        return statuses

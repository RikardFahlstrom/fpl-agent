"""Lineup parsing, against saved RotoWire HTML so the suite stays offline."""
import unittest
from pathlib import Path

from bs4 import BeautifulSoup

from fpl_agent.api.rotowire_scraper import RotoWireLineupScraper

FIXTURE = Path(__file__).parent / "fixtures" / "rotowire_lineups.html"


class LineupParsingTests(unittest.TestCase):
    def setUp(self):
        self.scraper = RotoWireLineupScraper()
        self.soup = BeautifulSoup(FIXTURE.read_text(), "html.parser")
        self.matches = self.scraper.parse_match_lineups(self.soup)

    def _match(self, home, away):
        return next(m for m in self.matches
                    if m.home_team == home and m.away_team == away)

    def test_fixtures_and_teams_are_identified(self):
        """Regression: every player was attributed to team "Unknown"."""
        self.assertEqual(len(self.matches), 2)
        for match in self.matches:
            self.assertNotEqual(match.home_team, "Unknown")
            self.assertNotEqual(match.away_team, "Unknown")
        self.assertEqual(self._match("IPS", "LIV").away_team, "LIV")

    def test_nottingham_forest_is_mapped_to_the_fpl_code(self):
        """RotoWire says NOT, FPL says NFO; without the alias a whole club vanishes."""
        self.assertEqual(RotoWireLineupScraper.normalise_team("NOT"), "NFO")
        self.assertEqual(RotoWireLineupScraper.normalise_team("liv"), "LIV")
        self.assertTrue(any("NFO" in (m.home_team, m.away_team) for m in self.matches))

    def test_each_side_has_exactly_eleven_starters(self):
        for match in self.matches:
            for team in (match.home_team, match.away_team):
                self.assertEqual(len(match.starters(team)), 11, f"{team} XI")

    def test_the_injury_list_is_not_a_bench(self):
        """Everything below the "Injuries" separator is unavailable, not substitutes."""
        match = self._match("IPS", "LIV")
        unavailable = match.unavailable("LIV")
        self.assertTrue(unavailable)
        for player in unavailable:
            self.assertIsNotNone(player.injury,
                                 f"{player.name} is in the injury list without a flag")
            self.assertFalse(player.is_starter)

    def test_a_doubtful_starter_appears_in_both_sections(self):
        """RotoWire lists a questionable starter in the XI and in the injury list."""
        match = self._match("IPS", "LIV")
        names = {p.name for p in match.starters("IPS")}
        flagged = {p.name for p in match.unavailable("IPS")}
        self.assertTrue(names & flagged, "expected at least one doubtful starter")

    def test_players_carry_position_and_injury(self):
        keeper = self._match("IPS", "LIV").starters("LIV")[0]
        self.assertEqual(keeper.position, "GK")
        self.assertIsNone(keeper.injury)
        self.assertTrue(keeper.available)

    def test_predicted_is_distinguished_from_confirmed(self):
        """A prediction is what a decision rests on; confirmation comes after the deadline."""
        match = self._match("IPS", "LIV")
        self.assertFalse(match.confirmed)
        self.assertEqual(match.status, "PREDICTED")

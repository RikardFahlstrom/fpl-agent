"""Resolving RotoWire lineups to FPL players, and feeding them into projections."""
import unittest

from fpl_agent import rotowire_scraper
from fpl_agent.engine import lineups, storage
from fpl_agent.engine.lineups import fold, resolve_element_id, squad_index
from fpl_agent.rotowire_scraper import LineupPlayer, MatchLineup

BOOTSTRAP = {
    "teams": [{"id": 1, "short_name": "LIV"}, {"id": 2, "short_name": "MCI"}],
    "elements": [
        # FPL calls the Liverpool keeper A.Becker; RotoWire only ever says Alisson.
        {"id": 10, "web_name": "A.Becker", "first_name": "Alisson", "second_name": "Becker",
         "team": 1},
        {"id": 11, "web_name": "Isak", "first_name": "Alexander", "second_name": "Isak",
         "team": 1},
        {"id": 12, "web_name": "Rúben", "first_name": "Rúben",
         "second_name": "dos Santos Gato Alves Dias", "team": 2},
        {"id": 13, "web_name": "Petrović", "first_name": "Đorđe", "second_name": "Petrović",
         "team": 2},
        {"id": 14, "web_name": "Haaland", "first_name": "Erling", "second_name": "Haaland",
         "team": 2},
    ],
}


class FoldTests(unittest.TestCase):
    def test_diacritics_are_stripped(self):
        self.assertEqual(fold("Yéremy Pino"), "yeremy pino")
        self.assertEqual(fold("Petrović"), "petrovic")

    def test_characters_nfkd_does_not_decompose(self):
        """Đ and Ø survive NFKD, so they need transliterating explicitly."""
        self.assertEqual(fold("Đorđe"), "dorde")
        self.assertEqual(fold("Ødegaard"), "odegaard")


class ResolveTests(unittest.TestCase):
    def setUp(self):
        self.index = squad_index(BOOTSTRAP)

    def test_exact_web_name(self):
        self.assertEqual(resolve_element_id(self.index, "Haaland", "MCI"), 14)

    def test_first_name_only(self):
        """Regression: 'Alisson' matched nothing, because FPL's web_name is 'A.Becker'."""
        self.assertEqual(resolve_element_id(self.index, "Alisson", "LIV"), 10)

    def test_diacritics_and_long_official_names(self):
        self.assertEqual(resolve_element_id(self.index, "Djordje Petrovic", "MCI"), 13)
        self.assertEqual(resolve_element_id(self.index, "Ruben Dias", "MCI"), 12)

    def test_full_name_when_fpl_uses_a_short_one(self):
        self.assertEqual(resolve_element_id(self.index, "Alexander Isak", "LIV"), 11)

    def test_the_club_constrains_the_search(self):
        """A name is only matched within its own club, so a namesake cannot win."""
        self.assertIsNone(resolve_element_id(self.index, "Haaland", "LIV"))

    def test_a_player_fpl_does_not_list_resolves_to_nothing(self):
        """Youth players appear on RotoWire and not in the FPL game."""
        self.assertIsNone(resolve_element_id(self.index, "Stephen Mfuni", "MCI"))

    def test_unknown_club(self):
        self.assertIsNone(resolve_element_id(self.index, "Isak", "ARS"))


def _match(confirmed=False):
    return MatchLineup(
        home_team="LIV", away_team="MCI", confirmed=confirmed,
        players=[LineupPlayer("Alisson", "LIV", "GK", True),
                 LineupPlayer("Alexander Isak", "LIV", "FW", True, injury="QUES"),
                 LineupPlayer("Haaland", "MCI", "FW", True)],
        injuries=[LineupPlayer("Alexander Isak", "LIV", "F", False, injury="QUES"),
                  LineupPlayer("Ruben Dias", "MCI", "D", False, injury="OUT")],
    )


class RecordTests(unittest.TestCase):
    def setUp(self):
        self.conn = storage.connect(":memory:")
        self.addCleanup(self.conn.close)
        self.conn.execute("INSERT INTO snapshot (captured_at, gameweek, kind) "
                          "VALUES ('t', 3, 'test')")
        self.snapshot_id = self.conn.execute(
            "SELECT MAX(id) AS id FROM snapshot").fetchone()["id"]

    def test_rows_are_written_and_unresolved_reported(self):
        captured = lineups.record_lineups(
            self.conn, self.snapshot_id, 3,
            [MatchLineup("LIV", "MCI", False,
                         players=[LineupPlayer("Nobody At All", "LIV", "FW", True),
                                  LineupPlayer("Haaland", "MCI", "FW", True)])],
            BOOTSTRAP)
        self.assertEqual(captured.rows, 1)
        self.assertEqual(len(captured.unresolved), 1)
        self.assertIn("Nobody At All", captured.unresolved[0])

    def test_a_doubtful_starter_is_stored_once_keeping_the_flag(self):
        """The player appears in the XI and the injury list; the flag has to survive.

        And so does the selection: the injury entry says `is_starter` 0 only because it
        sits below the separator, not because anyone benched him.
        """
        lineups.record_lineups(self.conn, self.snapshot_id, 3, [_match()], BOOTSTRAP)
        rows = self.conn.execute(
            "SELECT * FROM predicted_lineup WHERE element_id = 11").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["injury"], "QUES")
        self.assertEqual(rows[0]["is_starter"], 1)

    def test_the_position_stored_is_the_one_he_will_play(self):
        """The XI names the shirt; the injury list abbreviates it."""
        lineups.record_lineups(self.conn, self.snapshot_id, 3, [_match()], BOOTSTRAP)
        row = self.conn.execute(
            "SELECT * FROM predicted_lineup WHERE element_id = 11").fetchone()
        self.assertEqual(row["position"], "FW")

    def test_a_player_only_in_the_injury_list_is_not_promoted(self):
        """Nothing about the merge should invent a start for a player nobody named."""
        lineups.record_lineups(self.conn, self.snapshot_id, 3, [_match()], BOOTSTRAP)
        row = self.conn.execute(
            "SELECT * FROM predicted_lineup WHERE element_id = 12").fetchone()
        self.assertEqual(row["is_starter"], 0)
        self.assertEqual(row["injury"], "OUT")


def _fixtures(*rows) -> list[dict]:
    """(id, event, home id, away id, finished) as the fixtures endpoint returns them."""
    return [{"id": i, "event": e, "team_h": h, "team_a": a, "finished": f}
            for i, e, h, a, f in rows]


class FixtureEventTests(unittest.TestCase):
    """Which round a scraped match is in - the question the RotoWire page cannot answer."""

    def test_a_pair_maps_to_its_event(self):
        index = lineups.fixture_events(_fixtures((1, 3, 1, 2, False)), BOOTSTRAP)
        self.assertEqual(index[frozenset(("LIV", "MCI"))], 3)

    def test_the_order_of_the_pair_does_not_matter(self):
        """RotoWire's home/away is a page ordering; the warehouse's is the real one."""
        index = lineups.fixture_events(_fixtures((1, 3, 2, 1, False)), BOOTSTRAP)
        self.assertEqual(index[frozenset(("LIV", "MCI"))], 3)

    def test_a_finished_fixture_is_not_a_destination(self):
        index = lineups.fixture_events(_fixtures((1, 3, 1, 2, True)), BOOTSTRAP)
        self.assertEqual(index, {})

    def test_the_earliest_unfinished_meeting_wins(self):
        """Both legs are unfinished in August; the one about to be played is the answer."""
        index = lineups.fixture_events(
            _fixtures((1, 3, 1, 2, False), (2, 25, 2, 1, False)), BOOTSTRAP)
        self.assertEqual(index[frozenset(("LIV", "MCI"))], 3)

    def test_the_reverse_leg_takes_over_once_the_first_is_played(self):
        index = lineups.fixture_events(
            _fixtures((1, 3, 1, 2, True), (2, 25, 2, 1, False)), BOOTSTRAP)
        self.assertEqual(index[frozenset(("LIV", "MCI"))], 25)

    def test_an_unscheduled_fixture_is_skipped(self):
        """A postponed fixture carries a null event until it is rescheduled."""
        self.assertEqual(
            lineups.fixture_events(_fixtures((1, None, 1, 2, False)), BOOTSTRAP), {})


class FilingTests(unittest.TestCase):
    """A match belongs to the round its fixture is in, not the one bootstrap has moved to.

    The window is between a gameweek's deadline and its last kickoff: FPL flips `is_next`
    at the deadline while RotoWire still shows the round being played, and the hourly
    cron fires straight through it.
    """

    def setUp(self):
        self.conn = storage.connect(":memory:")
        self.addCleanup(self.conn.close)
        self.conn.execute("INSERT INTO snapshot (captured_at, gameweek, kind) "
                          "VALUES ('t', 4, 'test')")
        self.snapshot_id = self.conn.execute(
            "SELECT MAX(id) AS id FROM snapshot").fetchone()["id"]

    def _record(self, fixtures, gameweek=4):
        return lineups.record_lineups(
            self.conn, self.snapshot_id, gameweek, [_match()], BOOTSTRAP,
            fixture_events=lineups.fixture_events(fixtures, BOOTSTRAP))

    def _gameweeks(self) -> list[int]:
        return [r["gameweek"] for r in self.conn.execute(
            "SELECT DISTINCT gameweek FROM predicted_lineup ORDER BY gameweek")]

    def test_a_snapshot_taken_after_the_deadline_files_under_the_round_being_played(self):
        """The bug: LIV-MCI is a gameweek 3 fixture and the snapshot targets 4."""
        captured = self._record(_fixtures((1, 3, 1, 2, False)))
        self.assertEqual(self._gameweeks(), [3])
        self.assertEqual(captured.gameweeks, [3])
        self.assertEqual(captured.unplaceable, [])

    def test_the_ordinary_case_is_unchanged(self):
        """Every pair already in the target gameweek must file exactly where it did."""
        captured = self._record(_fixtures((1, 4, 1, 2, False)))
        self.assertEqual(self._gameweeks(), [4])
        self.assertEqual(captured.gameweeks, [4])

    def test_a_pair_in_no_unfinished_fixture_is_named_in_a_warning(self):
        """A silent fallback to the target is how this bug survived; it must be loud."""
        with self.assertLogs(lineups.logger, level="WARNING") as logs:
            captured = self._record(_fixtures((1, 3, 1, 2, True)))
        self.assertIn("LIV-MCI", " ".join(logs.output))
        self.assertEqual(captured.unplaceable, ["LIV-MCI"])
        self.assertEqual(self._gameweeks(), [4])    # the target, and said so

    def test_a_double_gameweek_takes_the_earliest_unfinished_meeting(self):
        self._record(_fixtures((1, 3, 1, 2, False), (2, 5, 2, 1, False)))
        self.assertEqual(self._gameweeks(), [3])

    def test_without_a_fixture_map_the_target_is_still_used(self):
        """Keeps `record_lineups` callable, and testable, without a fixture table."""
        lineups.record_lineups(self.conn, self.snapshot_id, 4, [_match()], BOOTSTRAP)
        self.assertEqual(self._gameweeks(), [4])

    def test_a_page_straddling_a_deadline_files_each_match_where_it_belongs(self):
        """A Monday-night game and the next round's Friday opener can share a page.

        Both rounds land, and the caller is told both, rather than one gameweek standing
        for every row written.
        """
        bootstrap = {
            "teams": BOOTSTRAP["teams"] + [{"id": 3, "short_name": "ARS"},
                                           {"id": 4, "short_name": "CHE"}],
            "elements": BOOTSTRAP["elements"] + [
                {"id": 15, "web_name": "Saka", "first_name": "Bukayo",
                 "second_name": "Saka", "team": 3},
                {"id": 16, "web_name": "Palmer", "first_name": "Cole",
                 "second_name": "Palmer", "team": 4}],
        }
        monday = MatchLineup("LIV", "MCI", False,
                             players=[LineupPlayer("Haaland", "MCI", "FW", True)])
        friday = MatchLineup("ARS", "CHE", False,
                             players=[LineupPlayer("Saka", "ARS", "AMR", True)])
        captured = lineups.record_lineups(
            self.conn, self.snapshot_id, 4, [monday, friday], bootstrap,
            fixture_events=lineups.fixture_events(
                _fixtures((1, 3, 1, 2, False), (2, 4, 3, 4, False)), bootstrap))
        self.assertEqual(captured.gameweeks, [3, 4])
        self.assertEqual(self._gameweeks(), [3, 4])
        self.assertEqual(
            self.conn.execute("SELECT gameweek FROM predicted_lineup "
                              "WHERE element_id = 14").fetchone()["gameweek"], 3)


class StartRateTests(unittest.TestCase):
    def setUp(self):
        self.conn = storage.connect(":memory:")
        self.addCleanup(self.conn.close)
        for element in BOOTSTRAP["elements"]:
            self.conn.execute(
                "INSERT OR REPLACE INTO player VALUES (?,?,?,?,?,3,'t','t')",
                (element["id"], element["web_name"], element["first_name"],
                 element["second_name"], element["team"]))
        for team in BOOTSTRAP["teams"]:
            self.conn.execute("INSERT OR REPLACE INTO team VALUES (?,?,?, '{}')",
                              (team["id"], team["short_name"], team["short_name"]))
        self.conn.execute("INSERT INTO snapshot (captured_at, gameweek, kind) "
                          "VALUES ('t', 3, 'test')")
        self.snapshot_id = self.conn.execute(
            "SELECT MAX(id) AS id FROM snapshot").fetchone()["id"]

    def _rates(self, confirmed=False, gameweek=3):
        lineups.record_lineups(self.conn, self.snapshot_id, 3, [_match(confirmed)], BOOTSTRAP)
        return lineups.lineup_start_rates(self.conn, gameweek,
                                          starter=0.90, omitted=0.15,
                                          confirmed_starter=0.97)

    def test_a_predicted_starter_is_likely_but_not_certain(self):
        self.assertAlmostEqual(self._rates()[10], 0.90)

    def test_a_confirmed_starter_ranks_higher(self):
        self.assertAlmostEqual(self._rates(confirmed=True)[10], 0.97)

    def test_a_player_ruled_out_is_zero(self):
        self.assertEqual(self._rates()[12], 0.0)

    def test_a_doubtful_player_named_in_the_xi_gets_the_starter_rate(self):
        """Regression: Isak was landing on 0.15, as if benched as well as injured.

        Fitness is FPL's flag's job - `project_player` multiplies this rate by
        `chance_of_playing_next_round` - so charging the doubt here too counts it twice.
        """
        self.assertAlmostEqual(self._rates()[11], 0.90)

    def test_a_doubtful_starter_is_confirmed_like_any_other(self):
        self.assertAlmostEqual(self._rates(confirmed=True)[11], 0.97)

    def test_a_doubtful_player_left_out_of_the_xi_is_demoted(self):
        """The other side of the same coin: QUES and unnamed is still a doubt."""
        match = MatchLineup(
            home_team="LIV", away_team="MCI", confirmed=False,
            players=[LineupPlayer("Haaland", "MCI", "FW", True)],
            injuries=[LineupPlayer("Djordje Petrovic", "MCI", "G", False, injury="QUES")],
        )
        lineups.record_lineups(self.conn, self.snapshot_id, 3, [match], BOOTSTRAP)
        rates = lineups.lineup_start_rates(self.conn, 3, starter=0.90, omitted=0.15,
                                           confirmed_starter=0.97)
        self.assertAlmostEqual(rates[13], 0.15)

    def test_a_squad_player_left_out_of_the_lineup_is_demoted(self):
        """The rotation case FPL's own flag never reports: silence is the signal."""
        rates = self._rates()
        self.assertAlmostEqual(rates[14], 0.90)     # Haaland is named
        self.assertAlmostEqual(rates[13], 0.15)     # Petrovic is not

    def test_a_suspended_player_is_zero_under_either_code(self):
        """Regression: RotoWire writes SUS as well as SUSP, and only SUSP was mapped.

        The warehouse holds a SUS row today. A banned player was scoring the 0.15 meant
        for someone his manager merely left out - and FPL's `s` flag, which catches most
        of them, is not a reason to leave the other ones wrong.
        """
        for code in ("SUS", "SUSP"):
            with self.subTest(code=code):
                match = MatchLineup(
                    home_team="LIV", away_team="MCI", confirmed=False,
                    players=[LineupPlayer("Haaland", "MCI", "FW", True)],
                    injuries=[LineupPlayer("Djordje Petrovic", "MCI", "G", False,
                                           injury=code)],
                )
                lineups.record_lineups(self.conn, self.snapshot_id, 3, [match], BOOTSTRAP)
                rates = lineups.lineup_start_rates(self.conn, 3, starter=0.90,
                                                   omitted=0.15, confirmed_starter=0.97)
                self.assertEqual(rates[13], 0.0)

    def test_the_zeroed_codes_are_the_scraper_s_own(self):
        """Derived, not restated: a code added to INJURY_STATUS as OUT is zeroed here."""
        out_codes = {c for c, s in rotowire_scraper.INJURY_STATUS.items() if s == "OUT"}
        self.assertTrue(out_codes <= lineups.UNAVAILABLE)
        self.assertIn("SUS", lineups.UNAVAILABLE)
        self.assertNotIn("QUES", lineups.UNAVAILABLE)

    def test_lineups_do_not_leak_into_later_gameweeks(self):
        """RotoWire publishes the next round only; a horizon must not reuse it."""
        self.assertEqual(self._rates(gameweek=5), {})

    def test_no_lineups_captured_yields_nothing(self):
        self.assertEqual(
            lineups.lineup_start_rates(self.conn, 3, 0.9, 0.15, 0.97), {})


if __name__ == "__main__":
    unittest.main()

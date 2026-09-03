"""Settling and calibration. Offline: actuals and projections are constructed locally."""
import tempfile
import unittest
from pathlib import Path

from fpl_agent.engine import settle, storage
from fpl_agent.engine.projection import MODEL_VERSION
from fpl_agent.engine.settle import GameweekNotFinished


def _seed_outcomes(conn, rows):
    """Seed outcome rows together with the projections they reference."""
    conn.execute("INSERT INTO snapshot (captured_at, gameweek, kind) VALUES ('t',2,'test')")
    snapshot_id = conn.execute("SELECT MAX(id) AS id FROM snapshot").fetchone()["id"]
    for i, (expected, actual, p_start, cost, element_type) in enumerate(rows, 1):
        conn.execute("INSERT OR REPLACE INTO player VALUES (?,?,'F','S',1,?,'t','t')",
                     (i, f"P{i}", element_type))
        conn.execute(
            """INSERT INTO projection (id, snapshot_id, gameweek, element_id,
               model_version, expected_points, p_start, expected_minutes, fixture_count,
               components, created_at) VALUES (?,?,2,?, ?, ?, ?, 80, 1, '{}', 't')""",
            (i, snapshot_id, i, MODEL_VERSION, expected, p_start))
    conn.executemany(
        "INSERT OR REPLACE INTO outcome VALUES (?,?,2,?,?,?,?,?,?,?,'t')",
        [(i, i, MODEL_VERSION, expected, actual, expected - actual, p_start, cost, element_type)
         for i, (expected, actual, p_start, cost, element_type) in enumerate(rows, 1)])
    conn.commit()


class SettleTests(unittest.TestCase):
    def setUp(self):
        self.conn = storage.connect(":memory:")
        self.addCleanup(self.conn.close)

    def _snapshot(self, gameweek=2, snapshot_id=None):
        self.conn.execute(
            "INSERT INTO snapshot (captured_at, gameweek, kind) VALUES ('t', ?, 'test')",
            (gameweek,))
        return self.conn.execute("SELECT MAX(id) AS id FROM snapshot").fetchone()["id"]

    def _player(self, element_id, element_type=3, now_cost=55):
        self.conn.execute(
            "INSERT OR REPLACE INTO player VALUES (?,?,?,?,?,?,'t','t')",
            (element_id, f"P{element_id}", "F", "S", 1, element_type))

    def _projection(self, snapshot_id, element_id, gameweek, expected, p_start=1.0,
                    now_cost=55, model_version=MODEL_VERSION):
        self.conn.execute(
            """INSERT OR REPLACE INTO player_snapshot (snapshot_id, element_id, now_cost,
               minutes, status, raw) VALUES (?,?,?,900,'a','{}')""",
            (snapshot_id, element_id, now_cost))
        self.conn.execute(
            """INSERT INTO projection (snapshot_id, gameweek, element_id, model_version,
               expected_points, p_start, expected_minutes, fixture_count, components,
               created_at) VALUES (?,?,?,?,?,?,80,1,'{}','t')""",
            (snapshot_id, gameweek, element_id, model_version, expected, p_start))

    def _actual(self, element_id, gameweek, points):
        self.conn.execute(
            "INSERT OR REPLACE INTO player_gameweek (element_id, round, total_points, "
            "minutes, raw) VALUES (?,?,?,90,'{}')", (element_id, gameweek, points))

    def _fixtures(self, gameweek, finished=True):
        self.conn.execute(
            "INSERT OR REPLACE INTO fixture VALUES (?,?,1,2,3,3,NULL,NULL,NULL,?,'{}')",
            (gameweek * 100, gameweek, 1 if finished else 0))
        self.conn.commit()

    def test_refuses_a_gameweek_that_has_not_finished(self):
        """Regression: grading an unplayed gameweek scored everyone against a zero that
        had not happened, reading as a huge over-projection."""
        snapshot_id = self._snapshot(gameweek=3)
        self._player(1)
        self._projection(snapshot_id, 1, 3, expected=5.0)
        self._fixtures(3, finished=False)

        with self.assertRaises(GameweekNotFinished):
            settle.settle_gameweek(self.conn, 3)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM outcome").fetchone()[0], 0)

    def test_a_gameweek_with_no_fixtures_is_not_finished(self):
        self.assertFalse(settle.gameweek_is_finished(self.conn, 7))

    def test_partially_played_gameweek_is_not_finished(self):
        self._fixtures(4, finished=True)
        self.conn.execute(
            "INSERT OR REPLACE INTO fixture VALUES (401,4,3,4,3,3,NULL,NULL,NULL,0,'{}')")
        self.conn.commit()
        self.assertFalse(settle.gameweek_is_finished(self.conn, 4))

    def test_grades_against_actuals(self):
        snapshot_id = self._snapshot(gameweek=2)
        self._fixtures(2)
        for element_id, expected, actual in [(1, 5.0, 8.0), (2, 3.0, 1.0)]:
            self._player(element_id)
            self._projection(snapshot_id, element_id, 2, expected)
            self._actual(element_id, 2, actual)
        self.conn.commit()

        self.assertEqual(settle.settle_gameweek(self.conn, 2), 2)
        rows = {r["element_id"]: r for r in
                self.conn.execute("SELECT * FROM outcome ORDER BY element_id")}
        self.assertEqual(rows[1]["actual_points"], 8.0)
        self.assertAlmostEqual(rows[1]["error"], -3.0)   # under-projected
        self.assertAlmostEqual(rows[2]["error"], +2.0)   # over-projected

    def test_a_player_who_never_featured_scores_zero(self):
        """No player_gameweek row means he did not make a squad, which is a real miss."""
        snapshot_id = self._snapshot(gameweek=2)
        self._fixtures(2)
        self._player(1)
        self._projection(snapshot_id, 1, 2, expected=6.0)
        self.conn.commit()

        settle.settle_gameweek(self.conn, 2)
        row = self.conn.execute("SELECT * FROM outcome").fetchone()
        self.assertEqual(row["actual_points"], 0.0)
        self.assertAlmostEqual(row["error"], 6.0)

    def test_only_the_projection_current_at_the_deadline_is_graded(self):
        """A horizon projection made two gameweeks earlier is not the decision-time one."""
        early = self._snapshot(gameweek=1)          # targeted gw1, also projected gw2
        self._player(1)
        self._projection(early, 1, 2, expected=9.9)
        late = self._snapshot(gameweek=2)           # the snapshot that targeted gw2
        self._projection(late, 1, 2, expected=4.0)
        self._actual(1, 2, 4.0)
        self._fixtures(2)
        self.conn.commit()

        self.assertEqual(settle.settle_gameweek(self.conn, 2), 1)
        row = self.conn.execute("SELECT * FROM outcome").fetchone()
        self.assertEqual(row["expected_points"], 4.0)

    def test_settling_twice_replaces_rather_than_duplicates(self):
        snapshot_id = self._snapshot(gameweek=2)
        self._fixtures(2)
        self._player(1)
        self._projection(snapshot_id, 1, 2, expected=5.0)
        self._actual(1, 2, 3.0)
        self.conn.commit()

        settle.settle_gameweek(self.conn, 2)
        settle.settle_gameweek(self.conn, 2)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM outcome").fetchone()[0], 1)


class CalibrationTests(unittest.TestCase):
    def setUp(self):
        self.conn = storage.connect(":memory:")
        self.addCleanup(self.conn.close)

    def _outcomes(self, rows):
        _seed_outcomes(self.conn, rows)

    def test_bias_is_predicted_minus_actual(self):
        self._outcomes([(5.0, 3.0, 1.0, 55, 3), (4.0, 2.0, 1.0, 55, 3)])
        overall = settle.calibration(self.conn, 2)["overall"][0]
        self.assertAlmostEqual(overall.bias, 2.0)     # over-projecting
        self.assertAlmostEqual(overall.mae, 2.0)

    def test_slices_split_by_position_price_and_start_probability(self):
        self._outcomes([(5.0, 1.0, 0.9, 120, 4), (2.0, 2.0, 0.1, 45, 2)])
        slices = settle.calibration(self.conn, 2)
        self.assertEqual({s.name for s in slices["by_position"]}, {"FWD", "DEF"})
        names = {s.name for s in slices["by_price"]}
        self.assertIn("elite (£10.0m+)", names)
        self.assertIn("budget (<£5.0m)", names)
        self.assertTrue(slices["by_start_probability"])

    def test_no_outcomes_yields_nothing(self):
        self.assertEqual(settle.calibration(self.conn, 2), {})

    def test_small_slices_are_not_reported_as_findings(self):
        """Two elite players with a huge miss is not evidence about elite players."""
        self._outcomes([(5.0, 30.0, 1.0, 120, 4), (6.0, 28.0, 1.0, 130, 4)]
                       + [(2.0, 2.0, 1.0, 55, 3)] * 0)
        slices = settle.calibration(self.conn, 2)
        self.assertIsNone(settle.biggest_deviation(slices))

    def test_the_largest_well_evidenced_bias_is_chosen(self):
        big = [(4.0, 1.0, 0.9, 55, 3)] * 25      # MID, +3.0 bias, n=25
        small = [(2.0, 2.0, 0.9, 55, 2)] * 25    # DEF, no bias
        self._outcomes(big + small)
        group, worst = settle.biggest_deviation(settle.calibration(self.conn, 2))
        self.assertEqual(worst.name, "MID")
        self.assertAlmostEqual(worst.bias, 3.0)


class LearningFileTests(unittest.TestCase):
    def setUp(self):
        self.conn = storage.connect(":memory:")
        self.addCleanup(self.conn.close)
        _seed_outcomes(self.conn, [(4.0, 1.0, 0.9, 55, 3)] * 25)

    def test_writes_frontmatter_and_evidence(self):
        slices = settle.calibration(self.conn, 2)
        with tempfile.TemporaryDirectory() as tmp:
            path = settle.draft_learning(slices, 2, directory=Path(tmp))
            self.assertIsNotNone(path)
            text = path.read_text()

        self.assertTrue(text.startswith("---"))
        self.assertIn("status: proposed", text)       # drafted, never auto-applied
        self.assertIn("action: none yet", text)
        self.assertIn("gameweek: 2", text)
        self.assertIn(f"model_version: {MODEL_VERSION}", text)
        self.assertIn("over-projected", text)
        self.assertIn("| group | slice | n |", text)  # the evidence travels with it

    def test_nothing_written_when_no_slice_deviates(self):
        conn = storage.connect(":memory:")
        self.addCleanup(conn.close)
        _seed_outcomes(conn, [(2.0, 2.0, 0.9, 55, 3)] * 25)
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(settle.draft_learning(
                settle.calibration(conn, 2), 2, directory=Path(tmp)))


if __name__ == "__main__":
    unittest.main()

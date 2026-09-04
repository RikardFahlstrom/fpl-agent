"""FPL scoring rules, read from the game they belong to.

Weights come from `game_config.scoring` in the warehouse, never from constants here.
FPL changes them between seasons - defensive_contribution is new this year - and a
projection made under old weights has to stay reproducible.

The one rule that is *not* in the API payload is the defensive-contribution threshold.
It was derived empirically from the 622 played appearances in `player_gameweek` - the
rows with minutes > 0, out of 1236 stored rows, the other 614 being benchings that
score nothing and so carry no signal. Reconstructing each total from its components
with the DC term withheld leaves a residual of exactly 0 or 2 points, and the split
lands at DEF >= 10 actions (highest non-scoring row: 9) and MID >= 12 (highest
non-scoring: 11). See DC_THRESHOLDS.

Both numbers are true of different things and the two must not be conflated: all 1236
stored rows reconstruct exactly once the DC term is included, which is what validates
the weights; only the 622 played rows separate the threshold.
"""

import json
import sqlite3
from typing import Any, Optional

POSITIONS = {1: "GKP", 2: "DEF", 3: "MID", 4: "FWD"}

# Defensive-contribution action thresholds. Not published in game_config; derived from
# scored gameweeks (see module docstring). FWD is inferred from MID rather than observed:
# no forward has yet reached the threshold, the highest seen being 8.
DC_THRESHOLDS = {"GKP": None, "DEF": 10, "MID": 12, "FWD": 12}

# Goals conceded and saves score per N, not per event.
GOALS_CONCEDED_PER = 2
SAVES_PER = 3


class Scoring:
    """The scoring table for one point in time."""

    def __init__(self, weights: dict[str, Any]):
        self.w = weights

    @classmethod
    def from_db(cls, conn: sqlite3.Connection) -> "Scoring":
        row = conn.execute(
            "SELECT scoring FROM game_config ORDER BY captured_at DESC LIMIT 1"
        ).fetchone()
        if not row:
            raise LookupError("no game_config captured yet; run a snapshot first")
        return cls(json.loads(row["scoring"]))

    def _by_position(self, key: str, position: str) -> float:
        value = self.w.get(key)
        if isinstance(value, dict):
            return float(value.get(position, 0))
        return float(value or 0)

    def goal(self, position: str) -> float:
        return self._by_position("goals_scored", position)

    def assist(self, position: str) -> float:
        return self._by_position("assists", position)

    def clean_sheet(self, position: str) -> float:
        return self._by_position("clean_sheets", position)

    def goal_conceded(self, position: str) -> float:
        return self._by_position("goals_conceded", position)

    def defensive_contribution(self, position: str) -> float:
        return self._by_position("defensive_contribution", position)

    @property
    def short_play(self) -> float:
        return float(self.w.get("short_play", 1))

    @property
    def long_play(self) -> float:
        return float(self.w.get("long_play", 2))

    def appearance(self, minutes: int) -> float:
        if minutes >= 60:
            return self.long_play
        return self.short_play if minutes > 0 else 0.0

    def points(self, line: dict[str, Any], position: str) -> int:
        """Exact points for a completed gameweek.

        Reconstructs FPL's own total from its components. Used to validate the scoring
        weights against reality, and as the ground truth the projection is measured on.
        """
        def n(key: str) -> int:
            return int(line.get(key) or 0)

        minutes = n("minutes")
        total = self.appearance(minutes)
        total += n("goals_scored") * self.goal(position)
        total += n("assists") * self.assist(position)
        if minutes >= 60:
            total += n("clean_sheets") * self.clean_sheet(position)
        total += n("bonus") * float(self.w.get("bonus", 1))
        total += (n("goals_conceded") // GOALS_CONCEDED_PER) * self.goal_conceded(position)
        total += (n("saves") // SAVES_PER) * float(self.w.get("saves", 0))
        total += n("yellow_cards") * float(self.w.get("yellow_cards", 0))
        total += n("red_cards") * float(self.w.get("red_cards", 0))
        total += n("own_goals") * float(self.w.get("own_goals", 0))
        total += n("penalties_missed") * float(self.w.get("penalties_missed", 0))
        total += n("penalties_saved") * float(self.w.get("penalties_saved", 0))

        threshold = DC_THRESHOLDS.get(position)
        if threshold is not None and n("defensive_contribution") >= threshold:
            total += self.defensive_contribution(position)

        return int(total)

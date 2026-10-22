"""Which term of the projection produced a calibration slice's bias.

    .venv/bin/python .claude/skills/fpl-learn/scripts/trace_slice.py learnings/0002-*.md

Takes the learning's frontmatter (gameweek, model_version, slice) and, for every graded
player in that slice, splits the projection into the components it stored and the actual
points into the same categories using the scoring table in `game_config`. The per-component
bias is what names a weight; the slice-level bias in the learning file does not.

Read-only. Actuals are decomposed the way `scoring.Scoring.points` builds them, so the
"actual" column sums to `outcome.actual_points` and the "predicted" column to
`outcome.expected_points` - the totals line is that check.
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from fpl_agent.engine import settle  # noqa: E402
from fpl_agent.engine.scoring import (  # noqa: E402
    DC_THRESHOLDS, GOALS_CONCEDED_PER, POSITIONS, SAVES_PER, Scoring)


def frontmatter(path: Path) -> dict[str, str]:
    text = path.read_text()
    match = re.match(r"---\n(.*?)\n---", text, re.S)
    if not match:
        sys.exit(f"{path}: no frontmatter")
    return dict(line.split(":", 1) for line in match.group(1).splitlines() if ":" in line)


def slice_filter(label: str):
    """The row predicate for a slice label, from settle's own band tables."""
    for low, high, name in settle.PRICE_BANDS:
        if name == label:
            return lambda r: r["now_cost"] is not None and low <= r["now_cost"] < high
    for low, high, name in settle.START_BUCKETS:
        if name == label:
            return lambda r: r["p_start"] is not None and low <= r["p_start"] < high
    for type_id, name in POSITIONS.items():
        if name == label:
            return lambda r: r["element_type"] == type_id
    sys.exit(f"unknown slice {label!r}; labels are those settle prints")


def actual_components(line: dict, position: str, scoring: Scoring) -> dict[str, float]:
    """`Scoring.points`, kept per category under the projection's component names."""
    def n(key: str) -> int:
        return int(line.get(key) or 0)

    minutes = n("minutes")
    out = {
        "appearance": scoring.appearance(minutes),
        "goals": n("goals_scored") * scoring.goal(position),
        "assists": n("assists") * scoring.assist(position),
        "clean_sheet": n("clean_sheets") * scoring.clean_sheet(position) if minutes >= 60 else 0.0,
        "bonus": n("bonus") * float(scoring.w.get("bonus", 1)),
        "goals_conceded": (n("goals_conceded") // GOALS_CONCEDED_PER) * scoring.goal_conceded(position),
        "cards": n("yellow_cards") * float(scoring.w.get("yellow_cards", 0))
        + n("red_cards") * float(scoring.w.get("red_cards", 0)),
        "defensive_contribution": 0.0,
        "other": (n("saves") // SAVES_PER) * float(scoring.w.get("saves", 0))
        + n("own_goals") * float(scoring.w.get("own_goals", 0))
        + n("penalties_missed") * float(scoring.w.get("penalties_missed", 0))
        + n("penalties_saved") * float(scoring.w.get("penalties_saved", 0)),
    }
    threshold = DC_THRESHOLDS.get(position)
    if threshold is not None and n("defensive_contribution") >= threshold:
        out["defensive_contribution"] = scoring.defensive_contribution(position)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("learning", type=Path, help="a learnings/*.md file")
    ap.add_argument("--db", default=str(ROOT / "data" / "fpl.db"))
    args = ap.parse_args()

    fm = {k.strip(): v.strip() for k, v in frontmatter(args.learning).items()}
    gameweek, version, label = int(fm["gameweek"]), fm["model_version"], fm["slice"]
    keep = slice_filter(label)

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    scoring = Scoring.from_db(conn)
    rows = [r for r in conn.execute(
        "SELECT o.*, p.components FROM outcome o JOIN projection p ON p.id = o.projection_id "
        "WHERE o.gameweek = ? AND o.model_version = ?", (gameweek, version)) if keep(r)]
    if not rows:
        sys.exit(f"no graded rows for GW{gameweek} model {version} slice {label!r}")
    actuals = {r["element_id"]: json.loads(r["raw"]) for r in conn.execute(
        "SELECT element_id, raw FROM player_gameweek WHERE round = ?", (gameweek,))}

    pred, act = defaultdict(float), defaultdict(float)
    for r in rows:
        for key, value in json.loads(r["components"]).items():
            pred[key] += value
        # No row after a finished gameweek is a zero in every category (CLAUDE.md).
        line = actuals.get(r["element_id"])
        if line:
            for key, value in actual_components(line, POSITIONS[r["element_type"]], scoring).items():
                act[key] += value

    n = len(rows)
    print(f"GW{gameweek} model {version} slice {label!r}: {n} players, "
          f"bias = predicted - actual, per player\n")
    print(f"{'component':<24}{'predicted':>10}{'actual':>10}{'bias':>8}")
    total_p = total_a = 0.0
    for key in sorted(set(pred) | set(act), key=lambda k: -abs(pred[k] - act[k])):
        p, a = pred[key] / n, act[key] / n
        total_p += p
        total_a += a
        print(f"{key:<24}{p:>10.2f}{a:>10.2f}{p - a:>+8.2f}")
    print(f"{'total':<24}{total_p:>10.2f}{total_a:>10.2f}{total_p - total_a:>+8.2f}")
    stored_p = sum(r["expected_points"] for r in rows) / n
    stored_a = sum(r["actual_points"] for r in rows) / n
    print(f"{'(outcome table)':<24}{stored_p:>10.2f}{stored_a:>10.2f}{stored_p - stored_a:>+8.2f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

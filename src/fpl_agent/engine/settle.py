"""Grade projections against what actually happened, and draft what was learned.

Two rules decide what gets graded, and both matter.

Which projection counts. A snapshot targeting gameweek N also projects N+1 and N+2 for
the planning horizon, so several projections exist for the same gameweek. The one that
counts is the one that was current at that gameweek's deadline - the projection a
decision would have been made on - which is the projection whose snapshot was targeting
that gameweek.

What counts as the actual. A player with no player_gameweek row never made a matchday
squad, so he scored zero. That is a real miss if he was projected to return, not missing
data, and dropping those rows would quietly flatter the model.

That last rule only holds once the gameweek has been played. Before kickoff every player
is missing a row, and treating those as zeroes grades the whole gameweek against nothing
- which reads as the model over-projecting enormously and would drag every weight down on
evidence that does not exist. Settling therefore refuses a gameweek whose fixtures have
not all finished.

A finished gameweek whose actuals were never fetched is the same wrong answer in a
different coat: the fixtures say played, the rows are absent anyway, and the query cannot
tell a backfill that failed from six hundred players who scored nothing. Settling refuses
that too, and refuses to settle at all behind a backfill that lost players.

Error is signed as predicted minus actual, so a positive bias means over-projecting.

    fpl-agent settle --gameweek 3
    fpl-agent settle --gameweek 3 --learn
"""

import argparse
import asyncio
import logging
import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from .. import config
from . import storage
from ..client import FPLClient
from .projection import MODEL_VERSION
from .scoring import POSITIONS
from .actuals import MAX_BACKFILL_FAILURE_RATE, backfill_actuals
from ..reference import reference

logger = logging.getLogger("fpl_settle")

LEARNINGS_DIR = Path("learnings")
# A slice needs this many players before its bias is worth writing down.
MIN_SLICE = 20
# Bias below this is noise at this sample size, not a finding.
NOTABLE_BIAS = 0.5

PRICE_BANDS = [(0, 50, "budget (<£5.0m)"), (50, 75, "mid (£5.0-7.5m)"),
               (75, 100, "premium (£7.5-10.0m)"), (100, 10**6, "elite (£10.0m+)")]
START_BUCKETS = [(0.0, 0.25, "P(start) 0-25%"), (0.25, 0.5, "P(start) 25-50%"),
                 (0.5, 0.75, "P(start) 50-75%"), (0.75, 1.01, "P(start) 75-100%")]


@dataclass
class Slice:
    name: str
    n: int
    mae: float
    bias: float          # predicted - actual; positive means over-projecting
    predicted: float
    actual: float


class GameweekNotFinished(RuntimeError):
    """Raised when asked to grade a gameweek that has not been played yet."""


class ActualsMissing(RuntimeError):
    """Raised when a finished gameweek's actuals were never fetched."""


def gameweek_is_finished(conn: sqlite3.Connection, gameweek: int) -> bool:
    """Whether every fixture in the gameweek has been played.

    A gameweek with no fixtures recorded is not finished either - absence of fixtures is
    absence of evidence, not a completed gameweek.
    """
    row = conn.execute(
        "SELECT COUNT(*) AS total, SUM(finished) AS done FROM fixture WHERE event = ?",
        (gameweek,),
    ).fetchone()
    return bool(row["total"]) and row["done"] == row["total"]


def has_actuals(conn: sqlite3.Connection, gameweek: int) -> bool:
    """Whether the round's actuals were ever fetched.

    A finished gameweek with an empty player_gameweek is not a gameweek nobody played in;
    it is a backfill that failed. The FPL API refuses requests intermittently, every
    element-summary call then warns and returns nothing, and COALESCE turns 652 absent
    rows into 652 zeroes - a confident +1.5 bias written to a learning file as fact.

    Eleven players a side per finished fixture is a floor no real round comes near:
    rounds 1 and 2 hold 610 and 626 rows against a threshold of 220. Zero rows never
    passes, whatever the fixtures say.
    """
    rows = conn.execute(
        "SELECT COUNT(*) FROM player_gameweek WHERE round = ?", (gameweek,)).fetchone()[0]
    fixtures = conn.execute(
        "SELECT COUNT(*) FROM fixture WHERE event = ? AND finished = 1",
        (gameweek,)).fetchone()[0]
    return bool(rows) and rows >= 22 * fixtures


def settle_gameweek(conn: sqlite3.Connection, gameweek: int,
                    model_version: str = MODEL_VERSION) -> int:
    """Join the decision-time projections for a gameweek against actuals.

    The snapshot that counts is the latest one targeting the gameweek that actually
    projected it. The nightly capture keeps targeting N until the deadline passes, so
    plain MAX(snapshot.id) lands on a snapshot taken hours after the decision with no
    projections on it, and settle reports "nothing to grade" every week. Where two
    snapshots targeting N both projected it - a re-run before the deadline - the later
    still wins; that is the one the decision was made on.
    """
    if not gameweek_is_finished(conn, gameweek):
        raise GameweekNotFinished(
            f"gameweek {gameweek} has not finished; grading it now would score every "
            f"player against a zero that has not happened yet")
    if not has_actuals(conn, gameweek):
        raise ActualsMissing(
            f"gameweek {gameweek} has finished but almost none of its actuals were "
            f"fetched; grading it now would score every player against a zero that only "
            f"means the backfill failed. Re-run the backfill, then settle")
    rows = conn.execute(
        """SELECT pr.id, pr.element_id, pr.expected_points, pr.p_start,
                  p.element_type, ps.now_cost,
                  COALESCE(pg.total_points, 0) AS actual
           FROM projection pr
           JOIN snapshot s ON s.id = pr.snapshot_id
           JOIN player p ON p.element_id = pr.element_id
           JOIN player_snapshot ps ON ps.snapshot_id = pr.snapshot_id
                                  AND ps.element_id = pr.element_id
           LEFT JOIN player_gameweek pg ON pg.element_id = pr.element_id
                                       AND pg.round = pr.gameweek
           WHERE pr.gameweek = ? AND pr.model_version = ?
             AND s.gameweek = pr.gameweek
             AND s.id = (SELECT MAX(s2.id) FROM snapshot s2
                           JOIN projection p2 ON p2.snapshot_id = s2.id
                                             AND p2.gameweek = s2.gameweek
                                             AND p2.model_version = ?
                         WHERE s2.gameweek = ?)""",
        (gameweek, model_version, model_version, gameweek),
    ).fetchall()

    now = datetime.now(timezone.utc).isoformat()
    conn.executemany(
        "INSERT OR REPLACE INTO outcome VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        [(r["id"], r["element_id"], gameweek, model_version, r["expected_points"],
          float(r["actual"]), r["expected_points"] - float(r["actual"]),
          r["p_start"], r["now_cost"], r["element_type"], now) for r in rows],
    )
    conn.commit()
    logger.info("settled gameweek %s: %s projections graded", gameweek, len(rows))
    return len(rows)


def _slice(name: str, rows: list[sqlite3.Row]) -> Optional[Slice]:
    if not rows:
        return None
    n = len(rows)
    errors = [r["error"] for r in rows]
    return Slice(
        name=name, n=n,
        mae=sum(abs(e) for e in errors) / n,
        bias=sum(errors) / n,
        predicted=sum(r["expected_points"] for r in rows) / n,
        actual=sum(r["actual_points"] for r in rows) / n,
    )


def calibration(conn: sqlite3.Connection, gameweek: int,
                model_version: str = MODEL_VERSION) -> dict[str, list[Slice]]:
    """Overall error plus the slices that make it actionable.

    An overall MAE says nothing you can act on. "Forwards are over-projected by 0.8"
    names a term to change.
    """
    rows = conn.execute(
        "SELECT * FROM outcome WHERE gameweek = ? AND model_version = ?",
        (gameweek, model_version),
    ).fetchall()
    if not rows:
        return {}

    by_position, by_price, by_start = [], [], []
    for position_id, position in POSITIONS.items():
        got = _slice(position, [r for r in rows if r["element_type"] == position_id])
        if got:
            by_position.append(got)
    for low, high, label in PRICE_BANDS:
        got = _slice(label, [r for r in rows
                             if r["now_cost"] is not None and low <= r["now_cost"] < high])
        if got:
            by_price.append(got)
    for low, high, label in START_BUCKETS:
        got = _slice(label, [r for r in rows
                             if r["p_start"] is not None and low <= r["p_start"] < high])
        if got:
            by_start.append(got)

    return {
        "overall": [_slice("all players", rows)],
        "by_position": by_position,
        "by_price": by_price,
        "by_start_probability": by_start,
    }


def biggest_deviation(slices: dict[str, list[Slice]]) -> Optional[tuple[str, Slice]]:
    """The slice most worth writing a learning about."""
    candidates = [
        (group, s) for group, entries in slices.items() if group != "overall"
        for s in entries if s.n >= MIN_SLICE and abs(s.bias) >= NOTABLE_BIAS
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda pair: abs(pair[1].bias))


def draft_learning(slices: dict[str, list[Slice]], gameweek: int,
                   model_version: str = MODEL_VERSION,
                   directory: Path = LEARNINGS_DIR) -> Optional[Path]:
    """Write a learning as markdown with frontmatter, for a human to accept or reject.

    Drafted, not applied: the file records a hypothesis and its evidence, and `status`
    stays `proposed` until someone changes a weight or rejects it.
    """
    finding = biggest_deviation(slices)
    if not finding:
        logger.info("no slice deviates enough to be worth recording")
        return None
    group, worst = finding
    overall = slices["overall"][0]

    directory.mkdir(parents=True, exist_ok=True)
    number = len(list(directory.glob("*.md"))) + 1
    slug = worst.name.lower().replace("(", "").replace(")", "")
    slug = "-".join(slug.replace("%", "").replace("£", "").split())[:40]
    direction = "over" if worst.bias > 0 else "under"
    path = directory / f"{number:04d}-gw{gameweek}-{direction}-projecting-{slug}.md"

    lines = [
        "---",
        f"id: {number:04d}",
        f"gameweek: {gameweek}",
        f"model_version: {model_version}",
        f"metric: bias_{group}",
        f"slice: {worst.name}",
        f"observation: {direction}-projected by {abs(worst.bias):.2f} points "
        f"across {worst.n} players",
        "status: proposed",
        "action: none yet",
        "---",
        "",
        f"# {worst.name}: {direction}-projected by {abs(worst.bias):.2f} in gameweek {gameweek}",
        "",
        f"Across {worst.n} players in this slice the model predicted "
        f"{worst.predicted:.2f} points on average and they scored {worst.actual:.2f}, "
        f"a bias of {worst.bias:+.2f} and a mean absolute error of {worst.mae:.2f}.",
        "",
        f"For comparison the whole gameweek ran at a bias of {overall.bias:+.2f} and an "
        f"MAE of {overall.mae:.2f} over {overall.n} players, so this slice is "
        f"{'worse than' if abs(worst.bias) > abs(overall.bias) else 'in line with'} "
        "the model as a whole.",
        "",
        "## Slices this gameweek",
        "",
        "| group | slice | n | predicted | actual | bias | MAE |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: |",
    ]
    for group_name, entries in slices.items():
        for s in entries:
            lines.append(f"| {group_name} | {s.name} | {s.n} | {s.predicted:.2f} | "
                         f"{s.actual:.2f} | {s.bias:+.2f} | {s.mae:.2f} |")
    lines += [
        "",
        "## Next",
        "",
        "One gameweek is one sample. Confirm the direction holds before changing a "
        "weight, and bump `model_version` when you do, so the change can be measured "
        "against this baseline rather than replacing it.",
        "",
    ]
    path.write_text("\n".join(lines))
    logger.info("drafted %s", path)
    return path


# Exit codes, because an unattended run is read by its status and not its log: 1 the
# gameweek is not gradeable, 5 the backfill lost too many players to trust it (the same
# code snapshot returns for the same condition), 6 the round's actuals were never fetched.
async def _run(args) -> int:
    conn = storage.connect(args.db)
    client = FPLClient(reference=reference)
    try:
        # Whether a gameweek is over is read from fixture.finished, which only a snapshot
        # writes - so without this, settle's verdict depends on an unrelated nightly job
        # having run. A fixture only ever gains its finished flag, so stale rows can
        # refuse a settle that should have run but never permit one that should not.
        try:
            stored = storage.upsert_fixtures(conn, await client.get_fixtures())
            conn.commit()
            logger.info("refreshed %s fixtures", stored)
        except Exception as e:
            logger.warning("could not refresh fixtures (%s); using the stored ones", e)

        if not args.no_backfill:
            element_ids = [r["element_id"] for r in
                           conn.execute("SELECT element_id FROM player ORDER BY element_id")]
            if element_ids:
                result = await backfill_actuals(conn, client, element_ids)
                if result.failure_rate > MAX_BACKFILL_FAILURE_RATE:
                    logger.error(
                        "backfill failed for %s of %s players (%.1f%%); refusing to "
                        "settle, because the players it could not fetch would be graded "
                        "as having scored zero",
                        result.failed, result.attempted, 100 * result.failure_rate)
                    return 5

        try:
            graded = settle_gameweek(conn, args.gameweek, args.model_version)
        except GameweekNotFinished as e:
            logger.error("%s", e)
            return 1
        except ActualsMissing as e:
            logger.error("%s", e)
            return 6
        if not graded:
            logger.error(
                "nothing to grade: no projection for gameweek %s was made from a snapshot "
                "targeting it. Project before the deadline, settle after.", args.gameweek)
            return 1

        slices = calibration(conn, args.gameweek, args.model_version)
        print(f"\nCalibration for gameweek {args.gameweek} (model {args.model_version})")
        print(f"{'group':22} {'slice':22} {'n':>5} {'pred':>7} {'actual':>7} "
              f"{'bias':>7} {'MAE':>6}")
        for group, entries in slices.items():
            for s in entries:
                print(f"{group:22} {s.name:22} {s.n:>5} {s.predicted:>7.2f} "
                      f"{s.actual:>7.2f} {s.bias:>+7.2f} {s.mae:>6.2f}")

        if args.learn:
            path = draft_learning(slices, args.gameweek, args.model_version, args.learnings)
            if path:
                print(f"\ndrafted {path}")
        return 0
    finally:
        await client.close()
        conn.close()


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Grade projections against actuals.")
    parser.add_argument("--db", type=Path, default=storage.DEFAULT_DB_PATH)
    parser.add_argument("--gameweek", type=int, required=True)
    parser.add_argument("--model-version", default=MODEL_VERSION)
    parser.add_argument("--learn", action="store_true", help="draft a learning file")
    parser.add_argument("--learnings", type=Path, default=LEARNINGS_DIR)
    parser.add_argument("--no-backfill", action="store_true",
                        help="skip fetching actuals; grade what is already stored")
    args = parser.parse_args(argv)

    config.load()

    logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stderr)
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())

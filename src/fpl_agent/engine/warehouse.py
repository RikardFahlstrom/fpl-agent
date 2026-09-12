"""What the warehouse holds: which capture a reader means, and what each gameweek holds.

`bootstrap-static` is current-state only, so every projection, recommendation and brief
is tied to the capture it was made from - and "the capture" is not one thing. Before
this module the question was answered by the same handful of queries pasted into seven
modules, each with its own docstring explaining which snapshot it meant and why, and
`status` had to say in a comment that it deliberately copied `lineups`' query so the two
would keep agreeing. This is that rule written once, so that agreement is by construction.

Four questions, and the reason each is its own:

- `latest` - the capture the pipeline is *in*: the one `recommend` prices against, `brief`
  describes and `status` checks. Highest id, because every capture is a new row.
- `with_lineups` - the capture whose predicted lineups a projection of gameweek N will
  read. Lineups are filed per fixture, not per capture, so the most recent capture holding
  lineups for N is often older than the latest capture: RotoWire publishes near matchday
  and a Tuesday capture legitimately holds none.
- `with_squad` - the latest capture that logged in. A market-only capture records no
  `my_state` row, and the entry id and selling prices live nowhere else.
- `projected` - the capture whose projections of gameweek N are graded: the most recent one
  *targeting* N with rows under the given model version. A projection of N made from a
  capture targeting N - 1 is a horizon row, not the decision-time record, and grading it
  would score the model on a question it was not answering.

Every reader takes the connection and returns a `Capture` value or None; none of them
writes. "None" is an answer, not an error - what it means is the caller's to say, because
"no capture yet" is a refusal in `recommend` and a failed check in `status`.

The second family is the per-gameweek ledger, `gameweeks`: one eager read of what the
warehouse holds for every round, from which "finished" and "settleable" are derived. It
exists for the same reason as the first. "Grade a gameweek only once it has finished"
had three homes - `settle`'s predicates, `status` restating them for every round, and
`schedule` asking `settle` through a connection - and a fourth, `deploy/fpl-cron.sh`'s
own SQL, is the one that offered a round still being played and stepped over an ungraded
one. The ledger is a value: `settle`, `status` and `schedule.due` all read it, and none
of them holds a rule of its own.
"""

import sqlite3
from dataclasses import dataclass, field
from typing import Optional


@dataclass(frozen=True)
class Capture:
    """One `snapshot` row: the moment the market, squad, fixtures and lineups were read."""
    id: int
    gameweek: Optional[int]
    captured_at: str
    kind: str


def _capture(conn: sqlite3.Connection, where: str, params: tuple = ()) -> Optional[Capture]:
    row = conn.execute(
        f"SELECT id, captured_at, gameweek, kind FROM snapshot WHERE {where} "
        f"ORDER BY id DESC LIMIT 1", params).fetchone()
    if row is None:
        return None
    return Capture(id=row["id"], gameweek=row["gameweek"],
                   captured_at=row["captured_at"], kind=row["kind"])


def latest(conn: sqlite3.Connection) -> Optional[Capture]:
    """The most recent capture, or None if the warehouse has never been captured."""
    return _capture(conn, "1 = 1")


def with_lineups(conn: sqlite3.Connection, gameweek: int) -> Optional[Capture]:
    """The most recent capture holding predicted lineups for `gameweek`, or None.

    Not necessarily the latest capture, and not necessarily one targeting `gameweek`
    either: a capture caught between the gameweek N deadline and that round's last
    kickoff files N's lineups while itself targeting N + 1.
    """
    return _capture(
        conn, "id = (SELECT MAX(snapshot_id) FROM predicted_lineup WHERE gameweek = ?)",
        (gameweek,))


def with_squad(conn: sqlite3.Connection) -> Optional[Capture]:
    """The most recent capture that recorded the authenticated squad, or None if none did.

    `my_state` is written only by a capture that logged in, so its presence is the test.
    """
    return _capture(conn, "id IN (SELECT snapshot_id FROM my_state)")


def projected(conn: sqlite3.Connection, gameweek: int,
              model_version: str) -> Optional[Capture]:
    """The most recent capture targeting `gameweek` with projections of it under
    `model_version`, or None. This is the record `settle` grades.
    """
    return _capture(
        conn,
        """gameweek = ? AND id IN (SELECT snapshot_id FROM projection
                                   WHERE gameweek = ? AND model_version = ?)""",
        (gameweek, gameweek, model_version))


@dataclass(frozen=True)
class Gameweek:
    """What the warehouse holds for one round, under one model version.

    `actuals` is a row count rather than a flag because `status` reports how far the
    backfill reached; `has_actuals` is the floor `settle` refuses below.
    """
    round: int
    fixtures: int      # fixtures recorded for the round
    played: int        # of which finished
    actuals: int       # player_gameweek rows for the round
    projected: bool    # a capture targeting the round projected it under the version
    graded: bool       # outcome rows exist under the version

    @property
    def finished(self) -> bool:
        """Every fixture played. No fixtures recorded is not finished either - absence of
        fixtures is absence of evidence, not a completed gameweek."""
        return bool(self.fixtures) and self.played == self.fixtures

    @property
    def has_actuals(self) -> bool:
        """Whether the round's actuals were ever fetched.

        A finished gameweek with an empty player_gameweek is not a gameweek nobody played
        in; it is a backfill that failed. The FPL API refuses requests intermittently,
        every element-summary call then warns and returns nothing, and COALESCE turns 652
        absent rows into 652 zeroes - a confident +1.5 bias written to a learning file as
        fact.

        Eleven players a side per finished fixture is a floor no real round comes near:
        rounds 1 and 2 hold 610 and 626 rows against a threshold of 220. Zero rows never
        passes, whatever the fixtures say.
        """
        return bool(self.actuals) and self.actuals >= 22 * self.played


@dataclass(frozen=True)
class GameweekLedger:
    """Every round the warehouse knows anything about, ascending, under one model version.

    A round is known if it has a fixture, an actual, a projection or an outcome; a round
    with none of those is not in the ledger, and `get` answers for it with an empty entry
    rather than None, because "nothing recorded" is a state a caller wants to reason
    about, not an error.
    """
    model_version: str
    rounds: tuple[Gameweek, ...] = field(default_factory=tuple)

    def get(self, gameweek: int) -> Gameweek:
        for entry in self.rounds:
            if entry.round == gameweek:
                return entry
        return Gameweek(gameweek, 0, 0, 0, False, False)

    def finished(self) -> list[int]:
        """Every gameweek all of whose fixtures have been played, ascending."""
        return [g.round for g in self.rounds if g.finished]

    def settleable(self) -> list[int]:
        """Every gameweek `settle` would actually grade, oldest first.

        Three conditions, each of which is a refusal in `settle` if broken:

        - every fixture in the round played
        - no `outcome` rows under this model version - grading twice is not idempotent
          bookkeeping, it is a second opinion recorded as a first
        - a projection made from a capture *targeting* that gameweek, under this model
          version. Rounds played before the warehouse existed have none and never can:
          the prices, lineups and ownership are gone and `bootstrap-static` has no
          history. Version matters too - after a `MODEL_VERSION` bump an old gameweek
          cannot be re-projected (see `projection.SettledProjection`), so it is not
          settleable under the new version either, and offering it would be advice that
          cannot be taken.
        """
        return [g.round for g in self.rounds if g.finished and g.projected and not g.graded]

    def backfilled_through(self) -> Optional[int]:
        """The highest round holding any actuals, or None if the backfill never ran."""
        rounds = [g.round for g in self.rounds if g.actuals]
        return max(rounds) if rounds else None


def gameweeks(conn: sqlite3.Connection, model_version: str) -> GameweekLedger:
    """One eager read of what the warehouse holds per round. Never writes.

    `model_version` is a parameter rather than the current constant so that a bump can
    compare two ledgers: what the old version graded and what the new one can.
    """
    rows = conn.execute(
        """WITH rounds AS (
               SELECT event AS round FROM fixture WHERE event IS NOT NULL
               UNION SELECT round FROM player_gameweek
               UNION SELECT gameweek FROM outcome WHERE model_version = :version
               UNION SELECT p.gameweek FROM projection p
                 JOIN snapshot s ON s.id = p.snapshot_id AND s.gameweek = p.gameweek
                WHERE p.model_version = :version)
           SELECT r.round,
                  (SELECT COUNT(*) FROM fixture WHERE event = r.round) AS fixtures,
                  (SELECT COUNT(*) FROM fixture WHERE event = r.round AND finished = 1)
                      AS played,
                  (SELECT COUNT(*) FROM player_gameweek WHERE round = r.round) AS actuals,
                  EXISTS (SELECT 1 FROM projection p
                            JOIN snapshot s ON s.id = p.snapshot_id
                                           AND s.gameweek = p.gameweek
                           WHERE p.gameweek = r.round AND p.model_version = :version)
                      AS projected,
                  EXISTS (SELECT 1 FROM outcome
                           WHERE gameweek = r.round AND model_version = :version)
                      AS graded
             FROM rounds r ORDER BY r.round""",
        {"version": model_version}).fetchall()
    return GameweekLedger(
        model_version=model_version,
        rounds=tuple(Gameweek(round=r["round"], fixtures=r["fixtures"], played=r["played"],
                              actuals=r["actuals"], projected=bool(r["projected"]),
                              graded=bool(r["graded"])) for r in rows))

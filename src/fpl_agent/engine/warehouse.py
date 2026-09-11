"""What the warehouse holds: which capture a reader means.

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
"""

import sqlite3
from dataclasses import dataclass
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

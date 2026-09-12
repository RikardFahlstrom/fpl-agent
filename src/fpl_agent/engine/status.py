"""One read-only answer to "is the warehouse trustworthy?".

Every serious bug this project has had was a state some tool reported as fine: a
preflight that promised a squad and logged nobody in, a settle that graded 651 players
against actuals that did not exist, a candidate list that threw away everyone nobody
owned. Each of those now has a guard at its own point in the pipeline. This is the
general one - the question a person asks at 03:00 reading a cron mail, and the question
an unattended run should end on:

    is the latest snapshot complete, are projections attached to it, are actuals current,
    which gameweek's lineups will `project` find, will tonight's job need a browser?

It fixes nothing. It reports, and it exits non-zero when the warehouse disagrees with
itself, because a check that cannot fail is a check nobody reads.

Three things fail: a squad that was not captured, no projection for the snapshot's
target gameweek under the current model, and a backfill behind a gameweek that has
finished. Everything else reports at `warn`, because a check that fires on a legitimate
state is a check the reader learns to skip - and the states this warns about (no lineups
published yet, no rivals captured, a finished gameweek not yet settled) are all things a
healthy warehouse passes through.

Read-only, twice over. The connection is opened `mode=ro` so `status` can never be the
thing that corrupts what it is checking, and the token cache is *read* - never exchanged.
The account service rotates the refresh token on every exchange, so a status command that
refreshed would kill the credential of whatever ran beside it.

Exit codes (the table in `docs/SCHEDULING.md` is the authority; 1-6 belong to `settle`
and `snapshot`):

    0   everything checked agrees
    7   at least one inconsistency, named on its own line
    2   the warehouse could not be opened at all

    fpl-agent status
    fpl-agent status --db data/fpl.db
"""

import argparse
import json
import logging
import sqlite3
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .. import config
from . import schedule, storage, warehouse
from .projection import MODEL_VERSION
from .snapshot import SQUAD_SIZE

logger = logging.getLogger("fpl_status")

# Exit codes. `status` owns 7; 1-6 are taken by settle and snapshot, and a shared code
# would make a cron mail ambiguous about which command complained.
EXIT_OK = 0
EXIT_UNREADABLE = 2
EXIT_INCONSISTENT = 7

OK, WARN, FAIL = "ok", "warn", "FAIL"

# Tables `status` reads. A file missing one of them is not a warehouse.
REQUIRED_TABLES = ("snapshot", "my_squad", "projection", "predicted_lineup",
                   "player_gameweek", "fixture", "outcome", "rival_squad", "decision")


@dataclass
class Check:
    """One line of the report.

    `level` is the whole contract: only FAIL moves the exit code. WARN exists for the
    states that are legitimately not-yet - no lineups published, no rivals captured -
    which a person still wants to see but which must not fail a scheduled run.
    """
    label: str
    level: str
    detail: str

    @property
    def failed(self) -> bool:
        return self.level == FAIL


# Lives in `storage` now, next to the read-write `connect` it is the counterpart to, so
# that `settle` can open a warehouse read-only without importing this module - which
# imports `settle`. Re-exported because callers and tests reach for it by this name.
connect_readonly = storage.connect_readonly


def missing_tables(conn: sqlite3.Connection) -> list[str]:
    present = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'")}
    return [t for t in REQUIRED_TABLES if t not in present]


# --------------------------------------------------------------------------
# Reading the warehouse
# --------------------------------------------------------------------------

# Over a day and a half: the daily capture has been missed at least once. Shared by the
# snapshot and the league table, which the same capture refreshes.
STALE_AFTER_HOURS = 36


def _age_hours(captured_at: Optional[str]) -> Optional[float]:
    """Hours since an ISO timestamp, or None if it cannot be read."""
    if not captured_at:
        return None
    try:
        when = datetime.fromisoformat(captured_at)
    except ValueError:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - when).total_seconds() / 3600


def _gameweeks(gameweeks: list[int]) -> str:
    return ", ".join(str(gw) for gw in gameweeks)


# --------------------------------------------------------------------------
# The checks. Each names the invariant it defends.
# --------------------------------------------------------------------------

def check_snapshot(snapshot: Optional[warehouse.Capture]) -> Check:
    """CLAUDE.md: "Snapshot before deciding."

    `bootstrap-static` is current-state only, so a gameweek without a snapshot can never
    be learned from. No snapshot at all is the one state from which nothing else can even
    be judged. How stale is *too* stale depends on the deadline, which this command does
    not know, so age is reported and only flagged, never failed.
    """
    if snapshot is None:
        return Check("snapshot", FAIL,
                     "none - the warehouse has never been captured, so nothing below can "
                     "be judged. Run `make snapshot`.")
    hours = _age_hours(snapshot.captured_at)
    age = "age unknown" if hours is None else f"{hours:.1f}h old"
    detail = (f"{snapshot.id} captured {snapshot.captured_at} ({age}), "
              f"targeting gameweek {snapshot.gameweek}")
    if snapshot.gameweek is None:
        return Check("snapshot", FAIL,
                     detail + " - no target gameweek was recorded, so no projection can "
                              "be tied to the decision it was made for")
    stale = hours is not None and hours > STALE_AFTER_HOURS
    return Check("snapshot", WARN if stale else OK,
                 detail + (" - over a day and a half old; prices, ownership and news "
                           "have moved since" if stale else ""))


def check_squad(conn: sqlite3.Connection, snapshot: warehouse.Capture) -> Check:
    """The bug that used to exit 0: the preflight promised a squad and captured none.

    Selling prices, bank and free transfers for a past moment exist in no public
    endpoint, so a snapshot without them cannot be repaired later - and `recommend` fails
    outright with "no squad captured". 15 is the only right answer.
    """
    rows = conn.execute("SELECT COUNT(*) FROM my_squad WHERE snapshot_id = ?",
                        (snapshot.id,)).fetchone()[0]
    if rows == SQUAD_SIZE:
        return Check("squad", OK,
                     f"{rows} of {SQUAD_SIZE} rows for snapshot {snapshot.id}")
    if rows == 0:
        return Check("squad", FAIL,
                     f"absent for snapshot {snapshot.id} - no my_squad rows at all. "
                     f"Either the login failed or the run was market-only; `recommend` "
                     f"will refuse, and the selling prices for this moment are gone.")
    return Check("squad", FAIL,
                 f"{rows} of {SQUAD_SIZE} rows for snapshot {snapshot.id} - a partial "
                 f"squad, so budget and bench order are both wrong")


def check_projections(conn: sqlite3.Connection, snapshot: warehouse.Capture) -> Check:
    """CLAUDE.md: "Bump MODEL_VERSION on any change that moves projections."

    Both versions then sit in the warehouse, which means the presence of *a* projection
    proves nothing. A snapshot carrying only 0.3.0 rows after the model moved to 0.5.0 is
    a snapshot `project` never re-ran over, and `recommend` reads the current version -
    so the check is for the current version, on the snapshot's own target gameweek.
    """
    gameweek = snapshot.gameweek
    rows = conn.execute(
        """SELECT model_version, COUNT(*) AS n FROM projection
           WHERE snapshot_id = ? AND gameweek = ? GROUP BY model_version
           ORDER BY model_version""", (snapshot.id, gameweek)).fetchall()
    by_version = {r["model_version"]: r["n"] for r in rows}

    if not by_version:
        return Check("projections", FAIL,
                     f"none for gameweek {gameweek} on snapshot {snapshot.id} - the "
                     f"snapshot was captured but never projected. Run `make project`.")
    if MODEL_VERSION not in by_version:
        stored = ", ".join(f"{v} ({n})" for v, n in sorted(by_version.items()))
        return Check("projections", FAIL,
                     f"none under model {MODEL_VERSION} for gameweek {gameweek} on "
                     f"snapshot {snapshot.id}; only {stored}. The model moved after "
                     f"this snapshot was projected - re-run `make project`.")
    horizon = conn.execute(
        """SELECT COUNT(DISTINCT gameweek) FROM projection
           WHERE snapshot_id = ? AND model_version = ?""",
        (snapshot.id, MODEL_VERSION)).fetchone()[0]
    others = ", ".join(f"{v} ({n})" for v, n in sorted(by_version.items())
                       if v != MODEL_VERSION)
    detail = (f"{by_version[MODEL_VERSION]} for gameweek {gameweek} under model "
              f"{MODEL_VERSION}, {horizon}-gameweek horizon, snapshot {snapshot.id}")
    return Check("projections", OK,
                 detail + (f"; also stored: {others}" if others else ""))


def check_lineups(conn: sqlite3.Connection, snapshot: warehouse.Capture) -> Check:
    """Which gameweek's lineups `project` will actually find.

    Filing is decided per fixture, not per snapshot: `lineups.record_lineups` asks the
    fixture list which unfinished event holds each pair of clubs, so a snapshot caught
    between the gameweek N deadline and that round's last kickoff files gameweek N
    lineups while itself targeting N + 1, and a page straddling a changeover files two
    rounds at once. All of that is correct, which is why a gameweek other than the
    snapshot's target is reported here and never failed - a check that called the normal
    case broken would be trained out of the reader within a week.

    What is worth knowing is the question `lineup_start_rates` asks: for the gameweek
    being projected, which snapshot's lineups win. With none, the projection falls back
    to historical start rates, which is a real degradation and not an error - RotoWire
    publishes near matchday, so a Tuesday snapshot legitimately has none.
    """
    target = snapshot.gameweek
    rows = conn.execute(
        """SELECT gameweek, COUNT(*) AS n FROM predicted_lineup
           WHERE snapshot_id = ? GROUP BY gameweek ORDER BY gameweek""",
        (snapshot.id,)).fetchall()
    filed = (", ".join(f"{r['n']} for gameweek {r['gameweek']}" for r in rows)
             if rows else "none")
    # The same capture lineup_start_rates reads, by construction.
    holder = warehouse.with_lineups(conn, target)
    source = holder.id if holder else None

    if source is None:
        return Check("lineups", WARN,
                     f"snapshot {snapshot.id} filed {filed}, and no snapshot holds "
                     f"any for gameweek {target} - projections for it fall back to "
                     f"historical start rates. Normal until RotoWire publishes.")
    used = conn.execute(
        "SELECT COUNT(*) FROM predicted_lineup WHERE snapshot_id = ? AND gameweek = ?",
        (source, target)).fetchone()[0]
    if source == snapshot.id:
        if len(rows) == 1:
            return Check("lineups", OK,
                         f"{filed}, snapshot {snapshot.id} - what `project` will use")
        # Two rounds under one snapshot is the changeover case, so name which half wins.
        return Check("lineups", OK,
                     f"{filed}, snapshot {snapshot.id} - the {used} for gameweek "
                     f"{target} are what `project` will use")
    return Check("lineups", WARN,
                 f"snapshot {snapshot.id} filed {filed}; gameweek {target}'s lineups "
                 f"come from the older snapshot {source} ({used} rows), which is what "
                 f"`project` will use")


def check_actuals(ledger: warehouse.GameweekLedger) -> Check:
    """CLAUDE.md: "Absence of a row is data" - but only once the gameweek has finished.

    Before kickoff every player is missing a `player_gameweek` row and that is correct.
    After the last fixture ends the same absence is a backfill that never ran, and
    settling over it grades real scores as zeroes. So the comparison is against the
    highest *finished* gameweek, never against the calendar.
    """
    finished = ledger.finished()
    backfilled = ledger.backfilled_through()
    if not finished:
        through = f"round {backfilled}" if backfilled else "no round"
        return Check("actuals", OK,
                     f"backfilled through {through}; no gameweek has finished yet, so "
                     f"there is nothing to be behind")
    latest = finished[-1]
    if backfilled is None:
        return Check("actuals", FAIL,
                     f"no player_gameweek rows at all, but gameweek {latest} has "
                     f"finished - the backfill has never run. Run `make backfill`.")
    if backfilled < latest:
        return Check("actuals", FAIL,
                     f"backfilled through round {backfilled}, but gameweek {latest} has "
                     f"finished - the actuals are {latest - backfilled} gameweek(s) "
                     f"behind, and settling over that gap grades real scores as zeroes")
    return Check("actuals", OK,
                 f"backfilled through round {backfilled}; latest finished gameweek is "
                 f"{latest}")


def check_grading(ledger: warehouse.GameweekLedger) -> Check:
    """CLAUDE.md: "Never grade a gameweek that has not finished."

    Which cuts both ways here. An empty `outcome` table for an unfinished gameweek is the
    correct state and must never be reported as a fault, so only finished gameweeks are
    ever counted as ungraded - and even then it is a nudge to run `make settle`, not a
    warehouse that disagrees with itself.

    Which of those settle would actually accept is the ledger's rule, not this module's.
    Asking it here in slightly different SQL is how the scheduler and the engine came to
    disagree once already; there is one definition and this is a reader of it.
    """
    finished = ledger.finished()
    if not finished:
        return Check("grading", OK,
                     "no gameweek has finished yet - an empty outcome table is the right "
                     "state, not a gap")
    ungraded = [gw for gw in finished if not ledger.get(gw).graded]
    if not ungraded:
        return Check("grading", OK,
                     f"gameweek(s) {_gameweeks(finished)} finished and graded under "
                     f"model {ledger.model_version}")

    settleable = ledger.settleable()
    unreachable = [gw for gw in ungraded if gw not in settleable]

    if not settleable:
        return Check("grading", OK,
                     f"gameweek(s) {_gameweeks(unreachable)} finished before this "
                     f"warehouse had a snapshot targeting them, so they can never be "
                     f"graded - nothing to do, and nothing lost that is recoverable")

    detail = (f"gameweek(s) {_gameweeks(settleable)} have finished but carry no outcome "
              f"rows under model {ledger.model_version} - run `make settle "
              f"GW={settleable[-1]}`")
    if unreachable:
        detail += (f" (gameweek(s) {_gameweeks(unreachable)} predate the warehouse and "
                   f"can never be graded)")
    return Check("grading", WARN, detail)


def check_rivals(conn: sqlite3.Connection, ledger: warehouse.GameweekLedger) -> Check:
    """CLAUDE.md: "Absence of a row is data" - a player in no rival squad is owned by 0%.

    That only holds if rival squads were captured at all. With none, every candidate has
    unknown ownership instead of zero, which is the bug that discarded 165 of 200
    candidates at exactly the point the edge lives.

    Rival picks and the league table are not gameweek facts, so those two reads stay
    here; the ledger supplies only which gameweek last finished.
    """
    finished = ledger.finished()
    row = conn.execute(
        """SELECT gameweek, COUNT(DISTINCT entry_id) AS managers, COUNT(*) AS picks
           FROM rival_squad GROUP BY gameweek ORDER BY gameweek DESC LIMIT 1""").fetchone()
    if row is None:
        return Check("rivals", WARN,
                     "no rival squads captured - effective ownership is unknown rather "
                     "than zero for every candidate. Run `make rivals`.")
    detail = (f"{row['managers']} managers, {row['picks']} picks for gameweek "
              f"{row['gameweek']}")
    # The table is the cheap half and has its own age: it is refreshed by every capture,
    # not only by the picks capture behind the deadline window, so a stale one is one
    # request from current. A warning, never a fault - see issue #49 for the five days
    # it sat wrong with nothing saying so.
    table = conn.execute("SELECT MIN(captured_at) AS captured_at FROM league").fetchone()
    hours = _age_hours(table["captured_at"]) if table else None
    if hours is not None:
        detail += f"; table {hours:.1f}h old"
    if finished and row["gameweek"] < finished[-1]:
        return Check("rivals", WARN,
                     detail + f" - behind the last finished gameweek ({finished[-1]}), "
                              f"so effective ownership is stale")
    if hours is not None and hours > STALE_AFTER_HOURS:
        return Check("rivals", WARN,
                     detail + " - the league standings are over a day and a half old; "
                              "`make now` refreshes them (or `fpl-agent rivals "
                              "--standings-only`)")
    return Check("rivals", OK, detail)


def check_decisions(conn: sqlite3.Connection) -> Check:
    """Informational, never a fault: an empty `decision` table is exactly what a
    warehouse that has proposed nothing yet looks like."""
    row = conn.execute(
        """SELECT COUNT(*) AS n, MAX(created_at) AS last,
                  SUM(status = 'made') AS made FROM decision""").fetchone()
    if not row["n"]:
        return Check("decisions", OK,
                     "none recorded - `recommend --record` is what writes them")
    return Check("decisions", OK,
                 f"{row['n']} recorded, {row['made'] or 0} made, latest {row['last']}")


def check_token(now: Optional[float] = None) -> Check:
    """Will tonight's job need a browser?

    Answered from the cache alone and never by exchanging it: the account service rotates
    the refresh token on every exchange, so a status command that refreshed would leave a
    concurrent job holding a dead credential. `cache_path` and `token_is_fresh` are the
    ones that own those two facts, so they are called rather than reimplemented.

    An expired access token is *not* a browser login - that is the entire point of the
    refresh grant - so the three answers are fresh, refreshable, and browser needed. No
    part of any token is ever printed; the cache is a bearer credential.
    """
    from ..api.headless_auth import cache_path, token_is_fresh

    path = cache_path()
    browser = "the next authenticated run launches a browser"
    try:
        with path.open() as handle:
            data = json.load(handle)
    except FileNotFoundError:
        return Check("token", WARN, f"no cache at {path} - {browser}")
    except (OSError, json.JSONDecodeError) as e:
        return Check("token", WARN, f"cache at {path} is unreadable ({e}) - {browser}")
    if not isinstance(data, dict) or not data.get("api_token"):
        return Check("token", WARN, f"cache at {path} holds no access token - {browser}")

    expires_at = data.get("expires_at")
    when = "expiry unknown"
    if isinstance(expires_at, (int, float)):
        remaining = expires_at - (time.time() if now is None else now)
        stamp = datetime.fromtimestamp(expires_at, timezone.utc).isoformat(timespec="seconds")
        when = f"expires {stamp} ({remaining / 3600:+.1f}h)"
    if token_is_fresh(data):
        return Check("token", OK, f"fresh, {when} - no browser needed")
    if data.get("refresh_token"):
        return Check("token", OK,
                     f"access token expired ({when}) but refreshable - no browser needed")
    return Check("token", WARN,
                 f"expired ({when}) and there is no refresh token to renew it - {browser}")


# --------------------------------------------------------------------------
# Report
# --------------------------------------------------------------------------

def gather(conn: sqlite3.Connection, *, include_token: bool = True) -> list[Check]:
    """Every check, in the order a person reads them: the capture, then what hangs off it.

    Returns the checks rather than printing them, so the notifier of review item 13 can
    build a message from exactly the facts the cron mail carries.
    """
    absent = missing_tables(conn)
    if absent:
        return [Check("warehouse", FAIL,
                      f"missing table(s): {', '.join(absent)} - this file is not an "
                      f"fpl-agent warehouse, or its schema predates them")]

    snapshot = warehouse.latest(conn)
    # One read of what every round holds; the checks below that could otherwise mistake
    # "not yet" for "missing" are gated on its `finished` list - CLAUDE.md's "absence of
    # a row is data" only starts being true once the fixtures are played.
    ledger = warehouse.gameweeks(conn, MODEL_VERSION)
    checks = [check_snapshot(snapshot)]
    # Squad, projections and lineups all hang off one snapshot; with no usable snapshot
    # there is nothing for them to be measured against, and check_snapshot has failed.
    if snapshot is not None and snapshot.gameweek is not None:
        checks += [check_squad(conn, snapshot),
                   check_projections(conn, snapshot),
                   check_lineups(conn, snapshot)]
    checks += [check_actuals(ledger),
               check_grading(ledger),
               check_rivals(conn, ledger),
               check_decisions(conn)]
    if include_token:
        checks.append(check_token())
    return checks


# Both live in `storage`, next to the fixtures they read, because `schedule` needs them
# too and neither module should have to import the other. Re-exported because callers and
# tests reach for this one by this name - `deploy/fpl-cron.sh` through
# `status --hours-to-deadline`.
hours_to_deadline = storage.hours_to_deadline




def next_action(conn: sqlite3.Connection,
                checks: Optional[list[Check]] = None) -> str:
    """The one line that says what to run, so the reader need not know the pipeline.

    `status` already names a remedy per failing line. This answers the other question -
    the one asked when nothing is wrong - because knowing the warehouse is healthy does
    not tell you whether a gameweek is sitting there ungraded.

    A failure outranks anything due. Written without that, this printed "nothing due"
    three lines under "1 inconsistency(ies): projections. Exiting 7." - a summary line
    contradicting the report it summarises, which is worse than no summary at all.
    """
    failed = [c for c in (checks or []) if c.failed]
    if failed:
        which = ", ".join(c.label for c in failed)
        return (f"next: {which} above must be fixed first - "
                f"that line names the command")

    # What is due is the schedule's question, and this is a reader of the answer. It used
    # to ask its own: its own settle query and its own deadline window, which made this
    # the fourth statement of the pipeline. `make now` runs this same `auto` plan - bar
    # the notification step, which depends on configuration this command does not read
    # and which the summary never mentions - so the line cannot promise work the command
    # will not do.
    try:
        plan = schedule.due("auto", now=datetime.now(timezone.utc),
                            warehouse=schedule.read(conn),
                            settings=schedule.Settings())
    except sqlite3.Error:
        return "next: could not tell - the warehouse would not answer"
    return f"next: {schedule.summarise(plan)}"


def render(checks: list[Check], db: Path | str, next_line: Optional[str] = None) -> str:
    """One fact per line, aligned, each line carrying its own verdict.

    The reader is a person at 03:00 with a cron mail and no other context, so a line says
    whether the number is right: `squad  15 rows` tells them nothing they can act on.
    """
    width = max((len(c.label) for c in checks), default=0)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    lines = [f"fpl-agent status  {db}  {now}", ""]
    lines += [f"{c.level:<4}  {c.label:<{width}}  {c.detail}" for c in checks]
    failed = [c for c in checks if c.failed]
    lines.append("")
    if failed:
        lines.append(f"{len(failed)} inconsistency(ies): "
                     f"{', '.join(c.label for c in failed)}. Exiting {EXIT_INCONSISTENT}.")
    else:
        warned = [c for c in checks if c.level == WARN]
        lines.append("the warehouse agrees with itself" +
                     (f"; {len(warned)} thing(s) worth a look above" if warned else ""))
    if next_line:
        lines.append(next_line)
    return "\n".join(lines)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Report whether the warehouse is trustworthy. Read-only.")
    parser.add_argument("--db", type=Path, default=storage.DEFAULT_DB_PATH)
    parser.add_argument("--no-token", action="store_true",
                        help="skip the token cache check (the cache is only ever read, "
                             "never exchanged)")
    parser.add_argument("--hours-to-deadline", action="store_true",
                        help="print the hours until the next deadline and exit; this is "
                             "what deploy/fpl-cron.sh consumes so the schedule and the "
                             "engine cannot disagree about when to project")
    args = parser.parse_args(argv)

    config.load()
    logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stderr)

    try:
        conn = connect_readonly(args.db)
    except FileNotFoundError:
        print(f"no warehouse at {args.db} - nothing has ever been captured. "
              f"Run `make snapshot`.", file=sys.stderr)
        return EXIT_UNREADABLE
    except sqlite3.Error as e:
        print(f"could not open {args.db} read-only: {e}", file=sys.stderr)
        return EXIT_UNREADABLE
    if args.hours_to_deadline:
        # Quiet and machine-readable, like `settle --list`: the scheduler substitutes
        # this into a shell variable, so nothing but the number reaches stdout, and an
        # empty answer (no unplayed fixture) prints nothing rather than a word.
        try:
            hours = hours_to_deadline(conn)
        finally:
            conn.close()
        if hours is not None:
            print(hours)
        return 0

    try:
        checks = gather(conn, include_token=not args.no_token)
        upcoming = next_action(conn, checks)
    except sqlite3.DatabaseError as e:
        # `connect_readonly` opens a file without reading a page of it, so a path that is
        # not a database at all gets this far and then raises on the first query. It used
        # to arrive as a traceback and exit 1; 2 is the code that says "the thing I was
        # asked about could not be read", and the schedule runs this command from cron
        # now, where a traceback is what the owner is mailed.
        print(f"could not read {args.db}: {e}", file=sys.stderr)
        return EXIT_UNREADABLE
    finally:
        conn.close()

    print(render(checks, args.db, upcoming))
    return EXIT_INCONSISTENT if any(c.failed for c in checks) else EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())

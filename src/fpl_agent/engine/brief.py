"""The gameweek brief, and the four facts that are worth a push notification.

`PLAN.md` §5 promised `logs/gwNN.md`, and this is it: everything the warehouse knows
that a person needs before a deadline, rendered as markdown they read on a phone at
07:00. `deadline` and `settle` are both deterministic and can run unattended. The human
is needed for exactly two things - choosing to act on a recommendation, and accepting or
rejecting a drafted learning - so the brief leads with what needs them and puts the
tables underneath.

Read-only, twice over, for the same reason `status` is: the connection is opened
`mode=ro` and the only thing this command writes is one markdown file. A brief that
could change the warehouse would be a brief you could not trust to describe it.

Two separate outputs, and the difference matters:

  `render_brief`      everything, written to `logs/gwNN.md` and committed. A price
                      forecast, a rival's ownership, a middling swap - all of it belongs
                      in the record, because the record is what the next decision is
                      read against.

  `evaluate_triggers` the small set of facts worth interrupting someone for. The review
                      named the risk plainly: notification spam erodes trust fast. So a
                      trigger has to earn its place, and every one of them ends in the
                      single action wanted from the human.

Four triggers, chosen by the owner:

  status_failed             the scheduled run is broken and nothing below can be trusted
  squad_player_unavailable  a player you own cannot play
  deadline_with_move        the deadline is close, a free transfer is unused, and there
                            is somewhere to spend it
  move_worth_making         a move clears the bar on its own, deadline or no deadline

Deliberately *not* a trigger, at the owner's choice: a held player very likely to fall in
price. It is the most frequent signal in the whole warehouse and the one most likely to
become noise, so it stays in the written brief where it can be read rather than pushed.

    fpl-agent brief                      # write logs/gwNN.md for the latest snapshot
    fpl-agent brief --gameweek 3
    fpl-agent brief --dry-run            # print it, and the triggers, and write nothing
"""

import argparse
import logging
import os
import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from .. import config
from . import lineups, pricing, recommend, settle, status, storage
from .projection import HORIZON_GAMEWEEKS, MODEL_VERSION, HorizonMissing

logger = logging.getLogger("fpl_brief")

BRIEF_DIR = Path("logs")

# FPL publishes no deadline in anything this warehouse stores - there is no `event`
# table, only fixtures - so the deadline is derived from the rule FPL states on its own
# help pages: the deadline is 90 minutes before the first kickoff of the gameweek.
# Derived, therefore approximate, therefore never used to *permit* an action: it is used
# to say how much time is left and to refuse to recommend a move that can no longer be
# made. If it is wrong it is wrong in the direction of saying less, not more.
DEADLINE_BEFORE_FIRST_KICKOFF = timedelta(minutes=90)

# "The deadline is near" for trigger 3. An hourly job that only fires inside the last
# hour would miss a deadline the moment one run is skipped; a day gives the owner an
# evening and a morning to act in.
DEADLINE_SOON = timedelta(hours=24)

# The bar a move has to clear before `move_worth_making` will interrupt anybody.
#
# The recommender always emits a ranked list. Topping a list is not the same as being
# worth doing, and the difference is the whole content of the word "worth": with a
# wildcard active every one of 54 candidates is "positive", and the top of that list is
# still a rounding error against the model's own error bars.
#
# 2.0 net expected points over the horizon, anchored twice:
#
#   * FPL charges 4 points for a transfer it does not consider free. That is the game's
#     own price for a move, and half of it is the least a gain can be and still survive
#     being wrong about which of two similar players is better.
#   * The projection's per-gameweek MAE has been running above 1 point per player, and a
#     swap's gain is a *difference* of two projections, so its error is larger than
#     either. 2.0 over three gameweeks is roughly 0.67 a week - comfortably inside one
#     MAE. This is a floor for "not obviously noise", not a claim of significance.
#
# It is a stated assumption, to be re-fitted from `settle`'s calibration slices once
# enough gameweeks have been graded to say what a 2-point edge is actually worth.
# Override with FPL_BRIEF_MIN_NET_XP (the ini maps it under [brief] min_net_xp).
WORTH_MAKING_NET_XP = 2.0
MIN_NET_XP_ENV = "FPL_BRIEF_MIN_NET_XP"

# A notification title has to fit on a lock screen.
HEADLINE_MAX = 120

# The four the owner chose, in the order they should be read. Named here so the brief can
# report on a trigger that did *not* fire: the set is a fixed contract, not whatever
# happened to be appended this run.
TRIGGER_NAMES = ("status_failed", "squad_player_unavailable", "deadline_with_move",
                 "move_worth_making")

# How many ranked transfers the written brief carries. The full list runs to dozens under
# a wildcard; the tail of it is not a decision anyone makes on a phone.
BRIEF_RECOMMENDATIONS = 8

# FPL status codes that mean the player cannot play at all, as opposed to `d`, which
# always carries a percentage `projection.availability` has already priced in.
CANNOT_PLAY = {"i": "injured", "s": "suspended"}


@dataclass(frozen=True)
class Trigger:
    """One fact worth interrupting a person for.

    `action` is the field the review's stated risk turns on. Notification spam erodes
    trust fast, and the stated cure is that every message ends with the one action wanted
    from the human - so an empty action is a construction error, not a formatting
    nicety, and it is refused here rather than caught by a reader who has already stopped
    reading.

    `fingerprint` is the field that lets a notifier exist at all. The `deadline` job runs
    hourly, so every fact below is re-evaluated dozens of times before it changes, and
    the owner must be told once rather than dozens of times. So a fingerprint is built
    only from the *identity* of the underlying fact:

        gameweek, player id, the (out -> in) pair of a move, the labels of the failing
        checks

    and never from anything that drifts between runs - no timestamps, no snapshot ids, no
    expected-points float that moves by 0.01 when a price ticks. Those would make every
    hourly run look like news. Conversely the fingerprint *must* move when the fact
    meaningfully changes: a different top move, a different player unavailable, a
    different check failing, are all things the owner has not been told yet.

    It is a readable slug rather than a hash, because the notifier stores these and a
    person debugging "why was I told twice" needs to be able to read what it stored.
    """

    name: str
    headline: str
    detail: str
    action: str
    fingerprint: str

    def __post_init__(self) -> None:
        if not self.action or not self.action.strip():
            raise ValueError(
                f"trigger {self.name!r} has no action; every message must end with the "
                f"one thing the human is being asked to do")
        if not self.fingerprint or not self.fingerprint.strip():
            raise ValueError(f"trigger {self.name!r} has no fingerprint; the notifier "
                             f"cannot tell a repeat from news without one")
        if "\n" in self.headline or len(self.headline) > HEADLINE_MAX:
            raise ValueError(
                f"trigger {self.name!r} headline must be one line of at most "
                f"{HEADLINE_MAX} characters; got {len(self.headline)}")


@dataclass(frozen=True)
class Evaluation:
    """One pass over the warehouse: what fired, what did not, and why not.

    The `silent` half is not a debugging aid. A brief that says "nothing needs you" and
    cannot say what it checked is the exact shape this project keeps being bitten by -
    a clean report for work that may never have happened. So every trigger that does not
    fire names the condition that stopped it, in the reader's words, and the brief prints
    those under the empty "What needs you".

    The warehouse reads are carried along because `render_brief` needs the same ones and
    reading them twice would let the two halves of a single brief disagree.
    """

    gameweek: int
    now: datetime
    threshold: float
    triggers: list[Trigger]
    silent: dict[str, str]          # trigger name -> why it stayed silent
    checks: list[status.Check]
    snapshot: Optional[sqlite3.Row]
    squad: list[dict[str, Any]]
    state: dict[str, Any]
    deadline: Optional[datetime]
    listing: dict[str, Any]


def headline(text: str) -> str:
    """Collapse to one line and fit a lock screen, truncating rather than raising.

    `Trigger` refuses an over-long headline outright, which is the right answer for a
    programming mistake. It is the wrong answer for a player whose name happens to be
    long, so every headline in this module is built through here.
    """
    text = " ".join(str(text).split())
    if len(text) <= HEADLINE_MAX:
        return text
    return text[:HEADLINE_MAX - 1].rstrip() + "…"


def worth_making_threshold() -> float:
    """The net-xP bar for `move_worth_making`, from the environment or the default.

    An unreadable override falls back to the default rather than failing the run: a
    scheduled brief that dies on a typo in the ini tells the owner nothing at all, which
    is strictly worse than telling them slightly the wrong thing and saying so.
    """
    raw = os.environ.get(MIN_NET_XP_ENV)
    if raw is None or not str(raw).strip():
        return WORTH_MAKING_NET_XP
    try:
        return float(raw)
    except (TypeError, ValueError):
        logger.warning("%s=%r is not a number; using the default %.1f",
                       MIN_NET_XP_ENV, raw, WORTH_MAKING_NET_XP)
        return WORTH_MAKING_NET_XP


# --------------------------------------------------------------------------
# Reading the warehouse. Nothing here writes.
# --------------------------------------------------------------------------

def brief_path(gameweek: int, root: Path = Path("logs")) -> Path:
    """Where a gameweek's brief lives: `logs/gw03.md`.

    Zero-padded so a directory listing sorts in gameweek order rather than putting
    gw10 before gw2.
    """
    return Path(root) / f"gw{int(gameweek):02d}.md"


def latest_snapshot(conn: sqlite3.Connection) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT id, captured_at, gameweek, kind FROM snapshot ORDER BY id DESC LIMIT 1"
    ).fetchone()


def default_gameweek(conn: sqlite3.Connection) -> Optional[int]:
    """The gameweek a brief is about when nobody says: the latest snapshot's target.

    The same gameweek `recommend` prices against and `status` checks, so a brief with no
    `--gameweek` describes the state the rest of the pipeline is in rather than a
    calendar the warehouse may not have caught up with.
    """
    snapshot = latest_snapshot(conn)
    return snapshot["gameweek"] if snapshot else None


def _parse_utc(stamp: Optional[str]) -> Optional[datetime]:
    """Parse an FPL timestamp, which is UTC whether or not it says so."""
    if not stamp:
        return None
    text = str(stamp).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        when = datetime.fromisoformat(text)
    except ValueError:
        return None
    return when if when.tzinfo else when.replace(tzinfo=timezone.utc)


def gameweek_deadline(conn: sqlite3.Connection, gameweek: int) -> Optional[datetime]:
    """The gameweek's transfer deadline, derived from its first kickoff.

    See DEADLINE_BEFORE_FIRST_KICKOFF: the warehouse stores fixtures, not events, so
    there is no published deadline to read. A gameweek with no fixtures recorded has no
    derivable deadline, and None is returned rather than a guess - absence of fixtures is
    absence of evidence, the same rule `settle.gameweek_is_finished` follows.
    """
    row = conn.execute(
        "SELECT MIN(kickoff_time) AS first FROM fixture WHERE event = ?",
        (gameweek,)).fetchone()
    kickoff = _parse_utc(row["first"] if row else None)
    return None if kickoff is None else kickoff - DEADLINE_BEFORE_FIRST_KICKOFF


def squad_availability(conn: sqlite3.Connection, snapshot_id: int,
                       gameweek: int) -> list[dict[str, Any]]:
    """Every squad player, with both availability signals attached.

    Two independent sources, kept apart because they answer different questions and
    disagree usefully. FPL's `status` is the club's own word and only moves when there is
    news; the predicted lineup catches rotation, which FPL's flag never reports. A player
    can be `a` in FPL and OUT on RotoWire, and that is the case worth knowing about.

    The lineup row is read from whichever snapshot `lineup_start_rates` would read - the
    most recent one holding lineups for this gameweek, which is not necessarily the
    snapshot the squad came from. Reading it from the squad's own snapshot would report
    "no lineup published" for a gameweek whose lineups arrived an hour later.
    """
    source = conn.execute(
        "SELECT MAX(snapshot_id) AS id FROM predicted_lineup WHERE gameweek = ?",
        (gameweek,)).fetchone()
    lineup_snapshot = source["id"] if source else None

    rows = conn.execute(
        """SELECT ms.position, ms.element_id, ms.multiplier, p.web_name,
                  t.short_name AS team, ps.status, ps.news,
                  ps.chance_of_playing_next_round AS chance
           FROM my_squad ms
           JOIN player p ON p.element_id = ms.element_id
           LEFT JOIN team t ON t.id = p.team_id
           LEFT JOIN player_snapshot ps ON ps.snapshot_id = ms.snapshot_id
                                       AND ps.element_id = ms.element_id
           WHERE ms.snapshot_id = ?
           ORDER BY ms.position""", (snapshot_id,)).fetchall()

    lineup: dict[int, sqlite3.Row] = {}
    if lineup_snapshot is not None:
        lineup = {r["element_id"]: r for r in conn.execute(
            "SELECT * FROM predicted_lineup WHERE snapshot_id = ? AND gameweek = ?",
            (lineup_snapshot, gameweek))}

    out = []
    for row in rows:
        entry = lineup.get(row["element_id"])
        # `UNAVAILABLE` is imported rather than restated: it is the set `lineups` already
        # derives from the scraper's own table, plus the suspension code that table
        # misses. A code added there must not quietly stop counting here.
        listed_out = bool(entry) and entry["injury"] in lineups.UNAVAILABLE
        out.append({
            "element_id": row["element_id"],
            "name": row["web_name"],
            "team": row["team"],
            "position": row["position"],
            "slot": "XI" if (row["position"] or 99) <= recommend.STARTING_XI else "bench",
            "status": row["status"],
            "chance": row["chance"],
            "news": (row["news"] or "").strip(),
            "in_lineup": entry is not None,
            "lineup_starter": bool(entry["is_starter"]) if entry else None,
            "lineup_injury": entry["injury"] if entry else None,
            "lineup_out": listed_out,
        })
    return out


def unavailable_players(squad: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The squad players who cannot play, with the one reason that identifies the fact.

    Priority is deliberate and is what the fingerprint is built from. FPL's own `i`/`s`
    outranks a RotoWire OUT because it is the club's word rather than a prediction, so a
    player who goes from "RotoWire says OUT" to "FPL says injured" changes fingerprint
    and is reported once more. That is the right answer: the doubt was confirmed, and
    confirmation is news.

    `d` is not here. A doubt always carries `chance_of_playing_next_round`, and
    `projection.availability` has already scaled the whole projection by exactly that
    percentage. Pushing it as "unavailable" would charge the same doubt twice, and it
    would fire most weeks - which is how a notification becomes something people mute.
    """
    flagged = []
    for player in squad:
        reason = CANNOT_PLAY.get(player["status"])
        code = player["status"] if reason else None
        if reason is None and player["lineup_out"]:
            reason, code = "out of the predicted lineup", "lineup-out"
        if reason is None:
            continue
        flagged.append(dict(player, reason=reason, reason_code=code))
    return flagged


def falling_holdings(conn: sqlite3.Connection, snapshot_id: int,
                     squad: list[dict[str, Any]],
                     now: Optional[datetime] = None) -> list[pricing.PriceOutlook]:
    """Held players FPL's own forecast makes Very Likely to fall.

    Not a trigger, at the owner's choice - it is the most frequent signal there is and
    the fastest to become noise - so it lives here, in the written record, where it can
    be read rather than pushed.
    """
    outlooks = pricing.price_outlooks(conn, snapshot_id, now)
    held = [outlooks.get(p["element_id"]) for p in squad]
    return [o for o in held if o is not None and o.falling]


def last_settled(conn: sqlite3.Connection) -> Optional[dict[str, Any]]:
    """The most recently graded gameweek, and how the model did on it.

    Reads `outcome`, which only `settle` writes, so an empty answer means "no gameweek
    has been settled" and never "settle failed silently" - and the brief says which.
    """
    row = conn.execute(
        """SELECT gameweek, model_version, COUNT(*) AS n FROM outcome
           GROUP BY gameweek, model_version
           ORDER BY gameweek DESC, model_version DESC LIMIT 1""").fetchone()
    if row is None:
        return None
    slices = settle.calibration(conn, row["gameweek"], row["model_version"])
    return {"gameweek": row["gameweek"], "model_version": row["model_version"],
            "n": row["n"], "slices": slices}


def transfer_state(conn: sqlite3.Connection) -> dict[str, Any]:
    """Free transfers, hit cost and the active transfer chip, or why none of it is known.

    `recommend.transfer_context` owns the pricing rule - unknown free transfers are
    priced as none, an active transfer chip suspends the hit - so it is called rather
    than reimplemented. A warehouse with no snapshot at all is reported as unknown
    instead of raising, because a brief that crashes on a fresh clone is a brief nobody
    can use to find out why.
    """
    try:
        return dict(recommend.transfer_context(conn), known=True)
    except LookupError as e:
        return {"free_transfers": None, "transfer_cost": None, "chip": None,
                "hit_cost": None, "known": False, "reason": str(e)}


def ranked_transfers(conn: sqlite3.Connection, weeks: int = HORIZON_GAMEWEEKS,
                     limit: int = BRIEF_RECOMMENDATIONS) -> dict[str, Any]:
    """The recommender's list, or the reason there is not one.

    Every way `recommend` legitimately declines - no squad captured, no projected
    horizon, no snapshot - is a sentence the brief should print, not a traceback. The
    reason is carried alongside so the reader is told which command was skipped rather
    than being shown an empty table.
    """
    try:
        return {"moves": recommend.recommend(conn, weeks, limit), "reason": None}
    except HorizonMissing as e:
        return {"moves": [], "reason": f"the horizon is not projected: {e}"}
    except LookupError as e:
        return {"moves": [], "reason": str(e)}


# --------------------------------------------------------------------------
# Triggers
# --------------------------------------------------------------------------

def _move_id(move: dict[str, Any]) -> str:
    """A move's identity: who leaves and who arrives. Not its expected points.

    The xP of the same swap moves every hour as prices tick and lineups firm up, so
    folding it into a fingerprint would make an unchanged recommendation look like news
    on every single run.
    """
    return f"{move['out']['element_id']}->{move['in']['element_id']}"


def _hours(delta: timedelta) -> str:
    total = delta.total_seconds() / 3600
    if abs(total) >= 48:
        return f"{total / 24:.1f} days"
    return f"{total:.1f}h"


def evaluate(conn: sqlite3.Connection, gameweek: int, *,
             now: Optional[datetime] = None,
             min_net_xp: Optional[float] = None,
             include_token: bool = True) -> Evaluation:
    """Evaluate all four triggers once, recording why each silent one stayed silent.

    Triggers come out in the order they should be read: a broken warehouse first,
    because nothing below it can be trusted; then a player who cannot play, which is
    points already lost; then the deadline; then the standing recommendation.
    """
    now = now or datetime.now(timezone.utc)
    threshold = worth_making_threshold() if min_net_xp is None else float(min_net_xp)
    triggers: list[Trigger] = []
    silent: dict[str, str] = {}

    # 1. status_failed. `status.gather` is called, never reimplemented: it is the module
    #    that owns what "the warehouse disagrees with itself" means, and a second copy of
    #    those rules here would drift from it within a month. Only a FAIL counts - WARN
    #    exists precisely for the states a healthy warehouse passes through, and a
    #    notification that fires on those is one the owner learns to swipe away.
    checks = status.gather(conn, include_token=include_token)
    failed = [c for c in checks if c.failed]
    if failed:
        labels = sorted(c.label for c in failed)
        triggers.append(Trigger(
            name="status_failed",
            headline=headline(f"fpl-agent status failed: {', '.join(labels)}"),
            detail="\n".join(f"- {c.label}: {c.detail}" for c in failed),
            action="Run `fpl-agent status` and fix what it names. Until it passes, "
                   "nothing else in this brief can be trusted.",
            # The failing labels, not their details: a squad check that goes from 0 rows
            # to 14 rows is still the same broken squad and should not be sent twice.
            fingerprint=f"status_failed:gw{gameweek}:{'+'.join(labels)}",
        ))
    else:
        warned = [c for c in checks if c.level == status.WARN]
        silent["status_failed"] = (
            f"all {len(checks)} warehouse checks passed"
            + (f"; {len(warned)} warn ({', '.join(c.label for c in warned)}), which is "
               f"not an inconsistency" if warned else ""))

    snapshot = latest_snapshot(conn)
    state = transfer_state(conn)
    deadline = gameweek_deadline(conn, gameweek)
    remaining = None if deadline is None else deadline - now
    squad = squad_availability(conn, snapshot["id"], gameweek) if snapshot else []
    listing = ranked_transfers(conn)
    moves = listing["moves"]
    top = moves[0] if moves else None

    # 2. squad_player_unavailable, one per player. Not batched into a single message:
    #    the owner acts on them one at a time, and a batched fingerprint would go stale
    #    the moment a second player was flagged.
    flagged = unavailable_players(squad)
    for player in flagged:
        where = "in your XI" if player["slot"] == "XI" else "on your bench"
        note = player["news"] or player["lineup_injury"] or "no reason published"
        triggers.append(Trigger(
            name="squad_player_unavailable",
            headline=headline(
                f"{player['name']} ({player['team'] or '?'}) is {player['reason']} "
                f"and is {where} for GW{gameweek}"),
            detail=(f"- {player['name']}, squad position {player['position']} "
                    f"({player['slot']}): {player['reason']}.\n"
                    f"- FPL says: {note}"),
            action=(f"Replace {player['name']} or move him to the bench before the "
                    f"GW{gameweek} deadline."),
            fingerprint=(f"squad_player_unavailable:gw{gameweek}:"
                         f"p{player['element_id']}:{player['reason_code']}"),
        ))
    if not flagged:
        silent["squad_player_unavailable"] = (
            "no squad captured on the latest snapshot, so nothing could be checked"
            if not squad else
            f"all {len(squad)} squad players checked: none flagged `i` or `s` by FPL, "
            f"none listed OUT in the gameweek {gameweek} predicted lineups")

    # 3. deadline_with_move. Every condition is about *this* deadline: it has not passed,
    #    it is close, a free transfer is sitting unused, and there is a positive-net move
    #    to spend it on. A free transfer is use-it-or-lose-it, so the bar here is
    #    deliberately lower than trigger 4's - the question is not "is this worth a hit"
    #    but "are you about to waste a move you already own".
    blocking = []
    if remaining is None:
        blocking.append(f"no fixtures recorded for gameweek {gameweek}, so no deadline "
                        f"can be derived")
    elif remaining < timedelta(0):
        blocking.append(f"the gameweek {gameweek} deadline passed {_hours(-remaining)} "
                        f"ago")
    elif remaining > DEADLINE_SOON:
        blocking.append(f"the deadline is {_hours(remaining)} away, beyond the "
                        f"{int(DEADLINE_SOON.total_seconds() // 3600)}h this trigger "
                        f"watches")
    if not (state["free_transfers"] or 0) > 0:
        blocking.append("no free transfer is available to spend"
                        if state["free_transfers"] == 0 else
                        "the snapshot recorded no free-transfer count, which is priced "
                        "as none rather than assumed")
    if top is None:
        blocking.append(listing["reason"] or "no move improves the squad within budget")
    elif top["net_xp_delta"] <= 0:
        blocking.append(f"the best move is net {top['net_xp_delta']:+.2f} xP")

    if blocking:
        silent["deadline_with_move"] = "; ".join(blocking)
    else:
        triggers.append(Trigger(
            name="deadline_with_move",
            headline=headline(
                f"GW{gameweek} deadline in {_hours(remaining)} and "
                f"{state['free_transfers']} free transfer(s) unused"),
            detail=(f"- Deadline {deadline.isoformat(timespec='minutes')} "
                    f"({_hours(remaining)} away).\n"
                    f"- Best move: {top['in']['name']} for {top['out']['name']}, "
                    f"net {top['net_xp_delta']:+.2f} xP over {top['horizon']} gameweeks."
                    + (f"\n- {state['chip'].upper()} is active, so this is a "
                       f"like-for-like swap ranked inside a squad rebuild the tool does "
                       f"not plan." if state["chip"] else "")),
            action=(f"Make {top['in']['name']} for {top['out']['name']}, or decide not "
                    f"to, before {deadline.isoformat(timespec='minutes')}."),
            # The move is the identity, never the hours left, which by definition change
            # on every one of the hourly runs.
            fingerprint=f"deadline_with_move:gw{gameweek}:{_move_id(top)}",
        ))

    # 4. move_worth_making. Not gated on the deadline being near - a move that clears the
    #    bar is worth knowing about on a Tuesday. It is gated on the two things that make
    #    "worth" mean something:
    #
    #    * the net gain clears the threshold. `net_xp_delta` is already after whatever
    #      hit the move would cost, which is what makes the owner's second real state
    #      resolve itself: with zero free transfers every option is charged 4 points, the
    #      whole list goes net-negative, `recommend` returns nothing, and this fires not
    #      at all.
    #    * acting is actually possible. Two ways it is not, and both are live states of
    #      this warehouse rather than hypotheticals:
    #        - an active transfer chip. Under a wildcard hits cost nothing, so every
    #          candidate is "positive" and the top of the list means nothing; worse, the
    #          tool ranks single like-for-like swaps while a wildcard rebuilds all
    #          fifteen, so its advice is the wrong *shape*, not merely optimistic.
    #          The brief says so instead.
    #        - the deadline has gone. The list is still priced against a horizon whose
    #          first gameweek is one you can no longer change your team for. This is not
    #          a nearness gate - a move six days out fires happily - it is the difference
    #          between "not urgent" and "impossible".
    blocking = []
    if state["chip"]:
        blocking.append(f"{state['chip']} is active, so hits cost nothing and every "
                        f"candidate scores positive; the tool ranks single swaps while "
                        f"a chip rebuilds the squad, so its advice is the wrong shape")
    if remaining is not None and remaining < timedelta(0):
        blocking.append(f"the gameweek {gameweek} deadline passed {_hours(-remaining)} "
                        f"ago, so no move on this list can still be made for the "
                        f"gameweek it is priced against")
    if top is None:
        blocking.append(listing["reason"] or "no move improves the squad within budget")
    elif top["net_xp_delta"] < threshold:
        blocking.append(f"the best move is net {top['net_xp_delta']:+.2f} xP, under the "
                        f"{threshold:.1f} bar")

    if blocking:
        silent["move_worth_making"] = "; ".join(blocking)
    else:
        triggers.append(Trigger(
            name="move_worth_making",
            headline=headline(
                f"{top['in']['name']} for {top['out']['name']}: "
                f"net {top['net_xp_delta']:+.2f} xP over {top['horizon']} gameweeks"),
            detail=(f"- Out: {top['out']['name']} "
                    f"(£{top['out']['selling_price'] / 10:.1f}m, {top['out']['xp']} xP)\n"
                    f"- In: {top['in']['name']} ({top['in']['team']}, "
                    f"£{top['in']['now_cost'] / 10:.1f}m, {top['in']['xp']} xP)\n"
                    f"- Net {top['net_xp_delta']:+.2f} after a {top['hit_cost']}-point "
                    f"hit; clears the {threshold:.1f} bar.\n"
                    f"- Price urgency: {top['urgency']} - "
                    f"{top['affordability']['reason']}"),
            action=(f"Make {top['in']['name']} for {top['out']['name']}, or record why "
                    f"not with `fpl-agent recommend --record`."),
            fingerprint=f"move_worth_making:gw{gameweek}:{_move_id(top)}",
        ))

    return Evaluation(gameweek=gameweek, now=now, threshold=threshold,
                      triggers=triggers, silent=silent, checks=checks,
                      snapshot=snapshot, squad=squad, state=state,
                      deadline=deadline, listing=listing)


def evaluate_triggers(conn: sqlite3.Connection, gameweek: int, **kwargs) -> list[Trigger]:
    """The facts worth a push notification. This is the seam a notifier codes against.

    `evaluate` does the work and also records why the silent triggers were silent; this
    is the half a notifier wants, kept under its own name so the contract stays one line
    long and cannot drift as the brief grows sections.
    """
    return evaluate(conn, gameweek, **kwargs).triggers


# --------------------------------------------------------------------------
# The brief
# --------------------------------------------------------------------------

def _table(header: list[str], align: list[str], rows: list[list[str]]) -> list[str]:
    if not rows:
        return []
    return (["| " + " | ".join(header) + " |", "| " + " | ".join(align) + " |"]
            + ["| " + " | ".join(r) + " |" for r in rows])


def render_brief(conn: sqlite3.Connection, gameweek: int, *,
                 now: Optional[datetime] = None,
                 evaluation: Optional[Evaluation] = None,
                 include_token: bool = True) -> str:
    """The gameweek brief as markdown.

    Written for a person holding a phone at 07:00, so the order is what changed, what to
    do, then the evidence. The transfer-chip banner sits at the very top rather than in
    the transfers section, because an active wildcard changes how every recommendation
    below it should be read, and a reader who scrolls past it has been misled by the
    layout rather than by the numbers.

    `evaluation` is accepted so a caller that has already run one does not run a second;
    it is not a different brief, and reading the warehouse twice for one page is how the
    two halves of it would come to disagree.
    """
    if evaluation is None:
        evaluation = evaluate(conn, gameweek, now=now, include_token=include_token)
    now = evaluation.now
    triggers = evaluation.triggers
    snapshot = evaluation.snapshot
    state = evaluation.state
    deadline = evaluation.deadline
    remaining = None if deadline is None else deadline - now
    squad = evaluation.squad
    listing = evaluation.listing
    checks = evaluation.checks

    lines = [f"# Gameweek {gameweek} brief",
             "",
             f"_{now.isoformat(timespec='minutes')} · model {MODEL_VERSION} · "
             + (f"snapshot {snapshot['id']} captured {snapshot['captured_at']}_"
                if snapshot else "no snapshot captured_"),
             ""]

    # The banner. First thing on the page, because it re-reads everything under it.
    if state["chip"]:
        lines += [f"> **{state['chip'].upper()} ACTIVE.** Transfers cost nothing this "
                  f"gameweek, so every move below is \"positive\" and the ranking means "
                  f"much less than usual. This tool ranks single like-for-like swaps; a "
                  f"wildcard rebuilds all fifteen, which it does not plan. Read the "
                  f"table as a shortlist of individual upgrades, not as a plan.", ""]
    elif not state["known"]:
        lines += [f"> **Transfer state unknown** ({state['reason']}). Moves below are "
                  f"priced as though no free transfer exists.", ""]

    # 1. What needs you.
    lines += ["## What needs you", ""]
    if triggers:
        for i, trigger in enumerate(triggers, 1):
            lines += [f"{i}. **{trigger.headline}**", ""]
            lines += [f"   {line}" for line in trigger.detail.splitlines()]
            lines += ["", f"   **Do:** {trigger.action}", ""]
        lead = "The other triggers were evaluated and declined:"
    else:
        lines += ["Nothing needs you. This brief is a record, not a request.", ""]
        lead = "Every trigger was evaluated and declined:"

    # Always printed, whether or not something fired. "Nothing needs you" is only worth
    # anything if it can say what it looked at - a clean report for checks that were
    # never made is the failure mode this whole project keeps tripping over.
    quiet = [n for n in TRIGGER_NAMES if n in evaluation.silent]
    if quiet:
        lines += [lead, ""]
        lines += [f"- `{n}` — {evaluation.silent[n]}." for n in quiet]
        lines.append("")

    # 2. Deadline and budget.
    lines += ["## Deadline and transfers", ""]
    if deadline is None:
        lines.append(f"- No fixtures recorded for gameweek {gameweek}, so no deadline "
                     f"can be derived.")
    else:
        when = deadline.isoformat(timespec="minutes")
        if remaining >= timedelta(0):
            lines.append(f"- Deadline **{when}**, {_hours(remaining)} away "
                         f"(90 minutes before the first kickoff).")
        else:
            lines.append(f"- Deadline **{when}** has passed ({_hours(-remaining)} ago). "
                         f"Transfers made now land in the next gameweek.")
    if state["known"]:
        free = state["free_transfers"]
        lines.append(f"- Free transfers: **{'unknown' if free is None else free}**; "
                     f"the next move costs "
                     f"{'nothing' if not state['hit_cost'] else str(state['hit_cost']) + ' points'}"
                     + (f" ({state['chip']} active)." if state["chip"] else "."))
    else:
        lines.append(f"- Free transfers: unknown ({state['reason']}).")
    lines.append("")

    # 3. Availability.
    lines += ["## Squad availability", ""]
    if not squad:
        lines += ["No squad captured on the latest snapshot, so availability cannot be "
                  "read. An authenticated snapshot is what records it.", ""]
    else:
        problems = [p for p in squad
                    if p["status"] not in (None, "a") or p["lineup_out"]
                    or p["lineup_starter"] is False]
        if not problems:
            lines += [f"All {len(squad)} squad players are available in FPL and named as "
                      f"starters in the predicted lineups for gameweek {gameweek}.", ""]
        else:
            rows = []
            for p in problems:
                if p["in_lineup"]:
                    lineup_note = ("named" if p["lineup_starter"] else "not named") + (
                        f" ({p['lineup_injury']})" if p["lineup_injury"] else "")
                else:
                    lineup_note = "no lineup published"
                rows.append([p["name"], p["team"] or "?", p["slot"],
                             p["status"] or "?",
                             "-" if p["chance"] is None else f"{p['chance']}%",
                             lineup_note, p["news"] or "-"])
            lines += _table(["player", "club", "slot", "FPL", "chance", "lineup", "news"],
                            ["---", "---", "---", "---", "---:", "---", "---"], rows)
            lines += ["",
                      "A `d` with a percentage is already priced into the projections "
                      "below: `projection.availability` scales the whole projection by "
                      "exactly that number, so it is reported here, not charged again.",
                      ""]

    # 4. Price watch. Not a trigger, and the brief says why.
    lines += ["## Price watch", ""]
    falling = (falling_holdings(conn, snapshot["id"], squad, now)
               if snapshot and squad else [])
    if falling:
        lines += _table(["player", "price", "predicted progress", "net transfers"],
                        ["---", "---:", "---:", "---:"],
                        [[o.web_name, f"£{o.now_cost / 10:.1f}m",
                          f"{o.projected_percent:+.0f}%", f"{o.net_transfers:+,}"]
                         for o in falling])
        lines += ["",
                  "A fall costs half the loss on the way back out, because the sell-on "
                  "fee only returns half of any profit. This is deliberately **not** a "
                  "notification: it is the most frequent signal in the warehouse and the "
                  "fastest to become noise.", ""]
    else:
        lines += ["No held player is Very Likely to fall at the next update "
                  "(FPL's own rule: predicted progress past -100%).", ""]

    # 5. The ranked list.
    lines += [f"## Transfers ranked (net xP over {HORIZON_GAMEWEEKS} gameweeks)", ""]
    if listing["reason"]:
        lines += [f"No ranking: {listing['reason']}", ""]
    elif not listing["moves"]:
        tail = (f" that survives a {state['hit_cost']}-point hit"
                if state.get("hit_cost") else "")
        lines += [f"No transfer improves the squad over the horizon within budget{tail}.",
                  ""]
    else:
        rows = []
        for i, move in enumerate(listing["moves"], 1):
            rows.append([str(i), f"{move['in']['name']} ({move['in']['team']})",
                         move["out"]["name"],
                         f"{move['net_xp_delta']:+.2f}", f"{move['xp_delta']:+.2f}",
                         str(move["hit_cost"]), move["urgency"],
                         move["out"]["slot"]])
        lines += _table(["#", "in", "out", "net xP", "gross xP", "hit", "urgency", "slot"],
                        ["---:", "---", "---", "---:", "---:", "---:", "---", "---"], rows)
        lines += ["",
                  f"Every option is priced as *the next transfer you would make*, not as "
                  f"the nth move of a plan, so the same hit applies to all of them. The "
                  f"bar for a notification is a net **{worth_making_threshold():.1f}** "
                  f"xP; anything under that is in this table and not on your phone.", ""]

    # 6. Calibration.
    lines += ["## Last settled gameweek", ""]
    settled = last_settled(conn)
    if settled is None:
        lines += ["No gameweek has been graded yet - `outcome` is empty. Run "
                  "`make settle GW=n` after a gameweek finishes; until then the model "
                  "has no measured error and every projection here is untested.", ""]
    else:
        overall = settled["slices"].get("overall") or []
        rows = []
        for group, entries in settled["slices"].items():
            for s in entries:
                rows.append([group, s.name, str(s.n), f"{s.predicted:.2f}",
                             f"{s.actual:.2f}", f"{s.bias:+.2f}", f"{s.mae:.2f}"])
        head = (f"Gameweek {settled['gameweek']} under model "
                f"{settled['model_version']}, {settled['n']} players graded.")
        if overall:
            head += (f" Overall bias {overall[0].bias:+.2f} (positive means "
                     f"over-projecting), MAE {overall[0].mae:.2f}.")
        lines += [head, ""]
        lines += _table(["group", "slice", "n", "predicted", "actual", "bias", "MAE"],
                        ["---", "---", "---:", "---:", "---:", "---:", "---:"], rows)
        lines.append("")

    # 7. The warehouse itself, last: it is the footnote unless it failed, and if it
    #    failed it is already at the top under "What needs you".
    lines += ["## Warehouse", ""]
    lines += _table(["check", "level", "detail"], ["---", "---", "---"],
                    [[c.label, c.level, c.detail.replace("|", "\\|")] for c in checks])
    lines += ["",
              "_Written by `fpl-agent brief`, read-only against the warehouse._", ""]
    return "\n".join(lines)


def write_brief(conn: sqlite3.Connection, gameweek: int, root: Path = BRIEF_DIR,
                **kwargs) -> Path:
    """Render the brief and write it to `logs/gwNN.md`, creating `logs/` if needed.

    The same pattern `settle.draft_learning` follows: the directory is created by the
    first write rather than committed empty, because an empty `logs/` in a fresh clone
    claims a run has happened that has not. `logs/` is not gitignored, so the file is
    tracked once written - which is the point. The reasoning trail is committed.
    """
    path = brief_path(gameweek, root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_brief(conn, gameweek, **kwargs))
    return path


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

EXIT_OK = 0
EXIT_UNREADABLE = 2


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Write the gameweek brief from the warehouse. Read-only.")
    parser.add_argument("--db", type=Path, default=storage.DEFAULT_DB_PATH)
    parser.add_argument("--gameweek", type=int,
                        help="defaults to the latest snapshot's target gameweek")
    parser.add_argument("--logs", type=Path, default=BRIEF_DIR,
                        help=f"where to write gwNN.md (default {BRIEF_DIR})")
    parser.add_argument("--dry-run", action="store_true",
                        help="print the brief and the fired triggers; write nothing")
    args = parser.parse_args(argv)

    config.load()
    logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stderr)

    # Read-only for the same reason `status` is: a brief that could change the warehouse
    # is a brief you cannot trust to describe it. `storage.connect` would create and
    # migrate the file, turning "no warehouse" into "an empty warehouse that looks fine".
    try:
        conn = status.connect_readonly(args.db)
    except FileNotFoundError:
        print(f"no warehouse at {args.db} - nothing has ever been captured. "
              f"Run `make snapshot`.", file=sys.stderr)
        return EXIT_UNREADABLE
    except sqlite3.Error as e:
        print(f"could not open {args.db} read-only: {e}", file=sys.stderr)
        return EXIT_UNREADABLE

    try:
        gameweek = args.gameweek if args.gameweek is not None else default_gameweek(conn)
        if gameweek is None:
            print("no snapshot carries a target gameweek, and none was given; "
                  "pass --gameweek or run `make snapshot`.", file=sys.stderr)
            return EXIT_UNREADABLE
        evaluation = evaluate(conn, gameweek)
        triggers = evaluation.triggers
        text = render_brief(conn, gameweek, evaluation=evaluation)
        if args.dry_run:
            print(text)
            print(f"\n--- {len(triggers)} trigger(s) would fire ---", file=sys.stderr)
            for t in triggers:
                print(f"{t.name}  {t.fingerprint}\n  {t.headline}\n  do: {t.action}",
                      file=sys.stderr)
            # The silent ones are printed too. A run that fires nothing has to be able to
            # say why, or it is indistinguishable from a run that checked nothing.
            for name in TRIGGER_NAMES:
                if name in evaluation.silent:
                    print(f"{name}  DID NOT FIRE\n  {evaluation.silent[name]}",
                          file=sys.stderr)
            return EXIT_OK
        path = write_brief(conn, gameweek, args.logs, evaluation=evaluation)
        print(f"wrote {path} ({len(triggers)} trigger(s): "
              f"{', '.join(t.name for t in triggers) or 'none'})")
        return EXIT_OK
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())

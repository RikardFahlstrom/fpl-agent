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

  `evaluate`          the small set of facts worth interrupting someone for, and why
                      the rest stayed silent - `notify` reads both halves. The review
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

**The brief opens with the same block every run** - move, ownership, wildcard,
availability, deadline, push, data - each line saying "none" or "not evaluated" when
there is nothing, so the reader looks at the same line every time rather than reading
the page to find out nothing happened (`CONTEXT.md`, *brief*). Under it, "What needs
you" carries the bodies of whatever fired, "Push" says what became of every trigger,
and the sections show the working.

**Plain English is a rule, not a style.** Nothing from the code - a trigger name, a
check level, a slice id - appears without its meaning beside it on first use; internal
names survive only where a command has to be typed. The owner's own example: a
learning that says "P(start) 75-100% under-projected by 0.54" means nothing on a phone,
and "players almost certain to start scored about half a point more per game than the
model expected" is the same fact. `plain_slice` is where slice names are translated.

**Push states are three words that are not interchangeable** (`CONTEXT.md`, *push*):
*did not fire* (the condition was not met), *sent <when>* (the phone got it), and
*fired, not delivered* (it should have and did not - no topic, or the send failed).
The last is a data problem and the Data line says so, because a push that fired and
went nowhere must never look like one that had nothing to say. `notify` runs before
this command in the scheduled jobs so that "sent" can be read from the record rather
than promised.

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
from . import (chips, held, lineups, pricing, recommend, settle, status, storage,
               warehouse)
from .projection import HORIZON_GAMEWEEKS, MODEL_VERSION, HorizonMissing

logger = logging.getLogger("fpl_brief")

BRIEF_DIR = Path("logs")

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
                 "move_worth_making", "chip_worth_playing")

# What each trigger is called on the page. The code names above are what `notify`
# stores and what a person types; these are what a person reads.
TRIGGER_TITLES = {
    "status_failed": "Broken warehouse",
    "squad_player_unavailable": "A player you own cannot play",
    "deadline_with_move": "Deadline near with a free transfer unused",
    "move_worth_making": "A move worth making",
    "chip_worth_playing": "A chip worth playing",
}

# The calibration slices, in the reader's words. Keyed on the names `settle` writes.
PLAIN_SLICES = {
    "all players": "all players",
    "GKP": "goalkeepers", "DEF": "defenders", "MID": "midfielders", "FWD": "forwards",
    "P(start) 0-25%": "players unlikely to start",
    "P(start) 25-50%": "players who might start",
    "P(start) 50-75%": "players likely to start",
    "P(start) 75-100%": "players almost certain to start",
}

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

    Everything the brief prints is read here, once, and carried: `render_block` and
    `render_brief` take an Evaluation and nothing else - no connection, no clock, no
    directory - so the opening block and the working beneath it cannot disagree, and
    a layout can be tested from a hand-built value.
    """

    gameweek: int
    now: datetime
    threshold: float
    triggers: list[Trigger]
    silent: dict[str, str]          # trigger name -> why it stayed silent
    checks: list[status.Check]
    capture: Optional[warehouse.Capture]
    squad: list[dict[str, Any]]
    state: dict[str, Any]
    deadline: Optional[datetime]
    listing: dict[str, Any]
    chips: list[chips.Verdict]
    #: Which rival picks ownership is measured from; shown when there is no move to
    #: measure (a move carries its own copy, see `recommend.move_lines`).
    ownership: warehouse.OwnershipSource
    captain_picks: list[dict[str, Any]]   # the model's captain for the target gameweek
    falling: list[pricing.PriceOutlook]   # held players FPL forecasts to fall
    last_settled: Optional[dict[str, Any]]
    learnings: list[settle.Learning]
    reports: list["PushReport"]           # one per trigger, fired or not


@dataclass(frozen=True)
class PushReport:
    """What became of one trigger this run, in the three words the owner chose."""

    name: str
    title: str
    state: str          # "did not fire" | "sent" | "fired, not delivered"
    detail: str         # why it did not fire, what was sent, or why it was not
    when: Optional[str] = None   # sent: when, in the reader's format

    @property
    def delivered(self) -> bool:
        return self.state == "sent"

    @property
    def undelivered(self) -> bool:
        return self.state == "fired, not delivered"


def _push_reports(conn: sqlite3.Connection, triggers: list[Trigger],
                  silent: dict[str, str], *,
                  configured: Optional[bool] = None) -> list[PushReport]:
    """One report per trigger, fired or not, in reading order.

    "sent" is read from the `notification` table, never inferred: `notify` writes a row
    only after the server accepted the message, so a row is the fact. A fired trigger
    with no row is *not delivered*, and the reason is the one thing the brief can know
    from here - no topic configured, or the send did not happen (it runs before this
    command in `make now`; by hand, `fpl-agent notify` says why).
    """
    if configured is None:
        from . import notify     # notify imports this module; a local import breaks the cycle
        configured = notify.target_from_env() is not None
    sent = storage.sent_notifications(conn, [t.fingerprint for t in triggers])
    reports = []
    for name in TRIGGER_NAMES:
        fired = [t for t in triggers if t.name == name]
        if not fired:
            reports.append(PushReport(name, TRIGGER_TITLES[name], "did not fire",
                                      silent.get(name, "not evaluated")))
            continue
        for trigger in fired:
            when = sent.get(trigger.fingerprint)
            if when:
                reports.append(PushReport(name, TRIGGER_TITLES[name], "sent",
                                          f"{_when(when)}: {trigger.headline}",
                                          when=_when(when)))
            elif not configured:
                reports.append(PushReport(
                    name, TRIGGER_TITLES[name], "fired, not delivered",
                    f"no ntfy topic in fpl-agent.ini, so nothing can reach your phone. "
                    f"{trigger.headline}"))
            else:
                reports.append(PushReport(
                    name, TRIGGER_TITLES[name], "fired, not delivered",
                    f"not in the sent record; `fpl-agent notify` sends it and says why "
                    f"if it cannot. {trigger.headline}"))
    return reports


def _when(stamp: str) -> str:
    parsed = storage.parse_utc(stamp)
    return parsed.strftime("%a %d %b %H:%M UTC") if parsed else stamp


def plain_slice(name: str) -> str:
    """A calibration slice in the reader's words, or the name itself when unknown."""
    return PLAIN_SLICES.get(name, name)


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


def default_gameweek(conn: sqlite3.Connection) -> Optional[int]:
    """The gameweek a brief is about when nobody says: the latest snapshot's target.

    The same gameweek `recommend` prices against and `status` checks, so a brief with no
    `--gameweek` describes the state the rest of the pipeline is in rather than a
    calendar the warehouse may not have caught up with.
    """
    capture = warehouse.latest(conn)
    return capture.gameweek if capture else None


def squad_availability(conn: sqlite3.Connection, holding: held.HeldSquad,
                       gameweek: int) -> list[dict[str, Any]]:
    """Every held player, with both availability signals attached.

    Two independent sources, kept apart because they answer different questions and
    disagree usefully. FPL's `status` is the club's own word and only moves when there is
    news; the predicted lineup catches rotation, which FPL's flag never reports. A player
    can be `a` in FPL and OUT on RotoWire, and that is the case worth knowing about.

    The lineup row is read from whichever capture `lineup_start_rates` would read -
    `warehouse.with_lineups`, which is not necessarily the snapshot the squad came from.
    Reading it from the squad's own snapshot would report "no lineup published" for a
    gameweek whose lineups arrived an hour later.
    """
    source = warehouse.with_lineups(conn, gameweek)
    lineup_snapshot = source.id if source else None

    # FPL's word on each held player, from the squad's own capture.
    market = {r["element_id"]: r for r in conn.execute(
        """SELECT ps.element_id, ps.status, ps.news,
                  ps.chance_of_playing_next_round AS chance
           FROM player_snapshot ps WHERE ps.snapshot_id = ?""", (holding.snapshot_id,))}
    teams = {r["id"]: r["short_name"] for r in conn.execute(
        "SELECT id, short_name FROM team")}

    lineup: dict[int, sqlite3.Row] = {}
    if lineup_snapshot is not None:
        lineup = {r["element_id"]: r for r in conn.execute(
            "SELECT * FROM predicted_lineup WHERE snapshot_id = ? AND gameweek = ?",
            (lineup_snapshot, gameweek))}

    out = []
    for player in holding.players:
        row = market.get(player.element_id)
        entry = lineup.get(player.element_id)
        # `UNAVAILABLE` is imported rather than restated: it is the set `lineups` already
        # derives from the scraper's own table, plus the suspension code that table
        # misses. A code added there must not quietly stop counting here.
        listed_out = bool(entry) and entry["injury"] in lineups.UNAVAILABLE
        out.append({
            "element_id": player.element_id,
            "name": player.name,
            "team": teams.get(player.team_id),
            "position": player.position,
            "slot": "XI" if player.starts else "bench",
            "status": row["status"] if row else None,
            "chance": row["chance"] if row else None,
            "news": ((row["news"] if row else None) or "").strip(),
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


def availability_problems(squad: list[dict[str, Any]]) -> list[tuple[dict[str, Any], str]]:
    """Every squad player either source has a question about, with the reason in words.

    Broader than `unavailable_players`, on purpose: a doubt or an unnamed starter is
    worth a line on the page even though neither is worth a push.
    """
    problems = []
    for p in squad:
        if p["status"] in CANNOT_PLAY:
            reason = CANNOT_PLAY[p["status"]]
        elif p["status"] == "d":
            reason = "doubtful" + (f", {p['chance']}% to play" if p["chance"] is not None else "")
        elif p["status"] not in (None, "a"):
            reason = f"FPL status `{p['status']}`"
        elif p["lineup_out"]:
            reason = "out of the predicted lineup" + (
                f" ({p['lineup_injury']})" if p["lineup_injury"] else "")
        elif p["lineup_starter"] is False:
            reason = "not named a starter in the predicted lineup"
        else:
            continue
        problems.append((p, reason))
    return problems


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


def _captain_line(picks: list[dict[str, Any]]) -> list[str]:
    """The push's captain line, on the captured XI: who, and one sentence why.

    Nothing when there is no squad or no projection - the push is about the move, and
    a captain line that says "not evaluated" would be noise on a lock screen.
    """
    if not picks:
        return []
    why = chips.captain_why(picks, short=True)
    return [f"- Captain: {chips.captain_line(picks)}" + (f". {why}" if why else "")]


def _deadline_line(deadline: Optional[datetime], remaining: Optional[timedelta]) -> str:
    if deadline is None:
        return "- Deadline: none derived (no fixtures recorded)"
    when = deadline.strftime("%a %d %b %H:%M UTC")
    if remaining < timedelta(0):
        return f"- Deadline {when} passed {_hours(-remaining)} ago"
    return f"- Deadline {when}, {_hours(remaining)} away"


def _hours(delta: timedelta) -> str:
    total = delta.total_seconds() / 3600
    if abs(total) >= 48:
        return f"{total / 24:.1f} days"
    return f"{total:.1f}h"


def evaluate(conn: sqlite3.Connection, gameweek: int, *,
             now: Optional[datetime] = None,
             min_net_xp: Optional[float] = None,
             include_token: bool = True,
             notifications_configured: Optional[bool] = None,
             learnings_dir: Path = settle.LEARNINGS_DIR) -> Evaluation:
    """Evaluate all four triggers once, recording why each silent one stayed silent,
    and read everything else the brief prints while at it.

    Triggers come out in the order they should be read: a broken warehouse first,
    because nothing below it can be trusted; then a player who cannot play, which is
    points already lost; then the deadline; then the standing recommendation.

    `notifications_configured` and `learnings_dir` are the two facts that come from
    outside the warehouse - whether a push can reach the phone, and where the drafted
    learnings are - taken as arguments so that a test never reads the repo's own.
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
        warned = [c.label for c in checks if c.level == status.WARN]
        silent["status_failed"] = (
            f"all {len(checks)} warehouse checks passed"
            + (f"; {', '.join(warned)} {'is' if len(warned) == 1 else 'are'} stale or "
               f"pending, not broken (the Warehouse table says what)" if warned else ""))

    capture = warehouse.latest(conn)
    state = transfer_state(conn)
    deadline = warehouse.deadline(conn, gameweek)
    remaining = None if deadline is None else deadline - now
    # The held squad, read once: availability, the captain and the chips all take it.
    holding = held.read(conn, capture) if capture else None
    squad = squad_availability(conn, holding, gameweek) if holding else []
    listing = ranked_transfers(conn)
    moves = listing["moves"]
    top = moves[0] if moves else None
    ownership, _ = recommend.ownership_source(conn)
    captain_picks = (chips.captain_picks(conn, gameweek, MODEL_VERSION, holding)
                     if holding else [])

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
                    f"GW{gameweek} deadline." if player["slot"] == "XI" else
                    f"{player['name']} is already on your bench, so he costs you "
                    f"nothing unless an XI player drops out; replace him only if a "
                    f"transfer is worth it."),
            fingerprint=(f"squad_player_unavailable:gw{gameweek}:"
                         f"p{player['element_id']}:{player['reason_code']}"),
        ))
    if not flagged:
        silent["squad_player_unavailable"] = (
            "no squad captured on the latest snapshot, so nothing could be checked"
            if not squad else
            f"all {len(squad)} players you own are fit in FPL's eyes (none injured or "
            f"suspended) and none is out of the predicted lineups for gameweek "
            f"{gameweek}")

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
                    f"net {top['net_xp_delta']:+.2f} xP over {top['horizon']} gameweeks.\n"
                    f"- {recommend.move_lines(top)[1]}"
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
            # The push body, in the order it is read on a lock screen: what the move is
            # worth, who owns whom, when it has to be done by, and the one action.
            detail="\n".join([
                f"- Out: {top['out']['name']} "
                f"(£{top['out']['selling_price'] / 10:.1f}m, {top['out']['xp']} xP); "
                f"in: {top['in']['name']} ({top['in']['team']}, "
                f"£{top['in']['now_cost'] / 10:.1f}m, {top['in']['xp']} xP)",
                *(f"- {line}" for line in recommend.move_lines(top)),
                f"- Net {top['net_xp_delta']:+.2f} clears the {threshold:.1f} bar. "
                f"Price: {top['affordability']['reason']}",
                *_captain_line(captain_picks),
                _deadline_line(deadline, remaining),
            ]),
            action=(f"Make {top['in']['name']} for {top['out']['name']}, or record why "
                    f"not with `fpl-agent recommend --record`."),
            fingerprint=f"move_worth_making:gw{gameweek}:{_move_id(top)}",
        ))

    # 5. chip_worth_playing, one per chip that clears its bar this week. Valued on the
    #    squad after the recommended move, so this and the move advice agree.
    verdicts = (chips.evaluate(conn, gameweek, MODEL_VERSION, holding.with_move(top))
                if holding else [])
    playable = [v for v in verdicts if v.play_now]
    if remaining is not None and remaining < timedelta(0):
        playable = []
    for verdict in playable:
        triggers.append(Trigger(
            name="chip_worth_playing",
            headline=headline(f"Play your {verdict.title} this gameweek: "
                              f"+{verdict.now.value:.1f} ({verdict.now.note})"),
            detail="\n".join([
                f"- {verdict.title.capitalize()} {verdict.reason}",
                f"- Best other week: GW{_runner_up(verdict).gameweek} "
                f"(+{_runner_up(verdict).value:.1f}, {_runner_up(verdict).note})"
                if _runner_up(verdict) else "- No other week to compare against",
                *(["- Valued on the squad after the recommended move "
                   f"({top['in']['name']} for {top['out']['name']})"] if top else []),
                _deadline_line(deadline, remaining),
            ]),
            action=(f"Play the {verdict.title} before "
                    f"{deadline.strftime('%a %d %b %H:%M UTC') if deadline else 'the deadline'}"
                    f", or record why not with `fpl-agent recommend --record --chip "
                    f"{verdict.state.name}`."),
            fingerprint=f"chip_worth_playing:gw{gameweek}:{verdict.state.name}",
        ))
    if not playable:
        evaluated = [v for v in verdicts if v.evaluated]
        if not verdicts:
            silent["chip_worth_playing"] = "no chips captured, so none could be valued"
        elif not evaluated:
            silent["chip_worth_playing"] = "no chip is available to value this gameweek"
        elif remaining is not None and remaining < timedelta(0):
            silent["chip_worth_playing"] = (f"the gameweek {gameweek} deadline has "
                                            f"passed, so no chip can be played for it")
        else:
            silent["chip_worth_playing"] = "; ".join(
                f"{v.title} {v.reason}" for v in evaluated)

    return Evaluation(
        gameweek=gameweek, now=now, threshold=threshold,
        triggers=triggers, silent=silent, checks=checks,
        capture=capture, squad=squad, state=state,
        deadline=deadline, listing=listing, chips=verdicts,
        ownership=ownership, captain_picks=captain_picks,
        falling=(falling_holdings(conn, capture.id, squad, now)
                 if capture and squad else []),
        last_settled=last_settled(conn),
        learnings=settle.read_learnings(learnings_dir),
        reports=_push_reports(conn, triggers, silent, configured=notifications_configured))


def _runner_up(verdict: chips.Verdict) -> Optional[chips.ChipValue]:
    others = [v for v in verdict.values if v.gameweek != verdict.now.gameweek]
    return max(others, key=lambda v: v.value) if others else None


# --------------------------------------------------------------------------
# The brief
# --------------------------------------------------------------------------

def _table(header: list[str], align: list[str], rows: list[list[str]]) -> list[str]:
    if not rows:
        return []
    return (["| " + " | ".join(header) + " |", "| " + " | ".join(align) + " |"]
            + ["| " + " | ".join(r) + " |" for r in rows])


def learnings_line(learnings: list[settle.Learning]) -> str:
    """The proposed learnings, grouped by what they claim, in the reader's words.

    Two drafts naming the same slice in different gameweeks are the signal a single
    draft asks the reader to wait for, so the grouping is the point: "GW3 and GW4 both"
    is what makes a learning worth acting on, and it is said here rather than left for
    someone to notice across two files.
    """
    proposed = [l for l in learnings if l.status == "proposed"]
    if not proposed:
        return "none proposed" if not learnings else (
            f"none proposed ({len(learnings)} closed)")
    groups: dict[tuple[str, str], list[settle.Learning]] = {}
    for learning in proposed:
        groups.setdefault((learning.metric, learning.slice), []).append(learning)
    claims = []
    for (_, slice_name), members in groups.items():
        weeks = [f"GW{l.gameweek}" for l in members if l.gameweek is not None]
        biases = [l.bias for l in members if l.bias is not None]
        who = plain_slice(slice_name)
        if biases:
            mean = sum(biases) / len(biases)
            size = ("about half a point" if 0.35 <= abs(mean) < 0.75 else
                    "about a point" if 0.75 <= abs(mean) < 1.5 else
                    f"about {abs(mean):.1f} points")
            what = (f"{who} scored {size} per game "
                    f"{'less' if mean > 0 else 'more'} than the model expected")
        else:
            what = f"{who}: {members[0].observation}"
        when = (", ".join(weeks[:-1]) + f" and {weeks[-1]} both" if len(weeks) > 1
                else weeks[0] if weeks else "")
        ids = ", ".join(l.id for l in members)
        claims.append(f"{what} ({when}; {ids})" if when else f"{what} ({ids})")
    return f"{len(proposed)} proposed - " + "; ".join(claims)


def render_block(evaluation: Evaluation, *, markdown: bool = True) -> list[str]:
    """The fixed opening block: the same lines in the same order, every run.

    Every line is present whether or not there is anything to say, and says "none" or
    "not evaluated" rather than going missing - the whole value of the block is that the
    reader looks at the same line every time.
    """
    state, squad, deadline = evaluation.state, evaluation.squad, evaluation.deadline
    remaining = None if deadline is None else deadline - evaluation.now
    listing, reports = evaluation.listing, evaluation.reports
    top = listing["moves"][0] if listing["moves"] else None
    threshold = evaluation.threshold

    # Move.
    if top is None:
        move = "none - " + (listing["reason"]
                            or "no transfer improves the squad within budget")
    else:
        bar = ("clears" if top["net_xp_delta"] >= threshold else "under")
        move = (f"{top['in']['name']} for {top['out']['name']}, net "
                f"{top['net_xp_delta']:+.2f} xP over {top['horizon']} gameweeks - "
                f"{bar} the {threshold:.1f} bar")
        if state.get("chip"):
            move += f" ({state['chip'].replace('freehit', 'free hit')} active: single swaps, not a rebuild)"
        elif any(v.play_now and v.state.name == "wildcard" for v in evaluation.chips):
            move += " - moot if you play the wildcard (see Chips)"

    # Ownership - of the move when there is one, of the rivals capture otherwise.
    if top is not None:
        ownership = recommend.move_lines(top)[1].replace("Ownership not shown", "not shown")
    else:
        source = evaluation.ownership
        ownership = (f"rivals captured for gameweek {source.gameweek} "
                     f"({source.managers} rivals); no move to measure"
                     if source.fresh else f"not shown: {source.reason}")

    # Captain: the model's pick for the target gameweek from the captured XI.
    picks = evaluation.captain_picks
    captain = chips.captain_line(picks) if squad else "not evaluated - no squad captured"
    why = chips.captain_why(picks)
    if why:
        captain += f". {why}"

    # Chips: one clause per chip, then the wall.
    chip_line = (chips.chips_line(evaluation.chips, evaluation.gameweek) if squad
                 else "not evaluated - no squad captured")

    # Availability.
    if not squad:
        availability = "unknown - no squad captured"
    else:
        problems = availability_problems(squad)
        availability = (f"{len(squad)} of {len(squad)}" if not problems else
                        f"{len(squad) - len(problems)} of {len(squad)} - "
                        + ", ".join(f"{p['name']} ({why})" for p, why in problems))

    # Deadline.
    if deadline is None:
        when = "unknown - no fixtures recorded"
    elif remaining < timedelta(0):
        when = f"{deadline.strftime('%a %d %b %H:%M UTC')} passed {_hours(-remaining)} ago"
    else:
        when = f"{deadline.strftime('%a %d %b %H:%M UTC')}, {_hours(remaining)} away"
    if state.get("known"):
        free = state["free_transfers"]
        when += ("; free transfers unknown" if free is None else
                 f"; {free} free transfer{'' if free == 1 else 's'} unused")

    # Push.
    delivered = [r for r in reports if r.delivered]
    undelivered = [r for r in reports if r.undelivered]
    parts = []
    if delivered:
        parts.append("sent: " + ", ".join(f"{r.title} ({r.when})" for r in delivered))
    if undelivered:
        parts.append("fired, not delivered: " + ", ".join(r.title for r in undelivered))
    push = "; ".join(parts) if parts else f"nothing fired - all {len(reports)} triggers checked"

    # Learnings.
    pending = learnings_line(evaluation.learnings)

    # Data.
    failed = [c.label for c in evaluation.checks if c.failed]
    warned = [c.label for c in evaluation.checks if c.level == status.WARN]
    if failed:
        data = f"NOT trustworthy - {', '.join(failed)} failed; see What needs you"
    elif warned:
        data = f"trustworthy; stale or pending, not broken: {', '.join(warned)} (see Warehouse)"
    else:
        data = f"trustworthy - all {len(evaluation.checks)} checks agree"
    if undelivered:
        data += "; a push fired and did not reach your phone (see Push)"

    rows = [("Move", move), ("Ownership", ownership), ("Captain", captain),
            ("Chips", chip_line),
            ("Availability", availability), ("Deadline", when), ("Push", push),
            ("Learnings", pending), ("Data", data)]
    if markdown:
        return [f"- **{label}:** {text}" for label, text in rows] + [""]
    width = max(len(label) for label, _ in rows) + 1
    return [f"{label + ':':<{width}} {text}" for label, text in rows]


def render_brief(evaluation: Evaluation) -> str:
    """The gameweek brief as markdown, from one Evaluation and nothing else.

    Written for a person holding a phone at 07:00, so the order is what changed, what to
    do, then the evidence. The transfer-chip banner sits at the very top rather than in
    the transfers section, because an active wildcard changes how every recommendation
    below it should be read, and a reader who scrolls past it has been misled by the
    layout rather than by the numbers.
    """
    gameweek = evaluation.gameweek
    reports = evaluation.reports
    now = evaluation.now
    triggers = evaluation.triggers
    capture = evaluation.capture
    state = evaluation.state
    deadline = evaluation.deadline
    remaining = None if deadline is None else deadline - now
    squad = evaluation.squad
    listing = evaluation.listing
    checks = evaluation.checks

    lines = [f"# Gameweek {gameweek} brief",
             "",
             f"_{now.isoformat(timespec='minutes')} · model {MODEL_VERSION} · "
             + (f"snapshot {capture.id} captured {capture.captured_at}_"
                if capture else "no snapshot captured_"),
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

    # 0. The block. Same lines, same order, every run.
    lines += render_block(evaluation)

    # 1. What needs you.
    lines += ["## What needs you", ""]
    if triggers:
        for i, trigger in enumerate(triggers, 1):
            lines += [f"{i}. **{TRIGGER_TITLES[trigger.name]} — {trigger.headline}**", ""]
            lines += [f"   {line}" for line in trigger.detail.splitlines()]
            lines += ["", f"   **Do:** {trigger.action}", ""]
    else:
        lines += ["Nothing needs you. This brief is a record, not a request.", ""]

    # 2. Push. Always printed, whether or not something fired. "Nothing needs you" is
    #    only worth anything if it can say what it looked at - a clean report for checks
    #    that were never made is the failure mode this whole project keeps tripping over.
    lines += ["## Push", ""]
    lines += [f"- {r.title} — **{r.state}**: {r.detail.rstrip('.')}." for r in reports]
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
                         f"({storage.DEADLINE_BEFORE_KICKOFF.seconds // 60} minutes before "
                         f"the first kickoff).")
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
        problems = [p for p, _ in availability_problems(squad)]
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
    falling = evaluation.falling
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

    # 4b. Chips: the week-by-week working behind the line.
    lines += ["## Chips", ""]
    valued = [v for v in evaluation.chips if v.values]
    if not evaluation.chips:
        lines += ["No chips captured - an authenticated snapshot records them.", ""]
    else:
        lines += [f"- {v.title.capitalize()}: {v.reason}." for v in evaluation.chips]
        lines.append("")
        if valued:
            weeks = sorted({x.gameweek for v in valued for x in v.values})
            by_chip = {v.title: {x.gameweek: x for x in v.values} for v in valued}
            rows = []
            last_note: dict[str, str] = {}
            for week in weeks:
                cells = []
                for title in by_chip:
                    x = by_chip[title].get(week)
                    if x is None:
                        cells.append("-")
                        continue
                    # The names only when they change, so a bench that is the same
                    # fifteen weeks running is not written fifteen times.
                    cell = f"+{x.value:.1f}"
                    if last_note.get(title) != x.note:
                        cell += f" ({x.note})"
                        last_note[title] = x.note
                    cells.append(cell)
                rows.append([f"GW{week}" + (" *" if week == gameweek else ""), *cells])
            lines += _table(["week", *by_chip], ["---", *["---:"] * len(by_chip)], rows)
            for v in valued:
                if v.play_now and v.now.squad:
                    lines += ["", f"The {v.title} squad for GW{v.now.gameweek} "
                                  f"(XI first, then bench): "
                                  + "; ".join(v.now.squad) + "."]
            wall = chips.window_end([v.state for v in evaluation.chips], gameweek)
            reach = (f"The set expires after GW{wall}" if wall else
                     "No expiry is recorded for this set")
            if wall and weeks[-1] < wall:
                reach += (f"; weeks GW{weeks[-1] + 1}-GW{wall} are not projected yet "
                          f"(`project --chips`)")
            lines += ["",
                      f"Values are what the chip would add that week, on the squad after "
                      f"the recommended move. Only the next gameweek has predicted "
                      f"lineups; every later week is the player's rates against that "
                      f"week's fixtures, which is why this week tends to look best and "
                      f"why a chip also has to clear its bar (bench boost "
                      f"{chips.CHIP_BARS['bboost']:.0f}, triple captain "
                      f"{chips.CHIP_BARS['3xc']:.0f}, free hit "
                      f"{chips.CHIP_BARS['freehit']:.0f} over the held squad, wildcard "
                      f"{chips.CHIP_BARS['wildcard']:.0f} over {chips.WILDCARD_HORIZON} "
                      f"weeks). A rebuild is the best legal fifteen today's prices buy "
                      f"with bank plus selling prices. {reach}.",
                      ""]

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
        source = listing["moves"][0]["ownership"]

        def owned(player: dict[str, Any]) -> str:
            if not source["fresh"]:
                return "-"
            return f"{player['owned_by']} of {player['managers']}"

        rows = []
        for i, move in enumerate(listing["moves"], 1):
            rows.append([str(i), f"{move['in']['name']} ({move['in']['team']})",
                         owned(move["in"]), move["out"]["name"], owned(move["out"]),
                         f"{move['net_xp_delta']:+.2f}", f"{move['xp_delta']:+.2f}",
                         str(move["hit_cost"]), move["urgency"],
                         move["out"]["slot"]])
        lines += _table(["#", "in", "rivals own", "out", "rivals own",
                         "net xP", "gross xP", "hit", "urgency", "slot"],
                        ["---:", "---", "---:", "---", "---:",
                         "---:", "---:", "---:", "---", "---"], rows)
        if source["fresh"]:
            lines += ["", f"*Rivals own* counts the rivals in your leagues holding the "
                          f"player, from their gameweek {source['gameweek']} squads "
                          f"({source['managers']} rivals)."]
        else:
            lines += ["", f"Ownership not shown: {source['reason']}."]
        lines += ["",
                  f"Every option is priced as *the next transfer you would make*, not as "
                  f"the nth move of a plan, so the same hit applies to all of them. The "
                  f"bar for a notification is a net **{worth_making_threshold():.1f}** "
                  f"xP; anything under that is in this table and not on your phone.", ""]

    # 6. Calibration.
    lines += ["## Last settled gameweek", ""]
    settled = evaluation.last_settled
    if settled is None:
        lines += ["No gameweek has been graded yet - `outcome` is empty. Run "
                  "`make settle GW=n` after a gameweek finishes; until then the model "
                  "has no measured error and every projection here is untested.", ""]
    else:
        overall = settled["slices"].get("overall") or []
        rows = []
        for group, entries in settled["slices"].items():
            for s in entries:
                rows.append([group.replace("_", " "), plain_slice(s.name), str(s.n),
                             f"{s.predicted:.2f}",
                             f"{s.actual:.2f}", f"{s.bias:+.2f}", f"{s.mae:.2f}"])
        head = (f"Gameweek {settled['gameweek']} under model "
                f"{settled['model_version']}, {settled['n']} players graded.")
        if overall:
            head += (f" Overall bias {overall[0].bias:+.2f} (predicted minus actual, "
                     f"per player per game: positive means the model expected too "
                     f"much), MAE {overall[0].mae:.2f} (average size of the miss).")
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


def write_brief(evaluation: Evaluation, root: Path = BRIEF_DIR) -> Path:
    """Render the brief and write it to `logs/gwNN.md`, creating `logs/` if needed.

    The same pattern `settle.draft_learning` follows: the directory is created by the
    first write rather than committed empty, because an empty `logs/` in a fresh clone
    claims a run has happened that has not. `logs/` is not gitignored, so the file is
    tracked once written - which is the point. The reasoning trail is committed.
    """
    path = brief_path(evaluation.gameweek, root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_brief(evaluation))
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
        text = render_brief(evaluation)
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
        path = write_brief(evaluation, args.logs)
        print(f"wrote {path} ({len(triggers)} trigger(s): "
              f"{', '.join(t.name for t in triggers) or 'none'})")
        return EXIT_OK
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())

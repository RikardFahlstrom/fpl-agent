"""The gameweek brief.

`PLAN.md` §5 promised `logs/gwNN.md`, and this is it: everything the warehouse knows
that a person needs before a deadline, rendered as markdown. The human is needed for
exactly two things - choosing to act on a recommendation, and accepting or rejecting a
drafted learning - so the brief leads with the facts that need them and puts the tables
underneath.

Everything is read through a connection opened `mode=ro`, for the same reason `status`
is: a brief that could change the warehouse would be a brief you could not trust to
describe it. Two things are written, both after the page is rendered and never by
`--dry-run`: the markdown file, and one appended Instructions record, through a separate
connection, so the advice that was given can be graded later (`instructions.store`).

`evaluate` reads everything the page prints, once, into an `Evaluation`; `render_brief`
and `render_block` take that value and nothing else - no connection, no clock, no
directory - so the opening block and the working beneath it cannot disagree.

**The brief opens with the same block every run** - the Instructions (starting XI,
captain, vice-captain, bench), then move, ownership, chips, availability, deadline,
learnings, data - each line saying "none" or "not evaluated" when
there is nothing, so the reader looks at the same line every time rather than reading
the page to find out nothing happened (`CONTEXT.md`, *brief*). The sections beneath show
the working.

**Plain English is a rule, not a style.** Nothing from the code - a check level, a slice
id - appears without its meaning beside it on first use; internal names survive only
where a command has to be typed. The owner's own example: a learning that says
"P(start) 75-100% under-projected by 0.54" means nothing on a phone, and "players almost
certain to start scored about half a point more per game than the model expected" is the
same fact. `plain_slice` is where slice names are translated.

    fpl-agent brief                      # write logs/gwNN.md for the latest snapshot
    fpl-agent brief --gameweek 3
    fpl-agent brief --dry-run            # print it and write nothing
"""

import argparse
import logging
import os
import re
import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional
from zoneinfo import ZoneInfo

from .. import config
from . import (chips, held, instructions as instr, lineups, pricing, recommend, settle, status, storage,
               warehouse)
from .projection import HORIZON_GAMEWEEKS, MODEL_VERSION, HorizonMissing

logger = logging.getLogger("fpl_brief")

BRIEF_DIR = Path("logs")

# The bar a move has to clear before the brief calls it worth making.
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
class Evaluation:
    """One pass over the warehouse: everything the brief prints, read once and carried.

    `render_block` and `render_brief` take an Evaluation and nothing else - no connection,
    no clock, no directory - so the opening block and the working beneath it cannot
    disagree, and a layout can be tested from a hand-built value.
    """

    gameweek: int
    now: datetime
    threshold: float
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
    instructions: instr.Instructions      # what to do for the target gameweek
    falling: list[pricing.PriceOutlook]   # held players FPL forecasts to fall
    last_settled: Optional[dict[str, Any]]
    learnings: list[settle.Learning]
    deadlines: dict[int, Optional[datetime]]
    previous: Optional[dict[str, Any]]
    #: Why nothing is advised, when the target gameweek's deadline has passed; else None.
    #: A locked gameweek is never evaluated: the move, the captain and the chips are not
    #: read at all, so a stale ranking cannot be mistaken for advice.
    locked: Optional[str] = None


def plain_slice(name: str) -> str:
    """A calibration slice in the reader's words, or the name itself when unknown."""
    return PLAIN_SLICES.get(name, name)


def worth_making_threshold() -> float:
    """The net-xP bar for a move worth making, from the environment or the default.

    An unreadable override falls back to the default rather than failing the run: a
    brief that dies on a typo in the ini tells the owner nothing at all, which is strictly
    worse than telling them slightly the wrong thing and saying so.
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


def availability_problems(squad: list[dict[str, Any]]) -> list[tuple[dict[str, Any], str]]:
    """Every squad player either source has a question about, with the reason in words.

    A doubt or an unnamed starter is worth a line on the page as well as a player who
    cannot play at all.
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

    The most frequent signal there is and the fastest to become noise, so it is a record
    on the page rather than anything the brief asks the owner to act on.
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


def previous_round(conn: sqlite3.Connection, gameweek: int,
                   holding: Optional[held.HeldSquad]) -> Optional[dict[str, Any]]:
    """Settlement and final Inner standings for the preceding round, never a live rank."""
    if gameweek <= 1:
        return None
    round = warehouse.gameweeks(conn, MODEL_VERSION).get(gameweek - 1)
    result = {"gameweek": round.round, "finished": round.finished,
              "settled": round.finished and (round.graded or round.settled_earlier),
              "league": "The inner: final standings unavailable"}
    if not result["settled"] or holding is None or holding.entry_id is None:
        return result
    if not conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='league_standing'").fetchone():
        return result
    rows = conn.execute(
        """SELECT s.*, l.name FROM league_standing s JOIN league l ON l.id=s.league_id
           WHERE s.gameweek=? AND s.finished=1 AND lower(l.name) IN ('inner','the inner')
           ORDER BY s.rank""", (round.round,)).fetchall()
    own = next((r for r in rows if r["entry_id"] == holding.entry_id), None)
    if own is None or own["rank"] is None:
        return result
    text = f"{own['name']}: "
    old, new = own["last_rank"], own["rank"]
    if old is None or old == 0:
        text += f"rank {new} (previous rank unavailable)"
    else:
        change = old - new
        movement = f"up {change}" if change > 0 else f"down {-change}" if change < 0 else "unchanged"
        text += f"{old} → {new} ({movement})"
    if own["event_points"] is not None:
        text += f"; {own['event_points']} points this round"
    leader = next((r for r in rows if r["rank"] == 1), None)
    if leader and leader["total_points"] is not None and own["total_points"] is not None:
        gap = leader["total_points"] - own["total_points"]
        text += "; leading the league" if new == 1 else f"; {gap} points behind the leader"
        if leader["event_points"] is not None and own["event_points"] is not None and new != 1:
            delta = own["event_points"] - leader["event_points"]
            text += f"; scored {delta:+d} points versus the current leader this round"
    result["league"] = text
    return result


def deadline_label(deadline: Optional[datetime]) -> str:
    if deadline is None:
        return "unknown"
    local = deadline.astimezone(ZoneInfo("Europe/Stockholm"))
    return f"{local.day} {local.strftime('%b %H:%M')}"


def with_deadlines(text: str, evaluation: Evaluation) -> str:
    """Annotate human gameweek references; preserve inline commands and existing dates."""
    def replace(match):
        week = int(match.group(1))
        return f"{match.group(0)} (deadline {deadline_label(evaluation.deadlines.get(week))})"
    return "`".join(
        part if i % 2 else re.sub(
            r"\b(?:GW|[Gg]ameweek )(\d+)\b(?! \(deadline)", replace, part)
        for i, part in enumerate(text.split("`")))


def previous_line(evaluation: Evaluation) -> str:
    past = evaluation.previous
    if past is None:
        return "no previous gameweek"
    text = f"GW{past['gameweek']} "
    if not past["settled"]:
        why = "fixtures not finished" if not past["finished"] else "not graded yet"
        return f"{text}not settled ({why}); league analysis pending"
    return f"{text}settled. {past['league']}"


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


def ranked_transfers(conn: sqlite3.Connection, gameweek: Optional[int] = None,
                     weeks: int = HORIZON_GAMEWEEKS,
                     limit: int = BRIEF_RECOMMENDATIONS) -> dict[str, Any]:
    """The recommender's list for `gameweek`, or the reason there is not one.

    Every way `recommend` legitimately declines - no squad captured, no projected
    horizon, no snapshot, a gameweek other than the one the latest snapshot can price -
    is a sentence the brief should print, not a traceback. The
    reason is carried alongside so the reader is told which command was skipped rather
    than being shown an empty table.
    """
    try:
        return {"moves": recommend.recommend(conn, weeks, limit, gameweek),
                "reason": None}
    except HorizonMissing as e:
        return {"moves": [], "reason": f"the horizon is not projected: {e}"}
    except LookupError as e:
        return {"moves": [], "reason": str(e)}


# --------------------------------------------------------------------------
# Evaluating
# --------------------------------------------------------------------------

def _hours(delta: timedelta) -> str:
    total = delta.total_seconds() / 3600
    if abs(total) >= 48:
        return f"{total / 24:.1f} days"
    return f"{total:.1f}h"


def evaluate(conn: sqlite3.Connection, gameweek: int, *,
             now: Optional[datetime] = None,
             min_net_xp: Optional[float] = None,
             include_token: bool = True,
             learnings_dir: Path = settle.LEARNINGS_DIR) -> Evaluation:
    """Read everything the brief prints, once.

    `learnings_dir` is the one fact that comes from outside the warehouse - where the
    drafted learnings are - taken as an argument so that a test never reads the repo's own.
    """
    now = now or datetime.now(timezone.utc)
    threshold = worth_making_threshold() if min_net_xp is None else float(min_net_xp)

    # `status.gather` is called, never reimplemented: it is the module that owns what "the
    # warehouse disagrees with itself" means, and a second copy of those rules here would
    # drift from it within a month.
    checks = status.gather(conn, include_token=include_token)

    capture = warehouse.latest(conn)
    state = transfer_state(conn)
    deadline = warehouse.deadline(conn, gameweek)
    # The held squad, read once: availability, the captain and the chips all take it.
    holding = held.read(conn, capture) if capture else None
    squad = squad_availability(conn, holding, gameweek) if holding else []
    locked = (warehouse.locked_note(gameweek, deadline, now,
                                    warehouse.last_kickoff(conn, gameweek))
              if deadline is not None and deadline <= now else None)
    listing = ({"moves": [], "reason": locked} if locked
               else ranked_transfers(conn, gameweek))
    ownership, _ = recommend.ownership_source(conn)
    instructions = instr.build(conn, gameweek, MODEL_VERSION, holding, capture=capture,
                               bar=threshold, locked=locked)

    verdicts = list(instructions.verdicts)

    return Evaluation(
        gameweek=gameweek, now=now, threshold=threshold,
        checks=checks,
        capture=capture, squad=squad, state=state,
        deadline=deadline, listing=listing, chips=verdicts,
        ownership=ownership, instructions=instructions,
        falling=(falling_holdings(conn, capture.id, squad, now)
                 if capture and squad else []),
        last_settled=last_settled(conn),
        learnings=settle.read_learnings(learnings_dir),
        deadlines={g.round: warehouse.deadline(conn, g.round)
                   for g in warehouse.gameweeks(conn, MODEL_VERSION).rounds},
        previous=previous_round(conn, gameweek, holding),
        locked=locked)


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
    listing = evaluation.listing
    top = listing["moves"][0] if listing["moves"] else None

    # Ownership - of the move when there is one, of the rivals capture otherwise.
    if top is not None:
        ownership = recommend.move_lines(top)[1].replace("Ownership not shown", "not shown")
    else:
        source = evaluation.ownership
        ownership = (f"rivals captured for gameweek {source.gameweek} "
                     f"({source.managers} rivals); no move to measure"
                     if source.fresh else f"not shown: {source.reason}")

    # Chips: one clause per chip, then the wall.
    if evaluation.locked:
        chip_line = f"not evaluated - gameweek {evaluation.gameweek} is locked"
    else:
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
        when = f"GW{evaluation.gameweek} passed {_hours(-remaining)} ago"
    else:
        when = f"GW{evaluation.gameweek}, {_hours(remaining)} away"
        if remaining > timedelta(hours=warehouse.LINEUPS_FIRM_WITHIN_HOURS):
            when += " (predicted lineups not yet firm)"
    when += "; Stockholm time"
    if state.get("known"):
        free = state["free_transfers"]
        when += ("; free transfers unknown" if free is None else
                 f"; {free} free transfer{'' if free == 1 else 's'} unused")

    # Learnings.
    pending = learnings_line(evaluation.learnings)

    # Data.
    failed = [c.label for c in evaluation.checks if c.failed]
    warned = [c.label for c in evaluation.checks if c.level == status.WARN]
    if failed:
        data = f"NOT trustworthy - {', '.join(failed)} failed; see Warehouse"
    elif warned:
        data = f"trustworthy; stale or pending, not broken: {', '.join(warned)} (see Warehouse)"
    else:
        data = f"trustworthy - all {len(evaluation.checks)} checks agree"

    lines = instr.rows(evaluation.instructions)
    if any(v.play_now and v.state.name == "wildcard" for v in evaluation.chips):
        lines = [(label, text + " - moot if you play the wildcard (see Chips)"
                  if label == "Moves" else text) for label, text in lines]
    rows = [*lines,
            ("Ownership", ownership),
            ("Chips", chip_line),
            ("Availability", availability), ("Deadline", when),
            ("Learnings", pending), ("Data", data),
            ("Previous gameweek", previous_line(evaluation))]
    rows = [(label, with_deadlines(text, evaluation)) for label, text in rows]
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
    now = evaluation.now
    capture = evaluation.capture
    state = evaluation.state
    deadline = evaluation.deadline
    remaining = None if deadline is None else deadline - now
    squad = evaluation.squad
    listing = evaluation.listing
    checks = evaluation.checks

    lines = [f"# Gameweek {gameweek} brief", "", "_All deadline times: Europe/Stockholm._",
             "",
             f"_{now.isoformat(timespec='minutes')} · model {MODEL_VERSION} · "
             + (f"snapshot {capture.id} captured {capture.captured_at}_"
                if capture else "no snapshot captured_"),
             ""]

    # The banner. First thing on the page, because it re-reads everything under it.
    if evaluation.locked:
        lines += [f"> **GAMEWEEK {gameweek} LOCKED.** {evaluation.locked}", ""]
    elif state["chip"]:
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

    # 2. Deadline and budget.
    lines += ["## Deadline and transfers", ""]
    if deadline is None:
        lines.append(f"- No fixtures recorded for gameweek {gameweek}, so no deadline "
                     f"can be derived.")
    else:
        when = deadline.astimezone(ZoneInfo("Europe/Stockholm")).isoformat(timespec="minutes")
        if remaining >= timedelta(0):
            lines.append(f"- Deadline **{when}**, {_hours(remaining)} away.")
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

    # 4. Price watch.
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
                  "fee only returns half of any profit. It is a watch, not a reason to "
                  "move on its own: the most frequent signal in the warehouse and the "
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
        lines += [f"- {v.title.capitalize()}: {v.reason}."
                  + (f" (best projected: GW{v.best.gameweek}, provisional)" if v.best else "")
                  for v in evaluation.chips]
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
                      f"the moves with no chip. Only the next gameweek has predicted "
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
                  f"the nth move of a plan, so the same hit applies to all of them. A move "
                  f"is worth making from a net **{evaluation.threshold:.1f}** xP; "
                  f"anything under that is in this table as a record.", ""]

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
    return with_deadlines("\n".join(lines), evaluation)


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


def record_instructions(db: Path, evaluation: Evaluation) -> str:
    """Append this run's Instructions to the warehouse and say what happened."""
    conn = storage.connect(db)
    try:
        record_id, why = instr.store(
            conn, evaluation.instructions, model_version=MODEL_VERSION,
            snapshot_id=evaluation.capture.id if evaluation.capture else None)
        conn.commit()
    finally:
        conn.close()
    if record_id is None:
        return f"not recorded: {why}"
    return (f"recorded Instructions #{record_id} for GW{evaluation.gameweek} "
            f"(model {MODEL_VERSION})")


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Write the gameweek brief from the warehouse. Read-only.")
    parser.add_argument("--db", type=Path, default=storage.DEFAULT_DB_PATH)
    parser.add_argument("--gameweek", type=int,
                        help="defaults to the latest snapshot's target gameweek")
    parser.add_argument("--logs", type=Path, default=BRIEF_DIR,
                        help=f"where to write gwNN.md (default {BRIEF_DIR})")
    parser.add_argument("--dry-run", action="store_true",
                        help="print the brief; write nothing")
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
        try:
            gameweek = warehouse.require_target(conn, args.gameweek).gameweek
        except LookupError as e:
            print(e, file=sys.stderr)
            return EXIT_UNREADABLE
        evaluation = evaluate(conn, gameweek)
        if args.dry_run:
            print(render_brief(evaluation))
            return EXIT_OK
        print(instr.render(evaluation.instructions))
        print(f"\nwrote {write_brief(evaluation, args.logs)}")
        print(record_instructions(args.db, evaluation))
        return EXIT_OK
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())

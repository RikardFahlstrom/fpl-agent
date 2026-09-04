"""Push the brief's triggers to a phone, once each.

`brief.evaluate` decides *what* is worth interrupting someone for. This decides *whether
it has already been said*, and puts it on the wire. That split is the whole design: the
judgement lives in one module with no network in it, and the transport lives here with no
judgement in it.

The channel is **ntfy** (https://ntfy.sh), chosen by the owner because it needs no
account and no new dependency - a message is an HTTP POST whose body is the text, and
`httpx` is already in the tree. Self-hosting is a change of `FPL_NTFY_SERVER` and nothing
else.

Two things about this module are load-bearing, and both are about not lying:

**Deduplication.** The `deadline` job runs hourly. Every fact the brief can fire on is
re-evaluated dozens of times before it changes, so without dedupe the owner gets the same
push twenty times before one deadline, turns notifications off, and every later
notification - including the one about a player who cannot play - is worth nothing. So
each trigger carries a fingerprint built from identity alone (see `brief.Trigger`), the
fingerprints already delivered live in the `notification` table, and a trigger whose
fingerprint is there is dropped without a word to the server.

**Record after, never before.** A fingerprint is written only once the server has
accepted the message. Writing it first would mean a failed POST retires a notification
that was never delivered: the owner is never told, and every log line still says "sent".
That is the bug class CLAUDE.md opens with, in its purest form, and it is one line's
worth of ordering in `send_pending`. `tests/test_notify.py` asserts the ordering directly
- it fails, not merely reports differently, if the two statements are ever swapped.

Unlike `status` and `brief`, this command **writes** to the warehouse, so it cannot use
`status.connect_readonly`. It still refuses to create one: `storage.connect` would happily
migrate a fresh empty file into place and then report a clean, silent run against it.

The topic is a credential. Anyone who knows an ntfy topic can read every message posted
to it, so it is mapped into `config.SECRET_ENV`, masked in the ini loader's log line, and
never printed here - `Target.describe` gives the server host and a redacted topic, and
that is the most any output of this module will say.
"""

import argparse
import logging
import os
import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from email.header import Header
from pathlib import Path
from typing import Callable, Optional

from .. import config
from . import brief, storage
from .brief import Trigger

logger = logging.getLogger("fpl_notify")

# ntfy's public instance. Overridable because someone may self-host later, and because
# every test in this project points it at a local stub instead.
DEFAULT_SERVER = "https://ntfy.sh"
SERVER_ENV = "FPL_NTFY_SERVER"

# The topic is the address *and* the password: ntfy has no accounts, so anyone who knows
# the topic can subscribe to it and read every message. Hence a long random string, and
# hence this name in config.SECRET_ENV.
TOPIC_ENV = "FPL_NTFY_TOPIC"

CHANNEL = "ntfy"

# How long to wait on the POST. Short: this runs from cron behind a lock that a slow
# request would hold, and a notification that arrives ten minutes late has already been
# overtaken by the next hourly run.
TIMEOUT_SECONDS = 10.0

# ntfy's priority scale is 1 (min) to 5 (max); 3 is default. The three triggers that mean
# points are being lost right now get 4, which is the level that breaks through a phone's
# quiet hours on most setups. `move_worth_making` is a standing recommendation with no
# clock on it, so it arrives at 3 and waits to be noticed.
PRIORITY = {
    "status_failed": "4",
    "squad_player_unavailable": "4",
    "deadline_with_move": "4",
    "move_worth_making": "3",
}
DEFAULT_PRIORITY = "3"

# Emoji shortcodes; ntfy renders these as the notification's icon.
TAGS = {
    "status_failed": "rotating_light",
    "squad_player_unavailable": "hospital",
    "deadline_with_move": "alarm_clock",
    "move_worth_making": "chart_with_upwards_trend",
}
DEFAULT_TAGS = "soccer"

EXIT_OK = 0
# Shared with `snapshot` and `status`, and meaning the same thing in all three: the thing
# this command needs was not configured or could not be read at all.
EXIT_UNREADABLE = 2
# notify's own. 1-7 belong to other commands; see the table in docs/SCHEDULING.md.
EXIT_SEND_FAILED = 8


class SendFailed(RuntimeError):
    """A message was not accepted by the server. The fingerprint stays unrecorded."""


@dataclass(frozen=True)
class Target:
    """Where messages go. Constructed from the environment, never printed whole."""

    server: str
    topic: str

    @property
    def url(self) -> str:
        return f"{self.server.rstrip('/')}/{self.topic}"

    def describe(self) -> str:
        """The most that may appear in a log line: the host, and a redacted topic.

        The topic is the credential, so only its length and a two-character prefix
        survive - enough to tell two configured topics apart when debugging, not enough
        to subscribe to either. A short topic is redacted entirely, because two
        characters of a four-character topic is most of it.
        """
        shown = f"{self.topic[:2]}***" if len(self.topic) >= 8 else "***"
        return f"{self.server.rstrip('/')} topic {shown} ({len(self.topic)} chars)"


def target_from_env(env: Optional[dict[str, str]] = None) -> Optional[Target]:
    """The configured target, or None if notifications are not set up.

    Not an error to be unconfigured. `notify` is opt-in, and a host that has never set a
    topic should be told how to set one rather than mailed a failure every hour.
    """
    env = os.environ if env is None else env
    topic = (env.get(TOPIC_ENV) or "").strip()
    if not topic:
        return None
    server = (env.get(SERVER_ENV) or "").strip() or DEFAULT_SERVER
    return Target(server=server, topic=topic)


# --------------------------------------------------------------------------
# The message
# --------------------------------------------------------------------------

def build_message(trigger: Trigger) -> str:
    """The body of one push. It always ends with the action.

    The review named the risk in one line - notification spam erodes trust - and named
    the cure in the next: every message ends with the one thing wanted from the human.
    `Trigger` refuses to be constructed without an action for that reason, and this
    checks again rather than trusting it, because the value of the rule is that it has no
    exceptions. A message whose last line is a fact rather than a request is a message
    that trains the reader to skim.
    """
    action = (trigger.action or "").strip()
    if not action:
        raise ValueError(
            f"trigger {trigger.name!r} has no action; a message that does not end in "
            f"what the reader should do is not worth sending")
    detail = (trigger.detail or "").strip()
    body = f"{detail}\n\n→ {action}" if detail else f"→ {action}"
    return body


def _header_value(text: str) -> str:
    """An HTTP header value that survives a non-ASCII player name.

    Headers are latin-1 on the wire and httpx refuses to encode anything else, so a
    headline containing "Ødegaard" - or the "…" `brief.headline` appends when it
    truncates - would raise before the request left the process, and the notification
    would be lost to a name. ntfy reads RFC 2047 encoded words in `Title`, so anything
    outside ASCII goes out as `=?utf-8?b?...?=` and arrives intact.
    """
    text = " ".join(str(text).split())
    try:
        text.encode("ascii")
    except UnicodeEncodeError:
        return Header(text, "utf-8").encode()
    return text


def message_headers(trigger: Trigger) -> dict[str, str]:
    """Title, Priority and Tags for one trigger. ntfy reads all three off the POST."""
    return {
        "Title": _header_value(trigger.headline),
        "Priority": PRIORITY.get(trigger.name, DEFAULT_PRIORITY),
        "Tags": TAGS.get(trigger.name, DEFAULT_TAGS),
    }


# --------------------------------------------------------------------------
# The wire
# --------------------------------------------------------------------------

def post_ntfy(target: Target, trigger: Trigger, *,
              timeout: float = TIMEOUT_SECONDS) -> int:
    """POST one message to ntfy. Returns the status code, raises `SendFailed` otherwise.

    Anything that is not a 2xx is a failure, including a redirect and including a
    connection that never opened. `httpx` is imported here rather than at module scope so
    the rest of this module - the dedupe, the message builder, the exit codes - can be
    imported and tested without a HTTP library present.
    """
    import httpx

    body = build_message(trigger).encode("utf-8")
    try:
        response = httpx.post(target.url, content=body,
                              headers=message_headers(trigger), timeout=timeout)
    except Exception as e:                      # httpx raises a family, not one class
        raise SendFailed(f"{type(e).__name__}: {e}") from e
    if not 200 <= response.status_code < 300:
        raise SendFailed(f"HTTP {response.status_code}: "
                         f"{response.text.strip()[:200] or 'no body'}")
    return response.status_code


# --------------------------------------------------------------------------
# Deciding what to send, and sending it
# --------------------------------------------------------------------------

@dataclass
class Outcome:
    """What one run did, in the terms the exit code and the printout are built from."""

    sent: list[Trigger]
    suppressed: list[Trigger]           # fingerprint already in the warehouse
    failed: list[tuple[Trigger, str]]   # trigger, why


def pending(conn: sqlite3.Connection,
            triggers: list[Trigger]) -> tuple[list[Trigger], list[Trigger]]:
    """Split fired triggers into (never sent, already sent).

    Order is preserved, and duplicates *within* one evaluation collapse too: two triggers
    cannot legitimately share a fingerprint, but if they ever did, one push is the right
    answer and two is the failure mode this module exists to prevent.
    """
    already = storage.sent_fingerprints(conn, [t.fingerprint for t in triggers])
    seen: set[str] = set()
    new: list[Trigger] = []
    suppressed: list[Trigger] = []
    for trigger in triggers:
        if trigger.fingerprint in already or trigger.fingerprint in seen:
            suppressed.append(trigger)
        else:
            seen.add(trigger.fingerprint)
            new.append(trigger)
    return new, suppressed


def send_pending(conn: sqlite3.Connection, triggers: list[Trigger], target: Target, *,
                 gameweek: Optional[int] = None,
                 post: Optional[Callable[[Target, Trigger], object]] = None) -> Outcome:
    """Send everything not sent before, and record only what the server accepted.

    The two statements in the loop are in the order they are in for the reason the module
    docstring gives, and swapping them is the single worst change that could be made to
    this file: a failed POST would retire the notification forever while every log line
    kept saying "sent". `record_notification` is called *after* `post` returns, and the
    commit is after that, so a crash between them costs a duplicate push - which the next
    run's dedupe absorbs - rather than a lost one.

    One failure does not stop the others. If the server is down they will all fail
    anyway; if a single message is the problem, the remaining facts are still worth
    telling someone about. Every failure is returned, and the caller exits 8.
    """
    post = post or post_ntfy
    outcome = Outcome(sent=[], suppressed=[], failed=[])
    new, outcome.suppressed = pending(conn, triggers)

    for trigger in new:
        try:
            post(target, trigger)
        except SendFailed as e:
            logger.error("NOT SENT %s (%s): %s", trigger.name, trigger.fingerprint, e)
            outcome.failed.append((trigger, str(e)))
            continue
        except Exception as e:      # an injected sender, or a bug in one
            logger.error("NOT SENT %s (%s): %s: %s",
                         trigger.name, trigger.fingerprint, type(e).__name__, e)
            outcome.failed.append((trigger, f"{type(e).__name__}: {e}"))
            continue
        # Only now. The server has the message.
        storage.record_notification(conn, trigger.fingerprint, trigger.name,
                                    trigger.headline, CHANNEL, gameweek)
        conn.commit()
        outcome.sent.append(trigger)
    return outcome


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def _report(evaluation, outcome: Optional[Outcome], suppressed: list[Trigger],
            stream=None) -> None:
    """Say what was evaluated, always - including when nothing was sent.

    Silence is a correct outcome and it is also what a broken notifier looks like, so a
    run that sends nothing still names all four triggers and, for each silent one, the
    condition that stopped it. "0 sent" on its own is the report this project has been
    bitten by twice.

    `stream` is resolved on the call rather than bound as a default, so a caller that has
    redirected stdout sees the report rather than the terminal this module was imported
    into.
    """
    stream = sys.stdout if stream is None else stream
    fired = len(evaluation.triggers)
    print(f"gameweek {evaluation.gameweek}: evaluated "
          f"{len(brief.TRIGGER_NAMES)} triggers, {fired} fired", file=stream)
    for name in brief.TRIGGER_NAMES:
        if name in evaluation.silent:
            print(f"  silent   {name}: {evaluation.silent[name]}", file=stream)
    for trigger in suppressed:
        print(f"  already  {trigger.name}: {trigger.fingerprint}", file=stream)
    if outcome is not None:
        for trigger in outcome.sent:
            print(f"  SENT     {trigger.name}: {trigger.fingerprint}", file=stream)
        for trigger, why in outcome.failed:
            print(f"  FAILED   {trigger.name}: {trigger.fingerprint}: {why}",
                  file=stream)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Push the brief's triggers to ntfy, once each.")
    parser.add_argument("--db", type=Path, default=storage.DEFAULT_DB_PATH)
    parser.add_argument("--gameweek", type=int,
                        help="defaults to the latest snapshot's target gameweek")
    parser.add_argument("--dry-run", action="store_true",
                        help="print what would be sent and what is suppressed as "
                             "already sent; send nothing and record nothing")
    args = parser.parse_args(argv)

    config.load()
    logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stderr)
    # httpx logs every request at INFO, and the request URL is the server *and the
    # topic*. That single line would put the credential in every cron mail and every log
    # file, which is the one thing this module promises not to do. Found by running it
    # against a local stub and reading the output, not by reading the code.
    logging.getLogger("httpx").setLevel(logging.WARNING)

    target = target_from_env()
    if target is None and not args.dry_run:
        print(f"notifications are not configured: set {TOPIC_ENV} (or [notify] "
              f"ntfy_topic in fpl-agent.ini) to a long random string. "
              f"See docs/SCHEDULING.md.", file=sys.stderr)
        return EXIT_UNREADABLE

    # Writes, so not `status.connect_readonly` - but `storage.connect` creates and
    # migrates a missing file, which would turn "nothing has ever been captured" into a
    # clean empty warehouse and a confident silent run against it. Refuse first.
    if not args.db.exists():
        print(f"no warehouse at {args.db} - nothing has ever been captured. "
              f"Run `make snapshot`.", file=sys.stderr)
        return EXIT_UNREADABLE
    try:
        conn = storage.connect(args.db)
    except sqlite3.Error as e:
        print(f"could not open {args.db}: {e}", file=sys.stderr)
        return EXIT_UNREADABLE

    try:
        gameweek = args.gameweek
        if gameweek is None:
            gameweek = brief.default_gameweek(conn)
        if gameweek is None:
            print("no snapshot carries a target gameweek, and none was given; "
                  "pass --gameweek or run `make snapshot`.", file=sys.stderr)
            return EXIT_UNREADABLE

        evaluation = brief.evaluate(conn, gameweek)
        where = target.describe() if target else "no target configured (dry run)"
        print(f"{datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC  ntfy: {where}")

        if args.dry_run:
            new, suppressed = pending(conn, evaluation.triggers)
            _report(evaluation, None, suppressed)
            for trigger in new:
                headers = message_headers(trigger)
                print(f"  WOULD SEND {trigger.name}: {trigger.fingerprint}\n"
                      f"    Title: {headers['Title']}  "
                      f"Priority: {headers['Priority']}  Tags: {headers['Tags']}")
                for line in build_message(trigger).splitlines():
                    print(f"    | {line}")
            print(f"dry run: {len(new)} would be sent, "
                  f"{len(suppressed)} suppressed as already sent, nothing recorded")
            return EXIT_OK

        outcome = send_pending(conn, evaluation.triggers, target, gameweek=gameweek)
        _report(evaluation, outcome, outcome.suppressed)
        print(f"{len(outcome.sent)} sent, {len(outcome.suppressed)} already sent, "
              f"{len(outcome.failed)} failed")
        if outcome.failed:
            # Loud, but the caller decides what it costs. A failed push must never be
            # what makes a `daily` job look like it lost the snapshot it did capture.
            print(f"a send failed; exiting {EXIT_SEND_FAILED}. The fingerprints above "
                  f"were NOT recorded, so the next run will try them again.",
                  file=sys.stderr)
            return EXIT_SEND_FAILED
        return EXIT_OK
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())

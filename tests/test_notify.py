"""The notifier: dedupe, ordering, and the message contract.

Offline, twice over. Every warehouse here is in memory or in a temporary directory, and
the only HTTP server any of these tests talk to is a stub bound to 127.0.0.1 on a port
the kernel picks. **Nothing in this file may reach ntfy.sh.** A test that posted to a
real topic would either leak the owner's messages onto a public one or invent a topic
somebody else is subscribed to.

The test that matters most is `test_the_fingerprint_is_recorded_only_after_the_send`.
Recording before sending is the single change to `notify` that would be invisible in
every log - the run would report "sent" and the owner would never hear about the player
who cannot play - so it is asserted directly, from inside the POST, rather than inferred
from the happy path.
"""

import datetime as dt
import http.server
import os
import socket
import tempfile
import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest import mock

from fpl_agent import config
from fpl_agent.engine import brief, notify, storage
from fpl_agent.engine.brief import Trigger

from test_brief import GAMEWEEK, Warehouse

TARGET = notify.Target(server="http://127.0.0.1:1", topic="a-long-random-topic")


def kickoff_in(hours):
    """A kickoff `hours` from now, in the ISO form the `fixture` table stores.

    `Warehouse`'s own default is a pinned date, which is right for `test_brief` - it
    freezes the clock with `NOW` and asserts on literal deadline strings. It is wrong
    here, because these tests run the CLI, and `notify.main` calls `brief.evaluate`
    without a `now` to inject. So the deadline is placed relative to the real clock
    instead: pinned, it sat at 2026-09-05T07:30 UTC, and every test below that needs a
    trigger to fire passed before that instant and failed after it.
    """
    when = dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=hours)
    return when.strftime("%Y-%m-%dT%H:%M:%SZ")


def trigger(name="move_worth_making", fingerprint="move_worth_making:gw3:1->2",
            headline="Swap P1 for P2", detail="+3.0 xP over 3 gameweeks",
            action="Make the transfer before 07:30 Saturday"):
    return Trigger(name=name, headline=headline, detail=detail, action=action,
                   fingerprint=fingerprint)


class Recorder:
    """A stand-in for `post_ntfy` that remembers what it was asked to send.

    `before` is what the warehouse knew at the moment of the POST, which is how the
    ordering test can tell "recorded after" from "recorded at some point".
    """

    def __init__(self, conn=None, fail=None):
        self.conn = conn
        self.fail = fail or set()
        self.sent = []
        self.recorded_at_post_time = []

    def __call__(self, target, trig):
        self.sent.append((target, trig))
        if self.conn is not None:
            self.recorded_at_post_time.append(
                storage.sent_fingerprints(self.conn, [trig.fingerprint]))
        if trig.name in self.fail or trig.fingerprint in self.fail:
            raise notify.SendFailed("stub refused it")
        return 200


class MessageTest(unittest.TestCase):
    """What goes on the wire, and the one rule about it that has no exceptions."""

    def test_every_message_ends_with_the_action(self):
        body = notify.build_message(trigger())
        self.assertTrue(body.rstrip().endswith("Make the transfer before 07:30 Saturday"),
                        body)
        self.assertIn("+3.0 xP over 3 gameweeks", body)

    def test_a_message_with_no_action_is_refused_not_shortened(self):
        # Trigger itself refuses this, so the only way to reach the builder with an
        # empty action is to bypass the constructor - which is what a future caller
        # building a message from somewhere other than `brief` would do.
        empty = Trigger.__new__(Trigger)
        object.__setattr__(empty, "name", "move_worth_making")
        object.__setattr__(empty, "headline", "h")
        object.__setattr__(empty, "detail", "d")
        object.__setattr__(empty, "action", "   ")
        object.__setattr__(empty, "fingerprint", "f")
        with self.assertRaises(ValueError):
            notify.build_message(empty)

    def test_an_action_with_no_detail_is_still_a_message(self):
        body = notify.build_message(trigger(detail=""))
        self.assertEqual(body, "→ Make the transfer before 07:30 Saturday")

    def test_urgent_triggers_carry_a_higher_priority_than_a_standing_suggestion(self):
        urgent = notify.message_headers(trigger(name="squad_player_unavailable"))
        standing = notify.message_headers(trigger(name="move_worth_making"))
        self.assertGreater(int(urgent["Priority"]), int(standing["Priority"]))
        self.assertEqual(urgent["Title"], "Swap P1 for P2")

    def test_a_non_ascii_headline_survives_as_an_encoded_word(self):
        # "Ødegaard" and the "…" brief.headline appends when it truncates are both
        # outside latin-1's useful range; httpx would raise before the POST left the
        # process and the notification would be lost to a player's name.
        headers = notify.message_headers(trigger(headline="Ødegaard cannot play…"))
        headers["Title"].encode("ascii")            # must not raise
        self.assertTrue(headers["Title"].startswith("=?utf-8?"), headers["Title"])

    def test_an_ascii_headline_is_left_alone(self):
        self.assertEqual(notify.message_headers(trigger())["Title"], "Swap P1 for P2")


class TargetTest(unittest.TestCase):
    """The topic is a credential, so the only question is what may be printed."""

    def test_describe_never_contains_the_topic(self):
        target = notify.Target(server="https://ntfy.sh", topic="fpl-8Kq2vN7xLp0aZ")
        described = target.describe()
        self.assertNotIn("fpl-8Kq2vN7xLp0aZ", described)
        self.assertNotIn("8Kq2vN7xLp0aZ", described)
        self.assertIn("https://ntfy.sh", described)
        self.assertIn(f"{len('fpl-8Kq2vN7xLp0aZ')} chars", described)

    def test_a_short_topic_is_redacted_entirely(self):
        self.assertNotIn("fpl", notify.Target(server="s", topic="fpl").describe())

    def test_unconfigured_is_none_rather_than_an_error(self):
        self.assertIsNone(notify.target_from_env({}))
        self.assertIsNone(notify.target_from_env({notify.TOPIC_ENV: "   "}))

    def test_the_server_defaults_and_the_url_is_the_two_joined(self):
        target = notify.target_from_env({notify.TOPIC_ENV: "secret-topic"})
        self.assertEqual(target.server, notify.DEFAULT_SERVER)
        self.assertEqual(target.url, "https://ntfy.sh/secret-topic")
        self_hosted = notify.target_from_env(
            {notify.TOPIC_ENV: "t", notify.SERVER_ENV: "http://box.local:8080/"})
        self.assertEqual(self_hosted.url, "http://box.local:8080/t")


class NotifyTestCase(unittest.TestCase):
    """A warehouse the brief can read, and an environment that is never the real one."""

    def setUp(self):
        self.conn = storage.connect(":memory:")
        self.warehouse = Warehouse(self.conn)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(self.conn.close)
        patch = mock.patch.dict(
            os.environ,
            {"FPL_TOKEN_CACHE": str(Path(self.tmp.name) / "token.json"),
             "FPL_RIVAL_LEAGUES": "",
             notify.TOPIC_ENV: "a-long-random-topic",
             notify.SERVER_ENV: "http://127.0.0.1:1"},
        )
        patch.start()
        self.addCleanup(patch.stop)

    def rows(self):
        return self.conn.execute(
            "SELECT * FROM notification ORDER BY fingerprint").fetchall()

    def fingerprints(self):
        return [r["fingerprint"] for r in self.rows()]


class DedupeTest(NotifyTestCase):
    """The reason this module exists. The `deadline` job runs hourly."""

    def test_a_fingerprint_already_sent_is_suppressed(self):
        storage.record_notification(self.conn, "move_worth_making:gw3:1->2",
                                    "move_worth_making", "h", "ntfy", GAMEWEEK)
        new, suppressed = notify.pending(self.conn, [trigger()])
        self.assertEqual(new, [])
        self.assertEqual([t.fingerprint for t in suppressed],
                         ["move_worth_making:gw3:1->2"])

    def test_a_different_fingerprint_is_news_even_for_the_same_trigger(self):
        storage.record_notification(self.conn, "move_worth_making:gw3:1->2",
                                    "move_worth_making", "h", "ntfy", GAMEWEEK)
        new, _ = notify.pending(
            self.conn, [trigger(fingerprint="move_worth_making:gw3:1->9")])
        self.assertEqual(len(new), 1)

    def test_two_triggers_sharing_a_fingerprint_are_sent_once(self):
        new, suppressed = notify.pending(self.conn, [trigger(), trigger()])
        self.assertEqual(len(new), 1)
        self.assertEqual(len(suppressed), 1)

    def test_the_second_hourly_run_sends_nothing(self):
        triggers = [trigger()]
        first = Recorder()
        notify.send_pending(self.conn, triggers, TARGET, gameweek=GAMEWEEK, post=first)
        second = Recorder()
        outcome = notify.send_pending(self.conn, triggers, TARGET, gameweek=GAMEWEEK,
                                      post=second)
        self.assertEqual(len(first.sent), 1)
        self.assertEqual(second.sent, [])
        self.assertEqual(len(outcome.suppressed), 1)
        self.assertEqual(outcome.sent, [])

    def test_a_send_records_what_it_sent_so_it_can_be_read_back(self):
        notify.send_pending(self.conn, [trigger()], TARGET, gameweek=GAMEWEEK,
                            post=Recorder())
        row = self.rows()[0]
        self.assertEqual(row["trigger_name"], "move_worth_making")
        self.assertEqual(row["headline"], "Swap P1 for P2")
        self.assertEqual(row["gameweek"], GAMEWEEK)
        self.assertEqual(row["channel"], "ntfy")
        self.assertTrue(row["sent_at"])


class OrderingTest(NotifyTestCase):
    """Record after the send, never before. This is the whole component.

    If the two statements in `send_pending` are swapped, the first test here fails on
    the assertion inside the POST and the second and third fail on a row that should not
    exist. Nothing about the happy path changes, which is exactly why this is asserted
    from the inside.
    """

    def test_the_fingerprint_is_recorded_only_after_the_send(self):
        recorder = Recorder(conn=self.conn)
        notify.send_pending(self.conn, [trigger()], TARGET, gameweek=GAMEWEEK,
                            post=recorder)
        self.assertEqual(recorder.recorded_at_post_time, [set()],
                         "the fingerprint was already in the warehouse when the POST "
                         "was made; a failed send would then be lost forever")
        self.assertEqual(self.fingerprints(), ["move_worth_making:gw3:1->2"])

    def test_a_failed_send_records_nothing(self):
        recorder = Recorder(fail={"move_worth_making"})
        outcome = notify.send_pending(self.conn, [trigger()], TARGET, gameweek=GAMEWEEK,
                                      post=recorder)
        self.assertEqual(self.fingerprints(), [])
        self.assertEqual(outcome.sent, [])
        self.assertEqual(len(outcome.failed), 1)

    def test_a_notification_that_failed_is_retried_and_arrives(self):
        # The point of not recording: the fact is still news on the next hourly run.
        notify.send_pending(self.conn, [trigger()], TARGET, gameweek=GAMEWEEK,
                            post=Recorder(fail={"move_worth_making"}))
        good = Recorder()
        outcome = notify.send_pending(self.conn, [trigger()], TARGET, gameweek=GAMEWEEK,
                                      post=good)
        self.assertEqual(len(good.sent), 1)
        self.assertEqual(len(outcome.sent), 1)
        self.assertEqual(self.fingerprints(), ["move_worth_making:gw3:1->2"])

    def test_one_failure_does_not_stop_the_others(self):
        triggers = [trigger(name="status_failed", fingerprint="status_failed:gw3:a"),
                    trigger(fingerprint="move_worth_making:gw3:1->2")]
        recorder = Recorder(fail={"status_failed"})
        outcome = notify.send_pending(self.conn, triggers, TARGET, gameweek=GAMEWEEK,
                                      post=recorder)
        self.assertEqual([t.name for t in outcome.sent], ["move_worth_making"])
        self.assertEqual(self.fingerprints(), ["move_worth_making:gw3:1->2"])

    def test_an_unexpected_exception_from_the_sender_is_a_failure_not_a_crash(self):
        def explode(target, trig):
            raise RuntimeError("something nobody predicted")

        outcome = notify.send_pending(self.conn, [trigger()], TARGET, post=explode)
        self.assertEqual(len(outcome.failed), 1)
        self.assertEqual(self.fingerprints(), [])


# --------------------------------------------------------------------------
# A real HTTP stub. Loopback, ephemeral port, never ntfy.sh.
# --------------------------------------------------------------------------

class StubNtfy:
    """Records real POSTs so the wire format is checked against a socket, not a mock."""

    def __init__(self, status=200):
        self.requests = []
        stub = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                stub.requests.append({
                    "path": self.path,
                    "body": self.rfile.read(length).decode("utf-8"),
                    "headers": {k.lower(): v for k, v in self.headers.items()},
                })
                self.send_response(status)
                self.end_headers()
                self.wfile.write(b"{}")

            def log_message(self, *args):
                pass

        self.server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self):
        return f"http://127.0.0.1:{self.server.server_port}"

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


class WireTest(unittest.TestCase):
    """What ntfy actually receives."""

    def setUp(self):
        self.stub = StubNtfy()
        self.addCleanup(self.stub.close)

    def test_a_post_carries_the_topic_the_body_and_the_headers(self):
        target = notify.Target(server=self.stub.url, topic="a-long-random-topic")
        self.assertEqual(notify.post_ntfy(target, trigger()), 200)
        request = self.stub.requests[0]
        self.assertEqual(request["path"], "/a-long-random-topic")
        self.assertTrue(request["body"].rstrip().endswith(
            "Make the transfer before 07:30 Saturday"))
        self.assertEqual(request["headers"]["title"], "Swap P1 for P2")
        self.assertEqual(request["headers"]["priority"], "3")
        self.assertEqual(request["headers"]["tags"], "chart_with_upwards_trend")

    def test_a_server_error_raises_rather_than_returning_quietly(self):
        failing = StubNtfy(status=500)
        self.addCleanup(failing.close)
        target = notify.Target(server=failing.url, topic="t")
        with self.assertRaises(notify.SendFailed):
            notify.post_ntfy(target, trigger())

    def test_a_refused_connection_is_a_send_failure(self):
        # Bind a socket, learn the port, close it: nothing is listening there now.
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()
        target = notify.Target(server=f"http://127.0.0.1:{port}", topic="t")
        with self.assertRaises(notify.SendFailed):
            notify.post_ntfy(target, trigger(), timeout=2.0)


class EndToEndTest(unittest.TestCase):
    """`send_pending` over a real socket, twice, with a real warehouse file."""

    def setUp(self):
        self.stub = StubNtfy()
        self.addCleanup(self.stub.close)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / "fpl.db"
        self.conn = storage.connect(self.db)
        self.addCleanup(self.conn.close)
        self.target = notify.Target(server=self.stub.url, topic="a-long-random-topic")

    def test_sent_once_then_never_again(self):
        triggers = [trigger()]
        first = notify.send_pending(self.conn, triggers, self.target, gameweek=GAMEWEEK)
        self.assertEqual(len(first.sent), 1)
        self.assertEqual(len(self.stub.requests), 1)

        second = notify.send_pending(self.conn, triggers, self.target, gameweek=GAMEWEEK)
        self.assertEqual(second.sent, [])
        self.assertEqual(len(second.suppressed), 1)
        self.assertEqual(len(self.stub.requests), 1, "the second run posted again")

    def test_a_500_leaves_the_fingerprint_unrecorded_and_the_next_run_sends_it(self):
        failing = StubNtfy(status=500)
        self.addCleanup(failing.close)
        bad = notify.Target(server=failing.url, topic="a-long-random-topic")

        outcome = notify.send_pending(self.conn, [trigger()], bad, gameweek=GAMEWEEK)
        self.assertEqual(len(outcome.failed), 1)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM notification").fetchone()[0], 0)

        outcome = notify.send_pending(self.conn, [trigger()], self.target,
                                      gameweek=GAMEWEEK)
        self.assertEqual(len(outcome.sent), 1)
        self.assertEqual(len(self.stub.requests), 1)


# --------------------------------------------------------------------------
# The command
# --------------------------------------------------------------------------

class CommandTest(unittest.TestCase):
    """`fpl-agent notify` end to end, against a warehouse on disk and a local stub."""

    def setUp(self):
        self.stub = StubNtfy()
        self.addCleanup(self.stub.close)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / "fpl.db"
        conn = storage.connect(self.db)
        # The deadline is derived as 90 minutes before the first kickoff, so a kickoff
        # 13.5 hours out puts it 12 hours away: unpassed, and inside the 24-hour window
        # `deadline_with_move` watches. Both deadline triggers need that to fire at all.
        Warehouse(conn).healthy().fixtures(kickoff=kickoff_in(13.5))
        conn.commit()
        conn.close()
        patch = mock.patch.dict(
            os.environ,
            {"FPL_TOKEN_CACHE": str(Path(self.tmp.name) / "token.json"),
             "FPL_RIVAL_LEAGUES": "",
             notify.TOPIC_ENV: "a-long-random-topic",
             notify.SERVER_ENV: self.stub.url},
        )
        patch.start()
        self.addCleanup(patch.stop)

    def run_notify(self, *argv):
        out, err = StringIO(), StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = notify.main(["--db", str(self.db), *argv])
        return code, out.getvalue(), err.getvalue()

    def recorded(self):
        conn = storage.connect(self.db)
        try:
            return [r["fingerprint"] for r in
                    conn.execute("SELECT fingerprint FROM notification")]
        finally:
            conn.close()

    def test_a_dry_run_sends_nothing_and_records_nothing(self):
        code, out, _ = self.run_notify("--gameweek", str(GAMEWEEK), "--dry-run")
        self.assertEqual(code, notify.EXIT_OK)
        self.assertEqual(self.stub.requests, [])
        self.assertEqual(self.recorded(), [])
        self.assertIn("nothing recorded", out)

    def test_a_dry_run_does_not_write_to_the_warehouse_at_all(self):
        """Not one byte, on a warehouse that is already up to date.

        This one passes even against a dry run that opens read-write, because the schema
        is already migrated here and there is nothing left to write. It is the sibling
        below, on a warehouse predating the notification table, that actually fails when
        the connection is not read-only. Both are kept: this guards the day a dry run
        starts recording, that one guards the day it starts migrating.
        """
        before = self.db.read_bytes()
        code, out, _ = self.run_notify("--gameweek", str(GAMEWEEK), "--dry-run")
        self.assertEqual(code, notify.EXIT_OK)
        self.assertIn("nothing recorded", out)
        self.assertEqual(self.db.read_bytes(), before,
                         "a dry run modified the warehouse")

    def test_a_dry_run_works_on_a_warehouse_predating_the_notifier(self):
        """Read-only means the notification table may genuinely not be there yet."""
        conn = storage.connect(self.db)
        conn.execute("DROP TABLE notification")
        conn.commit()
        conn.close()
        before = self.db.read_bytes()

        code, out, _ = self.run_notify("--gameweek", str(GAMEWEEK), "--dry-run")

        self.assertEqual(code, notify.EXIT_OK)
        self.assertIn("would be sent", out)
        self.assertEqual(self.db.read_bytes(), before,
                         "a dry run created the table it was only meant to read")

    def test_a_dry_run_names_what_is_being_suppressed_as_already_sent(self):
        code, _, _ = self.run_notify("--gameweek", str(GAMEWEEK))
        self.assertEqual(code, notify.EXIT_OK)
        already = self.recorded()
        self.assertTrue(already, "the fixture fires nothing; the CLI tests need it to")

        code, out, _ = self.run_notify("--gameweek", str(GAMEWEEK), "--dry-run")
        self.assertEqual(code, notify.EXIT_OK)
        for fingerprint in already:
            self.assertIn("already ", out)
            self.assertIn(fingerprint, out)
        self.assertIn("suppressed as already sent", out)

    def test_a_real_run_posts_and_the_next_one_does_not(self):
        code, out, _ = self.run_notify("--gameweek", str(GAMEWEEK))
        self.assertEqual(code, notify.EXIT_OK)
        posted = len(self.stub.requests)
        self.assertGreater(posted, 0)
        self.assertIn("SENT", out)

        code, out, _ = self.run_notify("--gameweek", str(GAMEWEEK))
        self.assertEqual(code, notify.EXIT_OK)
        self.assertEqual(len(self.stub.requests), posted)
        self.assertNotIn("SENT", out)

    def test_the_output_never_carries_the_topic(self):
        _, out, err = self.run_notify("--gameweek", str(GAMEWEEK))
        self.assertNotIn("a-long-random-topic", out + err)
        self.assertIn("ntfy:", out)

    def test_httpx_does_not_log_the_url_and_therefore_the_topic(self):
        # httpx logs "HTTP Request: POST <url> ..." at INFO, and the url ends in the
        # topic. Under the INFO basicConfig this command sets, that one line would put
        # the credential in every cron mail. Caught by running it, not by reading it.
        with self.assertNoLogs("httpx", level="INFO"):
            self.run_notify("--gameweek", str(GAMEWEEK))

    def test_a_failed_send_exits_8_and_records_nothing(self):
        failing = StubNtfy(status=500)
        self.addCleanup(failing.close)
        with mock.patch.dict(os.environ, {notify.SERVER_ENV: failing.url}):
            code, out, err = self.run_notify("--gameweek", str(GAMEWEEK))
        self.assertEqual(code, notify.EXIT_SEND_FAILED)
        self.assertEqual(self.recorded(), [])
        self.assertIn("FAILED", out)
        self.assertIn("NOT recorded", err)

    def test_an_unconfigured_topic_exits_2_without_sending(self):
        with mock.patch.dict(os.environ, {notify.TOPIC_ENV: ""}):
            code, _, err = self.run_notify("--gameweek", str(GAMEWEEK))
        self.assertEqual(code, notify.EXIT_UNREADABLE)
        self.assertEqual(self.stub.requests, [])
        self.assertIn(notify.TOPIC_ENV, err)

    def test_a_dry_run_works_without_a_topic(self):
        with mock.patch.dict(os.environ, {notify.TOPIC_ENV: ""}):
            code, out, _ = self.run_notify("--gameweek", str(GAMEWEEK), "--dry-run")
        self.assertEqual(code, notify.EXIT_OK)
        self.assertEqual(self.stub.requests, [])

    def test_a_missing_warehouse_is_refused_rather_than_created(self):
        missing = Path(self.tmp.name) / "nothing" / "fpl.db"
        out, err = StringIO(), StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = notify.main(["--db", str(missing)])
        self.assertEqual(code, notify.EXIT_UNREADABLE)
        self.assertFalse(missing.exists(),
                         "storage.connect created the warehouse it was meant to refuse")

    def test_silence_still_says_what_was_evaluated(self):
        # A wildcard silences every transfer trigger, and a clean squad silences the
        # rest - so nothing fires. That must be distinguishable from a broken notifier.
        conn = storage.connect(self.db)
        conn.execute(
            """UPDATE my_state SET free_transfers = 0, chips = ?""",
            ('[{"chip_type": "transfer", "name": "wildcard", '
             '"status_for_entry": "active"}]',))
        conn.commit()
        conn.close()

        code, out, _ = self.run_notify("--gameweek", str(GAMEWEEK))
        self.assertEqual(code, notify.EXIT_OK)
        self.assertEqual(self.stub.requests, [])
        self.assertIn("evaluated", out)
        for name in brief.TRIGGER_NAMES:
            self.assertIn(name, out, f"a silent run did not mention {name}")


class ConfigTest(unittest.TestCase):
    """The ini keys, and the masking that keeps the topic out of the log."""

    def test_the_notify_keys_are_mapped(self):
        self.assertEqual(config.MAPPING[("notify", "ntfy_topic")], "FPL_NTFY_TOPIC")
        self.assertEqual(config.MAPPING[("notify", "ntfy_server")], "FPL_NTFY_SERVER")
        self.assertEqual(config.MAPPING[("brief", "min_net_xp")],
                         "FPL_BRIEF_MIN_NET_XP")

    def test_the_topic_is_treated_as_a_secret(self):
        self.assertIn("FPL_NTFY_TOPIC", config.SECRET_ENV)

    def test_loading_an_ini_masks_the_topic_but_not_the_server(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "fpl-agent.ini"
            path.write_text("[notify]\nntfy_topic = a-long-random-topic\n"
                            "ntfy_server = http://box.local\n"
                            "[brief]\nmin_net_xp = 3.5\n")
            with mock.patch.dict(os.environ, {"FPL_NTFY_TOPIC": "",
                                              "FPL_NTFY_SERVER": "",
                                              "FPL_BRIEF_MIN_NET_XP": ""}):
                applied = config.load(path)
                self.assertEqual(applied["FPL_NTFY_TOPIC"], "***")
                self.assertEqual(applied["FPL_NTFY_SERVER"], "http://box.local")
                self.assertEqual(os.environ["FPL_NTFY_TOPIC"], "a-long-random-topic")
                self.assertEqual(brief.worth_making_threshold(), 3.5)


if __name__ == "__main__":
    unittest.main()

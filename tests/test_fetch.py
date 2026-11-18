"""Tests for scripts/fetch.py against tests/fixtures/calendar.ics.

Run: python3 -m unittest discover -s tests   (needs python-dateutil)
"""
import io
import json
import os
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "scripts"))
import fetch  # noqa: E402

ICS = os.path.join(HERE, "fixtures", "calendar.ics")


def run(now, hours=24):
    buf = io.StringIO()
    with redirect_stdout(buf):
        fetch.main(["--ics", ICS, "--now", now, "--hours", str(hours), "--tz", "Europe/Paris"])
    return json.loads(buf.getvalue())


def run_check(url, *extra):
    buf = io.StringIO()
    with redirect_stdout(buf):
        fetch.main(["--check", url, "--tz", "Europe/Paris", *extra])
    return json.loads(buf.getvalue())


class CheckTest(unittest.TestCase):
    def test_rejects_non_https(self):
        out = run_check("http://example.com/x.ics")
        self.assertFalse(out["ok"])
        self.assertIn("https://", out["message"])

    def test_http_404_message(self):
        import urllib.error
        msg = fetch.describe_error(urllib.error.HTTPError("u", 404, "nf", {}, None))
        self.assertIn("404", msg)
        self.assertIn("reset", msg)

    def test_timeout_and_dns_messages(self):
        import socket
        import urllib.error
        self.assertIn("did not answer", fetch.describe_error(urllib.error.URLError(socket.timeout())))
        self.assertIn("resolve", fetch.describe_error(urllib.error.URLError(socket.gaierror())))

    def test_valid_feed_counts_meetings(self):
        orig = fetch.download
        with open(ICS, encoding="utf-8") as f:
            text = f.read()
        fetch.download = lambda url: text
        try:
            out = run_check("https://example.com/basic.ics")
        finally:
            fetch.download = orig
        self.assertTrue(out["ok"])
        self.assertIsInstance(out["meetings"], int)

    def test_save_primes_caches_and_config(self):
        import tempfile
        saved = (fetch.download, fetch.CONFIG_FILE, fetch.CACHE_FILE, fetch.EVENTS_FILE)
        with open(ICS, encoding="utf-8") as f:
            text = f.read()
        with tempfile.TemporaryDirectory() as d:
            fetch.download = lambda url: text
            fetch.CONFIG_FILE = os.path.join(d, "cfg", "ics-url")
            fetch.CACHE_FILE = os.path.join(d, "cache", "calendar.ics")
            fetch.EVENTS_FILE = os.path.join(d, "cache", "events.json")
            try:
                out = run_check("https://example.com/basic.ics", "--save")
                self.assertTrue(out["ok"])
                with open(fetch.CONFIG_FILE, encoding="utf-8") as f:
                    self.assertEqual(f.read().strip(), "https://example.com/basic.ics")
                self.assertEqual(os.stat(fetch.CONFIG_FILE).st_mode & 0o777, 0o600)
                self.assertTrue(os.path.exists(fetch.CACHE_FILE))
                self.assertTrue(os.path.exists(fetch.EVENTS_FILE))
            finally:
                fetch.download, fetch.CONFIG_FILE, fetch.CACHE_FILE, fetch.EVENTS_FILE = saved

    def test_failed_check_saves_nothing(self):
        import tempfile
        saved = (fetch.download, fetch.CONFIG_FILE)
        def boom(url):
            raise ValueError("not an iCalendar feed")
        with tempfile.TemporaryDirectory() as d:
            fetch.download = boom
            fetch.CONFIG_FILE = os.path.join(d, "ics-url")
            try:
                out = run_check("https://example.com/x", "--save")
                self.assertFalse(out["ok"])
                self.assertFalse(os.path.exists(fetch.CONFIG_FILE))
            finally:
                fetch.download, fetch.CONFIG_FILE = saved

    def test_not_a_calendar(self):
        orig = fetch.download
        def boom(url):
            raise ValueError("not an iCalendar feed")
        fetch.download = boom
        try:
            out = run_check("https://example.com/x")
        finally:
            fetch.download = orig
        self.assertFalse(out["ok"])
        self.assertIn("does not point to a calendar", out["message"])

    def test_response_of(self):
        props = [("ATTENDEE", {"PARTSTAT": "TENTATIVE"}, "mailto:Ann@example.com")]
        self.assertEqual(fetch.response_of(props, "ann@example.com"), "tentative")
        self.assertEqual(fetch.response_of(props, ""), "")
        self.assertEqual(fetch.response_of([], "ann@example.com"), "")


EVENT = """BEGIN:VEVENT
UID:{uid}
DTSTART:{start}
DTEND:{end}
SUMMARY:{title}
{extra}
END:VEVENT
"""


def feed(*events):
    return "BEGIN:VCALENDAR\n" + "".join(events) + "END:VCALENDAR\n"


def expand(text, now="2026-11-17T12:00:00+00:00", hours=24):
    import datetime as dt
    from zoneinfo import ZoneInfo
    return fetch.occurrences(text, dt.datetime.fromisoformat(now), hours, ZoneInfo("UTC"))


class SecurityTest(unittest.TestCase):
    def link(self, text):
        return fetch.zoom_link([("LOCATION", {}, text)])

    def test_lookalike_zoom_hosts_are_rejected(self):
        for bad in ("https://evilzoom.us/j/1", "https://zoom.us.evil.com/j/1",
                    "https://zoom.us@evil.com/j/1", "http://zoom.us/j/1",
                    "zoommtg://evil.com/join?confno=1", "zoommtg://evilzoom.us/join"):
            self.assertIsNone(self.link(bad), bad)
        self.assertEqual(self.link("https://zoom.us/j/1"), "https://zoom.us/j/1")
        self.assertEqual(self.link("https://acme.zoom.us/j/1"), "https://acme.zoom.us/j/1")
        self.assertEqual(self.link("zoommtg://zoom.us/join?confno=1"), "zoommtg://zoom.us/join?confno=1")

    def test_runaway_recurrence_is_bounded(self):
        import time
        ok = EVENT.format(uid="ok", start="20261117T130000Z", end="20261117T133000Z",
                          title="Fine", extra="LOCATION:https://zoom.us/j/1")
        secondly = EVENT.format(uid="s", start="19000101T000000Z", end="19000101T000001Z",
                                title="Bomb", extra="RRULE:FREQ=SECONDLY\nLOCATION:https://zoom.us/j/2")
        # Never yields, so only the time limit can stop it.
        barren = EVENT.format(uid="b", start="19000101T000000Z", end="19000101T010000Z", title="Barren",
                              extra="RRULE:FREQ=HOURLY;BYMONTH=2;BYMONTHDAY=30\nLOCATION:https://zoom.us/j/3")
        saved = fetch.EXPAND_SECONDS
        fetch.EXPAND_SECONDS = 0.2
        try:
            began = time.monotonic()
            out = expand(feed(secondly, barren, ok))
        finally:
            fetch.EXPAND_SECONDS = saved
        self.assertLess(time.monotonic() - began, 3)
        self.assertEqual([e["title"] for e in out], ["Fine"])

    def test_occurrences_per_event_are_capped(self):
        hourly = EVENT.format(uid="h", start="20261101T000000Z", end="20261101T000100Z", title="Hourly",
                              extra="RRULE:FREQ=HOURLY;BYMINUTE=0,10,20,30,40,50\nLOCATION:https://zoom.us/j/1")
        self.assertLessEqual(len(expand(feed(hourly), hours=168)), fetch.MAX_OCCURRENCES)

    def test_malformed_event_does_not_hide_the_rest(self):
        ok = EVENT.format(uid="ok", start="20261117T130000Z", end="20261117T133000Z",
                          title="Fine", extra="LOCATION:https://zoom.us/j/1")
        bad = EVENT.format(uid="bad", start="garbage", end="garbage",
                           title="Bad", extra="LOCATION:https://zoom.us/j/2")
        bad_rule = EVENT.format(uid="r", start="20261117T130000Z", end="20261117T133000Z",
                                title="Bad rule", extra="RRULE:FREQ=NOPE\nLOCATION:https://zoom.us/j/3")
        self.assertEqual([e["title"] for e in expand(feed(bad, bad_rule, ok))], ["Fine"])

    def test_valid_url(self):
        self.assertTrue(fetch.valid_url("https://calendar.google.com/calendar/ical/a%40b.c/private-x/basic.ics"))
        for bad in ("http://x/y", "https://", "https://x/a b", "https://x/a\nb", "file:///etc/passwd", "", None):
            self.assertFalse(fetch.valid_url(bad), bad)

    def test_redirect_away_from_https_is_refused(self):
        import urllib.error
        import urllib.request
        handler = fetch.HttpsOnlyRedirect()
        req = urllib.request.Request("https://example.com/a.ics")
        for target in ("http://example.com/a.ics", "ftp://example.com/a.ics"):
            with self.assertRaises(urllib.error.URLError):
                handler.redirect_request(req, None, 302, "Found", {}, target)
        self.assertIsNotNone(handler.redirect_request(req, None, 302, "Found", {}, "https://example.org/a.ics"))

    def test_gzip_bomb_is_cut_off(self):
        import gzip
        import io
        from email.message import Message

        class Response(io.BytesIO):
            headers = Message()
            def __enter__(self): return self
            def __exit__(self, *a): return False
        Response.headers["Content-Encoding"] = "gzip"

        class Opener:
            def __init__(self, body): self.body = body
            def open(self, req, timeout=None): return Response(self.body)

        saved = (fetch.MAX_BYTES, fetch.urllib.request.build_opener)
        fetch.MAX_BYTES = 1024
        try:
            fetch.urllib.request.build_opener = lambda *h: Opener(gzip.compress(b"BEGIN:VCALENDAR\n" + b"x" * 10_000_000))
            with self.assertRaises(fetch.FeedTooLarge):
                fetch.download("https://example.com/a.ics")
            fetch.urllib.request.build_opener = lambda *h: Opener(gzip.compress(b"BEGIN:VCALENDAR\nEND:VCALENDAR\n"))
            self.assertIn("BEGIN:VCALENDAR", fetch.download("https://example.com/a.ics"))
        finally:
            fetch.MAX_BYTES, fetch.urllib.request.build_opener = saved

    def test_size_cap_is_100_mb(self):
        self.assertEqual(fetch.MAX_BYTES, 100 * 1024 * 1024)
        msg = fetch.describe_error(fetch.FeedTooLarge("feed too large"))
        self.assertIn("100 MB", msg)

    def test_too_large_feed_is_reported(self):
        import tempfile

        def too_large(url):
            raise fetch.FeedTooLarge("feed too large")

        def fetch_main():
            buf = io.StringIO()
            with redirect_stdout(buf), redirect_stderr(io.StringIO()):
                fetch.main(["--now", "2026-11-17T13:00:00+01:00", "--tz", "Europe/Paris"])
            return json.loads(buf.getvalue())

        saved = (fetch.download, fetch.read_url, fetch.CACHE_FILE, fetch.EVENTS_FILE)
        with tempfile.TemporaryDirectory() as tmp:
            fetch.download = too_large
            fetch.read_url = lambda: "https://example.com/a.ics"
            fetch.CACHE_FILE = os.path.join(tmp, "calendar.ics")
            fetch.EVENTS_FILE = os.path.join(tmp, "events.json")
            try:
                # No cache: nothing to show, only the error.
                out = fetch_main()
                self.assertEqual(out["error"], "feed-too-large")
                self.assertIn("100 MB", out["message"])
                self.assertEqual(out["events"], [])
                # With a cache: its meetings are shown and the error still surfaces.
                with open(ICS, encoding="utf-8") as f:
                    fetch.write_cache(f.read())
                out = fetch_main()
                self.assertEqual(out["error"], "feed-too-large")
                self.assertTrue(out["stale"])
                self.assertTrue(out["events"])
                self.assertFalse(os.path.exists(fetch.EVENTS_FILE))
            finally:
                fetch.download, fetch.read_url, fetch.CACHE_FILE, fetch.EVENTS_FILE = saved

    def test_parse_error_text_is_not_relayed(self):
        orig = (fetch.download, fetch.occurrences)
        fetch.download = lambda url: "BEGIN:VCALENDAR"
        def boom(*a, **k):
            raise ValueError("<img src=x>")
        fetch.occurrences = boom
        try:
            import contextlib
            with contextlib.redirect_stderr(io.StringIO()):
                out = run_check("https://example.com/basic.ics")
        finally:
            fetch.download, fetch.occurrences = orig
        self.assertFalse(out["ok"])
        self.assertNotIn("<img", out["message"])

    def test_private_write_cleans_up_and_tightens_dir(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            sub = os.path.join(d, "app")
            os.mkdir(sub, 0o755)
            def fail(f):
                raise OSError("disk full")
            with self.assertRaises(OSError):
                fetch.write_private(os.path.join(sub, "x"), fail)
            self.assertEqual(os.listdir(sub), [])
            fetch.write_private(os.path.join(sub, "x"), lambda f: f.write("y"))
            self.assertEqual(os.stat(os.path.join(sub, "x")).st_mode & 0o777, 0o600)
            self.assertEqual(os.stat(sub).st_mode & 0o777, 0o700)


class FetchTest(unittest.TestCase):
    def titles(self, out):
        return [(e["title"], e["start"]) for e in out["events"]]

    def test_today(self):
        out = run("2026-11-17T12:00:00+01:00")
        self.assertFalse(out["stale"])
        self.assertEqual(self.titles(out), [
            ("Design review", "2026-11-17T14:00:00+01:00"),
            ("Weekly 1:1, Bob", "2026-11-17T18:00:00+01:00"),
            # 18th standup is excluded by EXDATE, so the next standup is the 19th override
        ])
        self.assertEqual(out["events"][0]["zoomUrl"], "https://example.zoom.us/j/111?pwd=abc")

    def test_filters(self):
        titles = [t for t, _ in self.titles(run("2026-11-17T00:00:00+01:00", hours=24))]
        for hidden in ("Offsite", "Declined sync", "Cancelled call", "Google Meet chat"):
            self.assertNotIn(hidden, titles)
        self.assertIn("Standup", titles)

    def test_exdate_and_override(self):
        out = run("2026-11-18T00:00:00+01:00", hours=48)
        standups = [(t, s) for t, s in self.titles(out) if t.startswith("Standup")]
        self.assertEqual(standups, [("Standup (moved)", "2026-11-19T10:00:00+01:00")])

    def test_in_progress_is_kept(self):
        out = run("2026-11-17T14:10:00+01:00", hours=1)
        self.assertEqual(out["events"][0]["title"], "Design review")

    def test_ended_is_dropped(self):
        out = run("2026-11-17T14:31:00+01:00", hours=1)
        self.assertEqual(out["events"], [])

    def test_floating_until_and_alarm_ignored(self):
        out = run("2026-12-29T12:00:00+01:00", hours=24)
        weekly = [e for e in out["events"] if e["title"].startswith("Weekly")]
        self.assertEqual(len(weekly), 1)
        self.assertEqual(weekly[0]["zoomUrl"], "https://example.zoom.us/j/666")

    def test_no_config(self):
        old = fetch.CONFIG_FILE
        fetch.CONFIG_FILE = os.path.join(HERE, "does-not-exist")
        try:
            buf = io.StringIO()
            with redirect_stdout(buf):
                fetch.main([])
            self.assertEqual(json.loads(buf.getvalue())["error"], "no-config")
        finally:
            fetch.CONFIG_FILE = old


if __name__ == "__main__":
    unittest.main()


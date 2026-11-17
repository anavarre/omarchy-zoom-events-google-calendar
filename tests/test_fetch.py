"""Tests for scripts/fetch.py against tests/fixtures/calendar.ics.

Run: python3 -m unittest discover -s tests   (needs python-dateutil)
"""
import io
import json
import os
import sys
import unittest
from contextlib import redirect_stdout

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "scripts"))
import fetch  # noqa: E402

ICS = os.path.join(HERE, "fixtures", "calendar.ics")


def run(now, hours=24):
    buf = io.StringIO()
    with redirect_stdout(buf):
        fetch.main(["--ics", ICS, "--now", now, "--hours", str(hours), "--tz", "Europe/Paris"])
    return json.loads(buf.getvalue())


def run_check(url):
    buf = io.StringIO()
    with redirect_stdout(buf):
        fetch.main(["--check", url, "--tz", "Europe/Paris"])
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


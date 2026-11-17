#!/usr/bin/env python3
"""Print the upcoming Zoom meetings from a Google Calendar secret iCal feed.

Usage: fetch.py [--ics FILE] [--now ISO8601] [--hours N]

The feed URL is read from ~/.config/omarchy-zoom-events-google-calendar/ics-url (keep it
mode 0600: anyone holding it can read the calendar). The last good download is
cached so a network failure keeps showing meetings instead of nothing.

Output is one JSON object on stdout:
  {"events": [{"title", "start", "end", "zoomUrl", "response"}], "stale": bool}
or, when there is nothing to show at all:
  {"events": [], "error": "no-config" | "fetch-failed" | "parse-failed", "message": "..."}

  fetch.py --check URL validates a feed before it is saved and prints
  {"ok": true, "meetings": N} or {"ok": false, "message": "..."}.

Only the standard library and python-dateutil are used.
"""

import argparse
import datetime as dt
import json
import os
import re
import sys
import gzip
import socket
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dateutil import rrule

APP = "omarchy-zoom-events-google-calendar"
CONFIG_FILE = os.path.join(
    os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"), APP, "ics-url"
)
CACHE_FILE = os.path.join(
    os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache"), APP, "calendar.ics"
)
EVENTS_FILE = os.path.join(os.path.dirname(CACHE_FILE), "events.json")
MAX_BYTES = 256 * 1024 * 1024
TIMEOUT = 45
UTC = dt.timezone.utc

ZOOM_RE = re.compile(
    r"(?:https://[A-Za-z0-9.-]*zoom\.us/(?:j|my|w|s)/[^\s\"'<>\\]+|zoommtg://[^\s\"'<>\\]+)",
    re.IGNORECASE,
)


# --- iCalendar parsing ------------------------------------------------------

def unfold(text):
    lines = []
    for raw in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if raw[:1] in (" ", "\t") and lines:
            lines[-1] += raw[1:]
        elif raw:
            lines.append(raw)
    return lines


def split_property(line):
    """'NAME;A=1;B="x:y":value' -> ('NAME', {'A': '1', 'B': 'x:y'}, 'value')."""
    quoted = False
    for i, ch in enumerate(line):
        if ch == '"':
            quoted = not quoted
        elif ch == ":" and not quoted:
            head, value = line[:i], line[i + 1:]
            break
    else:
        return None
    parts, buf, quoted = [], "", False
    for ch in head:
        if ch == '"':
            quoted = not quoted
        elif ch == ";" and not quoted:
            parts.append(buf)
            buf = ""
            continue
        buf += ch
    parts.append(buf)
    params = {}
    for p in parts[1:]:
        if "=" in p:
            k, v = p.split("=", 1)
            params[k.upper()] = v.strip('"')
    return parts[0].upper(), params, value


def unescape(value):
    return re.sub(r"\\([\\;,nN])", lambda m: "\n" if m.group(1) in "nN" else m.group(1), value)


def zone(tzid, local_tz):
    if not tzid:
        return local_tz
    try:
        return ZoneInfo(tzid)
    except (ZoneInfoNotFoundError, ValueError):
        return local_tz


def parse_dt(value, params, local_tz):
    """Return (aware datetime, is_all_day)."""
    value = value.strip()
    if params.get("VALUE", "").upper() == "DATE" or re.fullmatch(r"\d{8}", value):
        d = dt.datetime.strptime(value[:8], "%Y%m%d")
        return d.replace(tzinfo=local_tz), True
    if value.endswith("Z"):
        return dt.datetime.strptime(value[:15], "%Y%m%dT%H%M%S").replace(tzinfo=UTC), False
    d = dt.datetime.strptime(value[:15], "%Y%m%dT%H%M%S")
    return d.replace(tzinfo=zone(params.get("TZID"), local_tz)), False


DURATION_RE = re.compile(
    r"([+-])?P(?:(\d+)W)?(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?$"
)


def parse_duration(value):
    m = DURATION_RE.match(value.strip())
    if not m:
        return None
    sign, w, d, h, mi, s = m.groups()
    delta = dt.timedelta(weeks=int(w or 0), days=int(d or 0), hours=int(h or 0),
                         minutes=int(mi or 0), seconds=int(s or 0))
    return -delta if sign == "-" else delta


def parse_events(text):
    """Return (calendar owner email or '', list of raw VEVENT property lists)."""
    owner = ""
    events, current, depth = [], None, 0
    for line in unfold(text):
        prop = split_property(line)
        if not prop:
            continue
        name, params, value = prop
        if name == "BEGIN":
            if value.upper() == "VEVENT" and current is None:
                current, depth = [], 0
            elif current is not None:
                depth += 1  # VALARM etc. nested inside the event
            continue
        if name == "END":
            if current is not None:
                if depth:
                    depth -= 1
                elif value.upper() == "VEVENT":
                    events.append(current)
                    current = None
            continue
        if current is not None:
            if not depth:
                current.append((name, params, value))
        elif name == "X-WR-CALNAME" and "@" in value:
            owner = value.strip().lower()
    return owner, events


def zoom_link(props):
    for key in ("LOCATION", "URL", "DESCRIPTION", "X-GOOGLE-CONFERENCE"):
        for name, _params, value in props:
            if name == key:
                m = ZOOM_RE.search(unescape(value))
                if m:
                    return m.group(0).rstrip(".,;)]")
    return None


def response_of(props, owner):
    """The owner's RSVP: accepted | declined | tentative | needs-action, or ''
    when unknown (no owner in the feed, or an event without attendees)."""
    if not owner:
        return ""
    for name, params, value in props:
        if name == "ATTENDEE" and value.lower().replace("mailto:", "").strip() == owner:
            return {"ACCEPTED": "accepted", "DECLINED": "declined",
                    "TENTATIVE": "tentative", "NEEDS-ACTION": "needs-action"
                    }.get(params.get("PARTSTAT", "").upper(), "")
    return ""


def first(props, key):
    for name, params, value in props:
        if name == key:
            return params, value
    return None


def normalize_until(rule, dtstart):
    """dateutil requires UNTIL in UTC when DTSTART is aware; Google sometimes
    writes a floating or date-only UNTIL. Rewrite it in UTC."""
    def fix(m):
        raw = m.group(1)
        if raw.endswith("Z"):
            return m.group(0)
        if len(raw) == 8:
            local = dt.datetime.strptime(raw, "%Y%m%d").replace(
                hour=23, minute=59, second=59, tzinfo=dtstart.tzinfo)
        else:
            local = dt.datetime.strptime(raw[:15], "%Y%m%dT%H%M%S").replace(tzinfo=dtstart.tzinfo)
        return "UNTIL=" + local.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    return re.sub(r"UNTIL=([0-9TZ]+)", fix, rule)


# --- expansion --------------------------------------------------------------

def owner_from_url(url):
    """Google's feed URL is .../ical/<email, percent-encoded>/private-.../basic.ics.
    X-WR-CALNAME is often a display name, so this is the reliable source."""
    m = re.search(r"/ical/([^/]+)/", url or "")
    if not m:
        return ""
    who = urllib.parse.unquote(m.group(1)).strip().lower()
    return who if "@" in who else ""


def occurrences(text, now, hours, local_tz, owner_hint=""):
    owner, raw_events = parse_events(text)
    owner = owner_hint or owner
    window_start = now - dt.timedelta(days=1)
    window_end = now + dt.timedelta(hours=hours)

    masters, overrides = [], []
    for props in raw_events:
        (overrides if first(props, "RECURRENCE-ID") else masters).append(props)

    # Instances moved or edited individually: (UID, original start instant).
    overridden = set()
    for props in overrides:
        uid = (first(props, "UID") or ({}, ""))[1]
        p, v = first(props, "RECURRENCE-ID")
        start, _ = parse_dt(v, p, local_tz)
        overridden.add((uid, start.astimezone(UTC)))

    out = []
    for props in masters + overrides:
        dtstart_prop = first(props, "DTSTART")
        if not dtstart_prop:
            continue
        status = (first(props, "STATUS") or ({}, ""))[1].upper()
        response = response_of(props, owner)
        if status == "CANCELLED" or response == "declined":
            continue
        start, all_day = parse_dt(dtstart_prop[1], dtstart_prop[0], local_tz)
        if all_day:
            continue
        link = zoom_link(props)
        if not link:
            continue

        end_prop = first(props, "DTEND")
        if end_prop:
            duration = parse_dt(end_prop[1], end_prop[0], local_tz)[0] - start
        else:
            dur_prop = first(props, "DURATION")
            duration = (parse_duration(dur_prop[1]) if dur_prop else None) or dt.timedelta(0)

        uid = (first(props, "UID") or ({}, ""))[1]
        title = unescape((first(props, "SUMMARY") or ({}, ""))[1]).strip() or "Zoom meeting"
        rule_prop = first(props, "RRULE") if not first(props, "RECURRENCE-ID") else None

        if rule_prop:
            rs = rrule.rruleset()
            rs.rrule(rrule.rrulestr(normalize_until(rule_prop[1], start), dtstart=start))
            for name, params, value in props:
                if name in ("EXDATE", "RDATE"):
                    for item in value.split(","):
                        when, _ = parse_dt(item, params, local_tz)
                        (rs.exdate if name == "EXDATE" else rs.rdate)(when)
            starts = [s for s in rs.between(window_start, window_end, inc=True)
                      if (uid, s.astimezone(UTC)) not in overridden]
        else:
            starts = [start] if window_start <= start <= window_end else []

        for s in starts:
            e = s + duration
            if e > now:
                out.append({
                    "title": title,
                    "start": s.astimezone(local_tz).isoformat(),
                    "end": e.astimezone(local_tz).isoformat(),
                    "zoomUrl": link,
                    "response": response,
                })

    out.sort(key=lambda ev: (ev["start"], ev["title"]))
    return out


# --- I/O --------------------------------------------------------------------

def read_url():
    try:
        with open(CONFIG_FILE, encoding="utf-8") as f:
            url = f.read().strip()
    except OSError:
        return None
    return url if url.startswith("https://") else None


_getaddrinfo = socket.getaddrinfo


def _ipv4_first(*args, **kwargs):
    """Try IPv4 addresses before IPv6: a broken IPv6 route otherwise hangs."""
    infos = _getaddrinfo(*args, **kwargs)
    return sorted(infos, key=lambda i: i[0] != socket.AF_INET)


def download(url):
    socket.getaddrinfo = _ipv4_first
    req = urllib.request.Request(
        url, headers={"User-Agent": APP, "Accept-Encoding": "gzip"}
    )
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        data = resp.read(MAX_BYTES + 1)
        if resp.headers.get("Content-Encoding") == "gzip":
            data = gzip.decompress(data)
    if len(data) > MAX_BYTES:
        raise ValueError("feed too large")
    text = data.decode("utf-8", errors="replace")
    if "BEGIN:VCALENDAR" not in text:
        raise ValueError("not an iCalendar feed")
    return text


def describe_error(exc):
    """Map a download failure to a short, human-readable message."""
    if isinstance(exc, urllib.error.HTTPError):
        if exc.code in (401, 403, 404):
            return (f"Google rejected the address (HTTP {exc.code}). It is wrong, "
                    "or the secret address was reset. Copy it again from Google "
                    "Calendar settings.")
        if exc.code == 429:
            return "Google is rate limiting requests (HTTP 429). Try again in a minute."
        return f"The server answered with an error (HTTP {exc.code})."
    reason = exc.reason if isinstance(exc, urllib.error.URLError) else exc
    if isinstance(reason, (socket.timeout, TimeoutError)):
        return f"The server did not answer within {TIMEOUT} seconds."
    if isinstance(reason, socket.gaierror):
        return "Could not resolve the host name. Check the address and your network connection."
    if isinstance(reason, ConnectionRefusedError):
        return "The connection was refused. Check the address."
    if isinstance(reason, ValueError):
        return f"The address does not point to a calendar: {reason}."
    return f"Could not download the calendar: {reason}"


def check(url, hours, local_tz):
    """Validate a feed before it is saved: download, parse, count meetings.
    Prints {"ok": true, "meetings": N} or {"ok": false, "message": "..."}."""
    if not url.startswith("https://"):
        print(json.dumps({"ok": False, "message": "The address must start with https://"}))
        return 0
    try:
        text = download(url)
    except Exception as exc:
        print(json.dumps({"ok": False, "message": describe_error(exc)}))
        return 0
    try:
        events = occurrences(text, dt.datetime.now(UTC), hours, local_tz, owner_from_url(url))
    except Exception as exc:
        print(json.dumps({"ok": False, "message": f"The calendar downloaded but could not be read: {exc}"}))
        return 0
    print(json.dumps({"ok": True, "meetings": len(events)}))
    return 0


def write_cache(text):
    os.makedirs(os.path.dirname(CACHE_FILE), mode=0o700, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(CACHE_FILE))
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, CACHE_FILE)


def read_cache():
    try:
        with open(CACHE_FILE, encoding="utf-8") as f:
            return f.read()
    except OSError:
        return None


def write_events(events):
    """Cache the expanded meeting list so --cached can answer instantly: the
    raw feed can be tens of MB and take half a minute to download."""
    try:
        os.makedirs(os.path.dirname(EVENTS_FILE), mode=0o700, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(EVENTS_FILE))
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(events, f)
        os.replace(tmp, EVENTS_FILE)
    except OSError as exc:
        print(f"{APP}: could not write events cache: {exc}", file=sys.stderr)


def cached_events(now):
    try:
        with open(EVENTS_FILE, encoding="utf-8") as f:
            events = json.load(f)
        return [e for e in events if dt.datetime.fromisoformat(e["end"]) > now]
    except (OSError, ValueError, KeyError, TypeError):
        return None


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--ics", help="read this file instead of the configured feed")
    ap.add_argument("--now", help="pretend it is this ISO 8601 instant")
    ap.add_argument("--hours", type=int, default=24)
    ap.add_argument("--check", metavar="URL", help="validate this feed URL and exit")
    ap.add_argument("--cached", action="store_true",
                    help="answer from the local cache only, never the network")
    ap.add_argument("--tz", help="local IANA zone (default: system)")
    args = ap.parse_args(argv)

    local_tz = ZoneInfo(args.tz) if args.tz else dt.datetime.now().astimezone().tzinfo
    now = dt.datetime.fromisoformat(args.now) if args.now else dt.datetime.now(UTC)
    if now.tzinfo is None:
        now = now.replace(tzinfo=local_tz)

    if args.check:
        return check(args.check, max(1, min(args.hours, 24 * 7)), local_tz)

    if args.cached:
        events = cached_events(now)
        if events is None:
            print(json.dumps({"events": [], "error": "no-cache"}))
        else:
            print(json.dumps({"events": events, "stale": False}))
        return 0

    stale = False
    if args.ics:
        with open(args.ics, encoding="utf-8") as f:
            text = f.read()
    else:
        url = read_url()
        if not url:
            print(json.dumps({"events": [], "error": "no-config"}))
            return 0
        try:
            text = download(url)
            write_cache(text)
        except Exception as exc:  # network, HTTP, size: fall back to the cache
            print(f"{APP}: fetch failed: {exc}", file=sys.stderr)
            text = read_cache()
            stale = True
            if text is None:
                print(json.dumps({"events": [], "error": "fetch-failed",
                                  "message": describe_error(exc)}))
                return 0

    try:
        events = occurrences(text, now, max(1, min(args.hours, 24 * 7)), local_tz,
                             owner_from_url(read_url()) if not args.ics else "")
    except Exception as exc:
        print(f"{APP}: parse failed: {exc}", file=sys.stderr)
        print(json.dumps({"events": [], "error": "parse-failed"}))
        return 0

    if not args.ics and not stale:
        write_events(events)
    print(json.dumps({"events": events, "stale": stale}))
    return 0


if __name__ == "__main__":
    sys.exit(main())

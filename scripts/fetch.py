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
  Adding --save also stores the address (mode 0600) and primes both caches from
  the same download, so connecting needs a single download and the widget can
  show the meetings immediately.

Only the standard library and python-dateutil are used.
"""

import argparse
import contextlib
import fcntl
import datetime as dt
import json
import os
import re
import signal
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
URL_ENV = "ZOOM_EVENTS_ICS_URL"
MAX_BYTES = 100 * 1024 * 1024
TIMEOUT = 45
# The socket timeout only bounds each read; this bounds a feed that trickles in.
DOWNLOAD_SECONDS = 180
UTC = dt.timezone.utc

# Event fields come from whoever sent the invite, so recurrence expansion is
# bounded: a rule such as FREQ=SECONDLY from 1900 would otherwise spin for hours.
EXPAND_SECONDS = 2        # per recurring event
MAX_SLOW_EVENTS = 5       # after this many hit the limit, stop expanding rules
MAX_OCCURRENCES = 500     # per recurring event, inside the window
MAX_TITLE = 200           # characters kept of a title
MAX_LINK = 2048           # a longer meeting link is not a real one: the event is skipped
MAX_OUTPUT_BYTES = 1 << 20  # serialized size of all expanded meetings together
MAX_OUTPUT_EVENTS = 2000    # and their count

# The host must be zoom.us or a subdomain of it: a looser match would accept
# look-alikes such as evilzoom.us, and the link is opened on click. zoommtg://
# is held to the same hosts, as the Zoom client acts on whatever it is handed.
ZOOM_HOST = r"(?:[A-Za-z0-9-]+\.)*zoom\.us"
ZOOM_RE = re.compile(
    r"(?:https://" + ZOOM_HOST + r"/(?:j|my|w|s)/[^\s\"'<>\\]+"
    r"|zoommtg://" + ZOOM_HOST + r"/[^\s\"'<>\\]+)",
    re.IGNORECASE,
)


class LimitReached(TimeoutError):
    pass


class FeedTooLarge(ValueError):
    """The feed is bigger than MAX_BYTES once decompressed."""


def too_large_message():
    return (f"The calendar is larger than the {MAX_BYTES // (1024 * 1024)} MB limit, "
            "so it was not loaded.")


@contextlib.contextmanager
def time_limit(seconds):
    """Raise LimitReached in the block after `seconds` of wall-clock time."""
    def expired(_signum, _frame):
        raise LimitReached("time limit reached")
    try:
        previous = signal.signal(signal.SIGALRM, expired)
    except ValueError:  # not the main thread: no alarm available
        yield
        return
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


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
                    link = m.group(0).rstrip(".,;)]")
                    return link if len(link) <= MAX_LINK else None
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

def expand(props, owner, overridden, now, window_start, window_end, local_tz, recurring=True, budget=None):
    """The occurrences of one VEVENT that have not ended, as output dicts."""
    dtstart_prop = first(props, "DTSTART")
    if not dtstart_prop:
        return []
    status = (first(props, "STATUS") or ({}, ""))[1].upper()
    response = response_of(props, owner)
    if status == "CANCELLED" or response == "declined":
        return []
    start, all_day = parse_dt(dtstart_prop[1], dtstart_prop[0], local_tz)
    if all_day:
        return []
    link = zoom_link(props)
    if not link:
        return []

    end_prop = first(props, "DTEND")
    if end_prop:
        duration = parse_dt(end_prop[1], end_prop[0], local_tz)[0] - start
    else:
        dur_prop = first(props, "DURATION")
        duration = (parse_duration(dur_prop[1]) if dur_prop else None) or dt.timedelta(0)

    uid = (first(props, "UID") or ({}, ""))[1]
    title = unescape((first(props, "SUMMARY") or ({}, ""))[1]).strip()[:MAX_TITLE] or "Zoom meeting"
    rule_prop = first(props, "RRULE") if not first(props, "RECURRENCE-ID") else None

    if rule_prop:
        # No calendar offers sub-hourly repeats; they only serve to burn CPU.
        if not recurring or re.search(r"FREQ=(?:SECONDLY|MINUTELY)", rule_prop[1], re.IGNORECASE):
            return []
        starts = []
        with time_limit(EXPAND_SECONDS):
            rs = rrule.rruleset()
            rs.rrule(rrule.rrulestr(normalize_until(rule_prop[1], start), dtstart=start))
            for name, params, value in props:
                if name in ("EXDATE", "RDATE"):
                    for item in value.split(","):
                        when, _ = parse_dt(item, params, local_tz)
                        (rs.exdate if name == "EXDATE" else rs.rdate)(when)
            for s in rs.xafter(window_start, count=MAX_OCCURRENCES, inc=True):
                if s > window_end:
                    break
                if (uid, s.astimezone(UTC)) not in overridden:
                    starts.append(s)
    else:
        starts = [start] if window_start <= start <= window_end else []

    out = []
    for s in starts:
        e = s + duration
        if e > now:
            item = {
                "title": title,
                "start": s.astimezone(local_tz).isoformat(),
                "end": e.astimezone(local_tz).isoformat(),
                "zoomUrl": link,
                "response": response,
            }
            if budget is not None:
                # Charged as each occurrence is built, so nothing past the
                # budget is ever accumulated, cached or printed.
                budget["events"] -= 1
                budget["bytes"] -= len(json.dumps(item))
                if budget["events"] < 0 or budget["bytes"] < 0:
                    budget["spent"] = True
                    break
            out.append(item)
    return out


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
    # One malformed event (anyone can send an invite) must not hide the rest,
    # so each event is handled on its own and skipped if it cannot be read.
    overridden = set()
    for props in overrides:
        try:
            uid = (first(props, "UID") or ({}, ""))[1]
            p, v = first(props, "RECURRENCE-ID")
            start, _ = parse_dt(v, p, local_tz)
            overridden.add((uid, start.astimezone(UTC)))
        except Exception:
            continue

    out, slow = [], 0
    budget = {"events": MAX_OUTPUT_EVENTS, "bytes": MAX_OUTPUT_BYTES}
    for props in masters + overrides:
        try:
            out.extend(expand(props, owner, overridden, now, window_start, window_end,
                              local_tz, slow < MAX_SLOW_EVENTS, budget))
            if budget.get("spent"):
                print(f"{APP}: output budget reached; remaining meetings dropped", file=sys.stderr)
                break
        except LimitReached:
            slow += 1
            print(f"{APP}: skipped an event whose recurrence took too long", file=sys.stderr)
        except Exception:
            continue

    out.sort(key=lambda ev: (ev["start"], ev["title"]))
    return out


# --- I/O --------------------------------------------------------------------

def valid_url(url):
    """An https URL with a host and no whitespace or control characters."""
    if not re.fullmatch(r"https://[\x21-\x7e]+", url or "", re.IGNORECASE):
        return False
    try:
        return bool(urllib.parse.urlsplit(url).hostname)
    except ValueError:
        return False


def read_url():
    try:
        with open(CONFIG_FILE, encoding="utf-8") as f:
            url = f.read().strip()
    except OSError:
        return None
    return url if valid_url(url) else None


_getaddrinfo = socket.getaddrinfo


def _ipv4_first(*args, **kwargs):
    """Try IPv4 addresses before IPv6: a broken IPv6 route otherwise hangs."""
    infos = _getaddrinfo(*args, **kwargs)
    return sorted(infos, key=lambda i: i[0] != socket.AF_INET)


class HttpsOnlyRedirect(urllib.request.HTTPRedirectHandler):
    """urllib follows redirects to http:// and ftp:// by default, which would
    send the request, and bring the calendar back, in the clear."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not valid_url(newurl):
            raise urllib.error.URLError("the server redirected away from https")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def download(url):
    if not valid_url(url):
        raise ValueError("not an https address")
    socket.getaddrinfo = _ipv4_first
    req = urllib.request.Request(
        url, headers={"User-Agent": APP, "Accept-Encoding": "gzip"}
    )
    opener = urllib.request.build_opener(HttpsOnlyRedirect)
    with time_limit(DOWNLOAD_SECONDS), opener.open(req, timeout=TIMEOUT) as resp:
        stream = resp
        if (resp.headers.get("Content-Encoding") or "").strip().lower() == "gzip":
            # Decompress as a stream so the size cap applies to the expanded
            # data: a small gzip body can otherwise inflate to fill the memory.
            stream = gzip.GzipFile(fileobj=resp)
        data = stream.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise FeedTooLarge("feed too large")
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
    if isinstance(reason, FeedTooLarge):
        return too_large_message()
    if isinstance(reason, LimitReached):
        return "The server did not answer in time: the download took too long."
    if isinstance(reason, (socket.timeout, TimeoutError)):
        return f"The server did not answer within {TIMEOUT} seconds."
    if isinstance(reason, socket.gaierror):
        return "Could not resolve the host name. Check the address and your network connection."
    if isinstance(reason, ConnectionRefusedError):
        return "The connection was refused. Check the address."
    if isinstance(reason, ValueError):
        return f"The address does not point to a calendar: {reason}."
    return f"Could not download the calendar: {reason}"


def write_private(path, write):
    """Write `path` atomically, readable by the owner only. The directory is
    made (or tightened to) 0700 and mkstemp creates the file as 0600; a failed
    write leaves no temporary copy of the calendar behind."""
    d = os.path.dirname(path)
    os.makedirs(d, mode=0o700, exist_ok=True)
    try:
        os.chmod(d, 0o700)
    except OSError:
        pass
    fd, tmp = tempfile.mkstemp(dir=d)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            write(f)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


@contextlib.contextmanager
def state_lock():
    """Exclusive lock shared by every process that changes the connected
    calendar or its caches (one widget instance runs per screen). Commits and
    disconnects serialize on it, so a refresh can never publish after a
    disconnect has wiped the files."""
    d = os.path.dirname(CACHE_FILE)
    os.makedirs(d, mode=0o700, exist_ok=True)
    fd = os.open(os.path.join(d, ".lock"), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)  # closing releases the lock


GENERATION_FILE = None  # default: next to the cache; tests may override


def _generation_path():
    return GENERATION_FILE or os.path.join(os.path.dirname(CACHE_FILE), ".generation")


def read_generation():
    """Marker shared by every process (one widget instance runs per screen).
    Disconnect replaces it, so work that captured the marker before a
    disconnect can tell, under the lock, that its result is out of date."""
    try:
        with open(_generation_path(), encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""


def bump_generation():
    token = os.urandom(8).hex()
    write_private(_generation_path(), lambda f: f.write(token + "\n"))


def still_configured(url, generation):
    """True while `url` is still the connected calendar and no disconnect has
    happened since `generation` was captured. Call under the state lock."""
    return read_generation() == generation and read_url() == url


def save_config(url):
    write_private(CONFIG_FILE, lambda f: f.write(url + "\n"))


def check(url, hours, local_tz, save=False):
    """Validate a feed: download, parse, count meetings. With save=True, also
    persist the address and prime the caches from this same download.
    Prints {"ok": true, "meetings": N} or {"ok": false, "message": "..."}."""
    if not url.lower().startswith("https://"):
        print(json.dumps({"ok": False, "message": "The address must start with https://"}))
        return 0
    if not valid_url(url):
        print(json.dumps({"ok": False, "message": "That is not a valid address."}))
        return 0
    generation = read_generation()  # before any work: a later disconnect voids this one
    try:
        text = download(url)
    except Exception as exc:
        print(json.dumps({"ok": False, "message": describe_error(exc)}))
        return 0
    now = dt.datetime.now(UTC)
    window = 24 * 7 if save else hours
    try:
        events = occurrences(text, now, window, local_tz, owner_from_url(url))
    except Exception as exc:
        # The exception quotes feed content, so it goes to the log, not the UI.
        print(f"{APP}: parse failed: {exc}", file=sys.stderr)
        print(json.dumps({"ok": False, "message": "The calendar downloaded but could not be read."}))
        return 0
    if save:
        try:
            with state_lock():
                if read_generation() != generation:
                    print(json.dumps({"ok": False, "message":
                                      "The calendar was disconnected while it was being "
                                      "connected. Connect it again if you still want it."}))
                    return 0
                write_cache(text)
                write_events(events)
                save_config(url)  # last: a half-written state never looks configured
        except OSError as exc:
            print(json.dumps({"ok": False, "message":
                              f"The calendar works, but the address could not be saved ({exc}). "
                              "Check that ~/.config is writable, then try again."}))
            return 0
    horizon = now + dt.timedelta(hours=hours)
    soon = [e for e in events if dt.datetime.fromisoformat(e["start"]) <= horizon]
    print(json.dumps({"ok": True, "meetings": len(soon)}))
    return 0


def write_cache(text):
    write_private(CACHE_FILE, lambda f: f.write(text))


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
        write_private(EVENTS_FILE, lambda f: json.dump(events, f))
    except OSError as exc:
        print(f"{APP}: could not write events cache: {exc}", file=sys.stderr)


def disconnect():
    """Forget the connected calendar: the address first, then the caches, all
    under the state lock. Prints {"ok": true} or {"ok": false, "message"}; a
    file that cannot be removed is reported, never silently kept."""
    failures = []
    try:
        with state_lock():
            try:
                bump_generation()  # first: voids every refresh/connect already in flight
            except OSError as exc:
                failures.append(f"generation: {exc.strerror or exc}")
            for path in (CONFIG_FILE, CACHE_FILE, EVENTS_FILE):
                try:
                    os.unlink(path)
                except FileNotFoundError:
                    pass
                except OSError as exc:
                    failures.append(f"{os.path.basename(path)}: {exc.strerror or exc}")
    except OSError as exc:
        failures.append(f"lock: {exc.strerror or exc}")
    if failures:
        print(json.dumps({"ok": False, "message":
                          "Could not remove all saved calendar data (" + "; ".join(failures) + ")."}))
        return 1
    print(json.dumps({"ok": True}))
    return 0


def cached_events(now):
    # Without a current connection the cache is leftover data, never shown.
    if read_url() is None:
        return None
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
    ap.add_argument("--check", metavar="URL",
                    help="validate this feed URL and exit; \"-\" reads it from the "
                         f"{URL_ENV} environment variable, which keeps the secret "
                         "address out of the process arguments")
    ap.add_argument("--save", action="store_true",
                    help="with --check: also store the address and prime the caches")
    ap.add_argument("--disconnect", action="store_true",
                    help="forget the address and cached calendar, then exit")
    ap.add_argument("--cached", action="store_true",
                    help="answer from the local cache only, never the network")
    ap.add_argument("--tz", help="local IANA zone (default: system)")
    args = ap.parse_args(argv)

    local_tz = ZoneInfo(args.tz) if args.tz else dt.datetime.now().astimezone().tzinfo
    now = dt.datetime.fromisoformat(args.now) if args.now else dt.datetime.now(UTC)
    if now.tzinfo is None:
        now = now.replace(tzinfo=local_tz)

    if args.disconnect:
        return disconnect()

    if args.check:
        if args.check == "-":
            args.check = os.environ.pop(URL_ENV, "")
        return check(args.check, max(1, min(args.hours, 24 * 7)), local_tz, args.save)

    if args.cached:
        events = cached_events(now)
        if events is None:
            print(json.dumps({"events": [], "error": "no-cache"}))
        else:
            print(json.dumps({"events": events, "stale": False}))
        return 0

    stale = False
    failure = {}
    if args.ics:
        with open(args.ics, encoding="utf-8") as f:
            text = f.read()
    else:
        generation = read_generation()
        url = read_url()
        if not url:
            print(json.dumps({"events": [], "error": "no-config"}))
            return 0
        try:
            text = download(url)
            with state_lock():
                if still_configured(url, generation):
                    write_cache(text)
        except Exception as exc:  # network, HTTP, size: fall back to the cache
            print(f"{APP}: fetch failed: {exc}", file=sys.stderr)
            text = read_cache()
            stale = True
            if isinstance(exc, FeedTooLarge):
                # Not a passing outage: the cached meetings are still shown,
                # but the error is reported so the user hears about it.
                failure = {"error": "feed-too-large", "message": describe_error(exc)}
            if text is None:
                print(json.dumps({"events": [], "error": "fetch-failed",
                                  "message": describe_error(exc), **failure}))
                return 0

    try:
        events = occurrences(text, now, max(1, min(args.hours, 24 * 7)), local_tz,
                             owner_from_url(read_url()) if not args.ics else "")
    except Exception as exc:
        print(f"{APP}: parse failed: {exc}", file=sys.stderr)
        print(json.dumps({"events": [], "error": "parse-failed"}))
        return 0

    if not args.ics and not stale:
        with state_lock():
            if still_configured(url, generation):
                write_events(events)
    print(json.dumps({"events": events, "stale": stale, **failure}))
    return 0


if __name__ == "__main__":
    sys.exit(main())

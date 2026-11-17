.pragma library

// Pure helpers for BarWidget.qml, unit tested by tests/model.test.js.
// Plain JS only: Qt.* is not reliably available inside a .pragma library.

var icon = ""   // nf-fa-calendar
var python = "/usr/bin/python3"

function localPath(url) {
  var s = String(url || "")
  if (s.indexOf("file://") !== 0) return ""
  try { return decodeURIComponent(s.slice("file://".length)) } catch (e) { return "" }
}

// -I keeps a user's PYTHONPATH or a planted module from being imported; the
// system python is used because a mise/pyenv python3 may lack python-dateutil.
function fetchCommand(script, lookaheadHours, cached) {
  var cmd = [python, "-I", script, "--hours", String(clampInt(lookaheadHours, 1, 168, 12))]
  if (cached) cmd.push("--cached")
  return cmd
}

function clampInt(value, min, max, fallback) {
  var n = Math.round(Number(value))
  if (!isFinite(n)) return fallback
  return Math.min(max, Math.max(min, n))
}

// -> { events: [{title, startMs, endMs, zoomUrl}], error: "", message: "", stale: bool }
function parseOutput(text) {
  var result = { events: [], error: "", message: "", stale: false }
  var data
  try { data = JSON.parse(String(text || "").trim()) } catch (e) {
    result.error = "bad-output"
    return result
  }
  if (!data || typeof data !== "object") { result.error = "bad-output"; return result }
  result.error = typeof data.error === "string" ? data.error : ""
  result.message = typeof data.message === "string" ? data.message : ""
  result.stale = data.stale === true
  var list = Array.isArray(data.events) ? data.events : []
  for (var i = 0; i < list.length; i++) {
    var ev = list[i] || {}
    var startMs = Date.parse(ev.start)
    var endMs = Date.parse(ev.end)
    if (!isFinite(startMs) || !isFinite(endMs)) continue
    var url = String(ev.zoomUrl || "")
    if (!/^(https:\/\/[A-Za-z0-9.-]*zoom\.us\/|zoommtg:\/\/)/i.test(url)) url = ""
    result.events.push({
      title: String(ev.title || "Zoom meeting").replace(/[\r\n\t]+/g, " ").trim(),
      startMs: startMs, endMs: endMs, zoomUrl: url,
      response: /^(accepted|tentative|needs-action)$/.test(ev.response) ? ev.response : ""
    })
  }
  result.events.sort(function(a, b) { return a.startMs - b.startMs })
  return result
}

// The meeting to show: the most recently started one still running (so a
// back-to-back meeting takes over as soon as it starts), else the next one
// starting within the lookahead window, else null.
function pick(events, nowMs, lookaheadHours) {
  var horizon = nowMs + clampInt(lookaheadHours, 1, 168, 12) * 3600 * 1000
  var current = null
  for (var i = 0; i < events.length; i++) {
    var ev = events[i]
    if (ev.endMs <= nowMs) continue
    if (ev.startMs <= nowMs) { current = ev; continue }
    if (current) return current
    return ev.startMs <= horizon ? ev : null
  }
  return current
}

function truncate(title, maxLength) {
  var max = clampInt(maxLength, 5, 120, 40)
  var s = String(title || "")
  return s.length > max ? s.slice(0, max - 1).replace(/\s+$/, "") + "…" : s
}

// When the meeting happens, for the bar: "Now", "14:30", or "Tue 14:30".
function whenText(ev, nowMs) {
  if (ev.startMs <= nowMs) return "Now"
  var days = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]
  var d = new Date(ev.startMs)
  var sameDay = d.toDateString() === new Date(nowMs).toDateString()
  return (sameDay ? "" : days[d.getDay()] + " ") + clock(ev.startMs)
}

function label(ev, maxLength, nowMs) {
  if (!ev) return ""
  var when = nowMs === undefined ? "" : whenText(ev, nowMs) + "  "
  return icon + "  " + when + truncate(ev.title, maxLength)
}

// RSVP glyph and wording. Unknown ("") shows nothing.
function responseMark(response) {
  return response === "accepted" ? "✓" : response === "tentative" ? "?"
    : response === "needs-action" ? "·" : ""
}

function responseText(response) {
  return response === "accepted" ? "Accepted" : response === "tentative" ? "Maybe"
    : response === "needs-action" ? "Not answered yet" : ""
}

function pad(n) { return (n < 10 ? "0" : "") + n }

function clock(ms) {
  var d = new Date(ms)
  return pad(d.getHours()) + ":" + pad(d.getMinutes())
}

function tooltip(ev, nowMs, stale) {
  if (!ev) return ""
  var when
  if (ev.startMs <= nowMs) {
    when = "Now, until " + clock(ev.endMs)
  } else {
    var mins = Math.ceil((ev.startMs - nowMs) / 60000)
    var rel = mins < 60 ? "in " + mins + " min"
      : "in " + Math.floor(mins / 60) + " h" + (mins % 60 ? " " + (mins % 60) + " min" : "")
    var day = new Date(ev.startMs).toDateString() === new Date(nowMs).toDateString() ? "" : "tomorrow "
    when = day + clock(ev.startMs) + "–" + clock(ev.endMs) + " (" + rel + ")"
  }
  var lines = [ev.title, when]
  if (responseText(ev.response)) lines.push(responseText(ev.response))
  if (ev.zoomUrl) lines.push("Click to join")
  if (stale) lines.push("Offline: showing cached calendar")
  return lines.join("\n")
}

function errorTooltip(error, message) {
  if (error === "no-config")
    return "Zoom events: no calendar configured.\nClick to add your Google Calendar secret iCal address."
  if (error === "fetch-failed")
    return "Zoom events: could not download the calendar" + (message ? "\n" + message : "")
  if (error) return "Zoom events: could not read the calendar (" + error + ")"
  return ""
}

var calendarUrl = "https://calendar.google.com/calendar/r"
var calendarSettingsUrl = "https://calendar.google.com/calendar/r/settings"

// Commands a left click runs, so every state does something visible: join the
// meeting or open the calendar. "no-config" returns nothing: the widget opens
// its setup panel instead.
function clickCommands(meeting, error, message) {
  if (meeting && meeting.zoomUrl) return [["xdg-open", meeting.zoomUrl]]
  if (error === "no-config") return []
  if (error) return [
    ["notify-send", "-a", "Zoom events", "Zoom events", errorTooltip(error, message)],
    ["xdg-open", calendarUrl]
  ]
  return [["xdg-open", calendarUrl]]
}

// A pasted secret iCal address, trimmed; "" when it is not a usable https URL.
function normalizeIcsUrl(text) {
  var s = String(text || "").trim()
  return /^https:\/\/[^\s]+$/i.test(s) ? s : ""
}

// Writes the URL to ~/.config/omarchy-zoom-events-google-calendar/ics-url with mode 0600.
// The URL is passed as an argument, never interpolated into the script.
function saveCommand(url) {
  return ["/bin/sh", "-c",
    'umask 077; d="${XDG_CONFIG_HOME:-$HOME/.config}/omarchy-zoom-events-google-calendar"; ' +
    'mkdir -p "$d" && printf "%s\\n" "$1" > "$d/ics-url.tmp" && mv -f "$d/ics-url.tmp" "$d/ics-url"',
    "sh", url]
}

// Validates a feed URL without saving it; prints one JSON line.
function checkCommand(script, url) {
  return [python, "-I", script, "--check", url]
}

// -> { ok: bool, message: string }. Never throws: a crash or garbage output
// becomes a readable failure.
function parseCheck(text, exitCode, stderr) {
  var data
  try { data = JSON.parse(String(text || "").trim()) } catch (e) { data = null }
  if (!data || typeof data !== "object") {
    var detail = String(stderr || "").trim().split("\n").pop()
    return { ok: false, meetings: 0, message: "The check failed unexpectedly" +
      (exitCode ? " (exit " + exitCode + ")" : "") + (detail ? ": " + detail : ".") }
  }
  if (data.ok === true) return { ok: true, meetings: Number(data.meetings) || 0, message: "" }
  return { ok: false, meetings: 0, message: String(data.message || "The calendar could not be loaded.") }
}

function successText(meetings) {
  return meetings === 0
    ? "Connected. No Zoom meetings in the next 24 hours."
    : "Connected. Found " + meetings + " Zoom meeting" + (meetings === 1 ? "" : "s") + " in the next 24 hours."
}

// Commands that forget the connected calendar: the address and the cached copy.
function disconnectCommand() {
  return ["/bin/sh", "-c",
    'rm -f "${XDG_CONFIG_HOME:-$HOME/.config}/omarchy-zoom-events-google-calendar/ics-url" ' +
    '"${XDG_CACHE_HOME:-$HOME/.cache}/omarchy-zoom-events-google-calendar/calendar.ics"']
}

// "Today", "Tomorrow", or e.g. "Thu 20 Nov".
function dayLabel(ms, nowMs) {
  var d = new Date(ms), n = new Date(nowMs)
  if (d.toDateString() === n.toDateString()) return "Today"
  var t = new Date(nowMs)
  t.setDate(t.getDate() + 1)
  if (d.toDateString() === t.toDateString()) return "Tomorrow"
  var days = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]
  var months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
  return days[d.getDay()] + " " + d.getDate() + " " + months[d.getMonth()]
}

// The next `count` (1-10) meetings that have not ended, across as many days as
// it takes, as rows: {kind:"day", text} headers and {kind:"meeting", ...}.
// When today is over, tomorrow's meetings simply come next, and so on.
function agenda(events, nowMs, count) {
  var max = clampInt(count, 1, 10, 5)
  var rows = [], lastDay = "", shown = 0
  for (var i = 0; i < (events || []).length && shown < max; i++) {
    var ev = events[i]
    if (ev.endMs <= nowMs) continue
    var day = new Date(ev.startMs <= nowMs ? nowMs : ev.startMs).toDateString()
    if (day !== lastDay) {
      rows.push({ kind: "day", text: dayLabel(ev.startMs <= nowMs ? nowMs : ev.startMs, nowMs) })
      lastDay = day
    }
    rows.push({
      kind: "meeting", title: ev.title, zoomUrl: ev.zoomUrl,
      time: clock(ev.startMs) + "–" + clock(ev.endMs), running: ev.startMs <= nowMs,
      response: ev.response || ""
    })
    shown++
  }
  return rows
}

// Connection status line for the panel header.
// -> { state: "loading"|"ok"|"none"|"error", text, detail }
function status(loaded, error, message, stale) {
  if (!loaded) return { state: "loading", text: "Connecting…", detail: "" }
  if (error === "no-config") return { state: "none", text: "Not connected",
    detail: "Click the cogwheel to connect your Google Calendar." }
  if (error === "fetch-failed") return { state: "error", text: "Could not reach the calendar",
    detail: (message || "") + " Click the cogwheel to reconnect." }
  if (error) return { state: "error", text: "Could not read the calendar",
    detail: "Click the cogwheel to check the address (" + error + ")." }
  return { state: "ok", text: "Connected", detail: stale ? "Offline: showing the cached calendar." : "" }
}

// Named text sizes offered in the panel's Display settings.
var textSizeValues = ["small", "medium", "large", "xlarge"]

function knownTextSize(value) {
  return textSizeValues.indexOf(value) >= 0 ? value : "medium"
}

// ---------- Theme-safe colours ----------
// Omarchy themes range from near-black to white, and their accent / red are not
// guaranteed to be legible as small text on the popup surface. readable() nudges
// a colour toward the popup's text colour until it reaches the contrast ratio.

function parseHex(color) {
  var m = /^#([0-9a-f]{2})?([0-9a-f]{2})([0-9a-f]{2})([0-9a-f]{2})$/i.exec(String(color || "").trim())
  return m ? [parseInt(m[2], 16), parseInt(m[3], 16), parseInt(m[4], 16)] : null
}

function luminance(rgb) {
  var c = rgb.map(function(v) {
    var s = v / 255
    return s <= 0.03928 ? s / 12.92 : Math.pow((s + 0.055) / 1.055, 2.4)
  })
  return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2]
}

function contrast(a, b) {
  var la = luminance(a), lb = luminance(b)
  return (Math.max(la, lb) + 0.05) / (Math.min(la, lb) + 0.05)
}

function hex(rgb) {
  return "#" + rgb.map(function(v) {
    var h = Math.round(v).toString(16)
    return h.length < 2 ? "0" + h : h
  }).join("")
}

// -> "#rrggbb" of `color`, blended toward `text` just far enough to reach `min`
// contrast against `bg`. Unparseable input is returned unchanged.
function readable(color, bg, text, min) {
  var c = parseHex(color), b = parseHex(bg), t = parseHex(text)
  if (!c || !b || !t) return color
  var target = min || 4.5
  for (var i = 0; i <= 20; i++) {
    var k = i / 20
    var mixed = [0, 1, 2].map(function(n) { return c[n] + (t[n] - c[n]) * k })
    if (contrast(mixed, b) >= target) return hex(mixed)
  }
  return text
}

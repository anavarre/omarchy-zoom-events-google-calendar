// Unit tests for Model.js: node --test tests/model.test.js
"use strict"
const test = require("node:test")
const assert = require("node:assert/strict")
const fs = require("node:fs")
const path = require("node:path")
const vm = require("node:vm")

const source = fs.readFileSync(path.join(__dirname, "..", "Model.js"), "utf8")
const M = vm.createContext({})
vm.runInContext(source.replace(/^\.pragma.*$/m, ""), M, { filename: "Model.js" })
const plain = (v) => JSON.parse(JSON.stringify(v))

const T = (iso) => Date.parse(iso)
const out = JSON.stringify({ stale: true, events: [
  { title: "B", start: "2026-11-17T15:00:00+01:00", end: "2026-11-17T16:00:00+01:00", zoomUrl: "https://x.zoom.us/j/2" },
  { title: "A", start: "2026-11-17T14:00:00+01:00", end: "2026-11-17T15:00:00+01:00", zoomUrl: "https://x.zoom.us/j/1" },
  { title: "Evil", start: "2026-11-17T20:00:00+01:00", end: "2026-11-17T21:00:00+01:00", zoomUrl: "file:///etc/passwd" },
  { title: "Broken", start: "nope", end: "nope" },
] })

test("parseOutput sorts, drops bad dates and unsafe urls", () => {
  const r = M.parseOutput(out)
  assert.equal(r.stale, true)
  assert.deepEqual(plain(r.events.map((e) => e.title)), ["A", "B", "Evil"])
  assert.equal(r.events[2].zoomUrl, "")
  assert.equal(M.parseOutput("garbage").error, "bad-output")
  assert.equal(M.parseOutput('{"events":[],"error":"no-config"}').error, "no-config")
})

test("pick prefers the running meeting, then the next within lookahead", () => {
  const ev = M.parseOutput(out).events
  assert.equal(M.pick(ev, T("2026-11-17T13:00:00+01:00"), 12).title, "A")
  assert.equal(M.pick(ev, T("2026-11-17T14:30:00+01:00"), 12).title, "A")
  assert.equal(M.pick(ev, T("2026-11-17T15:00:00+01:00"), 12).title, "B")
  assert.equal(M.pick(ev, T("2026-11-17T10:00:00+01:00"), 2), null)
  assert.equal(M.pick(ev, T("2026-11-17T22:00:00+01:00"), 12), null)
})

test("back-to-back overlap switches to the newer meeting", () => {
  const ev = [
    { title: "Long", startMs: 0, endMs: 100 },
    { title: "Next", startMs: 50, endMs: 150 },
  ]
  assert.equal(M.pick(ev, 60, 12).title, "Next")
  assert.equal(M.pick(ev, 10, 12).title, "Long")
})

test("label and truncate", () => {
  assert.equal(M.label({ title: "Design review" }, 40), "  Design review")
  assert.equal(M.truncate("abcdefghij", 6), "abcde…")
  assert.equal(M.label(null, 40), "")
})

test("label with time", () => {
  const now = new Date(2026, 10, 17, 9, 0).getTime()
  const at = (d, h, m) => new Date(2026, 10, d, h, m).getTime()
  const ev = (s, e) => ({ title: "Standup", startMs: s, endMs: e })
  assert.equal(M.label(ev(at(17, 14, 30), at(17, 15, 0)), 40, now), M.icon + "  14:30  Standup")
  assert.equal(M.label(ev(at(17, 8, 30), at(17, 9, 30)), 40, now), M.icon + "  Now  Standup")
  assert.equal(M.label(ev(at(18, 10, 0), at(18, 11, 0)), 40, now), M.icon + "  Wed 10:00  Standup")
})

test("fetchCommand clamps hours", () => {
  assert.deepEqual(plain(M.fetchCommand("/p/fetch.py", 999)), ["/usr/bin/python3", "-I", "/p/fetch.py", "--hours", "168"])
  assert.deepEqual(plain(M.fetchCommand("/p/fetch.py", "x")), ["/usr/bin/python3", "-I", "/p/fetch.py", "--hours", "12"])
})

test("tooltip", () => {
  const ev = { title: "A", startMs: T("2026-11-17T14:00:00Z"), endMs: T("2026-11-17T14:30:00Z"), zoomUrl: "https://x.zoom.us/j/1" }
  assert.match(M.tooltip(ev, T("2026-11-17T13:15:00Z"), false), /in 45 min/)
  assert.match(M.tooltip(ev, T("2026-11-17T14:10:00Z"), true), /^A\nNow, until .*\nClick to join\nOffline/)
})

test("clickCommands covers every state", () => {
  assert.deepEqual(plain(M.clickCommands({ zoomUrl: "https://x.zoom.us/j/1" }, "")), [["xdg-open", "https://x.zoom.us/j/1"]])
  assert.deepEqual(plain(M.clickCommands(null, "no-config")), [])
  assert.equal(plain(M.clickCommands(null, "fetch-failed"))[0][0], "notify-send")
  assert.deepEqual(plain(M.clickCommands(null, "")), [["xdg-open", M.calendarUrl]])
})

test("normalizeIcsUrl accepts only https URLs", () => {
  assert.equal(M.normalizeIcsUrl("  https://calendar.google.com/calendar/ical/x/basic.ics \n"), "https://calendar.google.com/calendar/ical/x/basic.ics")
  assert.equal(M.normalizeIcsUrl("http://example.com/a.ics"), "")
  assert.equal(M.normalizeIcsUrl("https://a b"), "")
  assert.equal(M.normalizeIcsUrl(""), "")
})

test("saveCommand passes the URL as an argument", () => {
  const cmd = plain(M.saveCommand("https://x/$(rm -rf ~)"))
  assert.equal(cmd[0], "/bin/sh")
  assert.equal(cmd[cmd.length - 1], "https://x/$(rm -rf ~)")
  assert.ok(!cmd[2].includes("https://x"))
})

test("parseCheck reads ok and failure results", () => {
  const good = M.parseCheck('{"ok":true,"meetings":3}', 0, "")
  assert.equal(good.ok, true)
  assert.equal(good.meetings, 3)
  const bad = M.parseCheck('{"ok":false,"message":"HTTP 404"}', 0, "")
  assert.equal(bad.ok, false)
  assert.equal(bad.message, "HTTP 404")
})

test("parseCheck turns a crash into a readable message", () => {
  const r = M.parseCheck("", 1, "Traceback\nModuleNotFoundError: dateutil")
  assert.equal(r.ok, false)
  assert.match(r.message, /exit 1/)
  assert.match(r.message, /dateutil/)
})

test("successText pluralizes", () => {
  assert.match(M.successText(0), /No Zoom meetings/)
  assert.match(M.successText(1), /1 Zoom meeting in/)
  assert.match(M.successText(2), /2 Zoom meetings/)
})

test("errorTooltip includes the fetch failure message", () => {
  assert.match(M.errorTooltip("fetch-failed", "HTTP 404"), /HTTP 404/)
})

test("agenda lists upcoming meetings across days, capped by count", () => {
  const now = T("2026-11-17T18:00:00+01:00")
  const ev = (title, s, e) => ({ title, startMs: T(s), endMs: T(e), zoomUrl: "" })
  const events = [
    ev("Done", "2026-11-17T09:00:00+01:00", "2026-11-17T10:00:00+01:00"),
    ev("Evening", "2026-11-17T19:00:00+01:00", "2026-11-17T20:00:00+01:00"),
    ev("Next day", "2026-11-18T09:00:00+01:00", "2026-11-18T10:00:00+01:00"),
    ev("Later", "2026-11-20T09:00:00+01:00", "2026-11-20T10:00:00+01:00"),
  ]
  const rows = plain(M.agenda(events, now, 3))
  assert.deepEqual(rows.map((r) => r.kind === "day" ? r.text : r.title),
    ["Today", "Evening", "Tomorrow", "Next day", "Fri 20 Nov", "Later"])
  assert.equal(rows.filter((r) => r.kind === "meeting").length, 3)
  assert.equal(plain(M.agenda(events, now, 1)).filter((r) => r.kind === "meeting").length, 1)
  // today over: tomorrow comes first
  const late = plain(M.agenda(events.slice(2), now, 5))
  assert.equal(late[0].text, "Tomorrow")
})

test("agenda clamps count to 1..10 and defaults to 5", () => {
  const events = Array.from({ length: 20 }, (_, i) => ({ title: "m" + i, startMs: 1e12 + i * 7200000, endMs: 1e12 + i * 7200000 + 3600000, zoomUrl: "" }))
  const n = (c) => plain(M.agenda(events, 0, c)).filter((r) => r.kind === "meeting").length
  assert.equal(n(undefined), 5)
  assert.equal(n(0), 1)
  assert.equal(n(99), 10)
})

test("status reflects connection state", () => {
  assert.equal(M.status(false, "", "", false).state, "loading")
  assert.equal(M.status(true, "no-config", "", false).state, "none")
  assert.equal(M.status(true, "fetch-failed", "boom", false).state, "error")
  assert.equal(M.status(true, "", "", false).state, "ok")
  assert.equal(M.status(true, "", "", false).text, "Connected")
})

test("RSVP response is parsed, shown in tooltip and agenda", () => {
  const out = M.parseOutput(JSON.stringify({ events: [
    { title: "A", start: "2026-11-17T14:00:00Z", end: "2026-11-17T15:00:00Z", response: "tentative" },
    { title: "B", start: "2026-11-17T16:00:00Z", end: "2026-11-17T17:00:00Z", response: "bogus" }] }))
  assert.equal(out.events[0].response, "tentative")
  assert.equal(out.events[1].response, "")
  assert.match(M.tooltip(out.events[0], 0, false), /Maybe/)
  assert.equal(M.responseMark("accepted"), "✓")
  assert.equal(M.agenda(out.events, 0, 5).find((r) => r.kind === "meeting").response, "tentative")
})

test("knownTextSize falls back to medium", () => {
  for (const size of ["small", "medium", "large", "xlarge"]) assert.equal(M.knownTextSize(size), size)
  for (const size of ["huge", "", null, 3, "Medium"]) assert.equal(M.knownTextSize(size), "medium")
})

test("readable() lifts low-contrast theme colours and leaves good ones alone", () => {
  // Rose Pine accent / Miasma red on their backgrounds fall below 4.5:1 raw.
  const cases = [["#d7827e", "#faf4ed", "#575279"], ["#685742", "#222222", "#c2c2b0"], ["#c97c7c", "#222222", "#c2c2b0"]]
  for (const [c, bg, fg] of cases) {
    const out = M.readable(c, bg, fg, 4.5)
    assert.ok(M.contrast(M.parseHex(out), M.parseHex(bg)) >= 4.5, `${c} on ${bg} -> ${out}`)
  }
  assert.equal(M.readable("#ffffff", "#000000", "#ffffff", 4.5), "#ffffff")
  assert.equal(M.readable("#80ff0000", "#000000", "#ffffff", 1), "#ff0000")
  assert.equal(M.readable("not-a-color", "#000", "#fff", 4.5), "not-a-color")
})

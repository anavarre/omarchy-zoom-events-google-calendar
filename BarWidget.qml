import QtQuick
import Quickshell
import Quickshell.Io
import qs.Ui
import "Model.js" as Model

// Calendar icon + title of the current or next Zoom meeting, read from a
// Google Calendar secret iCal feed by scripts/fetch.py. Shows a dimmed icon when
// nothing is coming up; left click always toggles the panel, right click refreshes.
BarWidget {
  id: root
  moduleName: "anavarre.zoom-events-google-calendar"

  readonly property string script: Model.localPath(Qt.resolvedUrl("scripts/fetch.py"))
  readonly property int refreshMinutes: Model.clampInt(root.setting("refreshMinutes", 10), 1, 120, 10)
  readonly property int lookaheadHours: Model.clampInt(root.setting("lookaheadHours", 12), 1, 168, 12)
  readonly property int maxTitleLength: Model.clampInt(root.setting("maxTitleLength", 40), 5, 120, 40)

  // Minutes before a meeting starts during which the bar takes the theme's
  // urgent colour. 0 turns the alert off.
  readonly property int alertMinutes: Model.clampInt(root.setting("alertMinutes", 5), 0, 60, 5)

  readonly property int maxMeetings: Model.clampInt(root.setting("maxMeetings", 5), 1, 10, 5)
  // The panel lists several days ahead; the bar label still honours lookaheadHours.
  readonly property int fetchHours: 168

  property bool loaded: false
  property var events: []
  property string error: ""
  property string errorMessage: ""
  property bool stale: false
  property real lastFetchMs: 0
  property real nowMs: Date.now()

  // A meeting about to start takes over from the one still running, so it can
  // alert you while you wrap up.
  readonly property var meeting: Model.upcoming(root.events, root.nowMs, root.alertMinutes)
    || Model.pick(root.events, root.nowMs, root.lookaheadHours)

  // Bumped after every finished load, so the panel can tell when a calendar it
  // just connected is actually on screen.
  property int loadSerial: 0

  // Connection progress lives in the panel; the bar mirrors it so it stays
  // visible while the panel is closed.
  readonly property string connectPhase: panelLoader.item ? panelLoader.item.phase : "idle"
  readonly property bool connecting: root.connectPhase === "connecting" || root.connectPhase === "finishing"
  readonly property bool connectFailed: panelLoader.item
    ? panelLoader.item.failed === true && root.loaded && root.error === "no-config" : false
  readonly property string connectMessage: panelLoader.item ? panelLoader.item.errorText : ""

  readonly property bool opened: panelLoader.item ? panelLoader.item.opened === true : false
  readonly property bool popoutSwitchClosing: panelLoader.item ? panelLoader.item.popoutSwitchClosing === true : false

  function open() { if (panelLoader.item) panelLoader.item.open() }
  function close() { if (panelLoader.item) panelLoader.item.close() }
  function toggle() { if (panelLoader.item) panelLoader.item.toggle() }
  function closeForPopoutSwitch() { if (panelLoader.item) panelLoader.item.closeForPopoutSwitch() }

  function injectPanel() {
    if (!panelLoader.item) return
    panelLoader.item.bar = root.bar
    panelLoader.item.anchorItem = button
    panelLoader.item.hostWidget = root
    panelLoader.item.textSize = Model.knownTextSize(root.setting("textSize", "medium"))
  }

  // The panel's Display section writes back through here: the widget owns the
  // shell.json entry, so the whole entry is rewritten with the one key changed.
  function saveSetting(name, value) {
    if (!root.bar || !root.bar.shell
        || typeof root.bar.shell.updateEntryInline !== "function") return
    var entry = { id: root.moduleName }
    for (var key in root.settings) if (key !== "id") entry[key] = root.settings[key]
    entry[name] = value
    root.bar.shell.updateEntryInline(root.moduleName, entry)
  }

  function saveTextSize(size) { root.saveSetting("textSize", size) }

  onBarChanged: injectPanel()

  Loader {
    id: panelLoader
    active: true
    source: Qt.resolvedUrl("Panel.qml")
    visible: false
    onLoaded: {
      root.injectPanel()
      Qt.callLater(root.injectPanel)
    }
  }

  Connections {
    target: panelLoader.item
    function onSaved() { root.reload() }
    function onDisconnectRequested() { root.beginDisconnect() }
  }

  implicitWidth: button.visible ? button.implicitWidth : 0
  implicitHeight: button.implicitHeight

  // True while the instant cache-only run is in flight.
  property bool cachedRun: false
  // The calendar changed on disk while a load was running: that load is stale.
  property bool rerun: false

  function refresh() {
    if (fetcher.running || root.disconnecting || root.script === "") return
    root.lastFetchMs = Date.now()
    root.cachedRun = false
    fetcher.command = Model.fetchCommand(root.script, root.fetchHours, false)
    fetcher.running = true
  }

  // The feed download can take tens of seconds, so show the cached meetings
  // first and refresh from the network right after.
  function start() {
    if (root.script === "" || root.disconnecting) return
    root.cachedRun = true
    fetcher.command = Model.fetchCommand(root.script, root.fetchHours, true)
    fetcher.running = true
  }

  // The calendar was connected or changed. Never drop this request because a
  // load happens to be running: abandon that load and start over, so the new
  // calendar shows at once instead of at the next timer tick.
  function reload() {
    if (root.script === "" || root.disconnecting) return
    if (fetcher.running) {
      root.rerun = true
      if (typeof fetcher.signal === "function") fetcher.signal(15)
      return
    }
    root.start()
  }

  // The user disconnected. An in-flight refresh would write the calendar back
  // into the cache after it is removed, so cancel it and wait until it has
  // exited before wiping anything.
  property bool disconnecting: false

  function beginDisconnect() {
    root.disconnecting = true
    root.rerun = false
    if (!fetcher.running) { root.wipe(); return }
    if (typeof fetcher.signal === "function") fetcher.signal(15)
    // Otherwise it ends on its own (socket timeout); fetcher.onExited wipes.
  }

  function wipe() {
    wiper.command = Model.disconnectCommand()
    wiper.running = true
  }

  Process {
    id: wiper
    running: false
    onExited: function() {
      root.disconnecting = false
      root.clearCalendar()
      if (panelLoader.item && typeof panelLoader.item.finishDisconnect === "function")
        panelLoader.item.finishDisconnect()
    }
  }

  function clearCalendar() {
    root.events = []
    root.error = "no-config"
    root.errorMessage = ""
    root.stale = false
    root.loaded = true
    root.loadSerial++
    root.reload()
  }

  Component.onCompleted: root.start()

  Process {
    id: fetcher
    running: false
    stdout: StdioCollector { id: out; waitForEnd: true }
    stderr: StdioCollector { id: err; waitForEnd: true }
    onExited: function(exitCode) {
      if (root.disconnecting) { root.wipe(); return }
      if (root.rerun) {
        root.rerun = false
        root.cachedRun = false
        root.start()
        return
      }
      if (root.cachedRun) {
        root.cachedRun = false
        var cached = Model.parseOutput(out.text)
        if (cached.error === "") {
          root.events = cached.events
          // The cache is a good calendar, so earlier errors no longer apply.
          root.error = ""
          root.errorMessage = ""
          root.stale = false
          root.loaded = true
          root.nowMs = Date.now()
        }
        root.loadSerial++
        root.refresh()
        return
      }
      var result = Model.parseOutput(out.text)
      if (exitCode !== 0 && result.error === "") result.error = "exit " + exitCode
      if (err.text) console.warn(root.moduleName + ": " + err.text.trim())
      // Keep the last good list on a hard failure; the script already falls
      // back to its cache for network errors and for a feed over the size limit.
      var tooLarge = result.error === "feed-too-large"
      if (result.error === "" || result.error === "no-config" || (tooLarge && result.stale))
        root.events = result.events
      // Alert once when the feed first goes over the limit, not on every refresh.
      if (tooLarge && root.error !== "feed-too-large" && !notifier.running) {
        notifier.command = Model.notifyCommand("Zoom events",
          Model.errorTooltip(result.error, result.message))
        notifier.running = true
      }
      root.error = result.error
      root.errorMessage = result.message
      root.stale = result.stale
      root.loaded = true
      root.nowMs = Date.now()
      root.loadSerial++
    }
  }

  Process { id: notifier; running: false }

  // The last minute before the start: the widget pulses. Off when alerts are.
  readonly property bool pulsing: root.alertMinutes > 0
    && Model.imminent(root.meeting, root.nowMs, 1)

  // nowMs only ticks every 30s, so tick each second while a start is close
  // (nowMs can be 30s stale, hence 95s) to begin and stop the pulse on time.
  Timer {
    interval: 1000
    repeat: true
    running: root.meeting !== null && root.meeting !== undefined
      && root.meeting.startMs > root.nowMs && root.meeting.startMs - root.nowMs <= 95000
    onTriggered: root.nowMs = Date.now()
  }

  // alwaysRunToEnd finishes the current breath, so it always ends fully opaque.
  SequentialAnimation {
    running: root.pulsing
    loops: Animation.Infinite
    alwaysRunToEnd: true
    NumberAnimation { target: root; property: "opacity"; to: 0.35; duration: 1600; easing.type: Easing.InOutSine }
    NumberAnimation { target: root; property: "opacity"; to: 1; duration: 1600; easing.type: Easing.InOutSine }
  }

  // Re-evaluates which meeting to show every 30s and refetches when due. Using
  // the wall clock (not a long interval timer) also catches up after suspend.
  Timer {
    interval: 30000
    running: true
    repeat: true
    onTriggered: {
      root.nowMs = Date.now()
      if (root.nowMs - root.lastFetchMs >= root.refreshMinutes * 60000) root.refresh()
    }
  }

  WidgetButton {
    id: button
    anchors.fill: parent
    bar: root.bar
    // With no meeting, show a dimmed bare icon so the widget stays visible.
    text: root.connecting ? Model.connectionLabel(root.connectPhase, false)
      : root.meeting ? Model.label(root.meeting, root.maxTitleLength, root.nowMs)
      : root.connectFailed ? Model.connectionLabel("", true) : Model.icon
    dimmed: !root.meeting && !root.connecting
    // Theme-driven: activeColor defaults to the bar's urgent colour.
    active: root.meeting ? Model.imminent(root.meeting, root.nowMs, root.alertMinutes) : false
    tooltipText: root.connecting || (root.connectFailed && !root.meeting)
      ? Model.connectionTooltip(root.connectPhase, root.connectFailed, root.connectMessage)
      : root.meeting ? Model.tooltip(root.meeting, root.nowMs, root.stale,
          root.error === "feed-too-large" ? Model.errorTooltip(root.error, root.errorMessage) : "")
      : Model.errorTooltip(root.error, root.errorMessage)
    onPressed: function(buttonCode) {
      if (buttonCode === Qt.LeftButton) {
        root.toggle()
      } else if (buttonCode === Qt.RightButton) {
        root.refresh()
      }
    }
  }
}

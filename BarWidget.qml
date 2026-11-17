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

  readonly property var meeting: Model.pick(root.events, root.nowMs, root.lookaheadHours)

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
    function onSaved() { root.refresh() }
  }

  implicitWidth: button.visible ? button.implicitWidth : 0
  implicitHeight: button.implicitHeight

  // True while the instant cache-only run is in flight.
  property bool cachedRun: false

  function refresh() {
    if (fetcher.running || root.script === "") return
    root.lastFetchMs = Date.now()
    root.cachedRun = false
    fetcher.command = Model.fetchCommand(root.script, root.fetchHours, false)
    fetcher.running = true
  }

  // The feed download can take tens of seconds, so show the cached meetings
  // first and refresh from the network right after.
  function start() {
    if (root.script === "") return
    root.cachedRun = true
    fetcher.command = Model.fetchCommand(root.script, root.fetchHours, true)
    fetcher.running = true
  }

  Component.onCompleted: root.start()

  Process {
    id: fetcher
    running: false
    stdout: StdioCollector { id: out; waitForEnd: true }
    stderr: StdioCollector { id: err; waitForEnd: true }
    onExited: function(exitCode) {
      if (root.cachedRun) {
        root.cachedRun = false
        var cached = Model.parseOutput(out.text)
        if (cached.error === "") {
          root.events = cached.events
          root.loaded = true
          root.nowMs = Date.now()
        }
        root.refresh()
        return
      }
      var result = Model.parseOutput(out.text)
      if (exitCode !== 0 && result.error === "") result.error = "exit " + exitCode
      if (err.text) console.warn(root.moduleName + ": " + err.text.trim())
      // Keep the last good list on a hard failure; the script already falls
      // back to its cache for network errors.
      if (result.error === "" || result.error === "no-config") root.events = result.events
      root.error = result.error
      root.errorMessage = result.message
      root.stale = result.stale
      root.loaded = true
      root.nowMs = Date.now()
    }
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
    text: root.meeting ? Model.label(root.meeting, root.maxTitleLength, root.nowMs) : Model.icon
    dimmed: !root.meeting
    tooltipText: root.meeting ? Model.tooltip(root.meeting, root.nowMs, root.stale)
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

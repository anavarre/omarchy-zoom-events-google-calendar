import QtQuick
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.Ui
import "Model.js" as Model

// Setup form shown when no calendar is configured: paste the Google Calendar
// secret iCal address, press Enter (or Save) to store it, and the widget
// refetches.
Panel {
  id: root
  moduleName: "anavarre.zoom-events-google-calendar"
  manageIpc: false

  property var anchorItem: null
  property var hostWidget: null

  // idle -> checking (download + parse) -> saving -> idle (back on the main view)
  property string phase: "idle"
  // The main view (status + meetings) is the default; the form lives behind the cogwheel.
  property bool showConfig: false
  readonly property var host: root.hostWidget
  readonly property var agenda: root.host ? Model.agenda(root.host.events, root.host.nowMs, root.host.maxMeetings) : []
  readonly property var status: root.host
    ? Model.status(root.host.loaded, root.host.error, root.host.errorMessage, root.host.stale)
    : Model.status(false, "", "", false)
  readonly property bool connected: root.status.state === "ok"

  readonly property bool busy: phase === "checking" || phase === "saving"
  property string errorText: ""
  property string successText: ""
  readonly property string script: Model.localPath(Qt.resolvedUrl("scripts/fetch.py"))

  signal saved()

  // Named text sizes; every font size in the panel is scaled by the factor.
  property string textSize: "medium"
  readonly property var textSizes: [
    { value: "small", label: "Small", scale: 0.85 },
    { value: "medium", label: "Medium", scale: 1.0 },
    { value: "large", label: "Large", scale: 1.2 },
    { value: "xlarge", label: "Extra large", scale: 1.45 }
  ]
  readonly property int textSizeIndex: {
    for (var i = 0; i < root.textSizes.length; i++)
      if (root.textSizes[i].value === root.textSize) return i
    return 1
  }
  readonly property real textScale: root.textSizes[root.textSizeIndex].scale
  readonly property string textSizeLabel: root.textSizes[root.textSizeIndex].label

  function fs(size) { return Math.round(size * root.textScale) }

  // Shared size for the day headings and the settings headings, a bit larger than
  // caption text and scaled with the user's text size.
  readonly property int headerSize: root.fs(Style.font.caption * 1.2)

  function stepTextSize(delta) {
    var i = Math.max(0, Math.min(root.textSizes.length - 1, root.textSizeIndex + delta))
    var next = root.textSizes[i].value
    if (next === root.textSize) return
    root.textSize = next
    if (root.hostWidget && typeof root.hostWidget.saveTextSize === "function")
      root.hostWidget.saveTextSize(next)
  }

  // The panel sits on the popup surface, so colours come from the popup roles
  // (not the bar's) and accent / urgent are lifted to a readable contrast on it.
  readonly property color foreground: Color.popups.text
  readonly property color accent: Model.readable(Color.accent, Color.popups.background, root.foreground, 4.5)
  readonly property color urgent: Model.readable(Color.urgent, Color.popups.background, root.foreground, 4.5)
  readonly property string fontFamily: bar ? bar.fontFamily : Style.font.family

  function open() { root.controller.show() }
  function close() { root.controller.hide() }

  function switchPanel(direction) {
    if (root.bar && typeof root.bar.switchPanelFrom === "function")
      return root.bar.switchPanelFrom(root.hostWidget || root, direction)
    return false
  }

  property string pendingUrl: ""
  // An outcome arrived while the panel was closed and has not been shown yet.
  property bool unseen: false

  // Desktop notification, used when a background result lands with the panel closed.
  function notify(title, body) {
    notifier.command = ["notify-send", "-a", "Zoom events", title, body]
    notifier.running = true
  }

  Process { id: notifier; running: false; command: [] }

  function save() {
    if (root.busy) return
    var url = Model.normalizeIcsUrl(field.text)
    if (url === "") {
      root.errorText = field.text.trim() === ""
        ? "Paste the secret address in iCal format first."
        : "That does not look like an address. It must start with https://"
      return
    }
    if (root.script === "") {
      root.errorText = "Plugin script not found; reinstall the plugin."
      return
    }
    root.errorText = ""
    root.successText = ""
    root.pendingUrl = url
    root.phase = "checking"
    checker.command = Model.checkCommand(root.script, url)
    checker.running = true
  }

  onOpenedChanged: {
    // A check keeps running in the background while the panel is closed;
    // reopening must show its progress or outcome, not reset it.
    if (opened) {
      root.unseen = false
      // Always land on the main view, unless a check is still running.
      if (!root.busy) root.showConfig = false
      root.focusView()
    } else if (!root.busy && !root.unseen) {
      root.errorText = ""
      root.successText = ""
      root.phase = "idle"
    }
  }

  function focusView() {
    Qt.callLater(function() { (root.showConfig ? field : mainKeys).forceActiveFocus() })
  }

  function toggleConfig() {
    root.showConfig = !root.showConfig
    if (root.showConfig) { root.errorText = ""; root.successText = "" }
    root.focusView()
  }

  function disconnect() {
    if (root.busy) return
    root.errorText = ""
    root.successText = ""
    disconnector.running = true
  }

  Process {
    id: disconnector
    running: false
    command: Model.disconnectCommand()
    onExited: function() {
      root.showConfig = true
      root.saved()
      root.focusView()
    }
  }

  // Step 1: download and parse the feed without saving anything.
  Process {
    id: checker
    running: false
    command: []
    stdout: StdioCollector { id: checkOut; waitForEnd: true }
    stderr: StdioCollector { id: checkErr; waitForEnd: true }
    onExited: function(exitCode) {
      var r = Model.parseCheck(checkOut.text, exitCode, checkErr.text)
      if (!r.ok) {
        root.phase = "idle"
        root.errorText = r.message
        if (!root.opened) {
          root.unseen = true
          root.notify("Calendar could not be connected",
            r.message + "\nClick the calendar widget to fix the address and try again.")
        }
        return
      }
      root.successText = Model.successText(r.meetings)
      root.phase = "saving"
      saver.command = Model.saveCommand(root.pendingUrl)
      saver.running = true
    }
  }

  // Step 2: only a verified address is written to disk.
  Process {
    id: saver
    running: false
    command: []
    stderr: StdioCollector { id: saverErr; waitForEnd: true }
    onExited: function(exitCode) {
      if (exitCode !== 0) {
        root.phase = "idle"
        root.successText = ""
        root.errorText = "The calendar works, but the address could not be saved" +
          (saverErr.text ? ": " + saverErr.text.trim() : ".") +
          " Check that ~/.config is writable, then try again."
        if (!root.opened) {
          root.unseen = true
          root.notify("Calendar could not be saved", root.errorText)
        }
        return
      }
      field.text = ""
      root.saved()
      root.phase = "idle"
      root.showConfig = false
      if (!root.opened) {
        root.notify("Calendar connected", root.successText)
        root.successText = ""
      }
      root.focusView()
    }
  }

  KeyboardPanel {
    id: panel
    anchorItem: root.anchorItem
    owner: root.hostWidget || root
    bar: root.bar
    open: root.opened
    focusTarget: root.showConfig ? field : mainKeys
    contentWidth: panel.fittedContentWidth(Style.space(Math.round(380 * root.textScale)))
    contentHeight: panel.fittedContentHeight(content.implicitHeight)

    Column {
      id: content
      width: parent.width
      spacing: Style.space(8)

      // Keeps keyboard focus (Escape, Tab) while the form is hidden.
      Item {
        id: mainKeys
        width: 0; height: 0
        Keys.onEscapePressed: root.close()
        Keys.onTabPressed: function(event) {
          if (!root.switchPanel(event.modifiers & Qt.ShiftModifier ? -1 : 1)) event.accepted = false
        }
      }

      // Header: connection status.
      Text {
        id: statusText
        width: parent.width - cog.width - Style.space(8)
        // The agenda already shows the calendar is working, so skip "Connected".
        visible: !root.showConfig && !(root.connected && root.agenda.length > 0)
        elide: Text.ElideRight
        text: (root.status.state === "ok" ? "✓ " : root.status.state === "error" ? "✗ " : "") + root.status.text
        color: root.status.state === "error" ? root.urgent
          : root.status.state === "ok" ? root.accent : root.foreground
        font.family: root.fontFamily
        font.pixelSize: root.fs(Style.font.body)
        font.bold: true
      }

      // Settings header: title on the left, close cross top right.
      Item {
        width: parent.width
        height: Math.max(settingsTitle.implicitHeight, closeCross.implicitHeight)
        visible: root.showConfig

        Text {
          id: settingsTitle
          anchors.left: parent.left
          anchors.right: closeCross.left
          anchors.rightMargin: Style.space(8)
          anchors.verticalCenter: parent.verticalCenter
          elide: Text.ElideRight
          text: "Calendar Settings"
          color: root.accent
          font.family: root.fontFamily
          font.pixelSize: root.headerSize
          font.bold: true
        }

        Text {
          id: closeCross
          anchors.right: parent.right
          anchors.verticalCenter: parent.verticalCenter
          text: "✕"
          color: crossArea.containsMouse ? root.accent : root.foreground
          font.family: root.fontFamily
          font.pixelSize: root.fs(Style.font.body)
          MouseArea {
            id: crossArea
            anchors.fill: parent
            anchors.margins: -Style.space(4)
            hoverEnabled: true
            cursorShape: Qt.PointingHandCursor
            onClicked: root.toggleConfig()
          }
        }
      }

      // ---------- Main view ----------
      Text {
        width: parent.width
        visible: !root.showConfig && (root.status.detail !== "" || root.successText !== "")
        text: root.successText !== "" ? root.successText : root.status.detail
        color: root.foreground
        opacity: 0.9
        font.family: root.fontFamily
        font.pixelSize: root.fs(Style.font.caption)
        wrapMode: Text.WordWrap
      }

      Text {
        width: parent.width
        visible: !root.showConfig && root.errorText !== ""
        text: "✗ " + root.errorText
        color: root.urgent
        font.family: root.fontFamily
        font.pixelSize: root.fs(Style.font.caption)
        wrapMode: Text.WordWrap
      }

      Column {
        id: agendaList
        width: parent.width
        visible: !root.showConfig
        spacing: Style.space(4)

        Repeater {
          id: agendaRepeater
          model: root.agenda
          delegate: Item {
            width: content.width
            height: modelData.kind === "day" ? dayText.implicitHeight + Style.space(4) : meetingRow.implicitHeight + Style.space(4)

            Text {
              id: dayText
              visible: modelData.kind === "day"
              anchors.bottom: parent.bottom
              text: modelData.text || ""
              color: root.accent
              font.family: root.fontFamily
              font.pixelSize: root.headerSize
              font.bold: true
            }

            Item {
              id: meetingRow
              visible: modelData.kind === "meeting"
              anchors.verticalCenter: parent.verticalCenter
              width: parent.width
              implicitHeight: Math.max(timeText.implicitHeight, titleText.implicitHeight)

              // Fixed-width time column so titles start at the same x on every row.
              TextMetrics {
                id: timeMetrics
                font: timeText.font
                text: "00:00–00:00"
              }
              Text {
                id: timeText
                anchors.left: parent.left
                // Share the title's baseline so both read as one line at any text size.
                anchors.baseline: titleText.baseline
                width: Math.ceil(timeMetrics.advanceWidth)
                text: modelData.time || ""
                color: root.foreground
                opacity: 0.85
                font.family: root.fontFamily
                font.pixelSize: root.fs(Style.font.caption)
              }
              Text {
                id: titleText
                anchors.left: timeText.right
                anchors.leftMargin: Style.space(8)
                anchors.right: parent.right
                anchors.top: parent.top
                text: (modelData.running ? "● " : "") + (modelData.title || "")
                      + (Model.responseMark(modelData.response) ? "  " + Model.responseMark(modelData.response) : "")
                elide: Text.ElideRight
                color: rowArea.containsMouse && modelData.zoomUrl ? root.accent : root.foreground
                font.family: root.fontFamily
                font.pixelSize: root.fs(Style.font.body)
              }
            }

            MouseArea {
              id: rowArea
              anchors.fill: parent
              enabled: modelData.kind === "meeting" && !!modelData.zoomUrl
              hoverEnabled: true
              cursorShape: Qt.PointingHandCursor
              onClicked: { Qt.openUrlExternally(modelData.zoomUrl); root.close() }
            }
          }
        }

        Text {
          width: parent.width
          visible: root.connected && root.agenda.length === 0
          text: "No upcoming Zoom meetings in the next 7 days."
          color: root.foreground
          opacity: 0.9
          font.family: root.fontFamily
          font.pixelSize: root.fs(Style.font.body)
          wrapMode: Text.WordWrap
        }
      }

      // ---------- Config view ----------
      Column {
        width: parent.width
        visible: root.showConfig
        spacing: Style.space(8)

        Row {
          visible: root.connected
          spacing: Style.space(8)
          Text {
            anchors.verticalCenter: parent.verticalCenter
            text: "✓ A calendar is connected."
            color: root.accent
            font.family: root.fontFamily
            font.pixelSize: root.fs(Style.font.caption)
          }
          Button {
            text: "Disconnect"
            enabled: !root.busy
            foreground: root.foreground
            onClicked: root.disconnect()
          }
        }

        Text {
          width: parent.width
          visible: !root.connected
          text: "Secret address in iCal format"
          color: root.foreground
          font.family: root.fontFamily
          font.pixelSize: root.fs(Style.font.caption)
          wrapMode: Text.WordWrap
        }

        Row {
          width: parent.width
          visible: !root.connected
          spacing: Style.space(6)

          TextField {
            id: field
            width: parent.width - saveButton.width - parent.spacing
            foreground: root.foreground
            // Hidden while the field has focus, shown again when it is left empty.
            placeholderText: activeFocus ? "" : "Enter the URL here..."
            inputMethodHints: Qt.ImhUrlCharactersOnly | Qt.ImhNoAutoUppercase
            enabled: !root.busy
            onAccepted: root.save()
            onTextChanged: root.errorText = ""
            Keys.onEscapePressed: root.close()
            Keys.onTabPressed: function(event) {
              if (!root.switchPanel(event.modifiers & Qt.ShiftModifier ? -1 : 1)) event.accepted = false
            }
          }

          Button {
            id: saveButton
            anchors.verticalCenter: field.verticalCenter
            text: root.connected ? "Reconnect" : "Save"
            enabled: !root.busy
            foreground: root.foreground
            onClicked: root.save()
          }
        }

        // Immediate feedback while the feed downloads.
        Row {
          visible: root.busy
          spacing: Style.space(8)

          Text {
            id: spinner
            text: "◜"
            color: root.accent
            font.family: root.fontFamily
            font.pixelSize: root.fs(Style.font.body)
            anchors.verticalCenter: parent.verticalCenter
            property int frame: 0
            readonly property var frames: ["◜", "◝", "◞", "◟"]
            Timer {
              interval: 120
              running: root.busy
              repeat: true
              onTriggered: { spinner.frame = (spinner.frame + 1) % 4; spinner.text = spinner.frames[spinner.frame] }
            }
          }
          Text {
            text: root.phase === "saving" ? "Saving…" : "Checking the calendar…"
            color: root.foreground
            font.family: root.fontFamily
            font.pixelSize: root.fs(Style.font.caption)
            anchors.verticalCenter: parent.verticalCenter
          }
        }

        Text {
          width: parent.width
          visible: root.errorText !== ""
          text: "✗ " + root.errorText
          color: root.urgent
          font.family: root.fontFamily
          font.pixelSize: root.fs(Style.font.caption)
          wrapMode: Text.WordWrap
        }

        Row {
          visible: !root.connected
          spacing: Style.space(8)

          Text {
            text: "Open Google Calendar settings"
            color: root.accent
            font.family: root.fontFamily
            font.pixelSize: root.fs(Style.font.caption)
            font.underline: true

            MouseArea {
              anchors.fill: parent
              cursorShape: Qt.PointingHandCursor
              onClicked: Qt.openUrlExternally(Model.calendarSettingsUrl)
            }
          }

          Text {
            text: "(Docs)"
            color: root.accent
            font.family: root.fontFamily
            font.pixelSize: root.fs(Style.font.caption)
            font.underline: true

            MouseArea {
              anchors.fill: parent
              cursorShape: Qt.PointingHandCursor
              onClicked: Qt.openUrlExternally("https://support.google.com/calendar/answer/37648?hl=en")
            }
          }
        }
      }

      // ---------- Display ----------
      // Extra breathing room between the settings sections.
      Item { width: 1; height: Style.space(16); visible: root.showConfig }

      Column {
        width: parent.width
        visible: root.showConfig
        spacing: Style.space(8)

        Text {
          width: parent.width
          text: "Display"
          color: root.accent
          font.family: root.fontFamily
          font.pixelSize: root.headerSize
          font.bold: true
        }

        Item {
          width: parent.width
          height: Math.max(textSizeCaption.implicitHeight, textSizeControls.implicitHeight)

          Text {
            id: textSizeCaption
            anchors.left: parent.left
            anchors.verticalCenter: parent.verticalCenter
            width: parent.width - textSizeControls.width - Style.space(8)
            text: "Text size"
            elide: Text.ElideRight
            color: root.foreground
            font.family: root.fontFamily
            font.pixelSize: root.fs(Style.font.body)
          }

          Row {
            id: textSizeControls
            anchors.right: parent.right
            anchors.verticalCenter: parent.verticalCenter
            spacing: Style.space(8)

            PanelActionButton {
              anchors.verticalCenter: parent.verticalCenter
              // nf-fa-minus (U+F068)
              iconText: "\uf068"
              tooltipText: "Smaller"
              enabled: root.textSizeIndex > 0
              foreground: root.foreground
              hoverColor: root.foreground
              fontSize: root.fs(Style.font.bodySmall)
              onClicked: root.stepTextSize(-1)
            }

            Text {
              anchors.verticalCenter: parent.verticalCenter
              width: Math.max(Style.space(Math.round(70 * root.textScale)), implicitWidth)
              horizontalAlignment: Text.AlignHCenter
              text: root.textSizeLabel
              color: root.foreground
              font.family: root.fontFamily
              font.pixelSize: root.fs(Style.font.body)
            }

            PanelActionButton {
              anchors.verticalCenter: parent.verticalCenter
              // nf-fa-plus (U+F067)
              iconText: "\uf067"
              tooltipText: "Larger"
              enabled: root.textSizeIndex < root.textSizes.length - 1
              foreground: root.foreground
              hoverColor: root.foreground
              fontSize: root.fs(Style.font.bodySmall)
              onClicked: root.stepTextSize(1)
            }
          }
        }
      }
    }

    // The cogwheel sits top right, vertically centred on the first visible header
    // (connection status, or the first day heading such as "Today"/"Tomorrow").
    Text {
      id: cog
      readonly property var firstDay: agendaRepeater.count > 0 ? agendaRepeater.itemAt(0) : null
      anchors.right: parent.right
      visible: !root.showConfig
      y: statusText.visible ? statusText.y + (statusText.height - height) / 2
        : firstDay ? agendaList.y + firstDay.y + firstDay.height - dayHeight(firstDay) / 2 - height / 2
        : 0
      function dayHeight(it) { return it.children[0].implicitHeight }
      text: ""
      color: cogArea.containsMouse ? root.accent : root.foreground
      font.family: root.fontFamily
      font.pixelSize: root.fs(Style.font.body)
      MouseArea {
        id: cogArea
        anchors.fill: parent
        anchors.margins: -Style.space(4)
        hoverEnabled: true
        cursorShape: Qt.PointingHandCursor
        onClicked: root.toggleConfig()
      }
    }
  }
}

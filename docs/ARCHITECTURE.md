# Robot Console Logger — Architecture

**Author:** Amin Adineh
**Stack:** Python 3.10+, PySide6 (Qt 6), Chrome DevTools Protocol (CDP) over
WebSocket, MutationObserver injected into the live page.

This document explains, end to end, how a desktop GUI that "looks like a
Windows 11 app and silently captures everything a Robot Framework Web UI
prints" is put together — both the code structure and the design choices.

---

## 1. Bird's-eye view

```
+---------------------------+              +-------------------------+
|     PySide6 Main Window   |  signals     |   Capture worker(s)     |
|  - Tab picker             |<-------------|  CDPConsoleClient       |
|  - Selector + diagnostics |              |  ScreenOCRWorker        |
|  - Glassy Win11 theme     |              +-----------+-------------+
|  - Buffered preview       |                          |
|  - Status bar             |                          | CDP / WebSocket
+-------------+-------------+                          v
              |                              +---------+---------+
              | append(line, level)          |   Chrome (DevTools |
              v                              |   port 9222)       |
       +------+------+                       +---------+---------+
       |  FileLogger |                                 ^
       |  (thread,   |                                 | MutationObserver
       |  flushed)   |              injected JS  ------+ window.__rclLine
       +------+------+              (live, no polling)
              |
              v
        console_log.txt / .csv
```

Three things happen in parallel:
1. **GUI** runs on the Qt main thread.
2. **CDP client** runs in its own background thread (one WebSocket).
3. **File logger** runs in its own thread (buffered disk writes).

They communicate through Qt signals (thread-safe) and a `Diagnostics`
dataclass that the CDP thread updates and the GUI re-renders every 500 ms.

---

## 2. Module map

```
app/
  __main__.py            entry point; just calls gui.main_window.run()
  cdp/
    client.py            CDP transport, console events, DOM observer,
                         element picker, selector test, diagnostics
  gui/
    main_window.py       all Qt widgets, layout, signal wiring
    theme.py             Win11 glassy QSS + DWM Mica (Windows-only)
  logger/
    file_logger.py       background appender + CSV exporter
  screen/
    region_picker.py     "drag a rectangle on the screen" fullscreen tool
    capture.py           Tesseract-based OCR fallback for non-Chrome UIs
```

The folder split is **by responsibility, not by feature**: any new capture
backend (e.g. WebSocket-tail of a server log) would become a new module
under `app/`, expose `start()/stop()` + `on_line/on_status` callbacks, and
the GUI would treat it as just another worker.

---

## 3. Capture pipeline (the important part)

A Robot Framework Web UI typically renders log lines as `<div>` rows inside
a scroll container. Polling that container with `innerText` works but is
laggy and noisy. We use **three** capture sources, ranked best to worst:

### 3.1 Chrome console events (CDP)

Once the WebSocket is open we send:

- `Runtime.enable`
- `Log.enable`

and we receive `Runtime.consoleAPICalled`, `Runtime.exceptionThrown`,
`Log.entryAdded`. Every event becomes one line with a `LEVEL` tag.
This is exact text — no OCR guesswork.

### 3.2 In-page MutationObserver (preferred for visible panels)

Console events miss anything the page renders into the DOM without calling
`console.log` (which is the case for most Robot Framework dashboards).
So we **inject** a tiny JS payload into the tab:

```js
const obs = new MutationObserver(muts => {
  for (const m of muts) {
    if (m.addedNodes) m.addedNodes.forEach(n =>
      emit(n.innerText || n.textContent || ''));
    if (m.type === 'characterData') emit(m.target.nodeValue || '');
  }
});
roots.forEach(r => obs.observe(r, {
  childList: true, subtree: true, characterData: true,
}));
```

`emit()` deduplicates against a `Set` and calls `window.__rclLine(s)` — a
**CDP binding** registered with `Runtime.addBinding`. Every call surfaces
in Python as `Runtime.bindingCalled` with `name = "__rclLine"` and the
line text in `payload`. No polling, no copies, no race with the page's
own scroll behaviour.

A second binding `__rclMeta` reports observer install status, the number
of matched roots, and a description of each root element. The Python side
stores this on `Diagnostics` so the GUI can show it.

On `Runtime.executionContextsCleared` (the page navigated or reloaded)
we re-inject the observer automatically — without this the capture would
silently die after the first refresh.

### 3.3 Polling fallback

If the user disables Live mode, the CDP thread polls the selector every
`dom_poll_ms` ms with `Runtime.evaluate`, diffs the result against the
last snapshot, and emits the new tail.

### 3.4 Screen OCR fallback

If the target is not a Chrome tab at all, the user drags a rectangle on
the screen and `ScreenOCRWorker` does Tesseract OCR on a 400 ms loop. Slow
and lossy, but works on any app.

---

## 4. "Pick on page" — smart container selection

The picker overlays a 1-pixel red box on the live tab and listens for
`mousemove` / `click`. The naive version would return whatever the user
clicked, but Robot UIs are full of nested `<div>` rows; if the user clicks
on **one** row, the MutationObserver would only see that single row and
ignore every new sibling.

So `bestContainer(el)` walks **up** from the clicked element to the
nearest meaningful ancestor:

1. First ancestor with an `id` wins (most stable selector).
2. Otherwise, the first ancestor whose computed style is
   `overflow-y: auto | scroll | overlay` **and** whose
   `scrollHeight > clientHeight` (i.e. it's an actual scroll container).
3. Fallback: the clicked element itself.

The selector is then a short, robust CSS path built by `cssPath()`
(ID-anchored if possible, otherwise tag + first 2 classes +
`:nth-of-type`).

This is the difference between catching "1 log line" and catching "all
future log lines".

---

## 5. Diagnostics, observability, and the GUI

Two `QGroupBox` panels show what the system is doing. Behind both is a
single `Diagnostics` dataclass mutated by the CDP thread:

| Field                  | Meaning                                            |
| ---------------------- | -------------------------------------------------- |
| `state`                | idle / connecting / connected / reconnecting / stopped |
| `attempt`              | reconnect attempt counter                          |
| `next_retry_in`        | seconds until next reconnect (live countdown)      |
| `events_received`      | total CDP events ever delivered                    |
| `observer_installed`   | true if `__rclMeta` reported success               |
| `observer_match_count` | number of DOM nodes the selector matched           |
| `observer_node_info`   | tag + id + class + size + scrollable flag          |
| `observer_fires`       | times `__rclLine` was called (= mutations seen)    |
| `last_fire_at`         | wall-clock time of the most recent fire           |

The GUI re-paints both panels on every diagnostics signal **and** every
500 ms tick, so countdowns and "X.X s ago" timestamps stay live even
when nothing else is happening.

### Preview controls

The preview is fed by a Python-side buffer of `(timestamp, level, message)`
tuples, capped at 5000 entries. Two toggles re-render the buffer:

- **Deduplicate** — show each unique message once (file log still
  records every occurrence).
- **Show timestamps** — prefix each line with `HH:MM:SS.mmm`.
- **Auto-scroll** — pin the view to the tail.

The counter shows `<raw> raw / <unique> unique` so you can tell whether
the capture is noisy or actually delivering new content.

---

## 6. Resilience

- **Exponential backoff with jitter** on the CDP reconnect loop:
  `1 → 2 → 4 → 8 → 16 → 30s cap`, ±20% jitter.
- **Target re-resolution**: if Chrome restarted, the WebSocket URL
  changes. The client re-queries `/json` and matches by `id`, then `url`,
  then URL prefix.
- **Error classification**: `classify_error()` turns raw exceptions
  ("Handshake status 403", "Connection refused") into a single line of
  human advice (e.g. "restart Chrome with `--remote-allow-origins=*`").
- **Selector test** is a one-shot WebSocket so you can verify a selector
  without touching the running monitor.

---

## 7. UI theme — making Qt look like Windows 11

Two layers:

### 7.1 QSS (cross-platform fallback)

`app/gui/theme.py` ships `WIN11_QSS` — a Qt stylesheet with:
- Segoe UI Variable font.
- Translucent dark "card" surfaces (`rgba(32,32,36,200)`).
- Rounded 6–8 px corners on every input and button.
- Fluent accent color `#4cc2ff` for the primary action button
  (`QPushButton[accent="true"]`) and `#c4444a` for destructive
  (`QPushButton[danger="true"]`).
- Custom checkbox indicator (filled accent square when checked).
- 10 px translucent scrollbars.

The widget tree is wrapped in a `QWidget(objectName="rclRoot")` so the
QSS can paint the glassy card without affecting children.

### 7.2 DWM Mica (real Windows 11 transparency)

`apply_mica(win)` calls `DwmSetWindowAttribute` via `ctypes` to:
- enable the immersive dark title bar (attr 20),
- set `DWMWA_SYSTEMBACKDROP_TYPE = 2` (Mica, attr 38),
- fall back to the legacy `DWMWA_MICA_EFFECT` (attr 1029) on older Win11.

For Mica to actually show through, the Qt window needs
`Qt.WA_TranslucentBackground`. That attribute is only applied on
`sys.platform == "win32"` — on other OSes the QSS alone provides the
"glassy" look, and the window is opaque.

This is a deliberately minimal native-integration surface: zero native
build, zero extra DLLs, no PyQt-Fluent-Widgets dependency.

---

## 8. Threading model

```
Qt main thread          CDP thread                FileLogger thread
   |                       |                             |
   | start() ------------->|                             |
   |                       | ws = create_connection()    |
   |                       | loop:                       |
   |                       |   recv() -> _handle()       |
   |                       |   _handle() -> on_line()    |
   |                       |        \---------|          |
   |<-- bridge.line.emit() | (queued connection)         |
   | _on_line(buffer +     |                  \--------->| append + flush
   |  render)              |                             |
```

- `_Bridge` is a `QObject` with three `Signal`s; PySide auto-promotes
  any cross-thread emit to a queued connection.
- The CDP thread never touches Qt widgets directly.
- The FileLogger writes on its own timer so disk hiccups never stall
  the GUI.

---

## 9. Extending it

- **New capture source**: implement `start()/stop()` + a callback
  `(level: str, message: str)`. Wire it in `_start()` next to the CDP
  branch; reuse `_enqueue_line`.
- **New diagnostic field**: add it to the `Diagnostics` dataclass, push
  it from the CDP thread, render it in `_refresh_diag_view`.
- **Theme variant**: copy `WIN11_QSS`, change colors, set with
  `app.setStyleSheet(...)`.

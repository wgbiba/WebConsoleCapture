# Usage guide

## 1. Source page

Pick *what* to capture.

### Chrome console (CDP)
1. **Launch Chrome** with the bundled flags - the app does this for you,
   or use **Copy command** to launch from a terminal.
2. Open the page you want to capture inside that Chrome window.
3. **Refresh tabs**, pick the tab, click **Test CDP connection**.
4. *(Optional)* Narrow to a panel:
   - paste a CSS selector, or
   - click **Pick on page...** and click the element in Chrome.

   With **Live (MutationObserver)** checked, new text appears instantly.
   Uncheck it to fall back to polling every *N* ms.

### Screen region (OCR)
1. Switch **Capture mode** to *Screen region (OCR)*.
2. Click **Pick screen region...** and drag a rectangle.
3. Tune **OCR poll (ms)** if needed (lower = faster but more CPU).

## 2. Capture page

- **Log file** - the on-disk destination. Appended live.
- **Timestamps** - prefix every written line.
- **Flush every N s** - how often the buffer is flushed to disk.
- **Preview options** - toggle deduplication, timestamps, auto-scroll in
  the live preview pane (file is unaffected).

## 3. Diagnostics page

Two boxes:
- **Connection** - state, host, port, WebSocket URL, reconnect attempts,
  next retry, total events received, last error.
- **Selector / MutationObserver** - current selector, match count,
  matched node info, observer status, fires, last fire age.

## 4. Action bar

- **Start capture** - begins streaming to file + preview.
- **Stop** - cleanly closes the WebSocket / OCR worker.
- **Export CSV** - dumps the current session's lines to a spreadsheet-friendly file.

## Troubleshooting

| Symptom | Likely cause | Fix |
| --- | --- | --- |
| *No tabs found* | Chrome wasn't launched with `--remote-debugging-port` | Use the **Launch Chrome** button. |
| *Observer not attached* | CSS selector matches nothing | Use **Pick on page** or **Test selector**. |
| *Reconnecting forever* | Chrome tab closed / port changed | Refresh tabs and pick again. |
| *OCR produces gibberish* | Region too small / low contrast | Pick a larger, higher-contrast rectangle. |

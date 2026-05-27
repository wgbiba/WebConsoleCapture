# DialogFilter

A tiny companion to **WebConsoleCapture** that continuously watches a
captured console log file and writes a clean transcript of conversational
turns (USER / AMY) to a separate file - then lets you export it as
**TXT, CSV, JSON, or Markdown**.

## Features

- **Tail-style watcher** - reads new bytes as they arrive, handles file
  rotation / truncation.
- **Dedup** - already-extracted turns are not re-written even across restarts.
- **Live preview** of every turn as it is detected.
- **Multi-format export**: TXT, CSV, JSON, Markdown.
- Pure Python + Tkinter. No external dependencies; packs into a tiny EXE.

## Usage

```bash
python dialog_filter_app.py
```

1. Pick the **Input log** (e.g. the `console_log.txt` written by
   WebConsoleCapture).
2. Confirm the **Output** path (defaults to `dialog_filter.txt` next to it).
3. Click **Start watching**.
4. When you have enough turns, use **Export &rarr; CSV / JSON / TXT / Markdown**
   to save a snapshot in your chosen format.

## Build a portable EXE (Windows)

```bat
BUILD_EXE.bat
```

Output: `dist\DialogFilter.exe` (~10-15 MB, no Python install needed).

## License

MIT - see the companion WebConsoleCapture repository.

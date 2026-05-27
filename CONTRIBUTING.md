# Contributing to WebConsoleCapture

Thanks for your interest in improving WebConsoleCapture!

## Development setup

```bash
git clone https://github.com/AminAdinehAhari/WebConsoleCapture.git
cd WebConsoleCapture
python -m venv .venv
.venv\Scripts\activate          # Windows
# source .venv/bin/activate     # macOS / Linux
pip install -r requirements.txt
python -m app
```

## Project layout

```
app/
  __main__.py        # entry point
  cdp/               # Chrome DevTools Protocol client
  logger/            # file logger + CSV export
  screen/            # screen region picker + OCR worker
  gui/               # PySide6 main window + theme
  assets/            # bundled icons
docs/                # extra documentation
BUILD_EXE.bat        # one-click Windows packager
```

## Pull requests

1. Fork the repo and create a feature branch:
   `git checkout -b feat/your-change`
2. Keep changes focused and small. One feature / fix per PR.
3. Run the app manually (`python -m app`) and verify the change.
4. Update `CHANGELOG.md` under `## [Unreleased]`.
5. Open a PR describing **what** changed and **why**.

## Reporting bugs

Please include:
- OS and Python version.
- Steps to reproduce.
- The relevant section from the **Diagnostics** page.
- The captured `console_log.txt` (trim sensitive content).

## Code style

- Python 3.10+ syntax (`|` unions, `match`/`case` welcome).
- 4-space indent, ~88-char lines.
- Keep GUI logic in `app/gui/`, transport logic in `app/cdp/` and `app/screen/`.

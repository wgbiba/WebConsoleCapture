# Changelog

All notable changes to **WebConsoleCapture** are documented in this file.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)
and the project adheres to [Semantic Versioning](https://semver.org/).

## [1.0.0] - 2026-05-27

Initial public release under the new **WebConsoleCapture** name.

### Added
- Professional **side-nav layout** with Source / Capture / Diagnostics pages.
- Persistent **live capture status banner** (state, observer, events/sec, last event).
- Sticky bottom **action bar** (Start / Stop / Export).
- Scrollable pages so the window never overflows on small displays.
- MIT license, contributor guide, GitHub-ready project layout.

### Changed
- Renamed from `AmiCDL` to **WebConsoleCapture** to reflect its general use:
  capturing console logs from any web UI, not only one specific robot.
- Smaller default window (1180x760) with a 960x600 minimum.

### Removed
- Robot-specific dialog filtering logic (now lives in the companion
  **DialogFilter** app).

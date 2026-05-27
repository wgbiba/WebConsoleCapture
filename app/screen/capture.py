"""Background screen-region OCR capture worker.

Uses `mss` for fast cross-platform screenshots and
`rapidocr-onnxruntime` for OCR (no Tesseract install, CPU-only ONNX
models bundled with the package).

The worker hashes each frame and only runs OCR when the captured
region actually changes - this keeps CPU usage low when the console
is idle and means OCR runs much more often than once-per-second when
new lines actually appear (fast + precise).
"""

from __future__ import annotations

import hashlib
import threading
import time
from typing import Callable, Optional

from .region_picker import Region


_OCR_SINGLETON = None
_OCR_LOCK = threading.Lock()


def _get_ocr():
    """Lazy-load RapidOCR once.  Heavy import; we keep it off the GUI
    startup path."""
    global _OCR_SINGLETON
    with _OCR_LOCK:
        if _OCR_SINGLETON is None:
            from rapidocr_onnxruntime import RapidOCR  # type: ignore
            _OCR_SINGLETON = RapidOCR()
        return _OCR_SINGLETON


class ScreenOCRWorker(threading.Thread):
    """Daemon thread that grabs the chosen region and OCRs it whenever
    the pixel content changes.

    Callbacks (called from this thread - the GUI must marshal them):
      on_line(level: str, message: str)
      on_status(text: str, ok: bool)
    """

    def __init__(
        self,
        region: Region,
        on_line: Callable[[str, str], None],
        on_status: Callable[[str, bool], None],
        poll_ms: int = 400,
        min_confidence: float = 0.5,
    ):
        super().__init__(daemon=True, name="ScreenOCRWorker")
        self.region = region
        self.on_line = on_line
        self.on_status = on_status
        self.poll_ms = max(100, int(poll_ms))
        self.min_confidence = min_confidence
        self._stop = threading.Event()
        self._seen: set[str] = set()
        self._last_hash: Optional[str] = None

    def stop(self) -> None:
        self._stop.set()

    # ------------------------------------------------------------------

    def run(self) -> None:
        try:
            import mss  # type: ignore
            import numpy as np  # type: ignore
        except Exception as e:
            self.on_status(
                f"Missing dependency for screen OCR: {e}.  "
                f"Run:  pip install mss rapidocr-onnxruntime numpy",
                False)
            return

        self.on_status(
            "Loading OCR model (first run downloads ~10 MB)...", True)
        try:
            ocr = _get_ocr()
        except Exception as e:
            self.on_status(f"Could not load OCR model: {e}", False)
            return
        self.on_status(
            f"Screen OCR active on region {self.region}.", True)

        sct = mss.mss()
        bbox = self.region.as_mss()
        sleep_s = self.poll_ms / 1000.0

        while not self._stop.is_set():
            try:
                raw = sct.grab(bbox)
                img = np.array(raw)  # BGRA
                h = hashlib.md5(img.tobytes()).hexdigest()
                if h == self._last_hash:
                    self._stop.wait(sleep_s)
                    continue
                self._last_hash = h

                # RapidOCR accepts BGR (or RGB) numpy arrays.
                rgb = img[:, :, :3]
                result, _ = ocr(rgb)
                if not result:
                    self._stop.wait(sleep_s)
                    continue

                # Sort top-to-bottom by the y of the first box point.
                try:
                    result.sort(key=lambda r: r[0][0][1])
                except Exception:
                    pass

                for box, text, conf in result:
                    if not text or conf < self.min_confidence:
                        continue
                    line = text.strip()
                    if not line:
                        continue
                    # Dedup: emit each unique line once per session.
                    key = f"{line}"
                    if key in self._seen:
                        continue
                    self._seen.add(key)
                    level = _infer_level(line)
                    self.on_line(level, line)

            except Exception as e:
                self.on_status(f"OCR loop error: {e}", False)
                self._stop.wait(1.0)
                continue

            self._stop.wait(sleep_s)

        self.on_status("Screen OCR stopped.", False)


def _infer_level(line: str) -> str:
    low = line.lower()
    if "error" in low or "exception" in low or "fail" in low:
        return "error"
    if "warn" in low:
        return "warning"
    if "debug" in low:
        return "debug"
    return "info"

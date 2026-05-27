"""Thread-safe, batched file logger."""
from __future__ import annotations

import csv, os, threading, time
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Deque, List, Optional, Tuple

MAX_RECORDS = 200_000


class FileLogger:
    def __init__(self, path: str, flush_interval: float = 1.0,
                 timestamps: bool = True):
        self.path = path
        self.flush_interval = flush_interval
        self.timestamps = timestamps

        self._buf: List[str] = []
        self._records: Deque[Tuple[str, str, str]] = deque(maxlen=MAX_RECORDS)
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._lines_written = 0

        Path(os.path.dirname(os.path.abspath(path)) or ".").mkdir(
            parents=True, exist_ok=True)
        with open(self.path, "a", encoding="utf-8"):
            pass

    def start(self) -> None:
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2.0)
        self._flush()

    def add(self, level: str, message: str) -> None:
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        line = f"[{level}] {message}"
        if self.timestamps:
            line = f"{ts} {line}"
        with self._lock:
            self._buf.append(line)
            self._records.append((ts, level, message))

    def dialog_stats(self) -> Tuple[int, int]:
        return (0, 0)

    def lines_written(self) -> int:
        return self._lines_written

    def export_csv(self, csv_path: str) -> int:
        with self._lock:
            rows = list(self._records)
        Path(os.path.dirname(os.path.abspath(csv_path)) or ".").mkdir(
            parents=True, exist_ok=True)
        with open(csv_path, "w", encoding="utf-8", newline="") as f:
            w = csv.writer(f)
            w.writerow(["timestamp", "level", "message"])
            w.writerows(rows)
        return len(rows)

    def _loop(self) -> None:
        while not self._stop.is_set():
            time.sleep(self.flush_interval)
            self._flush()

    def _flush(self) -> None:
        with self._lock:
            if not self._buf:
                return
            chunk = self._buf
            self._buf = []
        try:
            with open(self.path, "a", encoding="utf-8") as f:
                f.write("\n".join(chunk) + "\n")
            self._lines_written += len(chunk)
        except Exception:
            with self._lock:
                self._buf[:0] = chunk

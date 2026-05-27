"""
DialogFilter v2 - continuously watches a console log file (e.g. console_log.txt)
and appends new dialog turns to <input>_dialogue.txt (default: dialog_filter.txt).

v2 adds Export menu (CSV / JSON / TXT / Markdown) for the captured turns.
"""
from __future__ import annotations

import csv
import json
import os
import re
import sys
import threading
import time
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

APP_TITLE = "DialogFilter v2 - live dialog extractor"
DEFAULT_OUTPUT_NAME = "dialog_filter.txt"
POLL_INTERVAL = 0.5

USER_PATTERNS = [
    re.compile(r'\bUSER:\s*(.+)$'),
    re.compile(r"'role':\s*'user'[^{}]{0,4000}?'text':\s*'([^']{0,4000})'"),
    re.compile(r'Unidentified Speaker:\s*(.+)$'),
]
AMY_PATTERNS = [
    re.compile(r'\bASSISTANT:\s*(.+)$'),
    re.compile(r"'role':\s*'assistant'[^{}]{0,4000}?'text':\s*'([^']{0,4000})'"),
    re.compile(r"speech='([^']{0,4000})'[^']{0,200}purpose=None"),
]


def extract_turns(line: str):
    out = []
    for p in USER_PATTERNS:
        for m in p.finditer(line):
            t = m.group(1).strip()
            if t:
                out.append(("USER", t))
    for p in AMY_PATTERNS:
        for m in p.finditer(line):
            t = m.group(1).strip()
            if t:
                out.append(("AMY", t))
    return out


class Watcher(threading.Thread):
    def __init__(self, input_path: str, output_path: str, on_event):
        super().__init__(daemon=True)
        self.input_path = input_path
        self.output_path = output_path
        self.on_event = on_event
        self._stop = threading.Event()
        self._seen: set[tuple[str, str]] = set()
        self.kept = 0
        self.scanned = 0

    def stop(self):
        self._stop.set()

    def _load_existing_seen(self):
        if not os.path.exists(self.output_path):
            return
        try:
            with open(self.output_path, "r", encoding="utf-8", errors="ignore") as f:
                for line in f:
                    line = line.strip()
                    if line.startswith("USER:"):
                        self._seen.add(("USER", line[5:].strip()))
                    elif line.startswith("AMY:"):
                        self._seen.add(("AMY", line[4:].strip()))
        except Exception:
            pass

    def run(self):
        self._load_existing_seen()
        self.on_event("status", f"Watching: {self.input_path}")
        out_fh = open(self.output_path, "a", encoding="utf-8")
        in_fh = None
        last_inode = None
        last_size = 0
        try:
            while not self._stop.is_set():
                try:
                    st = os.stat(self.input_path)
                except FileNotFoundError:
                    self.on_event("status", "Waiting for input file...")
                    time.sleep(POLL_INTERVAL)
                    continue
                if (in_fh is None or st.st_ino != last_inode
                        or st.st_size < last_size):
                    if in_fh:
                        in_fh.close()
                    in_fh = open(self.input_path, "r",
                                 encoding="utf-8", errors="ignore")
                    last_inode = st.st_ino
                    last_size = 0
                wrote = False
                while True:
                    line = in_fh.readline()
                    if not line:
                        break
                    self.scanned += 1
                    for speaker, text in extract_turns(line.rstrip("\n")):
                        key = (speaker, text)
                        if key in self._seen:
                            continue
                        self._seen.add(key)
                        out_fh.write(f"{speaker}: {text}\n")
                        self.kept += 1
                        wrote = True
                        ts = datetime.now().isoformat(timespec="seconds")
                        self.on_event("turn", (ts, speaker, text))
                if wrote:
                    out_fh.flush()
                last_size = in_fh.tell()
                self.on_event("stats", (self.scanned, self.kept))
                time.sleep(POLL_INTERVAL)
        finally:
            if in_fh:
                in_fh.close()
            out_fh.close()
            self.on_event("status", "Stopped")


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(APP_TITLE)
        self.geometry("820x580")
        self.minsize(640, 440)
        self.watcher: Watcher | None = None
        # captured turns: list of (timestamp_iso, speaker, text)
        self.turns: list[tuple[str, str, str]] = []
        self._events: list = []
        self._events_lock = threading.Lock()

        self._build_menu()
        self._build_ui()

        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(100, self._drain)

    # ---------- UI ----------

    def _build_menu(self):
        menubar = tk.Menu(self)
        filemenu = tk.Menu(menubar, tearoff=0)
        filemenu.add_command(label="Open input log...", command=self._pick_input)
        filemenu.add_command(label="Set output file...", command=self._pick_output)
        filemenu.add_separator()
        filemenu.add_command(label="Quit", command=self._on_close)
        menubar.add_cascade(label="File", menu=filemenu)

        exportmenu = tk.Menu(menubar, tearoff=0)
        exportmenu.add_command(label="Export as CSV...", command=lambda: self._export("csv"))
        exportmenu.add_command(label="Export as JSON...", command=lambda: self._export("json"))
        exportmenu.add_command(label="Export as TXT...", command=lambda: self._export("txt"))
        exportmenu.add_command(label="Export as Markdown...", command=lambda: self._export("md"))
        menubar.add_cascade(label="Export", menu=exportmenu)

        helpmenu = tk.Menu(menubar, tearoff=0)
        helpmenu.add_command(label="About", command=self._show_about)
        menubar.add_cascade(label="Help", menu=helpmenu)
        self.config(menu=menubar)

    def _build_ui(self):
        pad = {"padx": 8, "pady": 4}

        frm = ttk.Frame(self)
        frm.pack(fill="x", **pad)

        ttk.Label(frm, text="Input log:").grid(row=0, column=0, sticky="w")
        self.in_var = tk.StringVar(value="console_log.txt")
        ttk.Entry(frm, textvariable=self.in_var, width=70).grid(row=0, column=1, sticky="ew")
        ttk.Button(frm, text="Browse...", command=self._pick_input).grid(row=0, column=2)

        ttk.Label(frm, text="Output:").grid(row=1, column=0, sticky="w")
        self.out_var = tk.StringVar(value=DEFAULT_OUTPUT_NAME)
        ttk.Entry(frm, textvariable=self.out_var, width=70).grid(row=1, column=1, sticky="ew")
        ttk.Button(frm, text="Browse...", command=self._pick_output).grid(row=1, column=2)
        frm.columnconfigure(1, weight=1)

        btn_row = ttk.Frame(self)
        btn_row.pack(fill="x", **pad)
        self.start_btn = ttk.Button(btn_row, text="Start watching", command=self._start)
        self.start_btn.pack(side="left")
        self.stop_btn = ttk.Button(btn_row, text="Stop", command=self._stop, state="disabled")
        self.stop_btn.pack(side="left", padx=4)

        ttk.Separator(btn_row, orient="vertical").pack(side="left", fill="y", padx=8)
        ttk.Label(btn_row, text="Export:").pack(side="left")
        ttk.Button(btn_row, text="CSV", width=6,
                   command=lambda: self._export("csv")).pack(side="left", padx=2)
        ttk.Button(btn_row, text="JSON", width=6,
                   command=lambda: self._export("json")).pack(side="left", padx=2)
        ttk.Button(btn_row, text="TXT", width=6,
                   command=lambda: self._export("txt")).pack(side="left", padx=2)
        ttk.Button(btn_row, text="Markdown", width=10,
                   command=lambda: self._export("md")).pack(side="left", padx=2)

        self.status_var = tk.StringVar(value="Idle")
        ttk.Label(self, textvariable=self.status_var, anchor="w").pack(fill="x", **pad)
        self.stats_var = tk.StringVar(value="Lines scanned: 0   Turns kept: 0")
        ttk.Label(self, textvariable=self.stats_var, anchor="w").pack(fill="x", **pad)

        ttk.Label(self, text="Live dialog preview:").pack(anchor="w", **pad)
        self.text = tk.Text(self, height=20, wrap="word")
        self.text.pack(fill="both", expand=True, **pad)

    # ---------- file pickers ----------

    def _pick_input(self):
        p = filedialog.askopenfilename(title="Choose console log file")
        if p:
            self.in_var.set(p)
            out = Path(p).with_name(DEFAULT_OUTPUT_NAME)
            self.out_var.set(str(out))

    def _pick_output(self):
        p = filedialog.asksaveasfilename(
            title="Choose output dialog file",
            defaultextension=".txt", initialfile=DEFAULT_OUTPUT_NAME)
        if p:
            self.out_var.set(p)

    # ---------- start / stop ----------

    def _start(self):
        ipath = self.in_var.get().strip()
        opath = self.out_var.get().strip() or DEFAULT_OUTPUT_NAME
        if not ipath:
            self.status_var.set("Pick an input file first.")
            return
        self.watcher = Watcher(ipath, opath, self._on_event)
        self.watcher.start()
        self.start_btn.configure(state="disabled")
        self.stop_btn.configure(state="normal")

    def _stop(self):
        if self.watcher:
            self.watcher.stop()
            self.watcher = None
        self.start_btn.configure(state="normal")
        self.stop_btn.configure(state="disabled")

    def _on_close(self):
        self._stop()
        self.destroy()

    # ---------- thread bridge ----------

    def _on_event(self, kind, payload):
        with self._events_lock:
            self._events.append((kind, payload))

    def _drain(self):
        with self._events_lock:
            evs = self._events
            self._events = []
        for kind, payload in evs:
            if kind == "status":
                self.status_var.set(payload)
            elif kind == "stats":
                scanned, kept = payload
                self.stats_var.set(
                    f"Lines scanned: {scanned}   Turns kept: {kept}")
            elif kind == "turn":
                ts, speaker, text = payload
                self.turns.append(payload)
                self.text.insert("end", f"{speaker}: {text}\n")
                self.text.see("end")
                if int(self.text.index("end-1c").split(".")[0]) > 2000:
                    self.text.delete("1.0", "500.0")
        self.after(150, self._drain)

    # ---------- export ----------

    def _export(self, fmt: str):
        if not self.turns:
            # Try to load from the output file as a fallback
            self._load_turns_from_output_file()
        if not self.turns:
            messagebox.showinfo(
                "Nothing to export",
                "No dialog turns captured yet. Start watching first.")
            return

        types = {
            "csv": ("CSV file", "*.csv", ".csv"),
            "json": ("JSON file", "*.json", ".json"),
            "txt": ("Text file", "*.txt", ".txt"),
            "md": ("Markdown file", "*.md", ".md"),
        }
        label, pattern, ext = types[fmt]
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        default_name = f"dialog_filter_{stamp}{ext}"
        path = filedialog.asksaveasfilename(
            title=f"Export as {label}",
            defaultextension=ext, initialfile=default_name,
            filetypes=[(label, pattern), ("All files", "*.*")])
        if not path:
            return
        try:
            if fmt == "csv":
                self._write_csv(path)
            elif fmt == "json":
                self._write_json(path)
            elif fmt == "txt":
                self._write_txt(path)
            elif fmt == "md":
                self._write_md(path)
        except Exception as e:
            messagebox.showerror("Export failed", str(e))
            return
        self.status_var.set(f"Exported {len(self.turns)} turns -> {path}")

    def _load_turns_from_output_file(self):
        opath = self.out_var.get().strip()
        if not opath or not os.path.exists(opath):
            return
        try:
            with open(opath, "r", encoding="utf-8", errors="ignore") as f:
                for line in f:
                    line = line.rstrip("\n")
                    if line.startswith("USER:"):
                        self.turns.append(("", "USER", line[5:].strip()))
                    elif line.startswith("AMY:"):
                        self.turns.append(("", "AMY", line[4:].strip()))
        except Exception:
            pass

    def _write_csv(self, path: str):
        with open(path, "w", encoding="utf-8", newline="") as f:
            w = csv.writer(f)
            w.writerow(["index", "timestamp", "speaker", "text"])
            for i, (ts, sp, tx) in enumerate(self.turns, 1):
                w.writerow([i, ts, sp, tx])

    def _write_json(self, path: str):
        data = [
            {"index": i, "timestamp": ts, "speaker": sp, "text": tx}
            for i, (ts, sp, tx) in enumerate(self.turns, 1)
        ]
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    def _write_txt(self, path: str):
        with open(path, "w", encoding="utf-8") as f:
            for ts, sp, tx in self.turns:
                if ts:
                    f.write(f"[{ts}] {sp}: {tx}\n")
                else:
                    f.write(f"{sp}: {tx}\n")

    def _write_md(self, path: str):
        with open(path, "w", encoding="utf-8") as f:
            f.write("# Dialog transcript\n\n")
            f.write(f"_Exported {datetime.now().isoformat(timespec='seconds')}_\n\n")
            f.write(f"**Total turns:** {len(self.turns)}\n\n---\n\n")
            for ts, sp, tx in self.turns:
                header = f"**{sp}**" + (f" _( {ts} )_" if ts else "")
                f.write(f"{header}\n\n> {tx}\n\n")

    def _show_about(self):
        messagebox.showinfo(
            "About DialogFilter",
            "DialogFilter v2\n\n"
            "Continuously extracts conversational turns from a console log\n"
            "and exports them to TXT / CSV / JSON / Markdown.\n\n"
            "Companion to WebConsoleCapture.\n\n"
            "(c) 2026 Amin Adineh")


if __name__ == "__main__":
    App().mainloop()

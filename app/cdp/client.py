"""
Chrome DevTools Protocol (CDP) client.

Capture modes (both can run at once):
1. CONSOLE — Runtime.consoleAPICalled / exceptionThrown / Log.entryAdded.
2. DOM     — poll a CSS selector with Runtime.evaluate, emit new lines.

Resilience:
- Exponential backoff with jitter (1s -> 2 -> 4 -> 8 -> 16 -> 30 cap).
- Re-resolves the target tab (id -> URL -> URL prefix) if Chrome
  restarted and the WebSocket URL changed.
- Friendly diagnostics: structured state + classified last error
  (Origin/403 hint, port unreachable hint, etc.).
"""

from __future__ import annotations

import json
import random
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, List, Optional

import requests
import websocket  # websocket-client


# ---------------------------------------------------------------------------
# Targets
# ---------------------------------------------------------------------------

@dataclass
class BrowserTarget:
    id: str
    title: str
    url: str
    ws_url: str

    def label(self) -> str:
        return f"{self.title or '(untitled)'}  -  {self.url}"


def list_targets(host: str = "127.0.0.1", port: int = 9222,
                 timeout: float = 2.0) -> List[BrowserTarget]:
    r = requests.get(f"http://{host}:{port}/json", timeout=timeout)
    r.raise_for_status()
    out: List[BrowserTarget] = []
    for t in r.json():
        if t.get("type") != "page":
            continue
        ws = t.get("webSocketDebuggerUrl")
        if not ws:
            continue
        out.append(BrowserTarget(
            id=t.get("id", ""), title=t.get("title", ""),
            url=t.get("url", ""), ws_url=ws,
        ))
    return out


def _fmt_arg(arg: dict) -> str:
    if "value" in arg:
        v = arg["value"]
        return v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)
    if "description" in arg:
        return arg["description"]
    if "unserializableValue" in arg:
        return str(arg["unserializableValue"])
    return arg.get("type", "")


# ---------------------------------------------------------------------------
# Error classification
# ---------------------------------------------------------------------------

def classify_error(err: BaseException) -> str:
    """Turn a raw exception into a one-line human hint."""
    s = str(err)
    low = s.lower()
    if "403" in s and ("forbidden" in low or "handshake" in low):
        return ("Chrome rejected the WebSocket (HTTP 403). The Origin "
                "header isn't allowed. Restart Chrome with "
                "--remote-allow-origins=* (or use the Launch Chrome "
                "button below).")
    if "connection refused" in low or "actively refused" in low:
        return ("Cannot reach the debug port. Chrome isn't running with "
                "--remote-debugging-port, or the port number is wrong.")
    if "timed out" in low or "timeout" in low:
        return "Connection timed out. Is Chrome responsive?"
    if "name or service not known" in low or "getaddrinfo" in low:
        return "Host not found. Check the Chrome host field."
    if "404" in s and "not found" in low:
        return "Target not found - the tab may have been closed."
    return s


# ---------------------------------------------------------------------------
# Quick connection test
# ---------------------------------------------------------------------------

@dataclass
class TestResult:
    ok: bool
    summary: str
    detail: str = ""
    events_seen: int = 0


def test_connection(host: str, port: int, ws_url: str,
                    wait_for_events_s: float = 1.5) -> TestResult:
    """
    1. GET http://host:port/json/version (proves the debug port is up).
    2. Open the WebSocket (proves handshake works).
    3. Runtime.enable + listen briefly to confirm events stream.
    """
    try:
        r = requests.get(f"http://{host}:{port}/json/version", timeout=3)
        r.raise_for_status()
        ver = r.json().get("Browser", "Chrome")
    except Exception as e:
        return TestResult(False, "Debug port unreachable",
                          classify_error(e))

    try:
        ws = websocket.create_connection(
            ws_url, timeout=5, suppress_origin=True)
    except Exception as e:
        return TestResult(False, "WebSocket handshake failed",
                          classify_error(e))

    events = 0
    try:
        ws.send(json.dumps({"id": 1, "method": "Runtime.enable"}))
        ws.send(json.dumps({"id": 2, "method": "Log.enable"}))
        # Fire a console.log so we *guarantee* at least one event.
        ws.send(json.dumps({
            "id": 3, "method": "Runtime.evaluate",
            "params": {"expression":
                       "console.log('[RobotConsoleLogger] test ping')"}
        }))
        ws.settimeout(0.3)
        deadline = time.time() + wait_for_events_s
        while time.time() < deadline:
            try:
                raw = ws.recv()
            except websocket.WebSocketTimeoutException:
                continue
            if not raw:
                break
            try:
                msg = json.loads(raw)
            except Exception:
                continue
            if msg.get("method", "").startswith(("Runtime.", "Log.")):
                events += 1
    except Exception as e:
        return TestResult(False, "Event stream failed",
                          classify_error(e), events)
    finally:
        try:
            ws.close()
        except Exception:
            pass

    if events == 0:
        return TestResult(False,
                          "Connected, but no events received",
                          f"{ver} accepted the WS but didn't emit any "
                          f"Runtime/Log events within "
                          f"{wait_for_events_s:.1f}s.", events)
    return TestResult(True, f"OK - {ver} reachable, "
                      f"{events} event(s) received",
                      f"WS: {ws_url}", events)


# ---------------------------------------------------------------------------
# CDP client
# ---------------------------------------------------------------------------

@dataclass
class Diagnostics:
    host: str = ""
    port: int = 0
    ws_url: str = ""
    state: str = "idle"          # idle|connecting|connected|reconnecting|stopped
    attempt: int = 0
    last_error: str = ""
    last_error_at: float = 0.0
    next_retry_in: float = 0.0   # seconds until next reconnect attempt
    connected_at: float = 0.0
    events_received: int = 0
    # MutationObserver telemetry
    observer_installed: bool = False
    observer_match_count: int = 0
    observer_node_info: str = ""
    observer_fires: int = 0
    last_fire_at: float = 0.0



class CDPConsoleClient:
    BACKOFF_BASE = 1.0
    BACKOFF_CAP = 30.0

    def __init__(self,
                 host: str,
                 port: int,
                 target: BrowserTarget,
                 on_line: Callable[[str, str], None],
                 on_status: Optional[Callable[[str, bool], None]] = None,
                 on_diagnostics: Optional[Callable[[Diagnostics], None]] = None,
                 dom_selector: str = "",
                 dom_poll_ms: int = 500,
                 use_mutation_observer: bool = True):
        self.host = host
        self.port = port
        self.initial_target = target
        self.current_ws_url = target.ws_url
        self.target_id = target.id
        self.target_url = target.url

        self.on_line = on_line
        self.on_status = on_status or (lambda *_: None)
        self.on_diagnostics = on_diagnostics or (lambda *_: None)

        self.dom_selector = (dom_selector or "").strip()
        self.dom_poll_s = max(0.1, dom_poll_ms / 1000.0)
        self.use_mutation_observer = use_mutation_observer

        self._ws: Optional[websocket.WebSocket] = None
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._msg_id = 0
        self._lock = threading.Lock()
        self._dom_last_text = ""
        self._dom_next_poll = 0.0
        self._observer_installed = False

        self.diag = Diagnostics(host=host, port=port,
                                ws_url=target.ws_url, state="idle")

    # ---------------------- lifecycle ----------------------

    def start(self) -> None:
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        try:
            if self._ws:
                self._ws.close()
        except Exception:
            pass
        if self._thread:
            self._thread.join(timeout=2.0)

    # ---------------------- diagnostics ----------------------

    def _set_state(self, state: str, ok: bool, msg: str) -> None:
        self.diag.state = state
        self.on_status(msg, ok)
        self.on_diagnostics(self.diag)

    def _record_error(self, err: BaseException) -> str:
        hint = classify_error(err)
        self.diag.last_error = hint
        self.diag.last_error_at = time.time()
        return hint

    # ---------------------- internals ----------------------

    def _next_id(self) -> int:
        with self._lock:
            self._msg_id += 1
            return self._msg_id

    def _send(self, method: str, params: Optional[dict] = None) -> None:
        if not self._ws:
            return
        self._ws.send(json.dumps({
            "id": self._next_id(), "method": method, "params": params or {}
        }))

    def _resolve_ws_url(self) -> Optional[str]:
        try:
            targets = list_targets(self.host, self.port)
        except Exception:
            return None
        for t in targets:
            if t.id == self.target_id:
                return t.ws_url
        for t in targets:
            if t.url == self.target_url:
                self.target_id = t.id
                return t.ws_url
        base = self.target_url.split("?")[0].split("#")[0]
        for t in targets:
            if t.url.startswith(base):
                self.target_id = t.id
                return t.ws_url
        return None

    def _backoff(self, attempt: int) -> float:
        # 1, 2, 4, 8, 16, 30 (cap) with +/- 20% jitter.
        base = min(self.BACKOFF_CAP,
                   self.BACKOFF_BASE * (2 ** max(0, attempt - 1)))
        return base * random.uniform(0.8, 1.2)

    def _run(self) -> None:
        attempt = 0
        while not self._stop.is_set():
            attempt += 1
            self.diag.attempt = attempt
            try:
                if attempt > 1:
                    fresh = self._resolve_ws_url()
                    if fresh and fresh != self.current_ws_url:
                        self.current_ws_url = fresh
                        self.diag.ws_url = fresh

                self._set_state("connecting", False,
                                f"Connecting to Chrome "
                                f"(attempt #{attempt})...")
                self._ws = websocket.create_connection(
                    self.current_ws_url, timeout=10,
                    suppress_origin=True)

                self.diag.connected_at = time.time()
                self.diag.next_retry_in = 0.0
                self._set_state("connected", True,
                                "Connected - capturing")
                attempt = 0  # reset on success
                self.diag.attempt = 0

                self._send("Runtime.enable")
                self._send("Log.enable")
                self._send("Runtime.discardConsoleEntries")
                self._dom_last_text = ""
                self._dom_next_poll = time.time()
                self._observer_installed = False
                if self.dom_selector and self.use_mutation_observer:
                    self._install_mutation_observer()

                self._ws.settimeout(0.25)
                next_observer_retry = time.time() + 3.0
                while not self._stop.is_set():
                    try:
                        raw = self._ws.recv()
                        if raw:
                            try:
                                self._handle(json.loads(raw))
                            except Exception:
                                pass
                    except websocket.WebSocketTimeoutException:
                        pass
                    if not self._observer_installed:
                        self._maybe_poll_dom()
                        # Keep trying to (re)install the live observer
                        # so capture resumes automatically after SPA
                        # navigations / container swaps.
                        if (self.dom_selector
                                and self.use_mutation_observer
                                and time.time() >= next_observer_retry):
                            next_observer_retry = time.time() + 3.0
                            try:
                                self._install_mutation_observer()
                            except Exception:
                                pass

            except Exception as e:
                if self._stop.is_set():
                    break
                hint = self._record_error(e)
                delay = self._backoff(attempt)
                self.diag.next_retry_in = delay
                self._set_state(
                    "reconnecting", False,
                    f"Disconnected: {hint}  "
                    f"Retrying in {delay:.1f}s (attempt #{attempt + 1}).")
                end = time.time() + delay
                while time.time() < end and not self._stop.is_set():
                    self.diag.next_retry_in = max(0.0, end - time.time())
                    self.on_diagnostics(self.diag)
                    time.sleep(0.2)
            finally:
                try:
                    if self._ws:
                        self._ws.close()
                except Exception:
                    pass
                self._ws = None

        self._set_state("stopped", False, "Stopped")

    # ---------------------- event handling ----------------------

    def _handle(self, msg: dict) -> None:
        method = msg.get("method")
        if not method:
            return
        params = msg.get("params", {})
        self.diag.events_received += 1

        if method == "Runtime.consoleAPICalled":
            level = params.get("type", "log").upper()
            args = params.get("args", []) or []
            self._emit(level, " ".join(_fmt_arg(a) for a in args))
        elif method == "Runtime.exceptionThrown":
            det = params.get("exceptionDetails", {}) or {}
            text = (det.get("exception") or {}).get("description") \
                or det.get("text", "Exception")
            self._emit("ERROR", text)
        elif method == "Log.entryAdded":
            entry = params.get("entry", {}) or {}
            self._emit(entry.get("level", "log").upper(),
                       entry.get("text", ""))
        elif method == "Runtime.bindingCalled":
            name = params.get("name")
            payload = params.get("payload", "") or ""
            if name == "__rclLine":
                self.diag.observer_fires += 1
                self.diag.last_fire_at = time.time()
                for ln in str(payload).splitlines():
                    ln = ln.rstrip()
                    if ln:
                        self.on_line("PAGE", ln)
                self.on_diagnostics(self.diag)
            elif name == "__rclMeta":
                try:
                    meta = json.loads(str(payload))
                except Exception:
                    meta = {}
                ok = bool(meta.get("ok"))
                self.diag.observer_installed = ok
                self.diag.observer_match_count = int(meta.get("count") or 0)
                self.diag.observer_node_info = str(meta.get("info") or "")
                # If the page reported the selector did not match, drop
                # the Python-side flag so DOM polling resumes and the
                # loop retries the install (the page may still be loading
                # or a SPA route just swapped the log container out).
                if not ok:
                    self._observer_installed = False
                self.on_diagnostics(self.diag)
        elif method == "Runtime.executionContextsCleared":
            # navigation/reload wiped the observer; reinstall next loop
            self._observer_installed = False
            self.diag.observer_installed = False
            if self.dom_selector and self.use_mutation_observer:
                self._install_mutation_observer()


    def _emit(self, level: str, text: str) -> None:
        for line in str(text).splitlines() or [""]:
            line = line.rstrip()
            if line:
                self.on_line(level, line)

    # ---------------------- DOM polling ----------------------

    def _maybe_poll_dom(self) -> None:
        if not self.dom_selector or not self._ws:
            return
        now = time.time()
        if now < self._dom_next_poll:
            return
        self._dom_next_poll = now + self.dom_poll_s

        expr = (
            "(()=>{" + _QUERY_HELPER_JS +
            "const els=window.__rclQueryAll("
            + json.dumps(self.dom_selector)
            + ");return els.map(e=>e.innerText||e.textContent||'')"
            ".join('\\n');})()"
        )
        msg_id = self._next_id()
        try:
            self._ws.send(json.dumps({
                "id": msg_id, "method": "Runtime.evaluate",
                "params": {"expression": expr, "returnByValue": True},
            }))
            deadline = time.time() + 1.0
            while time.time() < deadline:
                try:
                    raw = self._ws.recv()
                except websocket.WebSocketTimeoutException:
                    return
                if not raw:
                    return
                try:
                    msg = json.loads(raw)
                except Exception:
                    continue
                if msg.get("id") == msg_id:
                    text = ((msg.get("result") or {})
                            .get("result", {}).get("value") or "")
                    self._diff_emit_dom(str(text))
                    return
                self._handle(msg)
        except Exception:
            raise

    def _diff_emit_dom(self, current: str) -> None:
        if not current:
            return
        prev = self._dom_last_text
        if current == prev:
            return
        cur_lines = current.splitlines()
        if prev and current.startswith(prev):
            new_lines = current[len(prev):].splitlines()
        else:
            prev_set = set(prev.splitlines()[-500:])
            new_lines = [ln for ln in cur_lines if ln not in prev_set]
        self._dom_last_text = current
        for line in new_lines:
            line = line.rstrip()
            if line:
                self.on_line("PAGE", line)


# ---------------------------------------------------------------------------
# Shared JS helpers
# ---------------------------------------------------------------------------
# __rclQueryAll(sel): runs querySelectorAll across the top document AND every
# same-origin iframe (recursively). Critical for SPAs like Tritium that put
# the log panel inside an iframe (<iframe src="/scripts/">). Cross-origin
# iframes are silently skipped (we can't reach them with JS).
# __rclElementFromPoint(x,y): like elementFromPoint but descends into iframes.
_QUERY_HELPER_JS = r"""
window.__rclAllDocs = function() {
  const out = [document];
  const walk = (doc) => {
    let frames; try { frames = doc.querySelectorAll('iframe,frame'); }
    catch(_) { return; }
    for (const f of frames) {
      let d = null;
      try { d = f.contentDocument; } catch(_) {}
      if (d && out.indexOf(d) === -1) { out.push(d); walk(d); }
    }
  };
  try { walk(document); } catch(_) {}
  return out;
};
window.__rclQueryAll = function(sel) {
  const out = [];
  for (const d of window.__rclAllDocs()) {
    let m; try { m = d.querySelectorAll(sel); } catch(_) { continue; }
    for (const e of m) out.push(e);
  }
  return out;
};
window.__rclElementFromPoint = function(x, y) {
  let el = document.elementFromPoint(x, y);
  while (el && el.tagName && /^(IFRAME|FRAME)$/.test(el.tagName)) {
    let doc = null;
    try { doc = el.contentDocument; } catch(_) { break; }
    if (!doc) break;
    const r = el.getBoundingClientRect();
    const inner = doc.elementFromPoint(x - r.left, y - r.top);
    if (!inner || inner === el) break;
    el = inner;
  }
  return el;
};
"""

# ---------------------------------------------------------------------------
# Interactive element picker (injects an overlay into the live Chrome tab)
# ---------------------------------------------------------------------------

_PICKER_JS = r"""

(() => {
  if (window.__rclPickerActive) return 'already-active';
  window.__rclPickerActive = true;
  window.__rclPicked = null;
  window.__rclCancelled = false;

  const box = document.createElement('div');
  box.style.cssText =
    'position:fixed;pointer-events:none;z-index:2147483647;' +
    'border:2px solid #ff3b30;background:rgba(255,59,48,0.15);' +
    'transition:all 60ms;left:0;top:0;width:0;height:0;';
  const hint = document.createElement('div');
  hint.textContent =
    'Robot Console Logger: click the panel to capture. Press Esc to cancel.';
  hint.style.cssText =
    'position:fixed;top:12px;left:50%;transform:translateX(-50%);' +
    'z-index:2147483647;background:#111;color:#fff;' +
    'font:13px system-ui,Segoe UI,Arial;padding:8px 14px;' +
    'border-radius:6px;box-shadow:0 4px 16px rgba(0,0,0,.3);' +
    'pointer-events:none;';
  document.documentElement.appendChild(box);
  document.documentElement.appendChild(hint);

  function cssPath(el) {
    if (!(el instanceof Element)) return '';
    // Prefer stable data-testid (works across React/MUI dynamic classes
    // like jss90/jss91 and across iframes via __rclQueryAll).
    const tid = el.getAttribute && el.getAttribute('data-testid');
    if (tid) return '[data-testid="' + tid.replace(/"/g, '\\"') + '"]';
    const parts = [];
    while (el && el.nodeType === 1 && parts.length < 6) {
      const t2 = el.getAttribute && el.getAttribute('data-testid');
      if (t2) {
        parts.unshift('[data-testid="' + t2.replace(/"/g, '\\"') + '"]');
        break;
      }
      let sel = el.nodeName.toLowerCase();
      if (el.id) { parts.unshift(sel + '#' + CSS.escape(el.id)); break; }
      const cls = (el.getAttribute('class') || '').trim().split(/\s+/)
        .filter(Boolean).slice(0, 2)
        .map(c => '.' + CSS.escape(c)).join('');
      sel += cls;
      const parent = el.parentNode;
      if (parent && parent.children) {
        const sibs = Array.from(parent.children)
          .filter(c => c.nodeName === el.nodeName);
        if (sibs.length > 1) {
          sel += ':nth-of-type(' + (sibs.indexOf(el) + 1) + ')';
        }
      }
      parts.unshift(sel);
      el = el.parentElement;
    }
    return parts.join(' > ');
  }

  let hover = null;
  function onMove(e) {
    const el = (window.__rclElementFromPoint || document.elementFromPoint
      .bind(document))(e.clientX, e.clientY);
    if (!el || el === box || el === hint) return;
    hover = el;
    const r = el.getBoundingClientRect();
    box.style.left = r.left + 'px';
    box.style.top = r.top + 'px';
    box.style.width = r.width + 'px';
    box.style.height = r.height + 'px';
  }
  function bestContainer(el) {
    let cur = el;
    let scrollable = null;
    let testid = null;
    // Walk up through ancestors AND parent documents (iframe boundaries).
    while (cur && cur.nodeType === 1) {
      const tid = cur.getAttribute && cur.getAttribute('data-testid');
      if (tid && !testid) testid = cur;
      if (cur.id) return cur;
      try {
        const s = (cur.ownerDocument.defaultView || window)
          .getComputedStyle(cur);
        const oy = (s.overflowY || '') + ' ' + (s.overflow || '');
        if (/(auto|scroll|overlay)/.test(oy) &&
            cur.scrollHeight > cur.clientHeight + 4) {
          if (!scrollable) scrollable = cur;
        }
      } catch (_) {}
      if (cur.parentElement) {
        cur = cur.parentElement;
      } else {
        // Hop out of an iframe to its host element.
        const doc = cur.ownerDocument;
        const win = doc && doc.defaultView;
        cur = (win && win.frameElement) || null;
      }
    }
    return testid || scrollable || el;
  }
  function onClick(e) {
    e.preventDefault();
    e.stopPropagation();
    const raw = hover ||
      (window.__rclElementFromPoint || document.elementFromPoint
        .bind(document))(e.clientX, e.clientY);
    const el = bestContainer(raw);
    window.__rclPicked = cssPath(el);
    cleanup();
  }


  function onKey(e) {
    if (e.key === 'Escape') {
      window.__rclCancelled = true;
      cleanup();
    }
  }
  function cleanup() {
    document.removeEventListener('mousemove', onMove, true);
    document.removeEventListener('click', onClick, true);
    document.removeEventListener('keydown', onKey, true);
    try { box.remove(); } catch (_) {}
    try { hint.remove(); } catch (_) {}
    window.__rclPickerActive = false;
  }
  document.addEventListener('mousemove', onMove, true);
  document.addEventListener('click', onClick, true);
  document.addEventListener('keydown', onKey, true);
  return 'started';
})()
"""

_PICKER_POLL_JS = (
    "(()=>JSON.stringify({"
    "picked:window.__rclPicked||null,"
    "cancelled:!!window.__rclCancelled,"
    "active:!!window.__rclPickerActive"
    "}))()"
)


@dataclass
class PickResult:
    ok: bool
    selector: str = ""
    error: str = ""


def _eval(ws: "websocket.WebSocket", expr: str,
          msg_id: int, timeout: float = 2.0) -> dict:
    ws.send(json.dumps({
        "id": msg_id, "method": "Runtime.evaluate",
        "params": {"expression": expr, "returnByValue": True},
    }))
    deadline = time.time() + timeout
    ws.settimeout(0.25)
    while time.time() < deadline:
        try:
            raw = ws.recv()
        except websocket.WebSocketTimeoutException:
            continue
        if not raw:
            continue
        try:
            msg = json.loads(raw)
        except Exception:
            continue
        if msg.get("id") == msg_id:
            return ((msg.get("result") or {}).get("result") or {})
    return {}


def pick_element_in_tab(ws_url: str,
                        timeout_s: float = 60.0) -> PickResult:
    """Open the tab's WS, inject the picker, wait for a click. Returns
    a CSS selector or an error string. Safe to call only while no other
    client owns the WebSocket (i.e. monitoring stopped)."""
    try:
        ws = websocket.create_connection(
            ws_url, timeout=5, suppress_origin=True)
    except Exception as e:
        return PickResult(False, error=classify_error(e))
    try:
        ws.send(json.dumps({"id": 1, "method": "Runtime.enable"}))
        # Install cross-frame query helper first so picker can descend iframes.
        _eval(ws, "(()=>{" + _QUERY_HELPER_JS + "return 1;})()",
              msg_id=10, timeout=2.0)
        start = _eval(ws, _PICKER_JS, msg_id=2, timeout=3.0)
        val = start.get("value")
        if val not in ("started", "already-active"):
            return PickResult(False,
                              error=f"Could not inject picker: {start!r}")

        deadline = time.time() + timeout_s
        mid = 100
        while time.time() < deadline:
            mid += 1
            res = _eval(ws, _PICKER_POLL_JS, msg_id=mid, timeout=1.0)
            raw = res.get("value")
            try:
                data = json.loads(raw) if isinstance(raw, str) else {}
            except Exception:
                data = {}
            if data.get("cancelled"):
                return PickResult(False, error="Cancelled in browser.")
            picked = data.get("picked")
            if picked:
                return PickResult(True, selector=str(picked))
            time.sleep(0.3)
        return PickResult(False, error="Timed out waiting for a click.")
    except Exception as e:
        return PickResult(False, error=classify_error(e))
    finally:
        try:
            ws.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# MutationObserver injection + selector test
# ---------------------------------------------------------------------------

_OBSERVER_JS_TMPL = r"""
(() => {
  __HELPER__
  try { if (window.__rclObs) window.__rclObs.disconnect(); } catch(_) {}
  window.__rclObs = null;
  const sel = __SEL__;
  const roots = window.__rclQueryAll(sel);
  function describe(el) {
    if (!el) return '(none)';
    let s = el.tagName.toLowerCase();
    if (el.id) s += '#' + el.id;
    const cls = (el.getAttribute('class') || '').trim().split(/\s+/)
      .filter(Boolean).slice(0, 2).join('.');
    if (cls) s += '.' + cls;
    try {
      const r = el.getBoundingClientRect();
      s += ' [' + Math.round(r.width) + 'x' + Math.round(r.height) + ']';
      s += ' children=' + el.children.length;
      const st = getComputedStyle(el);
      const oy = (st.overflowY || '') + ' ' + (st.overflow || '');
      const scroll = /(auto|scroll|overlay)/.test(oy) &&
        el.scrollHeight > el.clientHeight + 4;
      if (scroll) s += ' [scrollable]';
    } catch(_) {}
    return s;
  }
  const info = roots.length
    ? Array.from(roots).slice(0, 3).map(describe).join(' | ')
    : '(no match)';
  function reportMeta(ok) {
    try {
      window.__rclMeta(JSON.stringify({
        ok: !!ok, count: roots.length, info: info,
      }));
    } catch(_) {}
  }
  if (!roots.length) { reportMeta(false); return JSON.stringify({ok:false,count:0}); }

  if (!window.__rclSeen) window.__rclSeen = new Set();
  const seen = window.__rclSeen;
  function emit(t) {
    t = (t == null ? '' : String(t));
    const lines = t.split(/\r?\n/);
    for (const raw of lines) {
      const s = raw.replace(/\s+$/, '');
      if (!s) continue;
      if (seen.has(s)) continue;
      seen.add(s);
      if (seen.size > 5000) {
        const arr = Array.from(seen).slice(-2500);
        seen.clear(); arr.forEach(x => seen.add(x));
      }
      try { window.__rclLine(s); } catch(_) {}
    }
  }
  roots.forEach(r => emit(r.innerText || r.textContent || ''));
  const obs = new MutationObserver(muts => {
    for (const m of muts) {
      if (m.addedNodes) m.addedNodes.forEach(n =>
        emit(n.innerText || n.textContent || n.nodeValue || ''));
      if (m.type === 'characterData') emit(m.target.nodeValue || '');
    }
  });
  roots.forEach(r => obs.observe(r, {
    childList: true, subtree: true, characterData: true,
  }));
  window.__rclObs = obs;
  reportMeta(true);
  return JSON.stringify({ok:true, count:roots.length});
})()
"""




def _install_observer_impl(client: "CDPConsoleClient") -> None:
    if not client._ws or not client.dom_selector:
        return
    try:
        # Make sure the bindings exist (idempotent).
        client._send("Runtime.addBinding", {"name": "__rclLine"})
        client._send("Runtime.addBinding", {"name": "__rclMeta"})
        expr = (_OBSERVER_JS_TMPL
                .replace("__HELPER__", _QUERY_HELPER_JS)
                .replace("__SEL__", json.dumps(client.dom_selector)))
        client._send("Runtime.evaluate",
                     {"expression": expr, "returnByValue": True})
        client._observer_installed = True
        client.on_status(
            f"Mutation observer attached to '{client.dom_selector}' "
            f"(live, no polling).", True)
    except Exception:
        client._observer_installed = False



# Bind as method
CDPConsoleClient._install_mutation_observer = (  # type: ignore[attr-defined]
    lambda self: _install_observer_impl(self))


@dataclass
class SelectorTestResult:
    ok: bool
    count: int = 0
    samples: List[str] = field(default_factory=list)
    error: str = ""


def test_selector(ws_url: str, selector: str,
                  max_lines: int = 8,
                  settle_s: float = 1.5) -> SelectorTestResult:
    """One-shot WS: count matches and stream a few sample lines."""
    selector = (selector or "").strip()
    if not selector:
        return SelectorTestResult(False, error="Selector is empty.")
    try:
        ws = websocket.create_connection(
            ws_url, timeout=5, suppress_origin=True)
    except Exception as e:
        return SelectorTestResult(False, error=classify_error(e))

    samples: List[str] = []
    try:
        # Count + snapshot of existing text (cross-frame).
        expr_count = (
            "(()=>{" + _QUERY_HELPER_JS +
            "const els=window.__rclQueryAll("
            + json.dumps(selector)
            + ");return JSON.stringify({count:els.length,"
            "text:els.map(e=>e.innerText||e.textContent||'')"
            ".join('\\n')});})()"
        )
        ws.send(json.dumps({
            "id": 1, "method": "Runtime.evaluate",
            "params": {"expression": expr_count, "returnByValue": True},
        }))
        ws.settimeout(0.3)
        deadline = time.time() + 3.0
        snapshot = {"count": 0, "text": ""}
        while time.time() < deadline:
            try:
                raw = ws.recv()
            except websocket.WebSocketTimeoutException:
                continue
            if not raw:
                continue
            try:
                msg = json.loads(raw)
            except Exception:
                continue
            if msg.get("id") == 1:
                val = ((msg.get("result") or {})
                       .get("result", {}).get("value") or "{}")
                try:
                    snapshot = json.loads(val)
                except Exception:
                    pass
                break

        count = int(snapshot.get("count") or 0)
        if count == 0:
            return SelectorTestResult(
                False, count=0,
                error=(f"No element matches '{selector}'. "
                       "Tip: this page renders the log inside an <iframe>. "
                       "The selector now searches all same-origin iframes "
                       "automatically — try [data-testid=\"tritium-logger-"
                       "console\"]."))

        text = str(snapshot.get("text") or "")
        for ln in text.splitlines():
            ln = ln.rstrip()
            if ln:
                samples.append(ln)
            if len(samples) >= max_lines:
                break

        # Briefly observe for new lines via a temporary MutationObserver
        # piped through console.log with a unique prefix.
        prefix = "__RCL_SAMPLE__"
        watch_js = (
            "(()=>{" + _QUERY_HELPER_JS +
            "try{if(window.__rclTestObs)window.__rclTestObs"
            ".disconnect();}catch(_){}"
            "const sel=" + json.dumps(selector) + ";"
            "const roots=window.__rclQueryAll(sel);"
            "if(!roots.length)return 0;"
            "const obs=new MutationObserver(ms=>{for(const m of ms){"
            "if(m.addedNodes)m.addedNodes.forEach(n=>{const t=n.innerText"
            "||n.textContent||n.nodeValue||'';for(const ln of String(t)"
            ".split(/\\r?\\n/)){const s=ln.replace(/\\s+$/,'');"
            "if(s)console.log(" + json.dumps(prefix) + "+s);}});"
            "if(m.type==='characterData'){const s=String(m.target.nodeValue"
            "||'').trim();if(s)console.log(" + json.dumps(prefix) + "+s);}}"
            "});roots.forEach(r=>obs.observe(r,{childList:true,subtree:true,"
            "characterData:true}));window.__rclTestObs=obs;return roots"
            ".length;})()"
        )
        ws.send(json.dumps({"id": 2, "method": "Runtime.enable"}))
        ws.send(json.dumps({
            "id": 3, "method": "Runtime.evaluate",
            "params": {"expression": watch_js, "returnByValue": True},
        }))

        live: List[str] = []
        deadline = time.time() + settle_s
        while time.time() < deadline and len(live) < max_lines:
            try:
                raw = ws.recv()
            except websocket.WebSocketTimeoutException:
                continue
            if not raw:
                continue
            try:
                msg = json.loads(raw)
            except Exception:
                continue
            if msg.get("method") == "Runtime.consoleAPICalled":
                args = msg.get("params", {}).get("args", []) or []
                txt = " ".join(_fmt_arg(a) for a in args)
                if txt.startswith(prefix):
                    live.append(txt[len(prefix):])

        # Tear down the test observer
        try:
            ws.send(json.dumps({
                "id": 4, "method": "Runtime.evaluate",
                "params": {"expression":
                           "try{window.__rclTestObs&&"
                           "window.__rclTestObs.disconnect();}catch(_){}"},
            }))
        except Exception:
            pass

        # Merge: prefer live lines first if any
        merged: List[str] = []
        for ln in live + samples:
            if ln not in merged:
                merged.append(ln)
            if len(merged) >= max_lines:
                break

        return SelectorTestResult(True, count=count, samples=merged)
    except Exception as e:
        return SelectorTestResult(False, error=classify_error(e),
                                  samples=samples)
    finally:
        try:
            ws.close()
        except Exception:
            pass

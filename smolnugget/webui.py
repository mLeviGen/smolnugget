"""`snt web`: a local web UI. Stdlib http.server + one static page polling a small JSON API.

API (all JSON):
  GET  /api/status                      snapshot (same as `snt status --json`) + cpu/memory
  GET  /api/apps/<name>/logs?lines=N    last N lines of the app's output.log
  POST /api/apps/<name>/<action>        action: restart | update | start | stop

The server binds to 127.0.0.1 by default. Because any web page the user visits can send
requests to localhost, every request must carry a Host header naming this server (blocks
DNS rebinding) and POSTs must carry `X-Snt: 1` plus, if present, a matching Origin
(a cross-site page can't set custom headers without a CORS preflight, which we never allow).
"""

from __future__ import annotations

import json
import logging
import re
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from smolnugget.config import ConfigError, Store
from smolnugget.state import is_managed, send_control, snapshot
from smolnugget.top import Usage, tail

log = logging.getLogger("snt.web")

ACTIONS = ("restart", "update", "start", "stop")
APP_ROUTE = re.compile(r"^/api/apps/([A-Za-z0-9._-]+)/(logs|restart|update|start|stop)$")
MAX_LOG_LINES = 2000


class ActionError(Exception):
    def __init__(self, status: HTTPStatus, message: str):
        super().__init__(message)
        self.status = status


def load_page() -> bytes:
    return resources.files("smolnugget").joinpath("static/index.html").read_bytes()


class Page:
    """index.html, re-read when the file changes (handy while editing the UI)."""

    def __init__(self):
        self.path = Path(__file__).with_name("static") / "index.html"
        self.mtime = None
        self.body = load_page()

    def get(self) -> bytes:
        try:
            mtime = self.path.stat().st_mtime_ns
        except OSError:  # installed as a zip: no file to watch
            return self.body
        if mtime != self.mtime:
            self.mtime, self.body = mtime, self.path.read_bytes()
        return self.body


def run_action(store: Store, name: str, action: str) -> str:
    apps = store.load()
    app = apps.get(name)
    if app is None:
        raise ActionError(HTTPStatus.NOT_FOUND, f"no app named {name!r}")
    if action in ("start", "stop"):
        enable = action == "start"
        if app.enabled != enable:
            app.enabled = enable
            store.save(apps)
        return f"{name}: {'enabled' if enable else 'disabled'}"
    if not is_managed(store, name):
        hint = "it is disabled" if not app.enabled else "snt is not running it (start: snt run)"
        raise ActionError(HTTPStatus.CONFLICT, f"{name} is not running: {hint}")
    send_control(store, name, action)
    return f"{name}: {'restart' if action == 'restart' else 'update check'} requested"


class Handler(BaseHTTPRequestHandler):
    server: "WebServer"
    protocol_version = "HTTP/1.1"

    # --- plumbing ---------------------------------------------------------
    def log_message(self, fmt, *args):  # keep the console quiet; errors are logged explicitly
        log.debug("%s %s", self.address_string(), fmt % args)

    def _send(self, status: int, body: bytes, ctype: str, extra: dict | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, status: int, data) -> None:
        self._send(status, json.dumps(data).encode(), "application/json; charset=utf-8")

    def _error(self, status: HTTPStatus, message: str) -> None:
        self._json(status, {"error": message})

    def _host_ok(self) -> bool:
        host = (self.headers.get("Host") or "").lower()
        return host in self.server.allowed_hosts

    # --- routes -----------------------------------------------------------------
    def do_GET(self):
        if not self._host_ok():
            return self._error(HTTPStatus.FORBIDDEN, "unexpected Host header")
        url = urlsplit(self.path)
        if url.path in ("/", "/index.html"):
            csp = ("default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
                   "img-src data:; connect-src 'self'; base-uri 'none'; form-action 'none'; "
                   "frame-ancestors 'none'")
            return self._send(HTTPStatus.OK, self.server.page.get(), "text/html; charset=utf-8",
                              {"Content-Security-Policy": csp, "X-Frame-Options": "DENY"})
        if url.path == "/api/status":
            try:
                return self._json(HTTPStatus.OK, self.server.status())
            except ConfigError as e:
                return self._error(HTTPStatus.INTERNAL_SERVER_ERROR, str(e))
        m = APP_ROUTE.match(url.path)
        if m and m.group(2) == "logs":
            name = m.group(1)
            try:
                if name not in self.server.store.load():
                    return self._error(HTTPStatus.NOT_FOUND, f"no app named {name!r}")
            except ConfigError as e:
                return self._error(HTTPStatus.INTERNAL_SERVER_ERROR, str(e))
            try:
                lines = int(parse_qs(url.query).get("lines", ["300"])[0])
            except ValueError:
                lines = 300
            lines = max(1, min(lines, MAX_LOG_LINES))
            return self._json(HTTPStatus.OK, {
                "name": name, "lines": tail(self.server.store.log_path(name), lines)})
        return self._error(HTTPStatus.NOT_FOUND, "not found")

    do_HEAD = do_GET

    def do_POST(self):
        if not self._host_ok():
            return self._error(HTTPStatus.FORBIDDEN, "unexpected Host header")
        if self.headers.get("X-Snt") != "1":
            return self._error(HTTPStatus.FORBIDDEN, "missing X-Snt header")
        origin = self.headers.get("Origin")
        if origin and urlsplit(origin).netloc.lower() not in self.server.allowed_hosts:
            return self._error(HTTPStatus.FORBIDDEN, "cross-origin request refused")
        if int(self.headers.get("Content-Length") or 0):
            self.rfile.read(int(self.headers["Content-Length"]))
        m = APP_ROUTE.match(urlsplit(self.path).path)
        if not m or m.group(2) not in ACTIONS:
            return self._error(HTTPStatus.NOT_FOUND, "not found")
        try:
            message = run_action(self.server.store, m.group(1), m.group(2))
        except ActionError as e:
            return self._error(e.status, str(e))
        except ConfigError as e:
            return self._error(HTTPStatus.INTERNAL_SERVER_ERROR, str(e))
        log.info("%s", message)
        return self._json(HTTPStatus.OK, {"ok": True, "message": message})

    def do_OPTIONS(self):  # never grant CORS
        self._error(HTTPStatus.FORBIDDEN, "CORS is not allowed")


class WebServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, store: Store, host: str = "127.0.0.1", port: int = 8770):
        super().__init__((host, port), Handler)
        self.store = store
        self.page = Page()
        self._usage = Usage()
        self._usage_lock = threading.Lock()
        real_port = self.server_address[1]
        names = {host, "localhost", "127.0.0.1", "[::1]"} if host in ("127.0.0.1", "localhost", "::1") \
            else {host}
        self.allowed_hosts = {f"{n}:{real_port}".lower() for n in names}

    @property
    def url(self) -> str:
        host = self.server_address[0]
        host = "localhost" if host in ("127.0.0.1", "::1") else host
        return f"http://{host}:{self.server_address[1]}/"

    def status(self) -> dict:
        apps = list(self.store.load().values())
        snap = snapshot(self.store, apps)
        with self._usage_lock:
            for a in snap["apps"]:
                cpu, mem = self._usage.get_raw(a["pid"])
                a["cpu_percent"], a["memory_bytes"] = cpu, mem
        return snap

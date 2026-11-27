"""`snt web` / `snt hub`: the web UI. Stdlib http.server + one static page polling a JSON API.

The UI always talks to one or more *sources* (servers). `snt web` serves a single source,
this machine; `snt hub` adds every saved remote server, each reached through its own ssh
tunnel (see hub.py).

API (all JSON, all need `Authorization: Bearer <token>`):
  GET  /api/status                               every server and every app
  GET  /api/s/<server>/system                    host metrics: cpu, memory, disk, load, uptime
  GET  /api/s/<server>/apps/<app>/logs?lines=N   last N lines of the app's output.log
  POST /api/s/<server>/apps/<app>/<action>       action: restart | update | start | stop

Security
  * binds to 127.0.0.1 by default;
  * a random token is made per run and handed to the browser in the URL it prints
    (`?t=...`); without it the API answers 401, so other users and processes on the
    same machine can't drive it;
  * the Host header must name this server (blocks DNS rebinding);
  * POSTs need `X-Snt: 1` and, if present, a matching Origin (a foreign page can't send
    custom headers without a CORS preflight, which is never granted); bodies are capped.
"""

from __future__ import annotations

import hmac
import json
import logging
import re
import secrets
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from smolnugget import __version__
from smolnugget.config import ConfigError, Store
from smolnugget.state import is_managed, iso, send_control, snapshot
from smolnugget.sysinfo import SystemInfo
from smolnugget.top import Usage, tail

log = logging.getLogger("snt.web")

SCHEMA = 2
ACTIONS = ("restart", "update", "start", "stop")
NAME = r"[A-Za-z0-9._-]+"
SYSTEM_ROUTE = re.compile(rf"^/api/s/({NAME})/system$")
APP_ROUTE = re.compile(rf"^/api/s/({NAME})/apps/({NAME})/(logs|restart|update|start|stop)$")
MAX_LOG_LINES = 2000
MAX_BODY = 64 * 1024


class SourceError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = int(status)


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


# --- sources -------------------------------------------------------------------------
class LocalSource:
    """This machine's snt home."""

    state = "online"
    error = None

    def __init__(self, store: Store, name: str = "local"):
        self.name = name
        self.store = store
        self.label = socket.gethostname()
        self._usage = Usage()
        self._lock = threading.Lock()
        self.sysinfo = SystemInfo(store.home)

    def status(self) -> dict:
        try:
            apps = list(self.store.load().values())
        except ConfigError as e:
            raise SourceError(500, str(e)) from e
        snap = snapshot(self.store, apps)
        with self._lock:
            for a in snap["apps"]:
                a["cpu_percent"], a["memory_bytes"] = self._usage.get_raw(a["pid"])
        return {"label": self.label, "daemon": snap["daemon"], "apps": snap["apps"]}

    def system(self) -> dict:
        with self._lock:
            return self.sysinfo.snapshot()

    def _known(self, app: str) -> None:
        try:
            if app not in self.store.load():
                raise SourceError(404, f"no app named {app!r}")
        except ConfigError as e:
            raise SourceError(500, str(e)) from e

    def logs(self, app: str, lines: int) -> dict:
        self._known(app)
        return {"name": app, "lines": tail(self.store.log_path(app), lines)}

    def action(self, app: str, action: str) -> str:
        try:
            apps = self.store.load()
        except ConfigError as e:
            raise SourceError(500, str(e)) from e
        cfg = apps.get(app)
        if cfg is None:
            raise SourceError(404, f"no app named {app!r}")
        if action in ("start", "stop"):
            enable = action == "start"
            if cfg.enabled != enable:
                cfg.enabled = enable
                self.store.save(apps)
            return f"{app}: {'enabled' if enable else 'disabled'}"
        if not is_managed(self.store, app):
            hint = "it is disabled" if not cfg.enabled else "snt is not running it (start: snt run)"
            raise SourceError(409, f"{app} is not running: {hint}")
        send_control(self.store, app, action)
        return f"{app}: {'restart' if action == 'restart' else 'update check'} requested"

    def close(self) -> None:
        pass


# --- HTTP ----------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    server: "WebServer"
    protocol_version = "HTTP/1.1"

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

    def _error(self, status: int, message: str) -> None:
        self._json(status, {"error": message})

    def _host_ok(self) -> bool:
        return (self.headers.get("Host") or "").lower() in self.server.allowed_hosts

    def _auth_ok(self) -> bool:
        got = self.headers.get("Authorization") or ""
        want = f"Bearer {self.server.token}"
        ok = hmac.compare_digest(got.encode(), want.encode())
        if ok:
            self.server.last_request = time.monotonic()
        return ok

    def _source(self, name: str):
        src = self.server.sources.get(name)
        if src is None:
            raise SourceError(404, f"no server named {name!r}")
        return src

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
        if not url.path.startswith("/api/"):
            return self._error(HTTPStatus.NOT_FOUND, "not found")
        if not self._auth_ok():
            return self._error(HTTPStatus.UNAUTHORIZED, "missing or wrong token")
        try:
            if url.path == "/api/ping":  # keepalive from tunnels and the hub
                return self._json(HTTPStatus.OK, {"ok": True})
            if url.path == "/api/status":
                return self._json(HTTPStatus.OK, self.server.status())
            if m := SYSTEM_ROUTE.match(url.path):
                return self._json(HTTPStatus.OK, self._source(m.group(1)).system())
            m = APP_ROUTE.match(url.path)
            if m and m.group(3) == "logs":
                try:
                    lines = int(parse_qs(url.query).get("lines", ["300"])[0])
                except ValueError:
                    lines = 300
                lines = max(1, min(lines, MAX_LOG_LINES))
                return self._json(HTTPStatus.OK, self._source(m.group(1)).logs(m.group(2), lines))
        except SourceError as e:
            return self._error(e.status, str(e))
        return self._error(HTTPStatus.NOT_FOUND, "not found")

    do_HEAD = do_GET

    def do_POST(self):
        if not self._host_ok():
            return self._error(HTTPStatus.FORBIDDEN, "unexpected Host header")
        if not self._auth_ok():
            return self._error(HTTPStatus.UNAUTHORIZED, "missing or wrong token")
        if self.headers.get("X-Snt") != "1":
            return self._error(HTTPStatus.FORBIDDEN, "missing X-Snt header")
        origin = self.headers.get("Origin")
        if origin and urlsplit(origin).netloc.lower() not in self.server.allowed_hosts:
            return self._error(HTTPStatus.FORBIDDEN, "cross-origin request refused")
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = -1
        if length < 0 or length > MAX_BODY:
            self.close_connection = True
            return self._error(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "request body too large")
        if length:
            self.rfile.read(length)
        m = APP_ROUTE.match(urlsplit(self.path).path)
        if not m or m.group(3) not in ACTIONS:
            return self._error(HTTPStatus.NOT_FOUND, "not found")
        try:
            message = self._source(m.group(1)).action(m.group(2), m.group(3))
        except SourceError as e:
            return self._error(e.status, str(e))
        log.info("%s: %s", m.group(1), message)
        return self._json(HTTPStatus.OK, {"ok": True, "message": message})

    def do_OPTIONS(self):  # never grant CORS
        self._error(HTTPStatus.FORBIDDEN, "CORS is not allowed")


class WebServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, sources, host: str = "127.0.0.1", port: int = 8770,
                 token: str | None = None, hub: bool = False):
        super().__init__((host, port), Handler)
        self.sources = {s.name: s for s in sources}
        self.hub = hub
        self.token = token or secrets.token_urlsafe(24)
        self.page = Page()
        self._pool = ThreadPoolExecutor(max_workers=max(4, len(self.sources)))
        self._cache: tuple[float, dict] | None = None
        self._cache_lock = threading.Lock()
        self.last_request = time.monotonic()
        real_port = self.server_address[1]
        names = {host, "localhost", "127.0.0.1", "[::1]"} if host in ("127.0.0.1", "localhost", "::1") \
            else {host}
        self.allowed_hosts = {f"{n}:{real_port}".lower() for n in names}

    @property
    def url(self) -> str:
        host = self.server_address[0]
        host = "localhost" if host in ("127.0.0.1", "::1") else host
        return f"http://{host}:{self.server_address[1]}/"

    @property
    def login_url(self) -> str:
        return f"{self.url}?t={self.token}"

    def _one(self, src) -> tuple[dict, list]:
        try:
            st = src.status()
        except SourceError as e:
            return ({"name": src.name, "label": getattr(src, "label", src.name),
                     "state": getattr(src, "state", "offline") if src.state != "online" else "offline",
                     "error": str(e), "daemon": None}, [])
        apps = st["apps"]
        for a in apps:
            a["server"] = src.name
        return ({"name": src.name, "label": st.get("label") or src.label, "state": "online",
                 "error": None, "daemon": st.get("daemon")}, apps)

    def status(self) -> dict:
        """All servers, fetched in parallel; cached for a second so several tabs stay cheap."""
        with self._cache_lock:
            if self._cache and time.monotonic() - self._cache[0] < 1.0:
                return self._cache[1]
        results = list(self._pool.map(self._one, self.sources.values()))
        data = {
            "schema": SCHEMA,
            "smolnugget": __version__,
            "generated_at": iso(time.time()),
            "hub": self.hub,
            "servers": [srv for srv, _ in results],
            "apps": [a for _, apps in results for a in apps],
        }
        with self._cache_lock:
            self._cache = (time.monotonic(), data)
        return data

    def server_close(self) -> None:
        super().server_close()
        self._pool.shutdown(wait=False)
        for s in self.sources.values():
            s.close()

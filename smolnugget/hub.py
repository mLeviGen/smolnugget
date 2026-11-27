"""`snt hub`: one dashboard for every saved server.

Each saved remote becomes a source behind its own ssh tunnel to `snt web` on that server
(see remote.Tunnel). A background thread keeps the tunnel up and reconnects when it
drops; while it is down the server shows as offline and its apps disappear from the list.
"""

from __future__ import annotations

import json
import logging
import threading
from urllib.parse import quote

from smolnugget.remote import Remote, RemoteError, Tunnel
from smolnugget.webui import SourceError

log = logging.getLogger("snt.hub")

RETRY_SECONDS = 15


class RemoteSource:
    def __init__(self, remote: Remote):
        self.name = remote.name
        self.label = remote.target
        self.remote = remote
        self.tunnel = Tunnel(remote)
        self.state = "connecting"
        self.error: str | None = None
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._keep_up, name=f"hub-{remote.name}", daemon=True)
        self._thread.start()

    # --- connection -----------------------------------------------------------------
    def _keep_up(self) -> None:
        while not self._stop.is_set():
            if not self.tunnel.alive():
                if self.state == "online":
                    log.warning("%s: connection lost (%s), reconnecting",
                                self.name, self.tunnel.diagnose())
                self.state = "connecting"
                try:
                    self.tunnel.start()
                    self.state, self.error = "online", None
                    log.info("%s: connected", self.name)
                except RemoteError as e:
                    self.state, self.error = "offline", str(e)
                    log.warning("%s: %s", self.name, e)
                    self._stop.wait(RETRY_SECONDS)
                    continue
            self._stop.wait(2)

    def close(self) -> None:
        self._stop.set()
        self.tunnel.close()

    # --- API ------------------------------------------------------------------------
    def _call(self, method: str, path: str) -> dict:
        if self.state != "online" or not self.tunnel.alive():
            raise SourceError(503, self.error or "connecting…")
        code, body = self.tunnel.request(method, path)
        try:
            data = json.loads(body or b"{}")
        except ValueError:
            data = {}
        if code == 0:
            raise SourceError(503, "no answer from the server")
        if code != 200:
            raise SourceError(code, data.get("error") or f"HTTP {code}")
        return data

    def status(self) -> dict:
        data = self._call("GET", "/api/status")
        servers = data.get("servers")
        if not servers:
            raise SourceError(502, "snt on the server is too old for the hub; update it")
        here = servers[0]
        return {"label": here.get("label") or self.label, "daemon": here.get("daemon"),
                "apps": data.get("apps", [])}

    def system(self) -> dict:
        return self._call("GET", "/api/s/local/system")

    def logs(self, app: str, lines: int) -> dict:
        return self._call("GET", f"/api/s/local/apps/{quote(app)}/logs?lines={lines}")

    def icon(self, app: str) -> bytes:
        if self.state != "online" or not self.tunnel.alive():
            raise SourceError(503, self.error or "connecting…")
        code, body = self.tunnel.request("GET", f"/api/s/local/apps/{quote(app)}/icon")
        if code != 200:
            raise SourceError(code or 503, "no icon")
        return body

    def action(self, app: str, action: str) -> str:
        return self._call("POST", f"/api/s/local/apps/{quote(app)}/{action}").get("message", "")

import http.client
import json
import tempfile
import threading
import unittest
from pathlib import Path

from smolnugget.config import AppConfig, Store
from smolnugget.locks import FileLock
from smolnugget.webui import MAX_BODY, LocalSource, SourceError, WebServer


class FakeRemote:
    """A source that behaves like a hub RemoteSource without ssh."""

    def __init__(self, name, online=True):
        self.name, self.label = name, f"me@{name}"
        self.state = "online" if online else "offline"
        self.error = None if online else "ssh could not connect"
        self.actions = []

    def status(self):
        if self.state != "online":
            raise SourceError(503, self.error)
        return {"label": self.label, "daemon": {"running": True, "pid": 7},
                "apps": [{"name": "bot", "status": "running", "pid": 99}]}

    def system(self):
        if self.state != "online":
            raise SourceError(503, self.error)
        return {"hostname": self.name}

    def logs(self, app, lines):
        return {"name": app, "lines": [f"{self.name} line"]}

    def icon(self, app):
        return b"\x89PNG\r\n\x1a\n" + b"0" * 10

    def action(self, app, action):
        self.actions.append((app, action))
        return f"{app}: {action} ok"

    def close(self):
        pass


class WebTestBase(unittest.TestCase):
    sources = None

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.store = Store(Path(tmp.name))
        self.store.save({
            "bot": AppConfig(name="bot", url="https://github.com/u/bot", command=["python", "bot.py"],
                             env={"SECRET": "hunter2"}),
            "web": AppConfig(name="web", url="https://github.com/u/web", command=["npm", "start"],
                             enabled=False),
        })
        self.server = WebServer(self.make_sources(), "127.0.0.1", 0, token="tok")
        self.port = self.server.server_address[1]
        t = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05})
        t.start()
        self.addCleanup(lambda: (self.server.shutdown(), self.server.server_close(), t.join()))

    def make_sources(self):
        return [LocalSource(self.store)]

    def request(self, method, path, headers=None, host=None, auth=True, body=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        hdrs = {"Host": host or f"localhost:{self.port}"}
        if auth:
            hdrs["Authorization"] = "Bearer tok"
        hdrs.update(headers or {})
        conn.request(method, path, body=body, headers=hdrs)
        resp = conn.getresponse()
        data = resp.read()
        conn.close()
        return resp, data

    def get_json(self, path):
        resp, body = self.request("GET", path)
        self.assertEqual(resp.status, 200, body)
        return json.loads(body)

    def post(self, path, **headers):
        h = {"X-Snt": "1"}
        h.update(headers)
        return self.request("POST", path, h)


class LocalWebTest(WebTestBase):
    def test_page_needs_no_token_but_api_does(self):
        resp, body = self.request("GET", "/", auth=False)
        self.assertEqual(resp.status, 200)
        self.assertIn(b"smolnugget", body)
        self.assertIn("default-src 'none'", resp.getheader("Content-Security-Policy"))
        self.assertEqual(resp.getheader("X-Frame-Options"), "DENY")
        self.assertEqual(self.request("GET", "/api/status", auth=False)[0].status, 401)
        self.assertEqual(self.request("GET", "/api/status",
                                      headers={"Authorization": "Bearer nope"})[0].status, 401)
        self.assertEqual(self.request("POST", "/api/s/local/apps/web/start",
                                      headers={"X-Snt": "1"}, auth=False)[0].status, 401)
        self.assertTrue(self.server.login_url.endswith("/?t=tok"))

    def test_status(self):
        resp, body = self.request("GET", "/api/status")
        self.assertNotIn(b"hunter2", body)
        data = json.loads(body)
        self.assertEqual(data["schema"], 2)
        self.assertFalse(data["hub"])
        self.assertEqual([s["name"] for s in data["servers"]], ["local"])
        self.assertEqual(data["servers"][0]["state"], "online")
        self.assertFalse(data["servers"][0]["daemon"]["running"])
        self.assertEqual([(a["server"], a["name"]) for a in data["apps"]], [("local", "bot"), ("local", "web")])
        self.assertEqual(data["apps"][1]["status"], "disabled")
        self.assertIn("cpu_percent", data["apps"][0])

    def test_system(self):
        data = self.get_json("/api/s/local/system")
        for key in ("hostname", "os", "cpu_count", "cpu_percent", "memory", "disk", "uptime_seconds"):
            self.assertIn(key, data)
        self.assertGreater(data["disk"]["total"], 0)
        self.assertEqual(self.request("GET", "/api/s/nope/system")[0].status, 404)

    def test_wrong_host_rejected(self):
        # DNS rebinding: evil.example resolving to 127.0.0.1
        resp, _ = self.request("GET", "/api/status", host=f"evil.example:{self.port}")
        self.assertEqual(resp.status, 403)
        resp, _ = self.request("GET", "/api/status", host=f"127.0.0.1:{self.port}")
        self.assertEqual(resp.status, 200)

    def test_post_needs_header_and_same_origin(self):
        resp, _ = self.request("POST", "/api/s/local/apps/web/start")
        self.assertEqual(resp.status, 403)
        resp, _ = self.post("/api/s/local/apps/web/start", Origin="https://evil.example")
        self.assertEqual(resp.status, 403)
        resp, _ = self.request("OPTIONS", "/api/s/local/apps/web/start")
        self.assertEqual(resp.status, 403)
        self.assertFalse(self.store.get("web").enabled)
        resp, _ = self.post("/api/s/local/apps/web/start", Origin=f"http://localhost:{self.port}")
        self.assertEqual(resp.status, 200)
        self.assertTrue(self.store.get("web").enabled)

    def test_body_size_capped(self):
        resp, _ = self.request("POST", "/api/s/local/apps/bot/stop", headers={"X-Snt": "1"},
                               body=b"x" * (MAX_BODY + 1))
        self.assertEqual(resp.status, 413)
        self.assertTrue(self.store.get("bot").enabled)

    def test_start_stop(self):
        resp, _ = self.post("/api/s/local/apps/bot/stop")
        self.assertEqual(resp.status, 200)
        self.assertFalse(self.store.get("bot").enabled)
        self.post("/api/s/local/apps/bot/start")
        self.assertTrue(self.store.get("bot").enabled)

    def test_restart_needs_running_app(self):
        resp, body = self.post("/api/s/local/apps/bot/restart")
        self.assertEqual(resp.status, 409)
        lock = FileLock(self.store.lock_path("bot"))  # pretend a daemon manages it
        self.assertTrue(lock.acquire())
        try:
            resp, _ = self.post("/api/s/local/apps/bot/restart")
            self.assertEqual(resp.status, 200)
            self.assertEqual(self.store.control_path("bot").read_text().strip(), "restart")
        finally:
            lock.release()

    def test_unknown(self):
        self.assertEqual(self.post("/api/s/local/apps/nope/restart")[0].status, 404)
        self.assertEqual(self.post("/api/s/local/apps/bot/explode")[0].status, 404)
        self.assertEqual(self.request("GET", "/api/s/local/apps/nope/logs")[0].status, 404)
        self.assertEqual(self.request("GET", "/api/nothing")[0].status, 404)
        self.assertEqual(self.request("GET", "/nothing", auth=False)[0].status, 404)

    def test_app_icon_from_repo(self):
        self.assertIsNone(self.get_json("/api/status")["apps"][0]["icon"])
        self.assertEqual(self.request("GET", "/api/s/local/apps/bot/icon")[0].status, 404)
        repo = self.store.repo_dir("bot")
        (repo / "assets").mkdir(parents=True)
        svg = b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1 1"/>'
        (repo / "assets" / "logo.svg").write_bytes(svg)
        self.server._cache = None  # /api/status is cached for a second
        self.assertTrue(self.get_json("/api/status")["apps"][0]["icon"].startswith("logo.svg:"))
        resp, body = self.request("GET", "/api/s/local/apps/bot/icon")
        self.assertEqual((resp.status, body), (200, svg))
        self.assertEqual(resp.getheader("Content-Type"), "image/svg+xml")
        self.assertIn("sandbox", resp.getheader("Content-Security-Policy"))
        self.assertEqual(self.request("GET", "/api/s/local/apps/bot/icon", auth=False)[0].status, 401)
        (repo / "icon.png").write_bytes(b"GIF89a not really a png")  # wins by order, wrong type
        self.assertEqual(self.request("GET", "/api/s/local/apps/bot/icon")[0].status, 415)

    def test_logs(self):
        path = self.store.log_path("bot")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(f"2026-01-01 00:00:00   line {i}\n" for i in range(50)))
        data = self.get_json("/api/s/local/apps/bot/logs?lines=3")
        self.assertEqual(data["lines"][-1], "2026-01-01 00:00:00   line 49")
        self.assertEqual(len(data["lines"]), 3)
        self.assertEqual(self.get_json("/api/s/local/apps/web/logs?lines=abc")["lines"], [])


class IdleExitTest(unittest.TestCase):
    def test_tunnel_server_exits_when_nobody_asks(self):
        import contextlib
        import io
        import time

        from smolnugget.cli import serve
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        server = WebServer([LocalSource(Store(Path(tmp.name)))], "127.0.0.1", 0, token="tok")
        done = threading.Event()

        def run():
            with contextlib.redirect_stdout(io.StringIO()):
                serve(server, False, idle_exit=1)
            done.set()

        start = time.monotonic()
        threading.Thread(target=run, daemon=True).start()
        self.assertTrue(done.wait(10), "server did not exit after being idle")
        self.assertLess(time.monotonic() - start, 5)


class HubWebTest(WebTestBase):
    def make_sources(self):
        self.vps, self.gcp = FakeRemote("vps"), FakeRemote("gcp", online=False)
        return [LocalSource(self.store), self.vps, self.gcp]

    def test_status_merges_servers(self):
        data = self.get_json("/api/status")
        self.assertEqual([s["name"] for s in data["servers"]], ["local", "vps", "gcp"])
        gcp = data["servers"][2]
        self.assertEqual((gcp["state"], gcp["error"]), ("offline", "ssh could not connect"))
        self.assertEqual(sorted((a["server"], a["name"]) for a in data["apps"]),
                         [("local", "bot"), ("local", "web"), ("vps", "bot")])

    def test_systems_and_totals(self):
        self.vps.system = lambda: {"hostname": "vps", "cpu_count": 1, "cpu_percent": 50.0,
                                   "memory": {"total": 1000, "used": 600},
                                   "disk": {"total": 10_000, "used": 9_000}}
        data = self.get_json("/api/systems")
        by_name = {s["name"]: s for s in data["servers"]}
        self.assertEqual(set(by_name), {"local", "vps", "gcp"})
        self.assertEqual(by_name["gcp"]["state"], "offline")
        self.assertIsNone(by_name["gcp"]["system"])
        local = by_name["local"]["system"]
        t = data["totals"]
        self.assertEqual(t["servers"], 2)  # the offline one is not counted
        self.assertEqual(t["cpu_count"], local["cpu_count"] + 1)
        # memory is unknown where neither psutil nor /proc exists (e.g. macOS/Windows in CI)
        local_mem = local["memory"]["total"] if local["memory"] else 0
        self.assertEqual(t["memory"]["total"], local_mem + 1000)
        self.assertEqual(t["disk"]["used"], local["disk"]["used"] + 9_000)

    def test_totals_weight_cpu_by_cores(self):
        from smolnugget.webui import totals
        t = totals([{"cpu_count": 1, "cpu_percent": 100.0, "memory": None, "disk": None},
                    {"cpu_count": 3, "cpu_percent": 0.0}])
        self.assertEqual((t["cpu_count"], t["cpu_percent"]), (4, 25.0))
        self.assertIsNone(t["memory"])
        self.assertEqual(totals([])["cpu_percent"], None)

    def test_routes_reach_the_right_server(self):
        self.assertEqual(self.get_json("/api/s/vps/system")["hostname"], "vps")
        self.assertEqual(self.get_json("/api/s/vps/apps/bot/logs")["lines"], ["vps line"])
        resp, _ = self.request("GET", "/api/s/vps/apps/bot/icon")
        self.assertEqual((resp.status, resp.getheader("Content-Type")), (200, "image/png"))
        resp, _ = self.post("/api/s/vps/apps/bot/restart")
        self.assertEqual(resp.status, 200)
        self.assertEqual(self.vps.actions, [("bot", "restart")])
        self.assertFalse(self.store.control_path("bot").exists())  # local app untouched


if __name__ == "__main__":
    unittest.main()

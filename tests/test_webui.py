import http.client
import json
import tempfile
import threading
import unittest
from pathlib import Path

from smolnugget.config import AppConfig, Store
from smolnugget.locks import FileLock
from smolnugget.webui import WebServer


class WebUITest(unittest.TestCase):
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
        self.server = WebServer(self.store, "127.0.0.1", 0)
        self.port = self.server.server_address[1]
        t = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05})
        t.start()
        self.addCleanup(lambda: (self.server.shutdown(), self.server.server_close(), t.join()))

    def request(self, method, path, headers=None, host=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        hdrs = {"Host": host or f"localhost:{self.port}"}
        hdrs.update(headers or {})
        conn.request(method, path, headers=hdrs)
        resp = conn.getresponse()
        body = resp.read()
        conn.close()
        return resp, body

    def post(self, path, **headers):
        h = {"X-Snt": "1"}
        h.update(headers)
        return self.request("POST", path, h)

    def test_page(self):
        resp, body = self.request("GET", "/")
        self.assertEqual(resp.status, 200)
        self.assertIn(b"smolnugget", body)
        self.assertIn("default-src 'none'", resp.getheader("Content-Security-Policy"))
        self.assertEqual(resp.getheader("X-Frame-Options"), "DENY")

    def test_status(self):
        resp, body = self.request("GET", "/api/status")
        self.assertEqual(resp.status, 200)
        self.assertNotIn(b"hunter2", body)
        data = json.loads(body)
        self.assertEqual([a["name"] for a in data["apps"]], ["bot", "web"])
        self.assertEqual(data["apps"][1]["status"], "disabled")
        self.assertIn("cpu_percent", data["apps"][0])
        self.assertFalse(data["daemon"]["running"])

    def test_wrong_host_rejected(self):
        # DNS rebinding: evil.example resolving to 127.0.0.1
        resp, _ = self.request("GET", "/api/status", host=f"evil.example:{self.port}")
        self.assertEqual(resp.status, 403)
        resp, _ = self.request("GET", "/api/status", host=f"127.0.0.1:{self.port}")
        self.assertEqual(resp.status, 200)

    def test_post_needs_header_and_same_origin(self):
        resp, _ = self.request("POST", "/api/apps/web/start")
        self.assertEqual(resp.status, 403)
        resp, _ = self.post("/api/apps/web/start", Origin="https://evil.example")
        self.assertEqual(resp.status, 403)
        resp, _ = self.request("OPTIONS", "/api/apps/web/start")
        self.assertEqual(resp.status, 403)
        self.assertFalse(self.store.get("web").enabled)
        resp, _ = self.post("/api/apps/web/start", Origin=f"http://localhost:{self.port}")
        self.assertEqual(resp.status, 200)
        self.assertTrue(self.store.get("web").enabled)

    def test_start_stop(self):
        resp, _ = self.post("/api/apps/bot/stop")
        self.assertEqual(resp.status, 200)
        self.assertFalse(self.store.get("bot").enabled)
        self.post("/api/apps/bot/start")
        self.assertTrue(self.store.get("bot").enabled)

    def test_restart_needs_running_app(self):
        resp, body = self.post("/api/apps/bot/restart")
        self.assertEqual(resp.status, 409)
        lock = FileLock(self.store.lock_path("bot"))  # pretend a daemon manages it
        self.assertTrue(lock.acquire())
        try:
            resp, _ = self.post("/api/apps/bot/restart")
            self.assertEqual(resp.status, 200)
            self.assertEqual(self.store.control_path("bot").read_text().strip(), "restart")
        finally:
            lock.release()

    def test_unknown(self):
        self.assertEqual(self.post("/api/apps/nope/restart")[0].status, 404)
        self.assertEqual(self.post("/api/apps/bot/explode")[0].status, 404)
        self.assertEqual(self.request("GET", "/api/apps/nope/logs")[0].status, 404)
        self.assertEqual(self.request("GET", "/nothing")[0].status, 404)

    def test_logs(self):
        path = self.store.log_path("bot")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(f"2026-01-01 00:00:00   line {i}\n" for i in range(50)))
        resp, body = self.request("GET", "/api/apps/bot/logs?lines=3")
        self.assertEqual(resp.status, 200)
        self.assertEqual(json.loads(body)["lines"][-1], "2026-01-01 00:00:00   line 49")
        self.assertEqual(len(json.loads(body)["lines"]), 3)
        resp, body = self.request("GET", "/api/apps/web/logs?lines=abc")
        self.assertEqual(json.loads(body)["lines"], [])


if __name__ == "__main__":
    unittest.main()

import json
import tempfile
import unittest
from pathlib import Path

from smolnugget.detect import detect


class DetectTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, name, text=""):
        (self.repo / name).write_text(text)

    def test_nothing(self):
        self.write("README.md")
        self.assertIsNone(detect(self.repo))

    def test_procfile_prefers_worker(self):
        self.write("Procfile", "web: gunicorn app:app\nworker: python bot.py --prod\n")
        self.write("main.py")
        d = detect(self.repo)
        self.assertEqual(d.command, ["python", "bot.py", "--prod"])
        self.assertTrue(d.venv)

    def test_node(self):
        self.write("package.json", json.dumps({"scripts": {"start": "node ."}}))
        self.write("package-lock.json", "{}")
        d = detect(self.repo)
        self.assertEqual((d.command, d.setup, d.venv), (["npm", "start"], ["npm ci"], False))

    def test_python_entrypoint_order(self):
        self.write("utils.py")
        self.write("bot.py")
        self.write("main.py")
        self.assertEqual(detect(self.repo).command, ["python", "main.py"])

    def test_single_python_file(self):
        self.write("snt.py")
        self.write("setup.py")
        d = detect(self.repo)
        self.assertEqual(d.command, ["python", "snt.py"])
        self.assertTrue(d.venv)

    def test_ambiguous_python(self):
        self.write("a.py")
        self.write("b.py")
        self.assertIsNone(detect(self.repo))


if __name__ == "__main__":
    unittest.main()

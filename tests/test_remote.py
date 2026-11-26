import contextlib
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from smolnugget import remote as rm
from smolnugget.cli import main
from smolnugget.config import Store


class StripArgsTest(unittest.TestCase):
    def test_only_global_part_is_touched(self):
        f = rm.strip_remote_args
        self.assertEqual(f(["-r", "vps", "status", "--json"]), ["status", "--json"])
        self.assertEqual(f(["-v", "--remote=vps", "--home", "/x", "logs", "a", "-n", "5"]),
                         ["-v", "--home", "/x", "logs", "a", "-n", "5"])
        self.assertEqual(f(["-rvps", "edit", "a", "-c", "echo -r x"]),
                         ["edit", "a", "-c", "echo -r x"])
        self.assertEqual(f(["--remote", "vps", "restart", "-r"]), ["restart", "-r"])


class RemoteCommandsTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        patcher = mock.patch.dict(os.environ, {"SNT_HOME": str(Path(tmp.name) / "home")})
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_add_list_rm(self):
        code, out, err = self.run_cli("remote", "add", "vps", "me@example.com", "-p", "2222",
                                      "-k", "~/.ssh/k", "--no-check")
        self.assertEqual(code, 0, err)
        saved = rm.get_remote(Store(None), "vps")
        self.assertEqual((saved.target, saved.port, saved.key), ("me@example.com", 2222, "~/.ssh/k"))
        _, out, _ = self.run_cli("remote", "list")
        self.assertIn("me@example.com -p 2222", out)
        self.assertEqual(self.run_cli("remote", "rm", "vps")[0], 0)
        self.assertEqual(rm.load_remotes(Store(None)), {})

    def test_unknown_server(self):
        code, _, err = self.run_cli("-r", "nope", "status")
        self.assertEqual(code, 1)
        self.assertIn("no saved server", err)

    def test_forwarding_builds_ssh_command(self):
        self.run_cli("remote", "add", "vps", "me@example.com", "-k", "/k", "--no-check")
        with mock.patch("smolnugget.remote.shutil.which", return_value="ssh"), \
                mock.patch("smolnugget.remote.subprocess.call", return_value=0) as call:
            code, _, _ = self.run_cli("-r", "vps", "logs", "my app", "-n", "5")
        self.assertEqual(code, 0)
        cmd = call.call_args[0][0]
        self.assertEqual(cmd[0], "ssh")
        self.assertIn("BatchMode=yes", cmd)
        self.assertEqual(cmd[cmd.index("-i") + 1], str(Path("/k")))
        self.assertEqual(cmd[-2], "me@example.com")
        self.assertTrue(cmd[-1].endswith("exec snt logs 'my app' -n 5"), cmd[-1])

    def test_remote_flag_not_for_remote_command(self):
        code, _, err = self.run_cli("-r", "vps", "remote", "list")
        self.assertEqual(code, 1)
        self.assertIn("drop -r", err)


if __name__ == "__main__":
    unittest.main()

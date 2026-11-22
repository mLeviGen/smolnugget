import base64
import tempfile
import unittest
from pathlib import Path

from smolnugget.git import GitError, GitRepo
from tests.helpers import Remote, git


class GitRepoTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.remote = Remote(self.root)
        self.first = self.remote.commit({"a.txt": "1", ".gitignore": ".env\n"})
        self.repo = GitRepo(self.root / "app" / "repo", self.remote.url)

    def tearDown(self):
        self.tmp.cleanup()

    def test_clone_then_noop(self):
        self.assertTrue(self.repo.sync())
        self.assertEqual(self.repo.head(), self.first)
        self.assertFalse(self.repo.sync())
        self.assertFalse((self.root / "app" / "repo.partial").exists())

    def test_update(self):
        self.repo.sync()
        second = self.remote.commit({"a.txt": "2"})
        self.assertTrue(self.repo.sync())
        self.assertEqual(self.repo.head(), second)
        self.assertEqual((self.repo.path / "a.txt").read_text(), "2")

    def test_force_push(self):
        self.repo.sync()
        self.remote.commit({"a.txt": "2"})
        self.repo.sync()
        git(self.remote.work, "reset", "--hard", self.first)
        rewritten = self.remote.commit({"b.txt": "x"}, msg="rewritten")
        self.assertTrue(self.repo.sync())
        self.assertEqual(self.repo.head(), rewritten)

    def test_local_changes_discarded_ignored_kept(self):
        self.repo.sync()
        (self.repo.path / "a.txt").write_text("hacked")
        (self.repo.path / "junk.txt").write_text("x")
        (self.repo.path / ".env").write_text("SECRET=1")
        self.assertFalse(self.repo.sync())  # same commit, but tree restored
        self.assertEqual((self.repo.path / "a.txt").read_text(), "1")
        self.assertFalse((self.repo.path / "junk.txt").exists())
        self.assertTrue((self.repo.path / ".env").exists())

    def test_detached_head_recovers(self):
        self.repo.sync()
        git(self.repo.path, "checkout", "--detach")
        second = self.remote.commit({"a.txt": "2"})
        self.assertTrue(self.repo.sync())
        self.assertEqual(self.repo.head(), second)
        self.assertEqual(git(self.repo.path, "rev-parse", "--abbrev-ref", "HEAD"), "main")

    def test_explicit_branch(self):
        dev = self.remote.commit({"a.txt": "dev"}, branch="dev")
        repo = GitRepo(self.root / "dev" / "repo", self.remote.url, branch="dev")
        repo.sync()
        self.assertEqual(repo.head(), dev)

    def test_branch_switch(self):
        self.repo.sync()
        dev = self.remote.commit({"a.txt": "dev"}, branch="dev")
        self.repo.branch = "dev"
        self.assertTrue(self.repo.sync())
        self.assertEqual(self.repo.head(), dev)

    def test_url_change_updates_remote(self):
        self.repo.sync()
        other = Remote(self.root / "other")
        new = other.commit({"z.txt": "z"})
        self.repo.url = other.url
        self.assertTrue(self.repo.sync())
        self.assertEqual(self.repo.head(), new)

    def test_clone_failure(self):
        repo = GitRepo(self.root / "bad" / "repo", str(self.root / "missing.git"))
        with self.assertRaises(GitError):
            repo.sync()
        self.assertFalse(repo.exists())
        self.assertFalse((self.root / "bad" / "repo.partial").exists())

    def test_token_goes_to_header_only(self):
        repo = GitRepo(self.root / "r", "https://github.com/u/r.git", token="ghp_abc")
        env = repo._env()
        self.assertEqual(env["GIT_CONFIG_KEY_0"], "http.https://github.com/.extraheader")
        creds = env["GIT_CONFIG_VALUE_0"].split()[-1]
        self.assertEqual(base64.b64decode(creds).decode(), "x-access-token:ghp_abc")
        self.assertEqual(repo._redact("bad ghp_abc here"), "bad *** here")

    def test_no_token_for_ssh(self):
        repo = GitRepo(self.root / "r", "git@github.com:u/r.git", token="ghp_abc")
        self.assertNotIn("GIT_CONFIG_COUNT", repo._env())


if __name__ == "__main__":
    unittest.main()

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from smolnugget import doctor
from smolnugget.config import AppConfig, Store


def levels(results, needle):
    return [lvl for lvl, title, _ in results if needle in title]


class DoctorTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(Path(self.tmp.name) / "home")

    def add(self, **kw):
        app = AppConfig(name="bot", url="https://github.com/u/bot.git", command=["x"], **kw)
        self.store.save({"bot": app})

    def test_empty_home_is_fine(self):
        results = doctor.run_checks(self.store)
        self.assertEqual(levels(results, "config.json"), [doctor.OK])

    def test_home_that_is_a_file_fails(self):
        self.store.home.parent.mkdir(parents=True, exist_ok=True)
        self.store.home.write_text("not a folder")
        results = doctor.run_checks(self.store)
        self.assertEqual(results[-1][0], doctor.FAIL)
        self.assertIn("not a directory", results[-1][1])

    def test_unreadable_path_is_a_warning_not_a_crash(self):
        with mock.patch.object(doctor, "check_config", side_effect=PermissionError(13, "Permission denied")):
            results = doctor.run_checks(self.store)
        self.assertIn((doctor.WARN, "config.json: Permission denied", "check the permissions of that path"),
                      results)

    def test_invalid_config_fails(self):
        self.store.home.mkdir(parents=True)
        self.store.config_path.write_text("{not json", encoding="utf-8")
        results = doctor.run_checks(self.store)
        # on POSIX the file's permissions (644 here) add a WARN about config.json as well
        self.assertIn(doctor.FAIL, levels(results, "config.json"))
        self.assertNotIn(doctor.OK, levels(results, "config.json"))

    def test_wrong_shape_config_fails(self):
        self.store.home.mkdir(parents=True)
        self.store.config_path.write_text('{"foo": 1}', encoding="utf-8")
        found = levels(doctor.run_checks(self.store), "config.json")
        self.assertIn(doctor.FAIL, found)
        self.assertNotIn(doctor.OK, found)

    def test_missing_token_fails(self):
        self.add(token_env="DOCTOR_TEST_TOKEN")
        with mock.patch.dict(os.environ):
            os.environ.pop("DOCTOR_TEST_TOKEN", None)
            results = doctor.run_checks(self.store)
            self.assertEqual(levels(results, "token"), [doctor.FAIL])
            self.store.set_secret("DOCTOR_TEST_TOKEN", "abc")
            self.assertEqual(levels(doctor.run_checks(self.store), "token"), [doctor.OK])

    def test_apps_without_daemon_and_clone_warn(self):
        self.add()
        results = doctor.run_checks(self.store)
        self.assertEqual(levels(results, "snt is not running"), [doctor.WARN])
        self.assertEqual(levels(results, "not cloned"), [doctor.WARN])

    def test_last_error_warns_with_first_line(self):
        self.add()
        self.store.write_state("bot", {"last_error": "boom\nsecond line"})
        results = doctor.run_checks(self.store)
        self.assertEqual(levels(results, "last error: boom"), [doctor.WARN])
        self.assertFalse(any("second line" in t for _, t, _ in results))

    def test_old_git_warns(self):
        with mock.patch.object(doctor.shutil, "which", return_value="git"), \
                mock.patch.object(doctor, "git_version", return_value=(2, 20, 1)):
            level, _, hint = doctor.check_git()
        self.assertEqual(level, doctor.WARN)
        self.assertIn("2.31", hint)

    def test_missing_ssh_only_warns(self):
        with mock.patch.object(doctor.shutil, "which", return_value=None):
            self.assertEqual(doctor.check_ssh()[0], doctor.WARN)

    def test_low_disk_warns(self):
        usage = mock.Mock(free=100 * 1024 ** 2, total=10 * 1024 ** 3)
        with mock.patch.object(doctor.shutil, "disk_usage", return_value=usage):
            self.assertEqual(doctor.check_disk(self.store)[0], doctor.WARN)

    @unittest.skipIf(os.name == "nt", "POSIX modes")
    def test_open_permissions_warn(self):
        self.add()
        os.chmod(self.store.home, 0o755)
        self.assertIn(doctor.WARN, levels(doctor.run_checks(self.store), "mode"))


if __name__ == "__main__":
    unittest.main()

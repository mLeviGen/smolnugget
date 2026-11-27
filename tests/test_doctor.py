import contextlib
import datetime as dt
import io
import json
import os
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from smolnugget import cli, doctor
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


class ReportTest(unittest.TestCase):
    RESULTS = [
        (doctor.OK, "python 3.12.0", ""),
        (doctor.WARN, "disk 0.8 GB free (8%)", "explain\n$ du -h\n$ sudo apt-get clean"),
        (doctor.FAIL, "config.json is not valid", "fix it\n$ mv a b"),
        (doctor.WARN, "reboot required", "$ sudo reboot"),
    ]

    def run_cli(self, results, **kw):
        out = io.StringIO()
        with mock.patch.object(doctor, "run_checks", return_value=results), \
                contextlib.redirect_stdout(out):
            code = cli.cmd_doctor(types.SimpleNamespace(**kw), mock.Mock())
        return code, out.getvalue()

    def test_numbers_put_fails_first(self):
        self.assertEqual(doctor.problem_numbers(self.RESULTS), {2: 1, 1: 2, 3: 3})

    def test_text_output_references_and_fix_section(self):
        code, text = self.run_cli(self.RESULTS)
        self.assertEqual(code, 1)
        self.assertIn("WARN  disk 0.8 GB free (8%)  [2]", text)
        self.assertIn("FAIL  config.json is not valid  [1]", text)
        self.assertIn("OK    python 3.12.0\n", text)
        self.assertIn("1 failed, 2 warning(s)", text)
        fixes = text.split("How to fix:\n")[1]
        self.assertLess(fixes.index("1. config.json"), fixes.index("2. disk"))
        self.assertIn("  explain\n    du -h\n    sudo apt-get clean (needs sudo)\n", fixes)
        self.assertIn("3. reboot required\n    sudo reboot (needs sudo)", fixes)

    def test_nothing_to_fix(self):
        code, text = self.run_cli(self.RESULTS[:1])
        self.assertEqual(code, 0)
        self.assertIn("Nothing to fix.", text)
        self.assertNotIn("How to fix", text)

    def test_warnings_only_exit_zero(self):
        self.assertEqual(self.run_cli([self.RESULTS[1]])[0], 0)

    def test_json_shape(self):
        code, text = self.run_cli(self.RESULTS, json=True)
        data = json.loads(text)
        self.assertEqual(code, 1)
        self.assertEqual((data["failed"], data["warnings"]), (1, 2))
        self.assertEqual(data["checks"][0], {"level": "OK", "title": "python 3.12.0", "fix": []})
        self.assertEqual(data["checks"][1]["fix"], ["explain", "$ du -h", "$ sudo apt-get clean"])
        self.assertNotIn("\x1b", text)


class HostCheckTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.store = Store(self.dir / "home")

    def write(self, rel, text):
        path = self.dir / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    # os end of life
    def release(self, ident, version, pretty):
        return self.write("os-release", f'ID={ident}\nVERSION_ID="{version}"\nPRETTY_NAME="{pretty}"\n')

    def test_os_past_eol_warns(self):
        path = self.release("debian", "11", "Debian GNU/Linux 11 (bullseye)")
        level, title, text = doctor.check_os(path, dt.date(2026, 11, 27))
        self.assertEqual(level, doctor.WARN)
        self.assertEqual(title, "Debian GNU/Linux 11 (bullseye): no security updates since 2026-08-31")
        self.assertIn("snt add", text)

    def test_os_before_eol_and_unknown_are_ok(self):
        path = self.release("debian", "11", "Debian GNU/Linux 11 (bullseye)")
        self.assertEqual(doctor.check_os(path, dt.date(2026, 8, 31))[0], doctor.OK)
        path = self.release("arch", "rolling", "Arch Linux")
        self.assertEqual(doctor.check_os(path, dt.date(2030, 1, 1)), (doctor.OK, "os Arch Linux", ""))

    def test_os_missing_file_is_skipped(self):
        self.assertIsNone(doctor.check_os(self.dir / "nope"))

    # package updates
    def test_updates_counted_after_header(self):
        out = "Listing...\na/stable 2 amd64 [upgradable from: 1]\nb/stable 2 amd64 [upgradable from: 1]\n"
        with mock.patch.object(doctor.shutil, "which", return_value="/usr/bin/apt"), \
                mock.patch.object(doctor, "_run", return_value=out) as run:
            level, title, text = doctor.check_updates()
        self.assertEqual((level, title), (doctor.WARN, "2 package updates pending"))
        self.assertIn("$ sudo apt update && sudo apt upgrade", text)
        self.assertEqual(run.call_args.kwargs["env"]["LC_ALL"], "C")

    def test_updates_skipped_without_apt_or_on_failure(self):
        with mock.patch.object(doctor.shutil, "which", return_value=None):
            self.assertIsNone(doctor.check_updates())
        with mock.patch.object(doctor.shutil, "which", return_value="apt"), \
                mock.patch.object(doctor, "_run", return_value=None):
            self.assertIsNone(doctor.check_updates())

    def test_run_swallows_timeouts(self):
        err = doctor.subprocess.TimeoutExpired("apt", 20)
        with mock.patch.object(doctor.subprocess, "run", side_effect=err):
            self.assertIsNone(doctor._run(["apt"]))

    # reboot
    def test_reboot_required(self):
        self.assertIsNone(doctor.check_reboot(self.dir / "reboot-required"))
        flag = self.write("reboot-required", "*** System restart required ***")
        level, _, text = doctor.check_reboot(flag)
        self.assertEqual(level, doctor.WARN)
        self.assertIn("$ sudo reboot", text)

    # ssh
    def ssh(self, main, extra=None):
        root = self.dir / "ssh"
        self.write("ssh/sshd_config", main)
        for name, text in (extra or {}).items():
            self.write(f"ssh/{name}", text)
        return root

    def test_ssh_default_is_password_login(self):
        result = doctor.check_ssh_password(self.ssh("Port 22\n"))
        self.assertEqual(result[:2], (doctor.WARN, "SSH allows password login"))
        self.assertIn("systemctl reload ssh", result[2])

    def test_ssh_include_first_value_wins(self):
        root = self.ssh("Include sshd_config.d/*.conf\nPasswordAuthentication yes\n",
                        {"sshd_config.d/a.conf": "passwordauthentication = no\n",
                         "sshd_config.d/b.conf": "PasswordAuthentication yes\n"})
        self.assertEqual(doctor.check_ssh_password(root)[0], doctor.OK)

    def test_ssh_value_before_include_wins(self):
        root = self.ssh("PasswordAuthentication yes\nInclude sshd_config.d/*.conf\n",
                        {"sshd_config.d/a.conf": "PasswordAuthentication no\n"})
        self.assertEqual(doctor.check_ssh_password(root)[0], doctor.WARN)

    def test_ssh_absolute_include_comments_and_match(self):
        extra = self.write("other.conf", "# PasswordAuthentication yes\nPasswordAuthentication no\n")
        root = self.ssh(f"Include {extra}\nMatch User x\nPasswordAuthentication yes\n")
        self.assertEqual(doctor.check_ssh_password(root)[0], doctor.OK)
        root = self.ssh("Match User x\nPasswordAuthentication no\n")
        self.assertEqual(doctor.check_ssh_password(root)[0], doctor.WARN)

    def test_ssh_unreadable_is_skipped(self):
        self.assertIsNone(doctor.check_ssh_password(self.dir / "missing"))

    # service
    def apps(self):
        return {"bot": AppConfig(name="bot", url="https://x/y.git", command=["x"])}

    def units(self, user, system):
        paths = {False: self.dir / "user.service", True: self.dir / "sys.service"}
        for flag, present in ((False, user), (True, system)):
            if present:
                paths[flag].write_text("[Unit]")
        return mock.patch.object(doctor.service, "unit_path", side_effect=lambda s: paths[s])

    def test_service_not_installed_warns(self):
        with self.units(False, False):
            found = doctor.check_service(self.apps())
        self.assertEqual(len(found), 1)
        self.assertIn("not installed as a service", found[0][1])
        self.assertIn("$ snt service install", found[0][2])
        with self.units(False, False):
            self.assertEqual(doctor.check_service({}), [])

    def test_system_unit_is_enough(self):
        with self.units(False, True):
            self.assertEqual(doctor.check_service(self.apps()), [])

    def test_linger_off_warns_and_on_is_quiet(self):
        with self.units(True, False), mock.patch.object(doctor.shutil, "which", return_value="x"), \
                mock.patch.object(doctor, "_run", return_value="Linger=no\n"):
            found = doctor.check_service(self.apps())
        self.assertEqual(found[0][1], "linger is off: the service stops when you log out")
        self.assertIn("sudo loginctl enable-linger $USER", found[0][2])
        with self.units(True, False), mock.patch.object(doctor.shutil, "which", return_value="x"), \
                mock.patch.object(doctor, "_run", return_value="Linger=yes\n"):
            self.assertEqual(doctor.check_service(self.apps()), [])
        with self.units(True, False), mock.patch.object(doctor.shutil, "which", return_value="x"), \
                mock.patch.object(doctor, "_run", return_value=None):
            self.assertEqual(doctor.check_service(self.apps()), [])

    # git
    def git(self, version, apps):
        with mock.patch.object(doctor.shutil, "which", return_value="git"), \
                mock.patch.object(doctor, "git_version", return_value=version):
            return doctor.check_git(apps)

    def test_old_git_warns_only_with_https_tokens(self):
        plain = self.apps()
        token = {"bot": AppConfig(name="bot", url="https://x/y.git", command=["x"], token_env="T")}
        ssh_url = {"bot": AppConfig(name="bot", url="git@x:y.git", command=["x"], token_env="T")}
        self.assertEqual(self.git((2, 30, 2), token)[0], doctor.WARN)
        self.assertEqual(self.git((2, 30, 2), plain),
                         (doctor.OK, "git 2.30.2 (fine: no HTTPS tokens in use)", ""))
        self.assertEqual(self.git((2, 30, 2), ssh_url)[0], doctor.OK)
        self.assertEqual(self.git((2, 40, 0), token), (doctor.OK, "git 2.40.0", ""))

    # leftover folders and disk
    def test_leftover_folders_with_size(self):
        self.store.save({})
        for name, size in (("old", 2048), ("older", 1024)):
            self.write(f"home/apps/{name}/repo/f.bin", "x" * size)
        self.write("home/apps/bot/state.json", "{}")
        apps = self.apps()
        level, title, text = doctor.check_leftovers(self.store, apps)
        self.assertEqual(level, doctor.WARN)
        self.assertEqual(title, "leftover app folders: old, older (3 KB)")
        self.assertEqual(text.count("$ "), 2)
        self.assertIn("old", text)
        self.assertIsNone(doctor.check_leftovers(self.store, {**apps, "old": apps["bot"],
                                                              "older": apps["bot"]}))
        self.assertIsNone(doctor.check_leftovers(self.store, None))

    def test_leftover_in_run_checks_not_when_config_is_broken(self):
        self.write("home/apps/ghost/f", "x")
        self.store.save({})
        self.assertTrue(any("leftover" in t for _, t, _ in doctor.run_checks(self.store)))
        self.store.config_path.write_text("{bad", encoding="utf-8")
        self.assertFalse(any("leftover" in t for _, t, _ in doctor.run_checks(self.store)))

    def test_dir_size_is_capped(self):
        for i in range(5):
            self.write(f"d/{i}", "xx")
        self.assertEqual(doctor.dir_size(self.dir / "d"), 10)
        self.assertEqual(doctor.dir_size(self.dir / "d", limit=2), 4)
        self.assertEqual(doctor.dir_size(self.dir / "absent"), 0)

    def test_disk_fix_is_actionable(self):
        self.store.save({})
        self.write("home/apps/ghost/f", "x")
        pip = self.dir / "pipcache"
        pip.mkdir()
        usage = mock.Mock(free=100 * 1024 ** 2, total=10 * 1024 ** 3)
        with mock.patch.object(doctor.shutil, "disk_usage", return_value=usage), \
                mock.patch.object(doctor, "pip_cache_dir", return_value=pip):
            level, _, text = doctor.check_disk(self.store, {})
        self.assertEqual(level, doctor.WARN)
        self.assertIn("snt cannot free space by itself", text)
        self.assertIn("python3 -m pip cache purge", text)
        self.assertIn("ghost", text)
        self.assertIn("du -xh" if os.name != "nt" else "Get-ChildItem", text)
        with mock.patch.object(doctor.shutil, "disk_usage", return_value=usage), \
                mock.patch.object(doctor, "pip_cache_dir", return_value=self.dir / "nope"):
            text = doctor.check_disk(self.store, {})[2]
        self.assertNotIn("pip cache purge", text)

    def test_linux_cleanups_only_where_they_apply(self):
        have = {"journalctl": "/bin/journalctl", "apt": "/bin/apt"}
        with mock.patch.object(doctor, "_linux", return_value=True), \
                mock.patch.object(doctor.shutil, "which", side_effect=have.get):
            text = "\n".join(doctor._cleanup_lines(self.store, None))
        self.assertIn("sudo journalctl --vacuum-size=100M", text)
        self.assertIn("sudo apt-get clean", text)
        with mock.patch.object(doctor, "_linux", return_value=True), \
                mock.patch.object(doctor.shutil, "which", return_value=None):
            text = "\n".join(doctor._cleanup_lines(self.store, None))
        self.assertNotIn("journalctl", text)
        self.assertNotIn("apt-get", text)


if __name__ == "__main__":
    unittest.main()

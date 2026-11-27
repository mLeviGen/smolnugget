"""Saved servers and running snt on them over ssh (`snt -r NAME ...`, `snt remote ...`).

Uses the system `ssh` client with key authentication; no passwords are stored.
`snt -r NAME web` opens the server's dashboard through an ssh tunnel.
"""

from __future__ import annotations

import collections
import json
import random
import secrets
import shlex
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from dataclasses import asdict, dataclass
from pathlib import Path

from smolnugget.config import ConfigError, Store, validate_name, write_atomic

REMOTE_SNT = 'PATH="$HOME/.local/bin:$PATH"; exec snt'
KEEPALIVE = 15  # seconds between pings through a tunnel
IDLE_EXIT = 90  # snt web on the server exits after this long without any request


class RemoteError(Exception):
    pass


@dataclass
class Remote:
    name: str
    target: str  # user@host
    port: int | None = None  # ssh port
    key: str | None = None  # path to the private key

    def ssh_base(self, batch: bool = True) -> list[str]:
        if not shutil.which("ssh"):
            raise RemoteError("the `ssh` command was not found; install OpenSSH")
        cmd = ["ssh", "-o", "ConnectTimeout=15", "-o", "ServerAliveInterval=30"]
        if batch:  # never hang on a password prompt
            cmd += ["-o", "BatchMode=yes"]
        if self.port:
            cmd += ["-p", str(self.port)]
        if self.key:
            cmd += ["-i", str(Path(self.key).expanduser()), "-o", "IdentitiesOnly=yes"]
        return cmd


def _path(store: Store) -> Path:
    return store.home / "remotes.json"


def load_remotes(store: Store) -> dict[str, Remote]:
    path = _path(store)
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return {n: Remote(name=n, **d) for n, d in raw.items()}
    except (OSError, ValueError, TypeError) as e:
        raise ConfigError(f"cannot read {path}: {e}") from e


def save_remotes(store: Store, remotes: dict[str, Remote]) -> None:
    data = {n: {k: v for k, v in asdict(r).items() if k != "name"} for n, r in sorted(remotes.items())}
    write_atomic(_path(store), json.dumps(data, indent=2) + "\n")


def get_remote(store: Store, name: str) -> Remote:
    remotes = load_remotes(store)
    if name not in remotes:
        known = ", ".join(remotes) or "none yet"
        raise RemoteError(f"no saved server {name!r} (known: {known}); add it with 'snt remote add'")
    return remotes[name]


def explain(code: int) -> str:
    if code == 255:
        return ("ssh could not connect or log in. Check the address, that your key is on the server "
                "(snt remote add ... --setup-key), and connect once with plain `ssh` to trust "
                "the host key.")
    if code == 127:
        return "snt is not installed on the server (install: curl -fsSL <install.sh url> | sh)"
    return f"exited with code {code}"


def run_remote(remote: Remote, snt_args: list[str]) -> int:
    """Run `snt ARGS` on the server with the terminal attached; returns the exit code."""
    tty = ["-t"] if sys.stdin.isatty() and sys.stdout.isatty() else []
    cmd = remote.ssh_base() + tty + [remote.target, f"{REMOTE_SNT} {shlex.join(snt_args)}"]
    try:
        code = subprocess.call(cmd)
    except KeyboardInterrupt:
        return 130
    if code in (255, 127):
        print(f"error: {explain(code)}", file=sys.stderr)
    return code


def _free_port() -> int:
    for _ in range(50):
        port = random.randint(20000, 60000)
        with socket.socket() as s:
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    raise RemoteError("no free local port found")


class Tunnel:
    """`snt web` on a server, reachable here through an ssh port forward.

    * The same port number is used on both ends: snt web only answers requests whose
      Host header names its own port.
    * The access token goes to the server over stdin, never on its command line (which
      other users there could read with `ps`); closing stdin makes the server side exit.
    * Before probing the forward we wait for the server's own "web UI:" line - probing
      earlier makes ssh print "channel N: open failed".
    """

    def __init__(self, remote: Remote):
        self.remote = remote
        self.token = secrets.token_urlsafe(24)
        self.port: int | None = None
        self.proc: subprocess.Popen | None = None
        self._err: collections.deque[str] = collections.deque(maxlen=20)

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def start(self, timeout: float = 30) -> None:
        last = "could not start the dashboard on the server"
        for _ in range(3):
            self.close()
            self.port = _free_port()
            cmd = self.remote.ssh_base() + [
                "-o", "ExitOnForwardFailure=yes", "-L", f"{self.port}:127.0.0.1:{self.port}",
                self.remote.target,
                f"{REMOTE_SNT} web --port {self.port} --token-stdin --exit-on-eof "
                f"--idle-exit {IDLE_EXIT}",
            ]
            self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                         stderr=subprocess.PIPE)
            self._err.clear()
            # always drain stderr: a full pipe would block the server side
            threading.Thread(target=self._drain_stderr, args=(self.proc,), daemon=True).start()
            try:
                self.proc.stdin.write(self.token.encode() + b"\n")
                self.proc.stdin.flush()
            except OSError:
                pass
            if self._wait_ready(timeout):
                threading.Thread(target=self._keepalive, args=(self.proc,), daemon=True).start()
                return
            code = self.proc.poll()
            err = self._stderr_tail()
            if code is None:
                self.close()
                raise RemoteError("the dashboard did not start on the server within "
                                  f"{timeout:.0f} s")
            if code in (255, 127):
                raise RemoteError(explain(code))
            if "unrecognized arguments" in err or "invalid choice" in err:
                raise RemoteError("snt on the server is too old for this; update it "
                                  "(re-run install.sh there)")
            last = err.splitlines()[-1] if err else last  # e.g. the port is taken: retry
        raise RemoteError(last)

    def _drain_stderr(self, proc: subprocess.Popen) -> None:
        try:
            for line in proc.stderr:
                self._err.append(line.decode(errors="replace").rstrip())
        except (OSError, ValueError):
            pass

    def _stderr_tail(self) -> str:
        time.sleep(0.2)  # let the drain thread catch the last lines
        return "\n".join(self._err).strip()

    def _wait_ready(self, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        started = threading.Event()
        proc = self.proc

        def read_stdout():
            for line in proc.stdout:
                if b"web UI:" in line:
                    started.set()
            started.set()  # EOF: the server side exited; poll() notices

        threading.Thread(target=read_stdout, daemon=True).start()
        if not started.wait(timeout) or proc.poll() is not None:
            return False
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                return False
            code, _ = self.request("GET", "/api/status", timeout=3)
            if code == 200:
                return True
            if code == 401:  # token mismatch: the server side is not ours
                return False
            time.sleep(0.4)
        return False

    def request(self, method: str, path: str, timeout: float = 10) -> tuple[int, bytes]:
        """HTTP to the server's snt web; (status, body), status 0 when unreachable."""
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}", method=method,
            headers={"Authorization": f"Bearer {self.token}", "X-Snt": "1"},
            data=b"" if method == "POST" else None)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()
        except OSError:
            return 0, b""

    def _keepalive(self, proc: subprocess.Popen) -> None:
        # the server side exits after IDLE_EXIT s without requests, so a tunnel whose
        # client vanished silently can't leave snt web running there forever
        while proc is self.proc and proc.poll() is None:
            time.sleep(KEEPALIVE)
            if proc is self.proc and proc.poll() is None:
                self.request("GET", "/api/ping", timeout=10)

    def diagnose(self) -> str:
        """Why the tunnel is down: exit code and the last lines ssh / snt web printed."""
        code = self.proc.poll() if self.proc else None
        tail = self._stderr_tail().splitlines()[-3:]
        return f"exit code {code}" + (": " + " | ".join(tail) if tail else "")

    def close(self) -> None:
        proc, self.proc = self.proc, None
        if proc is None or proc.poll() is not None:
            return
        try:
            proc.stdin.close()  # EOF tells snt web on the server to exit
        except OSError:
            pass
        try:
            proc.wait(5)
        except subprocess.TimeoutExpired:
            proc.terminate()
            proc.wait()


def open_dashboard(remote: Remote, open_browser: bool = True) -> int:
    """Tunnel to the server, start `snt web` there, open it locally; Ctrl+C closes both."""
    tunnel = Tunnel(remote)
    tunnel.start()
    url = f"http://localhost:{tunnel.port}/?t={tunnel.token}"
    print(f"{remote.name}: dashboard at {url}  (Ctrl+C to close)", flush=True)
    if open_browser:
        webbrowser.open(url)
    try:
        tunnel.proc.wait()
    except KeyboardInterrupt:
        pass
    finally:
        tunnel.close()
    return 0


def setup_key(remote: Remote) -> str:
    """Create ~/.ssh/snt_<name> if needed and append its public key on the server.

    ssh asks for the server password once, in this terminal.
    """
    key = Path(remote.key).expanduser() if remote.key else Path.home() / ".ssh" / f"snt_{remote.name}"
    pub = key.with_name(key.name + ".pub")
    if not key.exists():
        key.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C",
                        f"smolnugget {remote.name}", "-f", str(key)], check=True)
        print(f"created key {key}")
    print(f"installing {pub.name} on {remote.target} (enter the server password once)")
    install = "umask 077; mkdir -p ~/.ssh && cat >> ~/.ssh/authorized_keys"
    plain = Remote(remote.name, remote.target, remote.port, None)
    code = subprocess.run(plain.ssh_base(batch=False) + [remote.target, install],
                          input=pub.read_bytes()).returncode
    if code != 0:
        raise RemoteError(f"could not install the key: {explain(code)}")
    return str(key)


GLOBAL_WITH_VALUE = ("-r", "--remote", "--home", "--log-file")


def strip_remote_args(argv: list[str]) -> list[str]:
    """argv without the global -r/--remote option, for forwarding to the server.

    Only the global part (before the command) is touched, so a command's own
    arguments are forwarded exactly as typed.
    """
    out, i = [], 0
    while i < len(argv):
        a = argv[i]
        if not a.startswith("-"):  # the command: everything from here on is forwarded
            return out + argv[i:]
        if a == "--no-color":  # local presentation only; older servers would reject it
            i += 1
            continue
        if a in ("-r", "--remote"):
            i += 2
            continue
        if a.startswith("--remote=") or (a.startswith("-r") and len(a) > 2 and not a.startswith("--")):
            i += 1
            continue
        out.append(a)
        if a in GLOBAL_WITH_VALUE and i + 1 < len(argv):
            out.append(argv[i + 1])
            i += 1
        i += 1
    return out


def check_name(name: str) -> str:
    if name == "local":
        raise RemoteError("'local' is reserved for this computer; pick another name")
    return validate_name(name)

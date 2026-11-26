"""Saved servers and running snt on them over ssh (`snt -r NAME ...`, `snt remote ...`).

Uses the system `ssh` client with key authentication; no passwords are stored.
`snt -r NAME web` opens the server's dashboard through an ssh tunnel.
"""

from __future__ import annotations

import json
import random
import shlex
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
import webbrowser
from dataclasses import asdict, dataclass
from pathlib import Path

from smolnugget.config import ConfigError, Store, validate_name, write_atomic

REMOTE_SNT = 'PATH="$HOME/.local/bin:$PATH"; exec snt'


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


def _wait_ready(port: int, proc: subprocess.Popen, timeout: float = 30) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            return False
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/status", timeout=2) as r:
                if r.status == 200:
                    return True
        except OSError:
            pass
        time.sleep(0.4)
    return False


def open_dashboard(remote: Remote, open_browser: bool = True) -> int:
    """Tunnel to the server, start `snt web` there, open it locally; Ctrl+C closes both.

    The same port number is used on both ends because snt web only answers requests whose
    Host header names its own port.
    """
    for attempt in range(3):
        port = _free_port()
        cmd = remote.ssh_base() + [
            "-o", "ExitOnForwardFailure=yes", "-L", f"{port}:127.0.0.1:{port}", remote.target,
            f"{REMOTE_SNT} web --port {port} --exit-on-eof",
        ]
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL)
        if _wait_ready(port, proc):
            break
        code = proc.poll()
        if code is None:
            proc.kill()
            proc.wait()
            raise RemoteError("the dashboard did not start on the server within 30 s")
        if code in (255, 127):
            raise RemoteError(explain(code))
        # port taken on the server, or an old snt without --exit-on-eof: try again
    else:
        raise RemoteError("could not start the dashboard on the server "
                          "(is snt there up to date? reinstall it with install.sh)")

    url = f"http://localhost:{port}/"
    print(f"{remote.name}: dashboard at {url}  (Ctrl+C to close)")
    if open_browser:
        webbrowser.open(url)
    try:
        proc.wait()
    except KeyboardInterrupt:
        pass
    finally:
        if proc.poll() is None:
            try:
                proc.stdin.close()  # EOF tells snt web on the server to exit
            except OSError:
                pass
            try:
                proc.wait(5)
            except subprocess.TimeoutExpired:
                proc.terminate()
                proc.wait()
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
    return validate_name(name)

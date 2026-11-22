"""Application definitions, secrets and their on-disk storage."""

from __future__ import annotations

import json
import os
import re
import sys
import time
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Self
from urllib.parse import urlsplit

CONFIG_VERSION = 2
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
RESTART_POLICIES = ("always", "on-failure", "no")
PYTHON_NAMES = ("python", "python3")


class ConfigError(Exception):
    pass


def default_home() -> Path:
    if env := os.environ.get("SNT_HOME"):
        return Path(env).expanduser()
    return Path.home() / ".smolnugget"


def validate_name(name: str) -> str:
    if not NAME_RE.match(name):
        raise ConfigError(
            f"invalid app name {name!r}: use letters, digits, '.', '_' or '-' (max 64 chars)"
        )
    return name


def validate_url(url: str) -> str:
    if not url:
        raise ConfigError("repository URL is empty")
    parts = urlsplit(url)
    if parts.scheme in ("http", "https") and (parts.username or parts.password):
        raise ConfigError("do not put credentials into the URL; use --token or --token-env")
    return url


def name_from_url(url: str) -> str:
    """https://github.com/user/my-bot.git -> my-bot"""
    tail = re.split(r"[/\\:]", url.rstrip("/\\"))[-1]
    tail = tail.removesuffix(".git")
    name = re.sub(r"[^A-Za-z0-9._-]", "-", tail).strip("-._")[:64]
    return name or "app"


def token_var_for(name: str) -> str:
    return re.sub(r"[^A-Z0-9]", "_", name.upper()) + "_TOKEN"


@dataclass
class AppConfig:
    name: str
    url: str
    command: list[str]
    branch: str | None = None
    enabled: bool = True
    venv: bool = False
    setup: list[str] = field(default_factory=list)
    check_interval: int = 60
    restart: str = "always"
    stop_timeout: int = 10
    env: dict[str, str] = field(default_factory=dict)
    token_env: str | None = None

    def validate(self) -> Self:
        validate_name(self.name)
        validate_url(self.url)
        if not self.command:
            raise ConfigError(f"{self.name}: command is empty")
        if self.restart not in RESTART_POLICIES:
            raise ConfigError(f"{self.name}: restart must be one of {', '.join(RESTART_POLICIES)}")
        if self.check_interval < 5:
            raise ConfigError(f"{self.name}: check_interval must be at least 5 seconds")
        if self.stop_timeout < 1:
            raise ConfigError(f"{self.name}: stop_timeout must be at least 1 second")
        if self.token_env and not ENV_KEY_RE.match(self.token_env):
            raise ConfigError(f"{self.name}: invalid token variable name {self.token_env!r}")
        return self

    def to_dict(self) -> dict:
        data = asdict(self)
        del data["name"]
        return data

    @classmethod
    def from_dict(cls, name: str, data: dict) -> Self:
        known = {f.name for f in fields(cls)} - {"name"}
        if unknown := set(data) - known:
            raise ConfigError(f"{name}: unknown config keys: {', '.join(sorted(unknown))}")
        return cls(name=name, **data).validate()


class Store:
    """Owns the smolnugget home directory.

    home/
      config.json           app definitions
      secrets.env           KEY=VALUE tokens (mode 600), also usable as systemd EnvironmentFile
      daemon.lock           held by `snt run` while it follows the whole config
      apps/<name>/repo      checkout
      apps/<name>/venv      virtualenv
      apps/<name>/state.json, output.log, control, lock
    """

    def __init__(self, home: Path | None = None):
        self.home = Path(home) if home else default_home()
        self.config_path = self.home / "config.json"
        self.secrets_path = self.home / "secrets.env"
        self.daemon_lock_path = self.home / "daemon.lock"

    # --- paths -----------------------------------------------------------
    def app_dir(self, name: str) -> Path:
        return self.home / "apps" / validate_name(name)

    def repo_dir(self, name: str) -> Path:
        return self.app_dir(name) / "repo"

    def venv_dir(self, name: str) -> Path:
        return self.app_dir(name) / "venv"

    def state_path(self, name: str) -> Path:
        return self.app_dir(name) / "state.json"

    def lock_path(self, name: str) -> Path:
        return self.app_dir(name) / "lock"

    def log_path(self, name: str) -> Path:
        return self.app_dir(name) / "output.log"

    def control_path(self, name: str) -> Path:
        return self.app_dir(name) / "control"

    # --- config ----------------------------------------------------------
    def config_signature(self) -> tuple:
        """Changes whenever config.json is rewritten (saves replace the file, so the inode
        changes too, even on filesystems with coarse timestamps)."""
        try:
            st = self.config_path.stat()
        except OSError:
            return ()
        return (st.st_mtime_ns, st.st_size, st.st_ino)

    def load(self) -> dict[str, AppConfig]:
        if not self.config_path.exists():
            return {}
        try:
            raw = json.loads(self.config_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            raise ConfigError(f"cannot read {self.config_path}: {e}") from e
        if not isinstance(raw, dict) or "apps" not in raw:
            raise ConfigError(
                f"{self.config_path} is not a smolnugget config "
                "(configs of the old single-file snt.py must be re-created with 'snt add')"
            )
        apps = {}
        for name, data in raw["apps"].items():
            if "token" in data:  # config v1 kept tokens inline; move them to secrets.env
                data = dict(data)
                token = data.pop("token")
                if token and not data.get("token_env"):
                    data["token_env"] = token_var_for(name)
                    self.set_secret(data["token_env"], token)
            apps[name] = AppConfig.from_dict(name, data)
        return apps

    def save(self, apps: dict[str, AppConfig]) -> None:
        data = {
            "version": CONFIG_VERSION,
            "apps": {name: app.to_dict() for name, app in sorted(apps.items())},
        }
        write_atomic(self.config_path, json.dumps(data, indent=2) + "\n")

    def get(self, name: str) -> AppConfig:
        apps = self.load()
        if name not in apps:
            raise ConfigError(f"no app named {name!r} (see 'snt status')")
        return apps[name]

    # --- secrets ---------------------------------------------------------
    def secrets(self) -> dict[str, str]:
        try:
            text = self.secrets_path.read_text(encoding="utf-8")
        except OSError:
            return {}
        result = {}
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            result[key.strip()] = value.strip().strip("'\"")
        return result

    def set_secret(self, key: str, value: str | None) -> None:
        if not ENV_KEY_RE.match(key):
            raise ConfigError(f"invalid secret name {key!r}")
        secrets = self.secrets()
        if value is None:
            secrets.pop(key, None)
        else:
            secrets[key] = value
        text = "# smolnugget secrets (KEY=VALUE). Keep this file private.\n"
        text += "".join(f"{k}={v}\n" for k, v in sorted(secrets.items()))
        write_atomic(self.secrets_path, text, private=True)

    def resolve_token(self, app: AppConfig) -> str | None:
        """Token from the process environment, falling back to secrets.env."""
        if not app.token_env:
            return None
        return os.environ.get(app.token_env) or self.secrets().get(app.token_env) or None

    # --- runtime state ---------------------------------------------------
    def read_state(self, name: str) -> dict:
        try:
            return json.loads(self.state_path(name).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def write_state(self, name: str, state: dict) -> None:
        write_atomic(self.state_path(name), json.dumps(state, indent=2) + "\n")


def write_atomic(path: Path, text: str, private: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600 if private else 0o644)
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    for attempt in range(20):
        try:
            os.replace(tmp, path)
            break
        except PermissionError:  # Windows: the target is briefly open by a reader
            if attempt == 19:
                raise
            time.sleep(0.05)
    if private and sys.platform != "win32":
        os.chmod(path, 0o600)

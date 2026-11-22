# smolnugget

**Push to GitHub — your server updates and restarts the app by itself.**

smolnugget (`snt`) is a tiny, dependency-free deploy tool and process supervisor for
small projects: Discord/Telegram bots, web services, scripts. Point it at a repository and it
keeps a copy on your server in sync with GitHub, installs dependencies and keeps the process
running.

```console
$ snt add https://github.com/you/ihor_bot
added 'ihor_bot'
  repo     https://github.com/you/ihor_bot
  command  python bot.py   (detected from bot.py)
  venv     yes (requirements.txt is installed on every update)

$ snt status
NAME      STATUS   PID    UPTIME  RESTARTS  COMMIT    REPO
ihor_bot  running  41822  3h12m   0         56983e02  you/ihor_bot
```

- **Hands-off updates.** It checks GitHub every minute. On a new commit it pulls, installs
  dependencies and restarts the app. Force-pushes and branch switches are handled too.
- **Keeps things alive.** Crashed processes come back with exponential backoff (1 s up to 60 s).
  If setup fails after an update, the old version keeps running.
- **Several apps, one daemon.** Each app has its own folder, virtualenv and log.
  `snt add`, `edit`, `stop` and `start` apply live, no restart needed.
- **Convenient to watch.** `snt top` is a live monitor with logs and hotkeys, `snt logs -f`
  follows one app's output, and `snt status --json` feeds dashboards.
- **Safe with secrets.** The GitHub token never ends up in `.git/config`, in the URL or in logs.
- **Nothing to install.** Pure Python standard library plus `git`.
  Runs on Linux, macOS and Windows.

> **Not Docker.** There is no isolation: apps run as normal processes of the user that runs `snt`.
> smolnugget is closer to "pm2 + a git-pull cron job", done properly.

## Contents

- [Install](#install) · [Quick start](#quick-start) · [Everyday commands](#everyday-commands)
- [How it works](#how-it-works) · [App settings](#app-settings) · [Private repositories](#private-repositories)
- [Running on boot](#running-on-boot) · [Status for dashboards](#status-for-dashboards)
- [Files](#files) · [Troubleshooting](#troubleshooting) · [Upgrading from 1.x](#upgrading-from-1x) · [Development](#development)

## Install

You need **Python 3.11+** and **git**.

**Linux / macOS** (installs into `~/.local/share/smolnugget`, puts `snt` in `~/.local/bin`):

```sh
curl -fsSL https://raw.githubusercontent.com/mLeviGen/smolnugget/main/install.sh | sh
```

**Windows** (PowerShell; installs into `%LOCALAPPDATA%\smolnugget` and adds `snt` to your PATH):

```powershell
irm https://raw.githubusercontent.com/mLeviGen/smolnugget/main/install.ps1 | iex
```

Run the same command again to upgrade. To uninstall, run `sh install.sh --uninstall` on Linux/macOS
or `install.ps1 -Uninstall` on Windows. Your apps and data stay in `~/.smolnugget`.

<details>
<summary>Other ways: pipx, pip, from a checkout</summary>

```sh
pipx install "smolnugget[top] @ git+https://github.com/mLeviGen/smolnugget"
pip install "smolnugget[top] @ git+https://github.com/mLeviGen/smolnugget"
git clone https://github.com/mLeviGen/smolnugget && python3 smolnugget/snt.py --help
```

The `[top]` extra only adds `psutil` for the CPU/memory columns in `snt top`.
</details>

## Quick start

```sh
snt add https://github.com/you/your-bot   # clones it and figures out how to start it
snt run                                   # runs everything; Ctrl+C stops
```

Is your bot private? `snt add` asks for a token (see [Private repositories](#private-repositories)).

To keep it running after you log out and after reboots (Linux), install the service once.
From then on just `snt add` and `snt rm` apps:

```sh
snt service install
```

### How `snt add` guesses the start command

| found in the repo | command | extras |
|---|---|---|
| `Procfile` (`worker:` preferred, then `web:`) | that line | venv if it starts with `python` |
| `package.json` with a `start` script | `npm start` | setup `npm ci` (or `npm install`) |
| `main.py`, `bot.py`, `app.py`, `run.py`, `start.py`, `server.py`, `__main__.py` | `python <file>` | venv + `requirements.txt` |
| exactly one `*.py` file | `python <file>` | venv + `requirements.txt` |

If the guess is wrong, fix it with `snt edit NAME -c "python -m mybot --prod"`.
If nothing matches, pass `-c` to `snt add`.

## Everyday commands

| | |
|---|---|
| `snt status` | all apps at a glance (`ls` works too) |
| `snt top` | live monitor: ↑/↓ select, `r` restart, `u` update now, `s` start/stop, `q` quit |
| `snt logs NAME -f` | follow an app's output (`-n 200` for more history) |
| `snt restart NAME` | restart now |
| `snt update NAME` | check GitHub right now instead of waiting |
| `snt stop NAME` / `snt start NAME` | disable / enable; stays that way across reboots |
| `snt edit NAME ...` | change settings; the app restarts with them |
| `snt rm NAME [--purge]` | remove; `--purge` also deletes its checkout, venv and logs |
| `snt show NAME` | print the config and where the token comes from |
| `snt sync NAME` | clone/update and run setup once, without starting (handy for a first check) |
| `snt run [NAME ...]` | run in the foreground (all apps, or only the named ones) |

Global options: `--home DIR` (data directory, also `$SNT_HOME`), `-v` (debug output),
`--log-file FILE` (also write snt's own log there).

## How it works

```
               every check_interval (default 60 s), or `snt update`
GitHub  ──fetch──►  apps/NAME/repo  ──setup──►  restart process
                    (reset to the        venv + pip install -r requirements.txt
                     remote branch)      + your --setup commands
```

- **Updating** resets the checkout to the remote branch exactly. Local edits to tracked files and
  untracked files are discarded. Files in `.gitignore` (`.env`, `data/`, `*.db`) are kept, so
  that is where runtime data belongs.
- **Setup** runs once per new commit, before the restart, while the old version is still running.
  If it fails, the old process keeps running and setup is retried on the next check.
- **Starting.** The process runs inside its checkout folder, with `--env` variables,
  `PYTHONUNBUFFERED=1` and, for venv apps, the venv first on `PATH`. A leading `python`/`python3`
  in the command means the app's venv interpreter.
- **Stopping** sends SIGTERM (CTRL_BREAK on Windows) to the app's whole process group. After
  `--stop-timeout` seconds it kills whatever is left.
- **Output** of the process and snt's own events go to `apps/NAME/output.log`
  (1 MB × 4 files, rotated). snt's events are marked with `*`.
- **Control** works through files. CLI commands change `config.json` or drop a request file,
  and the running daemon picks it up within a second. There is no port and no socket.

## App settings

All of these work with both `snt add` and `snt edit`:

| option | default | |
|---|---|---|
| `-c, --command "..."` | detected | how to start the app, run inside its checkout |
| `-b, --branch NAME` | repo default | branch to follow |
| `--venv / --no-venv` | on for python | own virtualenv, `requirements.txt` installed after every update |
| `--setup "CMD"` | — | shell command run after every update, e.g. `npm ci`, `make build` (repeatable) |
| `-e, --env KEY=VALUE` | — | environment variable for the app (repeatable) |
| `--check-interval SEC` | 60 | how often to look for new commits |
| `--restart always\|on-failure\|no` | always | what to do when the process exits by itself |
| `--stop-timeout SEC` | 10 | how long to wait for a clean shutdown before killing |
| `--token TOKEN` / `--token-env VAR` | — | access to private repositories (see below) |

`snt edit` additionally takes `--url`, `--clear-setup`, `--unset-env KEY` and `--clear-token`.

## Private repositories

1. Create a **fine-grained token** at <https://github.com/settings/personal-access-tokens/new>.
   Under *Repository access* pick only the repos snt should see, and under *Permissions* set
   **Contents: Read-only**. Nothing else is needed.
2. Run `snt add https://github.com/you/private-bot` and paste the token when asked. Or pass
   `--token ghp_...`, but then it lands in your shell history.

The token is saved to `~/.smolnugget/secrets.env` (file mode 600) as `PRIVATE_BOT_TOKEN=...`.
If you prefer to manage secrets yourself, use `--token-env MY_VAR`: snt reads `MY_VAR` from
the environment first, then from `secrets.env`. The token is sent to git only as an HTTP header
for that host. It is never written into `.git/config` or shown in logs.

SSH works too: use a `git@github.com:you/repo.git` URL with a
[deploy key](https://docs.github.com/en/authentication/connecting-to-github-with-ssh/managing-deploy-keys).

## Running on boot

**Linux (systemd).** `snt service install` creates a *user* service. It needs no root and runs
as you, with the same `~/.smolnugget`.

```sh
snt service install              # write the unit, enable and start it
sudo loginctl enable-linger $USER   # once: start on boot, keep running after logout
journalctl --user -u smolnugget -f  # the daemon's own log
snt service uninstall
```

Prefer a system-wide unit? Run `sudo snt service install --system`. It runs as the user who
called sudo. `snt service show` prints the unit without installing it.

**Windows / macOS.** There is no built-in service yet. Keep `snt run` running in a terminal,
or start it at logon with Task Scheduler (Windows) or launchd (macOS).

## Status for dashboards

`snt status --json` prints a stable, machine-readable snapshot. Tokens and `--env` values are
never included. Times are ISO 8601 UTC.

```json
{
  "schema": 1,
  "smolnugget": "2.0.0",
  "generated_at": "2026-11-22T20:15:00+00:00",
  "daemon": { "running": true, "pid": 4100, "started_at": "2026-11-22T08:00:02+00:00" },
  "apps": [
    {
      "name": "ihor_bot",
      "status": "running",
      "enabled": true,
      "pid": 41822,
      "commit": "56983e0273c1...",
      "checkout": "56983e0273c1...",
      "started_at": "2026-11-22T17:03:10+00:00",
      "uptime_seconds": 11510,
      "restarts": 0,
      "last_check": "2026-11-22T20:14:35+00:00",
      "last_error": null,
      "repo": { "url": "https://github.com/you/ihor_bot", "branch": null },
      "command": ["python", "bot.py"],
      "restart_policy": "always",
      "check_interval": 60
    }
  ]
}
```

| `status` | meaning |
|---|---|
| `running` | the process is up |
| `waiting` | snt manages the app but the process is not up: first clone/setup, waiting to restart after a crash, or exited under restart policy `no` |
| `stopped` | enabled, but no snt daemon is running it |
| `disabled` | turned off with `snt stop` |

`commit` is the commit the running process started from. `checkout` is the commit on disk.
They differ when a new commit is waiting for setup to succeed. `schema` changes only on
incompatible changes; new fields may appear at any time, so ignore fields you don't know.

## Files

```
~/.smolnugget/                 (--home / $SNT_HOME)
├── config.json                app definitions
├── secrets.env                tokens, KEY=VALUE, mode 600
├── daemon.lock, daemon.json   the running `snt run`
└── apps/NAME/
    ├── repo/                  the checkout; .gitignored files survive updates
    ├── venv/                  virtualenv (venv apps)
    ├── output.log             process output + snt events (rotated)
    └── state.json             pid, commit, restarts, last error (read by `snt status`)
```

`config.json` is plain JSON and safe to edit by hand. A running daemon picks up changes within
a second, and a broken edit is reported and ignored.

## Troubleshooting

- **`snt: command not found` after installing.** Open a new terminal. On Linux, check that
  `~/.local/bin` is in `PATH`; the installer prints the line to add.
- **The app keeps restarting.** Run `snt logs NAME -n 100`. The `*` lines show exit codes and
  backoff delays, the rest is your app's output.
- **"setup failed".** Usually a broken `requirements.txt`. The previous version keeps running.
  Push a fix, then `snt update NAME`.
- **The app doesn't see its `.env`.** It runs inside `apps/NAME/repo/`. Put `.env` there and
  make sure it is in `.gitignore`, or pass values with `snt edit NAME -e KEY=VALUE`.
- **"already run by another snt process".** A daemon is already running, maybe the service.
  Check with `snt status`; manage apps with `snt start/stop/restart` instead of a second `snt run`.
- **The daemon itself:** `journalctl --user -u smolnugget -f` (service) or `snt -v run` (foreground).

## Upgrading from 1.x

**From 1.x (`pip install`ed snt).** Nothing to do. A token stored in `config.json` moves to
`secrets.env` on first use. Note the changes: `snt add URL [NAME]` (the URL comes first now,
the name is optional), `snt list` is now `snt status`, and `snt systemd` is now
`snt service install` / `snt service show`.

**From the original single-file `snt.py`.** Re-create each app with `snt add`, then delete the
old `config.json` and `repo/` next to the script. **Revoke the old token**: that version wrote
it in plain text into `config.json` and `repo/.git/config`.

| old | new |
|---|---|
| `python3 snt.py --token=T start --repo 2 --repo-url=URL --start-file=bot.py --check-time=60` | `snt add URL 2 -c "python bot.py" --token T --check-interval 60`, then `snt run` |
| `--debug` | `-v`, or `snt logs NAME -f` |

## Development

```sh
git clone https://github.com/mLeviGen/smolnugget && cd smolnugget
python -m unittest discover -s tests -t .      # needs git; no other dependencies
python snt.py --home /tmp/snt-dev status       # try things without touching ~/.smolnugget
```

CI runs the tests on Linux, macOS and Windows with Python 3.11–3.13.

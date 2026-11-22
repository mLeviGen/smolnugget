#!/bin/sh
# smolnugget installer for Linux and macOS.
#
#   curl -fsSL https://raw.githubusercontent.com/mLeviGen/smolnugget/main/install.sh | sh
#   curl -fsSL .../install.sh | sh -s -- --service      # also install the systemd user service
#   curl -fsSL .../install.sh | sh -s -- --uninstall    # remove snt (apps and data are kept)
#
# Installs into ~/.local/share/smolnugget (its own venv) and links ~/.local/bin/snt.
# Re-run it to upgrade. Environment overrides: SNT_REF (branch/tag), SNT_PREFIX, SNT_BIN, SNT_SOURCE.
set -eu

REPO="https://github.com/mLeviGen/smolnugget"
REF="${SNT_REF:-main}"
PREFIX="${SNT_PREFIX:-$HOME/.local/share/smolnugget}"
BIN="${SNT_BIN:-$HOME/.local/bin}"
SOURCE="${SNT_SOURCE:-$REPO/archive/$REF.tar.gz}"

say()  { printf '\033[1m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[33mwarning:\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[31merror:\033[0m %s\n' "$*" >&2; exit 1; }

SERVICE=0
for arg in "$@"; do
    case "$arg" in
        --service) SERVICE=1 ;;
        --uninstall)
            [ -x "$PREFIX/venv/bin/snt" ] && "$PREFIX/venv/bin/snt" service uninstall 2>/dev/null || true
            rm -rf "$PREFIX"
            [ -L "$BIN/snt" ] && rm -f "$BIN/snt"
            say "smolnugget removed. Your apps and data in ~/.smolnugget were kept."
            exit 0 ;;
        -h|--help) sed -n '2,10p' "$0" 2>/dev/null || true; exit 0 ;;
        *) die "unknown option: $arg" ;;
    esac
done

# --- requirements -------------------------------------------------------------
command -v git >/dev/null 2>&1 || die "git is required (Debian/Ubuntu: sudo apt install git)"

PY=""
for cand in python3.14 python3.13 python3.12 python3.11 python3; do
    if command -v "$cand" >/dev/null 2>&1 &&
       "$cand" -c 'import sys; sys.exit(sys.version_info < (3, 11))' 2>/dev/null; then
        PY="$(command -v "$cand")"
        break
    fi
done
[ -n "$PY" ] || die "Python 3.11+ is required (Debian/Ubuntu: sudo apt install python3 python3-venv)"
"$PY" -c 'import venv, ensurepip' 2>/dev/null ||
    die "the venv module is missing (Debian/Ubuntu: sudo apt install python3-venv)"

say "using $("$PY" --version) at $PY"

# --- install --------------------------------------------------------------------
if [ ! -x "$PREFIX/venv/bin/python" ]; then
    say "creating $PREFIX/venv"
    mkdir -p "$PREFIX"
    "$PY" -m venv "$PREFIX/venv"
fi
say "installing smolnugget ($REF)"
"$PREFIX/venv/bin/python" -m pip install --quiet --disable-pip-version-check --upgrade pip
if [ -d "$SOURCE" ]; then SPEC="$SOURCE[top]"; else SPEC="smolnugget[top] @ $SOURCE"; fi
"$PREFIX/venv/bin/python" -m pip install --quiet --disable-pip-version-check --upgrade "$SPEC" ||
    die "pip install failed"

mkdir -p "$BIN"
ln -sf "$PREFIX/venv/bin/snt" "$BIN/snt"
say "installed $("$BIN/snt" --version) -> $BIN/snt"

case ":$PATH:" in
    *":$BIN:"*) ;;
    *) warn "$BIN is not in PATH. Add this to ~/.profile (or ~/.bashrc) and re-login:
    export PATH=\"$BIN:\$PATH\"" ;;
esac

if [ "$SERVICE" = 1 ]; then
    "$BIN/snt" service install
fi

cat <<EOF

Next steps:
  snt add https://github.com/you/your-bot     # clone + detect how to run it
  snt service install                         # keep everything running (systemd)
  snt top                                     # watch it live
EOF

#!/bin/sh
# RJH — Reverse Job Hunting :: one-line installer
# SPDX-License-Identifier: GPL-3.0-or-later
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/ideotion/RJH/main/install.sh | sh
#
# What it does (nothing hidden, all open source):
#   1. clones (or updates) the RJH repository,
#   2. creates a local Python virtual environment,
#   3. installs the three core dependencies (fastapi, uvicorn, requests).
# It does NOT install the optional extras (Playwright/Firefox, pypdf/odfpy,
# Ollama) or start any network jobs — those are opt-in from inside the app.
#
# Override the install location with:  RJH_DIR=/path sh install.sh
set -eu

REPO_URL="https://github.com/ideotion/RJH.git"
RAW_URL="https://raw.githubusercontent.com/ideotion/RJH/main"
BRANCH="main"
DIR="${RJH_DIR:-$HOME/rjh}"

say()  { printf '\033[1;36m==>\033[0m %s\n' "$1"; }
warn() { printf '\033[1;33m[!]\033[0m %s\n' "$1" >&2; }
die()  { printf '\033[1;31m[x]\033[0m %s\n' "$1" >&2; exit 1; }

# --- prerequisites ---------------------------------------------------------
PY=""
for c in python3 python; do
    if command -v "$c" >/dev/null 2>&1; then PY="$c"; break; fi
done
[ -n "$PY" ] || die "Python 3 is required but was not found. Install Python 3, then re-run."
say "Using $($PY --version 2>&1)"

# --- fetch the source ------------------------------------------------------
if command -v git >/dev/null 2>&1; then
    if [ -d "$DIR/.git" ]; then
        say "Updating existing checkout in $DIR"
        git -C "$DIR" pull --ff-only origin "$BRANCH" || warn "Could not update; using existing copy."
    else
        say "Cloning RJH into $DIR"
        git clone --depth 1 --branch "$BRANCH" "$REPO_URL" "$DIR"
    fi
else
    warn "git not found — downloading individual files with curl instead."
    mkdir -p "$DIR"
    for f in rjh.py requirements.txt LICENSE README.md MANIFESTO.md; do
        say "Fetching $f"
        curl -fsSL "$RAW_URL/$f" -o "$DIR/$f" || die "Failed to download $f"
    done
fi

cd "$DIR"

# --- virtual environment + core deps --------------------------------------
if [ ! -d venv ]; then
    say "Creating virtual environment"
    "$PY" -m venv venv || die "Could not create a virtualenv. On Debian/Ubuntu: sudo apt install python3-venv"
fi
say "Installing core dependencies (fastapi, uvicorn, requests)"
./venv/bin/python -m pip install --upgrade pip >/dev/null
./venv/bin/python -m pip install -r requirements.txt

# --- done ------------------------------------------------------------------
say "RJH is installed in $DIR"
cat <<EOF

  Start it with:
      cd "$DIR" && ./venv/bin/python rjh.py
  then open the printed URL (default http://127.0.0.1:8765) in your browser.

  Optional extras (opt-in, all local / open source):
    * Browser pre-fill:   ./venv/bin/python -m pip install playwright && ./venv/bin/playwright install firefox
    * PDF / ODT resumes:  ./venv/bin/python -m pip install pypdf odfpy
    * Local AI drafting:  install + manage Ollama from the app's Settings -> Setup tab

EOF

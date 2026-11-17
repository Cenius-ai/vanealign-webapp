#!/usr/bin/env bash
# VaneAlign install: project-level dependencies only, then EXIT.
# Runs as an unprivileged user; installs nothing system-wide and never starts
# the tool in the foreground. System prerequisites are listed in INSTALL.md.
set -euo pipefail
cd "$(dirname "$0")"

PYTHON="${PYTHON:-python3}"

"$PYTHON" -m pip install --upgrade pip setuptools wheel
"$PYTHON" -m pip install -r requirements.txt
"$PYTHON" -m pip install -e .

# Fail loudly here if anything the tool imports is missing, rather than at boot.
"$PYTHON" -c "import vanealign; print('vanealign', vanealign.VERSION, 'imports cleanly')"
"$PYTHON" vanealign.py --version

cat <<'DONE'

Setup complete. Nothing was started.
  Run the menu : python3 vanealign.py        (or: vanealign)
  Run a screen : python3 vanealign.py align
  Run the demo : bash demo.sh
  Run the tests: python3 -m pytest
DONE

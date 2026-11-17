#!/usr/bin/env bash
# VaneAlign end-to-end demo: non-interactive, offline, standard library only.
#   bash demo.sh
# It uses a throw-away data file in a temp directory, so your real
# ~/.vanealign/vanealign.json is never touched.
set -euo pipefail
cd "$(dirname "$0")"

PYTHON="${PYTHON:-python3}"
mkdir -p demo
DATA="demo/vanealign-demo.json"
PASS="cenius"
trap 'rm -f "$DATA"' EXIT

banner() { printf '\n== %s\n' "$*"; }
show() { printf '\n$ vanealign %s\n' "$*"; }

banner "VaneAlign $( "$PYTHON" vanealign.py --version | awk '{print $2}')   demo session (throw-away data file: $DATA)"
"$PYTHON" vanealign.py --version

banner "1. First run: set a passphrase, then tick steps 3 and 5 from the menu"
printf '\n$ vanealign      # first run: passphrase %s, then 1 Align, toggle 3 and 5, b, q\n' "$PASS"
printf '%s\n' "$PASS" "$PASS" 1 3 5 b q | "$PYTHON" vanealign.py --data "$DATA"

banner "2. Relaunch: the ticks came back from the JSON file"
export VANEALIGN_PASSPHRASE="$PASS"   # non-interactive unlock for the rest of the demo
show "align"
"$PYTHON" vanealign.py --data "$DATA" align

banner "3. Eyepieces: 900 mm telescope, a 25 mm Plossl and a 9 mm Nagler"
show "eyepieces focal 900"
"$PYTHON" vanealign.py --data "$DATA" eyepieces focal 900
show "eyepieces add --label \"25mm Plossl\" --fl 25 --afov 50"
"$PYTHON" vanealign.py --data "$DATA" eyepieces add --label "25mm Plossl" --fl 25 --afov 50
show "eyepieces add --label \"9mm Nagler\" --fl 9 --afov 82"
"$PYTHON" vanealign.py --data "$DATA" eyepieces add --label "9mm Nagler" --fl 9 --afov 82
show "eyepieces"
"$PYTHON" vanealign.py --data "$DATA" eyepieces

banner "4. Notes: M31 logged first, M42 logged second - newest observation lists first"
show "notes add --object M31 --date 2024-10-01 --rating 3 --body \"core bright, dust lanes soft at 120x\""
"$PYTHON" vanealign.py --data "$DATA" notes add --object M31 --date 2024-10-01 --rating 3 --body "core bright, dust lanes soft at 120x"
show "notes add --object M42 --date 2024-11-02 --rating 4 --body \"Trapezium split cleanly at 120x, E star suspected\""
"$PYTHON" vanealign.py --data "$DATA" notes add --object M42 --date 2024-11-02 --rating 4 --body "Trapezium split cleanly at 120x, E star suspected"
show "notes"
"$PYTHON" vanealign.py --data "$DATA" notes

banner "5. Validation refuses bad input without writing anything"
show "align toggle 9"
set +e
"$PYTHON" vanealign.py --data "$DATA" align toggle 9
status=$?
set -e
echo "(exit status $status - the checklist is unchanged)"

show "eyepieces add --label \"7mm Ortho\" --fl 0 --afov 45"
set +e
"$PYTHON" vanealign.py --data "$DATA" eyepieces add --label "7mm Ortho" --fl 0 --afov 45
status=$?
set -e
echo "(exit status $status - nothing was added)"

banner "6. Reset clears the done marks and keeps the seven labels in order"
show "align reset"
"$PYTHON" vanealign.py --data "$DATA" align reset
show "align"
"$PYTHON" vanealign.py --data "$DATA" align

banner "7. Machine-readable state for scripts"
show "state --json"
"$PYTHON" vanealign.py --data "$DATA" state --json

banner "8. Everything lives in one human-readable JSON file"
printf '\n$ head -n 22 %s\n' "$DATA"
head -n 22 "$DATA"

banner "Demo complete. Your own data file at ~/.vanealign/vanealign.json was not touched."

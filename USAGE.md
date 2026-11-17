# Using VaneAlign

Every screen is reachable two ways: from the interactive menu (`python3 vanealign.py`)
and directly by subcommand. Both read and write the same JSON data file.

Throughout, `q` at any prompt (or Ctrl-D) quits cleanly with exit code 0 and leaves
the saved data untouched.

## First run and the passphrase gate

```
$ python3 vanealign.py
  No data file yet at /home/you/.vanealign/vanealign.json.
  Set a passphrase to protect your checklist, eyepieces and notes.
  It is stored only as a salted PBKDF2 hash; there is no reset.
  new passphrase:
  confirm passphrase:
  Created /home/you/.vanealign/vanealign.json
```

The passphrase is stored as a salted PBKDF2-HMAC-SHA256 hash (120 000 iterations,
iteration count recorded in the file) and compared with `hmac.compare_digest`.
Later runs ask for it before showing the checklist, the eyepiece table or the log;
a wrong entry prints `Passphrase not recognised.` and re-prompts (three attempts)
without revealing a single saved row. There is no reset and no recovery — deleting
the data file is the reset.

## Alignment checklist

```
$ python3 vanealign.py align

  Align ──────────────────────────────────────────────────────────────
  2 of 7 steps done

  [ ] 1.  Level the tripod and aim the mount roughly at true north
  [ ] 2.  Set the latitude scale to your site latitude
  [x] 3.  Fit and focus the polar scope
  [ ] 4.  Centre Polaris in the reticle
  [x] 5.  Rotate the RA axis to the correct clock position for the date and time
  [ ] 6.  Adjust the altitude and azimuth bolts to place Polaris on the reticle mark
  [ ] 7.  Confirm with a short drift check
  ────────────────────────────────────────────────────────────────────
```

* The seven steps are fixed: they are seeded in the documented order on first run
  and can never be reordered or removed.
* `vanealign align toggle 3` flips one step and saves immediately (`done` / `pending`
  do the same without the flip).
* `vanealign align reset` clears every done mark, keeps all seven labels in order,
  and saves.
* From the menu, the Align screen accepts `3`, `r` (reset, with a confirmation),
  `b` (back) or `q`.

Step 5 stays a manual instruction on purpose: computing the real Polaris clock
position needs an ephemeris and your longitude, which this tool deliberately keeps
out of scope.

## Eyepiece table

```
$ python3 vanealign.py eyepieces

  Eyepieces ──────────────────────────────────────────────────────────
  telescope focal length 900 mm

  no.  label                     focal    afov      mag true field
  ---------------------------------------------------------------
  1    25mm Plossl             25.0 mm   50.0°    36.0x      1.39°
  2    9mm Nagler               9.0 mm   82.0°   100.0x      0.82°
  ────────────────────────────────────────────────────────────────────
```

* `vanealign eyepieces add --label "25mm Plossl" --fl 25 --afov 50` — magnification is
  telescope focal length / eyepiece focal length (one decimal), true field is
  apparent field / magnification (two decimals, degrees).
* `vanealign eyepieces focal 900` — change the telescope focal length used for the
  maths (default 1200 mm).
* `vanealign eyepieces remove 2` — remove the eyepiece listed as entry 2.
* From the menu: `a` add, `d <n>` remove (with confirmation), `t` telescope focal
  length, `b` back.

Rejected values never reach the data file: a non-numeric, zero or negative focal
length and an apparent field outside 1–180° each print their own message and
re-prompt (or exit 1 when given as a flag).

## Seeing-conditions notes

```
$ python3 vanealign.py notes

  Notes ──────────────────────────────────────────────────────────────
  1.  2024-11-02  M42  seeing 4/5
      Trapezium split cleanly at 120x, E star suspected
  2.  2024-10-01  M31  seeing 3/5
      core bright, dust lanes soft at 120x
  ────────────────────────────────────────────────────────────────────
```

* `vanealign notes add --object M42 --date 2024-11-02 --rating 4 --body "..."`.
* The log is always listed newest observation date first; notes sharing a date fall
  back to creation time, newest first.
* `vanealign notes delete 1` deletes the note at that listed position.
* Validation: a blank object name, a date that is not `YYYY-MM-DD` (and a real
  calendar date) and a rating outside 1–5 each print a specific message naming the
  bad field and re-prompt; nothing is written.
* From the menu: `a` add, `d <n>` delete (with confirmation), `b` back.

## Scripting

`--json` gives stable machine-readable output for the listing commands:

```bash
python3 vanealign.py state --json
python3 vanealign.py align --json
python3 vanealign.py eyepieces --json
python3 vanealign.py notes --json
```

Set `VANEALIGN_PASSPHRASE` for unattended runs; results go to stdout, errors and
refusals to stderr with a non-zero exit code.

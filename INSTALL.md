# Installing VaneAlign

VaneAlign is a single-file, standard-library Python tool. Installation is short and
it never starts the tool for you.

## System prerequisites (never installed by the script)

| Requirement | Notes |
|---|---|
| Python 3.9+ | `python3 --version`. 3.11 is what this build is tested on. |
| pip | Ships with CPython; `python3 -m pip --version`. |

Nothing else. No compiler, no SQLite headers, no system services, no network at
runtime. Do not install anything with `sudo`/`apt`/`brew` for this tool.

## 1. Install (one command, then it exits)

```bash
bash install.sh
```

It runs, in order:

1. `python3 -m pip install --upgrade pip setuptools wheel`
2. `python3 -m pip install -r requirements.txt` (pytest, the only dependency)
3. `python3 -m pip install -e .` (puts the `vanealign` console script on PATH)
4. `python3 -c "import vanealign"` — fails loudly here if anything is missing
5. prints the run commands and **exits**

## 2. Seed / first run

There is no separate seed step: the first run of the tool creates and seeds its own
data file — the seven-step alignment checklist, the 1200 mm telescope and an empty
eyepiece table and note log — and asks you to choose a passphrase.

Non-interactively (tests, demos, recording) supply the passphrase through the
environment instead:

```bash
VANEALIGN_PASSPHRASE=cenius python3 vanealign.py --data ./demo.json state
```

## 3. Run

```bash
python3 vanealign.py            # interactive menu (passphrase first)
vanealign                       # same thing via the installed console script
```

Follow **quickstart** in [README.md](../README.md) or the walkthroughs in
[USAGE.md](../USAGE.md).

## 4. Test

```bash
python3 -m pytest
```

## Configuration

| Variable | Meaning | Default |
|---|---|---|
| `VANEALIGN_DATA` | path to the JSON data file | `~/.vanealign/vanealign.json` |
| `VANEALIGN_PASSPHRASE` | passphrase for non-interactive runs | unset (prompts) |
| `NO_COLOR` | disable ANSI colour | unset (colour only on a TTY) |

Copy `.env.example` to `.env` if you like — both variables are optional and the
tool boots with no environment at all.

## Uninstalling

```bash
python3 -m pip uninstall vanealign
rm -rf ~/.vanealign        # the data file, including the passphrase hash
```

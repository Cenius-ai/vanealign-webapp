#!/usr/bin/env python3
"""VaneAlign - a local, standard-library companion for visual astronomers.

Three things an observer actually needs at the mount: the seven-step polar
alignment procedure, an eyepiece magnification / true-field table for the
telescope in use, and a dated log of seeing conditions per object. Everything
lives in one human-readable JSON file behind a local passphrase gate, so a
checklist ticked in the dark is still ticked after a relaunch.

Design direction (committed): calm-precise, terminal dark, monospace, compact.
    accent        oklch(0.58 0.12 205)  ==  #008d9c  (used via ANSI truecolor;
                  the same hex is the source for any non-terminal surface)
    neutral ramp  terminal background + dim grey #7a8288 for secondary text
    signature     aligned columns, box-drawing separators, ONE accent for
                  primary status, and a textual marker ([x] / [ ]) so that no
                  signal is ever colour-only.
"""

from __future__ import annotations

import argparse
import getpass
import hashlib
import hmac
import json
import math
import os
import re
import secrets
import sys
import tempfile
import textwrap
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional, Sequence, TextIO

APP_NAME = "VaneAlign"
VERSION = "1.0.0"
SCHEMA_VERSION = 1

# --- committed palette (see module docstring) ------------------------------
ACCENT_HEX = "#008d9c"
_ANSI_ACCENT = "38;2;0;141;156"
_ANSI_DIM = "38;2;122;130;136"
_ANSI_BOLD = "1"

# --- tuning ----------------------------------------------------------------
PBKDF2_ITERATIONS = 120_000
PASSPHRASE_ATTEMPTS = 3
DEFAULT_TELESCOPE_MM = 1200
MAX_TELESCOPE_MM = 200_000
RULE_WIDTH = 68
PROMPT_COLUMN = 2

# --- exit codes (documented in README/--help) ------------------------------
EXIT_OK = 0
EXIT_USAGE = 1
EXIT_DATA = 2
EXIT_AUTH = 3

ALIGNMENT_STEPS: tuple[str, ...] = (
    "Level the tripod and aim the mount roughly at true north",
    "Set the latitude scale to your site latitude",
    "Fit and focus the polar scope",
    "Centre Polaris in the reticle",
    "Rotate the RA axis to the correct clock position for the date and time",
    "Adjust the altitude and azimuth bolts to place Polaris on the reticle mark",
    "Confirm with a short drift check",
)

DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")

MSG_STEP = "Step must be between 1 and 7."
MSG_LABEL = "Eyepiece label must not be blank."
MSG_FOCAL = "Eyepiece focal length must be a number greater than 0."
MSG_AFOV = "Apparent field of view must be a number between 1 and 180 degrees."
MSG_TELESCOPE = (
    f"Telescope focal length must be a whole number of millimetres "
    f"between 1 and {MAX_TELESCOPE_MM}."
)
MSG_OBJECT = "Object name must not be blank."
MSG_DATE = "Observation date must be YYYY-MM-DD, for example 2024-11-02."
MSG_RATING = "Seeing rating must be a whole number from 1 to 5."


# ---------------------------------------------------------------------------
# errors
# ---------------------------------------------------------------------------
class VaneAlignError(Exception):
    """Any error that ends the run with a single friendly line."""

    exit_code = EXIT_USAGE


class DataFileError(VaneAlignError):
    """The data file cannot be read, parsed or written."""

    exit_code = EXIT_DATA


class AuthError(VaneAlignError):
    """The passphrase gate refused the run."""

    exit_code = EXIT_AUTH


class QuitRequested(Exception):
    """The user typed q (or sent EOF / Ctrl-D) at a prompt."""


# ---------------------------------------------------------------------------
# console / rendering
# ---------------------------------------------------------------------------
def _rule_char() -> str:
    encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
    try:
        "\u2500".encode(encoding)
    except (UnicodeEncodeError, LookupError):
        return "-"
    return "\u2500"


def colour_enabled(no_color_flag: bool) -> bool:
    if no_color_flag or os.environ.get("NO_COLOR") is not None:
        return False
    if os.environ.get("TERM", "") == "dumb":
        return False
    return bool(getattr(sys.stdout, "isatty", lambda: False)())


@dataclass
class Console:
    """Prints the committed terminal palette, one accent only."""

    stream: TextIO
    colour: bool

    def paint(self, text: str, kind: str = "accent") -> str:
        if not self.colour:
            return text
        code = {"accent": _ANSI_ACCENT, "dim": _ANSI_DIM, "bold": _ANSI_BOLD}[kind]
        return f"\x1b[{code}m{text}\x1b[0m"

    def out(self, text: str = "") -> None:
        print(text, file=self.stream, flush=True)

    def blank(self) -> None:
        self.out()

    def rule(self, title: str = "") -> str:
        bar = _rule_char()
        if not title:
            return " " * PROMPT_COLUMN + bar * RULE_WIDTH
        head = " " * PROMPT_COLUMN + title + " "
        return head + bar * max(RULE_WIDTH - len(title) - 1, 3)

    def ok(self, message: str) -> None:
        self.out(" " * PROMPT_COLUMN + self.paint(message, "accent"))

    def problem(self, message: str) -> None:
        self.out(" " * PROMPT_COLUMN + "! " + self.paint(message, "accent"))

    def dim(self, message: str) -> None:
        self.out(" " * PROMPT_COLUMN + self.paint(message, "dim"))

    def fail(self, message: str) -> None:
        print(f"vanealign: {message}", file=sys.stderr, flush=True)


def emit_json(payload: Any) -> None:
    print(json.dumps(payload, indent=2, ensure_ascii=False), flush=True)


# ---------------------------------------------------------------------------
# input helpers
# ---------------------------------------------------------------------------
def read_answer(prompt: str, secret: bool = False) -> str:
    """Read one line. `q`, EOF and Ctrl-D all abandon the run cleanly."""
    try:
        if secret and sys.stdin.isatty():
            return getpass.getpass(prompt)
        return input(prompt)
    except (EOFError, KeyboardInterrupt):
        print("", flush=True)
        raise QuitRequested from None


def ask(
    con: Console,
    prompt: str,
    parse: Callable[[str], Any],
    message: str,
    secret: bool = False,
    default: Optional[str] = None,
) -> Any:
    """Prompt until `parse` accepts the answer; re-prompt with a reason."""
    while True:
        raw = read_answer(prompt, secret=secret)
        if default is not None and not raw.strip():
            return parse(default)
        try:
            return parse(raw)
        except ValueError:
            con.problem(message)


def parse_step(raw: str) -> int:
    try:
        value = int(raw.strip())
    except (TypeError, ValueError):
        raise ValueError(MSG_STEP) from None
    if not 1 <= value <= 7:
        raise ValueError(MSG_STEP)
    return value


def parse_text(raw: str) -> str:
    text = " ".join(raw.split())
    if not text:
        raise ValueError("Value must not be blank.")
    return text


def parse_object(raw: str) -> str:
    text = " ".join(raw.split())
    if not text:
        raise ValueError(MSG_OBJECT)
    return text


def parse_label(raw: str) -> str:
    text = " ".join(raw.split())
    if not text:
        raise ValueError(MSG_LABEL)
    return text


def parse_focal(raw: str) -> float:
    text = (raw or "").strip()
    if not text:
        raise ValueError(MSG_FOCAL)
    try:
        value = float(text)
    except ValueError:
        raise ValueError(MSG_FOCAL) from None
    if not math.isfinite(value) or value <= 0:
        raise ValueError(MSG_FOCAL)
    return value


def parse_afov(raw: str) -> float:
    text = (raw or "").strip()
    if not text:
        raise ValueError(MSG_AFOV)
    try:
        value = float(text)
    except ValueError:
        raise ValueError(MSG_AFOV) from None
    if not math.isfinite(value) or not 1.0 <= value <= 180.0:
        raise ValueError(MSG_AFOV)
    return value


def parse_telescope(raw: str) -> int:
    text = (raw or "").strip()
    if not text:
        raise ValueError(MSG_TELESCOPE)
    try:
        value = int(text)
    except ValueError:
        raise ValueError(MSG_TELESCOPE) from None
    if not 1 <= value <= MAX_TELESCOPE_MM:
        raise ValueError(MSG_TELESCOPE)
    return value


def parse_date(raw: str) -> str:
    text = (raw or "").strip()
    if not DATE_PATTERN.match(text):
        raise ValueError(MSG_DATE)
    try:
        date.fromisoformat(text)
    except ValueError:
        raise ValueError(MSG_DATE) from None
    return text


def parse_rating(raw: str) -> int:
    try:
        value = int((raw or "").strip())
    except (TypeError, ValueError):
        raise ValueError(MSG_RATING) from None
    if not 1 <= value <= 5:
        raise ValueError(MSG_RATING)
    return value


def parse_body(raw: str) -> str:
    return " ".join((raw or "").split())


def require(parse: Callable[[str], Any], raw: str, message: str) -> Any:
    """Parse a command-line flag value, turning failures into a usage error."""
    try:
        return parse(raw)
    except ValueError:
        raise VaneAlignError(message) from None


def today_iso() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def now_iso() -> str:
    """UTC ISO-8601 with microseconds, so two same-second notes still order."""
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def new_id() -> str:
    return secrets.token_hex(4)


# ---------------------------------------------------------------------------
# store: read, normalise, atomic write
# ---------------------------------------------------------------------------
def resolve_data_path(cli_path: Optional[str]) -> Path:
    raw = cli_path or os.environ.get("VANEALIGN_DATA") or ""
    if raw.strip():
        return Path(raw.strip()).expanduser()
    return Path.home() / ".vanealign" / "vanealign.json"


def _shape_error(path: Path, detail: str) -> DataFileError:
    return DataFileError(f"{path}: {detail}. Fix the file or delete it to start over.")


def _check_store_shape(path: Path, store: dict[str, Any]) -> None:
    for key in ("profile", "telescope"):
        if not isinstance(store.get(key), dict):
            raise _shape_error(path, f"section '{key}' is missing or not an object")
    for key in ("checklist", "eyepieces", "notes"):
        if not isinstance(store.get(key), list):
            raise _shape_error(path, f"section '{key}' is missing or not a list")
    telescope = store["telescope"]
    focal = telescope.get("focal_length_mm")
    if not isinstance(focal, int) or isinstance(focal, bool) or not 1 <= focal <= MAX_TELESCOPE_MM:
        raise _shape_error(path, "telescope focal_length_mm is not a positive whole number")
    for step in store["checklist"]:
        if not isinstance(step, dict) or not isinstance(step.get("label"), str):
            raise _shape_error(path, "checklist entries must be objects with a label")
        if not isinstance(step.get("index"), int) or not isinstance(step.get("completed"), bool):
            raise _shape_error(path, "checklist entries need an index and a completed flag")
    for entry in store["eyepieces"]:
        if not isinstance(entry, dict) or not isinstance(entry.get("label"), str):
            raise _shape_error(path, "eyepieces must be objects with a label")
    for note in store["notes"]:
        if not isinstance(note, dict) or not isinstance(note.get("object_name"), str):
            raise _shape_error(path, "notes must be objects with an object_name")


def load_store(path: Path) -> Optional[dict[str, Any]]:
    """Return the parsed store, or None when the data file does not exist yet."""
    if not path.exists():
        return None
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise DataFileError(f"{path}: cannot be read ({exc.strerror or exc})") from exc
    try:
        store = json.loads(raw)
    except ValueError:
        raise DataFileError(f"{path}: not valid JSON. Fix the file or delete it to start over.") from None
    if not isinstance(store, dict):
        raise _shape_error(path, "the top level is not a JSON object")
    _check_store_shape(path, store)
    return store


def save_store(path: Path, store: dict[str, Any]) -> None:
    """Write atomically: temp file in the same directory, then os.replace."""
    store["schema_version"] = SCHEMA_VERSION
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handle, temp_name = tempfile.mkstemp(dir=str(path.parent), prefix=".vanealign-", suffix=".tmp")
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                json.dump(store, stream, indent=2, ensure_ascii=False)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp_name, path)
        except BaseException:
            try:
                os.unlink(temp_name)
            except OSError:
                pass
            raise
    except OSError as exc:
        raise DataFileError(f"{path}: cannot be written ({exc.strerror or exc})") from exc


# ---------------------------------------------------------------------------
# passphrase gate
# ---------------------------------------------------------------------------
def default_checklist() -> list[dict[str, Any]]:
    return [
        {"index": index, "label": label, "completed": False}
        for index, label in enumerate(ALIGNMENT_STEPS, start=1)
    ]


def derive(passphrase: str, salt: bytes, iterations: int) -> str:
    return hashlib.pbkdf2_hmac("sha256", passphrase.encode("utf-8"), salt, iterations).hex()


def make_profile(passphrase: str) -> dict[str, Any]:
    salt = secrets.token_bytes(16)
    return {
        "passphrase_salt": salt.hex(),
        "passphrase_hash": derive(passphrase, salt, PBKDF2_ITERATIONS),
        "iterations": PBKDF2_ITERATIONS,
        "created_at": now_iso(),
    }


def fresh_store(passphrase: str) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "profile": make_profile(passphrase),
        "telescope": {"focal_length_mm": DEFAULT_TELESCOPE_MM},
        "checklist": default_checklist(),
        "eyepieces": [],
        "notes": [],
    }


def check_passphrase(path: Path, profile: dict[str, Any], candidate: str) -> bool:
    try:
        salt = bytes.fromhex(profile["passphrase_salt"])
        expected = str(profile["passphrase_hash"])
        iterations = int(profile["iterations"])
    except (KeyError, TypeError, ValueError):
        raise _shape_error(path, "the saved passphrase record is incomplete") from None
    if iterations < 1:
        raise _shape_error(path, "the saved passphrase iteration count is not a positive whole number")
    return hmac.compare_digest(derive(candidate, salt, iterations), expected)


def env_passphrase() -> Optional[str]:
    """The passphrase supplied through the environment, or None when unset.

    An empty or whitespace-only value counts as unset, so a bare
    `VANEALIGN_PASSPHRASE=` line in a shell profile never locks the tool out.
    """
    value = os.environ.get("VANEALIGN_PASSPHRASE")
    if value is None or not value.strip():
        return None
    return value


def _new_passphrase(con: Console) -> str:
    supplied = env_passphrase()
    if supplied is not None:
        return supplied
    while True:
        first = read_answer("  new passphrase: ", secret=True)
        if not first.strip():
            con.problem("Passphrase must not be blank.")
            continue
        second = read_answer("  confirm passphrase: ", secret=True)
        if first != second:
            con.problem("Passphrases did not match. Try again.")
            continue
        return first


def _verify_passphrase(con: Console, path: Path, store: dict[str, Any]) -> None:
    profile = store["profile"]
    supplied = env_passphrase()
    attempts = 1 if supplied is not None else PASSPHRASE_ATTEMPTS
    for _ in range(attempts):
        candidate = supplied if supplied is not None else read_answer("  passphrase: ", secret=True)
        if check_passphrase(path, profile, candidate):
            return
        con.problem("Passphrase not recognised.")
    raise AuthError("Passphrase not recognised. Nothing was shown and nothing was changed.")


def normalise_store(store: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    """Fill in anything a hand-edited file may have dropped."""
    reseeded = False
    store.setdefault("schema_version", SCHEMA_VERSION)
    if not store.get("checklist"):
        store["checklist"] = default_checklist()
        reseeded = True
    return store, reseeded


@dataclass
class Session:
    con: Console
    path: Path
    store: dict[str, Any]

    def save(self) -> None:
        save_store(self.path, self.store)

    # -- views over the store ------------------------------------------------
    @property
    def steps(self) -> list[dict[str, Any]]:
        return self.store["checklist"]

    @property
    def eyepieces(self) -> list[dict[str, Any]]:
        return self.store["eyepieces"]

    @property
    def notes(self) -> list[dict[str, Any]]:
        return self.store["notes"]

    @property
    def telescope_mm(self) -> int:
        return int(self.store["telescope"]["focal_length_mm"])

    def done_count(self) -> int:
        return sum(1 for step in self.steps if step["completed"])

    def status_line(self) -> str:
        return (
            f"telescope {self.telescope_mm} mm   "
            f"eyepieces {len(self.eyepieces)}   "
            f"notes {len(self.notes)}   "
            f"checklist {self.done_count()} of {len(self.steps)}"
        )


def open_session(con: Console, path: Path) -> Session:
    """Load the store behind the passphrase gate, creating it on first run."""
    store = load_store(path)
    if store is None:
        con.blank()
        con.rule("VaneAlign setup")
        con.out(f"  No data file yet at {path}.")
        con.out("  Set a passphrase to protect your checklist, eyepieces and notes.")
        con.out("  It is stored only as a salted PBKDF2 hash; there is no reset.")
        passphrase = _new_passphrase(con)
        store = fresh_store(passphrase)
        save_store(path, store)
        con.ok(f"Created {path}")
    else:
        _verify_passphrase(con, path, store)
    store, reseeded = normalise_store(store)
    session = Session(con, path, store)
    if reseeded:
        session.save()
        con.problem(
            f"The checklist was missing from {path} - the seven default steps were restored and saved."
        )
    return session


# ---------------------------------------------------------------------------
# domain operations
# ---------------------------------------------------------------------------
def magnification(telescope_mm: int, eyepiece_mm: float) -> float:
    return telescope_mm / eyepiece_mm


def true_field_deg(afov_deg: float, mag: float) -> float:
    return afov_deg / mag


def ordered_notes(notes: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """Newest observation first; same date falls back to newest creation time.

    Stored position is the final tie-break so that two notes created in the
    same instant still list the later entry first.
    """
    decorated = [
        (str(note.get("observed_on", "")), str(note.get("created_at", "")), position, note)
        for position, note in enumerate(notes)
    ]
    decorated.sort(key=lambda item: (item[0], item[1], item[2]), reverse=True)
    return [item[3] for item in decorated]


def set_step(session: Session, index: int, completed: bool) -> bool:
    step = session.steps[index - 1]
    step["completed"] = completed
    session.save()
    return bool(step["completed"])


def toggle_step(session: Session, index: int) -> bool:
    return set_step(session, index, not session.steps[index - 1]["completed"])


def reset_checklist(session: Session) -> int:
    for step in session.steps:
        step["completed"] = False
    session.save()
    return len(session.steps)


def add_eyepiece(session: Session, label: str, focal_mm: float, afov_deg: float) -> dict[str, Any]:
    entry = {
        "id": new_id(),
        "label": label,
        "focal_length_mm": round(focal_mm, 3),
        "afov_deg": round(afov_deg, 3),
        "added_at": now_iso(),
    }
    session.eyepieces.append(entry)
    session.save()
    return entry


def remove_eyepiece(session: Session, number: int) -> dict[str, Any]:
    if not 1 <= number <= len(session.eyepieces):
        raise VaneAlignError(f"There is no eyepiece {number}; the table has {len(session.eyepieces)}.")
    removed = session.eyepieces.pop(number - 1)
    session.save()
    return removed


def set_telescope(session: Session, focal_mm: int) -> int:
    session.store["telescope"]["focal_length_mm"] = focal_mm
    session.save()
    return focal_mm


def add_note(session: Session, object_name: str, observed_on: str, rating: int, body: str) -> dict[str, Any]:
    entry = {
        "id": new_id(),
        "object_name": object_name,
        "observed_on": observed_on,
        "seeing_rating": rating,
        "body": body,
        "created_at": now_iso(),
    }
    session.notes.append(entry)
    session.save()
    return entry


def delete_note(session: Session, number: int) -> dict[str, Any]:
    listed = ordered_notes(session.notes)
    if not 1 <= number <= len(listed):
        raise VaneAlignError(f"There is no note {number}; the log has {len(listed)}.")
    target = listed[number - 1]
    session.store["notes"] = [note for note in session.notes if note is not target]
    session.save()
    return target


# ---------------------------------------------------------------------------
# screens
# ---------------------------------------------------------------------------
def render_menu(session: Session) -> None:
    con = session.con
    con.blank()
    con.out("  " + con.paint(f"{APP_NAME} {VERSION}", "bold") + "   polar alignment, eyepieces and seeing notes")
    con.out(con.rule())
    con.out(f"  data      {session.path}")
    con.out(f"  {session.status_line()}")
    con.out(con.rule())
    con.out("  [1]  Align        seven-step polar alignment checklist")
    con.out("  [2]  Eyepieces     magnification and true field for this telescope")
    con.out("  [3]  Notes         seeing-conditions log, newest first")
    con.out("  [q]  Quit")
    if not session.eyepieces and not session.notes:
        con.blank()
        con.dim("Start with Align: the checklist is what gets you on the pole.")


def render_align(session: Session) -> None:
    con = session.con
    con.blank()
    con.out(con.rule("Align"))
    con.out(f"  {session.done_count()} of {len(session.steps)} steps done")
    con.blank()
    for step in session.steps:
        mark = con.paint("[x]", "accent") if step["completed"] else "[ ]"
        con.out(f"  {mark} {step['index']}.  {step['label']}")
    con.out(con.rule())


def render_eyepieces(session: Session) -> None:
    con = session.con
    con.blank()
    con.out(con.rule("Eyepieces"))
    con.out(f"  telescope focal length {session.telescope_mm} mm")
    con.blank()
    eyepieces = session.eyepieces
    if not eyepieces:
        con.out("  No eyepieces yet - add the first with a label, a focal length in mm")
        con.out("  and an apparent field of view in degrees.")
    else:
        con.out(f"  {'no.':<4} {'label':<20} {'focal':>10} {'afov':>7} {'mag':>8} {'true field':>10}")
        con.out("  " + "-" * 63)
        for number, entry in enumerate(eyepieces, start=1):
            mag = magnification(session.telescope_mm, float(entry["focal_length_mm"]))
            tfov = true_field_deg(float(entry["afov_deg"]), mag)
            con.out(
                f"  {number:<4} {str(entry['label'])[:20]:<20} "
                f"{float(entry['focal_length_mm']):>7.1f} mm {float(entry['afov_deg']):>6.1f}\u00b0 "
                f"{mag:>7.1f}x {tfov:>9.2f}\u00b0"
            )
    con.out(con.rule())


def render_notes(session: Session) -> None:
    con = session.con
    con.blank()
    con.out(con.rule("Notes"))
    listed = ordered_notes(session.notes)
    if not listed:
        con.out("  No observations recorded - add the first with an object, a date,")
        con.out("  a seeing rating 1-5 and the conditions you saw.")
    else:
        for number, note in enumerate(listed, start=1):
            con.out(
                f"  {number}.  {note.get('observed_on', '?')}  "
                f"{con.paint(str(note.get('object_name', '?')), 'accent')}  "
                f"seeing {note.get('seeing_rating', '?')}/5"
            )
            body = str(note.get("body") or "").strip()
            if body:
                con.out(textwrap.fill(body, width=RULE_WIDTH - 6, initial_indent="      ", subsequent_indent="      "))
    con.out(con.rule())


# ---------------------------------------------------------------------------
# interactive screens
# ---------------------------------------------------------------------------
def align_screen(session: Session) -> None:
    con = session.con
    while True:
        render_align(session)
        raw = read_answer("  step 1-7 to toggle, r reset, b back, q quit: ").strip().lower()
        if raw in {"", "b", "back"}:
            return
        if raw in {"r", "reset"}:
            answer = read_answer("  clear every done mark? [y/N]: ").strip().lower()
            if answer in {"y", "yes"}:
                reset_checklist(session)
                con.ok(f"Checklist reset - 0 of {len(session.steps)} done.")
            else:
                con.dim("Reset cancelled.")
            continue
        try:
            index = parse_step(raw)
        except ValueError:
            con.problem(MSG_STEP)
            continue
        completed = toggle_step(session, index)
        con.ok(f"Step {index} marked {'done' if completed else 'pending'} - {session.done_count()} of {len(session.steps)} done.")


def _prompt_eyepiece(session: Session, args: argparse.Namespace) -> dict[str, Any]:
    """Collect the three eyepiece fields from flags, then prompts, then save."""
    con = session.con
    label_arg = getattr(args, "label", None)
    focal_arg = getattr(args, "fl", None)
    afov_arg = getattr(args, "afov", None)
    label = (
        require(parse_label, label_arg, MSG_LABEL)
        if label_arg is not None
        else ask(con, "  label (for example 25mm Plossl): ", parse_label, MSG_LABEL)
    )
    focal_mm = (
        require(parse_focal, focal_arg, MSG_FOCAL)
        if focal_arg is not None
        else ask(con, "  focal length in mm: ", parse_focal, MSG_FOCAL)
    )
    afov = (
        require(parse_afov, afov_arg, MSG_AFOV)
        if afov_arg is not None
        else ask(con, "  apparent field of view in degrees: ", parse_afov, MSG_AFOV)
    )
    return add_eyepiece(session, label, focal_mm, afov)


def eyepieces_screen(session: Session) -> None:
    con = session.con
    while True:
        render_eyepieces(session)
        raw = read_answer("  a add, d <n> remove, t telescope, b back, q quit: ").strip().lower()
        if raw in {"", "b", "back"}:
            return
        if raw in {"a", "add"}:
            entry = _prompt_eyepiece(session, argparse.Namespace(label=None, fl=None, afov=None))
            mag = magnification(session.telescope_mm, float(entry["focal_length_mm"]))
            tfov = true_field_deg(float(entry["afov_deg"]), mag)
            con.ok(f"Added eyepiece {len(session.eyepieces)}: {entry['label']} ({mag:.1f}x, {tfov:.2f}\u00b0 true field).")
            continue
        if raw in {"t", "telescope"}:
            focal = ask(con, f"  telescope focal length in mm [{session.telescope_mm}]: ", parse_telescope, MSG_TELESCOPE, default=str(session.telescope_mm))
            set_telescope(session, focal)
            con.ok(f"Telescope focal length is now {focal} mm.")
            continue
        if raw.startswith("d"):
            parts = raw.split()
            number_raw = parts[1] if len(parts) > 1 else read_answer(f"  remove which eyepiece number (1-{len(session.eyepieces)}): ")
            try:
                number = int(number_raw.strip())
            except ValueError:
                con.problem(f"Eyepiece number must be between 1 and {len(session.eyepieces)}.")
                continue
            if not session.eyepieces:
                con.problem("There are no eyepieces to remove yet.")
                continue
            if not 1 <= number <= len(session.eyepieces):
                con.problem(f"Eyepiece number must be between 1 and {len(session.eyepieces)}.")
                continue
            label = session.eyepieces[number - 1]["label"]
            answer = read_answer(f"  remove eyepiece {number} ({label})? [y/N]: ").strip().lower()
            if answer not in {"y", "yes"}:
                con.dim("Removal cancelled.")
                continue
            remove_eyepiece(session, number)
            con.ok(f"Removed eyepiece {number} ({label}).")
            continue
        con.problem("Enter a for add, d <n> to remove, t for telescope, b to go back, q to quit.")


def _prompt_note(session: Session, args: argparse.Namespace) -> dict[str, Any]:
    """Collect the four note fields from flags, then prompts, then save."""
    con = session.con
    object_arg = getattr(args, "object", None)
    date_arg = getattr(args, "date", None)
    rating_arg = getattr(args, "rating", None)
    body_arg = getattr(args, "body", None)
    name = (
        require(parse_object, object_arg, MSG_OBJECT)
        if object_arg is not None
        else ask(con, "  object (for example M42): ", parse_object, MSG_OBJECT)
    )
    if date_arg is not None:
        observed_on = require(parse_date, date_arg, MSG_DATE)
    else:
        observed_on = ask(
            con,
            f"  observation date YYYY-MM-DD [{today_iso()}]: ",
            parse_date,
            MSG_DATE,
            default=today_iso(),
        )
    rating = (
        require(parse_rating, rating_arg, MSG_RATING)
        if rating_arg is not None
        else ask(con, "  seeing rating 1-5: ", parse_rating, MSG_RATING)
    )
    body = body_arg if body_arg is not None else read_answer("  conditions (one line): ")
    return add_note(session, name, observed_on, rating, parse_body(body))


def notes_screen(session: Session) -> None:
    con = session.con
    while True:
        render_notes(session)
        raw = read_answer("  a add, d <n> delete, b back, q quit: ").strip().lower()
        if raw in {"", "b", "back"}:
            return
        if raw in {"a", "add"}:
            entry = _prompt_note(session, argparse.Namespace(object=None, date=None, rating=None, body=None))
            con.ok(f"Added note for {entry['object_name']} on {entry['observed_on']} (seeing {entry['seeing_rating']}/5).")
            continue
        if raw.startswith("d"):
            listed = ordered_notes(session.notes)
            parts = raw.split()
            number_raw = parts[1] if len(parts) > 1 else read_answer(f"  delete which note number (1-{len(listed)}): ")
            try:
                number = int(number_raw.strip())
            except ValueError:
                con.problem(f"Note number must be between 1 and {len(listed)}.")
                continue
            if not listed:
                con.problem("There are no notes to delete yet.")
                continue
            if not 1 <= number <= len(listed):
                con.problem(f"Note number must be between 1 and {len(listed)}.")
                continue
            target = listed[number - 1]
            answer = read_answer(f"  delete the note for {target['object_name']} on {target['observed_on']}? [y/N]: ").strip().lower()
            if answer not in {"y", "yes"}:
                con.dim("Deletion cancelled.")
                continue
            delete_note(session, number)
            con.ok(f"Deleted the note for {target['object_name']} on {target['observed_on']}.")
            continue
        con.problem("Enter a for add, d <n> to delete, b to go back, q to quit.")


def menu_loop(session: Session) -> int:
    con = session.con
    while True:
        render_menu(session)
        choice = read_answer("  choose 1-3 or q: ").strip().lower()
        if not choice:
            continue
        if choice in {"1", "a", "align"}:
            align_screen(session)
        elif choice in {"2", "e", "eyepieces"}:
            eyepieces_screen(session)
        elif choice in {"3", "n", "notes"}:
            notes_screen(session)
        elif choice in {"4", "q", "quit"}:
            return EXIT_OK
        else:
            con.problem("Enter 1, 2, 3 or q.")
# ---------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------
def cmd_state(con: Console, path: Path, as_json: bool) -> int:
    """Report whether the data file and passphrase exist, then the live counts."""
    store = load_store(path)
    if store is None:
        payload = {"data_path": str(path), "exists": False, "passphrase_set": False}
        if as_json:
            emit_json(payload)
        else:
            con.blank()
            con.rule("State")
            con.out(f"  data      {path}")
            con.out("  passphrase   not set - the first run asks you to choose one")
            con.dim("No data file yet: run `vanealign` to create it.")
        return EXIT_OK

    session = open_session(con, path)
    payload = {
        "data_path": str(path),
        "exists": True,
        "passphrase_set": True,
        "telescope_focal_length_mm": session.telescope_mm,
        "eyepiece_count": len(session.eyepieces),
        "note_count": len(session.notes),
        "checklist_done": session.done_count(),
        "checklist_total": len(session.steps),
    }
    if as_json:
        emit_json(payload)
        return EXIT_OK
    con.blank()
    con.rule("State")
    con.out(f"  data      {path}")
    con.out(f"  {session.status_line()}")
    return EXIT_OK


def cmd_align(session: Session, args: argparse.Namespace) -> int:
    con = session.con
    action = args.align_action or "show"
    if action == "show":
        if args.json:
            emit_json(
                {
                    "steps": [dict(step) for step in session.steps],
                    "done": session.done_count(),
                    "total": len(session.steps),
                }
            )
            return EXIT_OK
        render_align(session)
        con.dim(f"Change it with `vanealign align toggle N`, or run `vanealign` for the menu.")
        return EXIT_OK
    if action == "reset":
        total = reset_checklist(session)
        con.ok(f"Checklist reset - 0 of {total} done.")
        return EXIT_OK
    step_arg = getattr(args, "step", None)
    if step_arg is None:
        index = ask(con, "  step 1-7: ", parse_step, MSG_STEP)
    else:
        index = require(parse_step, step_arg, MSG_STEP)
    if action == "toggle":
        completed = toggle_step(session, index)
    else:
        completed = set_step(session, index, action == "done")
    con.ok(
        f"Step {index} marked {'done' if completed else 'pending'} - "
        f"{session.done_count()} of {len(session.steps)} done."
    )
    return EXIT_OK


def cmd_eyepieces(session: Session, args: argparse.Namespace) -> int:
    con = session.con
    action = args.ep_action or "list"
    if action == "list":
        if args.json:
            emit_json(
                {
                    "telescope_focal_length_mm": session.telescope_mm,
                    "eyepieces": [
                        {
                            "entry": number,
                            "label": entry["label"],
                            "focal_length_mm": float(entry["focal_length_mm"]),
                            "afov_deg": float(entry["afov_deg"]),
                            "magnification": round(
                                magnification(session.telescope_mm, float(entry["focal_length_mm"])), 1
                            ),
                            "true_field_deg": round(
                                true_field_deg(
                                    float(entry["afov_deg"]),
                                    magnification(session.telescope_mm, float(entry["focal_length_mm"])),
                                ),
                                2,
                            ),
                        }
                        for number, entry in enumerate(session.eyepieces, start=1)
                    ],
                }
            )
            return EXIT_OK
        render_eyepieces(session)
        con.dim("Add with `vanealign eyepieces add`, or run `vanealign` for the menu.")
        return EXIT_OK
    if action == "add":
        entry = _prompt_eyepiece(session, args)
        mag = magnification(session.telescope_mm, float(entry["focal_length_mm"]))
        tfov = true_field_deg(float(entry["afov_deg"]), mag)
        con.ok(
            f"Added eyepiece {len(session.eyepieces)}: {entry['label']} "
            f"({mag:.1f}x, {tfov:.2f}\u00b0 true field)."
        )
        return EXIT_OK
    if action == "remove":
        number_arg = getattr(args, "number", None)
        message = f"Eyepiece number must be between 1 and {len(session.eyepieces)}."
        if number_arg is None:
            number = ask(
                con,
                "  eyepiece number: ",
                lambda raw: _positive_entry(raw, len(session.eyepieces)),
                message,
            )
        else:
            number = _flagged_entry(number_arg, len(session.eyepieces), message)
        removed = remove_eyepiece(session, number)
        con.ok(f"Removed eyepiece {number} ({removed['label']}).")
        return EXIT_OK
    focal_arg = getattr(args, "mm", None)
    if focal_arg is None:
        focal = ask(
            con,
            f"  telescope focal length in mm [{session.telescope_mm}]: ",
            parse_telescope,
            MSG_TELESCOPE,
            default=str(session.telescope_mm),
        )
    else:
        focal = require(parse_telescope, focal_arg, MSG_TELESCOPE)
    set_telescope(session, focal)
    con.ok(f"Telescope focal length is now {focal} mm.")
    return EXIT_OK


def cmd_notes(session: Session, args: argparse.Namespace) -> int:
    con = session.con
    action = args.note_action or "list"
    if action == "list":
        if args.json:
            emit_json(
                {
                    "notes": [
                        {
                            "entry": number,
                            "object_name": note["object_name"],
                            "observed_on": note["observed_on"],
                            "seeing_rating": note["seeing_rating"],
                            "body": note.get("body", ""),
                            "created_at": note.get("created_at", ""),
                        }
                        for number, note in enumerate(ordered_notes(session.notes), start=1)
                    ]
                }
            )
            return EXIT_OK
        render_notes(session)
        con.dim("Add with `vanealign notes add`, or run `vanealign` for the menu.")
        return EXIT_OK
    if action == "add":
        entry = _prompt_note(session, args)
        con.ok(
            f"Added note for {entry['object_name']} on {entry['observed_on']} "
            f"(seeing {entry['seeing_rating']}/5)."
        )
        return EXIT_OK
    number_arg = getattr(args, "number", None)
    listed = ordered_notes(session.notes)
    message = f"Note number must be between 1 and {len(listed)}."
    number = _flagged_entry(number_arg, len(listed), message) if number_arg is not None else ask(
        con, "  note number: ", lambda raw: _positive_entry(raw, len(listed)), message
    )
    target = delete_note(session, number)
    con.ok(f"Deleted the note for {target['object_name']} on {target['observed_on']}.")
    return EXIT_OK


def _flagged_entry(raw: str, count: int, message: str) -> int:
    """Parse a listed entry number given on the command line."""
    try:
        return _positive_entry(raw, count)
    except ValueError:
        raise VaneAlignError(message) from None


def _positive_entry(raw: str, count: int) -> int:
    try:
        number = int(str(raw).strip())
    except (TypeError, ValueError):
        raise ValueError(f"Number must be between 1 and {count}.") from None
    if not 1 <= number <= count:
        raise ValueError(f"Number must be between 1 and {count}.")
    return number


# ---------------------------------------------------------------------------
# argument parsing
# ---------------------------------------------------------------------------
EPILOG = """\
examples:
  vanealign                                             open the menu (passphrase first)
  vanealign align                                       show the seven-step checklist
  vanealign align toggle 3                              tick or untick step 3
  vanealign align reset                                 clear every done mark
  vanealign eyepieces add --label "25mm Plossl" --fl 25 --afov 50
  vanealign eyepieces focal 900                         change the telescope focal length
  vanealign notes add --object M42 --date 2024-11-02 --rating 4 \\
      --body "Trapezium split at 120x, steady"
  vanealign notes delete 1                              delete the first listed note
  vanealign --data ./sky.json notes                     use a different data file

The data file defaults to ~/.vanealign/vanealign.json; --data or
VANEALIGN_DATA overrides it, and VANEALIGN_PASSPHRASE supplies the
passphrase for non-interactive runs. `bash demo.sh` records a scripted
end-to-end session against a throw-away data file.

exit codes: 0 ok, 1 bad input, 2 data file problem, 3 passphrase refused\
"""


def _add_common(parser: argparse.ArgumentParser, defaults: bool) -> None:
    default = (lambda value: value) if defaults else (lambda value: argparse.SUPPRESS)
    parser.add_argument(
        "--data",
        metavar="PATH",
        default=default(None),
        help="JSON data file to use (default: ~/.vanealign/vanealign.json)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        default=default(False),
        help="print machine-readable JSON for the listing commands",
    )
    parser.add_argument(
        "--no-color",
        action="store_true",
        default=default(False),
        help="never emit ANSI colour",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="vanealign",
        description=(
            "Polar alignment checklist, eyepiece magnification table and "
            "seeing-conditions notes for visual astronomers. Local only: "
            "everything is kept in one JSON file behind a passphrase."
        ),
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    _add_common(parser, defaults=True)
    parser.add_argument("--version", action="version", version=f"{APP_NAME} {VERSION}")

    subparsers = parser.add_subparsers(dest="command", metavar="COMMAND")

    def add(name: str, help_text: str) -> argparse.ArgumentParser:
        child = subparsers.add_parser(
            name,
            help=help_text,
            description=help_text,
            parents=[_common_parent()],
        )
        return child

    add("menu", "open the interactive menu (the default with no command)")
    add("state", "show the data file, telescope focal length and current counts")
    add("unlock", "check the passphrase without opening a screen")

    align = add("align", "the seven-step polar alignment checklist")
    align_actions = align.add_subparsers(dest="align_action", metavar="ACTION")
    align_show = align_actions.add_parser("show", help="list the seven steps in order", parents=[_common_parent()])
    align_show.set_defaults(align_action="show")
    for action, action_help in (("toggle", "tick or untick one step"), ("done", "mark one step done"), ("pending", "mark one step pending")):
        child = align_actions.add_parser(action, help=action_help, parents=[_common_parent()])
        child.add_argument("step", nargs="?", help="step number 1-7")
    align_actions.add_parser("reset", help="clear every done mark, keeping the labels", parents=[_common_parent()])

    eyepieces = add("eyepieces", "the eyepiece magnification and true-field table")
    eyepiece_actions = eyepieces.add_subparsers(dest="ep_action", metavar="ACTION")
    eyepiece_actions.add_parser("list", help="list eyepieces with magnification and true field", parents=[_common_parent()])
    ep_add = eyepiece_actions.add_parser("add", help="add an eyepiece", parents=[_common_parent()])
    ep_add.add_argument("--label", help="eyepiece label, for example '25mm Plossl'")
    ep_add.add_argument("--fl", dest="fl", help="eyepiece focal length in mm")
    ep_add.add_argument("--afov", help="apparent field of view in degrees (1-180)")
    ep_remove = eyepiece_actions.add_parser("remove", help="remove an eyepiece by its listed number", parents=[_common_parent()])
    ep_remove.add_argument("number", nargs="?", help="listed entry number")
    ep_focal = eyepiece_actions.add_parser("focal", help="set the telescope focal length in mm", parents=[_common_parent()])
    ep_focal.add_argument("mm", nargs="?", help="telescope focal length in millimetres")

    notes = add("notes", "the seeing-conditions note log")
    note_actions = notes.add_subparsers(dest="note_action", metavar="ACTION")
    note_actions.add_parser("list", help="list notes newest observation first", parents=[_common_parent()])
    note_add = note_actions.add_parser("add", help="add a seeing-conditions note", parents=[_common_parent()])
    note_add.add_argument("--object", help="object name, for example M42")
    note_add.add_argument("--date", help="observation date as YYYY-MM-DD")
    note_add.add_argument("--rating", help="seeing rating 1-5")
    note_add.add_argument("--body", help="free-text conditions")
    note_delete = note_actions.add_parser("delete", help="delete a note by its listed number", parents=[_common_parent()])
    note_delete.add_argument("number", nargs="?", help="listed entry number")

    return parser


def _common_parent() -> argparse.ArgumentParser:
    parent = argparse.ArgumentParser(add_help=False)
    _add_common(parent, defaults=False)
    return parent


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------
def dispatch(args: argparse.Namespace, con: Console) -> int:
    path = resolve_data_path(args.data)
    command = args.command or "menu"
    if command == "state":
        return cmd_state(con, path, bool(getattr(args, "json", False)))
    session = open_session(con, path)
    if command == "unlock":
        con.ok("Passphrase accepted. Nothing was changed.")
        return EXIT_OK
    if command == "menu":
        return menu_loop(session)
    if command == "align":
        return cmd_align(session, args)
    if command == "eyepieces":
        return cmd_eyepieces(session, args)
    if command == "notes":
        return cmd_notes(session, args)
    raise VaneAlignError(f"Unknown command {command!r}. Try `vanealign --help`.")


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    con = Console(sys.stdout, colour_enabled(bool(getattr(args, "no_color", False))))
    try:
        return dispatch(args, con)
    except QuitRequested:
        con.out()
        con.dim("Quit - saved data is untouched.")
        return EXIT_OK
    except VaneAlignError as exc:
        con.fail(str(exc))
        return exc.exit_code
    except KeyboardInterrupt:
        con.out()
        con.dim("Interrupted - saved data is untouched.")
        return EXIT_OK
    except BrokenPipeError:
        return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())

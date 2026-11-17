"""End-to-end tests for VaneAlign.

Every test drives the real CLI in a subprocess against a throw-away data file,
exactly the way a user runs it, so the passphrase gate, atomic writes and the
relaunch behaviour are all exercised for real.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Optional, Sequence

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "vanealign.py"
PASSPHRASE = "cenius"

ALIGNMENT_LABELS = [
    "Level the tripod and aim the mount roughly at true north",
    "Set the latitude scale to your site latitude",
    "Fit and focus the polar scope",
    "Centre Polaris in the reticle",
    "Rotate the RA axis to the correct clock position for the date and time",
    "Adjust the altitude and azimuth bolts to place Polaris on the reticle mark",
    "Confirm with a short drift check",
]


def run(
    args: Sequence[str],
    stdin: str = "",
    data: Optional[Path] = None,
    passphrase: Optional[str] = PASSPHRASE,
) -> subprocess.CompletedProcess:
    env = {key: value for key, value in os.environ.items() if key != "VANEALIGN_PASSPHRASE"}
    env["NO_COLOR"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    if passphrase is not None:
        env["VANEALIGN_PASSPHRASE"] = passphrase
    command = [sys.executable, str(SCRIPT)]
    if data is not None:
        command += ["--data", str(data)]
    command += list(args)
    return subprocess.run(
        command,
        input=stdin,
        capture_output=True,
        text=True,
        env=env,
        cwd=str(ROOT),
        timeout=120,
    )


def combined(proc: subprocess.CompletedProcess) -> str:
    return (proc.stdout or "") + (proc.stderr or "")


def bootstrap(data: Path) -> Path:
    """Create the data file through the real first-run passphrase flow."""
    proc = run(["unlock"], stdin=f"{PASSPHRASE}\n{PASSPHRASE}\n", data=data, passphrase=None)
    assert proc.returncode == 0, combined(proc)
    return data


def read_store(data: Path) -> dict:
    return json.loads(data.read_text(encoding="utf-8"))


@pytest.fixture()
def data_file(tmp_path: Path) -> Path:
    return bootstrap(tmp_path / "vanealign.json")


# --- help, version, contract ----------------------------------------------
def test_help_lists_subcommands_and_data_option():
    proc = run(["--help"])
    assert proc.returncode == 0
    text = proc.stdout
    for word in ("align", "eyepieces", "notes", "--data", "--version"):
        assert word in text
    assert "examples:" in text


def test_version_flag():
    proc = run(["--version"])
    assert proc.returncode == 0
    assert "VaneAlign" in proc.stdout


def test_align_help_documents_toggle():
    proc = run(["align", "toggle", "--help"])
    assert proc.returncode == 0
    assert "step" in proc.stdout


# --- first run / passphrase gate ------------------------------------------
def test_first_run_creates_store_with_pbkdf2_hash(tmp_path: Path):
    data = tmp_path / "store.json"
    proc = run(["align"], stdin=f"{PASSPHRASE}\n{PASSPHRASE}\n", data=data, passphrase=None)
    assert proc.returncode == 0, combined(proc)
    store = read_store(data)
    assert store["schema_version"] == 1
    assert set(store) >= {"profile", "telescope", "checklist", "eyepieces", "notes"}
    profile = store["profile"]
    assert profile["iterations"] >= 100_000
    assert len(profile["passphrase_salt"]) == 32
    assert len(profile["passphrase_hash"]) == 64
    assert PASSPHRASE not in json.dumps(store)
    assert store["telescope"]["focal_length_mm"] == 1200
    assert store["eyepieces"] == [] and store["notes"] == []


def test_first_run_rejects_mismatched_passphrase(tmp_path: Path):
    data = tmp_path / "store.json"
    proc = run(["unlock"], stdin="first\nsecond\nletmein\nletmein\n", data=data, passphrase=None)
    assert proc.returncode == 0, combined(proc)
    assert "Passphrases did not match" in proc.stdout
    good = run(["unlock"], data=data, passphrase="letmein")
    assert good.returncode == 0, combined(good)


def test_wrong_passphrase_is_refused_and_shows_no_data(data_file: Path):
    proc = run(["align"], data=data_file, passphrase="not-the-passphrase")
    assert proc.returncode == 3
    assert "Passphrase not recognised" in combined(proc)
    assert "Fit and focus the polar scope" not in combined(proc)


def test_correct_passphrase_reaches_menu_status(data_file: Path):
    proc = run(["state"], data=data_file)
    assert proc.returncode == 0, combined(proc)
    assert "telescope 1200 mm" in proc.stdout
    assert "eyepieces 0" in proc.stdout
    assert "notes 0" in proc.stdout


def test_empty_passphrase_environment_variable_is_treated_as_unset(data_file: Path):
    proc = run(["unlock"], stdin=f"{PASSPHRASE}\n", data=data_file, passphrase="")
    assert proc.returncode == 0, combined(proc)
    assert "Passphrase not recognised" not in combined(proc)


def test_corrupt_data_file_exits_two_with_one_line(tmp_path: Path):
    data = tmp_path / "broken.json"
    data.write_text('{"profile": ', encoding="utf-8")
    proc = run(["align"], data=data)
    assert proc.returncode == 2
    assert str(data) in proc.stderr
    assert "Traceback" not in combined(proc)
    assert len(proc.stderr.strip().splitlines()) == 1


def test_unwritable_parent_reports_one_line(tmp_path: Path):
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("x", encoding="utf-8")
    proc = run(["align"], data=blocker / "store.json")
    assert proc.returncode == 2
    assert "Traceback" not in combined(proc)


# --- alignment checklist ---------------------------------------------------
def test_checklist_seeds_seven_steps_in_fixed_order(data_file: Path):
    proc = run(["align"], data=data_file)
    assert proc.returncode == 0, combined(proc)
    text = proc.stdout
    assert "0 of 7 steps done" in text
    positions = [text.index(label) for label in ALIGNMENT_LABELS]
    assert positions == sorted(positions)
    for number, label in enumerate(ALIGNMENT_LABELS, start=1):
        assert f"{number}.  {label}" in text
    assert text.count("[ ]") == 7


def test_toggle_one_step_marks_only_that_step(data_file: Path):
    proc = run(["align", "toggle", "3"], data=data_file)
    assert proc.returncode == 0, combined(proc)
    assert "Step 3 marked done - 1 of 7 done." in proc.stdout
    listing = run(["align"], data=data_file).stdout
    assert "[x] 3.  Fit and focus the polar scope" in listing
    assert listing.count("[x]") == 1
    assert "1 of 7 steps done" in listing


def test_done_marks_survive_a_relaunch(data_file: Path):
    assert run(["align", "toggle", "3"], data=data_file).returncode == 0
    assert run(["align", "toggle", "5"], data=data_file).returncode == 0
    listing = run(["align"], data=data_file).stdout
    assert "[x] 3.  Fit and focus the polar scope" in listing
    assert "[x] 5.  Rotate the RA axis to the correct clock position for the date and time" in listing
    assert listing.count("[x]") == 2
    assert "2 of 7 steps done" in listing
    store = read_store(data_file)
    assert [step["completed"] for step in store["checklist"]] == [
        False, False, True, False, True, False, False,
    ]


def test_toggle_rejects_out_of_range_and_keeps_list_unchanged(data_file: Path):
    before = read_store(data_file)
    proc = run(["align", "toggle", "9"], data=data_file)
    assert proc.returncode == 1
    assert "Step must be between 1 and 7" in combined(proc)
    assert read_store(data_file) == before


def test_reset_clears_done_marks_but_keeps_the_seven_labels(data_file: Path):
    run(["align", "toggle", "2"], data=data_file)
    run(["align", "toggle", "6"], data=data_file)
    proc = run(["align", "reset"], data=data_file)
    assert proc.returncode == 0, combined(proc)
    store = read_store(data_file)
    assert [step["label"] for step in store["checklist"]] == ALIGNMENT_LABELS
    assert [step["index"] for step in store["checklist"]] == list(range(1, 8))
    assert all(step["completed"] is False for step in store["checklist"])
    assert "0 of 7 steps done" in run(["align"], data=data_file).stdout


def test_missing_checklist_is_reseeded_and_saved(data_file: Path):
    store = read_store(data_file)
    store["checklist"] = []
    data_file.write_text(json.dumps(store), encoding="utf-8")
    proc = run(["align"], data=data_file)
    assert proc.returncode == 0, combined(proc)
    assert "seven default steps were restored" in proc.stdout
    assert len(read_store(data_file)["checklist"]) == 7


# --- eyepieces -------------------------------------------------------------
def test_empty_eyepiece_table_invites_the_first(data_file: Path):
    proc = run(["eyepieces"], data=data_file)
    assert proc.returncode == 0, combined(proc)
    assert "No eyepieces yet" in proc.stdout
    assert "1200 mm" in proc.stdout


def test_eyepiece_maths_for_a_1200mm_telescope(data_file: Path):
    proc = run(["eyepieces", "add", "--label", "25mm Plossl", "--fl", "25", "--afov", "50"], data=data_file)
    assert proc.returncode == 0, combined(proc)
    listing = run(["eyepieces"], data=data_file).stdout
    assert "25mm Plossl" in listing
    assert "48.0x" in listing
    assert "1.04" in listing


def test_eyepieces_survive_a_relaunch(data_file: Path):
    run(["eyepieces", "add", "--label", "25mm Plossl", "--fl", "25", "--afov", "50"], data=data_file)
    run(["eyepieces", "add", "--label", "9mm Nagler", "--fl", "9", "--afov", "82"], data=data_file)
    listing = run(["eyepieces"], data=data_file).stdout
    assert "25mm Plossl" in listing and "9mm Nagler" in listing
    assert "133.3x" in listing  # 1200 / 9
    store = read_store(data_file)
    assert [entry["label"] for entry in store["eyepieces"]] == ["25mm Plossl", "9mm Nagler"]


@pytest.mark.parametrize(
    "args, message",
    [
        (["--label", "Test", "--fl", "abc", "--afov", "50"], "focal length"),
        (["--label", "Test", "--fl", "0", "--afov", "50"], "focal length"),
        (["--label", "Test", "--fl", "-25", "--afov", "50"], "focal length"),
        (["--label", "Test", "--fl", "25", "--afov", "181"], "Apparent field of view"),
        (["--label", "   ", "--fl", "25", "--afov", "50"], "label must not be blank"),
    ],
)
def test_eyepiece_validation_rejects_bad_values(data_file: Path, args, message):
    before = read_store(data_file)
    proc = run(["eyepieces", "add", *args], data=data_file)
    assert proc.returncode == 1
    assert message.lower() in combined(proc).lower()
    assert read_store(data_file) == before


def test_remove_eyepiece_persists(data_file: Path):
    for label, fl, afov in (("32mm Plossl", "32", "50"), ("25mm Plossl", "25", "50"), ("9mm Nagler", "9", "82")):
        run(["eyepieces", "add", "--label", label, "--fl", fl, "--afov", afov], data=data_file)
    proc = run(["eyepieces", "remove", "2"], data=data_file)
    assert proc.returncode == 0, combined(proc)
    listing = run(["eyepieces"], data=data_file).stdout
    assert "25mm Plossl" not in listing
    assert "32mm Plossl" in listing and "9mm Nagler" in listing
    assert [entry["label"] for entry in read_store(data_file)["eyepieces"]] == ["32mm Plossl", "9mm Nagler"]


def test_telescope_focal_length_changes_the_maths(data_file: Path):
    proc = run(["eyepieces", "focal", "900"], data=data_file)
    assert proc.returncode == 0, combined(proc)
    run(["eyepieces", "add", "--label", "25mm Plossl", "--fl", "25", "--afov", "50"], data=data_file)
    listing = run(["eyepieces"], data=data_file).stdout
    assert "900 mm" in listing
    assert "36.0x" in listing
    assert read_store(data_file)["telescope"]["focal_length_mm"] == 900


def test_telescope_focal_length_rejects_zero(data_file: Path):
    before = read_store(data_file)
    proc = run(["eyepieces", "focal", "0"], data=data_file)
    assert proc.returncode == 1
    assert "Telescope focal length" in combined(proc)
    assert read_store(data_file) == before


def test_interactive_eyepiece_add_from_the_menu(tmp_path: Path):
    data = tmp_path / "menu.json"
    stdin = (
        f"{PASSPHRASE}\n{PASSPHRASE}\n"
        "2\n"
        "a\n25mm Plossl\n25\n50\n"
        "b\n"
        "q\n"
    )
    proc = run([], stdin=stdin, data=data, passphrase=None)
    assert proc.returncode == 0, combined(proc)
    assert "Added eyepiece 1: 25mm Plossl (48.0x, 1.04" in proc.stdout
    assert len(read_store(data)["eyepieces"]) == 1


# --- notes -----------------------------------------------------------------
def test_empty_note_log_invites_the_first(data_file: Path):
    proc = run(["notes"], data=data_file)
    assert proc.returncode == 0, combined(proc)
    assert "No observations recorded" in proc.stdout


def test_note_appears_at_the_top_of_the_log(data_file: Path):
    proc = run(
        ["notes", "add", "--object", "M42", "--date", "2024-11-02", "--rating", "4", "--body", "Trapezium split at 120x"],
        data=data_file,
    )
    assert proc.returncode == 0, combined(proc)
    listing = run(["notes"], data=data_file).stdout
    assert "2024-11-02" in listing
    assert "M42" in listing
    assert "seeing 4/5" in listing
    assert "Trapezium split at 120x" in listing


def test_notes_are_listed_newest_observation_first(data_file: Path):
    run(["notes", "add", "--object", "M31", "--date", "2024-10-01", "--rating", "3", "--body", "soft"], data=data_file)
    run(["notes", "add", "--object", "M42", "--date", "2024-11-02", "--rating", "4", "--body", "steady"], data=data_file)
    listing = run(["notes"], data=data_file).stdout
    assert listing.index("M42") < listing.index("M31")
    assert "1.  2024-11-02" in listing
    assert "2.  2024-10-01" in listing


def test_same_date_notes_keep_newest_created_first(data_file: Path):
    run(["notes", "add", "--object", "M13", "--date", "2024-09-20", "--rating", "5", "--body", "first"], data=data_file)
    run(["notes", "add", "--object", "M92", "--date", "2024-09-20", "--rating", "4", "--body", "second"], data=data_file)
    listing = run(["notes"], data=data_file).stdout
    assert listing.index("M92") < listing.index("M13")


@pytest.mark.parametrize(
    "args, message",
    [
        (["--object", "   ", "--date", "2024-11-02", "--rating", "4", "--body", "x"], "Object name must not be blank"),
        (["--object", "M42", "--date", "11/02/2024", "--rating", "4", "--body", "x"], "Observation date must be YYYY-MM-DD"),
        (["--object", "M42", "--date", "2024-13-40", "--rating", "4", "--body", "x"], "Observation date must be YYYY-MM-DD"),
        (["--object", "M42", "--date", "2024-11-02", "--rating", "7", "--body", "x"], "Seeing rating must be a whole number from 1 to 5"),
        (["--object", "M42", "--date", "2024-11-02", "--rating", "0", "--body", "x"], "Seeing rating"),
    ],
)
def test_note_validation_rejects_bad_fields(data_file: Path, args, message):
    before = read_store(data_file)
    proc = run(["notes", "add", *args], data=data_file)
    assert proc.returncode == 1
    assert message in combined(proc)
    assert read_store(data_file) == before


def test_delete_note_persists(data_file: Path):
    for name, day in (("M31", "2024-10-01"), ("M42", "2024-11-02"), ("M13", "2024-09-20")):
        run(["notes", "add", "--object", name, "--date", day, "--rating", "4", "--body", "seen"], data=data_file)
    proc = run(["notes", "delete", "2"], data=data_file)  # entry 2 is M31
    assert proc.returncode == 0, combined(proc)
    listing = run(["notes"], data=data_file).stdout
    assert "M31" not in listing
    assert "M42" in listing and "M13" in listing
    assert sorted(note["object_name"] for note in read_store(data_file)["notes"]) == ["M13", "M42"]


def test_delete_missing_note_is_a_usage_error(data_file: Path):
    proc = run(["notes", "delete", "4"], data=data_file)
    assert proc.returncode == 1
    assert "Note number must be between" in combined(proc)


# --- interactive behaviour -------------------------------------------------
def test_q_at_a_prompt_exits_zero_and_leaves_the_file_intact(tmp_path: Path):
    data = tmp_path / "quit.json"
    run(["align", "toggle", "1"], stdin=f"{PASSPHRASE}\n{PASSPHRASE}\n", data=data, passphrase=None)
    before = data.read_text(encoding="utf-8")
    proc = run([], stdin="1\nq\n", data=data, passphrase=None)
    assert proc.returncode == 0, combined(proc)
    assert data.read_text(encoding="utf-8") == before


def test_ctrl_d_at_a_prompt_exits_zero(data_file: Path):
    proc = run([], stdin="", data=data_file)
    assert proc.returncode == 0, combined(proc)


# --- persistence mechanics -------------------------------------------------
def test_writes_are_atomic_and_leave_no_temp_files(data_file: Path):
    run(["align", "toggle", "4"], data=data_file)
    run(["notes", "add", "--object", "M57", "--date", "2024-08-14", "--rating", "5", "--body", "ring crisp"], data=data_file)
    leftovers = [p.name for p in data_file.parent.iterdir() if p.name.startswith(".vanealign-")]
    assert leftovers == []
    assert read_store(data_file)["notes"][0]["object_name"] == "M57"


def test_json_state_contract(data_file: Path):
    run(["eyepieces", "add", "--label", "25mm Plossl", "--fl", "25", "--afov", "50"], data=data_file)
    proc = run(["state", "--json"], data=data_file)
    assert proc.returncode == 0, combined(proc)
    payload = json.loads(proc.stdout)
    assert payload["exists"] is True
    assert payload["passphrase_set"] is True
    assert payload["telescope_focal_length_mm"] == 1200
    assert payload["eyepiece_count"] == 1
    assert payload["note_count"] == 0
    assert payload["checklist_total"] == 7


def test_json_align_and_notes_payloads(data_file: Path):
    run(["align", "toggle", "1"], data=data_file)
    run(["notes", "add", "--object", "M42", "--date", "2024-11-02", "--rating", "4", "--body", "steady"], data=data_file)
    align = json.loads(run(["align", "--json"], data=data_file).stdout)
    assert align["done"] == 1 and align["total"] == 7
    assert align["steps"][0]["completed"] is True
    notes = json.loads(run(["notes", "--json"], data=data_file).stdout)
    assert notes["notes"][0]["object_name"] == "M42"


# --- recorded demo ---------------------------------------------------------
def test_recorded_demo_transcript_is_present_and_shows_persistence():
    transcript = ROOT / "demo" / "session.txt"
    assert transcript.is_file(), "demo/session.txt must be committed"
    text = transcript.read_text(encoding="utf-8")
    for label in ALIGNMENT_LABELS:
        assert label in text
    assert "[x] 3." in text and "[x] 5." in text
    assert "2 of 7 steps done" in text
    assert "25mm Plossl" in text and "M42" in text

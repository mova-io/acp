"""R1: a verified change keeps WHERE it is, when its producer knew — and says "unknown" otherwise.

Contract (every reader: get_remediation_diffs, list_remediation_diffs, remediation_diff_page,
get_remediation_evidence):
  locator:         str | None  — the exact target the writer resolved; never truncated
  page:            int | None  — a real one-based page; only a producer that read one sets it
  location_source: "recorded" | "legacy_note" | None
      "recorded"     stored in the v59 columns by the producer;
      "legacy_note"  reconstructed at read time from exact durable evidence written before the
                     columns existed — a writer note "<prefix> · <locator>" that was not cut at
                     the 500-character cap. Never stored back as a recorded location;
      None           unknown (locator and page both None).

Synthetic fixtures only.
"""
from __future__ import annotations

import sqlite3
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "api"))

SCAN, FILE = "s-location", "deck.pptx"


@pytest.fixture()
def st(monkeypatch):
    import store as store_mod
    monkeypatch.setattr(store_mod, "_SQLITE_PATH", Path(tempfile.mkdtemp()) / "diff.db")
    s = store_mod.Store()
    s.init_scan_run(SCAN, "drive", 1, "t0", "rubric", "hash")
    return s


def _readers(st):
    """The same rows through every reader, keyed so they can be compared."""
    got = {"get": st.get_remediation_diffs(SCAN, FILE),
           "list": st.list_remediation_diffs(SCAN),
           "page": st.remediation_diff_page(SCAN)["items"]}
    evidence = next((e for e in st.get_remediation_evidence(SCAN) if e["file"] == FILE), {})
    got["evidence"] = evidence.get("applied", [])
    return got


def _where(rows):
    return [(r["locator"], r["page"], r["location_source"]) for r in rows]


def test_recorded_locator_and_page_come_back_from_every_reader(st):
    long_locator = "https://example.invalid/" + "x" * 3000       # never truncated
    st.record_remediation_diffs(SCAN, FILE, [
        {"rule_id": "1.1.1", "before": "", "after": "A", "note": "n", "locator": "image 1"},
        {"rule_id": "1.1.1", "before": "", "after": "B", "note": "n",
         "locator": "pdf:fig:3:1", "page": 3},
        {"rule_id": "2.4.4", "before": "x", "after": "C", "note": "n", "locator": long_locator},
    ])
    expected = [("image 1", None, "recorded"), ("pdf:fig:3:1", 3, "recorded"),
                (long_locator, None, "recorded")]
    for name, rows in _readers(st).items():
        assert _where(rows) == expected, name


def test_unknown_location_is_none_everywhere_never_a_guess(st):
    st.record_remediation_diffs(SCAN, FILE, [
        {"rule_id": "1.3.1", "before": "a", "after": "b", "note": "automatic heading fix"}])
    for name, rows in _readers(st).items():
        assert _where(rows) == [(None, None, None)], name


@pytest.mark.parametrize("page", [0, -1, True, False, "3", 2.0, None])
def test_only_a_positive_int_is_a_page(st, page):
    st.record_remediation_diffs(SCAN, FILE, [
        {"rule_id": "1.1.1", "before": "", "after": "A", "note": "n", "page": page}])
    assert _where(st.get_remediation_diffs(SCAN, FILE)) == [(None, None, None)]


@pytest.mark.parametrize("locator", ["", "   ", 7, None, ["image 1"]])
def test_only_a_non_blank_string_is_a_locator(st, locator):
    st.record_remediation_diffs(SCAN, FILE, [
        {"rule_id": "1.1.1", "before": "", "after": "A", "note": "n", "locator": locator}])
    assert _where(st.get_remediation_diffs(SCAN, FILE)) == [(None, None, None)]


def test_legacy_reviewer_note_is_a_flagged_read_time_fallback(st):
    """The writer's own note, including a locator with a space in it ('image 1', a docPr name) —
    the shape a `\\S+` pattern silently dropped."""
    st.record_remediation_diffs(SCAN, FILE, [
        {"rule_id": "1.4.5", "before": "", "after": "T", "note": "approved by a reviewer · image 1"},
        {"rule_id": "1.1.1", "before": "", "after": "D",
         "note": "AI applied; exact saved copy subsequently verified · ppt/slides/slide1.xml#Picture 2"},
        {"rule_id": "1.1.1", "before": "", "after": "E", "note": "approved by a reviewer · "},
        {"rule_id": "1.1.1", "before": "", "after": "F", "note": "something approved by a reviewer · x"},
    ])
    for name, rows in _readers(st).items():
        assert sorted(_where(rows), key=str) == sorted([
            ("image 1", None, "legacy_note"),
            ("ppt/slides/slide1.xml#Picture 2", None, "legacy_note"),
            (None, None, None), (None, None, None)], key=str), name


def test_a_note_at_the_storage_cap_is_not_trusted(st):
    """record_remediation_diffs cuts notes at 500 characters; a cut locator names a different
    target, so a note that reached the cap yields no location at all."""
    note = "approved by a reviewer · " + "p" * 600
    st.record_remediation_diffs(SCAN, FILE, [
        {"rule_id": "1.1.1", "before": "", "after": "A", "note": note}])
    row = st.get_remediation_diffs(SCAN, FILE)[0]
    assert len(row["note"]) == 500
    assert _where([row]) == [(None, None, None)]


def test_replacement_keeps_recorded_locations_and_never_promotes_a_fallback(st):
    """Every replacement path (handlers commit_credit / office retry, unverified_changes
    re-verify) re-records get_remediation_diffs' rows. A recorded location survives; a location
    reconstructed from a note stays a reconstruction."""
    st.record_remediation_diffs(SCAN, FILE, [
        {"rule_id": "1.1.1", "before": "", "after": "A", "note": "n", "locator": "pdf:fig:2:1", "page": 2},
        {"rule_id": "1.4.5", "before": "", "after": "T", "note": "approved by a reviewer · image 4"}])
    existing = st.get_remediation_diffs(SCAN, FILE)
    st.record_remediation_diffs(SCAN, FILE, existing + [
        {"rule_id": "2.4.4", "before": "x", "after": "y", "note": "n", "locator": "https://example.invalid/b"}])
    assert sorted(_where(st.get_remediation_diffs(SCAN, FILE)), key=str) == sorted([
        ("pdf:fig:2:1", 2, "recorded"), ("image 4", None, "legacy_note"),
        ("https://example.invalid/b", None, "recorded")], key=str)
    with st._db.cursor() as cur:
        st._db.execute(cur, "SELECT locator FROM remediation_diff WHERE scan_id=%s AND rule_id=%s",
                       (SCAN, "1.4.5"))
        assert st._db.fetchone(cur)["locator"] is None     # still unstored; the note carries it


def test_an_entry_marked_as_a_fallback_stores_no_location(st):
    """Defence in depth for replacement callers: whatever a fallback row carries, only an entry
    that is absent/"recorded" may put a location into the columns."""
    st.record_remediation_diffs(SCAN, FILE, [
        {"rule_id": "1.1.1", "before": "", "after": "A", "note": "n",
         "locator": "image 9", "page": 9, "location_source": "legacy_note"}])
    assert _where(st.get_remediation_diffs(SCAN, FILE)) == [(None, None, None)]


def test_pre_v59_sqlite_database_gains_the_columns_and_its_rows_read_unknown(monkeypatch):
    """The existing migration mechanism (additive ALTER ... ADD COLUMN IF NOT EXISTS, translated
    for SQLite by _SQLiteAdapter.init_schema) upgrades a database created before v59 in place."""
    import store as store_mod
    path = Path(tempfile.mkdtemp()) / "pre_v59.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE remediation_diff (scan_id TEXT, file TEXT, rule_id TEXT, seq INT, "
                 "before TEXT, after TEXT, note TEXT, PRIMARY KEY (scan_id, file, rule_id, seq))")
    conn.execute("INSERT INTO remediation_diff VALUES (?,?,?,?,?,?,?)",
                 (SCAN, FILE, "1.1.1", 0, "", "old", "approved by a reviewer · image 2"))
    conn.execute("INSERT INTO remediation_diff VALUES (?,?,?,?,?,?,?)",
                 (SCAN, FILE, "1.3.1", 0, "a", "b", "automatic fix"))
    conn.commit()
    conn.close()
    monkeypatch.setattr(store_mod, "_SQLITE_PATH", path)
    st = store_mod.Store()
    cols = {r[1] for r in sqlite3.connect(path).execute("PRAGMA table_info(remediation_diff)")}
    assert {"locator", "page"} <= cols
    assert sorted(_where(st.get_remediation_diffs(SCAN, FILE)), key=str) == sorted([
        ("image 2", None, "legacy_note"), (None, None, None)], key=str)
    store_mod.Store()                                     # a second boot is a no-op, not an error


def test_the_schema_version_names_the_location_columns():
    import store as store_mod
    assert store_mod._PgAdapter._SCHEMA_VERSION >= 59
    ddl = " ".join(store_mod._SCHEMA)
    assert "ALTER TABLE remediation_diff ADD COLUMN IF NOT EXISTS locator TEXT" in ddl
    assert "ALTER TABLE remediation_diff ADD COLUMN IF NOT EXISTS page INT" in ddl


# ── the handler producers: commit_credit passes the writer's own locator (and PDF page) ──────────

@pytest.fixture()
def writer_env(st, monkeypatch):
    import core
    import handlers
    from proposals import Verification
    monkeypatch.setattr(core, "store", st)
    monkeypatch.setattr(core, "fire_webhook", lambda *a, **k: None)
    monkeypatch.setitem(sys.modules, "blob", SimpleNamespace(
        download_remediated=lambda *a: b"synthetic-local-only",
        upload_immutable_retry=lambda *a: "https://blob.invalid/copy"))
    monkeypatch.setattr(handlers, "_verify_residual", lambda *a, **kw: Verification(True, ()))
    monkeypatch.setattr("output_provenance.stamp_output", lambda data, name: data)
    return SimpleNamespace(st=st, handlers=handlers, monkeypatch=monkeypatch)


def _approve(st, file, rule_id, locator, value):
    item_id = st.enqueue_proposals(SCAN, file, rule_id, [
        {"locator": locator, "proposed_value": value, "before": "", "rationale": "draft"}])
    st.complete_hitl_decision(item_id, "approved", None, None, resolution=None,
                              approved_values=None, actor="reviewer@example.com", detail=None)
    return item_id


def test_commit_credit_records_the_written_locator_without_a_page_for_office(writer_env):
    st, env = writer_env.st, writer_env
    _approve(st, FILE, "1.1.1", "ppt/slides/slide1.xml#Picture 1", "A synthetic chart")
    env.monkeypatch.setattr("apply_alt.apply_alt_text", lambda data, values, **kw: (
        data + b"-w", [{"locator": k, "before": "", "after": v} for k, v in values.items()], []))
    env.monkeypatch.setattr("office_alt_integrity.verify_alt_write", lambda *a, **k: True)
    env.monkeypatch.setattr("office_verified_retry.contradicted_captions", lambda *a, **k: [])
    env.handlers._apply_approved_values({"scan_id": SCAN, "file": FILE}, {"id": "j1"})
    [row] = st.get_remediation_diffs(SCAN, FILE)
    assert (row["locator"], row["page"], row["location_source"]) == (
        "ppt/slides/slide1.xml#Picture 1", None, "recorded")
    assert row["note"] == "approved by a reviewer · ppt/slides/slide1.xml#Picture 1"


def test_commit_credit_records_a_page_only_when_the_pdf_writer_read_one(writer_env):
    st, env = writer_env.st, writer_env
    _approve(st, "form.pdf", "4.1.2", "pdf:field:7", "Synthetic field")
    _approve(st, "form.pdf", "1.1.1", "pdf:fig:5:1", "Synthetic figure")

    def pdf_writer(data, values):
        # The figure's writer read its page off the edited object; the field's did not.
        return data + b"-w", [{"locator": k, "before": "", "after": v,
                               **({"page": 5} if k.startswith("pdf:fig:") else {})}
                              for k, v in values.items()], []
    env.monkeypatch.setattr("remediate_pdf.apply_pdf_approved", pdf_writer)
    env.monkeypatch.setattr("unverified_changes.structurally_readable", lambda *a, **k: True)
    env.handlers._apply_approved_values({"scan_id": SCAN, "file": "form.pdf"}, {"id": "j1"})
    got = {r["locator"]: (r["page"], r["location_source"])
           for r in st.get_remediation_diffs(SCAN, "form.pdf")}
    assert got == {"pdf:fig:5:1": (5, "recorded"), "pdf:field:7": (None, "recorded")}

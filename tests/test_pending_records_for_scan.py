"""R-B4: pending_records_for_scan == pending_records per file, in a bounded number of reads.

The scan-wide reader exists so the report index does not make four reads per document. It is only
worth having if it answers EXACTLY what the per-file function answers, so the fixture below gives
each of 50 files one of the states pending_records distinguishes (and asserts the fixture really
reaches that state), then compares the two readers file by file.

Synthetic rows only, written straight into a throwaway SQLite store.
"""
from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "api"))

import unverified_changes as uc  # noqa: E402

SCAN = "scan-rb4"
CURRENT = "c" * 64
OLDER = "o" * 64
SOURCE = "s" * 64


@pytest.fixture
def store(monkeypatch, tmp_path):
    import store as st
    monkeypatch.setattr(st, "_SQLITE_PATH", tmp_path / "rb4.db")
    return st.Store()


class Seed:
    def __init__(self, store):
        self.store, self.n = store, 0

    def _id(self, prefix):
        self.n += 1
        return f"{prefix}-{self.n}"

    def sql(self, statement, params):
        with self.store._db.cursor() as cur:
            self.store._db.execute(cur, statement, params)

    def record(self, file, digest=CURRENT, scan=SCAN):
        self.sql("INSERT INTO file_records(scan_id,file,corrected_sha256) VALUES(%s,%s,%s)", (scan, file, digest))

    def log(self, file, action, detail, ts, scan=SCAN, raw=False):
        event = self._id("ev")
        self.sql("INSERT INTO decision_log(id,ts,actor,action,scan_id,file,rule_id,detail) "
                 "VALUES(%s,%s,'system',%s,%s,%s,'1.1.1',%s)",
                 (event, ts, action, scan, file, detail if raw else json.dumps(detail)))
        return event

    def saved(self, file, ts, *, artifact=CURRENT, semantic=False, revision=None, source=SOURCE,
              changes=None, item="item"):
        return self.log(file, "apply.saved_unverified", {
            "artifact_sha256": artifact, "source_sha256": source, "item_ids": [item],
            "changes": changes if changes is not None else [{"locator": "img#1", "before": "", "after": "A"}],
            "requires_semantic_review": semantic, "assessment_revision": revision,
            "verification": "not_verified"}, ts)

    def edge(self, file, before, after, *, revision="rev"):
        self.sql("INSERT INTO ai_validation_outcomes(id,scan_id,file,rule_id,outcome,source_revision,"
                 "actual_source_sha256,artifact_sha256) VALUES(%s,%s,%s,'1.1.1','verified_cleared',%s,%s,%s)",
                 (self._id("edge"), SCAN, file, revision, before, after))

    def confirmation(self, file, created_at, *, value_match=True, item="item"):
        approval = self._id("hitl")
        self.sql("INSERT INTO hitl_events(id,scan_id,file,rule_id,item_id,action,created_at,"
                 "approved_value_sha256,proposal_snapshot_ids) VALUES(%s,%s,%s,'1.1.1',%s,'approve',%s,'v',%s)",
                 (approval, SCAN, file, item, created_at, json.dumps(["snap"])))
        self.sql("INSERT INTO ai_validation_outcomes(id,scan_id,file,rule_id,item_id,outcome,artifact_sha256,"
                 "approval_event_id,proposal_snapshot_id,actual_approved_value_sha256,created_at) "
                 "VALUES(%s,%s,%s,'1.1.1',%s,'verified_cleared',%s,%s,'snap',%s,%s)",
                 (self._id("out"), SCAN, file, item, CURRENT, approval, "v" if value_match else "x", created_at))

    def retry(self, file, *, locator="img#1", caption="A", valid=True, item="item"):
        self.log(file, "office_retry.saved", {
            "artifact_sha256": CURRENT, "replacement_sha256": CURRENT, "previous_artifact_sha256": OLDER,
            "replacement_validation": {"approved": True},
            "verification": "independent_caption_and_actual_reassessment",
            "original_outcome": "superseded_not_verified", "operation_id": "op",
            "approval_identity": "standing-caption-retry:op" if valid else "someone-else",
            "source_revision": "rev", "item_id": item, "locator": locator, "original_caption": caption},
            "2026-09-01T00:00:09Z")


T1, T2 = "2026-09-01T00:00:01Z", "2026-09-01T00:00:02Z"
TWO = [{"locator": "img#1", "before": "", "after": "A"}, {"locator": "img#2", "before": "", "after": "B"}]


def _state(seed, kind, f):
    """Write one state for file f; returns the expected (count, changes-per-record) signature."""
    if kind == "no_digest":
        seed.record(f, digest=None); return 0, []
    seed.record(f)
    if kind == "no_rows":
        return 0, []
    if kind == "pending":
        seed.saved(f, T1); return 1, [1]
    if kind == "reverified":
        event = seed.saved(f, T1)
        seed.log(f, "apply.reverified", {"source_event_id": event, "artifact_sha256": CURRENT}, T2); return 0, []
    if kind == "reverified_other_artifact":
        event = seed.saved(f, T1)
        seed.log(f, "apply.reverified", {"source_event_id": event, "artifact_sha256": "z" * 64}, T2); return 1, [1]
    if kind == "older_plain":
        seed.saved(f, T1, artifact=OLDER); return 0, []
    if kind == "semantic_outcome_edge":
        seed.saved(f, T1, artifact=OLDER, semantic=True, revision="rev"); seed.edge(f, OLDER, CURRENT); return 1, [1]
    if kind == "semantic_null_revision_edge":
        seed.saved(f, T1, artifact=OLDER, semantic=True, revision="rev"); seed.edge(f, OLDER, CURRENT, revision=None)
        return 0, []
    if kind == "semantic_writer_chain":
        seed.saved(f, T1, artifact=OLDER, semantic=True, revision="rev")
        seed.saved(f, T2, artifact=CURRENT, source=OLDER, revision="rev"); return 2, [1, 1]
    if kind == "semantic_confirmed":
        seed.saved(f, T1, semantic=True, revision="rev"); seed.confirmation(f, T2); return 0, []
    if kind == "semantic_confirmed_too_early":
        seed.saved(f, T2, semantic=True, revision="rev"); seed.confirmation(f, T1); return 1, [1]
    if kind == "semantic_confirmation_value_mismatch":
        seed.saved(f, T1, semantic=True, revision="rev"); seed.confirmation(f, T2, value_match=False); return 1, [1]
    if kind == "retry_supersedes_all":
        seed.saved(f, T1, revision="rev"); seed.retry(f); return 0, []
    if kind == "retry_supersedes_one":
        seed.saved(f, T1, revision="rev", changes=TWO); seed.retry(f); return 1, [1]
    if kind == "retry_invalid":
        seed.saved(f, T1, revision="rev", changes=TWO); seed.retry(f, valid=False); return 1, [2]
    raise AssertionError(kind)


KINDS = ["no_digest", "no_rows", "pending", "reverified", "reverified_other_artifact", "older_plain",
         "semantic_outcome_edge", "semantic_null_revision_edge", "semantic_writer_chain", "semantic_confirmed",
         "semantic_confirmed_too_early", "semantic_confirmation_value_mismatch", "retry_supersedes_all",
         "retry_supersedes_one", "retry_invalid"]


def _scan(store, n=50):
    seed = Seed(store)
    expected = {}
    for i in range(n - 1):
        f = f"doc-{i:02d}.docx"
        expected[f] = _state(seed, KINDS[i % len(KINDS)], f)
    malformed = "doc-malformed.docx"
    seed.record(malformed)
    seed.log(malformed, "apply.saved_unverified", "{not json", T1, raw=True)
    # A same-named file in ANOTHER scan must not leak into this one.
    seed.record("doc-01.docx", scan="another-scan")
    seed.saved("doc-01.docx", T1)
    with store._db.cursor() as cur:
        store._db.execute(cur, "UPDATE decision_log SET scan_id='another-scan' WHERE id=%s", (f"ev-{seed.n}",))
    return expected, malformed


def _count_reads(store, monkeypatch):
    calls = []
    real = store._db.execute
    monkeypatch.setattr(store._db, "execute", lambda cur, sql, params=(): (calls.append(sql), real(cur, sql, params))[1])
    return calls


def test_scan_wide_equals_per_file_for_every_state(store):
    expected, malformed = _scan(store)
    files = [*expected, malformed, "never-recorded.docx"]
    errors = {}
    wide = uc.pending_records_for_scan(store, SCAN, files, errors=errors)
    assert set(wide) == set(files) - {malformed}
    assert isinstance(errors[malformed], ValueError)
    with pytest.raises(ValueError):
        uc.pending_records(store, SCAN, malformed)
    for f in [*expected, "never-recorded.docx"]:
        assert wide[f] == uc.pending_records(store, SCAN, f), f
    # The fixture really reaches each state: the per-file answers are what the state predicts.
    for f, (count, per_record) in expected.items():
        assert len(wide[f]) == count, f
        assert [len(r["changes"]) for r in wide[f]] == per_record, f
    assert wide["never-recorded.docx"] == []


def test_scan_wide_without_a_file_list_covers_every_record_of_the_scan(store):
    expected, malformed = _scan(store)
    errors = {}
    wide = uc.pending_records_for_scan(store, SCAN, errors=errors)
    assert set(wide) == set(expected) and set(errors) == {malformed}
    assert all(wide[f] == uc.pending_records(store, SCAN, f) for f in expected)


def test_without_errors_a_malformed_file_raises_as_it_does_per_file(store):
    _scan(store)
    with pytest.raises(ValueError):
        uc.pending_records_for_scan(store, SCAN)


def test_reads_are_bounded_by_chunks_not_by_files(store, monkeypatch):
    expected, malformed = _scan(store)
    active = sum(1 for f in expected if _has_digest(store, f)) + 1          # + the malformed file
    per_file = {f: uc.pending_records(store, SCAN, f) for f in expected}
    calls = _count_reads(store, monkeypatch)
    uc.pending_records(store, SCAN, "doc-02.docx")
    assert len(calls) == 5                        # for contrast: five reads for ONE file per file
    calls.clear()
    uc.pending_records_for_scan(store, SCAN, errors={})
    assert len(calls) == 1 + 4                                              # 50 files, one chunk
    calls.clear()
    monkeypatch.setattr(uc, "_FILE_CHUNK", 7)
    files = [*expected, malformed]
    wide = uc.pending_records_for_scan(store, SCAN, files, errors={})
    assert len(calls) == math.ceil(len(files) / 7) + 4 * math.ceil(active / 7)
    assert max(len(sql.split("%s")) - 1 for sql in calls) <= 8              # scan_id + 7 names
    assert {f: wide[f] for f in expected} == per_file                      # chunk seams change nothing


def _has_digest(store, f):
    return bool((store.get_file_record(SCAN, f) or {}).get("corrected_sha256"))

"""R-B2 — the assessment a re-assessment INSIDE one scan replaced (api/report_history.py).

save_file_result upserts the file row and then deletes the file's issue_records before
re-inserting them, so the only baseline a same-scan comparison could use is destroyed by the write
that makes the comparison interesting. Copying the outgoing issue rows is not enough on its own —
the parent review's point — because:

  * a successful ZERO-finding assessment has no rows to copy, and "no rows" must still read as
    "zero findings", not as "nothing recorded";
  * a failed assessment also has no rows, and must read as "unavailable", not as zero;
  * the scan row's rubric and scope can be re-initialised between the two writes, so rebuilding
    the old assessment's envelope from the scan row at replacement time claims a comparability
    nobody established.

So each write records its own envelope (fields, digests, rubric, scope) when it is WRITTEN, and a
replacement copies the rows it is about to delete in the same transaction.

UNTIL THE STORE HOOKS LAND (/tmp/acp-vr-requests.md, 'G -> D'), the fixture below calls the two
hook functions at exactly the statements D's hooks sit next to: capture_outgoing just before the
file_records upsert, record_assessment_write just before `DELETE FROM issue_records`, on the same
cursor. Once save_file_result carries the hooks natively the shim switches itself off and the SAME
tests run against the real method; test_store_methods_* are skipped until then.
"""
from __future__ import annotations

import inspect
import json
import sys
from pathlib import Path

import pytest

ACP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ACP / "api"))

import report_history as rh   # noqa: E402
import store as store_mod     # noqa: E402

OWNER = "owner@example.test"
OTHER = "someone-else@example.test"
SID = "s-same-scan"
FILE = "report.docx"

NATIVE = "report_history" in inspect.getsource(store_mod.Store.save_file_result)
HOOKS_LANDED = NATIVE and hasattr(store_mod.Store, "prior_assessment_in_scan")
HOOK_REASON = ("store.py hooks from /tmp/acp-vr-requests.md 'G -> D' (R-B2) have not landed: "
               "save_file_result capture + Store.prior_assessment_in_scan")


@pytest.fixture()
def s(isolated_store, monkeypatch):
    if NATIVE:
        return isolated_store
    rh.ensure_schema(isolated_store)
    state: dict = {}
    original_save = store_mod.Store.save_file_result

    def save_file_result(self, scan_id, f, completed_at, *, job=None):
        scope = self.scope_for_file(scan_id, f["file"], self.get_scan_scope(scan_id))
        state["call"] = (scan_id, f, completed_at, job, scope)
        state["outgoing"] = None
        try:
            return original_save(self, scan_id, f, completed_at, job=job)
        finally:
            state.pop("call", None)

    db = isolated_store._db
    original_execute = db.execute

    def execute(cur, sql, params=()):
        call = state.get("call")
        if call and sql.startswith("INSERT INTO file_records(") and "written_job" in sql:
            state["outgoing"] = rh.capture_outgoing(isolated_store, cur, call[0], call[1]["file"])
        elif call and sql.startswith("DELETE FROM issue_records WHERE scan_id=%s AND file=%s"):
            rh.record_assessment_write(isolated_store, cur, call[0], call[1], call[2],
                                       state["outgoing"], file_scope=call[4], job=call[3])
        return original_execute(cur, sql, params)

    monkeypatch.setattr(store_mod.Store, "save_file_result", save_file_result)
    monkeypatch.setattr(db, "execute", execute)
    return isolated_store


def prior(store, scan_id=SID, file=FILE, owner=OWNER):
    if HOOKS_LANDED:
        return store.prior_assessment_in_scan(scan_id, file, owner=owner)
    return rh.prior_assessment_in_scan(store, scan_id, file, owner=owner)


def _init(store, *, sid=SID, owner=OWNER, rubric="h1", scope=None, source="gdrive"):
    store.init_scan_run(sid, source, 1, "2026-09-01T00:00:00+00:00", "wcag-aa", rubric,
                        owner=owner, scope=scope)


def _issue(rule="DOCX-ALT-001", loc="docx:image:1", page=1, detail="Image has no description"):
    return {"ruleId": rule, "wcag": "1.1.1 Non-text Content", "severity": "SERIOUS",
            "detail": detail, "page": page, "location": loc}


def _doc(*, name=FILE, status="uncertain", issues=(), drive="d1", checksum="md5-1", skipped=0,
         score=70):
    return {"file": name, "engine": ".net/office", "status": status, "score": score,
            "compliant": 0, "skipped_rules": skipped, "issues": list(issues),
            "drive_file_id": drive, "checksum": checksum}


def _write(store, when, sid=SID, **kw):
    assert store.save_file_result(sid, _doc(**kw), when) is True


def _rows(store, table):
    with store._db.cursor() as cur:
        store._db.execute(cur, f"SELECT * FROM {table}")
        return store._db.fetchall(cur)


# ── zero findings, then a new problem ─────────────────────────────────────────────────────

def test_a_zero_finding_baseline_survives_as_zero_not_as_missing(s):
    _init(s)
    _write(s, "2026-09-01T01:00:00+00:00", status="certifiable", issues=[])
    _write(s, "2026-09-01T02:00:00+00:00", issues=[_issue()])
    got = prior(s)
    snap = got["snapshot"]
    assert got["issues"] == []
    assert snap["issues_state"] == "recorded"
    assert snap["zero_findings"] is True and snap["issue_count"] == 0
    assert snap["assessment_outcome"] == "assessed"
    assert got["file_row"]["status"] == "certifiable"
    assert got["run"]["assessed_at"] == "2026-09-01T01:00:00+00:00"
    # The current assessment is the new problem, and it is untouched by the capture.
    current = s.get_scan(SID, owner=OWNER)["files"][0]
    assert [i["rule_id"] for i in current["issues"]] == ["DOCX-ALT-001"]


def test_first_write_has_no_prior(s):
    _init(s)
    _write(s, "2026-09-01T01:00:00+00:00", issues=[_issue()])
    assert prior(s) is None


# ── an earlier failed assessment ──────────────────────────────────────────────────────────

def test_a_failed_assessment_is_unavailable_never_zero(s):
    _init(s)
    _write(s, "2026-09-01T01:00:00+00:00", status="error", issues=[], score=None)
    _write(s, "2026-09-01T02:00:00+00:00", issues=[_issue()])
    snap = prior(s)["snapshot"]
    assert snap["assessment_outcome"] == "failed"
    assert snap["issues_state"] == "unavailable"
    assert snap["zero_findings"] is False and snap["issue_count"] is None
    assert snap["completeness"] == "not_assessed"


def test_a_partial_assessment_says_partial(s):
    _init(s)
    _write(s, "2026-09-01T01:00:00+00:00", issues=[], skipped=2)
    _write(s, "2026-09-01T02:00:00+00:00", issues=[_issue()])
    snap = prior(s)["snapshot"]
    assert snap["completeness"] == "partial" and snap["zero_findings"] is True


# ── a changed rubric or scope ─────────────────────────────────────────────────────────────

def test_the_replaced_envelope_is_the_one_recorded_at_its_own_write(s):
    _init(s, rubric="h1", scope={"scan_scope": {"1.1.1": ["docx"]}})
    _write(s, "2026-09-01T01:00:00+00:00", issues=[_issue()])
    # The scan is re-initialised under another rubric and scope before the re-assessment.
    _init(s, rubric="h2", scope={"scan_scope": {"1.4.3": ["docx"]}})
    _write(s, "2026-09-01T02:00:00+00:00", issues=[])
    got = prior(s)
    assert got["run"]["rubric_hash"] == "h1"
    assert got["run"]["scan_scope"] == {"1.1.1": ["docx"]}
    basis = got["snapshot"]["comparison_basis"]
    assert basis["rubric"] == "different" and basis["scope"] == "different"
    # file_scope is the per-file scope each write ACTUALLY used (save_file_result's `scope`).
    # Store.get_scan_scope caches per process, so a re-init in the same process can leave it
    # unchanged; the basis reports what was used, not what the scan row now says.
    assert basis["file_scope"] in ("same", "different")
    # ...while the scan row itself now says h2: the envelope was not rebuilt from it.
    assert [r["rubric_hash"] for r in _rows(s, "scan_runs")] == ["h2"]


def test_an_unchanged_rubric_and_scope_compare_as_same(s):
    _init(s, scope={"scan_scope": {"1.1.1": ["docx"]}})
    _write(s, "2026-09-01T01:00:00+00:00", issues=[_issue()])
    _write(s, "2026-09-01T02:00:00+00:00", issues=[])
    assert prior(s)["snapshot"]["comparison_basis"] == {
        "rubric": "same", "scope": "same", "file_scope": "same"}


def test_an_assessment_written_before_history_existed_claims_no_context(s):
    """The request's own case — save_scan with A,B; save_file_result with A — plus the parent's
    rule: its rubric/scope were never recorded, so none is reported, even though the scan row
    has one to offer."""
    s.save_scan({"_scan_id": SID, "started_at": "2026-09-01T00:00:00+00:00",
                 "completed_at": "2026-09-01T00:30:00+00:00", "source": "gdrive", "owner": OWNER,
                 "rubric": {"name": "wcag-aa", "hash": "h1"},
                 "summary": {"files": 1, "certifiable": 0, "uncertain": 1, "error": 0,
                             "avg_score": 70},
                 "files": [_doc(issues=[_issue(loc="docx:image:1"),
                                        _issue(loc="docx:image:2")])]})
    _write(s, "2026-09-01T02:00:00+00:00", issues=[_issue(loc="docx:image:1")])
    got = prior(s)
    assert [i["location"] for i in got["issues"]] == ["docx:image:1", "docx:image:2"]
    assert got["file_row"]["status"] == "uncertain"
    assert got["snapshot"]["context_source"] == "not_recorded"
    assert got["run"]["rubric_hash"] is None and got["run"]["scan_scope"] is None
    assert got["run"]["assessed_at"] is None
    assert set(got["snapshot"]["comparison_basis"].values()) == {"not_recorded"}
    assert got["snapshot"]["integrity"] == "row_at_replacement"


def test_a_row_changed_in_place_after_its_write_keeps_the_assessed_values(s):
    _init(s)
    _write(s, "2026-09-01T01:00:00+00:00", issues=[_issue()])
    with s._db.cursor() as cur:     # what a verified remediation does to the row, in place
        s._db.execute(cur, "UPDATE file_records SET compliant=1, score=100, status='pass' "
                           "WHERE scan_id=%s AND file=%s", (SID, FILE))
    _write(s, "2026-09-01T02:00:00+00:00", issues=[])
    snap = prior(s)
    assert snap["file_row"]["status"] == "uncertain" and snap["file_row"]["score"] == 70
    assert snap["snapshot"]["row_at_replacement"]["status"] == "pass"
    assert snap["snapshot"]["integrity"] == "verified;file_row_changed_after_write"


# ── rename / provider identity ────────────────────────────────────────────────────────────

def test_another_document_taking_the_path_is_not_a_prior(s):
    _init(s)
    _write(s, "2026-09-01T01:00:00+00:00", drive="d1", issues=[_issue()])
    _write(s, "2026-09-01T02:00:00+00:00", drive="d2", issues=[])
    assert prior(s) is None


def test_a_renamed_document_finds_its_prior_by_provider_id(s):
    _init(s)
    _write(s, "2026-09-01T01:00:00+00:00", name="old-name.docx", drive="d7", issues=[_issue()])
    _write(s, "2026-09-01T02:00:00+00:00", name="old-name.docx", drive="d7", issues=[])
    _write(s, "2026-09-01T03:00:00+00:00", name="new-name.docx", drive="d7", issues=[])
    got = prior(s, file="new-name.docx")
    assert got["snapshot"]["artifact"]["file"] == "old-name.docx"
    assert [i["location"] for i in got["issues"]] == ["docx:image:1"]


def test_a_path_only_document_matches_only_id_less_history(s):
    _init(s)
    _write(s, "2026-09-01T01:00:00+00:00", drive=None, issues=[_issue()])
    _write(s, "2026-09-01T02:00:00+00:00", drive=None, issues=[])
    assert prior(s)["snapshot"]["artifact"]["drive_file_id"] is None
    _write(s, "2026-09-01T03:00:00+00:00", drive="d9", issues=[])
    assert prior(s) is None      # the path now names a provider document; history had no id


# ── owner isolation ───────────────────────────────────────────────────────────────────────

def test_another_owner_reads_nothing(s):
    _init(s)
    _write(s, "2026-09-01T01:00:00+00:00", issues=[_issue()])
    _write(s, "2026-09-01T02:00:00+00:00", issues=[])
    assert prior(s, owner=OTHER) is None
    assert prior(s, scan_id="no-such-scan") is None
    _init(s, sid="s-other", owner=OTHER)
    _write(s, "2026-09-01T01:00:00+00:00", sid="s-other", issues=[_issue(loc="docx:image:5")])
    _write(s, "2026-09-01T02:00:00+00:00", sid="s-other", issues=[])
    assert prior(s, scan_id="s-other") is None
    assert prior(s, scan_id="s-other", owner=OTHER)["issues"][0]["location"] == "docx:image:5"


# ── repeated writes in the same scan ──────────────────────────────────────────────────────

def test_repeated_writes_keep_order_and_an_identical_retry_hides_nothing(s):
    _init(s)
    a, b, c = _issue(loc="docx:image:1"), _issue(loc="docx:image:2"), _issue(loc="docx:image:3")
    _write(s, "2026-09-01T01:00:00+00:00", issues=[a, b])
    _write(s, "2026-09-01T02:00:00+00:00", issues=[a])
    first = prior(s)
    assert [i["location"] for i in first["issues"]] == ["docx:image:1", "docx:image:2"]
    _write(s, "2026-09-01T02:05:00+00:00", issues=[a])     # a retried job, same result
    assert prior(s)["snapshot"]["history_id"] == first["snapshot"]["history_id"]
    assert prior(s)["snapshot"]["history_total"] == 1
    _write(s, "2026-09-01T03:00:00+00:00", issues=[a, c])
    latest = prior(s)
    assert [i["location"] for i in latest["issues"]] == ["docx:image:1"]
    assert latest["snapshot"]["history_total"] == 2
    assert latest["snapshot"]["seq"] > first["snapshot"]["seq"]
    current = [r for r in _rows(s, "file_assessment_history") if r["state"] == "current"]
    assert len(current) == 1


def test_a_refused_stale_write_records_nothing(s):
    _init(s)
    assert s.save_file_result(SID, _doc(issues=[_issue()]), "2026-09-01T01:00:00+00:00",
                              job={"id": "j1", "attempts": 2}) is True
    before = _rows(s, "file_assessment_history")
    assert s.save_file_result(SID, _doc(issues=[]), "2026-09-01T02:00:00+00:00",
                              job={"id": "j1", "attempts": 1}) is False
    assert _rows(s, "file_assessment_history") == before
    assert prior(s) is None


def test_the_capture_is_atomic_with_the_replacement(s, monkeypatch):
    """A failure while recording aborts the whole write: the old findings are not deleted with
    no copy of them left behind, and no half-written history row survives."""
    _init(s)
    _write(s, "2026-09-01T01:00:00+00:00", issues=[_issue()])
    history = _rows(s, "file_assessment_history")

    def boom(*a, **k):
        raise RuntimeError("history write failed")
    monkeypatch.setattr(rh, "_next_seq", boom)
    with pytest.raises(RuntimeError):
        s.save_file_result(SID, _doc(issues=[]), "2026-09-01T02:00:00+00:00")
    assert _rows(s, "file_assessment_history") == history
    assert [i["location"] for i in _rows(s, "issue_records")] == ["docx:image:1"]
    assert _rows(s, "file_records")[0]["status"] == "uncertain"


def test_rule_manifest_of_the_replaced_assessment_is_kept(s):
    _init(s)
    _write(s, "2026-09-01T01:00:00+00:00", issues=[_issue()])
    _write(s, "2026-09-01T02:00:00+00:00", issues=[])
    manifest = prior(s)["snapshot"]["rule_manifest"]
    assert manifest, "the per-rule manifest of the replaced assessment was not captured"
    by_rule = {m["rule_id"]: m for m in manifest}
    assert by_rule["DOCX-ALT-001"]["status"] == "FAIL"
    stored = json.loads([r for r in _rows(s, "file_assessment_history")
                         if r["state"] == "superseded"][0]["issues"])
    assert stored[0]["rule_id"] == "DOCX-ALT-001"


# ── through the Store, once D has pasted the hooks ────────────────────────────────────────

@pytest.mark.skipif(not HOOKS_LANDED, reason=HOOK_REASON)
def test_store_methods_capture_natively(isolated_store):
    s = isolated_store
    _init(s)
    _write(s, "2026-09-01T01:00:00+00:00", status="certifiable", issues=[])
    _write(s, "2026-09-01T02:00:00+00:00", issues=[_issue()])
    got = s.prior_assessment_in_scan(SID, FILE, owner=OWNER)
    assert got == rh.prior_assessment_in_scan(s, SID, FILE, owner=OWNER)
    assert got["snapshot"]["zero_findings"] is True
    assert s.prior_assessment_in_scan(SID, FILE, owner=OTHER) is None


@pytest.mark.skipif(not HOOKS_LANDED, reason=HOOK_REASON)
def test_store_methods_delete_history_with_the_scan(isolated_store):
    s = isolated_store
    assert "file_assessment_history" in s._RESET_USER_SCAN_TABLES
    assert "file_assessment_history" in s._ANALYTICS_TABLES
    _init(s)
    _write(s, "2026-09-01T01:00:00+00:00", issues=[_issue()])
    _write(s, "2026-09-01T02:00:00+00:00", issues=[])
    s.delete_scan(SID, OWNER)
    assert _rows(s, "file_assessment_history") == []

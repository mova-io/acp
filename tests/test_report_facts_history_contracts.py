"""R-B1 / R-B2 / R-B3 consumption — CONTRACT-SHAPE tests.

The owner's real readers are on this branch now (0f0fd520; real-reader integration is in
tests/test_report_facts_real_fixture.py and tests/test_report_facts_scan_readers.py). These tests
keep pinning the report's reading of shapes a small real store cannot easily produce (truncation,
unrecorded context, failed/partial snapshots), via stand-ins attached the two ways a reader can be
found — without ever replacing the real `report_history` module (the Store's other wrappers
import it):

* as a Store method (`store.<name>(scan_id, …)`), patched on the class; and
* module-only: the REAL module's attribute patched and the Store method removed.

Everything else — the scan, its findings, the facts builder, the digests — is the real store.
What these pin is the report's reading of each shape: a truncated decision list says "showing N
of TOTAL"; a same-scan snapshot whose context was not recorded is "not recorded", never
"different scope", and is never scope-digested; an unusable snapshot is never a baseline; the
scan's own ledger is never applied to the snapshot; and the scan index still uses the per-file
projection, so row and route digests stay equal.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ACP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ACP / "api"))
sys.path.insert(0, str(ACP / "tests"))

import report_facts as rf   # noqa: E402
from test_report_facts import _scan, _doc, _image_issue, SID, FILE, OWNER   # noqa: E402

LANG = {"ruleId": "DOCX-LANG-001", "wcag": "3.1.1 Language of Page", "severity": "SERIOUS",
        "detail": "Document language is not set", "page": None, "location": None}


def _row(issue):
    """An issue as the store's readers return it (issue_records columns)."""
    return {"rule_id": issue["ruleId"], "wcag": issue["wcag"], "severity": issue["severity"],
            "detail": issue["detail"], "page": issue["page"], "location": issue["location"]}


def _prior(issues, *, context="recorded_at_write", basis=None, outcome="assessed",
           issues_state="recorded", completeness="complete", integrity="verified", total=1,
           rules_not_checked=()):
    """R-B2's return shape (owner response), built by hand."""
    recorded = context == "recorded_at_write"
    run = {"id": SID, "same_scan_snapshot": True,
           "owner_email": OWNER if recorded else None, "source": "local" if recorded else None,
           "rubric_name": "wcag-aa" if recorded else None, "rubric_hash": "h1" if recorded else None,
           "scope": None, "scan_scope": None, "status": "completed" if recorded else None,
           "assessed_at": "2026-09-01T05:02:00+00:00" if recorded else None}
    return {
        "run": run,
        "file_row": {"scan_id": SID, "run_id": SID, "file": FILE, "engine": ".net/office",
                     "status": {"assessed": "uncertain", "failed": "error"}.get(outcome, None),
                     "score": 60, "compliant": 0, "skipped_rules": 0, "drive_file_id": None,
                     "acp_stamped": None, "checksum": "md5-source-1", "size_kb": None,
                     "pages": None, "sheets": None, "source_modified": None},
        "issues": [_row(i) for i in issues],
        "snapshot": {
            "history_id": "h-1", "seq": 1, "history_total": total, "context_source": context,
            "assessment_outcome": outcome, "issues_state": issues_state,
            "issue_count": len(issues) if issues_state == "recorded" else None,
            "zero_findings": issues_state == "recorded" and not issues,
            "completeness": completeness, "rules_not_checked": list(rules_not_checked),
            "rule_manifest": [], "written_at": "2026-09-01T05:02:00+00:00",
            "superseded_at": "2026-09-01T05:05:00+00:00",
            "superseded_by": {"job_id": "j-2", "attempt": 1},
            "artifact": {"file": FILE, "drive_file_id": None, "checksum": "md5-source-1"},
            "integrity": integrity, "row_at_replacement": None,
            "comparison_basis": basis or ({"rubric": "same", "scope": "same", "file_scope": "same"}
                                          if recorded else
                                          {"rubric": "not_recorded", "scope": "not_recorded",
                                           "file_scope": "not_recorded"}),
        },
    }


HISTORY_READERS = ("previous_assessments_for_scan", "prior_assessment_in_scan",
                   "change_reviews_for_document")


@pytest.fixture()
def no_history_module(monkeypatch, isolated_store):
    """Neither landed, AS report_facts SEES IT: the Store has no reader methods and
    report_facts' history lookup finds no module. The real report_history stays importable — the
    Store's other wrappers (and its per-file previous_assessment_for_file) still import it."""
    for name in HISTORY_READERS:
        monkeypatch.delattr(type(isolated_store), name, raising=False)
    monkeypatch.setattr(rf, "_report_history", lambda: None)


def _as_store_method(monkeypatch, store, name, fn):
    monkeypatch.setattr(type(store), name, fn, raising=False)


def _as_module(monkeypatch, store, **fns):
    """The module-only landing (report_history has the function, the Store wrapper does not):
    the stand-in is patched onto the REAL module's attribute and the Store method is removed,
    both restored by monkeypatch. The module object itself is never replaced."""
    import report_history
    for name, fn in fns.items():
        monkeypatch.delattr(type(store), name, raising=False)
        monkeypatch.setattr(report_history, name, fn)
    return report_history


CURRENT = [LANG, _image_issue("a", 1, "docx:image:1"), _image_issue("c", 3, "docx:image:3")]
REPLACED = [LANG, _image_issue("a", 1, "docx:image:1"), _image_issue("b", 2, "docx:image:2")]


# ── R-B3: earlier-scan decisions, dict shape ─────────────────────────────────────────────────

def _decision(scan_id, change_id, verdict, ts):
    return {"scan_id": scan_id, "file": FILE, "kind": f"change_review:{change_id}",
            "change_id": change_id,
            "value": json.dumps({"change_id": change_id, "verdict": verdict, "reviewer": "r@x.org",
                                 "artifact_sha256": "a" * 64}),
            "ts": ts, "scan_at": "2026-08-01T00:00:00+00:00"}


def test_rb3_store_method_truncated_result_says_showing_n_of_total(isolated_store, monkeypatch):
    _scan(isolated_store, files=[_doc(issues=CURRENT)])
    seen = {}

    def reviews(self, scan_id, file, *, owner, limit=200):
        seen.update(scan_id=scan_id, file=file, owner=owner, limit=limit)
        decisions = [_decision("s-old", f"{FILE}::1.1.1::{i}", "accepted", f"2026-08-0{i + 1}")
                     for i in range(2)]
        return {"decisions": decisions, "total": 7, "returned": 2, "limit": 2, "truncated": True}

    _as_store_method(monkeypatch, isolated_store, "change_reviews_for_document", reviews)
    facts = rf.build_file_facts(isolated_store, SID, FILE, owner=OWNER)
    assert seen == {"scan_id": SID, "file": FILE, "owner": OWNER, "limit": rf.PRIOR_DECISIONS_LIMIT}
    assert [d["changeId"] for d in facts["priorDecisions"]] == [f"{FILE}::1.1.1::0", f"{FILE}::1.1.1::1"]
    assert all(d["status"] == "not_carried_forward" for d in facts["priorDecisions"])
    assert "showing 2 of 7" in facts["priorDecisionsReason"]
    assert facts["priorDecisionsBounds"] == {"total": 7, "returned": 2, "limit": 2,
                                             "truncated": True, "unreadable": 0}


def test_rb3_through_report_history_before_the_store_wrapper(isolated_store, monkeypatch):
    _scan(isolated_store, files=[_doc(issues=CURRENT)])
    got = {}

    def reviews(store, scan_id, file, *, owner, limit=200):
        got["store"] = store
        return {"decisions": [_decision("s-old", "x", "rejected", "2026-08-01")], "total": 1,
                "returned": 1, "limit": limit, "truncated": False}

    _as_module(monkeypatch, isolated_store, change_reviews_for_document=reviews)
    facts = rf.build_file_facts(isolated_store, SID, FILE, owner=OWNER)
    assert got["store"] is isolated_store
    assert [d["verdictLabel"] for d in facts["priorDecisions"]] == ["rejected"]
    assert "showing" not in facts["priorDecisionsReason"]
    assert "not carried forward" in facts["priorDecisionsReason"]


def test_rb3_none_and_empty_are_different_answers(isolated_store, monkeypatch):
    _scan(isolated_store, files=[_doc(issues=CURRENT)])
    _as_module(monkeypatch, isolated_store, change_reviews_for_document=lambda store, *a, **k: None)
    none = rf.build_file_facts(isolated_store, SID, FILE, owner=OWNER)
    assert none["priorDecisions"] is None and none["priorDecisionsBounds"] is None
    assert "not available" in none["priorDecisionsReason"]
    _as_module(monkeypatch, isolated_store, change_reviews_for_document=lambda store, *a, **k: {
        "decisions": [], "total": 0, "returned": 0, "limit": 200, "truncated": False})
    empty = rf.build_file_facts(isolated_store, SID, FILE, owner=OWNER)
    assert empty["priorDecisions"] == []
    assert empty["priorDecisionsReason"].startswith("no decision is recorded")


def test_rb3_neither_landed_is_named_not_dropped(isolated_store, no_history_module):
    _scan(isolated_store, files=[_doc(issues=CURRENT)])
    facts = rf.build_file_facts(isolated_store, SID, FILE, owner=OWNER)
    assert facts["priorDecisions"] is None
    assert "not shown" in facts["priorDecisionsReason"]


# ── R-B2: the assessment a re-assessment replaced inside this scan ───────────────────────────

def _same_scan(store, monkeypatch, found, *, via="store"):
    if via == "store":
        _as_store_method(monkeypatch, store, "prior_assessment_in_scan",
                         lambda self, scan_id, file, *, owner: found)
    else:
        _as_module(monkeypatch, store,
                   prior_assessment_in_scan=lambda st, scan_id, file, *, owner: found)
    return rf.build_file_facts(store, SID, FILE, owner=OWNER)


@pytest.mark.parametrize("via", ["store", "module"])
def test_rb2_a_usable_snapshot_is_compared_finding_by_finding(isolated_store, monkeypatch, via):
    _scan(isolated_store, files=[_doc(issues=CURRENT)])
    # The snapshot's run must never be scope-digested (a not_recorded run would read as
    # "different scope"): fail loudly if anything tries.
    real_digest = rf.scope_digest
    monkeypatch.setattr(rf, "scope_digest", lambda run: (_ for _ in ()).throw(
        AssertionError("scope_digest on a same-scan snapshot")) if (run or {}).get(
        "same_scan_snapshot") else real_digest(run))
    facts = _same_scan(isolated_store, monkeypatch, _prior(REPLACED, total=2), via=via)
    s = facts["sameScanHistory"]
    assert s["status"] == "compared"
    by_id = {f["id"]: f for f in facts["findings"]}
    assert [by_id[i]["detail"] for i in s["introduced"]] == ["c"]
    assert [r["detail"] for r in s["resolved"]] == ["b"]
    assert [by_id[i]["detail"] for i in s["persisting"]] == ["a"]
    # the unlocated language finding is in neither list, on either side
    assert s["notComparable"] == {"current": 1, "previous": 1}
    # the scan's per-finding ledger describes the CURRENT assessment: never applied here
    assert s["reopened"] is None and "not the one it replaced" in s["reopenedReason"]
    assert s["snapshot"]["historyTotal"] == 2 and "most recent of 2" in s["reason"]
    assert s["baseline"]["sameScanSnapshot"] is True


def test_rb2_context_not_recorded_is_not_recorded_never_different_scope(isolated_store,
                                                                        monkeypatch):
    _scan(isolated_store, files=[_doc(issues=CURRENT)])
    facts = _same_scan(isolated_store, monkeypatch,
                       _prior(REPLACED, context="not_recorded", integrity="row_at_replacement"))
    s = facts["sameScanHistory"]
    assert s["status"] == "baseline_unusable" and s["reasonCode"] == "context_not_recorded"
    assert "not recorded" in s["reason"] and "different" not in s["reason"]
    assert s["introduced"] == [] and s["resolved"] == []


@pytest.mark.parametrize("kwargs, code, words", [
    # an envelope that was not recorded is unknown even if a basis field claimed "same"
    ({"context": "not_recorded", "basis": {"rubric": "same", "scope": "same", "file_scope": "same"}},
     "context_not_recorded", "rubric and scope were not recorded"),
    ({"basis": {"rubric": "different", "scope": "same", "file_scope": "same"}},
     "same_scan_different_context", "different rubric"),
    ({"basis": {"rubric": "same", "scope": "not_recorded", "file_scope": "same"}},
     "context_not_recorded", "scan scope was not recorded"),
    ({"outcome": "failed", "issues_state": "unavailable", "completeness": "not_assessed"},
     "same_scan_failed", "did not produce a result"),
    ({"issues_state": "unavailable"}, "same_scan_issues_unavailable", "not recorded as a complete"),
    ({"completeness": "partial", "rules_not_checked": ["1.4.3"]}, "same_scan_partial",
     "only partly assessed (1 rule(s) not checked)"),
    ({"completeness": "unknown"}, "same_scan_partial", "of unrecorded completeness"),
    ({"integrity": "issues_changed_after_write"}, "same_scan_issues_changed", "changed after"),
])
def test_rb2_an_unusable_snapshot_is_never_a_baseline(isolated_store, monkeypatch, kwargs, code,
                                                      words):
    _scan(isolated_store, files=[_doc(issues=CURRENT)])
    s = _same_scan(isolated_store, monkeypatch, _prior(REPLACED, **kwargs))["sameScanHistory"]
    assert (s["status"], s["reasonCode"]) == ("baseline_unusable", code)
    assert words in s["reason"]
    assert s["introduced"] == [] and s["resolved"] == [] and s["persisting"] == []


def test_rb2_a_remediated_row_does_not_disqualify_verified_issue_rows(isolated_store, monkeypatch):
    _scan(isolated_store, files=[_doc(issues=CURRENT)])
    s = _same_scan(isolated_store, monkeypatch,
                   _prior(REPLACED, integrity="verified;file_row_changed_after_write"))["sameScanHistory"]
    assert s["status"] == "compared"


def test_rb2_a_recorded_zero_is_a_real_zero(isolated_store, monkeypatch):
    _scan(isolated_store, files=[_doc(issues=CURRENT)])
    s = _same_scan(isolated_store, monkeypatch, _prior([]))["sameScanHistory"]
    assert s["status"] == "compared" and s["snapshot"]["zeroFindings"] is True
    assert len(s["introduced"]) == 2 and s["resolved"] == []      # the two located images


def test_rb2_none_is_not_recorded_and_absent_reader_is_not_available(isolated_store, monkeypatch,
                                                                     no_history_module):
    _scan(isolated_store, files=[_doc(issues=CURRENT)])
    absent = rf.build_file_facts(isolated_store, SID, FILE, owner=OWNER)["sameScanHistory"]
    assert absent["status"] == "not_available"
    assert "not evidence" in absent["reason"]
    none = _same_scan(isolated_store, monkeypatch, None)["sameScanHistory"]
    assert none["status"] == "not_recorded" and none["snapshot"] is None
    assert "not evidence that there was no earlier one" in none["reason"]


def test_rb2_an_unfinished_current_assessment_is_not_compared(isolated_store, monkeypatch):
    _scan(isolated_store, files=[_doc(status="error", issues=[])])
    s = _same_scan(isolated_store, monkeypatch, _prior(REPLACED))["sameScanHistory"]
    assert s["status"] == "not_comparable" and s["reasonCode"] == "current_not_assessed"


def test_rb2_scan_index_rows_equal_the_per_file_route_and_aggregate(isolated_store, monkeypatch):
    _scan(isolated_store, files=[_doc(issues=CURRENT), _doc("other.docx", issues=CURRENT)])
    by_file = {FILE: _prior(REPLACED), "other.docx": None}
    _as_module(monkeypatch, isolated_store,
               prior_assessment_in_scan=lambda st, sid, f, *, owner: by_file[f])
    scan = rf.build_scan_facts(isolated_store, SID, owner=OWNER)
    rows = {r["file"]: r for r in scan["files"]}
    for name in by_file:
        assert rows[name]["factsDigest"] == rf.build_file_facts(
            isolated_store, SID, name, owner=OWNER)["factsDigest"]
    # the row key set is unchanged (rf-C's packet recording pins it); the per-file facts carry it
    assert "sameScanHistory" not in rows[FILE]
    same = scan["comparison"]["sameScan"]
    assert (same["filesCompared"], same["filesNotRecorded"]) == (1, 1)
    assert (same["introduced"], same["resolved"], same["persisting"]) == (1, 1, 1)
    notes = " ".join(scan["comparison"]["notes"])
    assert "1 document(s) were compared with the assessment each replaced (1 new, 1 no longer " \
           "reported, 1 still reported)" in notes
    assert "1 have no replaced assessment recorded, which is not evidence" in notes
    assert "are not compared" not in notes      # the "no reader" note is gone once one exists


def test_rb2_bite_the_snapshot_problem_check_is_what_keeps_a_failed_run_out(isolated_store,
                                                                           monkeypatch):
    """Bite check: with same_scan_problem disabled a FAILED replaced assessment (0 rows) would
    turn every current finding into "new within this scan" — the C2 shape, again."""
    import report_comparison as rc
    _scan(isolated_store, files=[_doc(issues=CURRENT)])
    monkeypatch.setattr(rc, "same_scan_problem", lambda found: None)
    s = _same_scan(isolated_store, monkeypatch,
                   _prior([], outcome="failed", issues_state="unavailable",
                          completeness="not_assessed"))["sameScanHistory"]
    assert s["status"] == "compared" and len(s["introduced"]) == 2


# ── R-B1: the batched baseline reader ────────────────────────────────────────────────────────

OLD = "2026-08-01T00:00:00+00:00"


def test_rb1_report_history_serves_both_the_index_and_the_per_file_route(isolated_store,
                                                                         monkeypatch):
    _scan(isolated_store, sid="s-old", completed=OLD, files=[_doc(issues=REPLACED)])
    _scan(isolated_store, files=[_doc(issues=CURRENT), _doc("new.docx", issues=CURRENT)])
    import report_history
    real = report_history.previous_assessments_for_scan      # the owner's REAL batched reader
    calls = []

    def batched(store, scan_id, *, owner, files=None):
        calls.append(list(files or []))
        return real(store, scan_id, owner=owner, files=files)

    _as_module(monkeypatch, isolated_store, previous_assessments_for_scan=batched)

    def per_file_must_not_run(*a, **k):
        raise AssertionError("the per-file store read ran although the batched reader exists")
    monkeypatch.setattr(type(isolated_store), "previous_assessment_for_file", per_file_must_not_run)

    per_file = rf.build_file_facts(isolated_store, SID, FILE, owner=OWNER)
    assert calls == [[FILE]]
    assert per_file["comparison"]["status"] == "compared"
    built = rf.build_scan_index(isolated_store, SID, owner=OWNER)
    assert built["baselineRead"] == "batched"
    assert sorted(calls[-1]) == sorted([FILE, "new.docx"])
    rows = {r["file"]: r for r in built["index"]}
    assert rows[FILE]["factsDigest"] == per_file["factsDigest"]
    assert rows["new.docx"]["comparison"]["reasonCode"] == "no_earlier_assessment"


def test_rb1_store_method_wins_over_the_module(isolated_store, monkeypatch):
    _scan(isolated_store, files=[_doc(issues=CURRENT)])
    import report_history
    monkeypatch.setattr(report_history, "previous_assessments_for_scan",
                        lambda *a, **k: (_ for _ in ()).throw(
                            AssertionError("module used although the Store has the method")))
    _as_store_method(monkeypatch, isolated_store, "previous_assessments_for_scan",
                     lambda self, scan_id, *, owner, files=None: {})
    built = rf.build_scan_index(isolated_store, SID, owner=OWNER)
    assert built["baselineRead"] == "batched"
    assert built["index"][0]["comparison"]["status"] == "no_baseline"

"""Contract 4: comparison with an earlier assessment, classified on the SERVER from the real store.

Three of the audit's defects were reproduced against the real SQLite store before this file was
written (/tmp/acp-audit2/tests/test_zz_audit_compare.py), and each test below starts from that
exact input:

* C1 — "Document language is not set" (3.1.1, no detector location) present in BOTH runs read as
  resolved AND newly introduced, because its synthetic identity is scan-scoped and the browser
  ignored `comparable: false`.
* C2 — a previous file row with status `error` and 0 findings was accepted as the baseline, so
  every current finding would read "new since then".
* C3 — a file renamed at the source (same provider id) was matched by the server and then rejected
  by the browser as "a different document".

The assertions are about what is NOT claimed as much as what is: a synthetic finding is never
introduced or resolved, an unusable baseline never produces "new", and an unknown reopened state
is `None`, not an empty list.
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
import report_comparison as rc   # noqa: E402
from test_report_facts import _scan, _doc, _image_issue, SID, FILE, OWNER   # noqa: E402

LANG = {"ruleId": "DOCX-LANG-001", "wcag": "3.1.1 Language of Page", "severity": "SERIOUS",
        "detail": "Document language is not set", "page": None, "location": None}
OLD = "2026-08-01T00:00:00+00:00"


def _facts(store, file=FILE):
    return rf.build_file_facts(store, SID, file, owner=OWNER)


# ── C1: a finding with no location is never both resolved and introduced ──────

def test_c1_unlocated_finding_in_both_runs_is_neither_new_nor_resolved(isolated_store):
    _scan(isolated_store, sid="s-old", source="drive", completed=OLD,
          files=[_doc(drive_file_id="d1", issues=[LANG, _image_issue("a", 1, "docx:image:1")])])
    _scan(isolated_store, source="drive",
          files=[_doc(drive_file_id="d1", issues=[LANG, _image_issue("a", 1, "docx:image:1")])])
    facts = _facts(isolated_store)
    # The reproduction's premise still holds: the two synthetic ids differ …
    cur = {f["id"] for f in facts["findings"] if not f["comparable"]}
    prev = {f["id"] for f in facts["previous"]["findings"] if not f["comparable"]}
    assert cur and prev and cur != prev
    # … and the server's classification does not turn that into a change.
    c = facts["comparison"]
    assert c["status"] == "compared"
    assert c["introduced"] == [] and c["resolved"] == []
    assert len(c["persisting"]) == 1                      # the located image, matched by identity
    assert c["notComparable"] == {"current": 1, "previous": 1}
    assert c["notComparableByCriterion"] == [
        {"sc": "3.1.1", "ruleId": "DOCX-LANG-001", "current": 1, "previous": 1}]


def test_c1_bite_check_classification_that_ignores_comparable_goes_red(isolated_store,
                                                                       monkeypatch):
    """Break the one rule and the defect is back: proves the test above tests the rule."""
    _scan(isolated_store, sid="s-old", source="drive", completed=OLD,
          files=[_doc(drive_file_id="d1", issues=[LANG])])
    _scan(isolated_store, source="drive", files=[_doc(drive_file_id="d1", issues=[LANG])])
    real = rc.classify
    monkeypatch.setattr(rc, "classify", lambda cur, prev, states: real(
        [{**f, "comparable": True} for f in cur], [{**f, "comparable": True} for f in prev],
        states))
    c = _facts(isolated_store)["comparison"]
    assert c["introduced"] and c["resolved"], "without the rule the language finding flips"


# ── C2: an error / partial / unfinished baseline is not a baseline ────────────

def test_c2_an_errored_earlier_row_is_baseline_unusable_not_everything_new(isolated_store):
    _scan(isolated_store, sid="s-old", source="drive", completed=OLD,
          files=[_doc(drive_file_id="d1", status="error", score=None, issues=[])])
    _scan(isolated_store, source="drive",
          files=[_doc(drive_file_id="d1", issues=[_image_issue("a", 1, "docx:image:1")])])
    facts = _facts(isolated_store)
    c = facts["comparison"]
    assert c["status"] == "baseline_unusable"
    assert c["reasonCode"] == "baseline_error"
    assert c["introduced"] == [] and c["resolved"] == []
    assert c["baseline"]["scanId"] == "s-old" and c["baseline"]["status"] == "error"
    assert facts["previous"] is None, "no client may compare against it either"
    assert "did not produce a result" in facts["previousReason"]


@pytest.mark.parametrize("doc_kwargs,code", [
    ({"status": "discovered", "score": None}, "baseline_not_assessed"),
    ({"skipped": 3}, "baseline_partial"),
])
def test_c2_a_partial_or_never_assessed_baseline_is_unusable(isolated_store, doc_kwargs, code):
    _scan(isolated_store, sid="s-old", source="drive", completed=OLD,
          files=[_doc(drive_file_id="d1", issues=[], **doc_kwargs)])
    _scan(isolated_store, source="drive",
          files=[_doc(drive_file_id="d1", issues=[_image_issue("a", 1, "docx:image:1")])])
    c = _facts(isolated_store)["comparison"]
    assert (c["status"], c["reasonCode"]) == ("baseline_unusable", code)


@pytest.mark.parametrize("run_status", ["cancelled", "interrupted", "superseded", "error"])
def test_c2_a_baseline_from_an_unfinished_scan_is_unusable(isolated_store, run_status):
    _scan(isolated_store, sid="s-old", source="drive", completed=OLD,
          files=[_doc(drive_file_id="d1", issues=[_image_issue("a", 1, "docx:image:1")])])
    with isolated_store._db.cursor() as cur:
        isolated_store._db.execute(cur, "UPDATE scan_runs SET status=%s WHERE id=%s",
                                   (run_status, "s-old"))
    _scan(isolated_store, source="drive", files=[_doc(drive_file_id="d1", issues=[])])
    c = _facts(isolated_store)["comparison"]
    assert c["status"] == "baseline_unusable"
    assert c["reasonCode"] == "baseline_run_unfinished"
    assert run_status in c["reason"]
    assert c["resolved"] == [], "a finding is not 'resolved' against an unfinished scan"


def test_an_unfinished_current_assessment_is_not_compared(isolated_store):
    _scan(isolated_store, sid="s-old", source="drive", completed=OLD,
          files=[_doc(drive_file_id="d1", issues=[_image_issue("a", 1, "docx:image:1")])])
    _scan(isolated_store, source="drive",
          files=[_doc(drive_file_id="d1", status="error", score=None, issues=[])])
    c = _facts(isolated_store)["comparison"]
    assert c["status"] == "not_comparable" and c["reasonCode"] == "current_not_assessed"
    assert c["resolved"] == [], "an errored run did not resolve anything"


# ── C3: a rename matched by the provider's id is compared ─────────────────────

def test_c3_a_renamed_file_with_the_same_provider_id_is_compared(isolated_store):
    _scan(isolated_store, sid="s-old", source="drive", completed=OLD,
          files=[_doc("old-name.docx", drive_file_id="d1",
                      issues=[_image_issue("a", 1, "docx:image:1"),
                              _image_issue("b", 2, "docx:image:2")])])
    _scan(isolated_store, source="drive",
          files=[_doc(drive_file_id="d1", issues=[_image_issue("a", 1, "docx:image:1"),
                                                  _image_issue("c", 3, "docx:image:3")])])
    facts = _facts(isolated_store)
    c = facts["comparison"]
    assert c["status"] == "compared" and c["renamed"] is True
    assert c["baseline"]["file"] == "old-name.docx"
    assert "named old-name.docx then" in c["reason"]
    assert len(c["persisting"]) == 1 and len(c["introduced"]) == 1 and len(c["resolved"]) == 1
    assert c["resolved"][0]["location"]["label"] == "Image 2"
    assert facts["previous"]["sameDocument"] is True
    assert facts["previous"]["matchedBy"] == "provider_file_id"


# ── C5: reopened, only where the baseline's own ledger can say so ──────────────

def test_c5_reopened_only_when_the_baseline_ledger_recorded_it_resolved(isolated_store,
                                                                       monkeypatch):
    from documents import resolve_doc_id
    from finding_ledger import stable_finding_id
    _scan(isolated_store, sid="s-old", source="drive", completed=OLD,
          files=[_doc(drive_file_id="d1", issues=[_image_issue("a", 1, "docx:image:1"),
                                                  _image_issue("b", 2, "docx:image:2")])])
    _scan(isolated_store, source="drive",
          files=[_doc(drive_file_id="d1", issues=[_image_issue("a", 1, "docx:image:1"),
                                                  _image_issue("b", 2, "docx:image:2")])])
    # Without a ledger the answer is UNKNOWN, not "none reopened".
    c = _facts(isolated_store)["comparison"]
    assert c["reopened"] is None and "no per-finding resolution ledger" in c["reopenedReason"]
    assert len(c["persisting"]) == 2

    document_id = resolve_doc_id("drive", "d1", FILE, "md5-source-1")
    ledger = {"snapshot_id": "s-old", "findings": [
        {"finding_id": stable_finding_id(document_id, "1.1.1", "docx:image:1"), "file": FILE,
         "state": "fixed", "reason": "applied and checked"},
        {"finding_id": stable_finding_id(document_id, "1.1.1", "docx:image:2"), "file": FILE,
         "state": "unresolved", "reason": "x"}]}
    real = rf.read_ledger
    monkeypatch.setattr(rf, "read_ledger",
                        lambda store, sid, owner: ledger if sid == "s-old" else real(store, sid, owner=owner))
    facts = _facts(isolated_store)
    c = facts["comparison"]
    image1 = next(f["id"] for f in facts["findings"] if f["instanceKey"] == "docx:image:1")
    assert c["reopened"] == [image1]
    assert image1 not in c["persisting"] and len(c["persisting"]) == 1


# ── the scan level (C4) and contract 3a ───────────────────────────────────────

def _estate(store):
    """Four documents, one per comparison outcome, in a real two-scan estate."""
    _scan(store, sid="s-old", source="drive", completed=OLD, files=[
        _doc("kept.docx", drive_file_id="d-kept",
             issues=[LANG, _image_issue("a", 1, "docx:image:1"), _image_issue("b", 2, "docx:image:2")]),
        _doc("was-called-this.docx", drive_file_id="d-renamed",
             issues=[_image_issue("a", 1, "docx:image:1")]),
        _doc("broken.docx", drive_file_id="d-broken", status="error", score=None, issues=[]),
    ])
    _scan(store, source="drive", files=[
        _doc("kept.docx", drive_file_id="d-kept",
             issues=[LANG, _image_issue("a", 1, "docx:image:1"), _image_issue("c", 3, "docx:image:3")]),
        _doc("renamed.docx", drive_file_id="d-renamed", issues=[_image_issue("a", 1, "docx:image:1")]),
        _doc("broken.docx", drive_file_id="d-broken", issues=[_image_issue("z", 1, "docx:image:9")]),
        _doc("brand-new.docx", drive_file_id="d-new", issues=[_image_issue("n", 1, "docx:image:1")]),
    ])


def test_c4_the_scan_facts_carry_a_real_estate_comparison(isolated_store):
    _estate(isolated_store)
    facts = rf.build_scan_facts(isolated_store, SID, owner=OWNER, limit=500)
    c = facts["comparison"]
    assert c["status"] == "compared"
    t = c["totals"]
    assert (t["filesCompared"], t["filesNoBaseline"], t["filesBaselineUnusable"], t["filesNew"]) \
        == (2, 1, 1, 1)
    assert (t["introduced"], t["resolved"], t["persisting"]) == (1, 1, 2)
    assert t["notComparable"] == 1 and t["filesRenamed"] == 1
    assert t["reopened"] is None and t["reopenedDetermined"] == 0
    rows = {r["file"]: r["comparison"] for r in facts["files"]}
    assert rows["broken.docx"]["status"] == "baseline_unusable"
    assert rows["brand-new.docx"]["reasonCode"] == "no_earlier_assessment"
    assert rows["renamed.docx"]["renamed"] is True
    assert rows["kept.docx"]["introduced"] == 1 and rows["kept.docx"]["resolved"] == 1
    # no score inference and no null baseline pretending to be the answer
    assert "Aggregate" not in c["reason"]


def test_contract_3a_every_index_row_digest_equals_the_per_file_route(client_owner, isolated_store):
    """Real store → scan pages over HTTP → per-file facts over HTTP: digests equal; then a
    reviewer decision changes, and exactly that file's digest moves in both."""
    _estate(isolated_store)
    c = client_owner
    rows = []
    first = c.get(f"/scans/{SID}/report-facts?limit=2").json()
    rows += first["files"]
    second = c.get(f"/scans/{SID}/report-facts?offset=2&limit=2&digest={first['factsDigest']}").json()
    rows += second["files"]
    assert len(rows) == 4
    for row in rows:
        per_file = c.get(f"/scans/{SID}/files/{row['file']}/report-facts").json()
        assert per_file["factsDigest"] == row["factsDigest"], row["file"]
        assert per_file["comparison"]["status"] == row["comparison"]["status"]

    isolated_store.save_decision(SID, "kept.docx", "change_review:kept.docx::1.1.1::0",
                                 json.dumps({"change_id": "kept.docx::1.1.1::0",
                                             "verdict": "rejected", "note": "wrong image"}),
                                 OWNER, "2026-09-02T00:00:00+00:00")
    fresh = c.get(f"/scans/{SID}/report-facts?limit=500").json()
    by_file = {r["file"]: r["factsDigest"] for r in fresh["files"]}
    before = {r["file"]: r["factsDigest"] for r in rows}
    assert by_file["kept.docx"] != before["kept.docx"]
    assert {f for f in by_file if by_file[f] != before[f]} == {"kept.docx"}
    assert c.get(f"/scans/{SID}/files/kept.docx/report-facts").json()["factsDigest"] \
        == by_file["kept.docx"]
    assert fresh["factsDigest"] != first["factsDigest"]


@pytest.fixture()
def client_owner(monkeypatch, isolated_store):
    import core
    from fastapi.testclient import TestClient
    from app import app
    rf.clear_scan_index_cache()
    monkeypatch.setattr(core, "store", isolated_store)
    monkeypatch.setattr(core, "ACCESS_CODE", "", raising=False)
    monkeypatch.setattr(core, "GOOGLE_CLIENT_ID", "test-client-id", raising=False)
    monkeypatch.setattr(core, "E2E_KEY", None, raising=False)
    monkeypatch.setattr(core, "OWNER_EMAIL", OWNER, raising=False)
    monkeypatch.setattr(core, "verify_gis_token", lambda tok: tok or None)
    monkeypatch.setattr(core, "email_allowed", lambda e: e == OWNER)
    c = TestClient(app)
    c.headers.update({"Authorization": f"Bearer {OWNER}"})
    yield c
    rf.clear_scan_index_cache()


# ── contract 3: snapshot paging ───────────────────────────────────────────────

def test_a_page_for_a_digest_the_evidence_no_longer_has_is_409(client_owner, isolated_store):
    _estate(isolated_store)
    first = client_owner.get(f"/scans/{SID}/report-facts?limit=2").json()
    assert first["snapshot"] == {"factsDigest": first["factsDigest"], "filesTotal": 4,
                                 "builtAt": first["snapshot"]["builtAt"], "servedFrom": "fresh",
                                 "ttlSeconds": rf.SCAN_INDEX_TTL_SECONDS}
    isolated_store.record_remediation_diffs(SID, "kept.docx", [
        {"rule_id": "1.1.1", "before": "", "after": "A description"}])
    # The memo is only a paging aid: after the evidence moves, a caller holding the OLD digest may
    # still page the old snapshot for the TTL — but anything that forces a fresh read says 409.
    rf.clear_scan_index_cache()
    r = client_owner.get(f"/scans/{SID}/report-facts?offset=2&limit=2&digest={first['factsDigest']}")
    assert r.status_code == 409
    assert r.json()["detail"] == "report evidence changed while paging; restart the export"


def test_a_malformed_digest_is_refused(client_owner, isolated_store):
    _estate(isolated_store)
    assert client_owner.get(f"/scans/{SID}/report-facts?digest=abc").status_code == 422


def test_the_memo_serves_only_a_matching_digest_and_never_the_render_check(isolated_store,
                                                                          monkeypatch):
    _estate(isolated_store)
    rf.clear_scan_index_cache()
    builds = []
    real = rf.build_scan_index
    monkeypatch.setattr(rf, "build_scan_index",
                        lambda *a, **k: builds.append(1) or real(*a, **k))
    first = rf.build_scan_facts(isolated_store, SID, owner=OWNER, limit=1)
    digest = first["factsDigest"]
    for offset in (1, 2, 3):
        page = rf.build_scan_facts(isolated_store, SID, owner=OWNER, offset=offset, limit=1,
                                   digest=digest)
        assert page["snapshot"]["servedFrom"] == "memo"
        assert page["factsDigest"] == digest
    assert len(builds) == 1, "four pages of one snapshot cost one build (S1)"
    # no digest → always fresh
    assert rf.build_scan_facts(isolated_store, SID, owner=OWNER, limit=1)["snapshot"]["servedFrom"] == "fresh"
    assert len(builds) == 2
    # the render route's seam never reads the memo
    assert rf.verify_digest(isolated_store, SID, None, digest, owner=OWNER) is True
    assert len(builds) == 3
    # another owner never sees the memo
    assert rf.build_scan_facts(isolated_store, SID, owner="someone@else.org", digest=digest) is None
    rf.clear_scan_index_cache()


def test_the_memo_expires(isolated_store, monkeypatch):
    _estate(isolated_store)
    rf.clear_scan_index_cache()
    first = rf.build_scan_facts(isolated_store, SID, owner=OWNER, limit=1)
    clock = [rf.time.monotonic() + rf.SCAN_INDEX_TTL_SECONDS + 1]
    monkeypatch.setattr(rf.time, "monotonic", lambda: clock[0])
    page = rf.build_scan_facts(isolated_store, SID, owner=OWNER, offset=1, limit=1,
                               digest=first["factsDigest"])
    assert page["snapshot"]["servedFrom"] == "fresh"
    rf.clear_scan_index_cache()


def test_the_index_build_is_linear_not_quadratic_in_file_lookups(isolated_store):
    """S1: file rows are looked up by dict. A list scan per file made the build O(N²)."""
    _scan(isolated_store, files=[_doc(f"doc{n:03d}.docx") for n in range(60)])
    ctx = rf.scan_context(isolated_store, SID, owner=OWNER)
    assert set(ctx["files_by_name"]) == {f"doc{n:03d}.docx" for n in range(60)}

    class NoIter(list):
        def __iter__(self):
            raise AssertionError("build_file_facts scanned the file list")
    ctx["scan"]["files"] = NoIter(ctx["scan"]["files"])
    assert rf.build_file_facts(isolated_store, SID, "doc059.docx", owner=OWNER,
                               context=ctx) is not None


# ── C11: queue approvals that need a recheck ──────────────────────────────────

def _approved_item(store, file=FILE):
    item_id = store.enqueue_proposals(SID, file, "1.1.1", [
        {"locator": "docx:image:1", "before": "", "proposed_value": "A barn", "rationale": "r",
         "source": "vision"}], rule_name="Non-text Content")
    with store._db.cursor() as cur:
        store._db.execute(cur, "UPDATE hitl_queue SET proposal_snapshot_ids=%s WHERE id=%s",
                          (json.dumps(["snap-1"]), item_id))
    store.complete_hitl_decision(item_id, "approved", None, None, resolution=None,
                                 approved_values=["A barn"], actor=OWNER, detail=None)
    return item_id


def test_c11_a_stale_queue_approval_is_in_the_file_facts(isolated_store):
    _scan(isolated_store, source="drive",
          files=[_doc(drive_file_id="d1", issues=[_image_issue("a", 1, "docx:image:1")])])
    isolated_store.record_remediation(SID, FILE, corrected_sha256="c" * 64)
    item_id = _approved_item(isolated_store)
    before = _facts(isolated_store)
    assert before["approvals"]["source"] == "ok"
    assert before["approvals"]["approvedAwaitingWrite"] == 1
    assert before["approvals"]["recheckRequired"] == []
    with isolated_store._db.cursor() as cur:
        isolated_store._db.execute(
            cur, "UPDATE hitl_queue SET approved_source_revision='an-older-revision' WHERE id=%s",
            (item_id,))
    after = _facts(isolated_store)
    assert [r["id"] for r in after["approvals"]["recheckRequired"]] == [item_id]
    assert after["factsDigest"] != before["factsDigest"]
    # The wording states the state; it never claims a recheck was queued.
    note = after["approvals"]["note"]
    assert "does not re-queue" in note
    assert "queued for recheck" not in note.lower() and "automatically" not in note.lower()
    scan = rf.build_scan_facts(isolated_store, SID, owner=OWNER)
    assert scan["totals"]["approvalsRecheckRequired"] == 1
    assert scan["files"][0]["approvalsRecheckRequired"] == 1


def test_c7_earlier_scan_decisions_are_named_as_unavailable_not_dropped(isolated_store):
    _scan(isolated_store, files=[_doc()])
    facts = _facts(isolated_store)
    if callable(getattr(isolated_store, "change_reviews_for_document", None)):
        pytest.skip("R-B3 landed; covered by the store's own test")
    assert facts["priorDecisions"] is None
    assert "not shown" in facts["priorDecisionsReason"]
    assert "not carried forward" in facts["priorDecisionsReason"]


def test_the_within_scan_note_never_replaces_the_across_scan_reason():
    """Found when the owner's real R-B2 reader landed: the same-scan note builder reused the
    `parts` list of the estate reason, so every scan with the reader read "5 have no replaced
    assessment recorded…" where it should say why there was no earlier-scan baseline."""
    rows = [{"status": rc.NO_BASELINE, "reasonCode": "no_earlier_assessment"}] * 2
    same = [{"status": rc.SAME_SCAN_NOT_RECORDED}] * 2
    with_reader = rc.aggregate(rows, same_scan_history=True, same_scan_rows=same)
    without = rc.aggregate(rows, same_scan_history=False, same_scan_rows=same)
    assert with_reader["reason"] == without["reason"]
    assert with_reader["reason"].startswith("no document in this scan has an earlier assessment")
    assert with_reader["notes"] == ["Re-assessments inside this scan: 2 have no replaced assessment "
                                    "recorded, which is not evidence that none was replaced."]

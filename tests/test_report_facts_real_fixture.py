"""The comparison/location evidence a browser report is fed — generated from the REAL store.

`frontend/src/__fixtures__/reportFacts.comparison.real.json` is written by this test from a real
SQLite store, through the real routes (TestClient), and is the ONLY input of
`frontend/src/reportComparisonReal.test.js`. No id in it was typed by a person: the finding ids,
change ids, digests and comparisons are what the server produced. The audit's point (section 2):
every earlier frontend comparison test used hand-invented ids, so none of them could have noticed
that the server's ids and the browser's classification disagreed.

The three reproduced scenarios are in it — the language-not-set synthetic key (C1), an errored
baseline (C2), a rename matched by provider id (C3) — plus a new file, a pptx shape location
(contract 1) and saved changes with a reviewer decision.

Since the owner's report-history readers landed (0f0fd520) every section runs them for REAL:
R-B1 baselines, R-B2 `prior_assessment_in_scan` (the `realHistory` section re-assesses a document
inside one scan through `save_file_result`, which captures the replaced assessment), R-B3
`change_reviews_for_document` (a decision recorded on the earlier scan of the renamed document)
and the R-B4 scan-wide readers. Only `contractShape` still substitutes two readers' RETURN VALUES —
for the shapes a small real store cannot produce (a truncated 5-decision history, a snapshot
written before history existed) — and it does so by patching the STORE INSTANCE's methods for the
duration of those reads, never by replacing the real `report_history` module.

CHECK MODE is the default: the committed fixture must equal what the store produces now, so a
server change that alters the evidence shape fails here until the fixture is regenerated and the
JS test re-run. Regenerate with:

    ACP_REGEN_REPORT_FIXTURES=1 python -m pytest tests/test_report_facts_real_fixture.py
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from urllib.parse import quote

import pytest

ACP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ACP / "api"))

FIXTURE = ACP / "frontend/src/__fixtures__/reportFacts.comparison.real.json"
OWNER = "reviewer@hosp.org"
OLD, NEW = "s-2026-08-baseline", "s-2026-09-current"
FIXED_NOW = "2026-09-17T12:00:00+00:00"

LANG = {"ruleId": "DOCX-LANG-001", "wcag": "3.1.1 Language of Page", "severity": "SERIOUS",
        "detail": "Document language is not set", "page": None, "location": None}


def _img(detail, location, page=None, rule="DOCX-ALT-001"):
    return {"ruleId": rule, "wcag": "1.1.1 Non-text Content", "severity": "SERIOUS",
            "detail": detail, "page": page, "location": location}


def _doc(name, drive_id, issues, status="uncertain", score=70):
    return {"file": name, "engine": ".net/office", "status": status, "score": score,
            "compliant": 0, "skipped_rules": 0, "issues": issues, "checksum": f"md5-{drive_id}",
            "drive_file_id": drive_id}


def _save(store, sid, completed, files):
    store.save_scan({
        "_scan_id": sid, "started_at": completed, "completed_at": completed, "source": "drive",
        "owner": OWNER, "rubric": {"name": "wcag-aa", "hash": "rubric-1"},
        "summary": {"files": len(files), "certifiable": 0, "uncertain": len(files), "error": 0,
                    "avg_score": 70},
        "files": files,
    })


def build_estate(store):
    _save(store, OLD, "2026-08-01T09:00:00+00:00", [
        _doc("policies/language-not-set.docx", "d-lang",
             [LANG, _img("Chart has no description", "docx:drawing:7:paragraph:3")]),
        _doc("policies/was-called-this.docx", "d-renamed",
             [_img("Logo has no description", "docx:image:1"),
              _img("Photo has no description", "docx:image:2")]),
        _doc("policies/analyser-failed.docx", "d-broken", [], status="error", score=None),
        _doc("decks/quarterly.pptx", "d-deck",
             [_img("Shape 5 has no alt text", "pptx:slide:1:element:5", page=2, rule="PPTX-ALT-001")]),
    ])
    _save(store, NEW, "2026-09-01T09:00:00+00:00", [
        _doc("policies/language-not-set.docx", "d-lang",
             [LANG, _img("Chart has no description", "docx:drawing:7:paragraph:3")]),
        _doc("policies/renamed.docx", "d-renamed",
             [_img("Logo has no description", "docx:image:1"),
              _img("Map has no description", "docx:image:3")]),
        _doc("policies/analyser-failed.docx", "d-broken",
             [_img("Diagram has no description", "docx:image:1")]),
        _doc("decks/quarterly.pptx", "d-deck",
             [_img("Shape 5 has no alt text", "pptx:slide:1:element:5", page=2, rule="PPTX-ALT-001"),
              _img("Shape 9 has no alt text", "pptx:slide:3:element:9", page=4, rule="PPTX-ALT-001")]),
        _doc("sheets/brand-new.xlsx", "d-new",
             [{"ruleId": "XLSX-LINK-001", "wcag": "2.4.4 Link Purpose", "severity": "MODERATE",
               "detail": "Link text is 'click here'", "page": None,
               "location": "xlsx:sheet:Budget 2026:cell:B3"}]),
    ])
    # A verified saved change and a reviewer decision on it, on the renamed document.
    renamed = "policies/renamed.docx"
    store.record_remediation(NEW, renamed, corrected_sha256="c" * 64)
    with store._db.cursor() as cur:
        store._db.execute(cur, "UPDATE file_records SET remediated_at=%s WHERE scan_id=%s AND file=%s",
                          ("2026-09-02T10:00:00+00:00", NEW, renamed))
    store.record_remediation_diffs(NEW, renamed, [
        {"rule_id": "1.1.1", "before": "", "after": "Hospital logo", "note": "vision"},
        # R1 (schema v59): a location the Word writer RECORDED — a 1-based OOXML paragraph token,
        # and a page it should never have sent (stored, never shown: Word has no pages) …
        {"rule_id": "1.3.1", "before": "Body Text", "after": "Heading 2", "note": "promoted",
         "locator": "word:p:4", "page": 3},
        # … and a legacy row the store reads back from the handlers' exact note prefix.
        {"rule_id": "1.4.5", "before": "", "after": "Floor plan",
         "note": "approved by a reviewer · word/document.xml#rId7"}])
    store.record_remediation_diffs(NEW, "decks/quarterly.pptx", [
        # a slide PART is not a slide number: never "Slide 3"
        {"rule_id": "1.1.1", "before": "", "after": "Revenue chart",
         "locator": "ppt/slides/slide3.xml#Picture 9"}])
    import report_facts as rf
    change_id = rf.verified_change_id(renamed, "1.1.1", 0)
    store.save_decision(NEW, renamed, "change_review:" + change_id, json.dumps({
        "change_id": change_id, "verdict": "accepted", "artifact_sha256": "c" * 64,
        "change_digest": rf.change_digest("1.1.1", "", "Hospital logo"),
        "reviewer": OWNER, "at": "2026-09-03T08:00:00+00:00"}), OWNER, "2026-09-03T08:00:00+00:00")
    # R-B3 (real): a decision recorded on the EARLIER scan of the renamed document, under its old
    # name. The real reader finds it by provider id; it is evidence, never carried forward.
    old_id = rf.verified_change_id("policies/was-called-this.docx", "1.1.1", 0)
    store.save_decision(OLD, "policies/was-called-this.docx", "change_review:" + old_id, json.dumps({
        "change_id": old_id, "verdict": "rejected", "reviewer": OWNER,
        "at": "2026-08-02T08:00:00+00:00"}), OWNER, "2026-08-02T08:00:00+00:00")


REASSESSED = "s-2026-09-reassessed"
REDOC = "board/minutes.docx"


def build_reassessment(store):
    """R-B2 (real): one document assessed, then RE-assessed inside the same scan, both through
    save_file_result — which captures the replaced assessment in file_assessment_history."""
    store.init_scan_run(REASSESSED, "drive", 1, "2026-09-05T09:00:00+00:00", "wcag-aa", "rubric-1",
                        owner=OWNER, scope={"scan_scope": {"1.1.1": ["docx"]}})

    def doc(issues):
        return {"file": REDOC, "engine": ".net/office", "status": "uncertain", "score": 70,
                "compliant": 0, "skipped_rules": 0, "issues": issues, "drive_file_id": "d-minutes",
                "checksum": "md5-minutes"}
    assert store.save_file_result(REASSESSED, doc([
        _img("Logo has no description", "docx:image:1"),
        _img("Seal has no description", "docx:image:4")]), "2026-09-05T09:10:00+00:00") is True
    assert store.save_file_result(REASSESSED, doc([
        _img("Logo has no description", "docx:image:1"),
        _img("Map has no description", "docx:image:3"), LANG]), "2026-09-05T09:20:00+00:00") is True


def real_history(client, store) -> dict:
    build_reassessment(store)
    r = client.get(f"/scans/{REASSESSED}/files/{quote(REDOC, safe='')}/report-facts")
    assert r.status_code == 200, r.text
    scan = client.get(f"/scans/{REASSESSED}/report-facts?limit=10")
    assert scan.status_code == 200, scan.text
    return {"_note": "REAL R-B2: re-assessed inside one scan through save_file_result; the owner's "
                     "prior_assessment_in_scan read it.",
            "files": {REDOC: r.json()}, "scanComparison": scan.json()["comparison"],
            "scanRow": scan.json()["files"][0]}


@pytest.fixture()
def client(monkeypatch, isolated_store):
    import core
    import report_facts as rf
    from fastapi.testclient import TestClient
    from app import app
    rf.clear_scan_index_cache()
    monkeypatch.setattr(rf, "_now", lambda: FIXED_NOW)
    # file_assessment_history ids are uuid4 and appear in the facts (sameScanHistory.snapshot),
    # so they are made deterministic for the fixture — ONLY report_history's own reference to
    # the uuid module is swapped, for this test; the real module and uuid itself are untouched.
    import itertools
    import types
    import report_history
    counter = itertools.count(1)
    monkeypatch.setattr(report_history, "uuid", types.SimpleNamespace(
        uuid4=lambda: types.SimpleNamespace(hex=f"{next(counter):032x}")))
    monkeypatch.delenv("ACP_BUILD_VERSION", raising=False)
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


def generate(client, store) -> dict:
    build_estate(store)
    first = client.get(f"/scans/{NEW}/report-facts?limit=3")
    assert first.status_code == 200, first.text
    first = first.json()
    second = client.get(f"/scans/{NEW}/report-facts?offset=3&limit=3&digest={first['factsDigest']}")
    assert second.status_code == 200, second.text
    second = second.json()
    files = {}
    for row in first["files"] + second["files"]:
        r = client.get(f"/scans/{NEW}/files/{quote(row['file'], safe='')}/report-facts")
        assert r.status_code == 200, r.text
        files[row["file"]] = r.json()
    return {"_generatedBy": "tests/test_report_facts_real_fixture.py (real store, real routes)",
            "scanPages": [first, second], "files": files,
            "realHistory": real_history(client, store),
            "contractShape": contract_shape(client, store)}


CONTRACT_SHAPE_NOTE = (
    "CONTRACT-SHAPE section: the same real store and real routes, but the store instance's "
    "prior_assessment_in_scan / change_reviews_for_document return owner-contract shapes a small "
    "real store cannot produce (a truncated 5-decision history; a snapshot whose context was not "
    "recorded). Every other reader is the owner's real one; see realHistory for real R-B2/R-B3.")


def contract_shape(client, store) -> dict:
    """Per-file facts with stand-in history readers. Built through the real route, so the
    projection, ids and digests are the server's; only the two readers' RETURN VALUES are made up."""
    import report_facts as rf
    renamed, lang = "policies/renamed.docx", "policies/language-not-set.docx"
    current = store.get_scan(NEW, owner=OWNER)
    renamed_issues = next(f for f in current["files"] if f["file"] == renamed)["issues"]
    replaced = [{"rule_id": i.get("rule_id") or i.get("ruleId"), "wcag": i.get("wcag"),
                 "severity": i.get("severity"), "detail": i.get("detail"), "page": i.get("page"),
                 "location": i.get("location")} for i in renamed_issues
                if i.get("location") != "docx:image:3"]
    replaced.append({"rule_id": "DOCX-ALT-001", "wcag": "1.1.1 Non-text Content",
                     "severity": "SERIOUS", "detail": "Seal has no description", "page": None,
                     "location": "docx:image:4"})

    def snapshot(context, basis):
        return {"history_id": "h-1", "seq": 1, "history_total": 2, "context_source": context,
                "assessment_outcome": "assessed", "issues_state": "recorded",
                "issue_count": len(replaced), "zero_findings": False, "completeness": "complete",
                "rules_not_checked": [], "rule_manifest": [],
                "written_at": "2026-09-01T08:30:00+00:00",
                "superseded_at": "2026-09-01T09:00:00+00:00",
                "superseded_by": {"job_id": "j-2", "attempt": 1},
                "artifact": {"file": renamed, "drive_file_id": "d-renamed", "checksum": "md5-d-renamed"},
                "integrity": "verified", "row_at_replacement": None, "comparison_basis": basis}

    same = {"rubric": "same", "scope": "same", "file_scope": "same"}
    unknown = {"rubric": "not_recorded", "scope": "not_recorded", "file_scope": "not_recorded"}
    priors = {
        renamed: {"run": {"id": NEW, "same_scan_snapshot": True}, "file_row": {"file": renamed},
                  "issues": replaced, "snapshot": snapshot("recorded_at_write", same)},
        lang: {"run": {"id": NEW, "same_scan_snapshot": True}, "file_row": {"file": lang},
               "issues": replaced, "snapshot": snapshot("not_recorded", unknown)},
    }
    decisions = [{"scan_id": OLD, "file": "policies/was-called-this.docx",
                  "kind": f"change_review:policies/was-called-this.docx::1.1.1::{i}",
                  "change_id": f"policies/was-called-this.docx::1.1.1::{i}",
                  "value": json.dumps({"change_id": f"policies/was-called-this.docx::1.1.1::{i}",
                                       "verdict": "accepted", "reviewer": OWNER,
                                       "at": f"2026-08-0{2 + i}T08:00:00+00:00"}),
                  "ts": f"2026-08-0{2 + i}T08:00:00+00:00", "scan_at": "2026-08-01T09:00:00+00:00"}
                 for i in (1, 0)]
    # The STORE INSTANCE's two methods, for these reads only (history_reader prefers the store's
    # method). The real report_history module is never replaced, so nothing else is affected.
    patched = {
        "prior_assessment_in_scan": lambda sid, f, *, owner: priors.get(f),
        "change_reviews_for_document": lambda sid, f, *, owner, limit=200: (
            {"decisions": decisions, "total": 5, "returned": 2, "limit": limit, "truncated": True}
            if f == renamed else {"decisions": [], "total": 0, "returned": 0, "limit": limit,
                                  "truncated": False}),
    }
    for name, fn in patched.items():
        setattr(store, name, fn)
    try:
        rf.clear_scan_index_cache()
        files = {}
        for name in (renamed, lang, "sheets/brand-new.xlsx"):
            r = client.get(f"/scans/{NEW}/files/{quote(name, safe='')}/report-facts")
            assert r.status_code == 200, r.text
            files[name] = r.json()
        scan = client.get(f"/scans/{NEW}/report-facts?limit=10").json()
    finally:
        for name in patched:
            delattr(store, name)          # the instance attribute; the class method is back
        rf.clear_scan_index_cache()
    return {"_note": CONTRACT_SHAPE_NOTE, "files": files, "scanComparison": scan["comparison"]}


def test_the_committed_fixture_is_what_the_real_store_produces(client, isolated_store):
    got = generate(client, isolated_store)
    # The contract-3a claim, on the generated data itself: every index row's digest equals the
    # per-file route's.
    rows = got["scanPages"][0]["files"] + got["scanPages"][1]["files"]
    assert {r["file"]: r["factsDigest"] for r in rows} == \
        {f: facts["factsDigest"] for f, facts in got["files"].items()}
    text = json.dumps(got, indent=1, sort_keys=True, ensure_ascii=False) + "\n"
    if os.environ.get("ACP_REGEN_REPORT_FIXTURES") == "1":
        FIXTURE.parent.mkdir(parents=True, exist_ok=True)
        FIXTURE.write_text(text, encoding="utf-8")
    assert FIXTURE.exists(), "run with ACP_REGEN_REPORT_FIXTURES=1 to create the fixture"
    assert json.loads(FIXTURE.read_text(encoding="utf-8")) == json.loads(text), (
        "the server's report evidence changed; regenerate the fixture with "
        "ACP_REGEN_REPORT_FIXTURES=1 and re-run frontend/src/reportComparisonReal.test.js")


def test_the_fixture_holds_the_three_reproduced_scenarios(client, isolated_store):
    got = generate(client, isolated_store)
    f = got["files"]
    lang = f["policies/language-not-set.docx"]["comparison"]
    assert lang["status"] == "compared" and lang["introduced"] == [] and lang["resolved"] == []
    assert lang["notComparable"] == {"current": 1, "previous": 1}
    broken = f["policies/analyser-failed.docx"]["comparison"]
    assert (broken["status"], broken["reasonCode"]) == ("baseline_unusable", "baseline_error")
    renamed = f["policies/renamed.docx"]["comparison"]
    assert renamed["status"] == "compared" and renamed["renamed"] is True
    assert f["sheets/brand-new.xlsx"]["comparison"]["reasonCode"] == "no_earlier_assessment"
    assert f["decks/quarterly.pptx"]["findings"][0]["location"]["label"] == "Slide 2 · shape 5"


def test_the_fixture_holds_the_r1_locations_from_the_real_store(client, isolated_store):
    got = generate(client, isolated_store)
    changes = {c["ruleId"]: c for c in got["files"]["policies/renamed.docx"]["savedChanges"]}
    assert changes["1.1.1"]["location"] is None                     # nothing recorded
    assert changes["1.3.1"]["location"]["label"] == "Paragraph 4"
    assert changes["1.3.1"]["location"]["page"] is None             # the stored page 3 is not shown
    assert changes["1.3.1"]["location"]["source"] == "recorded"
    legacy = changes["1.4.5"]["location"]
    assert legacy["source"] == "legacy_note" and legacy["page"] is None
    assert legacy["label"].endswith("(from the saving step's note, not a recorded location)")
    deck = got["files"]["decks/quarterly.pptx"]["savedChanges"][0]["location"]
    assert deck["label"] == "Object “Picture 9” · slide file slide3.xml"
    assert deck["page"] is None and deck["slide"] is None


def test_the_contract_shape_section(client, isolated_store):
    cs = generate(client, isolated_store)["contractShape"]
    renamed = cs["files"]["policies/renamed.docx"]
    same = renamed["sameScanHistory"]
    assert same["status"] == "compared"
    by_id = {x["id"]: x["detail"] for x in renamed["findings"]}
    assert [by_id[i] for i in same["introduced"]] == ["Map has no description"]
    assert [r["detail"] for r in same["resolved"]] == ["Seal has no description"]
    assert "showing 2 of 5" in renamed["priorDecisionsReason"]
    lang = cs["files"]["policies/language-not-set.docx"]
    assert lang["sameScanHistory"]["reasonCode"] == "context_not_recorded"
    assert lang["priorDecisions"] == []
    assert cs["files"]["sheets/brand-new.xlsx"]["sameScanHistory"]["status"] == "not_recorded"
    assert cs["scanComparison"]["sameScan"]["filesCompared"] == 1


def test_real_rb2_a_reassessment_inside_one_scan_is_compared_finding_by_finding(client,
                                                                               isolated_store):
    real = generate(client, isolated_store)["realHistory"]
    facts = real["files"][REDOC]
    same = facts["sameScanHistory"]
    assert same["status"] == "compared", same
    assert same["baseline"]["sameScanSnapshot"] is True
    by_id = {f["id"]: f["detail"] for f in facts["findings"]}
    assert [by_id[i] for i in same["introduced"]] == ["Map has no description"]
    assert [r["detail"] for r in same["resolved"]] == ["Seal has no description"]
    assert [by_id[i] for i in same["persisting"]] == ["Logo has no description"]
    # The unlocated language finding is in neither list: not matchable one by one.
    assert same["notComparable"]["current"] == 1
    assert real["scanComparison"]["sameScan"]["filesCompared"] == 1
    # Contract 3a still holds with the real readers: the index row IS the per-file facts.
    assert real["scanRow"]["factsDigest"] == facts["factsDigest"]


def test_real_rb3_an_earlier_scans_decision_is_evidence_not_carried_forward(client, isolated_store):
    f = generate(client, isolated_store)["files"]
    renamed = f["policies/renamed.docx"]
    assert [(d["scanId"], d["verdict"], d["status"]) for d in renamed["priorDecisions"]] == [
        (OLD, "rejected", "not_carried_forward")]
    assert renamed["priorDecisionsBounds"] == {"total": 1, "returned": 1, "limit": 200,
                                               "truncated": False, "unreadable": 0}
    # …and it is never applied to this scan's changes.
    assert all(v["verdict"] == "accepted" for v in renamed["reviews"].values())
    lang = f["policies/language-not-set.docx"]
    assert lang["priorDecisions"] == []
    assert lang["priorDecisionsReason"] == "no decision is recorded against an earlier scan of this document"


def test_real_rb1_the_index_used_the_batched_baselines_and_rows_equal_the_per_file_route(
        client, isolated_store):
    import report_facts as rf
    got = generate(client, isolated_store)
    built = rf.build_scan_index(isolated_store, NEW, owner=OWNER)
    assert built["baselineRead"] == "batched"
    assert built["inputsRead"] == {"diffs": "batched", "reviews": "batched", "unverified": "batched"}
    assert {r["file"]: r["factsDigest"] for r in built["index"]} == \
        {name: facts["factsDigest"] for name, facts in got["files"].items()}

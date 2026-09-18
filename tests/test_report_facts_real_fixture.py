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
        {"rule_id": "1.1.1", "before": "", "after": "Hospital logo", "note": "vision"}])
    import report_facts as rf
    change_id = rf.verified_change_id(renamed, "1.1.1", 0)
    store.save_decision(NEW, renamed, "change_review:" + change_id, json.dumps({
        "change_id": change_id, "verdict": "accepted", "artifact_sha256": "c" * 64,
        "change_digest": rf.change_digest("1.1.1", "", "Hospital logo"),
        "reviewer": OWNER, "at": "2026-09-03T08:00:00+00:00"}), OWNER, "2026-09-03T08:00:00+00:00")


@pytest.fixture()
def client(monkeypatch, isolated_store):
    import core
    import report_facts as rf
    from fastapi.testclient import TestClient
    from app import app
    rf.clear_scan_index_cache()
    monkeypatch.setattr(rf, "_now", lambda: FIXED_NOW)
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
            "scanPages": [first, second], "files": files}


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

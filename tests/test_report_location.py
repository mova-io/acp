"""Contract 1: a finding's location as a person reads it — one table, two parsers.

`tests/fixtures/report_location_cases.json` is read HERE by the Python parser and by
`frontend/src/reportLocation.test.js` by the browser's. The two must agree case for case: a
report built on the server and a report built in the browser from the same finding must name the
same place, and neither may fall back to page 1 or print `docx:paragraph:14` at a reader.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ACP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ACP / "api"))

import report_location   # noqa: E402

CASES = json.loads((ACP / "tests/fixtures/report_location_cases.json").read_text())["cases"]
KEYS = ("label", "kind", "page", "slide", "sheet", "cell", "objectId", "raw")


@pytest.mark.parametrize("case", CASES, ids=[c["name"] for c in CASES])
def test_the_shared_location_table(case):
    got = report_location.parse_location(case["location"], case["page"], case["fmt"])
    if case["expect"] is None:
        assert got is None
        return
    assert {k: got[k] for k in KEYS} == case["expect"]
    # the legacy key is the raw locator, kept for older consumers
    assert got["element"] == case["expect"]["raw"]


def test_no_label_is_a_parseable_machine_string():
    """The audit's L2 finding, as a property rather than a list: every prefixed locator in the
    table that the parser understands is labelled in words."""
    for case in CASES:
        got = report_location.parse_location(case["location"], case["page"], case["fmt"])
        if got and got["raw"] and got["raw"].split(":", 1)[0] in report_location.FORMATS \
                and got["kind"] is not None:
            assert got["label"] != got["raw"], case["name"]
            assert not got["label"].startswith(("docx:", "pptx:", "xlsx:", "pdf:")), case["name"]


def test_word_never_gets_a_page_and_nothing_gets_page_one_by_default():
    assert report_location.parse_location("docx:paragraph:0", 1, "docx")["page"] is None
    assert report_location.parse_location("docx:paragraph:0", None, None)["page"] is None
    assert report_location.parse_location(None, None, "pdf") is None
    assert report_location.parse_location("", 0, "pdf") is None


def test_the_facts_findings_carry_the_structured_location(isolated_store):
    """The builder uses this parser — not a copy of the machine string (the L2 regression)."""
    import report_facts as rf
    isolated_store.save_scan({
        "_scan_id": "s-loc", "started_at": "2026-09-01T05:00:00+00:00",
        "completed_at": "2026-09-01T05:05:00+00:00", "source": "local", "owner": "o@x.org",
        "rubric": {"name": "wcag-aa", "hash": "h"},
        "summary": {"files": 1, "certifiable": 0, "uncertain": 1, "error": 0, "avg_score": 70},
        "files": [{"file": "deck.pptx", "engine": ".net/office", "status": "uncertain",
                   "score": 70, "compliant": 0, "skipped_rules": 0, "checksum": "x",
                   "issues": [{"ruleId": "PPTX-ALT-001", "wcag": "1.1.1 Non-text Content",
                               "severity": "SERIOUS", "detail": "no alt", "page": 2,
                               "location": "pptx:slide:1:element:5"}]}],
    })
    finding = rf.build_file_facts(isolated_store, "s-loc", "deck.pptx", owner="o@x.org")["findings"][0]
    assert finding["location"]["label"] == "Slide 2 · shape 5"
    assert finding["location"]["objectId"] == "shape:5"
    assert finding["location"]["page"] == finding["location"]["slide"] == 2


def test_saved_changes_carry_a_location_and_verified_ones_read_the_r1_locator(isolated_store,
                                                                             monkeypatch):
    """Contract 1 for savedChanges. The unverified record has its own locator; a verified
    remediation_diff row has one only once store request R1 lands — read defensively, so a legacy
    row is `None` ("Location not recorded") and a row WITH `locator`/`page` is parsed."""
    import report_facts as rf
    isolated_store.save_scan({
        "_scan_id": "s-sc", "started_at": "2026-09-01T05:00:00+00:00",
        "completed_at": "2026-09-01T05:05:00+00:00", "source": "local", "owner": "o@x.org",
        "rubric": {"name": "wcag-aa", "hash": "h"},
        "summary": {"files": 1, "certifiable": 0, "uncertain": 1, "error": 0, "avg_score": 70},
        "files": [{"file": "a.docx", "engine": ".net/office", "status": "uncertain", "score": 70,
                   "compliant": 0, "skipped_rules": 0, "checksum": "x", "issues": []}],
    })
    isolated_store.record_remediation("s-sc", "a.docx", corrected_sha256="c" * 64)
    isolated_store.record_remediation_diffs("s-sc", "a.docx", [
        {"rule_id": "1.1.1", "before": "", "after": "A barn"}])
    isolated_store.log_decision(
        "system", "apply.saved_unverified", scan_id="s-sc", file="a.docx", rule_id="1.1.1",
        detail=json.dumps({"artifact_sha256": "c" * 64, "rule_id": "1.1.1",
                           "changes": [{"locator": "docx:image:2", "before": "", "after": "A cow"}]}))
    changes = rf.build_file_facts(isolated_store, "s-sc", "a.docx", owner="o@x.org")["savedChanges"]
    verified = next(c for c in changes if c["verification"] == "verified")
    unverified = next(c for c in changes if c["verification"] == "not_verified")
    assert verified["location"] is None and verified["locator"] is None
    assert unverified["location"]["label"] == "Image 2"
    assert unverified["location"]["objectId"] == "image:2"

    real = type(isolated_store).get_remediation_diffs
    monkeypatch.setattr(type(isolated_store), "get_remediation_diffs",
                        lambda self, sid, f: [{**d, "locator": "docx:image:1", "page": 3}
                                              for d in real(self, sid, f)])
    changes = rf.build_file_facts(isolated_store, "s-sc", "a.docx", owner="o@x.org")["savedChanges"]
    verified = next(c for c in changes if c["verification"] == "verified")
    assert verified["location"]["objectId"] == "image:1"
    assert verified["location"]["page"] is None          # a Word object never carries a page
    assert verified["locationSource"] == "recorded"

    # The owner's R1 contract also returns legacy locators reconstructed from a writer note
    # ("image 1", `location_source: "legacy_note"`): kept verbatim, labelled from the raw text,
    # and flagged — never promoted to a structured claim it cannot support.
    monkeypatch.setattr(type(isolated_store), "get_remediation_diffs",
                        lambda self, sid, f: [{**d, "locator": "image 1", "page": None,
                                               "location_source": "legacy_note"}
                                              for d in real(self, sid, f)])
    changes = rf.build_file_facts(isolated_store, "s-sc", "a.docx", owner="o@x.org")["savedChanges"]
    verified = next(c for c in changes if c["verification"] == "verified")
    assert verified["locationSource"] == "legacy_note"
    assert verified["location"]["label"] == "image 1" and verified["location"]["kind"] is None

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

TABLE = json.loads((ACP / "tests/fixtures/report_location_cases.json").read_text())
CASES = TABLE["cases"]
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


def test_the_legacy_note_qualifier_is_the_shared_text():
    """reportEvidence.js adds the same qualifier to a raw diff row the browser labels itself;
    both sides are pinned to the fixture, so a label is never qualified twice or differently."""
    assert report_location.LEGACY_NOTE_QUALIFIER == TABLE["legacyNoteQualifier"]


def test_a_saved_change_location_says_where_it_came_from():
    loc = report_location.saved_change_location("pdf:fig:2:0", 2, "pdf", "recorded")
    assert (loc["label"], loc["page"], loc["source"]) == ("Page 2 · figure 1", 2, "recorded")
    # legacy_note: the writer's words, flagged — and no page, not even one the text states
    loc = report_location.saved_change_location("pdf:fig:2:0", None, "pdf", "legacy_note")
    assert loc["page"] is None and loc["source"] == "legacy_note"
    assert loc["label"] == "Page 2 · figure 1" + TABLE["legacyNoteQualifier"]
    assert report_location.saved_change_location("word:p:2", None, "docx", None) is None
    assert report_location.saved_change_location("word:p:2", None, "docx", "guessed") is None


def test_no_label_glues_a_machine_string_to_a_page():
    """rf-C's finding: an unrecognised locator beside a page read "Page N · <raw>"."""
    for raw in ("/Document/Sect[2]/Figure", "rId5", "a_b", "x:y:z", "#frag", "{guid}"):
        loc = report_location.parse_location(raw, 4, "pdf")
        assert loc["label"] == "Page 4" and loc["raw"] == raw, raw


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


def _seed_one(store, sid, name, engine=".net/office"):
    store.save_scan({
        "_scan_id": sid, "started_at": "2026-09-01T05:00:00+00:00",
        "completed_at": "2026-09-01T05:05:00+00:00", "source": "local", "owner": "o@x.org",
        "rubric": {"name": "wcag-aa", "hash": "h"},
        "summary": {"files": 1, "certifiable": 0, "uncertain": 1, "error": 0, "avg_score": 70},
        "files": [{"file": name, "engine": engine, "status": "uncertain", "score": 70,
                   "compliant": 0, "skipped_rules": 0, "checksum": "x", "issues": []}],
    })
    store.record_remediation(sid, name, corrected_sha256="c" * 64)


def _by_rule(facts):
    return {c["ruleId"]: c for c in facts["savedChanges"] if c["verification"] == "verified"}


def test_verified_changes_read_the_real_r1_store_columns_and_legacy_notes(isolated_store):
    """R1 on the REAL store (schema v59): diffs recorded through record_remediation_diffs with a
    locator/page come back through get_remediation_diffs with `location_source`, and the facts
    carry `location` + `location.source`. No monkeypatching: this is the path production takes."""
    import report_facts as rf
    _seed_one(isolated_store, "s-r1", "a.docx")
    isolated_store.record_remediation_diffs("s-r1", "a.docx", [
        # the approved-alt writer's part#fragment locator
        {"rule_id": "1.1.1", "before": "", "after": "Revenue by quarter",
         "locator": "word/document.xml#rId7"},
        # a structural token with a page the Word writer should never have sent: a Word page is
        # a layout artefact, so it is stored (the store takes any positive int) but never shown
        {"rule_id": "1.3.1", "before": "b", "after": "a", "locator": "word:p:4", "page": 3},
        # a legacy row: no columns, but the exact handlers commit_credit note prefix
        {"rule_id": "1.4.5", "before": "", "after": "A cow", "note": "approved by a reviewer · image 1"},
        # nothing recorded at all
        {"rule_id": "2.4.2", "before": "", "after": "Title", "note": "set the title"},
    ])
    rows = _by_rule(rf.build_file_facts(isolated_store, "s-r1", "a.docx", owner="o@x.org"))

    alt = rows["1.1.1"]
    assert alt["locationSource"] == "recorded" and alt["location"]["source"] == "recorded"
    assert alt["location"]["label"] == "Image (relationship rId7) · document body"
    assert alt["location"]["objectId"] == "word/document.xml#rId7"
    assert alt["location"]["page"] is None

    para = rows["1.3.1"]
    assert para["location"]["label"] == "Paragraph 4"      # 1-based writer token, no +1
    assert para["location"]["page"] is None and "Page" not in para["location"]["label"]
    assert para["locationSource"] == "recorded"

    legacy = rows["1.4.5"]
    assert legacy["locationSource"] == "legacy_note" and legacy["location"]["source"] == "legacy_note"
    assert legacy["location"]["label"] == "image 1" + __import__("report_location").LEGACY_NOTE_QUALIFIER
    assert legacy["location"]["page"] is None and legacy["locator"] == "image 1"

    none = rows["2.4.2"]
    assert none["location"] is None and none["locator"] is None and none["locationSource"] is None


def test_verified_pdf_changes_name_their_recorded_page_and_nothing_else(isolated_store):
    import report_facts as rf
    _seed_one(isolated_store, "s-r1p", "p.pdf", engine="pdf")
    isolated_store.record_remediation_diffs("s-r1p", "p.pdf", [
        {"rule_id": "1.1.1", "before": "", "after": "Chart", "locator": "pdf:fig:2:0", "page": 2},
        {"rule_id": "1.4.3", "before": "grey", "after": "black", "page": 5},
        # a legacy note at the 500-character cap may have been cut: the store yields NO location
        {"rule_id": "2.4.4", "before": "", "after": "x",
         "note": "AI applied; exact saved copy subsequently verified · " + "y" * 460},
        # a legacy PDF locator: kept as the writer's string, flagged, and never given the page
        # column the store says it does not have
        {"rule_id": "4.1.2", "before": "", "after": "Name", "note": "approved by a reviewer · caption area"},
    ])
    rows = _by_rule(rf.build_file_facts(isolated_store, "s-r1p", "p.pdf", owner="o@x.org"))
    assert rows["1.1.1"]["location"]["label"] == "Page 2 · figure 1"
    assert rows["1.1.1"]["location"]["page"] == 2
    assert rows["1.4.3"]["location"]["label"] == "Page 5" and rows["1.4.3"]["locator"] is None
    assert rows["2.4.4"]["location"] is None and rows["2.4.4"]["locationSource"] is None
    legacy = rows["4.1.2"]["location"]
    assert legacy["page"] is None and legacy["source"] == "legacy_note"
    assert legacy["label"].startswith("caption area (from the saving step's note")


def test_a_row_the_store_does_not_vouch_for_is_not_given_a_location(monkeypatch):
    """`location_source` is authoritative: None (or an unknown value) → no location, even if a
    caller hands a locator through. Only a pre-R1 row (no key at all) is classified locally.
    (A hand-built store on purpose: the real store cannot produce these rows.)"""
    import report_facts as rf
    import unverified_changes

    class S:
        def __init__(self, rows):
            self.rows = rows

        def get_remediation_diffs(self, sid, f):
            return self.rows

    monkeypatch.setattr(unverified_changes, "saved_changes", lambda *a, **k: [])
    for source in (None, "inferred"):
        rows, *_ = rf.build_saved_changes(S([{"rule_id": "1.1.1", "seq": 0, "locator": "word:p:2",
                                              "page": 2, "location_source": source}]),
                                          "s", "a.docx", {})
        assert rows[0]["location"] is None and rows[0]["locationSource"] is None
    rows, *_ = rf.build_saved_changes(S([{"rule_id": "1.1.1", "seq": 0, "locator": "word:p:2"}]),
                                      "s", "a.docx", {})
    assert rows[0]["location"]["label"] == "Paragraph 2"
    assert rows[0]["locationSource"] == "recorded"


def test_saved_changes_carry_a_location_and_verified_ones_read_the_r1_locator(isolated_store):
    """Contract 1 for savedChanges: the unverified record has its own locator; a verified row with
    no recorded location is `None` ("Location not recorded")."""
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
    assert verified["locationSource"] is None
    assert unverified["location"]["label"] == "Image 2"
    assert unverified["location"]["objectId"] == "image:2"
    assert unverified["location"]["source"] == "recorded"

"""Per-fix before→after diff collection (certification report "Before → After").

The Office/HTML remediators optionally append {rule_id, before, after, note} records
for every fix they apply, WITHOUT changing their (path/text, applied, skipped) return
contract. These tests assert both: the diffs describe the real change, and omitting the
collector is a no-op (so every existing caller/test is unaffected).
"""
import shutil
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "api"))
import remediate_office  # noqa: E402
from remediate import remediate_html  # noqa: E402

_CORPUS = Path(__file__).resolve().parent.parent / "test-corpus/oracle"


def _diff_scs(diffs):
    return {d["rule_id"] for d in diffs}


def _well_formed(diffs):
    for d in diffs:
        # `locator`/`page` are optional and present only when the producer knew them (R1); an
        # unknown location is an absent key, never an empty or placeholder value.
        assert {"rule_id", "before", "after", "note"} <= set(d) <= {
            "rule_id", "before", "after", "note", "locator", "page"}
        if "locator" in d:
            assert isinstance(d["locator"], str) and d["locator"].strip()
        if "page" in d:
            assert type(d["page"]) is int and d["page"] > 0
        assert d["rule_id"] and "." in d["rule_id"]        # dotted WCAG SC
        assert isinstance(d["before"], str) and isinstance(d["after"], str)
        assert d["before"] != d["after"]                   # a diff must actually differ


def test_office_diffs_describe_real_changes(tmp_path):
    src = tmp_path / "xlsx-synthetic-violations.xlsx"
    shutil.copy(_CORPUS / "xlsx-synthetic-violations.xlsx", src)
    diffs = []
    out, applied, _ = remediate_office.remediate_office(src, diffs=diffs)
    assert out is not None and applied
    _well_formed(diffs)
    # Language + title + table-header + hidden-content fixes all record a before/after.
    assert {"3.1.1", "2.4.2", "1.3.1", "1.3.2"} <= _diff_scs(diffs)
    # The "after" for language is the value actually written into the file.
    lang = next(d for d in diffs if d["rule_id"] == "3.1.1")
    assert lang["after"] == "en-US"
    hdr = next(d for d in diffs if d["rule_id"] == "1.3.1")
    assert 'headerRowCount="1"' in hdr["after"]


def test_office_diffs_default_is_noop_and_matches_applied(tmp_path):
    """Omitting the collector keeps the 3-tuple contract; passing one never invents a
    fix the applied log didn't also report."""
    src = tmp_path / "pptx-reading-order.pptx"
    shutil.copy(_CORPUS / "pptx-reading-order.pptx", src)

    # No collector → unchanged 3-tuple, still a valid package.
    out0, applied0, _ = remediate_office.remediate_office(src)
    assert out0 is not None and zipfile.is_zipfile(out0)

    src2 = tmp_path / "pptx-reading-order-2.pptx"
    shutil.copy(_CORPUS / "pptx-reading-order.pptx", src2)
    diffs = []
    out1, applied1, _ = remediate_office.remediate_office(src2, diffs=diffs)
    assert out1 is not None
    # Every recorded SC corresponds to something in the applied log (no fabricated diffs).
    assert _diff_scs(diffs)
    joined = " ".join(applied1)
    assert any(d["rule_id"] in joined or d["rule_id"] == "2.4.2" for d in diffs)
    # Reading-order note captured for this fixture.
    assert "1.3.2" in _diff_scs(diffs)


def test_html_diffs_capture_before_and_after():
    html = ('<html><head></head><body><h1>Q3 Report</h1>'
            '<p style="color:#bbbbbb">faint</p>'
            '<input name="email">'
            '<meta name="viewport" content="user-scalable=no"></body></html>')
    diffs = []
    fixed, applied, _ = remediate_html(html, diffs=diffs)
    _well_formed(diffs)
    scs = _diff_scs(diffs)
    assert {"3.1.1", "2.4.2", "1.4.3", "1.4.4"} <= scs
    contrast = next(d for d in diffs if d["rule_id"] == "1.4.3")
    assert "#bbbbbb" in contrast["before"] and "#111111" in contrast["after"]
    title = next(d for d in diffs if d["rule_id"] == "2.4.2")
    assert title["after"] == "Q3 Report"   # derived from the page's <h1>


def test_html_default_no_collector_unchanged():
    html = "<html><head></head><body><h1>x</h1></body></html>"
    fixed, applied, deferred = remediate_html(html)   # 3-tuple, no diffs arg
    assert isinstance(fixed, str) and isinstance(applied, list)

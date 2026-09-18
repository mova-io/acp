"""The two PDF write-back lanes, proved end to end: 1.1.1 figure alt text and 4.1.2 field names.

One module because they are one package format, one writer entry point
(`remediate_pdf.apply_pdf_approved`, which dispatches on the locator's prefix) and one harness;
two sections because they are different criteria, written into different PDF objects — `/Alt` on
a figure's structure element, `/TU` on an AcroForm field.

Same bar as the Office lanes: the original document trips the finding, an approval changes the
saved document, a REAL re-scan verifies it, unrelated content survives, and a failed write earns
no credit. Nothing but the blob store is patched — `handlers._apply_approved_values` runs the
production seam with `_verify_residual_scs` UNPATCHED.

WHAT PDF ADDS THAT NO OFFICE LANE COULD SHOW. These fixtures fail SEVERAL criteria at once: the
tagged PDF reports 1.1.1, 1.4.11, 2.4.2 and 3.1.1; the form reports 1.3.1, 2.4.2, 2.4.3 and
3.1.1 alongside 4.1.2. So "the lane cleared its criterion" and "the lane cleared everything" are
distinguishable here, and `test_only_the_lanes_own_criterion_changes` asserts the difference
directly. On the Office fixtures, which fail one or two criteria, a writer that somehow silenced
the whole scan would look identical to one that fixed the right thing.

WHY THERE IS NO BROKEN-ANALYSER SECTION, stated rather than quietly omitted. The Office proofs
each re-assert the guarantee #1058 established: an Office CLI that cannot launch, dies or hangs
must still leave a real residual, because the fail-open lived in that shared seam. PDF does not
shell out — `_analyse_pdf` runs pikepdf in-process — so those three cases have no PDF analogue
and asserting them here would be theatre.

The PDF-shaped version of that risk is real and is NOT closed by this file: an unreadable PDF
scans as ZERO findings rather than as an error, so `residual` comes back empty, every criterion
reads as cleared, and credit is granted. Measured, on bytes an applier could in principle
produce:

    healthy pdf (fails 1.1.1) -> ['1.1.1', '1.4.11', '2.4.2', '3.1.1']
    truncated pdf             -> []

That is a defect in the shared verification seam (`proposals.verify_residual_scs` keeps only
`issues` and discards the scan's own `status: "error"`), it affects every format equally rather
than PDF alone, and it is reported separately. It is deliberately NOT asserted here in either
direction: pinning the current behaviour would make a test that requires the defect to remain.
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import pytest

ACP = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ACP / "api"))

# The (format, criterion) lanes this module PROVES end to end — read by
# tests/test_capability_assisted_contract.py, which derives the applier registry from
# these declarations instead of a hand-written list. A literal set, so it can be read
# without importing this module.
PROVES_LANES = {("pdf", "1.1.1"), ("pdf", "4.1.2"), ("pdf", "3.1.2")}
sys.path.insert(0, str(ACP / "tests"))

pytest.importorskip("reportlab")
pytest.importorskip("pikepdf")

from test_pdf_figure_alt_approval import _alts, _tagged_pdf   # noqa: E402
from test_pdf_form_field_names import _form_pdf, _tus         # noqa: E402
from hitl_viewed import approve_bound

FILE = "report.pdf"
SID = "rv-pdf"

FIRST_ALT = "A bar chart of Q3 findings, grouped by severity."
SECOND_ALT = "A photograph of the accessibility working group."
FIRST_NAME = "Full name"
SECOND_NAME = "Email address"


def _tagged(n: int = 2) -> bytes:
    p = Path(tempfile.mkdtemp()) / FILE
    _tagged_pdf(p, n)
    return p.read_bytes()


def _form(names: list[str]) -> bytes:
    p = Path(tempfile.mkdtemp()) / FILE
    _form_pdf(p, names)
    return p.read_bytes()


def _assess(data: bytes) -> set[str]:
    from assessment_policy import _extract_sc
    from scanner import analyse_and_assess
    with tempfile.TemporaryDirectory() as d:
        (Path(d) / FILE).write_bytes(data)
        fd, _ = analyse_and_assess(Path(d), FILE, detect_pii=False)
    return {sc for i in (fd or {}).get("issues", []) if (sc := _extract_sc(i.get("wcag", "")))}


class _Blob:
    def __init__(self, data: bytes):
        self.data, self.uploads = data, []

    def download_remediated(self, owner, sid, f):
        return self.data

    def upload_remediated(self, owner, sid, f, data, mime):
        self.data = data
        self.uploads.append((f, mime))
        return "http://b/2"
    # The approved writer publishes digest-scoped and moves the pointer at commit.
    def upload_immutable_retry(self, owner, sid, f, data, mime):
        return self.upload_remediated(owner, sid, f, data, mime)


@pytest.fixture()
def store(monkeypatch):
    import store as store_mod
    monkeypatch.setattr(store_mod, "_SQLITE_PATH", Path(tempfile.mkdtemp()) / "rv.db")
    return store_mod.Store()


def _seed(store, *, sc: str, rule_name: str, rule_id: str, values: dict[str, str]) -> str:
    store.init_scan_run(SID, "drive", 1, "2026-08-31T00:00:00Z", "rubric", "hash")
    store.save_file_result(SID, {
        "file": FILE, "engine": "python/pdf", "status": "pass", "score": 60, "compliant": 0,
        "skipped_rules": 0, "drive_file_id": "d1",
        "issues": [{"ruleId": rule_id, "wcag": f"{sc} {rule_name}", "severity": "SERIOUS",
                    "locator": loc} for loc in values],
    }, "2026-08-31T00:00:00Z")
    store.record_remediation(SID, FILE, drive_write_url="http://d/1", blob_url="http://b/1")
    item_id = store.enqueue_proposals(SID, FILE, sc, [
        {"locator": loc, "before": "(nothing)", "proposed_value": "", "rationale": "r",
         "source": "reviewer"} for loc in values], rule_name=rule_name)
    approve_bound(store, item_id, list(values.values()))
    return item_id


def _run_lane(monkeypatch, store, blob):
    """The production handler, with the re-scan UNPATCHED."""
    import core
    import handlers
    monkeypatch.setattr(core, "store", store)
    monkeypatch.setitem(sys.modules, "blob", blob)
    handlers._apply_approved_values({"scan_id": SID, "file": FILE}, {})


def _seed_alt(store, values):
    return _seed(store, sc="1.1.1", rule_name="Non-text Content",
                 rule_id="pdf.missing-alt-text", values=values)


def _seed_field(store, values):
    return _seed(store, sc="4.1.2", rule_name="Name, Role, Value",
                 rule_id="PDF_FORM_NO_ACCESSIBLE_NAME", values=values)


# ══ 1.1.1 — /Alt on a tagged figure ═══════════════════════════════════════════

def test_a_real_assessment_reports_1_1_1_on_undescribed_figures():
    assert "1.1.1" in _assess(_tagged(2))


def test_the_figures_start_with_no_alt():
    """The fixture's precondition, read out of the PDF rather than assumed — if the builder ever
    started writing /Alt, every assertion below would be about a document that never failed."""
    assert _alts(_tagged(2)) == ["", ""]


@pytest.fixture()
def alt_applied(store, monkeypatch):
    original = _tagged(2)
    blob = _Blob(original)
    _seed_alt(store, {"pdf:fig:1:0": FIRST_ALT, "pdf:fig:1:1": SECOND_ALT})
    _run_lane(monkeypatch, store, blob)
    return blob, store, original


def test_the_saved_pdf_carries_both_approved_descriptions(alt_applied):
    blob, _, _ = alt_applied
    assert _alts(blob.data) == [FIRST_ALT, SECOND_ALT]


def test_a_second_real_assessment_no_longer_reports_1_1_1(alt_applied):
    """THE claim: a fresh assessment of the SAVED bytes, not the writer's return value."""
    blob, _, _ = alt_applied
    assert "1.1.1" not in _assess(blob.data)


def test_only_the_lanes_own_criterion_changes(alt_applied):
    """What a multi-failure fixture makes visible and a single-failure one cannot: the PDF also
    fails 1.4.11, 2.4.2 and 3.1.1, and it still does. A write that silenced the whole scan —
    or bytes that stopped parsing — would clear 1.1.1 too, and read as success everywhere else
    in this file."""
    blob, _, original = alt_applied
    before, after = _assess(original), _assess(blob.data)
    assert before - after == {"1.1.1"}, (before, after)
    assert after, "the corrected PDF reports nothing at all — it may no longer be readable"


def test_the_row_is_credited_and_the_copy_is_stored(alt_applied):
    blob, store, _ = alt_applied
    assert store.count_unapplied_approved_values(SID, FILE) == 0
    assert blob.uploads


def test_a_partial_alt_write_is_not_credited(store, monkeypatch):
    """Two figures, one described. The write succeeds and the PDF genuinely improves — and
    1.1.1 still fails, because the other figure is still silent."""
    from remediate_pdf import apply_pdf_approved
    original = _tagged(2)

    written, ap, _ = apply_pdf_approved(original, {"pdf:fig:1:0": FIRST_ALT})
    assert ap, "the writer refused the value, so this control is not about crediting"
    assert _alts(written) == [FIRST_ALT, ""]
    assert "1.1.1" in _assess(written), (
        "describing one of two figures cleared the criterion — this control cannot distinguish "
        "a withheld credit from a cleared one")

    blob = _Blob(original)
    _seed_alt(store, {"pdf:fig:1:0": FIRST_ALT})
    _run_lane(monkeypatch, store, blob)

    assert store.count_unapplied_approved_values(SID, FILE) == 1
    assert store.mark_file_compliant_if_reviewed(SID, FILE) is False
    assert not blob.uploads
    assert blob.data == original


def test_an_approval_aimed_at_a_figure_that_is_not_there_is_not_credited(store, monkeypatch):
    original = _tagged(2)
    blob = _Blob(original)
    _seed_alt(store, {"pdf:fig:9:9": FIRST_ALT})
    _run_lane(monkeypatch, store, blob)

    assert blob.data == original
    assert store.count_unapplied_approved_values(SID, FILE) == 1
    assert not blob.uploads


# ══ 4.1.2 — /TU on an AcroForm field ══════════════════════════════════════════

def test_a_real_assessment_reports_4_1_2_on_unnamed_form_fields():
    assert "4.1.2" in _assess(_form(["fld1", "fld2"]))


def test_the_fields_start_with_no_tooltip():
    assert _tus(_form(["fld1", "fld2"])) == {"fld1": "", "fld2": ""}


@pytest.fixture()
def field_applied(store, monkeypatch):
    original = _form(["fld1", "fld2"])
    blob = _Blob(original)
    _seed_field(store, {"pdf:field:1:0": FIRST_NAME, "pdf:field:1:1": SECOND_NAME})
    _run_lane(monkeypatch, store, blob)
    return blob, store, original


def test_the_saved_pdf_carries_both_approved_field_names(field_applied):
    blob, _, _ = field_applied
    assert _tus(blob.data) == {"fld1": FIRST_NAME, "fld2": SECOND_NAME}


def test_a_second_real_assessment_no_longer_reports_4_1_2(field_applied):
    blob, _, _ = field_applied
    assert "4.1.2" not in _assess(blob.data)


def test_the_form_lane_also_leaves_the_other_criteria_alone(field_applied):
    """The form PDF fails 1.3.1, 2.4.2, 2.4.3 and 3.1.1 as well, and a field name fixes none of
    them."""
    blob, _, original = field_applied
    before, after = _assess(original), _assess(blob.data)
    assert before - after == {"4.1.2"}, (before, after)
    assert after


def test_the_field_row_is_credited_and_the_copy_is_stored(field_applied):
    blob, store, _ = field_applied
    assert store.count_unapplied_approved_values(SID, FILE) == 0
    assert blob.uploads


def test_a_partial_field_write_is_not_credited(store, monkeypatch):
    """Two fields, one named. 4.1.2 still fails on the other."""
    from remediate_pdf import apply_pdf_field_name
    original = _form(["fld1", "fld2"])

    written, ap, _ = apply_pdf_field_name(original, {"pdf:field:1:0": FIRST_NAME})
    assert ap
    assert _tus(written) == {"fld1": FIRST_NAME, "fld2": ""}
    assert "4.1.2" in _assess(written), (
        "naming one of two fields cleared the criterion — this control cannot distinguish a "
        "withheld credit from a cleared one")

    blob = _Blob(original)
    _seed_field(store, {"pdf:field:1:0": FIRST_NAME})
    _run_lane(monkeypatch, store, blob)

    assert store.count_unapplied_approved_values(SID, FILE) == 1
    assert not blob.uploads
    assert blob.data == original


def test_a_pdf_whose_fields_are_already_named_is_left_alone(store, monkeypatch):
    """Nothing to write, so nothing is written and nothing is published."""
    from remediate_pdf import apply_pdf_field_name
    named, _, _ = apply_pdf_field_name(_form(["fld1", "fld2"]),
                                       {"pdf:field:1:0": FIRST_NAME,
                                        "pdf:field:1:1": SECOND_NAME})
    assert "4.1.2" not in _assess(named)

    blob = _Blob(named)
    _seed_field(store, {})
    _run_lane(monkeypatch, store, blob)
    assert blob.data == named and not blob.uploads


def test_exact_tagged_language_approval_saved_and_verified(store, monkeypatch):
    from test_pdf_structural_language import fixture as language_pdf, targets
    data = language_pdf()
    target = targets(data)[0]
    assert "3.1.2" in _assess(data)
    blob = _Blob(data)
    _seed(store, sc="3.1.2", rule_name="Language of Parts", rule_id="PDF_PART_LANGUAGE", values={target.locator: "fr-FR"})
    _run_lane(monkeypatch, store, blob)
    assert blob.uploads
    assert targets(blob.data)[0].current_language == "fr-FR"
    assert "3.1.2" not in _assess(blob.data)
    assert store.count_unapplied_approved_values(SID, FILE) == 0

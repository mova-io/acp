"""PDF write-back: a reviewer-approved value must reach the PDF, not just the database.

`apply_pdf_approved` worked and was covered — but nothing in production called it. The chain was
broken in two places at once, so a PDF approval was stranded end to end:

  1. `_fix_pdf_figure_alt` / `_fix_pdf_form_fields` built per-figure and per-field review cards,
     and handlers._remediate_file dropped them: it routed only reading-order / structure-map /
     headings-map / link-purpose, so the cards never reached a HITL row and the reviewer never
     saw them. The deferral existed only as a tally.
  2. `_apply_approved_values` returned early for anything outside `_OFFICE_ALT_MIME`, logging
     "apply.unsupported" — so even a value approved by some other route was never written.

Either break alone is enough to make `mark_file_compliant_if_reviewed` refuse forever: the file
is approved, uncertified and unpublishable. This is the same stranded-approval failure
api/apply_alt.py was written to close for Office.

These pin both links, plus the property that would have caught break (1) generically: every
proposal kind remediate_pdf can emit must be routed somewhere by the handler.
"""
from __future__ import annotations
import ast
import io
import sys
import tempfile
from pathlib import Path

import pytest

pikepdf = pytest.importorskip("pikepdf")
pytest.importorskip("reportlab")

ACP = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ACP / "api"))
sys.path.insert(0, str(ACP / "tests"))

from test_pdf_figure_alt_approval import _alts, _tagged_pdf  # noqa: E402
from test_pdf_form_field_names import _form_pdf, _tus  # noqa: E402
from hitl_viewed import approve_bound

FILE = "report.pdf"
SID = "s-pdf"


@pytest.fixture()
def store(monkeypatch):
    import store as store_mod
    monkeypatch.setattr(store_mod, "_SQLITE_PATH", Path(tempfile.mkdtemp()) / "apply-pdf.db")
    return store_mod.Store()


class _Blob:
    """Stands in for Azure Blob: the remediated copy lives here and the applier rewrites it."""
    def __init__(self, data): self.data, self.uploads = data, []
    def download_remediated(self, owner, sid, f): return self.data
    def upload_remediated(self, owner, sid, f, data, mime):
        self.data = data; self.uploads.append((f, mime)); return "http://b/2"
    # The approved writer publishes digest-scoped and moves the pointer at commit.
    def upload_immutable_retry(self, owner, sid, f, data, mime):
        return self.upload_remediated(owner, sid, f, data, mime)


def _seed(store, *, sc, rule_name, locators, wcag_rule):
    """A remediated PDF with one HITL row for `sc`, one proposal per locator."""
    store.init_scan_run(SID, "drive", 1, "2026-07-10T00:00:00Z", "rubric", "hash")
    store.save_file_result(SID, {
        "file": FILE, "engine": "pdf", "status": "pass", "score": 40, "compliant": 0,
        "skipped_rules": 0, "drive_file_id": "drv1",
        "issues": [{"ruleId": wcag_rule, "wcag": sc, "severity": "CRITICAL"}],
    }, "2026-07-10T00:00:00Z")
    store.record_remediation(SID, FILE, drive_write_url="http://d/1", blob_url="http://b/1")
    return store.enqueue_proposals(SID, FILE, sc, [
        {"locator": loc, "before": "(nothing)", "proposed_value": "",
         "rationale": "r", "source": "human"}
        for loc in locators
    ], rule_name=rule_name)


def _run_handler(monkeypatch, store, blob, *, residual):
    """Drive the handler with Blob stubbed. `residual` is the re-scan verdict; pass the
    sentinel "real" to run the ACTUAL residual re-scan instead of stubbing it."""
    import core
    import handlers
    monkeypatch.setattr(core, "store", store)
    monkeypatch.setitem(sys.modules, "blob", blob)
    if residual != "real":
        from proposals import Verification
        monkeypatch.setattr(handlers, "_verify_residual",
                            lambda b, f, **kwargs: Verification(True, residual or ()))
    handlers._apply_approved_values({"scan_id": SID, "file": FILE}, {})


# ── break (1): the review card must reach a HITL row at all ───────────────────

def test_every_pdf_proposal_kind_is_routed_to_a_review_row():
    """A `kind` remediate_pdf emits but handlers never routes is a card built and thrown away.

    Asserted structurally rather than by example so the NEXT proposer to land is covered
    without editing this file — which is exactly how pdf-figure-alt and pdf-field-name were
    lost: both were written, tested at the unit level, and silently dropped by the handler.
    """
    emitted = {
        kw.value.value
        for node in ast.walk(ast.parse((ACP / "api" / "remediate_pdf.py").read_text()))
        if isinstance(node, ast.Call)
        for kw in node.keywords
        if kw.arg == "kind" and isinstance(kw.value, ast.Constant)
    }
    src = (ACP / "api" / "handlers.py").read_text()
    routed = {
        node.comparators[0].value
        for node in ast.walk(ast.parse(src))
        if isinstance(node, ast.Compare)
        and isinstance(node.left, ast.Call)
        and getattr(node.left.func, "attr", None) == "get"
        and node.left.args and getattr(node.left.args[0], "value", None) == "kind"
        and isinstance(node.comparators[0], ast.Constant)
    }
    assert emitted, "no proposal kinds found in remediate_pdf — the parse broke, not the code"
    assert not (emitted - routed), (
        "remediate_pdf builds these review cards and handlers._remediate_file drops them, so "
        f"no reviewer ever sees them: {sorted(emitted - routed)}")


# ── break (2): the approved value must reach the document ────────────────────

def test_approved_figure_alt_is_written_into_the_pdf(store, monkeypatch, tmp_path):
    """THE regression: the reviewer's alt text lands on the figure it was written for."""
    src = tmp_path / FILE
    _tagged_pdf(src, n_figs=2)
    blob = _Blob(src.read_bytes())
    item_id = _seed(store, sc="1.1.1", rule_name="Non-text Content",
                    locators=["pdf:fig:1:0", "pdf:fig:1:1"], wcag_rule="pdf.missing-alt-text")
    approve_bound(store, item_id, ["Bar chart of revenue by region", "Team photo"])

    assert store.mark_file_compliant_if_reviewed(SID, FILE) is False   # nothing written yet

    _run_handler(monkeypatch, store, blob, residual=set())

    assert _alts(blob.data) == ["Bar chart of revenue by region", "Team photo"]
    assert blob.uploads == [(FILE, "application/pdf")]
    assert store.count_unapplied_approved_values(SID, FILE) == 0


def test_approved_field_name_is_written_and_verified_by_a_real_re_scan(store, monkeypatch, tmp_path):
    """4.1.2 end to end with NO stubbed re-scan — the credit rests on real evidence.

    The first-party check emits 4.1.2 for a field with no /TU and stops emitting it once the
    name is written, so the write → verify → credit sequence is doing real work here rather
    than passing vacuously on a criterion nothing observes.
    """
    src = tmp_path / FILE
    _form_pdf(src, ["Text1"])
    blob = _Blob(src.read_bytes())
    item_id = _seed(store, sc="4.1.2", rule_name="Name, Role, Value",
                    locators=["pdf:field:1:0"], wcag_rule="PDF_FORM_NO_ACCESSIBLE_NAME")
    approve_bound(store, item_id, ["Home address"])

    _run_handler(monkeypatch, store, blob, residual="real")

    assert _tus(blob.data) == {"Text1": "Home address"}
    assert blob.uploads == [(FILE, "application/pdf")]
    assert store.count_unapplied_approved_values(SID, FILE) == 0


def test_approved_exact_pdf_language_written_and_rechecked(store, monkeypatch, tmp_path):
    from test_pdf_structural_language import fixture as language_pdf, targets
    from office_structure import checks_for, language_marked_spans
    data = language_pdf()
    target = targets(data)[0]
    path = tmp_path / FILE
    path.write_bytes(data)
    assert any(check.get('wcag') == '3.1.2' and check.get('location') == target.locator
               for check in checks_for(path, '.pdf'))
    blob = _Blob(data)
    item_id = _seed(store, sc='3.1.2', rule_name='Language of Parts',
                    locators=[target.locator], wcag_rule='PDF_PART_LANGUAGE')
    approve_bound(store, item_id, ['fr-FR'])

    _run_handler(monkeypatch, store, blob, residual='real')

    assert targets(blob.data)[0].current_language == 'fr-FR'
    assert blob.uploads == [(FILE, 'application/pdf')]
    assert store.count_unapplied_approved_values(SID, FILE) == 0
    path.write_bytes(blob.data)
    assert 'fr' in language_marked_spans(path, '.pdf')
    assert not any(check.get('wcag') == '3.1.2' for check in checks_for(path, '.pdf'))


@pytest.mark.parametrize('locator_kind', ['stale', 'prose'])
def test_pdf_language_stale_or_prose_locator_is_never_credited(store, monkeypatch, locator_kind):
    from test_pdf_structural_language import fixture as language_pdf, targets
    data = language_pdf()
    locator = targets(data)[0].locator
    if locator_kind == 'stale':
        locator = locator[:-8] + '00000000'
    else:
        locator = 'Bonjour, nous sommes heureux'
    blob = _Blob(data)
    item_id = _seed(store, sc='3.1.2', rule_name='Language of Parts',
                    locators=[locator], wcag_rule='PDF_PART_LANGUAGE')
    approve_bound(store, item_id, ['fr'])

    _run_handler(monkeypatch, store, blob, residual='real')

    assert blob.data == data and blob.uploads == []
    assert store.count_unapplied_approved_values(SID, FILE) == 1
    assert store.mark_file_compliant_if_reviewed(SID, FILE) is False


@pytest.mark.parametrize('wrong_prefix', ['figure', 'field'])
def test_language_approval_cannot_write_other_pdf_properties(store, monkeypatch, tmp_path, wrong_prefix):
    path = tmp_path / FILE
    if wrong_prefix == 'figure':
        _tagged_pdf(path, n_figs=1)
        locator = 'pdf:fig:1:0'
    else:
        _form_pdf(path, ['Text1'])
        locator = 'pdf:field:1:0'
    original = path.read_bytes()
    blob = _Blob(original)
    item_id = _seed(store, sc='3.1.2', rule_name='Language of Parts',
                    locators=[locator], wcag_rule='PDF_PART_LANGUAGE')
    approve_bound(store, item_id, ['fr'])

    _run_handler(monkeypatch, store, blob, residual='real')

    assert blob.data == original and blob.uploads == []
    assert store.count_unapplied_approved_values(SID, FILE) == 1
    assert store.mark_file_compliant_if_reviewed(SID, FILE) is False


def test_a_write_that_does_not_clear_the_criterion_credits_nothing(store, monkeypatch, tmp_path):
    """Credit follows the re-scan, not the write — same honesty rule as the Office lane."""
    src = tmp_path / FILE
    _tagged_pdf(src, n_figs=1)
    blob = _Blob(src.read_bytes())
    item_id = _seed(store, sc="1.1.1", rule_name="Non-text Content",
                    locators=["pdf:fig:1:0"], wcag_rule="pdf.missing-alt-text")
    approve_bound(store, item_id, ["Bar chart of revenue by region"])

    _run_handler(monkeypatch, store, blob, residual={"1.1.1"})   # still failing after the write

    assert blob.uploads == []                                     # nothing stored
    assert store.count_unapplied_approved_values(SID, FILE) == 1  # row stays unapplied
    assert store.mark_file_compliant_if_reviewed(SID, FILE) is False


def test_an_approval_for_a_figure_the_document_lost_is_recorded_not_guessed(store, monkeypatch, tmp_path):
    """A locator that no longer resolves must not be silently treated as honoured."""
    src = tmp_path / FILE
    _tagged_pdf(src, n_figs=1)
    blob = _Blob(src.read_bytes())
    item_id = _seed(store, sc="1.1.1", rule_name="Non-text Content",
                    locators=["pdf:fig:9:9"], wcag_rule="pdf.missing-alt-text")
    approve_bound(store, item_id, ["Alt for a figure that is gone"])

    _run_handler(monkeypatch, store, blob, residual=set())

    assert _alts(blob.data) == [""]      # the surviving figure was NOT given someone else's alt
    assert blob.uploads == []
    assert any(d.get("action") == "apply.unresolved"
               for d in store.list_decisions(SID)), "the stranded approval was not recorded"


def test_alt_and_field_names_are_written_in_one_pass(store, monkeypatch, tmp_path):
    """Both lanes run on the same bytes, so a file with both approved uploads once."""
    src = tmp_path / FILE
    _form_pdf(src, ["Text1"])
    # give the form PDF a tagged figure too
    raw = src.read_bytes()
    pdf = pikepdf.open(io.BytesIO(raw))
    page = pdf.pages[0].obj
    fig = pdf.make_indirect(pikepdf.Dictionary(
        Type=pikepdf.Name("/StructElem"), S=pikepdf.Name("/Figure"), Pg=page, K=0))
    doc = pikepdf.Dictionary(Type=pikepdf.Name("/StructElem"), S=pikepdf.Name("/Document"))
    doc_ref = pdf.make_indirect(doc)
    doc.K = pikepdf.Array([fig])
    fig.P = doc_ref
    st = pdf.make_indirect(pikepdf.Dictionary(Type=pikepdf.Name("/StructTreeRoot"),
                                              K=pikepdf.Array([doc_ref])))
    doc.P = st
    pdf.Root.StructTreeRoot = st
    pdf.Root.MarkInfo = pikepdf.Dictionary(Marked=True)
    out = io.BytesIO(); pdf.save(out); pdf.close()
    blob = _Blob(out.getvalue())

    alt_item = _seed(store, sc="1.1.1", rule_name="Non-text Content",
                     locators=["pdf:fig:1:0"], wcag_rule="pdf.missing-alt-text")
    approve_bound(store, alt_item, ["Bar chart of revenue by region"])
    fld_item = store.enqueue_proposals(SID, FILE, "4.1.2", [
        {"locator": "pdf:field:1:0", "before": "(nothing)", "proposed_value": "",
         "rationale": "r", "source": "human"}], rule_name="Name, Role, Value")
    approve_bound(store, fld_item, ["Home address"])

    _run_handler(monkeypatch, store, blob, residual=set())

    assert _alts(blob.data) == ["Bar chart of revenue by region"]
    assert _tus(blob.data) == {"Text1": "Home address"}
    assert blob.uploads == [(FILE, "application/pdf")], "each lane uploaded its own copy"


def test_the_written_values_are_recorded_as_remediation_diffs(store, monkeypatch, tmp_path):
    """The applied rows must carry before/after, or the certification record shows no change.

    The PDF writers returned {locator, rule_id, value}; the diff recorder reads
    before/after inside a bare `except Exception: pass`, so a mismatch here loses the evidence
    silently rather than failing.
    """
    src = tmp_path / FILE
    _tagged_pdf(src, n_figs=1)
    blob = _Blob(src.read_bytes())
    item_id = _seed(store, sc="1.1.1", rule_name="Non-text Content",
                    locators=["pdf:fig:1:0"], wcag_rule="pdf.missing-alt-text")
    approve_bound(store, item_id, ["Bar chart of revenue by region"])

    _run_handler(monkeypatch, store, blob, residual=set())

    diffs = store.get_remediation_diffs(SID, FILE) or []
    assert [d["after"] for d in diffs] == ["Bar chart of revenue by region"]
    assert diffs[0]["before"] == "(figure had no alt text)"
    assert "pdf:fig:1:0" in diffs[0]["note"]

"""1.1.1 on PDF: a FIRST-PARTY detector, so the write-back lane's credit gate is real evidence.

`RULE_FORMATS` has always listed pdf for 1.1.1, but the only implementation was the partner
catalog's `pdf.missing-alt-text`. The in-process re-scan that grants credit —
proposals.verify_residual_scs, which runs first-party checks only — therefore could not observe
1.1.1 on a PDF at all. So handlers._apply_one_value_kind wrote the reviewer's approved alt text,
asked "is 1.1.1 still failing?", got back a residual set that structurally could never contain
it, and credited the criterion on no evidence. The file certified against a criterion nothing
had checked.

That is precisely the vacuous-credit failure tests/test_applier_detector_parity.py exists to
prevent — reached through the one door that test cannot close, because it accepts a partner
CATALOG rule as proof a detector exists, and the verifier cannot run the partner engine.

These pin the detector itself, its agreement with the remediator (the two must judge the same
figures or the gate breaks in one of two silent directions), and the end-to-end gate running
against the REAL re-scan with nothing stubbed.
"""
from __future__ import annotations
import io
import sys
import tempfile
from pathlib import Path

import pytest

pikepdf = pytest.importorskip("pikepdf")
pytest.importorskip("reportlab")
from reportlab.pdfgen import canvas  # noqa: E402

ACP = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ACP / "api"))
sys.path.insert(0, str(ACP / "tests"))

from formats.pdf import structure  # noqa: E402
from formats.pdf.detectors import non_text_content  # noqa: E402
from test_pdf_figure_alt_approval import _tagged_pdf  # noqa: E402
from hitl_viewed import approve_bound

FILE = "report.pdf"
SID = "s-alt-gate"


def _plain_pdf(path: Path) -> None:
    """A PDF with no /StructTreeRoot at all — untagged."""
    c = canvas.Canvas(str(path))
    c.drawString(72, 720, "no tag tree here")
    c.showPage()
    c.save()


def _set_alt(src: Path, dst: Path, values: dict[int, str]) -> None:
    pdf = pikepdf.open(str(src))
    figs = structure.collect_figures(pdf.Root["/StructTreeRoot"])
    for i, v in values.items():
        figs[i]["/Alt"] = pikepdf.String(v)
    pdf.save(str(dst))
    pdf.close()


# ── the detector ──────────────────────────────────────────────────────────────

def test_a_tagged_figure_with_no_alt_is_reported(tmp_path):
    src = tmp_path / "f.pdf"
    _tagged_pdf(src, n_figs=2)
    findings = non_text_content.detect(src)
    assert len(findings) == 2
    assert all(f["wcag"].startswith("1.1.1") for f in findings)
    assert all(f["ruleId"] == "PDF_FIGURE_NO_ALT" for f in findings)
    assert "page 1" in findings[0]["detail"]


def test_a_described_figure_is_not_reported(tmp_path):
    src = tmp_path / "f.pdf"
    _tagged_pdf(src, n_figs=2)
    done = tmp_path / "done.pdf"
    _set_alt(src, done, {0: "Bar chart of revenue by region", 1: "Team photo"})
    assert non_text_content.detect(done) == []


def test_only_the_undescribed_figure_is_reported(tmp_path):
    src = tmp_path / "f.pdf"
    _tagged_pdf(src, n_figs=3)
    partial = tmp_path / "partial.pdf"
    _set_alt(src, partial, {0: "Bar chart", 2: "Team photo"})
    assert len(non_text_content.detect(partial)) == 1


def test_whitespace_alt_counts_as_undescribed(tmp_path):
    """A figure carrying `/Alt ( )` is not described — crediting it would certify on a space."""
    src = tmp_path / "f.pdf"
    _tagged_pdf(src, n_figs=1)
    blank = tmp_path / "blank.pdf"
    _set_alt(src, blank, {0: "   "})
    assert len(non_text_content.detect(blank)) == 1


def test_an_untagged_pdf_reports_nothing_here(tmp_path):
    """No tag tree means no tagged figures to judge. The missing tree is its own structural
    finding; reporting 1.1.1 too would count one defect twice."""
    src = tmp_path / "plain.pdf"
    _plain_pdf(src)
    assert non_text_content.detect(src) == []


def test_a_file_that_is_not_a_pdf_is_survived(tmp_path):
    junk = tmp_path / "junk.pdf"
    junk.write_bytes(b"not a pdf at all")
    assert non_text_content.detect(junk) == []


# ── detector / remediator agreement ───────────────────────────────────────────

def test_detector_and_remediator_judge_the_same_figures(tmp_path):
    """The gate breaks silently in BOTH directions if these two sets ever diverge:
    a figure only the detector sees can never be cleared, so the file never certifies; a figure
    only the fixer sees clears the criterion while staying undescribed."""
    import remediate_pdf as RP
    src = tmp_path / "f.pdf"
    _tagged_pdf(src, n_figs=3)
    mixed = tmp_path / "mixed.pdf"
    _set_alt(src, mixed, {1: "Described already"})

    pdf = pikepdf.open(str(mixed))
    fixer_view = [f for f in RP._collect_figures(pdf.Root["/StructTreeRoot"])
                  if RP._fig_alt(f) is None]
    detector_view = structure.undescribed_figures(pdf)
    assert len(fixer_view) == len(detector_view) == 2
    # Compared by PDF object identity, not Python id(): each traversal builds fresh pikepdf
    # wrappers, so id() would differ for the very same figure and prove nothing.
    assert [f.objgen for f in fixer_view] == [f.objgen for f in detector_view]
    # …and it is genuinely the two UNdescribed ones, not just two of the three.
    assert all(structure.figure_alt(f) is None for f in detector_view)
    pdf.close()


# ── the gate, end to end, with the real re-scan ───────────────────────────────

@pytest.fixture()
def store(monkeypatch):
    import store as store_mod
    monkeypatch.setattr(store_mod, "_SQLITE_PATH", Path(tempfile.mkdtemp()) / "gate.db")
    return store_mod.Store()


class _Blob:
    def __init__(self, data): self.data, self.uploads = data, []
    def download_remediated(self, owner, sid, f): return self.data
    def upload_remediated(self, owner, sid, f, data, mime):
        self.data = data; self.uploads.append((f, mime)); return "http://b/2"
    # The approved writer publishes digest-scoped and moves the pointer at commit.
    def upload_immutable_retry(self, owner, sid, f, data, mime):
        return self.upload_remediated(owner, sid, f, data, mime)


def _seed(store, locators):
    store.init_scan_run(SID, "drive", 1, "2026-07-10T00:00:00Z", "rubric", "hash")
    store.save_file_result(SID, {
        "file": FILE, "engine": "pdf", "status": "pass", "score": 40, "compliant": 0,
        "skipped_rules": 0, "drive_file_id": "drv1",
        "issues": [{"ruleId": "PDF_FIGURE_NO_ALT", "wcag": "1.1.1", "severity": "CRITICAL"}],
    }, "2026-07-10T00:00:00Z")
    store.record_remediation(SID, FILE, drive_write_url="http://d/1", blob_url="http://b/1")
    return store.enqueue_proposals(SID, FILE, "1.1.1", [
        {"locator": loc, "before": "(nothing)", "proposed_value": "",
         "rationale": "r", "source": "human"} for loc in locators
    ], rule_name="Non-text Content")


def _alts(data: bytes) -> list[str]:
    pdf = pikepdf.open(io.BytesIO(data))
    out = [str(f.get("/Alt", "")) for f in structure.collect_figures(pdf.Root["/StructTreeRoot"])]
    pdf.close()
    return out


def test_the_credit_gate_now_rests_on_a_rescan_that_can_see_1_1_1(store, monkeypatch, tmp_path):
    """THE point of this change: nothing is stubbed, so the credit is real evidence.

    Before the first-party detector this passed for the wrong reason — 1.1.1 was absent from
    the residual set because nothing could emit it, not because the figures were described.
    """
    import core
    import handlers
    from proposals import verify_residual_scs

    src = tmp_path / FILE
    _tagged_pdf(src, n_figs=2)
    assert "1.1.1" in (verify_residual_scs(src.read_bytes(), FILE) or set()), (
        "the re-scan cannot see 1.1.1 on an undescribed PDF — the gate would be vacuous again")

    blob = _Blob(src.read_bytes())
    item_id = _seed(store, ["pdf:fig:1:0", "pdf:fig:1:1"])
    approve_bound(store, item_id, ["Bar chart of revenue by region", "Team photo"])

    monkeypatch.setattr(core, "store", store)
    monkeypatch.setitem(sys.modules, "blob", blob)
    handlers._apply_approved_values({"scan_id": SID, "file": FILE}, {})   # real re-scan

    assert _alts(blob.data) == ["Bar chart of revenue by region", "Team photo"]
    assert blob.uploads == [(FILE, "application/pdf")]
    assert store.count_unapplied_approved_values(SID, FILE) == 0
    assert "1.1.1" not in (verify_residual_scs(blob.data, FILE) or set())


def test_a_partial_write_leaves_1_1_1_failing_and_credits_nothing(store, monkeypatch, tmp_path):
    """The gate's teeth: describe one figure of two and the criterion still fails, so the
    approval is written but never credited and the file stays uncertified. This is the case
    that was previously credited — and certified — on no evidence at all."""
    import core
    import handlers

    src = tmp_path / FILE
    _tagged_pdf(src, n_figs=2)
    blob = _Blob(src.read_bytes())
    item_id = _seed(store, ["pdf:fig:1:0"])           # only the first figure is approved
    approve_bound(store, item_id, ["Bar chart of revenue by region"])

    monkeypatch.setattr(core, "store", store)
    monkeypatch.setitem(sys.modules, "blob", blob)
    handlers._apply_approved_values({"scan_id": SID, "file": FILE}, {})

    assert blob.uploads == []                                     # nothing stored
    assert store.count_unapplied_approved_values(SID, FILE) == 1  # row stays unapplied
    assert store.mark_file_compliant_if_reviewed(SID, FILE) is False
    assert any(d.get("action") == "apply.unverified" for d in store.list_decisions(SID))

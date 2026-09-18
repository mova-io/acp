"""A fix that breaks another criterion must not certify the document as conformant.

Each write-back lane asks only whether ITS criterion cleared — `Verification.cleared(scs)`
intersects the residual with that lane's criteria and ignores the rest — and
`mark_file_compliant_if_reviewed` gated on approvals plus written content, never on the
regressions column. So an approved alt-text write that cleared 1.1.1 and tripped 1.4.3 left the
file at compliant=1, score=100, status='pass', advanced to Publish, and certified against a
criterion it was failing. That is the same shape as the bug the gate's own docstring records
fixing once before, where approval alone certified a deck whose ten images were undescribed.

The block is fail-closed and the queued row is what keeps it from being a dead end: a regression
nobody can see in the inbox is a file nobody can ever publish.
"""
from __future__ import annotations
import io
import sys
import tempfile
import zipfile
from pathlib import Path

import pytest
from hitl_viewed import approve_bound

ACP = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ACP / "api"))

SLIDE = "ppt/slides/slide1.xml"
FILE = "deck.pptx"
SID = "s1"


@pytest.fixture()
def store(monkeypatch):
    import store as store_mod
    monkeypatch.setattr(store_mod, "_SQLITE_PATH", Path(tempfile.mkdtemp()) / "regress.db")
    return store_mod.Store()


def _deck(*names: str) -> bytes:
    pics = "".join(f'<p:pic><p:cNvPr id="{i+2}" name="{n}"/></p:pic>' for i, n in enumerate(names))
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(SLIDE, f"<p:sld>{pics}</p:sld>")
        z.writestr("docProps/core.xml", "<cp:coreProperties xmlns:cp='http://schemas.openxmlformats.org/package/2006/metadata/core-properties'/>")
    return buf.getvalue()


class _Blob:
    def __init__(self, data): self.data, self.uploads = data, []
    def download_remediated(self, owner, sid, f): return self.data
    def upload_remediated(self, owner, sid, f, data, mime):
        self.data = data; self.uploads.append((f, mime)); return "http://b/2"
    # The approved writer publishes digest-scoped and moves the pointer at commit.
    def upload_immutable_retry(self, owner, sid, f, data, mime):
        return self.upload_remediated(owner, sid, f, data, mime)


def _seed(store, *, names=("Picture 1",)):
    """A remediated deck with one approved 1.1.1 row, its decision carrying the vision call."""
    store.init_scan_run(SID, "drive", 1, "2026-07-10T00:00:00Z", "rubric", "hash")
    store.save_file_result(SID, {
        "file": FILE, "engine": "office", "status": "pass", "score": 40, "compliant": 0,
        "skipped_rules": 0, "drive_file_id": "drv1",
        "issues": [{"ruleId": "PPTX-ALT-001", "wcag": "1.1.1", "severity": "CRITICAL"}],
    }, "2026-07-10T00:00:00Z")
    store.record_remediation(SID, FILE, drive_write_url="http://d/1", blob_url="http://b/1")
    item_id = store.enqueue_proposals(SID, FILE, "1.1.1", [
        {"locator": f"{SLIDE}#{n}", "before": "(no alt text)",
         "proposed_value": f"AI draft for {n}", "rationale": "r", "source": "llava"}
        for n in names
    ], rule_name="Non-text Content")
    approve_bound(store, item_id, [])
    call_id = store.record_ai_call(surface="vision", provider="ollama", model="llava:13b",
                                   zone="local", latency_ms=120, ok=True, scan_id=SID, file=FILE)
    store.record_hitl_event(SID, FILE, "1.1.1", item_id, "approve", model_call_id=call_id)
    return item_id


def _sequence(*residuals):
    """Verification(True, r) per call; the FIRST is the pre-write baseline. `Exception` means
    the re-scan could not run."""
    from proposals import Verification
    it = iter(residuals)

    def verify(_bytes, _file, *, scan_id=None):
        r = next(it)
        return (Verification(False, reason="rescan raised X") if r is Exception
                else Verification(True, r))
    return verify


def _run(monkeypatch, store, blob, verify):
    import core, handlers
    monkeypatch.setattr(core, "store", store)
    monkeypatch.setitem(sys.modules, "blob", blob)
    monkeypatch.setattr(handlers, "_verify_residual", verify)
    handlers._apply_approved_values({"scan_id": SID, "file": FILE}, {})


def _regressed_rows(store):
    return [r for r in store.list_hitl_queue(scan_id=SID, include_superseded=True)
            if str(r.get("rule_id") or "").endswith("/regressed")]


# ── the block ─────────────────────────────────────────────────────────────────

def test_a_write_that_breaks_another_criterion_does_not_certify_the_file(store, monkeypatch):
    """1.1.1 cleared, 1.4.3 newly failing. The write is kept — the alt text is a real
    improvement and discarding it helps nobody — but the document is not conformant and must
    not say it is."""
    _seed(store)
    blob = _Blob(_deck("Picture 1"))
    _run(monkeypatch, store, blob, _sequence({"1.1.1"}, {"1.4.3"}))

    rec = store.get_file_record(SID, FILE)
    assert rec["compliant"] == 0, "certified while failing 1.4.3"
    assert rec.get("score") != 100
    assert blob.uploads, "the corrected copy is still stored; only the claim is withheld"
    assert store.unresolved_regression(SID, FILE) is True
    assert [d["action"] for d in store.list_decisions(scan_id=SID)].count("file.certified") == 0


def test_the_regression_is_raised_as_review_work_naming_the_criterion(store, monkeypatch):
    _seed(store)
    _run(monkeypatch, store, _Blob(_deck("Picture 1")), _sequence({"1.1.1"}, {"1.4.3"}))

    rows = _regressed_rows(store)
    assert [r["rule_id"] for r in rows] == ["1.4.3/regressed"]
    assert rows[0]["status"] == "pending"
    assert "1.4.3" in rows[0]["rule_name"]


def test_the_regression_row_is_visible_rather_than_superseded(store, monkeypatch):
    """The trap this design exists around. 1.4.3 PASSED at scan time — that is why it was never
    a review item — so a row keyed on the bare criterion is retracted by _superseded_items the
    moment it is written, and the reviewer is asked to resolve something they cannot see."""
    _seed(store)
    store.save_file_result(SID, {
        "file": FILE, "engine": "office", "status": "pass", "score": 40, "compliant": 0,
        "skipped_rules": 0, "drive_file_id": "drv1",
        "issues": [{"ruleId": "PPTX-ALT-001", "wcag": "1.1.1", "severity": "CRITICAL"}],
        "rule_traces": [{"rule_id": "1.4.3", "outcome": "PASS", "rule_name": "Contrast"}],
    }, "2026-07-10T00:00:00Z")
    _run(monkeypatch, store, _Blob(_deck("Picture 1")), _sequence({"1.1.1"}, {"1.4.3"}))

    visible = [r["rule_id"] for r in store.list_hitl_queue(scan_id=SID)]
    assert "1.4.3/regressed" in visible, "the regression row was hidden from the inbox"


def test_accepting_the_regression_lets_the_file_certify(store, monkeypatch):
    """The way out. A human looks, decides the contrast trip is acceptable or out of scope, and
    the file publishes — approving owes the document no content, so nothing is left unwritten."""
    _seed(store)
    _run(monkeypatch, store, _Blob(_deck("Picture 1")), _sequence({"1.1.1"}, {"1.4.3"}))
    [row] = _regressed_rows(store)

    store.update_hitl_item(row["id"], "approved", "contrast is a brand mark",
                           None, resolution="out_of_scope")

    assert store.unresolved_regression(SID, FILE) is False
    assert store.count_unapplied_approved_values(SID, FILE) == 0
    assert store.mark_file_compliant_if_reviewed(SID, FILE) is True
    assert store.get_file_record(SID, FILE)["compliant"] == 1


def test_a_still_pending_regression_keeps_the_file_uncertified(store, monkeypatch):
    _seed(store)
    _run(monkeypatch, store, _Blob(_deck("Picture 1")), _sequence({"1.1.1"}, {"1.4.3"}))
    assert store.mark_file_compliant_if_reviewed(SID, FILE) is False


# ── what is NOT a regression ──────────────────────────────────────────────────

def test_a_criterion_already_failing_before_the_write_does_not_block(store, monkeypatch):
    """2.4.4 failed in the baseline and still fails. That is residue the write did not cause,
    and blocking on it would strand every file with an unrelated open finding."""
    _seed(store)
    _run(monkeypatch, store, _Blob(_deck("Picture 1")),
         _sequence({"1.1.1", "2.4.4"}, {"2.4.4"}))

    assert _regressed_rows(store) == []
    assert store.unresolved_regression(SID, FILE) is False
    assert store.get_file_record(SID, FILE)["compliant"] == 1


def test_a_discarded_write_is_not_charged_as_a_regression(store, monkeypatch):
    """The still-failing path returns the PRE-write bytes, so a criterion that broke inside a
    write nobody kept never reached the document. It stays evidence about the draft — recorded
    on its outcome row — and must not block the file or raise review work."""
    _seed(store)
    blob = _Blob(_deck("Picture 1"))
    _run(monkeypatch, store, blob, _sequence({"1.1.1"}, {"1.1.1", "1.4.3"}))

    assert blob.uploads == [], "the lane discarded the write"
    assert _regressed_rows(store) == []
    assert store.unresolved_regression(SID, FILE) is False
    assert [d["action"] for d in store.list_decisions(scan_id=SID)].count("apply.regression") == 0
    [outcome] = store.list_ai_validation_outcomes(SID, FILE)
    assert outcome["outcome"] == "verified_still_failing"
    assert outcome["regressions"] == ["1.4.3"], "still recorded against the model"


def test_an_unverifiable_rescan_makes_no_regression_claim(store, monkeypatch):
    _seed(store)
    _run(monkeypatch, store, _Blob(_deck("Picture 1")), _sequence({"1.1.1"}, Exception))
    assert _regressed_rows(store) == []
    assert store.unresolved_regression(SID, FILE) is False


def test_without_a_baseline_nothing_is_called_a_regression(store, monkeypatch):
    """No trustworthy pre-write residual means the comparison is unknown, not clean. Unknown
    must not block — it is not evidence of damage — and it must not certify silently either:
    the file certifies here only because every OTHER gate is satisfied."""
    _seed(store)
    _run(monkeypatch, store, _Blob(_deck("Picture 1")), _sequence(Exception, {"1.4.3"}))
    assert _regressed_rows(store) == []
    assert store.unresolved_regression(SID, FILE) is False


# ── the fail-closed half ──────────────────────────────────────────────────────

def test_a_regression_nobody_could_raise_still_blocks(store, monkeypatch):
    """Queueing is best-effort so a failure there cannot lose the corrected copy. The gate must
    therefore fail CLOSED on the missing row: a file whose regression nobody can see is exactly
    the file that must not certify on that silence."""
    import store as store_mod
    _seed(store)
    monkeypatch.setattr(store_mod.Store, "queue_regression_review",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("queue is down")))
    _run(monkeypatch, store, _Blob(_deck("Picture 1")), _sequence({"1.1.1"}, {"1.4.3"}))

    assert _regressed_rows(store) == []                      # nothing was raised
    assert store.unresolved_regression(SID, FILE) is True     # and it still refuses
    # On the RECORD, not on the return value. mark_file_compliant_if_reviewed answers False for
    # an already-certified file too, so asserting the call alone passes just as happily when the
    # gate let the file through during the apply job — which is the failure this test is for.
    assert store.get_file_record(SID, FILE)["compliant"] == 0
    assert store.mark_file_compliant_if_reviewed(SID, FILE) is False
    assert store.get_file_record(SID, FILE)["compliant"] == 0


def test_the_queue_never_reopens_a_regression_a_human_accepted(store, monkeypatch):
    """A re-run of the apply job re-observes the same regression. The accepted row stays
    accepted; reopening it would ask the reviewer the same question forever."""
    _seed(store)
    _run(monkeypatch, store, _Blob(_deck("Picture 1")), _sequence({"1.1.1"}, {"1.4.3"}))
    [row] = _regressed_rows(store)
    store.update_hitl_item(row["id"], "approved", None, None, resolution="out_of_scope")

    assert store.queue_regression_review(SID, FILE, ["1.4.3"]) == []
    rows = _regressed_rows(store)
    assert len(rows) == 1 and rows[0]["status"] == "approved"
    assert store.unresolved_regression(SID, FILE) is False


def test_a_regression_row_never_owes_the_document_content(store, monkeypatch):
    """A reviewer who types into the card must not strand the file. The card resolves
    '1.1.1/regressed' to WCAG 1.1.1, which the frontend classes as a VALUE_FIX and therefore
    offers an editor for — and a legacy approved_value on a row with no proposals counts as
    unwritten content forever. The row's rule_id decides, not what the client sent."""
    # Constructed directly, because no single lane can produce it: the alt lane's own criterion
    # is what it verifies, so a write that leaves 1.1.1 failing is never credited in the first
    # place. A 1.1.1 regression comes from a DIFFERENT lane's write, and the state it leaves is
    # exactly these two rows.
    item_id = _seed(store)
    store.mark_row_applied(item_id)
    store.queue_regression_review(SID, FILE, ["1.1.1"])
    store.log_decision("system", "apply.regression", scan_id=SID, file=FILE, detail="link lane")
    rows = _regressed_rows(store)
    assert [r["rule_id"] for r in rows] == ["1.1.1/regressed"]
    assert store.unresolved_regression(SID, FILE) is True

    store.update_hitl_item(rows[0]["id"], "approved", None, "a description the reviewer typed")

    assert store.count_unapplied_approved_values(SID, FILE) == 0
    assert store.unresolved_regression(SID, FILE) is False
    assert store.mark_file_compliant_if_reviewed(SID, FILE) is True


def test_queueing_is_idempotent_and_orders_multiple_criteria(store):
    store.init_scan_run(SID, "drive", 1, "2026-07-10T00:00:00Z", "rubric", "hash")
    first = store.queue_regression_review(SID, FILE, ["2.4.4", "1.4.3", "1.4.3", " "])
    assert len(first) == 2
    assert [r["rule_id"] for r in _regressed_rows(store)] == ["2.4.4/regressed", "1.4.3/regressed"]
    assert store.queue_regression_review(SID, FILE, ["1.4.3", "2.4.4"]) == []

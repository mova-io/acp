"""An approved AI draft is linked to what happened AFTER the human said yes.

#1667 recorded the first three post-write outcomes (cleared, still failing, could not verify)
against the exact model call a reviewer accepted. Three things were still missing before a
model comparison could be trusted, and each is pinned here:

  * a REGRESSION — the write cleared its criterion and made another one fail. Decidable only
    against a baseline re-scan of the bytes before the write, which the apply job now takes once.
  * an UNRESOLVED write — the reviewer approved a value for content the document no longer has.
    Nothing was written, so it must not inherit its neighbours' verified_cleared.
  * a CONSUMER — the per-model rollup and the document timeline. Rows nobody reads are not
    evidence; the Settings "model evidence" table said so in as many words.

Every count here is a join on a recorded model_call_id. A decision or validation that did not
carry one is absent from the numbers rather than attributed by proximity.
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
    monkeypatch.setattr(store_mod, "_SQLITE_PATH", Path(tempfile.mkdtemp()) / "linkage.db")
    return store_mod.Store()


# ── store: the rows ───────────────────────────────────────────────────────────

def _accepted_call(st, *, item_id="item-1", file=FILE, model="llava:13b", rule="1.1.1",
                   action="approve"):
    call_id = st.record_ai_call(surface="vision", provider="ollama", model=model,
                                zone="local", latency_ms=120, ok=True, scan_id=SID, file=file)
    st.record_hitl_event(SID, file, rule, item_id, action, model_call_id=call_id,
                         edited=(action == "edit"))
    return call_id


def test_regressions_are_stored_as_a_list_and_unknown_stays_unknown(store):
    _accepted_call(store)
    assert store.record_ai_validation_outcomes(
        SID, FILE, "1.1.1", ["item-1"], "verified_regressed",
        detail="cleared 1.1.1", regressions={"2.4.4", "1.4.3"}) == 1
    assert store.record_ai_validation_outcomes(
        SID, FILE, "1.1.1", ["item-1"], "could_not_verify", regressions=None) == 1
    assert store.record_ai_validation_outcomes(
        SID, FILE, "1.1.1", ["item-1"], "verified_cleared", regressions=[]) == 1
    rows = store.list_ai_validation_outcomes(SID, FILE)
    by = {r["outcome"]: r for r in rows}
    assert by["verified_regressed"]["regressions"] == ["1.4.3", "2.4.4"]   # sorted, decoded
    assert by["could_not_verify"]["regressions"] is None                    # unknown ≠ none
    assert by["verified_cleared"]["regressions"] == []


def test_write_unresolved_is_an_outcome_and_unknown_outcomes_still_refuse(store):
    _accepted_call(store)
    assert store.record_ai_validation_outcomes(
        SID, FILE, "1.1.1", ["item-1"], "write_unresolved", detail="gone") == 1
    with pytest.raises(ValueError, match="unsupported AI validation outcome"):
        store.record_ai_validation_outcomes(SID, FILE, "1.1.1", ["item-1"], "unresolved")


# ── store: the rollup consumer ────────────────────────────────────────────────

def test_rollup_joins_reviewer_and_validation_outcomes_to_the_exact_model_call(store):
    """Two models, one window. The strong model's drafts were accepted and cleared; the weak
    model's were edited, rejected, and one write regressed. A decision with no call id — the
    human-authored row — appears in neither model's count."""
    strong_a = _accepted_call(store, item_id="s-a", model="strong")
    strong_b = _accepted_call(store, item_id="s-b", model="strong")
    weak_a = _accepted_call(store, item_id="w-a", model="weak", action="edit")
    _accepted_call(store, item_id="w-b", model="weak", action="reject")
    store.record_hitl_event(SID, FILE, "1.1.1", "human", "approve")        # no model_call_id

    store.record_ai_validation_outcomes(SID, FILE, "1.1.1", ["s-a"], "verified_cleared",
                                        regressions=[])
    # A retry supersedes: could_not_verify, then cleared — counts ONCE, as cleared.
    store.record_ai_validation_outcomes(SID, FILE, "1.1.1", ["s-b"], "could_not_verify")
    store.record_ai_validation_outcomes(SID, FILE, "1.1.1", ["s-b"], "verified_cleared",
                                        regressions=[])
    store.record_ai_validation_outcomes(SID, FILE, "1.1.1", ["w-a"], "verified_regressed",
                                        regressions=["2.4.4"])

    roll = store.ai_cost_rollup(since_days=None)
    by = {m["model"]: m for m in roll["by_model"]}
    assert by["strong"]["reviewed"] == {"decisions": 2, "approved": 2, "edited": 0, "rejected": 0}
    assert by["strong"]["validation"] == {
        "validated": 2, "cleared": 2, "regressed": 0, "still_failing": 0,
        "could_not_verify": 0, "unresolved": 0, "newly_failing": 0}
    assert by["weak"]["reviewed"] == {"decisions": 2, "approved": 0, "edited": 1, "rejected": 1}
    assert by["weak"]["validation"]["regressed"] == 1
    assert by["weak"]["validation"]["newly_failing"] == 1
    assert by["weak"]["validation"]["validated"] == 1
    # The totals are the sum of the linked rows and nothing else: the unlinked human approval
    # is not in them.
    assert roll["reviewed"]["decisions"] == 4
    assert roll["validation"]["validated"] == 3
    assert {strong_a, strong_b, weak_a} <= {r["model_call_id"]
                                            for r in store.list_ai_validation_outcomes(SID)}


def test_rollup_window_is_the_calls_window(store):
    """A decision on a call outside the window is outside the rollup, even though the decision
    itself is recent — the grain is the model call, so the window is the call's."""
    call_id = store.record_ai_call(surface="vision", provider="ollama", model="old",
                                   zone="local", latency_ms=1, ok=True, scan_id=SID, file=FILE)
    with store._db.cursor() as cur:
        store._db.execute(cur, "UPDATE ai_calls SET ts=%s WHERE id=%s",
                          ("2020-01-01T00:00:00+00:00", call_id))
    store.record_hitl_event(SID, FILE, "1.1.1", "i-old", "approve", model_call_id=call_id)
    roll = store.ai_cost_rollup(since_days=30)
    assert roll["by_model"] == []
    assert roll["reviewed"]["decisions"] == 0
    assert store.ai_cost_rollup(since_days=None)["reviewed"]["decisions"] == 1


def test_a_model_with_calls_but_no_linked_outcomes_reports_zeros_not_absence(store):
    store.record_ai_call(surface="vision", provider="ollama", model="quiet", zone="local",
                         latency_ms=1, ok=True, scan_id=SID, file=FILE)
    m = store.ai_cost_rollup(since_days=None)["by_model"][0]
    assert m["reviewed"]["decisions"] == 0 and m["validation"]["validated"] == 0


# ── store: the timeline consumer ──────────────────────────────────────────────

def test_timeline_shows_each_post_write_outcome_with_its_regressions(store):
    _accepted_call(store)
    store.init_scan_run(SID, "drive", 1, "2026-07-10T00:00:00Z", "rubric", "hash")
    store.record_ai_validation_outcomes(SID, FILE, "1.1.1", ["item-1"], "verified_regressed",
                                        detail="cleared 1.1.1", regressions=["2.4.4"])
    store.record_ai_validation_outcomes(SID, FILE, "1.1.1", ["item-1"], "write_unresolved",
                                        detail="locator gone")
    events = [e for e in store.document_timeline(SID, FILE) if e["title"].startswith("AI draft")]
    assert [e["kind"] for e in events] == ["fix", "decision"]      # changed the bytes / did not
    assert events[0]["title"] == "AI draft written and verified, with a regression · 1.1.1"
    assert "newly failing: 2.4.4" in events[0]["detail"]
    assert events[1]["title"] == "AI draft not written — content no longer found · 1.1.1"
    assert events[1]["rule_id"] == "1.1.1"


# ── the applier: where the rows come from ─────────────────────────────────────

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


def _seed_linked(store, *, names=("Picture 1",)):
    """A remediated deck with ONE approved 1.1.1 row carrying one proposal per image, and the
    decision carrying the exact vision call that drafted it. One row, not one per image: the
    queue collapses to a single row per (scan, file, rule) — see test_hitl_row_collapse — which
    is the "multi-instance card" the backlog notes stays attributed at row grain."""
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
    return item_id, _accepted_call(store, item_id=item_id)


def _run(monkeypatch, store, blob, verify, *, file=FILE):
    """Drive the handler with Blob stubbed and `_verify_residual` replaced by `verify`, a
    callable that returns one Verification per call — the FIRST call is the baseline re-scan of
    the untouched copy, each later one is a lane's post-write re-scan."""
    import core, handlers
    monkeypatch.setattr(core, "store", store)
    monkeypatch.setitem(sys.modules, "blob", blob)
    monkeypatch.setattr(handlers, "_verify_residual", verify)
    handlers._apply_approved_values({"scan_id": SID, "file": file}, {})


def _sequence(*residuals):
    """Verification(True, r) for each r in turn; an `Exception` entry means could-not-verify."""
    from proposals import Verification
    it = iter(residuals)

    def verify(_bytes, _file, *, scan_id=None):
        r = next(it)
        return Verification(False, reason="rescan raised X") if r is Exception else Verification(True, r)
    return verify


def test_a_cleared_write_is_linked_to_its_call_with_no_regressions(store, monkeypatch):
    item_id, call_id = _seed_linked(store)
    _run(monkeypatch, store, _Blob(_deck("Picture 1")), _sequence({"1.1.1"}, set()))
    [row] = store.list_ai_validation_outcomes(SID, FILE)
    assert row["outcome"] == "verified_cleared"
    assert row["model_call_id"] == call_id and row["item_id"] == item_id
    assert row["regressions"] == []                    # baseline ran: known to be none


def test_post_write_outcome_keeps_every_call_on_a_multi_instance_card(store):
    """One card can approve two independently generated values; validation belongs to both."""
    item_id, first = _seed_linked(store, names=("Picture 1", "Picture 2"))
    second = _accepted_call(store, item_id=item_id)
    inserted = store.record_ai_validation_outcomes(
        SID, FILE, "1.1.1", [item_id], "verified_cleared", regressions=[])
    rows = store.list_ai_validation_outcomes(SID, FILE)
    assert inserted == 2
    assert {row["model_call_id"] for row in rows} == {first, second}


def test_a_write_that_breaks_another_criterion_is_recorded_as_a_regression(store, monkeypatch):
    """Before the write 1.1.1 failed; after it 1.1.1 is clear and 2.4.4 fails. The credit gate
    is unchanged — the file is still credited and uploaded — but the draft's row says what the
    write cost, and the decision log carries the same fact for the auditor."""
    _seed_linked(store)
    blob = _Blob(_deck("Picture 1"))
    _run(monkeypatch, store, blob, _sequence({"1.1.1"}, {"2.4.4"}))
    [row] = store.list_ai_validation_outcomes(SID, FILE)
    assert row["outcome"] == "verified_regressed"
    assert row["regressions"] == ["2.4.4"]
    assert blob.uploads, "the gate did not change: a cleared write is still stored"
    actions = [d["action"] for d in store.list_decisions(scan_id=SID)]
    assert "apply.regression" in actions
    assert store.ai_cost_rollup(since_days=None)["validation"]["regressed"] == 1


def test_a_criterion_that_failed_before_the_write_is_not_a_regression(store, monkeypatch):
    """2.4.4 was failing in the baseline and still fails after — that is residue, not damage."""
    _seed_linked(store)
    _run(monkeypatch, store, _Blob(_deck("Picture 1")), _sequence({"1.1.1", "2.4.4"}, {"2.4.4"}))
    [row] = store.list_ai_validation_outcomes(SID, FILE)
    assert row["outcome"] == "verified_cleared"
    assert row["regressions"] == []


def test_a_still_failing_write_keeps_its_regressions_visible(store, monkeypatch):
    _seed_linked(store)
    _run(monkeypatch, store, _Blob(_deck("Picture 1")), _sequence({"1.1.1"}, {"1.1.1", "1.4.3"}))
    [row] = store.list_ai_validation_outcomes(SID, FILE)
    assert row["outcome"] == "verified_still_failing"
    assert row["regressions"] == ["1.4.3"]
    assert store.ai_cost_rollup(since_days=None)["validation"]["newly_failing"] == 1


def test_without_a_trustworthy_baseline_regressions_are_unknown_not_none(store, monkeypatch):
    _seed_linked(store)
    _run(monkeypatch, store, _Blob(_deck("Picture 1")), _sequence(Exception, set()))
    [row] = store.list_ai_validation_outcomes(SID, FILE)
    assert row["outcome"] == "verified_cleared"
    assert row["regressions"] is None


def test_could_not_verify_records_no_regression_claim(store, monkeypatch):
    _seed_linked(store)
    _run(monkeypatch, store, _Blob(_deck("Picture 1")), _sequence({"1.1.1"}, Exception))
    [row] = store.list_ai_validation_outcomes(SID, FILE)
    assert row["outcome"] == "could_not_verify"
    assert row["regressions"] is None


def test_an_unresolved_locator_is_its_own_outcome_not_a_cleared_write(store, monkeypatch):
    """The reviewer approved a description for an image the deck no longer has. Nothing is
    written, so the lane never re-scans — and the draft's row must say so, against its call,
    rather than being silently absent (which reads as "never validated")."""
    item_id, call_id = _seed_linked(store, names=("Ghost 9",))
    blob = _Blob(_deck("Picture 1"))                                 # Ghost 9 is gone
    _run(monkeypatch, store, blob, _sequence({"1.1.1"}))             # baseline only: no write
    [row] = store.list_ai_validation_outcomes(SID, FILE)
    assert row["outcome"] == "write_unresolved"
    assert row["item_id"] == item_id and row["model_call_id"] == call_id
    assert "Ghost 9" in row["detail"]
    assert row["regressions"] is None                                # nothing was re-scanned
    assert blob.uploads == []
    assert store.ai_cost_rollup(since_days=None)["validation"]["unresolved"] == 1


def test_a_partly_unresolved_row_says_what_was_left_out(store, monkeypatch):
    """One row, two images, one of them gone. The present image is written and the criterion
    clears, so the ROW is credited (existing behaviour) — but its validation detail carries the
    unresolved count, so the cleared outcome cannot be read as a complete write."""
    item_id, call_id = _seed_linked(store, names=("Picture 1", "Ghost 9"))
    _run(monkeypatch, store, _Blob(_deck("Picture 1")), _sequence({"1.1.1"}, set()))
    [row] = store.list_ai_validation_outcomes(SID, FILE)
    assert row["outcome"] == "verified_cleared"
    assert "1 locator(s) unresolved" in row["detail"]
    assert row["model_call_id"] == call_id


def test_a_human_authored_approval_yields_no_validation_row(store, monkeypatch):
    store.init_scan_run(SID, "drive", 1, "2026-07-10T00:00:00Z", "rubric", "hash")
    store.save_file_result(SID, {
        "file": FILE, "engine": "office", "status": "pass", "score": 40, "compliant": 0,
        "skipped_rules": 0, "drive_file_id": "drv1",
        "issues": [{"ruleId": "PPTX-ALT-001", "wcag": "1.1.1", "severity": "CRITICAL"}],
    }, "2026-07-10T00:00:00Z")
    store.record_remediation(SID, FILE, drive_write_url="http://d/1", blob_url="http://b/1")
    item_id = store.enqueue_proposals(SID, FILE, "1.1.1", [
        {"locator": f"{SLIDE}#Picture 1", "before": "(no alt text)",
         "proposed_value": "A human wrote this", "rationale": "r", "source": "human"},
    ], rule_name="Non-text Content")
    approve_bound(store, item_id, [])
    store.record_hitl_event(SID, FILE, "1.1.1", item_id, "approve")     # no model_call_id
    _run(monkeypatch, store, _Blob(_deck("Picture 1")), _sequence({"1.1.1"}, set()))
    assert store.list_ai_validation_outcomes(SID, FILE) == []
    assert store.count_unapplied_approved_values(SID, FILE) == 0        # still applied


# ── two lanes, two rows: the baseline moves and the items are told apart ─────────
#
# A Word file, because it has two applier lanes that both take a plain fixture: the alt lane
# (1.1.1, resolved against wp:docPr by name) and the link lane (2.4.4/2.4.9, resolved by href).

DOC = "report.docx"
DOC_XML = "word/document.xml"
_DOC_RELS = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
             '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
             '<Relationship Id="rId1" '
             'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink" '
             'Target="https://example.com/pricing" TargetMode="External"/>'
             '</Relationships>')


def _docx() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(DOC_XML,
                   '<w:document><w:body>'
                   '<w:p><w:r><w:drawing><wp:inline><wp:docPr id="1" name="Picture 1"/>'
                   '</wp:inline></w:drawing></w:r></w:p>'
                   '<w:hyperlink r:id="rId1"><w:r><w:t>click here</w:t></w:r></w:hyperlink>'
                   '</w:body></w:document>')
        z.writestr("word/_rels/document.xml.rels", _DOC_RELS)
    return buf.getvalue()


def _seed_docx_rows(store, rows):
    """A remediated Word file with one approved row per (rule, proposals) given, each decision
    carrying the exact call that drafted it. Returns {rule_id: (item_id, call_id)}."""
    store.init_scan_run(SID, "drive", 1, "2026-07-10T00:00:00Z", "rubric", "hash")
    store.save_file_result(SID, {
        "file": DOC, "engine": "office", "status": "pass", "score": 40, "compliant": 0,
        "skipped_rules": 0, "drive_file_id": "drv2",
        "issues": [{"ruleId": "DOCX-ALT-001", "wcag": "1.1.1", "severity": "CRITICAL"},
                   {"ruleId": "DOCX-LINK-001", "wcag": "2.4.4", "severity": "SERIOUS"}],
    }, "2026-07-10T00:00:00Z")
    store.record_remediation(SID, DOC, drive_write_url="http://d/2", blob_url="http://b/2")
    out = {}
    for rule_id, proposals in rows:
        item_id = store.enqueue_proposals(SID, DOC, rule_id, proposals, rule_name=rule_id)
        approve_bound(store, item_id, [])
        out[rule_id] = (item_id, _accepted_call(store, item_id=item_id, file=DOC, rule=rule_id))
    return out


_ALT_ROW = ("1.1.1", [{"locator": f"{DOC_XML}#Picture 1", "before": "(no alt text)",
                       "proposed_value": "A clinician at a desk.", "rationale": "r",
                       "source": "llava"}])
_LINK_ROW = ("2.4.4", [{"locator": "https://example.com/pricing", "before": "click here",
                        "proposed_value": "Pricing details", "rationale": "r",
                        "source": "derived"}])
_GHOST_LINK_ROW = ("2.4.9", [{"locator": "https://example.com/gone", "before": "here",
                              "proposed_value": "A page this file no longer links",
                              "rationale": "r", "source": "derived"}])


def test_a_regression_is_charged_to_the_lane_that_caused_it_not_to_every_lane_after(
        store, monkeypatch):
    """Alt lane, then link lane, on the same bytes. The alt write clears 1.1.1 and makes 1.4.3
    fail; the link write clears 2.4.4 and changes nothing else. 1.4.3 is the alt draft's
    regression ONLY — the baseline advances with each credited lane, so the link draft is
    measured against bytes in which 1.4.3 was already failing."""
    rows = _seed_docx_rows(store, [_ALT_ROW, _LINK_ROW])
    blob = _Blob(_docx())
    _run(monkeypatch, store, blob, _sequence({"1.1.1", "2.4.4"},      # baseline
                                             {"2.4.4", "1.4.3"},      # after the alt write
                                             {"1.4.3"}),              # after the link write
         file=DOC)
    by_rule = {r["rule_id"]: r for r in store.list_ai_validation_outcomes(SID, DOC)}
    assert set(by_rule) == {"1.1.1", "2.4.4"}
    assert by_rule["1.1.1"]["outcome"] == "verified_regressed"
    assert by_rule["1.1.1"]["regressions"] == ["1.4.3"]
    assert by_rule["1.1.1"]["model_call_id"] == rows["1.1.1"][1]
    assert by_rule["2.4.4"]["outcome"] == "verified_cleared"
    assert by_rule["2.4.4"]["regressions"] == []
    assert by_rule["2.4.4"]["model_call_id"] == rows["2.4.4"][1]
    assert len(blob.uploads) == 1


def test_an_unresolved_row_does_not_inherit_its_lane_neighbours_cleared_outcome(
        store, monkeypatch):
    """One link lane, two rows (2.4.4 and 2.4.9 share it). The 2.4.4 row's href is in the file
    and is rewritten; the 2.4.9 row's href is not, so nothing of it is written. The lane clears
    on re-scan — and that outcome belongs to the row that was written, not to the one that was
    silently left out."""
    rows = _seed_docx_rows(store, [_LINK_ROW, _GHOST_LINK_ROW])
    _run(monkeypatch, store, _Blob(_docx()), _sequence({"2.4.4", "2.4.9"}, set()), file=DOC)
    outcomes = store.list_ai_validation_outcomes(SID, DOC)
    by_item = {r["item_id"]: r["outcome"] for r in outcomes}
    assert len(outcomes) == 2, [(r["item_id"], r["outcome"]) for r in outcomes]
    assert by_item[rows["2.4.4"][0]] == "verified_cleared"
    assert by_item[rows["2.4.9"][0]] == "write_unresolved"

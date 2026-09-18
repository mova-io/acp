"""The ordinary approved-value writer admits only approvals whose recorded binding still holds.

THE DEFECT (audit gap 9, reproduced at the handler boundary). An approval records the assessment
revision, value digest and proposal snapshots it was given for, and the single-item RETRY path has
refused a moved binding since #2129. The ORDINARY path did not look: `_apply_approved_values` read
every approved-unapplied row through `_approved_unapplied_rows`, so an approval the queue already
marked `approval_recheck_required` was written into the document the next time ANY approval on the
same file enqueued a job. Reproduced here with the real handler and the real `_apply_one_value_kind`,
stopped only at each lane's physical document writer.

THE CONSTRAINT that shapes the fix. `_approved_unapplied_rows` is also what every compliance counter
reads, and a stale approval is still approved work the document does not carry. Filtering it there
would let a file certify on an approval nobody can write. So the admission is WRITER-specific: the
counters below are asserted unchanged, and the held row stays approved, unapplied and flagged for
recheck, with one safe audit line saying why the writer passed it by.

Synthetic fixtures only: local stubs for storage and re-scan, no AI, no network.
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "api"))

SCAN = "s-writer-admission"
ACTOR = "reviewer@example.com"
STRUCT_LOCATOR = "pdf:struct:1:" + "a" * 64

# (lane, file, rule_id, proposal, resolution, writer module, writer attribute)
LANES = [
    ("alt", "deck.pptx", "1.1.1",
     {"locator": "ppt/slides/slide1.xml#Picture 1", "proposed_value": "Synthetic chart description"},
     None, "apply_alt", "apply_alt_text"),
    ("decorative", "banner.pptx", "1.1.1",
     {"locator": "ppt/slides/slide1.xml#Picture 2", "proposed_value": "Mark as decorative"},
     "decorative", "apply_alt", "apply_alt_text"),
    ("link", "links.docx", "2.4.4",
     {"locator": "https://example.invalid/report", "proposed_value": "Synthetic report"},
     None, "apply_link_text", "apply_link_text"),
    ("field", "form.docx", "4.1.2",
     {"locator": "docx:sdt:1", "proposed_value": "Synthetic field name"},
     None, "apply_field_name", "apply_docx_field_name"),
    ("sensory", "guide.docx", "1.3.3",
     {"locator": "Press the green button", "proposed_value": "Press the Start button"},
     None, "apply_text_values", "apply_sensory_rewrite"),
    ("language", "notes.docx", "3.1.2",
     {"locator": "Bonjour tout le monde", "proposed_value": "fr"},
     None, "apply_text_values", "apply_language_parts"),
    ("structure_label", "book.xlsx", "2.4.6",
     {"locator": "sheet:Sheet1", "proposed_value": "Synthetic totals"},
     None, "apply_xlsx_labels", "apply_xlsx_labels"),
    ("images_of_text", "slides.pptx", "1.4.5",
     {"locator": "image 1", "proposed_value": "Synthetic heading text"},
     None, "apply_pptx_image_replacement", "apply_pptx_image_replacement"),
    ("pdf_structure", "tagged.pdf", "1.3.1",
     {"locator": STRUCT_LOCATOR,
      "proposed_value": json.dumps({"op": "header-scope", "scope": "Column"})},
     None, "pdf_structure_repairs", "apply_pdf_structure_repairs"),
]


class ReachedLane(Exception):
    """The lane's physical document writer was called."""


@pytest.fixture()
def env(monkeypatch):
    import core
    import handlers
    import store as store_mod
    from proposals import Verification

    monkeypatch.setattr(store_mod, "_SQLITE_PATH", Path(tempfile.mkdtemp()) / "writer.db")
    st = store_mod.Store()
    st.init_scan_run(SCAN, "drive", 1, "t0", "rubric", "hash")
    monkeypatch.setattr(core, "store", st)
    monkeypatch.setattr(core, "fire_webhook", lambda *a, **k: None)
    downloads = []
    monkeypatch.setitem(sys.modules, "blob", SimpleNamespace(
        download_remediated=lambda *a: downloads.append(a) or b"synthetic-local-only"))
    monkeypatch.setattr(handlers, "_verify_residual", lambda *a, **kw: Verification(True, ()))
    return SimpleNamespace(st=st, handlers=handlers, downloads=downloads, monkeypatch=monkeypatch)


def _approve(st, item_id, resolution=None):
    """A recorded, bound approval (the store API the route and standing paths share)."""
    row, replayed = st.complete_hitl_decision(
        item_id, "approved", None, None, resolution=resolution, approved_values=None,
        actor=ACTOR, detail=None)
    assert not replayed and row["status"] == "approved"
    return row


def _row(env, lane):
    _, file, rule_id, proposal, resolution, module, attr = lane
    item_id = env.st.enqueue_proposals(SCAN, file, rule_id, [dict(proposal, before="", rationale="draft")])
    _approve(env.st, item_id, resolution)
    import importlib
    reached = []

    def writer(*args, **kwargs):
        reached.append((args, kwargs))
        raise ReachedLane(lane[0])
    env.monkeypatch.setattr(importlib.import_module(module), attr, writer)
    return item_id, file, reached


def _move_source(st):
    """A re-assessment: the scan's assessment input revision changes."""
    with st._db.cursor() as cur:
        st._db.execute(cur, "UPDATE scan_runs SET rubric_hash=%s WHERE id=%s",
                       ("synthetic-reassessment", SCAN))


def _held_lines(st):
    return [json.loads(d["detail"]) for d in st.list_decisions(SCAN)
            if d["action"] == "apply.stale_approval_held"]


@pytest.mark.parametrize("lane", LANES, ids=[lane[0] for lane in LANES])
def test_a_current_approval_reaches_its_lane(env, lane):
    """Positive control: the fixture really does reach each lane's writer when the binding holds,
    so the refusal test below cannot pass merely because the lane was never reachable."""
    item_id, file, reached = _row(env, lane)
    with pytest.raises(ReachedLane):
        env.handlers._apply_approved_values({"scan_id": SCAN, "file": file}, {})
    assert reached
    assert _held_lines(env.st) == []


@pytest.mark.parametrize("lane", LANES, ids=[lane[0] for lane in LANES])
def test_a_stale_approval_never_reaches_its_lane(env, lane):
    item_id, file, reached = _row(env, lane)
    _move_source(env.st)
    assert env.st.approved_write_binding(
        env.st.get_hitl_item(item_id), check_target_removal=False)[1] == env.st.RETRY_SOURCE_MOVED

    env.handlers._apply_approved_values({"scan_id": SCAN, "file": file}, {})

    assert reached == [], "a stale approval was handed to the document writer"
    assert env.downloads == [], "nothing admitted, so the corrected copy is not even fetched"
    row = env.st.get_hitl_item(item_id)
    assert row["status"] == "approved" and not row["applied"]
    listed = next(r for r in env.st.list_hitl_queue(scan_id=SCAN) if r["id"] == item_id)
    assert listed["approval_recheck_required"] is True
    assert _held_lines(env.st) == [{"item_id": item_id, "refusal": "source_moved",
                                    "approval_recheck_required": True}]


def test_parent_handler_repro_inverted(env):
    """The parent's exact shape: one approved caption, the assessment moves, the ordinary job runs.
    Before the fix `apply_alt.apply_alt_text` received the old approved caption."""
    item_id, file, reached = _row(env, LANES[0])
    _move_source(env.st)
    env.handlers._apply_approved_values({"scan_id": SCAN, "file": file}, {})
    assert reached == []


def test_only_the_stale_row_is_held_on_a_mixed_file(env):
    """Two approvals on one file, one re-bound to the current revision: the writer receives exactly
    the current one, and the other stays held — not the whole file."""
    st = env.st
    stale = st.enqueue_proposals(SCAN, "deck.pptx", "1.1.1", [
        {"locator": "ppt/slides/slide1.xml#Picture 1", "proposed_value": "Stale description",
         "before": "", "rationale": "draft"}])
    _approve(st, stale)
    _move_source(st)
    fresh = st.enqueue_proposals(SCAN, "deck.pptx", "2.4.4", [
        {"locator": "https://example.invalid/a", "proposed_value": "Current link text",
         "before": "", "rationale": "draft"}])
    _approve(st, fresh)
    import apply_alt
    import apply_link_text
    alt_calls, link_calls = [], []
    env.monkeypatch.setattr(apply_alt, "apply_alt_text",
                            lambda data, values, **kw: alt_calls.append(values) or (data, [], []))

    def link(data, ext, values):
        link_calls.append(values)
        raise ReachedLane("link")
    env.monkeypatch.setattr(apply_link_text, "apply_link_text", link)
    with pytest.raises(ReachedLane):
        env.handlers._apply_approved_values({"scan_id": SCAN, "file": "deck.pptx"}, {})
    assert alt_calls == []
    assert link_calls == [{"https://example.invalid/a": "Current link text"}]
    assert [line["item_id"] for line in _held_lines(st)] == [stale]


def test_values_changed_after_approval_are_held(env):
    """A later run replacing an approved row's proposals in place used to hand the NEW, never
    approved drafts to the writer through the draft fallback."""
    st = env.st
    item_id, file, reached = _row(env, LANES[0])
    st.enqueue_proposals(SCAN, file, "1.1.1", [
        {"locator": "ppt/slides/slide1.xml#Picture 1", "proposed_value": "Replacement never approved",
         "before": "", "rationale": "draft"}])
    env.handlers._apply_approved_values({"scan_id": SCAN, "file": file}, {})
    assert reached == []
    assert _held_lines(st) == [{"item_id": item_id, "refusal": "values_changed",
                                "approval_recheck_required": True}]


def test_a_hold_the_retry_gate_calls_unbound_is_still_flagged_for_recheck(env):
    """The flag must name EXACTLY what the writer holds, or a hold is an invisible wedge.

    An approval recorded before its proposals had snapshots carries [null] lineage; the retry
    gate reads that as "no binding" (RETRY_NO_BINDING), which the old flag ignored. A later run
    then replaces the proposals with snapshotted ones — same text, so the value digest still
    matches — and the writer holds the row as superseded. Without the flag following the writer,
    the row would sit approved, unwritten and uncounted as "needs recheck" forever."""
    from ai_run_policy import run_context
    st = env.st
    item_id, file, reached = _row(env, LANES[0])
    assert st.get_hitl_item(item_id)["approved_proposal_snapshot_ids"] == [None]
    with st._db.cursor() as cur:
        st._db.execute(cur, "UPDATE scan_runs SET owner_email=%s WHERE id=%s", (ACTOR, SCAN))
    batch = st.enqueue_stage_batch(SCAN, "remediate", "remediate_file", [{
        "owner": ACTOR, "scan_id": SCAN, "file": file,
        "remediation_impact_policy": {"ai": 1, "rule_based": 2, "ai_budget_usd": "0.10",
                                      "snapshot_id": "fixture"}}],
        snapshot_id=st.stage_snapshot_id(SCAN), request_fingerprint="fixture")
    job = st.get_job(batch["job_ids"][0])
    with run_context(st, job["payload"], job):
        st.enqueue_proposals(SCAN, file, "1.1.1", [dict(LANES[0][3], before="", rationale="draft")])
    row = st.get_hitl_item(item_id)
    assert row["proposal_snapshot_ids"] and all(row["proposal_snapshot_ids"])
    assert st.approved_write_binding(row, check_target_removal=False)[1] == st.RETRY_NO_BINDING
    assert st.approved_write_hold(row) == st.RETRY_PROPOSALS_SUPERSEDED
    listed = next(r for r in st.list_hitl_queue(scan_id=SCAN) if r["id"] == item_id)
    assert listed["approval_recheck_required"] is True
    env.handlers._apply_approved_values({"scan_id": SCAN, "file": file}, {})
    assert reached == []
    assert _held_lines(st) == [{"item_id": item_id, "refusal": "proposals_superseded",
                                "approval_recheck_required": True}]


def test_compliance_counters_still_count_the_held_row(env):
    """The writer's admission must not leak into the certification gate: a stale approval is
    approved content the document does not carry, before the hold and after it."""
    st = env.st
    item_id, file, _ = _row(env, LANES[0])
    with st._db.cursor() as cur:
        st._db.execute(cur, "INSERT INTO file_records(scan_id,file,status,compliant,remediated_at) "
                            "VALUES(%s,%s,'fail',0,%s)", (SCAN, file, "t1"))
    before = (st.count_unapplied_approved_values(SCAN, file),
              st.count_unapplied_approved_values_by_file(SCAN).get(file),
              len(st._approved_unapplied_rows(SCAN, file)))
    _move_source(st)
    env.handlers._apply_approved_values({"scan_id": SCAN, "file": file}, {})
    after = (st.count_unapplied_approved_values(SCAN, file),
             st.count_unapplied_approved_values_by_file(SCAN).get(file),
             len(st._approved_unapplied_rows(SCAN, file)))
    assert before == after == (1, 1, 1)
    assert st.mark_file_compliant_if_reviewed(SCAN, file) is False
    assert not (st.get_file_record(SCAN, file) or {}).get("compliant")
    assert st.has_approved_values_to_write(SCAN, file) is True


def test_a_binding_that_moves_during_the_write_is_rechecked_under_the_locks(env):
    """Admission is read before the (slow) write and re-read under the row-then-file locks before
    anything is credited. A re-assessment landing in between leaves the row approved, unapplied
    and uncredited, and the job retryable."""
    st = env.st
    item_id, file, _ = _row(env, LANES[0])
    import apply_alt
    import review_target_reconciliation as rtr

    def write(data, values, **kw):
        return data + b"-written", [{"locator": k, "before": "", "after": v}
                                    for k, v in values.items()], []

    def upload_then_move(*args):
        # The concurrent re-assessment commits after the pre-upload check and before the commit
        # transaction: only the re-check under the row-then-file locks can see it.
        _move_source(st)
        return "https://blob.invalid/copy"
    env.monkeypatch.setattr(apply_alt, "apply_alt_text", write)
    env.monkeypatch.setattr("office_alt_integrity.verify_alt_write", lambda *a, **k: True)
    env.monkeypatch.setattr("office_verified_retry.contradicted_captions", lambda *a, **k: [])
    env.monkeypatch.setattr("output_provenance.stamp_output", lambda data, name: data)
    locked = []
    real_lock_rows = rtr.lock_rows
    env.monkeypatch.setattr(rtr, "lock_rows",
                            lambda store, ids: locked.append(sorted(ids)) or real_lock_rows(store, ids))
    env.monkeypatch.setitem(sys.modules, "blob", SimpleNamespace(
        download_remediated=lambda *a: b"synthetic-local-only",
        upload_immutable_retry=upload_then_move))
    with pytest.raises(RuntimeError, match="approval binding changed"):
        env.handlers._apply_approved_values({"scan_id": SCAN, "file": file}, {"id": "j1"})
    assert locked == [[item_id]], "the refusal came from the re-check under the review-row lock"
    row = st.get_hitl_item(item_id)
    assert row["status"] == "approved" and not row["applied"]
    assert st.get_remediation_diffs(SCAN, file) == []
    assert not (st.get_file_record(SCAN, file) or {}).get("corrected_sha256")


def test_a_row_re_decided_during_the_write_is_not_credited(env):
    """The reviewer rejects the approval while the job is writing it. The commit re-reads the row
    under its lock, finds it no longer approved, and credits nothing (retryable)."""
    st = env.st
    item_id, file, _ = _row(env, LANES[0])
    import apply_alt
    env.monkeypatch.setattr(apply_alt, "apply_alt_text", lambda data, values, **kw: (
        data + b"-written", [{"locator": k, "before": "", "after": v} for k, v in values.items()], []))
    env.monkeypatch.setattr("office_alt_integrity.verify_alt_write", lambda *a, **k: True)
    env.monkeypatch.setattr("office_verified_retry.contradicted_captions", lambda *a, **k: [])
    env.monkeypatch.setattr("output_provenance.stamp_output", lambda data, name: data)

    def upload_then_reject(*args):
        st.complete_hitl_decision(item_id, "rejected", None, None, resolution=None,
                                  approved_values=None, actor=ACTOR, detail=None)
        return "https://blob.invalid/copy"
    env.monkeypatch.setitem(sys.modules, "blob", SimpleNamespace(
        download_remediated=lambda *a: b"synthetic-local-only", upload_immutable_retry=upload_then_reject))
    with pytest.raises(RuntimeError, match="approval binding changed"):
        env.handlers._apply_approved_values({"scan_id": SCAN, "file": file}, {"id": "j1"})
    row = st.get_hitl_item(item_id)
    assert row["status"] == "rejected" and not row["applied"]
    assert st.get_remediation_diffs(SCAN, file) == []


def test_gap10_one_documents_new_input_holds_every_approval_in_the_scan_and_recheck_is_actionable(env):
    """Audit gap 10, decided CONSERVATIVELY and pinned here.

    The approval binding is the scan-wide assessment revision (remediation_source_revision: the
    sealed assess manifest, else the discover/assess snapshot over the WHOLE inventory). Nothing
    recorded today identifies one document's assessment input inside it — manifest entries are
    keyed by job (`scan_batch:<job>` can cover several files), not by file — so a per-file identity
    could only be invented, and an invented one could ADMIT a stale approval. So a new input for
    document A also holds document B's approval: a false hold, never a false write. What keeps
    that from being a dead end is that the hold is visible (approval_recheck_required) and one
    re-approval of the version on screen re-binds it, after which the writer admits it.
    """
    from hitl_viewed import viewed_fields
    from routes.hitl import HitlUpdate, hitl_update
    st = env.st
    with st._db.cursor() as cur:
        for name in ("a.pptx", "b.pptx"):
            st._db.execute(cur, "INSERT INTO scan_inventory(scan_id,file,checksum,size_kb) "
                                "VALUES(%s,%s,%s,%s)", (SCAN, name, "sha-" + name, 10))
    request = SimpleNamespace(state=SimpleNamespace(user_email=ACTOR))
    ids = {}
    for name in ("a.pptx", "b.pptx"):
        ids[name] = st.enqueue_proposals(SCAN, name, "1.1.1", [
            {"locator": "ppt/slides/slide1.xml#Picture 1", "proposed_value": "Description of " + name,
             "before": "", "rationale": "draft"}])
        hitl_update(ids[name], HitlUpdate(status="approved", **viewed_fields(ids[name], st)), request)
    # Only document A's input changes (a new checksum for A alone).
    with st._db.cursor() as cur:
        st._db.execute(cur, "UPDATE scan_inventory SET checksum=%s WHERE scan_id=%s AND file=%s",
                       ("sha-a-edited", SCAN, "a.pptx"))
    listed = {r["id"]: r for r in st.list_hitl_queue(scan_id=SCAN)}
    assert listed[ids["a.pptx"]]["approval_recheck_required"] is True
    assert listed[ids["b.pptx"]]["approval_recheck_required"] is True     # the conservative hold

    import apply_alt
    written = []

    def lane(data, values, **kw):
        written.append(dict(values))
        raise ReachedLane("alt")
    env.monkeypatch.setattr(apply_alt, "apply_alt_text", lane)
    env.handlers._apply_approved_values({"scan_id": SCAN, "file": "b.pptx"}, {})
    assert written == []
    assert _held_lines(env.st) == [{"item_id": ids["b.pptx"], "refusal": "source_moved",
                                    "approval_recheck_required": True}]
    # The reason a client shows is actionable and names the way out.
    assert "Review it against the current version" in st.RETRY_SOURCE_MOVED

    # One re-approval of the version on screen re-binds B, and the writer admits it.
    rebound = hitl_update(ids["b.pptx"], HitlUpdate(status="approved",
                                                    **viewed_fields(ids["b.pptx"], st)), request)
    assert rebound["decision_version"] == 2
    with pytest.raises(ReachedLane):
        env.handlers._apply_approved_values({"scan_id": SCAN, "file": "b.pptx"}, {})
    assert written == [{"ppt/slides/slide1.xml#Picture 1": "Description of b.pptx"}]
    # A is untouched by B's re-check: still held until someone re-checks A.
    assert st.approved_write_hold(st.get_hitl_item(ids["a.pptx"])) == st.RETRY_SOURCE_MOVED

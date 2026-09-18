"""A late refusal of an approved write cannot change the ACTIVE corrected copy.

THE DEFECT. The approved writer uploaded its bytes over the canonical blob and only then took the
row-then-file locks and re-checked admission (a binding that moved, a row re-decided, a target
another verified fix removed). A refusal there rolled back the DB pointer and every credit, while
the bytes every reader served were already the refused write's.

THE FIX. The write is published to a new digest-scoped immutable object (the publication the
office retry already uses, blob.upload_immutable_retry, with its readback) and file_records is
pointed at it only inside the commit transaction, after the re-checks, with a compare-and-set on
the pointer the write was built from. Every corrected-copy reader resolves that pointer through
blob._remediated_client (download_remediated, download_report_evidence) — which, until this
change, could not: get_file_record did not return blob_url, so the reader always served the
mutable canonical object. There is no mutable fallback: an owner-less (SSO-less/demo) scan is
addressed under the same owner-scoped contract, and a name the reader cannot address is refused
BEFORE anything is uploaded.

These tests run the REAL blob module against an in-memory Azure service, force each late
refusal after the upload, and then read the corrected copy back through the real readers.
Synthetic fixtures only; no network.
"""
from __future__ import annotations

import hashlib
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "api"))

from test_remediation_source_cache_key import _FakeService  # noqa: E402

SCAN, FILE = "s-immutable", "deck.pptx"
LOC = "ppt/slides/slide1.xml#Picture 1"
PRIOR = b"PK-synthetic-prior-corrected-copy"


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _build(monkeypatch, owner, file=FILE):
    import blob
    import core
    import handlers
    import store as store_mod
    from proposals import Verification

    monkeypatch.setattr(store_mod, "_SQLITE_PATH", Path(tempfile.mkdtemp()) / "immutable.db")
    st = store_mod.Store()
    st.init_scan_run(SCAN, "drive", 1, "t0", "rubric", "hash")
    with st._db.cursor() as cur:
        st._db.execute(cur, "UPDATE scan_runs SET owner_email=%s WHERE id=%s", (owner, SCAN))
        st._db.execute(cur, "INSERT INTO file_records(scan_id,file,status,compliant) "
                            "VALUES(%s,%s,'fail',0)", (SCAN, file))
    monkeypatch.setattr(core, "store", st)
    monkeypatch.setattr(core, "fire_webhook", lambda *a, **k: None)
    service = _FakeService()
    monkeypatch.setattr(blob, "_ENABLED", True)
    monkeypatch.setattr(blob, "_service_client", lambda: service)
    # The prior committed artifact: published by an earlier remediation on the canonical path.
    url = blob.upload_remediated(owner, SCAN, file, PRIOR, "application/octet-stream")
    st.record_remediation(SCAN, file, blob_url=url, corrected_sha256=sha(PRIOR),
                          corrected_bytes=len(PRIOR))
    item_id = st.enqueue_proposals(SCAN, file, "1.1.1", [
        {"locator": LOC, "proposed_value": "A synthetic chart", "before": "", "rationale": "draft"}])
    st.complete_hitl_decision(item_id, "approved", None, None, resolution=None,
                              approved_values=None, actor=owner or "reviewer", detail=None)
    monkeypatch.setattr(handlers, "_verify_residual", lambda *a, **kw: Verification(True, ()))
    monkeypatch.setattr("apply_alt.apply_alt_text", lambda data, values, **kw: (
        data + b"-written", [{"locator": k, "before": "", "after": v} for k, v in values.items()], []))
    monkeypatch.setattr("office_alt_integrity.verify_alt_write", lambda *a, **k: True)
    monkeypatch.setattr("office_verified_retry.contradicted_captions", lambda *a, **k: [])
    monkeypatch.setattr("output_provenance.stamp_output", lambda data, name: data)
    return SimpleNamespace(st=st, blob=blob, handlers=handlers, service=service, item_id=item_id,
                           mp=monkeypatch, owner=owner, file=file,
                           canonical=f"remediated/{owner or 'demo'}/{SCAN}/{file}")


@pytest.fixture(params=["owner@example.test", None], ids=["owned", "ownerless"])
def env(monkeypatch, request):
    return _build(monkeypatch, request.param)


def _current(e):
    """The corrected copy as every reader resolves it, and the committed record."""
    record = e.st.get_file_record(SCAN, e.file) or {}
    return (e.blob.download_remediated(e.owner, SCAN, e.file),
            e.blob.download_report_evidence(e.owner, SCAN, e.file, max_bytes=10_000),
            record.get("corrected_sha256"), record.get("blob_url"))


def _run(e):
    e.handlers._apply_approved_values({"scan_id": SCAN, "file": e.file}, {"id": "j1"})


def test_an_admitted_write_publishes_a_new_immutable_object_and_moves_the_pointer(env):
    _run(env)
    written = PRIOR + b"-written"
    data, evidence, digest, url = _current(env)
    assert data == evidence == written and digest == sha(written)
    assert url.endswith(f"{env.canonical}.retry/{sha(written)}")
    assert env.service.blobs[env.canonical] == PRIOR     # the canonical object was never rewritten
    assert env.st.get_hitl_item(env.item_id)["applied"]


@pytest.mark.parametrize("refusal", ["binding_moved", "row_rejected", "target_removed"])
def test_a_late_refusal_leaves_the_active_artifact_bytes_and_pointer_unchanged(env, refusal):
    e = env
    before = _current(e)
    assert before[0] == PRIOR and before[2] == sha(PRIOR)
    import review_target_reconciliation as rtr
    uploaded = []
    real_removed = rtr.removed_item_ids
    e.mp.setattr(rtr, "removed_item_ids", lambda store, s, f: (
        {e.item_id} if uploaded and refusal == "target_removed" else real_removed(store, s, f)))
    real_upload = e.blob.upload_immutable_retry

    def upload_then_refuse(*args):
        url = real_upload(*args)
        uploaded.append(url)
        if refusal == "binding_moved":         # a re-assessment commits mid-write
            with e.st._db.cursor() as cur:
                e.st._db.execute(cur, "UPDATE scan_runs SET rubric_hash=%s WHERE id=%s",
                                 ("reassessed", SCAN))
        elif refusal == "row_rejected":        # the reviewer withdraws the approval mid-write
            e.st.complete_hitl_decision(e.item_id, "rejected", None, None, resolution=None,
                                        approved_values=None, actor="reviewer", detail=None)
        return url
    e.mp.setattr(e.blob, "upload_immutable_retry", upload_then_refuse)

    with pytest.raises(RuntimeError):
        _run(e)
    assert uploaded, "the refusal must come AFTER the bytes were uploaded, or this proves nothing"

    data, evidence, digest, url = _current(e)
    assert data == evidence == PRIOR, "a refused write changed the bytes readers are served"
    assert sha(data) == digest == sha(PRIOR)
    assert url == before[3] and e.service.blobs[e.canonical] == PRIOR
    assert not e.st.get_hitl_item(e.item_id)["applied"]
    assert e.st.get_remediation_diffs(SCAN, e.file) == []


def test_a_pointer_moved_by_another_writer_is_not_overwritten(env):
    """Compare-and-set on the pointer: the write was built on PRIOR; if another writer committed
    a newer artifact in between, publishing would silently discard its change."""
    e = env
    other = PRIOR + b"-other-writer"
    real_upload = e.blob.upload_immutable_retry

    def upload_then_other_commit(*args):
        url = real_upload(*args)
        other_url = real_upload(e.owner, SCAN, e.file, other, "application/octet-stream")
        e.st.record_remediation(SCAN, e.file, blob_url=other_url, corrected_sha256=sha(other),
                                corrected_bytes=len(other))
        return url
    e.mp.setattr(e.blob, "upload_immutable_retry", upload_then_other_commit)
    with pytest.raises(RuntimeError, match="corrected copy changed during this write"):
        _run(e)
    data, evidence, digest, _ = _current(e)
    assert data == evidence == other and digest == sha(other)
    assert not e.st.get_hitl_item(e.item_id)["applied"]


@pytest.mark.parametrize("name", ["100% plan.pptx", "odd\\name.pptx", "a/../b.pptx"])
def test_a_name_the_reader_cannot_address_is_refused_before_any_upload(monkeypatch, name):
    """No mutable fallback: overwriting the canonical object for these names would reopen the
    late-refusal overwrite. Nothing is uploaded, the approval stays, the prior copy stays."""
    from worker import FatalJobError
    e = _build(monkeypatch, "owner@example.test", file=name)
    calls = []
    e.mp.setattr(e.blob, "upload_immutable_retry", lambda *a: calls.append(a) or "x")
    e.mp.setattr(e.blob, "upload_remediated", lambda *a: calls.append(a) or "x")
    with pytest.raises(FatalJobError, match="cannot be published as an immutable corrected copy"):
        _run(e)
    assert calls == []
    assert e.service.blobs[e.canonical] == PRIOR
    row = e.st.get_hitl_item(e.item_id)
    assert row["status"] == "approved" and not row["applied"]
    assert [d["action"] for d in e.st.list_decisions(SCAN)].count("apply.publication_refused") == 1


def test_an_owned_scans_pointer_is_not_followed_for_a_caller_without_the_owner(monkeypatch):
    """The owner-less contract does not weaken the owned one: a caller that names no owner does
    not get an owned scan's digest-scoped copy."""
    e = _build(monkeypatch, "owner@example.test")
    _run(e)
    assert e.blob.download_remediated("owner@example.test", SCAN, FILE) == PRIOR + b"-written"
    assert e.blob.download_remediated(None, SCAN, FILE) is None
    assert e.blob.download_remediated("someone-else@example.test", SCAN, FILE) is None


def test_no_reader_bypasses_the_committed_pointer():
    """Delivery, saved-copy verification, previews, release and reports all read the corrected
    copy through blob.download_remediated / download_report_evidence, which resolve the pointer.
    A module that addressed the remediated container itself would read the canonical object
    whatever the committed record says — so none may."""
    import re
    offenders = []
    for path in sorted((ROOT / "api").rglob("*.py")):
        if path.name == "blob.py":
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        if re.search(r"ACP_BLOB_CONTAINER\b|blob\._CONTAINER\b|_blob\._CONTAINER\b|"
                     r"_remediated_client\(|blob\._blob_path\(|_blob\._blob_path\(", text):
            offenders.append(str(path.relative_to(ROOT)))
    assert offenders == []


def test_the_committed_pointer_is_what_get_file_record_returns():
    """The reader's only way to find the pointer. Pinned so it cannot silently drop out again."""
    import store as store_mod
    adapter = store_mod._SQLiteAdapter(str(Path(tempfile.mkdtemp()) / "pointer.db"))
    adapter.init_schema()
    with adapter.cursor() as cur:
        adapter.execute(cur, "INSERT INTO file_records(scan_id,file,blob_url) VALUES(%s,%s,%s)",
                        ("s", "f.docx", "https://fake/remediated/o/s/f.docx.retry/" + "a" * 64))
    reader = store_mod.Store.__new__(store_mod.Store)
    reader._db = adapter
    assert reader.get_file_record("s", "f.docx")["blob_url"].endswith("a" * 64)

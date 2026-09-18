"""Production-engine concurrency coverage for HITL request idempotency."""
from __future__ import annotations

import os
import sys
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "api"))
import store as store_mod  # noqa: E402

_PG = os.environ.get("DATABASE_URL", "")
pytestmark = pytest.mark.skipif(
    not _PG.startswith("postgres"), reason="needs the disposable PostgreSQL integration job")


def test_concurrent_duplicate_decision_commits_once():
    st = store_mod.Store()
    scan_id = f"hitl-idempotency-{uuid.uuid4()}"
    st.init_scan_run(scan_id, "drive", 1, "t0", "rubric", "hash")
    item_id = st.enqueue_proposals(scan_id, "deck.pptx", "1.1.1", [{
        "locator": "ppt/slides/slide1.xml#Picture 1",
        "proposed_value": "A quarterly revenue chart.",
    }])
    barrier = threading.Barrier(2)

    def decide():
        barrier.wait()
        return st.complete_hitl_decision(
            item_id, "approved", None, None,
            resolution=None, approved_values=["A quarterly revenue chart."],
            actor="reviewer@example.com", detail=None,
            request_id="same-transport-request", expected_version=0)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = [future.result() for future in [pool.submit(decide), pool.submit(decide)]]

    assert sorted(replayed for _row, replayed in results) == [False, True]
    row = st.get_hitl_item(item_id)
    assert row["decision_version"] == 1
    assert [d["action"] for d in st.list_decisions(scan_id)] == ["hitl.approved"]
    assert [j["type"] for j in st.list_jobs() if j.get("scan_id") == scan_id] == [
        "apply_approved_values"]


def test_viewed_binding_is_a_compare_and_set_under_the_row_lock():
    """Two reviewers approve the same row from the same viewed version with different text. The
    row lock serializes them: exactly one approval is recorded and bound to the viewed revision;
    the other is refused as a stale view and writes nothing — no second audit line, no second job.
    Then a re-assessment holds the recorded approval from the ordinary writer."""
    st = store_mod.Store()
    scan_id = f"hitl-viewed-{uuid.uuid4()}"
    st.init_scan_run(scan_id, "drive", 1, "t0", "rubric", "hash")
    item_id = st.enqueue_proposals(scan_id, "deck.pptx", "1.1.1", [{
        "locator": "ppt/slides/slide1.xml#Picture 1", "proposed_value": "A synthetic chart."}])
    viewed_revision = st.remediation_source_revision(scan_id)
    barrier = threading.Barrier(2)

    def decide(text, request_id):
        barrier.wait()
        try:
            return st.complete_hitl_decision(
                item_id, "approved", None, None, resolution=None, approved_values=[text],
                actor="reviewer@example.com", detail=None, request_id=request_id,
                expected_version=0, expected_proposal_snapshot_ids=[],
                expected_source_revision=viewed_revision, viewed=True,
                expected_corrected_sha256="none")
        except ValueError as exc:
            return str(exc)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = [f.result() for f in [pool.submit(decide, "First reviewer text", "r-1"),
                                        pool.submit(decide, "Second reviewer text", "r-2")]]
    assert sorted(r if isinstance(r, str) else "recorded" for r in results) == [
        "recorded", "stale viewed version"]
    row = st.get_hitl_item(item_id)
    assert row["decision_version"] == 1
    assert row["approved_source_revision"] == viewed_revision
    assert [d["action"] for d in st.list_decisions(scan_id)] == ["hitl.approved"]
    assert [j["type"] for j in st.list_jobs() if j.get("scan_id") == scan_id] == [
        "apply_approved_values"]
    with st._db.cursor() as cur:
        st._db.execute(cur, "UPDATE scan_runs SET rubric_hash=%s WHERE id=%s", ("moved", scan_id))
    assert st.approved_write_hold(st.get_hitl_item(item_id)) == st.RETRY_SOURCE_MOVED
    assert st.count_unapplied_approved_values(scan_id, "deck.pptx") == 1


def test_an_approval_is_bound_to_the_exact_corrected_artifact_on_postgres():
    """Same assessment revision, a different corrected sha: held on the real engine too."""
    st = store_mod.Store()
    scan_id = f"hitl-artifact-{uuid.uuid4()}"
    st.init_scan_run(scan_id, "drive", 1, "t0", "rubric", "hash")
    st.save_file_result(scan_id, {"file": "deck.pptx", "engine": "office", "status": "analysed",
                                  "score": 50, "compliant": 0, "skipped_rules": 0, "issues": []},
                        "2026-09-01T00:00:00Z")
    st.record_remediation(scan_id, "deck.pptx", corrected_sha256="a" * 64)
    item_id = st.enqueue_proposals(scan_id, "deck.pptx", "1.1.1", [{
        "locator": "ppt/slides/slide1.xml#Picture 1", "proposed_value": "A synthetic chart."}])
    st.complete_hitl_decision(
        item_id, "approved", None, None, resolution=None, approved_values=None,
        actor="reviewer@example.com", detail=None, expected_version=0,
        expected_proposal_snapshot_ids=[], viewed=True,
        expected_source_revision=st.remediation_source_revision(scan_id),
        expected_corrected_sha256="a" * 64)
    assert st.approved_write_hold(st.get_hitl_item(item_id)) is None
    revision = st.remediation_source_revision(scan_id)
    st.record_remediation(scan_id, "deck.pptx", corrected_sha256="b" * 64)
    assert st.remediation_source_revision(scan_id) == revision
    assert st.approved_write_hold(st.get_hitl_item(item_id)) == st.RETRY_ARTIFACT_MOVED
    assert st.count_unapplied_approved_values(scan_id, "deck.pptx") == 1


def test_same_scan_reassessment_history_is_captured_before_the_delete_on_postgres():
    """R-B2 on the real engine: the replaced assessment (a zero-finding baseline) is captured in
    the same transaction, under the row lock capture_outgoing takes, before issue rows go."""
    st = store_mod.Store()
    scan_id, owner = f"history-{uuid.uuid4()}", "reviewer@example.com"
    st.init_scan_run(scan_id, "gdrive", 1, "2026-09-01T00:00:00+00:00", "wcag-aa", "h1", owner=owner)
    doc = {"file": "doc.docx", "engine": ".net/office", "status": "certifiable", "score": 100,
           "compliant": 0, "skipped_rules": 0, "issues": [], "drive_file_id": "d1",
           "checksum": "c1"}
    assert st.save_file_result(scan_id, doc, "2026-09-01T01:00:00+00:00") is True
    assert st.save_file_result(scan_id, {**doc, "status": "uncertain", "score": 70, "issues": [
        {"ruleId": "DOCX-ALT-001", "wcag": "1.1.1 Non-text Content", "severity": "SERIOUS",
         "detail": "synthetic", "page": 1, "location": "docx:image:1"}]},
        "2026-09-01T02:00:00+00:00") is True
    prior = st.prior_assessment_in_scan(scan_id, "doc.docx", owner=owner)
    assert prior["issues"] == [] and prior["snapshot"]["zero_findings"] is True
    assert prior["file_row"]["status"] == "certifiable"


@pytest.mark.parametrize("late", ["none", "binding_moved"])
def test_approved_write_pointer_moves_only_on_commit_on_postgres(monkeypatch, late):
    """The immutable publication + pointer compare-and-set on the real engine: lock_file's
    FOR UPDATE read feeds the CAS, and a late refusal leaves the committed artifact in force."""
    import hashlib
    import blob
    import core
    import handlers
    from proposals import Verification
    from test_remediation_source_cache_key import _FakeService
    st = store_mod.Store()
    scan_id, owner, file = f"immutable-{uuid.uuid4()}", "owner@example.test", "deck.pptx"
    st.init_scan_run(scan_id, "drive", 1, "t0", "rubric", "hash")
    with st._db.cursor() as cur:
        st._db.execute(cur, "UPDATE scan_runs SET owner_email=%s WHERE id=%s", (owner, scan_id))
        st._db.execute(cur, "INSERT INTO file_records(scan_id,file,status,compliant) "
                            "VALUES(%s,%s,'fail',0)", (scan_id, file))
    monkeypatch.setattr(core, "store", st)
    monkeypatch.setattr(core, "fire_webhook", lambda *a, **k: None)
    service = _FakeService()
    monkeypatch.setattr(blob, "_ENABLED", True)
    monkeypatch.setattr(blob, "_service_client", lambda: service)
    prior = b"PK-synthetic-prior"
    st.record_remediation(scan_id, file, corrected_sha256=hashlib.sha256(prior).hexdigest(),
                          blob_url=blob.upload_remediated(owner, scan_id, file, prior, "x"),
                          corrected_bytes=len(prior))
    item_id = st.enqueue_proposals(scan_id, file, "1.1.1", [
        {"locator": "ppt/slides/slide1.xml#Picture 1", "proposed_value": "A synthetic chart"}])
    st.complete_hitl_decision(item_id, "approved", None, None, resolution=None,
                              approved_values=None, actor=owner, detail=None)
    monkeypatch.setattr(handlers, "_verify_residual", lambda *a, **kw: Verification(True, ()))
    monkeypatch.setattr("apply_alt.apply_alt_text", lambda data, values, **kw: (
        data + b"-written", [{"locator": k, "before": "", "after": v} for k, v in values.items()], []))
    monkeypatch.setattr("office_alt_integrity.verify_alt_write", lambda *a, **k: True)
    monkeypatch.setattr("office_verified_retry.contradicted_captions", lambda *a, **k: [])
    monkeypatch.setattr("output_provenance.stamp_output", lambda data, name: data)
    real_upload = blob.upload_immutable_retry

    def upload(*args):
        url = real_upload(*args)
        if late == "binding_moved":
            with st._db.cursor() as cur:
                st._db.execute(cur, "UPDATE scan_runs SET rubric_hash=%s WHERE id=%s",
                               ("moved", scan_id))
        return url
    monkeypatch.setattr(blob, "upload_immutable_retry", upload)
    if late == "none":
        handlers._apply_approved_values({"scan_id": scan_id, "file": file}, {"id": "j1"})
        expected = prior + b"-written"
    else:
        with pytest.raises(RuntimeError):
            handlers._apply_approved_values({"scan_id": scan_id, "file": file}, {"id": "j1"})
        expected = prior
    assert blob.download_remediated(owner, scan_id, file) == expected
    assert st.get_file_record(scan_id, file)["corrected_sha256"] == hashlib.sha256(expected).hexdigest()


@pytest.mark.parametrize('mutation', ['none', 'review_changed_upload', 'consent_changed_upload', 'serialized_review_note'])
def test_office_retry_commit_rechecks_exact_authority_on_postgres(monkeypatch, mutation):
    """Use the actual queued Office writer on a separate guarded disposable database.

    Review and standing consent changes during the external immutable upload must
    leave the original pointer and contribution unchanged. The unchanged control
    proves the real PostgreSQL FOR UPDATE/CAS branch still commits verified bytes.
    """
    import psycopg2
    from psycopg2 import sql
    from urllib.parse import urlparse
    from conftest import require_disposable_postgres
    import test_office_verified_retry as fixture
    from proposals import Verification
    # This job isolates PostgreSQL commit/lock semantics. The full backend fixture
    # separately invokes the actual Office scanner; this job has no .NET runtime.
    def caption_presence(data, filename):
        target=fixture.image_target(data, fixture.LOC)
        return Verification(True, set() if target and target[0] else {'1.1.1'})
    monkeypatch.setattr(fixture,'verify_residual',caption_presence)
    require_disposable_postgres(_PG)
    admin=psycopg2.connect(_PG)
    admin.autocommit=True
    require_disposable_postgres(_PG, conn=admin)
    name='office_retry_' + uuid.uuid4().hex + '_test'
    st=None
    try:
        with admin.cursor() as cur:
            cur.execute(sql.SQL('CREATE DATABASE {}').format(sql.Identifier(name)))
        isolated=urlparse(_PG)._replace(path='/' + name).geturl()
        require_disposable_postgres(isolated)
        monkeypatch.setattr(store_mod, '_DATABASE_URL', isolated)
        st=store_mod.Store()
        assert st._db.supports_for_update and st._db.supports_skip_locked
        worker=None; completed=threading.Event(); started=threading.Event(); shared={}
        if mutation=='serialized_review_note':
            from time import monotonic, sleep
            actual_cas=st.compare_and_set_retry_artifact
            def commit_with_competing_note(*args, **kwargs):
                def competing_note():
                    connection=psycopg2.connect(isolated)
                    try:
                        connection.autocommit=True
                        shared['pid']=connection.get_backend_pid()
                        with connection.cursor() as cur:
                            cur.execute("SET lock_timeout='3s'")
                            started.set()
                            cur.execute("UPDATE hitl_queue SET reviewer_note='concurrent note' WHERE scan_id='scan' AND file='file.docx'")
                    except Exception as exc:
                        shared['error']=exc
                    finally:
                        connection.close(); completed.set()
                nonlocal worker
                worker=threading.Thread(target=competing_note)
                worker.start(); assert started.wait(1)
                deadline=monotonic()+1; locked=False
                while monotonic()<deadline:
                    with admin.cursor() as cur:
                        cur.execute('SELECT wait_event_type FROM pg_stat_activity WHERE pid=%s',(shared['pid'],))
                        row=cur.fetchone();locked=bool(row and row[0]=='Lock')
                    if locked:break
                    sleep(.01)
                assert locked and not completed.is_set()
                return actual_cas(*args, **kwargs)
            monkeypatch.setattr(st,'compare_and_set_retry_artifact',commit_with_competing_note)
        fixture.test_live_adapter_uses_restored_frozen_run_and_only_next_settled_model(st, monkeypatch,
            'none' if mutation=='serialized_review_note' else mutation)
        if worker is not None:
            worker.join(3)
            assert completed.is_set() and 'error' not in shared

    finally:
        if st is not None and st._db._pool is not None:
            st._db._pool.closeall()
        with admin.cursor() as cur:
            cur.execute(sql.SQL('DROP DATABASE IF EXISTS {} WITH (FORCE)').format(sql.Identifier(name)))
        admin.close()

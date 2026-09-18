"""A correction saved AFTER publication must never read as published/current.

Production (SharePoint): Release published corrected copy V1 with authorized remaining issues and
the follow-up reports completed. The user then approved one more value, apply_approved_values saved
V2 as file_records.corrected_sha256 — and the publication receipt, the reports and the durable
release status all went on reading as published and current while the provider still held V1.

These tests run on the real isolated SQLite store. The first three reproduce that state as it was
on 16a6919f (reports current, durable status column stuck at 'running', a publish job failing after
a successful provider write); the rest pin the server-authored currency projection and the explicit,
digest-bound republish action that replaces it.
"""
import json
from types import SimpleNamespace

import pytest
from fastapi import HTTPException, Response

import release_report_delivery as delivery

OWNER = 'owner@example.com'
FOREIGN = 'foreign@example.com'
SID = 'scan-refresh'
FILE = 'doc.pdf'
V1 = '1' * 64
V2 = '2' * 64
PUBLISHED_AT = '2026-09-10T01:00:00+00:00'


def tag(digest):
    return 'sha256:' + digest


def request(owner=OWNER, headers=None):
    return SimpleNamespace(state=SimpleNamespace(user_email=owner), headers=headers or {})


def correct_to(store, digest, *, compliant=0, at='2026-09-11T00:00:00+00:00'):
    """What apply_approved_values leaves behind: a new durable corrected copy."""
    with store._db.cursor() as cur:
        store._db.execute(cur, 'UPDATE file_records SET corrected_sha256=%s,remediated_at=%s,compliant=%s '
                          'WHERE scan_id=%s AND file=%s', (digest, at, compliant, SID, FILE))


def column_status(store, release_id):
    with store._db.cursor() as cur:
        store._db.execute(cur, 'SELECT status,updated_at FROM release_executions WHERE id=%s', (release_id,))
        return store._db.fetchone(cur)


def jobs(store, job_type='publish_file'):
    with store._db.cursor() as cur:
        store._db.execute(cur, 'SELECT status,payload FROM jobs WHERE type=%s', (job_type,))
        return [json.loads(row['payload']) for row in store._db.fetchall(cur)]


def decisions(store, action):
    with store._db.cursor() as cur:
        store._db.execute(cur, 'SELECT actor,file,detail FROM decision_log WHERE action=%s ORDER BY ts', (action,))
        return store._db.fetchall(cur)


@pytest.fixture
def world(isolated_store, monkeypatch):
    """V1 published to SharePoint with remaining issues authorized; reports delivered."""
    import core
    import release_continuation
    import release_reports
    store = isolated_store
    monkeypatch.setattr(core, 'store', store)
    monkeypatch.setattr(core, 'get_scan_tokens', lambda sid: {'sp': 'secret-fixture'})
    monkeypatch.setattr(release_continuation, 'require_grants', lambda *a, **k: None)
    store.init_scan_run(SID, 'sharepoint', 1, '2026-09-09T10:00:00Z', 'rubric', 'hash', owner=OWNER, status='completed')
    with store._db.cursor() as cur:
        store._db.execute(cur, "INSERT INTO file_records(scan_id,file,engine,status,compliant,corrected_sha256,remediated_at,drive_file_id) "
                          "VALUES(%s,%s,'pdf','analysed',0,%s,'2026-09-10T00:00:00+00:00','source-item')", (SID, FILE, V1))
    release = store.ensure_release_execution(SID, OWNER, 'sharepoint', 1, parent_folder_id='library/parent',
                                             parent_folder_name='Parent')
    store.record_release_root(release['id'], OWNER, 'sharepoint', 'graph:library', 'release-folder', 'Release',
                              'https://example.com/release')
    store.record_release_document(release['id'], OWNER, dict(
        file=FILE, status='published', artifact_digest=tag(V1), published_at=PUBLISHED_AT,
        published_url='https://example.com/doc-v1', released_document_id='item-v1',
        released_relative_path='Policies/doc.pdf', verification='content verified'))
    monkeypatch.setattr(release_reports, 'build_release_reports', lambda *a: [dict(
        name='changes-doc.pdf', content=b'%PDF-1.7 describes V1', content_type='application/pdf',
        report_kind='changes', file=FILE, artifact_digest=tag(V1))])
    bundle = delivery.queue_release_reports(store, SID, OWNER, release['id'])
    monkeypatch.setattr(delivery, '_upload', lambda *a: dict(id='report', url='https://example.com/report'))
    assert delivery.process_release_reports(store, bundle['bundle_id'], OWNER)['status'] == 'completed'
    return SimpleNamespace(store=store, release_id=release['id'], bundle_id=bundle['bundle_id'])


# --- reproductions of the production state -------------------------------------------------------

def test_reports_read_out_of_date_when_the_copy_changes_after_publication(world):
    store = world.store
    before = delivery.get_latest_release_reports(store, SID, OWNER)
    assert (before['currency'], before['currency_reason'], before['out_of_date_files']) == ('current', None, [])
    frozen = delivery.get_release_report_asset(store, SID, OWNER, world.bundle_id, 0)
    with store._db.cursor() as cur:
        store._db.execute(cur, 'SELECT * FROM release_report_bundles WHERE id=%s', (world.bundle_id,))
        row_before = store._db.fetchone(cur)
    receipt_before = store.get_release_document(world.release_id, FILE, OWNER)

    correct_to(store, V2)
    latest = delivery.get_latest_release_reports(store, SID, OWNER)
    # The release documents did not change, so the report fingerprint did not either. That is
    # exactly why this used to read as current: currency was only checked on a fingerprint change.
    assert latest['bundle_id'] == world.bundle_id and latest['status'] == 'completed'
    assert latest['currency'] == 'out_of_date'
    assert latest['currency_reason'] == 'copy_changed_after_publication'
    assert latest['out_of_date_files'] == [
        {'file': FILE, 'reported_artifact_digest': tag(V1), 'current_artifact_digest': tag(V2)}]
    assert latest['can_regenerate'] is False
    assert latest['regeneration_blocked'] == delivery.REPORT_COPY_CHANGED
    # Immutable history: the frozen bundle, its asset bytes and the receipt are untouched.
    with store._db.cursor() as cur:
        store._db.execute(cur, 'SELECT * FROM release_report_bundles WHERE id=%s', (world.bundle_id,))
        assert store._db.fetchone(cur) == row_before
    assert delivery.get_release_report_asset(store, SID, OWNER, world.bundle_id, 0) == frozen
    assert store.get_release_document(world.release_id, FILE, OWNER) == receipt_before


def test_legacy_receipt_without_exact_identity_reports_unknown_currency(world):
    store = world.store
    with store._db.cursor() as cur:
        store._db.execute(cur, 'UPDATE release_documents SET artifact_digest=NULL WHERE release_id=%s', (world.release_id,))
    # Recreate a bundle for the legacy receipt shape; a legacy row never claims an identity.
    with store._db.cursor() as cur:
        store._db.execute(cur, 'DELETE FROM release_report_bundles')
    bundle = delivery.queue_release_reports(store, SID, OWNER, world.release_id)
    assert (bundle['currency'], bundle['currency_reason']) == ('unknown', 'legacy_identity')
    assert bundle['out_of_date_files'] == []


def test_durable_release_status_column_settles_with_every_receipt(world):
    store, rid = world.store, world.release_id
    # The fixture's release published its only document: the column must agree with the stage.
    assert column_status(store, rid)['status'] == 'completed'
    settled_at = column_status(store, rid)['updated_at']
    # An identical write changes nothing, so updated_at does not move.
    store.record_release_document(rid, OWNER, dict(file=FILE, status='published', artifact_digest=tag(V1)))
    assert column_status(store, rid) == {'status': 'completed', 'updated_at': settled_at}
    store.record_release_document(rid, OWNER, dict(file=FILE, status='queued'))
    assert column_status(store, rid)['status'] == 'running'
    store.record_release_document(rid, OWNER, dict(file=FILE, status='published', artifact_digest=tag(V2)))
    assert column_status(store, rid)['status'] == 'completed'
    # A foreign owner cannot write a receipt, and so cannot move the column either.
    store.record_release_document(rid, FOREIGN, dict(file=FILE, status='failed'))
    assert column_status(store, rid)['status'] == 'completed'
    # Growing the release reopens it; a failure with nothing delivered settles as attention.
    store.ensure_release_execution(SID, OWNER, 'sharepoint', 2)
    assert column_status(store, rid)['status'] == 'running'
    store.record_release_document(rid, OWNER, dict(file='other.pdf', status='failed', failure_category='provider_write_failed'))
    assert column_status(store, rid)['status'] == 'attention'


@pytest.mark.parametrize('failure', [
    dict(failure_category='provider_write_failed', explanation='The provider refused the write.'),
    # This category stamps the ATTEMPTED digest; it must not become the delivered identity.
    dict(failure_category='release_assessment_remaining', explanation='Remaining issues.', artifact_digest=tag(V2)),
    dict(failure_category='release_evidence_changed', explanation='The corrected artifact changed.'),
])
def test_a_failed_attempt_never_erases_the_delivered_receipt(world, failure):
    store, rid = world.store, world.release_id
    delivered = store.get_release_document(rid, FILE, OWNER)
    correct_to(store, V2)
    store.record_release_document(rid, OWNER, dict(file=FILE, status='queued'))
    store.record_release_document(rid, OWNER, dict(file=FILE, status='failed', released_relative_path=None, **failure))
    row = store.get_release_document(rid, FILE, OWNER)
    # The delivered row is restored EXACTLY — every column is part of the report fingerprint, so
    # even the attempt's failure fields must not land on it (they re-identified the reports).
    assert row == delivered
    assert column_status(store, rid)['status'] == 'completed'
    import json
    import release_publication
    attempts = [r for r in store.list_decisions(SID) if r['action'] == 'release.publish_attempt_failed']
    assert len(attempts) == 1 and attempts[0]['file'] == FILE
    detail = json.loads(attempts[0]['detail'])
    assert detail['release_id'] == rid and detail['published_artifact_digest'] == delivered['artifact_digest']
    assert (detail['failure_category'], detail['explanation']) == (failure['failure_category'], failure['explanation'])
    publication = release_publication.project(store, SID, OWNER, store.release_status(rid, OWNER))['publication']
    assert publication['state'] == 'out_of_date' and publication['can_republish'] is True
    last = publication['out_of_date'][0]['last_attempt_failure']
    assert (last['failure_category'], last['explanation']) == (failure['failure_category'], failure['explanation'])
    assert publication['out_of_date'][0]['published_artifact_digest'] == tag(V1)


def test_publish_job_is_not_failed_when_reports_cannot_describe_a_changed_copy(world, monkeypatch):
    import handlers
    store = world.store
    correct_to(store, V2)
    # The provider write succeeded for a document whose copy was corrected in the meantime.
    def published(payload, job):
        store.record_release_document(world.release_id, OWNER, dict(
            file=FILE, status='published', artifact_digest=tag(V1), published_at='2026-09-11T02:00:00+00:00'))
    monkeypatch.setattr(handlers, '_publish_file_guarded', published)
    payload = {'scan_id': SID, 'owner': OWNER, 'release_id': world.release_id, 'file': FILE}
    published(payload, {})
    # This is the refusal that used to fail the whole (already delivered) publish job.
    with pytest.raises(ValueError, match='saved copy changed'):
        delivery.queue_if_release_settled(store, SID, OWNER, world.release_id)
    handlers._publish_file(payload, {'id': 'job-1'})  # must not raise
    import release_publication
    state = release_publication.project(store, SID, OWNER, store.release_status(world.release_id, OWNER))
    assert state['publication']['state'] == 'out_of_date'
    # Only the two "reports describe a different copy" refusals are absorbed.
    def broken(*args):
        raise ValueError('something else')
    monkeypatch.setattr(delivery, 'queue_if_release_settled', broken)
    with pytest.raises(ValueError, match='something else'):
        handlers._publish_file(payload, {'id': 'job-2'})


def test_retried_job_for_an_already_delivered_digest_completes_without_touching_the_receipt(world, monkeypatch):
    """The second attempt of a V1 job, after V2 was saved, used to fail the delivered V1 row."""
    import handlers
    import release_report_delivery
    store = world.store
    correct_to(store, V2)
    before = store.get_release_document(world.release_id, FILE, OWNER)
    monkeypatch.setattr(handlers, '_release_failure', lambda *a, **k: pytest.fail('a delivered receipt must not be failed'))
    import publish
    monkeypatch.setattr(publish, 'remediated_content_digest', lambda *a: pytest.fail('nothing to re-validate'))
    payload = {'scan_id': SID, 'owner': OWNER, 'release_id': world.release_id, 'file': FILE,
               'artifact_digest': tag(V1), 'remediated_at': '2026-09-10T00:00:00+00:00', 'allow_remaining_issues': True}
    assert handlers._publish_file(payload, {'id': 'job-retry', 'attempts': 2, 'max_attempts': 5}) is None
    assert store.get_release_document(world.release_id, FILE, OWNER) == before
    assert release_report_delivery.get_latest_release_reports(store, SID, OWNER)['currency'] == 'out_of_date'


# --- projection ----------------------------------------------------------------------------------

def _record(digest=V2, compliant=0, remediated_at='t'):
    return {'corrected_sha256': digest, 'compliant': compliant, 'remediated_at': remediated_at}


@pytest.mark.parametrize('document,record,expected', [
    (dict(status='published', artifact_digest=tag(V1)), _record(V1), ('current', tag(V1))),
    (dict(status='published', artifact_digest=tag(V1)), _record(V2), ('out_of_date', tag(V1))),
    # Legacy receipts never claim current, whatever the record says.
    (dict(status='published', artifact_digest=None), _record(V1), ('identity_unknown', None)),
    (dict(status='published', artifact_digest='provider-md5'), _record(V1), ('identity_unknown', None)),
    (dict(status='failed', published_at=PUBLISHED_AT), _record(V1), ('identity_unknown', None)),
    (dict(status='published', artifact_digest=tag(V1)), _record(None), ('identity_unknown', tag(V1))),
    (dict(status='queued', artifact_digest=tag(V1), published_at=PUBLISHED_AT), _record(V2), ('publishing', tag(V1))),
    (dict(status='running'), _record(V2), ('publishing', None)),
    # A failed republish leaves the provider holding the old copy: retry must stay possible.
    (dict(status='failed', artifact_digest=tag(V1), published_at=PUBLISHED_AT), _record(V2), ('out_of_date', tag(V1))),
    (dict(status='interrupted', artifact_digest=tag(V1), published_at=PUBLISHED_AT), _record(V2), ('out_of_date', tag(V1))),
    (dict(status='failed', artifact_digest=tag(V2), published_at=PUBLISHED_AT), _record(V2), ('failed', tag(V2))),
    (dict(status='failed'), _record(V2), ('failed', None)),
    # These categories stamp the ATTEMPTED digest over the row; the held copy is then unknown.
    (dict(status='failed', artifact_digest=tag(V2), published_at=PUBLISHED_AT,
          failure_category='release_assessment_remaining'), _record('3' * 64), ('identity_unknown', None)),
])
def test_document_publication_state(document, record, expected):
    import release_publication
    state = release_publication.document_state({'file': FILE, **document}, record)
    assert (state['publication_state'], state['published_artifact_digest']) == expected
    assert state['current_artifact_digest'] == (tag(record['corrected_sha256']) if record['corrected_sha256'] else None)


def test_projection_out_of_date_and_republish_gates(world, monkeypatch):
    import release_publication
    store = world.store
    project = lambda: release_publication.project(store, SID, OWNER, store.release_status(world.release_id, OWNER))
    current = project()
    assert current['publication'] == {'state': 'current', 'out_of_date': [], 'identity_unknown': [],
                                      'can_republish': False, 'republish_blocked_reason': None}
    correct_to(store, V2, at='2026-09-11T00:00:00+00:00')
    stale = project()
    assert stale['documents'][0]['publication_state'] == 'out_of_date'
    assert stale['publication'] == {
        'state': 'out_of_date',
        'out_of_date': [{'file': FILE, 'published_artifact_digest': tag(V1), 'current_artifact_digest': tag(V2),
                         'published_at': PUBLISHED_AT, 'current_remediated_at': '2026-09-11T00:00:00+00:00',
                         'requires_remaining_issue_confirmation': True, 'last_attempt_failure': None}],
        'identity_unknown': [], 'can_republish': True, 'republish_blocked_reason': None}
    correct_to(store, V2, compliant=1)
    assert project()['publication']['out_of_date'][0]['requires_remaining_issue_confirmation'] is False
    monkeypatch.setattr(store, 'count_unapplied_approved_values', lambda sid, file: 1)
    blocked = project()['publication']
    assert blocked['state'] == 'out_of_date' and blocked['can_republish'] is False
    assert blocked['republish_blocked_reason'] == release_publication.REPUBLISH_UNAPPLIED


def test_projection_precedence_and_not_published(world):
    import release_publication
    store = world.store
    assert release_publication.project(store, SID, OWNER, None)['publication']['state'] == 'not_published'
    correct_to(store, V2)
    store.record_release_document(world.release_id, OWNER, dict(file=FILE, status='queued'))
    in_flight = release_publication.project(store, SID, OWNER, store.release_status(world.release_id, OWNER))
    assert in_flight['publication']['state'] == 'publishing'
    assert in_flight['publication']['can_republish'] is False
    assert in_flight['publication']['out_of_date'] == []
    # The republish failed: the provider still holds V1, so the retry is offered again.
    store.record_release_document(world.release_id, OWNER, dict(file=FILE, status='failed', failure_category='provider_write_failed'))
    failed = release_publication.project(store, SID, OWNER, store.release_status(world.release_id, OWNER))
    assert failed['publication']['state'] == 'out_of_date' and failed['publication']['can_republish'] is True
    assert failed['documents'][0]['published_artifact_digest'] == tag(V1)
    store.record_release_document(world.release_id, OWNER, dict(file='never.pdf', status='failed'))
    mixed = release_publication.project(store, SID, OWNER, store.release_status(world.release_id, OWNER))
    assert mixed['publication']['state'] == 'out_of_date'
    store.record_release_document(world.release_id, OWNER, dict(file=FILE, status='published', artifact_digest=tag(V2)))
    attention = release_publication.project(store, SID, OWNER, store.release_status(world.release_id, OWNER))
    assert attention['publication']['state'] == 'attention'


# --- routes --------------------------------------------------------------------------------------

@pytest.fixture
def routes(world, monkeypatch):
    from routes import scans
    monkeypatch.setattr(scans, '_register_scan_tokens', lambda *a, **k: None)
    monkeypatch.setattr(scans.core, 'register_scan_tokens', lambda *a, **k: None)
    return scans


def republish(scans, body, owner=OWNER):
    return scans.republish_release(SID, scans.ReleaseRepublishRequest(**body), request(owner, {'x-sp-token': 'sp-fixture'}))


def conflict(scans, body, owner=OWNER):
    with pytest.raises(HTTPException) as exc:
        republish(scans, body, owner)
    return exc.value


def test_release_get_carries_currency_and_is_owner_scoped(routes, world):
    status = routes.get_release_status(SID, request())
    assert status['publication']['state'] == 'current'
    assert status['documents'][0]['publication_state'] == 'current'
    correct_to(world.store, V2)
    status = routes.get_release_status(SID, request())
    assert status['publication']['state'] == 'out_of_date'
    assert status['documents'][0]['published_artifact_digest'] == tag(V1)
    assert status['documents'][0]['current_artifact_digest'] == tag(V2)
    with pytest.raises(HTTPException) as exc:
        routes.get_release_status(SID, request(FOREIGN))
    assert exc.value.status_code == 404
    history = routes.get_release_history(request(), limit=50)['releases'][0]
    assert history['publication_state'] == 'out_of_date'
    assert history['documents'][0]['publication_state'] == 'out_of_date'
    assert routes.get_release_history(request(FOREIGN), limit=50)['releases'] == []
    reports = routes.get_release_reports(SID, request(), Response())
    assert reports['currency'] == 'out_of_date'
    # Never hand back V1's bundle as a "refresh", and never rebuild it from V2 repair records.
    with pytest.raises(HTTPException) as refused:
        routes.retry_release_reports(SID, request(headers={'x-sp-token': 'sp'}), Response())
    assert refused.value.status_code == 409 and refused.value.detail == delivery.REPORT_COPY_CHANGED


def test_legacy_identity_cannot_be_republished(routes, world):
    store = world.store
    with store._db.cursor() as cur:
        store._db.execute(cur, 'UPDATE release_documents SET artifact_digest=NULL WHERE release_id=%s', (world.release_id,))
    correct_to(store, V2)
    status = routes.get_release_status(SID, request())
    assert status['documents'][0]['publication_state'] == 'identity_unknown'
    assert status['publication']['identity_unknown'] == [FILE]
    assert status['publication']['can_republish'] is False
    error = conflict(routes, {'expected_artifacts': {FILE: V2}, 'allow_remaining_issues': True})
    assert error.detail['code'] == 'republish_blocked' and 'Reconcile' in error.detail['message']
    assert jobs(store) == []


def test_republish_refusals(routes, world, monkeypatch):
    store = world.store
    correct_to(store, V2)
    before = len(jobs(store))
    # Stale confirmation: the user confirmed a copy that is no longer the current one.
    error = conflict(routes, {'expected_artifacts': {FILE: V1}, 'allow_remaining_issues': True})
    assert error.status_code == 409 and error.detail['code'] == 'artifact_changed'
    # V2 still has remaining issues; the V1 authorization is never reused.
    error = conflict(routes, {'expected_artifacts': {FILE: V2}})
    assert error.status_code == 409 and error.detail['code'] == 'remaining_issues_confirmation_required'
    assert error.detail['files'] == [FILE]
    with monkeypatch.context() as patch:
        patch.setattr(store, 'count_unapplied_approved_values', lambda sid, file: 2)
        assert routes.get_release_status(SID, request())['publication']['can_republish'] is False
        error = conflict(routes, {'expected_artifacts': {FILE: V2}, 'allow_remaining_issues': True})
    assert error.status_code == 409 and error.detail['code'] == 'republish_blocked'
    assert 'still being saved' in error.detail['message']
    # A corrected document that was never part of this release is not "out of date" here.
    with store._db.cursor() as cur:
        store._db.execute(cur, "INSERT INTO file_records(scan_id,file,engine,status,compliant,corrected_sha256,remediated_at) "
                          "VALUES(%s,'other.pdf','pdf','analysed',1,%s,'t')", (SID, V2))
    error = conflict(routes, {'expected_artifacts': {'other.pdf': V2}, 'allow_remaining_issues': True})
    assert error.status_code == 409 and error.detail['code'] == 'republish_blocked'
    assert error.detail['files'] == ['other.pdf']
    with pytest.raises(HTTPException) as exc:
        republish(routes, {'expected_artifacts': {}})
    assert exc.value.status_code == 422
    with pytest.raises(HTTPException) as exc:
        republish(routes, {'expected_artifacts': {FILE: V2}, 'allow_remaining_issues': True}, owner=FOREIGN)
    assert exc.value.status_code == 404
    assert len(jobs(store)) == before
    assert decisions(store, 'release.republish_authorized') == []


def test_republish_authorizes_exact_digest_and_is_idempotent(routes, world):
    store, rid = world.store, world.release_id
    correct_to(store, V2)
    result = republish(routes, {'expected_artifacts': {FILE: V2}, 'allow_remaining_issues': True})
    assert result['republished'] == [FILE] and result['already_current'] is False
    queued = [job for job in jobs(store) if job.get('release_id') == rid]
    assert [(job['file'], job['artifact_digest']) for job in queued] == [(FILE, tag(V2))]
    assert result['release']['publication']['state'] == 'publishing'
    assert column_status(store, rid)['status'] == 'running'
    # The audit binds the authorization to the exact new digest and names what it replaces.
    [authorized] = decisions(store, 'release.republish_authorized')
    detail = json.loads(authorized['detail'])
    assert (authorized['actor'], authorized['file']) == (OWNER, FILE)
    assert detail == {'release_id': rid, 'previous_artifact_digest': tag(V1), 'previous_published_at': PUBLISHED_AT,
                      'previous_url': 'https://example.com/doc-v1', 'artifact_digest': tag(V2),
                      'allow_remaining_issues': True}
    remaining = [json.loads(row['detail']) for row in decisions(store, 'release.remaining_issues_authorized')]
    assert [row['artifact_digest'] for row in remaining] == [tag(V2)]
    # A retry of the same click while it is still publishing starts nothing new.
    again = republish(routes, {'expected_artifacts': {FILE: V2}, 'allow_remaining_issues': True})
    assert again['republished'] == [] and again['result'] == 'already_publishing'
    assert len([job for job in jobs(store) if job.get('release_id') == rid]) == 1
    # A DIFFERENT copy cannot be started while V2 is still being delivered.
    correct_to(store, '3' * 64)
    error = conflict(routes, {'expected_artifacts': {FILE: '3' * 64}, 'allow_remaining_issues': True})
    assert error.detail['code'] == 'republish_blocked'
    correct_to(store, V2)
    # The worker delivers V2.
    store.record_release_document(rid, OWNER, dict(file=FILE, status='published', artifact_digest=tag(V2),
                                                    published_at='2026-09-11T03:00:00+00:00'))
    assert column_status(store, rid)['status'] == 'completed'
    status = routes.get_release_status(SID, request())
    assert status['publication']['state'] == 'current'
    done = republish(routes, {'expected_artifacts': {FILE: V2}, 'allow_remaining_issues': True})
    assert done['already_current'] is True and done['result'] == 'already_current' and done['republished'] == []
    assert len([job for job in jobs(store) if job.get('release_id') == rid]) == 1
    assert len(decisions(store, 'release.republish_authorized')) == 1


def test_mixed_republish_authorizes_remaining_issues_only_where_needed(routes, world):
    """One compliant file and one that still has remaining issues, in ONE request and ONE batch.
    The single allow_remaining_issues flag must not become an authorization for the compliant one."""
    store, rid = world.store, world.release_id
    w1, w2 = 'a' * 64, 'b' * 64
    with store._db.cursor() as cur:
        store._db.execute(cur, "INSERT INTO file_records(scan_id,file,engine,status,compliant,corrected_sha256,remediated_at,drive_file_id) "
                          "VALUES(%s,'two.pdf','pdf','analysed',1,%s,'t2','source-two')", (SID, w2))
    store.ensure_release_execution(SID, OWNER, 'sharepoint', 2)
    store.record_release_document(rid, OWNER, dict(file='two.pdf', status='published', artifact_digest=tag(w1),
                                                    published_at=PUBLISHED_AT, published_url='https://example.com/two-v1'))
    correct_to(store, V2)  # doc.pdf: compliant=0, so it needs the confirmation
    stale = routes.get_release_status(SID, request())['publication']['out_of_date']
    assert {row['file']: row['requires_remaining_issue_confirmation'] for row in stale} == {FILE: True, 'two.pdf': False}
    result = republish(routes, {'expected_artifacts': {FILE: V2, 'two.pdf': w2}, 'allow_remaining_issues': True})
    assert sorted(result['republished']) == [FILE, 'two.pdf']
    authorized = [(row['file'], json.loads(row['detail'])['artifact_digest'])
                  for row in decisions(store, 'release.remaining_issues_authorized')]
    assert authorized == [(FILE, tag(V2))]
    audit = {row['file']: json.loads(row['detail'])['allow_remaining_issues']
             for row in decisions(store, 'release.republish_authorized')}
    assert audit == {FILE: True, 'two.pdf': False}
    queued = {job['file']: job for job in jobs(store) if job.get('release_id') == rid}
    assert set(queued) == {FILE, 'two.pdf'}
    assert queued[FILE]['allow_remaining_issues'] is True and 'release_review' in queued[FILE]
    assert 'allow_remaining_issues' not in queued['two.pdf'] and 'release_review' not in queued['two.pdf']
    assert queued[FILE]['artifact_digest'] == tag(V2) and queued['two.pdf']['artifact_digest'] == tag(w2)
    # The identical request dedupes onto the batch in flight: no second batch, no new decisions.
    again = republish(routes, {'expected_artifacts': {FILE: V2, 'two.pdf': w2}, 'allow_remaining_issues': True})
    assert again['result'] == 'already_publishing'
    assert len([job for job in jobs(store) if job.get('release_id') == rid]) == 2
    assert len(decisions(store, 'release.remaining_issues_authorized')) == 1


def test_per_file_authorization_is_scoped_and_fingerprinted(routes, world):
    """The internal per-file set is intersected with the request, and it is part of the batch
    identity: the same files under a different confirmation set are a different request."""
    store = world.store
    correct_to(store, V2)
    with store._db.cursor() as cur:
        store._db.execute(cur, "INSERT INTO file_records(scan_id,file,engine,status,compliant,corrected_sha256,remediated_at) "
                          "VALUES(%s,'three.pdf','pdf','analysed',0,%s,'t3')", (SID, 'c' * 64))
    body = {'files': [FILE], 'expected_artifacts': {FILE: V2}, 'allow_remaining_issues': False}
    first = routes._publish_release_files(SID, request(OWNER, {'x-sp-token': 'sp'}), dict(body),
                                          remaining_issue_files={FILE, 'three.pdf'})
    assert [row['file'] for row in decisions(store, 'release.remaining_issues_authorized')] == [FILE]
    [payload] = [job for job in jobs(store) if job.get('release_id') == world.release_id]
    assert payload['allow_remaining_issues'] is True
    # Identical request -> the same batch (dedupe).
    again = routes._publish_release_files(SID, request(OWNER, {'x-sp-token': 'sp'}), dict(body),
                                          remaining_issue_files={FILE})
    assert again['batch_id'] == first['batch_id']
    # Same files and digest under a DIFFERENT authorization is a different request: it is not
    # silently folded into the authorized batch (the stage fence refuses it while that runs).
    correct_to(store, V2, compliant=1)
    with pytest.raises(HTTPException) as other:
        routes._publish_release_files(SID, request(OWNER, {'x-sp-token': 'sp'}), dict(body),
                                      remaining_issue_files=set())
    assert other.value.detail['code'] == 'stage_execution_active'
    assert len([job for job in jobs(store) if job.get('release_id') == world.release_id]) == 1


def test_confirmation_list_never_authorizes_an_unticked_file(routes, world):
    """The UI names the files whose remaining-issues box was ticked. A file the server finds needs
    confirmation (e.g. rescored non-compliant at the same digest after the page loaded) but that is
    not in that list is refused — another file's checkbox never authorizes it."""
    store, rid = world.store, world.release_id
    w1, w2 = 'a' * 64, 'b' * 64
    with store._db.cursor() as cur:
        store._db.execute(cur, "INSERT INTO file_records(scan_id,file,engine,status,compliant,corrected_sha256,remediated_at,drive_file_id) "
                          "VALUES(%s,'two.pdf','pdf','analysed',0,%s,'t2','source-two')", (SID, w2))
    store.ensure_release_execution(SID, OWNER, 'sharepoint', 2)
    store.record_release_document(rid, OWNER, dict(file='two.pdf', status='published', artifact_digest=tag(w1),
                                                    published_at=PUBLISHED_AT, published_url='https://example.com/two-v1'))
    correct_to(store, V2)
    before = len(jobs(store))
    error = conflict(routes, {'expected_artifacts': {FILE: V2, 'two.pdf': w2}, 'allow_remaining_issues': True,
                              'remaining_issue_files': [FILE]})
    assert error.status_code == 409 and error.detail['code'] == 'remaining_issues_confirmation_required'
    assert error.detail['files'] == ['two.pdf']
    assert len(jobs(store)) == before
    assert decisions(store, 'release.remaining_issues_authorized') == []
    assert decisions(store, 'release.republish_authorized') == []
    ok = republish(routes, {'expected_artifacts': {FILE: V2, 'two.pdf': w2}, 'allow_remaining_issues': True,
                            'remaining_issue_files': [FILE, 'two.pdf']})
    assert sorted(ok['republished']) == [FILE, 'two.pdf']


def test_a_refusal_older_than_the_current_copy_is_not_reported_as_its_failure(routes, world):
    """last_attempt_failure describes an attempt to publish the CURRENT copy. A refusal recorded
    before that copy was saved is about a different copy and must not be attached to it."""
    store, rid = world.store, world.release_id
    store.record_release_document(rid, OWNER, dict(file=FILE, status='failed', failure_category='not_approved',
                                                    explanation='Only approved corrected copies can be released.'))
    assert len(decisions(store, 'release.publish_attempt_failed')) == 1
    correct_to(store, V2, at='2999-01-01T00:00:00+00:00')
    [row] = routes.get_release_status(SID, request())['publication']['out_of_date']
    assert row['last_attempt_failure'] is None
    correct_to(store, V2, at='2000-01-01T00:00:00+00:00')
    [row] = routes.get_release_status(SID, request())['publication']['out_of_date']
    assert row['last_attempt_failure']['failure_category'] == 'not_approved'

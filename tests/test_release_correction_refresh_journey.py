"""End-to-end journey: a correction saved AFTER publication must never read as current.

The reproduction behind this file (2026-09-18, base 16a6919f): a SharePoint document was
published as V1, its follow-up reports were built and delivered for V1, and then an approved
value was saved as V2. Every surface the owner reads kept saying "done":

  * GET /release/reports answered status=completed with no regeneration signal, because the
    report fingerprint covers the release rows, not the current corrected artifact;
  * GET /release carried the V1 digest and the V2 digest side by side but no verdict;
  * release_executions.status stayed 'running' forever while the projection said 'completed'.

This drives the REAL routes (publish_files, get_release_status, the reports routes and the
republish route), the REAL job queue on SQLite (JobWorker.run_once claims and completes the
publish_file / publish_release_reports jobs), and the real handlers. Only the transport is
faked: Blob (the saved corrected bytes) and Microsoft Graph (an in-memory drive that stores
bytes and verifies them by SHA-256 exactly as Graph's hashes would).

API contract under test: /tmp/acp-release-refresh-status.md "API contract (v1)".
"""
import hashlib
import inspect
import json
import re
import typing
from types import SimpleNamespace
from urllib.parse import unquote

import pytest
from fastapi import HTTPException, Response
from pydantic import BaseModel

OWNER = 'journey-owner@example.com'
OTHER = 'someone-else@example.com'
SID = 'journey-scan'
FILE = 'policy.pdf'
V1, V2, V3 = b'corrected copy V1', b'corrected copy V2', b'corrected copy V3'


def sha(data):
    return hashlib.sha256(data).hexdigest()


def tag(data):
    return 'sha256:' + sha(data)


def bare(digest):
    """Digests appear both tagged ("sha256:<hex>") and bare; compare the hex."""
    return str(digest or '').removeprefix('sha256:')


# ── fake transport ───────────────────────────────────────────────────────────────────────────

class Graph:
    """An in-memory SharePoint drive: items keyed by id, verified by content SHA-256."""

    def __init__(self):
        self.items = {}

    def child(self, token, drive, parent, name):
        return next(({'id': i, 'webUrl': item['url']} for i, item in self.items.items()
                     if item['parent'] == parent and item['name'] == name), None)

    def matches(self, token, drive, item_id, digest):
        return item_id in self.items and sha(self.items[item_id]['bytes']) == digest

    def ensure_folder(self, token, drive, parent, segment):
        return f'{parent}/{segment}'

    def write(self, token, *, put_url, session_url, content, content_type,
              conflict_behavior, force_session=False):
        parent, name = re.search(r'/items/([^:]+):/([^:]+):/content$', put_url).groups()
        name = unquote(name)
        assert self.child(token, None, parent, name) is None, 'conflict_behavior=fail'
        item_id = f'item-{len(self.items) + 1}'
        self.items[item_id] = {'parent': parent, 'name': name, 'bytes': bytes(content),
                               'url': f'https://sp.example/{item_id}'}
        return {'id': item_id, 'webUrl': self.items[item_id]['url']}

    def delivered(self):
        """The corrected-document bytes that reached the destination (reports excluded)."""
        return [item['bytes'] for item in self.items.values() if item['name'] == FILE
                or item['name'].startswith(FILE.rsplit('.', 1)[0] + ' (')]


def _reports_for(store, sid, owner, release_id):
    """Stand-in for release_reports.build_release_reports: one changes report per published
    document naming the EXACT artifact digest it describes, plus a scan summary. PDF bytes, so
    byte-identity of the frozen bundle is meaningful (base64 storage round trip)."""
    release = store.release_status(release_id, owner)
    assets = [dict(name='scan-summary.pdf', content=b'%PDF-1.7\nsummary', content_type='application/pdf',
                   report_kind='scan_summary')]
    for document in release['documents']:
        if document['status'] == 'published':
            assets.append(dict(name=f"changes-{document['file']}.pdf",
                               content=b'%PDF-1.7\nchanges for ' + str(document.get('artifact_digest')).encode(),
                               content_type='application/pdf', report_kind='changes',
                               file=document['file'], artifact_digest=document.get('artifact_digest')))
    return assets


# ── fixture ──────────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def journey(isolated_store, monkeypatch):
    import blob
    import core
    import handlers  # noqa: F401  registers the publish_file / publish_release_reports handlers
    import publish
    import release_candidate_assessment
    import release_continuation
    import release_reports
    import scanner

    store = isolated_store
    graph = Graph()
    state = {'bytes': V1}
    tokens = {}
    monkeypatch.setattr(core, 'store', store)
    monkeypatch.setattr(core, 'register_scan_tokens',
                        lambda sid, **kw: tokens.update({k: v for k, v in kw.items() if k in ('sp', 'drive') and v}))
    monkeypatch.setattr(core, 'get_scan_tokens', lambda sid: dict(tokens))
    monkeypatch.setattr(release_continuation, 'require_grants', lambda *a, **k: None)
    # The saved corrected copy (Blob). remediated_content_digest and the SharePoint publisher
    # both read it, so a correction is visible to every guard at once — as in production.
    monkeypatch.setattr(blob, 'download_remediated', lambda *a: state['bytes'])
    monkeypatch.setattr(publish._blob, 'download_remediated', lambda *a: state['bytes'])
    # Microsoft Graph, below the real publish.archive_copy_publish_sharepoint / report _upload.
    monkeypatch.setattr(publish, '_sp_child', graph.child)
    monkeypatch.setattr(publish, '_sp_content_matches', graph.matches)
    monkeypatch.setattr(publish, '_sp_ensure_folder', graph.ensure_folder)
    monkeypatch.setattr(scanner, '_sp_write', graph.write)
    monkeypatch.setattr(publish, 'ensure_sharepoint_release_folder',
                        lambda token, drive, release_id, name, **kw: {
                            'id': 'release-root', 'name': name, 'url': 'https://sp.example/release-root'})
    # Sentinel bytes are not a PDF; the saved-byte scanner gate is proven in
    # test_release_candidate_assessment. Everything else in the release gate runs for real.
    monkeypatch.setattr(release_candidate_assessment, 'assess_candidate',
                        lambda *a, **k: {'fixture_assessment': True})
    monkeypatch.setattr(release_reports, 'build_release_reports', _reports_for)

    # A SharePoint scan whose one document still has a remaining issue after remediation, so
    # every release of it needs the explicit remaining-issues authorization bound to a digest.
    store.init_scan_run(SID, 'sharepoint', 1, '2026-09-18T00:00:00Z', 'rubric', 'hash',
                        owner=OWNER, status='completed')
    store.save_file_result(SID, {
        'file': FILE, 'engine': 'pdf', 'status': 'analysed', 'score': 60, 'compliant': 0,
        'skipped_rules': 0, 'errors': [],
        'issues': [{'ruleId': 'PDF-ALT-001', 'wcag': '1.1.1', 'severity': 'CRITICAL'}],
    }, '2026-09-18T00:00:00Z')

    def save_version(data):
        """What apply_approved_values does on success: new bytes, new digest, new remediated_at."""
        state['bytes'] = data
        return store.record_remediation(SID, FILE, blob_url='blob://corrected',
                                        corrected_sha256=sha(data))

    save_version(V1)
    return SimpleNamespace(store=store, graph=graph, state=state, save_version=save_version)


# ── helpers: requests, routes, the queue ─────────────────────────────────────────────────────

def request(owner=OWNER, headers=None):
    return SimpleNamespace(state=SimpleNamespace(user_email=owner),
                           headers={'x-sp-token': 'sp-fixture'} if headers is None else headers)


def get_release(owner=OWNER):
    from routes import scans
    return scans.get_release_status(SID, request(owner))


def get_reports(owner=OWNER):
    from routes import scans
    return scans.get_release_reports(SID, request(owner), Response())


def republish(body, owner=OWNER, headers=None):
    """Call POST /scans/{sid}/release/republish through whatever endpoint registers it.

    Resolved from the router rather than by name so this file does not guess the function
    name; the body is passed as a dict or as the endpoint's pydantic model."""
    from routes import scans
    route = next((r for r in scans.router.routes
                  if getattr(r, 'path', None) == '/scans/{sid}/release/republish'
                  and 'POST' in getattr(r, 'methods', ())), None)
    assert route is not None, 'POST /scans/{sid}/release/republish is not registered'
    kwargs = {}
    # routes/scans.py uses `from __future__ import annotations`: annotations are strings until resolved.
    hints = typing.get_type_hints(route.endpoint)
    for name in inspect.signature(route.endpoint).parameters:
        annotation = hints.get(name)
        if name == 'sid':
            kwargs[name] = SID
        elif name == 'request':
            kwargs[name] = request(owner, headers)
        elif name == 'response':
            kwargs[name] = Response()
        elif isinstance(annotation, type) and issubclass(annotation, BaseModel):
            kwargs[name] = annotation(**body)
        else:
            kwargs[name] = body
    return route.endpoint(**kwargs)


def http_code(exc):
    detail = exc.value.detail
    return detail.get('code') if isinstance(detail, dict) else detail


def run_job(store, job_type):
    """Claim and run ONE job through the real worker loop; return the row as it now stands."""
    from worker import JobWorker
    worker = JobWorker(store, worker_id=f'test-{job_type}', job_types=[job_type])
    claimed = []
    original = store.claim_job
    store.claim_job = lambda *a, **k: claimed.append(original(*a, **k)) or claimed[-1]
    try:
        assert worker.run_once(), f'no {job_type} job was queued'
    finally:
        store.claim_job = original
    return store.get_job(claimed[0]['id'])


def jobs(store, job_type):
    with store._db.cursor() as cur:
        store._db.execute(cur, 'SELECT id,status,payload FROM jobs WHERE type=%s ORDER BY created_at,id', (job_type,))
        rows = store._db.fetchall(cur)
    return [{**row, 'payload': json.loads(row['payload']) if isinstance(row['payload'], str) else row['payload']}
            for row in rows]


def durable_release_status(store):
    with store._db.cursor() as cur:
        store._db.execute(cur, 'SELECT status FROM release_executions WHERE scan_id=%s AND owner_email=%s', (SID, OWNER))
        return store._db.fetchone(cur)['status']


def document(release):
    return next(d for d in release['documents'] if d['file'] == FILE)


def publish_v1(j):
    """Step (a) as production does it: explicit remaining-issue authorization bound to V1,
    one queued publish_file job, the worker delivers, and the handler queues the reports."""
    from routes import scans
    response = scans.publish_files(SID, request(), {
        'files': [FILE], 'allow_remaining_issues': True, 'expected_artifacts': {FILE: sha(V1)}})
    assert response['queued'] == 1, response
    job = run_job(j.store, 'publish_file')
    assert job['status'] == 'done', job
    reports = get_reports()
    assert reports['status'] == 'queued', reports
    delivered = run_job(j.store, 'publish_release_reports')
    assert delivered['status'] == 'done', delivered
    return get_reports()


# ── the journey ──────────────────────────────────────────────────────────────────────────────

def test_correction_after_publication_is_out_of_date_until_one_action_republishes_it(journey):
    j = journey
    store = j.store

    # (a) V1 is published and its reports delivered. The release is consistent: projection
    # completed, and the DURABLE column agrees (it used to stay 'running' forever).
    v1_reports = publish_v1(j)
    assert j.graph.delivered() == [V1]
    release = get_release()
    assert release['status'] == 'completed'
    assert durable_release_status(store) == 'completed', (
        'release_executions.status must settle with the documents, not stay at its insert value')
    assert document(release).get('publication_state') == 'current', document(release)
    assert (release.get('publication') or {}).get('state') == 'current', release.get('publication')
    assert v1_reports['status'] == 'completed'
    assert v1_reports.get('currency') == 'current', v1_reports
    v1_bundle = v1_reports['bundle_id']
    v1_assets = {i: scans_download(v1_bundle, i) for i in range(len(v1_reports['reports']))}

    # (b) An approved value is saved: the corrected copy is now V2.
    j.save_version(V2)

    # (c) Every surface must say the delivered copy is out of date, with exact digests.
    release = get_release()
    doc = document(release)
    assert doc.get('publication_state') == 'out_of_date', doc
    assert doc.get('published_artifact_digest') == tag(V1), doc
    assert doc.get('current_artifact_digest') == tag(V2), doc
    publication = release.get('publication') or {}
    assert publication.get('state') == 'out_of_date', publication
    assert publication.get('can_republish') is True, publication
    assert publication.get('republish_blocked_reason') is None, publication
    stale = publication.get('out_of_date') or [{}]
    assert [s.get('file') for s in stale] == [FILE], stale
    assert stale[0].get('published_artifact_digest') == tag(V1)
    assert stale[0].get('current_artifact_digest') == tag(V2)
    assert stale[0].get('requires_remaining_issue_confirmation') is True, stale
    reports = get_reports()
    assert reports.get('currency') == 'out_of_date', (
        'reports for V1 read as current after V2 was saved', reports)
    assert reports.get('currency_reason') == 'copy_changed_after_publication', reports
    assert reports.get('out_of_date_files') == [{
        'file': FILE, 'reported_artifact_digest': tag(V1), 'current_artifact_digest': tag(V2)}], reports
    assert reports['regeneration_blocked'] and not reports['can_regenerate'], reports
    with pytest.raises(HTTPException) as refused:
        from routes import scans
        scans.retry_release_reports(SID, request(), Response())
    assert refused.value.status_code == 409  # never rebuild V1 reports from V2 repair records

    # (d) Republish is explicit and bound to the exact artifact.
    before = len(jobs(store, 'publish_file'))
    with pytest.raises(HTTPException) as missing_confirmation:
        republish({'expected_artifacts': {FILE: sha(V2)}, 'allow_remaining_issues': False})
    assert missing_confirmation.value.status_code == 409
    assert http_code(missing_confirmation) == 'remaining_issues_confirmation_required'
    with pytest.raises(HTTPException) as stale_digest:
        republish({'expected_artifacts': {FILE: sha(V1)}, 'allow_remaining_issues': True})
    assert stale_digest.value.status_code == 409
    assert http_code(stale_digest) == 'artifact_changed'
    assert len(jobs(store, 'publish_file')) == before, 'a refused republish must not queue work'

    accepted = republish({'expected_artifacts': {FILE: sha(V2)}, 'allow_remaining_issues': True})
    queued = [row for row in jobs(store, 'publish_file') if row['status'] == 'queued']
    assert [row['payload'].get('artifact_digest') for row in queued] == [tag(V2)], (accepted, queued)
    audit = [row for row in store.list_decisions(SID) if row['action'] == 'release.republish_authorized']
    assert len(audit) == 1, audit
    detail = json.loads(audit[0]['detail'])
    assert audit[0]['actor'] == OWNER and audit[0]['file'] == FILE
    assert bare(detail.get('previous_artifact_digest')) == sha(V1), detail
    assert bare(detail.get('artifact_digest')) == sha(V2), detail
    assert detail.get('release_id') == release['release_id'], detail
    assert detail.get('allow_remaining_issues') is True, detail
    assert detail.get('previous_published_at'), detail
    # In flight: the release says so. Resubmitting the SAME digest (a double click, a second tab)
    # is an idempotent 200 that joins the queued job rather than doubling it, and writes no second
    # authorization. (A DIFFERENT digest while this one publishes is the 409 — see the race test.)
    assert (get_release().get('publication') or {}).get('state') == 'publishing'
    in_flight = len(jobs(store, 'publish_file'))
    republish({'expected_artifacts': {FILE: sha(V2)}, 'allow_remaining_issues': True})
    assert len(jobs(store, 'publish_file')) == in_flight, 'same-digest resubmission must not queue again'
    assert len([row for row in store.list_decisions(SID)
                if row['action'] == 'release.republish_authorized']) == 1
    assert get_reports().get('currency') != 'current'

    job = run_job(store, 'publish_file')
    assert job['status'] == 'done', job
    assert j.graph.delivered() == [V1, V2], 'archive-copy: V1 stays, V2 is written beside it'
    assert document(get_release())['artifact_digest'] == tag(V2)
    with store._db.cursor() as cur:
        store._db.execute(cur, "SELECT content_digest FROM side_effect_receipts "
                               "WHERE effect_type='sharepoint.publish' AND status='completed' ORDER BY created_at")
        assert [r['content_digest'] for r in store._db.fetchall(cur)] == [sha(V1), sha(V2)]

    # The handler queued V2 reports on settle; they are a NEW bundle, V1's is untouched.
    v2_reports = get_reports()
    assert v2_reports['bundle_id'] != v1_bundle and v2_reports['status'] == 'queued', v2_reports
    assert run_job(store, 'publish_release_reports')['status'] == 'done'
    v2_reports = get_reports()
    assert v2_reports['status'] == 'completed'
    assert v2_reports.get('currency') == 'current', v2_reports
    assert [r['artifact_digest'] for r in v2_reports['reports'] if r['report_kind'] == 'changes'] == [tag(V2)]
    release = get_release()
    assert (release.get('publication') or {}).get('state') == 'current', release.get('publication')
    assert document(release).get('publication_state') == 'current'
    assert (release.get('publication') or {}).get('can_republish') is False
    assert release['status'] == 'completed'
    assert durable_release_status(store) == 'completed'
    assert {i: scans_download(v1_bundle, i) for i in v1_assets} == v1_assets, (
        'the V1 report bundle is immutable history')

    # (e) Repeating the same republish is idempotent: already current, no new work.
    before = len(jobs(store, 'publish_file'))
    again = republish({'expected_artifacts': {FILE: sha(V2)}, 'allow_remaining_issues': True})
    assert again.get('already_current') is True, again
    assert len(jobs(store, 'publish_file')) == before
    assert j.graph.delivered() == [V1, V2]


def scans_download(bundle_id, index, owner=OWNER):
    from routes import scans
    return scans.download_release_report(SID, bundle_id, index, request(owner)).body


# ── races ────────────────────────────────────────────────────────────────────────────────────

def test_correction_during_republish_is_not_published_and_not_claimed_current(journey):
    """V3 is saved after the republish POST (authorized for V2) but before the worker runs."""
    j = journey
    store = j.store
    publish_v1(j)
    j.save_version(V2)
    republish({'expected_artifacts': {FILE: sha(V2)}, 'allow_remaining_issues': True})
    j.save_version(V3)
    # V3 is the saved copy now, but V2's job is still queued: a request for a DIFFERENT digest
    # while one is publishing is refused (never two concurrent publications of one document).
    queued = len(jobs(store, 'publish_file'))
    with pytest.raises(HTTPException) as busy:
        republish({'expected_artifacts': {FILE: sha(V3)}, 'allow_remaining_issues': True})
    assert busy.value.status_code == 409 and http_code(busy) == 'republish_blocked'
    assert len(jobs(store, 'publish_file')) == queued

    job = run_job(store, 'publish_file')
    # Neither V3 (never authorized) nor V2 (no longer the saved copy) may reach the destination,
    # and the queue must not record the attempt as a successful delivery.
    assert j.graph.delivered() == [V1]
    assert job['status'] != 'done', job
    release = get_release()
    doc = document(release)
    publication = release.get('publication') or {}
    assert publication.get('state') == 'out_of_date', publication
    assert doc.get('publication_state') == 'out_of_date', doc
    assert doc.get('published_artifact_digest') == tag(V1), 'V1 is still what the destination holds'
    assert doc.get('current_artifact_digest') == tag(V3), doc
    assert publication.get('can_republish') is True, publication
    reports = get_reports()
    assert reports.get('currency') == 'out_of_date', reports
    assert (reports.get('out_of_date_files') or [{}])[0].get('current_artifact_digest') == tag(V3), reports
    # The stale V2 authorization cannot be replayed against V3.
    with pytest.raises(HTTPException) as stale:
        republish({'expected_artifacts': {FILE: sha(V2)}, 'allow_remaining_issues': True})
    assert http_code(stale) == 'artifact_changed'


def test_correction_between_publish_and_report_queueing_keeps_the_publish_and_builds_no_reports(journey, monkeypatch):
    """The copy changes after the provider write is recorded but before the handler queues
    reports. Today queue_if_release_settled raises REPORT_COPY_CHANGED inside the publish_file
    job, which fails a publication that actually succeeded and sends it round the retry loop."""
    import release_reports
    from routes import scans
    j = journey
    store = j.store
    built = []
    monkeypatch.setattr(release_reports, 'build_release_reports', lambda *a: built.append(a) or _reports_for(*a))
    original = store.record_release_document

    def record_then_correct(release_id, owner, result):
        original(release_id, owner, result)
        if result.get('status') == 'published' and j.state['bytes'] == V1:
            j.save_version(V2)
    monkeypatch.setattr(store, 'record_release_document', record_then_correct)

    scans.publish_files(SID, request(), {
        'files': [FILE], 'allow_remaining_issues': True, 'expected_artifacts': {FILE: sha(V1)}})
    job = run_job(store, 'publish_file')
    assert job['status'] == 'done', ('the publication succeeded and must stay successful', job)
    assert j.graph.delivered() == [V1]
    assert built == [], 'reports must not be rendered from V2 repair records for V1 bytes'
    assert jobs(store, 'publish_release_reports') == []
    release = get_release()
    assert document(release)['status'] == 'published'
    assert document(release)['artifact_digest'] == tag(V1)
    assert (release.get('publication') or {}).get('state') == 'out_of_date', release.get('publication')
    assert get_reports().get('currency') != 'current'
    assert durable_release_status(store) == release['status']


# ── isolation, legacy identity, gates ────────────────────────────────────────────────────────

def test_another_owner_cannot_read_or_republish(journey):
    j = journey
    publish_v1(j)
    j.save_version(V2)
    before = len(jobs(j.store, 'publish_file'))
    for call in (lambda: get_release(OTHER), lambda: get_reports(OTHER),
                 lambda: republish({'expected_artifacts': {FILE: sha(V2)}, 'allow_remaining_issues': True}, OTHER)):
        with pytest.raises(HTTPException) as refused:
            call()
        assert refused.value.status_code == 404
    assert len(jobs(j.store, 'publish_file')) == before
    assert not [r for r in j.store.list_decisions(SID) if r['action'] == 'release.republish_authorized']
    assert (get_release().get('publication') or {}).get('state') == 'out_of_date'


def test_legacy_receipt_without_digest_is_identity_unknown_never_current(journey):
    """A pre-digest receipt names no artifact: nobody can say whether it is current."""
    import release_report_delivery as delivery
    j = journey
    store = j.store
    release = store.ensure_release_execution(SID, OWNER, 'sharepoint', 1)
    store.record_release_root(release['id'], OWNER, 'sharepoint', 'graph:me', 'release-root',
                              'Release', 'https://sp.example/release-root')
    store.record_release_document(release['id'], OWNER, dict(
        file=FILE, status='published', published_at='2026-08-01T00:00:00Z',
        published_url='https://sp.example/legacy', released_document_id='legacy-item'))
    bundle = delivery.queue_release_reports(store, SID, OWNER, release['id'])
    delivery.process_release_reports(store, bundle['bundle_id'], OWNER)

    status = get_release()
    doc = document(status)
    publication = status.get('publication') or {}
    assert doc.get('publication_state') == 'identity_unknown', doc
    assert doc.get('published_artifact_digest') is None and doc.get('current_artifact_digest') == tag(V1), doc
    assert publication.get('state') == 'identity_unknown', publication
    assert publication.get('identity_unknown') == [FILE], publication
    # The existing publish path refuses an unresolved delivery ('delivery_version_unresolved');
    # republish must not become a way around that.
    assert publication.get('can_republish') is False, publication
    reports = get_reports()
    assert reports.get('currency') not in (None, 'current'), reports
    before = len(jobs(store, 'publish_file'))
    with pytest.raises(HTTPException) as refused:
        republish({'expected_artifacts': {FILE: sha(V1)}, 'allow_remaining_issues': True})
    assert refused.value.status_code == 409 and http_code(refused) == 'republish_blocked'
    assert len(jobs(store, 'publish_file')) == before
    assert not [r for r in store.list_decisions(SID) if r['action'] == 'release.republish_authorized']


def test_republish_is_blocked_while_approved_values_are_unapplied(journey):
    """An approved reviewer value not yet written into the copy: the saved V2 is not what the
    reviewer approved, so republishing it would publish a known-incomplete artifact."""
    from hitl_viewed import approve_bound
    j = journey
    store = j.store
    publish_v1(j)
    j.save_version(V2)
    item = store.enqueue_proposals(SID, FILE, '1.1.1', [
        {'locator': 'page1#img1', 'before': '(no alt text)', 'proposed_value': 'A chart of totals',
         'rationale': 'r', 'source': 'reviewer'}], rule_name='Non-text Content')
    approve_bound(store, item, [])
    assert store.count_unapplied_approved_values(SID, FILE) == 1

    publication = get_release().get('publication') or {}
    assert publication.get('state') == 'out_of_date', publication
    assert publication.get('can_republish') is False, publication
    assert publication.get('republish_blocked_reason'), publication
    before = len(jobs(store, 'publish_file'))
    with pytest.raises(HTTPException) as blocked:
        republish({'expected_artifacts': {FILE: sha(V2)}, 'allow_remaining_issues': True})
    assert blocked.value.status_code == 409 and http_code(blocked) == 'republish_blocked'
    assert len(jobs(store, 'publish_file')) == before
    assert j.graph.delivered() == [V1]


# ── adversarial review of aea8e6c6 (Agent C, p4) ─────────────────────────────────────────────

def test_refused_attempt_on_a_current_copy_does_not_regenerate_or_stale_its_reports(journey):
    """V1 is published, current, and its reports are delivered. A stale tab then sends the plain
    Publish request without the remaining-issues confirmation. The request is (rightly) refused
    per document with 'not_approved', and record_release_document now keeps the delivered V1
    receipt as 'published' — but it stamps that refusal's failure_category/explanation onto the
    row. Those columns are part of release_report_delivery._fingerprint, so the unchanged V1
    reports are re-identified: a refused request, which delivered nothing, must not mark the
    delivered reports out of date, nor build and deliver a second report bundle."""
    from routes import scans
    j = journey
    store = j.store
    v1_reports = publish_v1(j)
    assert v1_reports.get('currency') == 'current'
    bundles_before = len(jobs(store, 'publish_release_reports'))

    refused = scans.publish_files(SID, request(), {'files': [FILE]})
    assert [row['status'] for row in refused['published']] == ['failed']   # the refusal itself is right
    release = get_release()
    assert document(release)['status'] == 'published'                    # and V1's receipt survives
    assert document(release).get('publication_state') == 'current'

    reports = get_reports()
    assert reports['bundle_id'] == v1_reports['bundle_id'], (
        'a refused request that delivered nothing built a new report bundle', reports['bundle_id'])
    assert reports.get('currency') == 'current', reports
    assert len(jobs(store, 'publish_release_reports')) == bundles_before, 'no second report delivery'


def test_republish_refused_before_any_work_leaves_no_authorization_in_the_audit_log(journey):
    """republish_release logs 'release.republish_authorized' (naming V1 as superseded) BEFORE it
    delegates to publish_files — which can still refuse: here the SharePoint write grant is missing
    (403). The immutable decision log then says V2 was authorized to replace V1 when nothing was
    queued; a retry with a token logs a second one. The audit must not record a refused request."""
    j = journey
    store = j.store
    publish_v1(j)
    j.save_version(V2)
    with pytest.raises(HTTPException) as refused:
        republish({'expected_artifacts': {FILE: sha(V2)}, 'allow_remaining_issues': True}, headers={})
    assert refused.value.status_code == 403
    assert [row for row in jobs(store, 'publish_file') if row['status'] == 'queued'] == []
    audit = [row['action'] for row in store.list_decisions(SID)
             if row['action'] in ('release.republish_authorized', 'release.remaining_issues_authorized')
             and row['file'] == FILE]
    # Exactly the one remaining-issues authorization from the original V1 publish.
    assert audit == ['release.remaining_issues_authorized'], audit


def test_unknown_current_copy_is_not_current_in_reports_either(journey):
    """The receipt names V1 exactly, but the CURRENT copy has no recorded digest (a legacy record:
    the local/sync Release path attests Blob bytes and does not require corrected_sha256). GET
    release rightly says identity_unknown. release_report_delivery._copy_currency skips any file
    whose current digest is missing (`if not current: continue`), so the reports for the very same
    file answer currency 'current'. Two surfaces, one file, opposite answers — and the reports'
    one is the overstatement."""
    j = journey
    store = j.store
    publish_v1(j)
    with store._db.cursor() as cur:
        store._db.execute(cur, 'UPDATE file_records SET corrected_sha256=NULL WHERE scan_id=%s AND file=%s', (SID, FILE))
    doc = document(get_release())
    assert doc.get('publication_state') == 'identity_unknown', doc
    reports = get_reports()
    assert reports.get('currency') != 'current', (
        'release says identity_unknown for this file, reports say current', reports.get('currency'), reports.get('currency_reason'))


def test_failed_republish_settles_as_out_of_date_with_the_failure_readable(journey, monkeypatch):
    """GUARD (passes today): the provider refuses V2 on the final attempt. The V1 receipt stays
    'published' (V1 IS still at the destination), the verdict is out_of_date against V2, nothing is
    left 'publishing', and the attempt's failure is readable on the document. This pins the backend
    half of a UI defect: Publish.jsx afterRepublish's `settled` predicate treats exactly this row
    (status 'published', publication_state 'out_of_date', same published digest) as NOT settled, so
    the page shows 'Publishing updated copy…' for 6 minutes and then says the copy 'is still
    publishing safely in the background' — after it failed."""
    import scanner
    j = journey
    store = j.store
    publish_v1(j)
    j.save_version(V2)
    republish({'expected_artifacts': {FILE: sha(V2)}, 'allow_remaining_issues': True})

    def refuse(token, **kwargs):
        raise IOError('Graph 500 while writing the corrected copy')
    monkeypatch.setattr(scanner, '_sp_write', refuse)
    with store._db.cursor() as cur:
        store._db.execute(cur, "UPDATE jobs SET max_attempts=1 WHERE type='publish_file' AND status='queued'")
    job = run_job(store, 'publish_file')
    assert job['status'] == 'dead', job
    assert j.graph.delivered() == [V1]

    release = get_release()
    doc = document(release)
    assert doc['status'] == 'published' and doc['artifact_digest'] == tag(V1)
    assert doc.get('publication_state') == 'out_of_date'
    assert doc.get('published_artifact_digest') == tag(V1) and doc.get('current_artifact_digest') == tag(V2)
    # The delivered V1 row is restored EXACTLY (its columns are the report fingerprint); the failed
    # attempt is read from the immutable decision log and attached to the out-of-date entry.
    assert not doc.get('failure_category'), doc
    publication = release['publication']
    assert publication['state'] == 'out_of_date' and publication['can_republish'] is True, publication
    failure = publication['out_of_date'][0]['last_attempt_failure']
    assert failure and failure['failure_category'] == 'provider_write_failed' and failure['explanation'], failure
    assert 'Graph 500' not in failure['explanation'], 'provider exception text must not cross the API'
    attempts = [r for r in store.list_decisions(SID) if r['action'] == 'release.publish_attempt_failed']
    assert len(attempts) == 1 and attempts[0]['file'] == FILE
    assert get_reports().get('currency') == 'out_of_date'

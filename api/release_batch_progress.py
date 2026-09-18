"""Cumulative authorized delivery presentation, separate from an incremental job ledger."""
import json


def read(store, execution):
    try:
        return _read(store, execution)
    except Exception:
        # Optional presentation must never weaken or replace canonical accounting.
        return {'available': False, 'scope': 'unknown'}


def _read(store, execution):
    from automatic_release_store import get
    owner, scan = execution['owner_email'], execution['scan_id']
    with store._db.cursor() as cur:
        store._db.execute(cur, 'SELECT COALESCE(j.payload,o.payload) AS payload '
            'FROM stage_work_items w LEFT JOIN jobs j ON j.id=w.job_id '
            'LEFT JOIN stage_outbox o ON o.work_item_id=w.work_item_id '
            'WHERE w.execution_id=%s', (execution['execution_id'],))
        jobs = store._db.fetchall(cur)
    identities = set()
    if not jobs:
        return {'available': False, 'scope': 'unknown'}
    for job in jobs:
        payload = json.loads(job['payload']) if isinstance(job['payload'], str) else job['payload']
        identity = payload.get('automatic_release_id')
        if payload.get('owner') != owner or payload.get('scan_id') != scan:
            return {'available': False, 'scope': 'unknown'}
        identities.add(identity)
    if identities == {None}:
        return {'available': False, 'scope': 'manual'}
    if len(identities) != 1 or not next(iter(identities)):
        return {'available': False, 'scope': 'unknown'}
    row = get(store, next(iter(identities)), owner)
    if not row or row['scan_id'] != scan:
        return {'available': False, 'scope': 'automatic'}
    parent = store.get_stage_execution(row['run_id'], owner=owner)
    if (not parent or parent.get('stage') != 'remediate' or parent.get('scan_id') != scan
            or not parent.get('is_current')
            or parent.get('workflow_id') != execution.get('workflow_id')
            or parent.get('workflow_revision') != execution.get('workflow_revision')):
        return {'available': False, 'scope': 'automatic'}
    return _project(store, row)


def read_authorization(store, row):
    """The same saved-plan snapshot for the authorization observer and stage header."""
    try:
        parent = store.get_stage_execution(row['run_id'], owner=row['owner_email'])
        if not parent or parent.get('stage') != 'remediate' or not parent.get('is_current') or parent.get('scan_id') != row['scan_id']:
            return {'available': False, 'scope': 'automatic'}
        return _project(store, row)
    except Exception:
        return {'available': False, 'scope': 'automatic'}


def _project(store, row):
    from release_artifacts import artifact_tag
    owner, scan = row['owner_email'], row['scan_id']
    files = row['intent'].get('files')
    if isinstance(files, dict):
        files = list(files)  # Current authorizations freeze per-file source identities.
    if not isinstance(files, list) or not files or any(not isinstance(f, str) or not f for f in files):
        return {'available': False, 'scope': 'automatic'}
    if len(set(files)) != len(files):
        return {'available': False, 'scope': 'automatic'}
    release = store.release_for_scan(scan, owner) or {}
    destination = row['intent'].get('destination')
    provider = destination.get('provider') if isinstance(destination, dict) else None
    if provider not in {'drive', 'sharepoint', 'local'}:
        return {'available': False, 'scope': 'automatic', 'reason': 'destination_identity_unavailable'}
    destination_matches = (provider in {'drive', 'sharepoint', 'local'}
                           and release.get('source') == provider
                           and release.get('parent_folder_id') == row['intent'].get('release_parent_id')
                           and release.get('folder_name') == row['intent'].get('release_folder_name'))
    receipts = {r['file']: r for r in release.get('documents', [])} if destination_matches else {}
    entries = row['progress'].get('files', {})
    current_records = store.get_file_records(scan, owner=owner, files=files)
    delivered = 0
    buckets = dict(waiting=0, processing=0, published=0, failed=0, skipped=0, unclassified=0)
    membership = {}
    for file in files:
        digest = entries.get(file, {}).get('artifact_digest')
        receipt = receipts.get(file, {})
        # The continuation freezes bare corrected_sha256 values; durable provider
        # receipts use the tagged wire representation. Accept old tagged progress
        # too, without accepting malformed or partial identities.
        raw_digest = digest[7:] if isinstance(digest, str) and digest.startswith('sha256:') else digest
        current = current_records.get(file, {}).get('corrected_sha256')
        # Delivered means the CURRENT corrected copy's exact receipt is at this destination. The
        # plan froze the digest it admitted; a correction saved after publication and then
        # delivered by the owner's explicit, digest-bound republish (/release/republish) is the
        # current copy of an authorized document and counts. A receipt for any other bytes never
        # does, so an out-of-date delivery reads 0 until the current copy is actually delivered.
        exact = lambda value: (isinstance(value, str) and len(value) == 64
                               and all(c in '0123456789abcdef' for c in value))
        if (exact(current) and receipt.get('status') == 'published'
                and receipt.get('artifact_digest') == artifact_tag(current)
                and (current == raw_digest or exact(raw_digest))):
            delivered += 1
            category = 'published'
        else:
            state = entries.get(file, {}).get('state')
            category = {'waiting': 'waiting', 'publishing': 'processing',
                        'blocked': 'failed', 'failed': 'failed', 'stopped': 'failed',
                        'skipped': 'skipped'}.get(state, 'unclassified')
            if row['status'] in {'stopped', 'failed'} and category in {'waiting', 'processing'}:
                category = 'failed'
        buckets[category] += 1
        membership[file] = category
    status = row['status']
    if status == 'completed' and row['progress'].get('_package_job_id'):
        package = store.get_job(row['progress']['_package_job_id']) or {}
        if package.get('status') != 'done':
            status = 'failed' if package.get('status') in {'dead', 'cancelled'} else 'publishing'
    return {'available': True, 'authorization_id': row['id'], 'run_id': row['run_id'],
            'total': len(files), 'delivered': delivered, 'remaining': len(files) - delivered,
            'status': status, 'revision': row['revision'], 'scope_id': row['id'],
            'buckets': buckets, 'file_membership': membership}

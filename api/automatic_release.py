"""Separate run-scoped release permission; never approves or edits a document."""
from datetime import datetime, timedelta, timezone
import json
import re

import automatic_release_store as persistence
from release_continuation import record_identity, require_grants, request_for
from release_artifacts import ReleaseArtifactError, artifact_tag, require_current_record, require_current_source

ACTIVE = {'active', 'waiting', 'blocked'}
MAX_FILES = 500
MAX_DISPATCH_PER_TICK = 2
STALL_AFTER_SECONDS = 600
STALLED_CHECK_SECONDS = 300


class DeliveryAlreadyAdmitted(ValueError):
    pass


class DeliveryCapacityFull(ValueError):
    pass


class FileRemediationFinishedWithoutCopy(ValueError):
    """This authorized file settled without a publishable corrected artifact."""
    pass


class DriveReconnectRequired(ValueError):
    pass


class DeliveryPreflightBlocked(ValueError):
    pass


def current_run(store, sid, owner):
    with store._db.cursor() as cur:
        store._db.execute(cur, "SELECT * FROM stage_executions WHERE scan_id=%s AND owner_email=%s AND stage='remediate' AND is_current=1 ORDER BY created_at DESC LIMIT 1", (sid, owner))
        return store._db.fetchone(cur)


def run_files(store, run):
    with store._db.cursor() as cur:
        store._db.execute(cur, 'SELECT w.input_id,j.status FROM stage_work_items w JOIN jobs j ON j.id=w.job_id WHERE w.execution_id=%s', (run,))
        return {r['input_id']: r['status'] for r in store._db.fetchall(cur)}


def selection(store, sid, owner, files, run_id=None):
    from assessment_policy import selected_documents
    scan = store.get_scan(sid, owner=owner)
    if not scan:
        raise ValueError('Scan not found')
    run = current_run(store, sid, owner)
    if not run or (run_id is not None and run['execution_id'] != run_id):
        raise ValueError('Start Remediate before enabling automatic release for its accepted run.')
    if run.get('cancel_requested_at') or run['state'] in {'cancelled', 'failed', 'superseded', 'interrupted'}:
        raise ValueError('This remediation run is stopped or failed.')
    if run['input_snapshot_id'] != store.remediation_source_revision(sid):
        raise ValueError('The assessed source changed. Start a new remediation run.')
    if (not isinstance(files, list) or not 1 <= len(files) <= MAX_FILES
            or any(not isinstance(f, str) or not f or len(f) > 4096 for f in files)
            or len(set(files)) != len(files)):
        raise ValueError('Choose between 1 and 500 distinct files.')
    selected = selected_documents(store.get_decisions(sid, owner=owner))
    if set(files) - run_files(store, run['execution_id']).keys() or (selected is not None and set(files) - selected):
        raise ValueError('Files must belong to this accepted run and current document selection.')
    source = (scan.get('run') or {}).get('source')
    if source not in {'drive', 'sharepoint', 'local'}:
        raise ValueError('Automatic release requires a connected Google Drive or SharePoint destination.')
    records = store.get_file_records(sid, owner=owner)
    for file in files:
        record = records.get(file) or {}
        if source != 'local' and (not record.get('drive_file_id') or not record.get('source_modified') or (source == 'sharepoint' and not record.get('drive_id'))):
            raise ValueError('Tracked source identity and assessment freshness are required for every selected file.')
    require_grants(store, owner, review=False)
    return run, source, records


def destination_for(store, sid, owner, source, records, files, supplied=None):
    from routes.scans import _release_destination
    existing = store.release_for_scan(sid, owner)
    if existing:
        provider = existing.get('source') or source
        selected = dict(provider=provider, folder_id=existing.get('parent_folder_id') or 'root',
                        folder_name=existing.get('parent_folder_name') or ('Download package' if provider == 'local' else 'Existing release destination'))
    elif source == 'local':
        from routes.system import _release_destination as preference
        selected = preference(owner) or dict(provider='local', folder_id='root', folder_name='Download package')
    elif source == 'sharepoint':
        drives = {records[f]['drive_id'] for f in files}
        if len(drives) != 1:
            raise ValueError('Choose one explicit release destination for files from different libraries.')
        selected = dict(provider=source, folder_id=next(iter(drives)) + '/root', folder_name='Source library root')
    else:
        selected = dict(provider=source, folder_id='root', folder_name='Google Drive root')
    selected = _release_destination(source, supplied if supplied is not None else selected)
    if existing and (selected['provider'] != (existing.get('source') or source)
                     or selected['folder_id'] != (existing.get('parent_folder_id') or 'root')):
        raise ValueError('The existing Release destination cannot be changed by this authorization.')
    return selected


def destination_label(destination):
    if destination['provider'] == 'local':
        return 'Download package'
    return ('SharePoint' if destination['provider']=='sharepoint' else 'Google Drive') + ' / ' + destination['folder_name']


def legacy_sharepoint_failure(store, row, file, entry):
    """Recognize only a still-active, exact legacy dead delivery; never renew consent."""
    if row['status'] not in ACTIVE or row['intent']['destination']['provider'] != 'sharepoint' or entry.get('state') != 'failed' or entry.get('failure_category') == 'no_corrected_copy' or not entry.get('artifact_digest'):
        return False
    try:
        record = ready(store, row, file)
        if record['corrected_sha256'] != entry['artifact_digest']:
            return False
        original_sharepoint_job(store, row, file, entry['artifact_digest'])
        return True
    except (ValueError, KeyError):
        return False


def public(row, store=None):
    if row is None:
        return None
    files = row['intent']['files']
    progress = row['progress'].get('files', {})
    counts = dict(published=0, pending=0, blocked=0, failed=0)
    details = {}
    stalled_files = 0
    for file in files:
        entry = dict(progress.get(file, {'state': 'waiting', 'message': 'Waiting for a saved corrected copy' if row['intent'].get('allow_remaining_issues') else 'Waiting for approval and verification'}))
        # A delivery admitted before Stop may finish afterward. Its exact durable
        # receipt remains visible without reviving authorization or scheduling work.
        if store is not None and entry.get('artifact_digest'):
            saved = receipt(store, row, file, entry['artifact_digest'])
            if saved:
                entry = {**entry, 'state': 'published', 'receipt': saved, 'message': 'Delivered', 'requires_reconnect': False}
        if store is not None and legacy_sharepoint_failure(store, row, file, entry):
            entry.update(state='blocked', requires_reconnect=True, message='SharePoint delivery stopped. Restore access and resume this saved permission to check its receipt safely.')
        category = {'published':'published', 'failed':'failed', 'blocked':'blocked', 'stopped':'blocked'}.get(entry['state'], 'pending')
        if row['status'] == 'stopped' and category == 'pending':
            category = 'blocked'
        elif row['status'] == 'failed' and category == 'pending':
            category = 'failed'
        if category in {'pending', 'blocked'} and row['status'] in ACTIVE and row['progress'].get('_delivery_watch', {}).get('needs_attention') and (entry.get('artifact_digest') or entry.get('waiting_for_delivery')):
            category = 'blocked'
            stalled_files += 1
        counts[category] += 1
        details[file] = entry
    # Exact receipts describe delivery even after a terminal failure. This is
    # read-only presentation, never renewed authority; Stop remains explicit.
    status = 'completed' if row['status'] == 'failed' and files and counts['published'] == len(files) else row['status']
    package = None
    if store is not None and row['progress'].get('_package_job_id'):
        job = store.get_job(row['progress']['_package_job_id']) or {}
        package = dict(job_id=row['progress']['_package_job_id'], status=job.get('status', 'queued'))
        if status == 'completed' and package['status'] != 'done':
            status = 'failed' if package['status'] in {'dead', 'cancelled'} else 'publishing'
    from release_batch_progress import read_authorization
    batch_progress = read_authorization(store, row) if store is not None else {'available': False, 'scope': 'automatic'}
    reconnect_attention = row['status'] in ACTIVE and any(
        entry.get('requires_reconnect') for entry in details.values())
    return dict(id=row['id'], status=status, package=package, batch_progress=batch_progress, run_id=row['run_id'], files=list(files),
                request_id=row['request_id'], source_revision=row['intent']['source_revision'],
                destination_label=destination_label(row['intent']['destination']), destination=row['intent']['destination'],
                progress=counts, file_progress=details, stopped_at=row.get('stopped_at'),
                expires_at=row['intent']['expires_at'], revision=row['revision'],
                allow_remaining_issues=row['intent'].get('allow_remaining_issues', False),
                include_reports=row['intent'].get('include_reports', False),
                requires_reconnect=any(e.get('requires_reconnect') for e in details.values()),
                can_resume=row['intent']['destination']['provider'] in {'drive', 'sharepoint'} and row['status'] in ACTIVE and any(
                    e.get('state') == 'blocked' and e.get('artifact_digest') and e.get('failure_category') not in {'admitted_copy_changed', 'delivery_record_missing'} for e in details.values()),
                needs_attention=stalled_files > 0 or reconnect_attention,
                attention_reason=(row['progress'].get('_delivery_watch', {}).get('reason')
                                  if stalled_files else
                                  'Reconnect the delivery provider, then resume this saved release.'
                                  if reconnect_attention else None),
                last_progress_at=row['progress'].get('_delivery_watch', {}).get('last_progress_at'))


def planning_preview(store, sid, owner, files, destination=None):
    """Describe a local pre-Start choice without accepting release permission."""
    from assessment_policy import selected_documents
    result = dict(available=False, reason=None, files=[], source_revision=None,
                  destination=None, destination_label=None, blocked_files=[], source=None, destination_locked=False)
    try:
        scan = store.get_scan(sid, owner=owner)
        if not scan:
            raise ValueError('Scan not found')
        require_grants(store, owner, review=False)
        if (not isinstance(files, list) or not 1 <= len(files) <= MAX_FILES
                or any(not isinstance(f, str) or not f or len(f) > 4096 for f in files)
                or len(set(files)) != len(files)):
            raise ValueError('Choose between 1 and 500 distinct files.')
        selected = selected_documents(store.get_decisions(sid, owner=owner))
        if selected is not None and set(files) - selected:
            raise ValueError('Files must belong to the current document selection.')
        source = (scan.get('run') or {}).get('source')
        if source not in {'drive', 'sharepoint', 'local'}:
            raise ValueError('Automatic release requires a connected Google Drive or SharePoint destination.')
        result.update(source=source, destination_locked=bool(store.release_for_scan(sid, owner)))
        records = store.get_file_records(sid, owner=owner, files=files)
        blocked = []
        for file in files:
            record = records.get(file) or {}
            status = record.get('status')
            if not record:
                reason = 'No assessment record. Assess this file first.'
            elif status == 'error':
                reason = 'Assessment failed. Retry assessment for this file.'
            elif status in {'queued', 'pending', 'processing'}:
                reason = 'Assessment is still queued or running. Wait for it to finish, then refresh.'
            elif record.get('score') is None:
                reason = 'Assessment completion is not recorded. Recheck this file in Assess.'
            elif source != 'local' and (not record.get('drive_file_id') or not record.get('source_modified') or (source == 'sharepoint' and not record.get('drive_id'))):
                reason = 'Source identity or freshness is missing. Refresh the source and reassess this file.'
            else:
                continue
            blocked.append(dict(file=file, reason=reason))
        blocked_names = {entry['file'] for entry in blocked}
        ready_files = [file for file in files if file not in blocked_names]
        if not ready_files:
            result.update(blocked_files=blocked, reason=f'{len(blocked)} of {len(files)} selected files need attention before automatic publishing.')
            return result
        destination = destination_for(store, sid, owner, source, records, ready_files, destination)
        result.update(available=True, files=sorted(ready_files), blocked_files=blocked,
                      reason=(f'{len(ready_files)} files can publish automatically. {len(blocked)} files will be skipped and remain in the follow-up checklist.' if blocked else None),
                      source_revision=store.remediation_source_revision(sid),
                      destination=destination, destination_label=destination_label(destination))
    except ValueError as exc:
        result['reason'] = str(exc)
    return result


def preview(store, sid, owner, files, destination=None):
    run = current_run(store, sid, owner)
    row = persistence.latest(store, sid, owner, run_id=run['execution_id']) if run else None
    result = dict(available=False, reason=None, run_id=run['execution_id'] if run else None,
                  destination=None, destination_label=None, authorization=public(row, store),
                  planning=planning_preview(store, sid, owner, files, destination))
    try:
        run, source, records = selection(store, sid, owner, files)
        destination = destination_for(store, sid, owner, source, records, files, destination)
        result.update(available=True, destination=destination, destination_label=destination_label(destination))
    except ValueError as exc:
        result['reason'] = str(exc)
    return result


def authorize(store, sid, owner, run_id, files, destination, request_id, expected_source_revision=None, *, allow_remaining_issues=False, include_reports=False):
    import publish
    if type(allow_remaining_issues) is not bool or type(include_reports) is not bool:
        raise ValueError("Release options must be booleans.")
    if not isinstance(request_id, str) or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,128}', request_id):
        raise ValueError('A bounded unique request ID is required.')
    # A lost-response replay must not recompute a later expiry or folder name.
    prior = persistence.by_request(store, owner, sid, request_id)
    if prior:
        if (expected_source_revision is not None and expected_source_revision != prior['intent']['source_revision']) or prior['run_id'] != run_id or sorted(files) != list(prior['intent']['files']) or destination != prior['intent']['destination'] or allow_remaining_issues != prior['intent'].get('allow_remaining_issues', False) or include_reports != prior['intent'].get('include_reports', False):
            raise ValueError('This request ID belongs to different release permission.')
        return prior
    with store.transaction():
        with store._db.cursor() as cur:
            store._db.execute(cur,'UPDATE scan_runs SET owner_email=owner_email WHERE id=%s AND owner_email=%s',(sid,owner))
        prior = persistence.by_request(store,owner,sid,request_id)
        if prior:
            if (expected_source_revision is not None and expected_source_revision != prior['intent']['source_revision']) or prior['run_id'] != run_id or sorted(files) != list(prior['intent']['files']) or destination != prior['intent']['destination'] or allow_remaining_issues != prior['intent'].get('allow_remaining_issues', False) or include_reports != prior['intent'].get('include_reports', False):
                raise ValueError('This request ID belongs to different release permission.')
            return prior
        run, source, records = selection(store, sid, owner, files, run_id)
        if expected_source_revision is not None and expected_source_revision != run['input_snapshot_id']:
            raise ValueError('The assessed source changed after Plan. Review the plan and start again.')
        destination = destination_for(store, sid, owner, source, records, files, destination)
        existing = store.release_for_scan(sid, owner)
        intent = dict(version=2, allow_remaining_issues=allow_remaining_issues, include_reports=include_reports, source=source, source_revision=run['input_snapshot_id'],
                      files={f: record_identity(records[f]) for f in sorted(files)}, destination=destination,
                      release_folder_name=(existing or {}).get('folder_name') or publish.release_folder_name(timezone_name=publish.user_release_timezone(store, owner), owner_email=owner),
                      release_parent_id=existing.get('parent_folder_id') if existing else (None if destination['provider'] == 'local' else destination['folder_id']),
                      expires_at=(datetime.now(timezone.utc)+timedelta(hours=24)).isoformat())
        return persistence.create(store, owner, sid, run_id, request_id, intent)


def require_authority(store, row, file):
    if not row or row['status'] not in ACTIVE or file not in row['intent']['files']:
        raise ValueError('Automatic release is not active for this file.')
    if datetime.now(timezone.utc) >= datetime.fromisoformat(row['intent']['expires_at']):
        raise ValueError('Automatic release authorization expired. Enable it again explicitly.')
    run, source, records = selection(store, row['scan_id'], row['owner_email'], list(row['intent']['files']), row['run_id'])
    if source != row['intent']['source'] or run['input_snapshot_id'] != row['intent']['source_revision']:
        raise ValueError('The authorized run or source changed.')
    record = records[file]
    if record_identity(record) != row['intent']['files'][file]:
        raise ValueError('Source identity changed. Reassess before release.')
    existing = store.release_for_scan(row['scan_id'], row['owner_email'])
    if existing and (existing.get('parent_folder_id') != row['intent']['release_parent_id']
                     or existing.get('folder_name') != row['intent']['release_folder_name']):
        raise ValueError('The authorized release destination changed.')
    return record


def ready(store, row, file):
    record = require_authority(store, row, file)
    from vision_recovery import pending_for_file
    if pending_for_file(store, row['scan_id'], row['run_id'], file):
        raise ValueError('Waiting for the bounded vision retry to finish preparing available corrections.')
    work_state = run_files(store, row['run_id']).get(file)
    partial = row['intent'].get('allow_remaining_issues') is True
    with store._db.cursor() as cur:
        store._db.execute(cur, "SELECT payload FROM jobs WHERE scan_id=%s AND type='apply_approved_values' AND status IN ('queued','running','processing','retry')", (row['scan_id'],))
        for job in store._db.fetchall(cur):
            payload = json.loads(job['payload']) if isinstance(job['payload'], str) else job['payload']
            if payload.get('phase') == 'approve_current_run_ai' and payload.get('run_id') == row['run_id']:
                raise ValueError('Waiting for automatic approval to queue the current run’s corrections.')
            if payload.get('file') == file:
                raise ValueError('Waiting for active corrections to finish writing this file.')
    if partial and work_state in {'dead', 'cancelled'}:
        raise FileRemediationFinishedWithoutCopy('Remediation for this file stopped or failed. No corrected copy was published; the original is unchanged.')
    if work_state != 'done':
        raise ValueError('Waiting for this run to finish remediating the file.')
    if partial and (not record.get('remediated_at') or not re.fullmatch('[0-9a-f]{64}', record.get('corrected_sha256') or '')):
        from missing_corrected_copy import explanation
        raise FileRemediationFinishedWithoutCopy(explanation(store, row['scan_id'], row['owner_email'], file, record))
    if (not partial and not record.get('compliant')) or not record.get('remediated_at') or not re.fullmatch('[0-9a-f]{64}', record.get('corrected_sha256') or ''):
        raise ValueError('Waiting for a saved corrected artifact.' if partial else 'Waiting for a verified corrected artifact.')
    # Remaining-issue permission covers unresolved findings, not approved content
    # promised for this document. Job completion alone cannot prove that write.
    if store.count_unapplied_approved_values(row['scan_id'], file):
        raise ValueError('Approved changes still need application and verification.')
    from review_item_kind import optional_inspection
    for item in store.list_hitl_queue(scan_id=row['scan_id'], owner=row['owner_email'], include_superseded=True):
        if optional_inspection(item):
            continue
        if partial or item.get('file') != file or item.get('superseded'):
            continue
        if item.get('status') not in {'approved', 'resolved'}:
            raise ValueError('Per-file review or manual work remains.')
        if item.get('status') == 'approved' and item.get('proposals'):
            if item.get('approved_source_revision') != row['intent']['source_revision']:
                raise ValueError('Approval belongs to a different assessed source.')
    require_current_record(store, row['scan_id'], file, record['corrected_sha256'], record['remediated_at'], owner=row['owner_email'], allow_remaining_issues=partial)
    return record


def receipt(store, row, file, digest):
    destination = row['intent'].get('destination')
    provider = destination.get('provider') if isinstance(destination, dict) else None
    if provider not in {'drive', 'sharepoint', 'local'}:
        return None
    release = store.release_for_scan(row['scan_id'], row['owner_email'])
    if not release or release.get('source') != provider or release.get('parent_folder_id') != row['intent']['release_parent_id'] or release.get('folder_name') != row['intent']['release_folder_name']:
        return None
    saved = store.get_release_document(release['id'], file, row['owner_email'])
    return saved if saved and saved.get('status') == 'published' and saved.get('artifact_digest') == artifact_tag(digest) else None


def publish_admission(store, authorization_id, owner, sid, file, digest, *, queued=False):
    """Stop's linearization barrier: queued jobs must obtain fresh permission.

    A permit committed before Stop is in-flight; it may finish. Stop prevents all
    later permits, including jobs already queued in a different worker process.
    """
    with store.transaction():
        row = persistence.get(store, authorization_id, owner, lock=True)
        if not row or row['scan_id'] != sid:
            raise ValueError('Automatic release authorization not found.')
        record = ready(store, row, file)
        frozen = row['progress'].get('files', {}).get(file, {}).get('artifact_digest')
        if frozen and not queued:
            raise DeliveryAlreadyAdmitted('Delivery is already admitted. Reconcile its existing receipt.')
        if queued and not frozen:
            raise ValueError('Queued delivery has no prior exact automatic admission.')
        if frozen and frozen != digest:
            raise ValueError('This authorization already admitted a different artifact. Confirm a new authorization.')
        if not queued and not frozen:
            with store._db.cursor() as cur:
                store._db.execute(cur, "SELECT payload,status FROM jobs WHERE scan_id=%s "
                    "AND type='publish_file' AND status IN ('queued','running','processing','retry')", (sid,))
                outstanding = set()
                for item in store._db.fetchall(cur):
                    data = json.loads(item['payload']) if isinstance(item['payload'], str) else item['payload']
                    if data.get('automatic_release_id') == row['id']:
                        outstanding.add(data.get('file'))
            # Admission itself occupies a slot before a queue/response exists.
            outstanding.update(name for name, entry in row['progress'].get('files', {}).items()
                if entry.get('artifact_digest') and (entry.get('state') == 'publishing' or entry.get('dispatch_error')))
            if len(outstanding) >= MAX_DISPATCH_PER_TICK:
                raise DeliveryCapacityFull('Corrected copy is ready. Waiting for a delivery slot.')
        if digest != record['corrected_sha256']:
            raise ValueError('The verified artifact changed after dispatch was requested.')
        return persistence.update_file(store, row['id'], owner, file, dict(state='publishing', artifact_digest=digest, waiting_for_delivery=False,
            remediated_at=record['remediated_at'], message='Delivery admitted; an in-flight request may finish after Stop.'))


def publish_job(store, payload, job, callback):
    from worker import FatalJobError
    tag = payload.get('artifact_digest') or ''
    try:
        admitted = publish_admission(store, payload['automatic_release_id'], payload['owner'], payload['scan_id'], payload['file'], tag.removeprefix('sha256:'), queued=True)
    except ValueError as exc:
        raise FatalJobError(str(exc)) from exc
    # The barrier just proved these exact admitted bytes against the current
    # source/approval state. A metadata-only timestamp refresh must not send the
    # handler back to the stale queue-time timestamp. Never replace its digest.
    current = admitted['progress']['files'][payload['file']]
    return callback({**payload, 'remediated_at': current['remediated_at']}, job)


def delivery_watch(progress, pending_jobs):
    """Watch actual delivery transitions, never continuation heartbeats or row updates."""
    entries = progress.get('files', {})
    eligible = any(e.get('state') not in {'published', 'failed'} and
                   (e.get('artifact_digest') or e.get('waiting_for_delivery'))
                   for e in entries.values())
    signature = json.dumps({
        'files': {f: {k: e.get(k) for k in ('state', 'artifact_digest', 'receipt', 'waiting_for_delivery')}
                  for f, e in entries.items()},
        'jobs': {f: sorted(states) for f, states in pending_jobs.items()},
    }, sort_keys=True)
    previous = progress.get('_delivery_watch', {})
    now = datetime.now(timezone.utc)
    changed = previous.get('signature') != signature
    last = now.isoformat() if changed else previous.get('last_progress_at', now.isoformat())
    try:
        elapsed = (now - datetime.fromisoformat(last)).total_seconds()
    except (ValueError, TypeError):
        last, elapsed = now.isoformat(), 0
    stalled = eligible and elapsed >= STALL_AFTER_SECONDS
    return dict(signature=signature, last_progress_at=last, needs_attention=stalled,
                reason=('No delivery progress for 10 minutes. Check the destination and delivery receipt before retrying; a copy may already exist.'
                        if stalled else None))


def resume(store, authorization_id, owner, scan_id):
    """Wake the original permission after reconnect; never extend or replace it."""
    with store.transaction():
        row = persistence.get(store, authorization_id, owner, lock=True)
        if not row or row['scan_id'] != scan_id or row['status'] not in ACTIVE:
            raise ValueError('This release permission cannot be resumed. Review a new plan explicitly.')
        for file in row['intent']['files']:
            require_authority(store, row, file)
        progress = dict(row['progress'])
        entries = {f: dict(e) for f, e in progress.get('files', {}).items()}
        for file, entry in entries.items():
            saved = receipt(store, row, file, entry['artifact_digest']) if entry.get('artifact_digest') else None
            if saved:
                entry.update(state='published', receipt=saved, requires_reconnect=False, resume_requested=False, message='Delivered')
            if legacy_sharepoint_failure(store, row, file, entry):
                entry.update(state='blocked', requires_reconnect=True)
            if entry.get('state') == 'published' or entry.get('failure_category') == 'no_corrected_copy':
                continue
            if entry.get('artifact_digest'):
                record = ready(store, row, file)
                if entry['artifact_digest'] != record['corrected_sha256']:
                    raise ValueError('The admitted corrected copy changed. Review a new plan explicitly.')
                entry.update(state='publishing', resume_requested=True, requires_reconnect=False,
                    message='Checking the saved delivery before resuming.')
        progress['files'] = entries
        progress.pop('_delivery_watch', None)
        return persistence.save(store, row, status='waiting', progress=progress, schedule=True, delay=0)


def original_sharepoint_job(store, row, file, digest, *, lock=False):
    """Read and validate the original exact queue and destination identity."""
    with store._db.cursor() as cur:
        suffix = " FOR UPDATE" if lock and store._db.supports_skip_locked else ""
        store._db.execute(cur, "SELECT id,payload,status,batch_id FROM jobs WHERE scan_id=%s AND type='publish_file'" + suffix, (row['scan_id'],))
        matching = []
        for job in store._db.fetchall(cur):
            payload = json.loads(job['payload']) if isinstance(job['payload'], str) else job['payload']
            if payload.get('automatic_release_id') == row['id'] and payload.get('file') == file:
                if payload.get('owner') != row['owner_email'] or payload.get('artifact_digest') != 'sha256:' + digest:
                    raise ValueError('The original delivery identity differs. Review a new plan explicitly.')
                matching.append(job)
        if len(matching) != 1 or matching[0]['status'] != 'dead':
            raise ValueError('The original delivery cannot be resumed safely. Check its receipt and stage.')
        job = matching[0]
        original = json.loads(job['payload']) if isinstance(job['payload'], str) else job['payload']
        release = store.release_status(original.get('release_id'), row['owner_email'])
        if not release or release.get('scan_id') != row['scan_id'] or release.get('parent_folder_id') != row['intent']['release_parent_id'] or release.get('folder_name') != row['intent']['release_folder_name']:
            raise ValueError('The original release destination differs. Review a new plan explicitly.')
        store._db.execute(cur, "SELECT owner_email,state,is_current FROM stage_executions WHERE execution_id=%s" + suffix, (job['batch_id'],))
        stage = store._db.fetchone(cur)
        if not stage or stage['owner_email'] != row['owner_email'] or not stage['is_current'] or stage['state'] == 'cancelled':
            raise ValueError('The original release stage changed or was stopped. Review a new plan explicitly.')
        return job


def resume_sharepoint_job(store, row, file, digest):
    """Revive the original dead job, retaining its receipt and reservation identity.

    Advance consumes exact receipts first. The same worker then checks the exact
    destination bytes before writing; a newly computed batch could lose that proof.
    """
    with store.transaction():
        fresh = persistence.get(store, row['id'], row['owner_email'], lock=True)
        require_authority(store, fresh, file)
        if fresh['intent']['destination']['provider'] != 'sharepoint':
            raise ValueError('This recovery requires the original SharePoint destination.')
        entry = fresh['progress'].get('files', {}).get(file, {})
        if not entry.get('resume_requested') or entry.get('artifact_digest') != digest:
            raise ValueError('Resume the saved delivery explicitly before retrying.')
        job = original_sharepoint_job(store, fresh, file, digest, lock=True)
        with store._db.cursor() as cur:
            now = store._now()
            store._db.execute(cur, "UPDATE jobs SET status='queued',attempts=0,run_after=%s,locked_at=NULL,locked_by=NULL,lease_expires_at=NULL,last_error=NULL,updated_at=%s WHERE id=%s AND status='dead'", (now, now, job['id']))
            store._db.execute(cur, "UPDATE stage_work_items SET state='queued',revision=revision+1,attempt=0,lease_owner=NULL,lease_token=NULL,lease_expires_at=NULL,terminal_reason=NULL,updated_at=%s WHERE job_id=%s", (now, job['id']))
            store._db.execute(cur, "UPDATE stage_executions SET state='processing',revision=revision+1,updated_at=%s WHERE execution_id=%s", (now, job['batch_id']))


def delivery_preflight(row):
    """Read provider readiness before freezing a new delivery admission."""
    from routes.scans import _preflight_release_destination
    destination = row['intent']['destination']
    if destination['provider'] == 'local':
        return {'ready': True}
    return _preflight_release_destination(request_for(row['owner_email'], row['scan_id']), destination)


def dispatch(store, row, file, digest):
    from routes.scans import publish_files
    request = request_for(row['owner_email'], row['scan_id'])
    if row['intent']['destination']['provider'] == 'drive' and not request.headers.get('x-drive-token'):
        raise DriveReconnectRequired('Reconnect Google Drive to resume this saved release. No new upload has been requested.')
    return publish_files(row['scan_id'], request,
        dict(files=[file], destination=row['intent']['destination'] if row['intent']['release_parent_id'] or row['intent']['destination']['provider'] == 'local' else None,
             release_folder_name=row['intent']['release_folder_name'], automatic_release_id=row['id'],
             allow_remaining_issues=row['intent'].get('allow_remaining_issues', False),
             expected_artifacts={file: digest}, expected_destination=row['intent']['destination'] if row['intent']['release_parent_id'] else None))


def recover_approved_writes(store, row, file):
    """One bounded recovery of already-approved content, under automatic run authority.

    No new approvals are created. An old Done job is not proof its values reached
    the copy. The deterministic queue identity survives concurrent ticks and
    prevents a failed or unwritable obligation becoming an infinite retry loop.
    """
    from hashlib import sha256
    from store import job_priority
    with store.transaction():
        row = persistence.get(store, row['id'], row['owner_email'], lock=True)
        require_authority(store, row, file)
        if run_files(store, row['run_id']).get(file) != 'done' or not store.count_unapplied_approved_values(row['scan_id'], file):
            return False
        with store._db.cursor() as cur:
            store._db.execute(cur, "SELECT id,status,payload FROM jobs WHERE scan_id=%s "
                "AND type='apply_approved_values' ORDER BY created_at DESC,id DESC", (row['scan_id'],))
            previous = []
            for job in store._db.fetchall(cur):
                data = json.loads(job['payload']) if isinstance(job['payload'], str) else job['payload']
                if data.get('file') == file:
                    previous.append(dict(job, decoded_payload=data))
            if any(job['status'] in {'queued', 'running', 'processing', 'retry'} for job in previous):
                return True
        job_id = 'approved-recovery-' + sha256(f"{row['id']}\0{file}".encode()).hexdigest()[:32]
        if any(job['id'] == job_id for job in previous):
            raise ValueError('Approved-change recovery finished without applying every value. The previous copy is retained; review the recorded write failure before retrying.')
        items = [item for item in store._approved_unapplied_rows(row['scan_id'], file)
                 if store._row_approved_values(item) or
                    (not store._row_owes_no_document_content(item) and str(item.get('approved_value') or '').strip())]
        if not items or any(not store._row_approved_values(item) or
                item.get('approved_source_revision') != row['intent']['source_revision'] for item in items):
            raise ValueError('Approved changes lack current-source or writable-location evidence. Restore that evidence before publishing this copy.')
        current = {item['id']: item for item in store.list_hitl_queue(
            scan_id=row['scan_id'], owner=row['owner_email'], include_superseded=True)}
        if any(item['id'] not in current or current[item['id']].get('superseded') for item in items):
            raise ValueError('Approved changes belong to superseded proposals. Review the current source before publishing.')
        payload = {'owner': row['owner_email'], 'scan_id': row['scan_id'], 'file': file,
                   'automatic_release_recovery_id': row['id']}
        if any(str(item.get('last_decision_request_id') or '').startswith('standing:') for item in items):
            original = next((job['decoded_payload'] for job in previous
                             if job['decoded_payload'].get('standing_approval')), None)
            if not original:
                raise ValueError('The exact saved automatic-approval write request is unavailable. Restore its evidence before publishing.')
            from ai_standing_approval import check_application
            check_application(store, original)
            payload = dict(original, automatic_release_recovery_id=row['id'])
        now = store._now()
        with store._db.cursor() as cur:
            store._db.execute(cur, "INSERT INTO jobs(id,type,payload,status,priority,attempts,max_attempts,"
                "run_after,scan_id,created_at,updated_at) VALUES(%s,'apply_approved_values',%s,'queued',%s,0,2,%s,%s,%s,%s) "
                "ON CONFLICT(id) DO NOTHING", (job_id, json.dumps(payload), job_priority('apply_approved_values'),
                                                now, row['scan_id'], now, now))
        return True


def _permanent_delivery_pause(row, job_states):
    """Pause only frozen changed-copy exceptions; recoverable work keeps polling."""
    files = row['intent']['files']
    entries = row['progress'].get('files', {})
    changed = [entries.get(file, {}) for file in files
               if entries.get(file, {}).get('state') == 'blocked'
               and entries.get(file, {}).get('failure_category') == 'admitted_copy_changed'
               and entries.get(file, {}).get('artifact_digest')]
    return bool(changed) and all(
        entries.get(file, {}).get('state') in {'published', 'failed'} or
        (entries.get(file, {}).get('state') == 'blocked' and
         entries.get(file, {}).get('failure_category') == 'admitted_copy_changed' and
         entries.get(file, {}).get('artifact_digest')) for file in files
    ) and all(state in {'done', 'dead', 'cancelled'} for state in job_states)


def advance(store, payload, job):
    from routes.scans import publish_files
    from worker import check_cancel
    row = persistence.get(store, payload['authorization_id'], payload['owner'])
    if not row or row['status'] not in ACTIVE or payload['revision'] != row['progress'].get('_tick_revision', 0):
        return
    dispatched = 0
    preflight = None
    pending_jobs = {}
    with store._db.cursor() as cur:
        store._db.execute(cur,"SELECT payload,status FROM jobs WHERE scan_id=%s AND type='publish_file'",(row['scan_id'],))
        for item in store._db.fetchall(cur):
            data = json.loads(item['payload']) if isinstance(item['payload'],str) else item['payload']
            if data.get('automatic_release_id') == row['id']:
                pending_jobs.setdefault(data.get('file'),[]).append(item['status'])
    # Provider retry/backoff still occupies an admitted slot. Never bypass it.
    outstanding = sum(any(s in {'queued', 'running', 'processing', 'retry'} for s in states)
                      for states in pending_jobs.values())
    dispatch_limit = max(0, MAX_DISPATCH_PER_TICK - outstanding)
    for file in row['intent']['files']:
        check_cancel()
        row = persistence.get(store, row['id'], row['owner_email'])
        if row['status'] not in ACTIVE:
            return
        entry = row['progress'].get('files', {}).get(file, {})
        if entry.get('state') == 'failed' and entry.get('artifact_digest'):
            saved = receipt(store, row, file, entry['artifact_digest'])
            if saved:
                persistence.update_file(store, row['id'], row['owner_email'], file,
                    dict(state='published', receipt=saved, requires_reconnect=False, resume_requested=False, message='Delivered'))
                continue
        if legacy_sharepoint_failure(store, row, file, entry):
            row = persistence.update_file(store, row['id'], row['owner_email'], file,
                dict(state='blocked', requires_reconnect=True, message='SharePoint delivery stopped. Restore access and resume this saved permission to check its receipt safely.'))
            entry = row['progress']['files'][file]
        if entry.get('state') in {'published', 'failed'}:
            continue
        try:
            if entry.get('artifact_digest'):
                saved = receipt(store, row, file, entry['artifact_digest'])
                if saved:
                    persistence.update_file(store,row['id'],row['owner_email'],file,
                        dict(state='published',receipt=saved,message='Delivered',requires_reconnect=False,resume_requested=False,failure_category=None))
                elif pending_jobs.get(file) and all(s in {'dead','cancelled'} for s in pending_jobs[file]):
                    # A dead request for older bytes is not a connection problem.
                    # Keep its frozen identity and consume receipts first, but do
                    # not encourage recovery to upload a different corrected copy.
                    current_copy = store.get_file_record(row['scan_id'], file) or {}
                    if current_copy.get('corrected_sha256') and current_copy['corrected_sha256'] != entry['artifact_digest']:
                        persistence.update_file(store, row['id'], row['owner_email'], file,
                            dict(state='blocked', failure_category='admitted_copy_changed',
                                 requires_reconnect=False, resume_requested=False,
                                 message='The corrected copy changed after delivery was queued. Check the original delivery result, then review a new release plan for the current copy.'))
                        continue
                    if row['intent']['destination']['provider'] == 'drive':
                        if entry.get('resume_requested') and dispatched < dispatch_limit:
                            dispatch(store, row, file, entry['artifact_digest'])
                            dispatched += 1
                            persistence.update_file(store,row['id'],row['owner_email'],file,
                                dict(state='publishing', resume_requested=False, requires_reconnect=False, message='Checking the saved delivery before resuming.'))
                        else:
                            persistence.update_file(store,row['id'],row['owner_email'],file,
                                dict(state='blocked', message='Delivery job stopped or failed. Reconnect Google Drive and resume to check its receipt safely.'))
                    else:
                        if entry.get('resume_requested') and dispatched < dispatch_limit:
                            resume_sharepoint_job(store, row, file, entry['artifact_digest'])
                            dispatched += 1
                            persistence.update_file(store,row['id'],row['owner_email'],file,
                                dict(state='publishing', resume_requested=False, requires_reconnect=False,
                                     message='Checking the saved delivery before resuming.'))
                        else:
                            persistence.update_file(store,row['id'],row['owner_email'],file,
                                dict(state='blocked',message='SharePoint delivery stopped. Restore access and resume the saved release to check its receipt safely.'))
                elif row['intent']['destination']['provider'] == 'drive' and not pending_jobs.get(file) and dispatched < dispatch_limit:
                    # Legacy synchronous delivery has no durable worker. The queue helper
                    # retains its stage/reservation identities and only admits frozen bytes.
                    dispatch(store, row, file, entry['artifact_digest'])
                    dispatched += 1
                    persistence.update_file(store,row['id'],row['owner_email'],file,
                        dict(state='publishing', resume_requested=False, requires_reconnect=False, message='Checking the saved delivery before resuming.'))
                elif row['intent']['destination']['provider'] == 'sharepoint' and not pending_jobs.get(file):
                    # An admitted artifact without a durable delivery job is an uncertain
                    # delivery, not a worker still publishing. Preserve its frozen identity;
                    # creating another upload here could duplicate a provider-side result.
                    persistence.update_file(store,row['id'],row['owner_email'],file,
                        dict(state='blocked', failure_category='delivery_record_missing',
                             requires_reconnect=False, resume_requested=False,
                             message='No delivery job or receipt is recorded for this saved copy. Inspect the destination before retrying; a copy may already exist.'))
                else:
                    persistence.update_file(store,row['id'],row['owner_email'],file,
                        dict(state='publishing', message='Delivery not yet confirmed. Waiting for a recorded receipt; a copy may already exist.'))
                # Once admitted, freeze the artifact and reconcile its receipt.
                # A changed artifact or lost provider result never buys a new delivery.
                continue
            if recover_approved_writes(store, row, file):
                persistence.update_file(store, row['id'], row['owner_email'], file,
                    dict(state='waiting', message='Applying previously approved changes before preparing the published copy.'))
                continue
            record = ready(store, row, file)
            digest = record['corrected_sha256']
            saved = receipt(store, row, file, digest)
            if saved:
                persistence.update_file(store, row['id'], row['owner_email'], file, dict(state='published',artifact_digest=digest,receipt=saved,message='Delivered'))
                continue
            if entry.get('state') == 'publishing':
                # A queued or possibly-dispatched request is never blindly bought again.
                continue
            if dispatched >= dispatch_limit:
                continue
            with store._db.cursor() as cur:
                store._db.execute(cur, "SELECT j.payload FROM stage_executions e JOIN jobs j "
                    "ON j.batch_id=e.execution_id WHERE e.scan_id=%s AND e.stage='release' "
                    "AND e.is_current=1 AND e.state IN ('accepted','queued','processing','paused')", (row['scan_id'],))
                active = store._db.fetchall(cur)
                if any((json.loads(item['payload']) if isinstance(item['payload'], str) else item['payload']).get('automatic_release_id') != row['id'] for item in active):
                    persistence.update_file(store, row['id'], row['owner_email'], file,
                        dict(state='waiting', waiting_for_delivery=True, message='Corrected copy is ready. Waiting for the current delivery to finish.'))
                    continue
            if preflight is None:
                preflight = delivery_preflight(row)
            if not preflight.get('ready'):
                message = preflight.get('message') or 'The authorized delivery destination is not ready. Check its access before delivery.'
                if preflight.get('credential_valid') is False:
                    raise DriveReconnectRequired(message)
                raise DeliveryPreflightBlocked(message)
            row = publish_admission(store, row['id'], row['owner_email'], row['scan_id'], file, digest)
            dispatched += 1
            result = dispatch(store, row, file, digest)
            outcome = next((r for r in result.get('published',[]) if r.get('file')==file),{})
            confirmed = receipt(store,row,file,digest)
            state = 'published' if confirmed else 'publishing' if outcome.get('status') in {'queued','published'} else 'failed'
            persistence.update_file(store,row['id'],row['owner_email'],file,dict(state=state,artifact_digest=digest,requires_reconnect=False,failure_category=None,
                message='Delivered' if state=='published' else 'Waiting for delivery receipt' if state=='publishing' else 'Delivery was not confirmed. Inspect the receipt before retrying.',receipt=outcome))
        except DeliveryCapacityFull as exc:
            persistence.update_file(store, row['id'], row['owner_email'], file,
                dict(state='waiting', waiting_for_delivery=True, message=str(exc)))
        except DeliveryAlreadyAdmitted:
            continue
        except FileRemediationFinishedWithoutCopy as exc:
            persistence.update_file(store,row['id'],row['owner_email'],file,dict(state='failed',message=str(exc),failure_category='no_corrected_copy'))
        except DeliveryPreflightBlocked as exc:
            persistence.update_file(store,row['id'],row['owner_email'],file,dict(state='blocked',message=str(exc),failure_category='delivery_preflight_blocked',requires_reconnect=False,waiting_for_delivery=False))
        except DriveReconnectRequired as exc:
            persistence.update_file(store,row['id'],row['owner_email'],file,dict(state='blocked',message=str(exc),failure_category='delivery_preflight_blocked',requires_reconnect=True,waiting_for_delivery=False))
        except (ValueError, ReleaseArtifactError) as exc:
            persistence.update_file(store,row['id'],row['owner_email'],file,dict(state='blocked',message=str(exc),waiting_for_delivery=False))
        except Exception as exc:
            # Retain bounded diagnostic metadata; never serialize provider credentials.
            status = getattr(exc, 'status_code', None)
            diagnostic = {'error_type': type(exc).__name__[:80],
                          'http_status': status if type(status) is int and 100 <= status <= 599 else None}
            store.log_decision(row['owner_email'], 'release.dispatch_outcome_unknown', scan_id=row['scan_id'], file=file, detail=json.dumps(diagnostic))
            persistence.update_file(store,row['id'],row['owner_email'],file,
                # A lost response is not proof of failure. Retain admission and
                # continue receipt checks without dispatching the artifact again.
                dict(state='blocked', dispatch_error=diagnostic,
                     message='Delivery outcome is unknown. Reconcile its receipt before retrying.'))
    with store.transaction():
        row = persistence.get(store,row['id'],row['owner_email'],lock=True)
        if row['status'] not in ACTIVE or payload['revision'] != row['progress'].get('_tick_revision',0):
            return
        states = [r.get('state') for r in row['progress'].get('files',{}).values()]
        expired = datetime.now(timezone.utc) >= datetime.fromisoformat(row['intent']['expires_at'])
        completed = len(states)==len(row['intent']['files']) and all(s=='published' for s in states)
        terminal = len(states)==len(row['intent']['files']) and all(s in {'published', 'failed'} for s in states)
        if (terminal or expired) and row['intent'].get('include_reports'):
            from release_report_delivery import queue_release_reports
            release = store.release_for_scan(row['scan_id'], row['owner_email'])
            if release:
                # Include selected files that finished without a copy even if another
                # file created the release only later in this tick. Never replace a receipt.
                failures = [(file, entry) for file, entry in row['progress'].get('files', {}).items()
                            if entry.get('failure_category') == 'no_corrected_copy']
                if failures:
                    store.ensure_release_execution(row['scan_id'], row['owner_email'], row['intent']['destination']['provider'],
                        len(row['intent']['files']), preferred_folder_name=release['folder_name'],
                        parent_folder_id=release.get('parent_folder_id'), parent_folder_name=release.get('parent_folder_name'))
                    for file, entry in failures:
                        saved = store.get_release_document(release['id'], file, row['owner_email'])
                        if not saved or saved.get('status') != 'published':
                            store.record_release_document(release['id'], row['owner_email'],
                                dict(file=file, status='failed', failure_category='no_corrected_copy', explanation=entry['message']))
                # Freeze reports and enqueue delivery in the same transaction as completion.
                queue_release_reports(store, row['scan_id'], row['owner_email'], release['id'])
        progress = {**row['progress'], '_delivery_watch': delivery_watch(row['progress'], pending_jobs)}
        reconnect_blocked = any(
            entry.get('state') == 'blocked' and entry.get('requires_reconnect')
            for entry in progress.get('files', {}).values())
        if reconnect_blocked:
            progress['_delivery_watch'] = {
                **progress['_delivery_watch'],
                'needs_attention': True,
                'reason': 'Reconnect the delivery provider, then resume this saved release.',
            }
        wake_requested = progress.pop('_wake_requested', False)
        if terminal and row['intent']['destination']['provider'] == 'local' and not progress.get('_package_job_id'):
            published = {f: e['artifact_digest'] for f, e in progress.get('files', {}).items() if e.get('state') == 'published'}
            if published:
                report_bundle = None
                if row['intent'].get('include_reports'):
                    from release_report_delivery import get_latest_release_reports
                    report_bundle = get_latest_release_reports(store, row['scan_id'], row['owner_email']).get('bundle_id')
                progress['_package_job_id'] = store.enqueue_job('prepare_release_package', dict(
                    scan_id=row['scan_id'], owner=row['owner_email'], files=sorted(published),
                    preserve_hierarchy=True, include_manifest=True,
                    expected_artifacts=published, expected_source_revision=row['intent']['source_revision'],
                    report_bundle_id=report_bundle,
                    allow_remaining_issues=row['intent'].get('allow_remaining_issues', False)), scan_id=row['scan_id'], max_attempts=3)
        # Re-read durable job states under the authorization transaction: a late
        # receipt or an in-flight admitted request must remain reconcilable. A
        # permanently changed frozen copy needs a new plan, not successor ticks.
        with store._db.cursor() as cur:
            store._db.execute(cur, "SELECT payload,status FROM jobs WHERE scan_id=%s AND type='publish_file'", (row['scan_id'],))
            job_states = []
            for item in store._db.fetchall(cur):
                data = json.loads(item['payload']) if isinstance(item['payload'], str) else item['payload']
                if data.get('automatic_release_id') == row['id']:
                    job_states.append(item['status'])
        paused = _permanent_delivery_pause(row, job_states)
        stalled = progress['_delivery_watch']['needs_attention']
        persistence.save(store,row,status='completed' if completed else 'failed' if terminal or expired else 'blocked' if reconnect_blocked or stalled or paused or 'blocked' in states else 'waiting',
                         progress=progress,
                         schedule=not terminal and not expired and not paused and not reconnect_blocked,
                         delay=0 if wake_requested else (STALLED_CHECK_SECONDS if stalled else 20))


def validate_publish_request(store, sid, owner, files, body):
    """Only an exact previously admitted artifact may use automatic authority."""
    row = persistence.get(store,body['automatic_release_id'],owner)
    if not row or row['scan_id'] != sid:
        raise ValueError('Automatic release authorization not found in this scan.')
    if body.get('allow_remaining_issues', False) != row['intent'].get('allow_remaining_issues', False):
        raise ValueError('Automatic release options differ from the accepted plan.')
    expected_destination = row['intent']['destination'] if row['intent']['release_parent_id'] or row['intent']['destination']['provider'] == 'local' else None
    if body.get('destination') != expected_destination or body.get('release_folder_name') != row['intent']['release_folder_name']:
        raise ValueError('Automatic release destination differs from the authorized destination.')
    for file in files:
        record = ready(store,row,file)
        frozen = row['progress'].get('files',{}).get(file,{}).get('artifact_digest')
        if not frozen or record['corrected_sha256'] != frozen or body.get('expected_artifacts',{}).get(file) != frozen:
            raise ValueError('Automatic release requires the exact admitted verified artifact.')
    return row

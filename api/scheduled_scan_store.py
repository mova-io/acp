"""Bounded scheduled execution on the existing occurrence and revision-fenced job queue."""
from __future__ import annotations
import copy
import hashlib
import json
import math
from datetime import datetime, timedelta, timezone

SCHEMA = [f'ALTER TABLE schedule_occurrences ADD COLUMN IF NOT EXISTS {name} {kind}'
          for name, kind in [('execution_context', 'TEXT'), ('execution_revision', 'INT'),
                             ('scan_id', 'TEXT'), ('execution_deadline', 'TEXT'),
                             ('execution_failures', 'INT'), ('execution_job_id', 'TEXT')]]
SCHEMA.append('ALTER TABLE jobs ADD COLUMN IF NOT EXISTS scheduled_execution_binding TEXT')
MIN_WAIT_SECONDS = 30
DEFAULT_TIMEOUT_SECONDS = 21600
MAX_FAILURES = 3
TERMINAL = {'succeeded', 'failed', 'skipped', 'cancelled'}


def _instant(value=None):
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc)
    return datetime.fromisoformat(str(value).replace('Z', '+00:00')).astimezone(timezone.utc) if value else datetime.now(timezone.utc)


def get(store, owner, occurrence_key, *, lock=False):
    with store._db.cursor() as cur:
        suffix = ' FOR UPDATE' if lock and store._db.supports_for_update else ''
        store._db.execute(cur, 'SELECT * FROM schedule_occurrences WHERE owner_email=%s AND occurrence_key=%s' + suffix,
                          (str(owner).strip().lower(), occurrence_key))
        row = store._db.fetchone(cur)
    if row and row.get('execution_context'):
        row['execution_context'] = json.loads(row['execution_context'])
    return row


def _held(store, job):
    with store._db.cursor() as cur:
        suffix = ' FOR UPDATE' if store._db.supports_for_update else ''
        store._db.execute(cur, 'SELECT * FROM jobs WHERE id=%s' + suffix, (job['id'],))
        actual = store._db.fetchone(cur)
    if (not actual or actual['status'] != 'running' or actual['locked_by'] != job.get('locked_by')
            or actual['attempts'] != job.get('attempts')):
        raise ValueError('Scheduled execution claim changed')
    if actual.get('cancel_requested_at'):
        from worker import JobCancelledError
        raise JobCancelledError('Scheduled execution cancellation requested')
    actual['payload'] = json.loads(actual['payload']) if isinstance(actual['payload'], str) else actual['payload']
    return actual


def accept(store, payload, job, cfg, *, now=None, timeout_seconds=DEFAULT_TIMEOUT_SECONDS):
    instant = _instant(now or store._now())
    seconds = int(timeout_seconds)
    if not 300 <= seconds <= 86400:
        raise ValueError('Scheduled execution timeout must be between five minutes and twenty-four hours')
    owner = str(cfg.get('owner_email') or '').strip().lower()
    key = payload.get('occurrence_key')
    if not owner or not key or (payload.get('owner_email') and str(payload['owner_email']).strip().lower() != owner):
        raise ValueError('Scheduled occurrence ownership is required')
    with store.transaction():
        held = _held(store, job)
        if (held['type'] != 'scheduled_sweep' or held['payload'].get('occurrence_key') != key
                or (held.get('scheduled_owner') and held['scheduled_owner'] != owner
                    and not (not payload.get('owner_email') and held['scheduled_owner'] == '__singleton__'))):
            raise ValueError('Scheduled acceptance does not match the elected occurrence')
        row = get(store, owner, key, lock=True)
        if row and row.get('execution_context'):
            return row  # Accepted source, deadline and policy are immutable on retries.
        store.begin_schedule_occurrence(owner, key, payload.get('scheduled_for') or instant.isoformat(), instant.isoformat())
        context = {'owner_email': owner, 'source': str(cfg.get('source') or 'drive').strip().lower(),
                   'source_scope': copy.deepcopy(cfg.get('source_scope') or {}), 'config': copy.deepcopy(cfg),
                   'auth_mode': 'scheduled_adc' if cfg.get('source', 'drive') == 'drive' else 'scheduled_app',
                   'phase': 'discover', 'wake_count': 0, 'wake_limit': math.ceil(seconds / MIN_WAIT_SECONDS) + 2,
                   'failure_claims': [], 'initial_job_id': held['id'], 'legacy_singleton': not bool(payload.get('owner_email'))}
        from scheduled_scan_execution import _inputs
        context['inputs'] = _inputs(store, context)
        scan_id = 'scheduled-' + hashlib.sha256((owner + '\0' + key).encode()).hexdigest()[:32]
        deadline = (instant + timedelta(seconds=seconds)).isoformat()
        with store._db.cursor() as cur:
            store._db.execute(cur,
                'UPDATE schedule_occurrences SET execution_context=%s,execution_revision=0,scan_id=%s,'
                'execution_deadline=%s,execution_failures=0,execution_job_id=%s '
                'WHERE owner_email=%s AND occurrence_key=%s AND execution_context IS NULL',
                (json.dumps(context), scan_id, deadline, held['id'], owner, key))
            if cur.rowcount != 1:
                raise ValueError('Scheduled occurrence was already accepted')
            store._db.execute(cur, 'UPDATE jobs SET scan_id=%s,scheduled_owner=%s WHERE id=%s AND status=\'running\' AND locked_by=%s AND attempts=%s',
                              (scan_id, '__singleton__' if context['legacy_singleton'] else owner, held['id'], held['locked_by'], held['attempts']))
        return get(store, owner, key)


def _current(store, row, job):
    held = _held(store, job)
    actual = get(store, row['owner_email'], row['occurrence_key'], lock=True)
    if (not actual or actual.get('execution_revision') != row.get('execution_revision')
            or actual.get('execution_job_id') != held['id'] or actual.get('result') in TERMINAL):
        raise ValueError('Scheduled occurrence revision changed')
    return held, actual


def handoff(store, row, job, *, phase, now=None, updates=None):
    instant = _instant(now or store._now())
    with store.transaction():
        held, actual = _current(store, row, job)
        context = copy.deepcopy(actual['execution_context'])
        if instant >= _instant(actual['execution_deadline']) or context['wake_count'] >= context['wake_limit']:
            raise TimeoutError('Scheduled execution deadline reached')
        if int(actual['execution_failures'] or 0) >= MAX_FAILURES:
            raise RuntimeError('Scheduled execution failure budget exhausted')
        if set(updates or {}) - {'discover_job_id', 'assess_batch_id', 'snapshot_id', 'input_manifest_id', 'delta_plan'}:
            raise ValueError('Scheduled execution updates cannot change accepted authority')
        context.update(updates or {})
        context['phase'] = phase
        context['wake_count'] += 1
        revision = actual['execution_revision'] + 1
        next_id = 'sweep-tick-' + hashlib.sha256((actual['scan_id'] + ':' + str(revision)).encode()).hexdigest()[:32]
        with store._db.cursor() as cur:
            # Close the old owner token and insert the successor in the SAME transaction.
            store._db.execute(cur, 'UPDATE jobs SET status=\'done\',updated_at=%s,last_error=NULL '
                              'WHERE id=%s AND status=\'running\' AND locked_by=%s AND attempts=%s',
                              (instant.isoformat(), held['id'], held['locked_by'], held['attempts']))
            if cur.rowcount != 1:
                raise ValueError('Scheduled execution claim changed')
            store._db.execute(cur, 'UPDATE schedule_occurrences SET execution_context=%s,execution_revision=%s,execution_job_id=%s '
                              'WHERE owner_email=%s AND occurrence_key=%s AND execution_revision=%s',
                              (json.dumps(context), revision, next_id, actual['owner_email'], actual['occurrence_key'], actual['execution_revision']))
            if cur.rowcount != 1:
                raise ValueError('Scheduled occurrence revision changed')
            from store import job_priority
            payload = {'owner_email': actual['owner_email'], 'occurrence_key': actual['occurrence_key'], 'execution_revision': revision}
            store._db.execute(cur, 'INSERT INTO jobs(id,type,payload,status,priority,attempts,max_attempts,run_after,created_at,updated_at,scheduled_owner,scan_id) '
                              'VALUES(%s,\'scheduled_sweep\',%s,\'queued\',%s,0,3,%s,%s,%s,%s,%s)',
                              (next_id, json.dumps(payload), job_priority('scheduled_sweep'),
                               (instant + timedelta(seconds=MIN_WAIT_SECONDS)).isoformat(), instant.isoformat(), instant.isoformat(),
                               held.get('scheduled_owner'), actual['scan_id']))
        return get(store, actual['owner_email'], actual['occurrence_key'])


def record_failure(store, row, job, *, now=None):
    with store.transaction():
        held, actual = _current(store, row, job)
        context = copy.deepcopy(actual['execution_context'])
        claim = [held['id'], held['attempts']]
        if claim in context['failure_claims']:
            return actual
        context['failure_claims'].append(claim)
        with store._db.cursor() as cur:
            store._db.execute(cur, 'UPDATE schedule_occurrences SET execution_failures=execution_failures+1,execution_context=%s '
                              'WHERE owner_email=%s AND occurrence_key=%s AND execution_revision=%s',
                              (json.dumps(context), actual['owner_email'], actual['occurrence_key'], actual['execution_revision']))
        return get(store, actual['owner_email'], actual['occurrence_key'])


def source_context(store, scan_id, job, *, provider, owner, item=None):
    """Only server acceptance, an owned current claim and frozen inventory grant source auth."""
    from lifecycle_identity import source_identity
    with store.transaction():
        with store._db.cursor() as cur:
            store._db.execute(cur, 'SELECT owner_email,occurrence_key FROM schedule_occurrences WHERE scan_id=%s AND execution_context IS NOT NULL', (scan_id,))
            reference = store._db.fetchone(cur)
        if not reference:
            return None  # Ordinary scans never gain ADC authority from a payload flag.
        held = _held(store, job)
        if held.get('scan_id') != scan_id:
            raise ValueError('Scheduled source claim does not match the scan')
        row = get(store, reference['owner_email'], reference['occurrence_key'])
        context = row['execution_context']
        kind = held['type']
        if kind == 'scan_discover' and held['id'] != context.get('discover_job_id'):
            return None
        if kind == 'scan_assess' and (not context.get('assess_batch_id') or held.get('batch_id') != context['assess_batch_id']):
            return None
        if kind in ('scan_file', 'scan_batch'):
            raw_binding = held.get('scheduled_execution_binding')
            if not raw_binding:
                return None
            binding = json.loads(raw_binding)
            if (binding.get('owner') != row['owner_email'] or binding.get('occurrence') != row['occurrence_key']
                    or binding.get('scan_id') != scan_id or binding.get('batch_id') != context.get('assess_batch_id')
                    or binding.get('snapshot_id') != context.get('snapshot_id')
                    or binding.get('authority_hash') != _authority_hash(store, row)
                    or binding.get('payload_hash') != store.canonical_request_fingerprint(held['payload'])):
                raise ValueError('Scheduled descendant source binding changed')
        current_tick = store.get_job(row['execution_job_id'])
        if not current_tick or current_tick['status'] not in ('queued', 'running') or current_tick.get('cancel_requested_at'):
            raise ValueError('Scheduled occurrence has no active execution claim')
        if _instant(store._now()) >= _instant(row['execution_deadline']):
            raise ValueError('Scheduled source authorization expired')
        if (row.get('result') in TERMINAL or context['owner_email'] != str(owner or '').strip().lower()
                or context['source'] != str(provider or '').strip().lower() or held.get('scan_id') != scan_id):
            raise ValueError('Scheduled source authority does not match the owned scan')
        with store._db.cursor() as cur:
            store._db.execute(cur, 'SELECT owner_email,source FROM scan_runs WHERE id=%s', (scan_id,))
            root = store._db.fetchone(cur)
        if not root and held['type'] != 'scheduled_sweep':
            raise ValueError('Scheduled root scan is absent')
        if root and (str(root['owner_email'] or '').strip().lower() != context['owner_email']
                     or str(root['source'] or '').strip().lower() != context['source']):
            raise ValueError('Scheduled root scan ownership changed')
        if held['type'] == 'scheduled_sweep':
            if row['execution_job_id'] != held['id'] or item is not None:
                raise ValueError('Scheduled discovery claim changed')
            return context
        if held['type'] == 'scan_assess':
            if (held['payload'].get('scan_id') != scan_id
                    or held['payload'].get('source') != context['source']
                    or str(held['payload'].get('user') or '').strip().lower() != context['owner_email']):
                raise ValueError('Scheduled assessment scope changed')
            return context
        if held['type'] == 'scan_discover':
            if (not root or held['payload'].get('source') != context['source']
                    or str(held['payload'].get('user') or '').strip().lower() != context['owner_email']
                    or held['payload'].get('folder') != (None if context['source_scope'].get('include_ids') else context['config'].get('scheduled_source_folder'))
                    or list(held['payload'].get('folders') or []) != list(context['source_scope'].get('include_ids') or [])
                    or list(held['payload'].get('exclude_folders') or []) != list(context['source_scope'].get('exclude_ids') or [])):
                raise ValueError('Scheduled discovery scope changed')
            return context
        if held['type'] not in ('scan_file', 'scan_batch') or held['payload'].get('scan_id') != scan_id:
            raise ValueError('Scheduled content claim is not a descendant')
        payload_items = held['payload'].get('items') if held['type'] == 'scan_batch' else [held['payload']]
        selected = [item] if item is not None else payload_items
        for candidate in selected:
            identity = source_identity(owner, provider, candidate)
            if not identity or not any(source_identity(owner, provider, frozen) == identity for frozen in payload_items):
                raise ValueError('Scheduled content identity is not in the frozen job')
            with store._db.cursor() as cur:
                store._db.execute(cur, 'SELECT * FROM scan_inventory WHERE scan_id=%s AND file=%s', (scan_id, candidate['file']))
                current = store._db.fetchone(cur)
            if not current or source_identity(owner, provider, current) != identity:
                raise ValueError('Scheduled content binding changed')
        return context


def finish(store, row, job, *, result, now=None, error=None, changed=None):
    """Close the logical occurrence, owner token and once-only notification atomically."""
    instant = _instant(now or store._now())
    if result not in TERMINAL:
        raise ValueError('Scheduled occurrence requires a terminal outcome')
    with store.transaction():
        held, actual = _current(store, row, job)
        store.complete_schedule_occurrence(actual['owner_email'], actual['occurrence_key'], result=result,
                                           completed_at=instant.isoformat(), changed=changed, error=error)
        if result != 'cancelled':
            store.record_sweep_outcome(ok=result != 'failed', when=instant.isoformat(), source=actual['execution_context']['source'],
                                       scan_id=actual['scan_id'], error=error, skipped=result == 'skipped',
                                       owner=None if actual['execution_context']['legacy_singleton'] else actual['owner_email'])
        if not actual['execution_context']['legacy_singleton']:
            store.emit_schedule_notification_for_occurrence(actual['owner_email'], actual['occurrence_key'], result,
                changed=bool(changed), message=error or f"Scan {actual['scan_id']} {result}",
                policy=actual['execution_context']['config'].get('notification_policy'))
        with store._db.cursor() as cur:
            store._db.execute(cur, 'UPDATE jobs SET status=\'done\',updated_at=%s WHERE id=%s '
                              'AND status=\'running\' AND locked_by=%s AND attempts=%s',
                              (instant.isoformat(), held['id'], held['locked_by'], held['attempts']))
            if cur.rowcount != 1:
                raise ValueError('Scheduled execution claim changed')
        return get(store, actual['owner_email'], actual['occurrence_key'])


def cancel_for_scan(store, scan_id):
    """An authorized scan cancellation also stops its waiting logical occurrence."""
    with store._db.cursor() as cur:
        store._db.execute(cur, 'SELECT owner_email,occurrence_key FROM schedule_occurrences WHERE scan_id=%s '
                          'AND execution_context IS NOT NULL AND completed_at IS NULL', (scan_id,))
        references = store._db.fetchall(cur)
    for reference in references:
        row = get(store, reference['owner_email'], reference['occurrence_key'], lock=True)
        if not row or row.get('completed_at'):
            continue
        store.complete_schedule_occurrence(row['owner_email'], row['occurrence_key'], result='cancelled', completed_at=store._now())
        if not row['execution_context']['legacy_singleton']:
            store.emit_schedule_notification_for_occurrence(row['owner_email'], row['occurrence_key'], 'cancelled',
                message='Scheduled scan cancelled', policy=row['execution_context']['config'].get('notification_policy'))


def _authority_hash(store, row):
    context = row['execution_context']
    return store.canonical_request_fingerprint({
        'owner': row['owner_email'], 'occurrence': row['occurrence_key'], 'scan_id': row['scan_id'],
        'accepted': {key: context[key] for key in ('owner_email', 'source', 'source_scope', 'config', 'auth_mode', 'initial_job_id', 'legacy_singleton')},
        'inputs': store.get_scan_inputs(row['scan_id'])})


def enqueue_content(store, parent, kind, payload):
    return enqueue_contents(store, parent, [(kind, payload)])['job_ids'][0]


def enqueue_contents(store, parent, admissions):
    """Admit a complete frozen assessment fan-out once, together with server lineage."""
    from lifecycle_identity import source_identity
    if not admissions or any(kind not in ('scan_file', 'scan_batch') for kind, _ in admissions):
        raise ValueError('Scheduled content admission requires analysis jobs')
    with store.transaction():
        held = _held(store, parent)
        if held['type'] != 'scan_assess':
            raise ValueError('Scheduled content admission requires an assessment claim')
        first = admissions[0][1]
        scan_id, owner, provider = first['scan_id'], first.get('user'), first.get('source')
        context = source_context(store, scan_id, held, provider=provider, owner=owner)
        if not context:
            raise ValueError('Assessment does not own accepted scheduled source authority')
        row = get(store, owner, _occurrence_key_for_scan(store, scan_id), lock=True)
        authority = _authority_hash(store, row)
        with store._db.cursor() as cur:
            store._db.execute(cur, "SELECT id,type,payload,scheduled_execution_binding FROM jobs WHERE scan_id=%s "
                              "AND type IN ('scan_file','scan_batch') AND scheduled_execution_binding IS NOT NULL", (scan_id,))
            prior = store._db.fetchall(cur)
        reused, file_count = [], 0
        for previous in prior:
            binding = json.loads(previous['scheduled_execution_binding'])
            if binding['batch_id'] != held['batch_id']:
                continue
            if binding['authority_hash'] != authority:
                raise ValueError('Accepted scheduled content authority changed')
            frozen = json.loads(previous['payload']) if isinstance(previous['payload'], str) else previous['payload']
            if binding.get('payload_hash') != store.canonical_request_fingerprint(frozen):
                raise ValueError('Accepted scheduled content payload changed')
            reused.append(previous['id'])
            file_count += len(frozen['items']) if previous['type'] == 'scan_batch' else 1
        if reused:
            store.set_scan_files(scan_id, file_count)
            return {'job_ids': reused, 'reused': True, 'file_count': file_count}
        inventory = {item['file']: item for item in store.list_inventory(scan_id)}
        job_ids = []
        for kind, payload in admissions:
            if payload.get('scan_id') != scan_id or payload.get('user') != owner or payload.get('source') != provider:
                raise ValueError('Scheduled fan-out cannot widen the accepted source owner')
            candidates = payload['items'] if kind == 'scan_batch' else [payload]
            for candidate in candidates:
                current = inventory.get(candidate['file'])
                identity = source_identity(owner, provider, candidate)
                if provider == 'sharepoint' and candidate.get('drive_id') != context['config'].get('scheduled_drive_id'):
                    raise ValueError('Scheduled SharePoint content is outside the accepted sync library')
                if not current or not identity or source_identity(owner, provider, current) != identity:
                    raise ValueError('Scheduled admission source identity is incomplete or changed')
            job_id = store.enqueue_job(kind, payload, scan_id=scan_id)
            actual = store.get_job(job_id)
            binding = {'owner': row['owner_email'], 'occurrence': row['occurrence_key'], 'scan_id': scan_id,
                       'batch_id': held['batch_id'], 'parent_job_id': held['id'], 'parent_attempt': held['attempts'],
                       'snapshot_id': context.get('snapshot_id'), 'authority_hash': authority,
                       'payload_hash': store.canonical_request_fingerprint(actual['payload'])}
            with store._db.cursor() as cur:
                store._db.execute(cur, 'UPDATE jobs SET scheduled_execution_binding=%s WHERE id=%s AND status=\'queued\' '
                                  'AND scheduled_execution_binding IS NULL', (json.dumps(binding), job_id))
                if cur.rowcount != 1:
                    raise ValueError('Scheduled descendant admission changed')
            job_ids.append(job_id)
            file_count += len(candidates)
        store.set_scan_files(scan_id, file_count)
        return {'job_ids': job_ids, 'reused': False, 'file_count': file_count}


def _occurrence_key_for_scan(store, scan_id):
    with store._db.cursor() as cur:
        store._db.execute(cur, 'SELECT occurrence_key FROM schedule_occurrences WHERE scan_id=%s', (scan_id,))
        row = store._db.fetchone(cur)
    if not row:
        raise ValueError('Scheduled occurrence no longer exists')
    return row['occurrence_key']


def cancelled_tick(store, job):
    """Close accepted work when its current durable tick acknowledges cancellation."""
    if not job or job.get('type') != 'scheduled_sweep' or not job.get('scan_id'):
        return
    with store.transaction():
        with store._db.cursor() as cur:
            store._db.execute(cur, 'SELECT owner_email,occurrence_key FROM schedule_occurrences '
                              'WHERE scan_id=%s AND execution_job_id=%s AND completed_at IS NULL', (job['scan_id'], job['id']))
            reference = store._db.fetchone(cur)
        if not reference:
            return
        store.cancel_scan(job['scan_id'], owner=reference['owner_email'])
        cancel_for_scan(store, job['scan_id'])


def exhausted_tick(store, job, *, worker_id, attempt):
    """Close an accepted occurrence if its elected tick exhausts ordinary retries.

    This also covers failures before the handler could persist its logical counter.
    The same transaction fences the final claim, occurrence and scan cancellation.
    """
    with store.transaction():
        with store._db.cursor() as cur:
            store._db.execute(cur, 'SELECT owner_email,occurrence_key FROM schedule_occurrences '
                              'WHERE execution_job_id=%s AND completed_at IS NULL', (job['id'],))
            reference = store._db.fetchone(cur)
        if not reference:
            return False
        row = get(store, reference['owner_email'], reference['occurrence_key'], lock=True)
        claim = {**job, 'locked_by': worker_id, 'attempts': attempt}
        finish(store, row, claim, result='failed', error='scheduled_tick_attempts_exhausted')
        with store._db.cursor() as cur:
            store._db.execute(cur, "UPDATE jobs SET status='dead',last_error=%s WHERE id=%s "
                              "AND status='done' AND locked_by=%s AND attempts=%s",
                              ('scheduled_tick_attempts_exhausted', job['id'], worker_id, attempt))
            if cur.rowcount != 1:
                raise ValueError('Scheduled execution claim changed')
        store.cancel_scan(row['scan_id'], owner=row['owner_email'])
        return True

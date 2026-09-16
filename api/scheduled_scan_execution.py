"""Stage a scheduled occurrence without occupying a worker while child jobs advance."""
from __future__ import annotations
import os
import copy
from datetime import datetime, timezone
import scheduled_scan_store as execution
from worker import JobCancelledError, JobDrainingError


def context_for_job(store, scan_id, job, *, source, user, item=None):
    if not getattr(store, '_db', None) or not job or not job.get('id'):
        return None
    return execution.source_context(store, scan_id, job, provider=source, owner=user, item=item)


def _inputs(store, context):
    from second_opinion_policy import load_policy
    scope = context['source_scope']
    return {'source': context['source'], 'folder_ids': list(scope.get('include_ids') or []),
            'exclude_folder_ids': list(scope.get('exclude_ids') or []), 'actor': context['owner_email'],
            'connection_ref': 'scheduled:' + context['source'] + ':' + context['owner_email'],
            'scan_options': {'ai': store.get_ai_enabled(), 'pii': False, 'batch': False,
                             'incremental': True, 'fanout': True, 'include_subfolders': True,
                             'exclude_remediated': False},
            'feature_flags': {'ai_platform_enabled': store.get_ai_enabled(), 'defer_analysis_to_assess': True,
                              'second_opinion_policy': load_policy(store)},
            'provider_config': [{k: v for k, v in row.items() if k != 'key_secret_ref'}
                                for row in store.list_ai_provider_configs() if row.get('enabled')],
            'lifecycle_rules': [row for row in store.list_disposition_policies(owner=context['owner_email']) if row.get('enabled')],
            'app_version': os.environ.get('ACP_APP_VERSION')}


def _changed(context):
    delta = context.get('delta_plan')
    return bool(delta.get('changed') or delta.get('removed_ids')) if delta is not None else None


def _discovery_plan(core, context):
    source, scope = context['source'], context['source_scope']
    narrowed = bool(scope.get('include_ids') or scope.get('exclude_ids'))
    if source == 'drive' and not narrowed:
        return core._drive_sync_plan(context['owner_email'], 'drive' if context['legacy_singleton'] else 'drive:scheduled:' + context['owner_email'])
    if source == 'sharepoint' and not narrowed:
        import sp_sync
        if sp_sync.sp_sync_configured():
            return core._sp_sync_plan(context['owner_email'], _scheduled_app_token(context))
    return False, None


def _stop(store, row, job, *, result, error=None):
    # Closing the occurrence first preserves its once-only result when scan cancellation
    # removes queued waiting ticks and child work.
    with store.transaction():
        finished = execution.finish(store, row, job, result=result, error=error, changed=_changed(row['execution_context']))
        if result in ('failed', 'cancelled'):
            store.cancel_scan(row['scan_id'], owner=row['owner_email'])
        return finished


def run_tick(payload, job):
    import core
    from worker import check_cancel
    check_cancel()
    store = core.get_store()
    owner = payload.get('owner_email')
    key = payload.get('occurrence_key')
    if not key:
        raise ValueError('Scheduled tick requires an elected occurrence key')
    existing = execution.get(store, owner, key) if owner else None
    if not owner:
        with store._db.cursor() as cur:
            store._db.execute(cur, 'SELECT owner_email,occurrence_key FROM schedule_occurrences WHERE execution_job_id=%s', (job['id'],))
            accepted = store._db.fetchone(cur)
        if accepted:
            existing = execution.get(store, accepted['owner_email'], accepted['occurrence_key'])
    if existing and existing.get('execution_context'):
        row = existing
    else:
        cfg = store.get_user_scan_schedule(owner) if owner else store.get_schedule()
        owner = cfg.get('owner_email')
        if not cfg.get('enabled'):
            core._schedule_lifecycle_complete(store, {**payload, 'owner_email': owner}, result='skipped', error='schedule_disabled')
            return
        decision = core._scheduled_scan_admission(payload)
        if (not decision.get('admit') and decision.get('reason') == 'owner_concurrency_limit'
                and int(decision.get('owner_active') or 0) <= 1):
            decision = {**decision, 'admit': True}
        if not decision.get('admit'):
            if decision.get('terminal'):
                core._schedule_lifecycle_complete(store, payload, result='skipped', error=decision.get('reason'))
                return
            if decision.get('run_after'):
                store.defer_scheduled_sweep(owner, key, decision['run_after'], decision.get('reason') or 'queue_policy', payload.get('scheduled_for'))
            raise RuntimeError('scheduled scan deferred: ' + str(decision.get('reason')))
        if cfg.get('source') == 'sharepoint':
            import sp_sync
            if sp_sync.sp_sync_configured():
                cfg = dict(cfg, scheduled_drive_id=sp_sync.sync_drive_id(),
                           scheduled_source_folder=sp_sync.sync_drive_id() + '/root',
                           scheduled_app_identity=store.canonical_request_fingerprint(
                               [sp_sync._cfg('ACP_SP_SYNC_TENANT_ID'), sp_sync._cfg('ACP_SP_SYNC_CLIENT_ID')]))
        row = execution.accept(store, payload, job, cfg,
            timeout_seconds=int(os.environ.get('ACP_SCHEDULED_EXECUTION_TIMEOUT_S', str(execution.DEFAULT_TIMEOUT_SECONDS))))
    # Validate the exact current elected claim even on accepted retries, before new work.
    with store.transaction():
        execution._current(store, row, job)
    context, scan_id = row['execution_context'], row['scan_id']
    now = execution._instant(store._now())
    if now >= execution._instant(row['execution_deadline']) or context['wake_count'] >= context['wake_limit']:
        return _stop(store, row, job, result='failed', error='scheduled_execution_deadline')
    run = ((store.get_scan(scan_id, owner=row['owner_email']) or {}).get('run') or {})
    if run.get('status') in ('cancelled', 'superseded'):
        return _stop(store, row, job, result='cancelled')
    if run.get('status') == 'failed':
        return _stop(store, row, job, result='failed', error='scheduled_child_work_failed')
    if run.get('status') == 'done':
        with store._db.cursor() as cur:
            store._db.execute(cur, "SELECT COUNT(*) AS n FROM jobs WHERE scan_id=%s AND type<>'scheduled_sweep' "
                              "AND status='dead' AND cancel_requested_at IS NULL", (scan_id,))
            dead = store._db.fetchone(cur)['n']
        return _stop(store, row, job, result='failed' if dead else 'succeeded',
                     error='scheduled_child_work_failed' if dead else None)
    try:
        phase = context['phase']
        if phase == 'discover':
            # Reject invalid frozen library scope within the occurrence's cumulative
            # failure budget, before obtaining any provider token or listing metadata.
            if context['source'] == 'sharepoint' and context['config'].get('scheduled_drive_id'):
                for location in [*context['source_scope'].get('include_ids', []),
                                 *context['source_scope'].get('exclude_ids', [])]:
                    if '/' not in location or location.split('/', 1)[0] != context['config']['scheduled_drive_id']:
                        raise ValueError('Scheduled SharePoint scope is outside the accepted sync library')
            check_cancel()
            skip, delta = _discovery_plan(core, context)
            if skip:
                return _stop(store, row, job, result='skipped')
            inputs = copy.deepcopy(context['inputs'])
            scope = context['source_scope']
            discovery = {'scan_id': scan_id, 'source': context['source'], 'user': row['owner_email'],
                         'ai': inputs['scan_options']['ai'], 'pii': False, 'incremental': True,
                         'folders': list(scope.get('include_ids') or []), 'exclude_folders': list(scope.get('exclude_ids') or []),
                         'include_subfolders': True, 'folder': None if scope.get('include_ids') else context['config'].get('scheduled_source_folder')}
            with store.transaction():
                execution._current(store, row, job)
                _, discovery_job = store.enqueue_scan(scan_id, context['source'], row['owner_email'], 'scan_discover', discovery,
                                                       idempotency_key='scheduled:' + key, inputs=inputs, max_attempts=3)
                return execution.handoff(store, row, job, phase='awaiting_discover',
                    updates={'discover_job_id': discovery_job, 'delta_plan': delta})
        if phase == 'awaiting_discover':
            discovery_job = store.get_job(context['discover_job_id'])
            if discovery_job['status'] in ('dead', 'cancelled'):
                return _stop(store, row, job, result='failed', error='scheduled_discovery_failed')
            if discovery_job['status'] != 'done' or run.get('status') != 'discovered':
                return execution.handoff(store, row, job, phase=phase)
            work = store.stage_work_item_for_job(discovery_job['id'])
            stage = store.get_stage_execution(work['execution_id']) if work else None
            manifest = (stage or {}).get('output_manifest_id')
            snapshot = manifest or store.stage_snapshot_id(scan_id)
            assessment = {'scan_id': scan_id, 'source': context['source'], 'user': row['owner_email'],
                          'include_lifecycle_flagged': False}
            with store.transaction():
                execution._current(store, row, job)
                batch = store.enqueue_stage_batch(scan_id, 'assess', 'scan_assess', [assessment], snapshot_id=snapshot,
                    input_manifest_id=manifest, request_fingerprint=store.canonical_request_fingerprint(assessment))
                return execution.handoff(store, row, job, phase='awaiting_assess',
                    updates={'assess_batch_id': batch['batch_id'], 'snapshot_id': snapshot, 'input_manifest_id': manifest})
        if phase == 'awaiting_assess':
            with store._db.cursor() as cur:
                store._db.execute(cur, "SELECT COUNT(*) AS n FROM jobs WHERE scan_id=%s AND type<>'scheduled_sweep' AND status='dead'", (scan_id,))
                dead = store._db.fetchone(cur)['n']
                store._db.execute(cur, "SELECT COUNT(*) AS n FROM jobs WHERE scan_id=%s AND type<>'scheduled_sweep' AND status IN ('queued','running')", (scan_id,))
                active = store._db.fetchone(cur)['n']
            if dead and not active:
                return _stop(store, row, job, result='failed', error='scheduled_assessment_failed')
            return execution.handoff(store, row, job, phase=phase)
        raise ValueError('Scheduled execution phase is invalid')
    except TimeoutError:
        return _stop(store, row, job, result='failed', error='scheduled_execution_deadline')
    except JobDrainingError:
        raise  # Existing deployment handoff preserves accepted context without a failure.
    except JobCancelledError:
        raise  # Authorized cancellation closes the occurrence via the scan cancellation hook.
    except Exception:
        failed = execution.record_failure(store, row, job)
        if failed['execution_failures'] >= execution.MAX_FAILURES:
            return _stop(store, failed, job, result='failed', error='scheduled_execution_failure_budget')
        raise  # The SAME ordinary job attempt spends its ordinary retry budget.


def services_for_job(store, scan_id, job, *, source, user, item=None):
    """Refresh only a server-accepted scheduled source; never elevate an ordinary payload."""
    context = context_for_job(store, scan_id, job, source=source, user=user, item=item)
    if not context:
        return None
    if source == 'drive':
        from scanner import _drive_service, drive_account_id
        svc = _drive_service(None)
        if job['type'] in ('scan_file', 'scan_batch'):
            held_payload = store.get_job(job['id'])['payload']
            selected = [item] if item is not None else held_payload.get('items', [held_payload])
            expected = {row['drive_account_id'] for row in selected}
            if len(expected) != 1 or drive_account_id(svc) not in expected:
                raise ValueError('Scheduled Drive account changed before content access')
        return svc, None, context
    if source == 'sharepoint':
        return None, _scheduled_app_token(context), context
    if source == 'local':
        return None, None, context
    raise ValueError('Scheduled source authentication is unsupported')


def _scheduled_app_token(context):
    import sp_sync
    from store import Store
    expected = context['config'].get('scheduled_drive_id')
    identity = Store.canonical_request_fingerprint(
        [sp_sync._cfg('ACP_SP_SYNC_TENANT_ID'), sp_sync._cfg('ACP_SP_SYNC_CLIENT_ID')])
    if (not expected or not sp_sync.sp_sync_configured() or sp_sync.sync_drive_id() != expected
            or identity != context['config'].get('scheduled_app_identity')):
        raise ValueError('Scheduled SharePoint application/library authorization changed')
    return sp_sync.app_token()

"""Project admitted automatic checks into the inbox without changing approvals."""
import json
import hashlib


def _key(owner, run_id, item_id):
    return 'ai-item-disposition:' + hashlib.sha256(json.dumps([owner, run_id, item_id]).encode()).hexdigest()


from apply_outcome import COULD_NOT_VERIFY, NOTHING_WRITTEN, STILL_FAILING
from release_continuation import PDF_STRUCTURE_MANUAL, PDF_STRUCTURE_REVIEW
from sensory_rewrite_output import NON_ANSWER_REASON
from fix_approval_policy import REVIEW_REQUIRED

HUMAN_REASONS = {REVIEW_REQUIRED, 'Manual work or no supported proposal writer', PDF_STRUCTURE_MANUAL, PDF_STRUCTURE_REVIEW, NON_ANSWER_REASON, 'This change requires individual review', 'The draft contradicts visible image evidence; individual review is required', 'Proposal requires individual judgment or has no exact AI provenance'}


def record(store, owner, sid, run_id, source_revision, item, state, reason):
    """Display evidence only. Never authorizes a decision or a write."""
    value = dict(state=state, responsibility='human' if reason in HUMAN_REASONS else 'check' if state in {'review_required','blocked'} else 'acp', reason=reason, owner='ACP' if state in {'checking', 'queued', 'applying', 'verifying'} else 'You', scan_id=sid, run_id=run_id, source_revision=source_revision, proposal_snapshot_ids=item.get('proposal_snapshot_ids') or [])
    store.set_setting(_key(owner, run_id, item['id']), json.dumps(value, sort_keys=True))
    return value



def stalled_reason(item):
    """Why an approved suggestion is sitting still, in words its reviewer can act on.

    SHOWN TO THE USER VERBATIM — the recovery pane renders it as written, so it is product
    copy, not a log line. It prefers the row's own `apply_outcome`, the refusal reason
    `apply_outcome.annotate_apply_outcomes` already parsed onto it, because the previous
    wording ("No active writer job is recorded") was false for a job that ran and refused,
    and left re-approving as the only move it suggested. The fallback now claims only that
    none is running NOW — true whether one finished or none was ever made.

    Nothing here calls the value AI-generated: an image-of-text transcript comes from OCR.
    """
    if item.get('approval_recheck_required'):
        return ('The document or suggestion changed after approval. Review the current version '
                'before saving; the earlier approval remains in the audit history.')
    outcome = item.get('apply_outcome') or {}
    reason = str(outcome.get('reason') or '').strip()
    kept = ('Your approval and approved text are kept — retry saving it. '
            'Retry checks the document and suggestion against the approved versions. '
            'Matching records need no second approval; changed or missing version records require review.')
    if outcome.get('outcome') == NOTHING_WRITTEN:
        lead = ('Saving ran and left the document unchanged.'
                if reason else
                'Saving ran and left the document unchanged, because the approved text reaches '
                'nothing this document can carry it on.')
        return ' '.join(filter(None, (lead, reason, kept)))
    if outcome.get('outcome') == COULD_NOT_VERIFY:
        return ' '.join(filter(None, (
            'Your approved text was written, but the independent check could not confirm it.',
            reason, 'The change is kept and no further approval is needed.')))
    if outcome.get('outcome') == STILL_FAILING:
        return ('Your approved text was written, but the independent check still reports this '
                'problem on the document. The change is kept and no further approval is needed; '
                'this suggestion needs a closer look rather than another approval.')
    if item.get('applied'):
        return ('Saved, but not yet independently checked. No verification is running right now; '
                'start one again when you are ready. Another approval is not needed.')
    return ('Approved, not yet saved into the document. No saving is running right now; retry '
            'saving it. ' + kept)


def annotate(store, rows, owner):
    if not owner:
        return rows
    from ai_run_approval_override import read
    from ai_standing_approval import eligible_item
    result = []
    contexts = {}
    run_settings = {}
    writer_jobs = {}
    for item in rows:
        item = {key: value for key, value in item.items() if not key.startswith('auto_approval_') and key != 'automatic_approval'}
        if item.get('status') not in {'pending', 'approved'}:
            result.append(item)
            continue
        sid = item.get('scan_id')
        if sid not in contexts:
            contexts[sid] = None
            with store._db.cursor() as cur:
                store._db.execute(cur, '''SELECT execution_id FROM stage_executions
                    WHERE scan_id=%s AND owner_email=%s AND stage='remediate'
                      AND is_current=1 AND cancel_requested_at IS NULL''', (sid, owner))
                execution = store._db.fetchone(cur)
            if execution:
                run_id = execution['execution_id']
                try:
                    setting = read(store, owner, sid, run_id)
                except ValueError:
                    setting = None
                if setting:
                    run_settings[sid] = setting
                if setting and setting['enabled']:
                    with store._db.cursor() as cur:
                        store._db.execute(cur, '''SELECT payload FROM jobs WHERE scan_id=%s
                            AND type='apply_approved_values'
                            AND status IN ('queued','running','processing','retry')''', (sid,))
                        jobs = store._db.fetchall(cur)
                    for job in jobs:
                        payload = json.loads(job['payload']) if isinstance(job['payload'], str) else job['payload']
                        if (payload.get('phase') == 'approve_current_run_ai' and
                                payload.get('owner') == owner and payload.get('run_id') == run_id and
                                payload.get('source_revision') == setting['source_revision']):
                            contexts[sid] = setting
                            break
        setting = contexts[sid]
        current = run_settings.get(sid)
        if current:
            saved = json.loads(store.get_setting(_key(owner, current['run_id'], item['id'])) or '{}')
            if (saved.get('source_revision') == current['source_revision'] and saved.get('proposal_snapshot_ids') == item.get('proposal_snapshot_ids') and saved.get('scan_id') == sid):
                if item.get('status') == 'approved':
                    if sid not in writer_jobs:
                        with store._db.cursor() as cur:
                            store._db.execute(cur, "SELECT payload,status FROM jobs WHERE scan_id=%s AND type='apply_approved_values' AND status IN ('queued','running','processing','retry')", (sid,))
                            writer_jobs[sid] = store._db.fetchall(cur)
                    active = []
                    for job in writer_jobs[sid]:
                        payload = json.loads(job['payload']) if isinstance(job['payload'], str) else job['payload']
                        intent = payload.get('standing_approval') or {}
                        if (payload.get('scan_id') == sid and payload.get('file') == item.get('file')
                                and payload.get('item_id') == str(item['id'])):
                            binding, refusal = store.approved_write_binding(item)
                            if not refusal and payload.get('approved_binding') == binding:
                                active.append(job['status'])
                            continue
                        if (intent.get('owner') == owner and intent.get('run_id') == current['run_id'] and intent.get('source_revision') == current['source_revision'] and any(expected.get('id') == item['id'] for expected in intent.get('items', []))):
                            active.append(job['status'])
                    if item.get('validated'):
                        saved = {**saved, 'state': 'verified', 'owner': 'ACP', 'reason': 'Saved changes passed independent checks.'}
                    elif active:
                        state = 'verifying' if item.get('applied') else 'applying' if any(status in {'running','processing'} for status in active) else 'queued'
                        saved = {**saved, 'state': state, 'owner': 'ACP', 'reason': 'Approved changes await independent verification.' if item.get('applied') else 'An authorized writer job is active for this suggestion.'}
                    else:
                        saved = {**saved, 'state': 'blocked', 'owner': 'ACP', 'responsibility': 'check',
                                 'reason': stalled_reason(item)}
                elif not setting and saved.get('state') in {'checking', 'queued'}:
                    saved = {**saved, 'state': 'blocked', 'owner': 'ACP', 'responsibility': 'check', 'reason': 'Automatic checks ended without admission. Review this suggestion or refresh its result.'}
                item = {**item, 'automatic_approval': saved}

        if setting and item.get('status') == 'pending' and item.get('proposals'):
            try:
                # Database/proposal checks only. Provider freshness and saved bytes are
                # checked by the queued coordinator, never by this polling read.
                eligible_item(store, owner, sid, setting['run_id'], item)
            except ValueError as exc:
                # A failed check does not mean AI has accepted this item. Only
                # explicit authoring/judgment failures assign work to a person.
                reason = str(exc)
                human = reason in HUMAN_REASONS
                item = {**item, 'automatic_approval': dict(state='blocked', responsibility='human' if human else 'check', reason=reason, scan_id=sid, run_id=setting['run_id'], source_revision=setting['source_revision'], proposal_snapshot_ids=item.get('proposal_snapshot_ids') or [])}

            else:
                item = {**item, 'auto_approval_status': 'checking',
                        'auto_approval_run_id': setting['run_id'],
                        'auto_approval_source_revision': setting['source_revision']}
        result.append(item)
    return result

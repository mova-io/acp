"""Bounded recent narration from saved events, using the stream's privacy projection."""
import json
from datetime import datetime, timezone

KINDS = (
    'remediate.ai_request_started', 'remediate.ai_request_finished',
    'remediate.accepted', 'remediate.fix_applied', 'remediate.verified',
    'remediate.verification_failed', 'remediate.delivered', 'remediate.delivery_failed',
    'remediate.review_requested', 'remediate.document_completed', 'scan.interrupted',
    'scan.retrying', 'remediate.delivery_retry_requested', 'remediate.delivery_retry_refused',
    'remediate.cancel_requested', 'remediate.paused', 'remediate.resumed',
    'remediate.vision_retry_pending', 'remediate.vision_retry_recovered', 'remediate.vision_retry_blocked',
    'remediate.vision_retry_obsolete', 'remediate.review_target_replaced',
)

# ── activity stage (contract V3) ──────────────────────────────────────────────
#
# Which part of the work an event narrates. It exists so a reader can never word an AI DRAFT as a
# document edit, or a document edit as a verification: those are three different claims about the
# saved file, and the kind names alone left the distinction to every renderer.
_STAGE_BY_KIND = {
    'remediate.accepted': 'review',
    'remediate.review_requested': 'review',
    'remediate.review_target_replaced': 'review',
    'remediate.fix_applied': 'document_write',
    'remediate.verified': 'verification',
    'remediate.verification_failed': 'verification',
    'remediate.delivered': 'delivery',
    'remediate.delivery_failed': 'delivery',
}
_STAGE_BY_PREFIX = (
    ('remediate.vision_retry_', 'draft_generation'),
    ('remediate.ai_request_', 'draft_generation'),
    ('remediate.delivery_retry_', 'delivery'),
)


def activity_stage(kind) -> str:
    """'draft_generation' | 'review' | 'document_write' | 'verification' | 'delivery' | 'run'."""
    kind = str(kind or '')
    if kind in _STAGE_BY_KIND:
        return _STAGE_BY_KIND[kind]
    for prefix, stage in _STAGE_BY_PREFIX:
        if kind.startswith(prefix):
            return stage
    return 'run'


# ── historical obsolete retries (contract V3) ─────────────────────────────────
#
# Before `remediate.vision_retry_obsolete` existed, a retry that stopped because its input had
# legitimately moved on was written as `remediate.vision_retry_blocked` with the catch-all code
# 'vision_recovery_unresolved'. The event cannot tell that apart from a real failure; its decision
# log row can, because it kept the exact exception text. Those rows are immutable, so the correction
# is made HERE, at read time, and only when the pairing between event and decision is provable.
#
# PAIRING RULE. A stored blocked event E pairs with decision row D iff all of:
#   * D.action == 'vision.recovery.blocked', D.scan_id == E.scan_id, D.file == E.document;
#   * |D.ts - E.occurred_at| <= PAIRING_WINDOW_SECONDS;
#   * D.detail.run_id == E.correlation_id, BOTH present (an unknown run is not a match — the
#     owner half of the correlation comes from the read path's get_scan(owner=...) gate);
#   * D is E's ONLY such candidate, and E is D's only such candidate among EVERY stored blocked
#     event of that file (not just this page) — so the stream, which projects one tick at a time,
#     and the paged history reach the same verdict.
# Anything else — no candidate, two candidates, unparseable time, a named reason code — is left
# exactly as recorded. An unproven re-label would be a second wrong statement, not a correction.
#
# Only these two exact reasons are remapped. The raw text never leaves this module. The projected
# detail deliberately carries NO `no_ai_request`: a decision row proves why the retry stopped, not
# whether a provider was already called before it did.
BLOCKED_KIND = 'remediate.vision_retry_blocked'
BLOCKED_ACTION = 'vision.recovery.blocked'
CATCH_ALL = 'vision_recovery_unresolved'
OBSOLETE_REASONS = {
    'The authorized run or saved input changed.': 'vision_retry_input_changed',
    'The review changed; its current decision is preserved.': 'vision_retry_review_changed',
}
PAIRING_WINDOW_SECONDS = 5.0
# A scan with more stored blocks than this for the page's files is not paired at all: a truncated
# read cannot prove uniqueness.
_PAIRING_ROW_CAP = 2000


def _ts(value):
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        except (TypeError, ValueError):
            return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _detail(raw):
    if isinstance(raw, dict):
        return raw
    try:
        value = json.loads(raw) if raw else None
    except (TypeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _in(values):
    return ','.join(['%s'] * len(values))


def _blocked_decisions(store, sid, files):
    """ONE decision_log read for the whole page."""
    with store._db.cursor() as cur:
        store._db.execute(cur,
            'SELECT ts, file, detail FROM decision_log WHERE scan_id=%s AND action=%s AND file IN ('
            + _in(files) + ') LIMIT %s', (sid, BLOCKED_ACTION, *files, _PAIRING_ROW_CAP + 1))
        return store._db.fetchall(cur)


def _blocked_events(store, sid, files):
    """Every stored blocked event of those files — the competitors for the reverse check."""
    with store._db.cursor() as cur:
        store._db.execute(cur,
            'SELECT seq, occurred_at, document, correlation_id FROM scan_events WHERE scan_id=%s '
            'AND kind=%s AND document IN (' + _in(files) + ') LIMIT %s',
            (sid, BLOCKED_KIND, *files, _PAIRING_ROW_CAP + 1))
        return store._db.fetchall(cur)


def _pairs(decision, event):
    """decision is (ts, file, run_id); event is (ts, document, correlation_id)."""
    d_ts, d_file, d_run = decision
    e_ts, e_doc, e_run = event
    if d_file != e_doc or d_ts is None or e_ts is None:
        return False
    if not d_run or not e_run or str(d_run) != str(e_run):
        return False
    return abs((d_ts - e_ts).total_seconds()) <= PAIRING_WINDOW_SECONDS


def historical_retry_projections(store, sid, events) -> dict:
    """{seq: projected detail} for the stored blocked events that were really obsolete retries.

    Read-time only; never raises (a mapping failure leaves every event as recorded)."""
    try:
        return _historical_retry_projections(store, sid, events)
    except Exception:
        return {}


def _historical_retry_projections(store, sid, events):
    targets = []
    for event in events or ():
        detail = _detail(event.get('detail'))
        if (event.get('kind') == BLOCKED_KIND and event.get('document') and event.get('seq') is not None
                and detail and detail.get('reason_code') == CATCH_ALL):
            targets.append((event, detail))
    if not targets:
        return {}
    files = sorted({str(event['document']) for event, _ in targets})
    decisions = _blocked_decisions(store, sid, files)
    if len(decisions) > _PAIRING_ROW_CAP:
        return {}
    competitors = _blocked_events(store, sid, files)
    if len(competitors) > _PAIRING_ROW_CAP:
        return {}
    decision_keys = []
    for row in decisions:
        body = _detail(row.get('detail')) or {}
        decision_keys.append(((_ts(row.get('ts')), row.get('file'), body.get('run_id')), body))
    competitor_keys = [(int(row['seq']), (_ts(row.get('occurred_at')), row.get('document'),
                                          row.get('correlation_id'))) for row in competitors]
    out = {}
    for event, detail in targets:
        key = (_ts(event.get('occurred_at')), event.get('document'), event.get('correlation_id'))
        candidates = [(dkey, body) for dkey, body in decision_keys if _pairs(dkey, key)]
        if len(candidates) != 1:
            continue
        dkey, body = candidates[0]
        seq = int(event['seq'])
        if any(other != seq and _pairs(dkey, ckey) for other, ckey in competitor_keys):
            continue
        if body.get('reason_code') is not None:
            continue
        code = OBSOLETE_REASONS.get(body.get('reason'))
        if code is None:
            continue
        out[seq] = {**detail, 'reason_code': code, 'recorded_reason_code': detail.get('reason_code'),
                    'projection': 'historical_obsolete_retry'}
    return out


def read_recent_activity(store, sid, owner, *, after_seq=None, limit=50):
    from routes.scans import _project_events
    if store.get_scan(sid, owner=owner) is None:
        return {'available': False, 'reason': 'scan_not_found'}
    try:
        privacy = store.remediation_filename_privacy(sid)
        paged = after_seq is not None
        cursor_clause = ' AND seq>%s' if paged else ''
        order = 'ASC' if paged else 'DESC'
        parameters = (sid, *KINDS, after_seq, limit) if paged else (sid, *KINDS, limit)
        with store._db.cursor() as cur:
            store._db.execute(cur,
                'SELECT * FROM scan_events WHERE scan_id=%s AND kind IN (' +
                ','.join(['%s'] * len(KINDS)) + ')' + cursor_clause + ' ORDER BY seq ' + order + ' LIMIT %s',
                parameters)
            rows = store._db.fetchall(cur)
        for row in rows:
            raw = row.get('detail')
            row['detail'] = json.loads(raw) if isinstance(raw, str) and raw else raw
        events = _project_events(rows if paged else list(reversed(rows)), sid, privacy, store=store)
        result = {'available': True, 'scan_id': sid, 'events': events}
        if paged:
            result.update(count=len(events), latest_seq=events[-1]['seq'] if events else None)
        return result
    except Exception:
        return {'available': False, 'reason': 'history_unavailable'}

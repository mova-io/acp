"""Content-free narration at the actual AI HTTP transport boundary.

Requests name the model sent, not a configured candidate. HTTP response success
is neither a usable draft nor saved-file accessibility verification.
"""
import json
import logging
import re
import uuid
from time import perf_counter
import math


def _identifier(value):
    return value if isinstance(value, str) and len(value) <= 128 and '://' not in value and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:/-]*', value) else None


def _emit(identity, status, http_status=None, elapsed_ms=None):
    if identity is None:
        return
    store, ctx, detail = identity
    kind = 'remediate.ai_request_started' if status == 'dispatched' else 'remediate.ai_request_finished'
    detail = dict(detail, status=status)
    if type(elapsed_ms) in (int, float) and math.isfinite(elapsed_ms) and elapsed_ms >= 0:
        detail['transport_elapsed_ms'] = round(elapsed_ms, 3)
        detail['timing_basis'] = 'http_transport_round_trip'
    if type(http_status) is int and 100 <= http_status <= 599:
        detail['http_status'] = http_status
    try:
        with store.transaction():
            with store._db.cursor() as cur:
                store._db.execute(cur, "SELECT e.execution_id FROM stage_executions e JOIN scan_runs s ON s.id=e.scan_id AND s.owner_email=e.owner_email WHERE e.execution_id=%s AND e.scan_id=%s AND e.owner_email=%s AND e.stage='remediate' AND e.is_current=1 AND e.cancel_requested_at IS NULL", (ctx.run_id, ctx.scan_id, ctx.owner_id))
                if not store._db.fetchone(cur):
                    return
            event = store.append_scan_event(ctx.scan_id, kind, phase='remediate', document=ctx.file,
                                            owner_email=ctx.owner_id, detail=detail)
            if event is not None:
                store.log_decision('system', kind, scan_id=ctx.scan_id, file=ctx.file,
                                   detail=json.dumps(detail, sort_keys=True))
    except Exception as error:
        logging.getLogger(__name__).warning('ai_request_activity_unavailable error_type=%s', type(error).__name__)


def _identity(provider, model, zone, surface):
    try:
        from ai_run_policy import RunContext, optional_current_run_context
        import core
        ctx = optional_current_run_context()
        provider, model = _identifier(provider), _identifier(model)
        if (type(ctx) is not RunContext or not ctx.file or not provider or not model
                or ctx.ledger.db is not core.store._db or surface not in {'text', 'vision'}):
            return None
        return core.store, ctx, dict(request_id=uuid.uuid4().hex, run_id=ctx.run_id,
            provider=provider, model=model, surface=surface,
            processing_zone=zone if zone in {'local', 'cloud', 'tenant'} else 'unknown')
    except Exception:
        return None


def send(post, endpoint, *, provider, model, zone, surface, **kwargs):
    identity = _identity(provider, model, zone, surface)
    _emit(identity, 'dispatched')
    started = perf_counter()
    try:
        response = post(endpoint, **kwargs)
    except Exception:
        _emit(identity, 'failed', elapsed_ms=(perf_counter() - started) * 1000)
        raise
    elapsed_ms = (perf_counter() - started) * 1000
    status = getattr(response, 'status_code', None)
    _emit(identity, 'response_received' if type(status) is int and 200 <= status < 300 else 'failed', status, elapsed_ms)
    return response


def managed_transport(post, specs, providers):
    """Wrap deepest transport so image bounds and admission still precede narration."""
    def transport(endpoint, **kwargs):
        payload = kwargs.get('json') or {}
        model = payload.get('model')
        spec = specs.get(model)
        if spec is None:
            return post(endpoint, **kwargs)
        messages = payload.get('messages') or []
        vision = any(isinstance(message, dict) and isinstance(message.get('content'), list)
                     and any(isinstance(block, dict) and block.get('type') in {'image', 'image_url'}
                             for block in message['content']) for message in messages)
        return send(post, endpoint, provider=spec.provider, model=model,
                    zone=_zone(providers, endpoint), surface='vision' if vision else 'text', **kwargs)
    return transport


def _zone(providers, endpoint):
    try:
        return providers.zone_for_url(endpoint)
    except Exception:
        return None

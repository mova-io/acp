"""Strict, opt-in text transport for governed remediation; no default prices.

Unlike the legacy drafting adapters this never hides usage failures, retries,
aliases the selected model, or falls back to another provider.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_CEILING
import hashlib
import json
import logging
import os
import time
from uuid import uuid4
from typing import Callable

from llm_remediation_waterfall import Generation, Model, Request, _money


class PreDispatchRejected(ValueError):
    """Transport was never invoked; this attempt cannot incur a provider charge."""


class ProviderAccessDenied(ValueError):
    """Provider rejected model access; never try another provider/model."""


def dispatch_endpoint(spec, provider_module) -> str | None:
    """The exact URL `generate_text` would send this spec to, read from the provider
    module's configured endpoints.

    One source for the transport and for the reported zone, so the catalog can never
    name a processing location the dispatch would not actually use. Returns None when
    the configuration reports no endpoint for the provider — the caller must render
    that as unknown rather than substituting one.
    """
    if spec.provider == 'openai':
        base = getattr(provider_module, '_OPENAI_TEXT_BASE_URL', None)
        if isinstance(base, str) and base.strip():
            return base.rstrip('/') + '/chat/completions'
        return None
    if spec.provider == 'anthropic':
        url = getattr(provider_module, '_ANTHROPIC_MESSAGES_URL', None)
        return url if isinstance(url, str) and url.strip() else None
    return None


@dataclass(frozen=True)
class TextModelSpec:
    provider: str
    model: str
    pricing_ref: str
    input_usd_per_million: str
    output_usd_per_million: str
    context_token_limit: int  # verified hard provider/model context ceiling
    output_token_limit: int
    verified_until: int  # expiry of the server-owned pricing/model-limit snapshot
    timeout_seconds: int = 30
    plain_text_only: bool = False

    def validate(self, now: float) -> None:
        if self.provider not in {'openai', 'anthropic'} or not self.model.strip() or not self.pricing_ref.strip():
            raise ValueError('explicit supported provider/model/pricing reference required')
        for amount in (self.input_usd_per_million, self.output_usd_per_million):
            if _money(amount) <= 0:
                raise ValueError('verified positive token prices required')
        for value in (self.context_token_limit, self.output_token_limit, self.verified_until, self.timeout_seconds):
            if type(value) is not int or value <= 0:
                raise ValueError('positive integer model bounds and expiry required')
        if type(self.plain_text_only) is not bool or (self.plain_text_only and
                (self.provider != 'anthropic' or self.model != 'claude-opus-5')):
            raise ValueError('unsupported explicit plain-text transport mode')
        if self.output_token_limit > self.context_token_limit or self.timeout_seconds > 120:
            raise ValueError('invalid output or timeout bound')
        if now >= self.verified_until:
            raise ValueError('pricing/model-limit snapshot expired')

    def zone(self, provider_module) -> str | None:
        """Governance zone of the endpoint this spec dispatches to, or None when the
        configuration reports no endpoint for it.

        Derived from the configured URL by `providers.zone_for_url` — the single source
        of truth (`/config` and the per-call trace read the same function) — and never
        from the provider's NAME, which cannot distinguish a self-hosted
        OpenAI-compatible endpoint from api.openai.com.

        None means NOT REPORTED and must be rendered that way. It is deliberately not
        defaulted: `zone_for_url('')` answers 'local', so falling through to it would
        assert that no document left the network — the one wrong answer that matters.
        """
        url = dispatch_endpoint(self, provider_module)
        derive = getattr(provider_module, 'zone_for_url', None)
        if url is None or not callable(derive):
            return None
        zone = derive(url)
        return zone if isinstance(zone, str) and zone.strip() else None

    def maximum_cost(self) -> str:
        # Reserving the full verified context ceiling avoids an estimated tokenizer
        # count being mistaken for a verified spending bound.
        return str((Decimal(self.context_token_limit) * _money(self.input_usd_per_million)
                    + Decimal(self.output_token_limit) * _money(self.output_usd_per_million)) / 1_000_000)


class StrictTextGenerator:
    def __init__(self, specs: tuple[TextModelSpec, ...], *,
                 post: Callable | None = None, clock: Callable = time.time,
                 provider_module=None):
        if provider_module is None:
            import providers as provider_module
        self.providers = provider_module
        self.clock = clock
        if len(specs) not in (2, 3) or len({spec.model for spec in specs}) != len(specs):
            raise ValueError('two or three distinct model IDs required')
        for spec in specs:
            spec.validate(clock())
        # Reuse existing owner opt-in; mere credential presence never activates a second
        # provider or changes the global selection. The PRIMARY is still the owner-selected
        # text provider — nothing may displace the vendor the deployment chose. A later step
        # may use a different vendor only where the owner named it as a permitted fallback
        # (providers.permitted_text_providers), so a chain spans vendors by authorisation and
        # never by a key that merely happens to be present.
        active = self.providers.active_text_provider()
        if specs[0].provider != active:
            raise ValueError('the primary model must use the owner-selected text provider')
        permitted = self.providers.permitted_text_providers()
        unauthorised = sorted({spec.provider for spec in specs} - set(permitted))
        if unauthorised:
            raise ValueError('fallback provider not authorised for text: ' + ', '.join(unauthorised))
        # Every vendor in the chain needs its own resolvable credential, not just the primary.
        for provider in sorted({spec.provider for spec in specs}):
            if not self.providers._text_key_for(provider):
                raise ValueError('selected provider credential unavailable')
        self.specs = {spec.model: spec for spec in specs}
        self.models = tuple(Model(spec.model, spec.maximum_cost()) for spec in specs)
        self.pricing_refs = {spec.model: spec.pricing_ref for spec in specs}
        # Derived from the configured dispatch endpoint, not from the provider name.
        # A model whose endpoint the configuration does not report stays None here.
        self.zones = {spec.model: spec.zone(self.providers) for spec in specs}
        if post is None:
            import httpx
            post = httpx.post
        from ai_request_activity import managed_transport
        self.post = managed_transport(post, self.specs, self.providers)

    def __call__(self, model: str, request: Request) -> Generation:
        spec = self.specs[model]
        spec.validate(self.clock())
        # Re-read authorisation at dispatch, not just at construction: governance can be
        # withdrawn mid-run, and a chain built when a fallback was permitted must stop using it
        # the moment it is not.
        if spec.provider not in self.providers.permitted_text_providers():
            raise ValueError('provider governance changed; dispatch blocked')
        if request.family != 'html-root-language' or not request.authority_ref or not request.expected_language:
            raise ValueError('supported family and authoritative language required')
        prompt = ('Return only a JSON object with the key "language". Correct the HTML root language '
                  'using the authorized language metadata. Do not infer language or rewrite content.\n'
                  + json.dumps({'authorized_language': request.expected_language,
                                'html': request.source}, ensure_ascii=True))
        result = self.generate_text(model, prompt)
        try:
            patch = json.loads(result['text'])
        except (ValueError, TypeError):
            patch = {}
        if not isinstance(patch, dict) or result['bounds_exceeded'] or result.get('response_issue'):
            patch = {}
        return Generation(patch, result['cost_usd'], result['call_id'])

    def generate_text(self, model: str, prompt: str) -> dict:
        try:
            spec = self.specs[model]
            spec.validate(self.clock())
            # The real egress point. Authorisation is re-read here too — construction-time
            # approval is not a licence that outlives the setting that granted it.
            if spec.provider not in self.providers.permitted_text_providers():
                raise ValueError('provider governance changed; dispatch blocked')
            # A conservative payload bound prevents unbounded prompt construction
            # reaching transport, including room for the configured output allowance.
            # Spending reservation still uses the full context ceiling.
            if len(prompt.encode('utf-8')) + 1024 + spec.output_token_limit > spec.context_token_limit:
                raise ValueError('source exceeds bounded text request size')
            key = self.providers._text_key_for(spec.provider)
            if not key:
                raise ValueError('selected provider credential unavailable')
            payload = {'model': spec.model, 'messages': [{'role': 'user', 'content': prompt}]}
            # Same resolution the reported zone is derived from, so a catalog entry can
            # never name a destination other than the one this request is sent to.
            endpoint = dispatch_endpoint(spec, self.providers)
            if endpoint is None:
                raise ValueError('configured provider endpoint unavailable')
            if spec.provider == 'openai':
                payload['max_completion_tokens'] = spec.output_token_limit
                headers = {'Authorization': f'Bearer {key}'}
            else:
                payload['max_tokens'] = spec.output_token_limit
                if spec.plain_text_only or spec.model == 'claude-sonnet-5':
                    # This adapter accepts text only. Sonnet/Opus 5 default to thinking
                    # blocks; its documented disabled mode requires effort <= high.
                    payload['thinking'] = {'type': 'disabled'}
                    payload['output_config'] = {'effort': 'high'}
                headers = {'x-api-key': key, 'anthropic-version': self.providers._ANTHROPIC_API_VERSION}
        except Exception as exc:
            raise PreDispatchRejected(str(exc) if isinstance(exc, ValueError) else "request rejected before transport") from exc
        # One request, redirects off, no SDK retry. Never log headers/body/errors.
        response = self.post(endpoint, json=payload, headers=headers,
                             timeout=spec.timeout_seconds, follow_redirects=False)
        if getattr(response, 'status_code', None) in (401, 403):
            raise ProviderAccessDenied('provider access denied')
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, dict) or data.get('model') != spec.model:
            raise ValueError('response model identity missing or mismatched')
        usage = data.get('usage')
        if not isinstance(usage, dict):
            raise ValueError('provider usage missing')
        if spec.provider == 'openai':
            details = usage.get('prompt_tokens_details') or {}
            output_details = usage.get('completion_tokens_details') or {}
            if (not isinstance(details, dict) or not isinstance(output_details, dict)
                    or any(details.get(k, 0) != 0 for k in ('cached_tokens', 'audio_tokens'))
                    or output_details.get('audio_tokens', 0) != 0):
                raise ValueError('unsupported cached/audio token accounting')
            input_tokens = usage.get('prompt_tokens')
            output_tokens = usage.get('completion_tokens')
            choices = data.get('choices')
            if not isinstance(choices, list) or len(choices) != 1:
                raise ValueError('expected exactly one completion')
            message = choices[0].get('message', {})
            text = message.get('content')
            finish = choices[0].get('finish_reason')
            response_issue = ('refused' if message.get('refusal') or finish == 'content_filter'
                              else 'truncated' if finish == 'length' else None)
        else:
            input_tokens = usage.get('input_tokens')
            output_tokens = usage.get('output_tokens')
            # Cache billing has separate rates. This MVP sends no cache-control;
            # unexpected cached usage must not be silently costed at the wrong rate.
            if any(usage.get(k, 0) != 0 for k in ('cache_creation_input_tokens', 'cache_read_input_tokens')):
                raise ValueError('unsupported cached token accounting')
            blocks = data.get('content')
            if not isinstance(blocks, list) or len(blocks) != 1 or blocks[0].get('type') != 'text':
                raise ValueError('expected exactly one text block')
            text = blocks[0].get('text')
            response_issue = ('refused' if data.get('stop_reason') == 'refusal'
                              else 'truncated' if data.get('stop_reason') == 'max_tokens' else None)
        if any(type(n) is not int or n <= 0 for n in (input_tokens, output_tokens)):
            raise ValueError('positive measured token usage required')
        # Return actual cost even on a provider overrun so the ledger records and
        # blocks it. Never clamp usage to the reservation or default missing to zero.
        cost = (Decimal(input_tokens) * _money(spec.input_usd_per_million)
                + Decimal(output_tokens) * _money(spec.output_usd_per_million)) / 1_000_000
        return {'text': text if isinstance(text, str) else '', 'response_issue': response_issue,
                'cost_usd': str(cost), 'call_id': str(data.get('id') or ''),
                'model': spec.model, 'provider': spec.provider,
                'zone': self.providers.zone_for_url(endpoint),
                'prompt_tokens': input_tokens, 'completion_tokens': output_tokens,
                'bounds_exceeded': input_tokens > spec.context_token_limit or output_tokens > spec.output_token_limit}


def managed_context():
    """No implicit management for existing unbudgeted calls."""
    try:
        from ai_run_policy import current_run_context
    except ModuleNotFoundError as exc:
        if exc.name != 'ai_run_policy':
            raise
        return None
    return current_run_context(required=False)


REFUSAL_REASON_CHARS = frozenset('abcdefghijklmnopqrstuvwxyz0123456789_')


def bounded_refusal_reason(reason) -> bool:
    """A fixed lowercase refusal code, never provider text or a document detail."""
    return isinstance(reason, str) and 0 < len(reason) <= 64 and set(reason) <= REFUSAL_REASON_CHARS


def _record_pre_dispatch_refusal(ctx, reason: str, kind: str) -> None:
    """Persist the EXACT reason a request was refused before the ledger or a provider.

    This refusal leaves `ai_calls` and `ai_spending_attempts` empty precisely because nothing
    was attempted, so the only surviving record is a generic downstream block and a
    misconfiguration is indistinguishable from a budget or provider failure. A refusal that
    DID attempt already has its reservation and attempt-history rows, so it is not repeated
    here. Owner-gated and best effort, on the same narration path as ai_request_activity:
    evidence must never fail the work it describes.
    """
    try:
        import core
        from ai_run_policy import RunContext
        store = core.store
        if (type(ctx) is not RunContext or not bounded_refusal_reason(reason)
                or not getattr(ctx, 'scan_id', None) or not getattr(ctx, 'file', None)
                or ctx.ledger.db is not store._db):
            return
        detail = {'request_id': uuid4().hex, 'run_id': ctx.run_id, 'status': 'failed',
                  'surface': kind if kind in ('text', 'vision') else 'text',
                  'model': 'not-dispatched', 'processing_zone': 'unknown',
                  'dispatched': False, 'reason': reason}
        with store.transaction():
            with store._db.cursor() as cur:
                store._db.execute(cur, "SELECT e.execution_id FROM stage_executions e JOIN scan_runs s ON s.id=e.scan_id AND s.owner_email=e.owner_email WHERE e.execution_id=%s AND e.scan_id=%s AND e.owner_email=%s AND e.stage='remediate' AND e.is_current=1 AND e.cancel_requested_at IS NULL", (ctx.run_id, ctx.scan_id, ctx.owner_id))
                if not store._db.fetchone(cur):
                    return
            event = store.append_scan_event(ctx.scan_id, 'remediate.ai_request_finished',
                                            phase='remediate', document=ctx.file,
                                            owner_email=ctx.owner_id, correlation_id=ctx.run_id,
                                            detail=detail)
            if event is not None:
                store.log_decision('system', 'remediate.ai_request_finished', scan_id=ctx.scan_id,
                                   file=ctx.file, detail=json.dumps(detail, sort_keys=True))
    except Exception as error:
        logging.getLogger(__name__).warning('ai_refusal_evidence_unavailable error_type=%s',
                                            type(error).__name__)


def defer_managed(reason: str, *, kind: str = 'text', attempts=None) -> dict:
    ctx = managed_context()
    item = {'reason': reason, 'kind': kind, 'status': 'deferred',
            'attempts': attempts or []}
    if ctx is not None and hasattr(ctx, 'deferred'):
        # One record per distinct reason per run context. A repeat of the same refusal is the
        # same configuration fact, not new evidence, and this path fans out per image.
        first = not any(entry.get('reason') == reason for entry in ctx.deferred)
        ctx.deferred.append(item)
        if first and not item['attempts']:
            _record_pre_dispatch_refusal(ctx, reason, kind)
    return {'text': '', 'deferred': True, **item}


def managed_text_generate(prompt: str) -> dict:
    """Budgeted semantic DRAFT only; never an automatic approval decision.

    An unusable but fully accounted response can try the second model. Unknown
    usage retains its reservation and ends this call without another attempt.
    """
    from llm_remediation_waterfall import BudgetAdapter
    ctx = managed_context()
    if ctx is None:
        raise ValueError('managed text generation requires a durable run context')
    if not ctx.enabled:
        return defer_managed('ai_disabled_or_budget_zero')
    try:
        generator = configured_generator()
        budget = BudgetAdapter(ctx.ledger, ctx.owner_id, ctx.run_id, generator.pricing_refs)
    except Exception:
        return defer_managed('verified_model_pricing_unavailable')
    result = managed_generate_attempts(prompt, ctx, generator)
    if result.get('text') and not result.get('deferred') and getattr(ctx, 'policy', {}).get('ai_review', {}).get('enabled'):
        from ai_review_chain import review_managed_draft
        from ai_attempt_history import AttemptHistory
        history = AttemptHistory(ctx.ledger.db)
        return review_managed_draft(prompt, result, ctx, generator, history)
    return result


def managed_generate_attempts(prompt, ctx, generator, *, purpose='draft',
                              tier_indices=None, operation_id=None, image_prefix=False, verified_retry=None):
    """One bounded generation operation, also usable for explicit review stages.

    The caller supplies a trusted configured generator and immutable run context.
    Distinct purposes have separate spending identities; legacy draft keys stay
    unchanged so a rollout cannot buy an old paid attempt again.
    """
    from ai_attempt_history import AttemptHistory, PURPOSES
    from llm_remediation_waterfall import BudgetAdapter
    from ai_generation_chain import normalize_chain, STEP_IDS, ELIGIBLE
    if type(image_prefix) is not bool:
        raise ValueError('explicit image prefix mode required')
    # Fail closed: a model the zone map does not place ('zones' absent, or the model absent
    # from it, or mapped to None) is refused exactly like a local one. That is deliberate —
    # TextModelSpec.zone returns None for NOT REPORTED and must never be read as cloud.
    # The cost of it is that a WRAPPER which forgets to carry `zones` forward turns every
    # quality_first dispatch into this refusal, before any ledger or provider call, with no
    # error anywhere. Any object handed to this function must propagate the real map
    # (see _CaptionGenerator / _ValidatedGenerator); synthesising one here would trade a
    # silent refusal for a silent lie about where a document was sent.
    if ctx.policy.get('quality_first') and any(getattr(generator, 'zones', {}).get(model.name) != 'cloud' for model in generator.models):
        return defer_managed('quality_first_cloud_endpoint_required')
    chain = getattr(ctx, 'policy', {}).get('generation_chain')
    if chain is not None:
        try:
            chain = normalize_chain(chain)
            if any(index >= len(generator.models) or generator.models[index].name != step['model']
                   or generator.specs[step['model']].provider != step['provider']
                   for index, step in enumerate(chain['steps'])):
                return defer_managed('approved_generation_models_unavailable')
        except (ValueError, KeyError, TypeError):
            return defer_managed('approved_generation_chain_invalid')
    expected_indices = tuple(range(1, len(chain['steps']) + 1)) if chain else (1, 2)
    if tier_indices is None:
        tier_indices = expected_indices
    if image_prefix:
        # A verified image request may use the first two accepted positions;
        # the full immutable chain was checked above, including its unused third.
        # Other draft paths keep their exact-chain requirement.
        from document_wide_provider import VISION_MODELS
        if (purpose != 'draft' or not chain or len(chain['steps']) != 3
                or tuple(tier_indices) != (1, 2)
                or any(generator.models[i].name not in VISION_MODELS.get(
                    generator.specs[generator.models[i].name].provider, set())
                    or generator.specs[generator.models[i].name].plain_text_only for i in (0, 1))):
            return defer_managed('approved_image_prefix_unavailable')
    if purpose == 'draft' and chain and tuple(tier_indices) != expected_indices and not image_prefix:
        return defer_managed('approved_generation_chain_mismatch')
    if purpose not in PURPOSES or not tier_indices or any(i not in (expected_indices if purpose == 'draft' else (1, 2)) for i in tier_indices) or len(set(tier_indices)) != len(tier_indices):
        raise ValueError('supported purpose and unique model tiers required')
    if not ctx.enabled:
        return defer_managed('ai_disabled_or_budget_zero')
    adapter = None
    if purpose == 'draft' and chain and len(chain['steps']) == 3:
        from ai_generation_adapter import current_generation_adapter
        adapter = current_generation_adapter()
        if (adapter is None and not image_prefix) or not getattr(ctx, 'scan_id', None) or not getattr(ctx, 'file', None):
            return defer_managed('supported_generation_adapter_required')
    if verified_retry is not None:
        from office_verified_retry import RetryAuthority
        if (type(verified_retry) is not RetryAuthority or purpose != 'review'
                or tuple(tier_indices) != (2,) or not chain
                or (verified_retry.owner_id, verified_retry.run_id, verified_retry.scan_id, verified_retry.file)
                != (ctx.owner_id, ctx.run_id, ctx.scan_id, ctx.file)
                or verified_retry.check() is not True):
            return defer_managed('verified_retry_authority_unavailable')
    budget = BudgetAdapter(ctx.ledger, ctx.owner_id, ctx.run_id, generator.pricing_refs)
    input_hash = hashlib.sha256(prompt.encode('utf-8')).hexdigest()
    operation = operation_id or (input_hash if purpose == 'draft' else
        hashlib.sha256((purpose + ':' + prompt).encode('utf-8')).hexdigest())
    if adapter:
        # New-chain identities bind the exact assessed source and finding; an
        # identical prompt for another location can never replay its proposal.
        binding = {key: adapter[key] for key in ('source_sha256', 'assessment_revision',
                   'finding_ids', 'locator', 'adapter_id')}
        from remediation_contribution import SOURCE
        if SOURCE.get() != (ctx.scan_id, ctx.file, adapter['source_sha256']):
            return defer_managed('assessed_source_changed')
        operation = hashlib.sha256(json.dumps({'prompt': input_hash, 'binding': binding,
                     'chain': chain}, sort_keys=True).encode()).hexdigest()
    attempts = []
    parent_attempt_id = None
    escalation_reason = None
    history = None
    previous = []
    # Production RunContext always supplies canonical scan/file identity. Small
    # pre-existing isolated transport callers without a scan remain ledger-only.
    if getattr(ctx, 'scan_id', None):
        if not getattr(ctx, 'file', None):
            return defer_managed('attempt_history_source_unavailable')
        history = AttemptHistory(ctx.ledger.db)
        try:
            previous = history.list_operation(ctx.owner_id, ctx.scan_id, ctx.run_id, operation, file=ctx.file)
        except Exception:
            return defer_managed('attempt_history_unavailable')
        for row in previous:
            result = row.get('result') or {}
            if (row['status'] == 'drafted' and row['spending_state'] == 'settled'
                    and row['output_retention'] == 'full' and row['input_sha256'] == input_hash
                    and row['model'] in {generator.models[i - 1].name for i in tier_indices}
                    and row['provider'] == generator.specs[row['model']].provider
                    and row['purpose'] in ({'draft', 'fallback'} if purpose == 'draft' else {purpose}) and isinstance(result.get('text'), str)
                    and result['text'].strip() and not result.get('response_issue')
                    and result.get('bounds_exceeded') is False
                    and (not adapter or (result.get('validation_outcome') == 'usable'
                         and all((result.get('execution') or {}).get(k) == v for k,v in binding.items())))):
                return {**result, 'attempts': [_history_attempt(r) for r in previous],
                        'approval_required': True, 'replayed': True, 'operation_id': operation,
                        'input_sha256': input_hash, 'history_attempt_id': row['attempt_id']}

    def retain(attempt, status, result=None, reason=None):
        if history is None:
            return True
        try:
            history.finish(ctx.owner_id, ctx.scan_id, ctx.run_id, attempt['attempt_id'],
                           status=status, result=result, reason=reason)
            return True
        except Exception:
            return False

    for index in tier_indices:
        model = generator.models[index - 1]
        # A worker can stop after committing an unusable first response but
        # before reserving fallback. Reuse that completed step, not its charge.
        # The second tier still passes normal current budget/dispatch admission.
        if purpose == 'draft' and index < max(tier_indices):
            completed = [row for row in previous
                if row['purpose'] == ('draft' if index == 1 else 'fallback') and row['model'] == model.name
                and row['provider'] == generator.specs[model.name].provider
                and row['input_sha256'] == input_hash and row['spending_state'] == 'settled'
                and row['output_retention'] == 'full'
                and row['attempt_id'].startswith(f'text:{operation}:{index}:')
                and (row.get('result') or {}).get('bounds_exceeded') is False
                and ((row['status'] == 'empty_response'
                      and not (row.get('result') or {}).get('response_issue')
                      and ((row.get('result') or {}).get('text') == ''
                           or (adapter and isinstance((row.get('result') or {}).get('text'), str)
                               and not row['result']['text'].strip())))
                     or (row['status'] == 'unusable_response'
                         and (row.get('result') or {}).get('response_issue') in (ELIGIBLE if adapter else {'truncated'})))
                and (not adapter or all(((row.get('result') or {}).get('execution') or {}).get(k) == v for k,v in binding.items()))]
            if len(completed) == 1:
                attempts.append(_history_attempt(completed[0]))
                parent_attempt_id = completed[0]['attempt_id']
                escalation_reason = (completed[0].get('result') or {}).get('response_issue') or 'empty_response'
                continue
        if adapter:
            from remediation_contribution import SOURCE
            if SOURCE.get() != (ctx.scan_id, ctx.file, adapter['source_sha256']):
                return defer_managed('assessed_source_changed', attempts=attempts)
        if chain and len(chain['steps']) == 3:
            try:
                from worker import check_cancel
                check_cancel()
                with ctx.ledger.db.cursor() as cur:
                    ctx.ledger.db.execute(cur, '''SELECT cancel_requested_at,state FROM stage_executions
                        WHERE execution_id=%s AND scan_id=%s AND owner_email=%s''',
                        (ctx.run_id, ctx.scan_id, ctx.owner_id))
                    execution = ctx.ledger.db.fetchone(cur)
                allowed_states = ('accepted', 'queued', 'processing')
                if verified_retry is not None and verified_retry.check() is True:
                    allowed_states += ('processing_complete', 'succeeded')
                if execution is None or execution['cancel_requested_at'] or execution['state'] not in allowed_states:
                    return defer_managed('run_stopped_or_unavailable', attempts=attempts)
            except Exception:
                return defer_managed('run_dispatch_permission_unavailable', attempts=attempts)
        if adapter:
            if index > 1 and (parent_attempt_id is None or escalation_reason not in ELIGIBLE):
                return defer_managed('eligible_predecessor_unavailable', attempts=attempts)
        attempt_id = f'text:{operation}:{index}:0'
        attempt = {'attempt_id': attempt_id, 'model': model.name,
                   'max_cost_usd': model.max_cost_usd, 'status': 'reserving'}
        attempts.append(attempt)
        try:
            maximum = int((_money(model.max_cost_usd) * 1_000_000).to_integral_value(rounding=ROUND_CEILING))
            for retry in range(16):
                attempt_id = f'text:{operation}:{index}:{retry}'
                row = ctx.ledger.reserve(ctx.owner_id, ctx.run_id, attempt_id, maximum,
                                         generator.pricing_refs[model.name])
                if row['state'] != 'released':
                    break
            else:
                raise ValueError('pre-dispatch retry limit reached')
            token = attempt_id
            attempt['attempt_id'] = attempt_id
            if history is not None:
                try:
                    history.begin(ctx.owner_id, ctx.scan_id, ctx.run_id, operation, attempt_id,
                        file=ctx.file, input_sha256=input_hash, model=model.name,
                        provider=generator.specs[model.name].provider,
                        purpose='fallback' if purpose == 'draft' and index > 1 else purpose,
                        **({'execution': {**binding, 'chain_version': 1, 'step_id': STEP_IDS[index-1],
                            'generation_position': index-1, 'parent_attempt_id': parent_attempt_id,
                            'escalation_reason': escalation_reason, 'request_id': attempt_id}} if adapter else {}))
                except Exception:
                    if row['state'] == 'reserved':
                        ctx.ledger.release(ctx.owner_id, ctx.run_id, token, confirmed_not_charged=True)
                    return defer_managed('attempt_history_unavailable', attempts=attempts)
            if budget.claim_dispatch(token) is not True:
                attempt['status'] = 'existing_attempt_requires_reconciliation'
                return defer_managed('existing_draft_attempt_requires_reconciliation', attempts=attempts)
        except Exception:
            attempt['status'] = 'reservation_or_dispatch_denied'
            return defer_managed('budget_admission_denied', attempts=attempts)
        def narrate(status):
            if chain and index > 1 and getattr(ctx, 'scan_id', None) and getattr(ctx, 'file', None):
                try:
                    import core
                    from ai_escalation_activity import emit
                    emit(core.store, scan_id=ctx.scan_id, owner_id=ctx.owner_id,
                         run_id=ctx.run_id, file=ctx.file, operation_id=operation,
                         position=index-1, model=model.name, status=status,
                         reason_code=('independent_caption_verification_failed' if verified_retry is not None
                                      else 'approved_model_fallback'))
                except Exception as error:
                    # Narration cannot change admitted work or spending reconciliation.
                    logging.getLogger(__name__).warning('ai_escalation_narration_unavailable error_type=%s',
                                                       type(error).__name__)
        narrate('dispatched')
        try:
            result = generator.generate_text(model.name, prompt)
        except PreDispatchRejected:
            narrate('not_dispatched')
            attempt['status'] = 'rejected_before_dispatch'
            try:
                ctx.ledger.release(ctx.owner_id, ctx.run_id, token, confirmed_not_charged=True)
            except Exception:
                attempt['reconciliation_required'] = True
                return defer_managed('budget_release_failed', attempts=attempts)
            retain(attempt, 'rejected_before_dispatch')
            return defer_managed('request_rejected_before_dispatch', attempts=attempts)
        except Exception as exc:
            narrate('usage_unconfirmed')
            failure = 'provider_access_denied' if isinstance(exc, ProviderAccessDenied) else 'provider_usage_unknown'
            attempt['status'] = 'usage_unknown'
            try:
                budget.mark_uncertain(token, 'provider_failure_or_unknown_usage')
            except Exception:
                attempt['reconciliation_required'] = True
            retain(attempt, 'usage_unknown', reason=failure)
            return defer_managed(failure, attempts=attempts)
        attempt.update(cost_usd=result['cost_usd'], call_id=result['call_id'], status='settling')
        try:
            budget.settle(token, result['cost_usd'])
        except Exception:
            narrate('usage_unconfirmed')
            attempt['status'] = 'settlement_failed_or_breached'
            retain(attempt, attempt['status'], result)
            return defer_managed('budget_settlement_failed_or_breached', attempts=attempts)
        issue = result.get('response_issue')
        if adapter and not issue and not result['bounds_exceeded'] and result['text'].strip():
            try:
                issue = adapter['validator'](result['text'])
            except Exception:
                issue = 'validation_unavailable'
            if issue is not None and issue not in ELIGIBLE:
                issue = 'validation_unavailable'
            result['response_issue'] = issue
            result['validation_outcome'] = 'usable' if issue is None else issue
        if adapter and not result.get('validation_outcome'):
            result['validation_outcome'] = issue or ('empty_response' if not result['text'].strip() else 'unavailable')
        status = ('provider_limit_exceeded' if result['bounds_exceeded'] else
                  'refused' if issue == 'refused' else 'unusable_response' if issue else
                  'drafted' if result['text'].strip() else 'empty_response')
        attempt['status'] = status
        if adapter and status == 'drafted':
            result['skipped_step_ids'] = [STEP_IDS[i-1] for i in tier_indices if i > index]
        if issue:
            attempt['reason'] = issue
        if not retain(attempt, status, result, issue):
            narrate('needs_manual')
            return defer_managed('attempt_output_retention_failed', attempts=attempts)
        if verified_retry is None:
            narrate('response_ready' if status == 'drafted' else 'needs_manual')
        if status == 'provider_limit_exceeded':
            return defer_managed('provider_limit_exceeded', attempts=attempts)
        if status == 'refused':
            return defer_managed('provider_refused', attempts=attempts)
        if adapter and issue and issue not in ELIGIBLE:
            return defer_managed('generation_validation_unavailable', attempts=attempts)
        if status == 'drafted':
            return {**result, 'attempts': attempts, 'approval_required': True,
                    'operation_id': operation, 'input_sha256': input_hash,
                    'history_attempt_id': attempt_id if history else None}
        parent_attempt_id = attempt_id
        escalation_reason = issue or 'empty_response'
    return defer_managed('attempts_exhausted', attempts=attempts)


def _history_attempt(row):
    result = row.get('result') or {}
    return {'attempt_id': row['attempt_id'], 'model': row['model'], 'status': row['status'],
            'cost_usd': result.get('cost_usd'), 'call_id': result.get('call_id'),
            'reason': row.get('reason')}


def configured_generator() -> StrictTextGenerator:
    ctx = managed_context()
    if ctx is not None and ctx.policy.get('quality_first'):
        from quality_first import configured_quality_generator
        return configured_quality_generator()
    raw = os.environ.get('ACP_BOUNDED_TEXT_MODELS_JSON')
    if raw is None and os.environ.get('ACP_BOUNDED_TEXT_PROFILE'):
        from ai_model_profiles import model_config
        config = model_config(os.environ['ACP_BOUNDED_TEXT_PROFILE'], include_second_fallback=True)
    else:
        config = json.loads(raw or 'null')
    if not isinstance(config, list) or len(config) not in (2, 3):
        raise ValueError('two or three verified model configurations required')
    return StrictTextGenerator(tuple(TextModelSpec(**item) for item in config))


def managed_text_ready() -> bool:
    ctx = managed_context()
    if ctx is None or not ctx.enabled:
        defer_managed('ai_disabled_or_budget_zero')
        return False
    try:
        configured_generator()
    except Exception:
        defer_managed('verified_model_pricing_unavailable')
        return False
    return True

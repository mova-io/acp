"""quality_first must refuse a non-cloud endpoint — and only a non-cloud endpoint.

THE DEFECT (production 2026.9.17.3, sha f1da649). A run saved with AI on, `ai_zone` any,
`quality_first` true and a $25 cap produced an EMPTY `ai_calls` table and an EMPTY
`ai_spending_attempts` table for a .docx whose 1.1.1 review then sat pending with zero
proposals. Nothing failed; nothing was ever attempted.

`vision_generation.generate` builds its dispatch object by wrapping the configured generator
three times. Two of those wrappers are `copy.copy`, which carries every attribute. The third,
`_CaptionGenerator`, is a hand-written class that copied `models`, `specs` and `pricing_refs`
by hand and did not copy `zones`. `managed_generate_attempts` reads the zone map off whatever
object it is handed:

    getattr(generator, 'zones', {}).get(model.name) != 'cloud'

so against that wrapper every model resolved to `None`, `None != 'cloud'`, and EVERY
quality_first vision dispatch returned `quality_first_cloud_endpoint_required` before the
ledger or the provider was touched. That is exactly an empty `ai_calls` plus an empty
`ai_spending_attempts`, and it happened with both configured models genuinely on `cloud`.

The check itself is right and stays: `TextModelSpec.zone` returns None for NOT REPORTED, and
reading that as cloud would assert a document stayed on our own network when nothing knows
where it went. So these tests pin BOTH directions — the real map reaches the real dispatch
path, and an absent or non-cloud zone is still refused.

Hermetic: synthetic prices, a stub provider module, and a `post` that never leaves the
process. No paid dispatch.
"""
from io import BytesIO
from types import SimpleNamespace
import time

import pytest
from PIL import Image

import vision_generation as vision
from ai_attempt_history import AttemptHistory
from ai_run_policy import run_context
from llm_waterfall_provider import StrictTextGenerator, TextModelSpec, managed_generate_attempts
from test_ai_run_policy import seed
from test_llm_waterfall_provider import Response, result

# Both must be verified image-input models, or `configured_vision_generator` refuses the
# chain before the zone check is ever reached and the test would prove nothing.
MODELS = ('gpt-4.1-mini-2025-04-14', 'gpt-4.1-2025-04-14')
CAPTION = 'A red bicycle leaning against a brick wall.'

# The saved settings from the production run, through the real normalizer: quality_first is
# only accepted alongside automatic cloud input, document-wide AI, zone "any" and a cap.
POLICY = {'ai': 1, 'rule_based': 2, 'snapshot_id': 'fixture-snapshot',
          'ai_budget_usd': '25.00', 'ai_zone': 'any', 'cloud_input_strategy': 'automatic',
          'document_wide_ai': True, 'quality_first': True, 'auto_approve_ai': True}


def image(color='red'):
    out = BytesIO()
    Image.new('RGB', (16, 16), color).save(out, format='PNG')
    return out.getvalue()


def providers_reporting(zone, *, reported=True):
    """A provider module whose configured endpoints resolve to `zone`.

    `reported=False` drops `zone_for_url` entirely, which is how a configuration that cannot
    place its endpoint at all reaches `TextModelSpec.zone` — it returns None, not a default.
    """
    namespace = {
        '_OPENAI_TEXT_BASE_URL': 'https://fixture.invalid/v1',
        '_ANTHROPIC_MESSAGES_URL': 'https://fixture.invalid/messages',
        'active_text_provider': classmethod(lambda cls: 'openai'),
        'permitted_text_providers': classmethod(lambda cls: frozenset({'openai'})),
        '_text_key_for': staticmethod(lambda provider: 'fixture-not-a-secret'),
    }
    if reported:
        namespace['zone_for_url'] = staticmethod(lambda url: zone)
    return type('FixtureProviders', (), namespace)


@pytest.fixture
def quality_first_run(isolated_store, monkeypatch):
    """A real managed run under the saved quality-first policy, with a real ledger."""
    import core, lf
    monkeypatch.setattr(core, 'store', isolated_store)
    monkeypatch.setattr(lf, 'trace_ai_call', lambda *a, **kw: None)
    seed(isolated_store)
    batch = isolated_store.enqueue_stage_batch(
        'scan', 'remediate', 'remediate_file',
        [{'owner': 'owner', 'scan_id': 'scan', 'file': 'a.docx',
          'remediation_impact_policy': dict(POLICY)}],
        snapshot_id='snapshot', request_fingerprint='quality-first-zone')
    job = isolated_store.get_job(batch['job_ids'][0])
    return isolated_store, job


def build_generator(zone, *, reported=True, post=None, models=MODELS):
    specs = tuple(TextModelSpec('openai', name, 'fixture-price-v1', '1', '2', 20000, 128,
                                int(time.time()) + 3600) for name in models)
    if post is None:
        def post(*args, **kwargs):  # pragma: no cover - a call here is the failure
            raise AssertionError('a refused dispatch must never reach transport')
    return StrictTextGenerator(specs, provider_module=providers_reporting(zone, reported=reported),
                               post=post)


def test_the_saved_policy_really_is_quality_first(quality_first_run):
    """Guards the fixture: if normalization dropped the flag these tests are vacuous."""
    store, job = quality_first_run
    with run_context(store, job['payload'], job) as ctx:
        assert ctx.policy['quality_first'] is True
        assert ctx.policy['ai_zone'] == 'any'
        assert ctx.enabled is True


def test_a_cloud_zoned_caption_generator_is_admitted_and_spends(quality_first_run, monkeypatch):
    """Case 1 — the production case. Zone 'cloud' must reach the provider and the ledger."""
    store, job = quality_first_run
    calls = []
    def post(*args, **kwargs):
        calls.append(kwargs['json'])
        return Response(result(model=kwargs['json']['model'], text=CAPTION))
    generator = build_generator('cloud', post=post)
    assert generator.zones == {name: 'cloud' for name in MODELS}
    monkeypatch.setattr(vision, 'configured_generator', lambda: generator)

    with run_context(store, job['payload'], job) as ctx:
        generated = vision.generate('Describe the image', image())
        attempts = AttemptHistory(store._db).list_run(ctx.owner_id, ctx.scan_id, ctx.run_id)

    assert generated.get('reason') != 'quality_first_cloud_endpoint_required'
    assert generated['ok'] is True and generated['text'] == CAPTION
    assert generated['zone'] == 'cloud'
    # The two symptoms the user saw, inverted: a paid attempt was recorded, and a call traced.
    assert len(calls) == 1
    assert [row['spending_state'] for row in attempts] == ['settled']
    assert len(store.list_ai_calls('scan')) == 1


def test_the_wrapper_carries_the_real_zone_map_not_a_manufactured_one(quality_first_run, monkeypatch):
    """The fix must PROPAGATE. A hard-coded 'cloud' would pass case 1 and be a lie."""
    generator = build_generator('local')
    wrapped = vision._CaptionGenerator(generator, {}, clean=False)
    assert wrapped.zones == generator.zones == {name: 'local' for name in MODELS}
    assert wrapped.zones is not None


@pytest.mark.parametrize('zone,reported,label', [
    ('local', True, 'an endpoint on our own infrastructure'),
    ('fixture', True, 'a zone that is neither cloud nor local'),
    (None, True, 'a configuration that reports no zone for the endpoint'),
    (None, False, 'a configuration with no zone function at all'),
])
def test_a_non_cloud_or_unplaceable_zone_is_still_refused(quality_first_run, monkeypatch,
                                                          zone, reported, label):
    """Cases 2 and 3 — fail closed. Absent metadata is refused exactly like local."""
    store, job = quality_first_run
    generator = build_generator(zone, reported=reported)
    monkeypatch.setattr(vision, 'configured_generator', lambda: generator)

    with run_context(store, job['payload'], job) as ctx:
        generated = vision.generate('Describe the image', image())
        attempts = AttemptHistory(store._db).list_run(ctx.owner_id, ctx.scan_id, ctx.run_id)
        deferred = [item['reason'] for item in ctx.deferred]

    assert generated['deferred'] is True, label
    assert generated['reason'] == 'quality_first_cloud_endpoint_required', label
    assert deferred == ['quality_first_cloud_endpoint_required'], label
    # Refused BEFORE admission: no reservation, no attempt row, no traced call.
    assert attempts == []
    assert store.list_ai_calls('scan') == []


def test_one_unplaceable_model_refuses_the_whole_chain(quality_first_run, monkeypatch):
    """A partial map is not a pass. The refusal is per-chain, not per-model."""
    store, job = quality_first_run
    generator = build_generator('cloud')
    generator.zones = {MODELS[0]: 'cloud'}          # second model absent from the map
    monkeypatch.setattr(vision, 'configured_generator', lambda: generator)
    with run_context(store, job['payload'], job):
        generated = vision.generate('Describe the image', image())
    assert generated['reason'] == 'quality_first_cloud_endpoint_required'


def test_the_zone_refusal_is_what_paused_vision_recovery():
    """The `vision_recovery_unresolved` blocker is downstream of this deferral.

    `quality_first_cloud_endpoint_required` is in neither TRANSIENT nor ACTION_BLOCKS, so
    `_recovery_block` falls through to its catch-all — "any non-transient reason" — and pauses
    automatic retry. That is the blocker the user saw, and it is a CONSEQUENCE of the dropped
    zone map, not a second defect: with nothing deferred the same call returns no block.

    Recovery is still not self-healing. `schedule` records the block and enqueues no retry, and
    `process` refuses a third (`retry not in (1, 2)`), so an exhausted file resumes only through
    `schedule_existing_pending`. Asserted so that is a stated property, not an assumption.
    """
    import vision_recovery

    class HealthyLedger:
        """The real production ledger state: nothing reserved, nothing blocked."""
        @staticmethod
        def snapshot(owner_id, run_id):
            return {'blocked': False, 'available_units': 25_000_000}

    def context(reasons):
        return SimpleNamespace(deferred=[{'reason': r} for r in reasons], enabled=True,
                              local_drafting=False, ledger=HealthyLedger(),
                              owner_id='owner', run_id='run', scan_id='scan', file='a.docx')

    assert 'quality_first_cloud_endpoint_required' not in vision_recovery.TRANSIENT
    assert 'quality_first_cloud_endpoint_required' not in vision_recovery.ACTION_BLOCKS
    assert vision_recovery._recovery_block(
        context(['quality_first_cloud_endpoint_required'])) == 'vision_recovery_unresolved'
    assert vision_recovery._recovery_block(context([])) is None


def test_the_managed_dispatch_path_reads_the_wrapper_not_the_wrapped(quality_first_run, monkeypatch):
    """The minimal shape of the defect, straight at `managed_generate_attempts`.

    `vision.generate` above is the production path; this one names the contract, so a future
    wrapper that drops `zones` fails here with the cause rather than three layers away.
    """
    store, job = quality_first_run
    def post(*args, **kwargs):
        return Response(result(model=kwargs['json']['model'], text=CAPTION))
    wrapped = vision._CaptionGenerator(build_generator('cloud', post=post), {}, clean=False)
    with run_context(store, job['payload'], job) as ctx:
        outcome = managed_generate_attempts('Describe the image', ctx, wrapped, image_prefix=False)
    assert outcome.get('reason') != 'quality_first_cloud_endpoint_required'
    assert outcome['text'] == CAPTION

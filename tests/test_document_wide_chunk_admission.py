import json
import os
from concurrent.futures import ThreadPoolExecutor

import pytest

from ai_spending_budget import BudgetExceeded
from document_wide_chunk_admission import ChunkPlanAdmission
from test_document_wide_chunk_store import (OWNER, SID, RUN, PLAN, FILE,
                                            seed, plan, chunks)


@pytest.fixture(autouse=True)
def unit_tests_keep_their_isolated_sqlite(request, monkeypatch):
    if not request.node.name.startswith('test_postgres_'):
        import store as store_mod
        monkeypatch.setattr(store_mod, '_DATABASE_URL', '')


def admitted_chunks():
    result = chunks()
    for index, chunk in enumerate(result):
        chunk.update(attempt_id=f'{PLAN}:chunk:{index}', max_cost_units=20,
                     pricing_ref='fixture-price-v1')
    return result


def prepare(store, monkeypatch, cap=100):
    seed(store, monkeypatch)
    with store._db.cursor() as cur:
        store._db.execute(cur, 'UPDATE ai_spending_budgets SET cap_units=%s '
                                  'WHERE owner_id=%s AND run_id=%s',
                          (cap, OWNER, RUN))
    return ChunkPlanAdmission(store)


def counts(store):
    with store._db.cursor() as cur:
        store._db.execute(cur, 'SELECT COUNT(*) AS n FROM document_wide_chunk_plans')
        plans = store._db.fetchone(cur)['n']
        store._db.execute(cur, 'SELECT COUNT(*) AS n FROM document_wide_chunks')
        chunks_count = store._db.fetchone(cur)['n']
        store._db.execute(cur, 'SELECT COUNT(*) AS n FROM ai_spending_attempts')
        attempts = store._db.fetchone(cur)['n']
    return plans, chunks_count, attempts


def test_admission_commits_exact_plan_and_budget_batch_together(isolated_store, monkeypatch):
    admission = prepare(isolated_store, monkeypatch)
    expected = {'plan_id': PLAN, 'attempt_ids': [f'{PLAN}:chunk:0', f'{PLAN}:chunk:1'],
                'state': 'admitted'}
    assert admission.admit(plan(), admitted_chunks()) == expected
    assert admission.admit(plan(), admitted_chunks()) == expected
    assert counts(isolated_store) == (1, 2, 2)
    assert admission.ledger.snapshot(OWNER, RUN)['held_units'] == 40
    with isolated_store._db.cursor() as cur:
        isolated_store._db.execute(cur, 'SELECT plan_json FROM document_wide_chunk_plans')
        saved = json.loads(isolated_store._db.fetchone(cur)['plan_json'])
    assert [row['chunk_id'] for row in saved['budget_attempts']] == ['c1', 'c2']
    assert [row['attempt_id'] for row in saved['budget_attempts']] == expected['attempt_ids']


def test_budget_refusal_leaves_no_plan_or_hold(isolated_store, monkeypatch):
    admission = prepare(isolated_store, monkeypatch, cap=39)
    with pytest.raises(BudgetExceeded):
        admission.admit(plan(), admitted_chunks())
    assert counts(isolated_store) == (0, 0, 0)


def test_stale_authority_leaves_no_plan_or_hold(isolated_store, monkeypatch):
    admission = prepare(isolated_store, monkeypatch, cap=100)
    monkeypatch.setattr(isolated_store, 'remediation_source_revision', lambda _sid: 'changed')
    with pytest.raises(ValueError, match='stale'):
        admission.admit(plan(), admitted_chunks())
    assert counts(isolated_store) == (0, 0, 0)


def test_failure_after_plan_insert_rolls_back_plan_and_reservations(isolated_store, monkeypatch):
    admission = prepare(isolated_store, monkeypatch)
    original = admission.ledger._reserve_many_locked

    def fail_after_reserving(cur, budget, normalized):
        original(cur, budget, normalized)
        raise RuntimeError('simulated process boundary')

    monkeypatch.setattr(admission.ledger, '_reserve_many_locked', fail_after_reserving)
    with pytest.raises(RuntimeError, match='process boundary'):
        admission.admit(plan(), admitted_chunks())
    assert counts(isolated_store) == (0, 0, 0)


def test_later_cancellation_releases_only_still_reserved_attempts(isolated_store, monkeypatch):
    admission = prepare(isolated_store, monkeypatch)
    admission.admit(plan(), admitted_chunks())
    admission.ledger.claim_dispatch(OWNER, RUN, f'{PLAN}:chunk:0')
    result = admission.cancel_reserved(OWNER, RUN, PLAN)
    assert result == {'plan_id': PLAN, 'released_attempt_ids': [f'{PLAN}:chunk:1']}
    with isolated_store._db.cursor() as cur:
        isolated_store._db.execute(cur, 'SELECT state FROM document_wide_chunk_plans '
                                        'WHERE plan_id=%s', (PLAN,))
        assert isolated_store._db.fetchone(cur)['state'] == 'cancelled'
        isolated_store._db.execute(cur, 'SELECT attempt_id,state FROM ai_spending_attempts '
                                        'ORDER BY attempt_id')
        states = [(row['attempt_id'], row['state']) for row in isolated_store._db.fetchall(cur)]
    assert states == [
            (f'{PLAN}:chunk:0', 'dispatched'), (f'{PLAN}:chunk:1', 'released')]
    with pytest.raises(ValueError):
        admission.admit(plan(), admitted_chunks())


def test_cancelled_all_reserved_plan_cannot_replay_as_admitted(isolated_store, monkeypatch):
    admission = prepare(isolated_store, monkeypatch)
    admission.admit(plan(), admitted_chunks())
    assert admission.cancel_reserved(OWNER, RUN, PLAN)['released_attempt_ids'] == [
        f'{PLAN}:chunk:0', f'{PLAN}:chunk:1']
    with pytest.raises(ValueError):
        admission.admit(plan(), admitted_chunks())
    assert admission.ledger.snapshot(OWNER, RUN)['held_units'] == 0


def test_different_plan_cannot_reuse_anothers_attempts_or_release_holds(
        isolated_store, monkeypatch):
    admission = prepare(isolated_store, monkeypatch)
    admission.admit(plan(), admitted_chunks())
    other = {**plan(), 'plan_id': 'other-plan'}
    other_chunks = admitted_chunks()
    other_chunks[0]['chunk_id'], other_chunks[1]['chunk_id'] = 'other-c1', 'other-c2'
    with pytest.raises(ValueError, match='derived from plan'):
        admission.admit(other, other_chunks)
    assert counts(isolated_store) == (1, 2, 2)
    assert admission.ledger.snapshot(OWNER, RUN)['held_units'] == 40


@pytest.mark.parametrize('change', ['duplicate_chunk', 'duplicate_attempt', 'missing_price'])
def test_invalid_ordered_binding_never_writes(isolated_store, monkeypatch, change):
    admission = prepare(isolated_store, monkeypatch)
    rows = admitted_chunks()
    if change == 'duplicate_chunk':
        rows[1]['chunk_id'] = rows[0]['chunk_id']
    elif change == 'duplicate_attempt':
        rows[1]['attempt_id'] = rows[0]['attempt_id']
    else:
        rows[1]['pricing_ref'] = None
    with pytest.raises((ValueError, RuntimeError)):
        admission.admit(plan(), rows)
    assert counts(isolated_store) == (0, 0, 0)


@pytest.mark.skipif(not os.getenv('DATABASE_URL'), reason='disposable Postgres not configured')
def test_postgres_concurrent_duplicate_admission_commits_one_exact_batch(monkeypatch):
    from conftest import require_disposable_postgres
    from urllib.parse import urlparse
    import store as store_mod

    url = os.environ['DATABASE_URL']
    parsed = urlparse(url)
    require_disposable_postgres(url)
    assert parsed.hostname in ('127.0.0.1', 'localhost')
    assert parsed.path in ('/acp_ci', '/acp_budget_test')
    monkeypatch.setattr(store_mod, '_DATABASE_URL', url)
    primary = store_mod.Store()
    with primary._db.cursor() as cur:
        require_disposable_postgres(url, conn=cur.connection)
        primary._db.execute(cur, 'TRUNCATE document_wide_chunks,document_wide_chunk_plans,'
                                 'ai_spending_attempts,ai_spending_run_policies,'
                                 'ai_spending_budgets,stage_executions CASCADE')
    prepare(primary, monkeypatch)

    def run():
        store = store_mod.Store()
        monkeypatch.setattr(store, 'remediation_source_revision', lambda _sid: 'remediation-source')
        monkeypatch.setattr(store, 'get_file_record',
                            lambda _sid, _file: {'corrected_sha256': 'a' * 64})
        try:
            return ChunkPlanAdmission(store).admit(plan(), admitted_chunks())
        finally:
            if store._db._pool:
                store._db._pool.closeall()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _n: run(), range(2)))
    assert results[0] == results[1]
    assert counts(primary) == (1, 2, 2)
    if primary._db._pool:
        primary._db._pool.closeall()


@pytest.mark.skipif(not os.getenv('DATABASE_URL'), reason='disposable Postgres not configured')
@pytest.mark.parametrize('invalidate', ['cancel', 'artifact'])
def test_postgres_invalidation_wins_before_admission_without_plan_or_hold(monkeypatch, invalidate):
    from conftest import require_disposable_postgres
    from urllib.parse import urlparse
    import psycopg2
    import store as store_mod

    url = os.environ['DATABASE_URL']
    parsed = urlparse(url)
    require_disposable_postgres(url)
    assert parsed.hostname in ('127.0.0.1', 'localhost')
    assert parsed.path in ('/acp_ci', '/acp_budget_test')
    monkeypatch.setattr(store_mod, '_DATABASE_URL', url)
    store = store_mod.Store()
    with store._db.cursor() as cur:
        require_disposable_postgres(url, conn=cur.connection)
        store._db.execute(cur, 'TRUNCATE document_wide_chunks,document_wide_chunk_plans,'
                               'ai_spending_attempts,ai_spending_run_policies,'
                               'ai_spending_budgets,stage_executions,file_records CASCADE')
    admission = prepare(store, monkeypatch)
    blocker = psycopg2.connect(url)
    try:
        with blocker.cursor() as cur:
            if invalidate == 'cancel':
                cur.execute("UPDATE stage_executions SET cancel_requested_at='now' "
                            "WHERE execution_id=%s", (RUN,))
            else:
                cur.execute("UPDATE file_records SET corrected_sha256=%s "
                            "WHERE scan_id=%s AND file=%s", ('b' * 64, SID, FILE))
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(admission.admit, plan(), admitted_chunks())
            # The invalidating writer owns the same authoritative row admission
            # must lock. It cannot validate an older snapshot and commit past it.
            import time
            time.sleep(.2)
            assert not future.done()
            blocker.commit()
            with pytest.raises(ValueError):
                future.result(timeout=5)
        assert counts(store) == (0, 0, 0)
    finally:
        blocker.rollback()
        blocker.close()
        if store._db._pool:
            store._db._pool.closeall()

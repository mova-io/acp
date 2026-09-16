"""Real SQLite and opt-in local Postgres; prices are synthetic test fixtures."""
import sys
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "api"))
from ai_spending_budget import (  # noqa: E402
    BudgetLedger, BudgetError, BudgetExceeded, UnknownPricing, AttemptConflict,
)
from store import _SQLiteAdapter, _PgAdapter  # noqa: E402


@pytest.fixture(params=["sqlite"] + (["postgres"] if os.getenv("ACP_BUDGET_TEST_PG_URL") else []))
def ledger(tmp_path, request):
    if request.param == "postgres":
        from conftest import require_disposable_postgres
        from urllib.parse import urlparse
        url = os.environ["ACP_BUDGET_TEST_PG_URL"]
        require_disposable_postgres(url)
        parsed = urlparse(url)
        assert parsed.hostname in ("127.0.0.1", "localhost")
        assert parsed.path == "/acp_budget_test", "only a dedicated disposable test DB is allowed"
        adapter = _PgAdapter(url)
    else:
        adapter = _SQLiteAdapter(str(tmp_path / "budget.db"))
    result = BudgetLedger(adapter)
    result.init_schema()
    if request.param == "postgres":
        with adapter.cursor() as cur:
            require_disposable_postgres(url, conn=cur.connection)
            # The full migration may also have installed run policy references.
            # This fixture is restricted above to a disposable localhost test DB.
            adapter.execute(cur, "TRUNCATE ai_spending_attempts, ai_spending_budgets CASCADE")
    result.create_budget("owner", "run", 100)
    yield result
    if request.param == "postgres" and adapter._pool:
        adapter._pool.closeall()


def independent_ledger(ledger):
    if isinstance(ledger.db, _SQLiteAdapter):
        return BudgetLedger(_SQLiteAdapter(ledger.db._path))
    return BudgetLedger(_PgAdapter(ledger.db._url))


def close(ledger):
    if isinstance(ledger.db, _PgAdapter) and ledger.db._pool:
        ledger.db._pool.closeall()


def reserve(ledger, attempt="a", cost=60):
    return ledger.reserve("owner", "run", attempt, cost, "fixture-price-v1")


def reserve_many(ledger, *attempts):
    return ledger.reserve_many("owner", "run", [
        {"attempt_id": attempt, "max_cost_units": cost,
         "pricing_ref": "fixture-price-v1"}
        for attempt, cost in attempts
    ])


def test_cap_is_reserved_before_dispatch(ledger):
    reserve(ledger)
    with pytest.raises(BudgetExceeded):
        reserve(ledger, "b", 41)
    assert ledger.snapshot("owner", "run")["held_units"] == 60
    assert ledger.claim_dispatch("owner", "run", "a") is True
    assert ledger.claim_dispatch("owner", "run", "a") is False
    ledger.settle("owner", "run", "a", 20)
    reserve(ledger, "b", 80)
    assert ledger.snapshot("owner", "run")["available_units"] == 0


def test_idempotence_and_conflicting_replays(ledger):
    first = reserve(ledger)
    assert reserve(ledger) == first
    with pytest.raises(AttemptConflict):
        reserve(ledger, cost=61)
    with pytest.raises(AttemptConflict):
        ledger.reserve("owner", "run", "a", 60, "different")
    ledger.release("owner", "run", "a")
    assert reserve(ledger)["state"] == "released"
    assert ledger.claim_dispatch("owner", "run", "a") is False
    ledger.release("owner", "run", "a")


def test_unknown_and_invalid_prices_fail_closed(ledger):
    for cost, price in [(None, "p"), (4, ""), (4, None)]:
        with pytest.raises(UnknownPricing):
            ledger.reserve("owner", "run", "a", cost, price)
    for cost in [-1, 1.5, True, "1", 2**63]:
        with pytest.raises(ValueError):
            reserve(ledger, cost=cost)
    assert ledger.snapshot("owner", "run")["held_units"] == 0


def test_uncertain_charge_blocks_retry_and_retains_hold(ledger):
    reserve(ledger)
    reserve(ledger, "waiting", 10)
    ledger.claim_dispatch("owner", "run", "a")
    ledger.mark_uncertain("owner", "run", "a")
    ledger.mark_uncertain("owner", "run", "a")
    with pytest.raises(BudgetError):
        reserve(ledger, "retry", 1)
    with pytest.raises(BudgetError):
        ledger.claim_dispatch("owner", "run", "waiting")
    with pytest.raises(AttemptConflict):
        ledger.release("owner", "run", "a")
    assert ledger.snapshot("owner", "run")["held_units"] == 70
    ledger.settle("owner", "run", "a", 30)
    assert not ledger.snapshot("owner", "run")["blocked"]
    reserve(ledger, "retry", 60)


def test_release_after_dispatch_requires_zero_charge_evidence(ledger):
    reserve(ledger)
    ledger.claim_dispatch("owner", "run", "a")
    with pytest.raises(AttemptConflict):
        ledger.release("owner", "run", "a")
    ledger.release("owner", "run", "a", confirmed_not_charged=True)
    assert ledger.snapshot("owner", "run")["available_units"] == 100


@pytest.mark.parametrize("actual", [11, 140])
def test_overrun_is_durable_and_blocks_even_with_budget_remaining(ledger, actual):
    reserve(ledger, cost=10)
    ledger.claim_dispatch("owner", "run", "a")
    assert ledger.settle("owner", "run", "a", actual)["state"] == "breached"
    ledger.settle("owner", "run", "a", actual)
    assert ledger.snapshot("owner", "run")["spent_units"] == actual
    with pytest.raises(BudgetError):
        reserve(ledger, "b", 1)
    with pytest.raises(AttemptConflict):
        ledger.settle("owner", "run", "a", 10)


def test_scope_and_immutable_cap(ledger):
    reserve(ledger)
    ledger.create_budget("other", "run", 100)
    ledger.reserve("other", "run", "a", 100, "fixture")
    assert ledger.snapshot("owner", "run")["held_units"] == 60
    ledger.create_budget("owner", "run", 100)
    with pytest.raises(AttemptConflict):
        ledger.create_budget("owner", "run", 101)
    with pytest.raises(AttemptConflict):
        ledger.create_budget("owner", "run", 100, "EUR")
    with pytest.raises(BudgetError):
        ledger.claim_dispatch("unknown", "run", "a")


def test_restart_keeps_dispatch_claim_and_exposure(ledger):
    reserve(ledger)
    ledger.claim_dispatch("owner", "run", "a")
    restarted = independent_ledger(ledger)
    restarted.init_schema()
    assert restarted.claim_dispatch("owner", "run", "a") is False
    assert restarted.snapshot("owner", "run")["held_units"] == 60
    with pytest.raises(BudgetExceeded):
        reserve(restarted, "b", 50)
    close(restarted)


def test_concurrent_independent_connections_cannot_overreserve(ledger):
    def worker(i):
        independent = independent_ledger(ledger)
        try:
            reserve(independent, str(i), 7)
            return True
        except BudgetExceeded:
            return False
        finally:
            close(independent)
    with ThreadPoolExecutor(max_workers=12) as pool:
        admitted = list(pool.map(worker, range(40)))
    assert sum(admitted) == 14
    assert ledger.snapshot("owner", "run")["held_units"] == 98


def test_many_is_all_or_none_and_exact_replay_is_ordered(ledger):
    with pytest.raises(BudgetExceeded):
        reserve_many(ledger, ("a", 60), ("b", 41))
    assert ledger.snapshot("owner", "run")["held_units"] == 0
    first = reserve_many(ledger, ("b", 40), ("a", 60))
    replay = reserve_many(ledger, ("b", 40), ("a", 60))
    assert [row["attempt_id"] for row in first] == ["b", "a"]
    assert replay == first
    assert ledger.snapshot("owner", "run")["held_units"] == 100


def test_many_conflict_and_duplicate_never_leave_partial_holds(ledger):
    reserve(ledger, "existing", 10)
    with pytest.raises(AttemptConflict):
        reserve_many(ledger, ("new", 10), ("existing", 11))
    with pytest.raises(AttemptConflict, match="duplicate"):
        reserve_many(ledger, ("duplicate", 10), ("duplicate", 10))
    snapshot = ledger.snapshot("owner", "run")
    assert snapshot["held_units"] == 10
    with pytest.raises(BudgetError):
        ledger.claim_dispatch("owner", "run", "new")
    with pytest.raises(BudgetError):
        ledger.claim_dispatch("owner", "run", "duplicate")


def test_many_mixed_replay_counts_only_new_exposure(ledger):
    reserve_many(ledger, ("existing", 40))
    rows = reserve_many(ledger, ("existing", 40), ("new", 60))
    assert [row["state"] for row in rows] == ["reserved", "reserved"]
    assert ledger.snapshot("owner", "run")["held_units"] == 100


@pytest.mark.parametrize("finish", ["settled", "released"])
def test_many_terminal_full_replay_returns_evidence_but_cannot_admit_new(ledger, finish):
    reserve_many(ledger, ("done", 40))
    if finish == "settled":
        ledger.claim_dispatch("owner", "run", "done")
        ledger.settle("owner", "run", "done", 20)
    else:
        ledger.release("owner", "run", "done")
    assert reserve_many(ledger, ("done", 40))[0]["state"] == finish
    assert ledger.claim_dispatch("owner", "run", "done") is False
    with pytest.raises(AttemptConflict, match="advanced"):
        reserve_many(ledger, ("done", 40), ("new", 1))
    with pytest.raises(BudgetError):
        ledger.claim_dispatch("owner", "run", "new")


def test_many_uncertain_batch_blocks_without_partial_reservations(ledger):
    reserve(ledger, "uncertain", 10)
    ledger.claim_dispatch("owner", "run", "uncertain")
    ledger.mark_uncertain("owner", "run", "uncertain")
    with pytest.raises(BudgetError):
        reserve_many(ledger, ("a", 20), ("b", 20))
    assert ledger.snapshot("owner", "run")["held_units"] == 10


def test_concurrent_many_batches_cannot_overreserve(ledger):
    def worker(prefix):
        independent = independent_ledger(ledger)
        try:
            reserve_many(independent, (prefix + "-1", 30), (prefix + "-2", 30))
            return True
        except BudgetExceeded:
            return False
        finally:
            close(independent)
    with ThreadPoolExecutor(max_workers=4) as pool:
        admitted = list(pool.map(worker, ["a", "b", "c", "d"]))
    assert sum(admitted) == 1
    assert ledger.snapshot("owner", "run")["held_units"] == 60


def test_many_commit_failure_returns_no_success_and_rolls_back(ledger):
    from contextlib import contextmanager
    original = ledger.db.cursor
    @contextmanager
    def failing_cursor():
        with original() as cur:
            yield cur
            raise OSError("simulated commit failure")
    ledger.db.cursor = failing_cursor
    with pytest.raises(OSError):
        reserve_many(ledger, ("a", 20), ("b", 20))
    ledger.db.cursor = original
    assert ledger.snapshot("owner", "run")["held_units"] == 0


def test_concurrent_duplicate_attempt_dispatches_once(ledger):
    def worker(_):
        independent = independent_ledger(ledger)
        try:
            reserve(independent)
            return independent.claim_dispatch("owner", "run", "a")
        finally:
            close(independent)
    with ThreadPoolExecutor(max_workers=10) as pool:
        assert sum(pool.map(worker, range(25))) == 1
    assert ledger.snapshot("owner", "run")["held_units"] == 60


def test_ambient_transaction_cannot_grant_uncommitted_permission(ledger):
    reserve(ledger)
    with ledger.db.transaction():
        with pytest.raises(BudgetError, match="ambient"):
            ledger.claim_dispatch("owner", "run", "a")
    assert ledger.claim_dispatch("owner", "run", "a") is True


def test_bad_transitions_and_settlement_replay(ledger):
    reserve(ledger)
    with pytest.raises(AttemptConflict):
        ledger.settle("owner", "run", "a", 10)
    with pytest.raises(AttemptConflict):
        ledger.mark_uncertain("owner", "run", "a")
    ledger.claim_dispatch("owner", "run", "a")
    ledger.settle("owner", "run", "a", 10)
    ledger.settle("owner", "run", "a", 10)
    with pytest.raises(AttemptConflict):
        ledger.settle("owner", "run", "a", 11)
    with pytest.raises(AttemptConflict):
        ledger.release("owner", "run", "a", confirmed_not_charged=True)


def test_commit_failure_never_returns_dispatch_permission(ledger):
    from contextlib import contextmanager
    reserve(ledger)
    original = ledger.db.cursor
    @contextmanager
    def failing_cursor():
        with original() as cur:
            yield cur
            raise OSError("simulated commit failure")
    ledger.db.cursor = failing_cursor
    with pytest.raises(OSError):
        ledger.claim_dispatch("owner", "run", "a")
    ledger.db.cursor = original
    assert ledger.claim_dispatch("owner", "run", "a") is True


def test_release_racing_dispatch_never_removes_billable_hold(ledger):
    from threading import Barrier
    reserve(ledger)
    barrier = Barrier(2)
    def worker(dispatch):
        independent = independent_ledger(ledger)
        try:
            barrier.wait()
            if dispatch:
                return independent.claim_dispatch("owner", "run", "a")
            try:
                independent.release("owner", "run", "a")
            except AttemptConflict:
                pass
        finally:
            close(independent)
    with ThreadPoolExecutor(max_workers=2) as pool:
        dispatched, _ = list(pool.map(worker, [True, False]))
    assert ledger.snapshot("owner", "run")["held_units"] == (60 if dispatched else 0)

"""A HITL PUT is one durable decision, including under database pool failure."""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from hitl_viewed import viewed_fields

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "api"))


@pytest.fixture()
def decision(monkeypatch):
    import core
    import store as store_mod

    monkeypatch.setattr(store_mod, "_SQLITE_PATH", Path(tempfile.mkdtemp()) / "atomic.db")
    st = store_mod.Store()
    st.init_scan_run("s1", "drive", 1, "t0", "rubric", "hash")
    item_id = st.enqueue_proposals("s1", "deck.pptx", "1.1.1", [{
        "locator": "ppt/slides/slide1.xml#Picture 1",
        "before": "",
        "proposed_value": "A chart of quarterly revenue.",
        "rationale": "draft",
        "source": "vision",
    }])
    monkeypatch.setattr(core, "store", st)
    monkeypatch.setattr(core, "fire_webhook", lambda *a, **k: None)
    from routes.hitl import HitlUpdate, hitl_update
    request = SimpleNamespace(state=SimpleNamespace(user_email="reviewer@example.com"))
    return st, item_id, hitl_update, HitlUpdate, request


def test_pool_failure_after_card_update_rolls_back_every_decision_write(
        decision, monkeypatch):
    """The old route had committed status and values before attempting the audit insert."""
    import psycopg2.pool

    st, item_id, hitl_update, HitlUpdate, request = decision
    viewed = viewed_fields(item_id, st)
    original_execute = st._db.execute

    def fail_at_audit(cur, sql, params=()):
        if "INSERT INTO decision_log" in sql:
            raise psycopg2.pool.PoolError("connection pool exhausted")
        return original_execute(cur, sql, params)

    monkeypatch.setattr(st._db, "execute", fail_at_audit)
    with pytest.raises(psycopg2.pool.PoolError):
        hitl_update(item_id, HitlUpdate(
            status="approved", approved_values=["Reviewer-authored description"], **viewed), request)

    row = st.get_hitl_item(item_id)
    assert row["status"] == "pending"
    assert "approved_value" not in row["proposals"][0]
    assert st.list_decisions("s1") == []
    assert st.list_jobs() == []


def test_exact_retry_is_idempotent_for_audit_job_and_telemetry(decision, monkeypatch):
    st, item_id, hitl_update, HitlUpdate, request = decision
    telemetry = []
    monkeypatch.setattr(st, "record_hitl_event", lambda *a, **k: telemetry.append((a, k)))
    body = HitlUpdate(status="approved", approved_values=["Reviewer-authored description"],
                      **viewed_fields(item_id, st))

    first = hitl_update(item_id, body, request)
    replay = hitl_update(item_id, body, request)

    assert replay == first
    assert [d["action"] for d in st.list_decisions("s1")] == ["hitl.approved"]
    assert [j["type"] for j in st.list_jobs()] == ["apply_approved_values"]
    assert len(telemetry) == 1


def test_request_id_replay_is_idempotent_and_stale_retry_cannot_overwrite(decision):
    st, item_id, hitl_update, HitlUpdate, request = decision
    first = HitlUpdate(status="approved", approved_values=["First description"],
                       request_id="request-1", **viewed_fields(item_id, st))
    hitl_update(item_id, first, request)

    # The exact transport retry succeeds even though its expected version is now old.
    replay = hitl_update(item_id, first, request)
    assert replay["decision_version"] == 1

    # A different request based on the old card is rejected and cannot overwrite the value.
    with pytest.raises(Exception) as exc:
        hitl_update(item_id, HitlUpdate(
            status="rejected", request_id="request-2", expected_version=0), request)
    assert getattr(exc.value, "status_code", None) == 409
    row = st.get_hitl_item(item_id)
    assert row["status"] == "approved"
    assert row["proposals"][0]["approved_value"] == "First description"
    assert len(st.list_decisions("s1")) == 1


def test_reusing_request_id_with_different_payload_is_conflict(decision):
    st, item_id, hitl_update, HitlUpdate, request = decision
    viewed = viewed_fields(item_id, st)
    hitl_update(item_id, HitlUpdate(
        status="approved", approved_values=["First description"],
        request_id="request-1", **viewed), request)

    with pytest.raises(Exception) as exc:
        hitl_update(item_id, HitlUpdate(
            status="approved", approved_values=["Changed description"],
            request_id="request-1", **viewed), request)
    assert getattr(exc.value, "status_code", None) == 409
    assert st.get_hitl_item(item_id)["proposals"][0]["approved_value"] == "First description"


def test_telemetry_runs_only_after_the_decision_commit(decision, monkeypatch):
    st, item_id, hitl_update, HitlUpdate, request = decision

    def observe_commit(*args, **kwargs):
        assert st.get_hitl_item(item_id)["status"] == "approved"
        assert [d["action"] for d in st.list_decisions("s1")] == ["hitl.approved"]

    monkeypatch.setattr(st, "record_hitl_event", observe_commit)
    hitl_update(item_id, HitlUpdate(status="approved", approved_values=[None],
                                    **viewed_fields(item_id, st)), request)


def test_postgres_replay_check_locks_the_review_row():
    """Concurrent PUTs must serialize before either decides whether it is a replay."""
    import contextlib
    import store as store_mod

    sql = []

    class Db:
        supports_for_update = True
        supports_skip_locked = True

        @contextlib.contextmanager
        def cursor(self):
            yield object()

        def execute(self, cur, statement, params=()):
            sql.append(statement)

        def fetchone(self, cur):
            return None

    st = store_mod.Store.__new__(store_mod.Store)
    st._db = Db()
    assert st._get_hitl_item_for_decision("item-1") is None
    assert sql == ["SELECT * FROM hitl_queue WHERE id=%s FOR UPDATE"]


def test_hitl_lock_does_not_infer_for_update_from_skip_locked():
    """Queue claiming and review-row locking are separate adapter capabilities."""
    import contextlib
    import store as store_mod

    sql = []

    class Db:
        supports_for_update = False
        supports_skip_locked = True

        @contextlib.contextmanager
        def cursor(self):
            yield object()

        def execute(self, cur, statement, params=()):
            sql.append(statement)

        def fetchone(self, cur):
            return None

    st = store_mod.Store.__new__(store_mod.Store)
    st._db = Db()
    assert st._get_hitl_item_for_decision("item-1") is None
    assert sql == ["SELECT * FROM hitl_queue WHERE id=%s"]

"""HITL status/notifications slice.

Tests:
  - claim_hitl_item: sets status=in_review without touching reviewed_at; sets assignee
  - PUT /hitl/queue/{id} accepts in_review and returns early (no side-effects)
  - PUT /hitl/queue/{id} rejects unknown statuses
  - fire_webhook: event param is forwarded correctly
  - hitl.assigned fires on PATCH assign (not on clear)
  - hitl.resolved fires on PUT when status is approved/rejected/skipped (not in_review)
"""
from __future__ import annotations
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "api"))


@pytest.fixture()
def st(monkeypatch):
    import store as store_mod
    monkeypatch.setattr(store_mod, "_SQLITE_PATH", Path(tempfile.mkdtemp()) / "sn.db")
    return store_mod.Store()


def _item(st, sid="s1", f="doc.pdf", rule="1.1.1"):
    st.init_scan_run(sid, "drive", 1, "t0", "r", "h")
    with st._db.cursor() as cur:
        st._db.execute(cur,
            "INSERT INTO file_records(scan_id,file,engine,status,score,compliant,skipped_rules,remediated_at) "
            "VALUES(%s,%s,'pdf','fail',60,0,0,'2026-08-01T00:00:00')", (sid, f))
    st.queue_hitl_deferral(sid, f, "needs attention", 1, rule_id=rule)
    return next(i for i in st.list_hitl_queue(scan_id=sid))


# ── claim_hitl_item store method ───────────────────────────────────────────────

def test_claim_sets_status_in_review(st):
    item = _item(st)
    result = st.claim_hitl_item(item["id"], "alice@example.com")
    assert result["status"] == "in_review"


def test_claim_sets_assignee(st):
    item = _item(st)
    result = st.claim_hitl_item(item["id"], "alice@example.com")
    assert result["assignee"] == "alice@example.com"


def test_claim_does_not_set_reviewed_at(st):
    item = _item(st)
    st.claim_hitl_item(item["id"], "alice@example.com")
    row = st.get_hitl_item(item["id"])
    assert row["reviewed_at"] is None


def test_claim_preserves_existing_assignee_when_claimant_is_none(st):
    item = _item(st)
    st.assign_hitl_item(item["id"], "bob@example.com")
    st.claim_hitl_item(item["id"], None)  # claimant=None preserves the existing assignee
    row = st.get_hitl_item(item["id"])
    assert row["assignee"] == "bob@example.com"
    assert row["status"] == "in_review"


# ── PUT /hitl/queue/{id} with in_review ───────────────────────────────────────

def _client(st, monkeypatch):
    import core as core_mod
    monkeypatch.setattr(core_mod, "store", st)
    monkeypatch.setattr(core_mod, "HITL_WEBHOOK", "")  # suppress webhook calls in most tests

    from fastapi.testclient import TestClient
    from fastapi import FastAPI
    from routes import hitl as hitl_routes

    app = FastAPI()
    app.include_router(hitl_routes.router)
    return TestClient(app)


def test_put_in_review_accepted(st, monkeypatch):
    client = _client(st, monkeypatch)
    item = _item(st)
    resp = client.put(f"/hitl/queue/{item['id']}", json={"status": "in_review"})
    assert resp.status_code == 200
    assert resp.json()["status"] == "in_review"


def test_put_in_review_no_reviewed_at(st, monkeypatch):
    client = _client(st, monkeypatch)
    item = _item(st)
    client.put(f"/hitl/queue/{item['id']}", json={"status": "in_review"})
    row = st.get_hitl_item(item["id"])
    assert row["reviewed_at"] is None


def test_put_unknown_status_rejected(st, monkeypatch):
    client = _client(st, monkeypatch)
    item = _item(st)
    resp = client.put(f"/hitl/queue/{item['id']}", json={"status": "flying"})
    assert resp.status_code == 422


# ── fire_webhook event parameter ──────────────────────────────────────────────

def _run_webhook_sync(core_mod, items, *, event="hitl.queued"):
    """Run fire_webhook synchronously by replacing Thread with a run-inline stub."""
    captured = {}
    import httpx

    def fake_post(url, *, json, timeout):
        captured.update(json)

    class SyncThread:
        def __init__(self, target, *, daemon=False):
            self._target = target

        def start(self):
            self._target()

    with patch.object(core_mod, "HITL_WEBHOOK", "http://example.com/hook"):
        with patch("threading.Thread", SyncThread):
            with patch.object(httpx, "post", side_effect=fake_post):
                core_mod.fire_webhook(items, event=event)
    return captured


def test_fire_webhook_default_event():
    import core as core_mod
    captured = _run_webhook_sync(core_mod, [{"id": "x"}])
    assert captured.get("event") == "hitl.queued"


def test_fire_webhook_custom_event():
    import core as core_mod
    captured = _run_webhook_sync(core_mod, [{"id": "x"}], event="hitl.resolved")
    assert captured.get("event") == "hitl.resolved"


# ── hitl.assigned webhook fires on assign (not on clear) ─────────────────────

def test_assign_fires_hitl_assigned_webhook(st, monkeypatch):
    import core as core_mod
    monkeypatch.setattr(core_mod, "store", st)

    fired = []
    monkeypatch.setattr(core_mod, "fire_webhook",
                        lambda items, *, event="hitl.queued": fired.append(event))

    from fastapi.testclient import TestClient
    from fastapi import FastAPI
    from routes import hitl as hitl_routes

    app = FastAPI()
    app.include_router(hitl_routes.router)
    client = TestClient(app)

    item = _item(st)
    client.patch(f"/hitl/queue/{item['id']}/assign", json={"assignee": "reviewer@example.com"})
    assert "hitl.assigned" in fired


def test_assign_clear_does_not_fire_webhook(st, monkeypatch):
    import core as core_mod
    monkeypatch.setattr(core_mod, "store", st)

    fired = []
    monkeypatch.setattr(core_mod, "fire_webhook",
                        lambda items, *, event="hitl.queued": fired.append(event))

    from fastapi.testclient import TestClient
    from fastapi import FastAPI
    from routes import hitl as hitl_routes

    app = FastAPI()
    app.include_router(hitl_routes.router)
    client = TestClient(app)

    item = _item(st)
    client.patch(f"/hitl/queue/{item['id']}/assign", json={"assignee": None})
    assert "hitl.assigned" not in fired


# ── hitl.resolved webhook fires on terminal decisions, not in_review ──────────

@pytest.mark.parametrize("status", ["approved", "rejected", "skipped"])
def test_terminal_status_fires_hitl_resolved(st, monkeypatch, status):
    import core as core_mod
    monkeypatch.setattr(core_mod, "store", st)

    fired = []
    monkeypatch.setattr(core_mod, "fire_webhook",
                        lambda items, *, event="hitl.queued": fired.append(event))

    from fastapi.testclient import TestClient
    from fastapi import FastAPI
    from routes import hitl as hitl_routes

    app = FastAPI()
    app.include_router(hitl_routes.router)
    client = TestClient(app)

    item = _item(st)
    body = {"status": status}
    if status == "approved":
        from hitl_viewed import viewed_fields
        body.update(viewed_fields(item["id"], st))
    if status == "rejected":
        body["reject_reason"] = "other"
    client.put(f"/hitl/queue/{item['id']}", json=body)
    assert "hitl.resolved" in fired


def test_in_review_does_not_fire_hitl_resolved(st, monkeypatch):
    import core as core_mod
    monkeypatch.setattr(core_mod, "store", st)

    fired = []
    monkeypatch.setattr(core_mod, "fire_webhook",
                        lambda items, *, event="hitl.queued": fired.append(event))

    from fastapi.testclient import TestClient
    from fastapi import FastAPI
    from routes import hitl as hitl_routes

    app = FastAPI()
    app.include_router(hitl_routes.router)
    client = TestClient(app)

    item = _item(st)
    client.put(f"/hitl/queue/{item['id']}", json={"status": "in_review"})
    assert "hitl.resolved" not in fired

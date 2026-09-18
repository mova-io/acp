"""PUT /hitl/queue/{id} writes one PRIMARY event per decision, whatever the card held.

The store-level grain is pinned in test_hitl_decision_grain.py. This pins the writer: the
production path that fans a decision out across a card's model calls has to mark exactly one of
those rows as the decision, and has to record the decision at all when there is no model call to
attribute it to.
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from hitl_viewed import viewed_fields

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "api"))

OWNER = "deva@example.org"


@pytest.fixture()
def st(monkeypatch):
    import core
    import store as store_mod

    monkeypatch.setattr(store_mod, "_SQLITE_PATH", Path(tempfile.mkdtemp()) / "hitl-fanout.db")
    value = store_mod.Store()
    monkeypatch.setattr(core, "store", value)
    monkeypatch.setattr(core, "HITL_WEBHOOK", "")
    return value


def _client():
    from routes import hitl

    app = FastAPI()

    @app.middleware("http")
    async def identity(request: Request, call_next):
        request.state.user_email = OWNER
        return await call_next(request)

    app.include_router(hitl.router)
    return TestClient(app)


def _seed(st, drafts=3, ai_indices=None):
    """A 1.1.1 card of `drafts` images. `ai_indices` are the ones a vision call drafted; the
    rest carry no model_call_id, which is how a human-authored instance reaches the card."""
    ai_indices = range(drafts) if ai_indices is None else ai_indices
    st.init_scan_run("scan-1", "drive", 1, "t0", "r", "h", owner=OWNER)
    with st._db.cursor() as cur:
        st._db.execute(cur,
            "INSERT INTO file_records(scan_id,file,engine,status,score,compliant,skipped_rules) "
            "VALUES(%s,%s,'docx','fail',60,0,0)", ("scan-1", "report.docx"))
    st.queue_hitl_deferral("scan-1", "report.docx", "describe these", drafts, rule_id="1.1.1")
    # The route refuses a call id it cannot trace to this (scan, file), so the provenance rows
    # have to exist before the decision references them.
    for i in ai_indices:
        st.record_ai_call(surface="vision", provider="ollama", model="llava:13b", zone="local",
                          latency_ms=1200, ok=True, scan_id="scan-1", file="report.docx",
                          call_identity=f"call-{i}")
    return st.enqueue_proposals("scan-1", "report.docx", "1.1.1", [
        {"locator": f"image{i}", "before": "", "proposed_value": f"draft {i}",
         "rationale": "vision draft", "source": "ai",
         **({"model_call_id": f"call-{i}"} if i in ai_indices else {})}
        for i in range(drafts)])


def _events(st):
    with st._db.cursor() as cur:
        st._db.execute(cur,
            "SELECT action,decision_primary,model_call_id FROM hitl_events WHERE scan_id=%s",
            ("scan-1",))
        return [dict(r) for r in st._db.fetchall(cur)]


def test_one_primary_row_per_decision(st):
    item_id = _seed(st, drafts=3)
    response = _client().put(f"/hitl/queue/{item_id}", json={
        "status": "approved", **viewed_fields(item_id, st),
        "approved_values": ["draft 0", "draft 1", "draft 2"],
        "model_call_ids": ["call-0", "call-1", "call-2"],
        "review_ms": 45000,
    })
    assert response.status_code == 200

    rows = _events(st)
    assert len(rows) == 3                                        # every draft still recorded
    assert sum(r["decision_primary"] for r in rows) == 1         # one decision
    assert st.hitl_analytics("scan-1")["total"] == 1
    assert st.hitl_analytics("scan-1")["timed_reviews"] == 1


def test_primary_is_the_first_row_written_not_the_first_index(st):
    """Proposal 0 is human-authored (no call id), so the loop skips it. The decision must still
    be marked — keying the flag on `index == 0` would leave this burst with no primary row and
    the review uncounted."""
    item_id = _seed(st, drafts=3, ai_indices=(1, 2))
    response = _client().put(f"/hitl/queue/{item_id}", json={
        "status": "approved", **viewed_fields(item_id, st),
        "approved_values": ["hand written", "draft 1", "draft 2"],
        "model_call_ids": [None, "call-1", "call-2"],
        "review_ms": 30000,
    })
    assert response.status_code == 200

    rows = _events(st)
    assert len(rows) == 2
    assert sum(r["decision_primary"] for r in rows) == 1
    assert st.hitl_analytics("scan-1")["total"] == 1


def test_a_decision_with_no_attributable_call_is_still_recorded(st):
    """Every id blank means "no model call to attribute this to" — which is the human-authored
    case, not a reason to record nothing. Before the fallback the loop wrote zero rows and the
    decision was invisible to approval rate, review time and the maturity gate alike."""
    item_id = _seed(st, drafts=2, ai_indices=())
    response = _client().put(f"/hitl/queue/{item_id}", json={
        "status": "approved", **viewed_fields(item_id, st),
        "approved_values": ["hand written", "also hand written"],
        "model_call_ids": [None, None],
        "review_ms": 12000,
    })
    assert response.status_code == 200

    rows = _events(st)
    assert len(rows) == 1
    assert rows[0]["decision_primary"] == 1
    assert rows[0]["model_call_id"] is None
    analytics = st.hitl_analytics("scan-1")
    assert analytics["total"] == 1
    assert analytics["timed_reviews"] == 1


def test_analytics_endpoint_is_owner_scoped_without_a_scan_id(st):
    _seed(st, drafts=1)
    st.init_scan_run("scan-other", "drive", 1, "t0", "r", "h", owner="someone@else.org")
    st.record_hitl_event("scan-other", "theirs.docx", "1.1.1", "x1", "approve", review_ms=1000)
    st.record_hitl_event("scan-1", "report.docx", "1.1.1", "y1", "approve", review_ms=1000)

    body = _client().get("/hitl/analytics").json()
    assert body["total"] == 1                                    # not the other tenant's row

"""WCAG-exception resolutions (the HITL-six close-out) — the two findings a model must not decide.

A reviewer can resolve a finding by applying the standard's own exception instead of authoring a
fix: 'decorative' (WCAG 1.1.1 — the image conveys nothing, so no text alternative is required) and
'essential_exception' (WCAG 1.4.5/1.4.9 — a logo/brand mark is exempt from images-of-text). The
resolution is recorded in the immutable audit trail as WHY the finding was resolved, and — crucially
— writes NO value into the document, so nothing is misrepresented as an authored fix.
"""
from __future__ import annotations
import sys
import tempfile
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from hitl_viewed import viewed_fields

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "api"))


@pytest.fixture()
def st(monkeypatch):
    import store as store_mod
    monkeypatch.setattr(store_mod, "_SQLITE_PATH", Path(tempfile.mkdtemp()) / "res.db")
    return store_mod.Store()


def _req(user_email="reviewer@example.com"):
    return SimpleNamespace(state=SimpleNamespace(user_email=user_email))


def _row(st, sid="s1", f="deck.pptx", rule="1.1.1"):
    st.init_scan_run(sid, "drive", 1, "t0", "r", "h")
    with st._db.cursor() as cur:
        st._db.execute(cur,
            "INSERT INTO file_records(scan_id,file,engine,status,score,compliant,skipped_rules,remediated_at) "
            "VALUES(%s,%s,'office','fail',60,0,0,'2026-07-10T00:00:00')", (sid, f))
    st.queue_hitl_deferral(sid, f, "image lacks a faithful alt source", 1, rule_id=rule)
    return next(i for i in st.list_hitl_queue(scan_id=sid))


@pytest.fixture()
def route(st, monkeypatch):
    import core
    monkeypatch.setattr(core, "store", st)
    monkeypatch.setattr(core, "fire_webhook", lambda *a, **k: None, raising=False)
    jobs, decisions = [], []
    monkeypatch.setattr(st, "enqueue_job",
                        lambda name, payload, **k: jobs.append((name, payload)), raising=False)
    _orig = st.log_decision
    def _capture(actor, action, **k):
        decisions.append({"actor": actor, "action": action, **k})
        return _orig(actor, action, **k)
    monkeypatch.setattr(st, "log_decision", _capture, raising=False)
    from routes.hitl import hitl_update, HitlUpdate
    return hitl_update, HitlUpdate, jobs, decisions


def test_decorative_resolves_the_finding(st, route):
    hitl_update, HitlUpdate, _jobs, _dec = route
    row = _row(st, rule="1.1.1")
    hitl_update(row["id"], HitlUpdate(status="approved", **viewed_fields(row["id"]), resolution="decorative"), _req())
    assert st.get_hitl_item(row["id"])["status"] == "approved"


def test_decorative_records_the_exception_in_the_audit_trail(st, route):
    hitl_update, HitlUpdate, _jobs, decisions = route
    row = _row(st, rule="1.1.1")
    hitl_update(row["id"], HitlUpdate(status="approved", **viewed_fields(row["id"]), resolution="decorative"), _req())
    hitl_line = next(d for d in decisions if d["action"] == "hitl.approved")
    assert "decorative" in (hitl_line.get("detail") or "")
    assert "1.1.1" in (hitl_line.get("detail") or "")


def test_essential_exception_records_the_logo_exemption(st, route):
    hitl_update, HitlUpdate, _jobs, decisions = route
    row = _row(st, rule="1.4.5")
    hitl_update(row["id"], HitlUpdate(status="approved", **viewed_fields(row["id"]), resolution="essential_exception"), _req())
    hitl_line = next(d for d in decisions if d["action"] == "hitl.approved")
    assert "essential" in (hitl_line.get("detail") or "").lower()


def test_out_of_scope_resolves_a_finding_as_not_applicable(st, route):
    # A third resolution: the criterion does not APPLY to this document (out of scope / N/A). Accepted,
    # persisted on the row (status stays approved so it never blocks certification), and audited.
    hitl_update, HitlUpdate, _jobs, decisions = route
    row = _row(st, rule="1.4.5")
    hitl_update(row["id"], HitlUpdate(status="approved", **viewed_fields(row["id"]), resolution="out_of_scope"), _req())
    item = st.get_hitl_item(row["id"])
    assert item["status"] == "approved"
    assert item["resolution"] == "out_of_scope"
    hitl_line = next(d for d in decisions if d["action"] == "hitl.approved")
    assert "out of scope" in (hitl_line.get("detail") or "").lower()


def test_a_resolution_writes_no_value_into_the_document(st, route):
    """The whole point: an exception is NOT a written fix. No VALUE may be written, or the report
    would claim alt text was authored when the reviewer explicitly said none was needed.

    This row is a deferral — no proposals, so nothing addressable — and no job fires at all. A
    'decorative' resolution on a row that DOES carry proposal locators schedules the job for the
    OOXML decorative MARKING, which is a marking and not a value; that lane and the reason it
    exists live in tests/test_wcag_exception_resolution_writeback.py."""
    hitl_update, HitlUpdate, jobs, _dec = route
    row = _row(st, rule="1.1.1")
    hitl_update(row["id"], HitlUpdate(status="approved", **viewed_fields(row["id"]), resolution="decorative"), _req())
    assert not any(name == "apply_approved_values" for name, _ in jobs)


def test_an_unknown_resolution_is_rejected(st, route):
    from fastapi import HTTPException
    hitl_update, HitlUpdate, _jobs, _dec = route
    row = _row(st)
    with pytest.raises(HTTPException) as ei:
        hitl_update(row["id"], HitlUpdate(status="approved", **viewed_fields(row["id"]), resolution="made_up"), _req())
    assert ei.value.status_code == 422


def test_no_resolution_is_the_unchanged_default(st, route):
    """Every existing approval sends no resolution — behaviour must be identical to before."""
    hitl_update, HitlUpdate, _jobs, decisions = route
    row = _row(st, rule="1.1.1")
    hitl_update(row["id"], HitlUpdate(status="approved", **viewed_fields(row["id"])), _req())
    hitl_line = next(d for d in decisions if d["action"] == "hitl.approved")
    assert "resolution:" not in (hitl_line.get("detail") or "")


def test_review_decision_persists_its_exact_model_call(st, route):
    hitl_update, HitlUpdate, _jobs, _dec = route
    row = _row(st, rule="2.4.4")
    call_id = st.record_ai_call(surface="suggest", provider="anthropic", model="claude",
                                zone="cloud", latency_ms=80, ok=True,
                                scan_id="s1", file="deck.pptx")
    hitl_update(row["id"], HitlUpdate(status="approved", **viewed_fields(row["id"]), model_call_id=call_id), _req())
    with st._db.cursor() as cur:
        st._db.execute(cur, "SELECT model_call_id FROM hitl_events WHERE item_id=%s", (row["id"],))
        assert st._db.fetchone(cur)["model_call_id"] == call_id


def test_review_rejects_a_model_call_from_another_file(st, route):
    from fastapi import HTTPException
    hitl_update, HitlUpdate, _jobs, _dec = route
    row = _row(st, rule="2.4.4")
    call_id = st.record_ai_call(surface="suggest", provider="anthropic", model="claude",
                                zone="cloud", latency_ms=80, ok=True,
                                scan_id="s1", file="other.docx")
    with pytest.raises(HTTPException) as error:
        hitl_update(row["id"], HitlUpdate(status="approved", **viewed_fields(row["id"]), model_call_id=call_id), _req())
    assert error.value.status_code == 422
    assert st.get_hitl_item(row["id"])["status"] == "pending"


def test_multi_instance_review_records_each_exact_vision_call(st, route):
    """A collapsed card is one decision but each generated alt has its own producer."""
    hitl_update, HitlUpdate, _jobs, _dec = route
    row = _row(st)
    call_ids = [st.record_ai_call(surface="vision", provider="ollama", model="llava",
                                  zone="local", latency_ms=80, ok=True,
                                  scan_id="s1", file="deck.pptx") for _ in range(2)]
    evidence = [
        {"locator": "slide1#Picture 1", "proposed_value": "A chart", "model_call_id": call_ids[0]},
        {"locator": "slide2#Picture 2", "proposed_value": "A map", "model_call_id": call_ids[1]},
    ]
    with st._db.cursor() as cur:
        st._db.execute(cur, "UPDATE hitl_queue SET evidence=%s WHERE id=%s",
                       (json.dumps(evidence), row["id"]))
    hitl_update(row["id"], HitlUpdate(status="approved", **viewed_fields(row["id"]),
        approved_values=["A quarterly chart", "A map"], model_call_ids=call_ids), _req())
    with st._db.cursor() as cur:
        st._db.execute(cur, "SELECT model_call_id,ai_value,final_value,edited FROM hitl_events "
                           "WHERE item_id=%s ORDER BY final_value", (row["id"],))
        events = st._db.fetchall(cur)
    assert {event["model_call_id"] for event in events} == set(call_ids)
    assert {(event["ai_value"], event["final_value"], event["edited"]) for event in events} == {
        ("A chart", "A quarterly chart", 1), ("A map", "A map", 0)}


def test_multi_instance_review_rejects_a_foreign_call_before_recording(st, route):
    from fastapi import HTTPException
    hitl_update, HitlUpdate, _jobs, _dec = route
    row = _row(st)
    own = st.record_ai_call(surface="vision", provider="ollama", model="llava", zone="local",
                            latency_ms=80, ok=True, scan_id="s1", file="deck.pptx")
    foreign = st.record_ai_call(surface="vision", provider="ollama", model="llava", zone="local",
                                latency_ms=80, ok=True, scan_id="other-scan", file="deck.pptx")
    with pytest.raises(HTTPException) as error:
        hitl_update(row["id"], HitlUpdate(status="approved", **viewed_fields(row["id"]),
                    model_call_ids=[own, foreign]), _req())
    assert error.value.status_code == 422
    assert st.get_hitl_item(row["id"])["status"] == "pending"


def test_multi_instance_review_rejects_a_same_file_call_attached_to_the_wrong_value(st, route):
    """File ownership is necessary but insufficient when the proposal records its producer."""
    from fastapi import HTTPException
    hitl_update, HitlUpdate, _jobs, _dec = route
    row = _row(st)
    call_ids = [st.record_ai_call(surface="vision", provider="ollama", model="llava",
                                  zone="local", latency_ms=80, ok=True,
                                  scan_id="s1", file="deck.pptx") for _ in range(2)]
    evidence = [
        {"locator": "slide1#Picture 1", "proposed_value": "A chart", "model_call_id": call_ids[0]},
        {"locator": "slide2#Picture 2", "proposed_value": "A map", "model_call_id": call_ids[1]},
    ]
    with st._db.cursor() as cur:
        st._db.execute(cur, "UPDATE hitl_queue SET evidence=%s WHERE id=%s",
                       (json.dumps(evidence), row["id"]))
    with pytest.raises(HTTPException) as error:
        hitl_update(row["id"], HitlUpdate(status="approved", **viewed_fields(row["id"]),
                    model_call_ids=list(reversed(call_ids))), _req())
    assert error.value.status_code == 422
    assert st.get_hitl_item(row["id"])["status"] == "pending"

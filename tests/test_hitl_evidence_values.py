"""Per-image approved values for a DEFERRED alt-text row — the images with no AI draft.

PR 62 gave every AI-drafted image (`proposals`) its own approved value, keyed positionally, and a
write-back that applies them. But a deferred 1.1.1 row carries `evidence` [{locator, thumb}] and
NO proposals: Office images with no faithful alt source, described from scratch because the vision
model produced nothing. For those the reviewer's text had nowhere per-image to live — it went into
the single legacy `approved_value` column with no locator, so `apply_alt` could never write it and
the file was blocked forever.

`approve_proposal_values` now writes onto the `evidence` entries when a row has no proposals, and
`_row_approved_values` folds those in — so the gate, `approved_alt_values`, and `apply_alt` all
work unchanged, keyed by locator. Evidence has no draft, so an undescribed image contributes
nothing (no proposed_value fallback).
"""
from __future__ import annotations
import sys
import tempfile
from pathlib import Path

import pytest
from hitl_viewed import viewed_fields

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "api"))

_EV = [{"locator": "ppt/slides/slide1.xml#rId2", "thumb": "data:image/png;base64,AAAA"},
       {"locator": "ppt/slides/slide2.xml#rId3", "thumb": "data:image/png;base64,BBBB"},
       {"locator": "ppt/slides/slide3.xml#rId4", "thumb": "data:image/png;base64,CCCC"}]


@pytest.fixture()
def st(monkeypatch):
    import store as store_mod
    monkeypatch.setattr(store_mod, "_SQLITE_PATH", Path(tempfile.mkdtemp()) / "evval.db")
    return store_mod.Store()


def _deferred_row(st, sid="s1", f="deck.pptx", n=3):
    """A deferred 1.1.1 row with n evidence images and no proposals — vision drafted nothing."""
    st.init_scan_run(sid, "drive", 1, "t0", "r", "h")
    with st._db.cursor() as cur:
        st._db.execute(cur,
            "INSERT INTO file_records(scan_id,file,engine,status,score,compliant,skipped_rules,remediated_at) "
            "VALUES(%s,%s,'office','fail',60,0,0,'2026-07-10T00:00:00')", (sid, f))
    st.queue_hitl_deferral(sid, f, f"{n} images lack a faithful alt source", n)
    st.attach_hitl_evidence(sid, f, "1.1.1", _EV[:n])
    return next(i for i in st.list_hitl_queue(scan_id=sid))


# ---------------------------------------------------------------- storage

def test_values_are_written_onto_evidence_positionally(st):
    row = _deferred_row(st)
    n = st.approve_proposal_values(row["id"], ["a nurse", "a bar chart", "the logo"])
    assert n == 3
    got = st.get_hitl_item(row["id"])
    vals = {e["locator"]: e.get("approved_value") for e in got["evidence"]}
    assert vals == {
        "ppt/slides/slide1.xml#rId2": "a nurse",
        "ppt/slides/slide2.xml#rId3": "a bar chart",
        "ppt/slides/slide3.xml#rId4": "the logo",
    }


def test_a_partial_set_leaves_the_rest_undescribed(st):
    row = _deferred_row(st)
    n = st.approve_proposal_values(row["id"], ["only the first", "", None])
    assert n == 1
    ev = st.get_hitl_item(row["id"])["evidence"]
    assert ev[0].get("approved_value") == "only the first"
    assert "approved_value" not in ev[1]
    assert "approved_value" not in ev[2]


def test_evidence_has_no_draft_fallback(st):
    """A proposal falls back to its own draft; an undescribed evidence image must not — there is
    no draft, and inventing one would write alt text the reviewer never saw."""
    row = _deferred_row(st)
    assert st.approve_proposal_values(row["id"], [None, None, None]) == 0
    assert all("approved_value" not in e for e in st.get_hitl_item(row["id"])["evidence"])


def test_clearing_a_value_removes_it(st):
    row = _deferred_row(st)
    st.approve_proposal_values(row["id"], ["described", "", ""])
    st.approve_proposal_values(row["id"], ["   ", "", ""])   # blanked
    assert all("approved_value" not in e for e in st.get_hitl_item(row["id"])["evidence"])


def test_evidence_does_not_become_a_proposal(st):
    """Evidence keeps its own column; writing values must not fabricate proposals (confidence.js
    would then report an AI proposal that never existed)."""
    row = _deferred_row(st)
    st.approve_proposal_values(row["id"], ["a nurse", "", ""])
    assert not st.get_hitl_item(row["id"]).get("proposals")


# ---------------------------------------------------------------- write-back flow

def test_the_gate_counts_a_described_evidence_row(st):
    """Described-but-unwritten content must block certification, exactly like a proposal value."""
    row = _deferred_row(st)
    st.update_hitl_item(row["id"], "approved", None, None)
    st.approve_proposal_values(row["id"], ["a nurse", "a bar chart", "the logo"])
    assert st.count_unapplied_approved_values("s1", "deck.pptx") == 1
    assert st.mark_file_compliant_if_reviewed("s1", "deck.pptx") is False


def test_approved_alt_values_returns_the_evidence_descriptions(st):
    """apply_alt writes by locator; it neither knows nor cares the value came from evidence."""
    row = _deferred_row(st)
    st.update_hitl_item(row["id"], "approved", None, None)
    st.approve_proposal_values(row["id"], ["a nurse", "a bar chart", "the logo"])
    assert st.approved_alt_values("s1", "deck.pptx") == {
        "ppt/slides/slide1.xml#rId2": "a nurse",
        "ppt/slides/slide2.xml#rId3": "a bar chart",
        "ppt/slides/slide3.xml#rId4": "the logo",
    }


def test_an_undescribed_evidence_row_writes_nothing(st):
    row = _deferred_row(st)
    st.update_hitl_item(row["id"], "approved", None, None)
    st.approve_proposal_values(row["id"], [None, None, None])
    assert st.approved_alt_values("s1", "deck.pptx") == {}


def test_applying_lets_the_file_certify(st):
    """Once the evidence values are written in and the row marked applied, the promise is kept."""
    row = _deferred_row(st)
    st.update_hitl_item(row["id"], "approved", None, None)
    st.approve_proposal_values(row["id"], ["a nurse", "a bar chart", "the logo"])
    assert st.count_unapplied_approved_values("s1", "deck.pptx") == 1
    st.mark_row_applied(row["id"])
    assert st.count_unapplied_approved_values("s1", "deck.pptx") == 0


# ---------------------------------------------------------------- the route seam
# The store can be perfect while the route never calls it, or the apply job never fires.

def _req(user_email="reviewer@example.com"):
    from types import SimpleNamespace
    return SimpleNamespace(state=SimpleNamespace(user_email=user_email))


@pytest.fixture()
def route(st, monkeypatch):
    import core
    monkeypatch.setattr(core, "store", st)
    monkeypatch.setattr(core, "fire_webhook", lambda *a, **k: None, raising=False)
    jobs = []
    monkeypatch.setattr(st, "enqueue_job",
                        lambda name, payload, **k: jobs.append((name, payload)), raising=False)
    from routes.hitl import hitl_update, HitlUpdate
    return hitl_update, HitlUpdate, jobs


def test_route_stores_evidence_values_and_enqueues_the_write(st, route):
    hitl_update, HitlUpdate, jobs = route
    row = _deferred_row(st)
    hitl_update(row["id"], HitlUpdate(status="approved", **viewed_fields(row["id"]),
                                      approved_values=["a nurse", "a bar chart", "the logo"]), _req())
    got = {e["locator"]: e.get("approved_value") for e in st.get_hitl_item(row["id"])["evidence"]}
    assert got["ppt/slides/slide1.xml#rId2"] == "a nurse"
    assert any(name == "apply_approved_values" for name, _ in jobs), "the write-back job never fired"


def test_route_does_not_enqueue_when_nothing_was_described(st, route):
    hitl_update, HitlUpdate, jobs = route
    row = _deferred_row(st)
    hitl_update(row["id"], HitlUpdate(status="approved", **viewed_fields(row["id"]), approved_values=[None, None, None]), _req())
    assert not any(name == "apply_approved_values" for name, _ in jobs)

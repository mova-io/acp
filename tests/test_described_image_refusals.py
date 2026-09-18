"""Two defects ADR 0055 shipped with (#1742), each reproduced before it was fixed.

Both were found by an adversarial review of the merged change, and both are the SAME class of
failure the feature was built to prevent — arriving through doors the implementation did not
check. Neither was caught by the round-trip fixture, because that fixture describes every image
on a single-proposal row, which is exactly the shape that dodges both.

  1. A described decision on a row with NO proposals left the finding stamped approved-and-
     resolved with no 1.1.1 obligation recorded, so the file certified 100/100 with the images
     untouched and undescribed — and the 500 fired before log_decision, so nothing recorded who
     resolved it or why. Reachable: store.queue_hitl_items and handlers.queue_hitl_review_for_file
     both mint proposal-less 1.4.5 rows, and propose_images_of_text returns [] whenever OCR is
     unavailable or times out.

  2. The "no proposed_value fallback" guarantee did not exist. store.queue_described_image_alt
     refuses to fall back to a draft and explains at length why — the draft on a 1.4.5 card is the
     OCR TRANSCRIPT, and a transcript is not a description. But approve_proposal_values runs FIRST
     in the same request and had already substituted that transcript for every blank the reviewer
     left, so the guard was inspecting a value that was already wrong. A reviewer describing one
     image of two filed the second picture's own text as its description, recorded as
     `"source": "reviewer"`.

The guard being in the wrong function is the lesson worth keeping: it read correctly, it was
tested, and the thing it guarded had already happened one call earlier.
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import pytest
from hitl_viewed import viewed_fields

ACP = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ACP / "api"))

SID, FILE = "s-adr55-fix", "deck.pptx"
TRANSCRIPT_1 = "Benefits at a glance. Medical dental and vision cover."
TRANSCRIPT_2 = "Payroll calendar notice. Salaries are paid monthly."
DESCRIPTION_1 = "A benefits summary card in the brand's display face, listing the cover offered."


@pytest.fixture()
def st(monkeypatch):
    import store as store_mod
    monkeypatch.setattr(store_mod, "_SQLITE_PATH", Path(tempfile.mkdtemp()) / "adr55fix.db")
    return store_mod.Store()


def _scanned(st):
    st.init_scan_run(SID, "drive", 1, "2026-09-07T00:00:00Z", "rubric", "hash")
    st.save_file_result(SID, {
        "file": FILE, "engine": "office", "status": "pass", "score": 60, "compliant": 0,
        "skipped_rules": 0, "drive_file_id": "d1",
        "issues": [{"ruleId": "OCR_IMAGE_OF_TEXT", "wcag": "1.4.5 Images of Text",
                    "severity": "SERIOUS", "detail": "embedded image 1 contains readable text"}],
    }, "2026-09-07T00:00:00Z")
    st.record_remediation(SID, FILE, drive_write_url="http://d/1", blob_url="http://b/1")


def _decide(st, monkeypatch, item_id, *, values):
    import core
    from routes.hitl import HitlUpdate, hitl_update
    monkeypatch.setattr(core, "store", st)
    return hitl_update(item_id, HitlUpdate(status="approved", **viewed_fields(item_id),
                                           resolution=st.DESCRIBED_RESOLUTION,
                                           approved_values=values), None)


# ─────────────────────────────────────────────────────── defect 1: nowhere to put a description

def test_a_described_decision_is_refused_on_a_row_with_no_per_image_cards(st, monkeypatch):
    """The row shape two production writers actually mint, and the state it used to leave.

    Asserted on the ROW rather than only on the status code, because the status code was never the
    problem: the old behaviour also returned an error. What made it dangerous is what it left
    behind — approved, resolved, owing nothing, certifiable.
    """
    from fastapi import HTTPException
    _scanned(st)
    item_id = st.queue_hitl_deferral(SID, FILE, "images of text", 1, rule_id="1.4.5") \
        or next(r["id"] for r in st.list_hitl_queue(scan_id=SID) if r["rule_id"] == "1.4.5")
    assert not (st.get_hitl_item(item_id).get("proposals") or [])   # the shape under test

    with pytest.raises(HTTPException) as e:
        _decide(st, monkeypatch, item_id, values=[DESCRIPTION_1])
    assert e.value.status_code == 422
    assert "nowhere to attach" in str(e.value.detail)

    row = st.get_hitl_item(item_id)
    assert row["status"] == "pending", "the finding must stay unresolved"
    assert not (row.get("resolution") or "").strip()
    # And the file must NOT be certifiable off a decision that was refused.
    assert st.mark_file_compliant_if_reviewed(SID, FILE) is False


def test_no_described_row_is_created_when_the_decision_is_refused(st, monkeypatch):
    """The other half: a refused decision leaves no half-built 1.1.1 obligation either."""
    from fastapi import HTTPException
    _scanned(st)
    item_id = st.queue_hitl_deferral(SID, FILE, "images of text", 1, rule_id="1.4.5") \
        or next(r["id"] for r in st.list_hitl_queue(scan_id=SID) if r["rule_id"] == "1.4.5")
    with pytest.raises(HTTPException):
        _decide(st, monkeypatch, item_id, values=[DESCRIPTION_1])
    assert [r["rule_id"] for r in st.list_hitl_queue(scan_id=SID)] == ["1.4.5"]


# ─────────────────────────────────────────────── defect 2: the transcript filed as a description

def _two_image_row(st) -> str:
    """A 1.4.5 card with two drafted images — the shape the round-trip fixture never had."""
    _scanned(st)
    return st.enqueue_proposals(SID, FILE, "1.4.5", [
        {"locator": "image 1", "before": "text baked into an image",
         "proposed_value": TRANSCRIPT_1, "rationale": "r", "source": "OCR"},
        {"locator": "image 2", "before": "text baked into an image",
         "proposed_value": TRANSCRIPT_2, "rationale": "r", "source": "OCR"},
    ], rule_name="Images of Text")


def test_an_undescribed_image_contributes_nothing_rather_than_its_own_transcript(st, monkeypatch):
    """THE DEFECT: describe one image, leave the other — the other used to get its own OCR text.

    A transcript is the words INSIDE the picture; a description is prose ABOUT it. Writing the
    first as alt text tells a screen-reader user what the picture says without telling them it is
    a picture, and it does so under a row that records the reviewer as its author.
    """
    item_id = _two_image_row(st)
    _decide(st, monkeypatch, item_id, values=[DESCRIPTION_1, ""])   # image 2 left alone

    owed = st.approved_alt_values(SID, FILE)
    assert owed == {"image 1": DESCRIPTION_1}, (
        "an image the reviewer did not describe must owe the document nothing — if image 2 is "
        "here carrying its transcript, the draft fallback is back")
    assert TRANSCRIPT_2 not in owed.values()

    # And nothing on the source row claims the reviewer authored it.
    src = st.get_hitl_item(item_id)
    assert not (src["proposals"][1].get("approved_value") or "").strip()


def test_the_described_row_records_only_what_the_reviewer_wrote(st, monkeypatch):
    """The audit trail, which is the half that outlives the document.

    Each proposal on a described row carries source 'reviewer' and a rationale naming the
    criterion it resolved. That is a claim about authorship, so only text a human actually typed
    may appear under it.
    """
    item_id = _two_image_row(st)
    _decide(st, monkeypatch, item_id, values=[DESCRIPTION_1, "   "])   # whitespace is not a value

    described = next(r for r in st.list_hitl_queue(scan_id=SID)
                     if r["rule_id"] == f"1.1.1{st.DESCRIBED_RULE_SUFFIX}")
    authored = {p["locator"]: p["approved_value"] for p in described["proposals"]}
    assert authored == {"image 1": DESCRIPTION_1}
    assert all(p.get("source") == "reviewer" for p in described["proposals"])


def test_an_ordinary_approval_still_accepts_the_draft(st, monkeypatch):
    """The control, and the reason this is a flag rather than a change of behaviour.

    Everywhere else "I edited nothing" means "the drafts I was shown are correct", and the 1.4.5
    REPLACEMENT lane depends on exactly that: the transcript IS the text that should replace the
    picture. Only the described decision opts out.
    """
    import core
    from routes.hitl import HitlUpdate, hitl_update
    item_id = _two_image_row(st)
    monkeypatch.setattr(core, "store", st)
    hitl_update(item_id, HitlUpdate(status="approved", **viewed_fields(item_id), approved_values=[DESCRIPTION_1, ""]), None)

    owed = st.approved_images_of_text_values(SID, FILE, ("1.4.5",))
    assert owed == {"image 1": DESCRIPTION_1, "image 2": TRANSCRIPT_2}, (
        "the ordinary approve path must still accept an unedited draft — turning the fallback off "
        "for everyone would silently drop every image a reviewer agreed to as drafted")

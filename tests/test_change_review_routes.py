"""Versioned reviewer decisions on saved changes — api/routes/change_review.py.

Pins: owner isolation, file-must-belong-to-scan, the auth gate, validation, and the four
hardening rules the independent review asked for —

  1. `expected_sha256` AND `change_digest` are MANDATORY on every mutation (422 without either,
     409 on either mismatch). Without them a client that has been showing a stale copy has the
     server bind its verdict to whatever bytes exist at save time.
  2. A decision binds to the SAVED COPY's digest and never to the source checksum. Where the
     corrected copy's identity was never recorded there is nothing to bind to: 409.
  3. The write is a compare-and-set in one transaction, and the response RE-READS staleness
     rather than answering a constant `false`.
  4. `edited` is a CORRECTION REQUESTED — recorded, not applied, never counted as confirmed.

And that a review never alters remediation identity or the generic decision map.
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from urllib.parse import quote

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "api"))

OWNER = "jeremyyu.movate@gmail.com"
OTHER = "devamovate@gmail.com"
SID = "s-cr1"
FILE = "handbook.docx"
NESTED = "Policies/2026/guide.pdf"
SHA_A = "a" * 64
SHA_B = "b" * 64


def _seed(store, sid=SID, owner=OWNER, checksum="md5-source-1", corrected=SHA_A):
    store.save_scan({
        "_scan_id": sid,
        "started_at": "2026-09-01T05:00:00+00:00",
        "completed_at": "2026-09-01T05:05:00+00:00",
        "source": "drive", "owner": owner,
        "rubric": {"name": "wcag-aa", "hash": "h"},
        "summary": {"files": 2, "certifiable": 1, "uncertain": 0, "error": 0, "avg_score": 90},
        "files": [
            {"file": FILE, "engine": ".net/office", "status": "certifiable", "score": 90,
             "compliant": 1, "skipped_rules": 0, "issues": [], "checksum": checksum},
            {"file": NESTED, "engine": "pdf", "status": "certifiable", "score": 90,
             "compliant": 1, "skipped_rules": 0, "issues": [], "checksum": None},
        ],
    })
    if corrected:
        store.record_remediation(sid, FILE, corrected_sha256=corrected)
    store.record_remediation_diffs(sid, FILE, [
        {"rule_id": "1.1.1", "before": "(no alt)", "after": "A red barn", "note": "vision"},
        {"rule_id": "2.4.2", "before": "", "after": "Staff handbook"},
    ])


def _digest(rule, before, after):
    return hashlib.sha256(f"{rule}\n{before}\n{after}".encode()).hexdigest()


def _url(file=FILE, change=None, sid=SID):
    base = f"/scans/{sid}/files/{quote(file, safe='')}/change-reviews"
    return base if change is None else f"{base}/{quote(change, safe='')}"


@pytest.fixture()
def client(monkeypatch, isolated_store):
    import core
    from fastapi.testclient import TestClient
    from app import app

    monkeypatch.setattr(core, "store", isolated_store)
    monkeypatch.setattr(core, "ACCESS_CODE", "", raising=False)
    monkeypatch.setattr(core, "GOOGLE_CLIENT_ID", "test-client-id", raising=False)
    monkeypatch.setattr(core, "E2E_KEY", None, raising=False)
    monkeypatch.setattr(core, "OWNER_EMAIL", OWNER, raising=False)
    monkeypatch.setattr(core, "verify_gis_token", lambda tok: tok or None)
    monkeypatch.setattr(core, "email_allowed", lambda e: e in (OWNER, OTHER))
    c = TestClient(app)

    def as_user(email):
        c.headers.clear()
        if email:
            c.headers.update({"Authorization": f"Bearer {email}"})
        return c
    return as_user


CID = f"{FILE}::1.1.1::0"
CDIGEST = _digest("1.1.1", "(no alt)", "A red barn")
BODY = {"verdict": "accepted", "note": "Looks right",
        "change_digest": CDIGEST, "expected_sha256": SHA_A}


def test_routes_are_behind_the_auth_gate(client, isolated_store):
    import core
    _seed(isolated_store)
    assert core.is_public(_url()) is False
    assert core.is_public(_url(change=CID)) is False
    anon = client(None)
    assert anon.get(_url()).status_code == 401
    assert anon.put(_url(change=CID), json=BODY).status_code == 401


def test_save_binds_to_the_saved_copy_and_reads_back_fresh(client, isolated_store):
    _seed(isolated_store)
    c = client(OWNER)
    r = c.put(_url(change=CID), json=BODY)
    assert r.status_code == 200, r.text
    review = r.json()["review"]
    assert review["artifact_sha256"] == SHA_A
    assert review["reviewer"] == OWNER and review["verdict"] == "accepted" and review["at"]
    assert review["stale"] is False
    got = c.get(_url()).json()
    assert got["artifact"]["currentSha256"] == SHA_A
    assert got["artifact"]["identityKind"] == "corrected_sha256"
    assert got["artifact"]["sourceSha256"] is None   # an md5 is not reported as a sha256
    assert got["artifact"]["sourceChecksumKind"] == "other"
    assert got["reviews"][CID]["stale"] is False
    assert got["reviews"][CID]["note"] == "Looks right"


def test_the_decision_log_records_verdict_actor_time_and_the_bound_ids(client, isolated_store):
    _seed(isolated_store)
    assert client(OWNER).put(_url(change=CID), json=BODY).status_code == 200
    with isolated_store._db.cursor() as cur:
        isolated_store._db.execute(
            cur, "SELECT action,actor,file,rule_id,detail,ts FROM decision_log WHERE scan_id=%s",
            (SID,))
        rows = [dict(r) for r in isolated_store._db.fetchall(cur)]
    entry = next(r for r in rows if r["action"] == "change_review.accepted")
    assert entry["actor"] == OWNER and entry["file"] == FILE and entry["rule_id"] == "1.1.1"
    assert entry["ts"]
    detail = json.loads(entry["detail"])
    assert detail["verdict"] == "accepted"
    assert detail["change_id"] == CID
    assert detail["artifact_sha256"] == SHA_A
    assert detail["change_digest"] == CDIGEST
    assert detail["reviewer"] == OWNER and detail["at"]


def test_other_owner_cannot_read_or_write(client, isolated_store):
    _seed(isolated_store)
    assert client(OWNER).put(_url(change=CID), json=BODY).status_code == 200
    other = client(OTHER)
    assert other.get(_url()).status_code == 404
    assert other.put(_url(change=CID), json=BODY).status_code == 404


def test_file_not_in_scan_is_404(client, isolated_store):
    _seed(isolated_store)
    c = client(OWNER)
    assert c.get(_url(file="elsewhere.docx")).status_code == 404
    assert c.put(_url(file="elsewhere.docx", change="elsewhere.docx::1.1.1::0"),
                 json=BODY).status_code == 404
    assert c.get(_url(sid="nope")).status_code == 404


# ── the mandatory binding tokens ──────────────────────────────────────────────

def test_a_missing_expected_sha256_is_refused(client, isolated_store):
    """Without it the server would bind acceptance to bytes the client may never have shown."""
    _seed(isolated_store)
    body = {k: v for k, v in BODY.items() if k != "expected_sha256"}
    r = client(OWNER).put(_url(change=CID), json=body)
    assert r.status_code == 422
    assert "expected_sha256 is required" in r.text


def test_a_missing_change_digest_is_refused(client, isolated_store):
    _seed(isolated_store)
    body = {k: v for k, v in BODY.items() if k != "change_digest"}
    r = client(OWNER).put(_url(change=CID), json=body)
    assert r.status_code == 422
    assert "change_digest is required" in r.text


def test_neither_token_is_recorded_when_the_request_is_refused(client, isolated_store):
    _seed(isolated_store)
    c = client(OWNER)
    c.put(_url(change=CID), json={"verdict": "accepted"})
    assert c.get(_url()).json()["reviews"] == {}


@pytest.mark.parametrize("body,change,status", [
    ({"verdict": "approved", **{k: BODY[k] for k in ("change_digest", "expected_sha256")}},
     CID, 422),
    ({"verdict": "edited", "change_digest": CDIGEST, "expected_sha256": SHA_A}, CID, 422),
    ({"verdict": "edited", "edited_value": "   ", "change_digest": CDIGEST,
      "expected_sha256": SHA_A}, CID, 422),
    ({**BODY, "note": "x" * 2001}, CID, 413),
    ({**BODY, "note": 5}, CID, 422),
    ({**BODY, "change_digest": "zz"}, CID, 422),
    ({**BODY, "expected_sha256": 7}, CID, 422),
    (BODY, "other.docx::1.1.1::0", 422),
    (BODY, f"{FILE}::1.1.1::x", 422),
    (BODY, f"{FILE}::1.1.1::" + "9" * 600, 422),
    (BODY, f"{FILE}::9.9.9::0", 404),
])
def test_validation(client, isolated_store, body, change, status):
    _seed(isolated_store)
    assert client(OWNER).put(_url(change=change), json=body).status_code == status


def test_changed_artifact_and_changed_change_are_both_409(client, isolated_store):
    _seed(isolated_store)
    c = client(OWNER)
    assert c.put(_url(change=CID), json={**BODY, "change_digest": "c" * 64}).status_code == 409
    assert c.put(_url(change=CID), json={**BODY, "expected_sha256": SHA_B}).status_code == 409
    assert c.put(_url(change=CID), json=BODY).status_code == 200


def test_an_unrecorded_saved_copy_identity_refuses_to_bind(client, isolated_store):
    """The review's finding. A verdict on an AI-written change is a verdict on the WRITTEN bytes;
    the source checksum does not identify them, so there is nothing to bind to."""
    _seed(isolated_store, corrected=None)
    c = client(OWNER)
    r = c.put(_url(change=CID), json={**BODY, "expected_sha256": "md5-source-1"})
    assert r.status_code == 409
    assert "saved copy's identity is not recorded" in r.text
    # ... and nothing was written
    assert c.get(_url()).json()["reviews"] == {}


def test_a_legacy_unbindable_decision_reads_back_as_freshness_unknown(client, isolated_store):
    _seed(isolated_store, corrected=None)
    isolated_store.save_decision(
        SID, FILE, f"change_review:{CID}",
        json.dumps({"verdict": "accepted", "artifact_sha256": None}),
        OWNER, "2026-09-01T00:00:00+00:00")
    review = client(OWNER).get(_url()).json()["reviews"][CID]
    assert review["stale"] is None            # never False, never "accepted"
    assert "cannot be established" in review["staleReason"]


# ── staleness, re-evaluated rather than asserted ──────────────────────────────

def test_stale_after_the_saved_copy_changes(client, isolated_store):
    _seed(isolated_store)
    c = client(OWNER)
    assert c.put(_url(change=CID), json=BODY).json()["review"]["artifact_sha256"] == SHA_A
    assert c.get(_url()).json()["reviews"][CID]["stale"] is False
    with isolated_store._db.cursor() as cur:
        isolated_store._db.execute(
            cur, "UPDATE file_records SET corrected_sha256=%s WHERE scan_id=%s AND file=%s",
            (SHA_B, SID, FILE))
    got = c.get(_url()).json()
    assert got["artifact"]["currentSha256"] == SHA_B
    assert got["reviews"][CID]["stale"] is True


def test_stale_after_change_content_changes(client, isolated_store):
    _seed(isolated_store)
    c = client(OWNER)
    c.put(_url(change=CID), json=BODY)
    isolated_store.record_remediation_diffs(SID, FILE, [
        {"rule_id": "1.1.1", "before": "(no alt)", "after": "A blue barn"}])
    assert c.get(_url()).json()["reviews"][CID]["stale"] is True


def test_stale_when_the_change_no_longer_exists(client, isolated_store):
    _seed(isolated_store)
    c = client(OWNER)
    c.put(_url(change=CID), json=BODY)
    isolated_store.record_remediation_diffs(SID, FILE, [])
    assert c.get(_url()).json()["reviews"][CID]["stale"] is True


def test_a_concurrent_artifact_change_never_answers_stale_false(client, isolated_store,
                                                                monkeypatch):
    """The compare-and-set. The artifact is rewritten INSIDE the save, between the route's check
    and the store's write — which is the window the review named. The decision must be refused,
    not recorded against bytes nobody reviewed and reported fresh."""
    _seed(isolated_store)
    c = client(OWNER)
    original = type(isolated_store).save_change_review

    def racing(self, *args, **kwargs):
        with self._db.cursor() as cur:
            self._db.execute(
                cur, "UPDATE file_records SET corrected_sha256=%s WHERE scan_id=%s AND file=%s",
                (SHA_B, SID, FILE))
        return original(self, *args, **kwargs)

    monkeypatch.setattr(type(isolated_store), "save_change_review", racing)
    r = c.put(_url(change=CID), json=BODY)
    assert r.status_code == 409
    assert "changed while the decision was being recorded" in r.text
    monkeypatch.setattr(type(isolated_store), "save_change_review", original)
    # nothing was recorded, so nothing can read back as a fresh confirmation
    assert c.get(_url()).json()["reviews"] == {}


def test_the_response_reevaluates_staleness_rather_than_asserting_it(client, isolated_store,
                                                                    monkeypatch):
    """A decision that commits against an artifact which then moves reports stale, immediately.

    The store's compare-and-set is what refuses the racing case above; here the move happens
    after the transaction, so the write is legitimate and the RESPONSE is the thing under test.
    """
    _seed(isolated_store)
    c = client(OWNER)
    original = type(isolated_store).save_change_review

    def then_move(self, *args, **kwargs):
        ok = original(self, *args, **kwargs)
        with self._db.cursor() as cur:
            self._db.execute(
                cur, "UPDATE file_records SET corrected_sha256=%s WHERE scan_id=%s AND file=%s",
                (SHA_B, SID, FILE))
        return ok

    monkeypatch.setattr(type(isolated_store), "save_change_review", then_move)
    review = c.put(_url(change=CID), json=BODY).json()["review"]
    assert review["stale"] is True, "the response must re-read, not answer a constant"


# ── edited is a correction REQUEST ────────────────────────────────────────────

def test_edited_is_recorded_as_a_correction_request_and_never_as_confirmed(client,
                                                                          isolated_store):
    _seed(isolated_store)
    c = client(OWNER)
    r = c.put(_url(change=CID), json={"verdict": "edited", "edited_value": "A red barn at dusk",
                                      "change_digest": CDIGEST, "expected_sha256": SHA_A})
    assert r.status_code == 200
    assert r.json()["review"]["verdictLabel"] == "correction requested"
    got = c.get(_url()).json()["reviews"][CID]
    assert got["edited_value"] == "A red barn at dusk"
    assert got["verdict"] == "edited" and got["verdictLabel"] == "correction requested"

    import report_facts
    facts = report_facts.build_file_facts(isolated_store, SID, FILE, owner=OWNER)
    counts = facts["accounting"]["humanReviews"]
    assert counts["correctionRequested"] == 1 and counts["accepted"] == 0

    # ... and the document itself was not touched: a correction request writes no bytes
    assert isolated_store.get_remediation_diffs(SID, FILE)[0]["after"] == "A red barn"

    # a non-edit verdict never carries a stale edited value
    c.put(_url(change=CID), json={**BODY, "verdict": "rejected", "edited_value": "ignored"})
    assert c.get(_url()).json()["reviews"][CID]["edited_value"] is None


# ── unverified saved changes are reviewable, and are the point ────────────────

def test_an_applied_but_unverified_change_is_reviewable_by_its_own_id(client, isolated_store):
    _seed(isolated_store)
    isolated_store.log_decision(
        "system", "apply.saved_unverified", scan_id=SID, file=FILE, rule_id="1.3.1",
        detail=json.dumps({"artifact_sha256": SHA_A, "rule_id": "1.3.1",
                           "changes": [{"locator": "docx:table:1", "before": "",
                                        "after": "Quarterly results"}]}))
    c = client(OWNER)
    listed = c.get(_url()).json()["savedChanges"]
    unverified = next(x for x in listed if x["verification"] == "not_verified")
    assert unverified["id"].split("::")[-1].startswith("u")
    r = c.put(_url(change=unverified["id"]),
              json={"verdict": "accepted", "change_digest": unverified["changeDigest"],
                    "expected_sha256": SHA_A})
    assert r.status_code == 200, r.text
    assert r.json()["review"]["verification"] == "not_verified"
    assert c.get(_url()).json()["reviews"][unverified["id"]]["stale"] is False


def test_nested_filename_round_trip(client, isolated_store):
    _seed(isolated_store)
    isolated_store.record_remediation(SID, NESTED, corrected_sha256=SHA_A)
    isolated_store.record_remediation_diffs(SID, NESTED, [
        {"rule_id": "1.3.1", "before": "a", "after": "b"}])
    c = client(OWNER)
    cid = f"{NESTED}::1.3.1::0"
    r = c.put(_url(file=NESTED, change=cid),
              json={"verdict": "unable", "change_digest": _digest("1.3.1", "a", "b"),
                    "expected_sha256": SHA_A})
    assert r.status_code == 200, r.text
    assert c.get(_url(file=NESTED)).json()["reviews"][cid]["verdict"] == "unable"


def test_review_does_not_change_remediation_identity_or_decision_map(client, isolated_store):
    _seed(isolated_store)
    isolated_store.save_decision(SID, FILE, "triage", "inscope", OWNER,
                                 "2026-09-01T00:00:00+00:00")
    before_digest = isolated_store.remediation_decision_digest(SID, [FILE], owner=OWNER)
    before_map = isolated_store.get_decisions(SID, owner=OWNER)
    c = client(OWNER)
    assert c.put(_url(change=CID), json=BODY).status_code == 200
    assert isolated_store.remediation_decision_digest(SID, [FILE], owner=OWNER) == before_digest
    assert isolated_store.get_decisions(SID, owner=OWNER) == before_map == {FILE: {"triage": "inscope"}}
    # and the scan-decisions GET route consumers see only the fixed kinds
    assert c.get(f"/scans/{SID}/decisions").json() == {FILE: {"triage": "inscope"}}

"""Versioned reviewer decisions on saved changes — api/routes/change_review.py.

Pins: owner isolation, file-must-belong-to-scan, the auth gate, validation, binding to the
current artifact identity (and refusing to bind to nothing), staleness when the artifact or the
change moves on, and that a review never alters remediation identity or the generic decision map.
"""
from __future__ import annotations

import hashlib
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


def _seed(store, sid=SID, owner=OWNER, checksum="md5-source-1"):
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
BODY = {"verdict": "accepted", "note": "Looks right", "change_digest": _digest("1.1.1", "(no alt)", "A red barn")}


def test_routes_are_behind_the_auth_gate(client, isolated_store):
    import core
    _seed(isolated_store)
    assert core.is_public(_url()) is False
    assert core.is_public(_url(change=CID)) is False
    anon = client(None)
    assert anon.get(_url()).status_code == 401
    assert anon.put(_url(change=CID), json=BODY).status_code == 401


def test_save_binds_to_current_artifact_and_reads_back_fresh(client, isolated_store):
    _seed(isolated_store)
    c = client(OWNER)
    r = c.put(_url(change=CID), json=BODY)
    assert r.status_code == 200, r.text
    review = r.json()["review"]
    assert review["artifact_sha256"] == "md5-source-1"
    assert review["reviewer"] == OWNER and review["verdict"] == "accepted" and review["at"]
    got = c.get(_url()).json()
    assert got["artifact"]["currentSha256"] == "md5-source-1"
    assert got["artifact"]["identityKind"] == "source_checksum"
    assert got["artifact"]["sourceSha256"] is None   # an md5 is not reported as a sha256
    assert got["reviews"][CID]["stale"] is False
    assert got["reviews"][CID]["note"] == "Looks right"
    # the audit trail got the decision too
    with isolated_store._db.cursor() as cur:
        isolated_store._db.execute(cur, "SELECT action,actor,file FROM decision_log WHERE scan_id=%s", (SID,))
        rows = isolated_store._db.fetchall(cur)
    assert {"action": "change_review.accepted", "actor": OWNER, "file": FILE} in [dict(x) for x in rows]


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
    assert c.put(_url(file="elsewhere.docx", change="elsewhere.docx::1.1.1::0"), json=BODY).status_code == 404
    assert c.get(_url(sid="nope")).status_code == 404


@pytest.mark.parametrize("body,change,status", [
    ({"verdict": "approved"}, CID, 422),
    ({"verdict": "edited", "change_digest": BODY["change_digest"]}, CID, 422),
    ({"verdict": "edited", "edited_value": "   "}, CID, 422),
    ({"verdict": "accepted", "note": "x" * 2001}, CID, 413),
    ({"verdict": "accepted", "note": 5}, CID, 422),
    ({"verdict": "accepted", "change_digest": "zz"}, CID, 422),
    ({"verdict": "accepted"}, "other.docx::1.1.1::0", 422),
    ({"verdict": "accepted"}, f"{FILE}::1.1.1::x", 422),
    ({"verdict": "accepted"}, f"{FILE}::1.1.1::" + "9" * 600, 422),
    ({"verdict": "accepted"}, f"{FILE}::9.9.9::0", 404),
])
def test_validation(client, isolated_store, body, change, status):
    _seed(isolated_store)
    assert client(OWNER).put(_url(change=change), json=body).status_code == status


def test_edited_requires_and_stores_value(client, isolated_store):
    _seed(isolated_store)
    c = client(OWNER)
    r = c.put(_url(change=CID), json={"verdict": "edited", "edited_value": "A red barn at dusk"})
    assert r.status_code == 200
    assert c.get(_url()).json()["reviews"][CID]["edited_value"] == "A red barn at dusk"
    # a non-edit verdict never carries a stale edited value
    c.put(_url(change=CID), json={"verdict": "rejected", "edited_value": "ignored"})
    assert c.get(_url()).json()["reviews"][CID]["edited_value"] is None


def test_stale_after_corrected_artifact_changes(client, isolated_store):
    _seed(isolated_store)
    c = client(OWNER)
    isolated_store.record_remediation(SID, FILE, corrected_sha256=SHA_A)
    assert c.put(_url(change=CID), json=BODY).json()["review"]["artifact_sha256"] == SHA_A
    assert c.get(_url()).json()["reviews"][CID]["stale"] is False
    with isolated_store._db.cursor() as cur:
        isolated_store._db.execute(cur, "UPDATE file_records SET corrected_sha256=%s WHERE scan_id=%s AND file=%s",
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


def test_client_digest_mismatch_and_expected_sha_mismatch_are_409(client, isolated_store):
    _seed(isolated_store)
    c = client(OWNER)
    r = c.put(_url(change=CID), json={**BODY, "change_digest": "c" * 64})
    assert r.status_code == 409
    r = c.put(_url(change=CID), json={**BODY, "expected_sha256": "not-the-current-one"})
    assert r.status_code == 409
    r = c.put(_url(change=CID), json={**BODY, "expected_sha256": "md5-source-1"})
    assert r.status_code == 200


def test_no_artifact_identity_refuses_to_bind(client, isolated_store):
    _seed(isolated_store)
    isolated_store.record_remediation_diffs(SID, NESTED, [{"rule_id": "1.3.1", "before": "a", "after": "b"}])
    c = client(OWNER)
    got = c.get(_url(file=NESTED))
    assert got.status_code == 200
    assert got.json()["artifact"]["currentSha256"] is None
    r = c.put(_url(file=NESTED, change=f"{NESTED}::1.3.1::0"), json={"verdict": "accepted"})
    assert r.status_code == 409
    assert "artifact identity not recorded" in r.text
    # a legacy/unbindable record reads back with stale unknown, never fresh
    import json as _json
    isolated_store.save_decision(SID, NESTED, f"change_review:{NESTED}::1.3.1::0",
                                 _json.dumps({"verdict": "accepted", "artifact_sha256": None}),
                                 OWNER, "2026-09-01T00:00:00+00:00")
    assert c.get(_url(file=NESTED)).json()["reviews"][f"{NESTED}::1.3.1::0"]["stale"] is None


def test_nested_filename_round_trip(client, isolated_store):
    _seed(isolated_store)
    isolated_store.record_remediation(SID, NESTED, corrected_sha256=SHA_A)
    isolated_store.record_remediation_diffs(SID, NESTED, [{"rule_id": "1.3.1", "before": "a", "after": "b"}])
    c = client(OWNER)
    cid = f"{NESTED}::1.3.1::0"
    assert c.put(_url(file=NESTED, change=cid), json={"verdict": "unable"}).status_code == 200
    assert c.get(_url(file=NESTED)).json()["reviews"][cid]["verdict"] == "unable"


def test_review_does_not_change_remediation_identity_or_decision_map(client, isolated_store):
    _seed(isolated_store)
    isolated_store.save_decision(SID, FILE, "triage", "inscope", OWNER, "2026-09-01T00:00:00+00:00")
    before_digest = isolated_store.remediation_decision_digest(SID, [FILE], owner=OWNER)
    before_map = isolated_store.get_decisions(SID, owner=OWNER)
    c = client(OWNER)
    assert c.put(_url(change=CID), json=BODY).status_code == 200
    assert isolated_store.remediation_decision_digest(SID, [FILE], owner=OWNER) == before_digest
    assert isolated_store.get_decisions(SID, owner=OWNER) == before_map == {FILE: {"triage": "inscope"}}
    # and the scan-decisions GET route consumers see only the fixed kinds
    assert c.get(f"/scans/{SID}/decisions").json() == {FILE: {"triage": "inscope"}}

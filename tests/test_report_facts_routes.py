"""The report-facts routes, through the real app and its middleware.

Three claims live here rather than in test_report_facts.py, because none of them is decidable
from the builder alone: that the routes are behind the auth gate, that a foreign owner cannot
tell a scan exists, and that the exact-bytes preview renders ONLY bytes whose sha256 matches the
one in the path.

That last one is the fix for a specific false statement. The existing preview route,
`/scans/{id}/files/{file}/page/{n}`, prefers the original but falls back to the remediated blob,
so the same URL can answer with either and nothing says which — and a report that captioned it
"after the edit" was asserting provenance it did not have. Here the digest is the REQUEST, the
bytes are hashed before anything is rasterised, and a digest ACP does not hold is a 404 rather
than a different page.
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from urllib.parse import quote

import pytest

ACP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ACP / "api"))

OWNER = "owner@hosp.org"
OTHER = "other@hosp.org"
SID = "s-rf"
FILE = "handbook.pdf"

_PDF = (b"%PDF-1.4\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
        b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
        b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 200 200]>>endobj\n"
        b"trailer<</Root 1 0 R>>\n%%EOF\n")
_PDF_SHA = hashlib.sha256(_PDF).hexdigest()
_OTHER_SHA = hashlib.sha256(b"not these bytes").hexdigest()


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


def _seed(store, owner=OWNER, files=None):
    store.save_scan({
        "_scan_id": SID, "started_at": "2026-09-01T05:00:00+00:00",
        "completed_at": "2026-09-01T05:05:00+00:00", "source": "local", "owner": owner,
        "rubric": {"name": "wcag-aa", "hash": "h"},
        "summary": {"files": 1, "certifiable": 0, "uncertain": 1, "error": 0, "avg_score": 70},
        "files": files or [{
            "file": FILE, "engine": "pdf", "status": "uncertain", "score": 70, "compliant": 0,
            "skipped_rules": 0, "checksum": "md5-source-1",
            "issues": [{"ruleId": "PDF-ALT-001", "wcag": "1.1.1 Non-text Content",
                        "severity": "SERIOUS", "detail": "Missing description A",
                        "page": 1, "location": "pdf:fig:1"},
                       {"ruleId": "PDF-ALT-001", "wcag": "1.1.1 Non-text Content",
                        "severity": "SERIOUS", "detail": "Missing description B",
                        "page": 2, "location": "pdf:fig:2"}]}],
    })
    store.record_remediation(SID, FILE, corrected_sha256="c" * 64)
    store.record_remediation_diffs(SID, FILE, [
        {"rule_id": "1.1.1", "before": "", "after": "Image A description", "note": "vision"}])


def _facts_url(file=FILE, sid=SID):
    return f"/scans/{sid}/files/{quote(file, safe='')}/report-facts"


def test_local_preview_cannot_escape_corpus(client, isolated_store, monkeypatch, tmp_path):
    import blob
    import scanner
    from routes.report_facts import _candidate_bytes
    _seed(isolated_store)
    root = tmp_path / "corpus"
    root.mkdir()
    outside = tmp_path / "outside.pdf"
    outside.write_bytes(_PDF)
    monkeypatch.setenv("ACP_LOCAL_CORPUS", str(root))
    monkeypatch.setattr(blob, "download_remediated", lambda *a: None)
    monkeypatch.setattr(scanner, "read_cached_source", lambda *a: None)
    assert list(_candidate_bytes(None, SID, "../outside.pdf", OWNER)) == []
    assert list(_candidate_bytes(None, SID, str(outside), OWNER)) == []
    (root / "inside.pdf").write_bytes(_PDF)
    assert list(_candidate_bytes(None, SID, "inside.pdf", OWNER)) == [_PDF]


# ── the gate and owner isolation ──────────────────────────────────────────────

def test_the_routes_are_behind_the_auth_gate(client, isolated_store):
    import core
    _seed(isolated_store)
    for path in (_facts_url(), f"/scans/{SID}/report-facts",
                 f"/scans/{SID}/files/{quote(FILE, safe='')}/artifact/{_PDF_SHA}/page/1"):
        assert core.is_public(path) is False
        assert client(None).get(path).status_code == 401


def test_a_foreign_owner_gets_404_everywhere(client, isolated_store):
    _seed(isolated_store)
    other = client(OTHER)
    assert other.get(_facts_url()).status_code == 404
    assert other.get(f"/scans/{SID}/report-facts").status_code == 404
    assert other.get(
        f"/scans/{SID}/files/{quote(FILE, safe='')}/artifact/{_PDF_SHA}/page/1"
    ).status_code == 404


def test_a_file_that_is_not_in_the_scan_is_404(client, isolated_store):
    _seed(isolated_store)
    assert client(OWNER).get(_facts_url("elsewhere.pdf")).status_code == 404


# ── the per-file facts, over HTTP ─────────────────────────────────────────────

def test_the_file_facts_are_the_shape_the_other_streams_code_against(client, isolated_store):
    _seed(isolated_store)
    body = client(OWNER).get(_facts_url()).json()
    assert body["factsVersion"] == 1
    assert set(body) >= {"factsVersion", "factsDigest", "generatedAt", "identity", "assessment",
                         "findings", "savedChanges", "savedChangesComplete", "savedChangesTotal",
                         "savedChangesLimit", "reviews", "accounting", "previous",
                         "previousReason", "limits"}
    assert set(body["identity"]) >= {"scanId", "file", "sourceChecksum", "sourceChecksumKind",
                                     "sourceSha256", "correctedSha256", "currentArtifact",
                                     "remediatedAt", "platformVersion", "targetLevel",
                                     "scopeDigest", "scanScope", "rubricHash"}
    assert set(body["accounting"]) >= {"findingsTotal", "findingsOpen", "findingsResolvedVerified",
                                       "resolutionLedger", "savedChangesVerified",
                                       "savedChangesUnverified", "humanReviews"}
    change = body["savedChanges"][0]
    assert set(change) >= {"id", "ruleId", "sc", "seq", "locator", "before", "after", "note",
                           "verification", "verificationDetail", "artifactSha256", "valueClipped",
                           "changeDigest", "findingIds", "source"}
    assert change["id"] == f"{FILE}::1.1.1::0"
    assert body["limits"]["valueMaxChars"] == 2000


def test_the_reproduced_defect_is_not_reproduced_over_http(client, isolated_store):
    """Two findings for 1.1.1, one verified change. Never "2 resolved", never "all resolved"."""
    _seed(isolated_store)
    body = client(OWNER).get(_facts_url()).json()
    assert body["assessment"]["state"] == "assessed"
    assert body["assessment"]["findingsTotal"] == 2
    assert body["accounting"]["findingsResolvedVerified"] is None
    assert body["accounting"]["resolutionLedger"] == "none"
    assert body["accounting"]["savedChangesVerified"] == 1
    assert body["accounting"]["humanReviews"]["pending"] == 1


def test_the_digest_over_http_matches_the_in_process_seam(client, isolated_store):
    import report_facts
    _seed(isolated_store)
    body = client(OWNER).get(_facts_url()).json()
    assert body["factsDigest"] == report_facts.current_digest(
        isolated_store, SID, FILE, owner=OWNER)
    assert report_facts.verify_digest(isolated_store, SID, FILE, body["factsDigest"],
                                      owner=OWNER) is True


# ── the scan-level index ──────────────────────────────────────────────────────

def test_the_scan_facts_are_paginable_and_say_so(client, isolated_store):
    files = [{"file": f"doc{n:02d}.pdf", "engine": "pdf", "status": "uncertain", "score": 70,
              "compliant": 0, "skipped_rules": 0, "issues": [], "checksum": f"sum-{n}"}
             for n in range(7)]
    _seed(isolated_store, files=files)
    c = client(OWNER)
    page = c.get(f"/scans/{SID}/report-facts?offset=2&limit=3").json()
    assert [f["file"] for f in page["files"]] == ["doc02.pdf", "doc03.pdf", "doc04.pdf"]
    assert page["filesTotal"] == 7 and page["complete"] is False
    whole = c.get(f"/scans/{SID}/report-facts?limit=500").json()
    assert whole["complete"] is True
    # ... and the digest does not depend on which page you asked for
    assert page["factsDigest"] == whole["factsDigest"]


def test_an_out_of_range_limit_is_refused_rather_than_silently_clamped(client, isolated_store):
    _seed(isolated_store)
    assert client(OWNER).get(f"/scans/{SID}/report-facts?limit=99999").status_code == 422
    assert client(OWNER).get(f"/scans/{SID}/report-facts?offset=-1").status_code == 422


# ── the exact-bytes preview ───────────────────────────────────────────────────

@pytest.fixture()
def held_bytes(monkeypatch):
    """ACP holds exactly one set of bytes for the document, and nothing else."""
    import blob as _blob
    import render as _render
    monkeypatch.setattr(_blob, "download_remediated", lambda *a, **k: _PDF, raising=False)
    monkeypatch.setattr(_blob, "download_render", lambda *a, **k: None, raising=False)
    monkeypatch.setattr(_blob, "upload_render", lambda *a, **k: None, raising=False)
    rendered = []
    monkeypatch.setattr(_render, "can_render", lambda ext: True, raising=False)
    monkeypatch.setattr(_render, "render_page_png",
                        lambda data, ext, page: rendered.append(data) or b"\x89PNG-fake",
                        raising=False)
    return rendered


def _artifact_url(sha, page=1, file=FILE):
    return f"/scans/{SID}/files/{quote(file, safe='')}/artifact/{sha}/page/{page}"


def test_the_exact_bytes_route_is_registered_before_the_greedy_page_route():
    """THE ROOT CAUSE TEST for the four below, and the reason it is separate from them.

    `routes/scans.py` registers `GET /scans/{scan_id}/files/{filename:path}/page/{page}` with a
    GREEDY path parameter, and Starlette matches routes in REGISTRATION ORDER, not by
    specificity. So `/scans/s/files/a.pdf/artifact/<sha>/page/1` binds
    filename="a.pdf/artifact/<sha>" and is answered by the ambiguous preview route — the one
    whose provenance nobody can state — including for a digest ACP does not hold, which then
    returns an IMAGE where this route must return 404.

    Measured: with the routers in the other order, a deliberately wrong digest answered 200 with
    a rendered page. `api/routes/__init__.py` must list `report_facts.router` before
    `scans.router`.
    """
    from app import app
    import routes.report_facts as rfr
    import routes.scans as scans

    order = [getattr(x, "original_router", None) for x in app.router.routes]
    assert rfr.router in order and scans.router in order
    assert order.index(rfr.router) < order.index(scans.router), (
        "report_facts.router must be included BEFORE scans.router in api/routes/__init__.py — "
        "scans.py's greedy {filename:path} page route otherwise swallows the exact-bytes "
        "preview and answers it with bytes of unknown provenance")


def test_the_matching_digest_renders_those_exact_bytes(client, isolated_store, held_bytes):
    _seed(isolated_store)
    r = client(OWNER).get(_artifact_url(_PDF_SHA))
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("image/png")
    assert r.headers["X-ACP-Artifact-Sha256"] == _PDF_SHA
    assert r.content == b"\x89PNG-fake"
    assert held_bytes == [_PDF], "it rasterised the bytes it hashed, and only those"


def test_a_digest_acp_does_not_hold_is_404_and_renders_nothing(client, isolated_store,
                                                               held_bytes):
    """The whole point: no fallback to other bytes. A wrong digest must not produce a picture."""
    _seed(isolated_store)
    r = client(OWNER).get(_artifact_url(_OTHER_SHA))
    assert r.status_code == 404
    assert "does not hold bytes with that digest" in r.text
    assert held_bytes == [], "nothing was rasterised for a digest ACP does not hold"


def test_a_malformed_digest_is_refused_before_anything_is_read(client, isolated_store,
                                                               held_bytes):
    _seed(isolated_store)
    assert client(OWNER).get(_artifact_url("not-a-digest")).status_code == 422
    assert client(OWNER).get(_artifact_url("a" * 63)).status_code == 422
    assert held_bytes == []


def test_the_cache_key_carries_the_digest_so_it_cannot_serve_other_bytes(client, isolated_store,
                                                                        monkeypatch):
    import blob as _blob
    import render as _render
    seen = {}
    monkeypatch.setattr(_blob, "download_remediated", lambda *a, **k: _PDF, raising=False)
    monkeypatch.setattr(_blob, "download_render", lambda *a, **k: None, raising=False)
    monkeypatch.setattr(_blob, "upload_render",
                        lambda owner, sid, key, data: seen.setdefault("key", key), raising=False)
    monkeypatch.setattr(_render, "can_render", lambda ext: True, raising=False)
    monkeypatch.setattr(_render, "render_page_png", lambda d, e, p: b"png", raising=False)
    _seed(isolated_store)
    client(OWNER).get(_artifact_url(_PDF_SHA, page=3))
    assert _PDF_SHA in seen["key"] and "#p3" in seen["key"]


def test_a_file_outside_the_scan_cannot_be_previewed(client, isolated_store, held_bytes):
    _seed(isolated_store)
    assert client(OWNER).get(
        _artifact_url(_PDF_SHA, file="elsewhere.pdf")).status_code == 404


def test_the_preview_never_fetches_from_a_remote_provider(client, isolated_store, monkeypatch):
    """A preview must not be able to cause an outbound request — the review's last line."""
    import core
    def explode(*a, **k):
        raise AssertionError("the exact-bytes preview reached for a remote provider")
    monkeypatch.setattr(core, "drive_service", explode, raising=False)
    import blob as _blob
    import render as _render
    monkeypatch.setattr(_blob, "download_remediated", lambda *a, **k: None, raising=False)
    monkeypatch.setattr(_blob, "download_render", lambda *a, **k: None, raising=False)
    monkeypatch.setattr(_render, "can_render", lambda ext: True, raising=False)
    _seed(isolated_store)
    assert client(OWNER).get(_artifact_url(_PDF_SHA)).status_code == 404


# ── unverified changes reach the route too ────────────────────────────────────

def test_an_applied_but_unverified_change_is_in_the_facts_response(client, isolated_store):
    _seed(isolated_store)
    isolated_store.log_decision(
        "system", "apply.saved_unverified", scan_id=SID, file=FILE, rule_id="1.1.1",
        detail=json.dumps({"artifact_sha256": "c" * 64, "rule_id": "1.1.1",
                           "changes": [{"locator": "pdf:fig:2", "before": "",
                                        "after": "Image B description"}]}))
    body = client(OWNER).get(_facts_url()).json()
    unverified = [c for c in body["savedChanges"] if c["verification"] == "not_verified"]
    assert len(unverified) == 1
    assert unverified[0]["after"] == "Image B description"
    assert body["accounting"]["savedChangesUnverified"] == 1
    # two saved changes, neither reviewed — a report cannot say the review is finished
    assert body["accounting"]["humanReviews"]["pending"] == 2

"""POST /scans/{sid}/report-render through the real app and its access gate.

Owner scoping, file membership, server-owned identity, and refusal codes are asserted over HTTP
(TestClient against app.app), not by calling the handler, so the gate that decides "authenticated
or not" is the production one.
"""
from __future__ import annotations

import io
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "api"))

pytest.importorskip("weasyprint")
pikepdf = pytest.importorskip("pikepdf")

OWNER = "owner@example.com"
OTHER = "other@example.com"
SOURCE_CHECKSUM = "d41d8cd98f00b204e9800998ecf8427e"
CORRECTED = "c" * 64


def _model(**over):
    body = {
        "docTitle": "Accessibility review — report.docx", "lang": "en-US",
        "identity": {"scanId": "forged", "file": "forged.docx", "correctedSha256": "f" * 64},
        "cover": {"title": "Accessibility review — report.docx", "subtitle": "Summary", "meta": []},
        "blocks": [{"k": "heading", "text": "Decision summary"},
                   {"k": "decisionSummary", "caption": "Where it stands", "items": [
                       {"key": "documentsAssessed", "label": "Documents assessed", "value": 1}]}],
    }
    body.update(over)
    return body


def _body(**over):
    body = {"kind": "file", "file": "report.docx", "mode": "summary", "model": _model(),
            "factsDigest": None}
    body.update(over)
    return body


def digest(store, sid="scan-1", file="report.docx", owner=OWNER):
    """The digest a client that has just READ the facts would send — computed by stream D's own
    helper, not restated here, so this test cannot drift into agreeing with a stale copy."""
    import report_facts
    return report_facts.current_digest(store, sid, file, owner=owner)


def fresh(store, **over):
    """A body whose factsDigest describes the store as it is right now."""
    body = _body(**over)
    body["factsDigest"] = digest(store, file=body["file"])
    return body


@pytest.fixture()
def client(monkeypatch, isolated_store):
    import core
    from fastapi.testclient import TestClient
    from app import app

    monkeypatch.setattr(core, "store", isolated_store)
    monkeypatch.setattr(core, "ACCESS_CODE", "", raising=False)
    monkeypatch.setattr(core, "GOOGLE_CLIENT_ID", "test-client-id", raising=False)
    monkeypatch.setattr(core, "E2E_KEY", None, raising=False)
    monkeypatch.setattr(core, "verify_gis_token", lambda tok: tok or None)
    monkeypatch.setattr(core, "email_allowed", lambda e: e in (OWNER, OTHER))
    monkeypatch.setattr(core, "PUBLIC_URL", "https://acp.example.com", raising=False)
    monkeypatch.setenv("ACP_BUILD_VERSION", "2026.9.17.3")
    for sid, owner in (("scan-1", OWNER), ("scan-2", OWNER), ("scan-other", OTHER)):
        isolated_store.init_scan_run(sid, "drive", 1, "2026-09-17T10:00:00Z", "rubric", "hash",
                                     owner=owner, status="completed")
    isolated_store.save_file_result("scan-1", {
        "file": "report.docx", "engine": "docx", "status": "done", "score": 80, "compliant": False,
        "skipped_rules": 0, "checksum": SOURCE_CHECKSUM, "issues": []}, "2026-09-17T10:00:00Z")
    isolated_store.save_file_result("scan-2", {
        "file": "elsewhere.docx", "engine": "docx", "status": "done", "score": 80, "compliant": False,
        "skipped_rules": 0, "issues": []}, "2026-09-17T10:00:00Z")
    isolated_store.record_remediation("scan-1", "report.docx", corrected_sha256=CORRECTED)
    made = TestClient(app)
    made.acp_store = isolated_store          # the tests need it to compute a real facts digest
    return made


def post(client, sid="scan-1", body=None, user=OWNER, **kw):
    headers = {"Authorization": f"Bearer {user}"} if user else {}
    headers.update(kw.pop("headers", {}))
    if body is None:
        body = fresh(client.acp_store)
    elif isinstance(body, dict) and body.get("factsDigest", "keep") is None:
        # None is _body()'s "fill this in for me"; an ABSENT key is a test about absence.
        # kinds 'scan' and 'remediation' bind to the SCAN-level digest (file=None), per contract v2.
        target = body.get("file") if body.get("kind") == "file" else None
        body = {**body, "factsDigest": digest(client.acp_store, sid, target) or "0" * 64}
    data = body if isinstance(body, (bytes, str)) else json.dumps(body)
    return client.post(f"/scans/{sid}/report-render", content=data,
                       headers={"Content-Type": "application/json", **headers}, **kw)


def test_the_route_is_behind_the_access_gate():
    import core
    assert core.is_public("/scans/scan-1/report-render") is False


def test_unauthenticated_request_is_rejected_by_the_gate(client):
    r = post(client, user=None)
    assert r.status_code == 401
    assert r.headers.get("X-Acp-Auth") == "session"


def test_owner_gets_a_tagged_pdf_with_server_identity(client):
    r = post(client)
    assert r.status_code == 200, r.text
    assert r.headers["content-type"] == "application/pdf"
    assert r.headers["cache-control"] == "no-store"
    assert r.headers["x-content-type-options"] == "nosniff"
    assert "accessibility-file-summary-report.docx.pdf" in r.headers["content-disposition"]
    with pikepdf.open(io.BytesIO(r.content)) as doc:
        assert "/StructTreeRoot" in doc.Root and str(doc.Root.Lang) == "en-US"
        assert bool(doc.Root.MarkInfo.Marked)
    from test_report_render import pdftext
    text = pdftext(r.content)
    assert "scan-1" in text and "forged" not in text
    assert SOURCE_CHECKSUM in text and "as recorded by the source system" in text
    assert CORRECTED in text and "f" * 64 not in text
    assert "2026.9.17.3" in text


def test_another_owners_scan_is_404(client):
    assert post(client, sid="scan-other").status_code == 404
    assert post(client, sid="scan-1", user=OTHER).status_code == 404
    assert post(client, sid="does-not-exist").status_code == 404


def test_a_file_outside_the_scan_is_404(client):
    # The file exists — in a different scan of the same owner. Membership is per scan.
    r = post(client, body=_body(file="elsewhere.docx"))
    assert r.status_code == 404
    assert post(client, body=_body(file="../../etc/passwd")).status_code == 404


def test_scan_report_without_a_file_renders(client):
    r = post(client, body=_body(kind="scan", file=None, mode="full"))
    assert r.status_code == 200, r.text
    assert r.content.startswith(b"%PDF")


def test_foreign_scan_is_404_even_with_an_invalid_body(client):
    """Ownership is decided before the body is judged, so a 422 cannot confirm a scan exists."""
    assert post(client, sid="scan-other", body=b"{not json").status_code == 404


@pytest.mark.parametrize("body,status", [
    (b"{not json", 422),
    (_body(model=_model(blocks=[{"k": "script", "text": "x"}])), 422),
    (_body(mode="everything"), 422),
    (_body(model=_model(blocks=[{"k": "image", "src": "data:text/html;base64,PHNjcmlwdD4=", "alt": "x"}])), 422),
    (_body(model=_model(blocks=[{"k": "text", "text": "x" * 200_001}])), 413),
])
def test_refusals(client, body, status):
    r = post(client, body=body)
    assert r.status_code == status, r.text


def test_oversize_body_is_refused_before_it_is_parsed(client, monkeypatch):
    import report_render
    monkeypatch.setattr(report_render, "MAX_BODY_BYTES", 2048)
    r = post(client, body=_body(model=_model(blocks=[{"k": "text", "text": "x" * 4000}])))
    assert r.status_code == 413


# ── the model must be bound to the facts it was built from ───────────────────────────────────
#
# The route overwrites identity from the store. Without this binding, a client holding a model
# built before the document changed gets that old evidence printed under the CURRENT checksums —
# a report that is wrong in the one way a reader cannot detect. So: no digest, no render; wrong
# digest, no render.

def test_a_request_without_a_facts_digest_is_422(client):
    body = _body()
    body.pop("factsDigest")
    r = post(client, body=body)
    assert r.status_code == 422
    assert "factsDigest" in r.text


def test_a_stale_facts_digest_is_409_with_the_contract_wording(client):
    r = post(client, body=_body(factsDigest="e" * 64))
    assert r.status_code == 409, r.text
    assert r.json()["detail"] == "report data is out of date; regenerate the report"


def test_the_digest_of_a_DIFFERENT_file_does_not_pass(client):
    """A digest is a claim about one artifact. Any valid-looking digest passing would make the
    check decorative."""
    other = digest(client.acp_store, "scan-2", "elsewhere.docx")
    assert other and other != digest(client.acp_store)
    assert post(client, body=_body(factsDigest=other)).status_code == 409


def test_a_digest_read_before_the_document_changed_stops_being_accepted(client):
    """The bite check: the SAME body renders now and is refused after the file is re-saved. If it
    still rendered, this whole section would be ceremony."""
    body = fresh(client.acp_store)
    assert post(client, body=body).status_code == 200, "the fresh digest must be accepted first"
    client.acp_store.record_remediation("scan-1", "report.docx", corrected_sha256="d" * 64)
    r = post(client, body=body)
    assert r.status_code == 409, r.text
    assert r.json()["detail"] == "report data is out of date; regenerate the report"
    # …and re-reading the facts makes the very same model renderable again.
    assert post(client, body={**body, "factsDigest": digest(client.acp_store)}).status_code == 200


def test_a_scan_report_binds_to_the_scan_level_digest(client):
    scan_digest = digest(client.acp_store, file=None)
    file_digest = digest(client.acp_store)
    assert scan_digest and scan_digest != file_digest
    assert post(client, body=_body(kind="scan", file=None, mode="summary",
                                   factsDigest=scan_digest)).status_code == 200
    assert post(client, body=_body(kind="scan", file=None, mode="summary",
                                   factsDigest=file_digest)).status_code == 409


def test_ownership_is_still_decided_before_the_digest(client):
    """A 409 on a scan you do not own would say the scan exists. It answers 404 like everything
    else on this route."""
    assert post(client, sid="scan-other", body=_body(factsDigest="e" * 64)).status_code == 404


# ── absolute links: ACP_PUBLIC_URL, else the request's own origin, else text only ─────────────

EVIDENCE_REL = "/?view=evidence&scan=scan-1&file=report.docx&finding=abc123"


def _linked_body(client):
    blocks = [{"k": "heading", "text": "Remaining work"},
              {"k": "findingCard", "id": "f-1", "title": "1.4.3 · Contrast", "criterion": "1.4.3",
               "location": {"label": "Page 3", "page": 3, "href": EVIDENCE_REL},
               "description": "Low contrast", "status": "open"}]
    return fresh(client.acp_store, model=_model(blocks=blocks), mode="full")


def _uris(pdf_bytes):
    out = []
    with pikepdf.open(io.BytesIO(pdf_bytes)) as doc:
        for page in doc.pages:
            for annot in page.get("/Annots") or []:
                action = annot.get("/A")
                if action is not None and action.get("/URI") is not None:
                    out.append(str(action.URI))
    return out


def test_the_configured_public_url_anchors_evidence_links(client):
    r = post(client, body=_linked_body(client))
    assert r.status_code == 200, r.text
    assert "https://acp.example.com" + EVIDENCE_REL in _uris(r.content)


def test_without_a_public_url_the_requests_own_https_origin_anchors_them(client, monkeypatch):
    import core
    monkeypatch.setattr(core, "PUBLIC_URL", "", raising=False)
    r = post(client, body=_linked_body(client),
             headers={"Host": "acp-app.westus2.azurecontainerapps.io", "X-Forwarded-Proto": "https",
                      "Origin": "https://acp-app.westus2.azurecontainerapps.io"})
    assert r.status_code == 200, r.text
    uris = _uris(r.content)
    assert "https://acp-app.westus2.azurecontainerapps.io" + EVIDENCE_REL in uris, uris
    assert not any(u.startswith("/") for u in uris)


@pytest.mark.parametrize("headers", [
    # plain http, not loopback: not a trusted origin
    {"Host": "acp.internal", "X-Forwarded-Proto": "http"},
    # the browser says the app lives somewhere else: this server cannot vouch for that origin
    {"Host": "api.example.com", "X-Forwarded-Proto": "https", "Origin": "https://app.example.com"},
])
def test_with_no_trusted_origin_the_location_is_printed_unlinked(client, monkeypatch, headers):
    import core
    monkeypatch.setattr(core, "PUBLIC_URL", "", raising=False)
    r = post(client, body=_linked_body(client), headers=headers)
    assert r.status_code == 200, r.text
    uris = _uris(r.content)
    assert not any("view=evidence" in u for u in uris), uris
    from test_report_render import pdftext
    assert "Page 3" in pdftext(r.content)


def test_an_unavailable_facts_module_refuses_rather_than_skipping_the_check(client, monkeypatch):
    """A check that silently passes when its dependency is missing is worse than no check: it
    reports success for the exact case it exists to catch."""
    import routes.report_render as route

    def boom(*a, **k):
        raise ImportError("no module named report_facts")

    monkeypatch.setattr(route, "current_facts_digest", boom)
    r = post(client)
    assert r.status_code == 503
    assert "current document" in r.text

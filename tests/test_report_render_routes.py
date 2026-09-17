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
    body = {"kind": "file", "file": "report.docx", "mode": "summary", "model": _model()}
    body.update(over)
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
    return TestClient(app)


def post(client, sid="scan-1", body=None, user=OWNER, **kw):
    headers = {"Authorization": f"Bearer {user}"} if user else {}
    headers.update(kw.pop("headers", {}))
    data = body if isinstance(body, (bytes, str)) else json.dumps(body if body is not None else _body())
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

"""The report routes are behind the capability gate, and the gate actually refuses.

WHY THIS IS A SEPARATE FILE FROM tests/test_capability_map_is_complete.py. That file proves every
route is MAPPED. Mapping is a table entry; it is not a refusal. The independent review's finding
was that three of these routes were reachable by any authenticated role because they were absent
from the map altogether — "unmapped routes bypass the capability check" — and it also said, in
the same breath, that owner isolation alone is not enough. So each new route is exercised twice
here: once by a real role that does NOT hold the capability, which must get 403, and once by a
role that does, which must not.

Owner isolation is tested separately (tests/test_report_facts_routes.py and
tests/test_change_review_routes.py, 404 for a foreign owner). The two answer different questions —
403 is "your role may not do this", 404 is "this is not yours" — and a test that only showed one
would leave the other free to be wrong.
"""
from __future__ import annotations

import hashlib
import sys
import tempfile
from pathlib import Path
from urllib.parse import quote

import pytest

ACP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ACP / "api"))

import workspace_rbac as rbac        # noqa: E402
import workspace_roles as wr         # noqa: E402

OWNER = "owner@hosp.org"
SID = "s-auth"
FILE = "handbook.pdf"
SHA = hashlib.sha256(b"whatever").hexdigest()
QFILE = quote(FILE, safe="")

# Four purpose-built roles, each holding exactly one thing, so a pass cannot come from somewhere
# else in the grid. `permissions` maps tab → level, plus grant → 'granted' (workspace_roles._permission_rows).
ROLES = {
    "nothing@hosp.org":   ("none", {"overview": rbac.VIEW}),
    "scanread@hosp.org":  ("scan-read", {"overview": rbac.VIEW, "assess": rbac.VIEW}),
    "exporter@hosp.org":  ("exporter", {"overview": rbac.VIEW, "publish": rbac.VIEW,
                                        "reports.export": "granted"}),
    "reviewer@hosp.org":  ("reviewer", {"overview": rbac.VIEW, "remediate": rbac.OPERATE}),
}
NOBODY, SCAN_READ, EXPORTER, REVIEWER = ROLES


@pytest.fixture()
def client(monkeypatch):
    import core
    import store as store_mod

    monkeypatch.setattr(store_mod, "_SQLITE_PATH", Path(tempfile.mkdtemp()) / "acp-test.db")
    st = store_mod.Store()
    monkeypatch.setattr(core, "store", st, raising=False)
    monkeypatch.setattr(core, "get_store", lambda: st, raising=False)
    monkeypatch.setattr(core, "OWNER_EMAIL", OWNER, raising=False)
    monkeypatch.setattr(core, "OPEN_ACCESS", True, raising=False)
    monkeypatch.setattr(core, "ACCESS_CODE", "", raising=False)
    monkeypatch.setattr(core, "GOOGLE_CLIENT_ID", "test-client", raising=False)
    monkeypatch.setattr(core, "E2E_KEY", None, raising=False)
    monkeypatch.setattr(core, "verify_gis_token", lambda t: (t or "").strip().lower() or None,
                        raising=False)
    monkeypatch.setattr(core, "email_allowed", lambda e: bool(e), raising=False)
    monkeypatch.setenv(wr.FLAG, "1")

    for email in (OWNER, *ROLES):
        st.upsert_person({"email": email, "role": "user", "status": "access_ready"})
    wr.seed_builtin_roles(st, tenant_id=OWNER)
    for email, (role_id, permissions) in ROLES.items():
        st.upsert_workspace_role(tenant_id=OWNER, role_id=role_id, name=role_id,
                                 permissions=permissions, expected_version=None)
        wr.assign_role(st, email=email, role_id=role_id, actor=OWNER)

    # Every role's scan, so a 404 can never be mistaken for a 403. The scans are owned by the
    # CALLER in each case — what is under test is the role, not the object.
    for email in (OWNER, *ROLES):
        st.save_scan({
            "_scan_id": f"{SID}-{email}", "started_at": "2026-09-01T05:00:00+00:00",
            "completed_at": "2026-09-01T05:05:00+00:00", "source": "local", "owner": email,
            "rubric": {"name": "wcag-aa", "hash": "h"},
            "summary": {"files": 1, "certifiable": 1, "uncertain": 0, "error": 0,
                        "avg_score": 90},
            "files": [{"file": FILE, "engine": "pdf", "status": "certifiable", "score": 90,
                       "compliant": 1, "skipped_rules": 0, "issues": [],
                       "checksum": "md5-source-1"}]})
        st.record_remediation(f"{SID}-{email}", FILE, corrected_sha256="c" * 64)
        st.record_remediation_diffs(f"{SID}-{email}", FILE,
                                    [{"rule_id": "1.1.1", "before": "", "after": "A red barn"}])

    from fastapi.testclient import TestClient
    tc = TestClient(__import__("app").app)

    def call(method, path_template, who, **kwargs):
        path = path_template.format(sid=f"{SID}-{who}")
        return tc.request(method, path, headers={"Authorization": f"Bearer {who}"}, **kwargs)
    return call


READS = [
    "/scans/{sid}/files/" + QFILE + "/report-facts",
    "/scans/{sid}/report-facts",
    "/scans/{sid}/files/" + QFILE + "/change-reviews",
    "/scans/{sid}/files/" + QFILE + f"/artifact/{SHA}/page/1",
]


@pytest.mark.parametrize("path", READS)
def test_a_role_without_any_scan_view_is_refused_the_reads(client, path):
    r = client("GET", path, NOBODY)
    assert r.status_code == 403, f"{path} answered {r.status_code} to a role with no scan access"
    assert r.json()["capability_denied"] is True


@pytest.mark.parametrize("path", READS)
def test_a_role_with_a_scan_view_is_admitted_to_the_reads(client, path):
    r = client("GET", path, SCAN_READ)
    assert r.status_code != 403, r.text


def test_rendering_a_report_needs_the_export_grant_not_merely_a_tab(client):
    """An export leaves the workspace. PRD §5: a sensitive action is not implied by tab access."""
    body = {"kind": "file", "file": FILE, "mode": "summary", "model": {}}
    refused = client("POST", "/scans/{sid}/report-render", SCAN_READ, json=body)
    assert refused.status_code == 403
    assert "reports.export" in refused.json()["required"]
    allowed = client("POST", "/scans/{sid}/report-render", EXPORTER, json=body)
    assert allowed.status_code != 403, allowed.text


def test_recording_a_verdict_needs_the_review_capability(client):
    """A role that can WATCH remediation must not be able to sign off the changes it produced."""
    import report_facts
    path = "/scans/{sid}/files/" + QFILE + "/change-reviews/" + quote(
        f"{FILE}::1.1.1::0", safe="")
    body = {"verdict": "accepted", "expected_sha256": "c" * 64,
            "change_digest": report_facts.change_digest("1.1.1", "", "A red barn")}
    refused = client("PUT", path, SCAN_READ, json=body)
    assert refused.status_code == 403
    assert "remediate.review" in refused.json()["required"]
    allowed = client("PUT", path, REVIEWER, json=body)
    assert allowed.status_code == 200, allowed.text


def test_the_exporter_role_cannot_record_a_verdict_either(client):
    """The two capabilities are separate, and holding one is not holding the other."""
    import report_facts
    path = "/scans/{sid}/files/" + QFILE + "/change-reviews/" + quote(
        f"{FILE}::1.1.1::0", safe="")
    r = client("PUT", path, EXPORTER, json={
        "verdict": "accepted", "expected_sha256": "c" * 64,
        "change_digest": report_facts.change_digest("1.1.1", "", "A red barn")})
    assert r.status_code == 403


def test_no_new_route_is_exempt_from_the_capability_map():
    """A bite check on the two tests above: if any of these were EXEMPT rather than mapped, a
    role without the grant would sail through and both would still look like they passed for
    some other reason."""
    import workspace_capability_map as capmap
    for method, path in [
            ("GET", "/scans/{sid}/files/{filename:path}/report-facts"),
            ("GET", "/scans/{sid}/report-facts"),
            ("GET", "/scans/{sid}/files/{filename:path}/change-reviews"),
            ("GET", "/scans/{sid}/files/{filename:path}/artifact/{sha256}/page/{page}"),
            ("POST", "/scans/{sid}/report-render"),
            ("PUT", "/scans/{sid}/files/{filename:path}/change-reviews/{change_id:path}")]:
        assert capmap.required_capabilities(method, path), f"{method} {path} is unmapped"
        assert not capmap.is_exempt(method, path), f"{method} {path} is exempt"
    assert capmap.required_capabilities(
        "PUT", "/scans/{sid}/files/{filename:path}/change-reviews/{change_id:path}") == \
        frozenset({"remediate.review"})
    assert capmap.required_capabilities("POST", "/scans/{sid}/report-render") == \
        frozenset({"release.view", "reports.export"})

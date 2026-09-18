"""The access gate must judge the path the ROUTER will dispatch, not a URL rebuilt from it.

R-C1 (2026-09-17). `_access_gate` asked `core.is_public(request.url.path)`. Starlette builds
`request.url` by re-joining the DECODED path into a URL string and re-parsing it, so a filename
holding an encoded `#` or `?` is cut short there:

    GET /scans/S/files/a%23b.xlsx/report-facts
        scope["path"]      == "/scans/S/files/a#b.xlsx/report-facts"   (what the router matches)
        request.url.path   == "/scans/S/files/a"                       (the rest became a fragment)

`/scans/S/files/a` matches no registered route, so `is_public` said True and the gate waved the
request through without credentials; the handler then ran as owner "demo". Two failures from one
line: an ANONYMOUS caller reached a protected `{filename:path}` route, and a SIGNED-IN owner got
404 for their own document (their identity was never stamped, so the read ran as "demo").

Everything here goes through real HTTP — TestClient on the real app, the real gate, and a real
SQLite store — because the defect lives in the middleware, which a handler-level test never runs.
The capability middleware in the same file already reads `request.scope["path"]`; the gate now
does too.

A second, independent concern pinned here: a cross-origin browser only sees the response headers
CORSMiddleware lists in `expose_headers`. The provenance headers the preview/report routes send
(R-A2) are useless to a frontend on another origin unless they are listed, and a per-route
Access-Control-Expose-Headers is overwritten by the middleware, so the list in app.py is the only
place they can be exposed.
"""
from __future__ import annotations

import base64
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "api"))

OWNER = "owner@example.com"
FOREIGN = "foreign@example.com"
SID = "c0ffee000001"
DEMO_SID = "c0ffee000002"

# Names a real user can give a document; '#' and '?' are the two characters that, percent-encoded,
# survive into scope["path"] decoded and are then re-read as URL delimiters by request.url.
NAMES = ["a#b.xlsx", "a?b.xlsx", "q3 #1 report?.pdf"]


def _enc(name: str) -> str:
    from urllib.parse import quote
    return quote(name, safe="")


def _seed(store, sid: str, owner: str, names: list[str]) -> None:
    store.save_scan({
        "_scan_id": sid,
        "started_at": "2026-09-17T10:00:00+00:00",
        "completed_at": "2026-09-17T10:05:00+00:00",
        "source": "drive",
        "owner": owner,
        "rubric": {"name": "wcag-aa", "hash": "h"},
        "summary": {"files": len(names), "certifiable": len(names), "uncertain": 0,
                    "error": 0, "avg_score": 90},
        "files": [{"file": n, "engine": "pdf" if n.endswith(".pdf") else "office",
                   "status": "certifiable", "score": 90, "compliant": 1,
                   "skipped_rules": 0, "issues": []} for n in names],
    })


@pytest.fixture()
def gis_client(monkeypatch, isolated_store):
    """The production configuration: ACP_ACCESS_CODE empty, GIS sign-in required. token == email."""
    import core
    from fastapi.testclient import TestClient

    from app import app

    monkeypatch.setattr(core, "store", isolated_store)
    monkeypatch.setattr(core, "ACCESS_CODE", "", raising=False)
    monkeypatch.setattr(core, "GOOGLE_CLIENT_ID", "test-client-id", raising=False)
    monkeypatch.setattr(core, "E2E_KEY", None, raising=False)
    monkeypatch.setattr(core, "OWNER_EMAIL", OWNER, raising=False)
    monkeypatch.setattr(core, "verify_gis_token", lambda tok: tok or None)
    monkeypatch.setattr(core, "email_allowed", lambda e: e in (OWNER, FOREIGN))
    _seed(isolated_store, SID, OWNER, NAMES)
    # A document the "demo" owner holds, so an anonymous request that falls through to owner
    # "demo" would be served real content rather than an ambiguous 404.
    _seed(isolated_store, DEMO_SID, "demo", NAMES)
    return TestClient(app)


def _facts(client, sid, name, **headers):
    return client.get(f"/scans/{sid}/files/{_enc(name)}/report-facts", headers=headers)


def _bearer(email):
    return {"Authorization": f"Bearer {email}"}


def test_the_router_and_the_url_disagree_about_these_paths():
    """The premise, stated as a fact about Starlette rather than assumed: if request.url ever
    stops truncating, this fails and the reason for reading scope["path"] should be re-examined
    (the fix stays correct either way — scope["path"] is what the router dispatches on)."""
    from starlette.requests import Request

    scope = {"type": "http", "method": "GET", "scheme": "http", "server": ("t", 80),
             "root_path": "", "query_string": b"", "headers": [],
             "path": "/scans/S/files/a#b.xlsx/report-facts"}
    req = Request(scope)
    assert req.scope["path"] == "/scans/S/files/a#b.xlsx/report-facts"
    assert req.url.path == "/scans/S/files/a"


@pytest.mark.parametrize("name", NAMES)
def test_anonymous_request_for_an_encoded_name_is_refused(gis_client, name):
    # DEMO_SID first: before the fix it is the one that is SERVED (200, real content) rather than
    # merely answered, which is the unambiguous form of the leak.
    for sid in (DEMO_SID, SID):
        r = _facts(gis_client, sid, name)
        assert r.status_code == 401, (
            f"anonymous GET of {name!r} on {sid} answered {r.status_code} — the gate let an "
            f"unauthenticated request through to a protected route: {r.text[:200]}")
        assert r.headers.get("X-Acp-Auth") == "session"


@pytest.mark.parametrize("name", NAMES)
def test_the_owner_gets_their_document(gis_client, name):
    r = _facts(gis_client, SID, name, **_bearer(OWNER))
    assert r.status_code == 200, (
        f"owner asked for their own {name!r} and got {r.status_code}: {r.text[:200]}")
    # The document served is the one asked for — not some prefix of its name.
    assert name in r.text


@pytest.mark.parametrize("name", NAMES)
def test_a_foreign_owner_is_not_served(gis_client, name):
    r = _facts(gis_client, SID, name, **_bearer(FOREIGN))
    assert r.status_code in (403, 404), (
        f"a foreign signed-in user read {name!r}: {r.status_code} {r.text[:200]}")


def test_a_plain_name_behaves_the_same_before_and_after(gis_client, isolated_store):
    """Non-vacuity for the fixture, and a guard that the fix changed nothing for ordinary names."""
    _seed(isolated_store, "c0ffee000003", OWNER, ["plain.pdf"])
    assert _facts(gis_client, "c0ffee000003", "plain.pdf").status_code == 401
    assert _facts(gis_client, "c0ffee000003", "plain.pdf", **_bearer(OWNER)).status_code == 200
    assert _facts(gis_client, "c0ffee000003", "plain.pdf",
                  **_bearer(FOREIGN)).status_code in (403, 404)


def test_public_routes_stay_public(gis_client):
    """Reading scope["path"] must not close anything that was deliberately open."""
    assert gis_client.get("/healthz").status_code != 401


def test_the_access_code_gate_is_not_bypassed_either(monkeypatch, isolated_store):
    """The Basic-auth branch sits behind the same is_public check."""
    import core
    from fastapi.testclient import TestClient

    from app import app

    monkeypatch.setattr(core, "store", isolated_store)
    monkeypatch.setattr(core, "ACCESS_CODE", "s3cret", raising=False)
    monkeypatch.setattr(core, "GOOGLE_CLIENT_ID", "", raising=False)
    monkeypatch.setattr(core, "E2E_KEY", None, raising=False)
    _seed(isolated_store, DEMO_SID, "demo", NAMES)
    client = TestClient(app)
    for name in NAMES:
        assert _facts(client, DEMO_SID, name).status_code == 401, name
        auth = "Basic " + base64.b64encode(b"u:s3cret").decode()
        assert _facts(client, DEMO_SID, name, Authorization=auth).status_code == 200, name


# ── the '/trace/' carve-out: public by route SHAPE, never by substring ────────────────────────
#
# is_public used to wave through any path with `startswith("/scans/") and "/trace/" in path`, so
# a document inside a folder named `trace` opened every per-file route — reads AND writes — to an
# anonymous caller running as owner "demo". Found while writing the R-C1 tests above; the rule now
# matches only /scans/{sid}/trace/... (core._PUBLIC_TRACE_ROUTE).

TRACE_SID = "c0ffee000004"
TRACE_NAMES = ["trace/x.pdf", "x/trace/y.pdf"]


@pytest.fixture()
def trace_client(gis_client, isolated_store):
    # Owned by "demo": before the fix an anonymous request ran as "demo", so it was SERVED here
    # (and a PUT wrote), which is the unambiguous form of the bypass.
    _seed(isolated_store, TRACE_SID, "demo", TRACE_NAMES)
    _seed(isolated_store, "c0ffee000005", OWNER, TRACE_NAMES)
    return gis_client


@pytest.mark.parametrize("name", TRACE_NAMES)
@pytest.mark.parametrize("method,suffix,body", [
    ("get", "/report-facts", None),
    ("get", "/status", None),
    ("post", "/confirm", {"sc": "1.4.11"}),
    ("put", "/change-reviews/{name}::1.1.1::0", {"verdict": "accepted"}),
])
def test_a_folder_named_trace_does_not_open_a_files_route(trace_client, name, method, suffix,
                                                           body):
    url = f"/scans/{TRACE_SID}/files/{name}{suffix.format(name=name)}"
    r = trace_client.request(method.upper(), url, json=body)
    assert r.status_code == 401, (
        f"anonymous {method.upper()} {url} was answered {r.status_code}: {r.text[:160]}")
    assert r.headers.get("X-Acp-Auth") == "session"


@pytest.mark.parametrize("name", TRACE_NAMES)
@pytest.mark.parametrize("method", ["get", "put"])
def test_a_folder_named_trace_does_not_open_decisions(trace_client, isolated_store, name, method):
    url = f"/scans/{TRACE_SID}/decisions/{name}"
    kwargs = {"json": {"value": "inscope"}} if method == "put" else {}
    r = trace_client.request(method.upper(), url, **kwargs)
    assert r.status_code == 401, (
        f"anonymous {method.upper()} {url} was answered {r.status_code}: {r.text[:160]}")
    # And nothing was written behind the refusal.
    assert not isolated_store.get_decisions(TRACE_SID, owner="demo")


@pytest.mark.parametrize("name", TRACE_NAMES)
def test_the_owner_of_a_file_in_a_trace_folder_is_still_served(trace_client, isolated_store, name):
    sid = "c0ffee000005"
    r = trace_client.get(f"/scans/{sid}/files/{name}/report-facts", headers=_bearer(OWNER))
    assert r.status_code == 200, r.text[:200]
    assert name in r.text
    w = trace_client.put(f"/scans/{sid}/decisions/{name}", json={"value": "inscope"},
                         headers=_bearer(OWNER))
    assert w.status_code == 200, w.text[:200]
    assert isolated_store.get_decisions(sid, owner=OWNER)
    # A foreign signed-in user is still refused on the same paths.
    assert trace_client.get(f"/scans/{sid}/files/{name}/report-facts",
                            headers=_bearer(FOREIGN)).status_code in (403, 404)


# Every trace route routes/scans.py registers, instantiated. Each is documented "Public" in its
# own docstring (plain <a> redirect targets and the in-app trace panels' reads), so they stay
# reachable without a bearer — the fix must not close them.
INTENDED_PUBLIC_TRACE_PATHS = [
    "/scans/{sid}/trace/scan",
    "/scans/{sid}/trace/assess/exists",
    "/scans/{sid}/trace/session",
    "/scans/{sid}/trace/session/data",
    "/scans/{sid}/trace/file/a.pdf",
    "/scans/{sid}/trace/file/dir/trace/a.pdf",
    "/scans/{sid}/trace/file/a.pdf/data",
    "/scans/{sid}/trace/file/a.pdf/history",
    "/scans/{sid}/trace/file/a.pdf/exists",
]


@pytest.mark.parametrize("template", INTENDED_PUBLIC_TRACE_PATHS)
def test_the_intended_trace_routes_still_answer_anonymously(trace_client, template):
    import core
    path = template.format(sid="c0ffee000005")   # owned by OWNER: anonymous reads as "demo" → 404
    assert core.is_public(path), path
    r = trace_client.get(path, follow_redirects=False)
    assert r.status_code != 401, f"{path} now needs sign-in: {r.status_code} {r.text[:120]}"
    # Public does not mean unscoped: the route's own owner check still hides a foreign scan.
    assert r.status_code == 404 or r.json() == {"available": False}, (r.status_code, r.text[:120])


def test_no_other_registered_route_is_opened_by_the_trace_rule():
    """Worst case for every registered route: each path parameter filled with 'trace' (and the
    :path ones with a 'trace' folder). No non-trace route may come out public. This caught a real
    collision the shape alone admits: /scans/jobs/trace/stream has the trace shape and dispatches
    to GET /scans/jobs/{job_id}/stream."""
    import core
    from app import app

    opened = []
    for route in core.enumerate_api_routes(app):
        if (route.path in core.ALWAYS_PUBLIC or route.path.startswith("/public/")
                or route.path.startswith("/scans/{sid}/trace/")):
            continue
        sample = (route.path.replace("{filename:path}", "dir/trace/f.pdf")
                  .replace("{change_id:path}", "dir/trace/f.pdf::1.1.1::0"))
        sample = re.sub(r"\{[^}]+\}", "trace", sample)
        if core.is_public(sample):
            opened.append((route.path, sample))
    assert not opened, opened


def test_every_registered_trace_route_is_public():
    import core
    from app import app

    trace_routes = [r.path for r in core.enumerate_api_routes(app)
                    if r.path.startswith("/scans/{sid}/trace/")]
    assert len(trace_routes) == 8, trace_routes          # non-vacuity; update if routes change
    for template in trace_routes:
        sample = (template.replace("{sid}", "c0ffee000005").replace("{kind}", "assess")
                  .replace("{filename:path}", "dir/a.pdf"))
        assert core.is_public(sample), (template, sample)


@pytest.mark.parametrize("path", [
    "/scans/S/files/trace/x.pdf/report-facts",
    "/scans/S/files/x/trace/y.pdf/report-facts",
    "/scans/S/decisions/trace/x.pdf",
    "/scans/S/traces",
    "/trace/x",
])
def test_the_shape_rule_itself_is_anchored(path):
    """Two layers close the bypass: the anchored shape (`trace` is the segment right after the
    scan id) and the route-table check (every route the path matches is a trace route). Either
    alone closes the files/decisions cases above over HTTP, so the HTTP tests cannot tell whether
    the shape is anchored — this pins that layer on its own."""
    import core
    assert not core._PUBLIC_TRACE_ROUTE.match(path)


def test_the_colliding_job_stream_path_is_gated(trace_client):
    assert trace_client.get("/scans/jobs/trace/stream").status_code == 401


def test_the_authed_traces_endpoint_is_not_swept_in(trace_client):
    assert trace_client.get(f"/scans/{TRACE_SID}/traces").status_code == 401


# ── R-A2: the provenance headers are readable cross-origin ────────────────────────────────────

PROVENANCE = ["X-ACP-Artifact-Sha256", "X-ACP-Rendered-Page", "X-ACP-Page-Count"]


def _exposed(response) -> set[str]:
    raw = response.headers.get("access-control-expose-headers", "")
    return {h.strip().lower() for h in raw.split(",") if h.strip()}


def test_cors_exposes_the_provenance_headers(gis_client):
    r = _facts(gis_client, SID, "a#b.xlsx", Origin="https://app.example", **_bearer(OWNER))
    assert r.status_code == 200, r.text[:200]
    exposed = _exposed(r)
    missing = [h for h in PROVENANCE if h.lower() not in exposed]
    assert not missing, f"not exposed cross-origin: {missing} (got {sorted(exposed)})"
    # The headers that were already exposed still are.
    assert {"x-acp-auth", "content-disposition"} <= exposed


def test_the_middleware_configuration_lists_them():
    """Belt and braces against a response path that happens not to pass through CORSMiddleware:
    the configured list itself carries all five."""
    from starlette.middleware.cors import CORSMiddleware

    from app import app

    cors = [m for m in app.user_middleware if m.cls is CORSMiddleware]
    assert len(cors) == 1
    listed = {h.lower() for h in cors[0].kwargs["expose_headers"]}
    assert {h.lower() for h in PROVENANCE + ["X-Acp-Auth", "Content-Disposition"]} <= listed

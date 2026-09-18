"""R-A1: the GENERIC page preview says which page it drew.

GET /scans/{scan_id}/files/{filename:path}/page/{page} clamps: page 999 of a one-page PDF is a 200
with page 1, byte-identical, and until now nothing in the response said so. The client
(api.getFilePage(..., {detail: true}), PagePreview, Thumbnail, fileReportData.collectPreviews)
already reads `X-ACP-Rendered-Page`. A different page is shown as "page N is not in the document
... (it drew page M)", and an absent header as "Page N · not confirmed". This pins the server half:
- the clamp is KEPT (still 200, still the same bytes);
- the 200 says the page it drew (min(page, count)) and the count;
- the cache-hit path says the same thing without touching the source;
- an unknown count sends NO header, rather than a guessed one.

Real HTTP throughout: TestClient on the real app, the real GIS gate with a bearer, a real SQLite
store, a real local-corpus source file and the real pdfium renderer. Only blob storage is faked,
with an in-memory dict, because it is not configured locally (download_render would always miss,
and the cache path could not be exercised at all).
"""
from __future__ import annotations

import io
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "api"))

OWNER = "owner@example.com"
SID = "feed00000001"
ONE = "one.pdf"
TWO = "two.pdf"


def _pdf(pages: int) -> bytes:
    import pypdfium2 as pdfium
    doc = pdfium.PdfDocument.new()
    for i in range(pages):
        doc.new_page(200 + 100 * i, 300)      # distinct sizes, so pages render differently
    buf = io.BytesIO()
    doc.save(buf)
    doc.close()
    return buf.getvalue()


class _Cache(dict):
    """The in-memory render cache. `fail_suffixes`: blob keys whose next writes fail."""
    def __init__(self):
        super().__init__()
        self.fail_suffixes: set[str] = set()


@pytest.fixture()
def env(monkeypatch, isolated_store, tmp_path):
    import blob
    import core
    from fastapi.testclient import TestClient

    from app import app

    monkeypatch.setattr(core, "store", isolated_store)
    monkeypatch.setattr(core, "ACCESS_CODE", "", raising=False)
    monkeypatch.setattr(core, "GOOGLE_CLIENT_ID", "test-client-id", raising=False)
    monkeypatch.setattr(core, "E2E_KEY", None, raising=False)
    monkeypatch.setattr(core, "OWNER_EMAIL", OWNER, raising=False)
    monkeypatch.setattr(core, "verify_gis_token", lambda tok: tok or None)
    monkeypatch.setattr(core, "email_allowed", lambda e: e == OWNER)

    corpus = tmp_path / "corpus"
    corpus.mkdir()
    (corpus / ONE).write_bytes(_pdf(1))
    (corpus / TWO).write_bytes(_pdf(2))
    monkeypatch.setenv("ACP_LOCAL_CORPUS", str(corpus))

    isolated_store.save_scan({
        "_scan_id": SID, "started_at": "2026-09-18T10:00:00+00:00",
        "completed_at": "2026-09-18T10:05:00+00:00", "source": "local", "owner": OWNER,
        "rubric": {"name": "wcag-aa", "hash": "h"},
        "summary": {"files": 2, "certifiable": 2, "uncertain": 0, "error": 0, "avg_score": 90},
        "files": [{"file": n, "engine": "pdf", "status": "certifiable", "score": 90,
                   "compliant": 1, "skipped_rules": 0, "issues": []} for n in (ONE, TWO)],
    })

    cache = _Cache()
    monkeypatch.setattr(blob, "download_render", lambda o, s, k: cache.get((o, s, k)))

    def upload(o, s, k, data):
        # blob.upload_render's real contract on a failed write: None, nothing stored, no raise.
        if any(k.endswith(suffix) for suffix in cache.fail_suffixes):
            return None
        cache[(o, s, k)] = data
        return f"mem://{k}"
    monkeypatch.setattr(blob, "upload_render", upload)

    client = TestClient(app)
    client.headers.update({"Authorization": f"Bearer {OWNER}"})
    return client, cache, corpus


def _page(client, name, page, **params):
    return client.get(f"/scans/{SID}/files/{name}/page/{page}", params=params)


def test_page_one_of_a_one_page_pdf_says_page_one(env):
    client, _, _ = env
    r = _page(client, ONE, 1)
    assert r.status_code == 200, r.text[:200]
    assert r.headers["content-type"] == "image/png"
    assert r.headers.get("X-ACP-Rendered-Page") == "1"
    assert r.headers.get("X-ACP-Page-Count") == "1"


def test_page_999_keeps_the_clamp_and_says_so(env):
    client, _, _ = env
    first = _page(client, ONE, 1)
    r = _page(client, ONE, 999)
    assert r.status_code == 200, "the documented clamp is kept: an out-of-range page is still a 200"
    assert r.content == first.content, "and it is the same picture as page 1"
    assert r.headers.get("X-ACP-Rendered-Page") == "1"
    assert r.headers.get("X-ACP-Page-Count") == "1"


def test_a_two_page_pdf_names_the_page_it_drew(env):
    client, _, _ = env
    p2 = _page(client, TWO, 2)
    assert (p2.headers.get("X-ACP-Rendered-Page"), p2.headers.get("X-ACP-Page-Count")) == ("2", "2")
    p9 = _page(client, TWO, 9)
    assert (p9.headers.get("X-ACP-Rendered-Page"), p9.headers.get("X-ACP-Page-Count")) == ("2", "2")
    assert p9.content == p2.content
    p1 = _page(client, TWO, 1)
    assert p1.headers.get("X-ACP-Rendered-Page") == "1" and p1.content != p2.content


def test_the_cache_hit_path_sends_the_same_headers_without_the_source(env, monkeypatch):
    client, cache, corpus = env
    miss = _page(client, ONE, 999)
    assert any(k[2] == f"{ONE}#p999" for k in cache), "the miss must populate the render cache"

    # From here on the source is gone and the renderer would explode: a 200 can only be the cache.
    (corpus / ONE).unlink()
    import render
    monkeypatch.setattr(render, "render_page_png", lambda *a, **k: pytest.fail("re-rendered"))
    monkeypatch.setattr(render, "page_count", lambda *a, **k: pytest.fail("re-counted"))

    hit = _page(client, ONE, 999)
    assert hit.status_code == 200
    assert hit.content == miss.content
    assert hit.headers.get("X-ACP-Rendered-Page") == "1"
    assert hit.headers.get("X-ACP-Page-Count") == "1"


def test_a_cached_page_with_no_cached_count_invents_nothing(env):
    """A render cached before this change has no count beside it. Say nothing rather than guess."""
    client, cache, _ = env
    cache[(OWNER, SID, f"{ONE}#p3")] = b"\x89PNG-from-an-older-deploy"
    r = _page(client, ONE, 3)
    assert r.status_code == 200 and r.content == b"\x89PNG-from-an-older-deploy"
    assert "X-ACP-Rendered-Page" not in r.headers
    assert "X-ACP-Page-Count" not in r.headers


def test_an_unknown_count_on_a_fresh_render_invents_nothing(env, monkeypatch):
    client, cache, _ = env
    import render
    monkeypatch.setattr(render, "page_count", lambda data, ext: None)
    r = _page(client, ONE, 999)
    assert r.status_code == 200
    assert "X-ACP-Rendered-Page" not in r.headers
    assert "X-ACP-Page-Count" not in r.headers
    # ...and the cache hit that follows is just as silent.
    again = _page(client, ONE, 999)
    assert "X-ACP-Rendered-Page" not in again.headers


def test_a_fresh_rerender_does_not_leave_an_old_count_behind(env, monkeypatch):
    client, cache, _ = env
    assert _page(client, ONE, 1).headers.get("X-ACP-Page-Count") == "1"
    import render
    monkeypatch.setattr(render, "page_count", lambda data, ext: None)
    assert "X-ACP-Page-Count" not in _page(client, ONE, 1, fresh=1).headers
    assert "X-ACP-Page-Count" not in _page(client, ONE, 1).headers   # the next cache hit


def test_the_headers_are_readable_cross_origin(env):
    """R-A2 is what makes these usable from a frontend on another origin (app.py expose_headers)."""
    client, _, _ = env
    r = client.get(f"/scans/{SID}/files/{ONE}/page/1", headers={"Origin": "https://app.example"})
    exposed = {h.strip().lower() for h in r.headers.get("access-control-expose-headers", "").split(",")}
    assert {"x-acp-rendered-page", "x-acp-page-count"} <= exposed


# ── provenance belongs to the exact cached PNG, never to the file ─────────────────────────────
#
# The first prepared patch cached ONE count per file and applied it to every cached page. The
# parent reproduced what that does (/tmp/test_acp_parent_page_cache_count.py, which PASSED while
# asserting the bug): page 999 cached from a one-page version shows page 1; the file grows to two
# pages; a fresh page-1 render updates the shared count to 2; the cached page-999 PNG, still the
# old page-1 pixels, is then served as "Rendered-Page: 2". A label on the wrong picture is worse
# than no label, because the client believes it.

def _labels(r):
    return r.headers.get("X-ACP-Rendered-Page"), r.headers.get("X-ACP-Page-Count")


def _meta_key(name, page):
    return (OWNER, SID, f"{name}#p{page}#meta")


def test_old_cached_page_must_not_inherit_a_new_render_count(env):
    """The parent's reproduction, inverted into a regression."""
    client, cache, corpus = env
    original = _page(client, ONE, 999)
    assert original.status_code == 200
    assert original.headers["X-ACP-Rendered-Page"] == "1"
    (corpus / ONE).write_bytes(_pdf(2))
    fresh = _page(client, ONE, 1, fresh=1)
    assert fresh.headers["X-ACP-Page-Count"] == "2"
    cached = _page(client, ONE, 999)
    assert cached.content == original.content          # still the old page-1 pixels...
    assert cached.headers.get("X-ACP-Rendered-Page") != "2"
    # ...so it either keeps the labels that were true of those pixels, or says nothing.
    assert _labels(cached) in {("1", "1"), (None, None)}, _labels(cached)


def test_re_rendering_a_different_page_leaves_each_cached_png_with_its_own_labels(env):
    client, cache, corpus = env
    v1_p999 = _page(client, ONE, 999)
    v1_p1 = _page(client, ONE, 1)
    assert _labels(v1_p999) == _labels(v1_p1) == ("1", "1")

    (corpus / ONE).write_bytes(_pdf(2))
    v2_p999 = _page(client, ONE, 999, fresh=1)          # now draws page 2 of 2
    assert _labels(v2_p999) == ("2", "2")
    assert v2_p999.content != v1_p999.content

    hit_999 = _page(client, ONE, 999)
    assert hit_999.content == v2_p999.content and _labels(hit_999) == ("2", "2")
    hit_1 = _page(client, ONE, 1)                        # never re-rendered: the V1 picture
    assert hit_1.content == v1_p1.content
    assert _labels(hit_1) in {("1", "1"), (None, None)}, _labels(hit_1)


def test_a_failed_sidecar_write_after_the_png_landed_gives_no_false_headers(env):
    client, cache, corpus = env
    v1 = _page(client, ONE, 999)
    assert _labels(v1) == ("1", "1")                     # a V1 sidecar is now cached

    (corpus / ONE).write_bytes(_pdf(2))
    cache.fail_suffixes = {"#meta"}                      # the PNG write lands, the sidecar does not
    fresh = _page(client, ONE, 999, fresh=1)
    assert _labels(fresh) == ("2", "2")                  # true of THIS response's bytes
    cache.fail_suffixes = set()

    hit = _page(client, ONE, 999)
    assert hit.content == fresh.content                  # the new PNG is what is cached...
    assert _labels(hit) == (None, None), _labels(hit)    # ...and the stale V1 sidecar is refused


def test_a_first_render_whose_sidecar_write_failed_gives_no_headers_on_the_hit(env):
    client, cache, _ = env
    cache.fail_suffixes = {"#meta"}
    assert _labels(_page(client, ONE, 1)) == ("1", "1")
    cache.fail_suffixes = set()
    hit = _page(client, ONE, 1)
    assert hit.status_code == 200 and _labels(hit) == (None, None)


def test_a_failed_png_write_writes_no_sidecar_and_the_old_pair_stays_true(env):
    client, cache, corpus = env
    v1 = _page(client, ONE, 999)
    old_meta = cache[_meta_key(ONE, 999)]

    (corpus / ONE).write_bytes(_pdf(2))
    cache.fail_suffixes = {"#p999"}                      # the PNG write fails
    fresh = _page(client, ONE, 999, fresh=1)
    assert _labels(fresh) == ("2", "2")
    cache.fail_suffixes = set()
    assert cache[_meta_key(ONE, 999)] == old_meta, "a sidecar was written for a PNG that never landed"

    hit = _page(client, ONE, 999)
    assert hit.content == v1.content
    assert _labels(hit) in {("1", "1"), (None, None)}, _labels(hit)


def test_a_sidecar_that_landed_without_its_png_gives_no_headers(env):
    """The reverse order, as a cache state: a V2 sidecar (describing the page-2 PNG) is present
    but the PNG write never happened, so the cached PNG is still V1's page 1."""
    client, cache, corpus = env
    v1 = _page(client, ONE, 999)
    import hashlib
    import json
    v2_png = b"\x89PNG-the-page-two-render-that-never-landed"
    cache[_meta_key(ONE, 999)] = json.dumps({
        "v": 1, "png_sha256": hashlib.sha256(v2_png).hexdigest(),
        "rendered_page": 2, "page_count": 2}).encode()
    hit = _page(client, ONE, 999)
    assert hit.content == v1.content
    assert _labels(hit) == (None, None), _labels(hit)


@pytest.mark.parametrize("tamper", [
    "wrong_digest", "garbled", "empty", "partial", "rendered_beyond_count",
    "rendered_not_the_clamp", "string_numbers", "unknown_version",
])
def test_a_sidecar_that_does_not_describe_the_cached_png_gives_no_headers(env, tamper):
    import hashlib
    import json
    client, cache, _ = env
    first = _page(client, ONE, 999)
    assert _labels(first) == ("1", "1")
    good = json.loads(cache[_meta_key(ONE, 999)])
    assert good["png_sha256"] == hashlib.sha256(first.content).hexdigest()   # non-vacuity

    bad = dict(good)
    raw = None
    if tamper == "wrong_digest":
        bad["png_sha256"] = hashlib.sha256(b"another picture").hexdigest()
    elif tamper == "garbled":
        raw = b"{not json"
    elif tamper == "empty":
        raw = b""
    elif tamper == "partial":
        raw = json.dumps(good).encode()[:25]
    elif tamper == "rendered_beyond_count":
        bad.update(rendered_page=2, page_count=1)
    elif tamper == "rendered_not_the_clamp":             # page 999 of 3 pages is page 3, not 2
        bad.update(rendered_page=2, page_count=3)
    elif tamper == "string_numbers":
        bad.update(rendered_page="1", page_count="1")
    elif tamper == "unknown_version":
        bad["v"] = 2
    cache[_meta_key(ONE, 999)] = raw if raw is not None else json.dumps(bad).encode()

    hit = _page(client, ONE, 999)
    assert hit.status_code == 200 and hit.content == first.content
    assert _labels(hit) == (None, None), (tamper, _labels(hit))


def test_a_legacy_cached_png_with_no_sidecar_gives_no_headers(env):
    client, cache, _ = env
    cache[(OWNER, SID, f"{ONE}#p1")] = b"\x89PNG-cached-before-provenance-existed"
    r = _page(client, ONE, 1)
    assert r.status_code == 200 and r.content == b"\x89PNG-cached-before-provenance-existed"
    assert _labels(r) == (None, None)

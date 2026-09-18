"""Exact finding pages: a page that is not in the document is refused, never substituted.

THE DEFECT (reproduced before this file was written). `render.render_page_png` CLAMPS the page
to the document's range — by design, for the generic orientation thumbnail — and the hash-bound
artifact route (`routes/report_facts.get_artifact_page`) passed pages straight through to it. On a
one-page PDF, page 1 and page 999 came back as byte-identical PNGs, so an exact-finding preview of
"page 99" was a picture of the last page, captioned page 99. Measured on the parent's
`/tmp/acp-audit3-bench/scan-base-summary.pdf`: `render_page_png(d, '.pdf', 1) ==
render_page_png(d, '.pdf', 999)` → True.

What this file pins:
  * `render.render_exact_page_png` refuses page < 1 and page > count, naming the count;
  * the generic `render_page_png` KEEPS clamping (the reserved generic route depends on it, and
    its output is never presented as an exact page);
  * the artifact route answers 404 with an explicit detail for an out-of-range page, renders the
    real page otherwise, says which page it rendered (`X-ACP-Rendered-Page`), and never serves a
    render cached under the old clamping key.
"""
from __future__ import annotations

import hashlib
import sys
from pathlib import Path
from urllib.parse import quote

import pytest

ACP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ACP / "api"))

OWNER = "owner@hosp.org"
SID = "s-exact"
FILE = "handbook.pdf"


def _pdf(*sizes: tuple[int, int]) -> bytes:
    """A minimal PDF with one page per (width, height). Different sizes make every page's render
    distinguishable, so "which page came back" is decidable from the bytes alone."""
    n = len(sizes)
    kids = " ".join(f"{3 + i} 0 R" for i in range(n))
    objs = [b"1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n",
            f"2 0 obj<</Type/Pages/Kids[{kids}]/Count {n}>>endobj\n".encode()]
    for i, (w, h) in enumerate(sizes):
        objs.append(f"{3 + i} 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 {w} {h}]>>endobj\n"
                    .encode())
    return b"%PDF-1.4\n" + b"".join(objs) + b"trailer<</Root 1 0 R>>\n%%EOF\n"


ONE_PAGE = _pdf((200, 200))
THREE_PAGES = _pdf((200, 200), (300, 150), (120, 400))
THREE_SHA = hashlib.sha256(THREE_PAGES).hexdigest()


def _png_size(png: bytes) -> tuple[int, int]:
    import io
    from PIL import Image
    return Image.open(io.BytesIO(png)).size


# ── render.py ─────────────────────────────────────────────────────────────────

def test_the_defect_the_generic_renderer_clamps_and_keeps_doing_so():
    """The reproduction, kept as a pin: the GENERIC renderer substitutes the last page. That is
    its documented behaviour for the orientation thumbnail (routes/scans.py, reserved), so it is
    not changed here — it is exactly why an exact-finding preview may not use it unchecked."""
    import render
    p1 = render.render_page_png(ONE_PAGE, ".pdf", 1)
    p999 = render.render_page_png(ONE_PAGE, ".pdf", 999)
    assert p1 is not None and p1 == p999


def test_page_count_reads_the_real_document():
    import render
    assert render.page_count(ONE_PAGE, ".pdf") == 1
    assert render.page_count(THREE_PAGES, ".pdf") == 3
    assert render.page_count(b"not a pdf", ".pdf") is None
    assert render.page_count(b"", ".pdf") is None
    assert render.page_count(THREE_PAGES, ".txt") is None


def test_the_strict_renderer_refuses_a_page_beyond_the_document():
    import render
    got = render.render_exact_page_png(ONE_PAGE, ".pdf", 999)
    assert got["png"] is None
    assert got["reason"] == "out_of_range"
    assert got["pages"] == 1 and got["page"] == 999


@pytest.mark.parametrize("page", [0, -1])
def test_the_strict_renderer_refuses_a_page_below_one(page):
    import render
    got = render.render_exact_page_png(THREE_PAGES, ".pdf", page)
    assert got["png"] is None and got["reason"] == "out_of_range"


def test_the_strict_renderer_renders_the_page_it_was_asked_for():
    import render
    sizes = [_png_size(render.render_exact_page_png(THREE_PAGES, ".pdf", p)["png"])
             for p in (1, 2, 3)]
    # three different aspect ratios → three different pages, in order
    assert sizes[0][0] == sizes[0][1]
    assert sizes[1][0] > sizes[1][1]
    assert sizes[2][0] < sizes[2][1]
    got = render.render_exact_page_png(THREE_PAGES, ".pdf", 3)
    assert got["reason"] is None and got["pages"] == 3 and got["page"] == 3


def test_the_strict_renderer_never_raises_on_bad_bytes():
    import render
    got = render.render_exact_page_png(b"%PDF-garbage", ".pdf", 1)
    assert got["png"] is None and got["reason"] == "unrenderable" and got["pages"] is None
    got = render.render_exact_page_png(THREE_PAGES, ".txt", 1)
    assert got["png"] is None and got["reason"] == "unrenderable"


# ── the hash-bound route ──────────────────────────────────────────────────────

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
    monkeypatch.setattr(core, "email_allowed", lambda e: e == OWNER)
    c = TestClient(app)
    c.headers.update({"Authorization": f"Bearer {OWNER}"})
    isolated_store.save_scan({
        "_scan_id": SID, "started_at": "2026-09-01T05:00:00+00:00",
        "completed_at": "2026-09-01T05:05:00+00:00", "source": "local", "owner": OWNER,
        "rubric": {"name": "wcag-aa", "hash": "h"},
        "summary": {"files": 1, "certifiable": 0, "uncertain": 1, "error": 0, "avg_score": 70},
        "files": [{"file": FILE, "engine": "pdf", "status": "uncertain", "score": 70,
                   "compliant": 0, "skipped_rules": 0, "checksum": THREE_SHA, "issues": []}],
    })
    return c


@pytest.fixture()
def cache(monkeypatch):
    """ACP holds the three-page document; the render cache is an inspectable dict."""
    import blob as _blob
    store = {}
    monkeypatch.setattr(_blob, "download_remediated", lambda *a, **k: THREE_PAGES, raising=False)
    monkeypatch.setattr(_blob, "download_render",
                        lambda owner, sid, key: store.get(key), raising=False)
    monkeypatch.setattr(_blob, "upload_render",
                        lambda owner, sid, key, data: store.__setitem__(key, data), raising=False)
    return store


def _url(page, sha=THREE_SHA):
    return f"/scans/{SID}/files/{quote(FILE, safe='')}/artifact/{sha}/page/{page}"


def test_a_page_beyond_the_document_is_404_and_says_why(client, cache):
    r = client.get(_url(99))
    assert r.status_code == 404
    assert r.json()["detail"] == "page 99 is beyond this document's 3 pages"
    # The page COUNT is cached (so the next out-of-range request is refused without re-reading
    # the bytes); no image is cached for a page that does not exist.
    assert list(cache) == [f"{FILE}#a{THREE_SHA}#exact#pages"]
    assert cache[f"{FILE}#a{THREE_SHA}#exact#pages"] == b"3"


def test_the_cached_page_count_refuses_without_reading_bytes_again(client, cache, monkeypatch):
    import routes.report_facts as rfr
    assert client.get(_url(1)).status_code == 200
    monkeypatch.setattr(rfr, "_candidate_bytes",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("read bytes")))
    r = client.get(_url(7))
    assert r.status_code == 404
    assert r.json()["detail"] == "page 7 is beyond this document's 3 pages"


@pytest.mark.parametrize("page", [0, -2])
def test_a_page_below_one_is_404_before_anything_is_read(client, cache, monkeypatch, page):
    import routes.report_facts as rfr
    monkeypatch.setattr(rfr, "_candidate_bytes",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("read bytes")))
    r = client.get(_url(page))
    assert r.status_code == 404
    assert "pages are numbered from 1" in r.json()["detail"]


def test_an_in_range_page_renders_that_page_and_names_it(client, cache):
    r2 = client.get(_url(2))
    assert r2.status_code == 200, r2.text
    assert r2.headers["X-ACP-Rendered-Page"] == "2"
    assert r2.headers["X-ACP-Page-Count"] == "3"
    assert r2.headers["X-ACP-Artifact-Sha256"] == THREE_SHA
    w, h = _png_size(r2.content)
    assert w > h, "page 2 is the landscape page, not page 1 or the last page"
    r3 = client.get(_url(3))
    assert r3.status_code == 200 and r3.content != r2.content


def test_the_last_page_is_not_the_answer_for_every_page_past_it(client, cache):
    """The exact reproduction, over HTTP: page 3 exists, page 4 does not, and the two must not
    come back as the same picture."""
    assert client.get(_url(3)).status_code == 200
    r = client.get(_url(4))
    assert r.status_code == 404
    assert "beyond this document's 3 pages" in r.json()["detail"]


def test_a_cached_render_says_which_page_it_is(client, cache):
    first = client.get(_url(2))
    again = client.get(_url(2))
    assert again.status_code == 200 and again.content == first.content
    assert again.headers["X-ACP-Rendered-Page"] == "2"


def test_a_render_cached_under_the_old_clamping_key_is_never_served(client, cache):
    """Before this change the route cached `#a<sha>#p99` holding whatever page the clamp produced.
    Those entries outlive the fix in blob storage, so the strict route must not read that key."""
    cache[f"{FILE}#a{THREE_SHA}#p99"] = b"\x89PNG-the-clamped-last-page"
    r = client.get(_url(99))
    assert r.status_code == 404
    assert b"clamped" not in r.content

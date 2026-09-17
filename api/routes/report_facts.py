"""Report FACTS — the server's own answer to "what does the evidence say about this document?"

Three routes, all owner-scoped and all read-only:

* ``GET /scans/{sid}/files/{filename:path}/report-facts`` — the per-document facts
  (api/report_facts.py builds them; that module's docstring is the contract).
* ``GET /scans/{sid}/report-facts?offset=&limit=`` — scan totals plus a BOUNDED per-file index.
  Bounded rather than capped: `filesTotal`, `offset`, `limit` and `complete` are all returned, so
  a caller can page a large estate instead of being handed a silently truncated list. The
  `factsDigest` is computed over the FULL index, so two clients on different pages agree on it.
* ``GET /scans/{sid}/files/{filename:path}/artifact/{sha256}/page/{page}`` — a page image of
  EXACTLY the bytes whose sha256 is in the path.

WHY THE LAST ONE EXISTS. The existing preview route,
``/scans/{scan_id}/files/{filename:path}/page/{page}``, goes through
``routes.scans._source_bytes_for_render``, which prefers the original but FALLS BACK to the
remediated blob. Its output is therefore of unknown provenance: the same URL can return the
original or the corrected copy depending on what happens to be reachable, and nothing in the
response says which. A report that captions that image "after the edit" is asserting something
it cannot know, and it is wrong exactly when a corrected copy is missing — the case where a
reader most needs to be told.

So this route takes the digest as INPUT. It hashes each candidate source and rasterises only the
one that matches; if no bytes ACP holds have that digest it answers 404 rather than rendering
something else. That makes "Original (sha …)" and "Corrected copy (sha …)" captions checkable,
and it is why the report may use those words only for images fetched from here.

No candidate is fetched from a remote provider: the sources are the assessed-source cache, the
local corpus and the remediated blob. A preview must not be able to cause an outbound request.
"""
from __future__ import annotations

import hashlib
import os

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import Response

import core
import report_facts

router = APIRouter()

_SHA256_LEN = 64
_MAX_PAGE = 5000


def _owner(request: Request) -> str:
    """Same owner derivation as routes/scans.py — the gate-verified email, or 'demo'."""
    return getattr(request.state, "user_email", None) or "demo"


@router.get("/scans/{sid}/files/{filename:path}/report-facts")
def get_file_report_facts(sid: str, filename: str, request: Request):
    facts = report_facts.build_file_facts(core.store, sid, filename, owner=_owner(request))
    if facts is None:
        # A scan the caller does not own and a file that is not in the scan answer the same way,
        # like every other per-file route here: a scan id must not be an existence oracle.
        raise HTTPException(404, "scan or file not found")
    return facts


@router.get("/scans/{sid}/report-facts")
def get_scan_report_facts(sid: str, request: Request,
                          offset: int = Query(0, ge=0),
                          limit: int = Query(report_facts.FILE_PAGE_DEFAULT, ge=1,
                                             le=report_facts.FILE_PAGE_MAX)):
    facts = report_facts.build_scan_facts(core.store, sid, owner=_owner(request),
                                          offset=offset, limit=limit)
    if facts is None:
        raise HTTPException(404, "scan not found")
    return facts


def _candidate_bytes(request: Request, sid: str, filename: str, owner: str):
    """Every set of bytes ACP itself holds for this document. Local only, never a remote fetch."""
    def remediated():
        import blob as _blob
        return _blob.download_remediated(owner, sid, filename)

    def cached_source():
        import scanner
        return scanner.read_cached_source(sid, filename, owner)

    def corpus():
        import scanner
        scan = core.store.get_scan(sid, owner=owner) or {}
        if (scan.get("run") or {}).get("source") != "local":
            return None
        from pathlib import Path
        root = Path(os.environ.get("ACP_LOCAL_CORPUS") or (scanner.ACP / "test-corpus/files"))
        root = root.resolve()
        path = (root / filename).resolve()
        if not path.is_relative_to(root):
            return None
        return path.read_bytes() if path.is_file() else None

    for source in (remediated, cached_source, corpus):
        try:
            data = source()
        except Exception:
            data = None
        if data:
            yield data


@router.get("/scans/{sid}/files/{filename:path}/artifact/{sha256}/page/{page}")
def get_artifact_page(sid: str, filename: str, sha256: str, page: int, request: Request):
    """A page image of exactly the bytes named by `sha256`, or 404. Never a substitute."""
    import blob as _blob
    import render as _render

    owner = _owner(request)
    if core.store.get_scan(sid, owner=owner) is None:
        raise HTTPException(404, "scan not found")
    digest = (sha256 or "").strip().lower()
    if len(digest) != _SHA256_LEN or not all(c in "0123456789abcdef" for c in digest):
        raise HTTPException(422, "artifact digest must be a sha256 hex digest")
    if core.store.get_file_records(sid, files=[filename], owner=owner).get(filename) is None:
        raise HTTPException(404, "file not found in this scan")

    ext = os.path.splitext(filename)[1].lower()
    if not _render.can_render(ext):
        raise HTTPException(404, "no preview available for this file type")
    page = max(1, min(int(page or 1), _MAX_PAGE))

    # The cache key carries the digest, so a cached image can never be served for other bytes.
    cache_key = f"{filename}#a{digest}#p{page}"
    cached = _blob.download_render(owner, sid, cache_key)
    if cached is not None:
        return Response(cached, media_type="image/png",
                        headers={"Cache-Control": "private, max-age=86400",
                                 "X-ACP-Artifact-Sha256": digest})

    # Verify BEFORE rasterising: the digest is the question, not a label applied afterwards.
    data = next((d for d in _candidate_bytes(request, sid, filename, owner)
                 if hashlib.sha256(d).hexdigest() == digest), None)
    if data is None:
        raise HTTPException(404, "ACP does not hold bytes with that digest for this document")

    png = _render.render_page_png(data, ext, page)
    if not png:
        raise HTTPException(404, "could not render this page")
    _blob.upload_render(owner, sid, cache_key, png)   # best-effort cache; never raises
    return Response(png, media_type="image/png",
                    headers={"Cache-Control": "private, max-age=86400",
                             "X-ACP-Artifact-Sha256": digest})

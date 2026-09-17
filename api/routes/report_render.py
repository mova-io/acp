"""POST /scans/{sid}/report-render — the accessible server renderer for report models.

The browser builds the report MODEL (frontend/src/reportModel.js and friends); this route turns it
into a tagged PDF (api/report_render.py). It is authenticated by the app's route-table gate like
every registered route (core.is_public), and owner-scoped like /scans/{sid}/report.pdf:

  * a scan the caller does not own answers 404, not 403, so a scan id is not an existence oracle;
  * a `file` that is not part of that scan answers 404;
  * identity (scan, file, checksums, generation time, platform version) comes from the store and
    overwrites the client's — a report must not be able to claim a hash the server never recorded;
  * the body is size-capped BEFORE it is read in full, then validated (413 / 422, never silent
    truncation — see report_render.validate_request);
  * the body MUST carry the `factsDigest` the model was built from, and the server recomputes it
    (api/report_facts.py) — a mismatch is 409 "report data is out of date; regenerate the report".
    Without that the two bullets above combine into the worst outcome available: a model built
    from evidence recorded before the file changed, printed under the checksums it has now, with
    nothing on the page to say the two do not belong together.
"""
from __future__ import annotations

import json
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import Response

import core
import report_render
from routes.scans import _owner

router = APIRouter()


async def _read_capped(request: Request) -> bytes:
    declared = request.headers.get("content-length")
    if declared is not None:
        try:
            if int(declared) > report_render.MAX_BODY_BYTES:
                raise HTTPException(413, "report request is too large")
        except ValueError:
            raise HTTPException(400, "invalid Content-Length") from None
    chunks, size = [], 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > report_render.MAX_BODY_BYTES:
            raise HTTPException(413, "report request is too large")
        chunks.append(chunk)
    return b"".join(chunks)


def current_facts_digest(sid: str, file: str | None, owner) -> str | None:
    """The facts digest this scan/file has RIGHT NOW, or None when there is nothing to show.

    The seam onto api/report_facts.py (stream D), imported lazily so this router stays importable
    on its own, and so a test can replace exactly this function. A report the server cannot bind
    to a version of the facts is one whose evidence may predate the identity printed beside it —
    which is the failure this exists to stop — so an unavailable facts module is an error, never
    a skipped check.

    None means "no facts visible to this owner", which the route answers 404 with, like every
    other not-yours case here: a 409 would confirm the scan exists.
    """
    from report_facts import current_digest
    return current_digest(core.store, sid, file, owner=owner)


def _platform_version() -> str | None:
    try:
        from routes.system import _build_info
        return _build_info().get("version")
    except Exception:  # noqa: BLE001 — a missing build stamp must not block a report
        return None


@router.post("/scans/{sid}/report-render")
async def report_render_pdf(sid: str, request: Request):
    owner = _owner(request)
    raw = await _read_capped(request)
    scan = core.store.get_scan(sid, owner=owner)
    if scan is None:
        raise HTTPException(404, "scan not found")
    try:
        body = json.loads(raw.decode("utf-8") or "null")
    except (UnicodeDecodeError, ValueError):
        raise HTTPException(422, "request body must be JSON") from None
    try:
        request_ = report_render.validate_request(body)
    except report_render.ReportInputError as exc:
        raise HTTPException(exc.status, exc.detail) from None

    file = request_["file"]
    record = None
    if file is not None:
        if not any(f.get("file") == file for f in scan.get("files") or []):
            raise HTTPException(404, "file not found in this scan")
        record = core.store.get_file_record(sid, file) or {}
    # Bind the model to the facts it was built from BEFORE stamping server identity on it. A
    # 'scan' or 'remediation' report is bound to the scan-level digest; a 'file' report to its
    # file's. Anything else — a mismatch, or a facts module that cannot answer — refuses.
    try:
        current = current_facts_digest(sid, file if request_["kind"] == "file" else None, owner)
    except ImportError as exc:  # pragma: no cover — the facts module is part of the app
        raise HTTPException(503, "report facts are unavailable, so this report cannot be shown "
                                 "to describe the current document") from exc
    if not current:
        raise HTTPException(404, "scan not found")
    if current != request_["factsDigest"]:
        raise HTTPException(409, report_render.STALE_FACTS_DETAIL)
    model = request_["model"]
    identity = report_render.server_identity(
        scan_id=sid, kind=request_["kind"], file=file, record=record,
        client_identity=model.get("identity"), platform_version=_platform_version())
    base_url = getattr(core, "PUBLIC_URL", "") or None
    pdf = await run_in_threadpool(report_render.render_pdf, model, identity,
                                  request_["mode"], base_url)
    name = report_render.download_name(request_["kind"], request_["mode"], sid, file)
    return Response(pdf, media_type="application/pdf", headers={
        "Content-Disposition": f"attachment; filename=\"{name}\"; filename*=UTF-8''{quote(name)}",
        "Cache-Control": "no-store",
        "X-Content-Type-Options": "nosniff",
    })

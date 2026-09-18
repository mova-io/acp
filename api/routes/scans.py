"""Scan lifecycle, results, traces, manifest, report, inventory, and per-file
remediation endpoints."""
from __future__ import annotations
import hashlib
import json as _json
import logging
import mimetypes
import os
import re
import tempfile
import threading
import uuid
import zipfile
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Query, Request, Body
from fastapi.responses import RedirectResponse, Response, StreamingResponse
from pydantic import BaseModel, StrictBool

import core
import scanner
from store import ActiveStageExecutionError
from scanner import run_scan
from report import build_report
from report_tagged import build_tagged_report
# Safe to import without weasyprint installed: report_weasy imports it inside the render
# function, not at module scope, so a missing native stack fails one request rather than
# preventing the API from starting.
from report_weasy import build_weasy_report
from swallowed import swallowed

router = APIRouter()

_TERMINAL_SCAN_STATUSES = frozenset(
    {"completed", "failed", "cancelled", "interrupted", "superseded", "discovered"}
)
_FRESHNESS_LIVE_THRESHOLD_S = 30


def _scan_freshness(scan_id: str, run: dict) -> str:
    """Classify the data currency of a scan's progress snapshot.

    terminal — scan reached a final state; no worker is running.
    live     — Redis job state updated within the last 30 s.
    checkpoint — no live Redis signal but a durable Postgres snapshot exists.
    stale    — scan is running but no live or checkpoint signal is available.
    """
    from datetime import datetime, timezone
    if (run.get("status") or "") in _TERMINAL_SCAN_STATUSES:
        return "terminal"
    job_id = core.get_job_id_for_scan(scan_id)
    if job_id:
        state = core.get_job_state(job_id)
        if state and not state.get("done"):
            updated_at = state.get("updated_at")
            if updated_at:
                try:
                    ts = datetime.fromisoformat(updated_at.replace("Z", "+00:00"))
                    age = (datetime.now(timezone.utc) - ts).total_seconds()
                    if age < _FRESHNESS_LIVE_THRESHOLD_S:
                        return "live"
                except Exception:
                    swallowed("routes.scans._scan_freshness: computing the scan's freshness "
                              "failed", scan_id)
    if run.get("live_checkpoint_at"):
        return "checkpoint"
    return "stale"


def _owner(request: Request) -> str:
    """The current user for per-user data isolation — the gate-verified email, or
    'demo' for the keyless/demo path. Matches the owner stamped on scans at creation."""
    return getattr(request.state, "user_email", None) or "demo"


def _register_scan_tokens(scan_id: str, *, drive: str | None = None,
                          sp: str | None = None) -> None:
    """Require cross-replica credential admission before accepting durable work."""
    if not (drive or sp):
        return
    try:
        tokens = {"require_shared": True}
        if drive:
            tokens["drive"] = drive
        if sp:
            tokens["sp"] = sp
        core.register_scan_tokens(scan_id, **tokens)
    except core.SharedTokenStoreUnavailable as exc:
        raise HTTPException(status_code=503, detail={
            "code": "credential_store_unavailable",
            "message": ("The shared credential store is temporarily unavailable. "
                        "No processing was started; retry in a moment."),
        }) from exc


logger = logging.getLogger(__name__)


def _log_release_provider_error(exc: Exception, *, scan_id: str, file: str,
                                release_id: str, provider: str, uncertain: bool) -> dict:
    """Log allowlisted metadata, never exception text or provider response bodies.

    Google HTTP errors expose ``resp``; requests/Graph errors commonly expose
    ``response``. Only numeric HTTP codes and named correlation headers are read.
    """
    diagnostic = {"error_type": type(exc).__name__[:80]}
    for response in (getattr(exc, "resp", None), getattr(exc, "response", None), exc):
        if response is None:
            continue
        status = getattr(response, "status_code", None)
        if status is None:
            status = getattr(response, "status", None)
        if isinstance(status, (str, int)) and not isinstance(status, bool):
            status_text = str(status)
            if re.fullmatch(r"[1-5][0-9]{2}", status_text):
                diagnostic.setdefault("http_status", int(status_text))
        headers = getattr(response, "headers", None)
        if headers is None and hasattr(response, "get"):
            headers = response
        if headers is not None and hasattr(headers, "get"):
            for name in ("request-id", "x-request-id", "x-goog-request-id",
                         "x-guploader-uploadid"):
                value = headers.get(name) or headers.get(name.title())
                if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", value):
                    diagnostic.setdefault("provider_request_id", value)
    from telemetry import document_id
    document_key = document_id(scan_id, file)
    # Follow telemetry's HMAC convention; omit document identity without its salt.
    logger.warning("release_provider_error %s", _json.dumps({
        "scan_id": str(scan_id)[:128],
        **({"document_id": document_key} if document_key else {}),
        "release_id": str(release_id)[:128], "provider": provider,
        "outcome_uncertain": uncertain, **diagnostic,
    }, ensure_ascii=True, sort_keys=True))
    return diagnostic


def _enqueue_stage_batch(*args, **kwargs) -> dict:
    """Enqueue one stage execution, presenting the store's single-flight fence as API state."""
    try:
        return core.store.enqueue_stage_batch(*args, **kwargs)
    except ActiveStageExecutionError as exc:
        raise HTTPException(status_code=409, detail={
            "code": "stage_execution_active",
            "stage": exc.stage,
            "active_batch_id": exc.batch_id,
            "message": (f"A different {exc.stage} run is already active. Wait for it to finish "
                        "or stop it before starting revised work."),
        }) from exc


def _sealed_stage_input(sid: str, upstream_stage: str,
                        fallback_snapshot_id: str) -> tuple[str, str | None]:
    """Bind downstream work to the current sealed upstream output when available."""
    reader = getattr(core.store, "current_stage_output_manifest", None)
    manifest = reader(sid, upstream_stage) if callable(reader) else None
    manifest_id = (manifest or {}).get("manifest_id")
    return (manifest_id, manifest_id) if manifest_id else (fallback_snapshot_id, None)


def _inv_capability(row: dict) -> dict:
    """Add the estate capability {format, status} to a scan_inventory row, derived from its mime/name
    the same way estate_inventory.summarize classifies the whole estate — so the per-file list/export
    carries the identical assessable/metadata-only/unsupported label the dashboard shows."""
    import estate_inventory as ei
    c = ei.classify({"id": row.get("drive_file_id"), "name": row.get("file"),
                     "mimeType": row.get("mime")})
    return {**row, "format": c["format"], "status": c["status"]}


def _supersede_replaced_run(prior: dict | None, new_scan_id: str, owner: str) -> None:
    """Stop the run a newly accepted scan replaces — called only AFTER enqueue_scan has
    committed (PRD H-03).

    Two guards, both of which the pre-2026-08-30 inline version lacked:

    * **Never stop the scan we are about to return.** enqueue_scan honours Idempotency-Key by
      returning the ORIGINAL (scan_id, job_id) instead of inserting. On such a replay the scan
      being returned can be the very run active_scan() reported, and superseding it would kill
      the job the caller is being handed — a retry destroying the submission it was retrying.
    * **Never fail the request for this.** The new scan is already durable; raising here would
      report a failed submission that in fact succeeded, and the UI would offer a retry that
      enqueues a second scan. A prior run left running is self-correcting — the next submission
      supersedes it, and acquire_discovery_guard's stale reclaim covers the abandoned case — so
      the honest outcome is to log and return the identifiers the caller earned.
    """
    if not prior or not prior.get("id") or prior["id"] == new_scan_id:
        return
    try:
        core.store.supersede_scan(prior["id"], owner=owner)
    except Exception as exc:  # noqa: BLE001 — see the docstring: never fail an accepted scan
        logger.warning("scan %s accepted, but superseding prior run %s failed: %s",
                       new_scan_id, prior["id"], exc)


def _cancel_replaced_stage(prior: dict | None, new_scan_id: str, owner: str) -> None:
    """Stop an accepted downstream stage without hiding its partial results from history.

    Discovery replacement is an abandoned inventory attempt and uses ``superseded``. Assess and
    later stages may already contain useful durable evidence, so the user's explicit replacement
    is a cancellation: jobs stop, partial results remain reachable, and the newly accepted
    Discovery still starts even if this best-effort cleanup fails.
    """
    if not prior or not prior.get("id") or prior["id"] == new_scan_id:
        return
    try:
        core.store.cancel_scan(prior["id"], owner=owner)
    except Exception as exc:  # noqa: BLE001 — the replacement is already durable
        logger.warning("scan %s accepted, but cancelling prior stage on %s failed: %s",
                       new_scan_id, prior["id"], exc)


def sharepoint_site_overflow(folder: str | None, folders: list[str] | None) -> str | None:
    """The message to refuse a SharePoint request with when it names more sites than one scan
    may span, or None when it is within the cap.

    REFUSED AT THE EDGE rather than silently trimmed. scanner._sp_list caps the walk too, and
    that cap stays — a job queued before this check existed still has to be bounded somewhere —
    but a cap that only truncates hands the operator a floor and an explanation after the fact,
    while refusing here gives them the choice before the scan spends an hour against a customer's
    tenant. Both read the same number from the same helper, so raising ACP_SP_MAX_SITES moves
    them together and they cannot drift.

    A site is a root with no "/" (scanner._sp_locations makes the same split); "root" is Drive's
    no-narrowing sentinel and is not a site. Counted as a SET, because selecting one site twice
    is one site — the listing collapses the duplicate, so refusing it here would reject a request
    the scanner would have handled correctly.
    """
    roots = [f for f in (list(folders) if folders else ([folder] if folder else []))
             if f and f != "root" and "/" not in f]
    n, cap = len(set(roots)), scanner._sp_max_sites()
    if n <= cap:
        return None
    return (f"{n} SharePoint sites selected; one scan covers at most {cap}. Run them as "
            f"separate scans, or raise ACP_SP_MAX_SITES on the deployment.")


@router.post("/scans")
def start_scan(request: Request, source: str = Query(..., pattern="^(local|drive|sharepoint)$"),
               sync: bool = False, folder: str | None = Query(None),
               # Repeatable: ?folders=<id>&folders=<id>. `folder` stays the single-root form, so
               # every existing caller, saved link and already-queued job is unaffected. A
               # SharePoint/OneDrive folder is written `<driveId>/<itemId>` — the pair, because a
               # Graph item id is unique only within its drive.
               folders: list[str] | None = Query(None),
               # Subtrees to carve OUT of the chosen folders — a selected parent with an
               # excluded child (PRD 6.3). Ignored without `folders`, since there is nothing
               # to carve out of and applying them to a whole-estate scan would narrow it
               # invisibly.
               exclude_folders: list[str] | None = Query(None),
               # True preserves every existing client and saved scope. The discovery wizard sends
               # false explicitly when the operator chooses "This folder only".
               include_subfolders: bool = Query(True),
               ai: bool = Query(True), queue: bool = Query(False),
               # PII (deep) scan is opt-in: it doubles scan time by extracting + regex-scanning
               # every file's text, so a scan is fast unless the caller explicitly asks for it.
               # The UI mirrors this (deepScan defaults off). Scheduled sweeps / API callers that
               # want sensitive-data detection must pass pii=true.
               pii: bool = Query(False), fanout: bool = Query(False),
               batch: bool = Query(False), exclude_remediated: bool = Query(False),
               incremental: bool = Query(True),
               replace_active: bool = Query(False),
               prefer_recent: bool = Query(False)):
    token = request.headers.get("x-drive-token")      # per-user Drive token (GIS)
    sp_token = request.headers.get("x-sp-token")      # per-user MS Graph token (MSAL)
    # ACP_DEMO_DRIVE_KEY lets the E2E test and demo scripts trigger a server-side
    # ADC Drive scan without needing per-user GIS — pass as X-Demo-Key header.
    DEMO_KEY = os.environ.get("ACP_DEMO_DRIVE_KEY", "")
    demo_key = request.headers.get("x-demo-key", "")
    # The demo/ADC scan bypass is FAIL-CLOSED (core.TEST_BYPASS_ENABLED): off unless an
    # operator explicitly opts in, and refused in production regardless. It used to key off
    # `not core.IS_PROD`, which was True in production because nothing ever set the env var.
    is_demo_drive = core.TEST_BYPASS_ENABLED and source == "drive" and DEMO_KEY and demo_key == DEMO_KEY
    if source == "drive" and core.GOOGLE_CLIENT_ID and not token and not is_demo_drive:
        raise HTTPException(401, "sign in with Google to scan your Drive")
    if source == "sharepoint" and not sp_token:
        raise HTTPException(401, "sign in with Microsoft to scan OneDrive")
    if source == "sharepoint":
        over = sharepoint_site_overflow(folder, folders)
        if over:
            raise HTTPException(400, over)
    # Admin deterministic-only mode is a HARD override: if AI is disabled platform-wide,
    # no scan runs AI regardless of the per-scan ?ai= request.
    effective_ai = ai and core.store.get_ai_enabled()
    # Who ran this scan (GIS email, set by the access-gate). Used to group traces
    # by user in Langfuse; falls back to the demo identity on the keyless path.
    # Owner for per-user isolation: the gate-verified email, else 'demo' (keyless/demo).
    user = getattr(request.state, "user_email", None) or "demo"

    # ── Worker-free Discover (ACP_INLINE_DISCOVER) ────────────────────────────
    # Runs discovery in this API process instead of handing it to the worker tier — the
    # pre-worker arrangement, kept because for DISCOVER specifically the durable queue buys
    # little and costs a hard dependency: a deployment with no live worker cannot start a scan
    # at all (routes/discovery.py's preflight blocks on worker_tier_never_started), and every
    # queue fault between "user clicked" and "listing began" lands on the stage that is only
    # reading metadata anyway.
    #
    # Deliberately scoped to DISCOVER. Assess and Remediate still fan out to workers, which is
    # where the durable queue earns its keep: those stages download bytes, take minutes per file,
    # and — via the ADR 0044 blob path — can be retried by a worker that holds no user token.
    #
    # Gated on defer mode as well as the flag. With ACP_DEFER_ANALYSIS_TO_ASSESS=0 a "discover"
    # also downloads and analyses every file, and running THAT in the API process is exactly the
    # long, blocking, restart-losable work the queue exists for. The flag must not silently
    # widen from "list metadata here" to "analyse the whole estate here".
    #
    # WHAT IS GIVEN UP, stated plainly: the run no longer survives an API restart, and there is
    # no automatic retry. Single-flight is NOT given up — `_scan_discover` claims the durable
    # per-(owner, source) discovery guard itself (store.acquire_discovery_guard), so a double
    # click still gets a conflict rather than two concurrent listings.
    inline_discover = False
    if queue:
        from .discovery import inline_discover_enabled
        from handlers import _defer_analysis_to_assess as _defer_check
        if inline_discover_enabled() and _defer_check():
            inline_discover = True
            queue = False

    # ── Durable async path: enqueue a scan job for the worker pool (ADR 0004). ──
    # Survives restarts, retries on transient failure, shows up in /jobs + Grafana.
    if queue:
        # Single-flight per owner: nothing stopped a second "Re-scan all sources" click from
        # starting a new durable scan while an old one for the same user was still running.
        # Both then discover concurrently — wasted Drive API calls, wasted DB connections, and
        # confusing overlapping results (observed live 2026-08-26: a tiny folder-scoped listing
        # and a 15k-item whole-Drive listing logging almost simultaneously for one account).
        # "Re-scan" means "start fresh, superseding whatever's running" — there is no UI for
        # intentionally running two scans in parallel — so supersede the old one first. Scoped
        # like active_scan()/reconnect: one user has one meaningful "current scan" regardless of
        # source. Deliberately supersede_scan, NOT cancel_scan (the Stop button's path): cancel_scan
        # stamps completed_at=now(), which made the auto-killed run sort as the estate's NEWEST
        # scan — with files=0 since it barely started — hiding the real completed scan behind it
        # and tripping the production collapse monitor within minutes (found live 2026-08-26).
        # supersede_scan does the same job-kill but under a status list_scans() excludes.
        #
        # ORDERING (PRD reliability-hardening H-03): this READS the prior run here but does not
        # stop it until enqueue_scan below has committed. It used to supersede immediately, 46
        # lines before the durable write, with three more DB round trips in between
        # (list_ai_provider_configs, list_disposition_policies, get_ai_enabled). Any of those
        # raising — PoolError is the observed one, 2026-08-30, when the API replica's pool was
        # exhausted — left the user's running scan KILLED and no replacement created. The request
        # 500'd, so the UI reported failure, and the run it had silently destroyed was gone.
        # Losing work on the failure path is strictly worse than the concurrency the guard exists
        # to prevent, so acceptance now comes first and the stop follows it.
        idempotency_key = request.headers.get("idempotency-key") or None
        # Starting revised Discovery work must never silently destroy an accepted run. Include
        # queued work as well as a worker-claimed scan: the former is exactly where a second click
        # can otherwise create two jobs before active_scan() has a running row to report.
        # One active stage per owner workflow. Idempotency below prevents a duplicate submit for
        # the SAME Discovery intent; this fence prevents a DIFFERENT stage transition from being
        # admitted beside it. Previously this filtered to Discovery only, so a user could start a
        # new Discovery while Assess was still consuming the prior immutable snapshot. Both cards
        # then truthfully showed running work, but together described no coherent workflow.
        _active_workflow = next(iter(core.store.active_workflows(user)), None)
        _active_stage = (_active_workflow or {}).get("stage") or "discover"
        _prior_active = core.store.active_scan(owner=user)
        prior_scan_id = ((_active_workflow or {}).get("scan_id")
                         or (_prior_active or {}).get("id"))
        prior_run = ((core.store.get_scan(prior_scan_id, owner=user) or {}).get("run")
                     if prior_scan_id else {}) or {}
        prior_workflow = (core.store.workflow_for_scan(prior_scan_id, user)
                          if prior_scan_id else None)
        replaying_same_intent = bool(
            idempotency_key and prior_run.get("idempotency_key") == idempotency_key)
        if prior_scan_id and not replaying_same_intent and not replace_active:
            raise HTTPException(status_code=409, detail={
                "code": ("discovery_workflow_active" if _active_stage == "discover"
                         else "workflow_stage_active"),
                "active_scan_id": prior_scan_id,
                "active_stage": _active_stage,
                "workflow_id": (prior_workflow or {}).get("id", prior_scan_id),
                "workflow_revision": int((prior_workflow or {}).get("revision") or 1),
                "message": (f"{_active_stage.title()} is already active. Continue that workflow, "
                            "or explicitly stop and replace it before starting a new Discovery."),
            })
        scan_id = uuid.uuid4().hex[:12]
        # fanout=true → decompose into per-file jobs (ADR 0007); else the monolithic
        # 'scan' job (default, proven). Both are durable and resume across replicas.
        jtype = "scan_discover" if fanout else "scan"
        # Atomic enqueue: scan_runs stub + jobs row + immutable input snapshot committed
        # together in one transaction. GET /scans/{id} is immediately resolvable after this
        # call; a failure rolls back all rows — no orphan stubs. Same Idempotency-Key from
        # the same owner returns the original scan without creating a duplicate.
        #
        # Input snapshot: captures everything that governs how the scan will execute, so
        # a rule or config change after enqueue cannot silently alter an in-flight scan.
        # SECURITY: no tokens or credentials — the job payload carries drive_token/sp_token
        # for the worker; the snapshot stores only a connection reference.
        _provider_cfg = [
            {k: v for k, v in p.items() if k != "key_secret_ref"}
            for p in core.store.list_ai_provider_configs()
            if p.get("enabled")
        ]
        _lifecycle = [
            r for r in core.store.list_disposition_policies(owner=user)
            if r.get("enabled")
        ]
        _defer_flag = os.environ.get("ACP_DEFER_ANALYSIS_TO_ASSESS", "1").strip().lower() in (
            "1", "true", "yes", "on")
        _connection_ref = f"{source}:{user}" if source != "local" else "local"
        from second_opinion_policy import load_policy as _load_second_opinion_policy
        _scan_inputs = {
            "source": source,
            "folder_ids": list(folders or ([folder] if folder else [])),
            "exclude_folder_ids": list(exclude_folders or []),
            "scan_options": {
                "ai": ai, "pii": pii, "batch": batch,
                "exclude_remediated": exclude_remediated,
                "incremental": incremental, "fanout": fanout,
                "include_subfolders": include_subfolders,
            },
            "actor": user,
            "connection_ref": _connection_ref,
            "feature_flags": {
                "ai_platform_enabled": core.store.get_ai_enabled(),
                "defer_analysis_to_assess": _defer_flag,
                # Immutable consent boundary: workers read this snapshot, never the mutable
                # current setting, so a run cannot change eligibility halfway through.
                "second_opinion_policy": _load_second_opinion_policy(core.store),
            },
            "provider_config": _provider_cfg,
            "lifecycle_rules": _lifecycle,
            "app_version": os.environ.get("ACP_APP_VERSION") or None,
        }
        # The UI asks for continuity guidance before repeating the exact same frozen work.  This
        # comes after the snapshot is assembled so "compatible" means every execution-governing
        # input agrees, not merely the same source label. API clients keep their existing behavior
        # unless they explicitly opt into prefer_recent.
        if prefer_recent and not prior_scan_id:
            recent = core.store.recent_compatible_workflow(user, source, _scan_inputs)
            if recent and not replace_active:
                raise HTTPException(status_code=409, detail={
                    "code": "recent_compatible_workflow",
                    "active_scan_id": recent["scan_id"],
                    "active_stage": recent.get("current_stage") or "discover",
                    "workflow_id": recent["workflow_id"],
                    "workflow_revision": int(recent.get("revision") or 1),
                    "message": ("The same source, scope, settings, and lifecycle policy were "
                                "used recently. Continue that workflow or start a new revision."),
                })
            if recent:
                prior_scan_id = recent["scan_id"]
                prior_workflow = core.store.workflow_for_scan(prior_scan_id, user)
        # Workers fan out across replicas and deliberately do not persist delegated credentials
        # in Postgres. Prove the shared ephemeral credential exists before creating hundreds of
        # jobs; accepting first turned one Redis outage into thousands of auth dead-letters.
        _register_scan_tokens(scan_id, drive=token, sp=sp_token)
        scan_id, job_id = core.store.enqueue_scan(
            scan_id, source, user, jtype,
            {"source": source, "scan_id": scan_id, "folder": folder, "folders": folders,
             "exclude_folders": exclude_folders, "include_subfolders": include_subfolders, "ai": ai,
             "user": user, "pii": pii, "batch": batch,
             "exclude_remediated": exclude_remediated, "incremental": incremental,
             # Carry tokens in the payload so the worker container can authenticate
             # without sharing the API's in-memory token store (split topology, no Redis).
             "drive_token": token, "sp_token": sp_token},
            idempotency_key=idempotency_key,
            inputs=_scan_inputs,
            workflow_id=(prior_workflow or {}).get("id") if replace_active else None,
            workflow_revision=((prior_workflow or {}).get("current_revision", 0) + 1
                               if replace_active and prior_workflow else 1),
            supersedes_scan_id=prior_scan_id if replace_active else None)
        # Acceptance is durable from here on: scan_runs + jobs + scan_inputs are committed and
        # GET /scans/{scan_id} resolves. Only NOW is it safe to stop the run this one replaces.
        if replace_active:
            # active_workflows sees queued work and every downstream stage, while active_scan is
            # status-based. Use the durable run row as the fallback so replacing an Assess job
            # actually cancels that job and supersedes its run instead of only starting a sibling.
            if _active_stage == "discover":
                _supersede_replaced_run(_prior_active or prior_run, scan_id, user)
            else:
                _cancel_replaced_stage(_prior_active or prior_run, scan_id, user)
        # ADR 0042 — the run's first event, and the only one emitted from a request thread rather
        # than from the worker. After enqueue_scan, because that is the durable write that makes
        # the scan real: before it there is no job row and no scan_id worth anchoring to. This is
        # the ONLY path that produces a genuine "queued" state — the in-process thread path below
        # starts work immediately, so claiming it was queued there would be fiction.
        from handlers import scan_event
        scan_event(scan_id, "scan.queued", phase="queued", job_id=job_id, owner_email=user,
                   detail={"source": source, "job_type": jtype, "batch": batch,
                           "fanout": fanout})
        accepted_workflow = core.store.workflow_for_scan(scan_id, user) or {}
        return {"scan_id": scan_id, "job_id": job_id, "queued": True,
                "workflow_id": accepted_workflow.get("id", scan_id),
                "workflow_revision": accepted_workflow.get("revision", 1),
                "supersedes_scan_id": accepted_workflow.get("supersedes_scan_id"),
                "fanout": fanout, "batch": batch, "workers": core.WORKERS,
                # Split topology (#113): the API runs ACP_WORKERS=0 and a standalone worker
                # container carries the pool — report its heartbeat so the client's
                # "no workers" guard doesn't refuse a perfectly manned queue.
                "worker_tier_alive": core.store.worker_tier_alive()}

    if sync:  # synchronous path for scripts/tests
        from handlers import _defer_analysis_to_assess
        if _defer_analysis_to_assess():
            # Metadata-only discovery is the DEFAULT (ADR 0020): list + classify from metadata +
            # persist inventory and STOP — nothing is downloaded or opened. The download + WCAG
            # analysis run when Assess is called on this scan. Delegates to the same _scan_discover
            # the fan-out path uses, so the 'discovered' state (inventory + assess_params) is
            # identical. Set ACP_DEFER_ANALYSIS_TO_ASSESS=0 to force a full download+analyse scan.
            from handlers import _scan_discover
            scan_id = uuid.uuid4().hex[:12]
            _register_scan_tokens(scan_id, drive=token, sp=sp_token)
            # `folders`/`exclude_folders` MUST come along. Deferred discovery is the default
            # since #436, so this is the primary path — and a payload that carries only `folder`
            # drops a chosen scope silently: the card says "Scans: HR" and the scan covers the
            # whole Drive. Widening is the one direction nobody re-checks.
            _scan_discover({"source": source, "scan_id": scan_id, "folder": folder,
                            "folders": folders, "exclude_folders": exclude_folders,
                            "include_subfolders": include_subfolders, "ai": ai,
                            "user": user, "pii": pii, "batch": batch,
                            "exclude_remediated": exclude_remediated, "incremental": incremental},
                           {"scan_id": scan_id})
            _sync_run = (core.store.get_scan(scan_id) or {}).get("run") or {}
            if _sync_run.get("status") == "failed":
                raise HTTPException(
                    status_code=409,
                    detail=_sync_run.get("error") or "Discovery failed — conflict or error",
                )
            return {"scan_id": scan_id, "source": source, "discovered": True}
        inv: list = []
        report = run_scan(source, drive_token=token, folder=folder, sp_token=sp_token,
                          **({"folders": folders} if folders else {}),
                          **({"exclude_folders": exclude_folders} if exclude_folders else {}),
                          include_subfolders=include_subfolders,
                          ai_enabled=effective_ai, user=user, detect_pii=pii,
                          exclude_remediated=exclude_remediated, inventory_out=inv)
        sid = core.store.save_scan(report)
        # Persist per-file inventory + evaluate archival/deletion rules — same as the fanout path.
        from handlers import persist_discovery_inventory
        persist_discovery_inventory(sid, inv, source, user)
        core.finalize_scan(sid, effective_ai, source)
        return {"scan_id": sid, "source": source, "summary": report["summary"]}

    # Default: in-process background thread (fast, but lost on restart).
    job_id = uuid.uuid4().hex[:12]
    # Minted HERE rather than inside work() so the response can name the scan it started. The
    # durable path returns a scan_id and the SPA's queued flow reads it (api.js startScanQueued);
    # the worker-free path above lands in this branch and has to keep that contract. None when
    # the legacy full-scan branch will run, since only save_scan can mint an id for that one.
    from handlers import _defer_analysis_to_assess as _defer_now
    pre_scan_id = uuid.uuid4().hex[:12] if _defer_now() else None
    if pre_scan_id:
        # Before the thread starts, not inside it: a caller handed a scan_id in the response may
        # act on it immediately, and the tokens have to be resolvable by then.
        _register_scan_tokens(pre_scan_id, drive=token, sp=sp_token)
    # Written through core.set_job/update_job so the poll can be served by ANY replica. Writing
    # straight into the dict is what made ingress session affinity load-bearing, and affinity is
    # what blocks multi-revision mode and therefore blue-green.
    core.set_job(job_id, {"phase": "queued", "files_found": 0, "files_done": 0, "current": None,
                          "done": False, "scan_id": None, "error": None, "source": source,
                          "ai": effective_ai, "owner_email": user})

    # Liveness heartbeat, separate from progress. The deferred-discovery path below makes exactly
    # one core.update_job call (at the very end) — during the crawl itself, a large estate can go
    # a long time with no progress write, which is indistinguishable from a dead replica unless
    # something else proves the replica is still up. This companion thread does only that: touch
    # updated_at every core._JOB_HEARTBEAT_SECONDS regardless of what work() has found so far, so
    # core.get_job_state's staleness check tracks "is the replica alive" rather than "how big is
    # this estate" — and stops the moment work() ends, one way or another, via the Event below.
    _stop_heartbeat = threading.Event()

    def _heartbeat():
        while not _stop_heartbeat.wait(core._JOB_HEARTBEAT_SECONDS):
            core.update_job(job_id, {})

    def work():
        # Bound before the try so the failure path below can tell "we crashed before a scan_id
        # existed" from "we crashed after" without inspecting locals(). See its comment.
        sid = None
        try:
            from handlers import _defer_analysis_to_assess
            if _defer_analysis_to_assess():
                # Metadata-only discovery is the DEFAULT (ADR 0020): list + classify from metadata
                # + persist inventory and STOP — nothing downloaded. The download + WCAG analysis
                # run when Assess is called. Delegates to the fan-out _scan_discover so the
                # 'discovered' state is identical; tokens stay registered for the later Assess.
                # ACP_DEFER_ANALYSIS_TO_ASSESS=0 forces the legacy full download+analyse scan.
                from handlers import _scan_discover
                # Pre-minted above (with its tokens already registered) so the response could
                # name it; the fallback keeps this branch correct if it is ever reached without
                # one — the id has to exist before _scan_discover is called either way.
                sid = pre_scan_id or uuid.uuid4().hex[:12]
                if sid != pre_scan_id:
                    _register_scan_tokens(sid, drive=token, sp=sp_token)
                # Same as the sync branch above: the chosen scope has to travel with the
                # payload or the default scan silently widens to the whole source.
                _scan_discover({"source": source, "scan_id": sid, "folder": folder,
                                "folders": folders, "exclude_folders": exclude_folders,
                                "include_subfolders": include_subfolders, "ai": ai,
                                "user": user, "pii": pii, "batch": batch,
                                "exclude_remediated": exclude_remediated,
                                "incremental": incremental}, {"scan_id": sid, "id": job_id})
                # _scan_discover returns (without raising) on conflict, after writing phase=error
                # to the job. Only overwrite with phase=discovered when it actually succeeded —
                # otherwise this unconditional write masks the conflict and makes 0 files look
                # like a successful empty-corpus scan to the frontend.
                if (core.get_job_state(job_id) or {}).get("phase") != "error":
                    core.update_job(job_id, {"phase": "discovered", "done": True, "scan_id": sid})
                return
            inv: list = []
            report = run_scan(source, progress=lambda d: core.update_job(job_id, d),
                              drive_token=token, folder=folder, sp_token=sp_token,
                              **({"folders": folders} if folders else {}),
                              **({"exclude_folders": exclude_folders} if exclude_folders else {}),
                              include_subfolders=include_subfolders,
                              ai_enabled=effective_ai, user=user, detect_pii=pii,
                              exclude_remediated=exclude_remediated, inventory_out=inv)
            sid = core.store.save_scan(report)
            # Persist per-file inventory + evaluate archival/deletion rules — same as the fanout path,
            # so a default in-process Discover marks Archive/Delete candidates too (not only fanout).
            # Emit the dedicated "lifecycle" phase so the frontend shows the lifecycle step as active,
            # then stream per-10-file ticks with cumulative counters so the user sees live progress.
            from handlers import persist_discovery_inventory
            lifecycle_total = len(inv)
            core.update_job(job_id, {"phase": "lifecycle",
                                     "files_found": lifecycle_total,
                                     "files_evaluated": 0,
                                     "rules_enabled": 0})
            def _lc_progress(stats):
                core.update_job(job_id, {"phase": "lifecycle",
                                         "files_found": lifecycle_total,
                                         **stats})
            save_outcome = persist_discovery_inventory(sid, inv, source, user,
                                                       progress_cb=_lc_progress)
            core.finalize_scan(sid, effective_ai, source)
            done = core.get_job_state(job_id) or {}
            core.update_job(job_id, {"schema_version": 2, "phase": "done", "done": True,
                                     "scan_id": sid, "files_done": done.get("files_found", 0),
                                     "save_new": save_outcome.get("new"),
                                     "save_updated": save_outcome.get("updated"),
                                     "save_unchanged": save_outcome.get("unchanged"),
                                     "save_failed": save_outcome.get("failed"),
                                     "rules_enabled": save_outcome.get("rules_enabled"),
                                     "files_evaluated": save_outcome.get("files_evaluated"),
                                     "lifecycle_matches": save_outcome.get("lifecycle_matches"),
                                     "lifecycle_archive": save_outcome.get("lifecycle_archive"),
                                     "lifecycle_delete": save_outcome.get("lifecycle_delete"),
                                     "lifecycle_tagged": save_outcome.get("lifecycle_tagged"),
                                     "assessable": save_outcome.get("assessable"),
                                     "metadata_only": save_outcome.get("metadata_only"),
                                     "unsupported": save_outcome.get("unsupported"),
                                     "eligibility_unknown": save_outcome.get("eligibility_unknown"),
                                     "excluded": save_outcome.get("excluded")})
            # ADR 0042. persist_discovery_inventory (above) already emitted this run's
            # inventory_saved / lifecycle_applied / discovered; this is the extra fact only the
            # legacy full-download branch has — the analysis finished and the run finalized.
            from handlers import scan_event
            scan_event(sid, "scan.completed", phase="done", job_id=job_id, owner_email=user,
                       detail={"files": done.get("files_found", 0), "source": source})
        except Exception as e:
            core.update_job(job_id, {"phase": "error", "done": True, "error": str(e)})
            # `sid` exists only once save_scan has returned (or the deferred branch minted one),
            # and scan_event no-ops on a falsy scan_id — so a crash during the crawl itself is
            # deliberately NOT logged here. There is no scan row to anchor it to yet: the log is
            # scan-anchored, and inventing an anchor for a run that never got one would be worse
            # than the gap. The job record (phase=error above) plus core._job_is_stale remain the
            # record for that window, exactly as before this ADR.
            #
            # On the deferred branch this CAN double up: _scan_discover logs its own scan.failed
            # for a listing failure and then re-raises into here. Both rows are kept rather than
            # deduped — they are two true statements from two frames, with different `detail`,
            # and suppressing the second would need a read-before-write, which is precisely the
            # mutable-cell pattern this log replaces. ADR 0042 has readers take the FIRST
            # terminal event by seq, which resolves it without a write-side check.
            from handlers import scan_event
            scan_event(sid, "scan.failed", phase="error", job_id=job_id,
                       owner_email=user, detail={"message": str(e)[:200], "source": source})
        finally:
            _stop_heartbeat.set()

    threading.Thread(target=_heartbeat, daemon=True).start()
    threading.Thread(target=work, daemon=True).start()
    if inline_discover:
        # The shape the caller asked for by passing queue=true, minus the one claim that would be
        # false: queued=False, because nothing was enqueued and no worker will ever claim this.
        # `inline` says WHY a queue=true request came back unqueued, so a client (or an operator
        # reading a log) is never left inferring it from a missing job row.
        return {"scan_id": pre_scan_id, "job_id": job_id, "queued": False, "inline": True,
                "fanout": fanout, "batch": batch, "workers": core.WORKERS,
                "worker_tier_alive": core.store.worker_tier_alive()}
    return {"job_id": job_id}


@router.post("/scans/{sid}/remediate")
async def remediate_scan(sid: str, request: Request):
    """Async server-side remediation (ADR 0005): enqueue a remediate_file job per
    HTML file in the scan that came from Drive. The worker fixes it and writes the
    corrected copy back to a Remediated/ folder. Needs ACP_WORKERS>0.

    Optional body: {"scope": ["file1.html", "file2.pdf", ...]} — when provided,
    only the listed filenames are enqueued (respects the triage decisions made in
    the UI). Omit or pass an empty body to remediate all eligible files."""
    res = core.store.get_scan(sid, owner=_owner(request))
    if res is None:
        raise HTTPException(404, "scan not found")
    source = (res.get("run") or {}).get("source") or "drive"
    owner = _owner(request)
    import assessment_blocked
    assessment_blocks = assessment_blocked.read(core.store, sid, owner=owner, scan=res)
    blocked_names = {row['file'] for row in assessment_blocks}
    # A Drive token belongs to a Drive job and to nothing else. A SharePoint (or local) scan
    # reads the source bytes Assess cached, so it neither registers nor carries one — and the
    # worker's source dispatch (handlers._remediation_source_bytes) never asks for one either.
    token = request.headers.get("x-drive-token") if source == "drive" else None
    if source == "drive":
        _register_scan_tokens(sid, drive=token)

    # Parse optional scope list from request body.
    scope_set = None
    body = {}
    try:
        body = await request.json()
        if not isinstance(body, dict):
            raise HTTPException(422, "A remediation request must be an object.")
    except HTTPException:
        raise
    except Exception:
        # missing or non-JSON body — treat as no scope filter
        swallowed("routes.scans.remediate_scan: reading the remediate request body failed")

    if "scope" in body:
        scope = body["scope"]
        if (not isinstance(scope, list) or len(scope) > 10000
                or any(not isinstance(name, str) or not name.strip() for name in scope)):
            raise HTTPException(422, "scope must be a list of non-empty filenames.")
        scope_set = set(scope)

    # The planner's policy is an execution contract, not a presentation preference.
    # Recompute its allowed criteria from durable findings; never accept an allow-list
    # or eligibility evidence supplied by the browser. Saved defaults also govern old
    # entry points, while pre-planner deployments retain their existing behavior.
    import remediation_impact_settings as impact_settings
    saved_impact = impact_settings.read_impact_policy(core.store, owner)
    impact_snapshot = None
    impact_allowed = {}
    scan_approval = ((core.store.get_scan(sid, owner=owner) or {}).get('run', {}).get('scope') or {}).get('fix_approval_policy')
    if "remediation_policy" in body or saved_impact["revision"] > 0 or scan_approval is not None:
        try:
            selected_policy = body.get("remediation_policy")
            if scan_approval is not None:
                selected_policy = dict(selected_policy or {key: value for key, value in saved_impact.items() if key != 'revision'})
                selected_policy['fix_approval_policy'] = scan_approval
            impact_snapshot = impact_settings.snapshot_impact_policy(
                core.store, owner, selected_policy)
        except (ValueError, TypeError) as exc:
            raise HTTPException(422, str(exc)) from exc
        from remediation_impact import build_run_impact
        impact = build_run_impact(core.store, sid, owner, impact_snapshot,
                                  scope=sorted(scope_set) if scope_set is not None else None)
        if not impact["integrity"]["complete"] or not impact["capabilities"]["execute"]:
            raise HTTPException(409, "The remediation forecast is incomplete or this policy cannot execute.")
        # Recheck the exact server-derived policy at acceptance: a stale preview
        # or direct API caller must not bypass local-only endpoint readiness.
        if impact_snapshot.get('ai') and impact_snapshot.get('ai_zone') == 'local':
            from ai_plan_readiness import plan_ai_readiness
            readiness = plan_ai_readiness(impact_snapshot, core.store.get_ai_enabled())
            if readiness['blocked']:
                raise HTTPException(409, {'code': readiness['state'],
                    'message': 'Local AI is not ready. Check the private endpoint and configured models, retry readiness, or choose Rules only. This plan does not authorize cloud AI.'})
        for finding in impact["findings"]:
            if finding["lane"] == "automatic":
                impact_allowed.setdefault(finding["file"], set()).add(finding["criterion"])

    # Create the single 'Remediated' folder ONCE here (single-threaded), then pass
    # its id to every job — avoids concurrent workers each creating their own.
    remediated_folder_id = None
    if token:
        try:
            import handlers
            remediated_folder_id = handlers.ensure_remediated_folder(handlers._drive_client(token))
        except Exception:
            remediated_folder_id = None   # jobs fall back to find-or-create
    # One id per SUBMISSION, so live progress can be scoped to the batch a user is watching
    # rather than to everything this scan has ever queued. Re-submitting the same scan (the
    # honest response to a failed run) used to make its dead jobs accumulate against one total:
    # two 147-document batches reported "294 failed" out of 147, and the UI subtracted its way
    # to -147 remediated. See store.remediation_status.
    # THE KEY THE CACHE WAS WRITTEN UNDER. ADR 0020 keys a cached original by its content
    # checksum whenever the listing carried one — {owner}/{checksum} — and only falls back to
    # {owner}/{scan_id}/{filename} when it did not. SharePoint listings have carried
    # quickXorHash since #963, so a SharePoint scan's bytes are under the checksum key.
    #
    # This used to read `f.get("checksum")` off get_scan's file rows, which is ALWAYS None:
    # that SELECT has no checksum column, and file_records.checksum is NULL anyway because the
    # scan report's file rows carry none for save_scan to write. So every job looked under a key
    # nothing had written, missed, and fell through to the Drive downloader. scan_inventory is
    # where the value actually lives — one query for the batch, not one per document.
    try:
        checksums = core.store.get_source_checksums(sid)
    except Exception:
        swallowed("routes.scans.remediate_scan: reading the scan's source checksums failed", sid)
        checksums = {}
    payloads = []
    for f in res["files"]:
        if f['file'] in blocked_names:
            continue
        # Honour the triage scope: skip files the user marked N/A or deferred.
        if scope_set is not None and f["file"] not in scope_set:
            continue
        # Server-side remediators (ADR 0005 step 4): HTML (in-repo), PDF (vendored engine),
        # Office docx/pptx/xlsx (core-properties fixer), and media (a drafted caption file —
        # a proposal, not a rewrite; nothing re-encodes a customer's video).
        #
        # ASKED OF handlers, not spelled out here. This tuple and `_remediate_file`'s own were
        # two literals that had to agree and nothing made them: an extension admitted here and
        # refused there burns a job and logs a deferral, and one admitted there and refused here
        # is a code path nothing can reach. Neither fails loudly, and adding media needed both.
        import handlers
        if not f["file"].lower().endswith(handlers.remediable_extensions()):
            continue
        # Skip already-clean files — nothing to remediate, no point queuing a job.
        if not f.get("issues"):
            continue
        drive_file_id = core.store.get_file_drive_id(sid, f["file"])
        # Drive needs its remote id. Local demo/corpus files are intentionally id-less but their
        # source bytes were cached during Assess (with a corpus fallback in the worker), so they
        # are valid remediation inputs and produce the primary Blob artifact.
        if source == "drive" and not drive_file_id:
            continue
        payloads.append(
            {"scan_id": sid, "file": f["file"], "drive_file_id": drive_file_id,
             "remediated_folder_id": remediated_folder_id, "drive_token": token,
             "source": source, "owner": owner,
             "checksum": checksums.get(f["file"]) or f.get("checksum")})
    snapshot_id, input_manifest_id = _sealed_stage_input(
        sid, "assess", core.store.stage_snapshot_id(sid))
    # Fingerprint the EFFECTIVE file set, not raw request spelling: adding a nonexistent name or
    # reordering the same names is still the same work and must reuse the same execution. Human
    # intent is part of that identity too: editing an approved value for the same file set must
    # create new work rather than hand back a completed execution for the old value.
    selected_files = sorted(p["file"] for p in payloads)
    decision_digest = core.store.remediation_decision_digest(sid, selected_files, owner=owner)
    import remediation_automation_policy as automation_policy
    policy_snapshot = automation_policy.snapshot_for_future_run(core.store, owner)
    request_fingerprint = _json.dumps(
        {"files": selected_files, "decision_digest": decision_digest,
         "automation_policy_snapshot_id": policy_snapshot["snapshot_id"],
         **({"remediation_impact_policy": impact_snapshot,
             "allowed_rules": {name: sorted(impact_allowed.get(name, ())) for name in selected_files}}
            if impact_snapshot else {})}, sort_keys=True)
    import progress_evidence
    try:
        document_baseline = progress_evidence.capture_documents(core.store, res, selected_files,
            owner=owner, snapshot_id=snapshot_id, request_fingerprint=request_fingerprint)
    except Exception:
        document_baseline = None
        swallowed('routes.scans.remediate_scan: admission progress baseline unavailable', sid)
    for payload in payloads:
        # Provenance only; no decision content enters the queue payload.
        payload["decision_digest"] = decision_digest
        payload["automation_policy"] = policy_snapshot
        payload['document_progress_baseline'] = document_baseline
        if impact_snapshot:
            payload["remediation_impact_policy"] = impact_snapshot
            payload["remediation_impact_allowed_rules"] = sorted(impact_allowed.get(payload["file"], ()))
    execution = _enqueue_stage_batch(
        sid, "remediate", "remediate_file", payloads, snapshot_id=snapshot_id,
        request_fingerprint=request_fingerprint, input_manifest_id=input_manifest_id)
    automation_policy.bind_run_snapshot(core.store, owner, owner, sid,
                                        execution["batch_id"], policy_snapshot)
    core.store.seed_finding_dispositions(sid, execution["batch_id"], snapshot_id=snapshot_id)
    # AFTER the jobs exist, never before: the run is "accepted" precisely when durable work has
    # been enqueued for it, and an acceptance event that led the enqueue would let the panel show
    # a run that nothing will ever claim. Emitted once per batch — the run-level transition PRD §7
    # calls Accepted.
    #
    # NOT on a reused execution. enqueue_stage_batch is idempotent on the request fingerprint, so
    # re-submitting the same file set returns the EXISTING batch rather than making one. That is
    # the same run, already accepted; announcing it again would put a second acceptance in the
    # log for work that was never re-enqueued, and a client replaying the log would see one run
    # start twice.
    if execution["job_ids"] and not execution.get("reused"):
        import handlers
        handlers.scan_event(sid, "remediate.accepted", job_id=execution["job_ids"][0],
                            correlation_id=execution["batch_id"],
                            detail={"documents": len(execution["job_ids"]),
                                    "batch_id": execution["batch_id"]})
    return {"scan_id": sid, "enqueued": len(execution["job_ids"]),
            'assessment_blocked_files': assessment_blocks,
            "job_ids": execution["job_ids"], "batch_id": execution["batch_id"],
            "snapshot_id": snapshot_id, "decision_digest": decision_digest,
            "reused": execution["reused"],
            # How many DEAD documents this call revived. `enqueued` counts the execution's
            # documents either way, so on its own it cannot tell a retry that queued work from one
            # that matched an existing execution and queued none — which is exactly the question
            # an operator re-submitting after a failure is asking.
            "requeued": execution.get("requeued", 0),
            "workers": core.WORKERS, "worker_tier_alive": core.store.worker_tier_alive()}


@router.post("/scans/{sid}/cancel")
def cancel_scan(sid: str, request: Request):
    """Stop an in-flight fan-out scan (found live 2026-07-11: there was NO way to stop a
    scan — a wedged one blocked all new scans until the lease sweeper caught up). Kills the
    scan's outstanding jobs and closes the run as 'cancelled'; files already analysed keep
    their records. Owner-scoped: you can only cancel your own scan.

    Falls back to cancel_queued_job when cancel_scan says no: a scan_id issued by the durable
    (queued) start path is real and known to the caller from the moment it's enqueued, but has
    no scan_runs row — and so nothing for cancel_scan to find — until a worker actually claims
    it (found live 2026-08-21: a scan stuck waiting for a worker had no way to be cancelled
    either, the other half of the 2026-07-11 gap this route was built to close)."""
    if core.store.cancel_scan(sid, owner=_owner(request)):
        return {"scan_id": sid, "status": "cancelled"}
    if core.store.cancel_queued_job(sid):
        return {"scan_id": sid, "status": "cancelled", "was_queued": True}
    raise HTTPException(409, "scan not found, not yours, or not running")


@router.put("/scans/{sid}/acknowledge")
def acknowledge_scan(sid: str, request: Request):
    """Record that the operator has reviewed lifecycle recommendations and approved this
    discovery snapshot for handoff to Assess (PRD §EX-10). Idempotent — re-acknowledging
    an already-acknowledged scan overwrites the prior stamp."""
    actor = _owner(request)
    if core.store.acknowledge_scan(sid, actor=actor, owner=actor):
        return {"scan_id": sid, "acknowledged": True, "actor": actor}
    raise HTTPException(404, "scan not found or not yours")


@router.delete("/scans/{sid}/acknowledge")
def unacknowledge_scan(sid: str, request: Request):
    """Withdraw a prior acknowledgement (e.g. if lifecycle rules were changed after approval)."""
    if core.store.unacknowledge_scan(sid, owner=_owner(request)):
        return {"scan_id": sid, "acknowledged": False}
    raise HTTPException(404, "scan not found or not yours")


@router.get("/scans/jobs/{job_id}")
def scan_job(job_id: str, request: Request):
    # core.get_job_state, not core.JOBS: the poll must be answerable by whichever replica the
    # request lands on, which is the whole point of removing session affinity.
    j = _scan_job_state(job_id)
    if j is None:
        raise HTTPException(404, "job not found")
    _require_job_owner(job_id, j, request)
    return j


_LOAD_DURABLE_JOB = object()


def _scan_job_state(job_id: str, durable=_LOAD_DURABLE_JOB) -> dict | None:
    """Progress enriches a job; its durable queue row owns status and identity.

    A caller may pass its bounded-age durable reading to keep the 4Hz SSE
    progress loop from making database queries at the same rate.
    """
    progress = core.get_job_state(job_id, reconcile_durable=False)
    if durable is _LOAD_DURABLE_JOB:
        durable = core.store.get_job(job_id)
    if not isinstance(durable, dict):
        if progress is not None and core._job_is_stale(progress) and progress.get('state_source') != 'durable_queue':
            return {**progress, 'phase': 'error', 'done': True,
                    'error': 'scan interrupted — the server likely restarted mid-run; please start a new scan'}
        return progress
    payload = durable.get('payload')
    payload = payload if isinstance(payload, dict) else {}
    status = durable.get('status') or 'queued'
    scan_id = durable.get('scan_id') or payload.get('scan_id')
    previous = dict(progress or {})
    if previous.get('scan_id') != scan_id:
        previous = {}
    for private_key in ('payload', 'last_error', 'owner_email', 'user'):
        previous.pop(private_key, None)
    phase = previous.get('phase')
    if status != 'running' or previous.get('done') or phase in (
            None, 'error', 'failed', 'complete', 'done', 'cancelled'):
        phase = {'done': 'complete', 'dead': 'error'}.get(status, status)
    if not scan_id:
        previous['owner_email'] = payload.get('user') or payload.get('owner_email')
    return {**previous, 'job_id': job_id,
        'scan_id': scan_id,
        'status': 'failed' if status == 'dead' else status, 'phase': phase,
        'done': status in ('done', 'dead', 'cancelled'),
        'error': 'The job failed. Check the scan for details.' if status == 'dead' else None}


def _require_job_owner(job_id: str, state: dict, request: Request) -> None:
    """Fail closed unless this job belongs to the signed-in caller."""
    owner = _owner(request)
    scan_id = state.get("scan_id")
    if scan_id:
        if core.store.get_scan(scan_id, owner=owner) is None:
            raise HTTPException(404, "scan not found")
        return
    state_owner = state.get("owner_email") or state.get("user")
    if not state_owner:
        durable = core.store.get_job(job_id)
        payload = durable.get("payload") if durable else None
        if isinstance(payload, dict):
            state_owner = payload.get("user") or payload.get("owner_email")
    if state_owner != owner:
        raise HTTPException(404, "job not found")


@router.get("/scans/jobs/{job_id}/stream")
async def stream_job_state(job_id: str, request: Request):
    """SSE stream for live job progress. Yields an event whenever the job's seq counter
    advances (i.e. any field changed), then a final event when done/error. Polls Redis
    every 250 ms — fine-grained enough given the 500 ms coalesce window in core.update_job.

    Falls back gracefully when Redis is absent: get_job_state reads the in-memory JOBS dict
    in that case and the stream still works (same-replica only, but correctness is preserved).

    Frontend usage: new EventSource('/scans/jobs/{id}/stream') with .onmessage = e => setProgress(JSON.parse(e.data)).
    Keep the existing GET /scans/jobs/{id} polling as a fallback for browsers/proxies that
    strip SSE connections."""
    import asyncio
    import json as _j

    durable = await asyncio.to_thread(core.store.get_job, job_id)
    initial_durable_read = asyncio.get_running_loop().time()
    initial = await asyncio.to_thread(_scan_job_state, job_id, durable)
    if initial is None:
        raise HTTPException(404, "job not found")
    _require_job_owner(job_id, initial, request)

    async def _generate():
        last_signature = None
        not_found_streak = 0
        cached_durable = durable
        next_durable_read = initial_durable_read + 1.0
        authorized_identity = (initial.get('scan_id'), initial.get('owner_email'), initial.get('user'))
        while True:
            now = asyncio.get_running_loop().time()
            refreshed = now >= next_durable_read
            if refreshed:
                cached_durable = await asyncio.to_thread(core.store.get_job, job_id)
                next_durable_read = now + 1.0
            state = await asyncio.to_thread(_scan_job_state, job_id, cached_durable)
            if state is None:
                not_found_streak += 1
                if not_found_streak >= 4:   # ~1s of misses before giving up
                    yield "event: error\ndata: {\"error\": \"job not found\"}\n\n"
                    return
                await asyncio.sleep(0.25)
                continue
            not_found_streak = 0
            identity = (state.get('scan_id'), state.get('owner_email'), state.get('user'))
            if refreshed or identity != authorized_identity:
                try:
                    await asyncio.to_thread(_require_job_owner, job_id, state, request)
                except HTTPException:
                    yield "event: error\ndata: {\"error\": \"job not found\"}\n\n"
                    return
                authorized_identity = identity
            signature = (int(state.get('seq') or 0), state.get('status'), bool(state.get('done')))
            if signature != last_signature:
                last_signature = signature
                yield f"data: {_j.dumps(state)}\n\n"
            if state.get("done"):
                # One final event so the client can close the EventSource cleanly.
                yield "event: done\ndata: {\"done\": true}\n\n"
                return
            await asyncio.sleep(0.25)

    return StreamingResponse(
        _generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",       # disable nginx/ACA buffering
            "Connection": "keep-alive",
        },
    )


def _discover_fallback_frame(checkpoint: dict | None, checkpoint_at, events: list[dict]) -> dict | None:
    """The ONE not-live frame `stream_discover_state` emits after 4 missed polls (ADR 0042 PR 4).

    PURE — the load-bearing bit, unit-tested with plain dicts, so the generator around it stays
    the shape it has been through four bug fixes.

    WHAT CHANGED, AND WHAT DID NOT. Before this, the frame was the Postgres checkpoint alone
    (`scan_runs.live_checkpoint`) and was emitted ONLY when that column was non-NULL. A job that
    died before its first phase transition never got a checkpoint written — `_maybe_checkpoint`
    flushes on phase/done/error or every 20s, so a run killed inside its first seconds has
    nothing — and the client got the error frame with no preceding data at all: an empty panel,
    which is the exact gap this PR closes. `scan_events` has rows for that run (scan.queued,
    scan.claimed), so there is something honest to say.

    Still ONE frame, still `live: False`, still followed by the same `event: error` and the same
    close. No new terminal state and no new frame type — see the route's own comment.

    THE CHECKPOINT WINS WHEREVER IT SPEAKS. Events only FILL fields the checkpoint does not
    carry; they never overwrite one. The checkpoint is the accumulated job state, which is
    strictly richer than the handful of columns an event row holds, and a merge that let the
    coarser source win would make the frame worse than it was.

    THREE FIELDS ARE NEVER SYNTHESIZED, each for a specific reason:
      * `seq` — frontend/src/liveJobStateGuard.js drops a frame whose seq is lower than the one
        it already holds, and prefers a higher one. A seq invented here is not comparable with
        the Redis HINCRBY counter those frames carry, so it would either suppress this frame or
        suppress a real later one. Absent, the guard falls through to accepting — pinned by
        liveJobStateGuard.test.js's "accepts when seq is missing on either side" case — which is
        right here: this frame is built only after four consecutive Redis misses, so there is no
        newer truth left to protect.
      * `done` — a frame claiming the run finished would end the client's progress UI on a run
        that may simply have lost its replica. Not knowing is not the same as being done.
      * `error` — the terminal `event: error` that follows already says the stream ended; putting
        an error INTO the data frame would make a recoverable gap read as a failed scan.

    `attempt` IS carried through when an event has it, because it is a real durable counter
    (jobs.attempts) and it is the other half of the guard's staleness check.
    """
    frame: dict = dict(checkpoint) if isinstance(checkpoint, dict) else {}
    had_checkpoint = bool(frame)

    # Newest first — the most recent event is the best answer to "how far did it get".
    for e in reversed(events or []):
        if frame.get("phase") is None and e.get("phase"):
            frame["phase"] = e["phase"]
        if frame.get("attempt") is None and isinstance(e.get("attempt"), int):
            frame["attempt"] = e["attempt"]
        if frame.get("files_found") is None:
            # Only from an event that actually counted files. `detail` is per-kind narration, so
            # a key that is absent means the event never knew the number — not zero.
            d = e.get("detail")
            if isinstance(d, dict) and isinstance(d.get("files_found"), int):
                frame["files_found"] = d["files_found"]

    if not frame:
        return None                      # no checkpoint and no events — nothing honest to say

    frame["live"] = False
    # WHEN this state was true. The checkpoint's own stamp when we have one; otherwise the last
    # event's timestamp, because that is what the frame was actually built from. Either way the
    # client's "last known state, Ns ago" is measuring the right instant.
    if had_checkpoint:
        frame["checkpoint_at"] = checkpoint_at
    else:
        frame["checkpoint_at"] = (events[-1].get("occurred_at") if events else None)
    return frame


@router.get("/scans/{scan_id}/discover/stream")
async def stream_discover_state(scan_id: str, request: Request):
    """SSE stream for Discovery scan progress, anchored to scan_id.

    Preferred over /scans/jobs/{job_id}/stream: scan_id is stable across retries — if a job
    fails and the worker retries, the same scan_id gets a new job_id, but this stream
    re-resolves the mapping on every poll and continues seamlessly.

    Writes to the scan_id→job_id mapping come from core.set_job / update_job whenever a
    scan_id field appears in the state (durable queue path: at job-claim time; thread path:
    when the discover completes and update_job is called with the assigned scan_id).

    Frontend: new EventSource('/scans/{id}/discover/stream') — fall back to polling
    GET /scans/jobs/{id} when the stream is unavailable."""
    import asyncio
    import json as _j

    # Same ownership check GET /scans/{sid} makes — this endpoint originally had none at all,
    # so any authenticated user who had (or guessed) a scan_id could stream someone else's live
    # discovery progress (file counts, phase, lifecycle stats). 404, not 403, for the same reason
    # get_scan does it: a scan id must not be usable as an existence oracle across accounts.
    if core.store.get_scan(scan_id, owner=_owner(request)) is None:
        raise HTTPException(404, "scan not found")

    async def _generate():
        last_seq = -1
        job_id: str | None = None
        not_found_streak = 0
        while True:
            # Re-resolve on each iteration: the job_id can change on retry.
            job_id = await asyncio.to_thread(core.get_job_id_for_scan, scan_id)
            state = None
            if job_id:
                state = await asyncio.to_thread(core.get_job_state, job_id)
            if state is None:
                not_found_streak += 1
                if not_found_streak >= 4:
                    # Redis is the fast live source; it is also ephemeral (unreachable, a key
                    # TTL'd out, a no-Redis replica's in-memory JOBS no other replica can see).
                    # Before giving up entirely, offer the sparse Postgres checkpoint
                    # (core.py's _maybe_checkpoint) so the client can show "last known state, Ns
                    # ago" instead of nothing. One frame, clearly marked as NOT live, then the
                    # same terminal error as before — this is a recovery aid, not a substitute
                    # for the real stream.
                    row = await asyncio.to_thread(core.store.get_scan, scan_id, _owner(request))
                    checkpoint = (row or {}).get("run", {}).get("live_checkpoint")
                    checkpoint_at = (row or {}).get("run", {}).get("live_checkpoint_at")
                    # ADR 0042 PR 4 — fill the frame's gaps from the durable lifecycle log when
                    # the checkpoint is missing or thin. NO `owner=` on this read: the ownership
                    # gate is get_scan, above, and a run's earliest events carry owner_email=NULL
                    # (the thread path mints a scan_id before it knows the user), so filtering
                    # again here would drop exactly the queued/claimed rows that explain a scan
                    # that died before it ever started. Best-effort: a log read must not be able
                    # to cost the client the checkpoint frame it would have had without it.
                    # The default limit (500) takes the OLDEST rows, and this wants the newest —
                    # a mismatch that only bites above 500 events for one run, ~17x the 15-30
                    # ADR 0042 sizes a run at. Left as-is rather than adding a newest-first
                    # store parameter for it, because the degradation runs the safe way: a run
                    # that somehow exceeded 500 would fill from EARLIER events and understate
                    # how far it got. Overstating progress on a run that died is the failure
                    # that would matter.
                    try:
                        events = await asyncio.to_thread(
                            core.store.list_scan_events, scan_id)
                    except Exception:
                        events = []
                    frame = _discover_fallback_frame(checkpoint, checkpoint_at, events)
                    if frame:
                        yield f"data: {_j.dumps(frame)}\n\n"
                    yield "event: error\ndata: {\"error\": \"no active job for this scan\"}\n\n"
                    return
                await asyncio.sleep(0.25)
                continue
            not_found_streak = 0
            seq = int(state.get("seq") or 0)
            if seq != last_seq:
                last_seq = seq
                yield f"data: {_j.dumps(state)}\n\n"
            if state.get("done"):
                yield "event: done\ndata: {\"done\": true}\n\n"
                return
            await asyncio.sleep(0.25)

    return StreamingResponse(
        _generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


@router.get("/scans")
def scans(request: Request):
    # list_scans() filters to completed_at IS NOT NULL, which an ADR 0020 Discover-only run
    # never sets — the same blind spot list_finished_scans() was built to fix for /monitor/estate
    # (#907) and /schedule (#908). THIS is the call site those two were downstream symptoms of:
    # the SPA's own App.jsx calls listScans() -> here on every load, and pickDefaultScan() (see
    # frontend/src/defaultScan.js) returns null for an empty list — so on any deployment where
    # Discover-only is the default (ADR 0020, the common case since it shipped), a user whose
    # most recent scans are all Discover-only got NO scan auto-selected at all. Every tab reads
    # that as "nothing has ever been scanned": Discover shows 0 documents with no scope line, no
    # runinfo bar, and no inventory fetch even attempted (scanId was undefined, not just empty)
    # — because Assess is gated on the same missing `run`, it read as 0 there too.
    #
    # Found live 2026-08-28 from a genuinely fresh scan reporting 0/0 on both tabs, traced via a
    # screenshot showing the runinfo bar (which requires scanList.length > 0) entirely absent —
    # proof scanList itself was empty, not just under-detailed.
    return core.store.list_finished_scans(owner=_owner(request))


# Registered before /scans/{sid} so "active" isn't treated as a scan id.
@router.get("/scans/active")
def active_scan(request: Request):
    """The in-flight scan, if any — lets the UI reconnect to a running scan after a
    page reload (the durable fan-out keeps running server-side). Scoped to the user."""
    return core.store.active_scan(owner=_owner(request)) or {}


@router.get("/scans/{sid}")
def scan(sid: str, request: Request, response: Response):
    """The full per-file payload (file_records, issue_records) — the one genuinely heavy read
    in the app (get_scan's own docstring covers the query shape). Conditional-fetch support
    (ETag / If-None-Match) so a caller re-fetching a scan it already has doesn't pay that cost
    when nothing changed: `scan_runs.revision` is bumped on every write that would change this
    response (Discover/Assess/Remediate/Publish all go through the same bump_revision path), so
    it is exactly the freshness key an ETag needs. get_scan_head() is the SAME cheap
    id/status/revision-only lookup GET /workspace/bootstrap already uses for this — one indexed
    row read, none of get_scan's joins — so a matched ETag costs almost nothing, in contrast to
    the query this whole mechanism exists to let a caller skip.

    Weak (`W/`) because equality here is semantic (same revision), not a byte-identical response
    — the freshness enrichment below is computed fresh every call regardless.
    """
    owner = _owner(request)
    head = core.store.get_scan_head(sid, owner=owner)
    if head is None:
        raise HTTPException(404, "scan not found")
    etag = f'W/"{head["revision"]}"'
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304)
    res = core.store.get_scan(sid, owner=owner)
    if res is None:
        raise HTTPException(404, "scan not found")
    res["run"]["freshness"] = _scan_freshness(sid, res["run"])
    import assessment_blocked
    assessment_blocked.annotate(res, assessment_blocked.read(core.store, sid, owner=owner, scan=res))
    response.headers["ETag"] = etag
    # This payload changes document by document during Assess.  Require revalidation so the
    # two-second progress poll cannot be satisfied from a stale Discover response.
    response.headers["Cache-Control"] = "private, no-cache"
    return res


@router.delete("/scans/{sid}", status_code=200)
def delete_scan(sid: str, request: Request):
    """Permanently erase one scan and all data derived from it (HIPAA BAA right-to-erasure).

    Scope: the requesting user may only delete their own scans — the same owner_email gate
    that guards every other /scans/{sid} endpoint.  Returns 404 for a scan that does not
    exist OR belongs to a different user (no information leak about other users' scan IDs).

    What is erased:
      - All database rows keyed on scan_id (findings, file records, rule traces, HITL queue,
        AI call log, applied fixes, stage timings, PII findings, jobs, …).
      - The scan_runs row itself.
      - All blobs under {owner}/{scan_id}/ (remediated files, source copies, render previews)
        PLUS any {owner}/{checksum} source-cache blobs this scan's own downloads populated
        (ADR 0020 keys the sources cache by content checksum, not scan_id, when one was known
        pre-download — a plain {owner}/{scan_id}/ prefix sweep alone would miss those).

    What survives:
      - decision_log — the immutable audit trail. A deletion event IS appended to it here so
        the audit record of "who deleted what and when" is preserved, not erased.
    """
    import blob as blob_mod

    owner = _owner(request)
    # Read BEFORE delete_scan: it queries file_records, which delete_scan is about to remove.
    checksums = core.store.scan_checksums(sid, owner)
    result = core.store.delete_scan(sid, owner)
    if result is None:
        raise HTTPException(404, "scan not found")
    blobs = blob_mod.purge_scan(owner, sid, checksums=checksums)
    core.store.log_decision(owner, "delete_scan", scan_id=sid,
                            detail=f"scan deleted; blobs={blobs}")
    return {"deleted": True, "scan_id": sid, "blobs_purged": blobs}


@router.get("/scans/{sid}/timings")
def scan_timings(sid: str, request: Request):
    """Per-stage timing rollup for one scan (ADR 0037 Step 0 — measure first): where the scan spent its
    time (download vs analyse), the per-stage average seconds, and the bottleneck stage. Owner-scoped via
    the same get_scan gate, so it never reveals another user's scan; a scan with nothing recorded reports
    zeros and bottleneck=null rather than a fabricated number."""
    if core.store.get_scan(sid, owner=_owner(request)) is None:
        raise HTTPException(404, "scan not found")
    return core.store.scan_timings(sid)


@router.get("/scans/{sid}/files/{filename:path}/status")
def get_file_accessibility_status(sid: str, filename: str, request: Request):
    """ADR 0026 — the authoritative Accessibility Status for one file. Derived-at-read over the
    per-file certification facts (the SAME `_rule_outcome` the coverage matrix uses), so the hero
    card can never disagree with the detail. Owner-scoped; always 200 — an unknown scan/file returns
    `{available: false}` so the card degrades rather than erroring."""
    import accessibility_status as _status

    owner = _owner(request)
    if core.store.get_scan(sid, owner=owner) is None:
        return {"available": False, "reason": "scan_not_found"}
    return _status.file_status(core.store, sid, filename)


@router.get("/scans/{sid}/status")
def get_scan_accessibility_status(sid: str, request: Request, prefix: str | None = None):
    """ADR 0026 PR 3 — the Accessibility Status for a whole scan: the per-file models summed and the
    state machine re-derived over the totals (same derivation → the roll-up can never disagree with
    the per-file cards). ?prefix= narrows to a folder (same summation over the subset). Powers the
    scan-level card + the Confidence Dashboard. Owner-scoped, always-200 degrade."""
    import accessibility_status as _status

    if core.store.get_scan(sid, owner=_owner(request)) is None:
        return {"available": False, "reason": "scan_not_found"}
    return _status.scan_status(core.store, sid, path_prefix=prefix or None)


_QUEUE_ESTIMATE_KINDS = ("discover", "assess", "remediate")


@router.get("/scans/{sid}/queue-estimate")
def get_queue_estimate(sid: str, request: Request, kind: str = Query(...)):
    """"When will my work actually begin?" for the Discover/Assess/Remediate queue-status panel —
    Store.queue_estimate's docstring covers the math. Always 200, degrading to
    {"available": false} for an unknown/foreign scan (get_scan_head is the owner gate — same cheap
    id/status/revision-only lookup GET /workspace/bootstrap and the ETag route already use) or a
    scan with no live job of the requested `kind`."""
    if kind not in _QUEUE_ESTIMATE_KINDS:
        raise HTTPException(422, f"kind must be one of {_QUEUE_ESTIMATE_KINDS}")
    owner = _owner(request)
    if core.store.get_scan_head(sid, owner=owner) is None:
        return {"available": False, "reason": "scan_not_found"}
    # No API exposes the standalone worker tier's exact concurrent-replica count (#113 split
    # topology), so a live tier with zero in-process workers is floored at 1 ready worker rather
    # than reported as 0 — the same "online, capacity unknown" distinction WorkerAvailability.jsx
    # draws, not a real measurement. It plays no part in the wait math (see queue_estimate's
    # docstring); it is display-only.
    from worker_stage_capacity import worker_role_alive
    ready_workers = core.WORKERS if core.WORKERS > 0 else (
        1 if worker_role_alive(core.store, kind) else 0)
    return core.store.queue_estimate(sid, kind, owner=owner, ready_workers=ready_workers)


@router.get("/scans/{sid}/live")
def get_live_snapshot(sid: str, request: Request):
    """The authoritative live-run snapshot for the Assess running screen (Live Assessment Experience
    PRD §8): the reconciled file-outcome KPIs (read from the SAME run summary the final cert uses, so
    running can never disagree with final), the eligible denominator, and — when the queue layer is
    present — the live worker/queue block, as ONE owner-scoped object the running screen consumes and
    reconnects against. Always 200: an unknown or foreign scan returns {"available": false} so the
    screen degrades rather than erroring."""
    import datetime as _dt
    import live_snapshot as _ls
    return _ls.build_snapshot(core.store, sid, owner=_owner(request),
                              now_iso=_dt.datetime.now(_dt.timezone.utc).isoformat())


# Server-Sent-Events stream tuning. Interval: how often the server re-reads the run's state (§9 asks
# for 500–2000ms batching — 1s sits in that band). Heartbeat: idle intervals between comment frames
# that keep the connection warm without a data frame. Max iters: a safety ceiling (~30 min) so a run
# that never terminates can't hold a socket forever — the client (SSE) auto-reconnects and resumes.
_STREAM_INTERVAL_S = 1.0
_HEARTBEAT_EVERY = 15
_MAX_STREAM_ITERS = 1800


@router.get("/scans/{sid}/events")
async def stream_live_events(sid: str, request: Request):
    """Server-Sent-Events stream of the live-run snapshot (Live Assessment Experience PRD §8): the SAME
    authoritative object /scans/{sid}/live returns, PUSHED as it changes instead of polled. The server
    tails the run's persisted state; a `data:` frame is emitted only when the snapshot's content
    changes (generated_at aside), a comment frame keeps the connection warm between changes, and the
    stream sends a final frame then closes when the run reaches a terminal state (or the client
    disconnects, or the safety ceiling trips). Owner-scoped. No worker changes — reconciles with
    /live by construction (same builder).

    NOTHING CONSUMES THIS TODAY, and that is a decision, not an oversight (2026-08-30). The Assess
    running screen POLLS `GET /scans/{sid}/live` every 2s via `useLiveSnapshot`. Written down here
    because an endpoint that is live, owner-scoped and tested reads as shipped live-streaming on
    every status list — and this one streams to nobody. Surfaced by ADR 0043's research, which also
    corrected ADR 0042's table for crediting it a client it never had.

    Switching the running screen to it was considered and rejected on four counts, the first being
    the one that decides it: the generator below calls `build_snapshot` — real DB work — every
    `_STREAM_INTERVAL_S` (1.0s) per connected client, against the poll's 2.0s, so it is MORE
    Postgres read load per viewer, not less, plus a held socket and coroutine each. Then: the
    browser's EventSource cannot send the bearer header this app authenticates with (a token in the
    URL would reach proxy logs — see api.js's openDiscoverStream for the same constraint), so a
    client means hand-rolling a second fetch+ReadableStream reader; `_MAX_STREAM_ITERS` means a long
    run outlives its own stream and needs reconnect logic the poll does not; and ADR 0043 settles
    the general case — these are snapshot-REPLACE streams, so the first frame after any reconnect
    already IS current state.

    NOT deleted, per CLAUDE.md's instruction to keep retired features so restoring one is a single
    commit. The absence of a client is asserted by frontend/src/liveEventsStreamUnused.test.js; that
    test failing is the signal a client arrived, and the reminder to delete the test.

    The "reconnect is free" line this docstring used to carry has been removed rather than reworded:
    it described `EventSource`'s automatic reconnect, which requires a browser EventSource that, per
    the above, cannot exist for this endpoint as authenticated."""
    import asyncio
    import datetime as _dt
    import json as _json
    import live_snapshot as _ls

    owner = _owner(request)

    async def _gen():
        last_sig = None
        idle = 0
        for _ in range(_MAX_STREAM_ITERS):
            if await request.is_disconnected():
                return
            now = _dt.datetime.now(_dt.timezone.utc).isoformat()
            # build_snapshot is sync DB work — run it off the event loop so one stream can't block others.
            snap = await asyncio.to_thread(_ls.build_snapshot, core.store, sid, owner, now)
            sig = _ls.snapshot_signature(snap)
            if sig != last_sig:
                last_sig = sig
                idle = 0
                yield f"data: {_json.dumps(snap)}\n\n"
            else:
                idle += 1
                if idle >= _HEARTBEAT_EVERY:
                    idle = 0
                    yield ": keep-alive\n\n"
            # Terminal: an unknown/foreign scan, or a run no longer active — the final frame is already
            # out, so close. The client sees the terminal snapshot and stops reconnecting.
            if not snap.get("available") or not snap.get("active"):
                return
            await asyncio.sleep(_STREAM_INTERVAL_S)

    return StreamingResponse(_gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "Connection": "keep-alive",
                                      "X-Accel-Buffering": "no"})


@router.get("/scans/{sid}/files/{filename:path}/examined")
def get_examined_counts(sid: str, filename: str, request: Request):
    """Engine-reported examined-element counts for one document (ADR 0026 Epic 2): the classify()
    inventory persisted at scan time — a real walk of the package/PDF, so the manifest can say
    "of N images examined" honestly. Owner-scoped, always-200 degrade ({available:false} when the
    document was never classified)."""
    if core.store.get_scan(sid, owner=_owner(request)) is None:
        return {"available": False, "reason": "scan_not_found"}
    row = core.store.get_document_examined(filename)
    if not row:
        return {"available": False, "reason": "not_classified"}
    return {"available": True, "pages": row.get("pages"), "images": row.get("images"),
            "has_text": bool(row.get("has_text")), "is_scanned": bool(row.get("is_scanned"))}


@router.post("/scans/{sid}/files/{filename:path}/confirm")
async def confirm_review_criterion(sid: str, filename: str, request: Request):
    """Confirm-the-pass (ADR 0026 / Epic 3): record a human's verification of a 🟡 review
    criterion. Body: {"sc": "1.4.11", "note": "..."} — writes the same immutable hitl.approved
    decision the review queue writes, so every downstream surface (status buckets, certification
    facts, Assessment Timeline) picks it up unchanged. Guarded: only a REVIEW-outcome criterion can
    be confirmed (a FAIL needs a fix, not a signature). Owner-scoped."""
    import accessibility_status as _status

    owner = _owner(request)
    if core.store.get_scan(sid, owner=owner) is None:
        raise HTTPException(404, "scan not found")
    try:
        body = await request.json()
    except Exception:
        body = {}
    return _status.confirm_review_criterion(
        core.store, sid, filename, (body.get("sc") or "").strip(), owner, body.get("note"))


@router.get("/scans/{sid}/assessment-scope")
def get_assessment_scope(sid: str, request: Request):
    """Read the selected scope without exposing administrative settings."""
    from assessment_policy import scope_as_json
    if core.store.get_scan(sid, owner=_owner(request)) is None:
        raise HTTPException(404, "scan not found")
    scope = core.store.get_scan_scope(sid, refresh=True)
    return {"scan_id": sid, "scan_scope": (scope_as_json(scope) if scope is not None
                                           else core.store.get_setting("scan_scope", "") or "")}


@router.put("/scans/{sid}/assessment-scope")
def set_assessment_scope(sid: str, body: dict, request: Request):
    """Save this scan's exact selected criteria; never change platform defaults."""
    from assessment_policy import parse_scope_setting, scope_as_json
    owner = _owner(request)
    scan = core.store.get_scan(sid, owner=owner)
    if scan is None:
        raise HTTPException(404, "scan not found")
    if set(body) != {"scan_scope"} or not isinstance(body["scan_scope"], (str, dict)):
        raise HTTPException(422, "scan_scope must be the only field and contain a scope")
    if any(w.get("scan_id") == sid for w in core.store.active_workflows(owner)):
        raise HTTPException(409, "Wait for this scan's active work to finish before changing its criteria")
    raw = _json.dumps(body["scan_scope"]) if isinstance(body["scan_scope"], dict) else body["scan_scope"]
    scope, problem = parse_scope_setting(raw)
    if problem or not scope:
        raise HTTPException(422, f"scan_scope: {problem or 'select at least one criterion'}")
    frozen = scope_as_json(scope)
    core.store.merge_scan_scope(sid, {"scan_scope": frozen})
    if core.store.get_scan_scope(sid, refresh=True) != scope:
        raise HTTPException(409, "The scan scope could not be saved; reload this scan")
    core.store.log_decision(owner, "assessment.scope_selected", detail=f"{sid} · {len(scope)} criteria")
    return {"scan_id": sid, "scan_scope": frozen}


@router.post("/scans/{sid}/assess")
def assess(sid: str, request: Request, level: str = Query("AA"),
           include_lifecycle_flagged: bool = Query(False), body: dict | None = Body(None)):
    """Run the assessment. In the deferred-analysis model (ADR 0020) a Discover-only scan has an
    inventory but no assessed file_records yet, so this KICKS OFF the download+WCAG fan-out (the
    heavy work now lives here, not in Discover) — assessed_at is stamped when that analysis
    finalizes. In the immediate model it just marks assessed + builds the assess trace, as before.
    Both enqueue to the durable worker."""
    scan = core.store.get_scan(sid, owner=_owner(request))
    if scan is None:
        raise HTTPException(404, "scan not found")
    approval_choice = None
    if isinstance(body, dict) and 'fix_approval_policy' in body:
        from fix_approval_policy import normalize
        try:
            approval_choice = normalize(body['fix_approval_policy'])
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        from assess_approval_intent import freeze
        try:
            freeze(core.store, sid, approval_choice)
        except (ValueError, TypeError) as exc:
            raise HTTPException(409, str(exc)) from exc
    integrity = ((scan.get("run", {}).get("scope") or {}).get("integrity") or {})
    if integrity.get("status") == "blocked":
        raise HTTPException(409, integrity.get("message") or
                            "Discovery integrity check failed; run Discovery again before Assessment")
    import datetime as _dt
    # Deferred: analysis hasn't run yet (inventory present, no assessed rows, scan 'discovered').
    # Start it; do NOT mark assessed here — that happens at finalize when results actually exist.
    deferred_pending = (
        (scan.get("run", {}).get("status") == "discovered")
        and core.store.count_inventory(sid) > 0
        and core.store.get_setting(f"assess_params:{sid}") is not None
    )
    if deferred_pending:
        # include_lifecycle_flagged (PRD §4.5) is the authorized override that pulls
        # archive/delete-flagged files back into Assess. This route already gates on the scan
        # owner (get_scan owner=... above), so reaching here IS the owner-gate.
        # Phase 3a freeze: if no scope was captured at discover time (operator configured it
        # after the discover ran), freeze the current operator scope NOW before enqueuing so
        # _scan_assess / analyse_and_assess read the intended criteria, not an open slate.
        if core.store.get_scan_scope(sid, refresh=True) is None:
            from assessment_policy import active_scope as _active_scope, scope_as_json as _scope_as_json
            _live = _active_scope(core.store)
            if _live:
                core.store.merge_scan_scope(sid, {"scan_scope": _scope_as_json(_live)})
        snapshot_id, input_manifest_id = _sealed_stage_input(
            sid, "discover", core.store.stage_snapshot_id(sid))
        from assess_approval_intent import request_fingerprint as approval_fingerprint
        request_fingerprint = approval_fingerprint(level, include_lifecycle_flagged,
            approval_choice or (scan.get("run", {}).get("scope") or {}).get("fix_approval_policy"))
        execution = _enqueue_stage_batch(
            sid, "assess", "scan_assess",
            [{"scan_id": sid, "user": _owner(request),
              "include_lifecycle_flagged": include_lifecycle_flagged}],
            snapshot_id=snapshot_id, request_fingerprint=request_fingerprint,
            input_manifest_id=input_manifest_id)
        jid = execution["job_ids"][0]
        return {"scan_id": sid, "level": level, "job_id": jid, "workers": core.WORKERS,
                "worker_tier_alive": core.store.worker_tier_alive(),
                "phase": "assessing", "deferred": True,
                "snapshot_id": snapshot_id, "execution_id": execution["batch_id"],
                "reused": execution["reused"]}
    # Immediate model — the results views gate on assessed_at; stamp it + build the assess trace.
    core.store.mark_assessed(sid, _dt.datetime.now(_dt.timezone.utc).isoformat())
    snapshot_id, input_manifest_id = _sealed_stage_input(
        sid, "discover", core.store.stage_snapshot_id(sid))
    from assess_approval_intent import request_fingerprint as approval_fingerprint
    request_fingerprint = approval_fingerprint(level, include_lifecycle_flagged,
        approval_choice or (scan.get("run", {}).get("scope") or {}).get("fix_approval_policy"))
    execution = _enqueue_stage_batch(
        sid, "assess", "assess_trace", [{"scan_id": sid, "level": level}],
        snapshot_id=snapshot_id, request_fingerprint=request_fingerprint,
        input_manifest_id=input_manifest_id)
    return {"scan_id": sid, "level": level, "job_id": execution["job_ids"][0],
            "workers": core.WORKERS, "snapshot_id": snapshot_id,
            "execution_id": execution["batch_id"], "reused": execution["reused"]}


@router.get("/scans/{sid}/trace/session/data")
def session_trace_data(sid: str, request: Request):
    """Data source for the IN-APP session panel: the whole scan's Langfuse session (every file's
    trace) fetched server-side (lf.fetch_session) and returned as a PHI-safe list + rollup, so ACP
    renders the aggregate inline. This is the view that hung the Langfuse UI on large scans; in-app
    it is a plain, capped list ACP controls. Honest states like the file /data route:
      • {"status": "not_configured"} — tracing keys absent
      • {"status": "pending"}        — session not ingested yet
      • {"status": "ok", "session": …}
    Registered BEFORE the literal /trace/session and the /trace/{kind} wildcard (registration
    order), though its two-segment suffix already keeps it unambiguous. Public — read-only."""
    import lf as _lf
    owner = _owner(request)
    if core.store.get_scan(sid, owner=owner) is None:
        raise HTTPException(404, "scan not found")
    if not _lf.enabled():
        return {"status": "not_configured"}
    data = _lf.fetch_session(sid)
    if data is None:
        return {"status": "pending"}
    return {"status": "ok", "session": data}


@router.get("/scans/{sid}/trace/session")
def open_session(sid: str, request: Request):
    """'View this scan' target under file-centric tracing (see lf.file_trace): every file
    in this scan shares a Langfuse SESSION keyed by the scan id, so this is the
    replacement for the old single scan/assess/remediate trace chips. No ensure-exists
    polling needed — an empty/not-yet-ingested session renders as 'no traces yet' in
    Langfuse, not a 404, so this redirects immediately. Public — see core.is_public.
    Registered BEFORE /trace/{kind} below — that's a single-path-segment wildcard that
    would otherwise shadow this literal "session" path (FastAPI matches routes in
    registration order; the more specific route must come first)."""
    import lf as _lf
    owner = _owner(request)
    if core.store.get_scan(sid, owner=owner) is None:
        raise HTTPException(404, "scan not found")
    link = _lf.session_deep_link(sid)
    if not link:
        raise HTTPException(404, "tracing is not configured")
    return RedirectResponse(link, status_code=302)


@router.get("/scans/{sid}/trace/{kind}")
def open_trace(sid: str, request: Request, kind: str, level: str = Query("AA")):
    """Reliable 'View trace' target. Ensures the {kind} Langfuse trace for this scan
    exists, then 302s to its deep link — so the chips never land on a Not-Found. The
    assess trace is (re)built SYNCHRONOUSLY here on the API image when missing, which
    removes the async-job / worker-drift / early-return / ingestion-lag failure modes
    that made the direct deep-links flaky. Public (a plain <a> navigation target that
    only redirects to a Langfuse URL — see core.is_public)."""
    import time

    import lf as _lf
    if kind not in ("scan", "assess", "remediate"):
        raise HTTPException(404, "unknown trace kind")
    if core.store.get_scan(sid, owner=_owner(request)) is None:
        raise HTTPException(404, "scan not found")
    trace_id = sid if kind == "scan" else f"{sid}-{kind}"
    link = _lf.trace_deep_link(trace_id)
    if not link:
        raise HTTPException(404, "tracing is not configured")
    # Create the assess trace on the spot if Langfuse doesn't have it yet. (The scan and
    # remediate traces are written at their own lifecycle stage and can't be rebuilt
    # here, but the wait below still covers their ingestion lag.)
    if kind == "assess" and not _lf.trace_exists(trace_id):
        try:
            from handlers import ensure_assess_trace
            ensure_assess_trace(sid, level)
        except Exception:
            swallowed("routes.scans.open_trace: ensuring the assess trace exists failed")
    # Wait briefly for Langfuse ingestion (async after flush) so the detail view doesn't
    # 404 the instant we land on it — best-effort; redirect anyway after ~5s.
    for _ in range(8):
        if _lf.trace_exists(trace_id):
            break
        time.sleep(0.6)
    return RedirectResponse(link, status_code=302)


@router.get("/scans/{sid}/trace/{kind}/exists")
def trace_exists(sid: str, kind: str, request: Request):
    """Returns {available: bool} — whether the Langfuse trace for this scan exists.
    Used by the UI to grey out the trace chip for scans that have no trace yet
    (e.g. scans from before tracing was wired up). Public — no auth needed.
    Historical (kind-based) traces only — see /trace/file/{file} for the current,
    file-centric model."""
    import lf as _lf
    if core.store.get_scan(sid, owner=_owner(request)) is None:
        return {"available": False}
    if kind == "session":
        return {"available": _lf.session_exists(sid)}
    if kind not in ("scan", "assess", "remediate"):
        return {"available": False}
    trace_id = sid if kind == "scan" else f"{sid}-{kind}"
    return {"available": _lf.trace_exists(trace_id)}


# MUST be registered BEFORE open_file_trace below: that route's {filename:path} is a greedy
# catch-all (`.*`) that also matches ".../{file}/data", and Starlette returns the first
# matching route — so a later /data route would be shadowed and 302 to Langfuse instead of
# serving JSON. Declaring it first lets the trailing literal /data win. (The same greedy match
# already shadows /exists above; that route is best-effort and the SPA tolerates it, so it is
# left as-is rather than reordered in this change.)
@router.get("/scans/{sid}/trace/file/{filename:path}/data")
def file_trace_data(sid: str, filename: str, request: Request, level: str = Query("AA")):
    """Data source for the IN-APP trace panel: the file's Langfuse trace fetched
    server-side (lf.fetch_trace) and returned as PHI-safe JSON, so ACP renders it
    inline with no Langfuse login. Langfuse's own trace page hangs for logged-out
    users on our self-hosted v3 and sends X-Frame-Options: SAMEORIGIN, so it can be
    neither linked-to nor iframed — this is the replacement.

    Honest states, never a 500 for a missing trace:
      • {"status": "not_configured"} — tracing keys absent
      • {"status": "pending"}        — trace not ingested yet (async after flush)
      • {"status": "ok", "trace": …} — the normalized trace
    Public — read-only apart from the same best-effort assess-trace rebuild the
    redirect route does, so a never-opened Assess trace still materializes."""
    import lf as _lf
    if core.store.get_scan(sid, owner=_owner(request)) is None:
        raise HTTPException(404, "scan not found")
    if not _lf.enabled():
        return {"status": "not_configured"}
    trace_id = f"{sid}::{filename}"
    data = _lf.fetch_trace(trace_id)
    if data is None:
        try:
            from handlers import ensure_assess_trace
            ensure_assess_trace(sid, level)
        except Exception:
            swallowed("routes.scans.file_trace_data: ensuring the assess trace exists for the file "
                      "trace data failed")
        import time
        for _ in range(5):
            time.sleep(0.6)
            data = _lf.fetch_trace(trace_id)
            if data is not None:
                break
    if data is None:
        return {"status": "pending"}
    return {"status": "ok", "trace": data}


# MUST be registered BEFORE open_file_trace (its {filename:path} is a greedy catch-all that also
# matches ".../{file}/history"). `filename` here is the trace-facing document LABEL (what the trace
# panel holds as `document`), not a raw filename — lf.fetch_document_history uses it as-is.
@router.get("/scans/{sid}/trace/file/{filename:path}/history")
def file_trace_history(sid: str, filename: str, request: Request):
    """CROSS-SCAN history for one document: its trace in every scan it appears in, newest first —
    the "this document over time" view Langfuse's own session-grouped UI cannot give. Honest states
    like the /data route: {status: not_configured | pending | ok}. Public — read-only."""
    import lf as _lf
    owner = _owner(request)
    if core.store.get_scan(sid, owner=owner) is None:
        raise HTTPException(404, "scan not found")
    if not _lf.enabled():
        return {"status": "not_configured"}
    data = _lf.fetch_document_history(filename, owner_key=_lf._owner_key(owner))
    if data is None:
        return {"status": "pending"}
    return {"status": "ok", "history": data}


@router.get("/scans/{sid}/trace/file/{filename:path}")
def open_file_trace(sid: str, filename: str, request: Request, level: str = Query("AA")):
    """Reliable 'View trace' target for ONE file — its Discover/Assess/Remediate spans
    all live on this single trace (file-centric tracing). Ensures it exists (re-running
    the Assess write for this file if Langfuse doesn't have it yet — the same
    synchronous-rebuild approach as the old per-scan endpoint) then 302s to its deep
    link. Public — see core.is_public."""
    import time

    import lf as _lf
    if core.store.get_scan(sid, owner=_owner(request)) is None:
        raise HTTPException(404, "scan not found")
    trace_id = f"{sid}::{filename}"
    link = _lf.trace_deep_link(trace_id)
    if not link:
        raise HTTPException(404, "tracing is not configured")
    if not _lf.trace_exists(trace_id):
        try:
            from handlers import ensure_assess_trace
            ensure_assess_trace(sid, level)
        except Exception:
            swallowed("routes.scans.open_file_trace: ensuring the assess trace exists for the open "
                      "file trace failed")
    for _ in range(8):
        if _lf.trace_exists(trace_id):
            break
        time.sleep(0.6)
    return RedirectResponse(link, status_code=302)


@router.get("/scans/{sid}/trace/file/{filename:path}/exists")
def file_trace_exists(sid: str, filename: str, request: Request):
    """Returns {available: bool} for one file's trace — the file-centric counterpart to
    /trace/{kind}/exists above. Public — no auth needed."""
    import lf as _lf
    if core.store.get_scan(sid, owner=_owner(request)) is None:
        return {"available": False}
    return {"available": _lf.trace_exists(f"{sid}::{filename}")}


# ── Per-scan decision snapshots (PRD: time-travel) ────────────────────────────
@router.get("/scans/{sid}/decisions")
def get_decisions(sid: str, request: Request):
    """All persisted decisions for a scan — used to restore state on time-travel."""
    owner = _owner(request)
    if core.store.get_scan(sid, owner=owner) is None:
        raise HTTPException(404, "scan not found")
    return core.store.get_decisions(sid, owner=owner)


@router.put("/scans/{sid}/decisions")
def put_decisions_batch(sid: str, request: Request, body: dict):
    """Batch upsert/delete decisions — body {items: [{file, kind, value}]} (value=null deletes).
    Used by the save effect so a bulk triage is one request, not one-per-file."""
    import datetime as _dt
    import json as _json
    owner = _owner(request)
    if core.store.get_scan(sid, owner=owner) is None:
        raise HTTPException(404, "scan not found")
    when = _dt.datetime.now(_dt.timezone.utc).isoformat()
    n = 0
    for it in (body.get("items") or []):
        file, kind, value = it.get("file"), it.get("kind"), it.get("value")
        if not file or kind not in ("triage", "action", "assignee", "due_date"):
            continue
        if value is None:
            core.store.delete_decision(sid, file, kind)
        else:
            core.store.save_decision(sid, file, kind,
                                     value if isinstance(value, str) else _json.dumps(value), owner, when)
        n += 1
    return {"ok": True, "saved": n}


@router.put("/scans/{sid}/decisions/{filename:path}")
def put_decision(sid: str, filename: str, request: Request, body: dict,
                 kind: str = Query("triage", pattern="^(triage|action|assignee|due_date)$")):
    """Upsert one decision for a file on this scan. body {value}: a string (triage:
    inscope|na|defer; assignee: the assignee's email; due_date: an ISO date, R19) or an object
    (action: {state, action}); value=null deletes it."""
    import datetime as _dt
    import json as _json
    owner = _owner(request)
    if core.store.get_scan(sid, owner=owner) is None:
        raise HTTPException(404, "scan not found")
    value = body.get("value")
    if value is None:
        core.store.delete_decision(sid, filename, kind)
        return {"ok": True, "deleted": True}
    val = value if isinstance(value, str) else _json.dumps(value)
    core.store.save_decision(sid, filename, kind, val, owner,
                             _dt.datetime.now(_dt.timezone.utc).isoformat())
    return {"ok": True}


@router.get("/scans/{sid}/artifacts")
def scan_artifacts(sid: str, request: Request):
    """Every remediation artifact this scan produced, and where each one lives (PRD §12, §20.5).

    WHY THIS IS NOT `remediation-status`. That route answers "how many corrected documents are
    there" and this one answers "does the authoritative copy of each survive the pod that wrote
    it" — acceptance criterion §20.5, which no count can settle. A remediated file written only
    to a worker's own disk is produced, downloadable and correct until that pod is replaced, at
    which point it is silently gone and the scan still reports success.

    THE ENDPOINT IS ALLOWED TO REPORT BADLY. `ephemeral` lists the authoritative artifacts whose
    location is empty or on no durable scheme, and it is populated on any installation where
    object storage is unconfigured (`blob.upload_remediated` returns None and the writer falls
    back to Drive-only). That is a true statement about such an installation, and returning it is
    the entire point: the portable acceptance suite reads this route, and a version that quietly
    omitted the ephemeral rows would let every target pass §20.5 by construction.

    Owner-scoped, and the scoping is in SQL. These rows carry filenames and live links to
    remediated documents; `list_scan_artifacts` filters by owner in the query so a foreign row is
    never read into memory, the same posture `get_remediation_urls` takes for the same reason.

    Always 200 for a scan this caller owns, including one with no artifacts yet — an empty
    inventory is a fact, and 404 would make "no remediation has run" indistinguishable from "no
    such scan" to a client that can see neither.
    """
    import blob as _blob
    owner = _owner(request)
    if core.store.get_scan_head(sid, owner=owner) is None:
        raise HTTPException(404, "scan not found")
    items = core.store.list_scan_artifacts(sid, owner=owner)
    ephemeral = [a["file"] for a in items if a["authoritative"] and not a["durable"]]
    return {
        "scan_id": sid,
        "artifacts": items,
        "count": len(items),
        # Named at the top level so a monitor can alert on it without walking the list, and so the
        # answer to §20.5 is a field rather than something each reader re-derives.
        "ephemeral": ephemeral,
        "all_durable": not ephemeral,
        "durable_schemes": sorted(core.store.DURABLE_ARTIFACT_SCHEMES),
        # WHETHER THIS INSTALLATION COULD STORE ONE AT ALL, which is the difference between two
        # readings of an empty inventory that look identical and mean opposite things: remediation
        # produced nothing (a defect), or nothing here is configured to keep what it produced (a
        # deployment fact). Without this field a caller sees zero artifacts and cannot tell which,
        # and §20.5 is exactly the criterion that must not be answered by guessing.
        "object_storage_configured": _blob.enabled(),
    }


@router.get("/scans/{sid}/remediation-status")
def remediation_status(sid: str, request: Request, response: Response):
    """Live remediation progress (in-flight jobs + latest fixed file) for the bar.
    Owner-scoped — latest_file could otherwise leak another user's filename by scan id.

    Also carries `activity`: the one line naming the file, the criterion and the action currently
    in flight. The counts answer "how much is left"; a queue depth of 3 tells a user nothing about
    whether their document is being OCR'd, waiting on a vision model, or stuck. Served from the
    same poll rather than a new endpoint, so the bar that already exists gains a line without the
    UI gaining a second timer."""
    if core.store.get_scan(sid, owner=_owner(request)) is None:
        raise HTTPException(404, "scan not found")
    import activity
    out = core.store.remediation_status(sid)
    out["activity"] = activity.current(sid, stage="remediate")
    out["workers"] = {"active": int(out.get("running") or 0),
                      "capacity": int(getattr(core, "WORKERS", 0) or 0)}
    # Live queue depth must never be served from the browser cache; every completed remediation
    # job reduces in_flight and drives the visible completed count.
    response.headers["Cache-Control"] = "no-store"
    return out


def _remediation_snapshot(sid: str) -> dict:
    """The revisioned run snapshot for one scan — facts from the store, judgement from the pure
    module. One function so the poll route and the SSE stream cannot disagree about either.

    `policy_version` and `execution_mode` are absent, not blank: ACP records no version for the
    remediation lane table and has no per-run execution mode to read. build_snapshot carries them
    through as None, and the panel renders nothing for a fact it has not been told — the same rule
    RemediationRunHeader already applies to its counts.
    """
    import remediation_run
    facts = core.store.remediation_run_facts(sid)
    import progress_evidence
    import assessment_blocked
    return {**remediation_run.build_snapshot(facts),
            **progress_evidence.read(core.store, facts.get('batch_id')),
            'assessment_blocked_files': assessment_blocked.read(core.store, sid)}


@router.get("/scans/{sid}/remediation/snapshot")
def remediation_snapshot(sid: str, request: Request, response: Response):
    """One reconciled, revisioned account of this remediation run (PRD §8).

    Owner-scoped for the same reason `remediation-status` is: filenames and SharePoint site paths
    are in here, and a scan id must not work as a cross-account oracle.

    This does NOT replace `remediation-status`, which still feeds the shipped progress bar. It
    answers the different question that endpoint never could — what state is this run in, and do
    its numbers reconcile — and it answers it on the server, because every attempt to assemble it
    in the browser produced a screen whose parts contradicted each other.
    """
    if core.store.get_scan(sid, owner=_owner(request)) is None:
        raise HTTPException(404, "scan not found")
    response.headers["Cache-Control"] = "no-store"
    return _remediation_snapshot(sid)


@router.get("/scans/{sid}/remediation/automation-policy")
def remediation_automation_policy(sid: str, request: Request):
    """Saved future-run policy plus the truthful active-run action capability."""
    if core.store.get_scan(sid, owner=_owner(request)) is None:
        raise HTTPException(404, "scan not found")
    import remediation_automation_policy as policy
    return policy.read(core.store, _owner(request), scan_id=sid)


@router.post("/scans/{sid}/remediation/automation-policy/actions")
async def remediation_automation_policy_action(sid: str, request: Request):
    """Route one explicit preview action; slider movement never reaches this endpoint."""
    owner = _owner(request)
    if core.store.get_scan(sid, owner=owner) is None:
        raise HTTPException(404, "scan not found")
    key = (request.headers.get("idempotency-key") or "").strip()
    if not (16 <= len(key) <= 128):
        raise HTTPException(400, "Idempotency-Key must contain 16 to 128 characters")
    import remediation_automation_policy as policy
    try:
        body = await request.json()
        return policy.execute(core.store, owner, owner, action=body.get("action"),
                              level=body.get("level"),
                              expected_revision=body.get("expected_revision"),
                              idempotency_key=key)
    except policy.PolicyConflict as exc:
        raise HTTPException(409, detail={"code": "stale_policy_revision",
                                        "message": "The policy changed. Review the latest setting and try again.",
                                        "current": exc.current}) from exc
    except policy.IdempotencyConflict as exc:
        raise HTTPException(409, detail={"code": "idempotency_key_reused",
                                        "message": "That request key was already used for a different action."}) from exc
    except policy.ActiveRunUnsupported as exc:
        raise HTTPException(409, detail={"code": "active_run_routing_unsupported",
                                        "message": "This run cannot be safely re-routed after it starts."}) from exc
    except (TypeError, ValueError, AttributeError) as exc:
        raise HTTPException(400, str(exc)) from exc


# ── Exceptions and scoped recovery (PRD §6E, §11) ─────────────────────────────
#
# The panel's exception region is the one place a user ACTS on a run rather than reading it, so
# every decision it renders is made here and on the server. `remediation_exceptions` is pure and
# holds the rules; these routes hold the gate (owner, capability, ownership of the run) and the
# durable side effects. Nothing below trusts a filename the client sent as authorisation for
# anything: the request names WHICH documents to act on, and the server decides whether each one
# is eligible — which is what makes "group actions affect only visible/selected eligible items"
# true even when the client is wrong or hostile.

def _exception_view(sid: str) -> dict:
    """Grouped exceptions, run controls, and the delivery history behind them — one read.

    Composed from the SAME facts the snapshot is built from, so the exception counts and the
    counters cannot disagree: `remediation_run.classify_document` decides each document's outcome
    once, and both the partition and this grouping are derived from that one answer.

    Returns `(cancelled, view, records)` — the flag first because every mutating caller needs it
    before it needs either of the other two.
    """
    import remediation_run
    import remediation_exceptions as exceptions
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    run_facts = core.store.remediation_run_facts(sid)
    snapshot = remediation_run.build_snapshot(run_facts, now=now)
    facts = core.store.remediation_exception_facts(sid)

    review_docs = set(run_facts.get("review_documents") or ())
    corrected = set(run_facts.get("corrected_documents") or ())
    verified_docs = set(run_facts.get("verified_documents") or ())
    outcomes = {}
    for job in run_facts.get("jobs") or ():
        file = job.get("file")
        outcomes[file] = remediation_run.classify_document(
            job, now=now, review_pending=file in review_docs,
            has_correction=file in corrected, has_verified_fix=file in verified_docs)

    records = exceptions.compose_records(outcomes=outcomes, documents=facts.get("documents"),
                                         run_id=sid)
    # Cancel REQUESTED counts, not only cancelled: a run that is stopping must not accept a new
    # write to the customer's estate while it drains.
    cancelled = bool(run_facts.get("cancelled") or run_facts.get("cancel_requested"))
    return cancelled, {
        "run_id": sid,
        "generated_at": now.isoformat(),
        "revision": snapshot.get("revision"),
        "state": snapshot.get("state"),
        "provider": facts.get("provider"),
        "groups": exceptions.build_exception_groups(records, cancelled=cancelled),
        # The counterpart to the groups. Region E's question is "does anyone need to act?", and
        # "no, and here are the corrected copies it produced" is one of its two honest answers.
        "completed": exceptions.completed_outcomes(records),
        "completed_documents": sum(1 for r in records if r.get("outcome") == "completed"
                                   and r.get("artifact_stored_at")),
        "controls": exceptions.run_controls(
            state=snapshot.get("state"), counters=snapshot.get("documents"),
            paused=bool(run_facts.get("paused")),
            cancel_requested=bool(run_facts.get("cancel_requested")),
            cancelled=bool(run_facts.get("cancelled")),
            terminal=bool(snapshot.get("terminal"))),
        "paused_at": facts.get("paused_at"),
    }, records


@router.get("/scans/{sid}/remediation/exceptions")
def remediation_exceptions_view(sid: str, request: Request, response: Response):
    """What still needs a decision or a retry on this run, grouped by the response it needs.

    Owner-scoped like every other per-scan read, and for the sharper reason here: the rows carry
    filenames AND the provider container each corrected copy would be written to. A scan id must
    not work as a cross-account oracle for either.
    """
    if core.store.get_scan(sid, owner=_owner(request)) is None:
        raise HTTPException(404, "scan not found")
    response.headers["Cache-Control"] = "no-store"
    _cancelled, view, _records = _exception_view(sid)
    return view


async def _requested_files(request: Request) -> list[str] | None:
    """The `files` list from a request body, or None when the caller sent none.

    None and [] are DIFFERENT and both are honoured as sent. An explicit empty list is "act on
    nothing", which is what a group action fires with when the user has deselected everything,
    and turning it into "act on all" would be the worst possible reading of an empty selection.
    """
    try:
        body = await request.json()
    except Exception:
        swallowed("routes.scans._requested_files: reading the request body failed")
        return None
    if not isinstance(body, dict):
        return None
    files = body.get("files")
    if files is None:
        return None
    if not isinstance(files, list):
        raise HTTPException(400, "files must be a list of document names")
    return [str(f) for f in files]


def _selected(records: list[dict], files: list[str] | None) -> list[dict]:
    """The requested documents, in the run's own order, with unknown names dropped.

    A name the run does not contain is not an error and not an action: it is dropped and reported
    as such by the caller's count. Raising would let one stale row in a browser fail a group
    action over eleven good ones.
    """
    if files is None:
        return list(records)
    wanted = set(files)
    return [record for record in records if record.get("file") in wanted]


@router.post("/scans/{sid}/remediation/exceptions/retry-delivery")
async def retry_remediation_delivery(sid: str, request: Request):
    """Re-send already-verified corrected copies to the source provider. Fixes nothing.

    ONE DELIVERY OPERATION PER (run, document, destination, artifact), enforced by the primary key
    `store.claim_delivery` inserts on. A double-click, a retried request, or two operators pressing
    the button at the same second all compute the same idempotency key and produce one job; the
    losers come back as `duplicate` rather than as an error, because from the user's point of view
    the delivery they asked for IS in progress.

    EVERY DOCUMENT GETS ITS OWN OUTCOME. A group that starts nine, refuses two and fails one is
    reported as exactly that — see remediation_exceptions.summarize_outcomes, whose `summary` never
    says the group succeeded unless every member did.
    """
    if core.store.get_scan(sid, owner=_owner(request)) is None:
        raise HTTPException(404, "scan not found")
    import remediation_exceptions as exceptions

    files = await _requested_files(request)
    # ONE read of the run, shared by the gate and the grouping. Two reads would let the cancel flag
    # and the eligible set come from different instants — the class of split-read defect
    # store.remediation_run_facts exists to close.
    cancelled, _view, records = _exception_view(sid)
    owner = _owner(request)
    # Tokens travel with the job exactly as they do for a remediation batch: the worker tier has
    # no session, and a delivery to a customer's tenant needs the caller's grant, not ACP's.
    drive_token = request.headers.get("x-drive-token")
    sp_token = request.headers.get("x-sp-token")

    results = []
    for record in _selected(records, files):
        file = record.get("file")
        # NO in_flight PRE-CHECK HERE. `store.claim_delivery` is the authority — the
        # idempotency key is that table's primary key, so a duplicate is settled by the INSERT
        # rather than by a read this request took first and hoped nobody raced. Refusing here on
        # a row read moments ago would report `refused` for what is really the user's own second
        # press, and would still lose the race it was trying to win.
        decision = exceptions.delivery_retry_decision(record, cancelled=cancelled)
        if not decision["eligible"]:
            results.append({"file": file, "outcome": "refused", "code": decision["code"],
                            "message": decision["message"]})
            continue
        key = decision["idempotency_key"]
        claim = core.store.claim_delivery(
            sid, file, idempotency_key=key,
            destination_provider=decision["destination"]["provider"],
            destination_key=decision["destination"]["key"],
            artifact_digest=record.get("artifact_digest"), actor=owner)
        if not claim["claimed"]:
            # Already claimed. `delivered` is reported as a duplicate rather than as a fresh
            # success: this request did not deliver anything, and saying it did would credit it
            # with somebody else's write.
            results.append({"file": file, "outcome": "duplicate",
                            "code": claim["status"], "idempotency_key": key,
                            "message": exceptions.refusal_message("retry_in_flight")
                                       if claim["status"] == "in_flight" else None})
            continue
        payload = {"scan_id": sid, "file": file, "owner": owner, "actor": owner,
                   "idempotency_key": key, "artifact_digest": record.get("artifact_digest"),
                   "destination": decision["destination"],
                   "provider": decision["destination"]["provider"],
                   "drive_token": drive_token, "sp_token": sp_token}
        try:
            job_id = core.store.enqueue_job("deliver_corrected_copy", payload, scan_id=sid,
                                            max_attempts=3)
        except Exception as exc:      # noqa: BLE001 — an unqueued claim would wedge the document
            core.store.reopen_delivery(key)
            results.append({"file": file, "outcome": "failed",
                            "code": "enqueue_failed", "message": str(exc)[:200]})
            continue
        _remediation_action_event(sid, "remediate.delivery_retry_requested", file,
                                  actor=owner, destination=decision["destination"],
                                  idempotency_key=key, action="retry_delivery",
                                  outcome="requested")
        results.append({"file": file, "outcome": "started", "job_id": job_id,
                        "idempotency_key": key,
                        "destination": {"provider": decision["destination"]["provider"],
                                        "label": record.get("destination_label")}})
    return exceptions.summarize_outcomes(results)


@router.post("/scans/{sid}/remediation/exceptions/retry-documents")
async def retry_remediation_documents(sid: str, request: Request):
    """Re-run remediation for failed or unverified documents. This DOES re-apply fixes.

    Deliberately a different route, a different verb in the UI, and a different sentence in the
    group header from the delivery retry beside it. They look similar and are not: this one opens
    the source document again and can change what the run claims about it, which is why it is
    offered only for the two groups whose remedy actually is another attempt.
    """
    if core.store.get_scan(sid, owner=_owner(request)) is None:
        raise HTTPException(404, "scan not found")
    import remediation_exceptions as exceptions

    files = await _requested_files(request)
    _cancelled, _view, records = _exception_view(sid)
    owner = _owner(request)
    retryable = {"document_failure", "verification_failure"}
    import assessment_blocked
    assessment_blocks = {row['file']: row for row in assessment_blocked.read(core.store, sid, owner=owner)}
    results = []
    for record in _selected(records, files):
        file = record.get("file")
        if file in assessment_blocks:
            results.append({'file': file, 'outcome': 'refused', 'code': 'assessment_blocked',
                            'message': assessment_blocks[file]['reason']})
            continue
        classified = exceptions.classify_exception(record)
        if not classified or classified[0] not in retryable:
            results.append({"file": file, "outcome": "refused", "code": "not_retryable",
                            "message": "This document is not a failed or unverified document."})
            continue
        payload = core.store.latest_remediation_payload(sid, file)
        if not payload:
            results.append({"file": file, "outcome": "refused", "code": "no_prior_attempt",
                            "message": "ACP has no record of how this document was remediated, "
                                       "so it cannot repeat the attempt."})
            continue
        try:
            job_id = core.store.enqueue_job("remediate_file", payload, scan_id=sid,
                                            batch_id=_facts_batch(sid))
        except Exception as exc:      # noqa: BLE001
            results.append({"file": file, "outcome": "failed", "code": "enqueue_failed",
                            "message": str(exc)[:200]})
            continue
        core.store.log_decision(owner, "remediate.document_retry_requested", scan_id=sid,
                                file=file, detail=f"job {job_id}")
        results.append({"file": file, "outcome": "started", "job_id": job_id})
    return exceptions.summarize_outcomes(results)


def _facts_batch(sid: str) -> str | None:
    """The batch a retry should join — the one the panel is watching.

    Re-using it rather than minting a new one keeps the retried document inside the partition the
    counters are scoped to. A retry in its own batch would leave the run showing a failed document
    forever while a second, invisible batch quietly fixed it (store.remediation_status documents
    what unscoped counting cost the last time).
    """
    try:
        return core.store.remediation_run_facts(sid).get("batch_id")
    except Exception:
        swallowed("routes.scans._facts_batch: reading the run's batch failed", sid)
        return None


def _remediation_action_event(sid: str, kind: str, file: str | None, *, actor: str | None,
                              action: str, outcome: str, destination: dict | None = None,
                              idempotency_key: str | None = None,
                              reason: str | None = None) -> None:
    """One human action on a run, in the durable log AND in the decision audit.

    BOTH, because they answer different questions. The scan event is the narrative a reconnecting
    panel replays; the decision row is the audit trail PRD §13 requires — actor, run, document,
    destination, action, outcome. Neither carries extracted document content: the payload is built
    by remediation_exceptions.audit_payload, which is a whitelist, and the destination reaches it
    as container identifiers rather than as a signed URL.
    """
    import handlers
    import remediation_exceptions as exceptions
    payload = exceptions.audit_payload(
        actor=actor, run_id=sid, file=file, action=action, outcome=outcome,
        destination=destination, idempotency_key=idempotency_key, reason=reason)
    # `document` is a COLUMN (ADR 0052), and `actor` is in neither the column nor the detail: the
    # log is replayed to every authorised viewer of the run, so naming who pressed the button
    # would put one user's identity on another's screen. The audit row below carries it, and that
    # read is owner-scoped.
    detail = {k: v for k, v in payload.items() if k not in ("actor", "file")}
    try:
        handlers.scan_event(sid, kind, document=file or None, detail=detail or None)
    except Exception:
        swallowed("routes.scans._remediation_action_event: recording the lifecycle event failed", sid)
    try:
        core.store.log_decision(actor or "system", kind, scan_id=sid, file=file,
                                detail=_json.dumps(payload, sort_keys=True)[:400])
    except Exception:
        swallowed("routes.scans._remediation_action_event: recording the audit row failed", sid)


@router.post("/scans/{sid}/remediation/cancel")
def cancel_remediation_run(sid: str, request: Request):
    """Stop this run's outstanding remediation work. Corrected copies already made are kept.

    Distinct from `POST /scans/{sid}/cancel`, which ends the SCAN. This one touches only
    `remediate_file` jobs, so a cancelled remediation leaves the assessment, its findings and
    every verified correction exactly where they are — which is what makes it safe to offer beside
    a live run.
    """
    if core.store.get_scan(sid, owner=_owner(request)) is None:
        raise HTTPException(404, "scan not found")
    asked = core.store.request_remediation_cancel(sid)
    _remediation_action_event(sid, "remediate.cancel_requested", None, actor=_owner(request),
                              action="cancel", outcome="requested",
                              reason=f"{asked} outstanding")
    return {"run_id": sid, "cancel_requested": True, "documents_asked_to_stop": asked}


@router.post("/scans/{sid}/remediation/pause")
def pause_remediation_run(sid: str, request: Request):
    """Hold this run's UNCLAIMED work. Attempts already in flight run to completion.

    The limit is in the response, not only in the documentation: `in_flight` says how many
    documents this pause does NOT stop, so a caller can state it rather than implying a full stop
    the backend cannot deliver.
    """
    if core.store.get_scan(sid, owner=_owner(request)) is None:
        raise HTTPException(404, "scan not found")
    snapshot = _remediation_snapshot(sid)
    in_flight = int((snapshot.get("documents") or {}).get("processing") or 0)
    result = core.store.pause_remediation_run(sid, actor=_owner(request))
    _remediation_action_event(sid, "remediate.paused", None, actor=_owner(request),
                              action="pause", outcome="paused",
                              reason=f"{result['held']} held, {in_flight} in flight")
    return {"run_id": sid, "paused": True, "held": result["held"], "in_flight": in_flight,
            "paused_at": result["paused_at"]}


@router.post("/scans/{sid}/remediation/resume")
def resume_remediation_run(sid: str, request: Request):
    """Release the hold and return the deferred documents to the queue."""
    if core.store.get_scan(sid, owner=_owner(request)) is None:
        raise HTTPException(404, "scan not found")
    result = core.store.resume_remediation_run(sid, actor=_owner(request))
    _remediation_action_event(sid, "remediate.resumed", None, actor=_owner(request),
                              action="resume", outcome="resumed",
                              reason=f"{result['released']} released")
    return {"run_id": sid, "paused": False, "released": result["released"],
            "resumed_at": result["resumed_at"]}


def _resume_plan(sid: str, raw_cursor: str | None) -> tuple[int | None, str | None]:
    """Decide what a reconnecting client gets: (after_seq to replay from, reconcile reason).

    Exactly one of the two is ever set. ADR 0051's three reconcile conditions live here, in a pure
    function, so they can be tested without a stream.

    No cursor at all is NOT a reconcile — it is a first connection, and it gets the live loop with
    no replay, exactly as every client gets today.
    """
    if raw_cursor is None or raw_cursor.strip() == "":
        return (None, None)
    try:
        cursor = int(raw_cursor.strip())
    except (TypeError, ValueError):
        # A malformed cursor is a client bug. Reconciling is the safe answer; guessing at the
        # intended number is not.
        return (None, "malformed_cursor")
    if cursor < 0:
        return (None, "malformed_cursor")
    oldest, newest = core.store.scan_event_bounds(sid)
    if newest is None:
        # The scan has no events at all. A cursor against an empty log cannot be honoured, and
        # replaying nothing would read to the client as "caught up".
        return (None, "no_events")
    if cursor > newest:
        return (None, "cursor_ahead_of_log")
    if oldest is not None and oldest > cursor + 1:
        # Pruned past. Unreachable today (nothing prunes scan_events) and deliberately written
        # anyway — see ADR 0051 and store.scan_event_bounds.
        return (None, "events_pruned")
    return (cursor, None)


def _project_event(event: dict, sid: str, privacy: str, *, remapped: dict | None = None) -> dict:
    """One durable event as it goes on the wire: structured, correlated, and privacy-checked.

    THE PROJECTION IS THE ENFORCEMENT POINT, and there is exactly one of it. PRD §22 lets a
    deployment suppress document names; a rule applied at each of several read paths is a rule
    that eventually gets applied at all but one of them, so both the stream and
    `GET /scans/{sid}/events` come through here.

    Suppression removes the NAME and keeps the IDENTITY. `document_ref` is a per-run,
    non-reversible handle (see remediation_run.document_ref), so a client can still keep three
    parallel documents apart and each one's history in order — which is what PRD §6D actually
    needs the name for — without ever being told what any of them is called.

    `detail` is scrubbed of any `file`/`filename`/`path` key under suppression as well. Nothing
    writes those today — `_rem_event` puts the name in the column and nowhere else — and the
    scrub is here because "nothing writes it today" is a fact about today, and the cost of being
    wrong about it is a disclosure that cannot be taken back.
    """
    import remediation_run
    import remediation_activity_history
    out = dict(event)
    out["document_ref"] = remediation_run.document_ref(sid, event.get("document"))
    out["material"] = core.store.is_material_event(event.get("kind"))
    # Contract V3: which part of the work this narrates (draft / review / document write /
    # verification / delivery / run). Set here, once, so every read path carries the same label.
    out["activity_stage"] = remediation_activity_history.activity_stage(event.get("kind"))
    # A read-time correction `_project_events` computed for a stored event whose recorded detail
    # was provably wrong (a historical obsolete retry written as a generic block). Applied BEFORE
    # the suppression scrub below, so the scrub sees the detail that actually ships.
    if remapped and event.get("seq") is not None and int(event["seq"]) in remapped:
        out["detail"] = dict(remapped[int(event["seq"])])
    if privacy == "suppressed":
        out["document"] = None
        out["document_suppressed"] = True
        detail = out.get("detail")
        if isinstance(detail, dict):
            out["detail"] = {k: v for k, v in detail.items()
                             if k not in ("file", "filename", "path", "source_path")}
    return out


def _project_events(events: list[dict], sid: str, privacy: str, *, store=None) -> list[dict]:
    """A PAGE of events through `_project_event`, with that page's read-time corrections.

    Every read path (stream replay, live tick, /history, /remediation/activity) projects through
    here, so the historical obsolete-retry mapping is decided once per page — one decision_log
    read, not one per event — and identically wherever the event is shown. The mapping reads the
    raw `document` column, so it runs before suppression blanks it; it never raises.
    """
    import remediation_activity_history
    remapped = remediation_activity_history.historical_retry_projections(
        store or core.store, sid, events)
    return [_project_event(event, sid, privacy, remapped=remapped) for event in events]


def _stream_is_finished(out: dict) -> bool:
    """Whether the remediation stream may close (PRD §21, ADR 0052's second open question).

    IT USED TO CLOSE ON `in_flight == 0`, which is a statement about JOBS, not about the run. A
    run whose last document finished still owes corrected-copy delivery and final reconciliation,
    and the snapshot has always said so — `completing` / "delivery_reconciliation_outstanding" is
    a real state it can be in with zero jobs in flight. Closing there handed the client a `done`
    for work that was not done, and left the remaining transitions to the fallback poll.

    So the stream now closes on the SNAPSHOT'S OWN judgement: terminal, with no delivery pending.
    Those are the same words `derive_run_state` uses, which is the point — one definition of
    finished, not a second one implied by a queue depth.

    IT STAYS OPEN FOR RECONCILIATION, NOT FOR A HUMAN. `completing` is a state the run leaves on
    its own — the corrected copies are being written and the run is reconciling itself — so a
    client watching it will see the transition. `needs_attention` is not: it waits on a review
    decision that may be hours away, and holding a connection open for that would be a leak
    dressed up as liveness. A run parked in review closes exactly as it does today and the
    client's fallback poll takes it from there.

    `paused` (added by #1474 after this rule shipped) is the SAME CATEGORY reached by a different
    mechanism, and it was being treated as the opposite — see the operator-hold clause below for
    why the queue depth made it so. Both now close; the wait being on a person is what decides
    it, not how the queue happens to represent that wait.

    THIS CAN ONLY EVER EXTEND THE STREAM, never shorten it — with ONE deliberate exception, the
    operator hold below, which is that same rule applied to a state this function predates.

    NO SNAPSHOT MEANS FALL BACK TO `in_flight`, never to "finished". The snapshot build is
    wrapped in a try/except above precisely so a stream failure cannot take the stream down; if
    that swallowed, this must degrade to the behaviour that shipped rather than assert completion
    it has no evidence for.
    """
    snapshot = out.get("snapshot")
    # AN OPERATOR HOLD IS A WAIT ON A HUMAN, so it closes for the same reason `needs_attention`
    # does — and it has to be decided BEFORE the `in_flight` clause, because that clause is what
    # made pause the exception to the rule. `store` defers a held run's queued jobs to a
    # year-9999 sentinel rather than removing them, so `in_flight` (queued + running) stays
    # positive and the stream was held open for up to the iteration cap against work that
    # definitionally cannot be claimed while the hold stands.
    #
    # `processing` is what distinguishes the two halves of a pause. Attempts already claimed keep
    # draining after the hold (that is why `paused` is derived only while there is work a hold
    # could be holding), and cutting the stream mid-drain would drop live frames from real work.
    # So: close once nothing is actually running, and only then.
    if isinstance(snapshot, dict) and snapshot.get("state") == "paused":
        documents = snapshot.get("documents")
        processing = documents.get("processing") if isinstance(documents, dict) else None
        # An unknown count is not zero, here as everywhere else in this function.
        if processing is not None and int(processing) <= 0:
            return True
        return False
    if out.get("in_flight"):
        return False
    if not isinstance(snapshot, dict):
        return True
    # `completing` reads exactly "delivery_reconciliation_outstanding". Checked in `also` as well
    # as in `state`, because precedence displays the more severe headline: a run that owes both a
    # review decision and a delivery shows `needs_attention` and carries `completing` alongside.
    if "completing" in {snapshot.get("state"), *(snapshot.get("also") or ())}:
        return False
    delivery = snapshot.get("delivery")
    pending = delivery.get("pending") if isinstance(delivery, dict) else None
    # An unknown pending count is not zero. Staying open costs a connection until the iteration
    # cap; closing on an unknown would tell the client delivery finished when nothing said so.
    return pending is not None and int(pending) <= 0


@router.get("/scans/{sid}/remediation/stream")
async def stream_remediation_status(sid: str, request: Request):
    """Push the owner-scoped remediation status whenever it changes.

    This is the Remediate counterpart to the Discover stream.  It deliberately reads the same
    store method as ``remediation_status`` so the pushed and fallback-poll views cannot disagree.
    The browser consumes it with authenticated fetch (not EventSource, which cannot carry ACP's
    bearer header).  A final ``done`` event closes the connection once the batch drains.

    RESUME (ADR 0051). A client that sends ``Last-Event-ID`` gets the ``scan_events`` it missed,
    oldest first, each carrying its own SSE ``id:``, BEFORE any live frame — then the ordinary
    loop. A client that sends nothing gets exactly the stream it gets today; the snapshot frame is
    unchanged and untyped, so nothing that consumes it needs to know this exists.

    The header is read explicitly rather than inherited from the browser: ``Last-Event-ID`` is
    automatic only for ``EventSource``, which ACP cannot use (it cannot carry the bearer token,
    and putting the token in the URL would leak it into proxy logs and history). The name is kept
    because the concept is the same one.
    """
    import asyncio
    import json as _json
    import activity

    owner = _owner(request)
    if core.store.get_scan(sid, owner=owner) is None:
        raise HTTPException(404, "scan not found")

    after_seq, reconcile = _resume_plan(sid, request.headers.get("Last-Event-ID"))
    # Resolved ONCE, at connect, and applied to every frame this connection emits. Re-reading it
    # per frame would let a policy change mid-stream produce a connection that disclosed names in
    # its first half — the change takes effect on the next connect, which is a bounded window and
    # a legible rule.
    privacy = core.store.remediation_filename_privacy(sid)

    async def _gen():
        last = None
        idle = 0
        # Replay first, so a resumed client sees the history it missed in order and only then the
        # present. A reconcile reason skips replay entirely: the client must re-fetch a snapshot
        # before it applies anything later (PRD §17.6).
        if reconcile:
            yield ("event: reconciliation-required\n"
                   f"data: {_json.dumps({'reason': reconcile})}\n\n")
            cursor = (await asyncio.to_thread(core.store.scan_event_bounds, sid))[1]
        elif after_seq is not None:
            missed = await asyncio.to_thread(core.store.list_scan_events, sid,
                                             after_seq=after_seq)
            projected = (await asyncio.to_thread(_project_events, missed, sid, privacy)
                         if missed else [])
            for event in projected:
                # `id:` is what makes this resumable at all — the client stores the last one it
                # rendered and sends it back on the next connect.
                yield (f"id: {event['seq']}\n"
                       "event: remediation-event\n"
                       f"data: {_json.dumps(event, default=str)}\n\n")
            cursor = missed[-1]["seq"] if missed else after_seq
        else:
            # FIRST CONNECT, no cursor. Start from the newest event rather than 0: this client has
            # not missed anything, and replaying a finished run's whole history to a browser that
            # just opened the tab is not a resume, it is a backfill nobody asked for. It still
            # gets live frames from here, which is how it earns a cursor for its NEXT reconnect.
            cursor = (await asyncio.to_thread(core.store.scan_event_bounds, sid))[1]

        for _ in range(_MAX_STREAM_ITERS):
            if await request.is_disconnected():
                return
            # New events since the last tick, each with its id, so a CONNECTED client's cursor
            # advances too — otherwise it could only ever resume from where it first connected.
            # One indexed read (scan_id, seq) beside two much heavier ones already in this loop.
            if cursor is not None:
                fresh = await asyncio.to_thread(core.store.list_scan_events, sid,
                                                after_seq=cursor)
                projected = (await asyncio.to_thread(_project_events, fresh, sid, privacy)
                             if fresh else [])
                for event in projected:
                    yield (f"id: {event['seq']}\n"
                           "event: remediation-event\n"
                           f"data: {_json.dumps(event, default=str)}"
                           "\n\n")
                if fresh:
                    cursor = fresh[-1]["seq"]

            out = await asyncio.to_thread(core.store.remediation_status, sid)
            out["activity"] = activity.current(sid, stage="remediate")
            out["workers"] = {"active": int(out.get("running") or 0),
                              "capacity": int(getattr(core, "WORKERS", 0) or 0)}
            # The reconciled run snapshot rides the SAME frame as the legacy counts, so a client
            # can never render a state from one instant against counters from another. A stream
            # failure here must not take the stream down with it: the legacy payload is what the
            # shipped progress bar consumes, and it is still correct without this.
            try:
                out["snapshot"] = await asyncio.to_thread(_remediation_snapshot, sid)
            except Exception:
                swallowed("routes.scans.stream_remediation_status: building the run snapshot failed", sid)
                out.pop("snapshot", None)
            # generated_at moves every tick by construction, so comparing it would push a frame
            # per interval and defeat the change detection this loop exists for. The snapshot's
            # `revision` is the field that actually advances on a durable change — that is what
            # it is for — so the signature is taken over everything except the generation time.
            _sig_src = dict(out)
            if isinstance(_sig_src.get("snapshot"), dict):
                _sig_src["snapshot"] = {k: v for k, v in _sig_src["snapshot"].items()
                                        if k != "generated_at"}
            sig = _json.dumps(_sig_src, sort_keys=True, default=str)
            if sig != last:
                last = sig
                idle = 0
                yield f"data: {_json.dumps(out)}\n\n"
            else:
                idle += 1
                if idle >= _HEARTBEAT_EVERY:
                    idle = 0
                    yield ": keep-alive\n\n"
            # NOT `in_flight == 0` any more — see _stream_is_finished. Terminal document work is
            # not the end of the run while corrected copies are still being delivered, and the
            # client's `onDone` drives the batch's finalization, so ending early finalizes over
            # unfinished delivery.
            if _stream_is_finished(out):
                yield "event: done\ndata: {\"done\": true}\n\n"
                return
            await asyncio.sleep(_STREAM_INTERVAL_S)

    return StreamingResponse(_gen(), media_type="text/event-stream", headers={
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
        "Connection": "keep-alive",
    })


def _sp_freshness(run: dict, request: Request):
    """What has changed in this scan's SharePoint libraries since it listed them.

    Returns `(changed_by_key, removed_keys, error)`, or `(None, set(), None)` when this is not a
    SharePoint scan that can be asked — no recorded cursors (a scan from before this shipped, or
    one whose shape the delta query cannot serve), or no Microsoft token on the request. None is
    "cannot answer", and the caller renders those files `untracked`, which is the honest state
    and never a false `unchanged`.

    ONE GRAPH CALL PER LIBRARY. That is the whole design: Drive answers this per file, which on a
    thousands-of-documents estate is thousands of calls to render one screen, and is why
    SharePoint has never had a freshness answer at all rather than a slow one.

    The cursor is replayed and NOT saved. Advancing it here would move the scan's recorded
    position every time somebody opened the screen, so the second viewing would report "nothing
    changed" no matter what had — a read that quietly destroys the thing it reads.
    """
    if (run.get("source") != "sharepoint"):
        return None, set(), None
    scope = run.get("scope")
    if isinstance(scope, str):
        try:
            scope = _json.loads(scope)
        except Exception:  # noqa: BLE001
            scope = {}
    cursors = (scope or {}).get("sp_cursors") if isinstance(scope, dict) else None
    if not cursors:
        return None, set(), None
    token = request.headers.get("x-sp-token")
    if not token:
        return None, set(), None
    from scanner import sp_delta_since
    changed: dict = {}
    removed: set = set()
    for raw_drive, link in cursors.items():
        drive_id = raw_drive or None          # "" is the OneDrive/no-drive key (see handlers)
        try:
            items, gone, _ = sp_delta_since(token, drive_id, link)
        except Exception as e:  # noqa: BLE001 — one library's failure is not the screen's
            return None, set(), f"could not read changes for library {raw_drive or 'OneDrive'}: {e}"
        removed |= set(gone)
        for it in items:
            if it.get("id"):
                changed[(drive_id, it["id"])] = it.get("lastModifiedDateTime")
    return changed, removed, None


@router.get("/scans/{sid}/source-status")
def source_status(sid: str, request: Request):
    """Has each file's SOURCE changed since ACP scanned it, and — PRD Phase 3 — where does ACP's
    own side of the round trip stand: importing, failed, an unpublished fix, or the two sides
    actively disagreeing?

    Compares the source's CURRENT modifiedTime (fetched now with the caller's read-only Drive
    creds) to the baseline captured at scan time (file_records.source_modified), then layers
    ACP's import/publish state on top (source_staleness.classify_sync_state). Owner-scoped.

    A file with no baseline or no Drive id — and EVERY file when the scan's source isn't Drive —
    is 'untracked', never a false 'unchanged'. A source that 404s/403s is 'unavailable', not
    stale; one unreadable file never fails the batch. The Drive service is built lazily, so a scan
    with nothing trackable answers without needing a Drive token at all."""
    import source_staleness as _ss
    scan = core.store.get_scan(sid, owner=_owner(request))
    if scan is None:
        raise HTTPException(404, "scan not found")
    files = scan.get("files") or []
    run = scan.get("run") or {}
    run_status = run.get("status")
    source_is_drive = run.get("source") == "drive"
    # SHAREPOINT ANSWERS THIS A DIFFERENT WAY, and the difference is the whole reason it can
    # answer at all. Drive's answer is one metadata read per FILE; on a 30-site estate that is
    # thousands of Graph calls to render one screen. SharePoint has a delta cursor, so the same
    # question costs one call per LIBRARY — replayed from the position the scan itself recorded
    # (handlers._sp_scan_cursors), which is what makes it "changed since THIS scan" rather than
    # "changed since the last sync", a different question with an indistinguishable answer.
    sp_changed, sp_removed, sp_error = _sp_freshness(run, request)
    source_tracked = source_is_drive or sp_changed is not None
    trackable = source_is_drive and any(f.get("source_modified") and f.get("drive_file_id") for f in files)
    svc = core.drive_service(request) if trackable else None   # 401 in GIS mode without X-Drive-Token
    from googleapiclient.errors import HttpError
    rows = []
    for f in files:
        baseline, drive_id = f.get("source_modified"), f.get("drive_file_id")
        if sp_changed is not None and drive_id and baseline:
            # A file the delta mentions changed; one it does not, did not. `current` is the
            # item's own new timestamp so the SAME comparison Drive uses produces the state —
            # a second classification path would be a second thing to keep true.
            key = (f.get("drive_id"), drive_id)
            if key in sp_removed:
                row = _ss.classify_sync_state(f, None, source_is_drive=False,
                                              source_tracked=True, fetch_error="deleted",
                                              run_status=run_status)
            else:
                row = _ss.classify_sync_state(f, sp_changed.get(key, baseline),
                                              source_is_drive=False, source_tracked=True,
                                              run_status=run_status)
        elif not source_is_drive or not drive_id or not baseline:
            row = _ss.classify_sync_state(f, None, source_is_drive=source_is_drive,
                                          source_tracked=source_tracked, run_status=run_status)
        else:
            current, err = None, None
            try:
                current = svc.files().get(fileId=drive_id, fields="modifiedTime",
                                          supportsAllDrives=True).execute().get("modifiedTime")
            except HttpError as e:
                status = getattr(getattr(e, "resp", None), "status", None)
                err = "not_found" if status == 404 else "forbidden" if status == 403 else "drive_error"
            except Exception:
                err = "drive_error"   # a bad file must never 500 the batch
            row = _ss.classify_sync_state(f, current, source_is_drive=source_is_drive,
                                          fetch_error=err, run_status=run_status)
        rows.append({"file": f["file"], "drive_file_id": drive_id, **row})
    count = lambda st: sum(1 for r in rows if r["state"] == st)
    if sp_error:
        # Named, not swallowed. Every SharePoint file reads `untracked` when the delta replay
        # fails, and an operator seeing a wall of "untracked" deserves to know it is a Graph
        # problem this endpoint hit rather than a scan that recorded nothing.
        return {"scan_id": sid, "sharepoint_freshness_error": sp_error,
                "stale_count": count("stale"), "untracked_count": count("untracked"),
                "unavailable_count": count("unavailable"), "importing_count": count("importing"),
                "import_failed_count": count("import_failed"),
                "publish_pending_count": count("publish_pending"),
                "conflict_count": count("conflict"), "acp_newer_count": count("acp_newer"),
                "files": rows}
    return {"scan_id": sid, "stale_count": count("stale"), "untracked_count": count("untracked"),
            "unavailable_count": count("unavailable"), "importing_count": count("importing"),
            "import_failed_count": count("import_failed"),
            "publish_pending_count": count("publish_pending"),
            "conflict_count": count("conflict"), "acp_newer_count": count("acp_newer"),
            "files": rows}


@router.get("/scans/{sid}/inventory")
def scan_inventory_list(sid: str, request: Request,
                        offset: int = Query(0, ge=0),
                        limit: int = Query(200, ge=1, le=1000)):
    """The whole per-file discover inventory, paginated — EVERY discovered file with its source
    metadata (owner, size, path, dates, lifecycle) plus the estate capability (format/status),
    owner-scoped. This is the full list the capped 200/status dashboard sample could not provide
    (ADR 0020): `total` is the real count, page through it with offset/limit. Export via the
    `.csv` sibling. NB: the estate `status` is derived per row, so filtering by it is a client
    concern for now — a server-side status filter needs the classification persisted (follow-up)."""
    # get_scan_head, not get_scan (PRD H-09). The result is used ONLY as an ownership gate —
    # `is None` and nothing else — but get_scan assembles the entire scan aggregate to produce
    # it, and on a discover-only run (ADR 0020: inventory listed, no file_records yet, which is
    # exactly the state this endpoint serves) it takes the `if not files` fallback and reads the
    # WHOLE scan_inventory table ordered by file. To authorise one 1000-row page.
    #
    # Measured on the incident's own 6,916-row inventory: a full 7-page load read 55,328 rows to
    # return 6,916 — 8.0x amplification, all of it in the gate. get_scan_head is one indexed
    # SELECT on scan_runs with identical owner semantics (None for missing OR foreign), and takes
    # the same load to 6,916 rows read, 1.0x, byte-identical output. This endpoint is one of the
    # routes that 500'd on 2026-08-30, at offset=5000.
    if core.store.get_scan_head(sid, owner=_owner(request)) is None:
        raise HTTPException(404, "scan not found")
    rows = core.store.list_inventory_page(sid, limit=limit, offset=offset)
    return {"scan_id": sid, "total": core.store.count_inventory(sid),
            "offset": offset, "limit": limit,
            "rows": [_inv_capability(r) for r in rows]}


def _lifecycle_scan_owner(sid: str, request: Request) -> str:
    owner = _owner(request)
    if core.store.get_scan_head(sid, owner=owner) is None:
        raise HTTPException(404, "scan not found")
    return owner


@router.get("/scans/{sid}/lifecycle/summary")
def lifecycle_summary(sid: str, request: Request):
    """One mutually-exclusive estate reconciliation from the persisted scan snapshot."""
    owner = _lifecycle_scan_owner(sid, request)
    return core.store.lifecycle_summary(sid, owner)


@router.get("/scans/{sid}/lifecycle/rules")
def lifecycle_rule_results(sid: str, request: Request):
    owner = _lifecycle_scan_owner(sid, request)
    return {"scan_id": sid, "data_version": core.store.lifecycle_data_version(sid),
            "rules": core.store.list_lifecycle_rule_results(sid, owner)}


@router.get("/scans/{sid}/lifecycle/files")
def lifecycle_files(sid: str, request: Request, status: str | None = Query(None),
                    policy_id: str | None = Query(None), candidate_only: bool = Query(False),
                    offset: int = Query(0, ge=0),
                    limit: int = Query(200, ge=1, le=1000)):
    owner = _lifecycle_scan_owner(sid, request)
    rows = core.store.list_lifecycle_files(sid, owner, status=status, policy_id=policy_id,
                                           candidate_only=candidate_only,
                                           limit=limit, offset=offset)
    total = core.store.count_lifecycle_files(sid, owner, status=status, policy_id=policy_id,
                                             candidate_only=candidate_only)
    # One batched read for the whole scan, merged in memory. The queue needs the audit id, the
    # policy VERSION and the proposed action to bound a grouped approval (PRD §8), and asking
    # per row would rebuild the N+1 that #1163 removed from the CSV export next door.
    pending = core.store.pending_approvals_by_file(sid, owner)
    return {"scan_id": sid, "data_version": core.store.lifecycle_data_version(sid),
            "total": total, "offset": offset, "limit": limit,
            "rows": [{**_inv_capability(r), **(pending.get(r.get("file")) or {})} for r in rows]}


@router.get("/scans/{sid}/lifecycle/files/{document_id:path}/history")
def lifecycle_file_history(sid: str, document_id: str, request: Request,
                           limit: int = Query(300, ge=1, le=1000)):
    """One document's lifecycle timeline, across every scan this owner can see (PRD §7.4).

    Scan-scoped in its PATH but not in its ANSWER, and the difference is the feature: the
    reviewer arrives from a scan, and the question they have is what happened to this document
    BEFORE it. A history that stopped at the current scan would show one recommendation and
    look complete.

    §10.2 names this /documents/{document_id}/lifecycle/history. It lives here instead because
    the identifier the lifecycle surfaces actually carry is the file path, and the lifecycle doc
    id embeds the scan (`scan:{scan_id}:{file}`) — a route keyed on documents.doc_id could not
    find these events at all. The path follows the identity that exists rather than the one the
    PRD assumed.

    The scan in the path still authorises: it is the owner gate, exactly as the sibling
    detail route uses it.
    """
    owner = _lifecycle_scan_owner(sid, request)
    return {"scan_id": sid, "document_id": document_id,
            "events": core.store.lifecycle_history(document_id, owner, limit=limit)}


@router.get("/scans/{sid}/lifecycle/files/{document_id:path}")
def lifecycle_file_detail(sid: str, document_id: str, request: Request):
    owner = _lifecycle_scan_owner(sid, request)
    row = core.store.lifecycle_file_detail(sid, document_id, owner)
    if row is None:
        raise HTTPException(404, "document not found in this scan")
    return {**_inv_capability(row), "evaluations": row.get("evaluations", []),
            "data_version": core.store.lifecycle_data_version(sid)}


def _sp_export_cells(raw) -> dict:
    """The three export cells that come out of the `sp_metadata` JSON rather than a column.

    `managed_columns` flattens the tenant's own columns to "Name=Value; Name=Value" — a sheet
    cell an information architect can read, rather than JSON they have to parse to check one
    value. `sp_availability` names only the fields that are NOT present, because listing thirty
    "present" states per row would bury the two that matter under noise; a field absent from this
    cell was read and had a value. `sp_unread_reason` carries the explanations.

    Never raises: this blob is written by a scan and read by an export, and a malformed one from
    a partially-rolled-forward replica must cost that row its metadata cells, not the whole
    auditor's export.
    """
    if not raw:
        return {}
    try:
        blob = _json.loads(raw) if isinstance(raw, str) else raw
    except Exception:  # noqa: BLE001
        return {}
    if not isinstance(blob, dict):
        return {}
    managed = blob.get("managed_columns") or {}
    availability = blob.get("availability") or {}
    reasons = blob.get("reasons") or {}
    return {
        "managed_columns": "; ".join(f"{k}={v}" for k, v in managed.items()) or None,
        "sp_availability": "; ".join(f"{k}={v}" for k, v in availability.items()
                                     if v and v != "present") or None,
        "sp_unread_reason": "; ".join(f"{k}: {v}" for k, v in reasons.items()) or None,
        # SMART ARCHIVAL, and the two cells travel together on purpose. A sheet showing
        # "collaborators: 1" without saying how it was counted invites the reader to treat an
        # authorship FLOOR (creator + last editor, all a listing page can name) as a total. The
        # basis is what makes the number safe to act on — see sp_metadata.collaborator_count.
        "collaborator_count": (blob.get("collaborators") or {}).get("count"),
        "collaborator_basis": (blob.get("collaborators") or {}).get("basis"),
        # Access is not use. An empty cell here is "not measured", never "idle" — the counts are
        # absent from the blob entirely unless the analytics read actually happened, and
        # `sp_availability` carries the state for a reader who needs to be sure.
        "recent_actor_count": (blob.get("activity") or {}).get("actors"),
        "recent_action_count": (blob.get("activity") or {}).get("actions"),
    }


@router.get("/scans/{sid}/exceptions.csv")
def scan_exceptions_csv(sid: str, request: Request):
    """Everything this scan could NOT read, as CSV — the exportable exception report.

    An estate report says what was found. This says what was missed, which is the half an auditor
    and an IT admin actually act on: a site whose consent lapsed, a library that throttled out, a
    selection the site cap refused. Those facts have been on the scan's scope since Phase 1 and
    have only ever been visible inside the app, one run at a time; a customer chasing thirty
    consents needs a list they can send to somebody.

    EMPTY IS A REAL ANSWER AND IS RETURNED AS ONE — a header row and no data. A report that 404s
    when nothing failed is indistinguishable from a report that could not be produced, and the
    difference is exactly what the reader is asking about.

    Rows are per SITE and per LIBRARY, because those fail independently: a site can be readable
    while one of its libraries throttles out, and merging them would hide the working nine tenths
    of a site behind its broken tenth.
    """
    import csv
    import io
    if core.store.get_scan_head(sid, owner=_owner(request)) is None:
        raise HTTPException(404, "scan not found")
    scan = core.store.get_scan(sid, owner=_owner(request)) or {}
    scope = (scan.get("run") or {}).get("scope")
    if isinstance(scope, str):
        try:
            scope = _json.loads(scope)
        except Exception:  # noqa: BLE001
            scope = {}
    scope = scope if isinstance(scope, dict) else {}

    cols = ["level", "site_id", "site_name", "library_id", "library_name", "status",
            "documents_listed", "reason"]
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(cols)
    for site in (scope.get("sites") or []):
        if not isinstance(site, dict):
            continue
        # A site that completed is not an exception, however little it held: an empty library is
        # an answer about the tenant, and listing it here would bury the sites that actually
        # failed under the ones that are simply small.
        if site.get("status") in ("blocked", "skipped", "partial"):
            w.writerow(["site", site.get("id") or "", site.get("name") or "", "", "",
                        site.get("status") or "", site.get("listed") if site.get("listed")
                        is not None else "", site.get("error") or ""])
        for lib in (site.get("libraries") or []):
            if not isinstance(lib, dict) or not lib.get("full_reason"):
                continue
            # A library walked in full is not itself an exception — but the REASON it had to be
            # is the operational fact: an expired cursor, a forced reconciliation, a first sync.
            w.writerow(["library", site.get("id") or "", site.get("name") or "",
                        lib.get("id") or "", lib.get("name") or "", lib.get("mode") or "",
                        "", lib.get("full_reason")])
    return Response(buf.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="exceptions-{sid}.csv"'})


@router.get("/scans/{sid}/inventory.csv")
def scan_inventory_csv(sid: str, request: Request):
    """The whole per-file estate inventory as CSV (owner-scoped) — every discovered file, source
    metadata + capability, for offline analysis / an auditor. Not paginated: it IS the export."""
    # get_scan_head for the same reason as the paginated sibling above — this one reads the whole
    # inventory itself, so paying for a second full read in the ownership gate is pure duplication.
    if core.store.get_scan_head(sid, owner=_owner(request)) is None:
        raise HTTPException(404, "scan not found")
    import csv
    import io
    # lifecycle_rule_id/lifecycle_reason/lifecycle_override_* were missing here even though
    # store.list_inventory already selects them (_INV_COLS) — an auditor exporting the estate saw
    # THAT a file was tagged (lifecycle_status) but not WHICH rule tagged it, WHY, or whether a
    # human overrode the recommendation (lifecycle rules #8). Added alongside lifecycle_status,
    # not in place of it.
    # SharePoint-native columns (Phase 2) sit beside the lifecycle ones because they are what an
    # auditor checks a lifecycle decision AGAINST: "archived under the Superseded content type"
    # is checkable from this sheet, "archived because a rule said so" is not.
    #
    # `sp_availability` is the column that makes the rest readable. Every other SharePoint cell
    # can be empty for two opposite reasons — the tenant sets nothing, or ACP was refused — and
    # an export that cannot tell them apart invites the wrong conclusion in the more damaging
    # direction: an estate whose sensitivity labels nobody ever requested reads as an estate with
    # no sensitivity labels. This column carries the per-field state, and `sp_unread_reason` the
    # explanation where there is one.
    cols = ["file", "owner", "size_kb", "mime", "format", "status", "doc_class",
            "lifecycle_status", "lifecycle_rule_id", "lifecycle_reason",
            "policy_version", "evaluation_result", "evidence_json",
            "lifecycle_override_reason", "lifecycle_overridden_by", "lifecycle_overridden_at",
            "site_name", "library_name", "content_type", "retention_label",
            "sensitivity_label", "sharing_scope", "item_kind", "checked_out_by",
            "sp_version", "modified_by", "managed_columns",
            "collaborator_count", "collaborator_basis",
            "recent_actor_count", "recent_action_count",
            "sp_availability", "sp_unread_reason",
            "path", "parent_folder", "created_at", "source_modified",
            "discovered_at", "drive_file_id"]
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(cols)
    # ONE grouped read for the whole scan, not lifecycle_file_detail per row. This loop is
    # unbounded by construction ("it IS the export"), and that call costs two queries each, so
    # the estate that most needs an export — 6,000+ files — paid ~12,000 round trips for it.
    # tests/test_inventory_read_amplification.py measures ROWS and so was blind to this: with no
    # evaluations recorded the extra queries return nothing at all. Its new sibling counts queries.
    evaluations_by_document = core.store.lifecycle_evaluations_by_document(sid, _owner(request))
    for r in core.store.list_inventory(sid):
        e = _inv_capability(r)
        evaluations = evaluations_by_document.get(r.get("file")) or []
        winning = next((x for x in evaluations if str(x.get("policy_id")) == str(r.get("lifecycle_rule_id"))), None)
        if winning:
            e["policy_version"] = winning.get("policy_version")
            e["evaluation_result"] = winning.get("result")
            e["evidence_json"] = _json.dumps(winning.get("evidence") or {}, separators=(",", ":"))
        e.update(_sp_export_cells(r.get("sp_metadata")))
        w.writerow([e.get(c, "") if e.get(c) is not None else "" for c in cols])
    return Response(buf.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="inventory-{sid}.csv"'})


@router.get("/scans/{sid}/files/{filename:path}/remediation-state")
def file_remediation_state(sid: str, filename: str, request: Request):
    """Per-violation remediation state (ADR 0003 Phase 2) for one file — which specific
    WCAG rules were auto-fixed vs. still open, so the UI can distinguish a criterion that
    always passed from one that passes because remediation fixed it."""
    if core.store.get_scan(sid, owner=_owner(request)) is None:
        raise HTTPException(404, "scan not found")
    return core.store.get_remediation_state_for_file(sid, filename)


@router.get("/scans/{sid}/traces")
def scan_traces(sid: str, request: Request, file: str | None = None):
    """Per-rule trace for a scan. Returns one row per (file, rule) pair showing
    PASS/FAIL/SKIP and the finding count. Optionally filter to a single file."""
    if core.store.get_scan(sid, owner=_owner(request)) is None:
        raise HTTPException(404, "scan not found")
    return core.store.get_scan_traces(sid, file=file)


@router.get("/scans/{sid}/history")
def scan_history(sid: str, request: Request, after_seq: int | None = Query(None, ge=0),
                 limit: int = Query(500, ge=1, le=2000)):
    """ADR 0042 PR 3 — this RUN's durable lifecycle history: queued → claimed → listing →
    inventory saved → lifecycle applied → discovered, plus any retry or failure along the way.

    The run-level counterpart to /scans/{sid}/timeline, which answers the same question about one
    DOCUMENT. Until this endpoint, "what happened to this run?" had no answer that outlived Redis:
    the job record TTLs out after an hour, and `scan_runs.live_checkpoint` is a single overwritten
    cell holding last-known-state, not history. Everything here survives a container restart, an
    ACA revision rollout, and the Redis TTL, because it is ordinary Postgres rows.

    `after_seq` is exclusive — pass the highest `seq` you have and get only what you missed. That
    is the question a reconnecting client actually has, and the reason `seq` exists.

    ALWAYS 200, degrading to {"available": false} like /live and /status rather than raising: this
    is a supplementary narration panel, and a run-detail screen must not break because its history
    could not be read.

    OWNER-SCOPING IS THE get_scan GATE, deliberately NOT list_scan_events' own `owner` filter.
    Events written before an owner was known carry owner_email=NULL (the thread path assigns a
    scan_id before it has a user), so passing `owner=` here would silently drop the earliest
    events of a run the caller legitimately owns — hiding exactly the queued/claimed rows that
    explain a stuck scan. The store's docstring says as much; this is that warning obeyed.
    """
    if core.store.get_scan(sid, owner=_owner(request)) is None:
        return {"available": False, "reason": "scan_not_found"}
    # THE POLLING FALLBACK READS THE SAME PROJECTION AS THE STREAM. Two paths to the same rows
    # with one privacy rule between them is how a suppressed name gets served by the half nobody
    # was looking at — and this is the half a client falls back to precisely when the stream is
    # unavailable, so it is the less-watched one by construction.
    privacy = core.store.remediation_filename_privacy(sid)
    events = _project_events(core.store.list_scan_events(sid, after_seq=after_seq, limit=limit),
                             sid, privacy)
    return {"available": True, "scan_id": sid, "events": events, "count": len(events),
            # The cursor for the next call. None on an empty page rather than 0 — 0 is a real
            # `after_seq` meaning "from the start", and returning it for "nothing here" would make
            # a caught-up client re-request the whole history on every poll.
            "latest_seq": events[-1]["seq"] if events else None}


@router.get("/scans/{sid}/timeline")
def scan_timeline(sid: str, request: Request, file: str = Query(...)):
    """Audit trail (maturity Phase 4): the chronological provenance of one document in this
    scan — scanned → AI drafted → human decided → fix written → published — assembled from
    rows the pipeline already persists. Owner-scoped like every per-scan surface."""
    if core.store.get_scan(sid, owner=_owner(request)) is None:
        raise HTTPException(404, "scan not found")
    return core.store.document_timeline(sid, file)


class LifecycleOverrideIn(BaseModel):
    reason: str


@router.post("/scans/{sid}/files/{filename:path}/lifecycle-override")
def override_file_lifecycle(sid: str, filename: str, body: LifecycleOverrideIn, request: Request):
    """Lifecycle rules #8: record a human's reasoned disagreement with a rule's Archive/Delete
    Candidate recommendation for ONE file. A reason is required — an undocumented override is
    exactly the unaccountable state this exists to prevent (same discipline as W4's
    DispositionControl). It does not change the file's lifecycle_status: like every other
    lifecycle surface, this is itself only a recommendation, not an action.

    Writes BOTH audit tables, the same dual-write convention every other disposition route
    follows: `disposition_audit` (the audit_id-precise, action-typed record) and `log_decision`
    (the file-scoped record `document_timeline`/the Assessment Timeline drawer already reads —
    this is what makes the override show up in a file's existing audit history without a new
    viewer)."""
    if core.store.get_scan(sid, owner=_owner(request)) is None:
        raise HTTPException(404, "scan not found")
    reason = (body.reason or "").strip()
    if not reason:
        raise HTTPException(422, "a reason is required — it becomes part of the audit record")
    owner = _owner(request)
    prior = core.store.override_lifecycle(sid, filename, reason=reason, actor=owner)
    if prior is None:
        raise HTTPException(409, "this file has no archive/delete recommendation to override")
    rule_id = prior.get("lifecycle_rule_id")
    doc_id = f"scan:{sid}:{filename}"
    if rule_id:
        # Deterministic id (same pattern as handlers.py's Discover lifecycle evaluator, #808) —
        # PRD §20 idempotency audit, 2026-08-28: this used to be uuid.uuid4().hex, so a
        # double-click or an HTTP client retry of this POST inserted a second, distinct
        # disposition_audit row for the same override. Keyed on the reason text too (not just
        # sid/filename/rule_id): a genuinely NEW override with a different reason must still be
        # recorded — only an exact repeat of the same request collides via ON CONFLICT(id) DO
        # NOTHING.
        _audit_id = hashlib.sha256(
            f"override:{sid}:{filename}:{rule_id}:{reason}".encode()).hexdigest()[:24]
        core.store.create_disposition_audit(_audit_id, doc_id=doc_id, policy_id=rule_id,
            action="tag", result="overridden", detail=reason, owner_email=owner)
    core.store.log_decision(owner, "disposition.file_overridden", scan_id=sid, file=filename,
        rule_id=rule_id, detail=reason)
    return core.store.get_lifecycle_status(sid, filename)


@router.get("/scans/{sid}/comments")
def list_finding_comments(sid: str, request: Request, finding_key: str = Query(...)):
    """R18 · Comments on a finding — the human discussion thread for ONE finding, oldest first.
    Owner-scoped like every per-scan surface: a user reads only their own scans' threads."""
    if core.store.get_scan(sid, owner=_owner(request)) is None:
        raise HTTPException(404, "scan not found")
    return core.store.list_finding_comments(sid, finding_key)


@router.get("/scans/{sid}/comment-counts")
def finding_comment_counts(sid: str, request: Request):
    """How many comments each finding in the scan carries, keyed by finding_key — so the inbox
    can show a '💬 n' marker without fetching every thread. Owner-scoped."""
    if core.store.get_scan(sid, owner=_owner(request)) is None:
        raise HTTPException(404, "scan not found")
    return core.store.count_finding_comments(sid)


@router.post("/scans/{sid}/comments")
async def add_finding_comment(sid: str, request: Request):
    """R18 · Post one comment to a finding's thread. Body: {"finding_key": "...", "body": "...",
    "file": "...", "rule_id": "..."}. The author is the signed-in user, never the client — so a
    comment cannot be attributed to someone else. Append-only; empty bodies are rejected."""
    owner = _owner(request)
    if core.store.get_scan(sid, owner=owner) is None:
        raise HTTPException(404, "scan not found")
    try:
        body = await request.json()
    except Exception:
        body = {}
    finding_key = (body.get("finding_key") or "").strip()
    text = (body.get("body") or "").strip()
    if not finding_key:
        raise HTTPException(422, "finding_key is required")
    if not text:
        raise HTTPException(422, "comment body is required")
    return core.store.add_finding_comment(
        sid, finding_key, owner, text,
        file=(body.get("file") or "").strip(), rule_id=(body.get("rule_id") or "").strip())


@router.get("/scans/{sid}/applied-fixes")
def scan_applied_fixes(sid: str, request: Request):
    """The concrete values AI fixes wrote this scan (vision-generated alt text + a small
    image thumbnail), newest first — so 'Recent AI fixes' can show what was really applied
    instead of a canned template. Owner-scoped."""
    if core.store.get_scan(sid, owner=_owner(request)) is None:
        raise HTTPException(404, "scan not found")
    return core.store.list_applied_fixes(sid)


@router.get("/scans/{sid}/ai_calls")
def scan_ai_calls(sid: str, request: Request):
    """The AI provenance ledger for a scan (ADR 0019 Phase 0b) — one row per model call with its
    surface, provider, model, privacy zone (local vs cloud), latency, and outcome, newest first.
    This is the auditable answer to 'what model saw my document, where did it run, how long did it
    take?' that the review card's audit panel and the certification record surface. Owner-scoped."""
    if core.store.get_scan(sid, owner=_owner(request)) is None:
        raise HTTPException(404, "scan not found")
    return core.store.list_ai_calls(sid)


class ReleaseAiProvenanceRequest(BaseModel):
    files: list[str]


@router.post("/scans/{sid}/release/ai-provenance")
def release_ai_provenance(sid: str, request: Request, body: ReleaseAiProvenanceRequest):
    """Exact model, review, and post-write evidence for the selected release files.

    Unlike the scan-wide operational ledger, this projection links human and validation outcomes
    only by their durable model-call id.  Historical or human-authored decisions without that id
    are deliberately absent rather than inferred from a nearby successful call.
    """
    scan = core.store.get_scan(sid, owner=_owner(request))
    if scan is None:
        raise HTTPException(404, "scan not found")
    selected = list(dict.fromkeys(str(name) for name in body.files if name))
    known = {row.get("file") for row in scan.get("files", [])}
    unknown = [name for name in selected if name not in known]
    if unknown:
        raise HTTPException(404, f"corrected file not found: {unknown[0]}")
    return core.store.release_ai_provenance(sid, selected)


@router.post("/scans/{sid}/files/{filename:path}/undo-fix")
async def undo_fix(sid: str, filename: str, request: Request):
    """R15 — undo one deterministic fix ACP claims to have applied to this file.

    Body: {"rule_id": "1.1.1"}. There is no file to restore: remediation only ever produces a
    SEPARATE corrected copy (ensure_remediated_folder), never overwriting the source, so this
    cannot mean 'put the bytes back' — it means ACP stops claiming the finding is fixed. See
    store.undo_applied_fix for exactly what that clears. Owner-scoped like every other
    remediation route; 404s the same way as an unknown/foreign scan, 400 for a missing rule_id."""
    if core.store.get_scan(sid, owner=_owner(request)) is None:
        raise HTTPException(404, "scan not found")
    try:
        body = await request.json()
    except Exception:
        body = {}
    rule_id = str(body.get("rule_id") or "").strip()
    if not rule_id:
        raise HTTPException(400, "rule_id required")
    undone = core.store.undo_applied_fix(sid, filename, rule_id)
    return {"undone": undone}


@router.get("/scans/{sid}/files/{filename:path}/remediation-diffs")
def file_remediation_diffs(sid: str, filename: str, request: Request):
    """Per-fix before→after evidence for one file — the original text/markup and the
    remediated version of every deterministic fix that verifiably cleared. Feeds the
    certification PDF's 'Before → After' section. Owner-scoped like remediation-state."""
    if core.store.get_scan(sid, owner=_owner(request)) is None:
        raise HTTPException(404, "scan not found")
    return core.store.get_remediation_diffs(sid, filename)


@router.get("/scans/{sid}/remediation-diffs")
def scan_remediation_diffs(sid: str, request: Request, include_summary: bool = False):
    """Scan-wide before→after evidence — every verified-cleared fix across all files, so the
    Remediation view can group REAL applied fixes by rule/category (image descriptions,
    reading order, titles, headings, tables) without inventing counts. Covers all fix types,
    unlike applied-fixes (image alt text only). Owner-scoped."""
    if core.store.get_scan(sid, owner=_owner(request)) is None:
        raise HTTPException(404, "scan not found")
    return core.store.remediation_diff_page(sid) if include_summary else core.store.list_remediation_diffs(sid)


@router.get("/scans/{sid}/diff")
def scan_diff(sid: str, request: Request, vs: str | None = Query(None)):
    """Regression diff (ADR 0009): which documents got worse / better vs a prior scan, and
    the WCAG criteria that flipped pass→fail. Owner-scoped. `vs` defaults to the caller's
    immediately-prior scan."""
    owner = _owner(request)
    if core.store.get_scan(sid, owner=owner) is None:
        raise HTTPException(404, "scan not found")
    if not vs:                                          # default baseline = the prior scan
        ids = [s["id"] for s in core.store.list_scans(owner=owner)]
        i = ids.index(sid) if sid in ids else -1
        vs = ids[i + 1] if 0 <= i and i + 1 < len(ids) else None
    if not vs:
        return {"summary": {"regressed": 0, "improved": 0, "new": 0, "removed": 0},
                "regressed": [], "improved": [], "new": [], "removed": [], "no_baseline": True}
    diff = core.store.get_scan_diff(sid, vs, owner=owner)
    if diff is None:
        raise HTTPException(404, "scan not found")
    return diff


@router.get("/scans/{sid}/inventory-diff")
def scan_inventory_diff(sid: str, request: Request, vs: str | None = Query(None)):
    """Discovery diff: what this run's inventory gained, lost and changed vs a prior run of the
    SAME SOURCE.

    Deliberately not `/diff`, which compares `file_records` — the assessed grain, empty for an
    ADR 0020 Discover-only run. This reads `scan_inventory`, so it answers for the runs a source
    operations panel is actually about.

    THE BASELINE IS PER-SOURCE, and that is the difference that matters here. `/diff` defaults
    `vs` to the caller's immediately-prior scan across every source, which is the right default
    for "did my estate regress" and the wrong one for "what did OneDrive find this time": with
    two connectors alternating, the prior scan is routinely the OTHER source, and every file in
    it would read as removed while every file in this one reads as new. So the default walks back
    to the previous run of this run's own source, and returns `no_baseline` when there is none
    rather than diffing against something unrelated.

    An explicit `vs` is honoured as given — the caller may have a better baseline in mind (the
    drawer passes the prior run it is already displaying) — but is still owner-scoped by
    get_inventory_diff.
    """
    owner = _owner(request)
    run = core.store.get_scan(sid, owner=owner)
    if run is None:
        raise HTTPException(404, "scan not found")
    if not vs:
        vs = core.store.previous_run_for_source(sid, owner=owner)
    if not vs:
        return {"summary": {"new": 0, "changed": 0, "removed": 0, "unchanged": 0,
                            "not_listed": 0, "indeterminate": 0},
                "new": [], "changed": [], "removed": [], "not_listed": [], "indeterminate": [],
                "no_baseline": True}
    diff = core.store.get_inventory_diff(sid, vs, owner=owner)
    if diff is None:
        raise HTTPException(404, "scan not found")
    return diff


_digest_cache: dict = {}


@router.get("/scans/{sid}/digest")
def scan_digest(sid: str, request: Request, refresh: bool = Query(False)):
    """AI Compliance Digest (bundle #2): an executive paragraph grounded in REAL scan data —
    score, regressions vs the prior scan, the top systemic issue, and a recommended action.
    Deterministic facts + an AI-written narrative (fallback prose if the model is off/down).
    Owner-scoped; cached per scan (the model call is slow)."""
    import ai as _ai
    owner = _owner(request)
    res = core.store.get_scan(sid, owner=owner)
    if res is None:
        raise HTTPException(404, "scan not found")
    if not refresh and sid in _digest_cache:
        return _digest_cache[sid]
    run, files = res["run"], res["files"]
    total = len(files)
    certifiable = sum(1 for f in files if f.get("compliant"))
    # Drift vs the prior scan (reuses the ADR 0009 diff) + the score delta.
    ids = [s["id"] for s in core.store.list_scans(owner=owner)]
    i = ids.index(sid) if sid in ids else -1
    prev_id = ids[i + 1] if 0 <= i and i + 1 < len(ids) else None
    regressed, improved_count, score_delta = [], 0, None
    if prev_id:
        diff = core.store.get_scan_diff(sid, prev_id, owner=owner)
        if diff:
            regressed = diff.get("regressed", [])
            improved_count = diff.get("summary", {}).get("improved", 0)
            prev = core.store.get_scan(prev_id, owner=owner)
            if prev and prev["run"].get("avg_score") is not None and run.get("avg_score") is not None:
                score_delta = run["avg_score"] - prev["run"]["avg_score"]
    # Top systemic issues — criteria failing on the most documents (from per-rule traces).
    fail_by_rule: dict = {}
    for r in core.store.get_scan_traces(sid):
        if str(r.get("outcome", "")).upper() == "FAIL":
            k = r["rule_id"]
            fb = fail_by_rule.setdefault(k, {"sc": k, "name": r.get("plain_name") or r.get("rule_name"), "files": set()})
            fb["files"].add(r["file"])
    top_issues = sorted(
        ({"sc": v["sc"], "name": v["name"], "fail": len(v["files"])} for v in fail_by_rule.values()),
        key=lambda x: -x["fail"])[:3]
    pii = core.store.pii_summary(sid)
    data = {"avg_score": run.get("avg_score"), "total": total, "certifiable": certifiable,
            "regressed": regressed, "improved_count": improved_count, "score_delta": score_delta,
            "top_issues": top_issues, "pii_docs": (pii or {}).get("documents", 0)}
    digest = _ai.compliance_digest(data, ai_enabled=core.store.get_ai_enabled())
    digest["generated_at"] = run.get("completed_at")
    _digest_cache[sid] = digest
    return digest


@router.get("/scans/{sid}/pii")
def scan_pii(sid: str, request: Request):
    """Sensitive-data (PII) findings for a scan (ADR 0006).

    Returns a rollup (documents affected, total items, per-type counts) plus the
    per-document detail. All samples are MASKED — raw PII is never stored or
    returned."""
    if core.store.get_scan(sid, owner=_owner(request)) is None:
        raise HTTPException(404, "scan not found")
    return {"summary": core.store.pii_summary(sid), "files": core.store.list_pii(sid)}


@router.get("/scans/{sid}/manifest")
def scan_manifest(sid: str, request: Request):
    """Rule-execution manifest for a scan.

    Returns per-file completeness: how many rules were expected to run (based on
    the file type's rule catalog), how many ran successfully (PASS or FAIL), and
    how many errored (ENGINE failed to assess that rule). A scan is COMPLETE when
    rules_errored_total == 0. Use this to detect partial assessments before acting
    on a score.
    """
    if core.store.get_scan(sid, owner=_owner(request)) is None:
        raise HTTPException(404, "scan not found")
    return core.store.get_scan_manifest(sid)


def _canonical_lineage_export(scan_id: str, owner: str) -> dict:
    """Stable export form: omit request-time timestamps while preserving durable evidence time."""
    lineage = core.store.canonical_stage_lineage(scan_id, owner=owner)
    lineage.pop("generated_at", None)
    for stage in lineage.get("stages", []):
        stage.pop("generated_at", None)
        import progress_evidence
        stage.update(progress_evidence.read(core.store, stage.get('execution_id'), owner=owner))
    encoded = _json.dumps(lineage, sort_keys=True, separators=(",", ":"),
                          ensure_ascii=False, default=str).encode("utf-8")
    return {"lineage": lineage, "content_digest": {
        "algorithm": "SHA-256", "value": hashlib.sha256(encoded).hexdigest()}}


@router.get("/scans/{sid}/stage-lineage")
def stage_lineage(sid: str, request: Request):
    """Owner-scoped canonical stage totals, sealed handoffs, and integrity as one authority."""
    owner = _owner(request)
    if core.store.get_scan_head(sid, owner=owner) is None:
        raise HTTPException(404, "scan not found")
    return _canonical_lineage_export(sid, owner)


@router.get("/scans/{sid}/finding-dispositions")
def finding_dispositions(sid: str, request: Request,
                         disposition: str | None = Query(default=None)):
    """Owner-scoped rows behind the current reconciliation buckets.

    Counts still come from the canonical remediation snapshot.  This endpoint explains those
    counts; it never reconstructs a second total and never includes historical batches.
    """
    owner = _owner(request)
    if core.store.get_scan(sid, owner=owner) is None:
        raise HTTPException(404, "scan not found")
    facts = core.store.remediation_run_facts(sid)
    batch_id = facts.get("batch_id")
    import assessment_blocked
    blocked = assessment_blocked.read(core.store, sid, owner=owner)
    if not batch_id:
        return {"scan_id": sid, "batch_id": None, "disposition": disposition,
                "items": [], "available": False, 'assessment_blocked_files': blocked}
    try:
        items = core.store.finding_disposition_drilldown(
            sid, batch_id, disposition=disposition)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"scan_id": sid, "batch_id": batch_id, "snapshot_id": sid,
            "disposition": disposition, "items": items, "available": True,
            'assessment_blocked_files': blocked}


@router.get("/scans/{sid}/finding-dispositions/{finding_id}/events")
def finding_disposition_events(sid: str, finding_id: str, request: Request):
    """Owner-scoped transition history for a current-batch finding."""
    owner = _owner(request)
    if core.store.get_scan(sid, owner=owner) is None:
        raise HTTPException(404, "scan not found")
    batch_id = core.store.remediation_run_facts(sid).get("batch_id")
    if not batch_id:
        raise HTTPException(404, "finding not found")
    current = core.store.finding_disposition_drilldown(sid, batch_id)
    if not any(row["finding_id"] == finding_id for row in current):
        raise HTTPException(404, "finding not found")
    return {"scan_id": sid, "batch_id": batch_id, "finding_id": finding_id,
            "events": core.store.finding_disposition_events(sid, batch_id, finding_id)}


#: Which renderer serves /scans/{sid}/report.pdf. "weasy" (the default) is the PDF/UA-1
#: conformant one; "tagged" restores the previous Chromium renderer WITHOUT a redeploy, which is
#: the point of the switch existing at all — the cutover shipped before two of the gates ADR 0034
#: asks for (PAC 2024, and a screen-reader pass) had been run, so there needs to be a way back
#: that does not require a build. Anything else falls through to the default.
_REPORT_RENDERER = os.environ.get("ACP_REPORT_RENDERER", "weasy").strip().lower()


def _render_report(run: dict, files: list, meta: dict, **kw) -> bytes:
    """Render the conformance report, degrading rather than 500ing.

    Order is deliberate. WeasyPrint first: it is the only one of the three that produces a
    PDF/UA-1 conformant document (veraPDF ua1, 0 failures) with a real structure tree, and the
    only one that does not print `file:///tmp/acp_report_<random>/report.html` at the foot of
    every page — a local path on a document handed to a customer as audit evidence.

    It needs native libraries (Pango, HarfBuzz, fontconfig, gobject — verified against
    weasyprint.text.ffi's own dlopen list, not from documentation) and those are declared in
    deploy/public/Dockerfile.base-api. If the image is nevertheless missing them, `import
    weasyprint` raises OSError rather than ImportError, so this catches Exception rather than
    ImportError and an incomplete image degrades to the previous renderer instead of taking the
    endpoint down. That is not a licence to ship an image without them: a fallback that fires in
    production is a silent regression to a non-conformant report, which is why
    test_report_renderer_wiring.py asserts the dependency is declared wherever it is installed.
    """
    if _REPORT_RENDERER == "tagged":
        order = (build_tagged_report, build_report)
    else:
        order = (build_weasy_report, build_tagged_report, build_report)
    last: Exception | None = None
    for render in order:
        try:
            return render(run, files, meta, **kw)
        except Exception as exc:  # noqa: BLE001 — any renderer failure degrades to the next
            last = exc
            logging.warning("report renderer %s failed, falling back: %s: %s",
                            getattr(render, "__name__", render), type(exc).__name__, exc)
    raise HTTPException(500, f"could not render report: {last}")


@router.get("/scans/{sid}/report.pdf")
def report_pdf(sid: str, request: Request):
    # Owner-scoped. The frontend fetches this WITH the Bearer token (XHR → blob → open),
    # so a plain tokenless <a href> no longer works — that's intentional: it stops anyone
    # pulling another user's full report by guessing/replaying a scan id.
    res = core.store.get_scan(sid, owner=_owner(request))
    if res is None:
        raise HTTPException(404, "scan not found")
    owner = _owner(request)
    rb = core.active_rubric()
    lineage_export = _canonical_lineage_export(sid, owner)
    snapshot_id = core.store.stage_snapshot_id(sid)
    finding_reconciliation = _release_finding_reconciliation(
        sid, snapshot_id, lineage_export["lineage"])
    meta = {"target": rb.cfg.get("conformance_target"), "version": rb.version,
            "hash": res["run"].get("rubric_hash") or rb.hash,
            "stage_lineage_digest": lineage_export["content_digest"]["value"],
            "finding_reconciliation": finding_reconciliation,
            "stage_lineage_status": ("unavailable" if not
                lineage_export["lineage"]["available"] else "consistent" if
                lineage_export["lineage"]["integrity"]["ok"] else "inconsistent")}
    decisions = core.store.get_decisions(sid)
    evidence = core.store.get_remediation_evidence(sid)
    facts = dict(core.store.get_certification_facts(sid, apply_document_selection=True))
    # Preserve exact queued proposals for offline follow-up. The legacy evidence
    # rollup groups by criterion and cannot identify multiple affected objects.
    from assessment_selection import selected_for_file
    scope = core.store.get_scan_scope(sid)
    tasks = core.store.list_hitl_queue(scan_id=sid, owner=owner)
    scoped_tasks = []
    for task in tasks:
        name = task.get("file") or ""
        codes = selected_for_file(core.store.scope_for_file(sid, name, scope), name)
        match = re.search(r"(?:SC_)?(\d+)[._](\d+)[._](\d+)",
                          str(task.get("rule_id") or task.get("wcag") or ""))
        if codes is None or (match and ".".join(match.groups()) in codes):
            scoped_tasks.append(task)
    facts["audit_review_tasks"] = scoped_tasks
    pdf = _render_report(res["run"], res["files"], meta,
                         decisions=decisions, evidence=evidence, facts=facts)
    return Response(pdf, media_type="application/pdf",
                    headers={"Content-Disposition": f'attachment; filename="acp-report-{sid}.pdf"'})


@router.get("/inventory")
def inventory(request: Request):
    # The inventory table has no owner column — it is a global path-dedup index.
    # Only admins may read it; regular users access per-scan inventory via GET /scans/{sid}/inventory.
    if not core.is_admin(getattr(request.state, "user_email", None)):
        raise HTTPException(403, "admin access required")
    return core.store.inventory()


# ── Per-file remediation ──────────────────────────────────────────────────────

@router.post("/scans/{scan_id}/files/{filename:path}/remediate")
def mark_remediated(scan_id: str, filename: str, request: Request):
    """Record that a file was remediated (download or Drive write-back)."""
    if core.store.get_scan(scan_id, owner=_owner(request)) is None:
        raise HTTPException(404, "scan not found")
    now = core.store.record_remediation(scan_id, filename)
    core.emit_remediation_span(scan_id, filename, drive_write_url=None)
    return {"remediated_at": now}


@router.post("/scans/{sid}/rescore")
def rescore_file(sid: str, request: Request, file: str = Query(...)):
    """Re-download and re-analyse ONE file that a user fixed externally, then refresh the
    scan aggregate. Enqueues a rescore_file worker job and returns its id for polling."""
    scan = core.store.get_scan(sid, owner=_owner(request))
    if scan is None:
        raise HTTPException(404, "scan not found")
    run = scan.get("run", {})
    source = run.get("source", "local")
    drive_token = request.headers.get("x-drive-token")
    jid = core.store.enqueue_job(
        "rescore_file",
        {"scan_id": sid, "file": file, "source": source,
         "drive_token": drive_token, "user": _owner(request)},
        scan_id=sid,
    )
    return {"scan_id": sid, "file": file, "job_id": jid, "workers": core.WORKERS,
            "worker_tier_alive": core.store.worker_tier_alive()}


@router.post("/scans/{sid}/drive-token")
def refresh_scan_drive_token(sid: str, request: Request):
    """Refresh the Drive token of a RUNNING scan (ADR 0014). GIS access tokens expire ~1h;
    the frontend silently re-mints one and POSTs it here so a scan that outlasts the token
    keeps its Drive auth. Owner-checked; updates the ephemeral per-scan token store that
    scan_file/scan_batch workers re-read on every job."""
    if core.store.get_scan(sid, owner=_owner(request)) is None:
        raise HTTPException(404, "scan not found")
    token = request.headers.get("x-drive-token")
    if not token:
        raise HTTPException(422, "x-drive-token header required")
    _register_scan_tokens(sid, drive=token)
    return {"scan_id": sid, "refreshed": True}


@router.post("/scans/{sid}/sp-token")
def refresh_scan_sp_token(sid: str, request: Request):
    """Refresh the SharePoint token of a RUNNING scan. MSAL tokens expire ~1h; the frontend
    silently re-acquires one and POSTs it here so a scan that outlasts the token keeps its
    SharePoint auth. Owner-checked; updates the ephemeral per-scan token store."""
    if core.store.get_scan(sid, owner=_owner(request)) is None:
        raise HTTPException(404, "scan not found")
    token = request.headers.get("x-sp-token")
    if not token:
        raise HTTPException(422, "x-sp-token header required")
    _register_scan_tokens(sid, sp=token)
    return {"scan_id": sid, "refreshed": True}


@router.delete("/scans/{sid}/tokens")
def clear_scan_tokens(sid: str, request: Request):
    """Clear the token store for a scan — called on sign-out so a running scan's worker does not
    keep stale Drive/SharePoint credentials after the user has signed out. Best-effort: a 404
    (scan finished or never existed) is treated as success since the goal is just to not leave
    tokens around."""
    if core.store.get_scan(sid, owner=_owner(request)) is None:
        return {"scan_id": sid, "cleared": True}
    core.clear_scan_tokens(sid)
    return {"scan_id": sid, "cleared": True}


def _release_timezone(owner: str) -> str:
    """Read the preference; minimal/older adapters use the product's US Central default."""
    from publish import user_release_timezone
    return user_release_timezone(core.store, owner)


def _release_destination(source: str, raw: object) -> dict | None:
    """Normalize one optional provider parent without accepting credentials or URLs."""
    if not isinstance(raw, dict):
        return None
    provider = str(raw.get("provider") or "").strip().lower()
    folder_id = str(raw.get("folder_id") or "").strip()
    folder_name = str(raw.get("folder_name") or "").strip()
    if source == "local" and provider == "local" and raw == {"provider": "local", "folder_id": "root", "folder_name": "Download package"}:
        return dict(raw)
    if (source != "local" and provider != source) or provider not in {"drive", "sharepoint"}:
        raise HTTPException(422, "the Release destination must match this scan's provider")
    if not folder_id or len(folder_id) > 500 or not folder_name or len(folder_name) > 255:
        raise HTTPException(422, "the Release destination needs a valid folder id and name")
    if provider == "sharepoint" and "/" not in folder_id:
        raise HTTPException(422, "a SharePoint destination must identify its document library")
    return {"provider": provider, "folder_id": folder_id, "folder_name": folder_name}


def _preflight_release_destination(request: Request, destination: dict | None) -> dict:
    if destination is None:
        return {"ready": True, "mode": "provider_default",
                "message": "ACP will create the protected Remediated folder at the provider root."}
    if destination["provider"] == "local":
        import blob
        return {"ready": blob.enabled(), "mode": "download", "message": "A ZIP package will be prepared automatically."}
    provider, folder_id = destination["provider"], destination["folder_id"]
    if provider == "drive":
        try:
            svc = core.drive_service(request)
            info = svc.files().get(
                fileId=folder_id,
                fields="id,name,trashed,mimeType,capabilities(canAddChildren)").execute()
            writable = bool((info.get("capabilities") or {}).get("canAddChildren"))
            exists = not bool(info.get("trashed"))
            return {"ready": exists and writable, "credential_valid": True,
                    "folder_reachable": exists, "write_permission": writable,
                    "folder_name": info.get("name") or destination["folder_name"],
                    "message": None if exists and writable else
                    "Google Drive does not allow this account to add files to that folder."}
        except Exception as exc:  # provider errors are returned as actionable preflight, not 500
            return {"ready": False, "credential_valid": False, "folder_reachable": False,
                    "write_permission": False, "message": str(exc)}
    token = request.headers.get("x-sp-token")
    if not token:
        return {"ready": False, "credential_valid": False, "folder_reachable": False,
                "write_permission": False, "message": "Reconnect Microsoft to check this folder."}
    drive_id, _, item_id = folder_id.partition("/")
    try:
        import scanner as _scanner
        import sp_readiness
        info = _scanner._sp_item_exists(token, drive_id, item_id)
        scopes, why_unknown = sp_readiness.token_scopes(token)
        known_write = (sp_readiness._has(scopes, "Files.ReadWrite") or
                       sp_readiness._has(scopes, "Files.ReadWrite.All") or
                       sp_readiness._has(scopes, "Sites.ReadWrite.All"))
        scope_known = scopes is not None
        reachable = bool(info.get("exists"))
        return {"ready": reachable and (known_write or not scope_known),
                "credential_valid": True, "folder_reachable": reachable,
                "write_permission": True if known_write else None if not scope_known else False,
                "permission_assurance": "grant_and_connectivity" if known_write else "connectivity_only",
                "message": (None if reachable and known_write else
                    f"Folder is reachable; the token's write grant could not be inspected ({why_unknown})."
                    if reachable and not scope_known else
                    "Microsoft sign-in is missing a files or sites write grant."
                    if reachable else info.get("error") or "The selected Microsoft folder is unreachable.")}
    except Exception as exc:
        return {"ready": False, "credential_valid": True, "folder_reachable": False,
                "write_permission": None, "message": str(exc)}


def _queue_reports_if_settled(sid: str, owner: str, release_id: str) -> None:
    """Queue follow-up reports once the release settles, without failing the publish.

    Reports cannot be rebuilt for a copy corrected after it was delivered; that refusal is
    currency (surfaced by release_publication as out_of_date), not a failed publication.
    """
    from release_report_delivery import queue_if_release_settled, report_superseded
    try:
        queue_if_release_settled(core.store, sid, owner, release_id)
    except ValueError as exc:
        if not report_superseded(exc):
            raise


@router.post("/scans/{sid}/publish")
def publish_files(sid: str, request: Request, body: dict):
    """Publish one or more re-validated files — ADR 0010 archive-copy, NON-destructive.
    The fixed copy (durable in Blob) is placed beneath one timestamped ``Remediated``
    release root using the immutable source hierarchy; the original is never overwritten.
    Absent provider write support, Blob remains the durable document-of-record.
    Body: {"files": ["fname1", "fname2"]} or {"file": "fname"}."""
    return _publish_release_files(sid, request, body)


def _publish_release_files(sid: str, request: Request, body: dict,
                           remaining_issue_files: set[str] | None = None):
    """publish_files' implementation.

    ``remaining_issue_files`` is an INTERNAL, per-file form of ``allow_remaining_issues`` that
    no HTTP body can supply. /release/republish passes exactly the files whose current copy
    needs the confirmation, so a compliant file in the same request is never recorded as
    released-with-remaining-issues — and all files still go in ONE stage batch (a second
    publish call would be refused by the release stage's single-flight fence).
    """
    scan = core.store.get_scan(sid, owner=_owner(request))
    if scan is None:
        raise HTTPException(404, "scan not found")
    files = body.get("files") or ([body["file"]] if body.get("file") else [])
    if not files:
        raise HTTPException(422, "provide 'file' or 'files' in body")
    owner = _owner(request)
    automatic_release_id = body.get("automatic_release_id")
    if automatic_release_id:
        from automatic_release import validate_publish_request
        try:
            validate_publish_request(core.store, sid, owner, files, body)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
    allow_remaining_issues = body.get("allow_remaining_issues", False)
    if not isinstance(allow_remaining_issues, bool):
        raise HTTPException(422, "allow_remaining_issues must be a boolean")
    per_file_authorization = remaining_issue_files is not None and not allow_remaining_issues
    authorized_files = set(remaining_issue_files or ()) & set(files)

    def allowed(name) -> bool:
        return allow_remaining_issues or name in authorized_files
    expected_artifacts = body.get("expected_artifacts") or {}
    if not isinstance(expected_artifacts, dict):
        raise HTTPException(422, "expected_artifacts must map selected files to corrected digests")
    if any(allowed(f) and (
            not expected_artifacts.get(f) or expected_artifacts[f] !=
            (core.store.get_file_record(sid, f) or {}).get("corrected_sha256")) for f in files):
        raise HTTPException(409, "Confirm each current corrected artifact before releasing with remaining issues")
    if any((core.store.get_file_record(sid, file) or {}).get("corrected_sha256") != digest
           for file, digest in expected_artifacts.items() if file in files):
        raise HTTPException(409, "The authorized corrected artifact changed; confirm again")
    # Remediate's per-document selection is durable scan intent, not merely a frontend filter.
    # Enforce it again at the external-write boundary so a stale browser, crafted request, or
    # retry cannot release a document the operator excluded. With no explicit selection this is
    # intentionally unrestricted, matching the frontend contract.
    from assessment_policy import selected_documents
    document_selection = selected_documents(core.store.get_decisions(sid, owner=owner))
    outside_selection = sorted(set(files) - document_selection) if document_selection is not None else []
    if outside_selection:
        raise HTTPException(status_code=409, detail={
            "code": "document_out_of_scope",
            "message": "One or more documents are outside the Remediate selection.",
            "files": outside_selection,
        })
    owner_email = scan.get("run", {}).get("owner_email") or owner
    import publish as _publish
    from release_artifacts import ReleaseArtifactError, artifact_tag, reuse_state, require_current_record, require_current_source, release_ready, release_review_evidence
    source = scan.get("run", {}).get("source") or "local"
    source_origin = source
    saved_release = core.store.release_for_scan(sid, owner) if source == 'local' else None
    raw_destination = body.get('destination')
    if source == 'local' and saved_release and saved_release.get('source') in {'drive', 'sharepoint'}:
        raw_destination = raw_destination or dict(provider=saved_release['source'], folder_id=saved_release['parent_folder_id'], folder_name=saved_release.get('parent_folder_name') or 'Saved release location')
    destination = _release_destination(source, raw_destination)
    if source == "local" and destination:
        if saved_release and saved_release.get('source') != destination['provider']:
            raise HTTPException(409, 'This release already has a saved provider. Use its original destination.')
        source = destination["provider"]
    if destination:
        destination_check = _preflight_release_destination(request, destination)
        if not destination_check["ready"]:
            raise HTTPException(409, detail={"code": "release_destination_not_ready",
                                            "preflight": destination_check})
    eligible = [row for row in scan.get("files", [])
                if release_ready(row, allowed(row.get("file")))
                and (document_selection is None or row.get("file") in document_selection)]
    try:
        preferred_folder_name = _publish.normalize_release_name(
            body.get("release_folder_name"), field="Release folder name")
    except _publish.UnsafeReleasePath as exc:
        raise HTTPException(422, str(exc)) from exc
    if source == "sharepoint":
        preferred_folder_name = _publish.sharepoint_release_name(
            preferred_folder_name, owner, timezone_name=_release_timezone(owner))
    elif not preferred_folder_name:
        release_tz = _release_timezone(owner)
        preferred_folder_name = _publish.release_folder_name(timezone_name=release_tz, owner_email=owner)
    execution_options = {"preferred_folder_name": preferred_folder_name}
    if destination and destination["provider"] != "local":
        execution_options.update(parent_folder_id=destination["folder_id"],
                                 parent_folder_name=destination["folder_name"])
    release = core.store.ensure_release_execution(
        sid, owner, source, len(eligible), **execution_options)
    release_id = release["id"]
    if "expected_destination" in body and release.get("parent_folder_id") != (body["expected_destination"] or {}).get("folder_id"):
        # A worker or another tab may have frozen this execution after the UI
        # loaded its preview. Keep the authorization guard, but return the saved
        # destination so the caller can present it for a fresh confirmation.
        saved_parent = release.get("parent_folder_id")
        raise HTTPException(409, detail={
            "code": "release_destination_changed",
            "message": "This release has a saved destination. Review it and confirm publishing again.",
            "release_id": release_id,
            "release_folder_name": release["folder_name"],
            "destination": ({"provider": source, "folder_id": saved_parent,
                             "folder_name": release.get("parent_folder_name") or "Saved release location"}
                            if saved_parent else None),
        })
    created_at = release["created_at"]
    folder_name = release["folder_name"]
    drive_token = request.headers.get("x-drive-token")
    sp_token = request.headers.get("x-sp-token")
    # Provider Release is worker-backed: make the short-lived delegated credential available
    # to any remediation replica without ever persisting it in Postgres or a job payload.
    if source == "sharepoint" and sp_token:
        _register_scan_tokens(sid, sp=sp_token)
    if source == "drive" and drive_token:
        _register_scan_tokens(sid, drive=drive_token)
    drive_svc = None
    if source == "drive" and drive_token:
        try:
            import handlers
            drive_svc = handlers._drive_client(drive_token)
        except Exception:
            drive_svc = None
    remaining_issue_decisions = {}
    if allow_remaining_issues or authorized_files:
        import json
        for filename in files:
            if not allowed(filename):
                continue
            current = core.store.get_file_record(sid, filename) or {}
            remaining_issue_decisions[filename] = json.dumps({
                **release_review_evidence(current, owner=owner, allow_remaining_issues=True, store=core.store, scan_id=sid),
                "artifact_digest": artifact_tag(expected_artifacts[filename]), "release_id": release_id})

    def log_remaining_issue_decisions(names) -> None:
        for filename in names:
            if filename in remaining_issue_decisions:
                core.store.log_decision(owner, "release.remaining_issues_authorized", scan_id=sid,
                                        file=filename, detail=remaining_issue_decisions[filename])
    if source not in {"sharepoint", "drive"}:
        # Synchronous delivery: the authorization precedes the side effect it authorizes. The
        # provider path records it at admission instead (below), atomically with the work.
        log_remaining_issue_decisions(files)
    results = []
    folder_cache = {}
    synchronous_execution = None
    if source not in {"sharepoint", "drive"}:
        ensure_sync = getattr(core.store, "ensure_synchronous_stage_execution", None)
        if callable(ensure_sync):
            import hashlib, json
            requested = sorted(set(str(f) for f in files))
            snapshot_id, input_manifest_id = _sealed_stage_input(sid, "remediate", release_id)
            fingerprint = hashlib.sha256(json.dumps({
                "files": requested, "source": source, "release_id": release_id,
                "allow_remaining_issues": (sorted(authorized_files) if per_file_authorization
                                           else allow_remaining_issues),
                "artifacts": [(name, (core.store.get_file_record(sid, name) or {}).get("corrected_sha256"),
                               (core.store.get_file_record(sid, name) or {}).get("remediated_at"))
                              for name in requested],
            }, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
            try:
                synchronous_execution = ensure_sync(
                    scan_id=sid, stage="release", input_ids=requested,
                    input_snapshot_id=snapshot_id, request_fingerprint=fingerprint,
                    input_manifest_id=input_manifest_id)
            except ActiveStageExecutionError as exc:
                raise HTTPException(status_code=409, detail={
                    "code": "stage_execution_active", "stage": exc.stage,
                    "active_batch_id": exc.batch_id,
                    "message": "A different release is already active. Wait for it to finish or stop it.",
                }) from exc

    def finish_synchronous(filename: str, outcome: str, result: dict) -> None:
        if not synchronous_execution:
            return
        core.store.finish_synchronous_stage_item(
            synchronous_execution["execution_id"], filename, outcome=outcome, result=result)

    # A provider release can contain hundreds of documents. Running external traffic inside
    # this HTTP request makes the browser/proxy timeout the unit of durability. Queue one stable
    # job per corrected copy instead; completed documents are reused by the handler and a worker
    # restart resumes from the durable queue.
    if source in {"sharepoint", "drive"}:
        provider_token = sp_token if source == "sharepoint" else drive_token
        if not provider_token:
            provider_name = "SharePoint" if source == "sharepoint" else "Google Drive"
            raise HTTPException(403, f"{provider_name} publishing requires a current write grant.")
        payloads = []
        for f in files:
            record = core.store.get_file_record(sid, f)
            if not release_ready(record, allowed(f)):
                result = {"file": f, "original_relative_path": None,
                          "released_relative_path": None, "status": "failed",
                          "failure_category": "not_approved",
                          "explanation": "Only approved corrected copies can be released.",
                          "created": False}
                core.store.record_release_document(release_id, owner, result)
                results.append(result)
                continue
            saved = core.store.get_release_document(release_id, f, owner)
            digest = record.get("corrected_sha256")
            if expected_artifacts.get(f) and expected_artifacts[f] != digest:
                results.append({"file": f, "status": "failed", "failure_category": "artifact_changed",
                                "explanation": "The authorized corrected artifact changed; confirm again"})
                continue
            state = reuse_state(saved, digest) if digest else "unresolved" if saved else "new"
            if state == "unresolved":
                results.append({"file": f, "status": "failed", "failure_category": "delivery_version_unresolved",
                                "explanation": "Prior delivery has no exact artifact digest. Reconcile that delivery before retrying."})
                continue
            if state == "reuse":
                try:
                    actual_digest = _publish.remediated_content_digest(owner, sid, f)
                    require_current_record(core.store, sid, f, actual_digest, record.get("remediated_at"), owner=owner, allow_remaining_issues=allowed(f))
                    require_current_source(source_origin, record, sp_token=sp_token, drive_service=drive_svc)
                    require_current_record(core.store, sid, f, actual_digest, record.get("remediated_at"), owner=owner, allow_remaining_issues=allowed(f))
                    if not actual_digest or reuse_state(saved, actual_digest) != "reuse":
                        raise ValueError("Corrected bytes changed; verify the new copy before Release.")
                except Exception as exc:
                    results.append({"file": f, "status": "failed", "failure_category": "artifact_changed",
                                    "explanation": str(exc)})
                    continue
                results.append({"file": f, "status": "published", "artifact_digest": saved.get("artifact_digest"),
                                "published_at": saved.get("published_at"),
                                "published_url": saved.get("released_document_url"),
                                "released_relative_path": saved.get("destination_relative_path"),
                                "verification": saved.get("verification"), "created": False})
                continue
            queued = {"file": f,
                      "source_document_id": record.get("drive_file_id") or f,
                      "original_relative_path": (record.get("source_relative_path")
                                                 or record.get("parent_folder") or f),
                      "released_relative_path": None, "status": "queued", "created": False}
            # Written at admission, with the job (see below) — never ahead of it.
            results.append(queued)
            payloads.append({"scan_id": sid, "release_id": release_id,
                             "file": f, "owner": owner,
                             **({"allow_remaining_issues": True,
                                 "release_review": release_review_evidence(record, owner=owner, allow_remaining_issues=True, store=core.store, scan_id=sid)} if allowed(f) else {}),
                             "artifact_digest": artifact_tag(digest) if digest else None,
                             "remediated_at": record.get("remediated_at"),
                             **({"automatic_release_id": automatic_release_id} if automatic_release_id else {})})
        execution = None
        if payloads:
            import hashlib, json
            requested = sorted(p["file"] for p in payloads)
            fingerprint_inputs = [(p["file"], p.get("artifact_digest"), p.get("remediated_at"), p.get("allow_remaining_issues")) for p in payloads]
            if automatic_release_id:
                fingerprint_inputs = {"artifacts": fingerprint_inputs, "automatic_release_id": automatic_release_id}
            fingerprint = hashlib.sha256(json.dumps(fingerprint_inputs, sort_keys=True).encode()).hexdigest()
            snapshot_id, input_manifest_id = _sealed_stage_input(sid, "remediate", release_id)
            # ADMISSION IS ATOMIC. The queued receipts, the remaining-issue authorizations and the
            # stage enqueue commit together or not at all. Writing the receipts first let a request
            # that then lost the release stage's single-flight fence (409) leave a document reading
            # 'publishing' with no job behind it, and an authorization for work never started — a
            # concurrent request makes that reachable no matter what the caller checked beforehand.
            # It also means a worker can never see a job whose queued receipt is not yet written.
            #
            # MANUAL REQUESTS ONLY. An automatic request's enqueue locks its authorization row, and
            # the automatic tick (automatic_release.advance) locks that row BEFORE writing release
            # receipts; holding the release row first here would invert that order into a Postgres
            # deadlock. Automatic admission keeps its previous, unwrapped behaviour.
            import contextlib
            admission = None if automatic_release_id else getattr(core.store, "transaction", None)
            with admission() if callable(admission) else contextlib.nullcontext():
                log_remaining_issue_decisions([p["file"] for p in payloads])
                for queued in results:
                    if queued.get("status") == "queued":
                        core.store.record_release_document(release_id, owner, queued)
                if source == "drive":
                    try:
                        queue_drive = (core.store.enqueue_automatic_drive_release if automatic_release_id
                                       else core.store.enqueue_manual_drive_release)
                        options = {} if automatic_release_id else {"owner": owner}
                        execution = queue_drive(
                            sid, payloads, snapshot_id=snapshot_id,
                            request_fingerprint=fingerprint, input_manifest_id=input_manifest_id, **options)
                    except (ValueError, ActiveStageExecutionError) as exc:
                        raise HTTPException(409, str(exc)) from exc
                elif automatic_release_id:
                    try:
                        execution = core.store.enqueue_automatic_sharepoint_release(
                            sid, payloads, snapshot_id=snapshot_id,
                            request_fingerprint=fingerprint, input_manifest_id=input_manifest_id)
                    except (ValueError, ActiveStageExecutionError) as exc:
                        raise HTTPException(409, str(exc)) from exc
                else:
                    execution = _enqueue_stage_batch(
                        sid, "release", "publish_file", payloads,
                        snapshot_id=snapshot_id, request_fingerprint=fingerprint,
                        input_manifest_id=input_manifest_id)
        if not automatic_release_id:
            _queue_reports_if_settled(sid, owner, release_id)
        status = core.store.release_status(release_id, owner)
        return {"release_id": release_id, "release_folder_id": None,
                "release_folder_name": folder_name, "release_folder_url": None,
                "parent_folder_id": release.get("parent_folder_id"),
                "parent_folder_name": release.get("parent_folder_name"),
                "release_folders": status.get("roots", []),
                "documents_total": status.get("documents_total", 0),
                "published_count": status.get("published", 0),
                "failed": status.get("failed", 0), "remaining": status.get("remaining", 0),
                "published": results, "queued": len(payloads),
                "batch_id": execution.get("batch_id") if execution else None}
    for f in files:
        record = core.store.get_file_record(sid, f)
        if not release_ready(record, allowed(f)):
            result = {"file": f, "original_relative_path": None,
                            "released_relative_path": None, "status": "failed",
                            "failure_category": "not_approved",
                            "explanation": "Only approved corrected copies can be released.",
                            "created": False}
            core.store.record_release_document(release_id, owner, result)
            results.append(result)
            finish_synchronous(f, "failed", result)
            continue
        source_path = record.get("source_relative_path") or record.get("parent_folder") or f
        saved = core.store.get_release_document(release_id, f, owner)
        try:
            source_id = record.get("drive_file_id") or f
            root = None
            reservation = None
            # Release attests to the bytes that are durable now, not merely the digest recorded
            # when remediation ran.  In particular, local Release has no provider write whose
            # read would otherwise prove the Blob artifact still exists.
            content_digest = _publish.remediated_content_digest(owner, sid, f)
            if not content_digest:
                raise IOError("corrected content was unavailable")
            if expected_artifacts.get(f) and expected_artifacts[f] != content_digest:
                raise ReleaseArtifactError("The authorized corrected artifact changed; confirm again")
            record = require_current_record(core.store, sid, f, content_digest, record.get("remediated_at"), owner=owner, allow_remaining_issues=allowed(f))
            require_current_source(source_origin, record, drive_service=drive_svc, sp_token=sp_token)
            record = require_current_record(core.store, sid, f, content_digest, record.get("remediated_at"), owner=owner, allow_remaining_issues=allowed(f))
            state = reuse_state(saved, content_digest)
            if state == "unresolved":
                raise ReleaseArtifactError("Prior delivery has no exact artifact digest. Reconcile that delivery before retrying.", category="delivery_version_unresolved")
            if state == "reuse":
                results.append({"file": f, "source_document_id": saved.get("source_document_id"),
                                "original_relative_path": saved.get("source_relative_path"),
                                "released_relative_path": saved.get("destination_relative_path"),
                                "status": "published", "published_at": saved.get("published_at"),
                                "published_url": saved.get("released_document_url"),
                                "verification": saved.get("verification"),
                                "released_document_id": saved.get("released_document_id"),
                                "corrected_checksum": saved.get("corrected_checksum"),
                                "artifact_digest": saved.get("artifact_digest"),
                                "created": False})
                finish_synchronous(f, "completed", results[-1])
                continue
            from release_candidate_assessment import assess_candidate
            candidate_assessment = assess_candidate(
                core.store, sid, f, owner, content_digest, record.get("remediated_at"),
                allow_remaining_issues=allowed(f), release_id=release_id)
            execution_id = (synchronous_execution or {}).get("execution_id")
            work_item_id = ((synchronous_execution or {}).get("items") or {}).get(f)
            publication = None
            if source == "drive":
                if drive_svc is None:
                    raise PermissionError("Google Drive publishing requires a current write grant.")
                location = "google:me"
                folders, released_name = _publish.normalize_relative_path(source_path, f)
                planned_destination = f"google:me:{release_id}:{'/'.join([*folders, released_name])}"
                if execution_id:
                    reservation = core.store.reserve_side_effect(
                        execution_id=execution_id, work_item_id=work_item_id,
                        effect_type="drive.publish", destination=planned_destination,
                        content_digest=content_digest, worker_id=f"sync-release:{release_id}")
                    if reservation.get("reused") and reservation.get("status") == "completed":
                        receipt = dict(reservation.get("receipt") or {})
                        publication = {"id": receipt.get("provider_id"), "url": receipt.get("url"),
                                       "created": bool(receipt.get("created")),
                                       "checksum": receipt.get("checksum"),
                                       "verified": bool(receipt.get("verified", True))}
                    elif not reservation.get("acquired"):
                        queued = {"file": f, "source_document_id": source_id,
                                  "original_relative_path": source_path,
                                  "released_relative_path": None, "status": "queued",
                                  "created": False}
                        core.store.record_release_document(release_id, owner, queued)
                        results.append(queued)
                        continue
                    else:
                        publication = None
                root = core.store.get_release_root(release_id, location, owner)
                if not root:
                    from datetime import datetime
                    detail = _publish.ensure_published_folder(
                        drive_svc, release_id,
                        released_at=datetime.fromisoformat(created_at.replace("Z", "+00:00")),
                        folder_name=folder_name,
                        parent_id=release.get("parent_folder_id"),
                        return_details=True)
                    root = core.store.record_release_root(
                        release_id, owner, "drive", location, detail["id"],
                        detail["name"], detail.get("url"))
                if publication is None:
                    publication = _publish.archive_copy_publish(
                        drive_svc, root["folder_id"], owner_email, sid, f,
                        relative_path=source_path, source_id=source_id,
                        folder_cache=folder_cache, return_details=True, expected_digest=content_digest)
            elif source == "sharepoint":
                if not sp_token:
                    raise PermissionError("SharePoint publishing requires a current write grant.")
                drive_id = record.get("drive_id")
                location = f"graph:{drive_id or 'me'}"
                root = core.store.get_release_root(release_id, location, owner)
                if not root:
                    claimed_name = core.store.claim_release_root_name(
                        release_id, owner, "sharepoint", location, folder_name)
                    detail = _publish.ensure_sharepoint_release_folder(
                        sp_token, drive_id, release_id, claimed_name)
                    root = core.store.record_release_root(
                        release_id, owner, "sharepoint", location, detail["id"],
                        detail["name"], detail.get("url"))
                publication = _publish.archive_copy_publish_sharepoint(
                    sp_token, drive_id, root["folder_id"], owner_email, release_id,
                    sid, f, source_path, source_id, folder_cache)
                folders, _ = _publish.sharepoint_relative_path(source_path, f)
                released_name = publication.get("filename") if publication else f
            else:
                publication = None
                folders, released_name = _publish.normalize_relative_path(source_path, f)
            if source in ("drive", "sharepoint") and publication is None:
                raise IOError("corrected content was unavailable")
            url = publication.get("url") if publication else record.get("published_url")
            lineage = core.store.release_finding_lineage(execution_id, f) if execution_id else None
            if source == "drive" and reservation and reservation.get("acquired"):
                receipt = {"provider_id": publication.get("id"), "url": url,
                           "created": bool(publication.get("created")),
                           "checksum": publication.get("checksum"),
                           "content_sha256": content_digest,
                           "filename": released_name,
                           "verified": bool(publication.get("verified", True))}
                if lineage is not None:
                    receipt["finding_lineage"] = lineage
                receipt["corrected_copy_assessment"] = candidate_assessment
                receipt["release_review"] = release_review_evidence(record, owner=owner, allow_remaining_issues=allowed(f), store=core.store, scan_id=sid)
                core.store.finalize_side_effect(
                    reservation["effect_id"], reservation["reservation_token"], receipt)
            elif source == "local" and execution_id:
                receipt = {"checksum": content_digest, "filename": f, "verified": True}
                if lineage is not None:
                    receipt["finding_lineage"] = lineage
                receipt["corrected_copy_assessment"] = candidate_assessment
                receipt["release_review"] = release_review_evidence(record, owner=owner, allow_remaining_issues=allowed(f), store=core.store, scan_id=sid)
                core.store.record_side_effect_receipt(
                    execution_id=execution_id, work_item_id=work_item_id,
                    effect_type="blob.release",
                    destination=f"blob:remediated:{owner}/{sid}/{f}",
                    content_digest=content_digest, receipt=receipt)
            ts = core.store.record_publish(sid, f, published_url=url)
            result = {"file": f, "source_document_id": source_id,
                            "original_relative_path": source_path,
                            "released_relative_path": "/".join([*folders, released_name]),
                            "status": "published", "published_at": ts,
                            "artifact_digest": artifact_tag(content_digest),
                            "published_url": url,
                            "verification": "content verified" if publication else "durable Blob copy",
                            "released_document_id": publication.get("id") if publication else None,
                            "corrected_checksum": publication.get("checksum") if publication else None,
                            "created": publication.get("created", False) if publication else False}
            core.store.record_release_document(release_id, owner, result)
            results.append(result)
            finish_synchronous(f, "completed", result)
        except ReleaseArtifactError as exc:
            result = {"file": f, "status": "failed", "original_relative_path": source_path,
                      "failure_category": exc.category, "explanation": str(exc), "created": False,
                      "artifact_digest": artifact_tag(content_digest) if content_digest and exc.category in {'release_assessment_remaining', 'release_assessment_unavailable', 'corrected_copy_unreadable'} else None}
            core.store.record_release_document(release_id, owner, result)
            results.append(result)
            finish_synchronous(f, "failed", result)
        except _publish.UnsafeReleasePath as exc:
            result = {"file": f, "source_document_id": record.get("drive_file_id") or f,
                            "original_relative_path": source_path,
                            "released_relative_path": None, "status": "failed",
                            "failure_category": "invalid_source_path",
                            "explanation": str(exc), "created": False}
            core.store.record_release_document(release_id, owner, result)
            results.append(result)
            finish_synchronous(f, "failed", result)
        except PermissionError as exc:
            uncertain = bool(reservation and reservation.get("acquired"))
            diagnostic = _log_release_provider_error(
                exc, scan_id=sid, file=f, release_id=release_id,
                provider=source, uncertain=uncertain)
            status_hint = (f" (HTTP {diagnostic['http_status']})"
                           if "http_status" in diagnostic else "")
            result = {"file": f, "source_document_id": record.get("drive_file_id") or f,
                            "original_relative_path": source_path,
                            "released_relative_path": None,
                            "status": "queued" if uncertain else "failed",
                            **({} if uncertain else {
                                "failure_category": "provider_permission_denied"}),
                            "explanation": (("The provider outcome is uncertain. Access was denied; "
                                             "check the connection and destination permissions. "
                                             "Verify the existing copy before retrying."
                                             if uncertain else "Access to the release destination was denied. "
                                             "Reconnect the provider and check destination permissions.")
                                            + status_hint), "created": False}
            core.store.record_release_document(release_id, owner, result)
            results.append(result)
            if not uncertain:
                finish_synchronous(f, "failed", result)
        except Exception as exc:
            uncertain = bool(reservation and reservation.get("acquired"))
            diagnostic = _log_release_provider_error(
                exc, scan_id=sid, file=f, release_id=release_id,
                provider=source, uncertain=uncertain)
            status_hint = (f" (HTTP {diagnostic['http_status']})"
                           if "http_status" in diagnostic else "")
            result = {"file": f, "source_document_id": record.get("drive_file_id") or f,
                            "original_relative_path": source_path,
                            "released_relative_path": None,
                            "status": "queued" if uncertain else "failed",
                            **({} if uncertain else {
                                "failure_category": "provider_write_failed"}),
                            "explanation": (("The provider outcome is uncertain. Delivery could not be "
                                             "confirmed; verify the existing copy before retrying."
                                             if uncertain else "The corrected copy could not be "
                                             "verified at the release destination. Retry this document.")
                                            + status_hint),
                            "created": False}
            core.store.record_release_document(release_id, owner, result)
            results.append(result)
            if not uncertain:
                finish_synchronous(f, "failed", result)
    if not automatic_release_id:
        _queue_reports_if_settled(sid, owner, release_id)
    status = core.store.release_status(release_id, owner)
    roots = status.get("roots", []) if status else []
    first_root = roots[0] if roots else None
    return {"release_id": release_id,
            "release_folder_id": first_root.get("folder_id") if first_root else None,
            "release_folder_name": status.get("folder_name") if status else folder_name,
            "release_folder_url": first_root.get("folder_url") if first_root else None,
            "parent_folder_id": status.get("parent_folder_id") if status else None,
            "parent_folder_name": status.get("parent_folder_name") if status else None,
            "release_folders": roots, "documents_total": status.get("documents_total", 0),
            "published_count": status.get("published", 0), "failed": status.get("failed", 0),
            "remaining": status.get("remaining", 0), "published": results,
            **({"batch_id": synchronous_execution["execution_id"]}
               if synchronous_execution else {})}


@router.get("/releases")
def get_release_history(request: Request, limit: int = Query(50, ge=1, le=100)):
    """Owner-scoped release executions across scans, newest first."""
    owner = _owner(request)
    releases = core.store.list_release_history(owner, limit=limit)
    from release_publication import project as publication_currency
    currency = {release["id"]: publication_currency(core.store, release["scan_id"], owner, release)
                for release in releases}
    return {"releases": [{
        "release_id": release["id"],
        "scan_id": release["scan_id"],
        "actor": release["owner_email"],
        "source": release.get("source"),
        "folder_name": release.get("folder_name"),
        "parent_folder_id": release.get("parent_folder_id"),
        "parent_folder_name": release.get("parent_folder_name"),
        "status": release.get("status"),
        "created_at": release.get("created_at"),
        "updated_at": release.get("updated_at"),
        "documents_total": release.get("documents_total", 0),
        "published": release.get("published", 0),
        "failed": release.get("failed", 0),
        "remaining": release.get("remaining", 0),
        "destinations": [{
            "provider": root.get("provider"),
            "location": root.get("provider_location"),
            "folder_name": root.get("folder_name"),
            "folder_url": root.get("folder_url"),
        } for root in release.get("roots", [])],
        "documents": [{
            "file": row.get("file"),
            "status": row.get("status"),
            "destination_path": row.get("destination_relative_path"),
            "released_url": row.get("released_document_url"),
            "created": bool(row.get("created_result")),
            "checksum": row.get("corrected_checksum"),
            "artifact_digest": row.get("artifact_digest"),
            "verification": row.get("verification"),
            "failure_category": row.get("failure_category"),
            "explanation": row.get("explanation"),
            "published_at": row.get("published_at"),
            "publication_state": row.get("publication_state"),
        } for row in currency[release["id"]]["documents"]],
        "manifest_url": f"/api/scans/{release['scan_id']}/release/manifest",
        "publication_state": currency[release["id"]]["publication"]["state"],
        "publication": currency[release["id"]]["publication"],
    } for release in releases]}


_PUBLICATION_FIELDS = ("publication_state", "published_artifact_digest", "current_artifact_digest")


def _release_status_payload(sid: str, owner: str) -> dict:
    """GET /scans/{sid}/release, including the server-authored publication currency.

    The currency fields are added HERE, never to store.release_status()['documents']: that
    list is what release_report_delivery fingerprints, and every frozen report bundle would
    otherwise read as out of date.
    """
    from release_publication import project as publication_currency
    status = core.store.release_for_scan(sid, owner)
    if status is None:
        return {"release_id": None, "documents_total": 0, "published": 0,
                "failed": 0, "remaining": 0, "roots": [], "documents": [],
                "publication": publication_currency(core.store, sid, owner, None)["publication"]}
    currency = publication_currency(core.store, sid, owner, status)
    fields = {row["file"]: {key: row[key] for key in _PUBLICATION_FIELDS} for row in currency["documents"]}
    from missing_corrected_copy import project_documents
    documents = project_documents(core.store, sid, owner, status['documents'])
    from saved_copy_verification import project_documents as verification_documents
    documents = verification_documents(core.store, sid, owner, documents)
    documents = [{**row, **fields.get(row["file"], {})} for row in documents]
    return {"release_id": status["id"], "release_folder_name": status["folder_name"],
            "created_at": status["created_at"], "status": status["status"],
            "parent_folder_id": status.get("parent_folder_id"),
            "parent_folder_name": status.get("parent_folder_name"),
            "documents_total": status["documents_total"], "published": status["published"],
            "failed": status["failed"], "remaining": status["remaining"],
            "roots": status["roots"], "documents": documents,
            "publication": currency["publication"]}


@router.get("/scans/{sid}/release")
def get_release_status(sid: str, request: Request):
    """Return the durable structured-release state for this owner and scan."""
    owner = _owner(request)
    if core.store.get_scan(sid, owner=owner) is None:
        raise HTTPException(404, "scan not found")
    return _release_status_payload(sid, owner)


class ReleaseRepublishRequest(BaseModel):
    # file -> the exact 64-hex corrected digest the user confirmed publishing again.
    expected_artifacts: dict[str, str]
    allow_remaining_issues: StrictBool = False
    # The exact files whose remaining-issues confirmation the user ticked. A file the server finds
    # needs confirmation but that is NOT listed is refused, and an omitted list confirms nothing,
    # so a file rescored as non-compliant after the page loaded is never authorized by another
    # file's checkbox or by the request-wide flag.
    remaining_issue_files: list[str] | None = None


REPUBLISH_UNRESOLVED = ("The delivered copy has no exact artifact identity, so ACP cannot tell whether it is "
                        "current. Reconcile that delivery before publishing again.")


def _republish_blocked(files: list[str], message: str):
    return HTTPException(409, detail={"code": "republish_blocked", "files": files, "message": message})


@router.post("/scans/{sid}/release/republish")
def republish_release(sid: str, body: ReleaseRepublishRequest, request: Request):
    """Publish the CURRENT corrected copy of documents whose delivered copy is out of date.

    One explicit, digest-bound action. It never bypasses publish_files: selection, readiness,
    digest confirmation, the saved destination, provider tokens and the stage batch are all
    enforced there. Remaining-issue authorization covers only the exact digests in this
    request — the authorization given for the previously published copy is never reused.
    """
    import json
    import release_publication
    from release_artifacts import artifact_tag, release_ready
    owner = _owner(request)
    if core.store.get_scan(sid, owner=owner) is None:
        raise HTTPException(404, "scan not found")
    release = core.store.release_for_scan(sid, owner)
    if release is None:
        raise HTTPException(404, detail={"code": "release_not_found",
                                         "message": "This scan has no release to update."})
    expected = dict(body.expected_artifacts)
    if not expected:
        raise HTTPException(422, "confirm at least one corrected document to publish again")
    if any(not re.fullmatch(r"[0-9a-f]{64}", str(digest)) for digest in expected.values()):
        raise HTTPException(422, "expected_artifacts must map each file to its 64-hex corrected digest")
    files = sorted(expected)
    currency = release_publication.project(core.store, sid, owner, release)
    by_file = {row["file"]: row for row in currency["documents"]}
    records = core.store.get_file_records(sid, owner=owner, files=files)
    changed = [f for f in files if (records.get(f) or {}).get("corrected_sha256") != expected[f]]
    if changed:
        raise HTTPException(409, detail={
            "code": "artifact_changed", "files": changed,
            "message": "The corrected copy changed since you confirmed it. Review the current copy and confirm again."})
    state = {f: (by_file.get(f) or {}).get("publication_state") for f in files}
    already_current = [f for f in files if state[f] == "current"
                       and by_file[f]["published_artifact_digest"] == artifact_tag(expected[f])]
    publishing = [f for f in files if state[f] == "publishing"]
    # ANY document of this release in flight blocks a new request: the release stage admits one
    # batch at a time, and publish_files would only discover that fence after writing queued rows
    # and authorization decisions for files it then cannot enqueue.
    elsewhere = sorted(row["file"] for row in currency["documents"]
                       if row["publication_state"] == "publishing" and row["file"] not in expected)
    if elsewhere:
        raise _republish_blocked(elsewhere, release_publication.REPUBLISH_IN_FLIGHT)
    if publishing:
        active = release_publication.active_publish_digests(core.store, sid, owner, release["id"])
        if (set(publishing) | set(already_current) == set(files)
                and all(artifact_tag(expected[f]) in active.get(f, ()) for f in publishing)):
            # A repeated click while this exact copy is being delivered: nothing new to start.
            return {"result": "already_publishing", "already_current": False, "already_publishing": True,
                    "republished": [], "results": [], "batch_id": None,
                    "release": _release_status_payload(sid, owner)}
        raise _republish_blocked(publishing, release_publication.REPUBLISH_IN_FLIGHT)
    if set(already_current) == set(files):
        return {"result": "already_current", "already_current": True, "already_publishing": False,
                "republished": [], "results": [], "batch_id": None,
                "release": _release_status_payload(sid, owner)}
    targets = [f for f in files if f not in already_current]
    unresolved = [f for f in targets if state[f] == "identity_unknown"]
    if unresolved:
        raise _republish_blocked(unresolved, REPUBLISH_UNRESOLVED)
    not_stale = [f for f in targets if state[f] != "out_of_date"]
    if not_stale:
        raise _republish_blocked(not_stale, "Only documents whose delivered copy is out of date can be "
                                            "published again here.")
    not_ready = [f for f in targets if not release_ready(records.get(f), True)]
    if not_ready:
        raise _republish_blocked(not_ready, release_publication.REPUBLISH_NOT_READY)
    unapplied = [f for f in targets if core.store.count_unapplied_approved_values(sid, f)]
    if unapplied:
        raise _republish_blocked(unapplied, release_publication.REPUBLISH_UNAPPLIED)
    stale = {row["file"]: row for row in currency["publication"]["out_of_date"]}
    needs_confirmation = [f for f in targets if stale[f]["requires_remaining_issue_confirmation"]]
    # Consent is per file and never inferred: an omitted list confirms NOTHING. A request-wide
    # boolean must not expand into the server's current idea of which files need confirmation.
    confirmed = set(body.remaining_issue_files or ()) if body.allow_remaining_issues else set()
    unconfirmed = [f for f in needs_confirmation if f not in confirmed]
    if unconfirmed:
        raise HTTPException(409, detail={
            "code": "remaining_issues_confirmation_required", "files": unconfirmed,
            "message": "The updated copy still has remaining issues. Confirm publishing it with those issues."})
    # Refuse a missing provider grant HERE, before anything reaches the immutable decision log:
    # publish_files would refuse it too, but only after writing its own authorization rows.
    provider = release.get("source")
    if provider in {"sharepoint", "drive"} and not request.headers.get(
            "x-sp-token" if provider == "sharepoint" else "x-drive-token"):
        name = "SharePoint" if provider == "sharepoint" else "Google Drive"
        raise HTTPException(403, f"{name} publishing requires a current write grant.")
    # ONE delegation (a second publish call would hit the release stage's single-flight fence),
    # with the remaining-issue authorization scoped to the server-computed files that need it.
    published = _publish_release_files(sid, request, {
        "files": targets, "expected_artifacts": {f: expected[f] for f in targets},
        "allow_remaining_issues": False,
        "expected_destination": {"folder_id": release.get("parent_folder_id")},
    }, remaining_issue_files=set(needs_confirmation))
    results = published.get("published", [])
    admitted = {row["file"] for row in results if row.get("status") in {"queued", "published"}}
    refused = [{"file": row["file"], "status": row.get("status"),
                "failure_category": row.get("failure_category"), "explanation": row.get("explanation")}
               for row in results if row.get("file") in targets and row["file"] not in admitted]
    if not admitted:
        # Nothing was started — e.g. the copy changed between the check above and the provider
        # loop. That is a refusal, not a success the page should start following.
        changed_late = [row["file"] for row in refused if row["failure_category"] == "artifact_changed"]
        raise HTTPException(409, detail={
            "code": "artifact_changed" if changed_late else "republish_refused",
            "files": changed_late or [row["file"] for row in refused],
            "results": refused,
            "message": ("The corrected copy changed while publishing was starting. Review the current copy and confirm again."
                        if changed_late else "The updated copy could not be published.")})
    # Audit what was actually admitted, after the fact: a request refused inside publish_files
    # must not leave "authorized to replace V1" in the immutable log.
    for f in targets:
        if f not in admitted:
            continue
        previous = by_file[f]
        core.store.log_decision(owner, "release.republish_authorized", scan_id=sid, file=f, detail=json.dumps({
            "release_id": release["id"],
            "previous_artifact_digest": previous["published_artifact_digest"],
            "previous_published_at": previous.get("published_at"),
            "previous_url": previous.get("released_document_url"),
            "artifact_digest": artifact_tag(expected[f]),
            # Only where it is actually applied: a compliant file is not authorized with issues.
            "allow_remaining_issues": f in needs_confirmation,
        }))
    return {"result": "republished", "already_current": False, "already_publishing": False,
            "republished": sorted(admitted), "refused": refused,
            "results": results, "batch_id": published.get("batch_id"),
            "release": _release_status_payload(sid, owner)}


class SavedCopyVerificationRequest(BaseModel):
    file: str
    corrected_sha256: str
    remediated_at: str


@router.post("/scans/{sid}/verify-saved-copy")
def retry_saved_copy_verification(sid: str, body: SavedCopyVerificationRequest, request: Request):
    owner = _owner(request)
    if core.store.get_scan(sid, owner=owner) is None:
        raise HTTPException(404, "scan not found")
    from saved_copy_verification import verify_saved_copy
    from release_artifacts import ReleaseArtifactError
    try:
        evidence = verify_saved_copy(core.store, sid, owner, body.file,
                                     body.corrected_sha256, body.remediated_at)
    except ReleaseArtifactError as error:
        raise HTTPException(409, detail={"category": error.category, "message": str(error)})
    return {"scan_id": sid, "file": body.file, "corrected_copy_assessment": evidence}


class ReleasePreviewRequest(BaseModel):
    files: list[str]
    release_folder_name: str | None = None
    preserve_hierarchy: bool = True
    destination: dict | None = None
    allow_remaining_issues: StrictBool = False
    expected_artifacts: dict[str, str] = {}


@router.post("/scans/{sid}/release/preview")
def preview_release_destination(sid: str, request: Request, body: ReleasePreviewRequest):
    """Resolve the destination plan without creating folders, files, or release records."""
    scan = core.store.get_scan(sid, owner=_owner(request))
    if scan is None:
        raise HTTPException(404, "scan not found")
    selected = list(dict.fromkeys(name for name in body.files if name))
    if not selected:
        raise HTTPException(422, "select at least one corrected file")
    from release_artifacts import release_ready, release_review_evidence
    import publish as _publish
    try:
        requested_name = _publish.normalize_release_name(
            body.release_folder_name, field="Release folder name")
    except _publish.UnsafeReleasePath as exc:
        raise HTTPException(422, str(exc)) from exc
    owner = _owner(request)
    status = core.store.release_for_scan(sid, owner)
    source = (scan.get("run") or {}).get("source") or "local"
    source_origin = source
    destination_config = _release_destination(source_origin, body.destination)
    if source_origin == 'local':
        source = (status or {}).get('source') or (destination_config or {}).get('provider') or source
    if status:
        folder_name = status["folder_name"]
        folder_state = "existing"
    else:
        if source == "sharepoint":
            folder_name = _publish.sharepoint_release_name(
                requested_name, owner, timezone_name=_release_timezone(owner))
        elif requested_name:
            folder_name = requested_name
        else:
            release_tz = _release_timezone(owner)
            folder_name = _publish.release_folder_name(timezone_name=release_tz, owner_email=owner)
        folder_state = "proposed"
    destination_changed = False
    if status:
        # Publishing reuses the execution's original parent, even when the user's
        # saved preference has since changed. Preview that same immutable target
        # so a retry never promises a path the publish endpoint cannot authorize.
        saved_parent = status.get("parent_folder_id")
        destination_changed = saved_parent != (destination_config or {}).get("folder_id")
        destination_config = ({"provider": source, "folder_id": saved_parent,
                               "folder_name": status.get("parent_folder_name") or "Saved release location"}
                              if saved_parent else None)
    destination_preflight = _preflight_release_destination(request, destination_config)
    rows = {row.get("file"): row for row in scan.get("files", [])}
    existing = {row.get("file"): row for row in (status or {}).get("documents", [])}
    planned_paths: set[tuple[str, str]] = set()
    documents, blockers = [], []
    if destination_changed:
        blockers.append({"code": "release_destination_changed",
                         "reason": "This release already has a saved destination. Use that destination to publish or retry."})
    for name in selected:
        record = rows.get(name)
        if not release_ready(record, body.allow_remaining_issues):
            blockers.append({"file": name, "reason": "Only approved corrected copies can be released."})
            continue
        if body.allow_remaining_issues and body.expected_artifacts.get(name) != record.get("corrected_sha256"):
            blockers.append({"file": name, "reason": "Confirm the current corrected artifact before releasing with remaining issues."})
            continue
        source_path = record.get("source_relative_path") or record.get("parent_folder") or name
        try:
            if source == "sharepoint":
                folders, safe_name = _publish.sharepoint_relative_path(source_path, name)
                target_drive = (destination_config or {}).get("folder_id", "").partition("/")[0]
                location = f"graph:{target_drive or record.get('drive_id') or 'me'}"
            else:
                folders, safe_name = _publish.normalize_relative_path(source_path, name)
                location = "google:me" if source == "drive" else "azure:blob"
        except _publish.UnsafeReleasePath as exc:
            blockers.append({"file": name, "reason": str(exc)})
            continue
        relative = "/".join([*folders, safe_name]) if body.preserve_hierarchy else safe_name
        parent_name = (destination_config or {}).get("folder_name")
        destination = "/".join([*([parent_name] if parent_name else []),
                                "Remediated", folder_name, relative])
        key = (location, destination.casefold())
        if key in planned_paths:
            blockers.append({"file": name, "reason": f"Another selected file resolves to {destination}."})
            continue
        planned_paths.add(key)
        saved = existing.get(name)
        from release_artifacts import reuse_state
        identity = reuse_state(saved, record.get("corrected_sha256"))
        if identity == "unresolved":
            blockers.append({"file": name, "reason": "Prior delivery has no exact artifact digest. Reconcile that delivery before retrying."})
            continue
        action = "reuse" if identity == "reuse" else "create"
        documents.append({"file": name, "provider_location": location,
                          "destination_path": destination, "action": action,
                          **({"release_review": release_review_evidence(record, owner=owner, allow_remaining_issues=True, store=core.store, scan_id=sid)} if body.allow_remaining_issues else {})})
    return {
        "scan_id": sid,
        "folder_name": folder_name,
        "folder_state": folder_state,
        "provider": source,
        "documents": documents,
        "blockers": blockers,
        "can_release": not blockers and destination_preflight["ready"],
        "destination": destination_config,
        "preflight": destination_preflight,
        "collision_policy": ("Existing ACP copies are reused. Unrelated provider files are not "
                             "overwritten; ACP creates a stable suffixed copy and verifies it."),
        "original_files_unchanged": True,
        "preserve_hierarchy": body.preserve_hierarchy,
    }


def _release_document_reconciliation(status: dict, stage_lineage: dict | None) -> dict:
    """Expose one complete requested-document partition, preferring canonical stage facts."""
    release_stage = next((row for row in (stage_lineage or {}).get("stages", [])
                          if row.get("stage") == "release"), None)
    canonical = (release_stage or {}).get("domain_reconciliation")
    names = ("waiting", "processing", "published", "failed", "cancelled", "skipped",
             "completed_unverified")
    if isinstance(canonical, dict) and canonical.get("unit") == "requested documents":
        buckets = canonical.get("buckets") or {}
        published = int(buckets.get("published") or 0)
        return {
            **canonical,
            "buckets": {name: int(buckets.get(name) or 0) for name in names},
            "verified_receipt_count": published,
            "published_receipt_rule": canonical.get("published_receipt_rule")
                                      or "completed receipt with verified=true",
            "authority": "canonical_stage_snapshot",
        }

    # Rolling-deploy fallback for releases created before canonical Release executions existed.
    # It accounts only states the durable legacy rows actually name; unknown rows remain visible
    # as unaccounted instead of being guessed into a successful bucket.
    buckets = {name: 0 for name in names}
    legacy_states = {
        "queued": "waiting", "pending": "waiting", "waiting": "waiting",
        "running": "processing", "processing": "processing",
        "published": "published", "failed": "failed", "cancelled": "cancelled",
        "skipped": "skipped", "completed_unverified": "completed_unverified",
    }
    for document in status.get("documents", []):
        bucket = legacy_states.get(str(document.get("status") or "").lower())
        if bucket:
            buckets[bucket] += 1
    total = int(status.get("documents_total") or 0)
    accounted = sum(buckets.values())
    return {
        "unit": "requested documents", "scope": "legacy Release record",
        "equation": ("requested = waiting + processing + published + completed unverified "
                     "+ failed + cancelled + skipped"),
        "total": total, "accounted": accounted, "unaccounted": total - accounted,
        "buckets": buckets, "verified_receipt_count": None,
        "published_receipt_rule": "completed receipt with verified=true",
        "receipt_evidence_available": False,
        "exact": total == accounted, "authority": "legacy_release_documents",
    }


def _release_manifest_payload(status: dict, *, scan_id: str, owner: str,
                              snapshot_id: str | None,
                              stage_lineage: dict | None = None,
                              finding_reconciliation: dict | None = None) -> dict:
    """Build the authoritative, stable release record from persisted server evidence.

    This intentionally contains no request-time timestamp: downloading the same unchanged
    release twice must produce the same digest.  The digest is tamper evidence, not a digital
    signature; ACP has no configured signing identity and must not imply non-repudiation.
    """
    from release_candidate_assessment import saved_assessment
    roots = [{
        "provider": row.get("provider"),
        "provider_location": row.get("provider_location"),
        "folder_id": row.get("folder_id"),
        "folder_name": row.get("folder_name"),
        "folder_url": row.get("folder_url"),
        "created_at": row.get("created_at"),
    } for row in status.get("roots", [])]
    documents = [{
        "file": row.get("file"),
        "source_document_id": row.get("source_document_id"),
        "source_relative_path": row.get("source_relative_path"),
        "destination_relative_path": row.get("destination_relative_path"),
        "released_document_id": row.get("released_document_id"),
        "released_document_url": row.get("released_document_url"),
        "artifact_digest": row.get("artifact_digest"),
        "corrected_checksum": row.get("corrected_checksum"),
        "corrected_sha256": (
            row["artifact_digest"].removeprefix("sha256:")
            if (row.get("artifact_digest") or "").startswith("sha256:")
            else row.get("corrected_checksum") if row.get("verification") == "sha256" else None
        ),
        "verification": row.get("verification"),
        "corrected_copy_assessment": saved_assessment(
            core.store, scan_id, owner, row.get("file"), row.get("artifact_digest"), release_id=status.get("id")),
        "status": row.get("status"),
        "failure_category": row.get("failure_category"),
        "explanation": row.get("explanation"),
        "created": bool(row.get("created_result")),
        "published_at": row.get("published_at"),
    } for row in status.get("documents", [])]
    document_reconciliation = _release_document_reconciliation(status, stage_lineage)
    release_buckets = document_reconciliation["buckets"]
    return {
        "schema_version": 1,
        "release_id": status.get("id"),
        "scan_id": scan_id,
        "snapshot_id": snapshot_id,
        "canonical_stage_lineage": stage_lineage,
        "finding_reconciliation": finding_reconciliation,
        "actor": owner,
        "source": status.get("source"),
        "status": status.get("status"),
        "created_at": status.get("created_at"),
        "updated_at": status.get("updated_at"),
        "release_folder": status.get("folder_name"),
        "original_files_unchanged": True,
        "counts": {
            "total": document_reconciliation["total"],
            "published": release_buckets["published"],
            "failed": release_buckets["failed"],
            "remaining": (release_buckets["waiting"] + release_buckets["processing"]
                          + release_buckets["completed_unverified"]),
            "waiting": release_buckets["waiting"],
            "processing": release_buckets["processing"],
            "cancelled": release_buckets["cancelled"],
            "skipped": release_buckets["skipped"],
            "completed_unverified": release_buckets["completed_unverified"],
            "verified_receipts": document_reconciliation["verified_receipt_count"],
        },
        "document_reconciliation": document_reconciliation,
        "roots": roots,
        "documents": documents,
        "manifest_generated_by": {
            "service": "acp",
            "release_version": status.get("acp_version") or "not recorded",
        },
    }


def _release_finding_reconciliation(scan_id: str, snapshot_id: str | None,
                                    stage_lineage: dict | None) -> dict:
    """Freeze the canonical Remediation finding account for Release consumers.

    Counts are copied from the revisioned Remediation snapshot, never reconstructed from
    Release documents, fixes, or review cards.  The legacy snapshot deliberately carries null
    outcome buckets; preserving them as ``unavailable`` is more truthful than turning unknowns
    into zeroes.  Request-time freshness fields are excluded so unchanged evidence has one
    reproducible digest in the JSON manifest, ZIP package, and later report renderers.
    """
    snapshot = _remediation_snapshot(scan_id)
    outcomes = snapshot.get("finding_reconciliation")
    residual_keys = ("awaiting_review", "approved_pending_verification", "unchanged_no_fix",
                     "failed", "excluded", "superseded", "unaccounted")
    if not isinstance(outcomes, dict):
        status = "unavailable"
    elif outcomes.get("exact") is True:
        status = "reconciled"
    elif outcomes.get("violations"):
        status = "inconsistent"
    elif all(outcomes.get(key) is None for key in
             ("resolved_verified", "approved_pending_verification", "unchanged_no_fix",
              "failed", "excluded", "superseded")):
        status = "unavailable"
    else:
        status = "pending"
    export = {
        "schema_version": 1,
        "status": status,
        "identifiers": {
            "workflow_id": (stage_lineage or {}).get("workflow_id"),
            "workflow_revision": (stage_lineage or {}).get("workflow_revision"),
            "scan_id": scan_id,
            "snapshot_id": snapshot_id,
            "run_id": snapshot.get("run_id"),
            "batch_id": snapshot.get("batch_id"),
        },
        "revision": snapshot.get("revision"),
        "outcomes": outcomes,
        "residual_outcomes": ({key: outcomes.get(key) for key in residual_keys}
                              if isinstance(outcomes, dict) else None),
    }
    encoded = _json.dumps(export, sort_keys=True, separators=(",", ":"),
                          ensure_ascii=False, default=str).encode("utf-8")
    export["content_digest"] = {
        "algorithm": "SHA-256", "value": hashlib.sha256(encoded).hexdigest()}
    return export


@router.get("/scans/{sid}/release/manifest")
def get_release_manifest(sid: str, request: Request):
    """Return an owner-scoped server manifest plus a reproducible SHA-256 content digest."""
    owner = _owner(request)
    if core.store.get_scan(sid, owner=owner) is None:
        raise HTTPException(404, "scan not found")
    status = core.store.release_for_scan(sid, owner)
    if status is None:
        raise HTTPException(404, "release not found")
    snapshot_id = core.store.stage_snapshot_id(sid)
    lineage = _canonical_lineage_export(sid, owner)["lineage"]
    finding_reconciliation = _release_finding_reconciliation(sid, snapshot_id, lineage)
    manifest = _release_manifest_payload(
        status, scan_id=sid, owner=owner, snapshot_id=snapshot_id,
        stage_lineage=lineage, finding_reconciliation=finding_reconciliation)
    canonical = _json.dumps(manifest, sort_keys=True, separators=(",", ":"),
                            ensure_ascii=False, default=str).encode("utf-8")
    return {
        "manifest": manifest,
        "content_digest": {"algorithm": "SHA-256", "value": hashlib.sha256(canonical).hexdigest()},
        "digest_note": ("This digest makes changes detectable. It is not a digital signature "
                        "and does not provide non-repudiation."),
    }


class ReleasePackageRequest(BaseModel):
    files: list[str]
    package_name: str | None = None
    preserve_hierarchy: bool = True
    include_manifest: bool = True
    download_format: str = "zip"


class ReleasePackagePreviewRequest(BaseModel):
    files: list[str]
    preserve_hierarchy: bool = True
    include_manifest: bool = True


def _selected_release_rows(sid: str, request: Request, files: list[str]) -> tuple[dict, list[str], dict]:
    """Return an owner-scoped scan, de-duplicated selection, and indexed file rows."""
    scan = core.store.get_scan(sid, owner=_owner(request))
    if scan is None:
        raise HTTPException(404, "scan not found")
    selected = list(dict.fromkeys(name for name in files if name))
    if not selected:
        raise HTTPException(422, "select at least one corrected file")
    rows = {row.get("file"): row for row in scan.get("files", [])}
    unknown = [name for name in selected if name not in rows]
    if unknown:
        raise HTTPException(404, f"corrected file not found: {unknown[0]}")
    return scan, selected, rows


@router.post("/scans/{sid}/release/package/preview")
def preview_release_package(sid: str, request: Request, body: ReleasePackagePreviewRequest):
    """Estimate a download before reading corrected blobs or building an archive."""
    scan, selected, rows = _selected_release_rows(sid, request, body.files)
    source = (scan.get("run") or {}).get("source") or "local"
    paths: list[str] = []
    blockers: list[dict] = []
    seen: set[str] = set()
    estimated_bytes = 0
    sized = 0
    import publish as _publish
    for name in selected:
        row = rows[name]
        source_path = (row.get("source_relative_path") or row.get("path")
                       or row.get("parent_folder") or name)
        try:
            if source == "sharepoint":
                folders, safe_name = _publish.sharepoint_relative_path(source_path, name)
            else:
                folders, safe_name = _publish.normalize_relative_path(source_path, name)
        except _publish.UnsafeReleasePath as exc:
            blockers.append({"file": name, "reason": str(exc)})
            continue
        path = "/".join(["Remediated", *folders, safe_name]) if body.preserve_hierarchy else safe_name
        if path.casefold() in seen:
            blockers.append({"file": name, "reason": f"Another selected file resolves to {path}."})
            continue
        seen.add(path.casefold())
        paths.append(path)
        size = row.get("remediated_size") or row.get("size") or row.get("file_size")
        if isinstance(size, (int, float)) and size >= 0:
            estimated_bytes += int(size)
            sized += 1
    return {
        "scan_id": sid, "files": len(selected), "paths": paths,
        "estimated_bytes": estimated_bytes if sized else None,
        "estimate_complete": sized == len(selected),
        "preserve_hierarchy": body.preserve_hierarchy,
        "include_manifest": body.include_manifest,
        "blockers": blockers, "can_download": not blockers,
        "recommended_format": "original" if len(selected) == 1 else "zip",
    }


def _remediated_bytes(owner: str, scan_id: str, filename: str) -> bytes | None:
    """Read one corrected copy, including an unchanged file inherited by an incremental scan."""
    import blob as _blob
    urls = core.store.get_remediation_urls(scan_id, filename, owner=owner)
    source_scan_id, source_file = scan_id, filename
    if not urls or not (urls.get("blob_url") or urls.get("drive_write_url")):
        alt = core.store.find_remediation_for_file(owner, scan_id, filename)
        if not alt or not (alt.get("blob_url") or alt.get("drive_write_url")):
            return None
        source_scan_id, source_file = alt["scan_id"], alt["file"]
    return _blob.download_remediated(owner, source_scan_id, source_file)


def _build_release_zip(sid: str, owner: str, scan: dict, selected: list[str], rows: dict,
                       *, package_name: str, preserve_hierarchy: bool,
                       include_manifest: bool, expected_artifacts: dict | None = None, report_assets: list | None = None,
                       allow_remaining_issues: bool = True):
    """Build a release ZIP into a spill-to-disk stream shared by sync and queued delivery."""
    import publish as _publish
    source = (scan.get("run") or {}).get("source") or "local"
    documents: list[dict] = []
    used_paths: set[str] = set()
    output = tempfile.SpooledTemporaryFile(max_size=8 * 1024 * 1024)
    try:
        with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for name in selected:
                row = rows[name]
                data = _remediated_bytes(owner, sid, name)
                if data is None:
                    raise HTTPException(409, f"corrected copy is not available for packaging: {name}")
                if expected_artifacts is not None:
                    expected = expected_artifacts.get(name, '').removeprefix('sha256:')
                    if hashlib.sha256(data).hexdigest() != expected:
                        raise HTTPException(409, "The corrected copy changed before packaging")
                    from release_artifacts import require_current_record
                    require_current_record(core.store, sid, name, expected, row.get('remediated_at'), owner=owner, allow_remaining_issues=True)
                from release_candidate_assessment import assess_candidate
                digest = hashlib.sha256(data).hexdigest()
                candidate_assessment = assess_candidate(
                    core.store, sid, name, owner, digest, row.get("remediated_at"),
                    allow_remaining_issues=allow_remaining_issues, data=data)
                source_path = (row.get("source_relative_path") or row.get("path")
                               or row.get("parent_folder") or name)
                if source == "sharepoint":
                    folders, safe_name = _publish.sharepoint_relative_path(source_path, name)
                else:
                    folders, safe_name = _publish.normalize_relative_path(source_path, name)
                archive_path = ("/".join(["Remediated", *folders, safe_name])
                                if preserve_hierarchy else safe_name)
                if archive_path.casefold() in used_paths:
                    raise HTTPException(409, f"two selected files resolve to the same package path: {archive_path}")
                used_paths.add(archive_path.casefold())
                archive.writestr(archive_path, data)
                documents.append({"file": row.get("file"),
                    "source_relative_path": row.get("source_relative_path") or row.get("path"),
                    "package_path": archive_path,
                    "corrected_sha256": hashlib.sha256(data).hexdigest(),
                    "corrected_copy_assessment": candidate_assessment})
            status = core.store.release_for_scan(sid, owner)
            snapshot_id = core.store.stage_snapshot_id(sid)
            lineage = _canonical_lineage_export(sid, owner)["lineage"]
            finding_reconciliation = _release_finding_reconciliation(sid, snapshot_id, lineage)
            release_manifest = (_release_manifest_payload(
                status, scan_id=sid, owner=owner, snapshot_id=snapshot_id,
                stage_lineage=lineage, finding_reconciliation=finding_reconciliation)
                if status is not None else None)
            manifest = {"schema_version": 1, "package_type": "acp-corrected-files",
                "scan_id": sid, "snapshot_id": snapshot_id, "actor": owner,
                "package_name": package_name, "original_files_unchanged": True,
                "documents": documents, "finding_reconciliation": finding_reconciliation,
                "release": release_manifest}
            for asset in report_assets or []:
                from release_report_delivery import _asset_bytes
                archive.writestr('Reports/' + asset['name'], _asset_bytes(asset))
            if include_manifest:
                archive.writestr("release-manifest.json", _json.dumps(
                    manifest, sort_keys=True, indent=2, ensure_ascii=False,
                    default=str).encode("utf-8"))
    except Exception:
        output.close()
        raise
    output.seek(0, 2)
    content_length = output.tell()
    output.seek(0)
    default_name = f"acp-release-{re.sub(r'[^A-Za-z0-9._-]', '_', sid)}"
    return output, content_length, f"{package_name or default_name}.zip"


@router.post("/scans/{sid}/release/package")
def download_release_package(sid: str, request: Request, body: ReleasePackageRequest):
    """Return selected corrected copies as one hierarchy-preserving, owner-scoped ZIP."""
    scan, selected, rows = _selected_release_rows(sid, request, body.files)
    if body.download_format not in {"zip", "original"}:
        raise HTTPException(422, "download_format must be 'zip' or 'original'")
    if body.download_format == "original" and len(selected) != 1:
        raise HTTPException(422, "direct download requires exactly one corrected file")

    import publish as _publish
    try:
        requested_package_name = (body.package_name or "").strip()
        if requested_package_name.lower().endswith(".zip"):
            requested_package_name = requested_package_name[:-4]
        package_name = _publish.normalize_release_name(
            requested_package_name, field="ZIP filename")
    except _publish.UnsafeReleasePath as exc:
        raise HTTPException(422, str(exc)) from exc
    owner = _owner(request)
    source = (scan.get("run") or {}).get("source") or "local"
    if body.download_format == "original":
        name = selected[0]
        data = _remediated_bytes(owner, sid, name)
        if data is None:
            raise HTTPException(409, f"corrected copy is not available for download: {name}")
        safe_name = re.sub(r'[\r\n"]', "_", name.rsplit("/", 1)[-1])
        ascii_name = re.sub(r"[^A-Za-z0-9._ -]", "_", safe_name)
        disposition = f'attachment; filename="{ascii_name}"'
        if ascii_name != safe_name:
            disposition += f"; filename*=UTF-8''{quote(safe_name)}"
        return Response(
            data, media_type=mimetypes.guess_type(safe_name)[0] or "application/octet-stream",
            headers={"Content-Disposition": disposition,
                     "Cache-Control": "private, no-store",
                     "Content-Length": str(len(data))})
    output, content_length, filename = _build_release_zip(
        sid, owner, scan, selected, rows, package_name=package_name,
        preserve_hierarchy=body.preserve_hierarchy, include_manifest=body.include_manifest)

    def stream_package():
        try:
            while chunk := output.read(1024 * 1024):
                yield chunk
        finally:
            output.close()

    # Built in two steps rather than one nested f-string. The single-expression form —
    # f'{package_name or f"acp-release-{re.sub(r"[^A-Za-z0-9._-]", "_", sid)}"}.zip' — reuses the
    # inner f-string's own double quote inside its replacement field, which is PEP 701 and parses
    # only on 3.12+. CI pins 3.12, so nothing here went red while `import api.routes` failed
    # outright on 3.11 and took ~26 test modules with it. See tests/test_python_syntax_floor.py.
    ascii_filename = re.sub(r"[^A-Za-z0-9._ -]", "_", filename)
    disposition = f'attachment; filename="{ascii_filename}"'
    if ascii_filename != filename:
        disposition += f"; filename*=UTF-8''{quote(filename)}"
    return StreamingResponse(
        stream_package(), media_type="application/zip",
        headers={"Content-Disposition": disposition,
                 "Cache-Control": "private, no-store",
                 "Content-Length": str(content_length)})


@router.post("/scans/{sid}/release/package/prepare", status_code=202)
def prepare_release_package(sid: str, request: Request, body: ReleasePackageRequest):
    """Queue a durable ZIP build for selections too large for one browser request."""
    import blob as _blob
    if body.download_format != "zip":
        raise HTTPException(422, "queued preparation supports ZIP packages only")
    if not _blob.enabled():
        raise HTTPException(503, "durable package storage is not configured")
    scan, selected, _rows = _selected_release_rows(sid, request, body.files)
    import publish as _publish
    try:
        requested_name = (body.package_name or "").strip()
        if requested_name.lower().endswith(".zip"):
            requested_name = requested_name[:-4]
        package_name = _publish.normalize_release_name(requested_name, field="ZIP filename")
    except _publish.UnsafeReleasePath as exc:
        raise HTTPException(422, str(exc)) from exc
    owner = _owner(request)
    job_id = core.store.enqueue_job("prepare_release_package", {
        "scan_id": sid, "owner": owner, "files": selected,
        "package_name": package_name,
        "preserve_hierarchy": body.preserve_hierarchy,
        "include_manifest": body.include_manifest,
    }, scan_id=sid, max_attempts=3)
    return {"job_id": job_id, "scan_id": sid, "status": "queued", "files": len(selected)}


@router.get("/scans/{sid}/release/package/jobs/{job_id}/download")
def download_prepared_release_package(sid: str, job_id: str, request: Request):
    """Stream an owner-scoped package artifact after its durable worker finishes."""
    owner = _owner(request)
    if core.store.get_scan(sid, owner=owner) is None:
        raise HTTPException(404, "scan not found")
    job = core.store.get_job(job_id)
    payload = (job or {}).get("payload") or {}
    if (not job or job.get("type") != "prepare_release_package" or job.get("scan_id") != sid
            or payload.get("owner") != owner):
        raise HTTPException(404, "package not found")
    if job.get("status") != "done":
        raise HTTPException(409, "package is not ready")
    import blob as _blob
    downloader = _blob.open_release_package(owner, sid, job_id)
    if downloader is None:
        raise HTTPException(404, "prepared package is no longer available")
    default_name = f"acp-release-{re.sub(r'[^A-Za-z0-9._-]', '_', sid)}"
    filename = f"{payload.get('package_name') or default_name}.zip"
    ascii_filename = re.sub(r"[^A-Za-z0-9._ -]", "_", filename)
    disposition = f'attachment; filename="{ascii_filename}"'
    if ascii_filename != filename:
        disposition += f"; filename*=UTF-8''{quote(filename)}"
    return StreamingResponse(downloader.chunks(), media_type="application/zip", headers={
        "Content-Disposition": disposition, "Cache-Control": "private, no-store"})


@router.get("/scans/{scan_id}/files/{filename:path}/remediated")
def get_remediated_file(scan_id: str, filename: str, request: Request):
    """Stream a remediated file's fixed bytes (ADR 0010): Blob first (the durable source
    of truth), falling back to a redirect to the Drive mirror copy for a pre-ADR-0010
    remediation that only ever wrote there. 404 if neither exists."""
    import blob as _blob
    owner = _owner(request)
    # OWNERSHIP FIRST, like every other per-scan route. This endpoint was the one that did not,
    # and it was exploitable: `get_remediation_urls` has no owner predicate, the blob read below
    # is correctly scoped and so returns None for a foreign document, and the Drive fall-through
    # then handed the caller a link to somebody else's remediated file. Measured, not theorised —
    # tests/test_remediated_download_isolation.py redirected an allow-listed non-owner (307) to
    # the owner's Drive URL before this line existed.
    #
    # 404 rather than 403, matching tests/test_foreign_scan_404.py: a non-owner must not be able
    # to confirm the scan exists. That also closes the oracle — "wrong filename" and "not your
    # scan" were distinguishable (404 vs 307), which leaks which documents a scan contains.
    # "scan not found" verbatim: tests/test_scan_not_found_detail.py pins every owner-check 404
    # in this router to that exact string, because the SPA only recovers from that one. It also
    # gives the property this check needs — a non-owner gets the same answer for a foreign scan
    # as for one that never existed, so the response tells them nothing either way.
    if core.store.get_scan(scan_id, owner=owner) is None:
        raise HTTPException(404, "scan not found")
    urls = core.store.get_remediation_urls(scan_id, filename, owner=owner)
    src_scan_id, src_file = scan_id, filename
    if not urls or not (urls.get("blob_url") or urls.get("drive_write_url")):
        # ADR 0011: an incremental re-scan mints a new scan_id and, for an unchanged file,
        # never re-remediates — so the fixed copy (Blob object + DB record) lives under the
        # scan_id that actually ran the remediation. Both are scan_id-keyed, so resolve the
        # document across this owner's scans and stream from where the bytes really are.
        alt = core.store.find_remediation_for_file(owner, scan_id, filename)
        if not alt or not (alt.get("blob_url") or alt.get("drive_write_url")):
            raise HTTPException(404, "no remediated copy recorded for this file")
        urls, src_scan_id, src_file = alt, alt["scan_id"], alt["file"]
    data = _blob.download_remediated(owner, src_scan_id, src_file)
    if data is not None:
        ext = src_file.rsplit(".", 1)[-1].lower() if "." in src_file else "bin"
        mime_map = {"html": "text/html", "htm": "text/html", "pdf": "application/pdf",
                    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation"}
        return Response(data, media_type=mime_map.get(ext, "application/octet-stream"))
    if urls.get("drive_write_url"):
        return RedirectResponse(urls["drive_write_url"])
    raise HTTPException(404, "remediated copy recorded but not retrievable "
                             "(Blob not configured and no Drive mirror)")


@router.get("/scans/{scan_id}/files/{filename:path}/content")
def get_file_content(scan_id: str, filename: str, request: Request):
    """Fetch the original file bytes for playback in the caption review editor.

    Verifies scan ownership before serving bytes. Tries local corpus, Drive, and remediated
    blob in that order — the same cheapest-first chain as the render/thumbnail routes — so
    captions are reviewable regardless of whether the scan came from Drive, SharePoint, or a
    local corpus.

    NO `Accept-Ranges` HEADER, deliberately. This reads the whole object in one Response; it
    does not honour a Range request. Advertising byte ranges would give the browser a seek bar
    that silently re-serves the full body. Not having ranges yet is a limitation; claiming them
    would be a bug.
    """
    owner = _owner(request)
    if core.store.get_scan(scan_id, owner=owner) is None:
        raise HTTPException(404, "scan not found")
    data = _source_bytes_for_render(request, scan_id, filename, owner)
    if data is None:
        raise HTTPException(404, "file not retrievable from any configured source")
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else "bin"
    mime_map = {"html": "text/html", "htm": "text/html", "pdf": "application/pdf",
                "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation"}
    # Media MIME types come from api/media.py. A <video> element handed `application/octet-stream`
    # refuses to play — browsers do not sniff a container out of a generic type.
    import media as _media
    media_type = mime_map.get(ext) or _media.media_mime(filename) or "application/octet-stream"
    return Response(data, media_type=media_type)


def _source_bytes_for_render(request: Request, scan_id: str, filename: str, owner: str) -> bytes | None:
    """Best-effort original bytes to rasterize for a preview (ADR 0015), tried cheapest-first
    and preferring the *original* the reviewer is looking at over the remediated copy:
      1. exact assessed source cache (owner-scoped)
      2. local corpus file  (source=local — the demo default; no token, on disk)
      3. Drive original      (via drive_file_id + a live x-drive-token)
      4. remediated blob copy (post-remediation fallback; accessibility fixes are structurally
         near-identical to the original page, so it's an acceptable last resort)
    Returns None if none are reachable — the caller then 404s. Never raises."""
    scan = core.store.get_scan(scan_id, owner=owner)
    source = (scan or {}).get("run", {}).get("source")

    # Assess caches the exact bytes it evaluated. Prefer that owner-scoped copy so preview works
    # after a worker restart and for sources that are no longer reachable from this API replica.
    # read_cached_source validates both scan ownership and the recorded source metadata.
    try:
        import scanner
        cached = scanner.read_cached_source(scan_id, filename, owner)
        if cached is not None:
            return cached
    except Exception:
        swallowed("routes.scans._source_bytes_for_render: reading the assessed source cache "
                  "failed", scan_id)

    if source == "local":
        try:
            corpus = Path(os.environ.get("ACP_LOCAL_CORPUS") or (scanner.ACP / "test-corpus/files"))
            p = corpus / filename
            if p.is_file():
                return p.read_bytes()
        except Exception:
            swallowed("routes.scans._source_bytes_for_render: reading the source bytes from the "
                      "local path failed", scan_id)

    # SharePoint also stores its provider item ID in drive_file_id. That identifier
    # alone never authorizes a Google request or a Google sign-in prompt.
    drive_file_id = core.store.get_file_drive_id(scan_id, filename) if source == "drive" else None
    if drive_file_id:
        try:
            svc = core.drive_service(request)
            return svc.files().get_media(fileId=drive_file_id).execute()
        except Exception:
            swallowed("routes.scans._source_bytes_for_render: downloading the source bytes from "
                      "Drive failed", scan_id)

    try:
        import blob as _blob
        return _blob.download_remediated(owner, scan_id, filename)
    except Exception:
        return None


@router.get("/scans/{scan_id}/files/{filename:path}/thumbnail")
def get_file_thumbnail(scan_id: str, filename: str, request: Request,
                       fresh: int = Query(0)):
    """Serve a page-1 PNG preview of a file (ADR 0015): blob cache → render-on-demand →
    cache → serve. Opt-in and non-blocking — any miss or failure is a 404, never a 500, and
    nothing here touches scan/remediate/report. PDF only in phase 1; Office → 404 → the UI
    shows a placeholder. ?fresh=1 bypasses the cache read (forced re-render)."""
    import blob as _blob
    import render as _render

    owner = _owner(request)
    if core.store.get_scan(scan_id, owner=owner) is None:
        raise HTTPException(404, "scan not found")

    ext = os.path.splitext(filename)[1].lower()
    if not _render.can_render(ext):
        raise HTTPException(404, "no preview available for this file type")

    if not fresh:
        cached = _blob.download_render(owner, scan_id, filename)
        if cached is not None:
            return Response(cached, media_type="image/png",
                            headers={"Cache-Control": "private, max-age=86400"})

    data = _source_bytes_for_render(request, scan_id, filename, owner)
    if not data:
        raise HTTPException(404, "source document not retrievable for preview")

    png = _render.render_page1_png(data, ext)
    if not png:
        raise HTTPException(404, "could not render a preview for this document")

    _blob.upload_render(owner, scan_id, filename, png)  # best-effort cache; never raises
    return Response(png, media_type="image/png",
                    headers={"Cache-Control": "private, max-age=86400"})


@router.get("/scans/{scan_id}/files/{filename:path}/page/{page}")
def get_file_page(scan_id: str, filename: str, page: int, request: Request,
                  fresh: int = Query(0)):
    """Serve a PNG of page N of a file — the rendering primitive the Intelligent Review
    Workspace's 'locate in document' evidence uses. Same blob-cache → render-on-demand →
    cache → serve as the thumbnail; the renderer clamps `page` to the document's real range.
    PDF always; Office (docx/pptx/xlsx) wherever LibreOffice is installed (render.can_render),
    otherwise 404 → placeholder. Owner-scoped, non-blocking.

    THE CLAMP IS KEPT, AND NOW IT IS SAID. Page 999 of a one-page PDF is still a 200 with page 1,
    byte-identical — but the 200 carries `X-ACP-Rendered-Page` (the page actually drawn,
    min(page, count)) and `X-ACP-Page-Count`, so a client can tell "page 999" from "the last page".
    Both are omitted, never guessed, when the count is unknown: the client captions that as
    "not confirmed".

    PROVENANCE IS BOUND TO THE EXACT CACHED PNG, never to the file. Each cached page carries a
    sidecar (`{filename}#p{N}#meta`) recording the sha256 of the PNG it describes plus the page
    that PNG shows and the count at the time it was drawn. A hit emits headers only when the
    sidecar parses, is self-consistent, and its digest equals the cached bytes. A per-file count
    cannot do this: page 999 cached from a one-page version shows page 1, and a later two-page
    render would relabel those same pixels "page 2". Anything else — a legacy PNG with no sidecar,
    a partial cache write, a digest mismatch — serves the PNG with no provenance at all."""
    import blob as _blob
    import render as _render

    owner = _owner(request)
    if core.store.get_scan(scan_id, owner=owner) is None:
        raise HTTPException(404, "scan not found")

    ext = os.path.splitext(filename)[1].lower()
    if not _render.can_render(ext):
        raise HTTPException(404, "no preview available for this file type")

    page = max(1, min(int(page or 1), 5000))          # sane bound; renderer clamps to real range
    cache_key = f"{filename}#p{page}"                  # page-specific blob cache entry
    meta_key = f"{cache_key}#meta"                     # provenance of THAT exact PNG

    def served(png: bytes, provenance: tuple[int, int] | None) -> Response:
        headers = {"Cache-Control": "private, max-age=86400"}
        if provenance:
            headers["X-ACP-Rendered-Page"] = str(provenance[0])
            headers["X-ACP-Page-Count"] = str(provenance[1])
        return Response(png, media_type="image/png", headers=headers)

    def cached_provenance(png: bytes) -> tuple[int, int] | None:
        """(rendered_page, page_count) iff the sidecar describes exactly these bytes.

        Absent (legacy), partial or garbled sidecars are EXPECTED states of a best-effort cache,
        not failures: each answers None (no provenance), explicitly. download_render never raises,
        so the only thing caught is the parse of bytes we already hold."""
        raw = _blob.download_render(owner, scan_id, meta_key)
        if not raw:
            return None                                # legacy PNG or sidecar write that never landed
        try:
            meta = _json.loads(raw.decode())
        except (UnicodeDecodeError, ValueError):       # partial/garbled sidecar → say nothing
            return None
        if not isinstance(meta, dict):
            return None
        rendered, count = meta.get("rendered_page"), meta.get("page_count")
        if (meta.get("v") == 1
                and meta.get("png_sha256") == hashlib.sha256(png).hexdigest()
                and type(rendered) is int and type(count) is int
                and count >= 1 and rendered == min(page, count)):
            return rendered, count
        return None

    if not fresh:
        cached = _blob.download_render(owner, scan_id, cache_key)
        if cached is not None:
            return served(cached, cached_provenance(cached))

    data = _source_bytes_for_render(request, scan_id, filename, owner)
    if not data:
        raise HTTPException(404, "source document not retrievable for preview")

    png = _render.render_page_png(data, ext, page)
    if not png:
        raise HTTPException(404, "could not render this page")

    count = _render.page_count(data, ext)             # never raises; None = unknown
    provenance = (min(page, count), count) if count else None
    # PNG first, then its sidecar — and the sidecar only if the PNG write landed. The digest makes
    # the order a belt, not the braces: a sidecar can only ever vouch for the bytes it hashed, so a
    # PNG write that failed after, or a sidecar write that failed after, leaves a digest mismatch
    # (no headers) rather than a borrowed label. Unknown count → no sidecar worth trusting: write
    # one with no digest so an older sidecar for this key cannot survive a fresh re-render.
    if _blob.upload_render(owner, scan_id, cache_key, png):     # best-effort; never raises
        meta = ({"v": 1, "png_sha256": hashlib.sha256(png).hexdigest(),
                 "rendered_page": provenance[0], "page_count": provenance[1]}
                if provenance else {"v": 1, "png_sha256": None})
        _blob.upload_render(owner, scan_id, meta_key, _json.dumps(meta).encode())
    return served(png, provenance)


@router.get("/scans/{scan_id}/files/{filename:path}/geometry")
def get_file_geometry(scan_id: str, filename: str, request: Request,
                      locator: str = Query(...)):
    """Normalized bounding box `{page, x, y, w, h}` (fractions of the page) for the shape a
    finding's `part#rId` locator names — the rectangle the frontend overlays on the page render
    (ADR 0018 Slice 2). Owner-scoped and non-blocking: a shape whose geometry can't be attributed
    (grouped/inherited transform, a non-pptx format, a bad locator) returns `{"bbox": null}` — a
    200 with no box, so the card degrades to the plain large preview, never an error. Honesty rule
    (ADR 0016): a real measured rectangle or nothing — the box is never guessed."""
    import render as _render

    owner = _owner(request)
    if core.store.get_scan(scan_id, owner=owner) is None:
        raise HTTPException(404, "scan not found")

    ext = os.path.splitext(filename)[1].lower()
    # Only formats we can both render AND read geometry from are worth resolving bytes for.
    if not _render.can_render(ext):
        return {"bbox": None}

    data = _source_bytes_for_render(request, scan_id, filename, owner)
    if not data:
        return {"bbox": None}

    import geometry as _geom
    return {"bbox": _geom.shape_bbox(data, ext, locator)}


@router.get("/scans/{scan_id}/files/{filename:path}/source_link")
def get_file_source_link(scan_id: str, filename: str, request: Request,
                         page: int = Query(1, ge=1)):
    """Deep-link URL back to the source document, scoped to the given slide/page.

    Owner-scoped and non-blocking: returns 200 with `{url: null}` when a link cannot be
    constructed (local upload, missing token, Graph error) so the card degrades gracefully.

    Drive: constructs the link from the stored `drive_file_id` — no API call.
    SharePoint: calls Graph `GET /drives/{drive_id}/items/{drive_file_id}?$select=webUrl`
      with the caller-supplied `x-sp-token`; appends `?web=1&slide={page}` for .pptx.
      Returns `{url: null}` when no token is supplied or when the Graph call fails.
    Local / unknown: returns `{url: null}`.

    Honesty (ADR 0016): a real stored id or a live Graph response, or nothing."""
    import os as _os

    owner = _owner(request)
    row = core.store.get_source_link_data(scan_id, filename, owner=owner)
    if row is None:
        raise HTTPException(404, "scan not found")

    source = (row.get("source") or "").lower()
    drive_file_id = row.get("drive_file_id") or ""

    if source == "drive" and drive_file_id:
        return {"url": f"https://drive.google.com/file/d/{drive_file_id}/view",
                "label": "Open in Drive"}

    if source == "sharepoint" and drive_file_id:
        token = request.headers.get("x-sp-token")
        if not token:
            return {"url": None}
        drive_id = row.get("drive_id") or ""
        try:
            import scanner as _scanner
            graph_url = (f"{_scanner.GRAPH}/drives/{drive_id}/items/{drive_file_id}"
                         if drive_id else
                         f"{_scanner.GRAPH}/me/drive/items/{drive_file_id}")
            item = _scanner._sp_get(token, graph_url + "?$select=webUrl")
            web_url = item.get("webUrl", "")
            if not web_url:
                return {"url": None}
            ext = _os.path.splitext(filename)[1].lower()
            if ext == ".pptx":
                web_url = f"{web_url}?web=1&slide={page}"
            return {"url": web_url, "label": "Open in SharePoint"}
        except Exception:
            return {"url": None}

    return {"url": None}


_DISPOSITION_KINDS = frozenset({"attested", "out_of_scope"})


@router.post("/scans/{scan_id}/files/{filename:path}/dispose")
async def dispose_criterion(scan_id: str, filename: str, request: Request):
    """W4 — record a human disposition for one criterion in a file.

    Body: {"sc": "<criterion-id>", "kind": "attested"|"out_of_scope", "reason": "<text>"}

    Append-only: correcting a wrong disposition means calling again with the updated kind/reason —
    the most-recent row wins in /dispositions. Guarded to the dispositionable outcome types
    (UNCHECKED, GAP, AT) on the frontend; the backend trusts the kind value and validates only that
    it is a recognised kind and that reason is non-empty."""
    owner = _owner(request)
    if core.store.get_scan(scan_id, owner=owner) is None:
        raise HTTPException(404, "scan not found")
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(422, "JSON body required")
    sc = (body.get("sc") or "").strip()
    kind = (body.get("kind") or "").strip()
    reason = (body.get("reason") or "").strip()
    if not sc:
        raise HTTPException(422, "sc is required")
    if kind not in _DISPOSITION_KINDS:
        raise HTTPException(422, f"kind must be one of {sorted(_DISPOSITION_KINDS)}")
    if not reason:
        raise HTTPException(422, "reason is required")
    row = core.store.record_criterion_disposition(
        scan_id, filename, sc, kind, reason, actor=owner, owner=owner)
    core.store.log_decision(owner, "criterion.disposed", scan_id=scan_id,
                            file=filename, rule_id=sc, detail=f"{kind}: {reason}")
    return row


@router.get("/scans/{scan_id}/files/{filename:path}/dispositions")
def list_file_dispositions(scan_id: str, filename: str, request: Request):
    """W4 — all recorded criterion dispositions for one file, most-recent-first.

    The caller takes the first row per sc as the effective disposition. Returns an empty list
    when the scan is found but no dispositions have been recorded for this file."""
    owner = _owner(request)
    if core.store.get_scan(scan_id, owner=owner) is None:
        raise HTTPException(404, "scan not found")
    return core.store.list_criterion_dispositions(scan_id, filename, owner=owner)


@router.get("/scans/{scan_id}/files/{filename:path}/scanned-layout")
def get_scanned_pdf_layout(scan_id: str, filename: str, request: Request):
    """ADR 0027 Tier A — vision layout model for a scanned/untagged PDF.

    Returns per-page descriptions extracted during scanning when ACP_SCANNED_PDF_TIER_A is on
    and the file was detected as scanned. ``detected`` is True only when at least one page was
    assessed. Returns ``{"detected": false, "pages": []}`` for all other cases (structured files,
    flag off, scan not found)."""
    owner = _owner(request)
    if core.store.get_scan(scan_id, owner=owner) is None:
        raise HTTPException(404, "scan not found")
    rows = core.store.get_scanned_pdf_layouts(scan_id, filename)
    return {"detected": bool(rows), "pages": rows}


@router.get("/scans/{scan_id}/files/{filename:path}/heading-outline")
def get_heading_outline(scan_id: str, filename: str, request: Request):
    """The docx heading outline `{before,after}` for a heading finding's Structure evidence — the
    document's real styled headings in order, and a never-skip correction of them. docx-only (a PDF
    exposes heading PRESENCE, not an extractable outline, exactly as geometry is pptx/xlsx-only).
    Owner-scoped and non-blocking: any other format, a doc with fewer than two headings, or an
    outline that already nests correctly returns `{"outline": null}` — a 200 with nothing, so the
    card degrades to the honest generic note rather than erroring. Honesty (ADR 0016): real extracted
    headings + a deterministic renumber, never a fabricated tree."""
    owner = _owner(request)
    if core.store.get_scan(scan_id, owner=owner) is None:
        raise HTTPException(404, "scan not found")
    ext = os.path.splitext(filename)[1].lower()
    if ext != ".docx":
        return {"outline": None}
    data = _source_bytes_for_render(request, scan_id, filename, owner)
    if not data:
        return {"outline": None}
    import doc_structure as _ds
    return {"outline": _ds.heading_outline(data, ext)}


@router.get("/scans/{scan_id}/files/{filename:path}/table-structure")
def get_table_structure(scan_id: str, filename: str, request: Request):
    """The docx table(s) as `{tables:[{rows, headerRow, headerMarked, truncated}]}` for a
    header-association finding's (1.3.1) Structure evidence — real cell text, which row is the header,
    and whether that row is actually marked so a screen reader announces it. docx-only, owner-scoped,
    non-blocking: any other format, or a doc with no qualifying (multi-row, multi-column) table,
    returns `{"tables": null}` (a 200) so the card degrades to the honest generic note. Honesty
    (ADR 0016): extracted table content only — no fabricated grid, no invented header."""
    owner = _owner(request)
    if core.store.get_scan(scan_id, owner=owner) is None:
        raise HTTPException(404, "scan not found")
    ext = os.path.splitext(filename)[1].lower()
    if ext != ".docx":
        return {"tables": None}
    data = _source_bytes_for_render(request, scan_id, filename, owner)
    if not data:
        return {"tables": None}
    import doc_structure as _ds
    result = _ds.table_structure(data, ext)
    return {"tables": (result or {}).get("tables") if result else None}


# A reviewer opening one 1.4.3-hybrid card shouldn't trigger dozens of slide renders; bound the
# work per request (the OCR-cap precedent). A deck with more hybrid shapes than this reports the
# cap honestly (`checked` < `total`) rather than silently covering only some.
_TIER_B_SHAPE_CAP = 8


@router.get("/scans/{scan_id}/files/{filename:path}/verify-contrast")
def get_file_verify_contrast(scan_id: str, filename: str, request: Request,
                             locator: str = Query(None)):
    """ADR 0024 Tier B.1 — render-verified 1.4.3-hybrid contrast, ON DEMAND. The Tier-A scan flags
    "text over a picture/gradient fill — contrast unknowable from the file"; this endpoint renders
    the offending shape's page (ADR 0018 seam) and MEASURES the contrast from the actual pixels,
    upgrading the 🟡 flag from "possible" to "measured".

    With no `locator` it RE-DERIVES the hybrid shapes from the source and measures each (up to a
    per-request cap) — so the card needs only the file, and nothing is persisted at rest (ADR 0024,
    no schema change). A `locator` measures one named shape.

    View-time only — never in the bulk scan (same deferred posture as the thumbnail/geometry). Owner
    -scoped and always 200: every degrade (render disabled, no LibreOffice, unattributable geometry,
    ambiguous busy background) returns a `measured: false` shape, so the card falls back to the
    Tier-A 🟡, never an error and never a certified pass (ADR 0016). pptx only in B.1."""
    import office_structure as _off
    import render as _render
    import render_verify as _rv

    owner = _owner(request)
    if core.store.get_scan(scan_id, owner=owner) is None:
        raise HTTPException(404, "scan not found")

    ext = os.path.splitext(filename)[1].lower()
    if ext != ".pptx":
        return {"measured": False, "reason": "unsupported_format"}
    if not (_render.office_render_enabled() and _render.can_render(ext)):
        return {"measured": False, "reason": "render_unavailable"}

    data = _source_bytes_for_render(request, scan_id, filename, owner)
    if not data:
        return {"measured": False, "reason": "source_unavailable"}

    # One named shape → the single measurement (used by a per-shape "verify" action).
    if locator:
        return _rv.measure_hybrid_contrast(data, ext, locator)

    # No locator → re-derive every render-attributable hybrid shape and measure each, capped.
    targets = [t for t in _off.hybrid_contrast_locators(data) if t.get("shape")]
    if not targets:
        return {"measured": False, "reason": "no_hybrid_shapes"}
    shapes = []
    for t in targets[:_TIER_B_SHAPE_CAP]:
        m = _rv.measure_hybrid_contrast(data, ext, f"{t['part']}#{t['shape']}")
        shapes.append({"shape": t["shape"], "kind": t["kind"], **m})
    ok = [s for s in shapes if s.get("measured") and isinstance(s.get("ratio"), (int, float))]
    if not ok:
        # Nothing measurable (busy backgrounds / unattributable) — honest abstain, not a pass.
        return {"measured": False, "reason": "ambiguous_background",
                "checked": len(shapes), "total": len(targets)}
    worst = min(ok, key=lambda s: s["ratio"])
    return {"measured": True, "shapes": shapes,
            "worst_ratio": worst["ratio"], "worst_shape": worst["shape"],
            "any_fail_aa": any(not s["passes_aa"] for s in ok),
            "checked": len(shapes), "total": len(targets)}


@router.get("/scans/{scan_id}/files/{filename:path}/verify-resize")
def get_file_verify_resize(scan_id: str, filename: str, request: Request,
                           locator: str = Query(None)):
    """ADR 0024 Tier B.2 — render-verified 1.4.4 Resize Text, ON DEMAND. The Tier-A scan flags a
    fixed-size (auto-fit off) text box holding a lot of text — "may clip at 200%". This renders the
    box and MEASURES how much of it the text already fills: enlarging to 200% needs ≥2× the current
    text height, so a box already over half full overflows.

    Same posture as verify-contrast: view-time only, pptx-only, owner-scoped, always 200, re-derives
    targets from source (no schema change), degrades to the Tier-A 🟡. A measured overflow is
    actionable; measured headroom still says "verify" (rewrapping may clip) — never a certified pass."""
    import office_structure as _off
    import render as _render
    import render_verify as _rv

    owner = _owner(request)
    if core.store.get_scan(scan_id, owner=owner) is None:
        raise HTTPException(404, "scan not found")

    ext = os.path.splitext(filename)[1].lower()
    if ext != ".pptx":
        return {"measured": False, "reason": "unsupported_format"}
    if not (_render.office_render_enabled() and _render.can_render(ext)):
        return {"measured": False, "reason": "render_unavailable"}

    data = _source_bytes_for_render(request, scan_id, filename, owner)
    if not data:
        return {"measured": False, "reason": "source_unavailable"}

    if locator:
        return _rv.measure_resize_headroom(data, ext, locator)

    targets = [t for t in _off.resize_text_locators(data) if t.get("shape")]
    if not targets:
        return {"measured": False, "reason": "no_fixed_boxes"}
    boxes = []
    for t in targets[:_TIER_B_SHAPE_CAP]:
        m = _rv.measure_resize_headroom(data, ext, f"{t['part']}#{t['shape']}")
        boxes.append({"shape": t["shape"], **m})
    ok = [b for b in boxes if b.get("measured") and isinstance(b.get("height_frac"), (int, float))]
    if not ok:
        return {"measured": False, "reason": "no_text_measured",
                "checked": len(boxes), "total": len(targets)}
    worst = max(ok, key=lambda b: b["height_frac"])   # fullest box = closest to overflowing
    return {"measured": True, "boxes": boxes,
            "worst_fill": worst["height_frac"], "worst_shape": worst["shape"],
            "any_overflow_at_200": any(b["overflows_at_200"] for b in ok),
            "checked": len(boxes), "total": len(targets)}


@router.get("/scans/{scan_id}/files/{filename:path}/verify-pdf-contrast")
def get_file_verify_pdf_contrast(scan_id: str, filename: str, request: Request):
    """ADR 0025 Tier B — render-verified 1.4.3 text-over-image contrast for PDF, ON DEMAND. The
    scan-time detector flags "text sits over an image — declared colour can't prove contrast"; this
    renders the offending page (pdfium, no LibreOffice needed) and MEASURES the text-vs-image
    contrast from the pixels, upgrading the 🟡 flag from "possible" to "measured".

    Re-derives the text runs from the source bytes (no schema change, ADR 0024/0025 posture), so the
    card needs only the file. View-time only — never in the bulk scan. Owner-scoped and always 200:
    every degrade (not a PDF, pdfium missing, busy background) returns `measured: false`, so the card
    falls back to the scan-time 🟡, never an error and never a certified pass (ADR 0016). Unlike the
    Office Tier B endpoints this needs no `ACP_OFFICE_RENDER` — pdfium rasterizes PDF unconditionally."""
    import pdf_render_verify as _prv
    import render as _render

    owner = _owner(request)
    if core.store.get_scan(scan_id, owner=owner) is None:
        raise HTTPException(404, "scan not found")

    ext = os.path.splitext(filename)[1].lower()
    if ext != ".pdf":
        return {"measured": False, "reason": "unsupported_format"}
    if not _render.can_render(ext):
        return {"measured": False, "reason": "render_unavailable"}

    data = _source_bytes_for_render(request, scan_id, filename, owner)
    if not data:
        return {"measured": False, "reason": "source_unavailable"}

    return _prv.measure_pdf_over_image_contrast(data)


@router.get('/scans/{sid}/release/reports')
def get_release_reports(sid: str, request: Request, response: Response):
    from routes.release_continuation import owner_scan
    from release_report_delivery import get_latest_release_reports
    owner, _ = owner_scan(sid, request)
    response.headers['Cache-Control'] = 'no-store'
    return get_latest_release_reports(core.store, sid, owner)


@router.get('/scans/{sid}/release/reports/{bundle_id}/{asset_index}')
def download_release_report(sid: str, bundle_id: str, asset_index: int, request: Request):
    from routes.release_continuation import owner_scan
    from release_report_delivery import get_release_report_asset
    owner, _ = owner_scan(sid, request)
    try:
        asset = get_release_report_asset(core.store, sid, owner, bundle_id, asset_index)
        if not asset:
            raise KeyError('Report not found')
    except (KeyError, IndexError, ValueError) as exc:
        raise HTTPException(404, 'Report not found') from exc
    return Response(content=asset['content'], media_type=asset['content_type'], headers={
        'Content-Disposition': "attachment; filename*=UTF-8''" + quote(asset['name'], safe=''),
        'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff',
        'Content-Security-Policy': "sandbox; default-src 'none'; style-src 'unsafe-inline'",
    })


@router.post('/scans/{sid}/release/reports/retry')
def retry_release_reports(sid: str, request: Request, response: Response):
    from routes.release_continuation import owner_scan, credentials
    from release_report_delivery import retry_release_reports as retry
    owner, _ = owner_scan(sid, request)
    credentials(sid, request)
    response.headers['Cache-Control'] = 'no-store'
    try:
        return retry(core.store, sid, owner)
    except KeyError as exc:
        raise HTTPException(404, 'Report not found') from exc
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc

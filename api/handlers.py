"""Job handlers for the durable async queue (ADR 0004).

Registered with the worker by importing this module (see core.start_workers).
Each handler runs one job to completion; raising re-queues it with backoff,
raising FatalJobError dead-letters it.

Current handlers:
  scan            — run a full scan asynchronously (durable + retryable), persist
                    results, emit per-file/per-rule Langfuse spans, finalize.

Per-file parallelism is intentionally NOT used here: the .NET Office analyser
processes a directory in one batch, so the natural durable unit is one scan job.
Per-file fan-out (PDF/HTML) is a possible future optimization (ADR 0004 step 3).
"""
from __future__ import annotations

import hashlib as _hashlib
import json as _json
import logging
import os as _os

import core
import provenance
from worker import handler, FatalJobError, JobCancelledError, ReservationRetryError, check_cancel
from swallowed import swallowed
from scanner import run_scan

logger = logging.getLogger(__name__)


@handler("publish_release_reports")
def _publish_release_reports(payload: dict, job: dict) -> None:
    """Deliver the frozen follow-up report bundle without reopening file approval."""
    if not payload.get("bundle_id") or not payload.get("owner"):
        raise FatalJobError("Report delivery job missing bundle or owner")
    from release_report_delivery import process_release_reports
    process_release_reports(core.store, payload["bundle_id"], payload["owner"])


@handler("prepare_release_package")
def _prepare_release_package(payload: dict, job: dict) -> None:
    """Build a large release archive off-request and persist it for later download."""
    scan_id, owner = payload.get("scan_id"), payload.get("owner")
    files = list(dict.fromkeys(payload.get("files") or []))
    if not scan_id or not owner or not files or not job.get("id"):
        raise FatalJobError("prepare_release_package job missing identity or files")
    scan = core.store.get_scan(scan_id, owner=owner)
    if scan is None:
        raise FatalJobError("scan not found")
    rows = {row.get("file"): row for row in scan.get("files", [])}
    if any(name not in rows for name in files):
        raise FatalJobError("corrected file not found")
    expected = payload.get('expected_artifacts')
    report_assets = None
    if expected is not None:
        if core.store.remediation_source_revision(scan_id) != payload.get('expected_source_revision'):
            raise FatalJobError('The assessed source changed before packaging')
        if set(expected) != set(files):
            raise FatalJobError('The automatic package scope changed')
        if payload.get('report_bundle_id'):
            from release_report_delivery import _get
            bundle = _get(core.store, payload['report_bundle_id'], owner)
            if not bundle or bundle['scan_id'] != scan_id:
                raise FatalJobError('Package reports do not belong to this assessment')
            report_assets = bundle['assets']
    _phase(job, "building the ZIP package")
    from routes.scans import _build_release_zip
    import blob as _blob
    output = None
    try:
        output, _size, _filename = _build_release_zip(
            scan_id, owner, scan, files, rows,
            package_name=payload.get("package_name") or "",
            preserve_hierarchy=payload.get("preserve_hierarchy") is not False,
            include_manifest=payload.get("include_manifest") is not False,
            allow_remaining_issues=payload.get("allow_remaining_issues") is True if expected is not None else True,
            **({"expected_artifacts": expected, "report_assets": report_assets} if expected is not None else {}))
        _phase(job, "saving the package for download")
        if not _blob.upload_release_package(owner, scan_id, job["id"], output):
            raise FatalJobError("durable package storage is not configured")
    finally:
        if output is not None:
            output.close()


@handler("scheduled_sweep")
def _scheduled_sweep(payload: dict, job: dict) -> None:
    """Execute the one durable occurrence elected from all scheduler replicas."""
    if (getattr(core.get_store(), '_db', None) and job.get('id')
            and job.get('locked_by') and job.get('attempts')):
        from scheduled_scan_execution import run_tick
        return run_tick(payload, job)
    if payload.get("owner_email"):
        decision = core._scheduled_scan_admission(payload)
        # Admission counts active owner jobs. At handler time that population includes this
        # very scheduled_sweep (and the partial unique index guarantees there is no second
        # one). Do not make a concurrency limit of one reject itself forever.
        if (not decision.get("admit") and decision.get("reason") == "owner_concurrency_limit"
                and int(decision.get("owner_active") or 0) <= 1):
            decision = {**decision, "admit": True, "reason": None}
        if not decision.get("admit"):
            owner, key = payload["owner_email"], payload.get("occurrence_key")
            if decision.get("terminal"):
                core._schedule_lifecycle_complete(
                    core.get_store(), payload, result="skipped", error=decision.get("reason"))
                return
            defer = getattr(core.get_store(), "defer_scheduled_sweep", None)
            if callable(defer) and decision.get("run_after") and key:
                defer(owner, key, decision["run_after"],
                      decision.get("reason") or "queue_policy", payload.get("scheduled_for"))
            # The durable worker's ordinary failure path returns this same job to queued with
            # backoff.  No new occurrence key is minted, so fleet dedupe remains intact.
            raise RuntimeError(f"scheduled scan deferred: {decision.get('reason') or 'queue policy'}")
        core._do_scheduled_scan(payload)
    else:
        core._do_scheduled_scan()


# Longest-predicted work first reduces the tail of a parallel Assess run: without it, a large PDF
# or presentation that happens to be late in inventory order can occupy the final worker while all
# other slots sit idle. Size is the strongest metadata-only signal available before download. For
# native/cloud files whose source does not report bytes, use a deliberately modest format estimate
# so likely-expensive PDFs/slides still start ahead of tiny text documents. Python's sort is stable,
# so equal estimates preserve the source order and retries remain deterministic.
_ASSESS_UNKNOWN_SIZE_KB = {
    ".pdf": 8192,
    ".pptx": 4096,
    ".xlsx": 2048,
    ".docx": 1024,
    ".html": 256,
    ".htm": 256,
}


def _estimated_assess_work(item: dict) -> int:
    """Return a metadata-only work estimate in KiB-equivalent units."""
    try:
        size = item.get("size_kb")
        if size is not None:
            return max(0, int(size))
    except (TypeError, ValueError):
        pass
    from pathlib import Path as _Path
    return _ASSESS_UNKNOWN_SIZE_KB.get(_Path(item.get("file") or "").suffix.lower(), 0)


def _defer_analysis_to_assess() -> bool:
    """ADR 0020 — metadata-only discovery is now the DEFAULT. Discover only LISTS the estate
    (metadata, no file opened, nothing downloaded); the download + WCAG analysis run at Assess
    time instead. Read per-call so the behaviour can be overridden by env without a code change:
    set ACP_DEFER_ANALYSIS_TO_ASSESS=0 (or false/no/off) to force the legacy immediate-analysis
    scan that downloads and analyses at Discover time."""
    return _os.environ.get("ACP_DEFER_ANALYSIS_TO_ASSESS", "1").strip().lower() in ("1", "true", "yes", "on")


def _enqueue_analysis(scan_id: str, source: str, items: list[dict], *, ai: bool, pii: bool,
                      user: str | None, incremental: bool, exclude_remediated: bool,
                      force_batch: bool = False, parent_job: dict | None = None) -> None:
    """Fan out the download+analyse work over `items` — one scan_file per file, or scan_batch
    chunks for large estates (ADR 0008). Shared by the immediate scan path and the deferred
    Assess path so both enqueue identical work; the last completing job finalizes (ADR 0013)."""
    from store import logical_name as _logical_name
    from scanner import SCAN_BATCH_SIZE, SCAN_BATCH_THRESHOLD
    # A previous run of THIS scan may have halted on an unusable credential. Both the immediate
    # path and the deferred Assess path come through here, so this is the one place that has to
    # forget it — otherwise fixing the credential and pressing Assess again would short-circuit
    # every file against a stale marker and look like the fix had not worked.
    clear_drive_stop(scan_id)
    # ADR 0038 — same reasoning as clear_drive_stop above: any re-dispatch through this choke
    # point (resume included) must not find a stale pause marker and silently no-op every file.
    clear_scan_paused_marker(scan_id)
    if not items:
        core.store.enqueue_job("scan_finalize",
                               {"scan_id": scan_id, "source": source, "ai": ai, "pii": pii}, scan_id=scan_id)
        return
    # LPT scheduling is optimal for the two-file tail we repeatedly see in production: start the
    # likely stragglers while every worker slot is available, then drain the short documents.
    items = sorted(items, key=_estimated_assess_work, reverse=True)
    name_counts: dict[str, int] = {}
    for it in items:
        name_counts[_logical_name(it["file"])] = name_counts.get(_logical_name(it["file"]), 0) + 1
    pending = []
    def dispatch(kind, payload):
        if parent_job:
            pending.append((kind, payload))
        else:
            core.store.enqueue_job(kind, payload, scan_id=scan_id)
    use_batch = force_batch or len(items) >= SCAN_BATCH_THRESHOLD
    if use_batch:
        for i in range(0, len(items), SCAN_BATCH_SIZE):
            chunk = items[i:i + SCAN_BATCH_SIZE]
            dispatch("scan_batch", {
                "scan_id": scan_id, "source": source, "ai": ai, "pii": pii, "user": user,
                "incremental": incremental,
                "items": [{"file": it["file"], "drive_file_id": it.get("drive_file_id"),
                           "mime": it.get("mime"), "path": it.get("path"),
                           "checksum": it.get("checksum"), "drive_id": it.get("drive_id"),
                           "drive_account_id": it.get("drive_account_id"),
                           "source_modified": it.get("source_modified"),
                           "size_kb": it.get("size_kb"),
                           "shadow_candidate": name_counts[_logical_name(it["file"])] > 1,
                           "exclude_remediated": exclude_remediated} for it in chunk],
            })
    else:
        for it in items:
            dispatch("scan_file", {
                "scan_id": scan_id, "source": source, "file": it["file"],
                "drive_file_id": it.get("drive_file_id"), "mime": it.get("mime"), "path": it.get("path"),
                "checksum": it.get("checksum"), "drive_id": it.get("drive_id"),
                "drive_account_id": it.get("drive_account_id"),
                "source_modified": it.get("source_modified"),
                "size_kb": it.get("size_kb"),
                "shadow_candidate": name_counts[_logical_name(it["file"])] > 1,
                "exclude_remediated": exclude_remediated,
                "ai": ai, "pii": pii, "user": user, "incremental": incremental})
    if parent_job:
        from scheduled_scan_store import enqueue_contents
        enqueue_contents(core.store, parent_job, pending)
from remediate import remediate_html


def _drive_client(token: str):
    """Drive client for a worker (no request), from a bare GIS access token.

    NO expiry is set, and that is the point. google-auth attempts a refresh only when
    `credentials.expired` is True, and `expired` is False whenever `expiry` is None — so a
    credential with no expiry is sent as-is, forever. This used to set `expiry = now + 1h`
    under a comment claiming it PREVENTED the refresh; it caused it. Once that hour passed
    (a job queued behind a backlog, a long remediation), `expired` flipped True, google-auth
    called refresh(), and a GIS token has no refresh_token/client_id/client_secret — so the
    job died on "The credentials do not contain the necessary fields need to refresh the
    access token", five retries deep, against a failure nothing could fix.

    A GIS implicit-flow token genuinely cannot be refreshed. When it has really expired Drive
    answers 401, and worker.drive_session_expired turns that into one actionable dead-letter.
    Outliving the token needs a refresh token (the auth-code flow) — an auth change that wants
    an ADR, not a fabricated expiry."""
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build
    import httplib2
    from google_auth_httplib2 import AuthorizedHttp
    creds = Credentials(token=token, scopes=core.DRIVE_SCOPES)
    # Bounded socket — see core._DRIVE_HTTP_TIMEOUT_S / scanner._DRIVE_HTTP_TIMEOUT_S's identical
    # fix (found live 2026-08-29). `credentials=` builds its own AuthorizedHttp with no way to
    # carry a timeout, so a stalled write (not just a slow one) blocks this worker thread forever.
    http = AuthorizedHttp(creds, http=httplib2.Http(timeout=core._DRIVE_HTTP_TIMEOUT_S))
    return build("drive", "v3", http=http, cache_discovery=False)


def ensure_remediated_folder(svc) -> str:
    """Find-or-create the configured Drive mirror folder (default 'Remediated',
    admin-configurable — see core.store.get_drive_mirror_folder). If legacy
    duplicates exist, picks the oldest deterministically. Call this ONCE per
    remediate batch (in the request handler) and pass the id to the jobs — calling
    it concurrently from many workers is what created duplicate folders."""
    name = core.store.get_drive_mirror_folder()
    safe = name.replace("\\", "\\\\").replace("'", "\\'")
    q = f"name='{safe}' and mimeType='application/vnd.google-apps.folder' and trashed=false"
    folders = svc.files().list(q=q, fields="files(id)", orderBy="createdTime",
                               pageSize=1).execute().get("files", [])
    if folders:
        return folders[0]["id"]
    return svc.files().create(
        body={"name": name, "mimeType": "application/vnd.google-apps.folder"},
        fields="id").execute()["id"]


@handler("scan")
def _scan(payload: dict, job: dict) -> None:
    """Run a scan: discover → (analyse → score → persist → finalize).

    Metadata-only discovery is the DEFAULT (ADR 0020): this monolithic 'scan' job LISTS the estate
    and STOPS at a 'discovered' run, deferring the download + WCAG analysis to Assess — delegating
    to _scan_discover so it produces the exact same discovered state (inventory + assess_params +
    lifecycle + tracing) as the fan-out path, and the tokens are LEFT registered for that later
    Assess. Only when ACP_DEFER_ANALYSIS_TO_ASSESS=0 (the legacy override) does this download and
    analyse now, in which case it finalizes and clears the tokens here.

    payload: {source, scan_id, folder?, sp?, ai}
    The Drive/SharePoint tokens are looked up from the in-memory registry by
    scan_id (never carried in the job payload / Postgres)."""
    scan_id = payload.get("scan_id") or job.get("scan_id")
    if not scan_id:
        raise FatalJobError("scan job missing scan_id")
    # Fail closed on a missing source rather than defaulting to 'local'. 'local' scans the bundled
    # test corpus, not the production estate — a source-less job silently defaulting to it produces a
    # small scan that lands as 'latest' and collapses every dashboard/report/selector (the exact
    # fingerprint the production probe caught). The route always sets a source, so this only fires on
    # a malformed/legacy job, where a loud failure beats scanning test files.
    source = payload.get("source")
    if not source:
        raise FatalJobError("scan job missing source")
    ai = bool(payload.get("ai", True))
    effective_ai = ai and core.store.get_ai_enabled()

    if _defer_analysis_to_assess():
        # Metadata-only discovery: list + classify from metadata + persist inventory, then STOP.
        # Tokens stay registered so a later Assess can download; do NOT clear them here.
        _scan_discover(payload, job)
        return

    toks = core.get_scan_tokens(scan_id)

    inv: list = []
    report = run_scan(
        source,
        drive_token=toks.get("drive"),
        sp_token=toks.get("sp"),
        folder=payload.get("folder"),
        **({"folders": payload["folders"]} if payload.get("folders") else {}),
        **({"exclude_folders": payload["exclude_folders"]} if payload.get("exclude_folders") else {}),
        # Omit the new keyword for legacy/default jobs so older test doubles and downstream
        # wrappers retain the exact call shape they already support.
        **({"include_subfolders": False}
           if payload.get("include_subfolders", True) is False else {}),
        ai_enabled=effective_ai,
        scan_id=scan_id,
        user=payload.get("user"),
        detect_pii=payload.get("pii", False),
        exclude_remediated=bool(payload.get("exclude_remediated", False)),
        inventory_out=inv,
    )
    core.store.save_scan(report)
    # Persist per-file inventory + evaluate archival/deletion rules, as the fanout path does.
    persist_discovery_inventory(scan_id, inv, source, payload.get("user"))
    core.finalize_scan(scan_id, effective_ai, source)
    core.clear_scan_tokens(scan_id)


def _phase(job: dict, msg: str) -> None:
    """Report what this job is doing right now — the queue panel's per-row line reads it.

    Only ever called where the job genuinely changes activity, so the line stays true. Never
    raises: progress reporting must not be able to fail the work it reports on.
    """
    jid = (job or {}).get("id")
    if jid:
        core.store.set_job_phase(jid, msg)


def _remediation_scope(filename: str, scan_id: str):
    """The scope in force for THIS SCAN and file, as a `(sc) -> bool` predicate.

    The remediation-side twin of scanner._scoped_for_scoring (#107). That change made the
    `scan_scope` setting gate what gets ASSESSED and SCORED; nothing gated what gets FIXED, so
    a scoped scan still wrote changes into a customer's document for criteria they had
    explicitly excluded — and did it silently, since the resulting diffs were then filtered out
    of the score. Excluding a criterion has to mean ACP leaves it alone, not that ACP edits it
    and declines to mention it.

    PHASE 3a — resolved from THIS scan's FROZEN scope (store.get_scan_scope), not the live global
    `active_scope(core.store)`. This is the actual 3a bug: remediation read the live scope, so
    changing the operator's scope after a scan altered what that OLD scan would remediate while its
    frozen Assess counts stayed put — a Remediate/Assess contradiction. Reading the recorded
    per-scan scope makes remediation honour exactly the boundary the scan was assessed under. A
    legacy scan with nothing recorded → get_scan_scope None → predicate None → nothing gated, the
    same "unscoped behaves as before" contract as ever.

    Returns None only for a genuinely unrestricted legacy scan. Scope read failures propagate:
    inability to resolve authorization must never authorize every fixer.
    """
    from assessment_selection import selected_for_file
    scope = core.store.scope_for_file(scan_id, filename, core.store.get_scan_scope(scan_id, refresh=True))
    codes = selected_for_file(scope, filename)
    return None if codes is None else lambda sc: sc in codes


def _remediation_fix_scope(filename: str, scan_id: str, failing_auto_rules):
    """Gate mutations by both frozen policy scope and Assessment evidence.

    Scan scope says what ACP may inspect; it does not say every criterion failed. Previously a
    PDF with one auto-fixable failure still traversed every document-wide fixer allowed by that
    broad scope. Only criteria that Assessment observed failing in deterministic ``auto`` mode
    are eligible to mutate here. An empty/unavailable evidence list permits no mutation.
    """
    configured = _remediation_scope(filename, scan_id)
    eligible = frozenset(str(rule) for rule in (failing_auto_rules or ()) if rule)
    return lambda sc: sc in eligible and (configured is None or bool(configured(sc)))


def _verify_residual(fixed_bytes: bytes, filename: str, scan_id: str | None = None):
    """Re-scan the remediated bytes and return a `proposals.Verification` — verified-cleared,
    verified-still-failing, or COULD-NOT-VERIFY. Delegates to the single shared implementation
    in api/proposals.py — the proposal lane and this loop must use the exact same residual
    re-scan (one whole-file path, never a divergent copy).

    Both credit-granting call sites below go through this, and ask `Verification.cleared()`
    rather than testing the residual themselves. That is the whole point: a re-scan that could
    not run returns `ok=False`, and `cleared()` refuses to credit it. The previous shim
    returned `set | None` and every caller read `None` as "credit it", which published
    unreadable documents as remediated."""
    from proposals import verify_residual
    return verify_residual(fixed_bytes, filename, **({"scan_id": scan_id} if scan_id else {}))


def _verify_residual_scs(fixed_bytes: bytes, filename: str):
    """OBSERVATIONAL shim kept for tests that ask what a scan reports. NOT for granting
    credit — use `_verify_residual` and `Verification.cleared()`. See the docstring on
    proposals.verify_residual_scs for why the distinction is load-bearing."""
    from proposals import verify_residual_scs
    return verify_residual_scs(fixed_bytes, filename)


def _propose_text_findings(scan_id: str, filename: str, file_bytes: bytes, ai_enabled: bool) -> None:
    from assessment_selection import selected_for_file, selection
    scope = core.store.scope_for_file(scan_id, filename, core.store.get_scan_scope(scan_id, refresh=True))
    from ai_run_policy import optional_current_run_context
    from document_wide_workflow import suppressed_criteria
    selected = selected_for_file(scope, filename)
    suppressed = suppressed_criteria(optional_current_run_context(), filename)
    if suppressed:
        # Managed document mode has a frozen explicit scope; no duplicate per-image call.
        selected = set(selected or ()) - suppressed
    with selection(selected):
        return _propose_text_findings_selected(scan_id, filename, file_bytes, ai_enabled)


def _propose_text_findings_selected(scan_id: str, filename: str, file_bytes: bytes, ai_enabled: bool) -> None:
    """Format-agnostic proposers (WCAG 3.1.2 language-of-parts, 1.3.3 sensory rewrite, and
    1.4.5 images-of-text). All self-gate: they yield proposals ONLY when the document actually
    mixes languages / carries a sensory instruction / bakes text into an image, so this is safe
    to run on every remediated file. The text proposers run on the extracted text (same source
    as the scan-time detectors); the 1.4.5 proposer OCRs the embedded images off the temp path.
    Enqueues prefilled one-click values onto the file's HITL rows, and never fails the job."""
    # ADR 0021 stage 2 — org house-style guidance for the prose drafts. Computed once per file
    # from the scan owner; per-rule. "" (flag off / no rules / any lookup error) leaves every
    # proposer's prompt byte-identical to pre-memory.
    try:
        import memory as _mem
        _org = (core.store.get_scan(scan_id) or {}).get("run", {}).get("owner_email")
        _fmt = filename.rsplit(".", 1)[-1].lower() if "." in filename else None

        def _g(rule):
            return _mem.guidance_for(core.store, _org, rule, _fmt)

        # ADR 0021 §E — the rules behind that guidance, so the review card can show WHICH house
        # style shaped a scan-time draft (the chip #999 built for the live /ai/suggest path).
        # Same store.memory_applied_rules call `_g` composed its prompt block from, so the chip
        # reports what the proposer was actually asked rather than a second lookup that could
        # disagree with it.
        #
        # Passed ONLY at the call sites that pass `guidance=_g(...)`. That is the whole design
        # constraint: `_enqueue_proposals` is reached by 12 criteria and only five of them see
        # guidance at all — reading order, the colour/contrast cards, the one-click layout fixes,
        # chart datasheets and the language tags are deterministic, and ADR 0021 says so outright
        # ("Deterministic proposers … ignore memory — there is nothing to steer"). Stamping at the
        # choke point would put "house style applied" on drafts memory never touched, which is
        # precisely the claim the chip exists to make impossible.
        def _hs(rule):
            return _mem.applied_rules(core.store, _org, rule, _fmt)
    except Exception:
        def _g(rule):
            return ""

        def _hs(rule):
            return []
    try:
        import tempfile
        from pathlib import Path as _P
        import pii as _pii
        import proposals as _prop
        with tempfile.TemporaryDirectory(prefix="acp-textprop-") as _d:
            p = _P(_d) / filename
            p.write_bytes(file_bytes)
            text = _pii.extract_text(p)
            # 1.4.5 needs the file on disk (it OCRs embedded images), so compute it here while
            # the temp path is alive — and independently of `text`, since an image-only doc
            # carries no extractable text yet still fails 1.4.5.
            image_text = (_prop.propose_images_of_text(p, p.suffix) if ai_enabled else
                          _prop.propose_images_of_text(p, p.suffix, ai_enabled=False))
            # 2.4.4/2.4.9 link-text proposals read the OOXML zip, so same constraint.
            link_props = (_prop.propose_link_texts(p, p.suffix, ai_enabled=ai_enabled, guidance=_g("2.4.4"))
                          if p.suffix.lower() in (".docx", ".pptx", ".xlsx") else [])
            # 2.4.10 section-heading drafts (docx only) — likewise zip-bound.
            section_heads = _prop.propose_section_headings(p, p.suffix, ai_enabled=ai_enabled,
                                                           guidance=_g("2.4.10"))
            # 2.4.6 slide-title drafts (pptx only) — likewise zip-bound.
            slide_titles = _prop.propose_slide_titles(p, p.suffix, ai_enabled=ai_enabled,
                                                      guidance=_g("2.4.6"))
            # 2.4.6 xlsx label drafts — default sheet tabs / table columns get an AI-named label.
            xlsx_labels = _prop.propose_xlsx_labels(p, p.suffix, ai_enabled=ai_enabled,
                                                    guidance=_g("2.4.6"))
            # 1.3.2 docx reading-order recommendations (floating text boxes / frames) — deterministic.
            reading_order = _prop.propose_reading_order(p, p.suffix)
            # One-click deterministic layout cards (no AI): docx 1.4.8 + pptx 1.4.2.
            one_clicks = (_prop.propose_justified_fix(p, p.suffix)
                          + _prop.propose_autoplay_fix(p, p.suffix))
            # docx 1.4.1 / 1.4.11 — same shape, own criteria. Kept out of `one_clicks` above
            # because that list is enqueued under ONE criterion per format, and a colour card
            # filed under 1.4.8 would tell a reviewer they are fixing visual presentation.
            colour_cards = _prop.propose_underline_restore(p, p.suffix)
            contrast_cards = _prop.propose_outline_contrast(p, p.suffix)
            # 1.1.1 native-chart datasheets (docx/pptx/xlsx) — grounded alt from the chart's data.
            chart_sheets = _prop.propose_chart_datasheet(p, p.suffix)
            # 1.1.1 image alt — enumerate every unlabelled image and PRE-DRAFT it (vision when
            # reachable), so the review card arrives with a per-image thumbnail + AI description
            # for each, not a single "author it yourself" template. Reuses the fix-time alt logic.
            img_props, img_evidence = ([], [])
            if p.suffix.lower() in (".docx", ".pptx", ".xlsx") and _prop.criteria_enabled("1.1.1"):
                try:
                    from remediate_office import alt_proposals_for_office
                    img_props, img_evidence = alt_proposals_for_office(
                        file_bytes, p.suffix, ai_enabled=ai_enabled, scan_id=scan_id,
                        context_file=filename, guidance=_g("1.1.1"))
                    # STAMPED HERE, NOT AT THE ENQUEUE. 1.1.1 is the one criterion whose batch
                    # is MIXED: chart datasheets are deterministic (grounded in the chart's own
                    # cells, no prompt, no house style) and these image drafts are model-written
                    # with the guidance above. `_enqueue_proposals(house_style=...)` stamps every
                    # proposal it is given, so passing it there would hand the datasheets a chip
                    # claiming an influence they never had — the exact over-claim that argument
                    # is written to prevent, reached from the other side. Stamping the subset
                    # that was actually shaped keeps the chip true per proposal.
                    _hs_alt = _hs("1.1.1")
                    if _hs_alt and img_props:
                        img_props = [{**_p, "house_style": _hs_alt} for _p in img_props]
                except Exception:
                    img_props, img_evidence = [], []
    except Exception:
        return
    if text:
        try:
            # P4.4 — independent verification gate: re-run detect_langs on each proposed span
            # before it reaches the queue. 3.1.2 is fully verifiable (langdetect, seed-fixed)
            # so proposals that pass get validated=True; any that fail the re-check are still
            # enqueued but stay validated=False. One call to _enqueue_proposals — the store
            # replaces on conflict, so two calls would discard the first batch.
            _lang_props = _prop.propose_language_parts(text)
            if _lang_props:
                _all_verified = all(
                    _prop.verify_language_part(p["before"], p["proposed_value"])
                    for p in _lang_props)
                _enqueue_proposals(scan_id, filename, "3.1.2", "Language of Parts",
                                   _lang_props, validated=_all_verified)
        except Exception:
            swallowed("_propose_text_findings: enqueueing 3.1.2 Language of Parts proposals failed", scan_id)
        try:
            _enqueue_proposals(scan_id, filename, "1.3.3", "Sensory Characteristics",
                               _prop.propose_sensory_rewrite(text, filename=filename,
                                                             ai_enabled=ai_enabled, guidance=_g("1.3.3")),
                               house_style=_hs("1.3.3"))
        except Exception:
            swallowed("_propose_text_findings: enqueueing 1.3.3 Sensory Characteristics proposals failed", scan_id)
        try:
            _enqueue_proposals(scan_id, filename, "3.1.5", "Reading Level",
                               _prop.propose_reading_level(text, filename=filename, ai_enabled=ai_enabled))
        except Exception:
            swallowed("_propose_text_findings: enqueueing 3.1.5 Reading Level proposals failed", scan_id)
    # 2.4.10 — AI-drafted section headings for a long, heading-less docx (reads the zip, so
    # computed above while the temp path was alive; self-gates on the detector's conditions).
    try:
        _enqueue_proposals(scan_id, filename, "2.4.10", "Section Headings", section_heads,
                           house_style=_hs("2.4.10"))
    except Exception:
        swallowed("_propose_text_findings: enqueueing 2.4.10 Section Headings proposals failed", scan_id)
    # 2.4.6 — AI slide-title drafts for pptx title placeholders left empty.
    try:
        _enqueue_proposals(scan_id, filename, "2.4.6", "Headings and Labels", slide_titles + xlsx_labels,
                           house_style=_hs("2.4.6"))
    except Exception:
        swallowed("_propose_text_findings: enqueueing 2.4.6 Headings and Labels proposals failed", scan_id)
    # 1.3.2 — reading-order recommendations for docx floating text boxes / frames.
    try:
        _enqueue_proposals(scan_id, filename, "1.3.2", "Meaningful Sequence", reading_order)
    except Exception:
        swallowed("_propose_text_findings: enqueueing 1.3.2 Meaningful Sequence proposals failed", scan_id)
    # One-click deterministic layout cards — the fix is exact, the human elects it.
    try:
        if filename.lower().endswith(".docx"):
            _enqueue_proposals(scan_id, filename, "1.4.8", "Visual Presentation", one_clicks)
            _enqueue_proposals(scan_id, filename, "1.4.1", "Use of Color", colour_cards)
            _enqueue_proposals(scan_id, filename, "1.4.11", "Non-text Contrast", contrast_cards)
        else:
            _enqueue_proposals(scan_id, filename, "1.4.2", "Audio Control", one_clicks)
    except Exception:
        swallowed("_propose_text_findings: enqueueing the one-click/colour/contrast proposals failed", scan_id)
    # 1.1.1 — chart datasheets + per-image alt drafts (vision when reachable) go on the SAME
    # 1.1.1 card in one enqueue: enqueue_proposals REPLACES per (scan,file,rule), so two calls
    # would clobber. Together they turn a single fill-in template into an editable AI
    # description per image/chart.
    try:
        _enqueue_proposals(scan_id, filename, "1.1.1", "Non-text Content",
                           (chart_sheets or []) + (img_props or []))
    except Exception:
        swallowed("_propose_text_findings: enqueueing 1.1.1 Non-text Content proposals failed", scan_id)
    try:
        if img_evidence:
            core.store.attach_hitl_evidence(scan_id, filename, "1.1.1", img_evidence)
    except Exception:
        swallowed("_propose_text_findings: attaching 1.1.1 image evidence failed", scan_id)
    try:
        for sc in ("1.4.5", "1.4.9"):
            _enqueue_proposals(scan_id, filename, sc, "Images of Text",
                _mark_describable([p for p in image_text if p.get("sc", "1.4.5") == sc],
                                  file_bytes, filename, scan_id))
    except Exception:
        swallowed("_propose_text_findings: enqueueing 1.4.5 Images of Text proposals failed", scan_id)
    # 2.4.4 / 2.4.9 — descriptive link-text proposals for Office hyperlinks (vague text /
    # text reused across destinations). Needs the file on disk like the OCR proposer, so it
    # was computed above while the temp path was alive; split by the sc each proposal carries.
    try:
        # Both criteria carry _hs("2.4.4"), not each their own, because both halves of
        # `link_props` came out of ONE propose_link_texts call built with `guidance=_g("2.4.4")`.
        # The chip reports the rules the prompt actually received, so a 2.4.9 card can show a
        # rule scoped to WCAG 2.4.4 — which reads oddly but is the true answer. Looking up
        # _hs("2.4.9") here would look tidier and would name rules that shaped nothing.
        for sc, rule_name in (("2.4.4", "Link Purpose (In Context)"),
                              ("2.4.9", "Link Purpose (Link Only)")):
            _enqueue_proposals(scan_id, filename, sc, rule_name,
                               [p for p in link_props if p.get("sc") == sc],
                               house_style=_hs("2.4.4"))
    except Exception:
        swallowed("_propose_text_findings: enqueueing 2.4.4/2.4.9 Link Purpose proposals failed", scan_id)


def _propose_form_fields(scan_id: str, filename: str, file_bytes: bytes, ai_enabled: bool) -> None:
    """AI-assisted labels for unlabeled docx content-control form fields (WCAG 3.3.2 Labels or
    Instructions — the SC the scanner flags them under). Self-gates: yields a proposal only for
    an interactive content control that lacks a title, so it's safe to run on every docx. The
    label is derived from the field's adjacent prompt text (deterministic) and falls back to the
    local text model where there's no adjacent prompt — always a one-click value a human
    approves, never auto-applied. Enqueued only under 3.3.2 (never a fabricated 4.1.2 row).
    Never fails the remediation job."""
    allows = _remediation_scope(filename, scan_id)
    if allows is not None and not allows("3.3.2"):
        return
    try:
        import io as _io
        import propose_forms as _pf
        props = _pf.form_field_proposals(_io.BytesIO(file_bytes), filename=filename,
                                         ai_enabled=ai_enabled)
    except Exception:
        return
    _enqueue_proposals(scan_id, filename, "3.3.2", "Labels or Instructions", props)


def _record_applied_fixes(scan_id: str, filename: str, fixes: list) -> None:
    """Persist the concrete values the AI actually wrote — the alt text and the picture it
    was written for — so "Recent AI fixes" and the certification evidence show what really
    happened, per format, identically.

    Every remediator emits the same row shape: {rule_id, value, source, thumb}. `seq` orders
    them within a (scan, file, rule) so several figures on one criterion each keep a row —
    the table's primary key is (scan_id, file, rule_id, seq).

    Best-effort per row: a telemetry failure must never fail the remediation job, and one bad
    row must not discard the rest."""
    for i, fx in enumerate(fixes or []):
        try:
            core.store.record_applied_fix(
                scan_id, filename, fx["rule_id"], fx["value"],
                source=fx.get("source"), thumb=fx.get("thumb"), seq=i)
            validation = fx.get("caption_validation") or {}
            if validation.get("approved") is True and validation.get("status") == "validated":
                evidence = validation.get("evidence") or {}
                detail = {key: evidence[key] for key in ("version", "image_sha256", "method") if key in evidence}
                core.store.log_decision("system", "remediate.caption_validated", scan_id=scan_id,
                    file=filename, rule_id=fx["rule_id"], detail=__import__("json").dumps(detail, sort_keys=True))
        except Exception:
            swallowed("_record_applied_fixes: recording an applied fix failed", scan_id)


def _mark_describable(proposals: list, file_bytes: bytes, filename: str, scan_id: str) -> list:
    """Stamp each 1.4.5 proposal with whether an applier could ever write alt text to its image.

    ADR 0055 lets a reviewer KEEP an image of text and describe it, which records the description
    as 1.1.1 alt text the document owes. That is only honest if something can write it. But
    `ocr._ooxml_images` walks the whole ZIP NAMELIST while the appliers reach only the parts in
    `formats/office/images.ALT_TARGETS`, so a media part that NO alt-bearing part references — a
    Word footnote image, a VML sheet graphic — gets a review card no writer can action. #1767
    widened that reach to layouts and masters and made the remaining wedge VISIBLE; this is what
    stops it being accepted in the first place.

    THE REACHABILITY QUESTION IS ASKED OF THE RESOLVER ITSELF, never re-implemented. A locator
    resolves iff `resolve_media_locators` returns placements for it, so the check and the write
    cannot disagree about what is addressable — the drift CLAUDE.md records this repo losing days
    to. `describable` is therefore exactly "the translation would produce somewhere to write".

    COMPUTED HERE, WHERE THE BYTES ARE ALREADY OPEN, and that placement is the design. The
    alternative was a blob read inside the review request, which #1742 deliberately avoided:
    'image N' is a media INDEX, and resolving it against a copy that is not the one it was minted
    from can name a DIFFERENT PICTURE. Doing it at propose time removes that risk rather than
    managing it — the flag describes the same bytes the card does.

    Safe across the original -> remediated hop, and that was checked rather than assumed:
    remediate_office rebuilds the package from `z.namelist()` and writes every entry back,
    deleting no media, so the media list the reviewer's card was minted against is the one the
    apply job resolves. (The 1.4.5 REPLACEMENT lane does delete a media part — but that is a
    different decision on a different row, and it removes the picture rather than describing it.)

    Best-effort by construction: any failure leaves the proposals unstamped, and an unstamped
    proposal is treated as describable downstream. That is the pre-#1767 behaviour — the decision
    is accepted and, if it cannot be written, the reviewer now gets a card saying so — so a
    broken check degrades to the status quo instead of refusing work that would have succeeded.
    """
    if not proposals:
        return proposals
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    try:
        from apply_office_image_of_text import SUPPORTED_EXTS, resolve_media_locators
        if ext not in SUPPORTED_EXTS:
            return proposals                      # pdf and html mint other locator shapes
        locators = [str(p.get("locator") or "").strip()
                    for p in proposals if isinstance(p, dict)]
        reachable = set(resolve_media_locators(file_bytes, locators, ext))
    except Exception:
        swallowed("_propose_text_findings: reading image reachability failed", scan_id)
        return proposals
    for p in proposals:
        if isinstance(p, dict):
            p["describable"] = str(p.get("locator") or "").strip() in reachable
    return proposals


def _enqueue_proposals(scan_id: str, filename: str, sc: str, rule_name: str,
                       proposals: list, *, validated: bool = False,
                       house_style: list | None = None) -> None:
    """Best-effort: attach AI-proposed (not auto-applied) fix values to the file's HITL row
    for this SC, so the reviewer approves a prefilled value in one click. Never fails the
    remediation job — a telemetry/queue error just means the finding routes as a plain
    deferral. `validated` stays False for model/heuristic proposals (a machine guess a human
    confirms), so confidence.js surfaces them as Medium/Low, never a trusted 'fixed'.

    `house_style` is the org review-memory rules that shaped these drafts' prompt (ADR 0021 §E),
    for the card's "house style applied" chip. OPTIONAL AND DEFAULTED TO NONE ON PURPOSE, and
    that default is the safety property, not a convenience: this function is the choke point for
    12 criteria and only five of them are given guidance at all. A caller that does not pass it
    — every deterministic proposer — cannot accidentally acquire a chip claiming an influence its
    draft never had. Passing it here rather than deriving it here is deliberate for the same
    reason: derived, it would apply to all 12."""
    if not proposals:
        return
    from ai_run_policy import optional_current_run_context
    from document_wide_workflow import suppressed_criteria
    context = optional_current_run_context()
    if sc in suppressed_criteria(context, filename):
        try:
            import blob
            from remediate_pdf import bind_exact_pdf_findings
            if sc != '1.1.1' or not filename.lower().endswith('.pdf'):
                return
            data = blob.download_remediated(context.owner_id, scan_id, filename)
            proposals = bind_exact_pdf_findings(core.store, context.owner_id, scan_id, context.run_id, filename, data, proposals)
        except (ValueError, TypeError):
            return
    # OPERATOR SCOPE. One gate here covers every proposer — 19 call sites across 12 criteria —
    # because this is the single boundary where a proposal is still labelled with its SC. Gating
    # at each proposer instead would be 19 chances to forget one, and the one forgotten is the
    # one that writes into an excluded criterion.
    #
    # Suppression is RECORDED, never silent: an operator who narrowed the scope should be able to
    # see that the narrowing is what stopped a fix, rather than wonder why a known finding never
    # produced a review card.
    allows = _remediation_scope(filename, scan_id)
    if allows is not None and not allows(sc):
        try:
            core.store.log_decision("system", "remediate.out_of_scope", scan_id=scan_id,
                                    file=filename,
                                    detail=f"{sc} is outside the operator scope — "
                                           f"{len(proposals)} proposal(s) not enqueued")
        except Exception:
            swallowed("_enqueue_proposals: logging the remediate.out_of_scope decision failed", scan_id)
        return
    # Stamp the applied house style onto each proposal. It rides inside the existing `proposals`
    # JSON blob — no schema change: store._decode_proposals is a plain json.loads and
    # routes/hitl.py returns the rows unfiltered, so an extra key reaches the SPA intact.
    #
    # On every proposal rather than just the first: the value is card-level (it keys on org +
    # criterion + format, identical across a card's instances), but reading it off index 0 would
    # make the chip depend on list ORDER, and the frontend reads the first proposal that carries
    # one instead. A few hundred bytes per instance is the right price for not having an
    # ordering assumption in a claim about what shaped a draft.
    if house_style:
        proposals = [{**p, "house_style": house_style} for p in proposals]
    try:
        core.store.enqueue_proposals(scan_id, filename, sc, proposals,
                                     validated=validated, rule_name=rule_name)
    except Exception:
        swallowed("_enqueue_proposals: store.enqueue_proposals failed — EVERY proposal for this (file, "
                   "criterion) is lost, and the reviewer sees a document with no suggested fixes", scan_id)
        return
    # ADR 0041 auto-apply gate: skip human review for Group A SCs whose fix was already written
    # inline AND confirmed by structural re-scan (validated=True). The gate is at this choke
    # point because every proposal for every SC passes through here — one gate, not one per caller.
    if validated and sc in {"2.4.4", "2.4.9", "4.1.2"}:
        try:
            item_id = core.store.auto_approve_proposals(scan_id, filename, sc)
            if item_id:
                core.store.log_decision(
                    "system", "hitl.auto_approved", scan_id=scan_id, file=filename,
                    detail=f"{sc}: validated fix auto-approved by ADR 0041 gate (no human review)")
                try:
                    if core.store.mark_file_compliant_if_reviewed(scan_id, filename):
                        core.store.log_decision(
                            "system", "revalidate.certified", scan_id=scan_id, file=filename,
                            detail="all findings resolved — certified & advanced to Publish "
                                   "(ADR 0041 auto-approve)")
                except Exception:
                    swallowed("_enqueue_proposals: certify after auto-approve failed", scan_id)
        except Exception:
            swallowed("_enqueue_proposals: ADR 0041 auto-approve gate failed — "
                      "proposal stays pending for human review", scan_id)


# Media extensions the remediation lane admits. A SUBSET of scan_formats' "av" list, and
# deliberately not all of it: these are the container/codec combinations ffmpeg decodes to 16 kHz
# mono without surprises, and admitting one ACP cannot decode would enqueue a job whose only
# outcome is a deferral it could have predicted.
_AV_REMEDIABLE = (".mp4", ".m4v", ".mov", ".webm", ".mkv", ".mp3", ".m4a", ".wav", ".aac", ".flac")


def remediable_extensions() -> tuple[str, ...]:
    """Every extension with a server-side remediation path, dot-prefixed.

    ONE PREDICATE, because there were two literals and nothing made them agree. The route
    (`POST /scans/{sid}/remediate`) decided what to enqueue and `_remediate_file` decided what to
    accept, each spelling out `(".html", ".htm", ".pdf", ".docx", ".pptx", ".xlsx")` in its own
    words. Neither direction of drift fails loudly: an extension the route admits and the handler
    refuses burns a job and logs a deferral, and one the handler admits and the route does not is
    a code path nothing can reach. Adding media would have needed both edits, and getting one is
    exactly the shape of mistake nobody notices until a feature "does not work".

    Media earns its place here only because it now HAS a path — `_propose_media_captions` drafts a
    caption file. It is not a rewriter: nothing in this lane re-encodes a customer's video.
    """
    return (".html", ".htm", ".pdf", ".docx", ".pptx", ".xlsx", *_AV_REMEDIABLE)


def _propose_media_captions(scan_id: str, filename: str, drive_file_id: str,
                            payload: dict) -> None:
    """Draft captions (1.2.2) or a transcript (1.2.1) for one media file and enqueue the card.

    Never raises and never rewrites the media. The download is the only expensive step ACP takes
    on the customer's behalf here, and it happens before the engine probe on purpose: `probe`
    needs the bytes, and a media file's duration — the thing the cap is applied to — is not in
    any listing metadata ACP has.

    A file with no draft is NOT an error and is not logged as a failure. Every refusal inside
    `proposals.propose_captions` (no transcriber, no soundtrack, over the duration cap, captions
    already present) is a reason not to offer a draft, never a reason to suppress the finding —
    the 1.2.2 row stands and a person authors captions as before. `remediate.deferred` records
    which of those it was, so a reviewer looking at a card-less finding can tell "too long" from
    "no engine on this deployment" without reading source.
    """
    allows = _remediation_scope(filename, scan_id)
    if allows is not None and not any(allows(sc) for sc in ("1.2.1", "1.2.2")):
        return
    import tempfile
    from pathlib import Path as _P

    # Through the same source dispatch as the document lane: a local or SharePoint media file
    # reads the bytes Assess cached, and only a Drive job asks for a Drive token. This branch
    # used to demand one unconditionally, so the caption draft for a SharePoint (or local)
    # recording died on a token that source never had.
    try:
        data, _svc = _remediation_source_bytes(scan_id, filename, payload, drive_file_id)
    except FatalJobError:
        raise          # no bytes and no way to get them — the job's own failure, not a swallow
    except Exception:
        swallowed(f"_propose_media_captions: downloading {filename} failed", scan_id)
        return

    props = []
    try:
        import proposals as _prop
        with tempfile.TemporaryDirectory(prefix="acp-mediaprop-") as d:
            p = _P(d) / filename
            p.write_bytes(data)
            from assessment_selection import selected_for_file, selection
            scope = core.store.scope_for_file(scan_id, filename, core.store.get_scan_scope(scan_id, refresh=True))
            with selection(selected_for_file(scope, filename)):
                props = _prop.propose_captions(p, p.suffix)
    except Exception:
        swallowed(f"_propose_media_captions: drafting captions for {filename} failed", scan_id)
        return

    if not props:
        try:
            import media as _media
            reason = (_media.engine_status().get("reason")
                      or "no draft could be made (no speech found, no soundtrack, captions "
                         "already present, or the recording is longer than the transcription cap)")
            core.store.log_decision("system", "remediate.deferred", scan_id=scan_id,
                                    file=filename, detail=f"media: {reason}"[:200])
        except Exception:
            swallowed("_propose_media_captions: logging the no-draft reason failed", scan_id)
        return

    # validated=False, always. A transcription is a machine guess a person confirms — there is no
    # re-scan that could validate it, because the caption file is not in the document and a
    # cleared re-scan would only prove the finding stopped firing, never that the words are right.
    sc = props[0].get("sc", "1.2.2")
    name = "Captions (Prerecorded)" if sc == "1.2.2" else "Audio-only & Video-only"
    try:
        _enqueue_proposals(scan_id, filename, sc, name, props, validated=False)
    except Exception:
        swallowed(f"_propose_media_captions: enqueueing the {sc} proposal failed", scan_id)


# Every source a remediate_file job can carry. Anything else is a bug in the enqueuer, and the
# `else` that used to catch it sent the job to Drive (see _remediation_source_bytes).
REMEDIATION_SOURCES = ("drive", "local", "sharepoint")


def _release_failure(release_id: str, owner: str, filename: str, record: dict,
                     category: str, explanation: str) -> None:
    """Persist one safe Release failure; provider exception text never crosses the API."""
    core.store.record_release_document(release_id, owner, {
        "file": filename,
        "source_document_id": record.get("drive_file_id") or filename,
        "original_relative_path": (record.get("source_relative_path")
                                   or record.get("parent_folder") or filename),
        "released_relative_path": None,
        "status": "failed",
        "failure_category": category,
        "artifact_digest": 'sha256:' + record['corrected_sha256'] if record.get('corrected_sha256') and category in {'release_assessment_remaining', 'release_assessment_unavailable', 'corrected_copy_unreadable'} else None,
        "explanation": explanation,
        "created": False,
    })


@handler("publish_file")
def _publish_file(payload: dict, job: dict) -> None:
    if payload.get("automatic_release_id"):
        from automatic_release import publish_job
        return publish_job(core.store, payload, job, _publish_file_guarded)
    result = _publish_file_guarded(payload, job)
    from release_report_delivery import queue_if_release_settled
    queue_if_release_settled(core.store, payload["scan_id"], payload["owner"], payload.get("release_id"))
    return result


def _publish_file_guarded(payload: dict, job: dict) -> None:
    """Durably publish one approved corrected copy to its source provider.

    Tokens are resolved from the short-lived Redis token store at execution time and are never
    placed in the durable job payload. One file per job makes a deployment/restart resumable and
    bounds each Graph operation independently.
    """
    scan_id = payload.get("scan_id") or job.get("scan_id")
    filename = payload.get("file")
    owner = payload.get("owner")
    release_id = payload.get("release_id")
    if not all((scan_id, filename, owner, release_id)):
        raise FatalJobError("publish_file job missing release identity")
    scan = core.store.get_scan(scan_id, owner=owner)
    source = ((scan or {}).get("run") or {}).get("source")
    source_origin = source
    if not scan or source not in {"sharepoint", "drive", "local"}:
        raise FatalJobError("publish_file job is not an owned supported cloud scan")
    provider = "Google Drive" if source == "drive" else "SharePoint"
    release = core.store.release_status(release_id, owner)
    if not release or release.get("scan_id") != scan_id:
        raise FatalJobError("release execution does not belong to this scan")
    if source == "local":
        source = release.get("source")
        if source not in {"drive", "sharepoint"}:
            raise FatalJobError("Uploaded cloud delivery needs a saved cloud destination")
    provider = "Google Drive" if source == "drive" else "SharePoint"
    from release_artifacts import release_ready, release_review_evidence
    allow_remaining_issues = payload.get("allow_remaining_issues") is True
    record = core.store.get_file_record(scan_id, filename)
    if not release_ready(record, allow_remaining_issues):
        _release_failure(release_id, owner, filename, record or {}, "not_approved",
                         "Only approved corrected copies can be released.")
        return
    if allow_remaining_issues and (not payload.get("artifact_digest") or not payload.get("remediated_at")):
        raise FatalJobError("Release with remaining issues requires exact artifact authorization")
    saved = core.store.get_release_document(release_id, filename, owner)
    def require_reconnect():
        if source in {"drive", "sharepoint"} and payload.get("automatic_release_id"):
            import automatic_release_store as persistence
            persistence.update_file(core.store, payload["automatic_release_id"], owner, filename,
                                    {"state": "blocked", "requires_reconnect": True,
                                     "waiting_for_delivery": False,
                                     "message": f"Reconnect {provider} with write access to resume delivery."})

    token = core.get_scan_tokens(scan_id).get("drive" if source == "drive" else "sp")
    if not token:
        require_reconnect()
        _release_failure(release_id, owner, filename, record,
                         "provider_session_expired",
                         f"Reconnect {provider} and retry this document.")
        # Dead, not done: enqueue_stage_batch deliberately revives failed terminal rows when the
        # user retries with a fresh token. Marking this successful would make Retry a no-op.
        raise FatalJobError(f"{provider} session expired — reconnect and retry")
    import publish as _publish
    from release_artifacts import ReleaseArtifactError, artifact_tag, reuse_state, require_current_record, require_current_source
    import scanner as _scanner
    source_path = record.get("source_relative_path") or record.get("parent_folder") or filename
    source_name = record.get("source_name") or filename
    source_id = record.get("drive_file_id") or filename
    drive_id = record.get("drive_id")
    location = "google:me" if source == "drive" else f"graph:{drive_id or 'me'}"
    content_digest = None
    reservation = None
    target_file_id = None
    reconcile_only = False

    class PublicationReservationRetry(ReservationRetryError, RuntimeError):
        """Preserve the publication error contract while requesting a lease-aware retry."""

    def reservation_retry(message):
        from datetime import datetime, timezone
        remaining = 300
        if (reservation or {}).get("lease_expires_at"):
            expires = datetime.fromisoformat(reservation["lease_expires_at"].replace("Z", "+00:00"))
            remaining = max(1, (expires - datetime.now(timezone.utc)).total_seconds() + 1)
        return PublicationReservationRetry(message, retry_after_seconds=remaining)

    def log_provider_failure(exc):
        from routes.scans import _log_release_provider_error
        return _log_release_provider_error(
            exc, scan_id=scan_id, release_id=release_id, file=filename,
            provider=source, uncertain=bool(reservation and reservation.get("acquired")))

    try:
        drive_svc = _drive_client(token) if source == "drive" else None
        content_digest = _publish.remediated_content_digest(owner, scan_id, filename)
        if not content_digest:
            raise IOError("corrected content was unavailable")
        if payload.get("artifact_digest") and payload["artifact_digest"] != artifact_tag(content_digest):
            raise ReleaseArtifactError("The corrected artifact changed after this release was requested.")
        record = require_current_record(core.store, scan_id, filename, content_digest,
                                        payload.get("remediated_at") or record.get("remediated_at"), owner=owner, allow_remaining_issues=allow_remaining_issues)
        require_current_source(source_origin, record, sp_token=token, drive_service=drive_svc)
        record = require_current_record(core.store, scan_id, filename, content_digest, record.get("remediated_at"), owner=owner, allow_remaining_issues=allow_remaining_issues)
        identity = reuse_state(saved, content_digest)
        if identity == "unresolved":
            raise ReleaseArtifactError("Prior delivery has no exact artifact digest. Reconcile that delivery before retrying.", category="delivery_version_unresolved")
        if identity == "reuse":
            return
        _phase(job, "assessing the saved corrected copy before publishing")
        from release_candidate_assessment import assess_candidate
        candidate_assessment = assess_candidate(
            core.store, scan_id, filename, owner, content_digest, record.get("remediated_at"),
            allow_remaining_issues=allow_remaining_issues, release_id=release_id)
        chosen_parent = release.get("parent_folder_id")
        if chosen_parent and source == "sharepoint":
            chosen_drive, _, chosen_item = chosen_parent.partition("/")
            drive_id, parent_folder_id = chosen_drive, chosen_item
            location = f"graph:{drive_id}"
        else:
            parent_folder_id = chosen_parent if source == "drive" else None
        root = core.store.get_release_root(release_id, location, owner)
        if not root and source != "drive":
            claimed_name = core.store.claim_release_root_name(
                release_id, owner, source, location, release["folder_name"])
            folder_options = {"parent_id": parent_folder_id} if parent_folder_id else {}
            detail = _publish.ensure_sharepoint_release_folder(
                token, drive_id, release_id, claimed_name, **folder_options)
            root = core.store.record_release_root(
                release_id, owner, source, location, detail["id"],
                detail["name"], detail.get("url"))
        folders, planned_name = (_publish.normalize_relative_path(source_path, filename)
                                 if source == "drive" else
                                 _publish.sharepoint_relative_path(source_path, source_name))
        planned_destination = (f"google:me:{release_id}:{'/'.join([*folders, planned_name])}"
                               if source == "drive" else
                               f"graph:{drive_id or 'me'}:{root['folder_id']}:{'/'.join([*folders, planned_name])}")

        def publish_copy():
            nonlocal root
            if source == "drive":
                if not root:
                    if reconcile_only:
                        detail = _publish.find_published_folder(drive_svc, release_id, parent_id=parent_folder_id)
                    else:
                        claimed_name = core.store.claim_release_root_name(
                            release_id, owner, source, location, release["folder_name"])
                        detail = _publish.ensure_published_folder(
                            drive_svc, release_id, folder_name=claimed_name,
                            return_details=True, parent_id=parent_folder_id)
                    root = core.store.record_release_root(
                        release_id, owner, source, location, detail["id"],
                        detail["name"], detail.get("url"))
                return _publish.archive_copy_publish(
                    drive_svc, root["folder_id"], owner, scan_id, filename,
                    relative_path=source_path, source_id=source_id,
                    expected_digest=content_digest, return_details=True,
                    target_file_id=target_file_id, reconcile_only=reconcile_only)
            return _publish.archive_copy_publish_sharepoint(
                token, drive_id, root["folder_id"], owner, release_id, scan_id,
                filename, source_path, source_id, source_filename=source_name,
                expected_digest=content_digest)
        # The provider write is a canonical side effect, not merely a URL on file_records. The
        # deterministic receipt survives retries and is what a sealed Release manifest cites.
        receipt_writer = getattr(core.store, "record_side_effect_receipt", None)
        reserve = getattr(core.store, "reserve_side_effect", None)
        execution_id = (job or {}).get("batch_id")
        work_item = core.store.stage_work_item_for_job((job or {}).get("id")) \
            if execution_id else None
        reservation = None
        if callable(reserve) and execution_id:
            reservation = reserve(
                execution_id=execution_id, work_item_id=(work_item or {}).get("work_item_id"),
                effect_type=f"{source}.publish", destination=planned_destination,
                content_digest=content_digest,
                worker_id=(job or {}).get("locked_by") or (job or {}).get("id") or "release-worker")
            if reservation.get("reused") and reservation.get("status") == "completed":
                saved_receipt = dict(reservation.get("receipt") or {})
                publication = {"id": saved_receipt.get("provider_id"),
                               "url": saved_receipt.get("url"),
                               "created": bool(saved_receipt.get("created")),
                               "checksum": saved_receipt.get("checksum") or content_digest,
                               "filename": saved_receipt.get("filename") or planned_name,
                               "verified": bool(saved_receipt.get("verified", True))}
            elif not reservation.get("acquired"):
                raise reservation_retry(f"{provider} publication is owned by another worker attempt; waiting for its lease.")
            else:
                # A reclaimed reservation may represent a predecessor that wrote and died before
                # finalizing. The publisher verifies matching destination bytes before deciding
                # whether a write is needed, so takeover never blindly repeats the side effect.
                if source == "drive":
                    target_file_id = (reservation.get("receipt") or {}).get("planned_provider_id")
                    if not target_file_id and reservation.get("reclaimed"):
                        # Historical writes lack a preallocated ID. An empty marker search is
                        # not proof of absence, so legacy recovery may only reuse verified bytes.
                        reconcile_only = True
                    elif not target_file_id:
                        allocated = drive_svc.files().generateIds(count=1, space="drive", type="files").execute()
                        candidate = (allocated.get("ids") or [None])[0]
                        if not candidate:
                            raise IOError("Google Drive did not allocate a delivery identifier")
                        target_file_id = core.store.prepare_side_effect_provider_id(
                            reservation["effect_id"], reservation["reservation_token"], candidate)
                publication = publish_copy()
        else:
            if source == "drive":
                raise FatalJobError("Google Drive delivery requires a durable work-item reservation")
            publication = publish_copy()
        if publication is None:
            raise IOError("corrected content was unavailable")
        released_name = publication.get("filename") or planned_name
        if reservation and reservation.get("acquired"):
            lineage_reader = getattr(core.store, "release_finding_lineage", None)
            finding_lineage = (lineage_reader(execution_id, filename)
                               if callable(lineage_reader) else None)
            provider_receipt = {
                "corrected_copy_assessment": candidate_assessment,
                **({"release_review": payload.get("release_review") or release_review_evidence(record, owner=owner, allow_remaining_issues=True, store=core.store, scan_id=scan_id)} if allow_remaining_issues else {}),
                "provider_id": publication.get("id"), "url": publication.get("url"),
                "created": bool(publication.get("created")),
                "checksum": publication.get("checksum") or content_digest,
                "filename": released_name, "verified": bool(publication.get("verified", True)),
            }
            if finding_lineage is not None:
                # Freeze the exact Remediation findings this provider revision releases.  This
                # belongs in the immutable side-effect receipt, not in a later request-time
                # projection whose current disposition may have changed by the time it is read.
                provider_receipt["finding_lineage"] = finding_lineage
            core.store.finalize_side_effect(
                reservation["effect_id"], reservation["reservation_token"], provider_receipt)
        elif callable(receipt_writer) and execution_id:
            lineage_reader = getattr(core.store, "release_finding_lineage", None)
            finding_lineage = (lineage_reader(execution_id, filename)
                               if callable(lineage_reader) else None)
            provider_receipt = {
                "corrected_copy_assessment": candidate_assessment,
                **({"release_review": payload.get("release_review") or release_review_evidence(record, owner=owner, allow_remaining_issues=True, store=core.store, scan_id=scan_id)} if allow_remaining_issues else {}),
                "provider_id": publication.get("id"), "url": publication.get("url"),
                "created": bool(publication.get("created")),
                "checksum": publication.get("checksum") or content_digest,
                "filename": released_name, "verified": bool(publication.get("verified", True)),
            }
            if finding_lineage is not None:
                provider_receipt["finding_lineage"] = finding_lineage
            receipt_writer(
                execution_id=execution_id, work_item_id=(work_item or {}).get("work_item_id"),
                effect_type=f"{source}.publish", destination=planned_destination,
                content_digest=content_digest,
                receipt=provider_receipt)
        published_at = core.store.record_publish(
            scan_id, filename, published_url=publication.get("url"))
        core.store.record_release_document(release_id, owner, {
            "file": filename, "source_document_id": source_id,
            "original_relative_path": source_path,
            "released_relative_path": "/".join([*folders, released_name]),
            "status": "published", "published_at": published_at,
            "published_url": publication.get("url"),
            "verification": "content verified",
            "released_document_id": publication.get("id"),
            "corrected_checksum": publication.get("checksum"),
            "artifact_digest": artifact_tag(content_digest),
            "created": publication.get("created", False),
        })
    except ReleaseArtifactError as exc:
        # A stale job must never replace a newer correction's confirmed delivery with failure.
        if reuse_state(saved, content_digest) != "reuse":
            _release_failure(release_id, owner, filename, record, exc.category, str(exc))
        raise FatalJobError(str(exc)) from exc
    except _scanner.SharePointSessionExpired:
        if int((job or {}).get("attempts") or 1) < int((job or {}).get("max_attempts") or 5):
            raise
        _release_failure(release_id, owner, filename, record,
                         "provider_session_expired",
                         f"Reconnect {provider} and retry this document.")
        require_reconnect()
        raise FatalJobError(f"{provider} session expired — reconnect and retry")
    except PermissionError as exc:
        log_provider_failure(exc)
        require_reconnect()
        _release_failure(release_id, owner, filename, record,
                         "provider_permission_denied",
                         ("Google Drive refused the write. Reconnect with write access to the destination."
                          if source == "drive" else
                          "SharePoint refused the write. Reconnect after an administrator grants Files.ReadWrite.All and Sites.ReadWrite.All."))
        raise FatalJobError(f"{provider} write permission denied — reconnect with write access")
    except (FatalJobError, ReservationRetryError):
        raise
    except Exception as exc:
        if source in {"drive", "sharepoint"}:
            diagnostic = log_provider_failure(exc)
            status = diagnostic.get("http_status")
            if source == "sharepoint" and status == 401 and int((job or {}).get("attempts") or 1) < int((job or {}).get("max_attempts") or 5):
                # Allow the running page a bounded window to silently refresh access.
                raise
            if status in {401, 403}:
                require_reconnect()
                category = "provider_session_expired" if status == 401 else "provider_permission_denied"
                _release_failure(release_id, owner, filename, record, category,
                                 f"Reconnect {provider} with write access and retry this document.")
                raise FatalJobError(f"{provider} access needs reconnecting") from exc
        # Let transient Graph/Redis failures use the queue's normal retry/backoff. On the final
        # attempt, settle the document into an actionable durable state instead of leaving it
        # looking queued forever after the job dead-letters.
        if int((job or {}).get("attempts") or 1) < int((job or {}).get("max_attempts") or 5):
            if reservation:
                raise reservation_retry(f"{provider} delivery is unconfirmed; waiting to verify the reserved copy.") from exc
            raise
        _release_failure(release_id, owner, filename, record,
                         "provider_write_failed",
                         f"The corrected copy could not be verified at the {provider} release destination. Retry this document.")
        raise


def _remediation_source_bytes(scan_id: str, filename: str, payload: dict,
                              drive_file_id: str | None = None):
    """Original bytes for one remediation job, chosen by the job's OWN source. Returns
    (data, drive_service) — the service is None for every source but Drive, which needs the
    same client again for the mirror write.

    LOCAL AND SHAREPOINT READ THE CACHE, DRIVE DOWNLOADS. Assess already fetched every file and
    stashed the original bytes (ADR 0020, scanner.cache_source_bytes) whatever the source was,
    so remediation of a SharePoint document needs no Graph call and — the part that broke — no
    Drive call either.

    Live 2026-09-04, scan 8b83e9e1ca5c: this function's predecessor special-cased `local` and
    let everything else fall through to the Drive client, so all 147 SharePoint jobs asked for a
    Drive token they were never given and died with "no Drive token for this scan
    (expired/restarted)". Nothing about that message named SharePoint, and the batch was
    resubmitted (twice, on two worker revisions) on the strength of the "re-trigger" it asks for.
    An unsupported source now fails by NAME instead of borrowing Drive's identity.
    """
    from remediation_contribution import SOURCE
    SOURCE.set(None)
    source = payload.get("source") or "drive"
    if source in ("local", "sharepoint"):
        from scanner import read_cached_source
        owner = payload.get("owner")
        checksum = payload.get("checksum")
        if not checksum:
            # BOTH KEY SHAPES ARE LIVE, and a job that carries no checksum can still have its
            # bytes under the checksum key — every job enqueued before the route learned to
            # stamp one does, and jobs are durable, so those are still in the queue. Resolve it
            # from scan_inventory (store.get_source_checksum) rather than treating a payload
            # without one as proof the cache used the scan-keyed shape.
            try:
                checksum = core.store.get_source_checksum(scan_id, filename)
            except Exception:
                swallowed("_remediation_source_bytes: resolving the source checksum failed", scan_id)
        data = read_cached_source(scan_id, filename, owner, checksum=checksum)
        if data is None and checksum:
            # The checksum key only holds bytes when the LISTING carried that checksum. A file
            # whose recorded checksum was computed after the download (or has changed since)
            # misses it and is still in the cache under this scan's own scan_id/filename key.
            # Reading only the first key is a cache miss that looks like "never cached".
            data = read_cached_source(scan_id, filename, owner)
        if data is not None:
            from remediation_contribution import bind_assessed_input
            bind_assessed_input(scan_id, filename, data, data)
            return data, None
        if source == "local":
            # Local corpus remains the deterministic development/demo fallback when Blob caching
            # is disabled. Resolve beneath the configured corpus and never accept a path from the
            # job payload.
            import scanner as _scanner
            corpus = _Path(_os.environ.get("ACP_LOCAL_CORPUS") or
                           (_scanner.ACP / "test-corpus/files")).resolve()
            candidate = (corpus / filename).resolve()
            if corpus not in candidate.parents or not candidate.is_file():
                raise FatalJobError("local source bytes are unavailable — re-run Assess")
            return candidate.read_bytes(), None
        # No corpus fallback for SharePoint, and deliberately no Graph download: the remediation
        # worker holds no SharePoint token, and re-running Assess is what repopulates the cache.
        raise FatalJobError("no cached SharePoint source bytes for this scan — re-run Assess")
    if source == "drive":
        # Prefer the token carried in the durable job payload: the in-memory scan-token
        # store is per-replica and is wiped by a restart/redeploy, so a durable remediate
        # job that later runs on another replica (or after a restart) would otherwise fail
        # with "no Drive token". The payload token survives both; fall back to in-memory.
        token = payload.get("drive_token") or core.get_scan_tokens(scan_id).get("drive")
        if not token:
            raise FatalJobError("no Drive token for this scan (expired/restarted) — re-trigger")
        svc = _drive_client(token)
        file_id = drive_file_id or payload.get("drive_file_id")
        data = svc.files().get_media(fileId=file_id).execute()
        # Drive may have changed since Assess. Remediation can continue, but a live
        # download never manufactures the assessment-to-proposal source binding.
        from remediation_contribution import bind_assessed_input
        assessed = None
        try:
            from scanner import read_cached_source
            checksum = core.store.get_source_checksum(scan_id, filename)
            assessed = read_cached_source(scan_id, filename, payload.get("owner"), checksum=checksum)
            if assessed is None and checksum:
                assessed = read_cached_source(scan_id, filename, payload.get("owner"))
        except Exception:
            swallowed("_remediation_source_bytes: assessed source proof unavailable", scan_id)
        bind_assessed_input(scan_id, filename, data, assessed)
        return data, svc
    raise FatalJobError(f"unsupported remediation source {source!r} — expected one of "
                        f"{', '.join(REMEDIATION_SOURCES)}")


def _rem_event(scan_id: str, kind: str, job: dict | None, file: str | None, **detail) -> None:
    """One remediation lifecycle event, carrying the job identity the panel correlates on.

    A thin wrapper over `scan_event` rather than a second mechanism: it exists only so every
    remediation emit site carries the same identity fields (job id, attempt, filename) without
    thirteen call sites each remembering to. `scan_event` itself never raises — a narration line
    must never be able to fail the work it narrates — so this cannot either.

    `file` goes in the `document` COLUMN — it used to ride inside the JSON detail, on the
    reasoning that adding a column would migrate a table five other kinds share. Two requirements
    overturned that, and both are reads the JSON could not serve:

      * per-document replay (`list_scan_events(document=...)`) needs an index, and
      * PRD §22's filename suppression needs the name reachable from exactly ONE place, so that
        withholding it is a property of a projection rather than a search through a blob.

    It is NOT written to `detail` as well. One fact, one home: two copies is two places for a
    suppression rule to be applied to only one of them.

    `correlation_id` is the batch this event belongs to, taken from the job row. It is what
    separates two remediation runs over the same scan — `scan_id` alone cannot, and the panel is
    scoped to the latest batch.

    FILENAMES ARE IN HERE. That is deliberate and it is why the read path is owner-scoped —
    `list_scan_events(owner=...)` plus the route's own `get_scan(owner=...)` gate, exactly as PRD
    §13 requires. Nothing here carries extracted document CONTENT, only its name and the counts.
    """
    payload = (job or {}).get("payload")
    if isinstance(payload, str):
        try:
            import json as _json
            payload = _json.loads(payload)
        except (TypeError, ValueError):
            payload = None
    correlation = ((job or {}).get("batch_id")
                   or (payload or {}).get("stage_execution_id"))
    scan_event(scan_id, kind, job_id=(job or {}).get("id"),
               attempt=(job or {}).get("attempts"),
               document=file or None, correlation_id=correlation or None,
               detail=detail or None)


@handler("remediate_file")
def _remediate_file(payload: dict, job: dict) -> None:
    from ai_run_policy import run_context
    from remediation_contribution import SOURCE
    from remediation_run_insights import proposal_context
    from vision_recovery import capture, schedule
    with run_context(core.store, payload, job) as context, proposal_context(core.store, payload, job), capture() as vision_misses:
        source_token = SOURCE.set(None)
        try:
            result = _remediate_file_with_policy(payload, job)
            if context is not None:
                try:
                    from document_wide_workflow import process_file
                    process_file(core.store, context)
                except Exception as exc:
                    core.store.log_decision('system', 'document_wide.deferred',
                        scan_id=context.scan_id, file=context.file,
                        detail=__import__('json').dumps({'owner_id': context.owner_id, 'run_id': context.run_id,
                            'reason': f'Document-wide suggestions did not complete: {type(exc).__name__}'}))
                # Inspect after document-wide generation too: no captured transport
                # miss does not establish that a pending caption has a usable draft.
                schedule(core.store, context, job, vision_misses, inspect_pending=True)
            if context is not None:
                try:
                    from ai_standing_approval import approve_file
                    approve_file(core.store, context)
                except Exception as exc:
                    core.store.log_decision('system', 'ai.standing_approval.deferred',
                        scan_id=context.scan_id, file=context.file,
                        detail=f'Automatic approval did not complete; suggestions remain reviewable: {type(exc).__name__}')
            return result
        finally:
            SOURCE.reset(source_token)
            if context is not None:
                for reason in sorted(set(str(item) for item in context.deferred)):
                    core.store.log_decision("system", "remediate.ai_deferred",
                        scan_id=context.scan_id, file=payload.get("file"), detail=reason)


@handler("vision_proposal_retry")
def _vision_proposal_retry(payload: dict, job: dict) -> None:
    from vision_recovery import process
    process(core.store, payload)


def _remediate_file_with_policy(payload: dict, job: dict) -> None:
    """Apply server-side remediation to one file and write the fixed copy to Drive.

    payload: {scan_id, file, drive_file_id}
    HTML files are remediated deterministically (ADR 0005); other types are routed
    to human review (no in-repo Office/PDF remediator yet)."""
    scan_id = payload.get("scan_id") or job.get("scan_id")
    filename = payload.get("file")
    drive_file_id = payload.get("drive_file_id")
    source = payload.get("source") or "drive"
    if not (scan_id and filename) or (source == "drive" and not drive_file_id):
        raise FatalJobError("remediate_file job missing scan_id/file/source identity")
    # Fail an unknown source HERE, before the job spends an activity row, a rule lookup and a
    # format branch on work whose bytes can never be read. The check is cheap and it is the one
    # that names the source: the old code had no such check at all, and an unrecognised source
    # simply fell into the Drive branch and reported a missing Drive token.
    if source not in REMEDIATION_SOURCES:
        raise FatalJobError(f"unsupported remediation source {source!r} — expected one of "
                            f"{', '.join(REMEDIATION_SOURCES)}")

    # Never remediate ACP's own remediated copy. POST /scans/{sid}/remediate already can't
    # enqueue one — it iterates get_scan's filtered file list — but jobs are DURABLE: a job
    # queued before that filter existed, or retried from the dead-letter, still arrives here.
    # This guard sits before the download, so a phantom costs nothing: no Drive fetch, no
    # llava call, no HITL row asking a human to describe an image ACP itself produced.
    if core.store.is_shadowed_output(scan_id, filename):
        core.store.log_decision("system", "remediate.skipped", scan_id=scan_id, file=filename,
                                detail="ACP-generated copy shadowing its source — not a document")
        return

    from remediation_impact_execution import execution_controls
    try:
        impact_controls = (execution_controls(payload, core.store.get_ai_enabled())
                           if "remediation_impact_policy" in payload else None)
    except ValueError as exc:
        raise FatalJobError(str(exc)) from exc

    _OFFICE_MIME = {
        "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    }
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if f".{ext}" not in remediable_extensions():
        # No server-side remediator for this type → human review.
        core.store.log_decision("system", "remediate.deferred", scan_id=scan_id,
                                file=filename, detail=f"no server-side remediator for .{ext}")
        return

    # MEDIA STOPS HERE, with a caption draft and nothing else. Everything below this branch
    # downloads the file, runs a format remediator that REWRITES bytes, and uploads a fixed copy.
    # None of that applies to a video: ACP does not re-encode a customer's media, so there is no
    # fixed copy to make and no Drive round-trip to justify. What a reviewer needs is the draft.
    #
    # Returning early rather than threading media through the rest of the function is what keeps
    # this slice honest — the alternative is a series of `if ext not in _AV` guards down a
    # 200-line body, each one an opportunity to send a .mp4 into an OOXML path.
    if f".{ext}" in _AV_REMEDIABLE:
        if impact_controls is not None and not impact_controls["draft_ai"]:
            core.store.log_decision("system", "remediate.deferred", scan_id=scan_id,
                                    file=filename, detail="AI drafting disabled by run policy")
            return
        _propose_media_captions(scan_id, filename, drive_file_id, payload)
        return

    import activity as _activity
    # This evidence controls which mutations may run, so failure to read it must retry the job;
    # treating a database error as an empty list would complete successfully after doing no work.
    # Media returns above: caption proposals are not automatic document mutations and must not
    # depend on this format-fixer query (or on test doubles implementing it).
    _eligible_rules = core.store.list_auto_fail_rules(scan_id, filename)
    if impact_controls is not None:
        _eligible_rules = [rule for rule in _eligible_rules
                           if rule in impact_controls["allowed_rules"]]
    _rule_detail = ("WCAG " + ", ".join(sorted(_eligible_rules))
                    if _eligible_rules else "checking eligible WCAG criteria")
    _activity.record(scan_id, file=filename, action="starting automated remediation",
                     detail=_rule_detail, phase="remediating", force=True)

    _phase(job, f"downloading {filename}")
    _activity.record(scan_id, file=filename, action="opening source document",
                     detail=_rule_detail, phase="downloading", force=True)
    # One dispatch on the job's own source, for every source (see _remediation_source_bytes).
    # `svc` stays None unless this really is a Drive job, so the mirror block below cannot
    # reach a client a non-Drive job never built.
    data, svc = _remediation_source_bytes(scan_id, filename, payload, drive_file_id)

    # Format-agnostic text proposers (3.1.2 language-of-parts + 1.3.3 sensory rewrite) run on
    # the original bytes — the prose these check is unchanged by remediation, and running
    # here (not after the format branch) means they still surface even when a file has no
    # deterministic fixes and would hit the no-fixes early return below. Both self-gate.
    _draft_ai = (impact_controls["draft_ai"] if impact_controls is not None
                 else core.store.get_ai_enabled())
    _propose_text_findings(scan_id, filename, data, _draft_ai)

    # docx form-field label proposals (3.3.2) — prefills the unlabeled-content-control
    # deferral with one-click labels derived from each field's adjacent prompt text (or the
    # local model where none). Self-gating like the text proposers; runs on the original bytes.
    if ext == "docx":
        _propose_form_fields(scan_id, filename, data, _draft_ai)

    from ai_run_policy import optional_current_run_context
    from document_wide_workflow import enabled as document_wide_enabled
    _document_mode = document_wide_enabled(optional_current_run_context(), filename)
    _context = optional_current_run_context()
    _pdf_figure_mode = (ext == "pdf" and _draft_ai and _context is not None
                        and (_context.enabled or _context.local_drafting)
                        and core.store._selected_sc(scan_id, filename, "1.1.1")
                        and any(row.get("rule_id") == "1.1.1" and row.get("outcome") == "FAIL"
                                for row in core.store.get_scan_traces(scan_id, file=filename)))
    if impact_controls is not None and not _eligible_rules and not _document_mode and not _pdf_figure_mode:
        review_rules = [{"rule_id": row["rule_id"], "rule_name": row.get("rule_name"),
                         "finding_count": row.get("finding_count")}
                        for row in core.store.get_scan_traces(scan_id, file=filename)
                        if row.get("outcome") == "FAIL"]
        if review_rules:
            core.store.queue_hitl_review_for_file(scan_id, filename, review_rules)
        core.store.log_decision("system", "remediate.deferred", scan_id=scan_id,
                                file=filename,
                                detail="Run policy requires review; no automatic mutations permitted")
        return

    # New-policy jobs generate AI only through proposal-only paths above. Format remediators
    # contain inline AI writes, so draft-for-review must never enable their AI argument.
    _format_ai = False if impact_controls is not None else core.store.get_ai_enabled()

    # Per-fix before→after evidence for the certification report's "Before → After"
    # section. Each remediator appends {rule_id (SC), before, after, note}; we persist
    # only the ones that verifiably cleared on the post-fix re-scan (below).
    rem_diffs: list[dict] = []

    # AI-proposed one-click values applied/drafted inline during remediation (e.g. 2.4.4
    # link text). Collected here so they can be enqueued AFTER the residual re-scan below,
    # with an honest `validated` flag (True only when the applied fix actually cleared).
    inline_proposals: list[dict] = []
    # Normalized remediation tallies for the Langfuse Remediate span (G4). Every fixer returns a
    # list of applied-fix messages and a list of skipped/deferred ones; the HTML path names the
    # latter `_deferred` and the office/pdf paths `_skipped`, so collapse both onto one name here.
    # COUNTS only reach the trace — the messages are prose and stay out (see lf.remediate_span).
    rem_skipped: list = []

    _phase(job, f"applying fixes to {filename}")
    _activity.record(scan_id, file=filename, action="applying eligible WCAG fixes",
                     detail=_rule_detail, phase="remediating", force=True)
    # Scope alone is deliberately too broad: it describes what may be assessed, not what this
    # document failed. Restrict expensive format-wide mutations to the auto-fixable FAIL rows
    # already produced by Assessment.
    _scope_allows = _remediation_fix_scope(filename, scan_id, _eligible_rules)
    # The gap #137 recorded here as `remediate.scope_partial` is CLOSED. The office/pdf
    # deterministic fixers now take the same `in_scope` predicate the HTML fixer does, gated at
    # each individual fix by the SC it actually writes (remediate_office._sc_ok /
    # remediate_pdf._sc_ok) rather than at the format boundary — because several of those
    # functions write four different criteria in one pass, so a per-function gate would have been
    # the same "partial gate that looks total" in a new place.
    #
    # The `scope_partial` decision is deliberately NOT emitted any more: leaving it would tell an
    # operator their scope is being half-honoured when it is now honoured in full, which is a
    # worse lie than the one it was introduced to prevent. tests/test_remediation_scope_office_pdf.py
    # pins the closure per format, including an empty-scope case that catches an ungated fix
    # generically rather than relying on this list staying complete.
    if ext in ("html", "htm"):
        import joblog as _joblog
        with _joblog.stage("remediate.html", doc=_joblog.doc_id(filename),
                           scan_id=scan_id, eligible_rules=len(_eligible_rules)):
            fixed_html, applied, _deferred = remediate_html(
                data.decode("utf-8", errors="replace"),
                ai_enabled=_format_ai, diffs=rem_diffs,
                proposals=inline_proposals, in_scope=_scope_allows, filename=filename)
        rem_skipped = _deferred
        fixed_bytes = fixed_html.encode("utf-8")
        mimetype = "text/html"
    else:  # pdf / office — file-based deterministic remediators (ADR 0005 step 4)
        import tempfile
        from pathlib import Path as _Path
        with tempfile.TemporaryDirectory(prefix="acp-rem-") as _d:
            src = _Path(_d) / filename
            src.write_bytes(data)
            if ext == "pdf":
                from remediate_pdf import remediate_pdf
                _pdf_proposals: list = []
                _applied_fixes: list = []
                import joblog as _joblog
                with _joblog.stage("remediate.pdf", doc=_joblog.doc_id(filename),
                                   scan_id=scan_id, eligible_rules=len(_eligible_rules)):
                    out_path, applied, _skipped = remediate_pdf(
                        src, ai_enabled=_format_ai, scan_id=scan_id,
                        diffs=rem_diffs, proposals=_pdf_proposals,
                        applied_fixes=_applied_fixes, in_scope=_scope_allows)
                rem_skipped = _skipped
                mimetype = "application/pdf"
                # A PDF's AI-written alt text is evidence exactly like an Office document's.
                # It used to be dropped: remediate_pdf returned only prose, so no row reached
                # applied_fixes and the certification record showed the fix had never happened.
                _record_applied_fixes(scan_id, filename, _applied_fixes)
                _rem_event(scan_id, "remediate.fix_applied", job, filename,
                           fixes=len(_applied_fixes))
                # Untagged-PDF proposals, split by kind — 1.3.2 reading order (vision) and
                # 1.3.1 structure map (deterministic font rank). Surfaced for one-click
                # confirm, never auto-applied. Before the no-fixes early return.
                _enqueue_proposals(scan_id, filename, "1.3.2", "Meaningful Sequence",
                                   [p for p in _pdf_proposals if p.get("kind") == "reading-order"])
                _enqueue_proposals(scan_id, filename, "1.3.1", "Info and Relationships",
                                   [p for p in _pdf_proposals if p.get("kind") == "structure-map"])
                # 2.4.6 heading map (tagged PDF, no headings) — a deterministic proposal for
                # one-click confirm, never auto-applied.
                _enqueue_proposals(scan_id, filename, "2.4.6", "Headings and Labels",
                                   [p for p in _pdf_proposals if p.get("kind") == "headings-map"])
                # 2.4.4 link purpose is deliberately NOT enqueued for PDF. There is no PDF
                # write-back for link text (apply_pdf_approved routes pdf:fig:/pdf:field: only),
                # so the card could be approved but never honoured — and an approved value
                # nothing writes also blocks the file from ever certifying. The finding still
                # reaches a reviewer as a plain 2.4.4 judgement row further down. See the
                # explain-only note in remediate_pdf.py.
                # 1.1.1 per-figure alt + 4.1.2 per-field accessible name are the mirror case:
                # both carry a `pdf:fig:`/`pdf:field:` locator that _apply_approved_values DOES
                # write back through remediate_pdf.apply_pdf_approved, so they are enqueued.
                # Without these two lines the cards were built by remediate_pdf and then dropped
                # here — the reviewer never saw them, so the deferral existed only as a tally.
                if not _pdf_figure_mode:
                    _enqueue_proposals(scan_id, filename, "1.1.1", "Non-text Content",
                                       [p for p in _pdf_proposals if p.get("kind") == "pdf-figure-alt"])
                _enqueue_proposals(scan_id, filename, "4.1.2", "Name, Role, Value",
                                   [p for p in _pdf_proposals if p.get("kind") == "pdf-field-name"])
                _enqueue_proposals(scan_id, filename, "2.4.6", "Headings and Labels",
                                   [p for p in _pdf_proposals if p.get("kind") == "pdf-tag-heading"])
                _enqueue_proposals(scan_id, filename, "1.3.1", "Info and Relationships",
                                   [p for p in _pdf_proposals if p.get("kind") == "pdf-table-header-scope"])
            else:  # docx / pptx / xlsx
                from remediate_office import remediate_office
                _applied_fixes: list = []
                _proposals: list = []
                _evidence: list = []
                import joblog as _joblog
                with _joblog.stage("remediate.office", doc=_joblog.doc_id(filename),
                                   scan_id=scan_id, eligible_rules=len(_eligible_rules), ext=ext):
                    out_path, applied, _skipped = remediate_office(
                        src, ai_enabled=_format_ai, scan_id=scan_id,
                        applied_fixes=_applied_fixes, proposals=_proposals,
                        evidence=_evidence, diffs=rem_diffs, in_scope=_scope_allows)
                rem_skipped = _skipped
                mimetype = _OFFICE_MIME[ext]
                _record_applied_fixes(scan_id, filename, _applied_fixes)
                _rem_event(scan_id, "remediate.fix_applied", job, filename,
                           fixes=len(_applied_fixes))
                # AI-proposed (but not auto-applied) alt: an ungrounded vision guess is
                # surfaced for one-click approval rather than silently written (WCAG 1.1.1
                # intent stays human). Attach the prefilled drafts to the file's 1.1.1 HITL
                # row — before the no-fixes early return, or they die inside the job result.
                # Route each proposal to ITS criterion. remediate_office used to return only
                # vision alt, so hard-coding 1.1.1 here was correct; it now also drafts 2.4.4,
                # 1.3.3 and 3.1.2 (see _draft_docx_assisted), and a link-text draft filed under
                # 1.1.1 would ask a reviewer to approve alt text that is not alt text — and
                # would clear the wrong finding when they did.
                #
                # Untagged proposals default to 1.1.1: every proposer that predates the `sc`
                # field emits vision alt, so the default preserves their behaviour exactly
                # rather than silently dropping them into a bucket nobody reads.
                _PROP_RULE_NAMES = {
                    "1.1.1": "Non-text Content", "2.4.4": "Link Purpose (In Context)",
                    "1.3.3": "Sensory Characteristics", "3.1.2": "Language of Parts",
                }
                _by_sc: dict[str, list] = {}
                for _p in _proposals:
                    _by_sc.setdefault((_p or {}).get("sc") or "1.1.1", []).append(_p)
                for _sc, _group in _by_sc.items():
                    _enqueue_proposals(scan_id, filename, _sc,
                                       _PROP_RULE_NAMES.get(_sc, _sc), _group)
                # Deferred alt text (no faithful source — see remediate_office) must
                # reach a human: those findings are fix_mode 'auto', so the ai-assisted
                # HITL pull never sees them. Queue here — before the no-fixes early
                # return below — or the deferral dies inside the job result.
                for _msg in _skipped:
                    if "faithful alt source" in _msg:
                        try:
                            _n = int(_msg.split(" ", 1)[0])
                        except ValueError:
                            _n = 1
                        try:
                            # Merges into this file's 1.1.1 row when one already exists (the
                            # proposals row queued just above). rule_name so a row created here
                            # is headed "Non-text Content", not the raw deferral note.
                            _queued_item = core.store.queue_hitl_deferral(
                                scan_id, filename, _msg, _n, rule_name="Non-text Content")
                            # None means it MERGED into an existing 1.1.1 row rather than creating
                            # one (queue_hitl_deferral is idempotent per scan/file/criterion). Only
                            # a genuinely new item is a new thing for a person to do; emitting on
                            # the merge would narrate the same review request once per retry.
                            if _queued_item:
                                _rem_event(scan_id, "remediate.review_requested", job, filename,
                                           criterion="1.1.1", findings=_n)
                        except Exception:
                            swallowed("_remediate_file: queueing the 1.1.1 HITL deferral failed", scan_id)
                # Attach the deferred images to whichever 1.1.1 row now exists — the
                # deferral queued just above, or the proposals row. Last, because there is
                # nothing to attach to until one of them has been created.
                try:
                    core.store.attach_hitl_evidence(scan_id, filename, "1.1.1", _evidence)
                except Exception:
                    # evidence is a nicety; never fail a remediation job for a thumbnail
                    swallowed("_remediate_file: attaching 1.1.1 HITL evidence failed", scan_id)
            if not out_path or not _Path(out_path).exists():
                if not _document_mode and not _pdf_figure_mode:
                    core.store.log_decision("system", "remediate.deferred", scan_id=scan_id,
                                            file=filename, detail=f".{ext}: no deterministic fixes applied")
                    return
                # Document AI requires a durable working copy even when deterministic
                # remediation had nothing to change. This records zero applied fixes.
                fixed_bytes = data
            else:
                fixed_bytes = _Path(out_path).read_bytes()

    from output_provenance import stamp_output
    fixed_bytes = stamp_output(fixed_bytes, filename)

    # ADR 0010: Blob is now the PRIMARY, must-succeed write -- no per-user token needed
    # (managed identity), so this no longer hard-fails for orgs that only granted
    # read-only Drive access. Drive becomes a best-effort MIRROR below: failure there no
    # longer fails the whole remediation, since Blob already has the durable copy.
    import blob as _blob
    _phase(job, "storing the corrected copy")
    _activity.record(scan_id, file=filename, action="recording corrected copy",
                     detail="preserving the source document", phase="storing", force=True)
    owner = (core.store.get_scan(scan_id) or {}).get("run", {}).get("owner_email")
    blob_url = _blob.upload_remediated(owner, scan_id, filename, fixed_bytes, mimetype)

    web_url = None
    delivery_status = "saved_in_acp"
    delivery_reason = "source_delivery_unavailable"
    if source == "drive" and core.store.get_drive_mirror_enabled():
        delivery_status = "failed"
        delivery_reason = "provider_error"
        _phase(job, "writing the corrected copy to Drive")
        import io
        from googleapiclient.http import MediaIoBaseUpload
        from googleapiclient.errors import HttpError
        try:
            # Folder id is created once per batch in the endpoint and passed in, so
            # concurrent workers don't each create their own mirror folder.
            # Fall back to find-or-create for a standalone job.
            folder_id = payload.get("remediated_folder_id") or ensure_remediated_folder(svc)
            media = MediaIoBaseUpload(io.BytesIO(fixed_bytes), mimetype=mimetype, resumable=False)
            # Upsert: update an existing fixed copy rather than piling up duplicates on re-run.
            safe = filename.replace("\\", "\\\\").replace("'", "\\'")
            existing = svc.files().list(
                q=f"name='{safe}' and '{folder_id}' in parents and trashed=false",
                fields="files(id)", pageSize=1).execute().get("files", [])
            # Stamp ACP's own output so a later scan skips it by provenance rather than by
            # which folder it happens to live in (api/provenance.py).
            props = provenance.stamp(filename)
            # Ask Drive to echo `properties` back. A stamp that does not round-trip is invisible
            # to the next scan's provenance filter, which then re-ingests ACP's own output as if
            # it were a source document. That has been the observed state — every discovery logs
            # "0 skipped as ACP-generated output" — and nothing told us whether the write set the
            # property, or the read never saw it. Now the write says so, once, at the moment of
            # truth. Diagnostic only: a missing stamp never fails the mirror (the in-document
            # content stamp still catches the copy).
            if existing:
                _mode = "updated"
                result = svc.files().update(fileId=existing[0]["id"], media_body=media,
                                            body={"properties": props},
                                            fields="id,webViewLink,properties").execute()
            else:
                _mode = "created"
                result = svc.files().create(body={"name": filename, "parents": [folder_id],
                                                  "properties": props},
                                            media_body=media,
                                            fields="id,webViewLink,properties").execute()
            web_url = result.get("webViewLink", "")
            # ALWAYS one greppable line, stamped or not. Silence used to be ambiguous: this
            # branch only spoke up on failure, so "no stamp line in the logs" meant either the
            # stamp round-tripped, or the mirror never ran at all — and on the day it mattered
            # it was the second. A log that only reports failure cannot tell you a thing about
            # a system that is quiet.
            _stamped = provenance.is_acp_generated(result)
            print(f"[remediate] drive mirror: {filename} {_mode} id={result.get('id')} "
                  f"stamp={'persisted' if _stamped else 'MISSING'} "
                  f"properties={result.get('properties')!r}", flush=True)
            if not _stamped:
                # The audit trail records only the anomaly — one row per unstamped copy, not a
                # row per successful write. Diagnostic: a missing stamp never fails the mirror
                # (Blob holds the durable copy; the in-document content stamp still catches it).
                _detail = (f"Drive did not echo the ACP provenance stamp on {filename} "
                           f"(got properties={result.get('properties')!r}); the next scan will "
                           f"not skip this copy by provenance")
                core.store.log_decision("system", "remediate.stamp_not_persisted",
                                        scan_id=scan_id, file=filename, detail=_detail[:200])
        except HttpError as e:
            delivery_reason = "write_permission_required" if getattr(e, "resp", None) is not None and e.resp.status == 403 else "provider_error"
            # A 403 here means the user's Drive grant lacks write access (drive.file) --
            # no longer fatal now that Blob has the durable copy; log and move on.
            reason = ("Drive write denied (403) — the signed-in user hasn't granted write "
                     "access (drive.file)." if getattr(e, "resp", None) is not None and e.resp.status == 403
                     else f"Drive mirror failed: {type(e).__name__}: {e}")
            # To stdout as well as the decisions table: a mirror failure recorded only in the
            # database is invisible to anyone reading logs, which is where you look first.
            print(f"[remediate] drive mirror: {filename} FAILED — {reason}", flush=True)
            core.store.log_decision("system", "remediate.drive_mirror_failed", scan_id=scan_id,
                                    file=filename, detail=reason[:200])
        except Exception as e:
            print(f"[remediate] drive mirror: {filename} FAILED — "
                  f"{type(e).__name__}: {e}", flush=True)
            core.store.log_decision("system", "remediate.drive_mirror_failed", scan_id=scan_id,
                                    file=filename, detail=f"{type(e).__name__}: {e}"[:200])
    elif source == "drive":
        delivery_reason = "delivery_disabled"
        # The third silence: with the mirror switched off nothing was written and nothing was
        # said, so "no mirror line" could also mean "the operator turned it off". Say it.
        print(f"[remediate] drive mirror: {filename} skipped — disabled "
              f"(settings.drive_mirror_enabled=false)", flush=True)
    else:
        print(f"[remediate] source mirror: {filename} skipped — {source} scan; corrected copy "
              "stored in ACP", flush=True)

    # The digest of the bytes that were actually stored, recorded WITH the correction rather
    # than derived later. A delivery-only retry checks the stored object against this value, so
    # a correction saved without one can never be re-delivered — the gate answers
    # `artifact_provenance_unknown` rather than sending bytes whose provenance nobody can state.
    _digest = _hashlib.sha256(fixed_bytes).hexdigest()
    core.store.record_remediation(scan_id, filename, drive_write_url=web_url, blob_url=blob_url,
                                  corrected_sha256=_digest, corrected_bytes=len(fixed_bytes))
    if _pdf_figure_mode:
        # Managed runs draft against the immutable bytes actually stored. Inline
        # AI stays disabled; only independently validated exact raster drafts
        # can later qualify for standing approval and the separate verified writer.
        from remediate_pdf import alt_proposals_for_pdf
        _enqueue_proposals(scan_id, filename, "1.1.1", "Non-text Content",
            alt_proposals_for_pdf(fixed_bytes, scan_id=scan_id, context_file=filename))
    # `delivered` names the DESTINATION write, not the correction. A corrected copy that
    # reached blob but not the provider is stored-not-delivered — PRD §11's delivery-failure
    # class — and the snapshot counts it as pending. Saying `delivered` for it would make a
    # lost corrected copy invisible, which is the whole reason the two are counted apart.
    _rem_event(scan_id, "remediate.delivered" if web_url else "remediate.delivery_failed",
               job, filename, destination="provider" if web_url else "acp_only",
               delivery_status="delivered" if web_url else delivery_status,
               reason=None if web_url else delivery_reason)
    # G4: the Remediate span now carries what the fix pass DID — how many fixes applied vs
    # skipped/deferred — not just where the copy was written. `applied` and `rem_skipped` are
    # lists of prose messages; only their counts reach the trace (lf.remediate_span is PHI-safe).
    core.emit_remediation_span(scan_id, filename, drive_write_url=web_url,
                               fixes_applied=len(applied), fixes_skipped=len(rem_skipped))
    core.store.log_decision("system", "remediate.applied", scan_id=scan_id, file=filename,
                            detail="; ".join(applied) or "no auto fixes needed")
    # ADR 0003 Phase 2: mark this file's deterministically-auto-fixable violations
    # complete -- but VERIFY first. Some criteria (docx/pdf language & title) report
    # 'applied' yet do not clear on re-scan (metadata is written, but the engine reads
    # a field it does not touch). Re-scan the fixed bytes and only credit criteria that
    # ACTUALLY cleared; the rest stay failing for review, so the app never shows a fix
    # that did not take.
    _phase(job, "re-verifying the corrected copy")
    _activity.record(scan_id, file=filename, action="re-checking corrected document",
                     detail=_rule_detail, phase="verifying", force=True)
    import joblog as _joblog
    with _joblog.stage("remediate.verify", doc=_joblog.doc_id(filename),
                       scan_id=scan_id, ext=ext):
        verification = _verify_residual(fixed_bytes, filename, scan_id=scan_id)
    try:
        from native_chart_review_settlement import settle as settle_native_chart_reviews
        settle_native_chart_reviews(core.store, job, scan_id, filename, data, fixed_bytes, verification)
    except Exception:
        swallowed("_remediate_file: settling exact verified native-chart reviews failed", scan_id)
    # Enqueue the inline AI proposals (2.4.4 link text …) now that the re-scan has run, so a
    # deterministic fix that verifiably cleared carries validated=True (confidence.js reads
    # it as a High, one-click confirm) while a fix still failing / a model draft stays
    # unvalidated (Medium). Group per SC — one HITL row per (file, sc).
    if inline_proposals:
        _by_sc: dict[str, list] = {}
        for _p in inline_proposals:
            _by_sc.setdefault(_p.get("sc", ""), []).append(_p)
        _PROPOSAL_RULE_NAMES = {"2.4.4": "Link Purpose (In Context)",
                                "2.4.6": "Headings and Labels", "1.3.1": "Info and Relationships"}
        for _sc, _ps in _by_sc.items():
            if not _sc:
                continue
            _applied_any = any(p.get("applied") for p in _ps)
            _cleared = verification.cleared({_sc})
            _enqueue_proposals(scan_id, filename, _sc, _PROPOSAL_RULE_NAMES.get(_sc, _sc),
                               [{k: v for k, v in p.items() if k not in ("sc", "applied")} for p in _ps],
                               validated=bool(_applied_any and _cleared))
    # Truthfulness gate: keep a before→after record ONLY when the re-scan actually ran and
    # observed that criterion cleared. A fix that did not clear never reaches the PDF — and
    # neither does one nobody could verify. This used to read `residual is None or ...`, so a
    # re-scan that could not run published every before→after pair as though confirmed.
    try:
        verified_diffs = [d for d in rem_diffs
                          if verification.cleared({d.get("rule_id")})]
        core.store.record_remediation_diffs(scan_id, filename, verified_diffs)
        # rem_diffs is what the fixer produced; verified_diffs is what the re-scan confirmed
        # cleared. The difference is a verification FAILURE, and it is the number PRD §17.8
        # requires never to reach the delivery counter.
        _unverified = len(rem_diffs) - len(verified_diffs)
        from activity_event_evidence import record as record_activity_evidence
        if verified_diffs:
            _evidence = record_activity_evidence(core.store, scan_id, filename, job, fixed_bytes, verified_diffs, verified=True, verification_ok=verification.ok)
            _rem_event(scan_id, "remediate.verified", job, filename,
                       fixes=len(verified_diffs), **_evidence)
        from verification_event_detail import verification_event_detail
        _event_detail = verification_event_detail(verification, rem_diffs, fixed_bytes, filename)
        _manual_ids = {_row['criterion'] for _row in _event_detail['manual_criteria']}
        _automatic_unverified = [d for d in rem_diffs
                                 if not verification.cleared({d.get('rule_id')})
                                 and d.get('rule_id') not in _manual_ids]
        if _automatic_unverified:
            _evidence = record_activity_evidence(core.store, scan_id, filename, job, fixed_bytes, _automatic_unverified, verified=False, verification_ok=verification.ok)
            _rem_event(scan_id, "remediate.verification_failed", job, filename,
                       fixes=len(_automatic_unverified), failed_criteria=_event_detail['failed_criteria'], **_evidence)
        for _manual in _event_detail['manual_criteria']:
            _rem_event(scan_id, "remediate.review_requested", job, filename,
                       criterion=_manual['criterion'], reason_code=_manual['reason_code'])
    except Exception:
        swallowed("_remediate_file: recording the verified remediation diffs failed", scan_id)
    from unverified_changes import record_verification
    record_verification(core.store, scan_id, filename, fixed_bytes, verification)
    try:
        from documents import resolve_doc_id
        doc_id = resolve_doc_id(source, drive_file_id or f"{scan_id}:{filename}", filename, None)
        auto_rules = core.store.list_auto_fail_rules(scan_id, filename)
        cleared: set[str] = set()   # rule_ids this run verifiably auto-cleared
        kept = []
        for rule_id in auto_rules:
            if not verification.cleared({rule_id}):
                # Either the criterion is still failing, or verification could not run at all.
                # Both leave the rule failing and reviewable — "complete" is a claim about the
                # document that nothing here is entitled to make. Before the fail-closed fix
                # this branch was `residual is not None and rule_id in residual`, so a re-scan
                # that could not run marked EVERY auto-fixable rule complete.
                kept.append(rule_id)
                continue
            core.store.upsert_remediation_state(doc_id, rule_id, "complete", scan_id)
            cleared.add(rule_id)
        if kept:
            _why = ("still failing on re-scan" if verification.ok
                    else f"unverifiable ({verification.reason})")
            core.store.log_decision("system", "remediate.unverified", scan_id=scan_id, file=filename,
                                    detail=f"{len(cleared)} verified cleared; {len(kept)} reported-fixed but {_why} (kept for review): {', '.join(sorted(kept))}")
        # Tie the HITL review queue to the remediate action. Every FAILing finding this run
        # did NOT verifiably auto-clear — contrast sign-off, link purpose, structure, or an
        # auto fix that didn't take — must reach a human here, or it silently vanishes: the
        # mount-time queue_hitl_items pull only sees fix_mode='ai-assisted', so a stuck
        # fix_mode='auto' finding never routes to anyone, the reviewer has nothing to
        # approve, and the file can never re-validate to compliant (Publish stays empty).
        # A fully-cleared file still gets ONE verification item (user decision 2026-07-02)
        # so no unreviewed fix reaches Publish on trust alone.
        review_rules = residual_remediation_review_rules(core.store, scan_id, filename, cleared)
        if review_rules:
            queued = core.store.queue_hitl_review_for_file(scan_id, filename, review_rules)
            if queued:
                core.fire_webhook(queued)
                core.store.log_decision("system", "hitl.review_routed", scan_id=scan_id, file=filename,
                                        detail=f"{len(queued)} finding(s) routed to human review after remediation")
        else:
            core.store.queue_hitl_deferral(scan_id, filename,
                                           "Automatic fix applied — verify the result", 1,
                                           rule_id="auto/verify")
    except Exception:
        swallowed("_remediate_file: settling remediation state and routing findings to human review failed", scan_id)


def residual_remediation_review_rules(store, scan_id, filename, cleared):
    """Remaining failed or judgement findings need explicit review, never implicit success."""
    return [{"rule_id": row["rule_id"], "rule_name": row.get("rule_name"),
             "finding_count": row.get("finding_count")}
            for row in store.get_scan_traces(scan_id, file=filename)
            if row.get("outcome") in ("FAIL", "REVIEW") and row["rule_id"] not in cleared
            and int(row.get("finding_count") or 0) > 0]


# ── Fan-out scan pipeline (ADR 0007): discover → scan_file → finalize ─────────
import datetime as _dt
import shutil as _shutil
import tempfile as _tempfile
from pathlib import Path as _Path


# ── Classification bucket constants (PRD §6.4) ───────────────────────────────────
_ASSESSABLE_DOC_CLASSES = frozenset({'slide-deck', 'text-document', 'pdf-document', 'spreadsheet', 'web-page'})
_METADATA_ONLY_DOC_CLASSES = frozenset({'image', 'audio-video'})


def _count_inventory_classes(scan_id: str) -> dict:
    """Count the five mutually exclusive classification buckets (PRD §6.4). Buckets sum to the
    total inventory rows. 'excluded' is a catch-all for doc_class values not in the known sets
    (currently always 0; reserved for future policy-excluded classes)."""
    assessable = metadata_only = unsupported = eligibility_unknown = excluded = 0
    for r in core.store.list_inventory(scan_id):
        dc = r.get("doc_class") or ""
        if dc in _ASSESSABLE_DOC_CLASSES:
            assessable += 1
        elif dc in _METADATA_ONLY_DOC_CLASSES:
            metadata_only += 1
        elif dc in ("unsupported", ""):
            unsupported += 1
        elif dc == "unknown":
            eligibility_unknown += 1
        else:
            excluded += 1
    return {"assessable": assessable, "metadata_only": metadata_only,
            "unsupported": unsupported, "eligibility_unknown": eligibility_unknown,
            "excluded": excluded}


# ── Lifecycle rule evaluation during Discover (PRD §4.3 / §6, Phase B4) ─────────
# disposition_policy now has a priority column (Lifecycle Rules build-plan item #6) —
# list_disposition_policies() sorts by it (NULLs last, then name), so `policies` below is already
# in precedence order; nothing here needs to re-sort. The archive-vs-delete precedence decision
# itself lives in disposition.resolve_candidate — shared with the conflicts report
# (routes/disposition.list_conflicts) so both make the same call from one place.


def _sp_scan_cursors(user: str, plan: dict) -> dict:
    """The Graph deltaLink each library stood at when this scan listed it.

    Read from the store AFTER the plan ran, because that is when they are correct: building the
    plan advances (or seeds) every library's cursor to "now", immediately before the walk. So the
    stored value is the position this scan's estate describes, and replaying the delta from it
    later answers "what has changed since ACP listed this" — one Graph call per LIBRARY, not one
    per document, which is the only reason freshness is affordable for SharePoint at all.

    Best-effort per library: a cursor that cannot be read is simply absent, and freshness for that
    library reports `untracked` rather than a guess. Never raises — this is a diagnostic recorded
    on the way past, and it must not be able to fail a scan that has already listed its estate.
    """
    out: dict = {}
    for drive_id in list((plan.get("delta") or {}).keys()) + list((plan.get("full") or {}).keys()):
        try:
            cur = core.store.get_sync_cursor(core._sp_interactive_cursor_key(user, drive_id))
            if cur and cur.get("page_token"):
                # "" is the OneDrive/no-drive key: JSON object keys must be strings, and the
                # reader maps it back (routes/scans._sp_freshness).
                out[drive_id or ""] = cur["page_token"]
        except Exception:  # noqa: BLE001
            logger.debug("could not record the delta cursor for drive %s", drive_id, exc_info=True)
    return out


def _sp_site_delta_plan(user: str, token: str, folder, folders) -> dict | None:
    """Phase 3: the per-library incremental plan for a SITE-scoped SharePoint request.

    Resolving it needs the libraries, and the libraries need one Graph call per selected site —
    the same `_sp_drives` call the walk is about to make anyway. Paying it here buys the chance
    to not walk those libraries at all, which on an estate where most documents have not changed
    is the entire point of the feature.

    Returns None — meaning "walk everything, as before" — for any request this cannot answer for:
    a folder-narrowed scan (Graph's delta query has no folder filter, so a reconstruction could
    not honour the narrowing), no sites, or a Graph failure while listing them. Uncertainty
    resolves to the full, already-correct listing every time; that is the same contract
    `_sp_delta_check` keeps one level down, and it is why this feature cannot under-report.
    """
    try:
        from scanner import _sp_drives, _sp_locations
        roots = [f for f in (list(folders) if folders else ([folder] if folder else []))
                 if f and f != "root"]
        locs, sites = _sp_locations(roots)
        if locs or not sites:
            return None
        drive_ids: list[str] = []
        for site in sites:
            drive_ids += [d["id"] for d in _sp_drives(token, site) if d.get("id")]
        if not drive_ids:
            return None
        plan = core.sp_multi_sync_plan(user, token, drive_ids)
        return plan if plan.get("delta") or plan.get("full") else None
    except Exception:  # noqa: BLE001
        # THE WHOLE BODY, not just the Graph call. This function's only job is to decide whether
        # a shortcut is available; every failure mode of that decision — a Graph error, a shape
        # this code did not expect, a cursor row it could not read — has the same correct answer,
        # which is the full listing that would have run anyway. An optimisation that can fail a
        # scan is worse than no optimisation, and the failure would land on the largest estates
        # first.
        logger.debug("sharepoint site delta plan unavailable; walking in full", exc_info=True)
        return None


def _sp_scannable_metadata(it: dict) -> dict:
    """The inventory columns a SCANNABLE SharePoint item's normalized metadata contributes.

    scanner builds the full row itself for the non-scannable half (_sp_inventory_row); the
    scannable half comes back as an analysis record and is reshaped here. Both must land the same
    columns from the same normalized record, or one inventory ends up describing its media and
    its documents in different vocabularies.

    Reuses scanner._inv_row rather than re-deriving the mapping: a second copy of "which field of
    the metadata record becomes which column" is the drift this repo keeps paying for.
    """
    meta = it.get("sp_metadata")
    if not isinstance(meta, dict):
        return {}
    from scanner import _inv_row
    row = _inv_row(file=it.get("name") or "", sp_meta=meta)
    return {k: row.get(k) for k in ("content_type", "retention_label", "sensitivity_label",
                                    "sharing_scope", "item_kind", "checked_out_by",
                                    "sp_version", "modified_by", "sp_metadata")}


def _sp_rule_inputs(row: dict) -> dict:
    """The SharePoint-native half of a lifecycle rule's input document, from one inventory row.

    Two kinds of thing come out of `sp_metadata`, and they are not the same kind:

      * the tenant's own MANAGED COLUMNS, which a `managed:<Column>` condition reads;
      * the per-field AVAILABILITY and its reasons, which no condition reads — they exist so the
        rule's EVIDENCE can say "'retention_label' was not read from SharePoint" instead of
        "'retention_label' not recorded". A rule matches nothing either way; only the human
        reading why can act on the difference, and only if it reaches them.

    Never raises on a malformed blob: `sp_metadata` is JSON written by an older build or a
    partially-rolled-forward replica, and a lifecycle evaluation that died on it would take the
    whole Discover run with it for a field nothing had to have.
    """
    import json as _json
    out = {k: row.get(k) for k in ("content_type", "retention_label", "sensitivity_label",
                                   "sharing_scope", "item_kind", "checked_out_by",
                                   "site_name", "library_name")}
    raw = row.get("sp_metadata")
    if not raw:
        return out
    try:
        blob = _json.loads(raw) if isinstance(raw, str) else raw
    except Exception:  # noqa: BLE001 — a rule input must never fail a scan
        return out
    if not isinstance(blob, dict):
        return out
    out["managed_columns"] = blob.get("managed_columns") or {}
    # SMART ARCHIVAL. `collaborator_count` is what a rule keys on — "archive if older than seven
    # years AND at most one person was ever involved" — and `collaborator_basis` is what stops it
    # being read as more than it is: under `authorship` the count is a FLOOR off the listing page
    # (creator + last editor), under `permissions` it is everyone with access. A rule written as
    # `<= 1` is correct under both, which is the shape to recommend.
    collab = blob.get("collaborators") or {}
    out["collaborator_count"] = collab.get("count")
    out["collaborator_basis"] = collab.get("basis")
    # ACCESS IS NOT USE. `collaborator_count` says who CAN open a document; this says whether
    # anybody HAS, over Graph's own seven-day window. None when the analytics container was not
    # read — a rule keyed on it then matches nothing, which is correct and is why the count must
    # never be defaulted to 0 on the way through here.
    activity = blob.get("activity") or {}
    out["recent_actor_count"] = activity.get("actors")
    out["recent_action_count"] = activity.get("actions")
    out["sp_availability"] = blob.get("availability") or {}
    out["sp_reasons"] = blob.get("reasons") or {}
    return out


def _evaluate_discover_lifecycle_rules(scan_id: str, source: str, actor: str | None,
                                       progress_cb=None, tick_every: int = 10) -> dict:
    """Evaluate enabled disposition policies against the freshly persisted inventory and record
    CANDIDATE outcomes (PRD §4.3 / §6, Phase B4). Candidate-first: a matching archive rule flags
    the file 'Archive Candidate' and a delete rule 'Delete Candidate' — the actual Drive
    move/delete is NEVER performed here (that stays behind the approval/execute path). A tag rule
    writes system tags. Idempotent (AC-13): re-running Discover adds no duplicate tags (file_tags
    PK) and logs no duplicate audit row (doc_has_disposition guard), so unchanged inputs+rules
    produce no new actions.

    Keying: tags/status are keyed by (scan_id, file) — the file_tags / scan_inventory grain. The
    audit doc_id is a discover-grain key ("scan:<scan_id>:<file>"), deliberately distinct from the
    approval-time drive:<fileId> key the Tag-action PR (#314) uses, so the two paths never collide.

    progress_cb(files_evaluated, rules_enabled): called every 10 files so the route can emit
    live lifecycle progress ticks. Optional — omit for callers that don't surface live updates.

    Returns {"rules_enabled": N, "files_evaluated": M, "lifecycle_matches": K} so callers can
    surface lifecycle activity stats in the 'done' progress payload.
    """
    import disposition
    import hashlib
    from lifecycle_identity import TERMINAL, source_identity
    core.store.reconcile_source_lifecycle(scan_id)
    # owner=actor is the fix for the reported "rules created by the demo account can appear in
    # your workflow" defect: this was the one caller of list_disposition_policies() that already
    # had the scan owner in scope (as `actor`) and still fetched every tenant's enabled policies.
    policies = [p for p in core.store.list_disposition_policies(owner=actor) if p.get("enabled")]
    if not policies:
        return {"rules_enabled": 0, "files_evaluated": 0, "lifecycle_matches": 0,
                "lifecycle_archive": 0, "lifecycle_delete": 0, "lifecycle_tagged": 0,
                "rules_total": 0, "rules_completed": 0, "files_unevaluable": 0,
                "files_skipped": 0, "conflicts": 0}

    # Pre-load existing dispositions for this scan in one query — replaces N per-file
    # doc_has_disposition() SELECTs with a set lookup (idempotency guard AC-13, bulk path).
    seen = core.store.get_scan_dispositions(scan_id)

    # Pre-parse match conditions and action configs once per policy instead of once per
    # (file × policy). For M policies and N files this cuts JSON decodes from N×M to M.
    # _match=None marks a policy whose match JSON is unparseable — skip it in the loop.
    for p in policies:
        try:
            p["_match"] = _json.loads(p.get("match") or "[]")
        except Exception:
            p["_match"] = None
        try:
            p["_action_config"] = _json.loads(p.get("action_config") or "{}")
        except Exception:
            p["_action_config"] = {}

    # Accumulate writes across the loop; flush as bulk operations at the end instead of
    # issuing N individual INSERT/UPDATE calls (the dominant latency at scale).
    tag_rows: list = []    # (scan_id, file, tag, kind, rule_id)
    audit_rows: list = []  # (audit_id, doc_id, policy_id, action, result, detail, owner_email)
    status_rows: list = [] # (scan_id, file, status, rule_id, reason)
    evaluation_rows: list = []
    effective_rows: list = []

    files_evaluated = 0
    lifecycle_matches = 0
    lc_archive = 0
    lc_delete = 0
    lc_tagged_files: set = set()
    lc_errors = 0
    lc_conflicts = 0
    lc_skipped = 0
    _eval_start = _dt.datetime.now(_dt.timezone.utc).timestamp()
    for r in core.store.list_inventory(scan_id):
        files_evaluated += 1
        if progress_cb and files_evaluated % tick_every == 0:
            _elapsed = max(0.1, _dt.datetime.now(_dt.timezone.utc).timestamp() - _eval_start)
            progress_cb({
                "files_evaluated": files_evaluated,
                "rules_enabled": len(policies),
                "files_matched": lifecycle_matches,
                "archive_candidates": lc_archive,
                "delete_candidates": lc_delete,
                "files_tagged": len(lc_tagged_files),
                "unevaluable": lc_errors,
                "files_unevaluable": lc_errors,
                "files_skipped": lc_skipped,
                "conflicts": lc_conflicts,
                "rules_total": len(policies),
                "rules_completed": 0,
                "current_rule_id": None,
                "checkpoint_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
                "rate_per_second": round(files_evaluated / _elapsed),
            })
        file = r.get("file")
        if r.get('lifecycle_status') in TERMINAL:
            # An evaluated recommendation is not a provider restoration. Preserve both
            # projections even when a changed rule now recommends the opposite disposition.
            lc_skipped += 1
            evaluated_at = _dt.datetime.now(_dt.timezone.utc).isoformat()
            status = r['lifecycle_status']
            linked = source_identity(actor,source,r) is not None
            reason = r.get('lifecycle_reason') or f'retained lifecycle state: {status}'
            for p in policies:
                version = int(p.get('version') or 1)
                evaluation_id = hashlib.sha256(f"{scan_id}:{file}:{p['policy_id']}:{version}".encode()).hexdigest()[:32]
                evaluation_rows.append((evaluation_id,scan_id,file,p['policy_id'],version,'terminal',
                    _json.dumps({'conditions': [],'file': file,'reason': reason,'source_identity_available': linked}),
                    p.get('action'),p.get('priority'),evaluated_at,actor))
            effective_rows.append((file,scan_id,None,status,reason,'applied',None,evaluated_at,actor))
            continue
        # An Exempted file (legal hold etc.) is never moved to a candidate status, tagged, or
        # re-audited by a rule run (PRD §6). It still receives an immutable EXEMPT evaluation
        # for every enabled rule so the ledger reconciles evaluated/exempt files.
        if r.get("lifecycle_status") == "Exempted":
            lc_skipped += 1
            evaluated_at = _dt.datetime.now(_dt.timezone.utc).isoformat()
            for p in policies:
                version = int(p.get("version") or 1)
                evaluation_id = hashlib.sha256(
                    f"{scan_id}:{file}:{p['policy_id']}:{version}".encode()).hexdigest()[:32]
                evaluation_rows.append((evaluation_id, scan_id, file, p["policy_id"], version,
                                        "exempt", _json.dumps({"conditions": [], "policy_name": p.get("name"),
                                                               "file": file, "reason": "legal hold or explicit exemption"}),
                                        p.get("action"), p.get("priority"), evaluated_at, actor))
            effective_rows.append((file, scan_id, None, "Exempted", "legal hold or explicit exemption wins",
                                   "not_required", None, evaluated_at, actor))
            continue
        try:
            doc_id = f"scan:{scan_id}:{file}"
            doc = {
                "doc_id": doc_id,
                "source": source,
                "path": r.get("path"),
                "parent_folder": r.get("parent_folder"),
                "created_at": r.get("created_at"),
                # matches() maps source_modified -> modified_at so "modified before <date>" works.
                "source_modified": r.get("source_modified"),
                "owner": r.get("owner"),
                # doc_class/size_kb (Lifecycle Rules build-plan item #3, "file type"/"larger than")
                # were added to disposition.FIELDS and the condition builder in #610, but never wired
                # in here — a file-type or larger-than rule validated and saved fine, then silently
                # matched nothing at Discover time forever, because `values.get("doc_class")` (and
                # `size_kb`) read a key this dict never set. Both are already on the inventory row.
                "doc_class": r.get("doc_class"),
                "size_kb": r.get("size_kb"),
                # SharePoint-native rule inputs (Phase 2). Same wiring lesson as doc_class and
                # size_kb above: a field in disposition.FIELDS that this dict never sets is a
                # rule that validates, saves, and then silently matches nothing forever.
                **_sp_rule_inputs(r),
            }
            matched = []
            evaluated = []
            for p in policies:
                if p["_match"] is None:
                    evaluated.append((p, {"matched": False, "conditions": [],
                                          "_result": "unevaluable", "reason": "rule definition could not be read"}))
                    continue
                matched_by_policy = disposition.matches(doc, p["_match"])
                result = disposition.evaluate(doc, p["_match"])
                result["_result"] = disposition.evaluation_result(result)
                if result["_result"] != "unevaluable":
                    result["_result"] = "matched" if matched_by_policy else "not_matched"
                evaluated.append((p, result))
                if result["_result"] == "matched":
                    matched.append(p)
            evaluated_at = _dt.datetime.now(_dt.timezone.utc).isoformat()
            version_by_policy = {p["policy_id"]: int(p.get("version") or 1) for p in policies}
            eval_ids = {}
            for p, result in evaluated:
                version = version_by_policy[p["policy_id"]]
                evaluation_id = hashlib.sha256(
                    f"{scan_id}:{file}:{p['policy_id']}:{version}".encode()).hexdigest()[:32]
                eval_ids[p["policy_id"]] = evaluation_id
                evidence = {"conditions": result.get("conditions", []),
                            "policy_name": p.get("name"), "file": file,
                            "reason": result.get("reason")}
                evaluation_rows.append((evaluation_id, scan_id, file, p["policy_id"], version,
                                        result.get("_result", "unevaluable"), _json.dumps(evidence),
                                        p.get("action"), p.get("priority"), evaluated_at, actor))
            destructive_unevaluable = any(
                result.get("_result") == "unevaluable" and p.get("action") in ("archive", "delete")
                for p, result in evaluated)
            if not matched:
                if any(result.get("_result") == "unevaluable" for _, result in evaluated):
                    reason = "required lifecycle evidence was missing; no destructive recommendation was made"
                    status_rows.append((scan_id, file, "Unevaluable", None, reason))
                    effective_rows.append((file, scan_id, None, "Unevaluable", reason, "not_required", None, evaluated_at, actor))
                    lc_errors += 1
                else:
                    effective_rows.append((file, scan_id, None, "Active", "no enabled rule matched", "not_required", None, evaluated_at, actor))
                continue
            lifecycle_matches += 1
            # ── Tag rules: apply EVERY matching tag policy. Tag + Archive both match → tags AND the
            # Archive candidate status are applied (PRD §6), so tags are never suppressed by a
            # co-matching disposition rule.
            for p in matched:
                if p.get("action") != "tag":
                    continue
                key = (doc_id, p["policy_id"])
                if key in seen:
                    continue  # already tagged + audited on an earlier Discover — idempotent
                tags = disposition.tag_list(p["_action_config"])
                if not tags:
                    continue
                for tag in tags:
                    if tag:
                        tag_rows.append((scan_id, file, tag, "system", p["policy_id"]))
                _audit_id = hashlib.sha256(
                    f"discover:{scan_id}:{file}:{p['policy_id']}:tag".encode()).hexdigest()[:24]
                audit_rows.append((_audit_id, doc_id, p["policy_id"], "tag",
                                   "applied", "tagged: " + ", ".join(tags), actor,
                                   int(p.get("version") or 1)))
                lc_tagged_files.add(file)
                seen.add(key)  # idempotent within this run
            if destructive_unevaluable:
                reason = "required evidence for a destructive lifecycle rule was missing; no destructive recommendation was made"
                status_rows.append((scan_id, file, "Unevaluable", None, reason))
                effective_rows.append((file, scan_id, None, "Unevaluable", reason, "not_required", None, evaluated_at, actor))
                lc_errors += 1
                continue
            # ── Candidate status: archive-vs-delete precedence (PRD §6), shared with the conflicts
            # report — see disposition.resolve_candidate's own docstring for the precedence rule.
            chosen, new_status, reason = disposition.resolve_candidate(matched, actor)
            if chosen is None and new_status == "Conflict — review required":
                lc_conflicts += 1
                status_rows.append((scan_id, file, new_status, None, reason))
                effective_rows.append((file, scan_id, None, new_status, reason, "review_required", None, evaluated_at, actor))
                conflict_ids = {p["policy_id"] for p in matched if p.get("action") in ("archive", "delete")}
                for i, row in enumerate(evaluation_rows):
                    if row[1] == scan_id and row[2] == file and row[3] in conflict_ids:
                        evaluation_rows[i] = (*row[:5], "conflict", *row[6:])
                continue
            if chosen is None:
                effective_rows.append((file, scan_id, None, "Active", "matching rules were tag-only", "not_required", None, evaluated_at, actor))
                continue
            key = (doc_id, chosen["policy_id"])
            if key in seen:
                continue  # Retain the executed/overridden projection, not a new pending claim.
            effective_rows.append((file, scan_id, eval_ids.get(chosen["policy_id"]), new_status,
                                   reason, "pending_approval", None, evaluated_at, actor))
            status_rows.append((scan_id, file, new_status, chosen["policy_id"], reason))
            _audit_id = hashlib.sha256(
                f"discover:{scan_id}:{file}:{chosen['policy_id']}:{chosen.get('action', '')}".encode()
            ).hexdigest()[:24]
            audit_rows.append((_audit_id, doc_id, chosen["policy_id"],
                               chosen.get("action"), "pending_approval", reason, actor,
                               version_by_policy.get(chosen["policy_id"], 1)))
            if chosen.get("action") == "archive":
                lc_archive += 1
            elif chosen.get("action") == "delete":
                lc_delete += 1
            seen.add(key)  # idempotent within this run
        except Exception:  # noqa: BLE001 — one bad row must not discard the whole pass
            lc_errors += 1

    # Flush accumulated writes in bulk — one executemany each instead of N individual calls.
    # Evidence first: a lifecycle state without its immutable explanation is incomplete. If a
    # later projection write fails, a retry reuses the deterministic evaluation ids and safely
    # finishes the projection; the reverse order could expose an unexplained candidate forever.
    core.store.bulk_create_lifecycle_evaluations(evaluation_rows)
    core.store.bulk_upsert_effective_dispositions(effective_rows)
    core.store.bulk_add_file_tags(tag_rows)
    core.store.bulk_set_lifecycle_status(status_rows)
    core.store.bulk_create_disposition_audit(audit_rows)

    return {"rules_enabled": len(policies), "files_evaluated": files_evaluated,
            "rules_total": len(policies), "rules_completed": len(policies),
            "lifecycle_matches": lifecycle_matches,
            "lifecycle_archive": lc_archive, "lifecycle_delete": lc_delete,
            "lifecycle_tagged": len(lc_tagged_files),
            "lifecycle_errors": lc_errors, "files_unevaluable": lc_errors,
            "files_skipped": lc_skipped, "conflicts": lc_conflicts,
            "checkpoint_at": _dt.datetime.now(_dt.timezone.utc).isoformat()}


def scan_event(scan_id: str | None, kind: str, **kw) -> None:
    """Best-effort append to the durable lifecycle log (ADR 0042). NEVER raises.

    Swallows EVERYTHING, including the ValueError `append_scan_event` raises for an unknown
    `kind`. That looks like it defeats the store's own guard, and the trade is deliberate: the
    ADR's rule is absolute — "an append must never be able to fail the work it describes" — and a
    typo'd kind string on a rarely-taken branch (a discovery conflict, a listing failure) would
    otherwise crash a real scan in production over a telemetry line. The guard is not lost, only
    moved: `test_scan_events_emitted.py` extracts every kind literal passed to this function
    across the emit sites and asserts each is in `Store.SCAN_EVENT_KINDS`, so a typo fails CI on
    every branch — including the ones no test happens to execute.

    Exported (no leading underscore) because routes/scans.py and worker.py emit through it too;
    one wrapper, so the never-raises contract cannot be honoured at one call site and forgotten
    at the next.
    """
    if not scan_id:
        return                     # nothing to anchor the event to — see the thread-path comment
    try:
        core.store.append_scan_event(scan_id, kind, **kw)
    except Exception:              # noqa: BLE001 — see the docstring; this is the whole point
        logger.debug("scan_event(%s, %s) failed", scan_id, kind, exc_info=True)


def _mark_discovered(scan_id: str) -> None:
    """Record the run-level discovery-completion instant, and never fail discovery over it.

    The inventory is already written by the time this runs. Losing the timestamp costs a date on
    a screen; raising here would lose the inventory the job just spent the estate's listing budget
    producing — the same fail-quiet contract the Langfuse discover trace already follows, and for
    the same reason. A run with inventory rows still reads correctly if the stamp is lost: the
    frontend falls back to the newest per-file `scan_inventory.discovered_at`.

    A GENUINELY EMPTY run has no such fallback — zero rows means `resolveInventoryTime` on the
    frontend has nothing to fall back to, so a run that lists 0 files and then loses this stamp to
    a transient DB error shows "completion time not recorded" forever, with nothing in the UI or
    the decision log to say why. One retry closes the transient case without weakening the
    fail-quiet contract (a second failure still doesn't raise), and logging it as a decision makes
    a persistent failure visible via the same `/decisions?scan_id=` channel already used to
    diagnose `scan.suspicious_zero` / `scan.unreachable_zero` — no DB access required to tell the
    two apart from here on."""
    try:
        core.store.mark_discovery_complete(scan_id)
        return
    except Exception:
        logger.warning("_mark_discovered: failed to stamp discovered_at for %s, retrying once",
                       scan_id, exc_info=True)
    try:
        core.store.mark_discovery_complete(scan_id)
        return
    except Exception as exc:
        logger.warning("_mark_discovered: retry also failed to stamp discovered_at for %s",
                       scan_id, exc_info=True)
        try:
            core.store.log_decision("system", "scan.discovered_at_stamp_failed",
                                    scan_id=scan_id, detail=str(exc))
        except Exception:
            logger.warning("_mark_discovered: could not even log the stamp failure for %s",
                           scan_id, exc_info=True)


def _discover_norm_row(it: dict) -> dict:
    """One scanner listing item as the discover-phase normalised record.

    Lifted out of _scan_discover's listing block so the PER-SITE CHECKPOINT can build exactly the
    same row. A checkpoint that persisted a site through a second, parallel mapping would drift
    from the end-of-scan one the moment either grew a field — and the drift would show up as a
    resumed scan whose early sites are missing metadata the late ones have, which reads as a
    tenant that labels some sites and not others.
    """
    return {"file": it["name"], "source_name": it.get("source_name") or it["name"],
            "drive_file_id": it.get("id"), "mime": it.get("mime"),
            "path": it.get("path"), "checksum": it.get("checksum"),
            "drive_id": it.get("driveId"),
            # WHICH SharePoint site and library this document came from. Carried on the
            # scannable record by the walk (scanner._sp_classify_item) and persisted per
            # row: a run now spans a SET of sites, so the scan's scope can no longer answer
            # "which site is this file in" for any individual document.
            "site_id": it.get("siteId"), "library_name": it.get("libraryName"),
            "site_name": it.get("siteName"),
            # The SharePoint-native metadata for the ANALYSED half of the estate. The
            # non-scannable half already carries it (scanner._sp_inventory_row builds the
            # row itself); without this the two halves of one inventory would disagree —
            # a retention label on every video and none on any document, which reads as a
            # tenant that labels media and is in fact a wiring gap.
            **_sp_scannable_metadata(it),
            "drive_account_id": it.get("drive_account_id"),
            "source_modified": it.get("source_modified"),
            "source_mime": it.get("source_mime"), "created_at": it.get("created_at"),
            "owner": it.get("owner"), "parent_folder": it.get("parent_folder"),
            "size_kb": it.get("size_kb"),
            "content_type": it.get("content_type")}


def _discover_inventory_row(it: dict) -> dict:
    """One normalised record as the scan_inventory row add_inventory persists. See
    _discover_norm_row for why this is a function rather than a comprehension."""
    import classify as _cls
    return {"file": it["file"], "source_name": it.get("source_name") or it["file"],
            "drive_file_id": it.get("drive_file_id"),
            "mime": it.get("source_mime"), "size_kb": it.get("size_kb"),
            "doc_class": _cls.classify_from_metadata(it["file"], it.get("source_mime"))["doc_class"],
            "checksum": it.get("checksum"), "path": it.get("path"),
            "created_at": it.get("created_at"), "source_modified": it.get("source_modified"),
            "owner": it.get("owner"), "parent_folder": it.get("parent_folder"),
            "drive_id": it.get("drive_id"),
            "site_id": it.get("site_id"), "library_name": it.get("library_name"),
            "site_name": it.get("site_name"),
            **{k: it.get(k) for k in ("retention_label", "sensitivity_label",
                                      "sharing_scope", "item_kind", "checked_out_by",
                                      "sp_version", "modified_by", "sp_metadata")},
            "drive_account_id": it.get("drive_account_id"),
            "content_type": it.get("content_type")}


def _scope_collapse(current_count: int, baseline_count: int) -> dict | None:
    """Describe a material suspicious non-zero listing collapse, if one occurred."""
    import os as _os
    min_baseline = max(1, int(_os.getenv("ACP_DISCOVERY_COLLAPSE_MIN_BASELINE", "100")))
    min_drop = max(1, int(_os.getenv("ACP_DISCOVERY_COLLAPSE_MIN_DROP", "50")))
    ratio_limit = float(_os.getenv("ACP_DISCOVERY_COLLAPSE_RATIO", "0.25"))
    if baseline_count < min_baseline or baseline_count - current_count < min_drop:
        return None
    ratio = current_count / baseline_count if baseline_count else 1.0
    if ratio >= ratio_limit:
        return None
    return {"status": "blocked", "code": "unexpected_scope_collapse",
            "current_count": current_count, "baseline_count": baseline_count,
            "retained_ratio": round(ratio, 6), "threshold_ratio": ratio_limit}


def _enforce_scope_collapse_guard(scan_id: str, user: str | None, scope: dict,
                                  items: list[dict], inventory: list[dict] | None = None,
                                  prior_count: int = 0) -> None:
    """Fail a severely collapsed whole-source discovery before its inventory is published.

    `prior_count` is the estate THIS scan already persisted on an earlier attempt — the sites a
    per-site checkpoint completed before the run died. It is the load-bearing argument for
    resumable scans and it is easy to leave out: a resumed run lists only the sites it has left,
    so a 30-site estate that got 28 sites in on attempt 1 arrives here with two sites' worth of
    files and looks exactly like the permissions collapse this guard exists to catch. The scan
    would then be failed and blocked, with a message telling the operator to check access that is
    not the problem — strictly worse than having no resume at all, because the first attempt at
    least finished with a bad count rather than a wrong diagnosis.
    """
    whole_source = (scope.get("kind") == "drive" or
                    (scope.get("kind") == "sharepoint" and not scope.get("folders")))
    # `(items or prior_count)` rather than `items`: a resumed run whose remaining sites list
    # nothing still has an estate — the one a previous attempt persisted — and skipping the guard
    # on it would make "die once, retry" a way to walk a genuine collapse straight past the check.
    if not (whole_source and user and (items or prior_count) and not scope.get("truncated")):
        return
    current_account = next(
        (it.get("drive_account_id") for it in items if it.get("drive_account_id")), None)
    baseline = core.store.last_published_whole_source_baseline(
        scan_id, owner=user, current_scope=scope, drive_account_id=current_account)
    if not baseline:
        return
    # Compare the same grain on both sides: baseline count_inventory includes assessable AND
    # unsupported/media rows. Drive's raw listing count is ideal; other adapters fall back to
    # the two collections that together form the inventory.
    raw_count = scope.get("raw")
    current_count = (int(raw_count) if isinstance(raw_count, (int, float))
                     else len(items) + len(inventory or [])) + prior_count
    integrity = _scope_collapse(current_count, baseline["count"])
    if not integrity:
        return
    integrity["baseline_scan_id"] = baseline["scan_id"]
    message = (f"Discovery found {current_count} files, down from {baseline['count']} in the last "
               "verified whole-source scan; refusing to publish or assess this likely "
               "permissions/scope collapse. Reconnect the source or verify its access, then "
               "run Discovery again.")
    integrity["message"] = message
    scope["integrity"] = integrity
    scope.setdefault("enumeration", {})["complete"] = False
    # `current_count`, not `len(items)`: on a resumed run those differ, and the number left on the
    # row must be the one the refusal is about — otherwise the message and the estate a reader
    # sees beside it disagree, on the screen where the disagreement is the whole question.
    core.store.set_scan_files(scan_id, current_count)
    core.store.merge_scan_scope(scan_id, scope)
    core.store.set_scan_status(scan_id, "failed")
    core.store.log_decision("system", "scan.scope_collapse", scan_id=scan_id, detail=message)
    try:
        core.store.release_discovery_guard(scan_id)
    except Exception:
        logger.warning("_scan_discover: could not release guard after scope collapse for %s",
                       scan_id, exc_info=True)
    raise RuntimeError(message)


def persist_discovery_inventory(scan_id: str, inv: list[dict], source: str, actor: str | None,
                                progress_cb=None) -> dict:
    """Persist the per-file discovery inventory and evaluate the lifecycle (archival/deletion) rules
    over it — the shared post-discovery step so a scan marks Archive/Delete candidates regardless of
    which scan path ran it. Historically only the fanout path (_scan_discover) did this inline; the
    default in-process scan (routes/scans.py) skipped it, so archive/delete rules were silently NOT
    evaluated on a normal Discover, and that path also left the per-file inventory (which the Assess
    eligibility count reads) unpopulated. Both are fixed by routing every path through here.

    Idempotent: add_inventory de-dupes on (scan_id, file) and the rule evaluation is candidate-first
    and guarded (doc_has_disposition), so a re-run adds nothing. Never executes a Drive move/delete.
    mark_discovery_complete is set-once for the same reason, so a re-run does not re-date the
    snapshot either.

    Returns a merged dict with save-outcome, lifecycle stats, and classification bucket counts:
      {"new": N, "updated": M, "unchanged": 0, "failed": P,
       "rules_enabled": R, "files_evaluated": E, "lifecycle_matches": K,
       "assessable": A, "metadata_only": B, "unsupported": C, "eligibility_unknown": D, "excluded": X}.
    Callers that emit progress payloads should forward all keys for the respective step KPIs."""
    from scanner import _dedupe_inventory_files
    _dedupe_inventory_files(inv)
    outcome = core.store.add_inventory(scan_id, inv) if inv else {"new": 0, "updated": 0, "unchanged": 0, "failed": 0}
    # ADR 0042. Emitted HERE rather than at this function's three call sites because this is the
    # shared post-discovery step every non-fanout path routes through (routes/scans.py's sync and
    # thread branches, and _assess_discover) — the same reason the function itself exists. The
    # fan-out path (_scan_discover) inlines its own add_inventory/lifecycle/stamp and emits its
    # own events there; it does NOT call this, so nothing is double-counted.
    scan_event(scan_id, "scan.inventory_saved", phase="saving", owner_email=actor,
               detail={"new": outcome.get("new"), "updated": outcome.get("updated"),
                       "unchanged": outcome.get("unchanged"), "failed": outcome.get("failed")})
    lifecycle_stats = _evaluate_discover_lifecycle_rules(scan_id, source, actor,
                                                         progress_cb=progress_cb)
    scan_event(scan_id, "scan.lifecycle_applied", phase="lifecycle", owner_email=actor,
               detail={"rules_enabled": lifecycle_stats.get("rules_enabled", 0),
                       "files_evaluated": lifecycle_stats.get("files_evaluated", 0),
                       "matches": lifecycle_stats.get("lifecycle_matches", 0),
                       "archive": lifecycle_stats.get("lifecycle_archive", 0),
                       "delete": lifecycle_stats.get("lifecycle_delete", 0),
                       "tagged": lifecycle_stats.get("lifecycle_tagged", 0)})
    class_stats = _count_inventory_classes(scan_id)
    # The discovery phase is over: the inventory is persisted and the lifecycle rules have run.
    # Stamp WHEN, because every count taken from this inventory is only true as of this instant
    # and nothing else on scan_runs records it — completed_at is the end of ASSESS. Stamped after
    # the writes above so it dates an inventory that exists rather than one that was attempted.
    _mark_discovered(scan_id)
    # After the stamp, for the same reason the stamp comes after the writes. NOTE this function is
    # documented idempotent — a re-delivered job runs it again, add_inventory de-dupes and
    # mark_discovery_complete is set-once — so a redelivery DOES append a second scan.discovered.
    # That is the append-only contract working as intended, not a bug to suppress: the job really
    # did run twice, and ADR 0042 has readers take the FIRST terminal event by seq. Suppressing it
    # would need a read-before-write, which is the mutable-cell pattern this log exists to replace.
    scan_event(scan_id, "scan.discovered", phase="done", owner_email=actor,
               detail={"files": class_stats.get("assessable"),
                       "source": source})
    return {**outcome, **lifecycle_stats, **class_stats}


# ── ADR 0004 item 6: per-folder checkpoint helpers ───────────────────────────
# These two functions are module-level (not closures) so tests can monkeypatch them
# without reimporting. Production code calls the real Drive/SP listing APIs.

def _per_folder_mode() -> bool:
    """Return True when per-folder fan-out is enabled (ACP_PER_FOLDER_SCAN_JOBS=1)."""
    return _os.environ.get("ACP_PER_FOLDER_SCAN_JOBS", "0").strip().lower() in (
        "1", "true", "yes", "on")


def _list_top_level_folders(source: str, scope_folder: str | None, toks: dict) -> list:
    """List the immediate subfolders to fan out over (Drive only; SP falls back to file mode).

    Returns a list of dicts with at least {"folder_id": str, "name": str}.
    Module-level so tests can monkeypatch this without reimporting.

    SECURITY: toks is acquired at call time by the discover handler — never stored in the
    job payload. The result contains only folder IDs (not credentials)."""
    if source != "drive":
        return []
    try:
        from scanner import _drive_service, _list_drive_page_all
        drive_token = toks.get("drive")
        if not drive_token:
            return []
        svc = _drive_service(drive_token)
        parent = scope_folder or "root"
        q = (f"'{parent}' in parents "
             "and mimeType='application/vnd.google-apps.folder' "
             "and trashed=false")
        items, _ = _list_drive_page_all(svc, q, max_files=500)
        return [{"folder_id": it["id"], "name": it.get("name", "")} for it in items]
    except Exception:
        return []


def _list_folder_files(source: str, folder_id: str, toks: dict) -> list:
    """List all analysable files under one folder (recursive BFS via scanner._search_folder).

    Returns the same item shape as scanner._list. Module-level so tests can monkeypatch.

    SECURITY: toks is the result of core.get_scan_tokens() called at execution time by the
    scan_folder handler — credentials are never stored in the job payload itself."""
    if source != "drive":
        # A source this function cannot list is not a source with nothing in it. Naming it in the
        # error matters: the payload is built by scan_discover, so an unexpected value here means
        # the fan-out and the handler disagree, and "the folder was empty" is the one report that
        # would hide that from everybody.
        raise FatalJobError(f"cannot list folder {folder_id}: unsupported source {source!r}")
    from scanner import _drive_service, _search_folder
    drive_token = toks.get("drive")
    if not drive_token:
        # No credential is an AUTH failure, not an empty folder, and no number of retries will
        # produce one — so dead-letter it immediately rather than burning five attempts.
        raise FatalJobError(f"cannot list folder {folder_id}: no Drive credential for this scan")
    try:
        svc = _drive_service(drive_token)
        return _search_folder(svc, folder_id)
    except JobCancelledError:
        # Not an empty folder, and not a listing failure either. Distinct from the clause below
        # so a Stop is never recorded as a fault. Found by
        # test_no_blanket_handler_over_cancellable_work_omits_a_cancellation_clause, not by
        # reading, which is the whole argument for that guard.
        raise
    except Exception as e:
        # WHY THIS NO LONGER RETURNS []. It used to, and the old comment called that "a defensible
        # answer to 'we could not list it'". It is not: the caller does not distinguish the two,
        # so a rate-limited Drive, an expired token or a 500 was recorded as the folder's real
        # contents — zero files — and the scan then finalized reporting FULL coverage of an
        # estate it had never read. A wrong result that looks complete is worse than a visible
        # failure, and this was the one place in the folder path that could produce one.
        #
        # Raising is only safe because a dead scan_folder job now advances the folder counter
        # (store._record_dead_scan_folder). Before that, this raise would have traded a silent
        # wrong answer for a permanently wedged scan — which is why the two changes ship
        # together and why the store test exists.
        raise RuntimeError(f"could not list folder {folder_id}: {e}") from e


def _process_scan_folder_item(scan_id: str, item: dict, *, source: str,
                               ai: bool, pii: bool, user: str | None,
                               job: dict | None = None) -> None:
    """Download + analyse + persist ONE file from a scan_folder job.

    Module-level so tests can monkeypatch. Production delegates to _analyse_and_persist_one,
    the same function used by scan_file, so both paths share identical analysis logic."""
    import lf as _lf
    toks = core.get_scan_tokens(scan_id)
    svc = _make_svc(source, toks)
    now = _dt.datetime.now(_dt.timezone.utc).isoformat()
    _analyse_and_persist_one(
        scan_id, item, source, pii, svc, toks, now, _lf,
        user=user, rubric_hash=core.active_rubric().hash, incremental=True, job=job)


@handler("scan_folder")
def _scan_folder(payload: dict, job: dict) -> None:
    """Process all documents in one folder — the per-folder checkpoint unit for ADR 0004 item 6.

    Payload: {"scan_id": "...", "folder_id": "...", "source": "drive", "ai": bool, "pii": bool}
    Credentials are acquired at execution time via core.get_scan_tokens(scan_id) — NEVER stored
    in the job payload (security constraint: no token snapshot in durable storage).

    check_cancel() is called between every document so the scan can be cooperatively stopped
    mid-folder. Increments completed_folders on the scan_runs row and triggers scan_finalize
    once completed_folders reaches total_folders."""
    from worker import check_cancel
    scan_id = payload.get("scan_id") or job.get("scan_id")
    folder_id = payload.get("folder_id")
    source = payload.get("source", "drive")
    ai = bool(payload.get("ai", True)) and core.store.get_ai_enabled()
    pii = bool(payload.get("pii", False))
    user = payload.get("user")

    if not scan_id:
        raise FatalJobError("scan_folder job missing scan_id")
    if not folder_id:
        raise FatalJobError("scan_folder job missing folder_id")

    # SECURITY: acquire credentials at execution time, not from the durable job payload.
    # core.get_scan_tokens() looks up the authorized connection by scan_id — the payload
    # carries only a reference (the scan_id), never the credential itself.
    toks = core.get_scan_tokens(scan_id)

    _phase(job, f"listing files in folder {folder_id}")
    items = _list_folder_files(source, folder_id, toks)

    _phase(job, f"processing {len(items)} file(s) in folder {folder_id}")
    for item in items:
        check_cancel()
        _process_scan_folder_item(scan_id, item, source=source, ai=ai, pii=pii,
                                  user=user, job=job)
        check_cancel()

    # folder_id, not just scan_id: this line runs again for the same folder whenever the job
    # does — a reclaim after the increment but before the row reaches 'done', or a retry after
    # the enqueue below raises. Counting it twice makes `done >= total` true while other folders
    # are still running, and the scan finalizes over a partial estate reporting it complete.
    done, total = core.store.increment_completed_folders(scan_id, folder_id)
    if total > 0 and done >= total:
        core.store.enqueue_job(
            "scan_finalize",
            {"scan_id": scan_id, "source": source, "ai": ai, "pii": pii},
            scan_id=scan_id)


def _roots_reachable(source: str, svc, roots, sp_token: str | None, corpus) -> tuple[bool, str | None]:
    """Can this listing's roots actually be read? One metadata call each — never a listing.

    Exists because an empty result is not the same fact as a reachable-and-empty source, and the
    two are indistinguishable from the listing alone. A Drive folder the token cannot see, a
    SharePoint item in a library the account lost membership of, a corpus path that is not
    mounted: each returns an empty page with a 200 and no exception. That is the silent zero in
    its purest form — the listing succeeds, finds nothing, and nothing anywhere says the source
    was never actually read.

    Mirrors routes/drive.describe_drive_readiness and routes/sharepoint.describe_sharepoint_readiness
    rather than calling them: those take a FastAPI Request (for the header-derived credential) and
    this runs on a worker, which has the service object and raw token instead. Same probes, same
    bounded cost — `files().get` / the driveItem metadata endpoint, never `.list()` or /children.

    Returns (ok, reason). Two different failures, deliberately not collapsed:

    * a PER-ROOT call that fails (403, 404, a dead service object) means that root could not be
      confirmed readable. Not-confirmed is reason enough to refuse a zero — the question here is
      whether the empty result is evidence, and an unconfirmed root makes it not evidence.
    * the probe's own scaffolding failing (a bad argument, an import error — a bug in here) is
      inconclusive about the source, and falls open. A defect in this function must not become a
      scan outage for estates that were fine.
    """
    try:
        if source == "local":
            from pathlib import Path as _P
            p = _P(corpus) if corpus else None
            if p is not None and not p.is_dir():
                return False, f"local corpus path is not a readable directory: {p}"
            return True, None
        if source == "sharepoint":
            if not sp_token:
                return False, "no SharePoint token — the listing ran unauthenticated"
            from scanner import _sp_item_exists, _sp_default_drive
            checked = list(roots) if roots else [None]
            bad = []
            for r in checked:
                if r:
                    drive_id, _, item_id = r.partition("/") if "/" in r else ("", "", r)
                    if not drive_id:
                        drive_id = _sp_default_drive(sp_token) or ""
                else:
                    drive_id, item_id = (_sp_default_drive(sp_token) or ""), "root"
                if not drive_id:
                    bad.append("no default drive")
                    continue
                res = _sp_item_exists(sp_token, drive_id, item_id)
                if not res.get("exists"):
                    bad.append(res.get("error") or str(r))
            if bad:
                return False, f"{len(bad)} of {len(checked)} root(s) unreachable: {bad[0]}"
            return True, None
        if svc is None:
            return True, None
        checked = list(roots) if roots else ["root"]
        bad = []
        for r in checked:
            try:
                info = svc.files().get(fileId=r, fields="id,trashed").execute()
                # `is True`, not truthiness: Drive returns a JSON boolean here, so anything else
                # is not a trashed flag and must not be read as one. Truthiness quietly condemned
                # every root whose metadata came back as something unexpected — refusing a scan
                # for a field it never actually saw.
                if info.get("trashed") is True:
                    bad.append(f"{r} is trashed")
            except Exception as e:
                bad.append(f"{r}: {e.__class__.__name__}")
        if bad:
            return False, f"{len(bad)} of {len(checked)} root(s) unreachable: {bad[0]}"
        return True, None
    except Exception:
        logger.warning("_roots_reachable: probe failed for source=%s — reporting reachable",
                       source, exc_info=True)
        return True, None


@handler("scan_discover")
def _scan_discover(payload: dict, job: dict) -> None:
    """List the source (paginated, no cap), create the scan_runs row, and enqueue one
    scan_file job per file. Each file's Langfuse trace is opened later, per file, by
    _analyse_and_persist_one — not here."""
    from rubric import Rubric
    from scanner import _list, _drive_service, ACP, FANOUT_MAX_FILES, _scope_for_listing
    scan_id = payload.get("scan_id") or job.get("scan_id")
    # Populate scan_to_job:{scan_id} as early as possible so GET /scans/{scan_id}/discover/stream
    # can find this job's live Redis state. Found live 2026-08-28: no call anywhere in this durable
    # (queue=true) handler ever included "scan_id" in an update_job patch, so
    # core.get_job_id_for_scan(scan_id) — which reads ONLY that Redis mapping, with no fallback to
    # the jobs table's own scan_id column — returned None for every durable scan on any replica
    # that didn't happen to still hold it in the per-process _SCAN_JOB_MAP fallback. The SSE stream
    # then fell through to its 4-miss giveup path after ~1s, degrading every durable scan straight
    # to the Postgres-checkpoint fallback frame instead of ever actually going live.
    # `attempt` rides along on the same write: job["attempts"] is claim_job()'s own durable,
    # monotonic-per-job-row counter (Postgres jobs.attempts, bumped on every claim) — already
    # exactly the "attempt number" PRD Discover-card work wants, so this reuses it rather than
    # inventing a second counter. Read once at handler entry: attempts is fixed for the life of
    # THIS invocation (claim_job bumps it before the handler starts, not during), so every SSE
    # frame this run emits carries the same, correct attempt number.
    if job.get("id") and scan_id:
        core.update_job(job["id"], {"scan_id": scan_id, "attempt": job.get("attempts", 1)})
    source = payload.get("source", "drive")
    # ADR 0042: the claim is already durable before this handler body runs — claim_job stamped
    # jobs.locked_at/locked_by and bumped attempts — so appending here records a fact, not an
    # intention. Emitted on EVERY attempt, including a checkpoint-resume retry, which is the
    # point: a run reclaimed after a lease expiry shows two scan.claimed rows with different
    # worker_id/attempt, and that disagreement stays visible instead of being flattened into one
    # overwritten cell (the zombie-writer case test_job_completion_race.py exists for).
    scan_event(scan_id, "scan.claimed", phase="queued", job_id=job.get("id"),
               worker_id=job.get("locked_by"), attempt=job.get("attempts", 1),
               owner_email=payload.get("user"),
               detail={"source": payload.get("source", "drive")})
    ai = bool(payload.get("ai", True)) and core.store.get_ai_enabled()
    pii = bool(payload.get("pii", False))
    user = payload.get("user")
    folder = payload.get("folder")
    # The fan-out path is the PRODUCTION listing path (see below), so multi-folder scope has to
    # be read here too — wiring only run_scan would narrow scans correctly in dev and scan the
    # whole estate in the deployment that matters.
    folders = payload.get("folders")
    exclude_folders = payload.get("exclude_folders")
    include_subfolders = payload.get("include_subfolders", True)
    toks = core.get_scan_tokens(scan_id)
    # Prefer token from durable job payload — in-memory store is per-replica and invisible to a
    # worker container that does not share the API's memory (split topology without Redis).
    drive_token = payload.get("drive_token") or toks.get("drive")
    sp_tok = payload.get("sp_token") or toks.get("sp")
    rb = Rubric.load_active(ACP / "config")
    from scheduled_scan_execution import services_for_job
    scheduled_services = services_for_job(core.store, scan_id, job, source=source, user=user)
    scheduled_context = scheduled_services[2] if scheduled_services else None
    if scheduled_services:
        svc, sp_tok, _ = scheduled_services
        drive_token = None  # Accepted scheduled Drive reads its server ADC identity.
    if source not in ("local", "sharepoint") and not drive_token and not scheduled_services:
        raise RuntimeError(
            f"scan_id={scan_id!r}: Drive token missing from job payload and SCAN_TOKENS store. "
            "The session token was not forwarded, expired, or this worker replica has no Redis "
            "access to the shared token. Re-authenticate and start a new scan."
        )
    if not scheduled_services:
        svc = None if source in ("local", "sharepoint") else _drive_service(drive_token)
    effective_folder = folder if folder else (None if folders else ("root" if drive_token else None))
    scope: dict = {}
    # `inventory` collects per-file rows for the NON-scannable estate (media / unsupported /
    # extensionless) — every accessible file that is NOT in the assessable `items` set. The
    # scannable rows are built from `items` below; together they inventory the WHOLE estate
    # (PRD Phase A2) while only the assessable subset is ever downloaded and analysed.
    inventory: list[dict] = []
    started = _dt.datetime.now(_dt.timezone.utc).isoformat()
    defer = bool(scheduled_context) or _defer_analysis_to_assess()
    # ── Checkpoint: skip listing if inventory already persisted (retry resume) ──
    # A previous attempt that completed listing + add_inventory but crashed during lifecycle
    # evaluation or tracing already persisted the estate.  Re-listing the entire Drive API
    # would double wall-clock time and quota consumption for large estates.  count_inventory
    # is a cheap COUNT(*) query; the threshold > 0 is correct because add_inventory is
    # idempotent (ON CONFLICT) and the only writer for this scan_id is us.
    _checkpoint_resume = False
    # Sites a previous attempt of THIS scan listed and persisted, as
    # {site_id: {listed, estate, name}} — what the scanner needs to skip them AND to keep
    # reporting what each held. Empty on every run that is not a SharePoint partial resume, which
    # is every run today that did not die mid-listing.
    _sp_resume_sites: dict = {}
    _existing_inv_count = 0
    # The ASSESSABLE half of what a previous attempt persisted. Tracked separately from the row
    # count because scan_runs.files has always meant "assessable files", and a resumed run that
    # reported only the tail it listed would show a thirty-site estate as a two-site one on every
    # screen that reads that column.
    _sp_prior_assessable = 0
    if defer:
        _existing_inv_count = core.store.count_inventory(scan_id)
        if _existing_inv_count > 0:
            # The listing boundary and enumeration evidence were persisted before inventory.
            # Reload them so retry-time integrity checks do not silently see an empty scope.
            scope = (((core.store.get_scan(scan_id, owner=user) or {}).get("run") or {})
                     .get("scope") or {})
            _cp = scope.get("sp_checkpoint") or {}
            if _cp and not _cp.get("listing_complete"):
                # PARTIAL, not finished. `count_inventory > 0` used to mean one thing — a run
                # that listed the WHOLE estate and died afterwards — and the resume below skips
                # the listing entirely on the strength of it. Per-site checkpoints break that
                # equivalence: rows now appear mid-listing, so without this branch a 30-site scan
                # that died at site 3 would resume by declaring three sites the whole estate and
                # publishing it. The marker is what distinguishes the two, and it is written by
                # the same call that records the last site.
                # WHAT EACH SITE HELD, not just which sites are done. A resumed site the
                # scanner reports `complete` beside the zero this attempt walked is a WRONG
                # number rather than a missing one — indistinguishable, to the reader, from a
                # site that genuinely held nothing, which is the one confusion the per-site
                # report exists to remove. The counts are in this scan's own inventory, so
                # reading them here costs one pass over rows already being read.
                _sp_resume_sites = {str(x): {"listed": 0, "estate": 0, "name": None}
                                    for x in (_cp.get("sites") or []) if x}
                _persisted_rows = core.store.list_inventory(scan_id)
                for r in _persisted_rows:
                    assessable = r.get("doc_class") not in (None, "unsupported", "media")
                    _sp_prior_assessable += 1 if assessable else 0
                    known = _sp_resume_sites.get(str(r.get("site_id") or ""))
                    if known is None:
                        continue
                    known["estate"] += 1
                    known["listed"] += 1 if assessable else 0
                    known["name"] = known["name"] or r.get("site_name")
                # Carried through init_scan_run below, which writes scope=EXCLUDED.scope and
                # would otherwise NULL it — losing attempt 1's sites on attempt 2, so a run that
                # died twice would re-walk everything the first attempt had already paid for.
                scope = {"sp_checkpoint": dict(_cp)}
                print(f"[scan] {scan_id}: resuming a partial SharePoint listing — "
                      f"{len(_sp_resume_sites)} site(s) already persisted "
                      f"({_existing_inv_count} rows), listing only what is left", flush=True)
            else:
                _checkpoint_resume = True

    if not _checkpoint_resume:
        # Create the scan_runs row NOW, before the file listing, so GET /scans/{id} returns a
        # result as soon as a worker claims the job.  Skipped on retry: the row already exists
        # and re-initing would reset status and scope to their discover-start defaults.
        core.store.init_scan_run(scan_id, source, 0, started, rb.name, rb.hash, owner=user,
                                 status="running",
                                 # Only ever set on a partial-listing resume; None on a first
                                 # attempt, which is the argument this call has always passed.
                                 scope=(scope or None) if _sp_resume_sites else None)

        # Claim the active-Discovery slot before listing. Two concurrent requests for the same
        # source will both reach init_scan_run (each with their own scan_id), but only one will
        # claim the guard — the second sees a non-None holder, fails the scan, and returns early.
        # Skipped on checkpoint_resume: the slot was already claimed on the first attempt.
        if user:
            _holder = core.store.acquire_discovery_guard(user, source, scan_id)
            if _holder is not None:
                _conflict_msg = (f"Discovery already active for source {source!r}: "
                                 f"scan {_holder!r} is still running")
                logger.warning("_scan_discover: %s (rejecting %s)", _conflict_msg, scan_id)
                core.store.set_scan_status(scan_id, "failed")
                core.store.log_decision("system", "scan.discover_conflict",
                                        scan_id=scan_id, detail=_conflict_msg)
                # Tell the JOB, not just the scan row. The queued poll settles on
                # scan_runs.status and does see the 'failed' above — but the SSE progress stream
                # and GET /scans/jobs/{id} read the job record, and returning here without
                # touching it left the last write standing: files_found=0, error=null, and a
                # phase that reads as a finished discovery. A rejection is then indistinguishable
                # from a source that really is empty, on the surface built to narrate the run.
                # Shape matches core.get_job's own stale-job terminal (phase/done/error).
                _cjid = job.get("id")
                if _cjid:
                    core.update_job(_cjid, {"phase": "error", "done": True,
                                            "error": _conflict_msg})
                # After set_scan_status('failed') above, never before it — the ADR's ordering
                # rule (test_discover_completion_race's lesson): an event a reader can act on
                # must not out-run the durable state it claims.
                scan_event(scan_id, "scan.failed", phase="error", job_id=_cjid,
                           attempt=job.get("attempts", 1), owner_email=user,
                           detail={"reason": "discovery_conflict", "holder": _holder})
                return

    if _checkpoint_resume:
        print(f"[scan] {scan_id}: retry detected — {_existing_inv_count} inventory rows already "
              f"persisted, skipping Drive API listing", flush=True)
        inv = core.store.list_inventory(scan_id)
        items = [r for r in inv if r.get("doc_class") not in (None, "unsupported", "media")]
        norm = items
        _enforce_scope_collapse_guard(scan_id, user, scope, items, inv)
        core.store.set_scan_files(scan_id, len(items))
        _jid = job.get("id")
        if _jid:
            core.update_job(_jid, {"files_found": _existing_inv_count,
                                   "phase": "lifecycle"})
    else:
        # ADR 0042. Deliberately NOT emitted on the _checkpoint_resume branch above: that path
        # skips the Drive listing entirely (the inventory is already persisted), so claiming a
        # listing started there would be a false statement about the estate having been re-read.
        # Its scan.claimed row plus the absent listing pair is already the honest record of a
        # resumed attempt.
        scan_event(scan_id, "scan.listing_started", phase="discovering", job_id=job.get("id"),
                   attempt=job.get("attempts", 1), owner_email=user,
                   detail={"source": source})
        # scope_files gates what is READ, not what is scored. This is the PRODUCTION listing path
        # (ADR 0007 fan-out); run_scan's is the local one, and wiring only that would leave a
        # hospital's PDFs being downloaded and OCR'd in the deployment that matters.
        # Emit live file counts during the listing so the frontend ticks up rather than showing 0
        # for the full duration. Throttled to one DB write every 2 s — the scanner does the timing
        # inside _search_drive/_search_folder; this callback just persists whatever count arrived.
        def _listing_progress(count: int, folders: int | None = None,
                               active: list | None = None, recent: list | None = None,
                               sites: list | None = None) -> None:
            try:
                core.store.set_scan_files(scan_id, count)
                _jid = job.get("id")
                if _jid:
                    # 'phase' matters as much as 'files_found' here: queuedProgress.js (the
                    # frontend's durable-path progress derivation) only trusts the live job state
                    # at all once job.phase is set to something other than 'queued' — omitting it
                    # meant every tick of this callback was silently discarded, and the checklist
                    # fell back to inferring phase from scan_runs.files (0 vs nonzero), which
                    # cannot distinguish listing from metadata/classification from lifecycle and
                    # so jumped straight from "Connected" to "Applying lifecycle rules" the
                    # instant _list() returned — found live 2026-08-26 from a user screenshot
                    # showing zero live detail through this entire phase. 'discovering' matches
                    # DiscoverRunProgress.jsx's PHASE_DONE_COUNT key for "Listing folders and
                    # files" — listing, metadata and classification are one combined operation in
                    # _list() today (the Drive/SharePoint list call already returns metadata, and
                    # classification runs inline per item), so this single phase value covers all
                    # three checklist rows' real backend execution window honestly.
                    patch = {"files_found": count, "phase": "discovering"}
                    # folders is None for the flat Drive-query listing path (_search_drive has
                    # no folder-tree concept) and a real live count for the folder-BFS path
                    # (_search_folder/_search_folders). Omit the key entirely rather than send
                    # folders_found: None, so a real count from an earlier tick or root is never
                    # clobbered by a later call that has none to report.
                    if folders is not None:
                        patch["folders_found"] = folders
                    # Folder-level activity (bounded — see scanner._search_folder's own comment):
                    # which folders the BFS is fetching RIGHT NOW, and the last few that finished.
                    # None for the flat Drive-query path, same "omit rather than clobber" rule as
                    # folders_found above. A frontend without this field yet (or a scan predating
                    # it) simply never sees `active`/`recent` — nothing downstream requires them.
                    # Per-SITE progress for a multi-site SharePoint run: which sites are done,
                    # which are still queued, which are blocked and why. Emitted as each site
                    # resolves rather than per file — a thirty-site walk is otherwise one silent
                    # bar, and "which site is it on, and did any fail?" is the question an
                    # operator watching a long estate scan actually has. Same "omit rather than
                    # clobber" rule as folders_found: a later tick with nothing to report must
                    # not blank a breakdown an earlier one produced.
                    if sites is not None:
                        patch["sites"] = sites
                    if active is not None:
                        patch["active_folders"] = active
                    if recent is not None:
                        patch["recent_folders"] = recent
                    core.update_job(_jid, patch)
            except JobCancelledError:
                # "A diagnostic must never fail the scan" is right about diagnostics and wrong
                # about this: cancellation is not a progress-reporting failure, it is the scan
                # being stopped, and it happens to be travelling through a progress tick. Caught
                # by the blanket clause below, a Stop observed here would be logged at DEBUG and
                # discarded, and the listing would carry on as though nothing had been asked.
                # This callback runs on the BFS thread, so the raise reaches the drain in
                # scanner._search_folder that turns "observed" into "stopped".
                raise
            except Exception:  # noqa: BLE001 — a diagnostic must never fail the scan
                logger.debug("_listing_progress: progress update failed", exc_info=True)

        # init_scan_run (above) already created the scan_runs row — status='running', scope=NULL —
        # before this listing runs, so GET /scans/{id} has something to return the instant the job
        # is claimed. If listing itself raises (expired Drive token, a transient API error, the
        # worker being killed), that row is exactly what's left behind: status stuck at 'running',
        # scope still NULL, files still 0. Nothing downstream — the frontend's discoveredCount
        # fallback, the "inventory could not be read" export copy, "discovery completion time not
        # recorded" — treats that as a failure; each reads it as a normal, if empty, scan. Flip the
        # row to 'failed' with the reason on the decision log before re-raising, so the run is
        # visibly broken rather than silently empty, and so a retry that lands on the SAME job
        # (worker.py's own retry/backoff) still sees `init_scan_run` reset status to 'running' on
        # its next attempt — this is a between-attempts marker, not a terminal one.
        # PRD Phase 3, interactive scans: reconstruct the estate from a per-user delta cursor
        # instead of walking the whole source — the same drive_delta seam #951 built for the
        # scheduled sweep, now also available to a user-initiated scan (#978 for Drive). Both
        # branches share one precondition: a whole-source request (no folder/folders for Drive —
        # Changes API has no folder filter of its own; for SharePoint, only the two
        # whole-single-drive shapes scanner._sp_whole_library_target recognises — Graph's delta
        # query is scoped to exactly one drive and has no folder filter either).
        #
        # #978/#981 originally also required incremental=true, on the theory that it was the
        # same "let ACP skip redundant work" switch as ADR 0011's cross-scan analysis reuse.
        # It is not: ADR 0011's reuse is invisible in the result (a skipped file still reports a
        # score, carried over from a prior run — why `incremental` defaults OFF in the UI,
        # App.jsx's `incremental` useState, since 2026-08-19). Delta-sync reconstruction has no
        # such risk — every file the reconstructed listing reports is verified against a
        # drive-scoped baseline (core._sp_prior_inventory_for_drive) and still gets a fresh
        # analysis; it only changes HOW the estate is enumerated, not what gets scored. Tying it
        # to the same flag meant it could never fire for a real interactive scan: the UI that
        # sets it removed its own toggle for this group the same day and never sends true. So
        # eligibility here is the whole-source shape check alone, independent of `incremental`.
        drive_delta = scheduled_context.get('delta_plan') if scheduled_context and source == 'drive' else None
        sp_delta = scheduled_context.get('delta_plan') if scheduled_context and source == 'sharepoint' else None
        sp_delta_plan = None
        if user and not scheduled_context:
            if source == "drive" and drive_token and not folder and not folders:
                drive_delta = core._interactive_drive_sync_plan(user, svc)
            elif source == "sharepoint" and sp_tok:
                from scanner import _sp_whole_library_target
                eligible, sp_drive_id = _sp_whole_library_target(folder, folders)
                if eligible:
                    sp_delta = core._interactive_sp_sync_plan(user, sp_tok, sp_drive_id)
                else:
                    # PHASE 3. `_sp_whole_library_target` answers only for the one shape a
                    # single-drive delta can serve — the whole of exactly one library. A SITE
                    # request covers several, and until now every one of them fell through to a
                    # complete re-walk on every scan: the case the incremental feature was built
                    # for and the only one a 30-site estate is ever in.
                    #
                    # The plan is per LIBRARY, so a site whose libraries are individually fresh,
                    # expired, never-synced and due-for-reconciliation gets the right answer for
                    # each instead of one answer for all of them.
                    sp_delta_plan = _sp_site_delta_plan(user, sp_tok, folder, folders)

        # ── PER-SITE CHECKPOINT (Phase 4) ────────────────────────────────────────────────────
        #
        # A thirty-site estate is a long listing, and until now it was also an ATOMIC one: the
        # inventory was persisted in a single write after the last site, so a run that died at
        # site 28 threw away twenty-eight sites' worth of Graph calls and started again at site
        # one. That is the failure mode a large tenant hits most, because it is the tenant whose
        # listing runs long enough to be interrupted.
        #
        # This persists each site AS IT FINISHES and records which sites are done. add_inventory
        # is idempotent per (scan_id, file), so a row written here and again by the end-of-listing
        # write is one row — which is what makes the checkpoint safe to be wrong about: the worst
        # case is redundant work, never a duplicate or a missing document.
        #
        # ONLY COMPLETE SITES ARRIVE HERE. The scanner does not emit a site the cap truncated or
        # a library that failed (see _sp_list's _emit_site), because `sites` below is what the
        # resume SKIPS — and skipping a half-listed site would publish the half as the whole.
        _sp_checkpoint_sites: list[str] = sorted(_sp_resume_sites)

        def _sp_site_done(site_id: str, site_files: list, site_inventory: list) -> None:
            rows = ([_discover_inventory_row(_discover_norm_row(it)) for it in (site_files or [])]
                    + list(site_inventory or []))
            from scanner import _dedupe_inventory_files
            _dedupe_inventory_files(rows)
            if rows:
                core.store.add_inventory(scan_id, rows)
            if site_id not in _sp_checkpoint_sites:
                _sp_checkpoint_sites.append(site_id)
            # Written AFTER add_inventory, never before. The marker is a claim that this site's
            # rows are durable; a resume trusts it enough to not walk the site again, so a marker
            # that outran its own rows would silently delete a site from the estate.
            core.store.merge_scan_scope(scan_id, {"sp_checkpoint": {
                "sites": list(_sp_checkpoint_sites), "listing_complete": False}})
            print(f"[scan] {scan_id}: SharePoint site {site_id} checkpointed "
                  f"({len(rows)} row(s)); {len(_sp_checkpoint_sites)} site(s) durable", flush=True)

        _sp_checkpointing = (source == "sharepoint" and defer
                             and _os.environ.get("ACP_SP_CHECKPOINT", "1").strip() != "0")
        try:
            items = _list(source, svc, folder=effective_folder, sp_token=sp_tok,
                          max_files=FANOUT_MAX_FILES, **({"folders": folders} if folders else {}),
                          **({"exclude_folders": exclude_folders} if exclude_folders else {}),
                          include_subfolders=include_subfolders,
                          exclude_remediated=bool(payload.get("exclude_remediated", False)),
                          scope_out=scope, scope_files=_scope_for_listing(user), inventory_out=inventory,
                          progress_cb=_listing_progress, drive_delta=drive_delta, sp_delta=sp_delta,
                          **({"sp_delta_plan": sp_delta_plan} if sp_delta_plan else {}),
                          **({"sp_site_done": _sp_site_done} if _sp_checkpointing else {}),
                          **({"sp_skip_sites": _sp_resume_sites} if _sp_resume_sites else {}))
        except JobCancelledError:
            # A user pressed Stop. Before this clause existed the blanket handler below caught it
            # and recorded scan_runs.status='failed' with a 'listing_failed' event — so the one
            # outcome the user themselves asked for was the one the product reported as a fault,
            # and the run showed up in dead-letter diagnostics as a Drive listing error.
            #
            # Reached only after scanner._search_folder's drain has returned, so by here the
            # discovery pool is shut down and joined: no folder fetch can still be running or
            # writing. That is what makes 'cancelled' honest rather than optimistic — it is the
            # STOPPED state, not merely the observed one.
            #
            # A SHAREPOINT RUN MAY LEAVE ROWS BEHIND, and that is new. `items` is still never
            # assigned and every write below this raise is still skipped, but the per-site
            # checkpoint above has already persisted each site that finished before the Stop.
            # They stay: they are real, correctly attributed rows of a run the store marks
            # 'cancelled', never published (mark_published is downstream of this raise and gated
            # on a complete enumeration) and so never a collapse baseline. What they DO become is
            # a suspicious-zero baseline for a later scan of the same source, which is the
            # conservative direction — a subsequent zero is refused rather than published.
            try:
                core.store.set_scan_status(scan_id, "cancelled")
                core.store.log_decision("system", "scan.discover_cancelled", scan_id=scan_id,
                                        detail="cancelled during listing")
                if user:
                    core.store.release_discovery_guard(scan_id)
            except Exception:
                swallowed("_scan_discover: recording the cancelled listing (status, decision, guard) failed", scan_id)
            scan_event(scan_id, "scan.cancelled", phase="cancelled", job_id=job.get("id"),
                       attempt=job.get("attempts", 1), owner_email=user,
                       detail={"reason": "cancel_requested", "source": source,
                               "stopped_during": "listing"})
            # Re-raised as JobCancelledError, NOT converted: worker.run_once catches this exact
            # type and records the job 'cancelled'. Letting it reach the generic Exception path
            # instead would consume a retry and re-run the listing the user just stopped.
            raise
        except Exception as e:
            try:
                core.store.set_scan_status(scan_id, "failed")
                core.store.log_decision("system", "scan.discover_failed", scan_id=scan_id,
                                        detail=f"listing {source} failed: {e}")
                if user:
                    core.store.release_discovery_guard(scan_id)
            except Exception:
                swallowed("_scan_discover: recording the failed listing (status, decision, guard) failed", scan_id)
            # Inside the same best-effort block's shadow but outside its try, so a failure here
            # cannot swallow the re-raise below: scan_event never raises, and the original
            # exception must still propagate to the worker's retry policy.
            scan_event(scan_id, "scan.failed", phase="error", job_id=job.get("id"),
                       attempt=job.get("attempts", 1), owner_email=user,
                       detail={"reason": "listing_failed", "source": source,
                               "message": str(e)[:200]})
            raise
    # shadow_candidate (a file sharing a logical name with another — possibly ACP's own output
    # shadowing its source) is computed inside _enqueue_analysis from the item list, so the same
    # rule applies whether the fan-out runs now or later at Assess.
    _exclude_rem = bool(payload.get("exclude_remediated", False))

    incremental = bool(payload.get("incremental", True))
    if not _checkpoint_resume:
        try:
            scope["scope_rules"] = [
                {k: r.get(k) for k in ("rule_id", "selector", "value", "codes",
                                       "priority", "is_override", "enabled")}
                for r in core.store.list_scope_rules(enabled_only=True)
            ]
        except Exception:
            logger.warning("_scan_discover: failed to load scope_rules for %s", scan_id, exc_info=True)
            scope["scope_rules"] = []

        # Enumeration-completeness flag (#2): persist structured evidence of whether the listing
        # covered the entire estate. Stored in scope["enumeration"] and persisted via
        # merge_scan_scope below so that downstream callers (frontend, suspicious-zero check,
        # published-snapshot gate) can distinguish a verifiably complete listing from a truncated
        # or partial one without re-deriving it from scattered scope fields.
        _truncated = bool(scope.get("truncated", False))
        scope["enumeration"] = {
            "complete": not _truncated,
            # Measured on the zero path below, not asserted here. This used to read
            # `"auth_ok": True,  # _list() returned without raising — auth was valid`, which is
            # the one inference the silent zero defeats: a folder the token cannot see returns an
            # empty page with a 200 and raises nothing, so the flag recorded auth_ok=True for
            # exactly the case it exists to catch. None means "not probed" — a non-empty listing
            # proved it read the source by returning files, so there is nothing to establish.
            "auth_ok": None,
            "files_found": len(items),
            "truncated": _truncated,
            "folders_visited": scope.get("folders"),  # None for flat Drive path
            # How many folder subtrees scanner._search_folder skipped after Drive rate-limited
            # (exhausted-retries) requests to them — 0/None for the flat Drive-query path, which
            # has no per-subtree concept. Real count of a real, already-caught exception; not a
            # new failure mode, just one that was previously invisible past a server print line.
            "skipped_rate_limited": scope.get("skipped_rate_limited"),
        }
        if items:
            scope["enumeration"]["auth_ok"] = True

        # Suspicious-zero protection (#8): if _list() succeeded but returned 0 files, and the
        # previous scan for this source found files, the zero is likely a transient API failure
        # (expired token, quota, scope narrowing) rather than a genuinely empty estate. Failing
        # loudly here avoids publishing an empty inventory that overwrites a real one. Skipped
        # when the listing was truncated (truncated means large estate, not empty) and when the
        # source is new (no previous scan → legitimate first run can return 0).
        # `not _existing_inv_count` because a RESUMED listing only covers the sites the previous
        # attempt did not reach, and "the remaining two sites held nothing assessable" is not the
        # silent zero this whole block exists to refuse. Without it a resume that finished an
        # estate whose tail happens to be media would be failed as a suspicious zero, against a
        # baseline that includes the twenty-eight sites already sitting in its own inventory.
        if not items and not _truncated and not _existing_inv_count:
            _first_scan = True   # updated below once we know
            _baseline_id = None  # updated below once we know; initialized here so the
                                 # except block can test it safely even if the try raises
                                 # before the assignment at last_nonempty_run_for_source.
            try:
                # TWO QUESTIONS, TWO LOOKUPS. They read as one — "what came before?" — and a
                # single answer cannot serve both without breaking one of them.
                #
                # "Has this source EVER been scanned?" gates the first-scan retry below, and any
                # prior run answers it, empty or not. A source that is genuinely empty has prior
                # runs with no inventory; asking the non-empty question here would call it a first
                # scan forever and re-list it after a 5s sleep on every single scan.
                _prev_scan_id = core.store.previous_run_for_source(scan_id, owner=user)
                _first_scan = _prev_scan_id is None
                # "Did this source ever PROVE it had files?" is the guard baseline, and only a run
                # with inventory answers it. previous_run_for_source excludes just 'superseded', so
                # once this guard fails a scan, that failed run — 0 inventory rows — becomes the
                # previous one: the retry the guard explicitly invites (it releases the slot below)
                # then compared against 0, saw nothing suspicious, and published the zero the first
                # attempt had refused. Skipping to the last run that proved files exist makes the
                # check idempotent — attempt 2 and attempt 20 compare against the same inventory.
                _baseline_id = core.store.last_nonempty_run_for_source(scan_id, owner=user)
                if _baseline_id:
                    _prev_count = core.store.count_inventory(_baseline_id)
                    if _prev_count > 0:
                        _msg = (f"listing returned 0 files but previous scan {_baseline_id} "
                                f"found {_prev_count}; refusing to publish suspicious zero")
                        logger.error("_scan_discover: suspicious zero for %s: %s", scan_id, _msg)
                        core.store.set_scan_status(scan_id, "failed")
                        core.store.log_decision("system", "scan.suspicious_zero",
                                                scan_id=scan_id, detail=_msg)
                        try:
                            core.store.release_discovery_guard(scan_id)
                        except Exception:
                            logger.warning("_scan_discover: could not release guard on suspicious zero for %s",
                                           scan_id, exc_info=True)
                        raise RuntimeError(_msg)
            except RuntimeError:
                raise
            except Exception:
                logger.warning("_scan_discover: suspicious-zero check failed for %s — proceeding",
                               scan_id, exc_info=True)
                if _baseline_id is not None:
                    # We found a prior non-empty scan but count_inventory failed (transient DB
                    # error). Don't proceed — a DB error must not silently defeat the guard.
                    core.store.set_scan_status(scan_id, "failed")
                    try:
                        core.store.release_discovery_guard(scan_id)
                    except Exception:
                        logger.warning("_scan_discover: could not release guard for %s on baseline-count failure",
                                       scan_id, exc_info=True)
                    raise RuntimeError(
                        f"suspicious-zero check for {scan_id} could not verify baseline "
                        f"{_baseline_id!r} — refusing to publish zero with unverified count"
                    )
                _first_scan = False  # can't determine; skip retry
            # No proven non-empty baseline: retry once after a short delay.
            # Covers two cases: (a) first-ever scan — genuine empty Drive is
            # indistinguishable from a transient API hiccup; (b) all prior scans for
            # this source also returned 0 — _first_scan is False but _baseline_id is
            # None, so neither the guard above nor the old _first_scan check would fire,
            # leaving the zero silent on every subsequent attempt. Retrying once on any
            # zero-with-no-baseline is cheap and closes the stuck-at-zero loop.
            if not _baseline_id:
                import time as _time
                # Before the sleep AND before the re-listing. This retry is the one place the
                # discover handler starts substantial NEW work on its own initiative, so it is
                # the one place "stop scheduling new tasks after cancellation" has real content:
                # without this a Stop arriving here bought the user a 5s sleep followed by a
                # complete second walk of the estate they had just stopped.
                check_cancel()
                logger.info("_scan_discover: no non-empty baseline for %s, 0 files returned; retrying once in 5s", scan_id)
                _time.sleep(5)
                check_cancel()   # again after the sleep — 5s is long enough to be stopped inside
                _retry_scope: dict = {}
                _retry_inv: list = []
                try:
                    _retry_items = _list(
                        source, svc, folder=effective_folder, sp_token=sp_tok,
                        max_files=FANOUT_MAX_FILES,
                        **({"folders": folders} if folders else {}),
                        **({"exclude_folders": exclude_folders} if exclude_folders else {}),
                        include_subfolders=include_subfolders,
                        exclude_remediated=bool(payload.get("exclude_remediated", False)),
                        scope_out=_retry_scope, scope_files=_scope_for_listing(user),
                        inventory_out=_retry_inv, progress_cb=_listing_progress,
                    )
                    if _retry_items:
                        logger.info("_scan_discover: retry returned %d files for %s",
                                    len(_retry_items), scan_id)
                        items = _retry_items
                        scope.update(_retry_scope)
                        inventory[:] = _retry_inv
                except JobCancelledError:
                    # The only clause here that does not swallow. "first-scan retry failed" is
                    # the right reading of a Drive error — the zero stands and the handler goes
                    # on to check reachability — and exactly the wrong one for a Stop: it would
                    # discard the cancellation and let the run proceed to record an empty estate
                    # as fact, on a scan the user had already stopped.
                    raise
                except Exception:
                    logger.warning("_scan_discover: first-scan retry failed for %s",
                                   scan_id, exc_info=True)

            # Still nothing. Before recording an empty estate as fact, establish that the source
            # was actually READ — the listing returning cleanly does not establish it, which is
            # what makes this class of zero silent. Runs last, after the baseline check and after
            # the retry above has had its chance, so a transient blip is not reported as a
            # permissions problem; and only on the zero path, so a normal scan pays nothing.
            #
            # This is also the only signal that speaks to a FIRST scan of a source. The retry
            # above accepts a second zero because "there is no baseline to refuse against" — true
            # of history, but the root is checkable right now, and an unreachable root means the
            # zero is not evidence of an empty estate no matter how new the source is.
            if not items:
                _reach_ok, _reach_why = _roots_reachable(
                    source, svc,
                    (folders or ([folder] if folder else None)),
                    sp_tok,
                    scope.get("path"),   # scanner sets this for source=local (the corpus dir)
                )
                scope.setdefault("enumeration", {})["auth_ok"] = _reach_ok
                if not _reach_ok:
                    _msg = (f"listing returned 0 files and the source could not be read "
                            f"({_reach_why}); refusing to publish an unverified empty estate")
                    logger.error("_scan_discover: unreachable source for %s: %s", scan_id, _msg)
                    core.store.set_scan_status(scan_id, "failed")
                    core.store.log_decision("system", "scan.unreachable_zero",
                                            scan_id=scan_id, detail=_msg)
                    core.store.merge_scan_scope(scan_id, scope)
                    try:
                        core.store.release_discovery_guard(scan_id)
                    except Exception:
                        logger.warning("_scan_discover: could not release guard on unreachable "
                                       "zero for %s", scan_id, exc_info=True)
                    # Deliberately NOT marking the job done/error here, unlike the conflict path
                    # above. This raises, which hands the job to the worker's retry-and-backoff
                    # machinery; stamping done=True first would tell the SSE stream the run had
                    # ended while attempts were still pending. The conflict path returns cleanly
                    # and owns its job state precisely because nothing else will touch it.
                    raise RuntimeError(_msg)

        # THE DELTA POSITION THIS SCAN LISTED FROM, recorded on the scan's own scope so freshness
        # is answerable later. Without it, "has SharePoint changed since this scan?" can only be
        # asked as "since the LAST sync", which is a different question the moment a second scan
        # runs — and the answer to the wrong question is indistinguishable from the answer to the
        # right one.
        #
        # In the scope blob rather than a new column: the scope is already persisted per scan
        # (merge_scan_scope, below), already the place a run records what it covered, and a
        # migration for a per-scan JSON fact would be a schema change bought for nothing.
        if source == "sharepoint" and sp_delta_plan:
            scope["sp_cursors"] = _sp_scan_cursors(user, sp_delta_plan)
        # THE LISTING IS OVER. Recorded before anything reads the checkpoint back, and recorded
        # even when no site was checkpointed, because its absence is what a resume treats as "the
        # whole estate is here" — see the resume branch at the top of this function.
        if _sp_checkpointing:
            scope["sp_checkpoint"] = {"sites": list(_sp_checkpoint_sites),
                                      "listing_complete": True}
        _enforce_scope_collapse_guard(scan_id, user, scope, items, inventory,
                                      prior_count=_existing_inv_count)
        core.store.set_scan_files(scan_id, len(items) + _sp_prior_assessable)
        core.store.merge_scan_scope(scan_id, scope)
        # ADR 0042, ordered AFTER both durable writes above rather than after _list() returned:
        # the count and the enumeration evidence are what this event asserts, so it must not be
        # readable before they are. `truncated` rides along because "listed 5,000 files" and
        # "listed the first 5,000 of an unknown number" are different facts about the estate, and
        # the log is the one place that distinction survives the run.
        _enum = scope.get("enumeration") or {}
        scan_event(scan_id, "scan.listing_complete", phase="discovering", job_id=job.get("id"),
                   attempt=job.get("attempts", 1), owner_email=user,
                   detail={"files_found": len(items),
                           "folders_visited": _enum.get("folders_visited"),
                           "truncated": bool(_enum.get("truncated")),
                           "complete": bool(_enum.get("complete"))})
        norm = [_discover_norm_row(it) for it in items]
    if defer:
        if not _checkpoint_resume:
            from scanner import _dedupe_inventory_files
            inv = [_discover_inventory_row(it) for it in norm] + inventory
            _dedupe_inventory_files(inv)
            if inv:
                _job_id = job.get("id")
                if _job_id:
                    # Emit "saving" before the call so the frontend's checklist step goes
                    # active for this window instead of silently vanishing into "listing" —
                    # found live 2026-08-26: the save always ran here, before lifecycle rules,
                    # but the UI listed "Saving inventory" AFTER "Applying lifecycle rules" and
                    # never gave it a live phase at all, so it just flipped to done at the same
                    # instant as everything else. No per-item ticks (add_inventory is one bulk
                    # write, not a loop this callback can hook into) — just a real start/end
                    # window and the outcome counts below.
                    core.update_job(_job_id, {"phase": "saving"})
                _inv_outcome = core.store.add_inventory(scan_id, inv)
                # Persist the new/updated/unchanged delta so the completion card can show it
                # even after the job is gone. merge_scan_scope is a read-modify-write on the
                # scope JSON column, so all other scope keys (inventory, scan_scope, …) survive.
                try:
                    core.store.merge_scan_scope(scan_id, {
                        "inventory_delta": {
                            "new": _inv_outcome.get("new", 0),
                            "updated": _inv_outcome.get("updated", 0),
                            "unchanged": _inv_outcome.get("unchanged", 0),
                        }
                    })
                except Exception:
                    swallowed("_scan_discover: merging the inventory delta into the scan scope failed", scan_id)
                if _job_id:
                    core.update_job(_job_id, {
                        "schema_version": 2,
                        "save_new": _inv_outcome.get("new"),
                        "save_updated": _inv_outcome.get("updated"),
                        "save_unchanged": _inv_outcome.get("unchanged"),
                        "save_failed": _inv_outcome.get("failed"),
                    })
                # ADR 0042. THIS is the site test_discover_completion_race.py is about: the rows
                # are in scan_inventory before anything says so. #934's bug was a status flip
                # that raced this same write, and an event is read exactly like a status — so it
                # is appended after add_inventory returns, never beside the "saving" phase write
                # that precedes it.
                scan_event(scan_id, "scan.inventory_saved", phase="saving", job_id=_job_id,
                           attempt=job.get("attempts", 1), owner_email=user,
                           detail={"new": _inv_outcome.get("new"),
                                   "updated": _inv_outcome.get("updated"),
                                   "unchanged": _inv_outcome.get("unchanged"),
                                   "failed": _inv_outcome.get("failed")})
        if _sp_resume_sites:
            # THE ESTATE IS THE STORE'S, not this attempt's. Everything below — the lifecycle
            # denominator, the assessable/empty decision, the discovered event's count, the
            # decision log — reads `items` and `inv`, and on a resumed run those hold only the
            # sites this attempt had left to list. A 30-site scan that finished its last two
            # sites here would otherwise close as a two-site estate and, if those two held
            # nothing assessable, finalize instead of offering Assess over the other twenty-eight.
            #
            # Read back rather than concatenated: the rows this attempt just wrote and the rows
            # attempt 1 wrote are the same table, and re-deriving from it is the only version of
            # this that cannot double-count a site both attempts happened to touch.
            inv = core.store.list_inventory(scan_id)
            items = [r for r in inv
                     if r.get("doc_class") not in (None, "unsupported", "media")]
            norm = items
            core.store.set_scan_files(scan_id, len(items))

        # Phase B4 — with the inventory persisted, run enabled disposition rules against it and
        # record candidate lifecycle outcomes (never executing the Drive move/delete here). Runs
        # before the no-assessable-items short-circuit below because a rule may match a
        # non-scannable estate row (old media to archive, a /tmp file to flag for deletion).
        # Emit "lifecycle" phase before the call so the frontend step goes active immediately,
        # then pass a progress_cb so live ticks update the KPI in real time.
        _jid = job.get("id")
        _lifecycle_total = len(inv)
        if _jid and _lifecycle_total > 0:
            core.update_job(_jid, {"phase": "lifecycle",
                                   "files_found": _lifecycle_total,
                                   "files_evaluated": 0,
                                   "rules_enabled": 0,
                                   "files_matched": 0,
                                   "archive_candidates": 0,
                                   "delete_candidates": 0,
                                   "files_tagged": 0,
                                   "unevaluable": 0})
        # Fire roughly 100 ticks regardless of inventory size (min 10 files/tick).
        _tick_every = max(10, _lifecycle_total // 100)
        def _lc_progress(stats):
            try:
                if _jid:
                    core.update_job(_jid, {"phase": "lifecycle",
                                           "files_found": _lifecycle_total,
                                           **stats})
            except Exception:
                swallowed("_scan_discover/_lc_progress: updating lifecycle job progress failed", scan_id)
        _lc_stats = _evaluate_discover_lifecycle_rules(
            scan_id, source, user, progress_cb=_lc_progress, tick_every=_tick_every)
        # ADR 0042 — after the evaluator has written its outcomes, not beside the "lifecycle"
        # phase tick that opened the window. One event for the whole pass: the per-file ticks
        # (~100 of them) are exactly the high-frequency progress this log does not carry.
        scan_event(scan_id, "scan.lifecycle_applied", phase="lifecycle", job_id=_jid,
                   attempt=job.get("attempts", 1), owner_email=user,
                   detail={"rules_enabled": _lc_stats.get("rules_enabled", 0),
                           "files_evaluated": _lc_stats.get("files_evaluated", 0),
                           "matches": _lc_stats.get("lifecycle_matches", 0),
                           "archive": _lc_stats.get("lifecycle_archive", 0),
                           "delete": _lc_stats.get("lifecycle_delete", 0),
                           "tagged": _lc_stats.get("lifecycle_tagged", 0),
                           "unevaluable": _lc_stats.get("lifecycle_errors", 0)})
        # Final tick with true totals so the KPI reflects the last batch before the phase ends.
        if _jid and _lifecycle_total > 0:
            try:
                core.update_job(_jid, {"phase": "lifecycle",
                                       "files_found": _lifecycle_total,
                                       "files_evaluated": _lc_stats.get("files_evaluated", _lifecycle_total),
                                       "rules_enabled": _lc_stats.get("rules_enabled", 0),
                                       "files_matched": _lc_stats.get("lifecycle_matches", 0),
                                       "archive_candidates": _lc_stats.get("lifecycle_archive", 0),
                                       "delete_candidates": _lc_stats.get("lifecycle_delete", 0),
                                       "files_tagged": _lc_stats.get("lifecycle_tagged", 0),
                                       "unevaluable": _lc_stats.get("lifecycle_errors", 0)})
            except Exception:
                swallowed("_scan_discover: updating the job with lifecycle totals failed", scan_id)
        # Transition the job progress to "done" so DiscoverRunProgress shows the completion
        # summary (with lifecycle breakdown) before the frontend detects scan status="discovered"
        # and navigates away. Uses the lifecycle_* naming the completion summary reads.
        if _jid:
            try:
                core.update_job(_jid, {
                    "schema_version": 2,
                    "phase": "done",
                    "rules_enabled": _lc_stats.get("rules_enabled", 0),
                    "lifecycle_matches": _lc_stats.get("lifecycle_matches", 0),
                    "lifecycle_archive": _lc_stats.get("lifecycle_archive", 0),
                    "lifecycle_delete": _lc_stats.get("lifecycle_delete", 0),
                    "lifecycle_tagged": _lc_stats.get("lifecycle_tagged", 0),
                    "lifecycle_unevaluable": _lc_stats.get("lifecycle_errors", 0),
                })
            except Exception:
                swallowed("_scan_discover: marking the lifecycle job phase done failed", scan_id)
        # Persist lifecycle stats alongside inventory_delta so the completion card shows them
        # correctly after a page reload (job state in Redis is ephemeral).
        try:
            core.store.merge_scan_scope(scan_id, {
                "lifecycle_rules_enabled": _lc_stats.get("rules_enabled", 0),
                "lifecycle_archive": _lc_stats.get("lifecycle_archive", 0),
                "lifecycle_delete": _lc_stats.get("lifecycle_delete", 0),
                "lifecycle_tagged": _lc_stats.get("lifecycle_tagged", 0),
            })
        except Exception:
            swallowed("_scan_discover: merging the lifecycle counters into the scan scope failed", scan_id)
        # THIS is where an ADR 0020 run's discovery ends — the estate is listed, the inventory is
        # persisted, the lifecycle rules have run, and nothing further happens until somebody
        # triggers Assess. The run stays at status='discovered' with completed_at NULL, possibly
        # forever, so without this stamp there is no record of when its inventory was taken and every
        # count rendered from that inventory is a snapshot with no date. Set-once (see the store),
        # so a re-delivered discover job does not move it.
        _mark_discovered(scan_id)
        scan_event(scan_id, "scan.discovered", phase="done", job_id=_jid,
                   attempt=job.get("attempts", 1), owner_email=user,
                   detail={"files_found": len(items), "source": source})
        # Publish the snapshot only when enumeration was verifiably complete. A truncated
        # listing (FANOUT_MAX_FILES hit) is an incomplete snapshot; a suspicious zero would
        # have raised above and never reached this line. Published scans are preferred by
        # scan selection so the frontend never presents an incomplete estate as the current
        # truth. The stamp is set-once; a re-delivery of the same job is a no-op.
        #
        # ON A CHECKPOINT RESUME, READ THE FLAG BACK RATHER THAN SKIPPING THE STAMP. The resume
        # path skips the listing, so the local `scope` never gets an `enumeration` block and this
        # gate could only ever be false — meaning a scan that crashed once, resumed, and finished
        # perfectly stayed unpublished forever. That made a missing published_at ambiguous
        # ("incomplete" OR "merely retried"), and an ambiguous flag cannot be read as evidence:
        # anything falling back to the last published snapshot would skip good scans, so nothing
        # could safely consume it. Attempt 1 persisted the enumeration via merge_scan_scope before
        # it died — a resume only happens when inventory rows exist, which is written after that —
        # so the answer is already in the store. Reading it makes published_at mean exactly one
        # thing: enumeration was verifiably complete, however many attempts it took.
        _enum = scope.get("enumeration") or {}
        if _checkpoint_resume and not _enum:
            try:
                _enum = (((core.store.get_scan(scan_id, owner=user) or {})
                          .get("run") or {}).get("scope") or {}).get("enumeration") or {}
            except Exception:
                logger.warning("_scan_discover: could not read persisted enumeration for %s",
                               scan_id, exc_info=True)
                _enum = {}
        if _enum.get("complete"):
            try:
                core.store.mark_published(scan_id)
            except Exception:
                logger.warning("_scan_discover: failed to mark published for %s",
                               scan_id, exc_info=True)
        if not items:
            # The estate is inventoried, but nothing in it is assessable — close the run rather
            # than leave it waiting for an Assess that would enqueue zero files.
            core.store.enqueue_job("scan_finalize",
                                   {"scan_id": scan_id, "source": source, "ai": ai, "pii": pii}, scan_id=scan_id)
        else:
            core.store.set_setting(f"assess_params:{scan_id}", _json.dumps(
                {"source": source, "ai": ai, "pii": pii, "incremental": incremental,
                 "exclude_remediated": _exclude_rem, "batch": bool(payload.get("batch"))}))
            core.store.log_decision("system", "scan.discovered", scan_id=scan_id,
                                    detail=f"{len(inv)} file(s) inventoried from metadata (no file opened) — "
                                           f"{len(items)} assessable, awaiting Assess")
        # Discover-phase tracing (lf.discover_run_trace). Until this call, an ADR 0020
        # Discover-only run emitted NOTHING to Langfuse: the "Discover" span lives on the analyse
        # path, which under this ADR runs at Assess time, so the phase that lists the estate and
        # evaluates the lifecycle rules was invisible — and the inventoried-but-never-assessed
        # rows were invisible permanently, since nothing later opens them.
        #
        # Emitted AFTER the inventory is persisted and the rules have run, so the trace describes
        # what actually happened rather than what was about to be attempted, and wrapped because
        # a tracing failure must never lose an inventory that is already written.
        try:
            import lf as _lf                      # module-local, as every other _lf site here is
            _spans = _lf.discover_file_spans(scan_id, inv, user=user)
            _lf.discover_run_trace(scan_id, source, listed=len(norm), inventoried=len(inv),
                                   scope=scope, user=user, file_spans_emitted=_spans)
            _lf.flush()
        except Exception:
            logger.debug("_scan_discover: Langfuse tracing failed for %s", scan_id, exc_info=True)
        # The durable STATUS flips to "discovered" LAST — after every other write this function
        # makes (inventory, lifecycle stats, discovered_at, published flag, assess_params/finalize
        # dispatch, tracing). It used to flip right after listing, long before any of that. Found
        # live 2026-08-28: two independent readers key off exactly this status — the frontend's
        # poll loop, which clears `busy` the instant status != "running" and renders
        # DiscoverCompleteSummary, and POST /scans/{sid}/assess's deferred_pending check, which
        # requires status == "discovered" AND count_inventory(sid) > 0 AND assess_params:{sid} set.
        # A reader in the old window could see a scan that looked complete with an empty
        # scan_inventory table and un-evaluated lifecycle rules — the Discover card showed
        # "0 files inventoried" that silently self-corrected once the real writes landed — and a
        # client hitting /assess in that window fell through to the wrong (immediate-model) branch
        # instead of the deferred one. Putting the flip last means no reader can ever observe
        # "discovered" with any of this still unwritten — a status can lag behind what's true, but
        # what it says has to already be true when it's said.
        core.store.set_scan_status(scan_id, "discovered")
        # Discovery is complete and independent at this point. Holding this source slot until
        # Assess finishes blocks later inventory runs even though no listing is still active.
        # Release only after the durable discovered state is visible; acquire_discovery_guard
        # also reconciles from discovered_at if the worker dies between these two writes.
        try:
            core.store.release_discovery_guard(scan_id)
        except Exception:
            logger.warning("_scan_discover: failed to release discovery guard for %s",
                           scan_id, exc_info=True)
        return
    if not items:
        core.store.enqueue_job("scan_finalize",
                               {"scan_id": scan_id, "source": source, "ai": ai, "pii": pii}, scan_id=scan_id)
        return
    # Per-folder fan-out path (ADR 0004 item 6): when ACP_PER_FOLDER_SCAN_JOBS is enabled,
    # list the immediate subfolders and emit one scan_folder job per folder instead of one
    # scan_file/scan_batch per file. This enables mid-scan resume at folder granularity and
    # parallel processing across folders. Falls back to the file-level path when no folders
    # are found (flat estate, local source, or SP where folder listing is not yet wired).
    if _per_folder_mode():
        toks = core.get_scan_tokens(scan_id)
        folders = _list_top_level_folders(source, effective_folder, toks)
        if folders:
            core.store.set_total_folders(scan_id, len(folders))
            for f in folders:
                core.store.enqueue_job("scan_folder", {
                    "scan_id": scan_id,
                    "folder_id": f["folder_id"],
                    "source": source,
                    "ai": ai,
                    "pii": pii,
                    "user": user,
                }, scan_id=scan_id)
            return
    # Immediate path (default today): fan out the analysis now. ADR 0008 batches large estates.
    _enqueue_analysis(scan_id, source, norm, ai=ai, pii=pii, user=user,
                      incremental=incremental, exclude_remediated=_exclude_rem,
                      force_batch=bool(payload.get("batch")))


@handler("scan_assess")
def _scan_assess(payload: dict, job: dict) -> None:
    """ADR 0020 — begin the ASSESS phase for a discovered scan: rebuild the download+analyse
    fan-out from the persisted inventory + scan params, flip the run back to 'running', and let
    the existing per-file jobs + finalize trigger take over. Idempotent: if file_records already
    exist (a prior assess ran), re-enqueuing is harmless (save_file_result upserts)."""
    scan_id = payload.get("scan_id") or job.get("scan_id")
    user = payload.get("user")
    core.store.reconcile_source_lifecycle(scan_id)
    inv = core.store.list_inventory(scan_id)
    try:
        params = _json.loads(core.store.get_setting(f"assess_params:{scan_id}") or "{}")
    except Exception:
        params = {}
    source = params.get("source", payload.get("source", "drive"))
    from scheduled_scan_execution import context_for_job
    scheduled_context = context_for_job(core.store, scan_id, job, source=payload.get('source', source), user=user)
    if scheduled_context:
        source = scheduled_context['source']
        params = dict((core.store.get_scan_inputs(scan_id) or {}).get('scan_options') or {}, source=source)
    ai = bool(params.get("ai", True)) and core.store.get_ai_enabled()
    pii = bool(params.get("pii", False))
    incremental = bool(params.get("incremental", True))
    exclude_rem = bool(params.get("exclude_remediated", False))
    # Phase C3 (PRD §4.5) — by default Assess skips inventory rows a lifecycle rule flagged for
    # archive/deletion (LIFECYCLE_EXCLUDED_DEFAULT). include_lifecycle_flagged is the authorized
    # override; it reaches here only through the assess route, which already gates on the scan
    # owner (get_scan owner=...), so being present in the payload is the owner-gate.
    include_flagged = bool(payload.get("include_lifecycle_flagged")
                           or params.get("include_lifecycle_flagged"))
    if scheduled_context:
        include_flagged = False
    core.store.set_scan_status(scan_id, "running")
    # CRITICAL — the inventory now records the WHOLE estate, including media / unsupported /
    # extensionless files that must NEVER be downloaded or analysed. Rebuild the fan-out from the
    # ASSESSABLE rows only (a supported doc format with at least one applicable WCAG test); the
    # rest stay inventory-only. Estate capability is re-derived from name + real MIME so the gate
    # holds regardless of what doc_class label a row happens to carry.
    import estate_inventory as _est
    from lifecycle_identity import TERMINAL
    from scanner import EXPORT_MAP as _EXPORT_MAP
    items = []
    # ── What the lifecycle rules held back, COUNTED WHERE THE HOLDING BACK HAPPENS ────────────
    # These three are the run's own record of its lifecycle exclusion, persisted onto scope below.
    # Counted here, at the decision, rather than re-derived afterwards from the inventory: this is
    # the same discipline `skipped_out_of_scope` follows in scanner._list — the count is made by
    # the code that did the dropping, so it cannot disagree with what the run actually enqueued.
    #
    #   lifecycle_flagged   every flagged file, ANY format — the estate-wide fact.
    #   eligible_excluded   the ASSESSABLE subset actually held back. Disjoint from the
    #                       "no test exists" population by construction, because it is counted
    #                       only after the assessable gate above has already passed.
    #   overridden          flagged files an authorized override assessed anyway. They ARE
    #                       assessed, so they belong to the assessed bucket, never to a sixth one.
    lifecycle_flagged = 0
    eligible_excluded = 0
    overridden = 0
    for r in inv:
        lc = r.get("lifecycle_status")
        flagged = lc in core.store.LIFECYCLE_EXCLUDED_DEFAULT
        if flagged:
            # Counted BEFORE the assessable gate: a flagged .png was never assessable, but it is
            # still a file a lifecycle rule flagged, and the estate-wide total says so.
            lifecycle_flagged += 1
        if _est.classify({"name": r.get("file"), "mimeType": r.get("mime")})["status"] != _est.ASSESSABLE:
            continue
        # Phase C3 (PRD §4.5) — a file a lifecycle rule flagged for archive/deletion is excluded
        # from Assess by default. Either way the assess record retains the lifecycle status + the
        # exclusion reason that applied when this run was created (status/rule/reason preserved).
        if flagged:
            base = r.get("lifecycle_reason")
            if include_flagged and lc not in TERMINAL:
                excl = (f"included in Assess despite lifecycle status '{lc}' (authorized override)"
                        + (f" — {base}" if base else ""))
                core.store.set_lifecycle_status(scan_id, r["file"], lc,
                                                rule_id=r.get("lifecycle_rule_id"), reason=base,
                                                exclusion_reason=excl)
                overridden += 1
            else:
                excl = (f"excluded from Assess: lifecycle status '{lc}'"
                        + (' — verified provider restoration is required before assessment' if lc in TERMINAL else '')
                        + (f" — {base}" if base else ""))
                core.store.set_lifecycle_status(scan_id, r["file"], lc,
                                                rule_id=r.get("lifecycle_rule_id"), reason=base,
                                                exclusion_reason=excl)
                eligible_excluded += 1
                continue
        # `mime` on the analysis item is the Google-native EXPORT selector, NOT the stored source
        # MIME — feeding a real "application/pdf" here would KeyError in _download's EXPORT_MAP.
        src_mime = r.get("mime")
        items.append({"file": r["file"], "drive_file_id": r.get("drive_file_id"),
                      "mime": src_mime if src_mime in _EXPORT_MAP else None,
                      "path": r.get("path"), "checksum": r.get("checksum"),
                      "drive_id": r.get("drive_id"),
                      "drive_account_id": r.get("drive_account_id"),
                      "size_kb": r.get("size_kb"),
                      "source_modified": r.get("source_modified")})
    # ── PERSIST THE EXCLUSION ONTO THE RUN ───────────────────────────────────────────────────
    # Recorded on `scan_runs.scope`, beside `skipped_out_of_scope`, because it is the same kind of
    # fact: part of the boundary of what this run covered. Without it the Overview reconciliation
    # can only say "not recorded" for this bucket, permanently — the panel already reads these
    # exact keys and has nothing to read.
    #
    # A ZERO HERE IS A MEASUREMENT, and that is the only reason writing zeros is allowed. This
    # code ran, walked every inventory row, and found none flagged. A run that never reached this
    # point (a Discover that was never assessed) writes NOTHING, so its scope carries no such key
    # and a reader correctly sees "not recorded" rather than a reassuring 0. merge_scan_scope
    # touches only the keys handed to it, so nothing else on the scope is disturbed.
    core.store.merge_scan_scope(scan_id, {
        "lifecycle_excluded": lifecycle_flagged,
        "lifecycle_eligible_excluded": eligible_excluded,
        "lifecycle_overridden": overridden,
    })
    # ── THE RUN'S TOTAL IS THE POPULATION ASSESS ACTUALLY ENQUEUED ───────────────────────────
    # `files` was written once, at init_scan_run, from the DISCOVERED count — correct then,
    # because at discover time that is the only population there is. The loop above has since
    # narrowed it twice: non-assessable rows are dropped, and by default so is every row a
    # lifecycle rule flagged. Only `items` was ever enqueued.
    #
    # Left unwritten, `files` keeps describing the wider population while `files_done` counts the
    # narrower one, so `files - files_done` reports deliberately-excluded files as NOT STARTED.
    # That difference is what the frontend reads to call a run partially complete — so with the
    # old numbers the likeliest cause of a "partially completed" screen was a lifecycle rule doing
    # exactly what it was asked to. After this write, `files - files_done` means precisely
    # "selected for THIS assess and never started".
    #
    # Written HERE, with `items` in hand and immediately before the fan-out, so no worker can bump
    # files_done against a total that is still the discovered one. Assignment, not accumulation: a
    # re-assess re-enters this path and must describe ITS OWN population, not the sum of both runs.
    core.store.set_scan_files(scan_id, len(items))
    _enqueue_analysis(scan_id, source, items, ai=ai, pii=pii, user=user,
                      incremental=incremental, exclude_remediated=exclude_rem,
                      force_batch=bool(params.get("batch")),
                      parent_job=job if scheduled_context and source in ("drive", "sharepoint") else None)


def _analyse_and_persist_one(scan_id, item, source, pii, svc, toks, now, _lf, user=None,
                             rubric_hash=None, incremental=True, job=None) -> None:
    """Per-file WALL-CLOCK safety net around the real work (_impl below).

    The sub-steps are already individually bounded — download (httpx timeout 120s), the .NET
    office CLI (ACP_OFFICE_CLI_TIMEOUT 180s), OCR (ACP_OCR_MAX_IMAGES 30 + downscale). But "each
    sub-call is bounded" is not "the file is bounded": a step that ever slips its own timeout, a
    retry loop, or a future analyser added without one would let ONE document hold its worker
    forever — and with a small worker pool that stalls the whole scan at "0 of N", exactly the
    shape seen on a cold, image-heavy SharePoint run. This converts that into a bounded per-file
    error: if a file exceeds ACP_SCAN_FILE_TIMEOUT_S (default 600s), it is recorded as an error
    and the worker is freed, so the scan always drains and finalizes.

    Safe against the finalize trigger: the caller (scan_file / scan_batch) runs its
    count_files_done → scan_finalize check AFTER this returns, and the error row here counts
    toward that total — so a timed-out LAST file still finalizes the scan. save_file_result
    upserts, so if the orphaned worker thread finishes late with a real result it simply replaces
    the error row (no double count). ACP_SCAN_FILE_TIMEOUT_S=0 disables the watchdog.

    `job` is the queue row, threaded down to save_file_result purely so the result write can be
    fenced — see its docstring. The ORPHAN THIS FUNCTION CREATES is one of the two writers that
    fence exists for: the thread below is a daemon and is never cancelled, so after the join
    expires it keeps running with nothing left holding its claim, and its eventual write must not
    land on a later attempt's result. Within the same attempt it still wins, which is the
    replaces-the-error-row behaviour described above; only its attempt-1 self landing after
    attempt 2 is refused.
    """
    import threading
    try:
        cap = int(_os.environ.get("ACP_SCAN_FILE_TIMEOUT_S", "600") or "600")
    except ValueError:
        cap = 600
    if cap <= 0:
        return _analyse_and_persist_one_impl(scan_id, item, source, pii, svc, toks, now, _lf,
                                             user=user, rubric_hash=rubric_hash,
                                             incremental=incremental, job=job)
    outcome: dict = {}

    def _work():
        try:
            from ai import assessment_vision_budget
            try:
                budget = max(0, float(_os.environ.get("ACP_ASSESS_VISION_BUDGET_S", "240")))
            except ValueError:
                budget = 240
            budget = min(cap * 0.4, budget)
            with assessment_vision_budget(budget):
                _analyse_and_persist_one_impl(scan_id, item, source, pii, svc, toks, now, _lf,
                                          user=user, rubric_hash=rubric_hash,
                                          incremental=incremental, job=job)
            outcome["done"] = True
        except BaseException as e:   # noqa: BLE001 — re-raised on the caller thread below
            outcome["error"] = e

    # joblog.bind: contextvars do not cross a thread start, so without this the per-document
    # stage records emitted downstream would carry a document and no job — exactly the
    # correlation the diagnostic exists for. Captured here, re-entered inside the thread.
    import joblog as _jl
    th = threading.Thread(target=_jl.bind(_work), name=f"scanfile:{item.get('file')}", daemon=True)
    th.start()
    th.join(cap)
    if th.is_alive():
        name = item.get("file")
        print(f"[scan] {name}: exceeded the {cap}s per-file limit — recording it as an error and "
              f"moving on so the scan can finish (the file's own bounded sub-calls will let the "
              f"stuck worker thread exit on its own)", flush=True)
        try:
            core.store.log_decision("system", "scan.file_timeout", scan_id=scan_id, file=name,
                                    detail=f"exceeded per-file limit {cap}s")
        except Exception:
            swallowed("_analyse_and_persist_one: logging the scan.file_timeout decision failed", scan_id)
        # Record an error row so count_files_done reaches total and the scan finalizes. Upsert, so a
        # late-finishing orphan thread just overwrites this with its real result.
        try:
            core.store.save_file_result(scan_id, {
                "file": name, "engine": "n/a", "status": "error", "score": None,
                "compliant": 0, "skipped_rules": 0, "issues": [],
                "drive_file_id": item.get("drive_file_id"),
                "source_modified": item.get("source_modified"),
                "checksum": item.get("checksum")}, now, job=job)
        except Exception:
            swallowed("_analyse_and_persist_one: store.save_file_result for a timed-out file failed — this "
                       "file will never reach the files_done counter", scan_id)
        # Flag the timed-out file ERROR in its trace (item 2), so it stands out in Langfuse instead
        # of looking like a clean discover-only trace. Best-effort.
        try:
            import lf as _lf2
            _lf2.file_error_span(_lf2.file_trace(scan_id, name, user=user),
                                 f"exceeded per-file limit {cap}s")
        except Exception:
            swallowed("_analyse_and_persist_one: emitting the file-timeout error span failed", scan_id)
        return
    if "error" in outcome:
        raise outcome["error"]   # preserve the impl's original error propagation


def _escalate_low_confidence_findings(fdict: dict, filepath, *,
                                      scan_id: str | None = None,
                                      file: str | None = None) -> None:
    """Second-opinion HuggingFace vision pass for LOW-confidence WCAG findings.

    Looks up each finding's (rule, fmt) registration; when confidence is LOW the document's
    first-page render is sent to the cloud vision provider. Each matching finding is annotated
    in-place with an `hf_provenance` dict and one structured log line is emitted.

    Best-effort: never raises, never blocks the scan. AI-off mode is respected.
    Token/image bytes never touch a log line.
    """
    try:
        import core as _core
        if not _core.store.get_ai_enabled():
            return
        from second_opinion_policy import eligible as _second_opinion_eligible, load_policy
        if not load_policy(_core.store)["enabled"]:  # immediate kill switch for new calls
            return
        snapshot = _core.store.get_scan_inputs(scan_id) if scan_id else None
        policy = ((snapshot or {}).get("feature_flags") or {}).get("second_opinion_policy")
        # Missing/malformed snapshots fail closed. The general AI switch is not consent for
        # assessment-time off-box processing.
        if not policy or policy.get("enabled") is not True:
            return
        import providers as _prov
        cloud = _prov.cloud_vision_provider()
        if cloud is None:
            return
        import rule_registry as _reg
        import render as _render

        _reg.load()                             # idempotent — format packages register on import

        ext = _Path(file or "").suffix.lower()  # e.g. ".pdf", ".docx"
        _EXT_FMT = {".pdf": "pdf", ".docx": "docx", ".pptx": "pptx",
                    ".xlsx": "xlsx", ".html": "html", ".htm": "html"}
        fmt = _EXT_FMT.get(ext)
        if fmt is None:
            return

        issues = fdict.get("issues") or []
        low_conf = []
        for issue in issues:
            wcag_str = issue.get("wcag") or ""
            rule = wcag_str.split()[0] if wcag_str else ""
            reg = _reg.get(rule, fmt) if rule else None
            if (reg is not None
                    and _second_opinion_eligible(policy, rule, reg.confidence.value)):
                low_conf.append(issue)
        if not low_conf:
            return
        reserved, reserve_reason = _core.store.reserve_second_opinion(
            scan_id=scan_id, file=file or "", policy=policy)
        if not reserved:
            print(f"[hf-escalation] scan={scan_id} file={file} skipped={reserve_reason}", flush=True)
            return

        try:
            raw_bytes = _Path(filepath).read_bytes()
        except Exception:
            return
        img_bytes = _render.render_page1_png(raw_bytes, ext)
        if not img_bytes:
            return

        wcag_ids = ", ".join(sorted({i.get("wcag", "?") for i in low_conf}))
        prompt = (
            f"You are an accessibility reviewer. A WCAG heuristic detector flagged this "
            f"document page for potential issues with: {wcag_ids}. "
            f"Please confirm whether any of these violations are present."
        )
        res = cloud.generate(prompt, img_bytes, timeout=120.0)

        _core.store.record_ai_call(
            surface="assessment_second_opinion", provider=res.get("provider") or cloud.name,
            model=res.get("model") or "not_reported", zone=res.get("zone") or "not_reported",
            latency_ms=int(res.get("latency_ms") or 0), ok=bool(res.get("ok")),
            scan_id=scan_id, file=file, cost_usd=float(res.get("cost_usd") or 0),
            reason=res.get("reason"))

        provenance = {
            "provider": res.get("provider") or cloud.name,
            "zone": res.get("zone"),
            "escalated": True,
            "cost_usd": res.get("cost_usd", 0.0),
        }
        for issue in low_conf:
            issue["hf_provenance"] = provenance

        print(
            f"[hf-escalation] scan={scan_id} file={file} "
            f"findings={len(low_conf)} provider={provenance['provider']} "
            f"ok={res.get('ok')} cost_usd={provenance['cost_usd']:.6f}",
            flush=True,
        )
    except Exception:
        swallowed("_escalate_low_confidence_findings failed", scan_id)


def _queued_terminal_exclusion(scan_id, item, source, user):
    """Check current lifecycle before reusing findings or reading source bytes."""
    from lifecycle_identity import source_identity, TERMINAL
    provider = str(source or '').strip().lower()
    if provider not in ('drive', 'sharepoint', 'onedrive'):
        return None
    reader = getattr(core.store, 'queued_source_lifecycle_context', None)
    if not callable(reader):
        return None  # Storeless import paths have no durable context.
    context = reader(scan_id, item['file'])
    if not context:
        raise RuntimeError('queued analysis has no authoritative scan context')
    owner = context.get('owner_email')
    if (str(owner or '').strip().lower() != str(user or '').strip().lower()
            or str(context.get('source') or '').strip().lower() != provider):
        raise RuntimeError('queued analysis source ownership changed')
    current = context.get('inventory')
    frozen = source_identity(owner, provider, item)
    if frozen and not current:
        raise RuntimeError('queued analysis source binding is absent')
    if current:
        actual = source_identity(owner, provider, current)
        if ((frozen and actual != frozen)
                or item.get('drive_file_id') != current.get('drive_file_id')):
            raise RuntimeError('queued analysis source binding changed')
    previous = None
    if frozen:
        previous = core.store.get_source_lifecycle_states(owner, provider, [item])['states'].get(frozen)
    # A retained restoration takes precedence over an older inventory projection.
    status = previous.get('lifecycle_status') if previous else (current or {}).get('lifecycle_status')
    if status not in TERMINAL:
        return None
    return {'status': status, 'identity_state': 'matched' if frozen else 'missing_identity',
            'reason': (previous or {}).get('reason') or (current or {}).get('lifecycle_reason')}


def _analyse_and_persist_one_impl(scan_id, item, source, pii, svc, toks, now, _lf, user=None,
                                  rubric_hash=None, incremental=True, job=None) -> None:
    """Download + analyse + assess + persist ONE file and emit its Discover span on that
    file's own Langfuse trace. Shared by scan_file (per-file fan-out) and scan_batch
    (ADR 0008). A
    fetch/analyse failure is recorded as an 'error' file so the scan still finalizes."""
    from scanner import _download, analyse_and_assess
    import time as _time
    import stage_timing as _st
    name = item["file"]
    exclusion = _queued_terminal_exclusion(scan_id, item, source, user)
    if exclusion:
        written = core.store.save_file_result(scan_id, {
            "file": name, "engine": "lifecycle", "status": "skipped", "score": None,
            "compliant": False, "skipped_rules": 0, "succeeded": False,
            "drive_file_id": item.get("drive_file_id"), "issues": [], "errors": [],
            "lifecycle_exclusion": exclusion,
        }, now, job=job)
        if written:
            import json
            core.store.log_decision("system", "analysis.lifecycle_skipped", scan_id=scan_id, file=name,
                                    detail=json.dumps(exclusion, sort_keys=True))
        return
    checksum = item.get("checksum")
    drive_file_id = item.get("drive_file_id")
    dedup_of = None
    reused_from_scan = None
    # Checksum dedup: a byte-identical copy of a file already analysed earlier in THIS
    # scan (e.g. the same PDF uploaded to two folders under different names) — skip the
    # download + engine analysis + PII extraction entirely and copy the prior result
    # forward under this file's own name/id. Scoped to one scan_id only.
    dedup = core.store.find_by_checksum(scan_id, checksum, filename=name) if checksum else None
    # ADR 0011: reuse ACROSS scans when within-scan dedup didn't match. Gated on the
    # same owner + drive_file_id + checksum + rubric_hash (see find_prior_analysis).
    if not dedup and incremental:
        dedup = core.store.find_prior_analysis(user, drive_file_id, checksum, rubric_hash,
                                               scan_id=scan_id, filename=name)
    tmp = _Path(_tempfile.mkdtemp(prefix="acp-scanone-"))
    fdict = pinfo = None
    _timings = _st.ScanTimings()          # ADR 0037 Step 0 — measure download vs analyse (side-channel)
    try:
        if dedup:
            dedup_of = dedup.pop("dedup_of", None)
            reused_from_scan = dedup.pop("reused_from_scan", None)
            pinfo = dedup.pop("pii")
            fdict = {"file": name, **dedup}
            # Reuse was accepted only after comparing the measured and requested criteria.
            # Narrower requests still need their issue list and score projected to that scope.
            try:
                from scanner import rescore_reused
                # PHASE 3a — re-score the reused analysis under THIS scan's FROZEN scope
                # (get_scan_scope), not the live global, so the reused score matches the scope the
                # run was started under and the traces save_file_result writes for it below.
                # C4 — resolve this file's per-file scope, the same as save_file_result and
                # analyse_and_assess, so a reused score also honours folder/owner scope rules.
                fdict.update(rescore_reused(fdict.get("issues") or [], name,
                                            fdict.get("status"),
                                            scope=core.store.scope_for_file(
                                                scan_id, name, core.store.get_scan_scope(scan_id, refresh=True))))
            except Exception as exc:
                raise RuntimeError(f"cannot safely project reused assessment for {name}") from exc
            if reused_from_scan and pinfo and pinfo.get("total"):
                # PII carries more sensitivity than a WCAG score -- copying it forward
                # gets its own audit entry rather than a silent inherit (ADR 0011).
                core.store.log_decision("system", "pii.copied_forward", scan_id=scan_id, file=name,
                                        detail=f"from scan {reused_from_scan}: {pinfo['total']} item(s)")
        else:
            try:
                _dl_t0 = _time.monotonic()
                # ADR 0020 read side: `checksum` (above, from item metadata) lets this hit a
                # DIFFERENT scan's earlier download of the same content, not just a retry of
                # this one (e.g. a worker restart mid-Assess) — skip Drive/SharePoint entirely
                # on a hit, including the halted-credential check below, since no network call
                # is made either way. Cache miss (or no blob configured) falls through unchanged.
                from scanner import cache_source_bytes, read_cached_source
                cached = read_cached_source(scan_id, name, user, checksum=checksum)
                if cached is not None:
                    (tmp / name).write_bytes(cached)
                else:
                    # An earlier file in this scan already proved the credential cannot read Drive.
                    # Downloading anyway costs six HTTP round-trips (MediaIoBaseDownload retries
                    # five times) to re-learn it, per file. Raise the known reason instead and let
                    # the handler below record the row exactly as it would have.
                    # Drive only, and never a local file: `source` is what decides which credential
                    # the download will use, so it is what decides whether a Drive credential
                    # failure is relevant. A SharePoint or local-corpus scan is unaffected.
                    halted = (drive_download_halted(scan_id)
                              if source == "drive" and not item.get("path") else None)
                    if halted:
                        raise RuntimeError(halted)
                    it = {"name": name, "id": item.get("drive_file_id")}
                    if item.get("mime"):
                        it["mime"] = item["mime"]
                    if item.get("path"):                       # local source — read from disk
                        it["path"] = item["path"]
                    # SHAREPOINT GOES THROUGH GRAPH, NOT THE DRIVE CLIENT. Derived from the scan's own
                    # `source` rather than carried as a flag: a stored marker can drift out of step
                    # with the scan it belongs to, and this cannot. Without it `_download` fell through
                    # to files().get_media() with a Graph item id and every SharePoint file recorded
                    # status='error' — surfacing as "could not analyse — file unreadable" for files
                    # that were never fetched at all.
                    if source == "sharepoint" and not item.get("path"):
                        it["sp"] = True
                        # May be absent for a OneDrive listing, which genuinely has no driveId;
                        # _sp_download reads that as /me/drive, which is correct there and ONLY there.
                        if item.get("drive_id"):
                            it["driveId"] = item["drive_id"]
                    _download(it, tmp, svc, sp_token=toks.get("sp"))
                    # ADR 0020 — cache the source bytes for a later read (best-effort, never
                    # blocks the scan). Dedup'd files skip this branch entirely: their bytes
                    # live under the prior download's key, which the read side above already
                    # tried.
                    cache_source_bytes(tmp, name, scan_id, user, checksum=checksum)
                _timings.add("download", _time.monotonic() - _dl_t0)   # ADR 0037 Step 0
                # Stop BEFORE the expensive analysis. This file shares its logical name with
                # another discovered file and carries ACP's in-document stamp, so it is our own
                # remediated copy shadowing its source. Scanning it ran the Office/PDF engine,
                # the PII pass and the AI pass, then produced a phantom document, a phantom
                # duplicate, and a HITL item asking a reviewer to approve alt text for ACP's own
                # output. detect_acp_stamp only reads the document's properties — cheap.
                #
                # A row is still persisted: count_files_done() counts file_records rows against
                # scan_runs.files, so a missing row would leave the scan permanently unfinalized.
                # It carries acp_stamped, so get_scan's shadow filter hides it from every reader,
                # and it has no issues -> no scan_rule_traces -> it never reaches the HITL queue.
                if item.get("shadow_candidate") and item.get("exclude_remediated"):
                    from scanner import detect_acp_stamp
                    stamp = detect_acp_stamp(tmp / name, _Path(name).suffix.lower())
                    if stamp:
                        print(f"[scan] skipping {name}: ACP-generated output shadowing its "
                              f"source (not analysed, not queued for review)", flush=True)
                        fdict = {"file": name, "engine": "n/a", "status": "skipped",
                                 "score": None, "compliant": 0, "skipped_rules": 0,
                                 "issues": [], "acp_stamped": stamp}
                        pinfo = None
                if fdict is None:
                    # scan_id threads the per-rule progress line through. This is the PRODUCTION
                    # fan-out path (ADR 0007) — run_scan's in-process pool is the local one — so
                    # without it the line works in development and is silent where users are.
                    _an_t0 = _time.monotonic()
                    # doc_ref: the Drive file id, already opaque and already in the database, so
                    # the crash diagnostics name documents by a real identifier rather than by a
                    # digest of the filename. Absent for a source that has no such id, where
                    # joblog falls back.
                    fdict, pinfo = analyse_and_assess(tmp, name, detect_pii=pii, scan_id=scan_id,
                                                      doc_ref=item.get("id"))
                    _timings.add("analyse", _time.monotonic() - _an_t0)   # ADR 0037 Step 0
            except Exception as e:
                # A credential failure is true of the whole scan, so it is named once, acted on
                # once, and every remaining file skips its doomed download (drive_auth_failure).
                # Anything else is this file's own problem and is recorded verbatim.
                _reason = drive_auth_failure(e)
                if _reason:
                    _stop_scan_downloads(scan_id, _reason)
                _msg = _reason or f"{type(e).__name__}: {e}"
                core.store.log_decision("system", "scan.file_error", scan_id=scan_id, file=name,
                                        detail=_msg[:200])
        # Both branches converge here. The pre-analysis skip above only runs on a FRESH
        # analysis; with incremental=true, find_prior_analysis() reuses the previous scan's
        # record and short-circuits the whole download+analyse block — so the phantom sailed
        # straight through with all its issues, wrote scan_rule_traces, and landed back in the
        # human review queue. Observed live: get_scan hid it 2s after discovery, and no
        # "skipping" line was ever printed.
        #
        # The reused record carries acp_stamped (that is how get_scan recognises it), so one
        # check here covers reuse, fresh analysis, and any future path that produces an fdict.
        if (item.get("shadow_candidate") and item.get("exclude_remediated")
                and fdict and fdict.get("acp_stamped") and fdict.get("status") != "skipped"):
            print(f"[scan] skipping {name}: ACP-generated output shadowing its source "
                  f"(reused analysis discarded, not queued for review)", flush=True)
            fdict = {"file": name, "engine": "n/a", "status": "skipped", "score": None,
                     "compliant": 0, "skipped_rules": 0, "issues": [],
                     "acp_stamped": fdict.get("acp_stamped")}
            pinfo = None

        if fdict is None:                              # fetch/analyse failed → error record
            fdict = {"file": name, "engine": "n/a", "status": "error", "score": None,
                     "compliant": 0, "skipped_rules": 0, "issues": []}
        fdict["drive_file_id"] = item.get("drive_file_id")
        fdict["checksum"] = checksum
        fdict["source_modified"] = item.get("source_modified")
        if pinfo:
            fdict["pii"] = pinfo
        # HuggingFace second-opinion for LOW-confidence WCAG findings (item 1). Fresh analyses
        # only — dedup'd results inherit from the original run. Best-effort; never blocks save.
        if not dedup and fdict.get("status") not in ("error", "skipped", "unanalysable"):
            _escalate_low_confidence_findings(fdict, tmp / name, scan_id=scan_id, file=name)
        saved = core.store.save_file_result(scan_id, fdict, now, job=job)
        if saved:
            try:
                import assessment_blocked
                assessment_blocked.record(core.store, scan_id, fdict, job=job)
            except Exception:
                swallowed('_analyse_and_persist_one_impl: recording assessment readability failed', scan_id)
        # ADR 0037 Step 0 — record this file's stage timing (side-channel, best-effort: a timing write
        # must never fail the scan). Skipped when nothing was measured — the reuse/dedup path downloads
        # and analyses nothing, so it has no timing to record.
        try:
            _t = _timings.as_dict()
            if _t.get("totals_s"):
                core.store.record_file_timing(scan_id, name, _t)
        except Exception:
            swallowed("_analyse_and_persist_one_impl: recording the per-file timings failed", scan_id)
        # Document-centric layer (ADR 0003, Phase 1): every scan upserts the long-lived
        # document row (api/documents.py), independent of file_records' per-scan snapshot.
        # Defensively wrapped -- must never break the scan pipeline itself, only lose this
        # layer for that one file (same posture as the file-centric tracing right below).
        try:
            from documents import resolve_doc_id, compute_triage_score
            doc_id = resolve_doc_id(source, item.get("drive_file_id"), name, checksum)
            prior = core.store.get_document(doc_id)
            created_at = (prior or {}).get("created_at") or now
            age_days = ((_dt.datetime.fromisoformat(now) - _dt.datetime.fromisoformat(created_at)).days
                       if prior and prior.get("created_at") else None)
            tscore, rationale = compute_triage_score(
                compliance_score=fdict.get("score"), pii_severity=(pinfo or {}).get("severity"),
                pii_total=(pinfo or {}).get("total", 0), age_days=age_days,
                skipped_rules=fdict.get("skipped_rules", 0))
            # owner_email from the same `user` — see store.save_scan's note: the tenant gets its
            # own column now, and `owner` keeps whatever it had so nothing changes today.
            core.store.upsert_document(doc_id, source=source, path=name, content_hash=checksum,
                                       owner=user, owner_email=user,
                                       created_at=created_at, last_seen=now,
                                       triage_score=tscore, triage_rationale=rationale,
                                       classify=fdict.get("classify"),   # ADR 0020 stage 2
                                       size_kb=item.get("size_kb"))
        except Exception:
            swallowed("_analyse_and_persist_one_impl: upserting the document triage row failed", scan_id)
        # File-centric tracing (see lf.file_trace): each file gets its own trace, so unlike
        # the old shared-trace model there's no "too many spans on one trace" risk to cap —
        # always emit, regardless of deep-scan setting (the PII sub-span stays conditional).
        ftrace = _lf.file_trace(scan_id, name, user=user)
        dspan = _lf.discover_span(ftrace, fdict["engine"])
        if pii and pinfo and pinfo.get("total"):
            _lf.pii_span(dspan, pinfo, filename=name)
        dspan.end(output={"engine": fdict["engine"], "sensitive_data": (pinfo or {}).get("total", 0),
                          **({"duplicate_of": dedup_of} if dedup_of else {}),
                          **({"reused_from_scan": reused_from_scan} if reused_from_scan else {})})
        # Item 1 — write this file's ASSESS result to its trace NOW (not only in the finalize
        # batch), so scores show up in Langfuse as the scan progresses. Item 2 — a file that could
        # not be assessed gets an ERROR-level span so it stands out in the trace list instead of
        # looking like a clean discover-only trace. Both best-effort — tracing never breaks a scan.
        try:
            if str(fdict.get("status")) in ("error", "unanalysable"):
                _lf.file_error_span(ftrace, fdict.get("error") or fdict.get("status"))
            else:
                _emit_realtime_file_assess(scan_id, name, _assess_level(scan_id), user=user)
        except Exception:
            swallowed("_analyse_and_persist_one_impl: emitting the per-file assess event failed", scan_id)
        # Assess-time pre-draft: run _propose_text_findings now so HITL review cards
        # arrive pre-populated with AI image descriptions before any remediation job runs.
        # Mirrors the remediation-time call in _remediate_file. Fresh downloads only —
        # the dedup/reuse paths never write the file bytes to `tmp`.
        if (not dedup
                and fdict is not None
                and fdict.get("status") not in ("error", "skipped", "unanalysable")
                and _Path(name).suffix.lower() in (".docx", ".pptx", ".xlsx", ".pdf")
                and core.store.get_ai_enabled()):
            try:
                _pre_bytes = (tmp / name).read_bytes()
                _propose_text_findings(scan_id, name, _pre_bytes, True)
            except Exception:
                swallowed("_analyse_and_persist_one_impl: assess-time pre-draft failed", scan_id)
    finally:
        _shutil.rmtree(tmp, ignore_errors=True)


# ── Drive credential failures that are true of the SCAN, not of one file ──────────────────
#
# Every download in a scan uses the same credential, so when the credential is the problem the
# first file's failure has already decided the other N-1. The fan-out did not know that: each
# file ran its own download, MediaIoBaseDownload retried it five times, and each one landed as
# its own 'error' record. A 77-file estate spent ~460 HTTP requests establishing one fact, and
# then reported it as 77 unreadable documents rather than as one unusable credential.
#
# Observed live 2026-07-31 on scan f529ed607a26 (77 files, awaiting Assess). The deployed ADC
# credential is a stock `gcloud auth application-default login` grant — openid, email,
# cloud-platform, sqlservice.login — with no Drive scope at all, so every Drive call returns
# 403 "Request had insufficient authentication scopes". Nothing in the product said so: 403 was
# not classified, and the operator-facing outcome would have been an estate of 77 documents ACP
# claimed it could not read.
#
# 401 was already classified here, and its comment already says the condition is scan-wide
# ("on a long scan every remaining file fails with 401") — it just never acted on that.
_DRIVE_STOP_KEY = "drive_auth_stop:%s"


def drive_auth_failure(exc: Exception) -> str | None:
    """The operator-facing reason this Drive call failed, when no other file will fare better.

    Returns None for an ordinary per-file failure (a corrupt document, a file over the download
    cap, a transient 5xx) — those are genuinely about that one file and must not stop a scan.

    The two that ARE scan-wide:
      * 401 / Invalid Credentials — a GIS access token expired mid-scan (they live ~1h and
        cannot be refreshed server-side).
      * 403 insufficient scopes — the credential is valid and simply was not granted Drive.
        Distinct from a 403 on ONE file (`insufficientFilePermissions`), which is that file's
        own sharing and leaves the rest of the scan perfectly readable.
    """
    msg = f"{type(exc).__name__}: {exc}"
    low = msg.lower()
    if "401" in msg or "Invalid Credentials" in msg or "authError" in msg:
        return ("Drive authorization expired mid-scan — sign in again and re-run the scan "
                "to cover this file")
    scope_403 = ("insufficient authentication scopes" in low
                 or "access_token_scope_insufficient" in low
                 or "insufficientpermissions" in low)
    if scope_403 and "insufficientfilepermissions" not in low:
        return ("The server's Google credential is not authorized for Drive — it needs the "
                "drive.readonly scope. Re-authorize ADC and restart the worker; no file in "
                "this scan can be read until then")
    return None


def _stop_scan_downloads(scan_id: str, reason: str) -> None:
    """Record that this scan's credential is unusable, so the remaining files skip the download.

    A marker rather than an exception: count_files_done() counts file_records against
    scan_runs.files, so a file that never persists a row leaves the scan permanently
    unfinalized — the UI sits at N/M with no error, which is the "stuck scan" false alarm this
    codebase has already produced three times in one day. Every file still gets its row; what
    it no longer gets is a doomed download.
    """
    try:
        if not core.store.get_setting(_DRIVE_STOP_KEY % scan_id):
            core.store.set_setting(_DRIVE_STOP_KEY % scan_id, reason)
            print(f"[scan] {scan_id}: halting downloads — {reason}", flush=True)
            core.store.log_decision("system", "scan.drive_unusable", scan_id=scan_id,
                                    detail=reason[:200])
    except Exception:
        # a marker that cannot be written must not take the scan down with it
        swallowed("_stop_scan_downloads: setting the drive-stop marker failed", scan_id)


def drive_download_halted(scan_id: str) -> str | None:
    """The reason downloads were halted for this scan, or None to proceed."""
    try:
        return core.store.get_setting(_DRIVE_STOP_KEY % scan_id) or None
    except Exception:
        return None


def clear_drive_stop(scan_id: str) -> None:
    """Forget a previous credential failure so a re-run actually retries.

    Called from _enqueue_analysis, the ONE choke point both the immediate scan path and the
    deferred Assess path go through. Without this, fixing the credential and pressing Assess
    again on the same scan id would short-circuit every file against a stale marker — the fix
    would look like it had not worked.
    """
    try:
        core.store.set_setting(_DRIVE_STOP_KEY % scan_id, "")
    except Exception:
        swallowed("clear_drive_stop: clearing the drive-stop marker failed", scan_id)


# ADR 0038 — pausable/resumable scans. Same marker mechanism as _DRIVE_STOP_KEY above, but the
# opposite finalize semantics: a paused file gets NO row (the run stays legitimately unfinalized
# until resumed), where a halted file still gets a skip row (the run wants to finalize). Store
# methods only in this PR — pause_scan/resume_scan (api/store.py) and the marker below are wired
# into the job handlers so the mechanism is real and tested, but nothing sets the marker yet:
# the HTTP routes and frontend control are a deliberately separate follow-up PR, per this ADR's
# own "small, seam-aligned change" framing.
_PAUSE_KEY = "scan_paused:%s"


def scan_paused(scan_id: str) -> bool:
    """Whether new work should stop dispatching for this scan. Fails open to False — a marker
    that cannot be read must not accidentally freeze a scan that was never actually paused."""
    try:
        return bool(core.store.get_setting(_PAUSE_KEY % scan_id))
    except Exception:
        return False


def set_scan_paused_marker(scan_id: str) -> None:
    """Step 2 of ADR 0038's pause recipe (step 1 is store.pause_scan's CAS) — a caller sets both,
    same two-step shape as _stop_scan_downloads writing its own marker after the status check."""
    try:
        core.store.set_setting(_PAUSE_KEY % scan_id, "1")
    except Exception:
        # a marker that cannot be written must not take the pause request down with it
        swallowed("set_scan_paused_marker: setting the pause marker failed", scan_id)


def clear_scan_paused_marker(scan_id: str) -> None:
    """Step 2 of ADR 0038's resume recipe. Also called defensively from _enqueue_analysis (below)
    for the same reason clear_drive_stop is: any re-dispatch of this scan_id must not find a
    stale marker from an earlier pause and silently no-op every file."""
    try:
        core.store.set_setting(_PAUSE_KEY % scan_id, "")
    except Exception:
        swallowed("clear_scan_paused_marker: clearing the pause marker failed", scan_id)


def _make_svc(source, toks):
    """Build the Drive client once per job, resiliently — a build failure degrades to
    None (downloads then fail per-file into 'error' records) rather than killing the job."""
    from scanner import _drive_service
    if source in ("local", "sharepoint"):
        return None
    drive_token = toks.get("drive")
    if not drive_token:
        # A missing token means the user's session token was never forwarded or has expired.
        # Silently falling back to ADC here produced 0-file scans that looked successful.
        raise RuntimeError(
            "Drive auth token missing from SCAN_TOKENS — token expired or not forwarded "
            "to this worker replica. Re-authenticate and start a new scan."
        )
    try:
        return _drive_service(drive_token)
    except Exception:
        return None


def _analysis_services(scan_id, source, toks, job, user, *, item=None):
    from scheduled_scan_execution import services_for_job
    scheduled = services_for_job(core.store, scan_id, job, source=source, user=user, item=item)
    if scheduled:
        svc, sp_token, _ = scheduled
        return svc, {**toks, 'sp': sp_token}
    return _make_svc(source, toks), toks


@handler("scan_batch")
def _scan_batch(payload: dict, job: dict) -> None:
    """Analyse + persist a CHUNK of files in one durable job (ADR 0008), then bump the
    done counter ONCE by the chunk size. Cuts queue churn ~SCAN_BATCH_SIZE× on large
    estates; the job that completes the count enqueues finalize (same trigger as scan_file).
    Idempotent on retry — save_file_result replaces per file, so re-running a chunk is safe."""
    import lf as _lf
    scan_id = payload["scan_id"]
    source = payload.get("source", "drive")
    pii = bool(payload.get("pii", False))
    user = payload.get("user")
    items = payload.get("items", [])
    toks = core.get_scan_tokens(scan_id)
    now = _dt.datetime.now(_dt.timezone.utc).isoformat()
    svc, toks = _analysis_services(scan_id, source, toks, job, user)
    rubric_hash = core.active_rubric().hash
    incremental = bool(payload.get("incremental", True))
    try:
        workers = max(1, int(_os.environ.get("ACP_SCAN_BATCH_WORKERS", "4") or "4"))
    except ValueError:
        workers = 4

    def _run_one(it):
        # ADR 0038 — checked once per item, right when the executor actually starts it (not at
        # submission time, when all items in a batch are handed to the pool at once). A file
        # already past this point when pause fires runs to completion and persists its row, same
        # as the ADR requires; one not yet started gets no row at all, which is what keeps
        # count_files_done() below scan_runs.files and the run legitimately unfinalized.
        if scan_paused(scan_id):
            return
        _analyse_and_persist_one(scan_id, it, source, pii, svc, toks, now, _lf, user=user,
                                 rubric_hash=rubric_hash, incremental=incremental, job=job)

    if workers <= 1 or len(items) <= 1:
        for it in items:
            _run_one(it)
    else:
        import concurrent.futures
        import joblog as _jl
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(workers, len(items))) as ex:
            # Bound for the same reason as the Thread above: submit() does not carry the
            # caller's context into the pool thread. This is the OUTER of the two hops between
            # worker.run_once and the per-document stage records.
            futures = [ex.submit(_jl.bind(_run_one), it) for it in items]
        # collect results after all complete; re-raise first exception if any
        exc = None
        for f in futures:
            try:
                f.result()
            except Exception as e:
                if exc is None:
                    exc = e
        if exc is not None:
            raise exc
    _lf.flush()  # send any file spans before the batch job exits
    done, total = core.store.count_files_done(scan_id)   # ADR 0013: count, not a running counter
    if done >= total > 0:
        core.store.enqueue_job("scan_finalize",
                               {"scan_id": scan_id, "source": source,
                                "ai": bool(payload.get("ai", True)), "pii": pii}, scan_id=scan_id)


@handler("scan_file")
def _scan_file(payload: dict, job: dict) -> None:
    """Download + analyse + assess + persist ONE file, emit its Langfuse spans, then
    bump the done counter — the job that completes the count enqueues finalize.
    Resilient: a fetch/analyse failure is recorded as an 'error' file so the counter
    always advances and the scan can finalize."""
    import lf as _lf
    scan_id = payload["scan_id"]
    source = payload.get("source", "drive")
    pii = bool(payload.get("pii", False))
    user = payload.get("user")
    toks = core.get_scan_tokens(scan_id)
    now = _dt.datetime.now(_dt.timezone.utc).isoformat()
    svc, toks = _analysis_services(scan_id, source, toks, job, user, item=payload)
    # ADR 0038 — same checkpoint as _scan_batch's _run_one: a job not yet started when pause
    # fires is skipped entirely (no row), leaving it for resume's re-dispatch to pick up.
    if not scan_paused(scan_id):
        _analyse_and_persist_one(scan_id, payload, source, pii, svc, toks, now, _lf, user=user,
                                 rubric_hash=core.active_rubric().hash,
                                 incremental=bool(payload.get("incremental", True)), job=job)
    _lf.flush()  # send file span before this per-file job exits
    done, total = core.store.count_files_done(scan_id)   # ADR 0013: count, not a running counter
    if done >= total > 0:
        core.store.enqueue_job("scan_finalize",
                               {"scan_id": scan_id, "source": source,
                                "ai": bool(payload.get("ai", True)), "pii": pii}, scan_id=scan_id)


@handler("workspace_scan_discover")
def _workspace_scan_discover(payload: dict, job: dict) -> None:
    """Enumerate a content workspace and fan out one workspace_scan_file per eligible document.

    The Discover step for the upload-first flow (ADR 0044): the same shape as _scan_discover —
    list the source, fix the run's file total, enqueue the per-file work — against a source that
    is a DATABASE TABLE rather than a connector.

    WHY THIS IS A SEPARATE HANDLER AND NOT A BRANCH IN _scan_discover, for the same reason #1116
    gave for not reusing _scan_file: that function is connector-shaped throughout. It builds a
    Drive service, raises when no Drive token is present, walks paginated listings, and threads
    tokens into every downstream call. A workspace has none of those things, and adding a fifth
    source kind to that state machine would mean carrying its token checks past a path that can
    never have a token.

    AND THIS IS THE PART THAT MATTERS BEYOND TIDINESS. The enumeration that has been crashing in
    production is _scan_discover's initial _list(...) walk — minutes of connector traffic before
    any checkpoint exists, which is exactly why ACP_PER_FOLDER_SCAN_JOBS could not have protected
    it: the fan-out block sits below that walk. Here the enumeration is one indexed SELECT over
    rows that were durably written at upload time. There is no long walk to interrupt, no token
    to expire mid-enumeration, and no re-listing on a retry. A workspace-sourced run does not
    need the durable-checkpoint work the connector path still needs; it does not have the
    problem.

    NOT RESERVED-CAPACITY DISCOVERY, despite the name. #1124 reserves worker slots for
    `scan_discover` because a connector walk can starve the pool; this job is one indexed SELECT,
    so it belongs in the processing pool and gets there because the processing role is defined as
    the complement of that one literal type. That is correct but fragile — see
    tests/test_dedicated_worker_roles.py, which pins both that no registered type falls through
    every role's allow-list and that this one is not pinned behind the reserved workers.

    RESUMABLE BY CONSTRUCTION. A reclaim re-runs this handler from the top. It re-reads the
    workspace (the authoritative population, not a snapshot from the route) and enqueues only
    documents that have no workspace_scan_file job yet, so a fan-out interrupted at file 300 of
    500 resumes at 301 instead of enqueueing 500 more.

    payload: {scan_id, workspace_id, user}
    """
    scan_id = payload["scan_id"]
    workspace_id = payload["workspace_id"]
    user = payload.get("user")

    _phase(job, f"listing documents in workspace {workspace_id}")
    eligible, excluded = _workspace_scan_population(workspace_id, user)

    # HONEST COUNTS, both halves. `files` must be the population actually enqueued or the run can
    # never finalize — count_files_done compares file_records against it, and a document that was
    # never enqueued writes no row (the wedge _record_dead_scan_files exists to prevent, arrived
    # at from the other direction). But a quarantined or duplicate upload silently vanishing from
    # the total is its own dishonesty: the customer uploaded it, and "12 of 12 assessed" over an
    # estate of 15 is a worse answer than "12 assessed, 3 excluded". So the excluded rows are
    # recorded as decisions, where fileErrorReason.js already looks for a per-document reason,
    # rather than being dropped on the floor.
    for doc_name, reason in excluded:
        try:
            core.store.log_decision(user or "system", "content_workspace.excluded_from_scan",
                                    scan_id=scan_id, file=doc_name, detail=reason[:200])
        except Exception:
            swallowed("_workspace_scan_discover: logging an excluded document failed", scan_id)

    core.store.set_scan_files(scan_id, len(eligible))
    # scan.listing_complete, from the closed SCAN_EVENT_KINDS vocabulary — not a new kind for a
    # new source. append_scan_event RAISES on an unknown kind by design, and the emit-site guard
    # at handlers.py:1152 checks every call in this file against that set, so inventing
    # "scan.listed" here would have failed CI rather than degrading quietly.
    #
    # `excluded` rides in the detail because it is the half a bare count cannot express: a run
    # that lists 12 of 15 uploads is complete, not truncated, and the difference has a reason
    # per document recorded above.
    scan_event(scan_id, "scan.listing_complete", phase="discovering", job_id=job.get("id"),
               worker_id=job.get("locked_by"), attempt=job.get("attempts", 1),
               owner_email=user,
               detail={"source": "workspace", "workspace_id": workspace_id,
                       "files_found": len(eligible), "excluded": len(excluded)})

    if not eligible:
        # Mirrors _enqueue_analysis's own empty-items branch. Without it a workspace with nothing
        # assessable in it leaves a 'queued' run that nothing will ever finalize, because the
        # trigger is a per-file job completing and there are no per-file jobs.
        core.store.enqueue_job("scan_finalize",
                               {"scan_id": scan_id, "source": "workspace",
                                "ai": False, "pii": False}, scan_id=scan_id)
        return

    already = _already_enqueued_version_ids(scan_id)
    _phase(job, f"enqueuing {len(eligible) - len(already & {e['version_id'] for e in eligible})} "
                f"of {len(eligible)} document(s)")
    for item in eligible:
        check_cancel()
        if item["version_id"] in already:
            continue
        core.store.enqueue_job("workspace_scan_file", {
            "scan_id": scan_id, "workspace_id": workspace_id,
            "document_id": item["document_id"], "version_id": item["version_id"],
            "file": item["file"], "checksum": item.get("checksum"), "user": user},
            scan_id=scan_id)


def _workspace_scan_population(workspace_id: str, user: str | None) -> tuple[list[dict], list[tuple[str, str]]]:
    """Split a workspace's documents into what this scan will assess and what it will not.

    Returns (eligible, excluded) where `excluded` carries a REASON per document. Module-level so
    tests can exercise the partition without a queue, and so the route can report the same split
    back to the caller before anything is enqueued.

    ELIGIBILITY IS THE LATEST VERSION'S lifecycle_state, and only "ready" qualifies. The other
    states are all deliberate outcomes of the upload pipeline, not errors:

      quarantined  its magic bytes did not match its extension. PRD §13 treats this as a normal
                   terminal upload state; assessing it anyway would defeat the check.
      duplicate    the same content_hash already exists elsewhere in this workspace. Assessing
                   both would double-count one document in every total the run reports.
      expired      the retention sweep deleted its blob. The row survives; the bytes do not.

    A document with no version at all is excluded too — an upload session that was created and
    never completed leaves exactly that, and it has no bytes to assess.

    Filenames come from the version's original_filename, falling back to the document's display
    name: a workspace document's relative_path preserves the customer's folder structure, and
    two files with the same base name in different folders are ordinary. The path is what makes
    them distinguishable, so it is what the scan records.
    """
    eligible: list[dict] = []
    excluded: list[tuple[str, str]] = []
    docs = core.store.list_content_workspace_documents(workspace_id, owner_email=user or "")
    for doc in docs:
        name = doc.get("relative_path") or doc.get("display_name") or doc["id"]
        version = core.store.get_latest_content_workspace_document_version(doc["id"])
        if version is None:
            excluded.append((name, "no uploaded version — the upload was never completed"))
            continue
        state = version.get("lifecycle_state")
        if state != "ready":
            excluded.append((name, f"lifecycle_state={state!r}, not 'ready'"))
            continue
        eligible.append({
            "document_id": doc["id"], "version_id": version["id"],
            "file": version.get("original_filename") or name,
            "checksum": version.get("content_hash"),
        })
    return eligible, excluded


def _already_enqueued_version_ids(scan_id: str) -> set[str]:
    """Which versions this scan has ALREADY fanned out to, so a reclaimed discover resumes.

    Reads the queue rather than a marker column: the queue is the thing that would be duplicated,
    so it is the honest source for whether it already was. A payload that will not parse is
    treated as not-enqueued — re-enqueuing one document is a wasted job, whereas skipping one on
    a parse error loses it from the run entirely, and save_file_result upserts so the duplicate
    is harmless.
    """
    import json as _json
    out: set[str] = set()
    for row in core.store.list_scan_jobs_of_type(scan_id, "workspace_scan_file"):
        try:
            vid = (_json.loads(row.get("payload") or "{}") or {}).get("version_id")
        except Exception:
            continue
        if vid:
            out.add(vid)
    return out


@handler("workspace_scan_file")
def _workspace_scan_file(payload: dict, job: dict) -> None:
    """Assess ONE content-workspace document version (ADR 0044 / PRD upload-and-remediate).

    Unlike every other scan source, this one needs no connector session at all: the file is
    already durably stored in Blob, and workers read it with their OWN managed identity
    (workspace_blob._service_client), not a per-user Drive/SharePoint token cached in Redis
    with a one-hour expiry. That is the whole point of building this on stored uploads rather
    than routing queued processing through a browser-session-bound connector token — a worker
    restart or a slow queue cannot orphan this job the way a token expiry can for `scan_file`.

    So this does NOT reuse `_scan_file`: `_make_svc`/`get_scan_tokens` assume a connector
    source and would raise for one that carries neither a Drive token nor "local"/"sharepoint".
    Instead it downloads the blob to a local temp file itself, then hands off to the SAME
    per-file engine every connector-sourced file goes through (_analyse_and_persist_one) via
    `_download`'s existing `item["path"]` local-read branch — no new analysis code, only a new
    way to arrive at a local file.

    payload: {scan_id, workspace_id, document_id, version_id, file, checksum, user}"""
    import lf as _lf
    import tempfile as _tempfile
    from pathlib import Path as _Path
    import workspace_blob

    scan_id = payload["scan_id"]
    workspace_id = payload["workspace_id"]
    document_id = payload["document_id"]
    version_id = payload["version_id"]
    filename = payload["file"]
    user = payload.get("user")
    now = _dt.datetime.now(_dt.timezone.utc).isoformat()

    # Same pause checkpoint as _scan_file's ADR 0038 note — harmless here today (nothing yet
    # pauses a workspace scan), kept so a future generic "stop" mechanism keyed on scan_id
    # covers this source without a separate code path to remember.
    if not scan_paused(scan_id):
        data = workspace_blob.download_document_bytes(user, workspace_id, document_id, version_id)
        if data is None:
            # Not configured, or the object is gone — the same "can't verify" = "can't proceed"
            # stance every other workspace_blob read in this codebase takes. Recorded as an
            # error file (not a FatalJobError) so the scan still finalizes with an honest result
            # instead of leaving files_done permanently short.
            core.store.save_file_result(scan_id, {
                "file": filename, "engine": "n/a", "status": "error", "score": None,
                "compliant": 0, "skipped_rules": 0, "issues": [],
                "drive_file_id": None}, now, job=job)
            core.store.log_decision("system", "content_workspace.assess_blob_unreadable",
                                    scan_id=scan_id,
                                    detail=f"{workspace_id}/{document_id}/{version_id}")
        else:
            tmp = _Path(_tempfile.mkdtemp(prefix="acp-wsscan-"))
            local_path = tmp / filename
            local_path.write_bytes(data)
            item = {"file": filename, "path": str(local_path), "checksum": payload.get("checksum")}
            # incremental=False: cross-scan reuse (find_prior_analysis) is keyed on
            # drive_file_id, which a workspace file has none of — deliberately not attempted,
            # rather than reused on a mismatch that would never actually match.
            _analyse_and_persist_one(scan_id, item, "workspace", False, None, {}, now, _lf,
                                     user=user, rubric_hash=core.active_rubric().hash,
                                     incremental=False, job=job)
    _lf.flush()
    done, total = core.store.count_files_done(scan_id)
    if done >= total > 0:
        core.store.enqueue_job("scan_finalize",
                               {"scan_id": scan_id, "source": "workspace", "ai": False, "pii": False},
                               scan_id=scan_id)


@handler("scan_finalize")
def _scan_finalize(payload: dict, job: dict) -> None:
    """Aggregate the per-file results into the scan summary and run the shared post-scan
    step (HITL routing + audit). No scan-wide Langfuse trace to finish anymore — file-
    centric tracing (lf.file_trace) already wrote each file's Discover span as it was
    analysed; this just flushes anything still pending."""
    import lf as _lf
    scan_id = payload["scan_id"]
    source = payload.get("source", "drive")
    ai = bool(payload.get("ai", True)) and core.store.get_ai_enabled()
    now = _dt.datetime.now(_dt.timezone.utc).isoformat()
    core.store.finalize_scan_run(scan_id, now)
    _lf.flush()
    core.finalize_scan(scan_id, ai, source)
    # Release the active-Discovery slot. Must happen AFTER finalize_scan_run sets the terminal
    # status so the guard is only free once the scan is durably terminal. Wrapped so a guard-
    # release failure never loses a finalized scan — the guard can be reconciled later.
    try:
        core.store.release_discovery_guard(scan_id)
    except Exception:
        logger.warning("_scan_finalize: failed to release discovery guard for %s", scan_id, exc_info=True)
    # ADR 0020 — for a DEFERRED scan the Assess-phase analysis just completed, so this IS the
    # assessment: stamp assessed_at + build the assess trace now (in the immediate-scan model the
    # user runs Assess manually later, so we don't auto-mark there). assess_params exists only for
    # deferred scans, so this gate never fires on a normal scan.
    if core.store.get_setting(f"assess_params:{scan_id}"):
        core.store.mark_assessed(scan_id, now)
        core.store.enqueue_job("assess_trace", {"scan_id": scan_id, "level": "AA"}, scan_id=scan_id)
    core.clear_scan_tokens(scan_id)


def _assess_level(scan_id: str) -> str:
    """The WCAG conformance target this scan is assessed against — the deferred-Assess param when
    present, else the AA legal default. Used to write per-file assess results in real time."""
    try:
        lvl = _json.loads(core.store.get_setting(f"assess_params:{scan_id}") or "{}").get("level")
        if lvl:
            return str(lvl)
    except Exception:
        swallowed("_assess_level: reading the assess level from settings failed — falling back to the "
                   "default", scan_id)
    return "AA"


def _file_assess_from_traces(rule_rows: list[dict], level: str):
    """(sc_counts, outcomes, conformant) for ONE file from its scan_rule_traces rows — the same
    reduction ensure_assess_trace does per file, factored out so the real-time and finalize paths
    cannot diverge. `conformant` = no FAIL at or below the target WCAG level."""
    RANK = {"A": 1, "AA": 2, "AAA": 3}
    target = RANK.get(str(level).upper(), 2)
    sc_counts: dict[str, int] = {}
    outcomes: dict[str, int] = {}
    blocking = False
    for r in rule_rows:
        oc = r.get("outcome") or "NOT_EVALUATED"
        outcomes[oc] = outcomes.get(oc, 0) + 1
        if r.get("outcome") == "FAIL":
            sc_counts[r["rule_id"]] = r.get("finding_count") or 1
            if RANK.get((r.get("level") or "A").upper(), 1) <= target:
                blocking = True
    return sc_counts, outcomes, (not blocking)


def _emit_file_assess(scan_id: str, fname: str, level: str, *, sc_counts, outcomes, conformant,
                      score, pii_payload, remediation, user=None) -> None:
    """Write ONE file's assessment to its Langfuse trace: the Assess span (level-flagged so a
    non-conformant file stands out), its per-rule ✓/✗ children, the file score, and the trace-level
    verdict/output. Shared by the finalize pass (ensure_assess_trace) and the real-time per-file
    path so both write identically; Langfuse upserts by id, so calling it twice just refreshes."""
    import lf as _lf
    from store import RULE_CATALOG
    ftrace = _lf.file_trace(scan_id, fname, user=user)
    aspan = _lf.assess_span(ftrace, level, blocking=(not conformant), findings=bool(sc_counts))
    if sc_counts:
        _lf.rule_spans(aspan, sc_counts, RULE_CATALOG, filename=fname, scan_id=scan_id, user=user)
    aspan.end(output={"conformant": conformant, "failing_criteria": len(sc_counts or {})})
    _lf.file_score(scan_id, fname, score)
    _lf.file_assessment_result(scan_id, fname, score=score, conformant=conformant, level=level,
                               failing_criteria=sc_counts, outcomes=outcomes,
                               pii=pii_payload, remediation=remediation)
    # Outcome tags so the native Langfuse list filters by result / PII, not only document + format.
    # result:fail when a finding blocks conformance; needs-review when there are findings or review
    # items that don't block; pass when clean. This is the authoritative tag write (it re-includes
    # the base + rule-fail tags, since Langfuse replaces a trace's tags).
    result = ("fail" if not conformant
              else "needs-review" if (sc_counts or (outcomes or {}).get("REVIEW")) else "pass")
    _lf.set_outcome_tags(scan_id, fname, user, result=result,
                         pii_flagged=bool(pii_payload and pii_payload.get("flagged")),
                         failing_rule_ids=list((sc_counts or {}).keys()))


def _emit_realtime_file_assess(scan_id: str, fname: str, level: str, user=None) -> None:
    """Item 1: write a file's assessment to its trace as soon as it is scored, so scores appear in
    Langfuse AS a scan runs instead of only in one batch at finalize (the mid-run blind spot).
    Minimal on purpose — score / conformance / failing criteria / per-check breakdown from the
    file's own rule traces; the finalize pass adds PII + the complete record and upserts over it.
    Best-effort: observability must never break the scan, and it does no work when tracing is off."""
    import lf as _lf
    if not _lf.enabled():
        return
    rows = core.store.get_scan_traces(scan_id, file=fname)
    if not rows:
        return   # not scored yet (discover-only / errored) — nothing to assess
    rec = core.store.get_file_record(scan_id, fname) or {}
    sc_counts, outcomes, conformant = _file_assess_from_traces(rows, level)
    remediation = {"remediated": bool(rec.get("remediated_at")),
                   "written_back": bool(rec.get("drive_write_url")),
                   "published": bool(rec.get("published_at"))}
    _emit_file_assess(scan_id, fname, level, sc_counts=sc_counts, outcomes=outcomes,
                      conformant=conformant, score=rec.get("score"), pii_payload=None,
                      remediation=remediation, user=user)


def ensure_assess_trace(scan_id: str, level: str = "AA") -> None:
    """Write the WCAG assessment to each file's OWN Langfuse trace (file-centric tracing —
    see lf.file_trace): an 'Assess' span per file, with that file's per-rule ✓/✗ outcomes
    as children when scan_rule_traces has them (recorded at scan time), plus the file's own
    compliance score. Idempotent — safe to call repeatedly, Langfuse upserts a trace by id.
    Always emits something for every file (falls back to its stored issues when there's no
    per-rule data, e.g. an older scan) so a 'View trace' chip never 404s. Shared by the
    worker job AND the /scans/{sid}/trace/file/{file} endpoint."""
    import lf as _lf
    from store import RULE_CATALOG
    rows = core.store.get_scan_traces(scan_id)                 # per file + rule
    # A finding blocks conformance when its WCAG level is at or below the target
    # (A ⊆ AA ⊆ AAA), so the score is level-aware — matching the Assess tab.
    RANK = {"A": 1, "AA": 2, "AAA": 3}
    target = RANK.get(str(level).upper(), 2)
    by_file: dict[str, dict] = {}              # file → {rule_id: count} for ALL failures (spans)
    outcomes_by_file: dict[str, dict] = {}     # file → {PASS/FAIL/REVIEW/NOT_EVALUATED: count}
    blocking_files: set[str] = set()           # files with a failure at/below the target level
    for r in rows:
        f = r["file"]
        by_file.setdefault(f, {})
        oc = outcomes_by_file.setdefault(f, {})
        outcome = r.get("outcome") or "NOT_EVALUATED"
        oc[outcome] = oc.get(outcome, 0) + 1
        if r["outcome"] == "FAIL":
            by_file[f][r["rule_id"]] = r.get("finding_count") or 1
            if RANK.get((r.get("level") or "A").upper(), 1) <= target:
                blocking_files.add(f)
    res = core.store.get_scan(scan_id)
    owner = (res or {}).get("run", {}).get("owner_email")
    source = (res or {}).get("run", {}).get("source")
    # ADR 0003 Phase 2: seed a 'not_started' remediation_state row for every violation
    # newly seen at Assess time. Only inserts (never overwrites), so a rule still failing
    # on a later scan doesn't reset any progress already made on it.
    from documents import resolve_doc_id
    identities = {r["file"]: r for r in core.store.list_file_identities(scan_id)}
    # PII flag per file — fetched once, grouped by file. `pii_type` is a CATEGORY ('us_ssn',
    # 'email_address'), never the value (the same `sensitive_data_types` the PII span already
    # sends); masked samples are deliberately not read here.
    pii_by_file: dict[str, dict] = {}
    for p in core.store.list_pii(scan_id):
        e = pii_by_file.setdefault(p["file"], {"types": set(), "findings": 0, "critical": False})
        if p.get("pii_type"):
            e["types"].add(p["pii_type"])
        e["findings"] += int(p.get("count") or 0)
        if str(p.get("severity") or "").lower() in ("critical", "high"):
            e["critical"] = True
    for f in (res or {}).get("files", []):
        fname = f["file"]
        sc_counts = by_file.get(fname) or {}
        if sc_counts:
            conformant = fname not in blocking_files
            # Seed a remediation_state row for every violation newly seen at Assess time.
            ident = identities.get(fname) or {}
            try:
                doc_id = resolve_doc_id(source, ident.get("drive_file_id"), fname, ident.get("checksum"))
                for rule_id in sc_counts:
                    core.store.seed_remediation_state(doc_id, rule_id, scan_id)
            except Exception:
                swallowed("ensure_assess_trace: seeding remediation state for this file failed", scan_id)
        else:
            conformant = not bool(f.get("issues"))
        pe = pii_by_file.get(fname)
        pii_payload = ({"flagged": True, "types": sorted(pe["types"]),
                        "findings": pe["findings"], "critical": pe["critical"]}
                       if pe else {"flagged": False, "types": [], "findings": 0, "critical": False})
        remediation = {"remediated": bool(f.get("remediated_at")),
                       "written_back": bool(f.get("drive_write_url")),
                       "published": bool(f.get("published_at"))}
        # The Assess span (level-flagged), per-rule ✓/✗ children, file score, and the trace-level
        # verdict — the SAME emit the real-time per-file path uses, so a scan's finalize pass and
        # its incremental writes can never disagree. Structured only (see lf.file_assessment_result).
        _emit_file_assess(scan_id, fname, level, sc_counts=sc_counts,
                          outcomes=outcomes_by_file.get(fname), conformant=conformant,
                          score=f.get("score"), pii_payload=pii_payload,
                          remediation=remediation, user=owner)
    _lf.flush()


@handler("assess_trace")
def _assess_trace(payload: dict, job: dict) -> None:
    """Worker path for the on-demand assessment trace — delegates to the shared
    ensure_assess_trace so the job and the trace-redirect endpoint stay in agreement."""
    ensure_assess_trace(payload["scan_id"], payload.get("level", "AA"))


@handler("rescore_file")
def _rescore_file(payload: dict, job: dict) -> None:
    """Re-download and re-analyse ONE file from an existing scan, then refresh the scan
    aggregate. Called when a user self-remediates a file externally and clicks Re-scan
    to confirm. Tokens are embedded in the payload (scan tokens were cleared at finalize)."""
    import lf as _lf
    scan_id = payload["scan_id"]
    file = payload["file"]
    source = payload.get("source", "drive")
    pii = bool(payload.get("pii", False))
    user = payload.get("user")
    drive_token = payload.get("drive_token")
    from scanner import _drive_service
    svc = None
    if source not in ("local", "sharepoint") and drive_token:
        try:
            svc = _drive_service(drive_token)
        except Exception:
            swallowed("_rescore_file: building a Drive service for re-scoring failed", scan_id)
    toks = {"drive": drive_token} if drive_token else {}
    now = _dt.datetime.now(_dt.timezone.utc).isoformat()
    file_rec = core.store.get_file_record(scan_id, file) or {}
    item = {
        "file": file,
        "drive_file_id": file_rec.get("drive_file_id"),
        "source": source,
    }
    if source == "local":
        # Reconstruct the corpus path the same way the original scan did.
        import os as _os
        corpus = _os.environ.get("ACP_CORPUS_DIR", "/corpus")
        item["path"] = _os.path.join(corpus, file)
    # job=job even though a re-score is not a retry of anything: the fence compares attempts
    # only WITHIN one job id, so passing this job's own identity stamps the row without ever
    # making a re-score refusable — and leaves the row fenced against the scan job's orphans.
    _analyse_and_persist_one(scan_id, item, source, pii, svc, toks, now, _lf, user=user, job=job)
    core.store.refresh_scan_aggregate(scan_id)
    _lf.flush()


# ── Applying reviewer-approved content (WCAG 1.1.1, Office) ───────────────────────────────
_OFFICE_ALT_MIME = {
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}


# The criteria the link-text write-back may CREDIT, per format — never the union.
#
# Credit is granted by re-scanning the written bytes and finding the criterion absent
# (_apply_one_value_kind). A criterion no detector emits for this format is absent from every
# re-scan there will ever be, so crediting it on that evidence proves nothing: the gate passes
# vacuously and the file is certified against a criterion nobody checked. So a criterion
# appears here only where a detector actually emits it for that format — the pairing
# tests/test_applier_detector_parity.py asserts, and the reason 2.4.9 (duplicate display text,
# docx_checks/pptx_checks) is not claimed for xlsx, which has no such check.
_LINK_SCS_BY_EXT = {
    "docx": ("2.4.4", "2.4.9"),
    "pptx": ("2.4.4", "2.4.9"),
    "xlsx": ("2.4.4",),
}
_OFFICE_LINK_EXTS = tuple(_LINK_SCS_BY_EXT)

# The text-span lanes: a sentence rewrite (1.3.3) and a language mark (3.1.2), both keyed by a
# prose prefix rather than a part#rId or an href, both written by apply_text_values. Same rule
# as _LINK_SCS_BY_EXT above: a format appears here only where a detector actually emits the
# criterion, so the re-scan credit means something.
#
# The two lists differ, and the difference is structural rather than a gap in the roadmap.
# 1.3.3 is a text rewrite, which every Office format can carry. 3.1.2 needs somewhere to
# record a language, and SpreadsheetML's rich-text run properties have no language element at
# all — so an xlsx language lane could never clear its criterion and would strand every
# approval it accepted. xlsx is therefore absent from _LANGUAGE_EXTS on purpose.
_SENSORY_EXTS = ("docx", "pptx", "xlsx")
_LANGUAGE_EXTS = ("docx", "pptx", "pdf")
# 2.4.6 structure labels: sheet tab and table column renames (xlsx); slide title
# fill-in (pptx — title placeholder exists but was left empty).
_STRUCTURE_LABEL_EXTS = ("xlsx", "pptx")

# The lanes that exist only for PDF: figure alt (`pdf:fig:…` → /Alt) and form-field accessible
# names (`pdf:field:…` → /TU), both written by remediate_pdf.apply_pdf_approved.
_PDF_APPLY_EXTS = ("pdf",)

# 4.1.2 accessible names, per format. PDF writes /TU on an AcroForm field
# (remediate_pdf.apply_pdf_field_name); Word writes w:alias on a content control
# (apply_field_name.apply_docx_field_name) — different writers, one store getter, because
# both answer the same approved-value shape. pptx/xlsx have no content-control equivalent,
# so their 4.1.2 signal stays the ActiveX/OLE advisory no static write can resolve.
_FIELD_NAME_EXTS = ("pdf", "docx")

# 1.4.5 images of text: the approved OCR transcript REPLACES the picture. apply_pptx_image_replacement
# swaps every <p:pic> showing it for a real text box at the same rectangle, drops the image
# relationship, and deletes the media part from the package.
#
# Deleting the part is the whole lane, not a tidy-up. ocr._ooxml_images walks the ZIP NAMELIST
# for ppt/media/* rasters — it never opens a slide — so removing only the <p:pic> leaves the
# bytes tesseract reads and the finding re-fires. Writing descr instead — the pre-#1665 lane,
# whose writer is now RETIRED and called by nothing (api/apply_pptx_image_of_text.py, and
# tests/test_apply_pptx_image_of_text_retired.py holds it that way) — leaves those bytes
# untouched too: it is a 1.1.1 improvement, not removal of the image of text.
#
# 1.4.5 ONLY, deliberately. 1.4.9 is AAA and exempts nothing, so a 1.4.9 row can be a chart —
# and 1.4.5 exempts charts precisely because a picture of data is not a picture of prose.
# Replacing a chart with its axis labels destroys information, so 1.4.9 stays HUMAN and the
# getter is narrowed to ("1.4.5",) rather than reading both bands into one map.
_IMAGE_OF_TEXT_EXTS = ("docx", "xlsx", "pptx")
# ADR 0055. The 1.4.5 card's locator shape, recognised here only to decide whether the alt lane
# needs the translation below — the translation itself lives beside the enumeration it mirrors,
# in apply_office_image_of_text (docx/xlsx) and apply_pptx_image_of_text (pptx, delegated to by
# the former so this file has ONE call site). Matching the shape rather than the rule_id is
# deliberate: apply_alt cannot resolve this locator whatever row it came from, so the question
# the lane actually has is "is any of this untranslated", not "which row wrote it".
#
# Imported from the module that owns the pattern rather than recompiled here, so the recogniser
# and the translator can never disagree about what a media-index locator looks like.
from apply_office_image_of_text import (
    SUPPORTED_EXTS as _DESCRIBED_MEDIA_EXTS,
    is_media_index_locator as _is_media_index_locator,
)
_IMAGE_OF_TEXT_SCS = ("1.4.5",)
_PDF_STRUCTURE_SCS = ("1.3.1", "2.4.6")
_PDF_STRUCTURE_EXTS = ("pdf",)

# Every format an approved value can actually be WRITTEN into — the format scope
# _apply_approved_values gates on, derived from the per-lane constants rather than restated, so
# the two can never disagree. scripts/gen_matrix_coverage.py reads it to derive the matrix's
# applier surface, so a format here with no real writer behind it would over-claim.
_APPLY_VALUE_EXTS = tuple(_OFFICE_ALT_MIME) + _PDF_APPLY_EXTS


def _known_diff_location(change: dict) -> dict:
    """{'locator', 'page'} for a remediation_diff entry, copied from a writer's change record
    only where the writer actually recorded them. Absent means unknown: a page is never read
    out of the locator text, and a Word paragraph index is never a page."""
    out = {}
    locator = change.get('locator')
    if isinstance(locator, str) and locator.strip():
        out['locator'] = locator
    page = change.get('page')
    if type(page) is int and page > 0:
        out['page'] = page
    return out


def _apply_one_value_kind(
        *, scan_id: str, filename: str, working: bytes,
        values: dict[str, str], scs_to_clear: set[str],
        write_fn, diff_rule_id: str, credit_rule_ids: tuple[str, ...],
        noun: str, job: dict, pending_credits: list, extra_work: bool = False,
        residual_state: dict | None = None, diff_rule_ids: dict | None = None,
        refusal_reason_fn=None, only_item_id: str | None = None) -> tuple[bytes, bool]:
    """Shared write → verify → credit sequence for one kind of approved value (alt text or
    link text) applied on top of `working`. Returns (new_working, uploaded_this_kind).

    The values are NOT credited on a successful write. They are credited when a re-scan of the
    written bytes shows the criterion no longer failing AND the caller durably uploads them.
    Successful lanes append a callback to pending_credits; the caller commits those callbacks
    with the artifact fingerprint after storage succeeds, exactly as `verified_diffs` credits an
    automatic fix. A standing-authorized write that remains unverified may be saved as applied,
    with separate durable evidence and no certification or resolved credit.

    extra_work: this lane's `write_fn` carries approved work of its own that is not expressible
    as {locator: text} — today, the decorative markings closed over by the alt lane, whose whole
    point is that they write no text. Without it a file whose only approved 1.1.1 decision was
    "decorative" short-circuits here and the marking never reaches the document.

    residual_state: {"verification": Verification} — the residual re-scan of `working` as it
    stands BEFORE this lane, shared across the lanes of one apply job. It is what makes "did
    this write break something else" answerable: a criterion in this lane's re-scan that was
    absent from the baseline is a regression, and is recorded on the draft's validation row
    (verified_regressed, or `regressions` on a still-failing write). The credit gate above is
    unchanged by it — a regression is recorded evidence, not a new reason to withhold — and a
    lane that is credited advances the baseline, since its bytes become the next lane's
    `working`. None (or a baseline that could not run) records "unknown", never "none".
    """
    if not values and not extra_work:
        return working, False

    # Freeze the exact review items before the lane changes their applied state. Their immutable
    # HITL events carry model_call_id when a reviewer acted on an AI draft; human-authored work
    # simply yields no model outcome row.
    # `only_item_id` narrows this lane to ONE approved row — a retry re-attempts one reviewer's
    # decision, so nothing else on the file may be written or credited by it. None (the ordinary
    # approval path) leaves the lane file-wide, exactly as before.
    # Rows whose targets a different verified fix removed are never written or credited here
    # (review_target_reconciliation); the job computed them once, before reading any value.
    excluded = tuple((residual_state or {}).get('exclude_item_ids') or ())
    review_item_ids = []
    for rule_id in credit_rule_ids:
        review_item_ids.extend(core.store.approved_unapplied_item_ids(
            scan_id, filename, rule_id, item_id=only_item_id, exclude_item_ids=excluded))
    # The locators each item hands the writer, so an unresolved locator can be attributed to the
    # item — and through its HITL event, the model call — that approved it.
    try:
        item_locators = core.store.approved_unapplied_item_locators(
            scan_id, filename, credit_rule_ids, item_id=only_item_id, exclude_item_ids=excluded)
    except Exception:
        swallowed("_apply_one_value_kind: reading the approved items' locators failed", scan_id)
        item_locators = {}
    # The items this lane's verification outcome describes. Narrowed below when an item's
    # every locator failed to resolve: nothing of it was written, so the re-scan says nothing
    # about it and it must not inherit a verified_cleared from its neighbours.
    lane_items = list(review_item_ids)
    semantic_review = False
    semantic_review_revision = None
    if (residual_state or {}).get('retain_unverified'):
        for item_id in lane_items:
            item = core.store.get_hitl_item(item_id) or {}
            if (str(item.get('last_decision_request_id') or '').startswith('standing:')
                    and any(p.get('requires_semantic_review') is True for p in item.get('proposals', []))):
                semantic_review = True
                semantic_review_revision = item.get('approved_source_revision')
    from remediation_contribution import writer_tickets, record_writer_result
    from hashlib import sha256 as _proof_sha256
    import uuid as _proof_uuid
    writer_attempt_id = (f"{job['id']}:{job.get('attempts')}" if job.get('id') else _proof_uuid.uuid4().hex)
    exact_tickets = writer_tickets(core.store, scan_id, filename, review_item_ids,
                                   _proof_sha256(working).hexdigest(), actual_values=values)

    def _model_outcome(outcome: str, detail: str, *, item_ids=None, regressions=None) -> None:
        try:
            core.store.record_ai_validation_outcomes(
                scan_id, filename, diff_rule_id,
                lane_items if item_ids is None else item_ids,
                outcome, detail=detail, regressions=regressions)
            selected_items = set(lane_items if item_ids is None else item_ids)
            # Partial writes and unknown regressions cannot qualify as exact fixes.
            exact_outcome = outcome
            if outcome == "verified_cleared" and (regressions is None or unresolved):
                exact_outcome = "could_not_verify"
            with core.store._db.cursor() as proof_cur:
                core.store._db.execute(proof_cur, "SELECT corrected_sha256 FROM file_records WHERE scan_id=%s AND file=%s", (scan_id, filename))
                artifact = (core.store._db.fetchone(proof_cur) or {}).get("corrected_sha256")
            final_check = (residual_state or {}).get("verification")
            for ticket in exact_tickets:
                if ticket['item_id'] not in selected_items:
                    continue
                ticket_outcome = exact_outcome
                if exact_outcome == "verified_cleared":
                    actually_written = any(a.get('locator') == ticket['locator'] and
                                           a.get('after') == ticket['approved_value'] for a in applied)
                    if not actually_written or not final_check or not final_check.ok:
                        ticket_outcome = "could_not_verify"
                    elif not final_check.cleared({ticket['rule_id']}):
                        ticket_outcome = "verified_still_failing"
                record_writer_result(core.store, [ticket], outcome=ticket_outcome,
                    artifact_sha256=artifact, reference=detail, writer_attempt_id=writer_attempt_id)
        except Exception:
            swallowed("_apply_one_value_kind: recording the AI post-write outcome failed", scan_id)

    _phase(job, f"writing the approved {noun}")
    fixed, applied, unresolved = write_fn(working, values)
    crop_refusal = (refusal_reason_fn is not None and any(
        refusal_reason_fn(working, locator) == 'cropped_image_requires_visible_transcription'
        for locator in unresolved))
    if unresolved:
        # A locator that no longer resolves means the reviewer approved a value for content
        # this document no longer has. Never guess at different content — record and move on.
        core.store.log_decision(
            "system", "apply.unresolved", scan_id=scan_id, file=filename,
            detail=f"{len(unresolved)} approved {noun} value(s) were not written: "
                   + ", ".join(unresolved[:5]))
        # An item whose EVERY locator went unresolved had nothing written for it. That is a
        # post-write outcome of its own — the draft was accepted for content the document no
        # longer has — and it is recorded against the draft's call rather than folded into
        # whatever the rest of the lane goes on to verify.
        gone = set(unresolved)
        unresolved_items = [i for i in lane_items
                            if item_locators.get(i) and set(item_locators[i]) <= gone]
        if unresolved_items:
            _model_outcome("write_unresolved",
                           f"{noun} locator(s) could not be safely written: "
                           + ", ".join(sorted(gone)[:5]),
                           item_ids=unresolved_items)
            lane_items = [i for i in lane_items if i not in unresolved_items]
    unresolved_note = (f"; {len(unresolved)} locator(s) unresolved and not written"
                       if unresolved else "")
    if not applied:
        # NOTHING REACHED THE DOCUMENT. Until this branch logged, that was the quietest failure
        # in the lane: it returned here BEFORE any apply.unverified line, so annotate_apply_
        # outcomes set no apply_outcome, reviewCard mounted no card, and the row — being
        # `approved` — was not in the pending inbox either. The reviewer approved, clicked, and
        # got a file that never publishes, with nothing anywhere to say why. Only an
        # apply.unresolved line in the decision log recorded it, and nothing reads that for a card.
        #
        # A wedged file must never be invisible, so the same apply.unverified shape the two
        # branches below use is written here too, naming the criteria so apply_outcome can match
        # it to this row (normalise_sc('1.1.1/described') is '1.1.1', so a described row matches).
        # Logged only when locators actually went unresolved: a lane with nothing to write is an
        # ordinary no-op and must not manufacture an outcome for a reviewer to read.
        if unresolved:
            explanation = (
                "The image is cropped in Word. Review a transcription of the visible crop "
                "and confirm no useful diagram content would be lost before replacing it. "
                "The original image is kept unchanged."
                if crop_refusal else
                f"All {len(unresolved)} approved locator(s) reach no writable image in this document.")
            core.store.log_decision(
                "system", "apply.unverified", scan_id=scan_id, file=filename,
                detail=f"wrote no {noun} value(s) for {sorted(scs_to_clear)}: "
                       + explanation + " Credit withheld; the approved value is kept for retry")
            _model_outcome("write_unresolved",
                           f"nothing written; every {noun} locator was unresolved"
                           + unresolved_note, regressions=None)
        return working, False

    from output_provenance import stamp_output
    fixed = stamp_output(fixed, filename)

    # PDF name/alt writers may only change their approved target values. A WCAG
    # rescan cannot detect lost filled-in form data or certify the exact written text.
    if (filename.lower().endswith('.pdf') and values and not extra_work
            and all(str(loc).startswith(('pdf:fig:', 'pdf:field:')) for loc in values)):
        from unverified_changes import structurally_readable
        written_targets = {a.get('locator'): values[a['locator']] for a in applied
                           if a.get('locator') in values}
        if not written_targets or not structurally_readable(
                working, fixed, filename, pdf_semantic_targets=written_targets):
            core.store.log_decision('system', 'apply.integrity_failed', scan_id=scan_id,
                file=filename, rule_id=diff_rule_id,
                detail='PDF writer changed unrelated content or failed exact approved-value readback; previous copy retained.')
            _model_outcome('could_not_verify', 'PDF content preservation or exact-value readback failed', regressions=None)
            return working, False

    _phase(job, f"re-verifying the corrected copy ({noun})")
    verification = _verify_residual(fixed, filename, scan_id=scan_id)
    # Newly-failing criteria: in this re-scan, absent from the baseline. Only decidable when both
    # re-scans ran to a trustworthy result; otherwise "unknown" (None), which the row stores as
    # such rather than as an empty list.
    baseline = (residual_state or {}).get("verification")
    regressions = (sorted(verification.residual - baseline.residual)
                   if verification.ok and baseline is not None and baseline.ok else None)
    # Independent semantic rejection remains a gate when consent/model/usage blocks retry.
    # Legacy proposals may omit the semantic-review flag; presence cannot override pixels.
    caption_contradictions = []
    if filename.rsplit('.', 1)[-1].lower() in _OFFICE_ALT_MIME and '1.1.1' in scs_to_clear:
        from office_verified_retry import contradicted_captions
        caption_contradictions = contradicted_captions(fixed, values)
        if caption_contradictions:
            semantic_review = True
            semantic_review_revision = (semantic_review_revision or
                core.store.remediation_source_revision(scan_id))
            core.store.log_decision('system', 'apply.caption_contradicted', scan_id=scan_id,
                file=filename, rule_id='1.1.1',
                detail='Written caption contradicts independently checked visible pixels; verification credit withheld.')
    # A presence-only 1.1.1 pass is not caption accuracy. A single exact Office
    # contradiction can try only the next already-consented tier under standing approval.
    if (caption_contradictions and (residual_state or {}).get('office_retry_allowed')
            and baseline is not None and not unresolved):
        from office_verified_retry import attempt as retry_office_caption
        from ai_spending_budget import BudgetError
        try:
            retry = retry_office_caption(core.store, scan_id=scan_id, filename=filename,
                original=working, failed=fixed, values=values, applied=applied,
                baseline=baseline, failed_check=verification, tickets=exact_tickets,
                verify=lambda candidate: _verify_residual(candidate, filename, scan_id=scan_id))
        except (ValueError, BudgetError):
            retry = None  # Missing/frozen/replayed authority never opens another paid attempt.
        if retry:
            retry_bytes, retry_check, retry_changes, retry_proof = retry
            residual_state['verification'] = retry_check
            residual_state['office_retry'] = retry_proof
            def commit_retry():
                record = core.store.get_file_record(scan_id, filename) or {}
                if record.get('corrected_sha256') != retry_proof['replacement_sha256']:
                    raise ValueError('office_retry_artifact_mismatch')
                existing = core.store.get_remediation_diffs(scan_id, filename) or []
                # The note is the writer identity, not a location: carry the locator the retry
                # actually wrote, when its change record names one (R1).
                core.store.record_remediation_diffs(scan_id, filename, existing + [
                    {'rule_id': '1.1.1', 'before': a['before'], 'after': a['after'],
                     'note': retry_proof['writer_identity'], **_known_diff_location(a)}
                    for a in retry_changes])
                from office_verified_retry import persist_replacement_approval
                replacement_ticket = persist_replacement_approval(core.store, retry_proof)
                record_writer_result(core.store, [replacement_ticket], outcome='verified_cleared',
                    artifact_sha256=record['corrected_sha256'],
                    reference='Independent caption validation and actual Office reassessment',
                    writer_attempt_id=retry_proof['writer_identity'])
                core.store.mark_row_applied(retry_proof['item_id'])
                import json
                core.store.log_decision('system', 'office_retry.saved', scan_id=scan_id,
                    file=filename, rule_id='1.1.1', detail=json.dumps({**retry_proof,
                        'artifact_sha256': record['corrected_sha256'], 'changes': retry_changes,
                        'verification': 'independent_caption_and_actual_reassessment',
                        'original_outcome': 'superseded_not_verified'}, sort_keys=True))
            pending_credits.append(commit_retry)
            return retry_bytes, True

    if caption_contradictions:
        if exact_tickets:
            from ai_escalation_activity import emit
            ticket = exact_tickets[0]
            retry_owner = (core.store.get_scan(scan_id) or {}).get('run', {}).get('owner_email')
            retry_run = ticket.get('run_id')
            if retry_owner and retry_run:
                retry_operation = _proof_sha256((retry_owner + ':' + retry_run + ':' + filename
                    + ':office-caption-retry.v1').encode()).hexdigest()
                emit(core.store, scan_id=scan_id, owner_id=retry_owner, run_id=retry_run,
                     file=filename, operation_id=retry_operation,
                     reason_code='independent_caption_verification_failed', status='needs_manual')
        # Disproven text is different from an unknown draft. Keep the previous copy;
        # allowing remaining issues must never publish a caption known to be false.
        reason = 'Written caption contradicts independently checked visible pixels; previous copy retained. Manual review required.'
        _model_outcome('could_not_verify', reason, regressions=regressions)
        import json
        core.store.log_decision('system', 'apply.caption_rejected', scan_id=scan_id,
            file=filename, rule_id='1.1.1', detail=json.dumps({
                'reason': 'pixel_caption_contradiction_no_verified_alternative',
                'previous_artifact_sha256': _proof_sha256(working).hexdigest(),
                'failed_artifact_sha256': _proof_sha256(fixed).hexdigest(),
                'locators': caption_contradictions, 'manual_review_required': True,
                'previous_copy_retained': True}, sort_keys=True))
        return working, False

    def preserve_unverified(outcome, reason):
        if not (residual_state or {}).get('retain_unverified') or regressions:
            return working, False
        from unverified_changes import structurally_readable
        if not structurally_readable(working, fixed, filename):
            core.store.log_decision('system', 'apply.integrity_failed', scan_id=scan_id,
                file=filename, rule_id=diff_rule_id, detail='Written copy did not pass document integrity checks; previous copy retained.')
            return working, False
        written_locators = {a.get('locator') for a in applied}
        saved_items = [item_id for item_id in lane_items if item_locators.get(item_id)
                       and set(item_locators[item_id]).issubset(written_locators)]
        def commit_unverified():
            import json
            record = core.store.get_file_record(scan_id, filename) or {}
            for item_id in saved_items:
                core.store.mark_row_applied(item_id)
            with core.store._db.cursor() as cur:
                core.store._db.execute(cur, 'UPDATE file_records SET compliant=0 WHERE scan_id=%s AND file=%s', (scan_id, filename))
            core.store.log_decision('system', 'apply.saved_unverified', scan_id=scan_id,
                file=filename, rule_id=diff_rule_id, detail=json.dumps({
                    'artifact_sha256': record['corrected_sha256'], 'item_ids': saved_items,
                    'source_sha256': _proof_sha256(working).hexdigest(),
                    'baseline_residual': sorted(baseline.residual) if baseline is not None and baseline.ok else None,
                    'changes': applied, 'outcome': outcome, 'reason': reason,
                    'verification': 'not_verified', 'requires_semantic_review': semantic_review,
                    'assessment_revision': semantic_review_revision}))
            _model_outcome(outcome, reason + unresolved_note, regressions=regressions)
        pending_credits.append(commit_unverified)
        residual_state['verification'] = verification
        return fixed, True

    if semantic_review:
        return preserve_unverified('could_not_verify', 'AI text was applied; meaning and accuracy require human review. Structural presence alone is not semantic verification.')

    if not verification.ok:
        # COULD NOT VERIFY — the document was unreadable, the scan errored or timed out, an
        # engine was missing, or a rule threw and its criterion is simply absent from the
        # result. None of that is evidence the fix worked, so nothing is credited: the row
        # stays uncertified. The explicit standing-approval path can retain structurally
        # sound bytes with applied-but-unverified evidence; other paths retain the prior copy.
        core.store.log_decision(
            "system", "apply.unverified", scan_id=scan_id, file=filename,
            detail=f"wrote {len(applied)} {noun} value(s) but could not verify "
                   f"{sorted(scs_to_clear)}: {verification.reason}. Resolved credit withheld.")
        _model_outcome("could_not_verify",
                       (verification.reason or "verification unavailable") + unresolved_note,
                       regressions=None)
        return preserve_unverified("could_not_verify", verification.reason or "Verification unavailable")
    if not verification.cleared(scs_to_clear):
        # The value went in but the criterion still fails (content we never saw, or the engine
        # reads it differently). Credit no resolved findings. Explicit automatic approval
        # may retain the successful write while keeping the file uncertified.
        core.store.log_decision(
            "system", "apply.unverified", scan_id=scan_id, file=filename,
            detail=f"wrote {len(applied)} {noun} value(s) but "
                   f"{sorted(verification.still_failing(scs_to_clear))} still fails on re-scan")
        _model_outcome("verified_still_failing",
                       f"still failing: {sorted(verification.still_failing(scs_to_clear))}"
                       + unresolved_note,
                       regressions=regressions)
        return preserve_unverified("verified_still_failing", f"Remaining criterion failures: {sorted(verification.still_failing(scs_to_clear))}")

    if residual_state is not None:
        # These bytes are the next lane's `working`; its regressions are measured from here.
        residual_state["verification"] = verification

    def commit_credit():
        existing = core.store.get_remediation_diffs(scan_id, filename) or []
        # `locator` is the exact target this writer resolved (R1) — stored in its own column so it
        # is neither capped with the note nor parsed back out of prose. `page` only when the
        # writer read a real one off the object it edited (PDF); never derived here.
        core.store.record_remediation_diffs(scan_id, filename, list(existing) + [
            {"rule_id": (diff_rule_ids or {}).get(a['locator'], diff_rule_id), "before": a["before"], "after": a["after"],
             "note": f"approved by a reviewer · {a['locator']}", **_known_diff_location(a)}
            for a in applied])

        for item_id in review_item_ids:
            core.store.mark_row_applied(item_id)
        if regressions:
            # ONLY here, on the credited path. The two branches above return `working` — the bytes as
            # they were BEFORE this lane — so a regression observed in a write they discarded is
            # evidence about the draft (recorded on its outcome row above) and NOT a fact about the
            # document. Logging or queueing it there would block a file over damage it never took.
            core.store.log_decision(
                "system", "apply.regression", scan_id=scan_id, file=filename,
                detail=f"writing {len(applied)} {noun} value(s) made {regressions} fail on re-scan; "
                       f"none of them failed before the write")
            # The reviewer's way out. Certification is blocked by store.unresolved_regression until
            # one of these is approved; queueing is best-effort because a failed write here must not
            # lose the corrected copy, and the gate fails CLOSED on a missing row rather than open.
            try:
                core.store.queue_regression_review(scan_id, filename, regressions)
            except Exception:
                swallowed("_apply_one_value_kind: queueing the regression review failed", scan_id)
            _model_outcome("verified_regressed",
                           f"cleared on re-scan: {sorted(scs_to_clear)}; newly failing: {regressions}"
                           + unresolved_note,
                           regressions=regressions)
        else:
            _model_outcome("verified_cleared",
                           f"cleared on re-scan: {sorted(scs_to_clear)}" + unresolved_note,
                           regressions=regressions)
        core.store.log_decision(
            "system", "apply.applied", scan_id=scan_id, file=filename,
            detail=f"wrote {len(applied)} reviewer-approved {noun} value(s); "
                   f"{sorted(scs_to_clear)} cleared on re-scan")

    pending_credits.append(commit_credit)
    return fixed, True


@handler("apply_approved_values")
def _apply_approved_values(payload: dict, job: dict, *, _retry_locked=False) -> None:
    """Write reviewer-approved content (alt text, link text, PDF form-field names) into the
    remediated copy, then verify it.

    This closes the remediate → review → publish loop. Approving a 1.1.1, 2.4.4/2.4.9 or 4.1.2
    item used to store the value as evidence and stop: nothing wrote it in, so
    store.mark_file_compliant_if_reviewed correctly refused to certify — leaving the file
    approved but permanently unpublishable.

    Each criterion is its own write → verify → credit lane (_apply_one_value_kind), because a
    lane may only credit what its own re-scan observed. They run in sequence on the same
    `working` bytes, so a file with both alt text and field names approved gets one upload.

    payload: {scan_id, file} — every approved value the file owes, the ordinary approval path.

    payload: {scan_id, file, item_id, approved_binding} — ONE approval, re-attempted
    (store.retry_approved_write). The item narrows every value map and every credit below to
    that row, so a retry requested for one suggestion cannot sweep in whatever else has been
    approved on the document since. `approved_binding` is the source revision, value digest,
    decision version and proposal snapshots the retry was admitted on; it is re-checked HERE,
    against the row as it stands now, because a route-time check is minutes stale by the time a
    worker claims the job. A binding that no longer holds is fatal and writes nothing.
    """
    # Approval coordination shares the established approved-fix worker lane;
    # this phase authorizes exact pending proposals and queues normal file writes.
    if payload.get('phase') == 'approve_current_run_ai':
        allowed = {'phase', 'scan_id', 'owner', 'run_id', 'source_revision'}
        if (set(payload) - allowed or any(not isinstance(payload.get(key), str) or not payload[key]
                                         for key in allowed - {'phase'})):
            raise FatalJobError('Invalid current-run AI approval coordination payload')
        if job.get('scan_id') and job['scan_id'] != payload['scan_id']:
            raise FatalJobError('AI approval coordination scan mismatch')
        return _approve_run_ai(payload, job)
    if payload.get('phase'):
        raise FatalJobError('Unknown approved-fix job phase')
    scan_id = payload.get("scan_id") or job.get("scan_id")
    filename = payload.get("file")
    if not (scan_id and filename):
        raise FatalJobError("apply_approved_values job missing scan_id/file")

    # A retry re-attempts one already-approved row. Revalidate its binding here rather than
    # trusting the admission check: the row can be re-decided, re-proposed or re-remediated
    # between the request and the claim, and this worker is the last place that can refuse.
    only_item_id = payload.get("item_id") or None
    if only_item_id and not _retry_locked:
        with core.store.transaction():
            core.store._get_hitl_item_for_decision(only_item_id)
            suffix = " FOR UPDATE" if core.store._db.supports_for_update else ""
            with core.store._db.cursor() as cur:
                core.store._db.execute(cur,
                    f"SELECT file FROM file_records WHERE scan_id=%s AND file=%s{suffix}",
                    (scan_id, filename))
            return _apply_approved_values(payload, job, _retry_locked=True)
    if only_item_id:
        item = core.store.get_hitl_item(only_item_id)
        binding, refusal = core.store.approved_write_binding(item)
        if (not refusal and (item.get("scan_id") != scan_id or item.get("file") != filename)):
            refusal = "this review item does not belong to the document the job names"
        expected = payload.get("approved_binding")
        if not refusal and (not isinstance(expected, dict) or binding != expected):
            refusal = core.store.RETRY_VALUES_CHANGED
        if refusal:
            core.store.log_decision(
                "system", "apply.retry_refused", scan_id=scan_id, file=filename,
                rule_id=(item or {}).get("rule_id"),
                detail=f"the retry of {only_item_id} was not re-admitted at write time: {refusal}")
            raise FatalJobError(refusal)

    if payload.get('standing_approval'):
        from ai_standing_approval import check_application
        if check_application(core.store, payload):
            from unverified_changes import blocks_certification, record_verification
            if blocks_certification(core.store, scan_id, filename):
                import blob as _blob
                data = _blob.download_remediated(payload['standing_approval']['owner'], scan_id, filename)
                if data:
                    record_verification(core.store, scan_id, filename, data, _verify_residual(data, filename, scan_id=scan_id))
            return
    from ai_standing_approval import check_file_approvals
    check_file_approvals(core.store, scan_id, filename)

    if payload.get("release_intent_id"):
        from release_continuation import check_application
        check_application(core.store, payload["release_intent_id"], scan_id, filename)

    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if ext not in _APPLY_VALUE_EXTS:
        # No applier for this format. Say so rather than silently succeeding: the row stays
        # unapplied, so the file stays out of Publish.
        core.store.log_decision("system", "apply.unsupported", scan_id=scan_id, file=filename,
                                detail=f".{ext}: no approved-value applier for this format")
        return

    # `item_id=only_item_id` is None on the ordinary path (every approved value the file owes)
    # and one row's id on a retry. It is threaded into every map rather than filtered
    # afterwards so there is one place the narrowing happens and no kind can be missed.
    # A row whose every target a DIFFERENT verified fix already removed (1.1.1 alt text for the
    # picture a 1.4.5 replacement deleted) must never reach a writer: its locator names content
    # the document no longer has, and name-based resolution could land on a different picture.
    # Computed once here, re-checked before upload and again under the commit locks below.
    from review_target_reconciliation import removed_item_ids
    target_removed = removed_item_ids(core.store, scan_id, filename)
    approved_rows = core.store._approved_unapplied_rows(scan_id, filename, item_id=only_item_id)
    # WRITER ADMISSION (audit gap 9). An approval whose recorded binding no longer holds — the
    # assessment moved on, its values or its proposals changed since it was given — describes a
    # version of this row nobody approved, and this job used to write it anyway whenever ANY
    # other approval on the file enqueued it. Held here, for every lane at once, by the same
    # exclusion the target-removal narrowing uses. Deliberately NOT by filtering
    # _approved_unapplied_rows: the compliance counters read that, and a held approval is still
    # work the document does not carry. The row stays approved and unapplied, the queue flags it
    # approval_recheck_required, and one safe line (id + reason code, no content) says why.
    stale_held = {item_id: reason for item_id, reason in
                  core.store.approved_write_holds(approved_rows).items()
                  if item_id not in target_removed}
    for held_id in sorted(stale_held):
        held_row = next((r for r in approved_rows if str(r['id']) == held_id), {})
        core.store.log_decision(
            "system", "apply.stale_approval_held", scan_id=scan_id, file=filename,
            rule_id=held_row.get("rule_id"),
            detail=_json.dumps({"item_id": held_id,
                               "refusal": core.store.WRITE_HOLD_CODES.get(stale_held[held_id], "held"),
                               "approval_recheck_required": True}, sort_keys=True))
    excluded = target_removed | set(stale_held)
    candidate_items = {str(r['id']) for r in approved_rows} - excluded
    alt_values = core.store.approved_alt_values(scan_id, filename, item_id=only_item_id,
                                                exclude_item_ids=excluded)
    # Images a reviewer resolved as DECORATIVE. Office only: the marking is an OOXML extLst
    # marker (apply_alt), and the PDF equivalent — re-tagging the figure as an /Artifact — is a
    # structure edit no writer here performs, so on PDF the exception stays a recorded judgement.
    deco_locators = (core.store.approved_decorative_locators(scan_id, filename, item_id=only_item_id,
                                                exclude_item_ids=excluded)
                     if ext in _OFFICE_ALT_MIME else [])
    link_values = (core.store.approved_link_values(scan_id, filename, item_id=only_item_id,
                                                exclude_item_ids=excluded)
                   if ext in _OFFICE_LINK_EXTS else {})
    field_values = (core.store.approved_field_values(scan_id, filename, item_id=only_item_id,
                                                exclude_item_ids=excluded)
                    if ext in _FIELD_NAME_EXTS else {})
    sensory_values = (core.store.approved_sensory_values(scan_id, filename, item_id=only_item_id,
                                                exclude_item_ids=excluded)
                      if ext in _SENSORY_EXTS else {})
    language_values = (core.store.approved_language_values(scan_id, filename, item_id=only_item_id,
                                                exclude_item_ids=excluded)
                       if ext in _LANGUAGE_EXTS else {})
    structure_label_values = (core.store.approved_structure_label_values(
                                  scan_id, filename, item_id=only_item_id,
                                                exclude_item_ids=excluded)
                              if ext in _STRUCTURE_LABEL_EXTS else {})
    image_of_text_values = (core.store.approved_images_of_text_values(
                                scan_id, filename, _IMAGE_OF_TEXT_SCS, item_id=only_item_id,
                                                exclude_item_ids=excluded)
                            if ext in _IMAGE_OF_TEXT_EXTS else {})
    pdf_structure_groups = ({sc: core.store.approved_pdf_structure_values(
                                 scan_id, filename, sc, item_id=only_item_id,
                                                exclude_item_ids=excluded)
        for sc in _PDF_STRUCTURE_SCS} if ext in _PDF_STRUCTURE_EXTS else {})
    if not (alt_values or deco_locators or link_values or field_values
            or sensory_values or language_values or structure_label_values
            or image_of_text_values or any(pdf_structure_groups.values())):
        return                                   # nothing approved awaiting a write

    import blob as _blob
    owner = (core.store.get_scan(scan_id) or {}).get("run", {}).get("owner_email")
    _phase(job, "fetching the corrected copy")
    working = _blob.download_remediated(owner, scan_id, filename)
    prior_record_sha = (core.store.get_file_record(scan_id, filename) or {}).get('corrected_sha256')
    if not working:
        record = core.store.get_file_record(scan_id, filename) or {}
        # An approval may precede the first Remediate run. Start a new corrected
        # copy from the exact assessed bytes; never substitute the original for
        # a previously saved correction that has gone missing.
        if not record.get('remediated_at'):
            from scanner import read_cached_source
            checksum_reader = getattr(core.store, 'get_source_checksum', None)
            checksum = (checksum_reader(scan_id, filename) if callable(checksum_reader) else None) or record.get('checksum')
            working = read_cached_source(scan_id, filename, owner, checksum=checksum)
            if not working and checksum:
                working = read_cached_source(scan_id, filename, owner)
        if not working:
            core.store.log_decision("system", "apply.no_remediated_copy", scan_id=scan_id,
                                    file=filename, detail="no stored corrected copy or assessed source available")
            raise FatalJobError('No corrected copy or assessed source is available. Start remediation again to restore the copy; the approval remains saved.')

    if only_item_id:
        import hashlib
        if hashlib.sha256(working).hexdigest() != binding['corrected_sha256']:
            raise FatalJobError(core.store.RETRY_SOURCE_MOVED)

    if payload.get('standing_approval'):
        from ai_standing_approval import check_application
        check_application(core.store, payload, working=working)

    # The residual of the copy BEFORE anything is written — one extra re-scan per apply job, and
    # the only way a lane can tell a criterion it caused to fail from one that was failing all
    # along. Shared across the lanes: each credited lane advances it to its own re-scan, because
    # its bytes are what the next lane writes on top of. `_verify_residual` never raises (a
    # re-scan that cannot run is Verification(ok=False)), so this cannot block the write.
    _phase(job, "re-scanning the copy before writing (regression baseline)")
    prior_working = working
    residual_state = {"verification": _verify_residual(working, filename, scan_id=scan_id),
                      "retain_unverified": bool(payload.get("standing_approval")),
                      "exclude_item_ids": tuple(sorted(excluded))}
    pending_credits = []
    residual_state['office_retry_allowed'] = bool(
        payload.get('standing_approval') and ext in _OFFICE_ALT_MIME
        and len(alt_values) == 1 and not (deco_locators or link_values or field_values
        or sensory_values or language_values or structure_label_values or image_of_text_values
        or any(pdf_structure_groups.values())))

    # ADR 0055: a described-not-replaced row carries the 1.4.5 card's own 'image N' locator — a
    # media index, which apply_alt cannot read at all (parse_locator requires a '#'). Translate
    # it here, against `working`: these are the bytes about to be written, and a media index
    # resolved against any other copy can name a different picture.
    #
    # ONE LOCATOR BECOMES SEVERAL when the media part is placed more than once, because
    # 'image N' names the part and not a placement. Describing only one of them would leave the
    # others carrying their source filename, 1.1.1 would still fail on re-scan, and the lane
    # would withhold the credit for a write that was actually correct — see
    # apply_pptx_image_of_text.resolve_media_locators, where that was measured.
    #
    # THE TRANSLATION IS PER FORMAT, and that is not a tidy generalisation of the pptx one: a
    # docx places every body picture in one part behind ONE relationship id, so the pptx-shaped
    # 'part#rId' reaches only the first (apply_alt.resolve_target is first-match-wins), and an
    # xlsx writes its relationship targets absolute and its attributes in the other order, so the
    # pptx canonicaliser resolves nothing at all. Both measured on real packages; see
    # apply_office_image_of_text, which owns the docx/xlsx translation and delegates pptx.
    #
    # Non-media locators pass through untouched, and an unresolvable one is LEFT AS IT IS so it
    # reaches apply_alt, is reported unresolved, and appears in the apply.unresolved log under
    # the name the reviewer's card used rather than one they never saw.
    if ext in _DESCRIBED_MEDIA_EXTS and any(_is_media_index_locator(k) for k in alt_values):
        try:
            from apply_office_image_of_text import expand_media_locator_values
            alt_values = expand_media_locator_values(working, alt_values, ext)
        except Exception:
            swallowed("_apply_approved_values: translating the media-index alt locators failed",
                      scan_id)

    # Office images carry part#rId locators written by apply_alt; PDF figures carry the
    # `pdf:fig:{page}:{seq}` locator minted by remediate_pdf and are written by
    # apply_pdf_approved. Same (bytes, {locator: value}) -> (fixed, applied, unresolved)
    # contract either way, so only the writer differs.
    if ext in _PDF_APPLY_EXTS:
        from remediate_pdf import apply_pdf_approved
        alt_write_fn = apply_pdf_approved
    else:
        from apply_alt import apply_alt_text
        # Decorative markings go through the SAME lane as the descriptions, not one of their own.
        # A lane only credits what its own re-scan observed, and a re-scan cannot see 1.1.1 clear
        # while the other lane's images are still unresolved — split in two, each would verify
        # against the other's unfinished work and neither would ever be credited.
        crop_description_plans = {}
        if ext == 'docx':
            from word_crop_description import reviewed_plans
            crop_description_plans = reviewed_plans(core.store, scan_id, filename)
        def alt_write_fn(data, values):
            fixed, written, unresolved = apply_alt_text(data, values, decorative=deco_locators)
            # Verify the narrow description write before any separately approved crop
            # transform or provenance stamp. A presence-only re-scan cannot see lost
            # worksheet values, image bytes, formatting, or the wrong approved text.
            if written and not deco_locators:
                from office_alt_integrity import verify_alt_write
                expected = {item['locator']: values[item['locator']] for item in written
                            if item.get('locator') in values}
                if not verify_alt_write(data, fixed, expected):
                    core.store.log_decision('system', 'apply.integrity_failed', scan_id=scan_id,
                        file=filename, rule_id='1.1.1',
                        detail='Office description readback or package preservation failed; previous copy retained. No visual or semantic certification was granted.')
                    return data, [], list(values)
            if crop_description_plans and written and not unresolved:
                from word_crop_description import write_descriptions
                from apply_office_image_of_text import resolve_media_locators
                targets = resolve_media_locators(data, list(crop_description_plans), 'docx')
                selected = {locator: plan for locator, plan in crop_description_plans.items()
                    if targets.get(locator) and all(values.get(target) == plan['description']
                        for target in targets[locator])}
                fixed = write_descriptions(fixed, selected)
            return fixed, written, unresolved
    working, alt_uploaded = _apply_one_value_kind(
        scan_id=scan_id, filename=filename, working=working,
        values=alt_values, extra_work=bool(deco_locators),
        scs_to_clear={"1.1.1"}, write_fn=alt_write_fn,
        # '1.1.1/described' is credited by this lane because its content IS written by this
        # lane (ADR 0055). Left out, the described row would be written into the document and
        # never marked applied, so count_unapplied_approved_values would count it forever and
        # the file could never certify — the permanently-unpublishable dead end, reached by
        # doing everything else right.
        diff_rule_id="1.1.1", noun="description", job=job,
        credit_rule_ids=("1.1.1", f"1.1.1{core.store.DESCRIBED_RULE_SUFFIX}"),
        residual_state=residual_state, pending_credits=pending_credits,
            only_item_id=only_item_id)

    # 4.1.2 form-field accessible names. PDF keys on `pdf:field:…` and writes /TU; Word keys
    # on `docx:sdt:…` and writes w:alias. One lane, one criterion, the writer chosen by format.
    # Run as its own lane because it verifies and credits a DIFFERENT criterion: folding it
    # into the alt lane would credit 1.1.1 for a field name, and clear 4.1.2 on no evidence.
    field_uploaded = False
    if ext in _FIELD_NAME_EXTS and field_values:
        if ext in _PDF_APPLY_EXTS:
            from remediate_pdf import apply_pdf_approved
            field_write_fn = apply_pdf_approved
        else:
            from apply_field_name import apply_docx_field_name
            field_write_fn = apply_docx_field_name
        working, field_uploaded = _apply_one_value_kind(
            scan_id=scan_id, filename=filename, working=working,
            values=field_values, scs_to_clear={"4.1.2"}, write_fn=field_write_fn,
            diff_rule_id="4.1.2", credit_rule_ids=("4.1.2",), noun="field name", job=job,
            residual_state=residual_state, pending_credits=pending_credits,
            only_item_id=only_item_id)

    link_uploaded = False
    if link_values:
        from apply_link_text import apply_link_text
        link_write_fn = lambda data, values: apply_link_text(data, ext, values)  # noqa: E731
        # Every approved link value is WRITTEN whichever criterion it came from — the text is
        # better either way. Only the crediting is narrowed to what this format can re-verify.
        link_scs = _LINK_SCS_BY_EXT.get(ext, ())
        working, link_uploaded = _apply_one_value_kind(
            scan_id=scan_id, filename=filename, working=working,
            values=link_values, scs_to_clear=set(link_scs), write_fn=link_write_fn,
            diff_rule_id="2.4.4", credit_rule_ids=link_scs, noun="link text", job=job,
            residual_state=residual_state, pending_credits=pending_credits,
            only_item_id=only_item_id)

    # 1.3.3 sensory rewrites and 3.1.2 language marks (Word). Two lanes, not one, even though a
    # single module writes both: each lane may only credit the criterion its OWN re-scan saw
    # clear, and folding them together would credit 1.3.3 for a language mark.
    sensory_uploaded = False
    if sensory_values:
        from apply_text_values import apply_sensory_rewrite
        sensory_write_fn = lambda d, v: apply_sensory_rewrite(d, ext, v)  # noqa: E731
        working, sensory_uploaded = _apply_one_value_kind(
            scan_id=scan_id, filename=filename, working=working,
            values=sensory_values, scs_to_clear={"1.3.3"}, write_fn=sensory_write_fn,
            diff_rule_id="1.3.3", credit_rule_ids=("1.3.3",), noun="rewrite", job=job,
            residual_state=residual_state, pending_credits=pending_credits,
            only_item_id=only_item_id)

    language_uploaded = False
    if language_values:
        if ext == "pdf":
            from pdf_structural_language import apply_pdf_structure_language
            language_write_fn = apply_pdf_structure_language
        else:
            from apply_text_values import apply_language_parts
            language_write_fn = lambda d, v: apply_language_parts(d, ext, v)  # noqa: E731
        working, language_uploaded = _apply_one_value_kind(
            scan_id=scan_id, filename=filename, working=working,
            values=language_values, scs_to_clear={"3.1.2"}, write_fn=language_write_fn,
            diff_rule_id="3.1.2", credit_rule_ids=("3.1.2",), noun="language mark", job=job,
            residual_state=residual_state, pending_credits=pending_credits,
            only_item_id=only_item_id)

    structure_label_uploaded = False
    if structure_label_values:
        if ext == "pptx":
            from apply_pptx_slide_titles import apply_pptx_slide_titles
            _struct_write_fn = apply_pptx_slide_titles
        else:
            from apply_xlsx_labels import apply_xlsx_labels
            _struct_write_fn = apply_xlsx_labels
        working, structure_label_uploaded = _apply_one_value_kind(
            scan_id=scan_id, filename=filename, working=working,
            values=structure_label_values, scs_to_clear={"2.4.6"},
            write_fn=_struct_write_fn,
            diff_rule_id="2.4.6", credit_rule_ids=("2.4.6",),
            noun="structure label", job=job,
            residual_state=residual_state, pending_credits=pending_credits,
            only_item_id=only_item_id)

    # 1.4.5 images of text. The approved transcript replaces the picture with a real text box and
    # the image is deleted, so the words become selectable, resizable text and the raster the
    # detector reads is gone. The writer refuses — returning the locator as unresolved, so no
    # credit is given — when the image is referenced by a layout or master, sits in a group, or
    # has no geometry of its own; see apply_pptx_image_replacement for why each is unwritable.
    #
    # scs_to_clear is 1.4.5 alone even though deleting the image also clears 1.4.9 for that
    # picture: a deck with other images of text still fails 1.4.9, and that must not withhold
    # credit for the 1.4.5 the reviewer actually fixed.
    image_of_text_uploaded = False
    if image_of_text_values:
        image_replacement_refusal = None
        if ext == 'pptx':
            from apply_pptx_image_replacement import apply_pptx_image_replacement
            image_replacement_writer = apply_pptx_image_replacement
        else:
            from apply_office_image_replacement import (
                apply_office_image_replacement, office_image_replacement_refusal)
            image_replacement_refusal = lambda data, locator: office_image_replacement_refusal(data, ext, locator)
            image_replacement_writer = lambda data, values: apply_office_image_replacement(data, ext, values)
        working, image_of_text_uploaded = _apply_one_value_kind(
            scan_id=scan_id, filename=filename, working=working,
            values=image_of_text_values, scs_to_clear={"1.4.5"},
            write_fn=image_replacement_writer,
            diff_rule_id="1.4.5", credit_rule_ids=_IMAGE_OF_TEXT_SCS,
            noun="image-of-text replacement", job=job,
            refusal_reason_fn=image_replacement_refusal,
            residual_state=residual_state, pending_credits=pending_credits,
            only_item_id=only_item_id)

    pdf_structure_uploaded = False
    if ext in _PDF_STRUCTURE_EXTS and any(pdf_structure_groups.values()):
        # All plans share one exact tree anchor. Apply them together so one approved
        # edit cannot silently invalidate the next plan's identity.
        from pdf_structure_repairs import apply_pdf_structure_repairs
        structure_values = {loc: value for group in pdf_structure_groups.values() for loc, value in group.items()}
        rules = {loc: sc for sc, group in pdf_structure_groups.items() for loc in group}
        criteria = tuple(sc for sc, group in pdf_structure_groups.items() if group)
        # An explicitly approved, source-bound tag edit can be retained even when
        # broad structural semantics still need human review. The writer verifies
        # its exact target and preservation; this does not earn resolved credit.
        structure_state = {**residual_state, 'retain_unverified': True}
        working, pdf_structure_uploaded = _apply_one_value_kind(
            scan_id=scan_id, filename=filename, working=working, values=structure_values,
            scs_to_clear=set(criteria) & set(_PDF_STRUCTURE_SCS), write_fn=apply_pdf_structure_repairs,
            diff_rule_id=criteria[0], diff_rule_ids=rules,
            credit_rule_ids=tuple(sorted(set(criteria) & set(_PDF_STRUCTURE_SCS))),
            noun='PDF tag structure', job=job, residual_state=structure_state,
            pending_credits=pending_credits, only_item_id=only_item_id)
        if pdf_structure_uploaded:
            residual_state['verification'] = structure_state['verification']

    if not (alt_uploaded or link_uploaded or field_uploaded
            or sensory_uploaded or language_uploaded or structure_label_uploaded
            or image_of_text_uploaded or pdf_structure_uploaded):
        return

    if payload.get("release_intent_id"):
        check_application(core.store, payload["release_intent_id"], scan_id, filename)
    if payload.get('standing_approval'):
        from ai_standing_approval import check_application as check_standing_application
        check_standing_application(core.store, payload)
    check_file_approvals(core.store, scan_id, filename)
    def _removed_since_read():
        # A reconciliation that committed while this job was writing: never save or credit a
        # value for a row whose target it proved gone. Raised (retryable), so the next attempt
        # re-reads the rows and simply leaves that one out.
        late = removed_item_ids(core.store, scan_id, filename) & candidate_items
        if late:
            raise RuntimeError('review target removed by a verified fix during this write: '
                               + ', '.join(sorted(late)))

    def _binding_moved_since_read(locked_rows=None):
        # The same for an approval whose binding moved while this job was writing (a
        # re-assessment, a re-decision, replaced proposals). Under the commit locks the rows are
        # the ones lock_rows just read FOR UPDATE; before that, a fresh read. Retryable too: the
        # next attempt holds that row at admission and writes the rest.
        rows = (locked_rows if locked_rows is not None
                else {i: core.store.get_hitl_item(i) for i in candidate_items})
        moved = set(core.store.approved_write_holds(
            [rows.get(i) for i in sorted(candidate_items)])) & candidate_items
        # A row re-decided (rejected, skipped, reopened) or applied by another job since it was
        # read is no longer an approval awaiting THIS write, and must not be credited by it.
        moved |= {i for i in candidate_items
                  if not rows.get(i) or str(rows[i].get('status') or '') != 'approved'
                  or rows[i].get('applied')}
        if moved:
            raise RuntimeError('approval binding changed during this write: '
                               + ', '.join(sorted(moved)))
    _removed_since_read()
    _binding_moved_since_read()
    _phase(job, "storing the corrected copy")
    retry_proof = residual_state.get('office_retry')
    blob_url = (_blob.upload_immutable_retry(owner, scan_id, filename, working,
                    _OFFICE_ALT_MIME.get(ext, 'application/pdf')) if retry_proof
                else _blob.upload_remediated(owner, scan_id, filename, working,
                    _OFFICE_ALT_MIME.get(ext, 'application/pdf')))
    if not blob_url:
        raise RuntimeError("approved values were verified but durable storage is unavailable")
    # Upload first: failed storage must leave every approval retryable. The database commit
    # then binds its evidence to these exact bytes; no credit survives a metadata failure.
    record = core.store.get_remediation_urls(scan_id, filename) or {}
    from review_target_reconciliation import begin_write, lock_file, lock_rows
    with core.store.transaction():
        # Documented lock order: review rows (id order), THEN the file. Retry admission and
        # target reconciliation take the same order, so none of them can deadlock another.
        begin_write(core.store)
        locked_rows = lock_rows(core.store, candidate_items | {str(item['id']) for item in
                  ((payload.get('standing_approval') or {}).get('items') or []) if item.get('id')})
        lock_file(core.store, scan_id, filename)
        _removed_since_read()
        if retry_proof:
            # The upload is outside SQL: an intervening review/revocation must not
            # advance its pointer or inherit credit. Match decide_hitl's review-before-
            # stage lock order, then authorize and recheck under the same transaction.
            if getattr(core.store._db, 'supports_for_update', False):
                with core.store._db.cursor() as cur:
                    for expected in sorted(payload['standing_approval']['items'], key=lambda row: row['id']):
                        core.store._db.execute(cur,
                            'SELECT id FROM hitl_queue WHERE id=%s AND scan_id=%s AND file=%s FOR UPDATE',
                            (expected['id'], scan_id, filename))
                        core.store._db.fetchone(cur)
            from ai_standing_approval import authorization as retry_authorization, _source as retry_source
            revision = retry_authorization(core.store, owner, scan_id, retry_proof['run_id'])
            stage = core.store.get_stage_execution(retry_proof['run_id'], owner=owner) or {}
            from ai_run_approval_override import read as read_retry_consent
            if read_retry_consent(core.store, owner, scan_id, retry_proof['run_id'])['revision'] != retry_proof['consent_revision']:
                raise ValueError('office_retry_consent_changed')
            if check_standing_application(core.store, payload) is True:
                raise ValueError('office_retry_approval_already_applied')
            check_file_approvals(core.store, scan_id, filename)
            from worker import check_cancel as check_retry_cancel
            check_retry_cancel()
            if (revision != retry_proof['source_revision'] or not stage.get('is_current')
                    or stage.get('cancel_requested_at')
                    or stage.get('state') not in {'accepted', 'queued', 'processing', 'processing_complete', 'succeeded'}
                    or stage.get('input_snapshot_id') != revision):
                raise ValueError('office_retry_run_changed')
            retry_source(core.store, owner, scan_id, filename, retry_proof['run_id'])
            if not core.store.compare_and_set_retry_artifact(owner, scan_id, filename,
                    previous_sha256=retry_proof['previous_artifact_sha256'],
                    source_identity=retry_proof['source_identity'], run_id=retry_proof['run_id'],
                    source_revision=retry_proof['source_revision'], blob_url=blob_url,
                    corrected_sha256=_hashlib.sha256(working).hexdigest(), corrected_bytes=len(working)):
                raise ValueError('office_retry_source_or_artifact_changed')
        else:
            core.store.record_remediation(
                scan_id, filename, drive_write_url=record.get("drive_write_url"),
                blob_url=blob_url, corrected_sha256=_hashlib.sha256(working).hexdigest(),
                corrected_bytes=len(working))
        # Still inside the commit transaction and before any credit: a moved binding rolls back
        # the pointer update above with everything else. After the office-retry block so that
        # path keeps its own exact-authority refusals (office_retry_*), which cover the same rows.
        _binding_moved_since_read(locked_rows)
        for commit_credit in pending_credits:
            commit_credit()
        if payload.get("release_intent_id"):
            import release_continuation_store
            release_continuation_store.artifact(core.store, payload["release_intent_id"], owner,
                                                 filename, _hashlib.sha256(working).hexdigest())

        # Keep certification in the same transaction: a failed reconciliation must leave
        # approvals pending for retry, rather than returning early next time with no work.
        if core.store.mark_file_compliant_if_reviewed(scan_id, filename):
            core.store.log_decision(
                "system", "revalidate.certified", scan_id=scan_id, file=filename,
                detail="all findings resolved (auto-fixed + approved values written) — advanced to Publish")

    # Trigger (a): with the new copy committed, retire review rows whose targets THIS job's
    # verified write removed, against its own complete re-scan of exactly these bytes. Writes
    # decision_log + finding_disposition only; best-effort, the saved copy is already durable.
    try:
        from review_target_reconciliation import reconcile_after_apply
        reconcile_after_apply(core.store, scan_id, filename, owner=owner, prior=prior_working,
                              prior_sha256=prior_record_sha, corrected=working,
                              verification=residual_state.get('verification'))
    except Exception:
        swallowed("_apply_approved_values: reconciling removed review targets failed", scan_id)


@handler("deliver_corrected_copy")
def _deliver_corrected_copy(payload: dict, job: dict) -> None:
    """Re-send ONE already-verified corrected copy to its source provider. Fixes nothing.

    This is the worker behind PRD §11's "retry delivery only". Read what it does NOT do first,
    because that is the contract: it does not open the source document, does not run a fixer,
    does not re-verify, and does not touch `applied_fixes`, `remediation_diff`, `hitl_queue` or
    `file_records.remediated_at`. A delivery failure must not reduce the applied or verified
    counts, and the way that is guaranteed is that nothing on this path can write them.

    The route has already gated the request (owner, capability, artifact provenance, destination)
    and taken the idempotency claim. This re-checks the artifact against its digest anyway —
    see remediation_delivery.load_artifact for why once is not enough — and then makes exactly
    one provider write.

    THE CLAIM IS ALWAYS CLOSED. Every exit below finishes the `remediation_delivery` row, because
    a row left `in_flight` refuses every future retry of that artifact with `retry_in_flight` and
    nothing would ever clear it. A refusal closes it as refused, a provider error as failed.
    """
    import remediation_delivery as _delivery
    import remediation_exceptions as _exceptions

    scan_id = payload.get("scan_id")
    filename = payload.get("file")
    key = payload.get("idempotency_key")
    digest = payload.get("artifact_digest")
    destination = payload.get("destination") or {}
    provider = (destination.get("provider") or payload.get("provider") or "").lower()
    owner = payload.get("owner")
    actor = payload.get("actor")
    if not (scan_id and filename and key):
        raise FatalJobError("deliver_corrected_copy job missing scan_id/file/idempotency_key")

    import blob as _blob

    def _refuse(code: str) -> None:
        core.store.finish_delivery(key, status="refused", error=code)
        _rem_event(scan_id, "remediate.delivery_retry_refused", job, filename,
                   destination=provider or None, reason=code)
        core.store.log_decision(actor or "system", "remediate.delivery_retry_refused",
                                scan_id=scan_id, file=filename,
                                detail=_audit_detail(_exceptions.audit_payload(
                                    actor=actor, run_id=scan_id, file=filename,
                                    action="retry_delivery", outcome="refused",
                                    destination=destination, idempotency_key=key, reason=code)))

    try:
        data = _delivery.load_artifact(owner=owner, scan_id=scan_id, file=filename,
                                       expected_digest=digest or "",
                                       download=_blob.download_remediated)
    except _delivery.DeliveryRefused as refused:
        _refuse(refused.code)
        return

    _phase(job, "delivering the corrected copy")
    try:
        url = _delivery.perform_delivery(
            provider=provider, destination=destination, filename=filename, data=data,
            drive_client=_drive_client(payload["drive_token"]) if payload.get("drive_token")
            else None,
            graph_token=payload.get("sp_token"))
    except _delivery.DeliveryRefused as refused:
        _refuse(refused.code)
        return
    except Exception as exc:      # noqa: BLE001 — a provider failure is an outcome, not a crash
        core.store.finish_delivery(key, status="failed", error=f"{type(exc).__name__}: {exc}")
        _rem_event(scan_id, "remediate.delivery_failed", job, filename,
                   destination=provider or None)
        core.store.log_decision(actor or "system", "remediate.delivery_retry_failed",
                                scan_id=scan_id, file=filename,
                                detail=_audit_detail(_exceptions.audit_payload(
                                    actor=actor, run_id=scan_id, file=filename,
                                    action="retry_delivery", outcome="failed",
                                    destination=destination, idempotency_key=key,
                                    reason=type(exc).__name__)))
        raise

    # ONE COLUMN. record_remediation would restamp `remediated_at` and make a week-old
    # correction look like it was produced now — see store.record_delivery_url.
    if url:
        core.store.record_delivery_url(scan_id, filename, url)
    core.store.finish_delivery(key, status="delivered", delivered_url=url)
    _rem_event(scan_id, "remediate.delivered", job, filename, destination="provider")
    core.store.log_decision(actor or "system", "remediate.delivery_retry_delivered",
                            scan_id=scan_id, file=filename,
                            detail=_audit_detail(_exceptions.audit_payload(
                                actor=actor, run_id=scan_id, file=filename,
                                action="retry_delivery", outcome="delivered",
                                destination=destination, idempotency_key=key)))


def _audit_detail(payload: dict) -> str:
    """A bounded audit payload as the one string log_decision stores.

    JSON rather than prose so the fields stay machine-readable, and truncated to the column's
    own limit here rather than by whatever the database does silently. The payload is already a
    whitelist (remediation_exceptions.audit_payload); this only decides how it is spelled.
    """
    import json as _json
    try:
        return _json.dumps(payload, sort_keys=True)[:400]
    except (TypeError, ValueError):
        return str(payload)[:400]


@handler("release_continue")
def _release_continue(payload: dict, job: dict) -> None:
    if payload.get("mode") == "automatic":
        from automatic_release import advance
        return advance(core.store, payload, job)
    from release_continuation import advance
    advance(core.store, payload, job)


def _approve_run_ai(payload: dict, job: dict) -> None:
    from ai_run_approval_override import process_pending
    try:
        process_pending(core.store, payload)
    except ValueError as exc:
        raise FatalJobError(str(exc)) from exc

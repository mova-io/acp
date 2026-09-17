"""Human-in-the-loop review queue endpoints."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import Response
from pydantic import BaseModel

import core
from store import Store
from swallowed import swallowed

router = APIRouter()


class HitlUpdate(BaseModel):
    status: str                     # pending | approved | rejected | skipped
    reviewer_note: str | None = None
    approved_value: str | None = None   # AI-drafted or hand-edited final text (alt/link text)
    edited: bool = False                # reviewer changed the AI draft before approving (calibration signal)
    review_ms: int | None = None        # client-measured time from card-open to decision (reviewer-time metric)
    ai_value: str | None = None         # the AI-proposed value shown, so we store proposed-vs-final
    model_call_id: str | None = None    # exact ai_calls row reviewed; absent for human-authored work
    # One exact producing call per proposal/evidence value, positionally aligned with
    # approved_values. Multi-image cards contain independent vision calls, so collapsing them
    # into model_call_id would either discard attribution or invent a single producer.
    model_call_ids: list[str | None] | None = None
    # One final text per proposal, positionally: the row holds N proposals (one per image) and
    # a single approved_value could never describe ten different pictures. An entry that is
    # null/"" accepts that proposal's own draft, so approving an unedited card means exactly
    # "the drafts I was shown are correct". Omit entirely for a judgement finding.
    approved_values: list[str | None] | None = None
    # Reviewer Feedback Intelligence: WHY a rejection happened. A fixed vocabulary so the
    # rollup can answer "which rules are weakest" — free text goes in reviewer_note.
    reject_reason: str | None = None    # incorrect_object | too_vague | hallucinated | missed_text | org_preference | other
    # WCAG exception the reviewer applied INSTEAD of authoring a fix — the honest resolution for a
    # finding a model can't decide. 'decorative' (1.1.1: the image conveys nothing, so no text
    # alternative is required) or 'essential_exception' (1.4.5/1.4.9: a logo/brand mark is exempt
    # from the images-of-text rule). Recorded in the immutable audit trail as WHY the finding was
    # resolved, so the certification report never implies a written fix that never happened.
    # 'described_not_replaced' (ADR 0055) is the exception that DOES carry text: see RESOLUTIONS.
    resolution: str | None = None       # decorative | essential_exception | described_not_replaced | out_of_scope
    request_id: str | None = None       # stable across transport retries of one decision
    expected_version: int | None = None # row version the reviewer actually saw
    expected_proposal_snapshot_ids: list[str] | None = None
    expected_source_revision: str | None = None


REJECT_REASONS = {"incorrect_object", "too_vague", "hallucinated", "missed_text", "org_preference", "other", "unspecified"}
# Human-judgment exceptions that resolve a finding without a written value. Each maps to the WCAG
# clause that makes the exception legitimate — kept beside the enum so the audit note is honest.
RESOLUTIONS = {
    "decorative": "reviewer marked image decorative — no text alternative required (WCAG 1.1.1)",
    "essential_exception": "reviewer marked essential logo/brand mark — exempt from images-of-text (WCAG 1.4.5/1.4.9)",
    # ADR 0055. The odd one out among the exceptions, and the difference is worth stating: this
    # one is only honest once a write lands. The reviewer keeps an image of text — because the
    # replacement writer refuses it, because the styling carries meaning, or because it is a
    # 1.4.9 chart replacement would gut — and describes it instead. That resolves 1.4.5/1.4.9 by
    # judgement AND creates a 1.1.1 obligation the document did not have, so store.
    # queue_described_image_alt records the description as alt text owed and the file cannot
    # certify until it is written and a re-scan confirms 1.1.1 cleared. Without that second
    # half the description would be stored, reach nothing, and the file would certify as
    # conformant with the image untouched and undescribed.
    Store.DESCRIBED_RESOLUTION:
        "reviewer kept the image of text and described it instead of replacing it — "
        "resolves images-of-text by judgement (WCAG 1.4.5/1.4.9), owes alt text (WCAG 1.1.1)",
    # Unlike the two above (which RESOLVE a finding that IS in scope, so it stays a human_verified
    # pass), out_of_scope means the criterion does not APPLY to this document — the reviewer's
    # judgement that it is not applicable. It leaves the coverage denominator (accessibility_status
    # counts it as its own `not_applicable` bucket, outside in_scope), like an N/A cell on the matrix.
    "out_of_scope": "reviewer marked finding not applicable / out of scope for this document",
}


def _request_owner(request: Request | None) -> str | None:
    """Authenticated owner stamped by the access gate; absent only in demo/direct calls."""
    return getattr(getattr(request, "state", None), "user_email", None)


def _owned_item(item_id: str, request: Request | None) -> dict:
    """Resolve a queue row only when it belongs to the authenticated user's scan.

    The list endpoint has always been owner-scoped, but the item endpoints historically looked
    rows up by their opaque id alone. An opaque id is not authorization: assignment, decisions,
    and companion downloads must enforce the same boundary as the inbox that links to them.
    """
    item = core.store.get_hitl_item(item_id)
    owner = _request_owner(request)
    # Direct in-process callers used by remediation tests pass a small request-shaped object and
    # are not an HTTP authorization boundary. Real FastAPI requests always enforce ownership.
    if item is None or (isinstance(request, Request) and owner
                        and core.store.get_scan(item.get("scan_id"), owner=owner) is None):
        raise HTTPException(404, "item not found")
    return item


@router.post("/hitl/queue/{scan_id}/auto")
def hitl_auto_queue(scan_id: str, request: Request):
    """Auto-populate the HITL review queue from ai-assisted FAILs in an existing scan.

    Idempotent — safe to call multiple times. Returns the newly created items.
    Fires a webhook (HITL_WEBHOOK_URL) if configured.
    """
    if core.store.get_scan(scan_id, owner=_request_owner(request)) is None:
        raise HTTPException(404, "scan not found")
    created = core.store.queue_hitl_items(scan_id)
    created.extend(core.store.reconcile_completed_remediation_reviews(scan_id))
    core.fire_webhook(created)
    return {"queued": len(created), "items": created}


@router.post("/hitl/queue/{scan_id}/verify")
def hitl_verify_queue(scan_id: str, request: Request, file: str = Query(...)):
    """Queue a post-fix VERIFICATION item for one fully-automatic remediation
    (user decision 2026-07-02: automatic fixes also get human review). The
    ai-assisted pull above never sees auto-mode rules, so this is the only
    path that puts a fully-automatic fix in front of a person. Idempotent per
    (scan, file) — repeat clicks of remediate-now never duplicate the item."""
    if core.store.get_scan(scan_id, owner=_request_owner(request)) is None:
        raise HTTPException(404, "scan not found")
    item_id = core.store.queue_hitl_deferral(
        scan_id, file, "Automatic fix applied — verify the result", 1, rule_id="auto/verify")
    if item_id:
        core.fire_webhook([{"id": item_id, "scan_id": scan_id, "file": file,
                            "rule_id": "auto/verify", "status": "pending"}])
    return {"queued": 0 if item_id is None else 1, "id": item_id}


@router.get("/hitl/queue")
def hitl_list(request: Request, status: str | None = None, scan_id: str | None = None,
              include_superseded: bool = False):
    """List HITL review items, scoped to the signed-in user's own documents. Filter by
    status (pending/approved/rejected/skipped) or scan_id.

    Superseded items — queued while a finding failed, and since re-verified to PASS or
    NOT_EVALUATED — are omitted, so the inbox count is work outstanding rather than work ever
    queued. include_superseded=true returns them as well, each flagged `superseded`; that is
    the audit view of everything this scan ever asked a human to look at."""
    owner = getattr(request.state, "user_email", None)
    rows = core.store.list_hitl_queue(status=status, scan_id=scan_id, owner=owner,
                                      include_superseded=include_superseded)
    # Match the sealed assessment input used by remediation; legacy runs use the scan hash.
    from review_item_kind import serialize_review_item
    rows = [serialize_review_item(row) for row in rows]
    from automatic_review_queue import annotate
    rows = annotate(core.store, rows, owner)
    revisions = {}
    for row in rows:
        sid = row.get("scan_id")
        if sid and sid not in revisions:
            revisions[sid] = core.store.remediation_source_revision(sid)
        row["source_revision"] = revisions.get(sid)
    return rows


@router.get("/hitl/analytics")
def hitl_metrics(request: Request, scan_id: str | None = None):
    """Human-review telemetry for the Intelligent Review Workspace dashboard — decisions by
    action, approval rate, edit rate (confidence-calibration signal), and average review time
    (the headline metric: reviewer time eliminated). Scoped to one scan when scan_id is given,
    and in every case to the signed-in user's own scans.

    THE OWNER SCOPE IS NOT ONLY ON THE scan_id BRANCH. It used to be: a scan_id was checked
    against get_scan(owner=...), and omitting the parameter skipped the check and aggregated
    every tenant's decisions into one answer. That is a cross-tenant read reachable by any
    signed-in user, and the numbers it returns are not the caller's own either — the workspace
    asks this question about the caller's reviews, so an unscoped total is wrong even where it
    is permitted."""
    owner = getattr(request.state, "user_email", None)
    if scan_id is not None and core.store.get_scan(scan_id, owner=owner) is None:
        raise HTTPException(404, "scan not found")
    return core.store.hitl_analytics(scan_id, owner=owner)


@router.put("/hitl/queue/{item_id}")
def hitl_update(item_id: str, body: HitlUpdate, request: Request = None):
    """Update a HITL review item status (approve, reject, skip) with an optional reviewer note."""
    item = _owned_item(item_id, request)
    valid = {"pending", "approved", "rejected", "skipped", "in_review"}
    if body.status not in valid:
        raise HTTPException(422, f"status must be one of {sorted(valid)}")
    # in_review is a lightweight "I am working this" claim — no decision, no reviewed_at,
    # no side-effects. Return early so the certify gate, job queue, and telemetry paths
    # (which all assume a terminal decision) never see it.
    if body.status == "in_review":
        actor = getattr(getattr(request, "state", None), "user_email", None)
        return core.store.claim_hitl_item(item_id, actor)
    if body.reject_reason is not None and body.reject_reason not in REJECT_REASONS:
        raise HTTPException(422, f"reject_reason must be one of {sorted(REJECT_REASONS)}")
    if body.resolution is not None and body.resolution not in RESOLUTIONS:
        raise HTTPException(422, f"resolution must be one of {sorted(RESOLUTIONS)}")
    # ADR 0055: describe-instead-of-replace is the one resolution that is incomplete without
    # text, so it is refused without text — here, BEFORE anything is written, rather than
    # discovered after the row has been updated. Both halves are checked because both halves
    # can be wrong on their own: the criterion, because "I kept it and described it" means
    # nothing on a link-text or contrast row; and the descriptions, because a resolution with
    # none of them resolves the image-of-text finding while leaving the images undescribed and
    # letting the file certify that way. That is the silent failure the whole feature closes,
    # so the request fails loudly instead.
    if body.resolution == Store.DESCRIBED_RESOLUTION:
        rule_id = str(item.get("rule_id") or "").strip()
        if rule_id not in Store.DESCRIBED_SOURCE_SCS:
            raise HTTPException(
                422, f"{Store.DESCRIBED_RESOLUTION} applies to an images-of-text finding "
                     f"({', '.join(Store.DESCRIBED_SOURCE_SCS)}), not to {rule_id or 'this row'}")
        if not any(str(v or "").strip() for v in (body.approved_values or [])):
            raise HTTPException(
                422, f"{Store.DESCRIBED_RESOLUTION} needs a description for at least one image: "
                     "keeping an image of text without describing it leaves it unreadable to a "
                     "screen reader and resolves nothing")
        # THE ROW must have somewhere to put them, and checking only the REQUEST was not enough.
        # A 1.4.5 row carrying no proposals is the normal shape from two production writers —
        # store.queue_hitl_items (deterministic mode) and handlers.queue_hitl_review_for_file —
        # and propose_images_of_text returns [] whenever OCR is unavailable OR times out, so a
        # scan can report 1.4.5 while the card has no per-image slots at all.
        #
        # Approving one of those used to leave the worst state this feature exists to prevent:
        # approve_proposal_values wrote nothing, queue_described_image_alt found no values and
        # returned None, and the 500 fired AFTER update_hitl_item had already stamped the row
        # approved with the resolution — and BEFORE log_decision, so the file certified 100/100
        # with the images untouched, undescribed, and no audit line saying who resolved it or why.
        # Deterministic on retry, too: it 500s forever while the row stays approved.
        # AND THE IMAGES THEY DESCRIBED MUST BE WRITABLE. `ocr._ooxml_images` walks the whole ZIP
        # namelist while the appliers reach only the parts in formats/office/images.ALT_TARGETS,
        # so a media part no alt-bearing part references — a Word footnote image, a VML sheet
        # graphic — raises 1.4.5 and mints a card that no writer can action. Accepting a described
        # decision on one recorded an obligation nothing could meet: the row stayed approved and
        # unapplied forever, count_unapplied_approved_values counted it forever, and the file
        # could never certify. #1767 made that visible; this is what stops it being accepted.
        #
        # Refused when ANY described image is unreachable, not only when all are. A partial
        # acceptance would resolve the 1.4.5 finding for every image while wedging the file on
        # the one that cannot be written — the same dead end, reached by a narrower door. The
        # reviewer describes the ones that can be written, or resolves this row another way.
        #
        # `describable` absent means UNKNOWN, and unknown does not refuse: rows enqueued before
        # handlers._mark_describable existed carry no flag, and refusing them would break
        # decisions that work today. They keep the pre-#1767 behaviour, which is now at least
        # visible rather than silent.
        supplied = body.approved_values or []
        unwritable = []
        for i, p in enumerate(item.get("proposals") or []):
            if not isinstance(p, dict) or p.get("describable") is not False:
                continue                      # reachable, or unknown (see above)
            described = str((supplied[i] if i < len(supplied) else "") or "").strip()
            if described:
                unwritable.append(str(p.get("locator") or "").strip() or f"image {i + 1}")
        if unwritable:
            raise HTTPException(
                422, f"{Store.DESCRIBED_RESOLUTION} cannot be applied to "
                     f"{', '.join(unwritable)}: nothing in this document references "
                     "that image from a part any writer can reach, so a description would be "
                     "recorded and never written. Describe the images that can be written, or "
                     "resolve this finding another way.")
        if not [p for p in (item.get("proposals") or [])
                if isinstance(p, dict) and str(p.get("locator") or "").strip()]:
            raise HTTPException(
                422, f"{Store.DESCRIBED_RESOLUTION} needs the per-image cards this row does not "
                     "carry — there is nowhere to attach a description. Re-run remediation to "
                     "draft them, or resolve this finding another way")
    submitted_call_ids = ([body.model_call_id] if body.model_call_id else [])
    submitted_call_ids.extend(call_id for call_id in (body.model_call_ids or []) if call_id)
    if body.model_call_ids is not None:
        instances = item.get("proposals") or item.get("evidence") or []
        if len(body.model_call_ids) != len(instances):
            raise HTTPException(422, "model_call_ids must align with this review item's values")
        for index, instance in enumerate(instances):
            recorded = instance.get("model_call_id")
            if recorded and body.model_call_ids[index] != recorded:
                raise HTTPException(422, "model_call_id does not match the generated review value")
    if any(not core.store.ai_call_belongs_to_file(
            call_id, item.get("scan_id"), item.get("file")) for call_id in submitted_call_ids):
        raise HTTPException(422, "model_call_id does not belong to this review item")
    # Immutable audit trail: WHO decided what, when, on which finding — include the
    # approved value itself so the log is self-sufficient compliance evidence.
    #
    # The actor is the authenticated reviewer's email, not a generic "reviewer" label: a
    # certification report that says a human signed off must be able to say WHICH human, or
    # the chain of custody is unattributable. Falls back to the literal 'reviewer' when there
    # is no authenticated identity (the demo/SSO-less path, and direct in-process callers for
    # whom `request` is None) — we record what we actually know and never invent a name.
    actor = getattr(getattr(request, "state", None), "user_email", None) or "reviewer"
    _detail = body.reviewer_note or None
    # A WCAG-exception resolution IS the evidence for this finding — record it verbatim, so an
    # auditor reading the log sees the finding was resolved by human judgment (not a written fix).
    if body.resolution:
        _detail = f"{_detail + ' | ' if _detail else ''}resolution: {RESOLUTIONS[body.resolution]}"
    if body.approved_value:
        _detail = f"{_detail + ' | ' if _detail else ''}approved: {body.approved_value[:160]}"
    try:
        updated, replayed = core.store.complete_hitl_decision(
            item_id, body.status, body.reviewer_note, body.approved_value,
            resolution=body.resolution, approved_values=body.approved_values,
            actor=actor, detail=_detail, request_id=body.request_id,
            expected_version=body.expected_version,
            expected_proposal_snapshot_ids=body.expected_proposal_snapshot_ids,
            expected_source_revision=body.expected_source_revision)
    except ValueError as exc:
        if str(exc) in {"stale decision version", "stale proposal selection", "stale source revision",
                        "decision request id was reused with a different payload"}:
            raise HTTPException(409, str(exc))
        if str(exc) != "described decision produced no alt-text obligation":
            raise
        raise HTTPException(500, "the descriptions could not be recorded as alt text; "
                                 "the decision was not completed and the finding is unchanged")
    if replayed:
        return updated
    # Review telemetry (Intelligent Review Workspace): one event per decision so we can
    # report reviewer time saved + calibrate confidence from the edit/reject signal.
    # Best-effort — never blocks the review.
    try:
        _action = ("edit" if (body.status == "approved" and body.edited)
                   else {"approved": "approve", "rejected": "reject", "skipped": "skip"}.get(body.status, body.status))
        event_kwargs = {
            "review_ms": body.review_ms,
            "reviewer": (getattr(request.state, "user_email", None) if request is not None else None),
            "proposal_snapshot_ids": updated.get("approved_proposal_snapshot_ids")
                or updated.get("proposal_snapshot_ids"),
            "source_revision": updated.get("approved_source_revision"),
            "approved_value_sha256": updated.get("approved_value_sha256"),
        }
        if body.model_call_ids is not None:
            proposals = item.get("proposals") or item.get("evidence") or []
            final_values = body.approved_values or []
            # ONE of these rows is the decision; the rest are the drafts it covered. The flag is
            # what stops a five-image card from reading as five reviews in hitl_analytics — see
            # Store._decision_rows. It cannot be `index == 0`: the loop skips proposals with no
            # recorded call, so the first row WRITTEN is not always the first row considered, and
            # keying on the index would leave a burst with no primary row at all whenever
            # proposal 0 happened to be human-authored.
            wrote_primary = False
            for index, call_id in enumerate(body.model_call_ids):
                if not call_id:
                    continue
                ai_value = ((proposals[index].get("proposed_value")
                             if index < len(proposals) else None) or None)
                final_value = (final_values[index] if index < len(final_values) else None)
                core.store.record_hitl_event(
                    item.get("scan_id"), item.get("file"), item.get("rule_id"), item_id, _action,
                    edited=bool(final_value is not None and ai_value is not None
                                and final_value != ai_value),
                    ai_value=ai_value, final_value=final_value,
                    reject_reason=(body.reject_reason if body.status == "rejected" else None),
                    model_call_id=call_id, decision_primary=not wrote_primary, **event_kwargs)
                wrote_primary = True
            if not wrote_primary:
                # Every id in the list was empty, so the loop wrote nothing and the decision went
                # unrecorded — invisible in approval rate, review time and the maturity gate alike.
                # A list of blanks means "no model call to attribute this to", which is exactly the
                # human-authored case the else-branch below already handles; it just never ran,
                # because the branch is chosen on the field being PRESENT rather than useful.
                core.store.record_hitl_event(
                    item.get("scan_id"), item.get("file"), item.get("rule_id"), item_id, _action,
                    edited=body.edited, ai_value=body.ai_value, final_value=body.approved_value,
                    reject_reason=(body.reject_reason if body.status == "rejected" else None),
                    model_call_id=body.model_call_id, **event_kwargs)
        else:
            core.store.record_hitl_event(
                item.get("scan_id"), item.get("file"), item.get("rule_id"), item_id, _action,
                edited=body.edited, ai_value=body.ai_value, final_value=body.approved_value,
                reject_reason=(body.reject_reason if body.status == "rejected" else None),
                model_call_id=body.model_call_id, **event_kwargs)
    except Exception:
        swallowed("routes.hitl.hitl_update: recording the HITL event failed")
    # Observability: the human decision joins the file's Langfuse trace (audit P1 — HITL
    # decisions were previously untraced). Best-effort; never blocks the review.
    try:
        import lf as _lf
        _lf.trace_hitl_decision(item.get("scan_id"), item.get("file"), item.get("rule_id"),
                                body.status, note=body.reviewer_note,
                                approved_value=body.approved_value)
    except Exception:
        swallowed("routes.hitl.hitl_update: tracing the HITL decision failed")
    # ADR 0003 Phase 2: HITL resolution is an explicit remediation_state transition.
    # approved (AI draft accepted) -> complete; rejected (a human said it's wrong, still
    # needs work) -> in_progress; skipped (deferred, still needs attention) -> unchanged
    # (stays awaiting_review) -- pending doesn't transition anything.
    _STATE_FOR = {"approved": "complete", "rejected": "in_progress", "skipped": "awaiting_review"}
    if body.status in _STATE_FOR and item.get("scan_id") and item.get("file") and item.get("rule_id"):
        try:
            from documents import resolve_doc_id
            scan_id, file = item["scan_id"], item["file"]
            source = (core.store.get_scan(scan_id) or {}).get("run", {}).get("source")
            ident = next((i for i in core.store.list_file_identities(scan_id) if i["file"] == file), {})
            doc_id = resolve_doc_id(source, ident.get("drive_file_id"), file, ident.get("checksum"))
            core.store.upsert_remediation_state(doc_id, item["rule_id"], _STATE_FOR[body.status], scan_id)
        except Exception:
            swallowed("routes.hitl.hitl_update: reading the scan run for the HITL update failed")
    # Write the approved content into the document. Approving alt text used to store the text
    # and stop — the images stayed undescribed, so mark_file_compliant_if_reviewed below
    # refused to certify and the file could never reach Publish. The job applies the values to
    # the remediated copy, re-scans it, and only then marks the row applied; it re-runs the
    # certify seam itself once the document actually carries the content.
    # The gate asks for ANY approved content an applier can write, per kind — alt text (1.1.1),
    # link text (2.4.4/2.4.9), a PDF form-field name (4.1.2) — because each lives in its OWN
    # hitl row. Naming only some of them strands the rest: a file whose only pending value was a
    # field name, or link text, enqueued no job, so the document never carried the value and
    # mark_file_compliant_if_reviewed below correctly refused to certify it, forever. The
    # handler already writes all three; this is the gate that decides whether it ever runs.
    # A 'decorative' resolution is the fourth kind and the odd one out — it approves no TEXT,
    # only the OOXML marking — and it is folded into the same helper for exactly the reason
    # above: left out, the reviewer's decision would live only in our audit log.
    # Re-validate → certify: once a remediated file's every review item is approved AND every
    # approved value has been written in, it is fully conformant (auto fixes verified + human
    # findings signed off) and advances to Publish. A file still holding unwritten approved
    # content returns False here and certifies later, from the apply job.
    if body.status == "approved" and item.get("scan_id") and item.get("file"):
        try:
            if core.store.mark_file_compliant_if_reviewed(item["scan_id"], item["file"]):
                core.store.log_decision(
                    "system", "revalidate.certified", scan_id=item["scan_id"], file=item["file"],
                    detail="all findings resolved (auto-fixed + human-approved) — certified & advanced to Publish")
        except Exception:
            swallowed("routes.hitl.hitl_update: marking the file compliant after review failed")
    # Notify external systems of terminal decisions (approved / rejected / skipped).
    # Uses the resolved item dict so the payload includes the reviewer note and approved value.
    if body.status in {"approved", "rejected", "skipped"} and updated:
        core.fire_webhook([updated], event="hitl.resolved")
    return updated


@router.post("/hitl/queue/{item_id}/retry-write")
def hitl_retry_write(item_id: str, request: Request = None):
    """Retry one owned approval; report active job or refusal without changing the decision."""
    _owned_item(item_id, request)
    actor = getattr(getattr(request, "state", None), "user_email", None) or "reviewer"
    result = core.store.retry_approved_write(item_id, actor=actor)
    return {k: result[k] for k in ("accepted", "job_id", "status", "in_flight", "reason")}


class HitlAssign(BaseModel):
    assignee: str | None = None   # email address of the reviewer to assign, or null to clear


@router.get("/hitl/queue/{item_id}/companion")
def hitl_companion_download(item_id: str, request: Request = None):
    """Download the companion FILE this review row delivers — a caption track, a transcript.

    THE ARTEFACT HAD NO WAY OUT BEFORE THIS. #1177 drafted a WebVTT, put it on a review card and
    stopped: the value lived in a JSON blob that no route read back, so a reviewer could sign the
    finding off and still not obtain the file the sign-off was about. A caption file that cannot
    be delivered is not a remediation; it is a record that one was considered.

    Returns the reviewer's APPROVED text where there is one and the draft otherwise — the same
    "approving an unedited card means the drafts are correct" rule `_row_approved_values` uses.
    Downloading before approval is deliberately allowed: a reviewer checking a transcript against
    the audio wants the file in a player, and refusing until approval would make them approve it
    to find out whether they should.

    NO FILENAME IN THE PATH, deliberately. The name comes from the stored row, so nothing a
    caller sends reaches the Content-Disposition header or any later path — the traversal surface
    is closed at the route rather than only by `Store.companion_name`, which stays as the check on
    what a row (possibly written by an older build) actually holds.

    404 rather than 200-with-nothing when the row carries no companion: an empty file would be
    indistinguishable from a caption track for a silent video, and a player handed one shows
    nothing rather than reporting a problem.
    """
    item = _owned_item(item_id, request)
    files = core.store._row_companion_files(item)
    if not files:
        raise HTTPException(404, "this review item carries no companion file")
    if len(files) > 1:
        # One row, one artefact today (a caption card is enqueued alone). If that ever changes,
        # this must grow a selector rather than silently hand back whichever came first.
        raise HTTPException(409, f"this item carries {len(files)} companion files; "
                                 f"no selector exists yet")
    name, content = next(iter(files.items()))
    # The name is server-derived, but it still goes through the same header hygiene the uploaded
    # -filename path uses: it descends from a customer's media filename, and a quote or control
    # character in it would terminate or inject the header.
    safe = "".join(c for c in name if c.isprintable()).replace('"', "'") or "captions.vtt"
    media_type = ("text/vtt" if safe.lower().endswith(".vtt")
                  else "application/x-subrip" if safe.lower().endswith(".srt")
                  else "text/plain")
    return Response(content.encode("utf-8"),
                    media_type=f"{media_type}; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{safe}"'})


@router.patch("/hitl/queue/{item_id}/assign")
def hitl_assign(item_id: str, body: HitlAssign, request: Request):
    """Assign (or unassign) a reviewer to a HITL queue item. Persists to the DB so the
    assignment survives page reloads and is visible to other sessions.
    Fires hitl.assigned when an assignee is set (not on clear)."""
    _owned_item(item_id, request)
    result = core.store.assign_hitl_item(item_id, body.assignee)
    if body.assignee and result:
        core.fire_webhook([result], event="hitl.assigned")
    return result

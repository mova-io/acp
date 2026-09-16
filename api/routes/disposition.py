"""Configurable file disposition (ADR 0003, Phase 3) -- policy CRUD, preview,
and the EXECUTE path (approved 2026-07-02).

Execution flow: /execute evaluates an ENABLED policy against the documents
table; matches either become pending_approval rows in the append-only
disposition_audit (requires_approval policies — the default) or are actioned
immediately. /approvals lists the pending queue; approve performs the action,
reject records the refusal. Every outcome lands in disposition_audit.

Safety posture: authoring routes (create / update / enable / preview a policy) are open to any
authenticated user under the open-access model (_require_admin, which every signed-in user now
passes) — a policy is created disabled and never moves a file on its own. The two routes that
actually AUTHORISE or PERFORM a move-or-trash — execute and approve — are OWNER-gated
(_require_owner), so no single non-owner can act on the estate. delete is always Drive trash
(never permanent — see disposition.execute_action), and
a doc/policy pair with a live outcome (pending or applied) is never re-queued.

DISCOVER-LIFECYCLE CANDIDATES execute through the same path, per row. They are keyed
`scan:{scan_id}:{file}` rather than `drive:{id}`, so they are absent from the documents table
this module otherwise reads; the drive_file_id on their inventory row is what bridges the two,
and _lifecycle_drive_doc resolves it — owner-scoped — so that everything downstream treats them
as the Drive documents they are. A candidate WITHOUT one stays record-only: nothing ACP holds
could act on it. Executing also stamps the inventory's lifecycle status, because a file that has
actually been archived must stop reading "Active" in the view a reviewer checks.
"""
from __future__ import annotations
import hashlib
import json
import uuid

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel

import core
import disposition
from .system import _require_admin, _require_owner
from swallowed import swallowed

router = APIRouter()


def _owner(request: Request) -> str:
    """The current user for per-user data isolation — matches assess.py/scans.py's helper.
    disposition_policy/disposition_audit had no ownership column until this fix, so every
    signed-in user (demo account included) saw and could act on every other tenant's rules."""
    return getattr(request.state, "user_email", None) or "demo"


def _drive_svc(request: Request):
    """Drive client from the caller's token header, or None (leave-only mode)."""
    token = request.headers.get("x-drive-token")
    if not token:
        return None
    import handlers
    return handlers._drive_client(token)


def _persist_tags(doc: dict, cfg: dict, policy_id: str) -> None:
    """Write a tag policy's tags to file_tags after execute_action applied them.

    The disposition governance layer (the documents table) has no scan grain, so
    system tags are keyed by the document's STABLE doc_id (as scan_id) and its path
    (as file) — unique per document and idempotent by the file_tags primary key, so
    a re-run adds nothing new. kind='system'; rule_id is the policy that applied it,
    matching store.add_file_tags' contract (PRD §4.2 Tag / §3 auto-tagging)."""
    tags = disposition.tag_list(cfg)
    if tags:
        core.store.add_file_tags(doc["doc_id"], doc.get("path") or doc["doc_id"],
                                 tags, kind="system", rule_id=policy_id)


def _trace_decision(doc_id: str, path: str | None, *, action: str, status: str,
                    policy_id: str | None, reason: str | None) -> None:
    """Best-effort Langfuse span for a disposition/approval decision (Langfuse audit N2).

    Disposition was the one decision surface with no trace — the approval queue (#360) records
    to disposition_audit but emitted nothing to Langfuse, unlike the HITL review decisions. This
    mirrors that coverage. PHI-safe by construction: the helper sends only counts, statuses and
    ids and reduces the free-text `reason` to a length (see api/lf.trace_disposition_decision and
    docs/audit-langfuse-phi.md). Never blocks the decision it observes."""
    try:
        import lf as _lf
        _lf.trace_disposition_decision(doc_id, path, action=action, status=status,
                                       policy_id=policy_id, reason=reason)
    except Exception:
        swallowed("routes.disposition._trace_decision: tracing the disposition decision failed")


class PolicyCreate(BaseModel):
    name: str
    match: list[dict]
    action: str
    action_config: dict = {}
    requires_approval: bool = True
    enabled: bool = False        # created disabled by default -- an explicit opt-in to enable
    #: PRD §7.5: a destructive rule "cannot disable approval without an administrator capability
    #: and an explicit confirmation". requires_approval already DEFAULTS to True, which is the
    #: first half and the easy half; nothing enforced the second, so one boolean in a JSON body
    #: was the whole distance between "queued for a human" and a rule that moves or trashes
    #: documents unattended. In the body rather than a header or query flag, so it is part of the
    #: request a client had to compose deliberately.
    confirm_unattended: bool = False


def _guard_unattended(action: str, requires_approval: bool, confirmed: bool) -> None:
    """Refuse to arm a source-mutating rule that skips human approval, unless asked twice.

    The auto-apply branch (requires_approval false) is the one path in this system that changes a
    file with NO human in the loop, so this is the last point at which anybody is asked at all.

    `leave` and `tag` are exempt because they change no file. Gating them would train people to
    send the flag by reflex, and a confirmation everyone sends stops being a confirmation.
    """
    if action in disposition.SOURCE_MUTATING and not requires_approval and not confirmed:
        raise HTTPException(422, (
            f"'{action}' changes the source file, so a rule that skips approval would move or "
            f"trash documents with no human review. Send confirm_unattended=true to accept that, "
            f"or leave requires_approval=true."))


@router.post("/disposition/policies")
def create_policy(body: PolicyCreate, request: Request):
    _require_admin(request)
    if body.action not in disposition.ACTIONS:
        raise HTTPException(422, f"action must be one of {sorted(disposition.ACTIONS)}")
    _guard_unattended(body.action, body.requires_approval, body.confirm_unattended)
    try:
        disposition.validate_match(body.match)
        disposition.validate_action_config(body.action, body.action_config)
    except ValueError as e:
        raise HTTPException(422, str(e))
    policy_id = uuid.uuid4().hex[:12]
    owner = _owner(request)
    core.store.create_disposition_policy(
        policy_id, name=body.name, match=json.dumps(body.match), action=body.action,
        action_config=json.dumps(body.action_config), requires_approval=body.requires_approval,
        enabled=body.enabled, owner_email=owner)
    core.store.log_decision(owner, "disposition.policy_created", detail=body.name)
    return core.store.get_disposition_policy(policy_id, owner=owner)


def _readable(rows: list[dict], owner: str) -> list[dict]:
    """Join the human-readable facts onto audit rows: the document's source/path/department and
    the policy's NAME.

    A row as stored is (doc_id, policy_id, action, result) — four ids and an enum. Rendering that
    asks a reviewer to authorise "archive sp:1 under p2", which is not a decision anybody can
    make, and an auditor reading it back later has no way to tell what happened.

    `source` is what makes the rows SCOPEABLE. disposition_audit has no source column, so without
    this join a per-source panel would have to render the whole estate's history under a heading
    naming one source — the count-without-its-boundary error the rule match counts avoid.

    Shared by the approval queue and the audit trail deliberately: two endpoints enriching the
    same table differently is how one of them ends up missing a field nobody noticed.

    `document_exists` is false when the document has since been removed. Left visible, not
    filtered: for a pending row it is what a reviewer needs before deciding, and for a historical
    row it is part of what happened.
    """
    if not rows:
        return []
    docs = {d["doc_id"]: d for d in core.store.list_all_documents(owner=owner)}
    policies = {p["policy_id"]: p for p in core.store.list_disposition_policies(owner=owner)}
    out = []
    for r in rows:
        d = docs.get(r.get("doc_id")) or {}
        p = policies.get(r.get("policy_id")) or {}
        out.append({**r,
                    "source": d.get("source"), "path": d.get("path"),
                    "department": d.get("department"), "document_exists": bool(d),
                    # None, not the id, when the policy is gone: a deleted rule is a fact, and
                    # showing its id here would read as a name.
                    "policy_name": p.get("name")})
    return out


@router.get("/disposition/policies")
def list_policies(request: Request):
    return core.store.list_disposition_policies(owner=_owner(request))


class PolicyReorder(BaseModel):
    """The tenant's full rule order, listed. Not a single rule's new position — the whole
    ordering, so two people (or two tabs) reordering at once produce one clear last-write-wins
    outcome instead of two partial moves interleaving into an order nobody chose."""
    policy_ids: list[str]


@router.put("/disposition/policies/reorder")
def reorder_policies(body: PolicyReorder, request: Request):
    """Set evaluation priority for every one of this tenant's rules at once, 1..N in the order
    given. Registered before PUT /disposition/policies/{policy_id} so "reorder" is never read as
    a policy_id — see the routes above it for why that ordering matters here specifically.

    Requires the FULL current set, each rule exactly once — not a subset, and not an id that
    doesn't belong to this tenant — so there's never an ambiguous "what happened to the rule I
    left out" question. 422, not a silent partial reorder, when it doesn't match."""
    _require_admin(request)
    owner = _owner(request)
    current_ids = {p["policy_id"] for p in core.store.list_disposition_policies(owner=owner)}
    given_ids = list(body.policy_ids)
    if len(given_ids) != len(set(given_ids)) or set(given_ids) != current_ids:
        raise HTTPException(422,
            "policy_ids must list every one of this tenant's rules exactly once — "
            f"expected {len(current_ids)} rule(s), got {len(set(given_ids))} distinct id(s)")
    core.store.reorder_disposition_policies(owner, given_ids)
    core.store.log_decision(owner, "disposition.policies_reordered", detail=",".join(given_ids))
    return core.store.list_disposition_policies(owner=owner)


def _all_candidates(owner: str) -> list[dict]:
    """Every document a disposition rule could match, right now: assessed documents plus the
    inventory of scans still awaiting Assess. A file is covered by exactly one of the two sources
    (see store.list_pending_disposition_candidates's own docstring for why), so this is a plain
    concatenation, not a merge — no dedup needed."""
    return core.store.list_all_documents(owner=owner) + core.store.list_pending_disposition_candidates(owner=owner)


@router.get("/disposition/policies/conflicts")
def list_conflicts(request: Request):
    """Files matched by 2+ ENABLED archive/delete rules right now, and which one would win —
    the same decision disposition.resolve_candidate makes at discovery time, run here on demand
    against the CURRENT estate and CURRENT rules so a person can see a conflict before running
    Discover again, not only after it already happened.

    A tag-only overlap is not a conflict: every matching tag policy applies independently (see
    _evaluate_discover_lifecycle_rules), so only archive/delete matches are counted toward the
    2+ threshold. `actor` for the precedence call is the REQUESTING user, not necessarily who a
    future Discover run will be attributed to — an honest best-guess for "if this ran now, under
    you", not a guarantee of what a later run under a different actor would decide.
    """
    _require_admin(request)
    owner = _owner(request)
    policies = [p for p in core.store.list_disposition_policies(owner=owner) if p.get("enabled")]
    if not policies:
        return {"conflicts": []}
    out = []
    for doc in _all_candidates(owner):
        matched = []
        for p in policies:
            try:
                match = json.loads(p.get("match") or "[]")
            except Exception:
                continue
            if disposition.matches(doc, match):
                matched.append(p)
        candidates = [p for p in matched if p.get("action") in ("archive", "delete")]
        if len(candidates) < 2:
            continue
        chosen, outcome, reason = disposition.resolve_candidate(matched, owner)
        out.append({
            "doc_id": doc.get("doc_id"), "path": doc.get("path"),
            "matched_rules": [{"policy_id": p["policy_id"], "name": p.get("name"),
                               "action": p.get("action")} for p in candidates],
            "winner": ({"policy_id": chosen["policy_id"], "name": chosen.get("name")}
                      if chosen else None),
            "outcome": outcome, "reason": reason,
        })
    return {"conflicts": out}


@router.put("/disposition/policies/{policy_id}/enabled")
def set_policy_enabled(policy_id: str, enabled: bool, request: Request,
                       previewed_match_count: int | None = Query(None)):
    """Turn a rule on or off.

    PRD §7.5: "A rule cannot activate until preview completes successfully." Enforced here for
    the rules that CHANGE FILES, and the check is not merely that a preview could run — the
    server can always run one. It is that a person saw the result: the caller states the count
    they were shown, and activation is refused if the rule no longer selects that many.

    That closes the window the preview otherwise leaves open. Somebody previews a rule at 12
    files, goes to lunch, a Discover run lands 4,000 more documents, and the toggle they come
    back to arms a rule against an estate they never looked at. The count is re-derived at the
    moment of activation, so the number consented to is the number in force.

    The flow is deliberately two-step and self-describing: called without the count, the refusal
    NAMES the current one, so a client learns what to confirm by asking.

    DISABLING IS NEVER GATED. It is the safety valve, and a valve you have to argue with is not
    one. Nor are tag/leave rules: they change no file, and gating them would train people
    through the confirmation by reflex — the same reasoning that exempts them from
    confirm_unattended.
    """
    _require_admin(request)
    owner = _owner(request)
    policy = core.store.get_disposition_policy(policy_id, owner=owner)
    if policy is None:
        raise HTTPException(404, "policy not found")

    if enabled and str(policy.get("action")) in disposition.SOURCE_MUTATING:
        try:
            pv = _preview(json.loads(policy["match"]), policy["action"], policy_id, owner)
        except Exception as exc:      # noqa: BLE001 — an unrunnable preview must not activate
            # "until preview completes SUCCESSFULLY". A rule whose conditions cannot be evaluated
            # is precisely the one nobody should be arming on trust.
            raise HTTPException(422, f"the preview for this rule did not complete, so it cannot "
                                     f"be activated: {exc}") from exc
        current = pv["would_match"]
        if previewed_match_count is None:
            raise HTTPException(422, (
                f"'{policy.get('action')}' changes files, so this rule cannot be turned on until "
                f"someone has seen what it selects. It matches {current} document(s) right now — "
                f"re-send with previewed_match_count={current} to confirm you have reviewed that."))
        if previewed_match_count != current:
            raise HTTPException(409, (
                f"this rule selected {previewed_match_count} document(s) when you previewed it and "
                f"selects {current} now — the estate or the rule changed in between. Preview it "
                f"again before turning it on."))

    core.store.set_disposition_policy_enabled(policy_id, enabled)
    core.store.log_decision(owner, f"disposition.policy_{'enabled' if enabled else 'disabled'}",
                            detail=policy_id)
    return core.store.get_disposition_policy(policy_id, owner=owner)


#: How many non-matching documents a preview names. Small on purpose: this is a sample to reason
#: from, not a second result set, and the preview fires on every keystroke in the rule editor.
_NEAR_MISS_SAMPLE = 5


def _preview(match: list[dict], action: str, policy_id: str | None, owner: str) -> dict:
    """The dry run itself, shared by the saved-policy preview and the draft preview.

    ONE evaluator, deliberately. The rule editor previews a draft as it is typed and the rule list
    previews the saved rule; were those two implementations, the count a person approved a rule on
    and the count that rule actually produces could differ — and the entire reason the preview
    exists is that somebody is deciding on the strength of that number.

    Read-only by construction: reads `documents` plus any not-yet-assessed scan inventory (see
    `_all_candidates`), runs `disposition.matches` in Python, writes nothing. No file is touched,
    no disposition_audit row appended, no policy row created.

    Returns a breakdown alongside `would_match`:
      effective          — raw matches where this rule would be the winning action
      superseded         — raw matches where a higher-priority enabled archive/delete rule wins
      exempted           — files that match but carry lifecycle_status="Exempted" (legal hold);
                           they are never tagged regardless of any rule
      unable_to_evaluate — files that did NOT match, but only because a required metadata field
                           was absent; they might have matched had the data been recorded
    """
    docs = _all_candidates(owner)

    # Superseded computation requires knowing the full enabled policy list in priority order.
    # Only meaningful for saved archive/delete rules — draft rules (no policy_id) have no rank,
    # and tag/leave/rename/move rules don't compete with archive/delete for the same action slot.
    all_enabled_policies = []
    other_ad_policies = []
    this_policy_row = None
    if policy_id and action in ("archive", "delete"):
        all_enabled_policies = [p for p in core.store.list_disposition_policies(owner=owner)
                                 if p.get("enabled")]
        this_policy_row = next((p for p in all_enabled_policies
                                if p.get("policy_id") == policy_id), None)
        other_ad_policies = [p for p in all_enabled_policies
                             if p.get("policy_id") != policy_id
                             and p.get("action") in ("archive", "delete")]

    selected = []
    effective = 0
    superseded = 0
    exempted = 0
    # PRD §7.5 asks the test bench for "representative matches and non-matches". The matches are
    # already returned in full as `documents`; the non-matches were the missing half, and they are
    # the debugging half — a rule that selects far fewer files than expected is diagnosed by
    # seeing what it REJECTED and why, not by staring at the count it produced.
    #
    # The reasons cost nothing extra: disposition.evaluate() already runs for every document in
    # the loop below, so the failing condition is in hand and was being thrown away. Only the
    # SAMPLE is bounded, and that is about payload size — this runs on every keystroke in the
    # rule editor, and a 12,000-row list of near misses helps nobody.
    near_misses: list[dict] = []
    exempted_documents: list[dict] = []
    unable_to_evaluate = 0
    unable_to_evaluate_fields: dict[str, int] = {}

    for doc in docs:
        # Exempted docs (legal hold, PRD §6) are never tagged regardless of any rule.
        if doc.get("lifecycle_status") == "Exempted":
            if disposition.matches(doc, match):
                exempted += 1
                exempted_documents.append(doc)
            continue

        eval_result = disposition.evaluate(doc, match)

        if not eval_result["matched"]:
            # A condition failed with no observed value — the file might have matched if the
            # metadata were recorded; surface it as unable_to_evaluate rather than silently missing.
            missing_fields = [c["field"] for c in eval_result["conditions"]
                              if c["outcome"] == "fail" and c["observed_value"] is None]
            if missing_fields:
                unable_to_evaluate += 1
                for f in missing_fields:
                    unable_to_evaluate_fields[f] = unable_to_evaluate_fields.get(f, 0) + 1
            if len(near_misses) < _NEAR_MISS_SAMPLE:
                # The FIRST failing condition, not every one: a reader debugging "why didn't
                # this match?" wants the reason it fell out, and the conditions after it were
                # never the deciding factor.
                failed = next((c for c in eval_result["conditions"] if c["outcome"] == "fail"), None)
                near_misses.append({
                    "path": doc.get("path"), "doc_id": doc.get("doc_id"),
                    "field": (failed or {}).get("field"),
                    "observed_value": (failed or {}).get("observed_value"),
                    "reason": (failed or {}).get("reason"),
                    # Distinguishes "this file is genuinely not a match" from "we could not tell",
                    # which unable_to_evaluate counts in aggregate but never names a file for.
                    "unevaluable": bool(missing_fields),
                })
            continue

        selected.append(doc)

        # Determine effective vs superseded for saved archive/delete rules only.
        if not this_policy_row or action not in ("archive", "delete"):
            effective += 1
            continue

        # Check whether any other enabled archive/delete rule also matches this doc.
        other_matched = []
        for p in other_ad_policies:
            try:
                other_match = json.loads(p.get("match") or "[]")
            except Exception:
                continue
            if disposition.matches(doc, other_match):
                other_matched.append(p)

        if not other_matched:
            effective += 1
            continue

        # Resolve in priority order — all_enabled_policies is already sorted by the store.
        full_matched = [p for p in all_enabled_policies
                        if p.get("policy_id") == policy_id or p in other_matched]
        chosen, _, _ = disposition.resolve_candidate(full_matched, owner)
        if chosen and chosen.get("policy_id") == policy_id:
            effective += 1
        else:
            superseded += 1

    # `total` is `docs` already fetched — free to report, and it's what turns "would_match: 812"
    # into a percentage a person can judge a rule's breadth by (Lifecycle Rules item #5).
    return {
        "policy_id": policy_id,
        "action": action,
        "would_match": len(selected),
        "total": len(docs),
        "documents": selected,
        "effective": effective,
        "superseded": superseded,
        "exempted": exempted,
        "exempted_documents": exempted_documents,
        "unable_to_evaluate": unable_to_evaluate,
        "unable_to_evaluate_fields": unable_to_evaluate_fields,
        "near_misses": near_misses,
        "near_miss_sample": _NEAR_MISS_SAMPLE,
    }


@router.post("/disposition/policies/{policy_id}/preview")
def preview_policy(policy_id: str, request: Request):
    """Dry run: which documents would this policy select, right now? Never writes
    disposition_audit and never touches a file -- read-only by construction.

    Previously took no Request param at all, so it had zero auth gate and could not have been
    owner-scoped even if it wanted to be — any caller who knew a policy_id could preview any
    tenant's rule against the whole documents table. Now admin-gated and owner-scoped like its
    siblings: a policy_id belonging to a different tenant 404s rather than previewing."""
    _require_admin(request)
    owner = _owner(request)
    policy = core.store.get_disposition_policy(policy_id, owner=owner)
    if policy is None:
        raise HTTPException(404, "policy not found")
    return _preview(json.loads(policy["match"]), policy["action"], policy_id, owner)


class PolicyDraft(BaseModel):
    """An UNSAVED rule — just enough of one to say what it would select."""
    match: list[dict]
    action: str
    action_config: dict = {}


@router.post("/disposition/preview")
def preview_draft(body: PolicyDraft, request: Request):
    """Dry run a rule that does NOT exist yet: how many documents would `{match, action}` select?

    The saved-policy preview above needs a policy_id, so the rule editor could only show a count
    AFTER creating the rule — which inverts the order the decision is actually made in. A person
    writing "everything under /Finance/2019 not modified in five years" needs to know it selects
    40 files and not 40,000 BEFORE committing to it; a preview that arrives once the row exists is
    a preview of a decision already taken.

    Read-only by construction, and more strictly so than the saved preview: it creates no policy
    row, so there is nothing to enable, nothing to execute and nothing left behind when the person
    closes the editor. It runs the SAME `disposition.matches` over the SAME documents table via
    `_preview`, so the count shown while typing is the count the saved rule will produce.

    Returns the saved preview's shape with `policy_id: null` — null because this rule has no id,
    not because one was lost. The key is present so a caller reads both responses one way.

    Admin-gated like the rest of this module, and NOT like `preview_policy`, which carries no gate.
    The predicate here is CALLER-SUPPLIED: the saved preview can only re-run a predicate an admin
    already authored and stored, while this route would otherwise let any allow-listed user run an
    arbitrary predicate over the whole documents table and read the matching rows back. Same
    refusal shape as its neighbours — 403 "admin access required".
    """
    _require_admin(request)
    if body.action not in disposition.ACTIONS:
        raise HTTPException(422, f"action must be one of {sorted(disposition.ACTIONS)}")
    try:
        disposition.validate_match(body.match)
        disposition.validate_action_config(body.action, body.action_config)
    except ValueError as e:
        raise HTTPException(422, str(e))
    return _preview(body.match, body.action, None, _owner(request))


class PolicyUpdate(BaseModel):
    """An edit to a saved rule. Every field optional — an omitted field is left as it is.

    `enabled` is DELIBERATELY ABSENT. Arming a rule is its own decision, with its own route
    (PUT .../enabled) and its own audit line. Folding it into the edit payload would let a save
    somebody read as "rename this rule" also start it running.
    """
    name: str | None = None
    match: list[dict] | None = None
    action: str | None = None
    action_config: dict | None = None
    requires_approval: bool | None = None
    confirm_unattended: bool = False


def _policy_has_history(policy_id: str, owner: str) -> bool:
    """Has this rule already produced a recorded outcome?

    disposition_audit is the complete record of what a rule has done. Every path that acts on a
    rule appends to it: the execute path writes a row per selected document, and the discovery
    lifecycle evaluator (handlers._evaluate_discover_lifecycle_rules) writes one beside every
    lifecycle_status it sets — so a rule cannot have flagged a file without an audit row naming
    it. Checking this one table is therefore checking every kind of history, not a convenient
    subset of it.

    ANY result counts, 'rejected' and 'failed' included. doc_has_disposition treats those as
    non-live so a re-run may propose the document again; that is a different question. The
    question here is whether a stored record NAMES this rule, and a rejected row names it just as
    loudly — an auditor reading "rejected: matched delete rule 'stale-finance'" is owed the rule
    that sentence was written about.
    """
    return bool(core.store.list_disposition_audit(policy_id=policy_id, limit=1, owner=owner))


@router.put("/disposition/policies/{policy_id}")
def update_policy(policy_id: str, body: PolicyUpdate, request: Request):
    """Edit a saved rule. Validated exactly like create, and structurally unable to arm one.

    ── WHAT AN EDIT MAY NOT DO: CHANGE WHAT ALREADY HAPPENED ────────────────────────────────
    A rule that has run has produced records that name it — disposition_audit rows, and
    scan_inventory rows whose `lifecycle_rule_id` points at it with a `lifecycle_reason` quoting
    it by name. Those records carry the rule's ID and nothing else: no column anywhere records
    WHICH DEFINITION of the rule produced them.

    So editing a definition in place silently rewrites the past. A file flagged "Archive Candidate
    — matched archive rule 'stale finance'" under a rule that said "not modified in five years"
    would, after an edit, be read by every future reader as having matched "not modified in 90
    days". Nothing errors. The tag does not move. The reason string does not change. Only its
    meaning does — retroactively, for evidence a compliance reviewer is expected to rely on.

    THE RULE THIS ROUTE ENFORCES: edits apply going forward, and existing records keep the
    definition that produced them.

    Implemented as: a rule that has produced ANY recorded outcome may no longer have its
    DEFINITION changed (`match`, `action`, `action_config`) — refused with 409. Its `name` and
    `requires_approval` stay editable, because neither alters which files the rule selected or
    what it recommended for them. To change what a rule that has run selects, create a new rule
    and disable the old one: the old rule and its history stay intact and keep meaning what they
    meant.

    ── WHY NOT VERSIONING, WHICH WOULD BE MORE PERMISSIVE ───────────────────────────────────
    The alternative is to version the policy and stamp that version onto every record it produces,
    so an edit makes v2 while existing tags keep pointing at v1. That is the better long-term
    answer and it is deliberately NOT implemented here, because doing it honestly means adding a
    version to `disposition_policy` AND `disposition_audit` AND `scan_inventory` — the three
    places a rule reference is stored — plus a backfill decision for every row already written
    without one. Versioning only the policy table would be WORSE than not versioning at all: audit
    rows would still carry a bare ID, so history would still read against whatever version is
    current, while the schema now claims the problem is solved.

    A rule that has produced nothing has exactly one definition, so the guarantee holds trivially
    for it — and that is the case the rule editor is in almost every time, since rules are created
    disabled and are edited before they are ever armed.
    """
    _require_admin(request)
    owner = _owner(request)
    policy = core.store.get_disposition_policy(policy_id, owner=owner)
    if policy is None:
        raise HTTPException(404, "policy not found")

    # Resolve the RESULTING definition (current value wherever the caller omitted a field), then
    # validate THAT rather than the patch. A patch moving `action` from 'archive' to 'tag' without
    # supplying action_config has to be judged as the tag policy it produces — which is how "tag
    # with no tags" is caught here instead of failing silently at execute time.
    current_match = json.loads(policy.get("match") or "[]")
    current_cfg = json.loads(policy.get("action_config") or "{}")
    new_match = current_match if body.match is None else body.match
    new_action = policy["action"] if body.action is None else body.action
    new_cfg = current_cfg if body.action_config is None else body.action_config

    if new_action not in disposition.ACTIONS:
        raise HTTPException(422, f"action must be one of {sorted(disposition.ACTIONS)}")
    try:
        disposition.validate_match(new_match)
        disposition.validate_action_config(new_action, new_cfg)
    except ValueError as e:
        raise HTTPException(422, str(e))

    changed = [field for field, before, after in
               (("match", current_match, new_match),
                ("action", policy["action"], new_action),
                ("action_config", current_cfg, new_cfg))
               if before != after]
    if changed and _policy_has_history(policy_id, owner):
        raise HTTPException(409,
            f"this rule has already run — its {', '.join(changed)} can no longer be changed. "
            "Files it flagged carry its decision, and no record says which version of the rule "
            "made that decision, so editing the rule here would change what those records mean. "
            "Create a new rule with the definition you want and disable this one; its history "
            "stays intact. (Its name and approval requirement are still editable.)")

    new_name = policy.get("name") if body.name is None else body.name
    new_req = (bool(policy.get("requires_approval")) if body.requires_approval is None
               else body.requires_approval)
    # Guarded on the TRANSITION, not the state. Two things had to be got right here and the
    # first draft only got one:
    #
    #   · resolve first — `requires_approval` and `action` are both optional, so a rule created
    #     with approval could otherwise have it edited away in a one-field PATCH, which is the
    #     easier of the two bypasses to take and the harder to notice afterwards;
    #   · and fire only when this edit CREATES the unattended state. A rule that was already
    #     unattended — armed before this guard existed, or armed with a confirmation — must stay
    #     renameable. Guarding the state instead broke exactly that, and the suite said so:
    #     test_a_rule_that_has_run_can_still_be_renamed started returning 422 for a rename.
    was_unattended = (str(policy.get("action")) in disposition.SOURCE_MUTATING
                      and not bool(policy.get("requires_approval")))
    if not was_unattended:
        _guard_unattended(new_action, new_req, body.confirm_unattended)
    core.store.update_disposition_policy(
        policy_id, name=new_name, match=json.dumps(new_match), action=new_action,
        action_config=json.dumps(new_cfg), requires_approval=new_req)
    core.store.log_decision(owner, "disposition.policy_updated",
                            detail=f"{new_name}: {', '.join(changed) or 'name/approval only'}")
    return core.store.get_disposition_policy(policy_id, owner=owner)


@router.delete("/disposition/policies/{policy_id}")
def delete_policy(policy_id: str, request: Request):
    """Remove a rule outright — the counterpart to update_policy's edit-after-history guard.

    Same rule, same reason: a policy with ANY disposition_audit history is refused with 409
    instead of deleted. Its audit rows and any scan_inventory.lifecycle_rule_id pointing at it
    would otherwise name a policy_id nothing can look up — "matched rule p-a1b2c3" with no rule
    left to show for it is worse for an auditor than a rule that stays around disabled. A rule
    with no history has nothing pointing at it, so deleting it loses nothing on record.

    The UI's answer to "I don't want this rule that already ran" is the same one update_policy's
    409 message gives for editing one: disable it (PUT .../enabled) and leave it be, or create a
    replacement. Deletion is for a rule that was never armed, never matched anything, and is
    simply wrong.
    """
    _require_admin(request)
    owner = _owner(request)
    policy = core.store.get_disposition_policy(policy_id, owner=owner)
    if policy is None:
        raise HTTPException(404, "policy not found")
    if _policy_has_history(policy_id, owner):
        raise HTTPException(409,
            "this rule has already run and has audit history — it can't be deleted, only "
            "disabled. Disable it (it will stop tagging new files) and its history stays intact.")
    core.store.delete_disposition_policy(policy_id)
    core.store.log_decision(owner, "disposition.policy_deleted", detail=policy.get("name"))
    return {"deleted": policy_id}


@router.post("/disposition/policies/{policy_id}/execute")
def execute_policy(policy_id: str, request: Request):
    """Run an ENABLED policy for real. requires_approval matches queue as
    pending_approval; the rest are actioned immediately. Idempotent per
    (doc, policy): a pending or applied outcome is never re-queued."""
    _require_owner(request)   # actually moves/trashes files — owner-only
    owner = _owner(request)
    policy = core.store.get_disposition_policy(policy_id, owner=owner)
    if policy is None:
        raise HTTPException(404, "policy not found")
    if not policy.get("enabled"):
        raise HTTPException(409, "policy is disabled — enable it before executing")
    match = json.loads(policy["match"])
    cfg = json.loads(policy.get("action_config") or "{}")
    svc = _drive_svc(request)
    # 'leave' and 'tag' never touch Drive, so they can act immediately with no svc.
    if policy["action"] not in ("leave", "tag") and not policy.get("requires_approval") and svc is None:
        raise HTTPException(400, "this policy acts on Drive files immediately — "
                                 "connect Google Drive first")
    summary = {"matched": 0, "pending_approval": 0, "applied": 0, "failed": 0, "skipped": 0}
    for doc in core.store.list_all_documents(owner=owner):
        if not disposition.matches(doc, match):
            continue
        summary["matched"] += 1
        if core.store.doc_has_disposition(doc["doc_id"], policy_id):
            summary["skipped"] += 1
            continue
        # Deterministic id, not uuid.uuid4() — PRD §20 idempotency audit, 2026-08-28: the
        # doc_has_disposition check above is app-level check-then-insert, not a DB constraint, so
        # two concurrent execute_policy calls (a retried request racing the original, or two
        # workers) can both pass it before either inserts, producing two audit rows for the same
        # (doc, policy). This route's own docstring already promises "Idempotent per (doc,
        # policy)" — a doc/policy pair is meant to have at most one live disposition outcome ever,
        # per doc_has_disposition's contract (pending/applied/approved AND rejected/failed all
        # count), so keying the id on exactly that pair makes the promise a real DB-level
        # guarantee instead of a race-prone best-effort one.
        audit_id = hashlib.sha256(
            f"policy_execute:{doc['doc_id']}:{policy_id}".encode()).hexdigest()[:24]
        if policy.get("requires_approval"):
            detail = f"queued by policy '{policy['name']}' — awaiting approval"
            core.store.create_disposition_audit(
                audit_id, doc_id=doc["doc_id"], policy_id=policy_id,
                action=policy["action"], result="pending_approval", detail=detail,
                owner_email=owner)
            summary["pending_approval"] += 1
            _trace_decision(doc["doc_id"], doc.get("path"), action=policy["action"],
                            status="pending_approval", policy_id=policy_id, reason=detail)
        else:
            result, detail, before = disposition.execute_action(doc, policy["action"], cfg, svc)
            if result == "applied" and policy["action"] == "tag":
                _persist_tags(doc, cfg, policy_id)
            core.store.create_disposition_audit(
                audit_id, doc_id=doc["doc_id"], policy_id=policy_id,
                action=policy["action"], result=result, detail=detail, owner_email=owner)
            # An auto-applied policy (requires_approval=false) never passes through the approve
            # route, so without this the ONE path that moves a file with no human in the loop
            # would be the one path whose result could not be undone.
            core.store.set_disposition_before_state(audit_id, before)
            summary[result] += 1
            _trace_decision(doc["doc_id"], doc.get("path"), action=policy["action"],
                            status=result, policy_id=policy_id, reason=detail)
    core.store.log_decision(owner, "disposition.policy_executed",
                            detail=f"{policy['name']}: {summary}")
    return {"policy_id": policy_id, **summary}


@router.get("/disposition/audit")
def disposition_audit(request: Request, limit: int = Query(200, ge=1, le=1000),
                       doc_id: str | None = Query(None)):
    """Full disposition history, newest first — the visible face of the append-only
    audit table (pending, applied, rejected, failed alike).

    `doc_id` narrows this to one document's history — every rule that has ever tagged it, in
    order, not just the current recommendation `scan_inventory.lifecycle_status` holds. Discover's
    evaluator writes that key as "scan:<scan_id>:<file>" (handlers._evaluate_discover_lifecycle_rules),
    the same convention the per-file override route (scans.py's lifecycle-override) already uses —
    so a caller with a (scan_id, file) pair can always construct it without a lookup."""
    _require_admin(request)
    owner = _owner(request)
    return _readable(
        core.store.list_disposition_audit(limit=limit, doc_id=doc_id, owner=owner), owner)


@router.get("/disposition/approvals")
def list_approvals(request: Request):
    """The pending-approval queue — every doc a requires_approval policy selected
    that no admin has decided on yet.

    ENRICHED FROM `documents`, for two reasons. An audit row carries only `doc_id`, which is
    not something a reviewer can act on — approving "drive:1a2b3c" asks somebody to authorise a
    string. And without `source` the queue cannot be split by connector, so a per-source panel
    would have to render the whole estate's approvals under a heading naming one source: the
    same count-without-its-boundary defect the rule match counts avoid.

    `source`/`path` are None when the document has since disappeared. That is left visible
    rather than filtered out — a queued action against a document that no longer exists is
    exactly what a reviewer should see before deciding, and approve() already fails it with 410.
    """
    _require_admin(request)
    owner = _owner(request)
    return _readable(core.store.list_disposition_audit(result="pending_approval", owner=owner), owner)


@router.post("/disposition/approvals/{audit_id}/approve")
def approve_disposition(audit_id: str, request: Request,
                        execute: bool = Query(True)):
    """Approve the queued action. `execute` decides whether the file is TOUCHED.

    execute=true (default, unchanged): perform the action now — the audit row moves to applied
    or failed. This is what the route has always done, and callers that relied on it still get it.

    execute=false: RECORD THE DECISION ONLY. The row moves to 'approved' and no Drive call is
    made. Two reasons this is a distinct outcome rather than a client-side nicety:

      • `disposition.execute_action` supports Drive-backed documents ONLY (it returns
        "unsupported source" for anything else), so an approval on a SharePoint/OneDrive
        document cannot execute today whatever the caller asks for. Recording the decision is
        the honest half of the operation, and it is the half a compliance reviewer needs.
      • ACP holds READ-ONLY scopes (sharepointScopes.CAN_WRITE_BACK is false). A UI whose
        Approve button claimed to move a file would be describing a capability the deployment
        does not have.

    'approved' counts as a LIVE outcome in doc_has_disposition, so an approved-but-unexecuted
    decision is not re-proposed on the next execute run — asking a reviewer the same question
    twice is how an approval queue stops being trusted.

    A DISCOVER-LIFECYCLE CANDIDATE (`scan:{scan_id}:{file}`) executes here too, once its
    drive_file_id is resolved. Two things happen on this path and nowhere else, both because it
    is the only route that can now mutate a file the Discover queue proposed:

      • the exemption is RE-READ immediately before the action, not merely when the candidate was
        queued. A legal hold added while the reviewer was reading refuses with 409 and leaves the
        row pending — stopping the action and destroying the decision are different outcomes.
      • the inventory's lifecycle status is stamped Archived/Deleted, but only for the actions
        whose outcome those words state truthfully and only once the action APPLIED. The prior
        status goes into the before-state, so an undo restores the estate as well as the file.
    """
    _require_owner(request)   # authorises a move/trash of the file — owner-only
    owner = _owner(request)
    row = core.store.get_disposition_audit(audit_id, owner=owner)
    if row is None or row["result"] != "pending_approval":
        raise HTTPException(404, "no pending approval with that id")
    if not execute:
        detail = "approved by admin — decision recorded, file not touched"
        core.store.set_disposition_audit_result(audit_id, "approved", detail)
        core.store.log_decision(owner, "disposition.approved",
                                detail=f"{row['action']} {row['doc_id']}: recorded, not executed")
        _trace_decision(row["doc_id"], (core.store.get_document(row["doc_id"]) or {}).get("path"),
                        action=row["action"], status="approved", policy_id=row["policy_id"],
                        reason=detail)
        return core.store.get_disposition_audit(audit_id, owner=owner)
    policy = core.store.get_disposition_policy(row["policy_id"], owner=owner) or {}
    cfg = json.loads(policy.get("action_config") or "{}")
    docs = {d["doc_id"]: d for d in core.store.list_all_documents(owner=owner)}
    doc = docs.get(row["doc_id"])
    if doc is None and _is_lifecycle_doc_id(row["doc_id"]):
        # A Discover-lifecycle candidate is not missing — it was never in this table. The two
        # subsystems key documents differently: the lifecycle evaluator stamps
        # `scan:{scan_id}:{file}` (handlers.py), while this governance layer keys on
        # `drive:{id}` / `{source}:{hash}` (documents.resolve_doc_id). list_all_documents holds
        # none of the former, so every lifecycle approval once fell into the clause below and was
        # recorded FAILED with the reason "document no longer exists" — false, and it destroyed
        # the reviewer's decision. #1182 made it record-only; this executes it.
        #
        # THE EXEMPTION IS RE-READ HERE, immediately before the mutation. The batch route has
        # always done this (PRD §11) and this per-row route did not need to while it could not
        # act on a lifecycle candidate at all. It can now, which opens exactly the window the
        # rule exists for: the minutes a reviewer spends reading a queue are when a legal hold
        # gets added, and an approval given at 10:00 must not archive a file put on hold at
        # 10:02. Refused with the row left PENDING — a hold must stop the action without also
        # consuming the decision, or the reviewer is made to give it a second time.
        held = _exempt_now(row["doc_id"], owner)
        if held:
            raise HTTPException(409, f"this candidate cannot be actioned right now: {held}")
        doc = _lifecycle_drive_doc(row["doc_id"], owner)
        if doc is None:
            # No drive_file_id on the inventory row, so there is still nothing to execute — the
            # honest half of the operation, unchanged, with the reason narrowed to what is
            # actually true now. The blocker is no longer "not represented in the governance
            # layer" (that is resolvable, and resolved above); it is that this particular row is
            # not Drive-backed and no connector here can act on it.
            detail = ("approved — recorded, not executed: this lifecycle candidate has no Drive "
                      "file id on its inventory row, so no source action can be performed for it")
            core.store.set_disposition_audit_result(audit_id, "approved", detail)
            core.store.log_decision(owner, "disposition.approved",
                                    detail=f"{row['action']} {row['doc_id']}: recorded, not executed")
            _trace_decision(row["doc_id"], None, action=row["action"], status="approved",
                            policy_id=row["policy_id"], reason=detail)
            return {**(core.store.get_disposition_audit(audit_id, owner=owner) or {}),
                    # Explicit, because the caller asked for execute=true and did not get it. A
                    # response that looked identical to a real execution would be the same lie in
                    # a politer form.
                    "executed": False,
                    "why_not_executed": "this candidate has no Drive file id, so it is not "
                                        "actionable by any connector ACP holds"}
        # Resolved: fall through and execute it exactly as any Drive-backed document, through the
        # same execute_action, the same before-state capture and the same undo. Nothing below
        # this line knows a lifecycle candidate from a governance-layer one, which is the point —
        # a second execution path is a second set of safety rules to keep in step.
    if doc is None:
        core.store.set_disposition_audit_result(audit_id, "failed", "document no longer exists")
        _trace_decision(row["doc_id"], None, action=row["action"], status="failed",
                        policy_id=row["policy_id"], reason="document no longer exists")
        raise HTTPException(410, "document no longer exists")
    result, detail, before = disposition.execute_action(doc, row["action"], cfg,
                                                        _drive_svc(request))
    if result == "applied" and row["action"] == "tag":
        _persist_tags(doc, cfg, row["policy_id"])
    # The ROUTE owns persistence, so the route records what IT is about to change: the inventory
    # row's lifecycle status. execute_action stays a pure Drive operation and knows nothing about
    # scan_inventory — and an undo that put the file back in Drive while the estate still read
    # "Archived" would be a restoration in one system only, with Discover showing it archived and
    # Assess still excluding it. Read BEFORE the stamp below overwrites it.
    ref = _lifecycle_ref(row["doc_id"])
    if ref and before:
        before = {**before, "lifecycle_status":
                  (core.store.get_lifecycle_status(*ref) or {}).get("lifecycle_status")}
    # Recorded BEFORE the result is written, deliberately. A crash between the two then leaves a
    # before-state on a row that still reads pending_approval — harmless, and re-approving
    # overwrites nothing (set_disposition_before_state only fills a NULL). The other order loses
    # the file's origin for an action already marked applied, which is exactly the state PRD §8
    # cannot recover from.
    core.store.set_disposition_before_state(audit_id, before)
    core.store.set_disposition_audit_result(audit_id, result, detail)
    if ref and result == "applied" and row["action"] in _TERMINAL_STATUS:
        # Only after the action actually applied. A candidate whose archive FAILED is still
        # Active, and stamping it anyway would hide the failure in the one view a reviewer checks
        # to see whether the estate really changed — the failure is already in the audit, but
        # nobody reads the audit to find out what is in a folder.
        core.store.set_lifecycle_status(ref[0], ref[1], _TERMINAL_STATUS[row["action"]],
                                        rule_id=row["policy_id"],
                                        reason=f"{row['action']} executed after review approval")
    core.store.log_decision(owner, f"disposition.{result}",
                            detail=f"{row['action']} {row['doc_id']}: {detail}"[:200])
    _trace_decision(row["doc_id"], doc.get("path"), action=row["action"], status=result,
                    policy_id=row["policy_id"], reason=detail)
    return core.store.get_disposition_audit(audit_id, owner=owner)


@router.post("/disposition/approvals/{audit_id}/undo")
def undo_disposition(audit_id: str, request: Request):
    """Put a file back where an applied action moved it (PRD §8).

    Only an APPLIED row with a recorded before-state can be undone, and both halves matter. An
    action that failed may or may not have moved the file, so there is nothing trustworthy to
    reverse; an action applied before this column existed has no origin recorded, and restoring a
    file to a folder nobody wrote down is indistinguishable — afterwards — from moving it
    somewhere new. Both refuse rather than guess.

    OWNER-gated like approve and execute: this touches the estate.

    The undo is itself appended to the audit rather than erasing the original row. The record is
    append-only by design, and "this was archived, then restored" is the true history; a row that
    quietly reverted to pending would claim the archive never happened.
    """
    _require_owner(request)
    owner = _owner(request)
    row = core.store.get_disposition_audit(audit_id, owner=owner)
    if row is None:
        raise HTTPException(404, "no disposition with that id")
    if row.get("result") != "applied":
        raise HTTPException(409, f"only an applied action can be undone — this one is "
                                 f"{row.get('result')!r}")
    before = core.store.get_disposition_before_state(audit_id, owner)
    if not before:
        raise HTTPException(409, "no before-state was recorded for this action, so it cannot be "
                                 "undone — it was applied before ACP recorded where files came "
                                 "from")
    docs = {d["doc_id"]: d for d in core.store.list_all_documents(owner=owner)}
    # A lifecycle candidate is resolved here the same way the approval that executed it resolved
    # it. Without this the fallback below hands undo_action a `scan:` id labelled source="drive",
    # which _drive_file_id rejects — so the one class of row this route now most needs to reverse
    # would refuse with "unsupported source 'drive'": wrong, and impossible for the reader to act
    # on. The fallback is kept for governance-layer rows whose document has since been removed.
    doc = (docs.get(row["doc_id"]) or _lifecycle_drive_doc(row["doc_id"], owner)
           or {"doc_id": row["doc_id"], "source": "drive"})

    result, detail = disposition.undo_action(doc, before, _drive_svc(request))
    undo_id = hashlib.sha256(f"undo:{audit_id}".encode()).hexdigest()[:24]
    core.store.create_disposition_audit(
        undo_id, doc_id=row["doc_id"], policy_id=row["policy_id"],
        action=f"undo_{row['action']}", result=result, detail=detail, owner_email=owner)
    # The estate goes back too, to the status the approval recorded before it stamped a terminal
    # one. Restoring the file in Drive while Discover still reads "Archived" and Assess still
    # excludes it is an undo of the visible half only.
    #
    # The membership test is not defensive noise: set_lifecycle_status RAISES on a status outside
    # its closed set, and a 500 here would abort a request whose file has already been moved back
    # — reporting a failure for an undo that succeeded. A status it cannot restore is left alone
    # and the Drive restoration still stands.
    ref = _lifecycle_ref(row["doc_id"])
    prior = (before or {}).get("lifecycle_status")
    if ref and result == "applied" and prior in core.store.LIFECYCLE_STATUSES:
        core.store.set_lifecycle_status(ref[0], ref[1], prior, rule_id=row["policy_id"],
                                        reason=f"restored by undo of {row['action']}")
    core.store.log_decision(owner, f"disposition.undo_{result}",
                            detail=f"{row['action']} {row['doc_id']}: {detail}"[:200])
    _trace_decision(row["doc_id"], doc.get("path"), action=f"undo_{row['action']}",
                    status=result, policy_id=row["policy_id"], reason=detail)
    if result != "applied":
        raise HTTPException(502, detail)
    return {"undone": audit_id, "audit_id": undo_id, "result": result, "detail": detail}


class BatchApprovalIn(BaseModel):
    audit_ids: list[str]
    policy_id: str
    policy_version: int
    action: str
    reason: str | None = None


@router.post("/disposition/approvals/plan")
def plan_disposition_batch(body: BatchApprovalIn, request: Request):
    """What approving this batch WOULD do, without doing any of it (PRD §7.4's source-effect
    preview, and the safe first half of making lifecycle candidates executable).

    WRITES NOTHING. No audit row moves, no file is touched, no Drive client is even constructed.
    A dry run that can mutate is not a dry run, and the test suite asserts the absence rather
    than trusting the reading.

    Validated by the SAME rules as the approval it previews (_validated_batch), because a plan
    produced by a second copy of those rules is a plan that can drift from what approval will
    actually accept — showing a batch that is then refused, or worse, calling one safe that is
    not.

    It resolves the identifier gap rather than papering over it: a Discover-lifecycle candidate
    is keyed `scan:{scan_id}:{file}`, and the drive_file_id that would make it actionable has
    been on the inventory row all along. Resolving it HERE, in the one path that cannot act,
    makes the gap visible per row — "this one could be archived, that one has no Drive id and
    never could" — before anybody authorises anything.

    Owner-gated like the approval. A preview of what would happen to somebody's estate is still
    a read of somebody's estate.
    """
    _require_owner(request)
    owner = _owner(request)
    submitted, policy, rows = _validated_batch(body, owner, verb="planned")
    cfg = json.loads(policy.get("action_config") or "{}")

    # Resolve every lifecycle row's Drive id in one query per scan, rather than per row.
    by_scan: dict[str, list[str]] = {}
    for audit_id in submitted:
        row = rows.get(audit_id)
        doc_id = str((row or {}).get("doc_id") or "")
        if doc_id.startswith("scan:"):
            _, scan_id, file = doc_id.split(":", 2)
            by_scan.setdefault(scan_id, []).append(file)
    targets = {}
    for scan_id, files in by_scan.items():
        for file, fid in core.store.drive_targets_for_files(scan_id, files, owner).items():
            targets[f"scan:{scan_id}:{file}"] = fid

    plan, blocked = [], 0
    for audit_id in submitted:
        row = rows.get(audit_id)
        if row is None:
            plan.append({"audit_id": audit_id, "blocked": "not found for this owner"})
            blocked += 1
            continue
        doc_id = str(row.get("doc_id") or "")
        if row.get("result") != "pending_approval":
            plan.append({"audit_id": audit_id, "doc_id": doc_id,
                         "blocked": f"already {row.get('result')}"})
            blocked += 1
            continue
        held = _exempt_now(doc_id, owner) if doc_id.startswith("scan:") else None
        if held:
            plan.append({"audit_id": audit_id, "doc_id": doc_id, "blocked": held})
            blocked += 1
            continue
        # A resolved lifecycle candidate is planned AS the Drive document it would become, so
        # the preview is of the real action rather than of the record-only refusal it gets today.
        fid = targets.get(doc_id)
        # Built by the SAME helper the approval builds its document with, so a plan cannot
        # preview a differently shaped document from the one execution will act on. The
        # resolution itself stays batched above — one query per scan, not one per row.
        ref = _lifecycle_ref(doc_id)
        doc = (_drive_doc(fid, ref[1] if ref else None) if fid
               else {"doc_id": doc_id, "source": row.get("source") or "unknown"})
        step = disposition.plan_action(doc, row["action"], cfg)
        if step.get("blocked"):
            blocked += 1
        plan.append({"audit_id": audit_id, "doc_id": doc_id,
                     "drive_file_id": fid, **step})

    return {"planned": len(plan), "actionable": len(plan) - blocked, "blocked": blocked,
            "policy_id": body.policy_id, "policy_version": body.policy_version,
            "action": body.action,
            # Said in the payload, not only in the docs: a caller cannot mistake this for a
            # receipt of something that happened.
            "executed": False, "dry_run": True, "plan": plan}


@router.post("/disposition/approvals")
def approve_disposition_batch(body: BatchApprovalIn, request: Request):
    """Approve a HOMOGENEOUS batch of queued dispositions, by explicit id (PRD §8).

    The per-row sibling below approves one audit id and executes it. This exists because a
    reviewer facing 684 archive candidates will not click 684 times, and the shape that
    replaces those clicks is where a review queue becomes dangerous. Every rule here is one
    the PRD states, and each is a way a bulk approval can mean more than the reviewer meant:

      §11  EXPLICIT IDS ONLY. The caller sends the ids it displayed. There is deliberately no
           filter-based variant — "approve everything matching X" re-expands at execute time to
           whatever matches THEN, which is not what anyone reviewed. store's batch reader takes
           ids and nothing else for the same reason.
      §8   HOMOGENEOUS. Every row must share the policy, the policy VERSION and the action the
           caller named. A batch that mixes them is refused whole, not partially applied: the
           confirmation the reviewer read ("archive 40 files under Retention v3") has to be true
           of every row it covers.
      §11  THE VERSION MUST STILL BE CURRENT. If the policy has been edited since these rows
           were queued, the reviewer is approving an explanation that no longer describes the
           rule. Refused, with the version they asked for and the version that exists now.
      §11  LEGAL HOLD / EXEMPT IS FAIL-CLOSED AND RE-CHECKED HERE, immediately before the
           decision lands, not merely when the candidate was queued. An exemption added during
           review must win.
      §8   A DESTRUCTIVE ACTION NEEDS A STATED REASON. delete/trash always; the reason is
           recorded on every row so the audit answers "why" without a second lookup.
      §8   A PARTIAL BATCH IS NEVER REPORTED AS SUCCESS. The response reconciles: approved +
           refused + already_decided == submitted, and every refusal names its row and its cause.

    Idempotent on (doc_id, policy_version, action) via the audit row's own identity: a row that
    already moved out of pending_approval is reported as already_decided rather than approved a
    second time, so a double-submitted batch cannot double-count.

    RECORDS THE DECISION ONLY — this never touches a source file. Execution stays on the
    per-row /approve path, which is the one that holds the connector semantics (and today only
    supports Drive). Approving in bulk and executing per row is deliberate: it is the ordering
    that lets a legal hold added between the two still stop the mutation.
    """
    _require_owner(request)                     # authorises archival of the estate — owner-only
    owner = _owner(request)
    submitted, policy, rows = _validated_batch(body, owner, verb="approved")

    approved, refused, already = [], [], []
    detail = f"approved in batch under {body.policy_id} v{body.policy_version}"
    if (body.reason or "").strip():
        detail += f" — {body.reason.strip()}"
    for audit_id in submitted:
        row = rows.get(audit_id)
        if row is None:
            refused.append({"audit_id": audit_id, "why": "not found for this owner"})
            continue
        if row.get("result") != "pending_approval":
            already.append({"audit_id": audit_id, "result": row.get("result")})
            continue
        # Re-read the lifecycle state NOW. Queued-then-exempted is the case this exists for.
        held = _exempt_now(row.get("doc_id"), owner)
        if held:
            refused.append({"audit_id": audit_id, "why": held})
            continue
        core.store.set_disposition_audit_result(audit_id, "approved", detail)
        approved.append(audit_id)
        _trace_decision(row["doc_id"],
                        (core.store.get_document(row["doc_id"]) or {}).get("path"),
                        action=row["action"], status="approved",
                        policy_id=row["policy_id"], reason=detail)

    core.store.log_decision(owner, "disposition.batch_approved",
                            detail=f"{len(approved)} approved, {len(refused)} refused, "
                                   f"{len(already)} already decided — {body.action} under "
                                   f"{body.policy_id} v{body.policy_version}")
    return {"submitted": len(submitted), "approved": approved, "refused": refused,
            "already_decided": already,
            # The reconciliation the PRD asks for, computed here rather than left to the client:
            # a caller that only reads len(approved) still cannot mistake a partial for a whole.
            "reconciled": len(approved) + len(refused) + len(already) == len(submitted),
            "executed": False}


#: Most rows one approval (or its dry-run preview) may cover.
#
# TWO INDEPENDENT REASONS, and the first is the one that bites.
#
# The approve loop costs a MEASURED 3.0 queries per row — _exempt_now's lifecycle re-read, the
# audit result write, and _trace_decision's document lookup — and writes as it goes, with no
# transaction around it. 603 queries for 200 rows locally. At 6,000 rows that is ~18,000 round
# trips in one request; against Postgres over a network it is tens of seconds, and a gateway
# timeout leaves rows already approved with NO response to say which. PRD §8 requires that a
# partial batch is never reported as successful — a timeout reports nothing at all, which is the
# same failure with the evidence removed.
#
# And a confirmation dialog covering thousands of files is not meaningful consent. The review
# queue pages at 25/50/100, so a reviewer can only SEE a hundred rows at once; 500 is five times
# the largest page and still bounds the request at ~1,500 queries.
#
# Enforced in _validated_batch rather than at either route, so the dry run and the approval
# cannot disagree about what is submittable. A plan that previewed 2,000 rows the approval would
# then refuse is exactly the drift that sharing this function exists to prevent.
MAX_BATCH_ROWS = 500


def _validated_batch(body, owner: str, *, verb: str):
    """The batch rules of PRD §8/§11, applied identically to a plan and to an approval.

    Extracted rather than duplicated, and that is the point: a dry run is only a preview if the
    thing it previews is validated the same way. Two copies of these rules would drift, and the
    drift would be invisible — the plan would show a batch that approval then refuses, or worse,
    approve one the plan had called safe.

    Returns (submitted ids, policy row, {audit_id: row}). Raises the same HTTPExceptions either
    caller would have raised, with `verb` making the message true for both ("nothing was
    approved" / "nothing was planned").
    """
    submitted = list(dict.fromkeys(body.audit_ids))     # de-duped, order preserved
    if not submitted:
        raise HTTPException(400, "no audit ids submitted")
    if len(submitted) > MAX_BATCH_ROWS:
        raise HTTPException(400, f"{len(submitted)} rows submitted; this endpoint accepts at most "
                                 f"{MAX_BATCH_ROWS} at a time. Approve in smaller batches — a "
                                 f"request this size can time out mid-loop, and rows approved "
                                 f"before that point stay approved with no response to say so.")
    if body.action in ("delete", "trash") and not (body.reason or "").strip():
        raise HTTPException(400, "a delete approval must state a reason")

    policy = core.store.get_disposition_policy(body.policy_id, owner=owner)
    if policy is None:
        raise HTTPException(404, "no such policy")
    current_version = int(policy.get("version") or 1)
    if current_version != body.policy_version:
        raise HTTPException(409, f"policy {body.policy_id} is now version {current_version}, "
                                 f"not the version {body.policy_version} these rows were queued "
                                 f"under — re-evaluate before approving")

    rows = {r["id"]: r for r in core.store.list_disposition_audit_by_ids(submitted, owner)}
    # Homogeneity is checked across the WHOLE batch before anything is written, so a mixed
    # submission changes nothing at all rather than applying its consistent prefix.
    mixed = [rid for rid, r in rows.items()
             if r.get("policy_id") != body.policy_id
             or int(r.get("policy_version") or 0) != body.policy_version
             or r.get("action") != body.action]
    if mixed:
        raise HTTPException(409, f"{len(mixed)} of {len(submitted)} rows are not "
                                 f"{body.action}/{body.policy_id} v{body.policy_version} "
                                 f"({', '.join(sorted(mixed)[:5])}) — nothing was {verb}")
    return submitted, policy, rows


def _is_lifecycle_doc_id(doc_id: str | None) -> bool:
    """Whether this audit row came from the Discover lifecycle evaluator rather than a policy run.

    The evaluator stamps `scan:{scan_id}:{file}`; documents.resolve_doc_id produces
    `drive:{id}` or `{source}:{hash}`, and never the `scan:` form. Deliberately a positive test
    for the lifecycle shape rather than "not in the documents table": a genuinely deleted
    Drive-backed document must keep reporting that it no longer exists, which is true and
    useful, and a broader rule would swallow it.
    """
    return str(doc_id or "").startswith("scan:")


def _lifecycle_ref(doc_id: str | None) -> tuple[str, str] | None:
    """(scan_id, file) for a lifecycle candidate, or None if the id is not that shape.

    `_is_lifecycle_doc_id` tests the PREFIX only, and deliberately so — it answers "did the
    lifecycle evaluator stamp this", which a malformed two-part id is still evidence of. This
    answers the different question of whether the id can be taken apart, so callers that need
    the pieces do not inherit a ValueError from the callers that only needed the verdict.
    """
    if not _is_lifecycle_doc_id(doc_id):
        return None
    parts = str(doc_id).split(":", 2)
    return (parts[1], parts[2]) if len(parts) == 3 else None


def _drive_doc(fid: str, file: str | None = None) -> dict:
    """The document shape `disposition.plan_action` and `disposition.execute_action` both take.

    ONE builder, used by the dry run and by the approval it previews. Two would drift, and the
    drift is the invisible kind: a plan that resolved a candidate differently from the execution
    it claims to preview is a plan of a different action, and nothing would say so.
    """
    return {"doc_id": f"drive:{fid}", "source": "drive", "path": file}


def _lifecycle_drive_doc(doc_id: str | None, owner: str) -> dict | None:
    """The Drive document a `scan:{scan_id}:{file}` candidate refers to, or None.

    The bridge between the two identifier spaces. The lifecycle evaluator keys on the scan grain
    and the governance layer keys on `drive:{id}`; the value that connects them has been on the
    inventory row since Discover wrote it, and resolving it is the whole of what stood between a
    reviewer's approval and the action they approved.

    None has exactly one meaning: this candidate has no drive_file_id, so nothing could act on
    it. That is a record-only outcome and not a failure — it is what EVERY lifecycle approval
    got before execution existed, and it stays the answer for a row that is not Drive-backed.

    Owner-scoped by drive_targets_for_files, which joins scan_runs.owner_email: a caller cannot
    resolve a file id out of somebody else's scan and act on it.
    """
    ref = _lifecycle_ref(doc_id)
    if ref is None:
        return None
    scan_id, file = ref
    item = core.store.lifecycle_source_item(scan_id, file, owner)
    # drive_file_id is a shared inventory field: Graph adapters also put their item ids here.
    # An opaque id alone must never authorize a Google Drive operation on another provider.
    if not item or str(item.get("source") or "").lower() != "drive":
        return None
    fid = item.get("drive_file_id")
    return _drive_doc(fid, file) if fid else None


#: The lifecycle status an applied action leaves the inventory row in.
#
# Only the two actions whose outcome the status vocabulary can state TRUTHFULLY are here. A
# rename leaves the file exactly where it was, and a bare `move` is not necessarily an archival —
# its target folder is whatever the policy configured. Stamping either "Archived" would put a
# claim in the estate view the action does not support, and that view is what the next reviewer
# reads as fact, and what Assess's default exclusion acts on.
_TERMINAL_STATUS = {"archive": "Archived", "delete": "Deleted"}


def _exempt_now(doc_id: str | None, owner: str) -> str | None:
    """Why this document must not be dispositioned right now, or None (PRD §11).

    Reads scan_inventory, NOT documents. The first draft of this called get_document and asked
    it for `lifecycle_status` — a column that table does not have, so it read None, compared it
    to "Exempted", and let every row through. A fail-OPEN safety check that looks exactly like a
    fail-closed one is worse than no check, because the route's docstring then promises
    something nothing enforces.

    The lifecycle evaluator stamps `scan:{scan_id}:{file}` as the doc id (handlers.py), which is
    what makes the real state reachable. Any OTHER id shape is refused rather than guessed at:
    those rows come from paths this batch route was not written for, and they remain approvable
    one at a time on /approvals/{audit_id}/approve.

    A file that was ALREADY exempt never reaches here — the evaluator skips it before an audit
    row exists. The case this catches is the one that matters: queued as a candidate, then
    exempted while the reviewer was still reading the queue."""
    if not doc_id:
        return "no document id on the audit row"
    if not str(doc_id).startswith("scan:"):
        return ("not a discover-lifecycle candidate — approve this row individually")
    try:
        _, scan_id, file = str(doc_id).split(":", 2)
        # Owner-scoped already: the row came back from list_disposition_audit_by_ids, which
        # filters on owner_email, so this is a re-read of a row this caller may see.
        state = core.store.get_lifecycle_status(scan_id, file) or {}
    except Exception:                            # noqa: BLE001 — refuse, never approve blindly
        return "lifecycle state could not be re-checked"
    status = str(state.get("lifecycle_status") or "")
    if status in ("Exempted", "Deleted", "Archived"):
        return f"document is now {status} — excluded from bulk disposition"
    if (state.get("lifecycle_override_reason") or "").strip():
        # A human already said "keep this". Approving the rule's recommendation in a batch
        # would silently overturn a reasoned individual decision.
        return "a reviewer has overridden this recommendation — approve it individually or clear the override"
    return None


@router.post("/disposition/approvals/{audit_id}/reject")
def reject_disposition(audit_id: str, request: Request):
    """Decline the queued action. Recorded (result=rejected), never re-queued
    automatically — a later execute run may propose it again only if the doc
    still matches, since rejected rows don't block re-evaluation."""
    _require_admin(request)
    owner = _owner(request)
    row = core.store.get_disposition_audit(audit_id, owner=owner)
    if row is None or row["result"] != "pending_approval":
        raise HTTPException(404, "no pending approval with that id")
    core.store.set_disposition_audit_result(audit_id, "rejected", "declined by admin")
    core.store.log_decision(owner, "disposition.rejected",
                            detail=f"{row['action']} {row['doc_id']}")
    _trace_decision(row["doc_id"], (core.store.get_document(row["doc_id"]) or {}).get("path"),
                    action=row["action"], status="rejected", policy_id=row["policy_id"],
                    reason="declined by admin")
    return core.store.get_disposition_audit(audit_id, owner=owner)

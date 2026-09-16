"""Running an archive auto-fire pass: store reads, provider calls, and the audit row per item.

THE SEAM. archive_autofire.py decides, archive_sources.py acts on a tenant, and this module is
the only place the two meet a database. Everything it does is one of four things — read the
candidates, decide, execute, record — and it is written so the third can be removed (dry run) or
refused (kill switch) without changing the first, second or fourth. That is what makes a dry run
a real rehearsal rather than a different code path that happens to look similar.

FOUR PROPERTIES THIS FILE IS RESPONSIBLE FOR, and where each is enforced:

  * ONE EXECUTION PER DECISION. `store.claim_archive_execution` returns `created=False` when the
    idempotency key already exists, and this module returns the ORIGINAL record without touching
    the tenant. The guarantee is the unique index; this is the code that honours it.
  * THE KILL SWITCH IS LIVE. It is re-read from the store before EVERY item, not taken from the
    snapshot, because an operator who pulls it mid-run means "stop now" and a snapshot read at
    the top of a 500-item run would keep moving files for another twenty minutes.
  * NOTHING UNCERTAIN IS REPORTED AS DONE. The only path to `archived` runs through
    `archive_autofire.classify_outcome` over a verification read; every other path lands on
    `recovery_required` or `blocked`.
  * NO CREDENTIALS, EVER, in a row or an event. Provider messages reach an audit row through
    `_safe_detail` and an event through `archive_autofire.event_payload`, and both withhold
    anything credential-shaped rather than storing it.

WHY THE RUN IS SEQUENTIAL. Moving files is not throughput-bound work, and a parallel version
would have to coordinate the per-run and per-day ceilings across workers — a distributed counter
guarding an irreversible action, to save seconds on a bounded queue. The ceilings are small by
design; a sequential loop makes them exact.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

import archive_autofire as af
import archive_evidence
import archive_sources
from swallowed import swallowed


def source_connection(row: dict) -> str:
    """The stable connection identity a policy authorizes, as one string.

    `sharepoint:<drive id>` names the document library; `onedrive:me` names the signed-in user's
    own drive, which legitimately has no drive id (scanner._sp_base reads a missing one as
    /me/drive). The drive is the right grain rather than the site: item ids are unique only
    within a drive, so a policy that authorized a SITE would be authorizing a set of item-id
    namespaces rather than one.
    """
    source = str(row.get("source") or "").strip().lower()
    drive = str(row.get("drive_id") or "").strip()
    return f"{source}:{drive or 'me'}"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _safe_detail(text: str) -> str:
    """A provider message, bounded and stripped of anything credential-shaped, for an audit row.

    Reuses the event allow-list's secret markers rather than keeping a second list: the risk is
    identical in both destinations, and two lists would drift the moment somebody added a marker
    to one.
    """
    payload = af.event_payload({"detail": str(text or "")})
    return payload.get("detail", "")


# ── Evaluation ───────────────────────────────────────────────────────────────

def evaluate(store, owner: str, scan_id: str, *, policy: dict | None = None,
             now: datetime | None = None) -> dict:
    """Decide the lane for every archive candidate in one scan. Reads only; moves nothing.

    Returns `{"snapshot_id", "policy", "items", "counts"}` where each item carries its state, the
    reason for it, the evidence behind it and the destination it would go to. This is what the
    Discovery surface renders and what an operator inspects BEFORE anything runs — the PRD's
    "the user can inspect the supersession evidence before execution" is this function being
    callable on its own.
    """
    stamp = now or _now()
    live = policy if policy is not None else load_policy(store, owner)
    normalized = af.normalize_policy(live)
    snapshot = af.policy_snapshot(normalized)
    rows = store.list_archive_scan_rows(scan_id, owner)
    rules = {str(p.get("policy_id")): p for p in store.list_disposition_policies(owner)}

    candidates = [r for r in rows if str(r.get("lifecycle_status") or "") == "Archive Candidate"]
    # Siblings are every row in the scan, candidates included: a document can be superseded by
    # another document that is itself an archive candidate (a v2 that a v3 replaced), and
    # excluding candidates would silently drop that chain.
    siblings = list(rows)

    day_used = store.archive_actions_today(owner)
    run_used = 0
    items = []
    for row in candidates:
        rule = rules.get(str(row.get("lifecycle_rule_id") or ""))
        evidence = archive_evidence.derive(
            row, siblings, policy=normalized,
            action_config=(rule or {}).get("action_config"))
        candidate = dict(row)
        candidate["source_connection"] = source_connection(row)
        existing = _existing_execution(store, owner, candidate, snapshot, normalized)
        decision = af.decide(candidate, policy=normalized, evidence=evidence, now=stamp,
                             executed=existing, day_used=day_used, run_used=run_used)
        if decision["state"] == af.ELIGIBLE_AUTO:
            # Counted against BOTH ceilings at DECISION time.
            #
            # Against the per-run ceiling so the surface an operator reads shows the same eligible
            # set the run will attempt — counting only at execution would show 40 eligible and
            # move 25, which reads as 15 silent failures.
            #
            # And against the DAY's ceiling, which is the one that would otherwise be breached.
            # `day_used` is read once before this loop; without incrementing it here, a tenant
            # 99 moves into a 100-a-day ceiling would see all 25 remaining candidates decided
            # against the same stale 99 and then move all 25 — exceeding the daily limit by 24
            # while every individual decision looked correct. The ceiling is the one control a
            # customer sets to bound the blast radius of a misconfiguration, so it has to hold
            # within a run and not only between them.
            run_used += 1
            day_used += 1
        items.append({
            "file": row.get("file"), "path": row.get("path"),
            "source": row.get("source"), "source_connection": candidate["source_connection"],
            "source_item_id": row.get("drive_file_id"), "drive_id": row.get("drive_id"),
            # The modification marker AS EVALUATED — carried on the item so the execution can
            # compare it with the live one without re-reading the inventory (and without the
            # window a re-read would open).
            "source_marker": row.get("source_modified"),
            "lifecycle_rule_id": row.get("lifecycle_rule_id"),
            "state": decision["state"], "state_label": af.STATE_LABELS.get(decision["state"], ""),
            "reason": decision["reason"], "evidence": decision["evidence"],
            "evidence_summary": archive_evidence.summarize(decision["evidence"]),
            "rejected_evidence": decision["rejected_evidence"],
            "destination_path": decision["destination"],
            "execution_id": (existing or {}).get("execution_id"),
        })
    return {"snapshot_id": snapshot["snapshot_id"], "policy": normalized,
            "dry_run": normalized["dry_run"], "items": items, "counts": _counts(items)}


def _counts(items: list[dict]) -> dict:
    counts = {state: 0 for state in af.STATE_LABELS}
    for item in items:
        counts[item["state"]] = counts.get(item["state"], 0) + 1
    counts["eligible"] = counts.get(af.ELIGIBLE_AUTO, 0)
    counts["completed"] = counts.get(af.ARCHIVED, 0)
    counts["blocked"] = counts.get(af.BLOCKED, 0)
    return counts


def _existing_execution(store, owner: str, candidate: dict, snapshot: dict,
                        policy: dict) -> dict | None:
    """This candidate's already-recorded execution under this policy snapshot, if any.

    Keyed the same way the execution itself is, so "has this already run?" and "would this be a
    duplicate?" are the same question asked once rather than two predicates that could disagree.
    """
    destination = af.destination_path(policy["archive_root"], candidate.get("path") or "",
                                      preserve_hierarchy=policy["preserve_hierarchy"])
    if not destination or not candidate.get("drive_file_id"):
        return None
    key = af.idempotency_key(tenant=owner, source_connection=candidate["source_connection"],
                             source_item_id=str(candidate.get("drive_file_id")),
                             destination=destination, snapshot_id=snapshot["snapshot_id"])
    row = store.get_archive_execution(key, owner)
    if not row:
        return None
    return {"execution_id": row.get("execution_id"), "state": row.get("state"),
            "detail": row.get("detail"), "destination_path": row.get("destination_path"),
            "evidence": _load_json(row.get("evidence_json"))}


def _load_json(blob):
    try:
        value = json.loads(blob or "[]")
    except (TypeError, ValueError):
        return []
    return value if isinstance(value, list) else []


def load_policy(store, owner: str) -> dict:
    """This tenant's live policy, normalized. A tenant that never configured one gets the
    shipped defaults, which are disabled — so "never set up" and "switched off" behave
    identically and only the UI distinguishes them."""
    stored = store.get_archive_policy(owner)
    return af.normalize_policy((stored or {}).get("policy"))


# ── Execution ────────────────────────────────────────────────────────────────

def run(store, owner: str, scan_id: str, *, source_factory, actor: str,
        now: datetime | None = None, policy: dict | None = None) -> dict:
    """Evaluate, then execute every eligible item. Returns the run report.

    `source_factory` is called with the source connection string and returns an
    `archive_sources.GraphArchiveSource` (or None when no credential is available for it). It is
    a parameter rather than built here so a route supplies the caller's token and a test supplies
    a fake, and so this module never sees a credential it could accidentally store.

    THE RUN STOPS ON THE KILL SWITCH but does not abandon work in flight: an item whose move has
    already been issued is finished and recorded, because a move issued and then forgotten is the
    one outcome worse than a move that should not have started.
    """
    stamp = now or _now()
    store.reconcile_source_lifecycle(scan_id)
    report = evaluate(store, owner, scan_id, policy=policy, now=stamp)
    snapshot = af.policy_snapshot(report["policy"])
    store.save_archive_snapshot(snapshot["snapshot_id"], owner, snapshot["policy"], scan_id)

    eligible = [i for i in report["items"] if i["state"] == af.ELIGIBLE_AUTO]
    _emit(store, owner, scan_id, "lifecycle.archive_run_started", {
        "scan_id": scan_id, "snapshot_id": snapshot["snapshot_id"],
        "eligible": len(eligible), "dry_run": report["policy"]["dry_run"]})

    executions = []
    stopped = ""
    for index, item in enumerate(eligible):
        # LIVE, every iteration. A snapshot read at the top of the loop would keep moving files
        # for as long as the queue lasted after somebody pulled the switch.
        if load_policy(store, owner)["kill_switch"]:
            stopped = ("The kill switch was turned on during this run. No further moves were "
                       "started; work already in flight was finished and recorded.")
            break
        executions.append(execute_item(
            store, owner, item, scan_id=scan_id, snapshot=snapshot, actor=actor,
            source_factory=source_factory, now=_now()))

    completed = sum(1 for e in executions if e["state"] == af.ARCHIVED)
    blocked = sum(1 for e in executions if e["state"] == af.BLOCKED)
    recovery = sum(1 for e in executions if e["state"] == af.RECOVERY_REQUIRED)
    _emit(store, owner, scan_id, "lifecycle.archive_run_finished", {
        "scan_id": scan_id, "snapshot_id": snapshot["snapshot_id"], "eligible": len(eligible),
        "completed": completed, "blocked": blocked, "detail": stopped,
        "dry_run": report["policy"]["dry_run"]})
    return {"snapshot_id": snapshot["snapshot_id"], "dry_run": report["policy"]["dry_run"],
            "eligible": len(eligible), "completed": completed, "blocked": blocked,
            "recovery_required": recovery, "stopped": stopped, "executions": executions,
            "items": report["items"], "counts": report["counts"]}


def execute_item(store, owner: str, item: dict, *, scan_id: str, snapshot: dict, actor: str,
                 source_factory, now: datetime | None = None) -> dict:
    """One item: claim, preflight, move, verify, record. Returns the execution row as stored.

    Every early return leaves the source file untouched, and every one of them records WHY —
    an execution row with no reason is indistinguishable from a defect, and this is the record a
    customer's records manager reads when they ask why a document moved.
    """
    stamp = now or _now()
    policy = snapshot["policy"]
    evidence = list(item.get("evidence") or [])
    primary = evidence[0] if evidence else {}
    key = af.idempotency_key(tenant=owner, source_connection=item["source_connection"],
                             source_item_id=str(item.get("source_item_id") or ""),
                             destination=item.get("destination_path") or "",
                             snapshot_id=snapshot["snapshot_id"])
    execution_id = uuid.uuid4().hex
    row, created = store.claim_archive_execution(
        idempotency_key=key, execution_id=execution_id, owner_email=owner, scan_id=scan_id,
        file=item.get("file"), policy_id=item.get("lifecycle_rule_id"),
        snapshot_id=snapshot["snapshot_id"], source_connection=item["source_connection"],
        source_item_id=str(item.get("source_item_id") or ""),
        source_drive_id=item.get("drive_id"), source_etag=None,
        source_path=item.get("path") or "", replacement_item_id=primary.get("replacement_item_id", ""),
        replacement_path=primary.get("replacement_path"),
        evidence_json=json.dumps(evidence, sort_keys=True),
        destination_path=item.get("destination_path") or "", actor=actor,
        dry_run=bool(policy["dry_run"]))
    if not created:
        # THE IDEMPOTENCY CONTRACT, and the reason it is a return rather than a raise: a repeated
        # submission is not an error, it is the same request arriving twice, and the caller is
        # owed the original outcome.
        return row

    execution_id = row["execution_id"]
    _emit(store, owner, scan_id, "lifecycle.archive_item_started", {
        "execution_id": execution_id, "snapshot_id": snapshot["snapshot_id"],
        "source_path": item.get("path"), "replacement_path": primary.get("replacement_path"),
        "destination_path": item.get("destination_path"), "phase": "verifying source state",
        "dry_run": bool(policy["dry_run"])})

    source = source_factory(item["source_connection"])
    if source is None:
        return _finish(store, owner, execution_id, scan_id, af.BLOCKED,
                       "No connection is available for this source, so nothing was attempted.",
                       stamp)

    try:
        live = archive_sources.probe(
            source, snapshot={"drive_id": item.get("drive_id"),
                              "source_item_id": item.get("source_item_id"),
                              "source_modified": primary.get("source_modified")},
            evidence=primary, destination_path=item.get("destination_path") or "")
    except Exception as e:  # noqa: BLE001 — an unreadable preflight is UNKNOWN, never a pass
        swallowed("archive preflight")
        live = {"probe_error": f"{type(e).__name__}"}

    # THE SNAPSHOT SIDE IS THE EVALUATION'S OWN VALUES, never the live ones. Passing the live
    # marker on both sides would make `source_unchanged` compare a value with itself and pass
    # unconditionally — a stale-document guard that cannot fail, which is worse than no guard
    # because it reports a pass. `source_marker` here is what discovery wrote to
    # scan_inventory.source_modified for this row.
    checks = af.preflight(
        snapshot={"source_item_id": item.get("source_item_id"),
                  "source_marker": item.get("source_marker"),
                  "source_modified": primary.get("source_modified")},
        live=live, policy=load_policy(store, owner), already_executed=False)
    store.update_archive_execution(execution_id, owner,
                                   preflight_json=json.dumps(checks, sort_keys=True),
                                   source_etag=live.get("source_etag") or "",
                                   started_at=stamp.isoformat())
    if checks["route"] != "proceed":
        state = af.BLOCKED
        detail = _preflight_detail(checks)
        _emit(store, owner, scan_id, "lifecycle.archive_item_blocked", {
            "execution_id": execution_id, "source_path": item.get("path"), "detail": detail})
        return _finish(store, owner, execution_id, scan_id, state, detail, stamp)

    if policy["dry_run"]:
        # A dry run runs EVERY check above against the live tenant and stops exactly here, at the
        # one call that changes something. That is what makes it evidence about what a real run
        # would do rather than a separate, simpler code path that agrees by construction.
        return _finish(store, owner, execution_id, scan_id, af.ELIGIBLE_AUTO,
                       "Dry run: every safety check passed and the move was not performed.", stamp)

    try:
        verification = source.move(drive_id=item.get("drive_id"),
                                   item_id=str(item.get("source_item_id") or ""),
                                   etag=live.get("source_etag"),
                                   destination_path=item.get("destination_path") or "")
    except archive_sources.ArchiveSourceError as e:
        return _handle_failure(store, owner, execution_id, scan_id, e, stamp)
    except Exception as e:  # noqa: BLE001 — an unclassified provider failure is the AMBIGUOUS case
        detail = _safe_detail(f"The move failed in a way ACP could not classify: "
                              f"{type(e).__name__}: {e}")
        _emit(store, owner, scan_id, "lifecycle.archive_recovery_required", {
            "execution_id": execution_id, "source_path": item.get("path"), "detail": detail})
        return _finish(store, owner, execution_id, scan_id, af.RECOVERY_REQUIRED, detail, stamp)

    state, detail = af.classify_outcome(verification)
    store.update_archive_execution(
        execution_id, owner,
        destination_item_id=verification.get("destination_item_id") or "",
        destination_url=verification.get("destination_url") or "")
    _emit(store, owner, scan_id,
          "lifecycle.archive_item_completed" if state == af.ARCHIVED
          else "lifecycle.archive_recovery_required",
          {"execution_id": execution_id, "source_path": item.get("path"),
           "destination_path": item.get("destination_path"), "state": state,
           "detail": _safe_detail(detail)})
    return _finish(store, owner, execution_id, scan_id, state, _safe_detail(detail), stamp)


def _preflight_detail(checks: dict) -> str:
    """Why preflight refused, naming every check rather than only the first.

    The `cancel` route says something different from `review` and must not be flattened into it:
    a stale evaluation is not a decision a person can make, it is one that has to be redone, and
    telling a reviewer to review it would waste their time on a question with no answer.
    """
    parts = [c["detail"] for c in checks["checks"] if c["result"] in (af.FAIL, af.UNKNOWN)]
    lead = ("The evaluation is out of date, so the action was cancelled and will be re-evaluated: "
            if checks["route"] == "cancel"
            else "Automatic archival was refused and this needs a person: ")
    return (lead + " ".join(parts))[:1000]


def _handle_failure(store, owner: str, execution_id: str, scan_id: str,
                    error: archive_sources.ArchiveSourceError, stamp: datetime) -> dict:
    """Route one classified provider failure. The source file is untouched in every branch here.

    Rate limiting is the only kind that stays open: it is recorded as still-eligible with an
    attempt count, so a later pass re-runs it UNDER THE SAME IDEMPOTENCY KEY — which is what
    stops a retry becoming a second move. Everything else is terminal for this execution.
    """
    state, route = af.FAILURE_ROUTES.get(error.kind, (af.RECOVERY_REQUIRED, "recover"))
    detail = _safe_detail(error.detail)
    if route == "retry":
        row = store.get_archive_execution_by_id(execution_id, owner) or {}
        attempts = int(row.get("attempts") or 0) + 1
        store.update_archive_execution(execution_id, owner, attempts=attempts,
                                       state=af.ELIGIBLE_AUTO, detail=detail)
        return store.get_archive_execution_by_id(execution_id, owner) or {}
    kind = ("lifecycle.archive_recovery_required" if state == af.RECOVERY_REQUIRED
            else "lifecycle.archive_item_blocked")
    _emit(store, owner, scan_id, kind, {"execution_id": execution_id, "detail": detail,
                                        "state": state})
    return _finish(store, owner, execution_id, scan_id, state, detail, stamp)


def _finish(store, owner: str, execution_id: str, scan_id: str, state: str, detail: str,
            stamp: datetime) -> dict:
    store.update_archive_execution(execution_id, owner, state=state, detail=detail,
                                   completed_at=stamp.isoformat())
    if state == af.ARCHIVED:
        row = store.get_archive_execution_by_id(execution_id, owner) or {}
        if row.get('file') and not row.get('dry_run'):
            store.set_lifecycle_status(scan_id,row['file'],'Archived',rule_id=row.get('policy_id'),
                reason='archive move verified at source destination',evidence_id=execution_id)
    try:
        store.log_decision(owner, f"archive_autofire.{state}", scan_id=scan_id,
                           file=None, rule_id=None, detail=detail[:500])
    except Exception:  # noqa: BLE001 — narration must never fail the outcome it narrates
        swallowed("archive decision log")
    return store.get_archive_execution_by_id(execution_id, owner) or {}


def _emit(store, owner: str, scan_id: str, kind: str, payload: dict) -> None:
    """One bounded lifecycle event. Never fails the work it describes.

    `event_payload` is applied HERE, at the single call site, rather than trusted to each caller:
    the allow-list is only a guarantee if nothing can route around it, and the store's own
    `detail` handling bounds size but cannot judge content.
    """
    try:
        store.append_orchestration_event(
            owner_email=owner, kind=kind, scan_id=scan_id, workflow="lifecycle_archive",
            detail=af.event_payload(payload))
    except Exception:  # noqa: BLE001 — a missing event is a gap in narration, never a wrong move
        swallowed("archive lifecycle event")

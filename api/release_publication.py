"""Is the copy at the Release destination the CURRENT corrected copy? A server-authored answer.

A publication receipt (release_documents.artifact_digest) names the exact bytes the provider
holds. file_records.corrected_sha256 names the current corrected copy, and approving a value
after publication moves it (apply_approved_values saves a new copy). Nothing else joins the two,
so a receipt reading 'published' said nothing about whether it was the copy the user now has.

This projection is computed at the route layer ONLY. It must never be folded into
store.release_status()['documents']: release_report_delivery._fingerprint hashes those
documents, and adding fields there would mark every frozen report bundle out of date.

Identity rules (never inferred from a filename, a provider checksum or a timestamp):
- a receipt with no exact ``sha256:<hex>`` tag is ``identity_unknown`` — never ``current``;
- a failed or interrupted retry leaves the previously published copy at the provider, so a
  retained exact digest that differs from the current copy is still ``out_of_date``. (Since
  store.record_release_document stopped letting a failure overwrite a delivered receipt, new
  rows keep status 'published' there; the failed/interrupted forms remain for older rows and
  for queued rows whose job was lost);
- three failure categories stamp the ATTEMPTED digest over the row, so on those rows the digest
  does not name what the provider holds.
"""
from __future__ import annotations

import json
import re

from release_artifacts import artifact_tag, release_ready

_EXACT = re.compile(r"sha256:([0-9a-f]{64})")
IN_FLIGHT = frozenset({"queued", "running", "publishing"})
UNSETTLED = frozenset({"failed", "interrupted"})
# handlers._release_failure / publish_files write the attempted digest for exactly these.
ATTEMPTED_DIGEST_CATEGORIES = frozenset({
    "release_assessment_remaining", "release_assessment_unavailable", "corrected_copy_unreadable"})

REPUBLISH_UNAPPLIED = "Approved changes are still being saved to the corrected copy."
REPUBLISH_IN_FLIGHT = "A publication is already in progress. Wait for it to finish."
REPUBLISH_NOT_READY = "The corrected copy is not ready to publish. Review it before publishing again."


def exact_digest(value) -> str | None:
    match = _EXACT.fullmatch(str(value or ""))
    return match.group(1) if match else None


def held_copy(document: dict) -> tuple[bool, str | None]:
    """(has publication evidence, exact hex digest of the copy the provider holds or None)."""
    status = document.get("status")
    evidence = status == "published" or bool(document.get("published_at") or document.get("released_document_id"))
    if not evidence:
        return False, None
    if status != "published" and document.get("failure_category") in ATTEMPTED_DIGEST_CATEGORIES:
        return True, None
    return True, exact_digest(document.get("artifact_digest"))


def document_state(document: dict, record: dict | None) -> dict:
    current = (record or {}).get("corrected_sha256") or None
    status = document.get("status")
    evidence, held = held_copy(document)
    if status in IN_FLIGHT:
        state = "publishing"
    elif status == "published" or (status in UNSETTLED and evidence):
        if held is None or current is None:
            state = "identity_unknown"
        elif held == current:
            state = "current" if status == "published" else "failed"
        else:
            state = "out_of_date"
    elif status in UNSETTLED:
        state = "failed"
    else:
        state = "not_published"
    return {"publication_state": state,
            "published_artifact_digest": artifact_tag(held) if held else None,
            "current_artifact_digest": artifact_tag(current) if current else None}


def _overall(states: list[str]) -> str:
    if not states:
        return "not_published"
    for state, label in (("publishing", "publishing"), ("out_of_date", "out_of_date"),
                         ("identity_unknown", "identity_unknown"), ("failed", "attention"),
                         ("not_published", "attention")):
        if state in states:
            return label
    return "current"


def project(store, scan_id: str, owner: str, release: dict | None) -> dict:
    """Per-document currency fields plus the top-level ``publication`` verdict.

    ``release`` is an owner-scoped store.release_status() projection (or None). Records are
    read owner-scoped too, so a foreign release can never borrow another scan's identity.
    """
    documents = list((release or {}).get("documents") or [])
    records = store.get_file_records(scan_id, owner=owner, files=[d["file"] for d in documents]) if documents else {}
    projected, out_of_date, unknown = [], [], []
    for document in documents:
        record = records.get(document["file"])
        fields = document_state(document, record)
        projected.append({**document, **fields})
        if fields["publication_state"] == "out_of_date":
            out_of_date.append({
                "file": document["file"],
                "published_artifact_digest": fields["published_artifact_digest"],
                "current_artifact_digest": fields["current_artifact_digest"],
                "published_at": document.get("published_at"),
                "current_remediated_at": (record or {}).get("remediated_at"),
                "requires_remaining_issue_confirmation": bool(
                    not release_ready(record, False) and release_ready(record, True)),
                "last_attempt_failure": None,
            })
        elif fields["publication_state"] == "identity_unknown":
            unknown.append(document["file"])
    if out_of_date:
        failures = last_attempt_failures(store, scan_id, (release or {}).get("id"),
                                         {row["file"]: row["current_artifact_digest"] for row in out_of_date})
        for row in out_of_date:
            row["last_attempt_failure"] = failures.get(row["file"])
    states = [row["publication_state"] for row in projected]
    reason = None
    if out_of_date:
        if "publishing" in states:
            reason = REPUBLISH_IN_FLIGHT
        elif any(not release_ready(records.get(row["file"]), True) for row in out_of_date):
            reason = REPUBLISH_NOT_READY
        elif any(store.count_unapplied_approved_values(scan_id, row["file"]) for row in out_of_date):
            reason = REPUBLISH_UNAPPLIED
    return {"documents": projected,
            "publication": {"state": _overall(states), "out_of_date": out_of_date,
                            "identity_unknown": unknown,
                            "can_republish": bool(out_of_date) and reason is None,
                            "republish_blocked_reason": reason}}


def active_publish_digests(store, scan_id: str, owner: str, release_id: str) -> dict[str, set]:
    """file -> artifact tags that a queued/running publish_file job of this release carries."""
    with store._db.cursor() as cur:
        store._db.execute(cur, "SELECT payload FROM jobs WHERE scan_id=%s AND type='publish_file' "
                               "AND status IN ('queued','running')", (scan_id,))
        rows = store._db.fetchall(cur)
    active: dict[str, set] = {}
    for row in rows:
        try:
            payload = json.loads(row["payload"]) if isinstance(row.get("payload"), str) else row.get("payload")
        except (TypeError, ValueError):
            continue
        if (isinstance(payload, dict) and payload.get("release_id") == release_id
                and payload.get("owner") == owner and payload.get("file")):
            active.setdefault(payload["file"], set()).add(payload.get("artifact_digest"))
    return active


def last_attempt_failures(store, scan_id: str, release_id: str | None, current: dict) -> dict:
    """file -> the latest refused/failed attempt to publish exactly its CURRENT copy.

    store.record_release_document keeps a delivered receipt exact and appends the refusal to the
    immutable decision_log instead ('release.publish_attempt_failed'), so a failed republish is
    readable without re-identifying the delivered reports. ``current`` maps file -> the current
    artifact tag, and a failure is attached only when the attempt was FOR that tag. Time cannot
    decide this: a V2 job refused after V3 was saved is logged after V3's save, and attaching it
    would tell the user V3 was attempted and failed. An attempt with no recorded digest is never
    attributed. The caller has already proven ownership of ``scan_id``; rows are also bound to
    this ``release_id``.
    """
    if not release_id or not current or not hasattr(store, "_db"):
        return {}
    files = sorted(current)
    with store._db.cursor() as cur:
        store._db.execute(cur,
            "SELECT ts,file,detail FROM decision_log WHERE scan_id=%s AND action=%s AND file IN ("
            + ",".join(["%s"] * len(files)) + ") ORDER BY ts DESC",
            (scan_id, "release.publish_attempt_failed", *files))
        rows = store._db.fetchall(cur)
    latest = {}
    for row in rows:
        file = row.get("file")
        if file in latest:
            continue
        try:
            detail = json.loads(row.get("detail") or "{}")
        except (TypeError, ValueError):
            continue
        if not isinstance(detail, dict) or detail.get("release_id") != release_id:
            continue
        attempted = exact_digest(detail.get("attempted_artifact_digest"))
        if not attempted or artifact_tag(attempted) != current.get(file):
            continue
        latest[file] = {"failure_category": detail.get("failure_category"),
                        "explanation": detail.get("explanation"),
                        "attempted_artifact_digest": detail.get("attempted_artifact_digest"),
                        "at": row.get("ts")}
    return latest

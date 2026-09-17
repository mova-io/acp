"""Versioned reviewer decisions on individual saved changes.

A reviewer can Accept / request a Correction / Reject / mark "Unable to verify" one saved
before->after record. Both kinds of saved change are reviewable, and the second kind is the
point: `remediation_diff` rows are the writes a re-check already CONFIRMED, while
`unverified_changes.saved_changes` are writes the AI applied and nothing has re-checked. It is
the unverified ones a human actually has to look at, so a review surface that only offered the
verified ones was offering the work that was already done.

A verdict is only meaningful for the exact artifact the reviewer looked at, so each decision is
BOUND to two identities, and BOTH are now REQUIRED on every mutation:

* ``expected_sha256`` — the artifact identity the client was showing. Absent, the server would
  bind acceptance to whatever bytes exist at save time, which may be a copy nobody reviewed.
* ``change_digest`` — sha256 of ``f"{rule_id}\\n{before}\\n{after}"`` for the change itself.

The identity a decision binds to is ``file_records.corrected_sha256`` when a corrected copy is
recorded. It is NEVER the source checksum once a corrected copy exists (and the source checksum
is usually not even a sha256 — Drive md5, SharePoint quickXorHash), because a verdict on an
AI-written change is a verdict on the WRITTEN bytes. Where the saved copy's identity was never
recorded there is nothing to bind to and the PUT refuses with 409 rather than binding a verdict
to the hash of the document those bytes replaced.

The write is a COMPARE-AND-SET inside one transaction (``Store.save_change_review``): the
identity is re-read next to the write, so a concurrent artifact change loses the race with a 409
instead of being recorded against bytes the reviewer never saw. The response then RE-READS and
RE-EVALUATES freshness rather than asserting ``stale: false``.

``stale: null`` means freshness UNKNOWN. It is not "fresh" and it is not a confirmation.

Verdict ``edited`` means CORRECTION REQUESTED. This route records the proposed replacement text;
it writes nothing into the document. It is reported that way everywhere and is never counted as
a confirmation.

Storage is the existing ``scan_decisions`` table (kind ``change_review:<changeId>``, no schema
change). Those kinds are excluded from ``Store.get_decisions`` and from
``Store.remediation_decision_digest``: a reviewer verdict on a saved edit is not remediation
intent and must not change remediation identity. Every save is also appended to the immutable
``decision_log`` (action ``change_review.<verdict>``) inside the same transaction.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone

from fastapi import APIRouter, Body, HTTPException, Request

import core
import report_facts

router = APIRouter()

KIND_PREFIX = "change_review:"
VERDICTS = ("accepted", "edited", "rejected", "unable")
NOTE_MAX = 2000
EDITED_VALUE_MAX = 4000
CHANGE_ID_MAX = 512
# changeId = `${file}::${rule_id}::${seq}` (verified) or `${file}::${rule_id}::u<16 hex>`
# (applied-but-unverified). The file part is checked against the path's filename, so the id
# cannot address a different document; the charset only excludes control characters.
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")

# Re-exported for the tests and callers that used to import it from here.
change_digest = report_facts.change_digest


def _owner(request: Request) -> str:
    """Same owner derivation as routes/scans.py — the gate-verified email, or 'demo'."""
    return getattr(request.state, "user_email", None) or "demo"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _file_record(sid: str, filename: str, owner: str) -> dict:
    if core.store.get_scan(sid, owner=owner) is None:
        raise HTTPException(404, "scan not found")
    record = core.store.get_file_records(sid, files=[filename], owner=owner).get(filename)
    if record is None:
        raise HTTPException(404, "file not found in this scan")
    return record


def _artifact(record: dict) -> dict:
    """The identity block the client echoes back as `expected_sha256`."""
    identity = report_facts.artifact_identity(record)
    current = identity["currentArtifact"]
    return {
        "sourceSha256": identity["sourceSha256"],
        "sourceChecksum": identity["sourceChecksum"],
        "sourceChecksumKind": identity["sourceChecksumKind"],
        "correctedSha256": identity["correctedSha256"],
        "currentSha256": current["sha256"],
        "currentArtifact": current,
        "identityKind": ("corrected_sha256" if identity["correctedSha256"]
                         else "source_checksum" if identity["sourceChecksum"] and
                         current["kind"] == "source" else None),
        "remediatedAt": identity["remediatedAt"],
    }


def _parse_change_id(filename: str, change_id: str) -> tuple[str, int | None]:
    if not change_id or len(change_id) > CHANGE_ID_MAX or _CONTROL.search(change_id):
        raise HTTPException(422, "invalid change id")
    if not change_id.startswith(f"{filename}::"):
        raise HTTPException(422, "change id does not belong to this file")
    parsed = report_facts.parse_change_id(filename, change_id)
    if parsed is None:
        raise HTTPException(422, "change id must be <file>::<rule_id>::<seq|u…>")
    return parsed


def _saved_changes(sid: str, filename: str, record: dict) -> list[dict]:
    changes, _complete, _total, _source = report_facts.build_saved_changes(
        core.store, sid, filename, record)
    return changes


def _digests(changes: list[dict]) -> dict[str, str]:
    return {c["id"]: c["changeDigest"] for c in changes}


@router.get("/scans/{sid}/files/{filename:path}/change-reviews")
def get_change_reviews(sid: str, filename: str, request: Request):
    owner = _owner(request)
    record = _file_record(sid, filename, owner)
    artifact = _artifact(record)
    changes = _saved_changes(sid, filename, record)
    reviews = report_facts.read_reviews(
        core.store, sid, filename, owner=owner,
        current_sha256=report_facts.decision_binding_sha256(record),
        digests_by_id=_digests(changes))
    return {"artifact": artifact, "reviews": reviews,
            "savedChanges": [{"id": c["id"], "ruleId": c["ruleId"], "sc": c["sc"],
                              "verification": c["verification"],
                              "changeDigest": c["changeDigest"]} for c in changes]}


@router.put("/scans/{sid}/files/{filename:path}/change-reviews/{change_id:path}")
def put_change_review(sid: str, filename: str, change_id: str, request: Request,
                      body: dict = Body(...)):
    owner = _owner(request)
    # Starlette matches the DECODED path, so a changeId for a nested file ("dir/a.docx::…")
    # contains '/' even when the client percent-encodes it; hence `:path` on both parameters.
    # The filename match is greedy up to the last "/change-reviews/", and the id is then
    # required to start with that filename, so a split in the wrong place is refused (422).
    record = _file_record(sid, filename, owner)   # ownership first: no validation oracle
    rule_id, seq = _parse_change_id(filename, change_id)
    artifact = _artifact(record)

    verdict = body.get("verdict")
    if verdict not in VERDICTS:
        raise HTTPException(422, f"verdict must be one of {', '.join(VERDICTS)}")
    note = body.get("note")
    if note is not None and not isinstance(note, str):
        raise HTTPException(422, "note must be text")
    note = (note or "").strip()
    if len(note) > NOTE_MAX:
        raise HTTPException(413, f"note is longer than {NOTE_MAX} characters")
    edited_value = body.get("edited_value")
    if verdict == "edited":
        if not isinstance(edited_value, str) or not edited_value.strip():
            raise HTTPException(422, "a proposed correction is required for an 'edited' decision")
        if len(edited_value) > EDITED_VALUE_MAX:
            raise HTTPException(413, f"edited value is longer than {EDITED_VALUE_MAX} characters")
    else:
        edited_value = None

    # BOTH binding tokens are mandatory. Without them a client that has been showing a stale
    # copy can have the server bind its verdict to the current bytes — which is acceptance of
    # something nobody read.
    client_digest = body.get("change_digest")
    if not (isinstance(client_digest, str) and _SHA256.match(client_digest)):
        raise HTTPException(422, "change_digest is required and must be a sha256 hex digest")
    expected = body.get("expected_sha256")
    if not isinstance(expected, str) or not expected.strip():
        raise HTTPException(
            422, "expected_sha256 is required — a decision must name the copy it was made about")

    # The ONLY thing a verdict on a saved change may bind to. Not the source checksum — see
    # report_facts.decision_binding_sha256 and the module docstring.
    current = report_facts.decision_binding_sha256(record)
    if not current:
        raise HTTPException(409, "the saved copy's identity is not recorded, so a decision "
                                 "cannot be bound to the bytes that were reviewed")
    if expected != current:
        raise HTTPException(409, "the document changed since it was loaded — reload before deciding")

    changes = _saved_changes(sid, filename, record)
    server_digest = _digests(changes).get(change_id)
    if server_digest is None:
        raise HTTPException(404, "no saved change with this id")
    if client_digest != server_digest:
        raise HTTPException(409, "the change differs from the one you reviewed — reload before deciding")

    change = next(c for c in changes if c["id"] == change_id)
    decision = {
        "change_id": change_id, "rule_id": rule_id, "seq": seq,
        "verdict": verdict, "note": note or None, "edited_value": edited_value,
        "artifact_sha256": current, "change_digest": server_digest,
        "verification": change["verification"],
        "reviewer": owner, "at": _now(),
    }
    saved = core.store.save_change_review(
        sid, filename, KIND_PREFIX + change_id, json.dumps(decision, sort_keys=True),
        owner, decision["at"], expected_artifact=current,
        log_action=f"change_review.{verdict}", log_rule_id=rule_id,
        log_detail=json.dumps({"change_id": change_id, "verdict": verdict,
                               "artifact_sha256": current, "change_digest": server_digest,
                               "verification": change["verification"],
                               "reviewer": owner, "at": decision["at"],
                               "note": note or None, "edited_value": edited_value},
                              sort_keys=True))
    if not saved:
        # Somebody rewrote the corrected copy between the check above and the write. The verdict
        # was NOT recorded; saying so is the whole point of the compare-and-set.
        raise HTTPException(409, "the document changed while the decision was being recorded — "
                                 "reload and review the current copy")

    # Re-read rather than assert. The stored row's freshness is a fact about the store as it is
    # NOW, and "stale: false" was previously a constant.
    record = _file_record(sid, filename, owner)
    artifact = _artifact(record)
    changes = _saved_changes(sid, filename, record)
    reviews = report_facts.read_reviews(
        core.store, sid, filename, owner=owner,
        current_sha256=report_facts.decision_binding_sha256(record),
        digests_by_id=_digests(changes))
    review = reviews.get(change_id) or {
        **decision, "current_change_digest": None, "stale": None,
        "staleReason": "the decision could not be read back",
        "verdictLabel": report_facts.VERDICT_LABELS.get(verdict, "unknown")}
    return {"artifact": artifact, "review": review}

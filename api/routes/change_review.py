"""Versioned reviewer decisions on individual saved changes.

A reviewer can Accept / Edit / Reject / mark "Unable to verify" one before->after record
(``remediation_diff``). A verdict is only meaningful for the exact artifact the reviewer looked
at, so each decision is BOUND to two identities when it is saved:

* ``artifact_sha256`` — the file's current artifact identity: ``file_records.corrected_sha256``
  when a corrected copy is recorded, else the source checksum. (The source checksum is whatever
  the source system provides — Drive md5 / SharePoint quickXorHash — so it is reported as
  ``sourceChecksum`` and only echoed as ``sourceSha256`` when it is actually a sha256.)
* ``change_digest`` — sha256 of ``f"{rule_id}\\n{before}\\n{after}"`` for the change itself.

When either no longer matches what the store holds now, the decision is reported ``stale`` —
the document or the change moved on after the human looked, so the verdict needs a recheck.
With no artifact identity recorded at all there is nothing to bind to: GET reports
``stale: None`` and PUT refuses with 409 rather than binding a verdict to nothing.

Storage is the existing ``scan_decisions`` table (kind ``change_review:<changeId>``, no schema
change). Those kinds are excluded from ``Store.get_decisions`` and from
``Store.remediation_decision_digest``: a reviewer verdict on a saved edit is not remediation
intent and must not change remediation identity. Every save is also appended to the immutable
``decision_log`` (action ``change_review.<verdict>``).
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone

from fastapi import APIRouter, Body, HTTPException, Request

import core

router = APIRouter()

KIND_PREFIX = "change_review:"
VERDICTS = ("accepted", "edited", "rejected", "unable")
NOTE_MAX = 2000
EDITED_VALUE_MAX = 4000
CHANGE_ID_MAX = 512
# changeId = `${file}::${rule_id}::${seq}`. The file part is checked against the path's filename,
# so the id cannot address a different document; the charset only excludes control characters.
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


def _owner(request: Request) -> str:
    """Same owner derivation as routes/scans.py — the gate-verified email, or 'demo'."""
    return getattr(request.state, "user_email", None) or "demo"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def change_digest(rule_id: str, before: str, after: str) -> str:
    return hashlib.sha256(f"{rule_id}\n{before or ''}\n{after or ''}".encode("utf-8")).hexdigest()


def _file_record(sid: str, filename: str, owner: str) -> dict:
    if core.store.get_scan(sid, owner=owner) is None:
        raise HTTPException(404, "scan not found")
    record = core.store.get_file_records(sid, files=[filename], owner=owner).get(filename)
    if record is None:
        raise HTTPException(404, "file not found in this scan")
    return record


def _artifact(record: dict) -> dict:
    corrected = record.get("corrected_sha256") or None
    source = record.get("checksum") or None
    current = corrected or source
    return {
        "sourceSha256": source if source and _SHA256.match(str(source).lower()) else None,
        "sourceChecksum": source,
        "correctedSha256": corrected,
        "currentSha256": current,
        "identityKind": ("corrected_sha256" if corrected else "source_checksum" if source else None),
        "remediatedAt": record.get("remediated_at") or None,
    }


def _parse_change_id(filename: str, change_id: str) -> tuple[str, int]:
    if not change_id or len(change_id) > CHANGE_ID_MAX or _CONTROL.search(change_id):
        raise HTTPException(422, "invalid change id")
    prefix = f"{filename}::"
    if not change_id.startswith(prefix):
        raise HTTPException(422, "change id does not belong to this file")
    rest = change_id[len(prefix):]
    rule_id, sep, seq = rest.rpartition("::")
    if not sep or not rule_id or not seq.isdigit():
        raise HTTPException(422, "change id must be <file>::<rule_id>::<seq>")
    return rule_id, int(seq)


def _current_digests(sid: str, filename: str) -> dict[str, str]:
    return {f"{filename}::{d.get('rule_id')}::{d.get('seq')}":
            change_digest(str(d.get("rule_id") or ""), d.get("before") or "", d.get("after") or "")
            for d in core.store.get_remediation_diffs(sid, filename)}


def _stale(decision: dict, artifact: dict, current_digest: str | None) -> bool | None:
    current = artifact.get("currentSha256")
    if not current:
        return None  # nothing recorded to compare against — unknown, never "fresh"
    if decision.get("artifact_sha256") != current:
        return True
    return current_digest is None or decision.get("change_digest") != current_digest


@router.get("/scans/{sid}/files/{filename:path}/change-reviews")
def get_change_reviews(sid: str, filename: str, request: Request):
    owner = _owner(request)
    record = _file_record(sid, filename, owner)
    artifact = _artifact(record)
    digests = _current_digests(sid, filename)
    reviews: dict[str, dict] = {}
    for kind, row in core.store.get_change_reviews(sid, filename, owner=owner).items():
        try:
            decision = json.loads(row.get("value") or "{}")
        except (TypeError, ValueError):
            continue
        if not isinstance(decision, dict):
            continue
        change_id = decision.get("change_id") or kind[len(KIND_PREFIX):]
        reviews[change_id] = {**decision, "change_id": change_id,
                              "current_change_digest": digests.get(change_id),
                              "stale": _stale(decision, artifact, digests.get(change_id))}
    return {"artifact": artifact, "reviews": reviews}


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
            raise HTTPException(422, "an edited value is required for an 'edited' decision")
        if len(edited_value) > EDITED_VALUE_MAX:
            raise HTTPException(413, f"edited value is longer than {EDITED_VALUE_MAX} characters")
    else:
        edited_value = None
    client_digest = body.get("change_digest")
    if client_digest is not None and not (isinstance(client_digest, str)
                                          and _SHA256.match(client_digest)):
        raise HTTPException(422, "change_digest must be a sha256 hex digest")

    current = artifact["currentSha256"]
    if not current:
        raise HTTPException(409, "artifact identity not recorded")
    expected = body.get("expected_sha256")
    if expected is not None and expected != current:
        raise HTTPException(409, "the document changed since it was loaded — reload before deciding")

    server_digest = _current_digests(sid, filename).get(change_id)
    if server_digest is None:
        raise HTTPException(404, "no saved change with this id")
    if client_digest is not None and client_digest != server_digest:
        raise HTTPException(409, "the change differs from the one you reviewed — reload before deciding")

    decision = {
        "change_id": change_id, "rule_id": rule_id, "seq": seq,
        "verdict": verdict, "note": note or None, "edited_value": edited_value,
        "artifact_sha256": current, "change_digest": server_digest,
        "reviewer": owner, "at": _now(),
    }
    core.store.save_decision(sid, filename, KIND_PREFIX + change_id,
                             json.dumps(decision, sort_keys=True), owner, decision["at"])
    core.store.log_decision(owner, f"change_review.{verdict}", scan_id=sid, file=filename,
                            rule_id=rule_id,
                            detail=json.dumps({"change_id": change_id,
                                               "artifact_sha256": current,
                                               "change_digest": server_digest,
                                               "note": note or None,
                                               "edited_value": edited_value}, sort_keys=True))
    return {"artifact": artifact, "review": {**decision, "current_change_digest": server_digest,
                                             "stale": False}}

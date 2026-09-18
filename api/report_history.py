"""Report history reads: earlier assessments and earlier decisions about the SAME document.

Three questions a report asks about a document's past, and the store could not answer cheaply or
at all (report follow-up requests R-B1, R-B2, R-B3):

  R-B1  previous_assessments_for_scan — the most recent EARLIER assessment of every document in a
        scan, in a bounded number of queries. `previous_assessment_for_file` is this function
        applied to one file, so the per-file route and the scan index cannot disagree.
  R-B2  prior_assessment_in_scan — the assessment a re-assessment INSIDE the same scan replaced.
        save_file_result deletes the file's issue_records before re-inserting them, so without a
        capture the only baseline is destroyed by the write that makes it interesting.
  R-B3  change_reviews_for_document — reviewer verdicts recorded on EARLIER scans of the same
        document. Evidence about the past only: nothing here copies a decision forward or makes
        one current.

Every function takes the Store as its first argument and uses only `store._db`; nothing here
imports store at module level, so store.py can import this module for its SCHEMA.

"SAME DOCUMENT" IS ONE RULE, used by all three: the provider's own file id when the current row
has one, else `drive_file_id IS NULL` and the same path, within the same owner and the same
source. Never the checksum — a remediated copy has a different hash and would disqualify a good
baseline, while two unrelated files could share one.
"""
from __future__ import annotations

import copy
import hashlib
import json
import uuid

# Parameters per IN (...) list. SQLite builds before 3.32 cap a statement at 999 variables; the
# widest statement below binds two lists of CHUNK plus four scalars, so 400 keeps every statement
# under that cap while a 400-document scan still costs one query per step.
CHUNK = 400

# R-B3's page cap. A caller may ask for less, never for more.
MAX_DECISIONS = 1000

CHANGE_REVIEW_KIND_PREFIX = "change_review:"

# The columns of file_records that describe the ASSESSMENT (what save_file_result's upsert
# writes). Remediation state (remediated_at, blob_url, corrected_sha256, ...) is deliberately not
# here: it is written later, in place, and is not part of what an assessment found.
ASSESSMENT_FIELDS = ("file", "engine", "status", "score", "compliant", "skipped_rules",
                     "drive_file_id", "acp_stamped", "checksum", "size_kb", "pages", "sheets",
                     "source_modified")
_INT_FIELDS = frozenset({"score", "compliant", "skipped_rules", "size_kb", "pages", "sheets",
                         "page"})
ISSUE_COLUMNS = ("rule_id", "wcag", "severity", "detail", "page", "location")

# R-B2 storage. Additive, nullable where it can be, created through store._SCHEMA (the existing
# migration path — see the G -> D request). One row per assessment WRITE of a (scan, file):
#   state='current'    the assessment now in file_records/issue_records. Holds only what was
#                      true AT WRITE TIME: the assessment fields, digests of its content, and the
#                      scan's rubric/scope/source as they stood when it was written.
#   state='superseded' an assessment a later write replaced. Its issue rows and per-rule manifest
#                      are copied here BEFORE save_file_result deletes them, in the same
#                      transaction, and its envelope is the one recorded when IT was written —
#                      never rebuilt from the scan row as it is at replacement time.
SCHEMA = [
    """CREATE TABLE IF NOT EXISTS file_assessment_history (
      history_id TEXT PRIMARY KEY,
      scan_id TEXT NOT NULL,
      file TEXT NOT NULL,
      owner_email TEXT,
      seq INT NOT NULL,
      state TEXT NOT NULL,
      context_source TEXT NOT NULL,
      written_at TEXT,
      recorded_at TEXT NOT NULL,
      written_job TEXT,
      written_attempt INT,
      drive_file_id TEXT,
      checksum TEXT,
      file_fields TEXT NOT NULL,
      context TEXT,
      file_digest TEXT,
      issues_digest TEXT,
      superseded_at TEXT,
      superseded_recorded_at TEXT,
      superseded_by_job TEXT,
      superseded_by_attempt INT,
      row_at_replacement TEXT,
      issue_count INT,
      issues TEXT,
      rule_manifest TEXT,
      integrity TEXT
    )""",
    "CREATE INDEX IF NOT EXISTS idx_file_assessment_history_file "
    "ON file_assessment_history(scan_id,file,state)",
    "CREATE INDEX IF NOT EXISTS idx_file_assessment_history_drive "
    "ON file_assessment_history(scan_id,drive_file_id,state)",
    # At most one current assessment per (scan, file) — the invariant the supersede relies on.
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_file_assessment_history_current "
    "ON file_assessment_history(scan_id,file) WHERE state='current'",
]

# Assessment outcomes a file row can record, and what each lets a reader claim about its issues.
_FAILED = frozenset({"error"})
_SKIPPED = frozenset({"skipped"})


def ensure_schema(store) -> None:
    """Create the R-B2 table on a store whose _SCHEMA does not carry it yet (tests before the
    store hook lands). Idempotent; once store._SCHEMA includes SCHEMA this is a no-op."""
    with store._db.cursor() as cur:
        for stmt in SCHEMA:
            store._db.execute(cur, stmt)


# ── shared helpers ──────────────────────────────────────────────────────────────────────────

def _chunks(values: list, size: int | None = None):
    size = size or CHUNK
    for start in range(0, len(values), size):
        yield values[start:start + size]


def _placeholders(n: int) -> str:
    return ",".join(["%s"] * n)


def _when(run: dict) -> str | None:
    # Python `or`, not SQL COALESCE, exactly as previous_assessment_for_file has always computed
    # the current scan's instant: an empty string falls through to the next column.
    return run.get("assessed_at") or run.get("completed_at") or run.get("started_at")


def _sort_value(value):
    if value is None:
        return (1,)
    if isinstance(value, bool):
        return (0, 0, int(value))
    if isinstance(value, (int, float)):
        return (0, 0, value)
    return (0, 1, str(value))


def issue_sort_key(issue: dict) -> tuple:
    """Deterministic and backend-independent (NULLs last on both SQLite and Postgres)."""
    return tuple(_sort_value(issue.get(col)) for col in ISSUE_COLUMNS)


def _canon(field: str, value):
    """One representation for a value whichever side of the database it was read from."""
    if isinstance(value, bool):
        return int(value)
    if field in _INT_FIELDS and value is not None:
        if isinstance(value, float) and value.is_integer():
            return int(value)
        if isinstance(value, str):
            try:
                return int(value.strip())
            except ValueError:
                return value
    return value


def _digest(value) -> str:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _file_digest(fields: dict) -> str:
    return _digest({k: _canon(k, fields.get(k)) for k in ASSESSMENT_FIELDS})


def _issues_digest(issues: list[dict]) -> str:
    rows = [[_canon(c, i.get(c)) for c in ISSUE_COLUMNS] for i in issues]
    rows.sort(key=lambda r: json.dumps(r, default=str))
    return _digest(rows)


def _loads(raw, default=None):
    if raw is None:
        return default
    if not isinstance(raw, str):
        return raw
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return default


def _current_row(store, cur, scan_id: str, file: str) -> dict | None:
    """The scan row plus this file's provider id — the query previous_assessment_for_file uses."""
    store._db.execute(cur,
        "SELECT r.*, f.drive_file_id FROM scan_runs r "
        "LEFT JOIN file_records f ON f.scan_id=r.id AND f.file=%s WHERE r.id=%s",
        (file, scan_id))
    return store._db.fetchone(cur)


# ── R-B1: the latest EARLIER assessment of every document in a scan ─────────────────────────

# Shared by the window below and, through previous_assessment_for_file's delegation, by every
# per-file read. The two trailing keys make equal timestamps deterministic: the per-file query
# used to end at `... DESC LIMIT 1`, which leaves the winner of a tie to the query plan.
_EARLIER_ORDER = "COALESCE(r.assessed_at,r.completed_at,r.started_at) DESC, r.id DESC, f.file DESC"
_EARLIER_WHERE = ("r.owner_email=%s AND r.id<>%s AND r.source=%s "
                  "AND COALESCE(r.assessed_at,r.completed_at,r.started_at) < %s ")


def previous_assessments_for_scan(store, scan_id: str, *, owner: str,
                                  files: list[str] | None = None) -> dict[str, dict]:
    """{file: previous} for every file of `scan_id` (or only `files`) that HAS an earlier
    assessment; files without one are absent. A foreign owner or unknown scan returns {}.

    Each value is exactly previous_assessment_for_file's: {"run": <scan_runs row, aggregates
    filled, scope decoded, scan_scope>, "file_row": <run_id + file_records row>, "issues":
    [rule_id, wcag, severity, detail, page, location]} with issues in issue_sort_key order.

    Query count does not depend on the number of files beyond one statement per CHUNK: one for the
    scan row, one per chunk of file identities, one windowed query per chunk of provider ids and of
    paths, one per chunk of baseline runs plus the aggregate fill for each DISTINCT baseline run
    (store._fill_run_aggregate, 1-3 statements, reused rather than re-derived so the run dict is
    the same one get_scan would build), and one per chunk of issue lookups.
    """
    db = store._db
    with db.cursor() as cur:
        db.execute(cur, "SELECT * FROM scan_runs WHERE id=%s", (scan_id,))
        current = db.fetchone(cur)
        if not current or current.get("owner_email") != owner:
            return {}
        when = _when(current)
        source = current.get("source")

        # 1. Identity of every requested document, as the current scan recorded it.
        identity: dict[str, str | None] = {}
        if files is None:
            db.execute(cur, "SELECT file,drive_file_id FROM file_records WHERE scan_id=%s",
                       (scan_id,))
            for row in db.fetchall(cur):
                identity[row["file"]] = row.get("drive_file_id")
        else:
            wanted = list(dict.fromkeys(str(f) for f in files))
            for part in _chunks(wanted):
                db.execute(cur,
                    "SELECT file,drive_file_id FROM file_records WHERE scan_id=%s "
                    f"AND file IN ({_placeholders(len(part))})", (scan_id, *part))
                for row in db.fetchall(cur):
                    identity[row["file"]] = row.get("drive_file_id")
            for name in wanted:           # not in this scan: path identity, as the LEFT JOIN gives
                identity.setdefault(name, None)
        if not identity:
            return {}

        by_drive: dict[str, list[str]] = {}
        by_path: list[str] = []
        for name, drive_file_id in identity.items():
            if drive_file_id:
                by_drive.setdefault(drive_file_id, []).append(name)
            else:
                by_path.append(name)

        # 2. The latest earlier row per identity: one windowed statement per chunk.
        base = ("SELECT r.id AS run_id, f.*, ROW_NUMBER() OVER (PARTITION BY {part} "
                f"ORDER BY {_EARLIER_ORDER}) AS acp_rank "
                "FROM file_records f JOIN scan_runs r ON r.id=f.scan_id "
                f"WHERE {_EARLIER_WHERE}")
        scalars = (owner, scan_id, source, when or "")
        chosen: dict[str, dict] = {}
        drive_ids = sorted(by_drive)
        for part in _chunks(drive_ids):
            sql = ("SELECT * FROM (" + base.format(part="f.drive_file_id")
                   + f"AND f.drive_file_id IN ({_placeholders(len(part))})) ranked "
                   "WHERE acp_rank=1")
            db.execute(cur, sql, (*scalars, *part))
            for row in db.fetchall(cur):
                row.pop("acp_rank", None)
                for name in by_drive.get(row.get("drive_file_id"), []):
                    chosen[name] = row
        for part in _chunks(sorted(by_path)):
            sql = ("SELECT * FROM (" + base.format(part="f.file")
                   + f"AND f.drive_file_id IS NULL AND f.file IN ({_placeholders(len(part))})) "
                   "ranked WHERE acp_rank=1")
            db.execute(cur, sql, (*scalars, *part))
            for row in db.fetchall(cur):
                row.pop("acp_rank", None)
                chosen[row["file"]] = row
        if not chosen:
            return {}

        # 3. Each distinct baseline run, built exactly as the per-file read builds it.
        runs: dict[str, dict] = {}
        run_ids = sorted({row["scan_id"] for row in chosen.values()})
        for part in _chunks(run_ids):
            db.execute(cur, f"SELECT * FROM scan_runs WHERE id IN ({_placeholders(len(part))})",
                       tuple(part))
            for run in db.fetchall(cur):
                runs[run["id"]] = run
        for run_id in run_ids:
            run = store._fill_run_aggregate(cur, runs[run_id])
            raw = run.get("scope")
            scope = json.loads(raw) if isinstance(raw, str) and raw else (raw or None)
            run["scan_scope"] = scope.get("scan_scope") if isinstance(scope, dict) else None
            runs[run_id] = run

        # 4. Issues of every chosen (run, file) pair. Rows are fetched by the cross product of the
        #    chunk's runs and files and then filtered to the exact pairs, so one statement serves
        #    a chunk whatever mix of baseline runs it holds.
        pairs = sorted({(row["scan_id"], row["file"]) for row in chosen.values()})
        issues: dict[tuple, list[dict]] = {pair: [] for pair in pairs}
        for part in _chunks(pairs):
            part_runs = sorted({p[0] for p in part})
            part_files = sorted({p[1] for p in part})
            db.execute(cur,
                "SELECT scan_id,file,rule_id,wcag,severity,detail,page,location FROM issue_records "
                f"WHERE scan_id IN ({_placeholders(len(part_runs))}) "
                f"AND file IN ({_placeholders(len(part_files))})",
                (*part_runs, *part_files))
            for row in db.fetchall(cur):
                key = (row.pop("scan_id"), row.pop("file"))
                if key in issues:
                    issues[key].append(row)
    for rows in issues.values():
        rows.sort(key=issue_sort_key)

    out: dict[str, dict] = {}
    for name, row in chosen.items():
        out[name] = {"run": copy.deepcopy(runs[row["scan_id"]]),
                     "file_row": dict(row),
                     "issues": [dict(i) for i in issues[(row["scan_id"], row["file"])]]}
    return out


def previous_assessment_for_file(store, scan_id: str, file: str, *, owner: str) -> dict | None:
    """The one-file case of previous_assessments_for_scan — the body Store's method delegates to,
    so the per-file route and the scan index share one selection, tie-break and issue order."""
    return previous_assessments_for_scan(store, scan_id, owner=owner, files=[file]).get(file)


# ── R-B2: the assessment a re-assessment inside the same scan replaced ─────────────────────

def capture_outgoing(store, cur, scan_id: str, file: str) -> dict | None:
    """Hook 1 — call in save_file_result BEFORE the file_records upsert, on its cursor.

    Reads the row the upsert is about to overwrite: afterwards its assessment fields are gone.
    On Postgres the read locks the row (the lock the upsert would take a moment later), so a
    concurrent writer of the same file cannot replace it between this read and the capture.
    """
    suffix = " FOR UPDATE" if getattr(store._db, "supports_for_update", False) else ""
    store._db.execute(cur, "SELECT * FROM file_records WHERE scan_id=%s AND file=%s" + suffix,
                      (scan_id, file))
    return store._db.fetchone(cur)


def _incoming_fields(f: dict) -> dict:
    """The assessment fields exactly as save_file_result's upsert writes them."""
    return {"file": f["file"], "engine": f.get("engine"), "status": f.get("status"),
            "score": f.get("score"),
            "compliant": int(f["compliant"]) if f.get("compliant") is not None else None,
            "skipped_rules": f.get("skipped_rules"), "drive_file_id": f.get("drive_file_id"),
            "acp_stamped": f.get("acp_stamped"), "checksum": f.get("checksum"),
            "size_kb": f.get("size_kb"), "pages": f.get("pages"), "sheets": f.get("sheets"),
            "source_modified": f.get("source_modified")}


def _incoming_issues(f: dict) -> list[dict]:
    import store as _store_mod   # the same location accessor both INSERT sites use
    return [{"rule_id": i.get("ruleId"), "wcag": i.get("wcag"), "severity": i.get("severity"),
             "detail": i.get("detail"), "page": i.get("page"),
             "location": _store_mod._issue_location(i)} for i in (f.get("issues") or [])]


def _scope_json(scope) -> dict | None:
    if scope is None:
        return None
    if isinstance(scope, dict):
        return {str(k): (sorted(str(x) for x in v) if isinstance(v, (set, frozenset, list, tuple))
                         else v) for k, v in scope.items()}
    return {"value": str(scope)}


def _write_context(store, cur, scan_id: str, file_scope) -> dict:
    """The scan as it stands AT THIS WRITE — recorded now, so a later reader never has to guess
    it from a scan row that may since have been re-initialised with another rubric or scope."""
    store._db.execute(cur,
        "SELECT source,owner_email,rubric_name,rubric_hash,scope,status FROM scan_runs WHERE id=%s",
        (scan_id,))
    run = store._db.fetchone(cur) or {}
    scope = _loads(run.get("scope"))
    return {"source": run.get("source"), "owner_email": run.get("owner_email"),
            "rubric_name": run.get("rubric_name"), "rubric_hash": run.get("rubric_hash"),
            "scope": scope if isinstance(scope, dict) else None,
            "scope_recorded": run.get("scope") is not None,
            "scan_status": run.get("status"), "file_scope": _scope_json(file_scope)}


def record_assessment_write(store, cur, scan_id: str, f: dict, completed_at: str,
                            outgoing: dict | None, *, file_scope=None,
                            job: dict | None = None) -> dict:
    """Hook 2 — call in save_file_result AFTER the upsert is known to have applied (the rowcount
    refusal returned already) and BEFORE `DELETE FROM issue_records`, on the same cursor.

    Supersedes the outgoing assessment — copying its issue rows and per-rule manifest while they
    still exist — and records the incoming one as current, all inside the caller's transaction.
    `outgoing` is capture_outgoing's result. Returns {"superseded": history_id|None,
    "current": history_id, "unchanged": bool}.

    A write identical to the current assessment (same fields, same findings, same rubric/scope —
    a retried job) records nothing: otherwise the retry would become the "prior" and hide the
    assessment it actually followed.
    """
    db = store._db
    file = f["file"]
    now = store._now()
    job_id = (job or {}).get("id")
    attempt = (job or {}).get("attempts")
    incoming_fields = _incoming_fields(f)
    incoming_issues = _incoming_issues(f)
    context = _write_context(store, cur, scan_id, file_scope)
    file_digest = _file_digest(incoming_fields)
    issues_digest = _issues_digest(incoming_issues)

    db.execute(cur,
        "SELECT * FROM file_assessment_history WHERE scan_id=%s AND file=%s AND state='current'",
        (scan_id, file))
    ledger = db.fetchone(cur)
    if ledger and ledger.get("file_digest") == file_digest \
            and ledger.get("issues_digest") == issues_digest \
            and _loads(ledger.get("context")) == _loads(json.dumps(context, default=str)):
        return {"superseded": None, "current": ledger["history_id"], "unchanged": True}

    superseded_id = None
    if ledger or outgoing:
        db.execute(cur,
            "SELECT rule_id,wcag,severity,detail,page,location FROM issue_records "
            "WHERE scan_id=%s AND file=%s", (scan_id, file))
        old_issues = sorted(db.fetchall(cur), key=issue_sort_key)
        db.execute(cur,
            "SELECT rule_id,status,finding_count FROM scan_file_manifests "
            "WHERE scan_id=%s AND file=%s ORDER BY rule_id", (scan_id, file))
        manifest = db.fetchall(cur)
        old_issues_digest = _issues_digest(old_issues)
        row_at_replacement = (json.dumps(outgoing, sort_keys=True, default=str)
                              if outgoing else None)
        if ledger:
            # Integrity of the copy: issue_records is written only by save_scan/save_file_result,
            # so the rows about to be deleted must be the ones this assessment recorded.
            integrity = ("verified" if old_issues_digest == ledger.get("issues_digest")
                         else "issues_changed_after_write")
            if outgoing is not None and _file_digest(outgoing) != ledger.get("file_digest"):
                # Remediation updates status/score/compliant in place; the envelope keeps the
                # fields AS ASSESSED and row_at_replacement shows what readers last saw.
                integrity += ";file_row_changed_after_write"
            db.execute(cur,
                "UPDATE file_assessment_history SET state='superseded', superseded_at=%s, "
                "superseded_recorded_at=%s, superseded_by_job=%s, superseded_by_attempt=%s, "
                "row_at_replacement=%s, issue_count=%s, issues=%s, rule_manifest=%s, "
                "integrity=%s WHERE history_id=%s AND state='current'",
                (completed_at, now, job_id, attempt, row_at_replacement, len(old_issues),
                 json.dumps(old_issues, default=str), json.dumps(manifest, default=str),
                 integrity, ledger["history_id"]))
            superseded_id = ledger["history_id"]
        elif _file_digest(outgoing) == file_digest and old_issues_digest == issues_digest:
            # An unrecorded assessment rewritten identically: nothing was replaced. Only start
            # recording (below), so the NEXT replacement has a context to preserve.
            pass
        else:
            # Written before this history existed (or by save_scan): the rows are the real
            # outgoing assessment, but when and under which rubric/scope it was made was never
            # recorded, and the scan row now may describe a later write. Say so; do not guess.
            fields = {k: outgoing.get(k) for k in ASSESSMENT_FIELDS}
            superseded_id = uuid.uuid4().hex
            db.execute(cur,
                "INSERT INTO file_assessment_history(history_id,scan_id,file,owner_email,seq,state,"
                "context_source,written_at,recorded_at,written_job,written_attempt,drive_file_id,"
                "checksum,file_fields,context,file_digest,issues_digest,superseded_at,"
                "superseded_recorded_at,superseded_by_job,superseded_by_attempt,"
                "row_at_replacement,issue_count,issues,rule_manifest,integrity) "
                "VALUES(%s,%s,%s,%s,%s,'superseded','not_recorded',NULL,%s,%s,%s,%s,%s,%s,NULL,"
                "%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'row_at_replacement')",
                (superseded_id, scan_id, file, context.get("owner_email"),
                 _next_seq(store, cur, scan_id, file), now, outgoing.get("written_job"),
                 outgoing.get("written_attempt"), outgoing.get("drive_file_id"),
                 outgoing.get("checksum"), json.dumps(fields, default=str),
                 _file_digest(fields), old_issues_digest, completed_at, now, job_id, attempt,
                 row_at_replacement, len(old_issues), json.dumps(old_issues, default=str),
                 json.dumps(manifest, default=str)))

    current_id = uuid.uuid4().hex
    db.execute(cur,
        "INSERT INTO file_assessment_history(history_id,scan_id,file,owner_email,seq,state,"
        "context_source,written_at,recorded_at,written_job,written_attempt,drive_file_id,checksum,"
        "file_fields,context,file_digest,issues_digest) "
        "VALUES(%s,%s,%s,%s,%s,'current','recorded_at_write',%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
        (current_id, scan_id, file, context.get("owner_email"),
         _next_seq(store, cur, scan_id, file), completed_at, now, job_id, attempt,
         incoming_fields.get("drive_file_id"), incoming_fields.get("checksum"),
         json.dumps(incoming_fields, default=str), json.dumps(context, default=str),
         file_digest, issues_digest))
    return {"superseded": superseded_id, "current": current_id, "unchanged": False}


def _next_seq(store, cur, scan_id: str, file: str) -> int:
    store._db.execute(cur,
        "SELECT COALESCE(MAX(seq),0) AS seq FROM file_assessment_history "
        "WHERE scan_id=%s AND file=%s", (scan_id, file))
    return int((store._db.fetchone(cur) or {}).get("seq") or 0) + 1


def _outcome(status) -> str:
    value = (status or "").lower() if isinstance(status, str) else ""
    if not value:
        return "unknown"
    if value in _FAILED:
        return "failed"
    if value in _SKIPPED:
        return "skipped"
    return "assessed"


def _basis(prior_ctx: dict | None, current_ctx: dict | None) -> dict:
    """Is the replaced assessment comparable with the one that replaced it? Only from two
    RECORDED contexts; anything missing is 'not_recorded', never 'same' and never 'different'."""
    def compare(key):
        if not prior_ctx or not current_ctx:
            return "not_recorded"
        if key == "scope" and not (prior_ctx.get("scope_recorded")
                                   and current_ctx.get("scope_recorded")):
            return "not_recorded"
        a, b = prior_ctx.get(key), current_ctx.get(key)
        if key == "rubric_hash" and (not a or not b):
            return "not_recorded"
        return "same" if a == b else "different"
    return {"rubric": compare("rubric_hash"), "scope": compare("scope"),
            "file_scope": compare("file_scope")}


def prior_assessment_in_scan(store, scan_id: str, file: str, *, owner: str) -> dict | None:
    """The most recent assessment of THIS document that a later write in the SAME scan replaced,
    or None — which means none was CAPTURED, not that none existed (replacements made before this
    history was deployed left nothing to read). Unknown or foreign scan: None.

    Shape (previous_assessment_for_file's keys, plus the envelope):
      run       — the scan AS RECORDED WHEN THE REPLACED ASSESSMENT WAS WRITTEN: id, owner_email,
                  source, rubric_name, rubric_hash, scope, scan_scope, status, assessed_at (the
                  write's own completed_at), same_scan_snapshot=True. Every value but id is None
                  when context_source is 'not_recorded'. Never read from the current scan row.
      file_row  — the assessment fields as written (plus scan_id); for a 'not_recorded' snapshot,
                  the row as it stood at replacement.
      issues    — the replaced finding rows, issue_sort_key order. [] with issues_state
                  'recorded' is a real zero-finding baseline.
      snapshot  — see api/report_history.py and /tmp/acp-report-followup-owner-response.md.
    """
    db = store._db
    with db.cursor() as cur:
        current = _current_row(store, cur, scan_id, file)
        if not current or current.get("owner_email") != owner:
            return None
        drive_file_id = current.get("drive_file_id")
        sql = ("SELECT * FROM file_assessment_history WHERE scan_id=%s AND owner_email=%s "
               "AND state='superseded' ")
        params = [scan_id, owner]
        if drive_file_id:
            sql += "AND drive_file_id=%s "
            params.append(drive_file_id)
        else:
            sql += "AND drive_file_id IS NULL AND file=%s "
            params.append(file)
        db.execute(cur, sql.replace("SELECT *", "SELECT COUNT(*) AS n", 1), tuple(params))
        total = int((db.fetchone(cur) or {}).get("n") or 0)
        if not total:
            return None
        db.execute(cur, sql + "ORDER BY superseded_at DESC, superseded_recorded_at DESC, "
                   "seq DESC, history_id DESC LIMIT 1", tuple(params))
        row = db.fetchone(cur)
        db.execute(cur,
            "SELECT context FROM file_assessment_history WHERE scan_id=%s AND file=%s "
            "AND state='current'", (scan_id, file))
        replacing = db.fetchone(cur)

    recorded = row.get("context_source") == "recorded_at_write"
    ctx = _loads(row.get("context")) if recorded else None
    ctx = ctx if isinstance(ctx, dict) else None
    scope = (ctx or {}).get("scope")
    run = {"id": scan_id, "same_scan_snapshot": True,
           "owner_email": (ctx or {}).get("owner_email"), "source": (ctx or {}).get("source"),
           "rubric_name": (ctx or {}).get("rubric_name"),
           "rubric_hash": (ctx or {}).get("rubric_hash"),
           "scope": scope, "scan_scope": scope.get("scan_scope") if isinstance(scope, dict) else None,
           "status": (ctx or {}).get("scan_status"),
           "assessed_at": row.get("written_at") if recorded else None}
    fields = _loads(row.get("file_fields"), {}) or {}
    file_row = {"scan_id": scan_id, "run_id": scan_id, **fields}
    issues = _loads(row.get("issues"), None)
    manifest = _loads(row.get("rule_manifest"), None)
    outcome = _outcome(fields.get("status"))
    issues_state = "recorded" if (outcome == "assessed" and isinstance(issues, list)) \
        else "unavailable"
    skipped = fields.get("skipped_rules")
    not_checked = [m.get("rule_id") for m in (manifest or [])
                   if m.get("status") in ("NOT_CHECKED", "ERROR")]
    if outcome != "assessed":
        completeness = "not_assessed"
    elif (isinstance(skipped, int) and skipped > 0) or not_checked:
        completeness = "partial"
    elif skipped is None and manifest is None:
        completeness = "unknown"
    else:
        completeness = "complete"
    replacing_ctx = _loads((replacing or {}).get("context")) if replacing else None
    snapshot = {
        "history_id": row["history_id"], "seq": row.get("seq"),
        "history_total": total,
        "context_source": row.get("context_source"),
        "assessment_outcome": outcome,
        "issues_state": issues_state,
        "issue_count": row.get("issue_count") if issues_state == "recorded" else None,
        "zero_findings": issues_state == "recorded" and row.get("issue_count") == 0,
        "completeness": completeness,
        "rules_not_checked": not_checked,
        "rule_manifest": manifest,
        "written_at": row.get("written_at"),
        "superseded_at": row.get("superseded_at"),
        "superseded_by": {"job_id": row.get("superseded_by_job"),
                          "attempt": row.get("superseded_by_attempt")},
        "artifact": {"file": fields.get("file"), "drive_file_id": row.get("drive_file_id"),
                     "checksum": row.get("checksum")},
        "integrity": row.get("integrity"),
        "row_at_replacement": _loads(row.get("row_at_replacement")),
        "comparison_basis": _basis(ctx, replacing_ctx if isinstance(replacing_ctx, dict)
                                   else None),
    }
    return {"run": run, "file_row": file_row,
            "issues": list(issues) if issues_state == "recorded" else list(issues or []),
            "snapshot": snapshot}


# ── R-B3: reviewer decisions on EARLIER scans of the same document ─────────────────────────

def change_reviews_for_document(store, scan_id: str, file: str, *, owner: str,
                                limit: int = 200) -> dict | None:
    """Change-review verdicts recorded on strictly EARLIER scans of this document.

    Same owner, same source and the same identity rule as previous_assessment_for_file; a scan is
    earlier only when its COALESCE(assessed_at, completed_at, started_at) is strictly less than
    this one's, so a scan with an equal instant is not "earlier". Newest first. Read-only: this
    never writes, never copies a verdict to `scan_id` and never makes one current.

    Returns None for an unknown or foreign scan, else
      {"decisions": [{scan_id, file, kind, change_id, value, ts, scan_at}],
       "total": int, "returned": int, "limit": int, "truncated": bool}
    `value` is the stored JSON text, verbatim. `total` counts every matching verdict, so a
    truncated page still says how many exist.
    """
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        raise ValueError("limit must be a positive integer")
    limit = min(limit, MAX_DECISIONS)
    db = store._db
    with db.cursor() as cur:
        current = _current_row(store, cur, scan_id, file)
        if not current or current.get("owner_email") != owner:
            return None
        when = _when(current)
        drive_file_id = current.get("drive_file_id")
        where = ("FROM scan_decisions d JOIN file_records f ON f.scan_id=d.scan_id AND f.file=d.file "
                 "JOIN scan_runs r ON r.id=f.scan_id "
                 f"WHERE {_EARLIER_WHERE}AND d.kind LIKE %s AND d.owner_email=%s ")
        params = [owner, scan_id, current.get("source"), when or "",
                  CHANGE_REVIEW_KIND_PREFIX + "%", owner]
        if drive_file_id:
            where += "AND f.drive_file_id=%s "
            params.append(drive_file_id)
        else:
            where += "AND f.drive_file_id IS NULL AND f.file=%s "
            params.append(file)
        db.execute(cur, "SELECT COUNT(*) AS n " + where, tuple(params))
        total = int((db.fetchone(cur) or {}).get("n") or 0)
        rows = []
        if total:
            db.execute(cur,
                "SELECT d.scan_id,d.file,d.kind,d.value,d.updated_at AS ts,"
                "COALESCE(r.assessed_at,r.completed_at,r.started_at) AS scan_at " + where
                + "ORDER BY d.updated_at DESC, scan_at DESC, d.scan_id DESC, d.kind DESC LIMIT %s",
                (*params, limit))
            rows = db.fetchall(cur)
    decisions = [{"scan_id": r["scan_id"], "file": r["file"], "kind": r["kind"],
                  "change_id": str(r["kind"])[len(CHANGE_REVIEW_KIND_PREFIX):],
                  "value": r.get("value"), "ts": r.get("ts"), "scan_at": r.get("scan_at")}
                 for r in rows]
    return {"decisions": decisions, "total": total, "returned": len(decisions),
            "limit": limit, "truncated": total > len(decisions)}


# ── R-B4: scan-wide readers for the per-file report facts inputs ───────────────────────────

def _scan_owned(store, cur, scan_id: str, owner: str) -> bool:
    store._db.execute(cur, "SELECT owner_email FROM scan_runs WHERE id=%s", (scan_id,))
    row = store._db.fetchone(cur)
    return bool(row) and row.get("owner_email") == owner


def remediation_diffs_for_scan(store, scan_id: str, *, owner: str | None = None,
                               files: list[str] | None = None) -> dict[str, list[dict]]:
    """{file: store.get_remediation_diffs(scan_id, file)} for every file with a verified diff
    (or only `files`); files without one are absent. NOT the 2,000-capped list_remediation_diffs:
    every row, the same columns (rule_id, seq, before, after, note, locator, page, verified,
    location_source), the same per-file order (rule_id, seq), and the SAME location projection —
    store._with_diff_location, the reader helper get_remediation_diffs itself uses, not a copy.

    get_remediation_diffs has no owner check (its routes check); pass `owner` and an unknown or
    foreign scan reads {}. Statements: 1 (+1 with owner), or one per CHUNK of `files`.
    """
    db = store._db
    sql = ("SELECT file,rule_id,seq,before,after,note,locator,page,TRUE AS verified "
           "FROM remediation_diff WHERE scan_id=%s ")
    order = "ORDER BY file, rule_id, seq"
    out: dict[str, list[dict]] = {}
    with db.cursor() as cur:
        if owner is not None and not _scan_owned(store, cur, scan_id, owner):
            return {}
        if files is None:
            batches = [(sql + order, (scan_id,))]
        else:
            wanted = list(dict.fromkeys(str(f) for f in files))
            batches = [(sql + f"AND file IN ({_placeholders(len(part))}) " + order,
                        (scan_id, *part)) for part in _chunks(wanted)]
        for statement, params in batches:
            db.execute(cur, statement, params)
            for row in db.fetchall(cur):
                name = row.pop("file")
                out.setdefault(name, []).append(
                    store._with_diff_location({**row, "verified": bool(row["verified"])}))
    return out


def change_reviews_for_scan(store, scan_id: str, owner: str, *,
                            files: list[str] | None = None) -> dict[str, dict]:
    """{file: store.get_change_reviews(scan_id, file, owner=owner)} for every file with a verdict
    (or only `files`); files without one are absent (the per-file read returns {} for them).
    The same filter as the per-file read — this scan, the store's change-review kind prefix, the
    decision's owner_email — and the same shape: {kind: {"value", "updated_at"}}.

    `owner` is REQUIRED (ValueError otherwise): the per-file read silently drops its owner filter
    for a falsy owner, and a scan-wide read must never do that. Statements: 1, or one per CHUNK
    of `files`.
    """
    if not isinstance(owner, str) or not owner:
        raise ValueError("owner is required")
    prefix = getattr(store, "CHANGE_REVIEW_KIND_PREFIX", CHANGE_REVIEW_KIND_PREFIX)
    db = store._db
    sql = ("SELECT file,kind,value,updated_at FROM scan_decisions "
           "WHERE scan_id=%s AND kind LIKE %s AND owner_email=%s ")
    base = (scan_id, prefix + "%", owner)
    out: dict[str, dict] = {}
    with db.cursor() as cur:
        if files is None:
            batches = [(sql, base)]
        else:
            wanted = list(dict.fromkeys(str(f) for f in files))
            batches = [(sql + f"AND file IN ({_placeholders(len(part))})", (*base, *part))
                       for part in _chunks(wanted)]
        for statement, params in batches:
            db.execute(cur, statement, params)
            for row in db.fetchall(cur):
                out.setdefault(row["file"], {})[row["kind"]] = {
                    "value": row["value"], "updated_at": row["updated_at"]}
    return out

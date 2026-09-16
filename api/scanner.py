"""Reusable scan core: source -> engines -> rubric -> report dict.

Emits progress via a callback (phase / files_found / files_done / current) so the
control plane can stream live activity. Ephemeral working copies are deleted when the
scan finishes (the "documents never retained" guarantee).
"""
from __future__ import annotations
import concurrent.futures as _cf
import io, json, os, re, shutil, signal, subprocess, sys, tempfile, threading, time, uuid, zipfile
from datetime import datetime, timezone
from pathlib import Path
import lf as _lf_mod
import provenance
import estate_inventory
import sp_metadata
# Module level is safe in this direction and only this direction: worker imports nothing from
# this package but joblog, so scanner -> worker is not a cycle (handlers -> worker and
# handlers -> scanner are the edges that exist). _cancel_checkpoint() still imports check_cancel
# lazily, because that one is called from inside the listing loops and reads better next to its
# own explanation; this import is here because an `except` clause needs the NAME at runtime.
from worker import JobCancelledError

# Per-file analysis is CPU/IO bound and independent; run it across a small thread
# pool. pikepdf/lxml release the GIL and each analyser is built fresh per call.
_SCAN_WORKERS = min(8, (os.cpu_count() or 2) * 2)
# Concurrent folder-listing workers for BFS discovery. Drive's user quota is ~10 req/s;
# at ~300ms/call, 6 workers saturate it without reliably tripping secondary rate limits.
# Raise ACP_DISCOVERY_WORKERS if your estate has very deep folder hierarchies.
_DISCOVERY_WORKERS = int(os.environ.get("ACP_DISCOVERY_WORKERS", "6"))
# Per-request socket timeout for the Drive HTTP client. `build("drive", "v3", credentials=…)`
# has NO timeout by default — httplib2's underlying socket blocks with whatever the platform
# default is (effectively forever) until data arrives or the connection is torn down at a lower
# layer. `.execute(num_retries=5)` retries on an HTTP error or an httplib2/socket exception, but a
# TRULY STALLED connection (a network blip that neither returns data nor errors) raises neither,
# so it never gets the chance to retry — the call just never returns. Found live 2026-08-29: a
# discovery job sat "Build document inventory" for 250+ seconds with the queue worker reporting
# online and the job itself 'running' — exactly the failure mode worker.py's own
# max_unverified_lease_s() docstring warns about ("blocked on a socket with no timeout... the
# queue showing 'N active · 0 waiting' and draining nothing"). Bounded here so a stalled call
# becomes a caught exception num_retries can act on, instead of an unbounded hang.
_DRIVE_HTTP_TIMEOUT_S = int(os.environ.get("ACP_DRIVE_HTTP_TIMEOUT_S", "60"))

ACP = Path(__file__).resolve().parent.parent
# Engine + corpus locations default to the local dev layout but are env-overridable
# so the same code runs inside the deploy container (paths set in the Dockerfile).
# The PDF analyser is VENDORED into this repo (ADR 0029), the way ADR 0012 vendored the Office
# analysers. The previous default pointed at a checkout outside the tree
# (~/projects/_review-digital-accessibility/worker-python) which existed on exactly one laptop —
# so every other host, CI included, fell back to a path that was not there and skipped or errored
# every PDF. Defaulting into the repo means a fresh clone can assess a PDF.
WP = Path(os.environ.get("ACP_PDF_ENGINE") or (ACP / "engine" / "pdf-analyser"))
# Resolve dotnet the same way the test capability gates do (tests/engines.py): explicit
# override, then PATH, then the dev-machine install location. PATH matters wherever the
# SDK is installed by a package manager or CI action rather than the dotnet-install
# script — Homebrew puts it in /opt/homebrew/bin, actions/setup-dotnet on PATH — and
# neither creates ~/.dotnet/dotnet. Without the PATH lookup those environments resolved
# to a nonexistent path and every Office scan died with FileNotFoundError, while the
# gates (which DO check PATH) had already decided the engine was available.
DOTNET = (os.environ.get("ACP_DOTNET") or shutil.which("dotnet")
          or os.path.expanduser("~/.dotnet/dotnet"))
CLI_DLL = Path(os.environ.get("ACP_OFFICE_CLI")
               or (ACP / "spike/dotnet/AcpScan.Cli/bin/Release/net10.0/AcpScan.Cli.dll"))
# Demo corpus folder (ADC / keyless mode). Overridden by ACP_DRIVE_FOLDER env var.
# In per-user token mode (GIS), the scanner searches the user's whole Drive.
_DEMO_FOLDER = os.environ.get("ACP_DRIVE_FOLDER") or "1W27ULZsstP7gYGzgKKBId0qEfNxeKn0_"
# Fan-out discovery cap (ADR 0007): each file is its own durable job, so the
# whole estate need not fit one box — bound only against a runaway listing.
FANOUT_MAX_FILES = int(os.environ.get("ACP_FANOUT_MAX_FILES", "50000"))
# ADR 0008 — batch fan-out. Files per scan_batch job (capped ≤200 so job payloads stay
# bounded); estates with ≥ THRESHOLD files auto-use the batch path. Both env-tunable.
SCAN_BATCH_SIZE = max(1, min(200, int(os.environ.get("ACP_SCAN_BATCH_SIZE", "50"))))
SCAN_BATCH_THRESHOLD = int(os.environ.get("ACP_SCAN_BATCH_THRESHOLD", "2000"))
# No more SCAN_TRACE_SPAN_CAP — file-centric tracing (lf.file_trace) gives every file its
# own trace, so there's no single big trace whose span count could make the Langfuse OSS
# detail view un-openable (the problem that constant used to guard against).
SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]

sys.path.insert(0, str(ACP / "scripts"))
from rubric import Rubric
from swallowed import swallowed
import scan_formats

OFFICE = (".docx", ".pptx", ".xlsx")
HTML_EXTS = (".html", ".htm")

# Google Workspace native types → (export MIME, file extension)
EXPORT_MAP = {
    "application/vnd.google-apps.document":     ("application/vnd.openxmlformats-officedocument.wordprocessingml.document",  ".docx"),
    "application/vnd.google-apps.spreadsheet":  ("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",        ".xlsx"),
    "application/vnd.google-apps.presentation": ("application/vnd.openxmlformats-officedocument.presentationml.presentation", ".pptx"),
}

# All MIME types Discovery may list — uploaded files in scope, plus the Google Workspace natives
# whose export target is in scope (a Google Doc rides on docx; see scan_formats).
#
# FUNCTIONS, not the module-level constants they replaced. The scope is env-configurable
# (ACP_SCAN_FORMATS) and a value computed at import time latches whatever the environment said at
# first import — the exact shape of the incident api/worker_main.py's header records, where an
# import-time int() beat an env var set afterwards and the container ran with zero workers while
# reporting itself healthy. Cheap enough to call per page: frozenset construction over ~7 strings.
def _scannable_mime() -> frozenset[str]:
    """Every MIME type a listing may keep, for the CURRENT format scope."""
    return scan_formats.upload_mimes() | scan_formats.google_native_in_scope(EXPORT_MAP)


def _drive_mime_q() -> str:
    """The `mimeType=...` disjunction, for the callers that still build a Drive query from it.

    NOTE for anyone reaching for this in the scan path: `_search_drive` deliberately does NOT use
    it. A mimeType clause is served by Drive's SEARCH INDEX, which lags the live file store badly
    enough to hide freshly-uploaded files (see `_search_drive`'s docstring for the measurement).
    Sorted so the query string is stable across calls — it is otherwise set-ordered, which makes
    it differ run to run and defeats both caching and log comparison.
    """
    return " or ".join(f"mimeType='{m}'" for m in sorted(_scannable_mime()))


def _noop(_):
    pass


def _safe_name(name: str) -> str:
    """Strip chars that are invalid on most filesystems."""
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name)


# ── Whole-estate per-file inventory (PRD Phase A2) ──────────────────────────────────────────────
# Discovery inventories EVERY accessible file — supported docs AND media/unsupported/extensionless
# — with its source metadata, even though only the assessable subset is ever downloaded and
# analysed (handlers._scan_assess re-filters to assessable). These helpers build one inventory row
# from a raw source object; the row shape matches store.add_inventory's accepted keys.

def _inv_size_kb(size) -> int | None:
    """Bytes → whole KiB, or None when the source didn't report a size."""
    try:
        return round(int(size) / 1024) if size is not None else None
    except (TypeError, ValueError):
        return None


def _first_owner(owners) -> str | None:
    """First owner's display name (falling back to email), or None."""
    owners = owners or []
    if not owners:
        return None
    o = owners[0]
    return o.get("displayName") or o.get("emailAddress")


def _estate_doc_class(name: str, mime: str | None) -> str:
    """A doc_class for the inventory row that is honest about capability, uniform across formats.

    Assessable formats keep their document class (slide-deck / text-document / pdf-document /
    spreadsheet / web-page — the existing metadata classification); media and unsupported files
    report their estate bucket (image / audio-video / unsupported) so a reader can see WHY ACP
    will not assess the file rather than reading a blank as 'passed'.
    """
    row = estate_inventory.classify({"name": name, "mimeType": mime or ""})
    if row["status"] == estate_inventory.ASSESSABLE:
        import classify as _cls
        return _cls.classify_from_metadata(name, mime)["doc_class"]
    return {"image": "image", "av": "audio-video"}.get(row["format"], "unsupported")


def _inv_row(*, file: str, drive_file_id=None, mime=None, size=None, checksum=None, path=None,
             created_at=None, source_modified=None, owner=None, parent_folder=None,
             drive_id=None, site_id=None, library_name=None, site_name=None,
             sp_meta=None) -> dict:
    """One store.add_inventory row, with size normalised to KiB and doc_class derived.

    `drive_id` is the Graph DRIVE a SharePoint/OneDrive item was listed from. It is part of the
    item's identity, not decoration: Graph item ids are unique only within a drive, so a row
    carrying `drive_file_id` alone cannot be fetched back reliably (_sp_download). None for every
    other source, and None for a OneDrive listing, which legitimately has no drive to name.

    `site_id` and `library_name` complete that identity for SharePoint. A scan can span up to 30
    sites now, so "which site is this document in" is no longer answerable from the run's scope —
    the scope holds a SET. Recorded per row rather than derived, because a drive id names nothing
    to a reader and does not say which site it belongs to, and because every later phase (per-site
    metadata, per-library deltas, per-site exception reports, write-back targeting) needs the
    boundary this preserves. None for OneDrive, which has no site, and for every other source.

    `sp_meta` is the normalized SharePoint-native metadata record (api/sp_metadata.normalize).
    The fields a lifecycle rule keys on are promoted to their own columns so a rule preview can
    SELECT them; the rest — the tenant's own managed columns, and the per-field availability —
    ride in one JSON column, because ACP cannot know in advance what a tenant calls its columns
    and a schema that tried to would need a migration per customer.

    THE AVAILABILITY MAP IS PERSISTED, not just the values. An empty `retention_label` cell in an
    export is uninterpretable without it: the reader cannot tell an estate with no retention plan
    from one whose labels ACP was refused. That distinction is the Phase 2 exit gate, and it only
    survives to the auditor if it is written down at the row.
    """
    import json as _json
    _f = (sp_meta or {}).get("fields") or {}

    def _v(name):
        """The value, but only when the field is actually PRESENT — a `not_configured` or
        `unavailable` field must write NULL, never a placeholder that reads as a real answer."""
        f = _f.get(name) or {}
        return f.get("value") if f.get("state") == "present" else None

    _extra = {}
    if sp_meta:
        managed = _v("managed_columns")
        _extra = {"availability": {k: (f or {}).get("state") for k, f in _f.items()},
                  "managed_columns": managed or {},
                  # The reasons, kept only where there IS one: an export cell that says "not
                  # read because the tenant refused the listItem expansion" is actionable in a
                  # way that a blank is not.
                  "reasons": {k: (f or {}).get("reason") for k, f in _f.items()
                              if (f or {}).get("reason")},
                  # SMART-ARCHIVAL inputs, in the blob rather than in columns of their own. They
                  # are DERIVED from fields already stored (createdBy/lastModifiedBy, or the
                  # permissions collection), so a pair of new columns would buy a schema version
                  # (ADR 0045) for values the blob beside them can carry. The count never
                  # travels without the basis: see sp_metadata's collaborator_count for why one
                  # without the other is a number that means two different things.
                  "collaborators": {"count": _v("collaborator_count"),
                                    "basis": _v("collaborator_basis")},
                  # Activity, on the same convention and for the same reason: derived from a
                  # read that already happened, so no new column. Absent from the blob when the
                  # analytics container was never read — which is what keeps a rule from seeing
                  # a zero nobody measured.
                  "activity": {"actors": _v("recent_actor_count"),
                               "actions": _v("recent_action_count"),
                               "window_days": sp_metadata.ANALYTICS_WINDOW_DAYS}}
    return {"file": file, "drive_file_id": drive_file_id, "mime": mime or None,
            "size_kb": _inv_size_kb(size), "doc_class": _estate_doc_class(file, mime),
            "checksum": checksum, "path": path, "created_at": created_at,
            "source_modified": source_modified, "owner": owner, "parent_folder": parent_folder,
            "drive_id": drive_id, "site_id": site_id, "library_name": library_name,
            "site_name": site_name,
            "content_type": _v("content_type"),
            "retention_label": _v("retention_label"),
            "sensitivity_label": _v("sensitivity_label"),
            "sharing_scope": _v("sharing_scope"),
            "item_kind": _v("item_kind"),
            "checked_out_by": _v("checked_out_by"),
            "sp_version": _v("version"),
            "modified_by": _v("modified_by"),
            "sp_metadata": _json.dumps(_extra, separators=(",", ":")) if _extra else None}


def _drive_inventory_row(f: dict) -> dict:
    """Inventory row from a raw Drive file object (any type)."""
    parents = f.get("parents") or []
    return _inv_row(file=_safe_name(f.get("name", "") or ""), drive_file_id=f.get("id"),
                    mime=f.get("mimeType"), size=f.get("size"), checksum=f.get("md5Checksum"),
                    created_at=f.get("createdTime"), source_modified=f.get("modifiedTime"),
                    owner=_first_owner(f.get("owners")),
                    parent_folder=parents[0] if parents else None)


def _sp_inventory_row(item: dict, *, site_id: str | None = None,
                      library_name: str | None = None, site_name: str | None = None,
                      meta: dict | None = None) -> dict:
    """Inventory row from a raw MS Graph driveItem (any type).

    `site_id`/`library_name` come from the WALK, not from the item: a driveItem names its drive
    (parentReference.driveId) but never the site or the library's display name, so an item that
    travelled alone could not be attributed to the site the operator selected."""
    fmeta = item.get("file") or {}
    cb = (item.get("createdBy") or {}).get("user") or {}
    lb = (item.get("lastModifiedBy") or {}).get("user") or {}
    owner = cb.get("displayName") or cb.get("email") or lb.get("displayName") or lb.get("email")
    parent = (item.get("parentReference") or {}).get("path")
    return _inv_row(file=_safe_name(item.get("name", "") or ""), drive_file_id=item.get("id"),
                    mime=fmeta.get("mimeType"), size=item.get("size"),
                    # quickXorHash — see the identical field on _sp_list's scannable `rec`. Set
                    # here too for parity with _drive_inventory_row, even though a non-scannable
                    # item is never analysed: the estate inventory should not look checksum-less
                    # for SharePoint when the data was there all along.
                    checksum=(fmeta.get("hashes") or {}).get("quickXorHash"),
                    created_at=item.get("createdDateTime"),
                    source_modified=item.get("lastModifiedDateTime"),
                    owner=owner, parent_folder=parent,
                    # The drive half of the item's identity. Absent on a OneDrive listing, which
                    # has no driveId to give — read downstream as /me/drive, which is right there.
                    drive_id=(item.get("parentReference") or {}).get("driveId"),
                    site_id=site_id, library_name=library_name, site_name=site_name,
                    sp_meta=meta)


def _local_stat_meta(p: Path, corpus: Path) -> dict:
    """Filesystem metadata for one local file — the local-source analogue of the size / createdTime
    / modifiedTime / owner a Drive or SharePoint listing carries, so a local inventory is as rich as
    a connector one. Best-effort: any field the OS won't give us comes back None rather than
    aborting the walk. `parent_folder` is the file's directory relative to the scan root."""
    import mimetypes
    size = source_modified = created_at = None
    try:
        st = p.stat()
        size = st.st_size
        source_modified = datetime.fromtimestamp(st.st_mtime, timezone.utc).isoformat()
        # st_birthtime is the true creation time on macOS/BSD; ctime is the closest stand-in elsewhere.
        created_at = datetime.fromtimestamp(getattr(st, "st_birthtime", st.st_ctime),
                                            timezone.utc).isoformat()
    except OSError:
        pass
    try:
        owner = p.owner()                 # POSIX login name; unavailable on Windows / dangling uid
    except (KeyError, OSError, NotImplementedError):
        owner = None
    try:
        parent = str(p.parent.relative_to(corpus))
    except ValueError:
        parent = p.parent.name
    if parent in (".", ""):
        parent = corpus.name or None
    return {"size": size, "source_modified": source_modified, "created_at": created_at,
            "owner": owner, "parent_folder": parent, "mime": mimetypes.guess_type(p.name)[0]}


def _dedupe_inventory_files(rows: list[dict]) -> None:
    """Disambiguate colliding `file` names IN PLACE so store.add_inventory's (scan_id, file)
    primary key never silently upserts one estate file over another. First occurrence keeps its
    name (so the assessable rows, added first by the caller, stay canonical); a later collision
    gets a ' (N)' suffix, mirroring _dedupe_names."""
    seen: dict[str, int] = {}
    for r in rows:
        name = r.get("file") or ""
        r.setdefault("source_name", name)
        n = seen.get(name, 0)
        seen[name] = n + 1
        if n:
            stem, dot, ext = name.rpartition(".")
            r["file"] = f"{stem or name} ({n}){dot}{ext}" if dot else f"{name} ({n})"


def _drive_service(drive_token: str | None = None):
    """Drive client for THIS scan. A per-user token (from GIS 'Sign in with Google')
    scans that user's Drive; with no token it falls back to ADC (the demo identity)."""
    from googleapiclient.discovery import build
    if drive_token:
        from google.oauth2.credentials import Credentials
        # GIS tokens are short-lived and carry no refresh_token, so leave `expiry` None: a
        # credential without an expiry reports `expired` False and google-auth never tries to
        # refresh it. Drive returns 401 on its own if the token really expired.
        #
        # Setting `expiry = now + 1h` did the opposite of what its comment claimed. The hour is
        # measured from when this client is BUILT, not from when Google issued the token, so a
        # scan queued behind a backlog crossed it, google-auth called refresh(), and the job
        # died on "credentials do not contain the necessary fields need to refresh the access
        # token" — five retries deep. See worker.drive_session_expired.
        creds = Credentials(token=drive_token, scopes=SCOPES)
    else:
        import google.auth
        # ADC is correct only for scheduled sweeps. An interactive scan that reaches this path
        # has lost its token — it will scan the ADC service-account identity, find nothing, and
        # complete silently with 0 files. Log it so the failure is visible in worker output.
        print(
            "WARNING: _drive_service called with no user token — using ADC. "
            "If this is an interactive scan (not a scheduled sweep), the token was lost.",
            flush=True,
        )
        creds, _ = google.auth.default(scopes=SCOPES)
    # `credentials=` (rather than `http=`) is the shortcut build() uses to construct its own
    # AuthorizedHttp — with no way to pass a timeout through it. build() also refuses `http=` and
    # `credentials=` together, so getting a bounded socket means constructing the AuthorizedHttp
    # ourselves. See _DRIVE_HTTP_TIMEOUT_S above for why this matters.
    import httplib2
    from google_auth_httplib2 import AuthorizedHttp
    http = AuthorizedHttp(creds, http=httplib2.Http(timeout=_DRIVE_HTTP_TIMEOUT_S))
    # Folder BFS shares this service between threads. Its default httplib2 pool
    # cannot be shared: each execute owns and closes a bounded transport.
    from drive_http import isolated_request_builder
    return build("drive", "v3", http=http, cache_discovery=False,
                 requestBuilder=isolated_request_builder(creds, _DRIVE_HTTP_TIMEOUT_S))


def drive_account_id(svc) -> str | None:
    """The Google account THIS Drive client is authenticated as — svc.about()'s own identity,
    not a file's owner (scan_inventory.owner is a per-FILE fact, e.g. a shared file's owner,
    and can differ from the signed-in user). Stamped onto every Drive scan_inventory row (see
    _list's drive branch) so a later scan can tell whether it would be reconstructing from the
    SAME account's prior estate — core._drive_prior_inventory_for_account is the guard that
    reads it back.

    Best-effort: any failure (network, insufficient scope, a test double with no `.about`)
    returns None rather than raising, since identifying WHO ran a listing is secondary to the
    listing itself. A None here still resolves safely — it only ever matches another None, so
    an unverifiable current identity against a KNOWN prior one reads as a mismatch, never a
    silent pass (see _drive_prior_inventory_for_account)."""
    try:
        about = svc.about().get(fields="user").execute(num_retries=5) or {}
        return about.get("user", {}).get("emailAddress")
    except Exception:
        return None


def _normalize(files: list[dict]) -> list[dict]:
    """Convert raw Drive API file objects to scan items.

    Uploaded files keep their name. Google Workspace files get the export
    extension appended so the Office/PDF engine sees the right format.
    """
    result = []
    seen: set[str] = set()
    seen_ids: set[str] = set()
    skipped = 0
    relisted = 0
    for f in files:
        # IDENTITY dedup comes first. Drive can hand the SAME file back more than once in a
        # single discovery: a file may have several parents (so a subtree BFS meets it twice),
        # corpora="allDrives" can surface it from My Drive and a Shared Drive, and paged
        # listings can overlap. Without this, the name-dedup below renames the second sighting
        # to "Report (1).pptx" — a phantom document that the UI then shows as "x2 copies",
        # inflates the file count, and gets downloaded and scanned a second time.
        # A Drive file id IS the document's identity; two parents do not make two documents.
        fid = f.get("id")
        if fid is not None:
            if fid in seen_ids:
                relisted += 1
                continue
            seen_ids.add(fid)
        mime = f.get("mimeType", "")
        raw_name = f["name"]
        if mime in EXPORT_MAP:
            export_ext = EXPORT_MAP[mime][1]
            name = _safe_name(raw_name) + export_ext
        else:
            ext = Path(raw_name).suffix.lower()
            # Enforce the same operator-configurable scope as the MIME filter.
            if ext not in scan_formats.extensions():
                skipped += 1
                continue
            name = _safe_name(raw_name)
        # Disambiguate GENUINELY DISTINCT files that share a name (Drive allows two different
        # file ids named the same; a filesystem wouldn't). Reaching here means the id is new,
        # so a " (N)" suffix always denotes a real second document — never a re-listing.
        unique = name
        n = 1
        while unique in seen:
            stem = Path(name).stem
            suffix = Path(name).suffix
            unique = f"{stem} ({n}){suffix}"
            n += 1
        seen.add(unique)
        # md5Checksum is absent for native Google Workspace files (Docs/Sheets/Slides
        # have no fixed byte representation — exported on each request) so it's only
        # ever present for real binary uploads, which is exactly the case checksum
        # dedup cares about.
        # `source_mime` is the REAL source MIME (application/pdf, a Google-native type, …), kept
        # for the inventory row's `mime` column. It is deliberately SEPARATE from `mime`, which
        # stays the Google-native EXPORT selector _download keys off — overloading one field would
        # send a plain PDF's "application/pdf" into EXPORT_MAP and KeyError the download.
        parents = f.get("parents") or []
        result.append({"name": unique, "id": f["id"], "checksum": f.get("md5Checksum"),
                       "source_modified": f.get("modifiedTime"),
                       "source_mime": mime or None,
                       "created_at": f.get("createdTime"),
                       "owner": _first_owner(f.get("owners")),
                       "parent_folder": parents[0] if parents else None,
                       "size_kb": _inv_size_kb(f.get("size")),
                       **({"mime": mime} if mime in EXPORT_MAP else {})})
    if skipped:
        # Not silent anymore: unsupported types (images, .txt/.csv, legacy .doc/.ppt/.xls,
        # video, …) are out of accessibility scope — say how many so 'scan complete' isn't
        # mistaken for 'everything was covered'.
        print(f"[scan] {skipped} file(s) skipped as unsupported for accessibility scanning "
              f"(only pdf/docx/pptx/xlsx/html are analysed)", flush=True)
    if relisted:
        print(f"[scan] {relisted} duplicate listing(s) of the same Drive file id collapsed "
              f"(multi-parent / shared-drive / paging overlap) — not extra documents", flush=True)
    return result


# --- PRD Phase 3: Drive incremental sync (changes.list) -------------------------
# The scheduled sweep (core._do_scheduled_scan) re-lists Drive's ENTIRE estate on every
# fire today, even when nothing changed. Drive's Changes API answers "what changed since
# a prior checkpoint" directly, so a sweep can check cheaply and skip the expensive full
# scan when the answer is "nothing" — see core._drive_sync_plan for how this is used.
#
# Deliberately NOT plumbed into the scan pipeline itself (run_scan/_list still always do a
# full listing+download+analyse pass when they run at all): _list's scope/inventory/
# truncation bookkeeping is exactly the kind of estate-size accounting CLAUDE.md's own
# incident history warns is easy to get subtly wrong for a PARTIAL result. Gating whether
# the existing, already-correct full scan runs is the safe slice; feeding a partial item
# list into that pipeline is a larger, separate piece of work.

def drive_start_page_token(svc) -> str:
    """The current 'blank slate' cursor: changes.list(pageToken=this) returns nothing until
    something changes henceforth. Used to seed a fresh baseline when no cursor is stored yet."""
    return svc.changes().getStartPageToken().execute(num_retries=5)["startPageToken"]


# Same fields _search_drive/_normalize already read (provenance.DRIVE_FIELDS), plus `trashed`
# — changes.list is the only listing path that needs it: a trashed file is still technically
# present as far as `_search_drive`'s live q= filters are concerned (they exclude trashed at
# query time), but a change EVENT for a trashed file must be told apart from a real edit here.
_DRIVE_CHANGES_FIELDS = ("nextPageToken,newStartPageToken,"
                        f"changes(fileId,removed,file({provenance.DRIVE_FIELDS},trashed))")


def drive_changes_since(svc, page_token: str) -> tuple[list[dict], set[str], str]:
    """Page through Drive's changes.list from `page_token` to the end. Returns (changed_files —
    RAW Drive file resources, unfiltered and unnormalized, the exact shape files().list() would
    give you — removed_ids — file ids removed or trashed since `page_token` — and the new page
    token to persist for next time).

    Raw, not _normalize()d: a caller reconstructing a full estate (see apply_drive_delta) needs
    to run the SAME _normalize()/estate_inventory.summarize() pass over the MERGED prior+delta
    set that a fresh listing would, not a pre-filtered one — normalizing here would silently
    drop non-scannable changed files from that reconstruction (a video's metadata changing, a
    brand-new .zip) even though a fresh listing would have counted them in the estate inventory.
    OS metadata (.DS_Store, Thumbs.db, …) is dropped here, same as _search_drive's own page
    filter, since it is never a real document under any listing path.

    Raises the SDK's own HttpError on an expired or invalid page token (Drive expires an
    unused one after ~1 week — a 404) or any other API failure; the caller decides how to
    degrade (core._drive_delta_check always falls back to a full scan rather than trusting a
    failed check)."""
    raw_files: list[dict] = []
    removed_ids: set[str] = set()
    token = page_token
    while True:
        resp = svc.changes().list(pageToken=token, fields=_DRIVE_CHANGES_FIELDS,
                                  includeItemsFromAllDrives=True, supportsAllDrives=True,
                                  spaces="drive").execute(num_retries=5)
        for ch in resp.get("changes", []):
            f = ch.get("file")
            fid = ch.get("fileId")
            if ch.get("removed") or (f and f.get("trashed")):
                if fid:
                    removed_ids.add(fid)
                continue
            if f and not estate_inventory.is_os_metadata(f.get("name", "") or ""):
                raw_files.append(f)
        if "nextPageToken" in resp:
            token = resp["nextPageToken"]
            continue
        return raw_files, removed_ids, resp["newStartPageToken"]


def _find_remediated_folder_id(svc) -> str | None:
    """Look up the configured Drive mirror folder (default 'Remediated') WITHOUT
    creating it (unlike handlers.ensure_remediated_folder) — a discovery-time
    exclusion check shouldn't spuriously create the folder for a user who's never
    remediated anything. Returns None if it doesn't exist yet."""
    import core
    name = core.store.get_drive_mirror_folder()
    safe = name.replace("\\", "\\\\").replace("'", "\\'")
    q = f"name='{safe}' and mimeType='application/vnd.google-apps.folder' and trashed=false"
    folders = svc.files().list(q=q, fields="files(id)", orderBy="createdTime",
                               pageSize=1, includeItemsFromAllDrives=True,
                               supportsAllDrives=True).execute(num_retries=5).get("files", [])
    return folders[0]["id"] if folders else None


def _list_drive_page_all(svc, q: str, max_files: int, on_page=None) -> tuple[list[dict], bool]:
    """One full paginated listing for `q`. Returns (raw files, hit_cap).

    on_page, when given, is called with each page's raw file list immediately after it arrives —
    before the next API call. This lets callers filter and emit progress mid-listing rather than
    waiting for the full (potentially minutes-long) pagination to finish.
    """
    files: list[dict] = []
    page_token = None
    incomplete = False
    while len(files) < max_files:
        resp = svc.files().list(
            q=q,
            fields=f"nextPageToken,incompleteSearch,files({provenance.DRIVE_FIELDS})",
            pageSize=200,
            # Newest first: when a large Drive exceeds the raw cap, the files a user most likely
            # just uploaded are the ones we most want to have listed before the cap bites.
            orderBy="modifiedTime desc",
            pageToken=page_token,
            # Google's allDrives corpus can return incompleteSearch=true when it cannot search
            # every drive. The user corpus is the complete set owned by or shared to this signed-
            # in user and is Google's recommended broad corpus for this case.
            corpora="user",
            spaces="drive",
            includeItemsFromAllDrives=True,
            supportsAllDrives=True,
        ).execute(num_retries=5)             # backoff on 429/5xx instead of failing the file
        incomplete = incomplete or bool(resp.get("incompleteSearch"))
        page = resp.get("files", [])
        files.extend(page)
        if on_page:
            on_page(page)
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return files, bool(page_token) or incomplete


def _flag_on(name: str) -> bool:
    """Is this env flag on? `"0"` and `"false"` mean OFF.

    `os.environ.get(name)` is a non-empty STRING for `ACP_DRIVE_DISCOVERY_DEBUG=0`, and every
    non-empty string is truthy — so setting the flag to 0 to turn the diagnostic OFF left it on.
    Found 2026-07-29: the live container had the flag at "0" and was still emitting
    `[scan] discovery DEBUG:` on every sweep, including the extra unfiltered Drive listing the
    gate exists to avoid. Matches the on/off vocabulary core.py already uses for ACP_REVIEW_MEMORY.
    """
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


def _debug_dump_account(svc, max_files: int = 60) -> None:
    """Log EVERY file Drive returns for this account, filtered only by trashed=false — so a file
    the scan missed because its MIME type isn't scannable (a legacy .doc, an .odt, an image),
    or because it belongs to another owner (shared-with-me), is visible in the log instead of
    silently absent. Gated behind ACP_DRIVE_DISCOVERY_DEBUG because it is an extra unfiltered
    listing; turn it on to answer 'why did the scan only see N files?' and off again after.
    A best-effort diagnostic: it must never break a scan."""
    try:
        scannable = _scannable_mime() | {"application/vnd.google-apps.folder"}
        resp = svc.files().list(
            q="trashed=false", pageSize=max_files, orderBy="modifiedTime desc",
            fields="files(id,name,mimeType,owners(emailAddress))",
            corpora="allDrives", includeItemsFromAllDrives=True, supportsAllDrives=True,
        ).execute(num_retries=3)
        rows = resp.get("files", [])
        print(f"[scan] discovery DEBUG: account holds {len(rows)} recent non-trashed item(s) "
              f"(unfiltered, newest first):", flush=True)
        for f in rows:
            mark = "scannable" if f.get("mimeType") in scannable else "SKIPPED (type not scanned)"
            print(f"[scan] discovery DEBUG:   mime={f.get('mimeType')} · {mark}", flush=True)
    except Exception as e:  # noqa: BLE001 — a diagnostic must never fail the scan
        print(f"[scan] discovery DEBUG: unfiltered listing failed: {e}", flush=True)


def _is_scannable_mime(f: dict) -> bool:
    """Is this Drive file in Discovery's format scope? Applied in Python, deliberately."""
    return f.get("mimeType") in _scannable_mime()


def _is_drive_rate_limit_error(exc: BaseException) -> bool:
    """Was this exception Drive telling us to slow down, specifically — not just any failure?

    `.execute(num_retries=N)` already retries 429/5xx internally with backoff (see its call
    sites); by the time this sees the exception, those retries are exhausted and the folder is
    about to be silently skipped. This only CLASSIFIES what already happened — it does not touch
    retry behavior or timing, so it carries none of the risk a change to the actual backoff logic
    would. 403 is included because Drive reports its per-user rate limit as a 403 with a specific
    reason string, not a 429 — a bare 403 (e.g. permission denied) does NOT match unless that
    reason text is present.
    """
    try:
        from googleapiclient.errors import HttpError
    except ImportError:
        return False
    if not isinstance(exc, HttpError):
        return False
    status = getattr(exc.resp, "status", None) if getattr(exc, "resp", None) is not None else None
    if status == 429:
        return True
    if status == 403:
        reason = str(exc)
        return any(r in reason for r in ("rateLimitExceeded", "userRateLimitExceeded", "quotaExceeded"))
    return False


def _search_drive(svc, max_files: int = 500, exclude_remediated: bool = False,
                  scope_out: dict | None = None, inventory_out: list | None = None,
                  progress_cb=None) -> list[dict]:
    """Whole-Drive discovery — returns all scannable files regardless of folder.

    `scope_out`, when given, is filled in with WHAT THIS LISTING COVERED — see `_list`. The
    counts already printed below went only to stdout, so the file count was the one number that
    escaped to the UI and it arrived with no statement of its boundary.

    The type filter is applied HERE, in Python, not in the Drive query — and that is the whole
    point. A `mimeType='…'` clause is served by Drive's SEARCH INDEX, which lags badly behind a
    plain listing for freshly-uploaded files: proven live, an unfiltered `trashed=false` listing
    returned 60 documents while the same query with a mimeType filter returned 2, in the same
    second. A settle-retry cannot fix that — both passes hit the same stale index. So we ask
    Drive only for `trashed=false` (read from the live file store, so a just-uploaded file
    appears at once) and keep the scannable types ourselves.

    The remediated-folder exclusion clause is dropped for the same reason (`… in parents` is also
    index-served); provenance (is_acp_generated, a client-side check on file properties) already
    skips ACP's own output wherever it sits.

    The settle-retry (ACP_DRIVE_SETTLE_SECS>0) is kept as a small belt-and-suspenders for the
    residual lag in even the plain listing, re-listing and unioning by id until a pass adds
    nothing new.
    """
    if _flag_on("ACP_DRIVE_DISCOVERY_DEBUG"):
        _debug_dump_account(svc)

    q = "trashed=false"
    # Raw ceiling: we now page over ALL files, not just scannable ones, so on a large Drive the
    # scannable documents could sit behind many non-scannable items. Page generously past
    # max_files so scannable coverage holds, but stay bounded so a huge Drive can't run away.
    raw_cap = max(max_files * 5, 2500)
    settle_secs = float(os.environ.get("ACP_DRIVE_SETTLE_SECS", "0") or 0)
    settle_passes = int(os.environ.get("ACP_DRIVE_SETTLE_PASSES", "2") or 0)
    by_id: dict[str, dict] = {}      # union across settle passes, keyed by Drive file id
    inv_by_id: dict[str, dict] = {}  # EVERY file (any type) for the estate inventory, same keying
    raw_seen = 0
    hit_cap = False
    extra = 0
    _last_progress_at = [0.0]  # throttle: emit at most once every 2s per settle pass

    def _on_page(page: list[dict]) -> None:
        """Filter one Drive page into by_id/inv_by_id and emit live progress."""
        for f in page:
            # OS metadata files (.DS_Store, Thumbs.db, …) are synced by cloud agents but are
            # never user documents. Drop them before the inventory so they do not inflate
            # unsupported-file counts or consume lifecycle-rule evaluation capacity.
            if estate_inventory.is_os_metadata(f.get("name", "") or ""):
                continue
            # The estate inventory keeps EVERY file — the un-filtered denominator the dashboard
            # funnel reports. Scanning still keeps only the scannable subset just below; this only
            # changes what we COUNT, never what we assess or remediate.
            inv_by_id.setdefault(f["id"], f)
            if _is_scannable_mime(f):     # the filter Drive's index was too stale to do
                by_id.setdefault(f["id"], f)
        if progress_cb:
            now = time.monotonic()
            if now - _last_progress_at[0] >= 2.0:
                _last_progress_at[0] = now
                progress_cb(len(by_id))

    while True:
        before = len(by_id)
        batch, cap = _list_drive_page_all(svc, q, raw_cap, on_page=_on_page)
        hit_cap = cap
        raw_seen = max(raw_seen, len(batch))
        # by_id and inv_by_id are already populated by _on_page during paging
        added = len(by_id) - before
        # Stop: settle disabled, budget spent, or a follow-up pass found nothing new.
        if settle_secs <= 0 or extra >= settle_passes or (extra > 0 and added == 0):
            break
        extra += 1
        time.sleep(settle_secs)
    if extra:
        print(f"[scan] discovery settle: {extra} extra pass(es) over ~{extra * settle_secs:.0f}s "
              f"to let Drive's live listing settle", flush=True)

    if hit_cap:
        print(f"[scan] whole-Drive listing was incomplete or hit the {raw_cap}-item raw cap — "
              f"the reported inventory is a floor, not a complete estate count", flush=True)
    return _finish_drive_listing(by_id, inv_by_id, raw_seen=raw_seen, hit_cap=hit_cap,
                                 max_files=max_files, exclude_remediated=exclude_remediated,
                                 scope_out=scope_out, inventory_out=inventory_out, cap=raw_cap)


def _finish_drive_listing(by_id: dict[str, dict], inv_by_id: dict[str, dict], *, raw_seen: int,
                          hit_cap: bool, max_files: int, exclude_remediated: bool,
                          scope_out: dict | None, inventory_out: list | None,
                          cap: int | None = None, extra_scope: dict | None = None) -> list[dict]:
    """The common tail of a whole-Drive listing, regardless of how `by_id`/`inv_by_id` (every
    scannable / every discovered file, keyed by Drive id) were built: a live walk (_search_drive)
    and a delta-reconstructed listing (drive_reconstructed_listing) both end here, so a
    reconstruction is indistinguishable downstream from a fresh listing — same provenance
    filtering, same _normalize(), same non-scannable estate inventory, same scope_out contract.
    `cap` is the raw-listing ceiling (meaningless for a reconstruction, so omitted there);
    `extra_scope` merges into scope_out last, for caller-specific keys (e.g. marking a listing
    as reconstructed) without touching this shared contract."""
    listed = len(by_id)              # distinct SCANNABLE items, before provenance filtering
    files = list(by_id.values())
    skipped_acp = 0
    if exclude_remediated:
        # Provenance beats location: skip anything ACP itself wrote, wherever it sits.
        kept = [f for f in files if not provenance.is_acp_generated(f)]
        skipped_acp = len(files) - len(kept)
        files = kept
    result = _normalize(files[:max_files])
    # Whole-estate inventory: classify every file discovered (any format), flagging ACP's own
    # output as EXCLUDED so it isn't counted as the user's content. Assessment/remediation are
    # untouched — this is the DISCOVERED denominator, reported alongside the scannable one.
    inv_files = list(inv_by_id.values())
    for f in inv_files:
        if provenance.is_acp_generated(f):
            f["_excluded"] = True
    # Per-file inventory of the NON-scannable estate (media / unsupported / extensionless). The
    # scannable files are inventoried by the caller from the analysis set (canonical names), so
    # skip anything already in `result`; skip folders (not content) and ACP's own output.
    if inventory_out is not None:
        result_ids = {it.get("id") for it in result}
        for f in inv_files:
            if f.get("mimeType") == estate_inventory.FOLDER_MIME or f.get("_excluded"):
                continue
            if f.get("id") in result_ids:
                continue
            inventory_out.append(_drive_inventory_row(f))
    # hit_cap means the raw listing stopped at raw_cap before the end of the estate, so the
    # inventory counts are a floor — flag it so a >ceiling estate is never reported as complete.
    inventory = estate_inventory.summarize(inv_files, truncated=hit_cap)
    if scope_out is not None:
        scope_out.update({"kind": "drive", "raw": raw_seen, "scannable": listed,
                          "skipped_acp": skipped_acp, "kept": len(result),
                          "truncated": bool(hit_cap or len(files) > max_files),
                          "inventory": inventory})
        if cap is not None:
            scope_out["cap"] = cap
        if extra_scope:
            scope_out.update(extra_scope)
    print(f"[scan] estate inventory: {inventory['discovered']} file(s) discovered · "
          f"{inventory['assessment_eligible']} assessment-eligible · "
          f"by format {inventory['by_format']}", flush=True)
    # An audit trail of what this scan chose to ingest, and what it refused. Without it the
    # only place a file count exists is the UI, and "why 2 files?" can't be answered offline.
    print(f"[scan] discovery (whole-Drive): {raw_seen} raw · {listed} scannable · "
          f"{skipped_acp} skipped as ACP-generated output · {len(result)} kept", flush=True)
    print(f"[scan] discovery:   kept {len(result)} file(s)", flush=True)
    return result


# --- PRD Phase 3: reconstruct a whole-Drive listing from a prior scan + a Changes API delta ----
# Avoids walking Drive at all for a scheduled sweep when SOMETHING changed but not everything:
# the prior scan's own persisted scan_inventory already IS last known estate, and Drive's
# Changes API already hands over fresh metadata for exactly what's different. Reusing
# _finish_drive_listing above means this produces the identical scope_out/inventory_out
# contract a fresh _search_drive call would — the one property CLAUDE.md's own incident
# history says is easy to get subtly wrong for a partial result.

def _drive_file_from_inventory_row(row: dict) -> dict:
    """The inverse of _drive_inventory_row: reconstruct a raw Drive-file-resource-shaped dict
    from one persisted scan_inventory row (store.latest_scan_inventory_items), so a
    reconstructed listing can run through the exact same _normalize()/estate_inventory.summarize
    tail a fresh Drive listing does. `size` is approximate — scan_inventory only ever stored the
    KB-rounded value, so the byte count round-trips lossily — but size is cosmetic (sort/display
    only, e.g. 'biggest files first'), never a compliance-relevant fact, so the precision loss
    is acceptable and never surfaces as a wrong finding."""
    size_kb = row.get("size_kb")
    return {"id": row.get("drive_file_id"), "name": row.get("file"),
           "mimeType": row.get("mime"), "md5Checksum": row.get("checksum"),
           "createdTime": row.get("created_at"), "modifiedTime": row.get("source_modified"),
           "size": int(size_kb) * 1024 if size_kb is not None else None,
           "owners": [{"displayName": row["owner"]}] if row.get("owner") else [],
           "parents": [row["parent_folder"]] if row.get("parent_folder") else []}


def apply_drive_delta(prior_files: list[dict], changed_files: list[dict],
                      removed_ids) -> list[dict]:
    """Reconstruct 'the current known Drive estate' from `prior_files` (raw Drive-file-shaped
    dicts — see _drive_file_from_inventory_row) with `changed_files` (fresh raw Drive file
    resources from drive_changes_since) overlaid and `removed_ids` dropped. A changed id
    replaces its prior entry WHOLLY (fresh metadata wins, never merged field-by-field); anything
    not mentioned by the delta carries forward untouched, unlisted, unopened. A changed id with
    no prior entry is a genuinely new file. Pure and side-effect free."""
    removed_ids = set(removed_ids or ())
    changed_by_id = {f["id"]: f for f in changed_files if f.get("id")}
    seen: set[str] = set()
    out = []
    for f in prior_files:
        fid = f.get("id")
        if not fid or fid in removed_ids:
            continue
        seen.add(fid)
        out.append(changed_by_id.get(fid, f))
    for fid, f in changed_by_id.items():
        if fid not in seen and fid not in removed_ids:
            out.append(f)
    return out


def drive_reconstructed_listing(prior_files: list[dict], changed_files: list[dict],
                                removed_ids, *, max_files: int = 500,
                                exclude_remediated: bool = False,
                                scope_out: dict | None = None,
                                inventory_out: list | None = None) -> list[dict]:
    """A whole-Drive listing's worth of result, WITHOUT walking Drive — see apply_drive_delta for
    how the estate is reconstructed. `scope_out['reconstructed'] = True` is the only difference
    from a fresh _search_drive call's contract; everything else (kept/truncated/inventory) means
    exactly what it means for a live listing, since it is computed the same way, over the same
    shape of data."""
    merged = apply_drive_delta(prior_files, changed_files, removed_ids)
    by_id = {f["id"]: f for f in merged if f.get("id") and _is_scannable_mime(f)}
    inv_by_id = {f["id"]: f for f in merged if f.get("id")}
    return _finish_drive_listing(by_id, inv_by_id, raw_seen=len(merged), hit_cap=False,
                                 max_files=max_files, exclude_remediated=exclude_remediated,
                                 scope_out=scope_out, inventory_out=inventory_out,
                                 extra_scope={"reconstructed": True})


def _cancel_checkpoint() -> None:
    """Raise JobCancelledError if cancellation has been REQUESTED for the job running this scan.

    THREE STATES, deliberately distinguished — the queue and the UI mean different things by
    "stopped" and conflating them is what makes a Stop button lie:

      requested  `jobs.cancel_requested_at` is set. A user pressed Stop. Nothing has necessarily
                 noticed yet, and work is still running.
      observed   some thread's check_cancel() raised here. THIS work knows. Sibling folder
                 fetches may still be in flight, and the pool may still hold queued ones.
      stopped    no task belonging to this job can still run or write: the discovery pool has
                 been shut down with cancel_futures (queued tasks dropped) and joined (running
                 ones finished), and nothing was persisted after the observation.

    Only the caller that drains the pool may report `stopped`; a checkpoint can only ever report
    `observed`. See _search_folder's BFS, which is the one place that can make the promise.

    Lazy import because scanner is imported BY handlers, which is imported by worker — a
    module-level `import worker` is a cycle. Outside a worker turn (tests, scripts, the API
    process) check_cancel() is a no-op, so calling this anywhere is safe.
    """
    from worker import check_cancel
    check_cancel()


def _search_folder(svc, folder_id: str, max_files: int = 1000, exclude_remediated: bool = False,
                   scope_out: dict | None = None, inventory_out: list | None = None,
                   exclude_ids: set | None = None, raw_out: list | None = None,
                   progress_cb=None, include_subfolders: bool = True) -> list[dict]:
    """BFS over a folder subtree — returns all scannable files in the folder AND
    every nested subfolder. Bounded by max_files (newest folders may be skipped
    once the cap is hit) and a cycle guard, so a huge tree can't run unbounded.

    exclude_remediated: don't recurse into the configured Drive mirror folder
    (default 'Remediated', admin-configurable) — cheaper than tracking each file's
    parent-folder lineage, and sufficient since ACP only ever writes remediated
    output to that one well-known folder (handlers.ensure_remediated_folder).

    `exclude_ids` are folder ids whose subtrees are pruned: an "include this parent EXCEPT that
    child" selection (PRD §6.3). Pruned at the point of enqueue, so an excluded folder is never
    listed and neither are its descendants — "most specific path wins", and re-inclusion beneath
    an exclusion is deliberately not supported, so one check at the boundary is the whole rule.

    `scope_out`, when given, is filled in with WHAT THIS LISTING COVERED — see `_list`. This is
    the path that reported "1 document" on 2026-07-30 while the estate the user had in mind held
    eight: the folder held exactly one file, the listing was right, and NOTHING said the other
    seven were in a part of the Drive this scan never looked at.

    `raw_out`, when given, is extended with the raw (pre-normalize) Drive listing — the same
    shape `_search_drive` feeds `estate_inventory.summarize()`. `_search_folders` uses this to
    build ONE combined inventory over every root rather than merging per-root summaries, which
    `by_format`/`by_status` counts can't be recombined from without re-deriving them anyway."""
    remediated_folder_name = None
    if exclude_remediated:
        import core
        remediated_folder_name = core.store.get_drive_mirror_folder()
    excluded = set(exclude_ids or ())

    # Shared state — all mutations under `_lock`.
    _lock = threading.Lock()
    seen_folders: set[str] = {folder_id}
    raw: list[dict] = []
    _listed = [0]
    _skipped_acp = [0]
    _skipped_mirror = [0]
    _skipped_excluded = [0]
    _skipped_errors = [0]
    _skipped_rate_limited = [0]
    _truncated = [False]
    _last_progress_at = [0.0]
    # Folder-level activity (PRD "Live Discover Journey", folder-activity slice): which folders
    # are being fetched RIGHT NOW, and the last few that finished — bounded, ephemeral, and fed
    # into the SAME Redis job-state channel files_found/phase already flow through (no new
    # storage). This is deliberately NOT a durable, queryable history of every folder a scan ever
    # touched — that needs a real event store and is a separate, later piece of work. What this
    # answers honestly, and only this: "what is the BFS doing at this instant", which a single
    # aggregate total (files_found/folders_found) cannot.
    _folder_paths: dict[str, str] = {}
    # The lookup itself is a real Drive API call — spent only when something will actually see
    # the name (progress_cb is the only consumer of _active/_recent). A caller that wants files
    # and no live activity must not pay for a metadata read it will never look at, matching the
    # same rule scope_out's own folder-name lookup already follows elsewhere in this file. When
    # skipped, `folder_id` stands in as the path/name — never actually observed by anyone, since
    # nothing reads _active/_recent without a progress_cb to receive them.
    _root_name = (_folder_name(svc, folder_id) or "(scan root)") if progress_cb else folder_id
    _folder_paths[folder_id] = _root_name
    _active: dict[str, dict] = {}      # folder_id -> {name, path, started_at}
    _recent: list[dict] = []           # bounded to _RECENT_CAP, oldest dropped first
    _RECENT_CAP = 5

    def _record_folder_done(fid: str, state: str, files_found: int) -> None:
        """Move a folder from _active to _recent. MUST be called with `_lock` already held —
        this only touches the two dicts/list above, never the network or Drive API."""
        meta = _active.pop(fid, None)
        path = _folder_paths.get(fid, fid)
        name = (meta or {}).get("name") or path.rsplit("/", 1)[-1]
        _recent.append({"name": name, "path": path, "state": state, "files_found": files_found,
                        "completed_at": datetime.now(timezone.utc).isoformat()})
        if len(_recent) > _RECENT_CAP:
            del _recent[0]

    def _fetch_folder(fid: str) -> tuple[list[dict], list[tuple[str, str]], bool]:
        """Fetch all pages for one folder. Returns (raw_files, child_folders, capped) — each
        child folder as (id, name), the name needed to build its display path once it is
        actually enqueued (see the caller below).

        `capped` is True when pagination stopped because the cap was reached (meaning there are
        more pages we did not fetch) rather than because the server sent the last page naturally.
        The caller uses this to set the truncated flag even when the raw list fills exactly."""
        local_raw: list[dict] = []
        child_folders: list[tuple[str, str]] = []
        local_listed = local_skipped_acp = local_skipped_mirror = local_skipped_excluded = 0
        page_token = None
        capped = False
        while True:
            # BETWEEN PAGES, and before the first request. A folder with thousands of files is
            # many sequential round-trips, and without this the only cancellation opportunity in
            # the whole walk was between whole FOLDERS — so a Stop during one large folder waited
            # for every remaining page of it. Placed before the request rather than after, so a
            # cancelled scan stops SPENDING Drive quota immediately.
            _cancel_checkpoint()
            with _lock:
                if len(raw) + len(local_raw) >= max_files:
                    capped = True
                    break
            resp = svc.files().list(
                q=f"'{fid}' in parents and trashed=false",
                fields=f"nextPageToken,files({provenance.DRIVE_FIELDS})",
                pageSize=200,
                # Newest first, matching _list_drive_page_all. Without it Drive returns its own
                # default order, so when the max_files cap bites the files dropped are arbitrary
                # — and a file uploaded five minutes ago is as likely to be cut as one from last
                # year. That is the "I added files and the rescan didn't see them" symptom, and
                # it only appears on subtrees big enough to hit the cap, which is exactly where
                # nobody notices it. Discovery itself never caches; this is the other way a new
                # upload can go missing.
                orderBy="modifiedTime desc",
                pageToken=page_token,
                includeItemsFromAllDrives=True,
                supportsAllDrives=True,
            ).execute(num_retries=5)
            for f in resp.get("files", []):
                if f["mimeType"] == "application/vnd.google-apps.folder":
                    if not include_subfolders:
                        continue
                    # Folder-name exclusion is RETAINED, not replaced: copies written before
                    # the provenance stamp shipped carry no stamp, and this still skips them.
                    if exclude_remediated and f["name"] == remediated_folder_name:
                        local_skipped_mirror += 1
                        continue
                    # A user-excluded subtree. Pruned here rather than filtered afterwards so its
                    # descendants are never enqueued either — filtering the RESULT would still
                    # walk the subtree, spend the cap on files it then discards, and (worse) let
                    # an excluded branch push wanted files past the cap.
                    if f["id"] in excluded:
                        local_skipped_excluded += 1
                        continue
                    child_folders.append((f["id"], f["name"]))
                    continue
                local_listed += 1
                if estate_inventory.is_os_metadata(f.get("name", "") or ""):
                    continue  # OS metadata files (.DS_Store, Thumbs.db, …) — not user content
                if exclude_remediated and provenance.is_acp_generated(f):
                    local_skipped_acp += 1  # ACP's own output — never a source document
                else:
                    local_raw.append(f)
            page_token = resp.get("nextPageToken")
            if not page_token:
                break
        with _lock:
            _listed[0] += local_listed
            _skipped_acp[0] += local_skipped_acp
            _skipped_mirror[0] += local_skipped_mirror
            _skipped_excluded[0] += local_skipped_excluded
        return local_raw, child_folders, capped

    # Parallel BFS: up to _DISCOVERY_WORKERS Drive API calls in flight simultaneously.
    # Each completed folder's children are immediately submitted, so deeper levels start
    # as soon as any ancestor finishes — no level-barrier, no idle time between BFS levels.
    with _cf.ThreadPoolExecutor(max_workers=_DISCOVERY_WORKERS) as ex:
        # Bound so the listing threads carry the claiming job's context: without it
        # worker.check_cancel() is a silent no-op here and the joblog records name no job. This
        # is the DISCOVERY walk — the path a user's Stop button is most likely to be waiting on.
        # Binding does not by itself stop anything; it makes a checkpoint here capable of firing.
        import joblog as _jl_bind
        _active[folder_id] = {"name": _root_name, "path": _root_name,
                              "started_at": datetime.now(timezone.utc).isoformat()}
        pending: dict[_cf.Future, str] = {ex.submit(_jl_bind.bind(_fetch_folder), folder_id): folder_id}
        try:
            while pending:
                done, _ = _cf.wait(list(pending.keys()), return_when=_cf.FIRST_COMPLETED)
                for fut in done:
                    fid = pending.pop(fut)
                    try:
                        local_raw, child_folders, capped = fut.result()
                    except JobCancelledError:
                        # NOT a folder that failed. This clause used to be reached by cancellation
                        # too, and the result was that a Stop was recorded as "listing failed,
                        # skipping subtree" — one folder at a time, while the walk carried on to the
                        # next. Cancellation is a decision about the whole scan, so it leaves the
                        # per-folder error path entirely and is handled by the drain below.
                        raise
                    except Exception as _fetch_exc:  # noqa: BLE001 — one inaccessible folder must not abort the BFS
                        # Classified AFTER the fact, from an exception .execute()'s own internal
                        # retries already exhausted — this does not change what was retried or when,
                        # only whether the reason a subtree was skipped is visible to anyone.
                        rate_limited = _is_drive_rate_limit_error(_fetch_exc)
                        if rate_limited:
                            print(f"[scan] folder {fid}: Google Drive rate-limited this request "
                                  f"(exhausted retries) — skipping subtree", flush=True)
                        else:
                            print(f"[scan] folder {fid}: listing failed, skipping subtree", flush=True)
                        with _lock:
                            if rate_limited:
                                _skipped_rate_limited[0] += 1
                            _skipped_errors[0] += 1
                            _record_folder_done(fid, "rate_limited" if rate_limited else "failed", 0)
                        continue
                    with _lock:
                        space = max_files - len(raw)
                        if space > 0:
                            raw.extend(local_raw[:space])
                            if len(local_raw) > space:
                                _truncated[0] = True
                        elif local_raw:
                            _truncated[0] = True
                        if capped:
                            _truncated[0] = True
                        _record_folder_done(fid, "completed", len(local_raw))
                        if progress_cb:
                            _now = time.monotonic()
                            if _now - _last_progress_at[0] >= 2.0:
                                _last_progress_at[0] = _now
                                progress_cb(len(raw), len(seen_folders),
                                           active=list(_active.values()), recent=list(_recent))
                        for child_id, child_name in child_folders:
                            if child_id in seen_folders:
                                continue
                            seen_folders.add(child_id)
                            _folder_paths[child_id] = f"{_folder_paths.get(fid, _root_name)}/{child_name}"
                            if len(raw) < max_files:
                                _active[child_id] = {"name": child_name, "path": _folder_paths[child_id],
                                                     "started_at": datetime.now(timezone.utc).isoformat()}
                                pending[ex.submit(_jl_bind.bind(_fetch_folder), child_id)] = child_id
                            else:
                                _truncated[0] = True
        except JobCancelledError:
            # OBSERVED -> STOPPED. This is the only place in the walk that can promise "stopped",
            # so it is the only place that does the work to earn it.
            #
            # Be precise about what this adds, because the obvious justification is wrong and was
            # written here first: leaving the `with` block already joins RUNNING tasks
            # (ThreadPoolExecutor.__exit__ calls shutdown(wait=True)), and a QUEUED task that
            # starts afterwards hits _cancel_checkpoint() at the top of its page loop and raises
            # before issuing any Drive call. So neither "a cancelled scan keeps listing folders"
            # nor "results land after the user was told it stopped" is what cancel_futures
            # prevents. Deleting this block leaves every behavioural test in
            # test_discovery_cancellation.py green, which is how the overclaim was caught.
            #
            # What it actually prevents is a burst of DATABASE reads. Every queued task that runs
            # far enough to reach its checkpoint issues an is_job_cancelled() query, one per
            # folder still in the queue. Measured on a 60-folder tree: 61 cancellation-check
            # queries without this block, 9 with it. Those land precisely when a user has just
            # pressed Stop — which is often when they are reloading the queue view — so a
            # cancelled wide scan pushes ~50 extra queries into the moment the queue endpoints
            # are being read. Pinned by test_the_drain_stops_a_burst_of_cancellation_checks.
            #
            # wait=True is what lets the caller say no task can still be RUNNING; cancel_futures
            # is what stops the queued ones from running at all. The discover handler relies on
            # the first before recording the scan cancelled rather than failed.
            for _f in pending:
                _f.cancel()
            ex.shutdown(wait=True, cancel_futures=True)
            print(f"[scan] discovery cancelled — stopped after {len(seen_folders)} folders, "
                  f"{len(raw)} files listed; no partial listing was persisted", flush=True)
            raise

    # Unconditional final tick, bypassing the throttle above. Without it, the LAST folder(s)
    # processed can go unreported: `_cf.wait()`'s `done` set has no guaranteed order, so a
    # failing folder (whose branch never calls progress_cb — see above) processed after the last
    # successful one in the same batch would otherwise never appear in `recent` at all, not even
    # briefly. This guarantees the true end state is seen once, however fast the scan was.
    if progress_cb:
        progress_cb(len(raw), len(seen_folders), active=list(_active.values()), recent=list(_recent))

    truncated = _truncated[0]
    if truncated:
        print(f"[scan] folder listing hit the {max_files}-file cap — not all files were "
              f"scanned; raise ACP_FANOUT_MAX_FILES to cover the full subtree", flush=True)
    result = _normalize(raw[:max_files])
    # Inventory the NON-scannable files in this subtree (scannable ones are inventoried by the
    # caller from the analysis set). `raw` already excludes folders and ACP output.
    if inventory_out is not None:
        result_ids = {it.get("id") for it in result}
        for f in raw[:max_files]:
            if f.get("id") not in result_ids:
                inventory_out.append(_drive_inventory_row(f))
    if raw_out is not None:
        raw_out.extend(raw[:max_files])
    if scope_out is not None:
        # Parity with _search_drive: without this, a folder-scoped Drive scan (the common case —
        # "Specific folders", not "Entire Drive") never gets a `scope.inventory` summary at all,
        # so /assess/eligibility — which reads exactly that field — reported "0 documents will be
        # opened and scored" for every folder-scoped account regardless of estate size. Found live
        # 2026-08-21 minutes after #624 (which fixed a Discover-only scan's VISIBILITY) still left
        # this account's Assess tab empty: the scan was now visible, but had nothing in
        # scope.inventory to find, because this path never wrote it.
        scope_out.update({"kind": "folder", "folder_id": folder_id,
                          "include_subfolders": include_subfolders,
                          "folders_walked": len(seen_folders), "listed": _listed[0],
                          "skipped_acp": _skipped_acp[0], "skipped_mirror": _skipped_mirror[0],
                          "skipped_excluded": _skipped_excluded[0],
                          "skipped_errors": _skipped_errors[0],
                          "skipped_rate_limited": _skipped_rate_limited[0],
                          "kept": len(result), "truncated": truncated, "cap": max_files,
                          "inventory": estate_inventory.summarize(raw[:max_files], truncated=truncated)})
    _err_msg = f" · {_skipped_errors[0]} folder(s) inaccessible" if _skipped_errors[0] else ""
    _rl_msg = f" ({_skipped_rate_limited[0]} rate-limited)" if _skipped_rate_limited[0] else ""
    print(f"[scan] discovery (folder subtree): {len(seen_folders)} folder(s) walked · "
          f"{_listed[0]} listed · {_skipped_acp[0]} skipped as ACP-generated output · "
          f"{_skipped_mirror[0]} mirror folder(s) skipped · {len(result)} scannable{_err_msg}{_rl_msg}",
          flush=True)
    return result


def _search_folders(svc, folder_ids: list[str], max_files: int = 1000,
                    exclude_remediated: bool = False, scope_out: dict | None = None,
                    inventory_out: list | None = None,
                    exclude_ids: set | None = None, progress_cb=None,
                    include_subfolders: bool = True) -> list[dict]:
    """Walk SEVERAL folder subtrees and return their union.

    Scoping to one folder was never the real ask — an estate is "HR and Finance", not "HR". This
    keeps `_search_folder` as the single-root primitive and unions the results.

    TWO THINGS THIS HAS TO GET RIGHT, both of which produce a plausible wrong number rather than
    an error:

    1. DEDUPE BY ID ACROSS ROOTS. Picking a folder and something inside it is an ordinary
       selection, not a mistake, and Drive will then hand the same file back twice. Every source
       is responsible for yielding unique identities (see _sp_list) — without this, _dedupe_names
       renames the repeat to "Policy (1).docx", a phantom document that inflates the count, is
       downloaded and analysed twice, and shows up as "x2 copies".

    2. THE CAP IS SHARED, NOT PER ROOT. `max_files` bounds the whole listing; spending it per
       root would let four folders quietly list 4x the ceiling. The remaining budget shrinks as
       roots are walked, and truncation anywhere truncates the scan — a listing that stopped
       early in ONE subtree has still not seen the estate.
    """
    merged: list[dict] = []
    seen: set[str] = set()
    names: list[dict] = []
    truncated = False
    walked = listed = skipped_acp = skipped_mirror = skipped_excluded = 0
    # Cross-root union for the inventory summary — a SEPARATE dedup from `merged`'s, because this
    # one has to cover every file `_search_folder` saw (scannable and not), not just the analysis
    # set. Recomputed once over the union rather than merged per-root: by_format/by_status/by_age
    # counts and the sample caps in estate_inventory.summarize() can't be recombined from partial
    # summaries without re-deriving them anyway.
    raw_seen: set[str] = set()
    all_raw: list[dict] = []
    for fid in folder_ids:
        remaining = max_files - len(merged)
        if remaining <= 0:
            # Out of budget with roots still unwalked. That is truncation in the strict sense the
            # scope contract means: there are files we did not list.
            truncated = True
            break
        sub: dict = {}
        raw_batch: list = []
        batch = _search_folder(svc, fid, remaining, exclude_remediated=exclude_remediated,
                               scope_out=sub, inventory_out=inventory_out,
                               exclude_ids=exclude_ids, raw_out=raw_batch,
                               progress_cb=progress_cb, include_subfolders=include_subfolders)
        for it in batch:
            key = it.get("id") or it.get("path") or it.get("name")
            if key in seen:
                continue
            seen.add(key)
            merged.append(it)
        for f in raw_batch:
            key = f.get("id") or f.get("name")
            if key in raw_seen:
                continue
            raw_seen.add(key)
            all_raw.append(f)
        names.append({"id": fid, "name": _folder_name(svc, fid)})
        walked += int(sub.get("folders_walked") or 0)
        listed += int(sub.get("listed") or 0)
        skipped_acp += int(sub.get("skipped_acp") or 0)
        skipped_mirror += int(sub.get("skipped_mirror") or 0)
        skipped_excluded += int(sub.get("skipped_excluded") or 0)
        truncated = truncated or bool(sub.get("truncated"))
    if scope_out is not None:
        # `kind` stays "folder" for one root AND for many. isNarrowScope() keys off it, so a new
        # kind here would silently drop the ⚠ that stops a narrowed count reading as the estate —
        # the 2026-07-30 defect, re-introduced by a rename. `folders` carries the detail.
        #
        # `inventory`: parity with _search_drive/_search_folder — see _search_folder's comment on
        # why its absence here meant /assess/eligibility saw nothing for a multi-folder scan
        # (found live 2026-08-21).
        scope_out.update({"kind": "folder", "folder_id": folder_ids[0] if folder_ids else None,
                          "include_subfolders": include_subfolders,
                          "folders": names, "folders_walked": walked, "listed": listed,
                          "skipped_acp": skipped_acp, "skipped_mirror": skipped_mirror,
                          "skipped_excluded": skipped_excluded,
                          "kept": len(merged), "truncated": truncated, "cap": max_files,
                          "inventory": estate_inventory.summarize(all_raw, truncated=truncated)})
        if len(names) == 1:
            scope_out["folder_name"] = names[0]["name"]
    print(f"[scan] discovery ({len(folder_ids)} folder roots): {walked} folder(s) walked · "
          f"{listed} listed · {len(merged)} scannable after dedupe", flush=True)
    return merged


GRAPH = "https://graph.microsoft.com/v1.0"

# The driveItem fields the estate inventory needs — the source metadata columns (size, created /
# modified time, owner via createdBy, parent path) on top of the id/name/file/parentReference the
# scan always needed. Requesting them is free for the scannable set and populates the inventory
# rows for the rest; a Graph stub that omits any of them just yields None for that column.
# `shared` is the driveItem sharing facet (present when the item is shared, with a `scope` of
# anonymous | organization | users). Without it in the $select, Graph never returns it, so every
# `item.get("shared")` below is None and the estate drill-down's "shared" lens — the security-
# relevant one for a PHI estate — is dark for SharePoint while the Drive path (DRIVE_FIELDS carries
# `shared`) lights up. It is a list-level facet, so this costs no extra call.
_SP_ITEM_SELECT = "id,name,file,parentReference,size,createdDateTime,lastModifiedDateTime,createdBy,lastModifiedBy,shared"

# The driveItem properties Phase 2 wants that are NOT in the base select above, and that a tenant
# or a Graph version can refuse. Kept separate rather than appended, because `$select` is
# all-or-nothing: one unrecognised property 400s the entire page, and the page is the listing.
# The walk asks for these first and falls back (see _sp_children_url), so the worst case is
# metadata this scan does not carry — never a listing it does not return.
_SP_ITEM_SELECT_RICH = _SP_ITEM_SELECT + ",retentionLabel"

# The managed columns, the content type, the version and the check-out state all live on the
# item's backing LIST item, not on the driveItem. Expanded inline with the page so a library of
# 6,000 documents costs the same number of round trips it costs today — the alternative, and what
# the pre-Phase-2 content-type read actually did, is one extra Graph call per document.
_SP_LIST_EXPAND = "&$expand=listItem($expand=fields)"


def _sp_children_url(root: str, seg: str, tier: int) -> str:
    """The `/children` request for one folder page, at one fallback tier.

    THREE TIERS, tried in order, because the two risky asks fail independently and collapsing
    them would throw away a working one:

      0  rich select + list-item expansion — everything Phase 2 reads
      1  base select + list-item expansion — a tenant that refuses `retentionLabel` but keeps
         its managed columns, which is the ordinary case for a tenant with no retention plan
      2  base select alone — exactly the request that shipped before Phase 2

    Tier 2 is the floor and it is the pre-existing behaviour, which is the property that matters:
    a metadata feature nobody could test against a live tenant must not be able to break scanning
    itself. The old code bought that safety by never touching the listing call at all and paying
    one Graph call per document for a single field; this buys it with a retry, and gets the whole
    column bag for free when the tenant allows it.
    """
    if tier <= 0:
        return f"{root}/{seg}/children?$select={_SP_ITEM_SELECT_RICH},folder&$top=200{_SP_LIST_EXPAND}"
    if tier == 1:
        return f"{root}/{seg}/children?$select={_SP_ITEM_SELECT},folder&$top=200{_SP_LIST_EXPAND}"
    return f"{root}/{seg}/children?$select={_SP_ITEM_SELECT},folder&$top=200"


#: Why a field sourced from a tier this walk could not reach is unavailable. Phrased for the
#: operator reading an export cell, not for the developer reading the traceback: it names the ask
#: that was refused, so "no retention labels" and "we never got to ask" stay distinguishable.
_SP_TIER_REASON = {
    1: "Microsoft Graph refused the wider driveItem $select on this drive, so retention labels "
       "were not read (the listing itself is unaffected)",
    2: "Microsoft Graph refused the listItem expansion on this drive, so content types, managed "
       "columns, versions and check-out state were not read (the listing itself is unaffected)",
    3: "the listItem expansion is disabled for this deployment (ACP_SP_LIST_FIELDS=0)",
}


#: How many times a throttled or transiently-failed Graph GET is retried before it is allowed to
#: fail. Deliberately small: `_sp_get` is the single door EVERY SharePoint call goes through — the
#: walk, the delta feed, the site list, the drives list, the freshness replay — so a generous
#: retry budget here multiplies across an estate rather than adding to it. Four attempts covers
#: the throttle Graph actually applies; a fifth mostly buys a longer wait before the same answer.
def _sp_max_retries() -> int:
    try:
        n = int(os.environ.get("ACP_SP_MAX_RETRIES", "4") or 4)
    except ValueError:
        return 4
    return max(0, min(n, 10))


#: Counts every retry this process performed, per HTTP status. Read by the walk to record WHY a
#: scan was slow on the site report — "this took an hour" and "this took an hour because Graph
#: throttled us 240 times" are the same wall-clock and completely different problems, and the
#: second is the one an operator can act on (fewer sites per run, a different window, a support
#: case with Microsoft).
_SP_RETRIES: dict = {}
#: `d[k] = d.get(k, 0) + 1` is a read-modify-write, and with libraries walking on several threads
#: two of them can read the same value and both write back the same increment. A diagnostic that
#: undercounts throttling is a diagnostic that tells an operator their tenant is fine.
_SP_RETRIES_LOCK = threading.Lock()

#: Per-THREAD retry count, armed by whoever wants to attribute throttling to one unit of work.
#:
#: The global counter above cannot do that once libraries walk concurrently: a snapshot taken
#: around site A's turn also catches every retry that sites B and C spent in parallel, so the
#: number lands on whichever site happened to be in the loop — an operator chasing a throttled
#: library would be sent to the wrong one. `None` means nobody is counting on this thread, which
#: is the case for every caller that has not opted in.
_SP_RETRY_LOCAL = threading.local()


def _sp_note_retry(kind) -> None:
    """Record one retry, globally and (when armed) against the calling thread's own tally."""
    with _SP_RETRIES_LOCK:
        _SP_RETRIES[kind] = _SP_RETRIES.get(kind, 0) + 1
    n = getattr(_SP_RETRY_LOCAL, "n", None)
    if n is not None:
        _SP_RETRY_LOCAL.n = n + 1


def _sp_sleep(seconds: float) -> None:
    """The wait between Graph retries, as a named module attribute rather than a bare
    `time.sleep` call.

    A test suite must exercise the retry LOGIC — how many attempts, on which statuses, honouring
    which header — without spending the real seconds, and patching `time.sleep` globally would
    silence every other timing-dependent test in the process. One seam, patched in conftest, so
    the retries still happen in tests and simply take no time.
    """
    time.sleep(seconds)


def _sp_retry_delay(response, attempt: int) -> float:
    """How long to wait before retrying a throttled Graph call.

    `Retry-After` FIRST and always, when Graph sends one. It is the service telling us exactly
    how long it wants — guessing shorter earns another 429 and guessing longer wastes the scan's
    time, and on a tenant under load Graph's number is the only one that reflects the actual
    queue. Capped anyway, because a header is caller-controlled input and a scan must not be
    parked for an hour by one.

    Absent, capped exponential with FULL JITTER, the same shape worker._backoff_seconds uses.
    Jitter is not decoration here: a 30-site walk that hits a tenant-wide throttle would
    otherwise retry all of its libraries in lockstep, re-creating the burst that caused the
    throttle at exactly the moment the service asked for less.
    """
    header = None
    try:
        header = (getattr(response, "headers", None) or {}).get("Retry-After")
    except Exception:  # noqa: BLE001 — a malformed header must never fail the retry
        header = None
    if header:
        try:
            return max(0.0, min(120.0, float(str(header).strip())))
        except (TypeError, ValueError):
            pass                       # an HTTP-date form; fall through to the backoff below
    import random
    return random.uniform(0, min(60.0, 2.0 * (2 ** max(0, attempt - 1))))


def _sp_get(token: str, url: str, timeout: int = 30):
    """One Graph GET, with the permission failure translated into something actionable, and with
    the throttling every other connector in this codebase already handles.

    A 403 here is almost always a MISSING SCOPE rather than a missing document, and the raw
    Graph body says "Access denied" without naming what would fix it. Listing sites needs
    Sites.Read.All, which is admin-consent territory in most tenants — so the operator who sees
    this needs to be told which consent to go and get, not that something was denied.

    RETRIES 429 AND 5xx, AND NOTHING ELSE. Graph throttles hard, and until this every SharePoint
    call in this codebase — walk, delta, site list, drives list, freshness — failed on the first
    throttle, while the Drive path had `execute(num_retries=5)` from the start. On a 30-site
    estate that is not an edge case; it is the ordinary Friday afternoon.

    A 401/403 is NEVER retried, and that is the load-bearing exclusion. It is a scope problem,
    not a busy service: the answer will not change, Phase 1's per-site isolation reads the
    PermissionError to mark that site blocked with the consent that would fix it, and retrying
    would turn one fast correct diagnosis into four slow ones — delaying the only thing the
    operator can act on, and making a permissions failure look like a performance one.

    A 404 is not retried either. A deleted item is deleted.
    """
    import httpx
    attempts = _sp_max_retries()
    last_exc = None
    for attempt in range(1, attempts + 2):
        try:
            r = httpx.get(url, headers={"Authorization": f"Bearer {token}",
                                        "Accept": "application/json"},
                          timeout=timeout, follow_redirects=True)
        except Exception as e:  # noqa: BLE001 — a transport failure is the transient case too
            last_exc = e
            if attempt > attempts:
                raise
            _sp_note_retry("transport")
            _sp_sleep(_sp_retry_delay(None, attempt))
            continue
        if r.status_code in (401, 403):
            # WHOSE PROBLEM, not just that there is one. This used to say "grant Sites.Read.All
            # with tenant admin consent" for every refusal, including a 401 — where the token
            # itself was rejected and no consent can help — and including a 403 on a tenant where
            # the scope is already granted and the signed-in account simply is not a member of
            # that site. Both wrong answers point at an admin who cannot fix them, which costs a
            # pilot more than no answer would. sp_readiness reads the token's own claims to pick
            # between the explanations; see its docstring for why that is diagnostic only.
            #
            # NO `on_site` HERE, deliberately. This function is handed Graph-issued deltaLinks
            # whose target cannot be recovered from their shape, so it does not know whether the
            # refusal was about a site or a personal drive — and the owner verdict does not
            # depend on that anyway (it turns on what the token carries). An earlier version
            # passed the answer in as a keyword; ~29 test stubs replace `_sp_get` with
            # `lambda token, url:` and every one of them broke, with _sp_site_name's own swallow
            # turning the TypeError into a plausible `None` site name rather than an error.
            # Callers that genuinely know their target — the readiness endpoint and the discovery
            # preflight — pass `on_site` to diagnose_refusal directly.
            import sp_readiness
            message = (sp_readiness.diagnose_refusal(r.status_code, token=token)["message"]
                       + " URL: " + url.split("?")[0])
            if r.status_code == 401:
                raise SharePointSessionExpired(message)
            raise PermissionError(message)
        if (r.status_code == 429 or r.status_code >= 500) and attempt <= attempts:
            _sp_note_retry(r.status_code)
            delay = _sp_retry_delay(r, attempt)
            print(f"[scan] Microsoft Graph returned {r.status_code}; waiting {delay:.1f}s and "
                  f"retrying ({attempt}/{attempts}) — {url.split('?')[0]}", flush=True)
            _sp_sleep(delay)
            continue
        r.raise_for_status()
        return r.json()
    # Unreachable in practice: the loop either returns, raises, or exhausts its attempts on a
    # status that then raises through raise_for_status above. Kept so a future edit that changes
    # the loop cannot fall out of it silently returning None, which every caller would read as
    # an empty Graph response — a listing of nothing, reported as a small estate.
    raise last_exc or RuntimeError(f"Microsoft Graph request failed after {attempts} retries: {url}")


def _sp_sites(token: str, query: str = "", max_sites: int = 50) -> list[dict]:
    """SharePoint sites the signed-in user can see, newest Graph shape.

    `search=*` is Graph's own idiom for "everything I can see" — an empty `search=` returns
    nothing at all rather than everything, which reads as "this tenant has no sites" and is the
    single most confusing way this could fail.
    """
    q = query.strip() or "*"
    out: list[dict] = []
    url = f"{GRAPH}/sites?search={q}&$select=id,name,displayName,webUrl&$top=50"
    while url and len(out) < max_sites:
        data = _sp_get(token, url)
        for site in data.get("value", []):
            out.append({"id": site.get("id"),
                        "name": site.get("displayName") or site.get("name") or site.get("id"),
                        "url": site.get("webUrl")})
        url = data.get("@odata.nextLink")
    return out[:max_sites]


def _sp_drives(token: str, site_id: str) -> list[dict]:
    """The document libraries on a site. A team site routinely has several ("Documents",
    "Policies", …) and they are separate drives — scanning only the default would silently miss
    whole libraries, which looks exactly like an estate that is smaller than it is."""
    data = _sp_get(token, f"{GRAPH}/sites/{site_id}/drives?$select=id,name,webUrl")
    return [{"id": d.get("id"), "name": d.get("name"), "url": d.get("webUrl")}
            for d in data.get("value", []) if d.get("id")]


def _sp_folders(token: str, drive_id: str, item_id: str = "root") -> list[dict]:
    """Immediate subfolders of a driveItem — the Graph counterpart of routes/drive.py's /folders.

    A LOCATION HERE IS (drive, item), NEVER AN ITEM ALONE. Graph item ids are unique only WITHIN
    a drive, so an item id on its own does not identify a folder: two libraries can hand back the
    same id. _sp_list already carries driveId per file for exactly this reason — the download path
    once hardcoded /me/drive, which for a site-listed file would fetch the signed-in user's file of
    that id or 404. A folder picker that returned bare item ids would rebuild that bug in the
    scan's scope instead of its download.
    """
    seg = "root" if item_id in (None, "", "root") else f"items/{item_id}"
    url = (f"{GRAPH}/drives/{drive_id}/{seg}/children"
           "?$select=id,name,folder,parentReference&$top=200")
    out: list[dict] = []
    while url:
        data = _sp_get(token, url)
        for it in data.get("value", []):
            if it.get("folder") is None:
                continue                      # files are not pickable locations
            out.append({"id": it.get("id"), "name": it.get("name"), "drive_id": drive_id,
                        "child_count": (it.get("folder") or {}).get("childCount")})
        url = data.get("@odata.nextLink")
    return out


def _sp_item_exists(token: str, drive_id: str, item_id: str = "root") -> dict:
    """Does this ONE driveItem exist and is it readable — metadata only, no /children.

    The preflight existence check for `POST /discovery/preflight`: unlike `_sp_folders`, which
    lists (and paginates through) every child, this hits the item's own metadata endpoint —
    bounded to one request regardless of how large the folder is, which matters specifically for
    a check that runs before every scan start and must never approach a full enumeration.
    """
    seg = "root" if item_id in (None, "", "root") else f"items/{item_id}"
    try:
        data = _sp_get(token, f"{GRAPH}/drives/{drive_id}/{seg}?$select=id,name,folder")
        return {"exists": True, "name": data.get("name"), "is_folder": data.get("folder") is not None}
    except PermissionError as e:
        return {"exists": False, "error": str(e)}
    except Exception as e:
        return {"exists": False, "error": f"{e.__class__.__name__}: {e}"}


def _sp_default_drive(token: str, site: str | None = None) -> str | None:
    """The drive a picker starts in: a site's default library, or the user's OneDrive."""
    try:
        base = f"{GRAPH}/sites/{site}/drive" if site else f"{GRAPH}/me/drive"
        return (_sp_get(token, base + "?$select=id") or {}).get("id")
    except Exception:
        return None


def _sp_content_type(token: str, base: str, item_id: str) -> str | None:
    """The SharePoint Content Type name assigned to one item, or None.

    BEST-EFFORT, AND UNVERIFIED AGAINST A LIVE TENANT — flagged loudly rather than shipped quiet.
    `fields.ContentType` is the standard SharePoint column every list item carries (it is how
    CSOM/REST have always exposed the content type name), so this is the most likely-correct
    shape, but it has not been confirmed against a real Graph response, and Graph's exact
    behaviour for `$expand=fields($select=ContentType)` on a `listItem` under a driveItem can
    still surprise. Treat this function's return value as a hint until a real tenant confirms it,
    and see docs/sharepoint-gaps.md's read/write line — this is the "read native SharePoint
    metadata as a rule input" build it names, done for the one field that is safe to guess at.

    NEVER RAISES. This is deliberately a SEPARATE call from the listing walk, not folded into
    `_SP_ITEM_SELECT`/the paginated `/children` request: a malformed `$expand` on THAT call would
    fail the entire listing (`_sp_get` raises on any non-2xx), which would mean a classification
    feature I cannot test against production could break scanning itself. One extra Graph call
    per SCANNABLE item is the more expensive shape and the strictly safer one.
    """
    try:
        data = _sp_get(token, f"{base}/items/{item_id}/listItem?$expand=fields($select=ContentType)")
        ct = (data.get("fields") or {}).get("ContentType")
        return str(ct) if ct else None
    except Exception:      # noqa: BLE001 — a classification hint must never fail the scan
        return None


def _sp_permissions_budget() -> int:
    """How many per-document permissions reads one listing may make. Same shape and the same
    reason as _sp_content_type_budget: this is one Graph call per document, which is the cost
    tests/test_sp_scale.py exists to keep out of the walk."""
    try:
        return max(0, int(os.environ.get("ACP_SP_PERMISSIONS_MAX", "1000")))
    except (TypeError, ValueError):
        return 1000


def _sp_item_permissions(token: str, drive_id: str | None, item_id: str) -> list | None:
    """One item's permissions collection, or None when it could not be read.

    None and `[]` are different answers and the caller depends on it: `[]` is a successful read of
    an item nobody has been granted anything on, None is a read that did not happen. Collapsing
    them would report an unreadable item as having no collaborators — which archives it.
    """
    base = f"{GRAPH}/drives/{drive_id}" if drive_id else f"{GRAPH}/me/drive"
    try:
        data = _sp_get(token, f"{base}/items/{item_id}/permissions")
        value = data.get("value")
        return value if isinstance(value, list) else []
    except Exception:  # noqa: BLE001 — an archival hint must never fail the scan
        return None


def _sp_analytics_budget() -> int:
    """How many per-document activity reads one listing may make. Third of its kind
    (ACP_SP_CONTENT_TYPE_MAX, ACP_SP_PERMISSIONS_MAX) and for the same measured reason: one Graph
    call per document is what tests/test_sp_scale.py exists to keep out of a 30-site walk."""
    try:
        return max(0, int(os.environ.get("ACP_SP_ANALYTICS_MAX", "1000")))
    except (TypeError, ValueError):
        return 1000


def _sp_analytics_enabled() -> bool:
    """Whether to read each item's activity. OFF by default — one Graph call per document."""
    return os.environ.get("ACP_SP_ANALYTICS", "0").strip() == "1"


def _sp_item_analytics(token: str, drive_id: str | None, item_id: str) -> dict | None:
    """One item's activity over Graph's own seven-day window, or None when it could not be read.

    `/analytics/lastSevenDays` rather than `getActivitiesByInterval`: one call, no date maths, and
    the question here is "has anybody touched this at all lately", not a timeline.

    NONE AND AN EMPTY PAYLOAD ARE DIFFERENT ANSWERS, and the caller depends on it. Analytics is
    not served for every item or every tenant — a personal OneDrive, a library with reporting
    disabled, an item too new to have a rollup — and a 404 there means "we cannot ask", while an
    empty payload means "we asked and nobody touched it". Collapsing them reports an unmeasurable
    estate as an idle one, which is the reading that archives it.
    """
    base = f"{GRAPH}/drives/{drive_id}" if drive_id else f"{GRAPH}/me/drive"
    try:
        return _sp_get(token, f"{base}/items/{item_id}/analytics/lastSevenDays") or {}
    except Exception:  # noqa: BLE001 — an archival hint must never fail the scan
        return None


def _sp_enrich_analytics(token: str, files: list[dict]) -> dict:
    """Fill in `recent_actor_count` / `recent_action_count` from Graph's item analytics.

    ACCESS IS NOT USE. The collaborator fields say who CAN open a document; this says whether
    anybody HAS. A document twelve people can reach and nobody has opened since 2019 is the
    archival candidate, and no permissions read can tell you that.

    Off by default and budgeted when on, exactly like the permissions read beside it. What the
    budget running out costs is the field, not the scan: a document past it keeps
    `recent_actor_count` `unavailable` with a reason, so a rule keyed on activity skips it rather
    than reading a zero that was never measured.
    """
    if not _sp_analytics_enabled():
        return {"attempted": 0, "read": 0, "capped": False, "disabled": True}
    budget = _sp_analytics_budget()
    attempted = read = 0
    capped = False
    for rec in files:
        meta = rec.get("sp_metadata")
        item_id = rec.get("id")
        if not isinstance(meta, dict) or not item_id:
            continue
        if attempted >= budget:
            capped = True
            break
        attempted += 1
        blob = _sp_item_analytics(token, rec.get("driveId"), item_id)
        if blob is None:
            continue
        actors, actions = sp_metadata._analytics_counts(blob)
        fields = meta.get("fields") or {}
        fields["recent_actor_count"] = {"value": actors, "state": sp_metadata.PRESENT,
                                        "reason": None}
        fields["recent_action_count"] = {"value": actions, "state": sp_metadata.PRESENT,
                                         "reason": None}
        read += 1
    if capped:
        print(f"[scan] SharePoint activity read for the first {budget} document(s) "
              f"(ACP_SP_ANALYTICS_MAX); the rest report recent_actor_count as unavailable rather "
              f"than as zero, so an activity rule skips them instead of reading an idle count "
              f"that was never measured.", flush=True)
    return {"attempted": attempted, "read": read, "capped": capped}


def _sp_enrich_permissions(token: str, files: list[dict]) -> dict:
    """Upgrade `collaborator_count` from the authorship FLOOR to everyone with access.

    WIRING A SWITCH THAT WAS LYING. `sp_metadata.permissions_enabled()` has existed since Phase 2
    and had no caller: `ACP_SP_PERMISSIONS=1` did nothing, while the `permissions` field's own
    reason string told the operator to set it. A documented switch that does nothing is worse
    than an undocumented gap, because it ends the search for why the field is empty.

    OFF BY DEFAULT AND BUDGETED WHEN ON, for the reason tests/test_sp_scale.py measured: one
    Graph call per document is the difference between a scan and an outage on a 30-site estate.
    The budget is `ACP_SP_PERMISSIONS_MAX`, and what it buys when it runs out is a document that
    keeps the free authorship floor — a smaller number, honestly labelled — rather than nothing.

    BOTH HALVES ARE PATCHED. The count on the record AND the count inside `sp_metadata` move
    together, because the second is what the export and the rule evidence read. Updating only the
    first is the inconsistency `_sp_enrich_content_types` already has (it sets `content_type` on
    the record while the metadata blob still reports the field unavailable), and repeating it
    here would put two different collaborator counts on one document.
    """
    if not sp_metadata.permissions_enabled():
        return {"attempted": 0, "upgraded": 0, "capped": False, "disabled": True}
    budget = _sp_permissions_budget()
    attempted = upgraded = 0
    capped = False
    for rec in files:
        meta = rec.get("sp_metadata")
        if not isinstance(meta, dict):
            continue
        item_id = rec.get("id")
        if not item_id:
            continue
        if attempted >= budget:
            capped = True
            break
        attempted += 1
        perms = _sp_item_permissions(token, rec.get("driveId"), item_id)
        if perms is None:
            continue
        people = sp_metadata._permission_people(perms)
        fields = meta.get("fields") or {}
        fields["collaborator_count"] = {"value": len(people),
                                        "state": sp_metadata.PRESENT, "reason": None}
        fields["collaborator_basis"] = {"value": sp_metadata.BASIS_PERMISSIONS,
                                        "state": sp_metadata.PRESENT, "reason": None}
        fields["permissions"] = {"value": perms, "state": sp_metadata.PRESENT, "reason": None}
        upgraded += 1
    if capped:
        print(f"[scan] SharePoint permissions read for the first {budget} document(s) "
              f"(ACP_SP_PERMISSIONS_MAX); the rest keep the authorship floor for "
              f"collaborator_count, which is a smaller number and is labelled as one "
              f"(collaborator_basis=authorship).", flush=True)
    return {"attempted": attempted, "upgraded": upgraded, "capped": capped}


def _sp_content_type_budget() -> int:
    """How many per-document content-type calls one listing may make. Read at call time, never
    latched at import, for the same reason every other knob here is: a module-level read wins over
    an env var set afterwards, and the failure is silent. Floored at 0 so a nonsense value
    disables the fallback rather than crashing the scan it is an optimisation for."""
    try:
        return max(0, int(os.environ.get("ACP_SP_CONTENT_TYPE_MAX", "1000")))
    except (TypeError, ValueError):
        return 1000


def _sp_enrich_content_types(token: str, files: list[dict]) -> dict:
    """Best-effort content-type enrichment over the SCANNABLE set, in place.

    Bounded to `files` (the analysis set — docx/pptx/xlsx/pdf/html), not the whole raw estate: an
    estate is often mostly media the engine never opens, and a per-item Graph call for each of
    those would be real cost spent on files nobody classifies. The scannable set is already the
    set Assess is about to download, so the added cost is proportional to work already committed.

    A FALLBACK NOW, not the ordinary path. The walk reads the content type off the listing page
    it already fetches (see _sp_children_url's tiers), so this only runs for the items that came
    back without one — a tenant that refuses the listItem expansion, a personal OneDrive with no
    backing list, or a replayed delta row. An item that already has one is skipped, which is what
    keeps Phase 2 from being one extra Graph call per document on top of everything else.

    THREE-STRIKE CIRCUIT BREAKER, scoped to this one call. If a tenant does not support this shape
    at all — wrong Graph API version, a permission gap, a personal OneDrive with no backing list —
    every attempt fails the same way, and burning one call per remaining file for a guaranteed
    failure is pure waste. Disabled only for the REST of this listing; the next scan tries again,
    so a transient outage does not turn this off permanently. Gated by ACP_SP_CONTENT_TYPE=0 for
    an operator who wants it off without a code change, matching ACP_SP_ENUMERATE's precedent.

    AND A BUDGET, because the circuit breaker does not bound the case that actually costs. It
    trips on three consecutive FAILURES; a tenant that refuses the inline `$expand` on a
    collection but happily answers `/items/{id}/listItem` on a single resource succeeds every
    time, resets the counter every time, and pays one round trip per scannable document forever.
    Measured on a 30-site fixture: the walk cost 90 Graph calls whether the estate held 150
    documents or 1,500, and this function cost 150 and 1,500 — O(folders) beside O(documents), on
    the one path where the document count is the customer's, not ours. At the FANOUT_MAX_FILES
    default that is 50,000 serial round trips against a tenant that throttles, appended to a walk
    that took ninety.

    So: at most `ACP_SP_CONTENT_TYPE_MAX` (default 1,000) documents per listing. A single-library
    or small-tenant scan — where this fallback was designed and is genuinely useful — is
    unaffected, and a large estate degrades to a bounded sample instead of an outage.

    THE CAP IS REPORTED, not absorbed. Returns `{"attempted", "enriched", "skipped", "capped"}`
    and _sp_list puts it on the scan's scope. A document the budget did not reach keeps the
    `unavailable` state the walk already stamped on it (the tier reason), which stays true — but
    an operator seeing content types on 1,000 of 12,000 documents needs to know the other 11,000
    were not asked rather than not configured, and that the fix is the tenant's refusal of the
    inline expansion rather than a bigger budget.
    """
    if os.environ.get("ACP_SP_CONTENT_TYPE", "1").strip() == "0":
        return {"attempted": 0, "enriched": 0, "skipped": 0, "capped": False, "disabled": True}
    budget = _sp_content_type_budget()
    attempted = enriched = 0
    failures = 0
    capped = False
    for rec in files:
        if failures >= 3:
            break
        # ALREADY READ ON THE WALK — skip. This is the whole cost argument for Phase 2: the
        # listing page now carries the content type (sp_metadata reads the expanded listItem), so
        # the per-document call this function exists to make is a FALLBACK for the tenants that
        # refuse the expansion, not the ordinary path. Without this line the new read would be
        # pure addition: one Graph call per scannable document, for a value already in hand.
        if rec.get("content_type"):
            continue
        item_id = rec.get("id")
        if not item_id:
            continue
        if attempted >= budget:
            # Checked HERE, after the already-read skip, so the budget is spent only on documents
            # that would actually cost a round trip. Counting the skips against it would make the
            # cap bite on a tenant that answers the expansion and costs nothing at all.
            capped = True
            break
        drive_id = rec.get("driveId")
        base = f"{GRAPH}/drives/{drive_id}" if drive_id else f"{GRAPH}/me/drive"
        attempted += 1
        ct = _sp_content_type(token, base, item_id)
        if ct:
            rec["content_type"] = ct
            enriched += 1
            failures = 0
        else:
            failures += 1
    skipped = sum(1 for r in files if not r.get("content_type"))
    if capped:
        print(f"[scan] SharePoint content-type fallback stopped at {budget} documents "
              f"(ACP_SP_CONTENT_TYPE_MAX); {skipped} document(s) were not asked. This tenant "
              f"refuses the inline listItem expansion, so the walk cannot read the content type "
              f"off the listing page and each document costs its own Graph call — fixing the "
              f"expansion is the remedy, not a larger budget.", flush=True)
    return {"attempted": attempted, "enriched": enriched, "skipped": skipped, "capped": capped}


def _sp_walk_folder(token: str, drive_id: str, item_id: str, max_files: int,
                    exts: set[str], inventory_out: list | None = None,
                    exclude_ids: set | None = None, base: str | None = None,
                    skip_names: set | None = None, progress_cb=None,
                    root_label: str | None = None,
                    include_subfolders: bool = True) -> tuple[list[dict], bool]:
    """BFS one Graph folder subtree. Returns (raw driveItems, truncated).

    Recursion is server-side here for the same reason _search_folder does it for Drive: the
    picker hands back a folder and the user means "and everything under it". Making that an
    "include subfolders" toggle would offer a choice whose wrong answer silently under-reports.

    `base` is the Graph drive base to walk — `/drives/{id}` by default, but `/me/drive` for a
    whole-OneDrive walk, where there is no resolved drive id and the download path reads an absent
    `driveId` as /me/drive. Passing the base rather than resolving an id keeps that contract
    unchanged and costs no extra round trip.

    `skip_names` prunes folders BY NAME at enqueue — ACP's own archive and remediated-mirror
    folders. The caller filters those out afterwards by path anyway, so this changes no result;
    what it changes is the COST, and on a library saved back a few times the archive is the
    biggest subtree there is. Walking it spends the cap on items that are then discarded, which
    can push real documents past the cap and out of the estate.
    """
    excluded = set(exclude_ids or ())
    skip = set(skip_names or ())
    root = base or f"{GRAPH}/drives/{drive_id}"
    # Queue the human-readable path beside the Graph id. Graph's child response already includes
    # every folder name, so this adds no request; it merely retains information the walk used to
    # discard before the existing Discovery SSE channel could report it.
    queue = [(item_id, root_label or "SharePoint library")]
    seen: set[str] = set()
    raw: list[dict] = []
    truncated = False
    # The fallback tier this DRIVE settled on (see _sp_children_url). Held across the whole walk,
    # not per folder: a drive that refuses the expansion refuses it everywhere, and re-attempting
    # the wider ask on every folder of a deep library would spend a failed round trip per folder
    # to learn the same thing. Tier 3 is the operator's own opt-out and is never retried upward.
    tier = 0 if sp_metadata.expand_enabled() else 3
    while queue:
        cur, cur_path = queue.pop(0)
        if cur in seen:
            continue                          # a shortcut/cycle must not walk forever
        seen.add(cur)
        started_at = datetime.now(timezone.utc).isoformat()
        if progress_cb:
            progress_cb({"key": f"{drive_id or 'me'}/{cur or 'root'}", "name": cur_path.rsplit('/', 1)[-1],
                         "path": cur_path, "state": "scanning", "started_at": started_at})
        folder_files_from = len(raw)
        seg = "root" if cur in (None, "", "root") else f"items/{cur}"
        url = _sp_children_url(root, seg, tier)
        try:
            while url:
                if len(raw) >= max_files:
                    truncated = True
                    break
                # ONE STEP DOWN PER FAILURE, and the listing is never the thing that is lost. A
                # PermissionError is re-raised rather than demoted: that is a missing scope on the
                # drive itself, which the caller must see (site-level isolation reads it, Phase 1),
                # and stepping down would turn a 403 into a silently metadata-less success.
                while True:
                    try:
                        data = _sp_get(token, url)
                        break
                    except PermissionError:
                        raise
                    except Exception:             # noqa: BLE001 — a refused ASK, not a refused drive
                        if tier >= 2:
                            raise                 # the base request failed: that is a real failure
                        tier += 1
                        print(f"[scan] SharePoint drive {drive_id or 'me'}: {_SP_TIER_REASON[tier]}",
                              flush=True)
                        url = _sp_children_url(root, seg, tier)
                for it in data.get("value", []):
                    if it.get("folder") is not None:
                        if not include_subfolders:
                            continue
                        # Excluded subtree — pruned at enqueue, same rule as the Drive walker.
                        # Both `<driveId>/<itemId>` and a bare item id are accepted so a caller
                        # need not know which form reached it.
                        if (it.get("id") in excluded
                                or f"{drive_id}/{it.get('id')}" in excluded):
                            continue
                        if it.get("name") in skip:
                            continue
                        queue.append((it.get("id"), f"{cur_path}/{it.get('name') or 'unnamed folder'}"))
                        continue
                    it["_acp_drive_id"] = drive_id
                # The listItem CONTAINER's read outcome, stamped per item rather than inferred
                # later from whether a lookup came back empty — that inference is precisely the
                # bug sp_metadata exists to prevent, because it cannot tell a column the tenant
                # never set from a container nobody ever fetched.
                #
                # Note the three-way split: expansion refused for the drive (tier), expansion
                # granted but THIS item has no backing list item (a personal OneDrive file — the
                # key is absent from the payload), and a real list item.
                    if tier >= 2:
                        it["_acp_list_item_error"] = _SP_TIER_REASON[tier]
                    elif "listItem" in it:
                        it["_acp_list_item"] = it.get("listItem") or {}
                    else:
                        it["_acp_list_item_error"] = (
                            "this drive has no backing SharePoint list, so it carries no content "
                            "type, managed columns or version (a personal OneDrive)")
                    if tier >= 1:
                        it["_acp_rich_error"] = _SP_TIER_REASON[1]
                    raw.append(it)
                url = data.get("@odata.nextLink")
        except Exception as e:
            if progress_cb:
                progress_cb({"key": f"{drive_id or 'me'}/{cur or 'root'}",
                             "name": cur_path.rsplit('/', 1)[-1], "path": cur_path,
                             "state": "failed", "started_at": started_at,
                             "files_found": len(raw) - folder_files_from,
                             "scannable_found": sum(
                                 1 for item in raw[folder_files_from:]
                                 if Path(item.get("name") or "").suffix.lower() in exts),
                             "error": str(e)})
            raise
        if progress_cb:
            progress_cb({"key": f"{drive_id or 'me'}/{cur or 'root'}",
                         "name": cur_path.rsplit('/', 1)[-1], "path": cur_path,
                         "state": "partial" if truncated else "completed", "started_at": started_at,
                         "files_found": len(raw) - folder_files_from,
                         "scannable_found": sum(
                             1 for item in raw[folder_files_from:]
                             if Path(item.get("name") or "").suffix.lower() in exts)})
        if truncated:
            break
    return raw, truncated


def _sp_folder_name(token: str, drive_id: str, item_id: str) -> str | None:
    """A chosen folder's display name. Best-effort for the same reason _sp_site_name is: this
    exists only so the UI can NAME the boundary, and a scan must never fail because a label
    lookup did."""
    try:
        data = _sp_get(token, f"{GRAPH}/drives/{drive_id}/items/{item_id}?$select=name")
        return data.get("name") or None
    except Exception:
        return None


def _sp_site_name(token: str, site_id: str) -> str | None:
    """A site's display name, or None. The SharePoint counterpart of `_folder_name`, and
    best-effort for the same reason: this exists only so the UI can NAME the boundary it reports
    a count against, and a scan must never fail because a label lookup did.

    Swallows PermissionError too, not just transport errors. Listing sites needs Sites.Read.All,
    and a tenant can legitimately grant a token enough to READ a site's drives (which is what the
    scan then does successfully) while refusing the site metadata read — so a raising label lookup
    would fail scans that were about to work.
    """
    try:
        data = _sp_get(token, f"{GRAPH}/sites/{site_id}?$select=displayName,name")
        return data.get("displayName") or data.get("name") or None
    except Exception:
        return None


def _sp_scannable_exts() -> frozenset[str]:
    """The file extensions a SharePoint/OneDrive walk may keep, for the CURRENT format scope.

    Extension rather than MIME because that is what the Graph listing gives us to filter on
    before fetching anything — the connector-side filter the intake boundary depends on. Same
    read-per-call reasoning as `_scannable_mime` above."""
    return scan_formats.extensions()


def _sp_skip_folders(exclude_remediated: bool) -> set[str]:
    """The library-relative folder names a SharePoint/OneDrive listing must never enter —
    ACP's own output, so it is never re-ingested as a source document.

    ACP's own output must never be re-ingested: a remediated copy re-discovered inflates the
    file count and shows "remediated ✓" on a scan that remediated nothing (provenance.py).

    THIS IS THE WEAKER OF THE TWO DEFENCES AND THAT IS A KNOWN GAP. Drive stamps the ARTIFACT
    (properties.acpGenerated), which survives a rename or a move; provenance.py lists five ways
    folder-based exclusion breaks and rejects it for Drive on exactly those grounds. Graph has
    no equivalent of Drive's arbitrary `properties` — the nearest is a custom SharePoint column
    on the library's listItem, which needs Sites.Manage.All and per-library provisioning. So
    this ships folder-scoped, deliberately and with the limitation written down rather than
    implied: rename the mirror on one side only, move a remediated file out of it, or create a
    second folder of the same name, and re-ingestion comes back.

    The archive is skipped UNCONDITIONALLY, unlike the mirror, and the difference is not an
    oversight.

    The mirror holds ACP's OUTPUT, so excluding it is about not reporting "remediated ✓" on a
    scan that remediated nothing — a judgement an operator can reasonably switch off.
    SP_ARCHIVE_FOLDER holds displaced ORIGINALS: byte-for-byte copies of documents that still
    exist at their own paths, put there by ACP immediately before overwriting them. Counting
    one is counting the same document twice, and reporting its failures is reporting failures
    that the file at the real path no longer has. It also grows without bound — one more copy
    per save — so a library saved back a few times reads as an estate that is mostly broken.

    There is no scan for which including them is the right answer, so there is no flag."""
    skip_folders = {SP_ARCHIVE_FOLDER}
    if exclude_remediated:
        try:
            import core
            mirror = core.store.get_drive_mirror_folder()
        except Exception:      # noqa: BLE001 — no store (tests, tooling): fall back to the default
            mirror = "Remediated"
        if mirror:
            skip_folders.add(mirror)
    return skip_folders


def _sp_metadata_from_carried(carried: dict, *, site_id: str | None, site_name: str | None,
                              library_name: str | None) -> dict:
    """Rebuild a metadata record from the columns a prior scan stored (see _SP_CARRIED_METADATA).

    A stored value becomes `present` — it was read from SharePoint, and a delta that does not
    mention the item is what makes it still current. A stored NULL becomes `not_configured`
    rather than `unavailable`, and that is the one judgement worth stating: the earlier run DID
    read the container, and wrote NULL because there was nothing in it. Calling it `unavailable`
    now would invent a read failure that never happened, and would make an estate look
    progressively less readable the longer delta sync ran.

    The availability map the prior run recorded is preserved where it exists, so a field that was
    genuinely unread THEN still reads as unread now — the carry-forward must not launder a gap
    into an answer.
    """
    import json as _json
    blob = {}
    raw = carried.get("sp_metadata")
    if raw:
        try:
            parsed = _json.loads(raw) if isinstance(raw, str) else raw
            if isinstance(parsed, dict):
                blob = parsed
        except Exception:  # noqa: BLE001 — a stale blob must not fail a reconstruction
            blob = {}
    prior_states = blob.get("availability") or {}
    prior_reasons = blob.get("reasons") or {}

    def _field(name, value, *, applicable=True):
        if not applicable:
            return {"value": None, "state": sp_metadata.NOT_APPLICABLE, "reason": None}
        was = prior_states.get(name)
        if value is None and was in (sp_metadata.UNAVAILABLE, sp_metadata.NOT_APPLICABLE):
            return {"value": None, "state": was, "reason": prior_reasons.get(name)}
        if value is None or value == "" or value == {} or value == []:
            return {"value": None, "state": sp_metadata.NOT_CONFIGURED, "reason": None}
        return {"value": value, "state": sp_metadata.PRESENT, "reason": None}

    on_a_site = bool(site_id or carried.get("site_id"))
    fields = {
        "site_id": _field("site_id", site_id or carried.get("site_id"), applicable=on_a_site),
        "site_name": _field("site_name", site_name or carried.get("site_name"),
                            applicable=on_a_site),
        "library_name": _field("library_name", library_name or carried.get("library_name"),
                               applicable=on_a_site),
        "content_type": _field("content_type", carried.get("content_type")),
        "managed_columns": _field("managed_columns", blob.get("managed_columns") or None),
        "retention_label": _field("retention_label", carried.get("retention_label")),
        "sensitivity_label": _field("sensitivity_label", carried.get("sensitivity_label")),
        "compliance_tag": _field("compliance_tag", None),
        "is_record": _field("is_record", None),
        "created_by": _field("created_by", None),
        "modified_by": _field("modified_by", carried.get("modified_by")),
        "sharing_scope": _field("sharing_scope", carried.get("sharing_scope")),
        "permissions": _field("permissions", None),
        "version": _field("version", carried.get("sp_version")),
        "checked_out_by": _field("checked_out_by", carried.get("checked_out_by")),
        "item_kind": _field("item_kind", carried.get("item_kind")),
    }
    return {"fields": fields}


def _sp_item_metadata(item: dict, *, site_id: str | None, site_name: str | None,
                      library_name: str | None) -> dict:
    """One item's SharePoint-native metadata, with the CONTAINERS built from what the walk
    recorded rather than inferred from whether a lookup came back empty.

    That inference is the whole bug sp_metadata exists to prevent: `fields.get("ContentType")`
    returning None cannot tell a library with no content types from an expansion Graph refused,
    and the two call for opposite responses. `_sp_walk_folder` stamps which happened; this reads
    the stamp.

    An item with NEITHER stamp was not produced by that walk — sp_reconstructed_listing replays
    rows from a prior scan's inventory, which stores the metadata but not the raw Graph payload.
    Its container is missing, with that as the reason, so a delta-reconstructed row says "not
    re-read this run" instead of claiming the tenant configures nothing.
    """
    # A REPLAYED item whose metadata was read on an earlier run. Rebuilt from the stored record
    # rather than reported unread, because "unread" would be false: these values WERE read from
    # SharePoint, and the delta feed not mentioning the item is what says they are still current.
    #
    # Reporting them `unavailable` here would be the erasure this carry-forward exists to stop,
    # wearing an honest-looking label: every unchanged file would show "Not read" for a retention
    # label ACP holds, and every rule keyed on it would stop matching on the first delta sync.
    if "_acp_sp_carried" in item:
        return _sp_metadata_from_carried(item["_acp_sp_carried"], site_id=site_id,
                                         site_name=site_name, library_name=library_name)
    if "_acp_list_item" in item:
        li = sp_metadata.Container(item["_acp_list_item"])
    else:
        li = sp_metadata.Container.missing(
            item.get("_acp_list_item_error")
            or "replayed from a prior scan's inventory — the list item was not re-read this run")
    rich = (sp_metadata.Container.missing(item["_acp_rich_error"]) if "_acp_rich_error" in item
            else sp_metadata.Container(item))
    return sp_metadata.normalize(item, list_item=li, rich=rich, site_id=site_id,
                                 site_name=site_name, library_name=library_name)


def _sp_classify_item(item: dict, *, drive_id: str | None, skip_folders: set[str],
                      exts: set[str], site_id: str | None = None,
                      site_name: str | None = None,
                      library_name: str | None = None) -> dict | None:
    """Classify ONE Graph driveItem KNOWN to be a file (the caller has already checked
    "file" in item and deduped it by (drive_id, id)) — skip-folder + OS-metadata exclusion,
    then split into the whole-estate triage row, and either a scannable record or a
    non-scannable inventory row. Shared VERBATIM by _sp_list's live paging loop and
    sp_reconstructed_listing's replay over a merged prior+changed set, so a reconstruction
    classifies each item byte-identically to a fresh listing — not a close approximation of
    one. Returns None for an item neither listing should count (in a skip-folder, or OS
    metadata like .DS_Store/Thumbs.db).

    `site_id`/`site_name`/`library_name` are the walk's knowledge of WHERE this item was found,
    stamped onto both output shapes. They default to None so sp_reconstructed_listing — which
    replays items from a prior inventory and has no walk to ask — classifies exactly as it did.

    Both shapes also carry `sp_metadata`: the SharePoint-native metadata record (content type,
    managed columns, retention label, version, check-out state, page-vs-document) WITH per-field
    availability. Built for the non-scannable half too, deliberately — an estate report that can
    say which retention label covers a video is worth as much to a records manager as one about
    a Word document, and the metadata costs nothing extra there (it rides the same page).

    Return shape: {"est_row": ..., "scannable": {...} or None, "inventory_row": {...} or None}
    — est_row is built for every kept file (scannable or not), the SharePoint analogue of
    _search_drive's own est_files entries; exactly one of scannable/inventory_row is set,
    matching a supported extension or not.
    """
    # /me/drive is a request alias, not an absence of provider identity. Graph
    # returns the actual driveId on each item even when this walk has no explicit
    # library target. Preserve that identity for freshness checks and delivery.
    parent_drive = (item.get("parentReference") or {}).get("driveId")
    if drive_id and parent_drive and drive_id != parent_drive:
        raise ValueError("SharePoint item drive differs from its selected source library")
    drive_id = drive_id or parent_drive
    name = item.get("name", "")
    # parentReference.path looks like "/drive/root:/Remediated/sub". Matched as PATH SEGMENTS,
    # not substrings: a library called "Remediated Policies" is a different folder and must
    # still be scanned.
    parent = (item.get("parentReference") or {}).get("path", "")
    segments = parent.split(":", 1)[-1].strip("/").split("/")
    if skip_folders.intersection(segments):
        return None
    # OS metadata files (.DS_Store, Thumbs.db, …) are synced by cloud agents but are not user
    # documents. Skip before the estate row so they do not appear in counts.
    if estate_inventory.is_os_metadata(name):
        return None
    # The estate row for EVERY file (scannable or not), classified the same way the Drive
    # inventory is. It also carries the triage fields estate_inventory._sample_meta reads off a
    # Drive file object (owners[] / size / shared / modifiedTime), mapped from the Graph item's
    # own field names — without them the estate drill-down's owner / biggest-first / shared
    # lenses (the ones the Drive path populates) come back blank for SharePoint.
    cb = (item.get("createdBy") or {}).get("user") or {}
    lb = (item.get("lastModifiedBy") or {}).get("user") or {}
    owner = (cb.get("displayName") or cb.get("email")
             or lb.get("displayName") or lb.get("email"))
    meta = _sp_item_metadata(item, site_id=site_id, site_name=site_name,
                             library_name=library_name)
    est_row = {"id": item.get("id"), "name": name,
              "mimeType": (item.get("file") or {}).get("mimeType"),
              "owners": ([{"displayName": owner}] if owner else []),
              "size": item.get("size"), "shared": item.get("shared"),
              "modifiedTime": item.get("lastModifiedDateTime")}
    scannable = inventory_row = None
    if Path(name).suffix.lower() in exts:
        fmeta = item.get("file") or {}
        scannable = {"name": _safe_name(name), "id": item.get("id"), "sp": True,
                    # Source metadata for the inventory row (see _scan_discover). None-safe:
                    # a Graph stub without these fields just yields None.
                    "source_mime": fmeta.get("mimeType"),
                    # quickXorHash — OneDrive/SharePoint's own content hash, the Graph analogue
                    # of Drive's md5Checksum (_normalize, above). It rides along on the SAME
                    # `file` facet _SP_ITEM_SELECT already asks for (Graph returns a facet
                    # whole-or-not-at-all; there is no narrower sub-select for one of its
                    # properties), so no extra field or round trip is needed to read it. This is
                    # the ONE thing every checksum-gated reuse path downstream reads off
                    # item.get("checksum") and has always found None for SharePoint — ADR 0011
                    # cross-scan analysis reuse, within-scan dedup, the ADR 0020 source-bytes
                    # cache, and the ADR 0003 document identity layer. Populating it here is the
                    # only change any of them needed.
                    "checksum": (fmeta.get("hashes") or {}).get("quickXorHash"),
                    "size_kb": _inv_size_kb(item.get("size")),
                    "created_at": item.get("createdDateTime"),
                    "source_modified": item.get("lastModifiedDateTime"),
                    "owner": owner,
                    "parent_folder": (item.get("parentReference") or {}).get("path")}
        if drive_id:
            scannable["driveId"] = drive_id
        # Carried on the scannable record too, not only the inventory row: the scannable set is
        # what _scan_discover inventories for ANALYSED files, so omitting it here would give a
        # site id to every media file and none to the documents the assessment is about.
        if site_id:
            scannable["siteId"] = site_id
        if library_name:
            scannable["libraryName"] = library_name
        if site_name:
            scannable["siteName"] = site_name
        scannable["sp_metadata"] = meta
        # A Content Type carried forward from a prior scan's inventory
        # (_sp_file_from_inventory_row). Live Graph items never carry this key, so a fresh
        # listing classifies byte-identically to before — the property this function's
        # docstring is built on — and _sp_enrich_content_types, which only ever runs on a live
        # listing, is the sole writer of the same field there.
        #
        # The LIVE content type now arrives on the walk itself (sp_metadata reads the expanded
        # listItem), so this replay path and the walk both populate the same field from their own
        # honest source, and the per-item enrichment call below is only the fallback.
        ct_now = (meta["fields"]["content_type"]["value"]
                  if meta["fields"]["content_type"]["state"] == sp_metadata.PRESENT else None)
        if ct_now:
            scannable["content_type"] = ct_now
        elif item.get("_acp_content_type"):
            scannable["content_type"] = item["_acp_content_type"]
    else:
        inventory_row = _sp_inventory_row(item, site_id=site_id, library_name=library_name,
                                          site_name=site_name, meta=meta)
        if drive_id:
            inventory_row["drive_id"] = drive_id
    return {"est_row": est_row, "scannable": scannable, "inventory_row": inventory_row}


# The most SharePoint sites one scan may span. An estate assessment routinely covers a few dozen
# team sites, so the picker lets an operator select several — and the walk below is bounded here
# rather than by however many the tenant happens to have. 30 sites x every library on each is
# already a long Graph traversal; an unbounded selection is a scan that does not finish against a
# large tenant.
#
# SITES PAST THE CAP ARE DROPPED AND THE LISTING IS MARKED TRUNCATED. That distinction is the
# whole point of the cap being here: the single-site version kept the FIRST bare root and ignored
# every other one with no signal at all (see _sp_locations), so selecting five sites reported one
# site's documents as the estate. A floor that says it is a floor is a usable answer; a floor
# presented as a total is not.
def _sp_max_sites() -> int:
    """Read at call time, not import time, so a deployment can raise the ceiling with an env var
    and a test can lower it without reloading the module."""
    try:
        n = int(os.environ.get("ACP_SP_MAX_SITES", "30") or 30)
    except ValueError:
        return 30
    return n if n > 0 else 30


def _sp_cap_sites(sites: list[str]) -> tuple[list[str], list[str]]:
    """(kept, dropped) for a selected site list. One function, called by both _list (so the
    recorded scope names exactly the sites that were walked) and _sp_list (so a direct caller
    cannot exceed the cap either) — two independent caps would drift, and the scope blob would
    then disagree with the listing it describes."""
    cap = _sp_max_sites()
    return list(sites[:cap]), list(sites[cap:])


def _sp_concurrency() -> int:
    """How many document libraries may be walked at once.

    ONE BY DEFAULT — serial, byte-identical to the walk that shipped before this, and that is a
    deliberate refusal rather than caution. Overlapping the walks means dispatching a library
    BEFORE the budget that decides whether its result is wanted has been spent, so a scan with a
    tight cap walks libraries whose documents are then discarded: real Graph calls, against a
    customer's tenant, for an estate that is already a floor. Phase 1 rejected exactly that when
    it chose to walk libraries lazily rather than up front, and gaining wall-clock by overriding
    it is the operator's trade to make, not one to make silently on their behalf.

    Raise it when the cap is comfortably above the estate — the ordinary case for a scheduled
    30-site run — where nothing is ever discarded and the whole win is free. The look-ahead is
    bounded to this number, so at 4 at most three libraries are ever walked speculatively.

    1 also takes the identical code path, so anything that reproduces at 1 is not a concurrency
    problem.
    """
    try:
        n = int(os.environ.get("ACP_SP_CONCURRENCY", "1") or 1)
    except ValueError:
        return 1
    return max(1, min(n, 16))


class _SpWalkPipeline:
    """Walks libraries ahead of the consumer, by at most `concurrency - 1` of them.

    A SLIDING WINDOW, not a full dispatch. Submitting every library up front would walk all
    thirty of a truncated estate to use the first three — the cost Phase 1 avoided by walking
    lazily — so the window advances only as the consumer asks for results, and never further
    ahead than the concurrency allows.

    At concurrency 1 this submits exactly the unit being consumed and waits for it: the same
    Graph calls, in the same order, as the serial walk. That is what makes the setting a genuine
    diagnostic — a failure that survives ACP_SP_CONCURRENCY=1 is not about concurrency.
    """

    def __init__(self, units, run, concurrency):
        self._units = list(units)
        self._at = {u: i for i, u in enumerate(self._units)}
        self._run = run
        self._conc = max(1, concurrency)
        self._futures: dict = {}
        self._pool = (_cf.ThreadPoolExecutor(max_workers=min(self._conc, len(self._units)),
                                             thread_name_prefix="sp-walk")
                      if self._units and self._conc > 1 else None)

    def result(self, unit):
        """This library's (items, truncated), walking up to `concurrency - 1` later ones in
        parallel. Raises whatever the walk raised — the caller's per-library isolation is what
        turns that into a partial site, and swallowing it here would hide a broken library as an
        empty one."""
        import joblog as _jl_bind
        i = self._at.get(unit)
        if i is None:                      # not a walk unit (delta-reconstructed, or search mode)
            raise KeyError(unit)
        if self._pool is None:
            return self._run(unit)         # serial: no thread, no look-ahead, no window
        for j in range(i, min(i + self._conc, len(self._units))):
            u = self._units[j]
            if u not in self._futures:
                # BOUND, like every other thread start on the scan path. contextvars do not
                # cross ThreadPoolExecutor.submit, so an unbound walk thread starts from an empty
                # context: check_cancel() cannot see the job it belongs to, and every joblog
                # record a library walk emits loses its job identity. A Stop pressed during a
                # thirty-site walk would then be honoured by the consumer and ignored by the
                # walkers still in flight. tests/test_cancel_crosses_threads.py guards the whole
                # scan path for exactly this and caught it in CI.
                self._futures[u] = self._pool.submit(_jl_bind.bind(self._run), u)
        return self._futures[unit].result()

    def close(self):
        """Drop the walks still in flight for libraries the budget never reached. Cancelled
        rather than waited on: their results are already destined to be discarded, and holding
        the scan open for them would spend the wall-clock this exists to save."""
        if self._pool is None:
            return
        for f in self._futures.values():
            f.cancel()
        self._pool.shutdown(wait=False)


def _sp_walk_pipeline(token, site_ids, resolved, site_report, delta_plan, use_search,
                      max_files, exts, exclude_ids, skip_folders, folder_progress_cb=None):
    """The pipeline over every library this scan will actually WALK, in the operator's own order.

    Only walk-eligible libraries: one the delta plan can reconstruct costs no Graph call at all,
    and one listed through the search index is a lazy generator the consumer pages itself.

    Every walk is given `max_files` — the whole budget, as an UPPER BOUND, never a share. A share
    would reintroduce the per-site budget this connector deliberately does not have; the real cap
    is enforced on the scannable set by the consumer, in site order.

    The one visible consequence, stated rather than hidden: the library where the budget runs out
    may walk a page or two further than a `budget`-capped walk would have. Its extra items land
    in the estate DENOMINATOR (they are discovered, and reported as such) and never in the
    analysis set, so the effect is a slightly more complete floor for one library — and one
    consistent code path at every concurrency, which is what makes ACP_SP_CONCURRENCY=1 a real
    diagnostic rather than a different program.
    """
    units = []
    for s_id in site_ids:
        for d in (resolved.get(s_id) or []):
            if use_search:
                continue
            if (delta_plan or {}).get("delta", {}).get(d["id"]) is not None:
                continue
            units.append((s_id, d["id"]))

    throttled: dict = {}
    library_names = {(s_id, d["id"]): (d.get("name") or d["id"])
                     for s_id in site_ids for d in (resolved.get(s_id) or [])}

    def run(unit):
        # Armed per walk, so the retries THIS library spent are attributable to it however many
        # other libraries are in flight beside it.
        _SP_RETRY_LOCAL.n = 0
        try:
            def _folder_progress(event):
                if folder_progress_cb:
                    folder_progress_cb({**event, "site_id": unit[0], "drive_id": unit[1],
                                        "library_name": library_names.get(unit)})
            return _sp_walk_folder(token, unit[1], "root", max_files, exts,
                                   inventory_out=None, exclude_ids=exclude_ids,
                                   skip_names=skip_folders, progress_cb=_folder_progress,
                                   root_label=library_names.get(unit))
        finally:
            throttled[unit] = getattr(_SP_RETRY_LOCAL, "n", 0)
            _SP_RETRY_LOCAL.n = None

    pipeline = _SpWalkPipeline(units, run, _sp_concurrency())
    pipeline.throttled = throttled
    return pipeline


def _sp_list(token: str, max_files: int = 200, site: str | None = None,
             exclude_remediated: bool = False, inventory_out: list | None = None,
             scope_out: dict | None = None,
             locations: list[tuple[str, str]] | None = None,
             exclude_ids: set | None = None,
             sites: list[str] | None = None,
             progress_cb=None,
             delta_plan: dict | None = None,
             site_done_cb=None,
             skip_sites: set | dict | None = None,
             include_subfolders: bool = True) -> list[dict]:
    """List scannable files from OneDrive, or from every document library on a SharePoint site.

    The RETURN value is the scannable analysis set (the six supported extensions) — unchanged, so
    the SEARCH/analysis scope stays exactly as it was. `inventory_out`, when given, is additionally
    filled with an inventory row for every NON-scannable item the API returned (media / unsupported
    / extensionless), so Discover can report the whole estate while Assess still only opens
    supported types.

    `site` is a Graph site id. Absent, this behaves exactly as it always has (the signed-in
    user's OneDrive), so an existing scan is unchanged by this function growing a second mode.

    `sites` is the MULTI-site form and is what an estate-wide run passes: every document library
    on every listed site, walked into one listing under ONE shared budget. `site` remains the
    one-site spelling and is folded into `sites` here, so a caller that has never heard of the
    plural form is unaffected. The two are not different modes — there is one loop, and a single
    site is a list of one.

    `site_done_cb(site_id, files, inventory_rows)` fires as each SITE's libraries finish being
    consumed, with just that site's share of the listing. It exists so a caller can persist a
    site's estate the moment it is known instead of at the end of a thirty-site walk — a scan
    that dies at site 28 otherwise loses all twenty-eight. The scanner does not know what the
    caller does with it and deliberately does not: this module owns no store.

    `skip_sites` are sites a caller has ALREADY persisted and does not want walked again. They
    are dropped from the walk and reported `complete` on the scope, so a resumed run's per-site
    breakdown still names all thirty rather than the two it happened to finish. A SET names them;
    a DICT of {site_id: {"listed", "estate", "name"}} additionally supplies what the caller
    already knows about each. The dict form is what a resumable scan should pass: the exit gate
    is auditable per-site totals, and a site reported `complete` beside a zero it did not walk is
    a wrong number rather than a missing one — the reader has no way to tell it apart from a site
    that really did hold nothing.

    `delta_plan` is Phase 3's per-LIBRARY incremental plan (core.sp_multi_sync_plan):
    `{"delta": {drive_id: {...}}, "full": {drive_id: "why"}}`. A library in `delta` is
    RECONSTRUCTED from its stored cursor and prior inventory instead of walked; every other
    library is walked exactly as before, in the same loop, against the same budget. That hybrid
    is the point: a 30-site estate has no single answer to "can this scan skip walking?" — one
    library's cursor is fresh, another's expired last week, a third has never been synced — and
    collapsing it to one yes/no means either walking everything because one library needs it, or
    reconstructing everything and quietly serving a stale estate for the one that did not.

    WHY ONE SHARED BUDGET rather than max_files per site. `max_files` bounds the work a scan is
    allowed to do; per-site budgets would multiply it by the number of sites selected, so a
    30-site run would quietly cost 30x its cap against a customer's tenant. The consequence is
    that a large first site can exhaust the budget before a later one is reached — which is
    TRUNCATION, and is recorded as such (hit_cap), never returned as though the estate ended
    there.

    EVERY ITEM CARRIES ITS driveId, and that is not cosmetic. Graph item ids are unique only
    WITHIN a drive, so two libraries can legitimately hand back the same id — and the download
    path used to hardcode /me/drive, which would have fetched the signed-in user's file of that
    id, or 404ed, for anything listed from a site. Listing without recording the drive is how
    you get a scan that reports a site's documents and analyses somebody's OneDrive.

    Identity dedup, same rule as _normalize's for Drive: a drive-item id IS the document,
    and Graph's paged /search can return the same item on more than one page. Without this,
    _dedupe_names (which every source funnels through) would rename the repeat to
    "Deck (1).pptx" — a phantom document that inflates the file count, gets downloaded and
    analysed a second time, and surfaces in the UI as "x2 copies". Each source is responsible
    for yielding unique IDENTITIES; _dedupe_names only disambiguates genuine NAME collisions
    between different items."""
    exts = _sp_scannable_exts()
    skip_folders = _sp_skip_folders(exclude_remediated)
    # One list, order preserved, duplicates collapsed: selecting the same site twice must not
    # walk it twice (every item would be deduped by `seen` anyway, but the Graph calls would be
    # spent and the budget consumed before that check ever runs).
    site_ids: list[str] = []
    for _s in ([*(sites or []), *([site] if site else [])]):
        if _s and _s not in site_ids:
            site_ids.append(_s)
    site_ids, _over_cap = _sp_cap_sites(site_ids)
    # Sites a previous attempt already listed and persisted. Removed from the WALK, kept on the
    # REPORT: a resumed run whose breakdown named only the sites it happened to finish would read
    # as a two-site scan of a thirty-site estate, which is the under-report this connector's
    # whole design is against.
    _skipped = (dict(skip_sites) if isinstance(skip_sites, dict)
                else {s_id: {} for s_id in (skip_sites or ())})
    _resumed = [s_id for s_id in site_ids if s_id in _skipped]
    site_ids = [s_id for s_id in site_ids if s_id not in _skipped]
    files: list[dict] = []
    # Keyed by (drive, item) — an item id is unique only within its drive, so a bare id would
    # collapse two genuinely different documents from two libraries into one.
    seen: set[tuple[str | None, str]] = set()
    relisted = 0
    # Every FILE item (scannable or not) as a classify input, so SharePoint reports the same
    # three-denominator estate summary _search_drive builds for Drive — the parity gap this closes.
    # `hit_cap` is set ONLY when the scannable cap stops paging with items still unlisted (a
    # remaining nextLink or an unvisited library); a single page fully lists its estate even when the
    # analysis set exceeds the cap, so that is NOT truncation.
    est_files: list[dict] = []
    hit_cap = False

    # A target is (drive_id, an iterable of item BATCHES). Both modes below feed the identical
    # per-item processing beneath — dedupe, mirror-folder skip, inventory row, scannable filter.
    # Folder narrowing that re-implemented any of that would drift from the whole-drive path in
    # exactly the places (ACP output re-ingestion, driveId stamping) the comments here warn about.
    def _pages(url: str):
        while url:
            data = _sp_get(token, url)
            yield data.get("value", [])
            url = data.get("@odata.nextLink")

    # ── HOW THE ESTATE IS ENUMERATED, and why this is not `search` any more ─────────────────────
    #
    # Every branch below now WALKS `/children` from a root. It used to walk only for chosen
    # folders and use `root/search(q='')` for a whole drive or a whole site, and that difference
    # was the single largest correctness bug in discovery.
    #
    # `search(q='')` reads Microsoft's SEARCH INDEX, which is eventually consistent. It returns
    # what has been indexed, with no error and no signal when that is less than what exists.
    # Issue #333 measured it on prod: 178 files uploaded, 39 discovered, presented as a complete
    # inventory; the same query returned 157 five minutes later and 158 after fifteen. A partial
    # listing is indistinguishable from a genuinely small estate, and EVERYTHING downstream —
    # assessment coverage, the reconciliation, the compliance assertion — is a fraction of that
    # number. Discovery's entire value is completeness, so a fast wrong answer is the one outcome
    # it cannot ship.
    #
    # `/children` is a directory traversal against live metadata: immediately consistent, and it
    # returns what is actually there. That is what the folder-scoped path has always used, which
    # is why "pick the folders" was the workaround for an estate the index was under-reporting.
    # The workaround is now the behaviour.
    #
    # THE COST IS REAL AND IS ACCEPTED. Search is one paged call per drive; a walk is one call per
    # FOLDER, so a deep estate costs many more round trips and takes longer. Correctness wins:
    # the cap and `truncated` already bound the work and say when a listing stopped short, and a
    # slow complete answer can be waited for while a fast partial one cannot be detected.
    #
    # ACP_SP_ENUMERATE=search restores the old path without a code change, for an operator who
    # hits a wall on a very large estate and knowingly accepts an index-backed listing. It is not
    # the default and it is not silent — the scan logs which mode produced the inventory, so a
    # count can always be attributed to the method that produced it.
    use_search = os.environ.get("ACP_SP_ENUMERATE", "walk").strip().lower() == "search"
    # A TARGET is one document library's worth of work:
    # (drive_id, batches, site_id, site_name, library_name).
    # The last two are the WALK's knowledge of where these items were found — a Graph driveItem
    # names its drive and nothing else, so an item that travelled alone could never be attributed
    # to the site an operator selected. Carried here rather than looked up later because a scan
    # now spans a SET of sites and the run's scope can no longer answer it per document.
    targets: list[tuple] = []
    # Per SITE: what was covered, what it cost, and whether it finished. The exit gate for a
    # multi-site run is "no site silently omitted", and a total with no per-site breakdown cannot
    # show that — a missing site and a site that legitimately held nothing produce the same
    # number. Keyed by site id, in selection order.
    site_report: dict[str, dict] = {s_id: {"id": s_id, "libraries": [], "listed": 0, "estate": 0,
                                           "status": "queued", "error": None}
                                    for s_id in site_ids}
    # Bounded folder telemetry shared by concurrent library walkers. It records only active
    # folders and the four most recent outcomes; scan scope remains the durable site/library
    # audit record rather than growing one row per folder in a large estate.
    _sp_folder_lock = threading.Lock()
    _sp_active_folders: dict[str, dict] = {}
    _sp_recent_folders: list[dict] = []
    _sp_folders_seen: set[str] = set()
    # Folder events arrive before their library batch is consumed into `files`. Counting only
    # `files` therefore leaves the headline at zero while the activity stream visibly discovers
    # documents. This provisional tally feeds telemetry only; the durable estate still uses the
    # identity-deduped consumption path below.
    _sp_folder_scannable: dict[str, int] = {}
    _sp_last_folder_emit = [0.0]
    # Sites a previous ATTEMPT of this scan already listed. On the report, complete, with the
    # counts the caller supplied — see `skip_sites` above for why the counts and not just the ids.
    for s_id in _resumed:
        _known = _skipped.get(s_id) or {}
        site_report[s_id] = {"id": s_id,
                             "libraries": list(_known.get("libraries") or []),
                             "listed": int(_known.get("listed") or 0),
                             "estate": int(_known.get("estate") or 0),
                             "name": _known.get("name"),
                             "status": "complete", "error": None, "resumed": True}
    # Sites the CAP refused are in the report too, as skipped-with-a-reason. The exit gate is "no
    # site silently omitted"; a site dropped before the loop and absent from the breakdown is
    # exactly the omission the gate is about, and the count on its own does not name it.
    for s_id in _over_cap:
        site_report[s_id] = {"id": s_id, "libraries": [], "listed": 0, "estate": 0,
                             "status": "skipped",
                             "error": f"over the {_sp_max_sites()}-site limit for one scan"}

    def _tick():
        """Per-site progress out to the job/SSE stream. Emitted as each SITE resolves rather than
        per file: a thirty-site walk is otherwise a single silent bar, and "which site is it on"
        is the question an operator watching a long estate scan actually has."""
        if progress_cb:
            with _sp_folder_lock:
                _active = [dict(v) for v in _sp_active_folders.values()]
                _recent = [dict(v) for v in _sp_recent_folders]
                _folder_count = len(_sp_folders_seen) or None
                _files_found = max(len(files), sum(_sp_folder_scannable.values()))
            try:
                progress_cb(_files_found, folders=_folder_count, active=_active or None,
                            recent=_recent or None,
                            sites=[dict(v) for v in site_report.values()])
            except TypeError:
                try:
                    progress_cb(len(files), sites=[dict(v) for v in site_report.values()])
                except TypeError:
                    # An older callback that accepts only the count still works. A progress
                    # diagnostic must never be the thing that fails a scan.
                    progress_cb(len(files))

    def _folder_tick(event):
        key = event.get("key") or event.get("path")
        now = time.monotonic()
        with _sp_folder_lock:
            if key:
                _sp_folders_seen.add(key)
            if event.get("state") == "scanning":
                _sp_active_folders[key] = dict(event)
            else:
                _sp_active_folders.pop(key, None)
                if key:
                    _sp_folder_scannable[key] = int(event.get("scannable_found") or 0)
                _sp_recent_folders.insert(0, dict(event))
                del _sp_recent_folders[4:]
            # Match the existing Drive walker: update state for every folder, but bound database
            # and SSE writes during a wide concurrent traversal. Failures bypass the throttle.
            emit = (now - _sp_last_folder_emit[0] >= 2.0 or event.get("state") == "failed")
            if emit:
                _sp_last_folder_emit[0] = now
        if emit:
            _tick()

    if locations:
        # Chosen folders. Each location is (drive_id, item_id) — never a bare item id, because a
        # Graph item id is unique only within its drive (see _sp_folders).
        #
        # FOLDERS BEAT SITES when a request carries both, which is the product's own reading
        # (frontend/src/scanScope.js says the same about the label): the folders are the tighter
        # boundary and the later answer. What changes here is that the sites are SAID to have been
        # dropped rather than silently ignored — the pre-multi-site code took this branch and
        # never mentioned the site at all, which is the same shape as the defect this whole change
        # is about, one level down. The pickers never produce this mix today; an API caller can.
        for s_id in site_report:
            site_report[s_id].update(
                status="skipped",
                error="narrowed to the selected folders — this site's libraries were not walked")
        if site_report:
            hit_cap = True
        for drive_id, item_id in locations:
            walked, cut = _sp_walk_folder(token, drive_id, item_id, max_files, exts,
                                          inventory_out=None, exclude_ids=exclude_ids,
                                          skip_names=skip_folders,
                                          progress_cb=lambda event, d=drive_id:
                                              _folder_tick({**event, "drive_id": d}),
                                          root_label="Selected SharePoint folder",
                                          include_subfolders=include_subfolders)
            hit_cap = hit_cap or cut
            targets.append((drive_id, iter([walked]), None, None, None))
    elif site_ids or _resumed:
        # `or _resumed` because a resume can legitimately have NOTHING left to walk: the previous
        # attempt finished the final site and died before it could record that the listing was
        # over. Without it `site_ids` is empty, the chain falls through to the OneDrive branch at
        # the bottom, and the signed-in user's personal drive is returned as a thirty-site
        # SharePoint estate — silent, plausible, and wrong about every document in it.
        if _over_cap:
            # Sites the cap refused. Named in the log rather than counted, because "which site did
            # you not read?" is the operator's next question and an id is what answers it.
            print(f"[scan] {len(site_ids) + len(_over_cap)} SharePoint sites selected; scanning "
                  f"the first {len(site_ids)} (ACP_SP_MAX_SITES). Not listed: "
                  f"{', '.join(_over_cap)} — the estate below is a floor, not a total.", flush=True)
            hit_cap = True
        if use_search:
            print(f"[scan] {len(site_ids)} SharePoint site(s) listed via the SEARCH INDEX "
                  f"(ACP_SP_ENUMERATE=search) — recent changes may be missing", flush=True)
        # ONE BUDGET across every selected site. max_files bounds the work a scan may do; a
        # per-site budget would multiply it by the number of sites chosen, so a 30-site run would
        # quietly cost 30x its cap against a customer's tenant. The consequence — a large early
        # site can exhaust it before a later one is reached — is truncation, and is recorded as
        # such per site rather than left to look like an empty site.
        # ── PASS 1: resolve each site's libraries, and start walking them ────────────────────
        #
        # Split out from the consumption loop below ONLY so the walks can overlap. A 30-site
        # estate spends nearly all of its wall-clock inside _sp_walk_folder, one library at a
        # time, waiting on Graph — and Graph is perfectly happy to serve several at once.
        #
        # THE ESTATE MUST NOT DEPEND ON WHICH WALK FINISHES FIRST. That is the trap concurrency
        # sets here: the budget is shared, so with results consumed as they arrive, WHICH sites
        # get skipped when it runs out becomes whichever library happened to return first, and
        # the same tenant scanned twice reports a different estate. A count an operator cannot
        # reproduce is worse than a slow scan.
        #
        # So: dispatch concurrently, CONSUME IN SELECTION ORDER. Each walk is given `max_files`
        # as an upper bound (never a share of the budget), the loop below consumes results in the
        # order the operator chose their sites, and the budget is spent there. A library that
        # walked more than the budget left for it is truncated at consumption — to exactly the
        # items the serial walk would have kept, because the walk order is deterministic. The
        # only cost is work done and discarded, and only when the budget runs out mid-flight.
        resolved: dict = {}
        for s_id in site_ids:
            rep = site_report[s_id]
            # ONE SITE'S FAILURE IS NOT THE SCAN'S. A tenant of thirty sites reliably contains one
            # the token has lost access to, one mid-migration, and one that throttles; letting any
            # of them raise discards every site already walked and returns nothing. The failure is
            # recorded against the site that had it — with the message, because "Sites.Read.All"
            # and "429" are different problems with different owners — the listing is marked a
            # floor, and the walk carries on.
            try:
                resolved[s_id] = _sp_drives(token, s_id)
            except PermissionError as e:
                rep.update(status="blocked", error=str(e))
                print(f"[scan] SharePoint site {s_id} unreadable: {e} — continuing with the "
                      f"remaining sites; this estate is a floor", flush=True)
                hit_cap = True
                _tick()
            except Exception as e:  # noqa: BLE001 — a Graph/transport failure is one site's, not the run's
                rep.update(status="blocked", error=f"Microsoft Graph error: {e}")
                print(f"[scan] SharePoint site {s_id} failed to list its libraries: {e} — "
                      f"continuing with the remaining sites; this estate is a floor", flush=True)
                hit_cap = True
                _tick()

        _walks = _sp_walk_pipeline(
            token, site_ids, resolved, site_report, delta_plan, use_search,
            max_files, exts, exclude_ids, skip_folders, folder_progress_cb=_folder_tick)

        # ── PASS 2: consume, in the operator's own order ─────────────────────────────────────
        budget = max_files
        for s_id in site_ids:
            rep = site_report[s_id]
            if rep["status"] == "blocked":
                continue                        # pass 1 already recorded it and ticked
            if budget <= 0 and not use_search:
                rep["status"] = "skipped"       # never reached → the estate is a floor
                hit_cap = True
                continue
            drives = resolved.get(s_id) or []
            if not drives:
                print(f"[scan] SharePoint site {s_id} has no document libraries visible to "
                      f"this token — nothing to scan", flush=True)
                rep["status"] = "complete"
                _tick()
                continue
            rep["status"] = "scanning"
            # Resolved HERE, once per site, and reused by _list rather than looked up a second
            # time. The name is not decoration any more: sp_metadata records it as a field on
            # every document, so a report can say "Finance" where it used to say
            # "contoso.sharepoint.com,<guid>,<guid>" — and a per-DOCUMENT lookup for that would
            # be one Graph call per file. Best-effort, like _sp_site_name always was: a tenant
            # can let a token read a site's drives while refusing its metadata.
            rep["name"] = _sp_site_name(token, s_id)
            rep["libraries"] = [{"id": d["id"], "name": d.get("name"), "status": "queued",
                                  "listed": 0, "estate": 0} for d in drives]
            lib_by_id = {lib["id"]: lib for lib in rep["libraries"]}
            # Announce the site before waiting on its first library.  Previously the first live
            # row for a healthy site was often its terminal "complete" row, which made a long
            # Graph walk look queued until it suddenly finished.
            _tick()
            for d in drives:
                # RECONSTRUCTED, not walked. The merged prior+delta set becomes this library's
                # one batch and feeds the identical per-item loop below — same dedupe, same
                # classification, same inventory split — so a reconstructed library is
                # indistinguishable downstream from a walked one, which is what makes the
                # estate-wide totals comparable across the two modes.
                plan = (delta_plan or {}).get("delta", {}).get(d["id"])
                if plan is not None and not use_search:
                    merged = apply_sp_delta(plan["prior_files"], plan["changed"],
                                            plan["removed_ids"])
                    lib_by_id[d["id"]]["mode"] = "delta"
                    lib_by_id[d["id"]]["changed"] = len(plan["changed"])
                    lib_by_id[d["id"]]["removed"] = len(plan["removed_ids"])
                    lib_by_id[d["id"]]["status"] = "scanning"
                    rep["active_library"] = {"id": d["id"], "name": d.get("name"),
                                             "mode": "delta"}
                    _tick()
                    budget -= len(merged)
                    targets.append((d["id"], iter([merged]), s_id, rep["name"], d.get("name")))
                    continue
                lib_by_id[d["id"]]["mode"] = "full"
                lib_by_id[d["id"]]["status"] = "scanning"
                rep["active_library"] = {"id": d["id"], "name": d.get("name"),
                                         "mode": "search" if use_search else "full"}
                _tick()
                why = (delta_plan or {}).get("full", {}).get(d["id"])
                if why:
                    lib_by_id[d["id"]]["full_reason"] = why
                if use_search:
                    targets.append((d["id"], _pages(f"{GRAPH}/drives/{d['id']}/root/search(q='')"
                                                    f"?$select={_SP_ITEM_SELECT}&$top=200"),
                                    s_id, rep["name"], d.get("name")))
                    continue
                if budget <= 0:
                    rep["status"] = "partial"   # libraries left unlisted → the estate is a floor
                    lib_by_id[d["id"]]["status"] = "skipped"
                    lib_by_id[d["id"]]["error"] = "file budget exhausted before this library"
                    hit_cap = True
                    break
                try:
                    walked, cut = _walks.result((s_id, d["id"]))
                except Exception as e:  # noqa: BLE001 — same isolation, one library down
                    rep.update(status="partial", error=f"library {d.get('name') or d['id']}: {e}")
                    lib_by_id[d["id"]].update(status="blocked", error=str(e))
                    print(f"[scan] SharePoint library {d.get('name') or d['id']} on site {s_id} "
                          f"failed: {e} — continuing", flush=True)
                    hit_cap = True
                    continue
                hit_cap = hit_cap or cut
                if cut:
                    rep["status"] = "partial"
                    lib_by_id[d["id"]]["status"] = "partial"
                # NOT truncated here, and that is deliberate. `budget` counts raw items as a
                # cheap proxy for deciding whether to walk the NEXT library; the real cap is
                # enforced on the SCANNABLE set by the consumption loop below (`len(files) >=
                # max_files`), which is what `max_files` has always meant. Trimming the raw list
                # to `budget` would shrink the estate DENOMINATOR — the count of everything
                # discovered, media included — and report a smaller estate than the scan actually
                # saw, which is the one number this connector must never understate.
                budget -= len(walked)
                targets.append((d["id"], iter([walked]), s_id, rep["name"], d.get("name")))
            # WHY this site was slow, not just that it was. "This took an hour" and "this took an
            # hour because Graph throttled us 240 times" are the same wall-clock and completely
            # different problems, and only the second is one an operator can act on — fewer sites
            # per run, a different window, a support case with Microsoft.
            #
            # Summed from each LIBRARY's own tally rather than snapshotted around this site's turn
            # in the loop: with libraries walking concurrently a snapshot also catches whatever
            # other sites spent in parallel, and would send an operator chasing a throttled
            # library to the wrong site.
            _throttled = sum(n for (site_of, _drive), n in _walks.throttled.items()
                             if site_of == s_id)
            for (site_of, drive_of), count in _walks.throttled.items():
                if site_of == s_id and drive_of in lib_by_id and count:
                    lib_by_id[drive_of]["throttled"] = count
            if _throttled:
                rep["throttled"] = _throttled
            # The walk may be complete, but its batches have not been classified and counted yet.
            # Keep the site live until the consumption boundary below proves all of its targets
            # were consumed.  "Complete" is an outcome, not shorthand for "Graph returned".
            _tick()
        _walks.close()
    elif use_search:
        print("[scan] OneDrive listed via the SEARCH INDEX (ACP_SP_ENUMERATE=search) — "
              "recent changes may be missing", flush=True)
        targets = [(None, _pages(f"{GRAPH}/me/drive/root/search(q='')"
                                 f"?$select={_SP_ITEM_SELECT}&$top=200"), None, None, None)]
    elif (delta_plan or {}).get("delta", {}).get(None) is not None:
        # The signed-in user's whole OneDrive, reconstructed. Keyed by None because that is the
        # drive id a OneDrive listing carries throughout (see _sp_base) — the same key the
        # cursor and the prior inventory partition use, so the three cannot drift.
        _p = delta_plan["delta"][None]
        targets = [(None, iter([apply_sp_delta(_p["prior_files"], _p["changed"],
                                               _p["removed_ids"])]), None, None, None)]
    else:
        # `/me/drive` as the BASE, and `drive_id` stays None on purpose. Resolving the real id
        # would be an extra round trip and would change what is stored on every item; the download
        # path reads an absent `driveId` as /me/drive, which is correct there and only there, and
        # is the shape every item listed before site support already has.
        walked, cut = _sp_walk_folder(token, None, "root", max_files, exts,
                                      inventory_out=None, exclude_ids=exclude_ids,
                                      base=f"{GRAPH}/me/drive", skip_names=skip_folders,
                                      progress_cb=_folder_tick, root_label="OneDrive")
        hit_cap = hit_cap or cut
        targets = [(None, iter([walked]), None, None, None)]

    # ── PER-SITE EMISSION ────────────────────────────────────────────────────────────────────
    #
    # Targets are appended in the operator's own site order and consumed in it, so one site's
    # targets are CONTIGUOUS here. A site is therefore finished the moment the next site's first
    # target comes up, and its share of the listing is the slice of `files` / `inventory_out`
    # added since that boundary — bookkeeping over two indices, not a second data structure that
    # could disagree with the one this function returns.
    #
    # ONLY A SITE THE REPORT CALLS `complete` IS EMITTED. `site_done_cb` is what a caller
    # checkpoints against, and a caller that persists a site and then skips it on resume must
    # never be handed a site the cap cut in half — the resumed run would report the truncated
    # half as the whole library and never look again. Partial is not a small kind of complete.
    _emitted: set = set()

    def _emit_site(s_id, files_from: int, inv_from: int):
        if not site_done_cb or not s_id or s_id in _emitted:
            return
        if (site_report.get(s_id) or {}).get("status") != "complete":
            return
        _emitted.add(s_id)
        try:
            site_done_cb(s_id, files[files_from:],
                         inventory_out[inv_from:] if inventory_out is not None else [])
        except Exception as e:  # noqa: BLE001 — a checkpoint is an optimisation, never the scan
            print(f"[scan] SharePoint site {s_id} checkpoint failed: {e} — the scan continues "
                  f"and this site will be re-listed if it is retried", flush=True)

    cur_site, cur_drive, cur_files, cur_inv = None, None, 0, 0

    def _finish_consumed_library(s_id, drive_id):
        if not s_id or not drive_id:
            return
        rep = site_report.get(s_id) or {}
        lib = next((row for row in (rep.get("libraries") or []) if row.get("id") == drive_id), None)
        if lib and lib.get("status") == "scanning":
            lib["status"] = "complete"

    def _finish_consumed_site(s_id):
        if not s_id:
            return
        rep = site_report.get(s_id) or {}
        rep.pop("active_library", None)
        if rep.get("status") == "scanning":
            rep["status"] = "complete"
        _tick()

    for i, (drive_id, pages, target_site, target_site_name, library_name) in enumerate(targets):
        if drive_id != cur_drive or target_site != cur_site:
            _finish_consumed_library(cur_site, cur_drive)
        if target_site != cur_site:
            _finish_consumed_site(cur_site)
            _emit_site(cur_site, cur_files, cur_inv)
            cur_site = target_site
            cur_files = len(files)
            cur_inv = len(inventory_out) if inventory_out is not None else 0
        cur_drive = drive_id
        if target_site and target_site in site_report:
            rep = site_report[target_site]
            lib = next((row for row in (rep.get("libraries") or [])
                        if row.get("id") == drive_id), {})
            if lib.get("status") not in {"partial", "blocked", "skipped"}:
                lib["status"] = "scanning"
            rep["active_library"] = {"id": drive_id, "name": library_name,
                                     "mode": lib.get("mode")}
            _tick()
        for batch in pages:
            if len(files) >= max_files:
                break
            for item in batch:
                if "file" not in item:
                    continue
                item_id = item.get("id")
                if item_id is not None:
                    key = (drive_id, item_id)
                    if key in seen:
                        relisted += 1
                        continue
                    seen.add(key)
                classified = _sp_classify_item(item, drive_id=drive_id,
                                               skip_folders=skip_folders, exts=exts,
                                               site_id=target_site,
                                               site_name=target_site_name,
                                               library_name=library_name)
                if classified is None:
                    continue
                est_files.append(classified["est_row"])
                if target_site and target_site in site_report:
                    site_report[target_site]["estate"] += 1
                    lib["estate"] = int(lib.get("estate") or 0) + 1
                if classified["scannable"] is not None:
                    files.append(classified["scannable"])
                    if target_site and target_site in site_report:
                        site_report[target_site]["listed"] += 1
                        lib["listed"] = int(lib.get("listed") or 0) + 1
                elif inventory_out is not None and classified["inventory_row"] is not None:
                    # Non-scannable item — inventoried with metadata, never analysed.
                    inventory_out.append(classified["inventory_row"])
            # A Graph page is the natural bounded progress unit: frequent enough to show movement
            # on a large library, without turning every document into a database/SSE write.
            if target_site:
                _tick()
        if len(files) >= max_files:
            # Truncated only if the cap left something unlisted: this library still has a batch
            # pending, or a later library was never reached. A page fully read is not truncation —
            # the distinction the whole scope contract turns on.
            if next(pages, None) is not None or i < len(targets) - 1:
                hit_cap = True
                # The site whose listing the cap cut off, and every site after it, did not
                # complete. Left as-is they would read "complete" beside a total that stopped
                # mid-library — a per-site breakdown that agrees with a truncated grand total is
                # worse than none, because it looks like corroboration.
                for j in range(i, len(targets)):
                    later = targets[j][2]
                    if later and later in site_report:
                        rep = site_report[later]
                        rep["status"] = "partial" if j == i else "skipped"
            break

    _finish_consumed_library(cur_site, cur_drive)
    _finish_consumed_site(cur_site)
    _emit_site(cur_site, cur_files, cur_inv)
    # A site with no libraries visible to the token completes without ever producing a target, so
    # the boundary above never reaches it. It is still a site the caller has finished with, and a
    # resume that re-resolved it would pay the Graph calls again to learn the same nothing.
    for s_id in site_ids:
        _emit_site(s_id, len(files), len(inventory_out) if inventory_out is not None else 0)

    if relisted:
        where = (f"site {site_ids[0]}" if len(site_ids) == 1
                 else f"{len(site_ids)} sites" if site_ids else "OneDrive")
        print(f"[scan] {relisted} duplicate listing(s) of the same {where} item id collapsed "
              f"(paged /search overlap) — not extra documents", flush=True)
    if site_report:
        # Anything still 'queued' or 'scanning' at this point was never resolved by the loop —
        # the search-index path builds its targets up front and resolves them here, and a break
        # can leave a status mid-flight. Left alone they would read as an in-progress scan on a
        # finished run.
        for rep in site_report.values():
            if rep["status"] == "scanning":
                rep["status"] = "complete"
            elif rep["status"] == "queued":
                rep["status"] = "skipped"
                hit_cap = True
        _tick()
    if scope_out is not None:
        # Parity with _search_drive: the whole-estate three-denominator summary, so SharePoint's
        # coverage dashboard is populated and its truncation is honest (a floor when hit_cap).
        scope_out["inventory"] = estate_inventory.summarize(est_files, truncated=hit_cap)
        if site_report:
            # AUDITABLE PER-SITE TOTALS, which is what makes "no site was silently omitted" a
            # checkable claim rather than an assurance: a site that held nothing and a site that
            # was never opened produce the same contribution to the grand total and different
            # rows here.
            scope_out["sites"] = [dict(v) for v in site_report.values()]
        if delta_plan:
            # WHICH libraries were reconstructed and which were re-walked, and why. The Phase 3
            # exit gate is that an incremental scan produces the SAME inventory a full one does —
            # a claim nobody can check from a file count, because the two are supposed to be
            # equal. What distinguishes them is this: how much of the estate was carried forward
            # rather than re-read, and which libraries had to be reconciled anyway.
            libs = [lib for rep in site_report.values() for lib in (rep.get("libraries") or [])]
            scope_out["incremental"] = {
                "delta_libraries": sorted(l["id"] for l in libs if l.get("mode") == "delta"),
                "full_libraries": sorted(l["id"] for l in libs if l.get("mode") == "full"),
                "reconciled": {l["id"]: l["full_reason"] for l in libs if l.get("full_reason")},
                "carried_documents": int((delta_plan or {}).get("carried") or 0),
            }
    result = files[:max_files]
    # Content-type enrichment LAST, over the FINAL scannable set only — after truncation, so a
    # capped listing never pays for classification on files it is about to drop.
    _ct = _sp_enrich_content_types(token, result)
    # Permissions AFTER truncation too, and for the same reason: a capped listing must not pay a
    # per-document call for files it is about to drop.
    _perm = _sp_enrich_permissions(token, result)
    if scope_out is not None and (_perm.get("attempted") or _perm.get("capped")):
        scope_out["permissions_read"] = _perm
    _ana = _sp_enrich_analytics(token, result)
    if scope_out is not None and (_ana.get("attempted") or _ana.get("capped")):
        scope_out["activity_read"] = _ana
    if scope_out is not None and _ct and (_ct.get("attempted") or _ct.get("capped")):
        # Only when it actually ran. A tenant that answers the inline expansion pays nothing here
        # and gets no key, so the presence of this block on a scan is itself the signal that the
        # expansion was refused — see _sp_enrich_content_types for why that is the thing to fix.
        scope_out["content_type_fallback"] = _ct
    return result


# Graph's simple upload tops out at 4 MiB; past that it needs a resumable session. Remediated
# PPTX and PDF routinely exceed it, so this is not an edge case worth deferring — a write-back
# that works for small files and fails for the ones a customer notices is worse than none.
_SP_SIMPLE_MAX = 4 * 1024 * 1024

# Where an original goes immediately before it is overwritten in place. Matches the name the
# browser path has been using since the SharePoint write-back shipped, so archives written by
# either path land in the same folder rather than in two nobody thinks to look in both of.
SP_ARCHIVE_FOLDER = "_mova-originals"


def _sp_base(drive_id: str | None) -> str:
    """The Graph drive root for a write. Absent `drive_id` means the signed-in user's OneDrive.

    The same convention `_sp_download` established, and it has to be the same one: an item listed
    from OneDrive carries no driveId at all (see _sp_list), so requiring one here would break
    every OneDrive write-back while looking like a safety improvement. Where a drive IS named it
    must be honoured — item ids are unique only within a drive.
    """
    return f"{GRAPH}/drives/{drive_id}" if drive_id else f"{GRAPH}/me/drive"


# --- PRD Phase 3: SharePoint incremental sync (Graph delta query) --------------------------
# The mirror of drive_changes_since/drive_start_page_token, for the one connector-native change
# feed Microsoft Graph offers. Unlike Drive's changes.list, Graph's delta has no free-standing
# "give me a baseline token" call — the ONLY way to get a deltaLink is to walk the delta feed at
# least once, and on a token-less first call that walk returns the ENTIRE current tree, not an
# empty starting point. See core._sp_sync_plan for how a seed call's (large) first page is
# discarded rather than processed, same as this cost being paid once ever, not once per skip.

def sp_delta_since(token: str, drive_id: str | None, delta_link: str | None
                   ) -> tuple[list[dict], set[tuple[str | None, str]], str]:
    """Page through Microsoft Graph's delta query for one drive, from `delta_link` (a full
    Graph @odata.deltaLink URL persisted from a prior call), or from a fresh baseline when
    `delta_link` is None. Returns (changed_items — raw Graph driveItem resources, folders and OS
    metadata already dropped — removed_keys — (drive_id, item id) pairs deleted since
    `delta_link`, matching _sp_list's own (drive, item) identity keying — and the new deltaLink
    to persist for next time).

    Raises PermissionError (via _sp_get) on a missing Sites.Read.All grant, or the SDK's own
    error on an invalidated delta link (Graph 410 Gone — a link expires after roughly 30 days
    unused, or when the drive itself resets); the caller decides how to degrade
    (core._sp_delta_check always falls back to a full scan rather than trusting a failed check)."""
    # The listItem expansion on the delta feed too, for the same reason the walk asks for it:
    # a CHANGED file is replaced wholly by its fresh raw item (apply_sp_delta), so without the
    # expansion every file the delta reports would come back stripped of its content type and
    # managed columns — an edit to a document would silently blank the metadata a rule keys on,
    # which is worse than not syncing incrementally at all.
    #
    # Same discipline as the walk: try it, and on refusal fall back to the plain feed rather than
    # losing the delta. A delta link ALREADY carries its own query, so the expansion is only
    # added when starting fresh; a persisted deltaLink is passed through untouched, which is what
    # Graph requires of it.
    expand = _SP_LIST_EXPAND if sp_metadata.expand_enabled() else ""
    url = delta_link or f"{_sp_base(drive_id)}/root/delta?$select={_SP_ITEM_SELECT}{expand}"
    items: list[dict] = []
    removed: set[tuple[str | None, str]] = set()
    new_delta_link = None
    expand_error = None if expand else _SP_TIER_REASON[3]
    while url:
        try:
            data = _sp_get(token, url)
        except PermissionError:
            raise
        except Exception:                 # noqa: BLE001 — a refused ASK, not a refused drive
            if not expand or expand not in url:
                raise                     # the plain feed failed: that is a real failure
            expand_error = _SP_TIER_REASON[2]
            print(f"[scan] SharePoint drive {drive_id or 'me'} delta: {expand_error}", flush=True)
            url = url.replace(expand, "")
            continue
        for item in data.get("value", []):
            if item.get("deleted"):
                if item.get("id"):
                    removed.add((drive_id, item["id"]))
                continue
            if "folder" in item:
                continue   # a delta feed reports folder changes too; not user content
            if estate_inventory.is_os_metadata(item.get("name", "") or ""):
                continue
            # The same container stamps the walk applies, so a delta-sourced item classifies
            # through _sp_classify_item byte-identically to a walked one — including reporting
            # its metadata unread, with the reason, when the expansion was refused.
            if expand_error:
                item["_acp_list_item_error"] = expand_error
            elif "listItem" in item:
                item["_acp_list_item"] = item.get("listItem") or {}
            else:
                item["_acp_list_item_error"] = (
                    "this drive has no backing SharePoint list, so it carries no content "
                    "type, managed columns or version (a personal OneDrive)")
            # The delta feed never carries the wider driveItem $select, so retention labels are
            # not in it. Said as unread rather than left to read as unset.
            item["_acp_rich_error"] = (
                "Graph's delta feed does not carry the wider driveItem $select, so a retention "
                "label is not re-read on an incremental sync")
            items.append(item)
        url = data.get("@odata.nextLink")
        if "@odata.deltaLink" in data:
            new_delta_link = data["@odata.deltaLink"]
    return items, removed, new_delta_link


# --- PRD Phase 3: reconstruct a SharePoint/OneDrive listing from a prior scan + a Graph delta -
# The SharePoint mirror of _drive_file_from_inventory_row/apply_drive_delta/
# drive_reconstructed_listing (#951). _sp_list has no _normalize()-equivalent shared tail of its
# own (site/library iteration and budget tracking live inline in its loop), so this replays
# _sp_classify_item — the same per-item classification _sp_list's live loop uses — over a
# merged prior+changed set instead of sharing a finishing function the way the Drive path does.

#: The scan_inventory columns a delta reconstruction carries forward onto an unchanged file.
#: Every one is a value that was READ from SharePoint on an earlier run and that re-reading would
#: cost a Graph call per document. Kept as a list rather than "everything on the row" so a column
#: added for some other purpose does not silently start travelling through the delta path.
_SP_CARRIED_METADATA = ("site_id", "site_name", "library_name", "content_type",
                        "retention_label", "sensitivity_label", "sharing_scope", "item_kind",
                        "checked_out_by", "sp_version", "modified_by", "sp_metadata")


def _sp_file_from_inventory_row(row: dict) -> dict:
    """The inverse of _sp_inventory_row/the scannable `rec` _sp_classify_item builds: reconstruct
    a raw Graph-driveItem-shaped dict from one persisted scan_inventory row
    (store.latest_scan_inventory_items), so a reconstructed listing can run the SAME
    _sp_classify_item a fresh listing does. `size` is approximate (KB-rounded — scan_inventory
    only ever stored that) — cosmetic only, never a compliance-relevant fact.

    `content_type` (from _sp_enrich_content_types) IS carried forward, on the private
    `_acp_content_type` key that _sp_classify_item restamps onto the scannable record.

    This docstring used to say the opposite — "can NEVER be reconstructed: it is never persisted
    to scan_inventory" — and that premise was simply false. `scan_inventory.content_type` is a
    real column and `store.add_inventory` populates it. What was actually missing was one name in
    one SELECT list: `store.latest_scan_inventory_items` did not read the column back, so the
    reconstruction baseline arrived without it and the conclusion "it is never persisted" was
    drawn from the symptom. Every carried-forward SharePoint file lost its Content Type on every
    delta sync as a result.

    The cost argument in the old note was right and still holds: recovering it live is a per-item
    Graph call, and paying it for every carried-forward file would spend exactly the saving this
    feature exists for. But nothing had to be recovered — it was already stored.

    A `_acp_`-prefixed key, not a Graph-shaped one: this dict is otherwise a faithful raw
    driveItem, and a reader must be able to tell at a glance which fields came from Graph and
    which are ACP's own. Live items never carry it, so _sp_classify_item's behaviour on a fresh
    listing is byte-identical — which its own docstring requires.
    """
    size_kb = row.get("size_kb")
    hashes = {"quickXorHash": row["checksum"]} if row.get("checksum") else {}
    out = {"id": row.get("drive_file_id"), "name": row.get("source_name") or row.get("file"),
           "file": {"mimeType": row.get("mime"), "hashes": hashes},
           "createdDateTime": row.get("created_at"),
           "lastModifiedDateTime": row.get("source_modified"),
           "size": int(size_kb) * 1024 if size_kb is not None else None,
           "createdBy": {"user": {"displayName": row["owner"]}} if row.get("owner") else {},
           "parentReference": {"path": row.get("parent_folder"), "driveId": row.get("drive_id")}}
    # Only when the stored row actually has one: a None here would be indistinguishable from
    # "absent" downstream anyway, and omitting the key keeps a contentless row's reconstructed
    # shape exactly what it was before this.
    if row.get("content_type"):
        out["_acp_content_type"] = row["content_type"]
    # THE PHASE 2 METADATA, carried forward on the same `_acp_` convention and for the same
    # reason: re-reading it live is a Graph call per file, which is exactly the cost delta sync
    # exists to avoid, and the stored value is CORRECT for a file the delta did not mention —
    # an unchanged file's retention label is still its retention label.
    #
    # Without this, every delta sync would re-inventory each unchanged file with no content type,
    # no retention label and no managed columns: the identical silent erasure that cost the
    # Content Type on every sync until store.latest_scan_inventory_items was fixed to select it,
    # now on the wider set of fields lifecycle rules are actually written against.
    #
    # The honest limit, recorded rather than glossed: a managed-column edit that does not touch
    # the driveItem may never appear in Graph's delta feed, so a carried-forward column can go
    # stale silently. That is what periodic full reconciliation is for (ACP_SP_RECONCILE_DAYS) —
    # a correctness control, not a performance knob.
    carried = {k: row.get(k) for k in _SP_CARRIED_METADATA if row.get(k) is not None}
    if carried:
        out["_acp_sp_carried"] = carried
    return out


def apply_sp_delta(prior_files: list[dict], changed_files: list[dict], removed_ids) -> list[dict]:
    """Reconstruct 'the current known SharePoint/OneDrive estate' from `prior_files` (raw
    Graph-item-shaped dicts — see _sp_file_from_inventory_row) with `changed_files` (fresh raw
    items from sp_delta_since) overlaid and `removed_ids` dropped. Keyed by (driveId, item id) —
    a Graph item id is unique only within its drive (see _sp_list) — matching sp_delta_since's
    own (drive_id, id) keying for removed_ids and _SP_ITEM_SELECT's parentReference field for
    changed_files. A changed id replaces its prior entry WHOLLY (fresh metadata wins, never
    merged field-by-field); anything not mentioned by the delta carries forward untouched. A
    changed id with no prior entry is a genuinely new file. Pure and side-effect free — the
    same contract as apply_drive_delta, over the two-part identity SharePoint needs."""
    removed_ids = set(removed_ids or ())

    def _key(item):
        return ((item.get("parentReference") or {}).get("driveId"), item.get("id"))

    changed_by_key = {_key(f): f for f in changed_files if f.get("id")}
    seen: set = set()
    out = []
    for f in prior_files:
        k = _key(f)
        if not k[1] or k in removed_ids:
            continue
        seen.add(k)
        out.append(changed_by_key.get(k, f))
    for k, f in changed_by_key.items():
        if k not in seen and k not in removed_ids:
            out.append(f)
    return out


def sp_reconstructed_listing(prior_files: list[dict], changed_files: list[dict], removed_ids, *,
                             max_files: int = 200, exclude_remediated: bool = False,
                             scope_out: dict | None = None,
                             inventory_out: list | None = None) -> list[dict]:
    """A SharePoint/OneDrive listing's worth of result, WITHOUT walking Graph — see
    apply_sp_delta for how the estate is reconstructed. Classifies every merged item through
    _sp_classify_item, the SAME function _sp_list's live loop uses, so a reconstruction is
    indistinguishable downstream from a fresh listing: same skip-folder/OS-metadata filtering,
    same scannable/inventory split, same whole-estate triage rows.

    `scope_out['reconstructed'] = True` is the only difference from a fresh _sp_list call's
    contract. `truncated` can still fire here (the merged set exceeding `max_files`) even though
    there is no raw paging cap to hit — the same distinction _finish_drive_listing draws for
    Drive's own reconstruction.
    """
    merged = apply_sp_delta(prior_files, changed_files, removed_ids)
    skip_folders = _sp_skip_folders(exclude_remediated)
    files: list[dict] = []
    est_files: list[dict] = []
    for item in merged:
        if "file" not in item:
            continue
        drive_id = (item.get("parentReference") or {}).get("driveId")
        classified = _sp_classify_item(item, drive_id=drive_id, skip_folders=skip_folders,
                                       exts=_sp_scannable_exts())
        if classified is None:
            continue
        est_files.append(classified["est_row"])
        if classified["scannable"] is not None:
            files.append(classified["scannable"])
        elif inventory_out is not None and classified["inventory_row"] is not None:
            inventory_out.append(classified["inventory_row"])
    result = files[:max_files]
    if scope_out is not None:
        # _list()'s SharePoint tail reads scope_out["truncated"] straight off
        # inventory["truncated"] (unlike Drive, which folds a separate max_files-overflow check
        # in at its own top level) — so the overflow this reconstruction can hit (a merged set
        # bigger than max_files, with no "pages remaining" of its own to signal it) belongs HERE,
        # matching what a live _sp_list caller already reads.
        scope_out["inventory"] = estate_inventory.summarize(
            est_files, truncated=len(files) > max_files)
        scope_out["reconstructed"] = True
    return result


class SharePointSessionExpired(PermissionError):
    """A delegated Graph token expired; a freshly supplied token can make a retry succeed."""


def _sp_put(token: str, url: str, data: bytes, content_type: str):
    import httpx
    r = httpx.put(url, headers={"Authorization": f"Bearer {token}", "Content-Type": content_type},
                  content=data, timeout=120, follow_redirects=True)
    if r.status_code == 401:
        raise SharePointSessionExpired("Microsoft Graph access token expired.")
    if r.status_code == 403:
        raise PermissionError(
            "Microsoft Graph refused the write. Writing remediated copies needs a WRITE scope "
            "(Files.ReadWrite.All, or Sites.ReadWrite.All for a team site) on the Azure app "
            "registration — reading a site with Sites.Read.All does not grant it.")
    r.raise_for_status()
    return r.json() if r.content else {}


def _sp_folder_id(token: str, drive_id: str, name: str, parent_id: str = "") -> str:
    """Find-or-create a folder, returning its id. At the drive root unless `parent_id` is given.

    The Graph counterpart of handlers.ensure_remediated_folder, and it reuses that folder's
    NAME from the same setting so an operator who renames the mirror renames it everywhere.
    `conflictBehavior: replace` on create would clobber an existing folder's contents, so the
    lookup comes first and create is only the miss path. On an ARCHIVE folder that ordering is
    not a nicety — replace there would destroy the backups this whole path exists to keep.
    """
    import httpx
    root = _sp_base(drive_id)
    base = f"{root}/items/{parent_id}" if parent_id else f"{root}/root"
    children = f"{base}/children"
    listing = _sp_get(token, f"{children}?$select=id,name,folder&$top=200")
    for item in listing.get("value", []):
        if item.get("name") == name and "folder" in item:
            return item["id"]
    r = httpx.post(children,
                   headers={"Authorization": f"Bearer {token}",
                            "Content-Type": "application/json"},
                   json={"name": name, "folder": {},
                         "@microsoft.graph.conflictBehavior": "fail"},
                   timeout=30, follow_redirects=True)
    if r.status_code == 401:
        raise SharePointSessionExpired("Microsoft Graph access token expired.")
    if r.status_code == 403:
        raise PermissionError(
            f"Microsoft Graph refused to create the '{name}' folder — this needs a WRITE scope "
            "(Files.ReadWrite.All / Sites.ReadWrite.All).")
    if r.status_code == 409:
        # Created by a concurrent write between the list and the create. Re-read rather than
        # fail: two workers remediating the same library at once is normal, and this is exactly
        # how Drive grew duplicate mirror folders before ensure_remediated_folder was hoisted
        # out of the workers.
        listing = _sp_get(token, f"{children}?$select=id,name,folder&$top=200")
        for item in listing.get("value", []):
            if item.get("name") == name and "folder" in item:
                return item["id"]
    r.raise_for_status()
    return r.json()["id"]


def _sp_archive_original(token: str, drive_id: str, item_id: str, today: str) -> str:
    """Copy the item into SP_ARCHIVE_FOLDER/<today>/ before it is overwritten. Returns folder id.

    FAIL-CLOSED. Every failure raises, and the caller must not write if it does. An archive that
    only sometimes happens is not a backup, and it fails invisibly at exactly the moment it
    matters — so the worst outcome here is "your file was not remediated", which a user can see
    and retry, rather than "your file was replaced and the original is gone", which they cannot.

    Graph answers a copy with 202 Accepted and a Location header to poll: it is asynchronous by
    design, so any 2xx means the copy was ACCEPTED, not that it has finished. This does not
    poll, which is a deliberate limit worth naming — a copy accepted and then failing server-side
    would not be caught here. Polling would serialise every save behind Graph's copy queue.
    """
    import httpx
    root = _sp_folder_id(token, drive_id, SP_ARCHIVE_FOLDER)
    dated = _sp_folder_id(token, drive_id, today, parent_id=root)
    r = httpx.post(f"{_sp_base(drive_id)}/items/{item_id}/copy",
                   headers={"Authorization": f"Bearer {token}",
                            "Content-Type": "application/json"},
                   json={"parentReference": {"id": dated}},
                   timeout=60, follow_redirects=True)
    if r.status_code == 401:
        raise SharePointSessionExpired("Microsoft Graph access token expired.")
    if r.status_code == 403:
        raise PermissionError(
            "Microsoft Graph refused to archive the original — replacing a file in place needs a "
            "WRITE scope (Files.ReadWrite.All / Sites.ReadWrite.All). Nothing was overwritten.")
    # Any 2xx, checked on status_code rather than httpx's `is_success`: every other _sp_* helper
    # reads status_code, and the tests' Graph double implements exactly the surface those use.
    # A second idiom here means the double is silently partial for one function.
    if not 200 <= r.status_code < 300:
        raise RuntimeError(f"archive failed (HTTP {r.status_code}) — nothing was overwritten")
    return dated


def _sp_write(token: str, *, put_url: str, session_url: str, content: bytes,
              content_type: str, conflict_behavior: str = "replace",
              force_session: bool = False) -> dict:
    """One Graph write, simple or resumable depending on size.

    Graph rejects a simple PUT past 4 MiB with a 413 that says nothing about chunking, so the
    large path opens an upload session instead. Shared by the mirror upload and the in-place
    replace, which differ only in the URLs they aim at.
    """
    if len(content) <= _SP_SIMPLE_MAX and not force_session:
        return _sp_put(token, put_url, content, content_type)

    import httpx
    r = httpx.post(session_url,
                   headers={"Authorization": f"Bearer {token}",
                            "Content-Type": "application/json"},
                   json={"item": {"@microsoft.graph.conflictBehavior": conflict_behavior}},
                   timeout=30, follow_redirects=True)
    if r.status_code == 401:
        raise SharePointSessionExpired("Microsoft Graph access token expired.")
    if r.status_code == 403:
        raise PermissionError(
            "Microsoft Graph refused to open an upload session — writing needs "
            "Files.ReadWrite.All / Sites.ReadWrite.All.")
    r.raise_for_status()
    url = r.json()["uploadUrl"]

    # 320 KiB * 10. Graph requires every chunk except the last to be a multiple of 320 KiB, and
    # a size that is not is rejected with a message that does not say so.
    chunk = 320 * 1024 * 10
    total = len(content)
    out: dict = {}
    for start in range(0, total, chunk):
        end = min(start + chunk, total) - 1
        # No Authorization header: the session URL carries its own credential, and sending the
        # bearer token to it is rejected by Graph rather than ignored.
        cr = httpx.put(url, headers={"Content-Length": str(end - start + 1),
                                     "Content-Range": f"bytes {start}-{end}/{total}"},
                       content=content[start:end + 1], timeout=300)
        if cr.status_code == 401:
            raise SharePointSessionExpired("Microsoft Graph upload session expired.")
        cr.raise_for_status()
        if cr.content:
            out = cr.json()
    return out


def _sp_replace(token: str, drive_id: str, item_id: str, content: bytes,
                content_type: str = "application/octet-stream") -> dict:
    """Overwrite one existing item IN PLACE. The caller must have archived it first.

    Writing to the item's own id keeps its URL, its sharing links and its version history — the
    reason to replace rather than mirror is that everyone who already has a link to the document
    gets the remediated one. Nothing else in this module writes over a user's file, so the
    archive in front of it is not optional.
    """
    base = f"{_sp_base(drive_id)}/items/{item_id}"
    return _sp_write(token, put_url=f"{base}/content",
                     session_url=f"{base}/createUploadSession",
                     content=content, content_type=content_type)


def _sp_describe(token: str, drive_id: str, item_id: str, text: str) -> None:
    """Stamp a human-readable note on the item. Best-effort by design.

    This is the nearest SharePoint gets to Drive's provenance stamp, and it is NOT equivalent:
    a description is a label a person reads, not something the scanner keys off (see _sp_list —
    SharePoint re-ingestion is folder-scoped and this does not change that). So a failure here
    must never fail a write that already succeeded; the bytes are the deliverable.
    """
    import httpx
    try:
        httpx.patch(f"{_sp_base(drive_id)}/items/{item_id}",
                    headers={"Authorization": f"Bearer {token}",
                             "Content-Type": "application/json"},
                    json={"description": text}, timeout=30, follow_redirects=True)
    except Exception:      # noqa: BLE001 — a label is not worth failing a successful write over
        swallowed("scanner._sp_describe: patching the SharePoint description failed")


def _sp_upload(token: str, drive_id: str, folder: str, filename: str,
               content: bytes, content_type: str = "application/octet-stream") -> dict:
    """Write one remediated file into `folder` on `drive_id`. Returns the Graph driveItem.

    Small files go straight up; anything over 4 MiB uses a resumable session, because Graph
    rejects a simple PUT past that limit and the rejection is a 413 that says nothing about
    chunking.
    """
    folder_id = _sp_folder_id(token, drive_id, folder)
    safe = _safe_name(filename)
    base = f"{GRAPH}/drives/{drive_id}/items/{folder_id}:/{safe}:"
    return _sp_write(token, put_url=f"{base}/content",
                     session_url=f"{base}/createUploadSession",
                     content=content, content_type=content_type)


def _sp_download(token: str, item: dict, dest: Path) -> None:
    """Download from the drive the item was LISTED from, via MS Graph /content redirect.

    An absent `driveId` means /me/drive — the shape every item had before site support, so a
    re-run of an older scan still resolves. Present, it must be honoured: item ids are unique
    only within a drive, so asking /me/drive for a site's item id either 404s or, worse, returns
    a different document that happens to share the id.
    """
    import httpx
    hdrs = {"Authorization": f"Bearer {token}"}
    drive_id = item.get("driveId")
    base = f"{GRAPH}/drives/{drive_id}" if drive_id else f"{GRAPH}/me/drive"
    url = f"{base}/items/{item['id']}/content"
    r = httpx.get(url, headers=hdrs, timeout=120, follow_redirects=True)
    r.raise_for_status()
    (dest / item["name"]).write_bytes(r.content)


def _dedupe_names(items: list[dict]) -> list[dict]:
    """Disambiguate same-named items from one listing — e.g. two Drive files literally
    named "Clinical-Proposal-92.pptx" with different file ids (Drive allows this; a
    filesystem wouldn't). Every downstream consumer keys off item["name"] alone
    (_download's local temp path, the in-memory per-file dict, and file_records'
    PRIMARY KEY (scan_id, file)), so without this, the second same-named item silently
    clobbers the first at each of those layers — one of the two is never analysed,
    scored, or stored, with no error or trace. Appends " (N)" before the extension on
    the 2nd+ occurrence, mirroring Drive's own web-UI convention on a manual duplicate
    upload — the frontend's "Group duplicate uploads" toggle already strips exactly
    this pattern back off for display, so it composes for free."""
    seen: dict[str, int] = {}
    out = []
    for it in items:
        name = it["name"]
        item = {**it, "source_name": it.get("source_name") or name}
        n = seen.get(name, 0)
        seen[name] = n + 1
        if n == 0:
            out.append(item)
        else:
            stem, dot, ext = name.rpartition(".")
            disambiguated = f"{stem or name} ({n}){dot}{ext}" if dot else f"{name} ({n})"
            out.append({**item, "name": disambiguated})
    return out


def _sp_locations(roots: list[str]) -> tuple[list[tuple[str, str]], list[str]]:
    """Split chosen SharePoint roots into (drive, item) folder locations and site ids.

    A folder location is written `<driveId>/<itemId>` — the pair, because a Graph item id is
    unique only within its drive. A root with no "/" is a site id, which is what the site picker
    has always sent, so an existing caller is unchanged.

    SITES ARE A LIST, and that is the fix rather than a generalisation for its own sake. This
    returned a single `site` and kept the FIRST bare root — `elif site is None` — so a request
    naming five sites was answered with one, silently: no error, no truncation flag, a scan that
    reported one site's documents as the estate and a compliance number computed from it. Every
    caller now gets all of them, and the bound on how many are actually walked is stated in one
    place (_sp_cap_sites) where it can be recorded on the scope.

    Duplicates collapse. Selecting the same site twice is the same estate, not twice the estate,
    and the budget spent re-walking it would come out of the sites that were not walked yet.
    """
    locs: list[tuple[str, str]] = []
    sites: list[str] = []
    for r in roots:
        if "/" in r:
            drive_id, _, item_id = r.partition("/")
            if drive_id and item_id:
                locs.append((drive_id, item_id))
        elif r and r not in sites:
            sites.append(r)
    return locs, sites


def _sp_whole_library_target(folder: str | None,
                             folders: list[str] | None) -> tuple[bool, str | None]:
    """Is this SharePoint request narrow enough for delta reconstruction — a single, whole
    Graph drive, no sub-folder narrowing? Two shapes qualify, both scoped to exactly one drive:

      - no folder/folders at all: the signed-in user's whole OneDrive (drive_id=None, /me/drive)
      - a single folder written "{driveId}/root" — Graph's "root" item-id alias for one whole
        library, the same addressing the scheduled sweep's {drive_id}/root already uses (#961)

    Anything else — a bare site id (walks every library on the site, no single drive to scope a
    delta query to), a real sub-folder, or more than one location — returns (False, None):
    sp_delta_since is scoped to exactly one Graph drive, so none of those requests are "the
    whole of exactly one drive".

    Returns (eligible, drive_id) — drive_id is None for the bare-OneDrive case, which
    sp_delta_since/_sp_base already read as "the signed-in user's own drive"."""
    # Same roots-building line _list() itself uses — "root" alone is Drive's own no-narrowing
    # sentinel and is dropped here rather than being read as a (nonsensical) SharePoint site id.
    roots = [f for f in (list(folders) if folders else ([folder] if folder else []))
             if f and f != "root"]
    if not roots:
        return True, None
    if len(roots) > 1:
        return False, None
    locs, sites = _sp_locations(roots)
    if sites or not locs:
        return False, None
    [(drive_id, item_id)] = locs
    return (True, drive_id) if item_id == "root" else (False, None)


def _list(source: str, svc=None, folder: str | None = None, sp_token: str | None = None,
          max_files: int | None = None, exclude_remediated: bool = False,
          scope_out: dict | None = None, scope_files: dict | None = None,
          inventory_out: list | None = None,
          folders: list[str] | None = None,
          exclude_folders: list[str] | None = None,
          include_subfolders: bool = True,
          progress_cb=None, drive_delta: dict | None = None,
          sp_delta: dict | None = None,
          sp_delta_plan: dict | None = None,
          sp_site_done=None,
          sp_skip_sites: set | None = None) -> list[dict]:
    """List the source. `scope_out`, when given, is filled in with WHAT WAS COVERED.

    `inventory_out`, when given, is filled with per-file inventory rows for the NON-scannable
    estate (media / unsupported / extensionless) — every accessible file that is NOT in the
    returned analysis set. The caller inventories the scannable set itself (from the returned
    items, which carry canonical names + source metadata) and appends these, so the whole estate
    is recorded per-file while only the supported subset is ever downloaded and analysed.

    `scope_files` is the operator's resolved `scan_scope` map. Given, files whose format no
    in-scope criterion applies to are dropped from the listing and never read — see the block at
    the end of this function. None means no restriction, which is NOT the same as the full set of
    formats and must not be collapsed into one.

    A file count on its own is not a fact about an estate — it is a fact about a boundary the
    caller chose, and the two are only the same number when the boundary is "everything". On
    2026-07-30 a folder-scoped scan of a one-file folder reported "1 document" six seconds after
    a whole-Drive scan of the SAME account reported 8, and the product presented both as the
    size of the estate. Neither listing was wrong; the screen was, because it printed the
    number and dropped the boundary.

    So discovery now hands its boundary out with the count, `scope_out` is persisted on the scan
    (store.init_scan_run / save_scan), and the UI renders one from the other. Shape:

      {"kind": "folder"|"drive"|"local"|"sharepoint", "kept": int, "truncated": bool, ...}

    `truncated` is the strictly different claim "we hit a cap and there are files we did not
    list" — a folder scan that covered its folder completely is NOT truncated, however small the
    answer. `folder_name` is resolved here (one metadata read) so the UI can say which folder
    rather than showing a Drive id nobody recognises.
    """
    # `folders` is the multi-root form of `folder`; `folder` remains accepted so every existing
    # caller, saved link and queued job keeps working. "root" is Drive's sentinel for "no
    # narrowing" and is dropped here rather than at four branches below.
    roots = [f for f in (list(folders) if folders else ([folder] if folder else []))
             if f and f != "root"]
    # Exclusions only mean anything BENEATH an inclusion (PRD 6.3: a selected parent with an
    # excluded child). With no roots there is nothing to carve out of, and honouring them
    # against a whole-Drive scan would silently narrow a scan nobody asked to narrow — the
    # boundary defect in its most invisible form, since the card shows no chips either.
    excl = {e for e in (exclude_folders or ()) if e} if roots else set()

    # The monolithic scan keeps conservative caps (one box's disk holds every file);
    # the fan-out path (ADR 0007) passes a high cap since each file is its own job.
    if source == "local":
        # ACP_LOCAL_CORPUS: point local scans at a different directory — the test
        # suite uses it to scan the frozen oracle corpus (test-corpus/oracle/)
        # instead of the demo estate, which changes with the demo's needs.
        #
        # rglob, not glob: a real estate is a nested tree, so discovery walks the whole subtree
        # rather than a single level. Each file also carries the filesystem metadata a Drive /
        # SharePoint listing would (size, modified, created, owner, parent) via _local_stat_meta,
        # so the scannable rows are no longer path-only and the whole subtree is inventoried.
        corpus = Path(os.environ.get("ACP_LOCAL_CORPUS") or (ACP / "test-corpus/files"))
        # Local/demo scans use the same scope as Drive and SharePoint.
        scannable = scan_formats.extensions()
        result: list[dict] = []
        # Drive-shaped rows for the WHOLE walk (scannable + not), so estate_inventory.summarize
        # can classify local scans the same way it does Drive/SharePoint. Without this, `_list`
        # returned no `scope_out["inventory"]` at all for local/demo scans, and the Discover tab's
        # headline count (scope.inventory.discovered) fell back to 0 until Assess populated
        # file_records — a local scan read as "0 documents discovered" even when it found plenty.
        _estate_files: list[dict] = []
        for p in sorted(corpus.rglob("*")):
            if not p.is_file():
                continue
            if estate_inventory.is_os_metadata(p.name):
                continue  # OS metadata files (.DS_Store, Thumbs.db, …) — not user content
            meta = _local_stat_meta(p, corpus)
            _estate_files.append({"id": str(p), "name": p.name, "mimeType": meta["mime"] or "",
                                  "modifiedTime": meta["source_modified"],
                                  "size": meta["size"],
                                  "owners": [{"displayName": meta["owner"]}] if meta["owner"] else []})
            if p.suffix.lower() in scannable:
                result.append({"name": p.name, "path": str(p),
                               "size_kb": _inv_size_kb(meta["size"]),
                               "source_mime": meta["mime"],
                               "source_modified": meta["source_modified"],
                               "created_at": meta["created_at"], "owner": meta["owner"],
                               "parent_folder": meta["parent_folder"]})
            elif inventory_out is not None:
                inventory_out.append(_inv_row(
                    file=p.name, path=str(p), mime=meta["mime"], size=meta["size"],
                    source_modified=meta["source_modified"], created_at=meta["created_at"],
                    owner=meta["owner"], parent_folder=meta["parent_folder"]))
        if scope_out is not None:
            scope_out.update({"kind": "local", "path": str(corpus), "kept": len(result),
                              "truncated": False,
                              "inventory": estate_inventory.summarize(_estate_files, truncated=False)})
    elif source == "sharepoint":
        # `folder` carries the SharePoint SITE id here, reusing the parameter Drive already uses
        # to narrow a scan rather than threading a second one through five call sites. "root" is
        # Drive's own sentinel for "no narrowing" and means the same thing here: OneDrive.
        # Roots may now be FOLDERS as well as a site: `<driveId>/<itemId>` is a folder inside a
        # library or OneDrive, a bare id is a site. Folder narrowing is what makes OneDrive
        # scopeable at all — before this, "SharePoint" could only ever mean a whole site or the
        # whole of the signed-in user's OneDrive.
        sp_locs, sp_all_sites = _sp_locations(roots)
        # Capped HERE as well as inside _sp_list, through the same helper, so the scope recorded
        # below names exactly the sites that were walked. A scope blob listing 40 sites next to a
        # listing that covered 30 is worse than either number alone: it is a boundary the reader
        # can check the count against, and it would be wrong.
        sp_sites, sp_dropped_sites = _sp_cap_sites(sp_all_sites)
        # `site` stays the SINGLE-site spelling and stays None for a multi-site run. Everything
        # downstream that keys off it — the incremental baseline match, the inventory-diff
        # boundary, the UI's scope chip — reads one site id, and quietly handing it the first of
        # several would re-create the exact defect _sp_locations just stopped. Multi-site callers
        # read `sites` instead.
        site = sp_sites[0] if len(sp_sites) == 1 else None
        if sp_delta is not None:
            # PRD Phase 3: reconstruct the estate from the prior scan's inventory + a Graph
            # delta instead of walking SharePoint — see core._sp_sync_plan for how `sp_delta`
            # is built. Only ever populated for the scheduled sweep's whole-configured-library
            # request ({drive_id}/root, PR #961/#978's scope), so it is honored unconditionally
            # here — the same trust the Drive branch above places in `drive_delta`.
            result = sp_reconstructed_listing(
                sp_delta["prior_files"], sp_delta["changed"], sp_delta["removed_ids"],
                max_files=max_files or 200, exclude_remediated=exclude_remediated,
                scope_out=scope_out, inventory_out=inventory_out)
        else:
            # `locations` is passed ONLY when folders were actually chosen, so a scan that does not
            # use the new mode calls _sp_list with exactly the arguments it always did. Passing it
            # unconditionally broke four existing tests whose stubs pin the old signature — and those
            # stubs are right to: a caller that has not opted into a feature should not be able to
            # tell it exists.
            extra = {"locations": sp_locs} if sp_locs else {}
            if sp_locs and not include_subfolders:
                extra["include_subfolders"] = False
            if excl:
                extra["exclude_ids"] = excl
            if len(sp_all_sites) > 1:
                # Same reasoning as `locations` above, and the same discipline: a single-site or
                # OneDrive run calls _sp_list with EXACTLY the arguments it always did, so a test
                # stub pinning the old signature keeps passing. A caller that has not opted into
                # multi-site must not be able to tell the parameter exists.
                #
                # The UNCAPPED list is passed on purpose. _sp_list applies the same cap through
                # the same helper, and doing it there is what puts the refused sites in the
                # per-site breakdown as skipped-with-a-reason rather than dropping them before
                # anything could record them — which is the omission the exit gate is about.
                extra["sites"] = sp_all_sites
            if sp_delta_plan is not None:
                # Phase 3's per-LIBRARY plan, distinct from `sp_delta` above: that one replaces
                # the whole listing with a reconstruction of exactly one drive, this one lets the
                # walk reconstruct some libraries and re-walk others in the same pass. Passed
                # only when a caller built one, so a scan that has not opted in calls _sp_list
                # with the arguments it always did.
                extra["delta_plan"] = sp_delta_plan
            if progress_cb is not None:
                # Per-site progress out to the job/SSE stream. Passed only when a caller supplied
                # one, so a test stub pinning the old signature is unaffected — the same
                # discipline `locations` and `sites` follow above.
                extra["progress_cb"] = progress_cb
            if sp_site_done is not None:
                # Phase 4's per-site checkpoint seam. Same discipline again: passed only when a
                # caller asked for it, so nothing that has not opted in can tell it exists.
                extra["site_done_cb"] = sp_site_done
            if sp_skip_sites:
                # Passed through as given — a set names the sites, a dict also carries what the
                # caller already knows each one held. _sp_list normalises; this layer must not
                # flatten a dict to a set and throw the counts away.
                extra["skip_sites"] = sp_skip_sites
            result = _sp_list(sp_token, max_files or 200, site=site,
                              exclude_remediated=exclude_remediated, inventory_out=inventory_out,
                              scope_out=scope_out, **extra)
        if scope_out is not None:
            # `site_name` for the same reason `folder_name` exists on the Drive branch: a Graph
            # site id is `contoso.sharepoint.com,<guid>,<guid>`, and a boundary the reader cannot
            # recognise is not a boundary they can check the count against. Resolved only when a
            # site was actually chosen — OneDrive has no id to name, and an unasked-for lookup is
            # a Graph round trip spent on nothing.
            # `truncated` now comes from the estate inventory's honest hit_cap (set by _sp_list) — the
            # old `len(result) >= max_files` flagged a fully-listed estate whose analysis set merely
            # equalled the cap. `inventory` was already placed on scope_out by _sp_list.
            # One name lookup per selected site, reused for `site_name` below rather than paid
            # for twice. Bounded by the site cap, so this is at most _sp_max_sites() metadata
            # reads however many sites the tenant has.
            # Names _sp_list already resolved during the walk (it needs them per document now,
            # for sp_metadata's site_name field), looked up again ONLY for the sites it never
            # reached — a site the cap refused has no walk to have named it. Recomputing all of
            # them would be a second Graph call per site for an answer already in hand.
            _recorded = {r.get("id"): r.get("name") for r in (scope_out.get("sites") or [])
                         if isinstance(r, dict)}
            site_names = {s_id: (_recorded.get(s_id) if _recorded.get(s_id) is not None
                                 else _sp_site_name(sp_token, s_id))
                          for s_id in sp_sites}
            scope_out.update({"kind": "sharepoint", "site": site, "kept": len(result),
                              "site_name": site_names.get(site) if site else None,
                              # `or sp_dropped_sites` because the cap is applied HERE, before
                              # _sp_list ever sees the refused sites — so its own hit_cap cannot
                              # know about them and the estate would read as complete. Truncation
                              # that the layer which caused it does not report is the failure this
                              # whole change exists to stop.
                              "truncated": bool((scope_out.get("inventory") or {}).get("truncated")
                                                or sp_dropped_sites)})
            if sp_sites:
                # THE MULTI-SITE BOUNDARY, and it is load-bearing for the same reason `folders`
                # below is: a two-site run has no `site`, so without this the count renders with
                # no boundary at all and reads as the whole tenant. Always written when any site
                # was chosen — including the single-site case — so every consumer can read one
                # field instead of branching on how many there were.
                #
                # _sp_list already wrote the per-site rows (ids, libraries, listed/estate totals,
                # status, error). Only the NAME is added here, because naming a site is a Graph
                # metadata read and the walk should not pay for one per site while it is walking.
                # A site the cap refused keeps name=None rather than costing a lookup for an
                # estate nobody read.
                recorded = scope_out.get("sites")
                if isinstance(recorded, list) and recorded:
                    scope_out["sites"] = [{**row, "name": site_names.get(row.get("id"))}
                                          for row in recorded]
                else:
                    # The delta-reconstruction path never calls _sp_list, so there are no rows to
                    # decorate. Record the boundary anyway — a scope with a site set and no sites
                    # listed is the shape that renders a count with no boundary.
                    scope_out["sites"] = [{"id": s_id, "name": site_names.get(s_id)}
                                          for s_id in sp_sites]
            if sp_dropped_sites:
                # Sites the cap refused. `truncated` already says the estate is a floor; this
                # says WHY, which is the difference between "we hit a cap" and "raise
                # ACP_SP_MAX_SITES or run these in a second scan".
                scope_out["sites_omitted"] = len(sp_dropped_sites)
            if sp_locs:
                # THIS IS LOAD-BEARING, not decoration. isNarrowScope() fired on `site` for
                # SharePoint — and a OneDrive folder scan has NO site, so without `folders` here
                # a narrowed count would render with no boundary and read as the whole estate.
                # That is precisely the 2026-07-30 defect (see frontend/src/scanScope.js), which
                # a new narrowing mode gets to re-introduce for free unless it says so.
                scope_out["folders"] = [{"id": f"{d}/{i}", "name": _sp_folder_name(sp_token, d, i)}
                                        for d, i in sp_locs]
                scope_out["include_subfolders"] = include_subfolders
    elif source == "smb":
        # Network drive (ADR 0032). `folder` carries the in-scope SMB share root (a UNC path), the
        # same parameter Drive uses to narrow a scan and SharePoint reuses for the site id. The
        # adapter lists the same file dicts and builds the same scope_out["inventory"] the other
        # sources do — everything downstream is source-agnostic. The live SMB transport is
        # deployment-gated (see smb_source._walk); discovery shape and inventory are real here.
        import smb_source
        cfg = smb_source.smb_config()
        # A specific share via `folder`, else the whole configured estate — EVERY in-scope share, not
        # just the first (a UTSW estate spans up to ~10 shares; walking one under-reports it).
        roots = [folder] if folder not in (None, "", "root") else cfg["shares"]
        result = smb_source.list_smb_estate(roots, max_files=max_files or 2000, cfg=cfg,
                                            scope_out=scope_out, inventory_out=inventory_out)
        if scope_out is not None:
            scope_out.update({"kind": "smb", "root": ", ".join(roots) or None, "kept": len(result),
                              "truncated": bool((scope_out.get("inventory") or {}).get("truncated"))})
    elif len(roots) > 1:
        # Several chosen folders: walk each subtree and union them, sharing one cap.
        result = _search_folders(svc, roots, max_files or 1000,
                                 exclude_remediated=exclude_remediated, scope_out=scope_out,
                                 inventory_out=inventory_out, exclude_ids=excl,
                                 progress_cb=progress_cb, include_subfolders=include_subfolders)
    elif roots:
        # Specific folder: recursive BFS. Kept as its own branch rather than folded into
        # _search_folders so a single-folder scan produces byte-identical scope to before.
        result = _search_folder(svc, roots[0], max_files or 1000,
                                exclude_remediated=exclude_remediated, scope_out=scope_out,
                                inventory_out=inventory_out, exclude_ids=excl,
                                progress_cb=progress_cb, include_subfolders=include_subfolders)
        if scope_out is not None:
            scope_out["folder_name"] = _folder_name(svc, roots[0])
    elif folder == "root" or folder is None:
        if drive_delta is not None:
            # PRD Phase 3: reconstruct the whole-Drive estate from the prior scan's inventory +
            # a Changes API delta instead of walking Drive — see core._drive_sync_plan for how
            # `drive_delta` is built and why this is only ever populated for a whole-Drive scan.
            result = drive_reconstructed_listing(
                drive_delta["prior_files"], drive_delta["changed"], drive_delta["removed_ids"],
                max_files=max_files or 500, exclude_remediated=exclude_remediated,
                scope_out=scope_out, inventory_out=inventory_out)
        else:
            # No specific folder chosen: search the whole Drive
            result = _search_drive(svc, max_files or 500, exclude_remediated=exclude_remediated,
                                   scope_out=scope_out, inventory_out=inventory_out,
                                   progress_cb=progress_cb)
    else:
        # ADC/demo mode with a pinned folder. Requests provenance.DRIVE_FIELDS and honours
        # exclude_remediated like the two GIS paths above: this branch asked for a narrower
        # field set, so `properties` never came back and `is_acp_generated` was structurally
        # incapable of firing — ACP's own output would be re-ingested as a source document.
        resp = svc.files().list(q=f"'{_DEMO_FOLDER}' in parents and trashed=false",
                                fields=f"files({provenance.DRIVE_FIELDS})", pageSize=200,
                                orderBy="name", includeItemsFromAllDrives=True,
                                supportsAllDrives=True).execute(num_retries=5)
        batch = resp.get("files", [])
        if exclude_remediated:
            batch = [f for f in batch if not provenance.is_acp_generated(f)]
        result = _normalize(batch)
        if inventory_out is not None:
            result_ids = {it.get("id") for it in result}
            for f in batch:
                if f.get("mimeType") == estate_inventory.FOLDER_MIME:
                    continue
                if estate_inventory.is_os_metadata(f.get("name", "") or ""):
                    continue  # OS metadata files (.DS_Store, Thumbs.db, …) — not user content
                if f.get("id") not in result_ids:
                    inventory_out.append(_drive_inventory_row(f))
        if scope_out is not None:
            # Same estate-summary gap as the local branch above: without this, an ADC/demo scan's
            # scope carried no `inventory` key, and the Discover tab's headline count fell back to
            # 0 until Assess populated file_records. `batch` is already Drive-API-shaped, so no
            # translation is needed — just drop OS metadata the way inventory_out does.
            _estate_files = [f for f in batch if not estate_inventory.is_os_metadata(f.get("name", "") or "")]
            scope_out.update({"kind": "folder", "folder_id": _DEMO_FOLDER,
                              "folder_name": _folder_name(svc, _DEMO_FOLDER),
                              "folders_walked": 1, "listed": len(batch), "kept": len(result),
                              "truncated": False,
                              "inventory": estate_inventory.summarize(_estate_files, truncated=False)})
    if source == "drive":
        # PRD Phase 3 follow-up: stamp the Google account THIS listing ran as onto every
        # persisted row — scannable (`result`) and not (`inventory_out`) — regardless of which
        # branch above produced it (whole-Drive, folder-scoped, multi-folder, delta-
        # reconstructed, or the ADC/demo pinned folder all share this one `svc`). One about()
        # call per scan, not per file. See core._drive_prior_inventory_for_account for why: a
        # Drive token is a per-request browser credential, not a server-bound "connected
        # account", so nothing else stops the same ACP owner presenting a different Google
        # identity between scans.
        _drive_acct = drive_account_id(svc)
        for _it in result:
            _it["drive_account_id"] = _drive_acct
        if inventory_out is not None:
            for _it in inventory_out:
                _it["drive_account_id"] = _drive_acct
    # ── the operator's file-type scope, applied to what gets READ ───────────────────────────
    #
    # ONE PLACE, NOT FOUR. The proposal for this listed four enumeration sites (local, SharePoint,
    # Drive's mimeType allow-list, upload) and warned that missing one is how the feature
    # half-works. Every branch above converges here, so filtering at the dispatcher covers all of
    # them and any source added later — a stronger guarantee than four call sites that must be
    # kept in step.
    #
    # WHAT THIS DOES AND DOES NOT CLAIM. The out-of-scope file is still LISTED (its name and size
    # come back from a source the operator connected on purpose); it is never DOWNLOADED, opened,
    # rasterised, OCR'd, cached to blob or traced. That is the distinction that matters for PHI:
    # the content is what was being read, and `_download` runs over this list.
    #
    # Placed before `_dedupe_names` so `kept` — set by each branch above — is corrected here
    # rather than left describing a population the caller will never receive.
    if scope_out is not None:
        # The CRITERIA scope, recorded on the scan alongside the discovery boundary.
        #
        # `scope_out` is persisted to scan_runs.scope, so this rides along with no migration —
        # and the two belong together: "what this scan covered" is a folder AND a set of
        # criteria, and a reader given one without the other cannot reconstruct what was
        # measured. get_scan_diff needs it because a score is computed over the in-scope
        # findings: without it, a diff cannot tell a document that got worse from a document
        # measured against fewer criteria, and reports the second as the first.
        #
        # Recorded even when None (no restriction), because "unset" is a fact about the scan
        # worth keeping — absent, a reader cannot distinguish an unrestricted scan from one that
        # predates this field.
        from store import scope_as_json
        scope_out["scan_scope"] = scope_as_json(scope_files)

    # THE EXCLUSIONS ARE PART OF THE BOUNDARY. A scan of "/Programme except /Programme/Archive"
    # covers less than its included paths suggest, and a reader comparing two runs of the same
    # folder cannot see why one is smaller unless the carve-out is recorded alongside the count.
    # Written for every source that honoured them, so the UI renders one rule rather than two.
    if scope_out is not None and excl:
        scope_out["excluded"] = sorted(excl)
    if scope_files is not None:
        from store import file_in_scope       # module-level idiom here: avoids a circular import
        _before = len(result)
        result = [it for it in result if file_in_scope(it.get("name") or "", scope_files)]
        _skipped = _before - len(result)
        if scope_out is not None:
            # REPORTED, NEVER SILENT. Narrowing the scope makes the estate smaller, and an
            # operator who cannot see why cannot tell a scoped scan from a source that lost
            # files. This codebase has been bitten repeatedly by a number that changed for a
            # reason nobody could see; it is also the line that answers "did you look at
            # everything?" in an audit.
            scope_out["skipped_out_of_scope"] = _skipped
            scope_out["kept"] = len(result)
    # `kept` is set by each branch above, BEFORE this pass, and stays correct because
    # _dedupe_names renames colliding names without ever dropping an item — so the count the UI
    # states beside the scope is the number of rows the UI will show. That is an invariant of
    # _dedupe_names rather than an obvious truth, so it is pinned by test
    # (test_discovery_scope_reported.py::test_kept_counts_rows_the_ui_will_show_…) instead of
    # re-derived here.
    return _dedupe_names(result)


def _folder_name(svc, folder_id: str) -> str | None:
    """The folder's display name, or None. Best-effort and deliberately non-fatal: this exists
    only so the UI can name the boundary, and a scan must never fail because a label lookup
    did."""
    try:
        meta = svc.files().get(fileId=folder_id, fields="name",
                               supportsAllDrives=True).execute(num_retries=3)
        return meta.get("name") or None
    except Exception:
        return None


def cache_source_bytes(tmp: Path, name: str, scan_id: str, user: str | None,
                       checksum: str | None = None) -> None:
    """ADR 0020: stash a just-downloaded file's original bytes in the blob source cache
    (the 'sources' container), so a later read (see read_cached_source) can skip a second
    Drive/SharePoint download of the same content. Keyed by `checksum` when the caller has
    one (a pre-download source checksum, e.g. Drive's md5Checksum) — a cache hit then spans
    ANY scan that re-encounters this same content, not just a retry of this one. Without a
    checksum (SharePoint, local, Google-native exports), falls back to {owner}/{scan_id}/
    {filename} — a same-scan-retry benefit only, ADR 0020 §1's original behavior. Strictly
    best-effort and non-blocking: a cache failure (or no blob configured) must never fail or
    slow a scan."""
    try:
        import blob
        if not blob.enabled():
            return
        blob.upload_source(user, scan_id, name, (tmp / name).read_bytes(), checksum=checksum)
    except Exception:
        swallowed("scanner.cache_source_bytes: caching the source bytes to blob storage failed", scan_id)


def read_cached_source(scan_id: str, name: str, user: str | None,
                       checksum: str | None = None) -> bytes | None:
    """ADR 0020 read side: return previously-cached original bytes for `name` (written by
    cache_source_bytes), checking the same key a write would use — `checksum` when given
    (so this can hit on a DIFFERENT scan's earlier download of the same content), else this
    scan's own scan_id/filename key (a same-scan retry/resume only). None on any cache miss,
    failure, or unconfigured blob store — the caller's existing download path is the fallback
    either way. Best-effort and non-blocking, matching cache_source_bytes."""
    try:
        import blob
        if not blob.enabled():
            return None
        return blob.download_source(user, scan_id, name, checksum=checksum)
    except Exception:
        return None


def _download(item: dict, dest: Path, svc=None, sp_token: str | None = None) -> None:
    out = dest / item["name"]
    if item.get("smb"):
        # SMB items carry a UNC `path` (\\server\share\...), which is NOT a local filesystem path —
        # the plain-`path` branch below would misread it. Route through the SMB transport instead.
        # fetch_smb is deployment-gated (ADR 0032/0036): until the live transport exists it raises a
        # clear error, so an SMB fetch fails loudly rather than silently producing an empty file.
        import smb_source
        out.write_bytes(smb_source.fetch_smb(item["path"]))
        return
    if "path" in item:
        out.write_bytes(Path(item["path"]).read_bytes())
        return
    if item.get("sp"):
        _sp_download(sp_token, item, dest)
        return
    from googleapiclient.http import MediaIoBaseDownload
    from googleapiclient.errors import HttpError
    buf = io.BytesIO()
    # Per-file memory guard: workers hold the whole file in RAM (+a copy) on a 1GiB
    # container, so a few large files in parallel can OOM the worker. Abort early past
    # the cap and let the file bucket as an error instead of taking the process down.
    max_bytes = int(os.environ.get("ACP_MAX_DOWNLOAD_MB", "150")) * 1024 * 1024
    if "mime" in item:
        # Google Workspace native — export as OOXML
        export_mime = EXPORT_MAP[item["mime"]][0]
        req = svc.files().export_media(fileId=item["id"], mimeType=export_mime)
    else:
        req = svc.files().get_media(fileId=item["id"], supportsAllDrives=True)
    dl = MediaIoBaseDownload(buf, req)
    done = False
    try:
        while not done:
            _, done = dl.next_chunk(num_retries=5)
            if buf.tell() > max_bytes:
                raise ValueError(f"file exceeds the {max_bytes // (1024 * 1024)}MB download "
                                 f"cap — skipped to protect worker memory (raise ACP_MAX_DOWNLOAD_MB)")
    except HttpError as e:
        # Google-native exports are capped at 10MB by Drive; surface it clearly rather
        # than as an opaque 403 so the file records a meaningful error.
        if getattr(e, "resp", None) is not None and e.resp.status == 403 and "exportSizeLimit" in str(e):
            raise ValueError("Google Doc/Sheet/Slide exceeds Drive's 10MB export limit — "
                             "can't export to Office format for analysis") from e
        raise
    out.write_bytes(buf.getvalue())


def _loc_fields(loc) -> tuple[int | None, str | None]:
    """Normalise an engine IssueLocation (pydantic model, PDF path) or the .NET CLI's JSON
    dict (Office path) into (page, location_text).

    page — the 1-based page OR slide the finding sits on; to a reviewer they're the same
    idea ("go here"). None when the analyser could not attribute one: we never invent a page.
    location_text — the analyser's structured hint for the object ('pptx:slide:0', an XPath).
    """
    if not loc:
        return None, None
    if isinstance(loc, dict):
        page = loc.get("pageNumber") or loc.get("slideNumber")
        text = loc.get("description") or loc.get("xPath")
    else:
        page = getattr(loc, "page_number", None) or getattr(loc, "slide_number", None)
        text = getattr(loc, "description", None) or getattr(loc, "x_path", None)
    return (page if isinstance(page, int) and page > 0 else None), (text or None)


def _issue_with_loc(base: dict, loc) -> dict:
    """Attach page/location to a finding only when the analyser actually reported them —
    absent keys mean 'unknown', never a fabricated page-1 default."""
    page, text = _loc_fields(loc)
    if page is not None:
        base["page"] = page
    if text:
        base["location"] = text
    return base


from assessment_selection import enabled as _sc_enabled, selected as _selected_scs


def _analyse_pdf(path: Path) -> dict:
    import asyncio
    sys.path.insert(0, str(WP))
    try:
        from analysers.pdf_analyser import PdfAnalyser, _RULES
        from models.manifest import AnalysisJob, FileType
    except ModuleNotFoundError as exc:
        # worker-python is not vendored — it is loaded at runtime from ACP_PDF_ENGINE. Bare, this
        # import raised mid-scan, and the resulting ModuleNotFoundError read as "the scan broke on
        # this file" when the real state is "a required engine was never installed on this host".
        # Every PDF in the estate then failed one at a time with the same opaque error.
        #
        # This does NOT invent a result: the file is reported as errored and un-analysed, exactly
        # as any other engine failure would be, so nothing is scored as passing. What changes is
        # that the message names the cause and where to fix it, and /readyz reports the same
        # condition BEFORE a scan starts. Vendoring the engine the way ADR 0012 vendored the
        # Office analysers is what actually closes this.
        return {"succeeded": False, "issues": [], "errors": [{
            "message": (f"PDF engine unavailable: {exc}. worker-python is loaded at runtime from "
                        f"ACP_PDF_ENGINE (currently {WP}); this host has no importable analyser "
                        f"there, so no PDF can be assessed. See /readyz."),
            "rule": None}]}
    from assessment_selection import allowed_rules
    allowed = allowed_rules()
    disabled = [] if allowed is None else [r.rule_id for r in _RULES if r.rule_id not in allowed]
    job = AnalysisJob(job_id=uuid.uuid4(), batch_run_id=uuid.uuid4(), file_id=uuid.uuid4(),
                      file_path=str(path), file_type=FileType.PDF, queue="pdf",
                      enqueued_at=datetime.now(timezone.utc), department_id=uuid.uuid4(), disabled_rule_ids=disabled)
    try:
        r = asyncio.run(PdfAnalyser().analyse(path, job))
        issues = [_issue_with_loc({"ruleId": i.rule_id, "wcag": i.wcag_criterion.name,
                                   "severity": i.severity.name}, getattr(i, "location", None))
                  for i in r.issues]
        issues = _pdf_correct_title(path, issues)
        return {"succeeded": r.succeeded,
                "issues": issues,
                "errors": [{"message": e.message, "rule": e.rule_id} for e in r.errors]}
    except Exception as e:
        return {"succeeded": False, "issues": [], "errors": [{"message": f"{type(e).__name__}: {e}", "rule": None}]}


def _pdf_correct_title(path: Path, issues: list[dict]) -> list[dict]:
    """The pdf.document-title rule (WCAG 2.4.2) reads /Title via pikepdf, whose docinfo
    READS are nondeterministic once libxml2 is loaded in a long-lived worker (office/HTML
    remediation use lxml) — so it can false-flag a PDF that actually declares a title.
    Re-read the title with pypdf (pure-Python, reliable) and drop the false finding.
    Mirror of why remediate_pdf writes metadata with pypdf."""
    if not any(i.get("ruleId") == "pdf.document-title" for i in issues):
        return issues
    try:
        from pypdf import PdfReader
        title = str((PdfReader(str(path)).metadata or {}).get("/Title") or "").strip()
    except Exception:
        return issues
    if title:
        return [i for i in issues if i.get("ruleId") != "pdf.document-title"]
    return issues


def _office_err(e: dict) -> dict:
    code = e.get("Code", "") if isinstance(e, dict) else ""
    rule = code[len("RULE_EXECUTION_ERROR_"):] if code.startswith("RULE_EXECUTION_ERROR_") else None
    msg = (e.get("message") or e.get("Message") or str(e)) if isinstance(e, dict) else str(e)
    return {"message": msg, "rule": rule}


def _cli_exit_reason(returncode: int) -> str:
    """Readable cause for a non-zero CLI exit. A NEGATIVE code is a signal, not a status."""
    if returncode < 0:
        try:
            name = signal.Signals(-returncode).name
        except ValueError:
            name = f"signal {-returncode}"
        return f"was killed by {name}"
    return f"exited with status {returncode}"


def _clip_diag(text: str, head: int = 800, tail: int = 400) -> str:
    """Keep BOTH ends of a diagnostic, because the two ends say different things.

    This used to be `stderr[-400:]`, and that single decision is why this crash went two
    flaggings without a diagnosis. A .NET failure writes the part you need FIRST —

        Unhandled exception. System.ArgumentException: <the actual cause> (Parameter 'name')
           at DocumentFormat.OpenXml.<...>
           at AcpScan.Analysers.XlsxAnalyser.AnalyseAsync(...)
           ... 30+ more frames

    — so a tail-only window is guaranteed to hold nothing but stack frames once the trace is
    longer than the window, which it always is. Production's log line read

        [scan] office CLI exited -6: ne)

    where `ne)` is the last three characters of something like `(Parameter 'name')`: the
    exception TYPE and MESSAGE, the only fields that identify the bug, fell off the front.
    Eight such lines were captured over 12h across both analysers and both container apps,
    and every one of them was unactionable for the same reason.

    The head is the larger window because that is where the answer is; the tail is kept
    because for a SIGABRT the runtime's own last words (a glibc `free(): invalid pointer`,
    an `Aborted`) arrive after the managed trace, and those identify a native fault that no
    managed exception header would mention.
    """
    text = (text or "").strip()
    if len(text) <= head + tail:
        return text
    return f"{text[:head]}\n  … {len(text) - head - tail} chars elided …\n{text[-tail:]}"


def _docx_body_readable(path: Path) -> bool:
    """True unless word/document.xml is missing or not well-formed XML.

    A docx whose main story part is corrupt or absent still opens as a zip and still yields its
    docProps/core.xml — so the office CLI reports the file with whatever metadata findings it can
    (typically just "no document title") and NO error, and the file lands as a nearly-clean
    `uncertain`. To a reviewer that reads like "almost fine, missing a title," when in fact the
    document's entire body could not be read. This is the affirmative check that catches it.

    A file that is not a valid zip at all (zero-byte, truncated header, a renamed .txt, or an
    encrypted/CFB Office file) is deliberately reported readable=True here: that case already
    buckets as engine-error upstream, and owning it here too would only duplicate the message."""
    import zipfile
    import xml.etree.ElementTree as ET
    try:
        with zipfile.ZipFile(path) as zf:
            if "word/document.xml" not in zf.namelist():
                return False
            data = zf.read("word/document.xml")
    except (zipfile.BadZipFile, OSError, KeyError):
        return True
    try:
        ET.fromstring(data)
        return True
    except ET.ParseError:
        return False


def _office_log_doc(dest: Path) -> str:
    """Return a safe correlation handle for legacy Office CLI diagnostics."""
    import joblog as _jl
    inputs = [p.name for p in dest.iterdir() if p.is_file() and p.name != "_o.json"]
    return _jl.doc_id(inputs[0]) if len(inputs) == 1 else f"batch:{len(inputs)}"


def _analyse_office(dest: Path, *, rule_allowlists=None) -> dict:
    log_doc = _office_log_doc(dest)
    out = dest / "_o.json"
    # DOTNET_ROOT only when that install actually exists, and never clobbering one the
    # environment already set (actions/setup-dotnet and the Docker image both set it
    # correctly). Forcing ~/.dotnet unconditionally pointed the muxer at a missing root
    # anywhere the SDK came from a package manager or CI action.
    _root = os.path.expanduser("~/.dotnet")
    env = {**os.environ, "DOTNET_CLI_TELEMETRY_OPTOUT": "1", "DOTNET_NOLOGO": "1"}
    if "DOTNET_ROOT" not in env and os.path.isdir(_root):
        env["DOTNET_ROOT"] = _root
    # Bounded: a hung dotnet process would otherwise stall the worker thread forever —
    # and since the fan-out scan finalizes only when every file reports in, one hang
    # froze the whole scan.
    #
    # An abnormal exit does NOT mean res stays {}. This comment used to claim it did, and
    # that assumption is what let a crash certify a document: AcpScan.Cli writes its JSON
    # in ONE File.WriteAllText after the whole loop (Program.cs), so a CLI that finishes
    # the sweep, writes complete output and THEN aborts during runtime shutdown leaves a
    # perfectly parseable file behind. Production hit exactly that on 2026-07-30 — both
    # .xlsx files logged `office CLI exited -6` (SIGABRT) and still scored 90-odd and
    # certified, because the only trace of the abort was this print.
    #
    # The "EMPTY stderr" this comment used to report was wrong, and wrong in a way worth
    # keeping a note about: stderr was NOT empty. A later sweep of ContainerAppConsoleLogs_CL
    # found 8 stack traces in 12h, on BOTH analysers (XlsxAnalyser 5, DocxAnalyser 3) and BOTH
    # container apps — so this is neither xlsx-specific nor one bad file. What made stderr
    # look empty was the old `[-400:]` tail clip eating the whole line; see _clip_diag.
    #
    # So the exit status is now part of the result. Three outcomes, all honest:
    #   * abnormal exit + parseable output -> every file the CLI reported gains a
    #     process-level error, which the rubric turns into `uncertain`: findings kept,
    #     score marked an upper bound, `compliant` forced False (scripts/rubric.py).
    #   * output missing or unparseable    -> res stays {} and the files bucket as
    #     engine-error (unanalysable) via the caller's "no engine result" substitution.
    #   * clean exit                       -> unchanged.
    timeout_s = int(os.environ.get("ACP_OFFICE_CLI_TIMEOUT", "180"))
    aborted: str | None = None
    try:
        from assessment_selection import allowed_rules
        allowed = allowed_rules()
        command = [DOTNET, str(CLI_DLL), str(dest), str(out)]
        if rule_allowlists is not None:
            command.append(json.dumps(rule_allowlists))
        elif allowed is not None:
            command.append(json.dumps({"*": allowed}))
        proc = subprocess.run(command,
                              capture_output=True, text=True, env=env, timeout=timeout_s)
        if proc.returncode != 0:
            aborted = _cli_exit_reason(proc.returncode)
            # stdout as well as stderr, and both clipped from BOTH ends. A signal death is not
            # a managed exception exit, so there is no promise about which stream carries the
            # cause: .NET writes an unhandled exception to stderr, but the runtime's own
            # shutdown-time aborts and `Fatal error.` lines have been seen on stdout, and a
            # glibc abort writes straight to fd 2 underneath both. #109 recorded EMPTY stderr
            # for this same crash — if that was accurate rather than an artefact of the old
            # tail-only clip, whatever the process did say went to stdout and was never read.
            # Printing an empty stream would just be noise, so each is included only if present.
            streams = [n for n, s in (("stderr", proc.stderr), ("stdout", proc.stdout))
                       if (s or "").strip()]
            print(f"[scan] office CLI {aborted} ({proc.returncode}); doc={log_doc}; "
                  f"diagnostic_streams={','.join(streams) or 'none'}", flush=True)
    except subprocess.TimeoutExpired:
        # A timeout is not automatically engine-error either, for the same reason: the CLI may
        # have written its output and then hung on the way out. Whichever it did, the outcome is
        # logged below once we know, rather than guessed at here.
        aborted = f"timed out after {timeout_s}s"
        print(f"[scan] office CLI timed out after {timeout_s}s; doc={log_doc}", flush=True)
    except OSError as e:
        # The CLI could not be LAUNCHED at all — no dotnet on PATH, DOTNET pointing at nothing,
        # the dll absent, the binary not executable. Every other CLI failure above is already
        # handled as a result rather than an exception, and `analyse_and_assess`'s docstring
        # states the contract in terms: "Engines catch their own errors and return an error
        # result rather than raising." This branch was the one path that broke it.
        #
        # It is not a theoretical gap, and its blast radius is not the scan it kills. The
        # residual re-scan that decides whether a reviewer-approved value may be CREDITED
        # (proposals.verify_residual_scs) wraps its whole body in `except Exception: return
        # None`, and `_apply_one_value_kind` reads None as "cleared" so an infra hiccup never
        # penalises remediation. Chain those two and a host with no analyser credits every
        # approved value and certifies the file, on a re-scan that never ran — the file is
        # published as fixed having been checked by nothing.
        #
        # Degrading here restores what the surrounding code already does for a crash, an abort
        # and unparseable output: `res` stays {}, the caller substitutes "no engine result", and
        # the files bucket as engine-error. The first-party lanes in analyse_and_assess run
        # AFTER this call, each in its own try/except, so they still report — which is why a
        # 2.4.4 finding (office_structure, pure Python) survives a missing .NET runtime and can
        # be verified on a host that has none.
        aborted = f"could not be launched ({type(e).__name__})"
        print(f"[scan] office CLI could not be launched; doc={log_doc}; "
              f"error_type={type(e).__name__} — "
              f"files will be recorded as engine-error", flush=True)
    res = {}
    if out.exists():
        # A truncated write is the other half of the same crash, and json.loads raises on it.
        # That exception used to escape _analyse_office uncaught: run_scan wraps its body in a
        # bare `finally`, so one aborted CLI failed the ENTIRE scan rather than the files it
        # analysed. Unparseable output is missing output — bucket it as engine-error.
        try:
            items = json.loads(out.read_text())
        except (json.JSONDecodeError, UnicodeDecodeError, OSError) as e:
            print(f"[scan] office CLI output is unreadable; doc={log_doc}; "
                  f"error_type={type(e).__name__} — "
                  f"files will be recorded as engine-error", flush=True)
            return {}
        for item in items:
            res[item["file"]] = {
                "succeeded": item["succeeded"],
                # Keep the engine's human-readable per-finding title as `detail` — it's
                # the only evidence of WHY a finding fired ("Slide is missing a title",
                # "Reading order may not match visual order"). Dropping it left the UI
                # and traces showing a bare rule id with no explanation.
                "issues": [_issue_with_loc({"ruleId": i["ruleId"], "wcag": i["wcag"],
                                            "severity": i["severity"],
                                            **({"detail": i["title"]} if i.get("title") else {})},
                                           i.get("location"))
                           for i in item.get("issues", [])],
                "errors": [_office_err(e) for e in item.get("errors", [])],
            }
    if aborted:
        # `rule: None` deliberately — a process-level abort is not attributable to one rule, and
        # store.py's per-rule attribution already skips errors without a rule id. It still counts
        # toward skipped_rules, which is what makes the score read as an upper bound in the UI.
        #
        # succeeded is left as the CLI reported it: flipping it to False would score these files
        # `error` / "n/a (could not analyse)", throwing away findings that ARE real (production's
        # two files carried 2 and 5) and describing a run that produced findings as one that
        # produced nothing. `uncertain` is the truthful reading — we have findings, we cannot
        # claim they are the complete set — and it blocks certification just as firmly.
        for entry in res.values():
            entry["errors"] = [*entry["errors"],
                               {"message": f"office analyser {aborted} before exiting cleanly; "
                                           f"findings may be an incomplete set", "rule": None}]
        # Say what the abort actually cost, rather than predicting it before parsing: files the
        # CLI reported go uncertain, everything else in this batch falls to engine-error.
        print(f"[scan] office CLI {aborted} — {len(res)} reported file(s) recorded as uncertain "
              f"(score is an upper bound, not certifiable)", flush=True)
    _flag_unreadable_docx(dest, res)
    return res


UNREADABLE_BODY_MSG = ("The document's main content (word/document.xml) could not be read — the "
                       "file may be corrupt, incomplete, or password-protected. It could not be "
                       "assessed for accessibility; any findings shown are from its metadata only.")


def _flag_unreadable_docx(dest: Path, res: dict) -> None:
    """Append an explicit unreadable-content engine error to any reported docx whose body is
    missing or malformed, so it stays non-certifiable WITH a reason instead of a silent, nearly-
    clean `uncertain`. Mutates `res` in place; the message rides the same errors channel a CLI
    abort uses. (A file that is not a valid zip at all is already engine-error upstream.)"""
    for fname in list(res.keys()):
        fp = dest / fname
        if fp.suffix.lower() != ".docx" or not fp.exists():
            fp = dest / Path(fname).name  # in case the CLI reported a nested/relative path
        if fp.suffix.lower() == ".docx" and fp.exists() and not _docx_body_readable(fp):
            res[fname]["errors"] = [*res[fname].get("errors", []),
                                    {"message": UNREADABLE_BODY_MSG, "rule": None}]
            import joblog as _jl
            print(f"[scan] {_jl.doc_id(fname) or 'unknown'}: "
                  f"word/document.xml missing or malformed — recorded as "
                  f"uncertain with an explicit unreadable-content error", flush=True)


_VAGUE_LINK_TEXT = frozenset({"click here", "here", "read more", "more", "link", "this", "click", "learn more", "details"})

# ── 1.3.1 — text visually styled as a heading but left as body text ───────────
# The html mirror of office_structure's DOCX_PSEUDO_HEADING, filed under the SAME criterion.
# A <p>/<div> set large and bold but never marked up as a heading announces "this starts a
# section" through presentation alone, so it is absent from the outline assistive tech
# navigates by — that is 1.3.1 Info and Relationships.
#
# It is NOT 2.4.6, which is where frontend/src/rules/wcag-2-4-6.js used to file it: 2.4.6
# asks whether headings that EXIST describe their topic, and this element is not a heading
# at all yet. html and docx disagreed on the taxonomy for the one identical signal; filing
# both under 1.3.1 is what makes a cross-format report add up.
#
# The predicate is SHARED with the remediator (api/remediate.py imports it) so the fix
# promotes exactly what this flags and the re-scan verifiably clears — the same lock-step
# requirement office_structure documents, for the same reason: the fix auto-applies, so a
# false positive silently restyles real body text as a heading.
_PSEUDO_HEADING_MIN_PX = 18
_PSEUDO_HEADING_MAX_CHARS = 50
_FONT_SIZE_PX = re.compile(r"font-size:\s*(\d+(?:\.\d+)?)\s*px", re.I)
_FONT_WEIGHT_BOLD = re.compile(r"font-weight:\s*(?:bold|[6-9]00)\b", re.I)


def html_pseudo_headings(root) -> list:
    """<p>/<div> elements that read as a heading but are not marked up as one.

    Detection is the frontend module's original heuristic — a childless <p>/<div> whose
    inline style is at least 18px AND bold, holding short non-sentence text. Deliberately
    unchanged in sensitivity: refiling the criterion is one question, and how eagerly to
    promote body text is another, so this move does not quietly loosen the trigger.

    The one addition is office_structure.looks_like_heading_furniture, which the docx and
    PDF heading scans already share. "Big and bold" describes a cover-page wordmark, a
    headline figure and a pull quote just as well as a section title, and the fix writes
    without review — so html now rejects the same furniture the other formats do instead of
    promoting "$4.2B" to an <h2>.
    """
    try:
        import office_structure as _off_mod
        _furniture = _off_mod.looks_like_heading_furniture
    except Exception:                       # office deps absent — fall back to no filter
        _furniture = lambda _t: False       # noqa: E731
    out = []
    for el in root.iter("p", "div"):
        if len(el):                         # leaf text only, as the frontend module had it
            continue
        text = (el.text_content() or "").strip()
        if not text or len(text) > _PSEUDO_HEADING_MAX_CHARS or text[-1] in ".?!":
            continue
        style = el.get("style") or ""
        size = _FONT_SIZE_PX.search(style)
        if not size or float(size.group(1)) < _PSEUDO_HEADING_MIN_PX:
            continue
        if not _FONT_WEIGHT_BOLD.search(style):
            continue
        if _furniture(text):
            continue
        out.append(el)
    return out

# Phase 1 HTML-check helpers — patterns mirror frontend/src/rules/ modules.
_INPUT_PURPOSE = re.compile(
    r"e-?mail|(^|[\s_-])(tel|phone|mobile)([\s_-]|$)|(first|given)[-_ ]?name|(last|family|sur)[-_ ]?name"
    r"|full[-_ ]?name|(^|[\s_-])(zip|postal)([-_ ]?code)?([\s_-]|$)|(^|[\s_-])country([\s_-]|$)"
    r"|(^|[\s_-])(city|town)([\s_-]|$)|street|address|(^|[\s_-])(org|organization|company)([\s_-]|$)"
    r"|birth[-_ ]?(date|day)|(^|[\s_-])dob([\s_-]|$)", re.I)
_ORIENTATION_LOCK = re.compile(
    r"@media[^{]*\(\s*orientation\s*:\s*(?:portrait|landscape)\s*\)[^{]*\{[^@]*?display\s*:\s*none", re.I)
_INLINE_COLOR = re.compile(r"(?:^|[^-])color:\s*#([0-9a-fA-F]{3}|[0-9a-fA-F]{6})\b")


def _luma(h: str) -> float:
    """Relative luma of a hex color (0..1); >0.45 ≈ fails the 7:1 enhanced ratio on white."""
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    return (0.299 * r + 0.587 * g + 0.114 * b) / 255


def _analyse_html(path: Path) -> dict:
    try:
        from lxml import html as lx
        root = lx.fromstring(path.read_bytes(), base_url=str(path))
    except Exception as e:
        return {"succeeded": False, "issues": [], "errors": [{"message": f"{type(e).__name__}: {e}", "rule": None}]}

    issues: list[dict] = []
    _style_blob = " ".join(
        [el.get("style", "") for el in root.iter() if el.get("style")]
        + [s.text_content() for s in root.iter("style") if s.text_content()]
    ).lower()


    labelled_ids: set[str] = set()
    for label in root.iter("label"):
        for_attr = label.get("for")
        if for_attr:
            labelled_ids.add(for_attr)

    if _sc_enabled("2.4.2"):
        # 2.4.2 Page Titled — missing or empty <title>
        titles = root.findall(".//title")
        if not titles or not (titles[0].text or "").strip():
            issues.append({"ruleId": "HTML_MISSING_TITLE", "wcag": "2.4.2 Page Titled", "severity": "SERIOUS"})

    if _sc_enabled("3.1.1"):
        # 3.1.1 Language of Page — missing lang on <html>
        # lxml.html.fromstring returns the root element (html or body depending on fragment)
        html_el = root if root.tag == "html" else root.find(".//html") or root
        lang = html_el.get("lang") or html_el.get("{http://www.w3.org/XML/1998/namespace}lang")
        if not lang:
            issues.append({"ruleId": "HTML_MISSING_LANG", "wcag": "3.1.1 Language of Page", "severity": "SERIOUS"})

    if _sc_enabled("1.1.1"):
        # 1.1.1 Non-text Content — <img> without alt attribute (decorative: role=presentation is ok)
        for img in root.iter("img"):
            if img.get("alt") is None and img.get("role", "") not in ("presentation", "none"):
                issues.append({"ruleId": "HTML_IMG_MISSING_ALT", "wcag": "1.1.1 Non-text Content", "severity": "SERIOUS"})

    if _sc_enabled("2.4.4"):
        # 2.4.4 Link Purpose (In Context) — empty or vague <a> text
        for a in root.iter("a"):
            text = (a.text_content() or "").strip()
            aria = (a.get("aria-label") or a.get("title") or "").strip()
            if not text and not aria:
                issues.append({"ruleId": "HTML_EMPTY_LINK", "wcag": "2.4.4 Link Purpose (In Context)", "severity": "SERIOUS"})
            elif text.lower() in _VAGUE_LINK_TEXT and not aria:
                issues.append({"ruleId": "HTML_VAGUE_LINK", "wcag": "2.4.4 Link Purpose (In Context)", "severity": "MODERATE"})

    if _sc_enabled("2.4.9"):
        # 2.4.9 Link Purpose (Link Only) — text alone must convey purpose, with no
        # credit for surrounding context. Its genuine failure case: identical link
        # text pointing at different real destinations (2.4.4 tolerates this — context
        # sorts it out — 2.4.9 doesn't). href="#" is excluded — a common JS-hook
        # placeholder, not a real distinct destination. Vague text (2.4.4's list)
        # fails "text alone" regardless of context, so it's flagged here too.
        link_groups: dict[str, set[str]] = {}
        for a in root.iter("a"):
            href = a.get("href")
            n = (a.get("aria-label") or a.text_content() or "").strip().lower()
            if not href or href == "#" or not n:
                continue
            link_groups.setdefault(n, set()).add(href)
        ambiguous_hrefs = {h for hrefs in link_groups.values() if len(hrefs) > 1 for h in hrefs}
        for a in root.iter("a"):
            href = a.get("href")
            text = (a.text_content() or "").strip()
            aria = (a.get("aria-label") or "").strip()
            vague = text.lower() in _VAGUE_LINK_TEXT and not aria
            duplicated = bool(href) and href != "#" and href in ambiguous_hrefs
            if vague or duplicated:
                issues.append({"ruleId": "HTML_LINK_PURPOSE_AMBIGUOUS", "wcag": "2.4.9 Link Purpose (Link Only)", "severity": "MODERATE"})

    if _sc_enabled("2.4.6"):
        # 2.4.6 Headings and Labels — skipped heading levels (e.g. h1 → h3). EVERY gap is reported,
        # not just the first: _fix_heading_skip closes them all in one pass, so stopping at the first
        # understated the work and left a page looking one edit away from clean when it was several.
        # `prev_level` advances to the level actually in the document (not the clamped prev+1), which
        # is what keeps h1→h3→h4 a single finding — the outline has one gap there, and the h3→h4 step
        # is well-formed. Mirrors office_structure.docx_checks' DOCX_HEADING_SKIP.
        HEADING_TAGS = {"h1", "h2", "h3", "h4", "h5", "h6"}
        prev_level = 0
        ordinal = 0
        for el in root.iter():
            if el.tag in HEADING_TAGS:
                level = int(el.tag[1])
                ordinal += 1
                if prev_level > 0 and level > prev_level + 1:
                    # The ordinal keeps two identical gaps (two separate h1→h3s) distinguishable.
                    issues.append({"ruleId": "HTML_HEADING_SKIP", "wcag": "2.4.6 Headings and Labels",
                                   "severity": "MODERATE",
                                   "detail": f"Heading {ordinal}: level jumps from h{prev_level} to h{level} "
                                             f"(should step to h{prev_level + 1})"})
                prev_level = level

    if _sc_enabled("1.3.2"):
        # 1.3.2 Meaningful Sequence — CSS that visually reorders content away from source order
        # (flex 'order', reversed flex direction/flow), or an image-replacement technique (a large
        # negative text-indent hiding real text behind a background image). A screen reader follows
        # source order, so any of these can make the spoken order differ from the visual one.
        if re.search(r"order\s*:\s*-?[1-9]", _style_blob) or re.search(r"flex-(?:direction|flow)\s*:[^;]*reverse", _style_blob):
            issues.append({"ruleId": "HTML_VISUAL_REORDER", "wcag": "1.3.2 Meaningful Sequence", "severity": "MODERATE"})

    if _sc_enabled("1.4.5"):
        # 1.4.5 Images of Text — an <img> whose file name signals it carries text, or a CSS
        # image-replacement technique (large negative text-indent). Conservative: only these
        # unambiguous signals fire, so an ordinary photo/diagram is never mistaken for text.
        _TEXT_IMG_NAME = re.compile(
            r"\b(heading|headline|banner|title|quote|wordmark|slogan|tagline|typography|text)\b", re.I)
        _imgtext = any(_TEXT_IMG_NAME.search((img.get("src") or "").rsplit("/", 1)[-1]) for img in root.iter("img"))
        if not _imgtext and re.search(r"text-indent\s*:\s*-\s*(?:9{3,}|\d{4,})", _style_blob):
            _imgtext = True
        if _imgtext:
            issues.append({"ruleId": "HTML_IMAGE_OF_TEXT", "wcag": "1.4.5 Images of Text", "severity": "MODERATE"})

    if _sc_enabled("4.1.2"):
        # 4.1.2 Name, Role, Value — <input> without an associated label
        SKIP_INPUT_TYPES = {"hidden", "submit", "button", "image", "reset"}
        for inp in root.iter("input"):
            if (inp.get("type") or "text").lower() in SKIP_INPUT_TYPES:
                continue
            if not (inp.get("aria-label") or inp.get("aria-labelledby") or inp.get("title")):
                if inp.get("id", "") not in labelled_ids:
                    issues.append({"ruleId": "HTML_INPUT_NO_LABEL", "wcag": "4.1.2 Name, Role, Value", "severity": "CRITICAL"})

        # ── Phase 1 additions — predicates mirror frontend/src/rules/wcag-*.js so a
        # file remediated client-side re-scans clean here.

    if _sc_enabled("1.4.2"):
        # 1.4.2 Audio Control — autoplaying media with no way to stop it
        for m in root.iter("audio", "video"):
            if m.get("autoplay") is not None and m.get("controls") is None:
                issues.append({"ruleId": "HTML_AUTOPLAY_MEDIA", "wcag": "1.4.2 Audio Control", "severity": "SERIOUS"})

    if _sc_enabled("1.3.5"):
        # 1.3.5 Identify Input Purpose — recognizable personal-data inputs without autocomplete
        for inp in root.iter("input"):
            if inp.get("autocomplete"):
                continue
            hint = f'{inp.get("name") or ""} {inp.get("id") or ""} {inp.get("placeholder") or ""}'
            if (inp.get("type") or "").lower() in ("email", "tel") or _INPUT_PURPOSE.search(hint):
                issues.append({"ruleId": "HTML_INPUT_NO_AUTOCOMPLETE", "wcag": "1.3.5 Identify Input Purpose", "severity": "MODERATE"})

    if _sc_enabled("2.5.3"):
        # 2.5.3 Label in Name — accessible name omits the visible label text
        for el in list(root.iter("a")) + list(root.iter("button")):
            aria = re.sub(r"\s+", " ", el.get("aria-label") or "").strip().lower()
            if not aria:
                continue
            visible = re.sub(r"\s+", " ", el.text_content() or "").strip().lower()[:80]
            if visible and visible not in aria:
                issues.append({"ruleId": "HTML_LABEL_NOT_IN_NAME", "wcag": "2.5.3 Label in Name", "severity": "SERIOUS"})

    if _sc_enabled("2.4.1"):
        # 2.4.1 Bypass Blocks — repeated chrome (nav/header) with no skip mechanism
        roles = {el.get("role") for el in root.iter() if callable(getattr(el, "get", None)) and el.get("role")}
        has_chrome = (root.find(".//nav") is not None or root.find(".//header") is not None
                      or roles & {"navigation", "banner"})
        if has_chrome:
            has_main = root.find(".//main") is not None or "main" in roles
            ids = {el.get("id") for el in root.iter() if callable(getattr(el, "get", None)) and el.get("id")}
            has_skip = any((a.get("href") or "").startswith("#") and (a.get("href") or "")[1:] in ids
                           for a in root.iter("a"))
            if not (has_main or has_skip):
                issues.append({"ruleId": "HTML_NO_SKIP_LINK", "wcag": "2.4.1 Bypass Blocks", "severity": "MODERATE"})

    if _sc_enabled("3.3.2"):
        # 3.3.2 Labels or Instructions — required field with no guidance at all
        for tag in ("input", "select", "textarea"):
            for inp in root.iter(tag):
                if inp.get("required") is None:
                    continue
                if (inp.get("aria-label") or inp.get("aria-labelledby") or inp.get("aria-describedby")
                        or inp.get("title") or inp.get("placeholder")):
                    continue
                if next(inp.iterancestors("label"), None) is not None or inp.get("id", "") in labelled_ids:
                    continue
                issues.append({"ruleId": "HTML_REQUIRED_NO_GUIDANCE", "wcag": "3.3.2 Labels or Instructions", "severity": "SERIOUS"})

    if _sc_enabled("1.3.4"):
        # 1.3.4 Orientation — content hidden in one orientation ("rotate your device" lock)
        for st in root.iter("style"):
            if _ORIENTATION_LOCK.search(st.text_content() or ""):
                issues.append({"ruleId": "HTML_ORIENTATION_LOCK", "wcag": "1.3.4 Orientation", "severity": "MODERATE"})

    if _sc_enabled("1.4.6"):
        # 1.4.6 Contrast (Enhanced, AAA) — inline colors that pass 4.5:1 but miss 7:1
        for el in root.iter():
            style = el.get("style") if callable(getattr(el, "get", None)) else None
            if not style:
                continue
            m = _INLINE_COLOR.search(style)
            if m and _luma(m.group(1)) > 0.45:
                issues.append({"ruleId": "HTML_LOW_CONTRAST_AAA", "wcag": "1.4.6 Contrast (Enhanced)", "severity": "MODERATE"})

        # ── Phase 2 — media alternatives + target size (detect + route to HITL) ──

    if _sc_enabled("1.2.2") or _sc_enabled("1.2.3"):
        # 1.2.2 Captions — <video> with no captions/subtitles track
        # Audio Description — <video> with no descriptions track and no transcript
        for v in root.iter("video"):
            kinds = {(t.get("kind") or "").lower() for t in v.iter("track")}
            if _sc_enabled("1.2.2") and not (kinds & {"captions", "subtitles"}):
                issues.append({"ruleId": "HTML_VIDEO_NO_CAPTIONS", "wcag": "1.2.2 Captions (Prerecorded)", "severity": "SERIOUS"})
            if _sc_enabled("1.2.3") and "descriptions" not in kinds and not v.get("aria-describedby"):
                issues.append({"ruleId": "HTML_VIDEO_NO_DESCRIPTION", "wcag": "1.2.3 Audio Description or Media Alternative", "severity": "SERIOUS"})

    if _sc_enabled("1.2.1"):
        # 1.2.1 Audio-only — <audio> with no linked/naming transcript
        for a in root.iter("audio"):
            if not (a.get("aria-describedby") or re.search(r"transcript", a.get("aria-label") or "", re.I)):
                issues.append({"ruleId": "HTML_AUDIO_NO_TRANSCRIPT", "wcag": "1.2.1 Audio-only & Video-only (Prerecorded)", "severity": "SERIOUS"})

    if _sc_enabled("2.5.8"):
        # 2.5.8 Target Size — interactive element with an inline px dimension < 24
        for el in root.iter():
            tag = el.tag if isinstance(el.tag, str) else ""
            role = (el.get("role") or "") if callable(getattr(el, "get", None)) else ""
            interactive = (tag in ("a", "button", "input", "select", "textarea")
                           or role == "button" or el.get("onclick") is not None)
            if not interactive:
                continue
            if tag == "a" and not el.get("href"):
                continue
            style = el.get("style") or ""
            dims = [float(m) for m in re.findall(r"(?:^|;)\s*(?:width|height)\s*:\s*([\d.]+)px", style, re.I)]
            if any(d < 24 for d in dims):
                issues.append({"ruleId": "HTML_TARGET_TOO_SMALL", "wcag": "2.5.8 Target Size (Minimum)", "severity": "SERIOUS"})

        # ── Phase 4 — legacy rule modules with no prior backend mirror ──
        # These SCs have a frontend rule module (used for the client-side remediation
        # preview) and read as 'Shipped (demo)' in the catalog, but a real server-side
        # scan never actually evaluated them — every persisted scan silently showed
        # PASS for every HTML file, regardless of content. Ported here to close that
        # false-PASS gap; predicates mirror frontend/src/rules/wcag-*.js exactly.

    if _sc_enabled("1.3.1"):
        # 1.3.1 Info and Relationships — form control with no accessible name at all
        # (broader than 4.1.2's check above: also credits implicit <label>wrapping).
        SELF_NAMED_TYPES = {"hidden", "button", "submit", "reset", "image"}
        for inp in root.iter("input", "select", "textarea"):
            if inp.tag == "input" and (inp.get("type") or "text").lower() in SELF_NAMED_TYPES:
                continue
            if inp.get("aria-label") or inp.get("aria-labelledby"):
                continue
            if next(inp.iterancestors("label"), None) is not None:
                continue
            if inp.get("id", "") in labelled_ids:
                continue
            issues.append({"ruleId": "HTML_FORM_CONTROL_NO_NAME", "wcag": "1.3.1 Info and Relationships", "severity": "CRITICAL"})

    if _sc_enabled("1.3.1"):
        # 1.3.1 — text styled to look like a heading but never marked up as one. One finding per
        # element (docx emits one per document; html can carry many sections on one page, and the
        # fix promotes each, so under-reporting here would understate the work the way the 2.4.6
        # `break` did). See html_pseudo_headings for why this is 1.3.1 and not 2.4.6.
        for el in html_pseudo_headings(root):
            issues.append({"ruleId": "HTML_PSEUDO_HEADING", "wcag": "1.3.1 Info and Relationships",
                           "severity": "MODERATE",
                           "detail": f"Text styled as a heading but left as <{el.tag}>: "
                                     f"{(el.text_content() or '').strip()[:60]}"})

    if _sc_enabled("1.4.1"):
        # 1.4.1 Use of Color — link styled by inline color alone, no underline
        for a in root.iter("a"):
            style = a.get("style") or ""
            if not style:
                continue
            if re.search(r"(?:^|;)\s*color\s*:", style) and not re.search(r"text-decoration\s*:[^;]*underline", style, re.I):
                issues.append({"ruleId": "HTML_LINK_COLOR_ONLY", "wcag": "1.4.1 Use of Color", "severity": "SERIOUS"})

    if _sc_enabled("1.4.3"):
        # 1.4.3 Contrast (Minimum, AA) — inline color likely below 4.5:1 (luma > 0.62).
        # Same _INLINE_COLOR/_luma helpers as 1.4.6 above; AA's threshold is looser.
        for el in root.iter():
            style = el.get("style") if callable(getattr(el, "get", None)) else None
            if not style:
                continue
            m = _INLINE_COLOR.search(style)
            if m and _luma(m.group(1)) > 0.62:
                issues.append({"ruleId": "HTML_LOW_CONTRAST_AA", "wcag": "1.4.3 Contrast (Minimum)", "severity": "SERIOUS"})

    if _sc_enabled("1.4.4"):
        # 1.4.4 Resize Text — viewport meta blocks pinch-zoom / text resize
        for meta in root.iter("meta"):
            if (meta.get("name") or "").lower() != "viewport":
                continue
            content = meta.get("content") or ""
            if re.search(r"user-scalable\s*=\s*(no|0)|maximum-scale\s*=\s*(0|1)(\.0+)?\b", content, re.I):
                issues.append({"ruleId": "HTML_VIEWPORT_BLOCKS_ZOOM", "wcag": "1.4.4 Resize Text", "severity": "SERIOUS"})

    if _sc_enabled("1.4.10"):
        # 1.4.10 Reflow — a real page (has meta/link/style) with no responsive viewport
        has_head_content = any(next(root.iter(t), None) is not None for t in ("meta", "link", "style"))
        has_viewport = any((m.get("name") or "").lower() == "viewport" for m in root.iter("meta"))
        if has_head_content and not has_viewport:
            issues.append({"ruleId": "HTML_NO_VIEWPORT_REFLOW", "wcag": "1.4.10 Reflow", "severity": "SERIOUS"})

    if _sc_enabled("1.4.11"):
        # 1.4.11 Non-text Contrast (AA) — inline border color likely below 3:1
        for el in root.iter():
            style = el.get("style") if callable(getattr(el, "get", None)) else None
            if not style:
                continue
            m = re.search(r"border(?:-[a-z]+)?:[^;]*?#([0-9a-fA-F]{3}|[0-9a-fA-F]{6})\b", style, re.I)
            if m and _luma(m.group(1)) > 0.62:
                issues.append({"ruleId": "HTML_BORDER_LOW_CONTRAST", "wcag": "1.4.11 Non-text Contrast", "severity": "MODERATE"})

    if _sc_enabled("1.4.12"):
        # 1.4.12 Text Spacing — fixed-pixel line-height blocks the user's spacing override
        for el in root.iter():
            style = el.get("style") if callable(getattr(el, "get", None)) else None
            if style and re.search(r"line-height:\s*\d+px", style, re.I):
                issues.append({"ruleId": "HTML_FIXED_LINE_HEIGHT", "wcag": "1.4.12 Text Spacing", "severity": "MODERATE"})

    if _sc_enabled("2.4.3"):
        # 2.4.3 Focus Order — positive tabindex overrides natural reading order
        for el in root.iter():
            ti = el.get("tabindex") if callable(getattr(el, "get", None)) else None
            if ti is None:
                continue
            try:
                if int(ti) > 0:
                    issues.append({"ruleId": "HTML_POSITIVE_TABINDEX", "wcag": "2.4.3 Focus Order", "severity": "SERIOUS"})
            except ValueError:
                continue

    if _sc_enabled("2.4.7"):
        # 2.4.7 Focus Visible — outline suppressed (CSS or inline) with interactive content present
        css_blocks = "\n".join((s.text_content() or "") for s in root.iter("style"))
        inline_suppressed = any(
            re.search(r"outline:\s*(none|0)\b", el.get("style") or "", re.I)
            for el in root.iter() if callable(getattr(el, "get", None)) and el.get("style")
        )
        outline_suppressed = bool(re.search(r"outline:\s*(none|0)\b", css_blocks, re.I)) or inline_suppressed
        has_interactive = next(root.iter("a", "button", "input", "select", "textarea"), None) is not None
        if outline_suppressed and has_interactive:
            issues.append({"ruleId": "HTML_FOCUS_OUTLINE_SUPPRESSED", "wcag": "2.4.7 Focus Visible", "severity": "SERIOUS"})

    if _sc_enabled("3.1.4"):
        # 3.1.4 Abbreviations — known abbreviation with no <abbr title> expansion.
        # ABBR mirrors frontend/src/rules/utils.js's ABBR dict (a small editorial
        # glossary) — kept in sync by hand, same posture as RULE_CATALOG vs PLAIN_NAMES.
        ABBR = {
            "WCAG": "Web Content Accessibility Guidelines", "ADA": "Americans with Disabilities Act",
            "PDF": "Portable Document Format", "PPO": "Preferred Provider Organization",
            "HDHP": "High-Deductible Health Plan", "FSA": "Flexible Spending Account",
            "HSA": "Health Savings Account", "FAQ": "Frequently Asked Questions",
            "PII": "Personally Identifiable Information", "UTSW": "UT Southwestern", "HR": "Human Resources",
        }
        ABBR_RE = re.compile(r"\b(" + "|".join(ABBR) + r")\b")
        SKIP_ANCESTOR_TAGS = {"abbr", "script", "style", "title"}
        for el in root.iter():
            tag = el.tag if isinstance(el.tag, str) else ""
            if tag in SKIP_ANCESTOR_TAGS or any(a.tag in SKIP_ANCESTOR_TAGS for a in el.iterancestors()):
                continue
            for text in (el.text, el.tail):
                if text and ABBR_RE.search(text):
                    issues.append({"ruleId": "HTML_UNEXPANDED_ABBR", "wcag": "3.1.4 Abbreviations", "severity": "MINOR"})

    return {"succeeded": True, "issues": issues, "errors": []}


def detect_acp_stamp(path: Path, ext: str) -> str | None:
    """Detect a prior Mova.io ACP remediation stamp in a file (the provenance written
    by the remediators); return the remediation date, 'yes' if undated, else None.
    Lets a scan flag already-remediated documents."""
    try:
        if ext in OFFICE:
            with zipfile.ZipFile(path) as z:
                if "docProps/custom.xml" in z.namelist():
                    x = z.read("docProps/custom.xml").decode("utf-8", "replace")
                    if "Mova.io ACP" in x:
                        m = re.search(r"Remediation Date.*?<vt:lpwstr>([^<]+)</vt:lpwstr>", x, re.S)
                        return m.group(1).strip() if m else "yes"
        elif ext == ".pdf":
            import pikepdf
            with pikepdf.open(str(path)) as pdf:
                rb = pdf.docinfo.get("/RemediatedBy")
                if rb is not None and "Mova.io ACP" in str(rb):
                    d = pdf.docinfo.get("/RemediationDate")
                    return str(d) if d is not None else "yes"
        elif ext in HTML_EXTS:
            txt = path.read_text("utf-8", errors="replace")[:4000]
            if "Mova.io ACP" in txt and ("generator" in txt or "Remediated by" in txt):
                m = re.search(r"remediated (\d{4}-\d\d-\d\d)", txt)
                return m.group(1) if m else "yes"
    except Exception:
        swallowed("scanner.detect_acp_stamp: detecting the ACP stamp failed")
    return None


def _file_extent(path: Path, ext: str) -> dict:
    """Cheap physical metadata for the file drawer: size always; page count for
    PDF (pikepdf — already a scan dependency), slide/sheet counts read straight
    from the OOXML zip directory. Never raises — metadata must never fail a scan."""
    out: dict = {}
    try:
        out["size_kb"] = max(1, round(path.stat().st_size / 1024))
    except Exception:
        return out
    try:
        if ext == ".pdf":
            import pikepdf
            with pikepdf.open(str(path)) as pdf:
                out["pages"] = len(pdf.pages)
        elif ext == ".pptx":
            with zipfile.ZipFile(path) as z:
                out["pages"] = sum(1 for n in z.namelist()
                                   if re.fullmatch(r"ppt/slides/slide\d+\.xml", n)) or None
        elif ext == ".xlsx":
            with zipfile.ZipFile(path) as z:
                out["sheets"] = sum(1 for n in z.namelist()
                                    if re.fullmatch(r"xl/worksheets/sheet\d+\.xml", n)) or None
    except Exception:
        swallowed("scanner._file_extent: measuring the file's extent failed")
    return {k: v for k, v in out.items() if v is not None}


# Reading order is a low-confidence heuristic the partner engine emits PER SLIDE
# ("Reading order may not match visual order"). Left as-is, N per-slide SERIOUS
# findings zero a deck's score on a suspicion. Collapse them into a single MODERATE
# advisory — one penalty, still visible and routed to human review to verify —
# rather than N confirmed serious failures. Remediation reorders every slide, so a
# remediated file still re-scans clean (zero reading-order findings).
_READING_ORDER_RULES = {"PPTX-ORDER-001"}


def _collapse_reading_order(issues: list[dict]) -> list[dict]:
    ro = [i for i in issues if i.get("ruleId") in _READING_ORDER_RULES]
    if len(ro) <= 1:
        for i in ro:
            i["severity"] = "MODERATE"
        return issues
    kept = [i for i in issues if i.get("ruleId") not in _READING_ORDER_RULES]
    adv = dict(ro[0])
    adv["severity"] = "MODERATE"
    adv["detail"] = (f"Reading order may not match visual order on {len(ro)} slides"
                     " — a heuristic flag; verify the reading order (routed to review).")
    return kept + [adv]


# The first-party 1.1.1 image detectors (formats/<fmt>/detectors/non_text_content.py). They
# exist so the IN-PROCESS re-scan can observe 1.1.1 — proposals.verify_residual_scs runs
# first-party checks only, and without them the write-back lane credited approved alt text on a
# criterion nothing could report. They are a floor, not a second opinion.
_FIRST_PARTY_ALT_RULES = frozenset({
    "DOCX_IMAGE_NO_ALT", "PPTX_IMAGE_NO_ALT", "XLSX_IMAGE_NO_ALT", "PDF_FIGURE_NO_ALT",
})
# The criterion, in its normalised form. Compared through store._extract_sc rather than by
# string prefix because the wcag field arrives in several spellings — bare, SC_-prefixed with
# underscores, or with the criterion name appended — and a prefix test would match only one.
# (Those spellings are named without quoting them: tests/test_rules_index.py scans this module's
# raw source for rule-id-shaped literals, and a quoted SC_ form reads as one even in a comment.)
_ALT_SC = "1.1.1"


def _collapse_duplicate_alt(issues: list[dict]) -> list[dict]:
    """Drop the first-party 1.1.1 findings when the partner engine already reported 1.1.1.

    Both detectors answer the same question about the same images, so when both run every
    undescribed image is counted TWICE — inflating the finding count on every scan and raising
    two review rows for one picture. The engine is the richer detector, so it wins where it ran;
    the first-party check is what keeps the criterion observable where it did not.

    Scoped to 1.1.1 and to these rule ids deliberately: this must never suppress a first-party
    finding the engine did not also make.
    """
    from store import _extract_sc          # the one SC normaliser: '1.1.1', 'SC_1_1_1', …
    engine_alt = any(_extract_sc(str(i.get("wcag", ""))) == _ALT_SC
                     and i.get("ruleId") not in _FIRST_PARTY_ALT_RULES
                     for i in issues)
    if not engine_alt:
        return issues
    return [i for i in issues if i.get("ruleId") not in _FIRST_PARTY_ALT_RULES]


def _scope_for_listing(user: str | None = None) -> dict | None:
    """The `scan_scope` map for gating what gets READ, for the signed-in `user`. None = no restriction.

    Deliberately the same resolution `_scoped_for_scoring` uses — the Store, not `in_scope`'s
    storeless fallback, which cannot see the `scan_scope` setting and answers "everything is in
    scope" to every question. That fallback is precisely how this feature came to gate scoring
    and nothing else.

    `user` threads the per-user scope override (ADR 0035 stage 2): the effective scope is the owner
    default WIDENED by this user's override when they have one, resolved once here and frozen into
    `scan_runs.scope` by the caller. `user=None` is exactly the owner default, so an unscoped or
    single-tenant deployment behaves as before.

    NOT wrapped in a bare try/except, for the reason spelled out in `_scoped_for_scoring`: a gate
    that fails open and says nothing is indistinguishable from a gate that was never wired up,
    and here failing open means reading a hospital's PDFs after the operator asked for Word
    documents only. That must surface as a defect, not as a quietly wider scan.
    """
    import core
    from store import active_scope
    return active_scope(core.store, user=user)


def _scoped_for_scoring(issues: list[dict], filename: str,
                        scope: dict | None = None) -> list[dict]:
    """The findings a SCORE may be computed from, for one file — the operator scope applied.

    Both `rb.assess` call sites go through here so the single-file path and the batch path cannot
    diverge on what a score counts (they already diverged once on which of them filtered disabled
    rules). Returns `issues` unchanged when no scope is set, so an unscoped deployment scores
    exactly as it did before this existed.

    PHASE 3a — the `scope` is now THREADED IN by the caller, not resolved live from the Store
    here. It is this scan's FROZEN scope (store.get_scan_scope on the fan-out path, or the
    in-memory `scan_scope` on the monolithic path), so the score reads the same recorded scope the
    Assess traces are gated by, and a later global scope change cannot re-score an old scan while
    its trace counts stay frozen. `scope=None` = NO RESTRICTION (unscoped, or a legacy scan with
    nothing recorded), which is also what a caller with no scan context passes — so this stays a
    no-op for the storeless benchmark/proposal callers exactly as before.
    """
    from assessment_selection import selection, selected_for_file, filter_findings
    with selection(selected_for_file(scope, filename)):
        return filter_findings(issues)


def rescore_reused(issues: list[dict], filename: str, status: str | None = None,
                   scope: dict | None = None) -> dict:
    """Score for a REUSED analysis, computed under THIS RUN's frozen scope (ADR 0011 + Phase 3a).

    Incremental reuse skips the download, the engine and the OCR — the expensive parts, and the
    whole point of ADR 0011. It must not also skip the SCORE, because a score is a function of
    the operator's scope and the scope can change between scans while the file's bytes do not.
    `find_prior_analysis` gates reuse on `rubric_hash` and nothing else, so before this the
    reused row carried whatever score was right the last time somebody scanned.

    PHASE 3a — DELIBERATE SEMANTIC CHANGE. ADR 0011 originally re-scored the reused file under the
    LIVE global scope ("the scope in force now"). Under 3a "the scope in force now" is reinterpreted
    as THIS run's FROZEN scope — the caller resolves store.get_scan_scope(scan_id) and threads it in
    as `scope`. This keeps the reuse deterministic against the scope the run was actually started
    under (the same scope its traces are gated by), instead of drifting with a global setting that
    may have moved since. A reuse is still a fresh score for its run — just its own run's scope, not
    whatever the global happens to be at the instant the job runs.

    Deliberately goes through `_scoped_for_scoring` and `Rubric.assess` — the same two calls the
    fresh path uses — rather than re-deriving a score here. A second expression of "what a score
    counts" is exactly how the single-file and batch paths diverged once already (see
    `_scoped_for_scoring`'s note), and this is a third caller. `scope=None` = no restriction.

    Returns scoped findings and their score together. Engine and source metadata remain
    untouched. Cache eligibility must establish that the prior scan covered this scope.
    """
    rb = Rubric.load_active(ACP / "config")
    from assessment_selection import selection, selected_for_file, filter_findings
    with selection(selected_for_file(scope, filename)):
        issues = filter_findings(issues)
    assessed = rb.assess(status != "error", issues, [])
    return {"issues": issues, **{k: assessed[k] for k in ("score", "compliant", "skipped_rules") if k in assessed}}


def _with_assessment_scope(fn):
    from functools import wraps
    @wraps(fn)
    def assess(tmp, name, *, scan_id=None, **kwargs):
        from assessment_selection import selection, selected_for_file
        scope = None
        if scan_id:
            import core
            scope = core.store.get_scan_scope(scan_id, refresh=True)
            scope = core.store.scope_for_file(scan_id, name, scope)
        codes = selected_for_file(scope, name) if scan_id else _selected_scs()
        with selection(codes):
            return fn(tmp, name, scan_id=scan_id, **kwargs)
    return assess


@_with_assessment_scope
def analyse_and_assess(tmp: Path, name: str, *, detect_pii: bool = False,
                       scan_id: str | None = None, doc_ref: str | None = None):
    """Analyse + rubric-assess ONE already-downloaded file (fan-out path, ADR 0007).
    `tmp` is a directory containing `name`. Returns (assessed_file_dict, pii_info),
    or (None, None) for an unsupported extension. Engines catch their own errors and
    return an error result rather than raising.

    `scan_id` is optional and only publishes the progress line; every existing caller works
    unchanged without it, and the benchmark harnesses deliberately pass nothing so they do not
    write progress for a scan that does not exist.
    """
    rb = Rubric.load_active(ACP / "config")
    ext = Path(name).suffix.lower()
    # Per-file progress logging (the fan-out path was silent — no way to tell a slow file from a
    # stuck one in the container logs). The heavy steps are already bounded: the .NET office CLI
    # has ACP_OFFICE_CLI_TIMEOUT (180s) and OCR caps at ACP_OCR_MAX_IMAGES (30) + downscales, so a
    # single image-heavy deck can't hang its worker indefinitely.
    import activity as _act
    import joblog as _jl
    # Prefer the Drive file id, which is ALREADY an opaque system identifier, over a digest of
    # the filename — an unkeyed hash of a low-entropy name is a pseudonym, not anonymity, and
    # joblog's header says so plainly. Stable across attempts, replicas and restarts either way,
    # which is what makes "this one was open on all three crashes" sayable; keyed on the document
    # alone, deliberately not on scan_id, so a retry's separate run still correlates.
    #
    doc = _jl.doc_id(name, opaque_ref=doc_ref)
    _t0 = time.monotonic()
    print(f"[scan] analysing {doc or 'unknown'} ({ext or '?'}) …", flush=True)
    _act.record_file(scan_id, name, phase="analysing",
                     action="running the accessibility engine", force=True)
    # Each engine below is a NATIVE entry point, and the enter line is flushed before the call:
    # pdf goes through pikepdf/qpdf, office shells out to the .NET CLI (already out-of-process,
    # so a crash there cannot take this worker down), html through the python parser stack.
    # Which of them — if any — corrupted the heap on 2026-08-30 is exactly what was not
    # recoverable from the logs, and is not assumed here.
    if ext == ".pdf":
        with _jl.stage("analyse.pdf", doc=doc, scan_id=scan_id, ext=ext):
            raw = {"engine": "python/pdf", **_analyse_pdf(tmp / name)}
    elif ext in OFFICE:
        with _jl.stage("analyse.office", doc=doc, scan_id=scan_id, ext=ext):
            office = _analyse_office(tmp)             # .NET CLI over the one-file dir
        raw = {"engine": ".net/office",
               **office.get(name, {"succeeded": False, "issues": [], "errors": ["no engine result"]})}
    elif ext in HTML_EXTS:
        with _jl.stage("analyse.html", doc=doc, scan_id=scan_id, ext=ext):
            raw = {"engine": "python/html", **_analyse_html(tmp / name)}
    else:
        _jl.emit("analyse.skip", doc=doc, scan_id=scan_id, ext=ext, reason="unsupported_ext")
        return None, None
    # 1.4.5 / 1.4.9 Images of Text — OCR embedded images; self-gates + never raises.
    if _sc_enabled("1.4.5"):
        _act.record_file(scan_id, name, sc="1.4.5", phase="analysing",
                         action="reading text baked into images")
    try:
        import ocr as _ocr_mod
        # Pillow decodes and tesseract runs in-process, so this block is native too — and it is
        # bracketed even though the enclosing `except Exception: pass` swallows Python-level
        # failures, because a segfault is not an exception and would leave an enter with no exit.
        with _jl.stage("analyse.ocr", doc=doc, scan_id=scan_id, ext=ext):
            raw["issues"] = (list(raw.get("issues", []))
                              + (_ocr_mod.images_of_text(tmp / name, ext) if _sc_enabled("1.4.5") else [])
                              + (_ocr_mod.images_of_text_no_exception(tmp / name, ext) if _sc_enabled("1.4.9") else []))
    except Exception:
        swallowed("scanner.analyse_and_assess: running the images-of-text (OCR) checks failed", scan_id)
    # 1.3.3 Sensory Characteristics + 3.1.2 Language of Parts — text-content checks.
    if _sc_enabled("1.3.3"):
        _act.record_file(scan_id, name, sc="1.3.3", phase="analysing",
                         action="checking wording and language changes")
    try:
        import pii as _pii_mod2
        import textchecks as _txt_mod
        import office_structure as _off_lang
        # extract_text reaches native extractors (pikepdf/qpdf for .pdf, lxml for OOXML parts).
        with _jl.stage("analyse.text", doc=doc, scan_id=scan_id, ext=ext):
            raw["issues"] = list(raw.get("issues", [])) + _txt_mod.content_findings(
                (_pii_mod2.extract_text(tmp / name) if any(_sc_enabled(c) for c in ("1.3.3", "3.1.2", "3.1.5")) else ""),
                (_off_lang.language_marked_spans(tmp / name, ext) if _sc_enabled("3.1.2") else {}))
    except Exception:
        swallowed("scanner.analyse_and_assess: running the text content checks failed", scan_id)
    # 2.4.6 / 2.4.9 / 1.4.3 / 1.4.6 — first-party OOXML/PDF structural checks
    # (docx/pptx headings + link-purpose, PDF contrast); partner engine doesn't
    # reach these for these formats. Self-contained; never raises.
    if _sc_enabled("1.4.3"):
        _act.record_file(scan_id, name, sc="1.4.3", phase="analysing",
                         action="checking headings, links and contrast")
    try:
        import office_structure as _off_mod
        # PDF contrast measurement here rasterises through the native pdf stack.
        with _jl.stage("analyse.structure", doc=doc, scan_id=scan_id, ext=ext):
            raw["issues"] = list(raw.get("issues", [])) + _off_mod.checks_for(tmp / name, ext)
    except Exception:
        swallowed("scanner.analyse_and_assess: running the Office structure checks failed", scan_id)
    raw["issues"] = _collapse_duplicate_alt(_collapse_reading_order(raw["issues"]))
    raw["issues"] = [i for i in raw["issues"] if i["ruleId"] not in rb.disabled]
    raw["errors"] = [e for e in raw["errors"]
                     if (e.get("rule") if isinstance(e, dict) else None) not in rb.disabled]
    # veraPDF Phase 0 corroboration (ADR 0028). Feature-flagged via ACP_VERAPDF_REST env var.
    # Only runs for PDF files; annotates existing 1.3.1/2.4.2/3.1.1 findings in-place with
    # per-content-item counts from veraPDF/ua1. Never raises; absent corroboration is not an error.
    if ext == ".pdf" and _selected_scs() is None:
        try:
            import verapdf_corroborate as _vcr
            _pdf_path = tmp / name
            if _pdf_path.exists():
                _vcr_result = _vcr.corroborate_pdf(_pdf_path.read_bytes())
                _vcr.annotate_issues(raw["issues"], _vcr_result)
        except Exception:
            swallowed("scanner.analyse_and_assess: veraPDF corroboration failed", scan_id)
    # ADR 0027 Tier A — scanned-PDF vision layout extraction. Feature-flagged via
    # ACP_SCANNED_PDF_TIER_A env var; a no-op when the flag is off or the file is not a
    # scanned/untagged PDF. Stores per-page layout descriptions; never raises.
    # ADR 0027 Tier B — REVIEW findings injected from the layouts (same guard, same block).
    if ext == ".pdf" and scan_id and any(_sc_enabled(c) for c in ("1.1.1", "1.3.1", "1.3.2", "2.4.6", "3.1.1")):
        try:
            import pdf_vision_assess as _pva
            if _pva.enabled():
                _pdf_path = tmp / name
                if _pdf_path.exists() and _pva.is_scanned_pdf(_pdf_path):
                    _pdf_bytes2 = _pdf_path.read_bytes()
                    _layouts = _pva.extract_layout(_pdf_bytes2, scan_id=scan_id, file=name)
                    if _layouts:
                        import core as _core
                        _core.store.save_scanned_pdf_layout(scan_id, name, _layouts)
                        print(
                            f"[scan] scanned-PDF Tier A: {doc or 'unknown'} → "
                            f"{len(_layouts)} page(s) assessed",
                            flush=True,
                        )
                        import pdf_vision_review as _pvr
                        _tier_b = _pvr.findings_from_layouts(_layouts, filename=name)
                        if _tier_b:
                            raw["issues"] = list(raw.get("issues", [])) + _tier_b
                            print(
                                f"[scan] scanned-PDF Tier B: {doc or 'unknown'} → "
                                f"{len(_tier_b)} REVIEW finding(s)",
                                flush=True,
                            )
        except Exception:
            swallowed("scanner.analyse_and_assess: scanned-PDF Tier A/B failed", scan_id)
    from assessment_selection import filter_findings, allowed_rules
    raw["issues"] = filter_findings(raw["issues"])
    allowed = allowed_rules()
    if allowed is not None:
        raw["errors"] = [e for e in raw["errors"] if not isinstance(e, dict)
                         or not e.get("rule") or e["rule"] in allowed]
    assessed = rb.assess(raw["succeeded"], raw["issues"], raw["errors"])
    # `errors` is carried alongside `**assessed` rather than being consumed by it. Rubric.assess
    # turns the engine's error list into `status` + `skipped_rules` — a COUNT — and drops the
    # list, so until now nothing downstream could say WHICH rules the engine failed on. The
    # per-rule manifest read `f["errors"]` and therefore found an empty list on every production
    # write, which made its ERROR status unreachable and `rules_errored_total` structurally zero.
    # Additive: every existing reader picks named keys, and the rubric's own fields are unchanged.
    fdict = {"file": name, "engine": raw["engine"], **assessed, "issues": raw["issues"],
             "errors": raw["errors"],
             "acp_stamped": detect_acp_stamp(tmp / name, ext),
             **_file_extent(tmp / name, ext)}
    # ADR 0020 stage 2 — Discover-side inventory classification (cheap container peek, no
    # rule engine). Carried on the file result so the document upsert can persist it;
    # additive, never affects findings, never raises.
    try:
        import classify as _cls
        fdict["classify"] = _cls.classify(tmp / name, ext)
    except Exception:
        swallowed("scanner.analyse_and_assess: classifying the document failed", scan_id)
    pinfo = None
    if detect_pii:
        import pii as _pii_mod
        pinfo = _pii_mod.detect_file(tmp / name)
    print(f"[scan] {doc or 'unknown'}: {len(raw.get('issues', []))} finding(s) "
          f"in {time.monotonic() - _t0:.1f}s", flush=True)
    _act.finish_file(scan_id, name)
    return fdict, pinfo


# Phase → what a person would say is happening. The phase names are internal state ("scoring"),
# and showing them raw asks the user to learn ACP's pipeline vocabulary to read a status bar.
_SCAN_ACTIONS = {
    "connecting": "connecting to the document source",
    "discovering": "finding documents in scope",
    "reading": "downloading",
    "analysing": "checking against the WCAG criteria",
    "scoring": "scoring findings",
}


def _gate_source_lifecycle(items: list[dict], source: str, user: str | None,
                           scope: dict) -> list[dict]:
    """Exclude durable terminal sources before any content read, preserving the estate."""
    provider = str(source or '').strip().lower()
    if provider not in {'drive', 'sharepoint', 'onedrive'}:
        scope['lifecycle_gate'] = {'listed': len(items), 'excluded': 0,
                                   'unknown': len(items), 'state': 'unsupported_provider'}
        return items
    import core
    from lifecycle_identity import source_identity, TERMINAL
    inventory = [dict(it, file=it['name'], drive_file_id=it.get('id'),
                      drive_id=it.get('driveId') or it.get('drive_id')) for it in items]
    lookup = getattr(core.store, 'get_source_lifecycle_states', None)
    if not callable(lookup):
        # This build requires the durable lookup seam. Never silently bypass a broken gate.
        raise RuntimeError('source lifecycle lookup unavailable before content access')
    result = lookup(user, provider, inventory)
    states = result['states']
    unavailable = set(result['unavailable'])
    kept, evidence = [], []
    unknown = 0
    for index, (item, row) in enumerate(zip(items, inventory)):
        identity = source_identity(user, provider, row)
        previous = states.get(identity) if identity is not None else None
        status = previous.get('lifecycle_status') if previous else None
        terminal = status in TERMINAL
        identity_state = ('missing_identity' if identity is None or index in unavailable else
                          'ledger_absent' if previous is None else 'matched')
        if identity_state != 'matched':
            unknown += 1
        reason = None
        if terminal:
            reason = f"excluded from Assess: durable source lifecycle status '{status}'"
            if previous.get('reason'):
                reason += ' — ' + str(previous['reason'])
        else:
            kept.append(item)
        evidence.append({'file': item['name'], 'identity_state': identity_state,
                         'lifecycle_status': status, 'exclusion_reason': reason})
    scope['lifecycle_gate'] = {'listed': len(items), 'excluded': len(items) - len(kept),
                               'unknown': unknown, 'state': 'checked', 'files': evidence}
    return kept


def run_scan(source: str = "local", progress=_noop, drive_token: str | None = None,
             folder: str | None = None, sp_token: str | None = None,
             ai_enabled: bool = True, scan_id: str | None = None,
             user: str | None = None, detect_pii: bool = False,
             exclude_remediated: bool = False, inventory_out: list | None = None,
             folders: list[str] | None = None,
             exclude_folders: list[str] | None = None,
             include_subfolders: bool = True,
             drive_delta: dict | None = None, sp_delta: dict | None = None) -> dict:
    # `drive_delta` — PRD Phase 3: {"prior_files", "changed", "removed_ids"}, produced by
    # core._drive_sync_plan for the scheduled sweep only. When given (whole-Drive scans only —
    # folder/folders narrows the scope in a way a delta reconstruction can't honor), `_list`
    # reconstructs the estate from the prior scan's inventory + this delta instead of walking
    # Drive. None (every other caller) is today's unchanged behavior. `sp_delta` is the same
    # seam for SharePoint (core._sp_sync_plan, the scheduled sweep's whole-configured-library
    # request only).
    from store import RULE_CATALOG, _extract_sc  # import here to avoid circular at module load
    rb = Rubric.load_active(ACP / "config")
    started = datetime.now(timezone.utc).isoformat()
    scan_id = scan_id or uuid.uuid4().hex[:12]

    # Every progress payload gains a readable `activity` line, once, here — rather than at the
    # seven call sites below. The counters answer "how far along", and a user watching a 25-page
    # document sit at "analysing" for forty seconds is asking a different question: what is it
    # DOING? Wrapping means a new phase cannot ship with counters that update and a line that
    # does not, which is how the two drift apart.
    _inner_progress = progress
    import activity as _act          # also used by _analyse_one, defined further down

    _seen_phase: list[str | None] = [None]

    def progress(d: dict) -> None:                            # noqa: F811 — deliberate shadow
        try:
            phase = d.get("phase")
            d = dict(d, activity=_act.line(file=d.get("current"),
                                           action=_SCAN_ACTIONS.get(phase, phase or "")))
            # TWO WRITERS, ONE KEY — so they must not fight over it. During `analysing` the
            # per-file channel (record_file, from _analyse_one) is active and strictly better:
            # it names the criterion and how many documents are in flight. This coarse line fires
            # once per completed file in the same phase, and left unguarded it overwrote the
            # richer headline a fraction of a second later, so the line flickered between
            # "Onboarding.html · 1.3.3 Sensory Characteristics · …" and a bare
            # "checking against the WCAG criteria". Measured, on a 13-document scan.
            if phase != "analysing":
                # Forced on a phase CHANGE. The rate limit is right for repeats within a phase
                # and wrong for transitions: the last thing published before `scoring` began was
                # a finish_file with an empty headline, so the bar went blank for the whole
                # scoring pass rather than saying "scoring findings".
                changed = phase != _seen_phase[0]
                _seen_phase[0] = phase
                _act.record(scan_id, file=d.get("current"),
                            action=_SCAN_ACTIONS.get(phase, phase), phase=phase, force=changed)
        except Exception:
            # a progress line must never be able to fail the scan it describes
            swallowed("scanner.progress: rendering the progress activity line failed")
        _inner_progress(d)

    tmp = Path(tempfile.mkdtemp(prefix="acp-api-scan-"))
    # Per-user token: default to whole-Drive search. ADC/demo: pinned demo folder.
    # `folders` (multi-root) wins when given; `folder` stays the single-root form. The "root"
    # fallback means "whole Drive" and must not be applied when explicit folders were chosen —
    # doing so would widen a deliberately narrowed scan back to the entire estate.
    effective_folder = folder if folder else (None if folders else ("root" if drive_token else None))
    try:
        progress({"phase": "connecting", "files_found": 0, "files_done": 0, "current": None})
        svc = None if source in ("local", "sharepoint") else _drive_service(drive_token)
        scope: dict = {}
        # Honour the configured estate ceiling here too, matching the production fan-out path
        # (handlers._scan_discover). Without this run_scan fell back to _search_drive's 500-file
        # default, so a whole-Drive scan on this path silently covered ~500 of a large estate while
        # the "raise ACP_FANOUT_MAX_FILES" hint pointed at a knob that never reached it.
        items = _list(source, svc, folder=effective_folder, sp_token=sp_token,
                     max_files=FANOUT_MAX_FILES,
                     **({"folders": folders} if folders else {}),
                     **({"exclude_folders": exclude_folders} if exclude_folders else {}),
                     include_subfolders=include_subfolders,
                     exclude_remediated=exclude_remediated, scope_out=scope,
                     scope_files=_scope_for_listing(user), inventory_out=inventory_out,
                     drive_delta=drive_delta, sp_delta=sp_delta)
        # Preserve the full listing even when terminal state excludes content access.
        # save_scan persists it as inventory; only the assessment population is reduced.
        inventory_items = list(items)
        items = _gate_source_lifecycle(items, source, user, scope)
        # Freeze per-file rules with the same listing, before any assessment starts.
        import core as _scope_core
        scope["scope_rules"] = [
            {k: r.get(k) for k in ("rule_id", "selector", "value", "codes",
                                   "priority", "is_override", "enabled")}
            for r in _scope_core.store.list_scope_rules(enabled_only=True)
        ]
        n = len(items)
        # Metadata completeness: derivable from the listing itself before any download.
        exc_missing_optional = sum(
            1 for it in items if it.get("owner") is None or it.get("source_modified") is None)
        exc_missing_required = sum(
            1 for it in items if not it.get("name") or it.get("mime") is None and "path" not in it)
        progress({"schema_version": 2,
                  "phase": "discovering", "files_found": n, "files_done": 0, "current": None,
                  "folders_found": scope.get("folders_walked"),
                  "folders_visited": scope.get("folders_walked"),
                  "folder_workers_configured": _DISCOVERY_WORKERS,
                  "exc_missing_optional": exc_missing_optional,
                  "exc_missing_required": exc_missing_required,
                  "metadata_complete": n - exc_missing_optional,
                  "metadata_incomplete": exc_missing_optional})

        exc_inaccessible_file = 0
        exc_metadata_failure = 0
        exc_deleted_during_scan = 0
        skipped: set[str] = set()

        for i, it in enumerate(items):
            progress({"schema_version": 2,
                      "phase": "reading", "files_found": n, "files_done": i, "current": it["name"],
                      "exc_inaccessible_file": exc_inaccessible_file,
                      "exc_metadata_failure": exc_metadata_failure,
                      "exc_deleted_during_scan": exc_deleted_during_scan,
                      "exc_missing_optional": exc_missing_optional,
                      "exc_missing_required": exc_missing_required,
                      "metadata_complete": n - exc_missing_optional,
                      "metadata_incomplete": exc_missing_optional})
            # ADR 0020 read side: a pre-download checksum (Drive md5, when present) lets this
            # hit a DIFFERENT scan's earlier download of the same content, not just a retry of
            # this one — skip a second Drive/SharePoint download when so. Cache miss (or no
            # blob configured) falls through to the normal download path, unchanged.
            cached = read_cached_source(scan_id, it["name"], user, checksum=it.get("checksum"))
            if cached is not None:
                (tmp / it["name"]).write_bytes(cached)
            else:
                try:
                    _download(it, tmp, svc, sp_token=sp_token)
                except PermissionError:
                    exc_inaccessible_file += 1
                    skipped.add(it["name"])
                    continue
                except Exception as _dl_exc:
                    # Detect 404 (item deleted between listing and download) vs generic failures.
                    _status = None
                    try:
                        import httpx as _httpx
                        if isinstance(_dl_exc, _httpx.HTTPStatusError):
                            _status = _dl_exc.response.status_code
                    except Exception:
                        swallowed("scanner.run_scan: probing whether the download error was an httpx status "
                                  "error failed", scan_id)
                    if _status is None:
                        try:
                            from googleapiclient.errors import HttpError as _HttpError
                            if isinstance(_dl_exc, _HttpError) and getattr(_dl_exc, "resp", None):
                                _status = int(_dl_exc.resp.status)
                        except Exception:
                            swallowed("scanner.run_scan: probing whether the download error was a Google API "
                                      "HttpError failed", scan_id)
                    if _status == 404:
                        exc_deleted_during_scan += 1
                    elif _status in (401, 403):
                        exc_inaccessible_file += 1
                    else:
                        exc_metadata_failure += 1
                    skipped.add(it["name"])
                    continue
                cache_source_bytes(tmp, it["name"], scan_id, user,  # ADR 0020 — best-effort
                                  checksum=it.get("checksum"))

        # Remove skipped files from the items list so downstream phases don't try to analyse them.
        if skipped:
            items = [it for it in items if it["name"] not in skipped]
            n = len(items)

        from assessment_policy import scope_from_json
        from assessment_selection import selection, selected_for_file, filter_findings
        _execution_scope = scope_from_json((scope or {}).get("scan_scope"))
        from assessment_policy import resolve_file_scope
        scopes_by_file = {it["name"]: resolve_file_scope(it, _execution_scope, scope.get("scope_rules"))
                          for it in items}
        rule_allowlists = None
        if _execution_scope or scope.get("scope_rules"):
            from assessment_selection import allowed_rules
            rule_allowlists = {}
            for item in items:
                with selection(selected_for_file(scopes_by_file[item["name"]], item["name"])):
                    rule_allowlists[item["name"]] = allowed_rules()
        if not items:
            office = {}
        elif rule_allowlists is not None:
            office = _analyse_office(tmp, rule_allowlists=rule_allowlists)
        else:
            office = _analyse_office(tmp)

        import pii as _pii_mod  # sensitive-data detection dimension (ADR 0006)
        import ocr as _ocr_mod  # 1.4.5 images-of-text OCR detection
        import textchecks as _txt_mod  # 1.3.3 sensory + 3.1.2 language-of-parts
        import office_structure as _off_mod  # docx/pptx headings+links, PDF contrast
        pii_by_file: dict[str, dict] = {}
        raw: dict[str, dict] = {}

        # Analyse each file + detect PII concurrently. Each call builds a fresh
        # analyser (no shared state) and reads only its own temp file, so the work
        # parallelises safely; pikepdf/lxml release the GIL. Langfuse spans are then
        # emitted sequentially from the main thread (one trace, no concurrent writes).
        # Opt-out (detect_pii=False) skips PII text extraction — faster on PDF estates.
        def _analyse_one(it):
            with selection(selected_for_file(scopes_by_file[it["name"]], it["name"])):
                name, ext = it["name"], Path(it["name"]).suffix.lower()
                _act.record_file(scan_id, name, phase="analysing",
                                 action="running the accessibility engine", force=True)
                if ext == ".pdf":
                    r = {"engine": "python/pdf", **_analyse_pdf(tmp / name)}
                elif ext in OFFICE:
                    r = {"engine": ".net/office",
                         **office.get(name, {"succeeded": False, "issues": [], "errors": ["no engine result"]})}
                elif ext in HTML_EXTS:
                    r = {"engine": "python/html", **_analyse_html(tmp / name)}
                else:
                    return None
                # Per-RULE-GROUP progress, in the order the groups actually run. These are the
                # finest boundaries that exist: inside the .NET engine call there is one process
                # invocation and no callback, so a line claiming to be on a specific rule within it
                # would be invented. Each group below names the criteria it really evaluates.
                #
                # record_file, not record: up to _SCAN_WORKERS documents are in flight here, and a
                # single last-writer-wins line under eight threads flips several times a second and
                # reads as thrashing. See activity.record_file.
                if _sc_enabled("1.4.5"):
                    _act.record_file(scan_id, name, sc="1.4.5", phase="analysing",
                                     action="reading text baked into images")
                # 1.4.5 / 1.4.9 Images of Text — OCR embedded images; self-gates + never raises.
                try:
                    r["issues"] = (list(r.get("issues", []))
                                    + (_ocr_mod.images_of_text(tmp / name, ext) if _sc_enabled("1.4.5") else [])
                                    + (_ocr_mod.images_of_text_no_exception(tmp / name, ext) if _sc_enabled("1.4.9") else []))
                except Exception:
                    swallowed("scanner._analyse_one: running the images-of-text (OCR) checks failed")
                # 1.3.3 Sensory Characteristics + 3.1.2 Language of Parts — text-content checks.
                if _sc_enabled("1.3.3"):
                    _act.record_file(scan_id, name, sc="1.3.3", phase="analysing",
                                     action="checking wording and language changes")
                try:
                    r["issues"] = list(r.get("issues", [])) + _txt_mod.content_findings(
                        (_pii_mod.extract_text(tmp / name) if any(_sc_enabled(c) for c in ("1.3.3", "3.1.2", "3.1.5")) else ""),
                        (_off_mod.language_marked_spans(tmp / name, ext) if _sc_enabled("3.1.2") else {}))
                except Exception:
                    swallowed("scanner._analyse_one: running the text content checks failed")
                # 2.4.6 / 2.4.9 / 1.4.3 / 1.4.6 — first-party OOXML/PDF structural checks.
                if _sc_enabled("1.4.3"):
                    _act.record_file(scan_id, name, sc="1.4.3", phase="analysing",
                                     action="checking headings, links and contrast")
                try:
                    r["issues"] = list(r.get("issues", [])) + _off_mod.checks_for(tmp / name, ext)
                except Exception:
                    swallowed("scanner._analyse_one: running the Office structure checks failed")
                r["issues"] = _collapse_duplicate_alt(_collapse_reading_order(r["issues"]))
                try:                                          # ADR 0020 stage 2 — inventory peek
                    import classify as _cls
                    r["classify"] = _cls.classify(tmp / name, ext)
                except Exception:
                    swallowed("scanner._analyse_one: classifying the document failed")
                pinfo = _pii_mod.detect_file(tmp / name) if detect_pii else None
                # Drop it from the headline. Forced inside finish_file, because a stale entry here
                # names a document that has FINISHED while others are still running — the one way
                # this line can say something false rather than merely lag.
                _act.finish_file(scan_id, name)
                r["issues"] = filter_findings(r["issues"])
                return (name, r, pinfo)

        progress({"phase": "analysing", "files_found": n, "files_done": 0, "current": None})
        # as_completed, not map: `map` yields nothing until the whole batch finishes, so the
        # analysing phase — the long one — reported "0 documents" for its entire duration and
        # the UI looked stalled. Report each document as it lands. Order is not load-bearing:
        # `raw`/`assessed` are keyed by filename and `aggregate` sums over values.
        analysed = []
        with _cf.ThreadPoolExecutor(max_workers=_SCAN_WORKERS) as _ex:
            # Bound so the stage records _analyse_one emits keep the claiming job's identity;
            # submit() starts each pool thread from an empty context. See joblog's header.
            import joblog as _jl_bind
            _futs = [_ex.submit(_jl_bind.bind(_analyse_one), it) for it in items]
            for _done_n, _fut in enumerate(_cf.as_completed(_futs), start=1):
                _res = _fut.result()
                if _res is not None:
                    analysed.append(_res)
                progress({"phase": "analysing", "files_found": n, "files_done": _done_n,
                          "current": _res[0] if _res else None})

        for done_i, (name, r, pinfo) in enumerate(analysed):
            # files_done stays at n through scoring: every document has been analysed by now,
            # and scoring is a fast post-pass over results already in memory. Reporting the
            # scoring index here would make the counter fall from n back to 0 — the UI read as
            # "0 documents" while the Score stage was lit, which is what it did before.
            progress({"phase": "scoring", "files_found": n, "files_done": n, "current": name})
            raw[name] = r
            engine = r["engine"]
            pii_total = 0
            if detect_pii and pinfo is not None:
                pii_by_file[name] = pinfo
                pii_total = pinfo.get("total", 0)
            # File-centric tracing: each file gets its OWN Langfuse trace (Discover here;
            # Assess/Remediate spans land on this same trace later, when those phases run),
            # grouped into a session keyed by scan_id. Always emitted — unlike the old
            # deep-scan-only gate — so every file has a trace to open from FileDrawer
            # regardless of whether the deep scan ran; the PII sub-span stays conditional.
            ftrace = _lf_mod.file_trace(scan_id, name, user=user)
            dspan = _lf_mod.discover_span(ftrace, engine)
            if detect_pii and pii_total:
                _lf_mod.pii_span(dspan, pinfo, filename=name)
            dspan.end(output={"engine": engine, "sensitive_data": pii_total})

        progress({"phase": "scoring", "files_found": n, "files_done": n, "current": None})
        for r in raw.values():
            r["issues"] = [i for i in r["issues"] if i["ruleId"] not in rb.disabled]
            r["errors"] = [e for e in r["errors"] if (e.get("rule") if isinstance(e, dict) else None) not in rb.disabled]
        # Scored over the in-scope findings; `r["issues"]` stays whole on the record. Same helper
        # as the single-file path above, so the two cannot disagree about what a score counts.
        # PHASE 3a — score over THIS scan's FROZEN scope. The MONOLITHIC path's scan_runs row does
        # not exist yet (save_scan writes it next), so the frozen scope is read from the in-memory
        # `scope["scan_scope"]` that _list just recorded — the exact same value save_scan will
        # persist and re-read via scope_from_json to gate the traces, so score and traces read one
        # frozen scope. Resolved ONCE here rather than per file so every file scores under the same
        # value. None (unscoped / no restriction) is a no-op.
        from store import scope_from_json
        _frozen_scope = scope_from_json((scope or {}).get("scan_scope"))
        assessed = {k: rb.assess(r["succeeded"], _scoped_for_scoring(r["issues"], k, scopes_by_file[k]),
                                 r["errors"])
                    for k, r in raw.items()}
        summary = rb.aggregate(assessed)
        _lf_mod.flush()

        # Build name → Drive file id map so write-back can reference the original.
        drive_id_map = {it["name"]: it.get("id") for it in items}
        source_modified_map = {it["name"]: it.get("source_modified") for it in items}

        return {
            "_scan_id": scan_id,   # hint to save_scan so it reuses the same ID → trace joins
            # Hint to save_scan so the MONOLITHIC path also populates scan_inventory (see there
            # for why this matters) — the same raw `_list()` items ADR 0020's deferred discovery
            # path already turns into scan_inventory rows in handlers._scan_discover's `norm`.
            # Not part of the report's public shape: popped before anything else reads `report`.
            "_inventory_items": inventory_items,
            "rubric": {"name": rb.name, "version": rb.version, "hash": rb.hash},
            "summary": summary,
            "started_at": started,
            "completed_at": datetime.now(timezone.utc).isoformat(),
            "source": source,
            "scope": scope,           # WHAT this scan covered — never just the count (see _list)
            "owner": user,            # per-user isolation: who ran this scan

            "files": [{"file": k, "engine": raw[k]["engine"], **assessed[k], "issues": raw[k]["issues"],
                       "drive_file_id": drive_id_map.get(k), "source_modified": source_modified_map.get(k),
                       "pii": pii_by_file.get(k),
                       "acp_stamped": detect_acp_stamp(tmp / k, Path(k).suffix.lower()),
                       "classify": raw[k].get("classify"),   # ADR 0020 stage 2 — inventory peek
                       **_file_extent(tmp / k, Path(k).suffix.lower())}
                      for k in sorted(raw)],
        }
    finally:
        shutil.rmtree(tmp, ignore_errors=True)  # ephemeral: documents never retained

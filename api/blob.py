"""Azure Blob remediated-output store (ADR 0010).

The PRIMARY store for a remediated file's fixed copy — Drive write-back
(api/handlers.py's _remediate_file) is now a best-effort mirror, not the
source of truth. Managed-identity auth via DefaultAzureCredential: no storage
key to provision, rotate, or leak. A no-op (returns None everywhere) when
ACP_BLOB_ACCOUNT isn't set — e.g. local dev without the Azure infra — so
importing this module is always safe.

Blob path: {owner_email}/{scan_id}/{filename} (ACP_BLOB_CONTAINER, default
'remediated'). Scoped to remediated-output artifacts only — not a general
file-storage abstraction (ADR 0010's own non-goals).
"""
from __future__ import annotations
import logging
import os
from swallowed import swallowed

_ACCOUNT = os.environ.get("ACP_BLOB_ACCOUNT", "")
_CONTAINER = os.environ.get("ACP_BLOB_CONTAINER", "remediated")
# Rendered page-1 thumbnails (ADR 0015) live in their own container, keyed the same
# {owner}/{scan_id}/{filename} way — kept out of 'remediated' so a preview is never
# mistaken for a remediated artifact.
_RENDER_CONTAINER = os.environ.get("ACP_BLOB_RENDER_CONTAINER", "thumbnails")
_RELEASE_PACKAGE_CONTAINER = os.environ.get("ACP_BLOB_RELEASE_PACKAGE_CONTAINER", "release-packages")
_ENABLED = bool(_ACCOUNT)
_LOG = logging.getLogger(__name__)

# TRANSPORT BUDGET, STATED RATHER THAN INHERITED. A blob read sits on the remediation worker's
# hot path — `handlers._remediation_source_bytes` reads the cached original for every local and
# SharePoint document — and the remediate tier runs two slots, so a request that sits there
# occupies half the tier while it waits.
#
# The SDK is not timeout-free: measured on azure-storage-blob 12.24.0, its base client applies
# CONNECTION_TIMEOUT=20 / READ_TIMEOUT=60 and an ExponentialRetry with total_retries=3,
# initial_backoff=15, increment_base=3. So one stalled read is bounded at roughly four 60s
# attempts plus ~84s of backoff — about five to six minutes — PER chunk request, since readall()
# streams. Bounded, but far longer than anything on this path should take, and invisible: the
# caller's try/except sees a slow success, not a problem.
#
# These make the budget explicit and tighter, and env-tunable so an operator can widen it for a
# slow link without a deploy. Deliberately NOT lowering retry_total from the SDK default: the
# remediated-copy upload is the must-succeed write of ADR 0010 and goes through the same client.
_CONNECT_TIMEOUT_S = int(os.environ.get("ACP_BLOB_CONNECT_TIMEOUT_S", "10") or 10)
_READ_TIMEOUT_S = int(os.environ.get("ACP_BLOB_READ_TIMEOUT_S", "30") or 30)
_RETRY_TOTAL = int(os.environ.get("ACP_BLOB_RETRY_TOTAL", "3") or 3)

_client = None


def _timeouts() -> dict:
    """Per-operation transport timeouts.

    Passed at the CALL SITE as well as on the client because the client is a process-global
    cached on first use: whichever code path built it decided the budget for every later caller.
    azure-core's transport pops `connection_timeout`/`read_timeout` from per-request kwargs
    (RequestsTransport.send) ahead of the client's connection_config, and the storage
    StorageStreamDownloader stores the kwargs it was given and replays them on every chunk
    request — so passing them to download_blob() covers the whole streamed read, not just the
    first call. Both verified against the installed 12.24.0, not assumed.
    """
    return {"connection_timeout": _CONNECT_TIMEOUT_S, "read_timeout": _READ_TIMEOUT_S}


def enabled() -> bool:
    return _ENABLED


def _service_client():
    global _client
    if not _ENABLED:
        return None
    if _client is None:
        from azure.identity import DefaultAzureCredential
        from azure.storage.blob import BlobServiceClient
        _client = BlobServiceClient(
            account_url=f"https://{_ACCOUNT}.blob.core.windows.net",
            credential=DefaultAzureCredential(),
            retry_total=_RETRY_TOTAL, **_timeouts())
    return _client


def _blob_path(owner: str | None, scan_id: str, filename: str) -> str:
    return f"{owner or 'demo'}/{scan_id}/{filename}"


def upload_remediated(owner: str | None, scan_id: str, filename: str, data: bytes,
                      content_type: str) -> str | None:
    """Upload a remediated file's fixed bytes. Returns the blob's URL, or None when
    blob storage isn't configured (caller falls back to Drive-only, the pre-ADR-0010
    behavior) — never raises for that case, since local/dev environments legitimately
    don't have this infra."""
    svc = _service_client()
    if svc is None:
        return None
    from azure.storage.blob import ContentSettings
    blob = svc.get_blob_client(container=_CONTAINER, blob=_blob_path(owner, scan_id, filename))
    settings = ContentSettings(content_type=content_type)
    try:
        blob.upload_blob(data, overwrite=False, content_settings=settings)
    except Exception as exc:
        if getattr(exc, "status_code", None) == 409 or getattr(exc, "error_code", "") == "BlobAlreadyExists":
            _LOG.warning("upload_remediated: overwriting existing blob scan=%s file=%s", scan_id, filename)
            blob.upload_blob(data, overwrite=True, content_settings=settings)
        else:
            raise
    return blob.url


def upload_immutable_retry(owner, scan_id, filename, data, content_type):
    """Digest-scoped retry evidence; an existing object is never overwritten."""
    import hashlib
    digest = hashlib.sha256(data).hexdigest()
    svc = _service_client()
    if svc is None:
        return None
    from azure.storage.blob import ContentSettings
    client = svc.get_blob_client(container=_CONTAINER,
                                 blob=_blob_path(owner, scan_id, filename) + '.retry/' + digest)
    try:
        client.upload_blob(data, overwrite=False, content_settings=ContentSettings(content_type=content_type))
    except Exception as exc:
        if getattr(exc, 'status_code', None) != 409 and getattr(exc, 'error_code', '') != 'BlobAlreadyExists':
            raise
    # An SDK acknowledgement is not proof of the persisted candidate. Verify the
    # exact configured object on new uploads and immutable AlreadyExists replays.
    persisted = client.download_blob(offset=0, length=len(data) + 1, **_timeouts()).readall()
    if len(persisted) != len(data) or hashlib.sha256(persisted).hexdigest() != digest:
        raise ValueError('retry_artifact_readback_mismatch') from None
    return client.url


def _remediated_client(svc, owner, scan_id, filename):
    """Follow only a digest-scoped pointer within the configured account/container."""
    canonical = svc.get_blob_client(container=_CONTAINER, blob=_blob_path(owner, scan_id, filename))
    import core
    lookup = getattr(core.store, 'get_file_record', None)
    record = lookup(scan_id, filename) if callable(lookup) else None
    record = record or {}
    if record.get('blob_url') == canonical.url or '.retry/' not in str(record.get('blob_url') or ''):
        return canonical, None
    if (any(part in {'.', '..'} for part in filename.split('/'))
            or '%' in filename or '\\' in filename):
        raise ValueError('retry_artifact_identity_invalid')
    # Owner-scoped: the caller's owner must be the scan's recorded owner. An owner-less scan
    # (SSO-less/demo) matches only an owner-less caller, and its objects live under the same
    # 'demo/' prefix _blob_path gives its writer; an owned scan still refuses a caller with none.
    scan = core.store.get_scan(scan_id) or {}
    if (scan.get('run', {}).get('owner_email') or None) != (owner or None):
        raise ValueError('retry_artifact_owner_mismatch')
    digest = record.get('corrected_sha256') or ''
    if len(digest) != 64 or any(c not in '0123456789abcdef' for c in digest):
        raise ValueError('retry_artifact_digest_invalid')
    client = svc.get_blob_client(container=_CONTAINER,
                                blob=_blob_path(owner, scan_id, filename) + '.retry/' + digest)
    if client.url != record['blob_url']:
        raise ValueError('retry_artifact_pointer_invalid')
    return client, digest


def download_remediated(owner: str | None, scan_id: str, filename: str) -> bytes | None:
    """Stream a remediated file's bytes back out. None if not configured, or not found
    (e.g. a pre-ADR-0010 remediation that only ever wrote to Drive)."""
    svc = _service_client()
    if svc is None:
        return None
    try:
        blob, digest = _remediated_client(svc, owner, scan_id, filename)
        data = blob.download_blob(**_timeouts()).readall()
        if digest:
            import hashlib
            if hashlib.sha256(data).hexdigest() != digest:
                return None
        return data
    except Exception:
        return None


def download_report_evidence(owner, scan_id, filename, *, original=False, checksum=None, max_bytes):
    """Bounded cache-only read for optional report visuals; existing readers unchanged.

    Return at most limit+1 bytes so callers distinguish oversized artifacts from a
    cache miss without allocating/downloading the complete document.
    """
    if type(max_bytes) is not int or max_bytes < 1:
        raise ValueError('A positive evidence byte limit is required')
    svc = _service_client()
    if svc is None:
        return None
    container = _SOURCES_CONTAINER if original else _CONTAINER
    key = _source_key(owner, scan_id, filename, checksum) if original else _blob_path(owner, scan_id, filename)
    try:
        client, digest = ((svc.get_blob_client(container=container, blob=key), None) if original
                          else _remediated_client(svc, owner, scan_id, filename))
        data = client.download_blob(offset=0, length=max_bytes + 1, **_timeouts()).readall()
        if digest and len(data) <= max_bytes:
            import hashlib
            if hashlib.sha256(data).hexdigest() != digest:
                return None
        return data
    except Exception:
        return None


def upload_release_package(owner: str, scan_id: str, job_id: str, stream) -> str | None:
    """Persist a prepared ZIP so request navigation cannot discard expensive packaging work."""
    svc = _service_client()
    if svc is None:
        return None
    blob = svc.get_blob_client(
        container=_RELEASE_PACKAGE_CONTAINER,
        blob=_blob_path(owner, scan_id, f"{job_id}.zip"))
    try:
        stream.seek(0)
        blob.upload_blob(stream, overwrite=True)
    except Exception as exc:
        if getattr(exc, "status_code", None) == 404 or getattr(exc, "error_code", "") == "ContainerNotFound":
            try:
                svc.create_container(_RELEASE_PACKAGE_CONTAINER)
            except Exception:
                swallowed("blob.upload_release_package: creating the package container failed", scan_id)
            stream.seek(0)
            blob.upload_blob(stream, overwrite=True)
        else:
            raise
    return blob.url


def open_release_package(owner: str, scan_id: str, job_id: str):
    """Return Azure's chunked downloader for a prepared package, or None when unavailable."""
    svc = _service_client()
    if svc is None:
        return None
    blob = svc.get_blob_client(
        container=_RELEASE_PACKAGE_CONTAINER,
        blob=_blob_path(owner, scan_id, f"{job_id}.zip"))
    try:
        return blob.download_blob(**_timeouts())
    except Exception:
        return None


# --- ADR 0015 rendered-thumbnail cache ---------------------------------------
# Same seam as the remediated store (ADR 0010 non-goals bless the reuse), a distinct
# container. Blob key: {owner}/{scan_id}/{filename}.png. Like the rest of this module,
# a no-op returning None when blob storage isn't configured — the endpoint then just
# renders on demand every time instead of caching, which is correct for local dev.

def upload_render(owner: str | None, scan_id: str, filename: str, data: bytes) -> str | None:
    """Cache a rendered page-1 PNG. Returns the blob URL, or None when blob storage isn't
    configured or the cache write fails — render caching is best-effort and must NEVER raise
    into the request path. Self-heals a missing container on first use (unlike 'remediated',
    the thumbnails container is provisioned lazily here, not by one-time deploy infra)."""
    svc = _service_client()
    if svc is None:
        return None
    from azure.storage.blob import ContentSettings
    blob = svc.get_blob_client(container=_RENDER_CONTAINER, blob=_blob_path(owner, scan_id, filename) + ".png")
    settings = ContentSettings(content_type="image/png")
    try:
        blob.upload_blob(data, overwrite=True, content_settings=settings)
        return blob.url
    except Exception:
        # Most likely the container doesn't exist yet — create it and retry once. The app's
        # managed identity (Storage Blob Data Contributor) can create containers. Any other
        # failure (transient, perms) collapses to "not cached"; the endpoint still serves the
        # freshly rendered bytes, it just re-renders next time.
        try:
            svc.create_container(_RENDER_CONTAINER)
        except Exception:
            # already exists (race) or can't create — fall through to the retry
            swallowed("blob.upload_render: creating the render container failed", scan_id)
        try:
            blob.upload_blob(data, overwrite=True, content_settings=settings)
            return blob.url
        except Exception:
            return None


def download_render(owner: str | None, scan_id: str, filename: str) -> bytes | None:
    """Read a cached page-1 PNG back out. None if not configured, or not yet rendered."""
    svc = _service_client()
    if svc is None:
        return None
    blob = svc.get_blob_client(container=_RENDER_CONTAINER, blob=_blob_path(owner, scan_id, filename) + ".png")
    try:
        return blob.download_blob().readall()
    except Exception:
        return None


# --- ADR 0020 source-bytes cache -----------------------------------------------
# Discover downloads each file once; a later Assess phase (or a retried/resumed job) reads
# the SAME bytes back from this cache instead of paying a second Drive/SharePoint download.
# A distinct container so a source original can never be mistaken for a remediated artifact
# or a thumbnail. Same discipline as the render cache: lazy container creation, best-effort,
# a no-op returning None when blob storage isn't configured (local dev re-reads the source
# instead — the ADR's stated degradation, not an error).
#
# Keyed by content hash when one is known BEFORE download (Drive's md5Checksum, read from
# listing metadata) — {owner}/{checksum} — so an unchanged file is a cache hit across scans,
# not just within one (ADR 0020 §2). SharePoint and local sources carry no pre-download
# checksum today, so those fall back to the original {owner}/{scan_id}/{filename} key: a
# same-scan-retry benefit only, exactly ADR 0020 §1's behavior — no regression, no
# collision between the two key shapes (a checksum is never a valid scan_id).
_SOURCES_CONTAINER = os.environ.get("ACP_BLOB_SOURCES_CONTAINER", "sources")


def _checksum_key(owner: str | None, checksum: str) -> str:
    return f"{owner or 'demo'}/{checksum}"


def _source_key(owner: str | None, scan_id: str, filename: str, checksum: str | None = None) -> str:
    return _checksum_key(owner, checksum) if checksum else _blob_path(owner, scan_id, filename)


def upload_source(owner: str | None, scan_id: str, filename: str, data: bytes,
                  checksum: str | None = None) -> str | None:
    """Cache a discovered file's original bytes, keyed by `checksum` when the caller has one
    (a pre-download source checksum, e.g. Drive's md5Checksum), else by scan_id/filename.
    Returns the blob URL, or None when blob storage isn't configured or the write fails —
    caching is best-effort and must NEVER raise into the scan path. Self-heals a missing
    container on first use."""
    svc = _service_client()
    if svc is None:
        return None
    from azure.storage.blob import ContentSettings
    key = _source_key(owner, scan_id, filename, checksum)
    blob = svc.get_blob_client(container=_SOURCES_CONTAINER, blob=key)
    settings = ContentSettings(content_type="application/octet-stream")
    try:
        blob.upload_blob(data, overwrite=True, content_settings=settings)
        return blob.url
    except Exception:
        try:
            svc.create_container(_SOURCES_CONTAINER)
        except Exception:
            # already exists (race) or can't create — fall through to the retry
            swallowed("blob.upload_source: creating the source container failed", scan_id)
        try:
            blob.upload_blob(data, overwrite=True, content_settings=settings)
            return blob.url
        except Exception:
            return None


def download_source(owner: str | None, scan_id: str, filename: str,
                    checksum: str | None = None) -> bytes | None:
    """Read a discovered file's cached original bytes back out, checking the same key
    upload_source would have written (checksum when given, else scan_id/filename). None if
    not configured or not cached (the caller falls back to a fresh source download — today's
    behavior)."""
    svc = _service_client()
    if svc is None:
        return None
    key = _source_key(owner, scan_id, filename, checksum)
    blob = svc.get_blob_client(container=_SOURCES_CONTAINER, blob=key)
    try:
        # Bounded (see _timeouts): a read that stalls must give the worker slot back rather than
        # hold it for minutes. Returning None on the timeout is the existing contract — the caller
        # (scanner.read_cached_source) treats any failure as a cache miss.
        return blob.download_blob(**_timeouts()).readall()
    except Exception:
        return None


# Every container this module writes scan-derived artifacts into. All three are DATA
# (remediated copies, cached originals, rendered previews) that a scan re-populates —
# none holds configuration — so a demo reset should wipe them alongside the DB analytics
# tables. Kept as a list so purge_all() and any future infra tooling share one source of
# truth for "what does acp store in blob".
_DATA_CONTAINERS = (_CONTAINER, _SOURCES_CONTAINER, _RENDER_CONTAINER)


def purge_all(owner: str | None = None) -> dict:
    """Delete every scan-derived blob so a demo reset is a TRUE clean slate — the DB reset
    (store.reset_analytics) drops the remediation_state / applied_fixes / … rows but leaves
    the fixed-file bytes, cached originals and previews sitting in blob storage.

    Deletes the blobs, NOT the containers: `remediated` is provisioned once by deploy infra
    and upload_remediated does not self-heal a missing container, so dropping it would break
    the next remediation. owner=None purges the whole account (matching reset_analytics, which
    is global); pass an owner to scope to one tenant's `{owner}/` prefix. Best-effort and never
    raises: returns {container: deleted_count}, with -1 for a container that errored. No-op
    ({} ) when blob storage isn't configured (local dev)."""
    svc = _service_client()
    if svc is None:
        return {}
    prefix = f"{owner}/" if owner else None
    out: dict = {}
    for cname in _DATA_CONTAINERS:
        try:
            cc = svc.get_container_client(cname)
            n = 0
            for b in cc.list_blobs(name_starts_with=prefix):
                try:
                    cc.delete_blob(b.name)
                    n += 1
                except Exception:
                    # blob vanished mid-iteration or a lease — skip, keep going
                    swallowed("blob.purge_all: deleting a blob during purge_all failed")
            out[cname] = n
        except Exception:
            out[cname] = -1  # container missing / listing failed — surface, don't abort the rest
    return out


def purge_scan(owner: str, scan_id: str, checksums: list[str] | None = None) -> dict:
    """Delete every blob that belongs to ONE scan (BAA/HIPAA right-to-erasure path).

    Blob paths follow the `{owner}/{scan_id}/{filename}` convention defined in _blob_path,
    so listing with the prefix `{owner}/{scan_id}/` finds this scan's bytes across all three
    data containers — EXCEPT the 'sources' container's checksum-keyed entries (upload_source
    keys a file by `{owner}/{checksum}` whenever a pre-download checksum was known, dropping
    scan_id from the path entirely so the same bytes are a cache hit across scans). Pass this
    scan's own downloaded-file checksums (see store.scan_checksums) to also delete those:
    still owner-scoped, so this never reaches another owner's content. Deleting a checksum
    another live scan still references just costs that scan a cache hit on next use — the
    bytes are re-downloaded from source, no data loss — so it's safe to always include every
    checksum this scan touched rather than trying to prove exclusivity first.

    Best-effort and never raises; returns {container: deleted_count} with -1 for a container
    that errored. No-op ({}) when blob storage isn't configured.
    """
    svc = _service_client()
    if svc is None:
        return {}
    prefix = f"{owner}/{scan_id}/"
    out: dict = {}
    for cname in _DATA_CONTAINERS:
        try:
            cc = svc.get_container_client(cname)
            n = 0
            for b in cc.list_blobs(name_starts_with=prefix):
                try:
                    cc.delete_blob(b.name)
                    n += 1
                except Exception:
                    swallowed("blob.purge_scan: deleting a scan's blob failed", scan_id)
            out[cname] = n
        except Exception:
            out[cname] = -1
    if checksums:
        try:
            cc = svc.get_container_client(_SOURCES_CONTAINER)
            extra = 0
            for checksum in checksums:
                try:
                    cc.delete_blob(_checksum_key(owner, checksum))
                    extra += 1
                except Exception:
                    # never cached under this checksum, or already gone — not an error
                    swallowed("blob.purge_scan: deleting a scan's checksum-keyed blob failed", scan_id)
            out[_SOURCES_CONTAINER] = out.get(_SOURCES_CONTAINER, 0) + extra
        except Exception:
            # container missing — the prefix sweep above already recorded that
            swallowed("blob.purge_scan: purging the scan's blobs failed", scan_id)
    return out

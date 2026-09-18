"""Frozen, owner-scoped follow-up reports delivered alongside published copies."""
import base64
import hashlib
import json
import re
from urllib.parse import quote


def _asset_bytes(asset):
    if asset.get('encoding') == 'base64':
        return base64.b64decode(asset['content'], validate=True)
    return asset['content'] if isinstance(asset['content'], bytes) else asset['content'].encode('utf-8')


def _get(store, bundle_id, owner):
    with store._db.cursor() as cur:
        store._db.execute(cur, 'SELECT * FROM release_report_bundles WHERE id=%s AND owner_email=%s', (bundle_id, owner))
        row = store._db.fetchone(cur)
    if row:
        for key in ('assets', 'receipts', 'roots'):
            row[key] = json.loads(row[key])
    return row


def _legacy_asset_identity(release, row, asset):
    """Recover exact generated identities only while the frozen release still matches."""
    if asset.get('report_kind'):
        return {}
    if not release or release.get('scan_id') != row['scan_id'] or _fingerprint(row['release_id'], release)[:24] != row['id']:
        return {}
    suffix = '-' + row['id'][:10]
    if asset['name'] in {f'scan-summary{suffix}.pdf', f'scan-summary{suffix}.html'}:
        return {'report_kind': 'scan_summary'}
    matches = []
    for document in release['documents']:
        name = document['file']
        slug = re.sub(r'[^A-Za-z0-9._-]+', '-', name)[:65].strip('.-') or 'document'
        identity = hashlib.sha256(name.encode()).hexdigest()[:10]
        for kind in ('changes', 'checklist'):
            if asset['name'] in {f'{kind}-{slug}-{identity}{suffix}.pdf', f'{kind}-{slug}-{identity}{suffix}.html'}:
                matches.append(dict(report_kind=kind, file=name, artifact_digest=document.get('artifact_digest')))
    return matches[0] if len(matches) == 1 else {}


def _published_copy_changed(store, sid, owner, release):
    """Live repair records cannot describe different, already-published bytes.

    "Published" means the copy the provider HOLDS: a failed or interrupted retry keeps the
    previously published copy there, so its retained digest counts too.
    """
    return bool(_copy_currency(store, sid, owner, release)[0])


REPORT_COPY_CHANGED = 'The saved copy changed after publication. Keep the recorded reports, or publish the updated copy before refreshing them.'
REPORT_RELEASE_CHANGED = 'Release changed while preparing reports; retry with the current release'


def report_superseded(exc: Exception) -> bool:
    """The two refusals that mean "these reports would describe a different copy" — not faults.

    A publish job that already delivered its copy must not fail on them: the copy landed, and
    the out-of-date state is surfaced by the currency projection instead.
    """
    return isinstance(exc, ValueError) and str(exc) in {REPORT_COPY_CHANGED, REPORT_RELEASE_CHANGED}


def _copy_currency(store, sid, owner, release, assets=()):
    """Compare what the frozen reports describe with the current corrected copy, per file.

    Evaluated on every read, independently of the fingerprint: a correction saved after
    publication leaves the release documents — and so the fingerprint — unchanged, which is
    how reports describing V1 went on reading as current once V2 was saved.
    """
    from release_publication import held_copy
    held = {}
    for document in release['documents']:
        evidence, digest = held_copy(document)
        if evidence:
            held[document['file']] = digest
    if not held:
        return [], []
    records = store.get_file_records(sid, owner=owner, files=sorted(held))
    reported = {asset['file']: asset['artifact_digest'] for asset in assets
                if asset.get('file') and re.fullmatch(r'sha256:[0-9a-f]{64}', str(asset.get('artifact_digest') or ''))}
    changed, legacy = [], []
    for file in sorted(held):
        current = (records.get(file) or {}).get('corrected_sha256')
        if not current or held[file] is None:
            # Either side without an exact identity cannot be compared — the same answer the
            # Release projection gives (identity_unknown), never an implied 'current'.
            legacy.append(file)
        elif held[file] != current:
            changed.append(dict(file=file, reported_artifact_digest=reported.get(file) or 'sha256:' + held[file],
                                current_artifact_digest='sha256:' + current))
    return changed, legacy


def _public(row, store=None):
    if not row:
        return dict(status='not_started', bundle_id=None, reports=[], error=None,
                    currency=None, currency_reason=None, out_of_date_files=[])
    reports = []
    legacy_release = store.release_status(row['release_id'], row['owner_email']) if store and any(not asset.get('report_kind') for asset in row['assets']) else None
    for index, asset in enumerate(row['assets']):
        asset = {**asset, **_legacy_asset_identity(legacy_release, row, asset)}
        receipts = [v for k, v in row['receipts'].items() if k.endswith(':' + str(index))]
        reports.append(dict(name=asset['name'], content_type=asset['content_type'],
                            report_kind=asset.get('report_kind'), file=asset.get('file'), artifact_digest=asset.get('artifact_digest'),
                            url=next((r.get('url') for r in receipts if r.get('url')), None),
                            download_url=f"/scans/{quote(row['scan_id'], safe='')}/release/reports/{row['id']}/{index}"))
    release = store.release_status(row['release_id'], row['owner_email']) if store else None
    outdated = bool(row['status'] == 'completed' and release and
                    _fingerprint(row['release_id'], release)[:24] != row['id'])
    copy_changed, legacy = (_copy_currency(store, row['scan_id'], row['owner_email'], release, row['assets'])
                            if release else ([], []))
    # Regeneration is refused whenever live repair records would describe different bytes —
    # now whether or not the fingerprint moved. Retrying delivery of a frozen bundle that has
    # not completed is not regeneration, and stays as it was.
    blocked = row['status'] == 'completed' and bool(copy_changed)
    currency, reason = (('unknown', None) if not release else
                        ('out_of_date', 'copy_changed_after_publication') if copy_changed else
                        ('out_of_date', 'release_changed') if outdated else
                        ('unknown', 'legacy_identity') if legacy else ('current', None))
    return dict(status=row['status'], bundle_id=row['id'], scan_id=row['scan_id'], release_id=row['release_id'], reports=reports, error=row.get('error'), can_regenerate=outdated and not blocked,
                regeneration_blocked=REPORT_COPY_CHANGED if blocked else None,
                currency=currency, currency_reason=reason, out_of_date_files=copy_changed)


def _enqueue(store, row):
    return store.enqueue_job('publish_release_reports', dict(bundle_id=row['id'], owner=row['owner_email']), scan_id=row['scan_id'])


REPORT_FORMAT = 'pdf-v6-word-structural-report-locations'


def _fingerprint(release_id, release):
    return hashlib.sha256(json.dumps([REPORT_FORMAT, release_id, release['documents'], release['roots']], sort_keys=True).encode()).hexdigest()


def queue_release_reports(store, scan_id, owner, release_id):
    from release_reports import build_release_reports
    release = store.release_status(release_id, owner)
    if not release or release['scan_id'] != scan_id:
        raise KeyError('Release not found')
    fingerprint = _fingerprint(release_id, release)
    identity = fingerprint[:24]
    existing = _get(store, identity, owner)
    if existing:
        return _public(existing, store)
    if _published_copy_changed(store, scan_id, owner, release):
        raise ValueError(REPORT_COPY_CHANGED)
    # Download/render optional visuals before taking the local release row lock.
    # Recheck the snapshot and bundle under the lock before freezing any assets.
    assets = build_release_reports(store, scan_id, owner, release_id)
    with store.transaction():
        # Serialize creation and retries for this owner's release.
        with store._db.cursor() as cur:
            store._db.execute(cur, 'UPDATE release_executions SET id=id WHERE id=%s AND owner_email=%s', (release_id, owner))
        existing = _get(store, identity, owner)
        if existing:
            return _public(existing, store)
        current = store.release_status(release_id, owner)
        if not current or _fingerprint(release_id, current) != fingerprint:
            raise ValueError(REPORT_RELEASE_CHANGED)
        if _published_copy_changed(store, scan_id, owner, current):
            raise ValueError(REPORT_COPY_CHANGED)
        names = {a['name']: a['name'].rsplit('.', 1)[0] + '-' + identity[:10] + '.' + a['name'].rsplit('.', 1)[1] for a in assets}
        frozen = []
        for asset in assets:
            binary = asset['content_type'] in ('application/pdf', 'application/vnd.openxmlformats-officedocument.wordprocessingml.document')
            content = base64.b64encode(asset['content']).decode('ascii') if binary else asset['content'].decode('utf-8') if isinstance(asset['content'], bytes) else asset['content']
            if asset['content_type'].startswith('text/html'):
                for old, new in names.items():
                    content = content.replace('href="' + old + '"', 'href="' + new + '"')
            frozen.append(dict(report_kind=asset.get('report_kind'), file=asset.get('file'), artifact_digest=asset.get('artifact_digest'), name=names[asset['name']], content_type=asset['content_type'], content=content, encoding='base64' if binary else 'utf-8'))
        with store._db.cursor() as cur:
            store._db.execute(cur, '''INSERT INTO release_report_bundles
                (id,release_id,scan_id,owner_email,assets,roots,receipts,status,created_at,updated_at)
                VALUES(%s,%s,%s,%s,%s,%s,'{}','queued',%s,%s)''',
                (identity, release_id, scan_id, owner, json.dumps(frozen), json.dumps(release['roots']), store._now(), store._now()))
        row = _get(store, identity, owner)
        _enqueue(store, row)
        return _public(row, store)


def get_latest_release_reports(store, sid, owner):
    with store._db.cursor() as cur:
        store._db.execute(cur, 'SELECT id FROM release_report_bundles WHERE scan_id=%s AND owner_email=%s ORDER BY created_at DESC,id DESC LIMIT 1', (sid, owner))
        row = store._db.fetchone(cur)
    return _public(_get(store, row['id'], owner), store) if row else _public(None)


def get_release_report_asset(store, sid, owner, bundle_id, index):
    row = _get(store, bundle_id, owner)
    if not row or row['scan_id'] != sid or type(index) is not int or not 0 <= index < len(row['assets']):
        raise KeyError('Report not found')
    asset = row['assets'][index]
    return {**asset, 'content': _asset_bytes(asset)}


def retry_release_reports(store, sid, owner):
    latest = get_latest_release_reports(store, sid, owner)
    if not latest['bundle_id']:
        raise KeyError('Report not found')
    if latest.get('regeneration_blocked'):
        raise ValueError(latest['regeneration_blocked'])
    if latest['status'] == 'completed' and (latest.get('can_regenerate') or any(report['content_type'].startswith('text/html') for report in latest['reports']) or not any(report['name'].startswith('changes-') for report in latest['reports'])):
        result = queue_if_release_settled(store, sid, owner, _get(store, latest['bundle_id'], owner)['release_id'])
        if not result:
            raise ValueError('Wait for publication to finish before generating PDF reports')
        return result
    with store.transaction():
        with store._db.cursor() as cur:
            store._db.execute(cur, "UPDATE release_report_bundles SET status='queued',error=NULL,updated_at=%s WHERE id=%s AND owner_email=%s AND status='failed'", (store._now(), latest['bundle_id'], owner))
            retry = cur.rowcount == 1
        row = _get(store, latest['bundle_id'], owner)
        if retry:
            _enqueue(store, row)
        return _public(row, store)


def _upload(root, asset, tokens, key):
    import publish
    import scanner
    data = _asset_bytes(asset)
    digest = hashlib.sha256(data).hexdigest()
    if root['provider'] == 'sharepoint':
        token = tokens.get('sp')
        if not token:
            raise ValueError('SharePoint connection is unavailable')
        drive = root['provider_location'].removeprefix('graph:')
        drive = None if drive == 'me' else drive
        existing = publish._sp_child(token, drive, root['folder_id'], asset['name'])
        if existing:
            if not publish._sp_content_matches(token, drive, existing['id'], digest):
                raise ValueError('A report with different content already exists')
            return dict(id=existing['id'], url=existing.get('webUrl'), checksum=digest)
        base = f"{scanner._sp_base(drive)}/items/{root['folder_id']}:/{quote(asset['name'], safe='')}:"
        result = scanner._sp_write(token, put_url=base + '/content', session_url=base + '/createUploadSession',
                                   content=data, content_type=asset['content_type'], conflict_behavior='fail', force_session=True)
        if not result.get('id') or not publish._sp_content_matches(token, drive, result['id'], digest):
            raise ValueError('Report upload could not be verified')
        return dict(id=result['id'], url=result.get('webUrl'), checksum=digest)
    if root['provider'] == 'drive':
        from handlers import _make_svc
        if not tokens.get('drive'):
            raise ValueError('Google Drive connection is unavailable')
        svc = _make_svc('drive', tokens)
        result = publish.upload_published(svc, root['folder_id'], asset['name'], data, idempotency_key=key, return_details=True)
        # Read back bytes: absent provider checksums are not verification evidence.
        actual = svc.files().get_media(fileId=result['id']).execute()
        if hashlib.sha256(actual).hexdigest() != digest:
            raise ValueError('Report upload could not be verified')
        return {**result, 'checksum': digest}
    raise ValueError('Unsupported report destination')


def process_release_reports(store, bundle_id, owner):
    import core
    from release_continuation import require_grants
    row = _get(store, bundle_id, owner)
    if not row:
        raise KeyError('Report not found')
    if row['status'] == 'completed':
        return _public(row, store)
    try:
        require_grants(store, owner, review=False)
        release = store.release_status(row['release_id'], owner)
        if not release or release['scan_id'] != row['scan_id'] :
            raise ValueError('Published folder is not available')
        current = {(r['provider'], r['provider_location'], r['folder_id']) for r in release['roots']}
        if any((r['provider'], r['provider_location'], r['folder_id']) not in current for r in row['roots']):
            raise ValueError('Published folder changed')
        tokens = core.get_scan_tokens(row['scan_id']) if row['roots'] else {}
        with store._db.cursor() as cur:
            store._db.execute(cur, "UPDATE release_report_bundles SET status='publishing',error=NULL,updated_at=%s WHERE id=%s AND owner_email=%s AND status!='completed'", (store._now(), bundle_id, owner))
        for root_index, root in enumerate(row['roots']):
            for asset_index, asset in enumerate(row['assets']):
                key = str(root_index) + ':' + str(asset_index)
                if key in row['receipts']:
                    continue
                result = _upload(root, asset, tokens, bundle_id + ':' + key)
                with store.transaction():
                    with store._db.cursor() as cur:
                        store._db.execute(cur, 'UPDATE release_report_bundles SET id=id WHERE id=%s AND owner_email=%s', (bundle_id, owner))
                    row['receipts'] = _get(store, bundle_id, owner)['receipts']
                    row['receipts'][key] = result
                    with store._db.cursor() as cur:
                        store._db.execute(cur, 'UPDATE release_report_bundles SET receipts=%s,updated_at=%s WHERE id=%s AND owner_email=%s', (json.dumps(row['receipts']), store._now(), bundle_id, owner))
        with store._db.cursor() as cur:
            store._db.execute(cur, "UPDATE release_report_bundles SET status='completed',error=NULL,updated_at=%s WHERE id=%s AND owner_email=%s", (store._now(), bundle_id, owner))
    except Exception:
        # Keep provider errors/tokens out of public metadata and leave document receipts intact.
        with store._db.cursor() as cur:
            store._db.execute(cur, "UPDATE release_report_bundles SET status='failed',error=%s,updated_at=%s WHERE id=%s AND owner_email=%s AND status!='completed'", ('Reports could not be delivered. Download them here or reconnect and retry.', store._now(), bundle_id, owner))
    return _public(_get(store, bundle_id, owner), store)


def queue_if_release_settled(store, sid, owner, release_id=None):
    release = store.release_status(release_id, owner) if release_id else store.release_for_scan(sid, owner)
    if not release or release.get('scan_id') != sid:
        return None
    release = store.release_status(release['id'], owner)
    documents = release.get('documents', [])
    if len(documents) < int(release.get('documents_total') or 0) or not documents:
        return None
    if any(d.get('status') not in {'published', 'failed'} for d in documents):
        return None
    return queue_release_reports(store, sid, owner, release['id'])

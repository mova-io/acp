"""Owner- and exact-version-bound saved-copy checks; never rewrite or publish bytes."""
from release_candidate_assessment import saved_assessment, assess_candidate
from release_artifacts import ReleaseArtifactError, require_current_record


_UNSET = object()


def current_assessment(store, scan_id, owner, file, *, evaluator=_UNSET, record=_UNSET):
    if record is _UNSET:
        record = store.get_file_records(scan_id, files=[file], owner=owner).get(file)
    if not record or not record.get('corrected_sha256') or not record.get('remediated_at'):
        return None
    evidence = saved_assessment(store, scan_id, owner, file, record['corrected_sha256'])
    if not evidence or evidence.get('remediated_at') != record['remediated_at']:
        return None
    from verification_identity import evaluator_identity, scope_identity
    if evaluator is _UNSET:
        evaluator = evaluator_identity()
    if not evaluator:
        return None
    scope = store.scope_for_file(scan_id, file, store.get_scan_scope(scan_id, refresh=True))
    if evidence.get('assessment_scope') != scope_identity(scope) or evidence.get('evaluator_identity') != evaluator:
        return None
    return evidence


def project_documents(store, scan_id, owner, documents):
    from verification_identity import evaluator_identity
    evaluator = evaluator_identity()
    records = store.get_file_records(scan_id, files=[row['file'] for row in documents], owner=owner)
    return [{**row, 'corrected_sha256': records.get(row['file'], {}).get('corrected_sha256'),
             'remediated_at': records.get(row['file'], {}).get('remediated_at'),
             'corrected_copy_assessment': current_assessment(store, scan_id, owner, row['file'], evaluator=evaluator, record=records.get(row['file']))}
            for row in documents]


def _reconcile_targets(store, scan_id, owner, file):
    """Trigger (b): complete evidence was just recorded for the current copy. Retire review rows
    whose targets a different verified fix removed. Evidence only — no write, no approval — and
    best-effort: a verification result must never be lost to this bookkeeping."""
    if not hasattr(store, '_db'):
        return None
    try:
        from review_target_reconciliation import reconcile_from_saved_assessment
        return reconcile_from_saved_assessment(store, scan_id, file, owner)
    except Exception:
        from swallowed import swallowed
        swallowed('saved_copy_verification: reconciling removed review targets failed', scan_id)
        return None


def verify_saved_copy(store, scan_id, owner, file, digest, remediated_at):
    evidence = _verify_saved_copy(store, scan_id, owner, file, digest, remediated_at)
    _reconcile_targets(store, scan_id, owner, file)
    return evidence


def _verify_saved_copy(store, scan_id, owner, file, digest, remediated_at):
    # assess_candidate verifies ownership, actual bytes, current record both before and
    # after the detector runs, and outstanding approved writes. Only evidence is recorded.
    if not digest or not remediated_at:
        raise ReleaseArtifactError('Choose the current saved copy before retrying verification.', category='artifact_provenance_unknown')
    previous = current_assessment(store, scan_id, owner, file) if hasattr(store, '_db') else None
    reusable = (previous and previous.get('assessment_ok') is True
                and previous.get('assessment_status') == 'analysed'
                and not previous.get('skipped_rules')
                and isinstance(previous.get('remaining_issues'), list)
                and isinstance(previous.get('remaining_criteria'), list))
    if reusable:
        require_current_record(store, scan_id, file, digest, remediated_at,
                               owner=owner, allow_remaining_issues=True)
        if store.count_unapplied_approved_values(scan_id, file):
            raise ReleaseArtifactError('Approved changes still need to be written before verification.',
                                       category='approved_changes_unapplied')
        import blob
        from hashlib import sha256
        data = blob.download_remediated(owner, scan_id, file)
        if not data or sha256(data).hexdigest() != digest:
            raise ReleaseArtifactError('Stored bytes changed before verification reuse.')
        require_current_record(store, scan_id, file, digest, remediated_at,
                               owner=owner, allow_remaining_issues=True)
        if store.count_unapplied_approved_values(scan_id, file):
            raise ReleaseArtifactError('Approved changes arrived during verification reuse.',
                                       category='approved_changes_unapplied')
        if current_assessment(store, scan_id, owner, file) != previous:
            raise ReleaseArtifactError('The verification scope or evaluator changed during reuse.')
        return {**previous, 'assessment_reused': True}
    return assess_candidate(store, scan_id, file, owner, digest, remediated_at,
                            allow_remaining_issues=True)

"""Retire review rows whose exact targets a DIFFERENT, verified fix has removed from the document.

THE CASE (reproduced synthetically in the tests). One Word body picture carried two
review rows: 1.1.1 asking for alt text (pending, with a drafted description) and 1.4.5 replacing
the picture with its OCR transcript (approved). The 1.4.5 writer deleted the picture, the fresh
independent assessment of the saved copy was complete and reported nothing left — and the 1.1.1
row stayed pending forever. `store._superseded_items` only retracts on a trace reading PASS or
NOT_EVALUATED, and 1.1.1 reads REVIEW for every format, so nothing could ever retire it. The
inbox said "all clear" while the ledger said one finding was unresolved, and approving the stale
row would have asked a writer to describe a picture that no longer exists (or, through name
based resolution, a DIFFERENT picture sharing its name).

WHAT THIS MODULE PROVES BEFORE IT RECORDS ANYTHING, every condition fail-closed:

  * the evidence is a COMPLETE assessment of the exact current corrected bytes: status
    'analysed', zero skipped rules, no engine errors, the criterion inside the assessed scope,
    and a digest equal to both the bytes in hand and `file_records.corrected_sha256` — compared
    again under the per-file lock at commit, so a reconciliation computed against a copy that
    has since been replaced records nothing;
  * every target of the row resolves, uniquely, to drawing placement(s) in the BEFORE bytes
    (the assessed source when it is cached, else the exact prior corrected copy), and none of
    them survives in the corrected bytes — checked on the drawing elements themselves (docPr id,
    media reference) and on every locator the row carries, not merely on relationships;
  * an applied AND verified row of a different criterion targets the same placement(s): the
    "removed_by" fix. Without one, a vanished picture is unexplained and nothing is recorded;
  * the assessment reports no remaining issue of the row's criterion that locates, or could
    locate, the removed target (an unlocated remaining issue of that criterion blocks);
  * the exact finding_disposition rows are identified by instance key, never by count.

WHAT IT WRITES, and nothing else: ONE decision_log line per (row, decision version, proposals,
corrected artifact, batch) and a compare-and-set move of those exact findings to
`superseded_by_reassessment`. It never changes a row's status, never writes alt text, never
approves, never passes a criterion, never touches certification. Replays are no-ops.

Supersession is derived at READ time from that line (`current_removals`), bound to the row's
current decision_version and proposal snapshots, the current corrected sha, the current
Remediate batch and the current assessment scope. Any of those moving makes the row ordinary
work again until the full check is re-run against the new bytes with new complete evidence.
"""
from __future__ import annotations

import hashlib
import io
import json
import re
import zipfile
from dataclasses import dataclass

ACTION = 'hitl.target_removed_by_verified_fix'
REASON = 'target_removed_by_verified_fix'
TRACE_REASON = 'criterion_reassessed'
APPLY_EVIDENCE = 'apply.verification'
SAVED_EVIDENCE = 'release.corrected_copy_assessed'

# Findings a verified removal may retire. Terminal ones (resolved_verified, excluded_by_policy)
# are facts of their own and are never rewritten.
_OPEN_DISPOSITIONS = {None, 'awaiting_review', 'approved_pending_verification',
                      'unchanged_no_fix', 'remediation_failed'}

_W = 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'
_WP = 'http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing'
_A = 'http://schemas.openxmlformats.org/drawingml/2006/main'
_R = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships'
_PARTS = re.compile(r'^word/(?:document|header\d*|footer\d*|footnotes|endnotes)\.xml$')
_ALIAS = re.compile(r'^docx:drawing:([^:]+):paragraph:(\d+)$', re.I)
_IMAGE = re.compile(r'^image\s+(\d+)$', re.I)


# ── placements ─────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Placement:
    part: str
    ordinal: int                 # position among this part's w:drawing elements
    docpr_id: str | None
    name: str | None
    rids: tuple
    media: tuple                 # canonical zip paths the drawing's own blips reference
    paragraphs: frozenset        # indices of every enclosing w:p (Body.Descendants order)
    media_digests: tuple = ()    # sha256 of the referenced media bytes

    @property
    def key(self):
        # WITHIN ONE document only. An ordinal shifts when an earlier drawing is removed, so a
        # key from the before bytes must never be compared with one from the after bytes; use
        # `same_placement` for any cross-version question.
        return (self.part, self.ordinal)

    def describe(self):
        return {'part': self.part, 'docpr_id': self.docpr_id, 'name': self.name}


class _Doc:
    def __init__(self, items, media_index):
        self.items = items
        self.media_index = media_index


def _nearest(element, tag):
    for ancestor in element.iterancestors():
        if ancestor.tag == tag:
            return ancestor
    return None


def placements(data: bytes) -> _Doc:
    """Every Word drawing placement, read from the elements themselves. Raises ValueError."""
    from lxml import etree
    from apply_office_image_of_text import _media_index, _rid_map
    parser = etree.XMLParser(resolve_entities=False, no_network=True)
    items = []
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            media_index = _media_index(z)
            for part in z.namelist():
                if not _PARTS.match(part):
                    continue
                root = etree.fromstring(z.read(part), parser)
                rid_map = _rid_map(z, part)
                paragraphs = {p: i for i, p in enumerate(root.iter('{%s}p' % _W))}
                for ordinal, drawing in enumerate(root.iter('{%s}drawing' % _W)):
                    # A text box can nest a picture: each element belongs to its NEAREST drawing.
                    own = lambda el: _nearest(el, '{%s}drawing' % _W) is drawing  # noqa: E731
                    props = [p for p in drawing.iter('{%s}docPr' % _WP) if own(p)]
                    blips = [b for b in drawing.iter('{%s}blip' % _A) if own(b)]
                    rids = tuple(b.get('{%s}embed' % _R) for b in blips
                                 if b.get('{%s}embed' % _R))
                    enclosing = frozenset(paragraphs[a] for a in drawing.iterancestors()
                                          if a in paragraphs)
                    media = tuple(rid_map[r] for r in rids if r in rid_map)
                    names = set(z.namelist())
                    items.append(Placement(
                        part=part, ordinal=ordinal,
                        docpr_id=(props[0].get('id') if props else None),
                        name=(props[0].get('name') if props else None),
                        rids=rids, media=media, paragraphs=enclosing,
                        media_digests=tuple(hashlib.sha256(z.read(m)).hexdigest()
                                            for m in media if m in names)))
    except (zipfile.BadZipFile, etree.XMLSyntaxError, KeyError, OSError) as exc:
        raise ValueError('unreadable_document') from exc
    return _Doc(items, media_index)


def _norm(text):
    return re.sub(r'\s+', ' ', str(text or '').strip()).casefold()


def resolve(locator, doc: _Doc, *, loose_names=False):
    """The placement(s) a locator addresses, or None when it addresses nothing UNIQUELY.

    'image N' names a media part (every placement of it); 'docx:drawing:{id}:paragraph:{i}' is
    the Office analyser's finding location; 'part#fragment' is the alt writer's locator, tried by
    docPr name and then by relationship id exactly as apply_alt.resolve_target does — except that
    a fragment matching more than one placement is AMBIGUOUS here rather than first-match, so a
    shared name can never be read as proof about the wrong picture.
    """
    text = str(locator or '').strip()
    if not text:
        return None
    match = _IMAGE.match(text)
    if match:
        index = int(match.group(1)) - 1
        if not 0 <= index < len(doc.media_index):
            return None
        found = tuple(p for p in doc.items if doc.media_index[index] in p.media)
        return found or None
    match = _ALIAS.match(text)
    if match:
        drawing_id, paragraph = match.group(1), int(match.group(2))
        found = tuple(p for p in doc.items if p.part == 'word/document.xml'
                      and p.docpr_id is not None and p.docpr_id.casefold() == drawing_id.casefold()
                      and paragraph in p.paragraphs)
        return found if len(found) == 1 else None
    if '#' in text:
        part, _, fragment = text.partition('#')
        part, fragment = part.strip(), fragment.strip()
        if not part or not fragment:
            return None
        in_part = [p for p in doc.items if p.part.casefold() == part.casefold()]
        if loose_names:
            named = [p for p in in_part if p.name is not None and _norm(p.name) == _norm(fragment)]
        else:
            named = [p for p in in_part if p.name is not None and p.name.strip() == fragment]
        if named:
            return tuple(named) if len(named) == 1 else None
        by_rid = [p for p in in_part if any(r == fragment or (loose_names and r.casefold() == fragment.casefold())
                                            for r in p.rids)]
        return tuple(by_rid) if len(by_rid) == 1 else None
    return None


def same_placement(a: Placement, b: Placement) -> bool:
    """Cross-version identity: the same drawing (part + docPr id) or the same picture (a shared
    media path or identical media bytes). Never the ordinal — that shifts when an earlier
    drawing is removed. Deliberately generous: every caller uses it to BLOCK retirement."""
    if a.part == b.part and a.docpr_id is not None and a.docpr_id == b.docpr_id:
        return True
    return bool(set(a.media) & set(b.media) or set(a.media_digests) & set(b.media_digests))


def _present(placement: Placement, corrected: _Doc) -> bool:
    """Does anything of this placement survive? Conservative: any trace counts as present."""
    if placement.docpr_id is None and not placement.media:
        return True                   # nothing to prove absence against
    return any(same_placement(placement, other) for other in corrected.items)


# ── evidence ───────────────────────────────────────────────────────────────────────────────

def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _criterion(rule_id):
    from store import _extract_sc
    return _extract_sc(str(rule_id or ''))


def _incomplete(evidence, corrected_sha):
    """None when the evidence is a complete assessment of exactly these bytes, else why not."""
    if not isinstance(evidence, dict):
        return 'verification_missing'
    if evidence.get('assessment') not in (APPLY_EVIDENCE, SAVED_EVIDENCE):
        return 'verification_kind_unknown'
    if evidence.get('artifact_sha256') != corrected_sha:
        return 'verification_digest_mismatch'
    if evidence.get('assessment_ok') is not True:
        return 'verification_failed'
    if evidence.get('assessment_status') != 'analysed':
        return 'verification_incomplete'
    # MISSING is unknown, never zero: each field must be present and say "nothing skipped,
    # nothing errored" in so many words.
    if 'skipped_rules' not in evidence:
        return 'verification_skipped_rules_unknown'
    skipped = evidence['skipped_rules']
    if isinstance(skipped, bool) or skipped not in (0, []):
        return 'verification_partial'
    if 'errors' not in evidence or not isinstance(evidence['errors'], list):
        return 'verification_errors_unknown'
    if evidence['errors']:
        return 'verification_errors'
    if evidence.get('assessment_reused') is True:
        return 'verification_reused'
    if not isinstance(evidence.get('remaining_issues'), list):
        return 'verification_incomplete'
    return None


def evidence_from_verification(verification, *, verified_at):
    """An apply job's own re-scan, shaped like saved-copy evidence. None when it is unusable."""
    if verification is None:
        return None
    assessment = verification.assessment or {}
    return {'assessment': APPLY_EVIDENCE,
            'artifact_sha256': verification.artifact_sha256,
            'assessment_ok': bool(verification.ok and assessment.get('status') == 'analysed'
                                  and isinstance(assessment.get('issues'), list)),
            'assessment_status': assessment.get('status') or 'unavailable',
            'skipped_rules': assessment.get('skipped_rules'),
            'errors': assessment.get('errors'),
            'remaining_issues': assessment.get('issues'),
            'remaining_criteria': sorted(verification.residual),
            'verified_at': verified_at}


def persisted_evidence(store, scan_id, file, owner, record):
    """The newest release.corrected_copy_assessed line for the CURRENT corrected artifact.

    Bound to the digest, the remediated_at and the assessment scope current_assessment() binds
    to, and to the owner as actor. Deliberately NOT to the current evaluator identity: that
    identity hashes every api/*.py, so any deploy (this one included) would make the completed
    production run unreconcilable forever. The evidence is still a complete check of these exact
    bytes by the evaluator deployed when it ran.
    """
    sha = (record or {}).get('corrected_sha256')
    if not sha or not owner:
        return None
    with store._db.cursor() as cur:
        store._db.execute(cur,
            "SELECT id,ts,detail FROM decision_log WHERE scan_id=%s AND file=%s AND actor=%s "
            "AND action=%s ORDER BY ts DESC,id DESC", (scan_id, file, owner, SAVED_EVIDENCE))
        lines = store._db.fetchall(cur)
    for line in lines:
        try:
            detail = json.loads(line['detail'])
        except (TypeError, ValueError):
            continue
        if not isinstance(detail, dict) or detail.get('artifact_sha256') != sha:
            continue
        if detail.get('remediated_at') != record.get('remediated_at'):
            return None
        return {**detail, 'assessment': SAVED_EVIDENCE, 'verified_at': line['ts'],
                'evidence_line_id': line['id']}
    return None


# ── store reads ────────────────────────────────────────────────────────────────────────────

def current_batch(store, scan_id):
    with store._db.cursor() as cur:
        store._db.execute(cur,
            "SELECT execution_id FROM stage_executions WHERE scan_id=%s AND stage='remediate' "
            "AND is_current=1 ORDER BY created_at DESC,execution_id DESC LIMIT 1", (scan_id,))
        return (store._db.fetchone(cur) or {}).get('execution_id')


def _scope(store, scan_id, file, *, refresh=False):
    from verification_identity import scope_identity
    scope = store.scope_for_file(scan_id, file, store.get_scan_scope(scan_id, refresh=refresh))
    return scope, scope_identity(scope)


def _criterion_assessed(scope, file, sc):
    from assessment_selection import selected_for_file
    codes = selected_for_file(scope, file)
    return codes is None or sc in codes


def _eligible(row):
    status = str(row.get('status') or 'pending')
    return status in ('pending', 'in_review') or (status == 'approved' and not row.get('applied'))


def _targets(row):
    instances = row.get('proposals') or row.get('evidence') or []
    return [str(i.get('locator') or '').strip() for i in instances if isinstance(i, dict)]


def _snapshots(row):
    value = row.get('proposal_snapshot_ids')
    return list(value) if isinstance(value, list) else []


def _line_id(scan_id, file, row, corrected_sha, batch_id, removed_by):
    material = json.dumps([ACTION, scan_id, file, str(row['id']), int(row.get('decision_version') or 0),
                           _snapshots(row), corrected_sha, batch_id, removed_by],
                          separators=(',', ':'), ensure_ascii=False)
    return 'trm' + hashlib.sha256(material.encode()).hexdigest()[:21]


def _c1(detail):
    return {'removed_by_item_id': detail.get('removed_by_item_id'),
            'removed_by_rule_id': detail.get('removed_by_rule_id'),
            'targets': list(detail.get('targets') or []),
            'finding_ids': list(detail.get('finding_ids') or []),
            'corrected_artifact_sha256': detail.get('corrected_artifact_sha256'),
            'source_artifact_sha256': detail.get('source_artifact_sha256'),
            'verified_at': detail.get('verified_at'),
            'assessment': detail.get('assessment'),
            'assessment_status': detail.get('assessment_status'),
            'skipped_rules': detail.get('skipped_rules')}


def current_removals(store, rows) -> dict:
    """{item_id: superseded_evidence (C1)} for rows whose target removal is CURRENT.

    One decision_log query per scan; the corrected shas, batch and scope are read only for a scan
    that has such lines at all, so an ordinary inbox read pays one indexed lookup per scan.
    """
    eligible = [r for r in rows if r.get('id') and r.get('scan_id') and _eligible(r)]
    out = {}
    for scan_id in sorted({r['scan_id'] for r in eligible}):
        with store._db.cursor() as cur:
            store._db.execute(cur,
                "SELECT id,ts,file,detail FROM decision_log WHERE scan_id=%s AND action=%s "
                "ORDER BY ts DESC,id DESC", (scan_id, ACTION))
            lines = store._db.fetchall(cur)
            if not lines:
                continue
            store._db.execute(cur,
                "SELECT file,corrected_sha256 FROM file_records WHERE scan_id=%s", (scan_id,))
            shas = {r['file']: r['corrected_sha256'] for r in store._db.fetchall(cur)}
        batch = current_batch(store, scan_id)
        scopes = {}
        by_item = {}
        for line in lines:
            try:
                detail = json.loads(line['detail'])
            except (TypeError, ValueError):
                continue
            if isinstance(detail, dict) and detail.get('item_id'):
                by_item.setdefault(str(detail['item_id']), []).append(detail)
        for row in (r for r in eligible if r['scan_id'] == scan_id):
            for detail in by_item.get(str(row['id']), []):
                file = row.get('file')
                if (detail.get('file') != file
                        or int(detail.get('decision_version') or 0) != int(row.get('decision_version') or 0)
                        or list(detail.get('proposal_snapshot_ids') or []) != _snapshots(row)
                        or list(detail.get('targets') or []) != _targets(row)
                        or not detail.get('corrected_artifact_sha256')
                        or detail.get('corrected_artifact_sha256') != shas.get(file)
                        or not batch or detail.get('batch_id') != batch):
                    continue
                if file not in scopes:
                    try:
                        scopes[file] = _scope(store, scan_id, file)[1]
                    except Exception:
                        scopes[file] = object()     # unreadable scope never matches
                if detail.get('assessment_scope') != scopes[file]:
                    continue
                out[str(row['id'])] = _c1(detail)
                break
    return out


def removal_for(store, item) -> dict | None:
    """The current target-removal evidence for ONE row, or None."""
    if not item:
        return None
    return current_removals(store, [item]).get(str(item.get('id')))


def removed_item_ids(store, scan_id, file) -> set:
    """Ids of this file's eligible rows whose targets a verified fix has currently removed."""
    with store._db.cursor() as cur:
        store._db.execute(cur, "SELECT * FROM hitl_queue WHERE scan_id=%s AND file=%s", (scan_id, file))
        rows = [store._decode_proposals(r) for r in store._db.fetchall(cur)]
    return set(current_removals(store, rows))


def protected_findings(store, scan_id, file, batch_id) -> set:
    """Finding ids whose superseded_by_reassessment rests on CURRENT removal evidence."""
    with store._db.cursor() as cur:
        store._db.execute(cur, "SELECT * FROM hitl_queue WHERE scan_id=%s AND file=%s", (scan_id, file))
        rows = [store._decode_proposals(r) for r in store._db.fetchall(cur)]
    if batch_id != current_batch(store, scan_id):
        return set()
    return {fid for evidence in current_removals(store, rows).values()
            for fid in evidence.get('finding_ids') or []}


_SYNC = {'pending': 'awaiting_review', 'in_review': 'awaiting_review',
         'approved': 'approved_pending_verification',
         'rejected': 'unchanged_no_fix', 'skipped': 'unchanged_no_fix'}


def lapsed_findings(store, scan_id, file) -> list[dict]:
    """Findings this module retired whose removal evidence is no longer CURRENT. Reads only.

    Evidence lapses whenever anything it is bound to moves: the corrected artifact, the row's
    decision_version, its proposals/snapshots/targets, the batch or the scope. Only findings
    whose supersession evidence came from THIS module qualify; anything else that ever writes
    superseded_by_reassessment keeps its own semantics.
    """
    batch_id = current_batch(store, scan_id)
    if not batch_id:
        return []
    with store._db.cursor() as cur:
        store._db.execute(cur,
            "SELECT finding_id,revision,review_item_id,fix_evidence_ids FROM finding_disposition "
            "WHERE scan_id=%s AND batch_id=%s AND file=%s AND disposition='superseded_by_reassessment'",
            (scan_id, batch_id, file))
        retired = [dict(r, batch_id=batch_id) for r in store._db.fetchall(cur)
                   if any(str(e).startswith('target_removed:')
                          for e in json.loads(r.get('fix_evidence_ids') or '[]'))]
    if not retired:
        return []
    protected = protected_findings(store, scan_id, file, batch_id)
    return [r for r in retired if r['finding_id'] not in protected]


def reopen_lapsed(store, scan_id, file) -> int:
    """Reopen every lapsed retirement of `file`. Caller holds whatever locks its path requires.

    The derived row flag lapses on its own (current_removals is computed at read time), but the
    ledger row is persisted: left alone it would keep reporting the finding superseded while its
    review row is ordinary work again. So the retired finding goes back to the disposition its
    review row implies (the mapping sync_hitl_finding_dispositions uses), with an append-only
    event naming why. Called where the artifact changes (record_remediation, the retry CAS), where
    a row's binding changes (sync_hitl_finding_dispositions: proposal refresh, re-decision), and by
    the narrow reconcile-targets call before it looks for new proof. If the removal still holds,
    the next complete check retires it again against the evidence that is current then.
    """
    lapsed = lapsed_findings(store, scan_id, file)
    if not lapsed:
        return 0
    with store._db.cursor() as cur:
        store._db.execute(cur,
            "SELECT corrected_sha256 FROM file_records WHERE scan_id=%s AND file=%s", (scan_id, file))
        sha = (store._db.fetchone(cur) or {}).get('corrected_sha256') or 'none'
    reopened = 0
    for finding in lapsed:
        item = store.get_hitl_item(finding['review_item_id']) if finding.get('review_item_id') else None
        disposition = _SYNC.get(str((item or {}).get('status') or 'pending'), 'awaiting_review')
        revision = int(finding.get('revision') or 0)
        store.transition_finding_disposition(
            scan_id, finding['batch_id'], finding['finding_id'], disposition, expected_revision=revision,
            event_id=f"target-removal-lapsed:{scan_id}:{finding['batch_id']}:{finding['finding_id']}:r{revision}",
            review_item_id=finding.get('review_item_id'),
            fix_evidence_ids=[f'target_removal_lapsed:{sha}'])
        reopened += 1
    return reopened


def reopen_lapsed_locked(store, scan_id, file) -> int:
    """reopen_lapsed under the documented lock order (review rows, then the file), for callers
    that hold no locks of their own. A lock-free read first, so a poll with nothing lapsed takes
    no write lock at all; the answer is re-derived under the locks before anything is written."""
    lapsed = lapsed_findings(store, scan_id, file)
    if not lapsed:
        return 0
    with store.transaction():
        begin_write(store)
        rows = _file_rows(store, scan_id, file)
        lock_rows(store, [r['id'] for r in rows if _eligible(r)]
                  + [f['review_item_id'] for f in lapsed if f.get('review_item_id')])
        lock_file(store, scan_id, file)
        return reopen_lapsed(store, scan_id, file)


# ── reconciliation ─────────────────────────────────────────────────────────────────────────

def _recorded_source_digests(store, scan_id, file, batch_id):
    with store._db.cursor() as cur:
        store._db.execute(cur,
            "SELECT DISTINCT source_sha256 FROM remediation_contribution_proposals "
            "WHERE scan_id=%s AND file=%s AND run_id=%s", (scan_id, file, batch_id))
        return {r['source_sha256'] for r in store._db.fetchall(cur) if r.get('source_sha256')}


def _verified_fixer(store, row, batch_id, findings):
    if str(row.get('status') or '') != 'approved' or not row.get('applied'):
        return False
    if row.get('validated'):
        return True
    sc = _criterion(row.get('rule_id'))
    return any(f['file'] == row['file'] and f['rule_id'] == sc
               and f.get('disposition') == 'resolved_verified' for f in findings)


def _plan_row(row, *, file, before, after, fixers, findings, evidence, scope, corrected_sha):
    """(plan, None) when every condition holds for this row, else (None, reason)."""
    sc = _criterion(row.get('rule_id'))
    if not sc:
        return None, 'criterion_unknown'
    if not _criterion_assessed(scope, file, sc):
        return None, 'criterion_not_assessed'
    targets = _targets(row)
    if not targets or not all(targets):
        return None, 'target_unknown'
    placements_ = {}
    for locator in targets:
        found = resolve(locator, before)
        if not found:
            return None, 'target_unresolved'
        placements_.update({p.key: p for p in found})
    if any(_present(p, after) for p in placements_.values()):
        return None, 'target_remains'
    if any(resolve(locator, after) for locator in targets):
        return None, 'target_remains'
    removed = set(placements_)
    removed_by = None
    for fixer in fixers:
        if _criterion(fixer.get('rule_id')) == sc or str(fixer['id']) == str(row['id']):
            continue
        fixer_targets = _targets(fixer)
        covered = {}
        for locator in fixer_targets:
            found = resolve(locator, before) or ()
            covered.update({p.key: p for p in found})
        if removed <= set(covered) and not any(_present(p, after) for p in covered.values()):
            removed_by = fixer
            break
    if removed_by is None:
        return None, 'no_verified_removing_fix'
    removed_ids = {p.docpr_id for p in placements_.values()
                   if p.docpr_id is not None and p.part == 'word/document.xml'}
    for issue in evidence.get('remaining_issues') or []:
        if not isinstance(issue, dict) or _criterion(issue.get('wcag') or issue.get('ruleId')) != sc:
            continue
        location = str(issue.get('location') or '').strip()
        if not location:
            return None, 'unlocated_remaining_issue'
        still = resolve(location, after)
        alias = _ALIAS.match(location)
        if still is None and not alias:
            return None, 'unlocated_remaining_issue'
        if alias and alias.group(1) in removed_ids:
            return None, 'remaining_issue_locates_target'
        if still and any(same_placement(q, p) for q in still for p in placements_.values()):
            return None, 'remaining_issue_locates_target'
    # The exact finding rows: same file and criterion, instance key resolving to a removed
    # placement in the BEFORE bytes, owned by this row or by no row.
    matched = []
    for finding in findings:
        if finding['file'] != file or finding['rule_id'] != sc:
            continue
        located = resolve(finding.get('instance_key'), before, loose_names=True)
        linked = str(finding.get('review_item_id') or '') == str(row['id'])
        if located and {p.key for p in located} <= removed:
            if finding.get('review_item_id') not in (None, '') and not linked:
                continue
            matched.append(finding)
        elif linked:
            return None, 'finding_not_located'
    if not matched:
        return None, 'finding_not_located'
    declared = {fid for p in (row.get('proposals') or []) if isinstance(p, dict)
                for fid in (p.get('baseline_finding_ids') or [])}
    if declared and not declared <= {f['finding_id'] for f in matched}:
        return None, 'finding_identity_mismatch'
    if any(f.get('disposition') not in _OPEN_DISPOSITIONS | {'superseded_by_reassessment'}
           for f in matched):
        return None, 'finding_already_terminal'
    return {'row': row, 'criterion': sc, 'targets': targets, 'removed_by': removed_by,
            'placements': [placements_[k] for k in sorted(placements_)],
            'findings': matched}, None


def reconcile(store, scan_id, file, *, source: bytes, corrected: bytes, evidence: dict,
              source_kind: str, expected_source_sha256: str | None = None,
              actor: str = 'acp') -> dict:
    """Record target removals for `file`. Returns {'superseded': [...], 'skipped': [...]}.

    Skips are returned, never recorded: an unproven removal leaves no trace in the audit log.
    """
    result = {'superseded': [], 'unchanged': [], 'skipped': []}

    def skip(reason, item_id=None):
        result['skipped'].append({'item_id': item_id, 'reason': reason})
        return result

    if not str(file).lower().endswith('.docx'):
        return skip('format_unsupported')
    if not isinstance(source, bytes) or not isinstance(corrected, bytes):
        return skip('bytes_unavailable')
    corrected_sha, source_sha = _sha(corrected), _sha(source)
    reason = _incomplete(evidence, corrected_sha)
    if reason:
        return skip(reason)
    record = store.get_file_record(scan_id, file) or {}
    if record.get('corrected_sha256') != corrected_sha:
        return skip('artifact_not_current')
    batch_id = current_batch(store, scan_id)
    if not batch_id:
        return skip('no_current_batch')
    scope, scope_id = _scope(store, scan_id, file, refresh=True)
    if evidence['assessment'] == SAVED_EVIDENCE and evidence.get('assessment_scope') != scope_id:
        return skip('scope_changed')
    if expected_source_sha256 is not None and expected_source_sha256 != source_sha:
        return skip('source_digest_mismatch')
    if source_kind == 'assessed_source':
        recorded = _recorded_source_digests(store, scan_id, file, batch_id)
        if recorded and source_sha not in recorded:
            return skip('source_digest_mismatch')
    try:
        before, after = placements(source), placements(corrected)
    except ValueError:
        return skip('unreadable_document')
    with store._db.cursor() as cur:
        store._db.execute(cur, "SELECT * FROM hitl_queue WHERE scan_id=%s AND file=%s ORDER BY id",
                          (scan_id, file))
        rows = [store._decode_proposals(r) for r in store._db.fetchall(cur)]
    findings = store.list_finding_dispositions(scan_id, batch_id)
    fixers = [r for r in rows if _verified_fixer(store, r, batch_id, findings)]
    plans = []
    for row in rows:
        if not _eligible(row):
            continue
        plan, why = _plan_row(row, file=file, before=before, after=after, fixers=fixers,
                              findings=findings, evidence=evidence, scope=scope,
                              corrected_sha=corrected_sha)
        if plan:
            plans.append(plan)
        else:
            result['skipped'].append({'item_id': str(row['id']), 'reason': why})
    if not plans:
        return result
    return _commit(store, scan_id, file, plans, result, corrected_sha=corrected_sha,
                   source_sha=source_sha, source_kind=source_kind, batch_id=batch_id,
                   scope_id=scope_id, evidence=evidence, actor=actor)


def begin_write(store):
    """SQLite has no row locks: take the database write lock up front, as retry admission does."""
    if getattr(store._db, 'supports_for_update', False):
        return
    with store._db.cursor() as cur:
        connection = getattr(cur, 'connection', None)
        if connection is not None and not connection.in_transaction:
            store._db.execute(cur, 'BEGIN IMMEDIATE')


def lock_file(store, scan_id, file):
    """The per-file lock (file_records row) every approved writer commits under."""
    suffix = ' FOR UPDATE' if getattr(store._db, 'supports_for_update', False) else ''
    with store._db.cursor() as cur:
        store._db.execute(cur,
            f"SELECT corrected_sha256 FROM file_records WHERE scan_id=%s AND file=%s{suffix}",
            (scan_id, file))
        return (store._db.fetchone(cur) or {}).get('corrected_sha256')


def lock_rows(store, item_ids):
    """Review rows, in id order. ALWAYS before lock_file: the documented order the approved
    writer and retry admission take (review row, then file), so no two paths can deadlock."""
    return {str(i): store._get_hitl_item_for_decision(i) for i in sorted({str(i) for i in item_ids})}


def _commit(store, scan_id, file, plans, result, *, corrected_sha, source_sha, source_kind,
            batch_id, scope_id, evidence, actor):
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    with store.transaction():
        begin_write(store)
        locked_rows = lock_rows(store, [p['row']['id'] for p in plans])
        # Under the lock: a copy replaced since the evidence was read, a batch that moved or a
        # scope that changed records nothing at all.
        changed = (lock_file(store, scan_id, file) != corrected_sha
                   or current_batch(store, scan_id) != batch_id)
        if not changed:
            try:
                changed = _scope(store, scan_id, file, refresh=True)[1] != scope_id
            except Exception:
                changed = True
        if changed:
            result['skipped'].extend({'item_id': str(p['row']['id']), 'reason': 'artifact_changed_before_commit'}
                                     for p in plans)
            return result
        current = {r['finding_id']: r for r in store.list_finding_dispositions(scan_id, batch_id)}
        done = set()
        for plan in plans:
            locked = locked_rows.get(str(plan['row']['id']))
            row = plan['row']
            if (not locked or not _eligible(locked)
                    or int(locked.get('decision_version') or 0) != int(row.get('decision_version') or 0)
                    or _snapshots(locked) != _snapshots(row) or _targets(locked) != _targets(row)):
                result['skipped'].append({'item_id': str(row['id']), 'reason': 'row_changed_before_commit'})
                continue
            if any(f['finding_id'] not in current or current[f['finding_id']].get('disposition')
                   not in _OPEN_DISPOSITIONS | {'superseded_by_reassessment'} for f in plan['findings']):
                result['skipped'].append({'item_id': str(row['id']), 'reason': 'finding_already_terminal'})
                continue
            fixer = plan['removed_by']
            line_id = _line_id(scan_id, file, row, corrected_sha, batch_id, str(fixer['id']))
            finding_ids = sorted(f['finding_id'] for f in plan['findings'])
            with store._db.cursor() as cur:
                store._db.execute(cur, "SELECT id FROM decision_log WHERE id=%s", (line_id,))
                existing = store._db.fetchone(cur)
            if not existing:
                detail = {
                    'item_id': str(row['id']), 'file': file, 'rule_id': row.get('rule_id'),
                    'criterion': plan['criterion'],
                    'decision_version': int(row.get('decision_version') or 0),
                    'proposal_snapshot_ids': _snapshots(row), 'targets': plan['targets'],
                    'removed_by_item_id': str(fixer['id']),
                    'removed_by_rule_id': _criterion(fixer.get('rule_id')),
                    'removed_by_targets': _targets(fixer),
                    'removed_placements': [p.describe() for p in plan['placements']],
                    'finding_ids': finding_ids, 'batch_id': batch_id,
                    'corrected_artifact_sha256': corrected_sha,
                    'source_artifact_sha256': source_sha, 'source_kind': source_kind,
                    'assessment': evidence['assessment'],
                    'assessment_status': evidence.get('assessment_status'),
                    'skipped_rules': evidence.get('skipped_rules'),
                    'assessment_scope': scope_id,
                    'evidence_line_id': evidence.get('evidence_line_id'),
                    'verified_at': evidence.get('verified_at') or now,
                    'reconciled_at': now,
                    'effect': 'finding superseded; row status, approval, document and certification unchanged',
                }
                with store._db.cursor() as cur:
                    store._db.execute(cur,
                        "INSERT INTO decision_log(id,ts,actor,action,scan_id,file,rule_id,detail) "
                        "VALUES(%s,%s,%s,%s,%s,%s,%s,%s)",
                        (line_id, now, actor, ACTION, scan_id, file, row.get('rule_id'),
                         json.dumps(detail, sort_keys=True, separators=(',', ':'))))
                verified_at = detail['verified_at']
            else:
                with store._db.cursor() as cur:
                    store._db.execute(cur, "SELECT detail FROM decision_log WHERE id=%s", (line_id,))
                    verified_at = json.loads(store._db.fetchone(cur)['detail']).get('verified_at')
            moved = 0
            for finding_id in finding_ids:
                state = current[finding_id]
                if state.get('disposition') == 'superseded_by_reassessment' or finding_id in done:
                    continue
                done.add(finding_id)
                revision = int(state.get('revision') or 0)
                store.transition_finding_disposition(
                    scan_id, batch_id, finding_id, 'superseded_by_reassessment',
                    expected_revision=revision,
                    event_id=f'target-removed:{line_id}:{finding_id}:r{revision}',
                    review_item_id=str(row['id']), fix_evidence_ids=[f'target_removed:{line_id}'],
                    verified_at=verified_at)
                moved += 1
            (result['superseded'] if not existing or moved else result['unchanged']).append(
                {'item_id': str(row['id']), 'decision_log_id': line_id, 'finding_ids': finding_ids,
                 'findings_moved': moved, 'recorded': not existing})
    return result


# ── trigger points ─────────────────────────────────────────────────────────────────────────

def _assessed_source(scan_id, file, owner, store):
    from scanner import read_cached_source
    checksum_reader = getattr(store, 'get_source_checksum', None)
    checksum = checksum_reader(scan_id, file) if callable(checksum_reader) else None
    data = read_cached_source(scan_id, file, owner, checksum=checksum)
    if not data and checksum:
        data = read_cached_source(scan_id, file, owner)
    return data or None


def reconcile_after_apply(store, scan_id, file, *, owner, prior: bytes, prior_sha256: str | None,
                          corrected: bytes, verification) -> dict:
    """Trigger (a): an apply job just committed `corrected` with its own complete re-scan."""
    # A new artifact makes every earlier retirement non-current, so rows retired before this
    # job ARE candidates again and are re-affirmed on these bytes; nothing else re-reads storage.
    if not str(file).lower().endswith('.docx') or not candidates(store, scan_id, file)[0]:
        return {'superseded': [], 'unchanged': [], 'skipped': [{'item_id': None, 'reason': 'nothing_to_reconcile'}]}
    from datetime import datetime, timezone
    evidence = evidence_from_verification(verification, verified_at=datetime.now(timezone.utc).isoformat())
    if evidence is not None:
        # The apply lane's re-scan ran under the scan's own frozen scope (scanner
        # _with_assessment_scope); record that identity with the evidence it produced.
        evidence['assessment_scope'] = _scope(store, scan_id, file, refresh=True)[1]
    source = _assessed_source(scan_id, file, owner, store)
    if source:
        return reconcile(store, scan_id, file, source=source, corrected=corrected,
                         evidence=evidence, source_kind='assessed_source')
    return reconcile(store, scan_id, file, source=prior, corrected=corrected, evidence=evidence,
                     source_kind='prior_corrected_copy', expected_source_sha256=prior_sha256)


# Per-process memo of files whose last full check found nothing to retire, keyed by everything
# that check depended on. Bounded; a miss only costs one re-check.
_MEMO_LIMIT = 512
# How many files one call may download and re-check. The rest report 'deferred_bounded' and are
# picked up by the next call; files with nothing new to check never count against it.
MAX_FILES_PER_CALL = 20
# Skips that depend on something outside the fingerprint (storage availability): never memoized.
_TRANSIENT = {'bytes_unavailable'}


def _file_rows(store, scan_id, file):
    with store._db.cursor() as cur:
        store._db.execute(cur, "SELECT * FROM hitl_queue WHERE scan_id=%s AND file=%s ORDER BY id",
                          (scan_id, file))
        return [store._decode_proposals(r) for r in store._db.fetchall(cur)]


def candidates(store, scan_id, file):
    """(rows still worth checking, ids already retired on CURRENT evidence). Metadata only.

    A retired row keeps its audit status ('pending'), so eligibility alone would re-download
    both documents on every poll forever. current_removals already proves those rows retired
    without touching blob storage; only the rest are candidates.
    """
    rows = _file_rows(store, scan_id, file)
    eligible = [r for r in rows if _eligible(r)]
    retired = current_removals(store, eligible)
    return [r for r in eligible if str(r['id']) not in retired], sorted(retired), rows


def _fingerprint(store, scan_id, file, record, evidence, rows):
    """Everything a full check reads, except the bytes themselves (bound by their digests)."""
    batch = current_batch(store, scan_id)
    try:
        scope_id = _scope(store, scan_id, file)[1]
    except Exception:
        return None
    findings = sorted((f['finding_id'], f.get('disposition'), int(f.get('revision') or 0))
                      for f in (store.list_finding_dispositions(scan_id, batch) if batch else [])
                      if f['file'] == file)
    state = [(str(r['id']), r.get('rule_id'), r.get('status'), bool(r.get('applied')),
              bool(r.get('validated')), int(r.get('decision_version') or 0), _snapshots(r),
              _targets(r)) for r in rows]
    material = json.dumps([scan_id, file, record.get('corrected_sha256'), record.get('remediated_at'),
                           batch, scope_id, evidence.get('evidence_line_id'), state, findings],
                          sort_keys=True, default=str, separators=(',', ':'))
    return hashlib.sha256(material.encode()).hexdigest()


def _memo(store):
    from collections import OrderedDict
    memo = store.__dict__.get('_target_reconciliation_memo')
    if memo is None:
        memo = store.__dict__['_target_reconciliation_memo'] = OrderedDict()
    return memo


def reconcile_from_saved_assessment(store, scan_id, file, owner, *, budget=None) -> dict:
    """Triggers (b), (c) and the reconcile-targets route: persisted saved-copy evidence on the
    CURRENT corrected artifact.

    Reads stored bytes only, and only when there is something new to decide: rows already retired
    on current evidence, and files whose last complete check is unchanged in every input, are
    answered from metadata. `budget` ([remaining downloads]) bounds a whole-scan call.
    No model call, no document write, no approval.
    """
    result = {'superseded': [], 'unchanged': [], 'skipped': []}
    if not str(file).lower().endswith('.docx'):
        result['skipped'].append({'item_id': None, 'reason': 'format_unsupported'})
        return result
    # First make the ledger honest about what is NO LONGER proven, whether or not new proof
    # turns up below: a lapsed retirement must never outlive its evidence just because the
    # saved copy has not been re-checked yet.
    reopen_lapsed_locked(store, scan_id, file)
    todo, retired, rows = candidates(store, scan_id, file)
    if not todo:
        result['unchanged'] = [{'item_id': item_id} for item_id in retired]
        result['skipped'].append({'item_id': None, 'reason': 'nothing_to_reconcile'})
        return result
    record = store.get_file_record(scan_id, file) or {}
    evidence = persisted_evidence(store, scan_id, file, owner, record)
    if evidence is None:
        result['skipped'].append({'item_id': None, 'reason': 'verification_missing'})
        return result
    fingerprint = _fingerprint(store, scan_id, file, record, evidence, rows)
    memo = _memo(store)
    if fingerprint and fingerprint in memo:
        memo.move_to_end(fingerprint)
        return json.loads(memo[fingerprint])
    if budget is not None:
        if budget[0] <= 0:
            result['skipped'].append({'item_id': None, 'reason': 'deferred_bounded'})
            return result
        budget[0] -= 1
    import blob
    corrected = blob.download_remediated(owner, scan_id, file)
    source = _assessed_source(scan_id, file, owner, store)
    if not corrected or not source:
        result['skipped'].append({'item_id': None, 'reason': 'bytes_unavailable'})
        return result
    outcome = reconcile(store, scan_id, file, source=source, corrected=corrected, evidence=evidence,
                        source_kind='assessed_source')
    if (fingerprint and not outcome['superseded']
            and not any(s.get('reason') in _TRANSIENT for s in outcome['skipped'])):
        # Nothing was retired, so nothing this check read has changed: the next identical
        # call gets the same answer without reading either document again.
        memo[fingerprint] = json.dumps(outcome)
        while len(memo) > _MEMO_LIMIT:
            memo.popitem(last=False)
    return outcome


def reconcile_scan(store, scan_id, *, max_files=None) -> dict:
    """Every docx holding an eligible row, at most `max_files` (default MAX_FILES_PER_CALL) of
    them read from storage in one call. {file: result}."""
    with store._db.cursor() as cur:
        store._db.execute(cur, "SELECT owner_email FROM scan_runs WHERE id=%s", (scan_id,))
        owner = (store._db.fetchone(cur) or {}).get('owner_email')
        store._db.execute(cur,
            "SELECT DISTINCT file FROM hitl_queue WHERE scan_id=%s AND (status IN ('pending','in_review') "
            "OR (status='approved' AND (applied IS NULL OR applied=0)))", (scan_id,))
        files = sorted(r['file'] for r in store._db.fetchall(cur) if r.get('file'))
    budget = [MAX_FILES_PER_CALL if max_files is None else max_files]
    return {file: reconcile_from_saved_assessment(store, scan_id, file, owner, budget=budget)
            for file in files if str(file).lower().endswith('.docx')}

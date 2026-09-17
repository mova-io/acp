"""Read-only, owner-scoped follow-up reports. Publication is not certification."""
from collections import Counter, defaultdict
from datetime import datetime, timezone
from hashlib import sha256
from html import escape
from urllib.parse import urlsplit
import base64
from pathlib import Path
from functools import lru_cache
import csv
import io
import json
import re


def _text(value):
    return escape(str(value if value is not None else 'Not recorded'), quote=True)


def _rule(value):
    from assessment_selection import catalog
    raw = str(value or '')
    match = re.search(r'(?:SC_)?(\d+)[._](\d+)[._](\d+)', raw)
    return '.'.join(match.groups()) if match else catalog().get(raw, raw)


_OFFICE_LOCATION_TOKEN = r"word:(?:p:[1-9]\d*(?::run:[1-9]\d*)?|table:[1-9]\d*:row:[1-9]\d*|document:outline)"


def _display_note(note):
    """Hide only appended structural metadata; preserve recorded excerpt text."""
    return re.sub(r"\s*\[location:" + _OFFICE_LOCATION_TOKEN + r"\]\s*$", "", str(note or ""))


def _office_location(row):
    """Only recorded structural targets/excerpts; Word layout pages are not inferred."""
    note = str(row.get('note') or '')[:4000]
    token_pattern = _OFFICE_LOCATION_TOKEN
    # The writer appends one target at the end. Earlier wrappers can be
    # literal document text inside the recorded excerpt, not writer metadata.
    target = re.search(r'\[location:(' + token_pattern + r')\]\s*$', note)
    tokens = [target.group(1)] if target else []
    locator = str(row.get('locator') or '')[:200]
    if re.fullmatch(token_pattern, locator):
        tokens.append(locator)
    locations = []
    for token in dict.fromkeys(tokens):
        parts = token.split(':')
        if token == 'word:document:outline':
            value = 'Document heading outline'
        elif parts[1] == 'p':
            value = 'Paragraph ' + parts[2] + ('; Run ' + parts[4] if len(parts) == 5 else '')
        else:
            value = 'Table ' + parts[2] + '; Row ' + parts[4]
        locations.append(value)
    if locations:
        return '; '.join(locations)
    # Older writers retained useful excerpts, but not structural indices. Label
    # them as excerpts rather than implying a numbered paragraph or layout page.
    before = str(row.get('before') or '')[:2000]
    heading = re.match(r'^paragraph “([^”]{1,80})” was body text styled to look like a heading$', before)
    if heading:
        return 'Heading text (recorded excerpt): “' + heading.group(1) + '”'
    run = re.fullmatch(r'text run "([^"\n]{1,80})"', note)
    if run:
        return 'Text run (recorded excerpt): “' + run.group(1) + '”'
    if before == 'first row was ordinary data cells (<w:tr>)':
        return 'First table row; exact table index not recorded'
    if re.fullmatch(r'[1-9]\d* separate Heading 1s competed as the document title', before):
        return 'Document heading outline'
    return None


def _location(row):
    from pdf_release_evidence import evidence_location
    canonical = evidence_location(row)
    if canonical:
        return canonical
    parts = []
    for key, label in [('page', 'Page'), ('page_number', 'Page'), ('pages', 'Pages'),
                       ('slide', 'Slide'), ('slide_number', 'Slide'), ('sheet', 'Sheet'),
                       ('cell', 'Cell'), ('paragraph', 'Paragraph'), ('paragraph_index', 'Paragraph index'),
                       ('element', 'Element'), ('selector', 'Selector'), ('location', '')]:
        value = row.get(key)
        if value is not None and value != '':
            parts.append(f'{label}: {value}' if label else re.sub(r'^Location:\s*', '', str(value), flags=re.I))
    return '; '.join(parts) or _office_location(row) or str(row.get('locator') or 'Not recorded')


def _suggestions(task):
    proposals = task.get('proposals') or []
    if isinstance(proposals, dict):
        proposals = [proposals]
    rendered = []
    for proposal in proposals:
        if isinstance(proposal, dict):
            rendered.append('Before: ' + str(proposal.get('before', 'Not recorded')) +
                            '; Suggested: ' + str(proposal.get('proposed_value') or proposal.get('value') or proposal.get('text') or 'Not recorded'))
        elif isinstance(proposal, str):
            rendered.append(proposal)
    return '; '.join(rendered) or task.get('approved_value') or 'Not recorded'


def _link(url, label):
    try:
        parsed = urlsplit(str(url or ''))
        safe = parsed.scheme == 'https' and bool(parsed.hostname) and not parsed.username
    except ValueError:
        safe = False
    return f'<a href="{_text(url)}">{_text(label)}</a>' if safe else _text(label)


@lru_cache(maxsize=1)
def _logo():
    return base64.b64encode((Path(__file__).parent / 'assets' / 'mova-logo.png').read_bytes()).decode('ascii')


def _page(title, content):
    return ('<!doctype html><html lang="en"><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width, initial-scale=1">'
            f'<title>{_text(title)}</title><style>body{{font:16px/1.5 system-ui;max-width:1280px;margin:2rem auto;padding:1rem;color:#302535;background:#fbf9fc}}'
            'main{background:white;border:1px solid #e4dcea;border-radius:16px;padding:24px}'
            '.brand{display:flex;gap:24px;align-items:center;border-bottom:3px solid #62435d;padding-bottom:16px}.brand img{width:200px;height:auto;max-width:45%}'
            'table{border-collapse:collapse;width:100%;font-size:14px;margin:12px 0}th,td{border-bottom:1px solid #e4dcea;padding:.75rem;text-align:left;vertical-align:top;overflow-wrap:anywhere}'
            'th{background:#f5eff7}caption{text-align:left;font-weight:bold}a{color:#573352}h1{font-size:1.8rem}h2{margin-top:28px}'
            'details{border:1px solid #e4dcea;border-radius:8px;padding:10px;margin:8px 0}summary{cursor:pointer;font-weight:600}'
            '.report-card{overflow-wrap:anywhere;border-left:3px solid #854f0b;padding:8px 12px;margin:12px 0;break-inside:avoid}'
            '.table-scroll{overflow-x:auto}small{color:#655b6a}.notice{padding:12px;background:#f5eff7;border-radius:8px}'
            '@media(max-width:700px){main{padding:12px}table{min-width:650px}.brand{flex-wrap:wrap}}'
            '@media print{body{background:white;margin:0}main{border:0}.brand img{width:150px}details{break-inside:avoid}table{font-size:10px}}</style>'
            f'<main><header class="brand"><img src="data:image/png;base64,{_logo()}" alt="Mova iO"><div>Accessibility Compliance Platform<br><strong>Scan and remediation report</strong></div></header>'
            f'<h1>{_text(title)}</h1><p class="notice">Published copies may have remaining accessibility issues. '
            'This report does not certify full accessibility compliance.</p>'
            f'{content}</main></html>').encode('utf-8')


CATEGORIES = {
    'automatic': 'Fully automated', 'approval': 'Fix available — approval needed',
    'suggestion': 'AI suggestion needed', 'manual': 'Manual fix required',
    'unsupported': 'Cannot fix with ACP', 'blocked': 'Blocked',
    'applied': 'Applied — verification pending', 'verified': 'Fixed and verified',
}


def _category(trace=None, task=None, verified=False):
    trace, task = trace or {}, task or {}
    if verified:
        return 'verified'
    if task.get('applied'):
        return 'applied'
    if task.get('status') == 'blocked' or str(trace.get('outcome') or '').upper() in ('ERROR', 'NOT_EVALUATED', 'UNSUPPORTED'):
        return 'blocked'
    if task.get('status') in ('rejected', 'deferred'):
        return 'manual'
    if task.get('proposals') or task.get('approved_value'):
        return 'approval'
    mode = trace.get('fix_mode')
    if mode == 'auto':
        return 'automatic'
    if mode in ('ai-assisted', 'assisted'):
        return 'suggestion'
    if mode in ('human', 'manual', 'human-only') or trace.get('outcome') == 'REVIEW':
        return 'manual'
    if trace.get('remediation_supported') is False or mode in ('unsupported', 'none'):
        return 'unsupported'
    return 'blocked'


def _table(headers, rows):
    return '<div class="table-scroll"><table><thead><tr>' + ''.join(f'<th scope="col">{_text(h)}</th>' for h in headers) + '</tr></thead><tbody>' + ''.join('<tr>' + ''.join(f'<td>{c}</td>' for c in row) + '</tr>' for row in rows) + '</tbody></table></div>'


FOLLOW_UP_NOTICE = ('Ticking this printed copy does not record anything in ACP. Reassess the saved copy '
                    'after editing; a saved edit alone does not establish conformance.')


def _field(label, value_html, cls=''):
    return f'<p class="report-field{cls}"><strong>{_text(label)}:</strong> {value_html}</p>'


def _finding_card(criterion, row=None, category=None, guide=None):
    """ONE card per remaining finding: the checklist facts, the specific offline instructions,
    how to verify, and the printed reviewer response, together.

    The report used to print every finding twice — a checklist card, then the same finding again
    in a separate "offline guide" card — so two findings took three pages and a reviewer had to
    reconcile the two copies by hand. Nothing is dropped here: every checklist column and every
    guide field is still printed, once.
    """
    from wcag_codeset import _name_for
    name = _name_for(criterion) if criterion else ''
    title = f'SC {_text(criterion)}' + (f' — {_text(name)}' if name and name != criterion
                                        else f' — {_text(guide["title"])}' if guide and guide.get('title') else '')
    issue = row[1] if row else (guide or {}).get('description')
    location = row[2] if row else (guide or {}).get('location')
    severity = row[3] if row else (guide or {}).get('priority')
    owner = row[5] if row else 'Unassigned'
    status = row[6] if row else (guide or {}).get('status')
    parts = [f'<section class="report-card finding-card" data-criterion="{_text(criterion)}">',
             f'<h3>{title}</h3>',
             '<p class="report-field facts">'
             f'<strong>Severity:</strong> {_text(severity)} · '
             f'<strong>Status:</strong> {_text(status)} · '
             f'<strong>Owner:</strong> {_text(owner)}'
             + (f' · <strong>Remediation category:</strong> {_text(CATEGORIES[category])}' if category else '')
             + '</p>',
             _field('Location', _text(location)),
             _field('Issue', _text(issue or 'Accessibility issue remains'))]
    words = lambda value: set(re.findall(r'\w+', str(value or '').lower()))  # noqa: E731
    if guide and row and guide.get('location') and not guide['location'].startswith('Location not recorded') \
            and not words(guide['location']) <= words(location):
        # Only when the guide's target adds something ("pdf:figure:first" beside "Not recorded").
        parts.append(_field('Recorded target', _text(guide['location'])))
    if guide and guide.get('original_value') is not None:
        parts.append(_field('Original recorded value', _text(guide['original_value']), ' evidence'))
    if guide and guide.get('proposed_value') is not None:
        parts.append(_field('AI recommended value — not recorded as saved', _text(guide['proposed_value']), ' evidence'))
    if guide and guide.get('reason'):
        parts.append(_field('Recorded rationale', _text(guide['reason'])))
    action = row[4] if row else (guide or {}).get('recommendation')
    if action:
        parts.append(_field('Recommended action', _text(action)))
    if guide and guide.get('editor_steps'):
        parts.append('<p class="report-field"><strong>How to fix:</strong></p><ol>'
                     + ''.join(f'<li>{_text(step)}</li>' for step in guide['editor_steps']) + '</ol>')
    verification = (f'{_text(guide["technical_status"])} · {_text(guide["human_status"])}' if guide
                    else 'Technical verification not recorded · Human confirmation of meaning not recorded')
    parts.append(_field('Verification', verification))
    parts.append('<div class="check-off"><p><span class="print-box" aria-hidden="true">☐</span> '
                 'Remediated and rechecked</p>'
                 '<p class="notes">Reviewer / date / notes:</p>'
                 f'<p class="print-notice">{_text(FOLLOW_UP_NOTICE)}</p></div></section>')
    return ''.join(parts)


def _remaining_actions(categorized, document):
    """Join checklist rows to the guide's per-target rows, then render one card each.

    Rows are matched on criterion and recorded issue text, consuming guide rows in order, so two
    findings with identical text at different targets each keep their own instructions. A guide
    row with no checklist counterpart (an unlinked recommendation, or a finding whose criterion is
    still processing) is still printed, as its own card.
    """
    guide_rows = list(document['remaining'])
    used = set()
    cards = []
    for category, row in categorized:
        match = None
        for index, guide in enumerate(guide_rows):
            if index in used or guide.get('criterion') != row[0] or guide.get('kind') != 'finding':
                continue
            if (guide.get('description') or '') == (row[1] or ''):
                match = index
                break
        if match is not None:
            used.add(match)
        cards.append(_finding_card(row[0], row, category, guide_rows[match] if match is not None else None))
    for index, guide in enumerate(guide_rows):
        if index not in used:
            cards.append(_finding_card(guide.get('criterion'), None, None, guide))
    if not cards:
        return ''
    return ('<h2>Remaining actions · Offline remediation guide</h2><p>In-app review is optional. '
            'Work through the remaining actions in the saved document when convenient. Each card below '
            'is one recorded item: what was found, where, how to fix it, how to check it, and a place to '
            'record your follow-up. AI recommendations require checking the target and meaning before use.</p>'
            + ''.join(cards))


def build_release_report_sources(store, scan_id, owner, release_id):
    """Return separate checklist/change HTML assets per release document and a CSV.

    Original totals use sealed assessment evidence only. Verification credit requires
    ledger identity, timestamp AND a matching durable before/after evidence record.
    Current findings, review tasks and changes are intentionally separate units.
    """
    scan = store.get_scan(scan_id, owner=owner)
    release = store.release_status(release_id, owner)
    if not scan or not release or release.get('scan_id') != scan_id:
        raise KeyError('Release not found')
    scope = store.get_scan_scope(scan_id)
    @lru_cache(maxsize=None)
    def selected_codes(name):
        from assessment_selection import selected_for_file
        return selected_for_file(store.scope_for_file(scan_id, name, scope), name)
    def selected(row):
        codes = selected_codes(row['file'])
        rid = _rule(row.get('wcag') or row.get('rule_id') or row.get('ruleId'))
        return codes is None or rid in codes
    stages = store.canonical_stage_lineage(scan_id, owner=owner).get('stages', [])
    remediation = next((s for s in stages if s.get('stage') == 'remediate'), {})
    assessment = next((s for s in stages if s.get('stage') == 'assess'), {})
    # Follow remediation's own assessment lineage, never a later reassessment.
    audit = None
    if remediation.get('input_manifest_id'):
        manifest = store.get_stage_output_manifest(remediation['input_manifest_id'], owner=owner) or {}
        for entry in manifest.get('entries', []):
            if isinstance(entry.get('assessment_summary'), dict):
                audit = store._validated_assessment_audit(entry['assessment_summary'])
                break
    elif assessment:
        audit = store.assessment_audit_summary(assessment)
    original = audit.get('findings_recorded') if audit and audit.get('valid') is not False else None
    diffs = store.remediation_diff_page(scan_id, limit=100000)
    ledger = store.list_finding_dispositions(scan_id, remediation['execution_id']) if remediation.get('execution_id') else []
    groups = audit.get('finding_groups') if audit and audit.get('valid') is not False else None
    original_groups = Counter()
    for group in groups or []:
        if not selected(group):
            continue
        original_groups[(group['file'], _rule(group['rule_id']))] += int(group.get('finding_count') or 0)
    if groups is not None:
        original = sum(original_groups.values())
    ledger = [r for r in ledger if selected(r)]
    diffs['items'] = [d for d in diffs['items'] if selected(d)]
    ledger_groups = Counter((r['file'], _rule(r['rule_id'])) for r in ledger)
    ledger_exact = (original is not None and isinstance(groups, list) and sum(original_groups.values()) == original
                    and original_groups == ledger_groups and len(ledger) == original
                    and len({r['finding_id'] for r in ledger}) == original)
    evidence_by_key = defaultdict(set)
    for d in diffs['items']:
        evidence_by_key[(d['file'], _rule(d['rule_id']))].add(f"remediation_diff:{d['file']}:{d['rule_id']}:{d['seq']}")
    def valid_evidence(row):
        allowed = evidence_by_key[(row['file'], _rule(row['rule_id']))]
        return bool(row.get('fix_evidence_ids')) and all(i in allowed for i in row['fix_evidence_ids'])
    verified = [r for r in ledger if r.get('disposition') == 'resolved_verified' and r.get('verified_at') and valid_evidence(r)]
    resolved_groups = Counter((r['file'], _rule(r['rule_id'])) for r in verified)
    fully_resolved = {key for key, count in original_groups.items() if count and resolved_groups[key] == count} if ledger_exact and diffs['complete'] else set()
    verified_total = len(verified) if ledger_exact and diffs['complete'] else None
    traces = store.get_scan_traces(scan_id)
    # Unknown outcomes are explicitly unfinished, never inferred as passes.
    traces = [r for r in traces if selected(r)]
    failed = [r for r in traces if r.get('outcome') == 'FAIL']
    unfinished = [r for r in traces if selected(r) and str(r.get('outcome') or '').upper() not in ('PASS', 'FAIL', 'REVIEW', 'NA', 'NOT_APPLICABLE')]
    queue = [q for q in store.list_hitl_queue(scan_id=scan_id, owner=owner) if selected(q)]
    verified_keys = {(r['file'], _rule(r['rule_id'])) for r in diffs['items']}
    open_queue = [r for r in queue if r.get('status') != 'not_applicable' and not (r.get('status') in ('approved', 'resolved') and r.get('applied') and (r['file'], _rule(r['rule_id'])) in verified_keys)]
    unverified = [r for r in queue if r.get('applied') and (r['file'], _rule(r['rule_id'])) not in verified_keys]
    machine_processing = {(q['file'], _rule(q['rule_id'])) for q in queue if q.get('status') in ('queued', 'processing', 'applying', 'verifying')}
    outcomes = {r['file']: r for r in release['documents']}
    files = {f['file']: f for f in scan['files']}
    names = sorted(outcomes)
    rows = []
    assets = []
    index = []
    appendices = []
    candidate_assessments = []
    for name in names:
        file = files.get(name, {})
        outcome = outcomes.get(name, {})
        from release_candidate_assessment import saved_assessment
        candidate = saved_assessment(store, scan_id, owner, name,
            outcome.get('artifact_digest'), release_id=release_id)
        if candidate:
            candidate_assessments.append({'file': name, **candidate})
        status = outcome.get('status', 'not attempted')
        url = outcome.get('released_document_url') if status == 'published' else None
        checklist = []
        # Fresh saved-copy findings are a separate artifact assessment, never a
        # replacement of immutable source finding accounting or verification counts.
        current_issues = candidate.get('remaining_issues') if candidate else None
        issues = [i for i in (current_issues if isinstance(current_issues, list)
                             else file.get('issues') or []) if selected(dict(i, file=name))]
        for issue in issues:
            rid = _rule(issue.get('wcag') or issue.get('rule_id') or issue.get('ruleId'))
            if not candidate and ((name, rid) in fully_resolved or (name, rid) in machine_processing):
                continue
            task = next((q for q in open_queue if q['file'] == name and _rule(q['rule_id']) == rid), {})
            checklist.append([rid, issue.get('detail') or 'Accessibility issue remains', _location(issue), issue.get('severity') or 'Unclassified', issue.get('recommended_action') or issue.get('remediation') or task.get('instruction') or task.get('description') or 'Review and correct this issue in the source document; reassess when convenient.', task.get('assignee') or 'Unassigned', 'Remaining issue'])
        for trace in [t for t in failed if not (candidate and candidate.get('assessment_ok')) and t['file'] == name and (name, _rule(t['rule_id'])) not in fully_resolved and (name, _rule(t['rule_id'])) not in machine_processing]:
            rid = _rule(trace['rule_id'])
            if not any(r[0] == rid for r in checklist):
                checklist.append([rid, f"{trace.get('finding_count', 0)} recorded findings: {trace.get('plain_name') or trace.get('rule_name') or rid}", 'Not recorded', 'Unclassified', 'Review and correct this issue in the source document.', 'Unassigned', 'Remaining issue'])
        for task in [q for q in open_queue if q['file'] == name and q.get('status') not in ('queued', 'processing', 'applying', 'verifying')]:
            rid = _rule(task['rule_id'])
            if not any(r[0] == rid for r in checklist):
                checklist.append([rid, task.get('title') or task.get('rule_name') or 'Follow-up review task', _location(task), task.get('severity') or 'Unclassified', task.get('instruction') or 'Inspect the saved copy when convenient; this task does not block publication.', task.get('assignee') or 'Unassigned', 'Applied, verification not recorded' if task.get('applied') else task.get('status') or 'Pending'])
        for trace in [t for t in traces if not (candidate and candidate.get('assessment_ok')) and t['file'] == name and t.get('outcome') == 'REVIEW' and selected(t) and t.get('fix_mode') not in ('auto', 'ai-assisted', 'assisted') and (name, _rule(t['rule_id'])) not in fully_resolved and (name, _rule(t['rule_id'])) not in machine_processing]:
            rid = _rule(trace['rule_id'])
            if not any(r[0] == rid for r in checklist):
                checklist.append([rid, trace.get('plain_name') or trace.get('rule_name') or 'Review recommended', 'Not recorded', 'Unclassified', 'Check the meaning or usability of the saved result when convenient.', 'Unassigned', 'Review recommended; not a verified pass'])
        for check in [t for t in unfinished if t['file'] == name and (name, _rule(t['rule_id'])) not in fully_resolved and (name, _rule(t['rule_id'])) not in machine_processing]:
            checklist.append([_rule(check['rule_id']), check.get('plain_name') or check.get('rule_name') or 'Check not completed', 'Not recorded', 'Unknown', 'Check manually or rerun with supported analysis.', 'Unassigned', f"Check not completed: {check.get('outcome') or 'unknown'}"])
        slug = re.sub(r'[^A-Za-z0-9._-]+', '-', name)[:65].strip('.-') or 'document'
        report_name = f'checklist-{slug}-{sha256(name.encode()).hexdigest()[:10]}.html'
        detail = f'<p>Publication: {_text(status)} · {_link(url, "Open published file") if url else "Not published"}</p>'
        detail += ('<section class="notice"><h2>Document version for follow-up</h2>'
                   f'<p>Scan: {_text(scan_id)} · Release: {_text(release_id)}<br>'
                   f'Recorded released artifact identity: {_text(outcome.get("artifact_digest"))}</p>'
                   '<p>Use the published corrected copy for follow-up. Recommendations below are guidance, '
                   'not additional edits saved to that copy. If you edit the file externally, reassess '
                   'the new version; this report describes the recorded version.</p></section>')
        if candidate:
            detail += ('<h2>Saved-copy assessment before publication</h2>'
                f'<p>Exact corrected SHA-256: {_text(candidate.get("artifact_sha256"))}<br>'
                f'Assessment status: {_text(candidate.get("assessment_status"))} · '
                f'Completed selected checks: {_text(candidate.get("assessment_ok"))}<br>'
                f'Remaining recorded findings: {_text(len(current_issues) if isinstance(current_issues, list) else None)}</p>')
            if not candidate.get('assessment_ok'):
                detail += '<p class="notice">Some checks could not be completed. Missing findings do not establish a pass. ' + _text(candidate.get('reason')) + '</p>'
            detail += '<p>This assessment covers the run’s selected criteria; it does not establish full WCAG compliance or an Office/PDF checker pass.</p>'
        if status != 'published' and outcome.get('explanation'):
            detail += f'<p>Release explanation: {_text(outcome["explanation"])}</p>'
        original_file = sum(n for (f, _), n in original_groups.items() if f == name) if groups is not None else None
        verified_file = sum(r['file'] == name for r in verified) if ledger_exact and diffs['complete'] else None
        remaining_file = original_file - verified_file if original_file is not None and verified_file is not None else None
        detail += (f'<p><strong>Assessed findings:</strong> {_text(original_file)} · '
                   f'<strong>Verified fixed:</strong> {_text(verified_file)} · '
                   f'<strong>Not yet verified fixed:</strong> {_text(remaining_file)}</p>')
        if candidate and not candidate.get('assessment_ok'):
            detail += '<p class="notice">These are last-known recorded actions, not a complete fresh assessment of this copy. Current remaining findings are unknown.</p>'
        categorized = []
        for row in checklist:
            trace = next((t for t in traces if t['file'] == name and _rule(t['rule_id']) == row[0]), {})
            task = next((q for q in open_queue if q['file'] == name and _rule(q['rule_id']) == row[0]), {})
            category = _category(trace, task)
            categorized.append((category, row))
        from remediation_audit_guide import build_remediation_audit_guide
        # Unlike legacy category accounting, the offline guide preserves each target.
        # A processing task for one image must not hide another finding under its SC.
        guide_issues = [i for i in issues if candidate or (name, _rule(i.get('wcag') or i.get('rule_id') or i.get('ruleId'))) not in fully_resolved]
        guide_tasks = [q for q in open_queue if q['file'] == name and (name, _rule(q['rule_id'])) not in fully_resolved
                       and not q.get('applied')]
        guide = build_remediation_audit_guide(
            [{'file': name, 'issues': guide_issues, 'artifact_digest': outcome.get('artifact_digest')}],
            facts={'audit_review_tasks': guide_tasks})[0]
        actions = _remaining_actions(categorized, guide)
        if actions:
            detail += actions
        elif candidate and not candidate.get('assessment_ok'):
            detail += '<h2>Remaining actions</h2><p>Current remaining findings are unknown because the saved-copy assessment could not be completed.</p>'
        else:
            detail += '<h2>Remaining actions</h2><p>No remaining issues are recorded in the available evidence. This is not a guarantee of compliance.</p>'
        checklist_detail = detail
        from wcag_codeset import _name_for
        detail = f'<p>Document: {_text(name)}<br>Publication: {_text(status)}</p>'
        detail += '<section class="notice"><h2>Released file receipt</h2><p>' + (_link(url, 'Open published corrected file') if url else 'No published file link recorded') + '<br>Recorded artifact identity: ' + _text(outcome.get('artifact_digest')) + '</p><p>Applied changes and verified findings are separate. Pending semantic review remains listed in the checklist. Recorded values may be shortened by evidence storage limits; inspect the corrected file for the complete content.</p></section>'
        detail += '<p>Change records document applied edits; they are not additional findings. Before and after values also describe changes that are not visible on a page. Optional PDF images follow when the original and exact released copy are available.</p>'
        file_traces = [t for t in traces if t['file'] == name and selected(t)]
        file_codes = selected_codes(name)
        if file_codes is not None:
            recorded = {_rule(t['rule_id']) for t in file_traces}
            for criterion in file_codes:
                missing = {'file': name, 'rule_id': criterion, 'outcome': 'Not recorded', 'finding_count': None}
                if _rule(criterion) not in recorded and selected(missing):
                    file_traces.append(missing)
        detail += '<h2>Success criteria coverage</h2><p>All recorded selected criteria for this file. An incomplete or missing check is not a pass.</p>'
        detail += _table(['Success criterion', 'Name / level', 'Recorded outcome', 'Recorded findings'],
                         [[_text(_rule(t['rule_id'])), _text(_name_for(_rule(t['rule_id']))) + ' / ' + _text(t.get('level')),
                           _text(t.get('outcome')), _text(t.get('finding_count'))] for t in file_traces]) if file_traces else '<p>Criterion-level coverage was not recorded.</p>'
        changes = [d for d in diffs['items'] if d['file'] == name]
        from unverified_changes import saved_changes
        saved_unverified = [d for d in saved_changes(store, scan_id, name) if selected(d)]
        from pdf_release_evidence import build_visual_evidence
        visual_evidence = build_visual_evidence(store, scan_id, owner, name, outcome, changes + saved_unverified)
        from word_release_evidence import build_word_evidence
        word_evidence, word_assets = build_word_evidence(store, scan_id, owner, name, outcome)
        assets.extend(word_assets)
        detail += word_evidence
        evidence_targets = set(re.findall(r'id="(pdf-evidence-page-\d+)"', visual_evidence))
        detail += f'<p><strong>Recorded edit outcomes:</strong> {len(changes)} verified-process change records · {len(saved_unverified)} applied AI change records pending verification. These are change counts, not finding counts.</p>'
        detail += '<h2>Recorded changes by success criterion</h2><p>Change records are separate from findings. Verified finding totals in the checklist require matching ledger evidence.</p>'
        from pdf_release_evidence import evidence_links
        def change_location(d):
            links = evidence_links(d) if name.lower().endswith('.pdf') else ''
            links = ' '.join(link for link in re.findall(r'<a\b[^>]*>.*?</a>', links)
                             if any(f'href="#{target}"' in link for target in evidence_targets))
            return _text(_location(d)) + ('<br>' + links if links else '')
        for sc in sorted({_rule(d['rule_id']) for d in changes}):
            records = [d for d in changes if _rule(d['rule_id']) == sc]
            detail += f'<details open><summary>SC {_text(sc)} - {_text(_name_for(sc))} · {len(records)} change record{"s" if len(records) != 1 else ""}</summary>'
            detail += _table(['Location', 'Before', 'After', 'Verification evidence'], [[change_location(d), _text(d.get('before')), _text(d.get('after')), _text(_display_note(d.get('note')) or 'Recorded by the verified-change process; finding credit requires matching ledger evidence.')] for d in records]) + '</details>'
        if saved_unverified:
            detail += '<h2>Applied AI changes - not verified</h2><p>These edits were saved to the current processed copy. They do not count as verified fixes; remaining human actions are listed in the checklist.</p>'
            for sc in sorted({_rule(d['rule_id']) for d in saved_unverified}):
                records = [d for d in saved_unverified if _rule(d['rule_id']) == sc]
                detail += f'<details open><summary>SC {_text(sc)} - {_text(_name_for(sc))}</summary>'
                detail += _table(['Location', 'Before', 'After', 'Verification'], [
                    [change_location(d), _text(d.get('before')), _text(d.get('after')),
                     _text('Not verified. ' + str(d.get('reason') or 'Verification evidence is unavailable.'))]
                    for d in records]) + '</details>'
        if not changes and not saved_unverified:
            detail += '<p>No change records are available for this file.</p>'
        detail += visual_evidence
        appendices.append(f'<section class="document-appendix"><h2>Document: {_text(name)}</h2>{checklist_detail}</section>')
        assets.append({'name': report_name.replace('checklist-', 'changes-', 1), 'content': _page(f'Change record — {name}', detail), 'content_type': 'text/html; charset=utf-8', 'report_kind': 'changes', 'file': name, 'artifact_digest': outcome.get('artifact_digest')})
        assets.append({'name': report_name, 'content': _page(f'Follow-up checklist — {name}', checklist_detail), 'content_type': 'text/html; charset=utf-8', 'report_kind': 'checklist', 'file': name, 'artifact_digest': outcome.get('artifact_digest')})
        category_groups = ''
        for key, label in CATEGORIES.items():
            matches = [r for category, r in categorized if category == key]
            if matches:
                category_groups += f'<details><summary>{_text(label)} · {len(matches)} checklist entries</summary><ul>' + ''.join(f'<li>SC {_text(r[0])} — {_text(r[1])}</li>' for r in matches) + '</ul></details>'
        if verified_file:
            category_groups += f'<p>{_text(CATEGORIES["verified"])} · {verified_file} findings</p>'
        index.append([_text(name), _text(name.rsplit('.', 1)[-1].upper()), _text(status), _text(original_file), _text(verified_file),
                      category_groups or 'No classified checklist entries', _text(sum(t['file'] == name for t in unfinished) if traces else None),
                      _link(url, 'Open published file') if url else 'Not published', f'<a href="{report_name}">Open checklist</a>'])
        rows.extend([[name, url or '', r[0], str(r[1]) + ' · Severity: ' + str(r[3]), r[2], CATEGORIES[key], *r[4:]] for key, r in categorized])
    remaining = sum(int(r.get('finding_count') or 0) for r in failed) if traces else None
    metrics = [('Files published in this release', release['published']), ('Publication failures', release['failed']), ('Not yet published', release['remaining']), ('Original assessment findings (immutable, whole scan)', original), ('Original findings fixed and verified', verified_total), ('Original findings not yet verified fixed', original - verified_total if original is not None and verified_total is not None else None), ('Current recorded remaining findings (whole scan)', remaining), ('Verified change records (not findings)', len(diffs['items']) if diffs['complete'] else None), ('Applied review records without matching verification evidence (not findings)', len(unverified)), ('Checks not completed (current recorded traces)', len(unfinished) if traces else None)]
    summary = f'<p>Scan: {_text(scan_id)} · Release: {_text(release_id)} · Generated: {_text(datetime.now(timezone.utc).isoformat())}</p>'
    summary += '<p>Original and current counts describe different points in time. Review tasks and change records are not added to finding totals. Not recorded means evidence is unavailable, not zero.</p>'
    summary += _table(['Measure', 'Count'], [[_text(k), _text(v)] for k, v in metrics])
    if candidate_assessments:
        summary += '<h2>Saved-copy assessments before publication</h2>' + _table(
            ['File', 'Exact artifact SHA-256', 'Assessment status', 'Remaining findings'],
            [[_text(r['file']), _text(r['artifact_sha256']), _text(r['assessment_status']),
              _text(len(r['remaining_issues']) if isinstance(r.get('remaining_issues'), list) else None)]
             for r in candidate_assessments])
        assets.append({'name': 'saved-copy-assessments.json',
            'content': json.dumps({'scan_id': scan_id, 'release_id': release_id, 'documents': candidate_assessments}).encode('utf-8'),
            'content_type': 'application/json'})
    summary += '<h2>Documents and follow-up checklists</h2><p>Remediation categories describe recorded capability or state; future automatic fixes still require an accepted plan. Checklist entries, findings and change records use separate counts. Expand a category to see SCs by file.</p>' + _table(['Document', 'File type', 'Publication status', 'Original findings', 'Fixed and verified', 'Remediation category / SC', 'Incomplete checks', 'Published file', 'Follow-up'], index)
    summary += '<h2>Detailed printable checklists by document</h2>' + ''.join(appendices)
    assets.insert(0, {'name': 'scan-summary.html', 'content': _page('Remediation and publication summary', summary), 'content_type': 'text/html; charset=utf-8', 'report_kind': 'scan_summary'})
    stream = io.StringIO(newline='')
    writer = csv.writer(stream)
    writer.writerow(['File', 'Published URL', 'Criterion', 'Issue', 'Location', 'Remediation category', 'Recommended action', 'Owner', 'Status'])
    for row in rows:
        # Spreadsheet readers must not interpret user-controlled content as formulas.
        writer.writerow(["'" + str(v) if str(v).lstrip().startswith(('=', '+', '-', '@')) else str(v) for v in row])
    assets.append({'name': 'remaining-issues.csv', 'content': stream.getvalue().encode('utf-8-sig'), 'content_type': 'text/csv; charset=utf-8'})
    return assets


def build_release_reports(store, scan_id, owner, release_id):
    """Render printable reports and retain native Word comparison evidence."""
    from release_report_pdf import render_report_pdf
    assets = []
    for asset in build_release_report_sources(store, scan_id, owner, release_id):
        if asset['content_type'].startswith('text/html'):
            assets.append({**asset, 'name': asset['name'].removesuffix('.html') + '.pdf',
                           'content': render_report_pdf(asset['content']), 'content_type': 'application/pdf'})
        elif asset.get('report_kind') in {'tracked_changes', 'tracked_changes_evidence'}:
            assets.append(asset)
    return assets

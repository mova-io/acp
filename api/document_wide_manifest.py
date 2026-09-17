"""Bound document context to saved assessment identities, never invented ordinals."""
from dataclasses import replace
import hashlib
import io
import json
import re
import zipfile

from experiments.document_wide_ai.contracts.v1 import Evidence, EvidenceKind, ExtractionIssue
from experiments.document_wide_ai.packaging.manifest_builder import build_docx_manifest, build_pdf_manifest
from experiments.document_wide_ai.packaging.limits import ExtractionLimits

LIMITS = ExtractionLimits(max_text_chars=60000, max_pages=100, max_images=8, max_findings=20)


def criterion(value):
    match = re.fullmatch(r'(?:SC_)?([1-4])[._]([0-9]+)[._]([0-9]+)(?:[ \t]+[^\r\n]+)?', str(value or ''))
    return '.'.join(match.groups()) if match else None


def assessed_locations(issues, sc, count):
    """Use detector locations only when the entire counted group is represented uniquely."""
    rows = [i for i in issues if criterion(i.get('wcag')) == sc]
    locations = [str(i.get('location') or '').strip() for i in rows]
    if len(rows) != count or not all(locations) or len({x.casefold() for x in locations}) != count:
        return None
    return sorted(locations, key=str.casefold)


def _docx_targets(data):
    from lxml import etree
    ns = {'w': 'http://schemas.openxmlformats.org/wordprocessingml/2006/main',
          'wp': 'http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing',
          'a': 'http://schemas.openxmlformats.org/drawingml/2006/main',
          'r': 'http://schemas.openxmlformats.org/officeDocument/2006/relationships'}
    from experiments.document_wide_ai.packaging.docx_packager import unnamed_docx_images
    result = {}
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        unnamed = {i.locator for i in unnamed_docx_images({n: z.read(n) for n in z.namelist()})}
        for part in z.namelist():
            if not re.fullmatch(r'word/(?:document|header\d*|footer\d*|footnotes|endnotes)\.xml', part):
                continue
            root = etree.fromstring(z.read(part), etree.XMLParser(resolve_entities=False, no_network=True))
            for index, paragraph in enumerate(root.findall('.//w:p', ns)):
                for drawing in paragraph.findall('.//w:drawing', ns):
                    for props in drawing.findall('.//wp:docPr', ns):
                        name = props.get('name')
                        blips = drawing.findall('.//a:blip', ns)
                        rid = blips[0].get('{'+ns['r']+'}embed') if len(blips) == 1 else None
                        if not name:
                            if not rid or part+'#'+rid not in unnamed:
                                continue
                            name = rid
                        key = part + '#' + name
                        aliases = {key.casefold()}
                        if part == 'word/document.xml':
                            aliases.add(f"docx:drawing:{props.get('id')}:paragraph:{index}".casefold())
                        # Repeated names are ambiguous to the production writer too.
                        if key in result:
                            result[key] = (set(), None)
                        else:
                            result[key] = (aliases, part+'#'+rid if rid else None)
    return result


def _bounded_source(data, filename):
    if not isinstance(data, bytes) or len(data) > 20 * 1024 * 1024:
        raise ValueError('document_too_large')
    if filename.lower().endswith(('.docx', '.pptx', '.xlsx')):
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            if len(z.infolist()) > 4000 or sum(i.file_size for i in z.infolist()) > 80 * 1024 * 1024:
                raise ValueError('document_too_large')


def build_manifest(store, scan_id, filename, data):
    from llm_waterfall_provider import managed_context
    from assessment_selection import selected_for_file
    ctx = managed_context()
    if ctx is None or ctx.scan_id != scan_id or ctx.file != filename:
        raise ValueError('document_context_missing')
    input_mode = (getattr(ctx, 'policy', None) or {}).get('document_wide_input_mode', 'extracted')
    if input_mode not in {'extracted', 'native_pdf'}:
        raise ValueError('document_input_mode_unsupported')
    if input_mode == 'native_pdf' and not filename.lower().endswith('.pdf'):
        input_mode = 'extracted'
    if input_mode == 'native_pdf':
        from document_wide_native_pdf import validate_native_pdf
        validate_native_pdf(data)
    record = store.get_file_record(scan_id, filename) or {}
    actual = hashlib.sha256(data).hexdigest()
    if record.get('corrected_sha256') != actual:
        raise ValueError('document_source_changed')
    scope = store.scope_for_file(scan_id, filename, store.get_scan_scope(scan_id))
    selected = selected_for_file(scope, filename)
    if selected is None:
        raise ValueError('document_selected_criteria_missing')
    with store._db.cursor() as cur:
        store._db.execute(cur, 'SELECT snapshot_id,baseline_json FROM remediation_contribution_runs WHERE owner_id=%s AND scan_id=%s AND run_id=%s',
                          (ctx.owner_id, scan_id, ctx.run_id))
        baseline = store._db.fetchone(cur)
    if not baseline:
        raise ValueError('document_assessment_lineage_missing')
    rows = [r for r in json.loads(baseline['baseline_json']) if r['file'] == filename and r['rule_id'] in selected]
    dispositions = {r['finding_id']: r.get('disposition') for r in store.list_finding_dispositions(scan_id, ctx.run_id)}
    rows = [r for r in rows if dispositions.get(r['finding_id']) not in {'resolved_verified', 'excluded_by_policy', 'superseded_by_reassessment'}]
    _bounded_source(data, filename)
    from document_wide_office import build_office_manifest, targets as office_targets
    builder = build_docx_manifest if filename.lower().endswith('.docx') else build_pdf_manifest if filename.lower().endswith('.pdf') else build_office_manifest if filename.lower().endswith(('.pptx', '.xlsx')) else None
    if builder is None:
        raise ValueError('document_format_unsupported')
    try:
        packaged = builder(data, document_id=filename, assessment_revision=baseline['snapshot_id'],
                           selected_criteria=tuple(sorted(selected)), limits=LIMITS)
    except zipfile.BadZipFile as exc:
        raise ValueError('document_extraction_incomplete') from exc
    if any(i.kind in {'extraction_failed', 'extraction_truncated'} for i in packaged.extraction_issues):
        raise ValueError('document_extraction_incomplete')
    targets = _docx_targets(data) if filename.lower().endswith('.docx') else office_targets(data) if filename.lower().endswith(('.pptx', '.xlsx')) else {}
    findings, issues, matched = [], list(packaged.extraction_issues), set()
    for target in packaged.findings:
        loc = target.locator
        key = loc.part_name+'#'+loc.element_ref if loc.part_name else loc.element_ref
        aliases = targets.get(key, ({key.casefold()}, None))[0]
        candidates = [r for r in rows if r['rule_id'] == target.success_criterion
                      and str(r.get('instance_key', '')).casefold() in aliases]
        group = [r for r in rows if r['rule_id'] == target.success_criterion]
        # A single legacy assessed finding and a single current target are unambiguous.
        if not candidates and len(group) == 1 and len(packaged.findings) == 1 and str(group[0].get('instance_key', '')).startswith('aggregate-instance:'):
            candidates = group
        if len(candidates) == 1 and candidates[0]['finding_id'] not in matched:
            row = candidates[0]
            matched.add(row['finding_id'])
            findings.append(replace(target, finding_id=row['finding_id']))
    advisory = []
    for row in rows:
        if row['finding_id'] not in matched:
            advisory.append({key: row[key] for key in ('rule_id', 'instance_key', 'message', 'evidence') if key in row})
            issues.append(ExtractionIssue('finding_not_packaged', 'No unambiguous supported target remains in this saved document.', (row['finding_id'],)))
    context = packaged.text_context
    if advisory:
        context += '\n[Advisory selected findings: no write authorization; context only, never include advisory items in edits or unresolved]\n' + json.dumps(advisory, sort_keys=True)
    if len(context) > LIMITS.max_text_chars:
        raise ValueError('document_extraction_incomplete')
    manifest = replace(packaged, findings=tuple(findings), extraction_issues=tuple(issues), text_context=context)
    if filename.lower().endswith(('.docx', '.pptx', '.xlsx')):
        evidence = []
        for finding in findings:
            key = finding.locator.part_name+'#'+finding.locator.element_ref
            rid_locator = targets.get(key, (None, None))[1]
            image = _image(data, rid_locator) if rid_locator else None
            if image is None:
                issues.append(ExtractionIssue('missing_visual_evidence', 'Image content is unavailable or exceeds the image limit.', (finding.finding_id,)))
                continue
            evidence.append(Evidence(EvidenceKind.IMAGE, finding.locator, 'Image content needed to describe this assessed image.',
                                     image_ref='sha256:'+hashlib.sha256(image).hexdigest()))
        manifest = replace(manifest, evidence=tuple(evidence), extraction_issues=tuple(issues))
    if filename.lower().endswith('.pdf'):
        from collections import Counter
        from experiments.document_wide_ai.packaging.pdf_packager import package_pdf
        from experiments.document_wide_ai.packaging.pdf_images import render_pdf_page
        figures = package_pdf(data, max_text_chars=LIMITS.max_text_chars).figures
        per_page = Counter(f.page_index for f in figures)
        from document_wide_native_pdf import figure_regions, region_image
        regions = figure_regions(data) if input_mode == 'native_pdf' else {}
        evidence, rendered, image_bytes = [], {}, 0
        for finding in findings:
            if finding.success_criterion != '1.1.1':
                continue
            page = finding.locator.page_index
            image = None
            region = regions.get(finding.locator.element_ref)
            if region and len(evidence) < LIMITS.max_images:
                image = region_image(data, region)
                if image and image_bytes + len(image) <= 4 * 1024 * 1024:
                    image_bytes += len(image)
                else:
                    image = None
            elif page is not None and per_page[page] == 1 and len(evidence) < LIMITS.max_images and len(rendered) < LIMITS.max_images:
                image = render_pdf_page(data, page)
                if image and image_bytes + len(image) <= 4 * 1024 * 1024:
                    rendered[page] = image
                    image_bytes += len(image)
                else:
                    image = None
            if image is None:
                issues.append(ExtractionIssue('missing_visual_evidence',
                    'A unique tagged Figure page could not be rendered within limits; region grounding is required for multiple Figures.',
                    (finding.finding_id,)))
                continue
            evidence.append(Evidence(EvidenceKind.IMAGE, finding.locator,
                (f'Page {page + 1}, explicit tagged Figure Layout BBox {region[1]}; matching region crop.' if region else
                 f'Page {page + 1}, the sole tagged Figure on this page; full page context, not a region crop.'),
                text=json.dumps({'native_pdf_region': region[1], 'page_number': page + 1,
                                 'coordinate_system': 'PDF points, origin bottom-left; x increases right, y increases up'}) if region else None,
                image_ref='sha256:'+hashlib.sha256(image).hexdigest()))
        manifest = replace(manifest, evidence=tuple(evidence), extraction_issues=tuple(issues))
    return manifest


def _image(data, locator):
    from remediate_office import image_bytes_for_locator
    from PIL import Image
    if locator and locator.startswith('word/') and '#' in locator:
        from office_visible_image import visible_word_relationship
        part, rid = locator.split('#', 1)
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            cropped, visible = visible_word_relationship({n: z.read(n) for n in z.namelist()}, part, rid)
        if cropped:
            image = visible
        else:
            image = image_bytes_for_locator(data, locator)
    else:
        image = image_bytes_for_locator(data, locator)
    if not image or len(image) > 1024 * 1024:
        return None
    try:
        with Image.open(io.BytesIO(image)) as img:
            if img.format not in {'PNG', 'JPEG'} or max(img.size) > 1568 or img.width * img.height > 2500000:
                return None
            img.verify()
    except Exception:
        return None
    return image


def package_images(data, manifest):
    if hashlib.sha256(data).hexdigest() != manifest.source_sha256:
        raise ValueError('document_source_changed')
    from document_wide_office import targets as office_targets
    targets = _docx_targets(data) if manifest.document_format.value == 'docx' else office_targets(data) if manifest.document_format.value in {'pptx', 'xlsx'} else {}
    images = {}
    rendered = {}
    native_regions = None
    for evidence in manifest.evidence:
        if evidence.kind != EvidenceKind.IMAGE:
            continue
        loc = evidence.source_locator
        if manifest.document_format.value == 'pdf':
            from experiments.document_wide_ai.packaging.pdf_images import render_pdf_page
            page = loc.page_index
            if evidence.text and 'native_pdf_region' in evidence.text:
                from document_wide_native_pdf import figure_regions, region_image
                if native_regions is None:
                    native_regions = figure_regions(data)
                region = native_regions.get(loc.element_ref)
                if not region or json.loads(evidence.text).get('native_pdf_region') != list(region[1]) or json.loads(evidence.text).get('page_number') != region[0] + 1:
                    raise ValueError('document_image_changed')
                image = region_image(data, region)
            elif page not in rendered:
                rendered[page] = render_pdf_page(data, page) if page is not None else None
            if not (evidence.text and 'native_pdf_region' in evidence.text):
                image = rendered[page]
        else:
            rid = targets.get(loc.part_name+'#'+loc.element_ref, (None, None))[1]
            image = _image(data, rid) if rid else None
        if image is None or evidence.image_ref != 'sha256:'+hashlib.sha256(image).hexdigest():
            raise ValueError('document_image_changed')
        images[evidence.image_ref] = image
    if len(images) > 8 or sum(map(len, images.values())) > 4 * 1024 * 1024:
        raise ValueError('document_too_large')
    return images


def package_native_pdf(data, manifest):
    """Return the exact current candidate; never rewrite or persist native payload bytes."""
    if manifest.document_format.value != 'pdf':
        raise ValueError('document_format_unsupported')
    from document_wide_native_pdf import validate_native_pdf
    validate_native_pdf(data, source_sha256=manifest.source_sha256)
    if len(manifest.text_context) > LIMITS.max_text_chars or len(manifest.findings) > LIMITS.max_findings:
        raise ValueError('document_too_large')
    return data

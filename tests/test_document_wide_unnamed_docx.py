"""An assessed unnamed drawing must get exact visual evidence and a writable locator."""
import io
import zipfile
from lxml import etree
import pytest
from apply_alt import apply_alt_text
from document_wide_manifest import build_manifest, package_images
from experiments.document_wide_ai.packaging.docx_packager import package_docx
from test_office_empty_crop_drafting import _document, _entries, _descr_by_rid
from test_document_wide_manifest import setup_manifest


def test_real_office_finding_becomes_a_grounded_writable_target(isolated_store, monkeypatch, tmp_path):
    import scanner
    data = _document()
    path = tmp_path / 'a.docx'
    path.write_bytes(data)
    result, _ = scanner.analyse_and_assess(tmp_path, 'a.docx', detect_pii=False)
    findings = [r for r in result.get('issues', []) if r.get('ruleId') == 'DOCX-ALT-001']
    assert len(findings) == 1, result
    locations = [r['location'] for r in findings]
    rows = setup_manifest(isolated_store, monkeypatch, data, 'a.docx', '1.1.1', locations)
    manifest = build_manifest(isolated_store, 'scan', 'a.docx', data)
    assert [f.finding_id for f in manifest.findings] == [rows[0]['finding_id']]
    assert not manifest.extraction_issues
    images = package_images(data, manifest)
    assert len(images) == 1 and next(iter(images.values())) == _entries(data)['word/media/image1.png']
    loc = manifest.findings[0].locator
    fixed, applied, unresolved = apply_alt_text(data, {loc.part_name+'#'+loc.element_ref: 'Diagram of discharge equipment connections.'})
    assert applied and not unresolved
    assert set(_descr_by_rid(fixed).values()) == {'Diagram of discharge equipment connections.'}
    path.write_bytes(fixed)
    assert not [r for r in scanner.analyse_and_assess(tmp_path, 'a.docx', detect_pii=False)[0].get('issues', []) if r.get('ruleId') == 'DOCX-ALT-001']
    assert _entries(fixed)['word/media/image1.png'] == _entries(data)['word/media/image1.png']


def test_shared_relationship_is_not_guessed(isolated_store, monkeypatch):
    data = _document(shared=True)
    rows = setup_manifest(isolated_store, monkeypatch, data, 'a.docx', '1.1.1',
                          ['docx:drawing:1:paragraph:0', 'docx:drawing:2:paragraph:1'])
    manifest = build_manifest(isolated_store, 'scan', 'a.docx', data)
    assert not manifest.findings and not manifest.evidence
    assert {f for i in manifest.extraction_issues for f in i.related_finding_ids} == {r['finding_id'] for r in rows}


def test_two_distinct_unnamed_drawings_keep_their_identities(isolated_store, monkeypatch):
    data = _document(pictures=2)
    rows = setup_manifest(isolated_store, monkeypatch, data, 'a.docx', '1.1.1',
                          ['docx:drawing:2:paragraph:1', 'docx:drawing:1:paragraph:0'])
    manifest = build_manifest(isolated_store, 'scan', 'a.docx', data)
    assert [f.finding_id for f in manifest.findings] == [rows[1]['finding_id'], rows[0]['finding_id']]
    assert len(package_images(data, manifest)) == 2


def test_cropped_or_flipped_image_never_gets_unfaithful_pixels(isolated_store, monkeypatch):
    data = _document(xfrm={'flipH': '1'})
    setup_manifest(isolated_store, monkeypatch, data, 'a.docx', '1.1.1', ['docx:drawing:1:paragraph:0'])
    manifest = build_manifest(isolated_store, 'scan', 'a.docx', data)
    assert not manifest.evidence
    assert any(i.kind == 'missing_visual_evidence' for i in manifest.extraction_issues)


def test_a_named_shape_cannot_steal_an_unnamed_relationship():
    data = _document(pictures=2)
    entries = _entries(data)
    root = etree.fromstring(entries['word/document.xml'])
    ns = {'wp': 'http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing',
          'a': 'http://schemas.openxmlformats.org/drawingml/2006/main',
          'r': 'http://schemas.openxmlformats.org/officeDocument/2006/relationships'}
    rid = root.find('.//a:blip', ns).get('{'+ns['r']+'}embed')
    root.findall('.//wp:docPr', ns)[1].set('name', rid)
    entries['word/document.xml'] = etree.tostring(root)
    out = io.BytesIO()
    with zipfile.ZipFile(out, 'w') as z:
        for n, raw in entries.items(): z.writestr(n, raw)
    targets = package_docx(out.getvalue(), max_text_chars=60000).undescribed_images
    assert len(targets) == 1  # only the already-named target, no duplicate locator

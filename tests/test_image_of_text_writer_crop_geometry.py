"""The 1.4.5 image-of-text WRITER must measure the crop, not look for the element.

THE PRODUCTION DEFECT THIS REPRODUCES (2026-09-17, build 2026.9.17.3, sha f1da649).
A reviewer approved an OCR transcript for a Word picture in `UTSW_Discharge_Summary.docx`.
The approval was recorded, the `apply_approved_values` job was created, ran and finished
`done` in two seconds — and wrote nothing:

    apply.unresolved   1 approved image-of-text replacement value(s) were not written: image 1
    apply.unverified   wrote no image-of-text replacement value(s) for ['1.4.5']: The image is
                       cropped in Word. Review a transcription of the visible crop and confirm
                       no useful diagram content would be lost before replacing it. The original
                       image is kept unchanged. Credit withheld; the approved value is kept for
                       retry

The picture was NOT cropped. `apply_office_image_replacement` decided "cropped" from the mere
PRESENCE of an `a:srcRect` element, and OOXML treats an omitted inset as 0 — so `<a:srcRect/>`,
which Word writes for a picture whose crop handles were never moved or were reset, hides no
pixels at all. The row was left `approved`, `applied` NULL, the finding outstanding, and the
reviewer had no action left to take; re-approving would have produced the same refusal.

THE ASYMMETRY IS THE BUG. #2127 taught the DRAFTING path this exact geometry —
`office_visible_image.has_word_crop`, which vouches for the placement before it calls a zero
inset harmless — so the reviewer was shown a draft for the whole raster while the writer
refused the same picture. `test_office_empty_crop_drafting.py` pins the drafting half; this
module pins the writing half against the SAME predicate, so the two cannot drift apart again.

WHAT IS DELIBERATELY NOT WIDENED. The writer DELETES the picture and substitutes a text box,
so every refusal here is a safety refusal and stays one. A real inset, a second srcRect, an
attribute outside l/t/r/b, a tile, a rotation, a flip, shared media, or geometry that cannot be
read at all still refuses with the same reason string, and `test_cropped_write_outcome.py`
still parses it. Fail closed on unknown geometry: a refusal costs a retry, a wrong write
deletes a picture nobody agreed to lose. What changes is only that a picture hiding nothing is
no longer called cropped.

The fixtures are real OOXML packages built by python-docx and driven through the real writer
and, for the end-to-end cases, through `handlers._apply_approved_values` — not mocked xpath.
"""
from __future__ import annotations

import io
import sys
import zipfile

import pytest
from lxml import etree as ET

from apply_office_image_replacement import (
    NS, apply_office_image_replacement, office_image_replacement_refusal,
)
from office_visible_image import has_word_crop
from test_remediation_verified_office_image_replacement import (  # noqa: F401  (store fixture)
    TEXT, document, members, mutate, store,
)

CROP_REFUSAL = 'cropped_image_requires_visible_transcription'
W_DOC = 'word/document.xml'


def placed(srcrect=None, *, tile=False, xfrm=None, extra_rect=False, shared=False):
    """A real .docx whose one inline picture carries the requested geometry.

    `srcrect=None` omits the element; `{}` writes the bare `<a:srcRect/>` Word emits for a
    picture whose crop was never set or was cleared — the production shape. `shared` gives the
    document a second picture with IDENTICAL bytes, which python-docx deduplicates into one
    media part behind one relationship: the shared placement no lane may reason about.
    """
    data = document('docx', shared=shared)
    if srcrect is None and not tile and not xfrm and not extra_rect:
        return data

    def edit(root):
        for blip in root.xpath('.//a:blip', namespaces=NS):
            fill = blip.getparent()
            if tile:
                ET.SubElement(fill, '{' + NS['a'] + '}tile')
            for _ in range(2 if extra_rect else (1 if srcrect is not None else 0)):
                rectangle = ET.SubElement(fill, '{' + NS['a'] + '}srcRect')
                for key, value in (srcrect or {}).items():
                    rectangle.set(key, value)
        for transform in root.xpath('.//a:xfrm', namespaces=NS):
            for key, value in (xfrm or {}).items():
                transform.set(key, value)

    return mutate(data, W_DOC, edit)


def srcrects(data):
    root = ET.fromstring(members(data)[W_DOC])
    return [dict(e.attrib) for e in root.xpath('.//a:srcRect', namespaces=NS)]


# Geometry whose rendered pixels cannot be predicted from the raster. Every one carries an
# EMPTY srcRect where it can, so the case under test is "the insets are harmless but the
# placement is not" — the direction in which widening the writer would destroy content.
UNVOUCHABLE = [
    ('real inset l=10000', dict(srcrect={'l': '10000'})),
    ('real inset t=19861', dict(srcrect={'t': '19861'})),
    ('tiled fill', dict(srcrect={}, tile=True)),
    ('rotated', dict(srcrect={}, xfrm={'rot': '5400000'})),
    ('flipped horizontally', dict(srcrect={}, xfrm={'flipH': '1'})),
    ('flipped vertically', dict(srcrect={}, xfrm={'flipV': '1'})),
    ('two srcRects on one fill', dict(srcrect={}, extra_rect=True)),
    ('unknown attribute', dict(srcrect={'foo': '1'})),
    ('zero inset beside an unknown attribute', dict(srcrect={'l': '0', 'foo': '0'})),
    ('non-integer inset', dict(srcrect={'t': 'oops'})),
    ('fractional inset', dict(srcrect={'t': '0.0'})),
    ('negative inset', dict(srcrect={'l': '-1'})),
    ('out-of-range inset', dict(srcrect={'l': '100000'})),
]
UNVOUCHABLE_IDS = [label for label, _ in UNVOUCHABLE]
UNVOUCHABLE_OPTIONS = [options for _, options in UNVOUCHABLE]

# Insets that provably hide nothing, on a placement the vouching battery accepts.
HARMLESS = [{}, {'l': '0', 't': '0', 'r': '0', 'b': '0'}, {'b': '0'}, {'l': '0', 'r': '0'}]


@pytest.fixture
def office_analyser():
    """conftest's `_office_analyser_for_lane_proofs`, which is scoped to `test_remediation_verified_*`.

    Verification fails closed, so on a host with no .NET Office CLI every Office scan grades
    `error` and every lane correctly withholds credit — measured here: the PLAIN fixture, with
    no srcRect at all, re-scans `ok=False, "scan status 'error', 1 rule(s) skipped"` exactly as
    the empty-crop one does. Without this the end-to-end test below would prove only that the
    toolchain is missing, which it would do just as happily with the bug still in place.

    Requested explicitly rather than made autouse, and identical to conftest's stand-in: a run
    that SUCCEEDED and found nothing of its own, so every finding still comes from the
    first-party detectors. Where the real CLI is present (CI) this changes nothing.
    """
    import engines
    if engines.OFFICE_OK:
        yield
        return
    import scanner
    from pathlib import Path as _Path
    original = scanner._analyse_office
    scanner._analyse_office = lambda dest: {
        p.name: {'succeeded': True, 'errors': [], 'issues': []} for p in _Path(dest).iterdir()}
    try:
        yield
    finally:
        scanner._analyse_office = original


def test_fixture_carries_the_reproduced_production_shape():
    """A bare `<a:srcRect/>`, one picture, one media part — and a raster OCR really reads."""
    data = placed({})
    assert srcrects(data) == [{}]
    assert len([p for p in members(data) if '/media/' in p]) == 1
    root = ET.fromstring(members(data)[W_DOC])
    assert len(root.xpath('.//wp:inline//a:blip', namespaces=NS)) == 1
    # Not a mocked element: the same document with no srcRect at all is writable today, which
    # is what makes the refusal below attributable to the crop test and nothing else.
    assert apply_office_image_replacement(placed(None), 'docx', {'image 1': TEXT})[1]


@pytest.mark.parametrize('srcrect', HARMLESS, ids=[str(s) for s in HARMLESS])
def test_a_srcrect_that_hides_nothing_is_written_not_refused(srcrect):
    """THE DEFECT. Before the fix every one of these returned (data, [], ['image 1'])."""
    data = placed(srcrect)
    assert has_word_crop(data, 'image 1') is False, 'the drafting path already calls it uncropped'
    assert office_image_replacement_refusal(data, 'docx', 'image 1') is None
    fixed, applied, unresolved = apply_office_image_replacement(data, 'docx', {'image 1': TEXT})
    assert unresolved == []
    assert applied == [{'locator': 'image 1', 'before': 'image of text', 'after': TEXT}]
    # The raster is REMOVED, not merely unreferenced: ocr._ooxml_images walks the namelist, so
    # leaving the bytes behind would keep 1.4.5 failing.
    assert not any('/media/' in part for part in members(fixed))
    from docx import Document
    reopened = Document(io.BytesIO(fixed))
    assert reopened.paragraphs[0].text == 'Unrelated heading'
    assert reopened.paragraphs[2].text == TEXT
    assert len(reopened.inline_shapes) == 0


def test_a_zero_inset_answers_exactly_as_no_srcrect_at_all():
    """The only comparison that licenses the write: a plain untransformed inline picture.

    Nothing here says a TRANSFORMED picture may be treated that way — see the unvouchable
    battery below, which is most of this module.
    """
    with_rect = apply_office_image_replacement(placed({}), 'docx', {'image 1': TEXT})
    without = apply_office_image_replacement(placed(None), 'docx', {'image 1': TEXT})
    assert with_rect[1] == without[1] and with_rect[2] == without[2] == []
    assert members(with_rect[0]).keys() == members(without[0]).keys()


@pytest.mark.parametrize('options', UNVOUCHABLE_OPTIONS, ids=UNVOUCHABLE_IDS)
def test_unvouchable_geometry_still_refuses_and_leaves_the_package_untouched(options):
    data = placed(**options)
    assert has_word_crop(data, 'image 1') is True
    assert office_image_replacement_refusal(data, 'docx', 'image 1') == CROP_REFUSAL
    # Byte-identical: a refusal must never half-write.
    assert apply_office_image_replacement(data, 'docx', {'image 1': TEXT}) == (data, [], ['image 1'])


def test_shared_media_with_a_harmless_srcrect_is_still_refused():
    """One media part behind two drawings: replacing it would delete a picture twice over.

    The refusal REASON stays None rather than claiming a crop — the obstacle is the sharing,
    and telling a reviewer to check a crop that does not exist is the same lie this module
    exists to remove, pointed the other way.
    """
    data = placed({}, shared=True)
    root = ET.fromstring(members(data)[W_DOC])
    embeds = [b.get('{' + NS['r'] + '}embed') for b in root.xpath('.//a:blip', namespaces=NS)]
    assert len(embeds) == 2 and len(set(embeds)) == 1, 'fixture must actually share the media'
    assert has_word_crop(data, 'image 1') is True
    assert office_image_replacement_refusal(data, 'docx', 'image 1') is None
    assert apply_office_image_replacement(data, 'docx', {'image 1': TEXT}) == (data, [], ['image 1'])


def test_xlsx_keeps_the_fail_closed_bare_presence_test():
    """`has_word_crop` reads word/document.xml, so it can vouch for nothing in a workbook.

    The relaxation is Word-only by construction. A workbook picture with a harmless srcRect
    therefore still refuses — under-writing, which costs a retry, rather than over-writing.
    """
    def crop(root):
        ET.SubElement(root.xpath('.//a:blip/..', namespaces=NS)[0],
                      '{' + NS['a'] + '}srcRect')
    data = mutate(document('xlsx'), 'xl/drawings/drawing1.xml', crop)
    assert apply_office_image_replacement(data, 'xlsx', {'image 1': TEXT}) == (data, [], ['image 1'])


def test_a_real_crop_refusal_still_reaches_the_reviewer_through_the_apply_job(
        monkeypatch, store, office_analyser):
    """The safety path, end to end: a genuine crop still logs the exact production sentence.

    Same assertions `test_cropped_word_replacement_activity` makes, re-run here against a
    NON-zero inset so the fix cannot be mistaken for having removed the refusal.
    """
    import core
    import handlers
    import test_remediation_verified_pptx_image_of_text as lane
    monkeypatch.setattr(lane, 'FILE', 'genuine-crop.docx')
    original = placed({'t': '19861'})
    blob = lane._Blob(original)
    lane._seed(store, {'image 1': TEXT})
    monkeypatch.setattr(core, 'store', store)
    monkeypatch.setitem(sys.modules, 'blob', blob)
    handlers._apply_approved_values({'scan_id': lane.SID, 'file': lane.FILE}, {})

    notes = [d['detail'] for d in store.list_decisions(scan_id=lane.SID)
             if d['action'] == 'apply.unverified']
    assert len(notes) == 1
    assert 'image is cropped in Word' in notes[0]
    assert 'no useful diagram content would be lost' in notes[0]
    assert 'original image is kept unchanged' in notes[0]
    assert 'the approved value is kept for retry' in notes[0]
    # Nothing saved, nothing credited, the picture still there, the approval still standing.
    assert not blob.uploads
    assert blob.data == original
    assert store.count_unapplied_approved_values(lane.SID, lane.FILE) > 0
    row = [r for r in store.list_hitl_queue(scan_id=lane.SID) if r.get('apply_outcome')]
    assert len(row) == 1 and row[0]['apply_outcome']['outcome'] == 'nothing_written'
    assert row[0]['status'] == 'approved' and not row[0].get('applied')


def test_the_production_shape_now_writes_and_verifies_through_the_apply_job(
        monkeypatch, store, office_analyser):
    """The defect, end to end through `handlers._apply_approved_values`.

    Before the fix this logged the crop refusal and saved nothing: no upload, the raster still
    in the package, the row still counted as owing content.

    The assertions are about the WRITE, not about the re-scan's verdict. `_verify_residual`
    runs the production scanner, and this environment has no Office analyser CLI — measured:
    the plain, unmutated fixture re-scans `ok=False, "scan status 'error', 1 rule(s) skipped"`
    exactly as this one does, so a "verified" assertion here would be testing the toolchain.
    What it does pin is that NOTHING is refused as cropped and every locator resolved.
    """
    import core
    import handlers
    import test_remediation_verified_pptx_image_of_text as lane
    monkeypatch.setattr(lane, 'FILE', 'empty-crop.docx')
    original = placed({})
    blob = lane._Blob(original)
    lane._seed(store, {'image 1': TEXT})
    monkeypatch.setattr(core, 'store', store)
    monkeypatch.setitem(sys.modules, 'blob', blob)
    handlers._apply_approved_values({'scan_id': lane.SID, 'file': lane.FILE}, {})

    details = [d['detail'] for d in store.list_decisions(scan_id=lane.SID)]
    assert not any('cropped in Word' in d for d in details), details
    assert not any('wrote no image-of-text' in d for d in details), details
    assert [d['detail'] for d in store.list_decisions(scan_id=lane.SID)
            if d['action'] == 'apply.unresolved'] == []
    assert blob.uploads, 'the corrected copy must be saved'
    assert not any('/media/' in part for part in members(blob.data))
    # The row no longer owes the document content: the approved value reached the bytes.
    assert store.count_unapplied_approved_values(lane.SID, lane.FILE) == 0
    # The approved transcript is in the document as real text, and unrelated content survives.
    from docx import Document
    reopened = Document(io.BytesIO(blob.data))
    assert reopened.paragraphs[0].text == 'Unrelated heading'
    assert any(TEXT.splitlines()[0] in p.text for p in reopened.paragraphs)
    assert len(reopened.inline_shapes) == 0


def test_the_written_copy_no_longer_trips_the_real_ocr_detector(tmp_path):
    """Independent proof the write clears the finding, from the detector rather than the writer."""
    import ocr
    if not ocr.is_available():
        pytest.skip('tesseract unavailable')
    path = tmp_path / 'empty-crop.docx'
    path.write_bytes(placed({}))
    assert ocr.images_of_text(path, '.docx'), 'the fixture must actually trip 1.4.5'
    fixed, applied, unresolved = apply_office_image_replacement(
        path.read_bytes(), 'docx', {'image 1': TEXT})
    assert applied and not unresolved
    path.write_bytes(fixed)
    assert ocr.images_of_text(path, '.docx') == []


def test_a_genuine_crop_keeps_its_picture_byte_for_byte(tmp_path):
    """The safety promise stated as behaviour: nothing is destroyed and nothing is hidden.

    `ocr._ooxml_images` is the walk 1.4.5 is built on — it reads the ZIP namelist and never
    opens the document — so a raster it still finds is a finding still reachable. Asserted
    through the detector's own traversal rather than the writer's index, and byte-for-byte,
    because the failure this guards against is a "tidy up" that unreferences the picture
    while leaving the criterion failing.
    """
    import ocr
    original = placed({'t': '19861'})
    fixed, applied, unresolved = apply_office_image_replacement(original, 'docx', {'image 1': TEXT})
    assert (fixed, applied, unresolved) == (original, [], ['image 1'])
    path = tmp_path / 'genuine-crop.docx'
    path.write_bytes(fixed)
    assert list(ocr._ooxml_images(path)), 'a refused write must leave the raster reachable'
    before, after = members(original), members(fixed)
    media = [name for name in before if '/media/' in name]
    assert len(media) == 1 and after[media[0]] == before[media[0]]
    with zipfile.ZipFile(io.BytesIO(fixed)) as zin:
        assert 'srcRect' in zin.read(W_DOC).decode('utf-8')

"""ADR 0055 describe-instead-of-replace on DOCX and XLSX, proved end to end.

tests/test_remediation_described_image_round_trip.py proves the pptx lane. The ADR's closing
section left "docx and xlsx" explicitly undecided, and this is the other half: the same reviewer
decision — keep the image of text, describe it — reaching a Word document and a workbook.

WHAT IS NEW HERE IS THE LOCATOR TRANSLATION, AND ONLY THAT. Every other part of the feature is
format-agnostic and was already working: the `described_not_replaced` resolution, store.
queue_described_image_alt's `1.1.1/described` row, and the alt lane's credit_rule_ids. What did
not transfer is the one thing the ADR said was pptx-specific, and it fails for docx and xlsx in
three different measured ways (see api/apply_office_image_of_text.py). The two that this file
would go red on:

  * A docx places every body picture in ONE part behind ONE relationship id, so the pptx-shaped
    'word/document.xml#rId9' reaches only the FIRST placement — apply_alt.resolve_target is
    first-match-wins by contract. `test_the_pptx_shaped_locator_would_describe_only_one` measures
    that directly, and it is why the fixture is a two-placement document.
  * An xlsx writes its relationship targets ABSOLUTE ('/xl/media/image1.png'), which the pptx
    canonicaliser turns into a path matching nothing in the namelist.

WHAT THIS CLAIMS: the picture is still in the package, every placement carries the reviewer's
description as alt text, a REAL re-scan agrees 1.1.1 has cleared, and the file certifies with
1.4.5 recorded as resolved by judgement rather than fixed. NOT that the document now satisfies
1.4.5 — it does not, deliberately, and the reviewer said so.

Nothing but the blob store is patched: handlers._apply_approved_values runs the production seam
through proposals.verify_residual_scs to scanner.analyse_and_assess, and the decision is taken
through routes.hitl.hitl_update rather than by poking the store.

NO PROVES_LANES DECLARATION, AND THE FILENAME IS PART OF THAT — test_remediation_described_… and
not test_remediation_verified_…, which is the namespace tests/test_capability_assisted_contract.py
globs. That contract requires each (format, criterion) lane to be claimed by exactly ONE such
module, and ('docx','1.1.1') and ('xlsx','1.1.1') are already claimed by
test_remediation_verified_docx_alt.py and test_remediation_verified_xlsx_alt.py. What this adds
is a new PATH INTO those lanes, not a capability: a description is ordinary 1.1.1 alt text,
written by the same apply_alt_text and verified against the same criterion. Declaring the lanes
twice would tell the matrix they gained something they did not — the same call the pptx fixture
made, for the same reason, and the guard was right both times.
"""
from __future__ import annotations

import functools
import glob
import io
import sys
import tempfile
import zipfile
from pathlib import Path

import pytest
from hitl_viewed import viewed_fields

ACP = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ACP / "api"))

pytest.importorskip("docx")
pytest.importorskip("openpyxl")
pytest.importorskip("PIL")

SID = "rv-office-described"

BODY = "Unrelated body copy that must survive the write."
MORE = "More body copy that must also survive."

CARD = ("Benefits at a glance",
        "Medical dental and vision cover",
        "Enrollment closes on Friday")
NOTICE = ("Payroll calendar notice",
          "Timesheets are due each Monday",
          "Approvals close at five o'clock")

# What the reviewer writes: prose ABOUT the picture, never the transcript. The distinction is the
# whole reason this lane exists — store.queue_described_image_alt switches off _row_approved_values'
# proposed_value fallback precisely so an OCR transcript can never become alt text by default.
DESC_CARD = ("A benefits summary card in the company's display face, listing medical, dental "
             "and vision cover, and noting that enrollment closes on Friday.")
DESC_NOTICE = ("A payroll calendar notice card stating that timesheets are due on Mondays and "
               "that approvals close at five.")


def _ocr_ready() -> bool:
    import ocr
    return ocr.is_available()


needs_ocr = pytest.mark.skipif(not _ocr_ready(),
                               reason="tesseract/pytesseract unavailable — 1.4.5 cannot fire")


def _font():
    from PIL import ImageFont
    for pat in ("/usr/share/fonts/**/DejaVuSans.ttf", "/usr/share/fonts/**/DejaVuSans*.ttf",
                "/usr/share/fonts/**/*.ttf", "/System/Library/Fonts/Supplemental/Arial.ttf"):
        hits = glob.glob(pat, recursive=True)
        if hits:
            return ImageFont.truetype(hits[0], 34)
    return ImageFont.load_default()


@functools.lru_cache(maxsize=None)
def _png(lines: tuple[str, ...], size: tuple[int, int] = (760, 260)) -> Path:
    """A picture of prose. Cached for the reason the pptx fixture caches its own: tesseract is
    CPU-bound, the backend suite runs -n auto, and an OCR-heavy file can push OTHER tests' reads
    past ACP_OCR_TIMEOUT_S."""
    from PIL import Image, ImageDraw
    im = Image.new("RGB", size, "white")
    d = ImageDraw.Draw(im)
    font = _font()
    for i, line in enumerate(lines):
        d.text((24, 24 + i * 70), line, fill="black", font=font)
    p = Path(tempfile.mkdtemp()) / f"{abs(hash(lines))}.png"
    im.save(p)
    return p


@functools.lru_cache(maxsize=None)
def _docx() -> bytes:
    """ONE image of text, placed TWICE — one media part, one relationship id, two pictures.

    That shape is load-bearing rather than incidental, and it is the shape Word actually
    produces: a docx body picture lives in word/document.xml and an image reused in the same
    document is referenced through the SAME rId. So the pptx lane's 'part#rId' locator names one
    relationship that two <wp:docPr> elements share, and describing it reaches only the first.
    A single-placement fixture would pass against the broken translation.
    """
    import docx
    from docx.shared import Inches
    doc = docx.Document()
    doc.add_paragraph(BODY)
    doc.add_picture(str(_png(CARD)), width=Inches(3.5))
    doc.add_paragraph(MORE)
    doc.add_picture(str(_png(CARD)), width=Inches(3.5))
    out = Path(tempfile.mkdtemp()) / "keepme.docx"
    doc.save(out)
    return out.read_bytes()


@functools.lru_cache(maxsize=None)
def _xlsx() -> bytes:
    """TWO different images of text, one per sheet — two media parts, two DRAWING parts.

    Two rather than one because the discriminating question for a workbook is whether each
    description reaches its OWN picture: the sheet is not the alt-bearing part, the drawing it
    references is, and 'image N' names neither. A resolver that mapped both cards to the same
    drawing would still clear 1.1.1 on a one-image fixture and be wrong.
    """
    import openpyxl
    from openpyxl.drawing.image import Image as XLImage
    wb = openpyxl.Workbook()
    ws1 = wb.active
    ws1.title = "Summary"
    ws1["A1"] = BODY
    ws1.add_image(XLImage(str(_png(CARD))), "B2")
    ws2 = wb.create_sheet("Payroll")
    ws2["A1"] = MORE
    ws2.add_image(XLImage(str(_png(NOTICE))), "B2")
    out = Path(tempfile.mkdtemp()) / "keepme.xlsx"
    wb.save(out)
    return out.read_bytes()


def _spill(data: bytes, name: str) -> Path:
    p = Path(tempfile.mkdtemp()) / name
    p.write_bytes(data)
    return p


@functools.lru_cache(maxsize=None)
def _assess_cached(data: bytes, name: str) -> frozenset:
    from assessment_policy import _extract_sc
    from scanner import analyse_and_assess
    with tempfile.TemporaryDirectory() as d:
        (Path(d) / name).write_bytes(data)
        fd, _ = analyse_and_assess(Path(d), name, detect_pii=False)
    return frozenset(sc for i in (fd or {}).get("issues", [])
                     if (sc := _extract_sc(i.get("wcag", ""))))


def _assess(data: bytes, name: str) -> set[str]:
    """The SCs a REAL assessment reports — the same call the production re-verification makes.

    Keyed on the BYTES, so identical packages are scanned once. Several tests here assess the
    same original document, and the two round trips produce byte-identical output to the two
    certification tests that follow them; a full OCR scan each time is minutes of the backend
    suite spent re-deriving an answer that cannot have changed.
    """
    return set(_assess_cached(data, name))


def _media(data: bytes) -> list[str]:
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        return [n for n in z.namelist() if "/media/" in n]


def _docx_descrs(data: bytes) -> list[str]:
    """Every body picture's alt text, read back through python-docx — a reader with no part in
    the write, so this cannot pass by agreeing with the writer's own idea of the XML."""
    import docx
    doc = docx.Document(str(_spill(data, "read.docx")))
    return [el.get("descr", "") for el in doc.element.body.iter()
            if el.tag.endswith("}docPr")]


def _xlsx_descrs(data: bytes) -> dict[str, str]:
    """{drawing part: alt text} straight from the package. openpyxl drops the anchors' alt text
    on re-open, so the drawing parts are read as XML — still an independent read of the bytes,
    not a re-use of the writer's own map."""
    import re
    out: dict[str, str] = {}
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        for n in sorted(n for n in z.namelist() if re.match(r"xl/drawings/drawing\d+\.xml$", n)):
            xml = z.read(n).decode("utf-8")
            m = re.search(r"<(?:xdr:)?cNvPr\b[^>]*\bdescr=\"([^\"]*)\"", xml)
            out[n] = m.group(1) if m else ""
    return out


def _docx_texts(data: bytes) -> list[str]:
    import docx
    return [p.text for p in docx.Document(str(_spill(data, "read.docx"))).paragraphs]


def _xlsx_texts(data: bytes) -> list[str]:
    import openpyxl
    wb = openpyxl.load_workbook(str(_spill(data, "read.xlsx")))
    return [ws["A1"].value for ws in wb.worksheets]


@functools.lru_cache(maxsize=None)
def _locators(data: bytes, name: str) -> list[str]:
    """The locators the real 1.4.5 proposer mints — taken from the proposer, never hand-written,
    so this breaks if the locator scheme drifts instead of silently testing a fiction.

    Cached on the bytes: every test below asks the same two documents the same question, and the
    proposer OCRs each image twice (the AA band and the strict band) to answer it."""
    import proposals
    p = _spill(data, name)
    return [x["locator"] for x in proposals.propose_images_of_text(p, p.suffix)]


class _Blob:
    """The only thing patched in this module. Stores bytes verbatim; decides nothing."""

    def __init__(self, data: bytes):
        self.data, self.uploads = data, []

    def download_remediated(self, owner, sid, f):
        return self.data

    def upload_remediated(self, owner, sid, f, data, mime):
        self.data = data
        self.uploads.append((f, mime))
        return "http://b/2"
    # The approved writer publishes digest-scoped and moves the pointer at commit.
    def upload_immutable_retry(self, owner, sid, f, data, mime):
        return self.upload_remediated(owner, sid, f, data, mime)


@pytest.fixture()
def store(monkeypatch):
    import store as store_mod
    monkeypatch.setattr(store_mod, "_SQLITE_PATH", Path(tempfile.mkdtemp()) / "rvo.db")
    return store_mod.Store()


def _seed(store, name: str, data: bytes, locators: list[str]) -> str:
    """A scanned + remediated file with one image-of-text card, pending, one proposal per image."""
    store.init_scan_run(SID, "drive", 1, "2026-09-07T00:00:00Z", "rubric", "hash")
    store.save_file_result(SID, {
        "file": name, "engine": "office", "status": "pass", "score": 60, "compliant": 0,
        "skipped_rules": 0, "drive_file_id": "d1",
        "issues": [{"ruleId": "OCR_IMAGE_OF_TEXT", "wcag": "1.4.5 Images of Text",
                    "severity": "SERIOUS", "detail": f"embedded {loc} contains readable text"}
                   for loc in locators],
    }, "2026-09-07T00:00:00Z")
    store.record_remediation(SID, name, drive_write_url="http://d/1", blob_url="http://b/1")
    return store.enqueue_proposals(SID, name, "1.4.5", [
        {"locator": loc, "before": "text baked into an image",
         "proposed_value": "the OCR transcript", "rationale": "r", "source": "OCR"}
        for loc in locators], rule_name="Images of Text")


def _decide(store, item_id: str, monkeypatch, *, values):
    """The reviewer's decision, through the PRODUCTION route — not by poking the store.

    The route is where the ADR 0055 wiring lives (validation, and the queue_described_image_alt
    call), so a test that wrote the rows directly would prove the store and skip the feature.
    """
    import core
    from routes.hitl import HitlUpdate, hitl_update
    monkeypatch.setattr(core, "store", store)
    return hitl_update(item_id, HitlUpdate(status="approved", **viewed_fields(item_id),
                                           resolution=store.DESCRIBED_RESOLUTION,
                                           approved_values=values), None)


def _run_lane(monkeypatch, store, blob, name: str):
    """The production handler, with the re-scan UNPATCHED."""
    import core
    import handlers
    monkeypatch.setattr(core, "store", store)
    monkeypatch.setitem(sys.modules, "blob", blob)
    handlers._apply_approved_values({"scan_id": SID, "file": name}, {})


# ---------------------------------------------------------------- the fixtures are honest

@needs_ocr
def test_the_docx_pictures_are_really_one_image_of_text_placed_twice():
    """Without this every assertion below is vacuous: if OCR cannot read the fixture, 1.4.5
    never fires and 'the reviewer kept an image of text' describes nothing."""
    data = _docx()
    assert "1.4.5" in _assess(data, "keepme.docx")
    assert "1.1.1" in _assess(data, "keepme.docx")
    assert len(_media(data)) == 1                       # one media part…
    assert len(_docx_descrs(data)) == 2                 # …placed twice


@needs_ocr
def test_the_xlsx_sheets_really_carry_two_images_of_text():
    data = _xlsx()
    assert "1.4.5" in _assess(data, "keepme.xlsx")
    assert "1.1.1" in _assess(data, "keepme.xlsx")
    assert len(_media(data)) == 2
    assert len(_xlsx_descrs(data)) == 2                 # two drawing parts, one per sheet


@needs_ocr
@pytest.mark.parametrize("name,builder,count", [("keepme.docx", _docx, 1),
                                                ("keepme.xlsx", _xlsx, 2)])
def test_the_proposer_mints_media_index_locators(name, builder, count):
    """The locator this lane has to translate, taken from the real proposer. apply_alt cannot
    read it at all, which is the gap ADR 0055's translation fills."""
    import apply_alt
    import apply_office_image_of_text as aoit
    locs = _locators(builder(), name)
    assert locs == [f"image {i + 1}" for i in range(count)]
    for loc in locs:
        assert apply_alt.parse_locator(loc) is None
        assert aoit.is_media_index_locator(loc)


@needs_ocr
def test_the_pptx_shaped_locator_would_describe_only_one_placement():
    """The measurement this whole module exists because of, asserted rather than asserted-about.

    A docx reuses ONE relationship id for a picture placed twice, and apply_alt.resolve_target is
    first-match-wins, so the pptx lane's 'part#rId' shape describes the first placement and
    leaves the second carrying nothing. 1.1.1 therefore still fails on a real re-scan, the lane
    withholds the credit, and the reviewer's description is never marked applied — silently.

    If a future change makes 'part#rId' reach every placement, this test goes red, and the honest
    response is to delete it rather than to keep the name-based translation on its account.
    """
    import re
    import apply_alt
    data = _docx()
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        rels = z.read("word/_rels/document.xml.rels").decode("utf-8")
    rid = re.search(r'Id="([^"]+)"[^>]*Target="[^"]*media/', rels)
    rid = rid.group(1) if rid else re.search(
        r'<Relationship\b(?=[^>]*media/)[^>]*\bId="([^"]+)"', rels).group(1)

    out, applied, unresolved = apply_alt.apply_alt_text(
        data, {f"word/document.xml#{rid}": DESC_CARD})
    assert not unresolved and len(applied) == 1        # it resolves — to ONE of the two
    assert _docx_descrs(out) == [DESC_CARD, ""]
    assert "1.1.1" in _assess(out, "keepme.docx"), (
        "a half-described document still fails 1.1.1 — this is the credit the lane would "
        "withhold for a write that was otherwise right")


# ---------------------------------------------------------------- the round trips

@needs_ocr
def test_docx_description_reaches_every_placement_and_1_1_1_clears(store, monkeypatch):
    """The whole lane on Word: decide → write → REAL re-scan → credit."""
    data = _docx()
    locs = _locators(data, "keepme.docx")
    item_id = _seed(store, "keepme.docx", data, locs)
    _decide(store, item_id, monkeypatch, values=[DESC_CARD])

    # The decision created the 1.1.1 obligation, approved and unapplied.
    rows = {r["rule_id"]: r for r in store.list_hitl_queue(scan_id=SID)}
    described = rows[f"1.1.1{store.DESCRIBED_RULE_SUFFIX}"]
    assert described["status"] == "approved" and not described.get("applied")
    assert store.approved_alt_values(SID, "keepme.docx") == {locs[0]: DESC_CARD}
    assert store.count_unapplied_approved_values(SID, "keepme.docx") == 1
    assert store.mark_file_compliant_if_reviewed(SID, "keepme.docx") is False

    blob = _Blob(data)
    _run_lane(monkeypatch, store, blob, "keepme.docx")

    # EVERY placement carries the description — not just the first.
    assert _docx_descrs(blob.data) == [DESC_CARD, DESC_CARD]
    # The picture STAYED. This is the reviewer's actual decision, and the one thing that
    # distinguishes this lane from the replacement lane.
    assert _media(blob.data) == _media(data)
    assert BODY in _docx_texts(blob.data) and MORE in _docx_texts(blob.data)
    # A real re-scan agrees 1.1.1 has cleared, so the row was credited.
    assert "1.1.1" not in _assess(blob.data, "keepme.docx")
    assert store.get_hitl_item(described["id"])["applied"]
    assert store.count_unapplied_approved_values(SID, "keepme.docx") == 0


@needs_ocr
def test_xlsx_each_description_reaches_its_own_drawing_and_1_1_1_clears(store, monkeypatch):
    """The whole lane on Excel, and the mapping question a one-image workbook cannot ask.

    Two cards, two media parts, two drawing parts — and the sheet, which is what 'image N' is
    furthest from, is not the alt-bearing part at all. Each description must land on ITS picture.
    """
    data = _xlsx()
    locs = _locators(data, "keepme.xlsx")
    item_id = _seed(store, "keepme.xlsx", data, locs)
    _decide(store, item_id, monkeypatch, values=[DESC_CARD, DESC_NOTICE])

    rows = {r["rule_id"]: r for r in store.list_hitl_queue(scan_id=SID)}
    described = rows[f"1.1.1{store.DESCRIBED_RULE_SUFFIX}"]
    assert store.approved_alt_values(SID, "keepme.xlsx") == {locs[0]: DESC_CARD,
                                                             locs[1]: DESC_NOTICE}

    blob = _Blob(data)
    _run_lane(monkeypatch, store, blob, "keepme.xlsx")

    # Right description, right drawing — not both on one, and not swapped.
    assert _xlsx_descrs(blob.data) == {"xl/drawings/drawing1.xml": DESC_CARD,
                                       "xl/drawings/drawing2.xml": DESC_NOTICE}
    assert _media(blob.data) == _media(data)
    assert BODY in _xlsx_texts(blob.data) and MORE in _xlsx_texts(blob.data)
    assert "1.1.1" not in _assess(blob.data, "keepme.xlsx")
    assert store.get_hitl_item(described["id"])["applied"]
    assert store.count_unapplied_approved_values(SID, "keepme.xlsx") == 0


@needs_ocr
@pytest.mark.parametrize("name,builder,descs", [("keepme.docx", _docx, [DESC_CARD]),
                                                ("keepme.xlsx", _xlsx, [DESC_CARD, DESC_NOTICE])])
def test_the_file_still_fails_1_4_5_and_certifies_anyway(store, monkeypatch, name, builder, descs):
    """The split that makes the lane possible, asserted in both directions on both formats.

    1.4.5 still FAILS on the written copy — the raster is right there, by the reviewer's own
    decision — and the file certifies regardless, because that finding was resolved by judgement
    and the only content it owed (the 1.1.1 alt text) was written and verified.

    If a future change credits the description against 1.4.5 instead, the first assertion still
    passes and the second turns red: the row could never be marked applied, so the file would be
    permanently unpublishable. That is the failure this test exists to catch.
    """
    data = builder()
    item_id = _seed(store, name, data, _locators(data, name))
    _decide(store, item_id, monkeypatch, values=descs)
    blob = _Blob(data)
    _run_lane(monkeypatch, store, blob, name)

    assert "1.4.5" in _assess(blob.data, name), "the picture was supposed to STAY"
    assert store.mark_file_compliant_if_reviewed(SID, name) or _already_compliant(store, name)


def _already_compliant(store, name: str) -> bool:
    """mark_file_compliant_if_reviewed is idempotent and returns False once it has fired, so the
    assertion above accepts either 'it certified now' or 'it is already certified'."""
    with store._db.cursor() as cur:
        store._db.execute(cur, "SELECT compliant FROM file_records WHERE scan_id=%s AND file=%s",
                          (SID, name))
        return bool((store._db.fetchone(cur) or {}).get("compliant"))


@needs_ocr
def test_a_partly_described_workbook_is_not_certified(store, monkeypatch):
    """#1761's "an undescribed image contributes nothing", carried through to the DOCUMENT.

    That fix is proved at the store and the route on a pptx row; this is what it means on the
    format where a multi-image card is the NORMAL shape — an xlsx puts each picture in its own
    drawing part, so a workbook of several images of text is one row with several slots.

    The reviewer describes one and leaves the other blank. The blank must not become that
    picture's own OCR transcript (the defect #1761 closed), so the workbook still has an
    undescribed image, 1.1.1 still fails on a REAL re-scan, and the lane withholds the credit.

    AND THE PARTIAL WRITE IS NOT PUBLISHED AT ALL — measured here, and it is the part that is not
    obvious. _apply_one_value_kind returns `working` (the bytes as they were BEFORE the lane)
    whenever the re-scan does not clear the criterion, so the reviewer's one good description is
    discarded along with the write rather than shipped in a copy that still fails. The approved
    value is kept on the row for a retry, so nothing the reviewer typed is lost — it simply does
    not reach the document until the whole card is answered.
    """
    data = _xlsx()
    locs = _locators(data, "keepme.xlsx")
    item_id = _seed(store, "keepme.xlsx", data, locs)
    _decide(store, item_id, monkeypatch, values=[DESC_CARD, ""])

    # Only what the reviewer actually wrote — no transcript filed under their name.
    assert store.approved_alt_values(SID, "keepme.xlsx") == {locs[0]: DESC_CARD}

    blob = _Blob(data)
    _run_lane(monkeypatch, store, blob, "keepme.xlsx")

    # Nothing published: an unverified copy is not a deliverable.
    assert blob.uploads == []
    assert blob.data == data
    descrs = _xlsx_descrs(blob.data)
    assert " ".join(NOTICE) not in descrs["xl/drawings/drawing2.xml"]   # not its own transcript
    assert "1.1.1" in _assess(blob.data, "keepme.xlsx"), "one image is still undescribed"
    # The approved description survives for a retry rather than being thrown away with the write.
    assert store.approved_alt_values(SID, "keepme.xlsx") == {locs[0]: DESC_CARD}
    assert store.mark_file_compliant_if_reviewed(SID, "keepme.xlsx") is False


# ---------------------------------------------------------------- where the lane STOPS

@functools.lru_cache(maxsize=None)
def _docx_header_only() -> bytes:
    """The image of text lives ONLY in the page header — not in the document body."""
    import docx
    from docx.shared import Inches
    doc = docx.Document()
    doc.add_paragraph(BODY)
    doc.sections[0].header.paragraphs[0].add_run().add_picture(str(_png(CARD)), width=Inches(3))
    out = Path(tempfile.mkdtemp()) / "hdr.docx"
    doc.save(out)
    return out.read_bytes()


@functools.lru_cache(maxsize=None)
def _docx_unreachable_only() -> bytes:
    """A raster in word/media that NO alt-bearing part references.

    This is what a footnote image, a VML header graphic, or a chart's own picture presents to
    ocr._ooxml_images, which walks the ZIP NAMELIST and never opens a part. Built by injecting the
    media entry rather than by authoring one, because every authoring library reachable from here
    writes a relationship as well — and the relationship is precisely what this shape lacks.
    """
    import docx
    doc = docx.Document()
    doc.add_paragraph(BODY)
    base = Path(tempfile.mkdtemp()) / "b.docx"
    doc.save(base)
    buf = io.BytesIO()
    with zipfile.ZipFile(base) as zin, zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zout:
        for info in zin.infolist():
            zout.writestr(info, zin.read(info.filename))
        zout.writestr("word/media/image1.png", _png(CARD).read_bytes())
    return buf.getvalue()


@needs_ocr
def test_a_header_image_is_reached(store, monkeypatch):
    """Word HEADERS and FOOTERS are in the alt-bearing table, so the lane reaches them.

    Worth pinning because the pptx lane's equivalent — an image referenced only by a slideLayout
    or slideMaster — is NOT reachable, and the difference is a fact about ALT_TARGETS rather than
    about this module. A reader who assumed the formats behave alike would get this backwards.
    """
    import apply_office_image_of_text as aoit
    data = _docx_header_only()
    assert _locators(data, "keepme.docx") == ["image 1"]
    assert aoit.resolve_media_locators(data, ["image 1"], "docx") == {
        "image 1": ["word/header1.xml#Picture 1"]}

    item_id = _seed(store, "hdr.docx", data, ["image 1"])
    _decide(store, item_id, monkeypatch, values=[DESC_CARD])
    blob = _Blob(data)
    _run_lane(monkeypatch, store, blob, "hdr.docx")
    assert "1.1.1" not in _assess(blob.data, "hdr.docx")
    assert store.count_unapplied_approved_values(SID, "hdr.docx") == 0


@needs_ocr
def test_an_unreachable_image_wedges_the_file_rather_than_certifying_it_falsely(store, monkeypatch):
    """THE RESIDUAL DEAD END, PINNED RATHER THAN CLAIMED FIXED — measured, not inferred.

    ocr._ooxml_images walks the zip namelist, so a media part NOTHING references still raises
    1.4.5 and still gets an 'image N' card. The 1.1.1 detector reads only the alt-bearing parts
    (formats.office.images.ALT_TARGETS), so that image has no alt-bearing element for a
    description to land on — and this resolver correctly declines to invent one.

    What follows is a dead end: the described row is approved and can never be applied, so
    count_unapplied_approved_values counts it forever and the file can never certify. The
    direction is SAFE — no false certification, which is the failure ADR 0055 exists to prevent.

    IT IS NO LONGER SILENT, which was the half that actually hurt. The lane now logs
    apply.unverified when it writes nothing because every locator was unresolved, and
    apply_outcome reads that back as NOTHING_WRITTEN, so the reviewer gets a card saying the
    description reached no image instead of a file that never publishes and no explanation. That
    is asserted below, because a wedge nobody can see is a different defect from a wedge.

    The pptx half of this shape — a picture on a slideLayout or slideMaster — is now REACHED
    rather than merely visible (formats.office.images.ALT_TARGETS covers the inherited parts, and
    tests/test_described_image_inherited_and_unreachable.py proves the round trip). What is left
    here is the residual: a media part no alt-bearing part references at all, which no locator
    scheme can address. Extending reach further would mean widening ALT_TARGETS to parts that
    genuinely carry no alt-bearing element, which would break the detector/applier agreement that
    module exists to keep.
    """
    import apply_office_image_of_text as aoit
    data = _docx_unreachable_only()
    from formats.office.images import package_images
    assert _locators(data, "orphan.docx") == ["image 1"]          # 1.4.5 cards it…
    assert package_images(_spill(data, "orphan.docx")) == []      # …1.1.1 cannot see it
    assert aoit.resolve_media_locators(data, ["image 1"], "docx") == {}

    item_id = _seed(store, "orphan.docx", data, ["image 1"])
    _decide(store, item_id, monkeypatch, values=[DESC_CARD])
    blob = _Blob(data)
    _run_lane(monkeypatch, store, blob, "orphan.docx")

    assert blob.uploads == [], "nothing was written, so nothing should have been published"
    assert blob.data == data
    rows = {r["rule_id"]: r for r in store.list_hitl_queue(scan_id=SID)}
    assert not rows[f"1.1.1{store.DESCRIBED_RULE_SUFFIX}"].get("applied")
    assert store.count_unapplied_approved_values(SID, "orphan.docx") == 1
    # The half that matters: it does NOT certify. A wedge is recoverable; a false pass is not.
    assert store.mark_file_compliant_if_reviewed(SID, "orphan.docx") is False

    # And the reviewer is TOLD, rather than left with a file that silently never publishes.
    import apply_outcome
    row = rows[f"1.1.1{store.DESCRIBED_RULE_SUFFIX}"]
    outcome = apply_outcome.apply_outcome_for(row, store.list_decisions(scan_id=SID))
    assert outcome and outcome["outcome"] == apply_outcome.NOTHING_WRITTEN
    assert outcome["criteria"] == ["1.1.1"]


@needs_ocr
def test_the_transcript_is_never_written_as_the_description(store, monkeypatch):
    """No proposed_value fallback on a described row, on Word as on PowerPoint.

    Everywhere else a reviewer who edited nothing has agreed to the draft they were shown. Here
    the draft is the OCR TRANSCRIPT, and a transcript is not a description — falling back would
    write the picture's own words as its alt text, silently, on the one path whose entire premise
    is that the picture stays.
    """
    from fastapi import HTTPException
    data = _docx()
    item_id = _seed(store, "keepme.docx", data, _locators(data, "keepme.docx"))
    with pytest.raises(HTTPException) as e:
        _decide(store, item_id, monkeypatch, values=["   "])
    assert e.value.status_code == 422
    assert store.get_hitl_item(item_id)["status"] == "pending"        # nothing was written

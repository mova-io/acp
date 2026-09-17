"""DOCX (and other OOXML) document-context packaging.

Extracts page text plus the one structural fact its allowlisted operation needs: images
still missing alt text (WCAG 1.1.1), via `formats.office.images.undescribed_images` —
the exact same pure walk the production 1.1.1 detector and `apply_alt.apply_alt_text`
share, so a locator minted here is guaranteed to resolve at application time.
"""
from __future__ import annotations

import zipfile
from dataclasses import dataclass, field
from io import BytesIO

import docx

from experiments.document_wide_ai.application.production_adapters import office_undescribed_images
from experiments.document_wide_ai.contracts.v1 import ExtractionIssue, sha256_hex

EXTRACTOR_VERSION = "docx-extractor.v1"


@dataclass(frozen=True)
class DocxImage:
    locator: str  # "{part}#{docPr name | r:embed id}"
    part_name: str
    name: str


@dataclass(frozen=True)
class PackagedDocx:
    extractor_version: str
    paragraph_count: int
    text_context: str
    undescribed_images: tuple[DocxImage, ...]
    extraction_issues: tuple[ExtractionIssue, ...] = field(default_factory=tuple)

    def image_by_locator(self, locator: str) -> DocxImage | None:
        for img in self.undescribed_images:
            if img.locator == locator:
                return img
        return None


def fingerprint_missing() -> str:
    """Every undescribed image starts from the same precondition: no alt text yet."""
    return sha256_hex(b"<missing>")


def unnamed_docx_images(entries):
    """Use a unique relationship only when the existing writer resolves that exact drawing."""
    import re
    from lxml import etree
    from apply_alt import resolve_target, tag_for_part, _alt_elements
    from formats.office.images import is_junk_descr, is_decorative
    ns = {'wp': 'http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing',
          'w': 'http://schemas.openxmlformats.org/wordprocessingml/2006/main',
          'a': 'http://schemas.openxmlformats.org/drawingml/2006/main',
          'r': 'http://schemas.openxmlformats.org/officeDocument/2006/relationships'}
    images = []
    for part, raw in entries.items():
        if not re.fullmatch(r'word/(?:document|header\d*|footer\d*|footnotes|endnotes)\.xml', part):
            continue
        xml = raw.decode('utf-8')
        root = etree.fromstring(raw, etree.XMLParser(resolve_entities=False, no_network=True))
        props = root.findall('.//wp:docPr', ns)
        matches = _alt_elements(xml, tag_for_part(part))
        if len(matches) != len(props):
            continue  # namespace/prefix shape the production writer cannot address
        all_rids = [b.get('{'+ns['r']+'}embed') for b in root.findall('.//a:blip', ns)]
        names = {p.get('name', '').strip() for p in props}
        for index, prop in enumerate(props):
            if prop.get('name', '').strip() or not is_junk_descr(prop.get('descr', '')):
                continue
            if is_decorative(etree.tostring(prop, encoding='unicode')):
                continue
            drawing = next((a for a in prop.iterancestors() if a.tag == '{'+ns['w']+'}drawing'), None)
            if drawing is None or len(drawing.findall('.//wp:docPr', ns)) != 1:
                continue
            blips = drawing.findall('.//a:blip', ns)
            rid = blips[0].get('{'+ns['r']+'}embed') if len(blips) == 1 else None
            if not rid or all_rids.count(rid) != 1 or rid in names:
                continue
            if resolve_target(xml, tag_for_part(part), rid) != matches[index].start():
                continue
            images.append(DocxImage(part+'#'+rid, part, rid))
    return images


def package_docx(source_bytes: bytes, *, max_text_chars: int) -> PackagedDocx:
    issues: list[ExtractionIssue] = []
    text_parts: list[str] = []
    paragraph_count = 0

    try:
        d = docx.Document(BytesIO(source_bytes))
        for p in d.paragraphs:
            if p.text:
                text_parts.append(p.text)
        paragraph_count = len(d.paragraphs)
        for table in d.tables:
            for row in table.rows:
                for cell in row.cells:
                    if cell.text:
                        text_parts.append(cell.text)
    except Exception as exc:
        issues.append(ExtractionIssue(kind="extraction_failed", detail=f"python-docx open failed: {exc}"))

    text_context = "\n".join(text_parts)
    if len(text_context) > max_text_chars:
        text_context = text_context[:max_text_chars]
        issues.append(
            ExtractionIssue(kind="extraction_truncated", detail=f"text truncated to {max_text_chars} chars")
        )

    images: list[DocxImage] = []
    try:
        with zipfile.ZipFile(BytesIO(source_bytes)) as zf:
            entries = {n: zf.read(n) for n in zf.namelist()}
        for rec in office_undescribed_images(entries):
            images.append(DocxImage(locator=rec["locator"], part_name=rec["part"], name=rec["name"]))
        images.extend(unnamed_docx_images(entries))
    except Exception as exc:
        issues.append(ExtractionIssue(kind="extraction_failed", detail=f"image extraction failed: {exc}"))

    return PackagedDocx(
        extractor_version=EXTRACTOR_VERSION,
        paragraph_count=paragraph_count,
        text_context=text_context,
        undescribed_images=tuple(images),
        extraction_issues=tuple(issues),
    )

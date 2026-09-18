"""A finding's LOCATION, as a person reads it — the one parser for every report and viewer.

Contract 1 of the report follow-up. Detectors record a machine locator (`docx:paragraph:14`,
`pptx:slide:2:element:5`, `xlsx:sheet:Budget:cell:B3`, `pdf:fig:3:0`) and, separately, a 1-based
page/slide number. Before this module the report facts copied the machine string into `label`
verbatim, so a reviewer read "docx:paragraph:14" — and PPTX elements, XLSX sheets and cells were
dropped altogether (audit gap L2).

`frontend/src/reportEvidence.js` `locationOf()` parses the SAME strings into the SAME object with
the SAME labels. Both sides are pinned by one table-driven fixture,
`tests/fixtures/report_location_cases.json`, read by `tests/test_report_location.py` and by
`frontend/src/reportLocation.test.js`. Change one side and that fixture fails on the other.

Rules this module keeps, each of which was broken somewhere:

* NO PAGE-1 DEFAULT. `page` is set only from a recorded page, never assumed.
* `page` means a RENDERED page: a PDF page, or the slide number of a PPTX (a slide renders as one
  page). A Word paragraph has no page — Word paginates at layout time, and nothing the analyser
  records says where a paragraph falls — so a DOCX location never carries one, even when the
  issue row does. Excel cells are not pages either.
* `pptx:slide:{i}` is 0-based (LocationHelper.FromSlide(slideIndex)), while the analyser's
  `SlideNumber`/`page` is `slideIndex + 1`. The 1-based page is preferred; with only the string
  the slide is `i + 1`.
* Word paragraph and table indices are 0-based in the analyser (`for (int paraIdx = 0; …)`), so
  the human label is `index + 1`, while `objectId` keeps the machine index so it can be matched
  back to the locator.
* An unknown format keeps `raw` verbatim — never dropped. Its label is built from it only when
  the raw text is already human ("image 1", "document headings"): a machine string beside a
  recorded page is labelled by the page alone ("Page 7"), with the string kept in `raw`, rather
  than glued into the label ("Page 7 · /Document/Sect[2]/Figure").
* The locators ACP's own Office writers record (R1: remediation_diff.locator) are parsed too —
  `word:p:N` / `word:p:N:run:M` / `word:table:N:row:M` (1-based OOXML document order, NOT the
  analyser's 0-based `docx:paragraph:i`), `word:document:outline`, and OOXML part locators
  (`ppt/slides/slide7.xml`, `word/document.xml#Revenue chart`, `xl/tables/table3.xml`). None of
  them ever carries a page: a Word page is a layout artefact, and a slide PART name is not a slide
  number (reading order lives in presentation.xml), so `slide7.xml` is never called "Slide 7".
"""
from __future__ import annotations

import re

_POS_INT = re.compile(r"^[0-9]+$")
_A1 = re.compile(r"^\$?([A-Za-z]{1,3})\$?([0-9]+)$")
_SHEET_BANG = re.compile(r"^'?([^'!:/#]+)'?!\$?([A-Za-z]{1,3})\$?([0-9]+)")
_SHEET_EQ = re.compile(r"sheet[:=]([^:!/#]+)", re.I)
_CELL_EQ = re.compile(r"cell[:=]\$?([A-Za-z]{1,3})\$?([0-9]+)", re.I)

FORMATS = ("pdf", "docx", "pptx", "xlsx", "html")
# Formats whose `page` is not a rendered page a reader can open to.
_NO_PAGE_FORMATS = ("docx", "xlsx")


def _pos_int(value) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    text = str(value if value is not None else "").strip()
    return int(text) if _POS_INT.match(text) and int(text) > 0 else None


def _index(text) -> int | None:
    """A 0-based machine index, or None when the text is not one."""
    text = str(text if text is not None else "").strip()
    return int(text) if _POS_INT.match(text) else None


def fmt_of_name(name) -> str | None:
    ext = str(name or "").rsplit(".", 1)[-1].lower() if "." in str(name or "") else ""
    if ext in ("htm", "html"):
        return "html"
    return ext if ext in FORMATS else None


def _loc(label, kind, *, page=None, slide=None, sheet=None, cell=None, object_id=None,
         raw=None) -> dict:
    return {"label": label or None, "kind": kind, "page": page, "slide": slide,
            "sheet": sheet, "cell": cell, "objectId": object_id,
            # `element` is the legacy key: the raw locator, kept so older consumers still work.
            "element": raw, "raw": raw}


def _pdf(raw: str, page: int | None) -> dict | None:
    m = re.match(r"^pdf:(fig|field):(\?|[0-9]+):([0-9]+)$", raw)
    if m:
        kind_word = "figure" if m.group(1) == "fig" else "form field"
        stated = _pos_int(m.group(2))
        page = page or stated
        seq = int(m.group(3))
        oid = f"{'figure' if m.group(1) == 'fig' else 'field'}:{m.group(2)}:{seq}"
        label = (f"Page {page} · {kind_word} {seq + 1}" if page
                 else f"{kind_word.capitalize()} {seq + 1} (page not recorded)")
        return _loc(label, "object", page=page, object_id=oid, raw=raw)
    m = re.match(r"^pdf:struct:([0-9]+(?:\.[0-9]+)*)(?::[0-9a-f]+)?$", raw)
    if m:
        prefix = f"Page {page} · " if page else ""
        return _loc(f"{prefix}structure element {m.group(1)}", "object", page=page,
                    object_id=f"struct:{m.group(1)}", raw=raw)
    if re.match(r"^pdf:(lang|title|producer|metadata)\b", raw, re.I):
        return _loc("Document-wide", "document", raw=raw)
    return None


def _docx(raw: str) -> dict | None:
    m = re.match(r"^docx:paragraph:([0-9]+)$", raw)
    if m:
        i = int(m.group(1))
        return _loc(f"Paragraph {i + 1}", "paragraph", object_id=f"paragraph:{i}", raw=raw)
    m = re.match(r"^docx:drawing:(.+?):paragraph:([0-9]+)$", raw)
    if m:
        i = int(m.group(2))
        return _loc(f"Image (drawing id {m.group(1)}) · paragraph {i + 1}", "object",
                    object_id=f"drawing:{m.group(1)}", raw=raw)
    m = re.match(r"^docx:image:([0-9]+)$", raw)
    if m:
        # An ACP-minted image ordinal (remediate_office), already 1-based.
        return _loc(f"Image {int(m.group(1))}", "object", object_id=f"image:{int(m.group(1))}",
                    raw=raw)
    m = re.match(r"^docx:table:([0-9]+)(?::row:([0-9]+))?$", raw)
    if m:
        t = int(m.group(1))
        row = f" · row {int(m.group(2)) + 1}" if m.group(2) is not None else ""
        return _loc(f"Table {t + 1}{row}", "object", object_id=f"table:{t}", raw=raw)
    m = re.match(r"^docx:hyperlink:paragraph:([0-9]+)(?::url:.*)?$", raw, re.S)
    if m:
        i = int(m.group(1))
        return _loc(f"Link in paragraph {i + 1}", "paragraph", object_id=f"paragraph:{i}",
                    raw=raw)
    m = re.match(r"^docx:sdt:#?(.+)$", raw)
    if m:
        return _loc(f"Content control {m.group(1)}", "object", object_id=f"sdt:{m.group(1)}",
                    raw=raw)
    m = re.match(r"^docx:document-properties:(.+)$", raw)
    if m:
        return _loc(f"Document-wide ({m.group(1)} property)", "document", raw=raw)
    if raw.startswith("docx:document"):
        return _loc("Document-wide", "document", raw=raw)
    return None


def _pptx(raw: str, page: int | None) -> dict | None:
    m = re.match(r"^pptx:slide:([0-9]+)(?::(element|table):(.+))?$", raw)
    if not m:
        return None
    slide = page or (int(m.group(1)) + 1)
    if m.group(2) == "element":
        return _loc(f"Slide {slide} · shape {m.group(3)}", "object", page=slide, slide=slide,
                    object_id=f"shape:{m.group(3)}", raw=raw)
    if m.group(2) == "table":
        t = _index(m.group(3))
        shown = t + 1 if t is not None else m.group(3)
        return _loc(f"Slide {slide} · table {shown}", "object", page=slide, slide=slide,
                    object_id=f"table:{m.group(3)}", raw=raw)
    return _loc(f"Slide {slide}", "slide", page=slide, slide=slide, raw=raw)


def _xlsx(raw: str) -> dict | None:
    if raw == "xlsx:document":
        return _loc("Document-wide", "document", raw=raw)
    # Excel forbids ':' in a sheet name, so the name is everything up to the next ':'.
    m = re.match(r"^xlsx:sheet:([^:]+)(?::(cell|drawing|table|row|col):(.+))?$", raw)
    if not m:
        return None
    sheet, part, value = m.group(1), m.group(2), m.group(3)
    if part is None:
        return _loc(f"Sheet {sheet}", "sheet", sheet=sheet, raw=raw)
    if part == "cell":
        a1 = _A1.match(value)
        cell = f"{a1.group(1).upper()}{a1.group(2)}" if a1 else value
        return _loc(f"Sheet {sheet} · cell {cell}", "cell", sheet=sheet, cell=cell, raw=raw)
    word = {"drawing": "drawing", "table": "table", "row": "row", "col": "column"}[part]
    oid = {"col": "column"}.get(part, part)
    return _loc(f"Sheet {sheet} · {word} {value}", "object", sheet=sheet,
                object_id=f"{oid}:{value}", raw=raw)


def _word_token(raw: str) -> dict | None:
    """remediate_office's structural tokens: 1-based OOXML document order (paragraphs nested in
    tables included), so the number is shown as recorded — no +1."""
    m = re.match(r"^word:p:([1-9][0-9]*)(?::run:([1-9][0-9]*))?$", raw)
    if m:
        run = f" · text run {int(m.group(2))}" if m.group(2) else ""
        return _loc(f"Paragraph {int(m.group(1))}{run}", "paragraph", object_id=raw, raw=raw)
    m = re.match(r"^word:table:([1-9][0-9]*)(?::row:([1-9][0-9]*))?$", raw)
    if m:
        row = f" · row {int(m.group(2))}" if m.group(2) else ""
        return _loc(f"Table {int(m.group(1))}{row}", "object", object_id=raw, raw=raw)
    if raw == "word:document:outline":
        return _loc("Document-wide (heading outline)", "document", raw=raw)
    return None


_OOXML_PART = re.compile(r"^((?:word|ppt|xl)/[^#\s][^#]*?\.xml)(?:#(.+))?$", re.S)
_OOXML_PART_NAMES = (
    (re.compile(r"^word/document\.xml$"), lambda m: "document body"),
    (re.compile(r"^word/header([0-9]*)\.xml$"), lambda m: f"header {m.group(1)}".strip()),
    (re.compile(r"^word/footer([0-9]*)\.xml$"), lambda m: f"footer {m.group(1)}".strip()),
    (re.compile(r"^word/(footnotes|endnotes|comments)\.xml$"), lambda m: m.group(1)),
    # A slide PART, not a slide number: presentation.xml decides the order, so the file name is
    # stated as a file name.
    (re.compile(r"^ppt/slides/(slide[0-9]+\.xml)$"), lambda m: f"slide file {m.group(1)}"),
    (re.compile(r"^ppt/slideLayouts/slideLayout([0-9]+)\.xml$"), lambda m: f"slide layout {m.group(1)}"),
    (re.compile(r"^ppt/slideMasters/slideMaster([0-9]+)\.xml$"), lambda m: f"slide master {m.group(1)}"),
    (re.compile(r"^ppt/notesSlides/(notesSlide[0-9]+\.xml)$"), lambda m: f"speaker notes file {m.group(1)}"),
    (re.compile(r"^xl/tables/(table[0-9]+\.xml)$"), lambda m: f"Excel table file {m.group(1)}"),
    (re.compile(r"^xl/drawings/(drawing[0-9]+\.xml)$"), lambda m: f"drawing layer file {m.group(1)}"),
    (re.compile(r"^xl/worksheets/(sheet[0-9]+\.xml)$"), lambda m: f"worksheet file {m.group(1)}"),
)


def _ooxml_part(raw: str) -> dict | None:
    """`part` or `part#fragment` (apply_alt.parse_locator: split on the FIRST '#'). The fragment is
    the element's own name, or the relationship id of the image it describes."""
    m = _OOXML_PART.match(raw)
    if not m:
        return None
    part, fragment = m.group(1), (m.group(2) or "").strip() or None
    where = next((name(pm) for pat, name in _OOXML_PART_NAMES for pm in [pat.match(part)] if pm),
                 None) or f"part {part}"
    if fragment is None:
        return _loc(where[:1].upper() + where[1:], "object", object_id=raw, raw=raw)
    what = (f"Image (relationship {fragment})" if re.fullmatch(r"rId[0-9]+", fragment)
            else f"Object “{fragment}”")
    return _loc(f"{what} · {where}", "object", object_id=raw, raw=raw)


# Text a reader can take as it stands: starts with a letter, holds only letters, digits, spaces
# and ordinary punctuation, and — when it has digits — is more than one word ("image 1", not
# "rId5"). Anything else is a machine string, which is kept in `raw` but not put in a label.
_HUMAN = re.compile(r"^[^\W\d_](?:[^\W_]|[ .,'’()\-–])*$")


def _is_human(raw: str) -> bool:
    return bool(_HUMAN.match(raw)) and (" " in raw or not re.search(r"\d", raw))


def _legacy_sheet_cell(raw: str) -> dict | None:
    m = _SHEET_BANG.match(raw)
    if m:
        sheet, cell = m.group(1), f"{m.group(2).upper()}{m.group(3)}"
        return _loc(f"Sheet {sheet} · cell {cell}", "cell", sheet=sheet, cell=cell, raw=raw)
    sheet_m, cell_m = _SHEET_EQ.search(raw), _CELL_EQ.search(raw)
    if sheet_m and cell_m:
        sheet, cell = sheet_m.group(1), f"{cell_m.group(1).upper()}{cell_m.group(2)}"
        return _loc(f"Sheet {sheet} · cell {cell}", "cell", sheet=sheet, cell=cell, raw=raw)
    if sheet_m:
        return _loc(f"Sheet {sheet_m.group(1)}", "sheet", sheet=sheet_m.group(1), raw=raw)
    return None


def parse_location(raw, page=None, fmt=None) -> dict | None:
    """The structured location for a detector's `location` string and recorded `page`.

    Returns None when nothing was recorded. Never a page-1 default.
    """
    raw = str(raw).strip() if raw is not None else ""
    raw = raw or None
    page = _pos_int(page)
    prefix = raw.split(":", 1)[0].lower() if raw and ":" in raw else None
    fmt = (prefix if prefix in FORMATS else None) or (str(fmt).lower() if fmt else None)
    if fmt in _NO_PAGE_FORMATS:
        page = None
    if raw is None:
        if page is None:
            return None
        if fmt == "pptx":
            return _loc(f"Slide {page}", "slide", page=page, slide=page)
        return _loc(f"Page {page}", "page", page=page)
    parsed = None
    if prefix == "pdf":
        parsed = _pdf(raw, page)
    elif prefix == "docx":
        parsed = _docx(raw)
    elif prefix == "pptx":
        parsed = _pptx(raw, page)
    elif prefix == "xlsx":
        parsed = _xlsx(raw)
    elif prefix == "word":
        parsed = _word_token(raw)
    if parsed is None:
        # ACP's own Office writer locators. Never a page, whatever the row carried.
        parsed = _ooxml_part(raw)
    if parsed is None:
        parsed = _legacy_sheet_cell(raw)
    if parsed is not None:
        return parsed
    # Unknown format: `raw` is kept verbatim. It is put in the label only when it already reads
    # as words; a machine string beside a page is labelled by the page alone.
    if page is not None and raw.casefold() in (f"slide {page}", f"page {page}"):
        raw_is_page = True       # "Slide 3" beside page 3 says nothing twice
    else:
        raw_is_page = False
    tail = f" · {raw}" if (not raw_is_page and _is_human(raw)) else ""
    if page is not None and fmt == "pptx":
        return _loc(f"Slide {page}{tail}", "slide", page=page, slide=page, raw=raw)
    if page is not None:
        return _loc(f"Page {page}{tail}", "page", page=page, raw=raw)
    # Nothing else to name the place by: the recorded string is the only location there is, so
    # it is the label (kind None tells a renderer it is unparsed).
    return _loc(raw, None, raw=raw)


LOCATION_RECORDED = "recorded"
LOCATION_LEGACY_NOTE = "legacy_note"
# Appended to the label of a location the store reconstructed from a writer's note (R1
# `location_source: "legacy_note"`), so no renderer can present it as a stored location.
LEGACY_NOTE_QUALIFIER = " (from the saving step's note, not a recorded location)"


def saved_change_location(locator, page, fmt, source) -> dict | None:
    """The contract-1 location of a SAVED change, with `source` saying where it came from.

    `source` is the store's R1 `location_source`: "recorded" (the writer stored it), "legacy_note"
    (reconstructed at read time from an exact writer-note prefix; never a page) or None (unknown
    → None, "Location not recorded"). Anything else is treated as unknown.
    """
    if source == LOCATION_RECORDED:
        loc = parse_location(locator, page, fmt)
    elif source == LOCATION_LEGACY_NOTE:
        loc = parse_location(locator, None, fmt)
        if loc is not None:
            # No page, ever — not even one a locator's own text states (`pdf:fig:2:0`): nothing
            # recorded it, so no preview may present it as the exact page. The label keeps the
            # writer's words, flagged.
            loc["page"] = loc["slide"] = None
            loc["label"] = f"{loc['label'] or locator}{LEGACY_NOTE_QUALIFIER}"
    else:
        return None
    if loc is not None:
        loc["source"] = source
    return loc


def location_of_issue(issue: dict, fmt=None) -> dict | None:
    """`parse_location` for an issue_records row ({location, page})."""
    issue = issue or {}
    return parse_location(issue.get("location"), issue.get("page"), fmt)

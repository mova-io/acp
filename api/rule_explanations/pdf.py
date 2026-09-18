"""What ACP actually checks on a PDF, per WCAG success criterion — read off the current code.

Every statement here names the detector that makes it true. Numbers are never typed: each
Threshold is built by `threshold()` below from the constant (or, where the code holds an inline
literal, from that literal checked against the function's source), and the drift test in
tests/test_rule_explanations_pdf_html.py re-derives every one of them. A number that stops
matching the code fails that test instead of silently lying to an admin.

Where the checks live (PDF):
  * engine/pdf-analyser/analysers/rules/pdf/*   — the vendored PDF engine (ADR 0029), run by
    scanner._analyse_pdf; rule ids look like "pdf.document-title".
  * api/office_structure.py `checks_for(".pdf")` — first-party structure/contrast/link checks.
  * api/formats/pdf/detectors/*                 — registry-migrated first-party detectors.
  * api/textchecks.py, api/ocr.py, api/pdf_structural_language.py — text, OCR and tagged
    language checks that run on every PDF the scanner reads.

assessment_lane / remediation_lane are deliberately left None: the assembler overlays them from
remediation_capability so this file cannot disagree with the capability map.

The helpers at the top are shared with html.py (same package, same owner).
"""
from __future__ import annotations

import ast
import importlib
import inspect
import os
from pathlib import Path
from typing import Any, Callable

from .schema import FormatExplanation, RuleRef, Threshold

# ── threshold derivation (shared with html.py) ─────────────────────────────────────────────
#
# (source, label, value) -> how the value was derived, so the drift test can recompute it. Filled
# as a side effect of building the catalog; the test builds the catalog first. The value is part
# of the key because one function can yield two thresholds under one label (AA and AAA bars).
DERIVATIONS: dict[tuple[str, str, str], dict[str, Any]] = {}

_REPO = Path(__file__).resolve().parents[2]


def pdf_engine_root() -> Path:
    """The vendored PDF engine root — the same resolution as scanner.WP (the test pins them)."""
    return Path(os.environ.get("ACP_PDF_ENGINE") or (_REPO / "engine" / "pdf-analyser"))


def _engine_constant(module: str, name: str):
    """A module-level literal from the vendored engine, read by AST rather than import.

    The engine imports `models.manifest` from its own root, which is only on sys.path while a
    scan runs. Reading the literal avoids mutating sys.path to render a settings page; the test
    imports the real module and checks the two agree.
    """
    path = pdf_engine_root().joinpath(*module.split(".")).with_suffix(".py")
    tree = ast.parse(path.read_text())
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == name
                                                for t in node.targets):
            return ast.literal_eval(node.value)
    raise LookupError(f"{module}:{name} not found in {path}")


def resolve(source: str):
    """"module:NAME" -> the object. Engine modules ("analysers.*") are read by AST."""
    module, name = source.split(":", 1)
    if module.startswith("analysers."):
        return _engine_constant(module, name)
    obj = importlib.import_module(module)
    for part in name.split("."):
        obj = getattr(obj, part)
    return obj


def threshold(label: str, source: str, fmt: Callable[[Any], str], *, literal: str | None = None,
              standard: bool = False, configurable: bool = False,
              setting: str | None = None) -> Threshold:
    """Build a Threshold whose value is DERIVED, never typed.

    Without `literal`: value = fmt(resolve(source)) — a constant, or a function probed by `fmt`.
    With `literal`: the code holds the number inline inside the function `source` names (no
    constant exists to import). value = fmt(float(literal)), and the test asserts the literal
    token still appears in that function's source. These are the ones that must be extracted
    into a constant before they could ever be made configurable.
    """
    obj = resolve(source)
    if literal is not None:
        value = fmt(float(literal))
    else:
        value = fmt(obj)
    DERIVATIONS[(source, label, value)] = {"fmt": fmt, "literal": literal}
    return Threshold(label=label, value=value, source=source, configurable=configurable,
                     setting=setting, standard=standard)


# Formatters. Kept tiny and pure so the test can re-run them.
def ratio(v) -> str:
    return f"{float(v):g}:1"


def pt(v) -> str:
    return f"{float(v):g}pt"


def px(v) -> str:
    return f"{float(v):g}px"


def pct(v) -> str:
    return f"{float(v) * 100:g}%"


def times_font(v) -> str:
    return f"{float(v):g}× the font size"


def count(unit: str) -> Callable[[Any], str]:
    def _fmt(v) -> str:
        return f"{float(v):g} {unit}"
    return _fmt


def hex_colour(v) -> str:
    return f"#{str(v).upper()}"


def phrases(v) -> str:
    items = sorted(v)
    return f"{len(items)} phrases: " + ", ".join(f"“{p}”" for p in items)


def _aa_ratios(size: str, bold: bool = False) -> Callable[[Any], str]:
    """Probe office_structure._pdf_required_ratios with a synthetic glyph -> the AA bar."""
    def _fmt(fn) -> str:
        return ratio(fn({"size": size, "fontname": "X-Bold" if bold else "X"})[0])
    return _fmt


def _aaa_ratios(size: str, bold: bool = False) -> Callable[[Any], str]:
    def _fmt(fn) -> str:
        return ratio(fn({"size": size, "fontname": "X-Bold" if bold else "X"})[1])
    return _fmt


def _param_default(param: str, fmt: Callable[[Any], str]) -> Callable[[Any], str]:
    """A function's keyword default (e.g. pii._pdf_text max_pages) — derived by signature."""
    def _fmt(fn) -> str:
        return fmt(inspect.signature(fn).parameters[param].default)
    return _fmt


# Paths, so RuleRef sources are spelled once.
_OS = "api/office_structure.py"
_TXT = "api/textchecks.py"
_OCR = "api/ocr.py"
_DET = "api/formats/pdf/detectors"
_ENG = "engine/pdf-analyser/analysers/rules/pdf"
_ENGINE = "pdf-analyser"


def _eng(rule_id: str, module: str, method: str, fix: str) -> RuleRef:
    return RuleRef(id=rule_id, source=f"{_ENG}/{module}.py", method=method, fix=fix, engine=_ENGINE)


def _page_cap(source: str = "office_structure:_MAX_PAGES_SPACING") -> Threshold:
    return threshold("Pages examined (from the start of the document)", source, count("pages"))


def _text_checks_caps() -> tuple[Threshold, ...]:
    return (
        threshold("Text read from a PDF (pages)", "pii:_pdf_text",
                  _param_default("max_pages", count("pages"))),
        threshold("Text read from a PDF (characters)", "pii:_pdf_text",
                  _param_default("max_chars", count("characters"))),
    )


def pdf() -> dict[str, FormatExplanation]:
    """One FormatExplanation per SC in remediation_capability.remediation_table()["pdf"]."""
    import office_structure as osx

    vague_count = len(osx._VAGUE_LINK_TEXT)
    large = f"{osx._PDF_LARGE_PT:g}pt, or {osx._PDF_LARGE_BOLD_PT:g}pt when the font name says bold"

    out: dict[str, FormatExplanation] = {}

    out["1.1.1"] = FormatExplanation(
        status="partial",
        checks=(
            "Walks the tag tree (/StructTreeRoot) for every /Figure element and flags one that has "
            "no /Alt, or whose /Alt is only whitespace.",
            "Two detectors answer this same question — the vendored PDF engine and a first-party "
            "one the in-process re-scan can see; when the engine reports, the first-party "
            "duplicates are dropped so each figure is counted once.",
        ),
        does_not_check=(
            "Whether an /Alt that is present actually describes the image.",
            "Images in an untagged PDF: with no tag tree there are no figures to judge, and the "
            "missing tags are reported under 1.3.1 (pdf.tagged) instead of twice.",
            "Images that are drawn on the page but never wrapped in a /Figure tag.",
            "Decorative images that should be marked as artifacts rather than described.",
        ),
        evidence="One finding per undescribed tagged figure, with its page number (from /Pg) and "
                 "its position in the tag tree when the engine reports it.",
        fix="Assisted. A vision model drafts alt text from the figure's own pixels; it is written "
            "to /Alt automatically only when the caption is independently validated against the "
            "exact figure raster. Everything else becomes a review card, and the approved text is "
            "written back by apply_pdf_figure_alt.",
        rules=(
            _eng("pdf.missing-alt-text", "image_alt_text", "deterministic", "assisted"),
            RuleRef("PDF_FIGURE_NO_ALT", f"{_DET}/non_text_content.py", "deterministic", "assisted"),
        ),
    )

    out["1.3.1"] = FormatExplanation(
        status="partial",
        checks=(
            "Is the PDF tagged at all: the catalog must carry /MarkInfo /Marked true AND a "
            "/StructTreeRoot.",
            "Does every tagged /Table contain at least one /TH header cell.",
            "For tables that do have /TH cells: does each header carry a valid Table /Scope "
            "(Row, Column or Both), unless every data cell's /Headers already resolves through "
            "unique IDs in the same table.",
        ),
        does_not_check=(
            "Headings, lists, paragraphs or any other tag being the RIGHT tag for its content.",
            "Whether a header's scope is semantically correct — only that one is present.",
            "Structure in untagged PDFs (nothing is reconstructed from the page layout).",
            "Tables whose attributes use arrays or revisions; these are skipped, not judged.",
        ),
        evidence="pdf.tagged: one finding per document. pdf.table-headers: one per table, "
                 "located by its index in the tag tree. PDF_TABLE_HEADER_SCOPE_MISSING: one per "
                 "header cell, located by its structure path.",
        fix="Mostly review. ACP never writes tags into an untagged PDF; for one it proposes a "
            "heading structure map (from font sizes) as a re-authoring instruction only. For an "
            "existing rectangular table whose first row is all /TH, it proposes /Scope Column; a "
            "person approves it and ACP writes it into the existing tag (apply_pdf_structure_"
            "repairs), leaving page content untouched.",
        rules=(
            _eng("pdf.tagged", "tagged_pdf", "deterministic", "review"),
            _eng("pdf.table-headers", "table_headers", "deterministic", "review"),
            RuleRef("PDF_TABLE_HEADER_SCOPE_MISSING", f"{_DET}/table_headers.py",
                    "deterministic", "assisted"),
        ),
    )

    out["1.3.2"] = FormatExplanation(
        status="partial",
        checks=(
            "On an UNTAGGED PDF only: compares the order lines are drawn in the content stream "
            "with their top-to-bottom order on the page, and flags a page where a large share of "
            "line pairs are inverted.",
            "Abstains (reports nothing) on tagged PDFs, multi-column pages, pages with smaller "
            "out-of-flow text such as footnotes or captions, and pages with too few lines.",
        ),
        thresholds=(
            threshold("Inverted line pairs needed to report", "analysers.rules.pdf.reading_order:"
                      "_INVERSION_THRESHOLD", pct),
            threshold("Minimum lines on a page to judge", "analysers.rules.pdf.reading_order:"
                      "_MIN_LINES", count("lines")),
            threshold("Column gap that marks a page multi-column (share of page width)",
                      "analysers.rules.pdf.reading_order:_GUTTER_FRACTION", pct),
            threshold("Line size, relative to the body, treated as out-of-flow text",
                      "analysers.rules.pdf.reading_order:_SMALL_FONT_RATIO", pct),
            threshold("Words this close vertically share a line",
                      "analysers.rules.pdf.reading_order:_LINE_TOLERANCE", pt),
            threshold("Pages examined", "analysers.rules.pdf.reading_order:_MAX_PAGES",
                      count("pages")),
        ),
        does_not_check=(
            "Tagged PDFs — the tag tree defines their reading order and this rule does not judge "
            "whether that tree is in the right order. Silence on a tagged PDF is not a pass.",
            "Multi-column layouts, pages with footnotes or captions, tables, and columns implied "
            "by whitespace alone.",
        ),
        evidence="One finding per affected page, with the page number, the share of inverted "
                 "line pairs and the line count.",
        fix="Review. For an untagged PDF with AI on, a vision model proposes a reading order for "
            "page 1 as a re-authoring instruction; nothing is written to the file. (An approved, "
            "complete re-ordering of existing tag siblings can be written, but this detector "
            "never fires on tagged PDFs, so it does not produce one.)",
        rules=(_eng("pdf.reading-order", "reading_order", "heuristic", "review"),),
    )

    out["1.3.3"] = FormatExplanation(
        status="partial",
        checks=(
            "Searches the document's extracted text for an instruction verb (click, select, see, "
            "…) followed closely in the same sentence by a shape word (round, square, …) or a "
            "position phrase (on the left, top-right, below, …).",
        ),
        thresholds=(
            threshold("Maximum findings per document", "textchecks:_SENSORY_MAX",
                      count("sentences")),
            threshold("Longest gap between the verb and the shape/position word",
                      "textchecks:_SENSORY_RE", lambda rx: count("characters")(
                          rx.pattern.split("{0,", 1)[1].split("}", 1)[0])),
            *_text_checks_caps(),
        ),
        does_not_check=(
            "Colour, size or sound used as the only cue — only shape and position are matched.",
            "Whether the instruction also offers a non-sensory cue elsewhere (e.g. the button's "
            "name); every match is sent to a person to judge.",
            "Text in images, or pages beyond the text-extraction limit.",
        ),
        evidence="One finding per matching sentence (deduplicated), quoting the sentence.",
        fix="Review. The sensory rewrite proposer does not handle PDF, and no PDF write-back "
            "exists for body text.",
        rules=(RuleRef("SENSORY_INSTRUCTION", _TXT, "heuristic", "review"),),
    )

    out["1.3.5"] = FormatExplanation(
        status="partial",
        checks=(
            "Reads every interactive form field's name (/T) and tooltip (/TU) and flags fields "
            "whose wording matches the WCAG personal-data vocabulary (email, phone, given/family "
            "name, postal code, address, date of birth, …).",
            "Skips wording that says the data belongs to someone else (company, employer, "
            "billing, emergency contact, …), because 1.3.5 only covers data about the user.",
            "PDF has no autocomplete-equivalent, so any such field is reported as unable to "
            "declare its purpose.",
        ),
        does_not_check=(
            "Fields whose names don't use recognisable words (e.g. “Text1”).",
            "Whose data a field really collects — a sole trader's “company address” is theirs "
            "but is suppressed, and wording can mislead in both directions.",
        ),
        evidence="One finding per document, naming up to three matching fields and how many more.",
        fix="Review. The format cannot express input purpose, so the guidance is to move "
            "personal-data forms to accessible HTML with autocomplete.",
        rules=(RuleRef("PDF_INPUT_NO_PURPOSE", f"{_DET}/input_purpose.py", "heuristic", "review"),),
    )

    out["1.4.1"] = FormatExplanation(
        status="partial",
        checks=(
            "For each hyperlink annotation, looks at the characters inside its rectangle; flags "
            "the link when that text is in a real hue (not grey/black) and there is no drawn "
            "underline under it.",
            "An underline counts when a thin line or rectangle sits just under the link and spans "
            "most of its width.",
        ),
        thresholds=(
            threshold("Channel spread that makes a colour a hue rather than grey",
                      "office_structure:_pdf_is_chromatic", pct, literal="0.15"),
            threshold("Underline must span at least this much of the link's width",
                      "office_structure:_pdf_link_has_underline", pct, literal="0.6"),
            _page_cap(),
        ),
        does_not_check=(
            "Colour used as the only cue anywhere else — legends, charts, status colours.",
            "Links distinguished by other means (bold, a 3:1 contrast difference from body text).",
            "Links whose text can't be read from the page (no characters inside the annotation).",
        ),
        evidence="One finding per document; scanning stops at the first page that has one.",
        fix="Review. There is no PDF write-back that adds a non-colour cue to a link.",
        rules=(RuleRef("PDF_COLOUR_ONLY_LINK", _OS, "heuristic", "review"),),
    )

    contrast_common_limits = (
        "Text over an image: the real background is the picture's pixels, so no ratio is "
        "computed (PDF_TEXT_OVER_IMAGE reports it under 1.4.3 instead).",
        "A glyph straddling the edge of a filled shape: it has two backgrounds, so it is skipped.",
        "Backgrounds drawn as anything other than a filled rectangle (paths, gradients, "
        "patterns, shading) — a glyph with no filled rectangle behind it is measured against "
        "white, the default page background.",
        "Bold is only recognised from the font's name; a bold face not named “Bold” is "
        "judged as normal text.",
        "Characters past the per-page and per-document limits (the finding says when it "
        "stopped early).",
    )
    contrast_caps = (
        threshold("Default page background", "office_structure:_PDF_DEFAULT_BG", hex_colour),
        threshold("A filled shape counts as the background when it covers at least",
                  "office_structure:_PDF_BG_COVERS_FRAC", pct),
        threshold("…and is ignored when it covers no more than",
                  "office_structure:_PDF_BG_GRAZES_FRAC", pct),
        threshold("A glyph counts as over an image when this much of it is inside one",
                  "office_structure:_MIN_OVERLAP_FRAC", pct),
        threshold("Characters measured per page", "office_structure:_MAX_CHARS_PER_PAGE",
                  count("characters")),
        threshold("Characters measured per document", "office_structure:_MAX_CHARS_TOTAL",
                  count("characters")),
    )
    large_text = (
        threshold("Large text size", "office_structure:_PDF_LARGE_PT", pt, standard=True),
        threshold("Large text size when bold", "office_structure:_PDF_LARGE_BOLD_PT", pt,
                  standard=True),
    )

    out["1.4.3"] = FormatExplanation(
        status="partial",
        checks=(
            "For every character with a readable fill colour, finds the colour behind it from the "
            "page structure (the topmost filled rectangle that covers it, otherwise the page "
            "default) and computes the true WCAG contrast ratio.",
            f"Applies the large-text bar to text of at least {large}; the normal-text bar "
            "otherwise.",
            "Separately flags characters that sit over an image, where contrast can't be proven "
            "from the file.",
        ),
        thresholds=(
            threshold("Minimum contrast, normal text", "office_structure:_pdf_required_ratios",
                      _aa_ratios("12"), standard=True),
            threshold("Minimum contrast, large text", "office_structure:_pdf_required_ratios",
                      _aa_ratios(str(osx._PDF_LARGE_PT)), standard=True),
            *large_text,
            *contrast_caps,
            threshold("Pages examined for text over images",
                      "office_structure:_MAX_PAGES_OVER_IMAGE", count("pages")),
        ),
        does_not_check=contrast_common_limits,
        evidence="PDF_LOW_CONTRAST_AA: one finding per document for the single worst glyph, with "
                 "its text and background colours and the measured ratio against the required "
                 "one. PDF_TEXT_OVER_IMAGE: one advisory finding per document with the count of "
                 "characters over images. Neither names the page.",
        fix="Auto, text only. Each failing text colour is rewritten in the page content stream to "
            "the nearest colour that clears 7:1 (or at least the AA bar) against every background "
            "it is actually drawn on, keeping the hue. A colour is left alone when any of its "
            "glyphs sits over an image or straddles a fill edge, or when no single colour clears "
            "all of its backgrounds; those stay findings. Shapes and images are never changed.",
        rules=(
            RuleRef("PDF_LOW_CONTRAST_AA", _OS, "deterministic", "auto"),
            RuleRef("PDF_TEXT_OVER_IMAGE", _OS, "heuristic", "review"),
        ),
    )

    out["1.4.5"] = FormatExplanation(
        status="partial",
        checks=(
            "Scanned-page check: a page with almost no extractable text and one image covering "
            "most of it is reported as a likely scan.",
            "OCR check (when tesseract is installed and enabled): each embedded raster image is "
            "OCR'd and reported when it holds enough real words. Images whose text reads like "
            "chart data (mostly numbers, currency/percent values or an axis-tick run) are skipped "
            "as WCAG's essential exception.",
            "Says so explicitly when OCR could not read an image or when the image limit was hit.",
        ),
        thresholds=(
            threshold("Scanned page: at most this many extractable characters",
                      "office_structure:_SCANNED_MAX_CHARS", count("characters")),
            threshold("Scanned page: one image covering at least",
                      "office_structure:_SCANNED_MIN_IMAGE_AREA_FRAC", pct),
            threshold("Scanned page: pages examined", "office_structure:_MAX_PAGES_SCANNED",
                      count("pages")),
            threshold("OCR: words needed to count as an image of text", "ocr:_MIN_WORDS",
                      count("words"), configurable=True, setting="env:ACP_OCR_MIN_WORDS"),
            threshold("OCR: smallest image examined", "ocr:_MIN_PIXELS", count("pixels"),
                      configurable=True, setting="env:ACP_OCR_MIN_PIXELS"),
            threshold("OCR: images examined per document", "ocr:_MAX_IMAGES", count("images"),
                      configurable=True, setting="env:ACP_OCR_MAX_IMAGES"),
            threshold("OCR: time allowed per image", "ocr:_OCR_TIMEOUT_S", count("seconds"),
                      configurable=True, setting="env:ACP_OCR_TIMEOUT_S"),
        ),
        does_not_check=(
            "Anything, via OCR, when tesseract is unavailable or ACP_DETECT_IMAGES_OF_TEXT=0 — the "
            "scanned-page check still runs.",
            "Text drawn as vector outlines rather than as a raster image.",
            "Whether the text in the image is essential (a logo) beyond the chart exception.",
        ),
        evidence="OCR_IMAGE_OF_TEXT: one per image, quoting the OCR'd text. PDF_LIKELY_SCANNED: "
                 "one per document with the number of pages. Unread-image and image-limit "
                 "notices: one each per document.",
        fix="Review. Adding alt text makes the image described (1.1.1) but does not remove the "
            "image of text; replacing it with real text is re-authoring.",
        rules=(
            RuleRef("PDF_LIKELY_SCANNED", _OS, "heuristic", "review"),
            RuleRef("OCR_IMAGE_OF_TEXT", _OCR, "heuristic", "review"),
            RuleRef("OCR_IMAGE_UNREAD", _OCR, "deterministic", "none"),
            RuleRef("OCR_IMAGE_CAP_REACHED", _OCR, "deterministic", "none"),
        ),
    )

    out["1.4.6"] = FormatExplanation(
        status="partial",
        checks=(
            "The same per-character measurement as 1.4.3, against the enhanced (AAA) bars.",
        ),
        thresholds=(
            threshold("Minimum contrast, normal text", "office_structure:_pdf_required_ratios",
                      _aaa_ratios("12"), standard=True),
            threshold("Minimum contrast, large text", "office_structure:_pdf_required_ratios",
                      _aaa_ratios(str(osx._PDF_LARGE_PT)), standard=True),
            *large_text,
            *contrast_caps,
        ),
        does_not_check=contrast_common_limits,
        evidence="One finding per document for the single worst glyph, with both colours and the "
                 "measured ratio.",
        fix="Auto, text only — the same recolouring pass as 1.4.3, which aims for 7:1 first and "
            "so clears this criterion when it can.",
        rules=(RuleRef("PDF_LOW_CONTRAST_AAA", _OS, "deterministic", "auto"),),
    )

    out["1.4.9"] = FormatExplanation(
        status="partial",
        checks=(
            "OCRs each embedded raster image with lower floors than 1.4.5 and reports any image "
            "holding a few real words. No chart exception — AAA allows none.",
        ),
        thresholds=(
            threshold("Words needed to count as an image of text", "ocr:_MIN_WORDS_STRICT",
                      count("words"), configurable=True, setting="env:ACP_OCR_MIN_WORDS_STRICT"),
            threshold("Smallest image examined", "ocr:_MIN_PIXELS_STRICT", count("pixels"),
                      configurable=True, setting="env:ACP_OCR_MIN_PIXELS_STRICT"),
            threshold("Images examined per document", "ocr:_MAX_IMAGES", count("images"),
                      configurable=True, setting="env:ACP_OCR_MAX_IMAGES"),
        ),
        does_not_check=(
            "Anything when OCR is unavailable or disabled.",
            "Text drawn as vector outlines rather than as a raster image.",
        ),
        evidence="One finding per image, quoting the OCR'd text.",
        fix="Review. Replacing an image of text with real text is re-authoring.",
        rules=(RuleRef("OCR_IMAGE_OF_TEXT_STRICT", _OCR, "heuristic", "review"),),
    )

    out["1.4.11"] = FormatExplanation(
        status="partial",
        checks=(
            "For every rectangle that declares both an outline colour and a fill colour, computes "
            "the true WCAG contrast of the outline against its own fill.",
        ),
        thresholds=(
            threshold("Minimum contrast", "office_structure:pdf_nontext_contrast_checks", ratio,
                      literal="3.0", standard=True),
            _page_cap(),
        ),
        does_not_check=(
            "Shapes against the page or whatever is behind them — only outline against own fill.",
            "Paths, curves, icons, chart marks, gradients and images.",
            "Whether the shape conveys meaning at all or is decoration; every hit is advisory.",
        ),
        evidence="One advisory finding per document for the lowest-contrast rectangle, with both "
                 "colours and the measured ratio.",
        fix="Review. No PDF write-back recolours shapes.",
        rules=(RuleRef("PDF_NONTEXT_LOW_CONTRAST", _OS, "deterministic", "review"),),
    )

    out["1.4.12"] = FormatExplanation(
        status="partial",
        checks=(
            "On each page, groups characters into lines, measures the typical baseline-to-baseline "
            "distance as a multiple of the typical font size, and flags the tightest page below "
            "the limit — a flattened PDF cannot honour a reader's spacing override.",
        ),
        thresholds=(
            threshold("Line pitch below which a page is flagged",
                      "office_structure:_TIGHT_LINE_PITCH", times_font),
            threshold("Minimum lines on a page to judge",
                      "office_structure:_MIN_LINES_FOR_SPACING", count("lines")),
            threshold("Characters read per page", "office_structure:_MAX_CHARS_PER_PAGE",
                      count("characters")),
            _page_cap(),
        ),
        does_not_check=(
            "Letter, word or paragraph spacing — only line pitch.",
            "Whether text actually clips when spacing is increased (a rendered outcome). The "
            "finding's evidence cites WCAG's 1.5× override, but the decision boundary is the "
            "line-pitch limit above.",
        ),
        evidence="One advisory finding per document, with the tightest page's line pitch (the "
                 "page itself is not named).",
        fix="Review. Line spacing is fixed in a PDF's content stream; no write-back changes it.",
        rules=(RuleRef("PDF_TIGHT_LINE_SPACING", _OS, "heuristic", "review"),),
    )

    out["2.4.1"] = FormatExplanation(
        status="partial",
        checks=(
            "A PDF at or above the page floor must have a non-empty bookmark outline.",
        ),
        thresholds=(
            threshold("Documents this long need bookmarks", "office_structure:_MIN_PAGES_FOR_OUTLINE",
                      count("pages")),
            threshold("Fix: headings needed to build an outline",
                      "remediate_pdf:_MIN_OUTLINE_ENTRIES", count("headings")),
            threshold("Fix: a line this much larger than body text reads as a heading",
                      "remediate_pdf:_HEADING_SIZE_RATIO", times_font),
            threshold("Fix: longest line treated as a heading", "remediate_pdf:_HEADING_MAX_CHARS",
                      count("characters")),
        ),
        does_not_check=(
            "Whether existing bookmarks are useful or point at the right places.",
            "Tagged headings, which are another way to bypass blocks.",
        ),
        evidence="One finding per document.",
        fix="Auto when confident. ACP builds the outline from the document's own headings (lines "
            "noticeably larger than body text, excluding running headers/footers and page "
            "furniture). With fewer headings than the minimum it builds nothing and the finding "
            "stays for a person.",
        rules=(RuleRef("PDF_NO_BOOKMARKS", _OS, "deterministic", "auto"),),
    )

    out["2.4.2"] = FormatExplanation(
        status="partial",
        checks=(
            "The document information dictionary has a non-empty /Title (re-read with pypdf to "
            "avoid a known false alarm).",
            "The viewer is told to show that title instead of the file name "
            "(ViewerPreferences /DisplayDocTitle true).",
        ),
        does_not_check=(
            "Whether the title describes the document.",
            "XMP metadata titles (dc:title).",
        ),
        evidence="One finding per document for each missing piece.",
        fix="Auto. A missing /Title is set from the file name (dashes/underscores become spaces); "
            "an existing title is never overwritten. DisplayDocTitle is set to true.",
        rules=(
            _eng("pdf.document-title", "document_title", "deterministic", "auto"),
            _eng("pdf.display-doc-title", "display_title", "deterministic", "auto"),
        ),
    )

    out["2.4.3"] = FormatExplanation(
        status="partial",
        checks=(
            "Forms in a tagged PDF: compares the tab order of form fields (their order in the "
            "AcroForm field tree) with their order in the tag tree, and flags any pair that is "
            "out of order.",
            "Otherwise: flags pages that have form fields but don't set /Tabs to /S (tab in "
            "structure order) — a proxy, not a proof.",
        ),
        thresholds=(
            threshold("Fields present in both orders before comparing",
                      "formats.pdf.detectors.focus_order:_has_inversion", count("fields"),
                      literal="2"),
        ),
        does_not_check=(
            "Links and other non-field interactive elements.",
            "Pages set to row (/R) or column (/C) tab order, which can be legitimate.",
            "PDFs with no form fields — nothing is reported for them.",
        ),
        evidence="One finding per document (the fallback states how many pages are affected).",
        fix="Auto for the /Tabs proxy: pages with form fields get /Tabs = /S. A mismatch between "
            "field order and tag order is not reordered and stays a finding for a person.",
        rules=(
            RuleRef("PDF_FOCUS_ORDER_STRUCT_MISMATCH", f"{_DET}/focus_order.py",
                    "deterministic", "review"),
            RuleRef("PDF_TAB_ORDER_NOT_STRUCTURE", f"{_DET}/focus_order.py", "heuristic", "auto"),
        ),
    )

    out["2.4.4"] = FormatExplanation(
        status="partial",
        checks=(
            "Flags a link whose target URL is printed verbatim on the page as its label.",
            f"Reads the text inside each link's rectangle and flags it when it is empty of "
            f"meaning — one of {vague_count} generic phrases, or itself a URL.",
        ),
        thresholds=(
            threshold("Generic link phrases", "office_structure:_VAGUE_LINK_TEXT", phrases),
            threshold("Pages examined", "office_structure:pdf_link_purpose_check",
                      count("pages"), literal="20"),
        ),
        does_not_check=(
            "Whether descriptive link text names the right destination.",
            "Surrounding sentence or table context, which 2.4.4 allows to supply the purpose.",
        ),
        evidence="One finding per document for raw-URL labels; one per document for vague "
                 "labels, with the count and the first example.",
        fix="Review only. Link text is drawn by text operators in the page content; rewriting it "
            "re-flows the page, so ACP explains the finding and a person edits the source.",
        rules=(
            RuleRef("PDF_LINK_RAW_URL", _OS, "deterministic", "review"),
            RuleRef("PDF_LINK_PURPOSE_VAGUE", _OS, "deterministic", "review"),
        ),
    )

    out["2.4.6"] = FormatExplanation(
        status="partial",
        checks=(
            "A TAGGED PDF at or above the page floor must contain at least one heading tag "
            "(H, H1–H6 or Title) somewhere in its tag tree.",
        ),
        thresholds=(
            threshold("Documents this long need headings",
                      "office_structure:_MIN_PAGES_FOR_OUTLINE", count("pages")),
            threshold("Tag-tree nodes examined", "office_structure:pdf_headings_labels_check",
                      count("nodes"), literal="5000"),
        ),
        does_not_check=(
            "Whether headings describe their sections — the core of 2.4.6.",
            "Whether heading levels are well ordered.",
            "Untagged PDFs (reported under 1.3.1 instead).",
        ),
        evidence="One finding per document.",
        fix="Assisted for exact matches. ACP proposes headings from the font hierarchy; where an "
            "existing tagged paragraph matches a proposed heading exactly, a person can approve "
            "promoting its tag to H1–H6 and ACP writes that role. Everything else is a heading "
            "map for re-authoring.",
        rules=(RuleRef("PDF_NO_HEADINGS", _OS, "deterministic", "assisted"),),
    )

    out["2.5.3"] = FormatExplanation(
        status="partial",
        checks=(
            "Push buttons: the visible caption (/MK /CA) must appear, ignoring case, inside the "
            "accessible name (/TU, or /T when /TU is empty).",
            "Other form fields: flags an accessible name that looks like a developer identifier "
            "(snake_case, or camelCase starting lower-case), which a speech-input user would "
            "never say.",
        ),
        does_not_check=(
            "The visible label of text boxes, checkboxes and lists — it is separate page text "
            "not linked to the field, so it is never compared to the name.",
            "Buttons without a /MK /CA caption.",
        ),
        evidence="Up to one finding per rule per document, naming the first three fields.",
        fix="Review. No write-back for accessible names under this criterion yet.",
        rules=(
            RuleRef("PDF_LABEL_NOT_IN_NAME", f"{_DET}/label_in_name.py", "deterministic", "review"),
            RuleRef("PDF_ACCESSIBLE_NAME_PROGRAMMATIC", f"{_DET}/label_in_name.py",
                    "heuristic", "review"),
        ),
    )

    out["3.1.1"] = FormatExplanation(
        status="implemented",
        checks=("The document catalog has a non-empty /Lang entry.",),
        does_not_check=(
            "Whether /Lang is a valid language tag, or the right language for the content.",
        ),
        evidence="One finding per document.",
        fix="Auto. When /Lang is missing ACP writes “en” (the remediate_pdf default — the "
            "document's language is not detected). Only runs when this criterion failed.",
        rules=(_eng("pdf.document-language", "document_language", "deterministic", "auto"),),
    )

    out["3.1.2"] = FormatExplanation(
        status="partial",
        checks=(
            "Detects the language of each sentence-sized passage in the extracted text; when a "
            "confident second language appears, reports the passages not marked as that language.",
            "A passage counts as marked only when it is the /ActualText of a tagged /P or /Span "
            "leaf carrying that /Lang.",
            "Separately, for tagged /P or /Span leaves with /ActualText: flags a confidently "
            "foreign passage whose /Lang does not match.",
        ),
        thresholds=(
            threshold("Shortest passage judged", "textchecks:_MIN_SEG_WORDS", count("words")),
            threshold("Detector confidence needed", "textchecks:_MIN_CONF", pct),
            threshold("Passages examined", "textchecks:_MAX_SEGS", count("passages")),
            threshold("Tagged passage: shortest judged",
                      "pdf_structural_language:deficient_language_targets", count("words"),
                      literal="10"),
            threshold("Tagged passage: detector confidence needed",
                      "pdf_structural_language:deficient_language_targets", pct, literal=".95"),
            *_text_checks_caps(),
        ),
        does_not_check=(
            "Anything when the langdetect library is not installed.",
            "Language marks on marked content that is not /ActualText on a /P or /Span leaf.",
            "Short passages, names and single foreign words.",
            "Tagged-passage check: documents with no catalog /Lang.",
        ),
        evidence="LANG_PARTS_UNMARKED: one per document listing each language and passage count. "
                 "PDF_PART_LANGUAGE: one per tagged passage, located by its structure path.",
        fix="Assisted for tagged passages: a person approves the suggested language and ACP "
            "writes /Lang on that exact tag, verifying page content is unchanged. Untagged text "
            "cannot be marked.",
        rules=(
            RuleRef("LANG_PARTS_UNMARKED", _TXT, "heuristic", "review"),
            RuleRef("PDF_PART_LANGUAGE", "api/pdf_structural_language.py", "heuristic",
                    "assisted"),
        ),
    )

    out["3.1.5"] = FormatExplanation(
        status="partial",
        checks=(
            "Computes the Flesch-Kincaid grade of the whole extracted text and flags genuinely "
            "dense prose.",
        ),
        thresholds=(
            threshold("Grade at or above which the document is flagged", "textchecks:_MIN_GRADE",
                      count("(Flesch-Kincaid grade)")),
            threshold("Fewest words scored", "textchecks:_MIN_WORDS_FOR_READING", count("words")),
            threshold("Longer average sentences are treated as unpunctuated and not scored",
                      "textchecks:_MAX_WORDS_PER_SENTENCE", count("words")),
            *_text_checks_caps(),
        ),
        does_not_check=(
            "Text between WCAG's lower-secondary level (about grade 9) and the flag grade — "
            "deliberately left to the reviewer.",
            "Whether a supplementary plain-language version already exists.",
        ),
        evidence="One finding per document, with the grade.",
        fix="Review. No write-back for rewritten prose on PDF.",
        rules=(RuleRef("READING_LEVEL_ADVANCED", _TXT, "heuristic", "review"),),
    )

    out["4.1.2"] = FormatExplanation(
        status="partial",
        checks=(
            "For every interactive form field: it has an accessible name (/TU), a field type the "
            "PDF spec defines (/FT is Btn, Tx, Ch or Sig), and — only when marked required — a "
            "value (/V) or default (/DV).",
        ),
        does_not_check=(
            "Links, buttons and other components expressed through the tag tree rather than as "
            "form fields.",
            "Whether a name is meaningful — only that one is present.",
        ),
        evidence="One finding per field per problem, located as pdf:field:{page}:{n}.",
        fix="Auto for names when the field's own name (/T) reads like a label: it is tidied "
            "(‘first_name’ → ‘First Name’) and written to /TU. Generic names (“Text1”, "
            "“fld_03”) become a review card, and the approved name is written back. Missing "
            "types and values are left for a person.",
        rules=(
            RuleRef("PDF_FORM_NO_ACCESSIBLE_NAME", f"{_DET}/name_role_value.py",
                    "deterministic", "auto"),
            RuleRef("PDF_FORM_NO_FIELD_TYPE", f"{_DET}/name_role_value.py",
                    "deterministic", "review"),
            RuleRef("PDF_FORM_REQUIRED_NO_VALUE", f"{_DET}/name_role_value.py",
                    "deterministic", "review"),
        ),
    )

    return out

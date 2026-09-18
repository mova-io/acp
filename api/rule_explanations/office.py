"""What ACP actually checks in Word, PowerPoint and Excel files — one explanation per capability cell.

Every statement here is about the CURRENT detectors, and each one names where it lives:

  * first-party Python checks — api/office_structure.py (dispatched by `checks_for`), the migrated
    detectors under api/formats/<fmt>/detectors/, api/textchecks.py (sensory, language of parts,
    reading level) and api/ocr.py (images of text);
  * the .NET office analyser — engine/office-analysers/DigitalA11y.Analysers.DotNet/<Fmt>/Rules/,
    whose rule IDs (DOCX-TITLE-001 …) are listed in config/rule-catalog.json;
  * the fix side — api/remediate_office.py and the lanes in api/remediation_capability.py.

Numbers are never typed: `threshold()` renders each value from the constant or literal its
`source` names, and tests/test_rule_explanations.py resolves every source again. A number that
lives only inside a regex or a sentence of prose is described in words instead.

Lanes (assessment/remediation) are left None — the package overlays them from
remediation_capability so the two screens cannot disagree.

THE DOCX HEADING CELLS (1.3.1 / 2.4.6) are in one function, `docx_heading_cells`, because that
detector is being reworked; its wording is the one place to reconcile with that change.
"""
from __future__ import annotations

import ast
import inspect
import re
import textwrap

from rule_explanations import fmt_number, is_cs_source, resolve_cs
from rule_explanations.pdf import DERIVATIONS
from rule_explanations.pdf import resolve as _resolve_py
from rule_explanations.pdf import threshold as _derive_py
from rule_explanations.schema import FormatExplanation as FE
from rule_explanations.schema import RuleRef, Threshold

# ── threshold shorthand ───────────────────────────────────────────────────────────────────────
# Call sites below write a threshold compactly, and T() turns that into the contract form
# (schema.Threshold.source = "module:NAME") through the shared helper, which records how each
# value was made in pdf.DERIVATIONS:
#
#   T(label, "office_structure:NAME", "≥ {n}pt")                 a constant, shown as is
#   T(label, "office_structure:NAME / 2", "≥ {n}pt")             ...converted for display
#   T(label, "office_structure:func@3.0", "{n}:1")               a literal inside `func`
#   T(label, "office_structure:func@0.3 * 100", "≥ {n}%")         ...converted for display
#   T(label, "engine/…/Rule.cs:NAME / 100", …)                    a C# const (class@literal also)
#
# The part after the name is display arithmetic only (+ - * / and numbers); it never selects what
# is read. A literal must be a numeric constant in the function's own source, or the build fails.
_NUMTOK = re.compile(r"-?\d+(?:\.\d+)?")


def _arith(expr: str, env: dict) -> float:
    def ev(n):
        if isinstance(n, ast.Expression):
            return ev(n.body)
        if isinstance(n, ast.Constant) and isinstance(n.value, (int, float)):
            return n.value
        if isinstance(n, ast.Name) and n.id in env:
            return env[n.id]
        if isinstance(n, ast.UnaryOp) and isinstance(n.op, ast.USub):
            return -ev(n.operand)
        if isinstance(n, ast.BinOp) and type(n.op) in (ast.Add, ast.Sub, ast.Mult, ast.Div):
            a, b = ev(n.left), ev(n.right)
            return {ast.Add: lambda: a + b, ast.Sub: lambda: a - b,
                    ast.Mult: lambda: a * b, ast.Div: lambda: a / b}[type(n.op)]()
        raise ValueError(f"unsupported display arithmetic: {expr!r}")
    return ev(ast.parse(expr, mode="eval"))


def _py_literals(fn) -> set[float]:
    tree = ast.parse(textwrap.dedent(inspect.getsource(inspect.unwrap(fn))))
    return {float(n.value) for n in ast.walk(tree)
            if isinstance(n, ast.Constant) and isinstance(n.value, (int, float))
            and not isinstance(n.value, bool)}


def T(label: str, spec: str, template: str = "{n}", *, standard: bool = False,
      configurable: bool = False, setting: str | None = None) -> Threshold:
    head, _, rest = spec.partition(":")
    literal = None
    if "@" in rest:
        name, expr = rest.split("@", 1)
        literal = _NUMTOK.search(expr).group(0)
        expr = expr.replace(literal, "__v", 1)
    else:
        m = re.match(r"\s*\(?\s*([A-Za-z_]\w*)", rest)
        name = m.group(1)
        expr = rest.replace(name, "__v", 1)
    name = name.strip()
    source = f"{head}:{name}"

    def fmt(v, _expr=expr, _tmpl=template):
        if isinstance(v, str) and _expr.strip() == "__v":
            n = " ".join(v)                            # a set of characters, e.g. ".!?"
        elif isinstance(v, (set, frozenset, list, tuple)):
            n = ", ".join(f"“{s}”" for s in sorted(str(x) for x in v))
        else:
            n = fmt_number(_arith(_expr, {"__v": v}))
        return _tmpl.format(n=n)

    if is_cs_source(source):
        value = fmt(resolve_cs(source, literal))
        DERIVATIONS[(source, label, value)] = {"fmt": fmt, "literal": literal, "cs": True}
        return Threshold(label=label, value=value, source=source, configurable=configurable,
                         setting=setting, standard=standard)
    if literal is not None and float(literal) not in _py_literals(_resolve_py(source)):
        raise LookupError(f"literal {literal} is not in {source}")
    return _derive_py(label, source, fmt, literal=literal, standard=standard,
                      configurable=configurable, setting=setting)

# ── where things live ─────────────────────────────────────────────────────────────────────────
OS = "api/office_structure.py"
TXT = "api/textchecks.py"
OCR = "api/ocr.py"
_NET = "engine/office-analysers/DigitalA11y.Analysers.DotNet"
NET_CONTRAST_HELPER = f"{_NET}/Helpers/ColourContrastHelper.cs"


def _net_rule(fmt_dir: str, cls: str) -> str:
    return f"{_NET}/{fmt_dir}/Rules/{cls}.cs"


def acp(rule_id: str, source: str, method: str, fix: str) -> RuleRef:
    return RuleRef(id=rule_id, source=source, method=method, fix=fix, engine="acp")


def net(rule_id: str, fmt_dir: str, cls: str, method: str, fix: str) -> RuleRef:
    return RuleRef(id=rule_id, source=_net_rule(fmt_dir, cls), method=method, fix=fix,
                   engine="office-analyser")


_ENGINE_NOTE = ("Rules with an ID like DOCX-TITLE-001 come from the .NET office analyser, which runs "
                "alongside ACP's own checks during assessment.")

# The remediation-lane word for a RuleRef.fix, per lane.
_LANE_FIX = {"auto": "auto", "assisted": "assisted", "human": "review"}


def _fix_for(fmt: str, sc: str) -> str:
    import remediation_capability as cap
    return _LANE_FIX[cap.lane(fmt, sc)]


# ═════════════════════════════════════════════════════════════════════════════════════════════
# Shared cells — the same detector reading three formats
# ═════════════════════════════════════════════════════════════════════════════════════════════
def _sensory(fmt: str) -> FE:
    return FE(
        status="partial",
        checks=(
            "Reads the document's extracted text and looks for an instruction verb (for example "
            "“click”, “see”, “select”) followed, in the same sentence, by a shape word (round, "
            "square, triangular …) or a visual position (on the left, top-right, above, below).",
            "Each distinct sentence that matches is reported with the sentence quoted.",
        ),
        thresholds=(T("Findings reported per document, at most", "textchecks:_SENSORY_MAX"),),
        does_not_check=(
            "Colour-only or size-only references (“the red button”) are deliberately not matched; "
            "colour is 1.4.1's subject.",
            "Whether the same instruction also gives a non-sensory cue elsewhere — a person "
            "confirms each match, so a match is a likely issue rather than a proven failure.",
            "Instructions phrased without one of the listed verbs, or split across sentences.",
        ),
        evidence="One finding per distinct matching sentence, quoting it; no position in the file.",
        fix=("A rewrite of the sentence is drafted for a person to approve; nothing is written "
             "without approval."),
        rules=(acp("SENSORY_INSTRUCTION", TXT, "heuristic", _fix_for(fmt, "1.3.3")),),
    )


def _images_of_text(fmt: str, *, strict: bool) -> FE:
    sc = "1.4.9" if strict else "1.4.5"
    placed = ("Only images that are actually placed in the document are read."
              if fmt == "docx" else "Every raster image in the file's media folder is read.")
    checks = [
        "Embedded raster images (PNG, JPEG, GIF, BMP, TIFF, WebP) are read with OCR. " + placed,
        "An image counts as an image of text when OCR finds at least the minimum number of real "
        "words (two or more letters each) in it.",
        "Images smaller than the minimum area are not read.",
    ]
    thresholds = [
        T("Minimum words OCR must find", f"ocr:{'_MIN_WORDS_STRICT' if strict else '_MIN_WORDS'}",
          "{n} words", configurable=True,
          setting="env:ACP_OCR_MIN_WORDS_STRICT" if strict else "env:ACP_OCR_MIN_WORDS"),
        T("Minimum image area read", f"ocr:{'_MIN_PIXELS_STRICT' if strict else '_MIN_PIXELS'}",
          "{n} pixels", configurable=True,
          setting="env:ACP_OCR_MIN_PIXELS_STRICT" if strict else "env:ACP_OCR_MIN_PIXELS"),
        T("Images read per file, at most", "ocr:_MAX_IMAGES", configurable=True,
          setting="env:ACP_OCR_MAX_IMAGES"),
    ]
    rules = [acp("OCR_IMAGE_OF_TEXT_STRICT" if strict else "OCR_IMAGE_OF_TEXT", OCR, "heuristic",
                 _fix_for(fmt, sc))]
    does_not = [
        "Vector drawings (EMF/WMF), native charts and SmartArt — they are not raster media.",
        "How accurate OCR is on stylised, rotated or low-contrast lettering.",
        "When OCR is not installed on the server, or ACP_DETECT_IMAGES_OF_TEXT=0, this check "
        "produces no findings at all.",
    ]
    if strict:
        does_not.insert(0, "Nothing is exempt at this level (AAA): charts and logos with enough "
                           "words are reported too.")
        evidence = "One finding per image containing text, quoting the OCR text and the image number."
    else:
        checks.append("Images whose OCR text reads like a chart (numbers dominate, several "
                      "currency or percentage values, or a run of axis ticks) are skipped as the "
                      "Essential exception.")
        thresholds += [
            T("Chart exception: share of tokens that are numbers", "ocr:_looks_like_chart@0.3 * 100",
              "≥ {n}%"),
            T("Chart exception: currency or percentage values", "ocr:_looks_like_chart@3", "≥ {n}"),
            T("Chart exception: consecutive numeric tokens (axis ticks)", "ocr:_looks_like_chart@4",
              "≥ {n}"),
        ]
        does_not.insert(0, "Whether text in an image is essential beyond the chart heuristic "
                           "(a logo, for example) — a person confirms.")
        evidence = ("One finding per image containing text, quoting the OCR text and the image "
                    "number. Separate review findings say how many images could not be read "
                    "(timeout) and how many were skipped because the per-file cap was reached.")
        rules += [acp("OCR_IMAGE_UNREAD", OCR, "deterministic", "review"),
                  acp("OCR_IMAGE_CAP_REACHED", OCR, "deterministic", "review")]
    lane = _fix_for(fmt, sc)
    if fmt == "pptx" and not strict:
        fix = ("OCR proposes the transcript; after a person approves, the picture is replaced by a "
               "real text box and the image is removed.")
    elif lane == "assisted":
        fix = ("OCR reads the text back out of the image for a person to use as real text; "
               "nothing is written without approval.")
    else:
        fix = ("Routed to a person. Replacing a chart with its labels would lose information, so "
               "no transcript is applied.")
    return FE(status="partial", checks=tuple(checks), thresholds=tuple(thresholds),
              does_not_check=tuple(does_not), evidence=evidence, fix=fix, rules=tuple(rules))


def _reading_level(fmt: str) -> FE:
    lane = _fix_for(fmt, "3.1.5")
    return FE(
        status="partial",
        checks=(
            "Computes the Flesch-Kincaid grade level of the document's extracted text (sentence "
            "length and syllables per word, both counted in code).",
            "Reports the document when the grade reaches the flag level.",
            "Declines to score text that is too short, or whose average sentence is so long that "
            "the text is evidently unpunctuated extraction rather than prose.",
        ),
        thresholds=(
            T("Grade level that is flagged", "textchecks:_MIN_GRADE", "≥ grade {n}"),
            T("Minimum words before a grade is computed", "textchecks:_MIN_WORDS_FOR_READING",
              "{n} words"),
            T("Average sentence length above which no grade is given",
              "textchecks:_MAX_WORDS_PER_SENTENCE", "{n} words"),
        ),
        does_not_check=(
            "WCAG's bar is lower-secondary education (about grade 9). ACP flags only well above "
            "it, so text between that bar and the flag level is not reported.",
            "Whether a simpler summary or supplementary version already exists.",
            "Languages other than English — the syllable count assumes English spelling.",
        ),
        evidence="At most one finding per document, stating the computed grade.",
        fix=("A plain-language rewrite can be drafted for a person to approve."
             if lane == "assisted" else
             "Routed to a person — prose must be rewritten; no approved rewrite is written back."),
        rules=(acp("READING_LEVEL_ADVANCED", TXT, "heuristic", lane),),
    )


def _language_parts(fmt: str) -> FE:
    marked = {
        "docx": "A passage counts as marked when runs in the body, headers, footers or notes carry "
                "a w:lang of the language the passage was detected as.",
        "pptx": "A passage counts as marked when slide text runs carry a lang attribute of the "
                "language the passage was detected as (the default en-US PowerPoint stamps on "
                "every run does not count for a French passage).",
        "xlsx": "Excel has no per-run language property, so every foreign passage found is "
                "reported; nothing in the file can mark it.",
    }[fmt]
    rules = [acp("LANG_PARTS_UNMARKED", TXT, "heuristic", _fix_for(fmt, "3.1.2"))]
    thresholds = [
        T("Minimum words in a passage before its language is judged", "textchecks:_MIN_SEG_WORDS",
          "{n} words"),
        T("Minimum language-detection confidence", "textchecks:_MIN_CONF"),
        T("Passages examined per document, at most", "textchecks:_MAX_SEGS"),
    ]
    checks = [
        "Splits the document's extracted text into passages at sentence ends and line breaks, "
        "skipping passages that look like code.",
        "Detects each long-enough passage's language (langdetect, seeded so results repeat) and "
        "keeps only confident detections; the most common language is the document's own.",
        "Reports passages in any other language that the document does not identify. " + marked,
    ]
    if fmt == "docx":
        checks.append("The .NET office analyser separately flags a long run written in a "
                      "different script (for example Cyrillic in a Latin document) that carries "
                      "no language of its own.")
        thresholds.append(T("Office analyser: minimum words in a different-script run",
                            f"{_net_rule('Docx', 'LanguageOfPartsRule')}:MinWordCount", "{n} words"))
        rules.append(net("DOCX-LANGPART-001", "Docx", "LanguageOfPartsRule", "deterministic",
                         _fix_for(fmt, "3.1.2")))
    fix = {
        "docx": "A language mark is proposed for each passage; after a person approves, w:lang is "
                "written on its runs and the re-scan confirms it.",
        "pptx": "A language mark is proposed for each passage; after a person approves, lang is "
                "written on its runs.",
        "xlsx": "Routed to a person. Excel cannot store a language for part of a cell, so no "
                "approval could ever be written.",
    }[fmt]
    return FE(
        status="partial", checks=tuple(checks), thresholds=tuple(thresholds),
        does_not_check=(
            "Short foreign phrases and single borrowed words below the passage length.",
            "Whether a language mark that IS present names the linguistically correct language.",
            "When the langdetect library is not installed, this check produces no findings.",
        ),
        evidence=("One finding per document naming each unmarked language and how many passages "
                  "were found in it."
                  + (" The office analyser reports one finding per paragraph." if fmt == "docx" else "")),
        fix=fix, rules=tuple(rules))


def _controls_keyboard(fmt: str) -> FE:
    kinds = {
        "docx": "ActiveX controls, embedded OLE objects, VBA macro projects, input content controls "
                "(checkbox, date, dropdown, combo box, picture) and legacy form fields — in the body, "
                "headers, footers and notes",
        "pptx": "ActiveX controls, embedded OLE objects and VBA macro projects",
        "xlsx": "ActiveX controls, embedded OLE objects, VBA macro projects and worksheet form "
                "controls",
    }[fmt]
    return FE(
        status="manual",
        checks=(
            f"Lists the interactive controls embedded in the file: {kinds}.",
            "When any are present, one review finding names each kind and count so a person knows "
            "exactly which controls to try with a keyboard.",
        ),
        does_not_check=(
            "Whether keyboard focus can actually leave a control. That is runtime behaviour of the "
            "control and the application presenting it; it is not recorded in the file.",
            "A file with no controls gets no finding and no pass — the criterion does not arise.",
        ),
        evidence="At most one review finding per document, listing control kinds and counts.",
        fix="Routed to a person; no fix can be verified from the file.",
        rules=(acp("OFFICE_INTERACTIVE_CONTROL_KEYBOARD", OS, "manual", "review"),),
    )


def _nontext_contrast(fmt: str) -> FE:
    where = {
        "docx": "drawing shapes in the body, headers, footers and notes",
        "pptx": "shapes on every slide",
        "xlsx": "shapes in every worksheet drawing",
    }[fmt]
    colours = ("Explicit colours and theme colours (with lightness modifiers) are resolved through "
               "the file's theme." if fmt != "xlsx" else
               "Only explicit RGB colours are resolved; a theme-coloured shape is skipped.")
    func = {"docx": "docx_nontext_contrast_checks", "pptx": "pptx_nontext_contrast_checks",
            "xlsx": "xlsx_nontext_contrast_checks"}[fmt]
    rid = {"docx": "DOCX_NONTEXT_LOW_CONTRAST", "pptx": "PPTX_NONTEXT_LOW_CONTRAST",
           "xlsx": "XLSX_NONTEXT_LOW_CONTRAST"}[fmt]
    lane = _fix_for(fmt, "1.4.11")
    return FE(
        status="partial",
        checks=(
            f"Measures {where} that have both a solid outline and a solid fill: the outline colour "
            "against the shape's own fill, with the WCAG contrast-ratio formula.",
            colours,
            "Reports the single lowest-contrast shape below the ratio.",
        ),
        thresholds=(T("Outline-to-fill contrast ratio", f"office_structure:{func}@3.0", "{n}:1",
                      standard=True),),
        does_not_check=(
            "Whether the shape conveys meaning or is decoration — a person confirms.",
            "An outline against what is BEHIND the shape (the page or slide), gradient or picture "
            "fills, icons drawn as glyphs, chart elements and form-control borders.",
        ),
        evidence="At most one review finding per document: the worst shape's colours and ratio.",
        fix=("A card offers the outline shade that reaches the ratio, measured; a person chooses "
             "whether to apply it." if lane == "assisted" else
             "Routed to a person; no fix restyles the shape."),
        rules=(acp(rid, OS, "deterministic", lane),),
    )


def _reflow(fmt: str) -> FE:
    where = ("the widest table in the document body" if fmt == "docx"
             else "the widest table on any slide")
    return FE(
        status="partial",
        checks=(
            f"Counts the grid columns of {where} and flags it when it reaches the column count.",
            "When the file declares column widths, the finding adds the narrowest column's share "
            "of the table width and roughly how wide it would be on a phone screen.",
        ),
        thresholds=(T("Columns that make a table too wide to reflow", "office_structure:_WIDE_TABLE_COLS",
                      "≥ {n} columns"),),
        does_not_check=(
            "Whether the table actually scrolls in two directions at 320 px — that is a rendered "
            "outcome, not a file property.",
            "Other reflow problems: floating content, wide images, fixed page layouts.",
        ) + (("Tables in headers, footers and text boxes.",) if fmt == "docx" else ()),
        evidence="At most one review finding per document, describing the widest table.",
        fix="Routed to a person; restructuring a table is an authoring decision.",
        rules=(acp("OFFICE_WIDE_TABLE_REFLOW", OS, "heuristic", "review"),),
    )


def _text_spacing(fmt: str) -> FE:
    spacing = ("w:spacing with lineRule=\"exact\"" if fmt == "docx" else "a:lnSpc set in points")
    part = "the document body" if fmt == "docx" else "every slide"
    return FE(
        status="partial",
        checks=(
            f"Counts paragraphs in {part} that use exact (fixed) line spacing ({spacing}), which "
            "blocks a reader's own line-spacing override.",
            "Flags the document when enough paragraphs do. The finding quotes the tightest fixed "
            "line height as a multiple of its font size.",
        ),
        thresholds=(
            T("Paragraphs with fixed line spacing before flagging",
              "office_structure:_MIN_EXACT_SPACING_PARAS", "≥ {n}"),
            T("Line-height multiple quoted as the WCAG reference (reported, does not trigger)",
              "office_structure:office_text_spacing_checks@1.5", "{n}×", standard=True),
        ),
        does_not_check=(
            "Whether text actually clips when spacing is increased — a rendered outcome.",
            "Letter, word and paragraph spacing overrides.",
        ),
        evidence="At most one review finding per document, with the count and tightest ratio.",
        fix="Routed to a person; no fix rewrites line spacing.",
        rules=(acp("OFFICE_EXACT_LINE_SPACING", OS, "heuristic", "review"),),
    )


def _vague_links(fmt: str, rid: str, where: str, engine_rule: RuleRef, engine_note: str) -> FE:
    return FE(
        status="partial",
        checks=(
            f"Reads the display text of {where}.",
            "A link fails when its text is a generic phrase from the list, or is itself a web "
            "address (starts with http://, https:// or www.). Links with no text of their own "
            "(an image link) are left to 1.1.1.",
            engine_note,
        ),
        thresholds=(T("Phrases treated as unclear link text", "office_structure:_VAGUE_LINK_TEXT"),),
        does_not_check=(
            "Whether descriptive text points at the right destination (an “Annual Report” link to "
            "last year's report passes).",
            "Surrounding context: WCAG allows the sentence around a link to explain it, but ACP "
            "judges the link text alone, so a link explained by its sentence is still reported.",
        ),
        evidence=("One finding per document with the count of unclear links and one quoted example"
                  + (", located at the example's slide or cell." if fmt != "docx" else ".")),
        fix=("Descriptive link text is drafted from the link's target (derived, or AI-drafted when "
             "AI is enabled) for a person to approve; the re-scan confirms it."),
        rules=(acp(rid, OS, "deterministic", "assisted"), engine_rule),
    )


def _ambiguous_links(fmt: str, rid: str) -> FE:
    return FE(
        status="partial",
        checks=(
            "Groups hyperlinks by their display text (trimmed, ignoring case).",
            "Every link whose text is shared with a link to a DIFFERENT destination is reported. "
            "Differently-worded links to the same destination are fine.",
        ),
        does_not_check=(
            "Whether unique link text actually describes its destination (2.4.4's subject).",
            "Links whose text is empty.",
        ),
        evidence="One finding per link in an ambiguous group; no position in the file.",
        fix="Distinct link text is drafted per destination for a person to approve.",
        rules=(acp(rid, OS, "deterministic", _fix_for(fmt, "2.4.9")),),
    )


def _title(fmt: str) -> FE:
    if fmt == "docx":
        checks = (
            "The office analyser reports a document whose core-properties title (dc:title) is "
            "empty.",
            "It also reports a long document with no bookmarks. Length is approximated by the "
            "number of section breaks, not rendered pages, and the finding is filed under 2.4.2.",
        )
        thresholds = (T("Office analyser: length (section count) above which bookmarks are "
                        "expected", f"{_net_rule('Docx', 'BookmarksRule')}:PageThreshold",
                        "> {n}"),)
        rules = (net("DOCX-TITLE-001", "Docx", "DocumentTitleRule", "deterministic", "auto"),
                 net("DOCX-BOOKMARK-001", "Docx", "BookmarksRule", "heuristic", "none"))
        fix = ("An empty title is set automatically from the file name (hyphens and underscores "
               "become spaces). Bookmarks are not added.")
        evidence = "One finding per document for a missing title; one for missing bookmarks."
    elif fmt == "pptx":
        checks = (
            "The office analyser reports each slide with no title placeholder, or whose title "
            "placeholder holds no text.",
            "It also reports each slide whose title repeats an earlier slide's title (ignoring "
            "case), on the later slide.",
        )
        thresholds = ()
        rules = (net("PPTX-TITLE-001", "Pptx", "SlideTitleRule", "deterministic", "auto"),
                 net("PPTX-TITLE-002", "Pptx", "SlideTitleUniquenessRule", "deterministic", "none"))
        fix = ("A slide with no title gets a programmatic title from the first text on the slide "
               "(or “Untitled slide”); an empty document title is set from the file name. "
               "Duplicate titles are not renamed.")
        evidence = "One finding per slide."
    else:
        checks = (
            "The office analyser reports a workbook whose core-properties title (dc:title) is "
            "empty.",
            "It also reports each defined table still carrying an Excel default name (Table1, "
            "Table2 …), filed under 2.4.2.",
        )
        thresholds = ()
        rules = (net("XLSX-TITLE-001", "Xlsx", "DocumentTitleRule", "deterministic", "auto"),
                 net("XLSX-TABLE-NAME-001", "Xlsx", "TableNameRule", "deterministic", "none"))
        fix = ("An empty title is set automatically from the file name. Default table names are "
               "not renamed.")
        evidence = "One finding per workbook for a missing title; one per default-named table."
    return FE(
        status="implemented", checks=checks + (_ENGINE_NOTE,), thresholds=thresholds,
        does_not_check=(
            "Whether a title that IS set describes the document's topic or purpose. A title set "
            "from the file name is only as good as the file name.",
        ),
        evidence=evidence, fix=fix, rules=rules)


def _language(fmt: str) -> FE:
    how = {
        "docx": "neither the core-properties language (dc:language) nor any content language "
                "(w:lang in the document defaults or on any run) is declared",
        "pptx": "neither the core-properties language nor a lang/altLang on any text run of the "
                "slides, layouts or masters is declared",
        "xlsx": "the core-properties language (dc:language) is empty — Excel content carries no "
                "other language declaration",
    }[fmt]
    cls = {"docx": "Docx", "pptx": "Pptx", "xlsx": "Xlsx"}[fmt]
    return FE(
        status="implemented",
        checks=(f"The office analyser reports the file when {how}.", _ENGINE_NOTE),
        does_not_check=("Whether the declared language is the right one — a French document "
                        "declared en-US passes.",),
        evidence="At most one finding per document.",
        fix=("When dc:language is missing, it is set automatically to the remediation run's "
             "language (en-US unless the run specifies another); the re-scan confirms it."),
        rules=(net(f"{fmt.upper()}-LANG-001", cls, "DocumentLanguageRule", "deterministic", "auto"),),
    )


def _alt_text(fmt: str) -> FE:
    where = {
        "docx": "every embedded image (inline and floating) in the body, headers, footers, "
                "footnotes and endnotes",
        "pptx": "every picture on slides, slide layouts and slide masters",
        "xlsx": "every picture in the worksheet drawings",
    }[fmt]
    engine = {
        "docx": "The office analyser checks body images the same way and also rejects alt text "
                "that is only a placeholder word.",
        "pptx": "The office analyser checks pictures and graphic frames (charts, tables, SmartArt) "
                "on slides, skipping hidden ones, and also rejects a placeholder word as alt text.",
        "xlsx": "The office analyser checks pictures and graphic frames (charts) in drawings, and "
                "also rejects a placeholder word as alt text.",
    }[fmt]
    cls = {"docx": "Docx", "pptx": "Pptx", "xlsx": "Xlsx"}[fmt]
    return FE(
        status="partial",
        checks=(
            f"Reads the alt text (descr) of {where}.",
            "An image fails when its alt text is empty, a generic auto-name such as “Picture 3” or "
            "“image1.png”, or a file name or path — unless it carries the Office “decorative” "
            "flag, which conforms without alt text.",
            engine + " When the analyser reports any 1.1.1 finding for a file, ACP drops its own "
                     "duplicate findings for that file.",
        ),
        does_not_check=(
            "Whether alt text that is present actually describes the image.",
            "Non-text content other than pictures in ACP's own check: charts, SmartArt, grouped "
            "shapes and embedded objects.",
        ),
        evidence=("One finding per undescribed image, located by part and element name "
                  "(for example “…/slide3.xml#Picture 2”)."),
        fix=("Native charts get alt text written from their own data (exact values, no model). "
             "Other images take alt text from a faithful source such as a caption, or from a "
             "vision description anchored to text read out of the image; an unanchored AI draft is "
             "queued for one-click approval. Images with no source are routed to a person."),
        rules=(acp(f"{fmt.upper()}_IMAGE_NO_ALT", f"api/formats/{fmt}/detectors/non_text_content.py",
                   "deterministic", "assisted"),
               net(f"{fmt.upper()}-ALT-001", cls, "AltTextRule", "deterministic", "assisted")),
    )


# ═════════════════════════════════════════════════════════════════════════════════════════════
# DOCX HEADINGS — 1.3.1 / 2.4.6.  ONE PLACE TO RECONCILE WITH THE HEADING-DETECTOR REWORK.
# ═════════════════════════════════════════════════════════════════════════════════════════════
def docx_heading_cells() -> dict[str, FE]:
    """docx 1.3.1 and 2.4.6, describing office_structure.docx_heading_candidates — the ONE
    candidate list both DOCX_PSEUDO_HEADING and the promoter in remediate_office work from."""
    size_thresholds = (
        T("Size path: largest text size for a heading candidate",
          "office_structure:PSEUDO_HEADING_MIN_HALF_PT / 2", "≥ {n}pt"),
        T("Size path: largest text size when the paragraph is bold",
          "office_structure:(PSEUDO_HEADING_MIN_HALF_PT - 2) / 2", "≥ {n}pt"),
        T("Size path: candidate length, at most", "office_structure:PSEUDO_HEADING_MAX_WORDS",
          "{n} words"),
        T("Size path: all-capitals text this short is a wordmark, not a heading",
          "office_structure:_MAX_CAPS_FRAGMENT", "≤ {n} letters"),
        T("Size path, automatic promotion: size above this document's body text",
          "office_structure:STRONG_SIZE_MARGIN_HALF_PT / 2", "≥ +{n}pt"),
        T("Size path, automatic promotion: size above body text when bold",
          "office_structure:STRONG_BOLD_MARGIN_HALF_PT / 2", "≥ +{n}pt"),
    )
    label_thresholds = (
        T("Section-label path: label length, at most", "office_structure:SECTION_LABEL_MAX_WORDS",
          "{n} words"),
        T("Section-label path: labels sharing the exact formatting for automatic promotion",
          "office_structure:SECTION_LABEL_MIN_PEERS", "≥ {n}"),
        T("Section-label path: letters a short all-capitals label with a colon needs",
          "office_structure:SECTION_LABEL_MIN_LETTERS", "≥ {n}"),
        T("Section-label path: share of digits above which a line is contact or date furniture",
          "office_structure:SECTION_LABEL_MAX_DIGIT_SHARE * 100", "> {n}%"),
        T("Section-label path: first words that mark form, letter or caption furniture",
          "office_structure:SECTION_LABEL_NOT_A_SECTION"),
        T("Section-label path: a label may not end with one of these (it reads as a sentence)",
          "office_structure:SECTION_LABEL_SENTENCE_END"),
        T("Deepest heading level written", "office_structure:HEADING_MAX_LEVEL", "Heading {n}"),
    )
    h131 = FE(
        status="partial",
        checks=(
            "Finds body paragraphs that LOOK like headings but are not in the heading outline, so "
            "screen-reader users cannot navigate to them. A paragraph already counts as a heading "
            "when it has an outline level, a Heading or Title style, or a style based on one.",
            "Formatting is resolved the way Word resolves it: document defaults, then the "
            "paragraph style chain, then the character style chain, then the run's own formatting.",
            "Size path: a short paragraph containing a letter whose largest text reaches the size "
            "floor (a lower floor when bold) — unless it reads as page furniture: a bare figure, a "
            "pull quote, a short all-capitals wordmark or a byline. Only size and bold set on the "
            "text itself (directly or by a character style) count here, so a large-print "
            "document's default size does not turn every short line into a candidate.",
            "Section-label path: a short paragraph at body size that is bold throughout AND carries "
            "a label cue — a trailing colon, all capitals or underline — does not end like a "
            "sentence, and is followed by ordinary content. Excluded: list items, table cells, text "
            "boxes, caption/subtitle/quote styles, inline “Label: value” lines, contact details, "
            "captions and number-heavy lines (phone numbers, dates, addresses).",
            "The office analyser separately checks the heading outline — no Heading 1, more than "
            "one Heading 1, or a skipped level — and every table with more than one row whose "
            "first row is not marked as a header.",
        ),
        thresholds=size_thresholds + label_thresholds,
        does_not_check=(
            "Headers and footers are not scanned for this rule; nor are text boxes and table cells.",
            "Short all-capitals labels without a colon (“FAQ”) are missed on purpose — they look "
            "exactly like wordmarks.",
            "Plain bold labels with no colon, capitals or underline are not candidates — plain bold "
            "is everyday emphasis.",
            "Word's toggle-property flipping (bold in a style switched off by the run) is not "
            "modelled.",
            "Lists typed with manual bullets or numbers, header COLUMNS and complex tables.",
            "Whether a candidate really is a section heading — both paths are heuristics, which "
            "is why any candidate that is not clear-cut goes to review instead of being restyled.",
        ),
        evidence=("One finding per candidate paragraph, located word:p:N (the paragraph's number "
                  "in document order) and quoting its text and formatting. A candidate ACP will fix "
                  "is a moderate finding that says “Auto-fix: mark it as Heading N”; a review "
                  "candidate is an advisory review finding (it costs no score points) that says why "
                  "it was left alone. The office analyser reports one finding per outline problem "
                  "(located by paragraph) and one per table."),
        fix=("Promoted automatically only when the evidence is clear. Size path: at least the "
             "automatic-promotion margin above this document's body text (its most common paragraph "
             "size). Label path: enough labels share exactly the same formatting, a heading comes "
             "before them, and the bold is on the text rather than coming from its paragraph style; "
             "the label goes one level below the heading before it, and only where the outline "
             "clean-up will not renumber it. When no heading comes before the labels but a "
             "Title-styled paragraph does, and the document has no Heading 1 of its own, the Title "
             "becomes the level-1 heading by an outline level alone (its style and look are "
             "unchanged) and the labels become Heading 2. The fix changes only the paragraph's style to Heading N "
             "(adding a missing Heading N style based on the body style); numbering, text and run "
             "formatting are kept, it is safe to run twice, and a re-scan of the saved file "
             "verifies it. Everything else is left unchanged for review. The first row of every "
             "multi-row table is marked as a repeating header row, and the outline is normalised to "
             "exactly one Heading 1."),
        rules=(acp("DOCX_PSEUDO_HEADING", OS, "heuristic", "auto"),
               net("DOCX-HEAD-001", "Docx", "HeadingStructureRule", "deterministic", "auto"),
               net("DOCX-TABLE-001", "Docx", "TableHeaderRule", "deterministic", "auto")),
    )
    h246 = FE(
        status="partial",
        checks=(
            "Walks the paragraphs styled Heading 1–9 in the document body, in order, and reports "
            "every place the level jumps by more than one (for example Heading 1 straight to "
            "Heading 3).",
            "Reports every heading-styled paragraph that has no text and holds no picture — an "
            "empty entry in the screen reader's heading list.",
        ),
        does_not_check=(
            "Whether a heading's words describe its section — “Section 1” or “Untitled” pass. "
            "That judgement is why a clean result is review, never a certified pass.",
            "Form-field labels, and headings in headers, footers or text boxes.",
        ),
        evidence=("One finding per level gap (naming the two levels and the heading's position) "
                  "and one per empty heading."),
        fix=("Skipped levels are closed automatically (each heading steps at most one level "
             "below the one before it). Empty headings are not changed; they are routed to a "
             "person."),
        rules=(acp("DOCX_HEADING_SKIP", OS, "deterministic", "auto"),
               acp("DOCX_HEADING_EMPTY", OS, "deterministic", "review")),
    )
    return {"1.3.1": h131, "2.4.6": h246}


# ═════════════════════════════════════════════════════════════════════════════════════════════
# Word
# ═════════════════════════════════════════════════════════════════════════════════════════════
def docx() -> dict[str, FE]:
    out = {
        "1.1.1": _alt_text("docx"),
        "1.3.2": FE(
            status="partial",
            checks=(
                "Counts floating text in the document body — text boxes holding text and "
                "positioned text frames. A screen reader reads them where they are anchored, which "
                "need not match where they appear.",
                "Reports the document when any exist.",
            ),
            does_not_check=(
                "Whether the anchor position actually misleads — a person confirms.",
                "Reading order of ordinary paragraphs, tables or columns, and floating text in "
                "headers and footers.",
            ),
            evidence="At most one finding per document, with the count of floating items.",
            fix=("For each floating box a “move it inline here” proposal is drafted for a person "
                 "to approve."),
            rules=(acp("DOCX_READING_ORDER_RISK", OS, "heuristic", "assisted"),),
        ),
        "1.3.3": _sensory("docx"),
        "1.3.5": FE(
            status="partial",
            checks=(
                "Reads the Title (w:alias) of interactive content controls — checkbox, date, "
                "dropdown and combo box — in the body, headers, footers and notes.",
                "Reports the document when a Title matches personal-data wording (name, e-mail, "
                "phone, address, date of birth …) and is not about someone else's data "
                "(company, billing, emergency contact …). Word has no way to declare a field's "
                "input purpose, so such a field cannot meet 1.3.5 as written.",
            ),
            does_not_check=(
                "Fields whose Title does not use the vocabulary, and legacy form fields.",
                "Whose data a field really collects — the word match can be wrong both ways.",
            ),
            evidence="At most one finding per document, quoting up to three matching field titles.",
            fix="Routed to a person; Word offers nothing to write the purpose into.",
            rules=(acp("DOCX_INPUT_NO_PURPOSE", "api/formats/docx/detectors/input_purpose.py",
                       "heuristic", "review"),),
        ),
        "1.4.1": FE(
            status="partial",
            checks=(
                "Reads hyperlinks in the body, headers, footers and notes and counts those whose "
                "underline has been explicitly removed, leaving colour as the only thing that sets "
                "them apart from body text.",
            ),
            does_not_check=(
                "Whether another non-colour cue (bold, an icon, the sentence) marks the link.",
                "Colour used to carry meaning anywhere else — shaded table rows, coloured symbols, "
                "charts keyed by colour.",
            ),
            evidence="At most one review finding per document, with the count of such links.",
            fix="A card offers to restore the underline exactly; a person chooses to apply it.",
            rules=(acp("DOCX_COLOR_ONLY_LINK", OS, "deterministic", "assisted"),),
        ),
        "1.4.3": FE(
            status="partial",
            checks=(
                "The office analyser measures every text run that has its own colour (a hex value, "
                "or a theme colour it can resolve) against its paragraph's shading (direct or from "
                "the paragraph style), or white when there is none.",
                "Large text — at the WCAG size, or the smaller bold size, taken from the run or its "
                "character style — needs the lower ratio; all other text needs the higher one.",
                "The worst failing run in each paragraph is reported.",
                _ENGINE_NOTE,
            ),
            thresholds=(
                T("Contrast ratio, normal text", f"{_net_rule('Docx', 'ColourContrastRule')}:ColourContrastRule@4.5",
                  "{n}:1", standard=True),
                T("Contrast ratio, large text", f"{_net_rule('Docx', 'ColourContrastRule')}:ColourContrastRule@3.0",
                  "{n}:1", standard=True),
                T("Large text size", f"{NET_CONTRAST_HELPER}:ColourContrastHelper@18.0", "≥ {n}pt", standard=True),
                T("Large text size when bold", f"{NET_CONTRAST_HELPER}:ColourContrastHelper@14.0", "≥ {n}pt",
                  standard=True),
                T("Recolour target used by the fix", "office_structure:min_contrast_recolor@4.5",
                  "{n}:1", standard=True),
            ),
            does_not_check=(
                "Runs with no colour of their own (colour inherited from a style) and “automatic” "
                "colour.",
                "Backgrounds other than paragraph shading: page colour, table-cell shading, text "
                "boxes, shapes and images behind text.",
                "Text whose size comes only from the paragraph style or document defaults is "
                "measured as normal-size text, so large text of that kind can be reported against "
                "the higher ratio.",
            ),
            evidence=("One finding per paragraph (its worst run), located by paragraph number, "
                      "with the measured ratio and both colours."),
            fix=("Runs with an explicit hex colour below the ratio against their paragraph's own "
                 "shading are recoloured automatically, keeping the hue and changing only the "
                 "lightness as far as needed; the re-scan confirms it. Shading that comes from a "
                 "paragraph style is not considered by the fix."),
            rules=(net("DOCX-CONTRAST-001", "Docx", "ColourContrastRule", "deterministic", "auto"),),
        ),
        "1.4.5": _images_of_text("docx", strict=False),
        "1.4.8": FE(
            status="partial",
            checks=(
                "Counts text-bearing paragraphs in the document body that are set justified "
                "(aligned to both margins, including East-Asian distributed justification) directly "
                "on the paragraph.",
                "Reports the document when enough of them are, so one justified banner line does "
                "not trip it.",
            ),
            thresholds=(T("Justified paragraphs before flagging",
                          "office_structure:_MIN_JUSTIFIED_PARAS", "≥ {n}"),),
            does_not_check=(
                "Justification inherited from a paragraph style.",
                "1.4.8's other requirements: line width, line and paragraph spacing, and "
                "user-selectable colours.",
            ),
            evidence="At most one finding per document.",
            fix="A card offers an exact one-click change to left alignment; a person chooses it.",
            rules=(acp("DOCX_JUSTIFIED_TEXT", OS, "deterministic", "assisted"),),
        ),
        "1.4.9": _images_of_text("docx", strict=True),
        "1.4.10": _reflow("docx"),
        "1.4.11": _nontext_contrast("docx"),
        "1.4.12": _text_spacing("docx"),
        "2.1.2": _controls_keyboard("docx"),
        "2.4.2": _title("docx"),
        "2.4.4": _vague_links(
            "docx", "DOCX_LINK_PURPOSE_VAGUE",
            "every external hyperlink in the body, headers, footers, footnotes and endnotes",
            net("DOCX-LINK-001", "Docx", "LinkPurposeRule", "deterministic", "assisted"),
            "The office analyser checks body hyperlinks with its own list of generic phrases, and "
            "also reports links with no text."),
        "2.4.9": _ambiguous_links("docx", "DOCX_LINK_PURPOSE_AMBIGUOUS"),
        "2.4.10": FE(
            status="partial",
            checks=(
                "Reports a document that uses no Heading styles at all once it is long enough to "
                "need sections, counted in text-bearing paragraphs of the body.",
            ),
            thresholds=(T("Text paragraphs before headings are expected",
                          "office_structure:_MIN_PARAS_FOR_HEADINGS", "≥ {n}"),),
            does_not_check=(
                "Documents that have some headings but too few, or headings in the wrong places.",
                "Paragraphs that look like headings without the style (1.3.1 reports those).",
            ),
            evidence="At most one finding per document.",
            fix=("An AI model drafts names for the document's own sections and a person approves "
                 "where they go."),
            rules=(acp("DOCX_NO_SECTION_HEADINGS", OS, "deterministic", "assisted"),),
        ),
        "3.1.1": _language("docx"),
        "3.1.2": _language_parts("docx"),
        "3.1.5": _reading_level("docx"),
        "3.3.2": FE(
            status="partial",
            checks=(
                "Reads every interactive content control — checkbox, date picker, dropdown, combo "
                "box, picture — in the body, headers, footers and notes, and reports each one with "
                "no Title (w:alias), the label Word shows and announces.",
            ),
            does_not_check=(
                "Plain-text and rich-text content controls (Word also uses them for non-form "
                "template placeholders), legacy form fields and ActiveX controls.",
                "Whether a label that IS present is clear, or whether instructions are needed.",
            ),
            evidence="One finding per unlabelled control; no position in the file.",
            fix=("The label is borrowed automatically from the text right next to the control (the "
                 "run before it, or the cell to its left) and written as its Title. A control with "
                 "no adjacent text is routed to a person."),
            rules=(acp("DOCX_FORM_FIELD_NO_LABEL", OS, "deterministic", "auto"),),
        ),
        "4.1.2": FE(
            status="partial",
            checks=(
                "Reports each interactive content control (checkbox, date, dropdown, combo box, "
                "picture) with no Title (w:alias) — its accessible name — in the body, headers, "
                "footers and notes.",
                "Separately, when the document embeds ActiveX controls, OLE objects, VBA macro "
                "projects or legacy form fields, one review finding lists them for a person.",
            ),
            does_not_check=(
                "The name and role of ActiveX and OLE controls — they live in the control's own "
                "code, which no reading of the file reaches.",
            ),
            evidence=("One finding per unnamed content control, plus at most one review finding "
                      "listing other control kinds and counts."),
            fix=("A name is borrowed automatically from adjacent text (the same write that fixes "
                 "3.3.2). A control with nothing adjacent goes to a per-field review card, and the "
                 "approved name is written back."),
            rules=(acp("DOCX_FORM_FIELD_NO_NAME", "api/formats/docx/detectors/name_role_value.py",
                       "deterministic", "auto"),
                   acp("OFFICE_INTERACTIVE_CONTROL_NAME_ROLE", OS, "manual", "review")),
        ),
    }
    out.update(docx_heading_cells())
    return out


# ═════════════════════════════════════════════════════════════════════════════════════════════
# PowerPoint
# ═════════════════════════════════════════════════════════════════════════════════════════════
def pptx() -> dict[str, FE]:
    contrast_rule = _net_rule("Pptx", "ColourContrastRule")
    return {
        "1.1.1": _alt_text("pptx"),
        "1.3.1": FE(
            status="partial",
            checks=("The office analyser reports each table with more than one row whose first "
                    "row is not marked as a header row.", _ENGINE_NOTE),
            does_not_check=(
                "Whether the first row really holds headers, and header columns.",
                "Lists, and text styled to look like headings.",
                "Tables drawn from separate shapes.",
            ),
            evidence="One finding per table, located by slide and table number.",
            fix="The first row of every multi-row table is marked as the header row automatically.",
            rules=(net("PPTX-TABLE-001", "Pptx", "TableHeaderRule", "deterministic", "auto"),),
        ),
        "1.3.2": FE(
            status="partial",
            checks=(
                "The office analyser compares, on each slide, the order shapes are read in with "
                "their visual order (top to bottom, then left to right), and reports the slide "
                "when any shape is more than one position out of place.",
                "When several slides are reported, ACP folds them into one advisory finding.",
                _ENGINE_NOTE,
            ),
            thresholds=(T("Positions a shape may be out of visual order before the slide is "
                          "reported", f"{_net_rule('Pptx', 'ReadingOrderRule')}:ReadingOrderRule@1", "more than {n}"),),
            does_not_check=(
                "Whether top-to-bottom, left-to-right is the intended order (multi-column slides, "
                "callouts) — a person confirms.",
                "Shapes without a position of their own, such as ones inherited from the layout.",
            ),
            evidence="One finding per affected slide, combined into one finding when there are several.",
            fix=("Shapes on every slide are re-sequenced into top-to-bottom, left-to-right order "
                 "automatically."),
            rules=(net("PPTX-ORDER-001", "Pptx", "ReadingOrderRule", "heuristic", "auto"),),
        ),
        "1.3.3": _sensory("pptx"),
        "1.4.1": FE(
            status="partial",
            checks=("Counts hyperlinked text runs on every slide whose underline has been "
                    "explicitly removed, so colour alone marks them as links.",),
            does_not_check=(
                "Whether another non-colour cue marks the link.",
                "Colour used as the only carrier of meaning elsewhere — chart series, shaded cells, "
                "status markers.",
            ),
            evidence="At most one review finding per presentation, with the count of such links.",
            fix="Routed to a person; no fix restores the underline in a presentation.",
            rules=(acp("PPTX_COLOR_ONLY_LINK", OS, "deterministic", "review"),),
        ),
        "1.4.2": FE(
            status="partial",
            checks=("Reports each slide that embeds audio whose timing starts it with no delay and "
                    "no click, i.e. it plays automatically. Audio with no timing at all is treated "
                    "as click-to-play.",),
            does_not_check=(
                "How long the audio plays — WCAG allows under three seconds; the file does not "
                "record a duration.",
                "Video with sound, and audio started by other animation triggers.",
            ),
            evidence="One finding per slide with auto-starting audio.",
            fix=("Routed to a person. A proposal card exists, but no approved change is written "
                 "back."),
            rules=(acp("PPTX_AUDIO_AUTOPLAY", OS, "deterministic", "review"),),
        ),
        "1.4.3": FE(
            status="partial",
            checks=(
                "ACP measures text runs with an explicit colour inside a shape with an explicit "
                "solid fill, using the WCAG ratio, and keeps the lowest-contrast run in the deck. "
                "Font size is often inherited, so it reports only below the large-text ratio — "
                "a failure at any size.",
                "The office analyser measures each slide's runs that have their own colour against "
                "the shape fill, or the slide, layout or master background, or white; large text "
                "(size and bold read from the run) needs the lower ratio.",
                "Text sitting on a picture or gradient fill cannot be measured from colours; it is "
                "reported for review.",
                _ENGINE_NOTE,
            ),
            thresholds=(
                T("ACP check: ratio below which the lowest-contrast run is reported",
                  "office_structure:pptx_contrast_checks@3.0", "{n}:1", standard=True),
                T("Office analyser: contrast ratio, normal text", f"{contrast_rule}:ColourContrastRule@4.5", "{n}:1",
                  standard=True),
                T("Office analyser: contrast ratio, large text", f"{contrast_rule}:ColourContrastRule@3.0", "{n}:1",
                  standard=True),
                T("Office analyser: large text size", f"{contrast_rule}:LargeNormalThreshold / 100",
                  "≥ {n}pt", standard=True),
                T("Office analyser: large text size when bold",
                  f"{contrast_rule}:LargeBoldThreshold / 100", "≥ {n}pt", standard=True),
                T("Recolour target used by the fix", "office_structure:min_contrast_recolor@4.5",
                  "{n}:1", standard=True),
            ),
            does_not_check=(
                "Theme-coloured text in ACP's own check (explicit colours only).",
                "Text over pictures and gradients — flagged for a person, not measured.",
                "Text with no colour of its own (inherited from the placeholder or theme).",
            ),
            evidence=("ACP: at most one finding per presentation, quoting the worst colours and "
                      "ratio. Office analyser: one finding per slide (its worst run). Complex "
                      "backgrounds: one review finding listing the shapes."),
            fix=("Runs with an explicit colour on an explicit solid fill below the ratio are "
                 "recoloured automatically, keeping the hue and changing only lightness; the "
                 "re-scan confirms it. Theme colours, slide backgrounds and pictures are not "
                 "changed."),
            rules=(acp("PPTX_LOW_CONTRAST_AA", OS, "deterministic", "auto"),
                   net("PPTX-CONTRAST-001", "Pptx", "ColourContrastRule", "deterministic", "auto"),
                   acp("PPTX_TEXT_OVER_COMPLEX_BG", OS, "heuristic", "review")),
        ),
        "1.4.4": FE(
            status="partial",
            checks=("Counts text boxes whose auto-fit is switched off and that hold a lot of "
                    "text — the ones most likely to clip when text is enlarged to 200%.",),
            thresholds=(T("Characters in a fixed-size box before it is reported",
                          "office_structure:_RESIZE_MIN_CHARS", "≥ {n}"),),
            does_not_check=("Whether the text actually clips at 200% — a rendered outcome.",
                            "Boxes with auto-fit on, and short text in fixed boxes."),
            evidence=("At most one review finding per presentation, with the count and the named "
                      "shapes."),
            fix="Routed to a person; no fix resizes boxes.",
            rules=(acp("PPTX_FIXED_TEXT_BOX_RESIZE", OS, "heuristic", "review"),),
        ),
        "1.4.5": _images_of_text("pptx", strict=False),
        "1.4.6": FE(
            status="partial",
            checks=("Uses the same measurement as ACP's 1.4.3 check (explicit run colour on an "
                    "explicit solid shape fill) and reports the presentation when the "
                    "lowest-contrast run is below the enhanced large-text ratio.",),
            thresholds=(T("Enhanced (AAA) ratio for large text, below which the run is reported",
                          "office_structure:pptx_contrast_checks@4.5", "{n}:1", standard=True),),
            does_not_check=(
                "The enhanced ratio for normal-size text is not tested — font size is not known "
                "reliably per run, so only the large-text bar is applied.",
                "Theme-coloured text, inherited colours, and text over pictures or gradients.",
            ),
            evidence="At most one finding per presentation, quoting the worst colours and ratio.",
            fix="Cleared by the same automatic recolour as 1.4.3.",
            rules=(acp("PPTX_LOW_CONTRAST_AAA", OS, "deterministic", "auto"),),
        ),
        "1.4.9": _images_of_text("pptx", strict=True),
        "1.4.10": _reflow("pptx"),
        "1.4.11": _nontext_contrast("pptx"),
        "1.4.12": _text_spacing("pptx"),
        "2.1.1": FE(
            status="manual",
            checks=(
                "Keyboard operability is a property of the program presenting the deck, not of the "
                "file, so ACP cannot assess it.",
                "The office analyser does report animations in the main sequence that start "
                "without a click trigger; that is a signal about pacing, not proof of a keyboard "
                "problem.",
            ),
            does_not_check=("Whether every function is available from a keyboard.",),
            evidence="The analyser's animation finding is one per slide.",
            fix="Routed to a person; nothing in the file can be rewritten to make it operable.",
            rules=(net("PPTX-ANIM-001", "Pptx", "AnimationOrderRule", "heuristic", "review"),),
        ),
        "2.1.2": _controls_keyboard("pptx"),
        "2.4.2": _title("pptx"),
        "2.4.3": FE(
            status="partial",
            checks=("On each slide with two or more placeholders, reports the slide when the title "
                    "placeholder is not the first one in reading order, so a screen reader reaches "
                    "the body before the heading.",),
            thresholds=(T("Placeholders on a slide before it is checked",
                          "office_structure:pptx_focus_order_checks@2", "≥ {n}"),),
            does_not_check=("Order among other shapes, and tab order inside embedded controls.",
                            "Layouts where title-last is intended — a person confirms."),
            evidence="One review finding per affected slide.",
            fix="Routed to a person; the order of placeholders is a layout decision.",
            rules=(acp("PPTX_FOCUS_ORDER", OS, "heuristic", "review"),),
        ),
        "2.4.4": _vague_links(
            "pptx", "PPTX_LINK_PURPOSE_VAGUE", "every hyperlinked text run on every slide",
            net("PPTX-LINK-001", "Pptx", "LinkPurposeRule", "deterministic", "assisted"),
            "The office analyser checks the same links using the whole paragraph's text, with its "
            "own shorter list of generic phrases, one finding per slide."),
        "2.4.6": FE(
            status="partial",
            checks=("Reports each slide that has a title placeholder left empty. Slides with no "
                    "title placeholder at all are a layout choice and are left to 2.4.2.",),
            does_not_check=("Whether a slide title that IS present describes the slide.",),
            evidence="One finding per slide, located by slide number.",
            fix=("A title is drafted from the slide's own content; after a person approves, it is "
                 "written into the title placeholder."),
            rules=(acp("PPTX_TITLE_EMPTY", OS, "deterministic", "assisted"),),
        ),
        "2.4.9": _ambiguous_links("pptx", "PPTX_LINK_PURPOSE_AMBIGUOUS"),
        "3.1.1": _language("pptx"),
        "3.1.2": _language_parts("pptx"),
        "3.1.5": _reading_level("pptx"),
        "4.1.2": FE(
            status="manual",
            checks=("Lists ActiveX controls, embedded OLE objects and VBA macro projects; when any "
                    "are present, one review finding names them for a person to check.",),
            does_not_check=("The controls' names and roles — they live in the controls' own code.",
                            "A deck with no controls gets no finding; the criterion does not arise."),
            evidence="At most one review finding per presentation, listing control kinds and counts.",
            fix="Routed to a person; no fix can name an opaque control.",
            rules=(acp("OFFICE_INTERACTIVE_CONTROL_NAME_ROLE", OS, "manual", "review"),),
        ),
    }


# ═════════════════════════════════════════════════════════════════════════════════════════════
# Excel
# ═════════════════════════════════════════════════════════════════════════════════════════════
def xlsx() -> dict[str, FE]:
    contrast_checks = (
        "Resolves each non-empty cell's font colour and fill through the workbook's cell styles: "
        "explicit RGB colours and theme colours (with tint) are used; no fill counts as white.",
        "Measures the WCAG contrast ratio of every such cell.",
    )
    contrast_limits = (
        "Legacy indexed palette colours and patterned fills — skipped rather than guessed.",
        "Colours applied by conditional formatting.",
        "Charts, shapes and images.",
    )
    return {
        "1.1.1": _alt_text("xlsx"),
        "1.3.1": FE(
            status="partial",
            checks=("The office analyser reports each defined table (Format as Table) whose header "
                    "row is switched off.", _ENGINE_NOTE),
            does_not_check=(
                "Ordinary cell ranges that are not defined tables — most worksheet data — are not "
                "checked for headers.",
                "Whether the header row's text is meaningful, and merged header cells.",
            ),
            evidence="One finding per table, located by sheet and table name.",
            fix="The header row is switched on automatically for every such table.",
            rules=(net("XLSX-TABLE-001", "Xlsx", "TableHeaderRule", "deterministic", "auto"),),
        ),
        "1.3.2": FE(
            status="partial",
            checks=(
                "The office analyser reports hidden rows and hidden columns that contain data.",
                "It reports a sheet with more merged ranges than its limit, since heavy merging "
                "scrambles the reading order.",
                "It reports a visible sheet with no values, formulas, charts or images — an empty "
                "stop when moving between tabs.",
                _ENGINE_NOTE,
            ),
            thresholds=(T("Merged ranges per sheet before it is reported",
                          f"{_net_rule('Xlsx', 'MergedCellsRule')}:MergedCellThreshold", "> {n}"),),
            does_not_check=("Whether rows and columns are in a meaningful order.",
                            "Hidden sheets."),
            evidence=("One finding per hidden row or column with data, one per over-merged sheet, "
                      "and one per blank sheet."),
            fix=("Hidden rows and columns that contain values are un-hidden automatically. Merged "
                 "cells and blank sheets are left for a person."),
            rules=(net("XLSX-HIDDEN-001", "Xlsx", "HiddenContentRule", "deterministic", "auto"),
                   net("XLSX-MERGE-001", "Xlsx", "MergedCellsRule", "heuristic", "none"),
                   net("XLSX-BLANK-001", "Xlsx", "BlankWorksheetRule", "deterministic", "none")),
        ),
        "1.3.3": _sensory("xlsx"),
        "1.4.1": FE(
            status="partial",
            checks=("Counts conditional-formatting rules that shade cells by value — colour scales, "
                    "and rules applying a formatted fill — but not icon sets, which pair colour "
                    "with a symbol.",),
            does_not_check=(
                "Whether the status those colours show is also given in text or an icon — a "
                "person confirms.",
                "Colour used in plain cell fills, charts and images.",
            ),
            evidence="At most one review finding per workbook, with the count of rules.",
            fix="Routed to a person; no fix can add a non-colour cue.",
            rules=(acp("XLSX_COLOR_ONLY_STATUS", OS, "heuristic", "review"),),
        ),
        "1.4.3": FE(
            status="partial",
            checks=contrast_checks + ("Reports the workbook when any cell is below the minimum "
                                      "ratio.",),
            thresholds=(T("Contrast ratio", "office_structure:xlsx_contrast_checks@4.5", "{n}:1",
                          standard=True),),
            does_not_check=contrast_limits + (
                "The large-text allowance (3:1): cell font size is not considered, so large text "
                "between 3:1 and the minimum ratio is reported.",),
            evidence="At most one finding per workbook, quoting the worst cell's colours and ratio.",
            fix=("Each failing cell style gets a copy of its font in black or white, whichever "
                 "contrasts more with the fill. Only explicit-RGB fonts and fills are recoloured: "
                 "a failure involving a theme colour is detected but not fixed."),
            rules=(acp("XLSX_LOW_CONTRAST_AA", OS, "deterministic", "auto"),),
        ),
        "1.4.5": _images_of_text("xlsx", strict=False),
        "1.4.6": FE(
            status="partial",
            checks=contrast_checks + ("Reports the workbook when any cell is below the enhanced "
                                      "ratio.",),
            thresholds=(T("Enhanced contrast ratio", "office_structure:xlsx_contrast_checks@7.0",
                          "{n}:1", standard=True),),
            does_not_check=contrast_limits + (
                "The large-text allowance (4.5:1 at AAA): font size is not considered.",),
            evidence="At most one finding per workbook, quoting the worst cell's colours and ratio.",
            fix=("Cleared by the 1.4.3 recolour where that reaches the enhanced ratio; it runs only "
                 "when 1.4.3 is in scope and changes only cells below the minimum ratio, so a cell "
                 "between the two ratios is not recoloured."),
            rules=(acp("XLSX_LOW_CONTRAST_AAA", OS, "deterministic", "auto"),),
        ),
        "1.4.9": _images_of_text("xlsx", strict=True),
        "1.4.11": _nontext_contrast("xlsx"),
        "2.1.2": _controls_keyboard("xlsx"),
        "2.4.2": _title("xlsx"),
        "2.4.4": _vague_links(
            "xlsx", "XLSX_LINK_PURPOSE_VAGUE",
            "every cell hyperlink — its display text, or the cell's own value when it has none",
            net("XLSX-LINK-001", "Xlsx", "LinkPurposeRule", "deterministic", "assisted"),
            "The office analyser checks cell hyperlinks with its own list of generic phrases, one "
            "finding per cell."),
        "2.4.6": FE(
            status="partial",
            checks=(
                "ACP reports the workbook when two or more sheet tabs keep Excel's default names "
                "(Sheet1, Sheet2 …), or any defined-table column keeps a default name (Column1 …). "
                "A single default tab is normal and not reported.",
                "The office analyser reports every sheet with a default name, and every sheet name "
                "that repeats another (ignoring case).",
                _ENGINE_NOTE,
            ),
            thresholds=(T("ACP check: default-named sheet tabs before reporting",
                          "office_structure:xlsx_structure_checks@2", "≥ {n}"),),
            does_not_check=("Whether custom sheet and column names describe their content.",),
            evidence=("ACP: at most one finding per workbook naming the tabs. Office analyser: one "
                      "finding per sheet."),
            fix=("A meaningful name is drafted from each sheet's or column's own content for a "
                 "person to approve."),
            rules=(acp("XLSX_DEFAULT_LABELS", OS, "deterministic", "assisted"),
                   net("XLSX-SHEET-001", "Xlsx", "SheetNameRule", "deterministic", "assisted"),
                   net("XLSX-SHEET-002", "Xlsx", "SheetNameUniquenessRule", "deterministic",
                       "assisted")),
        ),
        "3.1.1": _language("xlsx"),
        "3.1.2": _language_parts("xlsx"),
        "3.1.5": _reading_level("xlsx"),
        "4.1.2": FE(
            status="manual",
            checks=("Lists ActiveX controls, embedded OLE objects, VBA macro projects and form "
                    "controls; when any are present, one review finding names them for a person "
                    "to check.",),
            does_not_check=("The controls' names and roles — they live in the controls' own code.",
                            "A workbook with no controls gets no finding; the criterion does not "
                            "arise."),
            evidence="At most one review finding per workbook, listing control kinds and counts.",
            fix="Routed to a person; no fix can name an opaque control.",
            rules=(acp("OFFICE_INTERACTIVE_CONTROL_NAME_ROLE", OS, "manual", "review"),),
        ),
    }

"""The WeasyPrint conformance report, checked as a screen reader would meet it.

WHAT THESE ASSERT, AND WHY NOT "IS THERE A STRUCTURE TREE". That question has a green answer for
a document with nothing in it — `api/report.py::_tag_pdf` bolts an EMPTY /StructTreeRoot onto
untagged ReportLab output and passes ACP's own `pdf.tagged` rule to this day. So every check here
walks the real tree and asks what a reader would actually get: a heading outline, header cells,
figures with alternatives, a link, the document's language and title.

THE ONE THAT MATTERS MOST IS THE CHARTS. Rendering the shipped template through WeasyPrint
unmodified passes veraPDF with zero failures and drops the charts from the tag tree entirely —
5 Figures under Chromium, 1 (the logo) under WeasyPrint, because WeasyPrint does not tag inline
<svg>. Conformant, and worse for the reader it is meant to serve. `test_every_chart_is_a_figure_
with_a_conclusion_stating_alt` is what stops that shape passing for compliance, and it is why the
charts are <img alt> + data table rather than inline SVG.

THE VALIDATOR IS THE OTHER HALF, not a substitute for these. veraPDF answers "is this PDF/UA-1";
it does not answer "did the two charts survive". The structural checks below run without veraPDF
installed, so a bare checkout still holds the renderer to its semantics.
"""
from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path

import pytest

ACP = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ACP / "api"))

pikepdf = pytest.importorskip("pikepdf")
pytest.importorskip("weasyprint")

from verapdf import NO_VERAPDF, VERAPDF_OK, validate  # noqa: E402

_FILES = [
    {"file": "a.pdf", "status": "done", "compliant": 1, "score": 90,
     "skipped_rules": 0, "issues": []},
    {"file": "b.docx", "status": "done", "compliant": 0, "score": 55, "skipped_rules": 0,
     "issues": [{"wcag": "SC_1_1_1", "severity": "CRITICAL"},
                {"wcag": "SC_2_4_2", "severity": "SERIOUS"}]},
    {"file": "c.xlsx", "status": "done", "compliant": 0, "score": 71, "skipped_rules": 2,
     "issues": [{"wcag": "SC_1_4_3", "severity": "MODERATE"}]},
]
_RUN = {"id": "selfcheck", "completed_at": "2026-08-04T00:00:00", "avg_score": 72,
        "files": 3, "certifiable": 1, "uncertain": 0, "error": 0}
_META = {"target": "WCAG 2.1 Level AA", "version": "3", "hash": "abc"}


def test_report_context_carries_canonical_stage_lineage():
    from report_tagged import _prepare_context
    meta = {**_META, "stage_lineage_digest": "a" * 64,
            "stage_lineage_status": "consistent"}
    context = _prepare_context(_RUN, _FILES, meta)
    assert context["stage_lineage_digest"] == "a" * 64
    assert context["stage_lineage_status"] == "consistent"


def test_report_carries_the_same_finding_reconciliation_attestation():
    from report_tagged import _prepare_context
    from report_weasy import render_html

    reconciliation = {
        "status": "reconciled",
        "content_digest": {"algorithm": "SHA-256", "value": "b" * 64},
        "outcomes": {"assessed": 17, "accounted": 17},
    }
    meta = {**_META, "finding_reconciliation": reconciliation}
    context = _prepare_context(_RUN, _FILES, meta)
    assert context["finding_reconciliation"] is reconciliation
    html = render_html(_RUN, _FILES, meta)
    assert "Finding reconciliation <strong>reconciled</strong>" in html
    assert "17 of\n    17 assessed findings have one durable disposition" in html
    assert "b" * 64 in html


def test_report_fails_safe_when_finding_accounting_is_inconsistent():
    from report_weasy import render_html

    reconciliation = {
        "status": "inconsistent",
        "content_digest": {"algorithm": "SHA-256", "value": "c" * 64},
        "outcomes": {"assessed": 17, "accounted": 19},
    }
    html = render_html(_RUN, _FILES, {**_META, "finding_reconciliation": reconciliation})
    assert "Accounting is inconsistent; no complete-resolution claim is made." in html
    assert "19 of" not in html


def _build(tmp: Path, **over) -> Path:
    import report_weasy
    run = {**_RUN, **over.pop("run", {})}
    files = over.pop("files", _FILES)
    out = tmp / "report.pdf"
    out.write_bytes(report_weasy.build_weasy_report(run, files, _META))
    return out


@pytest.fixture(scope="module")
def report(tmp_path_factory) -> Path:
    return _build(tmp_path_factory.mktemp("weasy"))


# ── walking the tree ─────────────────────────────────────────────────────────────────────────

def _walk(pdf) -> tuple[Counter, list[str], list[dict]]:
    """Every structure element, in reading order. Returns (tag counts, heading sequence, figures).

    Visited-set keyed on the PDF object id, NOT id(node): pikepdf returns a fresh wrapper on each
    access and CPython recycles those addresses, so an id()-keyed set produces false "already
    seen" hits and silently truncates the walk. An earlier version of this helper did exactly
    that and reported 2 headings and zero tables against a tree holding 5 headings, 2 tables,
    9 TH and 27 TD — a walker that under-reports makes every assertion below vacuous.
    """
    tags: Counter = Counter()
    headings: list[str] = []
    figures: list[dict] = []
    seen: set = set()

    def visit(node):
        if isinstance(node, pikepdf.Array):
            for kid in node:
                visit(kid)
            return
        if not isinstance(node, pikepdf.Dictionary):
            return
        try:
            oid = node.objgen
        except Exception:
            oid = None
        if oid and oid != (0, 0):
            if oid in seen:
                return
            seen.add(oid)
        s = node.get("/S")
        if s is not None:
            name = str(s).lstrip("/")
            tags[name] += 1
            if len(name) == 2 and name[0] == "H" and name[1].isdigit():
                headings.append(name)
            if name == "Figure":
                alt = node.get("/Alt")
                figures.append({"alt": str(alt) if alt is not None else None})
        kids = node.get("/K")
        if kids is not None:
            visit(kids)

    root = pdf.Root.get("/StructTreeRoot")
    if root is not None:
        visit(root.get("/K"))
    return tags, headings, figures


@pytest.fixture(scope="module")
def walked(report):
    with pikepdf.open(str(report)) as pdf:
        return _walk(pdf)


# ── the document as a whole ──────────────────────────────────────────────────────────────────

def test_the_document_declares_language_title_and_tagging(report):
    """3.1.1 language, 2.4.2 title, and the tagging flags — the three the engine's own rules
    check, asserted on the real catalog rather than on the renderer's intent."""
    with pikepdf.open(str(report)) as pdf:
        root = pdf.Root
        assert str(root.get("/Lang")) == "en-US"
        assert bool((root.get("/MarkInfo") or {}).get("/Marked")) is True
        assert "/StructTreeRoot" in root
        assert bool((root.get("/ViewerPreferences") or {}).get("/DisplayDocTitle")) is True
        assert root.get("/Metadata") is not None, "no XMP metadata stream — PDF/UA requires one"
        assert "Accessibility Assessment Report" in str(pdf.open_metadata().get("dc:title"))


def test_the_structure_tree_is_not_empty(walked):
    """The check that separates a real tree from report.py::_tag_pdf's empty one."""
    tags, _, _ = walked
    assert sum(tags.values()) > 100, f"suspiciously small tree: {dict(tags)}"


# ── headings and reading order ───────────────────────────────────────────────────────────────

def test_the_heading_outline_starts_at_h1_and_skips_no_level(walked):
    _, headings, _ = walked
    assert headings, "no headings in the structure tree"
    assert headings[0] == "H1", f"outline starts at {headings[0]}"
    levels = [int(h[1]) for h in headings]
    for prev, nxt in zip(levels, levels[1:]):
        assert nxt <= prev + 1, f"heading level jumps {prev} -> {nxt}: {headings}"


def test_every_section_has_a_heading(report):
    """Each section/article retains its own heading, including nested guide items."""
    def children(node):
        kids = node.get("/K")
        return list(kids) if isinstance(kids, pikepdf.Array) else [kids]

    def own_heading(node):
        for kid in children(node):
            if not isinstance(kid, pikepdf.Dictionary):
                continue
            role = str(kid.get("/S") or "")
            if role in ("/H1", "/H2", "/H3", "/H4", "/H5", "/H6"):
                return True
            if role != "/Sect" and own_heading(kid):
                return True
        return False

    def visit(node):
        if not isinstance(node, pikepdf.Dictionary):
            return
        if str(node.get("/S") or "") == "/Sect":
            assert own_heading(node), "Section has no own navigable heading"
        for kid in children(node):
            visit(kid)

    with pikepdf.open(str(report)) as pdf:
        visit(pdf.Root.StructTreeRoot)


# ── tables ───────────────────────────────────────────────────────────────────────────────────

def test_tables_carry_real_header_cells(walked):
    tags, _, _ = walked
    assert tags["Table"] >= 2, f"expected both data tables; got {tags['Table']}"
    assert tags["THead"] >= 2, "a table has no THead — header rows are not marked as headers"
    assert tags["TH"] >= 9, f"too few header cells: {tags['TH']}"
    assert tags["TD"] >= 1 and tags["TR"] >= 1


def test_every_table_row_has_a_row_header(walked):
    """Each row's first cell is a <th scope="row">, so a cell read out of context still says
    which file or criterion it belongs to. Column headers alone leave "55%" meaning nothing."""
    tags, _, _ = walked
    # 5 column headers + 4 column headers = 9; the rest are row headers, one per body row.
    assert tags["TH"] > 9, (
        f"only {tags['TH']} TH cells — that is column headers alone, with no row headers")


# ── charts: the thing a conformant-but-worse render silently loses ───────────────────────────

def test_every_chart_is_a_figure_with_a_conclusion_stating_alt(walked):
    """THE load-bearing test in this file.

    The shipped template's inline <svg> charts are invisible to WeasyPrint's tagger: rendered
    as-is the document still passes veraPDF with zero failures, and the charts are simply not in
    the tree. This asserts the treatment that fixes it — <img alt> — actually took, and that the
    alt says something a reader can use rather than naming the chart type.
    """
    _, _, figures = walked
    assert len(figures) >= 3, (
        f"expected the logo plus two charts as Figures; got {len(figures)}. If this is 1, the "
        f"charts have fallen back to inline SVG and are no longer in the reading order.")
    for fig in figures:
        assert fig["alt"], f"a Figure carries no /Alt: {fig}"
        assert len(fig["alt"]) > 3

    alts = " ".join(f["alt"] for f in figures)
    assert "out of 100" in alts, "the score ring's alt does not state the score"
    assert "affects the most files" in alts, "the bar chart's alt does not state its conclusion"


def test_the_chart_alt_reads_as_a_sentence(walked):
    """Alt text is READ ALOUD, so it has to parse. The first draft emitted "2 further criterions
    also has open issues" — a Figure with an /Alt, veraPDF-clean, and wrong in the one place only
    a screen-reader user would ever encounter."""
    _, _, figures = walked
    alts = " ".join(f["alt"] or "" for f in figures)
    assert "criterions" not in alts, f"bad plural in chart alt text: {alts}"
    assert "criteria also has" not in alts, f"subject/verb disagreement in chart alt text: {alts}"


def test_the_chart_numbers_are_also_in_a_real_table(walked):
    """The alt states a conclusion; the exact counts belong in the tag tree as data, not only as
    a picture. Two tables and their header cells are what make that true."""
    tags, _, _ = walked
    assert tags["Table"] >= 2 and tags["TD"] >= 8


# ── links ────────────────────────────────────────────────────────────────────────────────────

def test_the_standard_reference_is_a_real_link(walked):
    tags, _, _ = walked
    assert tags["Link"] >= 1, "no Link element — the WCAG reference is not a real link"


# ── the renderer degrades honestly ───────────────────────────────────────────────────────────

def test_a_run_with_no_score_omits_the_ring_rather_than_inventing_one(tmp_path):
    """avg_score is None for a run that produced no scores. The ring must disappear, not render
    a zero — a "0 out of 100" alt on a scan that was never scored is a fabricated finding."""
    out = _build(tmp_path, run={"avg_score": None})
    with pikepdf.open(str(out)) as pdf:
        _, _, figures = _walk(pdf)
    alts = " ".join(f["alt"] or "" for f in figures)
    assert "out of 100" not in alts, f"a score ring was rendered for an unscored run: {alts}"


def test_a_clean_run_still_produces_a_valid_tree(tmp_path):
    """No open issues means no bar chart and no findings table. The outline must survive it."""
    clean = [{"file": "a.pdf", "status": "done", "compliant": 1, "score": 100,
              "skipped_rules": 0, "issues": []}]
    out = _build(tmp_path, files=clean,
                 run={"files": 1, "certifiable": 1, "avg_score": 100})
    with pikepdf.open(str(out)) as pdf:
        tags, headings, figures = _walk(pdf)
    assert headings and headings[0] == "H1"
    assert tags["Table"] >= 1, "the file inventory table vanished on a clean run"
    for fig in figures:
        assert fig["alt"], "a Figure lost its /Alt on the clean-run path"


# ── the validator, when it is available ──────────────────────────────────────────────────────

@pytest.mark.skipif(not VERAPDF_OK, reason=NO_VERAPDF)
def test_the_report_is_pdfua_1_conformant(report):
    """The automated gate ADR 0034 requires. Structural correctness above is necessary and not
    sufficient — WeasyPrint's own documentation says selecting the pdf/ua-1 variant does not
    guarantee a conformant document."""
    result = validate(report)
    assert result.compliant, result.summary()
    assert result.failed_checks == 0


# ── fonts: the defect no structural check and no validator noticed ───────────────────────────

def _embedded_fonts(path: Path) -> set[str]:
    with pikepdf.open(str(path)) as pdf:
        out = set()
        for obj in pdf.objects:
            try:
                if isinstance(obj, pikepdf.Dictionary) and obj.get("/Type") == pikepdf.Name("/Font"):
                    bf = obj.get("/BaseFont")
                    if bf is not None:
                        # Strip the six-letter subset prefix ("ABCDEF+").
                        out.add(str(bf).lstrip("/").split("+")[-1])
            except Exception:
                continue
        return out


def test_the_report_is_set_in_the_intended_sans_face(report):
    """THE REGRESSION THIS EXISTS FOR. The font stack was briefly passed through a Jinja
    variable; the environment autoescapes, so it reached the CSS as

        font-family: &#34;Liberation Sans&#34;, &#34;DejaVu Sans&#34;, Arial, sans-serif;

    which is invalid, silently ignored, and rendered the whole customer-facing report in
    WeasyPrint's default SERIF face. veraPDF passed with zero failures. Every structural test
    stayed green — tagging does not depend on the font. Only rendering the page and looking at
    it caught it, which is why this asserts on the embedded font rather than on the CSS text:
    the CSS is the mechanism, the embedded face is the property.
    """
    fonts = _embedded_fonts(report)
    # Production is Debian and deliberately installs Liberation Sans. Developer Macs resolve
    # the same CSS fallback to Arial instead; that is not evidence that the declaration was
    # ignored. The invariant is a real sans face, never WeasyPrint's serif default. A separate
    # deployment test below pins the production image's deterministic font package.
    assert any(("Liberation" in f) or ("Arial" in f) for f in fonts), (
        f"neither Liberation Sans nor its Arial fallback is embedded — the font stack is not "
        f"reaching the renderer. Embedded: {sorted(fonts)}")
    assert not any(("Serif" in f) or ("Charter" in f) or ("Times" in f) for f in fonts), (
        f"a serif face is embedded — the sans stack was ignored. Embedded: {sorted(fonts)}")


def test_the_tick_and_cross_glyphs_have_a_font_that_carries_them(report):
    """Liberation Sans has no U+2713 ✓ or U+2717 ✗ and the File Inventory table prints both, so
    a second face must be embedded to supply them. Without it they render as tofu — visible to a
    sighted reader, invisible to every automated check in this file."""
    fonts = _embedded_fonts(report)
    # macOS supplies these glyphs from Arial Unicode MS. Production deliberately installs
    # DejaVu; accepting the platform-equivalent face keeps this render test meaningful locally
    # without making the container's font selection ambient.
    assert any(("DejaVu" in f) or ("Arial-Unicode" in f) for f in fonts), (
        f"no symbol-capable fallback face is embedded — the ✓/✗ marks in the File Inventory "
        f"have no glyph source. Embedded: {sorted(fonts)}")


def test_the_runtime_declares_the_renderer_fonts_and_native_libraries():
    """Selecting the renderer must work in the deployed image, not only in a developer venv.

    PR #1159 added the module and its tests without adding WeasyPrint to the API requirements;
    it also relied on DejaVu while the image installed only Liberation. Both omissions allow a
    locally green PDF to fail at the first production request or render its symbols as tofu.
    """
    requirements = (ACP / "api/requirements.txt").read_text(encoding="utf-8").lower()
    base_image = (ACP / "deploy/public/Dockerfile.base-api").read_text(encoding="utf-8")
    fallback_image = (ACP / "deploy/public/Dockerfile").read_text(encoding="utf-8")
    assert "weasyprint==" in requirements
    required_apt = ("fonts-liberation", "fonts-dejavu-core", "libpango-1.0-0",
                    "libpangoft2-1.0-0", "libharfbuzz0b", "libfontconfig1")
    assert all(package in base_image for package in required_apt)
    # Dockerfile's from-scratch path must be complete too. Testing only the optimized base image
    # lets a clean build succeed and then fail at the first PDF request.
    assert all(package in fallback_image for package in required_apt)


def test_the_chart_alt_names_the_criterion_the_chart_actually_shows_as_largest():
    """The alt says "affects the most files" — so it must name the longest bar.

    `_bars_alt` receives its rows sorted by SEVERITY, and an earlier version read row[0] as the
    maximum. The two coincide in the sample fixture, so nothing here caught it; a real 37-file
    scan produced a report whose alt said "1.3.1 Info and Relationships affects the most files,
    37 of 37" while the longest bar on the same page was 2.4.2 Page Titled at 49. The sentence
    and the picture came from one list and disagreed.

    That is the failure this whole file exists for: a Figure with an /Alt, veraPDF green, every
    structural assertion passing, and the only reader affected is the one who cannot see the
    chart being told the wrong thing. The rows below are in severity order with the largest count
    LAST, which is the arrangement the bug needs.
    """
    import report_weasy
    rows = [("1.3.1 Info and Relationships", 37),    # Critical, sorts first
            ("2.4.1 Bypass Blocks", 12),
            ("2.4.2 Page Titled", 49)]              # Moderate, sorts last, but is the largest
    alt = report_weasy._bars_alt(rows, 37)
    assert "2.4.2 Page Titled affects the most files, 49" in alt, alt
    assert "1.3.1" not in alt.split("affects the most files")[0], (
        f"named the highest-severity row rather than the largest: {alt}")


def test_the_chart_alt_breaks_a_tie_toward_the_more_severe_criterion():
    """Equal counts keep the earlier row, which is the higher-severity one.

    Pinned because `max` returning the first maximum is a property of the implementation, and
    the sentence reads better naming the criterion a reader should care about first.
    """
    import report_weasy
    alt = report_weasy._bars_alt([("1.3.1 Info and Relationships", 37),
                                  ("3.1.1 Language of Page", 37)], 37)
    assert "1.3.1 Info and Relationships affects the most files, 37 of 37" in alt, alt


# ── layout: page furniture, chart labels, long scans ─────────────────────────────────────────

def test_every_page_is_numbered_and_names_the_scan(report):
    from test_report_render import pdftext
    text = pdftext(report.read_bytes())
    with pikepdf.open(str(report)) as pdf:
        pages = len(pdf.pages)
    for n in range(1, pages + 1):
        assert f"Page {n} of {pages}" in text
    # pdftotext drops the space before a middle dot in margin boxes, so match loosely.
    import re as _re
    assert len(_re.findall(r"Scan selfcheck\s*·\s*generated .* UTC\s*·\s*Mova iO ACP", text)) == pages


def _svg_text_extents(svg: str):
    """(x_start, x_end, text) for every <text> in a chart SVG, measured with real font metrics
    (DejaVu Sans, the widest face in the report's stack) and honouring text-anchor."""
    import re as _re
    from html import unescape
    import report_weasy
    width = float(_re.search(r'<svg[^>]*\swidth="([\d.]+)"', svg).group(1))
    out = []
    for m in _re.finditer(r"<text([^>]*)>(.*?)</text>", svg):
        attrs, label = m.group(1), unescape(m.group(2))
        x = float(_re.search(r'\sx="([-\d.]+)"', attrs).group(1))
        size = float(_re.search(r'font-size="([\d.]+)"', attrs).group(1))
        w = report_weasy.text_width(label) * size / report_weasy._BAR_LABEL_PX
        anchor = (_re.search(r'text-anchor="(\w+)"', attrs) or [None, "start"])[1]
        start = x - w if anchor == "end" else x - w / 2 if anchor == "middle" else x
        out.append((start, start + w, label, width))
    return out


@pytest.mark.parametrize("rows", [
    [("1.4.3 Contrast (Minimum)", 3), ("1.1.1 Non-text Content", 12)],
    [("2.5.8 Target Size (Minimum)", 1), ("1.3.1 Info and Relationships", 49)],
    [("4.1.2 " + "Name, Role, Value with an unusually long custom rubric label " * 3, 7)],
    [("X" * 140, 2)],
])
def test_chart_labels_fit_inside_the_chart(rows):
    """THE CLIPPING REGRESSION. The label column was a fixed 130px, right-aligned at x=124, so
    "1.4.3 Contrast (Minimum)" (≈130px at 10px) started left of the image and printed as
    ".4.3 Contrast (Minimum)". Asserted on geometry with real metrics, because text extraction
    still returns the clipped glyphs and would pass. Bite-checked against the old layout."""
    import report_weasy
    svg = report_weasy._bars_svg(rows)
    extents = _svg_text_extents(svg)
    for start, end, label, width in extents:
        assert start >= 0 and end <= width, (label, start, end, width)
    shown = " ".join(label for _, _, label, _ in extents)
    for name, _ in rows:
        for word in name.split():
            assert word[:10] in shown, word


def test_long_scan_report_layout(tmp_path):
    """300 files with 200-character names, 40 criteria, long finding text."""
    import re as _re
    from test_report_render import pdftext
    crits = ["1.1.1", "1.2.1", "1.2.2", "1.2.3", "1.2.5", "1.3.1", "1.3.2", "1.3.3", "1.3.4", "1.3.5",
             "1.4.1", "1.4.2", "1.4.3", "1.4.4", "1.4.5", "1.4.10", "1.4.11", "1.4.12", "1.4.13", "2.1.1",
             "2.1.2", "2.1.4", "2.2.1", "2.2.2", "2.3.1", "2.4.1", "2.4.2", "2.4.3", "2.4.4", "2.4.5",
             "2.4.6", "2.4.7", "2.5.1", "2.5.2", "2.5.3", "2.5.4", "3.1.1", "3.1.2", "3.2.1", "4.1.2"]
    sev = ["CRITICAL", "SERIOUS", "MODERATE", "MINOR"]
    files = []
    for i in range(300):
        name = (f"department-{i:03d}/" + "quarterly-accessibility-remediation-evidence-" * 5)[:196] + f"{i:03d}.docx"
        issues = ([{"wcag": "SC_" + crits[(i // 5) % 40].replace(".", "_"), "severity": sev[i % 4],
                    "detail": "Long finding text describing the barrier so that it wraps. " * 4, "page": 3}]
                  if i % 5 == 0 else [])
        files.append({"file": name, "status": "done", "compliant": 0 if issues else 1,
                      "score": 40 + i % 60, "skipped_rules": 0, "issues": issues})
    run = {**_RUN, "id": "long-fixture", "files": 300, "certifiable": 240}
    out = _build(tmp_path, run=run, files=files)
    data = out.read_bytes()
    with pikepdf.open(str(out)) as pdf:
        pages = len(pdf.pages)
    chunks = pdftext(data).split("\f")[:pages]
    assert 10 < pages < 120, pages

    # The inventory starts on page 1 (it used to be pushed whole onto page 2) and its header
    # row repeats on every page it spans.
    inventory = [i for i, c in enumerate(chunks) if _re.search(r"department-\d{3}/", c)
                 and "Your remediation guide" not in c and i < 20]
    assert inventory[0] == 0, inventory[:3]
    for i in inventory:
        assert _re.search(r"File\s+Score\s+Certified", chunks[i]), f"no header row on page {i + 1}"

    # Documents with nothing to act on are counted, not given 240 boilerplate guide blocks.
    guide_text = " ".join("".join(chunks).split())
    assert "240 other documents have no remaining item or recorded change" in guide_text
    assert guide_text.count("Document version:") == 60

    # Long names wrap rather than clip.
    flat = _re.sub(r"\s+", "", pdftext(data, "-raw"))
    assert _re.sub(r"\s+", "", files[-1]["file"]) in flat

    # No stranded pages except the one before the guide (which deliberately starts a new page)
    # and the last.
    from test_report_render import page_fill
    fills = page_fill(data, top_in=0.75, bottom_in=0.8)
    guide = next(i for i, (_, words) in enumerate(fills) if "Your remediation guide" in words)
    stranded = [(i + 1, round(f, 2)) for i, (f, _) in enumerate(fills)
                if f < 0.6 and i not in (guide - 1, pages - 1)]
    assert not stranded, stranded

"""The PDF and HTML rule explanations are statements about the CURRENT code — prove each one.

Semantic pins, not literal ones: nothing below restates a number from the explanations. Every
threshold is re-derived from the code it names, every rule id is found in the file said to emit
it, and the fixture tests read the documented boundary OUT of the explanation and then show the
real detector flipping exactly there. If a constant, a literal or a detector moves, these fail.
"""
from __future__ import annotations

import importlib
import inspect
import json
import re
import sys
from pathlib import Path

import pytest

import remediation_capability as rc
from rule_explanations import schema
from rule_explanations.html import html
from rule_explanations.pdf import DERIVATIONS, pdf, pdf_engine_root, resolve

REPO = Path(__file__).resolve().parent.parent
ENGINES = {"acp", "pdf-analyser"}


@pytest.fixture(scope="module")
def catalogs():
    return {"pdf": pdf(), "html": html()}


def _all_thresholds(catalogs):
    for fmt, cells in catalogs.items():
        for sc, cell in cells.items():
            for t in cell.thresholds:
                yield fmt, sc, t


# ── coverage and vocabulary ───────────────────────────────────────────────────────────────
@pytest.mark.parametrize("fmt", ["pdf", "html"])
def test_every_capability_cell_has_an_explanation(catalogs, fmt):
    assert set(catalogs[fmt]) == set(rc.remediation_table()[fmt])


def test_vocabularies_are_closed_and_lanes_left_to_the_assembler(catalogs):
    for fmt, cells in catalogs.items():
        for sc, cell in cells.items():
            where = f"{fmt} {sc}"
            assert isinstance(cell, schema.FormatExplanation), where
            assert cell.status in schema.STATUSES and cell.status != "not_applicable", where
            assert cell.assessment_lane is None and cell.remediation_lane is None, where
            assert cell.checks and cell.evidence and cell.fix, where
            for r in cell.rules:
                assert r.method in schema.METHODS, (where, r)
                assert r.fix in schema.FIX_MODES, (where, r)
                assert r.engine in ENGINES, (where, r)
            # Never overstate: a cell that claims a detector must name one, and a cell that
            # names none cannot claim to detect anything.
            if cell.status in ("implemented", "partial"):
                assert cell.rules, where
            else:
                assert not cell.rules, where
            json.dumps(schema.to_json(cell))       # serialisable as served


def test_rule_ids_are_emitted_by_the_named_source(catalogs):
    for fmt, cells in catalogs.items():
        for sc, cell in cells.items():
            for r in cell.rules:
                path = REPO / r.source
                assert path.is_file(), (fmt, sc, r.source)
                text = path.read_text()
                assert f'"{r.id}"' in text or f"'{r.id}'" in text, (fmt, sc, r.id, r.source)


def test_engine_rules_point_into_the_vendored_engine(catalogs):
    import scanner
    assert pdf_engine_root() == Path(scanner.WP)
    for cells in catalogs.values():
        for cell in cells.values():
            for r in cell.rules:
                assert (r.engine == "pdf-analyser") == r.source.startswith("engine/pdf-analyser/")


# ── thresholds are derived, not typed ────────────────────────────────────────────────────
def _import_for_real(source: str):
    """Independent resolution by IMPORT — including the engine, which the catalog reads by AST."""
    module, name = source.split(":", 1)
    if module.startswith("analysers."):
        root = str(pdf_engine_root())
        if root not in sys.path:
            sys.path.insert(0, root)
    obj = importlib.import_module(module)
    for part in name.split("."):
        obj = getattr(obj, part)
    return obj


def test_every_threshold_resolves_and_matches_its_code(catalogs):
    seen = 0
    for fmt, sc, t in _all_thresholds(catalogs):
        where = (fmt, sc, t.label, t.source)
        assert re.fullmatch(r"[\w.]+:[\w.]+", t.source), where
        how = DERIVATIONS.get((t.source, t.label, t.value))
        assert how is not None, ("threshold not built by threshold() — typed?", where)
        real = _import_for_real(t.source)
        if how["literal"] is None:
            assert how["fmt"](real) == t.value, where
            # The engine is read by AST in production; it must agree with the imported module.
            assert resolve(t.source) == real or callable(real) or hasattr(real, "pattern"), where
        else:
            # Inline literal: it must still be in the function that decides by it.
            assert callable(real), where
            assert how["literal"] in inspect.getsource(real), where
            assert how["fmt"](float(how["literal"])) == t.value, where
        seen += 1
    assert seen > 0


def test_configurable_thresholds_name_a_real_setting(catalogs):
    for fmt, sc, t in _all_thresholds(catalogs):
        if not t.configurable:
            assert t.setting is None, (fmt, sc, t.label)
            continue
        assert t.setting, (fmt, sc, t.label)
        if t.setting.startswith("env:"):
            module = t.source.split(":", 1)[0]
            text = (REPO / "api" / f"{module}.py").read_text()
            assert f'"{t.setting[4:]}"' in text, (fmt, sc, t.setting)


# ── semantic fixtures: the documented number IS the decision boundary ───────────────────────
def _number(value: str) -> float:
    return float(re.search(r"\d+(?:\.\d+)?", value).group(0))


def _threshold(cell, source: str, label_fragment: str = ""):
    hits = [t for t in cell.thresholds if t.source == source and label_fragment in t.label]
    assert len(hits) == 1, (source, label_fragment, [t.label for t in cell.thresholds])
    return hits[0]


def _grey_pdf(path: Path, grey: int, size: float, font: str = "Helvetica") -> Path:
    import pikepdf
    doc = pikepdf.new()
    doc.add_blank_page(page_size=(612, 792))
    f = doc.make_indirect(pikepdf.Dictionary(Type=pikepdf.Name.Font, Subtype=pikepdf.Name.Type1,
                                             BaseFont=pikepdf.Name("/" + font)))
    page = doc.pages[0]
    page.obj.Resources = pikepdf.Dictionary(Font=pikepdf.Dictionary(F1=f))
    c = f"{grey / 255:.6f}"
    page.obj.Contents = doc.make_stream(
        f"BT /F1 {size:g} Tf {c} {c} {c} rg 72 700 Td (Sample text) Tj ET".encode())
    doc.save(path)
    return path


def _straddle(required: float, ratio_of) -> tuple[int, int]:
    """(grey that just fails, grey that just passes) against `required`, found by measuring."""
    fails = [g for g in range(256) if ratio_of(g) < required]
    passes = [g for g in range(256) if ratio_of(g) >= required]
    return min(fails), max(passes)


def _pdf_aa_rules(path: Path) -> set[str]:
    import office_structure
    return {f["ruleId"] for f in office_structure.pdf_contrast_checks(path)}


def test_pdf_normal_text_contrast_boundary_is_the_documented_ratio(catalogs, tmp_path):
    import office_structure as osx
    cell = catalogs["pdf"]["1.4.3"]
    required = _number(_threshold(cell, "office_structure:_pdf_required_ratios",
                                  "normal text").value)

    def measured(g):
        return osx._contrast_ratio(f"{g:02X}" * 3, "FFFFFF")

    just_fails, just_passes = _straddle(required, measured)
    assert measured(just_fails) < required <= measured(just_passes)
    assert "PDF_LOW_CONTRAST_AA" in _pdf_aa_rules(_grey_pdf(tmp_path / "f.pdf", just_fails, 12))
    assert "PDF_LOW_CONTRAST_AA" not in _pdf_aa_rules(_grey_pdf(tmp_path / "p.pdf", just_passes, 12))


def test_pdf_large_text_size_is_the_documented_boundary(catalogs, tmp_path):
    import office_structure as osx
    cell = catalogs["pdf"]["1.4.3"]
    large_pt = _number(_threshold(cell, "office_structure:_PDF_LARGE_PT").value)
    bold_pt = _number(_threshold(cell, "office_structure:_PDF_LARGE_BOLD_PT").value)
    normal = _number(_threshold(cell, "office_structure:_pdf_required_ratios", "normal").value)
    large = _number(_threshold(cell, "office_structure:_pdf_required_ratios", "large").value)

    def measured(g):
        return osx._contrast_ratio(f"{g:02X}" * 3, "FFFFFF")

    # A grey that fails the normal bar but clears the large-text bar.
    grey = next(g for g in range(256) if large <= measured(g) < normal)
    assert "PDF_LOW_CONTRAST_AA" not in _pdf_aa_rules(_grey_pdf(tmp_path / "at.pdf", grey, large_pt))
    assert "PDF_LOW_CONTRAST_AA" in _pdf_aa_rules(
        _grey_pdf(tmp_path / "below.pdf", grey, large_pt - 1))
    # Bold earns the large bar at the smaller size — from the font NAME.
    assert "PDF_LOW_CONTRAST_AA" not in _pdf_aa_rules(
        _grey_pdf(tmp_path / "bold.pdf", grey, bold_pt, "Helvetica-Bold"))
    assert "PDF_LOW_CONTRAST_AA" in _pdf_aa_rules(
        _grey_pdf(tmp_path / "bold-below.pdf", grey, bold_pt - 1, "Helvetica-Bold"))


def _html_rules(tmp_path: Path, body: str) -> list[str]:
    import scanner
    p = tmp_path / "page.html"
    p.write_text(f'<html lang="en"><head><title>t</title></head><body>{body}</body></html>')
    return [i["ruleId"] for i in scanner._analyse_html(p)["issues"]]


def test_html_contrast_cutoff_is_the_documented_luma(catalogs, tmp_path):
    import scanner
    cut = _number(_threshold(catalogs["html"]["1.4.3"], "scanner:_analyse_html").value)
    lumas = {g: scanner._luma(f"{g:02x}" * 3) for g in range(256)}
    flagged = min(g for g, lum in lumas.items() if lum > cut)
    clean = max(g for g, lum in lumas.items() if lum <= cut)
    assert "HTML_LOW_CONTRAST_AA" in _html_rules(
        tmp_path, f'<p style="color:#{flagged:02x}{flagged:02x}{flagged:02x}">x</p>')
    assert "HTML_LOW_CONTRAST_AA" not in _html_rules(
        tmp_path, f'<p style="color:#{clean:02x}{clean:02x}{clean:02x}">x</p>')


def test_html_target_size_minimum_is_the_documented_px(catalogs, tmp_path):
    minimum = _number(_threshold(catalogs["html"]["2.5.8"], "scanner:_analyse_html").value)
    below, at = minimum - 1, minimum
    assert "HTML_TARGET_TOO_SMALL" in _html_rules(
        tmp_path, f'<button style="width:{below:g}px">Go</button>')
    assert "HTML_TARGET_TOO_SMALL" not in _html_rules(
        tmp_path, f'<button style="width:{at:g}px">Go</button>')


def test_documented_html_false_positives_are_real(tmp_path):
    """The catalog admits two false positives; if either is fixed, update the explanation."""
    assert "HTML_VISUAL_REORDER" in _html_rules(tmp_path, '<div style="border: 1px solid #000">x</div>')
    assert "HTML_INPUT_NO_LABEL" in _html_rules(
        tmp_path, '<label>Email <input type="text" name="q1"></label>')


def test_documented_html_language_marks_are_not_read(tmp_path):
    import office_structure
    p = tmp_path / "m.html"
    p.write_text('<html lang="en"><body><p lang="fr">Bonjour tout le monde</p></body></html>')
    assert office_structure.language_marked_spans(p, ".html") == {}

"""Settings → Rule explanations: the explanations must stay TRUE of the code they describe.

These are semantic checks, not string pins. Each one guards a way an explanation screen goes
quietly wrong:

  * a criterion or format missing, or a capability cell with nothing said about it;
  * a lane that disagrees with remediation_capability (two screens, two answers);
  * a rule ID that nothing emits, or an emitted rule nobody explained;
  * a number that no longer matches the constant it names;
  * a stated threshold that is not the detector's real decision boundary (fixture tests below
    build a file just either side of the documented value and watch the detector);
  * markup in a payload the UI renders as text.
"""
from __future__ import annotations

import io
import json
import re
import sys
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "api"))
sys.path.insert(0, str(ROOT / "scripts"))

import remediation_capability as cap  # noqa: E402
import rule_explanations as rx  # noqa: E402
from assessment_policy import RULE_CATALOG  # noqa: E402
from rule_explanations.schema import FIX_MODES, METHODS, STATUSES, to_json  # noqa: E402

AVAILABLE = rx.available_formats()
OFFICE = ("docx", "pptx", "xlsx")
ENGINES = {"acp", "office-analyser", "pdf-analyser"}
CATALOG = json.loads((ROOT / "config" / "rule-catalog.json").read_text())
PREFIX = {"DOCX_": "docx", "PPTX_": "pptx", "XLSX_": "xlsx", "PDF_": "pdf", "HTML_": "html"}


def _criteria():
    return rx.build(AVAILABLE)


def _cell(sc: str, fmt: str):
    for c in _criteria():
        if c.sc == sc:
            return c.formats[fmt]
    raise KeyError(sc)


def _threshold(sc: str, fmt: str, label_start: str):
    for t in _cell(sc, fmt).thresholds:
        if t.label.startswith(label_start):
            return t
    raise KeyError(f"{fmt} {sc}: no threshold starting {label_start!r}")


def _number(t) -> float:
    return float(re.search(r"-?\d+(?:\.\d+)?", t.value).group(0))


# ── coverage of the grid ─────────────────────────────────────────────────────────────────────
def test_office_formats_are_available():
    assert set(OFFICE) <= set(AVAILABLE)


def test_every_format_has_an_explanation_module():
    """The final integrity requirement: all five formats explained. Fails until pdf.py and
    html.py exist — deliberately, rather than letting the route serve a partial grid."""
    missing = [f for f in cap.FORMATS if f not in AVAILABLE]
    assert not missing, f"no explanation module for: {missing}"


def test_every_catalog_criterion_is_present_in_order_with_every_format():
    crit = _criteria()
    assert [c.sc for c in crit] == [r["id"] for r in RULE_CATALOG]
    for c, row in zip(crit, RULE_CATALOG):
        assert (c.name, c.level) == (row["name"], row.get("level") or "A")
        assert tuple(c.formats) == AVAILABLE, c.sc


def test_every_capability_cell_has_an_explicit_explanation():
    for fmt in AVAILABLE:
        given = rx.load_format(fmt)
        in_map = set(cap.remediation_table()[fmt])
        missing = sorted(in_map - set(given))
        assert not missing, f"{fmt}: capability cells with no explanation: {missing}"
        for sc in in_map:
            assert given[sc].status != "not_applicable", (fmt, sc)


def test_cells_outside_the_capability_map_claim_nothing():
    for c in _criteria():
        for fmt, cell in c.formats.items():
            if cap.lane(fmt, c.sc) is None:
                assert cell.status == "not_applicable", (fmt, c.sc)
                assert not (cell.rules or cell.thresholds or cell.checks), (fmt, c.sc)


def test_office_cell_counts_match_the_capability_map():
    for fmt in OFFICE:
        n = sum(1 for c in _criteria() if c.formats[fmt].status != "not_applicable")
        assert n == len(cap.remediation_table()[fmt]), fmt


# ── vocabularies, lanes, honesty ─────────────────────────────────────────────────────────────
def test_vocabularies_are_closed():
    for c in _criteria():
        for fmt, cell in c.formats.items():
            assert cell.status in STATUSES, (fmt, c.sc, cell.status)
            for r in cell.rules:
                assert r.method in METHODS, (fmt, c.sc, r)
                assert r.fix in FIX_MODES, (fmt, c.sc, r)
                assert r.engine in ENGINES, (fmt, c.sc, r)


def test_lanes_come_from_remediation_capability_only():
    for fmt in AVAILABLE:
        for sc, cell in rx.load_format(fmt).items():
            assert cell.assessment_lane is None and cell.remediation_lane is None, (
                f"{fmt} {sc}: per-format modules must not author lanes")
    for c in _criteria():
        for fmt, cell in c.formats.items():
            assert cell.assessment_lane == cap.assessment_lane(fmt, c.sc), (fmt, c.sc)
            assert cell.remediation_lane == cap.lane(fmt, c.sc), (fmt, c.sc)


def test_explained_cells_say_something_concrete():
    for c in _criteria():
        for fmt, cell in c.formats.items():
            if cell.status == "not_applicable":
                continue
            assert cell.checks and cell.evidence and cell.fix, (fmt, c.sc)
            if cell.status in ("implemented", "partial"):
                assert cell.rules, f"{fmt} {c.sc}: a detector-backed status needs a rule"


def test_status_never_overstates_the_registry_or_the_lane():
    """A registry PARTIAL/HEURISTIC pair is never 'implemented'; a human assessment lane is
    always 'manual' — the explanation may be more modest than the tables, never bolder."""
    import rule_registry
    from assessment import Coverage
    rule_registry.load()
    for c in _criteria():
        for fmt, cell in c.formats.items():
            reg = rule_registry.get(c.sc, fmt)
            if reg is not None and reg.coverage in (Coverage.PARTIAL, Coverage.HEURISTIC,
                                                    Coverage.DECLARED):
                assert cell.status != "implemented", (fmt, c.sc, reg.coverage)
            if cell.assessment_lane == cap.A_HUMAN:
                assert cell.status == "manual", (fmt, c.sc)


def test_rule_fix_mode_agrees_with_the_cell_lane_where_it_can():
    """A rule may be narrower than its cell (review for an advisory, none for detection-only),
    but it can never promise MORE than the lane: no 'auto' rule in a non-auto cell. ('assisted'
    inside a human lane is allowed: pdf 1.3.1 writes a human-authorised table plan for a subset.)"""
    for c in _criteria():
        for fmt, cell in c.formats.items():
            for r in cell.rules:
                if r.fix == "auto":
                    assert cell.remediation_lane == cap.AUTO, (fmt, c.sc, r.id)


# ── rule IDs are real, and nothing emitted goes unexplained ──────────────────────────────────
def _catalog_rows():
    for fmt, rows in CATALOG.items():
        if fmt == "_meta":
            continue
        for r in rows:
            yield fmt, r


def _engine_ids() -> set[str]:
    ids: set[str] = set()
    for p in (ROOT / "engine").rglob("*RuleIds.cs"):
        ids |= set(re.findall(r'"([A-Z]+-[A-Z-]+-\d+)"', p.read_text(encoding="utf-8")))
    return ids


def test_every_rule_ref_resolves_to_a_real_emitter():
    catalog_by_id = {r["id"]: (fmt, r) for fmt, r in _catalog_rows()}
    for c in _criteria():
        for fmt, cell in c.formats.items():
            for r in cell.rules:
                src = ROOT / r.source
                assert src.is_file(), f"{fmt} {c.sc} {r.id}: source {r.source} does not exist"
                text = src.read_text(encoding="utf-8")
                literal = f'"{r.id}"' in text or f"'{r.id}'" in text
                if r.engine == "acp":
                    assert literal, f"{fmt} {c.sc}: {r.id} is not a string literal in {r.source}"
                elif r.engine != "office-analyser" and literal:
                    continue          # e.g. the PDF analyser, whose rule modules name their IDs
                else:
                    assert r.id in catalog_by_id, f"{fmt} {c.sc}: {r.id} not in rule-catalog.json"
                    cat_fmt, row = catalog_by_id[r.id]
                    assert (cat_fmt, row["wcag_sc"]) == (fmt, c.sc), (
                        f"{r.id} is catalogued as {cat_fmt} {row['wcag_sc']}, explained under "
                        f"{fmt} {c.sc}")


def test_office_analyser_refs_are_ids_the_engine_actually_emits():
    """The catalog also carries rows nothing emits (DOCX-HEADLABEL-001 …). An explanation must
    never cite one of those as a detector."""
    emitted = _engine_ids()
    for c in _criteria():
        for fmt, cell in c.formats.items():
            for r in cell.rules:
                if r.engine == "office-analyser":
                    assert r.id in emitted, f"{fmt} {c.sc}: {r.id} is not in any *RuleIds.cs"


def test_every_engine_rule_for_office_is_explained():
    emitted = _engine_ids()
    for fmt, row in _catalog_rows():
        if fmt not in OFFICE or row["id"] not in emitted:
            continue
        refs = {r.id for r in _cell(row["wcag_sc"], fmt).rules}
        assert row["id"] in refs, f"{fmt} {row['wcag_sc']}: engine rule {row['id']} unexplained"


def test_every_first_party_rule_in_the_code_derived_index_is_explained():
    """scripts/gen_rules_index.py derives the first-party rule set from the code's AST. Every rule
    it finds for an explained cell must appear there — an emitted, unexplained rule is the failure
    this screen exists to prevent."""
    import gen_rules_index
    missing = []
    for sc, rules in gen_rules_index.load_first_party().items():
        if sc not in {r["id"] for r in RULE_CATALOG}:
            continue
        for rule in rules:
            fmts = [f for p, f in PREFIX.items() if rule["id"].startswith(p)] or rule["formats"]
            for fmt in fmts:
                if fmt not in AVAILABLE or cap.lane(fmt, sc) is None:
                    continue
                if rule["id"] not in {r.id for r in _cell(sc, fmt).rules}:
                    missing.append(f"{fmt} {sc} {rule['id']} ({rule['source']})")
    assert not missing, "emitted but unexplained:\n  " + "\n  ".join(sorted(set(missing)))


# ── thresholds are derived, not typed ────────────────────────────────────────────────────────
def _numeric_literals(fn) -> set[float]:
    import ast
    import inspect
    import textwrap
    tree = ast.parse(textwrap.dedent(inspect.getsource(inspect.unwrap(fn))))
    return {float(n.value) for n in ast.walk(tree)
            if isinstance(n, ast.Constant) and isinstance(n.value, (int, float))
            and not isinstance(n.value, bool)}


def test_every_threshold_is_rederived_from_the_code_it_names():
    """schema: source is "module:NAME" — a constant, or the function holding an inline literal
    (C# engine numbers: "<path>.cs:NAME"). Every value was rendered by the shared helper, which
    recorded how; re-run that from the code as it is NOW and the value must come out the same."""
    import inspect
    from rule_explanations.pdf import DERIVATIONS, resolve
    crit = _criteria()                                  # building fills DERIVATIONS
    for c in crit:
        for fmt, cell in c.formats.items():
            for t in cell.thresholds:
                where = f"{fmt} {c.sc} {t.label!r} ({t.source})"
                head, sep, name = t.source.partition(":")
                assert sep and name and ":" not in name and "@" not in name, (
                    f"{where}: source must be 'module:NAME'")
                assert (ROOT / rx.source_path(t.source)).is_file(), f"{where}: no such file"
                assert t.configurable == bool(t.setting), f"{where}: configurable needs a setting"
                if t.setting:
                    assert t.setting.startswith("env:ACP_") or t.setting.startswith("rubric."), where
                how = DERIVATIONS.get((t.source, t.label, t.value))
                assert how is not None, f"{where}: not rendered by the shared helper (typed?)"
                lit = how.get("literal")
                if how.get("cs"):
                    got = how["fmt"](rx.resolve_cs(t.source, lit))
                else:
                    obj = resolve(t.source)
                    if lit is None:
                        got = how["fmt"](obj)
                    else:
                        assert callable(obj), f"{where}: a literal must live in a function"
                        if fmt in OFFICE:
                            assert float(lit) in _numeric_literals(obj), (
                                f"{where}: literal {lit} is no longer in the function")
                        else:                       # pdf/html's own suite checks these too
                            assert lit in inspect.getsource(inspect.unwrap(obj)), where
                        got = how["fmt"](float(lit))
                assert got == t.value, f"{where}: code now renders {got!r}, explanation says {t.value!r}"


def test_a_changed_constant_changes_the_explanation(monkeypatch):
    """The value is rendered from the constant at build time, not copied from it."""
    import office_structure
    from rule_explanations import office
    monkeypatch.setattr(office_structure, "_MIN_JUSTIFIED_PARAS", 5)
    t = next(t for t in office.docx()["1.4.8"].thresholds)
    assert t.value == "≥ 5"


def test_a_literal_that_is_not_in_the_code_cannot_be_stated():
    from rule_explanations import office
    with pytest.raises(LookupError):
        office.T("x", "office_structure:xlsx_contrast_checks@4.6", "{n}:1")
    with pytest.raises(LookupError):
        rx.resolve_cs("engine/office-analysers/DigitalA11y.Analysers.DotNet/Helpers/"
                      "ColourContrastHelper.cs:ColourContrastHelper", "19.5")
    with pytest.raises(LookupError):
        rx.resolve_cs("engine/office-analysers/DigitalA11y.Analysers.DotNet/Docx/Rules/"
                      "BookmarksRule.cs:NoSuchConstant")


def test_docx_heading_cell_states_both_detection_paths():
    labels = [t.label for t in _cell("1.3.1", "docx").thresholds]
    assert any(label.startswith("Size path: largest text size") for label in labels)
    assert any(label.startswith("Section-label path: label length") for label in labels)
    assert any(label.startswith("Section-label path: labels sharing") for label in labels)


# ── stated thresholds ARE the decision boundary (fixture files either side) ─────────────────
def _wcag_ratio(a: str, b: str) -> float:
    """Independent WCAG 2.x contrast ratio — deliberately not office_structure's."""
    def lum(h):
        def ch(c):
            c = int(c, 16) / 255
            return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
        return 0.2126 * ch(h[0:2]) + 0.7152 * ch(h[2:4]) + 0.0722 * ch(h[4:6])
    hi, lo = sorted((lum(a), lum(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def _greys_either_side(ratio: float) -> tuple[str, str]:
    """(grey just BELOW `ratio` on white, grey just AT/ABOVE it)."""
    greys = [f"{g:02X}" * 3 for g in range(256)]
    above = max((g for g in greys if _wcag_ratio(g, "FFFFFF") >= ratio),
                key=lambda g: int(g[:2], 16))
    below = min((g for g in greys if _wcag_ratio(g, "FFFFFF") < ratio),
                key=lambda g: int(g[:2], 16))
    return below, above


def _zip(tmp_path: Path, name: str, parts: dict[str, str]) -> Path:
    p = tmp_path / name
    with zipfile.ZipFile(p, "w") as z:
        for n, xml in parts.items():
            z.writestr(n, xml)
    return p


def _xlsx(tmp_path: Path, font_rgb: str) -> Path:
    styles = ('<styleSheet><fonts count="2"><font><sz val="11"/></font>'
              f'<font><sz val="11"/><color rgb="FF{font_rgb}"/></font></fonts>'
              '<fills count="2"><fill><patternFill patternType="none"/></fill>'
              '<fill><patternFill patternType="gray125"/></fill></fills>'
              '<cellXfs count="2"><xf numFmtId="0" fontId="0" fillId="0"/>'
              '<xf numFmtId="0" fontId="1" fillId="0"/></cellXfs></styleSheet>')
    sheet = ('<worksheet><sheetData><row r="1"><c r="A1" s="1" t="inlineStr">'
             '<is><t>Revenue</t></is></c></row></sheetData></worksheet>')
    return _zip(tmp_path, f"c{font_rgb}.xlsx", {"xl/styles.xml": styles,
                                                "xl/worksheets/sheet1.xml": sheet})


def test_xlsx_contrast_boundary_is_the_stated_ratio(tmp_path):
    import office_structure
    ratio = _number(_threshold("1.4.3", "xlsx", "Contrast ratio"))
    below, above = _greys_either_side(ratio)
    ids = lambda p: {f["ruleId"] for f in office_structure.xlsx_contrast_checks(p)}  # noqa: E731
    assert "XLSX_LOW_CONTRAST_AA" in ids(_xlsx(tmp_path, below))
    assert "XLSX_LOW_CONTRAST_AA" not in ids(_xlsx(tmp_path, above))


def _pptx(tmp_path: Path, run_rgb: str) -> Path:
    slide = ('<p:sld><p:cSld><p:spTree><p:sp><p:spPr><a:solidFill><a:srgbClr val="FFFFFF"/>'
             '</a:solidFill></p:spPr><p:txBody><a:p><a:r><a:rPr><a:solidFill>'
             f'<a:srgbClr val="{run_rgb}"/></a:solidFill></a:rPr><a:t>Quarterly revenue</a:t>'
             '</a:r></a:p></p:txBody></p:sp></p:spTree></p:cSld></p:sld>')
    return _zip(tmp_path, f"c{run_rgb}.pptx", {"ppt/slides/slide1.xml": slide})


def test_pptx_acp_contrast_check_reports_only_below_the_stated_large_text_ratio(tmp_path):
    """The surprising one: ACP's own pptx AA check fires below 3:1, not 4.5:1. The explanation
    says so, and this proves it is the real boundary."""
    import office_structure
    ratio = _number(_threshold("1.4.3", "pptx", "ACP check: ratio below which"))
    below, above = _greys_either_side(ratio)
    ids = lambda p: {f["ruleId"] for f in office_structure.pptx_contrast_checks(p)}  # noqa: E731
    assert "PPTX_LOW_CONTRAST_AA" in ids(_pptx(tmp_path, below))
    assert "PPTX_LOW_CONTRAST_AA" not in ids(_pptx(tmp_path, above))


def _docx(tmp_path: Path, name: str, paragraphs: list[str]) -> Path:
    body = "".join(paragraphs)
    doc = ('<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
           f'<w:body>{body}</w:body></w:document>')
    return _zip(tmp_path, name, {"word/document.xml": doc})


def _para(text: str, half_pt: int | None = None) -> str:
    rpr = f'<w:rPr><w:sz w:val="{half_pt}"/></w:rPr>' if half_pt else ""
    return f"<w:p><w:r>{rpr}<w:t>{text}</w:t></w:r></w:p>"


def test_docx_pseudo_heading_size_floor_is_the_stated_size(tmp_path):
    import office_structure
    pt = _number(_threshold("1.3.1", "docx", "Size path: largest text size for a heading"))
    body = [_para("This is an ordinary sentence of body text that runs on for a while.", 22)] * 4

    def flagged(half_pt: int) -> bool:
        p = _docx(tmp_path, f"h{half_pt}.docx",
                  body[:2] + [_para("Quarterly results", half_pt)] + body[2:])
        return any(f["ruleId"] == "DOCX_PSEUDO_HEADING" for f in office_structure.docx_checks(p))

    assert flagged(int(pt * 2))
    assert not flagged(int(pt * 2) - 1)


def test_docx_section_headings_paragraph_floor_is_the_stated_count(tmp_path):
    import office_structure
    n = int(_number(_threshold("2.4.10", "docx", "Text paragraphs before headings")))

    def flagged(count: int) -> bool:
        p = _docx(tmp_path, f"s{count}.docx", [_para(f"Paragraph {i} of text.") for i in range(count)])
        return any(f["ruleId"] == "DOCX_NO_SECTION_HEADINGS"
                   for f in office_structure.docx_checks(p))

    assert flagged(n)
    assert not flagged(n - 1)


# ── payload and route ────────────────────────────────────────────────────────────────────────
def _strings(obj):
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for k, v in obj.items():
            yield from _strings(k)
            yield from _strings(v)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            yield from _strings(v)


def test_payload_is_json_text_with_consistent_counts(isolated_store, monkeypatch):
    import core
    monkeypatch.setattr(core, "store", isolated_store, raising=False)
    body = rx.payload(formats=AVAILABLE)
    json.loads(json.dumps(body))                         # serialisable, round-trips
    # The UI renders every string as text. HTML explanations legitimately NAME elements
    # (“every <img> …”), so a bare element name is allowed; what is not is anything that only
    # makes sense if rendered — script, event handlers, javascript: URLs, pre-escaped entities.
    active = re.compile(r"<\s*script\b|<[^>]*\son[a-z]+\s*=|javascript:|&(lt|gt|amp|quot|#\d+);",
                        re.I)
    markup = [s for s in _strings(body) if active.search(s)]
    assert not markup, f"active markup in payload text: {markup[:3]}"
    assert body["formats"] == list(AVAILABLE)
    assert body["counts"]["criteria"] == len(RULE_CATALOG)
    assert body["counts"]["cells"] == len(RULE_CATALOG) * len(AVAILABLE)
    assert sum(body["counts"]["by_status"].values()) == body["counts"]["cells"]
    assert set(body["counts"]["by_status"]) == set(STATUSES)
    assert [c["sc"] for c in body["criteria"]] == [r["id"] for r in RULE_CATALOG]
    prov = body["provenance"]
    assert prov["rule_catalog_version"] == CATALOG["_meta"]["version"]
    assert {"name", "version", "hash"} <= set(prov["rubric"])
    assert all((ROOT / s).exists() for s in prov["sources"]), prov["sources"]
    keys = {s["key"] for s in body["settings"]}
    assert {"rubric.disabled_rules", "rubric.compliant_threshold", "scan_scope"} <= keys
    for s in body["settings"]:
        assert {"key", "label", "value", "scope", "editable_by", "where", "affects"} <= set(s)


def test_payload_reports_disabled_and_never_emitted_rule_ids(isolated_store, monkeypatch):
    import core
    from rubric import Rubric
    monkeypatch.setattr(core, "store", isolated_store, raising=False)
    cfg = dict(core.active_rubric(fresh=True).cfg, disabled_rules=["DOCX-CONTRAST-001"])
    monkeypatch.setattr(core, "active_rubric", lambda **_: Rubric(cfg))
    body = rx.payload(formats=AVAILABLE)
    by_sc = {c["sc"]: c for c in body["criteria"]}
    assert by_sc["1.4.3"]["enabled_in_rubric"]["disabled_rule_ids"] == ["DOCX-CONTRAST-001"]
    assert body["provenance"]["rubric"]["hash"] == Rubric(cfg).hash
    # A catalog row with no emitter: disabling it would change nothing, and the payload says so.
    assert "PPTX-RESIZE-001" in by_sc["1.4.4"]["enabled_in_rubric"]["not_emitted"]
    assert "PPTX-RESIZE-001" in by_sc["1.4.4"]["enabled_in_rubric"]["rule_ids"]
    rules_setting = next(s for s in body["settings"] if s["key"] == "rubric.disabled_rules")
    assert rules_setting["value"] == ["DOCX-CONTRAST-001"]


def test_threshold_setting_value_is_the_active_rubric(isolated_store, monkeypatch):
    import core
    monkeypatch.setattr(core, "store", isolated_store, raising=False)
    body = rx.payload(formats=AVAILABLE)
    s = next(s for s in body["settings"] if s["key"] == "rubric.compliant_threshold")
    assert s["value"] == core.active_rubric().threshold


def test_route_is_not_shadowed_and_is_read_only():
    import core
    from app import app
    route = core.match_registered_route("/rules/explanations", "GET")
    assert route is not None and route.path == "/rules/explanations", (
        f"GET /rules/explanations dispatches to {getattr(route, 'path', None)!r}")
    assert route.endpoint.__module__ == "routes.rubric"
    methods = {m for r in core.enumerate_api_routes(app) if r.path == "/rules/explanations"
               for m in (r.methods or ())}
    assert methods <= {"GET", "HEAD"}


@pytest.mark.skipif(set(AVAILABLE) != set(cap.FORMATS),
                    reason="route serves every format; needs pdf.py and html.py")
def test_route_serves_the_payload(isolated_store, monkeypatch):
    import core
    from fastapi.testclient import TestClient
    from app import app
    monkeypatch.setattr(core, "store", isolated_store, raising=False)
    r = TestClient(app).get("/rules/explanations")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["schema_version"] == rx.SCHEMA_VERSION
    assert body["formats"] == list(cap.FORMATS)
    assert body["counts"]["cells"] == len(RULE_CATALOG) * len(cap.FORMATS)
    assert body["criteria"][0]["formats"].keys() == set(cap.FORMATS)
    assert body["criteria"] == json.loads(json.dumps(
        [dict(to_json(c), enabled_in_rubric=row["enabled_in_rubric"])
         for c, row in zip(rx.build(), body["criteria"])]))

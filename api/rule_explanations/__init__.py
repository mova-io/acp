"""Settings → Rule explanations: what ACP ACTUALLY checks, per WCAG criterion and file format.

Read-only product metadata assembled from per-format modules (office.py for docx/pptx/xlsx,
pdf.py, html.py), each returning `{sc: FormatExplanation}` for the cells in that format's
capability map. This package owns the assembly; the per-format modules own the words.

THRESHOLDS ARE DERIVED, NEVER TYPED. `Threshold.source` is "module:NAME" (schema.py): NAME is the
constant, or — where the number is an inline literal — the function whose body holds it. Values are
rendered by the shared helper `rule_explanations.pdf.threshold`, which records HOW each value was
made in `pdf.DERIVATIONS` so the drift tests can re-derive every one. The .NET office analyser is
C#, so it cannot be imported: its numbers are written "<repo path>.cs:NAME" (a `const`, or the class
whose body holds the literal), recorded in the same table, and re-read by `resolve_cs` below.

WHY lanes are overlaid here and not written per format: `remediation_capability` is the single
source for both lanes, and a second copy in the explanation text is how two screens come to
disagree. Per-format modules leave `assessment_lane` / `remediation_lane` as None.

A capability cell with no explanation is an error (`ExplanationGap`), never a silent fill — an
empty cell would read as "nothing to say" about a criterion ACP does act on.
"""
from __future__ import annotations

import dataclasses
import importlib
import json
import re
from functools import lru_cache
from pathlib import Path

from rule_explanations.schema import (FIX_MODES, METHODS, SCHEMA_VERSION, STATUSES,
                                      CriterionExplanation, FormatExplanation, to_json)

REPO = Path(__file__).resolve().parents[2]
API = REPO / "api"

# format -> (module under rule_explanations, function returning {sc: FormatExplanation}).
FORMAT_MODULES: dict[str, tuple[str, str]] = {
    "docx": ("office", "docx"),
    "pptx": ("office", "pptx"),
    "xlsx": ("office", "xlsx"),
    "pdf": ("pdf", "pdf"),
    "html": ("html", "html"),
}


class ExplanationGap(RuntimeError):
    """A capability cell has no explanation, or an out-of-map cell claims coverage."""


# ── Threshold sources ─────────────────────────────────────────────────────────────────────────
_NUM = re.compile(r"-?\d+(?:\.\d+)?")


def fmt_number(x) -> str:
    """12 -> "12", 4.5 -> "4.5", 14.0 -> "14", 0.9 -> "0.9". The one rendering every value uses."""
    if isinstance(x, (bool, int)):
        return str(x)
    return f"{float(x):g}"


def is_cs_source(source: str) -> bool:
    return source.split(":", 1)[0].endswith(".cs")


def resolve_cs(source: str, literal: str | None = None) -> float:
    """A number from the C# office analyser. "<path>.cs:NAME" with no literal is a `const NAME = n;`
    in that file; with `literal`, NAME is the class whose file holds the literal as a numeric token.
    Raises when the file, the constant or the literal is gone — a source that cannot be followed is
    not a source."""
    path, name = source.split(":", 1)
    text = (REPO / path).read_text(encoding="utf-8")
    if literal is None:
        m = re.search(rf"\b{re.escape(name)}\s*=\s*(-?\d+(?:\.\d+)?)\s*;", text)
        if not m:
            raise LookupError(f"no C# constant {name!r} in {path}")
        return float(m.group(1))
    if not re.search(rf"\bclass\s+{re.escape(name)}\b", text):
        raise LookupError(f"no C# class {name!r} in {path}")
    if float(literal) not in {float(t) for t in _NUM.findall(text)}:
        raise LookupError(f"literal {literal} not found in {path}")
    return float(literal)


def source_path(source: str) -> str:
    """Repo-relative file a threshold source points into (for provenance and the drift test)."""
    head = source.split(":", 1)[0]
    if "/" in head:                                   # already a repo path (the C# engine)
        return head
    try:                                              # wherever the module really lives
        import importlib.util
        spec = importlib.util.find_spec(head)
        if spec is not None and spec.origin:
            return Path(spec.origin).resolve().relative_to(REPO).as_posix()
    except (ImportError, ValueError):
        pass
    rel = head.replace(".", "/") + ".py"
    for candidate in [API / rel, *sorted(REPO.glob(f"engine/*/{rel}"))]:
        if candidate.is_file():                       # e.g. the vendored PDF engine's modules
            return candidate.relative_to(REPO).as_posix()
    return f"api/{rel}"


# ── Assembly ──────────────────────────────────────────────────────────────────────────────────
def load_format(fmt: str) -> dict[str, FormatExplanation] | None:
    """The per-format explanations, or None when that format's module is not present in this build
    (so a partial checkout still builds the formats it has — the integrity test is what requires
    all five)."""
    mod_name, fn_name = FORMAT_MODULES[fmt]
    try:
        mod = importlib.import_module(f"rule_explanations.{mod_name}")
    except ModuleNotFoundError as e:
        if e.name == f"rule_explanations.{mod_name}":
            return None
        raise
    return getattr(mod, fn_name)()


def available_formats() -> tuple[str, ...]:
    import remediation_capability as cap
    return tuple(f for f in cap.FORMATS if load_format(f) is not None)


@lru_cache(maxsize=8)
def _build(formats: tuple[str, ...]) -> tuple[CriterionExplanation, ...]:
    import remediation_capability as cap
    from assessment_policy import RULE_CATALOG

    per_format: dict[str, dict[str, FormatExplanation]] = {}
    for fmt in formats:
        loaded = load_format(fmt)
        if loaded is None:
            raise ExplanationGap(f"no explanation module for {fmt} "
                                 f"(rule_explanations.{FORMAT_MODULES[fmt][0]})")
        per_format[fmt] = loaded

    out: list[CriterionExplanation] = []
    for row in RULE_CATALOG:
        sc = row["id"]
        cells: dict[str, FormatExplanation] = {}
        for fmt in formats:
            given = per_format[fmt].get(sc)
            rem = cap.lane(fmt, sc)
            if rem is None:
                if given is not None and given.status != "not_applicable":
                    raise ExplanationGap(f"{fmt} {sc} is outside the capability map but its "
                                         f"explanation claims status {given.status!r}")
                cells[fmt] = given or FormatExplanation(status="not_applicable")
                continue
            if given is None:
                raise ExplanationGap(f"{fmt} {sc} is in the capability map but has no explanation")
            if given.status == "not_applicable":
                raise ExplanationGap(f"{fmt} {sc} is in the capability map but explained as "
                                     "not_applicable")
            cells[fmt] = dataclasses.replace(given, assessment_lane=cap.assessment_lane(fmt, sc),
                                             remediation_lane=rem)
        # Explanations for criteria the catalog does not list would never be shown; say so.
        out.append(CriterionExplanation(sc=sc, name=row["name"], level=row.get("level") or "A",
                                        formats=cells))
    known = {r["id"] for r in RULE_CATALOG}
    for fmt, table in per_format.items():
        stray = sorted(set(table) - known)
        if stray:
            raise ExplanationGap(f"{fmt} explains criteria not in RULE_CATALOG: {stray}")
    return tuple(out)


def build(formats: tuple[str, ...] | None = None) -> list[CriterionExplanation]:
    """Every RULE_CATALOG criterion, in catalog order, with a cell for every format in
    remediation_capability.FORMATS (or the given subset). Static per process, so cached."""
    import remediation_capability as cap
    return list(_build(tuple(formats) if formats is not None else tuple(cap.FORMATS)))


# ── Payload (the route) ──────────────────────────────────────────────────────────────────────
@lru_cache(maxsize=1)
def _catalog() -> dict:
    return json.loads((REPO / "config" / "rule-catalog.json").read_text(encoding="utf-8"))


def catalog_ids_by_sc() -> dict[str, list[str]]:
    by: dict[str, list[str]] = {}
    for fmt, rows in _catalog().items():
        if fmt == "_meta" or not isinstance(rows, list):
            continue
        for r in rows:
            by.setdefault(str(r.get("wcag_sc")), []).append(r["id"])
    return by


def sources(criteria) -> list[str]:
    """Repo-relative files the explanations were derived from: every emitting module and every
    threshold's file, plus the tables the lanes and catalog come from."""
    out = {"api/remediation_capability.py", "api/assessment_policy.py", "config/rule-catalog.json",
           "api/rule_explanations/schema.py"}
    for fmt in FORMAT_MODULES:
        mod, _ = FORMAT_MODULES[fmt]
        if (API / "rule_explanations" / f"{mod}.py").exists():
            out.add(f"api/rule_explanations/{mod}.py")
    for c in criteria:
        for cell in c.formats.values():
            out.update(r.source for r in cell.rules)
            out.update(source_path(t.source) for t in cell.thresholds)
    return sorted(out)


def _safe(fn, default=None):
    try:
        return fn()
    except Exception:
        return default


def settings(rubric, *, user: str | None = None) -> list[dict]:
    """What an administrator can change TODAY that alters what is detected or how it is scored —
    read from the code paths that apply each one, with its current value where one exists."""
    import core
    import ocr
    from assessment_policy import SCOPE_PRESETS, config_target

    store = core.store
    scope_rules = _safe(lambda: store.list_scope_rules(enabled_only=True), None)
    out = [
        {"key": "rubric.disabled_rules", "label": "Disabled rules",
         "value": sorted(rubric.disabled), "scope": "platform (every assessment)",
         "editable_by": "Platform admin (owner)", "where": "PUT /rubric",
         "affects": ["Findings whose rule ID is listed are dropped before scoring (exact rule-ID "
                     "match, applied in scanner.py). The rules list offers catalog IDs only; "
                     "first-party rule IDs are matched too if listed."]},
        {"key": "rubric.compliant_threshold", "label": "Compliance score threshold",
         "value": rubric.threshold, "scope": "platform (every assessment)",
         "editable_by": "Platform admin (owner)", "where": "PUT /rubric",
         "affects": ["The minimum score a fully analysed file needs to count as compliant. "
                     "Changes scoring only, not what any detector checks."]},
        {"key": "rubric.severity_weights", "label": "Severity penalty weights",
         "value": dict(rubric.weights), "scope": "platform (every assessment)",
         "editable_by": "No API — carried in the stored rubric JSON (setting rubric_active) or "
                        "config/rubric.default.json",
         "where": "rubric configuration",
         "affects": ["Points deducted per finding by severity. REVIEW findings weigh 0, so "
                     "advisory findings never lower a score."]},
        {"key": "rubric.conformance_target", "label": "Conformance target",
         "value": _safe(config_target, None), "scope": "platform",
         "editable_by": "No API — read from config/rubric.default.json at process start",
         "where": "config/rubric.default.json",
         "affects": ["Criteria above the target level (AAA at an AA target) are excluded from "
                     "counts; their raw findings stay on the record."]},
        {"key": "scan_scope", "label": "Default assessment scope (criteria × formats)",
         "value": _safe(lambda: store.get_setting("scan_scope", "") or "", ""),
         "scope": "platform default for new assessments",
         "editable_by": "Platform admin (owner)", "where": "PUT /settings",
         "affects": [f"A preset ({', '.join(sorted(SCOPE_PRESETS))}) or a JSON map of criteria "
                     "to formats. A detector whose criteria are all out of scope does not run at "
                     "all; empty means no restriction."]},
        {"key": "scan_scope (per user)", "label": "Your own assessment scope override",
         "value": (_safe(lambda: store.get_user_setting(user, "scan_scope"), None) if user else None),
         "scope": "per user (widen-only)", "editable_by": "Each signed-in user, for themselves",
         "where": "PUT /settings/mine",
         "affects": ["Adds criteria or formats to the platform default for that user's "
                     "assessments; it can never remove one the default requires."]},
        {"key": "scans.{scan_id}.assessment_scope", "label": "This assessment's criteria",
         "value": None, "scope": "per assessment",
         "editable_by": "The scan's owner (not while its work is running)",
         "where": "PUT /scans/{scan_id}/assessment-scope",
         "affects": ["Frozen into the scan record; detectors for unselected criteria are skipped "
                     "for that scan's files."]},
        {"key": "scope_rules", "label": "Per-file scope rules",
         "value": (len(scope_rules) if isinstance(scope_rules, list) else None),
         "scope": "per file (by folder, owner, department or content type)",
         "editable_by": "Platform admin (owner)", "where": "POST/PATCH/DELETE /scope/rules",
         "affects": ["Assigns a subset of the Core-17 criteria to matching files; the value shown "
                     "is the number of enabled rules."]},
        {"key": "assess.level", "label": "Assessment level (A or AA)", "value": None,
         "scope": "per assessment run", "editable_by": "The scan's owner",
         "where": "POST /scans/{scan_id}/assess?level=",
         "affects": ["Which findings count as blocking in the assessment trace. Does not change "
                     "what detectors run."]},
        {"key": "ACP_DETECT_IMAGES_OF_TEXT / ACP_OCR_*", "label": "Images-of-text (OCR) limits",
         "value": {"ACP_OCR_MIN_WORDS": ocr._MIN_WORDS, "ACP_OCR_MIN_PIXELS": ocr._MIN_PIXELS,
                   "ACP_OCR_MIN_WORDS_STRICT": ocr._MIN_WORDS_STRICT,
                   "ACP_OCR_MIN_PIXELS_STRICT": ocr._MIN_PIXELS_STRICT,
                   "ACP_OCR_MAX_IMAGES": ocr._MAX_IMAGES, "ACP_OCR_TIMEOUT_S": ocr._OCR_TIMEOUT_S},
         "scope": "deployment (environment variables, read at process start)",
         "editable_by": "Operator, by redeploying with new environment values",
         "where": "container environment",
         "affects": ["The word and image-size floors for 1.4.5 / 1.4.9, how many images per file "
                     "are read, and the per-image OCR timeout. ACP_DETECT_IMAGES_OF_TEXT=0 turns "
                     "the OCR checks off."]},
        {"key": "ai_enabled", "label": "AI features",
         "value": _safe(store.get_ai_enabled, None), "scope": "platform",
         "editable_by": "Platform admin (owner)", "where": "PUT /settings",
         "affects": ["Whether assisted fixes can draft with a model (vision alt text, link text, "
                     "rewrites). Detection does not use AI and is unaffected."]},
    ]
    return out


def payload(*, user: str | None = None, formats: tuple[str, ...] | None = None) -> dict:
    """The GET /rules/explanations body. build() is cached; the rubric and settings are read per
    request, because they are the parts an administrator can change. `formats` narrows the build
    (tests of one format family); the route always serves every format."""
    import core
    import remediation_capability as cap

    criteria = build(formats)
    rb = core.active_rubric()
    disabled = set(rb.disabled)
    catalog_by_sc = catalog_ids_by_sc()
    emitted = {r.id for c in criteria for cell in c.formats.values() for r in cell.rules}

    rows = []
    by_status = {s: 0 for s in STATUSES}
    cells = 0
    for c in criteria:
        explained = {r.id for cell in c.formats.values() for r in cell.rules}
        ids = sorted(set(catalog_by_sc.get(c.sc, [])) | explained)
        row = to_json(c)
        row["enabled_in_rubric"] = {
            "rule_ids": ids,
            "disabled_rule_ids": sorted(i for i in ids if i in disabled),
            # Catalog rows that no explained detector emits: disabling one changes nothing.
            "not_emitted": sorted(i for i in catalog_by_sc.get(c.sc, []) if i not in emitted),
        }
        rows.append(row)
        for cell in c.formats.values():
            cells += 1
            by_status[cell.status] = by_status.get(cell.status, 0) + 1

    build_info = _safe(lambda: __import__("routes.system", fromlist=["_build_info"])._build_info(), {})
    return {
        "schema_version": SCHEMA_VERSION,
        "provenance": {
            "commit": (build_info or {}).get("commit"),
            "build_version": (build_info or {}).get("version"),
            "rule_catalog_version": (_catalog().get("_meta") or {}).get("version"),
            "rubric": {"name": rb.name, "version": rb.version, "hash": rb.hash},
            "sources": sources(criteria),
        },
        "formats": list(formats or cap.FORMATS),
        "vocabularies": {"statuses": list(STATUSES), "methods": list(METHODS),
                         "fix_modes": list(FIX_MODES)},
        "criteria": rows,
        "settings": settings(rb, user=user),
        "counts": {"criteria": len(criteria), "cells": cells, "by_status": by_status},
    }

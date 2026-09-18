"""The shape of a human-readable rule explanation — one (success criterion × format) cell.

Read-only product metadata served to Settings → Rule explanations. Every field is a statement
about what the CURRENT code does, so each one has to be derivable from, or tested against, the
module that does it (see tests/test_rule_explanations*.py). A generic description of the WCAG
criterion is not an explanation of ACP's implementation, and this schema has no field for one.

Vocabularies are closed on purpose: the UI renders them as words, never as colour alone, and an
unknown value is a contract break rather than a new state the screen has to guess about.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field

SCHEMA_VERSION = 1

# What ACP does for this criterion on this format today.
#   implemented     — a detector runs and its findings reach assessment
#   partial         — a detector runs, but covers a named subset of the criterion
#   manual          — ACP cannot judge it from the file; a person must (a manual test plan exists
#                     or the capability lane is "human")
#   not_implemented — the criterion applies to this format but nothing checks it yet
#   not_applicable  — the criterion has no meaning for this format (outside the capability map)
STATUSES = ("implemented", "partial", "manual", "not_implemented", "not_applicable")

# HOW a rule decides. A cell usually mixes more than one, so it is a per-rule property.
#   deterministic — exact reading of the file (an attribute is present or not, a ratio is computed)
#   heuristic     — a rule of thumb over the file that can be wrong in both directions
#   ai            — a model drafts or judges; always human-approved before it is applied
#   manual        — a person decides; ACP only records the decision
METHODS = ("deterministic", "heuristic", "ai", "manual")

# What remediation can do with a finding from this rule.
#   auto     — deterministic fix applied without review, verified by re-scanning the saved file
#   assisted — ACP drafts a fix (often AI); a person approves it before it is written
#   review   — ACP leaves the file unchanged and routes the finding to a person
#   none     — detection only; no fix path
FIX_MODES = ("auto", "assisted", "review", "none")


@dataclass(frozen=True)
class Threshold:
    """One number or condition a rule decides by.

    `value` is rendered verbatim, so derive it from the constant (f"{CONST / 2:g}pt"), never type
    it. `source` is "module:NAME" — a constant where one exists, otherwise the function whose body
    holds the number (the drift tests re-derive it either way), so a reader can find it.
    `configurable` is True ONLY when something changes it today without a code change: an admin
    setting ("rubric.compliant_threshold") or a deployment environment variable ("env:ACP_OCR_…").
    A value in code is False however easy it would be to expose.
    """
    label: str
    value: str
    source: str
    configurable: bool = False
    setting: str | None = None          # the setting key when configurable, e.g. "rubric.compliant_threshold"
    standard: bool = False              # True when the number IS the WCAG requirement (4.5:1), not an ACP heuristic


@dataclass(frozen=True)
class RuleRef:
    """A rule ID a detector actually emits, and where it is emitted from."""
    id: str                             # e.g. "DOCX_PSEUDO_HEADING" or "DOCX-ALT-001"
    source: str                         # repo-relative path of the emitting module
    method: str                         # one of METHODS
    fix: str                            # one of FIX_MODES
    engine: str = "acp"                 # "acp" (this repo's Python), "office-analyser" (the vendored .NET
                                        # engine) or "pdf-analyser" (the vendored PDF engine, ADR 0029)


@dataclass(frozen=True)
class FormatExplanation:
    status: str                         # one of STATUSES
    assessment_lane: str | None = None  # remediation_capability.assessment_lane(fmt, sc): auto|review|human|None
    remediation_lane: str | None = None # remediation_capability.lane(fmt, sc): auto|assisted|human|None
    checks: tuple[str, ...] = ()        # plain-language bullets: what it checks
    thresholds: tuple[Threshold, ...] = ()
    does_not_check: tuple[str, ...] = ()  # known limits, in plain language
    evidence: str = ""                  # granularity of a finding: "one finding per paragraph, located word:p:N"
    fix: str = ""                       # plain language: what a fix can apply vs detection only
    rules: tuple[RuleRef, ...] = ()


@dataclass(frozen=True)
class CriterionExplanation:
    sc: str                             # "1.3.1"
    name: str                           # "Info and Relationships"
    level: str                          # "A" | "AA" | "AAA"
    formats: dict[str, FormatExplanation] = field(default_factory=dict)  # every format in remediation_capability.FORMATS


def to_json(obj) -> dict:
    """Dataclass → JSON-ready dict. `asdict` keeps tuples as tuples; convert them so the value is
    plain JSON data, equal to what a client gets back after a round trip."""
    def plain(v):
        if isinstance(v, dict):
            return {k: plain(x) for k, x in v.items()}
        if isinstance(v, (list, tuple)):
            return [plain(x) for x in v]
        return v
    return plain(asdict(obj))

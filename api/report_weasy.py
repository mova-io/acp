"""PDF/UA-1 conformance report — WeasyPrint HTML/CSS → tagged PDF (ADR 0034).

WHY THIS EXISTS, measured rather than assumed. The shipped renderer (`report_tagged.py`)
prints the same HTML through headless Chromium's `--generate-tagged-pdf`. That produces a real
structure tree, and it is genuinely better than the ReportLab path it replaced — but run
against veraPDF 1.30.2 it **fails PDF/UA-1** on two counts:

    clause 7.1 test 8   x1   no XMP metadata stream in the catalog (Chromium writes none)
    clause 7.1 test 3   x7   content neither marked as Artifact nor tagged as real content

The second is Chromium's own print header/footer, which also leaks a local temp path
(`file:///tmp/acp_report_.../report.html`) onto every page of a document handed to a customer's
auditor. The same HTML through WeasyPrint passes PDF/UA-1 with 0 failures, writes XMP, tags
tables as Table/THead/TBody rather than a bare Table, and has no print furniture at all.

WHAT THIS MODULE ADDS ON TOP OF SWAPPING THE ENGINE. WeasyPrint does not tag inline `<svg>`:
rendered as-is, the shipped template's two charts drop out of the structure tree entirely
(1 Figure — the logo — against Chromium's 5). Passing PDF/UA while silently losing the charts
from the reading order is the failure mode this file exists to avoid, so the charts are
reauthored to the pattern ADR 0034's spike verified:

    an <img alt="…conclusion…"> carrying the chart as a data-URI SVG, tagged as a /Figure WITH
    /Alt, beside a real data <table> holding the same numbers.

NOTHING HERE FAKES STRUCTURE. Every tag in the output is derived by WeasyPrint from HTML
semantics. There is no pikepdf post-process bolting a `/StructTreeRoot` onto an untagged file —
that is what `report.py::_tag_pdf` does today, and it is the one option ADR 0034 explicitly
rules out: an empty `/Document` element with an empty ParentTree turns our own detector green
and gives a screen-reader user nothing.

THIS IS NOW THE RENDERER `/scans/{sid}/report.pdf` SERVES, by default, as of the cutover.
`ACP_REPORT_RENDERER=tagged` puts the previous Chromium renderer back without a redeploy.

TWO OF ADR 0034'S GATES WERE NOT RUN BEFORE THAT HAPPENED — PAC 2024 (Windows-only) and a real
NVDA or VoiceOver pass. Neither can run in this environment or in CI. What HAS been run: veraPDF
ua1 (0 failures, against Chromium's 8), the structural suite in tests/test_report_weasy_structure
.py, and a page-by-page visual comparison. The env switch above exists precisely because those
two gates are outstanding; `scripts/build_report_review_packet.py` builds what a reviewer needs
to close them. See the addendum to docs/adr/0034.
"""
from __future__ import annotations

import base64
import re
from functools import lru_cache
from pathlib import Path

from jinja2 import Environment, BaseLoader
from markupsafe import Markup

# The content model is imported, never re-derived. Two renderers computing "how many files are
# certified" from the same inputs is two chances to disagree, and the disagreement would show up
# as a customer-facing number differing between two PDFs of the same scan.
from report_tagged import (  # noqa: F401  (re-exported for tests)
    REPORT_LANG,
    AUDIT_GUIDE_TEMPLATE,
    _logo_data_uri,
    _prepare_context,
    _safe_guide_thumb,
    _sc_label,
)


# ── Charts ───────────────────────────────────────────────────────────────────
#
# Each chart is an <img> whose alt states the CONCLUSION, not the mechanics. "Bar chart showing
# criteria" describes the picture; "1.1.1 affects the most files (1 of 3)" is what a reader who
# cannot see it actually needs. The exact numbers live in the adjacent data table, so the alt
# does not have to carry them and does not go stale against it.


def _svg_data_uri(svg: str) -> str:
    return "data:image/svg+xml;base64," + base64.b64encode(svg.encode("utf-8")).decode()


def _score_ring_svg(score: int) -> str:
    r = 36
    circ = 2 * 3.14159 * r
    filled = circ * max(0, min(100, score)) / 100
    color = "#3B6D11" if score >= 80 else "#854F0B" if score >= 60 else "#A32D2D"
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="96" height="96" viewBox="0 0 96 96">'
        f'<circle cx="48" cy="48" r="{r}" fill="none" stroke="#e0dbe4" stroke-width="8"/>'
        f'<circle cx="48" cy="48" r="{r}" fill="none" stroke="{color}" stroke-width="8" '
        f'stroke-dasharray="{filled:.1f} {circ:.1f}" stroke-dashoffset="{circ * 0.25:.1f}" '
        f'stroke-linecap="round"/>'
        f'<text x="48" y="52" text-anchor="middle" font-size="18" font-weight="bold" '
        f'font-family="Liberation Sans, DejaVu Sans, sans-serif" fill="{color}">{score}</text>'
        f'</svg>'
    )


# Chart geometry. The label sits ABOVE its bar across the full chart width rather than in a
# fixed 130px column to the left: the column version right-aligned "1.4.3 Contrast (Minimum)"
# into 124px and clipped its first characters off the image (".4.3 Contrast (Minimum)" on the
# printed page). Labels are wrapped by MEASURED width, using the metrics of the bundled DejaVu
# Sans — the widest face in the stack — so the wrap is conservative for Liberation/Arial too.
BARS_W = 560
_BAR_LABEL_PX = 10
_BAR_LINE_H = 13
_BAR_H = 12
_BAR_GAP = 8
_BAR_VALUE_W = 48
_FONT_FILE = Path(__file__).resolve().parent / "assets" / "fonts" / "DejaVuSans.ttf"


@lru_cache(maxsize=1)
def _label_font():
    from PIL import ImageFont
    return ImageFont.truetype(str(_FONT_FILE), _BAR_LABEL_PX)


def text_width(text: str) -> float:
    """Rendered width in px at the chart's label size (DejaVu Sans metrics)."""
    return float(_label_font().getlength(str(text)))


def _wrap_label(text: str, width: float) -> list[str]:
    words, lines, line = str(text).split(), [], ""
    for word in words:
        candidate = f"{line} {word}".strip()
        if text_width(candidate) <= width:
            line = candidate
            continue
        if line:
            lines.append(line)
        # A single word wider than the chart (a long identifier) is broken by characters.
        while text_width(word) > width:
            cut = len(word)
            while cut > 1 and text_width(word[:cut]) > width:
                cut -= 1
            lines.append(word[:cut])
            word = word[cut:]
        line = word
    if line:
        lines.append(line)
    return lines or [""]


def _bars_layout(rows: list[tuple[str, int]]) -> tuple[list[dict], int]:
    usable = BARS_W - 4
    y, laid = 6, []
    for name, count in rows:
        lines = _wrap_label(name, usable * 0.95)
        laid.append({"name": name, "count": count, "lines": lines, "y": y})
        y += len(lines) * _BAR_LINE_H + _BAR_H + _BAR_GAP + 3
    return laid, max(40, y + 4)


def _bars_svg(rows: list[tuple[str, int]]) -> str:
    laid, height = _bars_layout(rows)
    bar_max_w = BARS_W - _BAR_VALUE_W - 4
    max_val = max((c for _, c in rows), default=1) or 1
    out = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{BARS_W}" height="{height}" '
        f'viewBox="0 0 {BARS_W} {height}" '
        f'font-family="DejaVu Sans, Liberation Sans, sans-serif">'
    ]
    for row in laid:
        y = row["y"]
        for n, line in enumerate(row["lines"]):
            out.append(f'<text x="2" y="{y + 10 + n * _BAR_LINE_H}" font-size="{_BAR_LABEL_PX}" '
                       f'fill="#46303F">{_x(line)}</text>')
        bar_y = y + len(row["lines"]) * _BAR_LINE_H + 2
        bar_w = max(2, int(bar_max_w * row["count"] / max_val))
        out.append(
            f'<rect x="2" y="{bar_y}" width="{bar_w}" height="{_BAR_H}" fill="#854F0B" rx="2"/>'
            f'<text x="{bar_w + 8}" y="{bar_y + 10}" font-size="9" fill="#6B6670">{row["count"]}</text>'
        )
    out.append("</svg>")
    return "".join(out)


def _x(s: str) -> str:
    """XML-escape for text going inside the SVG. The SVG is base64'd into a data URI, so Jinja's
    autoescaping never sees it — a criterion name containing & or < would otherwise produce an
    SVG that silently fails to parse and a chart that vanishes."""
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;"))


def _ring_alt(score: int) -> str:
    band = "meets the 80% threshold" if score >= 80 else (
        "is below the 80% threshold" if score >= 60 else "is well below the 80% threshold")
    return f"Average score {score} out of 100, which {band}."


def _bars_alt(rows: list[tuple[str, int]], total_files: int) -> str:
    """The chart's text alternative — a sentence, read aloud, so it has to parse as one.

    The first draft built the plural by appending "s" to "criterion" and left the verb fixed,
    producing "2 further criterions also has open issues" in the /Alt of a shipped accessibility
    report. Nothing structural would ever have caught it: the Figure had an /Alt, veraPDF was
    happy, and the only reader affected is the one who cannot see the chart. Both the noun and
    the verb inflect here for that reason.
    """
    if not rows:
        return "No criteria have open issues."
    # max BY COUNT, not rows[0]. `rows` arrives sorted by severity, and reading the first row as
    # the largest made the sentence contradict the picture it describes: on a real 37-file scan
    # the alt said "1.3.1 affects the most files, 37 of 37" while the longest bar on the page was
    # 2.4.2 at 49. Both statements came from the same list. A sighted reader sees the chart; the
    # reader this sentence exists for gets the wrong criterion, and nothing structural can tell —
    # the Figure has an /Alt either way. Ties keep the earlier (higher-severity) row.
    top, top_n = max(rows, key=lambda r: r[1])
    others = len(rows) - 1
    if others == 1:
        tail = " 1 further criterion also has open issues."
    elif others > 1:
        tail = f" {others} further criteria also have open issues."
    else:
        tail = ""
    return (f"Open issues by criterion. {top} affects the most files, {top_n} of {total_files}."
            f"{tail} Exact counts follow in the table below.")


# ── Template ─────────────────────────────────────────────────────────────────
#
# Deliberately the shipped template's markup and CSS, changed only where WeasyPrint or PDF/UA
# requires it. A visual-parity rewrite that also restyled would make any difference in the page
# review ambiguous between "the engine renders it differently" and "someone changed the design".

_TEMPLATE = r"""<!DOCTYPE html>
<html lang="{{ lang }}">
<head>
<meta charset="UTF-8">
<title>{{ page_title }}</title>
<style>
*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
/* DejaVu Sans is BUNDLED (api/assets/fonts) rather than left to whatever the host has installed:
   it is the face that carries the ✓/✗ marks, and a developer machine without it rendered them
   from a different fallback than production. */
@font-face { font-family: "DejaVu Sans"; src: url("{{ font_regular_uri }}"); font-weight: 400; }
@font-face { font-family: "DejaVu Sans"; src: url("{{ font_bold_uri }}"); font-weight: 700; }
@page {
  size: Letter; margin: 0.75in 0.7in 0.8in 0.7in;
  font-family: "Liberation Sans", Arial, "DejaVu Sans", sans-serif; font-size: 7.5pt; color: #6B6670;
  @top-left { content: {{ page_head_css }}; vertical-align: bottom; padding-bottom: 8pt; }
  @bottom-left { content: {{ page_foot_css }}; vertical-align: top; padding-top: 8pt; width: 78%; }
  @bottom-right { content: "Page " counter(page) " of " counter(pages); vertical-align: top;
                  padding-top: 8pt; text-align: right; width: 22%; }
}
body {
  /* Written out here rather than interpolated from a constant. A Jinja variable looked
     tidier and was a bug: this environment autoescapes, so the quotes arrived as
     &#34;Liberation Sans&#34; — invalid CSS, silently dropped, and the whole report rendered
     in WeasyPrint's default serif. Nothing structural noticed, because tagging does not
     depend on the font: veraPDF passed with zero failures and every structural test stayed
     green. Only rendering the page and looking at it caught it, which is why the regression
     test asserts the EMBEDDED FONT rather than this string.

     Liberation Sans is metric-compatible with Arial, which is what Chromium rendered with, so
     it is what holds the visual parity. It does not carry U+2713 or U+2717 — the tick and
     cross the File Inventory prints directly, measured with fontTools against the font file
     rather than assumed — so DejaVu Sans follows it purely to supply those two glyphs. Both
     embed; PDF/UA requires embedded fonts. */
  font-family: "Liberation Sans", Arial, "DejaVu Sans", sans-serif;
  font-size: 9.5pt;
  color: #2B2330;
  line-height: 1.45;
  background: #fff;
}
header { display: flex; align-items: center; gap: 14px; padding-bottom: 8px;
         border-bottom: 1.5px solid #46303F; margin-bottom: 10px; }
header img { width: 52px; height: auto; flex-shrink: 0; }
.header-text h1 { font-size: 15pt; color: #46303F; font-weight: 700; margin-bottom: 2px; }
.header-sub { font-size: 8pt; color: #6B6670; }
h2 { font-size: 11.5pt; color: #46303F; font-weight: 600; margin: 18px 0 6px; }
h3 { font-size: 9.5pt; color: #46303F; font-weight: 600; margin: 10px 0 4px; }
p { margin-bottom: 6px; }
.muted { color: #6B6670; font-size: 8.5pt; }
a { color: #46303F; }

.decision-card {
  display: flex; gap: 20px; align-items: center;
  background: #f6f3f7; border: 1px solid #e4e0e8; border-radius: 4px;
  padding: 12px 16px; margin: 10px 0;
}
.decision-label { font-size: 8pt; color: #6B6670; text-transform: uppercase;
                  letter-spacing: .04em; margin-bottom: 2px; }
.decision-value { font-size: 14pt; font-weight: 700; color: #46303F; }
.certifiable { color: #3B6D11; }
.not-certifiable { color: #A32D2D; }

h1, h2, h3, h4, h5, p, li, th, td, dd, caption, figcaption { overflow-wrap: anywhere; }
table { width: 100%; border-collapse: collapse; margin: 8px 0; font-size: 8.5pt; table-layout: fixed; }
thead { display: table-header-group; }
tr { break-inside: avoid; }
table.inventory col.c-file { width: 46%; }
table.criteria col.c-crit { width: 52%; }
caption { text-align: left; font-weight: 600; font-size: 9pt; color: #46303F;
          padding-bottom: 4px; }
th[scope="col"] { background: #f0edf2; color: #46303F; font-weight: 600; text-align: left;
     padding: 5px 8px; border-bottom: 1.5px solid #c8c2cc; }
/* A row header is a TH for the structure tree's sake, and must look like the TD it replaced.
   The shipped Chromium report renders this column as an ordinary cell, and the default TH
   styling above — bold, shaded, heavier rule — is a visual change PDF/UA never asked for:
   14289 wants the cell TAGGED as a header, and says nothing about its weight. Styling all TH
   alike is how a semantics fix quietly becomes a redesign. */
th[scope="row"], td { padding: 4px 8px; border-bottom: 1px solid #e4e0e8; vertical-align: top;
     font-weight: 400; text-align: left; background: none; color: inherit; }
tr:last-child th[scope="row"], tr:last-child td { border-bottom: none; }
tr:nth-child(even) th[scope="row"], tr:nth-child(even) td { background: #faf8fb; }
.score-ok { color: #3B6D11; font-weight: 600; }
.score-warn { color: #854F0B; font-weight: 600; }
.score-bad { color: #A32D2D; font-weight: 600; }
.cert-yes { color: #3B6D11; }
.cert-no  { color: #A32D2D; }
.sev-critical { color: #A32D2D; font-weight: 600; }
.sev-serious  { color: #854F0B; font-weight: 600; }
.sev-moderate { color: #2B2330; }
.sev-minor    { color: #6B6670; }

/* Follow-up guidance may span pages; keep long hashes and proposals in bounds. */
.remediation-guide { break-before: page; break-inside: auto; }
.guide-document { margin-top: 12px; overflow-wrap: anywhere; }
/* A guide item may be long; keeping every one whole stranded up to half a page before each.
   Items flow, but a document's heading block and an item's heading stay with what follows. */
.guide-item { break-inside: auto; border-left: 3px solid #854F0B; padding: 7px 10px; margin: 8px 0; }
.guide-document > h3, .guide-document > p.muted, .guide-document > h4, .guide-item h5 { break-after: avoid; }
.guide-item li, .guide-item p { break-inside: avoid; }
.guide-item.applied { border-color: #3B6D11; }
.guide-item img { max-width: 100%; max-height: 220px; object-fit: contain; }
h4, h5 { font-size: 9.5pt; margin: 8px 0 4px; }
.guide-item ol { padding-left: 20px; margin: 5px 0; }
.guide-item li { margin-bottom: 4px; }
.guide-value { white-space: pre-wrap; overflow-wrap: anywhere; background: #f6f3f7; padding: 5px 7px; }
figure { margin: 10px 0; }
figcaption { font-size: 8pt; color: #6B6670; margin-top: 4px; }

/* Only SHORT sections refuse to break. `section { page-break-inside: avoid }` on every section
   pushed a 300-file inventory wholesale onto the next page, leaving most of page 1 blank, and
   then split it anyway. Long tables flow; their header row repeats (thead above). */
section { break-inside: auto; }
section.keep { break-inside: avoid; }
h2, h3, caption, figcaption { break-after: avoid; }
figure.chart img { max-width: 100%; height: auto; }
.scope-list { list-style: disc; padding-left: 18px; font-size: 8.5pt;
              color: #2B2330; line-height: 1.5; }
dl { font-size: 8.5pt; }
dt { font-weight: 600; color: #46303F; margin-top: 6px; }
dd { color: #2B2330; margin-left: 12px; }
</style>
</head>
<body>
<header>
  {% if logo_uri %}
  <img src="{{ logo_uri }}" alt="mova.io logo" width="52">
  {% endif %}
  <div class="header-text">
    <h1>Accessibility Assessment Report</h1>
    <p class="header-sub">
      {{ std }} · Assessment completed {{ assessment_completed }} UTC ·
      Report generated {{ report_generated_at }} UTC
    </p>
  </div>
</header>

<p class="muted">
  Scan <strong>{{ run_id }}</strong> · rubric {{ rubric_display }} ·
  hash <strong>{{ rubric_hash }}</strong> — results are reproducible from the rubric hash.
  Scans run read-only; documents are never retained.
</p>
{% if stage_lineage_digest %}
<p class="muted">
  Canonical stage lineage <strong>{{ stage_lineage_status }}</strong> · SHA-256
  <strong>{{ stage_lineage_digest }}</strong>. Stage totals use sealed execution snapshots.
</p>
{% endif %}
{% if finding_reconciliation %}
<p class="muted">
  Finding reconciliation <strong>{{ finding_reconciliation.status }}</strong> · SHA-256
  <strong>{{ finding_reconciliation.content_digest.value }}</strong>.
  {% if finding_reconciliation.status == 'reconciled' %}
    {{ finding_reconciliation.outcomes.accounted }} of
    {{ finding_reconciliation.outcomes.assessed }} assessed findings have one durable disposition.
  {% elif finding_reconciliation.status == 'inconsistent' %}
    Accounting is inconsistent; no complete-resolution claim is made.
  {% else %}
    Exact per-finding outcomes are not available for this snapshot.
  {% endif %}
</p>
{% endif %}

<section class="keep">
<h2>Certification Decision</h2>
<div class="decision-card">
  <div>
    <div class="decision-label">Files certified</div>
    <div class="decision-value {{ 'certifiable' if certifiable > 0 else 'not-certifiable' }}">
      {{ certifiable }} of {{ total_files }}
    </div>
  </div>
  {% if avg_score is not none %}
  <div>
    <div class="decision-label">Average score</div>
    <div class="decision-value">{{ avg_score }}<span style="font-size:9pt;font-weight:400">%</span></div>
  </div>
  {% endif %}
  <div>
    <div class="decision-label">Open issues</div>
    <div class="decision-value {{ 'not-certifiable' if total_open > 0 else 'certifiable' }}">
      {{ total_open }}
    </div>
  </div>
  {% if ring_uri %}
  <figure style="margin:0">
    <figcaption class="muted" style="text-align:center;margin-bottom:4px">Score</figcaption>
    <img src="{{ ring_uri }}" alt="{{ ring_alt }}" width="96" height="96">
  </figure>
  {% endif %}
</div>
<p>
  {% if certifiable == total_files and total_open == 0 %}
  All <strong>{{ total_files }}</strong> document{{ 's' if total_files != 1 else '' }}
  certified against {{ std }} criteria within scope.
  {% elif certifiable > 0 %}
  <strong>{{ certifiable }}</strong> of <strong>{{ total_files }}</strong>
  document{{ 's' if total_files != 1 else '' }} certified;
  <strong>{{ total_files - certifiable }}</strong> ha{{ 've' if (total_files - certifiable) != 1 else 's' }}
  open issues requiring remediation.
  {% else %}
  No documents certified. <strong>{{ total_open }}</strong> open
  issue{{ 's' if total_open != 1 else '' }} across
  <strong>{{ total_files }}</strong> document{{ 's' if total_files != 1 else '' }}.
  {% endif %}
  Certification means no blocking issues remain among the criteria evaluated for each file's
  format — not that the document is fully WCAG conformant. Criteria with no validator for that
  format were not evaluated.
</p>
</section>

<section>
<h2>File Inventory</h2>
<table class="inventory">
  <caption>Files assessed in this scan</caption>
  <colgroup><col class="c-file"><col><col><col><col></colgroup>
  <thead>
    <tr>
      <th scope="col">File</th>
      <th scope="col">Score</th>
      <th scope="col">Certified</th>
      <th scope="col">Open issues</th>
      <th scope="col">Status</th>
    </tr>
  </thead>
  <tbody>
  {% for f in files %}
  {% set score_cls = 'score-ok' if f.score >= 80 else 'score-warn' if f.score >= 60 else 'score-bad' %}
  {% set cert_cls = 'cert-yes' if f.compliant else 'cert-no' %}
  <tr>
    <th scope="row">{{ f.file }}</th>
    <td class="{{ score_cls }}">{{ f.score }}%</td>
    <td class="{{ cert_cls }}">{{ '✓ Yes' if f.compliant else '✗ No' }}</td>
    <td>{{ f.issues | length if f.issues else 0 }}</td>
    <td>{{ f.status | capitalize }}</td>
  </tr>
  {% endfor %}
  </tbody>
</table>
</section>

{% if open_by_crit %}
<section>
<h2>Open Issues by Criterion</h2>
{% if bars_uri %}
<figure class="chart">
  <figcaption>Files with open issues per criterion{% if bars_more %} (the 8 most severe criteria are charted; all {{ open_by_crit|length }} are in the table below){% endif %}</figcaption>
  <img src="{{ bars_uri }}" alt="{{ bars_alt }}" width="{{ bars_w }}" height="{{ bars_h }}">
</figure>
{% endif %}
<table class="criteria">
  <caption>Open issues grouped by WCAG criterion</caption>
  <colgroup><col class="c-crit"><col><col><col></colgroup>
  <thead>
    <tr>
      <th scope="col">Criterion</th>
      <th scope="col">Level</th>
      <th scope="col">Severity</th>
      <th scope="col">Files affected</th>
    </tr>
  </thead>
  <tbody>
  {% for row in open_by_crit %}
  {% set sev_cls = 'sev-' + row.severity | lower %}
  <tr>
    <th scope="row">{{ row.criterion }}</th>
    <td>{{ row.level }}</td>
    <td class="{{ sev_cls }}">{{ row.severity | capitalize }}</td>
    <td>{{ row.file_count }}</td>
  </tr>
  {% endfor %}
  </tbody>
</table>
</section>
{% endif %}

<section>
<h2>Scope of Assertion</h2>
<p>
  This report evaluates a <strong>subset</strong> of {{ std }} criteria — those for which
  this platform has a validator for each document's file format. A 100% score means no
  blocking findings among the criteria evaluated, not full WCAG conformance. The following
  criteria were not evaluated:
</p>
{% if not_evaluated %}
<ul class="scope-list">
{% for sc in not_evaluated %}
  <li>{{ sc }}</li>
{% endfor %}
</ul>
{% else %}
<p class="muted">All criteria in scope were evaluated for at least one file in this scan.</p>
{% endif %}
</section>

<section>
<h2>Methodology</h2>
<dl>
  <dt>Standard</dt>
  <dd>{{ std }}</dd>
  <dt>Rubric</dt>
  <dd>{{ rubric_display }} (hash {{ rubric_hash }})</dd>
  <dt>Scan ID</dt>
  <dd>{{ run_id }}</dd>
  <dt>Assessment completed</dt>
  <dd>{{ assessment_completed }} UTC</dd>
  <dt>Report generated</dt>
  <dd>{{ report_generated_at }} UTC</dd>
  <dt>Scan approach</dt>
  <dd>Automated static analysis. Documents are analysed read-only and are never retained
      after the scan completes.</dd>
  <dt>Standard reference</dt>
  <dd><a href="https://www.w3.org/TR/WCAG21/">Web Content Accessibility Guidelines (WCAG) 2.1</a></dd>
</dl>
</section>

{% if ai_summary %}
<section>
<h2>AI Governance</h2>
<p>{{ ai_summary }}</p>
</section>
{% endif %}

""" + AUDIT_GUIDE_TEMPLATE.replace(
    "{% for doc in remediation_guide %}",
    "{% if guide_quiet_count %}<p class=\"muted\">{{ guide_quiet_count }} other document"
    "{{ 's have' if guide_quiet_count != 1 else ' has' }} no remaining item or recorded change in "
    "this evidence and {{ 'are' if guide_quiet_count != 1 else 'is' }} not listed below; see the "
    "File Inventory.</p>{% endif %}\n{% for doc in remediation_guide %}", 1) + r"""
</body>
</html>
"""

assert "guide_quiet_count" in _TEMPLATE, "AUDIT_GUIDE_TEMPLATE changed; the quiet-document note no longer applies"

_jinja_env = Environment(loader=BaseLoader(), autoescape=True)
_jinja_env.filters["safe_guide_thumb"] = _safe_guide_thumb


def render_html(run: dict, files: list, meta: dict, facts: dict | None = None,
                decisions: dict | None = None, evidence: list | None = None) -> str:
    """The HTML the PDF is made of. Exported so tests can assert on the markup directly —
    a semantic defect is far easier to read here than in a structure-tree dump, and the two
    are checked against each other by the structural tests."""
    ctx = _prepare_context(run, files, meta, facts, decisions, evidence)

    # The guide lists what to DO. A document with no remaining item and no recorded change has
    # nothing to act on; printing a version line and boilerplate for each one buried the real
    # items (240 of 300 blocks on a long scan). They are counted, not dropped silently, and the
    # File Inventory above still lists every document.
    guide = ctx.get("remediation_guide") or []
    actionable = [d for d in guide if d.get("remaining") or d.get("applied")]
    ctx["guide_quiet_count"] = len(guide) - len(actionable)
    ctx["remediation_guide"] = actionable

    score = ctx.get("avg_score")
    ctx["ring_uri"] = _svg_data_uri(_score_ring_svg(score)) if score is not None else ""
    ctx["ring_alt"] = _ring_alt(score) if score is not None else ""

    rows = [(r["criterion"], r["file_count"]) for r in ctx.get("open_by_crit", [])[:8]]
    if rows:
        svg = _bars_svg(rows)
        ctx["bars_uri"] = _svg_data_uri(svg)
        ctx["bars_alt"] = _bars_alt(rows, ctx["total_files"])
        ctx["bars_w"] = BARS_W
        ctx["bars_h"] = _bars_layout(rows)[1]
        ctx["bars_more"] = max(0, len(ctx.get("open_by_crit", [])) - len(rows))
    else:
        ctx["bars_uri"] = ctx["bars_alt"] = ""
        ctx["bars_w"] = ctx["bars_h"] = 0
        ctx["bars_more"] = 0

    ctx["font_regular_uri"] = _FONT_FILE.as_uri()
    ctx["font_bold_uri"] = _FONT_FILE.with_name("DejaVuSans-Bold.ttf").as_uri()
    version = (meta or {}).get("platform_version") or _platform_version()
    run_id = str(ctx.get("run_id") or "not recorded")
    run_id = run_id if len(run_id) <= 40 else run_id[:20] + "…" + run_id[-19:]
    foot = (f"Scan {run_id} · generated {ctx.get('report_generated_at')} UTC · "
            f"Mova iO ACP {version or 'version not recorded'}")
    ctx["page_foot_css"] = _css_string(foot)
    ctx["page_head_css"] = _css_string("Accessibility Assessment Report · " + (ctx.get("std") or ""))
    return _jinja_env.from_string(_TEMPLATE).render(**ctx)


def _css_string(text: str) -> Markup:
    """A CSS string literal with every non-trivial character hex-escaped (see report_render)."""
    out = []
    for ch in str(text):
        out.append(ch if ch.isascii() and (ch.isalnum() or ch in " .,:_-()") else "\\%06x" % ord(ch))
    return Markup('"' + "".join(out) + '"')


def _platform_version() -> str | None:
    import os
    value = (os.environ.get("ACP_BUILD_VERSION") or "").strip()
    return value if re.fullmatch(r"[\w.+-]{1,64}", value or "") else None


def build_weasy_report(run: dict, files: list, meta: dict,
                       decisions: dict | None = None,
                       evidence: list | None = None,
                       facts: dict | None = None) -> bytes:
    """Render the conformance report as a PDF/UA-1 tagged PDF.

    `pdf_variant="pdf/ua-1"` is what makes WeasyPrint emit the structure tree, the XMP
    identifier and the document-level entries the profile requires. WeasyPrint's own
    documentation is explicit that selecting the variant does NOT guarantee a conformant
    document — which is why this module ships with a veraPDF gate rather than a claim.

    The signature matches build_tagged_report / build_report so the three are drop-in
    interchangeable behind the renderer flag.
    """
    import weasyprint

    html = render_html(run, files, meta, facts, decisions, evidence)
    return weasyprint.HTML(string=html, base_url=str(Path(__file__).resolve().parent)).write_pdf(
        pdf_variant="pdf/ua-1")

"""Server renderer for the report model: block model -> semantic HTML -> tagged PDF.

WHY THIS EXISTS. The browser exports (frontend/src/pdfReport.js, jsPDF 4.2.1) produced PDFs with
no structure tree, no bookmarks, and a WinAnsi Helvetica that silently drops U+2713 and U+2192 —
an accessibility product handing its customers inaccessible evidence. This module renders the
SAME renderer-agnostic model the HTML export uses (contract v1, /tmp/acp-report-contract.md
section "Report model") on the server, through WeasyPrint, so every download is tagged.

WHAT IS CLAIMED, AND WHAT IS NOT. `pdf_variant="pdf/ua-1"` makes WeasyPrint emit a structure tree,
/Lang, /Title, XMP and DisplayDocTitle. WeasyPrint's own documentation says selecting the variant
does not make a document conformant, so nothing here says "PDF/UA conformant". The automated
tests (tests/test_report_render.py) check the structure a reader actually meets — heading tags,
table header cells, figures with /Alt, bookmarks, embedded fonts, extractable text. A screen-reader
pass and a PAC 2024 check are still manual, and the PDF itself says so in "About this PDF".

INPUT IS UNTRUSTED. The model is built in the browser. So:
  * every string is escaped; the model carries plain text only, never markup;
  * block kinds are a whitelist, and an unknown kind is a 422, not a silently skipped block;
  * limits reject (413) rather than truncate — a report that quietly drops evidence is worse than
    a refused request, because nobody notices the missing record;
  * images must be base64 PNG/JPEG; they are decoded, size-checked and RE-ENCODED with Pillow, so
    the bytes WeasyPrint sees are ones Pillow wrote, not ones the client chose;
  * links survive only as https: or app-relative paths; anything else is printed as text;
  * the WeasyPrint URL fetcher refuses everything except our own re-encoded PNGs and the bundled
    fonts, so no model value can make the server fetch a URL or read a file;
  * identity (scan, file, checksums, generation time, platform version) is supplied by the
    route from the server's own store and overwrites whatever the client sent.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import io
import re
from datetime import datetime, timezone
from functools import lru_cache
from html import escape
from pathlib import Path
from urllib.parse import urlsplit

_ASSETS = Path(__file__).resolve().parent / "assets"
FONT_REGULAR = _ASSETS / "fonts" / "DejaVuSans.ttf"
FONT_BOLD = _ASSETS / "fonts" / "DejaVuSans-Bold.ttf"
_FONT_URLS = frozenset({FONT_REGULAR.as_uri(), FONT_BOLD.as_uri()})

# ── Limits ────────────────────────────────────────────────────────────────────────────────────
MAX_BODY_BYTES = 16 * 1024 * 1024       # whole request, images included
MAX_STRING_CHARS = 200_000              # one before/after value in Full evidence can be long
MAX_NODES = 400_000                     # JSON values in the model
MAX_DEPTH = 16
MAX_BLOCKS = 20_000
MAX_IMAGES = 300
MAX_IMAGE_BYTES = 4 * 1024 * 1024       # decoded, per image
MAX_IMAGE_SIDE = 6000
MAX_IMAGE_PIXELS = 24_000_000
OUT_IMAGE_SIDE = 1600                   # re-encoded images are downscaled to this

KINDS = frozenset({"file", "scan", "remediation"})
MODES = {"summary": "Summary", "reviewer": "Reviewer packet", "full": "Full evidence"}
BLOCK_KINDS = frozenset({
    # existing model kinds (reportModel.js / scanReport.js)
    "heading", "text", "callout", "bullets", "metricGrid", "donut", "barChart", "table",
    "beforeAfter", "pageBreak", "gap", "checklist", "docTitle", "image",
    # contract v1 additions
    "decisionSummary", "stageStrip", "changeCard", "findingCard", "comparison",
    "appendixTable", "link",
})

NOT_RECORDED = "Not recorded"
LOCATION_NOT_RECORDED = "Location not recorded"
PREVIEW_UNAVAILABLE = "Visual preview not available"
REASON_NOT_RECORDED = "Reason not recorded"
DEFAULT_RESPONSE_OPTIONS = ("Accept", "Edit", "Reject", "Unable to verify")
DEFAULT_RESPONSE_NOTICE = ("Marking a printed or downloaded copy does not record a decision. "
                           "Only a decision entered in ACP is part of the record.")
STRUCTURE_STATEMENT = (
    "This PDF is generated with a tagged structure: a document title and language, headings "
    "that also appear as bookmarks, tables with header cells, and images with text alternatives. "
    "Automated tests check that structure on every build. It has not been confirmed by a manual "
    "screen-reader review or a PDF/UA checker for this document, so it is not a PDF/UA "
    "conformance claim. The report itself is evidence of recorded work, not a certification.")


class ReportInputError(Exception):
    """A request the renderer refuses. `status` is the HTTP status the route should answer."""

    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status = status
        self.detail = detail


# ── Validation ────────────────────────────────────────────────────────────────────────────────

def _check_limits(model) -> None:
    """Walk the whole model once: sizes are rejected, never truncated."""
    stack = [(model, 0)]
    nodes = 0
    while stack:
        value, depth = stack.pop()
        nodes += 1
        if nodes > MAX_NODES:
            raise ReportInputError(413, f"report model has more than {MAX_NODES} values")
        if depth > MAX_DEPTH:
            raise ReportInputError(422, f"report model is nested deeper than {MAX_DEPTH} levels")
        if isinstance(value, str):
            if len(value) > MAX_STRING_CHARS:
                raise ReportInputError(413, f"a report string exceeds {MAX_STRING_CHARS} characters")
        elif isinstance(value, dict):
            for key, item in value.items():
                if not isinstance(key, str) or len(key) > 200:
                    raise ReportInputError(422, "report model keys must be short strings")
                stack.append((item, depth + 1))
        elif isinstance(value, list):
            stack.extend((item, depth + 1) for item in value)
        elif value is None or isinstance(value, (bool, int, float)):
            continue
        else:
            raise ReportInputError(422, "report model contains an unsupported value type")


_DATA_URL = re.compile(r"data:image/(png|jpeg);base64,([A-Za-z0-9+/\s]+=*)\s*", re.I)


def sanitize_image(src) -> str | None:
    """Decode, verify and re-encode one image. Returns a PNG data URL written by Pillow."""
    if src is None or src == "":
        return None
    if not isinstance(src, str):
        raise ReportInputError(422, "image src must be a string")
    match = _DATA_URL.fullmatch(src)
    if not match:
        raise ReportInputError(422, "images must be base64 data:image/png or data:image/jpeg URLs")
    declared = match.group(1).lower()
    encoded = re.sub(r"\s+", "", match.group(2))
    if len(encoded) * 3 // 4 > MAX_IMAGE_BYTES:
        raise ReportInputError(413, f"an image exceeds {MAX_IMAGE_BYTES} bytes")
    try:
        raw = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError):
        raise ReportInputError(422, "image data is not valid base64") from None
    from PIL import Image, UnidentifiedImageError
    try:
        with Image.open(io.BytesIO(raw)) as probe:
            fmt = (probe.format or "").upper()
            width, height = probe.size
            if fmt not in ("PNG", "JPEG") or fmt.lower() != declared.replace("jpg", "jpeg"):
                raise ReportInputError(422, "image content does not match its declared PNG/JPEG type")
            if width > MAX_IMAGE_SIDE or height > MAX_IMAGE_SIDE or width * height > MAX_IMAGE_PIXELS:
                raise ReportInputError(413, "image dimensions exceed the renderer's limit")
            probe.verify()
        with Image.open(io.BytesIO(raw)) as img:
            img.load()
            img = img.convert("RGBA" if img.mode in ("RGBA", "LA", "P") else "RGB")
            img.thumbnail((OUT_IMAGE_SIDE, OUT_IMAGE_SIDE))
            out = io.BytesIO()
            img.save(out, format="PNG", optimize=True)
    except ReportInputError:
        raise
    except (UnidentifiedImageError, OSError, SyntaxError, ValueError, Image.DecompressionBombError):
        raise ReportInputError(422, "image could not be decoded") from None
    return "data:image/png;base64," + base64.b64encode(out.getvalue()).decode("ascii")


def block_kind(block) -> str | None:
    if not isinstance(block, dict):
        return None
    kind = block.get("k", block.get("kind"))
    return kind if isinstance(kind, str) else None


def validate_request(body) -> dict:
    """Validate a POST /report-render body. Returns {kind, file, mode, model} with images
    re-encoded. Raises ReportInputError (413/422)."""
    if not isinstance(body, dict):
        raise ReportInputError(422, "request body must be a JSON object")
    kind, mode, file = body.get("kind"), body.get("mode"), body.get("file")
    if kind not in KINDS:
        raise ReportInputError(422, "kind must be one of: file, scan, remediation")
    if mode not in MODES:
        raise ReportInputError(422, "mode must be one of: summary, reviewer, full")
    if file is not None and (not isinstance(file, str) or not file or len(file) > 4096):
        raise ReportInputError(422, "file must be a non-empty string or null")
    if kind == "file" and file is None:
        raise ReportInputError(422, "a file report must name its file")
    model = body.get("model")
    if not isinstance(model, dict):
        raise ReportInputError(422, "model must be an object")
    _check_limits(model)
    blocks = model.get("blocks")
    if not isinstance(blocks, list):
        raise ReportInputError(422, "model.blocks must be a list")
    if len(blocks) > MAX_BLOCKS:
        raise ReportInputError(413, f"report model has more than {MAX_BLOCKS} blocks")
    images = 0
    clean_blocks = []
    for index, block in enumerate(blocks):
        kind_name = block_kind(block)
        if kind_name not in BLOCK_KINDS:
            shown = escape(str(kind_name)[:40]) if kind_name is not None else "missing"
            raise ReportInputError(422, f"block {index}: unsupported block kind ({shown})")
        block = dict(block)
        if kind_name == "changeCard" and isinstance(block.get("image"), dict):
            image = dict(block["image"])
            if image.get("src"):
                images += 1
                image["src"] = sanitize_image(image.get("src"))
            block["image"] = image
        elif kind_name == "image":
            images += 1
            block["src"] = sanitize_image(block.get("src"))
        if images > MAX_IMAGES:
            raise ReportInputError(413, f"report model has more than {MAX_IMAGES} images")
        clean_blocks.append(block)
    cover = model.get("cover")
    if cover is not None and not isinstance(cover, dict):
        raise ReportInputError(422, "model.cover must be an object")
    return {"kind": kind, "mode": mode, "file": file,
            "model": {**model, "blocks": clean_blocks}}


# ── Small helpers ─────────────────────────────────────────────────────────────────────────────

def _s(value) -> str | None:
    """A model value as display text, or None when absent. Structured values are refused."""
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, (int, float)):
        return f"{value:,}" if isinstance(value, int) else f"{value:g}"
    if isinstance(value, str):
        return value
    if isinstance(value, dict) and ("text" in value or "label" in value):
        return _s(value.get("text", value.get("label")))
    return None


def _t(value, fallback: str = "") -> str:
    """Escaped display text."""
    text = _s(value)
    return escape(text if text is not None else fallback, quote=True)


def _nr(value) -> str:
    return _t(value, NOT_RECORDED)


def _list(value) -> list:
    return value if isinstance(value, list) else []


def _color(value) -> str | None:
    return value if isinstance(value, str) and re.fullmatch(r"#[0-9A-Fa-f]{6}", value) else None


def safe_href(href, base_url: str | None = None) -> str | None:
    """An absolute https URL the PDF may link to, or None (the renderer prints text only)."""
    if not isinstance(href, str) or not href or len(href) > 2048:
        return None
    if re.search(r"[\s<>\"'\\\x00-\x1f]", href):
        return None
    if href.startswith("/") and not href.startswith("//"):
        if not base_url:
            return None
        href = base_url.rstrip("/") + href
    try:
        parts = urlsplit(href)
    except ValueError:
        return None
    if parts.scheme != "https" or not parts.hostname or parts.username or parts.password:
        return None
    return href


def css_string(text: str) -> str:
    """A CSS string literal whose every non-trivial character is hex-escaped, so no model value
    can close the string, the rule, or the <style> element."""
    out = []
    for ch in str(text):
        if ch.isascii() and (ch.isalnum() or ch in " .,:_-()"):
            out.append(ch)
        else:
            out.append("\\%06x" % ord(ch))
    return '"' + "".join(out) + '"'


def _shorten(text: str, limit: int) -> str:
    """Middle-ellipsis for the running header only; the full value is printed in the body."""
    text = str(text)
    if len(text) <= limit:
        return text
    keep = limit - 1
    return text[: keep // 2] + "…" + text[-(keep - keep // 2):]


CARD_MAX_CHARS = 600
CARD_MAX_LINES = 7


def clamp_text(value: str) -> str:
    """The concise-packet clamp (frontend reportEvidence.clampText): 7 lines, 600 characters.
    Applied ONLY when the model flags a field as truncated and the mode is not Full evidence."""
    lines = value.split("\n")
    out = "\n".join(lines[:CARD_MAX_LINES]) if len(lines) > CARD_MAX_LINES else value
    out = out[:CARD_MAX_CHARS]
    return out.rstrip() + "…" if len(out) < len(value) else value


def _anchor(value, prefix: str) -> str:
    return prefix + hashlib.sha1(str(value).encode("utf-8")).hexdigest()[:16]


_LANG = re.compile(r"[A-Za-z]{2,3}(-[A-Za-z0-9]{2,8}){0,3}")


@lru_cache(maxsize=1)
def _logo_uri() -> str:
    return "data:image/png;base64," + base64.b64encode((_ASSETS / "mova-logo.png").read_bytes()).decode()


def _location_text(location) -> str:
    if isinstance(location, str) and location.strip():
        return location
    if isinstance(location, dict):
        label = _s(location.get("label"))
        if label:
            return label
        parts = []
        for key, name in (("page", "Page"), ("slide", "Slide"), ("sheet", "Sheet"),
                          ("cell", "Cell"), ("element", None)):
            value = _s(location.get(key))
            if value:
                parts.append(f"{name} {value}" if name else value)
        if parts:
            return " · ".join(parts)
    return LOCATION_NOT_RECORDED


def _hash_label(value) -> str:
    text = _s(value)
    return text if text else NOT_RECORDED


# ── CSS ───────────────────────────────────────────────────────────────────────────────────────

_CSS = """
@font-face { font-family: "DejaVu Sans"; src: url("%(regular)s"); font-weight: 400; font-style: normal; }
@font-face { font-family: "DejaVu Sans"; src: url("%(bold)s"); font-weight: 700; font-style: normal; }
@page {
  size: A4;
  margin: 20mm 15mm 19mm 15mm;
  font-family: "DejaVu Sans", sans-serif;
  font-size: 7.3pt;
  color: #5F5A66;
  @top-left { content: %(head_left)s; vertical-align: bottom; padding-bottom: 3mm; width: 62%%; }
  @top-right { content: %(head_right)s; vertical-align: bottom; padding-bottom: 3mm; text-align: right; width: 38%%; }
  @bottom-left { content: %(foot_left)s; vertical-align: top; padding-top: 3mm; width: 75%%; }
  @bottom-right { content: "Page " counter(page) " of " counter(pages); vertical-align: top; padding-top: 3mm; text-align: right; width: 25%%; }
}
html { font-family: "DejaVu Sans", sans-serif; }
body { margin: 0; font-size: 9.2pt; line-height: 1.42; color: #2B2330; }
h1, h2, h3, h4, h5, p, li, td, th, dd, dt, figcaption, caption, .value { overflow-wrap: anywhere; }
h1 { font-size: 18pt; line-height: 1.2; margin: 0 0 4pt; color: #2B2330; bookmark-level: 1; }
h2 { font-size: 12.5pt; margin: 16pt 0 5pt; color: #4B3460; border-bottom: 0.8pt solid #E4E0E8;
     padding-bottom: 2pt; bookmark-level: 1; break-after: avoid; }
h3 { font-size: 10.5pt; margin: 10pt 0 4pt; color: #4B3460; bookmark-level: 2; break-after: avoid; }
h4 { font-size: 9.6pt; margin: 8pt 0 3pt; color: #4B3460; bookmark-level: 3; break-after: avoid; }
h5, h6 { font-size: 9.2pt; margin: 6pt 0 2pt; color: #4B3460; bookmark-level: none; break-after: avoid; }
p { margin: 0 0 5pt; orphans: 2; widows: 2; }
ul, ol { margin: 0 0 6pt; padding-left: 14pt; }
li { margin-bottom: 2pt; }
a { color: #4B3460; }
.muted { color: #5F5A66; font-size: 8.4pt; }
.small { font-size: 8.4pt; }
.large { font-size: 10.5pt; }
.cover { border-bottom: 2pt solid #4B3460; padding-bottom: 8pt; margin-bottom: 8pt; }
.cover-top { display: flex; align-items: flex-start; gap: 12pt; }
.cover-top > div { flex: 1 1 auto; min-width: 0; }
.cover-logo { width: 34mm; height: auto; flex: 0 0 auto; }
.subtitle { font-size: 10.5pt; color: #5F5A66; margin-bottom: 3pt; }
.meta { list-style: none; padding: 0; margin: 2pt 0 0; font-size: 8.4pt; color: #5F5A66; }
.meta li { margin: 0; }
table { width: 100%%; border-collapse: collapse; table-layout: fixed; margin: 4pt 0 9pt; font-size: 8.4pt; }
caption { text-align: left; font-weight: 700; color: #4B3460; padding-bottom: 3pt; }
thead { display: table-header-group; }
tr { break-inside: avoid; }
th, td { text-align: left; vertical-align: top; padding: 3.2pt 5pt; border-bottom: 0.6pt solid #E4E0E8; }
thead th { background: #F1EDF4; color: #2B2330; border-bottom: 1pt solid #C9C1CF; }
tbody th { font-weight: 700; }
tbody tr:nth-child(even) td, tbody tr:nth-child(even) th { background: #FAF8FB; }
.identity th { width: 34%%; }
.callout { border-left: 3pt solid #4B3460; background: #F7F5F9; padding: 6pt 9pt; margin: 5pt 0 9pt; }
.metrics { list-style: none; padding: 0; margin: 5pt 0 9pt; display: flex; flex-wrap: wrap; gap: 6pt; }
.metrics li { flex: 1 1 22%%; min-width: 30mm; border: 0.7pt solid #E4E0E8; background: #FBFAFC;
              padding: 5pt 7pt; margin: 0; break-inside: avoid; }
.metric-label { display: block; font-size: 7.8pt; color: #5F5A66; }
.metric-value { display: block; font-size: 14pt; font-weight: 700; }
.decision th[scope="row"] { width: 40%%; }
.decision td.num { width: 20%%; font-size: 12pt; font-weight: 700; }
.bar-track { background: #EEECF0; height: 7pt; margin-top: 2.5pt; }
.bar-fill { background: #4B3460; height: 7pt; }
.stack { display: flex; height: 9pt; margin: 3pt 0 5pt; background: #EEECF0; }
.swatch { display: inline-block; width: 7pt; height: 7pt; margin-right: 4pt; }
.stages { list-style: none; padding: 0; display: flex; gap: 4pt; margin: 4pt 0 9pt; }
.stages li { flex: 1 1 0; min-width: 0; border-top: 3pt solid #C9C1CF; padding: 4pt 4pt 0; margin: 0; font-size: 8pt; }
.stages li.done { border-top-color: #3B6D11; }
.stages li.pending { border-top-color: #854F0B; }
.stage-status { font-weight: 700; display: block; }
.card { border: 0.7pt solid #DDD6E2; border-left: 3pt solid #86618B; padding: 5pt 8pt 4pt; margin: 6pt 0; break-inside: auto; }
.card.compact { break-inside: avoid; }
.card.finding.high { border-left-color: #A32D2D; }
.card.finding.medium { border-left-color: #854F0B; }
.card h3, .card h4, .card h5 { margin: 0 0 2pt; }
.card p { margin-bottom: 2.5pt; }
.card ol { margin-bottom: 3pt; }
.card .muted { font-size: 7.6pt; }
.facts { margin: 0 0 3pt; font-size: 8.3pt; }
.sep { color: #9C94A3; }
.where { font-weight: 400; color: #5F5A66; }
table.ba { margin: 2pt 0 3pt; font-size: 8.4pt; }
table.ba td { white-space: pre-wrap; border: 0.5pt solid #E4E0E8; }
table.ba td.before { background: #FBF2F1; }
table.ba td.after { background: #F0F5EA; }
table.ba thead th { padding: 2pt 5pt; }
.label { font-weight: 700; color: #4B3460; }
.value { white-space: pre-wrap; background: #F7F5F9; border: 0.5pt solid #E4E0E8; padding: 3pt 5pt; margin: 1pt 0 4pt; font-size: 8.6pt; }
.value.before { background: #FBF2F1; }
.value.after { background: #F0F5EA; }
.card-body { display: flex; gap: 9pt; align-items: flex-start; }
.card-text { flex: 1 1 auto; min-width: 0; }
.card-figure { flex: 0 0 36%%; margin: 0; }
.card-figure img, figure.image img { max-width: 100%%; max-height: 70mm; object-fit: contain; border: 0.5pt solid #DDD6E2; }
figure { margin: 4pt 0 8pt; }
figcaption { font-size: 7.8pt; color: #5F5A66; margin-top: 2pt; }
.no-preview { font-size: 8.2pt; color: #5F5A66; border: 0.5pt dashed #C9C1CF; padding: 4pt; }
.response { border-top: 0.6pt solid #E4E0E8; margin-top: 3pt; padding-top: 3pt; break-inside: avoid; }
.response p { margin-bottom: 1pt; }
.boxes { font-size: 8.8pt; }
.box { margin-right: 10pt; white-space: nowrap; }
.write-line { border-bottom: 0.6pt solid #9C94A3; height: 12pt; margin-bottom: 2pt; }
.notice { font-size: 7.4pt; color: #5F5A66; }
.partial { border-left: 3pt solid #854F0B; background: #FBF1DF; padding: 4pt 7pt; }
.appendix-start { break-before: page; }
.gap { height: 6pt; }
.about { margin-top: 14pt; }
"""


# ── Renderer ──────────────────────────────────────────────────────────────────────────────────

class _Renderer:
    def __init__(self, model: dict, identity: dict, mode: str, base_url: str | None):
        self.model = model
        self.identity = identity
        self.mode = mode
        self.base_url = base_url
        self.level = 1            # the current section's HTML heading level (h1 = cover)
        self.ids: set[str] = set()

    # headings ---------------------------------------------------------------------------------
    def _heading_tag(self, level) -> int:
        try:
            wanted = int(level)
        except (TypeError, ValueError):
            wanted = 1
        wanted = min(max(wanted, 1), 3) + 1          # model level 1..3 -> h2..h4
        tag = min(wanted, self.level + 1)            # never skip a level
        self.level = tag
        return tag

    def _child_tag(self) -> int:
        return min(self.level + 1, 6)

    def _unique(self, raw, prefix):
        anchor = _anchor(raw, prefix)
        n = 1
        candidate = anchor
        while candidate in self.ids:
            n += 1
            candidate = f"{anchor}-{n}"
        self.ids.add(candidate)
        return candidate

    def _link(self, text, href) -> str:
        url = safe_href(href, self.base_url)
        label = _t(text) or _t(href)
        if url:
            return f'<a href="{escape(url, quote=True)}">{label}</a>'
        if isinstance(href, str) and href.startswith("/") and not href.startswith("//") and label != _t(href):
            return f"{label} (in ACP: {_t(href)})"
        return label

    # document ---------------------------------------------------------------------------------
    def cover(self) -> str:
        cover = self.model.get("cover") if isinstance(self.model.get("cover"), dict) else {}
        title = _s(cover.get("title")) or _s(self.model.get("docTitle")) or "Accessibility report"
        meta = "".join(f"<li>{_t(m)}</li>" for m in _list(cover.get("meta")) if _s(m))
        return ('<header class="cover"><div class="cover-top"><div>'
                f'<h1 id="report-title">{_t(title)}</h1>'
                + (f'<p class="subtitle">{_t(cover.get("subtitle"))}</p>' if _s(cover.get("subtitle")) else "")
                + (f'<ul class="meta">{meta}</ul>' if meta else "")
                + f'</div><img class="cover-logo" src="{_logo_uri()}" alt="Mova iO"></div>'
                + ("" if self._has_identity_block() else self.identity_table())
                + "</header>")

    @staticmethod
    def _is_identity_block(block) -> bool:
        return block_kind(block) == "table" and (
            block.get("role") == "identity" or block.get("caption") == "Document and report identity")

    def _has_identity_block(self) -> bool:
        return any(self._is_identity_block(b) for b in _list(self.model.get("blocks")))

    def identity_table(self, caption: str = "Report identity") -> str:
        """Identity from the SERVER. A model's own identity table is replaced by this one, so a PDF
        never shows a client-side value beside a contradicting server value."""
        ident = self.identity
        rows = [("Report", f"{_t(MODES.get(self.mode, self.mode))} · {_t(ident.get('kindLabel'))}"),
                ("Scan", _nr(ident.get("scanId")))]
        if ident.get("kind") == "file" or ident.get("file"):
            rows.append(("File", _nr(ident.get("file"))))
            rows.append(("Source checksum", _t(_hash_label(ident.get("sourceChecksum")))
                         + (f' <span class="muted">({_t(ident.get("sourceChecksumKind"))})</span>'
                            if ident.get("sourceChecksumKind") else "")))
            rows.append(("Corrected copy SHA-256", _t(_hash_label(ident.get("correctedSha256")))))
        rows += [
            ("Artifact version", _nr(ident.get("artifactVersion"))),
            ("Target", _nr(ident.get("targetLevel"))),
            ("Generated", _nr(ident.get("generatedAt"))),
            ("Platform version", _nr(ident.get("platformVersion"))),
        ]
        return (f'<table class="identity"><caption>{escape(caption)}</caption><tbody>'
                + "".join(f'<tr><th scope="row">{_t(k)}</th><td>{v}</td></tr>' for k, v in rows)
                + "</tbody></table>")

    def blocks(self) -> str:
        blocks = _list(self.model.get("blocks"))
        # The appendix is the only thing that starts on a new page. Put the break on the heading
        # that introduces it, when there is one, so the heading is never stranded.
        break_at = None
        for i, block in enumerate(blocks):
            if block_kind(block) == "appendixTable":
                j = i - 1
                while j >= 0 and block_kind(blocks[j]) in ("pageBreak", "gap", "text", "callout"):
                    j -= 1
                break_at = j if j >= 0 and block_kind(blocks[j]) == "heading" else i
                break
        out = []
        for i, block in enumerate(blocks):
            kind = block_kind(block)
            html = getattr(self, "b_" + kind)(block)
            if i == break_at and html:
                html = f'<div class="appendix-start">{html}</div>'
            out.append(html)
        return "\n".join(out)

    # existing block kinds ---------------------------------------------------------------------
    def b_heading(self, b):
        tag = self._heading_tag(b.get("level", 1))
        anchor = self._unique(b.get("id") or f"h{len(self.ids)}", "sec-")
        return f'<h{tag} id="{anchor}">{_t(b.get("text"))}</h{tag}>'

    def b_docTitle(self, b):
        return self.b_heading({"text": b.get("text"), "level": 2})

    def b_text(self, b):
        o = b.get("o") if isinstance(b.get("o"), dict) else {}
        text = _t(b.get("text"))
        if not text:
            return ""
        if o.get("bold"):
            text = f"<strong>{text}</strong>"
        size = o.get("size")
        cls = ""
        if isinstance(size, (int, float)):
            cls = "small" if size < 9.5 else "large" if size > 10.5 else ""
        style = f' style="color:{_color(o.get("color"))}"' if _color(o.get("color")) else ""
        return f'<p class="{cls}"{style}>{text}</p>' if cls else f"<p{style}>{text}</p>"

    def b_callout(self, b):
        o = b.get("o") if isinstance(b.get("o"), dict) else {}
        style = ""
        if _color(o.get("color")):
            style += f"border-left-color:{_color(o.get('color'))};"
        if _color(o.get("bg")):
            style += f"background:{_color(o.get('bg'))};"
        return f'<div class="callout" style="{style}"><p>{_t(b.get("text"))}</p></div>'

    def b_bullets(self, b):
        items = [i for i in _list(b.get("items")) if _s(i) is not None]
        if not items:
            return ""
        return "<ul>" + "".join(f"<li>{_t(i)}</li>" for i in items) + "</ul>"

    def b_checklist(self, b):
        items = []
        for it in _list(b.get("items")):
            if isinstance(it, dict):
                label, meta = _t(it.get("label")), _t(it.get("meta"))
            else:
                label, meta = _t(it), ""
            items.append(f'<li>☐ {label}' + (f'<br><span class="muted">{meta}</span>' if meta else "") + "</li>")
        return '<ul class="meta" style="font-size:9pt;color:#2B2330">' + "".join(items) + "</ul>" if items else ""

    def b_metricGrid(self, b):
        cards = []
        for c in _list(b.get("cards")):
            if not isinstance(c, dict):
                continue
            style = f' style="color:{_color(c.get("color"))}"' if _color(c.get("color")) else ""
            cards.append(f'<li><span class="metric-label">{_t(c.get("label"))}</span>'
                         f'<span class="metric-value"{style}>{_nr(c.get("value"))}</span>'
                         + (f'<span class="muted">{_t(c.get("sub"))}</span>' if _s(c.get("sub")) else "")
                         + "</li>")
        return f'<ul class="metrics">{"".join(cards)}</ul>' if cards else ""

    def b_donut(self, b):
        items = [i for i in _list(b.get("items")) if isinstance(i, dict)]
        if not items:
            return '<p class="muted">No data.</p>'
        values = [i.get("value") if isinstance(i.get("value"), (int, float)) else None for i in items]
        total = sum(v for v in values if v)
        segs = "".join(
            f'<div style="width:{100 * v / total:.3f}%;background:{_color(i.get("color")) or "#4B3460"}"></div>'
            for i, v in zip(items, values) if v and total)
        rows = []
        for i, v in zip(items, values):
            share = f"{round(100 * v / total)}%" if (v is not None and total) else NOT_RECORDED if v is None else "0%"
            swatch = f'<span class="swatch" style="background:{_color(i.get("color")) or "#4B3460"}"></span>'
            rows.append(f'<tr><th scope="row">{swatch}{_t(i.get("label"))}</th>'
                        f'<td>{_nr(v)}</td><td>{share}</td></tr>')
        caption = _t(b.get("caption")) or "Distribution"
        return (f'<table><caption>{caption}</caption><thead><tr><th scope="col">Category</th>'
                '<th scope="col" style="width:20%">Count</th><th scope="col" style="width:20%">Share</th>'
                f'</tr></thead><tbody>{"".join(rows)}</tbody></table>'
                + (f'<div class="stack">{segs}</div>' if segs else ""))

    def b_barChart(self, b):
        o = b.get("o") if isinstance(b.get("o"), dict) else {}
        items = [i for i in _list(b.get("items")) if isinstance(i, dict)]
        if not items:
            return '<p class="muted">No data.</p>'
        nums = [i.get("value") for i in items if isinstance(i.get("value"), (int, float))]
        top = o.get("max") if isinstance(o.get("max"), (int, float)) and o.get("max") > 0 else max(nums + [1]) or 1
        suffix = _t(o.get("suffix"))
        rows = []
        for i in items:
            v = i.get("value") if isinstance(i.get("value"), (int, float)) else None
            width = max(0.0, min(100.0, 100.0 * v / top)) if v is not None else 0
            fill = _color(i.get("color")) or "#4B3460"
            rows.append(f'<tr><th scope="row">{_t(i.get("label"))}</th>'
                        f'<td><div class="bar-track"><div class="bar-fill" style="width:{width:.2f}%;background:{fill}"></div></div></td>'
                        f'<td>{_nr(v)}{suffix if v is not None else ""}</td></tr>')
        caption = _t(o.get("caption") or b.get("caption")) or "Chart data"
        return (f'<table class="bars"><caption>{caption}</caption><thead><tr>'
                '<th scope="col" style="width:46%">Item</th><th scope="col">Relative size</th>'
                '<th scope="col" style="width:14%">Value</th></tr></thead>'
                f'<tbody>{"".join(rows)}</tbody></table>')

    def _table(self, b, extra_class="", table_id=None):
        headers = _list(b.get("headers"))
        rows = [r for r in _list(b.get("rows")) if isinstance(r, list)]
        widths = [w for w in _list(b.get("widths")) if isinstance(w, (int, float)) and w > 0]
        cols = ""
        if widths and len(widths) == len(headers) and sum(widths) > 0:
            total = sum(widths)
            cols = "<colgroup>" + "".join(f'<col style="width:{100 * w / total:.2f}%">' for w in widths) + "</colgroup>"
        head = "".join(f'<th scope="col">{_t(h) or "<span class=muted>(unlabelled)</span>"}</th>' for h in headers)
        body = []
        for r in rows:
            cells = "".join(f"<td>{_t(c)}</td>" for c in r)
            body.append(f"<tr>{cells}</tr>")
        if not body:
            body.append(f'<tr><td colspan="{max(1, len(headers))}">No records.</td></tr>')
        caption = f"<caption>{_t(b.get('caption'))}</caption>" if _s(b.get("caption")) else ""
        ident = f' id="{table_id}"' if table_id else ""
        return (f'<table class="{extra_class}"{ident}>{caption}{cols}'
                + (f"<thead><tr>{head}</tr></thead>" if head else "")
                + f'<tbody>{"".join(body)}</tbody></table>')

    def b_table(self, b):
        if self._is_identity_block(b):
            return self.identity_table(_s(b.get("caption")) or "Document and report identity")
        return self._table(b)

    def b_appendixTable(self, b):
        anchor = self._unique(b.get("id") or "appendix", "apx-")
        shown = len([r for r in _list(b.get("rows")) if isinstance(r, list)])
        note = ""
        if b.get("complete") is not True:
            total = b.get("totalRecords")
            total_txt = f"{total:,}" if isinstance(total, int) else "an unknown number of"
            limit = _s(b.get("sourceLimit")) or _s(b.get("limitReason")) or _s(b.get("note")) \
                or "the records the source returned to this report"
            note = (f'<p class="partial">Partial: {shown:,} of {total_txt} records '
                    f"(source limited to {escape(limit)}).</p>")
        stable = f'<p class="muted">Record set: {_t(b.get("id"))}</p>' if _s(b.get("id")) else ""
        return note + self._table(b, "appendix", anchor) + stable

    def b_beforeAfter(self, b):
        out = []
        tag = self._child_tag()
        for it in _list(b.get("items")):
            if not isinstance(it, dict):
                continue
            size = len(_s(it.get("before")) or "") + len(_s(it.get("after")) or "")
            out.append(
                f'<section class="card{" compact" if size < 700 else ""}">'
                f'<h{tag}>{_t(it.get("label"), "Change")}</h{tag}>'
                + (f'<p class="muted">{_t(it.get("note"))}</p>' if _s(it.get("note")) else "")
                + f'<p class="label">Before</p><div class="value before">{_nr(it.get("before"))}</div>'
                + f'<p class="label">After</p><div class="value after">{_nr(it.get("after"))}</div>'
                + "</section>")
        return "".join(out)

    def b_pageBreak(self, b):
        return ""          # soft section break: never leave a near-empty page

    def b_gap(self, b):
        return '<div class="gap"></div>'

    def b_image(self, b):
        src = b.get("src")
        if not src:
            return f'<p class="no-preview">{PREVIEW_UNAVAILABLE}</p>'
        alt = _t(b.get("alt")) or _t(b.get("caption")) or "Document preview"
        caption = f"<figcaption>{_t(b.get('caption'))}</figcaption>" if _s(b.get("caption")) else ""
        return f'<figure class="image"><img src="{src}" alt="{alt}">{caption}</figure>'

    def b_link(self, b):
        return f"<p>{self._link(b.get('text'), b.get('href'))}</p>"

    # contract v1 block kinds ------------------------------------------------------------------
    def b_decisionSummary(self, b):
        rows = []
        for it in _list(b.get("items")):
            if not isinstance(it, dict):
                continue
            value = it.get("value")
            shown = f"{value:,}" if isinstance(value, int) and not isinstance(value, bool) else _nr(value)
            rows.append(f'<tr><th scope="row">{_t(it.get("label"), _s(it.get("key")) or "")}</th>'
                        f'<td class="num">{shown}</td><td>{_t(it.get("detail"))}</td></tr>')
        caption = _t(b.get("caption")) or "Decision summary"
        return (f'<table class="decision"><caption>{caption}</caption><thead><tr>'
                '<th scope="col">Measure</th><th scope="col" style="width:20%">Count</th>'
                f'<th scope="col">What it means</th></tr></thead><tbody>{"".join(rows)}</tbody></table>')

    _STAGE_STATUS = {"done": "Done", "pending": "Pending", "not_started": "Not started", "unknown": "Unknown"}

    def b_stageStrip(self, b):
        items = []
        for it in _list(b.get("items")):
            if not isinstance(it, dict):
                continue
            status = it.get("status") if it.get("status") in self._STAGE_STATUS else "unknown"
            items.append(f'<li class="{status}"><span class="stage-status">{_t(it.get("label"))}: '
                         f'{self._STAGE_STATUS[status]}</span>'
                         f'<span>{_nr(it.get("value"))}</span>'
                         + (f'<br><span class="muted">{_t(it.get("detail"))}</span>' if _s(it.get("detail")) else "")
                         + "</li>")
        return f'<ol class="stages">{"".join(items)}</ol>' if items else ""

    def _location_html(self, location) -> str:
        text = _location_text(location)
        href = location.get("href") if isinstance(location, dict) else None
        return self._link(text, href) if href else escape(text)

    def _response(self, options, notice, title="Your response") -> str:
        opts = [o for o in _list(options) if _s(o)] or list(DEFAULT_RESPONSE_OPTIONS)
        boxes = "".join(f'<span class="box">☐ {_t(o)}</span> ' for o in opts)
        return ('<div class="response">'
                f'<p class="boxes"><span class="label">{escape(title)}:</span> {boxes}</p>'
                '<p class="small">Reviewer, date and notes:</p><div class="write-line"></div>'
                f'<p class="notice">{_t(notice) or escape(DEFAULT_RESPONSE_NOTICE)}</p>'
                "</div>")

    @staticmethod
    def _facts(pairs) -> str:
        return ('<p class="facts">'
                + ' <span class="sep">·</span> '.join(
                    f'<span class="fact"><span class="label">{k}:</span> {v}</span>' for k, v in pairs)
                + "</p>")

    _TECH = {"verified": "Verified by re-scan", "pending": "Re-validation pending",
             "not_run": "Not run", "unknown": NOT_RECORDED}
    _HUMAN = {"pending": "Awaiting confirmation", "accepted": "Accepted", "edited": "Accepted with edits",
              "rejected": "Rejected", "unable": "Unable to verify",
              "stale": "Stale: the file changed after this decision"}

    # A card that is small enough to move whole to the next page does so; a long one flows, so
    # a long before/after value never pushes a mostly-empty page ahead of it.
    COMPACT_CHARS = 420

    def b_changeCard(self, b):
        anchor = self._unique(b.get("id") or f"change{len(self.ids)}", "chg-")
        tag = self._child_tag()
        before, after = _s(b.get("before")), _s(b.get("after"))
        # The model carries FULL strings. Reviewer/summary packets shorten a flagged field; Full
        # evidence never does, whatever the flag says.
        shortened = []
        if self.mode != "full":
            if b.get("beforeTruncated") and before is not None and clamp_text(before) != before:
                before = clamp_text(before)
                shortened.append("before")
            if b.get("afterTruncated") and after is not None and clamp_text(after) != after:
                after = clamp_text(after)
                shortened.append("after")
        reason = _s(b.get("reason"))
        image = b.get("image") if isinstance(b.get("image"), dict) else None
        has_image = bool(image and image.get("src")) and b.get("imageStatus") != "unavailable"
        size = len(before or "") + len(after or "") + len(reason or "")
        compact = size < self.COMPACT_CHARS
        tech = b.get("technical") if isinstance(b.get("technical"), dict) else {}
        human = b.get("human") if isinstance(b.get("human"), dict) else {}
        title = _s(b.get("title")) or "Recorded change"
        criterion = " · ".join(x for x in (_s(b.get("criterion")), _s(b.get("criterionName"))) if x)
        tech_status = tech.get("status") if tech.get("status") in self._TECH else "unknown"
        h_status = human.get("status") if human.get("status") in self._HUMAN else "pending"
        who = " · ".join(x for x in (_s(human.get("reviewer")), _s(human.get("at"))) if x)
        facts = []
        if criterion and criterion not in title:
            facts.append(("Criterion", escape(criterion)))
        facts += [
            ("Location", self._location_html(b.get("location"))),
            ("Technical check", self._TECH[tech_status]),
            ("Human confirmation", self._HUMAN[h_status] + (f" ({escape(who)})" if who else "")
             + (" (decisions not loaded)" if human.get("loaded") is False else "")),
        ]
        where = _location_text(b.get("location"))
        # Several changes often share a criterion; the location is what tells their bookmarks apart.
        heading_where = f' <span class="where">· {escape(where)}</span>' if where != LOCATION_NOT_RECORDED else ""
        text = [self._facts(facts)]
        if _s(tech.get("detail")):
            text.append(f'<p class="muted">{_t(tech.get("detail"))}</p>')
        full_ref = _s(b.get("fullRef"))
        before_html = escape(before) if before is not None else NOT_RECORDED
        after_html = escape(after) if after is not None else NOT_RECORDED
        if compact and not has_image:
            # Short values sit side by side; a table keeps the pair together and labelled.
            text.append('<table class="ba"><thead><tr><th scope="col">Before</th><th scope="col">After</th>'
                        f'</tr></thead><tbody><tr><td class="before">{before_html}</td>'
                        f'<td class="after">{after_html}</td></tr></tbody></table>')
        else:
            text.append(f'<p class="label">Before</p><div class="value before">{before_html}</div>')
            text.append(f'<p class="label">After</p><div class="value after">{after_html}</div>')
        if shortened:
            ref = full_ref if full_ref and "Full evidence" in full_ref else (
                f"Full evidence report, record {full_ref}" if full_ref else "Full evidence report")
            text.append(f'<p class="muted">The {" and ".join(shortened)} text is shortened in this packet. '
                        f"The full text is in the {escape(ref)}.</p>")
        text.append(f'<p><span class="label">Reason:</span> {escape(reason) if reason else REASON_NOT_RECORDED}</p>')
        if _s(human.get("note")):
            text.append(f'<p class="small">Reviewer note: {_t(human.get("note"))}</p>')
        if _s(human.get("editedValue")):
            text.append(f'<p class="label">Edited value</p><div class="value">{_t(human.get("editedValue"))}</div>')
        if h_status == "stale":
            text.append(f'<p class="small">Decision recorded against {_nr(human.get("boundSha256"))}; '
                        f'the current file is {_nr(human.get("currentSha256"))}.</p>')
        if has_image:
            alt = _t(image.get("alt")) or "Preview of the changed content"
            caption = f"<figcaption>{_t(image.get('caption'))}</figcaption>" if _s(image.get("caption")) else ""
            figure = f'<figure class="card-figure"><img src="{image["src"]}" alt="{alt}">{caption}</figure>'
            content = f'<div class="card-body">{figure}<div class="card-text">{"".join(text)}</div></div>'
        else:
            content = "".join(text) + f'<p class="no-preview">{PREVIEW_UNAVAILABLE}</p>'
        return (f'<section class="card change{" compact" if compact else ""}" id="{anchor}">'
                f'<h{tag}>{escape(title)}{heading_where}</h{tag}>'
                + content
                + self._response(b.get("responseOptions"), b.get("responseNotice"))
                + (f'<p class="muted">Change id: {_t(b.get("id"))}</p>' if _s(b.get("id")) else "")
                + "</section>")

    _FINDING_STATUS = {"open": "Open", "human_check": "Needs a human check",
                       "not_checked": "Not checked (not a pass)"}

    def b_findingCard(self, b):
        anchor = self._unique(b.get("id") or f"finding{len(self.ids)}", "fnd-")
        tag = self._child_tag()
        priority = b.get("priority") if b.get("priority") in ("high", "medium", "low") else "medium"
        steps = [s for s in _list(b.get("steps")) if _s(s)]
        rank = b.get("rank")
        title = _s(b.get("title")) or "Remaining work"
        criterion = " · ".join(x for x in (_s(b.get("criterion")), _s(b.get("criterionName"))) if x)
        heading = escape(title)
        if isinstance(rank, int) and not isinstance(rank, bool):
            heading = f"{rank}. {heading}"
        status = self._FINDING_STATUS.get(b.get("status"), NOT_RECORDED)
        size = len(_s(b.get("description")) or "") + sum(len(_s(s) or "") for s in steps)
        priority_txt = priority.capitalize() + (f" (severity {_t(b.get('severity')).lower()})" if _s(b.get("severity")) else "")
        facts = [("Status", escape(status)), ("Priority", priority_txt)]
        if criterion and criterion not in title:
            facts.append(("Criterion", escape(criterion)))
        facts += [("Location", self._location_html(b.get("location"))),
                  ("Owner", _t(b.get("owner"), "Unassigned"))]
        description = _s(b.get("description"))
        short = (self.mode != "full" and b.get("descriptionTruncated") and description is not None
                 and clamp_text(description) != description)
        if short:
            description = clamp_text(description)
        body = [self._facts(facts),
                f'<p><span class="label">Issue:</span> {escape(description) if description else NOT_RECORDED}</p>']
        if short:
            body.append('<p class="muted">Shortened in this packet. The full text is in the Full evidence report.</p>')
        if _s(b.get("impact")):
            body.append(f'<p><span class="label">Who is affected:</span> {_t(b.get("impact"))}</p>')
        if steps:
            body.append('<p class="label">How to fix</p><ol>' + "".join(f"<li>{_t(s)}</li>" for s in steps) + "</ol>")
        body.append(f'<p><span class="label">How to confirm:</span> {_t(b.get("recheck"), NOT_RECORDED)}</p>')
        options = b.get("responseOptions") or ["Fixed and rechecked", "Needs help", "Not applicable"]
        return (f'<section class="card finding {priority}{" compact" if size < 2 * self.COMPACT_CHARS else ""}" id="{anchor}">'
                f'<h{tag}>{heading}</h{tag}>' + "".join(body)
                + self._response(options, b.get("responseNotice"), "Follow-up")
                + (f'<p class="muted">Finding id: {_t(b.get("id"))}</p>' if _s(b.get("id")) else "")
                + "</section>")

    def b_comparison(self, b):
        prev = b.get("previous") if isinstance(b.get("previous"), dict) else None
        out = ['<div class="callout">']
        if b.get("status") != "compared":
            out.append(f'<p><strong>Change since the previous report: Unknown.</strong> {_t(b.get("reason"))}</p>')
        else:
            out.append(f'<p><strong>Compared with a previous report.</strong> {_t(b.get("reason"))}</p>')
        if prev:
            out.append(f'<p class="small">Previous: scan {_nr(prev.get("scanId"))} · '
                       f'{_nr(prev.get("generatedAt"))} · SHA-256 {_nr(prev.get("sha256"))}</p>')
        out.append("</div>")
        if b.get("status") == "compared":
            persisting = b.get("persisting")
            for key, label in (("resolved", "No longer reported"), ("introduced", "Newly reported")):
                items = [i for i in _list(b.get(key)) if isinstance(i, dict)]
                out.append(f'<p class="label">{label}: {len(items)}</p>')
                if items:
                    out.append("<ul>" + "".join(
                        f'<li>{_t(i.get("title"))} · {escape(_location_text(i.get("location")))}'
                        f' <span class="muted">({_t(i.get("id"))})</span></li>' for i in items) + "</ul>")
            out.append(f'<p class="label">Still reported: {_nr(persisting)}</p>')
        return "".join(out)


def _head_strings(identity: dict, model: dict) -> tuple[str, str, str]:
    name = _s(identity.get("file")) or _s(model.get("docTitle")) or "Accessibility report"
    left = _shorten(name, 70)
    if _s(identity.get("correctedSha256")):
        right = "Corrected copy SHA-256 " + str(identity["correctedSha256"])[:12] + "…"
    elif _s(identity.get("sourceSha256")):
        right = "Source SHA-256 " + str(identity["sourceSha256"])[:12] + "…"
    elif _s(identity.get("scanId")):
        right = "Scan " + _shorten(str(identity["scanId"]), 28)
    else:
        right = "Identity not recorded"
    foot = "Generated " + (_s(identity.get("generatedAt")) or NOT_RECORDED)
    foot += " · Mova iO ACP " + (_s(identity.get("platformVersion")) or "version not recorded")
    return left, right, foot


def render_html(model: dict, identity: dict | None = None, mode: str = "full",
                base_url: str | None = None) -> str:
    """The semantic HTML the PDF is made of. `model` must already have passed validate_request."""
    identity = dict(identity or {})
    lang = model.get("lang") if isinstance(model.get("lang"), str) and _LANG.fullmatch(model.get("lang")) else "en-US"
    renderer = _Renderer(model, identity, mode, base_url)
    title = _s(model.get("docTitle")) or _s((model.get("cover") or {}).get("title")) or "Accessibility report"
    if identity.get("file") and identity["file"] not in title:
        title = f"{title} · {identity['file']}"
    left, right, foot = _head_strings(identity, model)
    css = _CSS % {"regular": FONT_REGULAR.as_uri(), "bold": FONT_BOLD.as_uri(),
                  "head_left": css_string(left), "head_right": css_string(right),
                  "foot_left": css_string(foot)}
    cover = renderer.cover()
    body = renderer.blocks()
    renderer.level = 1
    about_tag = 2
    about = (f'<section class="about"><h{about_tag}>About this PDF</h{about_tag}>'
             f'<p class="small">{escape(STRUCTURE_STATEMENT)}</p></section>')
    return ("<!DOCTYPE html>"
            f'<html lang="{escape(lang)}"><head><meta charset="utf-8">'
            f"<title>{escape(title)}</title>"
            '<meta name="author" content="Mova iO Accessibility Compliance Platform">'
            f'<meta name="dcterms.created" content="{_t(identity.get("generatedAt"))}">'
            f"<style>{css}</style></head><body>"
            f"{cover}<main>{body}</main>{about}</body></html>")


def _fetcher(url, *args, **kwargs):
    from weasyprint import default_url_fetcher
    if url.startswith("data:image/png;base64,") or url in _FONT_URLS:
        return default_url_fetcher(url, *args, **kwargs)
    raise ValueError("the report renderer loads only embedded PNG images and bundled fonts")


def render_pdf(model: dict, identity: dict | None = None, mode: str = "full",
               base_url: str | None = None) -> bytes:
    """Render a validated model to a tagged PDF."""
    from weasyprint import HTML
    html = render_html(model, identity, mode, base_url)
    return HTML(string=html, url_fetcher=_fetcher).write_pdf(pdf_variant="pdf/ua-1")


KIND_LABELS = {"file": "File report", "scan": "Scan report", "remediation": "Remediation report"}


def server_identity(*, scan_id: str, kind: str, file: str | None, record: dict | None,
                    client_identity, platform_version: str | None, now: datetime | None = None) -> dict:
    """Identity for the report header: server facts overwrite the client's.

    `file_records.checksum` is whatever the SOURCE system reported (Drive md5, SharePoint
    quickXorHash), so it is only called a SHA-256 when it has that shape; otherwise it is shown as
    the source's own checksum, labelled as such.
    """
    client = client_identity if isinstance(client_identity, dict) else {}
    record = record or {}
    checksum = _s(record.get("checksum"))
    corrected = _s(record.get("corrected_sha256"))
    is_sha = bool(checksum and re.fullmatch(r"(sha256:)?[0-9a-fA-F]{64}", checksum))
    stamp = (now or datetime.now(timezone.utc)).strftime("%Y-%m-%d %H:%M UTC")
    return {
        "kind": kind,
        "kindLabel": KIND_LABELS.get(kind, kind),
        "scanId": scan_id,
        "file": file,
        "sourceSha256": checksum.split(":")[-1].lower() if is_sha else None,
        "sourceChecksum": checksum,
        "sourceChecksumKind": ("SHA-256" if is_sha else "as recorded by the source system") if checksum else None,
        "correctedSha256": corrected,
        "generatedAt": stamp,
        "platformVersion": platform_version,
        "artifactVersion": _s(client.get("artifactVersion")) if isinstance(client.get("artifactVersion"), (str, int)) else None,
        "targetLevel": _s(client.get("targetLevel")) if isinstance(client.get("targetLevel"), str) else None,
    }


def download_name(kind: str, mode: str, scan_id: str, file: str | None) -> str:
    stem = re.sub(r"[^A-Za-z0-9._-]+", "-", file or scan_id).strip(".-")[:80] or "report"
    return f"accessibility-{kind}-{mode}-{stem}.pdf"

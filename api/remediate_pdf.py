"""Server-side PDF remediation (ADR 0005 step 4).

Two-stage:
  1. pikepdf — Root-level structural fixes from the vendored DigitalA11y engine:
     document language (`/Lang`) and display-document-title (ViewerPreferences); plus
     genuine image alt text (WCAG 1.1.1) — every tagged /Figure struct element that
     lacks /Alt gets a description of its page from the local vision (llava-class)
     model, written as /Alt (exactly what the pdf.missing-alt-text analyser reads).
  2. pypdf — document metadata (docinfo): a filename-derived /Title (WCAG 2.4.2) when
     one is missing, plus the provenance stamp. Metadata is written with pypdf rather
     than pikepdf because pikepdf's docinfo writes persist nondeterministically once
     libxml2 is loaded in the same long-lived worker process (office/HTML remediation
     use lxml); pypdf is pure-Python and reliable, and `clone_from` preserves the
     stage-1 catalog fixes (/Lang, ViewerPreferences, and the /Alt attributes).

Alt text only applies to TAGGED PDFs with figure struct elements. Untagged PDFs (no
StructTreeRoot) are a separate structural-tagging finding and still route to human
review, as does any figure the vision model can't caption (AI off / unavailable).
"""
from __future__ import annotations
import os
import re
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace

# Bound worst-case latency: vision inference is slow on CPU (~30-90 s/image), so cap
# figures captioned per document; the rest defer to review (the residual re-scan routes
# them to HITL). Default 10: 10 × 90 s ≈ 15 min worst-case vs 25 × 90 s ≈ 37 min.
# Raise ACP_VISION_MAX_FIGURES when a GPU vision backend is configured.
_VISION_MAX_FIGURES = int(os.environ.get("ACP_VISION_MAX_FIGURES", "10"))
# Render at 150 DPI — enough detail for vision without excessive cost (mirrors the
# vendored PdfAltTextFixer).
_RENDER_SCALE = 150 / 72
# A page thumbnail stands in for a whole page, not one embedded picture, so it needs more
# pixels than the 96px default before the card can scale it up legibly. ~320px of a letter
# page is ≈40KB of base64 in hitl_queue.proposals — one row per untagged PDF, not per finding.
_PAGE_THUMB_EDGE = 320
# An applied-fix thumbnail is a receipt, not evidence a reviewer must read: "Recent AI fixes"
# renders it at 36px. Matches remediate_office's 96px default so both formats store the same
# size of row, and keeps a 25-figure document's applied_fixes well under a megabyte.
_FIX_THUMB_EDGE = 96


# ── 1.4.3 / 1.4.6 — deterministic text-contrast recolouring ────────────────────
# WHICH colours move, and where to, is decided entirely by
# office_structure.pdf_contrast_recolor_plan — the same per-glyph measurement the detector
# reports, against the background structurally resolved BEHIND the glyphs. This fixer runs
# unattended, so it must never act on an assumption about the page: the earlier
# white-background model darkened white-on-black cover text from a passing 21:1 to a 3.66:1
# AA FAILURE, silently. Now a colour is only rewritten where it is proved to fail on every
# background it is actually painted on, and only toward a colour proved to clear them all.
# Fill-colour operators this fixer understands. sc/scn (arbitrary colourspaces) are left
# alone — recolouring a value we can't interpret risks corrupting it, and the re-scan
# verify-guard means an unclear fix is simply never claimed.
_FILL_GRAY, _FILL_RGB, _FILL_CMYK = "g", "rg", "k"
# Operand count that makes each operator's colour readable as a pdfplumber-shaped colour.
_FILL_ARITY = {_FILL_GRAY: 1, _FILL_RGB: 3, _FILL_CMYK: 4}
# Path-painting operators: a fill colour consumed by one of these coloured a SHAPE, and a
# shape/background fill must never be darkened (it would invert the contrast problem).
_PAINT_OPS = {"f", "F", "f*", "B", "B*", "b", "b*", "S", "s"}
_TEXT_SHOW_OPS = {"Tj", "TJ", "'", '"'}


def _fill_hex(op: str, vals: list[float]) -> str | None:
    """A fill operator's operands → the 6-hex the recolour plan is keyed by. Runs them
    through office_structure._pdf_color_hex — the same function that hexed the pdfplumber
    colour the plan was built from — so the two sides round identically and the lookup is
    an exact match or nothing."""
    if len(vals) != _FILL_ARITY.get(op, -1):
        return None
    import office_structure as _os
    return _os._pdf_color_hex(vals)


def _fill_operands(op: str, hex6: str) -> tuple[str, list[float]]:
    """A replacement hex → (operator, operands), keeping the original colourspace wherever
    that is exact. DeviceGray stays `g` while the colour is neutral (a hue-preserving
    recolour of a grey always is) and widens to `rg` only if it somehow isn't; DeviceCMYK
    stays `k` as a zero-black mix, which `_pdf_color_hex` reads back to exactly this RGB."""
    r, g, b = (int(hex6[i:i + 2], 16) / 255 for i in (0, 2, 4))
    if op == _FILL_GRAY:
        return (_FILL_GRAY, [r]) if r == g == b else (_FILL_RGB, [r, g, b])
    if op == _FILL_CMYK:
        return _FILL_CMYK, [1 - r, 1 - g, 1 - b, 0.0]
    return _FILL_RGB, [r, g, b]


def _colours_text(ops, idx: int, in_text: bool) -> bool:
    """Does the fill colour set at ops[idx] colour TEXT? Inside BT..ET it unambiguously
    does. Outside, scan forward: it colours text if a text object begins and shows text
    before any path-painting op, graphics-state restore (Q), or another fill op consumes
    it — the common `set colour; BT … Tj … ET` pattern. Conservative by construction:
    any ambiguity (Q, painting, re-set) leaves the colour untouched."""
    if in_text:
        return True
    for operands, operator in ops[idx + 1:]:
        name = str(operator)
        if name in _TEXT_SHOW_OPS:
            return True
        if name in _PAINT_OPS or name == "Q" or name in (_FILL_GRAY, _FILL_RGB, _FILL_CMYK):
            return False
    return False


def _known_page(value) -> int | None:
    """A real one-based page number, or None. `bool` is an int in Python and is refused."""
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None


def _single_page(pages) -> int | None:
    """The page an aggregate edit touched, when it touched exactly one; None otherwise.

    Several of these fixers write one diff record for a whole pass ("3 run(s) rewritten").
    A record that spans pages has no single page, and picking the first would locate the
    other pages' edits wrongly — so a multi-page pass records no page at all."""
    distinct = {p for p in (pages or ()) if _known_page(p)}
    return next(iter(distinct)) if len(distinct) == 1 else None


def _fix_pdf_text_contrast(pdf, *, pages: list | None = None) -> int:
    """Recolour failing TEXT fill colours in every page's content stream so the document's
    text meets the contrast floors (WCAG 1.4.3/1.4.6). Deterministic content-stream rewrite
    via pikepdf, gated on `office_structure.pdf_contrast_recolor_plan`: a colour moves only
    where it is measured to fail against the background actually resolved behind its
    glyphs, and only to a colour proved to clear every such background — so white text on a
    dark cover, which already passes at 21:1, is left alone. Two guards, both required: the
    plan says WHICH colour and WHAT to, `_colours_text` says the operator colours TEXT, so
    shapes, backgrounds and images are never altered. Anything unresolved is left untouched
    rather than guessed at. Returns the number of colour operations recoloured.

    `pages`, when given, receives the one-based number of every page whose content stream was
    actually rewritten — not every page the plan named, since a page can fail to rewrite."""
    import io
    import pikepdf
    import office_structure as _os
    try:
        buf = io.BytesIO()
        pdf.save(buf)                    # measure the document exactly as it stands now
        plan = _os.pdf_contrast_recolor_plan(buf.getvalue())
    except Exception:
        return 0                         # backgrounds unresolvable → change nothing
    if not plan:
        return 0
    changed_total = 0
    for page_index, page in enumerate(pdf.pages):
        page_plan = plan.get(page_index)
        if not page_plan:
            continue
        try:
            ops = [[list(operands), operator] for operands, operator
                   in pikepdf.parse_content_stream(page)]
        except Exception:
            continue
        changed = 0
        in_text = False
        for i, (operands, operator) in enumerate(ops):
            name = str(operator)
            if name == "BT":
                in_text = True
                continue
            if name == "ET":
                in_text = False
                continue
            if name not in (_FILL_GRAY, _FILL_RGB, _FILL_CMYK):
                continue
            try:
                vals = [float(v) for v in operands]
            except (TypeError, ValueError):
                continue
            hex6 = _fill_hex(name, vals)
            new_hex = page_plan.get(hex6) if hex6 else None
            if not new_hex:                       # passes already, or not resolvable
                continue
            if not _colours_text(ops, i, in_text):
                continue
            new_op, new_vals = _fill_operands(name, new_hex)
            ops[i][0] = new_vals
            ops[i][1] = pikepdf.Operator(new_op)
            changed += 1
        if changed:
            try:
                page.Contents = pdf.make_stream(pikepdf.unparse_content_stream(
                    [(operands, operator) for operands, operator in ops]))
                changed_total += changed
                if pages is not None:
                    pages.append(page_index + 1)
            except Exception:
                continue                          # one unwritable page never sinks the rest
    return changed_total


def _propose_reading_order(pdf, source_path: str, *, ai_enabled: bool, scan_id, file,
                           proposals) -> None:
    """WCAG 1.3.2 — for an UNTAGGED (scanned/flat) PDF, vision proposes the page reading
    order for a human to confirm. Never auto-applied (a machine can't be trusted to fix
    reading order on a scan) and bounded to the first page. No-op when tagged, AI off, or no
    vision model — those degrade to the existing review path."""
    if proposals is None or not ai_enabled:
        return
    try:
        if "/StructTreeRoot" in pdf.Root:
            return  # tagged → reading order comes from the structure tree, not a guess
    except Exception:
        return
    try:
        import ai as _ai
        if not _ai.vision_is_available():
            return
    except Exception:
        return
    png = _render_page_png(source_path, 1)
    if not png:
        return
    res = _ai.describe_reading_order(png, filename=file, scan_id=scan_id, file=file)
    if not res:
        return
    import proposals as _prop
    proposals.append(_prop.proposal(
        locator="page 1", before="(untagged PDF — reading order not defined for assistive tech)",
        proposed_value=res["order"],
        rationale="AI read the page layout and proposed a reading order — confirm it matches the intended flow",
        source=f"AI vision model ({res['model']})",
        kind="reading-order",
        model=res.get("model"),
        # The confirmed order is the re-authoring instruction and the evidence of what the
        # order should be — "page 1" addresses no writable object and apply_pdf_approved
        # routes only pdf:fig:/pdf:field:. Unflagged, confirming this card would count as
        # content the file still owes and block certification for good (see proposals.py).
        explain_only=True,
        # The same render the model looked at. Asking a human to confirm a reading order for a
        # page they cannot see makes the approval meaningless. Rendered larger than an embedded
        # image's thumb: a whole page shrunk to 96px is an unreadable grey rectangle. `kind`
        # tells the card to show it at page size, and to describe it as a page, not an image.
        thumb=_prop.thumb_b64(png, max_edge=_PAGE_THUMB_EDGE)))


def _propose_structure_map(pdf, source_path: str, *, proposals) -> None:
    """WCAG 1.3.1 — for an UNTAGGED PDF, derive the document's heading structure
    deterministically (font-size rank, no AI) and hand it to a human as a structure map to
    confirm. Tags can't be auto-written safely (re-authoring), but "here is the structure"
    turns the re-author-this-file dead end into a one-click confirmation whose approved map
    is the tagging instruction + compliance evidence. Self-gating: a tagged PDF, or one
    with no visually-distinct headings (uniform-font / scanned), yields nothing — we never
    fabricate a structure that isn't in the document (ADR 0016)."""
    if proposals is None:
        return
    try:
        if "/StructTreeRoot" in pdf.Root:
            return                                 # tagged — structure already exists
    except Exception:
        return
    headings = _extract_pdf_headings(source_path)
    if len(headings) < _MIN_OUTLINE_ENTRIES:
        return
    from proposals import heading_level_sequence, proposal
    levels = heading_level_sequence(size for _t, _p, size in headings)
    lines = [f"Heading {lvl} · “{title}” (p.{page + 1})"
             for (title, page, _size), lvl in zip(headings, levels)]
    proposals.append(proposal(
        locator="document structure",
        before="(untagged PDF — no structure tree for assistive technology)",
        proposed_value="\n".join(lines),
        rationale=("Derived deterministically from the document's font hierarchy — confirm "
                   "it matches the intended structure. The approved map is the tagging "
                   "instruction for re-authoring and the compliance evidence of what the "
                   "structure should be."),
        source="font hierarchy in document order (deterministic) — human confirmation required",
        kind="structure-map",
        # The docstring above already says it: the approved map IS the tagging instruction and
        # the compliance evidence. Nothing is written into the PDF, so the certify gate must not
        # wait for a write that will never come (see proposals.py).
        explain_only=True))


# ── WCAG 2.4.4 Link Purpose on PDF: assessed, never proposed ──────────────────────────
#
# There WAS a proposer here. It read each /Link annotation's /URI, and where that URL appeared
# verbatim in the page text (office_structure.pdf_link_purpose_check's predicate) it derived a
# descriptive label from the target and enqueued it as an approvable review card.
#
# The card could not be honoured. Its locator was the raw URI, and the only PDF write-back
# entry, apply_pdf_approved, routes `pdf:fig:` (→ /Alt) and `pdf:field:` (→ /TU) and nothing
# else — a URI locator fell through to `unknown`. Approving it returned applied=[],
# unresolved=[the URI], the bytes unchanged, and a re-scan still reported PDF_LINK_RAW_URL.
# Worse than a missing card: the finding LOOKED handled, and because
# store.count_unapplied_approved_values counts any approved row holding a locator + value, the
# document then carried approved content nothing would ever write — so it could never certify
# and never reached Publish.
#
# Building the write-back is the alternative, and it is the one the original docstring already
# ruled out: the label is drawn by text-showing operators, so replacing it genuinely re-flows
# the page's glyph widths. That is a re-authoring edit, not a remediation this tool performs
# unattended (ADR 0016), and the same reason 1.3.1 re-tagging is not auto-written.
#
# So 2.4.4 on PDF is EXPLAIN-ONLY: assessed (the detector still fires and still routes to a
# human), never proposed. The residual FAIL reaches the reviewer through the normal
# queue_hitl_review_for_file path as a judgement card carrying no unwritten value, which is
# both honest about what ACP can do and — unlike the proposal card — resolvable.
# remediation_capability.REMEDIATION["pdf"]["2.4.4"] records the lane as HUMAN, which is what
# stops the matrix claiming a fix path that cannot complete.


def _propose_pdf_headings(pdf, source_path: str, *, proposals) -> None:
    """WCAG 2.4.6 Headings and Labels — a TAGGED PDF (has a StructTreeRoot) that carries NO
    heading struct elements: assistive tech then has no headings to navigate by. This is the
    complement of the 1.3.1 structure-map proposer (which handles UNTAGGED PDFs) — here the
    tagging exists but omits headings, so we derive candidate headings deterministically from
    the font hierarchy and hand them over as a heading map to confirm. Re-tagging re-authors
    the file, so this is never auto-applied. Self-gating via the detector: fires only where
    office_structure.pdf_headings_labels_check flagged (tagged, past the page floor, no
    headings), and yields nothing when no visually-distinct headings exist (ADR 0016)."""
    if proposals is None:
        return
    try:
        # Reuse the detector as the gate so proposal and finding stay in lock-step: it fires
        # only for a tagged PDF, past the page floor, with no heading struct elements.
        from office_structure import pdf_headings_labels_check
        if not pdf_headings_labels_check(Path(source_path)):
            return
    except Exception:
        return
    headings = _extract_pdf_headings(source_path)
    if len(headings) < _MIN_OUTLINE_ENTRIES:
        return
    from proposals import heading_level_sequence, proposal
    levels = heading_level_sequence(size for _t, _p, size in headings)
    lines = [f"Heading {lvl} · “{title}” (p.{page + 1})"
             for (title, page, _size), lvl in zip(headings, levels)]
    proposals.append(proposal(
        locator="document headings",
        before="(tagged PDF, but the structure tree contains no headings to navigate by)",
        proposed_value="\n".join(lines),
        rationale=("Derived deterministically from the document's font hierarchy — confirm it "
                   "matches the intended headings. The approved map is the tagging instruction "
                   "for re-authoring and the compliance evidence of what the headings should be."),
        source="font hierarchy in document order (deterministic) — human confirmation required",
        kind="headings-map",
        # As for the 1.3.1 structure map: re-tagging re-authors the file, so the confirmed map
        # is the deliverable and never bytes to write back (see proposals.py).
        explain_only=True))


def _sc_ok(in_scope, sc: str) -> bool:
    """Is this criterion inside the operator's scope? True when no scope is set.

    Mirror of remediate_office._sc_ok — deliberately a local copy rather than a shared import,
    because remediate_pdf is imported on its own in several places and a cross-module import for
    a three-line predicate would couple the PDF lane to the Office one for no benefit.
    """
    return in_scope is None or bool(in_scope(sc))


def remediate_pdf(path: Path, *, lang: str = "en", ai_enabled: bool = True,
                  scan_id: str | None = None, diffs=None, proposals=None,
                  applied_fixes=None, in_scope=None):
    """Apply deterministic PDF accessibility fixes to a copy of the file.

    ai_enabled — when True and a vision (llava-class) Ollama model is reachable, tagged
    figures missing /Alt get genuine vision-generated alt text; otherwise they defer to
    human review (AI-off degrades gracefully). scan_id threads the vision calls into that
    scan's Langfuse session.

    Returns (fixed_path, applied, skipped):
      fixed_path — Path to the remediated PDF, or None if nothing was applied.
      applied/skipped — human-readable change descriptions.
    """
    def _rec(rule_id, before, after, note="", *, page=None):
        # `page` only when the edited object's real page is known (see _single_page). The key is
        # absent otherwise: document-level edits (catalog /Lang, /Title, the outline) have no page.
        if diffs is not None:
            entry = {"rule_id": rule_id, "before": before, "after": after, "note": note}
            if _known_page(page):
                entry["page"] = page
            diffs.append(entry)
    from scanner import WP  # vendored worker-python root (engine + fixers)
    sys.path.insert(0, str(WP))
    import pikepdf
    from remediation.fixers.pdf.language_fixer import PdfLanguageFixer
    from remediation.fixers.pdf.display_title_fixer import PdfDisplayTitleFixer
    from remediation.base_fixer import FixBehavior, FixStatus

    # Read the ORIGINAL metadata up front with pypdf — reliably, from the source. pikepdf's
    # docinfo reads AND writes are unstable once libxml2 is loaded in a long-lived worker,
    # so we never trust it for metadata (title, provenance).
    from pypdf import PdfReader
    orig_meta: dict[str, str] = {}
    try:
        om = PdfReader(str(path)).metadata
        if om:
            orig_meta = {k: str(v) for k, v in om.items() if v is not None}
    except Exception:
        orig_meta = {}
    existing_title = str(orig_meta.get("/Title") or "").strip()

    try:
        pdf = pikepdf.open(str(path))
    except Exception as e:
        return None, [], [f"could not open PDF: {type(e).__name__}"]

    behavior = FixBehavior(allow_llm=False, allow_placeholders=True)
    applied: list[str] = []
    skipped: list[str] = []
    mid_path = path.with_name(f".mid-{path.name}")

    # ── Stage 1: pikepdf — Root-level structural fixes + figure alt text ──
    try:
        for fixer, meta, _diff in [t for t in (
                (PdfLanguageFixer(), {"language": lang},
                 ("3.1.1", "(no /Lang entry — screen readers guessed the language)",
                  lang, "catalog /Lang set")),
                (PdfDisplayTitleFixer(), {},
                 ("2.4.2", "ViewerPreferences did not request the title bar show the doc title",
                  "DisplayDocTitle = true", "viewer shows the document title, not the file name")))
                if _sc_ok(in_scope, t[2][0])]:
            # The deterministic fixers read only issue.issue_id, so a light stub
            # avoids constructing a full A11yIssue (and its enum imports).
            issue = SimpleNamespace(issue_id=uuid.uuid4())
            res = fixer.apply(pdf, issue, meta, behavior)
            if res.status == FixStatus.FIXED:
                applied.append(res.description)
                _rec(*_diff)
            else:
                skipped.append(res.description)
        # Genuine alt text for tagged figures (WCAG 1.1.1) — vision when AI is on and a
        # vision model is reachable; unfixed figures defer to review via the re-scan.
        if _sc_ok(in_scope, "1.1.1"):
            alt_applied, alt_deferred = _fix_pdf_figure_alt(
                pdf, str(path), ai_enabled=ai_enabled, scan_id=scan_id, file=path.name,
                applied_fixes=applied_fixes, proposals=proposals)
        else:
            alt_applied, alt_deferred = [], 0
        applied.extend(alt_applied)
        if alt_deferred:
            skipped.append(f"{alt_deferred} figure(s) need human alt text — each surfaced as a "
                           "review card with the page render · 1.1.1")
        # 1.3.2 reading order — a vision proposal for an untagged (scanned) PDF (never auto).
        try:
            _propose_reading_order(pdf, str(path), ai_enabled=ai_enabled,
                                   scan_id=scan_id, file=path.name, proposals=proposals)
        except Exception:
            swallowed("remediate_pdf.remediate_pdf: proposing the PDF reading order failed", scan_id)
        # 1.3.1 structure map — deterministic heading-hierarchy proposal for an untagged PDF
        # (kind='structure-map'; the handler routes it to the 1.3.1 review card).
        try:
            _propose_structure_map(pdf, str(path), proposals=proposals)
        except Exception:
            swallowed("remediate_pdf.remediate_pdf: proposing the PDF structure map failed", scan_id)
        # 2.4.6 heading map — for a TAGGED PDF that has no heading struct elements, propose the
        # headings deterministically from the font hierarchy (kind='headings-map').
        try:
            _propose_pdf_headings(pdf, str(path), proposals=proposals)
        except Exception:
            swallowed("remediate_pdf.remediate_pdf: proposing the PDF headings failed", scan_id)
        # 2.4.4 link purpose — no proposal: a raw-URL link label can only be changed by
        # rewriting the text-showing operators, so the finding is explain-only and routes to a
        # human unmodified. See the block above _propose_pdf_headings for why.
        # 4.1.2 form-field accessible names — deterministic /TU from a meaningful field name;
        # a generic auto-name defers to a per-field review card (write-back on approval).
        try:
            if _sc_ok(in_scope, "4.1.2"):
                fld_applied, fld_deferred = _fix_pdf_form_fields(
                    pdf, proposals=proposals, applied_fixes=applied_fixes)
            else:
                fld_applied, fld_deferred = [], 0
            applied.extend(fld_applied)
            if fld_deferred:
                skipped.append(f"{fld_deferred} form field(s) need an accessible name — each "
                               "surfaced as a review card · 4.1.2")
        except Exception:
            swallowed("remediate_pdf.remediate_pdf: fixing the PDF form fields failed", scan_id)
        # 1.4.3/1.4.6 — recolour failing text colours in the content streams (deterministic,
        # text-scoped only; shapes/backgrounds untouched), each against the background
        # resolved behind its own glyphs. Verified by the post-fix re-scan.
        try:
            # One recolour pass satisfies both contrast criteria, so it runs when EITHER is in
            # scope — excluding only the AAA band must not switch off the AA fix.
            recoloured_pages: list = []
            n_recoloured = (_fix_pdf_text_contrast(pdf, pages=recoloured_pages)
                            if (_sc_ok(in_scope, "1.4.3") or _sc_ok(in_scope, "1.4.6")) else 0)
            recoloured_page = _single_page(recoloured_pages)
            if n_recoloured:
                applied.append(f"Recoloured {n_recoloured} low-contrast text colour(s) to meet "
                               "the contrast floors · 1.4.3")
                # RECORD only the criteria actually in scope. The recolour itself is a single
                # pass targeting the 7:1 band, so it inevitably clears both AA and AAA together —
                # that coupling is in the fixer, not something scoping can undo. What scoping CAN
                # do is refuse to claim credit for a criterion the operator excluded, which is
                # what the certification report reads these records for.
                if _sc_ok(in_scope, "1.4.3"):
                    _rec("1.4.3", "text colour fails 4.5:1 against the background behind it",
                         "moved away from that background, hue preserved",
                         f"{n_recoloured} fill-colour run(s) rewritten in the content streams",
                         page=recoloured_page)
                if _sc_ok(in_scope, "1.4.6"):
                    _rec("1.4.6", "text colour below the enhanced (7:1) contrast band",
                         "cleared by the same recolouring pass",
                         "one recolour clears both the AA and AAA contrast checks",
                         page=recoloured_page)
        except Exception:
            skipped.append("contrast: could not rewrite content streams · 1.4.3")
        # 2.4.1 bypass blocks — deterministically build a bookmark outline from the document's
        # headings when a long PDF has none (no AI; only when confident).
        try:
            _outline_msg = (_generate_pdf_outline(pdf, str(path))
                            if _sc_ok(in_scope, "2.4.1") else None)
            if _outline_msg:
                applied.append(_outline_msg)
                _rec("2.4.1", "(no bookmark outline — no way to skip past repeated content)",
                     "bookmark outline", "screen-reader/keyboard users can jump between sections")
        except Exception:
            swallowed("remediate_pdf.remediate_pdf: generating the PDF outline failed", scan_id)
        # 2.4.3 focus order — set /Tabs = /S (structure order) on every page carrying form-field
        # widgets that doesn't already declare it, so the keyboard tab sequence follows the
        # tagged reading order rather than the PDF's default (raw annotation-array order, which
        # rarely matches visual layout). Existence check only — this does NOT verify the AcroForm
        # field order itself matches reading order; that needs /StructTreeRoot walking this
        # codebase doesn't do yet, a real, separate, larger gap.
        try:
            from office_structure import _pdf_page_has_widget
            n_tabs = 0
            tab_pages: list = []
            if (_sc_ok(in_scope, "2.4.3")
                    and "/AcroForm" in pdf.Root and "/Fields" in pdf.Root["/AcroForm"]):
                for page_number, page in enumerate(pdf.pages, start=1):
                    if not _pdf_page_has_widget(page, pikepdf):
                        continue
                    tabs = page.obj.get("/Tabs")
                    if tabs is None or str(tabs) != "/S":
                        page.obj["/Tabs"] = pikepdf.Name("/S")
                        n_tabs += 1
                        tab_pages.append(page_number)
            if n_tabs:
                applied.append(f"Set /Tabs to structure order on {n_tabs} page(s) with form "
                               "fields · 2.4.3")
                _rec("2.4.3", "(/Tabs missing or not /S — tab order may not follow reading order)",
                     "/Tabs = /S", f"{n_tabs} page(s) now tab in structure (reading) order",
                     page=_single_page(tab_pages))
        except Exception:
            skipped.append("focus order: could not set /Tabs · 2.4.3")
        # Exact existing tags can be repaired without rebuilding page content. The
        # untagged/scanned maps below remain explain-only re-authoring instructions.
        if proposals is not None:
            try:
                from pdf_structure_repairs import propose_tagged_repairs
                tagged = propose_tagged_repairs(pdf, _extract_pdf_headings(str(path)))
                proposals.extend(p for p in tagged if _sc_ok(in_scope,
                    "2.4.6" if p.get("kind") == "pdf-tag-heading" else "1.3.1"))
            except Exception:
                swallowed("remediate_pdf.remediate_pdf: proposing exact tag repairs failed", scan_id)
        pdf.save(str(mid_path))
    finally:
        pdf.close()

    # Deterministic document title (WCAG 2.4.2) — filename-derived when none is set.
    new_title = None
    if not existing_title and _sc_ok(in_scope, "2.4.2"):
        new_title = path.stem.replace("-", " ").replace("_", " ").strip() or "Document"
        applied.append(f"Set document title to '{new_title}' · 2.4.2")
        _rec("2.4.2", "(no document title in metadata)", new_title,
             "filename-derived /Title written to the docinfo dictionary")

    if not applied:
        _unlink(mid_path)
        return None, [], skipped

    # ── Stage 2: pypdf — reliable docinfo metadata (title + provenance stamp) ──
    from datetime import datetime, timezone

    from pypdf import PdfWriter

    from remediate_office import TOOL, VERSION
    # Preserve the document's ORIGINAL metadata (clone_from copies pages/catalog but not
    # docinfo — and pikepdf's mid save can drop it), then layer the provenance stamp on top.
    now = datetime.now(timezone.utc)
    metadata = {
        **orig_meta,
        "/Producer": f"{TOOL} {VERSION}",
        "/RemediatedBy": TOOL,
        "/RemediationDate": now.strftime("%Y-%m-%d"),
        "/ModDate": "D:" + now.strftime("%Y%m%d%H%M%S") + "Z",
        "/WCAGTarget": "WCAG 2.1 AA",
        "/FixesApplied": "; ".join(applied)[:255],
    }
    # Carry the title through: the new filename-derived one when missing, else the existing
    # one (which clone_from would otherwise drop). A real title is never overwritten.
    title_to_set = new_title or existing_title
    if title_to_set:
        metadata["/Title"] = title_to_set

    out_path = path.with_name(f"remediated-{path.name}")
    try:
        writer = PdfWriter(clone_from=str(mid_path))   # preserves /Lang + ViewerPreferences
        writer.add_metadata(metadata)
        with open(out_path, "wb") as f:
            writer.write(f)
    except Exception as e:
        _unlink(mid_path)
        return None, [], skipped + [f"could not write PDF metadata: {type(e).__name__}"]
    from output_provenance import stamp_output
    out_path.write_bytes(stamp_output(out_path.read_bytes(), path.name))
    _unlink(mid_path)
    return out_path, applied, skipped


def _figure_locators(figures, pdf) -> dict:
    """Stable locator per /Figure: `pdf:fig:{page}:{seq}` where seq is the figure's 0-based
    position among ALL figures on its page (collection order). Computed over EVERY figure (not
    just the ones missing alt) so the same figure resolves to the same locator at apply time,
    after some siblings have already been captioned. `id(fig)` keys the map for this pass only."""
    loc, per_page = {}, {}
    for fig in figures:
        pg = _resolve_page_number(fig, pdf)
        seq = per_page.get(pg, 0); per_page[pg] = seq + 1
        loc[id(fig)] = f"pdf:fig:{pg if pg is not None else '?'}:{seq}"
    return loc


# ── 4.1.2 Name, Role, Value — AcroForm field accessible names (/TU) ─────────────
import re as _re
from swallowed import swallowed

# Generic auto-generated field names carry no meaning — a screen reader announcing "Text1"
# helps no one. When /T looks like this, we defer to a human rather than write a bad name.
_GENERIC_FIELD = _re.compile(
    r"^(text|textfield|txt|field|fld|input|form|checkbox|check|chk|radio|button|btn|"
    r"combo|list|choice|sig|signature|untitled|blank)[\s_\-.]*\d*$", _re.I)


def _humanize_field_name(t: str) -> str:
    """`/T` → a readable label: split camelCase + separators, collapse, title-case if it was
    a machine token. 'first_name'→'First Name', 'dateOfBirth'→'Date Of Birth', 'Home Address'
    (already readable) → unchanged."""
    s = (t or "").strip()
    if not s:
        return ""
    had_space = " " in s
    s = _re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", s)          # camelCase → words
    s = _re.sub(r"[_\-.]+", " ", s)                          # separators → space
    s = _re.sub(r"\s+", " ", s).strip()
    if not had_space and s and s[0].islower():
        s = s.title()                                        # a bare token: title-case it
    return s


def _field_name_meaningful(t: str) -> bool:
    """A /T we can safely reuse as the accessible name: readable words, not an auto-name."""
    h = _humanize_field_name(t)
    if len(h) < 3 or not _re.search(r"[A-Za-z]", h):
        return False
    if _GENERIC_FIELD.match((t or "").strip()):
        return False
    return bool(_re.search(r"[aeiouAEIOU]", h))              # has a vowel → word-like, not a code


def _field_page(field, pdf) -> int | None:
    """1-based page for a field via its own /P or its first widget kid's /P."""
    import pikepdf
    for obj in (field, *(field.get("/Kids") or [])):
        try:
            pg = obj.get("/P") if isinstance(obj, pikepdf.Dictionary) else None
            if pg is not None:
                for i, page in enumerate(pdf.pages, start=1):
                    if page.obj.objgen == pg.objgen:
                        return i
        except Exception:
            continue
    return None


def _collect_form_fields(pdf) -> list:
    """Terminal AcroForm fields (those with /FT), following /Kids — mirrors the detector."""
    import pikepdf
    out, seen = [], set()

    def walk(node):
        if not isinstance(node, pikepdf.Dictionary) or id(node) in seen:
            return
        seen.add(id(node))
        kids = node.get("/Kids")
        if "/FT" in node and not (isinstance(kids, pikepdf.Array) and any(
                "/FT" in k for k in kids if isinstance(k, pikepdf.Dictionary))):
            out.append(node)
        if isinstance(kids, pikepdf.Array):
            for k in kids:
                walk(k)

    try:
        root = pdf.Root
        if "/AcroForm" in root and "/Fields" in root["/AcroForm"]:
            for f in root["/AcroForm"]["/Fields"]:
                walk(f)
    except Exception:
        swallowed("remediate_pdf._collect_form_fields: walking the form-field tree failed")
    return out


def _form_field_locators(fields, pdf) -> dict:
    """Stable `pdf:field:{page}:{seq}` per terminal field (over ALL fields, so it resolves the
    same at apply time after siblings were auto-named)."""
    loc, per_page = {}, {}
    for fld in fields:
        pg = _field_page(fld, pdf)
        seq = per_page.get(pg, 0); per_page[pg] = seq + 1
        loc[id(fld)] = f"pdf:field:{pg if pg is not None else '?'}:{seq}"
    return loc


def _field_unnamed(field) -> bool:
    try:
        tu = field.get("/TU")
        return tu is None or not str(tu).strip()
    except Exception:
        return False


def _fix_pdf_form_fields(pdf, *, proposals=None, applied_fixes=None) -> tuple[list[str], int]:
    """WCAG 4.1.2 — give every unnamed AcroForm field an accessible name (/TU). Deterministic
    when the field's own name (/T) reads like a label ('First Name', 'dateOfBirth'): set /TU to
    the humanized name — no model, ADR-0016 safe. A field whose /T is a generic auto-name
    ('Text1', 'fld_03') can't be named honestly by machine, so it defers to a per-field review
    card ('pdf:field:{page}:{seq}' locator) for a human to author, then write back on approval.
    Returns (applied messages, deferred_count). Mutates pdf in place. Never raises."""
    import pikepdf
    fields = _collect_form_fields(pdf)
    unnamed = [f for f in fields if _field_unnamed(f)]
    if not unnamed:
        return [], 0
    locators = _form_field_locators(fields, pdf)
    applied: list[str] = []
    deferred = 0
    for fld in unnamed:
        raw_t = ""
        try:
            raw_t = str(fld.get("/T", "")).strip()
        except Exception:
            raw_t = ""
        if _field_name_meaningful(raw_t):
            name = _humanize_field_name(raw_t)
            try:
                fld["/TU"] = pikepdf.String(name)
            except Exception:
                deferred += 1
                continue
            if applied_fixes is not None:
                applied_fixes.append({"rule_id": "SC_4_1_2", "value": name,
                                      "source": f"derived deterministically from the field name “{raw_t}”"})
            applied.append(f"Accessible name “{name}” set on form field (from its field name) · 4.1.2")
        else:
            deferred += 1
            if proposals is not None:
                import proposals as _prop
                proposals.append(_prop.proposal(
                    locator=locators.get(id(fld), "pdf:field:?:0"),
                    before=(f"(form field “{raw_t}” has no accessible name — a screen reader can't say what to enter · 4.1.2)"
                            if raw_t else "(an interactive form field has no accessible name · 4.1.2)"),
                    proposed_value="",
                    rationale=("This field's internal name is not a usable label. Write the label a "
                               "screen reader should announce (e.g. “Home address”), then approve — "
                               "ACP writes it into the field."),
                    source="field name is a generic auto-name — human authors",
                    kind="pdf-field-name"))
    return applied, deferred


def apply_pdf_field_name(data: bytes, values: dict) -> tuple[bytes, list[dict], list[str]]:
    """Write reviewer-approved accessible names into AcroForm fields (/TU) by the
    `pdf:field:{page}:{seq}` locator. Same contract as apply_pdf_figure_alt (applied rows carry
    `{locator, before, after}` for the diff record, plus `rule_id`/`value`); keys that aren't
    field locators pass through unresolved. Never raises."""
    import io
    import pikepdf
    fvals = {k: (v or "").strip() for k, v in (values or {}).items()
             if str(k).startswith("pdf:field:") and (v or "").strip()}
    other = [k for k in (values or {}) if not str(k).startswith("pdf:field:")]
    if not fvals:
        return data, [], other
    try:
        pdf = pikepdf.open(io.BytesIO(data))
    except Exception:
        return data, [], other + list(fvals)
    fields = _collect_form_fields(pdf)
    by_loc = {_form_field_locators(fields, pdf)[id(f)]: f for f in fields}
    applied, unresolved = [], list(other)
    for locator, value in fvals.items():
        fld = by_loc.get(locator)
        if fld is None:
            unresolved.append(locator); continue
        try:
            prev = str(fld.get("/TU", "") or "").strip()
            fld["/TU"] = pikepdf.String(value)
            row = {"locator": locator, "rule_id": "SC_4_1_2", "value": value,
                   "before": prev or "(form field had no accessible name)",
                   "after": value}
            page = _known_page(_field_page(fld, pdf))
            if page:
                row["page"] = page              # the field's own /P, not the locator text
            applied.append(row)
        except Exception:
            unresolved.append(locator)
    if not applied:
        return data, [], unresolved
    out = io.BytesIO(); pdf.save(out)
    return out.getvalue(), applied, unresolved


def apply_pdf_approved(data: bytes, values: dict) -> tuple[bytes, list[dict], list[str]]:
    """Single PDF write-back entry for the apply job: routes figure-alt (`pdf:fig:…` → /Alt),
    form-field-name (`pdf:field:…` → /TU), exact structural language (`pdf:lang:…` → /Lang), and source-anchored tag plans
    (`pdf:struct:…`) approvals by locator prefix, in sequence. The
    unresolved list only carries locators neither writer recognised."""
    fig_vals = {k: v for k, v in (values or {}).items() if str(k).startswith("pdf:fig:")}
    fld_vals = {k: v for k, v in (values or {}).items() if str(k).startswith("pdf:field:")}
    lang_vals = {k: v for k, v in (values or {}).items() if str(k).startswith("pdf:lang:")}
    struct_vals = {k: v for k, v in (values or {}).items() if str(k).startswith("pdf:struct:")}
    unknown = [k for k in (values or {})
               if not (str(k).startswith("pdf:fig:") or str(k).startswith("pdf:field:") or str(k).startswith("pdf:lang:") or str(k).startswith("pdf:struct:"))]
    applied: list[dict] = []
    cur = data
    if fig_vals:
        cur, a, _ = apply_pdf_figure_alt(cur, fig_vals); applied += a
    if fld_vals:
        cur, a, _ = apply_pdf_field_name(cur, fld_vals); applied += a
    if lang_vals:
        from pdf_structural_language import apply_pdf_structure_language
        cur, a, _ = apply_pdf_structure_language(cur, lang_vals); applied += a
    if struct_vals:
        from pdf_structure_repairs import apply_pdf_structure_repairs
        cur, a, _ = apply_pdf_structure_repairs(cur, struct_vals); applied += a
    unresolved = [k for k in list(fig_vals) + list(fld_vals) + list(lang_vals) + list(struct_vals)
                  if not any(x.get("locator") == k for x in applied)] + unknown
    return cur, applied, unresolved


def apply_pdf_figure_alt(data: bytes, values: dict) -> tuple[bytes, list[dict], list[str]]:
    """Write reviewer-approved alt text into PDF /Figure elements by the `pdf:fig:{page}:{seq}`
    locator minted at remediation time. Mirrors office `apply_alt.apply_alt_text` exactly —
    `(bytes, {locator: value}) -> (fixed_bytes, applied, unresolved)`, each applied row carrying
    `{locator, before, after}` so it can go straight to `store.record_remediation_diffs` (plus
    `rule_id`/`value`) — so the apply job branches only on file extension. Keys that aren't
    PDF-figure locators are left unresolved (a mixed or mis-routed call never silently drops
    them). Re-collects figures and recomputes locators, so an approval written now lands on the
    same figure the proposal pointed at, even after siblings were auto-captioned at remediation.
    Never raises; on any failure the input bytes come back unchanged."""
    import io
    import pikepdf
    pdf_values = {k: (v or "").strip() for k, v in (values or {}).items()
                  if str(k).startswith("pdf:fig:") and (v or "").strip()}
    other = [k for k in (values or {}) if not str(k).startswith("pdf:fig:")]
    if not pdf_values:
        return data, [], other
    try:
        pdf = pikepdf.open(io.BytesIO(data))
    except Exception:
        return data, [], other + list(pdf_values)
    try:
        root = pdf.Root
        figures = _collect_figures(root["/StructTreeRoot"]) if "/StructTreeRoot" in root else []
    except Exception:
        figures = []
    loc = _figure_locators(figures, pdf)
    by_loc = {loc[id(f)]: f for f in figures}
    applied, unresolved = [], list(other)
    for locator, value in pdf_values.items():
        fig = by_loc.get(locator)
        if fig is None:
            unresolved.append(locator)
            continue
        try:
            prev = _fig_alt(fig)
            fig["/Alt"] = pikepdf.String(value)
            row = {"locator": locator, "rule_id": "SC_1_1_1", "value": value,
                   "before": prev or "(figure had no alt text)", "after": value}
            page = _known_page(_resolve_page_number(fig, pdf))
            if page:
                row["page"] = page              # the figure's own /Pg, not the locator text
            applied.append(row)
        except Exception:
            unresolved.append(locator)
    if not applied:
        return data, [], unresolved
    out = io.BytesIO()
    pdf.save(out)
    return out.getvalue(), applied, unresolved


def _pdf_figure_drafts(pdf, *, ai_enabled, scan_id, file, skip_locators=(), guidance=""):
    """Pure proposals from exact pixels, with independent semantic approval attached."""
    import ai
    import proposals as proposal_api
    from pdf_figure_evidence import exact_figure_image, FigureEvidenceError, bounded_figures
    from caption_validation import validate_caption
    if "/StructTreeRoot" not in pdf.Root:
        return []
    try:
        figures = bounded_figures(pdf)
    except FigureEvidenceError:
        return []
    locators = _figure_locators(figures, pdf)
    drafts, budget = [], _VISION_MAX_FIGURES
    for figure in figures:
        if locators[id(figure)] in skip_locators or _fig_alt(figure) is not None:
            continue
        image, association, response = None, None, None
        validation = {"approved": False, "status": "needs_manual", "reason_codes": ["figure_image_association_unavailable"]}
        try:
            association = exact_figure_image(pdf, figure)
            image = association["image_bytes"]
            if ai_enabled and budget > 0:
                budget -= 1  # attempts, including provider misses, consume capacity
                response = ai.describe_image_structured(image, filename=file, scan_id=scan_id,
                    file=file, allow_transcription=True, guidance=guidance)
                if response:
                    validation = validate_caption(response["alt"], image)
                    current = exact_figure_image(pdf, figure)
                    if current["image_sha256"] != association["image_sha256"]:
                        validation = {"approved": False, "status": "needs_manual", "reason_codes": ["figure_image_changed"]}
            elif ai_enabled:
                validation["reason_codes"] = ["figure_vision_attempt_limit"]
        except FigureEvidenceError as error:
            validation["reason_codes"] = [str(error)]
        except Exception:
            validation = {"approved": False, "status": "needs_manual", "reason_codes": ["figure_caption_unavailable"]}
        from quality_source_review import supported_review
        source_review = (response or {}).get('quality_source_review') or {}
        assisted = (association is not None and response is not None
                    and source_review.get('status') == 'ai_reviewed'
                    and supported_review(response['alt'], image, source_review.get('cloud_review') or {}))
        approved = (response is not None and not response.get('automatic_write_blocked')
                    and ((validation.get('approved') is True and validation.get('status') == 'validated') or assisted))
        value = ((validation.get("canonical_caption") or response["alt"]) if approved
                 else response.get("alt", "") if response else "")
        proposal = proposal_api.proposal(locator=locators[id(figure)],
            before="(figure has no alt text — invisible to screen readers · 1.1.1)",
            proposed_value=value,
            rationale=("Exact figure pixels independently match this caption." if approved
                       else "A reliable automatic caption is unavailable. Review the exact image if shown and write the description individually."),
            source=("Verified against the figure image" if approved
                    else "Exact figure image or caption semantics unavailable — manual description required"),
            kind="pdf-figure-alt", thumb=proposal_api.thumb_b64(image, max_edge=_PAGE_THUMB_EDGE) if image else None,
            model=response.get("model") if response else None,
            model_call_id=response.get("ai_call_id") if response else None)
        if response and response.get("model"):
            proposal["model"] = response["model"]
        if response and response.get("review_status"):
            for key in ("chart_review", "review_status", "approval_required", "reason_code"):
                if key in response:
                    proposal[key] = response[key]
            proposal["why_review"] = response.get("evidence")
        proposal['quality_source_review'] = source_review
        if assisted:
            proposal['source'] = 'AI reviewed against exact source pixels; semantic correctness is not certified'
            proposal['rationale'] = 'Independent source observations support this caption; exact saved output and regressions are checked separately.'
        proposal["caption_validation"] = validation
        proposal["automatic_write_blocked"] = not approved
        proposal["requires_semantic_review"] = not approved
        if association:
            proposal["figure_image_sha256"] = association["image_sha256"]
            proposal["figure_association_method"] = association["method"]
        drafts.append((figure, proposal))
    return drafts


def alt_proposals_for_pdf(data: bytes, *, scan_id=None, context_file="", skip_locators=(), guidance="") -> list[dict]:
    """Proposal-only exact-image recovery; never modifies tags or page/source bytes."""
    import io
    import pikepdf
    import ai
    with pikepdf.open(io.BytesIO(data)) as pdf, ai.assessment_vision_budget(60):
        drafts = [proposal for _figure, proposal in _pdf_figure_drafts(
            pdf, ai_enabled=True, scan_id=scan_id, file=context_file, skip_locators=skip_locators, guidance=guidance)]
        import hashlib
        for proposal in drafts:
            proposal["source_sha256"] = hashlib.sha256(data).hexdigest()
        return drafts



def validate_exact_figure_proposals(data: bytes, proposals: list[dict]) -> bool:
    """Recompute caption facts from the actual corrected artifact, without AI calls.

    Proposal flags are insufficient: each canonical value must still match the
    associated pixels and their retained independent evidence at application.
    """
    import io
    import pikepdf
    from pdf_figure_evidence import exact_figure_image, bounded_figures
    from caption_validation import validate_caption
    try:
        with pikepdf.open(io.BytesIO(data)) as pdf:
            figures = bounded_figures(pdf)
            locators = _figure_locators(figures, pdf)
            by_locator = {locators[id(figure)]: figure for figure in figures}
            for proposal in proposals:
                figure = by_locator.get(proposal.get("locator"))
                if figure is None or proposal.get("automatic_write_blocked"):
                    return False
                association = exact_figure_image(pdf, figure)
                retained = proposal.get("caption_validation") or {}
                validation = validate_caption(proposal.get("proposed_value", ""), association["image_bytes"])
                if (not validation.get("approved") or validation.get("status") != "validated"
                        or retained.get("approved") is not True or retained.get("status") != "validated"
                        or validation.get("canonical_caption") != proposal.get("proposed_value")
                        or retained.get("canonical_caption") != proposal.get("proposed_value")
                        or retained.get("evidence") != validation.get("evidence")
                        or proposal.get("figure_image_sha256") != association["image_sha256"]
                        or proposal.get("figure_association_method") != association["method"]):
                    return False
        return True
    except Exception:
        return False


def bind_exact_pdf_findings(store, owner, sid, run_id, filename, data, proposals, *, applied=False):
    """Bind proved raster captions to the frozen real assessment, without provider calls.

    Ambiguous legacy aggregate groups cannot authorize individual Figure edits.
    Retained binding is checked again outside managed generation at application.
    """
    import hashlib, io, json
    import pikepdf
    from pdf_figure_evidence import bounded_figures
    if not proposals or not filename.lower().endswith('.pdf') or any(p.get('kind') != 'pdf-figure-alt' for p in proposals):
        raise ValueError('Exact PDF finding binding requires only figure captions')
    artifact = (store.get_file_record(sid, filename) or {}).get('corrected_sha256')
    from quality_source_review import reviewed_pdf_allowed
    quality_pdf = reviewed_pdf_allowed(store, owner, sid, run_id, filename, data, proposals, applied=applied)
    if not artifact or hashlib.sha256(data).hexdigest() != artifact or not (validate_exact_figure_proposals(data, proposals) or quality_pdf):
        raise ValueError('Exact PDF caption evidence is stale or unproved')
    with store._db.cursor() as cur:
        store._db.execute(cur, 'SELECT snapshot_id,baseline_json FROM remediation_contribution_runs WHERE owner_id=%s AND scan_id=%s AND run_id=%s', (owner, sid, run_id))
        baseline = store._db.fetchone(cur)
    if not baseline or baseline['snapshot_id'] != store.remediation_source_revision(sid):
        raise ValueError('Exact PDF assessment identities are stale or unavailable')
    rows = [r for r in json.loads(baseline['baseline_json']) if r['file'] == filename and r['rule_id'] == '1.1.1']
    with pikepdf.open(io.BytesIO(data)) as pdf:
        figures = bounded_figures(pdf)
        locators = _figure_locators(figures, pdf)
        missing = [locators[id(f)] for f in figures if not str(f.get('/Alt', '')).strip()]
    dispositions = {r['finding_id']: r.get('disposition') for r in store.list_finding_dispositions(sid, run_id)}
    result, seen = [], set()
    for proposal in proposals:
        loc = proposal.get('locator')
        if not applied and (loc not in missing or proposal.get('source_sha256') != artifact):
            raise ValueError('Exact PDF target is no longer an outstanding current figure')
        matches = [r for r in rows if str(r.get('instance_key', '')).casefold() == str(loc).casefold()]
        if not matches and len(rows) == 1 and len(figures) == 1 and (applied or len(missing) == 1) and str(rows[0].get('instance_key', '')).startswith('aggregate-instance:'):
            matches = rows
        if len(matches) != 1 or matches[0]['finding_id'] in seen:
            raise ValueError('Exact PDF figure has no unambiguous assessed identity')
        fid = matches[0]['finding_id']; seen.add(fid)
        if not applied and dispositions.get(fid) in {'resolved_verified', 'excluded_by_policy', 'superseded_by_reassessment'}:
            raise ValueError('Exact PDF finding is no longer outstanding')
        result.append({**proposal, 'source_sha256': artifact, 'finding_ids': [fid], 'baseline_finding_ids': [fid], 'assessment_revision': baseline['snapshot_id']})
    return result


def validate_exact_pdf_finding_bindings(store, owner, sid, run_id, filename, data, proposals, *, applied=False):
    try:
        bound = bind_exact_pdf_findings(store, owner, sid, run_id, filename, data, proposals, applied=applied)
        return all(all(p.get(k) == b[k] for k in ('finding_ids', 'baseline_finding_ids', 'assessment_revision')) for p, b in zip(proposals, bound))
    except Exception:
        return False


def _fix_pdf_figure_alt(pdf, source_path: str, *, ai_enabled: bool,
                        scan_id: str | None, file: str,
                        applied_fixes=None, proposals=None) -> tuple[list[str], int]:
    """Write only exact-raster captions independently validated against pixel facts.

    OCR presence and model agreement alone authorize nothing. Unmapped or complex
    figures remain manual; the retained page-caption implementation is not used.
    """
    import pikepdf
    import ai
    applied, deferred = [], 0
    with ai.assessment_vision_budget(60):
        drafts = _pdf_figure_drafts(pdf, ai_enabled=ai_enabled, scan_id=scan_id, file=file)
    for figure, proposal in drafts:
        if proposal["automatic_write_blocked"] or proposal.get("quality_source_review"):
            deferred += 1
            if proposals is not None:
                proposals.append(proposal)
            continue
        try:
            figure["/Alt"] = pikepdf.String(proposal["proposed_value"])
        except Exception:
            deferred += 1
            proposal["automatic_write_blocked"] = True
            proposal["requires_semantic_review"] = True
            if proposals is not None:
                proposals.append(proposal)
            continue
        validation = proposal["caption_validation"]
        if applied_fixes is not None:
            applied_fixes.append({"rule_id": "SC_1_1_1", "locator": proposal["locator"],
                "value": proposal["proposed_value"], "source": proposal["source"],
                "thumb": proposal.get("thumb"), "model": proposal.get("model"),
                "model_call_id": proposal.get("model_call_id"),
                "caption_validation": validation, "figure_image_sha256": proposal["figure_image_sha256"]})
        applied.append(f'Alt text "{proposal["proposed_value"][:60]}" set from independently validated exact figure pixels · 1.1.1')
    return applied, deferred


def _fix_pdf_figure_alt_from_page_legacy(pdf, source_path: str, *, ai_enabled: bool,
                        scan_id: str | None, file: str,
                        applied_fixes=None, proposals=None) -> tuple[list[str], int]:
    """Retired page-caption implementation; no live caller uses this path.

    Set /Alt on tagged /Figure struct elements that lack it, from a vision description
    of the figure's page. Returns (applied messages, deferred_count). Mutates pdf in place;
    the caller saves. Never raises — a figure we can't caption is just deferred.

    A GROUNDED vision description (the page render is text-anchored) is auto-applied inline. A
    figure we cannot caption (AI off / no vision result / budget spent) OR can only GUESS at
    (an ungrounded description — see _alt_write_anchor) is **deferred to a per-figure review
    card**, not silently counted: it emits one proposal into `proposals`
    carrying its stable locator (for write-back), the page render as its thumbnail, and any
    best-effort AI draft — mirroring the office undescribed-image flow, so a PDF figure the
    machine can't ground reaches a human with a picture instead of vanishing into a tally.

    `applied_fixes` receives one evidence row per figure captioned, in the SAME shape the
    office remediator emits — {rule_id, value, source, thumb}."""
    import pikepdf
    root = pdf.Root
    if "/StructTreeRoot" not in root:
        return [], 0            # untagged → separate tagging finding, not alt text
    try:
        figures = _collect_figures(root["/StructTreeRoot"])
    except Exception:
        return [], 0
    missing = [f for f in figures if _fig_alt(f) is None]
    if not missing:
        return [], 0
    locators = _figure_locators(figures, pdf)

    vision = False
    if ai_enabled:
        try:
            import ai as _ai
            vision = _ai.vision_is_available()
        except Exception:
            vision = False

    import proposals as _prop
    applied: list[str] = []
    deferred = 0
    budget = _VISION_MAX_FIGURES
    thumb_budget = _VISION_MAX_FIGURES        # bound page renders for deferred cards too
    page_cache: dict[int, bytes | None] = {}
    # The vision input is a render of the WHOLE PAGE, not an isolated figure. Multiple
    # /Figure elements on one page therefore used to send byte-identical PNGs through OCR,
    # local/cloud vision and (when enabled) the independent validation call once per figure.
    # Reuse that page-level result: it preserves the existing semantics exactly (each figure
    # still receives the same page description / review draft it did before), while avoiding
    # duplicate parsing and model waits. Cache misses too, so an unavailable model is not
    # retried N times for N figures on the same page.
    vision_cache: dict[int, tuple[dict | None, str | None]] = {}

    def _render(page_num):
        if page_num is None:
            return None
        if page_num not in page_cache:
            page_cache[page_num] = _render_page_png(source_path, page_num)
        return page_cache[page_num]

    def _defer(fig, page_num, img, draft="", model_call_id=None):
        """Emit a per-figure review card for a figure we could not auto-caption."""
        nonlocal deferred
        deferred += 1
        if proposals is None:
            return
        proposals.append(_prop.proposal(
            locator=locators.get(id(fig), "pdf:fig:?:0"),
            before="(figure has no alt text — invisible to screen readers · 1.1.1)",
            proposed_value=draft,
            rationale=("A screen reader cannot describe this figure. Write the alt text a "
                       "reader should hear. The page thumbnail is context, not isolated figure evidence. "
                       "Then approve — ACP writes "
                       "it into the PDF."),
            source=("Exact figure image unavailable — manual description required"
                    if not draft else "AI vision draft — confirm it matches the figure"),
            kind="pdf-figure-alt",
            thumb=(_prop.thumb_b64(img, max_edge=_PAGE_THUMB_EDGE) if img else None),
            model_call_id=model_call_id))

    for fig in missing:
        page_num = _resolve_page_number(fig, pdf)
        img = _render(page_num) if thumb_budget > 0 else None
        if img is not None:
            thumb_budget -= 1
        if not vision or budget <= 0 or img is None:
            _defer(fig, page_num, img)
            continue
        import ai as _ai
        cached = vision_cache.get(page_num)
        if cached is None:
            res = _ai.describe_image_structured(img, filename=file, scan_id=scan_id, file=file)
            # The spend is the call, including a miss. Count it before branching so a model
            # returning no usable result cannot bypass the document's configured call cap.
            budget -= 1
            anchor = (_alt_write_anchor(res, img, scan_id=scan_id, file=file)
                      if res is not None else None)
            vision_cache[page_num] = (res, anchor)
        else:
            res, anchor = cached
        if not res:
            _defer(fig, page_num, img)
            continue
        if anchor is None:
            # Ungrounded guess → the reviewer decides, we do not assert it (see helper).
            _defer(fig, page_num, img, draft=res.get("alt", ""),
                   model_call_id=res.get("ai_call_id"))
            continue
        try:
            fig["/Alt"] = pikepdf.String(res["alt"])
        except Exception:
            _defer(fig, page_num, img, draft=res.get("alt", ""),
                   model_call_id=res.get("ai_call_id"))
            continue
        if applied_fixes is not None:
            applied_fixes.append({
                "rule_id": "SC_1_1_1",
                "value": res["alt"],
                "source": (f"AI vision model ({res['model']}), {anchor}, "
                           f"from a render of page {page_num}"),
                "thumb": _prop.thumb_b64(img, max_edge=_FIX_THUMB_EDGE),
            })
        applied.append(f"Alt text \"{res['alt'][:60]}\" set on figure (page {page_num}) "
                       f"from an AI vision description {anchor} · 1.1.1")
    return applied, deferred


def _alt_write_anchor(res: dict, img: bytes, *, scan_id: str | None, file: str) -> str | None:
    """May this vision description be written into the PDF unattended? Returns the provenance
    phrase that says WHY it may, or None when it must go to a human instead.

    The same honesty split the office remediator applies (remediate_office._vision_alt): a
    GROUNDED description is anchored in text OCR'd from the render and is auto-applied; an
    UNGROUNDED one is a pure vision guess, and a guess written into /Alt becomes the sentence a
    screen-reader user hears as fact — worse than an unlabelled figure, because the file then
    looks remediated. Ungrounded goes to a review card instead (WCAG 1.1.1 intent stays human).

    One honest caveat specific to PDF: we caption a render of the whole PAGE, so `grounded`
    reports that the PAGE carried text, not that the FIGURE did. It is a weaker anchor here
    than the office path's per-image OCR — it reliably catches the textless page, and the
    reviewer remains the check on the rest.

    Ungrounded may still be applied when the auto-apply-validated policy is ON (opt-in, default
    OFF) and an INDEPENDENT second reading calls the draft consistent — a measurement, never
    the model grading itself (ADR 0016). Any other verdict falls through to the review card."""
    if res.get("grounded"):
        return "anchored in text read from the page render"
    try:
        import ai as _ai
        import core as _core
        if _core.store.get_auto_apply_validated():
            v = _ai.validate_alt_text(img, res["alt"], filename=file, scan_id=scan_id, file=file)
            if v and v.get("verdict") == "consistent":
                return ("confirmed by an independent second reading (consistency cross-check, "
                        "auto-apply-validated policy)")
    except Exception:
        # validator down / policy unreadable → the human decides
        swallowed("remediate_pdf._alt_write_anchor: validating and auto-applying the alt-text "
                  "anchor failed", scan_id)
    return None


def _collect_figures(node, figures: list | None = None) -> list:
    """Walk the structure tree collecting /Figure struct elements.

    Delegates to formats.pdf.structure so the 1.1.1 DETECTOR and this remediator judge the
    same set of figures. They must: the write-back lane credits an approved alt text by
    re-scanning and finding 1.1.1 gone, so a figure one side sees and the other misses either
    blocks certification forever or certifies a document with an undescribed image in it."""
    from formats.pdf import structure
    return structure.collect_figures(node, figures)


def _fig_alt(figure) -> str | None:
    """A figure's /Alt, or None when absent or whitespace-only. Shared with the detector."""
    from formats.pdf import structure
    return structure.figure_alt(figure)


def _resolve_page_number(figure, pdf) -> int | None:
    """1-based page number for a figure via its /Pg reference."""
    try:
        pg_ref = figure.get("/Pg")
        if pg_ref is None:
            return None
        for i, page in enumerate(pdf.pages):
            if page.obj.objgen == pg_ref.objgen:
                return i + 1
    except Exception:
        swallowed("remediate_pdf._resolve_page_number: resolving a figure's page number failed")
    return None


def _render_page_png(source_path: str, page_number: int) -> bytes | None:
    """Render a PDF page to PNG bytes via pypdfium2 (pure wheel; no poppler/PyMuPDF).
    The whole page is rendered — for a figure-bearing page that's dominated by the figure,
    which is what the vision model describes. Returns None on any failure."""
    try:
        import io
        import pypdfium2
        doc = pypdfium2.PdfDocument(source_path)
        try:
            i = page_number - 1
            if i < 0 or i >= len(doc):
                return None
            bitmap = doc[i].render(scale=_RENDER_SCALE)
            buf = io.BytesIO()
            bitmap.to_pil().save(buf, format="PNG")
            return buf.getvalue()
        finally:
            doc.close()
    except Exception:
        return None


def _unlink(p: Path) -> None:
    try:
        p.unlink()
    except Exception:
        swallowed("remediate_pdf._unlink: unlinking a temporary file failed")


# ── 2.4.1 Bypass Blocks — build a bookmark outline from the document's own headings ──
# The scan flags a long PDF (>= office_structure._MIN_PAGES_FOR_OUTLINE) with an empty
# outline. When the document's text carries visually-distinct headings we can rebuild the
# outline deterministically (no AI): a screen-reader/keyboard user then has the skip
# mechanism 2.4.1 requires. Auto-applied ONLY when confident — a heading-less or scanned
# PDF (no larger-than-body text) yields nothing and stays a human-authoring finding, so we
# never fabricate a junk "Page 1/2/3" outline that reads as done without being useful.
_MIN_OUTLINE_ENTRIES = 2        # fewer than this isn't a navigational outline worth adding
_HEADING_SIZE_RATIO = 1.2       # a line >= 1.2x the modal body size reads as a heading
_HEADING_MAX_CHARS = 100        # a heading is a short label, not a paragraph
_OUTLINE_SCAN_PAGE_CAP = 120    # bound worst-case parse cost on a huge PDF
_OUTLINE_ENTRY_CAP = 200        # a sane ceiling on bookmark count

# ── Large text that is NOT a section heading ───────────────────────────────────
# Which large lines are page furniture rather than sections is decided by
# office_structure.looks_like_heading_furniture — shared with the docx pseudo-heading
# detector and promoter so all three agree about what a heading is (the reasoning, and why
# it matters that they agree, is documented there). A running header or footer is the one
# kind of furniture that predicate cannot judge, because recognising it needs the whole
# document: the same line, in the same margin band, on page after page. Both halves of that
# are load-bearing — frequency alone would delete every section of a "Step 1 / Step 2 / …"
# document, and margin position alone would delete a legitimate heading that happens to sit
# at the very top of its page.
# 6% of a letter page is ~0.6in — inside a 1in margin, so a header/footer sits in the band
# while the first content line of a normally-margined page (top ~7.3%) stays out of it.
_MARGIN_BAND_FRACTION = 0.06
_RUNNING_MIN_PAGES = 3          # below this, "repeated" is not yet a pattern…
_RUNNING_PAGE_FRACTION = 0.5    # …and it must also cover half the scanned pages


def _running_key(text: str) -> str:
    """Normalised identity of a margin line for the repeated-header check. Digit runs are
    masked, so "Annual Report | 4" and "… | 5" are recognised as the same footer."""
    return re.sub(r"\d+", "#", " ".join(text.split()).lower())


def _extract_pdf_headings(source_path: str) -> list[tuple[str, int, int]]:
    """Heading candidates by font size. The modal rounded char size is body text; a line
    whose largest char is >= _HEADING_SIZE_RATIO x that, is short, and has letters reads as
    a heading — unless it is page furniture (office_structure.looks_like_heading_furniture,
    plus the running header/footer pass below), which size alone cannot tell from a section.
    Returns [(title, page_index, size)] in document order, deduped by text, capped — size
    lets a caller rank headings into levels (proposals.heading_level_sequence). Empty on
    any failure or when nothing stands out (uniform-font / scanned PDF)."""
    try:
        import pdfplumber
        import office_structure as _osx
        from collections import Counter
    except Exception:
        return []
    try:
        with pdfplumber.open(source_path) as doc:
            pages = doc.pages[:_OUTLINE_SCAN_PAGE_CAP]
            sizes = [round(ch.get("size", 0)) for pg in pages for ch in (pg.chars or [])]
            sizes = [s for s in sizes if s > 0]
            if not sizes:
                return []
            body = Counter(sizes).most_common(1)[0][0]
            if body <= 0:
                return []
            cands: list[tuple[str, int, int, bool]] = []   # + is it in a margin band?
            for i, pg in enumerate(pages):
                height = float(pg.height or 0)
                lines: dict[int, dict] = {}
                for ch in (pg.chars or []):
                    key = round(ch.get("top", 0))       # group chars sharing a baseline into a line
                    line = lines.setdefault(key, {"text": [], "size": 0})
                    line["text"].append(ch.get("text", ""))
                    line["size"] = max(line["size"], round(ch.get("size", 0)))
                for key in sorted(lines):
                    line = lines[key]
                    text = "".join(line["text"]).strip()
                    if (not text or len(text) > _HEADING_MAX_CHARS
                            or not any(c.isalpha() for c in text)):
                        continue
                    if line["size"] < body * _HEADING_SIZE_RATIO:
                        continue
                    if _osx.looks_like_heading_furniture(text):
                        continue
                    in_margin = bool(height) and (
                        key <= height * _MARGIN_BAND_FRACTION
                        or key >= height * (1 - _MARGIN_BAND_FRACTION))
                    cands.append((text[:120], i, line["size"], in_margin))
            # Running headers/footers. Dedupe-by-text hides the repetition (only the first
            # occurrence survives) but still leaves that one as a bookmark, so count the
            # pages each margin line appears on and drop the ones that recur.
            pages_by_key: dict[str, set[int]] = {}
            for text, i, _size, in_margin in cands:
                if in_margin:
                    pages_by_key.setdefault(_running_key(text), set()).add(i)
            floor = max(_RUNNING_MIN_PAGES, int(len(pages) * _RUNNING_PAGE_FRACTION))
            running = {k for k, on_pages in pages_by_key.items() if len(on_pages) >= floor}
            out: list[tuple[str, int, int]] = []
            seen: set[str] = set()
            for text, i, size, in_margin in cands:
                if in_margin and _running_key(text) in running:
                    continue
                norm = text.lower()
                if norm in seen:
                    continue
                seen.add(norm)
                out.append((text, i, size))
                if len(out) >= _OUTLINE_ENTRY_CAP:
                    break
            return out
    except Exception:
        return []


def _generate_pdf_outline(pdf, source_path: str) -> str | None:
    """Build a pikepdf outline in-place (WCAG 2.4.1) for a long PDF that has none, from its
    own headings. Returns a description when it applied, else None (short doc / existing
    outline / too few headings). Operates on the open stage-1 `pdf`, so the entries persist
    through the stage's save. Deterministic — no model."""
    try:
        import pikepdf
    except Exception:
        return None
    try:
        from office_structure import _MIN_PAGES_FOR_OUTLINE
    except Exception:
        _MIN_PAGES_FOR_OUTLINE = 5
    try:
        if len(pdf.pages) < _MIN_PAGES_FOR_OUTLINE:
            return None
        with pdf.open_outline() as outline:
            if outline.root:                       # already navigable — leave it untouched
                return None
            headings = _extract_pdf_headings(source_path)
            if len(headings) < _MIN_OUTLINE_ENTRIES:
                return None                        # nothing confident to build; stays HITL
            for title, page_idx, _size in headings:
                outline.root.append(pikepdf.OutlineItem(title, page_idx))
    except Exception:
        return None
    return (f"Built a {len(headings)}-entry bookmark outline from the document's headings, "
            "so assistive tech can skip between sections · 2.4.1")

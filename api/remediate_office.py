"""Server-side Office remediation (ADR 0005 step 4 — docx/pptx/xlsx).

The vendored DigitalA11y .NET engine flags Office accessibility issues but is
scan-only — there's no Office remediator to wrap. So this implements the
DETERMINISTIC Office fixes directly, in pure stdlib (no new dependency):

  * document language  → dc:language in docProps/core.xml  (the exact thing the
                         .NET DocumentLanguageRule reads: PackageProperties.Language)
  * document title     → dc:title    in docProps/core.xml  (DocumentTitleRule reads
                         PackageProperties.Title)
  * image alt text     → descr= on wp:docPr / p:cNvPr / xdr:cNvPr (what the
                         .NET AltTextRule reads). Preferred source order:
                         (1) a faithful in-document source — the author's own
                         Alt-Text *Title* field, an adjacent "Figure N:" caption
                         (docx), or a meaningful shape name; then, when AI is on
                         and a vision model is reachable, (2) a genuine
                         description of the image PIXELS from the local vision
                         (llava-class) model — the image bytes are resolved via
                         the part's .rels and sent to OLLAMA_VISION_MODEL. Images
                         with neither are left untouched and reported as deferred
                         — an unlabelled image never gets invented text; it routes
                         to human review.

OOXML files are zip archives of XML; the OPC core-properties part is identical
across docx/pptx/xlsx, so one code path covers all three. Anything needing
content judgement beyond the faithful-source rule above (reading order,
contrast, alt for context-free images) is NOT touched here and routes to human
review — same contract as the HTML/PDF remediators.
"""
from __future__ import annotations
import os
import posixpath
import re
import zipfile
from html import unescape
from datetime import datetime, timezone
from pathlib import Path
from xml.etree import ElementTree as ET

# part-glob → (tag, pic wrapper or None, captions?). The ONE target table, shared with the
# 1.1.1 detector (formats/office/images.py) so the detector and this remediator can never
# disagree about which elements carry alt text.
from formats.office.images import ALT_TARGETS as _ALT_TARGETS
from formats.office.images import is_decorative as _images_is_decorative
from activity import record as _act_record
from swallowed import swallowed

_CORE = "docProps/core.xml"
_CUSTOM = "docProps/custom.xml"
_FMTID = "{D5CDD505-2E9C-101B-9397-08002B2CF9AE}"  # standard OPC custom-properties GUID
TOOL = "Mova.io ACP"
# CalVer build version (v2026.M.D.N) — the same value /healthz and the UI report, so a
# remediated file's provenance stamp matches the deployed build. ACP_BUILD_VERSION is
# baked onto the image by deploy.sh; fall back to the legacy ACP_VERSION, then 'dev'.
VERSION = os.environ.get("ACP_BUILD_VERSION") or os.environ.get("ACP_VERSION") or "dev"


def _xesc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def _rec(diffs, rule_id: str, before: str, after: str, note: str = "", *,
         locator: str | None = None) -> None:
    """Append one before→after record for the certification report's "Before → After"
    section. No-op when the caller passed no collector (default None). rule_id is the
    dotted WCAG SC, matching how the residual re-scan keys criteria, so the worker can
    filter these to the fixes that verifiably cleared.

    `locator` is the location of the object this fixer actually edited, in a vocabulary ACP
    already uses for it (`word:p:N`, `part#name`, a slide/table part). Callers pass it only when
    they hold it; otherwise the key is ABSENT, which means "not recorded" — never a guess from
    the criterion. Office producers never pass a page: Word pages are a layout artefact, and a
    paragraph index is not one."""
    if diffs is not None:
        entry = {"rule_id": rule_id, "before": before, "after": after, "note": note}
        if isinstance(locator, str) and locator.strip():
            entry["locator"] = locator
        diffs.append(entry)


# Parts narrow enough that naming the part locates the edit: one slide, one defined table, one
# sheet's drawing layer. `word/document.xml` is deliberately absent — it is the whole body, so
# naming it would dress "somewhere in the document" up as a location.
_PART_LOCATOR = re.compile(r"ppt/slides/slide\d+\.xml|xl/tables/table\d+\.xml|xl/drawings/drawing\d+\.xml")


def _part_locator(part_name: str | None) -> str | None:
    return part_name if part_name and _PART_LOCATOR.fullmatch(part_name) else None


def _element_locator(xml: str, at: int, part_name: str | None) -> str | None:
    """`part#fragment` for the alt-bearing element at offset `at`, or None.

    Uses the fragment kinds the approved-alt writer resolves (apply_alt.resolve_target: the
    element's `name`, then its image's r:embed id) and keeps one ONLY if resolving it back
    against this same XML lands on this same element. A duplicated shape name or a shared image
    relationship resolves to a different element first, so it is refused rather than recorded
    as a location that points somewhere else."""
    if not part_name:
        return None
    try:
        from apply_alt import resolve_target, tag_for_part
    except Exception:
        return None
    part_tag = tag_for_part(part_name)
    if not part_tag:
        return None
    els = list(re.finditer(rf"<({part_tag})\b([^>]*?)(/?)>", xml))
    idx = next((i for i, el in enumerate(els) if el.start() == at), None)
    if idx is None:
        return None
    candidates = [_ATTR(els[idx].group(2), "name").strip()]
    stop = els[idx + 1].start() if idx + 1 < len(els) else len(xml)
    blip = re.search(r'\br:embed="([^"]+)"', xml[els[idx].end():stop])
    if blip:
        candidates.append(blip.group(1).strip())
    for fragment in candidates:
        if fragment and resolve_target(xml, part_tag, fragment) == at:
            return f"{part_name}#{fragment}"
    return None


def _sc_ok(in_scope, sc: str) -> bool:
    """Is this criterion inside the operator's scope? True when no scope is set.

    The office/pdf twin of remediate.remediate_html's `in_scope` gate (#137). That change gated
    the PROPOSAL lane and the HTML fixer; these deterministic fixers apply inline inside
    per-format functions with no per-rule boundary, so it left them ungated and said so in the
    audit trail (`remediate.scope_partial`). This closes that: every fix below is guarded by the
    SC it actually writes, so an excluded criterion means ACP leaves the bytes alone.

    `None` means no scope is configured, which must behave EXACTLY as before — an unscoped
    deployment pays nothing for this and cannot be broken by it.
    """
    return in_scope is None or bool(in_scope(sc))


def _stamp_provenance(entries: dict, applied: list[str]) -> None:
    """Write a remediation provenance stamp into docProps/custom.xml — shows in the
    'Custom' tab of the file's Properties: who/what fixed it, the standard, the date,
    and the fixes applied. Creates the part (+ content-type + relationship) if absent,
    or appends to an existing custom-properties part."""
    from output_provenance import stamp_office_entries
    stamp_office_entries(entries, applied, version=VERSION)

_CP = "http://schemas.openxmlformats.org/package/2006/metadata/core-properties"
_DC = "http://purl.org/dc/elements/1.1/"
_NS = {
    "cp": _CP, "dc": _DC,
    "dcterms": "http://purl.org/dc/terms/",
    "dcmitype": "http://purl.org/dc/dcmitype/",
    "xsi": "http://www.w3.org/2001/XMLSchema-instance",
}


# ── Image alt text (DOCX/PPTX/XLSX-ALT-001, WCAG 1.1.1) ───────────────────────
# The .NET AltTextRule reads descr= on wp:docPr (docx), p:cNvPr (pptx pictures)
# and xdr:cNvPr (xlsx drawings). Fixes are string-surgical — descr is injected
# into the exact original tag text so every other byte of the part is preserved
# (OOXML consumers are picky about re-serialization).
_GENERIC_NAME = re.compile(
    r"^\s*(picture|image|graphic|grafik|imagen|chart|diagramm|shape|object|content placeholder)\s*\d*\s*$",
    re.I)
_CAPTION_LEAD = re.compile(r"^\s*((figure|fig\.?|table|chart|diagram|abb\.?)\s*\d*[:.\s—-]\s*)", re.I)
# descr values that are effectively no alt at all: empty, a bare filename
# ("image.png"), or a generic auto-name — the .NET AltTextRule flags these too.
def _is_junk_descr(descr: str) -> bool:
    """True when descr is empty, a generic auto-name, or a file name/path rather than a real
    description — mirrors the .NET AltTextRule (WCAG 1.1.1).

    Delegates to formats.office.images so the 1.1.1 DETECTOR and this remediator apply the
    SAME predicate. They must: the write-back lane credits an approved alt text by re-scanning
    and finding 1.1.1 gone, so an image one side counts as described and the other does not
    either blocks certification forever or certifies a document with an undescribed image."""
    from formats.office import images as _images
    return _images.is_junk_descr(descr)


_ATTR = lambda attrs, name: (re.search(rf'\b{name}="([^"]*)"', attrs) or [None, ""])[1]


def _strip_tags(xml_chunk: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", xml_chunk)).strip()


def _derive_alt(attrs: str, caption: str | None) -> tuple[str, str] | None:
    """Faithful alt source, in priority order. Returns (alt, source) or None."""
    title = unescape(_ATTR(attrs, "title")).strip()
    if title:
        return title, "the image's own Alt-Text title"
    if caption:
        cap = unescape(_CAPTION_LEAD.sub("", caption)).strip()
        if len(cap) >= 4:
            # A faithful source must retain its content, including later instructions.
            return cap, "the adjacent caption"
    name = unescape(_ATTR(attrs, "name")).strip()
    if name and not _GENERIC_NAME.match(name):
        return name, "the shape's descriptive name"
    return None


# Genuine vision alt text is opt-in (AI on + a vision model reachable) and bounded:
# CPU vision inference is ~30-90 s/image, so cap how many images one document will
# caption. Default 10: 10 × 90 s ≈ 15 min worst-case vs 25 × 90 s ≈ 37 min.
# Raise ACP_VISION_MAX_IMAGES when a GPU vision backend is configured.
# Beyond the cap, remaining unlabelled images defer to review as before.
_VISION_MAX_IMAGES = int(os.environ.get("ACP_VISION_MAX_IMAGES", "10"))
# Skip degenerate images the model can't meaningfully describe (1x1 spacers etc.).
_MIN_IMG_BYTES = 64
# OOXML relationship IDs are opaque; alternative producers need not use numeric rId names.
_R_EMBED = re.compile(r'r:embed="([^"]+)"')


def _thumb_b64(img_bytes: bytes, *, max_edge: int = 96) -> str | None:
    """Thin alias — the implementation moved to proposals.thumb_b64 when the PDF
    reading-order proposal needed it too. Imported lazily, as everything else here
    imports `proposals`."""
    import proposals as _prop
    return _prop.thumb_b64(img_bytes, max_edge=max_edge)


def _image_bytes_for(xml: str, m, tag: str, pic_spans, entries, part_name):
    """(r:embed id, image bytes) for the drawing that `m` matched, or None.

    Finds this drawing's r:embed (the blip follows the docPr/cNvPr within the same pic block)
    and resolves it through the part's .rels. Lifted out of `_vision_alt` because the
    decorative-inference path needs the SAME image and must find it with vision switched off —
    the reviewer confirming "this is decorative" has to see what "this" is.

    Search window = to the end of this image's pic block (pptx/xlsx) or up to the next
    same-tag drawing (docx), so we never grab a sibling image's blip."""
    if entries is None or not part_name:
        return None
    if pic_spans is not None:
        region_end = next((b for a, b in pic_spans if a <= m.start() < b), m.end())
    else:
        # `tag` may be a regex alternation — find the next same-tag drawing with a regex.
        _nm = re.compile(rf"<{tag}\b").search(xml, m.end())
        nxt = _nm.start() if _nm else -1
        region_end = nxt if nxt != -1 else len(xml)
        # An authored docPr extension can exceed 8 kB before the actual blip. Bound by
        # the image container, not an arbitrary byte window that silently hides its pixels.
        container_end = re.search(r"</wp:(?:inline|anchor)>", xml[m.end():region_end])
        if container_end:
            region_end = m.end() + container_end.start()
    rid_m = _R_EMBED.search(xml, m.end(), region_end)
    if not rid_m:
        return None
    img = _resolve_media(entries, part_name, rid_m.group(1))
    if not img:
        return None
    # No _MIN_IMG_BYTES floor here. That floor is a VISION concern — a 1x1 spacer is an image
    # the model cannot describe — and it lives with the vision caller. To decorative inference a
    # degenerate image is not noise, it is the signal: a 37-byte 1x1 GIF spacer is the single
    # clearest "this is decorative" a document can offer, and this lookup used to discard it.
    if part_name.startswith("word/"):
        from office_visible_image import visible_word_relationship
        cropped, visible = visible_word_relationship(entries, part_name, rid_m.group(1))
        if cropped:
            if visible is None:
                return None
            img = visible
    return rid_m.group(1), img


def _img_size(img: bytes) -> tuple[int, int] | None:
    """(width, height) in the image's own pixels, or None if it will not decode.

    INTRINSIC size, not the rendered extent (a:ext cx/cy in EMU). A spacer is intrinsically
    1x1 or a hairline however it is stretched, and a photo scaled small in the layout is still
    a photo. Reading the bytes needs no XML archaeology and no unit conversion. The trade: an
    image whose only decorative quality is being rendered tiny is not caught here."""
    try:
        import io as _io
        from PIL import Image
        with Image.open(_io.BytesIO(img)) as im:
            return im.size
    except Exception:
        return None


def _rels_name_for(part_name: str) -> tuple[str, str]:
    """(directory, sibling .rels part name) for an OPC part — word/document.xml →
    ('word', 'word/_rels/document.xml.rels')."""
    d, base = (part_name.rsplit("/", 1) if "/" in part_name else ("", part_name))
    return d, (f"{d}/_rels/{base}.rels" if d else f"_rels/{base}.rels")


def _media_path(rels_bytes: bytes, d: str, rid: str) -> str | None:
    """The package path a drawing's r:embed id points at, or None for external/linked images.
    Split out of _resolve_media so a caller holding a zip (rather than a dict of every part)
    can resolve one image without inflating the whole package."""
    try:
        root = ET.fromstring(rels_bytes)
    except (ET.ParseError, TypeError, ValueError):
        return None
    # OPC namespace prefixes are arbitrary. Resolve an exact relationship element,
    # never a substring in unrelated metadata or a neighboring image relationship.
    namespace = "{http://schemas.openxmlformats.org/package/2006/relationships}"
    if root.tag not in (namespace + "Relationships", "Relationships"):
        return None
    matches = [rel for rel in root
               if rel.tag in (namespace + "Relationship", "Relationship")
               and rel.get("Id") == rid]
    if len(matches) != 1:
        return None
    rel = matches[0]
    if rel.get("TargetMode") == "External":
        return None
    target = rel.get("Target")
    if not target:
        return None
    if target.startswith(("http:", "https:", "file:")):
        return None
    if target.startswith("/"):
        # An ABSOLUTE package path ("/xl/media/image1.png"), valid OOXML meaning "from the package
        # root" — NOT an external link. Some generators emit this form; treating the leading slash
        # as "external" (as this did) discarded every image in such a workbook, so its pictures got
        # no alt/decorative/vision handling and fell through as bare HITL findings. Resolve from the
        # root (entries are keyed without the leading slash), not relative to the part directory.
        return posixpath.normpath(target.lstrip("/"))
    return posixpath.normpath(posixpath.join(d, target) if d else target)


def _resolve_media(entries: dict, part_name: str, rid: str) -> bytes | None:
    """Resolve a drawing's r:embed relationship id to the embedded image bytes.

    Reads the part's sibling .rels (word/_rels/document.xml.rels etc.), finds the
    Relationship with this Id, and returns the referenced media part's bytes. Returns
    None for external (linked) images or anything that can't be resolved locally."""
    d, rels_name = _rels_name_for(part_name)
    rels = entries.get(rels_name)
    if not rels:
        return None
    resolved = _media_path(rels, d, rid)
    return entries.get(resolved) if resolved else None


def image_bytes_for_locator(doc_bytes: bytes, locator: str) -> bytes | None:
    """The embedded image named by a `part_name#rId` locator, straight out of the package.

    Public because the REVIEW-TIME AI draft needs the very image the remediator deferred:
    /ai/suggest can only produce genuine alt text for 1.1.1 if it can hand a vision model the
    pixels, and hitl_queue.evidence[].locator is the only durable name that image has.

    Reads exactly two parts (the .rels and the media it points at) — never inflate a 50MB deck
    to fetch one picture. Best-effort: a malformed locator, an external/linked image, or an
    unreadable package all return None, and the caller falls back to the text template."""
    if not doc_bytes or not locator or "#" not in locator:
        return None
    part_name, rid = locator.rsplit("#", 1)
    d, rels_name = _rels_name_for(part_name)
    try:
        import io as _io
        with zipfile.ZipFile(_io.BytesIO(doc_bytes)) as z:
            names = set(z.namelist())
            if rels_name not in names:
                return None
            media = _media_path(z.read(rels_name), d, rid)
            if not media or media not in names:
                return None
            return z.read(media)
    except Exception:
        return None


def _inject_descr(xml: str, tag: str, *, pic_only_within: str | None = None,
                  captions: bool = False, entries: dict | None = None,
                  part_name: str | None = None, vision_enabled: bool = False,
                  context_file: str = "", scan_id: str | None = None,
                  vision_budget: list | None = None,
                  applied_fixes: list | None = None,
                  proposals: list | None = None,
                  evidence: list | None = None,
                  guidance: str = "", skip_locators=(),
                  fixed_locators: list | None = None) -> tuple[str, list[tuple[str, str]], int]:
    """Add descr= to every <tag …> lacking one, from a faithful source or (when
    vision_enabled) a genuine vision description of the image bytes.

    pic_only_within: restrict to tags inside <pic>…</pic> blocks (pptx/xlsx have
    cNvPr on every shape; only pictures need alt). captions: derive from the next
    paragraph's text when it looks like a caption (docx). entries/part_name let the
    vision path resolve the image bytes via the part's .rels; vision_budget is a
    single-element mutable [n] shared across parts so one document's total vision
    calls stay bounded. Returns (new_xml, [(alt, source)…], deferred_count).

    `evidence` collects {locator, thumb} for every image that ends up deferred to a human.
    It is NOT a proposal — there is no value to approve — it is the picture the reviewer is
    being asked to describe. Extracting it is deterministic (the bytes are in the package,
    reached through the part's .rels), so it does not depend on vision_enabled: the reviewer
    who most needs to see the image is exactly the one the vision model could not help.

    `fixed_locators`, when given, receives one entry per `fixed` entry, in the same order: the
    exact `part#fragment` of the element written (see _element_locator), or None when no
    fragment resolves back to that element alone.
    """
    fixed: list[tuple[str, str]] = []
    deferred = 0
    pic_spans = None
    if pic_only_within:
        pic_spans = [m.span() for m in re.finditer(
            rf"<{pic_only_within}[ >].*?</{pic_only_within}>", xml, re.S)]

    out, last = [], 0
    # Total candidates, counted before the walk so the line can say "3 of 35" rather than a bare
    # ordinal. "image 3" answers nothing on a document whose length the user cannot see; "3 of 35"
    # is the difference between "it is working" and "it is stuck", which is the entire question
    # this line exists to answer.
    _n_images = sum(1 for _mm in re.finditer(rf"<{tag}\b([^>]*?)(/?)>", xml)
                    if pic_spans is None or any(a <= _mm.start() < b for a, b in pic_spans))
    _img_i = 0
    for m in re.finditer(rf"<{tag}\b([^>]*?)(/?)>", xml):
        attrs, selfclose = m.group(1), m.group(2)
        out.append(xml[last:m.start()]); last = m.end()
        keep = m.group(0)
        inside_pic = pic_spans is None or any(a <= m.start() < b for a, b in pic_spans)
        if inside_pic and _is_junk_descr(_ATTR(attrs, "descr")):
            _img_i += 1
            # Published BEFORE the work, not after: the vision call below is the slow step, and a
            # line written afterwards names an image that has already finished while the user
            # waits on the next one. Rate-limited inside activity.record — a vision call takes
            # seconds and deserves its own line, a junk-descr skip takes microseconds and does not.
            _act_record(scan_id, file=context_file, sc="1.1.1",
                        action=f"describing image {_img_i} of {_n_images}",
                        phase="remediating")
            # decorative? the marker lives in the element's extLst children
            # `tag` may be a regex alternation (e.g. `(?:xdr:)?cNvPr`), so match the closing tag
            # with a regex, not a literal find. Only reached for a non-self-closing element.
            if selfclose:
                block_end = m.end()
            else:
                _cm = re.compile(rf"</{tag}>").search(xml, m.end())
                block_end = _cm.start() if _cm else -1
            block = xml[m.start():block_end if block_end != -1 else m.end()]
            # THE SHARED PREDICATE, not a third copy of it. This was its own inline substring
            # test for `decorative val="1"`, and the marker apply_alt writes declares its
            # namespace INLINE — `<adec:decorative xmlns:adec="…" val="1"/>` — so the xmlns sits
            # between the two tokens and the substring never matched. Measured, not inferred: an
            # image carrying ACP's own marker came back `deferred=1` from this function, so a
            # reviewer who marked an image decorative was asked to describe it again on the next
            # scan, and on every scan after that. Same root cause as the detector-side bug, same
            # fix: one expression, imported.
            if _images_is_decorative(block):
                out.append(keep); continue
            caption = None
            if captions:
                # text of the paragraph following the one holding this drawing
                p_end = xml.find("</w:p>", m.end())
                if p_end != -1:
                    nxt = re.search(r"<w:p[ >].*?</w:p>", xml[p_end + 6:], re.S)
                    if nxt:
                        cand = _strip_tags(nxt.group(0))
                        if _CAPTION_LEAD.match(cand):
                            caption = cand
            src = _derive_alt(attrs, caption)
            # Decorative inference (heuristic — Low, human, NEVER auto). Two signals now reach it:
            #
            #   NAME  — the shape is called "logo" / "divider" / "spacer". Announcing that layer
            #           name as alt is worse than a missing-alt finding, so propose "mark
            #           decorative" instead and let a human confirm.
            #   SIZE  — the image is intrinsically a 1x1 spacer, a hairline, an extreme-aspect
            #           divider, or an icon-sized glyph. infer_decorative has always accepted
            #           width/height; nothing ever passed them, so those three signals were dead
            #           code. Real documents name images "Picture 3", which is precisely the case
            #           the name signal cannot see.
            #
            # SIZE is consulted ONLY when no faithful alt source exists. A title, a caption, or a
            # descriptive shape name is authored by a human and outranks a size heuristic: a wide
            # banner photo named "Team Photo" has an extreme aspect ratio and is NOT decorative.
            # Overriding a faithful source would trade a mediocre alt for a proposal to erase the
            # image — the expensive direction of the error.
            _faithful = src[1] if src else None
            if proposals is not None and _faithful in (None, "the shape's descriptive name"):
                import proposals as _prop
                # Resolve the image once: its bytes give both the size signal and the thumbnail.
                _found = _image_bytes_for(xml, m, tag, pic_spans, entries, part_name)
                # Size is passed ONLY when there is no faithful source. With a descriptive name
                # in hand the name is the evidence, and a size that disagrees must not overrule
                # it — otherwise "Team Photo" (1200x100, ratio 12) is proposed for erasure.
                _size = _img_size(_found[1]) if (_found and _faithful is None) else None
                # The pixel bytes are a stronger, name-independent signal than size: a near-solid
                # background block (which a vision model can only describe as "black, nothing to
                # describe") is decorative regardless of its dimensions. Consulted only when there
                # is no faithful alt source, same as size — a human-authored name/caption outranks
                # any content heuristic, so real content is never erased on pixels alone.
                _bytes = _found[1] if (_found and _faithful is None) else None
                _deco = _prop.infer_decorative(
                    filename=(src[0] if src else _ATTR(attrs, "name").strip()),
                    width=_size[0] if _size else None,
                    height=_size[1] if _size else None,
                    image=_bytes)
                if _deco:
                    # Show the image. The reviewer's whole job is to look at the picture and say
                    # whether the guess is right — marking real content as decorative hides it
                    # from screen readers permanently. No vision model needed; the bytes are in
                    # the package. A thumb we can't build is simply omitted.
                    proposals.append(_prop.proposal(
                        locator=f"{part_name}#{_ATTR(attrs, 'name').strip()}",
                        before="(no alt text)",
                        proposed_value="Mark as decorative — no alt text needed",
                        rationale=_deco["rationale"],
                        source="decorative-image inference (heuristic)", kind="decorative",
                        thumb=_prop.thumb_b64(_found[1]) if _found else None))
                    deferred += 1
                    out.append(keep)
                    continue
            if src is None:
                src = _vision_alt(xml, m, tag, selfclose, pic_spans, entries, part_name,
                                  vision_enabled, context_file, caption, scan_id, vision_budget,
                                  applied_fixes, proposals, guidance=guidance, skip_locators=skip_locators)
            # xlsx chart with no vision draft (no GPU / no key / model down): compose a keyless draft
            # from the sheet's OWN adjacent data table — the numbers a chart plots live in cells right
            # beside it. Offered as a PROPOSAL so the reviewer confirms it against the visible chart;
            # grounded in the document's real values, never a guess. This is the "something to approve
            # even with no AI at all" fallback, one rung above handing the human a blank box.
            if src is None and proposals is not None and part_name and part_name.startswith("xl/drawings/"):
                _ev = _image_bytes_for(xml, m, tag, pic_spans, entries, part_name)
                if _ev:
                    _rid, _img = _ev
                    import xlsx_chart_context as _xcc
                    _cd = _xcc.draft_from_entries(entries, part_name, _rid)
                    if _cd:
                        import proposals as _prop
                        _draft, _source = _cd
                        proposals.append(_prop.proposal(
                            locator=f"{part_name}#{_rid}", before="(no alt text)",
                            proposed_value=_draft, kind="chart-data", source=_source,
                            rationale="composed from the chart's adjacent data table on the sheet",
                            thumb=_prop.thumb_b64(_img)))
                        deferred += 1
                        out.append(keep); continue
            if src is None:
                # This image is going to a human. Hand them the picture: without it the card
                # asks someone to write alt text for an image they cannot see. Deterministic —
                # same lookup the decorative branch above already does, no model involved.
                if evidence is not None:
                    _ev = _image_bytes_for(xml, m, tag, pic_spans, entries, part_name)
                    if _ev:
                        import proposals as _prop
                        _rid, _img = _ev
                        evidence.append({"locator": f"{part_name}#{_rid}",
                                         "thumb": _prop.thumb_b64(_img)})
                deferred += 1
                out.append(keep); continue
            alt, origin = src
            new_attrs = re.sub(r'\s*\bdescr="[^"]*"', "", attrs)  # drop the empty/junk descr
            # `tag` is a matching expression for Excel's prefixed/default namespace
            # variants, not an XML element name. Preserve the producer's actual name.
            actual_tag = re.match(r"<([^\s/>]+)", keep).group(1)
            keep = f'<{actual_tag}{new_attrs} descr="{_xesc(alt)}"{selfclose}>'
            fixed.append((alt, origin))
            if fixed_locators is not None:
                # Offsets of `m` are into the ORIGINAL xml, which is what a locator is resolved
                # against: this edit only adds an attribute, it moves no element.
                fixed_locators.append(_element_locator(xml, m.start(), part_name))
        out.append(keep)
    out.append(xml[last:])
    return "".join(out), fixed, deferred


def _vision_alt(xml, m, tag, selfclose, pic_spans, entries, part_name, vision_enabled,
                context_file, caption, scan_id, vision_budget, applied_fixes=None,
                proposals=None, *, guidance: str = "", skip_locators=()) -> tuple[str, str] | None:
    """Structured, OCR-anchored alt text for an unlabelled image from the local vision model.

    Finds this drawing's r:embed (the blip follows the docPr/cNvPr within the same
    pic/drawing), resolves it to image bytes, and calls the vision model. Bounded by
    vision_budget so a document with many images can't run unboundedly.

    Honesty split (WCAG 1.1.1 intent stays human): a GROUNDED description — one anchored in
    text OCR'd from the image (a chart's headline/labels) — is auto-applied inline and
    returned like a faithful source. An UNGROUNDED description — a pure vision guess on a
    textless image — is NOT written inline; it is collected into `proposals` (Medium) so the
    finding stays open and the reviewer approves the machine's guess in one click rather than
    the platform silently asserting alt a human never confirmed conveys the intended meaning.
    Explicit evidence contradictions remain drafts and cannot use either automatic
    grounding or the optional consistency-policy write path."""
    if not (vision_enabled and entries is not None and part_name):
        return None
    if vision_budget is not None and vision_budget[0] <= 0:
        return None
    found = _image_bytes_for(xml, m, tag, pic_spans, entries, part_name)
    if not found:
        return None
    rid, img = found
    if f"{part_name}#{rid}" in skip_locators:
        return None
    # Degenerate images (1x1 spacers etc.) have nothing for a vision model to describe. The
    # floor lives here rather than in the lookup, because the decorative heuristic wants them.
    if len(img) < _MIN_IMG_BYTES:
        return None
    try:
        import ai as _ai
        # allow_transcription: this path OCRs the embedded image itself, so prose read from it
        # really is the image's own text — the alt for an image of text is that text (WCAG 1.1.1).
        res = _ai.describe_image_structured(img, filename=context_file, context=caption or "",
                                            scan_id=scan_id, file=context_file,
                                            allow_transcription=True, guidance=guidance)
    except Exception:
        res = None
    if vision_budget is not None:
        # Failed inference consumes capacity too. A timeout must not allow every
        # remaining image to bypass the document's attempted-call bound.
        vision_budget[0] -= 1
    if not res:
        return None
    automatic_write_blocked = res.get("automatic_write_blocked") is True
    source_policy_review = bool(res.get("quality_source_review"))
    if res.get("grounded") and not automatic_write_blocked and not source_policy_review:
        # An image of text is transcribed, not described — no model ran, so the provenance must
        # not claim one (it used to interpolate res['model'], which is None on that path and
        # rendered as "AI vision model (None)").
        transcribed = res.get("source") == "ocr"
        if applied_fixes is not None:
            applied_fixes.append({
                "rule_id": "SC_1_1_1",
                "locator": f"{part_name}#{rid}",
                "model": res.get('model'),
                "model_call_id": res.get('ai_call_id'),
                "value": res["alt"],
                "source": ("the image's own text, read by OCR and transcribed verbatim"
                           if transcribed
                           else f"AI vision model ({res['model']}), anchored in image text"),
                "thumb": _thumb_b64(img),
            })
        return res["alt"], ("the image's own text, transcribed verbatim (WCAG 1.1.1 — the alt "
                            "for an image of text is that text)" if transcribed
                            else "an AI vision description anchored in the image's own text")
    # Auto-apply-validated policy (opt-in, default OFF): an ungrounded draft that an
    # INDEPENDENT second reading confirms ('consistent' consistency cross-check — a
    # measurement, never the model grading itself, ADR 0016) is applied inline like a
    # grounded one. Any other verdict — divergent, validator down, policy off — falls
    # through to the one-click proposal exactly as before, and the re-scan verify gate
    # still decides whether the criterion actually cleared.
    try:
        import core as _core
        if not automatic_write_blocked and not source_policy_review and _core.store.get_auto_apply_validated():
            v = _ai.validate_alt_text(img, res["alt"], filename=context_file,
                                      scan_id=scan_id, file=context_file)
            if v and v.get("verdict") == "consistent":
                if applied_fixes is not None:
                    applied_fixes.append({
                        "rule_id": "SC_1_1_1",
                        "locator": f"{part_name}#{rid}",
                        "model": res.get("model"),
                        "model_call_id": res.get("ai_call_id"),
                        "value": res["alt"],
                        "source": (f"AI vision model ({res['model']}), confirmed by an "
                                   "independent second reading (consistency cross-check)"),
                        "thumb": _thumb_b64(img),
                    })
                return res["alt"], ("an AI vision description confirmed by an independent "
                                    "second reading (auto-apply-validated policy)")
    except Exception:
        swallowed("remediate_office._vision_alt: validating and auto-applying vision alt text "
                  "failed", scan_id)
    # Ungrounded guess → surface for one-click approval instead of auto-writing it. When a
    # DISTINCT validator model is configured (ACP_ALT_VALIDATOR_MODEL), run the consensus
    # cross-check now and attach the verdict, so the card arrives already showing whether a
    # second model agrees — automatic transparency, no reviewer click. Real model agreement,
    # never a fabricated score (ADR 0016). Best-effort: no validator / a failure → no badge.
    agreement = None
    if not automatic_write_blocked and not source_policy_review and getattr(_ai, "_ALT_VALIDATOR_MODEL", ""):
        try:
            v = _ai.validate_alt_text(img, res["alt"], filename=context_file,
                                      scan_id=scan_id, file=context_file)
            if v:
                agreement = {"verdict": v.get("verdict"),
                             "second_opinion": v.get("second_opinion"),
                             "validator_model": v.get("validator_model")}
        except Exception:
            agreement = None
    if proposals is not None:
        import proposals as _prop
        p = _prop.proposal(
            locator=f"{part_name}#{rid}",
            before="(no alt text — image skipped by screen readers)",
            proposed_value=res["alt"],
            rationale=res.get("evidence") or "AI vision description — confirm it matches the intent",
            source=f"AI vision model ({res['model']})",
            thumb=_thumb_b64(img),
            sc="1.1.1",
            model=res.get("model"),
            model_call_id=res.get("ai_call_id"),
            # The reason this is a proposal and not an applied fix, stated rather than implied.
            # Every branch above this one auto-applied because it had an anchor — the image's own
            # OCR text, or an independent second reading that agreed. This branch has neither, so
            # the draft describes what the model believes it saw and nothing in the document
            # corroborates it. That is exactly what a reviewer needs to know before trusting it.
            why_review=("The image contains no readable text, so nothing in the document can "
                        "corroborate this description — it is the model's reading of the picture "
                        "and cannot be checked automatically. Confirm it conveys what the image "
                        "is FOR, not merely what it shows."),
            context=caption or None)
        if automatic_write_blocked:
            p.update(automatic_write_blocked=True, reason_code=res.get("reason_code"),
                     why_review=res.get("evidence") or "The draft contradicts visible image evidence; review it individually.")
        for key in ("chart_review", "review_status", "approval_required", "caption_validation", "quality_source_review"):
            if key in res:
                p[key] = res[key]
        if agreement:
            p["agreement"] = agreement          # {verdict, second_opinion, validator_model}
        proposals.append(p)
    return None




def _fix_image_alt(entries: dict, *, vision_enabled: bool = False,
                   context_file: str = "", scan_id: str | None = None,
                   applied_fixes: list | None = None, proposals: list | None = None,
                   evidence: list | None = None,
                   diffs=None) -> tuple[list[str], int]:
    """Inject alt text across all image-bearing parts — faithful source first, then a
    genuine vision description when vision_enabled. Grounded (OCR-anchored) vision alt is
    applied inline; an ungrounded vision guess is collected into `proposals` for one-click
    approval, not written. Returns (applied descriptions, count of images deferred/proposed
    to human review)."""
    applied: list[str] = []
    deferred = 0
    vision_budget = [_VISION_MAX_IMAGES]
    for name in list(entries):
        for pat, tag, wrapper, captions in _ALT_TARGETS:
            if not pat.match(name):
                continue
            try:
                xml = entries[name].decode("utf-8")
            except UnicodeDecodeError:
                continue
            fixed_locators: list = []
            new_xml, fixed, part_deferred = _inject_descr(
                xml, tag, pic_only_within=wrapper, captions=captions,
                entries=entries, part_name=name, vision_enabled=vision_enabled,
                context_file=context_file, scan_id=scan_id, vision_budget=vision_budget,
                applied_fixes=applied_fixes, proposals=proposals, evidence=evidence,
                fixed_locators=fixed_locators)
            deferred += part_deferred
            if fixed:
                entries[name] = new_xml.encode("utf-8")
                for (alt, origin), locator in zip(fixed, fixed_locators):
                    applied.append(f"Alt text \"{alt[:60]}\" set from {origin} · 1.1.1")
                    _rec(diffs, "1.1.1", "(no alt text — image was skipped by screen readers)",
                         alt, f"set from {origin}", locator=locator)
    return applied, deferred


# ── pptx slide-level structural remediation (2.4.2 / 1.4.3 / 1.4.6 / 1.3.2 / 1.3.1) ────
# These need the slide XML, not just OPC core-properties, and are pptx-only.
# Each was verified against the DigitalA11y engine (re-scan clears the finding):
#   * title   — an off-slide title placeholder (y above the canvas) is read by AT
#               and clears "slide is missing a title" without touching the design.
#   * contrast— an explicit low-contrast run's colour is swapped to black or white,
#               whichever reaches >=4.5:1 on the shape's explicit fill.
#   * reading — spTree children are sorted by visual position (y, then x). This
#               also sets z-order (first-in-document renders lowest); top-left
#               backgrounds (y~=0) sort first, so typical stacking is preserved.
# Only explicit srgbClr colours on explicit solid fills are recoloured — the same
# narrow scope office_structure.pptx_contrast_checks measures, so every recolour
# targets a run the scanner actually flagged. All changes go in the provenance stamp.
_PPTX_TITLE_PH = re.compile(r'<p:ph\b(?=[^>]*\btype="(?:ctrTitle|title)")')
_A_T = re.compile(r"<a:t\b[^>]*>([^<]*)</a:t>")
_A_R = re.compile(r"<a:r>.*?</a:r>", re.S)
_P_SP = re.compile(r"<p:sp>.*?</p:sp>", re.S)
# A DrawingML table and its parts. \b after "tbl" keeps this from matching <a:tblPr>
# or <a:tblGrid>; tables don't nest, so a non-greedy body captures exactly one table.
_A_TBL = re.compile(r"<a:tbl\b[^>]*>.*?</a:tbl>", re.S)
_A_TR = re.compile(r"<a:tr\b")
_A_TBLPR_OPEN = re.compile(r"<a:tblPr\b([^>]*?)(/?)>")
_FIRSTROW = re.compile(r'\bfirstRow="([^"]*)"')


def _pptx_has_title(xml: str) -> bool:
    m = _PPTX_TITLE_PH.search(xml)
    if not m:
        return False
    return bool("".join(_A_T.findall(xml[m.end():].split("</p:sp>", 1)[0])).strip())


_title_seq = [9000]


def _pptx_add_title(xml: str, text: str) -> str:
    _title_seq[0] += 1
    sp = (f'<p:sp><p:nvSpPr><p:cNvPr id="{_title_seq[0]}" name="Title {_title_seq[0]}"/>'
          f'<p:cNvSpPr><a:spLocks noGrp="1"/></p:cNvSpPr><p:nvPr><p:ph type="title"/></p:nvPr>'
          f'</p:nvSpPr><p:spPr><a:xfrm><a:off x="457200" y="-1200000"/>'
          f'<a:ext cx="8229600" cy="1000000"/></a:xfrm></p:spPr>'
          f'<p:txBody><a:bodyPr/><a:lstStyle/><a:p><a:r><a:rPr lang="en-US"/>'
          f'<a:t>{_xesc(text)}</a:t></a:r></a:p></p:txBody></p:sp>')
    # grpSpPr may be a closing tag (<p:grpSpPr>…</p:grpSpPr>) or self-closing
    # (<p:grpSpPr/>); insert the title placeholder right after it either way. A
    # lambda replacement avoids treating the shape XML as a backreference template.
    return re.sub(r"</p:grpSpPr>|<p:grpSpPr\s*/>", lambda m: m.group(0) + sp, xml, count=1)


def _pptx_mark_table_headers(xml: str, diffs=None, *, part_name: str | None = None) -> tuple[str, int]:
    """WCAG 1.3.1: mark the first row of every multi-row DrawingML table as a header
    row by setting firstRow="1" on its <a:tblPr> — the direct analogue of the docx
    <w:tblHeader> and xlsx headerRowCount fixes, and exactly what the analyser's
    PPTX-TABLE-001 rule checks for. String-surgical: only the tblPr tag is rewritten,
    so every other byte of the slide is preserved. Fail closed — a single-row or
    otherwise degenerate table (<= 1 <a:tr>) is never given a header row.
    Returns (new_xml, tables_marked)."""
    n = [0]

    def fix(m):
        block = m.group(0)
        if len(_A_TR.findall(block)) <= 1:
            return block                       # degenerate / single-row: leave it alone
        pr = _A_TBLPR_OPEN.search(block)
        if pr is None:                         # no <a:tblPr> — insert one as tbl's first child
            cut = block.index(">") + 1
            n[0] += 1
            _rec(diffs, "1.3.1", "table had no header row marked (<a:tblPr firstRow> absent)",
                 'first row marked as a header row (<a:tblPr firstRow="1"/>)',
                 "so a screen reader announces the column heading for every data cell",
                 locator=_part_locator(part_name))
            return block[:cut] + '<a:tblPr firstRow="1"/>' + block[cut:]
        attrs, close = pr.group(1), pr.group(2)
        fr = _FIRSTROW.search(attrs)
        if fr and fr.group(1) in ("1", "true"):
            return block                       # already a header row — nothing to do
        new_attrs = _FIRSTROW.sub('firstRow="1"', attrs, count=1) if fr else ' firstRow="1"' + attrs
        n[0] += 1
        _rec(diffs, "1.3.1", "table had no header row marked (<a:tblPr firstRow> absent)",
             'first row marked as a header row (firstRow="1")',
             "so a screen reader announces the column heading for every data cell",
             locator=_part_locator(part_name))
        return block[:pr.start()] + "<a:tblPr" + new_attrs + close + ">" + block[pr.end():]

    return _A_TBL.sub(fix, xml), n[0]


def _remediate_pptx_slides(entries: dict, diffs=None, in_scope=None) -> list[str]:
    """Mutate ppt/slides/*.xml in place; return the list of applied-fix messages."""
    import office_structure as _osx           # contrast helpers + narrow-scope regexes
    from xml.etree import ElementTree as ET
    P = "{http://schemas.openxmlformats.org/presentationml/2006/main}"
    A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
    shape_tags = {P + t for t in ("sp", "pic", "graphicFrame", "grpSp", "cxnSp")}
    slides = sorted((n for n in entries if re.fullmatch(r"ppt/slides/slide\d+\.xml", n)),
                    key=lambda s: int(re.search(r"(\d+)", s).group()))
    n_title = n_recolor = n_reorder = n_tblhdr = 0
    # The slide part each nested fixer below is editing. Slide PART, not slide number: the
    # presentation's reading order lives in presentation.xml, so slide3.xml is not "slide 3".
    current = [None]

    def recolor_run_in_sp(sp_match) -> str:
        nonlocal n_recolor
        sp = sp_match.group(0)
        sppr = _osx._PPTX_SPPR.search(sp)
        if not sppr:
            return sp
        fill = _osx._SOLID_SRGB.search(_osx._A_LN_BLOCK.sub("", sppr.group(0)))
        if not fill:
            return sp
        bg = fill.group(1)

        def fix_run(rm):
            nonlocal n_recolor
            run = rm.group(0)
            col = _osx._SOLID_SRGB.search(run)
            if not col or _osx._contrast_ratio(bg, col.group(1)) >= 4.5:
                return run
            text = "".join(_A_T.findall(run)).strip()
            if not text:
                return run
            # Hue-preserving fix: darken/lighten the brand colour only as far as AA needs,
            # rather than flattening every run to pure black/white.
            new = _osx.min_contrast_recolor(col.group(1), bg)
            n_recolor += 1
            _rec(diffs, "1.4.3", f"#{col.group(1).upper()} on #{bg.upper()} "
                 f"({_osx._contrast_ratio(bg, col.group(1)):.1f}:1 — fails AA)",
                 f"#{new} on #{bg.upper()} ({_osx._contrast_ratio(bg, new):.1f}:1 — passes AA)",
                 f'text run "{text[:40]}"', locator=_part_locator(current[0]))
            return run.replace(f'srgbClr val="{col.group(1)}"', f'srgbClr val="{new}"', 1)

        return _A_R.sub(fix_run, sp)

    def reorder(data: bytes) -> bytes:
        nonlocal n_reorder
        for pfx, uri in (("p", P[1:-1]), ("a", A[1:-1]),
                         ("r", "http://schemas.openxmlformats.org/officeDocument/2006/relationships")):
            ET.register_namespace(pfx, uri)
        # Real-world slides sometimes carry XML the .NET analyser tolerates but ElementTree rejects
        # — most commonly an unescaped '&' in a text run ("Workforce & Headcount"). This full parse
        # is the ONLY step in the slide loop that isn't string-surgical, so a strict-parse failure
        # here used to raise out of the whole function and abort the deck's remediation partway
        # (later slides lost their titles + reading order). Skip only the reading-order pass for that
        # one slide instead; the title/header/contrast edits already in `data` are returned intact.
        try:
            root = ET.fromstring(data)
        except ET.ParseError:
            return data
        tree = root.find(f".//{P}cSld/{P}spTree")
        if tree is None:
            return data
        shapes = [c for c in list(tree) if c.tag in shape_tags]

        def key(el):
            off = el.find(f".//{A}off")
            return (int(off.get("y")), int(off.get("x"))) if off is not None and off.get("y") else (10 ** 12, 0)

        ordered = sorted(shapes, key=key)
        if ordered != shapes:
            n_reorder += 1
            _rec(diffs, "1.3.2", "shapes read in the slide's stored (authoring) order",
                 f"{len(ordered)} shapes re-sequenced top-to-bottom, then left-to-right",
                 "so a screen reader follows the visual reading order",
                 locator=_part_locator(current[0]))
            for s in shapes:
                tree.remove(s)
            for s in ordered:
                tree.append(s)
            return ET.tostring(root, encoding="utf-8", xml_declaration=True)
        return data

    for sn in slides:
        current[0] = sn
        xml = entries[sn].decode("utf-8")
        if not _pptx_has_title(xml) and _sc_ok(in_scope, "2.4.2"):
            texts = [t.strip() for t in _A_T.findall(xml) if t.strip()]
            # The slide's title is the FIRST text on it (the title placeholder is authored first and
            # sits at the top), NOT the longest run. `max(…, key=len)` picked the longest text, which
            # is almost always a body paragraph — so the auto-title announced body copy instead of the
            # heading. First-in-authoring-order is the real title far more often.
            title_text = (texts[0] if texts else "Untitled slide")[:250]
            xml = _pptx_add_title(xml, title_text)
            n_title += 1
            _rec(diffs, "2.4.2", "(no title — assistive tech announced the slide as “Untitled”)",
                 title_text, f"programmatic title added to {sn.rsplit('/', 1)[-1]}",
                 locator=_part_locator(sn))
        if _sc_ok(in_scope, "1.3.1"):
            xml, n_th = _pptx_mark_table_headers(xml, diffs, part_name=sn)
            n_tblhdr += n_th
        if _sc_ok(in_scope, "1.4.3"):
            xml = _P_SP.sub(recolor_run_in_sp, xml)
        entries[sn] = (reorder(xml.encode("utf-8")) if _sc_ok(in_scope, "1.3.2")
                       else xml.encode("utf-8"))

    applied: list[str] = []
    if n_title:
        applied.append(f"Added a programmatic title to {n_title} slide(s)")
    if n_recolor:
        applied.append(f"Raised colour contrast to ≥4.5:1 on {n_recolor} text run(s)")
    if n_reorder:
        applied.append(f"Set reading order (visual top-to-bottom) on {n_reorder} slide(s)")
    if n_tblhdr:
        applied.append(f"Marked the first row as a header on {n_tblhdr} table(s) · 1.3.1")
    return applied


def _remediate_xlsx_contrast(entries: dict, diffs=None) -> list[str]:
    """1.4.3 / 1.4.6 xlsx contrast — clone each offending font with a black/white
    colour and repoint its low-contrast cell style, so every flagged font/fill pair
    reaches the true WCAG AA ratio (4.5:1) — the same guarantee min_contrast_recolor
    already makes for docx/pptx (one of pure black/white always clears 4.5:1 against
    any background). AAA (7:1) is a stricter bar some backgrounds can't reach with a
    text-only recolor (a mid-grey fill caps how far black/white text can separate
    from it); this fixer doesn't promise AAA, matching the docx/pptx fixer's own
    target=4.5 default. Mirrors office_structure's resolver exactly (direct-RGB
    fonts + solid/none fills only) so it clears what it flags at the AA bar."""
    import office_structure as _os
    styles = entries.get("xl/styles.xml", b"").decode("utf-8", "ignore")
    if not styles:
        return []
    fonts_raw = _os._FONT_BLOCK.findall(_os._container(_os._FONTS_CONTAINER, styles))
    fill_hexes = [_os._xlsx_fill_color(m) for m in _os._FILL_BLOCK.findall(_os._container(_os._FILLS_CONTAINER, styles))]
    font_hexes = [_os._xlsx_font_color(m) for m in fonts_raw]
    xfs = _os._XF.findall(_os._container(_os._CELLXFS_CONTAINER, styles))
    new_fonts, new_xfs, changed = list(fonts_raw), list(xfs), 0
    for i, xf in enumerate(xfs):
        fid_m, filid_m = _os._FONT_ID.search(xf), _os._FILL_ID.search(xf)
        if not fid_m or not filid_m:
            continue
        fid, filid = int(fid_m.group(1)), int(filid_m.group(1))
        fh = font_hexes[fid] if fid < len(font_hexes) else None
        kh = fill_hexes[filid] if filid < len(fill_hexes) else None
        if not fh or not kh or _os._contrast_ratio(fh[-6:], kh[-6:]) >= 4.5:
            continue
        # Whichever of pure black/white clears more contrast against this fill — same
        # darken-vs-lighten decision min_contrast_recolor already makes for docx/pptx.
        target = ("FF000000" if _os._contrast_ratio(kh[-6:], "000000") >= _os._contrast_ratio(kh[-6:], "FFFFFF")
                  else "FFFFFFFF")
        base = fonts_raw[fid]
        cloned = (re.sub(r"<color\b[^/]*/>", f'<color rgb="{target}"/>', base, count=1)
                  if re.search(r"<color\b[^/]*/>", base) else base + f'<color rgb="{target}"/>')
        new_xfs[i] = re.sub(r'fontId="\d+"', f'fontId="{len(new_fonts)}"', xf, count=1)
        new_fonts.append(cloned)
        changed += 1
        _hx = lambda a: "#" + (a[-6:] if a and len(a) >= 6 else (a or ""))
        _rec(diffs, "1.4.3", f"font {_hx(fh)} on fill {_hx(kh)} — too little luminance difference",
             f"font {_hx(target)} on fill {_hx(kh)}",
             "cell text recoloured to reach the required contrast")
    if not changed:
        return []
    fonts_inner = "".join(f"<font>{f}</font>" for f in new_fonts)
    styles = re.sub(r"<fonts\b[^>]*>.*?</fonts>",
                    lambda m: re.sub(r'count="\d+"', f'count="{len(new_fonts)}"', m.group(0).split(">", 1)[0]) + ">" + fonts_inner + "</fonts>",
                    styles, count=1, flags=re.S)
    styles = re.sub(r"(<cellXfs\b[^>]*>).*?(</cellXfs>)",
                    lambda m: m.group(1) + "".join(new_xfs) + m.group(2), styles, count=1, flags=re.S)
    entries["xl/styles.xml"] = styles.encode("utf-8")
    return [f"Recoloured {changed} low-contrast cell style(s) to reach AA/AAA · 1.4.3 / 1.4.6"]


def _remediate_docx_structure(entries: dict, diffs=None, skipped=None, in_scope=None) -> list[str]:
    """Deterministic docx structural fixes that clear the analyser (WCAG 1.3.1):
    mark the first row of every multi-row table as a header row (w:tblHeader), and ensure
    the heading outline has exactly one Heading 1. Uses lxml so every OOXML namespace in
    word/document.xml round-trips untouched.

    skipped — optional list the caller collects deferrals in; a BARE form field (an
    unlabelled content control with no adjacent text to borrow) is appended here so it
    is surfaced as routed-to-review rather than silently left."""
    from lxml import etree

    name = "word/document.xml"
    if name not in entries:
        return []
    W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    root = etree.fromstring(entries[name])
    applied: list[str] = []
    val_attr = f"{{{W}}}val"
    # Persist structural locations in the existing diff note. Counts are one-based
    # OOXML document order, including paragraphs/tables nested in body content;
    # they are not rendered page numbers, which vary with the document viewer.
    paragraph_numbers = {p: index for index, p in enumerate(root.iter(f"{{{W}}}p"), 1)}
    table_numbers = {table: index for index, table in enumerate(root.iter(f"{{{W}}}tbl"), 1)}
    run_tokens = {}
    run_counts = {}
    for run in root.iter(f"{{{W}}}r"):
        paragraph = next(run.iterancestors(f"{{{W}}}p"), None)
        if paragraph in paragraph_numbers:
            run_counts[paragraph] = run_counts.get(paragraph, 0) + 1
            run_tokens[run] = f"word:p:{paragraph_numbers[paragraph]}:run:{run_counts[paragraph]}"
    def located(note, token):
        return f"{note} [location:{token}]" if token else note
    def paragraph_token(p):
        return f"word:p:{paragraph_numbers[p]}"
    def run_token(run):
        return run_tokens.get(run)

    # Pseudo-heading promotion (1.3.1 / 2.4.6): a body-styled paragraph that is visually a
    # heading (large/bold) becomes a real Heading N so assistive tech can navigate to it.
    # Uses the SAME predicate the detector flags (office_structure.looks_like_pseudo_heading),
    # so the fix clears the re-scan — and that predicate now also rejects large text that is
    # really page furniture (a pull figure, a quote, a wordmark, a byline), so neither side
    # treats it as a heading. The level comes from the font hierarchy in document order and
    # the outline normalisation below then guarantees one H1 and closes any skip. Runs first
    # so promoted paragraphs are counted by the heading-collection that follows.
    import office_structure as _osx
    import proposals as _prop
    pseudo: list = []                      # (pPr_or_None, p, max_half_pt) — STRONG, auto-promote
    ambiguous: list = []                   # heading-like but not distinguished from body
    scanned: list = []                     # (pPr, p, max_hp, text, bold, styled)
    # One vote per BODY paragraph, at its effective size — the explicit run size when it has
    # one, otherwise the style default. Paragraphs already styled as headings are excluded:
    # the question is what this document's BODY is set at, and counting the headings toward it
    # is what would make them stop looking distinguished from it.
    default_hp = _osx.default_run_half_pt(entries.get("word/styles.xml"))
    para_sizes: list[int] = []
    for p in root.iter(f"{{{W}}}p"):
        pPr = p.find(f"{{{W}}}pPr")
        st = pPr.find(f"{{{W}}}pStyle") if pPr is not None else None
        styled = st is not None and (st.get(val_attr) or "").startswith("Heading")
        text = "".join(t.text or "" for t in p.iter(f"{{{W}}}t")).strip()
        bold, max_hp = False, 0
        for r in p.iter(f"{{{W}}}r"):
            rPr = r.find(f"{{{W}}}rPr")
            if rPr is None:
                continue
            bel = rPr.find(f"{{{W}}}b")
            if bel is not None and (bel.get(val_attr) or "1") not in ("0", "false", "off"):
                bold = True
            szel = rPr.find(f"{{{W}}}sz")
            if szel is not None:
                try:
                    sz = int(szel.get(val_attr) or 0)
                except (TypeError, ValueError):
                    sz = 0
                if sz:
                    max_hp = max(max_hp, sz)
        scanned.append((pPr, p, max_hp, text, bold, styled))
        if text and not styled:
            eff = max_hp or default_hp
            if eff:
                para_sizes.append(eff)

    # The baseline is measured across the WHOLE document, so it has to be complete before any
    # paragraph is judged — hence the two passes. `looks_like_pseudo_heading` asks the absolute
    # question ("does this read as a heading"); `heading_signal` adds the relative one ("is it
    # distinguished from the body around it") and only the STRONG answers are stamped.
    body_hp = _osx.body_baseline_half_pt(para_sizes)
    for pPr, p, max_hp, text, bold, styled in scanned:
        # max_hp RAW, not defaulted — heading_signal's first question is the detector's own
        # predicate, and the scanner reads raw run sizes. Substituting the style default here
        # would make a size-less body paragraph clear the 14pt floor in a large-print document
        # and become a candidate the scanner never flagged, breaking the lock-step this gate
        # depends on. The default belongs to the BASELINE only.
        sig = _osx.heading_signal(text, bold=bold, max_half_pt=max_hp,
                                  body_half_pt=body_hp, styled_heading=styled)
        if sig == "strong":
            pseudo.append((pPr, p, max_hp, text))
        elif sig == "weak":
            ambiguous.append(text)
    if pseudo and _sc_ok(in_scope, "1.3.1"):
        # Levels come from the font hierarchy IN DOCUMENT ORDER, not from absolute size rank
        # across the whole document. Rank gave Heading 1 to the largest paragraph whatever it
        # was — a pull quote or a headline figure took the document title's level and pushed
        # every real section down one — and collapsed the sections onto Heading 6 as soon as
        # the document used more than six distinct sizes. This path writes w:pStyle
        # unattended, so nobody sees that happen. Ordered nesting is also what makes the two
        # normalisation passes below land correctly: the first pseudo-heading is Heading 1, so
        # a document that already styled its own Heading 1 keeps it (the promoted one comes
        # later in document order and is the one demoted to Heading 2), and peer sections
        # share a level instead of arriving as gaps for the skip pass to close.
        _levels = _prop.heading_level_sequence([hp for _, _, hp, _ in pseudo])
        for (pPr, p, _hp, text), lvl in zip(pseudo, _levels):
            if pPr is None:
                pPr = p.makeelement(f"{{{W}}}pPr", {})
                p.insert(0, pPr)
            st = pPr.find(f"{{{W}}}pStyle")
            if st is None:
                st = pPr.makeelement(f"{{{W}}}pStyle", {})
                pPr.insert(0, st)
            st.set(val_attr, f"Heading{lvl}")
            _rec(diffs, "1.3.1", f'paragraph “{text[:40]}” was body text styled to look like a heading',
                 f"promoted to Heading {lvl} style",
                 located("so it joins the heading outline assistive tech navigates by", paragraph_token(p)),
                 locator=paragraph_token(p))
        applied.append(f"Promoted {len(pseudo)} visually-styled pseudo-heading(s) to real headings · 1.3.1")

    # Ambiguous candidates are DEFERRED, not dropped. The finding still stands — the scanner
    # flagged these and this function declined to guess — so it has to be visible, or a
    # large-print document reads as "nothing to fix here" when the truth is "we could not tell
    # which of these are sections". Reported whether or not anything was promoted, and outside
    # the `pseudo` branch for exactly that reason: the all-weak document is the case this exists
    # for, and it is the one where `pseudo` is empty.
    if ambiguous and skipped is not None and _sc_ok(in_scope, "1.3.1"):
        skipped.append(
            f"{len(ambiguous)} paragraph(s) look like headings but are not larger than this "
            f"document's body text — left unchanged for review rather than restyled "
            f"(e.g. “{ambiguous[0][:40]}”)")

    # Table headers (1.3.1): the first row of every multi-row table gets w:tblHeader,
    # which is exactly what the analyser's HasHeaderRow() checks for.
    tbl_fixed = 0
    for tbl in (root.iter(f"{{{W}}}tbl") if _sc_ok(in_scope, "1.3.1") else ()):
        # Rows may be wrapped in content controls. Nested tables own their rows.
        rows = [row for row in tbl.iter(f"{{{W}}}tr")
                if next(row.iterancestors(f"{{{W}}}tbl"), None) is tbl]
        if len(rows) <= 1:
            continue
        first = rows[0]
        trPr = first.find(f"{{{W}}}trPr")
        if trPr is None:
            trPr = first.makeelement(f"{{{W}}}trPr", {})
            first.insert(0, trPr)                      # trPr must be the first child of tr
        header = trPr.find(f"{{{W}}}tblHeader")
        # OOXML on/off properties may be present but explicitly disabled. Merely
        # finding the element must not leave a known-disabled header in the saved file.
        if header is None or (header.get(val_attr) or "").lower() in {"0", "false", "off"}:
            if header is None:
                trPr.insert(0, trPr.makeelement(f"{{{W}}}tblHeader", {}))
            else:
                header.attrib.pop(val_attr, None)
            tbl_fixed += 1
            _rec(diffs, "1.3.1", "first row was ordinary data cells (<w:tr>)",
                 "first row marked as a repeating header row (<w:tblHeader/>)",
                 located("so a screen reader announces the column heading for every data cell",
                         f"word:table:{table_numbers[tbl]}:row:1"),
                 locator=f"word:table:{table_numbers[tbl]}:row:1")
    if tbl_fixed:
        applied.append(f"Marked the first row as a header on {tbl_fixed} table(s) · 1.3.1")

    # Heading outline (1.3.1): exactly one Heading 1. Promote the top heading if none is
    # level 1; demote any extra Heading 1s to Heading 2. Matches the doc's own style-id
    # spelling ("Heading1" vs "Heading 1") so the promoted style still resolves.
    levels = {spelling: level for level in range(1, 7)
              for spelling in (f"heading{level}", f"heading {level}")}
    headings = []
    def heading_level(style, outline):
        explicit = outline.get(val_attr) if outline is not None else None
        if explicit is not None and explicit.isdigit() and 0 <= int(explicit) <= 8:
            return int(explicit) + 1
        return levels.get((style.get(val_attr) or '').casefold()) if style is not None else None
    def set_level(style, outline, level):
        if style is not None and (style.get(val_attr) or '').casefold() in levels:
            style.set(val_attr, f"Heading {level}" if ' ' in style.get(val_attr) else f"Heading{level}")
        if outline is not None:
            outline.set(val_attr, str(level - 1))
    for p in root.iter(f"{{{W}}}p"):
        pPr = p.find(f"{{{W}}}pPr")
        st = pPr.find(f"{{{W}}}pStyle") if pPr is not None else None
        outline = pPr.find(f"{{{W}}}outlineLvl") if pPr is not None else None
        level = heading_level(st, outline)
        if level is not None:
            headings.append((st, outline, level))
    if headings:
        h1s = [h for h in headings if h[2] == 1]
        if not h1s and _sc_ok(in_scope, "1.3.1"):
            st, outline, level = headings[0]
            _top = paragraph_token((st if st is not None else outline).getparent().getparent())
            set_level(st, outline, 1)
            applied.append("Promoted the top heading to Heading 1 · 1.3.1")
            _rec(diffs, "1.3.1", f"top heading level was H{level} — document had no Heading 1",
                 "top heading promoted to Heading 1",
                 located("so the outline has a single, unambiguous document title level", _top),
                 locator=_top)
        elif len(h1s) > 1 and _sc_ok(in_scope, "1.3.1"):
            for st, outline, _ in h1s[1:]:
                set_level(st, outline, 2)
            applied.append(f"Demoted {len(h1s) - 1} extra Heading 1(s) to Heading 2 · 1.3.1")
            _rec(diffs, "1.3.1", f"{len(h1s)} separate Heading 1s competed as the document title",
                 f"kept 1 Heading 1; demoted {len(h1s) - 1} to Heading 2",
                 located("so the heading outline nests correctly under one title", "word:document:outline"),
                 locator="word:document:outline")
        skip_fixed, prev_lvl = 0, 0
        for st, outline, _ in (headings if _sc_ok(in_scope, "2.4.6") else ()):
            lvl = heading_level(st, outline)
            if prev_lvl > 0 and lvl > prev_lvl + 1:
                lvl = prev_lvl + 1
                set_level(st, outline, lvl)
                skip_fixed += 1
            prev_lvl = lvl
        if skip_fixed:
            applied.append(f"Closed {skip_fixed} skipped heading level(s) so the outline has no gaps · 2.4.6")

    # Contrast (1.4.3): recolour any run whose explicit w:color falls below 4.5:1 against
    # its paragraph background — to black or white, whichever gives better contrast. Mirrors
    # the pptx contrast fix and matches what the analyser's ColourContrastRule measures
    # (direct run colour vs paragraph shading, default white).
    import office_structure as _osx
    val_attr = f"{{{W}}}val"
    contrast_fixed = 0
    for p in (root.iter(f"{{{W}}}p") if _sc_ok(in_scope, "1.4.3") else ()):
        pPr = p.find(f"{{{W}}}pPr")
        bg = "FFFFFF"
        if pPr is not None:
            shd = pPr.find(f"{{{W}}}shd")
            fill = shd.get(f"{{{W}}}fill") if shd is not None else None
            if fill and fill.lower() != "auto" and len(fill) == 6:
                bg = fill
        for run in p.iter(f"{{{W}}}r"):
            rPr = run.find(f"{{{W}}}rPr")
            color = rPr.find(f"{{{W}}}color") if rPr is not None else None
            fg = color.get(val_attr) if color is not None else None
            if not fg or fg.lower() == "auto" or len(fg) != 6:
                continue
            try:
                if _osx._contrast_ratio(bg, fg) >= 4.5:
                    continue
                # Hue-preserving fix: keep the run's colour, darken/lighten only to reach AA.
                new = _osx.min_contrast_recolor(fg, bg)
                ratio_before = _osx._contrast_ratio(bg, fg)
                ratio_after = _osx._contrast_ratio(bg, new)
            except Exception:
                continue
            color.set(val_attr, new)
            contrast_fixed += 1
            run_text = "".join(t.text or "" for t in run.iter(f"{{{W}}}t")).strip()
            _rec(diffs, "1.4.3", f"#{fg.upper()} on #{bg.upper()} ({ratio_before:.1f}:1 — fails AA)",
                 f"#{new} on #{bg.upper()} ({ratio_after:.1f}:1 — passes AA)",
                 located(f'text run "{run_text[:40]}"' if run_text else "low-contrast text run", run_token(run)),
                 locator=run_token(run))
    if contrast_fixed:
        applied.append(f"Recoloured {contrast_fixed} low-contrast run(s) to ≥4.5:1 · 1.4.3")

    # Form-field labels (3.3.2): an unlabelled content-control input (checkbox, date
    # picker, dropdown, combo box, picture) usually has its label sitting right next to
    # it as ordinary text — "Name:" in the run before it, or the left cell of its row.
    # Borrow that text deterministically into the control's <w:alias> so a screen reader
    # announces it; a genuinely BARE field (no adjacent text) is never labelled here — it
    # stays a finding and routes to review.
    #
    # THE SAME WRITE CLEARS 4.1.2, and until now only 3.3.2 was gated on and credited for it.
    # w:alias is simultaneously the visible prompt 3.3.2 asks for and the accessible NAME Word
    # exposes to assistive technology, which is 4.1.2's Name half — one attribute, two criteria,
    # as formats/docx/detectors/name_role_value.py says in its own docstring. Two consequences
    # were live before this change:
    #
    #   * A scan scoped to 4.1.2 but not 3.3.2 skipped the fixer entirely. The criterion was in
    #     scope, the fix was deterministic and available, and ACP left the bytes alone.
    #   * Nothing recorded the 4.1.2 clearance, so the report credited a criterion it had in
    #     fact fixed to the review lane instead.
    #
    # Verified rather than reasoned: running formats/docx/detectors/name_role_value.detect over
    # the same tree before and after this write takes an inline-caption field and a table-cell
    # field from 1 finding to 0, and leaves a bare field at 1. tests/test_docx_412_auto_lane.py
    # pins all three against the real detector, so a future change to either side breaks loudly.
    import form_labels as _fl
    _labels_in_scope = _sc_ok(in_scope, "3.3.2") or _sc_ok(in_scope, "4.1.2")
    labelled, bare = _fl.plan_labels(root) if _labels_in_scope else ([], 0)
    for sdt, label in labelled:
        _fl.set_alias(sdt, label)
        # An inline content control sits inside one paragraph, and that paragraph is where the
        # label was written. A block-level control wraps paragraphs instead of sitting in one:
        # it has no containing paragraph, so its location stays unrecorded.
        _holder = next(sdt.iterancestors(f"{{{W}}}p"), None)
        _where = paragraph_token(_holder) if _holder in paragraph_numbers else None
        for _sc, _after in (
            ("3.3.2", f'labelled “{label}” from the adjacent text'),
            ("4.1.2", f'accessible name “{label}” borrowed from the adjacent text'),
        ):
            _rec(diffs, _sc,
                 "form field had no label a screen reader could announce "
                 "(content control with no title/alias)",
                 _after,
                 "so assistive tech names the field when the user tabs into it",
                 locator=_where)
    if labelled:
        applied.append(f"Labelled {len(labelled)} form field(s) from adjacent text · 3.3.2, 4.1.2")
    if bare and skipped is not None:
        skipped.append(f"{bare} form field(s) have no adjacent label text to borrow — "
                       "needs a human/AI-supplied label (routed to review)")

    if applied:
        entries[name] = etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True)
    return applied


def _remediate_xlsx_structure(entries: dict, diffs=None, in_scope=None) -> list[str]:
    """Deterministic xlsx structural fixes: give every defined table a header row
    (headerRowCount>=1, WCAG 1.3.1) and unhide any row/column that is hidden but holds
    data (WCAG 1.3.2) — matching the analyser's TableHeaderRule and HiddenContentRule."""
    from lxml import etree

    SS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    applied: list[str] = []

    # Table headers: headerRowCount="0" -> "1" on every table-definition part.
    tbl_fixed = 0
    for name in (list(entries) if _sc_ok(in_scope, "1.3.1") else ()):
        if name.startswith("xl/tables/") and name.endswith(".xml"):
            root = etree.fromstring(entries[name])
            if root.get("headerRowCount") == "0":
                root.set("headerRowCount", "1")
                entries[name] = etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True)
                tbl_fixed += 1
                _rec(diffs, "1.3.1", 'table defined with headerRowCount="0" (no header row)',
                     'headerRowCount="1"',
                     "so the first row is announced as column headers",
                     locator=_part_locator(name))
    if tbl_fixed:
        applied.append(f"Gave {tbl_fixed} table(s) a header row · 1.3.1")

    # Hidden content: unhide rows/columns that are hidden AND contain data.
    hid_fixed = 0
    for name in (list(entries) if _sc_ok(in_scope, "1.3.2") else ()):
        if not (name.startswith("xl/worksheets/") and name.endswith(".xml")):
            continue
        root = etree.fromstring(entries[name])
        changed = False

        for row in root.iter(f"{{{SS}}}row"):
            if row.get("hidden") not in ("1", "true"):
                continue
            has_content = any(c.find(f"{{{SS}}}v") is not None or c.find(f"{{{SS}}}is") is not None
                              for c in row.findall(f"{{{SS}}}c"))
            if has_content:
                del row.attrib["hidden"]
                changed = True
                hid_fixed += 1

        # Column indices (1-based) that hold a value, so we only unhide columns with content.
        content_cols: set[int] = set()
        for c in root.iter(f"{{{SS}}}c"):
            if c.find(f"{{{SS}}}v") is None and c.find(f"{{{SS}}}is") is None:
                continue
            letters = "".join(ch for ch in (c.get("r") or "") if ch.isalpha())
            if letters:
                idx = 0
                for ch in letters:
                    idx = idx * 26 + (ord(ch.upper()) - 64)
                content_cols.add(idx)
        for col in root.iter(f"{{{SS}}}col"):
            if col.get("hidden") not in ("1", "true"):
                continue
            mn, mx = int(col.get("min", "0")), int(col.get("max", "0"))
            if any(mn <= i <= mx for i in content_cols):
                del col.attrib["hidden"]
                changed = True
                hid_fixed += 1

        if changed:
            entries[name] = etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True)
    if hid_fixed:
        applied.append(f"Unhid {hid_fixed} hidden row(s)/column(s) that contained data · 1.3.2")
        _rec(diffs, "1.3.2", f"{hid_fixed} row(s)/column(s) held data but were hidden — skipped by assistive tech",
             "hidden flag removed so the data is in the reading sequence",
             "only lines that actually contained values were unhidden")

    return applied


def alt_proposals_for_office(doc_bytes: bytes, ext: str, *, ai_enabled: bool = True,
                             scan_id: str | None = None, context_file: str = "",
                             guidance: str = "", include_grounded: bool = False, skip_locators=()) -> tuple[list, list]:
    """Assess-time WCAG 1.1.1: enumerate every unlabelled image and return
    (proposals, evidence) WITHOUT writing the file — so the review card can show a per-image
    thumbnail and, when a vision model is reachable, a PRE-FILLED AI description for each
    image, before any remediation runs.

    This reuses the exact alt logic remediate_office uses at fix time (_inject_descr:
    faithful-source first, decorative inference, the grounded/ungrounded vision split,
    evidence thumbnails), just run standalone with the rewritten XML discarded — so what the
    reviewer sees at assess time matches what remediation would do. Grounded (auto-applied)
    images don't produce a card proposal here (they clear deterministically at fix time);
    the ungrounded ones — the exact images that used to show only a text template — arrive
    with a real vision draft. Best-effort: ([], []) on any failure or unsupported ext.

    `guidance` is the org house-style block (ADR 0021). 1.1.1 was the ONE guidance-receiving
    criterion this function could not carry it to: handlers._scan_file passes `guidance=_g(...)`
    to all five other scan-time proposers, and this one had no parameter to pass it to — so the
    highest-volume drafts ACP writes, an alt text per image, were the only ones an org's house
    style never reached. It threads to the two vision prompts and stops there; see
    ai.describe_image_structured on why the transcription path must not receive it."""
    if (ext or "").lower().lstrip(".") not in ("docx", "pptx", "xlsx"):
        return [], []
    import io
    proposals: list = []
    evidence: list = []
    vision_enabled = False
    if ai_enabled:
        try:
            import ai as _ai
            vision_enabled = _ai.vision_is_available()
        except Exception:
            vision_enabled = False
    try:
        with zipfile.ZipFile(io.BytesIO(doc_bytes)) as z:
            entries = {n: z.read(n) for n in z.namelist()}
    except Exception:
        return [], []
    vision_budget = [_VISION_MAX_IMAGES]
    _throwaway_fixes: list = []
    for name in list(entries):
        for pat, tag, wrapper, captions in _ALT_TARGETS:
            if not pat.match(name):
                continue
            try:
                xml = entries[name].decode("utf-8")
            except UnicodeDecodeError:
                continue
            try:
                _inject_descr(xml, tag, pic_only_within=wrapper, captions=captions,
                              entries=entries, part_name=name, vision_enabled=vision_enabled,
                              context_file=context_file or name, scan_id=scan_id,
                              vision_budget=vision_budget, applied_fixes=_throwaway_fixes,
                              proposals=proposals, evidence=evidence,
                              guidance=guidance, skip_locators=skip_locators)  # rewritten XML discarded
            except Exception:
                continue
    if include_grounded:
        # Recovery discards the XML rewrite. Grounded captions are proposals here,
        # rather than pretending the throwaway document was saved or verified.
        for fix in _throwaway_fixes:
            if fix.get('locator') and fix.get('value'):
                proposals.append({'locator': fix['locator'], 'before': '(no alt text)',
                    'proposed_value': fix['value'], 'rationale': 'Recovered image description; save and verify through the existing review workflow.',
                    'source': fix.get('source'), 'thumb': fix.get('thumb'),
                    'model': fix.get('model'), 'model_call_id': fix.get('model_call_id')})
    import hashlib
    for proposal in proposals:
        if proposal.get('quality_source_review'):
            proposal['source_sha256'] = hashlib.sha256(doc_bytes).hexdigest()
    return proposals, evidence


# How many assisted findings one document may draft. A bound, not a quality judgement: each
# draft is a model call, and a policy manual with 200 "click here" links would otherwise turn a
# remediation job into a several-minute stall with no signal that it was drafting rather than
# hung. Mirrors _VISION_MAX_IMAGES, and like it, the overflow is REPORTED rather than silent.
_ASSISTED_MAX_DRAFTS = 12


def _draft_docx_assisted(entries: dict, path: Path, proposals: list | None, *,
                         scan_id: str | None = None, in_scope=None) -> int:
    """Draft — never write — fixes for the .docx criteria ACP assesses but cannot auto-apply.

    2.4.4 vague link text · 1.3.3 sensory-only instructions · 3.1.2 unmarked foreign passages.

    WHY THIS EXISTS. These three are `assisted` on .docx: detected during assessment, and until
    now producing nothing at all during remediation. The appliers exist (apply_link_text,
    apply_text_values) but run only from handlers._apply_one_value_kind on values a human has
    ALREADY approved — so a bulk remediation left them untouched even when the model would have
    drafted something good, and the reviewer got an empty card to fill by hand. Measured across
    15 documents and 3 text models: zero drafts, zero proposals, identical for every model.

    WHY PROPOSALS AND NOT FIXES. The posture is unchanged and deliberate — draft, approve, apply,
    never auto-write. Rewriting a link's visible text or a clinical instruction is a change to
    what the document SAYS, not to how it is marked up, and on a hospital's document that is a
    human's call. This closes the gap between "detected" and "a reviewer has something to
    approve", which is the whole distance the assisted lane was missing.

    Detection is the SAME code assessment uses (office_structure._is_vague_link_text,
    textchecks.detect_sensory / detect_language_parts) rather than a second implementation, so a
    proposal cannot be offered for something the scan does not report — the failure that would
    make a reviewer approve a fix for a finding they never saw.

    Returns the number of drafts produced. Best-effort throughout: no draft is worth failing a
    remediation job for.
    """
    if proposals is None:
        return 0
    import ai as _ai
    import proposals as _prop
    # model_is_available(), NOT is_available(). The latter only proves /api/tags answered — a
    # text-only or wrong-model Ollama passes it and every draft then comes back empty, which
    # reads as a model that cannot do the task. tests/test_textmodel_gate.py enforces this as a
    # drift guard across the proposal modules, and caught this exact line.
    if not _ai.model_is_available():
        return 0

    doc = entries.get("word/document.xml")
    if not doc:
        return 0
    try:
        xml = doc.decode("utf-8")
    except (UnicodeDecodeError, AttributeError):
        return 0

    import office_structure as _os
    import textchecks as _tc

    made = 0

    def _draft(sc: str, rule_name: str, detail: str, before: str, locator: str, source: str):
        nonlocal made
        if made >= _ASSISTED_MAX_DRAFTS or not _sc_ok(in_scope, sc):
            return
        # The single funnel for every assisted draft, so one line here covers 2.4.4, 1.3.3 and
        # 3.1.2 rather than three that can drift. Published before suggest_fix, which is the
        # slow call — a line written after it names work already finished.
        _act_record(scan_id, file=path.name, sc=sc,
                    action=f"drafting a fix for {rule_name}",
                    detail="draft only — nothing is written without approval",
                    phase="remediating")
        out = _ai.suggest_fix(sc, rule_name, "A", path.name, detail=detail)
        value = (out or {}).get("suggestion")
        if not value:
            return
        proposals.append(_prop.proposal(
            locator=locator, before=before, proposed_value=value,
            rationale=f"drafted from the surrounding text by {out.get('model', 'the local model')}",
            source=source, sc=sc,
            model=out.get("model"),
            # Why a human, stated rather than implied — the same gap the 1.1.1 cards had. These
            # criteria are assessed but never auto-applied: the draft rewrites the AUTHOR'S prose,
            # and only the author knows what the sentence was for. That is a different reason from
            # 1.1.1's ("nothing corroborates it") and the card should say which one applies.
            why_review=(f"ACP can detect this {rule_name} problem but cannot fix it "
                        "automatically — the correction rewrites your own wording, and only "
                        "you know what the original was meant to say. The draft below is a "
                        "starting point, not a decision."),
            context=detail))
        made += 1

    # 2.4.4 — vague link text. Resolved through the relationship so the draft can name the
    # DESTINATION, which is the whole point of the criterion; a rewrite that cannot see the
    # target is guessing from the same words the reader already found unhelpful.
    rels_xml = (entries.get("word/_rels/document.xml.rels") or b"").decode("utf-8", "ignore")
    hrefs = dict(re.findall(r'Id="(rId\w+)"[^>]*Target="([^"]+)"', rels_xml))
    for rid, inner in _os._HYPERLINK.findall(xml):          # noqa: SLF001 — the assessment regex
        text = "".join(_os._WT.findall(inner)).strip()      # noqa: SLF001
        if not text or not _os._is_vague_link_text(text):   # noqa: SLF001
            continue
        href = hrefs.get(rid, "")
        _draft("2.4.4", "Link Purpose (In Context)",
               f'link text is "{text}"' + (f'; it points to {href}' if href else ""),
               before=text, locator=f"word/document.xml#{rid}",
               source="AI draft from the link's destination and surrounding text")

    # 1.3.3 and 3.1.2 read the document's visible text, which is what their detectors take.
    body = " ".join(_os._WT.findall(xml)).strip()           # noqa: SLF001
    if body:
        # `detail` ALREADY reads "instruction relies on shape/position alone: “…”" — the
        # detector writes the framing, not just the sentence. Prefixing it again handed the
        # model that phrase twice and produced a draft that echoed the document instead of
        # rewriting the instruction. Pass it through, and quote out the offending sentence for
        # the `before` the reviewer sees.
        for f in (_tc.detect_sensory(body) or [])[:_ASSISTED_MAX_DRAFTS]:
            detail = str(f.get("detail") or "")[:300]
            quoted = re.search(r"[“\"']([^”\"']{4,200})[”\"']", detail)
            _draft("1.3.3", "Sensory Characteristics", detail,
                   before=quoted.group(1) if quoted else detail,
                   locator="word/document.xml#sensory",
                   source="AI draft replacing the sensory-only reference")
        for f in (_tc.detect_language_parts(body) or [])[:_ASSISTED_MAX_DRAFTS]:
            detail = str(f.get("detail") or "")[:300]
            _draft("3.1.2", "Language of Parts", detail,
                   before=detail, locator="word/document.xml#lang-part",
                   source="AI draft marking the passage's language")

        # 1.3.2 — floating text boxes and frames, read at their anchor rather than their visual
        # position. EXPLAIN-ONLY, and that is the honest shape: the fix is to re-flow the
        # document so the anchor order matches the visual order, which cannot be proposed as a
        # VALUE — there is no string to approve. A card offering a fill-in field here would be
        # asking a reviewer to type something that has nowhere to go.
        #
        # No model call: the finding already names what is wrong and how many, and a model asked
        # to "fix reading order" from text alone would invent a layout it cannot see.
        floating = sum(1 for inner in _os._TXBX_CONTENT.findall(xml)         # noqa: SLF001
                       if "".join(_os._WT.findall(inner)).strip())          # noqa: SLF001
        floating += len(_os._FRAMEPR.findall(xml))                          # noqa: SLF001
        if floating and _sc_ok(in_scope, "1.3.2") and made < _ASSISTED_MAX_DRAFTS:
            proposals.append(_prop.proposal(
                locator="word/document.xml#floating-text",
                before=f"{floating} floating text box(es)/frame(s)",
                proposed_value=(
                    "Move this content into the main document flow, in the order it should be "
                    "read. A screen reader announces a floating box at its ANCHOR point, which "
                    "need not match where it appears on the page."),
                rationale="the anchor order and the visual order can differ; only a person "
                          "reading the layout can say what the intended sequence is",
                source="structural finding (no model — the layout is not in the text)",
                explain_only=True, sc="1.3.2"))
            made += 1

    # Finding details are display excerpts, not recovered content. Reuse the
    # shared image proposer so complete visible OCR, locator and review evidence
    # remain bound to the actual image. This is still a draft, never an approval.
    try:
        if made < _ASSISTED_MAX_DRAFTS and _sc_ok(in_scope, "1.4.5"):
            import ocr as _ocr
            visible, _ = _ocr._embedded_images_and_total(path, path.suffix.lower(), visible_word=True)
            # Match the detector's bounded, visible Word membership. Unused
            # media and unsupported placement geometry cannot create findings.
            visible_locators = {f"image {image.media_number}" for image in visible if image.placements}
            for draft in _prop.propose_images_of_text(path, path.suffix.lower(), ai_enabled=False):
                if made >= _ASSISTED_MAX_DRAFTS:
                    break
                if draft.get("sc") != "1.4.5" or draft.get("locator") not in visible_locators:
                    continue
                # Preserve this lane's Essential exception for charts/diagrams.
                if _ocr._looks_like_chart(draft.get("proposed_value") or ""):
                    continue
                proposals.append(draft)
                made += 1
    except Exception:
        swallowed("remediate_office._draft_docx_assisted: drafting OCR image text failed", scan_id)
    return made


def _draft_pptx_assisted(entries: dict, path: Path, proposals: list | None, *,
                         scan_id: str | None = None, in_scope=None) -> int:
    """Draft — never write — 3.1.2 language-of-parts fixes for a pptx file.

    Mirrors _draft_docx_assisted for the docx lane: reads slide text, runs the same
    detect_language_parts detector the assessment uses, and calls suggest_fix for each
    unmarked foreign passage. Results go into `proposals` as HITL cards a reviewer can
    approve; nothing is written until they do.

    The write-back path (apply_text_values._pptx_set_lang) is already wired and has been
    since LANGUAGE_EXTS included pptx; this function supplies the proposals that were
    previously absent, closing the detect → draft → approve → apply loop.

    Returns the number of drafts produced. Best-effort: no draft is worth failing a job.
    """
    if proposals is None:
        return 0
    import ai as _ai
    import proposals as _prop
    if not _ai.model_is_available():
        return 0

    slides = sorted((n for n in entries if re.fullmatch(r"ppt/slides/slide\d+\.xml", n)),
                    key=lambda s: int(re.search(r"(\d+)", s).group()))
    if not slides:
        return 0

    import textchecks as _tc

    made = 0

    def _draft(sc: str, rule_name: str, detail: str, before: str, locator: str, source: str):
        nonlocal made
        if made >= _ASSISTED_MAX_DRAFTS or not _sc_ok(in_scope, sc):
            return
        _act_record(scan_id, file=path.name, sc=sc,
                    action=f"drafting a fix for {rule_name}",
                    detail="draft only — nothing is written without approval",
                    phase="remediating")
        out = _ai.suggest_fix(sc, rule_name, "A", path.name, detail=detail)
        value = (out or {}).get("suggestion")
        if not value:
            return
        proposals.append(_prop.proposal(
            locator=locator, before=before, proposed_value=value,
            rationale=f"drafted from the slide text by {out.get('model', 'the local model')}",
            source=source, sc=sc,
            model=out.get("model"),
            why_review=(f"ACP can detect this {rule_name} problem but cannot fix it "
                        "automatically — the correction rewrites your own wording, and only "
                        "you know what the original was meant to say. The draft below is a "
                        "starting point, not a decision."),
            context=detail))
        made += 1

    try:
        # Concatenate visible text from all slides as a single body — the same view
        # detect_language_parts takes in the assessment.
        body_parts = []
        for slide_name in slides:
            raw = entries.get(slide_name)
            if not raw:
                continue
            try:
                xml = raw.decode("utf-8")
            except (UnicodeDecodeError, AttributeError):
                continue
            body_parts.append(" ".join(_A_T.findall(xml)))
        body = " ".join(body_parts).strip()
        if body:
            for f in (_tc.detect_language_parts(body) or [])[:_ASSISTED_MAX_DRAFTS]:
                detail = str(f.get("detail") or "")[:300]
                _draft("3.1.2", "Language of Parts", detail,
                       before=detail, locator="ppt/slides#lang-part",
                       source="AI draft marking the passage's language")
    except Exception:
        swallowed("remediate_office._draft_pptx_assisted: drafting pptx 3.1.2 proposals "
                  "failed", scan_id)

    return made


def remediate_office(path: Path, *, lang: str = "en-US", ai_enabled: bool = True,
                     scan_id: str | None = None, applied_fixes: list | None = None,
                     proposals: list | None = None, evidence: list | None = None, diffs=None,
                     in_scope=None):
    """Apply deterministic Office accessibility fixes to a copy of the file.

    ai_enabled — when True and a vision (llava-class) Ollama model is reachable,
    unlabelled images get genuine vision-generated alt text; otherwise only faithful
    sources are used and the rest defer to human review (AI-off degrades gracefully).
    scan_id threads the vision calls into that scan's Langfuse session.

    evidence — collects {locator, thumb} for each image deferred to a human, so the review
    card can show the picture it is asking someone to describe. Filled whether or not AI ran.

    Returns (fixed_path, applied, skipped). fixed_path is None if nothing applied.
    """
    try:
        with zipfile.ZipFile(path) as zin:
            entries = {n: zin.read(n) for n in zin.namelist()}
    except Exception as e:
        return None, [], [f"could not open Office file: {type(e).__name__}"]

    applied: list[str] = []
    _core_skip: list[str] = []
    # Language + title live in docProps/core.xml. Some files (notably certain xlsx exports) omit that
    # part — set lang/title only when it exists, but STILL run every other fix below (chart alt, image
    # alt, structure). A missing core part must not abort the whole remediation — it used to return
    # with nothing done, so such a file got NO fixes at all.
    has_core = _CORE in entries
    root = None
    if has_core:
        for pfx, uri in _NS.items():
            ET.register_namespace(pfx, uri)
        root = ET.fromstring(entries[_CORE].decode("utf-8"))

        def _ensure(tag_uri: str, tag: str, value: str, label: str, sc: str, before: str):
            # Gated INSIDE the helper, not at its two call sites: a future third caller then
            # inherits the scope check instead of needing to remember it.
            if not _sc_ok(in_scope, sc):
                return
            el = root.find(f"{{{tag_uri}}}{tag}")
            if el is None or not (el.text or "").strip():
                if el is None:
                    el = ET.SubElement(root, f"{{{tag_uri}}}{tag}")
                el.text = value
                applied.append(label.format(value=value))
                _rec(diffs, sc, before, value, f"read by assistive tech from docProps/core.xml (dc:{tag})")

        _ensure(_DC, "language", lang, "Set document language to '{value}'",
                "3.1.1", "(no language declared — screen readers guessed pronunciation)")
        # A meaningful title beats an empty one; derive a readable default from the name.
        title = path.stem.replace("-", " ").replace("_", " ").strip() or "Document"
        _ensure(_DC, "title", title, "Set document title to '{value}'",
                "2.4.2", "(no document title — could not be identified in assistive tech)")
    else:
        _core_skip.append("no OPC core-properties part — language/title not set")

    # Image alt text (WCAG 1.1.1) — faithful source first, then a genuine vision
    # description when AI is on and a vision model is reachable; the rest defer to review.
    vision_enabled = False
    if ai_enabled:
        try:
            import ai as _ai
            vision_enabled = _ai.vision_is_available()
        except Exception:
            vision_enabled = False
    skipped: list[str] = list(_core_skip)
    # One line, per stage, naming the file and the criterion being worked. `scan_id` is None for
    # every offline caller (benchmarks, CLI, tests), and activity.record no-ops on that — so this
    # costs nothing outside a real scan and cannot fail one inside it.
    import activity as _act
    _act.record(scan_id, file=path.name, sc="1.1.1", action="describing images",
                phase="remediating")
    # Native charts (1.1.1) FIRST — before _fix_image_alt (which would otherwise put a weak
    # name-based descr on the chart shape, which we then wouldn't overwrite) and before any structural
    # pass re-encodes the XML. A native chart has NO image bytes, so vision can't help it; its data is
    # in the chart part (and, for xlsx, the cells it references), so we write an EXACT, deterministic
    # description from the real series/values — no model, no confabulation. Fills only a chart with no
    # real alt; runs for pptx/xlsx/docx.
    try:
        import chart_data as _chart
        _chart_edits = (_chart.chart_descr_edits(entries, path.suffix.lower())
                        if _sc_ok(in_scope, "1.1.1") else {})
        for name, (new_xml, alts) in _chart_edits.items():
            entries[name] = new_xml
            for a in alts:
                applied.append(f"Alt text \"{a[:60]}\" set from the chart's own data · 1.1.1")
                _rec(diffs, "1.1.1", "(native chart had no alt text)", a,
                     "read from the chart's embedded data — exact values, no model",
                     locator=_part_locator(name))
    except Exception:
        skipped.append("native-chart alt text could not be applied")

    if _sc_ok(in_scope, "1.1.1"):
        alt_applied, alt_deferred = _fix_image_alt(
            entries, vision_enabled=vision_enabled, context_file=path.name, scan_id=scan_id,
            applied_fixes=applied_fixes, proposals=proposals, evidence=evidence, diffs=diffs)
    else:
        alt_applied, alt_deferred = [], 0
    applied.extend(alt_applied)
    if alt_deferred:
        skipped.append(f"{alt_deferred} image(s) lack a faithful alt source — "
                       "needs human alt text (routed to review)")

    # docx structural fixes (table header rows + heading outline) — WCAG 1.3.1.
    if path.suffix.lower() == ".docx":
        _act.record(scan_id, file=path.name, sc="1.3.1",
                    action="marking table headers and the heading outline", phase="remediating")
        try:
            applied.extend(_remediate_docx_structure(entries, diffs, skipped, in_scope))
        except Exception:
            skipped.append("docx structural fixes (table headers / heading outline) could not be applied")
        # Assisted criteria — DRAFTED for review, never written. See the function's docstring.
        if ai_enabled:
            _act.record(scan_id, file=path.name, sc="2.4.4",
                        action="drafting link, sensory and language fixes for review",
                        detail="drafts only — nothing is written without approval",
                        phase="remediating")
            try:
                n = _draft_docx_assisted(entries, path, proposals, scan_id=scan_id,
                                         in_scope=in_scope)
                if n:
                    skipped.append(f"{n} link/sensory/language finding(s) drafted for review "
                                   "(2.4.4 / 1.3.3 / 3.1.2)")
            except Exception:
                skipped.append("assisted-criteria drafts could not be produced")

    # pptx-only structural fixes that need the slide XML (title / contrast / reading order).
    if path.suffix.lower() == ".pptx":
        try:
            applied.extend(_remediate_pptx_slides(entries, diffs, in_scope))
        except Exception:
            skipped.append("slide-level pptx fixes (title/contrast/reading order) could not be applied")
        # Assisted criteria — DRAFTED for review, never written.  Currently covers 3.1.2
        # (language of parts): detect_language_parts runs over all slide text and suggest_fix
        # drafts a correction for each unmarked foreign passage. apply_text_values._pptx_set_lang
        # is the write-back once a reviewer approves.
        if ai_enabled:
            try:
                n = _draft_pptx_assisted(entries, path, proposals, scan_id=scan_id,
                                         in_scope=in_scope)
                if n:
                    skipped.append(f"{n} language-of-parts finding(s) drafted for review (3.1.2)")
            except Exception:
                skipped.append("pptx assisted-criteria drafts could not be produced")

    # xlsx contrast recolour (1.4.3 / 1.4.6) — clone offending fonts to reach the
    # luma-diff the detector requires (mirrors the pptx contrast fix).
    if path.suffix.lower() == ".xlsx":
        try:
            if _sc_ok(in_scope, "1.4.3"):
                applied.extend(_remediate_xlsx_contrast(entries, diffs))
        except Exception:
            skipped.append("xlsx contrast recolour could not be applied")
        try:
            applied.extend(_remediate_xlsx_structure(entries, diffs, in_scope))
        except Exception:
            skipped.append("xlsx structural fixes (table headers / hidden content) could not be applied")

    if not applied:
        return None, [], skipped or ["language and title already set"]

    # Also stamp the STANDARD (visible) core properties: the remediation date as the Modified date +
    # "Last saved by" — so it shows on the General tab, not only Custom. Only when a core part exists.
    if root is not None:
        _DCTERMS, _XSI = _NS["dcterms"], _NS["xsi"]
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        lmb = root.find(f"{{{_CP}}}lastModifiedBy")
        if lmb is None:
            lmb = ET.SubElement(root, f"{{{_CP}}}lastModifiedBy")
        lmb.text = TOOL
        mod = root.find(f"{{{_DCTERMS}}}modified")
        if mod is None:
            mod = ET.SubElement(root, f"{{{_DCTERMS}}}modified")
        mod.set(f"{{{_XSI}}}type", "dcterms:W3CDTF")
        mod.text = ts
        new_core = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\r\n'
                    + ET.tostring(root, encoding="unicode"))
        entries[_CORE] = new_core.encode("utf-8")

    # Tamper-evident provenance in the Custom-properties tab (who/what/when/standard).
    _stamp_provenance(entries, applied)

    out_path = path.with_name(f"remediated-{path.name}")
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as zout:
        for name, data in entries.items():           # preserves archive order
            zout.writestr(name, data)
    return out_path, applied, skipped

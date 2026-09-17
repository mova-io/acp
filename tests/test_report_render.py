"""The server report renderer (api/report_render.py, POST /scans/{sid}/report-render).

Every assertion here is about the PDF a reader receives — the structure tree, the outline, the
embedded fonts, the extracted text — or about what the route refuses. None reads the renderer's
source. "The HTML contains <h2>" is not evidence that a screen reader meets a heading; the /H2
element in the StructTreeRoot is.

What this does NOT establish, and the PDF says so itself ("About this PDF"): PDF/UA conformance.
That needs veraPDF (skipped when absent, see tests/verapdf.py), PAC 2024 and a screen-reader pass.
"""
from __future__ import annotations

import base64
import io
import json
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path

import pytest

ACP = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ACP / "api"))

pikepdf = pytest.importorskip("pikepdf")
pytest.importorskip("weasyprint")

import report_render as rr  # noqa: E402
from verapdf import NO_VERAPDF, VERAPDF_OK, validate  # noqa: E402

PDFTOTEXT = next((p for p in ("/opt/homebrew/bin/pdftotext", "/usr/bin/pdftotext") if Path(p).exists()), None)


def png(w=240, h=140, color=(70, 50, 90)):
    from PIL import Image
    img = Image.new("RGB", (w, h), (245, 245, 250))
    img.paste(color, (30, 30, w - 30, h - 30))
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


def jpeg_bytes(w=60, h=40):
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (w, h), (10, 120, 30)).save(buf, "JPEG")
    return buf.getvalue()


def change(i, **over):
    card = {
        "k": "changeCard", "id": f"report.docx::SC_1_1_1::{i}",
        "title": "1.1.1 · Non-text Content", "criterion": "1.1.1", "criterionName": "Non-text Content",
        "location": {"label": f"Page {i + 2} · Figure {i + 1}", "page": i + 2, "href": None},
        "before": "(no alt text)", "after": "Revenue by region <script>alert(1)</script>",
        "beforeTruncated": False, "afterTruncated": False, "fullRef": None,
        "reason": "Set from the adjacent caption",
        "image": None, "imageStatus": "unavailable",
        "technical": {"status": "pending", "detail": "Edit saved; re-validation has not been recorded."},
        "human": {"status": "pending", "reviewer": None, "at": None, "note": None,
                  "boundSha256": None, "currentSha256": None},
        "responseOptions": ["Accept", "Edit", "Reject", "Unable to verify"],
        "responseNotice": "Marking a printed or downloaded copy does not record a decision.",
    }
    card.update(over)
    return card


def finding(i, **over):
    card = {"k": "findingCard", "id": f"f-{i}", "rank": i + 1, "priority": "high", "severity": "CRITICAL",
            "title": "1.4.3 · Contrast (Minimum)", "criterion": "1.4.3", "criterionName": "Contrast (Minimum)",
            "location": None, "description": "Grey text on white is 2.9:1",
            "impact": "Readers with low vision may be unable to read it.",
            "steps": ["Select the text", "Home → Font Color → a darker colour"], "owner": None,
            "recheck": "Re-run the ACP assessment on the corrected copy.", "status": "open"}
    card.update(over)
    return card


def model(blocks=None, **over):
    base = {
        "docTitle": "Accessibility review — report.docx", "lang": "en-US", "mode": "reviewer", "kind": "file",
        "identity": {"scanId": "CLIENT-SCAN", "file": "client.docx", "sourceSha256": "f" * 64,
                     "correctedSha256": "9" * 64, "artifactVersion": "v3", "targetLevel": "WCAG 2.1 AA",
                     "generatedAt": "1999-01-01", "platformVersion": "client-version"},
        "cover": {"title": "Accessibility review — report.docx", "subtitle": "Reviewer packet", "meta": ["WCAG 2.1 Level AA"]},
        "blocks": blocks if blocks is not None else [
            {"k": "heading", "text": "Decision summary"},
            {"k": "decisionSummary", "caption": "Where this document stands", "items": [
                {"key": "documentsAssessed", "label": "Documents assessed", "value": 1},
                {"key": "editsSaved", "label": "Edits saved", "value": 2},
                {"key": "findingsVerifiedResolved", "label": "Findings verified resolved", "value": 0},
                {"key": "findingsRemaining", "label": "Findings remaining", "value": 1},
                {"key": "humanChecksPending", "label": "Human checks pending", "value": 2},
                {"key": "checksNotPerformed", "label": "Checks not performed", "value": None}]},
            {"k": "stageStrip", "items": [
                {"key": "savedEdits", "label": "Saved edits", "value": 2, "status": "done", "detail": ""},
                {"key": "publication", "label": "Publication", "value": None, "status": "unknown", "detail": ""}]},
            {"k": "comparison", "status": "unknown", "reason": "No earlier snapshot.", "previous": None,
             "resolved": [], "introduced": [], "persisting": None},
            {"k": "barChart", "items": [{"label": "1.4.3 Contrast (Minimum)", "value": 3}]},
            {"k": "heading", "text": "Changes to confirm"},
            {"k": "text", "text": "Tick ✓ each change, then record the decision → in ACP."},
            {"k": "heading", "text": "Page-level changes", "level": 2},
            change(0, image={"src": png(), "alt": "Page 2 after the edit", "caption": "Page 2"}, imageStatus="available"),
            change(1, location={"label": "Slide 4", "href": "javascript:alert(1)"}),
            change(2, location=None, reason=None),
            {"k": "heading", "text": "Remaining work"},
            finding(0),
            {"k": "link", "text": "Open this file in ACP", "href": "/scans/s1/files/report.docx"},
            {"k": "link", "text": "Unsafe link text", "href": "javascript:alert(document.cookie)"},
            {"k": "link", "text": "WCAG 2.1", "href": "https://www.w3.org/TR/WCAG21/"},
            {"k": "pageBreak"},
            {"k": "heading", "text": "Evidence appendix"},
            {"k": "appendixTable", "id": "remediation_diff", "complete": False, "totalRecords": 40,
             "caption": "Saved changes", "headers": ["Id", "Before", "After"],
             "rows": [[f"id-{i}", "before", "after"] for i in range(3)]},
        ],
    }
    base.update(over)
    return base


RECORD = {"checksum": "0123abcd-md5-from-drive", "corrected_sha256": "c" * 64}


def identity(kind="file", file="report.docx", record=RECORD, client=None):
    return rr.server_identity(scan_id="scan-1", kind=kind, file=file, record=record,
                              client_identity=client or model()["identity"], platform_version="2026.9.17.1")


def render(m=None, mode="reviewer", base_url="https://acp.example.com", kind="file", file="report.docx"):
    req = rr.validate_request({"kind": kind, "file": file, "mode": mode, "model": m or model()})
    return rr.render_pdf(req["model"], identity(kind=kind, file=file), mode, base_url)


def pdftext(path_or_bytes, *args) -> str:
    if PDFTOTEXT is None:
        pytest.skip("pdftotext (poppler) is not installed")
    data = path_or_bytes if isinstance(path_or_bytes, bytes) else Path(path_or_bytes).read_bytes()
    out = subprocess.run([PDFTOTEXT, *args, "-", "-"], input=data, capture_output=True, check=True)
    return out.stdout.decode("utf-8")


@pytest.fixture(scope="module")
def pdf_bytes():
    return render()


@pytest.fixture(scope="module")
def pdf(pdf_bytes):
    with pikepdf.open(io.BytesIO(pdf_bytes)) as doc:
        yield doc


def walk(doc):
    tags, figures, seen = Counter(), [], set()

    def visit(node):
        if isinstance(node, pikepdf.Array):
            for kid in node:
                visit(kid)
            return
        if not isinstance(node, pikepdf.Dictionary):
            return
        if node.objgen != (0, 0):
            if node.objgen in seen:
                return
            seen.add(node.objgen)
        role = node.get("/S")
        if role is not None:
            tags[str(role).lstrip("/")] += 1
            if str(role) == "/Figure":
                figures.append(str(node.get("/Alt")) if node.get("/Alt") is not None else None)
        if node.get("/K") is not None:
            visit(node.get("/K"))

    visit(doc.Root.StructTreeRoot.get("/K"))
    return tags, figures


# ── document-level structure ─────────────────────────────────────────────────────────────────

def test_catalog_declares_language_title_and_marked_content(pdf):
    root = pdf.Root
    assert str(root.Lang) == "en-US"
    assert bool(root.MarkInfo.Marked) is True
    assert "/StructTreeRoot" in root
    assert bool(root.ViewerPreferences.DisplayDocTitle) is True
    assert "Accessibility review" in str(pdf.open_metadata().get("dc:title"))


def test_structure_tree_has_real_semantics(pdf):
    tags, figures = walk(pdf)
    assert tags["H1"] == 1
    assert tags["H2"] >= 4 and tags["H3"] >= 1
    assert tags["Table"] >= 3 and tags["THead"] >= 2 and tags["TH"] >= 10 and tags["TD"] >= 5
    assert tags["L"] >= 1 and tags["LI"] >= 2
    assert tags["Link"] >= 2
    # The logo and the change preview, each with a real text alternative.
    assert len(figures) == 2 and all(alt and len(alt) > 3 for alt in figures), figures
    assert "Page 2 after the edit" in figures


def test_bookmarks_follow_the_heading_outline(pdf):
    def tree(items):
        return [(item.title, tree(item.children)) for item in items]

    with pdf.open_outline() as outline:
        outline_tree = tree(outline.root)
    top = [title for title, _ in outline_tree]
    assert top[:1] == ["Accessibility review — report.docx"]
    for title in ("Decision summary", "Changes to confirm", "Remaining work", "Evidence appendix", "About this PDF"):
        assert title in top, top
    # A level-2 model heading nests under its section; the change cards nest under it and are
    # told apart by their location, not just their (shared) criterion.
    changes = dict(outline_tree)["Changes to confirm"]
    assert [t for t, _ in changes] == ["Page-level changes"]
    cards = [t for t, _ in changes[0][1]]
    assert cards == ["1.1.1 · Non-text Content · Page 2 · Figure 1",
                     "1.1.1 · Non-text Content · Slide 4",
                     "1.1.1 · Non-text Content"], cards


def test_fonts_are_embedded_dejavu(pdf):
    faces = {}
    for obj in pdf.objects:
        if isinstance(obj, pikepdf.Dictionary) and obj.get("/Type") == "/FontDescriptor":
            faces[str(obj.get("/FontName")).split("+")[-1]] = "/FontFile2" in obj
    assert faces and all(faces.values()), faces
    assert any("DejaVu" in name for name in faces), faces


def test_symbols_page_numbers_and_running_header_are_real_text(pdf_bytes):
    text = pdftext(pdf_bytes)
    assert "Tick ✓ each change, then record the decision → in ACP." in text
    assert "☐ Accept" in text and "☐ Unable to verify" in text
    pages = len(pikepdf.open(io.BytesIO(pdf_bytes)).pages)
    for n in range(1, pages + 1):
        assert f"Page {n} of {pages}" in text
    first = pdftext(pdf_bytes, "-f", "1", "-l", "1")
    assert "report.docx" in first.splitlines()[0]
    assert "Corrected copy SHA-256 cccccccccccc" in first


def test_explicit_missing_evidence_wording(pdf_bytes):
    text = pdftext(pdf_bytes)
    for phrase in ("Visual preview not available", "Location not recorded", "Reason not recorded",
                   "Not recorded", "Partial: 3 of 40 records"):
        assert phrase in text, phrase
    assert "Marking a printed or downloaded copy does not record a decision." in text


def test_the_pdf_does_not_claim_pdf_ua_conformance(pdf_bytes):
    text = " ".join(pdftext(pdf_bytes).split())
    assert "it is not a PDF/UA conformance claim" in text
    assert "screen-reader review" in text
    assert not re.search(r"PDF/UA[- ]?1? (conformant|compliant)", text)
    assert "certified" not in text.lower()


# ── identity is the server's ─────────────────────────────────────────────────────────────────

def test_server_identity_overwrites_the_client(pdf_bytes):
    text = pdftext(pdf_bytes)
    assert "scan-1" in text and "CLIENT-SCAN" not in text
    assert "client.docx" not in text and "client-version" not in text and "1999-01-01" not in text
    assert "f" * 64 not in text                       # the client's claimed source hash
    assert "0123abcd-md5-from-drive" in text          # what the store actually recorded …
    assert "as recorded by the source system" in text  # … labelled as not-a-SHA-256
    assert "2026.9.17.1" in text


def test_source_checksum_is_only_called_sha256_when_it_is_one():
    md5 = identity(record={"checksum": "d41d8cd98f00b204e9800998ecf8427e"})
    assert md5["sourceSha256"] is None and md5["sourceChecksumKind"] == "as recorded by the source system"
    sha = identity(record={"checksum": "sha256:" + "AB" * 32})
    assert sha["sourceSha256"] == "ab" * 32 and sha["sourceChecksumKind"] == "SHA-256"
    none = identity(record={})
    assert none["sourceChecksum"] is None and none["correctedSha256"] is None


def test_a_model_identity_table_is_replaced_by_the_server_one():
    blocks = [{"k": "heading", "text": "Document identity"},
              {"k": "table", "role": "identity", "caption": "Document and report identity", "headers": ["Field", "Value"],
               "rows": [["Source SHA-256", "Not recorded"], ["Platform version", "client-version"]]}]
    html = rr.render_html(rr.validate_request({"kind": "file", "file": "report.docx", "mode": "summary",
                                               "model": model(blocks)})["model"], identity(), "summary")
    assert html.count('class="identity"') == 1          # the cover does not print a second one
    assert "client-version" not in html and "0123abcd-md5-from-drive" in html


# ── untrusted input ──────────────────────────────────────────────────────────────────────────

def test_markup_in_model_strings_is_text(pdf_bytes):
    html = rr.render_html(model(), identity(), "reviewer", "https://acp.example.com")
    assert "<script" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert "<script>alert(1)</script>" in pdftext(pdf_bytes)


def test_only_https_and_app_relative_links_survive(pdf):
    uris = []
    for page in pdf.pages:
        for annot in page.get("/Annots") or []:
            action = annot.get("/A")
            if action is not None and action.get("/URI") is not None:
                uris.append(str(action.URI))
    assert "https://www.w3.org/TR/WCAG21/" in uris
    assert "https://acp.example.com/scans/s1/files/report.docx" in uris
    assert not any("javascript" in u.lower() for u in uris), uris


def test_app_relative_link_without_a_public_url_is_printed_not_linked():
    html = rr.render_html(model(), identity(), "reviewer", base_url=None)
    assert 'href="/scans' not in html
    assert "Open this file in ACP (in ACP: /scans/s1/files/report.docx)" in html
    assert "javascript" not in html.split("<main>")[1].split("Unsafe link text")[0][-200:]


@pytest.mark.parametrize("href", ["javascript:alert(1)", "//evil.example/x", "http://example.com", "data:text/html,x",
                                  "https://user:pw@example.com/", "https://exa mple.com", "/ok\"onmouseover=x"])
def test_safe_href_refuses(href):
    assert rr.safe_href(href, "https://acp.example.com") is None


def _refused(body):
    with pytest.raises(rr.ReportInputError) as exc:
        rr.validate_request(body)
    return exc.value.status


def _body(blocks, **over):
    return {"kind": "file", "file": "report.docx", "mode": "reviewer", "model": model(blocks), **over}


@pytest.mark.parametrize("src", [
    "data:text/html;base64," + base64.b64encode(b"<script>alert(1)</script>").decode(),
    "data:image/svg+xml;base64," + base64.b64encode(b"<svg onload='alert(1)'/>").decode(),
    "https://example.com/tracker.png",
    "file:///etc/passwd",
    "data:image/png;base64,not-base64!!",
    "data:image/png;base64," + base64.b64encode(b"GIF89a not a png").decode(),
    "data:image/png;base64," + base64.b64encode(jpeg_bytes()).decode(),   # declared PNG, is JPEG
])
def test_unsafe_or_mislabelled_images_are_rejected(src):
    status = _refused(_body([change(0, image={"src": src, "alt": "x"}, imageStatus="available")]))
    assert status == 422


def test_images_are_decoded_and_reencoded_as_png():
    src = "data:image/jpeg;base64," + base64.b64encode(jpeg_bytes()).decode()
    out = rr.validate_request(_body([{"k": "image", "src": src, "alt": "Page 1"}]))["model"]["blocks"][0]["src"]
    assert out.startswith("data:image/png;base64,") and out != src
    from PIL import Image
    with Image.open(io.BytesIO(base64.b64decode(out.split(",", 1)[1]))) as img:
        assert img.format == "PNG" and img.size == (60, 40)


def test_oversize_inputs_are_refused_not_truncated(monkeypatch):
    assert _refused(_body([{"k": "text", "text": "x" * (rr.MAX_STRING_CHARS + 1)}])) == 413
    assert _refused(_body([{"k": "text", "text": "x"}] * (rr.MAX_BLOCKS + 1))) == 413
    monkeypatch.setattr(rr, "MAX_IMAGE_BYTES", 200)
    assert _refused(_body([{"k": "image", "src": png(400, 300), "alt": "big"}])) == 413
    monkeypatch.setattr(rr, "MAX_IMAGE_BYTES", 4 * 1024 * 1024)
    monkeypatch.setattr(rr, "MAX_IMAGE_SIDE", 100)
    assert _refused(_body([{"k": "image", "src": png(400, 300), "alt": "wide"}])) == 413


@pytest.mark.parametrize("body", [
    _body([{"k": "script", "text": "x"}]),
    _body([{"text": "no kind"}]),
    _body([{"k": "heading", "text": "x"}], kind="estate"),
    _body([{"k": "heading", "text": "x"}], mode="everything"),
    {"kind": "file", "file": None, "mode": "full", "model": model()},
    {"kind": "scan", "file": None, "mode": "full", "model": {"blocks": "nope"}},
    {"kind": "scan", "file": None, "mode": "full", "model": [1, 2]},
])
def test_malformed_requests_are_422(body):
    assert _refused(body) == 422


def test_url_fetcher_refuses_anything_but_our_pngs_and_fonts():
    with pytest.raises(ValueError):
        rr._fetcher("file:///etc/passwd")
    with pytest.raises(ValueError):
        rr._fetcher("https://example.com/x.png")
    assert rr._fetcher(rr.FONT_REGULAR.as_uri())


# ── modes ────────────────────────────────────────────────────────────────────────────────────

LONG = "\n".join(f"line {i} " + "word " * 30 for i in range(40))


def test_reviewer_mode_shortens_only_flagged_fields_and_says_so():
    m = model([change(0, before=LONG, after="short", beforeTruncated=True, fullRef="report.docx::SC_1_1_1::0")])
    html = rr.render_html(rr.validate_request(_body(m["blocks"]))["model"], identity(), "reviewer")
    shown = re.search(r'<div class="value before">(.*?)</div>', html, re.S).group(1)
    assert shown.startswith("line 0 ") and shown.endswith("…")
    assert len(shown) <= rr.CARD_MAX_CHARS + 1 and shown.count("\n") < rr.CARD_MAX_LINES
    assert "The before text is shortened in this packet" in html
    assert "The after text" not in html        # not flagged, not shortened
    assert "record report.docx::SC_1_1_1::0" in html


def test_full_evidence_never_shortens():
    m = model([change(0, before=LONG, after="short", beforeTruncated=True, afterTruncated=True)])
    req = rr.validate_request({**_body(m["blocks"]), "mode": "full"})
    html = rr.render_html(req["model"], identity(), "full")
    assert "line 39 " in html
    assert "shortened" not in html


def test_unloaded_reviewer_decisions_are_said():
    m = model([change(0, human={"status": "pending", "loaded": False, "verdict": None, "editedValue": None})])
    html = rr.render_html(rr.validate_request(_body(m["blocks"]))["model"], identity(), "reviewer")
    assert "Awaiting confirmation (decisions not loaded)" in html


def test_stale_decision_names_both_hashes():
    m = model([change(0, human={"status": "stale", "reviewer": "ana@example.com", "at": "2026-09-10",
                                "note": "ok", "boundSha256": "a" * 64, "currentSha256": "b" * 64})])
    html = rr.render_html(rr.validate_request(_body(m["blocks"]))["model"], identity(), "reviewer")
    assert "Stale: the file changed after this decision (ana@example.com · 2026-09-10)" in html
    assert "a" * 64 in html and "b" * 64 in html


# ── long documents lay out sanely ────────────────────────────────────────────────────────────

LONG_NAME = ("department-of-accessibility-remediation/quarterly-evidence-" * 4)[:200]


@pytest.fixture(scope="module")
def long_pdf():
    blocks = [{"k": "heading", "text": "Changes to confirm " + LONG_NAME}]
    for i in range(300):
        if i and i % 30 == 0:
            # Section breaks the model marks with pageBreak: soft, so they must not strand space.
            blocks += [{"k": "pageBreak"}, {"k": "heading", "text": f"Changes, part {i // 30 + 1}"}]
        before = ("Original paragraph text " * (150 if i % 25 == 0 else 3)).strip()
        blocks.append(change(i, title=f"{LONG_NAME}-{i}", before=before,
                             location={"label": f"{LONG_NAME} · element {i}"}))
    blocks.append({"k": "heading", "text": "Remaining work"})
    blocks += [finding(i, description="Long finding text that explains the barrier. " * 12) for i in range(40)]
    blocks += [{"k": "heading", "text": "Evidence appendix"},
               {"k": "appendixTable", "id": "all", "complete": True, "totalRecords": 300, "caption": "Every saved change",
                "headers": ["Record", "File", "Before"],
                "rows": [[f"rec-{i}", f"{LONG_NAME}-{i}.docx", "value " * 20] for i in range(300)]}]
    data = render(model(blocks), mode="full")
    return data, len(pikepdf.open(io.BytesIO(data)).pages)


def page_fill(data, top_in=20 / 25.4, bottom_in=19 / 25.4):
    """For each page, how far down the body area the last line of body text reaches (0..1).
    Measured from word boxes, so a page that 'has text' but leaves its lower half empty counts.
    `top_in`/`bottom_in` are the @page margins in inches (defaults: this renderer's)."""
    from html import unescape
    html = pdftext(data, "-bbox")
    fills = []
    for page in re.finditer(r'<page width="([\d.]+)" height="([\d.]+)">(.*?)</page>', html, re.S):
        height = float(page.group(2))
        top, bottom = top_in * 72, height - bottom_in * 72
        ys = [float(m.group(2)) for m in re.finditer(r'<word xMin="[\d.]+" yMin="([\d.]+)" xMax="[\d.]+" yMax="([\d.]+)">', page.group(3))
              if top <= float(m.group(1)) and float(m.group(2)) <= bottom]
        words = " ".join(unescape(w) for w in re.findall(r"<word[^>]*>(.*?)</word>", page.group(3)))
        fills.append(((max(ys) - top) / (bottom - top) if ys else 0.0, words))
    return fills


def test_long_report_has_no_stranded_pages(long_pdf):
    data, pages = long_pdf
    fills = page_fill(data)
    assert len(fills) == pages
    appendix_page = next(i for i, (_, words) in enumerate(fills) if "Every saved change" in words)
    # Only the page before the appendix (which deliberately starts a new page) and the last page
    # may stop early. Elsewhere up to ~40% may go unused: a short card that does not fit moves
    # whole to the next page (a printed checklist item should not straddle two sheets).
    # Bite-checked: honouring pageBreak as a hard break, or keeping EVERY card whole however long,
    # both fail this (pages ending at 0-54%).
    stranded = [(i + 1, round(f, 2)) for i, (f, _) in enumerate(fills)
                if f < 0.6 and i not in (pages - 1, appendix_page - 1)]
    assert not stranded, f"pages ending early: {stranded} of {pages}"
    # 300 change cards + 40 finding cards + a 300-row appendix: sanity bound, not a golden number.
    assert 60 < pages < 260, pages


def test_long_values_wrap_instead_of_clipping(long_pdf):
    data, _ = long_pdf
    text = pdftext(data, "-raw")
    flat = re.sub(r"\s+", "", text)
    assert re.sub(r"\s+", "", LONG_NAME) + "-299" in flat


def test_table_header_repeats_on_every_page_of_a_long_table(long_pdf):
    data, pages = long_pdf
    chunks = pdftext(data).split("\f")[:pages]
    table_pages = [c for c in chunks if re.search(r"rec-\d+", c)]
    assert len(table_pages) >= 3
    for chunk in table_pages:
        assert re.search(r"Record\s+File\s+Before", chunk), chunk[:300]


# ── the real models the frontend builds ──────────────────────────────────────────────────────

FIXTURE = ACP / "tests" / "fixtures" / "report_render_js_models.json"


@pytest.mark.parametrize("name", sorted(json.loads(FIXTURE.read_text())))
def test_every_frontend_model_validates_and_renders(name):
    """Models dumped from reportModel.js / scanReport.js builders (see the fixture). A block kind
    the frontend emits and the server does not accept would 422 every download of that report."""
    kind, mode = name.split("-")
    m = json.loads(FIXTURE.read_text())[name]
    req = rr.validate_request({"kind": kind, "mode": mode, "file": "deck.pptx" if kind == "file" else None, "model": m})
    data = rr.render_pdf(req["model"], identity(kind=kind, file=req["file"]), mode)
    with pikepdf.open(io.BytesIO(data)) as doc:
        tags, _ = walk(doc)
        assert tags["H1"] == 1 and tags["H2"] >= 1 and tags["Table"] >= 1


@pytest.mark.skipif(not VERAPDF_OK, reason=NO_VERAPDF)
def test_verapdf_ua1(tmp_path, pdf_bytes):
    out = tmp_path / "render.pdf"
    out.write_bytes(pdf_bytes)
    result = validate(out)
    assert result.compliant, result.summary()

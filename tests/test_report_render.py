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
import shutil
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
# The token a client sends to say WHICH facts it built its model from (contract v2). Every body
# below carries one, so a 422 in these tests means what the test says it means and not "you forgot
# the digest". The digest's own rules get their own tests (test_facts_digest_is_mandatory,
# tests/test_report_render_routes.py::test_a_stale_facts_digest_is_409).
DIGEST = "d" * 64


def identity(kind="file", file="report.docx", record=RECORD, client=None):
    return rr.server_identity(scan_id="scan-1", kind=kind, file=file, record=record,
                              client_identity=client or model()["identity"], platform_version="2026.9.17.1")


def render(m=None, mode="reviewer", base_url="https://acp.example.com", kind="file", file="report.docx"):
    req = rr.validate_request({"kind": kind, "file": file, "mode": mode, "factsDigest": DIGEST,
                               "model": m or model()})
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
                                               "factsDigest": DIGEST,
                                               "model": model(blocks)})["model"], identity(), "summary")
    assert html.count('class="identity') == 1           # the cover does not print a second one
    assert "client-version" not in html and "0123abcd-md5-from-drive" in html
    # …and the same in the other two modes, where the cost of a duplicate is only clutter.
    for mode in ("reviewer", "full"):
        other = rr.render_html(rr.validate_request({"kind": "file", "file": "report.docx", "mode": mode,
                                                    "factsDigest": DIGEST,
                                                    "model": model(blocks)})["model"], identity(), mode)
        assert other.count('class="identity') == 1, mode


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
    return {"kind": "file", "file": "report.docx", "mode": "reviewer", "factsDigest": DIGEST,
            "model": model(blocks), **over}


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
    {"kind": "file", "file": None, "mode": "full", "factsDigest": DIGEST, "model": model()},
    {"kind": "scan", "file": None, "mode": "full", "factsDigest": DIGEST, "model": {"blocks": "nope"}},
    {"kind": "scan", "file": None, "mode": "full", "factsDigest": DIGEST, "model": [1, 2]},
])
def test_malformed_requests_are_422(body):
    assert _refused(body) == 422


@pytest.mark.parametrize("digest", [None, "", "not-a-digest", "d" * 63, "d" * 65, "g" * 64, 12345,
                                    ["d" * 64]])
def test_facts_digest_is_mandatory_and_must_be_a_sha256(digest):
    """No digest, no render. The server stamps ITS identity onto the client's model, so a model
    built before the document changed would otherwise be printed under the checksums the document
    has now — the one failure mode nothing on the page would reveal."""
    body = _body([{"k": "heading", "text": "x"}])
    if digest is None:
        body.pop("factsDigest")
    else:
        body["factsDigest"] = digest
    assert _refused(body) == 422


def test_the_digest_is_returned_normalised():
    req = rr.validate_request(_body([{"k": "heading", "text": "x"}], factsDigest="  " + "AB" * 32 + " "))
    assert req["factsDigest"] == "ab" * 32


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
    assert "Stale: the file changed after this decision" in html
    assert "(ana@example.com · 2026-09-10)" in html
    assert "a" * 64 in html and "b" * 64 in html


# ── what the evidence actually says ──────────────────────────────────────────────────────────
#
# Each of these was wrong in the first pass, and each wrong in the same direction: a record that
# nobody had confirmed read as confirmed. That is the failure mode worth testing, because the
# reader of a PDF has no way to check it.

def _card_html(**over):
    m = model([change(0, **over)])
    return rr.render_html(rr.validate_request(_body(m["blocks"]))["model"], identity(), "reviewer")


def test_an_unverified_ai_change_says_so_in_those_words():
    html = _card_html(verification="not_verified",
                      verificationDetail="Saved to the corrected copy. No re-scan result recorded.",
                      technical={"status": "pending", "detail": "ignored when verification is given"})
    assert "AI applied · not verified" in html
    assert "No re-scan result recorded." in html
    assert "ignored when verification is given" not in html


def test_a_verified_change_is_not_flagged():
    html = _card_html(verification="verified", verificationDetail="A remediation_diff record exists.")
    assert "Verified by re-scan" in html and "AI applied" not in html


@pytest.mark.parametrize("status", ["edited", "correction_requested"])
def test_a_requested_correction_is_never_reported_as_accepted(status):
    """change_review's PUT records a PROPOSED value; it does not write bytes. "Accepted with
    edits" claimed an edit that exists nowhere."""
    html = _card_html(human={"status": status, "reviewer": "ana@example.com", "at": "2026-09-16",
                             "note": None, "editedValue": "A clearer description",
                             "boundSha256": None, "currentSha256": None})
    assert "Correction requested — not applied" in html
    assert "Accepted with edits" not in html
    assert "Correction the reviewer asked for — not applied" in html
    assert "A clearer description" in html


def test_unknown_freshness_is_not_a_confirmation():
    html = _card_html(human={"status": "freshness_unknown", "reviewer": "ana@example.com",
                             "at": "2026-09-16", "note": None, "boundSha256": "e" * 64,
                             "currentSha256": None})
    assert "Freshness unknown — recheck" in html
    assert "e" * 64 in html and "is not recorded" in html
    assert ">Accepted<" not in html


def test_none_of_the_new_evidence_keys_are_refused():
    """A key the frontend emits and the server rejects is a 422 on every download of that report,
    so the new fields are accepted, not merely tolerated."""
    body = _body([change(0, verification="not_verified", verificationDetail="d", valueClipped=True,
                         changeDigest="9" * 64, findingIds=["abc"], source="unverified_changes",
                         human={"status": "correction_requested", "loaded": False,
                                "editedValue": "x", "boundSha256": None, "currentSha256": None}),
                  finding(0, recommendedAction="Add a text alternative naming the clinic.",
                          ledgerFindingId=None, state="open", stateReason="Present in the "
                          "current assessment")])
    req = rr.validate_request(body)
    assert len(req["model"]["blocks"]) == 2


def test_a_store_clipped_value_does_not_promise_the_full_text_elsewhere():
    """A value the STORE truncated at its 2,000-character cap is not in the Full evidence report
    either — it is not held anywhere. Pointing a reader there would send them for something that
    has never existed."""
    html = _card_html(beforeStoredClipped=True, before="x" * 2000)
    assert "clipped by the store" in html
    assert "The full text is in the Full evidence report" not in html


def test_a_recommended_action_is_printed_verbatim():
    action = "Add a text alternative describing the referral pathway; mark decorative only if the caption repeats it."
    m = model([finding(0, recommendedAction=action)])
    html = rr.render_html(rr.validate_request(_body(m["blocks"]))["model"], identity(), "reviewer")
    assert escape_ok(action) in html


def escape_ok(text):
    from html import escape as _e
    return _e(text, quote=True)


# ── nothing is fetched, ever ─────────────────────────────────────────────────────────────────

def test_a_model_full_of_urls_makes_no_network_request(monkeypatch):
    """The bite check the fetcher's unit test cannot make: render a whole document whose model is
    full of https images, https links and @import-shaped text, record every URL WeasyPrint asks
    for, and forbid the socket layer outright. A URL the renderer resolved would either appear in
    the log or raise from socket()."""
    import socket
    import weasyprint

    asked = []
    real = rr._fetcher

    def spy(url, *args, **kwargs):
        asked.append(url)
        return real(url, *args, **kwargs)

    class NoNetwork(socket.socket):
        def __init__(self, *a, **k):
            raise AssertionError("the report renderer opened a socket")

    monkeypatch.setattr(rr, "_fetcher", spy)
    monkeypatch.setattr(socket, "socket", NoNetwork)
    monkeypatch.setattr(socket, "create_connection",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("connect attempted")))

    hostile = [
        {"k": "heading", "text": "@import url('https://evil.example/x.css');"},
        {"k": "text", "text": "<style>@import url(https://evil.example/y.css)</style>"},
        {"k": "text", "text": "url(https://evil.example/z.png) and file:///etc/passwd"},
        {"k": "link", "text": "Remote", "href": "https://evil.example/tracker"},
        {"k": "link", "text": "App", "href": "/scans/s1"},
        change(0, image={"src": png(), "alt": "Embedded", "caption": "Re-encoded by Pillow"},
               imageStatus="available"),
        {"k": "table", "caption": "https://evil.example/caption.png", "headers": ["A"],
         "rows": [["https://evil.example/cell.png"]]},
    ]
    req = rr.validate_request(_body(hostile))
    pdf_data = weasyprint.HTML(string=rr.render_html(req["model"], identity(), "reviewer",
                                                     "https://acp.example.com"),
                               url_fetcher=rr._fetcher).write_pdf(pdf_variant="pdf/ua-1")
    assert pdf_data.startswith(b"%PDF")
    assert asked, "the fetcher was never called — this test would pass for the wrong reason"
    for url in asked:
        assert url.startswith("data:image/png;base64,") or url in rr._FONT_URLS, url
    assert not any("evil.example" in u for u in asked)
    # The URLs are still printed as literal text, escaped — dropped links must not become silence.
    text = pdftext(pdf_data)
    assert "https://evil.example/x.css" in text and "@import" in text


def test_an_https_image_in_the_model_is_refused_before_any_render():
    assert _refused(_body([{"k": "image", "src": "https://evil.example/pixel.png", "alt": "x"}])) == 422
    assert _refused(_body([change(0, imageStatus="available",
                                  image={"src": "https://evil.example/p.png", "alt": "x"})])) == 422


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
#
# These are not models written in this file. tests/fixtures/build_report_render_js_models.mjs runs
# frontend/src/reportModel.js and frontend/src/scanReport.js — the actual builders, as ES modules,
# not a copy — over facts-shaped input, and writes what they return. A renderer-only fake would
# stay green through a block kind the frontend emits and the server refuses, which is a 422 on
# every download of that report; test_the_fixture_matches_the_builders re-runs the generator and
# fails when the checked-in JSON has drifted.

FIXTURE = ACP / "tests" / "fixtures" / "report_render_js_models.json"
GENERATOR = ACP / "tests" / "fixtures" / "build_report_render_js_models.mjs"
MODELS = json.loads(FIXTURE.read_text())
NODE = shutil.which("node") or next(
    (p for p in ("/opt/homebrew/bin/node", "/usr/local/bin/node", "/usr/bin/node",
                 str(Path.home() / ".nvm/versions/node/current/bin/node")) if Path(p).exists()), None)

# The case names encode kind, data shape and mode: "file-long-summary".
CASES = sorted(MODELS)
SUMMARY_CASES = [n for n in CASES if n.endswith("-summary")]
LONG_PATH = (MODELS["file-long-summary"].get("identity") or {}).get("file")


def fixture_render(name):
    kind, case, mode = name.split("-")
    model_ = MODELS[name]
    file = (model_.get("identity") or {}).get("file") if kind == "file" else None
    # A case that recorded nothing gets a store record that holds nothing: no checksum, no
    # corrected copy, no build stamp. Handing it hashes would hide the very thing it is for.
    blank = case == "missing"
    req = rr.validate_request({"kind": kind, "mode": mode, "file": file,
                               "factsDigest": DIGEST, "model": model_})
    ident = rr.server_identity(
        scan_id="scan-2026-09-17-a", kind=kind, file=req["file"], record={} if blank else RECORD,
        client_identity={"targetLevel": "AA"},
        platform_version=None if blank else "2026.9.17.3")
    return rr.render_pdf(req["model"], ident, mode)


@pytest.fixture(scope="module")
def rendered():
    return {}


def fixture_pdf(rendered, name):
    if name not in rendered:
        rendered[name] = fixture_render(name)
    return rendered[name]


def test_the_fixture_matches_the_builders(tmp_path):
    """Re-run the generator and diff. A builder change that alters the model must land in the
    fixture in the same commit, or the renderer is being tested against a model nobody ships."""
    if NODE is None:
        pytest.skip("node is not installed; the fixture cannot be checked against the builders")
    proc = subprocess.run([NODE, str(GENERATOR)], capture_output=True, cwd=str(ACP))
    if proc.returncode != 0:
        pytest.skip(f"node could not run the generator: {proc.stderr.decode()[:400]}")
    current = json.loads(FIXTURE.read_text())
    assert set(current) == set(MODELS), "cases changed; re-read the fixture"
    drifted = sorted(k for k in current if current[k] != MODELS[k])
    assert not drifted, (f"the frontend builders now return different models for {drifted}. "
                         f"Run: node {GENERATOR.relative_to(ACP)}")


@pytest.mark.parametrize("name", CASES)
def test_every_frontend_model_validates_and_renders(name, rendered):
    data = fixture_pdf(rendered, name)
    with pikepdf.open(io.BytesIO(data)) as doc:
        tags, _ = walk(doc)
        assert tags["H1"] == 1 and tags["H2"] >= 1 and tags["Table"] >= 1
        assert "/StructTreeRoot" in doc.Root and str(doc.Root.Lang) == "en-US"
        assert doc.Root.get("/MarkInfo") is not None and bool(doc.Root.MarkInfo.Marked)
        assert str(doc.open_metadata().get("dc:title") or doc.docinfo.get("/Title") or "")


@pytest.mark.parametrize("name", SUMMARY_CASES)
def test_summary_mode_is_one_page_for_every_kind(name, rendered):
    """Contract v2: summary is ONE page, for file, scan and remediation reports alike, including
    the 30-change case and the one with a 150-character path. It got there by printing less, not
    by printing smaller — test_the_summary_does_not_shrink_the_type is the other half."""
    with pikepdf.open(io.BytesIO(fixture_pdf(rendered, name))) as doc:
        assert len(doc.pages) == 1, f"{name} is {len(doc.pages)} pages"


@pytest.mark.parametrize("name", SUMMARY_CASES)
def test_the_summary_prints_identity_exactly_once(name, rendered):
    """A "Report identity" table on page 1 above a "Document identity" table on page 2 is the
    same eight facts twice, and it is half of why the summary used to run to two pages."""
    text = pdftext(fixture_pdf(rendered, name))
    body = text.split("\f")[0]
    assert body.count("Artifact version") == 1, body
    assert body.count("Platform version") == 1, body


@pytest.mark.parametrize("name", SUMMARY_CASES)
def test_the_summary_leaves_out_method_and_says_where_the_rest_is(name, rendered):
    text = pdftext(fixture_pdf(rendered, name))
    flat = re.sub(r"\s+", " ", text)
    for gone in ("What this report is, and is not", "About this PDF",
                 "This PDF is generated with a tagged structure"):
        assert gone not in flat, f"{name} still prints {gone!r}"
    assert "Full evidence report" in flat, f"{name} does not say where the rest of the record is"


def test_the_summary_does_not_shrink_the_type():
    """The one-page rule must not be met by making the page unreadable. Body text in a summary is
    the same size as in the full report; only spacing and layout differ."""
    full = pdftext(fixture_render("file-base-full"), "-bbox")
    summary = pdftext(fixture_render("file-base-summary"), "-bbox")

    def body_line_heights(bbox):
        return sorted(round(float(m.group(2)) - float(m.group(1)), 1) for m in
                      re.finditer(r'<word xMin="[\d.]+" yMin="([\d.]+)" xMax="[\d.]+" yMax="([\d.]+)">', bbox))

    a, b = body_line_heights(full), body_line_heights(summary)
    assert a and b
    # Compare the commonest glyph height (the body face) in each.
    assert Counter(a).most_common(1)[0][0] == Counter(b).most_common(1)[0][0], (a[:5], b[:5])


def test_the_missing_evidence_case_says_not_recorded_and_never_invents_a_zero(rendered):
    """A document nothing has opened must read "Not recorded", not "0". A zero is an assertion
    that somebody looked; it is exactly the wrong way round for a report to be wrong."""
    # -layout keeps a table row on one line, so "field … value" can be read as a pair.
    text = pdftext(fixture_pdf(rendered, "file-missing-summary"), "-layout")
    identity_block = text.split("Document and report identity", 1)[1]
    for field in ("Source checksum", "Corrected copy SHA-256", "Platform version"):
        line = next((ln for ln in identity_block.splitlines() if ln.strip().startswith(field)), None)
        assert line and "Not recorded" in line, (field, line)
    # And the counts a never-assessed document cannot honestly report are not zeros.
    summary_block = text.split("Decision evidence", 1)[1].split("Document and report identity")[0]
    for measure in ("Documents assessed", "Edits saved", "Findings verified resolved"):
        line = next((ln for ln in summary_block.splitlines() if ln.strip().startswith(measure)), None)
        assert line and "Not recorded" in line, (measure, line)


@pytest.mark.parametrize("name", ["file-large-full", "file-large-reviewer", "scan-large-full",
                                  "remediation-large-full"])
def test_the_large_cases_are_multi_page_and_numbered(name, rendered):
    data = fixture_pdf(rendered, name)
    with pikepdf.open(io.BytesIO(data)) as doc:
        pages = len(doc.pages)
    assert pages > 3, f"{name} is only {pages} pages; it is meant to be the long case"
    text = pdftext(data)
    assert f"Page {pages} of {pages}" in text
    assert "Page 1 of %d" % pages in text
    # Nothing off the page, no replacement glyphs, no broken bookmark tree.
    assert "�" not in text
    with pikepdf.open(io.BytesIO(data)) as doc:
        assert "/Outlines" in doc.Root and int(doc.Root.Outlines.get("/Count", 0)) > 0


@pytest.mark.parametrize("name", ["file-long-summary", "file-long-reviewer", "file-long-full"])
def test_a_long_path_is_printed_whole_not_clipped(name, rendered):
    flat = re.sub(r"\s+", "", pdftext(fixture_pdf(rendered, name), "-raw"))
    assert LONG_PATH
    assert re.sub(r"\s+", "", LONG_PATH) in flat


@pytest.mark.parametrize("name", ["file-base-full", "scan-base-full", "remediation-base-full"])
def test_fonts_are_embedded_in_every_kind(name, rendered):
    with pikepdf.open(io.BytesIO(fixture_pdf(rendered, name))) as doc:
        fonts = [f for page in doc.pages
                 for f in (page.get("/Resources", {}).get("/Font", {}) or {}).values()]
        assert fonts
        for font in fonts:
            descendant = (font.get("/DescendantFonts") or [font])[0]
            desc = descendant.get("/FontDescriptor")
            assert desc is not None and any(k in desc for k in ("/FontFile", "/FontFile2", "/FontFile3")), font


@pytest.mark.skipif(not VERAPDF_OK, reason=NO_VERAPDF)
@pytest.mark.parametrize("name", ["file-base-summary", "file-base-reviewer", "file-base-full",
                                  "scan-base-summary", "scan-base-reviewer", "scan-base-full",
                                  "remediation-base-summary", "remediation-base-reviewer",
                                  "remediation-base-full", "file-large-full"])
def test_verapdf_ua1_over_every_kind_and_mode(tmp_path, name, rendered):
    """veraPDF's AUTOMATED PDF/UA-1 checks over each kind x mode of the real models.

    What this establishes: the machine-checkable clauses — tagged structure, /Lang, document
    title and DisplayDocTitle, embedded fonts, figures with alternate text, no untagged content.
    What it does NOT establish, and what the report itself never claims: PDF/UA CONFORMANCE.
    Clauses about whether a heading level is the RIGHT one, whether an alt text is meaningful, and
    whether reading order matches the visual order are not machine-decidable; veraPDF reports them
    as human-verification items. A screen-reader pass and a PAC 2024 check remain manual.
    """
    out = tmp_path / f"{name}.pdf"
    out.write_bytes(fixture_pdf(rendered, name))
    result = validate(out)
    assert result.compliant, result.summary()


@pytest.mark.skipif(not VERAPDF_OK, reason=NO_VERAPDF)
def test_verapdf_ua1(tmp_path, pdf_bytes):
    out = tmp_path / "render.pdf"
    out.write_bytes(pdf_bytes)
    result = validate(out)
    assert result.compliant, result.summary()


# ── S5: the appendix reads the key every builder writes (`limitNote`) ─────────────────────────

def _appendix_html(block):
    req = rr.validate_request({"kind": "scan", "file": None, "mode": "full", "factsDigest": DIGEST,
                               "model": {"docTitle": "Scan", "blocks": [block]}})
    return rr.render_html(req["model"], identity(kind="scan", file=None), "full")


def test_an_incomplete_appendix_names_the_builders_own_reason_and_never_says_3_of_3():
    """Reproduced first: this block (the shape scanReport.js writes for the per-document index when
    the facts index stopped early) rendered "Partial: 3 of 3 records (source limited to the records
    the source returned to this report)" — a contradiction, with the real reason dropped."""
    html = _appendix_html({"k": "appendixTable", "id": "scan_index", "complete": False,
                           "totalRecords": 3, "limitNote": "the documents included in the file list",
                           "caption": "Index", "headers": ["File"], "rows": [["a"], ["b"], ["c"]]})
    assert "Partial: 3 of 3" not in html
    assert "the documents included in the file list" in html
    assert "Incomplete record set: all 3 counted records are shown" in html


def test_a_genuinely_partial_appendix_prints_shown_of_total_with_the_limit_note():
    html = _appendix_html({"k": "appendixTable", "id": "remediation_diff", "complete": False,
                           "totalRecords": 40,
                           "limitNote": "the page of remediation records the server returned",
                           "caption": "Saved changes", "headers": ["Id"], "rows": [["a"], ["b"]]})
    assert ("Partial: 2 of 40 records (source limited to the page of remediation records the "
            "server returned).") in html


def test_legacy_limit_keys_still_read_and_an_unknown_total_is_said():
    html = _appendix_html({"k": "appendixTable", "id": "x", "complete": False, "totalRecords": None,
                           "sourceLimit": "the first page", "headers": ["Id"], "rows": [["a"]]})
    assert "Partial: 1 of an unknown number of records (source limited to the first page)." in html


# ── links in a downloaded PDF are ABSOLUTE and point at ACP's own origin ───────────────────────

APP = "https://acp.example.com"


def _linked_card_html(href, base_url):
    m = model([finding(0, location={"label": "Page 3", "page": 3, "href": href})])
    req = rr.validate_request({"kind": "file", "file": "report.docx", "mode": "full",
                               "factsDigest": DIGEST, "model": m})
    return rr.render_html(req["model"], identity(), "full", base_url)


def test_a_relative_evidence_link_is_absolutized_against_the_trusted_origin():
    rel = "/?view=evidence&scan=scan-1&file=Policies%2Fa+b.docx&finding=abc"
    html = _linked_card_html(rel, APP)
    assert (f'href="{APP}/?view=evidence&amp;scan=scan-1&amp;file=Policies%2Fa+b.docx'
            '&amp;finding=abc"') in html


def test_with_no_trusted_origin_a_location_is_text_and_never_a_relative_link():
    html = _linked_card_html("/?view=evidence&scan=scan-1&file=a.docx&finding=abc", None)
    main = html.split("<main")[1]
    assert 'href="/' not in main and "view=evidence" not in main
    assert "Page 3" in main
    # an origin that is not https (and not loopback) is not trusted either
    main = _linked_card_html("/?view=evidence&scan=s&file=f&finding=x", "http://acp.example.com").split("<main")[1]
    assert "view=evidence" not in main and "Page 3" in main


@pytest.mark.parametrize("href", [
    "https://evil.example/?view=evidence&scan=s&file=f&finding=x",   # another host
    "https://acp.example.com.evil.example/?view=evidence",           # lookalike host
    "http://acp.example.com/?view=evidence",                         # downgraded scheme
    "//evil.example/?view=evidence",                                 # protocol-relative
    "javascript:alert(1)",
])
def test_an_absolute_or_unsafe_location_href_is_never_promoted_to_an_app_link(href):
    main = _linked_card_html(href, APP).split("<main")[1]
    assert 'href="https://evil' not in main and 'href="https://acp.example.com.evil' not in main
    assert 'href="http://' not in main and 'href="//' not in main and "javascript" not in main
    assert "Page 3" in main


def test_a_record_with_no_location_links_as_the_record_not_as_a_place():
    m = model([finding(0, location={"label": None, "kind": None, "href": "/?view=evidence&scan=s&file=f&finding=x"})])
    req = rr.validate_request({"kind": "file", "file": "report.docx", "mode": "full", "factsDigest": DIGEST, "model": m})
    html = rr.render_html(req["model"], identity(), "full", APP)
    assert (f'Location not recorded · <a href="{APP}/?view=evidence&amp;scan=s&amp;file=f&amp;finding=x">'
            "open this record in ACP</a>") in html
    # with no trusted origin: the words only, no dangling link text
    html = rr.render_html(req["model"], identity(), "full", None)
    main = html.split("<main")[1]
    assert "Location not recorded" in main and "open this record in ACP" not in main


def test_a_same_origin_absolute_location_href_is_kept():
    html = _linked_card_html(f"{APP}/?view=evidence&scan=s&file=f&finding=x", APP)
    assert f'href="{APP}/?view=evidence&amp;scan=s&amp;file=f&amp;finding=x"' in html


@pytest.mark.parametrize("cand,expected", [
    ("https://acp.example.com/", "https://acp.example.com"),
    ("https://acp.example.com/app/", "https://acp.example.com/app"),
    ("http://localhost:5173", "http://localhost:5173"),
    ("http://acp.example.com", None),
    ("https://user:pw@acp.example.com", None),
    ("https://acp.example.com/?x=1", None),
    ("ftp://acp.example.com", None),
    ("", None),
    (None, None),
])
def test_trusted_app_origin(cand, expected):
    assert rr.trusted_app_origin(cand) == expected


def _comparison_html(block):
    req = rr.validate_request({"kind": "file", "file": "report.docx", "mode": "full", "factsDigest": DIGEST,
                               "model": model([block])})
    return rr.render_html(req["model"], identity(), "full")


def test_a_reopened_finding_is_listed_as_its_own_category():
    html = _comparison_html({"k": "comparison", "status": "compared", "reason": "Matched finding by finding.",
                             "previous": {"scanId": "s0"}, "resolved": [], "persisting": 2,
                             "introduced": [{"id": "n1", "title": "1.4.3 · Contrast", "location": {"label": "Page 2"}}],
                             "reopened": [{"id": "r1", "title": "1.1.1 · Non-text Content", "location": {"label": "Image 3"}}],
                             "notComparable": {"current": 1, "previous": 2}})
    assert "Reported again after being resolved: 1" in html and "Image 3" in html
    assert "Newly reported: 1" in html
    assert "counted as neither new nor resolved: 1 now, 2 before" in html


def test_reopened_unclassified_is_not_printed_as_zero():
    html = _comparison_html({"k": "comparison", "status": "compared", "reason": "x", "resolved": [],
                             "introduced": [], "persisting": 0, "reopened": None})
    assert "Reported again after being resolved" not in html


def test_estate_comparison_prints_totals_and_keeps_unknown_as_not_recorded():
    html = _comparison_html({"k": "comparison", "status": "unknown", "scope": "estate",
                             "reason": "Some documents had no earlier assessment.",
                             "totals": {"introduced": 4, "resolved": 1, "persisting": 9, "reopened": None,
                                        "notComparable": 2, "filesCompared": 3, "filesNoBaseline": 5,
                                        "filesBaselineUnusable": 1, "filesNew": 0}})
    assert "Change since the previous assessment, across documents" in html
    assert '<th scope="row">Newly reported</th><td>4</td>' in html
    assert '<th scope="row">Reported again after being resolved</th><td>Not recorded</td>' in html
    assert '<th scope="row">Documents with no earlier assessment</th><td>5</td>' in html


def test_the_summary_prints_comparison_counts_not_lists_and_stays_on_one_page():
    """Measured with facts-driven models (PDF QA): a real comparison's item lists pushed a file
    Summary onto a second page. The decision page carries the counts; the lists are named as left
    out and live in the Reviewer packet."""
    items = [{"id": f"n{i}", "title": "1.4.3 · Contrast (Minimum)", "location": {"label": f"Page {i + 2}"}}
             for i in range(12)]
    blocks = [
        {"k": "heading", "text": "Decision summary"},
        {"k": "callout", "text": "Outstanding before publication: " + "a finding to fix; " * 30},
        {"k": "decisionSummary", "caption": "Decision evidence", "items": [
            {"key": "documentsAssessed", "label": "Documents assessed", "value": 1, "detail": "x " * 40}] * 6},
        {"k": "heading", "text": "Since the previous assessment"},
        {"k": "comparison", "status": "compared", "reason": "Matched finding by finding.",
         "previous": {"scanId": "s0", "generatedAt": "2026-08-14T05:05:00+00:00"},
         "introduced": items, "resolved": items[:5], "persisting": 11, "reopened": None,
         "notComparable": {"current": 2, "previous": 1}},
    ]
    pdf = render(model(blocks), mode="summary")
    with pikepdf.open(io.BytesIO(pdf)) as doc:
        assert len(doc.pages) == 1
    text = " ".join(pdftext(pdf).split())
    assert "Since the previous assessment (2026-08-14): 12 newly reported; 5 no longer reported; 11 still reported; 2 now and 1 before not comparable." in text
    assert "Page 13" not in text                        # the item list is not on the decision page
    assert "finding-by-finding comparison with the previous assessment" in text
    # …and the Reviewer packet still lists every item
    reviewer = " ".join(pdftext(render(model(blocks), mode="reviewer")).split())
    assert "Newly reported: 12" in reviewer and "Page 13" in reviewer


def test_trusted_app_origin_falls_through_to_the_next_candidate():
    assert rr.trusted_app_origin("", "http://bad.example", "https://acp.example.com") == APP

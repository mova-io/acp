"""Page-1 raster preview for a document (ADR 0015).

A pure, dependency-light renderer: bytes in, PNG bytes out — no DB, no FastAPI, no
network, so it unit-tests against a corpus file directly. The owner-check, source-byte
resolution, and blob caching live in the endpoint (routes/scans.py); this module only
turns a document's bytes into a thumbnail.

Renderer: pypdfium2 (Google's pdfium — BSD-3-Clause/Apache-2.0, permissive; a pure pip
wheel bundling the binary, already pulled in transitively by pdfplumber). Pillow encodes
the bitmap to PNG. PyMuPDF (AGPL) and poppler (GPL, via pdf2image) are deliberately avoided
— see ADR 0015.

Contract: render_page1_png NEVER raises. Any failure — unsupported type, corrupt or
encrypted PDF, a pdfium error — returns None, so a missing thumbnail can never break scan,
remediate, or report.
"""
from __future__ import annotations
import io
import os

# Longest-edge cap for the output PNG. A page-1 preview, not a viewer — this keeps the
# cached object to tens of KB and is ample for the HITL surfaces and the certification PDF.
_MAX_EDGE = 1000
# pdfium renders at 72*scale DPI; 2.0 → 144 DPI, enough to downscale from cleanly.
_RENDER_SCALE = 2.0

# Extensions this module can always rasterize (pypdfium2, no external binary).
RENDERABLE_EXTS = (".pdf",)
# Office formats (ADR 0018): rasterized by converting to PDF with headless LibreOffice, then
# rendering the PDF with pdfium. Only available when `soffice` is on PATH (the deploy image adds
# it); everywhere it isn't — local dev without LibreOffice, a build that omits it — these degrade
# to None (no preview), exactly like an unsupported type. LibreOffice is MPL/LGPL and invoked as a
# subprocess (the tesseract precedent), never linked, so the license posture is unchanged.
_OFFICE_EXTS = (".docx", ".pptx", ".xlsx")


def _soffice() -> str | None:
    """Path to the headless LibreOffice binary, or None when it isn't installed."""
    import shutil
    return shutil.which("soffice") or shutil.which("libreoffice")


def can_render(ext: str) -> bool:
    """Whether render_page_png can (attempt to) rasterize this extension in this environment."""
    e = (ext or "").lower()
    return e in RENDERABLE_EXTS or (e in _OFFICE_EXTS and _soffice() is not None)


def office_render_enabled() -> bool:
    """Whether the heavy Office→PDF render path (ADR 0018/0024) is switched on. Defaults ON
    wherever LibreOffice is present — matching the existing thumbnail behaviour — but
    `ACP_OFFICE_RENDER=0` force-disables it, so the render/worker image can shed the cost of
    Tier-B on-demand rasterization without a code change. PDF rendering (no LibreOffice) is
    unaffected."""
    v = os.environ.get("ACP_OFFICE_RENDER")
    if v is None:
        return True
    return v.strip().lower() not in ("0", "false", "no", "off")


def render_page_png(data: bytes, ext: str, page: int = 1) -> bytes | None:
    """Render page N (1-indexed) of a document to a PNG, downscaled to _MAX_EDGE on the long
    side. `page` is CLAMPED to the document's real range, so an out-of-range request returns
    the nearest valid page rather than nothing — right for an orientation thumbnail, WRONG for a
    picture presented as the page a finding sits on: use render_exact_page_png for that.

    Returns the PNG bytes, or None for anything we can't render (non-PDF, empty, corrupt,
    encrypted, or any pdfium/Pillow error). Never raises — a preview is best-effort."""
    if not data or not can_render(ext):
        return None
    try:
        e = (ext or "").lower()
        # Office → PDF first (LibreOffice), then the same pdfium path renders the page.
        pdf_bytes = data if e in RENDERABLE_EXTS else _office_to_pdf(data, e)
        if not pdf_bytes:
            return None
        return _render_pdf_page(pdf_bytes, page)
    except Exception:
        # Corrupt bytes, password-protected PDF, a LibreOffice/pdfium/Pillow error — all collapse
        # to "no preview". Intentionally broad: this must never propagate (ADR 0015/0018).
        return None


def render_page1_png(data: bytes, ext: str) -> bytes | None:
    """Page-1 preview (ADR 0015). Back-compat wrapper over render_page_png."""
    return render_page_png(data, ext, 1)


# ── Exact pages ──────────────────────────────────────────────────────────────
# render_page_png CLAMPS, which is right for an orientation thumbnail and wrong for evidence: on a
# one-page PDF it returns byte-identical PNGs for page 1 and page 999, so a preview captioned
# "page 99" was a picture of the last page. Anything that shows a page AS the page a finding sits
# on goes through render_exact_page_png, which refuses a page the document does not have and says
# how many it does have. render_page_png itself is unchanged: the generic preview route relies on
# the clamp, and its output is never presented as an exact page.

def _as_pdf(data: bytes, ext: str) -> bytes | None:
    """The document as PDF bytes (itself for a PDF, a LibreOffice conversion for Office)."""
    if not data or not can_render(ext):
        return None
    e = (ext or "").lower()
    return data if e in RENDERABLE_EXTS else _office_to_pdf(data, e)


def _pdf_page_count(pdf_bytes: bytes) -> int | None:
    import pypdfium2 as pdfium
    pdf = pdfium.PdfDocument(pdf_bytes)
    try:
        return len(pdf)
    finally:
        pdf.close()


def page_count(data: bytes, ext: str) -> int | None:
    """How many rendered pages (slides, for pptx) the document has, or None when it cannot be
    rendered here. Never raises."""
    try:
        pdf_bytes = _as_pdf(data, ext)
        return _pdf_page_count(pdf_bytes) if pdf_bytes else None
    except Exception:
        return None


def render_exact_page_png(data: bytes, ext: str, page: int) -> dict:
    """Page `page` (1-indexed) of the document, or an explicit refusal. NEVER a substitute page.

    Returns {"png": bytes|None, "page": int, "pages": int|None, "reason": None|str}:
      reason None            png is exactly that page;
      reason "out_of_range"  the document has `pages` pages and `page` is not one of them;
      reason "unrenderable"  the type, the bytes or the renderer failed (pages may be None).
    Never raises, for the same reason render_page_png does not."""
    try:
        want = int(page)
    except (TypeError, ValueError):
        return {"png": None, "page": page, "pages": None, "reason": "out_of_range"}
    try:
        pdf_bytes = _as_pdf(data, ext)
        pages = _pdf_page_count(pdf_bytes) if pdf_bytes else None
    except Exception:
        pdf_bytes, pages = None, None
    if not pages:
        return {"png": None, "page": want, "pages": None, "reason": "unrenderable"}
    if want < 1 or want > pages:
        return {"png": None, "page": want, "pages": pages, "reason": "out_of_range"}
    # In range, so render_page_png's clamp is a no-op here. Going through it (with the already
    # converted PDF) rather than straight to _render_pdf_page keeps one render seam for tests and
    # keeps the Office conversion to a single LibreOffice run.
    png = render_page_png(pdf_bytes, ".pdf", want)
    return {"png": png or None, "page": want, "pages": pages,
            "reason": None if png else "unrenderable"}


_OFFICE_CONVERT_TIMEOUT = float(os.environ.get("ACP_OFFICE_RENDER_TIMEOUT", "60"))


def _office_to_pdf(data: bytes, ext: str) -> bytes | None:
    """Convert Office bytes → PDF with headless LibreOffice, bounded by a timeout. Returns the PDF
    bytes or None. A FRESH UserInstallation profile per call avoids the single-profile lock that
    makes concurrent headless `soffice` runs fail. Never lets a subprocess error escape — the outer
    render_page_png guard also catches, but a failed convert must simply mean 'no preview'."""
    soffice = _soffice()
    if not soffice:
        return None
    import subprocess
    import tempfile
    from pathlib import Path
    with tempfile.TemporaryDirectory(prefix="acp-office-render-") as d:
        src = Path(d) / f"in{ext}"
        try:
            src.write_bytes(data)
            subprocess.run(
                [soffice, "--headless", "--norestore", "--nolockcheck",
                 f"-env:UserInstallation=file://{Path(d) / 'lo-profile'}",
                 "--convert-to", "pdf", "--outdir", d, str(src)],
                capture_output=True, timeout=_OFFICE_CONVERT_TIMEOUT, check=False)
        except Exception:
            return None
        out = Path(d) / "in.pdf"
        return out.read_bytes() if out.exists() else None


def _render_pdf_page(data: bytes, page: int) -> bytes | None:
    import pypdfium2 as pdfium
    from PIL import Image

    pdf = pdfium.PdfDocument(data)  # raises on encrypted/corrupt — caught by render_page_png
    try:
        n = len(pdf)
        if n == 0:
            return None
        idx = max(0, min(n - 1, int(page or 1) - 1))   # 1-indexed page → clamped 0-indexed
        bitmap = pdf[idx].render(scale=_RENDER_SCALE)
        img = bitmap.to_pil().convert("RGB")
    finally:
        pdf.close()

    w, h = img.size
    longest = max(w, h)
    if longest > _MAX_EDGE:
        ratio = _MAX_EDGE / longest
        img = img.resize((max(1, round(w * ratio)), max(1, round(h * ratio))), Image.LANCZOS)

    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()
